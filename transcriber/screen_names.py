"""On-screen names: read who the meeting app shows as the active speaker, offline.

Teams and Zoom put each participant's name on their video tile and draw a coloured border
around the tile of whoever is talking. For each diarized speaker we sample a few frames at
the start of their turns, find the highlighted tile and read its name label with Apple's
on-device text recognition (transcriber/ocr/quorum_ocr.swift, Vision framework).

Frames the text reader can't settle (no highlight found, or several highlighted tiles) may
go to a local Ollama vision model, which is asked only to read the name label. It's never
asked about faces. Frames never leave the Mac.

Runs after Whisper and pyannote have finished and released their memory. The result is
saved on the transcript as `screen_names` (per speaker label: names seen, counts, times)
and `screen_info` (what happened). Pure helpers are unit-tested without the OCR helper.
"""

from __future__ import annotations

import base64
import json
import logging
import re
import shutil
import subprocess
import time
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable

from transcriber import config

log = logging.getLogger(__name__)

HERE = Path(__file__).resolve().parent
OCR_SOURCE = HERE / "ocr" / "quorum_ocr.swift"
OCR_BINARY = HERE / "bin" / "quorum-ocr"

PER_SPEAKER_TURNS = 10        # turns sampled per speaker
MAX_FRAMES = 240              # per meeting
MIN_TURN_SECONDS = 1.5        # shorter turns: the highlight may still be on the previous speaker
FRAME_WIDTH = 1280
VISION_CAP = 12               # vision-model frames per meeting (default)
SECONDS_PER_FRAME = 0.35      # extract + OCR, measured on the M4 Air
SECONDS_PER_VISION_FRAME = 9.0  # Qwen3-VL 4B on the M4 Air (benchmark_vision_models.py)
SCREEN_SHARE_LINES = 14       # this much text and no tiles: a shared screen fills the view

# Labels the meeting apps add to names, and words that are UI, not people.
_SUFFIX = re.compile(r"\s*(?:\((?:guest|external|unverified|host|co-host|organi[sz]er|presenter|you|me|"
                     r"he/him|she/her|they/them)\)|[|•·-]\s*(?:presenter|organi[sz]er|host|guest))\s*$", re.I)
_UI_WORDS = {
    "mute", "unmute", "muted", "share", "sharing", "leave", "chat", "recording", "record", "people", "react",
    "view", "more", "camera", "mic", "microphone", "raise", "hand", "meeting", "participants", "gallery",
    "speaker", "join", "live", "captions", "rec", "stop", "pin", "spotlight", "zoom", "teams", "microsoft",
    "screen", "presenting", "is", "presentation", "start", "video", "audio", "apps", "rooms", "whiteboard",
    "reactions", "notes", "transcript", "settings", "end", "call", "calling", "connecting", "waiting",
    "lobby", "admit", "you", "your", "the", "and", "with", "for",
}
_NAME_TOKEN = re.compile(r"^[^\W\d_][^\W\d_'’.\-]*(?:['’.\-][^\W\d_]+)*\.?$", re.UNICODE)
_ELLIPSIS = re.compile(r"\s*(?:…|\.\.\.)\s*$")


# ---------------------------------------------------------------------------
# Names
# ---------------------------------------------------------------------------

def clean_name(text: str) -> tuple[str, bool] | None:
    """(name, truncated) when `text` looks like a participant name label, else None."""
    text = " ".join(str(text or "").replace("​", "").split())
    text = re.sub(r"^[^\w(]+", "", text, flags=re.UNICODE)  # mic / pin icons read as symbols
    for _ in range(2):
        text = _SUFFIX.sub("", text).strip()
    truncated = bool(_ELLIPSIS.search(text))
    text = _ELLIPSIS.sub("", text).strip(" ,;:|")
    tokens = text.split()
    if not 1 <= len(tokens) <= 5 or not 2 <= len(text) <= 60:
        return None
    if any(not _NAME_TOKEN.match(t) for t in tokens):
        return None
    if not any(t[0].isupper() for t in tokens):
        return None
    if all(t.lower().strip(".") in _UI_WORDS for t in tokens) or tokens[0].lower() in _UI_WORDS:
        return None
    if len(tokens) == 1 and (len(text) < 3 or text.isupper() and len(text) > 3):
        return None  # "AB" initials avatars, "MUTED"
    return text, truncated


def same_person(short: str, full: str) -> bool:
    """A truncated label ("Priya Shar") is the start of the full one ("Priya Sharma")."""
    return len(short) >= 4 and full.lower().startswith(short.lower()) and len(full) > len(short)


# ---------------------------------------------------------------------------
# Highlighted tiles (numpy only)
# ---------------------------------------------------------------------------

def load_rgb(path: Path, width: int = 640):
    """A video frame as an RGB pixel array (numpy), scaled down to `width` for speed."""
    import numpy as np
    from PIL import Image

    with Image.open(path) as im:
        im = im.convert("RGB")
        if im.width > width:
            im = im.resize((width, max(1, round(im.height * width / im.width))))
        return np.asarray(im, dtype=np.float32) / 255.0


def highlight_mask(rgb, white: bool = False):
    """Vivid pixels: the coloured border meeting apps draw (Teams purple, Zoom green/yellow).
    `white`: near-white pixels instead (newer Teams versions draw a white/grey border)."""
    hi = rgb.max(axis=2)
    lo = rgb.min(axis=2)
    sat = (hi - lo) / (hi + 1e-6)
    if white:
        return (sat <= 0.12) & (hi >= 0.85)
    return (sat >= 0.33) & (hi >= 0.40)


def _runs(row) -> list[tuple[int, int]]:
    """Start and end of each run of True values in a row of pixels, e.g. [F,T,T,F,T] -> [(1,3), (4,5)]."""
    import numpy as np

    padded = np.concatenate([[False], row, [False]])
    edges = np.flatnonzero(padded[1:] != padded[:-1])
    return list(zip(edges[::2], edges[1::2]))


def _lines(mask, min_len: int, tol: int) -> list[dict]:
    """Horizontal border lines: runs of vivid pixels repeated on a few adjacent rows."""
    lines: list[dict] = []
    for y in range(mask.shape[0]):
        for x0, x1 in _runs(mask[y]):
            if x1 - x0 < min_len:
                continue
            for line in lines:
                if y - line["y1"] <= 1 and abs(line["x0"] - x0) <= tol and abs(line["x1"] - x1) <= tol:
                    line["y1"] = y
                    break
            else:
                lines.append({"y0": y, "y1": y, "x0": int(x0), "x1": int(x1)})
    return lines


def find_highlighted_tiles(rgb, white: bool = False) -> list[tuple[float, float, float, float]]:
    """Rectangles (x0, y0, x1, y1, normalised 0..1) outlined by a vivid (or, with `white`, a
    near-white) border."""
    mask = highlight_mask(rgb, white)
    h, w = mask.shape
    tol = max(3, round(0.02 * w))
    max_thick = max(3, round(0.04 * h))
    lines = [ln for ln in _lines(mask, round(0.08 * w), tol) if ln["y1"] - ln["y0"] + 1 <= max_thick]
    found = []
    for i, top in enumerate(lines):
        for bottom in lines[i + 1:]:
            if bottom["y0"] - top["y1"] < 0.08 * h:
                continue
            if abs(top["x0"] - bottom["x0"]) > tol or abs(top["x1"] - bottom["x1"]) > tol:
                continue
            x0, x1 = min(top["x0"], bottom["x0"]), max(top["x1"], bottom["x1"])
            y0, y1 = top["y0"], bottom["y1"]
            band = max(2, top["y1"] - top["y0"] + 2)
            left = mask[y0:y1, x0:x0 + band].any(axis=1).mean()
            right = mask[y0:y1, max(x0, x1 - band):x1].any(axis=1).mean()
            inner = mask[y0 + band:y1 - band, x0 + band:x1 - band]
            if left < 0.7 or right < 0.7 or (inner.size and inner.mean() > 0.5):
                continue
            found.append((x0 / w, y0 / h, x1 / w, y1 / h))
    out: list[tuple[float, float, float, float]] = []
    for rect in sorted(found, key=lambda r: (r[2] - r[0]) * (r[3] - r[1])):
        if not any(_inside(rect, o) or _inside(o, rect) for o in out):
            out.append(rect)
    return out


def _inside(a, b, slack: float = 0.01) -> bool:
    """True when box `a` lies inside box `b` (boxes are (left, top, right, bottom) fractions)."""
    return a[0] >= b[0] - slack and a[1] >= b[1] - slack and a[2] <= b[2] + slack and a[3] <= b[3] + slack


def name_in_tile(lines: list[dict], rect) -> tuple[str, bool] | None:
    """The name label of a tile: name-like text in the tile's lower part (or just below it)."""
    x0, y0, x1, y1 = rect
    height = y1 - y0
    best, best_score = None, -1.0
    for line in lines:
        bx, by, bw, bh = line["box"]
        cx, cy = bx + bw / 2, by + bh / 2
        if not (x0 <= cx <= x1 and y0 + 0.55 * height <= cy <= y1 + 0.06):
            continue
        name = clean_name(line["text"])
        if not name:
            continue
        score = float(line.get("confidence", 0.5)) + (0.2 if cy <= y1 else 0) + (0.1 if bx < x0 + (x1 - x0) / 2 else 0)
        if score > best_score:
            best, best_score = name, score
    return best


def speaker_view_name(lines: list[dict]) -> tuple[str, bool] | None:
    """Speaker view (one large video, no border): the single name label at the bottom left."""
    if len(lines) > 6:
        return None
    named = [(ln, clean_name(ln["text"])) for ln in lines]
    named = [(ln, n) for ln, n in named if n]
    if len(named) != 1:  # gallery view without a highlight shows every participant's name
        return None
    line, name = named[0]
    return name if line["box"][1] + line["box"][3] / 2 >= 0.75 and line["box"][0] < 0.5 else None


@dataclass
class FrameResult:
    """What was found in one sampled frame: time, speaker label, status (read by text, speaker view, vision
    model, screen share, unresolved…), the name and the tiles.
    """
    t: float
    speaker: str
    status: str                       # ocr | speaker_view | vision | screen_share | unresolved | ambiguous | error
    name: str = ""
    truncated: bool = False
    tiles: list = field(default_factory=list)
    path: str = ""


def read_frame(rgb, lines: list[dict]) -> tuple[str, tuple[str, bool] | None, list]:
    """(status, (name, truncated) or None, highlighted tiles) for one frame."""
    tiles = find_highlighted_tiles(rgb)
    named = [n for n in (clean_name(ln["text"]) for ln in lines) if n]
    if not tiles and len(lines) < SCREEN_SHARE_LINES and len(named) >= 2:
        # No coloured border: a white one around one of several participant tiles. Only when the
        # frame has little text, so the lines of a shared slide or table aren't taken for a tile.
        tiles = [t for t in find_highlighted_tiles(rgb, white=True) if name_in_tile(lines, t)]
    if tiles:
        names = [n for n in (name_in_tile(lines, r) for r in tiles) if n]
        distinct = {n[0].lower() for n in names}
        if len(distinct) == 1:
            return "ocr", names[0], tiles
        return ("ambiguous" if len(tiles) > 1 or len(distinct) > 1 else "unresolved"), None, tiles
    if len(lines) >= SCREEN_SHARE_LINES:
        return "screen_share", None, tiles
    found = speaker_view_name(lines)
    if found:
        return "speaker_view", found, tiles
    return "unresolved", None, tiles


# ---------------------------------------------------------------------------
# Sampling and aggregation
# ---------------------------------------------------------------------------

def plan_samples(segments: list[dict], per_speaker: int = PER_SPEAKER_TURNS,
                 max_frames: int = MAX_FRAMES) -> list[tuple[float, str]]:
    """(time, speaker) pairs: a frame early in a turn (and one mid-turn for long turns), spread
    evenly over each speaker's turns."""
    by: dict[str, list[dict]] = defaultdict(list)
    for seg in segments:
        if seg.get("end", 0) - seg.get("start", 0) >= MIN_TURN_SECONDS and seg.get("speaker"):
            by[seg["speaker"]].append(seg)
    samples: list[tuple[float, str]] = []
    for spk, segs in by.items():
        if len(segs) > per_speaker:
            step = len(segs) / per_speaker
            segs = [segs[int(i * step)] for i in range(per_speaker)]
        for seg in segs:
            length = seg["end"] - seg["start"]
            samples.append((round(seg["start"] + min(1.5, length * 0.3), 2), spk))
            if length >= 8:
                samples.append((round(seg["start"] + length / 2, 2), spk))
    samples.sort()
    if len(samples) > max_frames:
        step = len(samples) / max_frames
        samples = [samples[int(i * step)] for i in range(max_frames)]
    return samples


def estimate_frames(duration: float) -> int:
    """Frames a meeting of this length usually needs (before the speaker turns are known)."""
    return int(min(MAX_FRAMES, max(4, duration / 15)))


def estimate_seconds(duration: float, vision_cap: int = 0, vision_seconds: float | None = None) -> dict:
    """`vision_seconds`: the chosen vision model's time per frame (default: Qwen3-VL 4B's)."""
    frames = estimate_frames(duration)
    low = frames * SECONDS_PER_FRAME
    per_frame = vision_seconds or SECONDS_PER_VISION_FRAME
    return {"frames": frames, "low": round(low), "high": round(low * 1.5 + vision_cap * per_frame)}


def aggregate(results: Iterable[FrameResult]) -> dict[str, dict]:
    """Per speaker: the names seen (truncated labels folded into the full name), with counts."""
    seen: dict[str, dict[str, dict]] = defaultdict(dict)
    frames: Counter = Counter()
    resolved: Counter = Counter()
    for r in results:
        frames[r.speaker] += 1
        if not r.name:
            continue
        resolved[r.speaker] += 1
        entry = seen[r.speaker].setdefault(r.name, {"name": r.name, "count": 0, "times": [], "truncated": r.truncated,
                                                    "ocr": 0, "speaker_view": 0, "vision": 0})
        entry["count"] += 1
        entry[r.status] = entry.get(r.status, 0) + 1
        entry["truncated"] = entry["truncated"] and r.truncated
        if len(entry["times"]) < 12:
            entry["times"].append(r.t)
    out: dict[str, dict] = {}
    all_full = {n for names in seen.values() for n, e in names.items() if not e["truncated"]}
    for spk in frames:
        names = seen.get(spk, {})
        for short in [n for n, e in names.items() if e["truncated"]]:
            full = sorted(f for f in all_full if same_person(short, f))
            if len(full) == 1:
                target = names.setdefault(full[0], {"name": full[0], "count": 0, "times": [], "truncated": False,
                                                    "ocr": 0, "speaker_view": 0, "vision": 0})
                src = names.pop(short)
                for key in ("count", "ocr", "speaker_view", "vision"):
                    target[key] += src[key]
                target["times"] = sorted(target["times"] + src["times"])[:12]
        ranked = sorted(names.values(), key=lambda e: -e["count"])
        out[spk] = {"names": ranked, "frames": frames[spk], "resolved": resolved[spk]}
    return out


def fmt_time(seconds: float) -> str:
    """A time position as 'm:ss' (or 'h:mm:ss')."""
    seconds = int(seconds)
    return f"{seconds // 3600}:{seconds % 3600 // 60:02d}:{seconds % 60:02d}" if seconds >= 3600 \
        else f"{seconds // 60}:{seconds % 60:02d}"


# ---------------------------------------------------------------------------
# Media, OCR helper, vision model
# ---------------------------------------------------------------------------

def ocr_binary() -> Path | None:
    """The compiled OCR helper; built with swiftc the first time if missing."""
    if OCR_BINARY.exists() and OCR_BINARY.stat().st_mtime >= OCR_SOURCE.stat().st_mtime:
        return OCR_BINARY
    swiftc = shutil.which("swiftc")
    if not swiftc:
        return None
    OCR_BINARY.parent.mkdir(parents=True, exist_ok=True)
    result = subprocess.run([swiftc, "-O", str(OCR_SOURCE), "-o", str(OCR_BINARY)],
                            capture_output=True, text=True, timeout=600)
    if result.returncode != 0:
        log.warning("Building the OCR helper failed: %s", result.stderr[-500:])
        return None
    return OCR_BINARY


def run_ocr(paths: list[Path], on_line: Callable[[], None] | None = None) -> dict[str, list[dict]]:
    """{path: text lines} for each frame, from one OCR helper process."""
    binary = ocr_binary()
    if binary is None:
        raise RuntimeError("Text recognition isn't available (needs Xcode command line tools for swiftc).")
    out: dict[str, list[dict]] = {}
    for start in range(0, len(paths), 40):
        batch = paths[start:start + 40]
        proc = subprocess.Popen([str(binary), *map(str, batch)], stdout=subprocess.PIPE, text=True)
        assert proc.stdout is not None
        for raw in proc.stdout:
            try:
                data = json.loads(raw)
            except ValueError:
                continue
            out[data.get("path", "")] = data.get("lines") or []
            if on_line:
                try:
                    on_line()
                except BaseException:
                    proc.kill()
                    raise
        proc.wait(timeout=60)
    return out


def extract_frame(src: Path, t: float, dest: Path) -> bool:
    """Saves the video frame at time `t` as a JPEG with ffmpeg. True on success."""
    result = subprocess.run(
        ["ffmpeg", "-v", "error", "-y", "-ss", f"{t:.2f}", "-i", str(src), "-frames:v", "1",
         "-vf", f"scale='min({FRAME_WIDTH},iw)':-2", "-q:v", "3", str(dest)],
        capture_output=True, timeout=60)
    return result.returncode == 0 and dest.exists()


VISION_PROMPT = (
    "This is one frame from a video-call recording (for example Microsoft Teams or Zoom). "
    "Only read text in the image. Do not describe, recognise or identify anyone's face or appearance.\n"
    "1. Does a shared screen or presentation fill most of the view? \n"
    "2. Is exactly one participant tile highlighted as the active speaker (a coloured border around it)?\n"
    "3. If so, what is the name label written on that highlighted tile, exactly as shown?\n"
    'Reply with JSON only: {"screen_share": true or false, "highlighted": true or false, '
    '"name": "the label text" or null}'
)


def vision_prompt(candidates: list[str]) -> str:
    """The question for one frame; with the labels the text reader found, the model only picks one."""
    if not candidates:
        return VISION_PROMPT
    return (VISION_PROMPT.rsplit("\nReply", 1)[0]
            + "\nThe name labels visible in this frame are: " + "; ".join(candidates)
            + ". If a tile is highlighted, the name must be one of these, written exactly as listed."
            + '\nReply with JSON only: {"screen_share": true or false, "highlighted": true or false, '
              '"name": "one of the listed names" or null}')


def ask_vision(model: str, image: Path, ollama_url: str | None = None, timeout: float = 120,
               candidates: list[str] | None = None) -> dict:
    """The local vision model's reading of one frame: {"screen_share", "highlighted", "name"}.
    `candidates`: name labels the text reader found in the frame (the answer must be one of them)."""
    import requests

    url = (ollama_url or config.OLLAMA_URL).rstrip("/") + "/api/chat"
    payload = {"model": model, "stream": False, "think": False, "keep_alive": "2m",
               "options": {"temperature": 0, "num_predict": 120},
               "messages": [{"role": "user", "content": vision_prompt(candidates or []),
                             "images": [base64.b64encode(image.read_bytes()).decode()]}]}
    response = requests.post(url, json=payload, timeout=timeout)
    response.raise_for_status()
    reply = parse_vision_reply(response.json().get("message", {}).get("content", ""))
    if candidates and reply["name"] and reply["name"].lower() not in {c.lower() for c in candidates}:
        reply["name"] = ""  # not a label on screen: never trust an invented name
    return reply


def _true(value) -> bool:
    """JSON true, or "true" as a string (small models often quote booleans)."""
    return value is True or (isinstance(value, str) and value.strip().lower() in ("true", "yes"))


def parse_vision_reply(text: str) -> dict:
    """Reads the vision model's JSON answer {screen_share, highlighted, name}. Small models write booleans as
    "true"/"false" strings; both are accepted.
    """
    text = re.sub(r"<think>.*?</think>", "", text or "", flags=re.S)
    match = re.search(r"\{.*\}", text, re.S)
    try:
        data = json.loads(match.group(0)) if match else {}
    except ValueError:
        data = {}
    if not isinstance(data, dict):
        data = {}
    name = data.get("name")
    cleaned = clean_name(name) if isinstance(name, str) and name.strip().lower() not in ("null", "none") else None
    highlighted = _true(data.get("highlighted"))
    return {"screen_share": _true(data.get("screen_share")), "highlighted": highlighted,
            "name": cleaned[0] if cleaned and highlighted else "", "truncated": bool(cleaned and cleaned[1])}


def unload_model(model: str) -> None:
    """Asks Ollama to free the vision model's memory now (keep_alive 0)."""
    try:
        import requests
        requests.post(f"{config.OLLAMA_URL}/api/generate", json={"model": model, "keep_alive": 0}, timeout=10)
    except Exception:
        pass


def pick_for_vision(results: list[FrameResult], cap: int) -> list[FrameResult]:
    """Unresolved frames worth asking about, spread over speakers; speakers with no name first."""
    if cap <= 0:
        return []
    named = {r.speaker for r in results if r.name}
    todo = [r for r in results if r.status in ("unresolved", "ambiguous")]
    by: dict[str, list[FrameResult]] = defaultdict(list)
    for r in todo:
        by[r.speaker].append(r)
    order = sorted(by, key=lambda s: (s in named, s))
    picked: list[FrameResult] = []
    while len(picked) < cap and any(by.values()):
        for spk in order:
            if by[spk] and len(picked) < cap:
                frames = by[spk]
                picked.append(frames.pop(len(frames) // 2 if len(frames) > 2 else 0))
    return picked


# ---------------------------------------------------------------------------
# The stage
# ---------------------------------------------------------------------------

def run(src: Path, segments: list[dict], work: Path, progress: Callable[[float], None],
        options: dict | None = None, ocr: Callable | None = None, vision: Callable | None = None,
        has_video: bool = True) -> tuple[dict, dict]:
    """(screen_names, screen_info) for one recording. `progress(fraction)` may raise JobCancelled."""
    options = options or {}
    started = time.time()
    info = {"status": "done", "frames": 0, "resolved": 0, "screen_share": 0, "unresolved": 0,
            "vision_frames": 0, "vision_resolved": 0, "vision_model": "", "seconds": 0, "error": ""}
    if not options.get("enabled", True):
        return {}, dict(info, status="off")
    if not has_video:
        return {}, dict(info, status="no_video")
    samples = plan_samples(segments)
    if not samples:
        return {}, dict(info, status="no_turns")
    frames_dir = work / "frames"
    frames_dir.mkdir(parents=True, exist_ok=True)
    paths: list[tuple[float, str, Path]] = []
    done = [0]
    total_steps = len(samples) * 2 + max(0, int(options.get("vision_cap", VISION_CAP) or 0)) * 4

    def step(n: int = 1) -> None:
        """Advances the stage's progress by `n` units of work."""
        done[0] += n
        progress(min(0.99, done[0] / max(1, total_steps)))

    def grab(sample):
        """Extracts one sampled frame (reusing a frame saved earlier); None if ffmpeg failed."""
        t, spk = sample
        dest = frames_dir / f"{t:010.2f}.jpg"
        return (t, spk, dest) if dest.exists() or extract_frame(src, t, dest) else None

    with ThreadPoolExecutor(max_workers=4) as pool:
        for item in pool.map(grab, samples):
            step()
            if item:
                paths.append(item)
    if not paths:
        return {}, dict(info, status="no_frames", seconds=round(time.time() - started, 1))
    try:
        texts = (ocr or run_ocr)([p for _, _, p in paths], on_line=step)
    except RuntimeError as exc:
        info.update(status="error", error=str(exc))
        texts = {}
    results: list[FrameResult] = []
    for t, spk, path in paths:
        lines = texts.get(str(path))
        if lines is None:
            results.append(FrameResult(t, spk, "error", path=str(path)))
            continue
        status, found, tiles = read_frame(load_rgb(path), lines)
        results.append(FrameResult(t, spk, status, found[0] if found else "", bool(found and found[1]),
                                   tiles, str(path)))
    model = options.get("vision_model") or ""
    cap = int(options.get("vision_cap", VISION_CAP) or 0)
    if model and cap > 0:
        info["vision_model"] = model
        ask = vision or (lambda image, names: ask_vision(model, image, candidates=names))
        for r in pick_for_vision(results, cap):
            names = [n[0] for n in (clean_name(ln["text"]) for ln in texts.get(r.path) or []) if n]
            try:
                reply = ask(Path(r.path), list(dict.fromkeys(names))) if vision is None else ask(Path(r.path))
            except Exception as exc:  # the stage still finishes with what the text reader found
                log.warning("Vision fallback failed: %s", exc)
                info["error"] = info["error"] or f"Vision model: {type(exc).__name__}"
                break
            info["vision_frames"] += 1
            step(4)
            if reply.get("screen_share"):
                r.status = "screen_share"
            elif reply.get("name"):
                r.status, r.name, r.truncated = "vision", reply["name"], reply.get("truncated", False)
                info["vision_resolved"] += 1
        if vision is None:
            unload_model(model)
    if options.get("debug"):
        _save_debug(results, work)
    names = aggregate(results)
    counts = Counter(r.status for r in results)
    info.update(frames=len(results), resolved=sum(1 for r in results if r.name),
                screen_share=counts["screen_share"], unresolved=counts["unresolved"] + counts["ambiguous"],
                seconds=round(time.time() - started, 1))
    progress(1.0)
    return names, info


def _save_debug(results: list[FrameResult], work: Path) -> None:
    """Debug setting only: crops of the highlighted tiles, kept next to the transcripts."""
    from PIL import Image

    out = config.TRANSCRIPTS_DIR / "screen-debug" / work.name
    out.mkdir(parents=True, exist_ok=True)
    for r in results:
        if not r.path or not Path(r.path).exists():
            continue
        with Image.open(r.path) as im:
            for i, (x0, y0, x1, y1) in enumerate(r.tiles or [(0, 0, 1, 1)]):
                crop = im.crop((int(x0 * im.width), int(y0 * im.height), int(x1 * im.width), int(y1 * im.height)))
                crop.save(out / f"{r.t:08.2f}_{r.speaker}_{r.status}_{i}.jpg", quality=80)
