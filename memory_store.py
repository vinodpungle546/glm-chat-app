#!/usr/bin/env python3
"""Memory: one local store in the shape of a claude.ai memory export.

Entries (`kind`):
  profile   about the user (one entry)                          → profile.md
  person    one person                                          → people/<name>.md
  project   a project (its own facts; areas may belong to it)   → projects/<Project>/index.md
  area      a topic, top-level or inside a project (`project`)  → [projects/<Project>/]areas/<topic>.md

Every entry has a name, description, aliases and facts. Facts are bullets grouped under
headings, each with where it came from and when:
  {"id", "text", "heading", "source", "date", "key", "edited", "origin"}
  source: "claude.ai" (imported) | "chat" (learned from a chat) | "meeting" (learned from a
          meeting) | "user" (entered by the user) | "cloud_review" (suggested by a cloud speaker
          review and kept by the user)

People also keep what meetings teach: role, team, projects, relationships, meetings (transcript
ids), voiceprints (voice fingerprints, never exported), screen_names (names seen on their video
tile), plus the import fields people.py has always had. people.py reads and writes the person
entries of this store.

The first load migrates the older separate stores (people.json, imports/memory.json, each
project's chat memory) into this file, after copying them to memory/backup-<time>/. The old
files are left as they were.

File: MEMORY_DIR/memory.json  {"version": 1, "entries": [...], "migrated_at", "backup"}
"""

from __future__ import annotations

import io
import json
import os
import re
import shutil
import threading
import uuid
import zipfile
from datetime import datetime
from pathlib import Path

KINDS = ("profile", "person", "project", "area")
SOURCES = ("claude.ai", "chat", "meeting", "user", "cloud_review")
SOURCE_LABELS = {"claude.ai": "Imported from Claude", "chat": "Learned from chat", "meeting": "Learned from a meeting",
                 "user": "Entered by you", "cloud_review": "Suggested by cloud review, kept by you"}
VERSION = 1
LOCK = threading.RLock()
PERSON_FIELDS = {"role": "", "team": "", "projects": [], "relationships": [], "meetings": [], "voiceprints": [],
                 "screen_names": [], "notes": ""}
_STOP = {"the", "and", "for", "with", "that", "this", "from", "have", "has", "are", "was", "were", "will", "what",
         "when", "who", "how", "why", "about", "your", "you", "they", "their", "them", "into", "can", "could",
         "should", "would", "does", "did", "our", "out", "all", "any", "some", "more", "most", "than", "then",
         "there", "here", "which", "also", "just", "like", "been", "being", "only", "very", "much", "many",
         "please", "tell", "know", "need", "want", "make", "give", "show", "list"}


MEMORY_DIR = Path(os.environ.get("MEMORY_DIR", str(Path(os.environ.get("APP_DATA_DIR", "./data")) / "memory")))


def store_file() -> Path:
    """The memory file: APP_DATA_DIR/memory/memory.json."""
    return MEMORY_DIR / "memory.json"


def _now() -> str:
    """The current local time as an ISO string."""
    return datetime.now().isoformat(timespec="seconds")


def _id() -> str:
    """A new random 12-character id for an entry or fact."""
    return uuid.uuid4().hex[:12]


def norm(text: str) -> str:
    """Collapses whitespace to single spaces and trims the ends."""
    return re.sub(r"\s+", " ", str(text or "")).strip()


def key_for(text: str) -> str:
    """The stable key of a fact's text (same function as the claude.ai importer, so imported facts can be matched
    again).
    """
    import claude_import
    return claude_import.key_for(text)


def words(text: str) -> set[str]:
    """The meaningful lowercase words of a text (3+ letters, no stop words), for comparing facts."""
    return {w for w in re.findall(r"[a-z0-9]+", str(text or "").lower()) if len(w) > 2 and w not in _STOP}


def similarity(a: str, b: str) -> float:
    """Word overlap of two texts, 0 to 1 (Jaccard); 0.5+ counts as 'the same fact, reworded'."""
    wa, wb = words(a), words(b)
    return len(wa & wb) / len(wa | wb) if wa and wb else 0.0


def slug(name: str) -> str:
    """A file-name friendly version of a name: 'Owen Hart' -> 'owen-hart'."""
    return re.sub(r"[^a-z0-9]+", "-", norm(name).lower()).strip("-") or "untitled"


# ---------------------------------------------------------------------------
# Load / save
# ---------------------------------------------------------------------------

def _blank() -> dict:
    """An empty memory store (before the first migration)."""
    return {"version": VERSION, "entries": [], "migrated_at": "", "backup": ""}


def new_entry(kind: str, name: str, project: str = "") -> dict:
    """A new entry of one kind: 'profile' (you), 'person', 'project' or 'area' (a topic, optionally inside a
    project). People get the extra person fields (role, team, voices…).
    """
    if kind not in KINDS:
        raise ValueError(f"Unknown memory kind: {kind}")
    entry = {"id": _id(), "kind": kind, "name": norm(name) or ("Profile" if kind == "profile" else "Untitled"),
             "description": "", "aliases": [], "facts": [], "project": norm(project) if kind == "area" else "",
             "created_at": _now(), "updated_at": _now()}
    if kind == "person":
        entry.update({k: (list(v) if isinstance(v, list) else v) for k, v in PERSON_FIELDS.items()})
    return entry


def _normalize(entry: dict) -> dict:
    """Fills in any field an older entry or fact is missing, so the rest of the code can rely on them."""
    entry.setdefault("id", _id())
    entry.setdefault("kind", "person")
    entry.setdefault("description", "")
    entry.setdefault("aliases", [])
    entry.setdefault("facts", [])
    entry.setdefault("project", "")
    if entry["kind"] == "person":
        for key, value in PERSON_FIELDS.items():
            entry.setdefault(key, list(value) if isinstance(value, list) else value)
    for fact in entry["facts"]:
        fact.setdefault("id", _id())
        fact.setdefault("heading", "")
        fact.setdefault("source", "user")
        fact.setdefault("date", "")
        fact.setdefault("key", key_for(fact.get("text", "")))
        fact.setdefault("edited", False)
    return entry


def load_store() -> dict:
    """The whole store; migrates the older stores the first time."""
    with LOCK:
        path = store_file()
        if not path.exists():
            store = migrate()
            save_store(store)
            return store
        try:
            store = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            store = _blank()
        store.setdefault("entries", [])
        store["entries"] = [_normalize(e) for e in store["entries"] if isinstance(e, dict) and e.get("name")]
        return store


def save_store(store: dict) -> None:
    """Writes the memory file safely (temporary file, then rename) under the lock."""
    with LOCK:
        path = store_file()
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(store, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(path)


def entries(kind: str | None = None) -> list[dict]:
    """All entries, or only those of one kind."""
    return [e for e in load_store()["entries"] if kind is None or e["kind"] == kind]


def get(entry_id: str, store: dict | None = None) -> dict | None:
    """The entry with this id, or None."""
    store = store or load_store()
    return next((e for e in store["entries"] if e["id"] == entry_id), None)


def find(store: dict, kind: str, name: str, project: str = "") -> dict | None:
    """The entry of `kind` whose name or alias matches `name` (topics also match on their project). The profile
    always matches. None if there is none.
    """
    key = norm(name).lower()
    for entry in store["entries"]:
        if entry["kind"] != kind or (kind == "area" and entry.get("project", "").lower() != norm(project).lower()):
            continue
        if kind == "profile" or entry["name"].lower() == key or key in [a.lower() for a in entry.get("aliases", [])]:
            return entry
    return None


def ensure(store: dict, kind: str, name: str, project: str = "") -> dict:
    """Finds the entry, or creates it when it doesn't exist yet."""
    entry = find(store, kind, name, project)
    if entry is None:
        entry = new_entry(kind, name, project)
        store["entries"].append(entry)
    return entry


# ---------------------------------------------------------------------------
# Facts
# ---------------------------------------------------------------------------

def match_fact(entry: dict, text: str, heading: str = "", key: str = "") -> tuple[dict | None, str]:
    """(existing fact, "unchanged" | "update" | "new"): same key/wording, or similar wording under
    the same heading (an updated version of the same fact)."""
    key = key or key_for(text)
    if norm(entry.get("description", "")).lower() == norm(text).lower():
        return None, "unchanged"  # claude.ai exports repeat the description as a bullet
    for fact in entry["facts"]:
        if fact.get("key") == key or key in fact.get("merged_keys", []) or norm(fact["text"]).lower() == norm(text).lower():
            return fact, "unchanged"
    best, score = None, 0.0
    for fact in entry["facts"]:
        if (fact.get("heading") or "") != (heading or ""):
            continue
        s = similarity(fact["text"], text)
        if s > score:
            best, score = fact, s
    return (best, "update") if best is not None and score >= 0.5 else (None, "new")


def add_fact(entry: dict, text: str, heading: str = "", source: str = "user", date: str = "",
             key: str = "", origin: str = "", replace_similar: bool = True) -> tuple[dict | None, str]:
    """Add a fact unless it's already there. A similar fact is updated (unless the user edited it)."""
    text = norm(text)
    if not text:
        return None, "empty"
    match, status = match_fact(entry, text, heading, key)
    if status == "unchanged":
        return match, status
    if status == "update" and replace_similar and match is not None:
        if match.get("edited"):
            return match, "kept"  # the user's wording wins
        match.update(text=text, key=key or key_for(text), date=date or _now(), source=source,
                     origin=origin or match.get("origin", ""))
        entry["updated_at"] = _now()
        return match, "updated"
    fact = {"id": _id(), "text": text, "heading": norm(heading), "source": source if source in SOURCES else "user",
            "date": date or _now(), "key": key or key_for(text), "edited": False, "origin": origin}
    entry["facts"].append(fact)
    entry["updated_at"] = _now()
    return fact, "added"


# ---------------------------------------------------------------------------
# Placing imported claude.ai memory
# ---------------------------------------------------------------------------

def place(section: str, origin: str = "", project: str = "") -> tuple[str, str, str, str]:
    """(kind, entry name, parent project, heading) for an imported fact's section string, e.g.
    "People › Cian Byrne › Notes", "Profile › Work", "Atlas › Roadmap › Q3" (area of project Atlas)."""
    import claude_import

    parts = [p.strip() for p in str(section or "").split("›") if p.strip()]
    kind = claude_import.path_context(origin)[2] if origin else "memory"
    if parts and parts[0] == "People":
        return "person", parts[1] if len(parts) > 1 else "Unknown", "", " › ".join(parts[2:])
    if project and parts and parts[0] == project:
        parts = parts[1:]
        if kind in ("project", "profile") or not parts:
            heading = " › ".join(parts[1:] if parts and parts[0] in (project, "Profile") else parts)
            return "project", project, "", ("Profile › " + heading if kind == "profile" and heading else
                                             "Profile" if kind == "profile" else heading)
        return "area", parts[0], project, " › ".join(parts[1:])
    if parts and parts[0] == "Profile" or kind == "profile":
        return "profile", "Profile", "", " › ".join(parts[1:])
    if kind == "project" and project:
        return "project", project, "", " › ".join(parts[1:])
    return "area", parts[0] if parts else "General", project, " › ".join(parts[1:])


def find_imported_fact(store: dict, key: str) -> tuple[dict | None, dict | None]:
    """(entry, fact) holding an imported fact with this key anywhere in the store."""
    for entry in store["entries"]:
        for fact in entry["facts"]:
            if fact.get("source") == "claude.ai" and (fact.get("key") == key or key in fact.get("merged_keys", [])):
                return entry, fact
    return None, None


def add_imported(store: dict, item: dict, imported_at: str) -> tuple[dict | None, str]:
    """One imported memory fact ({key, text, section, origin, project}) into its entry. A fact an
    earlier import put elsewhere (older imports didn't keep folder paths) moves to its entry."""
    kind, name, parent, heading = place(item.get("section", ""), item.get("origin", ""), item.get("project", ""))
    if kind == "person":
        import people as people_store
        entry = people_store.find_by_name([e for e in store["entries"] if e["kind"] == "person"], name)
        if entry is None:
            entry = new_entry("person", name)
            entry.update(source="claude.ai", imported_at=imported_at)
            store["entries"].append(entry)
    else:
        entry = find(store, kind, name, parent)
        if entry is None:
            entry = new_entry(kind, name, parent)
            entry["source"] = "claude.ai"  # created by the import (removed again by "delete all imported")
            store["entries"].append(entry)
    key = item.get("key") or key_for(item["text"])
    holder, existing = find_imported_fact(store, key)
    if existing is not None and holder is not entry:
        holder["facts"] = [f for f in holder["facts"] if f is not existing]
        existing.update(heading=norm(heading) or existing.get("heading", ""), origin=item.get("origin", existing.get("origin", "")))
        entry["facts"].append(existing)
        if not holder["facts"] and _import_created(holder):
            store["entries"] = [e for e in store["entries"] if e is not holder]
        return existing, "moved"
    return add_fact(entry, item["text"], heading, "claude.ai", imported_at, key, item.get("origin", ""))


def _import_created(entry: dict) -> bool:
    """True for a project/topic entry that only exists because of a claude.ai import (not made by the user or
    linked to a Quorum project), so it may be removed when it becomes empty.
    """
    return (entry["kind"] in ("area", "project") and entry.get("source") == "claude.ai"
            and not entry.get("user_created") and not entry.get("quorum_project"))


def apply_imported_meta(store: dict, item: dict) -> bool:
    """A memory file's front matter ({section, origin, project, kind, description, aliases}) on its
    entry: the description when the entry has none (never over the user's), and any new aliases."""
    if item.get("kind") == "profile" and item.get("project"):
        return False  # a project's profile.md describes the user, not the project
    kind, name, parent, _heading = place(item.get("section", ""), item.get("origin", ""), item.get("project", ""))
    if kind == "person":
        return False  # people files are merged by people.merge_imported
    entry = find(store, kind, name, parent)
    if entry is None:
        entry = new_entry(kind, name, parent)
        entry["source"] = "claude.ai"
        store["entries"].append(entry)
    changed = False
    description = norm(item.get("description", ""))
    current = norm(entry.get("description", ""))
    placeholder = not current or current.endswith("(from the claude.ai export)")
    mine = entry.get("user_created")  # an entry the user made: its details are theirs
    if (description and description.lower() != entry["name"].lower() and placeholder and not mine
            and "description" not in entry.get("user_edited", [])):
        entry["description"] = description
        changed = True
    if not mine and "aliases" not in entry.get("user_edited", []):
        known = {entry["name"].lower(), *(a.lower() for a in entry.get("aliases", []))}
        for alias in item.get("aliases") or []:
            alias = norm(alias)
            if alias and alias.lower() not in known:
                entry.setdefault("aliases", []).append(alias)
                known.add(alias.lower())
                changed = True
    if changed:
        entry["updated_at"] = _now()
    return changed


_HEADER_FACT = re.compile(r'^(?:name: (?:"[^"]*"|[\w.-]+)|description: .+|aliases: \[.*\])$')


def tidy_header_facts(store: dict) -> int:
    """Remove facts an older import read from a file's front matter ("name: m4-contacts",
    "description: …", "aliases: […]"); the import now uses those as the entry's own details."""
    removed = 0
    for entry in list(store["entries"]):
        keep = [f for f in entry["facts"] if not (f.get("source") == "claude.ai" and not f.get("edited")
                                                   and _HEADER_FACT.match(f["text"]))]
        if len(keep) != len(entry["facts"]):
            removed += len(entry["facts"]) - len(keep)
            entry["facts"] = keep
            if not keep and _import_created(entry):
                store["entries"].remove(entry)
    return removed


# ---------------------------------------------------------------------------
# Migration from the older stores
# ---------------------------------------------------------------------------

def _legacy_paths() -> dict[str, Path | list[Path]]:
    """Where the older separate stores lived (people.json, imported memory, project files); read once by the
    migration.
    """
    import imports
    import people as people_store
    import projects

    project_dir = Path(projects.PROJECTS_DIR)
    return {"people": Path(people_store.PEOPLE_FILE), "imported": imports.memory_file(),
            "projects": sorted(project_dir.glob("*.json")) if project_dir.is_dir() else []}


def migrate() -> dict:
    """Fold people.json, imported claude.ai memory and project chat memory into one store.
    The old files are copied to memory/backup-<time>/ first and are never changed."""
    import people as people_store

    store = _blank()
    paths = _legacy_paths()
    found = [p for p in [paths["people"], paths["imported"], *paths["projects"]] if isinstance(p, Path) and p.is_file()]
    if found:
        backup = MEMORY_DIR / f"backup-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
        for path in found:
            folder = "projects" if path in paths["projects"] else "imports" if path == paths["imported"] else ""
            dest = backup / folder / path.name
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, dest)
        store["backup"] = str(backup)
    for person in people_store.load(paths["people"]) if paths["people"].is_file() else []:
        entry = _normalize({**person, "kind": "person"})
        entry.setdefault("created_at", person.get("updated_at") or _now())
        for sentence in person.get("evidence") or []:
            add_fact(entry, sentence, "", "claude.ai", person.get("imported_at", ""), origin="people")
        store["entries"].append(entry)
    imported = []
    if paths["imported"].is_file():
        try:
            imported = json.loads(paths["imported"].read_text(encoding="utf-8"))
        except (OSError, ValueError):
            imported = []
    for item in imported if isinstance(imported, list) else []:
        if not isinstance(item, dict) or not item.get("text"):
            continue
        fact, _ = add_imported(store, item, item.get("imported_at", ""))
        if fact is not None:
            if item.get("edited"):
                fact.update(text=item["text"], edited=True)
            if item.get("merged_keys"):
                fact["merged_keys"] = item["merged_keys"]
            fact["date"] = item.get("updated_at") or item.get("imported_at") or fact["date"]
    for path in paths["projects"]:
        try:
            project = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        facts = [m for m in project.get("memory", []) if isinstance(m, str) and m.strip()]
        if not facts:
            continue
        entry = ensure(store, "project", project.get("name") or "Project")
        entry["quorum_project"] = project.get("id", "")
        for text in facts:
            add_fact(entry, text, "", "chat", project.get("updated_at", ""))
    store["migrated_at"] = _now()
    return store


# ---------------------------------------------------------------------------
# Editing (Settings → Memory)
# ---------------------------------------------------------------------------

EDITABLE = {"name", "description", "aliases", "role", "team", "projects", "relationships", "notes", "project"}


def create(kind: str, name: str, description: str = "", project: str = "") -> dict:
    """Creates an entry the user asked for (e.g. a new topic). Raises ValueError if one with that name already
    exists.
    """
    with LOCK:
        store = load_store()
        if find(store, kind, name, project) and kind != "profile":
            raise ValueError(f"There's already a {kind} called {name}.")
        entry = ensure(store, kind, name, project)
        entry["description"] = norm(description)
        entry["user_created"] = True
        save_store(store)
        return entry


def update(entry_id: str, fields: dict) -> dict | None:
    """Changes editable fields of an entry (name, description, aliases, role…), noting which fields the user
    edited so imports never overwrite them.
    """
    with LOCK:
        store = load_store()
        entry = get(entry_id, store)
        if entry is None:
            return None
        for key, value in fields.items():
            if key not in EDITABLE:
                continue
            if isinstance(value, list):
                value = [norm(v) for v in value if norm(v)]
            elif isinstance(value, str):
                value = norm(value)
            else:
                continue
            if key == "name" and not value:
                raise ValueError("The name can't be empty.")
            if entry.get(key) != value:
                entry[key] = value
                if entry.get("source") and key not in entry.setdefault("user_edited", []):
                    entry["user_edited"].append(key)
        entry["updated_at"] = _now()
        save_store(store)
        return entry


def delete(entry_id: str) -> bool:
    """Deletes an entry and all its facts. True if it existed."""
    with LOCK:
        store = load_store()
        before = len(store["entries"])
        store["entries"] = [e for e in store["entries"] if e["id"] != entry_id]
        save_store(store)
        return len(store["entries"]) < before


def add_user_fact(entry_id: str, text: str, heading: str = "", source: str = "user") -> dict | None:
    """Adds a fact the user typed to an entry (never merged with a similar fact)."""
    with LOCK:
        store = load_store()
        entry = get(entry_id, store)
        if entry is None:
            return None
        fact, _ = add_fact(entry, text, heading, source, replace_similar=False)
        save_store(store)
        return fact


def update_fact(entry_id: str, fact_id: str, text: str | None = None, heading: str | None = None) -> dict | None:
    """Changes a fact's text or heading and marks it as edited by the user."""
    with LOCK:
        store = load_store()
        entry = get(entry_id, store)
        fact = next((f for f in (entry or {}).get("facts", []) if f["id"] == fact_id), None)
        if fact is None:
            return None
        if text is not None and norm(text):
            fact["text"], fact["edited"] = norm(text), True
        if heading is not None:
            fact["heading"] = norm(heading)
        fact["date"] = _now()
        entry["updated_at"] = _now()
        save_store(store)
        return fact


def delete_facts(entry_id: str, fact_ids: list[str]) -> int:
    """Deletes facts of an entry by id; returns how many were deleted."""
    with LOCK:
        store = load_store()
        entry = get(entry_id, store)
        if entry is None:
            return 0
        before = len(entry["facts"])
        entry["facts"] = [f for f in entry["facts"] if f["id"] not in fact_ids]
        save_store(store)
        return before - len(entry["facts"])


def merge_facts(entry_id: str, fact_ids: list[str], text: str = "") -> dict | None:
    """Keep the first fact (with `text`, or the longest wording) and drop the others."""
    with LOCK:
        store = load_store()
        entry = get(entry_id, store)
        chosen = [f for f in (entry or {}).get("facts", []) if f["id"] in fact_ids]
        if len(chosen) < 2:
            return None
        keep = chosen[0]
        keep["text"] = norm(text) or max((f["text"] for f in chosen), key=len)
        keep["edited"], keep["date"] = True, _now()
        keep["merged_keys"] = sorted({f.get("key", "") for f in chosen} | set(keep.get("merged_keys", [])))
        entry["facts"] = [f for f in entry["facts"] if f is keep or f["id"] not in fact_ids]
        save_store(store)
        return keep


def merge(keep_id: str, ids: list[str]) -> dict | None:
    """Merge entries of the same kind: names become aliases, facts and person data combine."""
    with LOCK:
        store = load_store()
        keep = get(keep_id, store)
        if keep is None:
            return None
        others = [e for e in store["entries"] if e["id"] in ids and e["id"] != keep_id and e["kind"] == keep["kind"]]
        for other in others:
            keep["aliases"] = [a for a in dict.fromkeys(keep["aliases"] + [other["name"]] + other.get("aliases", []))
                               if a.lower() != keep["name"].lower()]
            for fact in other["facts"]:
                add_fact(keep, fact["text"], fact.get("heading", ""), fact.get("source", "user"), fact.get("date", ""),
                         fact.get("key", ""), fact.get("origin", ""))
            if not keep.get("description") and other.get("description"):
                keep["description"] = other["description"]
            if keep["kind"] == "person":
                for key in ("meetings", "projects", "relationships", "evidence"):
                    keep[key] = list(dict.fromkeys((keep.get(key) or []) + (other.get(key) or [])))
                keep["voiceprints"] = ((keep.get("voiceprints") or []) + (other.get("voiceprints") or []))[-8:]
                keep["screen_names"] = merge_screen_names(keep.get("screen_names") or [], other.get("screen_names") or [])
                for key in ("role", "team", "notes", "source", "imported_at", "import_key"):
                    if not keep.get(key) and other.get(key):
                        keep[key] = other[key]
                keep["is_user"] = bool(keep.get("is_user") or other.get("is_user"))
        keep["updated_at"] = _now()
        store["entries"] = [e for e in store["entries"] if e not in others]
        save_store(store)
        return keep


def duplicates(store: dict | None = None) -> dict:
    """{"entries": [[ids]] same-kind entries that are probably the same, "facts": {entry: [[fact ids]]}}."""
    store = store or load_store()
    groups: dict[tuple, list[dict]] = {}
    for entry in store["entries"]:
        if entry["kind"] == "profile":
            continue
        first = entry["name"].split()[0].lower() if entry["kind"] == "person" else entry["name"].lower()
        groups.setdefault((entry["kind"], entry.get("project", "").lower(), first), []).append(entry)
    same = []
    for members in groups.values():
        if len(members) < 2:
            continue
        full = {e["name"].lower() for e in members if " " in e["name"]}
        if members[0]["kind"] != "person" or len(full) <= 1:
            same.append([e["id"] for e in members])
    facts: dict[str, list[list[str]]] = {}
    for entry in store["entries"]:
        used: set[str] = set()
        for i, a in enumerate(entry["facts"]):
            if a["id"] in used:
                continue
            group = [a["id"]] + [b["id"] for b in entry["facts"][i + 1:]
                                 if b["id"] not in used and similarity(a["text"], b["text"]) >= 0.6]
            if len(group) > 1:
                used.update(group)
                facts.setdefault(entry["id"], []).append(group)
    return {"entries": same, "facts": facts}


def merge_screen_names(a: list[dict], b: list[dict]) -> list[dict]:
    """Combines two lists of 'seen on screen' records, keeping the highest count per (name, meeting) and only the
    latest 30.
    """
    out: dict[str, dict] = {}
    for item in a + b:
        key = (item.get("name", ""), item.get("tid", ""))
        if key in out:
            out[key]["count"] = max(out[key]["count"], item.get("count", 0))
        else:
            out[key] = dict(item)
    return list(out.values())[-30:]


# ---------------------------------------------------------------------------
# Search and use
# ---------------------------------------------------------------------------

def entry_text(entry: dict) -> str:
    """An entry's searchable words: name, description, aliases and, for people, role, team and projects."""
    bits = [entry["name"], entry.get("description", ""), " ".join(entry.get("aliases", []))]
    if entry["kind"] == "person":
        bits += [entry.get("role", ""), entry.get("team", ""), " ".join(entry.get("projects") or [])]
    return " ".join(b for b in bits if b)


def search(query: str, kind: str | None = None) -> list[dict]:
    """Entries whose name, details or facts contain all the query words (case-insensitive)."""
    terms = [t for t in norm(query).lower().split() if t]
    out = []
    for entry in entries(kind):
        hay = (entry_text(entry) + " " + " ".join(f["text"] + " " + f.get("heading", "") for f in entry["facts"])).lower()
        if all(t in hay for t in terms):
            out.append(entry)
    return out


def _mentioned(entry: dict, text: str) -> bool:
    """True when the text mentions this entry by name or alias (for people also by a first name of 4+ letters),
    as a whole word.
    """
    names = [entry["name"]] + list(entry.get("aliases") or [])
    if entry["kind"] == "person":
        first = entry["name"].split()[0]
        if len(first) >= 4:
            names.append(first)
    return any(len(n) >= 3 and re.search(rf"(?<![\w-]){re.escape(n)}(?![\w-])", text, re.I) for n in names if n)


def fact_line(entry: dict, fact: dict | None = None) -> str:
    """How a fact is written into a prompt: "About me: …", "Priya Shah: …", "Atlas › Roadmap: …"."""
    where = ("About me" if entry["kind"] == "profile"
             else f"{entry['project']} › {entry['name']}" if entry["kind"] == "area" and entry.get("project")
             else entry["name"])
    if fact is None:
        if entry["kind"] == "person":
            details = ", ".join(x for x in (entry.get("role"), f"{entry['team']} team" if entry.get("team") else "",
                                            ("works on " + ", ".join(entry["projects"][:3])) if entry.get("projects")
                                            else "") if x)
            return f"{where}: {details}" if details else ""
        return f"{where}: {entry['description']}" if entry.get("description") else ""
    heading = f" ({fact['heading']})" if fact.get("heading") else ""
    return f"{where}{heading}: {fact['text']}"


def relevant(text: str, project_name: str = "", limit_chars: int = 3000, store: dict | None = None) -> list[dict]:
    """Facts worth giving the model for this message: a little about the user, everything about
    the people, projects and topics mentioned, and facts that share words with the message.
    Returns [{"text", "entry_id", "fact_id", "kind", "name", "source"}] (most relevant first)."""
    store = store or load_store()
    out: list[dict] = []
    seen: set[str] = set()
    used = 0
    message_words = words(text)

    def take(entry: dict, fact: dict | None) -> bool:
        """Adds one fact line to the result unless it's a duplicate; returns False once the character budget is
        used up (then no more facts are added).
        """
        nonlocal used
        line = fact_line(entry, fact)
        if not line or line in seen:
            return True
        if used + len(line) > limit_chars:
            return False
        seen.add(line)
        used += len(line)
        out.append({"text": line, "entry_id": entry["id"], "fact_id": fact["id"] if fact else "",
                    "kind": entry["kind"], "name": entry["name"], "source": (fact or {}).get("source", "")})
        return True

    # Facts are picked in three rounds, most useful first, until `limit_chars` is used up: (1) a few facts about
    # the user, (2) the people, projects and topics the message names, plus the current project, (3) any other
    # fact that shares at least two words with the message.
    mentioned = [e for e in store["entries"] if e["kind"] != "profile" and _mentioned(e, text)]
    project = next((e for e in store["entries"] if e["kind"] == "project" and project_name
                    and e["name"].lower() == project_name.lower()), None)
    for entry in store["entries"]:
        if entry["kind"] == "profile":
            take(entry, None)
            for fact in sorted(entry["facts"], key=lambda f: -len(words(f["text"]) & message_words))[:5]:
                take(entry, fact)
    for entry in mentioned + ([project] if project and project not in mentioned else []):
        take(entry, None)
        ranked = sorted(entry["facts"], key=lambda f: -len(words(f["text"]) & message_words))
        for fact in ranked[:8]:
            if not take(entry, fact):
                return out
    scored = []
    for entry in store["entries"]:
        for fact in entry["facts"]:
            overlap = len(words(fact["text"]) & message_words)
            if overlap >= 2:
                scored.append((overlap, entry, fact))
    for _, entry, fact in sorted(scored, key=lambda x: -x[0])[:10]:
        if not take(entry, fact):
            break
    return out


def facts_for_meeting(text: str, names: list[str], limit: int = 40) -> list[str]:
    """Memory lines for speaker identification / meeting notes: the people named in the meeting
    and the projects or topics it talks about."""
    store = load_store()
    lines = []
    lowered = {n.lower() for n in names}
    for entry in store["entries"]:
        if entry["kind"] == "profile":
            continue
        hit = entry["name"].lower() in lowered or entry["name"].split()[0].lower() in lowered \
            or _mentioned(entry, text[:200_000])
        if not hit:
            continue
        head = fact_line(entry)
        if head:
            lines.append(head)
        lines += [fact_line(entry, f) for f in entry["facts"][:4]]
        if len(lines) >= limit:
            break
    return lines[:limit]


def profile_entry(store: dict | None = None) -> dict | None:
    """The 'About me' profile entry, or None."""
    store = store or load_store()
    return next((e for e in store["entries"] if e["kind"] == "profile"), None)


def user_person(store: dict | None = None) -> dict | None:
    """The person entry that is the user themself (from the claude.ai export), or None."""
    store = store or load_store()
    return next((e for e in store["entries"] if e["kind"] == "person" and e.get("is_user")), None)


def project_entry(store: dict, project: dict) -> dict:
    """The memory entry for a Quorum project (chat-learned facts land here)."""
    for entry in store["entries"]:
        if entry["kind"] == "project" and entry.get("quorum_project") == project.get("id"):
            return entry
    entry = find(store, "project", project.get("name", "Project"))
    if entry is None:
        entry = new_entry("project", project.get("name", "Project"))
        store["entries"].append(entry)
    entry["quorum_project"] = project.get("id", "")
    return entry


def project_facts(project: dict) -> list[str]:
    """The facts of the memory entry linked to this Quorum project (used in its chats)."""
    store = load_store()
    entry = next((e for e in store["entries"] if e["kind"] == "project" and e.get("quorum_project") == project.get("id")),
                 None)
    return [f["text"] for f in entry["facts"]] if entry else []


def set_project_facts(project: dict, facts: list[str]) -> None:
    """Settings → Projects edits the project's memory list: chat/user facts mirror that list."""
    with LOCK:
        store = load_store()
        entry = project_entry(store, project)
        wanted = [norm(f) for f in facts if norm(f)]
        entry["facts"] = [f for f in entry["facts"] if f["text"] in wanted or f.get("source") not in ("chat", "user")]
        for text in wanted:
            add_fact(entry, text, "", "user", replace_similar=False)
        save_store(store)


def learn_chat_facts(project: dict, facts: list[str]) -> list[str]:
    """Facts the model extracted from a chat → the project's entry (source "chat")."""
    added = []
    with LOCK:
        store = load_store()
        entry = project_entry(store, project)
        for text in facts:
            fact, status = add_fact(entry, text, "", "chat", replace_similar=False)
            if status == "added":
                added.append(fact["text"])
        save_store(store)
    return added


def public(entry: dict) -> dict:
    """Without voice vectors (biometric data stays in the file); counts instead."""
    out = {k: v for k, v in entry.items() if k != "voiceprints"}
    if entry["kind"] == "person":
        prints = entry.get("voiceprints") or []
        out["voiceprint_count"] = len(prints)
        out["voiceprint_sources"] = dict(sorted(
            {s: sum(1 for p in prints if p.get("source", "user") == s) for s in {p.get("source", "user") for p in prints}}.items()))
    return out


# ---------------------------------------------------------------------------
# Export in the claude.ai memory format
# ---------------------------------------------------------------------------

def _yaml_value(value) -> str:
    """A value written for the export's YAML front matter (lists in [ ], quoted when needed)."""
    if isinstance(value, list):
        return "[" + ", ".join(str(v).replace(",", " ") for v in value) + "]"
    text = str(value).replace("\n", " ")
    return f'"{text}"' if ":" in text or text.startswith(("[", "-", "'", '"')) else text


def entry_markdown(entry: dict) -> str:
    """One entry as a claude.ai-style Markdown file: front matter (name, description, aliases, role…) then its
    facts grouped by heading. Voice fingerprints are never written.
    """
    meta = {"name": entry["name"], "description": entry.get("description", "")}
    if entry.get("aliases"):
        meta["aliases"] = entry["aliases"]
    if entry["kind"] == "person":
        for key in ("role", "team"):
            if entry.get(key):
                meta[key] = entry[key]
        if entry.get("projects"):
            meta["projects"] = entry["projects"]
    lines = ["---"] + [f"{k}: {_yaml_value(v)}" for k, v in meta.items() if v not in ("", [])] + ["---", ""]
    by_heading: dict[str, list[str]] = {}
    for fact in entry["facts"]:
        by_heading.setdefault(fact.get("heading", ""), []).append(fact["text"])
    for text in by_heading.pop("", []):
        lines.append(f"- {text}")
    for heading, texts in by_heading.items():
        lines += ["", f"## {heading.split(' › ')[-1]}"] + [f"- {t}" for t in texts]
    return "\n".join(lines).rstrip() + "\n"


def export_paths(store: dict | None = None) -> dict[str, str]:
    """{path in the ZIP: Markdown} in claude.ai's folder layout. Voice fingerprints never leave."""
    store = store or load_store()
    files: dict[str, str] = {}

    def put(path: str, text: str) -> None:
        """Adds a file to the export, adding '-2', '-3'… when two entries would get the same file name."""
        base, n = path, 2
        while path in files:
            path = base.replace(".md", f"-{n}.md")
            n += 1
        files[path] = text

    root = "memory"
    for entry in store["entries"]:
        if entry["kind"] == "profile":
            put(f"{root}/profile.md", entry_markdown(entry))
        elif entry["kind"] == "person":
            put(f"{root}/people/{slug(entry['name'])}.md", entry_markdown(entry))
        elif entry["kind"] == "project":
            put(f"{root}/projects/{_folder(entry['name'])}/index.md", entry_markdown(entry))
        elif entry.get("project"):
            put(f"{root}/projects/{_folder(entry['project'])}/areas/{slug(entry['name'])}.md", entry_markdown(entry))
        else:
            put(f"{root}/areas/{slug(entry['name'])}.md", entry_markdown(entry))
    return files


def _folder(name: str) -> str:
    """A safe folder name for a project (characters not allowed in file names replaced by '-')."""
    return re.sub(r"[\\/:*?\"<>|]+", "-", norm(name)) or "Project"


def export_zip(store: dict | None = None) -> bytes:
    """The whole memory as a ZIP in the claude.ai export layout (profile.md, people/, projects/<name>/, areas/),
    ready to download or re-import.
    """
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        for path, text in export_paths(store).items():
            zf.writestr(path, text)
    return buffer.getvalue()


def counts(store: dict | None = None) -> dict:
    """How many entries of each kind and facts there are (shown in Settings → Memory)."""
    store = store or load_store()
    out = {k: 0 for k in KINDS}
    for entry in store["entries"]:
        out[entry["kind"]] += 1
    out["facts"] = sum(len(e["facts"]) for e in store["entries"])
    return out
