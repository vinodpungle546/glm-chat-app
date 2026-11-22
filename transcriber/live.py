"""The live draft: a rough transcript while a call is still being recorded, so you can check actions, ask
questions in chat or copy what was said so far. The final transcript is still made after Stop, from the full
recording (pyannote needs the whole call to keep speaker labels consistent), and replaces this draft.

The recorder (transcriber/record) appends each track as raw 16 kHz mono 16-bit PCM (`system.pcm` = the call,
`mic.pcm` = you), both aligned to the start, and saves a frame every 2 s as `frames/<second>.jpg`. Every ~30 s
`LiveDraft` takes the new audio, cuts it at a pause, runs Whisper on each track (silent mic pieces skipped, the
call leaking into the mic dropped), and labels the lines: "You" for the mic; for the call, the name on the
highlighted tile in the frame of that moment (OCR only; no vision model and no pyannote during a call), else
"Others". The draft pauses while macOS reports memory pressure, or while a local model checks the meeting
(hold/resume, so Whisper and that model never share the 16 GB), and catches up later; the recording never pauses.
"""

from __future__ import annotations

import json
import logging
import subprocess
import threading
import time
from pathlib import Path
from typing import Callable

from transcriber import config, pipeline

log = logging.getLogger(__name__)

RATE = 16000
BYTES_PER_SECOND = RATE * 2
PIECE_SECONDS = 30.0     # how much new audio makes a piece
SNAP_SECONDS = 3.0       # the cut moves to the quietest moment within this many seconds before the target
OVERLAP_SECONDS = 1.0    # Whisper hears this much before the piece (words there belong to the previous piece)
FLUSH_MAX_BEHIND = 120.0  # at Stop, the rest is transcribed only when the draft is at most this far behind
FRAME_SEARCH_SECONDS = 3  # a call line takes its name from a frame at most this far from its start
YOU, OTHERS = "You", "Others"

DRAFTS: dict[str, "LiveDraft"] = {}
_LOCK = threading.Lock()


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------

def memory_pressure() -> int:
    """macOS memory pressure: 1 normal, 2 warning, 4 critical (1 when it can't be read)."""
    try:
        out = subprocess.run(["sysctl", "-n", "kern.memorystatus_vm_pressure_level"], capture_output=True,
                             text=True, timeout=5).stdout.strip()
        return int(out or 1)
    except (OSError, ValueError, subprocess.SubprocessError):
        return 1


def nearest_frame(frames: dict[int, Path], second: float) -> Path | None:
    """The saved frame closest to `second` (within FRAME_SEARCH_SECONDS), or None."""
    if not frames:
        return None
    best = min(frames, key=lambda s: abs(s - second))
    return frames[best] if abs(best - second) <= FRAME_SEARCH_SECONDS else None


def label_call_segments(segments: list[dict], name_at: Callable[[float], str | None]) -> list[dict]:
    """Call lines named from the screen at their start (`name_at(second)`), else "Others". Pure."""
    return [dict(seg, speaker=name_at(seg["start"]) or OTHERS) for seg in segments]


def to_markdown(draft: dict, title: str = "") -> str:
    """The draft as Markdown, in the same line format as transcripts: '[00:03:12] You: text'."""
    upto = pipeline.format_timestamp(draft.get("processed_seconds", 0))
    lines = [f"# Live transcript: {title or 'Call'}",
             f"_Draft while the call is recorded, up to {upto}. Speakers are rough: \"You\" is your microphone, "
             "other names are read from the screen; the final transcript replaces this._", ""]
    for seg in draft.get("segments", []):
        lines.append(f"[{pipeline.format_timestamp(seg['start'])}] {seg['speaker']}: {seg['text']}")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# The draft
# ---------------------------------------------------------------------------

class LiveDraft:
    """Transcribes one recording's live PCM piece by piece in a background thread. The heavy parts can be
    replaced in tests: `transcribe(samples, offset) -> words`, `read_names(paths) -> {path: name}`,
    `pressure() -> level`."""

    def __init__(self, rec_id: str, folder: Path, video: bool = True, title: str = "",
                 transcribe: Callable | None = None, read_names: Callable | None = None,
                 pressure: Callable[[], int] = memory_pressure, wait_for_ollama: bool = True):
        self.id, self.folder, self.video, self.title = rec_id, Path(folder), video, title
        self.file = self.folder / "live.json"
        self.transcribe = transcribe or self._whisper
        self.read_names = read_names or self._ocr_names
        self.pressure = pressure
        self.wait_for_ollama = wait_for_ollama
        self.language: str | None = None
        self.names: dict[str, str | None] = {}  # frame path -> name (cached)
        self.stop_event = threading.Event()
        self.thread: threading.Thread | None = None
        saved = self.load()
        self.segments: list[dict] = saved.get("segments", [])
        self.done = float(saved.get("processed_seconds", 0.0))
        self.state, self.error = "starting", None
        self.hold_until = 0.0  # a local model is checking the meeting: Whisper steps aside until then (or resume)

    # -- files ----------------------------------------------------------------

    def load(self) -> dict:
        """The draft saved so far (a restart of the transcriber resumes from it)."""
        try:
            return json.loads(self.file.read_text())
        except (OSError, ValueError):
            return {}

    def available(self) -> float:
        """Seconds of audio written by the recorder on both tracks."""
        sizes = [(self.folder / f"{t}.pcm").stat().st_size if (self.folder / f"{t}.pcm").exists() else 0
                 for t in ("system", "mic")]
        return min(sizes) / BYTES_PER_SECOND

    def read(self, track: str, start: float, end: float):
        """Seconds `start` to `end` of a track as float samples."""
        import numpy as np

        path = self.folder / f"{track}.pcm"
        first = int(start * RATE) * 2
        count = max(0, int(end * RATE) * 2 - first)
        with path.open("rb") as f:
            f.seek(first)
            raw = f.read(count)
        return np.frombuffer(raw[: len(raw) // 2 * 2], dtype="<i2").astype(np.float32) / 32768.0

    def snapshot(self) -> dict:
        """What the app shows: state, lines so far, how far it got and how far behind the call it is."""
        avail = self.available()
        return {"id": self.id, "state": self.state, "error": self.error, "segments": list(self.segments),
                "processed_seconds": round(self.done, 3), "behind_seconds": round(max(0.0, avail - self.done), 1),
                "title": self.title}

    def save(self) -> None:
        """Writes live.json (atomically)."""
        tmp = self.file.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.snapshot(), ensure_ascii=False))
        tmp.replace(self.file)

    # -- the heavy parts --------------------------------------------------------

    def _whisper(self, samples, offset: float) -> list[dict]:
        """Whisper (the model stays loaded between pieces) on a slice; words with times in the recording."""
        import mlx_whisper

        result = mlx_whisper.transcribe(samples, path_or_hf_repo=config.WHISPER_MODEL, word_timestamps=True,
                                        language=self.language, condition_on_previous_text=False, verbose=None)
        self.language = self.language or result.get("language")
        return [{"word": w["word"], "start": round(w["start"] + offset, 2), "end": round(w["end"] + offset, 2)}
                for seg in result.get("segments", []) for w in seg.get("words", [])]

    def _ocr_names(self, paths: list[Path]) -> dict[str, str | None]:
        """The highlighted tile's name in each frame (Apple Vision OCR, the same reader as screen_names)."""
        from transcriber import screen_names

        if not paths:
            return {}
        try:
            lines = screen_names.run_ocr(paths)
        except Exception:
            log.exception("Live draft: text recognition failed")
            return {str(p): None for p in paths}
        out = {}
        for path in paths:
            try:
                status, found, _tiles = screen_names.read_frame(screen_names.load_rgb(path), lines.get(str(path), []))
            except Exception:
                status, found = "error", None
            out[str(path)] = found[0] if found and status in ("ocr", "speaker_view") and not found[1] else None
        return out

    # -- one piece --------------------------------------------------------------

    def frames(self) -> dict[int, Path]:
        """Saved frames by second."""
        folder = self.folder / "frames"
        out = {}
        for path in folder.glob("*.jpg") if folder.exists() else []:
            if path.stem.isdigit():
                out[int(path.stem)] = path
        return out

    def name_call_lines(self, segments: list[dict]) -> list[dict]:
        """Names the call's lines from the frames of their moments (cached OCR per frame)."""
        if not self.video or not segments:
            return label_call_segments(segments, lambda _s: None)
        frames = self.frames()
        wanted = {seg["start"]: nearest_frame(frames, seg["start"] + 0.5) for seg in segments}
        todo = [p for p in dict.fromkeys(wanted.values()) if p is not None and str(p) not in self.names]
        self.names.update(self.read_names(todo))
        return label_call_segments(segments, lambda s: self.names.get(str(wanted.get(s))) if wanted.get(s) else None)

    def step(self, final: bool = False) -> bool:
        """Transcribes the next piece if enough new audio is there (`final`: whatever is left, up to one piece).
        True when a piece was done."""
        avail = self.available()
        new = avail - self.done
        if (not final and new < PIECE_SECONDS + SNAP_SECONDS) or new < 0.5:
            return False
        start = self.done
        if final and new <= PIECE_SECONDS + SNAP_SECONDS:
            cut = avail
        else:
            target = start + PIECE_SECONDS
            lo = target - SNAP_SECONDS
            both = self.read("system", lo, target + SNAP_SECONDS) + 0  # copy
            mic = self.read("mic", lo, target + SNAP_SECONDS)
            n = min(len(both), len(mic))
            cut = lo + pipeline.quietest_point(both[:n] + mic[:n]) if n else target
        a = max(0.0, start - OVERLAP_SECONDS)
        system = self.read("system", a, cut)
        mic = self.read("mic", a, cut)
        call_words = [] if pipeline.is_silent(system) else self.transcribe(system, a)
        my_words = [] if pipeline.is_silent(mic) else self.transcribe(mic, a)
        keep = lambda words: [w for w in words if start <= w["start"] < cut]  # noqa: E731
        call_words, my_words = keep(call_words), keep(my_words)
        my_words = pipeline.drop_echo(my_words, call_words)
        mine = [dict(seg, speaker=YOU) for seg in pipeline.group_segments([dict(w, speaker=YOU) for w in my_words])]
        theirs = self.name_call_lines(pipeline.group_segments([dict(w, speaker="CALL") for w in call_words]))
        self.segments.extend(sorted(mine + theirs, key=lambda s: (s["start"], s["end"])))
        self.done = cut
        self._drop_old_frames()
        return True

    def _drop_old_frames(self) -> None:
        """Frames well before the transcribed point aren't needed any more."""
        for second, path in self.frames().items():
            if second < self.done - 10:
                path.unlink(missing_ok=True)

    # -- the thread -------------------------------------------------------------

    def run(self) -> None:
        """Piece after piece until Stop; pauses under memory pressure; at Stop, finishes what's left when it
        isn't far behind, then frees Whisper."""
        try:
            if self.wait_for_ollama:
                pipeline.wait_for_ollama_idle(timeout=60)
            self.state = "running"
            self.save()
            while True:
                stopping = self.stop_event.is_set()
                if stopping and self.available() - self.done > FLUSH_MAX_BEHIND:
                    break  # far behind: the final transcript comes soon anyway
                if not stopping and time.time() < self.hold_until:
                    if self.state != "paused_check":  # free Whisper's memory for the local check model
                        self.state = "paused_check"
                        if self.transcribe == self._whisper:
                            pipeline._free_whisper()
                        self.save()
                    self.stop_event.wait(1)
                    continue
                if not stopping and self.pressure() >= 2:
                    if self.state != "paused_memory":
                        self.state = "paused_memory"
                        self.save()
                    self.stop_event.wait(5)
                    continue
                self.state = "catching_up" if self.available() - self.done > 2 * PIECE_SECONDS else "running"
                if self.step(final=stopping):
                    self.save()
                    continue
                if stopping:
                    break
                self.save()
                self.stop_event.wait(2)
            self.state = "done"
        except Exception as exc:  # the draft is a convenience: never take the recording down with it
            log.exception("Live draft failed")
            self.state, self.error = "error", f"{type(exc).__name__}: {str(exc)[:200]}"
        finally:
            if self.transcribe == self._whisper:
                pipeline._free_whisper()
            self.save()

    def start(self) -> None:
        self.thread = threading.Thread(target=self.run, daemon=True, name=f"live-{self.id}")
        self.thread.start()

    def stop(self) -> None:
        self.stop_event.set()

    def hold(self, seconds: float) -> None:
        """Pause for a local check model (at most `seconds`; resume() ends it sooner). The recording continues."""
        self.hold_until = time.time() + max(0.0, seconds)

    def resume(self) -> None:
        self.hold_until = 0.0


# ---------------------------------------------------------------------------
# Registry (one recording at a time, but a finished draft stays readable)
# ---------------------------------------------------------------------------

def start(rec_id: str, folder: Path, video: bool, title: str = "") -> LiveDraft:
    """Starts (or, after a transcriber restart, resumes) the draft of a recording."""
    with _LOCK:
        draft = DRAFTS.get(rec_id)
        if draft and draft.thread and draft.thread.is_alive():
            return draft
        draft = LiveDraft(rec_id, folder, video, title)
        DRAFTS[rec_id] = draft
        draft.start()
        return draft


def stop(rec_id: str) -> None:
    """Asks the draft to finish (it transcribes what's left when it isn't far behind)."""
    draft = DRAFTS.get(rec_id)
    if draft:
        draft.stop()


def active() -> bool:
    """True while a draft is still transcribing (the final transcription waits for it: one Whisper at a time)."""
    return any(d.thread and d.thread.is_alive() for d in DRAFTS.values())


def wait_idle(timeout: float = 600) -> None:
    """Waits until no draft is transcribing."""
    deadline = time.time() + timeout
    while active() and time.time() < deadline:
        time.sleep(0.5)


def snapshot(rec_id: str, folder: Path | None = None) -> dict | None:
    """The draft now (from memory, else from its live.json), or None."""
    draft = DRAFTS.get(rec_id)
    if draft:
        return draft.snapshot()
    if folder and (folder / "live.json").exists():
        try:
            return json.loads((folder / "live.json").read_text())
        except (OSError, ValueError):
            return None
    return None
