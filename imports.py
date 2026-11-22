#!/usr/bin/env python3
"""Imported claude.ai data: preview, selective import, re-import without duplicates, removal.

Where it goes (all local, under APP_DATA_DIR/imports unless noted):
  memory.json          memory facts [{id, key, text, section, project, source, origin,
                                      imported_at, updated_at, edited}]
  conversations/*.json read-only chat sessions (sessions.py format) with
                       "imported": {source, uuid, imported_at, project}, "read_only": true
  history.json         one line per import (when, what)
  people.json          (people.PEOPLE_FILE) people with role/team/projects/relationships

Re-importing matches memory facts by exact text first, then by similar wording in the same
section (an updated fact replaces the older version), conversations by their claude.ai uuid,
and people by import key or name.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path

import claude_import
import memory_store
import people as people_store
import sessions

IMPORTS_DIR = Path(os.environ.get("IMPORTS_DIR", str(Path(os.environ.get("APP_DATA_DIR", "./data")) / "imports")))
PREVIEW_TTL = 3600
_PREVIEWS: dict[str, dict] = {}
_LOCK = threading.Lock()


def _now() -> str:
    """The current local time as an ISO string."""
    return datetime.now().isoformat(timespec="seconds")


def memory_file() -> Path:
    """The old imported-memory file (kept for reading older installs; facts now live in the memory store)."""
    return IMPORTS_DIR / "memory.json"


def conversations_dir() -> Path:
    """Folder of imported claude.ai chats, stored as read-only sessions."""
    return IMPORTS_DIR / "conversations"


def _read(path: Path, default):
    """Reads a JSON file, or returns `default` when it is missing or broken."""
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def _write(path: Path, data) -> None:
    """Writes a JSON file safely (temporary file first, then renamed into place)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(path)


def session_id_for(conv_uuid: str) -> str:
    """A stable session id for a claude.ai conversation, so importing it again updates the same chat instead of
    making a copy.
    """
    return hashlib.sha1(f"claude.ai:{conv_uuid}".encode()).hexdigest()[:32]


def words(text: str) -> set[str]:
    """The lowercase words (3+ letters) of a text, used to compare facts."""
    return {w for w in re.findall(r"[a-z0-9]+", text.lower()) if len(w) > 2}


def similarity(a: str, b: str) -> float:
    """How much two texts overlap: shared words / all words (Jaccard), 0 to 1."""
    wa, wb = words(a), words(b)
    return len(wa & wb) / len(wa | wb) if wa and wb else 0.0


# ---------------------------------------------------------------------------
# Memory facts (they live in the unified memory store; these are the claude.ai ones)
# ---------------------------------------------------------------------------

def _flat(entry: dict, fact: dict) -> dict:
    """One imported fact as a flat row for the Memory screen (text, section, project, source, dates)."""
    section = memory_store.fact_line(entry, fact).rsplit(": ", 1)[0] if fact else entry["name"]
    return {"id": fact["id"], "entry_id": entry["id"], "key": fact.get("key", ""), "text": fact["text"],
            "section": section, "project": entry.get("project") or (entry["name"] if entry["kind"] == "project" else ""),
            "source": fact.get("source", ""), "origin": fact.get("origin", ""), "imported_at": fact.get("date", ""),
            "updated_at": fact.get("date", ""), "edited": bool(fact.get("edited"))}


def load_memory() -> list[dict]:
    """Imported claude.ai facts, flattened (Settings → Import shows these)."""
    return [_flat(e, f) for e in memory_store.load_store()["entries"] for f in e["facts"]
            if f.get("source") == claude_import.SOURCE]


def _locate(fact_id: str, store: dict) -> tuple[dict | None, dict | None]:
    """Finds a fact by id anywhere in the memory store: (entry, fact), or (None, None)."""
    for entry in store["entries"]:
        for fact in entry["facts"]:
            if fact["id"] == fact_id:
                return entry, fact
    return None, None


def update_memory_entry(entry_id: str, text: str) -> dict | None:
    """Changes the text of an imported fact and marks it as edited (re-imports then keep your wording)."""
    with memory_store.LOCK:
        store = memory_store.load_store()
        entry, fact = _locate(entry_id, store)
        if fact is None:
            return None
        fact["text"], fact["edited"], fact["date"] = text.strip(), True, _now()
        memory_store.save_store(store)
        return _flat(entry, fact)


def delete_memory_entries(ids: list[str]) -> int:
    """Deletes facts by id from the memory store; returns how many were removed."""
    with memory_store.LOCK:
        store = memory_store.load_store()
        removed = 0
        for entry in store["entries"]:
            before = len(entry["facts"])
            entry["facts"] = [f for f in entry["facts"] if f["id"] not in ids]
            removed += before - len(entry["facts"])
        memory_store.save_store(store)
        return removed


def merge_memory_entries(ids: list[str], text: str = "") -> dict | None:
    """Keep the first fact (with `text` if given, else the longest wording); delete the rest."""
    with memory_store.LOCK:
        store = memory_store.load_store()
        located = [(e, f) for i in ids for e, f in [_locate(i, store)] if f is not None]
        if len(located) < 2:
            return None
        keep_entry, keep = located[0]
        keep["text"] = text.strip() or max((f["text"] for _, f in located), key=len)
        keep["edited"], keep["date"] = True, _now()
        keep["merged_keys"] = sorted({f.get("key", "") for _, f in located} | set(keep.get("merged_keys", [])))
        drop = {f["id"] for _, f in located[1:]}
        for entry in store["entries"]:
            entry["facts"] = [f for f in entry["facts"] if f["id"] not in drop]
        memory_store.save_store(store)
        return _flat(keep_entry, keep)


def memory_duplicates(entries: list[dict] | None = None) -> list[list[str]]:
    """Groups of facts that say nearly the same thing (60%+ word overlap), so the user can merge them."""
    entries = load_memory() if entries is None else entries
    groups, used = [], set()
    for i, a in enumerate(entries):
        if a["id"] in used:
            continue
        group = [a["id"]] + [b["id"] for b in entries[i + 1:] if b["id"] not in used
                             and similarity(a["text"], b["text"]) >= 0.6]
        if len(group) > 1:
            used.update(group)
            groups.append(group)
    return groups


def memory_facts(limit_chars: int = 4000) -> list[str]:
    """Imported facts for prompts (written with the entry they belong to)."""
    out, used = [], 0
    for entry in memory_store.load_store()["entries"]:
        for fact in entry["facts"]:
            if fact.get("source") != claude_import.SOURCE:
                continue
            line = memory_store.fact_line(entry, fact)
            if used + len(line) > limit_chars:
                return out
            out.append(line)
            used += len(line)
    return out


def _preview_status(store: dict, item: dict) -> tuple[str, str]:
    """("new" | "update" | "move" | "unchanged", text it replaces) for an imported fact. "move": an
    older import filed it elsewhere (it didn't keep folder paths); the commit moves it to its entry."""
    kind, name, parent, heading = memory_store.place(item.get("section", ""), item.get("origin", ""),
                                                     item.get("project", ""))
    if kind == "person":
        entry = people_store.find_by_name([e for e in store["entries"] if e["kind"] == "person"], name)
    else:
        entry = memory_store.find(store, kind, name, parent)
    holder, _fact = memory_store.find_imported_fact(store, item.get("key") or memory_store.key_for(item["text"]))
    if holder is not None:
        return ("unchanged" if holder is entry else "move"), ""
    if entry is None:
        return "new", ""
    match, status = memory_store.match_fact(entry, item["text"], heading, item.get("key", ""))
    if status == "update" and match is not None and match.get("edited"):
        return "unchanged", ""  # the user edited it; their wording wins
    return status, match["text"] if status == "update" and match else ""


# ---------------------------------------------------------------------------
# Conversations (read-only sessions)
# ---------------------------------------------------------------------------

def list_conversations() -> list[dict]:
    """All imported claude.ai chats."""
    directory = conversations_dir()
    if not directory.exists():
        return []
    return [r for r in (sessions.load(directory, p.stem) for p in directory.glob("*.json")) if r]


def conversation_record(conv: dict, imported_at: str, project: str = "") -> dict:
    """Turns a parsed claude.ai conversation into a read-only chat session record."""
    return {"id": session_id_for(conv["uuid"]), "title": conv["title"] or "Untitled conversation",
            "custom_title": True, "created_at": conv["created_at"], "updated_at": conv["updated_at"],
            "messages": [{"role": m["role"], "content": m["content"], "created_at": m.get("created_at", "")}
                         for m in conv["messages"]],
            **{k: (list(v) if isinstance(v, list) else v) for k, v in sessions.EXTRA_DEFAULTS.items()
               if k != "custom_title"},
            "read_only": True,
            "imported": {"source": claude_import.SOURCE, "uuid": conv["uuid"], "imported_at": imported_at,
                         "project": project}}


# ---------------------------------------------------------------------------
# Preview → commit
# ---------------------------------------------------------------------------

def _expire_previews() -> None:
    """Forgets import previews older than PREVIEW_TTL (a preview is kept only until the user commits it)."""
    now = time.time()
    for pid in [p for p, v in _PREVIEWS.items() if now - v["created"] > PREVIEW_TTL]:
        _PREVIEWS.pop(pid, None)


def preview(files: list[tuple[str, bytes]], pasted_text: str = "") -> dict:
    """Parse, compare with what's already imported, and stage for commit."""
    parsed = claude_import.parse_upload(files, pasted_text)
    store = memory_store.load_store()
    existing_convs = {r["imported"]["uuid"]: r for r in list_conversations() if r.get("imported")}
    people = people_store.load()
    projects = {p["uuid"]: p["name"] for p in parsed.projects if p.get("uuid")}

    memory = []
    for entry in parsed.memory:
        status, replaces = _preview_status(store, entry)
        kind, name, parent, heading = memory_store.place(entry.get("section", ""), entry.get("origin", ""),
                                                         entry.get("project", ""))
        memory.append({**entry, "status": status, "replaces": replaces,
                       "place": {"kind": kind, "name": name, "project": parent, "heading": heading}})
    conversations = []
    for conv in parsed.conversations:
        old = existing_convs.get(conv["uuid"])
        if old is None:
            status = "new"
        elif conv["updated_at"] > (old.get("updated_at") or "") or len(conv["messages"]) != len(old["messages"]):
            status = "update"
        else:
            status = "unchanged"
        conversations.append({"uuid": conv["uuid"], "title": conv["title"], "created_at": conv["created_at"],
                              "updated_at": conv["updated_at"], "message_count": conv["message_count"],
                              "project": projects.get(conv.get("project_uuid") or "", ""), "status": status})
    people = [{**p, "status": "new" if (c := people_store.imported_changes(people_store.match_imported(people, p), p)) == ["new"]
               else "update" if c else "unchanged", "changes": [x for x in c if x != "new"]}
              for p in parsed.people]
    dates = sorted(c["created_at"] for c in conversations if c["created_at"])
    preview_id = uuid.uuid4().hex
    with _LOCK:
        _expire_previews()
        _PREVIEWS[preview_id] = {"created": time.time(), "parsed": parsed, "projects": projects}
    return {"preview_id": preview_id, "files": parsed.files, "warnings": parsed.warnings,
            "account_name": parsed.account_name, "projects": parsed.projects,
            "memory": memory, "people": people, "conversations": conversations,
            "date_range": [dates[0], dates[-1]] if dates else None,
            "counts": {"memory": len(memory), "people": len(people), "conversations": len(conversations)}}


def commit(preview_id: str, memory_keys: list[str] | None, people_keys: list[str] | None,
           conversation_uuids: list[str] | None) -> dict:
    """Import the selected items. `None` means "all"; an empty list means "none"."""
    with _LOCK:
        staged = _PREVIEWS.pop(preview_id, None)
    if staged is None:
        raise KeyError("This preview has expired. Upload the files again.")
    parsed: claude_import.ParsedExport = staged["parsed"]
    imported_at = _now()
    result = {"memory_added": 0, "memory_updated": 0, "memory_moved": 0, "memory_tidied": 0, "entries_updated": 0,
              "people_added": 0, "people_updated": 0, "conversations_added": 0, "conversations_updated": 0,
              "imported_at": imported_at}

    chosen = [e for e in parsed.memory if memory_keys is None or e["key"] in memory_keys]
    picked_people = [p for p in parsed.people if people_keys is None or p["key"] in people_keys]
    counts = people_store.merge_imported(picked_people, imported_at)  # people first: their facts attach to them
    result["people_added"], result["people_updated"] = counts["added"], counts["updated"]
    with memory_store.LOCK:
        store = memory_store.load_store()
        result["memory_tidied"] = memory_store.tidy_header_facts(store)
        for entry in chosen:
            fact, status = memory_store.add_imported(store, entry, imported_at)
            if status in ("added", "updated", "moved"):
                result[f"memory_{status}"] += 1
            if fact is not None and status in ("unchanged", "moved"):
                fact["last_seen_import"] = imported_at
        # Every project folder in a memory export is a project, even one without index.md.
        for project in parsed.projects:
            if not project.get("uuid") and project.get("name"):
                if memory_store.find(store, "project", project["name"]) is None:
                    memory_store.ensure(store, "project", project["name"])["source"] = "claude.ai"
                    result["entries_updated"] += 1
        for section in parsed.sections:
            if chosen or memory_keys is None:
                result["entries_updated"] += memory_store.apply_imported_meta(store, section)
        if parsed.account_name:
            profile = memory_store.ensure(store, "profile", "Profile")
            profile.setdefault("description", "")
            if not profile["description"]:
                profile["description"] = f"{parsed.account_name} (from the claude.ai export)"
        memory_store.save_store(store)

    directory = conversations_dir()
    for conv in parsed.conversations:
        if conversation_uuids is not None and conv["uuid"] not in conversation_uuids:
            continue
        sid = session_id_for(conv["uuid"])
        old = sessions.load(directory, sid)
        if old is not None and conv["updated_at"] <= (old.get("updated_at") or "") \
                and len(conv["messages"]) == len(old["messages"]):
            continue
        record = conversation_record(conv, imported_at, staged["projects"].get(conv.get("project_uuid") or "", ""))
        if old is not None and old.get("title") != record["title"] and old.get("renamed"):
            record["title"], record["renamed"] = old["title"], True
        sessions.save(directory, record, touch=False)
        result["conversations_added" if old is None else "conversations_updated"] += 1

    history = _read(IMPORTS_DIR / "history.json", [])
    history.append({"at": imported_at, **{k: v for k, v in result.items() if k != "imported_at"}})
    _write(IMPORTS_DIR / "history.json", history[-50:])
    return result


def status() -> dict:
    """Counts for Settings → Import: imported facts, chats, people, and the last import."""
    history = _read(IMPORTS_DIR / "history.json", [])
    return {"memory": len(load_memory()), "conversations": len(list(conversations_dir().glob("*.json")))
            if conversations_dir().exists() else 0,
            "people": sum(1 for p in people_store.load() if p.get("source")),
            "last_import": history[-1] if history else None}


def delete_all() -> dict:
    """Remove everything imported from claude.ai (people the user also labelled in meetings stay,
    without the imported fields)."""
    counts = status()
    with memory_store.LOCK:
        store = memory_store.load_store()
        for entry in store["entries"]:
            entry["facts"] = [f for f in entry["facts"] if f.get("source") != claude_import.SOURCE]
        # entries that only existed because of the import go too (people: see remove_imported)
        store["entries"] = [e for e in store["entries"] if e["kind"] == "person" or e["facts"]
                            or e.get("user_created") or e.get("quorum_project") or e.get("source") != claude_import.SOURCE]
        memory_store.save_store(store)
    with _LOCK:
        directory = conversations_dir()
        if directory.exists():
            for path in directory.glob("*.json"):
                path.unlink()
        (IMPORTS_DIR / "history.json").unlink(missing_ok=True)
    people_store.remove_imported()
    return {"deleted": {"memory": counts["memory"], "conversations": counts["conversations"],
                        "people": counts["people"]}}
