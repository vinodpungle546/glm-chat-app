#!/usr/bin/env python3
"""Cloud review of speaker names (runs after the local pass in speaker_id.py).

When the selected chat model is a cloud model, it gets the local result and the transcript and
may keep or change each speaker's name and role. Every change must cite transcript lines (or
match a name seen on the speaker's video tile); unsupported changes are rejected and the local
result stays. Names the user set (source "user") are never sent for change and never changed.

Sent: transcript lines (speaker label, time, text), the local suggestions per speaker (name, role,
confidence, evidence), on-screen names seen, and people from memory relevant to this meeting
(name, aliases, role, team, projects only). Never sent: voice fingerprints, video frames, or
memory unrelated to the meeting.

Long transcripts are split to fit the provider's context budget; results are merged per speaker.

Before the names, the same review fixes spelling (`spelling`): misspelled or misheard words in the lines it was sent
(names, product terms, acronyms), preferring the names and terms Quorum knows. Each fix is checked: the line keeps
its wording, only a few words change, and each changed word either sounds like the one it replaces or is a known
name or term. Lines from your microphone are never changed. Fixes go to the Corrections tab (Keep / Revert).
"""

from __future__ import annotations

import json
import re
from collections import defaultdict
from difflib import SequenceMatcher
from typing import Callable

import jsonschema

SYSTEM = ("You review speaker identification for a meeting transcript. A local model already suggested a name "
          "and role per speaker. Keep each suggestion unless the transcript clearly shows something else. "
          "Reply with JSON only.")
REPLY_SCHEMA = {
    "type": "object",
    "required": ["speakers"],
    "properties": {"speakers": {"type": "array", "items": {
        "type": "object",
        "required": ["id", "action"],
        "properties": {
            "id": {"type": "string"},
            "action": {"enum": ["keep", "change"]},
            "name": {"type": ["string", "null"]},
            "role": {"type": ["string", "null"]},
            "confidence": {"type": "number", "minimum": 0, "maximum": 1},
            "reason": {"type": "string"},
            "lines": {"type": "array", "items": {"type": "integer"}},
        }}}},
}
DEFAULT_CHARS = 280_000   # ~80k tokens of transcript per request
MAX_REASON = 300


def _fmt(seconds) -> str:
    """A time position as 'mm:ss', or 'h:mm:ss' after an hour."""
    seconds = int(seconds or 0)
    return f"{seconds // 3600}:{seconds % 3600 // 60:02d}:{seconds % 60:02d}" if seconds >= 3600 \
        else f"{seconds // 60:02d}:{seconds % 60:02d}"


def transcript_lines(transcript: dict) -> list[tuple[int, str, str]]:
    """[(line number, speaker label, "#n [mm:ss] SPEAKER_00: text")]"""
    out = []
    for i, seg in enumerate(transcript.get("segments", [])):  # numbered like the transcript view (?line=n)
        if not (seg.get("text") or "").strip():
            continue
        text = " ".join(seg["text"].split())
        out.append((i + 1, seg.get("speaker", ""), f"#{i + 1} [{_fmt(seg.get('start'))}] {seg.get('speaker', '')}: {text}"))
    return out


def relevant_people(known: list[dict], transcript_text: str, names: list[str], limit: int = 25) -> list[dict]:
    """People from memory this meeting is about: named in it, suggested, or seen on screen.
    Only name, aliases, role, team and projects are kept."""
    wanted = {n.lower() for n in names if n} | {n.split()[0].lower() for n in names if n}
    out = []
    for person in known:
        candidates = [person["name"]] + list(person.get("aliases") or [])
        hit = any(c.lower() in wanted for c in candidates) or any(
            len(c.split()[0]) >= 3 and re.search(rf"\b{re.escape(c.split()[0])}\b", transcript_text) for c in candidates if c)
        if hit:
            out.append({"name": person["name"], "aliases": list(person.get("aliases") or [])[:4],
                        "role": person.get("role", ""), "team": person.get("team", ""),
                        "projects": list(person.get("projects") or [])[:4]})
        if len(out) >= limit:
            break
    return out


def screen_summary(transcript: dict) -> dict[str, list[str]]:
    """Names seen on each speaker's video tile, e.g. {'SPEAKER_00': ['Priya Sharma (seen 4×)']}."""
    out = {}
    for spk, info in (transcript.get("screen_names") or {}).items():
        names = [f"{n['name']} (seen {n['count']}×)" for n in (info or {}).get("names", [])[:3]]
        if names:
            out[spk] = names
    return out


def build_messages(lines: list[str], labels: dict[str, dict], editable: list[str], people: list[dict],
                   screen: dict[str, list[str]], part: tuple[int, int] = (1, 1)) -> list[dict]:
    """The messages for the cloud review: the numbered transcript lines, the local suggestions to review, names
    the user fixed, names seen on screen and relevant people from memory, plus the reply format (JSON with
    cited line numbers).
    """
    suggestions = []
    for spk in editable:
        label = labels.get(spk) or {}
        suggestions.append({"id": spk, "name": label.get("name") or None, "role": label.get("role") or "",
                            "confidence": label.get("confidence", 0), "evidence": (label.get("evidence") or [])[:4],
                            "on_screen": screen.get(spk, [])})
    fixed = [{"id": spk, "name": lab.get("name")} for spk, lab in labels.items()
             if lab.get("source") in ("user", "microphone") and lab.get("name")]
    body = [f"Transcript part {part[0]} of {part[1]}." if part[1] > 1 else "",
            "Local suggestions per speaker (review these):", json.dumps(suggestions, ensure_ascii=False),
            ("Speakers the user named (fixed; don't reuse these names for others): " + json.dumps(fixed, ensure_ascii=False))
            if fixed else "",
            ("People from the user's memory who may be in this meeting: " + json.dumps(people, ensure_ascii=False))
            if people else "",
            "", "Transcript (#line [time] SPEAKER: text):", *lines, "",
            "Rules:",
            "- For every speaker above, reply with action \"keep\" or \"change\".",
            "- Change only when the transcript clearly supports it (introductions, being addressed by name and "
            "answering, the name on their video tile, what they say about their role). Give the line numbers that "
            "show it in \"lines\". A change without supporting lines is ignored.",
            "- A name must appear in the transcript, the on-screen names, or the people from memory. Never invent one.",
            "- Never give two speakers the same name. Use null for the name when it isn't clear.",
            "- reason: one short sentence a user can check.",
            'Reply as {"speakers": [{"id": "SPEAKER_00", "action": "keep" or "change", "name": "…" or null, '
            '"role": "…", "confidence": 0.0-1.0, "reason": "…", "lines": [12, 40]}]}']
    return [{"role": "system", "content": SYSTEM},
            {"role": "user", "content": "\n".join(x for x in body if x is not None)}]


def parse(reply: str) -> tuple[dict | None, str]:
    """(data, "") or (None, what's wrong) for one model reply."""
    text = re.sub(r"<think>.*?</think>", "", reply or "", flags=re.S)
    match = re.search(r"\{.*\}", text, re.S)
    if not match:
        return None, "The reply contained no JSON object."
    try:
        data = json.loads(match.group(0))
    except json.JSONDecodeError as exc:
        return None, f"The JSON is invalid: {exc.msg} at character {exc.pos}."
    try:
        jsonschema.validate(data, REPLY_SCHEMA)
    except jsonschema.ValidationError as exc:
        return None, f"The JSON doesn't match the format: {exc.message}"
    return data, ""


def ask(complete: Callable[[list[dict]], str], messages: list[dict]) -> dict:
    """One request, plus one retry with the errors when the reply isn't valid JSON."""
    reply = complete(messages)
    data, error = parse(reply)
    if data is not None:
        return data
    retry = messages + [{"role": "assistant", "content": reply[:4000]},
                        {"role": "user", "content": f"{error} Reply again with only the JSON in the format asked."}]
    data, error = parse(complete(retry))
    if data is None:
        raise ValueError(error)
    return data


def split_lines(lines: list[tuple[int, str, str]], budget_chars: int) -> list[list[tuple[int, str, str]]]:
    """Splits the numbered transcript lines into parts of at most `budget_chars` characters (long meetings are
    reviewed part by part).
    """
    parts, current, used = [], [], 0
    for line in lines:
        if current and used + len(line[2]) > budget_chars:
            parts.append(current)
            current, used = [], 0
        current.append(line)
        used += len(line[2]) + 1
    if current:
        parts.append(current)
    return parts or [[]]


def merge(proposals: dict[str, list[dict]]) -> dict[str, dict]:
    """One decision per speaker across parts: the change with the most total confidence, if any."""
    out = {}
    for spk, rows in proposals.items():
        changes = [r for r in rows if r["action"] == "change"]
        if not changes:
            out[spk] = {"action": "keep"}
            continue
        score: dict[str, float] = defaultdict(float)
        for row in changes:
            score[(row.get("name") or "").strip().lower()] += float(row.get("confidence") or 0.5)
        best = max(score, key=score.get)
        chosen = [r for r in changes if (r.get("name") or "").strip().lower() == best]
        top = max(chosen, key=lambda r: float(r.get("confidence") or 0))
        out[spk] = dict(top, lines=sorted({n for r in chosen for n in r.get("lines") or []}),
                        reason=" ".join(dict.fromkeys(r.get("reason", "") for r in chosen if r.get("reason")))[:MAX_REASON])
    return out


def review(transcript: dict, labels: dict[str, dict], known: list[dict], complete: Callable[[list[dict]], str],
           budget_chars: int = DEFAULT_CHARS, progress: Callable[[str], None] | None = None) -> dict:
    """{"changes": [...], "rejected": [...], "agreed": n, "parts": n, "sent": {...}} (labels not modified)."""
    lines = transcript_lines(transcript)
    valid_numbers = {n for n, _, _ in lines}
    speakers = list(dict.fromkeys(spk for _, spk, _ in lines if spk))
    # Names the user set are final: the cloud model may only review the others.
    editable = [spk for spk in speakers if (labels.get(spk) or {}).get("source") not in ("user", "microphone")]
    screen = screen_summary(transcript)
    text = " ".join(t for _, _, t in lines)
    names = [lab.get("name", "") for lab in labels.values()] + [
        n["name"] for info in (transcript.get("screen_names") or {}).values() for n in (info or {}).get("names", [])]
    people = relevant_people(known, text, names)
    result = {"changes": [], "rejected": [], "agreed": 0, "parts": 0,
              "sent": {"lines": len(lines), "speakers": editable, "people": [p["name"] for p in people],
                       "on_screen": sorted(screen)}}
    if not editable:
        return result
    # Long meetings are sent in parts; each part gives its own proposals, merged per speaker below.
    parts = split_lines(lines, budget_chars)
    result["parts"] = len(parts)
    proposals: dict[str, list[dict]] = defaultdict(list)
    for i, part in enumerate(parts):
        if progress:
            progress(f"Cloud review: part {i + 1} of {len(parts)}…" if len(parts) > 1 else "Cloud review…")
        data = ask(complete, build_messages([t for _, _, t in part], labels, editable, people, screen, (i + 1, len(parts))))
        for row in data["speakers"]:
            if row["id"] in editable:
                proposals[row["id"]].append(row)
    decided = merge(proposals)
    screen_names = {spk: {n["name"].lower() for n in (info or {}).get("names", [])}
                    for spk, info in (transcript.get("screen_names") or {}).items()}
    taken = {lab["name"].lower(): spk for spk, lab in labels.items() if lab.get("name") and lab.get("source") in ("user", "microphone")}
    accepted: list[dict] = []
    for spk in editable:
        row = decided.get(spk) or {"action": "keep"}
        local = labels.get(spk) or {}
        if row["action"] != "change":
            result["agreed"] += 1
            continue
        name = (row.get("name") or "").strip()
        role = (row.get("role") or "").strip()[:60]
        # Safety rules for a change: it must cite real transcript line numbers or match a name seen on that
        # speaker's video tile, and it can't give a name the user already gave to someone else. Changes that
        # fail are listed as 'rejected' with the reason.
        cited = [n for n in row.get("lines") or [] if n in valid_numbers]
        on_screen = bool(name) and name.lower() in screen_names.get(spk, set())
        if name == (local.get("name") or "") and role == (local.get("role") or ""):
            result["agreed"] += 1
            continue
        why_rejected = ("no transcript line supports it" if not cited and not on_screen
                        else f"{name} is already {taken[name.lower()]}" if name and name.lower() in taken
                        and taken[name.lower()] != spk else "")
        if why_rejected:
            result["rejected"].append({"speaker": spk, "name": name, "role": role, "reason": row.get("reason", ""),
                                       "why": why_rejected})
            continue
        accepted.append({"speaker": spk, "name": name, "role": role or local.get("role", ""),
                         "confidence": round(float(row.get("confidence") or 0.6), 2),
                         "reason": (row.get("reason") or "").strip()[:MAX_REASON], "lines": cited,
                         "on_screen": on_screen})
    # one name per speaker: the more confident claim wins
    for change in sorted(accepted, key=lambda c: -c["confidence"]):
        key = change["name"].lower()
        if key and key in taken and taken[key] != change["speaker"]:
            result["rejected"].append(dict(change, why=f"{change['name']} is already {taken[key]}"))
            continue
        if key:
            taken[key] = change["speaker"]
        local = labels.get(change["speaker"]) or {}
        result["changes"].append({**change, "from": {"name": local.get("name", ""), "role": local.get("role", ""),
                                                     "confidence": local.get("confidence", 0),
                                                     "level": local.get("level", "low")}, "state": "pending"})
    return result


def level(confidence: float) -> str:
    """'high' (≥ 0.75), 'medium' (≥ 0.5) or 'low' for a confidence number."""
    return "high" if confidence >= 0.75 else "medium" if confidence >= 0.5 else "low"


def apply(labels: dict[str, dict], result: dict, model: str) -> dict[str, dict]:
    """Labels with the accepted changes; the local label is kept under "previous" for Revert."""
    out = {spk: dict(v) for spk, v in labels.items()}
    for change in result["changes"]:
        spk = change["speaker"]
        previous = labels.get(spk) or {"name": "", "role": "", "confidence": 0.0, "level": "low",
                                       "evidence": [], "source": "auto"}
        why = f"{model}: {change['reason']}" if change["reason"] else f"changed by {model}"
        out[spk] = {"name": change["name"], "role": change["role"], "confidence": change["confidence"],
                    "level": level(change["confidence"]), "evidence": [why] + list(previous.get("evidence") or [])[:3],
                    "source": "cloud", "cloud": {"model": model, "reason": change["reason"], "lines": change["lines"],
                                                 "kept": False}, "previous": previous}
    return out


def revert(labels: dict[str, dict], speaker: str) -> dict[str, dict]:
    """Undoes the cloud review's change for one speaker: restores the label the local pass gave."""
    label = labels.get(speaker) or {}
    if label.get("source") == "cloud" and "previous" in label:
        labels[speaker] = label["previous"]
    return labels


# ---------------------------------------------------------------------------
# Spelling (before the names)
# ---------------------------------------------------------------------------

SPELLING_SYSTEM = ("You proofread a meeting transcript made by speech recognition. Fix only misspelled or misheard "
                   "words: people's names, product and project names, technical terms and acronyms. Prefer the "
                   "spellings in the known names and terms. Never rephrase, summarise, translate, add or remove "
                   "content, and never change punctuation alone. Reply with JSON only.")
SPELLING_SCHEMA = {
    "type": "object", "required": ["fixes"],
    "properties": {"fixes": {"type": "array", "items": {
        "type": "object", "required": ["line", "text"],
        "properties": {"line": {"type": "integer"}, "text": {"type": "string"}, "reason": {"type": "string"}}}}},
}
MAX_CHANGED = 4          # words changed in one place
CHANGED_SHARE = 0.34     # share of a line's words a fix may change (at least MAX_CHANGED)
SOUNDS_LIKE = 0.6


def _norm(word: str) -> str:
    return re.sub(r"[^\w']", "", word.lower())


def _sound(text: str) -> str:
    import participants_check
    return "".join(participants_check.sound_key(w) for w in text.split())


def check_fix(before: str, after: str, terms: set[str]) -> str:
    """"" when `after` only fixes spellings in `before`; else why it's refused."""
    a, b = before.split(), after.split()
    if [_norm(w) for w in a] == [_norm(w) for w in b]:
        return "nothing but punctuation or capitals changed"
    changed = 0
    term_words = {w for t in terms for w in (_norm(x) for x in t.split()) if w}
    for op, i1, i2, j1, j2 in SequenceMatcher(None, [_norm(w) for w in a], [_norm(w) for w in b]).get_opcodes():
        if op == "equal":
            continue
        old, new = " ".join(a[i1:i2]), " ".join(b[j1:j2])
        if i2 - i1 > MAX_CHANGED or j2 - j1 > MAX_CHANGED:
            return f"rewrites “{old or new}”"
        known = bool(new) and all(_norm(w) in term_words for w in b[j1:j2])
        alike = bool(old) and bool(new) and SequenceMatcher(None, _sound(old), _sound(new)).ratio() >= SOUNDS_LIKE
        if not (known or alike):
            return f"“{old}” → “{new}” isn't a spelling fix" if old and new else f"adds or drops “{old or new}”"
        changed += max(i2 - i1, j2 - j1)
    if changed > max(MAX_CHANGED, round(len(a) * CHANGED_SHARE)):
        return "changes too much of the line"
    return ""


def spelling_messages(lines: list[str], terms: list[str], part: tuple[int, int] = (1, 1)) -> list[dict]:
    body = [f"Transcript part {part[0]} of {part[1]}." if part[1] > 1 else "",
            "Known names and terms (preferred spellings): " + "; ".join(terms[:300]) if terms else "",
            "", "Transcript (#line [time] SPEAKER: text):", *lines, "",
            "Rules:",
            "- Only fix words the speech recognition got wrong: misspelled names, misheard terms or acronyms.",
            "- Give the whole corrected text of the line (without the #number, time or speaker).",
            "- Keep every other word exactly as it is. Don't fix grammar, filler words or style.",
            "- reason: what was wrong, in a few words (e.g. 'misheard name Ciarán').",
            "- Leave out lines that need nothing.",
            'Reply as {"fixes": [{"line": 12, "text": "…", "reason": "…"}]} or {"fixes": []}.']
    return [{"role": "system", "content": SPELLING_SYSTEM},
            {"role": "user", "content": "\n".join(x for x in body if x)}]


def _ask_schema(complete: Callable[[list[dict]], str], messages: list[dict], schema: dict) -> dict:
    """One request plus one retry with the errors, for any of the review's JSON formats."""
    def parse_with(reply: str) -> tuple[dict | None, str]:
        text = re.sub(r"<think>.*?</think>", "", reply or "", flags=re.S)
        match = re.search(r"\{.*\}", text, re.S)
        if not match:
            return None, "The reply contained no JSON object."
        try:
            data = json.loads(match.group(0))
            jsonschema.validate(data, schema)
        except json.JSONDecodeError as exc:
            return None, f"The JSON is invalid: {exc.msg}."
        except jsonschema.ValidationError as exc:
            return None, f"The JSON doesn't match the format: {exc.message}"
        return data, ""

    reply = complete(messages)
    data, error = parse_with(reply)
    if data is not None:
        return data
    data, error = parse_with(complete(messages + [{"role": "assistant", "content": reply[:4000]},
                                                  {"role": "user", "content": f"{error} Reply again with only the JSON."}]))
    if data is None:
        raise ValueError(error)
    return data


def spelling(transcript: dict, labels: dict[str, dict], terms: list[str], complete: Callable[[list[dict]], str],
             budget_chars: int = DEFAULT_CHARS, progress: Callable[[str], None] | None = None) -> dict:
    """{"fixes": [{line (0-based), before, text, reason}], "rejected": [{line, text, why}], "parts": n}. Lines of
    the microphone's speaker (a call Quorum recorded) are sent for context but never changed."""
    lines = transcript_lines(transcript)
    segs = transcript.get("segments") or []
    fixed = {spk for spk, lab in labels.items() if lab.get("source") == "microphone"}
    fixed |= set(transcript.get("confirmed_speakers") or {})
    term_set = set(terms)
    out: dict = {"fixes": [], "rejected": [], "parts": 0}
    parts = split_lines(lines, budget_chars)
    out["parts"] = len(parts)
    seen: set[int] = set()
    for k, part in enumerate(parts):
        if progress:
            progress(f"Cloud review: spelling, part {k + 1} of {len(parts)}…" if len(parts) > 1 else "Cloud review: spelling…")
        data = _ask_schema(complete, spelling_messages([t for _, _, t in part], terms, (k + 1, len(parts))),
                           SPELLING_SCHEMA)
        numbers = {n for n, _, _ in part}
        for fix in data["fixes"]:
            n, text = fix["line"], " ".join(str(fix.get("text") or "").split())
            reason = " ".join(str(fix.get("reason") or "").split())[:MAX_REASON]
            i = n - 1
            why = ("not a line it was sent" if n not in numbers or not 0 <= i < len(segs) else
                   "already fixed" if i in seen else
                   "your microphone's line" if segs[i].get("speaker") in fixed else
                   "no reason given" if not reason else
                   check_fix(segs[i].get("text", ""), text, term_set))
            if why:
                out["rejected"].append({"line": i, "text": text, "why": why})
                continue
            seen.add(i)
            out["fixes"].append({"line": i, "before": segs[i].get("text", ""), "text": text, "reason": reason})
    return out
