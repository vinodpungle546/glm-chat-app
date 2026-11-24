"""A meeting's attendees, kept in step with its speakers.

Built each time from:
- the participants list read from the video (`transcript.participants`: everyone who joined, with when they first
  and last appeared, and the people only invited);
- the named speakers: every named speaker is an attendee, linked to the listed person with the same name (or the
  only one with that first name, or the full form of a cut-off name), else added as an attendee of their own;
- your edits (`APP_DATA_DIR/attendees/{tid}.json`): people you added, removed or renamed. They're applied last and
  never changed by the app. Renaming an attendee who spoke renames the speaker (the API does that); renaming a
  speaker renames the listed attendee they were linked to (`follow_speaker_rename`).

Pure code; the API reads and writes the edits file.
"""

from __future__ import annotations

import participants_check as pc

LINK_HOW = ("exact", "accents", "first name", "cut short")  # not "sounds alike": the check renames those first


def empty_edits() -> dict:
    return {"added": [], "removed": [], "renamed": {}}


def speaker_name(spk: str, transcript: dict, labels: dict) -> str:
    """A speaker's real name (label, else a name the transcript came with), or ""."""
    label = labels.get(spk) or {}
    return (label.get("name") or (transcript.get("speaker_names") or {}).get(spk) or "").strip()


def build(transcript: dict, labels: dict, edits: dict | None = None) -> dict:
    """{"attendees": [{name, source (video | speaker | you), speaker, speaker_name, spoke, first_seen, last_seen,
    first_spoke, frames}], "invited": [names], "from_video": bool}."""
    edits = edits or empty_edits()
    renamed = edits.get("renamed") or {}
    removed = {pc.fold(n) for n in edits.get("removed") or []}
    video = transcript.get("participants") or {}
    rows: list[dict] = []
    for entry in video.get("names") or []:
        name = renamed.get(entry["name"], entry["name"])
        if pc.fold(name) in removed:
            continue
        rows.append({"name": name, "listed_as": entry["name"], "source": "video", "speaker": None, "speaker_name": "",
                     "spoke": False, "first_seen": entry.get("first_seen"), "last_seen": entry.get("last_seen"),
                     "first_spoke": None, "frames": entry.get("frames", 0)})
    first_spoke: dict[str, float] = {}
    for seg in transcript.get("segments") or []:
        spk = seg.get("speaker")
        if spk and spk not in first_spoke and (seg.get("text") or "").strip():
            first_spoke[spk] = float(seg.get("start") or 0)
    for spk in transcript.get("speakers") or []:
        name = speaker_name(spk, transcript, labels)
        if not name or spk not in first_spoke:
            continue
        free = [r for r in rows if r["speaker"] is None]
        found = pc.match(name, [r["name"] for r in free])
        row = next((r for r in free if found and found[1] in LINK_HOW and r["name"] == found[0]), None)
        if row is None:
            row = {"name": name, "listed_as": "", "source": "speaker", "speaker": None, "first_seen": None,
                   "last_seen": None, "frames": 0}
            rows.append(row)
        row.update(speaker=spk, speaker_name=name, spoke=True, first_spoke=first_spoke[spk])
    present = {pc.fold(r["name"]) for r in rows} | {pc.fold(r["speaker_name"]) for r in rows if r.get("speaker_name")}
    for name in edits.get("added") or []:
        if pc.fold(name) not in present and pc.fold(name) not in removed:
            rows.append({"name": name, "listed_as": "", "source": "you", "speaker": None, "speaker_name": "",
                         "spoke": False, "first_seen": None, "last_seen": None, "first_spoke": None, "frames": 0})
            present.add(pc.fold(name))
    rows.sort(key=lambda r: (r["source"] == "you", min(t for t in (r["first_seen"], r["first_spoke"], 1e9) if t is not None)))
    invited = [n for n in video.get("invited") or [] if pc.fold(n) not in present and pc.fold(n) not in removed]
    return {"attendees": rows, "invited": invited, "from_video": bool(video.get("names"))}


def edit(edits: dict, rows: list[dict], action: str, name: str, to: str = "") -> dict:
    """Your change to the attendees: add | remove | rename. Returns the new edits (raises ValueError with a message
    for the user when it can't be done). Renaming someone who spoke is done on the speaker (the caller)."""
    out = {"added": list(edits.get("added") or []), "removed": list(edits.get("removed") or []),
           "renamed": dict(edits.get("renamed") or {})}
    name, to = " ".join(name.split()), " ".join(to.split())
    row = next((r for r in rows if pc.fold(r["name"]) == pc.fold(name)), None)
    if action == "add":
        if not name:
            raise ValueError("Type a name.")
        if row is not None:
            raise ValueError(f"{name} is already an attendee.")
        out["removed"] = [n for n in out["removed"] if pc.fold(n) != pc.fold(name)]
        out["added"].append(name)
        return out
    if row is None:
        raise ValueError(f"{name} isn't an attendee.")
    if action == "remove":
        if row["speaker"]:
            raise ValueError(f"{name} spoke in this meeting. Rename the speaker instead, or move their lines to someone else.")
        if row["source"] == "you":
            out["added"] = [n for n in out["added"] if pc.fold(n) != pc.fold(name)]
        else:
            out["removed"].append(name)
        return out
    if action == "rename":
        if not to:
            raise ValueError("Type the new name.")
        if row["source"] == "you":
            out["added"] = [to if pc.fold(n) == pc.fold(name) else n for n in out["added"]]
        elif row["source"] == "video":
            out["renamed"][row["listed_as"]] = to
        return out
    raise ValueError("Unknown change.")


def follow_speaker_rename(edits: dict, rows: list[dict], speaker: str, new_name: str) -> dict:
    """You renamed a speaker: the listed attendee they were linked to takes the new name too (kept as your edit),
    so the attendees and the speakers stay in step."""
    row = next((r for r in rows if r.get("speaker") == speaker and r["source"] == "video"), None)
    if row is None or not new_name or pc.match(new_name, [row["name"]]):
        return edits
    out = {"added": list(edits.get("added") or []), "removed": list(edits.get("removed") or []),
           "renamed": dict(edits.get("renamed") or {})}
    out["renamed"][row["listed_as"]] = new_name
    return out


def names(view: dict) -> list[str]:
    """Every attendee's name (for meeting notes and the meeting check)."""
    return [r["name"] for r in view.get("attendees") or []]
