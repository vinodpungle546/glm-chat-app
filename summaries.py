#!/usr/bin/env python3
"""Meeting summaries (minutes) from a transcript, with any chat model.

A transcript that fits the model's window is summarised in one streamed call.
A longer one is split on speaker turns (long_context.split_lines), each part is
condensed to notes (map), and the minutes are written from all notes (reduce),
so nothing in the meeting is skipped or truncated.
"""

from __future__ import annotations

import time
from typing import Callable, Iterator

import long_context

MINUTES_INSTRUCTIONS = (
    "Write meeting minutes in Markdown with these sections:\n"
    "## Summary\n3-5 sentences.\n"
    "## Decisions\nBullets (write 'None recorded' if there are none).\n"
    "## Action items\nA table: | Owner | Action | Due | Timestamp |\n"
    "## Open questions\nBullets.\n"
    "## Key discussion points\nBullets with [hh:mm:ss] timestamps.\n"
    "Use the speaker names as they appear. Never invent owners, dates or facts; write 'not stated' instead."
)
SYSTEM = "You write accurate, concise meeting minutes from transcripts."
# The cloud keeps a fixed character budget; above it the transcript is processed in parts
# rather than truncated (local models use their real context window).
CLOUD_CONTEXT_TOKENS = 48_000


def plan(transcript_md: str, context_length: int, speeds: dict | None = None) -> dict:
    """{'mode': 'direct'|'map_reduce', 'parts', 'notice', ...} for a minutes request."""
    return long_context.plan("meeting minutes summary", transcript_md, 800, context_length, speeds)


def minutes_messages(source: str, title: str, from_notes: bool) -> list[dict]:
    """The prompt for free-form minutes: the system rules plus either the full transcript or, for long meetings,
    the notes made from each part.
    """
    what = ("Notes taken from every part of the meeting, in order" if from_notes
            else "The full transcript")
    return [{"role": "system", "content": SYSTEM},
            {"role": "user", "content": f"Meeting: {title}\n\n{MINUTES_INSTRUCTIONS}\n\n"
                                        f"{what}:\n\n{source}"}]


def summarize(transcript_md: str, title: str, context_length: int,
              stream: Callable[[list[dict]], Iterator[str]],
              complete: Callable[[list[dict]], str],
              emit: Callable[[dict], None] = lambda _e: None,
              cache: dict | None = None, speeds: dict | None = None) -> str:
    """Stream minutes; emits progress for long transcripts. Returns the full Markdown."""
    p = plan(transcript_md, context_length, speeds)
    source, from_notes = transcript_md, False
    if p["mode"] != "direct":
        p = dict(p, mode="map_reduce")
        emit({"type": "notice", "text": p["notice"] if "parts" in p["notice"] else
              f"ℹ️ Long transcript: processing it in {p['parts']} parts, then combining."})
        started = time.time()

        def progress(done: int, total: int) -> None:
            """Reports 'reading part N of M' (and time left) while a long transcript is summarised in parts."""
            left = ""
            if done:
                left = f" · ~{long_context.fmt_duration((time.time() - started) / done * (total - done))} left"
            emit({"type": "progress", "done": done, "total": total,
                  "text": f"Reading part {min(done + 1, total)} of {total}{left}"})

        notes = long_context.map_reduce_notes(
            transcript_md, "meeting minutes: summary, decisions, action items, open questions",
            long_context.content_budget(context_length), complete, progress, cache=cache)
        emit({"type": "progress", "done": len(notes), "total": len(notes), "text": "Writing the minutes"})
        source = "\n\n".join(notes)  # each block is headed "### Part i of n"
        from_notes = True
    chunks = []
    for chunk in stream(minutes_messages(source, title, from_notes)):
        chunks.append(chunk)
        emit({"type": "delta", "text": chunk})
    return "".join(chunks)
