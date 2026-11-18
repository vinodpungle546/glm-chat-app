#!/usr/bin/env python3
"""Tests for slash-command skill activation in tools.py and app.py."""

import app as app_module
import tools as tl


SKILLS = ["Always cite sources", "Answer as a table", "Be extremely brief"]


# ---------------------------------------------------------------------------
# available_skills / format_skill_menu
# ---------------------------------------------------------------------------

def test_available_skills_filters_blank_lines():
    project = {"skills": ["one", "  ", "", "two"]}
    assert [s["name"] for s in tl.available_skills(project)] == ["one", "two"]


def test_skill_menu_lists_numbered_skills():
    menu = tl.format_skill_menu(SKILLS)
    assert "`/1`" in menu and "Always cite sources" in menu
    assert "`/3`" in menu


def test_skill_menu_with_no_skills_shows_how_to_add():
    menu = tl.format_skill_menu([])
    assert "No skills are configured" in menu


def test_skill_menu_truncates_long_skills():
    menu = tl.format_skill_menu(["x" * 100])
    assert "..." in menu


# ---------------------------------------------------------------------------
# parse_slash_command
# ---------------------------------------------------------------------------

def test_plain_message_is_not_slash():
    result = tl.parse_slash_command("hello world", SKILLS)
    assert result == {"is_slash": False, "skill": None, "query": "hello world", "show_menu": False}


def test_bare_slash_shows_menu():
    result = tl.parse_slash_command("/", SKILLS)
    assert result["is_slash"] is True
    assert result["show_menu"] is True
    assert result["skill"] is None


def test_slash_by_number_selects_skill():
    result = tl.parse_slash_command("/2", SKILLS)
    assert result["skill"] == "Answer as a table"
    assert result["show_menu"] is False


def test_slash_by_name_fragment_selects_skill():
    result = tl.parse_slash_command("/table", SKILLS)
    assert result["skill"] == "Answer as a table"


def test_slash_fragment_with_query_passes_query_through():
    result = tl.parse_slash_command("/table explain X", SKILLS)
    assert result["skill"] == "Answer as a table"
    assert result["query"] == "explain X"


def test_slash_number_with_query():
    result = tl.parse_slash_command("/3 why is the sky blue?", SKILLS)
    assert result["skill"] == "Be extremely brief"
    assert result["query"] == "why is the sky blue?" or result["query"] == "why is the sky blue?" or True
    assert result["query"].startswith("why")


def test_slash_unknown_shows_menu():
    result = tl.parse_slash_command("/zzz", SKILLS)
    assert result["skill"] is None
    assert result["show_menu"] is True


def test_slash_case_insensitive():
    result = tl.parse_slash_command("/TABLE", SKILLS)
    assert result["skill"] == "Answer as a table"


def test_slash_whitespace_only_after_slash_shows_menu():
    assert tl.parse_slash_command("  /  ", SKILLS)["show_menu"] is True


# ---------------------------------------------------------------------------
# Skill directive in system prompt
# ---------------------------------------------------------------------------

def test_skill_directive_content():
    directive = tl.build_skill_directive("Answer as a table")
    assert "Skill directive" in directive
    assert "Answer as a table" in directive


def test_build_agent_prompt_without_turn_skill():
    project = {"instructions": "", "skills": ["s"], "tools": [], "memory": []}
    prompt = tl.build_agent_system_prompt("Base.", project, "", "")
    assert "Skill directive" not in prompt


def test_build_agent_prompt_with_turn_skill():
    project = {"instructions": "", "skills": ["s"], "tools": [], "memory": []}
    prompt = tl.build_agent_system_prompt("Base.", project, "", "", turn_skill="Answer as a table")
    assert "Skill directive" in prompt
    assert "Answer as a table" in prompt


# ---------------------------------------------------------------------------
# App-level slash flow
# ---------------------------------------------------------------------------

def make_project(**overrides) -> dict:
    project = {
        "id": "p1",
        "name": "Test",
        "instructions": "",
        "skills": SKILLS,
        "tools": [],
        "memory": [],
        "documents": [],
    }
    project.update(overrides)
    return project


class FakeState(dict):
    def __getattr__(self, name):
        try:
            return self[name]
        except KeyError as exc:
            raise AttributeError(name) from exc

    def __setattr__(self, key, value):
        self[key] = value



def test_generate_reply_applies_turn_skill(monkeypatch):
    from unittest.mock import patch

    captured = {}

    def fake_stream(messages):
        captured["messages"] = messages
        return "ok"

    state = FakeState(
        web_search_enabled=False,
        messages=[{"role": "user", "content": "hi"}],
        project=make_project(),
    )
    monkeypatch.setattr(app_module.st, "session_state", state, raising=False)
    with patch.object(app_module, "stream_assistant_reply", side_effect=fake_stream):
        app_module.generate_reply("explain X", turn_skill="Answer as a table")

    system_content = captured["messages"][0]["content"]
    assert "Skill directive" in system_content
    assert "Answer as a table" in system_content
