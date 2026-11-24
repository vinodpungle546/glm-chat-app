#!/usr/bin/env python3
"""Recording a call through the API: the record endpoints (transcriber faked), the confirmed microphone speaker,
the live draft attached to a chat (refreshed each turn, swapped for the final transcript), privacy checks.
Synthetic data and fakes only."""

from __future__ import annotations

from unittest.mock import patch

import pytest

import llm_backends as lb
import meetings
import memory_store
import people as people_store
import speaker_id
import speaker_review
from api import main
from tests.test_api import client, events, ollama_reply  # noqa: F401  (client is a fixture)

CALL = {"id": "0123456789abcdef", "title": "Release sync", "duration": 60, "diarization": True,
        "confirmed_speakers": {"SPEAKER_01": {"source": "microphone"}},
        "speaker_embeddings": {"SPEAKER_01": [0.1, 0.2, 0.3]},
        "segments": [{"speaker": "SPEAKER_00", "start": 0, "end": 3, "text": "Owen, is the build green?"},
                     {"speaker": "SPEAKER_01", "start": 4, "end": 6, "text": "Yes, all green, Priya."},
                     {"speaker": "SPEAKER_00", "start": 7, "end": 9, "text": "Great, ship it Friday."}]}


# --- speaker naming ----------------------------------------------------------------------------------

def test_your_microphone_track_is_named_after_you():
    me = {"name": "Owen Hart", "role": "Release Manager"}
    labels = speaker_id.identify(CALL, [], [], None, me=me)
    assert labels["SPEAKER_01"]["name"] == "Owen Hart" and labels["SPEAKER_01"]["source"] == "microphone"
    assert labels["SPEAKER_01"]["level"] == "high" and labels["SPEAKER_01"]["role"] == "Release Manager"
    assert labels["SPEAKER_00"]["name"] != "Owen Hart"  # the name is taken: nobody else gets it


def test_without_your_name_the_microphone_speaker_is_you():
    labels = speaker_id.identify(CALL, [], [], None)
    assert labels["SPEAKER_01"]["name"] == "You"
    assert any("Settings → Memory" in e for e in labels["SPEAKER_01"]["evidence"])


def test_the_model_cannot_rename_the_microphone_speaker_but_you_can():
    reply = '{"speakers": [{"id": "SPEAKER_01", "name": "Priya", "role": "QA", "confidence": 0.9, "evidence": "x"}]}'
    labels = speaker_id.identify(CALL, [], [], lambda _m: reply, me={"name": "Owen Hart"})
    assert labels["SPEAKER_01"]["name"] == "Owen Hart"
    corrected = speaker_id.identify(CALL, [], [], None, me={"name": "Owen Hart"},
                                    keep={"SPEAKER_01": {"name": "Sam Lee", "source": "user"}})
    assert corrected["SPEAKER_01"]["name"] == "Sam Lee"


def test_the_cloud_review_never_touches_the_microphone_speaker():
    labels = speaker_id.identify(CALL, [], [], None, me={"name": "Owen Hart"})
    sent = []

    def complete(messages, **_kw):
        sent.append(messages[-1]["content"])
        return '{"speakers": [{"id": "SPEAKER_00", "action": "keep"}]}'

    result = speaker_review.review(CALL, labels, [], complete)
    assert "SPEAKER_01" not in result["sent"]["speakers"]


def test_identify_saves_your_voice_from_the_microphone(client, monkeypatch):
    store = memory_store.load_store()
    me = memory_store.new_entry("person", "Owen Hart")
    me["is_user"] = True
    store["entries"].append(me)
    memory_store.save_store(store)
    monkeypatch.setattr(meetings, "transcript", lambda tid: dict(CALL, speaker_names={}))
    monkeypatch.setattr(meetings, "transcripts", lambda: [{"id": CALL["id"], "speaker_names": {}}])
    monkeypatch.setattr(meetings, "set_speaker_names", lambda tid, names: {})
    monkeypatch.setattr(main, "use_speaker_model", lambda *_a: None)  # no local model: rules only
    out = events(client.post(f"/api/meetings/transcripts/{CALL['id']}/identify", json={}))
    labels = next(e for e in out if e["type"] == "labels")["labels"]
    assert labels["SPEAKER_01"]["name"] == "Owen Hart" and labels["SPEAKER_01"]["source"] == "microphone"
    owen = next(p for p in people_store.load() if p["name"] == "Owen Hart")
    assert owen["voiceprints"][-1]["source"] == "microphone" and owen["voiceprints"][-1]["vector"] == [0.1, 0.2, 0.3]


# --- record endpoints ------------------------------------------------------------------------------------

def test_record_start_remembers_the_live_choice_and_explains_permissions(client, monkeypatch):
    seen = {}
    monkeypatch.setattr(meetings, "record_start", lambda source, mic, live, title: seen.update(
        source=source, mic=mic, live=live) or {"state": "recording"})
    client.post("/api/meetings/record/start", json={"source": {"kind": "app", "id": "us.zoom.xos"}, "live": True})
    assert seen["live"] is True and client.get("/api/preferences").json()["record_live"] is True
    client.post("/api/meetings/record/start", json={"source": {"kind": "audio"}})  # no choice: the saved one
    assert seen["live"] is True

    def refuse(*_a):
        raise meetings.RecordError("permission_screen", "Screen recording isn't allowed.", 503)

    monkeypatch.setattr(meetings, "record_start", refuse)
    r = client.post("/api/meetings/record/start", json={"source": {"kind": "audio"}})
    assert r.status_code == 503 and r.json()["detail"]["kind"] == "permission_screen"


def test_record_stop_starts_the_transcription_with_the_meetings_models(client, monkeypatch):
    seen = {}
    monkeypatch.setattr(meetings, "record_stop", lambda transcribe, speakers, screen, models: seen.update(
        models=models, speakers=speakers) or {"recording": {"id": "abc123abc123", "path": "/x/Call.mov"},
                                              "job": {"id": "job1"}})
    started = []
    monkeypatch.setattr(main.FOLLOWUPS, "start", lambda job_id, info: started.append(job_id) or {"status": "waiting"})
    done = client.post("/api/meetings/record/stop", json={"speakers": 4, "after": True,
                                                          "summary_model": "local:gemma4:e4b"}).json()
    assert seen["models"]["after"] is True and seen["models"]["summary_model"] == "local:gemma4:e4b"
    assert seen["speakers"] == 4 and started == ["job1"] and done["job"]["followup"] == {"status": "waiting"}


# --- the live draft in a chat ----------------------------------------------------------------------------

@pytest.fixture
def live_call(monkeypatch):
    state = {"text": "[00:00:05] Priya Sharma: Release is Friday.\n", "final": None}
    monkeypatch.setattr(meetings, "record_live", lambda rid=None, since=0: {
        "id": "abc123abc123", "title": "Release sync", "segments": [], "transcript_id": state["final"]})
    monkeypatch.setattr(meetings, "record_live_markdown", lambda rid=None: "# Live transcript\n" + state["text"])
    monkeypatch.setattr(meetings, "transcript", lambda tid: {"id": tid, "title": "Release sync"})
    monkeypatch.setattr(meetings, "markdown", lambda tid: "# Final transcript\n[00:00:05] Priya Sharma: Release is Friday.\n")
    return state


def test_the_live_draft_is_refreshed_each_turn_then_becomes_the_final_transcript(client, live_call):
    sid = client.post("/api/sessions", json={}).json()["id"]
    record = client.post(f"/api/sessions/{sid}/attachments/live", json={}).json()
    assert record["attachments"] == [{"name": "Transcript: Release sync (live)", "kind": "transcript",
                                      "chars": record["attachments"][0]["chars"], "transcript_id": None,
                                      "live": "abc123abc123"}]
    live_call["text"] += "[00:10:00] You: Owen owns the rollback plan.\n"  # said after it was attached
    with patch.object(lb.requests, "post", return_value=ollama_reply("Owen owns the rollback plan.")) as post:
        client.post(f"/api/sessions/{sid}/messages", json={"content": "Who owns rollback?",
                                                           "model_id": "local:gemma4:e4b"})
    chat = next(c.kwargs["json"] for c in post.call_args_list if "messages" in (c.kwargs.get("json") or {}))
    sent = " ".join(m["content"] for m in chat["messages"])
    assert "Owen owns the rollback plan." in sent  # the turn saw the meeting up to now
    live_call["final"] = "0123456789abcdef"
    with patch.object(lb.requests, "post", return_value=ollama_reply("ok")):
        client.post(f"/api/sessions/{sid}/messages", json={"content": "And now?", "model_id": "local:gemma4:e4b"})
    after = client.get(f"/api/sessions/{sid}").json()["attachments"]
    assert after[0]["transcript_id"] == "0123456789abcdef" and after[0]["live"] is None
    assert after[0]["name"] == "Transcript: Release sync"


def test_a_cloud_model_asks_before_reading_the_live_draft(client, live_call):
    sid = client.post("/api/sessions", json={}).json()["id"]
    client.post(f"/api/sessions/{sid}/attachments/live", json={})
    with patch.object(lb.requests, "post") as post:
        out = events(client.post(f"/api/sessions/{sid}/messages",
                                 json={"content": "Actions so far?", "model_id": "cloud:gaia"}))
        assert post.call_count == 0
    assert out[0]["type"] == "confirm" and out[0]["kind"] == "sensitive"


# --- playing a meeting back -------------------------------------------------------------------------------------

class FakeUpstream:
    """A streamed transcriber reply (requests.Response stand-in)."""

    def __init__(self, status, body, headers):
        self.status_code, self._body, self.headers = status, body, headers
        self.text = body.decode("latin-1")
        self.closed = False

    def iter_content(self, size):
        yield self._body

    def close(self):
        self.closed = True


def test_playback_streams_the_recording_with_seeking(client, monkeypatch):
    """The player's sound comes through the API with the browser's Range header passed on (so it can seek), and
    the transcriber's headers (206, Content-Range) come back."""
    asked = []

    def stream(tid, part, range_header=None):
        asked.append((tid, part, range_header))
        return FakeUpstream(206, b"0123456789", {"content-type": "audio/mp4", "content-length": "10",
                                                 "content-range": "bytes 0-9/5000", "accept-ranges": "bytes",
                                                 "x-internal": "no"})

    monkeypatch.setattr(meetings, "media_stream", stream)
    monkeypatch.setattr(meetings, "media_info", lambda tid: {"available": True, "video": False,
                                                             "parts": [{"part": "call", "label": "Call"}]})
    assert client.get("/api/meetings/transcripts/0123456789abcdef/media").json()["parts"][0]["part"] == "call"
    r = client.get("/api/meetings/transcripts/0123456789abcdef/media/call", headers={"Range": "bytes=0-9"})
    assert r.status_code == 206 and r.content == b"0123456789"
    assert r.headers["content-range"] == "bytes 0-9/5000" and "x-internal" not in r.headers
    assert asked == [("0123456789abcdef", "call", "bytes=0-9")]
    assert client.get("/api/meetings/transcripts/0123456789abcdef/media/secrets").status_code == 404


def test_playback_errors_are_passed_on(client, monkeypatch):
    up = FakeUpstream(404, b'{"detail": "Recording not found."}', {})
    monkeypatch.setattr(meetings, "media_stream", lambda *a, **k: up)
    r = client.get("/api/meetings/transcripts/0123456789abcdef/media/video")
    assert r.status_code == 404 and r.json()["detail"] == "Recording not found." and up.closed


def test_a_moved_recording_is_found_and_linked_through_the_api(client, monkeypatch):
    """The player's "find the recording" step: candidates and linking pass through to the transcriber; its
    refusal (a file of another length) reaches the page as a readable error."""
    linked = []
    monkeypatch.setattr(meetings, "recording_candidates", lambda tid: {"candidates": [
        {"path": "/Users/x/Desktop/Sync.mov", "name": "Sync.mov", "folder": "Desktop", "match": True}]})

    def relink(tid, path):
        linked.append((tid, path))
        if path.endswith("short.mov"):
            raise RuntimeError("This file is 0:09 long; the meeting is 0:04. It looks like a different recording.")
        return {"available": True, "video": True, "parts": [], "linked": "/m/uploads/Sync.mov", "copied": True}

    monkeypatch.setattr(meetings, "relink_recording", relink)
    tid = "0123456789abcdef"
    assert client.get(f"/api/meetings/transcripts/{tid}/recording/candidates").json()["candidates"][0]["folder"] == "Desktop"
    r = client.post(f"/api/meetings/transcripts/{tid}/recording", json={"path": "/Users/x/Desktop/Sync.mov"})
    assert r.status_code == 200 and r.json()["available"] is True
    bad = client.post(f"/api/meetings/transcripts/{tid}/recording", json={"path": "/x/short.mov"})
    assert bad.status_code == 400 and "different recording" in bad.json()["detail"]
    assert client.post(f"/api/meetings/transcripts/{tid}/recording", json={"path": ""}).status_code == 422
    assert client.get("/api/meetings/transcripts/bad%20id!/recording/candidates").status_code == 404
    assert linked == [(tid, "/Users/x/Desktop/Sync.mov"), (tid, "/x/short.mov")]
