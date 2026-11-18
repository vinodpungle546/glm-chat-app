#!/usr/bin/env python3
"""Project-based AI agent chat for GLM-5.3-Flash via the Globant GAIA API.

Configuration is read from environment variables (loaded from .env by docker-compose):
  GAIA_BASE_URL, GAIA_API_KEY, GAIA_MODEL, GAIA_ENDPOINT, PROJECTS_DIR, SESSIONS_DIR

Each project has its own chat sessions, instructions, context documents,
skills, tools, and long-term memory. The chat input has a built-in paperclip
(accept_file) so attachments sit right beside the message box.
"""

import json
import os
import re
import uuid
from datetime import datetime
from pathlib import Path

import requests
import streamlit as st

import connectors
import projects
import tools
from doc_extract import (
    UnsupportedDocumentError,
    build_document_context,
    extract_text,
)
from web_search import build_search_decision_messages, parse_search_decision

BASE_URL = os.environ.get("GAIA_BASE_URL", "https://api.clients.globant.com").rstrip("/")
API_KEY = os.environ.get("GAIA_API_KEY", "")
MODEL = os.environ.get("GAIA_MODEL", "globant_dgx/GLM-5.3-Flash")
ENDPOINT = os.environ.get("GAIA_ENDPOINT", f"{BASE_URL}/v1/chat/completions")

SESSIONS_DIR = Path(os.environ.get("SESSIONS_DIR", "/app/sessions"))
SYSTEM_PROMPT = "You are a helpful AI agent. Answer concisely."
ATTACHMENT_TYPES = ["txt", "md", "csv", "log", "pdf", "docx", "doc", "xlsx", "xlsm", "pptx", "ppt"]

st.set_page_config(page_title="Vinod's Personal Chat Agent", page_icon="💬", layout="centered")

DARK_MODE_CSS = """
<style>
    /* Chat input: visible text and placeholder in dark mode */
    div[data-testid="stChatInput"] textarea {
        background-color: #1b2130 !important;
        color: #f0f2f6 !important;
        border: 2.5px solid #4da6ff !important;
        border-radius: 12px !important;
        box-shadow: 0 0 6px rgba(77, 166, 255, 0.35) !important;
    }
    div[data-testid="stChatInput"] textarea:focus {
        border-color: #6db8ff !important;
        box-shadow: 0 0 10px rgba(77, 166, 255, 0.55) !important;
    }
    div[data-testid="stChatInput"] textarea::placeholder {
        color: #8b93a5 !important;
        opacity: 1 !important;
    }
    div[data-testid="stChatInput"] button {
        background-color: #4da6ff !important;
        border-color: #4da6ff !important;
        color: #0e1117 !important;
    }
    /* File uploader dropzone: visible in dark mode */
    div[data-testid="stFileUploaderDropzone"] {
        background-color: #1b2130 !important;
        color: #f0f2f6 !important;
        border: 1px dashed #3d4657 !important;
    }
    div[data-testid="stFileUploaderDropzone"] button {
        background-color: #2b3446 !important;
        color: #f0f2f6 !important;
        border: 1px solid #3d4657 !important;
    }
    /* Uploaded-file list and remove buttons in the sidebar */
    section[data-testid="stSidebar"] [data-testid="stFileUploaderFile"] {
        color: #f0f2f6 !important;
        background-color: #2b3446 !important;
    }
</style>
"""


def inject_css() -> None:
    """Apply dark-mode-friendly styling to chat input and uploader."""
    st.markdown(DARK_MODE_CSS, unsafe_allow_html=True)


# ---------------------------------------------------------------------------
# Session persistence (one JSON file per conversation, per project)
# ---------------------------------------------------------------------------

def list_sessions(sessions_dir: Path) -> list[dict]:
    """Return saved sessions, newest first."""
    if not sessions_dir.exists():
        return []
    sessions = []
    for path in sessions_dir.glob("*.json"):
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


def save_session(sessions_dir: Path, session_id: str, title: str, messages: list[dict]) -> None:
    """Persist one conversation to disk."""
    if not session_id:
        return
    sessions_dir.mkdir(parents=True, exist_ok=True)
    record = {
        "id": session_id,
        "title": title,
        "updated_at": datetime.now().isoformat(timespec="seconds"),
        "messages": messages,
    }
    (sessions_dir / f"{session_id}.json").write_text(
        json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def load_session(sessions_dir: Path, session_id: str) -> list[dict] | None:
    """Load messages for a saved session, or None if it no longer exists."""
    path = sessions_dir / f"{session_id}.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8")).get("messages", [])
    except (json.JSONDecodeError, OSError):
        return None


def delete_session(sessions_dir: Path, session_id: str) -> None:
    """Delete a saved session file."""
    path = sessions_dir / f"{session_id}.json"
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


# ---------------------------------------------------------------------------
# Reply generation (agent loop with tool use)
# ---------------------------------------------------------------------------

def build_chat_attachment_context() -> str:
    """Context block for files attached via the chat paperclip this session."""
    chat_files = st.session_state.get("chat_attachments", [])
    if not chat_files:
        return ""
    parts = [
        "The user attached the following files in this chat session. "
        "Prefer them over project context documents when answering:"
    ]
    for f in chat_files:
        parts.append(f"--- Chat attachment: {f['name']} ---\n{f['text']}")
    return "\n\n".join(parts)


def generate_reply(user_input: str, turn_skill=None) -> str:
    """Produce the agent reply; may run tool/skill rounds then answer.

    turn_skill applies a skill activated via a '/' command to this turn only.
    Global connectors (custom + MCP) are exposed to every project.
    """
    project = st.session_state.project
    memory_block = tools.build_memory_block(project)
    document_context = build_document_context(project["documents"])
    chat_context = build_chat_attachment_context()
    if chat_context:
        document_context = (
            (document_context + "\n\n" + chat_context) if document_context else chat_context
        )
    system_prompt = tools.build_agent_system_prompt(
        SYSTEM_PROMPT, project, document_context, memory_block, turn_skill
    )
    allowed = [t for t in project.get("tools", []) if t in tools.TOOL_REGISTRY]
    skills = tools.available_skills(project)
    global_connectors = connectors.load_connectors()

    # Web-search assist: decide first whether fresh web info is needed.
    if "web_search" in allowed and st.session_state.get("web_search_enabled", True):
        decision = get_search_decision()
        if decision["needs_search"]:
            with st.spinner(f"🌐 Searching the web: “{decision['query']}”…"):
                result = tools.run_tool("web_search", decision["query"], {})
            st.caption(f"🌐 Searched the web for: “{decision['query']}”")
            return stream_with_tool_result(result, f"web search results for “{decision['query']}”")

    api_messages = [{"role": "system", "content": system_prompt}] + st.session_state.messages
    # Guarantee this turn's content (including embedded attachment text) is an
    # explicit final user message - never only in the system prompt.
    last_content = (
        st.session_state.messages[-1]["content"] if st.session_state.messages else None
    )
    if user_input and user_input.strip() and user_input != last_content:
        api_messages.append({"role": "user", "content": user_input})
    reply = stream_assistant_reply(api_messages)

    # Debug: log what the model received (names + sizes only, no content)
    import logging
    logging.basicConfig(level=logging.INFO)
    summary = [f"{m['role']}:{len(m['content'])}ch" for m in api_messages]
    logging.getLogger("agent").warning(
        "AGENT-REQUEST %s | attachments=%s | chat_ctx_len=%s | sys_len=%s",
        summary,
        [f["name"] for f in st.session_state.get("chat_attachments", [])],
        len(build_chat_attachment_context()),
        len(system_prompt),
    )

    # Agent loop: the model may request a skill (progressive disclosure),
    # a project tool, or a global connector.
    skill_request = tools.parse_skill_request(reply, skills)
    if skill_request:
        with st.spinner(f"🧩 Loading skill `{skill_request['name']}`…"):
            skill_content = skill_request["content"] or skill_request["name"]
        st.caption(f"🧩 Using skill `{skill_request['name']}`")
        return stream_with_tool_result(
            f"Full content of skill '{skill_request['name']}':\n{skill_content}",
            f"skill {skill_request['name']}",
        )

    tool_call = tools.parse_tool_call(reply, allowed)
    if tool_call:
        with st.spinner(f"🔧 Using tool `{tool_call['tool']}`…"):
            result = tools.run_tool(tool_call["tool"], tool_call["input"], project)
        st.caption(f"🔧 Used tool `{tool_call['tool']}`")
        return stream_with_tool_result(result, f"`{tool_call['tool']}` tool")

    connector_call = parse_connector_call(reply, global_connectors)
    if connector_call:
        connector, tool_input = connector_call
        with st.spinner(f"🔌 Using connector `{connector['name']}`…"):
            result = connectors.execute_connector(connector, tool_input)
        st.caption(f"🔌 Used connector `{connector['name']}`")
        return stream_with_tool_result(result, f"`{connector['name']}` connector")

    return reply


def parse_connector_call(text: str, global_connectors: list[dict]) -> tuple[dict, str] | None:
    """Return (connector, input) if the model requests a global connector."""
    import json as _json
    import re as _re

    if not text or not global_connectors:
        return None
    match = _re.search(r"\{.*\}", text, _re.DOTALL)
    if not match:
        return None
    try:
        data = _json.loads(match.group(0))
    except _json.JSONDecodeError:
        return None
    if not isinstance(data, dict) or "connector" not in data:
        return None
    name = data.get("connector")
    for connector in global_connectors:
        if connector["name"].lower() == str(name).strip().lower():
            tool_input = data.get("input", "")
            return connector, tool_input if isinstance(tool_input, str) else str(tool_input)
    return None


def stream_with_tool_result(result: str, label: str) -> str:
    """Stream the final answer after injecting a tool result as context."""
    tool_message = {
        "role": "user",
        "content": f"[Tool output — {label}]\n{result}\n\nUse this to answer my previous question.",
    }
    api_messages = (
        [{"role": "system", "content": system_prompt_for_reply()}]
        + st.session_state.messages
        + [tool_message]
    )
    return stream_assistant_reply(api_messages)


def system_prompt_for_reply() -> str:
    """Rebuild the current system prompt (used by stream_with_tool_result)."""
    project = st.session_state.project
    document_context = build_document_context(project["documents"])
    chat_context = build_chat_attachment_context()
    if chat_context:
        document_context = (
            document_context + "\n\n" + chat_context if document_context else chat_context
        )
    return tools.build_agent_system_prompt(
        SYSTEM_PROMPT,
        project,
        document_context,
        tools.build_memory_block(project),
    )


def get_search_decision() -> dict:
    """Ask the model (non-streaming) whether a web search is required."""
    headers = {"Authorization": f"Bearer {API_KEY}", "Content-Type": "application/json"}
    payload = {
        "model": MODEL,
        "messages": build_search_decision_messages(st.session_state.messages),
        "stream": False,
    }
    try:
        response = requests.post(ENDPOINT, headers=headers, json=payload, timeout=60)
        response.raise_for_status()
        content = response.json()["choices"][0]["message"]["content"]
    except (requests.RequestException, KeyError, IndexError, ValueError):
        return {"needs_search": False}
    return parse_search_decision(content)


def update_memory_from_conversation() -> None:
    """Ask the model whether anything durable from the recent conversation
    should be stored in project memory, and apply it silently."""
    project = st.session_state.project
    recent = st.session_state.messages[-6:]
    if len(recent) < 2:
        return
    transcript = "\n".join(
        f"{m['role']}: {m['content'][:500]}" for m in recent
    )
    headers = {"Authorization": f"Bearer {API_KEY}", "Content-Type": "application/json"}
    payload = {
        "model": MODEL,
        "stream": False,
        "messages": [
            {
                "role": "system",
                "content": (
                    "You maintain long-term memory for a chat project.\n"
                    "Existing memory entries:\n"
                    + ("\n".join(f"- {m}" for m in project["memory"]) or "(none)")
                    + "\n\nGiven this conversation excerpt, extract up to 2 NEW durable facts "
                    "worth remembering long-term (user preferences, project facts, decisions). "
                    "Skip anything already covered by existing memory or trivial small talk. "
                    "Reply ONLY with a JSON array of short strings, e.g. "
                    '["User prefers Python", "Deploy target is GCP"]. Reply [] if nothing worth storing.'
                ),
            },
            {"role": "user", "content": transcript},
        ],
    }
    try:
        response = requests.post(ENDPOINT, headers=headers, json=payload, timeout=60)
        response.raise_for_status()
        content = response.json()["choices"][0]["message"]["content"]
        match = re.search(r"\[.*\]", content, re.DOTALL)
        if not match:
            return
        extracted = json.loads(match.group(0))
        if not isinstance(extracted, list):
            return
    except (requests.RequestException, KeyError, IndexError, ValueError, json.JSONDecodeError):
        return

    added = merge_new_memories(project, extracted)
    if added:
        projects.save_project(project)
        st.toast("🧠 Memory updated: " + "; ".join(added[:2]), icon="✍️")


def merge_new_memories(project: dict, extracted: list) -> list[str]:
    """Append only new, non-duplicate facts; return what was added."""
    existing = {entry.strip().lower() for entry in project["memory"]}
    candidates = [s for s in extracted if isinstance(s, str) and s.strip()][:2]
    added = []
    for item in candidates:
        if item.strip().lower() not in existing:
            project["memory"].append(item.strip())
            existing.add(item.strip().lower())
            added.append(item.strip())
    return added


def maybe_update_memory() -> None:
    """Update memory every few turns to limit extra API calls."""
    project = st.session_state.project
    turns_since = len(st.session_state.messages) - st.session_state.get("memory_last_len", 0)
    if turns_since >= 4:
        st.session_state.memory_last_len = len(st.session_state.messages)
        update_memory_from_conversation()


def stream_assistant_reply(api_messages: list[dict]) -> str:
    """Stream the assistant's reply for the given message list."""
    return st.write_stream(stream_reply(api_messages))


# ---------------------------------------------------------------------------
# Main UI
# ---------------------------------------------------------------------------

def main() -> None:
    inject_css()
    st.title("💬 Vinod's Personal Chat Agent")
    st.caption(f"Model: `{MODEL}` — served via Globant GAIA")

    init_state()
    inject_css()

    with st.sidebar:
        render_connectors_section()
        st.divider()
        render_projects_section()

        st.divider()
        st.subheader("Tools")
        st.session_state.web_search_enabled = st.toggle(
            "🌐 Auto web search",
            value=st.session_state.get("web_search_enabled", True),
            help="Let the agent search the web proactively when it lacks information.",
        )

        st.divider()
        st.write("🟢 Connected" if API_KEY else "🔴 No API key configured")
        st.caption("Sessions are persisted on the server and survive app restarts.")

    render_chat_and_handle_input()


def init_state() -> None:
    """Initialize per-session UI state."""
    if "messages" not in st.session_state:
        st.session_state.messages = []
    if "session_id" not in st.session_state:
        st.session_state.session_id = ""
    if "session_loaded" not in st.session_state:
        st.session_state.session_loaded = False
    if "project_id" not in st.session_state:
        default_project = projects.get_or_create_default_project()
        st.session_state.project_id = default_project["id"]
        st.session_state.project = default_project
    if "project" not in st.session_state or st.session_state.project["id"] != st.session_state.project_id:
        st.session_state.project = projects.load_project(st.session_state.project_id)


def sessions_dir_for_current_project() -> Path:
    """Where the active project's session files live."""
    return projects.sessions_dir_for(st.session_state.project, SESSIONS_DIR)


def switch_project(project_id: str) -> None:
    """Switch to a project and reset the current chat."""
    project = projects.load_project(project_id)
    if project is None:
        project = projects.get_or_create_default_project()
    st.session_state.project_id = project["id"]
    st.session_state.project = project
    st.session_state.session_id = ""
    st.session_state.session_loaded = False
    st.session_state.messages = []
    st.rerun()


def render_connectors_section() -> None:
    """Global MCP/custom connectors - available to ALL projects."""
    st.header("🔌 Connectors")
    existing = connectors.load_connectors()
    with st.expander("➕ Add connector", expanded=False):
        kind = st.selectbox("Type", ["mcp", "custom"], help="mcp: remote MCP server (URL). custom: tool registered in code.")
        name = st.text_input("Name", key="connector_name")
        description = st.text_input("Description", key="connector_description")
        url = st.text_input("MCP server URL", key="connector_url") if kind == "mcp" else ""
        if st.button("Add", key="add_connector", type="primary",
                     disabled=not name.strip() or (kind == "mcp" and not url.strip())):
            try:
                connectors.add_connector(kind, name, description, url)
                st.rerun()
            except ValueError as exc:
                st.warning(str(exc))
    if not existing:
        st.caption("No connectors yet.")
    for connector in existing:
        with st.expander(f"{connector['name']} ({connector['kind']})", expanded=False):
            st.caption(connector.get("description") or "(no description)")
            if connector["kind"] == "mcp":
                st.caption(connector["url"])
                try:
                    remote = connectors.list_mcp_tools(connector["url"])
                    for t in remote:
                        st.caption(f"• {t['name']}")
                except Exception as exc:
                    st.caption(f"⚠️ Cannot reach server: {exc}")
            if st.button("Remove", key=f"rm_conn_{connector['id']}"):
                connectors.remove_connector(connector["id"])
                st.rerun()


def render_projects_section() -> None:
    """Project cards with everything nested inside: instructions, skills,
    tools, memory, documents, and this project's chat sessions."""
    all_projects = projects.list_projects()
    names = {p["id"]: p["name"] for p in all_projects}
    current_id = st.session_state.project_id

    # Expandable project cards; only the active project is expanded.
    for project_summary in all_projects:
        pid = project_summary["id"]
        is_active = pid == current_id
        with st.expander(
            f"{'🟢' if is_active else '📁'} {project_summary['name']}",
            expanded=is_active,
        ):
            if not is_active:
                if st.button(
                    "Switch to this project",
                    key=f"switch_{pid}",
                    use_container_width=True,
                ):
                    switch_project(pid)
                continue

            # Active project: load fresh details and render all sub-sections.
            st.session_state.project = projects.load_project(pid)
            render_project_settings()

            st.divider()
            st.markdown("**💬 Chat sessions**")
            sessions_dir = sessions_dir_for_current_project()
            render_session_list(sessions_dir)

    # New / delete project controls
    col_new, col_del, _ = st.columns([1, 1, 2])
    if col_new.button("➕ New project", use_container_width=True):
        st.session_state.show_new_project = not st.session_state.get("show_new_project", False)
    if col_del.button(
        "🗑️",
        use_container_width=True,
        disabled=current_id == projects.DEFAULT_PROJECT_ID,
        help="Delete the active project",
    ):
        projects.delete_project(current_id)
        switch_project(projects.DEFAULT_PROJECT_ID)

    if st.session_state.get("show_new_project"):
        new_name = st.text_input("Project name", key="new_project_name", placeholder="e.g. Data analysis")
        col_create, col_cancel, _ = st.columns([2, 2, 1])
        if col_create.button(
            "Create", type="primary", use_container_width=True, disabled=not new_name.strip()
        ):
            created = projects.create_project(new_name.strip())
            st.session_state.show_new_project = False
            switch_project(created["id"])
        if col_cancel.button("Cancel", use_container_width=True):
            st.session_state.show_new_project = False


def render_session_list(sessions_dir: Path) -> None:
    """Saved sessions for the active project."""
    if st.button("➕ New chat", use_container_width=True):
        st.session_state.messages = []
        st.session_state.session_id = ""
        st.session_state.session_loaded = False
        st.session_state.chat_attachments = []
        st.rerun()
    saved_sessions = list_sessions(sessions_dir)
    if not saved_sessions:
        st.caption("No saved sessions yet.")
    for saved in saved_sessions:
        is_current = saved["id"] == st.session_state.session_id
        label = f"{'🟢 ' if is_current else '💬 '}{saved['title']}"
        col_open, col_delete = st.columns([4, 1])
        if col_open.button(label, key=f"open_{saved['id']}", use_container_width=True):
            loaded = load_session(sessions_dir, saved["id"])
            if loaded is not None:
                st.session_state.messages = loaded
                st.session_state.session_id = saved["id"]
                st.session_state.session_loaded = True
                st.rerun()
            else:
                st.error("Session file could not be loaded.")
        if col_delete.button("🗑️", key=f"delete_{saved['id']}", help="Delete this session"):
            delete_session(sessions_dir, saved["id"])
            if is_current:
                st.session_state.messages = []
                st.session_state.session_id = ""
            st.rerun()
        col_open.caption(saved["updated_at"].replace("T", " "))


def render_project_settings() -> None:
    """Edit instructions, skills, tools, memory, and context documents."""
    project = st.session_state.project

    instructions = st.text_area(
        "📋 Instructions",
        value=project["instructions"],
        help="Standing guidance for the agent in this project.",
        placeholder="e.g. You are a data analyst. Always show units.",
        key=f"instructions_{project['id']}",
    )
    st.caption("🧩 Skills — name, description (when to use), and content (procedure):")
    new_skill_name = st.text_input("New skill name", key=f"new_skill_name_{project['id']}")
    new_skill_desc = st.text_area(
        "New skill description (when the agent should use it)",
        key=f"new_skill_desc_{project['id']}",
    )
    new_skill_content = st.text_area(
        "New skill content (step-by-step instructions)",
        key=f"new_skill_content_{project['id']}",
    )
    if st.button(
        "Add skill",
        key=f"add_skill_{project['id']}",
        disabled=not new_skill_name.strip(),
    ):
        skill_name = new_skill_name.strip()
        skill_desc = new_skill_desc.strip()
        skill_content = new_skill_content.strip() or skill_desc or skill_name
        if any(
            (s["name"] if isinstance(s, dict) else s).lower() == skill_name.lower()
            for s in project["skills"]
        ):
            st.warning(f"A skill named '{skill_name}' already exists in this project.")
        else:
            project["skills"].append(
                {"name": skill_name, "description": skill_desc, "content": skill_content}
            )
            projects.save_project(project)
            for key in (
                f"new_skill_name_{project['id']}",
                f"new_skill_desc_{project['id']}",
                f"new_skill_content_{project['id']}",
            ):
                st.session_state[key] = ""
            st.toast(f"Skill saved: {skill_name}")
        st.rerun()
    for i, skill in enumerate(list(project["skills"])):
        with st.expander(f"🧩 {name if (name := (skill['name'] if isinstance(skill, dict) else skill[:40])) else name}", expanded=False):
            if isinstance(skill, str):
                new_text = st.text_area("Skill text", value=skill, key=f"edit_skill_{project['id']}_{i}")
                if st.button("💾 Save", key=f"save_skill_{project['id']}_{i}"):
                    project["skills"][i] = {
                        "name": new_text.split("\n")[0][:40].strip() or new_text.strip()[:40],
                        "description": "",
                        "content": new_text.strip(),
                    }
                    projects.save_project(project)
                    st.toast("Skill updated")
                    st.rerun()
            else:
                e_name = st.text_input("Name", value=skill["name"], key=f"esn_{project['id']}_{i}")
                e_desc = st.text_area("Description (when to use)", value=skill["description"], key=f"esd_{project['id']}_{i}")
                e_content = st.text_area("Content (procedure)", value=skill["content"], key=f"esc_{project['id']}_{i}")
                col_save, col_del = st.columns(2)
                if col_save.button("💾 Save", key=f"save_skill_{project['id']}_{i}", type="primary"):
                    if not e_name.strip():
                        st.warning("Skill name cannot be empty.")
                    else:
                        project["skills"][i] = {"name": e_name.strip(), "description": e_desc.strip(), "content": e_content.strip() or e_desc.strip()}
                        projects.save_project(project)
                        st.toast(f"Skill updated: {e_name.strip()}")
                        st.rerun()
                if col_del.button("🗑️ Delete", key=f"del_skill_{project['id']}_{i}"):
                    project["skills"].pop(i)
                    projects.save_project(project)
                    st.rerun()

    chosen_tools = st.multiselect(
        "🔧 Tools",
        options=sorted(tools.TOOL_REGISTRY),
        default=[t for t in project["tools"] if t in tools.TOOL_REGISTRY],
        format_func=lambda name: f"{name} — {tools.TOOL_REGISTRY[name]['description']}",
        key=f"tools_{project['id']}",
    )

    st.caption("🧠 Memory — facts the agent always remembers in this project:")
    for i, entry in enumerate(list(project["memory"])):
        with st.expander(f"• {entry[:60]}", expanded=False):
            edited = st.text_area("Memory entry", value=entry, key=f"edit_mem_{project['id']}_{i}")
            col_save, col_del = st.columns(2)
            if col_save.button("💾 Save", key=f"save_mem_{project['id']}_{i}", type="primary"):
                if edited.strip():
                    project["memory"][i] = edited.strip()
                    projects.save_project(project)
                    st.toast("Memory updated")
                    st.rerun()
            if col_del.button("🗑️ Delete", key=f"del_mem_{project['id']}_{i}"):
                project["memory"].pop(i)
                projects.save_project(project)
                st.rerun()
    new_memory = st.text_input("Add memory", key=f"memory_input_{project['id']}")

    st.caption("📎 Context documents (attached to every message):")
    uploaded = st.file_uploader(
        "Upload context documents",
        type=ATTACHMENT_TYPES,
        accept_multiple_files=True,
        key=f"pdocs_{project['id']}",
    )
    for upload in uploaded or []:
        if any(doc["name"] == upload.name for doc in project["documents"]):
            continue
        try:
            text = extract_text(upload.name, upload.getvalue())
        except UnsupportedDocumentError as exc:
            st.warning(str(exc))
            continue
        project["documents"].append({"name": upload.name, "text": text})
    for doc in list(project["documents"]):
        col_doc, col_remove = st.columns([4, 1])
        col_doc.caption(f"📄 {doc['name']}")
        if col_remove.button("✖", key=f"remove_pdoc_{project['id']}_{doc['name']}"):
            project["documents"] = [
                d for d in project["documents"] if d["name"] != doc["name"]
            ]
            st.rerun()

    if st.button("💾 Save project settings", type="primary", use_container_width=True):
        project["instructions"] = instructions
        project["tools"] = chosen_tools
        if new_memory.strip():
            project["memory"].append(new_memory.strip())
        projects.save_project(project)
        st.toast("Project settings saved")
        st.session_state.project = projects.load_project(project["id"])
        st.rerun()


def _ingest_dock_files(uploads: list, chat_files: list) -> None:
    """Extract text from dock uploads into session chat attachments."""
    added_any = False
    for upload in uploads:
        if any(f["name"] == upload.name for f in chat_files):
            continue
        try:
            file_text = extract_text(upload.name, upload.getvalue())
        except UnsupportedDocumentError as exc:
            st.warning(f"⚠️ {upload.name}: {exc}")
            file_text = f"(File '{upload.name}' attached but: {exc})"
        except Exception as exc:
            st.warning(f"⚠️ Failed to read {upload.name}: {exc}")
            file_text = f"(File '{upload.name}' attached but unreadable: {exc})"
        if not file_text.strip():
            file_text = f"(File '{upload.name}' contained no extractable text.)"
        chat_files.append({"name": upload.name, "text": file_text})
        added_any = True
    if added_any:
        st.toast(f"📎 File(s) attached to this chat")


def render_chat_and_handle_input() -> None:
    """Render past turns and process any new user message (with attachments)."""
    render_history(st.session_state.messages)

    skills = tools.available_skills(st.session_state.project)
    placeholder = (
        "Type your message... (use / for skills)" if skills else "Type your message..."
    )

    # Attached-in-this-chat chips (removable) + project context reminder
    chat_files = st.session_state.get("chat_attachments", [])
    if chat_files:
        chip_cols = st.columns(len(chat_files) + 1)
        for idx, f in enumerate(chat_files):
            if chip_cols[idx].button(
                f"📄 {f['name']} ✖", key=f"rm_chatfile_{idx}",
                help="Remove from this chat",
            ):
                chat_files.pop(idx)
                st.rerun()
        chip_cols[-1].caption("attached in this chat")
    docs = st.session_state.project.get("documents", [])
    if docs:
        chips = "  ".join(f"📄 {d['name']}" for d in docs[:5])
        st.caption(f"In project context: {chips}")

    # Dedicated attachment dock - files ingest immediately on upload (reliable;
    # the chat_input paperclip proved flaky in this environment).
    chat_files = st.session_state.setdefault("chat_attachments", [])
    dock_uploads = st.file_uploader(
        "Attach files to this chat",
        type=[t for t in ATTACHMENT_TYPES],
        accept_multiple_files=True,
        key=f"chat_dock_{st.session_state.project['id']}",
        label_visibility="collapsed",
    )
    if dock_uploads:
        _ingest_dock_files(dock_uploads, chat_files)
        st.toast(f"📎 {len(dock_uploads)} file(s) attached to this chat")

    user_input = st.chat_input(placeholder)
    if not user_input:
        return

    prompt_text = user_input.text if hasattr(user_input, "text") else user_input
    # chat_input paperclip (if used); dock files were already ingested above
    attachments = list(getattr(user_input, "attachments", None) or [])
    for upload in dock_uploads or []:
        if not any(a.name == upload.name for a in attachments) and not any(
            f["name"] == upload.name for f in chat_files
        ):
            attachments.append(upload)
    if not user_input:
        return

    prompt_text = user_input.text if hasattr(user_input, "text") else user_input
    # chat_input paperclip (if used) plus anything in the dock
    attachments = list(getattr(user_input, "attachments", None) or [])
    for upload in dock_uploads or []:
        if not any(a.name == upload.name for a in attachments):
            attachments.append(upload)

    if not st.session_state.session_id:
        st.session_state.session_id = uuid.uuid4().hex

    # Slash command: "/" shows the project's skills, "/<n|fragment> [query]"
    # applies that skill to this message only.
    skills = tools.available_skills(st.session_state.project)
    slash = tools.parse_slash_command(prompt_text, skills)

    # A skill picked by clicking a "/"-menu button applies to the next message.
    if not slash["skill"] and st.session_state.get("pending_skill") is not None:
        slash = dict(slash, skill=st.session_state.pop("pending_skill"))

    if slash["is_slash"] and slash["show_menu"]:
        menu = tools.format_skill_menu(skills)
        st.session_state.messages.append({"role": "user", "content": prompt_text})
        st.session_state.messages.append({"role": "assistant", "content": menu})
        with st.chat_message("user"):
            st.markdown(prompt_text)
        with st.chat_message("assistant"):
            st.markdown(menu)
            st.markdown("**Click a skill to select it for your next message:**")
            for i, skill in enumerate(skills, start=1):
                if st.button(f"/{i}  {skill['name']}", key=f"pick_skill_{i}", use_container_width=True):
                    st.session_state.pending_skill = skill
                    st.session_state.messages.append(
                        {"role": "assistant", "content": f"🧩 Skill **{skill['name']}** selected. Now type your question."}
                    )
                    st.rerun()
        return

    if slash["skill"]:
        skill_label = (
            slash["skill"]["name"] if isinstance(slash["skill"], dict) else str(slash["skill"])
        )
        st.caption(f"🧩 Skill applied: *{skill_label[:80]}*")
    if slash["is_slash"] and slash["skill"]:
        prompt_text = slash["query"] or "Please proceed following the active skill."

    # Chat paperclip = attachments for THIS chat session. Content is embedded in
    # the message AND kept for the session; project docs are untouched (use the
    # sidebar uploader for those).
    chat_files = st.session_state.setdefault("chat_attachments", [])
    attachment_context = ""
    if attachments:
        import logging
        logging.getLogger("agent").warning(
            "CHAT-ATTACH-START files=%s", [a.name for a in attachments]
        )
        names = ", ".join(a.name for a in attachments)
        for attachment in attachments:
            try:
                text = extract_text(attachment.name, attachment.getvalue())
            except UnsupportedDocumentError as exc:
                st.warning(f"⚠️ {attachment.name}: {exc}")
                text = f"(File '{attachment.name}' was attached but: {exc})"
            except Exception as exc:
                st.warning(f"⚠️ Failed to read {attachment.name}: {exc}")
                text = f"(File '{attachment.name}' attached but unreadable: {exc})"
            if not text.strip():
                st.warning(f"⚠️ {attachment.name} produced no readable text.")
                text = f"(File '{attachment.name}' attached but contained no extractable text.)"
            chat_files[:] = [f for f in chat_files if f["name"] != attachment.name]
            chat_files.append({"name": attachment.name, "text": text})
            st.toast(f"📎 {attachment.name} attached to this chat")
            attachment_context += (
                f"\n\n--- Attached file (this message): {attachment.name} ---\n"
                f"{text}\n--- end of {attachment.name} ---\n"
                "Treat the attached files in THIS message as the primary source."
            )

    if not prompt_text.strip() and attachments:
        prompt_text = "Please analyse the attached file(s) and give me a summary of their contents."
        st.caption("No text entered — asking the agent to analyse the attachment(s).")

    display_text = prompt_text
    if slash["skill"]:
        display_text = f"🧩 *{skill_label}*\n\n{prompt_text}"
    if attachments:
        display_text = f"{display_text}\n\n📎 *attached: {names}*"

    st.session_state.messages.append({"role": "user", "content": display_text})
    with st.chat_message("user"):
        st.markdown(display_text)

    effective_prompt = prompt_text + attachment_context

    with st.chat_message("assistant"):
        try:
            reply = generate_reply(effective_prompt, slash["skill"])
        except requests.RequestException as exc:
            st.error(f"⚠️ Request failed: {exc}")
            return

    st.session_state.messages.append({"role": "assistant", "content": reply})

    # Agent decides autonomously whether new facts should enter project memory
    maybe_update_memory()

    sessions_dir = sessions_dir_for_current_project()
    save_session(
        sessions_dir,
        st.session_state.session_id,
        session_title(st.session_state.messages),
        st.session_state.messages,
    )


if __name__ == "__main__":
    main()
