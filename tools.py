#!/usr/bin/env python3
"""Skills and tools registry for the GLM agent chat app.

A *tool* is a callable the agent can invoke during a turn
(built-ins: web search, current datetime; extend with your own).

A *skill* is a reusable instruction snippet the user attaches to a project;
active skills are injected into the system prompt so the agent follows the
same procedure every time (e.g. "always answer with a summary table").

The agent requests a tool by replying with ONLY:
    {"tool": "<tool name>", "input": "<input for the tool>"}
The app runs the tool, feeds the result back, and the model then answers.

Slash commands: typing "/" in the chat input shows the project's skills;
"/1" or "/<fragment>" applies a skill to the current message.
"""

from __future__ import annotations

import json
import re
from datetime import datetime

import web_search

# ---------------------------------------------------------------------------
# Tool registry
# ---------------------------------------------------------------------------

TOOL_REGISTRY: dict[str, dict] = {}


def register_tool(name: str, description: str, runner) -> None:
    """Register a tool. runner(input, context) -> str result."""
    TOOL_REGISTRY[name] = {"description": description, "runner": runner}


def run_tool(name: str, tool_input: str, context: dict) -> str:
    """Execute a registered tool by name; returns a human-readable result."""
    tool = TOOL_REGISTRY.get(name)
    if tool is None:
        return f"Error: unknown tool '{name}'."
    try:
        return str(tool["runner"](tool_input, context))
    except Exception as exc:  # tool failures must not crash the chat turn
        return f"Error running tool '{name}': {exc}"


# ---------------------------------------------------------------------------
# Built-in tools
# ---------------------------------------------------------------------------

def _tool_web_search(query: str, _context: dict) -> str:
    results = web_search.search_web(query)
    return web_search.format_results(results)


def _tool_datetime(_query: str, _context: dict) -> str:
    now = datetime.now()
    return f"Current date and time: {now.strftime('%A, %B %d, %Y at %H:%M')}"


register_tool(
    "web_search",
    "Search the public web for up-to-date information. Input: a search query.",
    _tool_web_search,
)
register_tool(
    "datetime",
    "Get the current date and time. Input is ignored.",
    _tool_datetime,
)


# ---------------------------------------------------------------------------
# Tool-call parsing
# ---------------------------------------------------------------------------

def parse_tool_call(text: str, allowed_tools: list[str]) -> dict | None:
    """Return {'tool', 'input'} if the reply is a tool call, else None.

    A tool call is a JSON object with a "tool" key naming an allowed tool,
    optionally wrapped in prose or code fences.
    """
    if not text or not text.strip():
        return None
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        return None
    try:
        data = json.loads(match.group(0))
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    name = data.get("tool")
    if not isinstance(name, str) or name not in allowed_tools:
        return None
    tool_input = data.get("input", "")
    return {"tool": name, "input": tool_input if isinstance(tool_input, str) else str(tool_input)}


# ---------------------------------------------------------------------------
# Prompt assembly
# ---------------------------------------------------------------------------

def build_agent_system_prompt(
    base_prompt: str,
    project: dict,
    document_context: str,
    memory_block: str,
    turn_skill: str | None = None,
) -> str:
    """Assemble the full system prompt from project config + context."""
    sections = [base_prompt.strip()]

    if project.get("instructions", "").strip():
        sections.append("## Project instructions\n" + project["instructions"].strip())

    skills = available_skills(project)
    if skills:
        sections.append(
            "## Available skills\n"
            + skills_index_block(skills)
            + "\n\nYou decide autonomously whether a skill is needed - do not wait "
            "for the user to ask. Before answering, check the descriptions: if a "
            "skill matches the task, use it. To use a skill, reply with ONLY a "
            "JSON object and nothing else:\n"
            '{"skill": "<skill name>"}\n'
            "You will then receive the full skill content and must follow it."
        )

    if turn_skill:
        sections.append(build_skill_directive(turn_skill))

    allowed = [t for t in project.get("tools", []) if t in TOOL_REGISTRY]
    if allowed:
        tool_lines = "\n".join(
            f"- {name}: {TOOL_REGISTRY[name]['description']}" for name in allowed
        )
        sections.append(
            "## Available tools\n"
            f"{tool_lines}\n\n"
            "To use a tool, reply with ONLY a JSON object and nothing else:\n"
            '{"tool": "<tool name>", "input": "<input for the tool>"}\n'
            "You will then receive the tool result and must answer the user with it."
        )

    if document_context:
        sections.append(document_context)

    if memory_block:
        sections.append("## Long-term memory\n" + memory_block)

    return "\n\n".join(sections)


def build_memory_block(project: dict) -> str:
    """Format the project's long-term memory entries as one block."""
    entries = [m for m in project.get("memory", []) if isinstance(m, str) and m.strip()]
    return "\n".join(f"- {entry}" for entry in entries)


# ---------------------------------------------------------------------------
# Slash commands ("/" to pick a skill for the current message)
# ---------------------------------------------------------------------------

def available_skills(project: dict) -> list[dict]:
    """Project skills normalized to [{'name','description','content'}].

    Accepts legacy plain-string skills (name == content, empty description).
    """
    normalized = []
    for skill in project.get("skills", []):
        if isinstance(skill, str) and skill.strip():
            normalized.append(
                {"name": skill.strip(), "description": "", "content": skill.strip()}
            )
        elif isinstance(skill, dict) and str(skill.get("name", "")).strip():
            normalized.append(
                {
                    "name": str(skill["name"]).strip(),
                    "description": str(skill.get("description", "")).strip(),
                    "content": str(skill.get("content", "")).strip(),
                }
            )
    return normalized


def skills_index_block(skills: list[dict]) -> str:
    """Progressive-disclosure index: name + description only (Claude-style)."""
    lines = []
    for i, skill in enumerate(skills, start=1):
        desc = skill["description"] or "(no description)"
        lines.append(f"- /{i} {skill['name']}: {desc}")
    return "\n".join(lines)


def format_skill_menu(skills: list[str]) -> str:
    """Render the slash-command help text for the given skills."""
    if not skills:
        return (
            "No skills are configured for this project.\n\n"
            "Add skills under **Project configuration > Skills** (one per line), "
            "then type `/` to use them."
        )
    lines = ["**Available skills - type `/` plus the number or a name fragment:**"]
    for i, skill in enumerate(skills, start=1):
        short = skill if len(skill) <= 60 else skill[:57] + "..."
        lines.append(f"- `/{i}` - {short}")
    lines.append("\nExample: `/2 Explain REST vs GraphQL` applies skill 2 to that question.")
    return "\n".join(lines)


def parse_slash_command(text: str, skills: list[str]) -> dict:
    """Interpret a message starting with '/'.

    Returns a dict:
      is_slash:  True when the message starts with '/'
      skill:     matched skill text, or None
      query:     remainder of the message after the command
      show_menu: True when the skill menu should be displayed
    """
    stripped = text.strip()
    if not stripped.startswith("/"):
        return {"is_slash": False, "skill": None, "query": text, "show_menu": False}

    body = stripped[1:].strip()
    if not body:
        return {"is_slash": True, "skill": None, "query": "", "show_menu": True}

    # "/2 explain X" or "/table explain X": first token picks the skill,
    # the rest is the actual question for this turn.
    parts = body.split(maxsplit=1)
    first, rest = parts[0], parts[1] if len(parts) > 1 else ""

    for i, skill in enumerate(skills, start=1):
        token = first.lower()
        if first == str(i) or (len(first) >= 3 and skill.lower().startswith(token := first.lower())):
            return {"is_slash": True, "skill": skill, "query": rest, "show_menu": False}
        if first == str(i) or first.lower() in skill.lower():
            return {"is_slash": True, "skill": skill, "query": rest, "show_menu": False}

    return {"is_slash": True, "skill": None, "query": rest, "show_menu": True}


def build_skill_directive(skill) -> str:
    """System-prompt directive applying a skill to the current turn only."""
    if isinstance(skill, dict):
        text = skill.get("content") or skill.get("name", "")
    else:
        text = str(skill)
    return (
        "## Skill directive (this turn)\n"
        "The user activated the following skill with a '/' command. "
        f"Follow it strictly for this reply:\n{text}"
    )


def parse_skill_request(text: str, skills: list[dict]) -> dict | None:
    """Return the requested skill if the model reply asks for one via JSON."""
    if not text or not text.strip():
        return None
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        return None
    try:
        data = json.loads(match.group(0))
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    name = data.get("skill")
    if not isinstance(name, str):
        return None
    for skill in skills:
        if skill["name"].lower() == name.strip().lower():
            return skill
    return None
