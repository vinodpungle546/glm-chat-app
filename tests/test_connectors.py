#!/usr/bin/env python3
"""Tests for connectors.py: global registry CRUD + MCP client protocol."""

import json
from pathlib import Path
from unittest.mock import patch

import connectors


def with_registry(monkeypatch, tmp_path):
    connectors.CONNECTORS_FILE = tmp_path / "connectors.json"


def test_add_and_list(tmp_path, monkeypatch):
    with_registry(monkeypatch, tmp_path)
    connectors.add_connector("mcp", "github", "GitHub MCP", "http://x/mcp")
    connectors.add_connector("custom", "calc", "calculator")
    assert [c["name"] for c in connectors.load_connectors()] == ["github", "calc"]


def test_add_mcp_requires_url(monkeypatch, tmp_path):
    with_registry(monkeypatch, tmp_path)
    try:
        connectors.add_connector("mcp", "x", "", "")
        raised = False
    except ValueError:
        raised = True
    assert raised


def test_add_rejects_duplicate_name(monkeypatch, tmp_path):
    with_registry(monkeypatch, tmp_path)
    connectors.add_connector("custom", "calc", "")
    try:
        connectors.add_connector("custom", "calc", "")
        raised = False
    except ValueError:
        raised = True
    assert raised


def test_remove_connector(monkeypatch, tmp_path):
    with_registry(monkeypatch, tmp_path)
    connector = connectors.add_connector("custom", "calc", "")
    connectors.remove_connector(connector["id"])
    assert connectors.load_connectors() == []


def test_corrupt_file_returns_empty(monkeypatch, tmp_path):
    connectors.CONNECTORS_FILE = tmp_path / "connectors.json"
    connectors.CONNECTORS_FILE.write_text("{broken", encoding="utf-8")
    assert connectors.load_connectors() == []


# --- MCP protocol ---

def test_rpc_parses_jsonrpc_result(monkeypatch):
    class Resp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"jsonrpc": "2.0", "id": 1, "result": {"tools": [{"name": "search"}]}}

    monkeypatch.setattr(connectors.requests, "post", lambda *a, **k: Resp())
    tools = connectors.list_mcp_tools("http://x/mcp")
    assert tools == [{"name": "search", "description": ""}]


def test_rpc_raises_on_error_response(monkeypatch):
    class Resp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"error": {"code": -32601, "message": "nope"}}

    monkeypatch.setattr(connectors.requests, "post", lambda *a, **k: Resp())
    try:
        connectors.list_mcp_tools("http://x/mcp")
        raised = False
    except RuntimeError:
        raised = True
    assert raised


def test_call_mcp_tool_extracts_text(monkeypatch):
    class Resp:
        def raise_for_status(self):
            pass

        def json(self):
            return {
                "jsonrpc": "2.0",
                "id": 1,
                "result": {"content": [{"type": "text", "text": "result text"}]},
            }

    monkeypatch.setattr(connectors.requests, "post", lambda *a, **k: Resp())
    assert connectors.call_mcp_tool("http://x/mcp", "search", {"q": "x"}) == "result text"


def test_execute_connector_json_input(monkeypatch):
    connector = {"kind": "mcp", "name": "gh", "url": "http://x/mcp"}
    with patch.object(
        connectors, "call_mcp_tool", return_value="42"
    ) as mock_call:
        result = connectors.execute_connector(connector, '{"q": "stars"}')
    assert result == "42"
    mock_call.assert_called_once_with("http://x/mcp", "gh", {"q": "stars"})


def test_execute_connector_plain_text_wraps_in_query(monkeypatch):
    with patch.object(connectors, "call_mcp_tool", return_value="ok") as mock_call:
        connectors.execute_connector({"name": "gh", "url": "http://x"}, "hello world")
    mock_call.assert_called_once_with("http://x", "gh", {"query": "hello world"})


def test_execute_connector_failure_is_safe():
    connector = {"name": "broken", "url": "http://127.0.0.1:1/mcp", "kind": "mcp"}
    result = connectors.execute_connector(connector, "{}")
    assert result.startswith("Error running connector")
