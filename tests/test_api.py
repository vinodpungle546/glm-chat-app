#!/usr/bin/env python3
"""The HTTP API used by the Next.js app (FastAPI TestClient; models and transcriber faked)."""

import json
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

import connectors
import llm_backends as lb
import meetings
import model_registry as mr
import people as people_store
import projects
from api import main


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "DATA_DIR", tmp_path)
    monkeypatch.setattr(main, "PREFS_FILE", tmp_path / "preferences.json")
    monkeypatch.setattr(main, "LABELS_DIR", tmp_path / "labels")
    monkeypatch.setattr(main, "SUMMARIES_DIR", tmp_path / "summaries")
    monkeypatch.setattr(main, "ATTENDEES_DIR", tmp_path / "attendees")
    monkeypatch.setattr(main, "SESSIONS_DIR", tmp_path / "sessions")
    monkeypatch.setattr(projects, "PROJECTS_DIR", tmp_path / "projects")
    monkeypatch.setattr(connectors, "CONNECTORS_FILE", tmp_path / "connectors.json")
    monkeypatch.setattr(people_store, "PEOPLE_FILE", tmp_path / "people.json")
    monkeypatch.setattr(mr, "ollama_state", lambda: {"running": True, "version": "0.40.16", "installed": {
        "gemma4:e4b": {"name": "gemma4:e4b", "size": 9_500_000_000, "details": {"family": "gemma4"}}}})
    isolate_new_storage(tmp_path, monkeypatch)
    main._CACHES.clear()
    with TestClient(main.app) as c:
        yield c


def fake_embed(texts, model="nomic-embed-text"):
    """Deterministic bag-of-words vectors, so semantic search works without Ollama."""
    import hashlib
    import re as _re
    out = []
    for text in texts:
        vec = [0.0] * 64
        for word in _re.findall(r"[a-z]{3,}", text.lower()):
            vec[int(hashlib.md5(word.encode()).hexdigest(), 16) % 64] += 1.0
        out.append(vec)
    return out


def isolate_new_storage(tmp_path, monkeypatch):
    """Imports, embeddings, template copies and secrets go to tmp_path; embeddings are faked."""
    import imports
    import long_context
    import meeting_notes
    import search_index
    monkeypatch.setattr(imports, "IMPORTS_DIR", tmp_path / "imports")
    monkeypatch.setattr(meeting_notes, "OVERRIDE_DIR", tmp_path / "skills" / "meeting-notes")
    store = search_index.VectorStore(tmp_path / "index" / "embeddings.json")
    monkeypatch.setattr(main, "VECTORS", store)
    monkeypatch.setattr(main, "INDEXER", search_index.Indexer(store))
    monkeypatch.setattr(long_context, "embed", fake_embed)
    monkeypatch.setenv("SECRETS_BACKEND", "file")
    monkeypatch.setenv("SECRETS_FILE", str(tmp_path / "secrets.json"))


def events(response) -> list[dict]:
    return [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: ")]


def ollama_reply(*chunks):
    resp = MagicMock()
    resp.status_code = 200
    resp.__enter__.return_value = resp
    resp.__exit__.return_value = False
    resp.iter_lines.return_value = [json.dumps({"message": {"content": c}, "done": False}) for c in chunks] + [
        json.dumps({"message": {"content": ""}, "done": True})]
    return resp


def test_models_have_status_and_selection_is_remembered(client):
    data = client.get("/api/models").json()
    by_id = {m["id"]: m for m in data["models"]}
    assert by_id["local:gemma4:e4b"]["status"] == "ready"
    assert by_id["local:qwen3:8b"]["status"] == "not_downloaded"
    assert data["selected"] == data["default"] == "cloud:gaia"  # registry default: chats start on the cloud model
    client.put("/api/preferences", json={"selected_model": "local:gemma4:e4b"})
    assert client.get("/api/models").json()["selected"] == "local:gemma4:e4b"
    assert client.put("/api/preferences", json={"selected_model": "nope"}).status_code == 400


def test_session_create_rename_search_delete(client):
    a = client.post("/api/sessions", json={}).json()
    b = client.post("/api/sessions", json={}).json()
    assert client.patch(f"/api/sessions/{a['id']}", json={"title": "Budget review"}).json()["title"] == "Budget review"
    found = client.get("/api/sessions", params={"q": "budget"}).json()
    assert [s["id"] for s in found] == [a["id"]]
    assert client.delete(f"/api/sessions/{b['id']}").json() == {"ok": True}
    assert client.get(f"/api/sessions/{b['id']}").status_code == 404
    assert client.patch(f"/api/sessions/{a['id']}", json={"title": "  "}).status_code == 400


def test_chat_turn_streams_on_local_model_and_saves(client):
    sid = client.post("/api/sessions", json={}).json()["id"]
    with patch.object(lb.requests, "post", return_value=ollama_reply("Hi ", "there")) as post:
        out = events(client.post(f"/api/sessions/{sid}/messages",
                                 json={"content": "hello", "model_id": "local:gemma4:e4b"}))
    assert [e["type"] for e in out] == ["start", "delta", "delta", "done"]
    assert out[0]["model"] == "Gemma 4 E4B · local"
    assert post.call_args.kwargs["json"]["model"] == "gemma4:e4b"
    saved = client.get(f"/api/sessions/{sid}").json()
    assert [m["content"] for m in saved["messages"]] == ["hello", "Hi there"]
    assert saved["messages"][1]["model_id"] == "local:gemma4:e4b" and saved["title"] == "hello"


def test_switching_model_mid_session(client):
    sid = client.post("/api/sessions", json={}).json()["id"]
    with patch.object(lb.requests, "post", return_value=ollama_reply("A")):
        client.post(f"/api/sessions/{sid}/messages", json={"content": "one", "model_id": "local:gemma4:e4b"})
    monkeypatch_state = {"running": True, "version": "1", "installed": {
        "gemma4:e4b": {"name": "gemma4:e4b"}, "qwen3:8b": {"name": "qwen3:8b"}}}
    with patch.object(mr, "ollama_state", lambda: monkeypatch_state), \
            patch.object(lb.requests, "post", return_value=ollama_reply("B")) as post:
        out = events(client.post(f"/api/sessions/{sid}/messages", json={"content": "two", "model_id": "local:qwen3:8b"}))
    assert out[-1]["message"]["model"] == "Qwen3 8B · local"
    sent = post.call_args_list[0].kwargs["json"]  # (the last call is memory extraction)
    assert sent["model"] == "qwen3:8b"
    assert [m["content"] for m in sent["messages"] if m["role"] != "system"] == ["one", "A", "two"]


def test_cloud_turn_with_sensitive_text_asks_first_then_runs_locally(client):
    sid = client.post("/api/sessions", json={}).json()["id"]
    with patch.object(lb.requests, "post") as post:
        out = events(client.post(f"/api/sessions/{sid}/messages",
                                 json={"content": "This is confidential: salary data", "model_id": "cloud:gaia"}))
        assert post.call_count == 0  # nothing sent anywhere before the user answers
    assert out == [{"type": "confirm", "kind": "sensitive", "reasons": out[0]["reasons"], "local_model": "gemma4:e4b",
                    "model": "GLM-5.3 Flash", "provider": "Globant GAIA", "connectors": []}]
    assert any("confidential" in r for r in out[0]["reasons"])
    with patch.object(lb.requests, "post", return_value=ollama_reply("ok")) as post:
        done = events(client.post(f"/api/sessions/{sid}/messages", json={
            "content": "This is confidential: salary data", "model_id": "cloud:gaia", "confirm": "local"}))
    assert done[-1]["type"] == "done" and "11434" in post.call_args.args[0]
    assert client.get(f"/api/sessions/{sid}").json()["confidential"] is True


def test_local_model_errors_are_reported_and_message_can_be_retried(client):
    import requests as rq
    sid = client.post("/api/sessions", json={}).json()["id"]
    with patch.object(lb.requests, "post", side_effect=rq.ConnectionError("refused")):
        out = events(client.post(f"/api/sessions/{sid}/messages", json={"content": "q", "model_id": "local:gemma4:e4b"}))
    assert out[-1]["type"] == "error" and out[-1]["kind"] == "local_model"
    assert "not reachable" in out[-1]["message"]
    assert client.get(f"/api/sessions/{sid}").json()["messages"] == []


def test_slash_shows_skill_menu(client):
    pid = client.get("/api/projects").json()[0]["id"]
    client.patch(f"/api/projects/{pid}", json={"skills": [{"name": "Tables", "description": "", "content": "Use tables"}]})
    sid = client.post("/api/sessions", json={}).json()["id"]
    out = events(client.post(f"/api/sessions/{sid}/messages", json={"content": "/", "model_id": "local:gemma4:e4b"}))
    assert "Tables" in out[-1]["message"]["content"]


def test_projects_memory_skills_and_connectors(client):
    project = client.post("/api/projects", json={"name": "Client X"}).json()
    client.put("/api/preferences", json={"active_project": project["id"]})
    updated = client.patch(f"/api/projects/{project['id']}", json={
        "memory": ["Owen is QA", " "], "skills": [{"name": "Minutes", "content": "Write minutes"}]}).json()
    assert updated["memory"] == ["Owen is QA"] and updated["skills"][0]["name"] == "Minutes"
    assert [p["active"] for p in client.get("/api/projects").json() if p["id"] == project["id"]] == [True]
    added = client.post("/api/connectors", json={"kind": "mcp", "name": "Files", "url": "http://localhost:9000/mcp"}).json()
    assert [c["name"] for c in client.get("/api/connectors").json()] == ["Files"]
    client.delete(f"/api/connectors/{added['id']}")
    assert client.get("/api/connectors").json() == []
    assert client.delete("/api/projects/default").status_code == 400


def test_upload_and_import_reject_unsupported_types(client):
    bad = client.post("/api/meetings/uploads", json={"name": "notes.exe", "size": 10})
    assert bad.status_code == 415 and "Unsupported file type" in bad.json()["detail"]
    bad = client.post("/api/meetings/import", files={"file": ("x.exe", b"MZ")})
    assert bad.status_code == 415


def test_transcriber_down_gives_503_with_fix(client, monkeypatch):
    def down():
        raise meetings.TranscriberUnavailable("Local transcriber not reachable. Start it with scripts/start_local_ai.sh")
    monkeypatch.setattr(meetings, "recordings", down)
    response = client.get("/api/meetings/recordings")
    assert response.status_code == 503 and "start_local_ai" in response.json()["detail"]


TRANSCRIPT = {
    "id": "t1", "title": "Standup", "speakers": ["SPEAKER_00", "SPEAKER_01"], "speaker_names": {},
    "speaker_embeddings": {"SPEAKER_00": [1.0, 0.0], "SPEAKER_01": [0.0, 1.0]},
    "segments": [{"speaker": "SPEAKER_00", "start": 0, "end": 3, "text": "Owen, can you give the update?"},
                 {"speaker": "SPEAKER_01", "start": 3, "end": 6, "text": "Sure, tests passed."}],
}


@pytest.fixture
def fake_transcriber(monkeypatch):
    store = {"names": {}}
    monkeypatch.setattr(meetings, "transcript", lambda tid: dict(TRANSCRIPT, speaker_names=dict(store["names"])))
    monkeypatch.setattr(meetings, "transcripts", lambda: [{"id": "t1", "speaker_names": {}}])
    monkeypatch.setattr(meetings, "markdown", lambda tid: "[00:00:00] SPEAKER_00: Owen, can you give the update?\n")
    monkeypatch.setattr(meetings, "set_speaker_names", lambda tid, names: store["names"].update(names) or {})
    return store


def test_identify_then_correct_speakers_saves_to_people_memory(client, fake_transcriber):
    with patch.object(lb.requests, "post", return_value=ollama_reply(json.dumps({"speakers": [
            {"id": "SPEAKER_00", "name": None, "role": "Lead", "confidence": 0},
            {"id": "SPEAKER_01", "name": "Owen", "role": "QA", "confidence": 0.7, "evidence": "answers"}]}))):
        out = events(client.post("/api/meetings/transcripts/t1/identify", json={}))
    done = out[-1]
    assert done["type"] == "done" and done["labels"]["SPEAKER_01"]["name"] == "Owen"
    assert 0 < done["labels"]["SPEAKER_01"]["confidence"] < 1 and done["labels"]["SPEAKER_01"]["level"] != "confirmed"
    fixed = client.put("/api/meetings/transcripts/t1/speakers",
                       json={"labels": {"SPEAKER_00": {"name": "Priya", "role": "Delivery manager"}}}).json()
    assert fixed["labels"]["SPEAKER_00"]["level"] == "confirmed"
    assert fixed["display_names"] == {"SPEAKER_00": "Priya", "SPEAKER_01": "Owen"}
    assert fake_transcriber["names"]["SPEAKER_00"] == "Priya"
    priya = next(p for p in client.get("/api/people").json() if p["name"] == "Priya")
    assert priya["role"] == "Delivery manager" and priya["voiceprint_count"] == 1 and priya["meetings"] == ["t1"]
    view = client.get("/api/meetings/transcripts/t1").json()
    assert "speaker_embeddings" not in view and view["has_voiceprints"] is True


def test_summary_streams_and_is_saved(client, fake_transcriber):
    client.put("/api/preferences", json={"meeting_notes_skill": False})  # the free-form minutes path
    with patch.object(lb.requests, "post", return_value=ollama_reply("## Summary\n", "All good.")):
        out = events(client.post("/api/meetings/transcripts/t1/summary", json={"model_id": "local:gemma4:e4b", "summary_model": "local:gemma4:e4b"}))
    assert out[0]["type"] == "start" and out[-1]["type"] == "done"
    assert out[-1]["summary"]["markdown"] == "## Summary\nAll good."
    assert client.get("/api/meetings/transcripts/t1").json()["summary"]["model"] == "Gemma 4 E4B · local"


def test_cloud_summary_asks_before_sending_a_transcript(client, fake_transcriber):
    with patch.object(lb.requests, "post") as post:
        out = events(client.post("/api/meetings/transcripts/t1/summary", json={"model_id": "cloud:gaia", "summary_model": "cloud:gaia"}))
    assert out[0]["type"] == "confirm" and post.call_count == 0


def test_cloud_summary_answered_local_runs_on_a_local_model(client, fake_transcriber):
    """"Use local model" must write the notes locally, not ask the same question again."""
    client.put("/api/preferences", json={"meeting_notes_skill": False})
    with patch.object(lb.requests, "post", return_value=ollama_reply("## Summary\n", "Local notes.")) as post:
        asked = events(client.post("/api/meetings/transcripts/t1/summary", json={"summary_model": "cloud:gaia"}))
        assert asked[0]["type"] == "confirm" and asked[0]["local_model"] and post.call_count == 0
        out = events(client.post("/api/meetings/transcripts/t1/summary",
                                 json={"summary_model": "cloud:gaia", "confirm": "local"}))
    assert [e["type"] for e in out][0] == "start" and out[-1]["type"] == "done"
    assert out[0]["model_id"].startswith("local:") and out[0]["model_id"].endswith(asked[0]["local_model"])
    assert all(c.args[0].startswith(lb.LOCAL_LLM_BASE_URL) for c in post.call_args_list)  # only Ollama, never GAIA
    assert out[-1]["summary"]["markdown"] == "## Summary\nLocal notes."


def test_chat_about_a_meeting_attaches_the_transcript(client, fake_transcriber):
    session = client.post("/api/meetings/transcripts/t1/chat", json={}).json()
    assert session["attachments"][0]["kind"] == "transcript" and session["title"] == "About Standup"


def test_summary_asks_which_model_when_none_is_chosen(client, fake_transcriber):
    """Summaries don't follow the chat model: without a choice the user is asked, and nothing is sent."""
    with patch.object(lb.requests, "post") as post:
        out = events(client.post("/api/meetings/transcripts/t1/summary", json={"model_id": "local:gemma4:e4b"}))
    assert [e["type"] for e in out] == ["choose_model"] and post.call_count == 0
