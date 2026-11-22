#!/usr/bin/env python3
"""Correcting a meeting with other transcripts of it, through the API (transcriber faked): known names from
memory go with the request, names a reference found become labels (never over yours or your microphone's, and
withdrawn with the source), search sees the corrected words, and a summary written before is marked. Synthetic
data only."""

from __future__ import annotations

import json

import meetings
import memory_store
from api import main
from tests.test_api import client  # noqa: F401  (client is a fixture)

TID = "abcdef0123456789"
T = {"id": TID, "title": "Release sync", "speakers": ["SPEAKER_00", "SPEAKER_01", "SPEAKER_02"],
     "speaker_names": {}, "segments": [{"speaker": "SPEAKER_00", "start": 0, "end": 2, "text": "Thanks Priya."}],
     "screen_names": {"SPEAKER_01": {"names": [{"name": "Owen Hart"}, {"name": "Mei L…", "truncated": True}]}}}
CORRECTION = {"refs": [{"id": "r1", "name": "claude.ai", "source": "claude", "applied": True}],
              "changes": [{"id": "c1", "kind": "word", "from": "Pria.", "to": "Priya.", "seg": 0}],
              "names": {"SPEAKER_00": {"name": "Priya Sharma", "share": 0.9, "words": 40},
                        "SPEAKER_02": {"name": "Vinod", "share": 0.95, "words": 30}},
              "stats": {"words": 1}, "updated_at": "2026-11-22T12:00:00"}


def setup_world(monkeypatch, sent):
    monkeypatch.setattr(meetings, "transcript", lambda tid: json.loads(json.dumps(T)))
    monkeypatch.setattr(meetings, "set_speaker_names", lambda *a, **k: {})

    def add_text(tid, source, name, text, terms):
        sent.append({"source": source, "name": name, "text": text, "terms": terms})
        return json.loads(json.dumps(CORRECTION))

    monkeypatch.setattr(meetings, "add_text_reference", add_text)
    store = memory_store.load_store()
    person = memory_store.new_entry("person", "Mei Lin")
    person["aliases"] = ["Mei"]
    store["entries"].append(person)
    memory_store.save_store(store)


def test_pasted_text_is_sent_with_known_names_and_named_speakers_become_labels(client, monkeypatch):
    sent = []
    setup_world(monkeypatch, sent)
    main.LABELS_DIR.mkdir(parents=True, exist_ok=True)
    (main.LABELS_DIR / f"{TID}.json").write_text(json.dumps({
        "SPEAKER_02": {"name": "Vinod Pungle", "source": "microphone", "confidence": 0.99}}))
    main.SUMMARIES_DIR.mkdir(parents=True, exist_ok=True)
    (main.SUMMARIES_DIR / f"{TID}.json").write_text(json.dumps({"markdown": "# Notes", "created_at": "2026-11-22T09:00:00"}))
    main._TRANSCRIPT_CHUNKS[TID] = ("old", [])

    r = client.post(f"/api/meetings/transcripts/{TID}/references",
                    json={"source": "claude", "text": "Thanks Priya. The build is green."})
    assert r.status_code == 200 and r.json()["stats"]["words"] == 1
    assert sent[0]["source"] == "claude" and "Mei Lin" in sent[0]["terms"] and "Mei" in sent[0]["terms"]
    assert "Owen Hart" in sent[0]["terms"] and "Mei L…" not in sent[0]["terms"]  # truncated screen names left out
    labels = json.loads((main.LABELS_DIR / f"{TID}.json").read_text())
    assert labels["SPEAKER_00"]["name"] == "Priya Sharma" and labels["SPEAKER_00"]["source"] == "reference"
    assert labels["SPEAKER_02"]["name"] == "Vinod Pungle"  # your microphone's label is never replaced
    assert TID not in main._TRANSCRIPT_CHUNKS  # search reads the corrected words
    assert json.loads((main.SUMMARIES_DIR / f"{TID}.json").read_text())["stale"] is True

    # Removing the source withdraws the names it gave.
    monkeypatch.setattr(meetings, "delete_reference", lambda tid, rid: {"refs": [], "changes": [], "names": {}})
    client.delete(f"/api/meetings/transcripts/{TID}/references/r1")
    labels = json.loads((main.LABELS_DIR / f"{TID}.json").read_text())
    assert "SPEAKER_00" not in labels and labels["SPEAKER_02"]["source"] == "microphone"
    assert json.loads((main.SUMMARIES_DIR / f"{TID}.json").read_text())["stale"] is False


def test_a_text_file_and_bad_input(client, monkeypatch):
    sent = []
    setup_world(monkeypatch, sent)
    r = client.post(f"/api/meetings/transcripts/{TID}/references",
                    files={"file": ("Voice memo.txt", b"Thanks Priya. The build is green.", "text/plain")},
                    data={"source": "voice_memos"})
    assert r.status_code == 200 and sent[0]["name"] == "Voice memo" and sent[0]["source"] == "voice_memos"
    assert client.post(f"/api/meetings/transcripts/{TID}/references", json={"text": "  "}).status_code == 400
    bad = client.post(f"/api/meetings/transcripts/{TID}/references",
                      files={"file": ("memo.exe", b"x", "application/octet-stream")})
    assert bad.status_code == 415


def test_audio_undo_apply_and_one_change(client, monkeypatch):
    sent, calls = [], []
    setup_world(monkeypatch, sent)
    monkeypatch.setattr(meetings, "add_audio_reference",
                        lambda tid, path, source, name, terms: calls.append(("audio", path, source)) or
                        {"job": {"id": "j1"}, "ref": {"id": "r2", "status": "transcribing"}})
    monkeypatch.setattr(meetings, "correction_action",
                        lambda tid, action, cid=None, terms=None: calls.append((action, cid, bool(terms))) or
                        {"refs": [], "changes": [], "names": {}})
    r = client.post(f"/api/meetings/transcripts/{TID}/references/audio", json={"path": "/x/uploads/memo.m4a"})
    assert r.json()["ref"]["status"] == "transcribing"
    client.post(f"/api/meetings/transcripts/{TID}/corrections/undo")
    client.post(f"/api/meetings/transcripts/{TID}/corrections/apply")
    client.post(f"/api/meetings/transcripts/{TID}/corrections/0123456789/revert")
    assert client.post(f"/api/meetings/transcripts/{TID}/corrections/delete-all").status_code == 404
    assert calls == [("audio", "/x/uploads/memo.m4a", "voice_memos"), ("undo", None, False), ("apply", None, True),
                     ("revert", "0123456789", False)]


def test_importing_conversation_text_still_makes_a_transcript(client, monkeypatch):
    """The text extraction is shared with the corrections; the import route must still answer with the
    transcript."""
    monkeypatch.setattr(meetings, "import_text", lambda title, text, name: {"id": "0123456789abcdef", "title": title})
    r = client.post("/api/meetings/import", json={"title": "Retro", "text": "Priya: Hello.\nOwen: Hi."})
    assert r.status_code == 200 and r.json() == {"id": "0123456789abcdef", "title": "Retro"}
    r = client.post("/api/meetings/import", files={"file": ("retro.vtt", b"WEBVTT\n\n00:00.000 --> 00:01.000\nHi")},
                    data={"title": "Retro file"})
    assert r.json()["title"] == "Retro file"
