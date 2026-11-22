#!/usr/bin/env python3
"""Local (Ollama) backend requests and error handling."""

import json
from unittest.mock import MagicMock, patch

import pytest
import requests

import llm_backends as lb

REAL_LOADED_WINDOW = lb._loaded_window  # conftest stubs it for every test; kept here to test it


def ndjson_response(*chunks, status=200):
    resp = MagicMock()
    resp.status_code = status
    resp.__enter__.return_value = resp
    resp.__exit__.return_value = False
    lines = [json.dumps({"message": {"content": c}, "done": False}) for c in chunks]
    resp.iter_lines.return_value = lines + [json.dumps({"message": {"content": ""}, "done": True})]
    return resp


def test_local_stream_hits_ollama_with_selected_model():
    with patch.object(lb.requests, "post", return_value=ndjson_response("Hi", " there")) as post:
        out = "".join(lb.stream_chat([{"role": "user", "content": "x", "model": "ui-only"}],
                                     lb.local("gemma3:12b")))
    assert out == "Hi there"
    url = post.call_args.args[0]
    payload = post.call_args.kwargs["json"]
    assert url == f"{lb.LOCAL_LLM_BASE_URL}/api/chat"
    assert payload["model"] == "gemma3:12b"
    assert payload["messages"] == [{"role": "user", "content": "x"}]
    assert payload["think"] is False
    assert "Authorization" not in (post.call_args.kwargs.get("headers") or {})


def test_local_complete_streams_caps_tokens_and_strips_think():
    with patch.object(lb.requests, "post",
                      return_value=ndjson_response("<think>hmm</think>", "\nAnswer")) as post:
        out = lb.complete([{"role": "user", "content": "q"}], lb.local("qwen3:8b"), max_tokens=50)
    assert out.strip() == "Answer"
    payload = post.call_args.kwargs["json"]
    assert payload["options"] == {"num_ctx": lb.LOCAL_CONTEXT_LENGTH, "num_predict": 50}
    assert payload["stream"] is True  # read timeout applies between tokens, not to the whole call


def test_measured_speeds_recorded_from_final_chunk(monkeypatch):
    monkeypatch.setattr(lb, "MEASURED_SPEEDS", {})
    lb.record_speeds("m", {"prompt_eval_count": 1000, "prompt_eval_duration": 10e9,
                           "eval_count": 100, "eval_duration": 10e9})
    assert lb.MEASURED_SPEEDS["m"] == {"prefill_tps": 100.0, "gen_tps": 10.0}
    lb.record_speeds("m", {"prompt_eval_count": 1000, "prompt_eval_duration": 5e9,
                           "eval_count": 100, "eval_duration": 5e9})
    assert lb.MEASURED_SPEEDS["m"] == {"prefill_tps": 150.0, "gen_tps": 15.0}  # moving average
    lb.record_speeds("m", {"prompt_eval_count": 5, "prompt_eval_duration": 1e6})  # too small: ignored
    assert lb.MEASURED_SPEEDS["m"]["prefill_tps"] == 150.0


def test_cloud_complete_ignores_max_tokens():
    resp = MagicMock()
    resp.json.return_value = {"choices": [{"message": {"content": "ok"}}]}
    with patch.object(lb.requests, "post", return_value=resp) as post:
        lb.complete([{"role": "user", "content": "q"}], lb.cloud(), max_tokens=50)
    assert "options" not in post.call_args.kwargs["json"]
    assert post.call_args.args[0] == lb.GAIA_ENDPOINT


def test_ollama_down_raises_clear_error_and_never_calls_cloud():
    calls = []

    def fake_post(url, **_kw):
        calls.append(url)
        raise requests.ConnectionError("refused")

    with patch.object(lb.requests, "post", side_effect=fake_post):
        with pytest.raises(lb.LocalModelError, match="not reachable"):
            "".join(lb.stream_chat([{"role": "user", "content": "x"}], lb.local("qwen3:8b")))
    assert calls == [f"{lb.LOCAL_LLM_BASE_URL}/api/chat"]  # no GAIA fallback


def test_missing_model_error_names_pull_command():
    resp = MagicMock(status_code=404)
    resp.json.return_value = {"error": "model 'foo' not found"}
    with patch.object(lb.requests, "post", return_value=resp):
        with pytest.raises(lb.LocalModelError, match="ollama pull foo"):
            lb.complete([{"role": "user", "content": "x"}], lb.local("foo"))


def test_think_stripper_handles_split_tags():
    s = lb._ThinkStripper()
    pieces = ["Hel", "lo <thi", "nk>secret reasoning</th", "ink> world", ""]
    out = "".join(s.feed(p) for p in pieces) + s.flush()
    assert out == "Hello  world"


def test_backend_labels():
    assert lb.cloud().label == "GLM-5.3-Flash · cloud"
    assert lb.local("qwen3:8b").label == "qwen3:8b · local"


def ps_response(models):
    """A fake GET /api/ps reply."""
    resp = MagicMock()
    resp.json.return_value = {"models": models}
    return resp


def test_every_local_request_sets_the_context_window():
    """Ollama's default window (4096 without OLLAMA_CONTEXT_LENGTH) would silently cut long transcripts."""
    with patch.object(lb.requests, "post", return_value=ndjson_response("ok")) as post:
        lb.complete([{"role": "user", "content": "q"}], lb.local("gemma4:e4b"))
    assert post.call_args.kwargs["json"]["options"]["num_ctx"] == lb.LOCAL_CONTEXT_LENGTH


def test_loaded_window_reads_api_ps(monkeypatch):
    monkeypatch.setattr(lb, "_loaded_window", REAL_LOADED_WINDOW)
    with patch.object(lb.requests, "get", return_value=ps_response([{"name": "gemma4:e4b", "context_length": 4096}])) as get:
        assert lb._loaded_window("gemma4:e4b") == 4096
        assert lb._loaded_window("qwen3:8b") is None
    assert get.call_args.args[0] == f"{lb.LOCAL_LLM_BASE_URL}/api/ps"
    with patch.object(lb.requests, "get", side_effect=lb.requests.ConnectionError("down")):
        assert lb._loaded_window("gemma4:e4b") is None  # can't tell: go ahead


def test_a_model_loaded_with_a_smaller_window_is_reloaded_first(monkeypatch):
    """num_ctx only applies when Ollama loads the model; one loaded elsewhere with 4096 is unloaded first."""
    monkeypatch.setattr(lb, "_loaded_window", lambda model: 4096)
    monkeypatch.setattr(lb, "_NATIVE_CONTEXT", {"gemma4:e4b": 131072})
    with patch.object(lb.requests, "post", return_value=ndjson_response("ok")) as post:
        lb.complete([{"role": "user", "content": "q"}], lb.local("gemma4:e4b"))
    urls = [c.args[0] for c in post.call_args_list]
    assert urls == [f"{lb.LOCAL_LLM_BASE_URL}/api/generate", f"{lb.LOCAL_LLM_BASE_URL}/api/chat"]
    assert post.call_args_list[0].kwargs["json"] == {"model": "gemma4:e4b", "keep_alive": 0}


def test_a_model_already_at_its_window_is_not_reloaded(monkeypatch):
    monkeypatch.setattr(lb, "_NATIVE_CONTEXT", {"small:1b": 8192})
    for model, loaded in (("gemma4:e4b", lb.LOCAL_CONTEXT_LENGTH), ("small:1b", 8192)):  # full, or the model's maximum
        monkeypatch.setattr(lb, "_loaded_window", lambda m, loaded=loaded: loaded)
        with patch.object(lb.requests, "post", return_value=ndjson_response("ok")) as post:
            lb.complete([{"role": "user", "content": "q"}], lb.local(model))
        assert [c.args[0] for c in post.call_args_list] == [f"{lb.LOCAL_LLM_BASE_URL}/api/chat"]
