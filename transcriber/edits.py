"""Your own edits of a transcript: wording, who said a line, splitting a line between two speakers, merging lines.

Edits are kept as a list of operations on the transcript (`edits.ops`) and applied last, after the corrections
from other transcripts and the models (server.apply_corrections), so nothing the app does later overwrites them.
Each operation names its line by the line's start time (with its position as a hint), not by its number, so it
still finds the line after corrections are recomputed or earlier lines were split. One that can't find its line
any more is kept and reported as stale.

Operations (the API fills in the ids and the "before" values):
- text:    {"kind": "text", "at": start, "i": index, "text": "...", "before": "..."}
- speaker: {"kind": "speaker", "at": [starts], "i": [indexes], "speaker": "SPEAKER_01", "before": [labels]}
- split:   {"kind": "split", "at": start, "i": index, "parts": [{"text", "speaker", "start", "end"}, ...],
            "before": {"text", "speaker"}}
- merge:   {"kind": "merge", "at": [start, next start], "i": index, "text": "...", "speaker": "...", "before": [...]}

Pure code; tested in tests/test_edits.py.
"""

from __future__ import annotations

import uuid
from datetime import datetime

KINDS = ("text", "speaker", "split", "merge")
TOLERANCE = 0.005


def new_id() -> str:
    return uuid.uuid4().hex[:10]


def find(segments: list[dict], at: float, hint: int | None = None) -> int | None:
    """The index of the segment that starts at `at` (the one nearest `hint` if several do), or None."""
    hits = [i for i, s in enumerate(segments) if abs(float(s.get("start") or 0) - float(at)) <= TOLERANCE]
    if not hits:
        return None
    return min(hits, key=lambda i: abs(i - hint)) if hint is not None else hits[0]


def apply(segments: list[dict], ops: list[dict]) -> tuple[list[dict], list[int | None], dict[str, str]]:
    """(edited segments, origin, state). origin[i] = the index in `segments` that line i still is unchanged (None
    when you edited, split or merged it, so marks from the corrections don't apply to it); state = {op id:
    "applied" | "stale"}."""
    segs = [dict(s) for s in segments]
    origin: list[int | None] = list(range(len(segs)))
    state: dict[str, str] = {}
    for op in ops:
        kind = op.get("kind")
        ok = False
        if kind == "text":
            i = find(segs, op["at"], op.get("i"))
            if i is not None:
                segs[i] = dict(segs[i], text=op["text"], edited=True)
                origin[i], ok = None, True
        elif kind == "speaker":
            hints = op.get("i") or [None] * len(op["at"])
            found = [find(segs, at, hint) for at, hint in zip(op["at"], hints)]
            for i in found:
                if i is not None:
                    segs[i] = dict(segs[i], speaker=op["speaker"], edited=True)
            ok = any(i is not None for i in found)
        elif kind == "split":
            i = find(segs, op["at"], op.get("i"))
            if i is not None:
                parts = [dict(segs[i], text=p["text"], speaker=p["speaker"], start=float(p["start"]),
                              end=float(p["end"]), edited=True) for p in op["parts"]]
                segs[i:i + 1] = parts
                origin[i:i + 1] = [None] * len(parts)
                ok = True
        elif kind == "merge":
            i = find(segs, op["at"][0], op.get("i"))
            if i is not None and i + 1 < len(segs) and abs(float(segs[i + 1].get("start") or 0) - float(op["at"][1])) <= TOLERANCE:
                merged = dict(segs[i], text=op["text"], speaker=op["speaker"], end=segs[i + 1]["end"], edited=True)
                segs[i:i + 2] = [merged]
                origin[i:i + 2] = [None]
                ok = True
        state[op["id"]] = "applied" if ok else "stale"
    return segs, origin, state


def make_op(segs: list[dict], body: dict) -> dict:
    """The stored operation for an edit `body` ({kind, lines, text?, speaker?, parts?}), checked against the lines
    as they are now. ValueError (a message for the user) when it can't be done."""
    lines = body.get("lines") or []
    if not lines or any(not 0 <= i < len(segs) for i in lines):
        raise ValueError("That line doesn't exist any more. Reload the meeting.")
    kind = body.get("kind")
    first = segs[lines[0]]
    op = {"id": new_id(), "kind": kind, "created": datetime.now().isoformat(timespec="seconds")}
    if kind == "text":
        text = " ".join((body.get("text") or "").split())
        if not text:
            raise ValueError("A line can't be empty.")
        op.update(at=first["start"], i=lines[0], text=text, before=first.get("text", ""))
    elif kind == "speaker":
        if not body.get("speaker"):
            raise ValueError("Choose who said it.")
        op.update(at=[segs[i]["start"] for i in lines], i=list(lines), speaker=body["speaker"],
                  before=[segs[i].get("speaker", "") for i in lines])
    elif kind == "split":
        parts = body.get("parts") or []
        if len(parts) < 2 or any(not p.get("speaker") for p in parts):
            raise ValueError("A split needs two parts, each with who said it.")
        words = [str(p.get("text") or "").split() for p in parts]
        if any(not ws for ws in words) or \
                " ".join(w for ws in words for w in ws) != " ".join((first.get("text") or "").split()):
            raise ValueError("The parts must be the line's words, in order.")
        out, done, start = [], 0, float(first["start"])
        for k, (p, ws) in enumerate(zip(parts, words)):
            done += len(ws)
            end = split_times(first, done) if k < len(parts) - 1 else float(first["end"])
            out.append({"text": " ".join(ws), "speaker": p["speaker"], "start": start, "end": end})
            start = end
        op.update(at=first["start"], i=lines[0], parts=out,
                  before={"text": first.get("text", ""), "speaker": first.get("speaker", "")})
    elif kind == "merge":
        i = lines[0]
        if i + 1 >= len(segs):
            raise ValueError("There's no next line to merge with.")
        nxt = segs[i + 1]
        text = body.get("text") or f"{first.get('text', '')} {nxt.get('text', '')}"
        op.update(at=[first["start"], nxt["start"]], i=i, text=" ".join(text.split()),
                  speaker=body.get("speaker") or first.get("speaker", ""),
                  before=[{"text": s.get("text", ""), "speaker": s.get("speaker", "")} for s in (first, nxt)])
    else:
        raise ValueError("Unknown edit.")
    return op


def split_times(seg: dict, words_before: int) -> float:
    """Where a line splits in time: in proportion to its words (the times of single words aren't kept)."""
    words = (seg.get("text") or "").split()
    share = words_before / max(1, len(words))
    start, end = float(seg.get("start") or 0), float(seg.get("end") or 0)
    return round(start + (end - start) * share, 2)


def describe(op: dict, names: dict[str, str]) -> str:
    """One line for the list of your edits."""
    name = lambda label: names.get(label, label)  # noqa: E731
    if op["kind"] == "text":
        return f"Wording: “{op.get('before', '')[:60]}” → “{op['text'][:60]}”"
    if op["kind"] == "speaker":
        n = len(op["at"])
        return f"{n} line{'s' if n != 1 else ''} now said by {name(op['speaker'])}"
    if op["kind"] == "split":
        return "Split: " + " / ".join(f"{name(p['speaker'])}: “{p['text'][:40]}”" for p in op["parts"])
    return f"Merged two lines: “{op['text'][:60]}”"
