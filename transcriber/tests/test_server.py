"""Transcriber HTTP service tests (run with the transcriber venv):

    transcriber/.venv/bin/python -m pytest transcriber/tests -q
"""

import importlib
import os

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("HF_HUB_OFFLINE", "1")
    from transcriber import config, pipeline, server
    monkeypatch.setattr(config, "MEETINGS_DIR", tmp_path / "meetings")
    monkeypatch.setattr(config, "DEFAULT_MEETINGS_DIR", tmp_path / "meetings")
    monkeypatch.setattr(config, "UPLOADS_DIR", tmp_path / "meetings" / "uploads")
    monkeypatch.setattr(config, "TRANSCRIPTS_DIR", tmp_path / "transcripts")
    monkeypatch.setattr(config, "WORK_DIR", tmp_path / "transcripts" / ".work")
    monkeypatch.setattr(config, "UPLOAD_CHUNK_BYTES", 10)
    monkeypatch.setattr(server, "whisper_cached", lambda: True)
    monkeypatch.setattr(pipeline, "diarization_available", lambda: False)
    with TestClient(server.app) as c:
        yield c, config


def test_health_reports_offline(client):
    c, _ = client
    data = c.get("/health").json()
    assert data["offline"] is True and data["diarization"] is False


def test_refuses_to_start_when_not_offline(monkeypatch):
    from transcriber import server
    monkeypatch.setenv("HF_HUB_OFFLINE", "0")
    with pytest.raises(RuntimeError, match="offline"):
        server._startup()


def test_path_traversal_rejected(client):
    c, _ = client
    r = c.post("/estimate", json={"path": "../../etc/passwd"})
    assert r.status_code == 400


def test_chunked_upload_resume_and_assemble(client):
    c, config = client
    payload = b"0123456789abcdefghijXYZ"  # 23 bytes -> 3 chunks of 10
    init = c.post("/uploads/init", json={"name": "talk.m4a", "size": len(payload)}).json()
    uid = init["upload_id"]
    assert init["received"] == []
    c.put(f"/uploads/{uid}/chunk/0", content=payload[0:10])
    # "Interrupted": init again with the same file -> resume info
    again = c.post("/uploads/init", json={"name": "talk.m4a", "size": len(payload)}).json()
    assert again["upload_id"] == uid and again["received"] == [0]
    assert c.post(f"/uploads/{uid}/complete").status_code == 409  # incomplete
    c.put(f"/uploads/{uid}/chunk/1", content=payload[10:20])
    c.put(f"/uploads/{uid}/chunk/2", content=payload[20:])
    done = c.post(f"/uploads/{uid}/complete").json()
    assert done["path"] == str((config.UPLOADS_DIR / "talk.m4a").resolve())
    assert (config.MEETINGS_DIR / "uploads" / "talk.m4a").read_bytes() == payload


def test_upload_rejects_non_media_and_too_big(client, monkeypatch):
    c, _ = client
    assert c.post("/uploads/init", json={"name": "notes.exe", "size": 5}).status_code == 400
    from transcriber import pipeline
    monkeypatch.setattr(pipeline, "free_disk_bytes", lambda _p: 2 * 1024**3)
    r = c.post("/uploads/init", json={"name": "huge.mp4", "size": 5 * 1024**3})
    assert r.status_code == 507


def test_speaker_names_saved(client):
    c, config = client
    from transcriber import pipeline
    t = {"id": "0123456789abcdef", "title": "T", "source_name": "a.m4a", "duration": 5,
         "language": "en", "diarization": True, "speakers": ["SPEAKER_00"], "speaker_names": {},
         "segments": [{"speaker": "SPEAKER_00", "start": 0, "end": 1, "text": "Hi."}],
         "created_at": "2026-01-01"}
    pipeline.save_transcript(t)
    r = c.post("/transcripts/0123456789abcdef/speakers", json={"names": {"SPEAKER_00": "Priya", "X": "y"}})
    assert r.json()["speaker_names"] == {"SPEAKER_00": "Priya"}
    assert "Priya: Hi." in c.get("/transcripts/0123456789abcdef/markdown").text


def test_eta_uses_estimate_early_and_rate_later():
    from transcriber.server import eta_seconds
    assert eta_seconds(elapsed=10, progress=0.06, estimate=90) == 80
    assert eta_seconds(elapsed=60, progress=0.5, estimate=90) == 60  # rate: 60s done -> 60s left
    assert eta_seconds(elapsed=200, progress=0.05, estimate=90) == 0


def test_diarization_available_needs_every_repo(monkeypatch):
    import huggingface_hub
    from transcriber import config, pipeline
    cached = {repo for repo, _ in config.DIARIZATION_REPOS} - {"pyannote/speaker-diarization-community-1"}

    def fake_snapshot(repo, allow_patterns=None, local_files_only=False):
        assert local_files_only
        if repo not in cached:
            raise huggingface_hub.errors.LocalEntryNotFoundError("missing")
        return "/x"

    monkeypatch.setattr(huggingface_hub, "snapshot_download", fake_snapshot)
    assert pipeline.diarization_available() is False
    cached.add("pyannote/speaker-diarization-community-1")
    assert pipeline.diarization_available() is True


def test_watch_folder_change_persist_and_reset(client, tmp_path):
    c, config = client
    other = tmp_path / "elsewhere"
    assert c.post("/settings", json={"meetings_dir": str(other)}).status_code == 404  # missing
    assert c.post("/settings", json={"meetings_dir": "relative/dir"}).status_code == 400
    r = c.post("/settings", json={"meetings_dir": str(other), "create": True})
    assert r.status_code == 200 and r.json()["meetings_dir"] == str(other.resolve())
    (other / "sub").mkdir()
    (other / "sub" / "standup.mp4").write_bytes(b"x")
    (other / ".hidden").mkdir()
    (other / ".hidden" / "secret.mp4").write_bytes(b"x")
    (config.UPLOADS_DIR / "up.m4a").write_bytes(b"x")
    recs = {r["name"]: r for r in c.get("/recordings").json()}
    assert set(recs) == {"standup.mp4", "up.m4a"}  # watch folder (recursive) + uploads, no hidden
    assert recs["up.m4a"]["source"] == "upload" and recs["standup.mp4"]["source"] == "folder"

    from transcriber import server
    config.MEETINGS_DIR = config.DEFAULT_MEETINGS_DIR
    server.load_settings()  # restart: saved folder comes back
    assert config.MEETINGS_DIR == other.resolve()
    r = c.post("/settings", json={"meetings_dir": ""})
    assert r.json()["meetings_dir"] == str(config.DEFAULT_MEETINGS_DIR)


def test_paths_outside_watch_and_upload_folders_rejected(client, tmp_path):
    c, _ = client
    outside = tmp_path / "outside.mp4"
    outside.write_bytes(b"x")
    assert c.post("/estimate", json={"path": str(outside)}).status_code == 400


def test_job_passes_speaker_count_and_validates_it(client, monkeypatch):
    c, config = client
    from transcriber import pipeline
    seen = []
    monkeypatch.setattr(pipeline, "probe_media", lambda _p: {"duration": 60.0, "audio_tracks": 1})
    monkeypatch.setattr(pipeline, "run", lambda path, progress, speakers=None, screen=None, models=None: seen.append(speakers)
                        or {"id": "0123456789abcdef"})
    rec = config.UPLOADS_DIR / "m.m4a"
    rec.write_bytes(b"x")
    bad = c.post("/jobs", json={"path": str(rec), "speakers": {"num": 3, "max": 4}})
    assert bad.status_code == 422
    assert c.post("/jobs", json={"path": str(rec), "speakers": {"num": 99}}).status_code == 422
    job = c.post("/jobs", json={"path": str(rec), "speakers": {"num": 5}}).json()
    for _ in range(100):
        if c.get(f"/jobs/{job['id']}").json()["status"] == "done":
            break
        import time
        time.sleep(0.02)
    assert seen == [{"num": 5}]


def test_import_conversation_text_keeps_user_names_on_reimport(client):
    c, _ = client
    vtt = ("WEBVTT\n\n00:00:01.000 --> 00:00:03.000\n<v Priya Shah>Revenue grew.</v>\n\n"
           "00:00:04.000 --> 00:00:06.000\n<v Raj>Costs are up.</v>\n")
    meta = c.post("/transcripts/import", json={"title": "Budget", "text": vtt,
                                               "source_name": "budget.vtt"}).json()
    assert meta["speakers"] == ["SPEAKER_00", "SPEAKER_01"] and meta["source_type"] == "text"
    assert meta["speaker_names"] == {"SPEAKER_00": "Priya Shah", "SPEAKER_01": "Raj"}
    c.post(f"/transcripts/{meta['id']}/speakers", json={"names": {"SPEAKER_01": "Raj Kumar"}})
    again = c.post("/transcripts/import", json={"title": "Budget", "text": vtt,
                                                "source_name": "budget.vtt"}).json()
    assert again["speaker_names"]["SPEAKER_01"] == "Raj Kumar"
    md = c.get(f"/transcripts/{meta['id']}/markdown").text
    assert "[00:00:04] Raj Kumar: Costs are up." in md
    assert c.post("/transcripts/import", json={"text": "   "}).status_code == 400


def test_delete_transcript_and_only_uploaded_recordings(client):
    c, config = client
    from transcriber import pipeline
    upload = config.UPLOADS_DIR / "up.m4a"
    upload.write_bytes(b"x")
    config.MEETINGS_DIR.mkdir(parents=True, exist_ok=True)
    watched = config.MEETINGS_DIR / "kept.m4a"
    watched.write_bytes(b"x")
    base = {"title": "T", "duration": 1, "diarization": False, "speakers": [], "speaker_names": {},
            "segments": [{"speaker": "SPEAKER_00", "start": 0, "end": 1, "text": "Hi."}],
            "created_at": "2026-01-01"}
    pipeline.save_transcript(dict(base, id="aaaaaaaaaaaaaaaa", source_name="up.m4a", source_path=str(upload)))
    pipeline.save_transcript(dict(base, id="bbbbbbbbbbbbbbbb", source_name="kept.m4a", source_path=str(watched)))
    index = {t["id"]: t for t in c.get("/transcripts").json()}
    assert index["aaaaaaaaaaaaaaaa"]["uploaded_recording"] is True
    assert index["bbbbbbbbbbbbbbbb"]["uploaded_recording"] is False

    r = c.delete("/transcripts/bbbbbbbbbbbbbbbb", params={"recording": "true"}).json()
    assert r["removed_recording"] is None and watched.exists()  # watch-folder file never deleted
    r = c.delete("/transcripts/aaaaaaaaaaaaaaaa", params={"recording": "true"}).json()
    assert r["removed_recording"] == "up.m4a" and not upload.exists()
    assert c.get("/transcripts").json() == []
    assert not list(config.TRANSCRIPTS_DIR.glob("aaaaaaaaaaaaaaaa.*"))
    assert c.delete("/transcripts/aaaaaaaaaaaaaaaa").status_code == 404
    assert c.delete("/transcripts/..%2F..%2Fetc").status_code in (400, 404)


def test_guessed_speakers_saved_and_undone(client):
    c, _ = client
    meta = c.post("/transcripts/import", json={"title": "Raw", "text": "Hi Lauren. Yes, all good."}).json()
    assert meta["diarization"] is False
    segs = [{"speaker": "SPEAKER_00", "start": 0, "end": 0, "text": "Hi Lauren."},
            {"speaker": "SPEAKER_01", "start": 0, "end": 0, "text": "Yes, all good."}]
    bad = c.put(f"/transcripts/{meta['id']}/segments",
                json={"segments": [dict(segs[0], speaker="../x")], "speaker_source": "m"})
    assert bad.status_code == 422
    out = c.put(f"/transcripts/{meta['id']}/segments", json={
        "segments": segs, "speaker_names": {"SPEAKER_01": "Lauren"}, "speaker_source": "qwen3:8b"}).json()
    assert out["diarization"] and out["speaker_source"] == "qwen3:8b" and out["can_undo_speakers"]
    md = c.get(f"/transcripts/{meta['id']}/markdown").text
    assert "guessed from the text by qwen3:8b" in md and "Lauren: Yes, all good." in md
    back = c.post(f"/transcripts/{meta['id']}/undo-speakers").json()
    assert back["diarization"] is False and not back.get("speaker_source") and not back["can_undo_speakers"]
    assert c.post(f"/transcripts/{meta['id']}/undo-speakers").status_code == 409


def test_screen_recording_without_sound_is_refused_with_advice(client, monkeypatch):
    c, config = client
    from transcriber import pipeline
    monkeypatch.setattr(pipeline, "probe_media", lambda _p: {"duration": 30.0, "audio_tracks": 0})
    rec = config.UPLOADS_DIR / "Screen Recording 2026-11-13 at 3.23.36 PM.mov"
    rec.write_bytes(b"x")
    est = c.post("/estimate", json={"path": str(rec)}).json()
    assert est["has_audio"] is False and "Options → Microphone" in est["no_audio_message"]
    job = c.post("/jobs", json={"path": str(rec)})
    assert job.status_code == 422 and "no sound" in job.json()["detail"]


def test_upload_name_keeps_macos_screen_recording_name():
    from transcriber import server
    assert server._clean_name("Screen Recording 2026-11-13 at 3.23.36\u202fPM.mov") == (
        "Screen Recording 2026-11-13 at 3.23.36 PM.mov")


def test_cancel_queued_and_running_jobs_and_partial_text(client, monkeypatch):
    import threading
    import time
    c, config = client
    from transcriber import pipeline
    monkeypatch.setattr(pipeline, "probe_media", lambda _p: {"duration": 60.0, "audio_tracks": 1})
    gate = threading.Event()

    def slow_run(path, progress, speakers=None, screen=None, models=None):
        progress({"stage": "transcribe", "progress": 0.1})
        gate.wait(5)
        progress({"stage": "transcribe", "progress": 0.5})  # raises once cancel was requested
        return {"id": "0123456789abcdef"}

    monkeypatch.setattr(pipeline, "run", slow_run)
    monkeypatch.setattr(pipeline, "partial_segments",
                        lambda _p: {"segments": [{"start": 0, "end": 1, "text": "Hello."}], "windows": 1})
    first, second = config.UPLOADS_DIR / "a.m4a", config.UPLOADS_DIR / "b.m4a"
    first.write_bytes(b"x")
    second.write_bytes(b"y")
    running = c.post("/jobs", json={"path": str(first)}).json()
    queued = c.post("/jobs", json={"path": str(second)}).json()
    for _ in range(100):
        if c.get(f"/jobs/{running['id']}").json()["status"] == "running":
            break
        time.sleep(0.02)
    assert c.get(f"/jobs/{running['id']}/partial").json()["segments"][0]["text"] == "Hello."
    assert c.post(f"/jobs/{queued['id']}/cancel").json()["status"] == "cancelled"
    c.post(f"/jobs/{running['id']}/cancel")
    gate.set()
    for _ in range(100):
        if c.get(f"/jobs/{running['id']}").json()["status"] == "cancelled":
            break
        time.sleep(0.02)
    assert c.get(f"/jobs/{running['id']}").json()["status"] == "cancelled"
    assert c.get(f"/jobs/{queued['id']}").json()["status"] == "cancelled"


def test_job_keeps_the_meetings_model_choices(client, monkeypatch):
    c, config = client
    from transcriber import pipeline
    seen = []
    monkeypatch.setattr(pipeline, "probe_media", lambda _p: {"duration": 60.0, "audio_tracks": 1})
    monkeypatch.setattr(pipeline, "run", lambda path, progress, speakers=None, screen=None, models=None:
                        seen.append(models) or {"id": "0123456789abcdef"})
    rec = config.UPLOADS_DIR / "m.m4a"
    rec.write_bytes(b"x")
    models = {"speaker_model": "gemma4:e4b", "frames_model": "qwen3-vl:4b-instruct",
              "summary_model": "cloud:gaia", "after": True}
    job = c.post("/jobs", json={"path": str(rec), "models": models}).json()
    assert job["models"] == models
    for _ in range(100):
        if c.get(f"/jobs/{job['id']}").json()["status"] == "done":
            break
        import time
        time.sleep(0.02)
    assert seen == [models]


def test_estimate_uses_the_chosen_vision_models_speed():
    from transcriber import screen_names as sn
    base = sn.estimate_seconds(600, vision_cap=10)
    slow = sn.estimate_seconds(600, vision_cap=10, vision_seconds=15.5)
    assert abs((slow["high"] - base["high"]) - 10 * (15.5 - sn.SECONDS_PER_VISION_FRAME)) <= 1


def test_whisper_waits_until_ollama_has_freed_its_model(monkeypatch):
    """A reply still being written keeps its model loaded: the job waits (and says so)."""
    from transcriber import pipeline

    loaded = [["gemma4:e4b"], ["gemma4:e4b"], []]
    calls = {"unload": 0}

    class Resp:
        def __init__(self, models):
            self.models = models

        def json(self):
            return {"models": [{"name": n} for n in self.models]}

    import requests
    monkeypatch.setattr(requests, "get", lambda url, timeout=3: Resp(loaded.pop(0) if len(loaded) > 1 else loaded[0]))
    monkeypatch.setattr(pipeline, "unload_ollama_models", lambda: calls.__setitem__("unload", calls["unload"] + 1) or [])
    stages = []
    assert pipeline.wait_for_ollama_idle(stages.append, poll=0)
    assert calls["unload"] == 3 and stages[0] == {"stage": "freeing_memory", "waiting_for": ["gemma4:e4b"]}

    loaded[:] = [["gemma4:e4b"]]
    assert pipeline.wait_for_ollama_idle(None, timeout=0, poll=0) is False  # gives up, the job goes ahead
