"""Local transcriber HTTP service (bind to 127.0.0.1 only).

Run via scripts/start_local_ai.sh. Strictly offline: refuses to start unless the
Whisper model is already cached; speaker labels are reported unavailable (not
downloaded) if pyannote's model isn't cached.

Endpoints
  GET  /health                       status, models, offline flag
  GET  /settings                     watch folder (current, default) and uploads folder
  POST /settings      {meetings_dir} change the watch folder (saved; "" = default)
  GET  /recordings                   media files in the watch folder + uploads
  POST /estimate      {path}         duration, time range, disk check
  POST /jobs          {path, speakers?, screen?}  queue a transcription -> {id};
                                     speakers = {num} or {min, max}, default auto-detect;
                                     screen = {enabled, vision_model, vision_cap, debug}
  POST /transcripts/{id}/screen-names {screen?}  queue on-screen name reading for an older transcript
  POST /transcripts/{id}/voiceprints  queue voice fingerprints for an older transcript (backfill)
  GET  /jobs[/{id}]                  status, stage, progress, elapsed, eta
  GET  /transcripts[/{id}]           saved transcripts
  GET  /transcripts/{id}/markdown    transcript as Markdown
  POST /transcripts/{id}/speakers    {names: {SPEAKER_00: "Priya"}}
  POST /transcripts/import {title, text, source_name}  transcript from conversation text
  DELETE /transcripts/{id}[?recording=true]  delete a transcript (and its uploaded recording)
  PUT  /transcripts/{id}/segments    {segments, speaker_names, speaker_source}  speakers guessed
                                     in the app by the local model (original kept for undo)
  POST /transcripts/{id}/undo-speakers  restore the transcript as it was before the guess
  GET  /transcripts/{id}/media[/{part}]  play the meeting back (picture, call/mic/mixed sound; Range)
  GET  /transcripts/{id}/recording/candidates  a moved recording, found again by name and length
  POST /transcripts/{id}/recording   {path}  play the meeting from that copy (cloned into uploads)
  GET  /upload                       upload page (embedded in the app)
  POST /uploads/init  {name, size}   start or resume a chunked upload
  PUT  /uploads/{uid}/chunk/{n}      one 8 MB chunk, streamed to disk
  POST /uploads/{uid}/complete       assemble -> recording path
"""

from __future__ import annotations

import json
import os
import queue
import re
import shutil
import subprocess
import threading
import time
import uuid
from datetime import datetime
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, PlainTextResponse
from pydantic import BaseModel, Field, model_validator

from transcriber import config, correct, edits, live, pipeline, recorder, screen_names, text_import

JOBS: dict[str, dict] = {}
JOB_QUEUE: "queue.Queue[str]" = queue.Queue()
_LOCK = threading.Lock()
_WORKER: threading.Thread | None = None


# ---------------------------------------------------------------------------
# Startup: offline guarantees
# ---------------------------------------------------------------------------

def whisper_cached() -> bool:
    """True when the Whisper model is already downloaded (checked without the internet)."""
    try:
        from huggingface_hub import snapshot_download
        snapshot_download(config.WHISPER_MODEL, local_files_only=True)
        return True
    except Exception:
        return False


def _startup() -> None:
    """Safety checks before the service accepts work: offline mode must be on and Whisper must be cached; then
    the saved watch folder is loaded and the single worker thread starts.
    """
    if os.environ.get("HF_HUB_OFFLINE") != "1":
        raise RuntimeError("Refusing to start: HF_HUB_OFFLINE=1 is required (offline mode).")
    if not whisper_cached():
        raise RuntimeError(
            f"Refusing to start: {config.WHISPER_MODEL} is not downloaded. "
            "Run scripts/setup_local_ai.sh once (needs internet)."
        )
    for d in (config.UPLOADS_DIR, config.TRANSCRIPTS_DIR, config.WORK_DIR):
        d.mkdir(parents=True, exist_ok=True)
    load_settings()
    config.MEETINGS_DIR.mkdir(parents=True, exist_ok=True)
    app.state.diarization = pipeline.diarization_available()
    resume_recording()
    global _WORKER
    if _WORKER is None or not _WORKER.is_alive():  # exactly one job at a time, even if restarted
        _WORKER = threading.Thread(target=_worker, daemon=True)
        _WORKER.start()


@asynccontextmanager
async def _lifespan(_app):
    """FastAPI start-up hook: runs _startup() once when the service starts."""
    _startup()
    yield


app = FastAPI(title="Local meeting transcriber", lifespan=_lifespan)


# ---------------------------------------------------------------------------
# Path safety
# ---------------------------------------------------------------------------

def media_roots() -> list[Path]:
    """Folders recordings may come from: the watch folder and the uploads folder."""
    roots = []
    for root in (config.MEETINGS_DIR, config.UPLOADS_DIR):
        root = root.resolve()
        if root not in roots:
            roots.append(root)
    return roots


def safe_media_path(raw: str) -> Path:
    """Resolve a path and require it to be a media file inside the watch or uploads folder."""
    base = config.MEETINGS_DIR.resolve()
    path = (base / raw).resolve() if not os.path.isabs(raw) else Path(raw).resolve()
    if not any(path == root or root in path.parents for root in media_roots()):
        raise HTTPException(400, "Path is outside the meetings folder.")
    if not path.is_file():
        raise HTTPException(404, "Recording not found.")
    if path.suffix.lower() not in config.MEDIA_SUFFIXES:
        raise HTTPException(400, f"Unsupported file type: {path.suffix}")
    return path


# ---------------------------------------------------------------------------
# Health / listings
# ---------------------------------------------------------------------------

@app.get("/health")
def health() -> dict:
    """GET /health: whether the service is up and offline, its models, whether speaker labels and on-screen names
    are available, and how many jobs are waiting.
    """
    busy = [j for j in JOBS.values() if j["status"] in ("queued", "running")]
    return {
        "ok": True,
        "offline": os.environ.get("HF_HUB_OFFLINE") == "1",
        "whisper_model": config.WHISPER_MODEL,
        "diarization_model": config.DIARIZATION_MODEL,
        "diarization": bool(getattr(app.state, "diarization", False)),
        "screen_names": bool(shutil.which("swiftc") or screen_names.OCR_BINARY.exists()),
        "meetings_dir": str(config.MEETINGS_DIR),
        "uploads_dir": str(config.UPLOADS_DIR),
        "busy": len(busy),
        "recording": recorder.busy(),  # a call is being recorded: heavy work (Ollama too) waits
        "recorder": recorder.available(),
    }


def _transcript_index() -> dict[str, dict]:
    """A short summary of every saved transcript (no segments), by id; broken files are skipped."""
    index = {}
    for path in config.TRANSCRIPTS_DIR.glob("*.json"):
        if path.name == "speed.json":
            continue
        try:
            data = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        if not isinstance(data, dict) or "id" not in data or "segments" not in data:
            continue  # speed.json, settings.json, ...
        index[data["id"]] = {k: data.get(k) for k in
                             ("id", "title", "source_name", "source_path", "created_at", "duration",
                              "language", "diarization", "speakers", "speaker_names",
                              "processing_seconds", "source_type", "timestamps",
                              "diarization_error", "speaker_source", "recording_id")}
        index[data["id"]]["can_undo_speakers"] = "before_speaker_guess" in data
        index[data["id"]]["corrected"] = "before_correction" in data
        index[data["id"]]["uploaded_recording"] = _uploaded_recording(data) is not None
    return index


def _uploaded_recording(transcript: dict) -> Path | None:
    """The transcript's recording if it is an upload (watch-folder files are never deleted)."""
    raw = transcript.get("source_path")
    if not raw:
        return None
    path = Path(raw).resolve()
    return path if config.UPLOADS_DIR.resolve() in path.parents and path.is_file() else None


def _media_files(root: Path, depth: int = 0):
    """Media files in a folder and its subfolders (2 levels), skipping hidden files and unfinished uploads."""
    try:
        entries = list(os.scandir(root))
    except OSError:
        return
    for entry in entries:
        if entry.name.startswith(".") or entry.name == ".parts":
            continue
        try:
            if entry.is_dir(follow_symlinks=False):
                if depth < config.MAX_SCAN_DEPTH:
                    yield from _media_files(Path(entry.path), depth + 1)
            elif Path(entry.name).suffix.lower() in config.MEDIA_SUFFIXES:
                yield Path(entry.path)
        except OSError:
            continue


@app.get("/recordings")
def recordings() -> list[dict]:
    """GET /recordings: recordings in the watch and uploads folders, newest first, with the transcript id when
    one was already made.
    """
    index = _transcript_index()
    uploads = config.UPLOADS_DIR.resolve()
    seen: dict[Path, None] = {}
    for root in media_roots():
        for path in _media_files(root):
            seen.setdefault(path.resolve())
    found = []
    for path in seen:
        try:
            stat = path.stat()
        except OSError:
            continue
        found.append((stat.st_mtime, path, stat))
    found.sort(key=lambda item: item[0], reverse=True)
    by_path = {}  # a recording linked to a meeting later (relinked copy) has another source_id
    for tid, t in index.items():
        if t.get("source_path") and os.path.isabs(t["source_path"]):
            by_path.setdefault(str(Path(t["source_path"]).resolve()), tid)
    items = []
    for mtime, path, stat in found[: config.MAX_RECORDINGS]:
        sid = pipeline.source_id(path)
        items.append({
            "path": str(path),
            "name": path.name,
            "source": "upload" if uploads in path.parents else "folder",
            "size": stat.st_size,
            "modified": time.strftime("%Y-%m-%d %H:%M", time.localtime(mtime)),
            "transcript_id": sid if sid in index else by_path.get(str(path)),
        })
    return items


# ---------------------------------------------------------------------------
# Settings: the watch folder can be changed from the app
# ---------------------------------------------------------------------------

def _settings_file() -> Path:
    """Where the chosen watch folder is saved."""
    return config.TRANSCRIPTS_DIR / "settings.json"


def check_folder(raw: str, create: bool = False) -> Path:
    """Validate a folder the user wants to watch; raises HTTPException with a clear reason."""
    path = Path(os.path.expandvars(raw.strip())).expanduser()
    if not path.is_absolute():
        raise HTTPException(400, "Enter a full folder path, e.g. /Users/you/Meetings or ~/Meetings.")
    path = path.resolve()
    if create and not path.exists():
        try:
            path.mkdir(parents=True)
        except OSError as exc:
            raise HTTPException(400, f"Could not create {path}: {exc.strerror or exc}")
    if not path.exists():
        raise HTTPException(404, f"Folder not found: {path}")
    if not path.is_dir():
        raise HTTPException(400, f"Not a folder: {path}")
    try:
        next(os.scandir(path), None)
    except PermissionError:
        raise HTTPException(403, f"macOS blocked access to {path}. Allow it in System Settings → "
                                 "Privacy & Security → Files and Folders (or Full Disk Access) for "
                                 "the Terminal app that runs the transcriber, then restart it.")
    return path


def load_settings() -> None:
    """Restores the watch folder chosen in the app (falls back to the default if it no longer exists)."""
    config.MEETINGS_DIR = config.DEFAULT_MEETINGS_DIR
    try:
        saved = json.loads(_settings_file().read_text()).get("meetings_dir")
    except (OSError, ValueError):
        return
    if saved:
        try:
            config.MEETINGS_DIR = check_folder(saved)
        except HTTPException:
            pass  # folder gone or blocked: fall back to the default


class SettingsBody(BaseModel):
    """POST /settings body: the new watch folder, and whether to create it."""
    meetings_dir: str
    create: bool = False


@app.get("/settings")
def get_settings() -> dict:
    """GET /settings: the current, default and uploads folders."""
    return {"meetings_dir": str(config.MEETINGS_DIR),
            "default_meetings_dir": str(config.DEFAULT_MEETINGS_DIR),
            "uploads_dir": str(config.UPLOADS_DIR)}


@app.post("/settings")
def set_settings(body: SettingsBody) -> dict:
    """POST /settings: changes the watch folder ('' = back to the default) and saves it."""
    if body.meetings_dir.strip():
        folder = check_folder(body.meetings_dir, body.create)
    else:
        folder = config.DEFAULT_MEETINGS_DIR
        folder.mkdir(parents=True, exist_ok=True)
    config.MEETINGS_DIR = folder
    saved = "" if folder == config.DEFAULT_MEETINGS_DIR.resolve() else str(folder)
    write_setting("meetings_dir", saved)
    return get_settings()


def read_settings() -> dict:
    """Everything in settings.json ({} when missing or broken)."""
    try:
        data = json.loads(_settings_file().read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def write_setting(key: str, value) -> None:
    """Saves one setting, keeping the others."""
    data = read_settings()
    data[key] = value
    _settings_file().parent.mkdir(parents=True, exist_ok=True)
    _settings_file().write_text(json.dumps(data))


class PathBody(BaseModel):
    """A request body with the path of a recording."""
    path: str


def _probe(path: Path) -> dict:
    """Duration and audio/video tracks of a recording; a readable 400 error when ffprobe can't read it."""
    try:
        return pipeline.probe_media(path)
    except Exception as exc:
        raise HTTPException(400, f"Could not read this recording: {exc}")


class EstimateBody(PathBody):
    """POST /estimate body: the recording, plus the vision-model frames to include in the time."""
    vision_cap: int = Field(0, ge=0, le=100)  # frames the vision model may read (adds to the time)
    vision_seconds: float | None = Field(None, gt=0, le=120)  # that model's time per frame


@app.post("/estimate")
def estimate(body: EstimateBody) -> dict:
    """POST /estimate: how long transcription will take (from this Mac's measured speed), the disk space needed,
    and whether the recording has sound and video.
    """
    path = safe_media_path(body.path)
    media = _probe(path)
    duration = media["duration"]
    factor = pipeline.load_speed_factor(app.state.diarization)
    rng = pipeline.estimate_seconds(duration, factor)
    need = pipeline.wav_bytes_for(duration)
    free = pipeline.free_disk_bytes(config.WORK_DIR)
    screen = (screen_names.estimate_seconds(duration, body.vision_cap, body.vision_seconds)
              if media.get("video_tracks") else None)
    return {
        "has_video": bool(media.get("video_tracks")),
        "screen_estimate": screen,
        "path": body.path,
        "duration": duration,
        "audio_tracks": media["audio_tracks"],
        "has_audio": media["audio_tracks"] > 0,
        "no_audio_message": "" if media["audio_tracks"] else pipeline.NO_AUDIO_MESSAGE,
        "estimate_low": rng["low"],
        "estimate_high": rng["high"],
        "speed_factor": factor,
        "temp_disk_bytes": need,
        "free_disk_bytes": free,
        "disk_ok": free - need > config.DISK_SAFETY_MARGIN_BYTES,
        "diarization": app.state.diarization,
        "queued_ahead": sum(1 for j in JOBS.values() if j["status"] in ("queued", "running")),
    }


# ---------------------------------------------------------------------------
# Jobs (one at a time: the laptop has one GPU and 16 GB)
# ---------------------------------------------------------------------------

class SpeakerHint(BaseModel):
    """Optional speaker count: exact (`num`) or a range (`min`/`max`); empty = auto-detect."""
    num: int | None = Field(None, ge=1, le=config.MAX_SPEAKERS)
    min: int | None = Field(None, ge=1, le=config.MAX_SPEAKERS)
    max: int | None = Field(None, ge=1, le=config.MAX_SPEAKERS)

    @model_validator(mode="after")
    def _check(self):
        """Rejects contradictory hints (an exact number and a range, or min > max)."""
        if self.num and (self.min or self.max):
            raise ValueError("Give either an exact number of speakers or a range, not both.")
        if self.min and self.max and self.min > self.max:
            raise ValueError("Minimum speakers is larger than maximum.")
        return self


class ScreenOptions(BaseModel):
    """On-screen names (video only). vision_model: the local Ollama model that reviews frames of every speaker
    ("" = none installed); vision_cap: frames it reviews per meeting (None or 0 = 4 per speaker, 12 to 48)."""
    enabled: bool = True
    vision_model: str = Field("", max_length=200)
    vision_cap: int | None = Field(None, ge=0, le=200)
    debug: bool = False
    participants: bool = True   # scan the whole recording for the call's participants list


class MeetingModels(BaseModel):
    """The meeting's model choices, kept with the job and the transcript ("" = the app's default)."""
    speaker_model: str = Field("", max_length=200)
    frames_model: str = Field("", max_length=200)
    summary_model: str = Field("", max_length=300)
    after: bool = False  # identify speakers and write the summary when the transcript is ready


class JobBody(PathBody):
    """POST /jobs body: the recording, an optional speaker count, on-screen name options and the meeting's model
    choices.
    """
    speakers: SpeakerHint | None = None
    screen: ScreenOptions | None = None
    models: MeetingModels | None = None


@app.post("/jobs")
def create_job(body: JobBody) -> dict:
    """POST /jobs: queues a transcription (refused without sound or disk space). Starting the same recording
    twice returns the job already queued.
    """
    path = safe_media_path(body.path)
    return queue_transcription(path, body.speakers.model_dump(exclude_none=True) if body.speakers else None,
                               body.screen.model_dump() if body.screen else None,
                               body.models.model_dump() if body.models else None)


def queue_transcription(path: Path, speakers: dict | None, screen: dict | None, models: dict | None,
                        recording_id: str | None = None) -> dict:
    """Queues a transcription of `path` (refused without sound or disk space). The same recording already queued
    or running returns that job. `recording_id`: a call recorded by Quorum (its live draft is cleaned up after)."""
    media = _probe(path)
    if not media["audio_tracks"]:
        raise HTTPException(422, pipeline.NO_AUDIO_MESSAGE)
    duration = media["duration"]
    two_tracks = bool(pipeline.track_roles(path, media))
    need = pipeline.wav_bytes_for(duration) * (2 if two_tracks else 1)
    if pipeline.free_disk_bytes(config.WORK_DIR) - need < config.DISK_SAFETY_MARGIN_BYTES:
        raise HTTPException(507, "Not enough free disk space for this recording.")
    with _LOCK:
        for job in JOBS.values():
            if job["path"] == str(path) and job["status"] in ("queued", "running"):
                return job
        job_id = uuid.uuid4().hex[:12]
        factor = pipeline.load_speed_factor(app.state.diarization)
        rng = pipeline.estimate_seconds(duration * (pipeline.CALL_TRACKS_COST if two_tracks else 1.0), factor)
        JOBS[job_id] = {
            "id": job_id, "path": str(path), "name": path.name, "status": "queued",
            "stage": "queued", "progress": 0.0, "duration": duration,
            "created": time.time(), "started": None, "finished": None,
            "estimate_low": rng["low"], "estimate_high": rng["high"],
            "error": None, "transcript_id": None,
            "speakers": speakers, "screen": screen, "kind": "transcribe", "models": models,
            "recording_id": recording_id,
        }
    JOB_QUEUE.put(job_id)
    return JOBS[job_id]


def _queue_transcript_job(tid: str, kind: str, screen: dict | None = None) -> dict:
    """A follow-up job on a finished transcript (on-screen names, voice fingerprints)."""
    transcript = _load_transcript(tid)
    src = pipeline.recording_for(transcript)
    if src is None:
        raise HTTPException(404, "The recording for this transcript is no longer available.")
    with _LOCK:
        for job in JOBS.values():
            if job.get("transcript_id") == tid and job.get("kind") == kind and job["status"] in ("queued", "running"):
                return job
        job_id = uuid.uuid4().hex[:12]
        duration = float(transcript.get("duration") or 0)
        frames = screen_names.review_budget(len(transcript.get("speakers") or []), (screen or {}).get("vision_cap"))
        cost = (screen_names.estimate_seconds(duration, frames if (screen or {}).get("vision_model") else 0)["high"]
                if kind == "screen" else duration * 0.02 + 20)
        JOBS[job_id] = {
            "id": job_id, "path": str(src), "name": src.name, "status": "queued", "stage": "queued",
            "progress": 0.0, "duration": duration, "created": time.time(), "started": None, "finished": None,
            "estimate_low": round(cost * 0.5), "estimate_high": round(cost), "error": None,
            "transcript_id": tid, "speakers": None, "screen": screen, "kind": kind,
        }
    JOB_QUEUE.put(job_id)
    return JOBS[job_id]


class ScreenJobBody(BaseModel):
    """Body for re-reading names from a finished transcript's video."""
    screen: ScreenOptions | None = None


@app.post("/transcripts/{tid}/screen-names")
def screen_names_job(tid: str, body: ScreenJobBody) -> dict:
    """POST /transcripts/{id}/screen-names: queues reading names from the video of a transcript made earlier."""
    return _queue_transcript_job(tid, "screen", (body.screen or ScreenOptions()).model_dump())


@app.post("/transcripts/{tid}/voiceprints")
def voiceprints_job(tid: str) -> dict:
    """POST /transcripts/{id}/voiceprints: queues making voice fingerprints for an older transcript (needs the
    speaker models).
    """
    if not app.state.diarization:
        raise HTTPException(409, "The speaker models aren't downloaded, so voice fingerprints can't be made.")
    return _queue_transcript_job(tid, "voiceprints")


def _public(job: dict) -> dict:
    """A job as returned to the app, with elapsed time and, while running, the time left."""
    out = dict(job)
    if job["started"]:
        end = job["finished"] or time.time()
        out["elapsed"] = round(end - job["started"])
        if job["status"] == "running":
            out["eta"] = eta_seconds(out["elapsed"], job["progress"],
                                     (job["estimate_low"] + job["estimate_high"]) / 2)
    return out


def eta_seconds(elapsed: float, progress: float, estimate: float) -> int:
    """Time left: trust the up-front estimate early, the measured rate later."""
    by_estimate = max(0.0, estimate - elapsed)
    if progress < 0.1:
        return round(by_estimate)
    by_rate = elapsed / progress * (1 - progress)
    weight = min(1.0, (progress - 0.1) / 0.3)  # fully rate-based from 40%
    return round(by_estimate * (1 - weight) + by_rate * weight)


@app.get("/jobs")
def list_jobs() -> list[dict]:
    """GET /jobs: all jobs since the service started, newest first."""
    return [_public(j) for j in sorted(JOBS.values(), key=lambda j: j["created"], reverse=True)]


@app.get("/jobs/{job_id}")
def get_job(job_id: str) -> dict:
    """GET /jobs/{id}: one job's status and progress."""
    if job_id not in JOBS:
        raise HTTPException(404, "Unknown job.")
    return _public(JOBS[job_id])


@app.post("/jobs/{job_id}/cancel")
def cancel_job(job_id: str) -> dict:
    """Queued jobs are dropped; a running job stops at its next step (parts done are kept)."""
    with _LOCK:
        job = JOBS.get(job_id)
        if not job:
            raise HTTPException(404, "Unknown job.")
        if job["status"] == "queued":
            job.update(status="cancelled", stage="cancelled", finished=time.time())
        elif job["status"] == "running":
            job["cancel_requested"] = True
    return _public(job)


@app.get("/jobs/{job_id}/partial")
def job_partial(job_id: str) -> dict:
    """Transcript text produced so far by a running job (speakers come at the end)."""
    job = JOBS.get(job_id)
    if not job:
        raise HTTPException(404, "Unknown job.")
    if job["status"] != "running":
        return {"segments": [], "windows": 0, "status": job["status"]}
    return dict(pipeline.partial_segments(Path(job["path"])), status=job["status"])


def _worker() -> None:
    """The single worker thread: runs one job at a time from the queue (one GPU, 16 GB), records
    done/cancelled/error, and keeps serving after a failure.
    """
    while True:
        job_id = JOB_QUEUE.get()
        job = JOBS[job_id]
        if job["status"] == "cancelled":
            continue
        while recorder.busy() or live.active():  # never compete with a call being recorded or its draft
            if job.get("cancel_requested"):
                break
            job.update(stage="waiting_for_call")
            time.sleep(2)
        if job.get("cancel_requested"):
            job.update(status="cancelled", stage="cancelled", finished=time.time())
            continue
        job.update(status="running", started=time.time(), stage="starting")

        def progress(update: dict, job=job) -> None:
            """Called by the pipeline with progress updates; raises JobCancelled once the user pressed Cancel,
            which stops the job at the next step.
            """
            if job.get("cancel_requested"):
                raise pipeline.JobCancelled()
            job.update(update)

        try:
            kind = job.get("kind", "transcribe")
            if kind == "reference":  # another recording of the meeting (a Voice Memo): words, then correct
                words = pipeline.run_reference(job["transcript_id"], Path(job["path"]), progress)
                ref = load_ref(job["transcript_id"], job["ref_id"])
                if ref is None:  # removed while it was being transcribed
                    raise FileNotFoundError("This recording was removed from the meeting.")
                ref.update(status="ready", words=words, error=None)
                save_ref(job["transcript_id"], ref)
                apply_corrections(job["transcript_id"], job.get("known_terms"), reapply=True)
                transcript = {"id": job["transcript_id"]}
            elif kind == "screen":
                transcript = pipeline.run_screen_only(job["transcript_id"], progress, job.get("screen"))
            elif kind == "voiceprints":
                transcript = pipeline.backfill_voiceprints(job["transcript_id"], progress)
            else:
                transcript = pipeline.run(Path(job["path"]), progress, job.get("speakers"), job.get("screen"),
                                          job.get("models"))
                if job.get("recording_id"):  # a call recorded by Quorum: the app swaps its live draft for this
                    transcript["recording_id"] = job["recording_id"]
                    pipeline.save_transcript(transcript)
                    recorder.cleanup(job["recording_id"])
                    live.DRAFTS.pop(job["recording_id"], None)
            job.update(status="done", progress=1.0, stage="done", transcript_id=transcript["id"])
        except pipeline.JobCancelled:
            job.update(status="cancelled", stage="cancelled")
        except Exception as exc:  # report, keep serving
            job.update(status="error", error=str(exc) if isinstance(exc, (pipeline.NoAudioError, FileNotFoundError))
                       else f"{type(exc).__name__}: {exc}")
            if job.get("kind") == "reference" and (ref := load_ref(job["transcript_id"], job["ref_id"])):
                ref.update(status="failed", error=job["error"])
                save_ref(job["transcript_id"], ref)
        finally:
            job["finished"] = time.time()


# ---------------------------------------------------------------------------
# Transcripts
# ---------------------------------------------------------------------------

def _load_transcript(tid: str) -> dict:
    """Reads one transcript; rejects ids that aren't 16 hex characters (no path tricks)."""
    if not re.fullmatch(r"[0-9a-f]{16}", tid):
        raise HTTPException(400, "Bad transcript id.")
    path = config.TRANSCRIPTS_DIR / f"{tid}.json"
    if not path.exists():
        raise HTTPException(404, "Transcript not found.")
    return json.loads(path.read_text())


@app.get("/transcripts")
def list_transcripts() -> list[dict]:
    """GET /transcripts: summaries of all transcripts, newest first."""
    return sorted(_transcript_index().values(), key=lambda t: t["created_at"] or "", reverse=True)


@app.get("/transcripts/{tid}")
def get_transcript(tid: str) -> dict:
    """GET /transcripts/{id}: the full transcript."""
    return _load_transcript(tid)


@app.get("/transcripts/{tid}/markdown", response_class=PlainTextResponse)
def get_markdown(tid: str) -> str:
    """GET /transcripts/{id}/markdown: the transcript as Markdown."""
    return pipeline.to_markdown(_load_transcript(tid))


# ---------------------------------------------------------------------------
# Playing a meeting back
# ---------------------------------------------------------------------------

MEDIA_PARTS = {"video", "audio", "call", "mic", "mix"}
VIDEO_TYPES = {".mp4": "video/mp4", ".m4v": "video/mp4", ".mov": "video/mp4", ".webm": "video/webm",
               ".mkv": "video/x-matroska"}
AUDIO_TYPES = {".m4a": "audio/mp4", ".mp3": "audio/mpeg", ".wav": "audio/wav", ".aac": "audio/aac",
               ".flac": "audio/flac", ".ogg": "audio/ogg", ".mp4": "audio/mp4", ".webm": "audio/webm"}
_MEDIA_LOCKS: dict[str, threading.Lock] = {}
_MEDIA_LOCKS_GUARD = threading.Lock()


def media_cache(tid: str) -> Path:
    """Sound prepared for playback (a call recording's tracks, or a mix), per transcript; deleted with it."""
    return config.TRANSCRIPTS_DIR / ".media" / tid


def _recording_of(transcript: dict) -> Path | None:
    """The transcript's recording when the file still exists (else None). The saved `source_path` was written by
    this service from a path it checked when it transcribed (or relinked) the file, so it stays playable after the
    watch folder changes; paths from the browser still go through safe_media_path."""
    raw = transcript.get("source_path")
    if not raw or transcript.get("source_type") == "text":
        return None
    if not os.path.isabs(raw):  # very old transcripts: relative to the watch folder
        try:
            return safe_media_path(raw)
        except HTTPException:
            return None
    path = Path(raw)
    return path if path.suffix.lower() in config.MEDIA_SUFFIXES and path.is_file() else None


def media_layout(src: Path) -> dict:
    """What the player gets: `video` (the file has a picture), and `parts`, the sound to play next to it: a call
    recording's call and microphone tracks (separate volumes), a mix of several tracks, or the file's own sound."""
    media = pipeline.probe_media(src)
    roles = pipeline.track_roles(src, media) if media["audio_tracks"] > 1 else {}
    video = media["video_tracks"] > 0
    if {"system", "mic"} <= set(roles):
        parts = [{"part": "call", "label": "Call"}, {"part": "mic", "label": "You"}]
    elif media["audio_tracks"] > 1:
        parts = [{"part": "mix", "label": "Sound"}]
    elif media["audio_tracks"] == 1 and not video:
        parts = [{"part": "audio", "label": "Sound"}]
    else:
        parts = []  # a video with one sound track plays its own sound
    return {"video": video, "parts": parts, "duration": media["duration"], "roles": roles,
            "audio_tracks": media["audio_tracks"]}


def prepare_part(tid: str, src: Path, part: str, layout: dict) -> Path:
    """The .m4a for `part` (call, mic or mix), made once with ffmpeg and cached: a call track is copied as is
    (AAC), a mix is encoded."""
    with _MEDIA_LOCKS_GUARD:
        lock = _MEDIA_LOCKS.setdefault(tid, threading.Lock())
    with lock:
        folder = media_cache(tid)
        out = folder / f"{part}.m4a"
        if out.exists() and out.stat().st_mtime >= src.stat().st_mtime:
            return out
        folder.mkdir(parents=True, exist_ok=True)
        tmp = folder / f"{part}.partial.m4a"
        if part == "mix":
            sound = [*pipeline.audio_map_args(layout["audio_tracks"]), "-c:a", "aac", "-b:a", "128k"]
        else:
            track = layout["roles"]["system" if part == "call" else "mic"]
            sound = ["-map", f"0:a:{track}", "-c:a", "aac", "-b:a", "96k"]
            codec = subprocess.run(["ffprobe", "-v", "error", "-select_streams", f"a:{track}", "-show_entries",
                                    "stream=codec_name", "-of", "csv=p=0", str(src)],
                                   capture_output=True, text=True).stdout.strip()
            if codec == "aac":
                sound = ["-map", f"0:a:{track}", "-c:a", "copy"]
        result = subprocess.run(["ffmpeg", "-nostdin", "-y", "-loglevel", "error", "-i", str(src), "-vn", *sound,
                                 "-movflags", "+faststart", "-f", "mp4", str(tmp)], capture_output=True, text=True)
        if result.returncode != 0 or not tmp.exists():
            tmp.unlink(missing_ok=True)
            raise HTTPException(500, f"Couldn't prepare the sound for playback: {result.stderr[-300:]}")
        tmp.replace(out)
        return out


@app.get("/transcripts/{tid}/media")
def media_info(tid: str) -> dict:
    """GET /transcripts/{id}/media: whether the recording can be played back, and how (see media_layout)."""
    transcript = _load_transcript(tid)
    src = _recording_of(transcript)
    if transcript.get("source_type") == "text":
        return {"available": False, "reason": "text", "message": "This transcript was imported from text."}
    if src is None:
        return {"available": False, "reason": "missing", "path": transcript.get("source_path"),
                "name": _recording_names(transcript)[0] if _recording_names(transcript) else None,
                "message": "The recording isn't where it was transcribed from any more."}
    try:
        layout = media_layout(src)
    except (subprocess.CalledProcessError, ValueError) as exc:
        return {"available": False, "reason": "unreadable", "path": str(src), "message": str(exc)[:200]}
    return {"available": True, "path": str(src), "name": src.name, "video": layout["video"],
            "parts": layout["parts"], "duration": layout["duration"]}


@app.get("/transcripts/{tid}/media/{part}")
def media_part(tid: str, part: str) -> FileResponse:
    """GET /transcripts/{id}/media/{part}: the picture (`video`: the recording itself), the file's own sound
    (`audio`), or prepared sound (`call`, `mic`, `mix`). Supports Range requests, so the player can seek."""
    if part not in MEDIA_PARTS:
        raise HTTPException(404, "No such part.")
    src = _recording_of(_load_transcript(tid))
    if src is None:
        raise HTTPException(404, "Recording not found.")
    suffix = src.suffix.lower()
    if part == "video":
        return FileResponse(src, media_type=VIDEO_TYPES.get(suffix, "video/mp4"))
    if part == "audio":
        return FileResponse(src, media_type=AUDIO_TYPES.get(suffix, "application/octet-stream"))
    layout = media_layout(src)
    if part in ("call", "mic") and not {"system", "mic"} <= set(layout["roles"]):
        raise HTTPException(404, "This recording has no separate call and microphone tracks.")
    if part == "mix" and layout["audio_tracks"] < 2:
        raise HTTPException(404, "This recording has a single sound track.")
    return FileResponse(prepare_part(tid, src, part, layout), media_type="audio/mp4")


# A recording that moved (an upload copy deleted, the original still on the Desktop) can be found again by name.
HOME_FOLDERS = ("Desktop", "Downloads", "Movies", "Documents")
HOME_DEPTH = 1  # subfolder levels searched in those folders
MAX_CANDIDATES = 20


def _home_folders() -> list[Path]:
    """The usual places a recording lives on a Mac (tests point these at tmp_path)."""
    return [Path.home() / name for name in HOME_FOLDERS]


def _name_key(name: str) -> str:
    """A file name compared loosely: Unicode-normalised, macOS's narrow spaces ("8.02.21 PM") as plain spaces."""
    import unicodedata
    name = unicodedata.normalize("NFC", name).replace(" ", " ").replace("\xa0", " ")
    return name.casefold()


def _recording_names(transcript: dict) -> list[str]:
    """The names the meeting's recording had: its original name and the name it was transcribed under."""
    names = []
    for raw in (transcript.get("source_name"), Path(transcript.get("source_path") or "").name):
        if raw and Path(raw).suffix.lower() in config.MEDIA_SUFFIXES and raw not in names:
            names.append(raw)
    return names


def _search_folders() -> list[tuple[Path, int, str]]:
    """(folder, depth, label) searched for a moved recording: the home folders, then the watch folders and uploads."""
    folders, seen = [], set()
    entries = [(f, HOME_DEPTH, f.name) for f in _home_folders()]
    entries += [(config.MEETINGS_DIR, config.MAX_SCAN_DEPTH, "watch folder"),
                (config.DEFAULT_MEETINGS_DIR, config.MAX_SCAN_DEPTH, "meetings folder"),
                (config.UPLOADS_DIR, 0, "uploads")]
    for folder, depth, label in entries:
        try:
            key = folder.resolve()
        except OSError:
            continue
        if key not in seen:
            seen.add(key)
            folders.append((key, depth, label))
    return folders


def _named_files(root: Path, wanted: set[str], depth: int):
    """Files in `root` (and `depth` levels of subfolders) whose name is in `wanted`; hidden files, symlinks and
    folders macOS won't let us read are skipped."""
    try:
        entries = list(os.scandir(root))
    except OSError:
        return
    for entry in entries:
        if entry.name.startswith("."):
            continue
        try:
            if entry.is_dir(follow_symlinks=False):
                if depth > 0:
                    yield from _named_files(Path(entry.path), wanted, depth - 1)
            elif entry.is_file(follow_symlinks=False) and _name_key(entry.name) in wanted:
                yield Path(entry.path)
        except OSError:
            continue


def _same_length(duration: float | None, meeting: float | None) -> bool:
    """Whether a file is as long as the meeting (within 2 s or 1%); True when the meeting's length isn't known."""
    if not meeting:
        return True
    return duration is not None and abs(duration - meeting) <= max(2.0, 0.01 * meeting)


def _clock(seconds: float | None) -> str:
    """12:03 or 1:02:03."""
    total = int(round(seconds or 0))
    h, rest = divmod(total, 3600)
    return f"{h}:{rest // 60:02d}:{rest % 60:02d}" if h else f"{rest // 60}:{rest % 60:02d}"


def find_recording(transcript: dict) -> list[dict]:
    """Files on this Mac with the meeting recording's name, matched by length (`match`), best first."""
    wanted = {_name_key(n) for n in _recording_names(transcript)}
    if not wanted or transcript.get("source_type") == "text":
        return []
    found: dict[Path, str] = {}
    for folder, depth, label in _search_folders():
        for path in _named_files(folder, wanted, depth):
            found.setdefault(path.resolve(), label)
            if len(found) >= MAX_CANDIDATES:
                break
    items = []
    for path, label in found.items():
        try:
            stat = path.stat()
            duration = pipeline.probe_media(path)["duration"]
        except (OSError, subprocess.CalledProcessError, ValueError):
            stat, duration = None, None
        if stat is None:
            continue
        items.append({"path": str(path), "name": path.name, "folder": label, "size": stat.st_size,
                      "modified": time.strftime("%Y-%m-%d %H:%M", time.localtime(stat.st_mtime)),
                      "duration": duration, "match": _same_length(duration, transcript.get("duration"))})
    items.sort(key=lambda c: (not c["match"], c["folder"] == "uploads"))
    return items


@app.get("/transcripts/{tid}/recording/candidates")
def media_candidates(tid: str) -> dict:
    """GET /transcripts/{id}/recording/candidates: where the meeting's recording might be now (same name; `match` when
    it is as long as the meeting). Searches Desktop, Downloads, Movies, Documents and the meetings folders."""
    transcript = _load_transcript(tid)
    return {"candidates": find_recording(transcript), "names": _recording_names(transcript),
            "folders": [label for _, _, label in _search_folders()], "duration": transcript.get("duration")}


def _unique_upload(name: str) -> Path:
    """A free name in the uploads folder: "Name.mov", else "Name 2.mov", ..."""
    config.UPLOADS_DIR.mkdir(parents=True, exist_ok=True)
    target = config.UPLOADS_DIR / name
    stem, suffix, n = Path(name).stem, Path(name).suffix, 2
    while target.exists():
        target = config.UPLOADS_DIR / f"{stem} {n}{suffix}"
        n += 1
    return target


class RelinkBody(BaseModel):
    """POST /transcripts/{id}/recording body: the file to play this meeting from."""
    path: str = Field(..., min_length=1, max_length=4096)


@app.post("/transcripts/{tid}/recording")
def relink_recording(tid: str, body: RelinkBody) -> dict:
    """POST /transcripts/{id}/recording: plays the meeting from another copy of its recording: one found by
    recording/candidates, or a file just uploaded. It must be as long as the meeting. A file outside the meetings
    folders is cloned into uploads (APFS clone: instant, no extra space, and it survives deleting the original);
    if cloning isn't possible it's used where it is."""
    transcript = _load_transcript(tid)
    if transcript.get("source_type") == "text":
        raise HTTPException(400, "This transcript was imported from text; it has no recording.")
    raw = Path(os.path.expanduser(body.path))
    if not raw.is_absolute():
        raise HTTPException(400, "Give the recording's full path.")
    path = raw.resolve()
    uploads = config.UPLOADS_DIR.resolve()
    in_roots = any(root in path.parents for root in media_roots())
    if not in_roots and str(path) not in {c["path"] for c in find_recording(transcript)}:
        raise HTTPException(400, "Choose one of the recordings found for this meeting, or upload it.")
    if not path.is_file():
        raise HTTPException(404, "Recording not found.")
    if path.suffix.lower() not in config.MEDIA_SUFFIXES:
        raise HTTPException(400, f"Unsupported file type: {path.suffix}")
    duration = _probe(path)["duration"]
    if not _same_length(duration, transcript.get("duration")):
        raise HTTPException(409, f"This file is {_clock(duration)} long; the meeting is "
                                 f"{_clock(transcript.get('duration'))}. It looks like a different recording.")
    target, copied = path, False
    if not in_roots:
        dest = _unique_upload(path.name)
        result = subprocess.run(["cp", "-c", str(path), str(dest)], capture_output=True, text=True)
        if result.returncode == 0 and dest.is_file():
            target, copied = dest, True
        else:
            dest.unlink(missing_ok=True)  # another volume: play it from where it is
    with _CORRECT_LOCK:
        transcript = _load_transcript(tid)
        transcript["source_path"] = str(target)
        transcript["relinked_at"] = datetime.now().isoformat(timespec="seconds")
        pipeline.save_transcript(transcript)
    shutil.rmtree(media_cache(tid), ignore_errors=True)
    return {**media_info(tid), "linked": str(target), "copied": copied, "in_uploads": uploads in target.parents}


# ---------------------------------------------------------------------------
# Correcting a transcript with other transcripts of the same meeting (see correct.py)
# ---------------------------------------------------------------------------

REF_SOURCES = ("voice_memos", "claude", "other")


def refs_dir(tid: str) -> Path:
    """Other transcripts of a meeting (Voice Memos text and audio, claude.ai, …), one JSON file each."""
    return config.TRANSCRIPTS_DIR / ".refs" / tid


def load_refs(tid: str) -> list[dict]:
    folder = refs_dir(tid)
    refs = []
    for path in sorted(folder.glob("*.json")) if folder.exists() else []:
        try:
            refs.append(json.loads(path.read_text()))
        except (OSError, ValueError):
            continue
    return sorted(refs, key=lambda r: r.get("created_at", ""))


def load_ref(tid: str, rid: str) -> dict | None:
    if not re.fullmatch(r"[0-9a-f]{8}", rid or ""):
        return None
    path = refs_dir(tid) / f"{rid}.json"
    return json.loads(path.read_text()) if path.exists() else None


def save_ref(tid: str, ref: dict) -> None:
    refs_dir(tid).mkdir(parents=True, exist_ok=True)
    tmp = refs_dir(tid) / f"{ref['id']}.tmp"
    tmp.write_text(json.dumps(ref, ensure_ascii=False))
    tmp.replace(refs_dir(tid) / f"{ref['id']}.json")


def new_ref(source: str, name: str, kind: str) -> dict:
    rid = uuid.uuid4().hex[:8]
    return {"id": rid, "name": name.strip() or {"voice_memos": "Voice Memos", "claude": "claude.ai"}.get(source, "Other"),
            "source": source, "kind": kind,
            # Voice Memos text and audio are the same recording: one vote between them (see correct.py)
            "recording": source if source in ("voice_memos", "claude") else f"other-{rid}",
            "created_at": datetime.now().isoformat(timespec="seconds"), "status": "ready"}


def ref_summary(ref: dict) -> dict:
    return {k: ref.get(k) for k in ("id", "name", "source", "kind", "status", "error", "created_at", "job_id")}


_CORRECT_LOCK = threading.Lock()


LAYER_ORDER = ("review", "cloud_review")  # model fixes on top of the engine's: the chosen model's review, the
#                                         cloud review's spelling fixes


def model_layers(correction: dict) -> dict[str, dict]:
    """The models' fixes by source (older transcripts kept one review under "model")."""
    layers = dict(correction.get("layers") or {})
    if correction.get("model") and "review" not in layers:
        layers["review"] = correction["model"]
    return {k: v for k, v in layers.items() if v and v.get("changes")}


def apply_corrections(tid: str, known_terms: list[str] | None = None, reapply: bool = False) -> dict:
    """Rebuilds the transcript from its original segments: the corrections from every ready reference (minus the
    reverted changes), the models' fixes (model review, cloud spelling review), then your own edits, which always
    win. Saves it and returns its `correction`. After Undo all the corrections aren't applied until `reapply`
    (adding a source, or Apply again); your edits still are."""
    with _CORRECT_LOCK:
        transcript = _load_transcript(tid)
        previous = transcript.get("correction") or {}
        if known_terms is None:
            known_terms = previous.get("known_terms") or []
        undone = previous.get("undone", False) and not reapply
        refs = load_refs(tid)
        ready = [r for r in refs if r.get("status") == "ready"]
        layers = model_layers(previous)
        ops = (transcript.get("edits") or {}).get("ops") or []
        fixed = set(transcript.get("confirmed_speakers") or {})
        reverted = set(previous.get("reverted", []))
        original = (transcript.get("before_correction") or {}).get("segments") or transcript["segments"]
        if ready and not undone:
            result = correct.correct(original, ready, pipeline.load_words(tid), known_terms, fixed, reverted)
        else:
            result = {"segments": [dict(s) for s in original], "changes": [], "refs": [], "marks": {}, "names": {},
                      "sentences": [], "stats": {"words": 0, "turns": 0, "names": 0, "applied_refs": 0}}
        if not undone:
            for source in LAYER_ORDER:
                if source in layers:
                    correct.apply_model_changes(result, layers[source], reverted, fixed)
        final, origin, state = edits.apply(result["segments"], ops)
        if ops:  # marks and line numbers of the corrections follow your splits and merges
            new_index = {old: new for new, old in enumerate(origin) if old is not None}
            result["marks"] = {str(new_index[int(k)]): v for k, v in result["marks"].items() if int(k) in new_index}
            for item in result["changes"] + result.get("sentences", []):
                if item.get("seg") is not None:
                    item["seg"] = new_index.get(item["seg"])
        if ready or layers or ops:
            transcript["before_correction"] = {"segments": original}
        else:
            transcript.pop("before_correction", None)
        transcript["segments"] = final
        if ops:
            transcript["edits"] = {"ops": ops, "state": state}
        else:
            transcript.pop("edits", None)
        transcript["speakers"] = pipeline.speaker_order(transcript["segments"])
        found = {r["id"]: r for r in result["refs"]}
        correction = {
            "refs": [dict(ref_summary(r), **{k: v for k, v in found.get(r["id"], {}).items() if k not in ("name",)})
                     for r in refs],
            "changes": result["changes"], "marks": result["marks"], "names": result["names"],
            "sentences": result.get("sentences", []), "model": layers.get("review"), "layers": layers,
            "stats": result["stats"], "reverted": previous.get("reverted", []), "kept": previous.get("kept", []),
            "known_terms": known_terms, "undone": undone, "updated_at": datetime.now().isoformat(timespec="seconds"),
        }
        if refs or layers:
            transcript["correction"] = correction
        else:
            transcript.pop("correction", None)
        transcript["updated_at"] = correction["updated_at"]
        pipeline.save_transcript(transcript)
        return correction


class ModelChange(BaseModel):
    """One sentence fix a model proposed (validated by the app): the line, what it read, and the new text and/or
    speaker label."""
    line: int = Field(ge=0)
    before: str = Field(max_length=20_000)
    text: str | None = Field(None, max_length=20_000)
    speaker: str | None = Field(None, max_length=40)
    speaker_name: str | None = Field(None, max_length=120)
    reason: str = Field("", max_length=500)


class ModelChangesBody(BaseModel):
    """POST /transcripts/{id}/corrections/model: a model's review of the corrected transcript (`source` review),
    or the cloud review's spelling fixes (`source` cloud_review, possible without another transcript)."""
    model: str = Field(max_length=300)
    label: str = Field("", max_length=200)
    changes: list[ModelChange] = Field(max_length=2000)
    source: str = Field("review", pattern=r"^(review|cloud_review)$")


@app.post("/transcripts/{tid}/corrections/model")
def correction_model(tid: str, body: ModelChangesBody) -> dict:
    """POST /transcripts/{id}/corrections/model: saves a model's fixes (replacing that source's earlier ones) and
    corrects again with them on top of the engine's corrections."""
    transcript = _load_transcript(tid)
    if body.source == "review" and not transcript.get("correction"):
        raise HTTPException(409, "Add another transcript first: the model reviews the corrections.")
    speakers = set(transcript.get("speakers") or [])
    changes = []
    for ch in body.changes:
        if ch.speaker and ch.speaker not in speakers:
            raise HTTPException(400, f"Unknown speaker {ch.speaker}.")
        data = ch.model_dump()
        data["id"] = correct.change_id(body.source, ch.line, ch.line + 1, f"{ch.text or ''}|{ch.speaker or ''}")
        changes.append(data)
    with _CORRECT_LOCK:
        transcript = _load_transcript(tid)
        correction = transcript.setdefault("correction", {})
        layers = model_layers(correction)
        layers[body.source] = {"model": body.model, "label": body.label, "changes": changes, "source": body.source,
                               "at": datetime.now().isoformat(timespec="seconds")}
        correction["layers"], correction["model"] = layers, layers.get("review")
        pipeline.save_transcript(transcript)
    return apply_corrections(tid)


@app.delete("/transcripts/{tid}/corrections/model")
def correction_model_clear(tid: str, source: str = "review") -> dict:
    """DELETE /transcripts/{id}/corrections/model?source=review|cloud_review: drops that model's fixes (the
    engine's corrections stay)."""
    with _CORRECT_LOCK:
        transcript = _load_transcript(tid)
        correction = transcript.get("correction")
        if correction:
            layers = model_layers(correction)
            layers.pop(source, None)
            correction["layers"], correction["model"] = layers, layers.get("review")
            pipeline.save_transcript(transcript)
    return apply_corrections(tid)


@app.get("/transcripts/{tid}/corrections/context")
def correction_context(tid: str) -> dict:
    """GET /transcripts/{id}/corrections/context: each line of the (corrected) transcript with what the other
    transcripts say there, for a model's review."""
    transcript = _load_transcript(tid)
    ready = [r for r in load_refs(tid) if r.get("status") == "ready"]
    return {"lines": correct.line_context(transcript["segments"], ready),
            "speakers": transcript.get("speakers") or [],
            "fixed": sorted(transcript.get("confirmed_speakers") or {})}


class TextRefBody(BaseModel):
    """POST /transcripts/{id}/references: another transcript of the meeting as text."""
    source: str = Field("other", pattern=r"^(voice_memos|claude|other)$")
    name: str = Field("", max_length=120)
    text: str = Field(min_length=1, max_length=2_000_000)
    known_terms: list[str] | None = None


class AudioRefBody(BaseModel):
    """POST /transcripts/{id}/references/audio: another recording of the meeting (uploaded first)."""
    path: str
    source: str = Field("voice_memos", pattern=r"^(voice_memos|claude|other)$")
    name: str = Field("", max_length=120)
    known_terms: list[str] | None = None


class TermsBody(BaseModel):
    known_terms: list[str] | None = None


@app.get("/transcripts/{tid}/references")
def list_references(tid: str) -> dict:
    """GET /transcripts/{id}/references: the other transcripts added and the current correction."""
    transcript = _load_transcript(tid)
    return {"refs": [ref_summary(r) for r in load_refs(tid)], "correction": transcript.get("correction")}


@app.post("/transcripts/{tid}/references")
def add_text_reference(tid: str, body: TextRefBody) -> dict:
    """POST /transcripts/{id}/references: adds pasted text (Voice Memos, claude.ai, VTT, `Name: text`…) and
    corrects the transcript at once (pure code, a second or two)."""
    _load_transcript(tid)
    segments, timed = text_import.parse(body.text)
    if not segments:
        raise HTTPException(422, "No words found in this text.")
    ref = dict(new_ref(body.source, body.name, "text"), text=body.text, segments=segments, timed=timed)
    save_ref(tid, ref)
    return apply_corrections(tid, body.known_terms, reapply=True)


@app.post("/transcripts/{tid}/references/audio")
def add_audio_reference(tid: str, body: AudioRefBody) -> dict:
    """POST /transcripts/{id}/references/audio: another recording of the meeting (e.g. the Voice Memo), uploaded
    to the uploads folder. It's moved out of sight (never offered as a new recording), transcribed by Whisper as a
    job, and then the transcript is corrected."""
    transcript = _load_transcript(tid)
    src = safe_media_path(body.path)
    if config.UPLOADS_DIR.resolve() not in src.parents:
        raise HTTPException(400, "Upload the recording first.")
    media = _probe(src)
    if not media["audio_tracks"]:
        raise HTTPException(422, pipeline.NO_AUDIO_MESSAGE)
    ref = new_ref(body.source, body.name or src.stem, "audio")
    hidden = config.UPLOADS_DIR / f".ref-{tid}-{ref['id']}{src.suffix.lower()}"
    src.rename(hidden)
    duration = media["duration"]
    rng = pipeline.estimate_seconds(duration, pipeline.load_speed_factor(False))
    job_id = uuid.uuid4().hex[:12]
    with _LOCK:
        JOBS[job_id] = {
            "id": job_id, "path": str(hidden), "name": f"{ref['name']} (for {transcript.get('title', 'meeting')})",
            "status": "queued", "stage": "queued", "progress": 0.0, "duration": duration, "created": time.time(),
            "started": None, "finished": None, "estimate_low": rng["low"], "estimate_high": rng["high"],
            "error": None, "transcript_id": tid, "speakers": None, "screen": None, "kind": "reference",
            "ref_id": ref["id"], "known_terms": body.known_terms,
        }
    save_ref(tid, dict(ref, status="transcribing", path=str(hidden), job_id=job_id))
    JOB_QUEUE.put(job_id)
    return {"job": _public(JOBS[job_id]), "ref": ref_summary(load_ref(tid, ref["id"]))}


@app.delete("/transcripts/{tid}/references/{rid}")
def delete_reference(tid: str, rid: str) -> dict:
    """DELETE /transcripts/{id}/references/{ref}: removes a source (and its recording) and corrects again."""
    ref = load_ref(tid, rid)
    if ref is None:
        raise HTTPException(404, "No such source.")
    for job in JOBS.values():
        if job.get("ref_id") == rid and job["status"] in ("queued", "running"):
            job["cancel_requested"] = True
            if job["status"] == "queued":
                job.update(status="cancelled", stage="cancelled", finished=time.time())
    if ref.get("path"):
        Path(ref["path"]).unlink(missing_ok=True)
    (refs_dir(tid) / f"{rid}.json").unlink(missing_ok=True)
    return apply_corrections(tid)


@app.post("/transcripts/{tid}/corrections/{cid}/{action}")
def correction_action(tid: str, cid: str, action: str) -> dict:
    """POST /transcripts/{id}/corrections/{change}/revert|restore|keep: undoes, brings back or keeps (marks as
    checked) one change."""
    if action not in ("revert", "restore", "keep") or not re.fullmatch(r"[0-9a-f]{10}", cid):
        raise HTTPException(404, "No such action.")
    with _CORRECT_LOCK:
        transcript = _load_transcript(tid)
        correction = transcript.get("correction")
        if not correction:
            raise HTTPException(404, "This transcript has no corrections.")
        reverted, kept = set(correction.get("reverted", [])), set(correction.get("kept", []))
        if action == "keep":
            kept.add(cid)
            reverted.discard(cid)
        else:
            (reverted.add if action == "revert" else reverted.discard)(cid)
            kept.discard(cid)
        correction["reverted"], correction["kept"] = sorted(reverted), sorted(kept)
        pipeline.save_transcript(transcript)
    return apply_corrections(tid)


@app.post("/transcripts/{tid}/corrections/undo")
def undo_corrections(tid: str) -> dict:
    """POST /transcripts/{id}/corrections/undo: back to the original transcript; the sources are kept, so the
    corrections can be applied again."""
    with _CORRECT_LOCK:
        transcript = _load_transcript(tid)
        if transcript.get("correction"):
            transcript["correction"]["undone"] = True
            pipeline.save_transcript(transcript)
    return apply_corrections(tid)


@app.post("/transcripts/{tid}/corrections/apply")
def reapply_corrections(tid: str, body: TermsBody) -> dict:
    """POST /transcripts/{id}/corrections/apply: applies the corrections again (after Undo all, or with new known
    names), keeping changes reverted one by one."""
    return apply_corrections(tid, body.known_terms, reapply=True)


class EditPart(BaseModel):
    text: str = Field(min_length=1, max_length=20_000)
    speaker: str = Field(pattern=r"^[A-Za-z0-9_]{1,40}$")


class EditBody(BaseModel):
    """POST /transcripts/{id}/edits: one edit of yours. `lines`: the line numbers (0-based) it applies to, as you
    saw them; `text` for a wording change or a merge; `speaker` for a reassignment or merge; `parts` for a split
    (the words of each part, in order, and who said it)."""
    kind: str = Field(pattern=r"^(text|speaker|split|merge)$")
    lines: list[int] = Field(min_length=1, max_length=5000)
    text: str | None = Field(None, max_length=20_000)
    speaker: str | None = Field(None, pattern=r"^[A-Za-z0-9_]{1,40}$")
    parts: list[EditPart] | None = Field(None, min_length=2, max_length=10)


def edit_op(transcript: dict, body: EditBody) -> dict:
    """The stored operation for an edit, checked against the lines as they are now (edits.make_op)."""
    try:
        return edits.make_op(transcript["segments"], body.model_dump())
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


def _edit_result(tid: str) -> dict:
    transcript = _load_transcript(tid)
    return {"segments": transcript["segments"], "speakers": transcript.get("speakers") or [],
            "edits": transcript.get("edits") or {"ops": [], "state": {}}, "correction": transcript.get("correction")}


@app.post("/transcripts/{tid}/edits")
def add_edit(tid: str, body: EditBody) -> dict:
    """POST /transcripts/{id}/edits: saves one edit of yours and rebuilds the transcript (your edits win)."""
    with _CORRECT_LOCK:
        transcript = _load_transcript(tid)
        op = edit_op(transcript, body)
        transcript.setdefault("edits", {"ops": []}).setdefault("ops", []).append(op)
        if "before_correction" not in transcript:
            transcript["before_correction"] = {"segments": transcript["segments"]}
        pipeline.save_transcript(transcript)
    apply_corrections(tid)
    return dict(_edit_result(tid), op=op)


@app.delete("/transcripts/{tid}/edits/{op_id}")
def undo_edit(tid: str, op_id: str) -> dict:
    """DELETE /transcripts/{id}/edits/{op}: undoes one edit of yours."""
    with _CORRECT_LOCK:
        transcript = _load_transcript(tid)
        ops = (transcript.get("edits") or {}).get("ops") or []
        if not any(o["id"] == op_id for o in ops):
            raise HTTPException(404, "No such edit.")
        transcript["edits"]["ops"] = [o for o in ops if o["id"] != op_id]
        pipeline.save_transcript(transcript)
    apply_corrections(tid)
    return _edit_result(tid)


@app.delete("/transcripts/{tid}/edits")
def undo_all_edits(tid: str) -> dict:
    """DELETE /transcripts/{id}/edits: undoes all your edits."""
    with _CORRECT_LOCK:
        transcript = _load_transcript(tid)
        if transcript.get("edits"):
            transcript["edits"]["ops"] = []
            pipeline.save_transcript(transcript)
    apply_corrections(tid)
    return _edit_result(tid)


@app.delete("/transcripts/{tid}/voiceprints")
def forget_voiceprints(tid: str) -> dict:
    """DELETE /transcripts/{id}/voiceprints: removes this transcript's voice fingerprints (biometric data)."""
    transcript = _load_transcript(tid)
    count = len(transcript.pop("speaker_embeddings", None) or {})
    transcript.pop("voiceprints_backfilled", None)
    pipeline.save_transcript(transcript)
    return {"deleted": count}


class ImportBody(BaseModel):
    """POST /transcripts/import body: a title, the conversation text and the file it came from."""
    title: str = ""
    text: str
    source_name: str = ""


@app.post("/transcripts/import")
def import_text(body: ImportBody) -> dict:
    """POST /transcripts/import: turns pasted or exported conversation text (VTT, SRT, 'Name: text'…) into a
    transcript; importing the same text again keeps your speaker names.
    """
    if len(body.text) > config.MAX_IMPORT_CHARS:
        raise HTTPException(413, "This text is too large to import (over 20 million characters).")
    try:
        transcript = text_import.build_transcript(body.title, body.text, body.source_name)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    existing = config.TRANSCRIPTS_DIR / f"{transcript['id']}.json"
    if existing.exists():  # same text imported again: keep names the user already set
        try:
            old = json.loads(existing.read_text()).get("speaker_names") or {}
            transcript["speaker_names"].update(old)
        except (OSError, ValueError):
            pass
    pipeline.save_transcript(transcript)
    return _transcript_index()[transcript["id"]]


@app.delete("/transcripts/{tid}")
def delete_transcript(tid: str, recording: bool = False) -> dict:
    """DELETE /transcripts/{id}: deletes a transcript and, with recording=true, its uploaded recording (never
    files in the watch folder).
    """
    transcript = _load_transcript(tid)
    removed = None
    if recording:
        src = _uploaded_recording(transcript)
        if src is not None:
            if any(j["path"] == str(src) and j["status"] in ("queued", "running") for j in JOBS.values()):
                raise HTTPException(409, "This recording is being transcribed; try again when it's done.")
            src.unlink()
            Path(str(src) + ".quorum.json").unlink(missing_ok=True)  # a call recording's track layout
            removed = src.name
    for suffix in (".json", ".md"):
        (config.TRANSCRIPTS_DIR / f"{tid}{suffix}").unlink(missing_ok=True)
    shutil.rmtree(media_cache(tid), ignore_errors=True)
    for ref in load_refs(tid):
        if ref.get("path"):
            Path(ref["path"]).unlink(missing_ok=True)
    shutil.rmtree(refs_dir(tid), ignore_errors=True)
    pipeline.words_file(tid).unlink(missing_ok=True)
    return {"ok": True, "deleted": tid, "removed_recording": removed}


class Segment(BaseModel):
    """One transcript segment: speaker label, start and end seconds, text."""
    speaker: str = Field(pattern=r"^SPEAKER_\d{2,3}$")
    start: float = Field(ge=0)
    end: float = Field(ge=0)
    text: str


class GuessedSegments(BaseModel):
    """PUT /transcripts/{id}/segments body: segments with speakers guessed by the local model, their names, and
    which model guessed them.
    """
    segments: list[Segment] = Field(min_length=1)
    speaker_names: dict[str, str] = {}
    speaker_source: str = Field(min_length=1, max_length=120)


@app.put("/transcripts/{tid}/segments")
def replace_segments(tid: str, body: GuessedSegments) -> dict:
    """PUT /transcripts/{id}/segments: replaces the segments with guessed speakers, keeping the very first
    version so it can be undone.
    """
    transcript = _load_transcript(tid)
    if "before_correction" in transcript:  # corrections were made on the old segments: start again from these
        transcript.pop("before_correction")
        transcript.pop("correction", None)
    transcript.pop("edits", None)  # so were your edits
    if "before_speaker_guess" not in transcript:  # keep the very first version for undo
        transcript["before_speaker_guess"] = {
            k: transcript.get(k) for k in ("segments", "speakers", "speaker_names", "diarization")}
    segments = [seg.model_dump() for seg in body.segments]
    speakers = pipeline.speaker_order(segments)
    transcript.update(
        segments=segments, speakers=speakers, diarization=True,
        speaker_names={k: v.strip() for k, v in body.speaker_names.items() if k in speakers and v.strip()},
        speaker_source=body.speaker_source,
    )
    pipeline.save_transcript(transcript)
    return _transcript_index()[tid]


@app.post("/transcripts/{tid}/undo-speakers")
def undo_speakers(tid: str) -> dict:
    """POST /transcripts/{id}/undo-speakers: restores the transcript from before the speaker guess."""
    transcript = _load_transcript(tid)
    before = transcript.pop("before_speaker_guess", None)
    if before is None:
        raise HTTPException(409, "There is no speaker guess to undo.")
    transcript.update(before)
    transcript.pop("speaker_source", None)
    pipeline.save_transcript(transcript)
    return _transcript_index()[tid]


class SpeakerNames(BaseModel):
    """Body with names for speaker labels, e.g. {'SPEAKER_00': 'Priya'}."""
    names: dict[str, str]


@app.post("/transcripts/{tid}/speakers")
def set_speakers(tid: str, body: SpeakerNames) -> dict:
    """POST /transcripts/{id}/speakers: saves the names shown for speaker labels."""
    transcript = _load_transcript(tid)
    transcript["speaker_names"] = {
        k: v.strip() for k, v in body.names.items() if k in transcript["speakers"] and v.strip()
    }
    pipeline.save_transcript(transcript)
    return {"ok": True, "speaker_names": transcript["speaker_names"]}


# ---------------------------------------------------------------------------
# Resumable chunked upload (no size limit beyond free disk space)
# ---------------------------------------------------------------------------

class UploadInit(BaseModel):
    """POST /uploads/init body: the file name and its size in bytes."""
    name: str
    size: int


def _clean_name(name: str) -> str:
    """A safe file name from the browser's name (no folders, odd characters replaced)."""
    name = Path(name).name
    name = re.sub(r"\s+", " ", name)  # macOS puts a narrow no-break space before AM/PM
    name = re.sub(r"[^\w.\- ()]+", "_", name).strip() or "recording"
    return name


def _upload_dir(uid: str) -> Path:
    """The folder holding one upload's chunks; rejects ids that aren't 32 hex characters."""
    if not re.fullmatch(r"[0-9a-f]{32}", uid):
        raise HTTPException(400, "Bad upload id.")
    return config.UPLOADS_DIR / ".parts" / uid


@app.post("/uploads/init")
def upload_init(body: UploadInit) -> dict:
    """POST /uploads/init: starts an upload, or resumes one with the same name and size (returns the chunks
    already received). Checks type and free disk space first.
    """
    name = _clean_name(body.name)
    if Path(name).suffix.lower() not in config.MEDIA_SUFFIXES:
        raise HTTPException(400, f"Unsupported file type: {Path(name).suffix or '(none)'}")
    free = pipeline.free_disk_bytes(config.UPLOADS_DIR)
    if free - body.size < config.DISK_SAFETY_MARGIN_BYTES:
        raise HTTPException(507, f"Not enough free disk space ({free / 1e9:.1f} GB free).")
    # Same name + size => resume the earlier upload.
    uid = uuid.uuid5(uuid.NAMESPACE_URL, f"{name}|{body.size}").hex
    parts = _upload_dir(uid)
    parts.mkdir(parents=True, exist_ok=True)
    (parts / "meta.json").write_text(json.dumps({"name": name, "size": body.size}))
    received = sorted(int(p.stem) for p in parts.glob("*.part"))
    return {"upload_id": uid, "chunk_size": config.UPLOAD_CHUNK_BYTES, "received": received}


@app.put("/uploads/{uid}/chunk/{n}")
async def upload_chunk(uid: str, n: int, request: Request) -> dict:
    """PUT /uploads/{id}/chunk/{n}: saves one chunk, streamed to disk (never held in memory), renamed into place
    only when complete.
    """
    parts = _upload_dir(uid)
    if not (parts / "meta.json").exists():
        raise HTTPException(404, "Unknown upload.")
    tmp = parts / f"{n:06d}.tmp"
    with tmp.open("wb") as fh:
        async for block in request.stream():
            fh.write(block)
    tmp.rename(parts / f"{n:06d}.part")
    return {"ok": True, "chunk": n}


@app.post("/uploads/{uid}/complete")
def upload_complete(uid: str) -> dict:
    """POST /uploads/{id}/complete: joins the chunks into the recording in the uploads folder, once all of them
    have arrived.
    """
    parts = _upload_dir(uid)
    meta = json.loads((parts / "meta.json").read_text())
    chunks = sorted(parts.glob("*.part"))
    expected = -(-meta["size"] // config.UPLOAD_CHUNK_BYTES)
    if len(chunks) != expected:
        raise HTTPException(409, f"Upload incomplete: {len(chunks)}/{expected} chunks received.")
    target = config.UPLOADS_DIR / meta["name"]
    if target.exists():
        target = target.with_name(f"{target.stem}-{uid[:6]}{target.suffix}")
    with target.open("wb") as out:
        for chunk in chunks:
            with chunk.open("rb") as fh:
                while block := fh.read(1024 * 1024):
                    out.write(block)
    if target.stat().st_size != meta["size"]:
        target.unlink()
        raise HTTPException(500, "Assembled file size mismatch; please retry.")
    for chunk in chunks:
        chunk.unlink()
    (parts / "meta.json").unlink()
    parts.rmdir()
    return {"path": str(target.resolve()), "name": target.name}


@app.get("/upload", response_class=HTMLResponse)
def upload_page() -> str:
    """GET /upload: the standalone upload page."""
    return (Path(__file__).parent / "upload.html").read_text()


# ---------------------------------------------------------------------------
# Recording calls (Quorum Recorder.app) and the live draft
# ---------------------------------------------------------------------------

def _recorder_error(exc: recorder.RecorderError) -> HTTPException:
    """A recorder problem as an HTTP error the app can explain (`kind` says which: permissions, busy, disk…)."""
    code = {"busy": 409, "not_recording": 409, "disk": 507, "unavailable": 501, "bad_source": 400,
            "app_not_found": 404, "display_not_found": 404,
            "cancelled": 409}.get(exc.kind, 503)
    return HTTPException(code, {"kind": exc.kind, "message": str(exc)})


def resume_recording() -> None:
    """After a restart: a call still being recorded gets its live draft back (the recorder kept running)."""
    state = recorder.load_state()
    if state and state.get("live_dir") and recorder.busy():
        live.start(state["id"], Path(state["live_dir"]), state.get("video", True), state.get("title", ""))


@app.get("/record/sources")
def record_sources() -> dict:
    """GET /record/sources: displays, apps with windows (calls and remote desktops first), microphones, and the
    last choice made in the app."""
    try:
        found = recorder.list_sources()
    except recorder.RecorderError as exc:
        raise _recorder_error(exc)
    return dict(found, last=read_settings().get("record_last") or {})


class RecordSource(BaseModel):
    """What to record: a meeting (or remote desktop) app by bundle id, a display by id, or sound only."""
    kind: str = Field(pattern=r"^(app|display|audio)$")
    id: str | int | None = None


class RecordStartBody(BaseModel):
    """POST /record/start body."""
    source: RecordSource
    mic: str | None = Field(None, max_length=300)  # None: follow the Mac's input
    live: bool = False                             # transcribe during the meeting (a rough draft)
    title: str = Field("", max_length=120)


@app.post("/record/start")
def record_start(body: RecordStartBody) -> dict:
    """POST /record/start: starts recording the call (one at a time) and, with `live`, the live draft."""
    source = body.source.model_dump()
    try:
        state = recorder.start(source, body.mic, body.live, body.title)
    except recorder.RecorderError as exc:
        raise _recorder_error(exc)
    write_setting("record_last", {"source": source, "mic": body.mic, "live": body.live})
    if state.get("live_dir"):
        live.start(state["id"], Path(state["live_dir"]), state.get("video", True), state.get("title", ""))
    return recorder.status()


@app.get("/record/status")
def record_status() -> dict:
    """GET /record/status: idle, recording (elapsed, levels, warnings), stopped by itself, or interrupted."""
    status = recorder.status()
    if status.get("live"):
        draft = live.snapshot(status["id"])
        status["live_state"] = draft and {k: draft.get(k) for k in ("state", "processed_seconds", "behind_seconds")}
    return status


class RecordStopBody(BaseModel):
    """POST /record/stop body: whether to transcribe right away, with the New meeting choices."""
    transcribe: bool = True
    speakers: SpeakerHint | None = None
    screen: ScreenOptions | None = None
    models: MeetingModels | None = None


@app.post("/record/stop")
def record_stop(body: RecordStopBody) -> dict:
    """POST /record/stop: ends the recording (or collects one that ended by itself or was interrupted), stops the
    live draft, and queues the final transcription. Returns {recording, job}."""
    try:
        done = recorder.stop()
    except recorder.RecorderError as exc:
        raise _recorder_error(exc)
    live.stop(done["id"])
    job = None
    if done["path"] and body.transcribe:
        job = queue_transcription(Path(done["path"]), body.speakers.model_dump(exclude_none=True) if body.speakers
                                  else None, body.screen.model_dump() if body.screen else None,
                                  body.models.model_dump() if body.models else None, recording_id=done["id"])
    elif not done["path"]:
        recorder.cleanup(done["id"])
    return {"recording": done, "job": job}


def _draft_folder(rec_id: str) -> Path:
    """A recording's live folder (ids are 12 hex characters: no path tricks)."""
    if not re.fullmatch(r"[0-9a-f]{12}", rec_id):
        raise HTTPException(400, "Bad recording id.")
    return recorder.work_root() / rec_id / "live"


def _current_recording_id(rec_id: str | None) -> str:
    """The given id, else the recording in progress, else the newest draft."""
    if rec_id:
        return rec_id
    state = recorder.load_state()
    if state:
        return state["id"]
    if live.DRAFTS:
        return list(live.DRAFTS)[-1]
    raise HTTPException(404, "No live transcript.")


@app.get("/record/live")
def record_live(id: str | None = None, since: int = 0) -> dict:
    """GET /record/live?id=&since=n: the live draft's lines after index n, with its state (running,
    paused_memory, catching_up, done, error) and how far behind the call it is. Once the final transcript exists,
    `transcript_id` says which (the draft is then gone)."""
    rec_id = _current_recording_id(id)
    final = next((t["id"] for t in _transcript_index().values() if t.get("recording_id") == rec_id), None)
    draft = live.snapshot(rec_id, _draft_folder(rec_id))
    if draft is None:
        if final:
            return {"id": rec_id, "state": "final", "segments": [], "total": 0, "transcript_id": final}
        raise HTTPException(404, "No live transcript for this recording.")
    segments = draft.get("segments", [])
    return dict(draft, segments=segments[max(0, since):], total=len(segments), transcript_id=final)


@app.get("/record/live/markdown", response_class=PlainTextResponse)
def record_live_markdown(id: str | None = None) -> str:
    """GET /record/live/markdown: the whole draft as Markdown (Copy, and the chat attachment)."""
    rec_id = _current_recording_id(id)
    draft = live.snapshot(rec_id, _draft_folder(rec_id))
    if draft is None:
        raise HTTPException(404, "No live transcript for this recording.")
    return live.to_markdown(draft, draft.get("title", ""))


class LiveHoldBody(BaseModel):
    """POST /record/live/pause body: how long the draft may stay paused at most (a safety net)."""
    id: str | None = None
    seconds: float = Field(600, gt=0, le=1800)


@app.post("/record/live/pause")
def record_live_pause(body: LiveHoldBody) -> dict:
    """POST /record/live/pause: the live draft frees Whisper while a local model checks the meeting."""
    rec_id = _current_recording_id(body.id)
    draft = live.DRAFTS.get(rec_id)
    if draft:
        draft.hold(body.seconds)
    return {"id": rec_id, "state": draft.state if draft else None}


@app.post("/record/live/resume")
def record_live_resume(body: LiveHoldBody) -> dict:
    """POST /record/live/resume: the live draft carries on (and catches up)."""
    rec_id = _current_recording_id(body.id)
    draft = live.DRAFTS.get(rec_id)
    if draft:
        draft.resume()
    return {"id": rec_id, "state": draft.state if draft else None}
