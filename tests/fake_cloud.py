"""Fake Anthropic (Messages API) and OpenAI (Chat Completions) endpoints for tests.

Used in-process by tests/test_cloud_providers.py (FakeCloud) and by the Playwright mock server
(web/e2e/mock_services.py, routes /anthropic/... and /openai/...). Never contacts the real APIs.

Keys: "sk-ant-test" (Anthropic), "sk-openai-test" (OpenAI). Special model names trigger errors:
  *-missing → 404, *-limit → 429, *-busy → 529 (Anthropic) / 503 (OpenAI), claude-refuse → refusal.
A request whose last user message asks for a speaker review ("review speaker identification")
gets a JSON review reply (see review_reply).
"""

from __future__ import annotations

import json
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ANTHROPIC_KEY = "sk-ant-test"
OPENAI_KEY = "sk-openai-test"
ANTHROPIC_MODELS = [("claude-opus-5-5", "Claude Opus 5.5"), ("claude-sonnet-5-5", "Claude Sonnet 5.5"),
                    ("claude-haiku-5-5", "Claude Haiku 5.5")]
OPENAI_MODELS = ["gpt-5.5", "gpt-5.5-mini", "text-embedding-3-small", "whisper-1"]
REQUESTS: list[dict] = []          # every request seen: {"api", "path", "headers", "body"}
REVIEW_PLAN: dict = {"changes": {}}  # {"SPEAKER_01": {"name", "role", "lines", "reason"}} for review replies


def _last_user(body: dict) -> str:
    """The text of the last user message in a request (string or content blocks)."""
    for message in reversed(body.get("messages") or []):
        if message.get("role") == "user":
            content = message.get("content")
            return content if isinstance(content, str) else json.dumps(content)
    return ""


def review_reply(body: dict) -> str | None:
    """A JSON speaker review when the request is one (follows REVIEW_PLAN)."""
    system = body.get("system") or next((m.get("content") for m in body.get("messages") or [] if m.get("role") == "system"), "")
    if "review speaker identification" not in str(system):
        return None
    prompt = _last_user(body)
    ids = re.findall(r'"id": "(SPEAKER_\d+)"', prompt)
    rows = []
    for spk in dict.fromkeys(ids):
        change = REVIEW_PLAN["changes"].get(spk)
        if change:
            rows.append({"id": spk, "action": "change", "name": change["name"], "role": change.get("role", ""),
                         "confidence": change.get("confidence", 0.8), "reason": change.get("reason", "Introduces themselves."),
                         "lines": change.get("lines", [])})
        else:
            rows.append({"id": spk, "action": "keep", "confidence": 0.7, "reason": "Fits the transcript."})
    return json.dumps({"speakers": rows})


def reply_text(body: dict) -> str:
    """What the fake model answers: a speaker-review reply when asked for one (see REVIEW_PLAN), otherwise 'Fake
    reply about: <question>'.
    """
    return review_reply(body) or f"Fake reply about: {_last_user(body)[:60]}"


class Handler(BaseHTTPRequestHandler):
    """Routes /v1/... (Anthropic) and /openai/v1/... (OpenAI). Mounted under a prefix by mock_services."""

    prefix = ""

    def log_message(self, *_args):
        """Silences the HTTP server's request log in test output."""
        pass

    def _send(self, status: int, payload: dict, headers: dict | None = None) -> None:
        """Sends a JSON response."""
        data = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def _body(self) -> dict:
        """The request's JSON body ({} if empty or invalid); also recorded in REQUESTS so tests can check what
        was sent.
        """
        length = int(self.headers.get("Content-Length") or 0)
        try:
            return json.loads(self.rfile.read(length) or b"{}")
        except ValueError:
            return {}

    def do_GET(self):  # noqa: N802
        """Routes GET requests to handle() (the path prefix selects Anthropic or OpenAI)."""
        handle(self, "GET", self.path[len(self.prefix):] if self.path.startswith(self.prefix) else self.path)

    def do_POST(self):  # noqa: N802
        """Routes POST requests to handle()."""
        handle(self, "POST", self.path[len(self.prefix):] if self.path.startswith(self.prefix) else self.path)


def handle(h, method: str, path: str) -> bool:
    """Serve one fake request on handler `h` (which has _send/_body). Returns False if not ours."""
    path = path.split("?", 1)[0]
    if path.startswith("/anthropic"):
        path = path[len("/anthropic"):]
    if path.startswith("/openai/"):
        return _openai(h, method, path[len("/openai"):])
    if path.startswith("/v1/"):
        return _anthropic(h, method, path)
    return False


def _sse(h, events: list[tuple[str, dict]] | list[str]) -> None:
    """Sends a Server-Sent Events response (the streaming format both APIs use)."""
    h.send_response(200)
    h.send_header("Content-Type", "text/event-stream")
    h.send_header("Cache-Control", "no-cache")
    h.end_headers()
    for item in events:
        if isinstance(item, tuple):
            name, data = item
            h.wfile.write(f"event: {name}\ndata: {json.dumps(data)}\n\n".encode())
        else:
            h.wfile.write(f"data: {item}\n\n".encode())
        h.wfile.flush()


def _anthropic_error(h, status: int, kind: str, message: str, headers: dict | None = None) -> bool:
    """Sends an error in Anthropic's format; returns True (request handled)."""
    h._send(status, {"type": "error", "error": {"type": kind, "message": message}}, headers)
    return True


def _anthropic(h, method: str, path: str) -> bool:
    """The fake Anthropic API: checks the key, lists models, and answers /v1/messages (streamed or not). Special
    model names trigger errors: '-missing' (404), '-limit' (429), '-busy' (overloaded), 'claude-refuse' (a
    refusal).
    """
    if h.headers.get("x-api-key") != ANTHROPIC_KEY and h.headers.get("Authorization") != f"Bearer {ANTHROPIC_KEY}":
        return _anthropic_error(h, 401, "authentication_error", "invalid x-api-key")
    if method == "GET" and path.startswith("/v1/models"):
        data = [{"type": "model", "id": m, "display_name": label, "created_at": "2026-10-01T00:00:00Z"}
                for m, label in ANTHROPIC_MODELS]
        h._send(200, {"data": data, "has_more": False, "first_id": data[0]["id"], "last_id": data[-1]["id"]})
        return True
    if method != "POST" or path != "/v1/messages":
        return _anthropic_error(h, 404, "not_found_error", "unknown path")
    body = h._body()
    REQUESTS.append({"api": "anthropic", "path": path, "headers": dict(h.headers), "body": body})
    model = body.get("model", "")
    if model.endswith("-missing"):
        return _anthropic_error(h, 404, "not_found_error", f"model: {model}")
    if model.endswith("-limit"):
        return _anthropic_error(h, 429, "rate_limit_error", "rate limited", {"retry-after": "0"})
    if model.endswith("-busy"):
        return _anthropic_error(h, 529, "overloaded_error", "Overloaded")
    if "max_tokens" not in body:
        return _anthropic_error(h, 400, "invalid_request_error", "max_tokens: Field required")
    refuse = model == "claude-refuse"
    text = "" if refuse else reply_text(body)
    stop = "refusal" if refuse else "end_turn"
    message = {"id": "msg_fake", "type": "message", "role": "assistant", "model": model, "content": [],
               "stop_reason": None, "stop_sequence": None, "usage": {"input_tokens": 10, "output_tokens": 0}}
    if not body.get("stream"):
        h._send(200, {**message, "content": [{"type": "text", "text": text}] if text else [], "stop_reason": stop,
                      "usage": {"input_tokens": 10, "output_tokens": len(text.split())}})
        return True
    events = [("message_start", {"type": "message_start", "message": message})]
    if text:
        events.append(("content_block_start", {"type": "content_block_start", "index": 0,
                                               "content_block": {"type": "text", "text": ""}}))
        for i in range(0, len(text), 12):
            events.append(("content_block_delta", {"type": "content_block_delta", "index": 0,
                                                   "delta": {"type": "text_delta", "text": text[i:i + 12]}}))
        events.append(("content_block_stop", {"type": "content_block_stop", "index": 0}))
    events += [("message_delta", {"type": "message_delta", "delta": {"stop_reason": stop, "stop_sequence": None},
                                  "usage": {"output_tokens": len(text.split())}}),
               ("message_stop", {"type": "message_stop"})]
    _sse(h, events)
    return True


def _openai(h, method: str, path: str) -> bool:
    """The fake OpenAI API: checks the key, lists models, and answers /chat/completions (streamed or not), with
    the same special model names for errors.
    """
    auth = h.headers.get("Authorization", "")
    if auth != f"Bearer {OPENAI_KEY}" and h.headers.get("api-key") != OPENAI_KEY:
        h._send(401, {"error": {"message": "Incorrect API key provided", "type": "invalid_request_error"}})
        return True
    if method == "GET" and path == "/v1/models":
        h._send(200, {"object": "list", "data": [{"id": m, "object": "model"} for m in OPENAI_MODELS]})
        return True
    if method != "POST" or path != "/v1/chat/completions":
        h._send(404, {"error": {"message": "unknown path"}})
        return True
    body = h._body()
    REQUESTS.append({"api": "openai", "path": path, "headers": dict(h.headers), "body": body})
    model = body.get("model", "")
    if model.endswith("-missing"):
        h._send(404, {"error": {"message": f"The model `{model}` does not exist", "code": "model_not_found"}})
        return True
    if model.endswith("-limit"):
        h._send(429, {"error": {"message": "Rate limit reached"}})
        return True
    if model.endswith("-busy"):
        h._send(503, {"error": {"message": "overloaded"}})
        return True
    text = reply_text(body)
    if not body.get("stream"):
        h._send(200, {"id": "chatcmpl-fake", "object": "chat.completion", "model": model,
                      "choices": [{"index": 0, "message": {"role": "assistant", "content": text}, "finish_reason": "stop"}]})
        return True
    chunks = [json.dumps({"choices": [{"index": 0, "delta": {"content": text[i:i + 12]}}]}) for i in range(0, len(text), 12)]
    _sse(h, chunks + [json.dumps({"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}), "[DONE]"])
    return True


class FakeCloud:
    """In-process server: `with FakeCloud() as url:` → base URL (Anthropic at url, OpenAI at url/openai/v1)."""

    def __enter__(self) -> str:
        """Starts the fake server on a free port with a clean request log; returns its URL."""
        REQUESTS.clear()
        REVIEW_PLAN["changes"] = {}
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        return f"http://127.0.0.1:{self.server.server_address[1]}"

    def __exit__(self, *_exc) -> None:
        """Stops the fake server."""
        self.server.shutdown()
        self.server.server_close()
