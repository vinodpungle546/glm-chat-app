"""Recording calls: the transcriber drives a fake Quorum Recorder (fake_recorder.py, same events as the real app)
through /record/*. Whisper never runs: pipeline.run is replaced. Synthetic files only.

    transcriber/.venv/bin/python -m pytest transcriber/tests -q
"""

import json
import os
import shlex
import signal
import sys
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

FAKE = Path(__file__).with_name("fake_recorder.py")


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("HF_HUB_OFFLINE", "1")
    monkeypatch.setenv("QUORUM_RECORD_BIN", shlex.join([sys.executable, str(FAKE)]))
    monkeypatch.setenv("FAKE_RECORD_TICK", "0.05")
    from transcriber import config, live, pipeline, recorder, server
    monkeypatch.setattr(config, "MEETINGS_DIR", tmp_path / "meetings")
    monkeypatch.setattr(config, "DEFAULT_MEETINGS_DIR", tmp_path / "meetings")
    monkeypatch.setattr(config, "UPLOADS_DIR", tmp_path / "meetings" / "uploads")
    monkeypatch.setattr(config, "TRANSCRIPTS_DIR", tmp_path / "transcripts")
    monkeypatch.setattr(config, "WORK_DIR", tmp_path / "transcripts" / ".work")
    monkeypatch.setattr(server, "whisper_cached", lambda: True)
    monkeypatch.setattr(pipeline, "diarization_available", lambda: False)
    monkeypatch.setattr(recorder, "MIN_FREE_BYTES", 0)
    ran = []

    def fake_run(src, progress, speakers=None, screen=None, models=None):
        media = pipeline.probe_media(src)
        ran.append({"src": src, "roles": pipeline.track_roles(src, media), "models": models})
        t = {"id": "0123456789abcdef", "title": src.stem, "source_name": src.name, "source_path": str(src),
             "created_at": "2026-11-22T14:05:00", "duration": media["duration"], "diarization": True,
             "speakers": ["SPEAKER_00"], "speaker_names": {}, "segments": []}
        pipeline.save_transcript(t)
        return t

    monkeypatch.setattr(pipeline, "run", fake_run)
    with TestClient(server.app) as c:
        yield c, config, ran
    state = recorder.load_state()  # never leave a fake helper running
    if state and recorder.alive(state.get("pid")):
        os.kill(state["pid"], signal.SIGKILL)
    for draft in list(live.DRAFTS.values()):
        draft.stop()
    live.DRAFTS.clear()


def wait(predicate, timeout=10.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.05)
    raise AssertionError("timed out")


def test_sources_list_calls_first_then_remote_desktops(client):
    c, _config, _ran = client
    data = c.get("/record/sources").json()
    assert [(a["name"], a["meeting"], a["remote"]) for a in data["apps"]] == [
        ("zoom.us", True, False), ("Windows App", False, True), ("TextEdit", False, False)]
    assert data["microphones"][1] == {"id": "airpods-uid", "name": "AirPods Pro", "default": False, "bluetooth": True}
    assert data["last"] == {}


def test_record_then_stop_transcribes_with_separate_tracks(client):
    c, config, ran = client
    started = c.post("/record/start", json={"source": {"kind": "app", "id": "us.zoom.xos"}, "title": "Release sync"})
    assert started.status_code == 200 and started.json()["state"] == "recording"
    # A call in progress is never offered as a recording to transcribe (hidden file).
    assert not any("recording-" in r["name"] for r in c.get("/recordings").json())
    assert c.get("/health").json()["recording"] is True
    status = wait(lambda: (s := c.get("/record/status").json())["elapsed"] and s["elapsed"] >= 3 and s)
    assert status["levels"]["system"] > 0 and status["source"] == {"kind": "app", "id": "us.zoom.xos"}
    assert c.post("/record/start", json={"source": {"kind": "audio"}}).status_code == 409  # one at a time

    done = c.post("/record/stop", json={"models": {"speaker_model": "gemma4:e4b", "frames_model": "",
                                                    "summary_model": "", "after": True}}).json()
    path = Path(done["recording"]["path"])
    assert path.name.startswith("Release sync ") and path.suffix == ".mov" and path.exists()
    assert Path(str(path) + ".quorum.json").exists()  # the track layout travels with the file
    assert done["job"]["recording_id"] == done["recording"]["id"]
    job = wait(lambda: (j := c.get(f"/jobs/{done['job']['id']}").json())["status"] == "done" and j)
    assert ran[0]["roles"] == {"system": 0, "mic": 1} and ran[0]["models"]["after"] is True
    t = json.loads((config.TRANSCRIPTS_DIR / f"{job['transcript_id']}.json").read_text())
    assert t["recording_id"] == done["recording"]["id"]
    assert c.get("/record/status").json() == {"state": "idle"}
    assert c.get("/health").json()["recording"] is False
    assert c.get("/record/sources").json()["last"]["source"] == {"kind": "app", "id": "us.zoom.xos"}


def test_permission_problems_are_explained(client, monkeypatch):
    c, _config, _ran = client
    monkeypatch.setenv("FAKE_RECORD_ERROR", "permission_screen")
    r = c.post("/record/start", json={"source": {"kind": "display", "id": 1}})
    assert r.status_code == 503 and r.json()["detail"]["kind"] == "permission_screen"
    assert c.get("/record/status").json() == {"state": "idle"}


def test_without_screen_access_macos_picker_chooses_the_window(client, monkeypatch):
    """No Screen Recording permission (e.g. a managed Mac): sources say so, Start opens macOS's picker and records
    what was picked; closing the picker records nothing."""
    c, _config, _ran = client
    monkeypatch.setenv("FAKE_RECORD_PICKER", "1")
    assert c.get("/record/sources").json()["picker"] is True
    monkeypatch.setenv("FAKE_RECORD_PICK", "cancel")
    r = c.post("/record/start", json={"source": {"kind": "app"}})
    assert r.status_code == 409 and r.json()["detail"]["kind"] == "cancelled"
    assert c.get("/record/status").json() == {"state": "idle"}
    monkeypatch.delenv("FAKE_RECORD_PICK")
    started = c.post("/record/start", json={"source": {"kind": "app"}, "title": "Picked"})
    assert started.status_code == 200
    assert started.json()["source"] == {"kind": "app", "id": "us.zoom.xos", "name": "zoom.us", "picked": True}
    done = c.post("/record/stop", json={"transcribe": False}).json()
    layout = json.loads(Path(done["recording"]["path"] + ".quorum.json").read_text())
    assert layout["source"]["picked"] is True


def test_a_recorded_call_plays_back_with_separate_call_and_mic_sound(client):
    """The meeting page's player: the call and your mic as two sounds (copied once from the recording, cached),
    served with Range support so the player can seek; the cache goes with the transcript."""
    c, config, _ran = client
    c.post("/record/start", json={"source": {"kind": "audio"}, "title": "Playback"})
    wait(lambda: c.get("/record/status").json().get("elapsed"))
    done = c.post("/record/stop", json={"models": {}}).json()
    job = wait(lambda: (j := c.get(f"/jobs/{done['job']['id']}").json())["status"] == "done" and j)
    tid = job["transcript_id"]

    info = c.get(f"/transcripts/{tid}/media").json()
    assert info["available"] and info["video"] is False
    assert info["parts"] == [{"part": "call", "label": "Call"}, {"part": "mic", "label": "You"}]
    part = c.get(f"/transcripts/{tid}/media/call", headers={"Range": "bytes=0-99"})
    assert part.status_code == 206 and part.headers["content-range"].startswith("bytes 0-99/")
    assert part.headers["content-type"] == "audio/mp4" and len(part.content) == 100
    mic = c.get(f"/transcripts/{tid}/media/mic")
    assert mic.status_code == 200 and mic.content[4:8] == b"ftyp"  # an MP4/M4A file
    assert (config.TRANSCRIPTS_DIR / ".media" / tid / "call.m4a").exists()
    assert c.get(f"/transcripts/{tid}/media/subtitles").status_code == 404
    assert c.get(f"/transcripts/{tid}/media/../../x").status_code in (400, 404)  # refused
    c.delete(f"/transcripts/{tid}")
    assert not (config.TRANSCRIPTS_DIR / ".media" / tid).exists()


def test_a_missing_recording_or_a_text_import_is_explained(client):
    c, config, _ran = client
    from transcriber import pipeline
    pipeline.save_transcript({"id": "fedcba9876543210", "title": "Gone", "source_path": "/elsewhere/gone.mov",
                              "created_at": "2026-11-22T10:00:00", "duration": 5, "speakers": [], "segments": []})
    info = c.get("/transcripts/fedcba9876543210/media").json()
    assert info == {"available": False, "reason": "missing", "path": "/elsewhere/gone.mov",
                    "message": "The recording isn't in the meetings or uploads folder any more."}
    assert c.get("/transcripts/fedcba9876543210/media/audio").status_code == 404


def test_an_app_without_an_id_is_chosen_in_the_picker():
    from transcriber import recorder
    assert recorder.source_args({"kind": "app"}) == ["--app-any"]
    assert recorder.source_args({"kind": "app", "id": "us.zoom.xos"}) == ["--app", "us.zoom.xos"]


def test_a_mic_switch_mid_call_is_reported_and_the_recording_continues(client, monkeypatch):
    c, _config, _ran = client
    monkeypatch.setenv("FAKE_RECORD_MIC_SWITCH", "2")
    c.post("/record/start", json={"source": {"kind": "audio"}})
    status = wait(lambda: (s := c.get("/record/status").json())["warnings"] and s)
    assert status["warnings"][0]["kind"] == "mic_switched" and status["warnings"][0]["mic"] == "AirPods Pro"
    later = wait(lambda: (s := c.get("/record/status").json())["elapsed"] >= 5 and s)
    assert later["state"] == "recording"
    assert c.post("/record/stop", json={"transcribe": False}).json()["recording"]["complete"] is True


def test_a_silent_call_track_warns(client, monkeypatch):
    c, _config, _ran = client
    from transcriber import recorder
    monkeypatch.setattr(recorder, "CALL_SILENT_SECONDS", 3)
    monkeypatch.setenv("FAKE_RECORD_CALL_SILENT", "1")
    c.post("/record/start", json={"source": {"kind": "app", "id": "us.zoom.xos"}})
    def silent_warning():
        s = c.get("/record/status").json()
        return s if any(w["kind"] == "call_silent" for w in s["warnings"]) else None

    status = wait(silent_warning)
    assert "No sound from the call" in status["warnings"][-1]["message"]
    c.post("/record/stop", json={"transcribe": False})


def test_a_recording_that_ends_by_itself_can_still_be_collected(client, monkeypatch):
    c, _config, ran = client
    monkeypatch.setenv("FAKE_RECORD_STOP_AFTER", "3")
    c.post("/record/start", json={"source": {"kind": "app", "id": "us.zoom.xos"}})
    status = wait(lambda: (s := c.get("/record/status").json())["state"] == "stopped" and s)
    assert status["reason"] == "app_quit"
    done = c.post("/record/stop", json={}).json()
    assert done["recording"]["reason"] == "app_quit" and done["job"] is not None


def test_an_interrupted_recorder_is_reported(client):
    c, _config, _ran = client
    from transcriber import recorder
    c.post("/record/start", json={"source": {"kind": "audio"}})
    os.kill(recorder.load_state()["pid"], signal.SIGKILL)
    wait(lambda: c.get("/record/status").json()["state"] == "interrupted")
    done = c.post("/record/stop", json={}).json()
    assert done["recording"]["reason"] == "interrupted" and done["job"] is None  # nothing was written


def test_a_queued_transcription_waits_while_a_call_is_recorded(client, tmp_path):
    c, config, ran = client
    import subprocess
    config.MEETINGS_DIR.mkdir(parents=True, exist_ok=True)
    other = config.MEETINGS_DIR / "earlier.m4a"
    subprocess.run(["ffmpeg", "-nostdin", "-y", "-loglevel", "error", "-f", "lavfi", "-i",
                    "sine=frequency=300:duration=2", str(other)], check=True)
    c.post("/record/start", json={"source": {"kind": "audio"}})
    job = c.post("/jobs", json={"path": str(other)}).json()
    wait(lambda: c.get(f"/jobs/{job['id']}").json()["stage"] == "waiting_for_call")
    assert not ran
    c.post("/record/stop", json={"transcribe": False})
    wait(lambda: c.get(f"/jobs/{job['id']}").json()["status"] == "done")


def test_track_titles_are_read_from_a_real_file(tmp_path):
    import subprocess
    from transcriber import pipeline
    path = tmp_path / "two.mov"
    subprocess.run(["ffmpeg", "-nostdin", "-y", "-loglevel", "error",
                    "-f", "lavfi", "-i", "sine=frequency=440:duration=1", "-f", "lavfi", "-i", "anullsrc=d=1",
                    "-map", "0:a", "-map", "1:a", "-c:a", "aac", "-t", "1",
                    "-metadata:s:a:0", "handler_name=System audio", "-metadata:s:a:1", "handler_name=Microphone", str(path)],
                   check=True)
    media = pipeline.probe_media(path)
    assert media["audio_titles"] == ["System audio", "Microphone"]
    assert pipeline.track_roles(path, media) == {"system": 0, "mic": 1}
