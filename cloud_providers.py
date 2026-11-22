#!/usr/bin/env python3
"""Cloud chat providers besides GAIA: Anthropic (Claude), OpenAI, and any OpenAI-compatible
endpoint (Azure OpenAI, OpenRouter, Groq, …). Added and managed in Settings → Models.

  * Anthropic goes through the official `anthropic` SDK (Messages API, streaming).
  * OpenAI and OpenAI-compatible endpoints use the Chat Completions API over HTTP.
  * GAIA (GLM-5.3-Flash) stays the built-in provider in model_registry/llm_backends and is
    untouched by this module.

Config: CONFIG_FILE (APP_DATA_DIR/cloud_providers.json)
  {"providers": [{"id", "kind", "label", "base_url", "auth_header", "models": [{"id", "label"}],
                  "enabled": [model ids shown in the picker], "manual": [typed model ids],
                  "added_at", "tested_at", "last_error"}]}
API keys live in secrets_store (macOS Keychain) as "provider_<id>_api_key", never in the config,
preferences, logs or exports. Model ids in the app: "cloud:<provider id>:<model>".

Only role and content are sent (llm_backends.clean_messages); no tool schemas, no app metadata.
"""

from __future__ import annotations

import json
import os
import re
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterator

import requests

import llm_backends
import secrets_store

CONFIG_FILE = Path(os.environ.get("CLOUD_PROVIDERS_FILE",
                                  str(Path(os.environ.get("APP_DATA_DIR", "./data")) / "cloud_providers.json")))
_LOCK = threading.RLock()

KINDS = {
    "anthropic": {"label": "Anthropic (Claude)", "base_url": "https://api.anthropic.com", "env": "ANTHROPIC_PROVIDER_URL",
                  "default_models": ["claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-5-5"]},
    "openai": {"label": "OpenAI (ChatGPT models)", "base_url": "https://api.openai.com/v1", "env": "OPENAI_PROVIDER_URL",
               "default_models": []},
    "openai_compatible": {"label": "OpenAI-compatible endpoint", "base_url": "", "env": "", "default_models": []},
}
STREAM_MAX_TOKENS = 64_000     # Anthropic requires max_tokens; streaming has room
COMPLETE_MAX_TOKENS = 16_000   # non-streaming: stays under HTTP timeouts
# Claude models that support server-side refusal fallbacks (see the claude-api reference):
# if one declines on policy grounds, the API re-runs the request on a fallback Claude model.
FALLBACK_BETA = "server-side-fallback-2026-07-01"
FALLBACK_MODELS = {"claude-fable-5-2", "claude-fable-5-1", "claude-opus-5-5", "claude-opus-5", "claude-sonnet-5-5"}
REFUSAL_NOTE = "(Claude declined to answer this request.)"
_NOT_CHAT = re.compile(r"embed|whisper|tts|dall-e|image|moderation|audio|realtime|transcribe|search|davinci|babbage|"
                       r"computer-use|codex-mini|omni-moderation", re.I)


class CloudProviderError(requests.RequestException):
    """A cloud provider call failed. kind: missing_key | invalid_key | rate_limited | offline |
    unknown_model | overloaded | error. Never falls back to another provider."""

    def __init__(self, kind: str, message: str, provider: str = ""):
        """Stores why the call failed (`kind`) and which provider, next to the message."""
        super().__init__(message)
        self.kind = kind
        self.provider = provider


def _now() -> str:
    """The current local time as an ISO string."""
    return datetime.now().isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

def load() -> dict:
    """Reads cloud_providers.json (provider settings, never keys); empty when it doesn't exist yet."""
    try:
        data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    data.setdefault("providers", [])
    return data


def save(data: dict) -> None:
    """Writes cloud_providers.json safely (temporary file, then rename)."""
    CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = CONFIG_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(CONFIG_FILE)


def key_name(provider_id: str) -> str:
    """The secrets_store name of a provider's API key, e.g. 'provider_ab12_api_key'."""
    return f"provider_{provider_id}_api_key"


def get_config(provider_id: str) -> dict | None:
    """The saved settings of one provider, or None."""
    return next((p for p in load()["providers"] if p["id"] == provider_id), None)


def base_url(config: dict) -> str:
    """The endpoint; tests point the Anthropic/OpenAI providers at fakes through env."""
    kind = KINDS[config["kind"]]
    override = os.environ.get(kind["env"]) if kind["env"] else ""
    return (override or config.get("base_url") or kind["base_url"]).rstrip("/")


def public(config: dict) -> dict:
    """A provider as shown in Settings → Models: its models, which are switched on, and whether a key is stored
    (only its last 4 characters, never the key itself).
    """
    key = secrets_store.get(key_name(config["id"]))
    models = list(config.get("models") or [])
    known = {m["id"] for m in models}
    models += [{"id": m, "label": m, "manual": True} for m in config.get("manual") or [] if m not in known]
    return {"id": config["id"], "kind": config["kind"], "label": config["label"], "base_url": config.get("base_url", ""),
            "default_base_url": KINDS[config["kind"]]["base_url"], "auth_header": config.get("auth_header", "bearer"),
            "has_key": bool(key), "key_hint": secrets_store.masked(key), "models": models,
            "enabled": list(config.get("enabled") or []), "added_at": config.get("added_at", ""),
            "tested_at": config.get("tested_at", ""), "last_error": config.get("last_error", "")}


def list_providers() -> list[dict]:
    """All added providers, in their public form."""
    return [public(p) for p in load()["providers"]]


def add_provider(kind: str, label: str = "", url: str = "", auth_header: str = "bearer", key: str = "") -> dict:
    """Adds a cloud provider (Anthropic, OpenAI or an OpenAI-compatible endpoint) and stores its key in the
    Keychain.

    For compatible endpoints the URL must be https (http is only allowed for this Mac).
    """
    if kind not in KINDS:
        raise ValueError(f"Unknown provider type: {kind}")
    url = url.strip().rstrip("/")
    if kind == "openai_compatible":
        if not re.match(r"^https?://", url):
            raise ValueError("Give the endpoint's base URL, e.g. https://openrouter.ai/api/v1")
        if url.startswith("http://") and not re.match(r"^http://(localhost|127\.0\.0\.1)(:|/|$)", url):
            raise ValueError("Use https:// for a remote endpoint (http:// is only allowed for localhost).")
    with _LOCK:
        data = load()
        if kind in ("anthropic", "openai"):
            pid = kind
        else:
            base = re.sub(r"[^a-z0-9]+", "-", (label or url.split("//", 1)[-1]).lower()).strip("-")[:30] or "endpoint"
            pid, n = f"compat-{base}", 2
            while any(p["id"] == pid for p in data["providers"]):
                pid, n = f"compat-{base}-{n}", n + 1
        existing = next((p for p in data["providers"] if p["id"] == pid), None)
        if existing is None:
            existing = {"id": pid, "kind": kind, "models": [], "enabled": [], "manual": [], "added_at": _now()}
            data["providers"].append(existing)
        existing["label"] = label.strip() or KINDS[kind]["label"]
        existing["base_url"] = url if kind == "openai_compatible" else ""
        existing["auth_header"] = auth_header if auth_header in ("bearer", "api-key") else "bearer"
        save(data)
    if key.strip():
        secrets_store.set(key_name(pid), key.strip())
    return public(existing)


def update_provider(provider_id: str, fields: dict) -> dict | None:
    """Changes a provider's label, switched-on models, manual model names, URL, auth header or key. None if the
    provider doesn't exist.
    """
    with _LOCK:
        data = load()
        config = next((p for p in data["providers"] if p["id"] == provider_id), None)
        if config is None:
            return None
        if isinstance(fields.get("label"), str) and fields["label"].strip():
            config["label"] = fields["label"].strip()
        if isinstance(fields.get("enabled"), list):
            config["enabled"] = [str(m) for m in fields["enabled"] if str(m).strip()]
        if isinstance(fields.get("manual_model"), str) and fields["manual_model"].strip():
            model = fields["manual_model"].strip()
            config["manual"] = list(dict.fromkeys((config.get("manual") or []) + [model]))
            config["enabled"] = list(dict.fromkeys((config.get("enabled") or []) + [model]))
        if isinstance(fields.get("remove_model"), str):
            config["manual"] = [m for m in config.get("manual") or [] if m != fields["remove_model"]]
            config["enabled"] = [m for m in config.get("enabled") or [] if m != fields["remove_model"]]
        if config["kind"] == "openai_compatible" and isinstance(fields.get("base_url"), str) and fields["base_url"].strip():
            config["base_url"] = fields["base_url"].strip().rstrip("/")
        if fields.get("auth_header") in ("bearer", "api-key"):
            config["auth_header"] = fields["auth_header"]
        save(data)
    if isinstance(fields.get("key"), str) and fields["key"].strip():
        secrets_store.set(key_name(provider_id), fields["key"].strip())
    return public(config)


def remove_provider(provider_id: str) -> bool:
    """Removes a provider and deletes its stored API key. True if it existed."""
    with _LOCK:
        data = load()
        before = len(data["providers"])
        data["providers"] = [p for p in data["providers"] if p["id"] != provider_id]
        save(data)
    secrets_store.delete(key_name(provider_id))
    return len(data["providers"]) < before


def _record(provider_id: str, **values) -> None:
    """Saves extra fields on a provider (e.g. when it was last tested and the last error)."""
    with _LOCK:
        data = load()
        for config in data["providers"]:
            if config["id"] == provider_id:
                config.update(values)
        save(data)


# ---------------------------------------------------------------------------
# Talking to the providers
# ---------------------------------------------------------------------------

def _key(config: dict) -> str:
    """The provider's API key; raises a 'missing_key' error that tells the user where to add it."""
    key = secrets_store.get(key_name(config["id"]))
    if not key:
        raise CloudProviderError("missing_key", f"No API key for {config['label']}. Add one in Settings → Models.",
                                 config["id"])
    return key


@contextmanager
def _anthropic_errors(config: dict):
    """Translates Anthropic SDK exceptions into CloudProviderError with a readable message (bad key, unknown
    model, rate limit, overloaded, offline).
    """
    import anthropic

    label = config["label"]
    try:
        yield
    except (anthropic.AuthenticationError, anthropic.PermissionDeniedError) as exc:
        raise CloudProviderError("invalid_key", f"{label} rejected the API key ({exc.status_code}).", config["id"]) from exc
    except anthropic.NotFoundError as exc:
        raise CloudProviderError("unknown_model", f"{label}: model not found. Pick another in Settings → Models.",
                                 config["id"]) from exc
    except anthropic.RateLimitError as exc:
        wait = exc.response.headers.get("retry-after", "") if exc.response is not None else ""
        raise CloudProviderError("rate_limited", f"{label} rate limit reached" + (f"; try again in {wait} s." if wait else "."),
                                 config["id"]) from exc
    except anthropic.APIStatusError as exc:
        if exc.status_code in (529, 503) or isinstance(exc, getattr(anthropic, "OverloadedError", ())):
            raise CloudProviderError("overloaded", f"{label} is overloaded right now. Try again shortly.",
                                     config["id"]) from exc
        raise CloudProviderError("error", f"{label} error {exc.status_code}: {str(exc.message)[:200]}",
                                 config["id"]) from exc
    except (anthropic.APIConnectionError, anthropic.APITimeoutError) as exc:
        raise CloudProviderError("offline", f"Can't reach {label}. Check the internet connection.", config["id"]) from exc


def _anthropic_client(config: dict):
    """An Anthropic SDK client for this provider (one retry, long read timeout)."""
    import anthropic

    # The key is always passed explicitly: no environment variable or CLI profile is used.
    return anthropic.Anthropic(api_key=_key(config), base_url=base_url(config), max_retries=1,
                               timeout=anthropic.Timeout(600.0, connect=10.0))


def to_anthropic(messages: list[dict]) -> tuple[str, list[dict]]:
    """(system, messages) for the Messages API: system text goes to `system`, roles alternate,
    the conversation starts with the user and ends with the user (prefill isn't supported)."""
    system, out = [], []
    for message in llm_backends.clean_messages(messages):
        content = str(message["content"] or "").strip() or "(empty)"
        if message["role"] == "system":
            system.append(content)
            continue
        role = "assistant" if message["role"] == "assistant" else "user"
        if out and out[-1]["role"] == role:
            out[-1]["content"] += "\n\n" + content
        else:
            out.append({"role": role, "content": content})
    if not out or out[0]["role"] != "user":
        out.insert(0, {"role": "user", "content": "(Start of the conversation.)"})
    if out[-1]["role"] != "user":
        out.append({"role": "user", "content": "Continue."})
    return "\n\n".join(system), out


def _anthropic_request(model: str, messages: list[dict], max_tokens: int) -> dict:
    """The Messages API request: model, max_tokens, messages and system prompt; models in FALLBACK_MODELS also
    get server-side fallbacks if the model refuses.
    """
    system, msgs = to_anthropic(messages)
    request = {"model": model, "max_tokens": max_tokens, "messages": msgs}
    if system:
        request["system"] = system
    if model in FALLBACK_MODELS:
        request.update(betas=[FALLBACK_BETA], fallbacks="default")
    return request


def _openai_headers(config: dict) -> dict:
    """Auth headers for OpenAI-style APIs: 'Authorization: Bearer <key>', or 'api-key: <key>' for Azure-style
    endpoints.
    """
    key = _key(config)
    if config.get("auth_header") == "api-key":
        return {"api-key": key, "Content-Type": "application/json"}
    return {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}


def _openai_error(config: dict, response: requests.Response) -> CloudProviderError:
    """Translates an OpenAI-style error reply into CloudProviderError (bad key, unknown model, rate limit,
    overloaded or other).
    """
    label, code = config["label"], response.status_code
    try:
        detail = response.json().get("error", {})
        detail = detail.get("message", "") if isinstance(detail, dict) else str(detail)
    except ValueError:
        detail = response.text[:200]
    if code in (401, 403):
        return CloudProviderError("invalid_key", f"{label} rejected the API key ({code}).", config["id"])
    if code == 404 or "model" in detail.lower() and "not" in detail.lower() and "found" in detail.lower():
        return CloudProviderError("unknown_model", f"{label}: model not found. Pick another in Settings → Models.",
                                  config["id"])
    if code == 429:
        return CloudProviderError("rate_limited", f"{label} rate limit reached. {detail[:120]}".strip(), config["id"])
    if code in (502, 503, 529):
        return CloudProviderError("overloaded", f"{label} is overloaded right now. Try again shortly.", config["id"])
    return CloudProviderError("error", f"{label} error {code}: {detail[:200]}", config["id"])


def _openai_post(config: dict, payload: dict, stream: bool) -> requests.Response:
    """POSTs to /chat/completions; network problems and error replies become CloudProviderError."""
    try:
        response = requests.post(f"{base_url(config)}/chat/completions", headers=_openai_headers(config),
                                 json=payload, stream=stream, timeout=(10, 300))
    except (requests.ConnectionError, requests.Timeout) as exc:
        raise CloudProviderError("offline", f"Can't reach {config['label']}. Check the internet connection.",
                                 config["id"]) from exc
    if response.status_code >= 400:
        raise _openai_error(config, response)
    return response


def list_remote_models(config: dict) -> list[dict]:
    """[{id, label}] from the provider's models endpoint (also checks the key)."""
    if config["kind"] == "anthropic":
        with _anthropic_errors(config):
            page = _anthropic_client(config).models.list(limit=100)
            return [{"id": m.id, "label": getattr(m, "display_name", "") or m.id} for m in page.data]
    try:
        response = requests.get(f"{base_url(config)}/models", headers=_openai_headers(config), timeout=20)
    except (requests.ConnectionError, requests.Timeout) as exc:
        raise CloudProviderError("offline", f"Can't reach {config['label']}.", config["id"]) from exc
    if response.status_code >= 400:
        raise _openai_error(config, response)
    rows = response.json().get("data") or []
    ids = sorted({str(r.get("id")) for r in rows if isinstance(r, dict) and r.get("id")})
    if config["kind"] == "openai":
        ids = [m for m in ids if not _NOT_CHAT.search(m)]
    return [{"id": m, "label": m} for m in ids]


def test_provider(provider_id: str) -> dict:
    """Check the key by listing models; the list is saved for the model checkboxes."""
    config = get_config(provider_id)
    if config is None:
        raise KeyError(provider_id)
    try:
        models = list_remote_models(config)
    except CloudProviderError as exc:
        _record(provider_id, last_error=exc.kind, tested_at=_now())
        return {"ok": False, "kind": exc.kind, "message": str(exc), "provider": public(get_config(provider_id))}
    enabled = config.get("enabled") or [m for m in KINDS[config["kind"]]["default_models"]
                                        if m in {x["id"] for x in models}][:3]
    _record(provider_id, models=models, enabled=enabled, last_error="", tested_at=_now())
    return {"ok": True, "count": len(models), "provider": public(get_config(provider_id))}


# ---------------------------------------------------------------------------
# Providers (the ModelProvider interface in model_registry)
# ---------------------------------------------------------------------------

def model_id(provider_id: str, model: str) -> str:
    """The app's id for a provider's model: 'cloud:<provider id>:<model>'."""
    return f"cloud:{provider_id}:{model}"


def parse_model_id(value: str) -> tuple[str, str] | None:
    """Splits 'cloud:<provider>:<model>' into (provider, model); None for anything else (GAIA has its own fixed
    id 'cloud:gaia').
    """
    match = re.match(r"^cloud:([a-z0-9-]+):(.+)$", value or "")
    return (match.group(1), match.group(2)) if match and match.group(1) != "gaia" else None


@dataclass
class CloudChatProvider:
    """A model from an added cloud provider. Requests contain only the conversation text."""
    provider_id: str
    model: str

    @property
    def config(self) -> dict:
        """This provider's saved settings; an error if it was removed meanwhile."""
        config = get_config(self.provider_id)
        if config is None:
            raise CloudProviderError("error", f"The provider for {self.model} was removed.", self.provider_id)
        return config

    @property
    def model_id(self) -> str:
        """The app's id of this model ('cloud:<provider>:<model>')."""
        return model_id(self.provider_id, self.model)

    @property
    def backend(self) -> llm_backends.Backend:
        """A cloud Backend for this model (used for labels and routing checks)."""
        return llm_backends.Backend(llm_backends.CLOUD, self.model)

    @property
    def is_local(self) -> bool:
        """Always False: these models run in the cloud."""
        return False

    @property
    def provider_key(self) -> str:
        """Key for per-provider approvals and notices (the provider id)."""
        return self.provider_id

    @property
    def provider_label(self) -> str:
        """The provider's name as the user labelled it."""
        config = get_config(self.provider_id)
        return config["label"] if config else self.provider_id

    def stream(self, messages: list[dict]) -> Iterator[str]:
        """Streams the reply: through the Anthropic SDK for Claude, or Server-Sent Events from /chat/completions
        for OpenAI-style APIs.
        """
        config = self.config
        if config["kind"] == "anthropic":
            yield from self._anthropic_stream(config, messages)
            return
        response = _openai_post(config, {"model": self.model, "messages": llm_backends.clean_messages(messages),
                                         "stream": True}, stream=True)
        try:
            for raw in response.iter_lines(decode_unicode=True):
                if not raw or not raw.startswith("data:"):
                    continue
                data = raw[5:].strip()
                if data == "[DONE]":
                    break
                try:
                    event = json.loads(data)
                except ValueError:
                    continue
                if event.get("error"):
                    raise CloudProviderError("error", f"{config['label']}: {str(event['error'])[:200]}", config["id"])
                for choice in event.get("choices") or []:
                    text = (choice.get("delta") or {}).get("content")
                    if text:
                        yield text
        except (requests.ConnectionError, requests.Timeout) as exc:
            raise CloudProviderError("offline", f"Lost the connection to {config['label']}.", config["id"]) from exc

    def _anthropic_stream(self, config: dict, messages: list[dict]) -> Iterator[str]:
        """Streams a Claude reply and, if Claude declined the request, appends a short note explaining why
        there's no answer.
        """
        request = _anthropic_request(self.model, messages, STREAM_MAX_TOKENS)
        with _anthropic_errors(config):
            client = _anthropic_client(config)
            with client.beta.messages.stream(**request) as stream:
                for text in stream.text_stream:
                    yield text
                final = stream.get_final_message()
            if final.stop_reason == "refusal":
                yield ("\n\n" if final.content else "") + REFUSAL_NOTE

    def complete(self, messages: list[dict], max_tokens: int | None = None, temperature: float | None = None,
                 payload_order: str | None = None) -> str:
        """Whole reply. `temperature` is not sent (current Claude models reject sampling settings;
        OpenAI reasoning models do too); `max_tokens` only where the API requires it (Anthropic)."""
        config = self.config
        if config["kind"] == "anthropic":
            request = _anthropic_request(self.model, messages, max(1024, min(max_tokens or COMPLETE_MAX_TOKENS,
                                                                             COMPLETE_MAX_TOKENS)))
            with _anthropic_errors(config):
                message = _anthropic_client(config).beta.messages.create(**request)
            text = "".join(block.text for block in message.content if getattr(block, "type", "") == "text")
            return text if message.stop_reason != "refusal" else (text + "\n\n" + REFUSAL_NOTE).strip()
        response = _openai_post(config, {"model": self.model, "messages": llm_backends.clean_messages(messages)},
                                stream=False)
        try:
            return response.json()["choices"][0]["message"].get("content") or ""
        except (ValueError, KeyError, IndexError) as exc:
            raise CloudProviderError("error", f"{config['label']} sent an unexpected reply.", config["id"]) from exc


def registry_entries() -> list[dict]:
    """Enabled models of every added provider, as model_registry entries."""
    out = []
    for config in load()["providers"]:
        labels = {m["id"]: m.get("label") or m["id"] for m in config.get("models") or []}
        ready = bool(secrets_store.get(key_name(config["id"])))
        for model in config.get("enabled") or []:
            out.append({"id": model_id(config["id"], model), "provider": "cloud", "provider_id": config["id"],
                        "provider_kind": config["kind"], "model": model, "label": labels.get(model, model),
                        "group": "cloud", "description": f"{config['label']}. Sends content off this Mac.",
                        "uses": ["chat", "summary", "speakers"], "status": "ready" if ready else "not_configured"})
    return out


def provider_for(entry: dict) -> CloudChatProvider:
    """The provider object for a registry entry of a cloud model."""
    return CloudChatProvider(entry["provider_id"], entry["model"])
