#!/usr/bin/env python3
"""Chat-model backends: cloud (Globant GAIA, unchanged) and local (Ollama).

Cloud requests are byte-for-byte what the original Streamlit app sent before this module existed
(see tests/test_cloud_regression.py): same env vars and defaults, same
OpenAI-compatible endpoint, headers, payload and timeouts.

Local requests go to Ollama's native /api/chat on this machine, so nothing
leaves the laptop. The native API lets us switch "thinking" off for speed and
gives clear errors when Ollama or a model is missing.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass

import requests

# --- Cloud: Globant GAIA (unchanged) -----------------------------------------
GAIA_BASE_URL = os.environ.get("GAIA_BASE_URL", "https://api.clients.globant.com").rstrip("/")
GAIA_API_KEY = os.environ.get("GAIA_API_KEY", "")
GAIA_MODEL = os.environ.get("GAIA_MODEL", "globant_dgx/GLM-5.3-Flash")
GAIA_ENDPOINT = os.environ.get("GAIA_ENDPOINT", f"{GAIA_BASE_URL}/v1/chat/completions")

# --- Local: Ollama ------------------------------------------------------------
LOCAL_LLM_BASE_URL = os.environ.get("LOCAL_LLM_BASE_URL", "http://localhost:11434").rstrip("/")
LOCAL_LLM_MODEL = os.environ.get("LOCAL_LLM_MODEL", "qwen3:8b")
# Helper calls (memory extraction) reuse the selected chat model by default: on a
# 16 GB Mac, swapping models costs more than it saves. Set to force another model.
LOCAL_BACKGROUND_MODEL = os.environ.get("LOCAL_BACKGROUND_MODEL", "")
BACKGROUND_MAX_TOKENS = int(os.environ.get("LOCAL_BACKGROUND_MAX_TOKENS", "300"))
LOCAL_LLM_THINK = os.environ.get("LOCAL_LLM_THINK", "false").lower() == "true"
# The context window (tokens) the app plans with for local models. Sent as num_ctx with every request, because
# Ollama's own default when OLLAMA_CONTEXT_LENGTH isn't set (4096 on a 16 GB Mac) is smaller than a long
# transcript, and Ollama drops what doesn't fit without an error. Ollama caps it at the model's own maximum.
LOCAL_CONTEXT_LENGTH = int(os.environ.get("OLLAMA_CONTEXT_LENGTH", "32768"))
# Read timeout (seconds without any bytes). Local calls stream, so this only
# bounds the silent "reading the prompt" phase, which is slow for long transcripts.
LOCAL_TIMEOUT = int(os.environ.get("LOCAL_LLM_TIMEOUT", "3600"))

# Speeds actually measured on this machine (from Ollama's timings), per model:
# {"qwen3:8b": {"prefill_tps": 101.0, "gen_tps": 12.9}}. Feeds time estimates.
MEASURED_SPEEDS: dict[str, dict[str, float]] = {}


def record_speeds(model: str, chunk: dict) -> None:
    """Fold Ollama's final-chunk timings into MEASURED_SPEEDS (moving average)."""
    def rate(count_key, dur_key):
        """Tokens per second from Ollama's count and duration (nanoseconds); None for tiny samples that would
        give a misleading speed.
        """
        count, dur = chunk.get(count_key) or 0, chunk.get(dur_key) or 0
        return count / (dur / 1e9) if count > 200 and dur > 0 else None  # ignore tiny/cached samples
    prefill = rate("prompt_eval_count", "prompt_eval_duration")
    gen = (chunk.get("eval_count") or 0) / ((chunk.get("eval_duration") or 0) / 1e9) \
        if (chunk.get("eval_count") or 0) > 20 and chunk.get("eval_duration") else None
    entry = MEASURED_SPEEDS.setdefault(model, {})
    for key, value in (("prefill_tps", prefill), ("gen_tps", gen)):
        if value:
            entry[key] = round(value if key not in entry else 0.5 * entry[key] + 0.5 * value, 1)

CLOUD = "cloud"
LOCAL = "local"


@dataclass(frozen=True)
class Backend:
    """Which model answers: `kind` is 'cloud' (GAIA) or 'local' (Ollama) and `model` its name.

    Frozen (immutable), so it can be shared safely between threads.
    """
    kind: str  # "cloud" | "local"
    model: str
    think: bool = False  # local only: request the model's reasoning mode

    @property
    def label(self) -> str:
        """A readable label such as 'Gemma 4 E4B · local'."""
        return f"{display_model_name(self.model)} · {self.kind}"


def cloud() -> Backend:
    """The cloud backend: GLM-5.3-Flash through Globant GAIA."""
    return Backend(CLOUD, GAIA_MODEL)


def local(model: str | None = None, think: bool = False) -> Backend:
    """A local Ollama backend for `model` (default LOCAL_LLM_MODEL). `think` asks for the model's reasoning mode,
    which is off by default because it's slow.
    """
    return Backend(LOCAL, model or LOCAL_LLM_MODEL, think)


def display_model_name(model: str) -> str:
    """'globant_dgx/GLM-5.3-Flash' -> 'GLM-5.3-Flash'; local names unchanged."""
    return model.rsplit("/", 1)[-1]


class LocalModelError(requests.RequestException):
    """Ollama unreachable or the model is missing - never falls back to cloud."""


def clean_messages(messages: list[dict]) -> list[dict]:
    """Only role/content go to a model; UI metadata (e.g. 'model') is dropped."""
    return [{"role": m["role"], "content": m["content"]} for m in messages]


# ---------------------------------------------------------------------------
# Cloud (GAIA) - identical requests to the original app (pinned by tests/test_cloud_regression.py)
# ---------------------------------------------------------------------------

def _gaia_headers() -> dict:
    """HTTP headers for GAIA: the API key as a Bearer token. (Pinned by tests/test_cloud_regression.py: don't
    change.)
    """
    return {"Authorization": f"Bearer {GAIA_API_KEY}", "Content-Type": "application/json"}


def _gaia_stream(messages: list[dict]):
    """Streams a GAIA reply piece by piece (Server-Sent Events: lines starting with 'data: ', ending with
    '[DONE]'). Yields the text of each piece.
    """
    payload = {"model": GAIA_MODEL, "messages": clean_messages(messages), "stream": True}
    with requests.post(GAIA_ENDPOINT, headers=_gaia_headers(), json=payload, stream=True,
                       timeout=120) as response:
        response.raise_for_status()
        for line in response.iter_lines(decode_unicode=True):
            if not line or not line.startswith("data: "):
                continue
            data = line[len("data: "):]
            if data.strip() == "[DONE]":
                break
            chunk = json.loads(data)
            delta = chunk.get("choices", [{}])[0].get("delta", {}).get("content")
            if delta:
                yield delta


def _gaia_complete(messages: list[dict], payload_order: str = "messages_first") -> str:
    """One non-streaming GAIA request; returns the whole reply text.

    `payload_order` keeps the exact key order of the original app's requests (the memory-extraction call put
    'stream' before 'messages').
    """
    if payload_order == "stream_first":
        payload = {"model": GAIA_MODEL, "stream": False, "messages": clean_messages(messages)}
    else:
        payload = {"model": GAIA_MODEL, "messages": clean_messages(messages), "stream": False}
    response = requests.post(GAIA_ENDPOINT, headers=_gaia_headers(), json=payload, timeout=60)
    response.raise_for_status()
    return response.json()["choices"][0]["message"]["content"]


# ---------------------------------------------------------------------------
# Local (Ollama native API)
# ---------------------------------------------------------------------------

def _ollama_payload(messages: list[dict], backend: Backend, stream: bool,
                    max_tokens: int | None = None, temperature: float | None = None) -> dict:
    """The JSON body for Ollama's /api/chat: model, messages, stream, the reasoning switch, the context window
    (num_ctx, always) and optional limits (num_predict = max tokens, temperature).
    """
    payload = {"model": backend.model, "messages": clean_messages(messages), "stream": stream}
    payload["think"] = bool(backend.think)
    options = {"num_ctx": LOCAL_CONTEXT_LENGTH}  # always explicit: never Ollama's smaller default window
    if max_tokens:
        options["num_predict"] = max_tokens
    if temperature is not None:
        options["temperature"] = temperature
    payload["options"] = options
    return payload


_NATIVE_CONTEXT: dict[str, int] = {}  # model -> its own maximum window (from /api/show; fixed per model)


def _loaded_window(model: str) -> int | None:
    """The context window `model` was loaded with, if Ollama has it in memory now (GET /api/ps); else None."""
    try:
        loaded = requests.get(f"{LOCAL_LLM_BASE_URL}/api/ps", timeout=2).json().get("models") or []
    except (requests.RequestException, ValueError, AttributeError):
        return None  # can't tell: the request goes ahead and Ollama reports any real problem
    for entry in loaded:
        if entry.get("name") in (model, f"{model}:latest") or entry.get("model") in (model, f"{model}:latest"):
            return entry.get("context_length")
    return None


def _native_context(model: str) -> int | None:
    """The model's own maximum window (cached); None if Ollama doesn't say."""
    if model not in _NATIVE_CONTEXT:
        try:
            info = requests.post(f"{LOCAL_LLM_BASE_URL}/api/show", json={"model": model}, timeout=6).json()
        except (requests.RequestException, ValueError):
            return None
        native = next((int(v) for k, v in (info.get("model_info") or {}).items() if k.endswith(".context_length")), None)
        if native is None:
            return None
        _NATIVE_CONTEXT[model] = native
    return _NATIVE_CONTEXT[model]


def ensure_window(model: str) -> None:
    """Makes sure the next request gets the full window. Ollama applies num_ctx only when it loads a model: one
    already in memory with a smaller window (loaded by `ollama run`, the Ollama app or another client) keeps it
    and silently drops the start of long prompts. Such a model is unloaded, so this request reloads it with
    num_ctx.
    """
    loaded = _loaded_window(model)
    if not loaded or loaded >= LOCAL_CONTEXT_LENGTH:
        return
    native = _native_context(model)
    if native is not None and loaded >= min(native, LOCAL_CONTEXT_LENGTH):
        return  # already as big as this model allows
    try:
        requests.post(f"{LOCAL_LLM_BASE_URL}/api/generate", json={"model": model, "keep_alive": 0}, timeout=30)
    except requests.RequestException:
        pass  # the request itself will report it if Ollama is down


def _ollama_error(exc: Exception, model: str) -> LocalModelError:
    """Turns a network problem into a LocalModelError with a clear fix (start Ollama, try again). Every message
    says nothing was sent to the cloud.
    """
    if isinstance(exc, requests.ConnectionError):
        return LocalModelError(
            f"Local model server (Ollama) is not reachable at {LOCAL_LLM_BASE_URL}. "
            "Start it with `scripts/start_local_ai.sh` (or `ollama serve`). "
            "Nothing was sent to the cloud."
        )
    if isinstance(exc, requests.Timeout):
        return LocalModelError(
            f"Local model `{model}` timed out (no reply for {LOCAL_TIMEOUT} s). It may still be "
            "loading or the machine is busy; try again or pick a smaller model. Nothing was sent to the cloud."
        )
    return LocalModelError(f"Local model `{model}` failed: {exc}. Nothing was sent to the cloud.")


def _ollama_post(payload: dict, stream: bool):
    """Sends the request to Ollama and returns the response. Turns HTTP errors into LocalModelError; retries once
    without reasoning mode if the model doesn't support it.
    """
    try:
        response = requests.post(f"{LOCAL_LLM_BASE_URL}/api/chat", json=payload, stream=stream,
                                 timeout=LOCAL_TIMEOUT)
    except requests.RequestException as exc:
        raise _ollama_error(exc, payload["model"]) from exc
    if response.status_code >= 400:
        detail = ""
        try:
            detail = response.json().get("error", "")
        except ValueError:
            detail = response.text[:200]
        if "does not support thinking" in detail and payload.get("think"):
            # Model has no reasoning mode: retry plainly.
            payload = dict(payload, think=False)
            return _ollama_post(payload, stream)
        if "not found" in detail:
            raise LocalModelError(
                f"Local model `{payload['model']}` is not installed. "
                f"Run `ollama pull {payload['model']}` or pick another model."
            )
        raise LocalModelError(f"Local model `{payload['model']}` error: {detail or response.status_code}")
    return response


class _ThinkStripper:
    """Remove <think>...</think> blocks from a token stream (models that inline them)."""

    def __init__(self):
        """Starts with an empty buffer, outside any <think> block."""
        self.buffer = ""
        self.inside = False

    def feed(self, text: str) -> str:
        """Takes the next streamed piece of text and returns what can be shown now.

        Text inside <think>…</think> is dropped. Because a tag can be split across two pieces, the last few
        characters are held back until the next piece arrives.
        """
        self.buffer += text
        out = []
        while self.buffer:
            if self.inside:
                end = self.buffer.find("</think>")
                if end == -1:
                    self.buffer = self.buffer[-8:]  # keep a possible partial closing tag
                    return "".join(out)
                self.buffer = self.buffer[end + len("</think>"):]
                self.inside = False
            else:
                start = self.buffer.find("<think>")
                if start == -1:
                    # hold back a possible partial "<think" at the end
                    keep = 0
                    for i in range(1, min(7, len(self.buffer)) + 1):
                        if "<think>".startswith(self.buffer[-i:]):
                            keep = i
                    out.append(self.buffer[: len(self.buffer) - keep])
                    self.buffer = self.buffer[len(self.buffer) - keep:]
                    return "".join(out)
                out.append(self.buffer[:start])
                self.buffer = self.buffer[start + len("<think>"):]
                self.inside = True
        return "".join(out)

    def flush(self) -> str:
        """Returns whatever was held back once the stream has ended."""
        rest = "" if self.inside else self.buffer
        self.buffer = ""
        return rest


def strip_think(text: str) -> str:
    """Removes <think>…</think> reasoning blocks from a finished reply."""
    return re.sub(r"<think>.*?</think>\s*", "", text, flags=re.DOTALL)


def _ollama_stream(messages: list[dict], backend: Backend, max_tokens: int | None = None,
                   temperature: float | None = None):
    """Streams a local model's reply (Ollama sends one JSON object per line), hides reasoning blocks and records
    the model's measured speed when the reply is done.
    """
    ensure_window(backend.model)
    response = _ollama_post(_ollama_payload(messages, backend, True, max_tokens, temperature), stream=True)
    stripper = _ThinkStripper()
    try:
        with response:
            for line in response.iter_lines(decode_unicode=True):
                if not line:
                    continue
                chunk = json.loads(line)
                if chunk.get("error"):
                    raise LocalModelError(f"Local model `{backend.model}` error: {chunk['error']}")
                text = stripper.feed(chunk.get("message", {}).get("content", ""))
                if text:
                    yield text
                if chunk.get("done"):
                    record_speeds(backend.model, chunk)
                    break
    except requests.RequestException as exc:
        if isinstance(exc, LocalModelError):
            raise
        raise _ollama_error(exc, backend.model) from exc
    tail = stripper.flush()
    if tail:
        yield tail


def _ollama_complete(messages: list[dict], backend: Backend, max_tokens: int | None = None,
                     temperature: float | None = None) -> str:
    # Streamed under the hood: the read timeout then applies between tokens,
    # so long generations never hit it, and timings are recorded.
    """The whole reply of a local model as one string (streamed internally, see the comment below)."""
    return strip_think("".join(_ollama_stream(messages, backend, max_tokens, temperature)))


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def stream_chat(messages: list[dict], backend: Backend):
    """Yield reply text chunks from the chosen backend."""
    if backend.kind == LOCAL:
        yield from _ollama_stream(messages, backend)
    else:
        yield from _gaia_stream(messages)


def complete(messages: list[dict], backend: Backend, payload_order: str = "messages_first",
             max_tokens: int | None = None, temperature: float | None = None) -> str:
    """Return a whole (non-streamed) reply from the chosen backend.

    max_tokens and temperature apply to local models only; cloud requests are unchanged.
    """
    if backend.kind == LOCAL:
        return _ollama_complete(messages, backend, max_tokens, temperature)
    return _gaia_complete(messages, payload_order)
