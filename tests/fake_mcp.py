"""Fake MCP world for tests: a registry (/v0/servers), remote MCP servers (Streamable HTTP with no
auth, an API key, or OAuth; and the older SSE transport) and an OAuth authorization server.
Used in-process (FakeMcp) and by web/e2e/mock_services.py under the /mcpfake prefix.

Remote servers offer two tools: echo(text) and add(a, b).
  /mcp           no auth          /mcp-key  needs "Authorization: Bearer key-123"
  /mcp-oauth     needs a token from the OAuth flow (401 + WWW-Authenticate otherwise)
  /sse           the HTTP+SSE transport (POST /messages?session=…)
"""

from __future__ import annotations

import base64
import hashlib
import json
import queue
import secrets
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlencode, urlparse

API_KEY = "key-123"
SEARCHES: list[dict] = []          # query parameters of every registry search
CALLS: list[dict] = []             # tools/call requests: {"path", "name", "arguments"}
STATE = {"codes": {}, "tokens": set(), "refresh": {}, "clients": set(), "sse": {}}
TOOLS = [{"name": "echo", "description": "Repeat the text back", "inputSchema": {"type": "object"}},
         {"name": "add", "description": "Add two numbers", "inputSchema": {"type": "object"}}]


def registry_servers(base: str) -> list[dict]:
    """What the fake registry lists (base = this server's URL, for the remotes)."""
    meta = {"io.modelcontextprotocol.registry/official": {"status": "active", "isLatest": True,
                                                          "publishedAt": "2026-10-01T00:00:00Z", "updatedAt": "2026-10-02T00:00:00Z"}}
    return [
        {"server": {"name": "io.github.example/echo-remote", "title": "Echo Remote", "version": "1.2.0",
                    "description": "Echo and add tools over Streamable HTTP (needs an API key).",
                    "repository": {"url": "https://github.com/example/echo-remote", "source": "github"},
                    "remotes": [{"type": "streamable-http", "url": f"{base}/mcp-key",
                                 "headers": [{"name": "Authorization", "description": "Bearer API key", "isRequired": True,
                                              "isSecret": True, "value": "Bearer {api_key}"}]}]}, "_meta": meta},
        {"server": {"name": "com.example/echo-oauth", "title": "Echo OAuth", "version": "0.9.0",
                    "description": "Echo tools that need you to sign in.",
                    "remotes": [{"type": "streamable-http", "url": f"{base}/mcp-oauth"}]}, "_meta": meta},
        {"server": {"name": "io.github.example/echo-local", "title": "Echo Local", "version": "2.0.1",
                    "description": "Echo tools as a local npm package.",
                    "repository": {"url": "https://github.com/example/echo-local", "source": "github"},
                    "packages": [{"registryType": "npm", "identifier": "@example/echo-mcp", "version": "2.0.1",
                                  "runtimeHint": "npx", "transport": {"type": "stdio"},
                                  "environmentVariables": [{"name": "ECHO_TOKEN", "description": "Token", "isRequired": True,
                                                            "isSecret": True},
                                                           {"name": "ECHO_MODE", "default": "loud"}]}]}, "_meta": meta},
        {"server": {"name": "io.github.example/docker-only", "title": "Docker Only", "version": "1.0.0",
                    "description": "Only a Docker image.",
                    "packages": [{"registryType": "oci", "identifier": "example/docker-only", "version": "1.0.0",
                                  "transport": {"type": "stdio"}}]}, "_meta": meta},
    ]


def _rpc_result(message: dict) -> dict | None:
    """The fake MCP server's answer to one JSON-RPC message (initialize, tools/list, tools/call…); None for
    notifications, which get no answer.
    """
    method, rid = message.get("method"), message.get("id")
    if rid is None:
        return None  # notification
    if method == "initialize":
        result = {"protocolVersion": message["params"].get("protocolVersion", "2025-06-18"), "capabilities": {"tools": {}},
                  "serverInfo": {"name": "fake-mcp", "version": "1.0"}}
    elif method == "tools/list":
        result = {"tools": TOOLS}
    elif method == "tools/call":
        name, args = message["params"].get("name"), message["params"].get("arguments") or {}
        if name == "echo":
            result = {"content": [{"type": "text", "text": f"echo: {args.get('text', args.get('query', ''))}"}]}
        elif name == "add":
            result = {"content": [{"type": "text", "text": str(float(args.get("a", 0)) + float(args.get("b", 0)))}]}
        else:
            return {"jsonrpc": "2.0", "id": rid, "error": {"code": -32602, "message": f"Unknown tool {name}"}}
    elif method == "ping":
        result = {}
    else:
        return {"jsonrpc": "2.0", "id": rid, "error": {"code": -32601, "message": "Method not found"}}
    return {"jsonrpc": "2.0", "id": rid, "result": result}


def _challenge(verifier: str) -> str:
    """The PKCE challenge for a verifier (base64url of its SHA-256), to check sign-in requests."""
    return base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")


def handle(h, method: str, raw_path: str, base: str) -> bool:
    """Serve one request on handler `h` (needs _send, _body_raw, send_response…). False if not ours."""
    parsed = urlparse(raw_path)
    path, query = parsed.path, parse_qs(parsed.query)
    if path == "/v0/servers" and method == "GET":
        SEARCHES.append({k: v[0] for k, v in query.items()})
        term = (query.get("search") or [""])[0].lower()
        if term == "offline-please":
            h._send(503, {"error": "unavailable"})
            return True
        if term == "slow-down":
            h._send(429, {"error": "rate limited"})
            return True
        found = [s for s in registry_servers(base) if term in json.dumps(s).lower()]
        h._send(200, {"servers": found, "metadata": {"count": len(found)}})
        return True
    if path == "/.well-known/oauth-protected-resource":
        h._send(200, {"resource": f"{base}/mcp-oauth", "authorization_servers": [base]})
        return True
    if path == "/.well-known/oauth-authorization-server":
        h._send(200, {"issuer": base, "authorization_endpoint": f"{base}/authorize", "token_endpoint": f"{base}/token",
                      "registration_endpoint": f"{base}/register", "code_challenge_methods_supported": ["S256"]})
        return True
    if path == "/register" and method == "POST":
        client = "client-" + secrets.token_hex(4)
        STATE["clients"].add(client)
        h._send(201, {"client_id": client})
        return True
    if path == "/authorize" and method == "GET":
        q = {k: v[0] for k, v in query.items()}
        code = "code-" + secrets.token_hex(4)
        STATE["codes"][code] = q
        h.send_response(302)
        h.send_header("Location", f"{q['redirect_uri']}?{urlencode({'code': code, 'state': q['state']})}")
        h.end_headers()
        return True
    if path == "/token" and method == "POST":
        form = {k: v[0] for k, v in parse_qs(h._body_raw().decode()).items()}
        if form.get("grant_type") == "authorization_code":
            issued = STATE["codes"].pop(form.get("code", ""), None)
            if not issued or _challenge(form.get("code_verifier", "")) != issued.get("code_challenge"):
                h._send(400, {"error": "invalid_grant"})
                return True
        elif form.get("grant_type") == "refresh_token":
            if form.get("refresh_token") not in STATE["refresh"]:
                h._send(400, {"error": "invalid_grant"})
                return True
        token, refresh = "tok-" + secrets.token_hex(4), "ref-" + secrets.token_hex(4)
        STATE["tokens"].add(token)
        STATE["refresh"][refresh] = token
        h._send(200, {"access_token": token, "refresh_token": refresh, "expires_in": 3600, "token_type": "Bearer"})
        return True
    if path in ("/mcp", "/mcp-key", "/mcp-oauth") and method == "POST":
        auth = h.headers.get("Authorization", "")
        if path == "/mcp-key" and auth != f"Bearer {API_KEY}":
            h._send(401, {"error": "missing key"})
            return True
        if path == "/mcp-oauth" and auth.removeprefix("Bearer ") not in STATE["tokens"]:
            h._send(401, {"error": "sign in"}, {"WWW-Authenticate":
                                                f'Bearer resource_metadata="{base}/.well-known/oauth-protected-resource"'})
            return True
        message = json.loads(h._body_raw() or b"{}")
        if message.get("method") == "tools/call":
            CALLS.append({"path": path, "name": message["params"]["name"], "arguments": message["params"].get("arguments")})
        reply = _rpc_result(message)
        if reply is None:
            h.send_response(202)
            h.send_header("Content-Length", "0")
            h.end_headers()
            return True
        headers = {"Mcp-Session-Id": "sess-1"} if message.get("method") == "initialize" else {}
        h._send(200, reply, headers)
        return True
    if path == "/sse" and method == "GET":
        session = secrets.token_hex(4)
        q: "queue.Queue[dict | None]" = queue.Queue()
        STATE["sse"][session] = q
        h.send_response(200)
        h.send_header("Content-Type", "text/event-stream")
        h.send_header("Cache-Control", "no-cache")
        h.end_headers()
        h.wfile.write(f"event: endpoint\ndata: /messages?session={session}\n\n".encode())
        h.wfile.flush()
        try:
            while True:
                item = q.get(timeout=30)
                if item is None:
                    break
                h.wfile.write(f"event: message\ndata: {json.dumps(item)}\n\n".encode())
                h.wfile.flush()
        except (queue.Empty, BrokenPipeError, ConnectionResetError):
            pass
        finally:
            STATE["sse"].pop(session, None)
        return True
    if path == "/messages" and method == "POST":
        q = STATE["sse"].get((query.get("session") or [""])[0])
        if q is None:
            h._send(404, {"error": "no session"})
            return True
        message = json.loads(h._body_raw() or b"{}")
        if message.get("method") == "tools/call":
            CALLS.append({"path": "/sse", "name": message["params"]["name"], "arguments": message["params"].get("arguments")})
        reply = _rpc_result(message)
        if reply is not None:
            q.put(reply)
        h.send_response(202)
        h.send_header("Content-Length", "0")
        h.end_headers()
        return True
    return False


class Handler(BaseHTTPRequestHandler):
    """HTTP handler of the fake MCP world: registry search, MCP servers (plain, with an API key, with OAuth, over
    SSE) and the OAuth server.
    """
    base = ""

    def log_message(self, *_args):
        """Silences the HTTP server's request log."""
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

    def _body_raw(self) -> bytes:
        """The raw request body."""
        return self.rfile.read(int(self.headers.get("Content-Length") or 0))

    def do_GET(self):  # noqa: N802
        """Routes GET requests to handle() (404 if nothing matches)."""
        if not handle(self, "GET", self.path, self.base):
            self._send(404, {"error": "not found"})

    def do_POST(self):  # noqa: N802
        """Routes POST requests to handle() (404 if nothing matches)."""
        if not handle(self, "POST", self.path, self.base):
            self._send(404, {"error": "not found"})


def reset() -> None:
    """Clears everything recorded between tests (searches, tool calls, tokens, sessions)."""
    SEARCHES.clear()
    CALLS.clear()
    for key in ("codes", "refresh", "sse"):
        STATE[key].clear()
    STATE["tokens"].clear()
    STATE["clients"].clear()


class FakeMcp:
    """Runs the fake MCP world in a thread for a test: `with FakeMcp() as url: …`."""
    def __enter__(self) -> str:
        """Starts the server on a free port; returns its URL."""
        reset()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        base = f"http://127.0.0.1:{self.server.server_address[1]}"
        Handler.base = base
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        return base

    def __exit__(self, *_exc) -> None:
        """Closes open SSE streams and stops the server."""
        for q in list(STATE["sse"].values()):
            q.put(None)
        self.server.shutdown()
        self.server.server_close()
