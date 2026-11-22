#!/usr/bin/env python3
"""HTTP API for the Next.js frontend (web/). Runs natively: `uvicorn api.main:app --port 8000`.

It wraps the existing Python modules: llm_backends / model_registry (models), agent
(chat turns), projects, connectors, tools (skills), sessions, meetings (the local
transcriber service), speaker_id + people (speaker labels), summaries (minutes).

Long operations (chat turns, summaries, speaker identification) stream Server-Sent
Events: each `data:` line is a JSON event; the last one is `done` or `error`.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import queue
import re
import uuid
import subprocess
import threading
import time
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable

import requests
from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import PlainTextResponse, Response, StreamingResponse
from pydantic import BaseModel, Field

import agent
import claude_import
import cloud_providers
import connector_directory
import connectors
import imports
import llm_backends
import long_context
import meeting_check
import meeting_notes
import meetings
import memory_store
import model_registry
import notes_export
import people as people_store
import projects
import search_index
import secrets_store
import sessions
import speaker_guess
import speaker_id
import speaker_review
import summaries
import tools
import web_providers
from doc_extract import UnsupportedDocumentError, _extract_docx, _extract_pdf, extract_text

log = logging.getLogger("api")

DATA_DIR = Path(os.environ.get("APP_DATA_DIR", "./data"))
SESSIONS_DIR = Path(os.environ.get("SESSIONS_DIR", str(DATA_DIR / "sessions")))
API_PUBLIC_URL = os.environ.get("API_PUBLIC_URL", "http://127.0.0.1:8000").rstrip("/")  # OAuth redirects
PREFS_FILE = DATA_DIR / "preferences.json"
LABELS_DIR = DATA_DIR / "speaker_labels"
SUMMARIES_DIR = DATA_DIR / "summaries"
PRODUCT_NAME = "Quorum"
DEFAULT_SEARCH = {"web_enabled": False, "provider": web_providers.DEFAULT_PROVIDER, "max_results": 5,
                  "sources": {"chats": True, "transcripts": True, "memory": True, "connectors": False},
                  "embed_model": long_context.EMBED_MODEL}
DEFAULT_PREFS = {"selected_model": "", "active_project": projects.DEFAULT_PROJECT_ID,
                 "confirm_cloud_sensitive": True, "web_search": True, "search": DEFAULT_SEARCH,
                 "meeting_notes_skill": True, "imported_cloud_notice": False,
                 # cloud providers that already showed the one-time "memory may be sent" notice
                 "cloud_notices": [],
                 # speakers: voice fingerprints (biometric, on by default), cloud review of names,
                 # on-screen names from video and the local vision-model fallback
                 "voiceprints": True, "speaker_cloud_review": True, "screen_names": True,
                 "vision_fallback": True, "vision_cap": 12, "screen_debug": False,
                 "vision_model": "",    # video frames: "" = the recommended vision model (vision_default)
                 "speaker_model": "",   # reading the transcript (local): "" = models.json speaker_default
                 "summary_model": "",   # meeting notes: "" = ask which model each time (never the chat model)
                 "record_live": False,  # Record a call: transcribe during the meeting (a live draft)
                 # Meeting check (conflicts, things to raise, pending items, my actions): "" = the local default
                 # model; pop-up alerts (count only, never the content); automatic checks every N minutes during
                 # a call (0 = off); how far back and whether other projects' meetings count.
                 "check_model": "", "check_popups": True, "check_auto_minutes": 0, "check_days": 90,
                 "check_all_projects": False}
CONVERSATION_TYPES = {".txt", ".md", ".vtt", ".srt", ".docx", ".pdf"}
MEDIA_SUFFIXES = {".mp4", ".m4a", ".mov", ".mkv", ".webm", ".wav", ".mp3", ".aac", ".flac", ".ogg", ".m4v", ".qta"}

@contextlib.asynccontextmanager
async def lifespan(_app):
    """Runs when the API starts (and stops): resumes any 'identify and summarise after transcription' work that
    was interrupted by a restart.
    """
    FOLLOWUPS.resume()  # "identify and summarise when transcription finishes" survives a restart
    yield


app = FastAPI(title=f"{PRODUCT_NAME} API", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=[o.strip() for o in os.environ.get(
        "WEB_ORIGINS", "http://localhost:3000,http://127.0.0.1:3000").split(",") if o.strip()],
    allow_methods=["*"], allow_headers=["*"],
)
_CACHES: dict[str, dict] = {}  # long-context caches per session / transcript+model
SPEEDS_FILE = DATA_DIR / "model_speeds.json"


def load_speeds() -> None:
    """Measured local-model speeds survive restarts, so time estimates are right from the first turn."""
    for model, speeds in _read_json(SPEEDS_FILE, {}).items():
        llm_backends.MEASURED_SPEEDS.setdefault(model, {}).update(
            {k: float(v) for k, v in speeds.items() if k in ("prefill_tps", "gen_tps")})


def save_speeds() -> None:
    """Saves the measured local-model speeds (tokens per second) so time estimates stay accurate after a restart.
    """
    if llm_backends.MEASURED_SPEEDS:
        _write_json(SPEEDS_FILE, llm_backends.MEASURED_SPEEDS)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _read_json(path: Path, default):
    """Reads a JSON file, or returns `default` when it's missing or broken."""
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def _write_json(path: Path, data) -> None:
    """Writes a JSON file safely: to a temporary file first, then renamed into place, so a crash never leaves
    half a file.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(path)


def prefs() -> dict:
    """The user's preferences: the saved values on top of DEFAULT_PREFS (so new settings always have a value),
    with older installs' search settings carried over.
    """
    stored = _read_json(PREFS_FILE, {})
    merged = {**DEFAULT_PREFS, **stored}
    legacy = {}
    if "search" not in stored and "web_search" in stored:
        # Installs from before Settings → Search keep their web search, on the keyless provider.
        legacy = {"web_enabled": bool(stored["web_search"]), "provider": "duckduckgo"}
    search = {**DEFAULT_SEARCH, **legacy, **(stored.get("search") or {})}
    search["sources"] = {**DEFAULT_SEARCH["sources"], **(search.get("sources") or {})}
    merged["search"] = search
    return merged


def save_prefs(values: dict) -> dict:
    """Saves the given preference values (unknown keys are ignored) and returns all preferences."""
    current = prefs()
    for key, value in values.items():
        if key not in DEFAULT_PREFS:
            continue
        if key == "search" and isinstance(value, dict):
            search = {**current["search"], **{k: v for k, v in value.items() if k in DEFAULT_SEARCH}}
            search["sources"] = {**current["search"]["sources"], **(value.get("sources") or {})}
            current["search"] = search
        else:
            current[key] = value
    _write_json(PREFS_FILE, current)
    return current


def active_project() -> dict:
    """The project the user is working in (the default 'General' project if none)."""
    project = projects.load_project(prefs()["active_project"])
    return project or projects.get_or_create_default_project()


def sessions_dir(project: dict | None = None) -> Path:
    """The folder holding the chats of a project."""
    return projects.sessions_dir_for(project or active_project(), SESSIONS_DIR)


def session_home(sid: str) -> Path:
    """The folder a chat lives in: the active project's, or the imported claude.ai chats."""
    if sessions.load(sessions_dir(), sid) is None and sessions.load(imports.conversations_dir(), sid) is not None:
        return imports.conversations_dir()
    return sessions_dir()


def load_session(sid: str) -> dict:
    """One chat by id, from wherever it lives; 404 when it doesn't exist."""
    record = sessions.load(session_home(sid), sid)
    if record is None:
        raise HTTPException(404, "Chat not found.")
    return record


def sse(events: Callable[[Callable[[dict], None]], None]) -> StreamingResponse:
    """Run `events(emit)` in a thread and stream what it emits. Disconnect = cancel."""
    q: "queue.Queue[dict | None]" = queue.Queue()
    cancelled = threading.Event()

    class Cancelled(Exception):
        """Raised inside the worker when the browser disconnected, to stop the work early."""
        pass

    def emit(event: dict) -> None:
        """Hands one event to the browser (via the queue). Raises Cancelled once the browser has gone."""
        if cancelled.is_set():
            raise Cancelled()
        q.put(event)

    def worker() -> None:
        """Runs the work in a background thread and turns known failures into 'error' events with a `kind` the
        browser knows how to show.
        """
        try:
            events(emit)
        except Cancelled:
            pass
        except llm_backends.LocalModelError as exc:
            q.put({"type": "error", "kind": "local_model", "message": str(exc)})
        except cloud_providers.CloudProviderError as exc:
            q.put({"type": "error", "kind": "cloud", "error_kind": exc.kind, "provider": exc.provider,
                   "message": str(exc)})
        except meetings.TranscriberUnavailable as exc:
            q.put({"type": "error", "kind": "transcriber", "message": str(exc)})
        except requests.RequestException as exc:
            q.put({"type": "error", "kind": "network", "message": f"Request failed: {exc}"})
        except Exception as exc:  # report everything; never leave the client hanging
            log.exception("stream failed")
            q.put({"type": "error", "kind": "server", "message": f"{type(exc).__name__}: {exc}"})
        finally:
            q.put(None)

    threading.Thread(target=worker, daemon=True).start()

    def stream():
        """The response body: yields each event as 'data: {json}' lines, sends a keep-alive comment every 15 s of
        silence, and marks the work cancelled when the browser disconnects.
        """
        try:
            while True:
                try:
                    event = q.get(timeout=15)
                except queue.Empty:
                    yield ": keep-alive\n\n"
                    continue
                if event is None:
                    break
                yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
        finally:
            cancelled.set()

    return StreamingResponse(stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


def provider_for(model_id: str | None):
    """The model provider for a model id (the selected model when none is given). 400 for an unknown id."""
    model_id = model_id or prefs()["selected_model"] or model_registry.load_registry()["default"]
    try:
        return model_registry.get_provider(model_id)
    except KeyError as exc:
        raise HTTPException(400, str(exc)) from exc


def model_label(provider) -> str:
    """A label such as 'Gemma 4 E4B · local' for messages and saved notes."""
    entry = model_registry.find(provider.model_id) or {}
    where = "local" if provider.is_local else "cloud"
    return f"{entry.get('label') or provider.backend.model} · {where}"


def model_name(provider) -> str:
    """"Claude Opus 5.5" / "GLM-5.3 Flash": what confirm prompts and notices call the model."""
    entry = model_registry.find(provider.model_id) or {}
    return entry.get("label") or provider.backend.model


def provider_key(provider) -> str:
    """Which provider's approvals apply: 'local', 'gaia' or a cloud provider's id."""
    return "local" if provider.is_local else getattr(provider, "provider_key", "gaia")


def provider_label(provider) -> str:
    """Where the model runs, in words: 'this Mac' or the provider's name."""
    return "this Mac" if provider.is_local else getattr(provider, "provider_label", "the cloud")


def cloud_notice(provider, what: str) -> dict | None:
    """The one-time notice per cloud provider that memory (and imported chats) may be sent to it."""
    key = provider_key(provider)
    current = prefs()
    shown = set(current.get("cloud_notices") or [])
    if key == "gaia" and current.get("imported_cloud_notice"):
        shown.add("gaia")
    if key in shown:
        return None
    save_prefs({"cloud_notices": sorted(shown | {key}), **({"imported_cloud_notice": True} if key == "gaia" else {})})
    return {"type": "notice", "kind": "privacy",
            "text": f"ℹ️ You chose {model_name(provider)} ({provider_label(provider)}), so {what} can be sent to it. "
                    "With a local model it stays on this Mac. This notice is shown once for this provider."}


# ---------------------------------------------------------------------------
# Health, preferences, models
# ---------------------------------------------------------------------------

@app.get("/api/health")
def health() -> dict:
    """GET /api/health: the status of Ollama, the cloud model, the transcriber and the search index, shown in the
    app's status indicators.
    """
    state = model_registry.ollama_state()
    try:
        transcriber = meetings.health()
    except (meetings.TranscriberUnavailable, RuntimeError) as exc:
        transcriber = {"ok": False, "error": str(exc)}
    return {"product": PRODUCT_NAME, "ollama": {"running": state["running"], "version": state["version"],
                                                "url": llm_backends.LOCAL_LLM_BASE_URL},
            "cloud": {"configured": bool(llm_backends.GAIA_API_KEY), "model": llm_backends.GAIA_MODEL,
                      "providers": len(cloud_providers.load()["providers"])},
            "transcriber": transcriber}


@app.get("/api/preferences")
def get_prefs() -> dict:
    """GET /api/preferences: all preferences."""
    return prefs()


@app.put("/api/preferences")
def put_prefs(body: dict) -> dict:
    """PUT /api/preferences: saves preferences after checking the values (known project and models; speaker and
    video models must be local).
    """
    if "active_project" in body and projects.load_project(str(body["active_project"])) is None:
        raise HTTPException(404, "Project not found.")
    if "selected_model" in body and body["selected_model"] and model_registry.find(body["selected_model"]) is None:
        raise HTTPException(400, "Unknown model.")
    for key in ("summary_model", "check_model"):
        if body.get(key) and model_registry.find(str(body[key])) is None:
            raise HTTPException(400, "Unknown model.")
    if "check_auto_minutes" in body and body["check_auto_minutes"] not in (0, 5, 10, 15, 30):
        raise HTTPException(400, "Automatic checks run every 5, 10, 15 or 30 minutes, or never.")
    if "check_days" in body and body["check_days"] not in (30, 90, 180, 365):
        raise HTTPException(400, "Look back 30, 90, 180 or 365 days.")
    for key in ("speaker_model", "vision_model"):
        if str(body.get(key) or "").startswith("cloud:"):
            raise HTTPException(400, "Speaker identification and video frames always use a local model.")
    return save_prefs(body)


@app.get("/api/models")
def list_models() -> dict:
    """GET /api/models: chat models with their status (ready, not downloaded…), downloads in progress, and which
    one is selected.
    """
    data = model_registry.list_models(pulls=model_registry.PULLS.snapshot())
    data["selected"] = prefs()["selected_model"] or data["default"]
    return data


@app.get("/api/models/vision")
def list_vision_models() -> dict:
    """Models for the on-screen names fallback (Settings → Meetings)."""
    data = model_registry.vision_models()
    data["pulls"] = model_registry.PULLS.snapshot()
    data["selected"] = prefs()["vision_model"]
    data["effective"] = model_registry.pick_vision_model(data["selected"]) or ""
    return data


class PullBody(BaseModel):
    """Body naming the Ollama model to download."""
    model: str


@app.post("/api/local-models/pull")
def start_pull(body: PullBody) -> dict:
    """POST /api/local-models/pull: starts downloading a model."""
    return model_registry.PULLS.start(body.model)


@app.get("/api/local-models/pulls")
def list_pulls() -> dict:
    """GET /api/local-models/pulls: progress of all downloads."""
    return model_registry.PULLS.snapshot()


@app.delete("/api/local-models/pull/{model:path}")
def cancel_pull(model: str) -> dict:
    """DELETE /api/local-models/pull/{model}: cancels a download."""
    result = model_registry.PULLS.cancel(model)
    if result is None:
        raise HTTPException(404, "No download for that model.")
    return result


# ---------------------------------------------------------------------------
# Cloud providers (Settings → Models): Anthropic, OpenAI, OpenAI-compatible endpoints
# ---------------------------------------------------------------------------

@app.get("/api/providers")
def list_providers() -> dict:
    """GET /api/providers: added cloud providers, the kinds that can be added, and GAIA's status."""
    return {"providers": cloud_providers.list_providers(),
            "kinds": [{"kind": k, "label": v["label"], "base_url": v["base_url"]} for k, v in cloud_providers.KINDS.items()],
            "gaia": {"label": "Globant GAIA", "model": llm_backends.GAIA_MODEL, "configured": bool(llm_backends.GAIA_API_KEY)}}


class ProviderBody(BaseModel):
    """POST /api/providers body: provider kind, label, URL, auth header style and API key."""
    kind: str
    label: str = ""
    base_url: str = ""
    auth_header: str = "bearer"
    key: str = ""


@app.post("/api/providers")
def add_provider(body: ProviderBody) -> dict:
    """POST /api/providers: adds a cloud provider (400 with the reason when the input is invalid)."""
    try:
        return cloud_providers.add_provider(body.kind, body.label, body.base_url, body.auth_header, body.key)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@app.patch("/api/providers/{pid}")
def update_provider(pid: str, body: dict) -> dict:
    """PATCH /api/providers/{id}: changes a provider (models switched on, key, URL…)."""
    result = cloud_providers.update_provider(pid, body)
    if result is None:
        raise HTTPException(404, "Provider not found.")
    return result


@app.delete("/api/providers/{pid}")
def remove_provider(pid: str) -> dict:
    """DELETE /api/providers/{id}: removes a provider and its key; if one of its models was selected, the
    selection goes back to the default.
    """
    if not cloud_providers.remove_provider(pid):
        raise HTTPException(404, "Provider not found.")
    if cloud_providers.parse_model_id(prefs()["selected_model"]) and \
            cloud_providers.parse_model_id(prefs()["selected_model"])[0] == pid:
        save_prefs({"selected_model": ""})
    return {"ok": True}


@app.post("/api/providers/{pid}/test")
def test_provider(pid: str) -> dict:
    """POST /api/providers/{id}/test: checks the key and lists the provider's models."""
    try:
        return cloud_providers.test_provider(pid)
    except KeyError as exc:
        raise HTTPException(404, "Provider not found.") from exc


@app.delete("/api/local-models/{model:path}")
def delete_model(model: str) -> dict:
    """DELETE /api/local-models/{model}: deletes a downloaded model from Ollama."""
    try:
        model_registry.delete_local_model(model)
    except requests.ConnectionError as exc:
        raise HTTPException(503, "Ollama is not running.") from exc
    except RuntimeError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"ok": True}


# ---------------------------------------------------------------------------
# Projects (incl. memory and skills), connectors
# ---------------------------------------------------------------------------

def _project_out(project: dict) -> dict:
    """A project for the browser: document names and sizes (not their text), skills, and whether it's the active
    project.
    """
    out = dict(project)
    out["documents"] = [{"name": d["name"], "kind": d.get("kind", "document"), "chars": len(d.get("text", ""))}
                        for d in project.get("documents", [])]
    out["skills"] = tools.available_skills(project)
    out["active"] = project["id"] == prefs()["active_project"]
    out["memory"] = memory_store.project_facts(project)
    return out


@app.get("/api/projects")
def list_projects() -> list[dict]:
    """GET /api/projects: all projects, marking the active one."""
    projects.get_or_create_default_project()
    active = prefs()["active_project"]
    return [dict(p, active=p["id"] == active) for p in projects.list_projects()]


class ProjectCreate(BaseModel):
    """POST /api/projects body: a name and optional instructions."""
    name: str
    instructions: str = ""


@app.post("/api/projects")
def create_project(body: ProjectCreate) -> dict:
    """POST /api/projects: creates a project."""
    if not body.name.strip():
        raise HTTPException(400, "Give the project a name.")
    return _project_out(projects.create_project(body.name.strip(), body.instructions))


def _project(pid: str) -> dict:
    """One project by id, or a 404 error."""
    project = projects.load_project(pid)
    if project is None:
        raise HTTPException(404, "Project not found.")
    return project


@app.get("/api/projects/{pid}")
def get_project(pid: str) -> dict:
    """GET /api/projects/{id}: one project."""
    return _project_out(_project(pid))


EDITABLE = {"name", "instructions", "memory", "skills", "tools", "confidential_terms",
            "default_local_model", "default_model_mode"}


@app.patch("/api/projects/{pid}")
def update_project(pid: str, body: dict) -> dict:
    """PATCH /api/projects/{id}: changes editable fields (name, instructions, tools, skills, confidential
    terms…); the project's memory is kept in the memory store.
    """
    project = _project(pid)
    for key, value in body.items():
        if key not in EDITABLE:
            continue
        if key in ("memory", "confidential_terms", "tools"):
            value = [str(v).strip() for v in value if str(v).strip()]
        if key == "memory":  # project memory lives in the memory store
            memory_store.set_project_facts(project, value)
            continue
        if key == "skills":
            value = [{"name": str(s.get("name", "")).strip(), "description": str(s.get("description", "")).strip(),
                      "content": str(s.get("content", "")).strip()} for s in value
                     if isinstance(s, dict) and str(s.get("name", "")).strip()]
        if key == "name" and not str(value).strip():
            raise HTTPException(400, "The name can't be empty.")
        project[key] = value
    projects.save_project(project)
    return _project_out(projects.load_project(pid))


@app.delete("/api/projects/{pid}")
def delete_project(pid: str) -> dict:
    """DELETE /api/projects/{id}: deletes a project (not the default one)."""
    if pid == projects.DEFAULT_PROJECT_ID:
        raise HTTPException(400, "The default project can't be deleted.")
    projects.delete_project(pid)
    if prefs()["active_project"] == pid:
        save_prefs({"active_project": projects.DEFAULT_PROJECT_ID})
    return {"ok": True}


@app.post("/api/projects/{pid}/documents")
async def add_document(pid: str, file: UploadFile = File(...)) -> dict:
    """POST /api/projects/{id}/documents: uploads a document; its text is extracted and given to every chat of
    the project.
    """
    project = _project(pid)
    data = await file.read()
    try:
        text = extract_text(file.filename, data)
    except UnsupportedDocumentError as exc:
        raise HTTPException(415, str(exc)) from exc
    project["documents"] = [d for d in project["documents"] if d["name"] != file.filename]
    project["documents"].append({"name": file.filename, "text": text})
    projects.save_project(project)
    return _project_out(project)


@app.delete("/api/projects/{pid}/documents/{name}")
def remove_document(pid: str, name: str) -> dict:
    """DELETE /api/projects/{id}/documents/{name}: removes a document from the project."""
    project = _project(pid)
    project["documents"] = [d for d in project["documents"] if d["name"] != name]
    projects.save_project(project)
    return _project_out(project)


@app.get("/api/tools")
def list_tools() -> list[dict]:
    """GET /api/tools: the tools a project can switch on."""
    return [{"name": name, "description": entry["description"]} for name, entry in tools.TOOL_REGISTRY.items()]


@app.get("/api/connectors")
def list_connectors() -> list[dict]:
    """GET /api/connectors: all connectors (never their secrets)."""
    return [connectors.public(c) for c in connectors.load_connectors()]


class ConnectorBody(BaseModel):
    """POST /api/connectors body: a remote MCP server (URL, transport, authentication)."""
    kind: str = "mcp"
    name: str
    description: str = ""
    url: str = ""
    transport: str = ""             # "" = the original simple connector; "http" | "sse" = full MCP client
    auth_type: str = "none"         # none | header | oauth
    header: str = "Authorization"
    header_value: str = ""          # secret: stored in the Keychain
    extra_headers: dict[str, str] = {}
    source: dict | None = None      # {"type": "directory", "directory", "server", "version", "repository", "publisher"}


@app.post("/api/connectors")
def add_connector(body: ConnectorBody) -> dict:
    """POST /api/connectors: adds a remote connector and, unless it needs OAuth sign-in, reads its tools right
    away.
    """
    try:
        if body.kind == "mcp" and body.transport:
            connector = connectors.add_remote(body.name, body.url, body.transport, body.description, body.auth_type,
                                              body.header, body.header_value, body.extra_headers, body.source)
            if body.auth_type != "oauth":
                connectors.test_connector(connector["id"])
            return connectors.public(connectors.get_connector(connector["id"]))
        return connectors.add_connector(body.kind, body.name, body.description, body.url)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


class StdioConnectorBody(BaseModel):
    """POST /api/connectors/stdio body: a local package from the directory (exact version, environment, keys) and
    the user's confirmation to run it.
    """
    name: str
    description: str = ""
    registry_type: str
    identifier: str
    version: str
    args: list[str] = []
    env: dict[str, str] = {}
    secret_env: dict[str, str] = {}
    source: dict | None = None
    confirm: bool = False           # the user saw the command, version and publisher and agreed


@app.post("/api/connectors/stdio")
def add_stdio_connector(body: StdioConnectorBody) -> dict:
    """A local package (npm/PyPI, run over stdio). Installed on first start, only after confirm."""
    try:
        connector = connectors.add_stdio(body.name, body.registry_type, body.identifier, body.version, body.description,
                                         body.env, body.secret_env, body.args, body.source, confirmed=body.confirm)
    except PermissionError as exc:
        raise HTTPException(428, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    result = connectors.test_connector(connector["id"])
    return dict(connectors.public(connectors.get_connector(connector["id"])), test=result)


@app.post("/api/connectors/preview-command")
def preview_command(body: StdioConnectorBody) -> dict:
    """The exact command a local package would run (shown before the user confirms)."""
    try:
        command = connectors.stdio_command(body.registry_type, body.identifier, body.version, body.args)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"command": command, "available": bool(__import__("shutil").which(command[0]))}


@app.patch("/api/connectors/{cid}")
def edit_connector(cid: str, body: dict) -> dict:
    """PATCH /api/connectors/{id}: switch on/off, description or key."""
    fields = {k: v for k, v in body.items() if k in ("enabled", "description", "header_value")}
    connector = connectors.update_connector(cid, fields)
    if connector is None:
        raise HTTPException(404, "Connector not found.")
    return connectors.public(connector)


@app.post("/api/connectors/{cid}/test")
def test_connector(cid: str) -> dict:
    """POST /api/connectors/{id}/test: connects, refreshes the tool list and reports the result."""
    try:
        result = connectors.test_connector(cid)
    except KeyError as exc:
        raise HTTPException(404, "Connector not found.") from exc
    return dict(result, connector=connectors.public(connectors.get_connector(cid)))


@app.get("/api/connectors/{cid}/updates")
def connector_updates(cid: str) -> dict:
    """GET /api/connectors/{id}/updates: whether the directory has a newer version of a package connector."""
    connector = connectors.get_connector(cid)
    if connector is None:
        raise HTTPException(404, "Connector not found.")
    source = connector.get("source") or {}
    if source.get("type") != "directory":
        return {"available": False, "current": "", "latest": ""}
    try:
        latest = connector_directory.directory(source.get("directory", "mcp-registry")).latest_version(source["server"])
    except connector_directory.DirectoryError as exc:
        raise HTTPException(502, str(exc)) from exc
    version = (latest or {}).get("version", "")
    return {"available": bool(version and connector_directory.version_newer(version, source.get("version", ""))),
            "current": source.get("version", ""), "latest": version}


class VersionBody(BaseModel):
    """Body for updating a package connector: the new version and the user's confirmation."""
    version: str
    confirm: bool = False


@app.post("/api/connectors/{cid}/update")
def update_connector_version(cid: str, body: VersionBody) -> dict:
    """POST /api/connectors/{id}/update: switches a package connector to another version (428 until the user has
    confirmed).
    """
    try:
        connectors.update_version(cid, body.version, confirmed=body.confirm)
    except PermissionError as exc:
        raise HTTPException(428, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return dict(connectors.public(connectors.get_connector(cid)), test=connectors.test_connector(cid))


@app.post("/api/connectors/{cid}/oauth/start")
def connector_sign_in(cid: str) -> dict:
    """POST /api/connectors/{id}/oauth/start: the sign-in page URL for an OAuth connector."""
    try:
        return {"auth_url": connectors.start_oauth(cid, f"{API_PUBLIC_URL}/api/connectors/oauth/callback")}
    except KeyError as exc:
        raise HTTPException(404, "Connector not found.") from exc
    except (RuntimeError, requests.RequestException) as exc:
        raise HTTPException(502, str(exc)) from exc


@app.get("/api/connectors/oauth/callback")
def connector_oauth_callback(state: str = "", code: str = "", error: str = "") -> Response:
    """GET /api/connectors/oauth/callback: where the sign-in page sends the browser back; stores the tokens and
    shows a small page that closes itself.
    """
    if error or not code:
        message, ok = f"Sign-in was cancelled ({error or 'no code'}).", False
    else:
        try:
            connector = connectors.finish_oauth(state, code)
            connectors.test_connector(connector["id"])
            message, ok = f"Signed in to {connector['name']}. You can close this tab and go back to Quorum.", True
        except (RuntimeError, requests.RequestException) as exc:
            message, ok = str(exc), False
    html = (f"<!doctype html><meta charset=utf-8><title>Quorum sign-in</title><body style='font-family:system-ui;"
            f"padding:40px'><h2>{'✓ ' if ok else ''}{'Signed in' if ok else 'Sign-in failed'}</h2>"
            f"<p>{__import__('html').escape(message)}</p></body>")
    return Response(html, media_type="text/html", status_code=200 if ok else 400)


@app.post("/api/connectors/{cid}/sign-out")
def connector_sign_out(cid: str) -> dict:
    """POST /api/connectors/{id}/sign-out: forgets the connector's sign-in."""
    connectors.sign_out(cid)
    return {"ok": True}


@app.delete("/api/connectors/{cid}")
def remove_connector(cid: str) -> dict:
    """DELETE /api/connectors/{id}: removes a connector."""
    connectors.remove_connector(cid)  # stops a running package and deletes its stored secrets
    return {"ok": True}


@app.get("/api/connector-directory/search")
def search_connector_directory(q: str = "", cursor: str = "", directory: str = "mcp-registry") -> dict:
    """Only the words typed (plus paging) go to the directory."""
    if not q.strip():
        return {"results": [], "next_cursor": "", "directory": ""}
    try:
        found = connector_directory.directory(directory).search(q, cursor)
    except KeyError as exc:
        raise HTTPException(404, "Unknown directory.") from exc
    except connector_directory.DirectoryError as exc:
        raise HTTPException(503 if exc.kind == "offline" else 429 if exc.kind == "rate_limited" else 502,
                            detail={"kind": exc.kind, "message": str(exc)}) from exc
    results = connector_directory.mark_installed([dict(r) for r in found["results"]], connectors.load_connectors())
    return dict(found, results=results)


class ProbeBody(BaseModel):
    """Body for checking a remote MCP server before adding it: its URL and transport."""
    url: str
    transport: str = "http"


@app.post("/api/connector-directory/probe")
def probe_connector(body: ProbeBody) -> dict:
    """The tools of a remote server before adding it (when it works without signing in)."""
    if not re.match(r"^https://", body.url) and not connectors.is_local_url(body.url):
        raise HTTPException(400, "Only https:// servers can be checked.")
    return connectors.probe_tools(body.url, body.transport)


# ---------------------------------------------------------------------------
# People memory
# ---------------------------------------------------------------------------

@app.get("/api/people")
def list_people() -> list[dict]:
    """GET /api/people: the people in memory (without voice fingerprints)."""
    return [people_store.public(p) for p in people_store.load()]


class PersonBody(BaseModel):
    """POST /api/people body: a person's name, role, notes and aliases."""
    name: str
    role: str = ""
    notes: str = ""
    aliases: list[str] = []


@app.post("/api/people")
def add_person(body: PersonBody) -> dict:
    """POST /api/people: adds a person (or updates the one with that name)."""
    if not body.name.strip():
        raise HTTPException(400, "Give the person a name.")
    return people_store.public(people_store.upsert(body.name, body.role, body.notes, body.aliases))


@app.patch("/api/people/{person_id}")
def edit_person(person_id: str, body: dict) -> dict:
    """PATCH /api/people/{id}: changes a person's details."""
    person = people_store.update(person_id, body)
    if person is None:
        raise HTTPException(404, "Person not found.")
    return people_store.public(person)


@app.delete("/api/people/{person_id}")
def remove_person(person_id: str) -> dict:
    """DELETE /api/people/{id}: removes a person and their voice fingerprints."""
    people_store.delete(person_id)
    return {"ok": True}


@app.delete("/api/people/{person_id}/voice")
def forget_voice(person_id: str) -> dict:
    """Delete this person's voice fingerprints (biometric data)."""
    person = people_store.forget_voice(person_id)
    if person is None:
        raise HTTPException(404, "Person not found.")
    return people_store.public(person)


@app.delete("/api/voiceprints")
def forget_all_voices() -> dict:
    """DELETE /api/voiceprints: deletes every stored voice fingerprint."""
    return {"deleted": people_store.forget_all_voices()}


# ---------------------------------------------------------------------------
# Memory (one store in the claude.ai memory format: profile, people, projects, areas)
# ---------------------------------------------------------------------------

@app.get("/api/memory")
def get_memory() -> dict:
    """GET /api/memory: all memory entries (no voice vectors), counts, likely duplicates, source labels and the
    migration backup's location.
    """
    store = memory_store.load_store()
    return {"entries": [memory_store.public(e) for e in store["entries"]], "counts": memory_store.counts(store),
            "duplicates": memory_store.duplicates(store), "sources": memory_store.SOURCE_LABELS,
            "migrated_at": store.get("migrated_at", ""), "backup": store.get("backup", "")}


@app.get("/api/memory/search")
def search_memory(q: str = "", kind: str | None = None) -> list[dict]:
    """GET /api/memory/search: entries matching the words in `q` (optionally one kind)."""
    return [memory_store.public(e) for e in memory_store.search(q, kind)]


class MemoryEntryBody(BaseModel):
    """POST /api/memory/entries body: kind, name, description and parent project."""
    kind: str
    name: str
    description: str = ""
    project: str = ""


@app.post("/api/memory/entries")
def create_memory_entry(body: MemoryEntryBody) -> dict:
    """POST /api/memory/entries: creates an entry (400 if the name is taken)."""
    try:
        return memory_store.public(memory_store.create(body.kind, body.name, body.description, body.project))
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@app.patch("/api/memory/entries/{entry_id}")
def edit_memory_entry(entry_id: str, body: dict) -> dict:
    """PATCH /api/memory/entries/{id}: changes an entry's name, description, aliases…"""
    try:
        entry = memory_store.update(entry_id, body)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    if entry is None:
        raise HTTPException(404, "Memory entry not found.")
    return memory_store.public(entry)


@app.delete("/api/memory/entries/{entry_id}")
def delete_memory_entry(entry_id: str) -> dict:
    """DELETE /api/memory/entries/{id}: deletes an entry and its facts."""
    if not memory_store.delete(entry_id):
        raise HTTPException(404, "Memory entry not found.")
    return {"ok": True}


class FactBody(BaseModel):
    """Body for a fact: its text and optional heading."""
    text: str = ""
    heading: str | None = None


@app.post("/api/memory/entries/{entry_id}/facts")
def add_memory_fact(entry_id: str, body: FactBody) -> dict:
    """POST /api/memory/entries/{id}/facts: adds a fact the user typed."""
    if not body.text.strip():
        raise HTTPException(400, "The fact is empty.")
    fact = memory_store.add_user_fact(entry_id, body.text, body.heading or "")
    if fact is None:
        raise HTTPException(404, "Memory entry not found.")
    return fact


@app.patch("/api/memory/entries/{entry_id}/facts/{fact_id}")
def edit_memory_fact(entry_id: str, fact_id: str, body: FactBody) -> dict:
    """PATCH …/facts/{fact_id}: changes a fact's text or heading."""
    fact = memory_store.update_fact(entry_id, fact_id, body.text if body.text.strip() else None, body.heading)
    if fact is None:
        raise HTTPException(404, "Fact not found.")
    return fact


@app.delete("/api/memory/entries/{entry_id}/facts/{fact_id}")
def delete_memory_fact(entry_id: str, fact_id: str) -> dict:
    """DELETE …/facts/{fact_id}: deletes a fact."""
    if not memory_store.delete_facts(entry_id, [fact_id]):
        raise HTTPException(404, "Fact not found.")
    return {"ok": True}


class MergeFactsBody(BaseModel):
    """Body for merging facts: their ids and, optionally, the merged wording."""
    ids: list[str]
    text: str = ""


@app.post("/api/memory/entries/{entry_id}/facts/merge")
def merge_memory_facts(entry_id: str, body: MergeFactsBody) -> dict:
    """POST …/facts/merge: merges two or more facts of an entry into one."""
    fact = memory_store.merge_facts(entry_id, body.ids, body.text)
    if fact is None:
        raise HTTPException(400, "Pick at least two facts of this entry to merge.")
    return fact


class MergeEntriesBody(BaseModel):
    """Body for merging entries: the one to keep and the ones merged into it."""
    keep: str
    ids: list[str]


@app.post("/api/memory/merge")
def merge_memory_entries(body: MergeEntriesBody) -> dict:
    """POST /api/memory/merge: merges duplicate entries (their facts and aliases move to the kept one)."""
    entry = memory_store.merge(body.keep, body.ids)
    if entry is None:
        raise HTTPException(404, "Memory entry not found.")
    return memory_store.public(entry)


@app.get("/api/memory/export")
def export_memory() -> Response:
    """The whole memory as a claude.ai-format ZIP (voice fingerprints are never exported)."""
    return Response(memory_store.export_zip(), media_type="application/zip",
                    headers={"Content-Disposition": f'attachment; filename="quorum-memory-{datetime.now():%Y-%m-%d}.zip"'})


# ---------------------------------------------------------------------------
# Chat sessions
# ---------------------------------------------------------------------------

def session_summary(record: dict) -> dict:
    """A chat for the sidebar list, including whether it was imported from claude.ai."""
    out = sessions.summary(record)
    out["imported"] = bool(record.get("imported"))
    out["read_only"] = bool(record.get("read_only"))
    if record.get("imported"):
        out["imported_at"] = record["imported"].get("imported_at", "")
        out["imported_source"] = record["imported"].get("source", "")
    if record.get("continued_from"):
        out["continued_from"] = record["continued_from"]
    return out


@app.get("/api/sessions")
def list_sessions(q: str = "", imported: str = "show") -> list[dict]:
    """Chats, newest first. imported = show | hide | only (claude.ai conversations)."""
    out = []
    if imported != "only":
        out += [dict(x, imported=False, read_only=False) for x in sessions.list_all(sessions_dir(), q)]
    if imported != "hide":
        for x in sessions.list_all(imports.conversations_dir(), q):
            record = sessions.load(imports.conversations_dir(), x["id"]) or {}
            out.append(session_summary(record) if record else x)
    out.sort(key=lambda x: x["updated_at"], reverse=True)
    return out


class SessionCreate(BaseModel):
    """POST /api/sessions body: the model the new chat starts with."""
    model_id: str = ""


@app.post("/api/sessions")
def create_session(body: SessionCreate) -> dict:
    """POST /api/sessions: creates an empty chat in the active project."""
    return sessions.create(sessions_dir(), body.model_id)


def _session_out(record: dict) -> dict:
    """A chat for the browser: messages, and attachment names and sizes (not their text)."""
    out = dict(record)
    out["imported"] = record.get("imported") or None
    out["read_only"] = bool(record.get("read_only"))
    out["attachments"] = [{"name": a["name"], "kind": a.get("kind", "document"), "chars": len(a.get("text", "")),
                           "transcript_id": a.get("transcript_id"), "live": a.get("live")}
                          for a in record.get("attachments", [])]
    return out


@app.get("/api/sessions/{sid}")
def get_session(sid: str) -> dict:
    """GET /api/sessions/{id}: one chat."""
    return _session_out(load_session(sid))


@app.patch("/api/sessions/{sid}")
def rename_session(sid: str, body: dict) -> dict:
    """PATCH /api/sessions/{id}: renames a chat (or changes its model)."""
    record = load_session(sid)
    if "title" in body:
        title = str(body["title"]).strip()
        if not title:
            raise HTTPException(400, "The title can't be empty.")
        record["title"], record["custom_title"] = title[:120], True
        if record.get("imported"):
            record["renamed"] = True
    if "model_id" in body:
        record["model_id"] = str(body["model_id"])
    sessions.save(session_home(sid), record, touch=False)
    return _session_out(record)


@app.delete("/api/sessions/{sid}")
def delete_session(sid: str) -> dict:
    """DELETE /api/sessions/{id}: deletes a chat and its cached long-context work."""
    load_session(sid)
    sessions.delete(session_home(sid), sid)
    _CACHES.pop(sid, None)
    return {"ok": True}


CONTINUE_CHARS = 60_000  # history carried into a continued chat (the newest messages)


@app.post("/api/sessions/{sid}/continue")
def continue_session(sid: str, body: SessionCreate) -> dict:
    """Continue a read-only (imported) chat in a new chat with any model: the history is copied."""
    source = load_session(sid)
    history, used = [], 0
    for message in reversed(source["messages"]):
        used += len(message["content"])
        if used > CONTINUE_CHARS and history:
            break
        history.append({"role": message["role"], "content": message["content"]})
    history.reverse()
    if history and history[0]["role"] == "assistant":
        history.insert(0, {"role": "user", "content": "(Earlier messages of this conversation are in the original chat.)"})
    record = sessions.create(sessions_dir(), body.model_id)
    record["messages"] = history
    record["title"], record["custom_title"] = f"{source.get('title') or 'Chat'} (continued)"[:120], True
    record["continued_from"] = {"id": sid, "title": source.get("title", ""),
                                "source": (source.get("imported") or {}).get("source", "")}
    sessions.save(sessions_dir(), record)
    return _session_out(record)


def writable_session(sid: str) -> dict:
    """A chat that may be changed; imported claude.ai chats are read-only (409)."""
    record = load_session(sid)
    if record.get("read_only"):
        raise HTTPException(409, "This chat was imported from claude.ai and is read-only. Continue it in a new chat.")
    return record


@app.post("/api/sessions/{sid}/attachments")
async def attach_file(sid: str, file: UploadFile = File(...)) -> dict:
    """POST /api/sessions/{id}/attachments: attaches a document to this chat (its text is extracted; 415 for
    unsupported files).
    """
    record = writable_session(sid)
    data = await file.read()
    try:
        text = extract_text(file.filename, data)
    except UnsupportedDocumentError as exc:
        raise HTTPException(415, str(exc)) from exc
    if not text.strip():
        raise HTTPException(422, f"{file.filename} contains no readable text.")
    record["attachments"] = [a for a in record["attachments"] if a["name"] != file.filename]
    record["attachments"].append({"name": file.filename, "text": text, "kind": "document"})
    sessions.save(sessions_dir(), record, touch=False)
    return _session_out(record)


class AttachTranscript(BaseModel):
    """Body for attaching a meeting transcript to a chat."""
    transcript_id: str


def transcript_attachment(tid: str) -> dict:
    """A meeting transcript as a chat attachment (its Markdown text, kind 'transcript')."""
    meta = meetings.transcript(tid)
    return {"name": f"Transcript: {meta['title']}", "text": meetings.markdown(tid), "kind": "transcript",
            "transcript_id": tid}


@app.post("/api/sessions/{sid}/transcripts")
def attach_transcript(sid: str, body: AttachTranscript) -> dict:
    """POST /api/sessions/{id}/transcripts: attaches a meeting to this chat (the only way a meeting reaches the
    model in a chat).
    """
    record = writable_session(sid)
    item = transcript_attachment(body.transcript_id)
    record["attachments"] = [a for a in record["attachments"] if a["name"] != item["name"]] + [item]
    sessions.save(sessions_dir(), record, touch=False)
    return _session_out(record)


class AttachLive(BaseModel):
    """Body for attaching the live draft of a call being recorded (None: the recording in progress)."""
    recording_id: str | None = None


def live_attachment(recording_id: str | None) -> dict:
    """The live draft as a chat attachment: kind 'transcript' (so the privacy check and the minutes skill treat it
    like a transcript), with `live` = the recording id so each turn refreshes it."""
    draft = meetings.record_live(recording_id)
    rec_id = draft["id"]
    if draft.get("transcript_id"):  # the meeting is over and transcribed: attach the final transcript
        return transcript_attachment(draft["transcript_id"])
    title = draft.get("title") or "Call"
    return {"name": f"Transcript: {title} (live)", "text": meetings.record_live_markdown(rec_id),
            "kind": "transcript", "live": rec_id}


@app.post("/api/sessions/{sid}/attachments/live")
def attach_live(sid: str, body: AttachLive | None = None) -> dict:
    """POST /api/sessions/{id}/attachments/live: attaches the meeting so far; it keeps itself up to date."""
    record = writable_session(sid)
    item = _transcriber(live_attachment, body.recording_id if body else None)
    record["attachments"] = [a for a in record["attachments"]
                             if a["name"] != item["name"] and a.get("live") != item.get("live", "-")] + [item]
    sessions.save(sessions_dir(), record, touch=False)
    return _session_out(record)


def refresh_live_attachments(record: dict) -> bool:
    """Before a chat turn: live drafts get the meeting up to now, and a draft whose call is transcribed becomes
    the final transcript. True when something changed. A transcriber that's down keeps the last text."""
    changed = False
    out = []
    for item in record.get("attachments", []):
        if item.get("live"):
            try:
                fresh = live_attachment(item["live"])
            except (meetings.TranscriberUnavailable, RuntimeError):
                fresh = item
            changed = changed or fresh != item
            item = fresh
        out.append(item)
    if changed:
        record["attachments"] = out
    return changed


@app.delete("/api/sessions/{sid}/attachments/{name}")
def detach(sid: str, name: str) -> dict:
    """DELETE /api/sessions/{id}/attachments/{name}: removes an attachment from the chat."""
    record = load_session(sid)
    record["attachments"] = [a for a in record["attachments"] if a["name"] != name]
    sessions.save(sessions_dir(), record, touch=False)
    return _session_out(record)


class MessageBody(BaseModel):
    """POST /api/sessions/{id}/messages body: the message, the selected model, the answer to a confirm question,
    a picked skill and the files attached with this message.
    """
    content: str
    model_id: str = ""
    confirm: str | None = None        # "local" | "cloud" | "run" (answers to a confirm event)
    skill: str | None = None          # skill name picked in the composer
    attached_now: list[str] = []      # attachment names added with this message


@app.post("/api/sessions/{sid}/messages")
def send_message(sid: str, body: MessageBody) -> StreamingResponse:
    """POST /api/sessions/{id}/messages: one chat turn, streamed to the browser (SSE).

    The steps are numbered in the comments below: slash commands, choosing the model, the privacy check,
    building the turn, long transcripts, running the agent, saving, and learning new memory facts.
    """
    record = writable_session(sid)
    project = active_project()
    directory = sessions_dir(project)
    preferences = prefs()

    def run(emit: Callable[[dict], None]) -> None:
        """The work of one chat turn, run in a background thread by sse()."""
        skills = tools.available_skills(project)
        text, skill = body.content, None
        if body.skill:
            skill = next((s for s in skills if s["name"] == body.skill), None)
        names = [s["name"] for s in skills]
        # 1. Slash commands: '/' alone shows the skills menu; '/2 question' or '/table question' runs a skill.
        slash = tools.parse_slash_command(text, names)
        if slash["is_slash"] and slash["show_menu"] and not skill:
            menu = tools.format_skill_menu(names)
            record["messages"] += [{"role": "user", "content": text}, {"role": "assistant", "content": menu}]
            sessions.save(directory, record)
            emit({"type": "done", "message": record["messages"][-1], "session": sessions.summary(record)})
            return
        if slash["skill"]:
            skill = next(s for s in skills if s["name"] == slash["skill"])
            text = slash["query"] or "Please proceed following the active skill."

        # 2. Which model answers: the one selected in the app (or the chat's own). If the user answered a
        # privacy question with 'use local model', switch to a local model and mark the chat confidential.
        provider = provider_for(body.model_id or record.get("model_id"))
        if body.confirm == "local":
            name = model_registry.pick_local_model(record.get("local_model") or project.get("default_local_model"))
            if not name:
                emit({"type": "error", "kind": "local_model",
                      "message": "No local model is installed. Download one in Settings → Local models."})
                return
            provider = model_registry.OllamaProvider(name)
            record["confidential"] = True
        if refresh_live_attachments(record):  # a call's live draft: the meeting up to now
            sessions.save(sessions_dir(), record, touch=False)
        attachments = record.get("attachments", [])
        key = provider_key(provider)
        # Approvals are per provider: approving GAIA doesn't approve Claude or OpenAI.
        approvals = (record.get("cloud_approved_reasons", []) if key == "gaia"
                     else (record.get("cloud_approved") or {}).get(key, []))
        remote = [c["name"] for c in connectors.active_connectors() if connectors.is_remote(c)]
        # 3. Privacy check for cloud models: if the message or this chat's attachments look confidential (and
        # weren't approved before for this provider), stop and ask the user. The browser sends the message again
        # with confirm='local' or 'cloud'.
        if not provider.is_local and body.confirm != "cloud":
            reasons = []
            if preferences["confirm_cloud_sensitive"]:
                reasons = agent.sensitive_reasons(text, project, attachments, approvals)
            if record.get("confidential") and "confidential chat" not in approvals:
                reasons = ["this chat was moved to a local model as confidential"] + reasons
            if reasons:
                emit({"type": "confirm", "kind": "sensitive", "reasons": reasons,
                      "local_model": model_registry.pick_local_model(record.get("local_model")),
                      "model": model_name(provider), "provider": provider_label(provider),
                      "connectors": [] if record.get("confidential") else remote})
                return
        # The user said 'send to the cloud anyway': remember these reasons for this provider so the same kind of
        # content doesn't ask again in this chat.
        if body.confirm == "cloud":
            reasons = agent.sensitive_reasons(text, project, attachments)
            approved = set(approvals) | set(reasons)
            if record.get("confidential"):
                approved.add("confidential chat")
            if key == "gaia":
                record["cloud_approved_reasons"] = sorted(approved)
            else:
                record.setdefault("cloud_approved", {})[key] = sorted(approved)

        # 4. Build the turn: relevant memory facts, project facts, attachments, tools. Remote connectors are off
        # in a confidential chat.
        recalled = memory_store.relevant(text, project.get("name", ""))
        state = agent.TurnState(project=dict(project, memory=memory_store.project_facts(project)),
                                messages=list(record["messages"]), provider=provider,
                                attachments=attachments, web_search_enabled=preferences["search"]["web_enabled"],
                                caches=_CACHES.setdefault(sid, {}),
                                imported_facts=[m["text"] for m in recalled if m["kind"] != "project"
                                                or m["name"] != project.get("name")],
                                memory_label="Relevant facts from the user's memory (profile, people, projects, topics):",
                                remote_connectors=not record.get("confidential"),
                                extra_tools=["search"],
                                minutes_skill=notes_chat_skill() if preferences["meeting_notes_skill"] else None)
        # 5. Long transcripts on a local model: if reading them will take a while, ask first ('long' confirm).
        plan = agent.plan_long_context(state, text)
        if plan and plan["confirm"] and body.confirm != "run":
            emit({"type": "confirm", "kind": "long", "notice": plan["notice"]})
            return

        # 6. Files attached with THIS message are added to the question itself, so the model focuses on them.
        now = [a for a in attachments if a["name"] in body.attached_now]
        effective = text + "".join(
            f"\n\n--- Attached file (this message): {a['name']} ---\n{a['text']}\n--- end of {a['name']} ---\n"
            "Treat the attached files in THIS message as the primary source." for a in now)
        display = f"🧩 *{skill['name']}*\n\n{text}" if skill else text
        if now:
            display += "\n\n📎 *attached: " + ", ".join(a["name"] for a in now) + "*"
        user_message = {"role": "user", "content": display}
        record["messages"].append(user_message)
        state.messages = list(record["messages"])
        if provider.is_local:
            record["local_model"] = provider.backend.model
        emit({"type": "start", "model": model_label(provider), "model_id": provider.model_id,
              "user": user_message})
        # Tell the browser which memory facts were used (shown under the answer), and show the one-time notice
        # that memory may be sent to a cloud provider.
        used = [m["text"] for m in recalled][:12]
        if used:
            emit({"type": "memory_used", "facts": used})
        if not provider.is_local and (used or imports.status()["conversations"]):
            notice = cloud_notice(provider, "relevant facts from your memory (and imported chats the assistant searches)")
            if notice:
                emit(notice)
        try:
            if plan and plan["show_notice"]:
                emit({"type": "notice", "text": plan["notice"]})
            state.transcript_override = agent.prepare_long_context(state, effective, plan, emit) if plan else None
            # 7. Run the agent: it streams the answer (and any tool use) to the browser through `emit`.
            reply = agent.generate_reply(state, effective, skill, emit)
        except Exception:
            record["messages"].pop()  # let the user retry the same message
            sessions.save(directory, record)
            raise
        message = {"role": "assistant", "content": reply, "model": model_label(provider),
                   "model_id": provider.model_id}
        if used:
            message["memory_used"] = used
        record["messages"].append(message)
        record["model_id"] = body.model_id or record.get("model_id") or provider.model_id
        sessions.save(directory, record)
        # 8. Every 4 messages, ask the model for new long-term facts and add them to the project's memory.
        if len(record["messages"]) - record.get("memory_last_len", 0) >= 4:
            record["memory_last_len"] = len(record["messages"])
            state.messages = list(record["messages"])
            added = memory_store.learn_chat_facts(project, agent.extract_memories(state))
            if added:
                emit({"type": "memory", "added": added})
            sessions.save(directory, record, touch=False)
        save_speeds()
        emit({"type": "done", "message": message, "session": sessions.summary(record)})

    return sse(run)


# ---------------------------------------------------------------------------
# Meetings: recordings, uploads, jobs (proxied to the local transcriber)
# ---------------------------------------------------------------------------

def _transcriber(call: Callable, *args, **kwargs):
    """Calls the transcriber and turns its failures into HTTP errors for the browser (503 when it isn't running,
    400 with its message otherwise).
    """
    try:
        return call(*args, **kwargs)
    except meetings.TranscriberUnavailable as exc:
        raise HTTPException(503, str(exc)) from exc
    except meetings.RecordError as exc:  # the call recorder: keep `kind` (permissions, busy, disk…) for the page
        raise HTTPException(exc.status if exc.status in (400, 404, 409, 501, 503, 507) else 400,
                            {"kind": exc.kind, "message": str(exc)}) from exc
    except RuntimeError as exc:
        raise HTTPException(400, str(exc)) from exc


@app.get("/api/meetings/status")
def meetings_status() -> dict:
    """GET /api/meetings/status: the transcriber's health."""
    return _transcriber(meetings.health)


@app.get("/api/meetings/recordings")
def recordings() -> list[dict]:
    """GET /api/meetings/recordings: recordings in the watch and uploads folders."""
    return _transcriber(meetings.recordings)


class PathBody(BaseModel):
    """A request body with the path of a recording."""
    path: str


# ---------------------------------------------------------------------------
# Models per meeting: reading the transcript (local), video frames (local vision), summary
# ---------------------------------------------------------------------------

MODEL_KEYS = ("speaker_model", "frames_model", "summary_model")


def meeting_models_dir() -> Path:
    """Folder for per-meeting model choices and the after-transcription follow-ups."""
    return DATA_DIR / "meeting_models"


def meeting_models(tid: str, transcript: dict | None = None) -> dict:
    """The meeting's model choices: Settings, then what New meeting kept with the transcript, then
    a model picked later in the Speakers or Summary panel ("" there = back to Settings)."""
    if transcript is None:
        try:
            transcript = meetings.transcript(tid)
        except (meetings.TranscriberUnavailable, requests.RequestException, RuntimeError, KeyError):
            transcript = {}
    current = prefs()
    defaults = {"speaker_model": current["speaker_model"], "frames_model": current["vision_model"],
                "summary_model": current["summary_model"]}
    choice = dict(defaults)
    from_job = transcript.get("models") or {}
    changed = _read_json(meeting_models_dir() / f"{tid}.json", {})
    for key in MODEL_KEYS:
        if from_job.get(key):
            choice[key] = from_job[key]
        if key in changed:
            choice[key] = changed[key] or defaults[key]
    return {**choice, "speaker_used": changed.get("speaker_used", ""), "after": bool(from_job.get("after"))}


def remember_meeting_models(tid: str, **values) -> None:
    """Saves model choices for one meeting (merged with what's already saved)."""
    path = meeting_models_dir() / f"{tid}.json"
    _write_json(path, {**_read_json(path, {}), **values})


def use_speaker_model(wanted: str, emit) -> str | None:
    """The installed model that reads the transcript; says so when the chosen one is missing."""
    name = model_registry.pick_speaker_model(wanted or None)
    if wanted and name and name.split(":latest")[0] != wanted.split(":latest")[0]:
        emit({"type": "notice", "kind": "model_fallback",
              "text": f"{wanted} isn't installed, so {name} reads the transcript. "
                      "Download it in Settings → Meetings or in the panel."})
    return name


def transcriber_running() -> bool:
    """True while the transcriber is working on a job (False if it isn't reachable)."""
    try:
        return any(j.get("status") == "running" for j in meetings.jobs())
    except (meetings.TranscriberUnavailable, requests.RequestException, RuntimeError, ValueError):
        return False


TRANSCRIBER_POLL = 3.0


def wait_for_transcriber(emit) -> None:
    """Whisper/pyannote and an Ollama model never share the 16 GB: a local step waits while a
    transcription is running (cancelling the request stops the wait)."""
    told = False
    while transcriber_running():
        if not told:
            emit({"type": "status", "text": "Waiting for the running transcription to finish "
                                            "(this Mac can't hold both models at once)…"})
            told = True
        time.sleep(TRANSCRIBER_POLL)


def screen_options(frames_model: str | None = None) -> dict:
    """On-screen name options for the transcriber, from Settings → Meetings (or the meeting's own
    frames model). Only a model that can read images; a missing one falls back to an installed one."""
    current = prefs()
    wanted = current["vision_model"] if frames_model is None else frames_model
    vision = (model_registry.pick_vision_model(wanted or None) or "") if current["vision_fallback"] else ""
    return {"enabled": bool(current["screen_names"]), "vision_model": vision,
            "vision_cap": int(current["vision_cap"]) if vision else 0, "debug": bool(current["screen_debug"])}


class MeetingModelsBody(BaseModel):
    """A meeting's model choices from New meeting (None = Settings)."""
    speaker_model: str | None = None   # local Ollama tag that reads the transcript
    frames_model: str | None = None    # local vision model for video frames
    summary_model: str | None = None   # model id; "" = the chat model
    model_id: str = ""                 # the selected chat model


def plan_models(body: MeetingModelsBody) -> dict:
    """{chosen: what the meeting asked for, use: the installed models that will run, fallbacks}."""
    current = prefs()
    chosen = {"speaker_model": current["speaker_model"] if body.speaker_model is None else body.speaker_model,
              "frames_model": current["vision_model"] if body.frames_model is None else body.frames_model,
              "summary_model": current["summary_model"] if body.summary_model is None else body.summary_model}
    speaker = model_registry.pick_speaker_model(chosen["speaker_model"] or None) or ""
    frames = screen_options(chosen["frames_model"])["vision_model"]
    summary = chosen["summary_model"]  # "" = the user is asked when the summary is written
    fallbacks = []
    for key, used in (("speaker_model", speaker), ("frames_model", frames)):
        if chosen[key] and used and used.split(":latest")[0] != chosen[key].split(":latest")[0]:
            fallbacks.append({"kind": key, "wanted": chosen[key], "using": used})
    return {"chosen": chosen, "use": {"speaker_model": speaker, "frames_model": frames, "summary_model": summary},
            "fallbacks": fallbacks}


class EstimateBody(MeetingModelsBody):
    """POST /api/meetings/estimate body: the recording, the expected number of speakers and the meeting's model
    choices.
    """
    path: str
    speakers: int | None = None


def followup_estimates(duration: float, speakers: int | None, use: dict) -> dict:
    """Time for identifying speakers and writing the summary afterwards, with these models."""
    out: dict = {}
    if use["speaker_model"]:
        rng = speaker_id.estimate_seconds(speakers or 4, model_registry.speeds_for(use["speaker_model"]))
        out["speakers"] = {"model": use["speaker_model"], **rng}
    entry = (model_registry.find(use["summary_model"]) or {}) if use["summary_model"] else {}
    if not use["summary_model"]:
        out["summary"] = {"ask": True, "model": "", "model_id": "", "local": False, "low": None, "high": None}
    elif entry.get("provider") == "ollama":
        model = entry["model"]
        rng = long_context.summary_seconds(duration, agent.context_length_for(model), model_registry.speeds_for(model))
        out["summary"] = {"model": model, "model_id": use["summary_model"], "local": True, **rng}
    elif entry:
        out["summary"] = {"model": entry.get("label") or use["summary_model"], "model_id": use["summary_model"],
                          "local": False, "low": None, "high": None}
    return out


@app.post("/api/meetings/estimate")
def estimate(body: EstimateBody) -> dict:
    """POST /api/meetings/estimate: time for transcription, names from video, speaker identification and the
    summary, with the chosen models (and which missing models will be replaced).
    """
    plan = plan_models(body)
    options = screen_options(plan["use"]["frames_model"] or "")
    cap = options["vision_cap"] if options["enabled"] else 0
    data = _transcriber(meetings.estimate, body.path, cap,
                        model_registry.seconds_per_frame(options["vision_model"]) if cap else None)
    data["models"] = {**plan, "screen_enabled": options["enabled"]}
    data["followups"] = followup_estimates(float(data.get("duration") or 0), body.speakers, plan["use"])
    return data


class JobBody(MeetingModelsBody):
    """POST /api/meetings/jobs body: the recording, speaker count, model choices and whether to identify speakers
    and summarise afterwards.
    """
    path: str
    speakers: int | None = None
    after: bool = False   # identify speakers and write the summary when the transcript is ready


@app.post("/api/meetings/jobs")
def start_job(body: JobBody) -> dict:
    """POST /api/meetings/jobs: starts a transcription with this meeting's models, and the after-transcription
    follow-up when asked.
    """
    plan = plan_models(body)
    use = plan["use"]
    models = {"speaker_model": use["speaker_model"] or plan["chosen"]["speaker_model"],
              "frames_model": use["frames_model"] or plan["chosen"]["frames_model"],
              "summary_model": plan["chosen"]["summary_model"], "after": body.after}
    job = _transcriber(meetings.start, body.path, body.speakers, screen_options(use["frames_model"] or ""), models)
    if body.after:
        job["followup"] = FOLLOWUPS.start(job["id"], {"chat_model": body.model_id or prefs()["selected_model"]})
    return job


# ---------------------------------------------------------------------------
# Recording a call (Quorum Recorder through the transcriber) and its live draft
# ---------------------------------------------------------------------------

@app.get("/api/meetings/record/sources")
def record_sources() -> dict:
    """What can be recorded: calls and remote desktops first, displays, microphones, and the last choice."""
    data = _transcriber(meetings.record_sources)
    data["live_default"] = prefs()["record_live"]
    return data


class RecordStartBody(BaseModel):
    """POST /api/meetings/record/start body."""
    source: dict
    mic: str | None = None
    live: bool | None = None   # None: the saved preference
    title: str = ""


@app.post("/api/meetings/record/start")
def record_start(body: RecordStartBody) -> dict:
    """Starts recording the call; `live` (remembered) also transcribes during the meeting."""
    live = prefs()["record_live"] if body.live is None else body.live
    if body.live is not None:
        save_prefs({"record_live": bool(body.live)})
    return _transcriber(meetings.record_start, body.source, body.mic, live, body.title)


@app.get("/api/meetings/record/status")
def record_status() -> dict:
    """The recording now (polled by the sidebar indicator on every page)."""
    return _transcriber(meetings.record_status)


class RecordStopBody(MeetingModelsBody):
    """POST /api/meetings/record/stop body: transcribe right away (with New meeting's choices) or keep the file."""
    transcribe: bool = True
    speakers: int | None = None
    after: bool = False
    model_id: str | None = None


@app.post("/api/meetings/record/stop")
def record_stop(body: RecordStopBody) -> dict:
    """Ends the recording and starts its transcription with this meeting's models, plus the
    identify-and-summarise follow-up when asked (same as POST /api/meetings/jobs)."""
    plan = plan_models(body)
    use = plan["use"]
    models = {"speaker_model": use["speaker_model"] or plan["chosen"]["speaker_model"],
              "frames_model": use["frames_model"] or plan["chosen"]["frames_model"],
              "summary_model": plan["chosen"]["summary_model"], "after": body.after}
    done = _transcriber(meetings.record_stop, body.transcribe, body.speakers,
                        screen_options(use["frames_model"] or ""), models)
    job = done.get("job")
    if job and body.after:
        job["followup"] = FOLLOWUPS.start(job["id"], {"chat_model": body.model_id or prefs()["selected_model"]})
    return done


@app.get("/api/meetings/record/live")
def record_live(id: str | None = None, since: int = 0) -> dict:
    """The live draft: lines after `since`, its state, and the final transcript id once it exists."""
    return _transcriber(meetings.record_live, id, since)


@app.get("/api/meetings/record/live/markdown", response_class=PlainTextResponse)
def record_live_markdown(id: str | None = None) -> str:
    """The live draft as Markdown (Copy)."""
    return _transcriber(meetings.record_live_markdown, id)


@app.delete("/api/meetings/transcripts/{tid}/models")
def forget_meeting_models(tid: str) -> dict:
    """Forget the models picked for this meeting in its panels: back to New meeting's / Settings' choice."""
    (meeting_models_dir() / f"{tid}.json").unlink(missing_ok=True)
    return {"ok": True}


class ScreenNamesBody(BaseModel):
    """Body for re-reading names from a meeting's video with a chosen vision model."""
    frames_model: str | None = None   # read with this vision model (kept for the meeting; "" = Settings)


@app.post("/api/meetings/transcripts/{tid}/screen-names")
def read_screen_names(tid: str, body: ScreenNamesBody | None = None) -> dict:
    """Read on-screen names for a transcript made earlier (needs its video), with its frames model."""
    if body and body.frames_model is not None:
        remember_meeting_models(tid, frames_model=body.frames_model)
    frames = meeting_models(tid)["frames_model"]
    return _transcriber(meetings.read_screen_names, tid, dict(screen_options(frames), enabled=True))


@app.post("/api/meetings/voiceprints/backfill")
def backfill_voiceprints() -> dict:
    """Queue voice fingerprints for older transcripts that have none (recordings that still exist)."""
    if not prefs()["voiceprints"]:
        raise HTTPException(409, "Voice fingerprints are switched off in Settings → Meetings.")
    queued, skipped = [], []
    for meta in _transcriber(meetings.transcripts):
        if meta.get("source_type") == "text":
            continue
        full = _transcriber(meetings.transcript, meta["id"])
        if full.get("speaker_embeddings"):
            continue
        try:
            job = meetings.backfill_voiceprints(meta["id"])
            queued.append({"id": meta["id"], "title": meta.get("title", ""), "job": job["id"]})
        except RuntimeError as exc:
            skipped.append({"id": meta["id"], "title": meta.get("title", ""), "reason": str(exc)[:160]})
        except meetings.TranscriberUnavailable as exc:
            raise HTTPException(503, str(exc)) from exc
    return {"queued": queued, "skipped": skipped}


@app.get("/api/meetings/jobs")
def jobs() -> list[dict]:
    """GET /api/meetings/jobs: all transcription jobs."""
    return _transcriber(meetings.jobs)


@app.get("/api/meetings/jobs/{job_id}")
def job(job_id: str, partial: bool = False) -> dict:
    """GET /api/meetings/jobs/{id}: a job's progress (with the text so far while running) and its follow-up
    state.
    """
    data = _transcriber(meetings.job, job_id)
    data["followup"] = FOLLOWUPS.get(job_id)
    if partial and data["status"] == "running":
        data["partial"] = _transcriber(meetings.job_partial, job_id)
    return data


@app.post("/api/meetings/jobs/{job_id}/cancel")
def cancel_job(job_id: str) -> dict:
    """POST /api/meetings/jobs/{id}/cancel: cancels a queued job or stops a running one."""
    return _transcriber(meetings.cancel_job, job_id)


class UploadInit(BaseModel):
    """POST /api/meetings/uploads body: the file name and size."""
    name: str
    size: int


@app.post("/api/meetings/uploads")
def upload_init(body: UploadInit) -> dict:
    """POST /api/meetings/uploads: starts (or resumes) a chunked upload after checking the file type (415 for
    unsupported files).
    """
    if Path(body.name).suffix.lower() not in MEDIA_SUFFIXES:
        raise HTTPException(415, f"Unsupported file type “{Path(body.name).suffix or body.name}”. Use a video "
                                 "or audio recording: " + ", ".join(sorted(s[1:] for s in MEDIA_SUFFIXES)) + ".")
    return _transcriber(meetings.upload_init, body.name, body.size)


@app.put("/api/meetings/uploads/{uid}/chunks/{n}")
async def upload_chunk(uid: str, n: int, request: Request) -> dict:
    """PUT /api/meetings/uploads/{id}/chunks/{n}: passes one chunk to the transcriber."""
    data = await request.body()
    return _transcriber(meetings.upload_chunk, uid, n, data)


@app.post("/api/meetings/uploads/{uid}/complete")
def upload_complete(uid: str) -> dict:
    """POST /api/meetings/uploads/{id}/complete: finishes the upload."""
    return _transcriber(meetings.upload_complete, uid)


@app.get("/api/meetings/settings")
def meeting_settings() -> dict:
    """GET /api/meetings/settings: the transcriber's watch folder settings."""
    return _transcriber(meetings.settings)


class WatchFolder(BaseModel):
    """POST /api/meetings/settings body: the new watch folder and whether to create it."""
    folder: str = ""
    create: bool = False


@app.post("/api/meetings/settings")
def set_watch_folder(body: WatchFolder) -> dict:
    """POST /api/meetings/settings: changes the watch folder."""
    return _transcriber(meetings.set_watch_folder, body.folder, body.create)


async def conversation_text(request: Request) -> tuple[dict, str, str]:
    """Conversation text from a request: pasted JSON {text, …} or a multipart `file` (.txt .md .vtt .srt .docx
    .pdf; Word and PDF text extracted, not cut). Returns (the other fields, text, file name)."""
    if request.headers.get("content-type", "").startswith("multipart/"):
        form = await request.form()
        upload = form.get("file")
        fields = {k: str(v) for k, v in form.items() if k != "file"}
        if upload is None:
            raise HTTPException(400, "No file.")
        name = upload.filename
        suffix = Path(name).suffix.lower()
        if suffix not in CONVERSATION_TYPES:
            raise HTTPException(415, f"Unsupported file type “{suffix}”. Use .txt, .md, .vtt, .srt, .docx or .pdf.")
        raw = await upload.read()
        text = (_extract_docx(raw) if suffix == ".docx" else _extract_pdf(raw) if suffix == ".pdf"
                else raw.decode("utf-8", errors="replace"))
        return fields, text, name
    body = await request.json()
    return {k: v for k, v in body.items() if k != "text"}, str(body.get("text") or ""), ""


@app.post("/api/meetings/import")
async def import_conversation(request: Request) -> dict:
    """Conversation text (pasted JSON {title, text} or a multipart file) → transcript."""
    fields, text, name = await conversation_text(request)
    if not text.strip():
        raise HTTPException(400, "The conversation is empty.")
    return _transcriber(meetings.import_text, str(fields.get("title") or ""), text, name)


# ---------------------------------------------------------------------------
# Transcripts: view, delete, speakers, summary, chat
# ---------------------------------------------------------------------------

def labels_for(tid: str) -> dict:
    """The saved speaker labels of a meeting ({} when none yet)."""
    return _read_json(LABELS_DIR / f"{tid}.json", {})


def summary_for(tid: str) -> dict | None:
    """The saved summary of a meeting, or None."""
    return _read_json(SUMMARIES_DIR / f"{tid}.json", None)


def display_names(transcript: dict, labels: dict) -> dict[str, str]:
    """The name to show for each speaker: a label's name when there is one, else the transcript's name, else
    'Speaker 1', 'Speaker 2'….
    """
    names = dict(transcript.get("speaker_names") or {})
    for i, spk in enumerate(transcript.get("speakers") or []):
        label = labels.get(spk) or {}
        if label.get("name"):
            names[spk] = label["name"]
        names.setdefault(spk, f"Speaker {i + 1}" if spk != "UNKNOWN" else "Unknown")
    return names


@app.get("/api/meetings/transcripts")
def list_transcripts() -> list[dict]:
    """GET /api/meetings/transcripts: all transcripts (summaries only)."""
    return _transcriber(meetings.transcripts)


@app.get("/api/meetings/transcripts/{tid}")
def get_transcript(tid: str) -> dict:
    """GET /api/meetings/transcripts/{id}: a transcript with its labels, display names, summary, cloud review,
    model choices and follow-up state (voice vectors removed).
    """
    transcript = _transcriber(meetings.transcript, tid)
    labels = labels_for(tid)
    transcript["has_voiceprints"] = bool(transcript.pop("speaker_embeddings", None))
    transcript.pop("before_speaker_guess", None)
    transcript.pop("before_correction", None)
    transcript["labels"] = labels
    transcript["display_names"] = display_names(transcript, labels)
    transcript["summary"] = summary_for(tid)
    transcript["review"] = review_for(tid)
    chosen = meeting_models(tid, transcript)
    transcript["models"] = {**chosen, "frames_used": (transcript.get("screen_info") or {}).get("vision_model", ""),
                            "summary_used": (transcript["summary"] or {}).get("model", "")}
    transcript["followup"] = FOLLOWUPS.for_transcript(tid)
    return transcript


@app.get("/api/meetings/transcripts/{tid}/markdown", response_class=PlainTextResponse)
def transcript_markdown(tid: str) -> str:
    """GET /api/meetings/transcripts/{id}/markdown: the transcript as Markdown."""
    return _transcriber(meetings.markdown, tid)


@app.get("/api/meetings/transcripts/{tid}/media")
def transcript_media_info(tid: str) -> dict:
    """GET /api/meetings/transcripts/{id}/media: how the meeting can be played back (see transcriber)."""
    return _transcriber(meetings.media_info, tid)


MEDIA_PASS_HEADERS = ("content-type", "content-length", "content-range", "accept-ranges", "last-modified", "etag")


@app.get("/api/meetings/transcripts/{tid}/media/{part}")
def transcript_media(tid: str, part: str, request: Request) -> Response:
    """GET /api/meetings/transcripts/{id}/media/{part}: the recording's picture or sound for the player, streamed
    from the transcriber with Range support (seeking). Stays on this Mac."""
    if part not in ("video", "audio", "call", "mic", "mix") or not re.fullmatch(r"[\w-]{1,64}", tid):
        raise HTTPException(404, "No such part.")
    try:
        upstream = meetings.media_stream(tid, part, request.headers.get("range"))
    except meetings.TranscriberUnavailable as exc:
        raise HTTPException(503, str(exc))
    if upstream.status_code >= 400:
        detail = upstream.text[:300]
        upstream.close()
        try:
            detail = json.loads(detail).get("detail", detail)
        except ValueError:
            pass
        raise HTTPException(upstream.status_code, detail)
    headers = {k: v for k, v in upstream.headers.items() if k.lower() in MEDIA_PASS_HEADERS}

    def body():
        try:
            yield from upstream.iter_content(256 * 1024)
        finally:
            upstream.close()
    return StreamingResponse(body(), status_code=upstream.status_code, headers=headers,
                             media_type=upstream.headers.get("content-type"))


# --- other transcripts of the same meeting: correct words and speakers -------------------------------------------

REF_SOURCES = ("voice_memos", "claude", "other")


def known_terms(transcript: dict) -> list[str]:
    """Names and terms a correction may trust over an unknown spelling: people in memory (names and aliases),
    the user, projects, and names read from the screen in this meeting."""
    store = memory_store.load_store()
    terms = []
    for e in store["entries"]:
        if e["kind"] in ("person", "project", "profile"):
            terms.append(e["name"])
            terms += e.get("aliases") or []
    for info in (transcript.get("screen_names") or {}).values():
        terms += [item["name"] for item in (info or {}).get("names", []) if not item.get("truncated")]
    return [t for t in dict.fromkeys(t.strip() for t in terms if t and t.strip())][:400]


def after_correction(tid: str, correction: dict) -> dict:
    """Keeps the rest of the app in step with a corrected transcript: search sees the new words, speakers a
    reference named get that name (never over your own or your microphone's labels; withdrawn when the source
    goes), and a summary written earlier is marked as made before the correction."""
    _TRANSCRIPT_CHUNKS.pop(tid, None)
    labels = labels_for(tid)
    names = (correction or {}).get("names") or {}
    changed = False
    for spk, label in list(labels.items()):
        if label.get("source") == "reference" and spk not in names:
            labels.pop(spk)
            changed = True
    sources = {r["id"]: r.get("name") for r in (correction or {}).get("refs", [])}
    for spk, found in names.items():
        current = labels.get(spk) or {}
        if current.get("source") in ("user", "microphone"):
            continue
        evidence = [f"named in another transcript of this meeting ({', '.join(n for n in sources.values() if n)})",
                    f"{round(found['share'] * 100)}% of {found['words']} matched words"]
        label = {"name": found["name"], "role": current.get("role", ""), "confidence": 0.9, "level": "high",
                 "evidence": evidence, "source": "reference"}
        if current != label:
            labels[spk] = label
            changed = True
    if changed:
        _write_json(LABELS_DIR / f"{tid}.json", labels)
        try:
            _push_names(tid, meetings.transcript(tid), labels)
        except (meetings.TranscriberUnavailable, RuntimeError, requests.RequestException):
            pass
    summary = summary_for(tid)
    if summary and correction is not None:
        stale = bool(correction.get("changes")) and summary.get("created_at", "") < correction.get("updated_at", "")
        if summary.get("stale") != stale:
            summary["stale"] = stale
            _write_json(SUMMARIES_DIR / f"{tid}.json", summary)
    return correction


@app.get("/api/meetings/transcripts/{tid}/references")
def list_references(tid: str) -> dict:
    """GET /api/meetings/transcripts/{id}/references: the other transcripts added and the correction."""
    return _transcriber(meetings.references, tid)


@app.post("/api/meetings/transcripts/{tid}/references")
async def add_reference(tid: str, request: Request) -> dict:
    """POST /api/meetings/transcripts/{id}/references: another transcript of the meeting as text (pasted JSON
    {source, name, text} or a file), applied at once. Stays on this Mac; no model is used."""
    fields, text, filename = await conversation_text(request)
    if not text.strip():
        raise HTTPException(400, "The text is empty.")
    source = fields.get("source") if fields.get("source") in REF_SOURCES else "other"
    name = str(fields.get("name") or "") or (Path(filename).stem if filename else "")
    transcript = _transcriber(meetings.transcript, tid)
    correction = _transcriber(meetings.add_text_reference, tid, source, name[:120], text, known_terms(transcript))
    return after_correction(tid, correction)


class AudioReferenceBody(BaseModel):
    """POST …/references/audio: an uploaded recording of the same meeting (e.g. the Voice Memo)."""
    path: str = Field(max_length=2000)
    source: str = Field("voice_memos", pattern=r"^(voice_memos|claude|other)$")
    name: str = Field("", max_length=120)


@app.post("/api/meetings/transcripts/{tid}/references/audio")
def add_audio_reference(tid: str, body: AudioReferenceBody) -> dict:
    """POST …/references/audio: transcribes the uploaded recording (a job) and then corrects the meeting."""
    transcript = _transcriber(meetings.transcript, tid)
    return _transcriber(meetings.add_audio_reference, tid, body.path, body.source, body.name, known_terms(transcript))


@app.delete("/api/meetings/transcripts/{tid}/references/{rid}")
def delete_reference(tid: str, rid: str) -> dict:
    """DELETE …/references/{ref}: removes a source; the meeting is corrected again without it."""
    return after_correction(tid, _transcriber(meetings.delete_reference, tid, rid))


@app.post("/api/meetings/transcripts/{tid}/corrections/{action}")
def corrections_action(tid: str, action: str) -> dict:
    """POST …/corrections/undo (back to the original, sources kept) or …/corrections/apply (again)."""
    if action not in ("undo", "apply"):
        raise HTTPException(404, "No such action.")
    terms = known_terms(_transcriber(meetings.transcript, tid)) if action == "apply" else None
    return after_correction(tid, _transcriber(meetings.correction_action, tid, action, None, terms))


@app.post("/api/meetings/transcripts/{tid}/corrections/{cid}/{action}")
def correction_change(tid: str, cid: str, action: str) -> dict:
    """POST …/corrections/{change}/revert|restore: one change."""
    if action not in ("revert", "restore"):
        raise HTTPException(404, "No such action.")
    return after_correction(tid, _transcriber(meetings.correction_action, tid, action, cid))


@app.delete("/api/meetings/transcripts/{tid}")
def delete_transcript(tid: str, recording: bool = False) -> dict:
    """DELETE /api/meetings/transcripts/{id}: deletes a transcript with its labels, summary and review (and, if
    asked, its uploaded recording).
    """
    result = _transcriber(meetings.delete_transcript, tid, recording)
    for path in (LABELS_DIR / f"{tid}.json", SUMMARIES_DIR / f"{tid}.json", review_file(tid)):
        path.unlink(missing_ok=True)
    return result


def _push_names(tid: str, transcript: dict, labels: dict) -> None:
    """Names the transcript (and its Markdown for chat/minutes) uses: confirmed + medium/high guesses."""
    names = {spk: lab["name"] for spk, lab in labels.items()
             if lab.get("name") and (lab.get("source") == "user" or lab.get("confidence", 0) >= 0.5)}
    keep = {k: v for k, v in (transcript.get("speaker_names") or {}).items() if k not in labels}
    meetings.set_speaker_names(tid, {**keep, **names})


class IdentifyBody(BaseModel):
    """POST …/identify body: the selected chat model (a cloud one may review), the model that reads the
    transcript, and answers to the cloud-review question.
    """
    model_id: str = ""                 # the selected chat model: a cloud one reviews the names
    speaker_model: str | None = None   # read the transcript with this local model (kept for the meeting)
    review: bool = True                # False: local pass only (after transcription, nobody to ask)
    confirm: str | None = None   # "cloud" | "local": answer to the cloud-review confirm (Auto)
    review_only: bool = False    # the local pass already ran; only do the cloud review


def review_file(tid: str) -> Path:
    """Where the cloud review of a meeting's speaker names is saved."""
    return LABELS_DIR / f"{tid}.review.json"


def review_for(tid: str) -> dict | None:
    """The saved cloud review of a meeting, or None."""
    return _read_json(review_file(tid), None)


def transcript_in_confidential_chat(tid: str) -> bool:
    """True when this meeting is attached to a chat that was marked confidential (then it's never sent for cloud
    review).
    """
    for directory in (sessions_dir(),):
        if not directory.exists():
            continue
        for path in directory.glob("*.json"):
            record = _read_json(path, {})
            if record.get("confidential") and any(a.get("transcript_id") == tid for a in record.get("attachments") or []):
                return True
    return False


def review_skip_reason(provider, project: dict, tid: str) -> str:
    """Why the cloud review doesn't run ("" = it may run)."""
    if provider.is_local:
        return "local"
    if not prefs()["speaker_cloud_review"]:
        return "off"
    if project.get("default_model_mode") == "local":
        return f"the project “{project.get('name', '')}” is confidential (local only)"
    if transcript_in_confidential_chat(tid):
        return "this meeting is attached to a confidential chat"
    return ""


def speaker_people() -> list[dict]:
    """People memory for speaker identification (voice prints only when they're switched on)."""
    known = people_store.load()
    if not prefs()["voiceprints"]:
        known = [dict(p, voiceprints=[]) for p in known]
    return known


def save_microphone_voiceprint(tid: str, transcript: dict, labels: dict, me: dict | None) -> bool:
    """A call Quorum recorded: your microphone track's voice fingerprint is saved as yours (when your name is
    known and voice fingerprints are on), so you're recognised in recordings made any other way too."""
    if not prefs()["voiceprints"] or not (me or {}).get("name"):
        return False
    vectors = transcript.get("speaker_embeddings") or {}
    for spk, label in labels.items():
        if label.get("source") == "microphone" and vectors.get(spk):
            people_store.learn(tid, spk, me["name"], label.get("role", ""), vectors[spk], source="microphone")
            return True
    return False


def save_screen_voiceprints(tid: str, transcript: dict, labels: dict, emit) -> list[str]:
    """Voice fingerprints saved automatically when the on-screen name is consistent (source "screen")."""
    if not prefs()["voiceprints"]:
        return []
    vectors = transcript.get("speaker_embeddings") or {}
    saved = []
    known_names = [p["name"] for p in people_store.load()]
    for spk, (name, count) in speaker_id.screen_confirmed(transcript, known_names).items():
        label = labels.get(spk) or {}
        if label.get("source") == "user" and label.get("name", "").lower() != name.lower():
            continue  # the user said this is someone else
        if label.get("name", "").lower() != name.lower():
            continue  # the combined evidence picked someone else: don't teach this voice
        people_store.learn(tid, spk, name, label.get("role", ""), vectors.get(spk), source="screen")
        people_store.note_screen_name(tid, spk, name, count)
        saved.append(name)
    if saved:
        emit({"type": "status", "text": "Remembered the voices of " + ", ".join(saved) + " (their names were on screen)"})
    return saved


def run_cloud_review(tid: str, transcript: dict, labels: dict, provider, emit) -> dict:
    """The cloud model reviews the local names; changes are applied and can be kept or reverted."""
    name = model_name(provider)
    emit({"type": "status", "text": f"Asking {name} to review the speaker names…"})
    notice = cloud_notice(provider, "the transcript, speaker-name suggestions and people from your memory "
                                    "relevant to this meeting")
    if notice:
        emit(notice)
    budget = int(summaries.CLOUD_CONTEXT_TOKENS * 3.2) if hasattr(summaries, "CLOUD_CONTEXT_TOKENS") else \
        speaker_review.DEFAULT_CHARS
    try:
        result = speaker_review.review(transcript, labels, people_store.load(), provider.complete,
                                       budget_chars=min(budget, speaker_review.DEFAULT_CHARS),
                                       progress=lambda text: emit({"type": "status", "text": text}))
    except (requests.RequestException, ValueError) as exc:
        kind = getattr(exc, "kind", "") or ("bad_json" if isinstance(exc, ValueError) else "network")
        why = {"missing_key": "no API key", "invalid_key": "the API key was rejected", "rate_limited": "rate limit",
               "offline": "offline", "bad_json": "the reply wasn't valid JSON after a retry",
               "overloaded": "the service is overloaded"}.get(kind, str(exc)[:120])
        record = {"status": "failed", "model": name, "provider": provider_label(provider), "error": why,
                  "created_at": datetime.now().isoformat(timespec="seconds"), "changes": [], "rejected": []}
        _write_json(review_file(tid), record)
        emit({"type": "notice", "kind": "review_failed",
              "text": f"The cloud review by {name} didn't run ({why}). The local names are kept."})
        return record
    labels = speaker_review.apply(labels, result, name)
    _write_json(LABELS_DIR / f"{tid}.json", labels)
    _push_names(tid, transcript, labels)
    record = {"status": "done", "model": name, "model_id": provider.model_id, "provider": provider_label(provider),
              "created_at": datetime.now().isoformat(timespec="seconds"), **result}
    _write_json(review_file(tid), record)
    return record


def identify_job(tid: str, body: IdentifyBody):
    """Local pass (always on this Mac), then, when a cloud model is selected, its review."""
    def run(emit):
        """The work of identifying speakers (run by sse() or by the follow-up runner)."""
        transcript = meetings.transcript(tid)
        if not prefs()["voiceprints"]:
            transcript = dict(transcript, speaker_embeddings={})
        # The cloud review only runs when the request names a cloud chat model (the one selected
        # in the app); a request without one is the local pass only.
        selected = provider_for(body.model_id) if body.model_id else None
        project = active_project()
        if body.review_only:
            labels = labels_for(tid)
        else:
            if body.speaker_model is not None:
                remember_meeting_models(tid, speaker_model=body.speaker_model)
            model = use_speaker_model(meeting_models(tid, transcript)["speaker_model"], emit)
            complete = None
            if model:
                wait_for_transcriber(emit)
                local = model_registry.OllamaProvider(model)
                # The reply limit grows with the number of speakers (one JSON row each).
                limit = speaker_id.reply_tokens(len(speaker_id.speaker_ids(
                    [s for s in transcript.get("segments", []) if s.get("text")])))
                complete = lambda msgs: local.complete(msgs, max_tokens=limit, temperature=0)  # noqa: E731
                emit({"type": "status", "text": f"Reading the transcript with {model} on this Mac"})
            else:
                emit({"type": "notice", "text": "No local model installed: using voices, names on screen and name clues only."})
            known = speaker_people()
            prior = [n for t in meetings.transcripts() if t["id"] != tid
                     for n in (t.get("speaker_names") or {}).values()]
            emit({"type": "status", "text": "Looking for the same people and projects in your past conversations…"})
            past = related_conversation_people(transcript, known)
            text = " ".join(seg.get("text", "") for seg in transcript.get("segments", []))
            facts = memory_store.facts_for_meeting(text, prior + [p["name"] for p in known])
            me = memory_store.user_person()
            labels = speaker_id.identify(transcript, known, memory_store.project_facts(project) + facts, complete,
                                         keep=labels_for(tid), prior_names=prior,
                                         progress=lambda t: emit({"type": "status", "text": t}), past=past,
                                         me=me)
            _write_json(LABELS_DIR / f"{tid}.json", labels)
            remember_meeting_models(tid, speaker_used=model or "")
            review_file(tid).unlink(missing_ok=True)
            _push_names(tid, transcript, labels)
            save_screen_voiceprints(tid, transcript, labels, emit)
            save_microphone_voiceprint(tid, transcript, labels, me)
            emit({"type": "labels", "labels": labels,
                  "display_names": display_names(meetings.transcript(tid), labels), "model": model or ""})
        skip = review_skip_reason(selected, project, tid) if body.review and selected else "off"
        review = None
        if skip in ("local", "off"):
            pass
        elif skip:
            emit({"type": "notice", "kind": "review_skipped", "text": f"No cloud review: {skip}."})
        elif body.confirm == "local":
            pass  # the user kept the names on this Mac
        elif prefs()["confirm_cloud_sensitive"] and body.confirm != "cloud":
            emit({"type": "confirm", "kind": "sensitive", "stage": "speaker_review",
                  "reasons": ["meeting transcripts are treated as confidential"],
                  "model": model_name(selected), "provider": provider_label(selected),
                  "question": f"Send speaker names and the transcript to {model_name(selected)} for review?"})
            return
        else:
            review = run_cloud_review(tid, transcript, labels_for(tid), selected, emit)
        labels = labels_for(tid)
        emit({"type": "done", "labels": labels, "display_names": display_names(meetings.transcript(tid), labels),
              "review": review})

    return run


@app.post("/api/meetings/transcripts/{tid}/identify")
def identify_speakers(tid: str, body: IdentifyBody) -> StreamingResponse:
    """POST /api/meetings/transcripts/{id}/identify: identifies speakers, streamed (SSE)."""
    return sse(identify_job(tid, body))


class ReviewAction(BaseModel):
    """Body for Keep/Revert of the cloud review: the speaker it applies to."""
    speaker: str = ""


@app.post("/api/meetings/transcripts/{tid}/speakers/review/{action}")
def review_action(tid: str, action: str, body: ReviewAction) -> dict:
    """Keep or revert one cloud change ("keep" / "revert"), or all of them ("revert-all")."""
    labels, record = labels_for(tid), review_for(tid)
    if record is None or action not in ("keep", "revert", "revert-all"):
        raise HTTPException(404, "No cloud review for this meeting.")
    transcript = _transcriber(meetings.transcript, tid)
    targets = [c for c in record.get("changes", []) if c.get("state") == "pending"
               and (action == "revert-all" or c["speaker"] == body.speaker)]
    if not targets:
        raise HTTPException(404, "No pending change for that speaker.")
    vectors = transcript.get("speaker_embeddings") or {} if prefs()["voiceprints"] else {}
    for change in targets:
        spk = change["speaker"]
        if action == "keep":
            change["state"] = "kept"
            label = labels.get(spk) or {}
            if label.get("source") == "cloud":
                label.setdefault("cloud", {})["kept"] = True
            person = people_store.learn(tid, spk, change["name"], change.get("role", ""), vectors.get(spk),
                                        source="cloud_review")
            if person and change.get("role"):
                with memory_store.LOCK:
                    store = memory_store.load_store()
                    entry = memory_store.get(person["id"], store)
                    if entry is not None:
                        memory_store.add_fact(entry, f"{change['role']} in “{transcript.get('title', 'a meeting')}”",
                                              "Meetings", "cloud_review")
                        memory_store.save_store(store)
        else:
            change["state"] = "reverted"
            labels = speaker_review.revert(labels, spk)
    _write_json(LABELS_DIR / f"{tid}.json", labels)
    _write_json(review_file(tid), record)
    _push_names(tid, transcript, labels)
    return {"labels": labels, "display_names": display_names(meetings.transcript(tid), labels), "review": record}


class SpeakerCorrection(BaseModel):
    """PUT …/speakers body: the names and roles the user set, per speaker label."""
    labels: dict[str, dict]  # {"SPEAKER_00": {"name": "Owen", "role": "QA"}}


@app.put("/api/meetings/transcripts/{tid}/speakers")
def correct_speakers(tid: str, body: SpeakerCorrection) -> dict:
    """User-confirmed names: saved on the transcript and taught to people memory (voice + role)."""
    transcript = _transcriber(meetings.transcript, tid)
    vectors = (transcript.get("speaker_embeddings") or {}) if prefs()["voiceprints"] else {}
    labels = labels_for(tid)
    record = review_for(tid)
    for spk, value in body.labels.items():
        if spk not in (transcript.get("speakers") or []):
            raise HTTPException(400, f"Unknown speaker {spk}.")
        name, role = str(value.get("name") or "").strip(), str(value.get("role") or "").strip()
        if name:
            labels[spk] = {"name": name, "role": role, "confidence": 1.0, "level": "confirmed",
                           "evidence": ["confirmed by you"], "source": "user"}
        else:
            labels.pop(spk, None)
        for change in (record or {}).get("changes", []):
            if change["speaker"] == spk and change.get("state") == "pending":
                change["state"] = "edited"
        people_store.learn(tid, spk, name, role, vectors.get(spk))
    _write_json(LABELS_DIR / f"{tid}.json", labels)
    if record is not None:
        _write_json(review_file(tid), record)
    _push_names(tid, transcript, labels)
    return {"labels": labels, "display_names": display_names(meetings.transcript(tid), labels)}


@app.post("/api/meetings/transcripts/{tid}/split-speakers")
def split_speakers(tid: str, body: IdentifyBody) -> StreamingResponse:
    """For text without speaker turns: the local model splits it into turns (speaker_guess)."""
    def run(emit):
        """Splits text without speaker turns into turns with the local speaker model, part by part."""
        meta = meetings.transcript(tid)
        if body.speaker_model is not None:
            remember_meeting_models(tid, speaker_model=body.speaker_model)
        model = use_speaker_model(meeting_models(tid, meta)["speaker_model"], emit)
        if not model:
            emit({"type": "error", "kind": "local_model",
                  "message": "No local model installed. Download one in Settings → Local models."})
            return
        sentences = speaker_guess.split_sentences(meta["segments"])
        wait_for_transcriber(emit)
        provider = model_registry.OllamaProvider(model)
        started = time.time()

        def progress(done: int, total: int) -> None:
            """Reports 'Part N of M' and the time left."""
            left = (time.time() - started) / max(done, 1) * (total - done)
            emit({"type": "progress", "done": done, "total": total,
                  "text": f"Part {done} of {total}" + (f" · ~{meetings.fmt_seconds(left)} left" if done < total else "")})

        emit({"type": "progress", "done": 0, "total": 1, "text": f"Reading the conversation with {model}…"})
        labels = speaker_guess.guess(sentences, lambda m: provider.complete(m, max_tokens=2500, temperature=0),
                                     progress)
        segments, names = speaker_guess.to_segments(sentences, labels, bool(meta.get("timestamps")))
        meetings.save_guessed_speakers(tid, segments, names, model)
        (LABELS_DIR / f"{tid}.json").unlink(missing_ok=True)
        emit({"type": "done", "speakers": len(set(labels))})

    return sse(run)


class SummaryBody(BaseModel):
    """POST …/summary body: the summary model (kept for the meeting), the answer to the privacy question, meeting
    type, detail level and sections.
    """
    model_id: str = ""                  # the selected chat model (not used to pick the summary model)
    summary_model: str | None = None    # write with this model (kept for the meeting; "" = Settings)
    confirm: str | None = None
    meeting_type: str = "auto"
    detail: str = "standard"
    sections: dict[str, bool] | None = None
    sensitive_detail: bool = False


def notes_memory(transcript: dict) -> dict:
    """What meeting notes may use from memory: people (names, roles), project names, the user."""
    store = memory_store.load_store()
    people = [{"name": e["name"], "aliases": e.get("aliases") or [], "role": e.get("role", ""), "team": e.get("team", "")}
              for e in store["entries"] if e["kind"] == "person"]
    projects_ = [e["name"] for e in store["entries"] if e["kind"] == "project"]
    me = memory_store.user_person(store)
    return {"people": people, "projects": projects_, "me": me["name"] if me else ""}


def summary_provider(tid: str, body: SummaryBody, emit):
    """The meeting's summary model; a missing local one falls back, and says so. None when no
    model has been chosen (for this meeting or in Settings): the user is asked (`choose_model`)."""
    if body.summary_model is not None:
        remember_meeting_models(tid, summary_model=body.summary_model)
    wanted = meeting_models(tid)["summary_model"]
    if not wanted:
        emit({"type": "choose_model", "kind": "summary",
              "text": "Which model should write the summary? Pick one; it's kept for this meeting."})
        return None
    entry = model_registry.find(wanted)
    if entry and entry.get("provider") == "ollama":
        installed = model_registry.ollama_state()["installed"]
        if not model_registry._installed_name(entry["model"], installed):
            name = model_registry.pick_local_model()
            if name:
                emit({"type": "notice", "kind": "model_fallback",
                      "text": f"{entry.get('label') or entry['model']} isn't installed, so {name} writes the summary."})
                return model_registry.OllamaProvider(name)
    return provider_for(wanted)


def summary_local_model() -> str | None:
    """The local model that writes a summary when the user answers "Use local model" to the cloud question:
    the Settings summary model if it's local, else the meeting default (Gemma 4 E4B), else any ready local model.
    """
    wanted = model_registry.find(prefs()["summary_model"]) if prefs()["summary_model"] else None
    preferred = wanted["model"] if wanted and wanted.get("provider") == "ollama" else model_registry.load_registry()["speaker_default"]
    return model_registry.pick_local_model(preferred)


def summary_job(tid: str, body: SummaryBody):
    """Builds the work of writing a meeting's notes (run by sse() or the follow-up runner)."""
    def run(emit):
        """Writes the notes: picks the model (asking when none is chosen), asks before sending to a cloud model,
        waits while a transcription runs, generates the notes, saves them.
        """
        provider = summary_provider(tid, body, emit)
        if provider is None:
            return  # waiting for the user to choose the model
        # A cloud model asks first; the answer comes back as confirm "cloud" (send it) or "local" (use a local
        # model this time). Either answer must skip the question, or the user is asked again forever.
        if not provider.is_local and body.confirm not in ("cloud", "local") and prefs()["confirm_cloud_sensitive"]:
            emit({"type": "confirm", "kind": "sensitive",
                  "reasons": ["meeting transcripts are treated as confidential"],
                  "local_model": summary_local_model()})
            return
        if body.confirm == "local" and not provider.is_local:
            name = summary_local_model()
            if not name:
                emit({"type": "error", "kind": "local_model", "message": "No local model installed."})
                return
            provider = model_registry.OllamaProvider(name)
        if provider.is_local:
            wait_for_transcriber(emit)
        meta = meetings.transcript(tid)
        length = (agent.context_length_for(provider.backend.model) if provider.is_local
                  else summaries.CLOUD_CONTEXT_TOKENS)
        emit({"type": "start", "model": model_label(provider), "model_id": provider.model_id})
        started = time.time()
        cache = _CACHES.setdefault(f"summary:{tid}", {}).setdefault(provider.model_id, {})
        record = {"model_id": provider.model_id, "model": model_label(provider)}
        if prefs()["meeting_notes_skill"]:
            labels = labels_for(tid)
            detail = body.detail if body.detail in meeting_notes.DETAIL_LEVELS else "standard"
            if provider.is_local:  # bounded, low-temperature JSON; cloud requests stay exactly as they are
                complete = lambda m: provider.complete(m, max_tokens=6000, temperature=0.2)  # noqa: E731
                stream = lambda m: llm_backends._ollama_stream(m, provider.backend, 6000, 0.2)  # noqa: E731
            else:
                complete, stream = provider.complete, provider.stream
            result = meeting_notes.generate(meta, display_names(meta, labels), labels, complete, stream, emit,
                                            meeting_type=body.meeting_type, detail=detail,
                                            sensitive_detail=body.sensitive_detail, context_length=length,
                                            cache=cache.setdefault("notes", {}), memory=notes_memory(meta))
            sections = body.sections or meeting_notes.default_sections(detail)
            record.update(skill="meeting-notes", notes=result["notes"], meeting_type=result["meeting_type"],
                          detected_type=result["detected_type"], detail=detail, sections=sections,
                          warnings=result["warnings"], follow_up=result["follow_up"], valid=result["valid"],
                          attempts=result["attempts"], parts=result["parts"], edited=False,
                          markdown=meeting_notes.render(result["notes"], tid, result["meeting_type"], detail, sections,
                                                        result["follow_up"]))
        else:
            record["markdown"] = summaries.summarize(meetings.markdown(tid), meta["title"], length, provider.stream,
                                                     provider.complete, emit, cache=cache,
                                                     speeds=llm_backends.MEASURED_SPEEDS.get(provider.backend.model))
        record.update(created_at=datetime.now().isoformat(timespec="seconds"), seconds=round(time.time() - started, 1))
        _write_json(SUMMARIES_DIR / f"{tid}.json", record)
        save_speeds()
        emit({"type": "done", "summary": record})

    return run


@app.post("/api/meetings/transcripts/{tid}/summary")
def summarize(tid: str, body: SummaryBody) -> StreamingResponse:
    """POST /api/meetings/transcripts/{id}/summary: writes the notes, streamed (SSE)."""
    return sse(summary_job(tid, body))


# ---------------------------------------------------------------------------
# After transcription: identify speakers, then write the summary (New meeting option)
# ---------------------------------------------------------------------------

class FollowUps:
    """Runs here, not in the browser, so it works with the page closed. State per transcription job
    in meeting_models/jobs/<job id>.json; unfinished ones resume when the API starts.

    States: waiting (for the transcript) → running (step "speakers", then "summary") → done |
    needs_ok (the summary model is a cloud model and transcripts ask first) | needs_model (no summary
    model chosen: the user picks one) | error | cancelled."""

    TERMINAL = {"done", "needs_ok", "needs_model", "error", "cancelled"}
    POLL = 3.0

    def __init__(self) -> None:
        """No follow-ups running yet; a lock protects the thread list."""
        self._threads: dict[str, threading.Thread] = {}
        self._lock = threading.Lock()

    def _file(self, job_id: str) -> Path:
        """The state file of one follow-up (the job id is cleaned so it's a safe file name)."""
        return meeting_models_dir() / "jobs" / f"{re.sub(r'[^0-9a-zA-Z_-]', '', job_id)}.json"

    def get(self, job_id: str) -> dict | None:
        """The saved state of a follow-up, or None."""
        return _read_json(self._file(job_id), None)

    def for_transcript(self, tid: str) -> dict | None:
        """The latest follow-up that worked on this transcript, or None."""
        folder = meeting_models_dir() / "jobs"
        found = [d for d in (_read_json(f, None) for f in folder.glob("*.json")) if d and d.get("transcript_id") == tid] \
            if folder.exists() else []
        return max(found, key=lambda d: d.get("updated_at", ""), default=None)

    def _save(self, key: str, /, **update) -> dict:
        """Merges new values into a follow-up's saved state and stamps the time."""
        data = {**(self.get(key) or {}), **update, "updated_at": datetime.now().isoformat(timespec="seconds")}
        _write_json(self._file(key), data)
        return data

    def start(self, job_id: str, plan: dict) -> dict:
        """Records a new follow-up for a job and starts its thread."""
        data = self._save(job_id, job_id=job_id, state="waiting", step="transcription", message="", **plan)
        self._spawn(job_id)
        return data

    def resume(self) -> None:
        """Restarts the threads of follow-ups that hadn't finished (called at API start-up)."""
        folder = meeting_models_dir() / "jobs"
        for path in folder.glob("*.json") if folder.exists() else []:
            data = _read_json(path, {})
            if data.get("job_id") and data.get("state") not in self.TERMINAL:
                self._spawn(data["job_id"])

    def _spawn(self, job_id: str) -> None:
        """Starts the follow-up's thread unless one is already running for that job."""
        with self._lock:
            if job_id in self._threads and self._threads[job_id].is_alive():
                return
            thread = threading.Thread(target=self._run, args=(job_id,), daemon=True, name=f"followup-{job_id}")
            self._threads[job_id] = thread
            thread.start()

    def wait(self, job_id: str, timeout: float = 30) -> None:  # tests
        """Waits for a follow-up's thread to finish (used by tests)."""
        thread = self._threads.get(job_id)
        if thread:
            thread.join(timeout)

    def _wait_for_transcript(self, job_id: str) -> str | None:
        """Polls the transcriber until the job is done (returns the transcript id) or failed/cancelled (returns
        None).
        """
        while True:
            try:
                job = meetings.job(job_id)
            except meetings.TranscriberUnavailable:
                time.sleep(self.POLL)
                continue
            except (requests.RequestException, RuntimeError, ValueError):
                self._save(job_id, state="error", message="The transcriber no longer knows this job (it was restarted).")
                return None
            if job.get("status") == "done" and job.get("transcript_id"):
                return job["transcript_id"]
            if job.get("status") in ("error", "cancelled"):
                self._save(job_id, state="cancelled", message=f"The transcription {job['status'].replace('error', 'failed')}.")
                return None
            time.sleep(self.POLL)

    @staticmethod
    def _collect(run) -> list[dict]:
        """Runs an identify or summary job without a browser, collecting its events and turning exceptions into
        'error' events.
        """
        events: list[dict] = []
        try:
            run(events.append)
        except llm_backends.LocalModelError as exc:
            events.append({"type": "error", "message": str(exc)})
        except cloud_providers.CloudProviderError as exc:
            events.append({"type": "error", "message": str(exc)})
        except (meetings.TranscriberUnavailable, requests.RequestException) as exc:
            events.append({"type": "error", "message": str(exc)[:200]})
        return events

    def _run(self, job_id: str) -> None:
        """The follow-up thread: waits for the transcript, identifies speakers, frees that model's memory, then
        writes the summary; every step is saved so the browser can show progress.
        """
        try:
            plan = self.get(job_id) or {}
            tid = plan.get("transcript_id") or self._wait_for_transcript(job_id)
            if not tid:
                return
            # Step 1: identify speakers on this Mac (no cloud review: nobody is there to approve it). Skipped
            # when resuming after a restart that happened during the summary step.
            if plan.get("step") in (None, "transcription", "speakers"):
                self._save(job_id, transcript_id=tid, state="running", step="speakers",
                           message="Identifying speakers on this Mac…")
                events = self._collect(identify_job(tid, IdentifyBody(review=False)))
                error = next((e for e in events if e["type"] == "error"), None)
                if error:
                    self._save(job_id, state="error", message=f"Speaker identification failed: {error['message']}")
                    return
                used = next((e.get("model") for e in events if e["type"] == "labels"), "")
                self._save(job_id, speaker_model_used=used)
                if used:
                    model_registry.unload_local(used)  # free it before the summary model loads
            # Step 2: write the summary. It stops at 'choose a model' when none is set, and at 'needs OK' for a
            # cloud model, exactly like pressing Generate by hand.
            self._save(job_id, state="running", step="summary", message="Writing the summary…")
            events = self._collect(summary_job(tid, SummaryBody(model_id=plan.get("chat_model", ""))))
            choose = next((e for e in events if e["type"] == "choose_model"), None)
            if choose:
                self._save(job_id, state="needs_model", message="Speakers identified. Choose which model writes the "
                                                                "summary in the meeting's Summary tab.")
                return
            ask = next((e for e in events if e["type"] == "confirm"), None)
            error = next((e for e in events if e["type"] == "error"), None)
            done = next((e for e in events if e["type"] == "done"), None)
            if ask:
                self._save(job_id, state="needs_ok", message="The summary model is a cloud model: open the meeting "
                                                             "and approve sending the transcript, or pick a local model.")
            elif error or not done:
                self._save(job_id, state="error", message=f"The summary failed: {(error or {}).get('message', 'no result')}")
            else:
                self._save(job_id, state="running", step="check", summary_model_used=done["summary"].get("model", ""),
                           message="Checking against earlier meetings…")
                # Step 3: the meeting check, on a local model only (a cloud check model waits for the user).
                events = self._collect(check_job(CheckBody(transcript_id=tid, unattended=True)))
                found = next((e for e in events if e["type"] == "alerts"), None)
                note = (f" {len(found['new'])} alert{'s' if len(found['new']) != 1 else ''} from the meeting check."
                        if found and found["new"] else "")
                self._save(job_id, state="done", message="Speakers identified and summary written." + note)
        except Exception as exc:  # keep the API up; the panels still work by hand
            log.exception("Follow-up after transcription failed")
            self._save(job_id, state="error", message=f"{type(exc).__name__}: {str(exc)[:160]}")


# ---------------------------------------------------------------------------
# Meeting check: conflicts with earlier meetings, things to raise, pending items, my actions
# ---------------------------------------------------------------------------

def checks_dir() -> Path:
    """Alerts per meeting: rec-<recording id>.json (during the call) or t-<transcript id>.json."""
    return DATA_DIR / "meeting_checks"


def my_actions_file() -> Path:
    """The user's tracked actions ("My actions"), per project."""
    return DATA_DIR / "my_actions.json"


def check_key(recording_id: str | None, transcript_id: str | None) -> str:
    """The alerts file for a meeting (ids are checked: no path tricks)."""
    if transcript_id and re.fullmatch(r"[0-9a-zA-Z_-]{1,40}", transcript_id):
        return f"t-{transcript_id}"
    if recording_id and re.fullmatch(r"[0-9a-f]{12}", recording_id):
        return f"rec-{recording_id}"
    raise HTTPException(400, "Say which meeting to check.")


def load_alerts(key: str) -> list[dict]:
    return _read_json(checks_dir() / f"{key}.json", {"alerts": []}).get("alerts", [])


def save_alerts(key: str, alerts: list[dict]) -> None:
    _write_json(checks_dir() / f"{key}.json", {"alerts": alerts})


def meeting_alerts(recording_id: str | None, transcript_id: str | None) -> list[dict]:
    """A meeting's alerts: its own, plus those found during the call when the transcript came from a recording."""
    key = check_key(recording_id, transcript_id)
    alerts = [dict(a, key=key) for a in load_alerts(key)]
    if transcript_id and not recording_id:
        try:
            rec = (meetings.transcript(transcript_id) or {}).get("recording_id")
        except (meetings.TranscriberUnavailable, RuntimeError):
            rec = None
        if rec:
            ids = {a["id"] for a in alerts}
            alerts = [dict(a, key=f"rec-{rec}") for a in load_alerts(f"rec-{rec}") if a["id"] not in ids] + alerts
    return alerts  # each with the `key` of the file it lives in (for Dismiss / Track)


def load_my_actions() -> list[dict]:
    return _read_json(my_actions_file(), {"items": []}).get("items", [])


def save_my_actions(items: list[dict]) -> None:
    _write_json(my_actions_file(), {"items": items})


def earlier_meetings(exclude: set[str], attendees: set[str], project: dict, now: datetime) -> list[dict]:
    """Earlier meetings with saved notes, newest first (at most 8): from the last `check_days`, in the same project
    (every meeting counts in the default project, or with `check_all_projects`) or with someone in common."""
    current = prefs()
    since = now - timedelta(days=int(current["check_days"]))
    everywhere = current["check_all_projects"] or project.get("id") == projects.DEFAULT_PROJECT_ID
    found = []
    for meta in meetings.transcripts():
        if meta["id"] in exclude:
            continue
        record = summary_for(meta["id"])
        notes = (record or {}).get("notes")
        if not notes:
            continue
        date = (meta.get("created_at") or "")[:19]
        try:
            if date and datetime.fromisoformat(date) < since:
                continue
        except ValueError:
            pass
        names = {n.lower() for n in (meta.get("speaker_names") or {}).values() if n}
        names |= {str(a.get("name", a) if isinstance(a, dict) else a).lower()
                  for a in ((notes.get("meeting") or {}).get("attendees") or [])}
        same_project = (notes.get("meeting") or {}).get("project", "").strip().lower() == project.get("name", "").lower()
        if everywhere or same_project or (names & attendees):
            found.append({"tid": meta["id"], "title": meta.get("title", ""), "date": date, "notes": notes})
    found.sort(key=lambda m: m["date"], reverse=True)
    return found[:8]


def earlier_passages(meeting_list: list[dict], lines: list[dict], limit: int = 6) -> list[dict]:
    """Passages of those meetings' transcripts that match what is being said now (keywords, BM25)."""
    tids = {m["tid"] for m in meeting_list}
    if not tids or not lines:
        return []
    chunks = [c for c in gather_chunks(["transcripts"]) if c.doc_id in tids and c.kind == "transcript"]
    query = " ".join(l["text"] for l in lines[-25:])[:1500]
    hits = search_index.search(query, chunks, None, None, limit=limit)
    out = []
    for hit in hits[:limit]:
        chunk = hit["chunk"] if isinstance(hit, dict) and "chunk" in hit else hit
        if not hasattr(chunk, "text"):
            continue
        out.append({"tid": chunk.doc_id, "meeting": chunk.title, "date": chunk.date[:10],
                    "time": chunk.meta.get("timestamp", ""), "text": chunk.text[:420]})
    return meeting_check.number(out, "P")


def check_model_for(body: "CheckBody", project: dict):
    """The model for the check: Settings → Meetings, else the local default; a project set to local stays local."""
    wanted = prefs()["check_model"]
    provider = provider_for(wanted) if wanted else None
    if provider is None or (not provider.is_local and project.get("default_model_mode") == "local"):
        name = summary_local_model()
        return model_registry.OllamaProvider(name) if name else None
    return provider


class CheckBody(BaseModel):
    """POST /api/meetings/check body: the call being recorded (its live draft) or a transcript."""
    recording_id: str | None = None
    transcript_id: str | None = None
    confirm: str | None = None      # "local" | "cloud": the answer to the privacy question
    unattended: bool = False        # the after-transcription follow-up: never asks, never uses the cloud


def check_job(body: CheckBody):
    """The meeting check (SSE): gathers this meeting, earlier meetings' notes and passages, memory and tracked
    actions, asks the check model, keeps only alerts with a source, and saves the new ones."""
    def run(emit):
        project = active_project()
        live = body.transcript_id is None
        rec_id = body.recording_id
        if live:
            draft = meetings.record_live(rec_id)
            rec_id = draft["id"]
            if draft.get("transcript_id"):  # the call is transcribed: check the final transcript instead
                live, body.transcript_id = False, draft["transcript_id"]
        key = check_key(None if body.transcript_id else rec_id, body.transcript_id)
        text = meetings.record_live_markdown(rec_id) if live else meetings.markdown(body.transcript_id)
        lines = meeting_check.parse_lines(text)
        if not lines:
            emit({"type": "notice", "text": "Nothing to check yet: no lines in this meeting so far."})
            emit({"type": "alerts", "alerts": meeting_alerts(None if body.transcript_id else rec_id, body.transcript_id),
                  "new": []})
            return
        provider = check_model_for(body, project)
        if provider is None:
            emit({"type": "error", "kind": "local_model",
                  "message": "No local model is installed for the meeting check. Download one in Settings → Local models."})
            return
        if not provider.is_local:
            if body.unattended:
                emit({"type": "notice", "text": "The meeting check model is a cloud model: run the check from the meeting page."})
                return
            if body.confirm == "local":
                name = summary_local_model()
                if not name:
                    emit({"type": "error", "kind": "local_model", "message": "No local model is installed."})
                    return
                provider = model_registry.OllamaProvider(name)
            elif body.confirm != "cloud" and prefs()["confirm_cloud_sensitive"]:
                emit({"type": "confirm", "kind": "sensitive", "local_model": summary_local_model(),
                      "reasons": ["This meeting's transcript, notes of earlier meetings and memory would be sent"],
                      "model": model_name(provider), "provider": provider_label(provider)})
                return
        store = memory_store.load_store()
        me_entry = memory_store.user_person(store)
        me = (me_entry or {}).get("name", "")
        role = (me_entry or {}).get("role", "") or next(
            (f["text"] for e in store["entries"] if e["kind"] == "profile" for f in e.get("facts", [])
             if re.search(r"\b(role|i am|i'm|title)\b", f["text"], re.I)), "")
        attendees = {l["speaker"].lower() for l in lines if l["speaker"] not in ("You", "Others")}
        emit({"type": "status", "text": "Reading earlier meetings and memory…"})
        earlier_list = earlier_meetings({body.transcript_id or ""}, attendees, project, datetime.now())
        earlier = meeting_check.earlier_items(earlier_list)
        passages = earlier_passages(earlier_list, lines)
        said = " ".join(l["text"] for l in lines)
        facts = memory_store.project_facts(project) + memory_store.facts_for_meeting(said, list(attendees))
        memory = meeting_check.number([{"text": f} for f in dict.fromkeys(facts)][:15], "M")
        tracked = meeting_check.number(
            [{"text": a["text"], "tracked_id": a["id"]} for a in load_my_actions()
             if not a.get("done") and a.get("project") in (project.get("id"), None)][:15], "T")
        paused = False
        try:
            if provider.is_local:
                if live:  # Whisper (live draft) and the check model never share the 16 GB
                    try:
                        meetings.record_live_pause(rec_id)
                        paused = True
                    except (meetings.TranscriberUnavailable, RuntimeError):
                        pass
                else:
                    wait_for_transcriber(emit)
                complete = lambda m: provider.complete(m, max_tokens=2000, temperature=0.1)  # noqa: E731
            else:
                complete = provider.complete
            emit({"type": "status", "text": f"Checking with {model_name(provider)}…"})
            seen = meeting_alerts(None if body.transcript_id else rec_id, body.transcript_id)
            result = meeting_check.check(lines, earlier, passages, memory, tracked, complete, me=me, role=role,
                                         project=project.get("name", "") if project.get("id") != projects.DEFAULT_PROJECT_ID else "",
                                         seen=seen)
        finally:
            if paused:
                try:
                    meetings.record_live_resume(rec_id)
                except (meetings.TranscriberUnavailable, RuntimeError):
                    pass
        for alert in result["alerts"]:
            alert["model"] = model_name(provider)
        save_alerts(key, load_alerts(key) + result["alerts"])
        emit({"type": "alerts", "alerts": meeting_alerts(None if body.transcript_id else rec_id, body.transcript_id),
              "new": [a["id"] for a in result["alerts"]], "summary": meeting_check.summary_line(result["alerts"]),
              "dropped": len(result["dropped"]), "repeats": result["repeats"], "key": key})
    return run


@app.post("/api/meetings/check")
def meeting_check_run(body: CheckBody) -> StreamingResponse:
    """POST /api/meetings/check: checks a meeting (live or finished) against earlier meetings and memory (SSE)."""
    return sse(check_job(body))


@app.get("/api/meetings/check/alerts")
def meeting_check_alerts(recording_id: str | None = None, transcript_id: str | None = None) -> dict:
    """The meeting's alerts so far (new, dismissed, tracked)."""
    return {"alerts": meeting_alerts(recording_id, transcript_id), "key": check_key(recording_id, transcript_id)}


@app.delete("/api/meetings/check/alerts")
def meeting_check_clear(recording_id: str | None = None, transcript_id: str | None = None) -> dict:
    """Forgets a meeting's alerts (the next check starts afresh)."""
    (checks_dir() / f"{check_key(recording_id, transcript_id)}.json").unlink(missing_ok=True)
    return {"ok": True}


@app.post("/api/meetings/check/alerts/{key}/{alert_id}/{action}")
def meeting_check_act(key: str, alert_id: str, action: str) -> dict:
    """Dismiss an alert, or Track it (added to My actions for the active project), or restore it."""
    if action not in ("dismiss", "track", "restore") or not re.fullmatch(r"(rec|t)-[0-9a-zA-Z_-]{1,40}", key):
        raise HTTPException(400, "Unknown action.")
    alerts = load_alerts(key)
    alert = next((a for a in alerts if a["id"] == alert_id), None)
    if alert is None:
        raise HTTPException(404, "Alert not found.")
    if action == "track" and alert["state"] != "tracked":
        items = load_my_actions()
        items.append({"id": uuid.uuid4().hex[:10], "text": alert["text"], "type": alert["type"],
                      "project": active_project().get("id"), "alert": {"key": key, "id": alert_id},
                      "sources": alert.get("sources", []), "line": alert.get("line"),
                      "created": datetime.now().isoformat(timespec="seconds"), "done": False})
        save_my_actions(items)
    alert["state"] = {"dismiss": "dismissed", "track": "tracked", "restore": "new"}[action]
    save_alerts(key, alerts)
    return alert


@app.get("/api/my-actions")
def my_actions() -> list[dict]:
    """My actions for the active project (open first)."""
    project = active_project().get("id")
    items = [a for a in load_my_actions() if a.get("project") in (project, None)]
    return sorted(items, key=lambda a: (a.get("done", False), a.get("created", "")))


class MyActionBody(BaseModel):
    """PATCH /api/my-actions/{id} body."""
    done: bool


@app.patch("/api/my-actions/{action_id}")
def update_my_action(action_id: str, body: MyActionBody) -> dict:
    """Marks a tracked action done (or not)."""
    items = load_my_actions()
    item = next((a for a in items if a["id"] == action_id), None)
    if item is None:
        raise HTTPException(404, "Action not found.")
    item["done"] = body.done
    save_my_actions(items)
    return item


@app.delete("/api/my-actions/{action_id}")
def delete_my_action(action_id: str) -> dict:
    """Removes a tracked action."""
    save_my_actions([a for a in load_my_actions() if a["id"] != action_id])
    return {"ok": True}


FOLLOWUPS = FollowUps()


@app.get("/api/models/speaker")
def list_speaker_models() -> dict:
    """Local models that can read the transcript (Settings → Meetings, New meeting, Speakers panel)."""
    data = model_registry.speaker_models()
    data["pulls"] = model_registry.PULLS.snapshot()
    data["selected"] = prefs()["speaker_model"]
    data["effective"] = model_registry.pick_speaker_model(data["selected"] or None) or ""
    data["frames"] = model_registry.pick_vision_model(prefs()["vision_model"] or None) or ""
    return data


@app.post("/api/meetings/transcripts/{tid}/chat")
def chat_about(tid: str, body: SessionCreate) -> dict:
    """New chat with this transcript attached."""
    item = _transcriber(transcript_attachment, tid)
    record = sessions.create(sessions_dir(), body.model_id)
    record["attachments"] = [item]
    record["title"], record["custom_title"] = f"About {item['name'][len('Transcript: '):]}"[:120], True
    sessions.save(sessions_dir(), record, touch=False)
    return _session_out(record)


# ---------------------------------------------------------------------------
# Import from claude.ai (memory, people, conversations)
# ---------------------------------------------------------------------------

@app.post("/api/imports/claude/preview")
async def import_preview(request: Request) -> dict:
    """Multipart: files (ZIP / JSON / text, repeatable) and/or text (pasted memory). Nothing is saved yet."""
    if not request.headers.get("content-type", "").startswith(("multipart/", "application/x-www-form-urlencoded")):
        raise HTTPException(400, "Upload the export as a file, or paste memory text.")
    form = await request.form()
    files = []
    for key, value in form.multi_items():
        if key == "files" and hasattr(value, "read"):
            files.append((value.filename or "upload", await value.read()))
    text = str(form.get("text") or "")
    if not files and not text.strip():
        raise HTTPException(400, "Choose an export file or paste your memory text.")
    return imports.preview(files, text)


class ImportCommit(BaseModel):
    """POST /api/imports/claude/commit body: the preview and which items to import (None = all)."""
    preview_id: str
    memory: list[str] | None = None          # keys; None = all
    people: list[str] | None = None
    conversations: list[str] | None = None   # claude.ai uuids


@app.post("/api/imports/claude/commit")
def import_commit(body: ImportCommit) -> dict:
    """POST /api/imports/claude/commit: imports the chosen items, then refreshes search."""
    try:
        result = imports.commit(body.preview_id, body.memory, body.people, body.conversations)
    except KeyError as exc:
        raise HTTPException(410, str(exc.args[0])) from exc
    start_indexing()
    return {**result, "status": imports.status()}


@app.get("/api/imports/claude/status")
def import_status() -> dict:
    """GET /api/imports/claude/status: what has been imported so far."""
    return imports.status()


@app.delete("/api/imports/claude")
def import_delete_all() -> dict:
    """DELETE /api/imports/claude: removes everything that came from claude.ai."""
    result = imports.delete_all()
    start_indexing(prune=True)
    return result


@app.get("/api/memory/imported")
def imported_memory() -> dict:
    """GET /api/memory/imported: imported facts and likely duplicates (older Memory screen)."""
    entries = imports.load_memory()
    return {"entries": entries, "duplicates": imports.memory_duplicates(entries)}


class MemoryEdit(BaseModel):
    """Body with a fact's new text."""
    text: str


@app.patch("/api/memory/imported/{entry_id}")
def edit_imported_memory(entry_id: str, body: MemoryEdit) -> dict:
    """PATCH /api/memory/imported/{id}: changes an imported fact's text."""
    if not body.text.strip():
        raise HTTPException(400, "The memory can't be empty.")
    entry = imports.update_memory_entry(entry_id, body.text)
    if entry is None:
        raise HTTPException(404, "Memory entry not found.")
    return entry


@app.delete("/api/memory/imported/{entry_id}")
def delete_imported_memory(entry_id: str) -> dict:
    """DELETE /api/memory/imported/{id}: deletes an imported fact."""
    return {"deleted": imports.delete_memory_entries([entry_id])}


class MergeBody(BaseModel):
    """Body for merging facts: their ids and the merged wording."""
    ids: list[str]
    text: str = ""


@app.post("/api/memory/imported/merge")
def merge_imported_memory(body: MergeBody) -> dict:
    """POST /api/memory/imported/merge: merges imported facts into one."""
    entry = imports.merge_memory_entries(body.ids, body.text)
    if entry is None:
        raise HTTPException(400, "Pick at least two entries to merge.")
    return entry


@app.get("/api/people/duplicates")
def people_duplicates() -> list[list[str]]:
    """GET /api/people/duplicates: groups of people who are probably the same person."""
    return people_store.duplicate_groups(people_store.load())


class PeopleMerge(BaseModel):
    """Body for merging people: the one to keep and the ones merged into it."""
    keep: str
    merge: list[str]


@app.post("/api/people/merge")
def merge_people(body: PeopleMerge) -> dict:
    """POST /api/people/merge: merges duplicate people (names become aliases)."""
    person = people_store.merge(body.keep, body.merge)
    if person is None:
        raise HTTPException(404, "Person not found.")
    return people_store.public(person)


# ---------------------------------------------------------------------------
# Search: chats (native + imported), transcripts, notes, memory, connectors, web
# ---------------------------------------------------------------------------

VECTORS = search_index.VectorStore(DATA_DIR / "index" / "embeddings.json")
INDEXER = search_index.Indexer(VECTORS)
_TRANSCRIPT_CHUNKS: dict[str, tuple[str, list]] = {}
INTERNAL_SOURCES = ("chats", "transcripts", "memory")
WEB_URL = meeting_notes.WEB_URL


def chat_chunks() -> list:
    """Searchable pieces of every chat, including imported claude.ai chats."""
    out = []
    for directory in (sessions_dir(), imports.conversations_dir()):
        if not directory.exists():
            continue
        for path in directory.glob("*.json"):
            record = sessions.load(directory, path.stem)
            if record and record.get("messages"):
                out += search_index.chat_chunks(record)
    return out


def transcript_chunks(notes: list[str] | None = None) -> list:
    """Searchable pieces of every transcript and its notes; adds a note to `notes` when the transcriber isn't
    running.
    """
    try:
        metas = meetings.transcripts()
    except (meetings.TranscriberUnavailable, RuntimeError, requests.RequestException):
        if notes is not None:
            notes.append("The transcriber isn't running, so transcripts weren't searched.")
        return []
    out = []
    for meta in metas:
        tid = meta["id"]
        label_file = LABELS_DIR / f"{tid}.json"
        key = json.dumps([meta.get("created_at"), meta.get("speaker_names"), meta.get("corrected"),
                          meta.get("updated_at"), label_file.stat().st_mtime if label_file.exists() else 0])
        cached = _TRANSCRIPT_CHUNKS.get(tid)
        if cached is None or cached[0] != key:
            try:
                transcript = meetings.transcript(tid)
            except (meetings.TranscriberUnavailable, RuntimeError, requests.RequestException):
                continue
            names = display_names(transcript, labels_for(tid))
            cached = (key, search_index.transcript_chunks(meta, transcript.get("segments", []), names))
            _TRANSCRIPT_CHUNKS[tid] = cached
        out += cached[1]
        summary = summary_for(tid)
        if summary and summary.get("markdown"):
            out += search_index.markdown_chunks(tid, f"Notes: {meta.get('title', '')}", summary["markdown"],
                                                summary.get("created_at", ""), "notes", f"/meetings/{tid}?tab=summary")
    return out


def memory_chunks() -> list:
    """Searchable pieces of memory: every fact and every person."""
    facts = []
    for entry in memory_store.load_store()["entries"]:
        for fact in entry["facts"]:
            facts.append({"id": fact["id"], "text": fact["text"], "source": fact.get("source", ""),
                          "section": memory_store.fact_line(entry, fact).rsplit(": ", 1)[0],
                          "updated_at": fact.get("date", "")})
    return search_index.memory_chunks(facts, people_store.load())


def gather_chunks(sources: list[str], notes: list[str] | None = None) -> list:
    """All searchable pieces from the chosen sources."""
    chunks = []
    if "chats" in sources:
        chunks += chat_chunks()
    if "transcripts" in sources:
        chunks += transcript_chunks(notes)
    if "memory" in sources:
        chunks += memory_chunks()
    return chunks


def embed_model() -> str:
    """The embedding model used for search by meaning."""
    return prefs()["search"]["embed_model"] or long_context.EMBED_MODEL


def start_indexing(prune: bool = False) -> dict:
    """Starts embedding new pieces in the background (and with prune=True removes vectors of deleted ones).
    Returns the indexing state.
    """
    model = embed_model()
    chunks = gather_chunks(list(INTERNAL_SOURCES))
    if prune:
        VECTORS.prune(model, {c.hash for c in chunks})
        VECTORS.save()
    return INDEXER.start(chunks, model, search_index.ollama_embedder(model))


def private_terms() -> set[str]:
    """Names and terms that must not reach the web provider unless the user typed them."""
    terms: set[str] = set()
    for person in people_store.load():
        terms.add(person["name"])
        terms.add(person["name"].split()[0])
        terms.update(person.get("aliases") or [])
    terms.update(active_project().get("confidential_terms") or [])
    try:
        for meta in meetings.transcripts():
            terms.update((meta.get("speaker_names") or {}).values())
    except (meetings.TranscriberUnavailable, RuntimeError, requests.RequestException):
        pass
    return {t for t in terms if len(t) >= 3 and not re.fullmatch(r"(?i)speaker[ _]\d+|unknown", t)}


def blocked_terms(query: str, user_text: str) -> list[str]:
    """Private names (people, projects from memory) that are in a web query but weren't typed by the user in this
    message. Such a query is not sent to the web.
    """
    return sorted(t for t in private_terms()
                  if re.search(rf"\b{re.escape(t)}\b", query, re.I) and not re.search(rf"\b{re.escape(t)}\b", user_text or "", re.I))


def web_search(query: str, limit: int) -> tuple[list[dict], dict]:
    """Runs a web search with the chosen provider; returns (results, status for the browser). Failures become a
    status message instead of an error.
    """
    settings = prefs()["search"]
    provider = settings["provider"]
    status = {"status": "ok", "provider": provider, "label": web_providers.PROVIDERS.get(provider, {}).get("label", provider),
              "message": ""}
    try:
        results = web_providers.search(provider, query, limit)
    except web_providers.WebSearchError as exc:
        return [], {**status, "status": exc.kind, "message": str(exc) + " Showing results from your own data only."}
    return [{"source": "web", "kind": "web", "id": r["url"], "title": r["title"] or r["url"], "snippet": r["snippet"][:300],
             "date": r["date"], "link": r["url"], "score": 0, "method": "web", "meta": {"provider": provider}}
            for r in results], status


def run_search(query: str, sources: list[str], date_from: str = "", date_to: str = "", limit: int | None = None,
               web: bool | None = None, local_only: bool = False) -> dict:
    """The search behind ⌘K and the assistant's search tool: keyword (BM25) and meaning (embeddings) scores
    combined (RRF), plus connectors and the web when allowed.
    """
    settings = prefs()["search"]
    limit = max(1, min(int(limit or settings["max_results"]), 20))
    enabled = [s for s in sources if s in INTERNAL_SOURCES and settings["sources"].get(s, True)]
    notes: list[str] = []
    everything = gather_chunks(list(INTERNAL_SOURCES), notes if "transcripts" in enabled else None)
    chunks = [c for c in everything if c.source in enabled and search_index.in_range(c.date, date_from, date_to)]
    model = embed_model()
    vectors = VECTORS.get(model)
    query_vector, method = None, "keywords"
    if vectors:
        try:
            query_vector = long_context.embed([f"search_query: {query}"], model=model)[0]
            method = "keywords + meaning"
        except (requests.RequestException, KeyError, IndexError, ValueError):
            notes.append("Semantic search is unavailable (Ollama or the embedding model isn't running): keywords only.")
    index_state = INDEXER.start(everything, model, search_index.ollama_embedder(model)) if enabled else INDEXER.snapshot()
    hits = search_index.search(query, chunks, vectors, query_vector, limit=limit * len(enabled or [1]) * 2)
    groups: dict[str, list[dict]] = {"chats": [], "transcripts": [], "memory": [], "connectors": [], "web": []}
    for hit in hits:
        item = search_index.result_json(hit)
        if len(groups[item["source"]]) < limit:
            groups[item["source"]].append(item)
    if "connectors" in sources and settings["sources"].get("connectors"):
        for connector in connectors.active_connectors():
            if local_only and connectors.is_remote(connector):
                continue
            text = connectors.execute_connector(connector, query)
            if text and not text.startswith("Error running connector"):
                groups["connectors"].append({"source": "connectors", "kind": "connector", "id": connector["id"],
                                             "title": connector["name"], "snippet": text[:300], "date": "",
                                             "link": connector.get("url", ""), "score": 0, "method": "connector",
                                             "meta": {}})
            elif text:
                notes.append(f"{connector['name']}: {text[:160]}")
    want_web = ("web" in sources) if web is None else bool(web)
    web_status = {"status": "disabled", "provider": settings["provider"], "message": ""}
    if want_web and local_only:
        web_status = {"status": "local", "provider": settings["provider"],
                      "message": "Web search is off for chats on a local model (nothing leaves this Mac)."}
    elif want_web and not settings["web_enabled"]:
        web_status["message"] = "Web search is off, so only your own data was searched. Turn it on in Settings → Search."
    elif want_web:
        groups["web"], web_status = web_search(query, limit)
    return {"query": query, "groups": groups, "web": web_status, "method": method, "notes": notes,
            "index": index_state, "total": sum(len(v) for v in groups.values())}


class SearchBody(BaseModel):
    """POST /api/search body: the query, sources, date range, limit and whether to use the web."""
    q: str
    sources: list[str] = ["chats", "transcripts", "memory", "connectors", "web"]
    date_from: str = ""
    date_to: str = ""
    limit: int | None = None
    web: bool | None = None


@app.post("/api/search")
def search_endpoint(body: SearchBody) -> dict:
    """POST /api/search: global search (⌘K)."""
    if not body.q.strip():
        raise HTTPException(400, "Type something to search for.")
    return run_search(body.q.strip(), body.sources, body.date_from, body.date_to, body.limit, body.web)


def search_settings_out() -> dict:
    """Search settings for the browser: providers, which have a key (masked), sources, and the indexing state."""
    settings = prefs()["search"]
    providers = []
    for pid, info in web_providers.PROVIDERS.items():
        key = secrets_store.get(web_providers.key_name(pid)) if info["needs_key"] else ""
        providers.append({"id": pid, **info, "has_key": bool(key), "key_hint": secrets_store.masked(key)})
    state = model_registry.ollama_state()
    installed = any(m.split(":")[0] == embed_model().split(":")[0] for m in state.get("installed", []))
    return {"settings": settings, "providers": providers, "key_storage": secrets_store.backend(),
            "embedding": {"model": embed_model(), "installed": installed, "ollama_running": state["running"],
                          "pull": model_registry.PULLS.snapshot().get(embed_model())},
            "index": INDEXER.snapshot()}


@app.get("/api/search/settings")
def get_search_settings() -> dict:
    """GET /api/search/settings."""
    return search_settings_out()


@app.put("/api/search/settings")
def put_search_settings(body: dict) -> dict:
    """PUT /api/search/settings: changes provider, result count or sources."""
    if "provider" in body and body["provider"] not in web_providers.PROVIDERS:
        raise HTTPException(400, "Unknown search provider.")
    if "max_results" in body:
        body["max_results"] = max(1, min(int(body["max_results"]), 20))
    save_prefs({"search": body})
    return search_settings_out()


class KeyBody(BaseModel):
    """Body with an API key."""
    key: str


@app.put("/api/search/keys/{provider}")
def put_search_key(provider: str, body: KeyBody) -> dict:
    """PUT /api/search/keys/{provider}: stores a search provider's API key in the Keychain."""
    if provider not in web_providers.PROVIDERS or not web_providers.PROVIDERS[provider]["needs_key"]:
        raise HTTPException(400, "This provider doesn't use a key.")
    if not body.key.strip():
        raise HTTPException(400, "Paste the API key.")
    try:
        secrets_store.set(web_providers.key_name(provider), body.key)
    except (OSError, subprocess.CalledProcessError) as exc:
        raise HTTPException(500, f"Couldn't store the key securely: {exc}") from exc
    return search_settings_out()


@app.delete("/api/search/keys/{provider}")
def delete_search_key(provider: str) -> dict:
    """DELETE /api/search/keys/{provider}: deletes a search provider's key."""
    secrets_store.delete(web_providers.key_name(provider))
    return search_settings_out()


@app.post("/api/search/test")
def test_search_provider() -> dict:
    """Checks the key with a harmless query (no personal data)."""
    provider = prefs()["search"]["provider"]
    try:
        results = web_providers.search(provider, "Quorum connection test", 1)
    except web_providers.WebSearchError as exc:
        return {"ok": False, "status": exc.kind, "message": str(exc)}
    return {"ok": True, "status": "ok", "message": f"Connected · {len(results)} result{'s' if len(results) != 1 else ''}"}


@app.post("/api/search/reindex")
def reindex() -> dict:
    """POST /api/search/reindex: rebuilds the search index."""
    return start_indexing(prune=True)


@app.get("/api/search/status")
def search_status() -> dict:
    """GET /api/search/status: progress of the search index."""
    return INDEXER.snapshot()


def _cite(results: list[dict], start: int = 1) -> str:
    """Search results as numbered sources with links, for the model to cite as [1], [2]…."""
    lines = []
    for i, r in enumerate(results, start=start):
        link = r["link"] if r["link"].startswith("http") else f"{WEB_URL}{r['link']}"
        where = {"chats": "Chat", "transcripts": "Meeting", "memory": "Memory", "connectors": "Connector",
                 "web": "Web"}[r["source"]]
        if r["kind"] == "imported_chat":
            where = "Imported claude.ai chat"
        stamp = f" at {r['meta']['timestamp']}" if r.get("meta", {}).get("timestamp") else ""
        lines.append(f"[{i}] {where} · {r['title']}{stamp}{' · ' + r['date'][:10] if r['date'] else ''}\n"
                     f"    {r['snippet']}\n    link: {link}")
    return "\n".join(lines)


def _search_tool(query: str, context: dict) -> str:
    """The assistant's 'search' tool: searches the user's own data, and the web only when allowed, never for a
    local model, and never with private names the user didn't type.
    """
    turn = context.get("_turn") or {}
    local = bool(turn.get("local"))
    settings = prefs()["search"]
    use_web = settings["web_enabled"] and not local
    blocked = blocked_terms(query, turn.get("user", "")) if use_web else []
    result = run_search(query, ["chats", "transcripts", "memory", "connectors"] + (["web"] if use_web and not blocked else []),
                        web=use_web and not blocked, local_only=local)
    internal = sorted((r for key in ("transcripts", "chats", "memory", "connectors") for r in result["groups"][key]),
                      key=lambda r: -r["score"])  # most relevant first, whatever the source
    ordered = internal + result["groups"]["web"]
    text = "Search results (cite them inline as [n](link)):\n" + (_cite(ordered) if ordered else "No matches.")
    if blocked:
        text += ("\n\nWeb search was skipped: the query mentions private names or terms (" + ", ".join(blocked)
                 + "). Say so; the user can search the web explicitly with their own words.")
    elif use_web and result["web"]["status"] not in ("ok", "disabled"):
        text += f"\n\nWeb search unavailable: {result['web']['message']}"
    return text


def _web_tool(query: str, context: dict) -> str:
    """The assistant's 'web_search' tool, with the same privacy rules as the search tool."""
    turn = context.get("_turn") or {}
    if turn.get("local"):
        return "Web search is off for chats on a local model."
    blocked = blocked_terms(query, turn.get("user", ""))
    if blocked:
        return ("Web search skipped: the query mentions private names or terms (" + ", ".join(blocked)
                + "). Answer without it, and say so.")
    results, status = web_search(query, prefs()["search"]["max_results"])
    if status["status"] != "ok":
        return status["message"]
    return "Web search results (cite them inline as [n](link)):\n" + _cite(results)


tools.register_tool(
    "search",
    "Search the user's own chats (including chats imported from claude.ai), meeting transcripts, meeting notes, "
    "memory and connectors — plus the web when enabled. Input: a short search query. Then answer using the "
    "results with inline citations like [1](link).",
    _search_tool,
)
tools.register_tool("web_search", "Search the public web for up-to-date information. Input: a search query.", _web_tool)


def related_conversation_people(transcript: dict, known: list[dict]) -> dict[str, list[str]]:
    """People (from memory) who appear in past chats about the same topics as this meeting."""
    text = " ".join(seg.get("text", "") for seg in transcript.get("segments", []))
    if not text.strip() or not known:
        return {}
    projects_mentioned = list(dict.fromkeys(p for person in known for p in person.get("projects") or []
                                            if p and re.search(rf"\b{re.escape(p)}\b", text, re.I)))
    names = {w.lower() for person in known for w in person["name"].split()}
    counts = Counter(w for w in search_index.tokens(text) if len(w) > 3 and w not in names)
    query = " ".join(projects_mentioned + [w for w, _ in counts.most_common(8)])
    chunks = chat_chunks()
    hits = search_index.search(query, chunks, limit=6)
    docs = {h["chunk"].doc_id: h["chunk"].title for h in hits if h["score"] > 0}
    texts: dict[str, str] = {}
    for c in chunks:
        if c.doc_id in docs:
            texts[c.doc_id] = texts.get(c.doc_id, "") + " " + c.text
    past: dict[str, list[str]] = {}
    for person in known:
        first = person["name"].split()[0]
        titles = [docs[d] for d, t in texts.items() if re.search(rf"\b{re.escape(first)}\b", t)]
        if titles:
            past[person["name"]] = titles
    return past


# ---------------------------------------------------------------------------
# Meeting notes (the Professional Meeting Notes skill)
# ---------------------------------------------------------------------------

def notes_chat_skill() -> dict:
    """The chat's "Meeting minutes" skill follows the meeting-notes writing rules."""
    return {**agent.MEETING_MINUTES_SKILL, "content": (
        "Write professional meeting notes in Markdown with: a header (title, date, attendees with roles), "
        "## Summary (3-5 bullets), ## Agenda & discussion (by topic, with speaker and [hh:mm:ss]), "
        "## Decisions (table: Decision | Made by | Rationale | Timestamp), "
        "## Action items (table: # | Action | Owner | Due date | Priority | Timestamp), "
        "## Risks, issues & blockers, ## Open questions, ## Next steps.\n\nRules:\n" + meeting_notes.writing_rules())}


@app.get("/api/meetings/types")
def meeting_types() -> list[dict]:
    """GET /api/meetings/types: the meeting types notes can be written for."""
    return [{"id": t, "label": info["label"]} for t, info in meeting_notes.meeting_types().items()]


@app.get("/api/meetings/transcripts/{tid}/meeting-type")
def detect_meeting_type(tid: str) -> dict:
    """GET /api/meetings/transcripts/{id}/meeting-type: the type guessed from the transcript's words (standup,
    retrospective, interview…).
    """
    transcript = _transcriber(meetings.transcript, tid)
    labels = labels_for(tid)
    display = meeting_notes.speaker_display(transcript, display_names(transcript, labels), labels)
    text = meeting_notes.lines_text(meeting_notes.transcript_lines(transcript, display))
    return meeting_notes.detect_type(text, transcript.get("title", ""))


class RenderBody(BaseModel):
    """POST …/summary/render body: show the saved notes as another type, detail level or set of sections (no
    model call).
    """
    meeting_type: str | None = None
    detail: str | None = None
    sections: dict[str, bool] | None = None


@app.post("/api/meetings/transcripts/{tid}/summary/render")
def rerender_summary(tid: str, body: RenderBody) -> dict:
    """Re-render stored notes with another type/detail level/sections (no model call; discards edits)."""
    record = summary_for(tid)
    if not record or not record.get("notes"):
        raise HTTPException(404, "No structured notes to re-render. Generate the notes first.")
    meeting_type = body.meeting_type if body.meeting_type in meeting_notes.meeting_types() else record["meeting_type"]
    detail = body.detail if body.detail in meeting_notes.DETAIL_LEVELS else record.get("detail", "standard")
    if body.sections is not None:
        sections = body.sections
    elif body.detail and body.detail != record.get("detail"):
        sections = meeting_notes.default_sections(detail)  # a new detail level starts from its defaults
    else:
        sections = record.get("sections")
    record.update(meeting_type=meeting_type, detail=detail, sections=sections, edited=False,
                  markdown=meeting_notes.render(record["notes"], tid, meeting_type, detail, sections, record.get("follow_up")))
    _write_json(SUMMARIES_DIR / f"{tid}.json", record)
    return record


@app.delete("/api/meetings/transcripts/{tid}/summary")
def delete_summary(tid: str) -> dict:
    """DELETE /api/meetings/transcripts/{id}/summary: deletes the saved notes."""
    (SUMMARIES_DIR / f"{tid}.json").unlink(missing_ok=True)
    _CACHES.pop(f"summary:{tid}", None)
    return {"ok": True}


@app.delete("/api/meetings/transcripts/{tid}/speakers")
def clear_speaker_labels(tid: str) -> dict:
    """Forget the guessed and confirmed labels of this transcript (people memory is kept)."""
    (LABELS_DIR / f"{tid}.json").unlink(missing_ok=True)
    review_file(tid).unlink(missing_ok=True)
    try:
        meetings.set_speaker_names(tid, {})
    except (meetings.TranscriberUnavailable, RuntimeError):
        pass
    return {"ok": True}


class SummaryEdit(BaseModel):
    """PUT …/summary body: the notes as edited by the user."""
    markdown: str


@app.put("/api/meetings/transcripts/{tid}/summary")
def edit_summary(tid: str, body: SummaryEdit) -> dict:
    """PUT /api/meetings/transcripts/{id}/summary: saves the user's edits to the notes."""
    record = summary_for(tid)
    if record is None:
        raise HTTPException(404, "No summary yet.")
    record.update(markdown=body.markdown, edited=True, edited_at=datetime.now().isoformat(timespec="seconds"))
    _write_json(SUMMARIES_DIR / f"{tid}.json", record)
    return record


EXPORT_TYPES = {"md": "text/markdown; charset=utf-8", "html": "text/html; charset=utf-8",
                "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                "pdf": "application/pdf", "csv": "text/csv; charset=utf-8", "txt": "text/plain; charset=utf-8"}


@app.get("/api/meetings/transcripts/{tid}/summary/export")
def export_summary(tid: str, format: str = "md") -> Response:  # noqa: A002 (query parameter name)
    """GET …/summary/export?format=…: the notes as Markdown, Word, PDF, email HTML or action-items CSV, as a
    download.
    """
    record = summary_for(tid)
    if record is None:
        raise HTTPException(404, "No summary yet.")
    if format not in EXPORT_TYPES:
        raise HTTPException(400, "Unknown format.")
    md = record["markdown"]
    title = next((l[2:].strip() for l in md.splitlines() if l.startswith("# ")), "Meeting notes")
    body = {"md": lambda: md, "html": lambda: notes_export.email_html(md, title), "docx": lambda: notes_export.to_docx(md),
            "pdf": lambda: notes_export.to_pdf(md, title), "csv": lambda: notes_export.actions_csv(md),
            "txt": lambda: notes_export.plain_text(md)}[format]()
    stem = re.sub(r"[^\w\- ]+", "", title).strip()[:80] or "meeting-notes"
    name = f"{stem} - action items.csv" if format == "csv" else f"{stem}.{format}"
    return Response(body, media_type=EXPORT_TYPES[format],
                    headers={"Content-Disposition": f"attachment; filename*=UTF-8''{requests.utils.quote(name)}"})


@app.get("/api/meetings/transcripts/{tid}/summary/clipboard")
def summary_clipboard(tid: str) -> dict:
    """GET …/summary/clipboard: the notes as HTML and plain text for copy & paste."""
    record = summary_for(tid)
    if record is None:
        raise HTTPException(404, "No summary yet.")
    md = record["markdown"]
    return {"html": notes_export.email_html(md), "text": notes_export.plain_text(md), "markdown": md}


@app.get("/api/meetings/transcripts/{tid}/summary/email")
def summary_email(tid: str) -> dict:
    """GET …/summary/email: a draft follow-up email from the notes."""
    record = summary_for(tid)
    if record is None:
        raise HTTPException(404, "No summary yet.")
    return notes_export.follow_up_email(record["markdown"])


# ---------------------------------------------------------------------------
# Built-in skills (Settings → Skills)
# ---------------------------------------------------------------------------

def notes_skill_out() -> dict:
    """The meeting-notes skill for Settings → Skills: on/off, its rules and templates (and which ones the user
    edited).
    """
    info = meeting_notes.skill()
    types = meeting_notes.meeting_types()
    by_template = {v["template"]: (k, v["label"]) for k, v in types.items()}
    templates = []
    for name in meeting_notes.template_names():
        _, customised = meeting_notes.read_template(name)
        kind, label = by_template.get(name, ("partial", name.split("/")[-1].replace("_", " ").title()))
        templates.append({"name": name, "type": kind, "label": label, "customized": customised})
    return {"id": "meeting-notes", "name": info["name"], "description": info["description"],
            "enabled": prefs()["meeting_notes_skill"], "default_for": "Meeting summaries",
            "instructions": info["body"], "schema": meeting_notes.schema(), "templates": templates,
            "folder": "skills/meeting-notes"}


@app.get("/api/skills/builtin")
def builtin_skills() -> list[dict]:
    """GET /api/skills/builtin: the built-in skills."""
    return [notes_skill_out()]


@app.patch("/api/skills/meeting-notes")
def toggle_notes_skill(body: dict) -> dict:
    """PATCH /api/skills/meeting-notes: switches the meeting-notes skill on or off."""
    if "enabled" in body:
        save_prefs({"meeting_notes_skill": bool(body["enabled"])})
    return notes_skill_out()


@app.get("/api/skills/meeting-notes/templates/{name:path}")
def get_notes_template(name: str) -> dict:
    """GET …/templates/{name}: a template's text and whether it was customised."""
    try:
        text, customised = meeting_notes.read_template(name)
    except KeyError as exc:
        raise HTTPException(404, "Template not found.") from exc
    return {"name": name, "text": text, "customized": customised}


@app.put("/api/skills/meeting-notes/templates/{name:path}")
def put_notes_template(name: str, body: dict) -> dict:
    """PUT …/templates/{name}: saves an edited template after checking it renders."""
    text = str(body.get("text") or "")
    if not text.strip():
        raise HTTPException(400, "The template can't be empty.")
    try:
        meeting_notes.render_template(text, meeting_notes.render_context({"meeting": {"title": "Check"}}, "x", "general"),
                                      lambda n: meeting_notes.read_template(f"partials/{n}")[0])
        meeting_notes.save_template(name, text)
    except KeyError as exc:
        raise HTTPException(404, "Template not found.") from exc
    except Exception as exc:  # a template that can't render must not be saved
        raise HTTPException(400, f"The template doesn't render: {exc}") from exc
    return get_notes_template(name)


@app.delete("/api/skills/meeting-notes/templates/{name:path}")
def reset_notes_template(name: str) -> dict:
    """DELETE …/templates/{name}: goes back to the built-in template."""
    meeting_notes.reset_template(name)
    return get_notes_template(name)


load_speeds()
