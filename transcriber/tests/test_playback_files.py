"""Playing back meetings transcribed from uploads or the watch folder: they keep playing after the watch folder
changes, and a recording that moved (its upload copy deleted, the original still on the Desktop) is found again by
name and length and linked with one click. Synthetic files only (ffmpeg tones in tmp_path).

    transcriber/.venv/bin/python -m pytest transcriber/tests/test_playback_files.py -q
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

TID = "0123456789abcdef"


def tone(path: Path, seconds: float, video: bool = False) -> Path:
    """A short synthetic recording (a sine tone, with a test picture when `video`)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    args = ["ffmpeg", "-nostdin", "-y", "-loglevel", "error", "-f", "lavfi", "-i",
            f"sine=frequency=440:duration={seconds}"]
    if video:
        args += ["-f", "lavfi", "-i", f"testsrc=size=160x90:rate=5:duration={seconds}", "-c:v", "libx264",
                 "-pix_fmt", "yuv420p"]
    subprocess.run([*args, "-c:a", "aac", "-shortest", str(path)], check=True)
    return path


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("HF_HUB_OFFLINE", "1")
    from transcriber import config, pipeline, server
    home = tmp_path / "home"
    monkeypatch.setattr(config, "MEETINGS_DIR", tmp_path / "meetings")
    monkeypatch.setattr(config, "DEFAULT_MEETINGS_DIR", tmp_path / "meetings")
    monkeypatch.setattr(config, "UPLOADS_DIR", tmp_path / "meetings" / "uploads")
    monkeypatch.setattr(config, "TRANSCRIPTS_DIR", tmp_path / "transcripts")
    monkeypatch.setattr(config, "WORK_DIR", tmp_path / "transcripts" / ".work")
    monkeypatch.setattr(server, "whisper_cached", lambda: True)
    monkeypatch.setattr(server, "_home_folders", lambda: [home / n for n in server.HOME_FOLDERS])
    monkeypatch.setattr(pipeline, "diarization_available", lambda: False)
    with TestClient(server.app) as c:
        yield c, config, pipeline, home


def save(pipeline, path: Path, duration: float, name: str | None = None, tid: str = TID) -> None:
    pipeline.save_transcript({
        "id": tid, "title": "Sync", "source_name": name or path.name, "source_path": str(path),
        "created_at": "2026-11-20T10:00:00", "duration": duration, "diarization": False, "speakers": ["SPEAKER_00"],
        "speaker_names": {}, "segments": [{"speaker": "SPEAKER_00", "start": 0, "end": duration, "text": "Hello."}]})


def test_a_watch_folder_recording_still_plays_after_the_watch_folder_changes(client, tmp_path):
    c, config, pipeline, _home = client
    src = tone(config.MEETINGS_DIR / "standup.m4a", 3)
    save(pipeline, src, 3)
    assert c.post("/settings", json={"meetings_dir": str(tmp_path / "elsewhere"), "create": True}).status_code == 200
    info = c.get(f"/transcripts/{TID}/media").json()
    assert info["available"] is True and info["parts"] == [{"part": "audio", "label": "Sound"}]
    assert c.get(f"/transcripts/{TID}/media/audio", headers={"Range": "bytes=0-9"}).status_code == 206
    # A path the browser sends is still limited to the meetings folders.
    assert c.post("/estimate", json={"path": str(src)}).status_code == 400


def test_a_moved_recording_is_found_by_name_and_length_and_linked(client):
    c, config, pipeline, home = client
    name = "Screen Recording 2026-11-19 at 5.30.55 PM.mov"  # macOS puts a narrow space before PM
    save(pipeline, config.UPLOADS_DIR / name, 4)                   # the upload copy was deleted
    original = tone(home / "Desktop" / name.replace(" ", " "), 4, video=True)
    tone(home / "Downloads" / "old" / name, 9)                    # same name, another recording
    tone(home / "Documents" / "a" / "b" / name, 4)               # too deep: not searched
    assert c.get(f"/transcripts/{TID}/media").json()["reason"] == "missing"

    found = c.get(f"/transcripts/{TID}/recording/candidates").json()
    assert [(f["folder"], f["match"]) for f in found["candidates"]] == [("Desktop", True), ("Downloads", False)]
    assert found["candidates"][0]["path"] == str(original.resolve())

    wrong = c.post(f"/transcripts/{TID}/recording", json={"path": found["candidates"][1]["path"]})
    assert wrong.status_code == 409 and "0:09 long; the meeting is 0:04" in wrong.json()["detail"]
    linked = c.post(f"/transcripts/{TID}/recording", json={"path": str(original)}).json()
    assert linked["available"] is True and linked["video"] is True and linked["copied"] is True
    copy = Path(linked["linked"])
    assert copy.parent == config.UPLOADS_DIR.resolve() and copy.stat().st_size == original.stat().st_size
    original.unlink()  # the clone survives the original
    assert c.get(f"/transcripts/{TID}/media/video", headers={"Range": "bytes=0-9"}).status_code == 206
    # The copy shows as this meeting's recording, not as a new one to transcribe.
    listed = {r["name"]: r for r in c.get("/recordings").json()}
    assert listed[copy.name]["transcript_id"] == TID


def test_only_found_files_or_uploads_can_be_linked(client, tmp_path):
    c, config, pipeline, home = client
    save(pipeline, config.UPLOADS_DIR / "call.m4a", 3)
    stray = tone(tmp_path / "secret" / "call.m4a", 3)  # same name, outside the searched folders
    assert c.post(f"/transcripts/{TID}/recording", json={"path": str(stray)}).status_code == 400
    assert c.post(f"/transcripts/{TID}/recording", json={"path": "call.m4a"}).status_code == 400
    uploaded = tone(config.UPLOADS_DIR / "call 2.m4a", 3)  # the upload fallback
    linked = c.post(f"/transcripts/{TID}/recording", json={"path": str(uploaded)}).json()
    assert linked["available"] is True and linked["copied"] is False and linked["linked"] == str(uploaded.resolve())
    short = tone(config.UPLOADS_DIR / "short.m4a", 0.5)  # 2.5 s shorter: beyond the 2 s tolerance
    assert c.post(f"/transcripts/{TID}/recording", json={"path": str(short)}).status_code == 409
    pipeline.save_transcript({"id": "fedcba9876543210", "title": "Text", "source_type": "text",
                              "created_at": "2026-11-20", "segments": []})
    assert c.get("/transcripts/fedcba9876543210/recording/candidates").json()["candidates"] == []
    assert c.post("/transcripts/fedcba9876543210/recording", json={"path": str(uploaded)}).status_code == 400


def test_relinking_clears_the_prepared_sound(client):
    c, config, pipeline, home = client
    save(pipeline, config.UPLOADS_DIR / "gone.m4a", 3)
    cache = config.TRANSCRIPTS_DIR / ".media" / TID
    cache.mkdir(parents=True)
    (cache / "mix.m4a").write_bytes(b"old")
    tone(home / "Movies" / "gone.m4a", 3)
    c.post(f"/transcripts/{TID}/recording", json={"path": str(home / "Movies" / "gone.m4a")})
    assert not cache.exists()
