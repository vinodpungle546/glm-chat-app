#!/usr/bin/env python3
"""Professional meeting notes: the skill in skills/meeting-notes, run with any chat model.

Pipeline (generate):
  1. Transcript → lines "[hh:mm:ss] Name: text" (low-confidence names marked "(?)").
  2. Meeting type: detected from the transcript (keywords in each template's front matter)
     unless the user picked one.
  3. The model returns JSON for schema.json (one call; long meetings: one call per part,
     then merged and de-duplicated). Invalid JSON → one retry with the validation errors;
     still invalid → the best partial result plus a warning.
  4. Post-processing, so even small local models give consistent notes:
     "Unassigned"/"TBD" + a needs-follow-up list, one owner per action, relative dates →
     actual dates, a timestamp on every decision and action, and every decision/action is
     traced back to a transcript line (items that can't be found are dropped, with a warning).
  5. Rendering with templates/<type>.md (a small Mustache subset), by detail level and
     section toggles. Exports live in notes_export.py.

Templates can be customised in the app; customised copies live in APP_DATA_DIR and the
originals in the repo stay untouched ("Reset" deletes the copy).
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Callable, Iterator

import jsonschema

import long_context
import relative_dates

SKILL_DIR = Path(__file__).resolve().parent / "skills" / "meeting-notes"
OVERRIDE_DIR = Path(os.environ.get("APP_DATA_DIR", "./data")) / "skills" / "meeting-notes"
WEB_URL = os.environ.get("WEB_PUBLIC_URL", "http://localhost:3000").rstrip("/")
SECTIONS = ["header", "tldr", "discussion", "type", "decisions", "actions", "risks", "questions", "next", "appendix"]
CORE = {"header", "tldr", "decisions", "actions"}
DETAIL_LEVELS = ("brief", "standard", "detailed")
TRACE_MIN = 0.34
_GENERIC = re.compile(r"^(?:SPEAKER_\d+|UNKNOWN|Speaker \d+|Unknown)$")
STOP = set("""a an the and or but of to in on at by for with from as is are was were be been it its this that
these those i you he she we they me him her us them my your our their will would should could can do does did
have has had just also so then than there here what which who when where how all any some not no yes ok okay
please thanks thank let lets get got going gonna want need make sure about into over after before up down out
today tomorrow week friday monday tuesday wednesday thursday""".split())

Complete = Callable[[list[dict]], str]
Stream = Callable[[list[dict]], Iterator[str]]
Emit = Callable[[dict], None]


# ---------------------------------------------------------------------------
# The skill on disk
# ---------------------------------------------------------------------------

def frontmatter(text: str) -> tuple[dict, str]:
    """Simple `key: value` YAML front matter (lists are comma-separated)."""
    if not text.startswith("---\n"):
        return {}, text
    end = text.find("\n---", 4)
    if end < 0:
        return {}, text
    meta = {}
    for line in text[4:end].splitlines():
        if ":" in line:
            key, value = line.split(":", 1)
            meta[key.strip()] = value.strip()
    return meta, text[end + 4:].lstrip("\n")


def _template_paths() -> dict[str, Path]:
    """Every built-in template file, by name: {'standup': Path, 'partials/header': Path, …}."""
    paths = {}
    for base in (SKILL_DIR / "templates",):
        for path in sorted(base.rglob("*.md")):
            paths[path.relative_to(base).with_suffix("").as_posix()] = path
    return paths


def template_names() -> list[str]:
    """The names of all built-in templates (meeting types and partials)."""
    return list(_template_paths())


def read_template(name: str) -> tuple[str, bool]:
    """(text, customised) — the user's copy if there is one."""
    override = OVERRIDE_DIR / "templates" / f"{name}.md"
    if override.exists():
        return override.read_text(encoding="utf-8"), True
    path = _template_paths().get(name)
    if path is None:
        raise KeyError(name)
    return path.read_text(encoding="utf-8"), False


def save_template(name: str, text: str) -> None:
    """Saves the user's edited version of a template (in OVERRIDE_DIR; the built-in file is never changed).
    KeyError for an unknown template.
    """
    if name not in _template_paths():
        raise KeyError(name)
    path = OVERRIDE_DIR / "templates" / f"{name}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def reset_template(name: str) -> None:
    """Deletes the user's edited version, so the built-in template is used again."""
    (OVERRIDE_DIR / "templates" / f"{name}.md").unlink(missing_ok=True)


def meeting_types() -> dict[str, dict]:
    """{type: {label, keywords, guidance, fields}} from the type templates' front matter."""
    out = {}
    for name in template_names():
        if name.startswith("partials/"):
            continue
        meta, _ = frontmatter(read_template(name)[0])
        t = meta.get("type") or name
        out[t] = {"label": meta.get("label") or t.replace("_", " ").title(), "template": name,
                  "keywords": [k.strip().lower() for k in meta.get("keywords", "").split(",") if k.strip()],
                  "guidance": meta.get("guidance", ""),
                  "fields": [f.strip() for f in meta.get("fields", "").split(",") if f.strip()]}
    general = out.pop("general", {"label": "General meeting", "template": "general", "keywords": [], "guidance": "", "fields": []})
    return {"general": general, **out}


def skill() -> dict:
    """The meeting-notes skill from SKILL.md: name, description, what it's the default for, and its rules (the
    Markdown body).
    """
    meta, body = frontmatter((SKILL_DIR / "SKILL.md").read_text(encoding="utf-8"))
    return {"name": meta.get("name", "Professional Meeting Notes"), "description": meta.get("description", ""),
            "default_for": meta.get("default_for", ""), "body": body}


def schema() -> dict:
    """The JSON Schema the model's notes must match (skills/meeting-notes/schema.json)."""
    return json.loads((SKILL_DIR / "schema.json").read_text(encoding="utf-8"))


def writing_rules() -> str:
    """The 'Writing rules' section of SKILL.md (sent to the model with every request)."""
    body = skill()["body"]
    match = re.search(r"## Writing rules\n(.*?)(?=\n## |\Z)", body, re.S)
    return (match.group(1) if match else body).strip()


# ---------------------------------------------------------------------------
# Template engine (Mustache subset: {{x}}, {{a.b}}, {{.}}, {{#x}}…{{/x}}, {{^x}}…{{/x}}, {{> partial}})
# ---------------------------------------------------------------------------

_STANDALONE = re.compile(r"^[ \t]*(\{\{[#/^!>][^}]*\}\})[ \t]*\n", re.M)
_TAG = re.compile(r"\{\{([#^/>!]?)\s*([^}]*?)\s*\}\}")


def _lookup(stack: list, name: str):
    """Finds a template variable such as 'meeting.title' in the current context stack, innermost first ('.' means
    the current item inside a loop).
    """
    if name == ".":
        return stack[-1]
    first, *rest = name.split(".")
    for ctx in reversed(stack):
        if isinstance(ctx, dict) and first in ctx:
            value = ctx[first]
            for part in rest:
                if isinstance(value, dict):
                    value = value.get(part)
                elif isinstance(value, list) and part.isdigit():
                    value = value[int(part)] if int(part) < len(value) else None
                else:
                    value = None
            return value
    return None


def render_template(text: str, ctx: dict, partial: Callable[[str], str] | None = None, depth: int = 0) -> str:
    """Fills a template written in a small Mustache subset:

      {{name}}            a value
      {{#name}}…{{/name}} a section: repeated for each item of a list, shown once for a true value, hidden for an empty one
      {{^name}}…{{/name}} shown only when the value is empty
      {{> partial}}       another template file inserted here

    `depth` stops runaway partials that include each other.
    """
    if depth > 8:
        return ""
    text = frontmatter(text)[1]
    text = _STANDALONE.sub(r"\1", text)
    out, pos = [], 0
    stack = [ctx]

    def render(segment: str, stack: list) -> str:
        """Renders one piece of template text with the given context stack (called again for the inside of each
        section).
        """
        result, i = [], 0
        while True:
            m = _TAG.search(segment, i)
            if not m:
                result.append(segment[i:])
                break
            result.append(segment[i:m.start()])
            kind, name = m.group(1), m.group(2)
            if kind == "!":
                i = m.end()
            elif kind == ">":
                result.append(render_template(partial(name), stack[-1] if len(stack) == 1 else _merged(stack), partial, depth + 1) if partial else "")
                i = m.end()
            elif kind in "#^" and kind:
                close = _find_close(segment, name, m.end())
                inner = segment[m.end():close.start()] if close else ""
                value = _lookup(stack, name)
                if kind == "#":
                    if isinstance(value, list):
                        result.extend(render(inner, stack + [item]) for item in value)
                    elif value:
                        result.append(render(inner, stack + [value]))
                elif not value:
                    result.append(render(inner, stack))
                i = close.end() if close else len(segment)
            else:
                value = _lookup(stack, name)
                result.append("" if value is None or isinstance(value, (dict, list)) else str(value))
                i = m.end()
        return "".join(result)

    rendered = render(text, stack)
    return rendered if depth else re.sub(r"\n{3,}", "\n\n", rendered).strip() + "\n"


def _merged(stack: list) -> dict:
    """All context dicts of the stack merged into one (inner values win)."""
    merged: dict = {}
    for ctx in stack:
        if isinstance(ctx, dict):
            merged.update(ctx)
    return merged


def _find_close(text: str, name: str, start: int):
    """Finds the {{/name}} that closes a section, skipping nested sections of the same name. Returns a small
    match-like object with absolute positions, or None.
    """
    depth = 1
    for m in re.finditer(r"\{\{([#^/])\s*" + re.escape(name) + r"\s*\}\}", text[start:]):
        depth += -1 if m.group(1) == "/" else 1
        if depth == 0:
            class _M:  # minimal match-like object with absolute positions
                """Tiny stand-in for a regex match: only start() and end()."""
                def __init__(self, s, e):
                    """Stores the start and end positions."""
                    self._s, self._e = s, e

                def start(self):
                    """Where the closing tag starts."""
                    return self._s

                def end(self):
                    """Where the closing tag ends."""
                    return self._e
            return _M(start + m.start(), start + m.end())
    return None


# ---------------------------------------------------------------------------
# Transcript → prompt input
# ---------------------------------------------------------------------------

def fmt_ts(seconds: float) -> str:
    """A time position as 'hh:mm:ss'."""
    s = int(seconds or 0)
    return f"{s // 3600:02d}:{s % 3600 // 60:02d}:{s % 60:02d}"


def ts_seconds(ts: str) -> float | None:
    """Seconds for a time like '1:02:03' or '02:03'; None if it isn't a time."""
    m = re.fullmatch(r"(?:(\d{1,2}):)?(\d{1,2}):(\d{2})", (ts or "").strip())
    if not m:
        return None
    return int(m.group(1) or 0) * 3600 + int(m.group(2)) * 60 + int(m.group(3))


def speaker_display(transcript: dict, names: dict[str, str], labels: dict[str, dict]) -> dict[str, str]:
    """Display name per speaker id; low-confidence guesses get "(?)"."""
    out = {}
    for i, spk in enumerate(transcript.get("speakers") or []):
        name = names.get(spk) or f"Speaker {i + 1}"
        label = labels.get(spk) or {}
        if label.get("name") and label.get("source") != "user" and label.get("level") == "low":
            name = f"{label['name']} (?)"
        out[spk] = name
    return out


def transcript_lines(transcript: dict, display: dict[str, str]) -> list[dict]:
    """The transcript as numbered lines {ts, seconds, speaker, text}; text imported without times uses line
    numbers '#12' instead of times.
    """
    timed = transcript.get("timestamps") is not False
    lines = []
    for i, seg in enumerate(s for s in transcript.get("segments", []) if (s.get("text") or "").strip()):
        ts = fmt_ts(seg.get("start", 0)) if timed else f"#{i + 1}"
        lines.append({"ts": ts, "seconds": seg.get("start", 0) if timed else None, "speaker": display.get(seg["speaker"], seg["speaker"]),
                      "text": " ".join(seg["text"].split())})
    return lines


def lines_text(lines: list[dict]) -> str:
    """Lines as '[00:03:12] Priya: text', the format the model reads and cites."""
    return "\n".join(f"[{l['ts']}] {l['speaker']}: {l['text']}" for l in lines)


_RECORDING_NAME = re.compile(r"(\d{4})-(\d{2})-(\d{2})(?: at (\d{1,2})[.:](\d{2})(?:[.:](\d{2}))?\s*([AP]M)?)?", re.I)


def meeting_meta(transcript: dict, display: dict[str, str], labels: dict[str, dict]) -> dict:
    """Facts the app knows for sure (not left to the model): date, time, duration, attendees."""
    when, start = None, ""
    m = _RECORDING_NAME.search(transcript.get("source_name") or "")
    if m:
        try:
            when = datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            when = None
        if when and m.group(4):
            hour = int(m.group(4)) % 12 + (12 if (m.group(7) or "").upper() == "PM" else 0) if m.group(7) else int(m.group(4))
            start = f"{hour:02d}:{m.group(5)}"
    day = when.date() if when else relative_dates.meeting_date(transcript.get("created_at"))
    tz = datetime.now().astimezone().tzname() or ""
    duration = float(transcript.get("duration") or 0)
    attendees = []
    for spk in transcript.get("speakers") or []:
        if spk == "UNKNOWN":
            continue
        name = display.get(spk, spk)
        label = labels.get(spk) or {}
        attendees.append({"name": name, "role": label.get("role", ""), "uncertain": name.endswith("(?)")})
    return {"title": transcript.get("title") or "Meeting", "date": relative_dates.fmt(day), "date_iso": day.isoformat(),
            "weekday": day.strftime("%A"), "start_time": f"{start} {tz}".strip() if start else "",
            "duration": _fmt_duration(duration) if duration else "", "attendees": attendees, "timezone": tz}


def _fmt_duration(seconds: float) -> str:
    """A meeting length as '45 min' or '1 h 05 min'."""
    minutes = round(seconds / 60)
    return f"{minutes} min" if minutes < 60 else f"{minutes // 60} h {minutes % 60:02d} min"


def detect_type(text: str, title: str = "", types: dict | None = None) -> dict:
    """{'type', 'label', 'confidence', 'scores'}: keyword evidence per type; title counts triple."""
    types = types or meeting_types()
    body, head = text.lower(), (title or "").lower()
    scores = {}
    for t, info in types.items():
        score = 0.0
        for kw in info["keywords"]:
            pattern = r"(?<![a-z])" + re.escape(kw) + (r"(?![a-z])" if not kw.endswith(("escalat",)) else "")
            score += min(len(re.findall(pattern, body)), 6) + 3 * len(re.findall(pattern, head))
        scores[t] = score
    best = max(scores, key=lambda t: scores[t]) if scores else "general"
    ordered = sorted(scores.values(), reverse=True) + [0, 0]
    if scores.get(best, 0) < 3:
        best = "general"
    margin = ordered[0] - ordered[1]
    confidence = "high" if best != "general" and margin >= 4 else "medium" if best != "general" and margin >= 2 else "low"
    return {"type": best, "label": types[best]["label"], "confidence": confidence,
            "scores": {k: v for k, v in sorted(scores.items(), key=lambda kv: -kv[1]) if v}}


# ---------------------------------------------------------------------------
# Prompt
# ---------------------------------------------------------------------------

def _example(node: dict, root: dict, name: str = "") -> object:
    """An example value built from the JSON Schema (shown to the model so it knows the exact shape to answer
    with).
    """
    if "$ref" in node:
        node = root["$defs"][node["$ref"].split("/")[-1]]
    if "enum" in node:
        return "|".join(str(e) for e in node["enum"] if e) or ""
    kind = node.get("type")
    if kind == "object":
        return {k: _example(v, root, k) for k, v in node.get("properties", {}).items()}
    if kind == "array":
        return [_example(node.get("items", {}), root, name)]
    if kind == "boolean":
        return False
    if "pattern" in node and "\\d" in node["pattern"]:
        return "00:01:05"
    return node.get("description", "") or "…"


def output_shape(meeting_type: str, types: dict) -> dict:
    """An example JSON object (the schema's shape) with only this type's detail fields."""
    s = schema()
    shape = _example(s, s)
    shape.pop("meeting")
    fields = types.get(meeting_type, {}).get("fields", [])
    shape["details"] = {k: v for k, v in shape["details"].items() if k in fields}
    if not fields:
        shape.pop("details")
    shape["meeting"] = {"project": "", "absentees": []}  # last: the app fills in the rest
    return shape


DETAIL_HINT = {
    "brief": "Brief: fill tldr, decisions and action_items; other lists may be empty.",
    "standard": "Standard: all sections; 2-4 key points per topic; up to 3 quotes.",
    "detailed": "Detailed: all sections; every substantive point per topic with the speaker; up to 8 quotes.",
}


# ---------------------------------------------------------------------------
# Memory: full names and roles for people already in the meeting, the project, "I"/"me"
# ---------------------------------------------------------------------------

_SELF = {"i", "me", "myself", "me (the user)"}


def _person_for(name: str, people: list[dict]) -> dict | None:
    """The one remembered person a speaker or owner name refers to (full name, alias, or a
    first name that only one person has)."""
    key = name.replace("(?)", "").strip().lower()
    if not key:
        return None
    exact = [p for p in people if p["name"].lower() == key or key in [a.lower() for a in p.get("aliases") or []]]
    if exact:
        return exact[0]
    first = [p for p in people if p["name"].split()[0].lower() == key and " " in p["name"]]
    return first[0] if len(first) == 1 else None


def apply_memory(meta: dict, memory: dict | None) -> dict:
    """Enrich attendees (already identified in the transcript) with full names and roles from
    memory. Never adds anyone. memory = {"people": [{name, aliases, role, team}], "projects": [names],
    "me": "the user's name"}."""
    memory = memory or {}
    people = memory.get("people") or []
    meta = dict(meta, memory=memory)
    attendees = []
    for a in meta["attendees"]:
        a = dict(a)
        person = None if a.get("uncertain") else _person_for(a["name"], people)
        if person:
            if person["name"].lower() != a["name"].lower():
                a["memory_name"] = person["name"]
                a["name"] = person["name"]
            if not a.get("role") and person.get("role"):
                a["role"] = person["role"]
        attendees.append(a)
    meta["attendees"] = attendees
    return meta


def memory_prompt_lines(meta: dict) -> list[str]:
    """Prompt lines from the user's memory about people already in this meeting (full names and roles), the user
    themself and known projects. Memory may only complete names, never add people or actions.
    """
    memory = meta.get("memory") or {}
    names = {a["name"].replace("(?)", "").strip().lower() for a in meta["attendees"]}
    lines = []
    people = [p for p in memory.get("people") or [] if p["name"].lower() in names]
    if people:
        lines.append("From the user's memory, about attendees already listed (use only for full names and roles; "
                     "never add a person, decision or action that isn't in the transcript): "
                     + "; ".join(p["name"] + (f" — {p['role']}" if p.get("role") else "")
                                 + (f", {p['team']} team" if p.get("team") else "") for p in people[:15]))
    if memory.get("projects"):
        lines.append("Projects the user works on (name one as the meeting's project only if it is discussed): "
                     + ", ".join(memory["projects"][:12]))
    if memory.get("me"):
        lines.append(f"The user is {memory['me']}; \"I\"/\"me\" in an owner field means them.")
    return lines


def resolve_owner(owner: str, meta: dict) -> str:
    """"Owen" → "Owen Hart" (an attendee, via memory); "me"/"I" → the user, when known."""
    memory = meta.get("memory") or {}
    plain = owner.replace("(?)", "").strip()
    if plain.lower() in _SELF and memory.get("me"):
        return memory["me"]
    attendees = [a["name"].replace("(?)", "").strip() for a in meta["attendees"]]
    full = [a for a in attendees if " " in a and a.split()[0].lower() == plain.lower()]
    if " " not in plain and len(full) == 1:
        return owner.replace(plain, full[0])
    person = _person_for(plain, memory.get("people") or [])
    if person and person["name"] in attendees:
        return owner.replace(plain, person["name"])
    return owner


def memory_project(text: str, projects: list[str]) -> str:
    """The remembered project the meeting talks about most (must be said in the meeting)."""
    counts = {p: len(re.findall(rf"\b{re.escape(p)}\b", text, re.I)) for p in projects if len(p) >= 3}
    best = max(counts, key=counts.get, default="")
    return best if best and counts[best] >= 2 else ""


def build_messages(text: str, meta: dict, meeting_type: str, types: dict, detail: str, sensitive_detail: bool,
                   part: tuple[int, int] | None = None) -> list[dict]:
    """The request for the notes: rules, meeting facts (title, date, attendees), the meeting type's guidance, the
    detail level, the expected JSON shape and the transcript (or one part of it).
    """
    info = types.get(meeting_type, types["general"])
    attendees = ", ".join(a["name"] + (f" ({a['role']})" if a.get("role") else "") for a in meta["attendees"]) or "not identified"
    lines = [
        f"Meeting: {meta['title']}",
        f"Meeting date: {meta['weekday']} {meta['date']}" + (f", started {meta['start_time']}" if meta["start_time"] else "")
        + (f", time zone {meta['timezone']}" if meta.get("timezone") else ""),
        f"Attendees (identified speakers; (?) = low-confidence guess): {attendees}",
        f"Meeting type: {info['label']}. {info['guidance']}",
        f"Detail level: {DETAIL_HINT[detail]}",
        "Sensitive topics (HR, salary, health, personal): " + ("may be described in detail." if sensitive_detail
                                                               else "summarise at a high level only."),
        *memory_prompt_lines(meta),
    ]
    if part:
        lines.append(f"This is part {part[0]} of {part[1]} of a long meeting. Extract only what is in this part; "
                     "the tldr is for this part.")
    lines += [
        "",
        "Reply with ONE JSON object shaped like this example (keep the keys; use [] or \"\" when there is nothing; "
        "values like \"High|Medium|Low|\" list the allowed choices):",
        json.dumps(output_shape(meeting_type, types), ensure_ascii=False),
        "",
        "Transcript (each line: [timestamp] speaker: words). Use these timestamps exactly:",
        text,
    ]
    system = ("You write professional meeting notes from transcripts and reply with JSON only — no Markdown, "
              "no commentary.\n\nRules:\n" + writing_rules())
    return [{"role": "system", "content": system}, {"role": "user", "content": "\n".join(lines)}]


# ---------------------------------------------------------------------------
# JSON: parse, validate, retry, coerce
# ---------------------------------------------------------------------------

def parse_json(reply: str) -> tuple[dict | None, str]:
    """Gets the JSON object out of the model's reply (it may wrap it in ```json fences or add text around it).
    Returns (data, '') or (None, what went wrong).
    """
    text = re.sub(r"<think>.*?</think>", "", reply or "", flags=re.S).strip()
    fenced = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.S)
    if fenced:
        text = fenced.group(1)
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        return None, "The reply contained no JSON object."
    decoder = json.JSONDecoder()
    try:
        data, _ = decoder.raw_decode(text[start:])  # the first object; anything after it is ignored
    except json.JSONDecodeError as exc:
        data = repair_json(text[start:end + 1])
        if data is None:
            return None, f"The JSON is malformed: {exc.msg} at line {exc.lineno}, column {exc.colno}."
    if not isinstance(data, dict):
        return None, "The reply is JSON but not an object."
    return data, ""


def repair_json(text: str, attempts: int = 30) -> dict | None:
    """Fix the slips small models make: trailing commas, and quotes inside a string that weren't
    escaped ("He said "yes"" → the parser stops at yes; that quote gets escaped and it tries again)."""
    text = re.sub(r",\s*([}\]])", r"\1", text)
    decoder = json.JSONDecoder()
    closed = False
    for _ in range(attempts):
        try:
            data, _ = decoder.raw_decode(text)
            if not isinstance(data, dict):
                return None
            if closed:
                data["_repair"] = "incomplete"
            return data
        except json.JSONDecodeError as exc:
            if exc.pos >= len(text.rstrip()) - 1:  # complete text, but containers left open: close them
                closers = _open_brackets(text)
                if closers and not closers.startswith('"'):
                    text = text.rstrip() + closers
                    closed = True
                    continue
            if exc.msg == "Expecting ':' delimiter" and text[:exc.pos].rstrip().endswith('"'):
                text = text[:exc.pos] + ":" + text[exc.pos:]  # "key" "value" → "key": "value"
                continue
            if exc.msg != "Expecting ',' delimiter":
                return None
            quote = text.rfind('"', 0, exc.pos)
            if quote <= 0 or text[quote - 1] == "\\":
                return None
            text = text[:quote] + "\\" + text[quote:]
    return None


def _open_brackets(text: str) -> str:
    """The closing brackets `text` is missing (string-aware); '"' first if it ends inside a string."""
    stack, in_string, escape = [], False, False
    for ch in text:
        if in_string:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_string = False
        elif ch == '"':
            in_string = True
        elif ch in "{[":
            stack.append("}" if ch == "{" else "]")
        elif ch in "}]" and stack:
            stack.pop()
    return ('"' if in_string else "") + "".join(reversed(stack))


TOP_LEVEL = ("tldr", "topics", "decisions", "action_items", "risks", "open_questions", "next_steps", "next_meeting",
             "quotes", "details")


def _hoist(data: dict) -> dict:
    """Sections that ended up inside "meeting" or "details" (an object left open) go back to the top."""
    for holder in ("meeting", "details"):
        inner = data.get(holder)
        if isinstance(inner, dict):
            for key in TOP_LEVEL:
                if key != holder and key in inner and key not in data:
                    data[key] = inner.pop(key)
    return data


def salvage_json(reply: str) -> dict | None:
    """Everything up to the first syntax error: cut at the last complete value, close the brackets."""
    text = re.sub(r"<think>.*?</think>", "", reply or "", flags=re.S)
    start = text.find("{")
    if start < 0:
        return None
    text = text[start:]
    stack, safe, in_string, escape = [], [], False, False
    for i, ch in enumerate(text):
        if in_string:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch in "{[":
            stack.append("}" if ch == "{" else "]")
        elif ch in "}]":
            if not stack:
                break
            stack.pop()
            safe.append((i + 1, list(stack)))
            if not stack:
                break
        elif ch == ",":
            safe.append((i, list(stack)))
    else:  # ran out of text (a reply cut short): try closing it where it stopped
        if not in_string and stack:
            safe.append((len(text), list(stack)))
    for cut, open_brackets in reversed(safe[-200:]):
        candidate = text[:cut] + "".join(reversed(open_brackets))
        try:
            data = json.loads(re.sub(r",\s*([}\]])", r"\1", candidate))
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict):
            return data
    return None


def validate(data: dict, schema_doc: dict | None = None) -> list[str]:
    """Checks the notes against the schema; returns up to 12 readable errors ([] when valid)."""
    validator = jsonschema.Draft202012Validator(schema_doc or schema())
    errors = []
    for err in sorted(validator.iter_errors(data), key=lambda e: list(e.absolute_path)):
        where = "/".join(str(p) for p in err.absolute_path) or "(top level)"
        errors.append(f"{where}: {err.message[:160]}")
    return errors[:12]


def coerce(data: dict, schema_doc: dict | None = None) -> dict:
    """Best partial result: keep what matches the schema, drop list items that don't."""
    s = schema_doc or schema()
    out = copy.deepcopy(data) if isinstance(data, dict) else {}
    out.setdefault("meeting", {})
    if not isinstance(out["meeting"], dict):
        out["meeting"] = {}
    out["meeting"].setdefault("title", "Meeting")
    for key in ("tldr", "topics", "decisions", "action_items", "risks", "open_questions", "next_steps", "quotes"):
        value = out.get(key)
        if not isinstance(value, list):
            out[key] = []
            continue
        # A missing or odd timestamp doesn't cost the item: post-processing finds it in the transcript.
        item_schema = json.loads(json.dumps({"$defs": s["$defs"], **s["properties"][key]["items"]})
                                 .replace('"#/$defs/timestamp"', '"#/$defs/timestamp_or_empty"'))
        validator = jsonschema.Draft202012Validator(item_schema)
        for item in value:
            if isinstance(item, dict) and "timestamp" in item and list(jsonschema.Draft202012Validator(
                    {"$defs": s["$defs"], "$ref": "#/$defs/timestamp_or_empty"}).iter_errors(item["timestamp"])):
                item["timestamp"] = ""
        out[key] = [item for item in value if not list(validator.iter_errors(item))]
    out["tldr"] = out["tldr"][:5]
    for key in ("next_meeting", "details"):
        if not isinstance(out.get(key), dict):
            out[key] = {}
    for key, node in s["properties"]["details"]["properties"].items():
        if key in out["details"] and list(jsonschema.Draft202012Validator({"$defs": s["$defs"], **node}).iter_errors(out["details"][key])):
            out["details"].pop(key)
    return out


MAIN_FIELD = {"decisions": "decision", "action_items": "action", "risks": "item", "quotes": "quote", "topics": "title"}


def _fix_timestamps(data: dict) -> dict:
    """Common, harmless slips before validation: "1:05" → "00:01:05", "[00:01:05]" → "00:01:05", null → "",
    and empty rows (a risk with no text, a blank summary bullet) are dropped rather than failing the notes."""
    _hoist(data)
    placeholder = {"", "n/a", "na", "none", "unknown", "not stated", "not mentioned", "-", "—", "tbd", "unassigned",
                   "none.", "n/a.", "nothing"}
    for key in ("tldr", "open_questions", "next_steps"):
        if isinstance(data.get(key), list):
            data[key] = [x for x in data[key] if not (isinstance(x, str) and x.strip().lower() in placeholder)]

    def empty_row(row) -> bool:
        """True for a table row with only placeholder values ('', 'n/a', '-')."""
        return isinstance(row, dict) and all(str(v or "").strip().lower() in placeholder for v in row.values()
                                             if not isinstance(v, (list, dict)))
    if isinstance(data.get("risks"), list):
        data["risks"] = [r for r in data["risks"] if not (isinstance(r, dict) and
                         str(r.get("item") or "").strip().lower() in placeholder)]
    if isinstance(data.get("details"), dict):
        for key, value in list(data["details"].items()):
            if isinstance(value, list):
                data["details"][key] = [r for r in value if not empty_row(r)]
    if not isinstance(data.get("meeting"), dict):
        data["meeting"] = {}
    data["meeting"].setdefault("title", "Meeting")  # the app sets the header itself
    for key, field in MAIN_FIELD.items():
        if isinstance(data.get(key), list):
            data[key] = [x for x in data[key] if not (isinstance(x, dict) and not str(x.get(field) or "").strip())]
    for key in ("tldr", "open_questions", "next_steps"):
        if isinstance(data.get(key), list):
            data[key] = [x for x in data[key] if not (isinstance(x, str) and not x.strip())]
    def fix(value):
        """Normalises one timestamp to 'hh:mm:ss' (numbers, '3:12' or '[00:03:12]')."""
        if value is None:
            return ""
        if isinstance(value, (int, float)):
            return fmt_ts(value)
        value = str(value).strip().strip("[]()")
        m = re.fullmatch(r"(?:(\d{1,2}):)?(\d{1,2}):(\d{2})(?:\.\d+)?", value)
        if m:
            return f"{int(m.group(1) or 0):02d}:{int(m.group(2)):02d}:{m.group(3)}"
        return value
    for key in ("topics",):
        for topic in data.get(key) or []:
            for point in (topic.get("points") or []) if isinstance(topic, dict) else []:
                if isinstance(point, dict) and "timestamp" in point:
                    point["timestamp"] = fix(point["timestamp"])
    for key in ("decisions", "action_items", "risks", "quotes"):
        for item in data.get(key) or []:
            if isinstance(item, dict):
                if "timestamp" in item:
                    item["timestamp"] = fix(item["timestamp"])
                for field in list(item):
                    if item[field] is None:
                        item[field] = ""
    gng = (data.get("details") or {}).get("go_no_go") if isinstance(data.get("details"), dict) else None
    if isinstance(gng, dict) and "timestamp" in gng:
        gng["timestamp"] = fix(gng["timestamp"])
    return data


MAX_REPLY_CHARS = 40_000  # a runaway reply (small models sometimes loop) is cut off here


class JsonEnd:
    """Watches streamed text and reports when the first JSON object is complete."""

    def __init__(self):
        """Starts outside any JSON object and string."""
        self.depth, self.started, self.in_string, self.escape = 0, False, False, False

    def feed(self, text: str) -> int:
        """Index in `text` just after the object closes, or -1 while it's still open."""
        for i, ch in enumerate(text):
            if self.in_string:
                if self.escape:
                    self.escape = False
                elif ch == "\\":
                    self.escape = True
                elif ch == '"':
                    self.in_string = False
            elif ch == '"' and self.started:
                self.in_string = True
            elif ch == "{":
                self.depth += 1
                self.started = True
            elif ch == "}" and self.started:
                self.depth -= 1
                if self.depth == 0:
                    return i + 1
        return -1


def run_model(messages: list[dict], stream: Stream | None, complete: Complete, emit: Emit, label: str) -> str:
    """The model's reply. When streaming, reading stops as soon as the JSON object is complete
    (closing the stream also stops the generation), or after MAX_REPLY_CHARS."""
    if stream is None:
        return complete(messages)
    chunks, shown, size, end = [], 0, 0, JsonEnd()
    gen = stream(messages)
    try:
        for chunk in gen:
            cut = end.feed(chunk)
            chunks.append(chunk if cut < 0 else chunk[:cut])
            size += len(chunk)
            if cut >= 0 or size >= MAX_REPLY_CHARS:
                break
            if size - shown >= 400:
                shown = size
                emit({"type": "status", "text": f"{label} · {size:,} characters"})
    finally:
        close = getattr(gen, "close", None)
        if close:
            close()
    return "".join(chunks)


def extract(messages: list[dict], stream: Stream | None, complete: Complete, emit: Emit, label: str,
            schema_doc: dict) -> dict:
    """One extraction with at most one retry. Returns {data, valid, errors, attempts}."""
    reply = run_model(messages, stream, complete, emit, label)
    data, problem = parse_json(reply)
    errors = [problem] if problem else validate(_fix_timestamps(data), schema_doc)
    if not errors:
        return {"data": data, "valid": True, "errors": [], "attempts": 1}
    emit({"type": "status", "text": "The notes didn't match the format — retrying once with the problems listed"})
    retry = messages + [
        {"role": "assistant", "content": reply[:12000]},
        {"role": "user", "content": "Your JSON did not match the required format. Problems:\n- " + "\n- ".join(errors)
         + "\n\nReply with the corrected, complete JSON object only."},
    ]
    reply2 = run_model(retry, stream, complete, emit, f"{label} (retry)")
    data2, problem2 = parse_json(reply2)
    errors2 = [problem2] if problem2 else validate(_fix_timestamps(data2), schema_doc)
    if not errors2:
        return {"data": data2, "valid": True, "errors": [], "attempts": 2}
    best = data2 if data2 is not None and (data is None or len(errors2) <= len(errors)) else data
    if best is None:  # both replies broken: keep what came before the syntax error
        best = salvage_json(reply2) or salvage_json(reply)
        if best is not None:
            _fix_timestamps(best)
    return {"data": coerce(best or {}, schema_doc), "valid": False, "errors": errors2 or errors, "attempts": 2}


# ---------------------------------------------------------------------------
# Merge (long meetings)
# ---------------------------------------------------------------------------

def content_words(text: str) -> set[str]:
    """The meaningful words of a text with simple endings removed ('deploying' -> 'deploy'), for comparing items.
    """
    out = set()
    for w in re.findall(r"[a-z0-9]+", (text or "").lower()):
        if len(w) < 3 or w in STOP:
            continue
        for suffix in ("ing", "ed", "es", "s"):
            if w.endswith(suffix) and len(w) - len(suffix) >= 4:
                w = w[: -len(suffix)]
                break
        out.add(w)
    return out


def similar(a: str, b: str) -> float:
    """How alike two items are by their content words, 0 to 1."""
    wa, wb = content_words(a), content_words(b)
    return len(wa & wb) / len(wa | wb) if wa and wb else 0.0


def _same_owner(a: dict, b: dict) -> bool:
    """True when two action items have the same owner (or no known owner)."""
    owners = {str(x.get("owner") or "").replace("(?)", "").strip().lower() for x in (a, b)} - {"", "unassigned"}
    return len(owners) <= 1


def _dedupe(items: list[dict], key: str, threshold: float = 0.6) -> list[dict]:
    """Merge near-identical items (an action for two different owners stays two actions)."""
    out: list[dict] = []
    for item in items:
        twin = next((o for o in out if similar(o.get(key, ""), item.get(key, "")) >= threshold and _same_owner(o, item)), None)
        if twin is None:
            out.append(dict(item))
            continue
        for field, value in item.items():  # fill gaps from the duplicate
            if value and (not twin.get(field) or twin.get(field) in ("Unassigned", "TBD")):
                twin[field] = value
        if item.get("timestamp") and twin.get("timestamp") and (ts_seconds(item["timestamp"]) or 0) < (ts_seconds(twin["timestamp"]) or 0):
            twin["timestamp"] = item["timestamp"]
    return out


def _dedupe_strings(items: list[str], threshold: float = 0.7) -> list[str]:
    """Drops items that repeat an earlier one (70%+ similar), keeping the first."""
    out: list[str] = []
    for item in items:
        if item and not any(similar(o, item) >= threshold for o in out):
            out.append(item)
    return out


def merge_notes(parts: list[dict]) -> dict:
    """Combine per-part notes: lists concatenated in order, duplicates merged."""
    if not parts:
        return {}
    merged = copy.deepcopy(parts[0])
    for part in parts[1:]:
        for key in ("tldr", "open_questions", "next_steps"):
            merged[key] = (merged.get(key) or []) + (part.get(key) or [])
        for key in ("decisions", "action_items", "risks", "quotes"):
            merged[key] = (merged.get(key) or []) + (part.get(key) or [])
        topics = merged.setdefault("topics", [])
        for topic in part.get("topics") or []:
            twin = next((t for t in topics if similar(t["title"], topic["title"]) >= 0.5), None)
            if twin:
                twin["points"] = twin.get("points", []) + topic.get("points", [])
            else:
                topics.append(copy.deepcopy(topic))
        if (part.get("next_meeting") or {}).get("date"):
            merged["next_meeting"] = part["next_meeting"]
        details = merged.setdefault("details", {})
        for key, value in (part.get("details") or {}).items():
            if isinstance(value, list):
                details[key] = (details.get(key) or []) + value
            elif isinstance(value, dict):
                details[key] = {**value, **{k: v for k, v in (details.get(key) or {}).items() if v}}
            elif value and not details.get(key):
                details[key] = value
            elif value and details.get(key) and value not in details[key]:
                details[key] = f"{details[key]} {value}"
        meeting = merged.setdefault("meeting", {})
        for key, value in (part.get("meeting") or {}).items():
            if value and not meeting.get(key):
                meeting[key] = value
    merged["decisions"] = _dedupe(merged.get("decisions") or [], "decision")
    merged["action_items"] = _dedupe(merged.get("action_items") or [], "action")
    merged["risks"] = _dedupe(merged.get("risks") or [], "item")
    merged["tldr"] = _dedupe_strings(merged.get("tldr") or [])
    merged["open_questions"] = _dedupe_strings(merged.get("open_questions") or [])
    merged["next_steps"] = _dedupe_strings(merged.get("next_steps") or [])
    for key in ("standup_updates",):
        if key in merged.get("details", {}):
            merged["details"][key] = _merge_people_rows(merged["details"][key])
    return merged


def _merge_people_rows(rows: list[dict]) -> list[dict]:
    """Merges rows about the same person from different parts of a long meeting."""
    out: dict[str, dict] = {}
    for row in rows:
        person = row.get("person", "")
        if person in out:
            for k, v in row.items():
                if v and v not in (out[person].get(k) or ""):
                    out[person][k] = f"{out[person][k]}; {v}" if out[person].get(k) else v
        else:
            out[person] = dict(row)
    return list(out.values())


# ---------------------------------------------------------------------------
# Post-processing
# ---------------------------------------------------------------------------

_NO_OWNER = {"", "none", "n/a", "na", "unknown", "unassigned", "tbd", "tba", "?", "not stated", "not specified", "nobody"}
_NO_DATE = {"", "none", "n/a", "na", "unknown", "tbd", "tba", "?", "not stated", "not specified", "no date"}


def trace(text: str, lines: list[dict], hint: str = "") -> tuple[int, float]:
    """Index of the transcript line `text` most likely comes from, and the share of its words found there."""
    words = content_words(text)
    if not words or not lines:
        return -1, 0.0
    hint_s = ts_seconds(hint) if hint and not hint.startswith("#") else None
    best, best_score = -1, 0.0
    for i, line in enumerate(lines):
        window = " ".join(lines[j]["text"] for j in range(max(0, i - 1), min(len(lines), i + 2)))
        score = len(words & content_words(window)) / len(words)
        if hint_s is not None and line["seconds"] is not None and abs(line["seconds"] - hint_s) <= 60:
            score += 0.05  # prefer the place the model pointed at
        own = len(words & content_words(line["text"])) / len(words)
        score += own * 0.1
        if score > best_score:
            best, best_score = i, score
    # report the plain overlap (without the bonuses)
    window = " ".join(lines[j]["text"] for j in range(max(0, best - 1), min(len(lines), best + 2)))
    return best, len(words & content_words(window)) / len(words)


_DUE_IN_TEXT = re.compile(
    r"\s*,?\s+(?:by|on|for|before|until|due|no later than)\s+((?:the\s+)?(?:end of (?:the |this |next )?(?:day|week|month)|"
    r"(?:this |next |coming )?(?:mon|tues|wednes|thurs|fri|satur|sun)day|tomorrow|today|eod|next week|"
    r"\d{1,2}(?:st|nd|rd|th)? [a-z]{3,9}|[a-z]{3,9} \d{1,2}(?:st|nd|rd|th)?|\d{1,2}(?::\d{2})?\s*(?:am|pm)))\.?$", re.I)


_DATE_PHRASE = re.compile(
    r"\b(?:(?:by|on|for|before|until|due)\s+)?((?:the\s+)?end of (?:the |this |next )?(?:day|week|month)|"
    r"(?:this |next |coming )?(?:mon|tues|wednes|thurs|fri|satur|sun)day|tomorrow|today|tonight|next week|"
    r"\d{1,2}(?:st|nd|rd|th)? (?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*)\b", re.I)


def due_from_line(line: str, day) -> str:
    """The due date said on the transcript line an action came from, if exactly one date is mentioned."""
    found = {relative_dates.resolve(m.group(1), day) for m in _DATE_PHRASE.finditer(line or "")}
    found = {f for f in found if re.match(r"(?:w/c )?\d{1,2} [A-Z][a-z]{2} \d{4}", f)}
    return found.pop() if len(found) == 1 else ""


def due_from_text(action: str, day) -> tuple[str, str]:
    """("Schedule the demo", "24 Nov 2026") from "Schedule the demo for Tuesday"; unchanged when no date is found."""
    m = _DUE_IN_TEXT.search(action)
    if not m:
        return action, ""
    resolved = relative_dates.resolve(m.group(1), day)
    if resolved == m.group(1).strip():
        return action, ""
    return action[: m.start()].rstrip(" ,"), resolved


def _split_owners(owner: str) -> list[str]:
    """Splits 'Priya and Owen' / 'Priya, Owen' into separate owners (only for short names)."""
    parts = [p.strip() for p in re.split(r"\s+(?:and|&)\s+|\s*/\s*|,\s*", owner or "") if p.strip()]
    return parts if len(parts) > 1 and all(len(p.split()) <= 3 for p in parts) else [owner]


def _mark_uncertain(name: str, uncertain: set[str]) -> str:
    """Adds '(?)' to a name that is a low-confidence speaker guess."""
    base = (name or "").replace("(?)", "").strip()
    return f"{base} (?)" if base in uncertain and "(?)" not in (name or "") else name


def postprocess(data: dict, lines: list[dict], meta: dict, meeting_type: str) -> tuple[dict, list[str], list[str]]:
    """Normalise the model's notes. Returns (notes, warnings, needs_follow_up)."""
    notes = copy.deepcopy(data)
    warnings: list[str] = []
    follow_up: list[str] = []
    day = datetime.fromisoformat(meta["date_iso"]).date()
    uncertain = {a["name"].replace("(?)", "").strip() for a in meta["attendees"] if a.get("uncertain")}
    full_text = " ".join(l["text"] for l in lines).lower()

    # Header: facts the app knows replace the model's guesses.
    model_meeting = notes.get("meeting") or {}
    project = str(model_meeting.get("project") or "").strip()
    if project and project.lower() not in full_text and not content_words(project) <= content_words(full_text):
        project = ""  # not said in the meeting
    if not project:
        project = memory_project(full_text, (meta.get("memory") or {}).get("projects") or [])
    absentees = [a for a in (model_meeting.get("absentees") or []) if isinstance(a, str) and a.split()[0].lower() in full_text]
    notes["meeting"] = {**{k: meta[k] for k in ("title", "date", "start_time", "duration")}, "type": meeting_type,
                        "project": project, "attendees": meta["attendees"], "absentees": absentees}

    def place(item: dict, text_key: str, kind: str) -> bool:
        """Finds the transcript line an item comes from and sets its timestamp. Returns False (and warns) when
        the item can't be traced, so it's dropped.
        """
        idx, score = trace(item.get(text_key, ""), lines, item.get("timestamp", ""))
        if idx < 0 or score < TRACE_MIN:
            warnings.append(f"Removed {kind} that couldn't be traced to the transcript: “{item.get(text_key, '')[:90]}”")
            return False
        model_ts = item.get("timestamp", "")
        model_s = ts_seconds(model_ts) if model_ts and not model_ts.startswith("#") else None
        if not model_ts or (lines[idx]["seconds"] is not None and (model_s is None or abs(model_s - lines[idx]["seconds"]) > 90)) \
                or (model_ts.startswith("#") and model_ts != lines[idx]["ts"]):
            item["timestamp"] = lines[idx]["ts"]
        return True

    decisions = []
    for d in notes.get("decisions") or []:
        d = {"decision": d.get("decision", "").strip(), "made_by": _mark_uncertain(d.get("made_by", "").strip(), uncertain),
             "rationale": d.get("rationale", "").strip(), "timestamp": d.get("timestamp", "")}
        if d["decision"] and place(d, "decision", "a decision"):
            decisions.append(d)
    notes["decisions"] = _dedupe(decisions, "decision")

    actions = []
    for a in notes.get("action_items") or []:
        action = a.get("action", "").strip().rstrip(".")
        if not action:
            continue
        action = action[0].upper() + action[1:]
        for owner in _split_owners(str(a.get("owner") or "").strip()):
            owner = "Unassigned" if owner.lower().strip(" .") in _NO_OWNER else \
                _mark_uncertain(resolve_owner(owner, meta), uncertain)
            due = str(a.get("due_date") or "").strip()
            if re.fullmatch(r"\d{1,2}:\d{2}:\d{2}", due):
                due = ""  # a transcript timestamp in the wrong field
            text = action
            if due.lower().strip(" .") in _NO_DATE:
                text, found = due_from_text(action, day)  # "… by the end of next week" in the action itself
                due = found or "TBD"
            else:
                due = relative_dates.resolve(due, day)
            priority = a.get("priority") if a.get("priority") in ("High", "Medium", "Low") else ""
            item = {"action": text, "owner": owner, "due_date": due, "priority": priority,
                    "timestamp": a.get("timestamp", "")}
            if place(item, "action", "an action item"):
                if item["due_date"] == "TBD":
                    line = next((l for l in lines if l["ts"] == item["timestamp"]), None)
                    item["due_date"] = (due_from_line(line["text"], day) if line else "") or "TBD"
                actions.append(item)
    notes["action_items"] = _dedupe(actions, "action", 0.75) if actions else []
    for i, a in enumerate(notes["action_items"], start=1):
        missing = [w for w, bad in (("owner", a["owner"] == "Unassigned"), ("due date", a["due_date"] == "TBD")) if bad]
        if missing:
            follow_up.append(f"#{i} “{a['action'][:60]}” has no {' or '.join(missing)}")

    for r in notes.get("risks") or []:
        r["owner"] = _mark_uncertain(r.get("owner", ""), uncertain) if r.get("owner") else ""
    for q in notes.get("quotes") or []:
        if q.get("quote") and q["quote"].lower().strip(" .“”\"") not in full_text:
            idx, score = trace(q["quote"], lines, q.get("timestamp", ""))
            if score < 0.6:
                q["quote"] = ""  # not verbatim; drop rather than misquote
            elif idx >= 0:
                q["timestamp"] = lines[idx]["ts"]
    notes["quotes"] = [q for q in notes.get("quotes") or [] if q.get("quote")]
    nm = notes.get("next_meeting") or {}
    for key in ("date", "time"):
        if str(nm.get(key) or "").strip().lower().strip(" .") in _NO_DATE:
            nm[key] = ""  # "TBD" placeholders: no next meeting was mentioned
    if nm.get("date"):
        nm["date"] = relative_dates.resolve(nm["date"], day)
    for row in (notes.get("details") or {}).get("our_commitments") or []:
        if row.get("due_date"):
            row["due_date"] = relative_dates.resolve(row["due_date"], day)
    for row in (notes.get("details") or {}).get("milestones") or []:
        if row.get("date"):
            row["date"] = relative_dates.resolve(row["date"], day)
    notes["tldr"] = [t.strip() for t in notes.get("tldr") or [] if t and t.strip()][:5]
    return notes, warnings, follow_up


# ---------------------------------------------------------------------------
# Generate
# ---------------------------------------------------------------------------

def generate(transcript: dict, names: dict[str, str], labels: dict[str, dict], complete: Complete,
             stream: Stream | None = None, emit: Emit = lambda _e: None, *, meeting_type: str = "auto",
             detail: str = "standard", sensitive_detail: bool = False, context_length: int = 32768,
             cache: dict | None = None, speeds: dict | None = None, memory: dict | None = None) -> dict:
    """`memory` (optional): {"people", "projects", "me"} from the memory store, used only to give
    full names/roles for people already in the meeting, link the project and resolve "I"/"me"."""
    types = meeting_types()
    schema_doc = schema()
    display = speaker_display(transcript, names, labels)
    lines = transcript_lines(transcript, display)
    if not lines:
        raise ValueError("This transcript is empty.")
    meta = apply_memory(meeting_meta(transcript, display, labels), memory)
    text = lines_text(lines)
    detected = detect_type(text, meta["title"], types)
    chosen = meeting_type if meeting_type in types else detected["type"]
    emit({"type": "status", "text": f"Meeting type: {types[chosen]['label']}"
          + (" (detected)" if meeting_type not in types else " (your choice)")})

    overhead = long_context.estimate_tokens(json.dumps(build_messages("", meta, chosen, types, detail, sensitive_detail)))
    budget = long_context.content_budget(context_length) - overhead - 2500  # room for the JSON answer
    parts_text = [text] if long_context.estimate_tokens(text) <= budget else long_context.split_lines(text, max(2000, min(budget, long_context.MAP_PART_TOKENS)))
    results, valid, errors, attempts, incomplete = [], True, [], 0, False
    if len(parts_text) > 1:
        emit({"type": "notice", "text": f"Long meeting: extracting notes from {len(parts_text)} parts, then merging "
                                        "and removing duplicate decisions and action items."})
    for i, part in enumerate(parts_text, start=1):
        label = f"Writing notes" + (f" for part {i} of {len(parts_text)}" if len(parts_text) > 1 else "")
        if len(parts_text) > 1:
            emit({"type": "progress", "done": i - 1, "total": len(parts_text), "text": label})
        messages = build_messages(part, meta, chosen, types, detail, sensitive_detail,
                                  (i, len(parts_text)) if len(parts_text) > 1 else None)
        key = hashlib.sha1(json.dumps(messages).encode()).hexdigest()
        if cache is not None and key in cache:
            result = cache[key]
        else:
            result = extract(messages, stream, complete, emit, label, schema_doc)
            if cache is not None and result["valid"]:
                cache[key] = result
        if result["data"].pop("_repair", None):
            incomplete = True
        results.append(result["data"])
        valid &= result["valid"]
        errors += result["errors"]
        attempts += result["attempts"]
    data = merge_notes(results) if len(results) > 1 else results[0]
    if len(results) > 1:
        emit({"type": "progress", "done": len(results), "total": len(results), "text": "Merging the parts"})
        if len(data.get("tldr") or []) > 5:
            data["tldr"] = _consolidate_tldr(data, complete) or data["tldr"][:5]
    emit({"type": "status", "text": "Checking every decision and action against the transcript"})
    notes, warnings, follow_up = postprocess(data, lines, meta, chosen)
    if not valid:
        warnings.insert(0, "The model's notes didn't fully match the expected format, even after a retry. "
                           "Showing the parts that did: " + "; ".join(errors[:4]))
    elif incomplete:
        warnings.insert(0, "The model's reply was cut short or left a section open, so some sections may be missing. "
                           "Regenerate if something looks incomplete.")
    return {"notes": notes, "meeting_type": chosen, "detected_type": detected, "warnings": warnings,
            "follow_up": follow_up, "valid": valid, "attempts": attempts, "parts": len(parts_text)}


def _consolidate_tldr(data: dict, complete: Complete) -> list[str] | None:
    """For long meetings: asks the model to rewrite the per-part summary bullets as 3-5 bullets for the whole
    meeting. None if that fails (the merged bullets stay).
    """
    source = "\n".join(f"- {t}" for t in data.get("tldr", []))
    messages = [{"role": "system", "content": "You condense meeting summaries. Reply with JSON only."},
                {"role": "user", "content": "These summary bullets come from consecutive parts of one meeting. "
                 "Rewrite them as 3-5 bullets for the whole meeting (outcome first). Use only facts below.\n"
                 f"{source}\n\nReply as {{\"tldr\": [\"…\"]}}"}]
    try:
        data2, _ = parse_json(complete(messages))
    except Exception:  # the merged bullets are still usable
        return None
    tldr = (data2 or {}).get("tldr")
    return [str(t) for t in tldr][:5] if isinstance(tldr, list) and tldr else None


# ---------------------------------------------------------------------------
# Render
# ---------------------------------------------------------------------------

def _cell(value) -> str:
    """A value made safe for a Markdown table cell (pipes escaped, 'n/a' and similar left empty)."""
    text = re.sub(r"\s+", " ", str(value or "")).replace("|", "\\|").strip()
    return "" if text.lower() in ("n/a", "na", "none.", "unknown") else text


def _ts_link(ts: str, tid: str) -> str:
    """A timestamp as a link that opens the meeting at that moment (or at that line)."""
    if not ts:
        return ""
    if ts.startswith("#"):
        return f"[line {ts[1:]}]({WEB_URL}/meetings/{tid}?line={ts[1:]})"
    seconds = ts_seconds(ts)
    return f"[{ts}]({WEB_URL}/meetings/{tid}?t={int(seconds or 0)})"


def default_sections(detail: str) -> dict[str, bool]:
    """Which optional sections are shown for a detail level ('brief' shows only the core)."""
    if detail == "brief":
        return {s: s in CORE for s in SECTIONS}
    return {s: True for s in SECTIONS}


def render_context(notes: dict, tid: str, meeting_type: str, detail: str = "standard",
                   sections: dict[str, bool] | None = None, follow_up: list[str] | None = None) -> dict:
    """Everything the template needs: header facts, summary, decisions, action items, the type-specific details and
    which sections to show.
    """
    types = meeting_types()
    show = default_sections(detail)
    for key, value in (sections or {}).items():
        if key in show and key not in CORE:  # header, summary, decisions and actions always show
            show[key] = bool(value)
    meeting = notes.get("meeting") or {}
    attendees = meeting.get("attendees") or []
    max_points = None if detail == "detailed" else 4
    max_quotes = 8 if detail == "detailed" else 3
    topics = []
    for t in notes.get("topics") or []:
        points = [{"text": _cell(p.get("text")), "speaker": _cell(p.get("speaker")), "ts": _ts_link(p.get("timestamp", ""), tid)}
                  for p in (t.get("points") or [])][:max_points]
        topics.append({"title": _cell(t.get("title")), "points": points})
    decisions = [{"n": i, "decision": _cell(d["decision"]), "made_by": _cell(d.get("made_by")) or "—",
                  "rationale": _cell(d.get("rationale")) or "—", "ts": _ts_link(d.get("timestamp", ""), tid) or "—"}
                 for i, d in enumerate(notes.get("decisions") or [], start=1)]
    actions = [{"n": i, "action": _cell(a["action"]), "owner": _cell(a.get("owner")) or "Unassigned",
                "due_date": _cell(a.get("due_date")) or "TBD", "priority": _cell(a.get("priority")) or "—",
                "ts": _ts_link(a.get("timestamp", ""), tid) or "—"}
               for i, a in enumerate(notes.get("action_items") or [], start=1)]
    risks = [{k: _cell(r.get(k)) or "—" for k in ("item", "type", "impact", "owner", "mitigation")}
             for r in notes.get("risks") or []]
    quotes = [{"quote": _cell(q["quote"]), "speaker": _cell(q.get("speaker")), "ts": _ts_link(q.get("timestamp", ""), tid)}
              for q in (notes.get("quotes") or [])][:max_quotes]
    nm = notes.get("next_meeting") or {}
    next_meeting = None
    if nm.get("date") or nm.get("time"):
        next_meeting = {"date": _cell(nm.get("date")) or "Date TBD", "time": _cell(nm.get("time")),
                        "agenda_display": "; ".join(_cell(a) for a in nm.get("agenda") or [])}
    details = _clean_details(notes.get("details") or {}, tid)
    has = {"decisions": bool(decisions), "actions": bool(actions), "risks": bool(risks), "quotes": bool(quotes),
           "follow_up": bool(follow_up), "next": bool(next_meeting or notes.get("next_steps"))}
    has.update({k: bool(v) for k, v in details.items()})
    return {
        "meeting": {"title": _cell(meeting.get("title")) or "Meeting", "date": _cell(meeting.get("date")),
                    "start_time": _cell(meeting.get("start_time")), "duration": _cell(meeting.get("duration")),
                    "type_label": types.get(meeting_type, types["general"])["label"],
                    "project": _cell(meeting.get("project")),
                    "attendees_display": ", ".join(_cell(a["name"]) + (f" ({_cell(a['role'])})" if a.get("role") else "")
                                                   for a in attendees) or "Not identified",
                    "absentees_display": ", ".join(_cell(a) for a in meeting.get("absentees") or [])},
        "show": show, "has": has, "tldr": [_cell(t) for t in notes.get("tldr") or []], "topics": topics,
        "decisions": decisions, "actions": actions, "risks": risks,
        "open_questions": [_cell(q) for q in notes.get("open_questions") or []],
        "next_steps": [_cell(s) for s in notes.get("next_steps") or []], "next_meeting": next_meeting,
        "quotes": quotes, "details": details, "follow_up_display": "; ".join(follow_up or []),
        "transcript_link": f"[Open in Quorum]({WEB_URL}/meetings/{tid})",
    }


def _clean_details(details: dict, tid: str) -> dict:
    """The type-specific details made ready for the template: table cells cleaned, empty values shown as '—',
    timestamps turned into links.
    """
    out = {}
    for key, value in details.items():
        if isinstance(value, list):
            rows = []
            for row in value:
                if isinstance(row, dict):
                    clean = {k: (_cell(v) if not isinstance(v, list) else [_cell(x) for x in v]) for k, v in row.items()}
                    rows.append({k: (v if v not in ("", []) else "—") for k, v in clean.items()})
                elif str(row).strip():
                    rows.append(_cell(row))
            if rows:
                out[key] = rows
        elif isinstance(value, dict):
            clean = {k: _cell(v) for k, v in value.items() if k != "timestamp"}
            if value.get("timestamp"):
                clean["ts"] = _ts_link(value["timestamp"], tid)
            if any(clean.values()):
                out[key] = clean
        elif str(value or "").strip():
            out[key] = _cell(value)
    for key in ("sprint_goal", "capacity", "rollback_plan", "impact", "root_cause", "recommendation", "candidate"):
        out.setdefault(key, "Not discussed.")
    out.setdefault("rag_status", {"overall": "Not stated"})
    out.setdefault("go_no_go", {"decision": "Not stated"})
    return out


def render(notes: dict, tid: str, meeting_type: str, detail: str = "standard",
           sections: dict[str, bool] | None = None, follow_up: list[str] | None = None) -> str:
    """The final Markdown notes: the meeting type's template filled with the notes."""
    types = meeting_types()
    template = types.get(meeting_type, types["general"])["template"]
    ctx = render_context(notes, tid, meeting_type, detail, sections, follow_up)
    return render_template(read_template(template)[0], ctx, lambda name: read_template(f"partials/{name}")[0])
