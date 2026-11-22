#!/usr/bin/env python3
"""Meeting transcripts of any size with a local model (no silent truncation).

* fits the model's window      -> sent whole ("direct")
* too big, summary-type ask    -> map-reduce: summarise ~parts, then combine
* too big, specific question   -> retrieval: local embeddings pick the most
                                  relevant passages (with timestamps)

plan() estimates the work up front so the UI can tell the user how long it
will take - and why - before anything runs.
"""

from __future__ import annotations

import hashlib
import math
import os
import re
from typing import Callable

import requests

from llm_backends import LOCAL_LLM_BASE_URL

CHARS_PER_TOKEN = 3  # transcripts tokenize densely (timestamps, names): measured ~2.7
CONTENT_SHARE = 0.75          # of the window; the rest is prompt + answer
# Smaller parts: prompt reading slows down superlinearly with length on this
# laptop, and each finished part is cached, so a failure loses little.
MAP_PART_TOKENS = int(os.environ.get("LONG_CONTEXT_PART_TOKENS", "12000"))
RETRIEVAL_CHUNK_TOKENS = 700
RETRIEVAL_TOP_K = 10
EMBED_MODEL = os.environ.get("LOCAL_EMBED_MODEL", "nomic-embed-text")
# Measured on the M4 MacBook Air (16 GB), qwen3:8b Q4_K_M, 32k context, 2026-11-18:
# reading 100-123 tok/s, writing 13 tok/s (short context) to 7.5 tok/s (16k context).
PREFILL_TPS = float(os.environ.get("LOCAL_PREFILL_TPS", "105"))
GEN_TPS = float(os.environ.get("LOCAL_GEN_TPS", "10"))
EMBED_TPS = float(os.environ.get("LOCAL_EMBED_TPS", "3000"))
PART_NOTES_TOKENS = 500
ANSWER_TOKENS = 600
CONFIRM_ABOVE_SECONDS = 30

SUMMARY_RE = re.compile(
    r"\b(summar\w*|minutes|mom|recap|overview|action items?|takeaways?|key points|decisions?|"
    r"highlights|tl;?dr|brief me|what happened|whole meeting|entire meeting|full meeting)\b",
    re.IGNORECASE,
)


def estimate_tokens(text: str) -> int:
    """Rough token count of a text (about CHARS_PER_TOKEN characters per token). Good enough for planning; no
    tokenizer needed.
    """
    return math.ceil(len(text or "") / CHARS_PER_TOKEN)


def content_budget(context_length: int) -> int:
    """How many tokens of the model's window can be used for documents and transcripts (the rest is kept for
    instructions and the answer).
    """
    return int(context_length * CONTENT_SHARE)


def is_summary_request(prompt: str) -> bool:
    """True when the question asks for a summary, minutes or action items (then the whole transcript must be
    read, not just the relevant passages).
    """
    return bool(SUMMARY_RE.search(prompt or ""))


def split_lines(text: str, max_tokens: int) -> list[str]:
    """Split on line boundaries (speaker turns) into pieces <= max_tokens.

    Lossless: "".join(pieces) == text. A single over-long line is hard-split.
    """
    max_chars = max(1, max_tokens * CHARS_PER_TOKEN)
    pieces, current = [], ""
    for line in text.splitlines(keepends=True):
        while len(line) > max_chars:
            if current:
                pieces.append(current)
                current = ""
            pieces.append(line[:max_chars])
            line = line[max_chars:]
        if len(current) + len(line) > max_chars and current:
            pieces.append(current)
            current = ""
        current += line
    if current:
        pieces.append(current)
    return pieces


def fmt_duration(seconds: float) -> str:
    """A duration for people: '45 s', '3 min' or '1.2 h'."""
    seconds = max(1, int(round(seconds)))
    if seconds < 90:
        return f"{seconds} s"
    return f"{seconds / 60:.0f} min" if seconds < 3600 else f"{seconds / 3600:.1f} h"


AUDIO_TOKENS_PER_SECOND = 3.5   # a transcript (with timestamps and speakers) per second of speech
NOTES_ANSWER_TOKENS = 1500      # meeting notes JSON


def summary_seconds(duration: float, context_length: int, speeds: dict | None = None) -> dict:
    """{low, high, parts} for meeting notes on a local model, before the transcript exists."""
    speeds = speeds or {}
    prefill = speeds.get("prefill_tps") or PREFILL_TPS
    gen = speeds.get("gen_tps") or GEN_TPS
    tokens = duration * AUDIO_TOKENS_PER_SECOND
    budget = content_budget(context_length)
    if tokens + 2000 <= budget:
        parts, seconds = 1, (tokens + 2000) / prefill + NOTES_ANSWER_TOKENS / gen
    else:
        part = max(2000, min(MAP_PART_TOKENS, budget - 4000))
        parts = int(-(-tokens // part))
        seconds = tokens / prefill + parts * NOTES_ANSWER_TOKENS / gen + NOTES_ANSWER_TOKENS / gen
    seconds += 8  # loading the model
    return {"low": round(seconds * 0.7), "high": round(seconds * 1.5), "parts": parts}


def plan(prompt: str, transcript_text: str, other_tokens: int, context_length: int,
         speeds: dict | None = None) -> dict:
    """Decide how to handle the transcript(s) for this question and estimate time.

    speeds: measured {"prefill_tps", "gen_tps"} for the selected model on this
    machine (llm_backends.MEASURED_SPEEDS); defaults are used until measured.
    """
    speeds = speeds or {}
    prefill_tps = speeds.get("prefill_tps") or PREFILL_TPS
    gen_tps = speeds.get("gen_tps") or GEN_TPS
    t_tokens = estimate_tokens(transcript_text)
    budget = content_budget(context_length)
    fits = t_tokens + other_tokens <= budget
    if fits:
        # Only the reading (prefill) depends on size; the answer itself is normal speed.
        seconds = (t_tokens + other_tokens) / prefill_tps
        mode, parts = "direct", 1
    elif is_summary_request(prompt):
        part_tokens = max(2000, min(MAP_PART_TOKENS, budget - other_tokens - 2000))
        parts = len(split_lines(transcript_text, part_tokens))
        seconds = (t_tokens / prefill_tps + parts * PART_NOTES_TOKENS / gen_tps
                   + (parts * PART_NOTES_TOKENS + other_tokens) / prefill_tps + ANSWER_TOKENS / gen_tps)
        mode = "map_reduce"
    else:
        parts = len(split_lines(transcript_text, RETRIEVAL_CHUNK_TOKENS))
        seconds = (t_tokens / EMBED_TPS
                   + (RETRIEVAL_TOP_K * RETRIEVAL_CHUNK_TOKENS + other_tokens) / prefill_tps
                   + ANSWER_TOKENS / gen_tps)
        mode = "retrieve"
    low, high = seconds * 0.7, seconds * 1.5
    return {
        "show_notice": mode != "direct" or high > 15,
        "mode": mode,
        "transcript_tokens": t_tokens,
        "context_length": context_length,
        "parts": parts,
        "seconds_low": low,
        "seconds_high": high,
        "confirm": high > CONFIRM_ABOVE_SECONDS,
        "notice": notice(mode, t_tokens, context_length, parts, low, high),
    }


def notice(mode, t_tokens, context_length, parts, low, high) -> str:
    """The message shown before a long local job: how big the transcript is, what will be done (read directly, in
    parts, or by searching) and how long it should take.
    """
    est = f"≈ {fmt_duration(low)}–{fmt_duration(high)}"
    size = f"~{t_tokens / 1000:.0f}k tokens" if t_tokens >= 1000 else f"~{t_tokens} tokens"
    window = f"{context_length // 1000}k"
    if mode == "direct":
        return (f"ℹ️ Transcript is {size}; it fits the local model's {window} window. "
                f"Reading it takes {est} before the answer starts; follow-up questions are faster.")
    if mode == "map_reduce":
        return (f"ℹ️ Transcript is {size}, larger than the local model's {window} window. "
                f"Processing the whole meeting in {parts} parts, then combining {est}. Nothing is skipped.")
    return (f"ℹ️ Transcript is {size}, larger than the local model's {window} window. "
            f"Searching all {parts} passages locally and answering from the most relevant ones {est}. "
            "Ask for a summary/minutes to process the whole meeting.")


# ---------------------------------------------------------------------------
# Map-reduce
# ---------------------------------------------------------------------------

MAP_PROMPT = (
    "You are reading part {i} of {n} of a confidential meeting transcript. Extract, keeping the "
    "[hh:mm:ss] timestamps and speaker names: key points, decisions, action items (owner, due "
    "date), open questions, and anything relevant to this request: \"{question}\". Be concise; "
    "bullet points only; do not invent anything."
)


def map_reduce_notes(
    transcript_text: str,
    question: str,
    budget_tokens: int,
    complete: Callable[[list[dict]], str],
    progress: Callable[[int, int], None] | None = None,
    cache: dict | None = None,
) -> list[str]:
    """Summarise each part with the local model; returns one note block per part.

    cache (optional) keeps each finished part, keyed by its content, so a retry
    after a failure or cancel only processes the parts that are missing.
    """
    part_tokens = max(2000, min(MAP_PART_TOKENS, budget_tokens - 2000))
    parts = split_lines(transcript_text, part_tokens)
    notes = []
    for i, part in enumerate(parts, start=1):
        if progress:
            progress(i - 1, len(parts))
        key = hashlib.sha1(f"{question}\x00{part}".encode()).hexdigest()
        if cache is not None and key in cache:
            text = cache[key]
        else:
            messages = [
                {"role": "system", "content": MAP_PROMPT.format(i=i, n=len(parts), question=question)},
                {"role": "user", "content": part},
            ]
            text = complete(messages).strip()
            if cache is not None:
                cache[key] = text
        notes.append(f"### Part {i} of {len(parts)}\n{text}")
    if progress:
        progress(len(parts), len(parts))
    return notes


# ---------------------------------------------------------------------------
# Retrieval
# ---------------------------------------------------------------------------

def _cosine(a, b) -> float:
    """Cosine similarity of two vectors: 1 = same direction (very similar text), 0 = unrelated."""
    dot = sum(x * y for x, y in zip(a, b))
    na, nb = math.sqrt(sum(x * x for x in a)), math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


def embed(texts: list[str], model: str = EMBED_MODEL) -> list[list[float]]:
    """Turns texts into vectors with the local embedding model (Ollama /api/embed), 64 at a time."""
    out = []
    for i in range(0, len(texts), 64):
        response = requests.post(f"{LOCAL_LLM_BASE_URL}/api/embed",
                                 json={"model": model, "input": texts[i:i + 64]}, timeout=300)
        response.raise_for_status()
        out.extend(response.json()["embeddings"])
    return out


def _keyword_scores(query: str, chunks: list[str]) -> list[float]:
    """Fallback relevance when embeddings aren't available: how often the question's words (3+ letters) appear in
    each chunk, scaled down for long chunks.
    """
    terms = {t for t in re.findall(r"\w{3,}", query.lower())}
    return [sum(c.lower().count(t) for t in terms) / (1 + len(c) / 2000) for c in chunks]


def retrieve(
    transcript_text: str,
    question: str,
    cache: dict | None = None,
    top_k: int = RETRIEVAL_TOP_K,
    embed_fn: Callable[[list[str]], list[list[float]]] | None = None,
) -> tuple[list[str], str]:
    """Return the most relevant passages (in transcript order) and the method used.

    Uses local embeddings (cached per transcript); falls back to keyword
    scoring if the embedding model isn't installed.
    """
    chunks = split_lines(transcript_text, RETRIEVAL_CHUNK_TOKENS)
    if len(chunks) <= top_k:
        return chunks, "all"
    embed_fn = embed_fn or embed
    key = hashlib.sha1(transcript_text.encode()).hexdigest()
    try:
        if cache is not None and key in cache:
            vectors = cache[key]
        else:
            vectors = embed_fn([f"search_document: {c}" for c in chunks])
            if cache is not None:
                cache[key] = vectors
        query_vec = embed_fn([f"search_query: {question}"])[0]
        scores = [_cosine(query_vec, v) for v in vectors]
        method = "embeddings"
    except (requests.RequestException, KeyError, ValueError):
        scores = _keyword_scores(question, chunks)
        method = "keywords"
    best = sorted(range(len(chunks)), key=lambda i: scores[i], reverse=True)[:top_k]
    return [chunks[i] for i in sorted(best)], method
