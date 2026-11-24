#!/usr/bin/env python3
"""People memory: who appears in meetings, their role, and their voice fingerprints.

Stored as one JSON file (PEOPLE_FILE). It grows from the user's speaker-label
corrections, so later meetings can be labelled from voice and context:

  {"id", "name", "role", "aliases": [], "notes", "meetings": [tid],
   "voiceprints": [{"tid", "speaker", "vector": [...]}], "updated_at",
   # imported from claude.ai memory (claude_import.extract_people):
   "team", "projects": [], "relationships": [], "source": "claude.ai", "imported_at", "import_key",
   "evidence": [sentences], "user_edited": [field names the user changed since]}
"""

from __future__ import annotations

import json
import math
import os
import threading
import uuid
from datetime import datetime
from pathlib import Path

import memory_store

# Before the unified memory store, people lived in their own file. It's now read once by the
# migration (memory_store.migrate); without an explicit `path`, load/save use the store.
PEOPLE_FILE = Path(os.environ.get("PEOPLE_FILE", "./data/people.json"))
MAX_VOICEPRINTS = 8
_LOCK = memory_store.LOCK


def _now() -> str:
    """The current local time as an ISO string."""
    return datetime.now().isoformat(timespec="seconds")


def cosine(a: list[float], b: list[float]) -> float:
    """Cosine similarity of two voice fingerprints: close to 1 means very likely the same voice."""
    dot = sum(x * y for x, y in zip(a, b))
    na, nb = math.sqrt(sum(x * x for x in a)), math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


def load(path: Path | None = None) -> list[dict]:
    """People: the person entries of the memory store (or, with `path`, an old people.json)."""
    if path is None:
        return [e for e in memory_store.load_store()["entries"] if e["kind"] == "person"]
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return [p for p in data if isinstance(p, dict) and p.get("name")]


def save(people: list[dict], path: Path | None = None) -> None:
    """Saves the people list into the memory store as 'person' entries (or, with an explicit `path`, into an old-
    style people.json file, used by tests and the migration).
    """
    if path is None:
        with _LOCK:
            store = memory_store.load_store()
            others = [e for e in store["entries"] if e["kind"] != "person"]
            store["entries"] = others + [memory_store._normalize({**p, "kind": "person"}) for p in people]
            memory_store.save_store(store)
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(people, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(path)


def public(person: dict) -> dict:
    """Without the (large) voice vectors."""
    out = {k: v for k, v in person.items() if k != "voiceprints"}
    out["voiceprint_count"] = len(person.get("voiceprints") or [])
    return out


def forget_voice(person_id: str, path: Path | None = None) -> dict | None:
    """Delete every voice fingerprint stored for this person (they're biometric data)."""
    with _LOCK:
        people = load(path)
        for person in people:
            if person["id"] == person_id:
                person["voiceprints"] = []
                person["updated_at"] = _now()
                save(people, path)
                return person
    return None


def forget_meeting(tid: str, path: Path | None = None) -> int:
    """Deletes the voice fingerprints one meeting taught (when the meeting is deleted, or on request). Returns how
    many were deleted."""
    with _LOCK:
        people = load(path)
        count = 0
        for person in people:
            keep = [v for v in person.get("voiceprints") or [] if v.get("tid") != tid]
            count += len(person.get("voiceprints") or []) - len(keep)
            person["voiceprints"] = keep
        if count:
            save(people, path)
        return count


def forget_all_voices(path: Path | None = None) -> int:
    """Deletes every stored voice fingerprint (names, roles and facts stay). Returns how many were deleted."""
    with _LOCK:
        people = load(path)
        count = sum(len(p.get("voiceprints") or []) for p in people)
        for person in people:
            person["voiceprints"] = []
        save(people, path)
        return count


def note_screen_name(tid: str, speaker: str, name: str, count: int, path: Path | None = None) -> None:
    """Remember that this person's name was seen on their video tile in a meeting."""
    with _LOCK:
        people = load(path)
        person = find_by_name(people, name)
        if person is None:
            return
        seen = [s for s in person.get("screen_names") or [] if s.get("tid") != tid]
        seen.append({"tid": tid, "speaker": speaker, "name": name, "count": count, "at": _now()})
        person["screen_names"] = seen[-30:]
        save(people, path)


def find_by_name(people: list[dict], name: str) -> dict | None:
    """The person whose name or one of whose aliases matches `name` (ignoring case), or None."""
    key = name.strip().lower()
    for person in people:
        if person["name"].lower() == key or key in [a.lower() for a in person.get("aliases", [])]:
            return person
    return None


def upsert(name: str, role: str = "", notes: str | None = None, aliases: list[str] | None = None,
           path: Path | None = None) -> dict:
    """Finds a person by name or creates them, then updates the given fields. Returns the person."""
    with _LOCK:
        people = load(path)
        person = find_by_name(people, name)
        if person is None:
            person = _new_person(name)
            people.append(person)
        if role:
            person["role"] = role.strip()
        if notes is not None:
            person["notes"] = notes
        if aliases is not None:
            person["aliases"] = [a.strip() for a in aliases if a.strip()]
        person["updated_at"] = _now()
        save(people, path)
        return person


def update(person_id: str, fields: dict, path: Path | None = None) -> dict | None:
    """Updates fields of one person by id, remembering which fields the user edited so an import never overwrites
    them. None if the person doesn't exist.
    """
    with _LOCK:
        people = load(path)
        for person in people:
            if person["id"] == person_id:
                for key in ("name", "role", "notes", "team"):
                    if isinstance(fields.get(key), str) and fields[key].strip() != person.get(key, ""):
                        person[key] = fields[key].strip()
                        _mark_edited(person, key)
                for key in ("aliases", "projects", "relationships"):
                    if isinstance(fields.get(key), list):
                        person[key] = [str(a).strip() for a in fields[key] if str(a).strip()]
                        _mark_edited(person, key)
                person["updated_at"] = _now()
                save(people, path)
                return person
    return None


def delete(person_id: str, path: Path | None = None) -> None:
    """Removes a person (and their voice fingerprints)."""
    with _LOCK:
        save([p for p in load(path) if p["id"] != person_id], path)


def forget_label(tid: str, speaker: str, people: list[dict]) -> None:
    """Drop what an earlier correction of this speaker taught (it may have been wrong)."""
    for person in people:
        person["voiceprints"] = [v for v in person.get("voiceprints", [])
                                 if not (v.get("tid") == tid and v.get("speaker") == speaker)]


def learn(tid: str, speaker: str, name: str, role: str = "", vector: list[float] | None = None,
          path: Path | None = None, source: str = "user") -> dict | None:
    """Remember a confirmed label: the person's role, this meeting and (if any) their voice.
    `source`: "user" (the user confirmed it) or "screen" (their name was on their video tile)."""
    with _LOCK:
        people = load(path)
        # If this speaker of this meeting was labelled before (maybe as someone else), first undo what that
        # taught: otherwise a corrected mistake would keep a wrong voice fingerprint.
        forget_label(tid, speaker, people)
        if not name.strip():
            save(people, path)
            return None
        person = find_by_name(people, name)
        if person is None:
            person = _new_person(name)
            people.append(person)
        if role.strip():
            person["role"] = role.strip()
        if tid not in person["meetings"]:
            person["meetings"].append(tid)
        if vector:
            person["voiceprints"].append({"tid": tid, "speaker": speaker, "vector": vector, "source": source,
                                          "at": _now()})
            # Keep only the most recent fingerprints per person (enough to recognise them, and the file stays
            # small).
            person["voiceprints"] = person["voiceprints"][-MAX_VOICEPRINTS:]
        person["updated_at"] = _now()
        save(people, path)
        return person


def _mark_edited(person: dict, key: str) -> None:
    """Notes that the user changed `key` on an imported person, so re-imports leave it alone."""
    if person.get("source") and key not in person.setdefault("user_edited", []):
        person["user_edited"].append(key)


def _new_person(name: str) -> dict:
    """A new, empty person record with a random id."""
    return {"id": uuid.uuid4().hex[:12], "kind": "person", "name": name.strip(), "role": "", "aliases": [],
            "notes": "", "meetings": [], "voiceprints": [], "facts": [], "description": ""}


def match_imported(people: list[dict], entry: dict) -> dict | None:
    """The stored person an imported entry refers to: same import key, same name/alias, or the
    same first name when one of the two is only a first name ("Cian" ↔ "Cian Byrne")."""
    for person in people:
        if entry.get("key") and person.get("import_key") == entry["key"]:
            return person
    found = find_by_name(people, entry["name"])
    if found:
        return found
    first = entry["name"].split()[0].lower()
    candidates = [p for p in people if p["name"].split()[0].lower() == first
                  and (" " not in p["name"] or " " not in entry["name"])]
    return candidates[0] if len(candidates) == 1 else None


def imported_changes(person: dict | None, entry: dict) -> list[str]:
    """Fields an import would add or change (user-edited fields are never overwritten)."""
    if person is None:
        return ["new"]
    edited = set(person.get("user_edited") or [])
    changes = []
    if entry.get("role") and entry["role"] != person.get("role") and "role" not in edited \
            and (not person.get("role") or person.get("source")):
        changes.append("role")
    if entry.get("team") and entry["team"] != person.get("team") and "team" not in edited:
        changes.append("team")
    for key in ("projects", "relationships"):
        if key not in edited and set(entry.get(key) or []) - set(person.get(key) or []):
            changes.append(key)
    return changes


def merge_imported(entries: list[dict], imported_at: str, source: str = "claude.ai",
                   path: Path | None = None) -> dict:
    """Add or update people from an import. Re-importing updates the same records."""
    added = updated = 0
    with _LOCK:
        people = load(path)
        for entry in entries:
            person = match_imported(people, entry)
            changes = imported_changes(person, entry)
            if person is None:
                person = _new_person(entry["name"])
                people.append(person)
                added += 1
                changes = ["role", "team", "projects", "relationships"]
            elif changes:
                updated += 1
            edited = set(person.get("user_edited") or [])
            if "role" in changes or (entry.get("role") and not person.get("role")):
                person["role"] = entry["role"]
            if "team" in changes:
                person["team"] = entry["team"]
            for key in ("projects", "relationships", "aliases"):
                if key not in edited:
                    person[key] = list(dict.fromkeys((person.get(key) or []) + (entry.get(key) or [])))
            person["aliases"] = [a for a in person.get("aliases", []) if a.lower() != person["name"].lower()]
            if " " in entry["name"] and " " not in person["name"] and "name" not in edited:
                person["aliases"] = list(dict.fromkeys(person.get("aliases", []) + [person["name"]]))
                person["name"] = entry["name"]
            person.setdefault("team", "")
            person["evidence"] = list(dict.fromkeys((entry.get("evidence") or []) + (person.get("evidence") or [])))[:6]
            person["source"] = person.get("source") or source
            person["import_key"] = entry.get("key") or person.get("import_key", "")
            person["imported_at"] = imported_at
            person["is_user"] = bool(entry.get("is_user") or person.get("is_user"))
            person["updated_at"] = _now()
        save(people, path)
    return {"added": added, "updated": updated}


def merge(keep_id: str, merge_ids: list[str], path: Path | None = None) -> dict | None:
    """Merge duplicates into `keep_id`: names become aliases; meetings, voices, projects combine."""
    with _LOCK:
        people = load(path)
        keep = next((p for p in people if p["id"] == keep_id), None)
        others = [p for p in people if p["id"] in merge_ids and p["id"] != keep_id]
        if keep is None:
            return None
        for other in others:
            keep["aliases"] = list(dict.fromkeys(keep.get("aliases", []) + [other["name"]] + other.get("aliases", [])))
            keep["aliases"] = [a for a in keep["aliases"] if a.lower() != keep["name"].lower()]
            for key in ("meetings", "projects", "relationships", "evidence"):
                keep[key] = list(dict.fromkeys((keep.get(key) or []) + (other.get(key) or [])))
            keep["voiceprints"] = ((keep.get("voiceprints") or []) + (other.get("voiceprints") or []))[-MAX_VOICEPRINTS:]
            for key in ("role", "team", "notes", "source", "imported_at", "import_key"):
                if not keep.get(key) and other.get(key):
                    keep[key] = other[key]
        keep["updated_at"] = _now()
        save([p for p in people if p not in others], path)
        return keep


def duplicate_groups(people: list[dict]) -> list[list[str]]:
    """Ids of people who are probably the same person (same name, or one is a first name only)."""
    groups: dict[str, list[dict]] = {}
    for person in people:
        groups.setdefault(person["name"].split()[0].lower(), []).append(person)
    out = []
    for members in groups.values():
        if len(members) < 2:
            continue
        full = {p["name"].lower() for p in members if " " in p["name"]}
        if len(full) <= 1:  # "Cian" + "Cian Byrne", or the same name twice
            out.append([p["id"] for p in members])
    return out


def remove_imported(path: Path | None = None) -> int:
    """Undo imports: imported-only people are deleted; others lose the imported fields."""
    removed = 0
    with _LOCK:
        kept = []
        for person in load(path):
            if not person.get("source"):
                kept.append(person)
                continue
            if not person.get("meetings") and not person.get("voiceprints") and not person.get("user_edited"):
                removed += 1
                continue
            for key in ("source", "imported_at", "import_key", "evidence", "is_user"):
                person.pop(key, None)
            kept.append(person)
        save(kept, path)
    return removed
