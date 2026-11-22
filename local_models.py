#!/usr/bin/env python3
"""Discover and describe the chat models installed in the local Ollama.

Any model you `ollama pull` appears in the app's picker automatically. Embedding
models (which cannot chat) are filtered out. Transcription models are separate
and never listed here.
"""

from __future__ import annotations

import os

import requests

from llm_backends import LOCAL_CONTEXT_LENGTH, LOCAL_LLM_BASE_URL, LOCAL_LLM_MODEL

# Context the Ollama server was started with (scripts/start_local_ai.sh).
SERVER_CONTEXT_LENGTH = LOCAL_CONTEXT_LENGTH  # the window the app plans with; also sent as num_ctx (llm_backends)
# Above this a model is likely to push a 16 GB Mac into swap (weights + cache + apps).
FIT_WARNING_BYTES = int(float(os.environ.get("LOCAL_MODEL_FIT_WARNING_GB", "8")) * 1e9)
EMBEDDING_FAMILIES = {"bert", "nomic-bert", "bert-moe"}


class OllamaUnavailable(RuntimeError):
    """Raised when Ollama (the local model server) can't be reached."""
    pass


def _get(path: str, timeout: float = 4):
    """GET a path from the Ollama server and return the JSON reply; raises OllamaUnavailable with a hint on how
    to start Ollama.
    """
    try:
        response = requests.get(f"{LOCAL_LLM_BASE_URL}{path}", timeout=timeout)
        response.raise_for_status()
        return response.json()
    except (requests.RequestException, ValueError) as exc:
        raise OllamaUnavailable(
            f"Ollama not reachable at {LOCAL_LLM_BASE_URL} — start it with "
            "`scripts/start_local_ai.sh` (or `ollama serve`)."
        ) from exc


def is_embedding_model(entry: dict) -> bool:
    """True for embedding-only models (e.g. nomic-embed-text): they turn text into vectors for search and can't
    chat, so the model picker hides them.
    """
    details = entry.get("details") or {}
    families = set(details.get("families") or []) | {details.get("family") or ""}
    return "embed" in entry.get("name", "").lower() or bool(families & EMBEDDING_FAMILIES)


def list_models() -> list[dict]:
    """Installed chat models, largest-capability first is not assumed: sorted by name."""
    data = _get("/api/tags")
    models = []
    for entry in data.get("models", []):
        if is_embedding_model(entry):
            continue
        details = entry.get("details") or {}
        size = int(entry.get("size") or 0)
        models.append({
            "name": entry["name"],
            "size_bytes": size,
            "parameter_size": details.get("parameter_size") or "?",
            "quantization": details.get("quantization_level") or "?",
            "family": details.get("family") or "",
            "too_big": size > FIT_WARNING_BYTES,
        })
    return sorted(models, key=lambda m: m["name"])


def label(model: dict) -> str:
    """'qwen3:8b · 8.2B · Q4_K_M · 5.2 GB' (+ fit warning)."""
    text = (f"{model['name']} · {model['parameter_size']} · {model['quantization']} · "
            f"{model['size_bytes'] / 1e9:.1f} GB")
    return text + ("  ⚠️ may be slow on 16 GB (swap)" if model["too_big"] else "")


def model_info(name: str) -> dict:
    """Native context length and capabilities (e.g. 'thinking') from /api/show."""
    try:
        response = requests.post(f"{LOCAL_LLM_BASE_URL}/api/show", json={"model": name}, timeout=6)
        response.raise_for_status()
        data = response.json()
    except (requests.RequestException, ValueError) as exc:
        raise OllamaUnavailable(f"Could not read details of local model `{name}`.") from exc
    native = None
    for key, value in (data.get("model_info") or {}).items():
        if key.endswith(".context_length"):
            native = int(value)
            break
    return {
        "native_context": native,
        "context_length": effective_context(native),
        "capabilities": data.get("capabilities") or [],
    }


def effective_context(native: int | None) -> int:
    """Smaller of the model's native window and what the server allocates."""
    return min(native, SERVER_CONTEXT_LENGTH) if native else SERVER_CONTEXT_LENGTH


