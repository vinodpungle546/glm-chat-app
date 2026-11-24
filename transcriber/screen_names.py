"""On-screen names: read who the meeting app shows as the active speaker, offline.

Teams and Zoom put each participant's name on their video tile and draw a coloured border
around the tile of whoever is talking. For each diarized speaker we sample a few frames at
the start of their turns, find the highlighted tile and read its name label with Apple's
on-device text recognition (transcriber/ocr/quorum_ocr.swift, Vision framework).

Before that, the whole recording is scanned for the call's participants list (transcriber/participants.py): it
gives the attendees and, where the list marks who is talking, a second source for each speaker's name (counted as
`list` next to the tiles, agreements as `both`, disagreements as conflicts). Its full names complete names cut short
on tiles and limit the names the vision model may answer with.

Then a local Ollama vision model reviews frames of every speaker (compulsory, not a fallback):
it confirms what the text reader found, corrects it (both are kept as evidence, the conflict is
recorded) or fills in a name the text reader missed. Frames marked "screen share" are reviewed too:
a Teams window with a chat or side panel open has lots of text, and its thin speaking outline is
often missed by the tile detector. The model only reads name labels, never faces, and may only
answer with a label the text reader saw in that frame. Frames never leave the Mac.

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

from transcriber import config, participants

log = logging.getLogger(__name__)

HERE = Path(__file__).resolve().parent
OCR_SOURCE = HERE / "ocr" / "quorum_ocr.swift"
OCR_BINARY = HERE / "bin" / "quorum-ocr"

PER_SPEAKER_TURNS = 10        # turns sampled per speaker
MAX_FRAMES = 240              # per meeting
MIN_TURN_SECONDS = 1.5        # shorter turns: the highlight may still be on the previous speaker
FRAME_WIDTH = 1280
VISION_CAP = 12               # vision-model frames per meeting when a number is set (older default)
REVIEW_PER_SPEAKER = 4        # frames the vision model reviews per speaker ("auto" budget)
REVIEW_MIN, REVIEW_MAX = 12, 48
SECONDS_PER_FRAME = 0.35      # extract + OCR, measured on the M4 Air
SECONDS_PER_VISION_FRAME = 9.0  # Qwen3-VL 4B on the M4 Air (benchmark_vision_models.py)
SCREEN_SHARE_LINES = 14       # this much text and no tiles: a shared screen fills the view
SCAN_SECONDS_PER_FRAME = 0.12  # participants scan: extract + colour per frame, text only when the view changed

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
    review: str = ""                  # vision review: confirmed | corrected | filled | no_answer
    vision_name: str = ""             # the vision model's name when it differs from the text reader's
    list_name: str = ""               # the participants list's speaker at this moment
    list_by_vision: bool = False      # …read by the vision model (not found by the scan)


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


def review_budget(speakers: int, setting: int | None = None) -> int:
    """Frames the vision model reviews: a number from Settings, else 4 per speaker (12 to 48)."""
    if setting:
        return int(setting)
    return max(REVIEW_MIN, min(REVIEW_MAX, REVIEW_PER_SPEAKER * max(1, speakers)))


def estimate_seconds(duration: float, vision_cap: int = 0, vision_seconds: float | None = None,
                     scan: bool = True) -> dict:
    """`vision_seconds`: the chosen vision model's time per frame (default: Qwen3-VL 4B's); `scan`: the whole
    recording is scanned for the participants list (one frame a second)."""
    frames = estimate_frames(duration)
    low = frames * SECONDS_PER_FRAME + (duration / participants.SCAN_EVERY * SCAN_SECONDS_PER_FRAME if scan else 0)
    per_frame = vision_seconds or SECONDS_PER_VISION_FRAME
    return {"frames": frames, "low": round(low), "high": round(low * 1.5 + vision_cap * per_frame)}


def _entry(name: str, truncated: bool = False) -> dict:
    return {"name": name, "count": 0, "times": [], "truncated": truncated, "ocr": 0, "speaker_view": 0, "vision": 0,
            "list": 0, "both": 0}


def aggregate(results: Iterable[FrameResult], listed: dict[str, Counter] | None = None,
              list_names: list[str] | None = None) -> dict[str, dict]:
    """Per speaker: the names seen (truncated labels folded into the full name, spellings that differ only in
    accents merged), with counts: `count` frames read on tiles (text reader, speaker view or vision model), `list`
    frames where the participants list marked them as speaking, `both` sampled frames where tile and list agreed.
    `listed`: {speaker: Counter(name)} from the participants list scan; `list_names`: its full names."""
    listed = listed or {}
    seen: dict[str, dict[str, dict]] = defaultdict(dict)
    frames: Counter = Counter()
    resolved: Counter = Counter()
    conflicts: dict[str, list] = defaultdict(list)

    def add(spk: str, name: str, status: str, truncated: bool, t: float, confirmed: bool = False) -> dict:
        entry = seen[spk].setdefault(name, _entry(name, truncated))
        entry["count"] += 1
        entry[status] = entry.get(status, 0) + 1
        if confirmed:
            entry["confirmed"] = entry.get("confirmed", 0) + 1
        entry["truncated"] = entry["truncated"] and truncated
        if len(entry["times"]) < 12:
            entry["times"].append(t)
        return entry

    def conflict(spk: str, item: dict) -> None:
        if len(conflicts[spk]) < 10:
            conflicts[spk].append(item)

    for r in results:
        frames[r.speaker] += 1
        if r.name or r.vision_name:
            resolved[r.speaker] += 1
        if r.name:
            entry = add(r.speaker, r.name, r.status, r.truncated, r.t, confirmed=r.review == "confirmed")
            if r.list_name and (participants.fold(r.list_name) == participants.fold(r.name)
                                or same_person(r.name, r.list_name)):
                entry["both"] += 1
            elif r.list_name:  # the tile and the list name different people at this moment
                conflict(r.speaker, {"t": r.t, ("vision" if r.status == "vision" else "ocr"): r.name, "list": r.list_name})
        if r.vision_name:  # the vision model saw someone else: both stay as evidence
            add(r.speaker, r.vision_name, "vision", False, r.t)
            conflict(r.speaker, {"t": r.t, "ocr": r.name, "vision": r.vision_name})
        if r.list_by_vision and r.list_name:  # the vision model read the list's speaker (the scan didn't)
            seen[r.speaker].setdefault(r.list_name, _entry(r.list_name))["list"] += 1
    list_total: Counter = Counter()
    for spk, names in listed.items():
        for name, c in names.items():
            seen[spk].setdefault(name, _entry(name))["list"] += c
            list_total[spk] += c
    for spk in list(seen):  # spellings that differ only in accents or case: one entry
        groups: dict[str, list[str]] = defaultdict(list)
        for name in seen[spk]:
            groups[participants.fold(name)].append(name)
        for spellings in groups.values():
            if len(spellings) < 2:
                continue
            best = participants.best_spelling(spellings, Counter({n: seen[spk][n]["count"] for n in spellings}))
            target = seen[spk][best]
            for other in spellings:
                if other != best:
                    _fold_into(target, seen[spk].pop(other))
    out: dict[str, dict] = {}
    all_full = {n for names in seen.values() for n, e in names.items() if not e["truncated"]} | set(list_names or [])
    for spk in set(frames) | set(seen):
        names = seen.get(spk, {})
        for short in [n for n, e in names.items() if e["truncated"]]:
            full = sorted(f for f in all_full if same_person(short, f))
            if len(full) == 1:
                _fold_into(names.setdefault(full[0], _entry(full[0])), names.pop(short))
        tile_frames, list_frames = max(1, frames[spk]), max(1, list_total[spk])
        ranked = sorted(names.values(), key=lambda e: -(e["count"] / tile_frames + e.get("list", 0) / list_frames))
        out[spk] = {"names": ranked, "frames": frames[spk], "resolved": resolved[spk]}
        if list_total[spk]:
            out[spk]["list_frames"] = list_total[spk]
        if conflicts.get(spk):
            out[spk]["conflicts"] = conflicts[spk]
    return out


def _fold_into(target: dict, src: dict) -> None:
    """Adds one name entry's counts and times to another (a cut-off or differently accented spelling)."""
    for key in ("count", "ocr", "speaker_view", "vision", "confirmed", "list", "both"):
        if key in src:
            target[key] = target.get(key, 0) + src[key]
    target["times"] = sorted(target["times"] + src["times"])[:12]


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
    "2. Is exactly one participant tile highlighted as the active speaker? Apps mark the person talking with a "
    "coloured or white outline around their tile or thumbnail (in Teams often a thin purple, blue or white rounded "
    "border), a glow, or a ring around their picture or initials.\n"
    "3. If so, what is the name label written on that highlighted tile, exactly as shown?\n"
    'Reply with JSON only: {"screen_share": true or false, "highlighted": true or false, '
    '"name": "the label text" or null}'
)


def vision_prompt(candidates: list[str], panel_names: list[str] | None = None) -> str:
    """The question for one frame; with the labels the text reader found (and the participants list's names), the
    model only picks one of them. `panel_names`: the frame shows a participants list with these names; the model
    is also asked which of them the list marks as speaking."""
    if not candidates and not panel_names:
        return VISION_PROMPT
    text = VISION_PROMPT.rsplit("\nReply", 1)[0]
    if candidates:
        text += ("\nThe name labels visible in this frame are: " + "; ".join(candidates)
                 + ". If a tile is highlighted, the name must be one of these, written exactly as listed.")
    fields = '"screen_share": true or false, "highlighted": true or false, "name": ' + (
        '"one of the listed names" or null' if candidates else '"the label text" or null')
    if panel_names:
        text += ("\n4. The frame also shows the call's participants list: " + "; ".join(panel_names) + ". Does the "
                 "list mark one of them as speaking (a ring around their picture, a highlighted row, or a lit or moving "
                 "microphone icon)? If so, which one, written exactly as listed?")
        fields += ', "list_name": "one of the participants" or null'
    return text + "\nReply with JSON only: {" + fields + "}"


def ask_vision(model: str, image: Path, ollama_url: str | None = None, timeout: float = 120,
               candidates: list[str] | None = None, panel_names: list[str] | None = None) -> dict:
    """The local vision model's reading of one frame: {"screen_share", "highlighted", "name", "list_name"}.
    `candidates`: name labels the text reader found in the frame (the answer must be one of them); `panel_names`:
    the participants list in the frame (its speaker must be one of them)."""
    import requests

    url = (ollama_url or config.OLLAMA_URL).rstrip("/") + "/api/chat"
    payload = {"model": model, "stream": False, "think": False, "keep_alive": "2m",
               "options": {"temperature": 0, "num_predict": 160},
               "messages": [{"role": "user", "content": vision_prompt(candidates or [], panel_names),
                             "images": [base64.b64encode(image.read_bytes()).decode()]}]}
    response = requests.post(url, json=payload, timeout=timeout)
    response.raise_for_status()
    reply = parse_vision_reply(response.json().get("message", {}).get("content", ""))
    return restrict(reply, candidates, panel_names)


def restrict(reply: dict, candidates: list[str] | None, panel_names: list[str] | None) -> dict:
    """Drops answers that aren't among the names on screen (never trust an invented name)."""
    def match(name: str, allowed: list[str] | None) -> str:
        if not name or not allowed:
            return name if allowed is None else ""
        return next((a for a in allowed if participants.fold(a) == participants.fold(name)), "")

    if candidates:
        reply["name"] = match(reply.get("name", ""), candidates)
    reply["list_name"] = match(reply.get("list_name", ""), panel_names or [])
    return reply


def _true(value) -> bool:
    """JSON true, or "true" as a string (small models often quote booleans)."""
    return value is True or (isinstance(value, str) and value.strip().lower() in ("true", "yes"))


def parse_vision_reply(text: str) -> dict:
    """Reads the vision model's JSON answer {screen_share, highlighted, name, list_name}. Small models write
    booleans as "true"/"false" strings; both are accepted.
    """
    text = re.sub(r"<think>.*?</think>", "", text or "", flags=re.S)
    match = re.search(r"\{.*\}", text, re.S)
    try:
        data = json.loads(match.group(0)) if match else {}
    except ValueError:
        data = {}
    if not isinstance(data, dict):
        data = {}

    def name_of(value) -> tuple[str, bool] | None:
        return clean_name(value) if isinstance(value, str) and value.strip().lower() not in ("null", "none") else None

    cleaned = name_of(data.get("name"))
    listed = name_of(data.get("list_name"))
    highlighted = _true(data.get("highlighted"))
    return {"screen_share": _true(data.get("screen_share")), "highlighted": highlighted,
            "name": cleaned[0] if cleaned and highlighted else "", "truncated": bool(cleaned and cleaned[1]),
            "list_name": listed[0] if listed else ""}


def unload_model(model: str) -> None:
    """Asks Ollama to free the vision model's memory now (keep_alive 0)."""
    try:
        import requests
        requests.post(f"{config.OLLAMA_URL}/api/generate", json={"model": model, "keep_alive": 0}, timeout=10)
    except Exception:
        pass


REVIEW_FIRST = ("unresolved", "ambiguous", "screen_share")  # frames the text reader couldn't settle come first


def pick_for_review(results: list[FrameResult], cap: int) -> list[FrameResult]:
    """Frames the vision model reviews: every speaker gets frames in turn (speakers without a name first, then
    the most sampled, i.e. most-spoken), spread over their turns; frames the text reader couldn't settle (including
    "screen share") before the ones it read, which the review confirms or corrects."""
    if cap <= 0:
        return []
    named = {r.speaker for r in results if r.name}
    by: dict[str, list[FrameResult]] = defaultdict(list)
    for r in results:
        if r.status != "error" and r.path:
            by[r.speaker].append(r)
    for spk, frames in by.items():
        todo = [r for r in frames if r.status in REVIEW_FIRST]
        done = [r for r in frames if r.status not in REVIEW_FIRST]
        by[spk] = _spread(todo) + _spread(done)
    order = sorted(by, key=lambda s: (s in named, -len(by[s]), s))
    picked: list[FrameResult] = []
    while len(picked) < cap and any(by.values()):
        for spk in order:
            if by[spk] and len(picked) < cap:
                picked.append(by[spk].pop(0))
    return picked


def pick_for_vision(results: list[FrameResult], cap: int) -> list[FrameResult]:
    """Older name of pick_for_review (kept for scripts)."""
    return pick_for_review(results, cap)


def _spread(frames: list[FrameResult]) -> list[FrameResult]:
    """The frames reordered so the first few are spread over the meeting: middle, ends, then the rest."""
    out, pending = [], [sorted(frames, key=lambda r: r.t)]
    while pending:
        part = pending.pop(0)
        if not part:
            continue
        mid = len(part) // 2
        out.append(part[mid])
        pending += [part[:mid], part[mid + 1:]]
    return out


def review_frame(r: FrameResult, reply: dict) -> str:
    """Applies the vision model's answer to one frame: confirmed (same name as the text reader), corrected
    (another name: kept next to the text reader's), filled (the text reader had none) or no_answer."""
    if reply.get("list_name") and not r.list_name:  # the list's speaker, read by the model (the scan had none)
        r.list_name, r.list_by_vision = reply["list_name"], True
    seen = reply.get("name") or ""
    if not seen:
        if reply.get("screen_share") and not r.name:
            r.status = "screen_share"
        r.review = "no_answer"
    elif r.name and (seen.lower() == r.name.lower() or same_person(r.name, seen) or same_person(seen, r.name)):
        r.review = "confirmed"
    elif r.name:
        r.review, r.vision_name = "corrected", seen
    else:
        r.status, r.name, r.truncated, r.review = "vision", seen, reply.get("truncated", False), "filled"
    return r.review


# ---------------------------------------------------------------------------
# The stage
# ---------------------------------------------------------------------------

def run(src: Path, segments: list[dict], work: Path, progress: Callable[[float], None],
        options: dict | None = None, ocr: Callable | None = None, vision: Callable | None = None,
        has_video: bool = True) -> tuple[dict, dict]:
    """(screen_names, screen_info) for one recording; screen_info["participants"] holds the participants list
    (the pipeline stores it on the transcript). `progress(fraction)` may raise JobCancelled.
    Options: enabled, vision_model, vision_cap, debug, participants (scan for the list, default on)."""
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
    duration = max((float(s.get("end") or 0) for s in segments), default=0.0) + 2
    scan_on = options.get("participants", True)
    scan_steps = int(duration / participants.SCAN_EVERY) if scan_on else 0
    total_steps = scan_steps + len(samples) * 2 \
        + review_budget(len({spk for _, spk in samples}), options.get("vision_cap")) * 4

    def step(n: int = 1) -> None:
        """Advances the stage's progress by `n` units of work."""
        done[0] += n
        progress(min(0.99, done[0] / max(1, total_steps)))

    # 1. The participants list, on every scanned frame of the recording.
    readings, scanned = [], 0
    if scan_on:
        try:
            readings, scanned = participants.scan(src, duration, work, lambda _d, _t: step(), ocr=ocr)
        except pipeline_cancelled():
            raise
        except RuntimeError as exc:  # no text reader: the tiles can't be read either (reported below)
            log.warning("Participants scan failed: %s", exc)
    talking = participants.speaking(readings)
    canon = participants.canonical_map(readings)
    summary = participants.summarise(readings, talking, scanned) if scan_on else {"status": "off"}
    list_names = [e["name"] for e in summary.get("names", [])]
    listed = participants.list_evidence(talking, segments, canon)

    # 2. Sampled frames at the start of each speaker's turns: the highlighted tile's name.
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
        return {}, dict(info, status="no_frames", seconds=round(time.time() - started, 1), participants=summary)
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
        said = participants.at(talking, t)
        results.append(FrameResult(t, spk, status, found[0] if found else "", bool(found and found[1]),
                                   tiles, str(path), list_name=canon.get(said, said)))

    # 3. The vision model reviews frames of every speaker (and reads the list's speaker where a list is shown).
    model = options.get("vision_model") or ""
    speakers = len({r.speaker for r in results})
    cap = review_budget(speakers, options.get("vision_cap"))
    info.update(vision_model=model, reviewed=0, confirmed=0, corrected=0, filled=0, no_answer=0, review_reason="")
    if not model:
        info["review_reason"] = "No vision model is installed (Settings → Meetings → Video frames)."
    else:
        ask = _asker(model, vision)
        for r in pick_for_review(results, cap):
            lines = texts.get(r.path) or []
            labels = [n[0] for n in (clean_name(ln["text"]) for ln in lines) if n]
            names = _unique(labels + list_names)
            panel = participants.find_list(lines)
            try:
                reply = ask(Path(r.path), names, [row.name for row in panel.rows] if panel else None)
            except pipeline_cancelled() as exc:  # a cancelled job stops here
                raise exc
            except Exception as exc:  # the stage still finishes with what the text reader found
                log.warning("Vision review failed: %s", exc)
                info["error"] = info["error"] or f"Vision model: {type(exc).__name__}"
                if not info["reviewed"]:
                    info["review_reason"] = f"The vision model ({model}) didn't answer: {type(exc).__name__}."
                break
            info["reviewed"] += 1
            info[review_frame(r, reply)] += 1
            step(4)
        if vision is None:
            unload_model(model)
        if not info["reviewed"] and not info["review_reason"]:
            info["review_reason"] = "No frames could be read from the video."
    info["vision_frames"] = info["reviewed"]
    info["vision_resolved"] = info["filled"] + info["corrected"]
    if options.get("debug"):
        _save_debug(results, work)
    names = aggregate(results, listed, list_names)
    counts = Counter(r.status for r in results)
    info.update(frames=len(results), resolved=sum(1 for r in results if r.name),
                screen_share=counts["screen_share"], unresolved=counts["unresolved"] + counts["ambiguous"],
                list_frames=sum(sum(c.values()) for c in listed.values()),
                list_agree=sum(e.get("both", 0) for v in names.values() for e in v["names"]),
                list_conflicts=sum(1 for v in names.values() for c in v.get("conflicts", []) if "list" in c),
                seconds=round(time.time() - started, 1), participants=summary)
    progress(1.0)
    return names, info


def _unique(names: list[str]) -> list[str]:
    """Names without repeats (spellings that differ only in accents count once; the accented one is kept)."""
    groups: dict[str, list[str]] = {}
    for name in names:
        groups.setdefault(participants.fold(name), []).append(name)
    return [participants.best_spelling(v) for v in groups.values()]


def _asker(model: str, vision: Callable | None) -> Callable:
    """(image, names, panel names) → reply. An injected `vision(image, names[, panel_names])` (tests) or the
    local Ollama model."""
    if vision is None:
        return lambda image, names, panel: ask_vision(model, image, candidates=names, panel_names=panel)
    import inspect

    takes_panel = len(inspect.signature(vision).parameters) >= 3
    return lambda image, names, panel: restrict(vision(image, names, panel) if takes_panel else vision(image, names),
                                                names, panel)


def pipeline_cancelled() -> type:
    """The job-cancelled exception (imported lazily: pipeline imports this module)."""
    try:
        from transcriber.pipeline import JobCancelled
        return JobCancelled
    except ImportError:  # pragma: no cover
        return KeyboardInterrupt


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
