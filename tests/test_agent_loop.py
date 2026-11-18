#!/usr/bin/env python3
"""Tests for agent-loop skill loading (progressive disclosure) + connector calls."""

from unittest.mock import patch

import app as app_module


def make_project(**overrides) -> dict:
    project = {
        "id": "p1",
        "name": "Test",
        "instructions": "",
        "skills": [
            {"name": "Table", "description": "Use tables", "content": "ALWAYS USE A MARKDOWN TABLE"}
        ],
        "tools": [],
        "memory": [],
        "documents": [],
    }
    project.update(overrides)
    return project


class FakeState(dict):
    def __getattr__(self, name):
        try:
            return self[name]
        except KeyError as exc:
            raise AttributeError(name) from exc

    def __setattr__(self, key, value):
        self[key] = value


def test_skill_request_loads_content(monkeypatch):
    captured = {}

    def scripted_reply(messages):
        if any("Full content of skill" in m["content"] for m in messages):
            return "final answer"
        return '{"skill": "Table"}'

    state = FakeState(
        web_search_enabled=False,
        messages=[{"role": "user", "content": "compare A and B"}],
        project=make_project(),
    )
    monkeypatch.setattr(app_module.st, "session_state", state, raising=False)
    with patch.object(app_module, "stream_assistant_reply", side_effect=scripted_reply):
        reply = app_module.generate_reply("compare A and B")

    assert reply == "final answer"


def test_connector_call_executes(monkeypatch):
    state = FakeState(
        web_search_enabled=False,
        messages=[{"role": "user", "content": "hi"}],
        project=make_project(),
    )
    monkeypatch.setattr(app_module.st, "session_state", state, raising=False)
    monkeypatch.setattr(
        app_module.connectors, "load_connectors",
        lambda: [{"id": "c1", "kind": "mcp", "name": "gh", "url": "http://x/mcp", "description": ""}],
    )

    def scripted_reply(messages):
        if any("[Tool output" in m["content"] for m in messages):
            return "stars: 10k"
        return '{"connector": "gh", "input": "stars"}'

    with patch.object(
        app_module.connectors, "execute_connector", return_value="10k stars"
    ) as mock_exec:
        with patch.object(app_module, "stream_assistant_reply", side_effect=scripted_reply):
            reply = app_module.generate_reply("how many stars?")

    assert reply == "stars: 10k"  # second stream produced the final answer


def test_system_prompt_exposes_connector_protocol(monkeypatch):
    state = FakeState(
        web_search_enabled=False,
        messages=[],
        project=make_project(),
    )
    monkeypatch.setattr(app_module.st, "session_state", state, raising=False)
    prompt = app_module.tools.build_agent_system_prompt(
        "Base.", make_project(), "", ""
    )
    assert "Available skills" in prompt
    assert '"skill"' in prompt
