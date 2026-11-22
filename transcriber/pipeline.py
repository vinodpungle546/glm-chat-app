"""Meeting transcription pipeline - runs entirely on this Mac, offline.

    recording --ffmpeg--> 16 kHz mono WAV
              --mlx-whisper (large-v3-turbo, 10-min windows)--> timestamped words
              --pyannote 3.1 (60-min pieces, voice-matched)--> speaker turns
              --assign_speakers--> speaker-labelled segments -> .json + .md

Long recordings: memory stays flat (Whisper reads the WAV window by window),
progress is saved after every window/piece so a crash or sleep resumes where it
stopped, and speakers keep one label across pieces via voice fingerprints.

Heavy libraries (mlx, torch, pyannote) are imported lazily so the pure helpers
below can be unit-tested without them.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import re
import shutil
import subprocess
import time
from datetime import datetime
from pathlib import Path
from typing import Callable

from transcriber import config

log = logging.getLogger(__name__)

ProgressFn = Callable[[str, float], None]  # (stage, fraction 0..1 of whole job)

STAGE_WEIGHTS = {"extract": 0.05, "transcribe": 0.66, "diarize": 0.21, "screen": 0.05, "finish": 0.03}


# ---------------------------------------------------------------------------
# Pure helpers (unit-tested)
# ---------------------------------------------------------------------------

def format_timestamp(seconds: float) -> str:
    """Seconds as 'hh:mm:ss'."""
    seconds = max(0, int(seconds))
    return f"{seconds // 3600:02d}:{seconds % 3600 // 60:02d}:{seconds % 60:02d}"


def plan_windows(duration: float, window: float) -> list[tuple[float, float]]:
    """Split [0, duration) into consecutive windows."""
    if duration <= 0:
        return []
    count = max(1, math.ceil(duration / window))
    return [(i * window, min(duration, (i + 1) * window)) for i in range(count)]


def plan_diarization_chunks(
    duration: float,
    chunk: float = config.DIARIZATION_CHUNK_SECONDS,
    overlap: float = config.DIARIZATION_OVERLAP_SECONDS,
    single_pass_max: float = config.DIARIZATION_SINGLE_PASS_MAX_SECONDS,
) -> list[tuple[float, float]]:
    """One pass for short audio; otherwise overlapping pieces."""
    if duration <= single_pass_max:
        return [(0.0, duration)]
    pieces, start = [], 0.0
    while start < duration:
        end = min(duration, start + chunk)
        pieces.append((start, end))
        if end >= duration:
            break
        start = end - overlap
    return pieces


def _overlap(a_start, a_end, b_start, b_end) -> float:
    """How many seconds two time ranges share (0 when they don't overlap)."""
    return max(0.0, min(a_end, b_end) - max(a_start, b_start))


def assign_speakers(words: list[dict], turns: list[dict], max_gap: float = 1.0) -> list[dict]:
    """Label each word with the speaker whose turn overlaps it most.

    Words falling in a gap between turns take the nearest turn within max_gap
    seconds, else the previous word's speaker. Returns new word dicts.
    """
    turns = sorted(turns, key=lambda t: t["start"])
    labelled, previous = [], None
    for word in words:
        best, best_overlap = None, 0.0
        for turn in turns:
            if turn["start"] > word["end"]:
                break
            ov = _overlap(word["start"], word["end"], turn["start"], turn["end"])
            if ov > best_overlap:
                best, best_overlap = turn["speaker"], ov
        if best is None and turns:
            mid = (word["start"] + word["end"]) / 2
            nearest = min(turns, key=lambda t: min(abs(mid - t["start"]), abs(mid - t["end"])))
            if min(abs(mid - nearest["start"]), abs(mid - nearest["end"])) <= max_gap:
                best = nearest["speaker"]
        if best is None:
            best = previous or "SPEAKER_00"
        labelled.append(dict(word, speaker=best))
        previous = best
    return labelled


def group_segments(words: list[dict], max_seconds: float = 60.0, pause_seconds: float = 1.5) -> list[dict]:
    """Merge consecutive words of the same speaker into readable segments.

    A new segment starts on a speaker change, after a pause at a sentence end,
    or when a segment runs past max_seconds at a sentence end.
    """
    segments: list[dict] = []
    for word in words:
        text = word["word"]
        current = segments[-1] if segments else None
        sentence_end = bool(current) and bool(re.search(r"[.!?]$", current["text"].strip() or ""))
        too_long = bool(current) and word["start"] - current["start"] > max_seconds and sentence_end
        paused = bool(current) and word["start"] - current["end"] >= pause_seconds and sentence_end
        if current and current["speaker"] == word.get("speaker") and not too_long and not paused:
            current["text"] += text
            current["end"] = word["end"]
        else:
            segments.append({"speaker": word.get("speaker", "SPEAKER_00"), "start": word["start"],
                             "end": word["end"], "text": text})
    for seg in segments:
        seg["text"] = re.sub(r"\s+", " ", seg["text"]).strip()
    return [s for s in segments if s["text"]]


def _tokens(text: str) -> list[str]:
    """Lower-case words without punctuation (for comparing what two tracks heard)."""
    return re.findall(r"[\w']+", text.lower())


def drop_echo(mic_words: list[dict], system_words: list[dict], margin: float = 1.5, share: float = 0.6) -> list[dict]:
    """Your microphone words minus the call leaking into it (laptop speakers instead of headphones).

    The mic words are grouped into phrases; a phrase whose words mostly (`share`) also appear on the call track
    within `margin` seconds is the call heard through the speakers, so it's dropped. Pure.
    """
    if not mic_words or not system_words:
        return list(mic_words)
    system = sorted(system_words, key=lambda w: w["start"])
    kept: list[dict] = []
    phrase: list[dict] = []

    def flush() -> None:
        if not phrase:
            return
        start, end = phrase[0]["start"] - margin, phrase[-1]["end"] + margin
        heard = {t for w in system if start <= w["start"] <= end for t in _tokens(w["word"])}
        mine = [t for w in phrase for t in _tokens(w["word"])]
        if not (mine and heard and sum(t in heard for t in mine) / len(mine) >= share):
            kept.extend(phrase)
        phrase.clear()

    for word in sorted(mic_words, key=lambda w: w["start"]):
        if phrase and word["start"] - phrase[-1]["end"] > 0.8:
            flush()
        phrase.append(word)
    flush()
    return kept


def others_hint(speakers: dict | None) -> dict | None:
    """The speaker-count hint for the call track: everyone except you (you're on the microphone track)."""
    if not speakers:
        return None
    out = {}
    for key in ("num", "min", "max"):
        if speakers.get(key):
            out[key] = max(1, int(speakers[key]) - 1)
    return out or None


MIC_SPEAKER = "MIC"


def merge_tracks(mic_words: list[dict], system_words: list[dict]) -> tuple[list[dict], dict[str, str]]:
    """One transcript from the two tracks: each track is grouped into phrases on its own, then the phrases are
    ordered by time, so when you and someone else talk at once both are kept whole (not interleaved word by word).
    `system_words` carry their speaker labels. Returns (segments labelled SPEAKER_00… in order of first
    appearance, {original label: new label}); your words' original label is MIC_SPEAKER. Pure.
    """
    mine = group_segments([dict(w, speaker=MIC_SPEAKER) for w in mic_words])
    theirs = group_segments(sorted(system_words, key=lambda w: w["start"]))
    segments = sorted(mine + theirs, key=lambda seg: (seg["start"], seg["end"]))
    order = {spk: f"SPEAKER_{i:02d}" for i, spk in enumerate(speaker_order(segments))}
    return [dict(seg, speaker=order[seg["speaker"]]) for seg in segments], order


def stitch_turns(pieces: list[dict]) -> list[dict]:
    """Combine per-piece turns (already in global time + global labels).

    Each piece: {"start", "end", "turns"}. In overlaps, each side keeps the half
    nearest to its own centre, so nothing is duplicated or lost.
    """
    result = []
    for i, piece in enumerate(pieces):
        lo = piece["start"] if i == 0 else (pieces[i - 1]["end"] + piece["start"]) / 2
        hi = piece["end"] if i == len(pieces) - 1 else (piece["end"] + pieces[i + 1]["start"]) / 2
        for turn in piece["turns"]:
            start, end = max(turn["start"], lo), min(turn["end"], hi)
            if end > start:
                result.append({"start": start, "end": end, "speaker": turn["speaker"]})
    return sorted(result, key=lambda t: t["start"])


def _cosine(a, b) -> float:
    """Cosine similarity of two voice fingerprints (close to 1 = same voice)."""
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


def match_speakers(
    known: dict[str, list[float]],
    piece_embeddings: dict[str, list[float]],
    threshold: float = config.SPEAKER_MATCH_THRESHOLD,
) -> dict[str, str]:
    """Map a piece's local speaker labels onto global labels by voice similarity.

    Greedy best-first matching, one-to-one; unmatched voices become new global
    speakers (SPEAKER_NN). `known` is updated in place with new speakers.
    """
    pairs = sorted(
        ((_cosine(emb, gemb), local, glabel)
         for local, emb in piece_embeddings.items() if emb is not None
         for glabel, gemb in known.items()),
        reverse=True,
    )
    mapping, used = {}, set()
    for sim, local, glabel in pairs:
        if sim < threshold or local in mapping or glabel in used:
            continue
        mapping[local] = glabel
        used.add(glabel)
    for local in sorted(piece_embeddings):
        if local not in mapping:
            new_label = f"SPEAKER_{len(known):02d}"
            while new_label in known:
                new_label = f"SPEAKER_{int(new_label[8:]) + 1:02d}"
            known[new_label] = piece_embeddings[local]
            mapping[local] = new_label
    return mapping


def speaker_order(segments: list[dict]) -> list[str]:
    """Speakers in order of first appearance."""
    seen = []
    for seg in segments:
        if seg["speaker"] not in seen:
            seen.append(seg["speaker"])
    return seen


def to_markdown(transcript: dict) -> str:
    """The transcript as Markdown: a short header (source, duration, speakers, models) then one line per segment,
    '[00:03:12] Priya: text'.
    """
    names = transcript.get("speaker_names") or {}
    from_text = transcript.get("source_type") == "text"
    timed = transcript.get("timestamps", True)
    details = [f"Source: {transcript.get('source_name', '')}"]
    if from_text:
        details.append("Imported from conversation text")
    if not from_text or timed:
        details.append(f"Duration: {format_timestamp(transcript.get('duration', 0))}")
    if transcript.get("language") not in (None, "?"):
        details.append(f"Language: {transcript['language']}")
    header = [f"# Meeting transcript: {transcript.get('title', '')}", " · ".join(details), ""]
    if transcript.get("speaker_source"):
        header.insert(2, f"_Speakers guessed from the text by {transcript['speaker_source']}; "
                         "they may be wrong._")
    elif transcript.get("diarization_error"):
        header.insert(2, f"_Speaker labels failed: {transcript['diarization_error']}_")
    elif not transcript.get("diarization"):
        header.insert(2, "_No speaker names found in the text._" if from_text
                      else "_Speaker labels unavailable (diarization model not installed)._")
    lines = []
    for s in transcript.get("segments", []):
        stamp = f"[{format_timestamp(s['start'])}] " if timed else ""
        who = f"{names.get(s['speaker']) or s['speaker']}: " if transcript.get("diarization") else ""
        lines.append(f"{stamp}{who}{s['text']}")
    return "\n".join(header + lines) + "\n"


def estimate_seconds(duration: float, speed_factor: float) -> dict:
    """Processing-time range for a recording of `duration` seconds.

    The fanless MacBook Air slows down under sustained load, so the upper bound
    widens for long recordings (measured: 3 h took ~1.35x the short-file rate).
    """
    mid = duration * speed_factor + 20  # + model load
    throttle = 1.0 + min(0.5, duration / 3600 * 0.15)
    return {"low": round(mid * 0.7), "high": round(mid * 1.25 * throttle)}


def updated_speed_factor(old: float, measured: float, weight: float = 0.5) -> float:
    """Blends the stored speed with a new measurement (half each by default), so estimates adapt to this Mac
    without jumping on one odd job.
    """
    return round(old * (1 - weight) + measured * weight, 4)


def source_id(path: Path) -> str:
    """Stable id for a recording (path + size + mtime): resumes the same work dir."""
    st = path.stat()
    raw = f"{path.resolve()}|{st.st_size}|{int(st.st_mtime)}"
    return hashlib.sha1(raw.encode()).hexdigest()[:16]


# ---------------------------------------------------------------------------
# Speed factor persistence (calibrated on this laptop after every job)
# ---------------------------------------------------------------------------

def _speed_path() -> Path:
    """Where the measured transcription speed is saved (speed.json in the transcripts folder)."""
    return config.TRANSCRIPTS_DIR / "speed.json"


def _speed_key(diarization: bool) -> str:
    """Speed is kept separately with and without speaker labelling (pyannote adds time)."""
    return "with_speakers" if diarization else "without_speakers"


def load_speed_factor(diarization: bool) -> float:
    """Measured seconds of processing per second of audio, per pipeline variant."""
    try:
        data = json.loads(_speed_path().read_text())
    except (OSError, ValueError):
        data = {}
    if _speed_key(diarization) in data:
        return float(data[_speed_key(diarization)])
    base = float(data.get(_speed_key(False), config.DEFAULT_SPEED_FACTOR))
    return round(base * config.DIARIZATION_COST_FACTOR, 4) if diarization else base


def save_speed_factor(measured: float, diarization: bool) -> None:
    """Saves the measured seconds-of-work per second-of-audio after a job, for better estimates next time."""
    config.TRANSCRIPTS_DIR.mkdir(parents=True, exist_ok=True)
    try:
        data = json.loads(_speed_path().read_text())
    except (OSError, ValueError):
        data = {}
    data.pop("factor", None)  # old single-factor format
    data[_speed_key(diarization)] = updated_speed_factor(load_speed_factor(diarization), measured)
    data["updated_at"] = datetime.now().isoformat(timespec="seconds")
    _speed_path().write_text(json.dumps(data))


# ---------------------------------------------------------------------------
# Media helpers (ffmpeg / ffprobe)
# ---------------------------------------------------------------------------

NO_AUDIO_MESSAGE = (
    "This recording has no sound, so there is nothing to transcribe. macOS screen recordings "
    "(⇧⌘5) record sound only when a microphone is chosen under Options → Microphone before "
    "recording starts; the default is “None”."
)


class JobCancelled(Exception):
    """Raised from the progress callback when the user cancels a running job."""


def partial_segments(src: Path) -> dict:
    """Text transcribed so far for a running job (no speakers yet), from the per-window files."""
    work = config.WORK_DIR / source_id(src)
    words: list[dict] = []
    files = sorted(work.glob("whisper_*.json")) if work.exists() else []
    for part in files:
        try:
            words.extend(dict(w, speaker="") for w in json.loads(part.read_text())["words"])
        except (OSError, ValueError, KeyError):
            break  # being written right now
    words.sort(key=lambda w: w["start"])  # a call recording has a part per track
    segments = [{"start": s["start"], "end": s["end"], "text": s["text"]} for s in group_segments(words)]
    return {"segments": segments, "windows": len(files)}


class NoAudioError(RuntimeError):
    """The recording has no audio track (e.g. a screen recording made without a microphone)."""


def _seconds(raw) -> float:
    """A duration from ffprobe as a positive float (0.0 for missing or invalid values)."""
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return 0.0
    return value if math.isfinite(value) and value > 0 else 0.0


def probe_media(path: Path) -> dict:
    """Duration and number of audio tracks (screen recordings may have none, or several)."""
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries",
         "format=duration:stream=index,codec_type,duration:stream_tags=title,handler_name"
         ":stream_disposition=attached_pic",
         "-of", "json", str(path)],
        capture_output=True, text=True, check=True,
    ).stdout
    info = json.loads(out or "{}")
    streams = info.get("streams") or []
    audio = [s for s in streams if s.get("codec_type") == "audio"]
    # Cover art in audio files is a "video" stream too; it has no meeting tiles.
    video = [s for s in streams if s.get("codec_type") == "video"
             and not (s.get("disposition") or {}).get("attached_pic")]
    duration = _seconds((info.get("format") or {}).get("duration")) or max(
        (_seconds(s.get("duration")) for s in streams), default=0.0)
    titles = [((s.get("tags") or {}).get("title") or (s.get("tags") or {}).get("handler_name") or "").strip()
              for s in audio]
    return {"duration": duration, "audio_tracks": len(audio), "video_tracks": len(video), "audio_titles": titles}


# Quorum's own call recordings (transcriber/record) keep the call and your microphone as separate tracks.
QUORUM_TRACK_TITLES = {"system audio": "system", "microphone": "mic"}


def recording_layout(src: Path) -> dict | None:
    """The <recording>.quorum.json written next to a Quorum call recording (None for any other file)."""
    side = Path(str(src) + ".quorum.json")
    try:
        data = json.loads(side.read_text())
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) and data.get("recorder") == "quorum-record" else None


def track_roles(src: Path, media: dict) -> dict[str, int]:
    """{"system": i, "mic": j}: which audio track is the call and which is your microphone, for Quorum's own
    recordings (from the layout file, else from the track titles). {} for every other recording, which keeps the
    mixed path (⇧⌘5 recordings, exports from Teams or Zoom)."""
    count = media.get("audio_tracks", 0)
    if count < 2:
        return {}
    layout = recording_layout(src)
    if layout:
        roles = {role: i for i, role in enumerate(layout.get("audio_tracks") or [])
                 if role in ("system", "mic") and i < count}
        if len(roles) == 2:
            return roles
    roles = {}
    for i, title in enumerate(media.get("audio_titles") or []):
        role = QUORUM_TRACK_TITLES.get((title or "").lower())
        if role and role not in roles:
            roles[role] = i
    return roles if len(roles) == 2 else {}


def probe_duration(path: Path) -> float:
    """A recording's length in seconds."""
    return probe_media(path)["duration"]


def audio_map_args(tracks: int) -> list[str]:
    """ffmpeg args selecting the sound: one track as is; several (e.g. mic + system audio) mixed."""
    if tracks <= 1:
        return ["-map", "0:a:0"]
    inputs = "".join(f"[0:a:{i}]" for i in range(tracks))
    return ["-filter_complex", f"{inputs}amix=inputs={tracks}:duration=longest[mix]", "-map", "[mix]"]


def extract_audio(src: Path, wav: Path, duration: float, progress: ProgressFn, tracks: int = 1,
                  track: int | None = None) -> None:
    """Stream the audio track(s) to 16 kHz mono 16-bit WAV (video never loaded in RAM). `track`: only that audio
    track (Quorum recordings: the call or the microphone), instead of all of them mixed."""
    if wav.exists() and wav.with_suffix(".done").exists():
        return
    tmp = wav.with_suffix(".partial.wav")
    proc = subprocess.Popen(
        ["ffmpeg", "-nostdin", "-y", "-loglevel", "error", "-i", str(src), "-vn",
         *(["-map", f"0:a:{track}"] if track is not None else audio_map_args(tracks)), "-ac", "1",
         "-ar", "16000", "-c:a", "pcm_s16le", "-progress", "pipe:1", str(tmp)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    for line in proc.stdout:
        if line.startswith("out_time_us=") and duration > 0:
            try:
                done = int(line.split("=", 1)[1]) / 1_000_000
                progress("extract", min(1.0, done / duration))
            except ValueError:
                pass
    if proc.wait() != 0:
        err = proc.stderr.read()
        if "does not contain any stream" in err or "matches no streams" in err:
            raise NoAudioError(NO_AUDIO_MESSAGE)
        raise RuntimeError(f"ffmpeg could not read the audio: {err[-300:]}")
    tmp.rename(wav)
    wav.with_suffix(".done").touch()


def free_disk_bytes(path: Path) -> int:
    """Free disk space where temporary files are written."""
    path.mkdir(parents=True, exist_ok=True)
    return shutil.disk_usage(path).free


def wav_bytes_for(duration: float) -> int:
    """Size of the temporary 16 kHz mono 16-bit WAV for this many seconds (to check disk space before starting)."""
    return int(duration * 16000 * 2) + 1024


# ---------------------------------------------------------------------------
# Memory handoff with Ollama
# ---------------------------------------------------------------------------

def unload_ollama_models() -> list[str]:
    """Ask Ollama to free its models before a job so Whisper has the RAM (see wait_for_ollama_idle)."""
    import requests
    unloaded = []
    try:
        running = requests.get(f"{config.OLLAMA_URL}/api/ps", timeout=3).json().get("models", [])
        for model in running:
            requests.post(f"{config.OLLAMA_URL}/api/generate",
                          json={"model": model["name"], "keep_alive": 0}, timeout=30)
            unloaded.append(model["name"])
    except Exception:
        pass  # Ollama not running: nothing to free
    return unloaded


def wait_for_ollama_idle(progress: Callable[[dict], None] | None = None, timeout: float = 300,
                         poll: float = 2.0) -> bool:
    """Free Ollama's models and wait until none is loaded (a reply still being written keeps its
    model in memory until it finishes), so Whisper and an Ollama model never share the RAM.
    True when Ollama is idle (or not running); False after `timeout` (the job then goes ahead)."""
    import requests
    deadline = time.time() + timeout
    while True:
        unload_ollama_models()
        try:
            running = requests.get(f"{config.OLLAMA_URL}/api/ps", timeout=3).json().get("models", [])
        except Exception:
            return True  # Ollama isn't running
        if not running:
            return True
        if time.time() >= deadline:
            log.warning("Ollama still holds %s; starting anyway", [m.get("name") for m in running])
            return False
        if progress:
            progress({"stage": "freeing_memory", "waiting_for": [m.get("name", "") for m in running]})
        time.sleep(poll)


# ---------------------------------------------------------------------------
# Whisper (mlx) - windowed and resumable
# ---------------------------------------------------------------------------

def _read_wav(wav: Path, start: float, end: float):
    """Reads seconds `start` to `end` of the WAV as float samples (for one diarization piece)."""
    import soundfile as sf
    return sf.read(str(wav), start=int(start * 16000), stop=int(end * 16000), dtype="float32")[0]


def quietest_point(samples, rate: int = 16000, frame_seconds: float = 0.05) -> float:
    """Offset (s) of the quietest frame in a short audio slice (pure; list or array)."""
    frame = max(1, int(rate * frame_seconds))
    best_i, best_energy = 0, None
    for i in range(0, max(1, len(samples) - frame + 1), frame):
        chunk = samples[i:i + frame]
        energy = sum(float(x) * float(x) for x in chunk) / max(1, len(chunk))
        if best_energy is None or energy < best_energy:
            best_i, best_energy = i, energy
    return (best_i + frame / 2) / rate


def snap_windows_to_silence(wav: Path, windows: list[tuple[float, float]], search: float = 3.0):
    """Move each internal boundary to the quietest moment within +/- search seconds,
    so no word is cut in half between two Whisper windows. Deterministic."""
    if len(windows) < 2:
        return windows
    cuts = []
    for _start, end in windows[:-1]:
        lo = max(0.0, end - search)
        cuts.append(lo + quietest_point(_read_wav(wav, lo, end + search)))
    edges = [windows[0][0]] + cuts + [windows[-1][1]]
    return [(round(a, 3), round(b, 3)) for a, b in zip(edges, edges[1:])]


SILENCE_RMS = 0.02          # a 50 ms frame louder than this counts as sound…
SILENCE_MIN_SECONDS = 0.5   # …and a window needs at least this much sound to be worth transcribing


def is_silent(samples, rate: int = 16000, frame_seconds: float = 0.05) -> bool:
    """True when a slice of audio has less than SILENCE_MIN_SECONDS of sound. Your microphone track is silent
    while others talk, and Whisper invents text ("Thank you.") on silence, so such windows are skipped."""
    import numpy as np

    audio = np.asarray(samples, dtype=np.float32)
    frame = max(1, int(rate * frame_seconds))
    usable = len(audio) // frame * frame
    if usable == 0:
        return True
    rms = np.sqrt(np.mean(audio[:usable].reshape(-1, frame) ** 2, axis=1))
    return float(np.sum(rms > SILENCE_RMS)) * frame_seconds < SILENCE_MIN_SECONDS


def transcribe_words(wav: Path, duration: float, work: Path, progress: ProgressFn, name: str = "whisper",
                     skip_silent: bool = False, free: bool = True) -> tuple[list[dict], str]:
    """Speech to text with Whisper (MLX on the Apple GPU), in windows of about 2 minutes cut at silences.

    Each window's words are saved as soon as it's done (`<name>_0000.json`…), so a stopped job resumes where it
    left off, and the browser can show the text so far. `skip_silent`: windows without sound aren't transcribed.
    Returns (words with times, detected language).
    """
    import mlx_whisper

    windows = snap_windows_to_silence(wav, plan_windows(duration, config.WHISPER_WINDOW_SECONDS))
    words: list[dict] = []
    language = None
    for i, (start, end) in enumerate(windows):
        progress("transcribe", i / len(windows))
        part = work / f"{name}_{i:04d}.json"
        if part.exists():
            data = json.loads(part.read_text())
        elif skip_silent and is_silent(_read_wav(wav, start, end)):
            data = {"language": None, "words": [], "silent": True}
            part.write_text(json.dumps(data))
        else:
            audio = _read_wav(wav, start, end)
            result = mlx_whisper.transcribe(
                audio, path_or_hf_repo=config.WHISPER_MODEL, word_timestamps=True,
                language=language, condition_on_previous_text=False, verbose=None,
            )
            data = {
                "language": result.get("language"),
                "words": [
                    {"word": w["word"], "start": round(w["start"] + start, 2),
                     "end": round(w["end"] + start, 2), "p": round(float(w.get("probability", 1.0)), 3)}
                    for seg in result.get("segments", []) for w in seg.get("words", [])
                ],
            }
            part.write_text(json.dumps(data))
        language = language or data.get("language")
        words.extend(data["words"])
        progress("transcribe", (i + 1) / len(windows))
    if free:
        _free_whisper()
    return words, language or "?"


def _free_whisper() -> None:
    """Releases Whisper's model and GPU memory before the next stage loads its model."""
    try:
        import mlx.core as mx
        from mlx_whisper import transcribe as mw_transcribe
        holder = getattr(mw_transcribe, "ModelHolder", None)
        if holder is not None:
            holder.model, holder.model_path = None, None
        mx.clear_cache()
    except Exception:
        pass


# ---------------------------------------------------------------------------
# pyannote diarization - pieces + voice matching, resumable
# ---------------------------------------------------------------------------

_PIPELINE = None


def diarization_available() -> bool:
    """True when every pyannote model file is cached locally (the service never downloads while running)."""
    try:
        from huggingface_hub import snapshot_download
        for repo, patterns in config.DIARIZATION_REPOS:
            snapshot_download(repo, allow_patterns=patterns, local_files_only=True)
        return True
    except Exception:
        return False


def _load_pipeline():
    """Loads the pyannote speaker pipeline once (on the Apple GPU when available) and keeps it until
    _free_pipeline().
    """
    global _PIPELINE
    if _PIPELINE is None:
        import torch
        from pyannote.audio import Pipeline
        pipeline = Pipeline.from_pretrained(config.DIARIZATION_MODEL)
        if pipeline is None:
            raise RuntimeError("Diarization model is not available offline.")
        if torch.backends.mps.is_available():
            pipeline.to(torch.device("mps"))
        _PIPELINE = pipeline
    return _PIPELINE


def _free_pipeline() -> None:
    """Releases pyannote's model and GPU memory."""
    global _PIPELINE
    _PIPELINE = None
    try:
        import gc
        import torch
        gc.collect()
        if torch.backends.mps.is_available():
            torch.mps.empty_cache()
    except Exception:
        pass


def speaker_kwargs(speakers: dict | None, pieces: int) -> dict:
    """pyannote speaker-count arguments for one piece.

    `speakers` is {"num": n} or {"min": a, "max": b} (any may be missing). A long recording is
    split into pieces and a piece may hear only some of the people, so an exact count becomes
    an upper bound there.
    """
    speakers = speakers or {}
    num, low, high = speakers.get("num"), speakers.get("min"), speakers.get("max")
    if num:
        return {"num_speakers": num} if pieces == 1 else {"max_speakers": num}
    out = {}
    if low and pieces == 1:
        out["min_speakers"] = low
    if high:
        out["max_speakers"] = high
    return out


def _diarize_piece(wav: Path, start: float, end: float,
                   kwargs: dict | None = None) -> tuple[list[dict], dict[str, list[float]]]:
    """Runs speaker labelling on one piece of the audio; returns its speaker turns and a voice fingerprint per
    speaker (used to match speakers across pieces).
    """
    import numpy as np
    import torch

    pipeline = _load_pipeline()
    audio = _read_wav(wav, start, end)
    output = pipeline({"waveform": torch.from_numpy(np.ascontiguousarray(audio))[None],
                       "sample_rate": 16000}, **(kwargs or {}))
    if hasattr(output, "speaker_diarization"):  # pyannote >= 4
        annotation = getattr(output, "exclusive_speaker_diarization", None) or output.speaker_diarization
        embeddings = getattr(output, "speaker_embeddings", None)
    else:  # pyannote 3.x
        annotation, embeddings = output, None
    labels = sorted(annotation.labels())
    turns = [
        {"start": round(seg.start + start, 2), "end": round(seg.end + start, 2), "speaker": label}
        for seg, _track, label in annotation.itertracks(yield_label=True)
    ]
    vectors: dict[str, list[float]] = {}
    for idx, label in enumerate(labels):
        vec = None
        if embeddings is not None and idx < len(embeddings):
            vec = [float(x) for x in embeddings[idx]]
            if any(math.isnan(x) for x in vec):
                vec = None
        vectors[label] = vec
    return turns, vectors


def diarize(wav: Path, duration: float, work: Path, progress: ProgressFn,
            speakers: dict | None = None, vectors_out: dict | None = None) -> list[dict]:
    """Speaker turns; `vectors_out` (optional) receives one voice fingerprint per speaker."""
    pieces_plan = plan_diarization_chunks(duration)
    kwargs = speaker_kwargs(speakers, len(pieces_plan))
    hint = hashlib.sha1(json.dumps(kwargs, sort_keys=True).encode()).hexdigest()[:6]
    known: dict[str, list[float]] = {}
    pieces = []
    for i, (start, end) in enumerate(pieces_plan):
        part = work / f"diarize_{hint}_{i:04d}.json"  # a different speaker count re-runs it
        if part.exists():
            data = json.loads(part.read_text())
        else:
            turns, vectors = _diarize_piece(wav, start, end, kwargs)
            data = {"turns": turns, "vectors": vectors}
            part.write_text(json.dumps(data))
        if len(pieces_plan) == 1:
            mapping = {label: label for label in data["vectors"]} or {}
            known = {k: v for k, v in data["vectors"].items() if v}
        else:
            mapping = match_speakers(known, {k: v for k, v in data["vectors"].items() if v})
        turns = [dict(t, speaker=mapping.get(t["speaker"], t["speaker"])) for t in data["turns"]]
        pieces.append({"start": start, "end": end, "turns": turns})
        progress("diarize", (i + 1) / len(pieces_plan))
    _free_pipeline()
    if vectors_out is not None:
        vectors_out.update(known)
    return stitch_turns(pieces)


def _renumber(segments: list[dict]) -> list[dict]:
    """SPEAKER_00, SPEAKER_01... in order of first appearance."""
    order = {spk: f"SPEAKER_{i:02d}" for i, spk in enumerate(speaker_order(segments))}
    return [dict(s, speaker=order[s["speaker"]]) for s in segments]


# ---------------------------------------------------------------------------
# Whole job
# ---------------------------------------------------------------------------

def stage_reporter(progress: Callable[[dict], None], skipped: set[str]) -> ProgressFn:
    """Whole-job progress from per-stage fractions; skipped stages (no speaker models, no video)
    don't count, so the bar still ends at 100%."""
    weights = {k: (0.0 if k in skipped else v) for k, v in STAGE_WEIGHTS.items()}
    total_weight = sum(weights.values()) or 1.0
    order = list(STAGE_WEIGHTS)

    def report(stage: str, fraction: float) -> None:
        """Reports progress inside one stage as overall job progress (0..1)."""
        done = sum(weights[s] for s in order[: order.index(stage)])
        total = (done + weights[stage] * fraction) / total_weight
        progress({"stage": stage, "progress": round(min(1.0, total), 4)})

    return report


def run(src: Path, progress: Callable[[dict], None], speakers: dict | None = None,
        screen: dict | None = None, models: dict | None = None) -> dict:
    """Transcribe one recording; returns the saved transcript dict.
    `screen`: on-screen name options {"enabled", "vision_model", "vision_cap", "debug"}.
    `models`: the meeting's model choices (speaker identification, video frames, summary), kept
    with the transcript for the app; the transcriber itself only uses the frames model."""
    started = time.time()
    sid = source_id(src)
    work = config.WORK_DIR / sid
    resumed = work.exists() and any(work.glob("whisper_*.json"))  # partial timing: don't calibrate
    work.mkdir(parents=True, exist_ok=True)
    media = probe_media(src)
    if not media["audio_tracks"]:
        raise NoAudioError(NO_AUDIO_MESSAGE)
    duration = media["duration"]
    use_diarization = diarization_available()
    screen = dict(screen or {})
    has_video = bool(media.get("video_tracks"))
    skipped = ({"diarize"} if not use_diarization else set()) \
        | ({"screen"} if not (has_video and screen.get("enabled", True)) else set())
    report = stage_reporter(progress, skipped)

    wait_for_ollama_idle(progress)
    roles = track_roles(src, media)
    report("extract", 0)
    confirmed: dict[str, dict] = {}
    if roles:
        tracks = transcribe_call_tracks(src, duration, work, report, roles, speakers, use_diarization)
        segments, language, embeddings, diarization_error = (tracks["segments"], tracks["language"],
                                                             tracks["embeddings"], tracks["diarization_error"])
        words = tracks["words"]
        confirmed = tracks["confirmed"]
        use_diarization = True  # your track and the call are always told apart
    else:
        wav = work / "audio.wav"
        extract_audio(src, wav, duration, report, media["audio_tracks"])
        words, language = transcribe_words(wav, duration, work, report)

        diarization_error = None
        vectors: dict[str, list[float]] = {}
        if use_diarization:
            try:
                turns = diarize(wav, duration, work, report, speakers, vectors)
                words = assign_speakers(words, turns)
            except JobCancelled:
                raise
            except Exception as exc:  # keep the finished transcript rather than failing the job
                log.exception("Speaker labelling failed; saving transcript without labels")
                diarization_error = f"{type(exc).__name__}: {str(exc)[:200]}"
                use_diarization = False
        if not use_diarization:
            words = [dict(w, speaker="SPEAKER_00") for w in words]

        grouped = group_segments(words)
        order = {spk: f"SPEAKER_{i:02d}" for i, spk in enumerate(speaker_order(grouped))}
        segments = [dict(seg, speaker=order[seg["speaker"]]) for seg in grouped]
        embeddings = {order[spk]: vec for spk, vec in vectors.items() if spk in order and vec}
    screen_found, screen_info = read_screen_names(src, segments, work, report, screen, has_video)
    report("finish", 0.5)
    elapsed = time.time() - started
    transcript = {
        "id": sid,
        "title": src.stem,
        "source_name": src.name,
        "source_path": str(src),
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "duration": duration,
        "language": language,
        "diarization": use_diarization,
        "diarization_error": diarization_error,
        "speaker_hint": speakers or None,
        "models": models or {},            # the meeting's model choices (set in New meeting)
        "speakers": speaker_order(segments),
        "speaker_names": {},
        "speaker_embeddings": embeddings,  # voice fingerprints: recognise people across meetings
        "screen_names": screen_found,      # names on the active speaker's tile, per speaker label
        "screen_info": screen_info,
        "segments": segments,
        "processing_seconds": round(elapsed),
    }
    if roles:
        transcript["audio_tracks"] = {"mic": True, "system": True}
        transcript["confirmed_speakers"] = confirmed  # {label: {"source": "microphone"}}: you, on your mic
        layout = recording_layout(src) or {}
        transcript["recorded_by_quorum"] = {"source": layout.get("source"), "started_at": layout.get("started_at")}
    save_transcript(transcript)
    save_words(sid, words)
    if duration > 60 and not resumed and not diarization_error:
        # Two Whisper passes (call + mic): scale back to the one-track rate used for estimates.
        save_speed_factor(elapsed / duration / (CALL_TRACKS_COST if roles else 1.0), use_diarization)
    report("finish", 1.0)
    shutil.rmtree(work, ignore_errors=True)  # temporary WAV + per-window parts
    return transcript


CALL_TRACKS_COST = 1.5  # a Quorum call recording: Whisper on the call and on your mic (mostly silent, skipped)


def transcribe_call_tracks(src: Path, duration: float, work: Path, report: ProgressFn, roles: dict[str, int],
                           speakers: dict | None, use_diarization: bool) -> dict:
    """A Quorum call recording: the call and your microphone are transcribed separately, the call leaking into
    your mic is dropped, speaker separation runs on the call only (everyone but you), and your words get one
    label of their own, marked confirmed. Returns {segments, language, embeddings, confirmed, diarization_error}.
    """
    system_wav, mic_wav = work / "system.wav", work / "mic.wav"
    extract_audio(src, system_wav, duration, lambda st, f: report(st, f * 0.5), track=roles["system"])
    extract_audio(src, mic_wav, duration, lambda st, f: report(st, 0.5 + f * 0.5), track=roles["mic"])
    system_words, language = transcribe_words(system_wav, duration, work, lambda st, f: report(st, f * 0.65),
                                              name="whisper_system", free=False)
    mic_words, mic_language = transcribe_words(mic_wav, duration, work, lambda st, f: report(st, 0.65 + f * 0.35),
                                               name="whisper_mic", skip_silent=True)
    mic_words = drop_echo(mic_words, system_words)

    diarization_error = None
    vectors: dict[str, list[float]] = {}
    if use_diarization and system_words:
        try:
            turns = diarize(system_wav, duration, work, report, others_hint(speakers), vectors)
            system_words = assign_speakers(system_words, turns)
        except JobCancelled:
            raise
        except Exception as exc:  # keep your track and the call apart even without pyannote
            log.exception("Speaker labelling of the call failed; keeping the call as one speaker")
            diarization_error = f"{type(exc).__name__}: {str(exc)[:200]}"
            use_diarization = False
    if not use_diarization:
        system_words = [dict(w, speaker="CALL") for w in system_words]

    segments, order = merge_tracks(mic_words, system_words)
    mine = order.get(MIC_SPEAKER)
    embeddings = {order[spk]: vec for spk, vec in vectors.items() if spk in order and vec}
    if mine and use_diarization and diarization_available():
        try:
            vec = voice_fingerprint(mic_wav, segments, mine)
            if vec:
                embeddings[mine] = vec
        except Exception:
            log.exception("Couldn't make a voice fingerprint from the microphone")
    return {"segments": segments, "language": language if language != "?" else mic_language,
            "embeddings": embeddings, "confirmed": {mine: {"source": "microphone"}} if mine else {},
            "diarization_error": diarization_error, "words": mic_words + system_words}


def voice_fingerprint(wav: Path, segments: list[dict], speaker: str) -> list[float] | None:
    """One voice fingerprint for `speaker` from their longest turns (pyannote's embedding model)."""
    import numpy as np
    import torch

    try:
        embed = _load_pipeline()._embedding
        found = []
        for start, end in backfill_crops(segments, speaker):
            audio = _read_wav(wav, start, end)
            if len(audio) < 16000:
                continue
            batch = torch.from_numpy(np.ascontiguousarray(audio, dtype=np.float32))[None, None]
            found.append([float(x) for x in embed(batch)[0]])
        return mean_vector(found)
    finally:
        _free_pipeline()


def words_file(tid: str) -> Path:
    """Whisper's words with times and probabilities for a transcript (kept for corrections; not in the index)."""
    return config.TRANSCRIPTS_DIR / ".words" / f"{tid}.json"


def save_words(tid: str, words: list[dict]) -> None:
    """Keeps `{word, start, end, p}` for each word (the transcript itself only keeps segments)."""
    path = words_file(tid)
    path.parent.mkdir(parents=True, exist_ok=True)
    slim = sorted(({"word": w["word"], "start": w["start"], "end": w["end"], "p": w.get("p")} for w in words),
                  key=lambda w: w["start"])
    path.write_text(json.dumps(slim, ensure_ascii=False))


def load_words(tid: str) -> list[dict] | None:
    """The saved words, or None for transcripts made before they were kept."""
    try:
        return json.loads(words_file(tid).read_text())
    except (OSError, ValueError):
        return None


def save_transcript(transcript: dict) -> None:
    """Saves the transcript as JSON (for the app) and Markdown (for people)."""
    config.TRANSCRIPTS_DIR.mkdir(parents=True, exist_ok=True)
    base = config.TRANSCRIPTS_DIR / transcript["id"]
    base.with_suffix(".json").write_text(json.dumps(transcript, ensure_ascii=False, indent=1))
    base.with_suffix(".md").write_text(to_markdown(transcript), encoding="utf-8")


# ---------------------------------------------------------------------------
# On-screen names and voice fingerprints for finished transcripts
# ---------------------------------------------------------------------------

def read_screen_names(src: Path, segments: list[dict], work: Path, report: ProgressFn,
                      options: dict | None, has_video: bool) -> tuple[dict, dict]:
    """The on-screen name stage. Runs after Whisper and pyannote released their memory; a failure
    here never loses the transcript."""
    from transcriber import screen_names

    options = options or {}
    if not has_video or not options.get("enabled", True):
        return screen_names.run(src, segments, work, lambda f: None, options, has_video=has_video)
    try:
        return screen_names.run(src, segments, work, lambda f: report("screen", f), options)
    except JobCancelled:
        raise
    except Exception as exc:
        log.exception("On-screen names failed; keeping the transcript without them")
        return {}, {"status": "error", "error": f"{type(exc).__name__}: {str(exc)[:200]}"}


def load_transcript(tid: str) -> dict:
    """Reads a saved transcript by id."""
    return json.loads((config.TRANSCRIPTS_DIR / f"{tid}.json").read_text())


def recording_for(transcript: dict) -> Path | None:
    """The original recording of a transcript if it still exists (None for imported text or a deleted file)."""
    path = Path(transcript.get("source_path") or "")
    return path if transcript.get("source_type", "audio") != "text" and path.is_file() else None


def run_reference(tid: str, src: Path, progress: Callable[[dict], None]) -> list[dict]:
    """Transcribes another recording of the meeting (e.g. a Voice Memo) for correcting the transcript: Whisper
    words with times only (no speakers). Returns the words."""
    media = probe_media(src)
    if not media["audio_tracks"]:
        raise NoAudioError(NO_AUDIO_MESSAGE)
    duration = media["duration"]
    work = config.WORK_DIR / f"ref-{tid}-{source_id(src)}"
    work.mkdir(parents=True, exist_ok=True)
    report = stage_reporter(progress, {"diarize", "screen"})
    wait_for_ollama_idle(progress)
    try:
        wav = work / "audio.wav"
        extract_audio(src, wav, duration, report, media["audio_tracks"])
        words, _language = transcribe_words(wav, duration, work, report, name="whisper_ref")
    finally:
        shutil.rmtree(work, ignore_errors=True)
    report("finish", 1.0)
    return words


def run_screen_only(tid: str, progress: Callable[[dict], None], options: dict | None = None) -> dict:
    """Read on-screen names for a transcript made earlier (no Whisper or pyannote re-run)."""
    transcript = load_transcript(tid)
    src = recording_for(transcript)
    if src is None:
        raise FileNotFoundError("The recording for this transcript is no longer available.")
    media = probe_media(src)
    work = config.WORK_DIR / f"screen-{tid}"
    work.mkdir(parents=True, exist_ok=True)
    try:
        report = stage_reporter(progress, {"extract", "transcribe", "diarize"})
        found, info = read_screen_names(src, transcript["segments"], work, report,
                                        dict(options or {}, enabled=True), bool(media.get("video_tracks")))
    finally:
        shutil.rmtree(work, ignore_errors=True)
    transcript = load_transcript(tid)  # it may have been renamed meanwhile
    transcript["screen_names"], transcript["screen_info"] = found, info
    save_transcript(transcript)
    report("finish", 1.0)
    return transcript


BACKFILL_MAX_CROPS = 12
BACKFILL_CROP_SECONDS = 10.0


def backfill_crops(segments: list[dict], speaker: str) -> list[tuple[float, float]]:
    """The speaker's longest turns, cut to at most 10 s each: enough speech for a fingerprint."""
    turns = sorted((s for s in segments if s.get("speaker") == speaker and s["end"] - s["start"] >= 2.0),
                   key=lambda s: s["end"] - s["start"], reverse=True)[:BACKFILL_MAX_CROPS]
    return [(s["start"], min(s["end"], s["start"] + BACKFILL_CROP_SECONDS)) for s in turns]


def mean_vector(vectors: list[list[float]]) -> list[float] | None:
    """The average direction of several voice fingerprints (each scaled to length 1 first); None when there are
    no usable vectors.
    """
    vectors = [v for v in vectors if v and not any(math.isnan(x) for x in v)]
    if not vectors:
        return None
    units = []
    for v in vectors:
        norm = math.sqrt(sum(x * x for x in v)) or 1.0
        units.append([x / norm for x in v])
    return [sum(col) / len(units) for col in zip(*units)]


def backfill_voiceprints(tid: str, progress: Callable[[dict], None]) -> dict:
    """Voice fingerprints for an older transcript, from its recording: the pyannote embedding model
    only (the same one diarization uses), no Whisper re-run. Speaker labels stay as they are."""
    import numpy as np
    import torch

    transcript = load_transcript(tid)
    src = recording_for(transcript)
    if src is None:
        raise FileNotFoundError("The recording for this transcript is no longer available.")
    if not diarization_available():
        raise RuntimeError("The speaker models aren't downloaded, so voice fingerprints can't be made.")
    media = probe_media(src)
    if not media["audio_tracks"]:
        raise NoAudioError(NO_AUDIO_MESSAGE)
    unload_ollama_models()
    work = config.WORK_DIR / f"voice-{tid}"
    work.mkdir(parents=True, exist_ok=True)
    report = stage_reporter(progress, {"transcribe", "diarize", "screen"})
    try:
        wav = work / "audio.wav"
        extract_audio(src, wav, media["duration"], report, media["audio_tracks"])
        embed = _load_pipeline()._embedding
        speakers = [s for s in dict.fromkeys(seg["speaker"] for seg in transcript["segments"]) if s]
        vectors: dict[str, list[float]] = {}
        for i, spk in enumerate(speakers):
            found = []
            for start, end in backfill_crops(transcript["segments"], spk):
                audio = _read_wav(wav, start, end)
                if len(audio) < 16000:
                    continue
                batch = torch.from_numpy(np.ascontiguousarray(audio, dtype=np.float32))[None, None]
                found.append([float(x) for x in embed(batch)[0]])
            vec = mean_vector(found)
            if vec:
                vectors[spk] = vec
            report("finish", (i + 1) / max(1, len(speakers)) * 0.9)
    finally:
        _free_pipeline()
        shutil.rmtree(work, ignore_errors=True)
    transcript = load_transcript(tid)
    transcript["speaker_embeddings"] = {**(transcript.get("speaker_embeddings") or {}), **vectors}
    transcript["voiceprints_backfilled"] = datetime.now().isoformat(timespec="seconds")
    save_transcript(transcript)
    report("finish", 1.0)
    return transcript
