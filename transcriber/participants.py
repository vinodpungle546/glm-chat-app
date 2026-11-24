"""The participants list in a call recording: who attended, and who the list shows as speaking.

Teams and Zoom can show a list of everyone on the call: the People / Participants panel (a heading such as
"Participants (5)" with one row per person, and sections like "Invited" for people who haven't joined), or, while
someone shares their screen, a strip of participant thumbnails with a name under each. The list usually marks the
person talking: a coloured ring around their picture, or a lit-up microphone icon.

This module scans the whole recording (one frame per second, every frame of a Quorum recording at 2 fps is at most
two apart), reads the list's names with the on-device text reader (the same OCR helper as screen_names.py) and
finds the speaking row from the pixels: a row whose colour rises clearly above its own usual level (a person's
avatar colour is constant, the speaking ring or mic level is not) while no other row's does.

Results:
- the attendees: every name seen in the list (first and last time seen, frames), and names listed only as invited;
- "list speakers": {time: name} for the frames where exactly one row was marked as speaking, which
  screen_names.py matches with the diarized speaker talking at that moment.

Frames are read on this Mac and deleted afterwards; nothing here leaves the machine. Pure helpers are tested with
OCR lines and synthetic frames (tests/synthetic_meeting.py `panel` / `share_strip`).
"""

from __future__ import annotations

import logging
import re
import shutil
import subprocess
import unicodedata
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from pathlib import Path
from typing import Callable

log = logging.getLogger(__name__)

SCAN_EVERY = 1.0            # seconds between scanned frames
CHUNK_SECONDS = 300         # frames extracted per ffmpeg call (keeps temporary disk use small)
REOCR_SECONDS = 30.0        # read the text again at least this often, even when the frame looks unchanged
CHANGE_THRESHOLD = 0.012    # mean pixel change (0..1) of a small grey thumbnail that counts as a new view
MIN_DELTA = 0.006           # a speaking row's colour share rises at least this much above its usual level…
SECOND_RATIO = 0.5          # …and no other row rises more than half as much
MIN_FRAMES = 2              # a name must be read in at least this many frames to count as an attendee

_HEADER = re.compile(r"^(?:participants|people|attendees|in (?:this|the) (?:meeting|call))\s*(?:\(\s*(\d+)\s*\))?\s*$",
                     re.I)
_INVITED = re.compile(r"^(?:invited|others invited|not (?:yet )?joined|suggestions|waiting in (?:the )?lobby|"
                      r"in the lobby|waiting room)\b", re.I)
_TOOLBAR = {"chat", "raise", "react", "reactions", "view", "more", "leave", "camera", "mic", "share", "apps", "rooms",
            "notes", "whiteboard", "record", "present", "copilot", "people", "participants", "hand", "mute", "unmute",
            "video", "audio", "security", "summary", "ai"}
STRIP_MIN = 3               # thumbnails in a strip (fewer: lines at the side of a shared document look alike)
ALIGN = 0.012               # names in a list start at the same left edge (fraction of the frame width)
FIRST_ROW = 0.16            # the first name is this close under the heading
_ROLE_LINES = {"organizer", "organiser", "presenter", "presenters", "attendee", "attendees", "guest", "external",
               "host", "co-host", "cohost", "unverified", "mute all", "invite", "share invite", "manage permissions",
               "everyone", "panelist", "panelists", "you", "me", "muted", "unmuted"}


@dataclass
class Row:
    """One name in the list on one frame: the name, whether it was cut short, its text box and the colour share of
    its row (its picture and icons)."""
    name: str
    truncated: bool
    box: tuple[float, float, float, float]
    vivid: float = 0.0


@dataclass
class Panel:
    """The participants list found on one frame: panel (with a heading) or strip (thumbnails while sharing)."""
    kind: str
    rows: list[Row]
    invited: list[str] = field(default_factory=list)
    count: int | None = None              # "Participants (5)"
    left: float = 0.0
    right: float = 1.0
    spacing: float = 0.05


def fold(name: str) -> str:
    """A name compared loosely: lower case, without accents ("Tomás" and "Tomas" are the same; the text reader
    often drops accents in small fonts)."""
    text = unicodedata.normalize("NFKD", name or "")
    return " ".join("".join(c for c in text if not unicodedata.combining(c)).lower().split())


def best_spelling(names: list[str], counts: Counter | None = None) -> str:
    """Of spellings of one name, the one with accents (the reader drops them, never adds them), else the most read."""
    counts = counts or Counter()
    return max(names, key=lambda n: (sum(1 for c in n if ord(c) > 127), counts[n], n))


def _clean(text: str):
    """(name, truncated) for a list row, or None for headings, roles and other interface text."""
    from transcriber.screen_names import clean_name

    if text.strip().lower().strip("()") in _ROLE_LINES:
        return None
    return clean_name(text)


def _toolbar(line: dict, lines: list[dict]) -> bool:
    """A heading word that is really a button in the meeting's toolbar ("People" next to Chat, React, View, More…)."""
    x, y, _w, h = line["box"]
    cy = y + h / 2
    same_row = [ln for ln in lines if ln is not line and abs(ln["box"][1] + ln["box"][3] / 2 - cy) <= max(0.015, h)]
    buttons = sum(1 for ln in same_row if " ".join(str(ln.get("text", "")).lower().split()).strip(".:") in _TOOLBAR)
    return buttons >= 2


def _aligned(rows: list[Row]) -> list[Row]:
    """The rows that start at the list's common left edge (a list is left-aligned; text around it isn't)."""
    if len(rows) < 2:
        return rows
    lefts = sorted(r.box[0] for r in rows)
    edge = max(lefts, key=lambda x: sum(1 for v in lefts if abs(v - x) <= ALIGN))
    return [r for r in rows if abs(r.box[0] - edge) <= ALIGN]


def find_panel(lines: list[dict]) -> Panel | None:
    """A Participants / People panel: a heading (not a toolbar button), then names left-aligned in the column just
    under it (names after an "Invited" or lobby heading are only invited)."""
    for line in lines:
        m = _HEADER.match(" ".join(str(line.get("text", "")).split()))
        if not m or _toolbar(line, lines):
            continue
        panel = _panel_under(line, int(m.group(1)) if m.group(1) else None, lines)
        if panel:
            return panel
    return None


def _panel_under(header: dict, count: int | None, lines: list[dict]) -> Panel | None:
    hx, hy, _hw, hh = header["box"]
    left, right = max(0.0, hx - 0.04), min(1.0, hx + 0.32)
    below = sorted((ln for ln in lines if ln is not header and ln["box"][1] > hy + hh * 0.5
                    and left <= ln["box"][0] <= hx + 0.12), key=lambda ln: ln["box"][1])
    rows, invited, section = [], [], "in"
    for line in below:
        text = " ".join(str(line.get("text", "")).split())
        if _INVITED.match(text):
            section = "invited"
            continue
        if _HEADER.match(text):
            section = "in"
            continue
        found = _clean(text)
        if not found:
            continue
        if section == "invited":
            invited.append((found[0], line["box"]))
        else:
            rows.append(Row(found[0], found[1], tuple(line["box"])))
    rows = _aligned(rows)
    if invited and rows:
        invited = [(n, b) for n, b in invited if abs(b[0] - rows[0].box[0]) <= ALIGN]
    if len(rows) < 2 or rows[0].box[1] - (hy + hh) > FIRST_ROW:
        return None  # not a list: a heading word above other text
    gaps = [b.box[1] - a.box[1] for a, b in zip(rows, rows[1:]) if b.box[1] > a.box[1]]
    spacing = sorted(gaps)[len(gaps) // 2] if gaps else 0.05
    kept = [rows[0]]
    for row in rows[1:]:  # one list: stop at a big jump (text further down isn't part of it)
        if row.box[1] - kept[-1].box[1] > spacing * 2.6:
            break
        kept.append(row)
    if len(kept) < 2:
        return None
    return Panel("panel", kept, [n for n, _ in invited], count, left, right, spacing)


def find_strip(lines: list[dict]) -> Panel | None:
    """The thumbnails strip while someone shares their screen: three or more names stacked at the same left edge in
    the right quarter of the view, evenly spaced (a picture above each name)."""
    by_x: dict[int, list[tuple[dict, tuple[str, bool]]]] = defaultdict(list)
    for line in lines:
        x = line["box"][0]
        if x < 0.72:
            continue
        found = _clean(line.get("text", ""))
        if found:
            by_x[round(x * 100)].append((line, found))
    best: list[tuple[dict, tuple[str, bool]]] = []
    for key in sorted(by_x):
        group = by_x[key] + by_x.get(key + 1, [])
        if len(group) > len(best):
            best = group
    best.sort(key=lambda item: item[0]["box"][1])
    if len(best) < STRIP_MIN:  # two lines of text at the right of a shared screen look like a strip too
        return None
    gaps = [b[0]["box"][1] - a[0]["box"][1] for a, b in zip(best, best[1:])]
    if min(gaps) < 0.08 or max(gaps) > 2.2 * min(gaps):  # not evenly spaced: lines of a document, not thumbnails
        return None
    rows = [Row(found[0], found[1], tuple(line["box"])) for line, found in best]
    left = max(0.0, min(r.box[0] for r in rows) - 0.02)
    spacing = sorted(gaps)[len(gaps) // 2]
    return Panel("strip", rows, [], None, left, 1.0, spacing)


def find_list(lines: list[dict]) -> Panel | None:
    """The participants panel, else the thumbnails strip, else None."""
    return find_panel(lines) or find_strip(lines)


def row_band(panel: Panel, row: Row) -> tuple[float, float, float, float]:
    """The part of the frame that belongs to a row (left, top, right, bottom): around the name in a panel (picture
    to its left, icons to its right), the thumbnail above the name in a strip."""
    x, y, _w, h = row.box
    if panel.kind == "strip":
        return panel.left, max(0.0, y - panel.spacing * 0.85), panel.right, y + h
    half = max(h, panel.spacing * 0.45)
    return panel.left, max(0.0, y + h / 2 - half), panel.right, min(1.0, y + h / 2 + half)


def measure(rgb, panel: Panel) -> None:
    """Sets each row's colour share (vivid pixels in its band; text is white or grey, so it doesn't count)."""
    from transcriber.screen_names import highlight_mask

    mask = highlight_mask(rgb)
    height, width = mask.shape
    for row in panel.rows:
        x0, y0, x1, y1 = row_band(panel, row)
        part = mask[int(y0 * height):max(int(y0 * height) + 1, int(y1 * height)),
                    int(x0 * width):max(int(x0 * width) + 1, int(x1 * width))]
        row.vivid = float(part.mean()) if part.size else 0.0


@dataclass
class Reading:
    """The list as read on one scanned frame."""
    t: float
    panel: Panel


def speaking(readings: list[Reading]) -> dict[float, str]:
    """{time: name} for frames where exactly one row's colour rose clearly above its own usual level (median over
    the meeting): that row is marked as speaking."""
    levels: dict[str, list[float]] = defaultdict(list)
    for r in readings:
        for row in r.panel.rows:
            levels[fold(row.name)].append(row.vivid)
    usual = {name: sorted(v)[len(v) // 2] for name, v in levels.items()}
    out: dict[float, str] = {}
    for r in readings:
        if len(r.panel.rows) < 2:
            continue
        deltas = sorted(((row.vivid - usual[fold(row.name)], row.name) for row in r.panel.rows), reverse=True)
        top, second = deltas[0], deltas[1]
        if top[0] >= MIN_DELTA and second[0] < top[0] * SECOND_RATIO:
            out[r.t] = top[1]
    return out


def _canonical(names: Counter) -> dict[str, str]:
    """Spelling variants and cut-off names → the most often read full spelling ("Priya Shar" → "Priya Sharma",
    an OCR slip "Priya Sharrna" → "Priya Sharma")."""
    from transcriber.screen_names import same_person

    groups: dict[str, list[str]] = defaultdict(list)  # spellings that differ only in accents or case
    for name in names:
        groups[fold(name)].append(name)
    merged: Counter = Counter()
    first: dict[str, str] = {}
    for key, spellings in groups.items():
        best = best_spelling(spellings, names)
        merged[best] = sum(names[n] for n in spellings)
        for n in spellings:
            first[n] = best
    order = [n for n, _ in merged.most_common()]
    out: dict[str, str] = {}
    for name in order:
        target = next((o for o in order if o != name and out.get(o, o) == o and (
            same_person(name, o) or (merged[o] > merged[name] and SequenceMatcher(None, fold(name), fold(o)).ratio() >= 0.9))),
            None)
        out[name] = out.get(target, target) if target else name
    return {n: out[first[n]] for n in first}


def summarise(readings: list[Reading], talking: dict[float, str], scanned: int) -> dict:
    """The attendees seen in the list (and the invited who never appeared), for the transcript's `participants`."""
    seen: Counter = Counter()
    times: dict[str, list[float]] = defaultdict(list)
    invited: Counter = Counter()
    counts = []
    for r in readings:
        for name in dict.fromkeys(row.name for row in r.panel.rows):  # once per frame
            seen[name] += 1
            times[name].append(r.t)
        for name in r.panel.invited:
            invited[name] += 1
        if r.panel.count:
            counts.append(r.panel.count)
    canon = _canonical(seen + invited)
    merged: dict[str, dict] = {}
    for name, n in seen.items():
        key = canon.get(name, name)
        entry = merged.setdefault(key, {"name": key, "frames": 0, "first_seen": None, "last_seen": None, "spoke": 0})
        entry["frames"] += n
        first, last = min(times[name]), max(times[name])
        entry["first_seen"] = first if entry["first_seen"] is None else min(entry["first_seen"], first)
        entry["last_seen"] = last if entry["last_seen"] is None else max(entry["last_seen"], last)
    for name in talking.values():
        key = canon.get(name, name)
        if key in merged:
            merged[key]["spoke"] += 1
    least = MIN_FRAMES
    names = sorted((e for e in merged.values() if e["frames"] >= least), key=lambda e: e["first_seen"])
    present = {e["name"].lower() for e in names}
    only_invited = sorted({canon.get(n, n) for n, c in invited.items() if c >= least} - {n["name"] for n in names},
                          key=str.lower)
    return {"status": "done" if readings else "no_list", "frames_scanned": scanned, "frames_with_list": len(readings),
            "frames_speaking": len(talking), "count": max(counts) if counts else None,
            "kinds": sorted({r.panel.kind for r in readings}),
            "names": [dict(e, first_seen=round(e["first_seen"], 1), last_seen=round(e["last_seen"], 1)) for e in names],
            "invited": [n for n in only_invited if n.lower() not in present]}


def canonical_map(readings: list[Reading]) -> dict[str, str]:
    """Every spelling read in the list → its canonical name (for matching list speakers with attendees)."""
    seen: Counter = Counter(row.name for r in readings for row in r.panel.rows)
    return _canonical(seen)


# ---------------------------------------------------------------------------
# Scanning the recording
# ---------------------------------------------------------------------------

def _thumb(path: Path):
    """A small grey version of a frame, to tell whether the view changed since the text was last read."""
    import numpy as np
    from PIL import Image

    with Image.open(path) as im:
        return np.asarray(im.convert("L").resize((48, 27)), dtype=np.float32) / 255.0


def extract_chunk(src: Path, start: float, length: float, every: float, out: Path) -> list[tuple[float, Path]]:
    """Frames every `every` seconds from `start` for `length` seconds, as JPEGs in `out`: [(time, path)]."""
    out.mkdir(parents=True, exist_ok=True)
    for old in out.glob("*.jpg"):
        old.unlink()
    result = subprocess.run(
        ["ffmpeg", "-v", "error", "-y", "-ss", f"{start:.2f}", "-t", f"{length:.2f}", "-i", str(src),
         "-vf", f"fps=1/{every},scale='min(1280,iw)':-2", "-q:v", "3", str(out / "%06d.jpg")],
        capture_output=True, timeout=1800)
    if result.returncode != 0:
        log.warning("Frame extraction failed: %s", result.stderr[-300:])
    return [(round(start + i * every, 2), p) for i, p in enumerate(sorted(out.glob("*.jpg")))]


def scan(src: Path, duration: float, work: Path, progress: Callable[[int, int], None] | None = None,
         ocr: Callable | None = None, every: float = SCAN_EVERY) -> tuple[list[Reading], int]:
    """Reads the participants list on every scanned frame of the recording. The text is read again only when the
    view changed (or every REOCR_SECONDS); the speaking colours are measured on every frame. Returns (readings,
    frames scanned). `ocr(paths)` → {path: lines} (default: the OCR helper)."""
    from transcriber.screen_names import load_rgb, run_ocr

    read = ocr or run_ocr
    total = max(1, int(duration / every))
    done = 0
    readings: list[Reading] = []
    last_thumb, last_lines, last_read = None, None, -1e9
    folder = work / "scan"
    try:
        start = 0.0
        while start < duration:
            frames = extract_chunk(src, start, min(CHUNK_SECONDS, duration - start), every, folder)
            thumbs = {p: _thumb(p) for _t, p in frames}
            todo, prev, prev_t = [], last_thumb, last_read
            for t, p in frames:  # which frames need their text read
                if prev is None or float(abs(thumbs[p] - prev).mean()) >= CHANGE_THRESHOLD or t - prev_t >= REOCR_SECONDS:
                    todo.append(p)
                    prev, prev_t = thumbs[p], t
            last_thumb, last_read = prev, prev_t
            texts = read(todo) if todo else {}
            todo_set = set(todo)
            for t, p in frames:
                if p in todo_set:
                    last_lines = texts.get(str(p)) or []
                panel = find_list(last_lines or [])
                if panel:
                    measure(load_rgb(p), panel)
                    readings.append(Reading(t, panel))
                done += 1
                if progress:
                    progress(min(done, total), total)
            start += CHUNK_SECONDS
    finally:
        shutil.rmtree(folder, ignore_errors=True)
    return readings, done


def list_evidence(talking: dict[float, str], segments: list[dict], canon: dict[str, str]) -> dict[str, Counter]:
    """{speaker label: Counter(name)} — the name the list marked as speaking, for the diarized speaker talking at
    that moment (inside a turn of at least 1.5 s, not in its first half second, when the highlight may still be on
    the previous speaker)."""
    turns = sorted((s for s in segments if s.get("speaker") and s.get("end", 0) - s.get("start", 0) >= 1.5),
                   key=lambda s: s["start"])
    out: dict[str, Counter] = defaultdict(Counter)
    import bisect

    starts = [s["start"] for s in turns]
    for t, name in talking.items():
        i = bisect.bisect_right(starts, t) - 1
        if i >= 0 and turns[i]["start"] + 0.5 <= t <= turns[i]["end"]:
            out[turns[i]["speaker"]][canon.get(name, name)] += 1
    return out


def at(talking: dict[float, str], t: float, within: float = 1.0) -> str:
    """The list speaker at time `t` (the nearest scanned frame within `within` seconds), or ""."""
    best, gap = "", within + 1
    for when, name in talking.items():
        if abs(when - t) <= within and abs(when - t) < gap:
            best, gap = name, abs(when - t)
    return best
