#!/usr/bin/env python3
"""Local search over chats (native + imported), transcripts, summaries and memory.

Everything is split into passages ("chunks"). A query is ranked two ways and the two
rankings are fused (reciprocal rank fusion):
  - keywords: BM25 over the passage words
  - meaning:  cosine similarity of local embeddings (Ollama, nomic-embed-text by default)
Embeddings are computed in the background and cached on disk by passage content, so
search works immediately with keywords and gets semantic matches once indexing is done.
If the embedding model isn't installed or Ollama is down, search is keyword-only.
Nothing here leaves the machine.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import threading
import time
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import requests

import long_context

CHUNK_CHARS = 1100
RRF_K = 60
STOP = set("""a an the and or but if of to in on at by for with from as is are was were be been being it its this
that these those i you he she we they me him her us them my your his our their what which who whom when where why how
all any both each few more most other some such no nor not only own same so than too very can will just do does did
have has had about into over after before again further then once here there also would could should shall may might
must let lets get got please thanks thank yes yeah ok okay""".split())

Embed = Callable[[list[str]], list[list[float]]]


@dataclass
class Chunk:
    """One searchable piece of text (part of a chat, a transcript, notes or memory) with where it came from, its
    date and a link to open it.
    """
    source: str          # chats | transcripts | memory
    kind: str            # chat | imported_chat | transcript | summary | notes | fact | imported_fact | person
    doc_id: str
    title: str
    text: str
    date: str = ""
    link: str = ""
    meta: dict = field(default_factory=dict)

    @property
    def hash(self) -> str:
        """A short fingerprint of the chunk's text; its embedding is stored under this key, so unchanged text is
        never embedded twice.
        """
        return hashlib.sha1(f"{self.kind}\x00{self.text}".encode()).hexdigest()[:20]


def tokens(text: str) -> list[str]:
    """The words of a text for keyword search: lowercase, without stop words like 'the'."""
    return [w for w in re.findall(r"[a-z0-9][a-z0-9'_-]*", text.lower()) if w not in STOP and len(w) > 1]


# ---------------------------------------------------------------------------
# Building passages
# ---------------------------------------------------------------------------

def chat_chunks(record: dict) -> list[Chunk]:
    """Splits a chat into chunks of about CHUNK_CHARS characters, keeping whole messages."""
    imported = bool(record.get("imported"))
    out, buf, start = [], [], 0
    messages = record.get("messages") or []
    for i, m in enumerate(messages):
        line = f"{'You' if m['role'] == 'user' else 'Claude' if imported else 'Assistant'}: {m['content']}"
        if buf and sum(len(b) for b in buf) + len(line) > CHUNK_CHARS:
            out.append((start, "\n".join(buf)))
            buf, start = [], i
        buf.append(line[:CHUNK_CHARS * 2])
    if buf:
        out.append((start, "\n".join(buf)))
    return [Chunk("chats", "imported_chat" if imported else "chat", record["id"], record.get("title") or "Chat",
                  text, (messages[i].get("created_at") or record.get("updated_at") or "")[:19],
                  f"/c/{record['id']}", {"message": i, "imported": imported}) for i, text in out]


def _fmt(t: float) -> str:
    """A time position as 'hh:mm:ss'."""
    t = int(t)
    return f"{t // 3600:02d}:{t % 3600 // 60:02d}:{t % 60:02d}"


def transcript_chunks(meta: dict, segments: list[dict], names: dict[str, str]) -> list[Chunk]:
    """Splits a transcript into chunks of whole lines '[time] Speaker: text', each linked to the time where it
    starts.
    """
    out, buf, first = [], [], None
    for seg in segments:
        line = f"[{_fmt(seg['start'])}] {names.get(seg['speaker'], seg['speaker'])}: {seg['text']}"
        if buf and sum(len(b) for b in buf) + len(line) > CHUNK_CHARS:
            out.append((first, "\n".join(buf)))
            buf, first = [], None
        first = seg["start"] if first is None else first
        buf.append(line)
    if buf:
        out.append((first or 0.0, "\n".join(buf)))
    return [Chunk("transcripts", "transcript", meta["id"], meta.get("title") or "Transcript", text,
                  (meta.get("created_at") or "")[:19], f"/meetings/{meta['id']}?t={int(start)}",
                  {"timestamp": _fmt(start), "seconds": start}) for start, text in out]


def markdown_chunks(doc_id: str, title: str, markdown: str, date: str, kind: str, link: str) -> list[Chunk]:
    """Splits Markdown notes at their headings (and long sections into pieces)."""
    parts = re.split(r"\n(?=#{1,3} )", markdown or "")
    out = []
    for part in parts:
        part = part.strip()
        while part:
            out.append(Chunk("transcripts", kind, doc_id, title, part[:CHUNK_CHARS], (date or "")[:19], link))
            part = part[CHUNK_CHARS:]
    return out


def memory_chunks(facts: list[dict], people: list[dict]) -> list[Chunk]:
    """Memory facts and people as searchable chunks (a person becomes one line: name, role, team, projects,
    notes).
    """
    out = [Chunk("memory", "imported_fact" if f.get("source") else "fact", f.get("id") or f["text"][:20],
                 f.get("section") or "Memory", f["text"], (f.get("updated_at") or "")[:19], "/settings/memory",
                 {"source": f.get("source", "")}) for f in facts]
    for p in people:
        bits = [p["name"]] + [b for b in (p.get("role"), p.get("team") and f"{p['team']} team") if b]
        if p.get("projects"):
            bits.append("projects: " + ", ".join(p["projects"]))
        if p.get("relationships"):
            bits.append("; ".join(p["relationships"]))
        if p.get("notes"):
            bits.append(p["notes"])
        out.append(Chunk("memory", "person", p["id"], p["name"], " · ".join(bits), (p.get("updated_at") or "")[:19],
                         "/settings/memory", {"source": p.get("source", "")}))
    return out


# ---------------------------------------------------------------------------
# Embedding cache + background indexer
# ---------------------------------------------------------------------------

class VectorStore:
    """{model: {chunk_hash: vector}} on disk, loaded lazily."""

    def __init__(self, path: Path):
        """Remembers the file path; the file is read lazily on first use."""
        self.path = path
        self.lock = threading.Lock()
        self.data: dict[str, dict[str, list[float]]] | None = None

    def _load(self) -> dict:
        """Reads the vectors file once (an empty store if it's missing or broken)."""
        if self.data is None:
            try:
                self.data = json.loads(self.path.read_text())
            except (OSError, ValueError):
                self.data = {}
        return self.data

    def get(self, model: str) -> dict[str, list[float]]:
        """All stored vectors of one embedding model: {chunk hash: vector}."""
        with self.lock:
            return self._load().setdefault(model, {})

    def add(self, model: str, vectors: dict[str, list[float]]) -> None:
        """Adds vectors (rounded to 5 decimals to keep the file small)."""
        with self.lock:
            self._load().setdefault(model, {}).update(
                {h: [round(x, 5) for x in v] for h, v in vectors.items()})

    def save(self) -> None:
        """Writes the vectors file safely (temporary file, then rename)."""
        with self.lock:
            if self.data is None:
                return
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.data))
            tmp.replace(self.path)

    def prune(self, model: str, keep: set[str]) -> None:
        """Removes vectors of chunks that no longer exist."""
        with self.lock:
            store = self._load().setdefault(model, {})
            for h in [h for h in store if h not in keep]:
                store.pop(h)


class Indexer:
    """Embeds passages that have no vector yet, in a background thread."""

    def __init__(self, store: VectorStore):
        """Starts idle; `state` is what Settings → Search shows (running, done/total, errors)."""
        self.store = store
        self.lock = threading.Lock()
        self.thread: threading.Thread | None = None
        self.state = {"state": "idle", "done": 0, "total": 0, "model": "", "error": "", "finished_at": ""}

    def snapshot(self) -> dict:
        """A copy of the current indexing state (safe to send to the browser)."""
        with self.lock:
            return dict(self.state)

    def start(self, chunks: list[Chunk], model: str, embed: Embed) -> dict:
        """Starts embedding the chunks that have no vector yet; does nothing if indexing is already running or
        everything is up to date.
        """
        with self.lock:
            if self.thread and self.thread.is_alive():
                return dict(self.state)
            have = self.store.get(model)
            todo = list({c.hash: c for c in chunks if c.hash not in have}.values())
            self.state = {"state": "running" if todo else "done", "done": 0, "total": len(todo), "model": model,
                          "error": "", "finished_at": "" if todo else time.strftime("%Y-%m-%dT%H:%M:%S")}
            if not todo:
                return dict(self.state)
            self.thread = threading.Thread(target=self._run, args=(todo, model, embed), daemon=True)
            self.thread.start()
            return dict(self.state)

    def _run(self, todo: list[Chunk], model: str, embed: Embed) -> None:
        """The background work: embeds chunks 32 at a time, saving every 10 batches and at the end; records an
        error instead of crashing.
        """
        try:
            for i in range(0, len(todo), 32):
                batch = todo[i:i + 32]
                vectors = embed([f"search_document: {c.title}\n{c.text}" for c in batch])
                self.store.add(model, {c.hash: v for c, v in zip(batch, vectors)})
                with self.lock:
                    self.state["done"] = min(len(todo), i + len(batch))
                if i % 320 == 0:
                    self.store.save()
            self.store.save()
            with self.lock:
                self.state.update(state="done", finished_at=time.strftime("%Y-%m-%dT%H:%M:%S"))
        except (requests.RequestException, KeyError, ValueError) as exc:
            self.store.save()
            with self.lock:
                self.state.update(state="error", error=_embed_error(exc, model))


def _embed_error(exc: Exception, model: str) -> str:
    """A readable reason why indexing stopped (Ollama not running, model not downloaded…)."""
    if isinstance(exc, requests.ConnectionError):
        return "Ollama isn't running, so search uses keywords only."
    if isinstance(exc, requests.HTTPError) and exc.response is not None and exc.response.status_code == 404:
        return f"The embedding model {model} isn't downloaded, so search uses keywords only."
    return f"Indexing stopped: {exc}"


def ollama_embedder(model: str) -> Embed:
    """A function that embeds texts with the given local Ollama model."""
    return lambda texts: long_context.embed(texts, model=model)


# ---------------------------------------------------------------------------
# Ranking
# ---------------------------------------------------------------------------

def bm25(query: str, chunks: list[Chunk], k1: float = 1.4, b: float = 0.75) -> list[float]:
    """Keyword relevance of each chunk for the query (the BM25 formula used by search engines: rare words count
    more, very long chunks a bit less).
    """
    q = tokens(query)
    if not q or not chunks:
        return [0.0] * len(chunks)
    docs = [tokens(f"{c.title} {c.text}") for c in chunks]
    avg = sum(len(d) for d in docs) / len(docs) or 1.0
    df = Counter(t for d in docs for t in set(d) if t in q)
    n = len(docs)
    scores = []
    for d in docs:
        tf = Counter(d)
        s = 0.0
        for t in set(q):
            if tf[t]:
                idf = math.log(1 + (n - df[t] + 0.5) / (df[t] + 0.5))
                s += idf * tf[t] * (k1 + 1) / (tf[t] + k1 * (1 - b + b * len(d) / avg))
        if len(q) > 1 and s and " ".join(q) in " ".join(d):
            s *= 1.3  # the words appear together, in order
        scores.append(s)
    return scores


def cosine(a: list[float], b: list[float]) -> float:
    """Cosine similarity of two vectors: close to 1 = similar meaning."""
    dot = sum(x * y for x, y in zip(a, b))
    na, nb = math.sqrt(sum(x * x for x in a)), math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


def snippet(text: str, query: str, width: int = 240) -> str:
    """A short excerpt of the text around the first query word, with '…' where it was cut."""
    flat = re.sub(r"\s+", " ", text).strip()
    terms = [t for t in tokens(query) if len(t) > 2]
    pos = min((flat.lower().find(t) for t in terms if flat.lower().find(t) >= 0), default=0)
    start = max(0, pos - width // 3)
    out = flat[start:start + width]
    return ("…" if start else "") + out + ("…" if start + width < len(flat) else "")


def in_range(date: str, date_from: str = "", date_to: str = "") -> bool:
    """True when a date (YYYY-MM-DD…) falls within the optional from/to dates."""
    day = (date or "")[:10]
    if not day:
        return not (date_from or date_to)
    return (not date_from or day >= date_from[:10]) and (not date_to or day <= date_to[:10])


MIN_SIMILARITY = 0.45  # nomic-embed-text: unrelated passages score ~0.3-0.45


def search(query: str, chunks: list[Chunk], vectors: dict[str, list[float]] | None = None,
           query_vector: list[float] | None = None, limit: int = 8, per_doc: int = 1,
           min_similarity: float = MIN_SIMILARITY) -> list[dict]:
    """Ranked results (best passage per document). Returns [{chunk, score, method, snippet}]."""
    if not query.strip() or not chunks:
        return []
    kw = bm25(query, chunks)
    kw_rank = {i: r for r, i in enumerate(sorted((i for i, s in enumerate(kw) if s > 0), key=lambda i: -kw[i]))}
    sem_rank: dict[int, int] = {}
    sims: dict[int, float] = {}
    if vectors and query_vector:
        for i, c in enumerate(chunks):
            v = vectors.get(c.hash)
            if v:
                sims[i] = cosine(query_vector, v)
        if sims:
            best = max(sims.values())
            floor = max(min_similarity, best - 0.12)
            ranked = sorted((i for i, s in sims.items() if s >= floor), key=lambda i: -sims[i])[:limit * 4]
            sem_rank = {i: r for r, i in enumerate(ranked)}
    fused: dict[int, float] = {}
    for i, r in kw_rank.items():
        fused[i] = fused.get(i, 0) + 1 / (RRF_K + r)
    for i, r in sem_rank.items():
        fused[i] = fused.get(i, 0) + 1 / (RRF_K + r)
    out, per = [], Counter()
    for i in sorted(fused, key=lambda i: -fused[i]):
        c = chunks[i]
        if per[(c.kind, c.doc_id)] >= per_doc:
            continue
        per[(c.kind, c.doc_id)] += 1
        method = "both" if i in kw_rank and i in sem_rank else "keywords" if i in kw_rank else "meaning"
        c = focus_line(c, query)
        out.append({"chunk": c, "score": round(fused[i], 5), "method": method,
                    "similarity": round(sims.get(i, 0.0), 3),
                    "snippet": snippet(c.meta.get("line") or c.text, query) if c.kind == "transcript" else snippet(c.text, query)})
        if len(out) >= limit:
            break
    return out


def focus_line(c: Chunk, query: str) -> Chunk:
    """For a transcript passage: point the link and timestamp at the line that matches best."""
    if c.kind != "transcript":
        return c
    terms = set(tokens(query))
    best, best_score = None, 0
    for m in re.finditer(r"^\[(\d{2}):(\d{2}):(\d{2})\] (.*)$", c.text, re.M):
        score = len(terms & set(tokens(m.group(4))))
        if score > best_score:
            best, best_score = m, score
    if best is None:
        return c
    seconds = int(best.group(1)) * 3600 + int(best.group(2)) * 60 + int(best.group(3))
    base = c.link.split("?")[0]
    return Chunk(c.source, c.kind, c.doc_id, c.title, c.text, c.date, f"{base}?t={seconds}",
                 dict(c.meta, timestamp=f"{best.group(1)}:{best.group(2)}:{best.group(3)}", seconds=seconds,
                      line=best.group(4)[:200]))


def result_json(hit: dict) -> dict:
    """One search hit as JSON for the browser."""
    c: Chunk = hit["chunk"]
    return {"source": c.source, "kind": c.kind, "id": c.doc_id, "title": c.title, "snippet": hit["snippet"],
            "date": c.date, "link": c.link, "score": hit["score"], "method": hit["method"], "meta": c.meta}
