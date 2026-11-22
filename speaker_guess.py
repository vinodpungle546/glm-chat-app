#!/usr/bin/env python3
"""Guess who said what in a transcript that has no speaker labels, using the local model.

The text goes to the model in parts, and the model rewrites each part as a script
("Name: words"). Small local models do this far better than answering with sentence numbers.
Its script is only used to read off who speaks when: every original sentence is matched to the
script and keeps its original words, so nothing can be lost or reworded. Labels found in earlier
parts are passed on so the same person keeps the same label, and a final rule fixes sentences
that greet or address someone by name (they aren't said by that person).
It's a guess from the text alone: the UI says so, and it can be undone.
"""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from typing import Callable

PART_CHARS = 3000  # about 1k tokens of transcript per model call (and about as much output)
CHARS_PER_TOKEN = 3
MATCH_RATIO = 0.6  # how closely a script line must match an original sentence

_SENTENCE_RE = re.compile(r"(?<=[.!?…])\s+(?=[\"'“‘(\[]?[A-Z0-9À-ÖØ-Þ])")
_SCRIPT_RE = re.compile(r"^\s*[*_`>#•-]*\s*\[?([^:\]\n]{1,40}?)\]?\s*[*_`]*\s*:\s*(.+?)\s*$")
_NOT_LABELS = {"hi", "hello", "hey", "yes", "no", "yeah", "great", "okay", "ok", "thanks", "thank you",
               "so", "well", "sure", "good", "right", "alright", "perfect", "bye", "note", "transcript"}
_GENERIC_RE = re.compile(r"^(?:speaker|person|participant)\s*#?\s*(\d+)$", re.I)

# Words that are capitalised but aren't people (the "never written in lower case" test below
# already removes ordinary words such as "Yeah" or "Okay").
_NOT_PEOPLE = {
    "january", "february", "march", "april", "may", "june", "july", "august", "september",
    "october", "november", "december", "monday", "tuesday", "wednesday", "thursday", "friday",
    "saturday", "sunday", "android", "apple", "ios", "google", "microsoft", "teams", "zoom", "jira",
    "windows", "mac", "christmas", "easter", "ireland", "england", "india", "london", "dublin",
    "europe", "america", "english", "irish", "okay", "hi", "hey", "hello", "thanks", "the",
    "welcome", "congratulations", "congrats", "goodbye", "afternoon", "evening", "great", "perfect",
    "they", "she", "he", "we", "you", "it", "this", "that", "there", "who", "what", "thank", "nope",
    "yep", "yup", "sorry", "please", "cheers", "bye", "morning", "cool", "nice", "oh", "um", "uh",
    "absolutely", "exactly", "actually", "anyway", "anyways", "everyone", "everybody", "guys",
}
_NAME = r"[A-Z][a-z]{1,15}"
_PERSON_CONTEXTS = [
    rf"\b(?:[Hh]i|[Hh]ey|[Hh]ello|[Tt]hanks|[Tt]hank you|[Cc]heers|[Bb]ye|[Mm]orning|[Ww]elcome|[Ss]orry)[,!.]?\s+({_NAME})\b",
    rf"(?:^|[.?!]\s+)({_NAME}),\s",  # "Owen, can you…"
    rf",\s({_NAME})\s*[.?!]",  # "…over to you, Owen."
    rf"\b(?:with|ask|asked|tell|told|ping|email|call|called|cc)\s+({_NAME})\b",
    rf"\b({_NAME})\s+(?:is|was|will|said|says|has|had|joined|joins|mentioned|asked|sent|wants|"
    rf"needs|can|could|would|should)\b",  # "Sandra is still on another call"
    rf"\b(?:I'm|I am|[Mm]y name is)\s+({_NAME})\b",
]


def candidate_names(text: str, limit: int = 15) -> list[str]:
    """People mentioned in the text, most mentioned first (greeted, addressed, talked about).

    A word only counts if it is never written in lower case anywhere in the text, which drops
    sentence starters such as "Yeah" or "Okay"."""
    lower_words = set(re.findall(r"\b[a-z][a-z']*\b", text))
    counts: Counter = Counter()
    for pattern in _PERSON_CONTEXTS:
        for match in re.finditer(pattern, text, re.M):
            counts[match.group(1)] += 1
    return [name for name, _ in counts.most_common()
            if name.lower() not in lower_words and name.lower() not in _NOT_PEOPLE
            and name.lower() not in _NOT_LABELS][:limit]


SYSTEM = ("You turn meeting transcripts without speaker labels into scripts. Reply only with "
          "lines of the form NAME: words, and nothing else.")


def split_sentences(segments: list[dict]) -> list[dict]:
    """Sentences with the start time of the segment they came from."""
    out = []
    for seg in segments:
        for sentence in _SENTENCE_RE.split(" ".join(seg["text"].split())):
            if sentence.strip():
                out.append({"text": sentence.strip(), "start": seg.get("start", 0.0)})
    return out


def plan_parts(sentences: list[dict], part_chars: int = PART_CHARS) -> list[tuple[int, int]]:
    """[start, end) sentence index ranges, each about `part_chars` long."""
    parts, start, size = [], 0, 0
    for i, sentence in enumerate(sentences):
        if size and size + len(sentence["text"]) > part_chars:
            parts.append((start, i))
            start, size = i, 0
        size += len(sentence["text"]) + 1
    if start < len(sentences):
        parts.append((start, len(sentences)))
    return parts


def normalize_label(raw: str) -> str:
    """Tidies a speaker label written by the model: strips quotes and punctuation, turns 'speaker 2' /
    'SPEAKER_02' into 'Speaker 2', and capitalises names ('priya' -> 'Priya').
    """
    label = raw.strip().strip("\"'*`.[]()").strip()
    generic = _GENERIC_RE.match(label)
    if generic:
        return f"Speaker {int(generic.group(1))}"
    return " ".join(w if w.isupper() and len(w) > 1 else w[:1].upper() + w[1:] for w in label.split())


EXAMPLE = (
    "Transcript: Morning all, shall we start? Morning Ana. Yes, let's go. Ana, can you share the "
    "numbers? Sure, thanks Tom. Revenue is up five percent. Great. Ben, anything from sales? Two new "
    "deals this week. Nice one, Ben.\n"
    "Script:\n"
    "Ana: Morning all, shall we start?\n"
    "Tom: Morning Ana. Yes, let's go. Ana, can you share the numbers?\n"
    "Ana: Sure, thanks Tom. Revenue is up five percent.\n"
    "Tom: Great. Ben, anything from sales?\n"
    "Ben: Two new deals this week.\n"
    "Tom: Nice one, Ben.\n"
)


def build_prompt(sentences: list[dict], first: int, last: int, known: list[str],
                 previous: tuple[str, str] | None, people: list[str] | None = None) -> str:
    """The prompt for one part of the text: the rules, a worked example, the names mentioned in the meeting, the
    speakers found in earlier parts and the last line before this part (so the model can continue the same
    people).
    """
    text = " ".join(sentences[i]["text"] for i in range(first, last))
    context = ""
    if people:
        context += (f"- People named in this meeting (not all of them may speak): {', '.join(people)}. "
                    "Use these names as labels whenever the conversation shows who is speaking.\n")
    if known:
        context += f"- Speakers found so far: {', '.join(known)}. Reuse these labels for the same people.\n"
    if previous:
        context += f'- Just before this part, {previous[0]} said: "{previous[1][:200]}"\n'
    return (
        "Turn a meeting transcript into a script. Each line is one speaker turn: the speaker, "
        "a colon, then their exact words.\n\n"
        f"Example\n{EXAMPLE}\n"
        "Rules\n"
        "- Copy the words exactly and in order; never drop or add words.\n"
        "- Someone greeted, thanked or addressed by name (\"Morning Ana\", \"Ana, can you...\") "
        "is not the one saying it; they usually speak just before or just after.\n"
        "- Work names out like in the example: whoever answers \"Ana, can you...\" is Ana; whoever "
        "is thanked or greeted right after speaking is that person.\n"
        "- Only if a person's name never becomes clear, label them Speaker 1, Speaker 2, ... "
        "Different people always get different labels; never use one label for everyone you "
        "are unsure about.\n"
        f"{context}\n"
        f"Transcript: {text}\n"
        "Script:\n"
    )


def _valid_label(raw: str) -> bool:
    """A person's name ("Lauren", "Priya Shah") or "Speaker 2" — checked as the model wrote it,
    so sentence starts such as "Most of it is" or "Will do" are not taken for names."""
    label = raw.strip().strip("\"'*`.[]()").strip()
    if _GENERIC_RE.match(label):
        return True
    words = label.split()
    if not words or len(words) > 3 or label.lower() in _NOT_LABELS | {"name", "speaker"}:
        return False
    return words[0].lower() not in _NOT_LABELS and all(w[:1].isupper() for w in words)


def parse_script(reply: str) -> list[tuple[str, str]]:
    """(speaker, sentence) pairs from the model's script; bad labels join the previous turn."""
    out: list[tuple[str, str]] = []
    for line in reply.splitlines():
        m = _SCRIPT_RE.match(line)
        if m and _valid_label(m.group(1)):
            who, said = normalize_label(m.group(1)), m.group(2).lstrip("*_` ")
        elif line.strip():  # e.g. "Hi everyone: can you hear me?" — the colon isn't a label
            who, said = (out[-1][0] if out else "Speaker 1"), line.strip()
        else:
            continue
        for sentence in split_sentences([{"text": said, "start": 0.0}]):
            out.append((who, sentence["text"]))
    return out


def _similar(a: str, b: str) -> float:
    """How alike two strings are, from 0 (nothing in common) to 1 (identical), ignoring case."""
    import difflib
    return difflib.SequenceMatcher(None, a.lower(), b.lower()).ratio()


def align(sentences: list[dict], script: list[tuple[str, str]], fallback: str) -> list[str]:
    """A label for each original sentence, read off the script in order."""
    labels, j = [], 0
    current = fallback
    for sentence in sentences:
        best, best_k = 0.0, None
        for k in range(j, min(j + 5, len(script))):
            ratio = _similar(sentence["text"], script[k][1])
            if ratio > best:
                best, best_k = ratio, k
        if best_k is not None and best >= MATCH_RATIO:
            current, j = script[best_k][0], best_k + 1
        labels.append(current)
    return labels


def guess(sentences: list[dict], complete: Callable[[list[dict]], str],
          progress: Callable[[int, int], None] | None = None) -> list[str]:
    """Speaker label for every sentence."""
    labels: list[str] = []
    known: list[str] = []
    previous: tuple[str, str] | None = None
    people = candidate_names(" ".join(s["text"] for s in sentences))
    parts = plan_parts(sentences)
    for n, (first, last) in enumerate(parts):
        prompt = build_prompt(sentences, first, last, known, previous, people)
        reply = complete([{"role": "system", "content": SYSTEM}, {"role": "user", "content": prompt}])
        part_labels = align(sentences[first:last], parse_script(reply),
                            previous[0] if previous else "Speaker 1")
        labels.extend(part_labels)
        for label in part_labels:
            if label not in known:
                known.append(label)
        previous = (labels[-1], sentences[last - 1]["text"])
        if progress:
            progress(n + 1, len(parts))
    labels = drop_example_names(labels, " ".join(s["text"] for s in sentences))
    labels = fix_addressed(sentences, labels, people)
    return name_generic_speakers(sentences, labels, people)


EXAMPLE_NAMES = ("Ana", "Tom", "Ben")


def drop_example_names(labels: list[str], text: str) -> list[str]:
    """Names the model copied from the worked example (and that never occur in the
    transcript) become separate "Speaker N" labels."""
    used = {int(m.group(1)) for l in labels if (m := _GENERIC_RE.match(l))}
    rename: dict[str, str] = {}
    for name in EXAMPLE_NAMES:
        if name in labels and not re.search(rf"\b{name}\b", text):
            n = next(i for i in range(1, 100) if i not in used)
            used.add(n)
            rename[name] = f"Speaker {n}"
    return [rename.get(label, label) for label in labels]


_ADDRESS_WORDS = r"(?:hi|hello|hey|thanks|thank you|bye|goodbye|morning|good morning|welcome|sorry)"


def addressed_names(text: str, names: list[str]) -> set[str]:
    """Names a sentence speaks *to*: "Raj, can you…", "Thanks Mei", "Over to you, Lauren."."""
    found = set()
    for name in names:
        first = re.escape(name.split()[0])
        patterns = (rf"^\W*{first}\b\s*[,?!]",  # "Raj, can you…"
                    rf"\b{_ADDRESS_WORDS}\b[\s,]+{first}\b",  # "Thanks Mei"
                    rf",\s*{first}\s*[.?!]*\s*$")  # "…over to you, Lauren."
        if any(re.search(pattern, text, re.I) for pattern in patterns):
            found.add(name)
    return found


def fix_addressed(sentences: list[dict], labels: list[str], people: list[str] | None = None) -> list[str]:
    """A sentence that addresses someone by name isn't said by that person: give it to the
    neighbouring speaker instead (the previous one, else the next one)."""
    names = sorted({label for label in labels if not _GENERIC_RE.match(label)} | set(people or []))
    fixed = list(labels)
    for i, sentence in enumerate(sentences):
        if fixed[i] not in addressed_names(sentence["text"], names):
            continue
        before = fixed[i - 1] if i > 0 else None
        after = labels[i + 1] if i + 1 < len(labels) else None
        replacement = next((c for c in (before, after) if c and c != fixed[i]), None)
        if replacement:
            fixed[i] = replacement
    return fixed


def address_kind(text: str, name: str) -> str | None:
    """How a sentence refers to `name`: "self" (I'm Owen), "ask" (Owen, can you…?),
    "thanks" (thanks Owen), "greet" (hi Owen), or None."""
    n = re.escape(name.split()[0])
    if re.search(rf"\b(?:i'm|i am|my name is)\s+{n}\b|^\W*{n}\s+here\b", text, re.I):
        return "self"
    if re.search(rf"\b(?:thanks|thank you|cheers)\b[\s,]+{n}\b", text, re.I):
        return "thanks"
    if re.search(rf"\b(?:hi|hello|hey|morning|good morning|welcome|bye|goodbye)\b[\s,]+{n}\b", text, re.I):
        return "greet"
    if re.search(rf"^\W*{n}\b\s*[,?]|,\s*{n}\s*[?.!]*\s*$", text, re.I):
        return "ask"
    return None


def _runs(labels: list[str]) -> list[tuple[str, int, int]]:
    """Groups consecutive equal labels: ['A','A','B','A'] -> [('A',0,2), ('B',2,3), ('A',3,4)] as (label, first
    index, end index).
    """
    runs: list[tuple[str, int, int]] = []
    for i, label in enumerate(labels):
        if runs and runs[-1][0] == label:
            runs[-1] = (label, runs[-1][1], i + 1)
        else:
            runs.append((label, i, i + 1))
    return runs


def name_generic_speakers(sentences: list[dict], labels: list[str], people: list[str]) -> list[str]:
    """Give "Speaker N" a real name when the conversation points to it: whoever answers
    "Owen, can you…?" is probably Owen; whoever is thanked or greeted right after speaking
    is probably that person; "I'm Owen" names the speaker. Needs at least two clues."""
    names = list(dict.fromkeys([*people, *(l for l in labels if not _GENERIC_RE.match(l))]))
    runs = _runs(labels)
    votes: dict[str, Counter] = defaultdict(Counter)
    for r, (label, a, b) in enumerate(runs):
        before = runs[r - 1][0] if r > 0 else None
        after = runs[r + 1][0] if r + 1 < len(runs) else None
        for i in range(a, b):
            for name in names:
                kind = address_kind(sentences[i]["text"], name)
                if kind == "self":
                    votes[label][name] += 3
                elif kind == "ask" and after:
                    votes[after][name] += 2
                elif kind == "thanks" and before:
                    votes[before][name] += 2
                elif kind == "greet":
                    for neighbour in (before, after):
                        if neighbour:
                            votes[neighbour][name] += 1
    taken = {l for l in labels if not _GENERIC_RE.match(l)}
    ranked = sorted(((count, label, name) for label, counter in votes.items() if _GENERIC_RE.match(label)
                     for name, count in counter.items()), reverse=True)
    rename: dict[str, str] = {}
    for count, label, name in ranked:
        if count >= 2 and label not in rename and name not in taken:
            rename[label] = name
            taken.add(name)
    return [rename.get(label, label) for label in labels]


def to_segments(sentences: list[dict], labels: list[str], timed: bool) -> tuple[list[dict], dict[str, str]]:
    """Consecutive sentences of one speaker become one segment; ids SPEAKER_00.. by first turn."""
    ids: dict[str, str] = {}
    segments: list[dict] = []
    for sentence, label in zip(sentences, labels):
        sid = ids.setdefault(label, f"SPEAKER_{len(ids):02d}")
        if segments and segments[-1]["speaker"] == sid:
            segments[-1]["text"] += " " + sentence["text"]
            continue
        segments.append({"speaker": sid, "start": sentence["start"] if timed else 0.0,
                         "end": sentence["start"] if timed else 0.0, "text": sentence["text"]})
    for seg, nxt in zip(segments, segments[1:]):
        seg["end"] = max(seg["start"], nxt["start"])
    names = {sid: label for label, sid in ids.items()}  # "Speaker 2" shows as such, not SPEAKER_01
    return segments, names
