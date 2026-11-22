#!/usr/bin/env python3
"""Meeting check: alerts for conflicts with earlier meetings, things to raise, pending items and the user's actions
(as Release Manager), during a call (live draft) or after it. Synthetic meetings and a fake local model only."""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest

import llm_backends as lb
import meeting_check as mc
import meetings
import memory_store
import search_index
from api import main
from tests.test_api import client, events, ollama_reply  # noqa: F401  (client is a fixture)

LIVE = """# Live transcript: Release sync
[00:00:05] Priya Sharma: Let's move the release to the 14th.
[00:00:40] Others: The vendor API is rate limiting us in staging.
[00:01:10] You: I'll check the go/no-go checklist.
"""

EARLIER_NOTES = {
    "meeting": {"title": "Release planning", "project": "Atlas", "attendees": ["Priya Sharma", "Mei Lin"]},
    "decisions": [{"decision": "Release on Friday the 7th", "made_by": "Priya Sharma", "timestamp": "00:02:10"}],
    "action_items": [{"action": "QA sign-off for the release", "owner": "Mei Lin", "due_date": "Thursday the 6th",
                      "timestamp": "00:04:00"}],
    "risks": [], "open_questions": [{"question": "Who approves the change window?", "timestamp": "00:05:00"}],
}


# The fake check model: one alert of each type, plus two that must be dropped.
REPLY_ALERTS = [
    {"type": "conflict", "text": "The release moves to the 14th, but the 7th was decided.", "line": "00:00:05",
     "sources": ["E1"]},
    {"type": "raise", "text": "Tell everyone the vendor API rate limits staging.", "line": "00:00:40", "sources": []},
    {"type": "pending", "text": "Mei Lin's QA sign-off (due the 6th) isn't done.", "line": None, "sources": ["E2"]},
    {"type": "my_action", "text": "Run the go/no-go checklist before the new date.", "line": "00:01:10", "sources": []},
    {"type": "conflict", "text": "Budget was cut.", "line": "00:00:05", "sources": []},           # no source
    {"type": "raise", "text": "Hiring freeze.", "line": "00:09:59", "sources": []},              # no such line
]


def model_reply(*_a, **_k):
    return ollama_reply(json.dumps({"alerts": REPLY_ALERTS}))


# --- pure rules -------------------------------------------------------------------------------------------------

def test_lines_are_read_from_transcript_markdown():
    lines = mc.parse_lines(LIVE)
    assert [(l["time"], l["speaker"]) for l in lines] == [("00:00:05", "Priya Sharma"), ("00:00:40", "Others"),
                                                          ("00:01:10", "You")]
    assert lines[2]["seconds"] == 70


def test_alerts_without_a_source_or_a_real_line_are_dropped():
    lines = mc.parse_lines(LIVE)
    earlier = mc.earlier_items([{"tid": "t1", "title": "Release planning", "date": "2026-11-01", "notes": EARLIER_NOTES}])
    assert [(e["id"], e["kind"]) for e in earlier] == [("E1", "decision"), ("E2", "action"), ("E3", "question")]
    context = {e["id"]: {"ref": e["id"], "kind": e["kind"], "text": e["text"]} for e in earlier}
    kept, dropped = mc.validate(REPLY_ALERTS, lines, context)
    assert [a["type"] for a in kept] == ["conflict", "raise", "pending", "my_action"]
    assert kept[0]["sources"][0]["text"] == "Release on Friday the 7th" and kept[0]["seconds"] == 5
    assert {d["reason"] for d in dropped} == {"no earlier source", "cites a line that isn't in the meeting"}


def test_repeats_are_not_shown_again_and_the_popup_has_no_content():
    shown = [{"type": "conflict", "text": "The release moves to the 14th but the 7th was decided", "sources": []}]
    again = {"type": "conflict", "text": "The release moves to the 14th, while the 7th was decided.", "sources": []}
    other = {"type": "raise", "text": "The release moves to the 14th, while the 7th was decided.", "sources": []}
    assert mc.is_repeat(again, shown) and not mc.is_repeat(other, shown)
    line = mc.summary_line([{"type": "conflict"}, {"type": "pending"}, {"type": "my_action"}])
    assert line == "3 alerts: 1 conflict, 1 still pending, 1 action for you"


# --- through the API --------------------------------------------------------------------------------------------

@pytest.fixture
def meeting_world(client, monkeypatch):
    """An earlier meeting with notes, a call being recorded with a live draft, and the user (Release Manager)."""
    main.SUMMARIES_DIR.mkdir(parents=True, exist_ok=True)
    (main.SUMMARIES_DIR / "t-earlier.json").write_text(json.dumps({"skill": "meeting-notes", "notes": EARLIER_NOTES,
                                                                   "markdown": "# Release planning"}))
    monkeypatch.setattr(meetings, "transcripts", lambda: [
        {"id": "t-earlier", "title": "Release planning", "created_at": "2026-11-10T10:00:00",
         "speaker_names": {"SPEAKER_00": "Priya Sharma"}}])
    monkeypatch.setattr(meetings, "record_live", lambda rid=None, since=0: {"id": "abc123abc123", "title": "Release sync",
                                                                             "segments": [], "transcript_id": None})
    monkeypatch.setattr(meetings, "record_live_markdown", lambda rid=None: LIVE)
    held = []
    monkeypatch.setattr(meetings, "record_live_pause", lambda rid=None, seconds=600: held.append(("pause", rid)) or {})
    monkeypatch.setattr(meetings, "record_live_resume", lambda rid=None: held.append(("resume", rid)) or {})
    monkeypatch.setattr(main, "gather_chunks", lambda sources, notes=None: [search_index.Chunk(
        "transcripts", "transcript", "t-earlier", "Release planning",
        "[00:02:10] Priya Sharma: We release on Friday the 7th.", "2026-11-10T10:00:00", "/meetings/t-earlier",
        {"timestamp": "00:02:10", "seconds": 130})])
    store = memory_store.load_store()
    me = memory_store.new_entry("person", "Owen Hart")
    me.update(is_user=True, role="Release Manager")
    store["entries"].append(me)
    memory_store.save_store(store)
    return {"held": held}


def test_a_live_check_finds_the_four_kinds_with_sources_and_pauses_the_draft(client, meeting_world):
    with patch.object(lb.requests, "post", side_effect=model_reply) as post:
        out = events(client.post("/api/meetings/check", json={}))
    prompt = next(c.kwargs["json"] for c in post.call_args_list if "messages" in (c.kwargs.get("json") or {}))
    text = prompt["messages"][-1]["content"]
    assert "Owen Hart's role: Release Manager." in text and "E1 (decision, Release planning 2026-11-10" in text
    assert "P1 (from Release planning" in text  # a matching passage of the earlier transcript
    found = next(e for e in out if e["type"] == "alerts")
    assert [a["type"] for a in found["alerts"]] == ["conflict", "raise", "pending", "my_action"]
    assert len(found["new"]) == 4 and found["dropped"] == 2
    assert found["summary"] == "4 alerts: 1 conflict, 1 to raise with everyone, 1 still pending, 1 action for you"
    conflict = found["alerts"][0]
    assert conflict["sources"][0]["meeting"] == "Release planning" and conflict["line"] == "00:00:05"
    assert meeting_world["held"] == [("pause", "abc123abc123"), ("resume", "abc123abc123")]

    # Again: nothing new (no repeats), and the stored alerts come back.
    with patch.object(lb.requests, "post", side_effect=model_reply):
        again = next(e for e in events(client.post("/api/meetings/check", json={})) if e["type"] == "alerts")
    assert again["new"] == [] and len(again["alerts"]) == 4


def test_dismiss_and_track_alerts_into_my_actions(client, meeting_world):
    with patch.object(lb.requests, "post", side_effect=model_reply):
        found = next(e for e in events(client.post("/api/meetings/check", json={})) if e["type"] == "alerts")
    key = found["key"]
    pending = next(a for a in found["alerts"] if a["type"] == "pending")
    raise_ = next(a for a in found["alerts"] if a["type"] == "raise")
    assert client.post(f"/api/meetings/check/alerts/{key}/{raise_['id']}/dismiss").json()["state"] == "dismissed"
    assert client.post(f"/api/meetings/check/alerts/{key}/{pending['id']}/track").json()["state"] == "tracked"
    actions = client.get("/api/my-actions").json()
    assert [a["text"] for a in actions] == [pending["text"]] and actions[0]["done"] is False
    assert client.patch(f"/api/my-actions/{actions[0]['id']}", json={"done": True}).json()["done"] is True
    states = {a["id"]: a["state"] for a in client.get("/api/meetings/check/alerts",
                                                       params={"recording_id": "abc123abc123"}).json()["alerts"]}
    assert states[raise_["id"]] == "dismissed" and states[pending["id"]] == "tracked"


def test_a_cloud_check_model_asks_first_and_never_runs_unattended(client, meeting_world):
    client.put("/api/preferences", json={"check_model": "cloud:gaia"})
    with patch.object(lb.requests, "post") as post:
        out = events(client.post("/api/meetings/check", json={}))
        assert post.call_count == 0
    assert out[0]["type"] == "confirm" and out[0]["kind"] == "sensitive" and out[0]["local_model"] == "gemma4:e4b"
    with patch.object(lb.requests, "post", side_effect=model_reply):  # "Use local model"
        local = events(client.post("/api/meetings/check", json={"confirm": "local"}))
    assert len(next(e for e in local if e["type"] == "alerts")["new"]) == 4
    with patch.object(lb.requests, "post") as post:
        out = events(client.post("/api/meetings/check", json={"transcript_id": "t-new", "unattended": True}))
        assert post.call_count == 0


def test_settings_for_the_check_are_validated(client):
    assert client.put("/api/preferences", json={"check_auto_minutes": 7}).status_code == 400
    assert client.put("/api/preferences", json={"check_days": 12}).status_code == 400
    assert client.put("/api/preferences", json={"check_model": "nope"}).status_code == 400
    saved = client.put("/api/preferences", json={"check_auto_minutes": 10, "check_popups": False}).json()
    assert saved["check_auto_minutes"] == 10 and saved["check_popups"] is False
