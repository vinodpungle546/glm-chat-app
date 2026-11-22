#!/usr/bin/env python3
"""The chat agent, without any UI: one turn = routing checks + tool loop + memory.

Used by the API (api/main.py) for the Next.js frontend. Progress is reported through an `emit`
callback as events:

  {"type": "status", "text": "..."}      tool/skill/search activity, long-context progress
  {"type": "delta", "text": "..."}       streamed answer text
  {"type": "reset"}                      the text streamed so far was a tool call: discard it
  {"type": "notice", "text": "..."}      e.g. "transcript processed in 3 parts"
  {"type": "progress", "done": n, "total": m, "text": "..."}

The model has no native tool calling: tools, skills and connectors are requested
as JSON in plain text and parsed with regex (see tools.py). Cloud requests go
through llm_backends unchanged.
"""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Callable
from urllib.parse import urlparse

import requests

import connectors
import llm_backends
import long_context
import local_models
import sensitivity
import tools
from doc_extract import MAX_CHARS, build_document_context
from web_search import build_search_decision_messages, parse_search_decision

log = logging.getLogger("agent")
Emit = Callable[[dict], None]

SYSTEM_PROMPT = "You are a helpful AI agent. Answer concisely."
MAX_TOOL_ROUNDS = 4
TOOL_LIMIT_NOTICE = (
    "You have reached the tool-use limit for this turn. Answer my previous "
    "question now using the information above. Do NOT request any more "
    "tools, skills or connectors - reply in plain text only."
)
TOOL_LOOP_FALLBACK = (
    "⚠️ I couldn't finish this answer: the model kept requesting tools "
    "instead of replying. Please rephrase the question or try again."
)
LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1", "host.docker.internal"}
MEETING_MINUTES_SKILL = {
    "name": "Meeting minutes",
    "description": "Use when asked for minutes, a summary, decisions or action items of an attached meeting transcript.",
    "content": (
        "Write meeting minutes from the transcript with these sections:\n"
        "1. **Summary** (3-5 sentences)\n2. **Decisions**\n"
        "3. **Action items** as a table: Owner | Action | Due date | Timestamp\n"
        "4. **Open questions**\n5. **Key discussion points** with [hh:mm:ss] timestamps.\n"
        "Use speaker names as they appear. Never invent owners or dates; write 'not stated' instead."
    ),
}


@dataclass
class TurnState:
    """Everything one turn reads. `messages` already ends with this turn's user message."""
    project: dict
    messages: list[dict]
    provider: object  # model_registry.ModelProvider
    attachments: list[dict] = field(default_factory=list)  # {"name", "text", "kind"?}
    web_search_enabled: bool = True
    transcript_override: str | None = None
    context_length: int | None = None
    caches: dict = field(default_factory=dict)  # long-context caches kept per session
    imported_facts: list[str] = field(default_factory=list)  # memory facts relevant to this turn
    memory_label: str = "From the user's claude.ai memory:"   # heading for imported_facts in the prompt
    remote_connectors: bool = True                            # False in confidential chats
    extra_tools: list[str] = field(default_factory=list)     # always offered (e.g. "search")
    minutes_skill: dict | None = None                         # replaces MEETING_MINUTES_SKILL

    @property
    def backend(self) -> llm_backends.Backend:
        """The llm_backends.Backend (kind 'local' or 'cloud' + model name) of the chosen model provider."""
        return self.provider.backend

    @property
    def is_local(self) -> bool:
        """True when this turn runs on a local Ollama model (then nothing may leave the Mac)."""
        return self.backend.kind == llm_backends.LOCAL


def _noop(_event: dict) -> None:
    """Default `emit` callback that ignores events (used when the caller doesn't stream progress)."""
    pass


def is_local_url(url: str) -> bool:
    """True when a URL points at this Mac (localhost, 127.0.0.1, ::1 or Docker's host alias).

    Used to tell local MCP connectors (allowed on the private path) from remote ones.
    """
    try:
        return (urlparse(url).hostname or "") in LOCAL_HOSTS
    except ValueError:
        return False


# ---------------------------------------------------------------------------
# Routing checks (before a turn runs)
# ---------------------------------------------------------------------------

def project_documents(project: dict) -> list[dict]:
    """The project's documents that go into every chat of the project.

    Meeting transcripts are left out: a meeting only reaches the model when it is attached to
    *this* chat (Meetings → "Ask about this meeting", or the paperclip). Otherwise a transcript
    saved to a project (as the old Streamlit app did) would be sent with every new chat."""
    return [d for d in project.get("documents", []) if d.get("kind") != "transcript"]


def transcript_sources(state: TurnState) -> list[dict]:
    """Meeting transcripts attached to this chat (never ones stored in the project)."""
    return [f for f in state.attachments if f.get("kind") == "transcript"]


def sensitive_reasons(text: str, project: dict, attachments: list[dict],
                      approved: list[str] | None = None) -> list[str]:
    """Reasons to ask before sending to the cloud (empty = fine to send). Only this chat's
    attachments count: project transcripts are not sent (see project_documents)."""
    reasons = sensitivity.detect(text, attachments, project.get("confidential_terms", []))
    if reasons and set(reasons) <= set(approved or []):
        return []
    return reasons


def transcripts_text(state: TurnState) -> str:
    """All meeting transcripts attached to this chat as one text block, each with a heading."""
    return "\n\n".join(f"--- Meeting transcript: {t['name']} ---\n{t['text']}"
                       for t in transcript_sources(state))


def context_length_for(model: str) -> int:
    """How many tokens a local model can read at once (its context window).

    Asks Ollama; if Ollama can't be reached, falls back to the server's configured size.
    """
    try:
        return local_models.model_info(model)["context_length"]
    except local_models.OllamaUnavailable:
        return local_models.SERVER_CONTEXT_LENGTH


def plan_long_context(state: TurnState, prompt: str) -> dict | None:
    """Size check for transcripts on the local path (None when no transcript or cloud)."""
    if not state.is_local or not transcript_sources(state):
        return None
    other = "\n".join(
        [d["text"] for d in project_documents(state.project)]
        + [f["text"] for f in state.attachments if f.get("kind") != "transcript"]
        + [m["content"] for m in state.messages]
    )
    other_tokens = long_context.estimate_tokens(other) + 1500
    length = state.context_length or context_length_for(state.backend.model)
    return long_context.plan(prompt, transcripts_text(state), other_tokens, length,
                             llm_backends.MEASURED_SPEEDS.get(state.backend.model))


def prepare_long_context(state: TurnState, prompt: str, plan: dict, emit: Emit = _noop) -> str | None:
    """Run map-reduce or retrieval; returns the text that replaces the transcripts."""
    if plan["mode"] == "direct":
        return None
    text = transcripts_text(state)
    if plan["mode"] == "map_reduce":
        part_cache = state.caches.setdefault("long_notes", {}).setdefault(state.backend.model, {})
        started = time.time()

        def progress(done: int, total: int) -> None:
            """Reports 'part N of M' (and time left) to the browser while long notes are made."""
            left = ""
            if done:
                left = f" · ~{long_context.fmt_duration((time.time() - started) / done * (total - done))} left"
            emit({"type": "progress", "done": done, "total": total,
                  "text": f"Reading the meeting: part {min(done + 1, total)} of {total}{left}"})

        notes = long_context.map_reduce_notes(
            text, prompt, long_context.content_budget(plan["context_length"]),
            lambda msgs: state.provider.complete(msgs), progress, cache=part_cache,
        )
        return (f"## Meeting transcript notes\nThe full transcript (~{plan['transcript_tokens'] // 1000}k "
                f"tokens) was processed locally in {len(notes)} parts; these notes cover the whole "
                "meeting. Answer from them and cite timestamps.\n\n" + "\n\n".join(notes))
    emit({"type": "status", "text": "🔎 Searching the transcript locally…"})
    passages, method = long_context.retrieve(text, prompt, cache=state.caches.setdefault("embed", {}))
    return ("## Most relevant transcript passages\nRetrieved locally from the full transcript "
            f"({method}); timestamps preserved. Answer from these and cite timestamps. If they "
            "don't contain the answer, say so.\n\n" + "\n…\n".join(passages))


# ---------------------------------------------------------------------------
# Turn context
# ---------------------------------------------------------------------------

def build_chat_attachment_context(state: TurnState) -> str:
    """The text of the files attached to this chat, ready to put in the system prompt.

    Transcripts are cut at MAX_CHARS for cloud models (local models get the full text), and are left out when
    long-context processing already replaced them with notes (`transcript_override`).
    """
    if not state.attachments:
        return ""
    parts = ["The user attached the following files in this chat session. "
             "Prefer them over project context documents when answering:"]
    override = state.transcript_override
    for f in state.attachments:
        text = f["text"]
        if f.get("kind") == "transcript":
            if override:
                continue  # replaced by locally processed notes/passages below
            if not state.is_local and len(text) > MAX_CHARS:
                text = text[:MAX_CHARS] + f"\n...[truncated at {MAX_CHARS} characters for the cloud model]"
        parts.append(f"--- Chat attachment: {f['name']} ---\n{text}")
    if override:
        parts.append(override)
    return "\n\n".join(parts)


def build_turn_context(state: TurnState, turn_skill=None) -> dict:
    """Everything the model needs for this turn, built once per turn.

    Returns a dict with the system prompt (instructions, memory, documents, skills, tools, connectors), the
    tools the model may call (`allowed`), the skills and the connectors. Privacy guards are applied here: a
    local model never gets web search or remote connectors, and a confidential chat never gets remote
    connectors.
    """
    project = state.project
    document_context = build_document_context(project_documents(project))
    chat_context = build_chat_attachment_context(state)
    if chat_context:
        document_context = (document_context + "\n\n" + chat_context) if document_context else chat_context
    # Tools the model may call: the project's tools plus always-on ones (such as search), only if they exist in
    # the registry. dict.fromkeys removes duplicates and keeps the order.
    allowed = [t for t in list(project.get("tools", [])) + state.extra_tools if t in tools.TOOL_REGISTRY]
    allowed = list(dict.fromkeys(allowed))
    global_connectors = connectors.active_connectors()
    if state.is_local or not state.remote_connectors:
        # Hard guard: nothing leaves the Mac on the local path or in a confidential chat.
        if state.is_local:
            allowed = [t for t in allowed if t != "web_search"]
        global_connectors = [c for c in global_connectors if not connectors.is_remote(c)]
    # A copy of the project with the filtered tool list, so the prompt only offers tools that can run.
    prompt_project = dict(project, tools=allowed)
    minutes = state.minutes_skill or MEETING_MINUTES_SKILL
    # Offer the built-in meeting-minutes skill only when a meeting is attached to this chat.
    if transcript_sources(state) and not any(
        s["name"] == minutes["name"] for s in tools.available_skills(project)
    ):
        prompt_project["skills"] = list(project.get("skills", [])) + [minutes]
    memory_block = tools.build_memory_block(project)
    if state.imported_facts:
        imported = "\n".join(f"- {fact}" for fact in state.imported_facts)
        memory_block = (memory_block + "\n" if memory_block else "") + state.memory_label + "\n" + imported
    system_prompt = tools.build_agent_system_prompt(
        SYSTEM_PROMPT, prompt_project, document_context, memory_block, turn_skill
    )
    block = connectors_block(global_connectors)
    if block:
        system_prompt += "\n\n" + block
    return {"project": project, "system_prompt": system_prompt, "allowed": allowed,
            "skills": tools.available_skills(prompt_project), "connectors": global_connectors}


# ---------------------------------------------------------------------------
# Tool / skill / connector requests in plain-text replies
# ---------------------------------------------------------------------------

def connectors_block(global_connectors: list[dict]) -> str:
    """The connectors the model may call, with their tools (only when there are any)."""
    if not global_connectors:
        return ""
    lines = ["## Connectors",
             "You can call these connectors (MCP servers). To call one, reply with ONLY this JSON and nothing else:",
             '{"connector": "<connector name>", "tool": "<tool name>", "input": {<arguments>}}']
    for connector in global_connectors:
        line = f"- {connector['name']}" + (f": {connector['description']}" if connector.get("description") else "")
        tool_list = connector.get("tools") or []
        if tool_list:
            line += ". Tools: " + "; ".join(f"{t['name']}" + (f" ({t['description'][:120]})" if t.get("description") else "")
                                            for t in tool_list[:20])
        lines.append(line)
    return "\n".join(lines)


def parse_connector_call(text: str, global_connectors: list[dict]) -> tuple[dict, str] | None:
    """Reads a connector request out of the model's reply.

    The model asks for a connector by writing JSON such as {"connector": "github", "tool": "search", "input":
    {...}}. Returns (connector, input as text) when the name matches an active connector, otherwise None.
    """
    if not text or not global_connectors:
        return None
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        return None
    try:
        data = json.loads(match.group(0))
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict) or "connector" not in data:
        return None
    name = data.get("connector")
    for connector in global_connectors:
        if connector["name"].lower() == str(name).strip().lower():
            tool_input = data.get("input", "")
            if connector.get("transport") and isinstance(tool_input, dict):
                tool_input = json.dumps(tool_input)
            tool = str(data.get("tool") or "").strip()
            if tool:
                connector = dict(connector, _tool=tool)
            return connector, tool_input if isinstance(tool_input, str) else str(tool_input)
    return None


def parse_unavailable_request(text: str) -> str | None:
    """Name of a tool/skill/connector requested that can't run (reply is only that JSON)."""
    stripped = re.sub(r"^```(?:json)?\s*|\s*```$", "", (text or "").strip())
    if not (stripped.startswith("{") and stripped.endswith("}")):
        return None
    try:
        data = json.loads(stripped)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    for key in ("tool", "skill", "connector"):
        if isinstance(data.get(key), str) and data[key].strip():
            return data[key].strip()
    return None


def detect_action(reply: str, ctx: dict) -> dict | None:
    """Decides whether the model's reply is a request to do something instead of an answer.

    Checks, in order: a skill request, a tool call, a connector call, and a request for something that isn't
    available. Returns a small dict describing the action, or None when the reply is a normal answer.
    """
    skill_request = tools.parse_skill_request(reply, ctx["skills"])
    if skill_request:
        return {"kind": "skill", "name": skill_request["name"], "skill": skill_request}
    tool_call = tools.parse_tool_call(reply, ctx["allowed"])
    if tool_call:
        return {"kind": "tool", "name": tool_call["tool"], "input": tool_call["input"]}
    connector_call = parse_connector_call(reply, ctx["connectors"])
    if connector_call:
        connector, tool_input = connector_call
        return {"kind": "connector", "name": connector["name"], "connector": connector, "input": tool_input}
    unavailable = parse_unavailable_request(reply)
    if unavailable:
        return {"kind": "unavailable", "name": unavailable, "input": ""}
    return None


def tool_output_message(result: str, label: str) -> dict:
    """Wraps a tool's result as a user message that is fed back to the model on the next round."""
    return {"role": "user",
            "content": f"[Tool output — {label}]\n{result}\n\nUse this to answer my previous question."}


def execute_action(state: TurnState, action: dict, ctx: dict, emit: Emit) -> tuple[str, str]:
    """Runs what the model asked for and returns (result text, label for the transcript).

    Skills return their full instructions, tools run through tools.run_tool, connectors call the MCP server,
    and an unavailable request returns an explanation the model can use.
    """
    if action["kind"] == "unavailable":
        why = (" Web search and remote connectors are disabled because this chat runs on the "
               "local model (offline)." if state.is_local else "")
        available = ", ".join(ctx["allowed"]) or "none"
        return (f"'{action['name']}' is not available.{why} Available tools: {available}. "
                "Answer in plain text without it; if you can't, say what you would need."), "tool not available"
    if action["kind"] == "skill":
        skill = action["skill"]
        emit({"type": "status", "text": f"🧩 Using skill “{skill['name']}”"})
        return f"Full content of skill '{skill['name']}':\n{skill['content'] or skill['name']}", f"skill {skill['name']}"
    if action["kind"] == "tool":
        emit({"type": "status", "text": f"🔧 Using tool “{action['name']}”" if action["name"] != "search"
              else f"🔎 Searching for “{action['input'][:80]}”"})
        user_text = next((m["content"] for m in reversed(state.messages) if m["role"] == "user"), "")
        context = dict(ctx["project"], _turn={"local": state.is_local, "user": user_text})
        return tools.run_tool(action["name"], action["input"], context), f"`{action['name']}` tool"
    emit({"type": "status", "text": f"🔌 Using connector “{action['name']}”"})
    tool = action["connector"].get("_tool")
    if tool:
        return (connectors.execute_connector(action["connector"], action["input"], tool),
                f"`{action['name']}` connector ({tool})")
    return connectors.execute_connector(action["connector"], action["input"]), f"`{action['name']}` connector"


def stream_round(state: TurnState, api_messages: list[dict], ctx: dict, emit: Emit) -> tuple[str, dict | None]:
    """Stream one reply to the UI; if it turns out to be a tool request, tell the UI to drop it."""
    chunks = []
    for chunk in state.provider.stream(api_messages):
        chunks.append(chunk)
        emit({"type": "delta", "text": chunk})
    reply = "".join(chunks)
    action = detect_action(reply, ctx)
    if action:
        emit({"type": "reset"})  # never leave raw tool-call JSON on screen
    return reply, action


def get_search_decision(state: TurnState) -> dict:
    """Asks the model (a short, non-streaming call) whether this question needs a web search.

    Returns {'needs_search': bool, 'query': str}; any error means 'no search'.
    """
    messages = build_search_decision_messages(state.messages)
    try:
        content = state.provider.complete(messages, max_tokens=llm_backends.BACKGROUND_MAX_TOKENS)
    except (requests.RequestException, KeyError, IndexError, ValueError):
        return {"needs_search": False}
    return parse_search_decision(content)


def generate_reply(state: TurnState, user_input: str, turn_skill=None, emit: Emit = _noop) -> str:
    """Agent reply with up to MAX_TOOL_ROUNDS tool rounds."""
    ctx = build_turn_context(state, turn_skill)
    base_messages = [{"role": "system", "content": ctx["system_prompt"]}] + state.messages
    last_content = state.messages[-1]["content"] if state.messages else None
    # The API already put this turn's message in state.messages; only add user_input when it differs (for
    # example when a slash command rewrote the question).
    if user_input and user_input.strip() and user_input != last_content:
        base_messages.append({"role": "user", "content": user_input})
    turn_messages: list[dict] = []

    # Step 1 (optional): ask the model whether it needs fresh web results; if so, search first and feed the
    # results in as a tool output.
    if "web_search" in ctx["allowed"] and state.web_search_enabled:
        decision = get_search_decision(state)
        if decision["needs_search"]:
            emit({"type": "status", "text": f"🌐 Searching the web for “{decision['query']}”"})
            user_text = next((m["content"] for m in reversed(state.messages) if m["role"] == "user"), "")
            result = tools.run_tool("web_search", decision["query"], {"_turn": {"local": state.is_local, "user": user_text}})
            turn_messages.append(tool_output_message(result, f"web search results for “{decision['query']}”"))

    log.info("AGENT-REQUEST %s | sys_len=%s", [f"{m['role']}:{len(m['content'])}ch" for m in base_messages],
             len(ctx["system_prompt"]))
    # Step 2: the tool loop. Each round streams a reply. If the reply asks for a tool, skill or connector, run
    # it, add the request and its result to the conversation and ask again. `executed` remembers calls already
    # made, so an identical repeat is answered with 'you already have that result'.
    executed: dict[tuple, str] = {}
    for _round in range(MAX_TOOL_ROUNDS):
        reply, action = stream_round(state, base_messages + turn_messages, ctx, emit)
        if action is None:
            return reply
        key = (action["kind"], action["name"], action.get("input", ""))
        if key in executed:
            turn_messages += [{"role": "assistant", "content": reply},
                              {"role": "user", "content": "You already have that result above. "
                               "Answer my previous question now in plain text."}]
            continue
        result, label = execute_action(state, action, ctx, emit)
        executed[key] = result
        turn_messages += [{"role": "assistant", "content": reply}, tool_output_message(result, label)]

    # Step 3: out of rounds. Ask once more for a plain answer; if the model still wants a tool, show a friendly
    # fallback message instead of raw JSON.
    turn_messages.append({"role": "user", "content": TOOL_LIMIT_NOTICE})
    reply, action = stream_round(state, base_messages + turn_messages, ctx, emit)
    if action is not None:
        emit({"type": "delta", "text": TOOL_LOOP_FALLBACK})
        return TOOL_LOOP_FALLBACK
    return reply


# ---------------------------------------------------------------------------
# Long-term memory
# ---------------------------------------------------------------------------

def merge_new_memories(project: dict, extracted: list) -> list[str]:
    """Adds up to two new facts to the project's memory list, skipping duplicates.

    Ignores anything that isn't a non-empty string. Returns the facts that were added.
    """
    existing = {entry.strip().lower() for entry in project["memory"]}
    added = []
    for item in [s for s in extracted if isinstance(s, str) and s.strip()][:2]:
        if item.strip().lower() not in existing:
            project["memory"].append(item.strip())
            existing.add(item.strip().lower())
            added.append(item.strip())
    return added


def extract_memories(state: TurnState) -> list[str]:
    """Ask the model for up to 2 new durable facts from the recent conversation."""
    recent = state.messages[-6:]
    if len(recent) < 2:
        return []
    transcript = "\n".join(f"{m['role']}: {m['content'][:500]}" for m in recent)
    messages = [
        {"role": "system", "content": (
            "You maintain long-term memory for a chat project.\nExisting memory entries:\n"
            + ("\n".join(f"- {m}" for m in state.project["memory"]) or "(none)")
            + "\n\nGiven this conversation excerpt, extract up to 2 NEW durable facts "
            "worth remembering long-term (user preferences, project facts, decisions). "
            "Skip anything already covered by existing memory or trivial small talk. "
            "Reply ONLY with a JSON array of short strings, e.g. "
            '["User prefers Python", "Deploy target is GCP"]. Reply [] if nothing worth storing.')},
        {"role": "user", "content": transcript},
    ]
    try:
        if state.is_local:
            content = state.provider.complete(messages, max_tokens=llm_backends.BACKGROUND_MAX_TOKENS)
        else:
            content = state.provider.complete(messages, payload_order="stream_first")
        match = re.search(r"\[.*\]", content, re.DOTALL)
        extracted = json.loads(match.group(0)) if match else []
    except (requests.RequestException, KeyError, IndexError, ValueError, json.JSONDecodeError):
        return []
    return extracted if isinstance(extracted, list) else []
