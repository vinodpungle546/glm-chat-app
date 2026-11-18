#!/usr/bin/env python3
"""Tests for tools.py: registry, tool-call parsing, prompt assembly."""

import tools as tl


# ---------------------------------------------------------------------------
# Registry / run_tool
# ---------------------------------------------------------------------------

def test_builtin_tools_registered():
    assert "web_search" in tl.TOOL_REGISTRY
    assert "datetime" in tl.TOOL_REGISTRY


def test_run_tool_unknown_returns_error_message():
    result = tl.run_tool("does_not_exist", "", {})
    assert "unknown tool" in result


def test_run_tool_datetime():
    result = tl.run_tool("datetime", "", {})
    assert "Current date and time" in result


def test_run_tool_catches_exceptions():
    def boom(_input, _context):
        raise RuntimeError("kaput")

    tl.register_tool("boom", "always fails", boom)
    result = tl.run_tool("boom", "", {})
    assert "Error running tool 'boom'" in result
    assert "kaput" in result  # exception text surfaces for debugging


# ---------------------------------------------------------------------------
# parse_tool_call
# ---------------------------------------------------------------------------

def test_parse_tool_call_plain_json():
    call = tl.parse_tool_call('{"tool": "web_search", "input": "btc price"}', ["web_search"])
    assert call == {"tool": "web_search", "input": "btc price"}


def test_parse_tool_call_wrapped_in_prose():
    call = tl.parse_tool_call('I will search: {"tool": "web_search", "input": "q"}', ["web_search"])
    assert call["tool"] == "web_search"


def test_parse_tool_call_in_code_fence():
    text = '```json\n{"tool": "web_search", "input": "q"}\n```'
    assert tl.parse_tool_call(text, ["web_search"])["input"] == "q"


def test_parse_tool_call_rejects_disallowed_tool():
    assert tl.parse_tool_call('{"tool": "rm_rf", "input": ""}', ["web_search"]) is None


def test_parse_tool_call_plain_answer_returns_none():
    assert tl.parse_tool_call("The capital of France is Paris.", ["web_search"]) is None


def test_parse_tool_call_empty_returns_none():
    assert tl.parse_tool_call("", ["web_search"]) is None


def test_parse_tool_call_broken_json_returns_none():
    assert tl.parse_tool_call("{tool: web_search}", ["web_search"]) is None


def test_parse_tool_call_coerces_non_string_input():
    call = tl.parse_tool_call('{"tool": "web_search", "input": 42}', ["web_search"])
    assert call["input"] == "42"


# ---------------------------------------------------------------------------
# Memory block / system prompt assembly
# ---------------------------------------------------------------------------

def make_project(**overrides) -> dict:
    project = {
        "id": "p1",
        "name": "Test",
        "instructions": "",
        "skills": [],
        "tools": [],
        "memory": [],
        "documents": [],
    }
    project.update(overrides)
    return project


def test_build_agent_prompt_base_only():
    prompt = tl.build_agent_system_prompt("Be helpful.", make_project(), "", "")
    assert prompt == "Be helpful."


def test_build_agent_prompt_includes_instructions():
    prompt = tl.build_agent_system_prompt("Base.", make_project(instructions="Be terse."), "", "")
    assert "## Project instructions" in prompt
    assert "Be terse." in prompt


def test_build_agent_prompt_includes_skills():
    prompt = tl.build_agent_system_prompt(
        "Base.", make_project(skills=["Always cite sources", ""]), "", ""
    )
    assert "## Available skills" in prompt
    assert "Always cite sources" in prompt


def test_build_agent_prompt_lists_tools_with_protocol():
    prompt = tl.build_agent_system_prompt(
        "Base.", make_project(tools=["web_search"]), "", ""
    )
    assert "- web_search:" in prompt
    assert '"tool"' in prompt and '"input"' in prompt


def test_build_agent_prompt_skips_unknown_tools():
    prompt = tl.build_agent_system_prompt("Base.", make_project(tools=["nonexistent"]), "", "")
    assert "Available tools" not in prompt


def test_build_agent_prompt_includes_documents_and_memory():
    prompt = tl.build_agent_system_prompt(
        "Base.",
        make_project(memory=["User likes Python"]),
        "Attached docs here",
        "- User prefers Python",
    )
    assert "Attached docs here" in prompt
    assert "- User prefers Python" in prompt


def test_build_memory_block_formats_entries():
    block = tl.build_memory_block(make_project(memory=["a", " ", "b"]))
    assert block == "- a\n- b"


def test_build_memory_block_empty():
    assert tl.build_memory_block(make_project()) == ""
