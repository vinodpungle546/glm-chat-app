#!/usr/bin/env python3
"""Meeting check: compares a meeting (its live draft during the call, or the final transcript) with earlier
meetings and memory, and finds four kinds of alerts:

  conflict   something said now contradicts an earlier decision, date or commitment
  raise      a risk, blocker, dependency or change all stakeholders should hear about, not yet said to the group
  pending    an action or open question from an earlier meeting (or tracked by the user) that isn't done and
             wasn't said to be done in this meeting
  my_action  something the user should raise or track in their role (e.g. Release Manager: release dates,
             go/no-go, scope changes, test sign-off, environments, deployments, approvals)

The model only sees numbered context: this meeting's lines (by timestamp), earlier meetings' saved notes
(decisions, actions, risks, open questions: E1…), matching passages of earlier transcripts (P1…), memory facts
(M1…) and the user's tracked actions (T1…). Every alert must point at that context, the same rule as the notes'
`trace`: conflict and pending need an earlier source, conflict/raise/my_action need a line of this meeting;
anything else is dropped. Alerts already shown or dismissed for this meeting aren't repeated (`is_repeat`).
"""

from __future__ import annotations

import json
import re
import uuid
from datetime import datetime
from typing import Callable

TYPES = ("conflict", "raise", "pending", "my_action")
LABELS = {"conflict": "Conflict", "raise": "To raise with everyone", "pending": "Still pending",
          "my_action": "My action"}
NEEDS_LINE = {"conflict", "raise", "my_action"}
NEEDS_SOURCE = {"conflict", "pending"}
MAX_ALERTS = 12
MAX_MEETING_CHARS = 60_000   # the meeting itself (the end is kept when longer: the check is about "now")
MAX_CONTEXT_CHARS = 14_000   # earlier meetings, passages, memory, tracked actions together
REPEAT_SIMILARITY = 0.6

_LINE = re.compile(r"^\[(\d{2}):(\d{2}):(\d{2})\]\s*([^:]{1,80}?):\s*(.+)$")


def parse_lines(markdown: str) -> list[dict]:
    """This meeting's lines from transcript Markdown ('[00:03:12] Priya: text'): time, seconds, speaker, text."""
    out = []
    for raw in (markdown or "").splitlines():
        m = _LINE.match(raw.strip())
        if m:
            h, mi, se, who, text = m.groups()
            out.append({"time": f"{h}:{mi}:{se}", "seconds": int(h) * 3600 + int(mi) * 60 + int(se),
                        "speaker": who.strip(), "text": text.strip()})
    return out


def earlier_items(meetings: list[dict]) -> list[dict]:
    """Numbered items (E1…) from earlier meetings' saved notes: {tid, title, date, notes}."""
    items = []
    for meeting in meetings:
        notes = meeting.get("notes") or {}
        where = {"tid": meeting["tid"], "meeting": meeting.get("title", ""), "date": (meeting.get("date") or "")[:10]}
        for d in notes.get("decisions") or []:
            items.append({**where, "kind": "decision", "text": d.get("decision", ""), "owner": d.get("made_by", ""),
                          "time": d.get("timestamp", "")})
        for a in notes.get("action_items") or []:
            items.append({**where, "kind": "action", "text": a.get("action", ""), "owner": a.get("owner", ""),
                          "due": a.get("due_date", ""), "time": a.get("timestamp", ""), "status": a.get("status", "")})
        for r in notes.get("risks") or []:
            text = r.get("risk", "") if isinstance(r, dict) else str(r)
            items.append({**where, "kind": "risk", "text": text, "time": (r.get("timestamp", "") if isinstance(r, dict) else "")})
        for q in notes.get("open_questions") or []:
            text = q.get("question", "") if isinstance(q, dict) else str(q)
            items.append({**where, "kind": "question", "text": text,
                          "time": (q.get("timestamp", "") if isinstance(q, dict) else "")})
    items = [i for i in items if i["text"].strip()]
    for n, item in enumerate(items, 1):
        item["id"] = f"E{n}"
    return items


def number(items: list[dict], prefix: str) -> list[dict]:
    """Gives each item an id: P1…, M1…, T1…"""
    return [dict(item, id=f"{prefix}{n}") for n, item in enumerate(items, 1)]


SYSTEM = ("You check a meeting against earlier meetings for the user. Reply only with JSON, no prose. "
          "Use only the numbered context you are given; never invent facts, names or dates.")


def _fit(lines: list[str], budget: int) -> list[str]:
    """As many lines as fit in `budget` characters, in order."""
    out, used = [], 0
    for line in lines:
        if used + len(line) > budget:
            break
        out.append(line)
        used += len(line) + 1
    return out


def build_messages(lines: list[dict], earlier: list[dict], passages: list[dict], memory: list[dict],
                   tracked: list[dict], me: str, role: str, project: str) -> list[dict]:
    """The prompt: who the user is, the numbered context, this meeting's lines, the four alert types and the JSON
    shape."""
    meeting = [f"[{l['time']}] {l['speaker']}: {l['text']}" for l in lines]
    total = 0
    kept: list[str] = []
    for line in reversed(meeting):  # keep the end: the check is about what's being said now
        if total + len(line) > MAX_MEETING_CHARS:
            break
        kept.append(line)
        total += len(line) + 1
    meeting = list(reversed(kept))
    context = []
    context += [f"{e['id']} ({e['kind']}, {e['meeting']} {e['date']}{', ' + e['time'] if e.get('time') else ''}): "
                f"{e['text']}" + (f" — owner {e['owner']}" if e.get("owner") else "")
                + (f", due {e['due']}" if e.get("due") else "") + (f" [{e['status']}]" if e.get("status") else "")
                for e in earlier]
    context += [f"{p['id']} (from {p['meeting']} {p['date']}, {p.get('time', '')}): {p['text']}" for p in passages]
    context += [f"{m['id']} (memory): {m['text']}" for m in memory]
    context += [f"{t['id']} (tracked by the user, not done): {t['text']}" for t in tracked]
    context = _fit(context, MAX_CONTEXT_CHARS)
    who = me or "the user"
    role_text = f"{who}'s role: {role}." if role else f"{who}'s role isn't known: for my_action, list what {who} " \
                                                       "was asked to do or offered to do."
    body = [
        f"The user is {who} (\"You\" in this meeting's lines). {role_text}" + (f" Project: {project}." if project else ""),
        "",
        "Earlier context (cite these ids):",
        *(context or ["(none)"]),
        "",
        "This meeting so far (cite the [hh:mm:ss] time of a line):",
        *(meeting or ["(nothing yet)"]),
        "",
        "Find alerts of these types:",
        "- conflict: something said in this meeting contradicts an earlier decision, date or commitment. "
        "Needs the line and the earlier source.",
        "- raise: a risk, blocker, dependency or change all stakeholders should hear about that nobody has said to "
        "the whole group yet. Needs the line.",
        "- pending: an earlier action or open question (or one the user tracks) that isn't done and wasn't said to be "
        "done in this meeting. Needs the source; line is null unless a line touches it.",
        f"- my_action: something {who} should raise or track{' as ' + role if role else ''}. Needs the line.",
        f"At most {MAX_ALERTS} alerts, the most important first; none is fine. Each text is one short sentence "
        "addressed to the user.",
        'Reply as {"alerts": [{"type": "conflict", "text": "…", "line": "00:12:04", "sources": ["E3"]}]}',
    ]
    return [{"role": "system", "content": SYSTEM}, {"role": "user", "content": "\n".join(body)}]


def parse_reply(reply: str) -> list[dict] | None:
    """The alerts in the model's JSON (None when there is no JSON object to read)."""
    match = re.search(r"\{.*\}", reply or "", re.DOTALL)
    if not match:
        return None
    try:
        data = json.loads(match.group(0))
    except ValueError:
        return None
    alerts = data.get("alerts") if isinstance(data, dict) else None
    return alerts if isinstance(alerts, list) else None


def validate(alerts: list, lines: list[dict], context: dict[str, dict]) -> tuple[list[dict], list[dict]]:
    """(kept, dropped with a reason). Kept alerts get their line's seconds and their sources' details."""
    times = {l["time"]: l for l in lines}
    kept, dropped = [], []
    for raw in alerts[: MAX_ALERTS * 2]:
        if not isinstance(raw, dict):
            continue
        kind = str(raw.get("type", "")).strip().lower()
        text = re.sub(r"\s+", " ", str(raw.get("text", ""))).strip()[:300]
        line = str(raw.get("line") or "").strip() or None
        sources = [s for s in (raw.get("sources") or []) if isinstance(s, str)]
        known = [s for s in sources if s in context]
        reason = None
        if kind not in TYPES or not text:
            reason = "not an alert"
        elif line and line not in times:
            reason = "cites a line that isn't in the meeting"
        elif kind in NEEDS_LINE and not line:
            reason = "no line of this meeting"
        elif kind in NEEDS_SOURCE and not known:
            reason = "no earlier source"
        elif sources and not known:
            reason = "cites context that doesn't exist"
        if reason:
            dropped.append({"type": kind, "text": text, "reason": reason})
            continue
        kept.append({"type": kind, "text": text, "line": line,
                     "seconds": times[line]["seconds"] if line else None,
                     "quote": times[line]["text"][:200] if line else "",
                     "sources": [context[s] for s in known]})
    return kept[:MAX_ALERTS], dropped


def _words(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9']+", text.lower()) if len(w) > 2}


def is_repeat(alert: dict, earlier: list[dict]) -> bool:
    """True when an alert of the same type says much the same as one already shown (or dismissed)."""
    mine = _words(alert["text"])
    for old in earlier:
        if old["type"] != alert["type"]:
            continue
        theirs = _words(old["text"])
        if mine and theirs and len(mine & theirs) / len(mine | theirs) >= REPEAT_SIMILARITY:
            return True
        same_source = {s.get("ref") for s in alert["sources"]} & {s.get("ref") for s in old.get("sources", [])}
        if alert["type"] == "pending" and same_source - {None}:
            return True
    return False


def check(lines: list[dict], earlier: list[dict], passages: list[dict], memory: list[dict], tracked: list[dict],
          complete: Callable[[list[dict]], str], me: str = "", role: str = "", project: str = "",
          seen: list[dict] | None = None) -> dict:
    """Runs the check: one model call (one retry when the reply isn't JSON), validation, de-duplication against
    `seen`. Returns {alerts (new ones, with ids), dropped, repeats}."""
    context: dict[str, dict] = {}
    for e in earlier:
        context[e["id"]] = {"ref": e["id"] + ":" + e["tid"] + ":" + e["text"][:40], "kind": e["kind"], "text": e["text"],
                            "meeting": e["meeting"], "date": e["date"], "tid": e["tid"], "time": e.get("time", ""),
                            "owner": e.get("owner", ""), "due": e.get("due", "")}
    for p in passages:
        context[p["id"]] = {"ref": p["id"] + ":" + p.get("tid", "") + ":" + p.get("time", ""), "kind": "passage",
                            "text": p["text"], "meeting": p["meeting"], "date": p["date"], "tid": p.get("tid", ""),
                            "time": p.get("time", "")}
    for m in memory:
        context[m["id"]] = {"ref": "memory:" + m["text"][:60], "kind": "memory", "text": m["text"]}
    for t in tracked:
        context[t["id"]] = {"ref": "tracked:" + t.get("tracked_id", ""), "kind": "tracked", "text": t["text"],
                            "tracked_id": t.get("tracked_id", "")}
    messages = build_messages(lines, earlier, passages, memory, tracked, me, role, project)
    raw = parse_reply(complete(messages))
    if raw is None:
        retry = messages + [{"role": "user", "content": 'That wasn\'t valid JSON. Reply again with only '
                                                        '{"alerts": [...]} as asked.'}]
        raw = parse_reply(complete(retry)) or []
    kept, dropped = validate(raw, lines, context)
    fresh, repeats = [], 0
    for alert in kept:
        if is_repeat(alert, (seen or []) + fresh):
            repeats += 1
            continue
        alert.update(id=uuid.uuid4().hex[:10], state="new", created=datetime.now().isoformat(timespec="seconds"))
        fresh.append(alert)
    return {"alerts": fresh, "dropped": dropped, "repeats": repeats}


def summary_line(alerts: list[dict]) -> str:
    """'2 alerts: 1 conflict, 1 pending' (the pop-up shows only this, never the content)."""
    counts: dict[str, int] = {}
    for a in alerts:
        counts[a["type"]] = counts.get(a["type"], 0) + 1
    parts = [f"{n} {LABELS[t].lower() if t != 'my_action' else ('action for you' if n == 1 else 'actions for you')}"
             for t, n in counts.items()]
    return f"{len(alerts)} alert{'s' if len(alerts) != 1 else ''}: " + ", ".join(parts)
