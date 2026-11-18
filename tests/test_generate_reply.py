#!/usr/bin/env python3
"""Integration tests for the app's agent reply flow (all network mocked)."""

from unittest.mock import patch

import pytest
import requests

import app as app_module


def make_project(**overrides) -> dict:
    project = {
        "id": "p1",
        "name": "Test",
        "instructions": "",
        "skills": [],
        "tools": [],
        "memory": [],
        "documents": [],
    }
    project.update(overrides)
    return project


class FakeState(dict):
    """Dict with attribute access, like streamlit's session_state."""

    def __getattr__(self, name):
        try:
            return self[name]
        except KeyError as exc:
            raise AttributeError(name) from exc

    def __setattr__(self, key, value):
        self[key] = value


def make_state(**kwargs) -> FakeState:
    state = FakeState(
        web_search_enabled=False,
        messages=[],
        project=make_project(),
    )
    state.update(kwargs)
    return state


@pytest.fixture
def app_env(monkeypatch):
    monkeypatch.setattr(app_module, "SYSTEM_PROMPT", "You are helpful.")
    monkeypatch.setattr(app_module.st, "session_state", make_state(), raising=False)
    return app_module


def test_generate_reply_plain(app_env):
    app_module.st.session_state = make_state(
        messages=[{"role": "user", "content": "hi"}]
    )
    with patch.object(app_module, "stream_assistant_reply", return_value="Hello!") as mock_stream:
        reply = app_module.generate_reply("hi")

    assert reply == "Hello!"
    sent = mock_stream.call_args[0][0]
    assert sent[0]["role"] == "system"
    assert "You are helpful" in sent[0]["content"]
    assert sent[1] == {"role": "user", "content": "hi"}  # history included


def test_generate_reply_includes_document_context(app_env):
    app_module.st.session_state = make_state(
        project=make_project(documents=[{"name": "doc.txt", "text": "SECRET-CODE-123"}])
    )
    captured = {}

    def fake_stream(messages):
        captured["messages"] = messages
        return "ok"

    with patch.object(app_module, "stream_assistant_reply", side_effect=fake_stream):
        app_module.generate_reply("what is the code?")

    system_content = captured["messages"][0]["content"]
    assert "SECRET-CODE-123" in system_content
    assert "doc.txt" in system_content


def test_generate_reply_includes_instructions_and_memory(app_env):
    app_module.st.session_state = make_state(
        project=make_project(instructions="Be terse.", memory=["User likes Rust"])
    )
    captured = {}

    def fake_stream(messages):
        captured["messages"] = messages
        return "ok"

    with patch.object(app_module, "stream_assistant_reply", side_effect=fake_stream):
        app_module.generate_reply("hello")

    system_content = captured["messages"][0]["content"]
    assert "Be terse." in system_content
    assert "- User likes Rust" in system_content


def test_generate_reply_auto_search_when_requested(app_env):
    app_module.st.session_state = make_state(
        web_search_enabled=True, project=make_project(tools=["web_search"])
    )
    with patch.object(
        app_module, "get_search_decision", return_value={"needs_search": True, "query": "btc price"}
    ):
        with patch.object(app_module.tools, "run_tool", return_value="[1] price is 100k") as mock_run:
            with patch.object(
                app_module, "stream_assistant_reply", return_value="btc is 100k"
            ) as mock_final:
                reply = app_module.generate_reply("what is btc price?")

    assert reply == "btc is 100k"
    mock_run.assert_called_once_with("web_search", "btc price", {})
    final_messages = mock_final.call_args[0][0]
    assert any("[Tool output" in m["content"] for m in final_messages)


def test_generate_reply_skips_auto_search_when_disabled(app_env):
    app_module.st.session_state = make_state(web_search_enabled=False)
    with patch.object(app_module, "get_search_decision") as mock_decision:
        with patch.object(app_module, "stream_assistant_reply", return_value="direct"):
            reply = app_module.generate_reply("capital of France?")

    mock_decision.assert_not_called()
    assert reply == "direct"


def test_generate_reply_no_auto_search_without_web_search_tool(app_env):
    app_module.st.session_state = make_state(
        web_search_enabled=True, project=make_project(tools=[])
    )
    with patch.object(app_module, "get_search_decision") as mock_decision:
        with patch.object(app_module, "stream_assistant_reply", return_value="answer"):
            reply = app_module.generate_reply("hello")

    mock_decision.assert_not_called()
    assert reply == "answer"


def test_generate_reply_runs_tool_round(app_env):
    app_module.st.session_state = make_state(project=make_project(tools=["datetime"]))
    seen_streams = []

    def scripted_reply(messages):
        seen_streams.append(messages)
        if any("[Tool output" in m["content"] for m in messages):
            return "It is Thursday."
        return '{"tool": "datetime", "input": ""}'

    with patch.object(app_module.tools, "run_tool", return_value="Thu, Jan 1") as mock_run:
        with patch.object(app_module, "stream_assistant_reply", side_effect=scripted_reply):
            reply = app_module.generate_reply("what time is it?")

    mock_run.assert_called_once()
    assert reply == "It is Thursday."
    # The tool result was fed back to the model
    tool_feed = seen_streams[1]
    assert any("[Tool output" in m["content"] and "Thu, Jan 1" in m["content"] for m in tool_feed)


def test_generate_reply_replaces_raw_tool_call_in_history(app_env):
    app_module.st.session_state = make_state(
        project=make_project(tools=["datetime"]),
        messages=[{"role": "user", "content": "what time is it?"}],
    )

    def scripted_reply(messages):
        if any("[Tool output" in m["content"] for m in messages):
            return "It is Thursday."
        return '{"tool": "datetime", "input": ""}'

    with patch.object(app_module.tools, "run_tool", return_value="Thu, Jan 1"):
        with patch.object(app_module, "stream_assistant_reply", side_effect=scripted_reply):
            app_module.generate_reply("what time is it?")

    # History must NOT contain the raw tool-call JSON
    contents = [m["content"] for m in app_module.st.session_state["messages"]]
    assert all("{" not in c for c in contents)


def test_generate_reply_plain_answer_passes_through(app_env):
    app_module.st.session_state = make_state(project=make_project(tools=["datetime"]))
    with patch.object(app_module, "stream_assistant_reply", return_value="Just an answer."):
        reply = app_module.generate_reply("hello")

    assert reply == "Just an answer."


def test_get_search_decision_handles_api_failure(app_env, monkeypatch):
    def boom(*_args, **_kwargs):
        raise requests.ConnectionError("api down")

    monkeypatch.setattr(app_module.requests, "post", boom)
    assert app_module.get_search_decision() == {"needs_search": False}
