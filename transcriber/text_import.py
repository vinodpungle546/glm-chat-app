"""Turn an existing conversation text into a transcript (no audio, no models).

Accepted input (auto-detected):
  * WebVTT / SRT captions, e.g. Teams or Zoom exports. The speaker comes from
    `<v Name>` tags or a leading "Name:".
  * Line-based text:
      [00:01:02] Name: text      00:01:02 Name: text      Name (00:01:02): text
      Name: text                 Name   0:03   (header line, text follows below)
      12/03/2024, 10:15 - Name: text   (chat exports; the clock time is dropped)
  * Anything else becomes plain paragraphs without speakers.

The output has the same shape as an audio transcript, so renaming speakers,
"Add to chat", minutes and downloads work the same way.
"""

from __future__ import annotations

import hashlib
import re
from datetime import datetime

MAX_SEGMENT_SECONDS = 60  # merge consecutive caption cues of one speaker up to this length

_TIME = r"(?:\d{1,2}:)?\d{1,2}:\d{2}(?:[.,]\d{1,3})?"
_CUE_RE = re.compile(rf"^\s*({_TIME})\s*-->\s*({_TIME})")
_VOICE_RE = re.compile(r"<v(?:\.[^ >]*)?\s+([^>]+)>")
_TAG_RE = re.compile(r"</?[^>]+>")
_NAME = r"[A-Za-zÀ-￿][\w.'’\- ]{0,38}?"
# "[00:01:02] Name: text" / "00:01:02 - Name: text" / "(00:01) Name: text"
_TIME_NAME_RE = re.compile(rf"^\s*[\[(]?({_TIME})[\])]?\s*[-–]?\s*({_NAME})\s*:\s+(.+)$")
# "Name [00:01:02]: text" / "Name (00:01:02): text" / "Name 00:01:02: text"
_NAME_TIME_RE = re.compile(rf"^\s*({_NAME})\s*[\[(]?({_TIME})[\])]?\s*:\s*(.+)$")
# "[00:01:02] text" (timestamp, no speaker)
_TIME_ONLY_RE = re.compile(rf"^\s*[\[(]({_TIME})[\])]\s*(.+)$")
# "Name: text"
_NAME_RE = re.compile(rf"^\s*({_NAME})\s*:\s+(.+)$")
# Header line "Name   0:03" / "Name 00:01:02" / "[Name] 10:05:12" with the text below it
_HEADER_RE = re.compile(rf"^\s*\[?({_NAME})\]?\s{{1,}}({_TIME})\s*$")
# Chat exports: "12/03/2024, 10:15 - " / "[12/03/24, 10:15:02 AM] "
_CHAT_PREFIX_RE = re.compile(
    r"^\s*\[?\d{1,4}[/.-]\d{1,2}[/.-]\d{1,4},?\s+\d{1,2}:\d{2}(?::\d{2})?\s*(?:[AaPp][Mm])?\]?\s*[-–]?\s*"
)
_NOT_NAMES = {
    "note", "notes", "agenda", "action", "action item", "action items", "summary", "decision",
    "decisions", "todo", "to do", "date", "time", "subject", "re", "fyi", "ps", "http", "https",
    "attendees", "participants", "location", "topic", "title", "minutes", "next steps", "update",
    "status", "owner", "due", "example", "warning", "important", "webvtt", "budget", "revenue",
    "risk", "risks", "issue", "issues", "goal", "goals", "objective", "objectives", "context",
    "background", "result", "results", "conclusion", "overview", "purpose", "outcome", "outcomes",
    "next", "deadline", "cost", "costs", "timeline", "scope", "priority", "feedback", "comment",
    "comments", "reminder", "link", "email", "phone", "address", "cc", "to", "from", "sent",
    "tip", "step", "steps", "total", "result", "answer key", "q&a", "recap", "highlights",
}


def parse_time(raw: str) -> float:
    """A caption time such as '00:03:12.500' or '3:12,5' in seconds."""
    parts = raw.replace(",", ".").split(":")
    seconds = 0.0
    for part in parts:
        seconds = seconds * 60 + float(part)
    return seconds


def _is_name(candidate: str) -> bool:
    """True when a text before ':' looks like a speaker name (short, no URLs, long numbers or sentence
    punctuation).
    """
    name = candidate.strip()
    if not name or name.lower() in _NOT_NAMES or len(name) > 40:
        return False
    if len(name.split()) > 4 or re.search(r"[!?;,]|\d{3,}|://", name):
        return False
    return True


_NAME_PARTICLES = {"de", "da", "di", "van", "von", "der", "la", "le", "bin", "al", "el", "du"}


def _looks_like_person(name: str) -> bool:
    """'Priya Shah' / 'SPEAKER_01' yes; 'The meeting starts at' (a sentence ending in a time) no."""
    words = name.split()
    return all(w[0].isupper() or w[0].isdigit() or w.lower() in _NAME_PARTICLES for w in words)


def _parse_captions(text: str) -> list[dict]:
    """Reads WebVTT/SRT captions (Teams, Zoom exports) into segments with times and, when the captions name them,
    speakers.
    """
    segments = []
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        match = _CUE_RE.match(lines[i])
        if not match:
            i += 1
            continue
        start, end = parse_time(match.group(1)), parse_time(match.group(2))
        i += 1
        body = []
        while i < len(lines) and lines[i].strip():
            body.append(lines[i].strip())
            i += 1
        raw = " ".join(body)
        voice = _VOICE_RE.search(raw)
        speaker = voice.group(1).strip() if voice else None
        words = " ".join(_TAG_RE.sub("", raw).split())
        if not speaker:
            named = _NAME_RE.match(words)
            if named and _is_name(named.group(1)):
                speaker, words = named.group(1).strip(), named.group(2).strip()
        if words:
            segments.append({"speaker": speaker, "start": start, "end": end, "text": words})
    return _merge_cues(segments)


def _merge_cues(segments: list[dict]) -> list[dict]:
    """Joins consecutive captions of the same speaker into one segment (up to MAX_SEGMENT_SECONDS long)."""
    merged: list[dict] = []
    for seg in segments:
        last = merged[-1] if merged else None
        if (last and last["speaker"] == seg["speaker"]
                and seg["end"] - last["start"] <= MAX_SEGMENT_SECONDS):
            last["text"] = f"{last['text']} {seg['text']}"
            last["end"] = seg["end"]
        else:
            merged.append(dict(seg))
    return merged


def _parse_lines(text: str) -> tuple[list[dict], bool]:
    """Line-based formats; returns (segments, has_timestamps)."""
    rows = []  # (kind, speaker, time, text, original line)
    for original in text.splitlines():
        if not original.strip():
            rows.append(("blank", None, None, "", ""))
            continue
        line = _CHAT_PREFIX_RE.sub("", original, count=1)
        for regex, kind in ((_TIME_NAME_RE, "tn"), (_NAME_TIME_RE, "nt"), (_HEADER_RE, "header"),
                            (_TIME_ONLY_RE, "t"), (_NAME_RE, "n")):
            m = regex.match(line)
            if not m:
                continue
            if kind == "tn" and _is_name(m.group(2)):
                rows.append(("speech", m.group(2).strip(), parse_time(m.group(1)), m.group(3).strip(), original))
            elif kind in ("nt", "header") and _is_name(m.group(1)) and (
                    kind == "nt" or _looks_like_person(m.group(1))):
                body = m.group(3).strip() if kind == "nt" else ""
                rows.append(("speech" if body else "header", m.group(1).strip(),
                             parse_time(m.group(2)), body, original))
            elif kind == "t":
                rows.append(("speech", None, parse_time(m.group(1)), m.group(2).strip(), original))
            elif kind == "n" and _is_name(m.group(1)) and not m.group(1)[0].islower():
                rows.append(("speech", m.group(1).strip(), None, m.group(2).strip(), original))
            else:
                continue
            break
        else:
            rows.append(("text", None, None, line.strip(), original))


    segments: list[dict] = []
    for kind, speaker, time, body, _original in rows:
        if kind in ("speech", "header"):
            segments.append({"speaker": speaker, "start": time, "text": body})
        elif kind == "text":
            if segments and (segments[-1]["speaker"] or not segments[-1].get("closed")):
                segments[-1]["text"] = f"{segments[-1]['text']} {body}".strip()
            else:
                segments.append({"speaker": None, "start": None, "text": body})
        elif kind == "blank" and segments and not segments[-1]["speaker"]:
            segments[-1]["closed"] = True  # paragraph break in unlabelled text
    segments = [s for s in segments if s["text"]]
    has_times = any(s["start"] is not None for s in segments)
    last = 0.0
    for seg in segments:
        seg.pop("closed", None)
        if seg["start"] is None:
            seg["start"] = last
        last = seg["start"]
    for seg, nxt in zip(segments, segments[1:] + [None]):
        seg["end"] = nxt["start"] if nxt and nxt["start"] >= seg["start"] else seg["start"]
    return segments, has_times


def parse(text: str) -> tuple[list[dict], bool]:
    """Segments with `speaker` (a name or None), `start`, `end`, `text`; plus has_timestamps."""
    text = text.replace("\r\n", "\n").replace("\r", "\n").lstrip("﻿")
    if sum(1 for line in text.splitlines() if _CUE_RE.match(line)) >= 1:
        segments = _parse_captions(text)
        if segments:
            return segments, True
    return _parse_lines(text)


def text_id(title: str, text: str) -> str:
    """A stable id for imported text (importing the same text again gives the same transcript)."""
    return hashlib.sha1(f"text|{title}|{text}".encode()).hexdigest()[:16]


def build_transcript(title: str, text: str, source_name: str = "") -> dict:
    """Transcript dict (same shape as pipeline.run's) from conversation text."""
    if not text.strip():
        raise ValueError("The conversation text is empty.")
    segments, has_times = parse(text)
    if not segments:
        raise ValueError("No conversation lines found in this text.")
    order: dict[str, str] = {}
    for seg in segments:
        name = seg["speaker"]
        if name and name not in order:
            order[name] = f"SPEAKER_{len(order):02d}"
    has_speakers = bool(order)
    names = {sid: name for name, sid in order.items() if not re.fullmatch(r"SPEAKER_\d+", name)}
    out_segments = [
        {"speaker": order.get(seg["speaker"], "SPEAKER_00") if has_speakers and seg["speaker"]
         else ("UNKNOWN" if has_speakers else "SPEAKER_00"),
         "start": round(seg["start"], 2), "end": round(seg["end"], 2), "text": seg["text"]}
        for seg in segments
    ]
    speakers = list(order.values()) + (["UNKNOWN"] if any(s["speaker"] == "UNKNOWN" for s in out_segments) else [])
    title = title.strip() or (source_name.rsplit(".", 1)[0] if source_name else "Conversation")
    return {
        "id": text_id(title, text),
        "title": title,
        "source_name": source_name or "pasted text",
        "source_path": "",
        "source_type": "text",
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "duration": max((s["end"] for s in out_segments), default=0) if has_times else 0,
        "language": "?",
        "diarization": has_speakers,
        "timestamps": has_times,
        "speakers": speakers,
        "speaker_names": names,
        "segments": out_segments,
        "processing_seconds": 0,
    }
