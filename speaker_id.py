#!/usr/bin/env python3
"""Who is who in a meeting: likely names and roles for diarized speakers, with confidence.

Evidence, strongest first:
  0. On-screen name: in a video recording, the name on the active speaker's tile while this
     speaker talks (transcriber/screen_names.py). Strong when the same name shows up across
     their turns; weaker when seen once or read by the vision model instead of the text reader.
  1. Voice: the speaker's voice fingerprint (from pyannote) matches a person the user
     confirmed in an earlier meeting (people memory).
  2. Conversation clues: "I'm Owen" names the speaker; whoever answers "Owen, can you…?"
     is probably Owen; whoever is thanked right after speaking ("thanks Owen") too.
  3. The local model reading what each speaker says, given the people mentioned, the people
     known from memory and earlier meetings, and project facts. It also infers a role from
     the content (assigning tasks → lead/manager, reporting test results → QA, …).
  4. Memory (people, including those imported from claude.ai): a person whose remembered
     role matches what the speaker does, or whose projects/team the speaker talks about
     ("Matched to Cian: release manager in memory").
  5. Past conversations (native and imported chats): people who appear in earlier
     conversations about the same topics are likely participants.

The pieces are combined with a noisy-OR into one confidence per label, and names are
assigned one-to-one. Labels the user confirmed are never changed. This local pass always runs on
a local model; when a cloud model is selected, speaker_review.py may then review its result.
"""

from __future__ import annotations

import json
import re
from collections import Counter, defaultdict
from typing import Callable

import people as people_store
import speaker_guess

VOICE_HIGH = 0.70     # same person, confidently (wespeaker cosine across recordings)
VOICE_MEDIUM = 0.55
SAMPLE_CHARS = 650    # what the model reads per speaker
REPLY_TOKENS_MIN = 1800       # the model's JSON answer: never less than this…
REPLY_TOKENS_PER_SPEAKER = 150  # …and room per speaker (a row is ~70-90 tokens; evidence can run longer)
LLM_CAP = 0.45        # the model alone (no voice or clue) never gives more than "low/medium"
MEMORY_ROLE = 0.35    # remembered role matches what the speaker does
MEMORY_TOPIC = 0.15   # speaker talks about the person's project or team
PAST_CONVERSATION = 0.15
SCREEN_STRONG = 0.85    # the same name on the active tile across 3+ of their turns
SCREEN_MEDIUM = 0.7     # seen twice, mostly this name
SCREEN_ONCE = 0.45      # seen once
SCREEN_VISION_CAP = 0.55        # read only by the vision model
SCREEN_SPEAKER_VIEW_CAP = 0.7   # read only from speaker view (no highlight border)
SCREEN_CONFIRM_COUNT = 3        # automatic voice fingerprint: seen at least this often…
SCREEN_CONFIRM_SHARE = 0.7      # …and this share of the speaker's named frames
_ROLE_FILLER = {"the", "a", "an", "of", "and", "team", "senior", "junior", "for", "on", "in"}
# Words many roles share: "Delivery manager" vs "release manager" is not a match.
_GENERIC_ROLE = {"manager", "lead", "leader", "head", "engineer", "developer", "dev", "tester", "analyst", "specialist",
                 "owner", "member", "consultant", "coordinator", "director", "officer", "staff", "principal", "chief"}
LEVELS = ((0.75, "high"), (0.5, "medium"), (0.0, "low"))
# Labels nothing automatic may change: the user's own corrections, and you on your microphone track (a call
# recorded by Quorum keeps your mic separate, so that speaker is certainly you).
FIXED_SOURCES = ("user", "microphone")
MIC_CONFIDENCE = 0.99
YOU = "You"


def microphone_labels(transcript: dict, me: dict | None) -> dict[str, dict]:
    """The confirmed speaker of a Quorum call recording (`confirmed_speakers`): named after the user (their person
    entry in memory) or "You" when the user's name isn't known yet."""
    out = {}
    for spk, info in (transcript.get("confirmed_speakers") or {}).items():
        if (info or {}).get("source") != "microphone":
            continue
        name = (me or {}).get("name") or YOU
        evidence = ["Your microphone (recorded on its own track)"]
        if not (me or {}).get("name"):
            evidence.append("Set your name in Settings → Memory to show it instead of “You”")
        out[spk] = {"name": name, "role": (me or {}).get("role", ""), "confidence": MIC_CONFIDENCE,
                    "level": "high", "evidence": evidence, "source": "microphone"}
    return out
_GENERIC = re.compile(r"^(?:SPEAKER_\d+|UNKNOWN|Speaker \d+)$")


def level(confidence: float) -> str:
    """The confidence level name for a number: 'high', 'medium' or 'low' (see LEVELS)."""
    return next(name for floor, name in LEVELS if confidence >= floor)


def reply_tokens(speakers: int) -> int:
    """The reply limit (max_tokens) for the model's reading of `speakers` speakers. A fixed limit cut the JSON off
    at about 20 speakers, and a cut-off reply can't be parsed, so the model's reading was lost."""
    return max(REPLY_TOKENS_MIN, 400 + REPLY_TOKENS_PER_SPEAKER * speakers)


def speaker_ids(segments: list[dict]) -> list[str]:
    """The speaker labels in the order they first speak, without duplicates."""
    return list(dict.fromkeys(s["speaker"] for s in segments))


# ---------------------------------------------------------------------------
# Evidence
# ---------------------------------------------------------------------------

def voice_matches(embeddings: dict[str, list[float]], known: list[dict]) -> dict[str, tuple[dict, float]]:
    """Best person per speaker by voice (one-to-one, only above VOICE_MEDIUM)."""
    pairs = []
    for spk, vec in (embeddings or {}).items():
        for person in known:
            prints = person.get("voiceprints") or []
            if prints and vec:
                sim = max(people_store.cosine(vec, p["vector"]) for p in prints)
                if sim >= VOICE_MEDIUM:
                    pairs.append((sim, spk, person["name"], person))
    out, used = {}, set()
    for sim, spk, name, person in sorted(pairs, key=lambda p: p[0], reverse=True):
        if spk in out or name in used:
            continue
        out[spk] = (person, sim)
        used.add(name)
    return out


def _fmt(seconds: float) -> str:
    """A time position as 'm:ss', or 'h:mm:ss' after an hour."""
    seconds = int(seconds or 0)
    return f"{seconds // 3600}:{seconds % 3600 // 60:02d}:{seconds % 60:02d}" if seconds >= 3600 \
        else f"{seconds // 60}:{seconds % 60:02d}"


def resolve_screen_name(name: str, truncated: bool, known_names: list[str]) -> str:
    """A label cut short on screen ("Priya Shar…") or a first name maps to the one known full name."""
    lowered = name.lower()
    if truncated:
        full = [n for n in known_names if n.lower().startswith(lowered) and len(n) > len(name)]
    elif " " not in name:
        full = [n for n in known_names if n.split()[0].lower() == lowered and " " in n]
    else:
        full = []
    return full[0] if len(set(full)) == 1 else name


def screen_evidence(transcript: dict, known_names: list[str]) -> dict[str, dict[str, tuple[float, str, dict]]]:
    """{speaker: {name: (probability, why, details)}} from the names seen on the active tile."""
    found = transcript.get("screen_names") or {}
    top: dict[str, list[tuple[int, str]]] = defaultdict(list)  # name → [(count, speaker)]
    rows: dict[str, list[dict]] = {}
    for spk, info in found.items():
        names = []
        for item in (info or {}).get("names") or []:
            name = resolve_screen_name(item["name"], bool(item.get("truncated")), known_names)
            names.append(dict(item, name=name))
        rows[spk] = names
        if names:
            top[names[0]["name"].lower()].append((names[0]["count"], spk))
    out: dict[str, dict[str, tuple[float, str, dict]]] = defaultdict(dict)
    for spk, names in rows.items():
        resolved = max(1, (found[spk] or {}).get("resolved") or sum(n["count"] for n in names))
        for item in names[:3]:
            count, share = item["count"], item["count"] / resolved
            p = SCREEN_STRONG if count >= 3 and share >= 0.7 else SCREEN_MEDIUM if count >= 2 and share >= 0.5 \
                else SCREEN_ONCE if share >= 0.5 else SCREEN_ONCE * 0.6
            text_read = item.get("ocr", 0) + item.get("speaker_view", 0)
            if not text_read:
                p = min(p, SCREEN_VISION_CAP)
            elif not item.get("ocr"):
                p = min(p, SCREEN_SPEAKER_VIEW_CAP)
            owners = top.get(item["name"].lower(), [])
            if len(owners) > 1 and max(owners)[1] != spk:  # the same name was top for another speaker
                p *= 0.6
            first = _fmt((item.get("times") or [0])[0])
            where = ("name on the active tile" if item.get("ocr") else "name under the main video"
                     if item.get("speaker_view") else "name read by the local vision model")
            why = f"{where} at {first} (seen {count}×)"
            out[spk][item["name"]] = (round(p, 3), why, {"count": count, "share": round(share, 2),
                                                         "ocr": item.get("ocr", 0)})
    return out


def screen_confirmed(transcript: dict, known_names: list[str] | None = None) -> dict[str, tuple[str, int]]:
    """Speakers whose on-screen name is consistent enough to save their voice fingerprint
    automatically: seen 3+ times, ≥70% of their named frames, read as text (not only by the
    vision model), and not the top name of another speaker too."""
    evidence = screen_evidence(transcript, known_names or [])
    by_name: dict[str, list[str]] = defaultdict(list)
    best: dict[str, tuple[str, dict]] = {}
    for spk, names in evidence.items():
        if not names:
            continue
        name, (p, _why, info) = max(names.items(), key=lambda kv: kv[1][0])
        best[spk] = (name, info)
        by_name[name.lower()].append(spk)
    out = {}
    for spk, (name, info) in best.items():
        if (info["count"] >= SCREEN_CONFIRM_COUNT and info["share"] >= SCREEN_CONFIRM_SHARE and info["ocr"] >= 2
                and len(by_name[name.lower()]) == 1):
            out[spk] = (name, info["count"])
    return out


def text_clues(segments: list[dict], names: list[str]) -> tuple[dict[str, Counter], dict[str, list[str]]]:
    """Votes per speaker for each name from how the conversation addresses people."""
    votes: dict[str, Counter] = defaultdict(Counter)
    why: dict[str, list[str]] = defaultdict(list)
    for i, seg in enumerate(segments):
        spk = seg["speaker"]
        before = next((segments[j]["speaker"] for j in range(i - 1, -1, -1)
                       if segments[j]["speaker"] != spk), None)
        after = next((segments[j]["speaker"] for j in range(i + 1, len(segments))
                      if segments[j]["speaker"] != spk), None)
        sentences = [x for x in speaker_guess._SENTENCE_RE.split(" ".join(seg["text"].split())) if x]
        for name in names:
            kinds = [k for k in (speaker_guess.address_kind(x, name) for x in sentences) if k]
            kind = "self" if "self" in kinds else (kinds[0] if kinds else None)
            if kind == "self":
                votes[spk][name] += 3
                why[spk].append(f"introduces themselves as {name}")
            elif kind in ("ask", "greet", "thanks"):
                votes[spk][name] -= 2  # people don't address themselves
                target = after if kind == "ask" else before if kind == "thanks" else after
                if target:
                    votes[target][name] += 2 if kind != "greet" else 1
                    verb = {"ask": "answers when", "thanks": "is thanked as",
                            "greet": "speaks after a greeting to"}[kind]
                    why[target].append(f"{verb} {name}" + (" is addressed" if kind == "ask" else ""))
    return votes, why


def clue_probability(score: int) -> float:
    """One clue (score 2) stays "low" (0.43); two agreeing clues reach "medium", three or more "high"."""
    return 0.0 if score <= 0 else 1 - 0.5 ** (score / 2.5)


def speaker_profiles(segments: list[dict], names: list[str]) -> dict[str, dict]:
    """Talk time, turn count and a sample of what each speaker says (name mentions first)."""
    by: dict[str, list[dict]] = defaultdict(list)
    for seg in segments:
        by[seg["speaker"]].append(seg)
    profiles = {}
    name_re = re.compile(r"\b(" + "|".join(map(re.escape, names)) + r")\b") if names else None
    for spk, segs in by.items():
        ranked = sorted(segs, key=lambda s: (not (name_re and name_re.search(s["text"])), -len(s["text"])))
        sample, used = [], 0
        for seg in ranked:
            text = " ".join(seg["text"].split())
            if used + len(text) > SAMPLE_CHARS and sample:
                continue
            sample.append((seg["start"], text[:SAMPLE_CHARS]))
            used += len(text)
            if used >= SAMPLE_CHARS:
                break
        profiles[spk] = {
            "talk_seconds": round(sum(max(0.0, s["end"] - s["start"]) for s in segs)),
            "turns": len(segs),
            "sample": [t for _, t in sorted(sample)],
        }
    return profiles


def _words(text: str) -> set[str]:
    """The meaningful words of a role or text (lowercase, without filler words like 'the' or 'team'), used to
    compare roles.
    """
    return {w for w in re.findall(r"[a-z0-9]+", (text or "").lower()) if w not in _ROLE_FILLER and len(w) > 1}


def memory_matches(ids: list[str], texts: dict[str, str], llm: dict[str, dict], known: list[dict],
                   candidates: set[str]) -> dict[str, dict[str, tuple[list[float], list[str]]]]:
    """{speaker: {name: (probabilities, evidence)}} from remembered roles, teams and projects."""
    out: dict[str, dict[str, tuple[list[float], list[str]]]] = defaultdict(dict)
    for spk in ids:
        said = texts.get(spk, "")
        said_words = _words(said)
        guess_words = _words(llm.get(spk, {}).get("role", ""))
        for person in known:
            if person["name"].lower() not in candidates and person["name"].split()[0].lower() not in candidates:
                continue
            probs, evidence = [], []
            role = person.get("role") or ""
            role_words = _words(role)
            specific = role_words - _GENERIC_ROLE
            if specific:  # at least one distinctive word must match ("backend", "release", "QA")
                matched = bool(specific & guess_words) and len(role_words & guess_words) / len(role_words) >= 0.5 \
                    or role_words <= said_words
            else:         # an all-generic role ("Developer") must match exactly
                matched = bool(role_words) and role_words == guess_words
            if matched:
                probs.append(MEMORY_ROLE)
                where = "claude.ai memory" if person.get("source") else "memory"
                evidence.append(f"Matched to {person['name']}: {role} in {where}")
            topics = [t for t in (person.get("projects") or []) + ([person["team"]] if person.get("team") else [])
                      if t and re.search(rf"\b{re.escape(t)}\b", said, re.I)]
            if topics:
                probs.append(MEMORY_TOPIC)
                evidence.append(f"talks about {topics[0]}, which {person['name']} works on (memory)")
            if probs:
                out[spk][person["name"]] = (probs, evidence)
    return out


# ---------------------------------------------------------------------------
# The model's reading
# ---------------------------------------------------------------------------

SYSTEM = ("You identify the speakers of a meeting transcript. Reply only with JSON, no prose.")


def build_prompt(profiles: dict[str, dict], clues: dict[str, list[str]], mentioned: list[str],
                 known: list[dict], facts: list[str], voice: dict[str, tuple[dict, float]],
                 past: dict[str, list[str]] | None = None) -> str:
    """The prompt for the local model: each speaker's sample of what they said, the clues found in the text,
    names mentioned, people from memory, voice matches and past conversations. The model answers with JSON:
    name, role, confidence and evidence per speaker.
    """
    lines = ["Speakers were separated by voice and are labelled SPEAKER_00, SPEAKER_01, …. "
             "For each speaker, give their likely name and role.", ""]
    if mentioned:
        lines.append("Names mentioned in the meeting: " + ", ".join(mentioned))
    if known:
        lines.append("People known from earlier meetings and memory:")
        for person in known[:30]:
            seen = len(person.get("meetings") or [])
            lines.append(f"- {person['name']}" + (f" — {person['role']}" if person.get("role") else "")
                         + (f", {person['team']} team" if person.get("team") else "")
                         + (f", works on {', '.join(person['projects'][:3])}" if person.get("projects") else "")
                         + (f" (in {seen} earlier meeting{'s' if seen != 1 else ''})" if seen else "")
                         + (f". {person['notes'][:120]}" if person.get("notes") else ""))
    if past:
        lines.append("People who appear in the user's past conversations about these topics: "
                     + ", ".join(f"{n} ({len(t)} conversation{'s' if len(t) != 1 else ''})" for n, t in list(past.items())[:10]))
    if facts:
        lines.append("Project facts: " + " ".join(f"{f}." if not f.endswith(".") else f for f in facts[:12])[:1200])
    lines.append("")
    for spk, prof in profiles.items():
        hint = []
        if spk in voice:
            person, sim = voice[spk]
            hint.append(f"voice matches {person['name']} (similarity {sim:.2f})")
        hint += clues.get(spk, [])[:4]
        lines.append(f"### {spk} · {prof['turns']} turns · {prof['talk_seconds']} s"
                     + (f" · clues: {'; '.join(hint)}" if hint else ""))
        lines.extend(f"- {t}" for t in prof["sample"])
        lines.append("")
    lines += [
        "Rules:",
        "- A name must come from the names mentioned, the known people, or the clues. Never invent one.",
        "- Use null for the name when the transcript doesn't make it clear. Never give two speakers the same name.",
        "- People rarely say their own name; someone addressed by name is usually NOT the one speaking.",
        "- Infer the role from what they say: assigning tasks or running the meeting → lead/manager; "
        "reporting test results or bugs → QA/tester; describing code or fixes → developer; "
        "use a known person's role when the name matches.",
        "- confidence is 0.0-1.0 for the name (0.0 when the name is null).",
        'Reply as {"speakers": [{"id": "SPEAKER_00", "name": "Owen" or null, "role": "QA tester", '
        '"confidence": 0.6, "evidence": "short reason"}]}',
    ]
    return "\n".join(lines)


def parse_reply(reply: str, ids: list[str]) -> dict[str, dict]:
    """Reads the model's JSON answer into {speaker: {name, role, confidence, evidence}}, ignoring unknown
    speakers and malformed rows. {} when the reply has no JSON.
    """
    match = re.search(r"\{.*\}", reply or "", re.DOTALL)
    if not match:
        return {}
    try:
        data = json.loads(match.group(0))
    except json.JSONDecodeError:
        return {}
    rows = data.get("speakers") if isinstance(data, dict) else None
    out = {}
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict) or row.get("id") not in ids:
            continue
        name = row.get("name")
        name = name.strip() if isinstance(name, str) and name.strip().lower() not in ("", "null", "none", "unknown") else None
        try:
            conf = max(0.0, min(1.0, float(row.get("confidence") or 0)))
        except (TypeError, ValueError):
            conf = 0.0
        out[row["id"]] = {"name": name, "role": str(row.get("role") or "").strip()[:60],
                          "confidence": conf, "evidence": str(row.get("evidence") or "").strip()[:200]}
    return out


# ---------------------------------------------------------------------------
# Combine
# ---------------------------------------------------------------------------

def _noisy_or(probs: list[float]) -> float:
    """Combines independent pieces of evidence into one probability.

    Each probability is the chance one clue is right; the result is the chance that at least one is right: 1 -
    (1-p1)(1-p2)…. Two 0.5 clues give 0.75. Values are capped at 0.99 so one clue can never make it certain.
    """
    rest = 1.0
    for p in probs:
        rest *= 1 - max(0.0, min(0.99, p))
    return 1 - rest


def combine(ids: list[str], voice: dict, votes: dict[str, Counter], why: dict[str, list[str]],
            llm: dict[str, dict], allowed_names: set[str], keep: dict[str, dict],
            memory: dict | None = None, past: dict[str, list[str]] | None = None,
            screen: dict | None = None) -> dict[str, dict]:
    """One label per speaker: {name, role, confidence, level, evidence, source}."""
    memory, past, screen = memory or {}, past or {}, screen or {}
    candidates = []  # (p, spk, name, evidence)
    for spk in ids:
        if spk in keep:
            continue
        names = set(votes.get(spk, {})) | ({voice[spk][0]["name"]} if spk in voice else set())
        if llm.get(spk, {}).get("name"):
            names.add(llm[spk]["name"])
        names |= set(memory.get(spk, {}))
        # Every name any source suggests for this speaker becomes a candidate; each is scored below.
        names |= set(screen.get(spk, {}))
        for name in names:
            probs, evidence = [], []
            # Evidence 1: the name was on this speaker's video tile (strongest when seen several times).
            if name in screen.get(spk, {}):
                p_screen, why_screen, _info = screen[spk][name]
                probs.append(p_screen)
                evidence.append(why_screen)
            # Evidence 2: the voice matches a person labelled in an earlier meeting.
            if spk in voice and voice[spk][0]["name"] == name:
                sim = voice[spk][1]
                probs.append(0.9 if sim >= VOICE_HIGH else 0.6)
                evidence.append(f"voice matches {name} from earlier meetings ({sim:.2f})")
            # Evidence 3: clues in the text ('I'm Owen', 'Owen, can you…' answered by this speaker).
            p_clue = clue_probability(votes.get(spk, {}).get(name, 0))
            if p_clue:
                probs.append(p_clue)
                evidence += [w for w in why.get(spk, []) if name in w and not w.startswith("name ")][:3]
            guess = llm.get(spk, {})
            if guess.get("name") == name:
                # Evidence 4: the local model's guess. Capped at LLM_CAP, and weakened further when the name
                # doesn't appear anywhere else, because small models can invent names.
                trusted = name.lower() in allowed_names
                probs.append(min(guess["confidence"], LLM_CAP) * (1.0 if trusted else 0.4))
                if guess.get("evidence"):
                    evidence.append(f"model: {guess['evidence']}")
            if name in memory.get(spk, {}):
                m_probs, m_evidence = memory[spk][name]
                probs += m_probs
                evidence = m_evidence + evidence
            titles = past.get(name) or past.get(name.split()[0]) or []
            if titles and probs:  # only strengthens another signal; it doesn't point at a speaker
                probs.append(PAST_CONVERSATION)
                evidence.append(f"appears in {len(titles)} past conversation{'s' if len(titles) != 1 else ''}"
                                f" about these topics (“{titles[0][:50]}”)")
            # Combine all evidence for (speaker, name); weak candidates below 0.2 are dropped.
            p = _noisy_or(probs)
            if p >= 0.2:
                candidates.append((p, spk, name, evidence))
    labels: dict[str, dict] = {spk: dict(v) for spk, v in keep.items()}
    taken = {v["name"].lower() for v in keep.values() if v.get("name")}
    # Assign names one-to-one, strongest first: each speaker gets one name and each name is used by one speaker
    # only. Labels the user set (`keep`) are never changed.
    for p, spk, name, evidence in sorted(candidates, key=lambda c: c[0], reverse=True):
        if spk in labels or name.lower() in taken:
            continue
        role = llm.get(spk, {}).get("role", "")
        if spk in voice and voice[spk][0]["name"] == name and voice[spk][0].get("role"):
            role = voice[spk][0]["role"]
        remembered = next((m for m in memory.get(spk, {}).get(name, ([], []))[1] if m.startswith("Matched to")), None)
        if remembered:
            role = remembered.split(": ", 1)[1].rsplit(" in ", 1)[0]
        labels[spk] = {"name": name, "role": role, "confidence": round(p, 2), "level": level(p),
                       "evidence": evidence, "source": "auto"}
        taken.add(name.lower())
    for spk in ids:  # no name: keep the role guess
        if spk not in labels:
            guess = llm.get(spk, {})
            labels[spk] = {"name": "", "role": guess.get("role", ""), "confidence": 0.0, "level": "low",
                           "evidence": [f"model: {guess['evidence']}"] if guess.get("evidence") else [],
                           "source": "auto"}
    return labels


# Measured with the synthetic meetings (scripts/benchmark_screen_names.py): the prompt is about
# 300 + 120 tokens per speaker plus memory, the JSON reply about 30 + 60 tokens per speaker.
LOAD_SECONDS = 8.0   # loading a model after the transcriber freed the memory


def estimate_seconds(speakers: int, speeds: dict | None = None, facts_tokens: int = 300,
                     loaded: bool = False) -> dict:
    """{low, high} seconds for the transcript-reading pass (one model call) on a local model."""
    import long_context

    speeds = speeds or {}
    prefill = speeds.get("prefill_tps") or long_context.PREFILL_TPS
    gen = speeds.get("gen_tps") or long_context.GEN_TPS
    n = max(1, speakers)
    seconds = (300 + 120 * n + facts_tokens) / prefill + (30 + 60 * n) / gen + 0.5
    seconds += 0 if loaded else LOAD_SECONDS
    return {"low": round(seconds * 0.7), "high": round(seconds * 1.4 + 2)}


def identify(transcript: dict, known: list[dict], facts: list[str], complete: Callable[[list[dict]], str] | None,
             keep: dict[str, dict] | None = None, prior_names: list[str] | None = None,
             progress: Callable[[str], None] | None = None,
             past: dict[str, list[str]] | None = None, me: dict | None = None) -> dict[str, dict]:
    """Labels for every speaker in `transcript`. `keep` = user-confirmed labels (unchanged).
    `past` = {name: [conversation titles]} for people found in related past conversations.
    `me` = the user's person entry: names the confirmed microphone speaker of a call Quorum recorded (a correction
    by the user still wins)."""
    segments = [s for s in transcript.get("segments", []) if s.get("text")]
    ids = speaker_ids(segments)
    # Your corrections, and names read from another transcript of the meeting (e.g. Teams' own), are kept.
    keep = {k: v for k, v in (keep or {}).items() if k in ids and v.get("source") in ("user", "reference")}
    mic = {k: v for k, v in microphone_labels(transcript, me).items() if k in ids and k not in keep}
    if mic and any(v.get("name", "").lower() == next(iter(mic.values()))["name"].lower() for v in keep.values()):
        mic = {}  # the user gave their own name to another speaker: their correction wins
    keep = {**mic, **keep}
    text = " ".join(s["text"] for s in segments)
    mentioned = speaker_guess.candidate_names(text, limit=25)
    known_names = [p["name"] for p in known] + [a for p in known for a in p.get("aliases", [])]
    in_text = [n for n in dict.fromkeys(known_names + (prior_names or [])) if re.search(rf"\b{re.escape(n.split()[0])}\b", text)]
    past = past or {}
    on_screen = [item["name"] for info in (transcript.get("screen_names") or {}).values()
                 for item in (info or {}).get("names", [])[:2] if not item.get("truncated")]
    names = list(dict.fromkeys(mentioned + in_text + [n for n in past if n in known_names] + on_screen))
    if progress:
        progress("Matching voices with people you labelled before…")
    voice = voice_matches(transcript.get("speaker_embeddings") or {}, known)
    votes, why = text_clues(segments, names)
    screen = screen_evidence(transcript, known_names)
    for spk, found in screen.items():  # the model sees it as a clue too
        if found:
            name, (_p, seen, _info) = max(found.items(), key=lambda kv: kv[1][0])
            why[spk].insert(0, f"{seen.split(' at ')[0]}: {name}")
    llm: dict[str, dict] = {}
    if complete is not None:
        if progress:
            progress("Reading what each speaker says…")
        profiles = speaker_profiles(segments, names)
        prompt = build_prompt(profiles, why, names, known, facts, voice, past)
        reply = complete([{"role": "system", "content": SYSTEM}, {"role": "user", "content": prompt}])
        llm = parse_reply(reply, ids)
        for guess in llm.values():  # "Priya" from the model is the one known "Priya Shah"
            name = guess.get("name")
            if name and " " not in name and name.lower() not in {n.lower() for n in known_names}:
                full = [p["name"] for p in known if p["name"].split()[0].lower() == name.lower()]
                if len(full) == 1:
                    guess["name"] = full[0]
    allowed = {n.lower() for n in names} | {n.lower() for n in known_names} \
        | {n.lower() for spk in screen.values() for n in spk}
    texts: dict[str, str] = defaultdict(str)
    for seg in segments:
        texts[seg["speaker"]] += " " + seg["text"]
    candidates = {n.lower() for n in names} | {n.split()[0].lower() for n in names}
    memory = memory_matches(ids, texts, llm, known, candidates)
    return combine(ids, voice, votes, why, llm, allowed, keep, memory, past, screen)
