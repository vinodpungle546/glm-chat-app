# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A Streamlit "project-based AI agent" chat app that talks to GLM-5.3-Flash through the Globant GAIA API (OpenAI-compatible `/v1/chat/completions`, SSE streaming). `chat.py` is a standalone terminal chat client kept from the first version; it uses different env vars (`CODA_GEAI_BASE_URL`, `CODA_GEAI_API_KEY`, `GLM_MODEL`) than the web app.

## Commands

```bash
# Run locally (set env vars first; see .env.example)
pip install -r requirements.txt -r requirements-dev.txt
PROJECTS_DIR=./data/projects SESSIONS_DIR=./data/sessions CONNECTORS_FILE=./data/connectors/connectors.json \
  streamlit run app.py

# Docker (reads .env; serves on http://localhost:8503)
docker compose up --build

# Tests (pytest.ini sets testpaths=tests and -q)
pytest
pytest tests/test_tools.py
pytest tests/test_agent_loop.py::test_connector_call_executes
```

No linter/formatter is configured.

Env vars: `GAIA_BASE_URL`, `GAIA_API_KEY`, `GAIA_MODEL`, `GAIA_ENDPOINT` (model access); `PROJECTS_DIR`, `SESSIONS_DIR`, `CONNECTORS_FILE` (storage). Storage paths default to `/app/...` container paths, so set them when running outside Docker.

When adding a new top-level module, also add it to the explicit `COPY` line in the `Dockerfile`, because the image doesn't copy the whole directory.

## Architecture

- **`app.py`**: Streamlit UI plus the agent loop. All per-user state lives in `st.session_state` (`project`, `messages`, `chat_attachments`, `web_search_enabled`, …).
- **`projects.py`**: each project is a JSON file in `PROJECTS_DIR` holding `instructions`, `skills`, `tools`, `memory`, and `documents` (see `PROJECT_FIELDS`). A project's chat sessions are stored in their own directory, chosen by `sessions_dir_for`. A `default` ("General") project is created automatically.
- **`tools.py`**: holds the tool registry (`TOOL_REGISTRY`, `register_tool`; the built-ins are `web_search` and `datetime`), skill helpers, slash-command parsing, and `build_agent_system_prompt`, which puts together the instructions, documents, memory, skill index, and tool/connector protocol.
- **`connectors.py`**: global connectors shared by **all** projects and stored in `CONNECTORS_FILE`. There are two kinds: `custom` (Python tools) and `mcp` (remote MCP servers called over Streamable HTTP with JSON-RPC `tools/list` and `tools/call`).
- **`doc_extract.py`**: converts uploaded files (pdf, docx, xlsx, csv, txt, …) to text. Each document is capped at `MAX_CHARS`, and `build_document_context` puts them into the system prompt. `python-docx` is optional.
- **`web_search.py`**: DuckDuckGo search (`ddgs`) plus a prompt and parser that ask the model whether a turn needs a search at all.

### Agent turn (`generate_reply` in app.py)

The model has no native tool-calling. Every tool use goes through **JSON that the model writes in plain text**, which the app parses with regex:

1. If `web_search` is enabled, a non-streaming "search decision" call runs first. If the model says a search is needed, the app runs the search and answers using the results.
2. Otherwise the app streams a reply and then checks it for, in this order:
   - a skill request: `tools.parse_skill_request`. Skills use progressive disclosure: the system prompt lists only skill names and descriptions, and the full content loads on request.
   - `{"tool": ..., "input": ...}` through `tools.parse_tool_call`. Only tools enabled on the project count.
   - `{"connector": ..., "input": ...}` through `parse_connector_call`.
3. Only **one** round is done. The result is added as a synthetic user message (`[Tool output — …]`), and `stream_with_tool_result` streams the final answer.

Other pieces of a turn:
- A slash command (`/1`, `/<fragment>`) applies a skill to that turn only (`turn_skill`).
- Long-term project memory is pulled out of conversations by a separate model call (`update_memory_from_conversation` / `maybe_update_memory`).

## Testing notes

- `app.py` imports fine without a running Streamlit server. Tests replace `st.session_state` using `monkeypatch.setattr(app_module.st, "session_state", FakeState(...), raising=False)`, where `FakeState` is a dict that also allows attribute access. They also patch `stream_assistant_reply` or `stream_reply` so no network calls are made.
- Storage functions take explicit directories, or tests monkeypatch the module path constants (`connectors.CONNECTORS_FILE`, `projects.PROJECTS_DIR`) to point at `tmp_path`.
