"""The last step of speaker identification: every speaker name checked against the call's participants list.

The list read from the video (transcriber/participants.py, saved as the transcript's `participants`) holds the
names as the meeting app shows them. After the local pass (and the cloud review, when it runs):

- a name close to a listed one takes the list's spelling: "Kieran" → "Ciarán Byrne" (sounds alike, the only
  listed first name like it), "Owen" → "Owen Hart" (the only Owen), "Priya Shar" → "Priya Sharma" (cut short);
  the change is recorded with its reason, and can be kept or reverted (the label keeps the earlier one under
  `previous`);
- a name that matches nobody on the list keeps its place but loses confidence and is flagged;
- listed names no speaker has are offered as suggestions for the speakers without a name.

Names you set and your microphone's speaker are never changed. Pure code; runs on this Mac only.
"""

from __future__ import annotations

import re
import unicodedata
from difflib import SequenceMatcher

FIXED_SOURCES = ("user", "microphone")
SOUND_ALIKE = 0.8         # first names this similar (after spelling rules) sound alike
NOT_LISTED_FACTOR = 0.7   # confidence kept by a name that matches nobody on the list
MAX_SUGGESTIONS = 6


def fold(name: str) -> str:
    """Lower case, no accents, single spaces ("Ciarán" → "ciaran")."""
    text = unicodedata.normalize("NFKD", name or "")
    return " ".join("".join(c for c in text if not unicodedata.combining(c)).lower().split())


def sound_key(word: str) -> str:
    """A rough spoken form of a name, so spellings that sound alike compare equal-ish: c→k, ph→f, y→i,
    doubled letters once ("Ciarán" → "kiaran", "Kieran" → "kieran", "Steven"/"Stephen" → "steven"/"stefen")."""
    w = re.sub(r"[^a-z]", "", fold(word))
    w = w.replace("ph", "f").replace("ck", "k").replace("q", "k").replace("c", "k").replace("y", "i").replace("z", "s")
    return re.sub(r"(.)\1+", r"\1", w)


def level(confidence: float) -> str:
    return "high" if confidence >= 0.75 else "medium" if confidence >= 0.5 else "low"


def match(name: str, roster: list[str]) -> tuple[str, str] | None:
    """(listed name, how) for a speaker name: exact | accents | first name | cut short | sounds alike; None when
    nobody on the list matches. Only a single candidate counts (two Owens: no match)."""
    key = fold(name)
    if not key:
        return None
    for listed in roster:
        if listed == name:
            return listed, "exact"
    same = [r for r in roster if fold(r) == key]
    if len(same) == 1:
        return same[0], "accents"
    first = key.split()[0]
    if " " not in key:
        by_first = [r for r in roster if fold(r).split()[0] == first]
        if len(by_first) == 1:
            return by_first[0], "first name"
    longer = [r for r in roster if fold(r).startswith(key) and len(fold(r)) > len(key) and len(key) >= 4]
    if len(longer) == 1:
        return longer[0], "cut short"
    words = key.split()
    alike = []
    for r in roster:
        parts = fold(r).split()
        if len(words) > len(parts):
            continue
        scores = [SequenceMatcher(None, sound_key(a), sound_key(b)).ratio() for a, b in zip(words, parts)]
        if scores and min(scores) >= SOUND_ALIKE:
            alike.append(r)
    if len(alike) == 1:
        return alike[0], "sounds alike"
    return None


def check(labels: dict[str, dict], roster: list[str]) -> tuple[dict[str, dict], dict]:
    """(new labels, result). result = {"changes": [{speaker, from, to, how, reason, state}], "flagged":
    [{speaker, name}], "confirmed": [speakers], "suggestions": [names]}. Labels you set and the microphone's are
    never changed; one listed name never goes to two speakers."""
    out = {spk: dict(v) for spk, v in labels.items()}
    result: dict = {"changes": [], "flagged": [], "confirmed": [], "suggestions": []}
    if not roster:
        return out, result
    taken = {fold(v["name"]): spk for spk, v in labels.items() if v.get("name") and v.get("source") in FIXED_SOURCES}
    listed_keys = {fold(r) for r in roster}
    by_confidence = sorted(labels.items(), key=lambda kv: -float(kv[1].get("confidence") or 0))
    for spk, label in by_confidence:  # names already spelled as listed are claimed first (the surest one wins)
        key = fold(label.get("name") or "")
        if key in listed_keys and key not in taken:
            taken[key] = spk
    for spk, label in by_confidence:
        name = (label.get("name") or "").strip()
        if not name or label.get("source") in FIXED_SOURCES:
            continue
        found = match(name, roster)
        if found is None:
            new = dict(label, confidence=round(float(label.get("confidence") or 0) * NOT_LISTED_FACTOR, 2),
                       flag="not_in_list")
            new["level"] = level(new["confidence"])
            new["evidence"] = ["not in the meeting's participants list"] + list(label.get("evidence") or [])[:4]
            out[spk] = new
            result["flagged"].append({"speaker": spk, "name": name})
            continue
        listed, how = found
        owner = taken.get(fold(listed))
        if owner and owner != spk:
            result.setdefault("taken", []).append({"speaker": spk, "name": name, "listed": listed, "by": owner})
            continue  # another speaker has this listed name already
        taken[fold(listed)] = spk
        evidence = [f"in the meeting's participants list as {listed}"] + [
            e for e in (label.get("evidence") or []) if "participants list" not in e][:4]
        if listed == name:
            out[spk] = dict(label, evidence=evidence, listed=True)
            result["confirmed"].append(spk)
            continue
        reason = {"accents": f"the participants list spells it {listed}",
                  "first name": f"{listed} is the only {name} in the participants list",
                  "cut short": f"{name} is the start of {listed} in the participants list",
                  "sounds alike": f"{name} sounds like {listed}, the only similar name in the participants list",
                  "exact": ""}[how]
        previous = {k: v for k, v in label.items() if k != "previous"}
        out[spk] = dict(label, name=listed, evidence=[reason] + evidence[1:], source="participants", listed=True,
                        previous=previous)
        result["changes"].append({"speaker": spk, "from": name, "to": listed, "how": how, "reason": reason,
                                  "state": "pending"})
    used = {fold(v.get("name") or "") for v in out.values()}
    result["suggestions"] = [r for r in roster if fold(r) not in used][:MAX_SUGGESTIONS]
    for spk, label in out.items():
        if not label.get("name") and result["suggestions"]:
            out[spk] = dict(label, suggestions=result["suggestions"])
        elif "suggestions" in label:
            out[spk] = {k: v for k, v in label.items() if k != "suggestions"}
    return out, result


def revert(labels: dict[str, dict], speaker: str) -> dict[str, dict]:
    """Undoes the list check's change for one speaker: the earlier label comes back."""
    label = labels.get(speaker) or {}
    if label.get("source") == "participants" and "previous" in label:
        labels[speaker] = label["previous"]
    return labels


def roster_of(transcript: dict) -> list[str]:
    """The names in the transcript's participants list (people who joined, not only invited)."""
    return [e["name"] for e in ((transcript.get("participants") or {}).get("names") or []) if e.get("name")]
