#!/usr/bin/env python3
"""Parse a claude.ai data export: conversations, memory, projects, and the people in them.

claude.ai has no API for this; the user exports their data and uploads the files:
  - Full export (Settings → Privacy → Export data): a ZIP of JSON files.
  - Memory-only export (memory settings): text / Markdown.
  - Individual JSON files from the ZIP.

The export format is not documented and changes over time, so files are recognised by
their content rather than their names, unknown fields are ignored, and problems become
warnings instead of failures. Observed shape (see README → "claude.ai export format"):

  conversations.json  [{uuid, name, created_at, updated_at, account{uuid}, summary?,
                        chat_messages: [{uuid, sender: human|assistant, text, created_at,
                                         content: [{type: text|tool_use|tool_result, text, …}],
                                         attachments: [{file_name, extracted_content}], files}]}]
  projects.json       [{uuid, name, description, prompt_template, created_at, docs: [{filename, content}]}]
  users.json          [{uuid, full_name, email_address, verified_phone_number}]
  memories.json       memory text, e.g. [{conversations_memory: "…", project_memories: {uuid: "…"}}]

Nothing here talks to the network.
"""

from __future__ import annotations

import hashlib
import io
import json
import re
import zipfile
from collections import defaultdict
from dataclasses import dataclass, field

SOURCE = "claude.ai"
MAX_MEMBER_BYTES = 400 * 1024 * 1024
TEXT_SUFFIXES = (".txt", ".md", ".markdown")


@dataclass
class ParsedExport:
    """Everything found in the uploaded files, before anything is saved: conversations, memory facts, people,
    projects, the account's name, per-file counts, warnings, and each memory file's front matter (`sections`).
    """
    conversations: list[dict] = field(default_factory=list)
    memory: list[dict] = field(default_factory=list)
    people: list[dict] = field(default_factory=list)
    projects: list[dict] = field(default_factory=list)
    account_name: str = ""
    files: list[dict] = field(default_factory=list)      # [{name, kind, items}]
    warnings: list[str] = field(default_factory=list)
    # Front matter of each memory file: {section, origin, project, kind, description, aliases}
    sections: list[dict] = field(default_factory=list)

    def as_dict(self) -> dict:
        """The parsed export as a plain dict (for JSON)."""
        return {"conversations": self.conversations, "memory": self.memory, "people": self.people,
                "projects": self.projects, "account_name": self.account_name, "files": self.files,
                "warnings": self.warnings, "sections": self.sections}


def norm(text: str) -> str:
    """Collapses all whitespace to single spaces and trims the ends."""
    return re.sub(r"\s+", " ", str(text or "")).strip()


def key_for(*parts: str) -> str:
    """A stable 16-character key for a fact (a hash of its normalised, lowercase text). Re-importing the same
    fact gives the same key, which is how duplicates are spotted.
    """
    return hashlib.sha1("\x00".join(norm(p).lower() for p in parts).encode()).hexdigest()[:16]


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def parse_upload(files: list[tuple[str, bytes]], pasted_text: str = "") -> ParsedExport:
    """Parse uploaded files (ZIP, JSON, text/Markdown) and/or pasted memory text."""
    out = ParsedExport()
    raw_memory: list[tuple[str, str, str]] = []   # (text, origin, project)
    for name, data in files:
        lower = name.lower()
        if lower.endswith(".zip") or data[:4] == b"PK\x03\x04":
            _parse_zip(name, data, out, raw_memory)
        else:
            _parse_member(name, data, out, raw_memory)
    if pasted_text.strip():
        raw_memory.append((pasted_text, "pasted text", "", "memory"))
        out.files.append({"name": "Pasted text", "kind": "memory", "items": 0})
    project_names = {p["uuid"]: p["name"] for p in out.projects if p.get("uuid")}
    person_files: list[dict] = []
    profile_names: list[str] = []
    for text, origin, project, kind in raw_memory:
        if kind == "profile" and not project:
            profile_names.append(frontmatter(text)[1])  # profile.md is about the user
        meta, _ = frontmatter(text)
        if kind == "profile" and not out.account_name and _looks_like_name(meta.get("name", "")):
            out.account_name = meta["name"]
        if project and project not in project_names and project not in [p["name"] for p in out.projects]:
            out.projects.append({"uuid": "", "name": project, "description": meta.get("description", "")[:500]})
        entries = memory_entries(text, origin, project_names.get(project, project), kind)
        aliases = meta.get("aliases") or []
        aliases = [norm(str(a)) for a in (aliases if isinstance(aliases, list) else str(aliases).split(","))
                   if norm(str(a))]
        if kind in ("profile", "area", "project") and (meta.get("description") or aliases):
            out.sections.append({"section": section_base(meta, origin, project_names.get(project, project), kind),
                                 "origin": origin, "project": project_names.get(project, project), "kind": kind,
                                 "description": norm(str(meta.get("description") or ""))[:500], "aliases": aliases})
        if kind == "person":
            person_files.append(person_from_file(meta, origin, entries, project))
        for f in out.files:
            if f["name"] == origin or (origin == "pasted text" and f["name"] == "Pasted text"):
                f["items"] += len(entries)
        out.memory.extend(entries)
    if not out.account_name and profile_names:
        out.account_name = user_name_from_profile("\n".join(profile_names), [m["text"] for m in out.memory])
    out.memory = _dedupe(out.memory, "key")
    out.conversations = _dedupe(out.conversations, "uuid")
    out.people = merge_people(extract_people(out.memory, [p["name"] for p in out.projects], out.account_name),
                              person_files)
    if not (out.conversations or out.memory or out.projects):
        out.warnings.append("No conversations or memory were found. Upload the ZIP from claude.ai → Settings → "
                            "Privacy → Export data, a JSON file from it, or your exported memory text.")
    return out


def _dedupe(items: list[dict], key: str) -> list[dict]:
    """Removes items with the same `key` value; the later copy wins (e.g. a file uploaded twice)."""
    seen: dict[str, dict] = {}
    for item in items:
        seen[item[key]] = item  # the later copy wins (e.g. same file uploaded twice)
    return list(seen.values())


def _parse_zip(name: str, data: bytes, out: ParsedExport, raw_memory: list) -> None:
    """Reads every file inside an uploaded ZIP (skipping folders and macOS '__MACOSX' metadata) and parses each
    one with _parse_member.
    """
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        out.warnings.append(f"{name}: not a valid ZIP file (it may be incomplete or damaged).")
        out.files.append({"name": name, "kind": "error", "items": 0})
        return
    skipped = 0
    for info in archive.infolist():
        if info.is_dir() or "__MACOSX" in info.filename or info.filename.split("/")[-1].startswith("."):
            continue
        lower = info.filename.lower()
        if not (lower.endswith(".json") or lower.endswith(TEXT_SUFFIXES)):
            skipped += 1
            continue
        if info.file_size > MAX_MEMBER_BYTES:
            out.warnings.append(f"{info.filename}: larger than 400 MB, skipped.")
            continue
        try:
            member = archive.read(info)
        except (zipfile.BadZipFile, OSError, RuntimeError) as exc:
            out.warnings.append(f"{info.filename}: couldn't be read from the ZIP ({exc}).")
            continue
        _parse_member(info.filename, member, out, raw_memory)
    if skipped:
        out.warnings.append(f"Skipped {skipped} file{'s' if skipped != 1 else ''} that aren't JSON or text "
                            "(attachments, images).")


def _parse_member(name: str, data: bytes, out: ParsedExport, raw_memory: list) -> None:
    """Parses one file: JSON (conversations, projects, memory, users) or text/Markdown memory. Problems become
    warnings rather than errors.
    """
    short = name.split("/")[-1]
    text = data.decode("utf-8-sig", errors="replace")
    if short.lower().endswith(".json") or text.lstrip()[:1] in ("[", "{"):
        try:
            payload = json.loads(text)
        except json.JSONDecodeError as exc:
            if short.lower().endswith(".json"):
                out.warnings.append(f"{short}: not valid JSON (line {exc.lineno}: {exc.msg}). Skipped.")
                out.files.append({"name": short, "kind": "error", "items": 0})
                return
            payload = None
        if payload is not None:
            _classify_json(short, payload, out, raw_memory)
            return
    if short.lower().endswith(TEXT_SUFFIXES) or not short.lower().endswith(".json"):
        if text.strip():
            rel, project, kind = path_context(name)
            raw_memory.append((text, rel, project, kind))
            out.files.append({"name": rel, "kind": "memory", "items": 0})


# ---------------------------------------------------------------------------
# JSON shapes
# ---------------------------------------------------------------------------

def _records(payload) -> list[dict]:
    """A top-level list, or the first list found under a wrapper key ({"conversations": [...]})."""
    if isinstance(payload, list):
        return [r for r in payload if isinstance(r, dict)]
    if isinstance(payload, dict):
        for key in ("conversations", "data", "items", "projects", "users", "memories", "results"):
            if isinstance(payload.get(key), list):
                return [r for r in payload[key] if isinstance(r, dict)]
        return [payload]
    return []


def _looks_like(records: list[dict], *keys: str) -> bool:
    """True when the first records have any of these keys (how a JSON file's contents are recognised, whatever
    the file is called).
    """
    return any(any(k in r for k in keys) for r in records[:20])


def _classify_json(name: str, payload, out: ParsedExport, raw_memory: list) -> None:
    """Works out what a JSON file holds (conversations, projects, memory or the user profile) from its keys, and
    adds it to `out`.
    """
    records = _records(payload)
    if _looks_like(records, "chat_messages", "messages") and _looks_like(records, "uuid", "name", "created_at"):
        before = len(out.conversations)
        bad = 0
        for record in records:
            conv = parse_conversation(record)
            if conv is None:
                bad += 1
            else:
                out.conversations.append(conv)
        if bad:
            out.warnings.append(f"{name}: {bad} conversation{'s' if bad != 1 else ''} without readable messages skipped.")
        out.files.append({"name": name, "kind": "conversations", "items": len(out.conversations) - before})
        return
    if _looks_like(records, "prompt_template", "docs", "is_starter_project"):
        for record in records:
            if record.get("name") or record.get("title"):
                out.projects.append({"uuid": str(record.get("uuid") or ""),
                                     "name": norm(record.get("name") or record.get("title")),
                                     "description": norm(record.get("description"))[:500]})
            # Project knowledge files and instructions aren't memory; their *names* help find projects.
        out.files.append({"name": name, "kind": "projects", "items": len(records)})
        return
    if _looks_like(records, "email_address", "full_name") and not _memory_strings(payload):
        names = [norm(r.get("full_name")) for r in records if r.get("full_name")]
        out.account_name = names[0] if names else out.account_name
        out.files.append({"name": name, "kind": "account", "items": len(records)})
        return
    found = _memory_strings(payload)
    if found:
        for text, project in found:
            raw_memory.append((text, name, project, "memory"))
        out.files.append({"name": name, "kind": "memory", "items": 0})
        return
    out.warnings.append(f"{name}: not recognised (no conversations, projects or memory). Skipped.")
    out.files.append({"name": name, "kind": "unknown", "items": 0})


def _memory_strings(payload, path: tuple = (), project: str = "") -> list[tuple[str, str]]:
    """Memory text anywhere in a JSON value: strings under keys containing "memor" (or a
    list of {content|text|memory} entries under such a key). Returns [(text, project_uuid)];
    under a key like "project_memories", each sub-key is the project's id."""
    found: list[tuple[str, str]] = []
    in_memory = any("memor" in str(p).lower() for p in path)
    if isinstance(payload, str):
        if in_memory and len(payload.strip()) >= 3:
            found.append((payload, project))
    elif isinstance(payload, dict):
        by_project = bool(path) and "project" in str(path[-1]).lower()
        for key, value in payload.items():
            if in_memory and key in ("uuid", "id", "created_at", "updated_at", "account_uuid"):
                continue
            found += _memory_strings(value, path + (key,), str(key) if by_project else project)
    elif isinstance(payload, list):
        for item in payload:
            if in_memory and isinstance(item, dict):
                text = item.get("content") or item.get("text") or item.get("memory")
                if isinstance(text, str):
                    found.append((text, str(item.get("project_uuid") or project)))
                    continue
            found += _memory_strings(item, path, project)
    return found


def _block_text(block) -> str:
    """The readable text of one message content block (text, a tool call or a tool result)."""
    if isinstance(block, str):
        return block
    if not isinstance(block, dict):
        return ""
    kind = block.get("type")
    if kind == "text" or (kind is None and isinstance(block.get("text"), str)):
        return block.get("text") or ""
    if kind == "tool_use":
        data = block.get("input") or {}
        if isinstance(data, dict) and isinstance(data.get("content"), str):
            title = data.get("title") or "Artifact"
            return f"\n\n**{title}**\n\n{data['content']}"
        return ""
    if kind == "tool_result":
        inner = block.get("content")
        if isinstance(inner, list):
            return "".join(_block_text(b) for b in inner if isinstance(b, dict) and b.get("type") == "text")
    return ""


def parse_conversation(record: dict) -> dict | None:
    """One claude.ai conversation as {uuid, title, dates, messages}; None when the record isn't a conversation."""
    messages_in = record.get("chat_messages")
    if not isinstance(messages_in, list):
        messages_in = record.get("messages") if isinstance(record.get("messages"), list) else None
    if messages_in is None:
        return None
    messages = []
    for m in messages_in:
        if not isinstance(m, dict):
            continue
        sender = str(m.get("sender") or m.get("role") or "").lower()
        role = "user" if sender in ("human", "user") else "assistant" if sender in ("assistant", "claude", "model") else None
        if role is None:
            continue
        content = m.get("content")
        text = "".join(_block_text(b) for b in content) if isinstance(content, list) else ""
        if not text.strip():
            text = (m.get("text") if isinstance(m.get("text"), str) else "") or (content if isinstance(content, str) else "")
        files = [a.get("file_name") or a.get("name") for a in (m.get("attachments") or []) + (m.get("files") or [])
                 if isinstance(a, dict) and (a.get("file_name") or a.get("name"))]
        if not str(text).strip() and not files:
            continue
        if files:
            text = (str(text).rstrip() + "\n\n📎 *attached: " + ", ".join(dict.fromkeys(files)) + "*").strip()
        messages.append({"role": role, "content": str(text).strip(), "created_at": str(m.get("created_at") or "")})
    if not messages:
        return None
    uuid = str(record.get("uuid") or record.get("id") or key_for(str(record.get("name")), str(record.get("created_at"))))
    created = str(record.get("created_at") or messages[0]["created_at"] or "")
    updated = str(record.get("updated_at") or messages[-1]["created_at"] or created)
    title = norm(record.get("name") or record.get("title")) or _title_from(messages)
    return {"uuid": uuid, "title": title[:120], "created_at": created, "updated_at": updated,
            "messages": messages, "message_count": len(messages),
            "project_uuid": str(record.get("project_uuid") or (record.get("project") or {}).get("uuid", "")
                                if isinstance(record.get("project"), (dict, type(None))) else "")}


def _title_from(messages: list[dict]) -> str:
    """A title from the first user message (60 characters), for conversations without a name."""
    first = next((m["content"] for m in messages if m["role"] == "user"), "Untitled conversation")
    first = norm(first)
    return first[:60] + ("…" if len(first) > 60 else "")


# ---------------------------------------------------------------------------
# Memory text → entries
# ---------------------------------------------------------------------------

_HEADING = re.compile(r"^\s*(?:#{1,6}\s+(.+?)|\*\*([^*]+?)\*\*:?|__([^_]+)__:?|([A-Z][A-Za-z /&-]{2,40}):)\s*$")
_BULLET = re.compile(r"^\s*(?:[-*•·]|\d+[.)])\s+")
_SENTENCE = re.compile(r"(?<=[.!?])\s+(?=[A-Z\"'(])")


KNOWN_DIRS = {"areas", "people", "projects"}


def path_context(path: str) -> tuple[str, str, str]:
    """(path shown to the user, project, kind) for a file in a memory export.

    The claude.ai memory export is a folder of Markdown files with YAML front matter:
      profile.md                         about the user        → kind "profile"
      areas/<topic>.md                   a topic               → kind "area"
      people/<name>.md                   one person            → kind "person"
      projects/<Project>/index.md        the project           → kind "project"
      projects/<Project>/{profile.md, areas/<topic>.md}       → that project
    """
    parts = [p for p in path.replace("\\", "/").split("/") if p]
    if len(parts) > 1 and parts[0] not in KNOWN_DIRS and parts[0].lower() not in ("profile.md", "index.md"):
        parts = parts[1:]  # the export's wrapper folder, e.g. "claude-memory-2026-11-20"
    rel = "/".join(parts)
    project = ""
    if "projects" in parts:
        i = parts.index("projects")
        if i + 2 < len(parts):
            project = parts[i + 1]
    file = parts[-1].lower() if parts else ""
    kind = ("person" if "people" in parts[:-1] else "profile" if file == "profile.md"
            else "project" if file == "index.md" and project else "area" if "areas" in parts[:-1] else "memory")
    return rel, project, kind


def frontmatter(text: str) -> tuple[dict, str]:
    """YAML-ish front matter (name, description, aliases) and the body after it."""
    text = text.replace("\r\n", "\n").lstrip("\ufeff")
    if not text.startswith("---\n"):
        return {}, text
    end = text.find("\n---", 4)
    if end < 0:
        return {}, text
    meta: dict = {}
    key = ""
    for line in text[4:end].splitlines():
        item = re.match(r"^\s+-\s+(.*)$", line)
        if item and key:
            meta.setdefault(key, [])
            if isinstance(meta[key], list):
                meta[key].append(item.group(1).strip().strip("'\""))
            continue
        if ":" in line:
            key, value = (x.strip() for x in line.split(":", 1))
            value = value.strip("'\"")
            if value.startswith("[") and value.endswith("]"):
                value = [v.strip().strip("'\"") for v in value[1:-1].split(",") if v.strip()]
            meta[key] = value
    body = text[end + 4:]
    return meta, body[body.find("\n") + 1:] if "\n" in body else ""


def humanize(name: str) -> str:
    """Turns a file-style name into a readable one: 'job-search' -> 'Job search'."""
    name = norm(str(name or ""))
    if re.fullmatch(r"[a-z0-9]+(?:[-_][a-z0-9]+)*", name):
        name = name.replace("-", " ").replace("_", " ")
        name = name[:1].upper() + name[1:]
    return name


def _looks_like_name(text: str) -> bool:
    """True when a text looks like a person's name (one or two capitalised words that aren't common words)."""
    return bool(re.fullmatch(NAME, norm(text))) and norm(text).split()[0] not in NOT_NAMES


def section_base(meta: dict, origin: str, project: str = "", kind: str = "memory") -> str:
    """The section a memory file's facts go under, e.g. "Profile", "People › Owen", "Atlas › Roadmap"."""
    stem = re.sub(r"\.(md|markdown|txt)$", "", origin.split("/")[-1], flags=re.I)
    title = humanize(meta.get("name") or (stem if meta else ""))
    if kind == "profile":
        base = "Profile"
    elif kind == "person":
        base = f"People › {title or humanize(stem)}"
    else:
        base = title or project or "Memory"
    if project and kind in ("profile", "area", "project") and not base.startswith(project):
        base = f"{project} › {base}"
    return base


def memory_entries(text: str, origin: str, project: str = "", kind: str = "memory") -> list[dict]:
    """One entry per bullet or sentence. Front matter names the section (its description is an
    entry too); "##" headings inside a file become sub-sections."""
    meta, body = frontmatter(text)
    base = section_base(meta, origin, project, kind)
    entries, section = [], base
    description = norm(meta.get("description") or "")
    lines = ([f"- {description}"] if len(description) >= 8 else []) + body.split("\n")
    for raw in lines:
        line = raw.strip()
        if not line:
            continue
        heading = _HEADING.match(line)
        if heading and len(line) < 80:
            sub = norm(next(g for g in heading.groups() if g)).strip(":")
            section = f"{base} › {sub}" if meta else (sub or section)
            continue
        line = _BULLET.sub("", line)
        line = re.sub(r"\*\*|__|`", "", line)
        pieces = [line] if len(line) < 220 else _SENTENCE.split(line)
        for piece in pieces:
            piece = norm(piece)
            if len(piece) < 8 or not re.search(r"[A-Za-z]{3}", piece):
                continue
            entries.append({"key": key_for(piece), "text": piece, "section": section, "origin": origin,
                            "project": project})
    return entries


# ---------------------------------------------------------------------------
# People, roles, teams, projects, relationships
# ---------------------------------------------------------------------------

NAME = r"[A-Z][a-z]+(?:[-'][A-Z]?[a-z]+)?(?: [A-Z][a-z]+(?:[-'][A-Z]?[a-z]+)?)?"
NOT_NAMES = {
    "The", "This", "That", "These", "They", "He", "She", "We", "It", "His", "Her", "Their", "Our", "My", "Your",
    "User", "Users", "Claude", "Assistant", "I", "In", "On", "At", "For", "With", "And", "But", "Also", "When",
    "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday", "January", "February", "March",
    "April", "May", "June", "July", "August", "September", "October", "November", "December", "Project",
    "Projects", "Team", "Work", "Personal", "Memory", "Top", "Brief", "History", "Context", "Current", "Recent",
    "Previously", "Currently", "Mobile", "Android", "iOS", "Web", "Sprint", "Release", "QA", "API", "Payments",
    "Globant", "Google", "Microsoft", "Apple", "Amazon", "Jira", "Slack", "Teams", "Zoom", "Confluence", "Python",
    "Java", "React", "Next", "Docker", "AWS", "Azure", "Staging", "Production",
    # places
    "India", "Ireland", "Dublin", "Cork", "Galway", "Belfast", "London", "Europe", "America", "USA", "UK", "US",
    "Pune", "Mumbai", "Delhi", "Bangalore", "Bengaluru", "Hyderabad", "Chennai", "Kolkata", "Noida", "Gurgaon",
    "Gurugram", "Ahmedabad", "Remote", "Office",
}
ROLE_TITLES = {"Engineer", "Developer", "Manager", "Lead", "Tester", "Analyst", "Architect", "Designer", "Director",
               "Consultant", "Specialist", "Intern", "Senior", "Junior", "Principal", "Head", "Contact", "Contacts",
               "Notes", "Team", "Teams", "Member", "Members", "Owner", "Keen", "Key", "Current", "Former"}
ROLE_WORDS = (r"(?:manager|lead|leader|head|director|engineer|developer|dev|tester|analyst|architect|designer|"
              r"owner|scrum master|consultant|specialist|administrator|admin|officer|vp|cto|ceo|cfo|coo|"
              r"president|intern|recruiter|coordinator|coach|stakeholder|sponsor|client|customer|"
              r"product owner|qa|sre|devops|pm|po|ba|founder|partner|executive|editor|writer|researcher|"
              r"scientist|support|tech lead|technical lead|team lead)")
_ROLE_RE = re.compile(rf"\b((?:[a-z0-9&/-]+ ){{0,3}}{ROLE_WORDS})\b", re.I)
_TEAM_RE = re.compile(r"\b(?:on|in|of|from|for|leads?|manages?|runs?|heads?) (?:the |our |their )?"
                      r"([A-Z][\w&+-]*(?: [A-Z][\w&+-]*){0,2}) (?:team|squad|group|department|dept)\b")
_PROJECT_RE = re.compile(r"\b(?:[Pp]roject|[Pp]rogramme|[Pp]rogram|[Ii]nitiative) ([A-Z][\w-]*(?: [A-Z][\w-]*)?)"
                         r"|\b(?:the |on |for )([A-Z][\w-]*(?: [A-Z][\w-]*)?) (?:project|app|platform|programme|program|initiative|account|client)\b")


def _names_in(sentence: str) -> list[str]:
    """The person-like names in a sentence (capitalised words that aren't days, places, products or @mentions)."""
    out = []
    for match in re.finditer(rf"(?<![\w-])({NAME})(?![\w-])", sentence):
        name = match.group(1)
        first = name.split()[0]
        if first in NOT_NAMES or name in NOT_NAMES:
            # "The Payments" etc.; keep the second word if it's a name ("Project Atlas" is a project).
            continue
        if match.start() > 0 and sentence[match.start() - 1] in "@#/":
            continue
        out.append(name)
    return list(dict.fromkeys(out))


def _clean_role(role: str) -> str:
    """Tidies a role found in text: drops leading words like 'the' or 'currently' and punctuation, max 60
    characters.
    """
    role = norm(role).strip(" ,.;:-–—")
    while True:
        stripped = re.sub(r"^(?:the|a|an|our|their|his|her|my|also|now|currently)\s+", "", role, flags=re.I)
        if stripped == role:
            break
        role = stripped
    return role[:60]


def _role_after(sentence: str, name: str) -> str:
    """Role stated for `name`: "Cian is the release manager", "Cian (release manager)", "Cian — QA lead"."""
    n = re.escape(name)
    patterns = [
        rf"\b{n}\b,? (?:is|was|works as|acts as|serves as|became|has been)(?: now| currently| also)? (?:the |a |an |our |their )?([^.,;()]+)",
        rf"\b{n}\b \(([^)]+)\)",
        rf"\b{n}\b\s*[—–:-]\s*([^.,;()]+)",
        rf"\b{n}\b, (?:the |a |an |our )?([^,]+),",
        rf"(?:the |our |their )?([a-z][a-z /&-]*{ROLE_WORDS}),? {n}\b",
    ]
    for pattern in patterns:
        match = re.search(pattern, sentence, re.I if pattern.startswith("(?:the") else 0)
        if not match:
            continue
        candidate = match.group(1)
        candidate = re.split(r"\b(?:who|which|and (?:is|was|works)|at [A-Z]|where|because|since)\b", candidate)[0]
        role = _ROLE_RE.search(candidate)
        if role:
            return _clean_role(candidate[: role.end()])
    lead = re.search(rf"\b{n}\b (leads|manages|runs|heads|owns) (?:the |our )?([A-Z][\w&+-]*(?: [A-Z][\w&+-]*){{0,2}})", sentence)
    if lead:
        verb = {"leads": "lead", "manages": "manager", "runs": "lead", "heads": "head", "owns": "owner"}[lead.group(1)]
        return f"{lead.group(2)} {verb}"
    return ""


def _projects_in(sentence: str, known_projects: list[str]) -> list[str]:
    """Project names in a sentence: 'Project Atlas', 'the Atlas app', or a known project name."""
    found = []
    for match in _PROJECT_RE.finditer(sentence):
        name = match.group(1) or match.group(2)
        if name and name.split()[0] not in NOT_NAMES - {"Payments", "Mobile", "Web"}:
            found.append(name.strip())
    for project in known_projects:
        if project and re.search(rf"\b{re.escape(project)}\b", sentence, re.I):
            found.append(project)
    return list(dict.fromkeys(found))


_RELATION_VERBS = [("reports to", "reports to"), ("works closely with", "works with"), ("works with", "works with"),
                   ("worked with", "works with"), ("collaborates with", "works with"), ("partners with", "works with"),
                   ("manages", "manages"), ("mentors", "manages"), ("supervises", "manages")]


def relations(sentence: str, people_names: list[str]) -> list[tuple[str, str, str]]:
    """(a, verb, b): the person named nearest before the verb, and the person right after it."""
    spots = sorted((m.start(), name) for name in people_names
                   for m in re.finditer(r"(?<![\w-])" + re.escape(name) + r"(?![\w-])", sentence))
    out = []
    for phrase, verb in _RELATION_VERBS:
        for m in re.finditer(r"\b" + phrase + " (" + NAME + ")", sentence):
            b = next((n for n in people_names if m.group(1) == n or m.group(1).startswith(n + " ")), None)
            a = next((name for pos, name in reversed(spots) if pos < m.start() and name != b), None)
            if a and b:
                out.append((a, verb, b))
    return out


def extract_people(memory: list[dict], known_projects: list[str], account_name: str = "") -> list[dict]:
    """Structured people (person → role → team → projects, relationships) from memory entries."""
    people: dict[str, dict] = {}
    me = norm(account_name)
    me_first = me.split()[0] if me else ""
    # Words also written in lower case somewhere ("contacts", "engineer", "keen") are not names.
    lowercase_words = {w for e in memory for w in re.findall(r"\b[a-z][a-z'-]+\b", e["text"])}

    def plausible(name: str) -> bool:
        """False for 'names' made of ordinary words, job titles or words seen in lower case elsewhere (those
        aren't people).
        """
        words = name.split()
        if any(w.lower() in lowercase_words or w in NOT_NAMES or w in ROLE_TITLES for w in words):
            return False
        return True

    def person(name: str) -> dict:
        """Returns the record for a person, creating it if needed; 'Cian' and 'Cian Byrne' are merged when only
        one full name uses that first name.
        """
        key = name.lower()
        # "Cian" and "Cian Byrne" are the same person when only one full name uses that first name.
        for existing in list(people):
            if existing.split()[0] == key.split()[0] and (" " not in existing or " " not in key):
                if " " in key and " " not in existing:
                    people[key] = people.pop(existing)
                    people[key]["name"] = name
                key = key if key in people else existing
                break
        if key not in people:
            people[key] = {"key": key_for(name.split()[0] if " " not in name else name), "name": name, "role": "",
                           "team": "", "projects": [], "relationships": [], "evidence": [], "is_user": False}
        return people[key]

    for entry in memory:
        sentence = entry["text"]
        names = [n for n in _names_in(sentence) if plausible(n) or n == me]
        if me and re.search(r"\b(?:the user|user|I)\b", sentence) and me not in names:
            names = names + [me]
        projects = _projects_in(sentence, known_projects)
        project_words = {w for p in projects for w in p.split()}
        names = [n for n in names if n not in projects and n.split()[0] not in project_words]
        team_match = _TEAM_RE.search(sentence)
        for name in names:
            role = _role_after(sentence, name) if name != me or me in sentence else _role_after(sentence.replace("The user", me).replace("the user", me), me)
            near_team = team_match and (len(names) == 1 or name in sentence[: team_match.end()][-120:])
            related = any(name in (a, b) for a, _, b in relations(sentence, names))
            if not role and not near_team and not related and not (projects and name == me):
                continue  # just a mention (or an organisation: "at Globant", "for Acme Bank")
            p = person(name)
            if role and (not p["role"] or len(role) > len(p["role"])):
                p["role"] = role
            if near_team and not p["team"]:
                p["team"] = team_match.group(1)
            for project in projects:
                if project not in p["projects"]:
                    p["projects"].append(project)
            if sentence not in p["evidence"]:
                p["evidence"].append(sentence)
            p["is_user"] = p["is_user"] or bool(me and (name == me or name == me_first))
        for a, verb, b in relations(sentence, names):
            pa, pb = person(a), person(b)
            if f"{verb} {pb['name']}" not in pa["relationships"]:
                pa["relationships"].append(f"{verb} {b}")
            if verb == "works with" and f"works with {pa['name']}" not in pb["relationships"]:
                pb["relationships"].append(f"works with {a}")
    out = []
    for p in people.values():
        if not (p["role"] or p["team"] or p["projects"] or p["relationships"]):
            continue
        p["evidence"] = p["evidence"][:4]
        out.append(p)
    return sorted(out, key=lambda p: p["name"].lower())


def user_name_from_profile(profile: str, all_texts: list[str]) -> str:
    """The user's name: the most frequent full name in profile.md that is a real name
    (its words are never written in lower case anywhere in the memory)."""
    lowercase = {w for t in all_texts for w in re.findall(r"\b[a-z][a-z'-]+\b", t)}
    candidates = [n for n in _names_in(profile) if " " in n
                  and not any(w.lower() in lowercase or w in NOT_NAMES or w in ROLE_TITLES for w in n.split())]
    return max(dict.fromkeys(candidates), key=candidates.count) if candidates else ""


def person_from_file(meta: dict, origin: str, entries: list[dict], project: str = "") -> dict:
    """people/<name>.md: the file is about one person; its front matter names them."""
    stem = re.sub(r"\.(md|markdown|txt)$", "", origin.split("/")[-1], flags=re.I)
    name = norm(meta.get("name") or "") or humanize(stem)
    if name.islower():
        name = name.title()
    aliases = meta.get("aliases") or []
    aliases = [a for a in (aliases if isinstance(aliases, list) else str(aliases).split(",")) if str(a).strip()]
    texts = [e["text"] for e in entries]
    role = ""
    for text in texts[:4]:
        found = _role_after(text, name.split()[0]) or (_clean_role(text[: m.end()]) if (m := _ROLE_RE.search(text)) and len(text) < 90 else "")
        if found:
            role = found
            break
    projects = list(dict.fromkeys(([project] if project else []) + [p for t in texts for p in _projects_in(t, [])]))
    team = next((m.group(1) for t in texts if (m := _TEAM_RE.search(t))), "")
    return {"key": key_for(name if " " in name else name.split()[0]), "name": name, "role": role, "team": team,
            "projects": projects, "relationships": [], "evidence": texts[:4], "is_user": False,
            "aliases": [norm(str(a)) for a in aliases]}


def merge_people(found: list[dict], from_files: list[dict]) -> list[dict]:
    """People files win over what was read out of sentences; the same first name is one person."""
    out = list(found)
    for person in from_files:
        first = person["name"].split()[0].lower()
        twin = next((p for p in out if p["name"].lower() == person["name"].lower()
                     or (p["name"].split()[0].lower() == first and (" " not in p["name"] or " " not in person["name"]))), None)
        if twin is None:
            out.append(person)
            continue
        for key in ("role", "team"):
            twin[key] = person[key] or twin[key]
        for key in ("projects", "evidence", "aliases"):
            twin[key] = list(dict.fromkeys((person.get(key) or []) + (twin.get(key) or [])))
        if " " in person["name"]:
            twin["name"] = person["name"]
    return sorted(out, key=lambda p: p["name"].lower())


def people_in_conversations(conversations: list[dict], known: list[str]) -> dict[str, int]:
    """How many conversations mention each known name (used for search and speaker hints)."""
    counts: dict[str, int] = defaultdict(int)
    for conv in conversations:
        text = " ".join(m["content"] for m in conv["messages"])
        for name in known:
            if re.search(rf"\b{re.escape(name.split()[0])}\b", text):
                counts[name] += 1
    return dict(counts)
