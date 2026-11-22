#!/usr/bin/env python3
"""The agent turn (agent.py) the web app runs: tool loop, skills, connectors, attachments,
local-path guards and memory extraction. Ported from the Streamlit app's tests; no network."""

from __future__ import annotations

from unittest.mock import patch

import pytest

import agent
import llm_backends
import tools


class FakeProvider:
    """A chat model that replies from a script: script(messages, call_number) -> text."""

    def __init__(self, script=None, local: bool = False, complete_reply: str = "[]"):
        self.script = script or (lambda messages, n: "ok")
        self.local = local
        self.complete_reply = complete_reply
        self.calls: list[list[dict]] = []
        self.completions: list[list[dict]] = []

    @property
    def backend(self):
        return llm_backends.local("qwen3:8b") if self.local else llm_backends.cloud()

    def stream(self, messages):
        self.calls.append(messages)
        yield self.script(messages, len(self.calls))

    def complete(self, messages, max_tokens=None, temperature=None, payload_order="messages_first"):
        self.completions.append(messages)
        return self.complete_reply


def project(**overrides) -> dict:
    p = {"id": "p1", "name": "T", "instructions": "", "skills": [], "tools": [], "memory": [],
         "documents": [], "confidential_terms": []}
    p.update(overrides)
    return p


def state(script=None, local=False, messages=None, **kw) -> agent.TurnState:
    kw.setdefault("web_search_enabled", False)
    return agent.TurnState(project=kw.pop("project", project()), provider=FakeProvider(script, local),
                           messages=messages if messages is not None else [{"role": "user", "content": "q"}], **kw)


@pytest.fixture(autouse=True)
def no_connectors(monkeypatch):
    monkeypatch.setattr(agent.connectors, "load_connectors", lambda: [])


@pytest.fixture
def two_connectors(monkeypatch):
    monkeypatch.setattr(agent.connectors, "load_connectors", lambda: [
        {"id": "1", "kind": "mcp", "name": "remote", "url": "https://example.com/mcp", "description": ""},
        {"id": "2", "kind": "mcp", "name": "onbox", "url": "http://localhost:9000/mcp", "description": ""},
    ])


def run(s: agent.TurnState, user_input: str = "q", tool_result: str = "RESULT", **kw):
    with patch.object(agent.tools, "run_tool", return_value=tool_result) as run_tool:
        reply = agent.generate_reply(s, user_input, **kw)
    return reply, s.provider.calls, run_tool


# --- Plain replies and the system prompt ---------------------------------------

def test_plain_reply_sends_system_prompt_and_history():
    s = state(lambda m, n: "Hello!", messages=[{"role": "user", "content": "hi"}])
    reply, calls, _ = run(s, "hi")
    assert reply == "Hello!" and len(calls) == 1
    assert calls[0][0]["role"] == "system" and agent.SYSTEM_PROMPT in calls[0][0]["content"]
    assert calls[0][1:] == [{"role": "user", "content": "hi"}]


def test_documents_instructions_and_memory_reach_the_system_prompt():
    s = state(project=project(instructions="Be terse.", memory=["User likes Rust"],
                              documents=[{"name": "doc.txt", "text": "SECRET-CODE-123"}]))
    run(s)
    system = s.provider.calls[0][0]["content"]
    assert "SECRET-CODE-123" in system and "doc.txt" in system
    assert "Be terse." in system and "- User likes Rust" in system


def test_memory_facts_are_listed_under_their_label():
    s = state(imported_facts=["Prefers tables."], memory_label="From memory:")
    run(s)
    assert "From memory:\n- Prefers tables." in s.provider.calls[0][0]["content"]


def test_chat_attachments_are_preferred_and_project_docs_kept():
    s = state(project=project(documents=[{"name": "project_doc.txt", "text": "PROJECT DOC CONTENT"}]),
              attachments=[{"name": "chat_file.txt", "text": "CHAT FILE SECRET-XYZ"}])
    run(s, "summarize")
    system = s.provider.calls[0][0]["content"]
    assert "Chat attachment: chat_file.txt" in system and "Prefer them over project context" in system
    assert "SECRET-XYZ" in system and "PROJECT DOC CONTENT" in system
    assert agent.build_chat_attachment_context(state()) == ""


def test_turn_skill_adds_the_directive():
    s = state(project=project(skills=["Answer as a table"]))
    run(s, "explain X", turn_skill="Answer as a table")
    system = s.provider.calls[0][0]["content"]
    assert "Skill directive" in system and "Answer as a table" in system


# --- Web search ----------------------------------------------------------------

def test_auto_search_runs_when_the_model_asks_for_it():
    s = state(lambda m, n: "btc is 100k", web_search_enabled=True, project=project(tools=["web_search"]))
    with patch.object(agent, "get_search_decision", return_value={"needs_search": True, "query": "btc price"}):
        reply, calls, run_tool = run(s, "what is btc price?", tool_result="[1] price is 100k")
    assert reply == "btc is 100k"
    assert run_tool.call_args.args[:2] == ("web_search", "btc price")
    assert any("[Tool output" in m["content"] for m in calls[-1])


@pytest.mark.parametrize("enabled, tools_on", [(False, ["web_search"]), (True, [])])
def test_no_search_decision_when_disabled_or_not_a_project_tool(enabled, tools_on):
    s = state(web_search_enabled=enabled, project=project(tools=tools_on))
    with patch.object(agent, "get_search_decision") as decision:
        assert run(s)[0] == "ok"
    decision.assert_not_called()


def test_local_model_never_runs_the_search_decision():
    s = state(local=True, web_search_enabled=True, project=project(tools=["web_search", "datetime"]))
    with patch.object(agent, "get_search_decision") as decision:
        run(s, "latest news?")
    decision.assert_not_called()


def test_search_decision_failure_means_no_search():
    import requests

    class Down(FakeProvider):
        def complete(self, *a, **k):
            raise requests.ConnectionError("api down")

    s = agent.TurnState(project=project(), messages=[{"role": "user", "content": "q"}], provider=Down())
    assert agent.get_search_decision(s) == {"needs_search": False}


# --- Tool loop -------------------------------------------------------------------

def test_web_search_then_tool_call_is_executed():
    """After the automatic web search the model asks for datetime: the tool runs, the raw JSON is
    never the answer."""
    s = state(lambda m, n: '{"tool": "datetime", "input": ""}' if n == 1 else "It is 10:00.",
              web_search_enabled=True, project=project(tools=["datetime", "web_search"]))
    with patch.object(agent, "get_search_decision", return_value={"needs_search": True, "query": "time now"}):
        reply, calls, run_tool = run(s)
    assert reply == "It is 10:00."
    assert [c.args[0] for c in run_tool.call_args_list] == ["web_search", "datetime"]
    assert sum("[Tool output" in m["content"] for m in calls[-1]) == 2


def test_tool_result_is_fed_back_after_the_call():
    s = state(lambda m, n: '{"tool": "datetime", "input": ""}' if n == 1 else "It is Thursday.",
              project=project(tools=["datetime"]))
    events = []
    with patch.object(agent.tools, "run_tool", return_value="Thu, Jan 1") as run_tool:
        reply = agent.generate_reply(s, "q", emit=events.append)
    run_tool.assert_called_once()
    assert reply == "It is Thursday."
    tail = s.provider.calls[1][-2:]
    assert tail[0] == {"role": "assistant", "content": '{"tool": "datetime", "input": ""}'}
    assert tail[1]["role"] == "user" and "[Tool output" in tail[1]["content"] and "Thu, Jan 1" in tail[1]["content"]
    assert {"type": "reset"} in events  # the streamed tool JSON is dropped from the screen


def test_skill_then_tool_chain():
    s = state(lambda m, n: {1: '{"skill": "Clock"}', 2: '{"tool": "datetime", "input": ""}'}.get(n, "Done."),
              project=project(tools=["datetime"], skills=[{"name": "Clock", "description": "time",
                                                           "content": "Call datetime"}]))
    reply, calls, run_tool = run(s)
    assert reply == "Done." and len(calls) == 3
    run_tool.assert_called_once()
    assert any("Full content of skill 'Clock'" in m["content"] for m in calls[-1])


def test_model_loads_a_skill_from_its_description():
    s = state(lambda m, n: "Here is your bar chart." if any("Full content of skill" in x["content"] for x in m)
              else '{"skill": "BarChart"}',
              project=project(skills=[{"name": "BarChart", "description": "Use for bar charts",
                                       "content": "CREATE A BAR CHART"}]))
    assert run(s)[0] == "Here is your bar chart."
    prompt = tools.build_agent_system_prompt("Base", project(skills=[{"name": "S", "description": "d",
                                                                      "content": "c"}]), "", "")
    assert "decide autonomously" in prompt and "Available skills" in prompt and '"skill"' in prompt


def test_round_limit_returns_the_fallback_not_json():
    s = state(lambda m, n: f'{{"tool": "datetime", "input": "{n}"}}', project=project(tools=["datetime"]))
    reply, calls, run_tool = run(s)
    assert reply == agent.TOOL_LOOP_FALLBACK and "{" not in reply
    assert run_tool.call_count == agent.MAX_TOOL_ROUNDS
    assert len(calls) == agent.MAX_TOOL_ROUNDS + 1
    assert agent.TOOL_LIMIT_NOTICE in calls[-1][-1]["content"]


def test_round_limit_uses_a_final_plain_answer():
    s = state(lambda m, n: "Best answer I can give." if agent.TOOL_LIMIT_NOTICE in m[-1]["content"]
              else f'{{"tool": "datetime", "input": "{n}"}}', project=project(tools=["datetime"]))
    assert run(s)[0] == "Best answer I can give."


def test_repeated_identical_call_is_not_rerun():
    s = state(lambda m, n: '{"tool": "datetime", "input": ""}' if n <= 2 else "final",
              project=project(tools=["datetime"]))
    reply, calls, run_tool = run(s)
    assert reply == "final"
    run_tool.assert_called_once()
    assert "already have that result" in calls[2][-1]["content"]


def test_unavailable_tool_is_explained_to_the_model():
    s = state(lambda m, n: '{"tool": "web search", "input": "news"}' if n == 1 else "I can't browse right now.")
    reply, calls, run_tool = run(s)
    run_tool.assert_not_called()
    assert reply == "I can't browse right now."
    assert "'web search' is not available" in calls[1][-1]["content"]


def test_unavailable_tool_on_the_local_model_says_offline():
    s = state(lambda m, n: '{"tool": "web_search", "input": "news"}' if n == 1 else "Offline, sorry.",
              local=True, project=project(tools=["web_search", "datetime"]))
    reply, calls, run_tool = run(s)
    run_tool.assert_not_called()
    assert reply == "Offline, sorry." and "local model (offline)" in calls[1][-1]["content"]


def test_an_answer_containing_example_json_is_not_intercepted():
    text = 'Use this config:\n{"tool": "x"}\nThat is all.'
    s = state(lambda m, n: text)
    reply, calls, _ = run(s)
    assert reply == text and len(calls) == 1


def test_connector_call_runs_and_its_result_is_used(monkeypatch):
    monkeypatch.setattr(agent.connectors, "load_connectors",
                        lambda: [{"id": "c1", "kind": "mcp", "name": "gh", "url": "http://x/mcp", "description": ""}])
    s = state(lambda m, n: "stars: 10k" if any("[Tool output" in x["content"] for x in m)
              else '{"connector": "gh", "input": "stars"}')
    with patch.object(agent.connectors, "execute_connector", return_value="10k stars") as execute:
        assert agent.generate_reply(s, "how many stars?") == "stars: 10k"
    assert execute.call_args.args[1] == "stars"


# --- Local-path and confidential guards ------------------------------------------

def test_cloud_keeps_every_tool_and_connector(two_connectors):
    ctx = agent.build_turn_context(state(project=project(tools=["web_search", "datetime"])))
    assert ctx["allowed"] == ["web_search", "datetime"] and len(ctx["connectors"]) == 2


def test_local_model_drops_web_search_and_remote_connectors(two_connectors):
    ctx = agent.build_turn_context(state(local=True, project=project(tools=["web_search", "datetime"])))
    assert ctx["allowed"] == ["datetime"]
    assert [c["name"] for c in ctx["connectors"]] == ["onbox"]
    assert "web_search" not in ctx["system_prompt"] and "remote" not in ctx["system_prompt"]


def test_confidential_chat_on_the_cloud_drops_remote_connectors_only(two_connectors):
    ctx = agent.build_turn_context(state(remote_connectors=False, project=project(tools=["web_search"])))
    assert ctx["allowed"] == ["web_search"] and [c["name"] for c in ctx["connectors"]] == ["onbox"]


def test_transcripts_are_cut_for_the_cloud_only():
    big = "x" * (agent.MAX_CHARS + 500)
    files = [{"name": "Transcript: big", "text": big, "kind": "transcript"}]
    assert "truncated at" in agent.build_chat_attachment_context(state(attachments=files))
    local = agent.build_chat_attachment_context(state(local=True, attachments=files))
    assert big in local and "truncated" not in local


def test_processed_transcript_notes_replace_the_full_text():
    s = state(local=True, transcript_override="## Meeting transcript notes\nPART NOTES",
              attachments=[{"name": "Transcript: t", "text": "FULL TEXT", "kind": "transcript"},
                           {"name": "notes.txt", "text": "other file"}])
    ctx = agent.build_chat_attachment_context(s)
    assert "FULL TEXT" not in ctx and "PART NOTES" in ctx and "other file" in ctx


def test_meeting_minutes_skill_is_offered_only_with_a_transcript():
    with_transcript = state(attachments=[{"name": "Transcript: t", "text": "x", "kind": "transcript"}])
    assert any(s["name"] == "Meeting minutes" for s in agent.build_turn_context(with_transcript)["skills"])
    assert not any(s["name"] == "Meeting minutes" for s in agent.build_turn_context(state())["skills"])


def test_sensitive_reasons_and_remembered_approval():
    p = project(confidential_terms=["Falcon"])
    assert agent.sensitive_reasons("capital of France?", p, []) == []
    assert agent.sensitive_reasons("Falcon status?", p, [])
    assert "meeting transcript" in agent.sensitive_reasons("summarise", p, [{"name": "T", "kind": "transcript"}])[0]
    reasons = agent.sensitive_reasons("mail a@b.com", p, [])
    assert agent.sensitive_reasons("mail a@b.com", p, [], approved=reasons) == []
    assert agent.sensitive_reasons("mail a@b.com, confidential", p, [], approved=reasons)


# --- Memory --------------------------------------------------------------------------

def test_merge_new_memories_dedupes_and_ignores_non_strings():
    p = {"memory": ["User likes Rust"]}
    assert agent.merge_new_memories(p, ["user likes rust", "Deploys to GCP"]) == ["Deploys to GCP"]
    p = {"memory": []}
    assert agent.merge_new_memories(p, [42, None, "Real fact", "  "]) == ["Real fact"]


def test_extract_memories_reads_the_json_list():
    s = state(messages=[{"role": "user", "content": "I only deploy to GCP"},
                        {"role": "assistant", "content": "Got it."}])
    s.provider.complete_reply = 'Sure: ["Deploy target is GCP"]'
    assert agent.extract_memories(s) == ["Deploy target is GCP"]
    sent = s.provider.completions[0]
    assert "You maintain long-term memory" in sent[0]["content"]
    assert sent[1]["content"] == "user: I only deploy to GCP\nassistant: Got it."
    assert agent.extract_memories(state()) == []  # one message is not a conversation yet


def test_a_transcript_saved_in_the_project_is_not_sent_with_every_chat():
    """A new chat about something else must not carry an old meeting (regression)."""
    p = project(documents=[{"name": "Transcript - standup.md", "text": "SECRET MEETING LINE", "kind": "transcript"},
                           {"name": "handbook.pdf", "text": "HANDBOOK TEXT"}])
    s = state(project=p, messages=[{"role": "user", "content": "what is eye dryness"}])
    run(s, "what is eye dryness")
    system = s.provider.calls[0][0]["content"]
    assert "SECRET MEETING LINE" not in system and "HANDBOOK TEXT" in system
    assert not any(k["name"] == "Meeting minutes" for k in agent.build_turn_context(s)["skills"])
    assert agent.sensitive_reasons("what is eye dryness", p, []) == []
    attached = state(project=p, attachments=[{"name": "Transcript: standup", "text": "ATTACHED LINE", "kind": "transcript"}])
    run(attached)
    assert "ATTACHED LINE" in attached.provider.calls[0][0]["content"]
