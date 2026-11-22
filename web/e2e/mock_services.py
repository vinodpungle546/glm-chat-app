#!/usr/bin/env python3
"""Fake external services for the Playwright end-to-end tests.

  admin mode   (one process, port 18999): admin API + fake GAIA cloud + fake transcriber;
               it also runs the fake Ollama as a child process it can stop and start,
               so "Ollama not running" is a real refused connection.
  ollama mode  (child, port 18434): Ollama's /api/version, /api/tags, /api/show,
               /api/chat (NDJSON stream), /api/pull (progress stream), /api/delete.

State shared between the two lives in a JSON file (--state).
"""

from __future__ import annotations

import argparse
import atexit
import json
import re
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, StreamingResponse

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from transcriber import correct, text_import  # noqa: E402  (pure Python: conversation text, corrections)
from tests import fake_cloud, fake_mcp  # noqa: E402  (fake Claude/OpenAI APIs, fake MCP registry and servers)

CLOUD_PORT = 18997   # fake Anthropic at http://127.0.0.1:18997, fake OpenAI at …/openai/v1
MCP_PORT = 18996     # fake MCP Registry + remote MCP servers + OAuth

DEFAULT_OLLAMA = {"version": "0.40.16", "installed": ["gemma4:e4b", "qwen3:8b", "nomic-embed-text"], "chat_delay": 0.02,
                  "pull_steps": 40, "pull_step_seconds": 0.12}
SIZES = {"gemma4:e4b": 9_500_000_000, "qwen3:8b": 5_200_000_000, "qwen3:4b": 2_500_000_000}


FIXTURES = ROOT / "tests" / "fixtures" / "meetings"


def fixture_meetings() -> dict[str, tuple[dict, dict]]:
    """Synthetic meetings, one per type: {title: (transcript, notes the model returns)}."""
    out = {}
    for path in sorted(FIXTURES.glob("*.notes.json")):
        transcript = json.loads(path.with_name(path.name.replace(".notes", "")).read_text())
        out[transcript["title"]] = (transcript, json.loads(path.read_text()))
    return out


FIXTURE_MEETINGS = fixture_meetings()


def embed_vector(text: str) -> list[float]:
    """Deterministic bag-of-words vector (stands in for nomic-embed-text)."""
    import hashlib
    vec = [0.0] * 64
    for word in re.findall(r"[a-z]{3,}", text.lower()):
        vec[int(hashlib.md5(word.encode()).hexdigest(), 16) % 64] += 1.0
    return vec


def notes_reply(user: str, model: str) -> str:
    """What a model returns for the meeting-notes prompt: the fixture's notes, or notes read off the lines."""
    title = (re.search(r"^Meeting: (.+)$", user, re.M) or [None, ""])[1]
    part = re.search(r"This is part (\d+) of (\d+)", user)
    if title in FIXTURE_MEETINGS and not part:
        notes = json.loads(json.dumps(FIXTURE_MEETINGS[title][1]))
        notes["tldr"] = notes["tldr"][:4] + [f"Notes written by {model}"]
        return json.dumps(notes)
    lines = re.findall(r"^\[(\d\d:\d\d:\d\d)\] ([^:\n]+): (.+)$", user, re.M)
    actions, decisions = [], []
    for ts, speaker, text in lines:
        please = re.search(r"(?:(\w+), )?please ([^.]+)", text, re.I)
        will = re.search(r"I'll ([^.]+)", text)
        due = "Friday" if "Friday" in text else ""
        if please:
            act = please.group(2).strip()
            actions.append({"action": act[0].upper() + act[1:], "owner": please.group(1) or "", "due_date": due,
                            "priority": "High", "timestamp": ts})
        elif will:
            act = will.group(1).strip()
            actions.append({"action": act[0].upper() + act[1:], "owner": speaker.strip(), "due_date": "",
                            "priority": "Medium", "timestamp": ts})
        if re.search(r"\b(agreed|decided|decision)\b", text, re.I):
            decisions.append({"decision": text.strip()[:120], "made_by": speaker.strip(), "rationale": "", "timestamp": ts})
    if part:
        first = lines[0] if lines else ("00:00:00", "", "")
        words = ["alpha", "bravo", "charlie", "delta", "echo", "foxtrot", "golf", "hotel", "india", "juliet", "kilo", "lima"]
        tldr = [f"Section {words[(int(part.group(1)) - 1) % 12]} reviewed the roadmap and budget"]
        actions = actions or [{"action": "Review the roadmap, budget and hiring plan", "owner": "", "due_date": "",
                               "priority": "", "timestamp": first[0]}]
    else:
        tldr = [f"The team reviewed {title}, written by {model}"]
    return json.dumps({"meeting": {"title": title, "project": ""}, "tldr": tldr,
                       "topics": [{"title": "Updates", "points": [{"text": t[:100], "speaker": s, "timestamp": ts} for ts, s, t in lines[:3]]}],
                       "decisions": decisions, "action_items": actions, "risks": [], "open_questions": [], "next_steps": [],
                       "next_meeting": {"date": "", "time": "", "agenda": []}, "quotes": []})


def connector_turn(system: str, user: str) -> str | None:
    """Connectors: "Use <connector> to <tool> <text>" → a connector call; its output → an answer."""
    if "## Connectors" not in system:
        return None
    if user.startswith("[Tool output — `") and "connector" in user.split("]", 1)[0]:
        result = user.split("]\n", 1)[1].split("\n\nUse this to answer", 1)[0]
        return f"The connector answered: {result}"
    match = re.match(r"^use (.+?) to (\w+) (.+)$", user.strip(), re.I)
    if match:
        return json.dumps({"connector": match.group(1), "tool": match.group(2), "input": {"text": match.group(3)}})
    return None


def video_transcript() -> dict:
    """A screen-recorded call: names were read off the video tiles (SPEAKER_01 was screen sharing)."""
    segs = [("SPEAKER_00", "Morning all, let's start the Atlas release sync."),
            ("SPEAKER_01", "Hi, Daniel here. The Atlas payments regression is green."),
            ("SPEAKER_00", "Thanks. Daniel, please rerun the refund tests by Friday."),
            ("SPEAKER_02", "I'll deploy the refund fix to staging today."),
            ("SPEAKER_01", "Sounds good, I'll watch the dashboards.")]
    tile = lambda name, times: {"name": name, "count": len(times), "times": times, "truncated": False,  # noqa: E731
                                "ocr": len(times), "speaker_view": 0, "vision": 0}
    return {"id": "t-video", "title": "Atlas release sync", "source_name": "Screen Recording 2026-11-19 at 10.00.00.mov",
            "source_path": "/fake/Meetings/Screen Recording 2026-11-19 at 10.00.00.mov", "source_type": "audio",
            "created_at": "2026-11-19T10:00:00", "duration": 25.0, "language": "en", "diarization": True,
            "speakers": ["SPEAKER_00", "SPEAKER_01", "SPEAKER_02"], "speaker_names": {},
            "speaker_embeddings": {"SPEAKER_00": [0.9, 0.1, 0.0], "SPEAKER_01": [0.1, 0.9, 0.0], "SPEAKER_02": [0.0, 0.1, 0.9]},
            "screen_names": {"SPEAKER_00": {"names": [tile("Priya Sharma", [1.5, 6.0, 10.5, 12.0])], "frames": 4, "resolved": 4},
                             "SPEAKER_01": {"names": [], "frames": 3, "resolved": 0},
                             "SPEAKER_02": {"names": [tile("Mei Lin Chen", [16.5, 18.0, 19.0])], "frames": 3, "resolved": 3}},
            "screen_info": {"status": "done", "frames": 10, "resolved": 7, "screen_share": 3, "unresolved": 0,
                            "vision_frames": 0, "vision_model": "", "seconds": 4.2, "error": ""},
            "segments": [{"speaker": s, "start": i * 5.0, "end": i * 5.0 + 4.5, "text": t} for i, (s, t) in enumerate(segs)],
            "processing_seconds": 14, "timestamps": True}


def search_turn(system: str, user: str) -> str | None:
    """The agent's search tool: "Find …" → tool call; tool output → an answer citing result [1]."""
    if "- search: Search the user's own chats" not in system:
        return None
    if user.startswith("[Tool output — `search` tool]"):
        first = re.search(r"^\[1\] (.+?)\n.*?link: (\S+)", user, re.M | re.S)
        if not first:
            return "I couldn't find anything about that in your data."
        return f"Here is what I found: {first.group(1).split(' · ')[1]} [1]({first.group(2)})."
    if user.lower().startswith("find "):
        return json.dumps({"tool": "search", "input": user[5:].strip(" ?.")})
    return None


def read_state(path: Path) -> dict:
    """Reads the state shared by the two processes (fake Ollama and the admin server) from the state file."""
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return {"ollama": dict(DEFAULT_OLLAMA)}


def write_state(path: Path, state: dict) -> None:
    """Writes the shared state file (so the fake Ollama sees settings changed through the admin API)."""
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state))
    tmp.replace(path)


# ---------------------------------------------------------------------------
# Fake Ollama
# ---------------------------------------------------------------------------

def make_ollama(state_file: Path) -> FastAPI:
    """The fake Ollama server (its own process, so tests can really stop it): versions, installed models, chat,
    embeddings, downloads and deletes, driven by the shared state.
    """
    app = FastAPI()
    lock = threading.Lock()

    def cfg() -> dict:
        """The current fake-Ollama settings from the state file."""
        return read_state(state_file)["ollama"]

    @app.get("/api/version")
    def version():
        """GET /api/version."""
        return {"version": cfg()["version"]}

    @app.get("/api/tags")
    def tags():
        """GET /api/tags: the installed models."""
        return {"models": [{"name": m, "model": m, "size": SIZES.get(m, 4_000_000_000),
                            "details": {"family": m.split(":")[0], "parameter_size": "8B", "quantization_level": "Q4_K_M"}}
                           for m in cfg()["installed"]]}

    @app.get("/api/ps")
    def ps():
        """GET /api/ps: no model is ever kept loaded."""
        return {"models": []}

    @app.post("/api/embed")
    async def embed(request: Request):
        """POST /api/embed: deterministic fake vectors (bag of words), so search by meaning works."""
        body = await request.json()
        if body.get("model", "").split(":")[0] not in [m.split(":")[0] for m in cfg()["installed"]]:
            return JSONResponse({"error": f'model "{body.get("model")}" not found'}, status_code=404)
        texts = body["input"] if isinstance(body["input"], list) else [body["input"]]
        return {"model": body["model"], "embeddings": [embed_vector(t) for t in texts]}

    @app.post("/api/show")
    async def show(request: Request):
        """POST /api/show: context length, and the 'vision' capability for models that can see."""
        body = await request.json()
        sees = any(v in str(body.get("model", "")) for v in ("gemma4", "-vl"))  # like the real models
        return {"model_info": {"general.context_length": 131072},
                "capabilities": ["completion"] + (["vision"] if sees else [])}

    def check_reply(user: str) -> str:
        """The meeting check: alerts built from the numbered context and the meeting's lines in the prompt, plus
        one without a source (the app must drop it)."""
        lines = re.findall(r"^\[(\d{2}:\d{2}:\d{2})\] ([^:]+): (.+)$", user, re.M)
        decision = re.search(r"^(E\d+) \(decision, ([^)]*)\): (.+)$", user, re.M)
        action = re.search(r"^(E\d+) \(action, ([^)]*)\): (.+)$", user, re.M)
        alerts = []
        moved = next((t for t, _w, text in lines if "14th" in text), None)
        if moved and decision:
            alerts.append({"type": "conflict", "text": f"The date moves to the 14th, but earlier: {decision.group(3)[:60]}",
                           "line": moved, "sources": [decision.group(1)]})
        pending = next((t for t, _w, text in lines if "pending" in text.lower()), None)
        if pending:
            alerts.append({"type": "raise", "text": "Tell all stakeholders that QA sign-off is still pending.",
                           "line": pending, "sources": []})
        if action:
            alerts.append({"type": "pending", "text": f"Still open from an earlier meeting: {action.group(3)[:60]}",
                           "line": None, "sources": [action.group(1)]})
        mine = next((t for t, who, _text in lines if who.strip() == "You"), None)
        if mine:
            alerts.append({"type": "my_action", "text": "Update the release plan and tell the stakeholders.",
                           "line": mine, "sources": []})
        alerts.append({"type": "conflict", "text": "The budget was cut.", "line": mine, "sources": []})  # dropped
        return json.dumps({"alerts": alerts})

    def reply_for(model: str, messages: list[dict]) -> tuple[str, float]:
        """Picks the fake answer from the prompt: speaker identification JSON, meeting notes (from the fixture),
        a tool call for 'Find …' messages, or an echo. Returns (text, seconds to wait between pieces).
        """
        system = messages[0]["content"] if messages and messages[0]["role"] == "system" else ""
        user = next((m["content"] for m in reversed(messages) if m["role"] == "user"), "")
        if "You identify the speakers" in system:
            return json.dumps({"speakers": [
                {"id": "SPEAKER_00", "name": "Priya", "role": "Delivery manager", "confidence": 0.7, "evidence": "runs the meeting and assigns work"},
                {"id": "SPEAKER_01", "name": "Owen", "role": "QA tester", "confidence": 0.8, "evidence": "reports test results"},
                {"id": "SPEAKER_02", "name": None, "role": "Backend developer", "confidence": 0, "evidence": "talks about a fix"}]}), 0
        if "You check a meeting against earlier meetings" in system:
            return check_reply(user), 0.002
        if "You write professional meeting notes" in system:
            return notes_reply(user, model), 0.002
        if "Rewrite them as 3-5 bullets" in user:
            n = len(re.findall(r"^- ", user, re.M))
            return json.dumps({"tldr": [f"Quarterly planning combined from {n} parts", "Roadmap, budget and hiring plan reviewed"]}), 0
        tool = search_turn(system, user) or connector_turn(system, user)
        if tool:
            return tool, 0
        part = re.search(r"You are reading part (\d+) of (\d+)", user)
        if part:
            return f"- Notes for part {part.group(1)} of {part.group(2)}: decisions and action items.", 0
        if "Write meeting minutes" in user:
            parts = user.count("### Part ")
            src = f" (combined from {parts} parts)" if parts else ""
            return (f"## Summary\nThe team reviewed the release{src}, written by {model}.\n\n## Decisions\n- Ship on Friday\n\n"
                    "## Action items\n| Owner | Action | Due | Timestamp |\n|---|---|---|---|\n| Owen | Retest refunds | Friday | 00:00:20 |"), 0.01
        if "You maintain long-term memory" in system:
            return "[]", 0
        if "slow" in user.lower():
            return " ".join(f"word{i}" for i in range(400)), 0.05
        return f"Echo from {model}: {user[:80]}", 0

    @app.post("/api/chat")
    async def chat(request: Request):
        """POST /api/chat: streams the fake answer as NDJSON lines (404 for models that aren't installed)."""
        body = await request.json()
        model = body["model"]
        if model not in cfg()["installed"]:
            return JSONResponse({"error": f'model "{model}" not found, try pulling it first'}, status_code=404)
        text, delay = reply_for(model, body["messages"])
        delay = delay or cfg()["chat_delay"]
        words = re.findall(r"\S+\s*", text) or [text]

        def gen():
            """Yields the answer piece by piece, then the final 'done' line with timings."""
            for w in words:
                time.sleep(delay)
                yield json.dumps({"model": model, "message": {"role": "assistant", "content": w}, "done": False}) + "\n"
            yield json.dumps({"model": model, "message": {"role": "assistant", "content": ""}, "done": True,
                              "prompt_eval_count": 10, "prompt_eval_duration": 1e8, "eval_count": len(words),
                              "eval_duration": 1e9}) + "\n"
        return StreamingResponse(gen(), media_type="application/x-ndjson")

    @app.post("/api/pull")
    async def pull(request: Request):
        """POST /api/pull: a fake download that streams progress and then installs the model."""
        body = await request.json()
        model = body["model"]
        steps, pause = cfg()["pull_steps"], cfg()["pull_step_seconds"]
        total = SIZES.get(model, 4_000_000_000)

        def gen():
            """Yields the download progress lines."""
            yield json.dumps({"status": "pulling manifest"}) + "\n"
            for i in range(1, steps + 1):
                time.sleep(pause)
                yield json.dumps({"status": "pulling 4e2f…", "digest": "sha256:4e2f", "total": total,
                                  "completed": total * i // steps}) + "\n"
            with lock:
                state = read_state(state_file)
                if model not in state["ollama"]["installed"]:
                    state["ollama"]["installed"].append(model)
                write_state(state_file, state)
            yield json.dumps({"status": "success"}) + "\n"
        return StreamingResponse(gen(), media_type="application/x-ndjson")

    @app.delete("/api/delete")
    async def delete(request: Request):
        """DELETE /api/delete: uninstalls a model."""
        body = await request.json()
        with lock:
            state = read_state(state_file)
            state["ollama"]["installed"] = [m for m in state["ollama"]["installed"] if m != body["model"]]
            write_state(state_file, state)
        return {}

    return app


# ---------------------------------------------------------------------------
# Admin + fake GAIA + fake transcriber
# ---------------------------------------------------------------------------

def standup_transcript() -> dict:
    """The synthetic standup meeting used by most e2e tests (t-standup)."""
    segs = [("SPEAKER_00", "Morning all. Owen, can you give us the test update?"),
            ("SPEAKER_01", "Sure. Regression passed on Android, two bugs left on iOS payments."),
            ("SPEAKER_00", "Thanks, Owen. Anything on the API?"),
            ("SPEAKER_02", "The refund endpoint fix is merged, I'll deploy it to staging today."),
            ("SPEAKER_00", "Great. Owen, please retest refunds after the deploy. Due Friday.")]
    return {"id": "t-standup", "title": "Weekly standup", "source_name": "standup.mp4", "source_path": "/fake/Meetings/standup.mp4",
            "source_type": "audio", "created_at": "2026-11-18T10:00:00", "duration": 25.0, "language": "en",
            "diarization": True, "speakers": ["SPEAKER_00", "SPEAKER_01", "SPEAKER_02"], "speaker_names": {},
            "speaker_embeddings": {"SPEAKER_00": [1.0, 0.0, 0.0], "SPEAKER_01": [0.0, 1.0, 0.0], "SPEAKER_02": [0.0, 0.0, 1.0]},
            "segments": [{"speaker": s, "start": i * 5.0, "end": i * 5.0 + 4.5, "text": t} for i, (s, t) in enumerate(segs)],
            "processing_seconds": 12, "timestamps": True}


def long_transcript() -> dict:
    """A 2.5-hour synthetic meeting (1,500 lines) that must be summarised in parts (t-long)."""
    segs = [{"speaker": f"SPEAKER_0{i % 3}", "start": i * 6.0, "end": i * 6.0 + 5.5,
             "text": f"Point {i}: we discussed the roadmap, budget and hiring plan in some detail for the next quarter."}
            for i in range(1500)]
    return {"id": "t-long", "title": "Quarterly planning (long)", "source_name": "planning.m4a", "source_path": "/fake/Meetings/planning.m4a",
            "source_type": "audio", "created_at": "2026-11-17T09:00:00", "duration": 9000.0, "language": "en",
            "diarization": True, "speakers": ["SPEAKER_00", "SPEAKER_01", "SPEAKER_02"], "speaker_names": {},
            "segments": segs, "processing_seconds": 600, "timestamps": True}


def fmt(t: float) -> str:
    """Seconds as 'hh:mm:ss'."""
    t = int(t)
    return f"{t // 3600:02d}:{t % 3600 // 60:02d}:{t % 60:02d}"


def markdown(t: dict) -> str:
    """A transcript as Markdown lines, like the real transcriber's."""
    names = t.get("speaker_names") or {}
    lines = [f"# Meeting transcript: {t['title']}", ""]
    lines += [f"[{fmt(s['start'])}] {names.get(s['speaker'], s['speaker'])}: {s['text']}" for s in t["segments"]]
    return "\n".join(lines) + "\n"


def make_admin(state_file: Path, ollama_port: int) -> FastAPI:
    """The main fake server: the admin API tests use to set things up, the fake GAIA, web search providers, cloud
    providers and MCP servers, and the fake transcriber.
    """
    app = FastAPI()
    child: dict = {"proc": None}
    mem: dict = {}
    requests_log: list[dict] = []

    def start_ollama():
        """Starts the fake Ollama child process and waits until it answers."""
        if child["proc"] and child["proc"].poll() is None:
            return
        child["proc"] = subprocess.Popen([sys.executable, __file__, "ollama", "--port", str(ollama_port),
                                          "--state", str(state_file)])
        import urllib.request
        for _ in range(100):
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{ollama_port}/api/version", timeout=0.5)
                return
            except Exception:
                time.sleep(0.1)

    def stop_ollama():
        """Stops the fake Ollama child process (to test 'Ollama not running')."""
        if child["proc"] and child["proc"].poll() is None:
            child["proc"].terminate()
            child["proc"].wait(5)
        child["proc"] = None

    atexit.register(stop_ollama)

    def reset():
        """Puts everything back to the starting state (called before each test)."""
        write_state(state_file, {"ollama": dict(DEFAULT_OLLAMA, installed=list(DEFAULT_OLLAMA["installed"]))})
        mem.clear()
        mem.update({
            "gaia_fail": False, "job_seconds": 6.0, "settings": {"meetings_dir": "/fake/Meetings"},
            "transcripts": {"t-standup": standup_transcript(), "t-long": long_transcript(), "t-video": video_transcript(),
                            **{t["id"]: json.loads(json.dumps(t)) for t, _ in FIXTURE_MEETINGS.values()}},
            "web_mode": "ok", "web_queries": [],
            "recordings": [
                {"path": "/fake/Meetings/standup.mp4", "name": "standup.mp4", "size": 52_000_000, "modified": "2026-11-18 10:00", "source": "folder"},
                {"path": "/fake/Meetings/Screen Recording silent.mov", "name": "Screen Recording silent.mov", "size": 9_000_000, "modified": "2026-11-18 11:00", "source": "folder"},
            ],
            "jobs": {}, "uploads": {},
            # Recording a call: the one in progress, the last choice, a refusal to simulate (record_error), and
            # live drafts by recording id (lines appear one per `live_line_seconds`). `record_picker`: the recorder
            # has no screen access, so macOS's picker chooses at start (`record_pick`: "zoom" or "cancel").
            "record": None, "record_last": {}, "record_error": None, "record_drafts": {}, "live_line_seconds": 1.0,
            "record_picker": False, "record_pick": "zoom",
            # Other transcripts of a meeting (corrections): sources per transcript, and the original segments.
            "refs": {}, "corr": {}, "ref_seconds": 1.5,
        })
        requests_log.clear()
        fake_cloud.REQUESTS.clear()
        fake_cloud.REVIEW_PLAN["changes"] = {}
        fake_mcp.reset()
        start_ollama()

    reset()

    @app.get("/__admin/health")
    def admin_health():
        """GET /__admin/health: the mocks are up."""
        return {"ok": True}

    @app.post("/__admin/reset")
    def admin_reset():
        """POST /__admin/reset."""
        reset()
        return {"ok": True}

    @app.post("/__admin/ollama")
    async def admin_ollama(request: Request):
        """POST /__admin/ollama: changes the fake Ollama (running, installed models, speed…), starting or
        stopping its process.
        """
        body = await request.json()
        state = read_state(state_file)
        for key in ("installed", "version", "chat_delay", "pull_steps", "pull_step_seconds"):
            if key in body:
                state["ollama"][key] = body[key]
        write_state(state_file, state)
        if body.get("running") is False:
            stop_ollama()
        elif body.get("running") is True:
            start_ollama()
        return state["ollama"]

    @app.post("/__admin/config")
    async def admin_config(request: Request):
        """POST /__admin/config: changes other fake settings (job length, GAIA failing, web search mode…)."""
        mem.update(await request.json())
        return {"ok": True}

    @app.get("/__admin/requests")
    def admin_requests():
        """GET /__admin/requests: which models were called (tests check routing with it)."""
        return requests_log

    @app.post("/__admin/review-plan")
    async def admin_review_plan(request: Request):
        """POST /__admin/review-plan: what the fake cloud review should change."""
        fake_cloud.REVIEW_PLAN["changes"] = (await request.json()).get("changes", {})
        return {"ok": True}

    @app.get("/__admin/cloud-requests")
    def admin_cloud_requests():
        """GET /__admin/cloud-requests: what was sent to the fake cloud providers."""
        return [{"api": r["api"], "path": r["path"], "body": r["body"]} for r in fake_cloud.REQUESTS]

    @app.get("/__admin/mcp")
    def admin_mcp():
        """GET /__admin/mcp: registry searches and tool calls the fake MCP world received."""
        return {"searches": fake_mcp.SEARCHES, "calls": fake_mcp.CALLS}

    # --- fake GAIA (OpenAI-compatible) ---
    @app.post("/gaia/v1/chat/completions")
    async def gaia(request: Request):
        """POST /gaia/v1/chat/completions: the fake GAIA cloud model (streams an echo; can be made to fail with
        gaia_fail).
        """
        body = await request.json()
        requests_log.append({"backend": "cloud", "model": body["model"]})
        if mem["gaia_fail"]:
            return JSONResponse({"error": {"message": "upstream unavailable"}}, status_code=502)
        user = next((m["content"] for m in reversed(body["messages"]) if m["role"] == "user"), "")
        system = body["messages"][0]["content"] if body["messages"][0]["role"] == "system" else ""
        if not body.get("stream"):
            content = "[]" if "long-term memory" in system else '{"needs_search": false}'
            if "Rewrite them as 3-5 bullets" in user:
                content = json.dumps({"tldr": ["Cloud combined the parts"]})
            return {"choices": [{"message": {"content": content}}]}
        text = "Cloud answer: " + user[:60]
        if "Write meeting minutes" in user:
            text = "## Summary\nCloud minutes for the meeting."
        if "You check a meeting against earlier meetings" in system:
            return check_reply(user), 0.002
        if "You write professional meeting notes" in system:
            text = notes_reply(user, "GLM-5.3 Flash (cloud)")
        text = search_turn(system, user) or text

        def gen():
            """Yields the streamed answer as Server-Sent Events."""
            for w in re.findall(r"\S+\s*", text):
                time.sleep(0.02)
                yield f"data: {json.dumps({'choices': [{'delta': {'content': w}}]})}\n\n"
            yield "data: [DONE]\n\n"
        return StreamingResponse(gen(), media_type="text/event-stream")

    # --- fake web search providers (Tavily, Exa) ---
    def web_results(query: str) -> list[dict]:
        """Two fake web results for any query."""
        return [{"title": f"Guide: {query.title()}", "url": "https://example.com/guide", "date": "2026-11-01",
                 "snippet": f"A web article about {query}."},
                {"title": f"{query.title()} — community thread", "url": "https://forum.example.org/t/1", "date": "2026-10-20",
                 "snippet": f"People discussing {query}."}]

    def web_gate(key: str):
        """Simulates provider errors: rate limit or invalid key, depending on `web_mode` or a key starting with
        'bad'. None when the request may go ahead.
        """
        if mem["web_mode"] == "rate" :
            return JSONResponse({"detail": "rate limit"}, status_code=429)
        if mem["web_mode"] == "invalid" or key.startswith("bad"):
            return JSONResponse({"detail": "invalid key"}, status_code=401)
        return None

    @app.post("/tavily/search")
    async def tavily(request: Request):
        """The fake Tavily search API."""
        body = await request.json()
        mem["web_queries"].append(body["query"])
        blocked = web_gate(request.headers.get("authorization", "").removeprefix("Bearer "))
        if blocked:
            return blocked
        return {"results": [{"title": r["title"], "url": r["url"], "content": r["snippet"], "published_date": r["date"]}
                            for r in web_results(body["query"])[: body.get("max_results", 5)]]}

    @app.post("/exa/search")
    async def exa(request: Request):
        """The fake Exa search API."""
        body = await request.json()
        mem["web_queries"].append(body["query"])
        blocked = web_gate(request.headers.get("x-api-key", ""))
        if blocked:
            return blocked
        return {"results": [{"title": r["title"], "url": r["url"], "text": r["snippet"], "publishedDate": r["date"]}
                            for r in web_results(body["query"])]}

    # --- fake transcriber ---
    T = "/transcriber"

    def index(t: dict) -> dict:
        """A transcript's list entry (without its lines)."""
        return {k: t.get(k) for k in ("id", "title", "source_name", "source_type", "created_at", "duration", "language",
                                      "diarization", "speakers", "speaker_names", "speaker_source", "timestamps",
                                      "recording_id")}

    @app.get(T + "/health")
    def t_health():
        """Fake transcriber GET /health."""
        return {"ok": True, "offline": True, "whisper_model": "mlx-community/whisper-large-v3-turbo",
                "diarization_model": "pyannote/speaker-diarization-3.1", "diarization": True,
                "meetings_dir": mem["settings"]["meetings_dir"], "uploads_dir": "/fake/Meetings/uploads",
                "busy": sum(1 for j in mem["jobs"].values() if j["status"] == "running")}

    @app.get(T + "/settings")
    def t_settings():
        """Fake transcriber GET /settings."""
        return {**mem["settings"], "default_meetings_dir": "/fake/Meetings", "uploads_dir": "/fake/Meetings/uploads"}

    @app.post(T + "/settings")
    async def t_set_settings(request: Request):
        """Fake transcriber POST /settings (watch folder)."""
        body = await request.json()
        folder = body.get("meetings_dir") or "/fake/Meetings"
        if folder.startswith("/missing"):
            raise HTTPException(404, f"Folder not found: {folder}")
        mem["settings"]["meetings_dir"] = folder
        return t_settings()

    @app.get(T + "/recordings")
    def t_recordings():
        """Fake transcriber GET /recordings."""
        done = {t.get("source_path"): t["id"] for t in mem["transcripts"].values()}
        return [dict(r, transcript_id=done.get(r["path"])) for r in mem["recordings"]]

    @app.post(T + "/estimate")
    async def t_estimate(request: Request):
        """Fake transcriber POST /estimate (videos also get a names-on-screen estimate)."""
        body = await request.json()
        path = body["path"]
        silent = "silent" in path
        video = path.lower().endswith((".mp4", ".mov"))
        cap = int(body.get("vision_cap") or 0)
        screen = {"frames": 40, "low": 14, "high": round(21 + cap * float(body.get("vision_seconds") or 9))} if video else None
        return {"has_video": video, "screen_estimate": screen,
                "path": path, "duration": 600.0, "audio_tracks": 0 if silent else 1, "has_audio": not silent,
                "no_audio_message": "This recording has no sound, so there is nothing to transcribe." if silent else "",
                "estimate_low": 60, "estimate_high": 110, "speed_factor": 0.12, "temp_disk_bytes": 19_000_000,
                "free_disk_bytes": 200_000_000_000, "disk_ok": True, "diarization": True, "queued_ahead": 0}

    def job_view(job: dict) -> dict:
        """A job's state: it finishes after `job_seconds`, creating a transcript that keeps the job's model
        choices, like the real transcriber.
        """
        if job["status"] in ("queued", "running"):
            elapsed = time.time() - job["created"]
            total = mem["job_seconds"]
            if elapsed >= total:
                tid = "t-" + uuid.uuid4().hex[:8]
                t = standup_transcript()
                t.update(id=tid, title=Path(job["path"]).stem, source_path=job["path"], source_name=job["name"],
                         created_at=time.strftime("%Y-%m-%dT%H:%M:%S"), models=job.get("models") or {})
                if job.get("recording_id"):  # a call Quorum recorded: you are confirmed on your microphone track
                    t.update(recording_id=job["recording_id"], audio_tracks={"mic": True, "system": True},
                             confirmed_speakers={"SPEAKER_01": {"source": "microphone"}})
                mem["transcripts"][tid] = t
                job.update(status="done", stage="done", progress=1.0, transcript_id=tid, finished=time.time())
            else:
                frac = elapsed / total
                stage = "starting" if frac < 0.1 else "transcribe" if frac < 0.8 else "diarize"
                job.update(status="running", stage=stage, progress=round(min(0.99, frac), 3))
        out = {k: v for k, v in job.items() if k != "created"}
        out["elapsed"] = round(time.time() - job["created"])
        out["eta"] = max(0, round(mem["job_seconds"] - out["elapsed"])) if job["status"] == "running" else None
        return out

    @app.post(T + "/jobs")
    async def t_jobs(request: Request):
        """Fake transcriber POST /jobs (refuses recordings with 'silent' in the name)."""
        body = await request.json()
        if "silent" in body["path"]:
            raise HTTPException(422, "This recording has no sound, so there is nothing to transcribe.")
        jid = uuid.uuid4().hex[:12]
        mem["jobs"][jid] = {"id": jid, "path": body["path"], "name": Path(body["path"]).name, "status": "queued",
                            "stage": "queued", "progress": 0.0, "duration": 600.0, "created": time.time(),
                            "error": None, "transcript_id": None, "speakers": body.get("speakers"),
                            "models": body.get("models"), "screen": body.get("screen")}
        return job_view(mem["jobs"][jid])

    @app.get(T + "/jobs")
    def t_list_jobs():
        """Fake transcriber GET /jobs."""
        return [job_view(j) for j in mem["jobs"].values()]

    @app.get(T + "/jobs/{jid}")
    def t_job(jid: str):
        """Fake transcriber GET /jobs/{id}."""
        if jid not in mem["jobs"]:
            raise HTTPException(404, "Unknown job.")
        return job_view(mem["jobs"][jid])

    @app.get(T + "/jobs/{jid}/partial")
    def t_partial(jid: str):
        """Fake transcriber GET /jobs/{id}/partial: the text so far grows with time."""
        job = mem["jobs"].get(jid)
        if not job or job["status"] != "running":
            return {"segments": [], "windows": 0, "status": job["status"] if job else "missing"}
        n = int((time.time() - job["created"]) / mem["job_seconds"] * 10)
        return {"segments": [{"start": i * 12.0, "end": i * 12.0 + 10, "text": f"Live line {i + 1} of the recording."}
                             for i in range(n)], "windows": n, "status": "running"}

    @app.post(T + "/jobs/{jid}/cancel")
    def t_cancel(jid: str):
        """Fake transcriber POST /jobs/{id}/cancel."""
        job = mem["jobs"][jid]
        if job["status"] in ("queued", "running"):
            job.update(status="cancelled", stage="cancelled")
        return job_view(job)

    # --- Recording a call (fake Quorum Recorder) -------------------------------------------------------------

    LIVE_SCRIPT = [("Priya Sharma", "Release is on Friday, the 7th."), ("You", "Owen owns the rollback plan."),
                   ("Others", "QA sign-off is still pending."), ("Priya Sharma", "Let's move the release to the 14th."),
                   ("You", "I'll tell the stakeholders.")]

    def record_refusal():
        if mem["record_error"]:
            raise HTTPException(503, {"kind": mem["record_error"], "message": "Not allowed yet (fake)."})

    def live_lines(rec_id: str) -> list[dict]:
        draft = mem["record_drafts"].get(rec_id)
        if not draft:
            return []
        end = draft.get("stopped") or time.time()
        n = min(len(LIVE_SCRIPT), int((end - draft["started"]) / mem["live_line_seconds"]))
        return [{"speaker": who, "start": i * 30.0 + 5, "end": i * 30.0 + 9, "text": text}
                for i, (who, text) in enumerate(LIVE_SCRIPT[:n])]

    @app.get(T + "/record/sources")
    def t_record_sources():
        """Fake transcriber GET /record/sources."""
        record_refusal()
        return {"displays": [{"id": 1, "name": "Built-in Retina Display", "width": 1512, "height": 982, "main": True},
                             {"id": 2, "name": "DELL U2723QE", "width": 3840, "height": 2160, "main": False}],
                "apps": [{"bundle_id": "us.zoom.xos", "name": "zoom.us", "meeting": True, "remote": False, "display": 1,
                          "windows": [{"title": "Zoom Meeting", "width": 1200, "height": 800, "display": 1}]},
                         {"bundle_id": "com.microsoft.rdc.macos", "name": "Windows App", "meeting": False, "remote": True,
                          "display": 1, "windows": [{"title": "Work PC", "width": 1400, "height": 900, "display": 1}]},
                         {"bundle_id": "com.apple.TextEdit", "name": "TextEdit", "meeting": False, "remote": False,
                          "display": 1, "windows": [{"title": "Notes", "width": 800, "height": 600, "display": 1}]}],
                "microphones": [{"id": "builtin", "name": "MacBook Air Microphone", "default": True, "bluetooth": False},
                                {"id": "airpods", "name": "AirPods Pro", "default": False, "bluetooth": True}],
                "picker": mem["record_picker"], "last": mem["record_last"]}

    @app.post(T + "/record/start")
    async def t_record_start(request: Request):
        """Fake transcriber POST /record/start."""
        record_refusal()
        body = await request.json()
        if mem["record"]:
            raise HTTPException(409, {"kind": "busy", "message": "A call is already being recorded."})
        if mem["record_picker"] or (body["source"].get("kind") == "app" and not body["source"].get("id")):
            if mem["record_pick"] == "cancel":
                raise HTTPException(409, {"kind": "cancelled",
                                          "message": "No window or screen was chosen, so nothing is recorded."})
            if body["source"].get("kind") == "app":
                body["source"] = {"kind": "app", "id": "us.zoom.xos", "name": "zoom.us", "picked": True}
        rec_id = uuid.uuid4().hex[:12]
        mem["record"] = {"id": rec_id, "started": time.time(), **body}
        mem["record_last"] = {"source": body["source"], "mic": body.get("mic"), "live": body.get("live", False)}
        if body.get("live"):
            mem["record_drafts"][rec_id] = {"started": time.time(), "title": body.get("title") or "Call"}
        return t_record_status()

    @app.get(T + "/record/status")
    def t_record_status():
        """Fake transcriber GET /record/status."""
        rec = mem["record"]
        if not rec:
            return {"state": "idle"}
        elapsed = round(time.time() - rec["started"])
        live = None
        if rec.get("live"):
            live = {"state": "running", "processed_seconds": len(live_lines(rec["id"])) * 30.0, "behind_seconds": 3.0}
        return {"state": "recording", "id": rec["id"], "title": rec.get("title", ""), "source": rec["source"],
                "mic": "MacBook Air Microphone", "video": rec["source"].get("kind") != "audio", "live": bool(rec.get("live")),
                "started_at": time.strftime("%Y-%m-%dT%H:%M:%S"), "elapsed": elapsed,
                "levels": {"system": 0.08, "mic": 0.04}, "bytes": elapsed * 200_000, "warnings": [], "reason": None,
                "live_state": live}

    @app.post(T + "/record/stop")
    async def t_record_stop(request: Request):
        """Fake transcriber POST /record/stop: queues the transcription of the call (with its recording id)."""
        body = await request.json()
        rec = mem["record"]
        if not rec:
            raise HTTPException(409, {"kind": "not_recording", "message": "No call is being recorded."})
        mem["record"] = None
        if rec["id"] in mem["record_drafts"]:
            mem["record_drafts"][rec["id"]]["stopped"] = time.time()
        name = f"{rec.get('title') or 'Call'} {time.strftime('%Y-%m-%d %H.%M')}.mov"
        path = f"/fake/Meetings/uploads/{name}"
        done = {"id": rec["id"], "path": path, "seconds": round(time.time() - rec["started"]), "complete": True,
                "reason": "stopped", "live_dir": None, "title": rec.get("title", "")}
        job = None
        if body.get("transcribe", True):
            jid = uuid.uuid4().hex[:12]
            mem["jobs"][jid] = {"id": jid, "path": path, "name": name, "status": "queued", "stage": "queued",
                                "progress": 0.0, "duration": 600.0, "created": time.time(), "error": None,
                                "transcript_id": None, "speakers": body.get("speakers"), "models": body.get("models"),
                                "screen": body.get("screen"), "recording_id": rec["id"]}
            job = job_view(mem["jobs"][jid])
        return {"recording": done, "job": job}

    def final_for(rec_id: str) -> str | None:
        return next((t["id"] for t in mem["transcripts"].values() if t.get("recording_id") == rec_id), None)

    @app.get(T + "/record/live")
    def t_record_live(id: str | None = None, since: int = 0):
        """Fake transcriber GET /record/live."""
        for job in list(mem["jobs"].values()):
            job_view(job)  # finished jobs create their transcripts
        rec_id = id or (mem["record"] or {}).get("id") or next(reversed(mem["record_drafts"]), None)
        if not rec_id or rec_id not in mem["record_drafts"]:
            raise HTTPException(404, "No live transcript.")
        lines = live_lines(rec_id)
        draft = mem["record_drafts"][rec_id]
        final = final_for(rec_id)
        state = "final" if final else ("done" if draft.get("stopped") else "running")
        return {"id": rec_id, "title": draft["title"], "state": state, "error": None, "segments": lines[since:],
                "total": len(lines), "processed_seconds": len(lines) * 30.0, "behind_seconds": 3.0,
                "transcript_id": final}

    @app.get(T + "/record/live/markdown", response_class=PlainTextResponse)
    def t_record_live_md(id: str | None = None):
        """Fake transcriber GET /record/live/markdown."""
        data = t_record_live(id)
        rows = [f"[{fmt(s['start'])}] {s['speaker']}: {s['text']}" for s in live_lines(data["id"])]
        return f"# Live transcript: {data['title']}\n_Draft while the call is recorded._\n\n" + "\n".join(rows) + "\n"

    @app.post(T + "/uploads/init")
    async def t_upload_init(request: Request):
        """Fake transcriber POST /uploads/init."""
        body = await request.json()
        uid = uuid.uuid4().hex
        mem["uploads"][uid] = {"name": body["name"], "size": body["size"], "chunks": set()}
        return {"upload_id": uid, "chunk_size": 1_000_000, "received": []}

    @app.put(T + "/uploads/{uid}/chunk/{n}")
    async def t_chunk(uid: str, n: int, request: Request):
        """Fake transcriber PUT /uploads/{id}/chunk/{n}."""
        await request.body()
        mem["uploads"][uid]["chunks"].add(n)
        return {"ok": True, "chunk": n}

    @app.post(T + "/uploads/{uid}/complete")
    def t_complete(uid: str):
        """Fake transcriber POST /uploads/{id}/complete."""
        up = mem["uploads"].pop(uid)
        path = f"/fake/Meetings/uploads/{up['name']}"
        mem["recordings"].append({"path": path, "name": up["name"], "size": up["size"], "modified": time.strftime("%Y-%m-%d %H:%M"), "source": "upload"})
        return {"path": path, "name": up["name"]}

    @app.get(T + "/transcripts")
    def t_transcripts():
        """Fake transcriber GET /transcripts."""
        return sorted((index(t) for t in mem["transcripts"].values()), key=lambda t: t["created_at"], reverse=True)

    def get_t(tid: str) -> dict:
        """One fake transcript, or 404."""
        if tid not in mem["transcripts"]:
            raise HTTPException(404, "Transcript not found.")
        return mem["transcripts"][tid]

    @app.get(T + "/transcripts/{tid}")
    def t_get(tid: str):
        """Fake transcriber GET /transcripts/{id}."""
        return get_t(tid)

    def corr_apply(tid: str, reapply: bool = False) -> dict:
        """The fake transcriber's correction: the real engine (transcriber/correct.py) on the fake meeting."""
        t = get_t(tid)
        state = mem["corr"].setdefault(tid, {"original": t["segments"], "reverted": [], "undone": False})
        if reapply:
            state["undone"] = False
        refs = mem["refs"].get(tid, [])
        for ref in refs:  # a recording "transcribed" by now
            if ref["status"] == "transcribing" and time.time() >= ref["ready_at"]:
                ref["status"] = "ready"
        ready = [r for r in refs if r["status"] == "ready"]
        if not ready or state["undone"]:
            t["segments"] = state["original"]
            result = {"changes": [], "refs": [], "marks": {}, "names": {},
                      "stats": {"words": 0, "turns": 0, "names": 0, "applied_refs": 0}}
        else:
            result = correct.correct(state["original"], ready, None, [], set(t.get("confirmed_speakers") or {}),
                                     set(state["reverted"]))
            t["segments"] = result["segments"]
        found = {r["id"]: r for r in result["refs"]}
        summary = lambda r: {k: r.get(k) for k in ("id", "name", "source", "kind", "status", "error", "created_at")}
        correction = {"refs": [dict(summary(r), **{k: v for k, v in found.get(r["id"], {}).items() if k != "name"})
                               for r in refs],
                      "changes": result["changes"], "marks": result["marks"], "names": result["names"],
                      "stats": result["stats"], "reverted": state["reverted"], "known_terms": [],
                      "undone": state["undone"], "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S")}
        if refs:
            t["correction"] = correction
        else:
            t.pop("correction", None)
        return correction

    def new_ref(source: str, name: str, kind: str) -> dict:
        rid = uuid.uuid4().hex[:8]
        return {"id": rid, "name": name or {"voice_memos": "Voice Memos", "claude": "claude.ai"}.get(source, "Other"),
                "source": source, "kind": kind, "recording": source if source != "other" else f"other-{rid}",
                "created_at": time.strftime("%Y-%m-%dT%H:%M:%S"), "status": "ready"}

    @app.get(T + "/transcripts/{tid}/references")
    def t_refs(tid: str):
        """Fake transcriber GET /transcripts/{id}/references."""
        correction = corr_apply(tid) if mem["refs"].get(tid) else None
        return {"refs": mem["refs"].get(tid, []), "correction": correction}

    @app.post(T + "/transcripts/{tid}/references")
    async def t_add_ref(tid: str, request: Request):
        """Fake transcriber POST /transcripts/{id}/references (text)."""
        body = await request.json()
        get_t(tid)
        segments, timed = text_import.parse(body["text"])
        ref = dict(new_ref(body.get("source", "other"), body.get("name", ""), "text"), segments=segments, timed=timed)
        mem["refs"].setdefault(tid, []).append(ref)
        return corr_apply(tid, reapply=True)

    @app.post(T + "/transcripts/{tid}/references/audio")
    async def t_add_audio_ref(tid: str, request: Request):
        """Fake transcriber POST …/references/audio: "transcribed" after `ref_seconds`, as the meeting's words
        with "Friday" heard as "Thursday" (the e2e correction)."""
        body = await request.json()
        t = get_t(tid)
        original = mem["corr"].get(tid, {}).get("original") or t["segments"]
        words = []
        for seg in original:
            parts = seg["text"].replace("Friday", "Thursday").split()
            for k, part in enumerate(parts):
                at = seg["start"] + (seg["end"] - seg["start"]) * k / max(1, len(parts)) + 12.0
                words.append({"word": " " + part, "start": round(at, 2), "end": round(at + 0.3, 2)})
        ref = dict(new_ref(body.get("source", "voice_memos"), body.get("name") or "New Recording", "audio"),
                   words=words, status="transcribing", ready_at=time.time() + mem["ref_seconds"])
        mem["refs"].setdefault(tid, []).append(ref)
        corr_apply(tid)
        return {"job": {"id": "ref-job", "status": "queued"}, "ref": {k: ref[k] for k in ("id", "name", "status")}}

    @app.delete(T + "/transcripts/{tid}/references/{rid}")
    def t_delete_ref(tid: str, rid: str):
        mem["refs"][tid] = [r for r in mem["refs"].get(tid, []) if r["id"] != rid]
        return corr_apply(tid)

    @app.post(T + "/transcripts/{tid}/corrections/{action}")
    async def t_corrections(tid: str, action: str):
        if action == "undo":
            mem["corr"].setdefault(tid, {"original": get_t(tid)["segments"], "reverted": [], "undone": False})["undone"] = True
            return corr_apply(tid)
        return corr_apply(tid, reapply=True)

    @app.post(T + "/transcripts/{tid}/corrections/{cid}/{action}")
    def t_correction_change(tid: str, cid: str, action: str):
        state = mem["corr"][tid]
        reverted = set(state["reverted"])
        (reverted.add if action == "revert" else reverted.discard)(cid)
        state["reverted"] = sorted(reverted)
        return corr_apply(tid)

    def media_parts(t: dict) -> list[dict]:
        if t.get("confirmed_speakers"):
            return [{"part": "call", "label": "Call"}, {"part": "mic", "label": "You"}]
        return [{"part": "audio", "label": "Sound"}]

    @app.get(T + "/transcripts/{tid}/media")
    def t_media_info(tid: str):
        """Fake transcriber GET /transcripts/{id}/media: a recorded call has call + mic sound, others one sound
        (no video: the test browser has no H.264)."""
        t = get_t(tid)
        if t.get("source_type") == "text":
            return {"available": False, "reason": "text", "message": "This transcript was imported from text."}
        return {"available": True, "path": t.get("source_path"), "name": t.get("source_name"), "video": False,
                "parts": media_parts(t), "duration": t.get("duration")}

    @app.get(T + "/transcripts/{tid}/media/{part}")
    def t_media_part(tid: str, part: str):
        """Fake transcriber GET /transcripts/{id}/media/{part}: a synthetic tone as WAV (Range supported)."""
        import math
        import wave
        t = get_t(tid)
        if part not in {p["part"] for p in media_parts(t)}:
            raise HTTPException(404, "No such part.")
        path = state_file.parent / "media" / f"{tid}-{part}.wav"
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            rate, seconds = 8000, int(min(max(t.get("duration") or 10, 10), 600))
            pitch = 220 if part == "mic" else 440
            frames = b"".join(int(3000 * math.sin(2 * math.pi * pitch * i / rate)).to_bytes(2, "little", signed=True)
                              for i in range(rate * seconds))
            with wave.open(str(path), "wb") as w:
                w.setnchannels(1)
                w.setsampwidth(2)
                w.setframerate(rate)
                w.writeframes(frames)
        return FileResponse(path, media_type="audio/wav")

    @app.get(T + "/transcripts/{tid}/markdown", response_class=PlainTextResponse)
    def t_md(tid: str):
        """Fake transcriber GET /transcripts/{id}/markdown."""
        return markdown(get_t(tid))

    @app.post(T + "/transcripts/import")
    async def t_import(request: Request):
        """Fake transcriber POST /transcripts/import (pasted conversation text)."""
        body = await request.json()
        try:
            t = text_import.build_transcript(body["title"], body["text"], body.get("source_name", ""))
        except ValueError as exc:
            raise HTTPException(400, str(exc))
        mem["transcripts"][t["id"]] = t
        return index(t)

    @app.delete(T + "/transcripts/{tid}")
    def t_delete(tid: str, recording: bool = False):
        """Fake transcriber DELETE /transcripts/{id}."""
        get_t(tid)
        mem["transcripts"].pop(tid)
        return {"deleted": tid}

    @app.post(T + "/transcripts/{tid}/speakers")
    async def t_names(tid: str, request: Request):
        """Fake transcriber POST /transcripts/{id}/speakers."""
        t = get_t(tid)
        t["speaker_names"] = (await request.json())["names"]
        return index(t)

    @app.put(T + "/transcripts/{tid}/segments")
    async def t_segments(tid: str, request: Request):
        """Fake transcriber PUT /transcripts/{id}/segments."""
        t = get_t(tid)
        body = await request.json()
        t.update(segments=body["segments"], speaker_names=body["speaker_names"], speaker_source=body["speaker_source"],
                 speakers=list(dict.fromkeys(s["speaker"] for s in body["segments"])))
        return index(t)

    return app


def main() -> None:
    """Starts the mock services: the fake Ollama (child process) or the admin server, depending on the command-
    line arguments.
    """
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=["admin", "ollama"])
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--ollama-port", type=int, default=18434)
    parser.add_argument("--state", type=Path, required=True)
    args = parser.parse_args()
    args.state.parent.mkdir(parents=True, exist_ok=True)
    app = make_admin(args.state, args.ollama_port) if args.mode == "admin" else make_ollama(args.state)
    if args.mode == "admin":
        from http.server import ThreadingHTTPServer
        fake_mcp.Handler.base = f"http://127.0.0.1:{MCP_PORT}"
        for port, handler in ((CLOUD_PORT, fake_cloud.Handler), (MCP_PORT, fake_mcp.Handler)):
            server = ThreadingHTTPServer(("127.0.0.1", port), handler)
            threading.Thread(target=server.serve_forever, daemon=True).start()
    uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
