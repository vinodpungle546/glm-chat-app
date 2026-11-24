"""A model's review of a corrected meeting transcript, line by line, against the other transcripts of the meeting.

The correction engine (transcriber/correct.py) fixes words, speakers and sentence ends by lining the transcripts
up. A model can then look at each line next to what the other transcripts say there and propose sentence-level
fixes: missing or extra words, a garbled phrase, or a line said by someone else.

The model is never trusted blindly:
- every word it writes must be in the line or in the other transcripts' words for that line (no invented text);
- a new speaker must be one of the meeting's speakers, never your microphone, and differ from the current one;
- each proposal needs a reason; anything else is dropped (and counted).

Used by api/main.py (POST /api/meetings/transcripts/{id}/correction-review). Pure code: the model is a callable.
"""

from __future__ import annotations

import json
import re
from typing import Callable

BATCH = 30                 # lines per model call
KEEP_SHARE = 0.5           # a fix keeps at least this share of the line's own words (no rewriting a line)
MAX_TOKENS = 2500
_STRIP = "\"'“”‘’.,;:!?()[]{}…–—-*_/\\<>"

SYSTEM = (
    "You check a meeting transcript made by speech recognition (Quorum) against other transcripts of the same "
    "meeting (for example a Voice Memos recording or a claude.ai transcript). For each numbered line you get "
    "Quorum's speaker and text, and what the other transcripts say at that point (their speaker names appear "
    "as [Name:]).\n"
    "Propose a fix only where the other transcripts clearly show Quorum's line is wrong:\n"
    "- missing or extra words, a misheard or garbled phrase, sentences run together or split;\n"
    "- the line was said by a different person (use the speaker ids given, never invent one).\n"
    "Rules:\n"
    "- Every word you write must appear in Quorum's line or in the other transcripts' text for that line. "
    "Never add facts, names or words that are in neither.\n"
    "- Keep Quorum's wording where the transcripts agree or where you are unsure. Don't rephrase or tidy up.\n"
    "- Give a short reason for each fix.\n"
    'Reply with JSON only: {"changes": [{"line": <number>, "text": "<the corrected line, or null if only the '
    'speaker changes>", "speaker": "<speaker id, or null>", "reason": "<why>"}]}. '
    'Reply {"changes": []} when nothing needs fixing.'
)


def norm(word: str) -> str:
    """A word compared loosely: lower case, without surrounding punctuation."""
    return word.strip(_STRIP).lower().replace("’", "'")


def words(text: str) -> list[str]:
    return [n for n in (norm(w) for w in (text or "").split()) if n]


def worth_reviewing(line: dict) -> bool:
    """A line whose other transcripts say something different (or name a speaker): the rest is left alone."""
    if not line.get("refs"):
        return False
    mine = words(line["text"])
    for ref in line["refs"]:
        theirs = [w for w in words(re.sub(r"\[[^\]]*:\]", " ", ref["text"]))]
        if theirs != mine or "[" in ref["text"]:
            return True
    return False


def batches(lines: list[dict], size: int = BATCH) -> list[list[dict]]:
    todo = [line for line in lines if worth_reviewing(line)]
    return [todo[i:i + size] for i in range(0, len(todo), size)]


def prompt(batch: list[dict], speakers: dict[str, str], fixed: set[str]) -> list[dict]:
    """The chat messages for one batch. `speakers`: {label: display name}."""
    who = "\n".join(f"- {label}: {name}" + (" (your microphone: never move lines to or from it)" if label in fixed else "")
                    for label, name in speakers.items())
    rows = []
    for line in batch:
        others = "\n".join(f"    {r['name']}: {r['text']}" for r in line["refs"])
        rows.append(f"#{line['line']} [{line['speaker']}] {line['text']}\n  Other transcripts:\n{others}")
    user = f"Speakers:\n{who}\n\nLines:\n" + "\n\n".join(rows)
    return [{"role": "system", "content": SYSTEM}, {"role": "user", "content": user}]


def parse(reply: str) -> list[dict]:
    """The `changes` list from the model's reply; ValueError when it isn't the JSON asked for."""
    text = re.sub(r"<think>.*?</think>", "", reply or "", flags=re.S)
    match = re.search(r"\{.*\}", text, re.S)
    if not match:
        raise ValueError("no JSON object in the reply")
    data = json.loads(match.group(0))
    if not isinstance(data, dict) or not isinstance(data.get("changes"), list):
        raise ValueError('the JSON has no "changes" list')
    return data["changes"]


def validate(changes: list, batch: list[dict], speakers: dict[str, str], fixed: set[str]) -> tuple[list[dict], list[str]]:
    """(valid changes, reasons for the dropped ones). A valid change: {line, before, text?, speaker?,
    speaker_name?, reason}."""
    by_line = {line["line"]: line for line in batch}
    valid, dropped, seen = [], [], set()
    for ch in changes:
        if not isinstance(ch, dict):
            dropped.append("not an object")
            continue
        try:
            n = int(str(ch.get("line", "")).lstrip("#"))
        except ValueError:
            dropped.append(f"bad line number {ch.get('line')!r}")
            continue
        line = by_line.get(n)
        if line is None or n in seen:
            dropped.append(f"line {n} wasn't asked about" if line is None else f"line {n} twice")
            continue
        reason = str(ch.get("reason") or "").strip()
        text = ch.get("text")
        text = str(text).strip() if isinstance(text, str) and text.strip() and text.strip().lower() != "null" else None
        speaker = ch.get("speaker")
        speaker = str(speaker).strip() if isinstance(speaker, str) and speaker.strip() and speaker.lower() != "null" else None
        out = {"line": n, "before": line["text"], "reason": reason[:500]}
        if text is not None and text != line["text"]:
            allowed = set(words(line["text"]))
            for ref in line["refs"]:
                allowed |= set(words(re.sub(r"\[[^\]]*:\]", " ", ref["text"])))
            invented = [w for w in words(text) if w not in allowed]
            if invented:
                dropped.append(f"line {n}: words in neither transcript ({', '.join(invented[:5])})")
                continue
            mine, new = words(line["text"]), set(words(text))
            if mine and sum(1 for w in mine if w in new) / len(mine) < KEEP_SHARE:
                dropped.append(f"line {n}: rewrites most of the line")
                continue
            out["text"] = text
        if speaker is not None and speaker != line["speaker"]:
            if speaker not in speakers:
                dropped.append(f"line {n}: unknown speaker {speaker}")
                continue
            if speaker in fixed or line["speaker"] in fixed:
                dropped.append(f"line {n}: never moved to or from your microphone")
                continue
            out["speaker"], out["speaker_name"] = speaker, speakers[speaker]
        if "text" not in out and "speaker" not in out:
            continue  # nothing to change
        if not reason:
            dropped.append(f"line {n}: no reason given")
            continue
        seen.add(n)
        valid.append(out)
    return valid, dropped


def review(lines: list[dict], speakers: dict[str, str], fixed: set[str], complete: Callable[[list[dict]], str],
           progress: Callable[[int, int], None] | None = None) -> tuple[list[dict], list[str]]:
    """Runs the model over every batch (one retry with the error when a reply isn't valid JSON). Returns
    (valid changes, dropped reasons)."""
    parts = batches(lines)
    valid, dropped = [], []
    for k, batch in enumerate(parts):
        messages = prompt(batch, speakers, fixed)
        reply = complete(messages)
        try:
            changes = parse(reply)
        except ValueError as exc:
            retry = messages + [{"role": "assistant", "content": reply},
                                {"role": "user", "content": f"That wasn't valid ({exc}). Reply with the JSON only."}]
            try:
                changes = parse(complete(retry))
            except ValueError as again:
                dropped.append(f"lines {batch[0]['line']}–{batch[-1]['line']}: no valid answer ({again})")
                changes = []
        ok, bad = validate(changes, batch, speakers, fixed)
        valid += ok
        dropped += bad
        if progress:
            progress(k + 1, len(parts))
    return valid, dropped
