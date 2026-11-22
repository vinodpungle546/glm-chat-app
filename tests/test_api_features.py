#!/usr/bin/env python3
"""API for the claude.ai import, imported chats, search, meeting notes and built-in skills.
Transcriber, Ollama, GAIA and web providers are faked; data is synthetic."""

import io
import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

import llm_backends as lb
import meetings
import web_providers
from tests.test_api import client, events, ollama_reply  # noqa: F401 (fixture)
from tests.test_claude_import import FULL_EXPORT, MEMORY_TEXT, conversation, make_zip

FIXTURES = Path(__file__).parent / "fixtures" / "meetings"


def fixture(t):
    return json.loads((FIXTURES / f"{t}.json").read_text()), json.loads((FIXTURES / f"{t}.notes.json").read_text())


@pytest.fixture
def transcriber(monkeypatch):
    """The release review fixture, with speakers not yet named."""
    transcript, _ = fixture("release_review")
    transcript = dict(transcript, id="t1", speaker_names={})
    names: dict = {}
    monkeypatch.setattr(meetings, "transcript", lambda tid: dict(transcript, speaker_names=dict(names)))
    monkeypatch.setattr(meetings, "transcripts", lambda: [{k: v for k, v in transcript.items() if k != "segments"}
                                                         | {"speaker_names": dict(names)}])
    monkeypatch.setattr(meetings, "set_speaker_names", lambda tid, n: names.update(n) or {})
    return transcript


def upload(client, files=None, text=""):
    multipart = [("files", (name, data, "application/octet-stream")) for name, data in (files or {}).items()]
    return client.post("/api/imports/claude/preview", files=multipart or None, data={"text": text})


def import_all(client, files=None, text=""):
    preview = upload(client, files, text).json()
    return client.post("/api/imports/claude/commit", json={"preview_id": preview["preview_id"]}).json()


# ---------------------------------------------------------------------------
# Import
# ---------------------------------------------------------------------------

def test_preview_selective_import_and_reimport(client):
    preview = upload(client, {"export.zip": make_zip(FULL_EXPORT)}).json()
    assert preview["counts"] == {"memory": len(preview["memory"]), "people": len(preview["people"]), "conversations": 2}
    people = {p["name"]: p for p in preview["people"]}
    assert people["Cian Byrne"]["role"] == "release manager" and people["Cian Byrne"]["status"] == "new"
    done = client.post("/api/imports/claude/commit", json={
        "preview_id": preview["preview_id"], "memory": [preview["memory"][0]["key"]],
        "people": [people["Cian Byrne"]["key"]], "conversations": ["c-1"]}).json()
    assert (done["memory_added"], done["people_added"], done["conversations_added"]) == (1, 1, 1)
    assert client.get("/api/imports/claude/status").json()["conversations"] == 1
    again = upload(client, {"export.zip": make_zip(FULL_EXPORT)}).json()
    assert {c["uuid"]: c["status"] for c in again["conversations"]} == {"c-1": "unchanged", "c-2": "new"}
    assert client.post("/api/imports/claude/commit", json={"preview_id": "nope"}).status_code == 410


def test_malformed_and_empty_uploads(client):
    bad = upload(client, {"conversations.json": b"{not json", "x.zip": b"PK\x03\x04junk"}).json()
    assert bad["counts"]["conversations"] == 0 and len(bad["warnings"]) >= 3
    assert upload(client).status_code == 400


def test_imported_chats_are_tagged_filterable_read_only_and_continuable(client):
    import_all(client, {"export.zip": make_zip(FULL_EXPORT)})
    native = client.post("/api/sessions", json={}).json()
    listed = client.get("/api/sessions").json()
    imported = [s for s in listed if s["imported"]]
    assert len(imported) == 2 and all(s["read_only"] and s["imported_source"] == "claude.ai" for s in imported)
    assert [s["id"] for s in client.get("/api/sessions", params={"imported": "hide"}).json()] == [native["id"]]
    assert len(client.get("/api/sessions", params={"imported": "only"}).json()) == 2
    sid = next(s["id"] for s in imported if s["title"] == "Release checklist")
    chat = client.get(f"/api/sessions/{sid}").json()
    assert chat["read_only"] and chat["created_at"].startswith("2026-11-10") and len(chat["messages"]) == 2
    assert client.post(f"/api/sessions/{sid}/messages", json={"content": "more"}).status_code == 409
    new = client.post(f"/api/sessions/{sid}/continue", json={"model_id": "cloud:gaia"}).json()
    assert not new["read_only"] and new["model_id"] == "cloud:gaia" and new["continued_from"]["id"] == sid
    assert [m["content"][:20] for m in new["messages"]] == [m["content"][:20] for m in chat["messages"]]
    assert client.patch(f"/api/sessions/{sid}", json={"title": "Atlas checklist"}).json()["title"] == "Atlas checklist"
    deleted = client.delete("/api/imports/claude").json()["deleted"]
    assert deleted["conversations"] == 2
    assert [s["id"] for s in client.get("/api/sessions").json()] == [new["id"], native["id"]] or \
        {s["id"] for s in client.get("/api/sessions").json()} == {new["id"], native["id"]}


def test_imported_memory_edit_merge_delete(client):
    import_all(client, text="## Work\nOwen tests the Android app.\n## Team\nOwen tests the Android app every day.")
    data = client.get("/api/memory/imported").json()
    assert len(data["entries"]) == 2 and len(data["duplicates"]) == 1
    entry = data["entries"][0]
    assert entry["source"] == "claude.ai" and entry["imported_at"]
    assert client.patch(f"/api/memory/imported/{entry['id']}", json={"text": "Owen is our Android tester."}).json()["edited"]
    merged = client.post("/api/memory/imported/merge", json={"ids": data["duplicates"][0], "text": "Owen tests Android daily."}).json()
    assert merged["text"] == "Owen tests Android daily."
    assert client.delete(f"/api/memory/imported/{merged['id']}").json() == {"deleted": 1}
    assert client.get("/api/memory/imported").json()["entries"] == []


def test_people_duplicates_and_merge(client):
    a = client.post("/api/people", json={"name": "Cian"}).json()
    b = client.post("/api/people", json={"name": "Cian Byrne", "role": "Release manager"}).json()
    assert client.get("/api/people/duplicates").json() == [[a["id"], b["id"]]]
    merged = client.post("/api/people/merge", json={"keep": b["id"], "merge": [a["id"]]}).json()
    assert merged["aliases"] == ["Cian"] and len(client.get("/api/people").json()) == 1


# ---------------------------------------------------------------------------
# Speaker identification with imported memory and past conversations
# ---------------------------------------------------------------------------

def test_speaker_labels_use_imported_memory_and_past_conversations(client, transcriber):
    export = {"conversations.json": [conversation("c-9", "Release 4.2 rollback plan", "2026-11-12T10:00:00Z")],
              "memories.json": [{"conversations_memory": MEMORY_TEXT}]}
    export["conversations.json"][0]["chat_messages"][0]["text"] = (
        "Cian wants the release notes and the rollback plan for the 4.2 release before the go/no-go.")
    export["conversations.json"][0]["chat_messages"][0]["content"] = []
    import_all(client, {"export.zip": make_zip(export)})
    reply = json.dumps({"speakers": [
        {"id": "SPEAKER_00", "name": None, "role": "Release manager", "confidence": 0, "evidence": "runs the go/no-go"},
        {"id": "SPEAKER_01", "name": None, "role": "QA tester", "confidence": 0, "evidence": "signs off regression"},
        {"id": "SPEAKER_02", "name": None, "role": "Backend developer", "confidence": 0, "evidence": "migration scripts"}]})
    with patch.object(lb.requests, "post", return_value=ollama_reply(reply)):
        out = events(client.post("/api/meetings/transcripts/t1/identify", json={}))
    assert any("past conversations" in e.get("text", "") for e in out if e["type"] == "status")
    labels = out[-1]["labels"]
    cian = labels["SPEAKER_00"]
    assert cian["name"] == "Cian Byrne" and cian["role"] == "release manager"
    assert "Matched to Cian Byrne: release manager in claude.ai memory" in cian["evidence"]
    assert any("past conversation" in e and "Release 4.2 rollback plan" in e for e in cian["evidence"])
    assert labels["SPEAKER_01"]["name"] == ""  # Owen (QA) isn't known with that role, nobody is guessed


# ---------------------------------------------------------------------------
# Meeting notes
# ---------------------------------------------------------------------------

def notes_reply(notes):
    return ollama_reply(json.dumps(notes))


def test_notes_generation_render_edit_and_exports(client, transcriber, tmp_path):
    _, golden = fixture("release_review")
    with patch.object(lb.requests, "post", return_value=notes_reply(golden)):
        out = events(client.post("/api/meetings/transcripts/t1/summary", json={"model_id": "local:gemma4:e4b", "summary_model": "local:gemma4:e4b"}))
    summary = out[-1]["summary"]
    assert summary["skill"] == "meeting-notes" and summary["meeting_type"] == "release_review"
    assert summary["detected_type"]["type"] == "release_review" and summary["valid"]
    assert "## Go / no-go" in summary["markdown"] and "## Action items" in summary["markdown"]
    brief = client.post("/api/meetings/transcripts/t1/summary/render", json={"detail": "brief"}).json()
    assert "## Risks" not in brief["markdown"] and brief["detail"] == "brief"
    typed = client.post("/api/meetings/transcripts/t1/summary/render", json={"meeting_type": "incident", "detail": "standard"}).json()
    assert "Incident / postmortem" in typed["markdown"]
    edited = client.put("/api/meetings/transcripts/t1/summary", json={"markdown": brief["markdown"].replace(
        "Finish the release notes", "Publish the release notes")}).json()
    assert edited["edited"]
    assert "Publish the release notes" in client.get("/api/meetings/transcripts/t1").json()["summary"]["markdown"]
    for fmt, start in (("md", b"# Release"), ("html", b"<!doctype html>"), ("pdf", b"%PDF"), ("docx", b"PK"),
                       ("csv", b"#,Action")):
        res = client.get("/api/meetings/transcripts/t1/summary/export", params={"format": fmt})
        assert res.status_code == 200 and res.content.startswith(start), fmt
        assert "attachment" in res.headers["content-disposition"]
    assert b"Publish the release notes" in client.get("/api/meetings/transcripts/t1/summary/export",
                                                      params={"format": "csv"}).content
    email = client.get("/api/meetings/transcripts/t1/summary/email").json()
    assert email["subject"].startswith("Follow-up: Release 4.2 go/no-go") and "Publish the release notes" in email["text"]
    clip = client.get("/api/meetings/transcripts/t1/summary/clipboard").json()
    assert "<table" in clip["html"] and "Publish the release notes" in clip["text"]


def test_notes_with_type_override_detail_and_sections(client, transcriber):
    _, golden = fixture("release_review")
    with patch.object(lb.requests, "post", return_value=notes_reply(golden)) as post:
        out = events(client.post("/api/meetings/transcripts/t1/summary", json={
            "model_id": "local:gemma4:e4b", "summary_model": "local:gemma4:e4b", "meeting_type": "status_review", "detail": "detailed",
            "sections": {"appendix": False, "risks": True}}))
    summary = out[-1]["summary"]
    assert summary["meeting_type"] == "status_review" and summary["detail"] == "detailed"
    assert "## Appendix" not in summary["markdown"]
    prompt = post.call_args.kwargs["json"]["messages"][1]["content"]
    assert "Meeting type: Status / steering review" in prompt and "Detailed:" in prompt


def test_notes_with_the_cloud_model_ask_first_then_use_gaia(client, transcriber):
    _, golden = fixture("release_review")
    out = events(client.post("/api/meetings/transcripts/t1/summary", json={"model_id": "cloud:gaia", "summary_model": "cloud:gaia"}))
    assert out[0]["type"] == "confirm"
    body = json.dumps(golden)
    sse = [f"data: {json.dumps({'choices': [{'delta': {'content': body[i:i + 200]}}]})}" for i in range(0, len(body), 200)]
    resp = MagicMock(status_code=200)
    resp.iter_lines.return_value = sse + ["data: [DONE]"]
    resp.__enter__.return_value = resp
    resp.__exit__.return_value = False
    with patch.object(lb.requests, "post", return_value=resp) as post:
        out = events(client.post("/api/meetings/transcripts/t1/summary", json={"model_id": "cloud:gaia", "summary_model": "cloud:gaia", "confirm": "cloud"}))
    assert out[-1]["type"] == "done" and out[-1]["summary"]["model"].endswith("· cloud")
    assert post.call_args.args[0] == lb.GAIA_ENDPOINT  # the unchanged cloud endpoint


def test_builtin_skill_toggle_and_template_editing(client):
    skill = client.get("/api/skills/builtin").json()[0]
    assert skill["name"] == "Professional Meeting Notes" and skill["enabled"] and skill["default_for"] == "Meeting summaries"
    names = {t["name"]: t for t in skill["templates"]}
    assert names["standup"]["label"] == "Daily stand-up" and "partials/actions" in names
    tpl = client.get("/api/skills/meeting-notes/templates/standup").json()
    saved = client.put("/api/skills/meeting-notes/templates/standup", json={"text": tpl["text"] + "\nCustom footer\n"}).json()
    assert saved["customized"] and saved["text"].endswith("Custom footer\n")
    assert not client.delete("/api/skills/meeting-notes/templates/standup").json()["customized"]
    assert client.get("/api/skills/meeting-notes/templates/nope").status_code == 404
    assert client.patch("/api/skills/meeting-notes", json={"enabled": False}).json()["enabled"] is False


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------

def test_search_groups_internal_sources_with_links(client, transcriber):
    import_all(client, {"export.zip": make_zip(FULL_EXPORT)})
    res = client.post("/api/search", json={"q": "rollback plan release"}).json()
    groups = res["groups"]
    assert groups["chats"] and groups["chats"][0]["kind"] == "imported_chat" and groups["chats"][0]["link"].startswith("/c/")
    assert groups["transcripts"] and groups["transcripts"][0]["link"].startswith("/meetings/t1?t=")
    assert res["web"]["status"] == "disabled" and groups["web"] == []
    memory = client.post("/api/search", json={"q": "release manager", "sources": ["memory"]}).json()["groups"]
    assert memory["memory"] and not memory["chats"]
    old = client.post("/api/search", json={"q": "rollback plan release", "date_to": "2026-11-01"}).json()["groups"]
    assert old["chats"] == [] and old["transcripts"] == []


def test_web_search_fallbacks_and_keys_never_leave_the_server(client, transcriber):
    client.put("/api/search/settings", json={"web_enabled": True, "provider": "brave"})
    res = client.post("/api/search", json={"q": "release checklist"}).json()
    assert res["web"]["status"] == "missing_key" and "own data only" in res["web"]["message"]
    assert res["groups"]["transcripts"]  # internal results still there
    settings = client.put("/api/search/keys/brave", json={"key": "brave-secret-987654"}).json()
    brave = next(p for p in settings["providers"] if p["id"] == "brave")
    assert brave["has_key"] and brave["key_hint"] == "••••7654" and "brave-secret" not in json.dumps(settings)
    ok = MagicMock(status_code=200, ok=True)
    ok.json.return_value = {"web": {"results": [{"title": "Release checklists", "url": "https://example.com/r", "description": "How to"}]}}
    with patch.object(web_providers.requests, "request", return_value=ok):
        res = client.post("/api/search", json={"q": "release checklist"}).json()
    assert res["web"]["status"] == "ok" and res["groups"]["web"][0]["link"] == "https://example.com/r"
    limited = MagicMock(status_code=429, ok=False)
    with patch.object(web_providers.requests, "request", return_value=limited):
        res = client.post("/api/search", json={"q": "release checklist"}).json()
    assert res["web"]["status"] == "rate_limited" and res["groups"]["transcripts"]
    import requests
    with patch.object(web_providers.requests, "request", side_effect=requests.ConnectionError()):
        assert client.post("/api/search", json={"q": "x release"}).json()["web"]["status"] == "offline"


def test_agent_uses_search_with_citations(client, transcriber):
    import_all(client, {"export.zip": make_zip(FULL_EXPORT)})
    sid = client.post("/api/sessions", json={}).json()["id"]
    replies = [ollama_reply('{"tool": "search", "input": "rollback plan"}'),
               ollama_reply("The rollback plan is to redeploy 4.1 [1](http://localhost:3000/meetings/t1?t=80).")]
    with patch.object(lb.requests, "post", side_effect=replies) as post:
        out = events(client.post(f"/api/sessions/{sid}/messages", json={"content": "What was our rollback plan?",
                                                                         "model_id": "local:gemma4:e4b"}))
    assert any(e["type"] == "status" and "Searching for" in e["text"] for e in out)
    tool_output = post.call_args_list[1].kwargs["json"]["messages"][-1]["content"]
    assert "Meeting · Release 4.2 go/no-go at 00:01:20" in tool_output and "link: http://localhost:3000/meetings/t1?t=80" in tool_output
    assert tool_output.index("[1] ") < tool_output.index("[2] ")  # numbered for inline citations
    assert "[1](http://localhost:3000/meetings/t1" in out[-1]["message"]["content"]


def test_private_names_never_reach_the_web_provider_implicitly(client, transcriber):
    from api import main
    client.post("/api/people", json={"name": "Cian Byrne", "role": "Release manager"})
    client.put("/api/search/settings", json={"web_enabled": True, "provider": "tavily"})
    client.put("/api/search/keys/tavily", json={"key": "tvly-secret-123456"})
    with patch.object(web_providers.requests, "request") as call:
        text = main._search_tool("Cian Byrne release history", {"_turn": {"local": False, "user": "who handles releases?"}})
    assert call.call_count == 0 and "Web search was skipped" in text and "Cian" in text
    ok = MagicMock(status_code=200, ok=True)
    ok.json.return_value = {"results": []}
    with patch.object(web_providers.requests, "request", return_value=ok) as call:
        main._search_tool("Cian Byrne talk", {"_turn": {"local": False, "user": "search the web for Cian Byrne's talk"}})
    assert call.call_count == 1  # the user typed the name: explicit
    with patch.object(web_providers.requests, "request") as call:
        main._search_tool("release history", {"_turn": {"local": True, "user": "x"}})
    assert call.call_count == 0  # local chats never use the web


def test_cloud_chat_shows_the_imported_data_notice_once(client):
    import_all(client, text=MEMORY_TEXT)
    sid = client.post("/api/sessions", json={}).json()["id"]
    client.put("/api/preferences", json={"confirm_cloud_sensitive": False})

    def gaia(*_a, **_k):
        resp = MagicMock(status_code=200)
        resp.iter_lines.return_value = [f"data: {json.dumps({'choices': [{'delta': {'content': 'Hi'}}]})}", "data: [DONE]"]
        resp.__enter__.return_value = resp
        resp.__exit__.return_value = False
        return resp
    with patch.object(lb.requests, "post", side_effect=gaia) as post:
        first = events(client.post(f"/api/sessions/{sid}/messages",
                                   json={"content": "What does Cian Byrne do?", "model_id": "cloud:gaia"}))
        second = events(client.post(f"/api/sessions/{sid}/messages",
                                    json={"content": "And Cian Byrne's project?", "model_id": "cloud:gaia"}))
    notice = [e for e in first if e["type"] == "notice" and e.get("kind") == "privacy"]
    assert len(notice) == 1 and "memory" in notice[0]["text"] and "GLM-5.3 Flash" in notice[0]["text"]
    assert not any(e["type"] == "notice" and e.get("kind") == "privacy" for e in second)
    used = next(e for e in first if e["type"] == "memory_used")["facts"]
    assert any("Cian Byrne" in f for f in used)
    system = post.call_args_list[0].kwargs["json"]["messages"][0]["content"]
    assert "Relevant facts from the user's memory" in system and "release manager" in system


def test_existing_web_search_setting_is_kept(client):
    from api import main
    main.PREFS_FILE.write_text(json.dumps({"web_search": True, "selected_model": ""}))
    search = client.get("/api/preferences").json()["search"]
    assert search["web_enabled"] is True and search["provider"] == "duckduckgo"
    main.PREFS_FILE.unlink()
    fresh = client.get("/api/preferences").json()["search"]
    assert fresh["web_enabled"] is False and fresh["provider"] == "tavily"
