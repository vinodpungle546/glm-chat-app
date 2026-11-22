#!/usr/bin/env python3
"""Global tool/connectors registry shared by ALL projects.

Two kinds of connectors:
- "custom": Python-registered tools (built-ins or registered by code).
- "mcp":    MCP servers. Older entries (no "transport" field) are remote servers over Streamable
            HTTP called the original way: the connector's name is the tool name and no handshake
            is made. Connectors added since Settings → Connectors gained search have:
              transport  "http" (Streamable HTTP) | "sse" (HTTP + SSE) | "stdio" (a local package)
              auth       {"type": "none" | "header" | "oauth", "header": "Authorization"}
              tools      [{name, description}] from tools/list (the agent picks one per call)
              enabled    false = not offered to the agent
              source     {"type": "url"} or {"type": "directory", "directory", "server", "version",
                          "repository", "publisher"}
              stdio      {"registry_type": "npm" | "pypi", "identifier", "version", "command",
                          "args", "env": {non-secret values}, "secret_env": [names]}
Secrets (API keys, header values, secret environment variables, OAuth tokens) live in
secrets_store (macOS Keychain) as "connector_<id>_<name>", never in CONNECTORS_FILE.

Local packages (stdio) run with the user's permissions: they're installed only after an explicit
confirm, with a pinned version, started on first use, stopped after IDLE_SECONDS without use,
when disabled or removed, and when the API stops.

State is persisted in CONNECTORS_FILE (default APP_DATA_DIR/connectors/connectors.json)
so added MCP servers are available to every project and survive restarts.
"""

from __future__ import annotations

import atexit
import base64
import hashlib
import json
import os
import queue
import re
import secrets as pysecrets
import shlex
import shutil
import subprocess
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path
from urllib.parse import urlencode, urljoin, urlparse

import requests

CONNECTORS_FILE = Path(os.environ.get("CONNECTORS_FILE",
                                   Path(os.environ.get("APP_DATA_DIR", "./data")) / "connectors" / "connectors.json"))
PROTOCOL_VERSION = "2025-06-18"
CLIENT_INFO = {"name": "Quorum", "version": "1.0"}
TRANSPORTS = ("http", "sse", "stdio")
IDLE_SECONDS = 15 * 60
STDIO_TIMEOUT = 60
PINNED = re.compile(r"^v?\d+(?:\.\d+){0,3}(?:[-+.][0-9A-Za-z.-]+)?$")
_LOCK = threading.RLock()


def _now() -> str:
    """The current local time as an ISO string."""
    return datetime.now().isoformat(timespec="seconds")


def save_connectors(connectors: list[dict]) -> None:
    """Writes the connector list to CONNECTORS_FILE (secrets are never stored there)."""
    CONNECTORS_FILE.parent.mkdir(parents=True, exist_ok=True)
    CONNECTORS_FILE.write_text(json.dumps(connectors, indent=2, ensure_ascii=False), encoding="utf-8")


def add_connector(kind: str, name: str, description: str = "", url: str = "") -> dict:
    """Adds a connector the old way (a 'custom' description or a single-URL MCP server). Raises ValueError for a
    missing name or URL.
    """
    name = (name or "").strip()
    if not name:
        raise ValueError("Connector name cannot be empty.")
    if kind not in {"custom", "mcp"}:
        raise ValueError("Connector kind must be 'custom' or 'mcp'.")
    if kind == "mcp" and not str(url).strip():
        raise ValueError("MCP connectors require a server URL.")
    connectors = load_connectors()
    if any(c["name"] == name for c in connectors):
        raise ValueError(f"A connector named '{name}' already exists.")
    connector = {
        "id": uuid.uuid4().hex,
        "kind": kind,
        "name": name.strip(),
        "description": (description or "").strip(),
        "url": str(url).strip(),
        "created_at": _now(),
    }
    connectors.append(connector)
    save_connectors(connectors)
    return connector


def remove_connector(connector_id: str) -> None:
    """Removes a connector: stops its local process if it has one and deletes its stored keys and sign-in tokens.
    """
    connector = get_connector(connector_id)
    STDIO.stop(connector_id)
    save_connectors([c for c in load_connectors() if c["id"] != connector_id])
    if connector:
        for name in secret_names(connector):
            _secrets().delete(secret_name(connector_id, name))


def load_connectors() -> list[dict]:
    """All saved connectors ([] when the file is missing or unreadable)."""
    if not CONNECTORS_FILE.exists():
        return []
    try:
        data = json.loads(CONNECTORS_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except (OSError, json.JSONDecodeError):
        return []


def get_connector(connector_id: str) -> dict | None:
    """One connector by id, or None."""
    return next((c for c in load_connectors() if c.get("id") == connector_id), None)


def active_connectors() -> list[dict]:
    """What the agent may use: enabled MCP connectors."""
    return [c for c in load_connectors() if c.get("enabled", True)]


# ---------------------------------------------------------------------------
# MCP client (JSON-RPC 2.0 over Streamable HTTP), as used by older connectors
# ---------------------------------------------------------------------------

def _rpc(url: str, method: str, params: dict) -> dict:
    """One JSON-RPC call to an MCP server over HTTP (the original, single-request path used by older connectors).
    Returns the 'result' or raises on an error reply.
    """
    response = requests.post(
        url,
        json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
        headers={"Content-Type": "application/json", "Accept": "application/json, text/event-stream"},
        timeout=30,
    )
    response.raise_for_status()
    body = response.json()
    if "error" in body:
        raise RuntimeError(str(body["error"]))
    return body.get("result", {})


def list_mcp_tools(url: str) -> list[dict]:
    """tools/list -> [{'name','description'}]"""
    result = _rpc(url, "tools/list", {})
    return [
        {"name": t.get("name", ""), "description": t.get("description", "")}
        for t in result.get("tools", [])
    ]


def call_mcp_tool(url: str, tool_name: str, arguments: dict) -> str:
    """Calls a tool on an older-style MCP connector and returns its text output."""
    result = _rpc(url, "tools/call", {"name": tool_name, "arguments": arguments})
    return _tool_text(result)


def _tool_text(result: dict) -> str:
    """The readable text of an MCP tool result (its text parts), prefixed with 'Error from the tool:' when the
    tool reported a failure.
    """
    parts = result.get("content", [])
    texts = [p.get("text", "") for p in parts if isinstance(p, dict) and p.get("type") == "text"]
    text = "\n".join(texts) or json.dumps(result)
    return ("Error from the tool: " + text) if result.get("isError") else text


def _arguments(tool_input) -> dict:
    """Turns the model's tool input into an arguments dict: JSON stays as is, plain text becomes {'query': text}.
    """
    if isinstance(tool_input, dict):
        return tool_input
    tool_input = str(tool_input or "")
    try:
        args = json.loads(tool_input) if tool_input.strip().startswith("{") else {"query": tool_input}
    except json.JSONDecodeError:
        args = {"query": tool_input}
    return args if isinstance(args, dict) else {"query": tool_input}


def execute_connector(connector: dict, tool_input, tool: str | None = None) -> str:
    """Run an MCP connector tool; input is JSON arguments or plain text."""
    try:
        args = _arguments(tool_input)
        if not connector.get("transport"):  # older connectors: the connector name is the tool
            return call_mcp_tool(connector["url"], connector["name"], args)
        tools = [t["name"] for t in connector.get("tools") or []]
        name = tool or (tools[0] if len(tools) == 1 else "")
        if not name:
            return (f"Connector '{connector['name']}' has several tools; name one with \"tool\": "
                    + ", ".join(tools))
        return call_tool(connector, name, args)
    except Exception as exc:
        return f"Error running connector '{connector['name']}': {exc}"


# ---------------------------------------------------------------------------
# Connectors with transports, auth and tools
# ---------------------------------------------------------------------------

def _secrets():
    """The secrets_store module (imported lazily, so importing this module stays cheap)."""
    import secrets_store
    return secrets_store


def secret_name(connector_id: str, name: str) -> str:
    """The secrets_store name for one of a connector's secrets, e.g. 'connector_ab12_Authorization'."""
    return f"connector_{connector_id}_{re.sub(r'[^A-Za-z0-9_-]', '_', name)}"


def secret_names(connector: dict) -> list[str]:
    """The names of every secret a connector may have stored: OAuth tokens, an auth header, secret headers and
    secret environment variables.
    """
    names = ["oauth"]
    auth = connector.get("auth") or {}
    if auth.get("type") == "header":
        names.append(auth.get("header") or "Authorization")
    names += list((connector.get("headers") or {}).get("secret", []))
    names += list((connector.get("stdio") or {}).get("secret_env", []))
    return list(dict.fromkeys(names))


def is_local_url(url: str) -> bool:
    """True when the server runs on this Mac (localhost addresses)."""
    return bool(re.match(r"^https?://(localhost|127\.0\.0\.1|\[::1\]|host\.docker\.internal)(:|/|$)", url or ""))


def is_remote(connector: dict) -> bool:
    """Remote connectors are external services: they follow the cloud privacy rules."""
    return connector.get("transport") != "stdio" and not is_local_url(connector.get("url", ""))


def public(connector: dict) -> dict:
    """A connector as shown in Settings: its fields plus whether it's remote, running, signed in and which
    secrets are set (never the secrets themselves).
    """
    out = {k: v for k, v in connector.items()}
    out.setdefault("enabled", True)
    out.setdefault("tools", [])
    out["remote"] = is_remote(connector)
    out["running"] = STDIO.running(connector["id"]) if connector.get("transport") == "stdio" else None
    stored = {n: bool(_secrets().get(secret_name(connector["id"], n))) for n in secret_names(connector)}
    out["secrets_set"] = {k: v for k, v in stored.items() if k != "oauth"}
    out["signed_in"] = stored.get("oauth", False)
    return out


def _check_name(name: str, existing: list[dict]) -> str:
    """Trims a new connector's name and rejects empty or duplicate names."""
    name = (name or "").strip()
    if not name:
        raise ValueError("Connector name cannot be empty.")
    if any(c["name"].lower() == name.lower() for c in existing):
        raise ValueError(f"A connector named '{name}' already exists.")
    return name


def add_remote(name: str, url: str, transport: str = "http", description: str = "", auth_type: str = "none",
               header: str = "Authorization", header_value: str = "", extra_headers: dict | None = None,
               source: dict | None = None) -> dict:
    """A remote MCP server (Streamable HTTP or SSE). Secret header values go to the Keychain."""
    url = (url or "").strip()
    if not re.match(r"^https?://", url):
        raise ValueError("Give the server's URL (https://…).")
    if url.startswith("http://") and not is_local_url(url):
        raise ValueError("Use https:// for a remote server (http:// is only allowed on this Mac).")
    if transport not in ("http", "sse"):
        raise ValueError("Transport must be Streamable HTTP or SSE.")
    if auth_type not in ("none", "header", "oauth"):
        raise ValueError("Unknown sign-in type.")
    with _LOCK:
        existing = load_connectors()
        connector = {"id": uuid.uuid4().hex, "kind": "mcp", "name": _check_name(name, existing),
                     "description": (description or "").strip(), "url": url, "transport": transport,
                     "auth": {"type": auth_type, "header": header or "Authorization"}, "enabled": True,
                     "tools": [], "source": source or {"type": "url"}, "created_at": _now(),
                     "headers": {"secret": [h for h, v in (extra_headers or {}).items() if v]}}
        existing.append(connector)
        save_connectors(existing)
    if auth_type == "header" and header_value.strip():
        _secrets().set(secret_name(connector["id"], connector["auth"]["header"]), header_value.strip())
    for header_name, value in (extra_headers or {}).items():
        if value and str(value).strip():
            _secrets().set(secret_name(connector["id"], header_name), str(value).strip())
    return connector


def stdio_command(registry_type: str, identifier: str, version: str, args: list[str] | None = None) -> list[str]:
    """The exact command for a pinned package: npx -y pkg@1.2.3 / uvx pkg==1.2.3."""
    if not PINNED.match(version or "") or version.lower() == "latest":
        raise ValueError("A local package needs an exact version (e.g. 1.2.3), never “latest”.")
    if not re.match(r"^(@[a-z0-9][\w.-]*/)?[A-Za-z0-9][\w.-]*$", identifier or ""):
        raise ValueError("That package name isn't valid.")
    args = [str(a) for a in args or [] if str(a).strip() and str(a) != "-y"]
    if registry_type == "npm":
        return ["npx", "-y", f"{identifier}@{version.lstrip('v')}", *args]
    if registry_type == "pypi":
        return ["uvx", f"{identifier}=={version.lstrip('v')}", *args]
    raise ValueError(f"{registry_type} packages aren't supported; npm and PyPI packages are.")


def add_stdio(name: str, registry_type: str, identifier: str, version: str, description: str = "",
              env: dict | None = None, secret_env: dict | None = None, args: list[str] | None = None,
              source: dict | None = None, confirmed: bool = False) -> dict:
    """A local package run over stdio. Needs `confirmed` (the user saw the command and publisher)."""
    if not confirmed:
        raise PermissionError("Confirm first: this installs and runs third-party code with your user's permissions.")
    command = stdio_command(registry_type, identifier, version, args)
    if not shutil.which(command[0]):
        raise ValueError(f"{command[0]} isn't installed on this Mac "
                         f"({'Node.js' if command[0] == 'npx' else 'uv'} is needed for this package).")
    with _LOCK:
        existing = load_connectors()
        connector = {"id": uuid.uuid4().hex, "kind": "mcp", "name": _check_name(name, existing),
                     "description": (description or "").strip(), "url": "", "transport": "stdio",
                     "auth": {"type": "none"}, "enabled": True, "tools": [], "source": source or {"type": "package"},
                     "created_at": _now(),
                     "stdio": {"registry_type": registry_type, "identifier": identifier, "version": version.lstrip("v"),
                               "command": command, "args": list(args or []),
                               "env": {k: str(v) for k, v in (env or {}).items() if str(v).strip()},
                               "secret_env": [k for k, v in (secret_env or {}).items() if str(v).strip()]}}
        existing.append(connector)
        save_connectors(existing)
    for key, value in (secret_env or {}).items():
        if str(value).strip():
            _secrets().set(secret_name(connector["id"], key), str(value).strip())
    return connector


def update_connector(connector_id: str, fields: dict) -> dict | None:
    """Switches a connector on/off, renames it or changes its description, URL or keys. None if it doesn't exist.
    """
    with _LOCK:
        connectors = load_connectors()
        connector = next((c for c in connectors if c["id"] == connector_id), None)
        if connector is None:
            return None
        if isinstance(fields.get("enabled"), bool):
            connector["enabled"] = fields["enabled"]
            if not fields["enabled"]:
                STDIO.stop(connector_id)
        if isinstance(fields.get("description"), str):
            connector["description"] = fields["description"].strip()
        for key in ("tools", "tools_checked_at", "last_error", "source", "stdio"):
            if key in fields:
                connector[key] = fields[key]
        save_connectors(connectors)
    if isinstance(fields.get("header_value"), str) and fields["header_value"].strip():
        header = (connector.get("auth") or {}).get("header") or "Authorization"
        _secrets().set(secret_name(connector_id, header), fields["header_value"].strip())
    return connector


def update_version(connector_id: str, version: str, confirmed: bool = False) -> dict:
    """Move a local package to another pinned version (after the user confirmed the change)."""
    connector = get_connector(connector_id)
    if connector is None or connector.get("transport") != "stdio":
        raise ValueError("Only local package connectors have versions to update.")
    if not confirmed:
        raise PermissionError("Confirm the version change first.")
    pkg = connector["stdio"]
    command = stdio_command(pkg["registry_type"], pkg["identifier"], version, pkg.get("args"))
    STDIO.stop(connector_id)
    source = dict(connector.get("source") or {}, version=version.lstrip("v"))
    return update_connector(connector_id, {"stdio": dict(pkg, version=version.lstrip("v"), command=command),
                                           "source": source, "tools": []})


# ---------------------------------------------------------------------------
# Talking MCP: Streamable HTTP, HTTP+SSE, stdio
# ---------------------------------------------------------------------------

class NeedsSignIn(RuntimeError):
    """The server answered 401: OAuth sign-in (or a key) is needed."""

    def __init__(self, message: str, www_authenticate: str = ""):
        """Keeps the server's WWW-Authenticate header, which says where to sign in."""
        super().__init__(message)
        self.www_authenticate = www_authenticate


def _auth_headers(connector: dict) -> dict:
    """The HTTP headers that authenticate requests to a connector: a stored API key header or an OAuth access
    token (refreshed when it has expired).
    """
    headers: dict[str, str] = {}
    auth = connector.get("auth") or {}
    if auth.get("type") == "header":
        name = auth.get("header") or "Authorization"
        value = _secrets().get(secret_name(connector["id"], name))
        if value:
            headers[name] = value if name != "Authorization" or " " in value else f"Bearer {value}"
    elif auth.get("type") == "oauth":
        token = oauth_access_token(connector)
        if token:
            headers["Authorization"] = f"Bearer {token}"
    for name in (connector.get("headers") or {}).get("secret", []):
        value = _secrets().get(secret_name(connector["id"], name))
        if value:
            headers[name] = value
    return headers


def _parse_body(response: requests.Response, want_id: int) -> dict | None:
    """Reads the JSON-RPC reply with id `want_id` from a response, whether it came back as plain JSON or as an
    event stream.
    """
    kind = response.headers.get("Content-Type", "")
    if "text/event-stream" in kind:
        for line in response.iter_lines(chunk_size=None, decode_unicode=True):
            if line and line.startswith("data:"):
                try:
                    message = json.loads(line[5:].strip())
                except ValueError:
                    continue
                if isinstance(message, dict) and message.get("id") == want_id:
                    return message
        return None
    if not response.content:
        return None
    return response.json()


class HttpSession:
    """Streamable HTTP: initialize (keeps Mcp-Session-Id), then requests."""

    def __init__(self, connector: dict):
        """Remembers the connector; the session id comes from the server's reply to 'initialize'."""
        self.connector = connector
        self.url = connector["url"]
        self.session_id = ""
        self.next_id = 1

    def _post(self, payload: dict, timeout: float = 60) -> requests.Response:
        """POSTs one JSON-RPC message with the protocol, session and auth headers. A 401 reply raises
        NeedsSignIn.
        """
        headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream",
                   "MCP-Protocol-Version": PROTOCOL_VERSION, **_auth_headers(self.connector)}
        if self.session_id:
            headers["Mcp-Session-Id"] = self.session_id
        response = requests.post(self.url, json=payload, headers=headers, timeout=timeout,
                                 stream="id" in payload)
        if response.status_code == 401:
            raise NeedsSignIn(f"{self.connector['name']} needs you to sign in.",
                              response.headers.get("WWW-Authenticate", ""))
        if response.status_code >= 400:
            raise RuntimeError(f"{self.connector['name']} answered {response.status_code}: {response.text[:200]}")
        return response

    def request(self, method: str, params: dict, timeout: float = 60) -> dict:
        """Sends a request and waits for its reply ('result'); an error reply raises RuntimeError with the
        server's message.
        """
        rid = self.next_id
        self.next_id += 1
        response = self._post({"jsonrpc": "2.0", "id": rid, "method": method, "params": params}, timeout)
        if method == "initialize":
            self.session_id = response.headers.get("Mcp-Session-Id", "") or self.session_id
        body = _parse_body(response, rid)
        if body is None:
            raise RuntimeError(f"{self.connector['name']} sent no reply to {method}.")
        if "error" in body:
            raise RuntimeError(str(body["error"].get("message") if isinstance(body["error"], dict) else body["error"]))
        return body.get("result", {})

    def notify(self, method: str) -> None:
        """Sends a notification (a message that gets no reply)."""
        self._post({"jsonrpc": "2.0", "method": method}, 15)

    def close(self) -> None:
        """Nothing to close: every HTTP request is independent."""
        pass


class SseSession:
    """The older HTTP+SSE transport: GET opens the event stream, which names the POST endpoint;
    replies arrive as "message" events on the stream."""

    def __init__(self, connector: dict):
        """Opens the event stream and waits for the 'endpoint' event that says where to POST requests."""
        self.connector = connector
        headers = {"Accept": "text/event-stream", **_auth_headers(connector)}
        self.stream = requests.get(connector["url"], headers=headers, stream=True, timeout=(10, 120))
        if self.stream.status_code == 401:
            raise NeedsSignIn(f"{connector['name']} needs you to sign in.",
                              self.stream.headers.get("WWW-Authenticate", ""))
        self.stream.raise_for_status()
        self.events = self._events()
        self.endpoint = ""
        for event, data in self.events:
            if event == "endpoint":
                self.endpoint = urljoin(connector["url"], data.strip())
                break
        if not self.endpoint:
            raise RuntimeError(f"{connector['name']} didn't send its message endpoint.")
        self.next_id = 1

    def _events(self):
        """Reads the event stream and yields (event name, data) for each complete event (events end with a blank
        line).
        """
        event, data = "message", []
        # chunk_size=1: hand over each event as soon as it arrives (a bigger buffer would wait for
        # more data, and None reads to the end on streams that aren't chunk-encoded)
        for line in self.stream.iter_lines(chunk_size=1, decode_unicode=True):
            if line is None:
                continue
            if line == "":
                if data:
                    yield event, "\n".join(data)
                event, data = "message", []
            elif line.startswith("event:"):
                event = line[6:].strip()
            elif line.startswith("data:"):
                data.append(line[5:].lstrip())

    def _post(self, payload: dict) -> None:
        """POSTs one JSON-RPC message to the server's endpoint."""
        response = requests.post(self.endpoint, json=payload, timeout=30,
                                 headers={"Content-Type": "application/json", **_auth_headers(self.connector)})
        if response.status_code == 401:
            raise NeedsSignIn(f"{self.connector['name']} needs you to sign in.",
                              response.headers.get("WWW-Authenticate", ""))
        if response.status_code >= 400:
            raise RuntimeError(f"{self.connector['name']} answered {response.status_code}.")

    def request(self, method: str, params: dict, timeout: float = 60) -> dict:
        """Sends a request, then reads the event stream until the reply with the same id arrives."""
        rid = self.next_id
        self.next_id += 1
        self._post({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})
        for event, data in self.events:
            if event != "message":
                continue
            try:
                message = json.loads(data)
            except ValueError:
                continue
            if isinstance(message, dict) and message.get("id") == rid:
                if "error" in message:
                    raise RuntimeError(str(message["error"]))
                return message.get("result", {})
        raise RuntimeError(f"{self.connector['name']} closed the stream before replying.")

    def notify(self, method: str) -> None:
        """Sends a notification (no reply expected)."""
        self._post({"jsonrpc": "2.0", "method": method})

    def close(self) -> None:
        """Closes the event stream."""
        self.stream.close()


class StdioServer:
    """One running local package: newline-delimited JSON-RPC on stdin/stdout."""

    def __init__(self, connector: dict):
        """Starts the package's process with its environment (stored keys are passed as environment variables)
        and a thread that reads its output.
        """
        self.connector = connector
        pkg = connector["stdio"]
        env = dict(os.environ)
        env.update(pkg.get("env") or {})
        for name in pkg.get("secret_env") or []:
            value = _secrets().get(secret_name(connector["id"], name))
            if value:
                env[name] = value
        log_dir = CONNECTORS_FILE.parent / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        self.log = open(log_dir / f"{connector['id']}.log", "ab")  # noqa: SIM115 (closed in stop)
        command = pkg["command"]
        if os.environ.get("CONNECTORS_TEST_STDIO"):  # end-to-end tests only: a fake server, nothing installed
            command = shlex.split(os.environ["CONNECTORS_TEST_STDIO"])
        self.proc = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=self.log, env=env, text=True, bufsize=1,
                                     cwd=str(CONNECTORS_FILE.parent))
        self.replies: "queue.Queue[dict | None]" = queue.Queue()
        self.next_id = 1
        self.lock = threading.Lock()
        self.last_used = time.time()
        threading.Thread(target=self._read, daemon=True).start()
        self.initialized = False

    def _read(self) -> None:
        """Background thread: reads the process's output lines and hands replies to whoever is waiting for them."""
        assert self.proc.stdout is not None
        for line in self.proc.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                message = json.loads(line)
            except ValueError:
                continue
            if isinstance(message, dict) and "id" in message and ("result" in message or "error" in message):
                self.replies.put(message)
        self.replies.put(None)

    def _send(self, payload: dict) -> None:
        """Writes one JSON message (a line) to the process."""
        assert self.proc.stdin is not None
        self.proc.stdin.write(json.dumps(payload) + "\n")
        self.proc.stdin.flush()

    def request(self, method: str, params: dict, timeout: float = STDIO_TIMEOUT) -> dict:
        """Sends a request and waits for the reply with the same id (up to `timeout` seconds). One request at a
        time per server.
        """
        with self.lock:
            self.last_used = time.time()
            rid = self.next_id
            self.next_id += 1
            self._send({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})
            deadline = time.time() + timeout
            while True:
                left = deadline - time.time()
                if left <= 0:
                    raise TimeoutError(f"{self.connector['name']} didn't answer {method} within {int(timeout)} s.")
                try:
                    message = self.replies.get(timeout=left)
                except queue.Empty:
                    continue
                if message is None:
                    raise RuntimeError(f"{self.connector['name']} stopped (see its log).")
                if message.get("id") != rid:
                    continue
                if "error" in message:
                    raise RuntimeError(str(message["error"].get("message") if isinstance(message["error"], dict)
                                           else message["error"]))
                return message.get("result", {})

    def notify(self, method: str) -> None:
        """Sends a notification (no reply expected)."""
        with self.lock:
            self._send({"jsonrpc": "2.0", "method": method})

    def alive(self) -> bool:
        """True while the process is running."""
        return self.proc.poll() is None

    def stop(self) -> None:
        """Stops the process politely (close input, terminate), or kills it after 5 seconds."""
        try:
            if self.proc.stdin:
                self.proc.stdin.close()
            self.proc.terminate()
            self.proc.wait(timeout=5)
        except Exception:
            self.proc.kill()
        finally:
            self.log.close()


class StdioManager:
    """Started on first use, reused, stopped when idle, disabled, removed or on exit."""

    def __init__(self):
        """No servers running yet."""
        self.servers: dict[str, StdioServer] = {}
        self.lock = threading.Lock()
        self._reaper: threading.Thread | None = None

    def get(self, connector: dict) -> StdioServer:
        """The running server for a connector, starting (and initialising) it when needed."""
        with self.lock:
            server = self.servers.get(connector["id"])
            if server is not None and not server.alive():
                server.stop()
                server = None
            if server is None:
                server = StdioServer(connector)
                self.servers[connector["id"]] = server
                if self._reaper is None:
                    self._reaper = threading.Thread(target=self._reap, daemon=True)
                    self._reaper.start()
        if not server.initialized:
            # First start may download the package: allow a few minutes.
            server.request("initialize", {"protocolVersion": PROTOCOL_VERSION, "capabilities": {},
                                          "clientInfo": CLIENT_INFO}, timeout=240)
            server.notify("notifications/initialized")
            server.initialized = True
        return server

    def running(self, connector_id: str) -> bool:
        """True when the connector's process is running."""
        server = self.servers.get(connector_id)
        return bool(server and server.alive())

    def stop(self, connector_id: str) -> None:
        """Stops one connector's process."""
        with self.lock:
            server = self.servers.pop(connector_id, None)
        if server:
            server.stop()

    def stop_all(self) -> None:
        """Stops every process (called when the API shuts down)."""
        for connector_id in list(self.servers):
            self.stop(connector_id)

    def _reap(self) -> None:
        """Background thread: every 30 s, stops servers that have been idle too long."""
        while True:
            time.sleep(30)
            for connector_id, server in list(self.servers.items()):
                if time.time() - server.last_used > IDLE_SECONDS:
                    self.stop(connector_id)


STDIO = StdioManager()
atexit.register(STDIO.stop_all)


def open_session(connector: dict):
    """Opens a session with the right transport (stdio, SSE or HTTP) and performs the MCP handshake
    ('initialize', then 'notifications/initialized').
    """
    transport = connector.get("transport") or "http"
    if transport == "stdio":
        return STDIO.get(connector)
    session = SseSession(connector) if transport == "sse" else HttpSession(connector)
    try:
        session.request("initialize", {"protocolVersion": PROTOCOL_VERSION, "capabilities": {},
                                       "clientInfo": CLIENT_INFO}, timeout=30)
        session.notify("notifications/initialized")
    except NeedsSignIn:
        session.close()
        raise
    except RuntimeError:
        if transport == "sse":
            session.close()
            raise
        session.session_id = ""  # a minimal server without a handshake: plain requests still work
    return session


def list_tools(connector: dict) -> list[dict]:
    """The tools a connector offers: [{name, description}]."""
    session = open_session(connector)
    try:
        result = session.request("tools/list", {})
    finally:
        if connector.get("transport") != "stdio":
            session.close()
    return [{"name": t.get("name", ""), "description": (t.get("description") or "")[:500]}
            for t in result.get("tools", []) if isinstance(t, dict) and t.get("name")]


def call_tool(connector: dict, tool: str, arguments: dict) -> str:
    """Calls one tool on a connector and returns its text output."""
    session = open_session(connector)
    try:
        return _tool_text(session.request("tools/call", {"name": tool, "arguments": arguments}, timeout=120))
    finally:
        if connector.get("transport") != "stdio":
            session.close()


def test_connector(connector_id: str) -> dict:
    """Connect, list the tools and save them: {"ok", "tools", "error", "needs_sign_in"}."""
    connector = get_connector(connector_id)
    if connector is None:
        raise KeyError(connector_id)
    try:
        tools = list_tools(connector) if connector.get("transport") else list_mcp_tools(connector["url"])
    except NeedsSignIn as exc:
        update_connector(connector_id, {"last_error": "sign_in", "tools_checked_at": _now()})
        return {"ok": False, "needs_sign_in": True, "error": str(exc), "tools": []}
    except Exception as exc:  # noqa: BLE001 (shown to the user)
        update_connector(connector_id, {"last_error": str(exc)[:300], "tools_checked_at": _now()})
        return {"ok": False, "needs_sign_in": False, "error": str(exc)[:300], "tools": []}
    update_connector(connector_id, {"tools": tools, "last_error": "", "tools_checked_at": _now()})
    return {"ok": True, "needs_sign_in": False, "error": "", "tools": tools}


def probe_tools(url: str, transport: str = "http") -> dict:
    """Tools of a remote server before adding it (works only when it doesn't need sign-in)."""
    temp = {"id": "probe", "name": "server", "url": url, "transport": transport, "auth": {"type": "none"}}
    try:
        return {"tools": list_tools(temp), "needs_sign_in": False, "error": ""}
    except NeedsSignIn:
        return {"tools": [], "needs_sign_in": True, "error": ""}
    except Exception as exc:  # noqa: BLE001
        return {"tools": [], "needs_sign_in": False, "error": str(exc)[:200]}


# ---------------------------------------------------------------------------
# OAuth sign-in (MCP authorization: protected-resource metadata, dynamic client
# registration, authorization code + PKCE, redirect back to the API on 127.0.0.1)
# ---------------------------------------------------------------------------

_PENDING: dict[str, dict] = {}
PENDING_TTL = 15 * 60


def _b64url(data: bytes) -> str:
    """Base64 without padding, safe for URLs (used for PKCE values)."""
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def _origin(url: str) -> str:
    """The scheme and host of a URL: 'https://example.com/mcp' -> 'https://example.com'."""
    parts = urlparse(url)
    return f"{parts.scheme}://{parts.netloc}"


def discover_oauth(connector: dict, www_authenticate: str = "") -> dict:
    """Authorization server endpoints for this MCP server."""
    match = re.search(r'resource_metadata="([^"]+)"', www_authenticate or "")
    # MCP sign-in discovery: the server's 'protected resource' document names its authorization server; that
    # server's well-known document lists the sign-in and token URLs.
    resource_url = match.group(1) if match else f"{_origin(connector['url'])}/.well-known/oauth-protected-resource"
    issuer = _origin(connector["url"])
    try:
        resource = requests.get(resource_url, timeout=15)
        if resource.ok:
            servers = resource.json().get("authorization_servers") or []
            issuer = servers[0].rstrip("/") if servers else issuer
    except (requests.RequestException, ValueError):
        pass
    meta = {}
    for path in ("/.well-known/oauth-authorization-server", "/.well-known/openid-configuration"):
        try:
            response = requests.get(issuer + path, timeout=15)
            if response.ok:
                meta = response.json()
                break
        except (requests.RequestException, ValueError):
            continue
    if not meta.get("authorization_endpoint") or not meta.get("token_endpoint"):
        raise RuntimeError("This server doesn't publish OAuth sign-in details. Add it with an API key instead.")
    return meta


def start_oauth(connector_id: str, redirect_uri: str) -> str:
    """The URL to open in the browser to sign in."""
    connector = get_connector(connector_id)
    if connector is None:
        raise KeyError(connector_id)
    www = ""
    try:
        HttpSession(connector)._post({"jsonrpc": "2.0", "id": 0, "method": "ping", "params": {}}, 15)
    except NeedsSignIn as exc:
        www = exc.www_authenticate
    except Exception:  # noqa: BLE001
        pass
    meta = discover_oauth(connector, www)
    client_id, client_secret = (connector.get("oauth") or {}).get("client_id", ""), ""
    # Dynamic client registration: Quorum registers itself with the server to get a client id (no secret,
    # because a desktop app can't keep one).
    if meta.get("registration_endpoint"):
        response = requests.post(meta["registration_endpoint"], timeout=20, json={
            "client_name": "Quorum (local meeting assistant)", "redirect_uris": [redirect_uri],
            "grant_types": ["authorization_code", "refresh_token"], "response_types": ["code"],
            "token_endpoint_auth_method": "none"})
        if not response.ok:
            raise RuntimeError(f"The server refused to register Quorum for sign-in ({response.status_code}).")
        registered = response.json()
        client_id, client_secret = registered.get("client_id", ""), registered.get("client_secret", "")
    if not client_id:
        raise RuntimeError("This server needs a pre-registered OAuth client; add it with an API key instead.")
    # PKCE: a random secret (the verifier) stays here; only its SHA-256 hash (the challenge) goes into the sign-
    # in URL. When the browser comes back with a code, the verifier proves it's us asking for the tokens.
    # `state` ties the callback to this sign-in attempt.
    verifier = _b64url(pysecrets.token_bytes(48))
    state = _b64url(pysecrets.token_bytes(16))
    now = time.time()
    for key in [k for k, v in _PENDING.items() if now - v["created"] > PENDING_TTL]:
        _PENDING.pop(key, None)
    _PENDING[state] = {"connector_id": connector_id, "verifier": verifier, "client_id": client_id,
                       "client_secret": client_secret, "token_endpoint": meta["token_endpoint"],
                       "redirect_uri": redirect_uri, "created": now}
    update_connector(connector_id, {"source": connector.get("source") or {"type": "url"}})
    query = {"response_type": "code", "client_id": client_id, "redirect_uri": redirect_uri,
             "code_challenge": _b64url(hashlib.sha256(verifier.encode()).digest()),
             "code_challenge_method": "S256", "state": state, "resource": connector["url"]}
    if meta.get("scopes_supported"):
        query["scope"] = " ".join(meta["scopes_supported"])
    return f"{meta['authorization_endpoint']}?{urlencode(query)}"


def finish_oauth(state: str, code: str) -> dict:
    """Completes an OAuth sign-in: exchanges the code from the redirect for tokens (with the PKCE verifier) and
    stores them in the Keychain.
    """
    pending = _PENDING.pop(state, None)
    if pending is None or time.time() - pending["created"] > PENDING_TTL:
        raise RuntimeError("This sign-in link expired. Start the sign-in again.")
    connector = get_connector(pending["connector_id"])
    if connector is None:
        raise RuntimeError("The connector was removed.")
    data = {"grant_type": "authorization_code", "code": code, "redirect_uri": pending["redirect_uri"],
            "client_id": pending["client_id"], "code_verifier": pending["verifier"], "resource": connector["url"]}
    if pending["client_secret"]:
        data["client_secret"] = pending["client_secret"]
    response = requests.post(pending["token_endpoint"], data=data, timeout=20)
    if not response.ok:
        raise RuntimeError(f"Sign-in failed: the server answered {response.status_code}.")
    _save_tokens(connector["id"], response.json(), pending)
    return connector


def _save_tokens(connector_id: str, tokens: dict, client: dict) -> None:
    """Stores OAuth tokens and what's needed to refresh them, as one Keychain secret."""
    record = {"access_token": tokens.get("access_token", ""), "refresh_token": tokens.get("refresh_token", ""),
              "expires_at": time.time() + float(tokens.get("expires_in") or 3600) - 60,
              "token_endpoint": client["token_endpoint"], "client_id": client["client_id"],
              "client_secret": client.get("client_secret", "")}
    _secrets().set(secret_name(connector_id, "oauth"), json.dumps(record))


def oauth_access_token(connector: dict) -> str:
    """A valid OAuth access token for the connector, refreshing it when it has expired ('' when not signed in or
    refreshing failed).
    """
    raw = _secrets().get(secret_name(connector["id"], "oauth"))
    if not raw:
        return ""
    try:
        record = json.loads(raw)
    except ValueError:
        return ""
    if record.get("expires_at", 0) > time.time() or not record.get("refresh_token"):
        return record.get("access_token", "")
    data = {"grant_type": "refresh_token", "refresh_token": record["refresh_token"], "client_id": record["client_id"],
            "resource": connector.get("url", "")}
    if record.get("client_secret"):
        data["client_secret"] = record["client_secret"]
    try:
        response = requests.post(record["token_endpoint"], data=data, timeout=20)
    except requests.RequestException:
        return record.get("access_token", "")
    if not response.ok:
        return ""
    tokens = response.json()
    tokens.setdefault("refresh_token", record["refresh_token"])
    _save_tokens(connector["id"], tokens, record)
    return tokens.get("access_token", "")


def sign_out(connector_id: str) -> None:
    """Forgets the connector's OAuth tokens."""
    _secrets().delete(secret_name(connector_id, "oauth"))
