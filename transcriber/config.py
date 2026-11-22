"""Transcriber settings. Models are fixed by design (see plan); paths come from env."""

from __future__ import annotations

import os
from pathlib import Path

WHISPER_MODEL = "mlx-community/whisper-large-v3-turbo"
DIARIZATION_MODEL = "pyannote/speaker-diarization-3.1"
# Everything the diarization pipeline loads, as (repo, allow_patterns). pyannote 4 also loads
# its PLDA weights from the community-1 repo even when running the 3.1 pipeline; only that
# subfolder is needed. All of these are gated: accept each repo's terms on huggingface.co.
DIARIZATION_REPOS = [
    (DIARIZATION_MODEL, None),
    ("pyannote/segmentation-3.0", None),
    ("pyannote/wespeaker-voxceleb-resnet34-LM", None),
    ("pyannote/speaker-diarization-community-1", ["plda/*"]),
]

# The watch folder can be changed from the app (saved in TRANSCRIPTS_DIR/settings.json);
# MEETINGS_DIR is the current one and DEFAULT_MEETINGS_DIR the one from the environment.
DEFAULT_MEETINGS_DIR = Path(os.environ.get("MEETINGS_DIR", str(Path.home() / "Meetings"))).expanduser()
MEETINGS_DIR = DEFAULT_MEETINGS_DIR
TRANSCRIPTS_DIR = Path(
    os.environ.get("TRANSCRIPTS_DIR", str(Path(__file__).resolve().parent.parent / "data" / "transcripts"))
).expanduser()
# Uploads stay in one place even when the watch folder changes.
UPLOADS_DIR = Path(os.environ.get("UPLOADS_DIR", str(DEFAULT_MEETINGS_DIR / "uploads"))).expanduser()
MAX_SCAN_DEPTH = 4  # subfolder levels scanned in the watch folder
MAX_RECORDINGS = 500
MAX_SPEAKERS = 30
MAX_IMPORT_CHARS = 20_000_000
WORK_DIR = TRANSCRIPTS_DIR / ".work"
OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://127.0.0.1:11434").rstrip("/")

MEDIA_SUFFIXES = {".mp4", ".m4a", ".mov", ".mkv", ".webm", ".wav", ".mp3", ".aac", ".flac", ".ogg", ".m4v", ".qta"}

# Long recordings: Whisper runs in windows (memory stays flat, progress is saved
# after each); diarization runs in overlapping pieces matched by voice fingerprint.
WHISPER_WINDOW_SECONDS = 2 * 60  # small windows: smooth progress, cheap resume
DIARIZATION_CHUNK_SECONDS = 60 * 60
DIARIZATION_OVERLAP_SECONDS = 2 * 60
DIARIZATION_SINGLE_PASS_MAX_SECONDS = 90 * 60
SPEAKER_MATCH_THRESHOLD = 0.55  # cosine similarity for "same voice" across pieces

UPLOAD_CHUNK_BYTES = 8 * 1024 * 1024
DISK_SAFETY_MARGIN_BYTES = 1024 * 1024 * 1024  # keep 1 GB free

# Initial speed guesses (seconds of processing per second of audio); replaced by
# measurements from this laptop after each job (stored in TRANSCRIPTS_DIR/speed.json).
DEFAULT_SPEED_FACTOR = 0.115  # measured without speakers: 11-min meeting in 72 s on the M4 Air
DIARIZATION_COST_FACTOR = 1.35  # initial guess until a run with speaker labels is measured
