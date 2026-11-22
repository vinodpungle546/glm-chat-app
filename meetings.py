#!/usr/bin/env python3
"""Client for the local transcriber service (transcriber/server.py).

Transcription is local-only: there is no cloud path. If the service is down,
callers get TranscriberUnavailable with the command to start it.
"""

from __future__ import annotations

import os

import requests

TRANSCRIBER_URL = os.environ.get("TRANSCRIBER_URL", "http://localhost:8765").rstrip("/")
# URL the *browser* uses for the embedded upload page.
TRANSCRIBER_PUBLIC_URL = os.environ.get("TRANSCRIBER_PUBLIC_URL", "http://localhost:8765").rstrip("/")
START_HINT = "Start the local transcriber with `scripts/start_local_ai.sh` (runs natively on this Mac)."


class TranscriberUnavailable(RuntimeError):
    """Raised when the local transcriber service can't be reached."""
    pass


class RecordError(RuntimeError):
    """The call recorder refused or failed; `kind` says why (permission_screen, permission_mic, busy, disk,
    unavailable, app_not_found, timeout, not_recording…) and `status` is the transcriber's HTTP code."""

    def __init__(self, kind: str, message: str, status: int = 400):
        super().__init__(message)
        self.kind, self.status = kind, status


def _call(method: str, path: str, timeout: float = 10, **kwargs):
    """Sends one request to the transcriber and returns the response. Network errors become
    TranscriberUnavailable (with a hint on how to start it); error replies become RuntimeError with the
    transcriber's own message.
    """
    try:
        response = requests.request(method, f"{TRANSCRIBER_URL}{path}", timeout=timeout, **kwargs)
    except requests.RequestException as exc:
        raise TranscriberUnavailable(f"Local transcriber not reachable at {TRANSCRIBER_URL}. {START_HINT}") from exc
    if response.status_code >= 400:
        try:
            detail = response.json().get("detail", response.text)
        except ValueError:
            detail = response.text
        if isinstance(detail, dict) and detail.get("kind"):
            raise RecordError(detail["kind"], str(detail.get("message", ""))[:300], response.status_code)
        raise RuntimeError(str(detail)[:300])
    return response


def health() -> dict:
    """The transcriber's status: which models are loaded, whether it runs offline, etc."""
    return _call("GET", "/health", timeout=3).json()


def recordings() -> list[dict]:
    """Media files in the watch folder and the uploads folder."""
    return _call("GET", "/recordings").json()


def estimate(path: str, vision_cap: int = 0, vision_seconds: float | None = None) -> dict:
    """How long a recording will take to transcribe, plus disk space and whether it has sound or video.
    `vision_cap` / `vision_seconds` add the time of reading names from video.
    """
    body: dict = {"path": path}
    if vision_cap:
        body["vision_cap"] = int(vision_cap)
        if vision_seconds:
            body["vision_seconds"] = float(vision_seconds)
    return _call("POST", "/estimate", json=body, timeout=30).json()


def start(path: str, num_speakers: int | None = None, screen: dict | None = None,
          models: dict | None = None) -> dict:
    """Queue a transcription; `num_speakers` (exact count) helps speaker labelling, None = auto.
    `screen`: on-screen name options {"enabled", "vision_model", "vision_cap", "debug"}.
    `models`: the meeting's model choices {speaker_model, frames_model, summary_model, after}."""
    body: dict = {"path": path}
    if num_speakers:
        body["speakers"] = {"num": int(num_speakers)}
    if screen is not None:
        body["screen"] = screen
    if models is not None:
        body["models"] = models
    return _call("POST", "/jobs", json=body, timeout=30).json()


def read_screen_names(transcript_id: str, screen: dict | None = None) -> dict:
    """Queue on-screen name reading for an existing transcript (its video must still exist)."""
    return _call("POST", f"/transcripts/{transcript_id}/screen-names", json={"screen": screen} if screen else {},
                 timeout=30).json()


def backfill_voiceprints(transcript_id: str) -> dict:
    """Queue voice fingerprints for an older transcript (pyannote embeddings only)."""
    return _call("POST", f"/transcripts/{transcript_id}/voiceprints", timeout=30).json()


def settings() -> dict:
    """The transcriber's settings (the watch folder)."""
    return _call("GET", "/settings").json()


def set_watch_folder(folder: str, create: bool = False) -> dict:
    """Change the watch folder (a path on this Mac); "" resets to the default."""
    return _call("POST", "/settings", json={"meetings_dir": folder, "create": create}).json()


def import_text(title: str, text: str, source_name: str = "") -> dict:
    """Create a transcript from existing conversation text (VTT/SRT/plain); returns its summary."""
    return _call("POST", "/transcripts/import", timeout=120,
                 json={"title": title, "text": text, "source_name": source_name}).json()


def jobs() -> list[dict]:
    """All transcription jobs the transcriber knows (since it last started)."""
    return _call("GET", "/jobs").json()


def transcripts() -> list[dict]:
    """The list of transcripts (without their text)."""
    return _call("GET", "/transcripts").json()


def transcript(transcript_id: str) -> dict:
    """One full transcript: segments with times, speakers and text."""
    return _call("GET", f"/transcripts/{transcript_id}", timeout=30).json()


def media_info(transcript_id: str) -> dict:
    """Whether a transcript's recording can be played back: {available, video, parts, duration, path} (or
    {available: False, reason, message})."""
    return _call("GET", f"/transcripts/{transcript_id}/media", timeout=30).json()


def media_stream(transcript_id: str, part: str, range_header: str | None = None) -> requests.Response:
    """A streaming GET of one playback part (video, audio, call, mic, mix), passing the browser's Range header so
    the player can seek. The caller closes the response."""
    headers = {"Range": range_header} if range_header else {}
    try:
        return requests.get(f"{TRANSCRIBER_URL}/transcripts/{transcript_id}/media/{part}", headers=headers,
                            stream=True, timeout=(5, 600))
    except requests.RequestException as exc:
        raise TranscriberUnavailable(f"Local transcriber not reachable at {TRANSCRIBER_URL}. {START_HINT}") from exc


def references(transcript_id: str) -> dict:
    """Other transcripts added to a meeting and its current correction: {refs, correction}."""
    return _call("GET", f"/transcripts/{transcript_id}/references", timeout=15).json()


def add_text_reference(transcript_id: str, source: str, name: str, text: str, known_terms: list[str]) -> dict:
    """Adds another transcript of the meeting as text and corrects at once; returns the correction."""
    return _call("POST", f"/transcripts/{transcript_id}/references", timeout=120,
                 json={"source": source, "name": name, "text": text, "known_terms": known_terms}).json()


def add_audio_reference(transcript_id: str, path: str, source: str, name: str, known_terms: list[str]) -> dict:
    """Adds another recording of the meeting (uploaded first); returns {job, ref}."""
    return _call("POST", f"/transcripts/{transcript_id}/references/audio", timeout=30,
                 json={"path": path, "source": source, "name": name, "known_terms": known_terms}).json()


def delete_reference(transcript_id: str, ref_id: str) -> dict:
    return _call("DELETE", f"/transcripts/{transcript_id}/references/{ref_id}", timeout=120).json()


def correction_action(transcript_id: str, action: str, change_id: str | None = None,
                      known_terms: list[str] | None = None) -> dict:
    """revert/restore one change, undo all, or apply again; returns the correction."""
    if action in ("revert", "restore"):
        return _call("POST", f"/transcripts/{transcript_id}/corrections/{change_id}/{action}", timeout=120).json()
    body = {"known_terms": known_terms} if action == "apply" else None
    return _call("POST", f"/transcripts/{transcript_id}/corrections/{action}", json=body, timeout=120).json()


def markdown(transcript_id: str) -> str:
    """A transcript as Markdown ('[00:03:12] Priya: …' lines), as sent to the models."""
    return _call("GET", f"/transcripts/{transcript_id}/markdown", timeout=30).text


def delete_transcript(transcript_id: str, recording: bool = False) -> dict:
    """Delete a transcript; `recording=True` also deletes its recording if it was uploaded."""
    return _call("DELETE", f"/transcripts/{transcript_id}",
                 params={"recording": "true" if recording else "false"}).json()


def save_guessed_speakers(transcript_id: str, segments: list[dict], speaker_names: dict[str, str],
                          model: str) -> dict:
    """Store speakers guessed by the local model (the transcriber keeps the original for undo)."""
    return _call("PUT", f"/transcripts/{transcript_id}/segments", timeout=60, json={
        "segments": segments, "speaker_names": speaker_names, "speaker_source": model}).json()


def set_speaker_names(transcript_id: str, names: dict[str, str]) -> dict:
    """Saves the names shown for speaker labels, e.g. {'SPEAKER_00': 'Priya'}."""
    return _call("POST", f"/transcripts/{transcript_id}/speakers", json={"names": names}).json()


def fmt_seconds(seconds: float) -> str:
    """A duration as '2 h 05 min', '1 min 15 s' or '42 s'."""
    seconds = int(max(0, seconds))
    h, m, s = seconds // 3600, seconds % 3600 // 60, seconds % 60
    return f"{h} h {m:02d} min" if h else (f"{m} min {s:02d} s" if m else f"{s} s")


def cancel_job(job_id: str) -> dict:
    """Stop a transcription: queued jobs are dropped, a running one stops at its next step."""
    return _call("POST", f"/jobs/{job_id}/cancel").json()


def job(job_id: str) -> dict:
    """One job's status and progress."""
    return _call("GET", f"/jobs/{job_id}").json()


def job_partial(job_id: str) -> dict:
    """Text transcribed so far by a running job (no speakers yet)."""
    return _call("GET", f"/jobs/{job_id}/partial", timeout=15).json()


def upload_init(name: str, size: int) -> dict:
    """Starts (or resumes) a chunked upload; returns its id, the chunk size and the chunks already received."""
    return _call("POST", "/uploads/init", json={"name": name, "size": size}).json()


def upload_chunk(upload_id: str, n: int, data: bytes) -> dict:
    """Sends chunk number `n` of an upload."""
    return _call("PUT", f"/uploads/{upload_id}/chunk/{n}", data=data, timeout=120).json()


def upload_complete(upload_id: str) -> dict:
    """Finishes an upload: the transcriber joins the chunks into the recording file."""
    return _call("POST", f"/uploads/{upload_id}/complete", timeout=600).json()


# ---------------------------------------------------------------------------
# Recording calls (the transcriber drives Quorum Recorder.app) and the live draft
# ---------------------------------------------------------------------------

def record_sources() -> dict:
    """Displays, apps (calls and remote desktops first), microphones and the last choice."""
    return _call("GET", "/record/sources", timeout=30).json()


def record_start(source: dict, mic: str | None = None, live: bool = False, title: str = "") -> dict:
    """Starts recording a call: source {kind: app|display|audio, id}; `mic` None follows the Mac's input."""
    return _call("POST", "/record/start", json={"source": source, "mic": mic, "live": live, "title": title},
                 timeout=330).json()


def record_status() -> dict:
    """The recording now: idle, recording (elapsed, levels, warnings, live draft state), stopped or interrupted."""
    return _call("GET", "/record/status", timeout=5).json()


def record_stop(transcribe: bool = True, num_speakers: int | None = None, screen: dict | None = None,
                models: dict | None = None) -> dict:
    """Ends the recording and queues its transcription: {recording, job}."""
    body: dict = {"transcribe": transcribe}
    if num_speakers:
        body["speakers"] = {"num": int(num_speakers)}
    if screen is not None:
        body["screen"] = screen
    if models is not None:
        body["models"] = models
    return _call("POST", "/record/stop", json=body, timeout=90).json()


def record_live(recording_id: str | None = None, since: int = 0) -> dict:
    """The live draft's lines after `since`, its state, and the final transcript id once there is one."""
    params: dict = {"since": since}
    if recording_id:
        params["id"] = recording_id
    return _call("GET", "/record/live", params=params, timeout=5).json()


def record_live_markdown(recording_id: str | None = None) -> str:
    """The whole live draft as Markdown."""
    return _call("GET", "/record/live/markdown", params={"id": recording_id} if recording_id else None,
                 timeout=5).text


def record_live_pause(recording_id: str | None = None, seconds: float = 600) -> dict:
    """Pauses the live draft (Whisper frees its memory) while a local model checks the meeting."""
    return _call("POST", "/record/live/pause", json={"id": recording_id, "seconds": seconds}, timeout=5).json()


def record_live_resume(recording_id: str | None = None) -> dict:
    """Lets the live draft carry on after a check."""
    return _call("POST", "/record/live/resume", json={"id": recording_id}, timeout=5).json()
