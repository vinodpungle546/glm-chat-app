#!/usr/bin/env python3
"""Global tool/connectors registry shared by ALL projects.

Two kinds of connectors:
- "custom": Python-registered tools (built-ins or registered by code).
- "mcp":    remote MCP servers over Streamable HTTP (JSON-RPC 2.0).
            execute sends tools/call; list_tools sends tools/list.

State is persisted in CONNECTORS_FILE (default /app/data/connectors.json)
so added MCP servers are available to every project and survive restarts.
"""

from __future__ import annotations

import json
import os
import uuid
from datetime import datetime
from pathlib import Path

import requests

CONNECTORS_FILE = Path(os.environ.get("CONNECTORS_FILE", "/app/data/connectors.json"))


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def save_connectors(connectors: list[dict]) -> None:
    CONNECTORS_FILE.parent.mkdir(parents=True, exist_ok=True)
    CONNECTORS_FILE.write_text(json.dumps(connectors, indent=2, ensure_ascii=False), encoding="utf-8")


def add_connector(kind: str, name: str, description: str = "", url: str = "") -> dict:
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
    save_connectors([c for c in load_connectors() if c["id"] != connector_id])


def load_connectors() -> list[dict]:
    if not CONNECTORS_FILE.exists():
        return []
    try:
        data = json.loads(CONNECTORS_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except (OSError, json.JSONDecodeError):
        return []


# ---------------------------------------------------------------------------
# MCP client (JSON-RPC 2.0 over Streamable HTTP)
# ---------------------------------------------------------------------------

def _rpc(url: str, method: str, params: dict) -> dict:
    import requests
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
    result = _rpc(url, "tools/call", {"name": tool_name, "arguments": arguments})
    parts = result.get("content", [])
    texts = [p.get("text", "") for p in parts if isinstance(p, dict) and p.get("type") == "text"]
    return "\n".join(texts) or json.dumps(result)


def execute_connector(connector: dict, tool_input: str) -> str:
    """Run an MCP connector tool; input is JSON arguments or plain text."""
    try:
        args = json.loads(tool_input) if tool_input.strip().startswith("{") else {"query": tool_input}
        if not isinstance(args, dict):
            args = {"query": tool_input}
        return call_mcp_tool(connector["url"], connector["name"], args)
    except Exception as exc:
        return f"Error running connector '{connector['name']}': {exc}"
