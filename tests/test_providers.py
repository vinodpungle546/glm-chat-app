#!/usr/bin/env python3
"""Model registry + provider interface: Ollama success / timeout / missing model / refused,
status badges, downloads with progress and cancel, and the unchanged cloud requests."""

import json
import threading
import time
from unittest.mock import MagicMock, patch

import pytest
import requests

import agent
import llm_backends as lb
import model_registry as mr


def ndjson(*chunks, status=200, done=True):
    resp = MagicMock()
    resp.status_code = status
    resp.__enter__.return_value = resp
    resp.__exit__.return_value = False
    lines = [json.dumps({"message": {"content": c}, "done": False}) for c in chunks]
    if done:
        lines.append(json.dumps({"message": {"content": ""}, "done": True}))
    resp.iter_lines.return_value = lines
    return resp


def error_response(status, error):
    resp = MagicMock()
    resp.status_code = status
    resp.json.return_value = {"error": error}
    return resp


# --- Ollama provider ----------------------------------------------------------

def test_ollama_provider_streams_from_local_api_with_model_name():
    provider = mr.get_provider("local:gemma4:e4b")
    with patch.object(lb.requests, "post", return_value=ndjson("Hel", "lo")) as post:
        assert "".join(provider.stream([{"role": "user", "content": "hi", "model": "x · local"}])) == "Hello"
    url = post.call_args.args[0]
    payload = post.call_args.kwargs["json"]
    assert url == f"{lb.LOCAL_LLM_BASE_URL}/api/chat"
    assert payload["model"] == "gemma4:e4b" and payload["think"] is False
    assert payload["messages"] == [{"role": "user", "content": "hi"}]  # UI metadata never sent


def test_ollama_provider_complete_passes_options():
    provider = mr.OllamaProvider("gemma4:e4b")
    with patch.object(lb.requests, "post", return_value=ndjson("<think>x</think>Answer")) as post:
        assert provider.complete([{"role": "user", "content": "q"}], max_tokens=50, temperature=0) == "Answer"
    assert post.call_args.kwargs["json"]["options"] == {"num_ctx": lb.LOCAL_CONTEXT_LENGTH, "num_predict": 50, "temperature": 0}


def test_ollama_timeout_gives_clear_error():
    provider = mr.OllamaProvider("gemma4:e4b")
    with patch.object(lb.requests, "post", side_effect=requests.ReadTimeout("read timed out")):
        with pytest.raises(lb.LocalModelError, match="timed out"):
            "".join(provider.stream([{"role": "user", "content": "q"}]))


def test_ollama_missing_model_names_the_download():
    provider = mr.OllamaProvider("gemma4:e4b")
    with patch.object(lb.requests, "post", return_value=error_response(404, 'model "gemma4:e4b" not found')):
        with pytest.raises(lb.LocalModelError, match="ollama pull gemma4:e4b"):
            provider.complete([{"role": "user", "content": "q"}])


def test_ollama_connection_refused_never_falls_back_to_cloud():
    provider = mr.OllamaProvider("gemma4:e4b")
    urls = []

    def refuse(url, **_kw):
        urls.append(url)
        raise requests.ConnectionError("Connection refused")

    with patch.object(lb.requests, "post", side_effect=refuse):
        with pytest.raises(lb.LocalModelError, match="not reachable"):
            provider.complete([{"role": "user", "content": "q"}])
    assert all("11434" in u or "localhost" in u for u in urls) and not any("globant" in u for u in urls)


# --- Registry and status ------------------------------------------------------

def test_registry_lists_gemma_from_config_not_code():
    registry = mr.load_registry()
    gemma = mr.find("local:gemma4:e4b", registry)
    assert gemma["label"] == "Gemma 4 E4B" and gemma["group"] == "local" and gemma["size_gb"] == 9.6
    cloud = mr.find("cloud:gaia", registry)
    assert cloud["model"] == lb.GAIA_MODEL and cloud["group"] == "cloud"


def test_status_badges_ready_not_downloaded_and_ollama_down():
    state = {"running": True, "version": "0.40.16", "installed": {
        "qwen3:8b": {"name": "qwen3:8b", "size": 5_200_000_000, "details": {"family": "qwen3"}},
        "nomic-embed-text:latest": {"name": "nomic-embed-text:latest", "details": {"family": "nomic-bert"}},
        "llama3.1:8b": {"name": "llama3.1:8b", "size": 4_900_000_000, "details": {"family": "llama"}}}}
    models = {m["id"]: m for m in mr.list_models(state=state)["models"]}
    assert models["local:qwen3:8b"]["status"] == "ready"
    assert models["local:gemma4:e4b"]["status"] == "not_downloaded"
    assert models["local:llama3.1:8b"]["status"] == "ready"  # installed but not in models.json
    assert "local:nomic-embed-text:latest" not in models     # embeddings can't chat
    down = {m["id"]: m for m in mr.list_models(state={"running": False, "version": "", "installed": {}})["models"]}
    assert down["local:gemma4:e4b"]["status"] == down["local:qwen3:8b"]["status"] == "ollama_down"


def test_old_ollama_is_flagged_for_gemma():
    state = {"running": True, "version": "0.19.2", "installed": {}}
    gemma = next(m for m in mr.list_models(state=state)["models"] if m["id"] == "local:gemma4:e4b")
    assert gemma["ollama_too_old"] is True


def test_pick_local_model_prefers_registry_default():
    state = {"running": True, "version": "1", "installed": {"qwen3:8b": {"name": "qwen3:8b"},
                                                            "gemma4:e4b": {"name": "gemma4:e4b"}}}
    assert mr.pick_local_model(state=state) == "gemma4:e4b"
    assert mr.pick_local_model("qwen3:8b", state=state) == "qwen3:8b"
    assert mr.pick_local_model(state={"running": False, "version": "", "installed": {}}) is None


# --- Downloads ----------------------------------------------------------------

class FakePull:
    """Streams ollama /api/pull progress lines; waits on a gate between lines."""

    def __init__(self, lines, gate=None):
        self.lines, self.gate, self.status_code = lines, gate, 200

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def iter_lines(self, decode_unicode=True):
        for line in self.lines:
            if self.gate:
                self.gate.wait(2)
            yield json.dumps(line)


def wait_for(manager, model, state, seconds=3):
    end = time.time() + seconds
    while time.time() < end:
        if (manager.get(model) or {}).get("state") == state:
            return manager.get(model)
        time.sleep(0.01)
    return manager.get(model)


def test_pull_reports_progress_and_completes():
    lines = [{"status": "pulling manifest"},
             {"status": "pulling a", "digest": "a", "total": 100, "completed": 50},
             {"status": "pulling a", "digest": "a", "total": 100, "completed": 100},
             {"status": "success"}]
    manager = mr.PullManager()
    with patch.object(mr.requests, "post", return_value=FakePull(lines)) as post:
        manager.start("gemma4:e4b")
        done = wait_for(manager, "gemma4:e4b", "done")
    assert done["percent"] == 100.0 and done["completed"] == 100
    assert post.call_args.kwargs["json"] == {"model": "gemma4:e4b", "stream": True}
    assert post.call_args.kwargs["timeout"] == (5, 600)  # no overall timeout on a ~10 GB download


def test_pull_can_be_cancelled():
    gate = threading.Event()
    lines = [{"status": "pulling a", "digest": "a", "total": 100, "completed": i} for i in range(0, 100, 10)]
    manager = mr.PullManager()
    with patch.object(mr.requests, "post", return_value=FakePull(lines, gate)):
        manager.start("gemma4:e4b")
        manager.cancel("gemma4:e4b")
        gate.set()
        assert wait_for(manager, "gemma4:e4b", "cancelled")["state"] == "cancelled"


def test_pull_when_ollama_is_down():
    manager = mr.PullManager()
    with patch.object(mr.requests, "post", side_effect=requests.ConnectionError("refused")):
        manager.start("gemma4:e4b")
        failed = wait_for(manager, "gemma4:e4b", "error")
    assert "Ollama is not running" in failed["error"]


# --- Cloud requests through the new agent are unchanged ------------------------

def sse(*chunks):
    resp = MagicMock()
    resp.__enter__.return_value = resp
    resp.__exit__.return_value = False
    resp.iter_lines.return_value = [f"data: {json.dumps({'choices': [{'delta': {'content': c}}]})}"
                                    for c in chunks] + ["data: [DONE]"]
    return resp


def test_agent_cloud_request_is_identical_to_the_pinned_one(monkeypatch):
    monkeypatch.setattr(agent.connectors, "load_connectors", lambda: [])
    project = {"id": "p", "name": "T", "instructions": "", "skills": [], "tools": [], "memory": [], "documents": []}
    state = agent.TurnState(project=project, messages=[{"role": "user", "content": "hi"}],
                            provider=mr.get_provider("cloud:gaia"))
    with patch.object(lb.requests, "post", return_value=sse("Hel", "lo")) as post:
        assert agent.generate_reply(state, "hi") == "Hello"
    args, kwargs = post.call_args
    assert args == (lb.GAIA_ENDPOINT,)
    assert set(kwargs) == {"headers", "json", "stream", "timeout"} and kwargs["timeout"] == 120
    assert kwargs["headers"] == {"Authorization": f"Bearer {lb.GAIA_API_KEY}", "Content-Type": "application/json"}
    assert list(kwargs["json"]) == ["model", "messages", "stream"] and kwargs["json"]["model"] == lb.GAIA_MODEL


def test_agent_cloud_memory_request_keeps_payload_order(monkeypatch):
    project = {"id": "p", "memory": []}
    state = agent.TurnState(project=project, provider=mr.get_provider("cloud:gaia"),
                            messages=[{"role": "user", "content": "hi"}, {"role": "assistant", "content": "hello"}])
    resp = MagicMock()
    resp.json.return_value = {"choices": [{"message": {"content": '["User likes tea"]'}}]}
    with patch.object(lb.requests, "post", return_value=resp) as post:
        assert agent.extract_memories(state) == ["User likes tea"]
    assert list(post.call_args.kwargs["json"]) == ["model", "stream", "messages"]
    assert post.call_args.kwargs["timeout"] == 60


# --- Vision models (on-screen names fallback) -----------------------------------------

VISION_REGISTRY = {"default": "local:qwen3:8b", "models": [], "vision_default": "qwen3-vl:8b-instruct",
                   "vision_models": [{"model": "qwen3-vl:8b-instruct", "label": "Qwen3-VL 8B", "size_gb": 6.1},
                                     {"model": "gemma4:e4b", "label": "Gemma 4 E4B", "size_gb": 9.6}]}


def vision_state(*names):
    return {"running": True, "version": "0.40.16", "installed": {n: {"name": n, "details": {}} for n in names}}


@pytest.fixture
def caps(monkeypatch):
    table = {"qwen3:8b": ["completion", "tools"], "gemma4:e4b": ["completion", "vision"],
             "qwen3-vl:8b-instruct": ["completion", "vision"], "llava:7b": ["completion", "vision"]}
    monkeypatch.setattr(mr, "capabilities", lambda name: table.get(name, ["completion"]))
    monkeypatch.setattr(mr, "load_registry", lambda path=None: VISION_REGISTRY)


def test_vision_models_lists_suggestions_and_installed_image_models_only(caps):
    data = mr.vision_models(vision_state("qwen3:8b", "gemma4:e4b", "llava:7b"))
    by = {m["model"]: m for m in data["models"]}
    assert set(by) == {"qwen3-vl:8b-instruct", "gemma4:e4b", "llava:7b"}  # never the text-only qwen3:8b
    assert by["qwen3-vl:8b-instruct"]["recommended"] and not by["qwen3-vl:8b-instruct"]["installed"]
    assert by["gemma4:e4b"]["installed"] and by["llava:7b"]["installed"]


def test_pick_vision_model_never_picks_a_text_only_model(caps):
    assert mr.pick_vision_model("qwen3:8b", vision_state("qwen3:8b", "gemma4:e4b")) == "gemma4:e4b"
    assert mr.pick_vision_model("", vision_state("qwen3:8b", "gemma4:e4b", "qwen3-vl:8b-instruct")) == \
        "qwen3-vl:8b-instruct"
    assert mr.pick_vision_model("llava:7b", vision_state("llava:7b", "qwen3-vl:8b-instruct")) == "llava:7b"
    assert mr.pick_vision_model("", vision_state("qwen3:8b")) is None


def test_the_shipped_registry_names_a_vision_default_it_lists():
    registry = mr.load_registry()
    assert registry["vision_default"] in [m["model"] for m in registry["vision_models"]]
