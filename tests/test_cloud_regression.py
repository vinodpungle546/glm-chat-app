#!/usr/bin/env python3
"""Cloud (GAIA / GLM-5.3-Flash) requests must stay byte-for-byte unchanged.

These expectations were captured from the original Streamlit app before the local-model
work. The app is gone; the same requests are now pinned on the code the web app uses
(llm_backends, and agent.py through model_registry.GaiaProvider).
"""

import json
from unittest.mock import MagicMock, patch

import agent
import llm_backends as lb
import model_registry as mr
from web_search import build_search_decision_messages

GAIA_ENDPOINT = "https://api.clients.globant.com/v1/chat/completions"
GAIA_MODEL = "globant_dgx/GLM-5.3-Flash"
HISTORY = [{"role": "user", "content": "hi"}, {"role": "assistant", "content": "hello"}]


def sse_response(*chunks):
    resp = MagicMock()
    resp.__enter__.return_value = resp
    resp.__exit__.return_value = False
    lines = [f"data: {json.dumps({'choices': [{'delta': {'content': c}}]})}" for c in chunks]
    resp.iter_lines.return_value = lines + ["data: [DONE]"]
    return resp


def json_response(content):
    resp = MagicMock()
    resp.json.return_value = {"choices": [{"message": {"content": content}}]}
    return resp


def gaia_state(messages=None) -> agent.TurnState:
    return agent.TurnState(project={"id": "p1", "name": "T", "instructions": "", "skills": [], "tools": [],
                                    "memory": [], "documents": []},
                           messages=list(messages or HISTORY), provider=mr.get_provider("cloud:gaia"))


def headers():
    return {"Authorization": f"Bearer {lb.GAIA_API_KEY}", "Content-Type": "application/json"}


def test_defaults_unchanged():
    assert lb.GAIA_MODEL == GAIA_MODEL
    assert lb.GAIA_ENDPOINT == GAIA_ENDPOINT or lb.GAIA_ENDPOINT.endswith("/v1/chat/completions")


def test_stream_request_identical():
    msgs = [{"role": "system", "content": "S"}, {"role": "user", "content": "U"}]
    with patch.object(lb.requests, "post", return_value=sse_response("Hel", "lo")) as post:
        out = "".join(lb.stream_chat(msgs, lb.cloud()))
    assert out == "Hello"
    args, kwargs = post.call_args
    assert args == (lb.GAIA_ENDPOINT,)
    assert kwargs == {
        "headers": headers(),
        "json": {"model": lb.GAIA_MODEL, "messages": msgs, "stream": True},
        "stream": True,
        "timeout": 120,
    }
    assert list(kwargs["json"]) == ["model", "messages", "stream"]


def test_stream_strips_ui_only_keys():
    """Per-message model captions are UI metadata and never sent to GAIA."""
    msgs = [{"role": "assistant", "content": "A", "model": "qwen3:8b · local"}]
    with patch.object(lb.requests, "post", return_value=sse_response("x")) as post:
        "".join(mr.get_provider("cloud:gaia").stream(msgs))
    assert post.call_args.kwargs["json"]["messages"] == [{"role": "assistant", "content": "A"}]


def test_search_decision_request_identical():
    state = gaia_state()
    expected_messages = build_search_decision_messages(state.messages)
    with patch.object(lb.requests, "post", return_value=json_response('{"needs_search": false}')) as post:
        agent.get_search_decision(state)
    args, kwargs = post.call_args
    assert args == (lb.GAIA_ENDPOINT,)
    assert kwargs == {
        "headers": headers(),
        "json": {"model": lb.GAIA_MODEL, "messages": expected_messages, "stream": False},
        "timeout": 60,
    }
    assert list(kwargs["json"]) == ["model", "messages", "stream"]  # max_tokens is never added


def test_memory_request_identical():
    with patch.object(lb.requests, "post", return_value=json_response("[]")) as post:
        agent.extract_memories(gaia_state())
    args, kwargs = post.call_args
    assert args == (lb.GAIA_ENDPOINT,)
    assert set(kwargs) == {"headers", "json", "timeout"}
    assert kwargs["timeout"] == 60 and kwargs["headers"] == headers()
    payload = kwargs["json"]
    assert list(payload) == ["model", "stream", "messages"]  # the original app's key order
    assert payload["model"] == lb.GAIA_MODEL and payload["stream"] is False
    assert [m["role"] for m in payload["messages"]] == ["system", "user"]
    assert "You maintain long-term memory" in payload["messages"][0]["content"]
    assert payload["messages"][1]["content"] == "user: hi\nassistant: hello"
