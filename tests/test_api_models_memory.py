"""API: cloud providers in Settings → Models (keys never in prefs or logs), chatting with Claude and
OpenAI models, per-provider approval in Auto mode, the unified memory endpoints, memory in chat
and notes, voice settings, and the on-screen name / backfill endpoints. Synthetic data only."""

from __future__ import annotations

import io
import json
import logging
import zipfile
from unittest.mock import patch

import pytest

import cloud_providers as cp
import llm_backends as lb
import meeting_notes
import meetings
import memory_store
import people as people_store
from tests import fake_cloud
from tests.fake_cloud import FakeCloud
from tests.test_api import client, events, ollama_reply  # noqa: F401  (fixtures)


@pytest.fixture
def providers(client, monkeypatch):  # noqa: F811
    with FakeCloud() as url:
        monkeypatch.setenv("ANTHROPIC_PROVIDER_URL", url)
        monkeypatch.setenv("OPENAI_PROVIDER_URL", f"{url}/openai/v1")
        yield client


def add(client, kind, key):
    added = client.post("/api/providers", json={"kind": kind, "key": key}).json()
    result = client.post(f"/api/providers/{added['id']}/test").json()
    return added, result


def test_add_provider_test_key_and_choose_models(providers, caplog):
    caplog.set_level(logging.DEBUG)
    added, result = add(providers, "anthropic", "sk-ant-test")
    assert added["has_key"] and added["key_hint"] == "••••test" and result["ok"]
    providers.patch("/api/providers/anthropic", json={"enabled": ["claude-sonnet-5-5"]})
    models = {m["id"]: m for m in providers.get("/api/models").json()["models"]}
    assert models["cloud:anthropic:claude-sonnet-5-5"]["label"] == "Claude Sonnet 5.5"
    assert "cloud:anthropic:claude-opus-5-5" not in models
    providers.put("/api/preferences", json={"selected_model": "cloud:anthropic:claude-sonnet-5-5"})
    everything = json.dumps(providers.get("/api/preferences").json()) + json.dumps(providers.get("/api/providers").json())
    assert "sk-ant-test" not in everything and "sk-ant-test" not in caplog.text
    bad, bad_result = add(providers, "openai", "sk-wrong")
    assert not bad_result["ok"] and bad_result["kind"] == "invalid_key"
    providers.delete("/api/providers/anthropic")
    assert providers.get("/api/preferences").json()["selected_model"] == ""


def test_chat_with_claude_and_openai_models(providers):
    add(providers, "anthropic", "sk-ant-test")
    add(providers, "openai", "sk-openai-test")
    providers.patch("/api/providers/openai", json={"enabled": ["gpt-5.5"]})  # OpenAI models are picked by the user
    sid = providers.post("/api/sessions", json={}).json()["id"]
    providers.put("/api/preferences", json={"confirm_cloud_sensitive": False})
    out = events(providers.post(f"/api/sessions/{sid}/messages",
                                json={"content": "hello claude", "model_id": "cloud:anthropic:claude-opus-5-5"}))
    assert out[0]["model"] == "Claude Opus 5.5 · cloud" and out[-1]["message"]["content"] == "Fake reply about: hello claude"
    out = events(providers.post(f"/api/sessions/{sid}/messages",
                                json={"content": "hello gpt", "model_id": "cloud:openai:gpt-5.5"}))
    assert out[-1]["message"]["model"] == "gpt-5.5 · cloud" and out[-1]["message"]["content"].startswith("Fake reply")
    sent = fake_cloud.REQUESTS[-1]["body"]["messages"]
    assert all(set(m) == {"role", "content"} for m in sent)  # the per-message model caption isn't sent


def test_cloud_errors_reach_the_chat(providers):
    added, _ = add(providers, "anthropic", "sk-ant-test")
    providers.patch("/api/providers/anthropic", json={"manual_model": "claude-limit"})
    providers.put("/api/preferences", json={"confirm_cloud_sensitive": False})
    sid = providers.post("/api/sessions", json={}).json()["id"]
    out = events(providers.post(f"/api/sessions/{sid}/messages", json={"content": "hi", "model_id": "cloud:anthropic:claude-limit"}))
    assert out[-1]["type"] == "error" and out[-1]["kind"] == "cloud" and out[-1]["error_kind"] == "rate_limited"


def test_approval_is_per_provider_and_names_the_model(providers):
    add(providers, "anthropic", "sk-ant-test")
    sid = providers.post("/api/sessions", json={}).json()["id"]
    text = "Our salary bands are confidential."
    with patch.object(lb.requests, "post") as gaia:
        first = events(providers.post(f"/api/sessions/{sid}/messages", json={"content": text, "model_id": "cloud:gaia"}))
        assert first[-1]["type"] == "confirm" and first[-1]["model"] == "GLM-5.3 Flash" and gaia.call_count == 0
        gaia.return_value = ollama_reply()  # not used: GAIA is answered below
    def gaia_stream(*_a, **_k):
        from unittest.mock import MagicMock
        resp = MagicMock(status_code=200)
        resp.iter_lines.return_value = [f"data: {json.dumps({'choices': [{'delta': {'content': 'ok'}}]})}", "data: [DONE]"]
        resp.__enter__.return_value = resp
        resp.json.return_value = {"choices": [{"message": {"content": "[]"}}]}  # memory extraction
        return resp
    with patch.object(lb.requests, "post", side_effect=gaia_stream):
        approved = events(providers.post(f"/api/sessions/{sid}/messages",
                                         json={"content": text, "model_id": "cloud:gaia", "confirm": "cloud"}))
        assert approved[-1]["type"] == "done"
        again_gaia = events(providers.post(f"/api/sessions/{sid}/messages", json={"content": text, "model_id": "cloud:gaia"}))
        assert again_gaia[-1]["type"] == "done"  # approved for GAIA in this chat
    claude = events(providers.post(f"/api/sessions/{sid}/messages",
                                   json={"content": text, "model_id": "cloud:anthropic:claude-opus-5-5"}))
    assert claude[-1]["type"] == "confirm" and claude[-1]["model"] == "Claude Opus 5.5"  # not approved for Claude
    assert claude[-1]["provider"] == "Anthropic (Claude)"
    assert not [r for r in fake_cloud.REQUESTS if r["api"] == "anthropic" and r["path"] == "/v1/messages"]


def test_memory_endpoints_and_export(client):  # noqa: F811
    person = client.post("/api/people", json={"name": "Owen Hart", "role": "QA tester"}).json()
    area = client.post("/api/memory/entries", json={"kind": "area", "name": "Hiring"}).json()
    fact = client.post(f"/api/memory/entries/{area['id']}/facts", json={"text": "Interviews are on Wednesdays."}).json()
    assert fact["source"] == "user" and fact["date"]
    client.post(f"/api/memory/entries/{area['id']}/facts", json={"text": "Interviews are on Wednesdays mornings."})
    data = client.get("/api/memory").json()
    assert data["counts"]["person"] == 1 and data["counts"]["area"] == 1 and data["sources"]["claude.ai"]
    assert data["duplicates"]["facts"][area["id"]]
    merged = client.post(f"/api/memory/entries/{area['id']}/facts/merge",
                         json={"ids": data["duplicates"]["facts"][area["id"]][0]}).json()
    assert merged["edited"]
    client.patch(f"/api/memory/entries/{person['id']}", json={"team": "Payments", "aliases": ["Owen"]})
    assert client.get("/api/memory/search", params={"q": "payments"}).json()[0]["name"] == "Owen Hart"
    response = client.get("/api/memory/export")
    assert response.headers["content-type"] == "application/zip"
    names = zipfile.ZipFile(io.BytesIO(response.content)).namelist()
    assert "memory/people/owen-hart.md" in names and "memory/areas/hiring.md" in names
    assert client.delete(f"/api/memory/entries/{area['id']}").json() == {"ok": True}


def test_project_memory_lives_in_the_store(client):  # noqa: F811
    project = client.get("/api/projects").json()[0]
    client.patch(f"/api/projects/{project['id']}", json={"memory": ["Sprint reviews are on Fridays"]})
    assert client.get(f"/api/projects/{project['id']}").json()["memory"] == ["Sprint reviews are on Fridays"]
    entry = next(e for e in client.get("/api/memory").json()["entries"] if e["kind"] == "project")
    assert entry["quorum_project"] == project["id"] and entry["facts"][0]["source"] == "user"


def test_chat_learns_facts_into_the_store_and_lists_memory_used(client):  # noqa: F811
    person = client.post("/api/people", json={"name": "Cian Byrne", "role": "Release manager"}).json()
    memory_store.add_user_fact(person["id"], "Runs the release train every second Tuesday.")
    sid = client.post("/api/sessions", json={}).json()["id"]
    replies = [ollama_reply("He runs the train."), ollama_reply('["Atlas launches in March"]')]
    with patch.object(lb.requests, "post", side_effect=replies + [ollama_reply("[]")] * 4):
        for text in ("When does Cian Byrne run the release train?", "Thanks"):
            out = events(client.post(f"/api/sessions/{sid}/messages", json={"content": text, "model_id": "local:gemma4:e4b"}))
            if text.startswith("When"):
                used = next(e for e in out if e["type"] == "memory_used")["facts"]
                assert "Cian Byrne: Release manager" in used
                assert out[-1]["message"]["memory_used"] == used


def test_notes_use_memory_for_full_names_project_and_me(client, monkeypatch):  # noqa: F811
    owen = client.post("/api/people", json={"name": "Owen Hart", "role": "QA tester"}).json()
    memory_store.update(owen["id"], {"team": "Payments"})
    me = people_store.upsert("Alex Rivera")
    with memory_store.LOCK:
        store = memory_store.load_store()
        memory_store.get(me["id"], store)["is_user"] = True
        memory_store.ensure(store, "project", "Atlas")
        memory_store.save_store(store)
    from api import main
    memory = main.notes_memory({})
    assert memory["me"] == "Alex Rivera" and "Atlas" in memory["projects"]
    transcript = {"title": "Atlas sync", "speakers": ["SPEAKER_00", "SPEAKER_01"], "duration": 60, "created_at": "2026-11-20T10:00:00",
                  "segments": [{"speaker": "SPEAKER_00", "start": 0, "end": 5, "text": "Atlas regression: Owen, please rerun it by Friday."},
                               {"speaker": "SPEAKER_01", "start": 5, "end": 9, "text": "Sure, I'll rerun the Atlas regression."},
                               {"speaker": "SPEAKER_00", "start": 9, "end": 12, "text": "And I will update the Atlas plan."}]}
    names = {"SPEAKER_00": "Alex Rivera", "SPEAKER_01": "Owen"}
    seen = []

    def complete(messages):
        seen.append(messages[-1]["content"])
        return json.dumps({"tldr": ["Regression rerun"], "decisions": [], "discussion": [], "risks": [], "open_questions": [],
                           "action_items": [{"action": "Rerun the Atlas regression", "owner": "Owen", "due_date": "Friday",
                                             "priority": "", "timestamp": "00:05"},
                                            {"action": "Update the Atlas plan", "owner": "me", "due_date": "", "priority": "",
                                             "timestamp": "00:09"}],
                           "next_steps": [], "details": {}, "meeting": {"title": "Atlas sync", "project": "", "absentees": []}})
    result = meeting_notes.generate(transcript, names, {}, complete, memory=memory)
    prompt = seen[0]
    assert "Owen Hart — QA tester, Payments team" in prompt and "The user is Alex Rivera" in prompt
    notes = result["notes"]
    assert [a["owner"] for a in notes["action_items"]] == ["Owen Hart", "Alex Rivera"]
    assert notes["meeting"]["project"] == "Atlas"
    assert {a["name"] for a in notes["meeting"]["attendees"]} == {"Alex Rivera", "Owen Hart"}  # nobody added


def test_voice_settings_and_forgetting(client):  # noqa: F811
    person = people_store.learn("t1", "SPEAKER_00", "Owen Hart", "", [0.1, 0.2], source="screen")
    assert client.get("/api/people").json()[0]["voiceprint_count"] == 1
    assert client.delete(f"/api/people/{person['id']}/voice").json()["voiceprint_count"] == 0
    people_store.learn("t1", "SPEAKER_00", "Owen Hart", "", [0.1, 0.2])
    assert client.delete("/api/voiceprints").json() == {"deleted": 1}
    client.put("/api/preferences", json={"voiceprints": False})
    assert client.post("/api/meetings/voiceprints/backfill").status_code == 409


def test_backfill_queues_only_old_transcripts_with_recordings(client, monkeypatch):  # noqa: F811
    metas = [{"id": "a" * 16, "title": "Old"}, {"id": "b" * 16, "title": "New"}, {"id": "c" * 16, "title": "Gone"},
             {"id": "d" * 16, "title": "Notes", "source_type": "text"}]
    full = {"a" * 16: {}, "b" * 16: {"speaker_embeddings": {"S": [1.0]}}, "c" * 16: {}}
    monkeypatch.setattr(meetings, "transcripts", lambda: metas)
    monkeypatch.setattr(meetings, "transcript", lambda tid: full[tid])

    def backfill(tid):
        if tid == "c" * 16:
            raise RuntimeError("The recording for this transcript is no longer available.")
        return {"id": "job1"}

    monkeypatch.setattr(meetings, "backfill_voiceprints", backfill)
    result = client.post("/api/meetings/voiceprints/backfill").json()
    assert [q["title"] for q in result["queued"]] == ["Old"]
    assert result["skipped"][0]["title"] == "Gone" and "no longer available" in result["skipped"][0]["reason"]


def test_new_jobs_pass_on_screen_options(client, monkeypatch):  # noqa: F811
    seen = {}
    monkeypatch.setattr(meetings, "start", lambda path, speakers=None, screen=None, models=None:
                        seen.update(screen=screen, models=models) or {"id": "j"})
    client.post("/api/meetings/jobs", json={"path": "/x/call.mov"})
    # The vision review always runs (vision_cap None = 4 frames per speaker, 12 to 48).
    assert seen["screen"] == {"enabled": True, "vision_model": "gemma4:e4b", "vision_cap": None, "debug": False,
                              "participants": True}
    client.put("/api/preferences", json={"vision_fallback": False, "screen_debug": True, "vision_frames": 30})
    client.post("/api/meetings/jobs", json={"path": "/x/call.mov"})
    assert seen["screen"] == {"enabled": True, "vision_model": "gemma4:e4b", "vision_cap": 30, "debug": True,
                              "participants": True}
    from api import main
    assert main.review_frames(2) == 12 and main.review_frames(6) == 24 and main.review_frames(20) == 48
    client.put("/api/preferences", json={"selected_model": "cloud:gaia", "vision_fallback": True})
    client.post("/api/meetings/jobs", json={"path": "/x/call.mov"})
    assert seen["screen"]["vision_model"] == "gemma4:e4b"  # never a cloud model
    assert cp.parse_model_id("cloud:gaia") is None
    # A Settings page from before the review saves the old number box (vision_cap): it becomes the frame count.
    assert client.put("/api/preferences", json={"vision_cap": 18}).json()["vision_frames"] == 18
    assert client.put("/api/preferences", json={"vision_frames": 500}).status_code == 400
