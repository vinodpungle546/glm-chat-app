#!/usr/bin/env python3
"""Chat-model registry and provider interface.

Models are configured in models.json (not hard-coded). Every model is reached
through the same `ModelProvider` interface:

  * `GaiaProvider`  – the cloud model (Globant GAIA, GLM-5.3-Flash). It delegates to
    llm_backends' cloud functions, so cloud requests stay byte-for-byte unchanged.
  * `OllamaProvider` – any local model in Ollama (http://localhost:11434), e.g. gemma4:e4b.
  * `cloud_providers.CloudChatProvider` – models of cloud providers the user added in
    Settings → Models (Anthropic Claude, OpenAI, OpenAI-compatible endpoints).

Installed Ollama models that aren't in models.json are listed as well, so a newly
pulled model shows up without a config change. `PullManager` downloads models with
progress and cancel.
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Protocol

import requests

import llm_backends

REGISTRY_FILE = Path(os.environ.get("MODEL_REGISTRY_FILE", Path(__file__).with_name("models.json")))
OLLAMA_STATUS = ("ready", "not_downloaded", "ollama_down")


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

def _expand(value: str) -> str:
    """Replaces ${NAME} placeholders in models.json with real values (e.g. ${GAIA_MODEL})."""
    return re.sub(r"\$\{(\w+)\}", lambda m: {"GAIA_MODEL": llm_backends.GAIA_MODEL}.get(
        m.group(1), os.environ.get(m.group(1), "")), value)


def load_registry(path: Path | None = None) -> dict:
    """Reads models.json: the chat models (each tagged 'local' or 'cloud'), the cloud providers' models, and the
    defaults for chat, speaker identification and video frames.
    """
    data = json.loads((path or REGISTRY_FILE).read_text(encoding="utf-8"))
    models = []
    for entry in data.get("models", []):
        entry = {k: (_expand(v) if isinstance(v, str) else v) for k, v in entry.items()}
        entry["group"] = "cloud" if entry["provider"] == "gaia" else "local"
        models.append(entry)
    import cloud_providers
    models += cloud_providers.registry_entries()
    return {"default": data.get("default", ""), "models": models,
            "vision_default": data.get("vision_default", ""), "vision_models": data.get("vision_models", []),
            "speaker_default": data.get("speaker_default", "")}


def find(model_id: str, registry: dict | None = None) -> dict | None:
    """The registry entry with this model id. An installed Ollama model that isn't in models.json still gets an
    entry ('local:<name>'). None if unknown.
    """
    registry = registry or load_registry()
    for entry in registry["models"]:
        if entry["id"] == model_id:
            return entry
    if model_id.startswith("local:"):  # an installed Ollama model that isn't in models.json
        name = model_id[len("local:"):]
        return {"id": model_id, "provider": "ollama", "model": name, "label": name,
                "group": "local", "description": "Installed in Ollama", "uses": ["chat", "summary"]}
    return None


def local_id(model: str) -> str:
    """The app's id for a local model: 'local:<ollama name>'."""
    return f"local:{model}"


# ---------------------------------------------------------------------------
# Provider interface
# ---------------------------------------------------------------------------

class ModelProvider(Protocol):
    """What every model provider offers (an interface): an id, a backend, stream() and complete().
    OllamaProvider, GaiaProvider and CloudChatProvider implement it.
    """
    model_id: str
    backend: llm_backends.Backend

    def stream(self, messages: list[dict]) -> Iterator[str]:
        """Yields the reply text piece by piece as the model writes it."""
        ...

    def complete(self, messages: list[dict], max_tokens: int | None = None,
                 temperature: float | None = None) -> str:
        """The whole reply as one string (for background calls that don't stream to the screen)."""
        ...


@dataclass
class OllamaProvider:
    """Local model in Ollama. Errors are LocalModelError and never fall back to the cloud."""
    model: str

    @property
    def model_id(self) -> str:
        """The app's id of this local model."""
        return local_id(self.model)

    @property
    def backend(self) -> llm_backends.Backend:
        """The local Backend for this model."""
        return llm_backends.local(self.model)

    @property
    def is_local(self) -> bool:
        """Always True: Ollama runs on this Mac."""
        return True

    def stream(self, messages: list[dict]) -> Iterator[str]:
        """Streams the local model's reply."""
        yield from llm_backends.stream_chat(messages, self.backend)

    def complete(self, messages: list[dict], max_tokens: int | None = None,
                 temperature: float | None = None) -> str:
        """The local model's whole reply (max_tokens and temperature are applied)."""
        return llm_backends.complete(messages, self.backend, max_tokens=max_tokens, temperature=temperature)


@dataclass
class GaiaProvider:
    """The cloud model: requests are exactly llm_backends' (pinned by test_cloud_regression)."""
    model_id: str = "cloud:gaia"

    @property
    def backend(self) -> llm_backends.Backend:
        """The GAIA cloud Backend."""
        return llm_backends.cloud()

    @property
    def is_local(self) -> bool:
        """Always False: GAIA is a cloud service."""
        return False

    @property
    def provider_key(self) -> str:
        """Key for GAIA's approvals ('gaia')."""
        return "gaia"

    @property
    def provider_label(self) -> str:
        """The provider's display name."""
        return "Globant GAIA"

    def stream(self, messages: list[dict]) -> Iterator[str]:
        """Streams GAIA's reply (requests are pinned by the regression tests)."""
        yield from llm_backends.stream_chat(messages, self.backend)

    def complete(self, messages: list[dict], max_tokens: int | None = None,
                 temperature: float | None = None, payload_order: str = "messages_first") -> str:
        # max_tokens / temperature are ignored: the cloud payload must not change.
        """GAIA's whole reply; `payload_order` keeps the original request's key order."""
        return llm_backends.complete(messages, self.backend, payload_order=payload_order)


def get_provider(model_id: str, registry: dict | None = None):
    """The provider object for a model id (GAIA, another cloud provider or Ollama). Raises KeyError for an
    unknown id.
    """
    entry = find(model_id, registry)
    if entry is None:
        raise KeyError(f"Unknown model: {model_id}")
    if entry["provider"] == "gaia":
        return GaiaProvider(entry["id"])
    if entry["provider"] == "cloud":
        import cloud_providers
        return cloud_providers.provider_for(entry)
    return OllamaProvider(entry["model"])


# ---------------------------------------------------------------------------
# Ollama status
# ---------------------------------------------------------------------------

def _ollama(path: str, timeout: float = 3) -> dict:
    """GET a path from Ollama and return its JSON (raises on network or HTTP errors)."""
    response = requests.get(f"{llm_backends.LOCAL_LLM_BASE_URL}{path}", timeout=timeout)
    response.raise_for_status()
    return response.json()


def ollama_state() -> dict:
    """{'running', 'version', 'installed': {name: tag entry}}."""
    try:
        version = _ollama("/api/version").get("version", "")
        tags = _ollama("/api/tags").get("models", [])
    except (requests.RequestException, ValueError):
        return {"running": False, "version": "", "installed": {}}
    return {"running": True, "version": version, "installed": {t["name"]: t for t in tags}}


def version_tuple(version: str) -> tuple[int, ...]:
    """A version string as numbers for comparing: '0.40.16' -> (0, 40, 16)."""
    return tuple(int(x) for x in re.findall(r"\d+", version)[:3]) or (0,)


def _installed_name(model: str, installed: dict) -> str | None:
    """The name under which `model` is installed in Ollama ('gemma4' may be installed as 'gemma4:latest'), or
    None.
    """
    if model in installed:
        return model
    if ":" not in model and f"{model}:latest" in installed:
        return f"{model}:latest"
    return None


def list_models(registry: dict | None = None, state: dict | None = None,
                pulls: dict | None = None) -> dict:
    """Registry + installed Ollama chat models, each with a status badge."""
    import local_models

    registry = registry or load_registry()
    state = state if state is not None else ollama_state()
    installed = state["installed"]
    out, listed = [], set()
    for entry in registry["models"]:
        item = dict(entry)
        if entry["provider"] == "ollama":
            name = _installed_name(entry["model"], installed)
            item["status"] = ("ollama_down" if not state["running"]
                              else "ready" if name else "not_downloaded")
            if name:
                listed.add(name)
                item["installed_bytes"] = int(installed[name].get("size") or 0)
            need = entry.get("min_ollama")
            item["ollama_too_old"] = bool(state["running"] and need
                                          and version_tuple(state["version"]) < version_tuple(need))
        elif entry["provider"] == "gaia":
            item["status"] = "ready" if llm_backends.GAIA_API_KEY else "not_configured"
        if pulls and item.get("model") in pulls:
            item["pull"] = pulls[item["model"]]
        out.append(item)
    for name, tag in sorted(installed.items()):
        if name in listed or local_models.is_embedding_model(tag):
            continue
        details = tag.get("details") or {}
        out.append({"id": local_id(name), "provider": "ollama", "model": name, "label": name,
                    "group": "local", "status": "ready", "installed_bytes": int(tag.get("size") or 0),
                    "description": f"{details.get('parameter_size', '')} {details.get('quantization_level', '')}".strip()
                    or "Installed in Ollama", "uses": ["chat", "summary"]})
    return {"ollama_running": state["running"], "ollama_version": state["version"],
            "default": registry["default"], "models": out}


def pick_local_model(preferred: str | None = None, state: dict | None = None) -> str | None:
    """A ready local model: `preferred` if installed, else the registry default, else any."""
    state = state if state is not None else ollama_state()
    installed = state["installed"]
    registry = load_registry()
    candidates = [preferred] if preferred else []
    default = find(registry["default"], registry) if registry["default"] else None
    if default and default["provider"] == "ollama":
        candidates.append(default["model"])
    candidates += [e["model"] for e in registry["models"] if e["provider"] == "ollama"]
    for candidate in candidates:
        name = _installed_name(candidate, installed) if candidate else None
        if name:
            return name
    import local_models
    chat = [n for n, t in sorted(installed.items()) if not local_models.is_embedding_model(t)]
    return chat[0] if chat else None


_CAPABILITIES: dict[str, list[str]] = {}  # model name -> Ollama capabilities (fixed per installed model)


def capabilities(name: str) -> list[str]:
    """What an installed model can do according to Ollama, e.g. ['completion', 'vision'] (cached; a model's
    capabilities don't change).
    """
    if name not in _CAPABILITIES:
        import local_models
        try:
            _CAPABILITIES[name] = list(local_models.model_info(name)["capabilities"])
        except local_models.OllamaUnavailable:
            return []
    return _CAPABILITIES[name]


def vision_models(state: dict | None = None, registry: dict | None = None) -> dict:
    """Local models that can read video frames (on-screen names): the registry's suggestions
    (installed or not) and any other installed model whose Ollama capabilities include vision."""
    import local_models

    registry = registry or load_registry()
    state = state if state is not None else ollama_state()
    installed = state["installed"]
    default = registry.get("vision_default", "")
    out, listed = [], set()
    for entry in registry.get("vision_models", []):
        name = _installed_name(entry["model"], installed)
        out.append({**entry, "installed": bool(name), "recommended": entry["model"] == default})
        if name:
            listed.add(name)
    for name, tag in sorted(installed.items()):
        if name in listed or local_models.is_embedding_model(tag) or "vision" not in capabilities(name):
            continue
        details = tag.get("details") or {}
        out.append({"model": name, "label": name, "installed": True, "recommended": False,
                    "description": f"{details.get('parameter_size', '')} {details.get('quantization_level', '')}".strip()})
    return {"default": default, "models": out, "ollama_running": state["running"]}


def pick_vision_model(preferred: str | None = None, state: dict | None = None) -> str | None:
    """An installed model that can read images: `preferred`, else the recommended one, else any."""
    state = state if state is not None else ollama_state()
    listing = vision_models(state)
    ready = [m["model"] for m in listing["models"] if m["installed"]]
    for candidate in ([preferred] if preferred else []) + [listing["default"]] + ready:
        name = _installed_name(candidate, state["installed"]) if candidate else None
        if name and (candidate in ready or "vision" in capabilities(name)):
            return name
    return None


def _registry_entry(model: str, registry: dict) -> dict:
    """What models.json says about a local model (chat or vision list), merged."""
    out: dict = {}
    for entry in registry.get("vision_models", []) + [e for e in registry["models"] if e.get("provider") == "ollama"]:
        if entry.get("model") == model:
            out = {**entry, **out}
    return out


def speeds_for(model: str, registry: dict | None = None) -> dict:
    """{prefill_tps, gen_tps}: measured on this Mac, else models.json's, else {} (env defaults)."""
    measured = llm_backends.MEASURED_SPEEDS.get(model)
    if measured:
        return dict(measured)
    return dict(_registry_entry(model, registry or load_registry()).get("speeds") or {})


def seconds_per_frame(model: str, registry: dict | None = None) -> float | None:
    """How long this vision model takes per video frame (from models.json), or None."""
    return _registry_entry(model, registry or load_registry()).get("seconds_per_frame")


def speaker_models(state: dict | None = None, registry: dict | None = None) -> dict:
    """Local models for speaker identification (reading the transcript): the registry's local models
    and vision models, and anything else installed. Each says whether it can also read video frames."""
    import local_models

    registry = registry or load_registry()
    state = state if state is not None else ollama_state()
    installed = state["installed"]
    default = registry.get("speaker_default", "")
    out: dict[str, dict] = {}
    entries = [e for e in registry["models"] if e.get("provider") == "ollama"] + registry.get("vision_models", [])
    for entry in entries:
        if entry["model"] in out:
            continue
        name = _installed_name(entry["model"], installed)
        out[entry["model"]] = {"model": entry["model"], "label": entry.get("label", entry["model"]),
                               "description": entry.get("description", ""), "size_gb": entry.get("size_gb"),
                               "installed": bool(name), "recommended": entry["model"] == default,
                               "vision": bool(name) and "vision" in capabilities(name)
                               or any(v["model"] == entry["model"] for v in registry.get("vision_models", []))}
    for name, tag in sorted(installed.items()):
        if name in out or local_models.is_embedding_model(tag):
            continue
        out[name] = {"model": name, "label": name, "installed": True, "recommended": False,
                     "vision": "vision" in capabilities(name), "description": "Installed in Ollama"}
    return {"default": default, "models": list(out.values()), "ollama_running": state["running"]}


def pick_speaker_model(preferred: str | None = None, state: dict | None = None) -> str | None:
    """An installed local model for reading the transcript: `preferred`, else the recommended
    one (models.json speaker_default), else any local chat model."""
    state = state if state is not None else ollama_state()
    for candidate in ([preferred] if preferred else []) + [load_registry().get("speaker_default", "")]:
        name = _installed_name(candidate, state["installed"]) if candidate else None
        if name:
            return name
    return pick_local_model(state=state)


def unload_local(model: str) -> None:
    """Free a local model's memory now (before the next model or a transcription loads)."""
    try:
        requests.post(f"{llm_backends.LOCAL_LLM_BASE_URL}/api/generate", json={"model": model, "keep_alive": 0},
                      timeout=30)
    except requests.RequestException:
        pass


def delete_local_model(model: str) -> None:
    """Deletes a model from Ollama (frees its disk space)."""
    response = requests.delete(f"{llm_backends.LOCAL_LLM_BASE_URL}/api/delete",
                               json={"model": model}, timeout=30)
    if response.status_code >= 400:
        raise RuntimeError(response.json().get("error", response.text) if response.content else "delete failed")


# ---------------------------------------------------------------------------
# Downloads (ollama pull) with progress and cancel
# ---------------------------------------------------------------------------

class PullManager:
    """One background download per model; progress is polled by the UI."""

    def __init__(self):
        """No downloads yet; a lock protects `_pulls` because downloads run in threads."""
        self._pulls: dict[str, dict] = {}
        self._lock = threading.Lock()

    def snapshot(self) -> dict[str, dict]:
        """The state of every download for the browser (internal fields starting with '_' are left out)."""
        with self._lock:
            return {k: {kk: vv for kk, vv in v.items() if not kk.startswith("_")}
                    for k, v in self._pulls.items()}

    def get(self, model: str) -> dict | None:
        """The state of one model's download, or None."""
        return self.snapshot().get(model)

    def start(self, model: str) -> dict:
        """Starts downloading a model in a background thread (or returns the running download)."""
        with self._lock:
            current = self._pulls.get(model)
            if current and current["state"] == "running":
                return {k: v for k, v in current.items() if not k.startswith("_")}
            entry = {"model": model, "state": "running", "status": "starting", "completed": 0,
                     "total": 0, "percent": 0.0, "error": "", "started": time.time(),
                     "_cancel": threading.Event(), "_layers": {}}
            self._pulls[model] = entry
        threading.Thread(target=self._run, args=(entry,), daemon=True).start()
        return self.get(model)

    def cancel(self, model: str) -> dict | None:
        """Asks a running download to stop; it stops at the next progress update."""
        with self._lock:
            entry = self._pulls.get(model)
            if entry and entry["state"] == "running":
                entry["_cancel"].set()
        return self.get(model)

    def _update(self, entry: dict, **values) -> None:
        """Updates a download's state under the lock."""
        with self._lock:
            entry.update(values)

    def _run(self, entry: dict) -> None:
        """The download thread: streams Ollama's /api/pull progress, adds up the size of every layer for the
        percentage, and handles cancel and errors.
        """
        url = f"{llm_backends.LOCAL_LLM_BASE_URL}/api/pull"
        try:
            # No overall timeout: ~10 GB takes a while. The read timeout only bounds silent gaps.
            with requests.post(url, json={"model": entry["model"], "stream": True}, stream=True,
                               timeout=(5, 600)) as response:
                if response.status_code >= 400:
                    raise RuntimeError(response.text[:300])
                for line in response.iter_lines(decode_unicode=True):
                    if entry["_cancel"].is_set():
                        self._update(entry, state="cancelled", status="cancelled")
                        return
                    if not line:
                        continue
                    event = json.loads(line)
                    if event.get("error"):
                        raise RuntimeError(event["error"])
                    layers = entry["_layers"]
                    if event.get("digest") and event.get("total"):
                        layers[event["digest"]] = (int(event.get("completed") or 0), int(event["total"]))
                    done = sum(c for c, _ in layers.values())
                    total = sum(t for _, t in layers.values())
                    self._update(entry, status=event.get("status", ""), completed=done, total=total,
                                 percent=round(100 * done / total, 1) if total else 0.0)
                    if event.get("status") == "success":
                        self._update(entry, state="done", percent=100.0)
                        return
            if entry["state"] == "running":
                self._update(entry, state="error", error="Download ended unexpectedly.")
        except requests.ConnectionError:
            self._update(entry, state="error",
                         error="Ollama is not running. Start it with `ollama serve` and try again.")
        except (requests.RequestException, RuntimeError, ValueError) as exc:
            self._update(entry, state="error", error=str(exc)[:300])


PULLS = PullManager()
