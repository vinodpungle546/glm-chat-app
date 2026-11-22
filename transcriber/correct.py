"""Correct a meeting transcript with other transcripts of the same meeting (pure code, no model).

References are other transcripts of the meeting: the Voice Memos recording (its audio, transcribed by Quorum's
Whisper, with word times) and/or its text, a claude.ai voice transcript, or any other text (VTT, `Name: text`…).
Any combination may exist, including none.

How a correction is made (always from the ORIGINAL transcript, so adding, removing or reverting is repeatable):

1. Every transcript becomes a list of words; `norm` compares them (lower case, no punctuation, number words as
   digits).
2. Each reference is lined up with Quorum's words: 3-word sequences found exactly once in both are anchors; the
   longest chain of anchors in order is kept, and the gaps between anchors are lined up word by word (difflib).
   A timed reference also gets a time offset and drift. A reference that matches too little isn't applied.
3. Votes: Quorum's word is one vote; references are grouped by the recording they come from (Voice Memos text and
   audio are one recording: one vote where they agree, none where they don't); the claude.ai transcript is another.
   The majority wins. A tie keeps Quorum's word, except that a known name or term beats an unknown spelling, and a
   word Whisper was unsure of (probability < 0.5) loses. With a single reference, its wording wins a tie where it
   lines up well (≥ 60% of the surrounding words agree), unless Whisper was very sure (≥ 0.9). Words are removed only
   when two references agree, or when they're a known Whisper invention ("Thank you.") a covering reference lacks.
4. Speakers: corrected words keep the speaker of the words they replace. References with speaker names suggest a
   name for each Quorum speaker they mostly match, and move turn boundaries that are off by a few words. The
   microphone speaker (your own track) is never moved.
"""

from __future__ import annotations

import bisect
import difflib
import hashlib
import re
import statistics
from collections import Counter, defaultdict

GRAM = 3                    # words per anchor
MIN_COVERAGE = 0.3          # share of Quorum's words (in the covered part) a reference must agree with
MIN_ANCHORS = 3             # and at least this many anchors (more for long meetings, see min_anchors)
WELL_ALIGNED = 0.6          # a single reference's wording wins a tie where this share of nearby words agree
NEAR = 5                    # words on each side for "nearby"
EDGE = 40                   # words before the first / after the last anchor lined up too
EDGE_RATIO = 0.6            # when they match at least this well
SURE = 0.9                  # Whisper probability above which a single reference never overrides
UNSURE = 0.5                # below this, a reference word wins a tie
REF_SURE = 0.8              # a recording's word must be at least this sure to win a tie on its own
SURER_BY = 0.15             # and this much surer than Quorum's word
MAX_SINGLE_INSERT = 3       # words a single reference may add in one place
NAME_SHARE = 0.7            # a speaker gets a name when this share of their matched words carry it
NAME_MIN_WORDS = 8
TURN_WINDOW = 8             # words around a turn boundary looked at for moving it
TURN_MAX_SHIFT = 5
GENERIC_NAMES = re.compile(r"^(user|human|claude|assistant|ai|me|speaker\s*\d*|unknown|others?|you|narrator)$", re.I)
HALLUCINATIONS = {"thank you", "thanks for watching", "thank you for watching", "thank you very much",
                  "please subscribe", "bye", "you"}

_UNITS = ("zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen "
          "sixteen seventeen eighteen nineteen").split()
_TENS = {"twenty": 20, "thirty": 30, "forty": 40, "fifty": 50, "sixty": 60, "seventy": 70, "eighty": 80,
         "ninety": 90, "hundred": 100}
_ORDINALS = ("first second third fourth fifth sixth seventh eighth ninth tenth eleventh twelfth thirteenth "
             "fourteenth fifteenth sixteenth seventeenth eighteenth nineteenth twentieth").split()
NUMBER_WORDS = {w: str(i) for i, w in enumerate(_UNITS)} | {w: str(n) for w, n in _TENS.items()}
NUMBER_WORDS |= {w: f"{i + 1}{'st' if i + 1 in (1,) else 'nd' if i + 1 == 2 else 'rd' if i + 1 == 3 else 'th'}"
                 for i, w in enumerate(_ORDINALS)}
NUMBER_WORDS |= {"thirtieth": "30th", "thirty-first": "31st", "twenty-first": "21st", "twenty-second": "22nd",
                 "twenty-third": "23rd"}
_STRIP = "\"'“”‘’.,;:!?()[]{}…–—-*_/\\<>"


# ---------------------------------------------------------------------------
# Words
# ---------------------------------------------------------------------------

def norm(token: str) -> str:
    """How words are compared: lower case, no surrounding punctuation, '1,000' = '1000', 'fourteenth' = '14th'."""
    word = token.strip().strip(_STRIP).lower().replace("’", "'")
    word = re.sub(r"(?<=\d),(?=\d{3})", "", word)
    return NUMBER_WORDS.get(word, word)


def squash(norms) -> str:
    """Letters and digits only: 'go-live' = 'go live', so spacing and hyphens never count as corrections."""
    return re.sub(r"[^0-9a-z]+", "", "".join(norms))


def quorum_tokens(segments: list[dict], words: list[dict] | None = None) -> list[dict]:
    """Quorum's words in order: {seg, text, norm, t, p}. Times come from the saved Whisper words when there are
    (with their probability `p`), else they are spread over the segment by length."""
    by_time = []
    for w in sorted(words or [], key=lambda w: w.get("start", 0)):
        if by_time and not w.get("word", " ")[:1].isspace():  # a piece of the previous word ("go" "-no" "-go")
            prev = by_time[-1]
            ps = [x for x in (prev.get("p"), w.get("p")) if x is not None]
            by_time[-1] = dict(prev, word=prev["word"] + w["word"], end=w.get("end", prev.get("end")),
                               p=min(ps) if ps else None)
        else:
            by_time.append(dict(w))
    starts = [w.get("start", 0) for w in by_time]
    tokens = []
    for si, seg in enumerate(segments):
        parts = seg.get("text", "").split()
        total = sum(len(p) + 1 for p in parts) or 1
        span = max(0.0, float(seg.get("end", 0)) - float(seg.get("start", 0)))
        lo = bisect.bisect_left(starts, float(seg.get("start", 0)) - 0.5)
        hi = bisect.bisect_right(starts, float(seg.get("end", 0)) + 0.5)
        pool = by_time[lo:hi]
        used = 0
        done = 0
        for part in parts:
            n = norm(part)
            t = float(seg.get("start", 0)) + span * done / total
            p = None
            for k in range(used, min(len(pool), used + 6)):  # the next saved word with the same spelling
                if norm(pool[k].get("word", "")) == n:
                    t = float(pool[k]["start"])
                    p = pool[k].get("p", pool[k].get("probability"))
                    used = k + 1
                    break
            tokens.append({"seg": si, "text": part, "norm": n, "t": round(t, 2), "p": p})
            done += len(part) + 1
    return tokens


def reference_tokens(ref: dict) -> list[dict]:
    """A reference's words: {text, norm, t (seconds or None), name (speaker name or None)}."""
    if ref.get("words") is not None:  # audio, transcribed by Whisper
        tokens = []
        for w in ref["words"]:
            word = w.get("word", "")
            if not word.strip():
                continue
            if tokens and not word[:1].isspace():  # a piece of the previous word ("go" "-no" "-go")
                last = tokens[-1]
                last["text"] += word.strip()
                last["norm"] = norm(last["text"])
                last["p"] = min(x for x in (last["p"], w.get("p")) if x is not None) if (last["p"] is not None or w.get("p") is not None) else None
                continue
            tokens.append({"text": word.strip(), "norm": norm(word), "t": float(w["start"]), "name": None,
                           "p": w.get("p")})
        return tokens
    tokens = []
    timed = bool(ref.get("timed"))
    for seg in ref.get("segments") or []:
        name = (seg.get("speaker") or "").strip() or None
        if name and GENERIC_NAMES.match(name):
            name = None
        for part in seg.get("text", "").split():
            tokens.append({"text": part, "norm": norm(part), "t": float(seg["start"]) if timed else None,
                           "name": name})
    return tokens


# ---------------------------------------------------------------------------
# Lining up
# ---------------------------------------------------------------------------

def _grams(seq: list[str]) -> dict[tuple, list[int]]:
    found: dict[tuple, list[int]] = defaultdict(list)
    for i in range(len(seq) - GRAM + 1):
        found[tuple(seq[i:i + GRAM])].append(i)
    return found


def _longest_chain(pairs: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """The longest list of (q, r) pairs increasing in both (pairs sorted by q)."""
    tails: list[int] = []
    tail_idx: list[int] = []
    prev = [-1] * len(pairs)
    for i, (_q, r) in enumerate(pairs):
        k = bisect.bisect_left(tails, r)
        if k == len(tails):
            tails.append(r)
            tail_idx.append(i)
        else:
            tails[k] = r
            tail_idx[k] = i
        prev[i] = tail_idx[k - 1] if k else -1
    chain = []
    i = tail_idx[-1] if tail_idx else -1
    while i >= 0:
        chain.append(pairs[i])
        i = prev[i]
    return chain[::-1]


def min_anchors(n_words: int) -> int:
    return max(MIN_ANCHORS, min(20, n_words // 40))


def fit_time(points: list[tuple[float, float]]) -> tuple[float, float] | None:
    """Robust line q_time = slope * r_time + offset through (r_time, q_time) points (Theil-Sen on a sample)."""
    if len(points) < 4:
        return None
    pts = sorted(points)
    step = max(1, len(pts) // 60)
    sample = pts[::step]
    slopes = [(q2 - q1) / (r2 - r1) for i, (r1, q1) in enumerate(sample) for (r2, q2) in sample[i + 1:]
              if r2 - r1 > 5]
    slope = statistics.median(slopes) if slopes else 1.0
    if not 0.9 < slope < 1.1:
        slope = 1.0
    offset = statistics.median(q - slope * r for r, q in pts)
    return slope, offset


def align(q: list[dict], r: list[dict]) -> dict:
    """Lines up a reference with Quorum's words. Returns {ops: [(tag, q1, q2, r1, r2)] over token indices,
    covered: (first, last) Quorum token range, coverage, anchors, offset, drift}."""
    qi = [i for i, t in enumerate(q) if t["norm"]]
    ri = [i for i, t in enumerate(r) if t["norm"]]
    qs = [q[i]["norm"] for i in qi]
    rs = [r[i]["norm"] for i in ri]
    qg, rg = _grams(qs), _grams(rs)
    pairs = sorted((qp[0], rp[0]) for g, qp in qg.items() if len(qp) == 1 and len(rg.get(g, ())) == 1
                   for rp in [rg[g]])
    chain = _longest_chain(pairs)
    timing = None
    timed = [(r[ri[b]]["t"], q[qi[a]]["t"]) for a, b in chain if r[ri[b]]["t"] is not None]
    if len(timed) >= 8:
        timing = fit_time(timed)
        if timing:  # drop anchors far off the fitted line (a repeated phrase in the wrong place)
            slope, offset = timing
            chain = [(a, b) for a, b in chain
                     if r[ri[b]]["t"] is None or abs(q[qi[a]]["t"] - (slope * r[ri[b]]["t"] + offset)) <= 20]
    result = {"ops": [], "covered": None, "coverage": 0.0, "anchors": len(chain), "offset": None, "drift": None,
              "share": 0.0}
    if timing:
        result["offset"], result["drift"] = round(timing[1], 1), round((timing[0] - 1) * 1e6)
    if not chain:
        return result
    kept = []
    for a, b in chain:  # anchors that don't overlap the previous one
        if not kept or (a >= kept[-1][0] + GRAM and b >= kept[-1][1] + GRAM):
            kept.append((a, b))
    ops = []
    # Before the first anchor and after the last one: line up the few words there too, when they match well
    # (the first and last words of a meeting would otherwise never be corrected).
    # The anchor's own words are part of the window, so a single word there is judged with its context.
    head_q, head_r = kept[0]
    kq, kr = min(head_q, EDGE), min(head_r, EDGE)
    if kq and kr:
        matcher = difflib.SequenceMatcher(None, qs[head_q - kq:head_q + GRAM], rs[head_r - kr:head_r + GRAM],
                                          autojunk=False)
        if matcher.ratio() >= EDGE_RATIO:
            ops += [(tag, head_q - kq + i1, head_q - kq + i2, head_r - kr + j1, head_r - kr + j2)
                    for tag, i1, i2, j1, j2 in matcher.get_opcodes() if head_q - kq + i1 < head_q]
    for k, (a, b) in enumerate(kept):
        ops.append(("equal", a, a + GRAM, b, b + GRAM))
        if k + 1 < len(kept):
            na, nb = kept[k + 1]
            qa, ra = a + GRAM, b + GRAM
            if na - qa > 400 or nb - ra > 400:  # a long stretch without anchors: don't guess inside it
                continue
            matcher = difflib.SequenceMatcher(None, qs[qa:na], rs[ra:nb], autojunk=False)
            for tag, i1, i2, j1, j2 in matcher.get_opcodes():
                ops.append((tag, qa + i1, qa + i2, ra + j1, ra + j2))
    tail_q, tail_r = kept[-1][0], kept[-1][1]  # from the last anchor's first word
    kq, kr = min(len(qs) - tail_q - GRAM, EDGE), min(len(rs) - tail_r - GRAM, EDGE)
    if kq > 0 and kr > 0:
        matcher = difflib.SequenceMatcher(None, qs[tail_q:tail_q + GRAM + kq], rs[tail_r:tail_r + GRAM + kr],
                                          autojunk=False)
        if matcher.ratio() >= EDGE_RATIO:
            ops += [(tag, tail_q + i1, tail_q + i2, tail_r + j1, tail_r + j2)
                    for tag, i1, i2, j1, j2 in matcher.get_opcodes() if tail_q + i2 > tail_q + GRAM]
    # back to token indices (punctuation-only tokens are skipped by the comparison)
    def qx(i): return qi[i] if i < len(qi) else len(q)
    def rx(j): return ri[j] if j < len(ri) else len(r)
    out = []
    for tag, i1, i2, j1, j2 in ops:
        if tag == "replace" and squash(qs[i1:i2]) == squash(rs[j1:j2]):
            tag = "equal"
        out.append((tag, qx(i1), qx(i2 - 1) + 1 if i2 > i1 else qx(i1), rx(j1), rx(j2 - 1) + 1 if j2 > j1 else rx(j1)))
    first, last = min(o[1] for o in out), max(o[2] for o in out)
    words = sum(1 for i in range(first, last) if q[i]["norm"])
    equal = sum(sum(1 for i in range(o[1], o[2]) if q[i]["norm"]) for o in out if o[0] == "equal")
    result.update(ops=out, covered=(first, last), coverage=round(equal / words, 3) if words else 0.0,
                  share=round(words / max(1, sum(1 for t in q if t["norm"])), 3))
    return result


# ---------------------------------------------------------------------------
# Voting
# ---------------------------------------------------------------------------

def _proposals(q, r, aligned) -> tuple[dict, set, list[bool]]:
    """One reference's view: {(q1, q2): (ref token indices)} alternatives, the set of positions it agrees with,
    and `covered` per Quorum token."""
    alts: dict[tuple[int, int], tuple[int, ...]] = {}
    agree: set[int] = set()
    covered = [False] * len(q)
    if aligned["covered"]:
        for i in range(*aligned["covered"]):
            covered[i] = True
    for tag, q1, q2, r1, r2 in aligned["ops"]:
        if tag == "equal":
            agree.update(range(q1, q2))
        elif tag == "replace" and q2 - q1 == r2 - r1:  # word for word: vote per word
            for k in range(q2 - q1):
                alts[(q1 + k, q1 + k + 1)] = (r1 + k,)
        else:
            alts[(q1, q2)] = tuple(range(r1, r2))
    return alts, agree, covered


def _well_aligned(agree: set[int], covered: list[bool], q1: int, q2: int) -> bool:
    lo, hi = max(0, q1 - NEAR), min(len(covered), q2 + NEAR)
    span = [i for i in range(lo, hi) if covered[i] and not (q1 <= i < q2)]
    return bool(span) and sum(1 for i in span if i in agree) / len(span) >= WELL_ALIGNED


def _restyle(new: str, old: str) -> str:
    """The reference's word with Quorum's surrounding punctuation and sentence-start capital."""
    core = new.strip(_STRIP) or new
    lead = old[: len(old) - len(old.lstrip(_STRIP))]
    trail = old[len(old.rstrip(_STRIP)):]
    if old.strip(_STRIP)[:1].isupper() and core[:1].islower() and not lead:
        core = core[:1].upper() + core[1:]
    return lead + core + trail


def change_id(kind: str, q1: int, q2: int, to: str) -> str:
    return hashlib.sha1(f"{kind}|{q1}|{q2}|{to}".encode()).hexdigest()[:10]


def correct(segments: list[dict], references: list[dict], words: list[dict] | None = None,
            known_terms: list[str] | None = None, fixed_speakers: set[str] | None = None,
            reverted: set[str] | None = None) -> dict:
    """The corrected transcript. `references`: [{id, name, recording, words | segments+timed}]. Returns
    {segments, changes, refs, marks, names, stats}."""
    reverted = reverted or set()
    fixed_speakers = fixed_speakers or set()
    known = {norm(w) for term in (known_terms or []) for w in term.split() if len(norm(w)) >= 3}
    q = quorum_tokens(segments, words)
    refs_out, groups = [], defaultdict(list)
    for ref in references:
        r = reference_tokens(ref)
        aligned = align(q, r)
        applied = aligned["coverage"] >= MIN_COVERAGE and aligned["anchors"] >= min_anchors(len(q))
        refs_out.append({"id": ref["id"], "name": ref.get("name", ""), "source": ref.get("source", "other"),
                         "kind": "audio" if ref.get("words") is not None else "text",
                         "coverage": aligned["coverage"], "share": aligned["share"], "anchors": aligned["anchors"],
                         "offset": aligned["offset"], "drift": aligned["drift"], "applied": applied,
                         "reason": None if applied else "doesn't match this meeting"})
        if applied:
            alts, agree, covered = _proposals(q, r, aligned)
            groups[ref.get("recording") or ref["id"]].append(
                {"ref": ref, "r": r, "alts": alts, "agree": agree, "covered": covered, "aligned": aligned})

    # Each group's vote per place: keep, an alternative, or nothing (not covered / its references disagree).
    keys = sorted({k for views in groups.values() for v in views for k in v["alts"]})
    decided = []
    for key in keys:
        q1, q2 = key
        span = range(q1, q2) if q2 > q1 else range(max(0, q1 - 1), min(len(q), q1 + 1))
        votes: Counter = Counter()
        sources: dict[tuple, list[str]] = defaultdict(list)
        display: dict[tuple, list[str]] = {}
        keep, single_ok, groups_here = 1.0, False, 0
        alt_p: dict[tuple, float] = {}  # how sure Whisper was of a reference's words (recordings only)
        for gname, views in groups.items():
            here = [v for v in views if all(v["covered"][i] for i in span)]
            if not here:
                continue
            groups_here += 1
            choices = set()
            for v in here:
                if key in v["alts"]:
                    alt = tuple(v["r"][j]["norm"] for j in v["alts"][key])
                    choices.add(alt)
                    display[alt] = [v["r"][j]["text"] for j in v["alts"][key]]
                elif q2 > q1 and all(i in v["agree"] for i in span):
                    choices.add("keep")
                elif q2 == q1:
                    choices.add("keep")  # no insertion here
                else:
                    choices.add(None)  # it changes this place differently
            if len(choices) != 1 or None in choices:
                continue  # the group's own references disagree: no vote
            choice = choices.pop()
            if choice == "keep":
                keep += 1
            else:
                votes[choice] += 1
                sources[choice] += [v["ref"].get("name") or v["ref"]["id"] for v in here]
                ps = [v["r"][j]["p"] for v in here if key in v["alts"] for j in v["alts"][key]
                      if v["r"][j].get("p") is not None]
                if ps:
                    alt_p[choice] = max(alt_p.get(choice, 0.0), min(ps))
                if any(_well_aligned(v["agree"], v["covered"], q1, max(q2, q1 + 1)) for v in here):
                    single_ok = True
        if not votes:
            continue
        alt, n = votes.most_common(1)[0]
        old = [q[i] for i in range(q1, q2)]
        old_norm = tuple(t["norm"] for t in old)
        p = min((t["p"] for t in old if t["p"] is not None), default=None)
        score = float(n)
        if len(alt) == 1 and len(old) == 1:
            if alt[0] in known and old_norm[0] not in known:
                score += 0.5
            elif old_norm[0] in known and alt[0] not in known:
                keep += 0.5
        if not alt:  # removing words
            invented = " ".join(old_norm) in HALLUCINATIONS
            ok = (n >= 2 and score > keep) or invented
        elif q1 == q2:  # adding words
            ok = score > keep or (groups_here == 1 and single_ok and len(alt) <= MAX_SINGLE_INSERT)
        elif score > keep:
            ok = True
        elif score == keep:
            if groups_here == 1 and alt in alt_p:
                # A recording transcribed by Whisper too: its word wins only where Whisper was clearly surer of it
                # (a noisier memo shouldn't undo the call's better hearing).
                ok = single_ok and alt_p[alt] >= REF_SURE and (p is None or alt_p[alt] > p + SURER_BY)
            elif groups_here == 1:
                ok = single_ok and not (p is not None and p >= SURE)
            else:
                ok = p is not None and p < UNSURE
        else:
            ok = False
        if ok:
            decided.append((q1, q2, alt, display[alt], sorted(set(sources[alt])), score - keep))

    # Apply, best first, never two edits on the same words.
    decided.sort(key=lambda d: (-d[5], d[0]))
    taken: set[int] = set()
    edits, skipped = [], []
    for q1, q2, alt, shown, srcs, _margin in decided:
        positions = set(range(q1, q2)) if q2 > q1 else {-(q1 + 1)}
        if positions & taken:
            continue
        kind = "delete" if not alt else "insert" if q1 == q2 else "word"
        to = " ".join(shown)
        cid = change_id(kind, q1, q2, " ".join(alt))
        if cid in reverted:
            skipped.append({"id": cid, "kind": kind, "q1": q1, "q2": q2, "to": to, "sources": srcs})
            continue
        taken |= positions
        edits.append({"id": cid, "kind": kind, "q1": q1, "q2": q2, "to": to, "sources": srcs})
    edits.sort(key=lambda e: (e["q1"], e["q2"]))

    # New words per Quorum token (and insertions before it).
    new_text: dict[int, str | None] = {}
    cid_at: dict[int, str] = {}
    inserts: dict[int, list[tuple[str, str]]] = defaultdict(list)
    for e in edits:
        if e["kind"] == "insert":
            inserts[e["q1"]].append((e["to"], e["id"]))
            continue
        shown = e["to"].split()
        n_old = e["q2"] - e["q1"]
        for k, i in enumerate(range(e["q1"], e["q2"])):
            cid_at[i] = e["id"]
            if k >= len(shown):
                new_text[i] = None
            elif k == n_old - 1:  # the last old word takes any extra new words
                new_text[i] = _restyle(" ".join(shown[k:]), q[i]["text"])
            else:
                new_text[i] = _restyle(shown[k], q[i]["text"])

    # Speakers: names from references with names, and turn boundaries off by a few words.
    owner = [segments[t["seg"]].get("speaker") for t in q]
    tally: dict[str, Counter] = defaultdict(Counter)
    name_at: dict[int, str] = {}
    for views in groups.values():
        for v in views:
            for tag, q1, q2, r1, r2 in v["aligned"]["ops"]:
                if tag == "equal" or (tag == "replace" and q2 - q1 == r2 - r1):
                    for k in range(q2 - q1):
                        name = v["r"][r1 + k]["name"]
                        if name:
                            tally[owner[q1 + k]][name] += 1
                            name_at[q1 + k] = name
    names = {}
    for spk, counts in tally.items():
        name, n = counts.most_common(1)[0]
        total = sum(counts.values())
        if spk and total >= NAME_MIN_WORDS and n / total >= NAME_SHARE:
            names[spk] = {"name": name, "share": round(n / total, 2), "words": total}

    seg_of = [t["seg"] for t in q]
    moves = []
    if names:
        for b in range(1, len(q)):
            a_seg, b_seg = seg_of[b - 1], seg_of[b]
            if a_seg == b_seg:
                continue
            sa, sb = segments[a_seg].get("speaker"), segments[b_seg].get("speaker")
            if sa == sb or sa in fixed_speakers or sb in fixed_speakers or sa not in names or sb not in names:
                continue
            na, nb = names[sa]["name"], names[sb]["name"]
            if na == nb:
                continue
            lo = b
            while lo > 0 and b - lo < TURN_WINDOW and seg_of[lo - 1] == a_seg:
                lo -= 1
            hi = b
            while hi < len(q) and hi - b < TURN_WINDOW and seg_of[hi] == b_seg:
                hi += 1
            def cost(split):
                return sum(1 for i in range(lo, hi) if i in name_at and name_at[i] != (na if i < split else nb))
            best = min(range(lo, hi + 1), key=lambda s: (cost(s), abs(s - b)))
            if best != b and abs(best - b) <= TURN_MAX_SHIFT and cost(b) - cost(best) >= 2:
                cid = change_id("turn", min(best, b), max(best, b), f"{sa}>{sb}")
                if cid in reverted:
                    skipped.append({"id": cid, "kind": "turn", "q1": min(best, b), "q2": max(best, b),
                                    "to": nb if best < b else na, "sources": []})
                else:
                    moves.append({"id": cid, "kind": "turn", "q1": min(best, b), "q2": max(best, b),
                                  "to": nb if best < b else na, "boundary": b, "split": best,
                                  "from_seg": a_seg, "to_seg": b_seg})
    for m in moves:
        for i in range(m["q1"], m["q2"]):
            seg_of[i] = m["to_seg"] if m["split"] < m["boundary"] else m["from_seg"]

    # Rebuild the segments (same order, speakers and times; empty ones dropped) and where each change landed.
    built: dict[int, list[tuple[str, str | None]]] = defaultdict(list)
    times: dict[int, list[float]] = defaultdict(list)
    for i, t in enumerate(q):
        for text, cid in inserts.get(i, []):
            target = seg_of[i - 1] if i > 0 else seg_of[i]
            built[target].append((text, cid))
        text = new_text[i] if i in new_text else t["text"]
        if text is None:
            continue
        built[seg_of[i]].append((text, cid_at.get(i)))
        times[seg_of[i]].append(t["t"])
    for text, cid in inserts.get(len(q), []):
        built[seg_of[-1]].append((text, cid))
    out, marks, new_index = [], {}, {}
    for si, seg in enumerate(segments):
        pieces = built.get(si, [])
        if not pieces:
            continue
        text, spans = "", []
        for word, cid in pieces:
            if text:
                text += " "
            start = len(text)
            text += word
            if cid:
                spans.append([start, len(text), cid])
        new_seg = dict(seg, text=text)
        moved = [m for m in moves if si in (m["from_seg"], m["to_seg"])]
        if moved and times.get(si):
            new_seg["start"] = round(min(times[si]), 2) if any(m["to_seg"] == si and m["split"] < m["boundary"] for m in moved) else seg["start"]
            new_seg["end"] = round(max(max(times[si]) + 0.3, new_seg["start"]), 2) if any(m["from_seg"] == si and m["split"] < m["boundary"] for m in moved) else seg["end"]
        new_index[si] = len(out)
        if spans:
            marks[str(len(out))] = spans
        out.append(new_seg)

    changes = []
    for e in edits:
        old = " ".join(q[i]["text"] for i in range(e["q1"], e["q2"]))
        seg = seg_of[e["q1"]] if e["q1"] < len(q) else seg_of[-1]
        if e["kind"] == "insert" and e["q1"] > 0:
            seg = seg_of[e["q1"] - 1]
        changes.append({"id": e["id"], "kind": e["kind"], "from": old, "to": e["to"], "sources": e["sources"],
                        "seg": new_index.get(seg), "t": q[min(e["q1"], len(q) - 1)]["t"] if q else 0,
                        "name_or_number": bool(re.search(r"\d|^[A-Z]", e["to"]) or re.search(r"\d", old))})
    for m in moves:
        changes.append({"id": m["id"], "kind": "turn", "from": " ".join(q[i]["text"] for i in range(m["q1"], m["q2"])),
                        "to": m["to"], "sources": [], "seg": new_index.get(m["to_seg"]), "t": q[m["q1"]]["t"],
                        "name_or_number": False})
    for e in skipped:  # reverted by you: listed so they can be brought back
        changes.append({"id": e["id"], "kind": e["kind"], "from": " ".join(q[i]["text"] for i in range(e["q1"], e["q2"])),
                        "to": e["to"], "sources": e["sources"], "seg": None, "reverted": True,
                        "t": q[min(e["q1"], len(q) - 1)]["t"] if q else 0, "name_or_number": False})
    changes.sort(key=lambda c: c["t"])
    words_changed = sum(max(1, len(c["from"].split())) if c["kind"] == "word" else
                        len(c["to"].split()) if c["kind"] == "insert" else len(c["from"].split())
                        for c in changes if c["kind"] != "turn" and not c.get("reverted"))
    return {"segments": out, "changes": changes, "refs": refs_out, "marks": marks, "names": names,
            "stats": {"words": words_changed, "turns": len(moves), "names": len(names),
                      "applied_refs": sum(1 for r in refs_out if r["applied"])}}
