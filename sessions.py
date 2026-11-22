#!/usr/bin/env python3
"""Chat sessions: one JSON file per conversation, in the project's sessions folder.

Same file format as the original Streamlit app (id, title, updated_at, messages, confidential,
local_model), plus fields used by the web app: model_id, attachments,
cloud_approved_reasons, custom_title, created_at.
"""

from __future__ import annotations

import json
import re
import uuid
from datetime import datetime
from pathlib import Path

EXTRA_DEFAULTS = {"confidential": False, "local_model": "", "model_id": "", "attachments": [],
                  "cloud_approved_reasons": [], "custom_title": False}


def _now() -> str:
    """The current local time as an ISO string, e.g. '2026-11-21T17:24:26' (used for timestamps)."""
    return datetime.now().isoformat(timespec="seconds")


def _path(sessions_dir: Path, session_id: str) -> Path:
    """The JSON file of one chat. Raises KeyError for an id that isn't plain hex, so a crafted id like '../../x'
    can never reach a file outside the sessions folder.
    """
    if not re.fullmatch(r"[0-9a-f]{8,40}", session_id or ""):
        raise KeyError(session_id)
    return sessions_dir / f"{session_id}.json"


def title_from(messages: list[dict]) -> str:
    """A short title for a chat: its first user message, whitespace tidied, cut at 48 characters ('New chat' when
    there is none yet).
    """
    for message in messages:
        if message["role"] == "user":
            text = re.sub(r"\s+", " ", message["content"]).strip()
            text = re.sub(r"^🧩 \*[^*]+\*\s*", "", text)
            return text[:48] + ("…" if len(text) > 48 else "") if text else "New chat"
    return "New chat"


def create(sessions_dir: Path, model_id: str = "") -> dict:
    """Creates and saves an empty chat (optionally remembering which model it uses) and returns it."""
    record = {"id": uuid.uuid4().hex, "title": "New chat", "created_at": _now(), "updated_at": _now(),
              "messages": [], **EXTRA_DEFAULTS, "model_id": model_id}
    save(sessions_dir, record, touch=False)
    return record


def load(sessions_dir: Path, session_id: str) -> dict | None:
    """Reads one chat from disk, filling in fields older files don't have. None if missing or unreadable."""
    try:
        data = json.loads(_path(sessions_dir, session_id).read_text(encoding="utf-8"))
    except (OSError, ValueError, KeyError):
        return None
    for key, value in EXTRA_DEFAULTS.items():
        data.setdefault(key, type(value)() if isinstance(value, list) else value)
    data.setdefault("messages", [])
    data.setdefault("title", title_from(data["messages"]))
    data.setdefault("created_at", data.get("updated_at", ""))
    return data


def save(sessions_dir: Path, record: dict, touch: bool = True) -> None:
    """Writes a chat to disk. Updates `updated_at` unless touch=False, and re-derives the title unless the user
    renamed the chat (`custom_title`).
    """
    sessions_dir.mkdir(parents=True, exist_ok=True)
    if touch:
        record["updated_at"] = _now()
    if not record.get("custom_title"):
        record["title"] = title_from(record["messages"])
    _path(sessions_dir, record["id"]).write_text(json.dumps(record, ensure_ascii=False, indent=2),
                                                 encoding="utf-8")


def delete(sessions_dir: Path, session_id: str) -> None:
    """Deletes a chat's file (no error if it is already gone)."""
    path = _path(sessions_dir, session_id)
    if path.exists():
        path.unlink()


def clean_title(title: str) -> str:
    """'🧩 *Meeting minutes*  Write…' (older chats) -> 'Meeting minutes · Write…'."""
    title = re.sub(r"\s+", " ", title or "").strip()
    title = re.sub(r"^🧩\s*\*([^*]+)\*\s*", r"\1 · ", title)
    title = re.sub(r"📎\s*\*attached:[^*]*\*?", "", title).replace("**", "").strip(" ·")
    return title or "New chat"


def summary(record: dict) -> dict:
    """The small version of a chat shown in the sidebar list: title, dates, message count, model and a preview of
    the last answer.
    """
    last = next((m["content"] for m in reversed(record.get("messages", [])) if m.get("role") == "assistant"), "")
    return {"id": record["id"], "title": clean_title(record.get("title", "")),
            "updated_at": record.get("updated_at", ""), "created_at": record.get("created_at", ""),
            "message_count": len(record.get("messages", [])), "confidential": bool(record.get("confidential")),
            "model_id": record.get("model_id", ""), "preview": re.sub(r"\s+", " ", last)[:120]}


def list_all(sessions_dir: Path, query: str = "") -> list[dict]:
    """Sessions, newest first; `query` matches the title or any message text."""
    if not sessions_dir.exists():
        return []
    q = query.strip().lower()
    out = []
    for path in sessions_dir.glob("*.json"):
        record = load(sessions_dir, path.stem)
        if record is None:
            continue
        if q and q not in record.get("title", "").lower() and not any(
                q in str(m.get("content", "")).lower() for m in record.get("messages", [])):
            continue
        out.append(summary(record))
    out.sort(key=lambda s: s["updated_at"], reverse=True)
    return out
