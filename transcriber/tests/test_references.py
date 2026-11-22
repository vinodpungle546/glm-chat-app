"""Other transcripts of a meeting through the transcriber's API: pasted text corrects at once, a Voice Memo
recording is transcribed as a job (Whisper stubbed) and then corrects; revert, restore, Undo all, apply again,
remove a source, and clean-up. Synthetic meeting only (tests/test_correct.py).

    transcriber/.venv/bin/python -m pytest transcriber/tests/test_references.py -q
"""

import subprocess
import time

import pytest
from fastapi.testclient import TestClient

from transcriber.tests.test_correct import QUORUM_MISTAKES, SCRIPT, build

TID = "abcdef0123456789"


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("HF_HUB_OFFLINE", "1")
    from transcriber import config, pipeline, server
    monkeypatch.setattr(config, "MEETINGS_DIR", tmp_path / "meetings")
    monkeypatch.setattr(config, "DEFAULT_MEETINGS_DIR", tmp_path / "meetings")
    monkeypatch.setattr(config, "UPLOADS_DIR", tmp_path / "meetings" / "uploads")
    monkeypatch.setattr(config, "TRANSCRIPTS_DIR", tmp_path / "transcripts")
    monkeypatch.setattr(config, "WORK_DIR", tmp_path / "transcripts" / ".work")
    monkeypatch.setattr(server, "whisper_cached", lambda: True)
    monkeypatch.setattr(pipeline, "diarization_available", lambda: False)
    segs, words = build(SCRIPT, QUORUM_MISTAKES)
    pipeline.save_transcript({"id": TID, "title": "Release sync", "source_path": "", "created_at": "2026-11-22T10:00:00",
                              "duration": 120, "speakers": ["SPEAKER_00", "SPEAKER_01", "SPEAKER_02"],
                              "speaker_names": {}, "confirmed_speakers": {"SPEAKER_02": {"source": "microphone"}},
                              "segments": segs})
    pipeline.save_words(TID, words)
    with TestClient(server.app) as c:
        yield c, config, pipeline, segs


def claude_text():
    segs, _ = build(SCRIPT)
    return "\n".join(s["text"] for s in segs)


def text(c):
    return " ".join(s["text"] for s in c.get(f"/transcripts/{TID}").json()["segments"])


def test_pasted_text_corrects_and_everything_can_be_undone(client):
    c, _config, _pipeline, original = client
    r = c.post(f"/transcripts/{TID}/references", json={"source": "claude", "text": claude_text(),
                                                       "known_terms": ["Mei Lin"]})
    assert r.status_code == 200
    correction = r.json()
    assert correction["stats"]["words"] >= 4 and correction["refs"][0]["applied"]
    assert correction["refs"][0]["name"] == "claude.ai" and correction["known_terms"] == ["Mei Lin"]
    assert "Thanks Priya." in text(c) and "Jira ticket" in text(c)
    assert c.get("/transcripts").json()[0]["corrected"] is True
    assert "Jira" in c.get(f"/transcripts/{TID}/markdown").text  # the Markdown follows

    first = correction["changes"][0]
    after = c.post(f"/transcripts/{TID}/corrections/{first['id']}/revert").json()
    assert first["id"] in after["reverted"]
    assert [ch["id"] for ch in after["changes"] if ch.get("reverted")] == [first["id"]]
    after = c.post(f"/transcripts/{TID}/corrections/{first['id']}/restore").json()
    assert first["id"] in [ch["id"] for ch in after["changes"]]

    undone = c.post(f"/transcripts/{TID}/corrections/undo").json()
    assert undone["undone"] is True and undone["changes"] == []
    assert c.get(f"/transcripts/{TID}").json()["segments"] == original
    again = c.post(f"/transcripts/{TID}/corrections/apply", json={}).json()
    assert again["undone"] is False and "Thanks Priya." in text(c)

    listed = c.get(f"/transcripts/{TID}/references").json()
    assert [r["source"] for r in listed["refs"]] == ["claude"] and "text" not in listed["refs"][0]
    c.delete(f"/transcripts/{TID}/references/{listed['refs'][0]['id']}")
    t = c.get(f"/transcripts/{TID}").json()
    assert t["segments"] == original and "correction" not in t and "before_correction" not in t


def test_unrelated_text_changes_nothing(client):
    c, _config, _pipeline, original = client
    r = c.post(f"/transcripts/{TID}/references", json={"source": "other", "name": "Wrong meeting",
               "text": "Budget review for the spring campaign, agency fees and the influencer program."}).json()
    assert r["refs"][0]["applied"] is False and r["refs"][0]["reason"] == "doesn't match this meeting"
    assert c.get(f"/transcripts/{TID}").json()["segments"] == original
    assert c.post(f"/transcripts/{TID}/references", json={"text": "   "}).status_code in (422, 200)


def test_a_voice_memo_is_transcribed_then_corrects(client, monkeypatch):
    c, config, pipeline, _original = client
    config.UPLOADS_DIR.mkdir(parents=True, exist_ok=True)
    memo = config.UPLOADS_DIR / "New Recording 12.m4a"
    subprocess.run(["ffmpeg", "-nostdin", "-y", "-loglevel", "error", "-f", "lavfi", "-i",
                    "sine=frequency=300:duration=2", "-c:a", "aac", str(memo)], check=True)
    _segs, memo_words = build(SCRIPT, start=40.0)
    for w in memo_words:
        w.pop("p")
    monkeypatch.setattr(pipeline, "run_reference", lambda tid, src, progress: memo_words)
    r = c.post(f"/transcripts/{TID}/references/audio", json={"path": str(memo)}).json()
    assert r["ref"]["status"] == "transcribing" and r["ref"]["name"] == "New Recording 12"
    assert not memo.exists() and not any("Recording 12" in x["name"] for x in c.get("/recordings").json())
    job = r["job"]
    for _ in range(100):
        job = c.get(f"/jobs/{job['id']}").json()
        if job["status"] in ("done", "error"):
            break
        time.sleep(0.05)
    assert job["status"] == "done", job
    correction = c.get(f"/transcripts/{TID}/references").json()["correction"]
    ref = correction["refs"][0]
    assert ref["status"] == "ready" and ref["kind"] == "audio" and abs(ref["offset"] + 40) < 1.5
    assert "Thanks Priya." in text(c)  # Whisper was unsure of "Pria." (p 0.3)

    hidden = list(config.UPLOADS_DIR.glob(".ref-*"))
    assert len(hidden) == 1
    c.delete(f"/transcripts/{TID}")
    assert not list(config.UPLOADS_DIR.glob(".ref-*")) and not (config.TRANSCRIPTS_DIR / ".refs" / TID).exists()
    assert not pipeline.words_file(TID).exists()


def test_new_speaker_segments_drop_a_correction_made_on_the_old_ones(client):
    c, _config, _pipeline, _original = client
    c.post(f"/transcripts/{TID}/references", json={"source": "claude", "text": claude_text()})
    segments = c.get(f"/transcripts/{TID}").json()["segments"]
    c.put(f"/transcripts/{TID}/segments", json={"segments": segments, "speaker_names": {}, "speaker_source": "test"})
    t = c.get(f"/transcripts/{TID}").json()
    assert "before_correction" not in t and "correction" not in t
