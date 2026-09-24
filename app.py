#!/usr/bin/env python3
"""Streamlit chat UI for GLM-5.3-Flash via the Globant GAIA API.

Configuration is read from environment variables (loaded from .env by docker-compose):
  GAIA_BASE_URL, GAIA_API_KEY, GAIA_MODEL, GAIA_ENDPOINT

Chat sessions are persisted as JSON files in SESSIONS_DIR and can be
re-opened or deleted from the sidebar.
"""

import json
import os
import uuid
from datetime import datetime
from pathlib import Path

import requests
import streamlit as st

BASE_URL = os.environ.get("GAIA_BASE_URL", "https://api.clients.globant.com").rstrip("/")
API_KEY = os.environ.get("GAIA_API_KEY", "")
MODEL = os.environ.get("GAIA_MODEL", "globant_dgx/GLM-5.3-Flash")
ENDPOINT = os.environ.get("GAIA_ENDPOINT", f"{BASE_URL}/v1/chat/completions")

SESSIONS_DIR = Path(os.environ.get("SESSIONS_DIR", "/app/sessions"))
SYSTEM_PROMPT = "You are a helpful assistant. Answer concisely."

st.set_page_config(page_title="GLM-5.3-Flash Chat", page_icon="💬", layout="centered")


# ---------------------------------------------------------------------------
# Session persistence (one JSON file per conversation in SESSIONS_DIR)
# ---------------------------------------------------------------------------

def list_sessions() -> list[dict]:
    """Return saved sessions, newest first."""
    if not SESSIONS_DIR.exists():
        return []
    sessions = []
    for path in SESSIONS_DIR.glob("*.json"):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            sessions.append({
                "id": data.get("id", path.stem),
                "title": data.get("title", "Untitled chat"),
                "updated_at": data.get("updated_at", ""),
                "path": path,
            })
        except (json.JSONDecodeError, OSError):
            continue
    sessions.sort(key=lambda s: s["updated_at"], reverse=True)
    return sessions


def save_session(session_id: str, title: str, messages: list[dict]) -> None:
    """Persist one conversation to disk."""
    if not session_id:
        return
    SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
    record = {
        "id": session_id,
        "title": title,
        "updated_at": datetime.now().isoformat(timespec="seconds"),
        "messages": messages,
    }
    (SESSIONS_DIR / f"{session_id}.json").write_text(
        json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def load_session(session_id: str) -> list[dict]:
    """Load messages for a saved session, or None if it no longer exists."""
    path = SESSIONS_DIR / f"{session_id}.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8")).get("messages", [])
    except (json.JSONDecodeError, OSError):
        return None


def delete_session(session_id: str) -> None:
    """Delete a saved session file."""
    path = SESSIONS_DIR / f"{session_id}.json"
    if path.exists():
        path.unlink()


def session_title(messages: list[dict]) -> str:
    """Derive a session title from the first user message."""
    for message in messages:
        if message["role"] == "user":
            text = message["content"].strip().replace("\n", " ")
            return text[:40] + ("…" if len(text) > 40 else "")
    return "Untitled chat"


def stream_reply(messages: list[dict]):
    """Stream a model reply for the given conversation, yielding text chunks."""
    headers = {"Authorization": f"Bearer {API_KEY}", "Content-Type": "application/json"}
    payload = {"model": MODEL, "messages": messages, "stream": True}

    with requests.post(ENDPOINT, headers=headers, json=payload, stream=True, timeout=120) as response:
        response.raise_for_status()
        for line in response.iter_lines(decode_unicode=True):
            if not line or not line.startswith("data: "):
                continue
            data = line[len("data: "):]
            if data.strip() == "[DONE]":
                break
            chunk = json.loads(data)
            delta = chunk.get("choices", [{}])[0].get("delta", {}).get("content")
            if delta:
                yield delta


def render_history(messages: list[dict]) -> None:
    """Render a list of conversation messages in the chat area."""
    for message in messages:
        with st.chat_message(message["role"]):
            st.markdown(message["content"])


def main() -> None:
    st.title("💬 GLM-5.3-Flash Chat")
    st.caption(f"Model: `{MODEL}` — served via Globant GAIA")

    # Initialize per-session UI state
    if "messages" not in st.session_state:
        st.session_state.messages = []
    if "session_id" not in st.session_state:
        st.session_state.session_id = ""
    if "session_loaded" not in st.session_state:
        st.session_state.session_loaded = False

    # Sidebar: new chat, saved sessions, controls
    with st.sidebar:
        st.header("Chats")

        if st.button("➕ New chat", use_container_width=True):
            st.session_state.messages = []
            st.session_state.session_id = ""
            st.session_state.session_loaded = False
            st.rerun()

        st.divider()

        saved_sessions = list_sessions()
        st.subheader("Previous sessions")
        if not saved_sessions:
            st.caption("No saved sessions yet.")
        for saved in saved_sessions:
            is_current = saved["id"] == st.session_state.session_id
            label = f"{'🟢 ' if is_current else '💬 '}{saved['title']}"
            col_open, col_delete = st.columns([4, 1])
            if col_open.button(label, key=f"open_{saved['id']}", use_container_width=True):
                loaded = load_session(saved["id"])
                if loaded is not None:
                    st.session_state.messages = loaded
                    st.session_state.session_id = saved["id"]
                    st.session_state.session_loaded = True
                    st.rerun()
                else:
                    st.error("Session file could not be loaded.")
            if col_delete.button("🗑️", key=f"delete_{saved['id']}", help="Delete this session"):
                delete_session(saved["id"])
                if is_current:
                    st.session_state.messages = []
                    st.session_state.session_id = ""
                st.rerun()
            col_open.caption(saved["updated_at"].replace("T", " "))

        st.divider()
        st.write("🟢 Connected" if API_KEY else "🔴 No API key configured")
        st.caption("Sessions are persisted on the server and survive app restarts.")

    # Render current conversation
    render_chat_and_handle_input()


def render_chat_and_handle_input() -> None:
    """Render past turns and process any new user message."""
    render_history(st.session_state.messages)

    if user_input := st.chat_input("Type your message..."):
        # New conversation: mint an id on first message
        if not st.session_state.session_id:
            st.session_state.session_id = uuid.uuid4().hex

        st.session_state.messages.append({"role": "user", "content": user_input})
        with st.chat_message("user"):
            st.markdown(user_input)

        api_messages = [{"role": "system", "content": SYSTEM_PROMPT}] + st.session_state.messages

        with st.chat_message("assistant"):
            try:
                reply = st.write_stream(stream_reply(api_messages))
            except requests.RequestException as exc:
                st.error(f"⚠️ Request failed: {exc}")
                st.session_state.messages.pop()  # keep history consistent
                return

        st.session_state.messages.append({"role": "assistant", "content": reply})

        # Persist the conversation after each completed turn
        save_session(
            st.session_state.session_id,
            session_title(st.session_state.messages),
            st.session_state.messages,
        )


if __name__ == "__main__":
    main()
