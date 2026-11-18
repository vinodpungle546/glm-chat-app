#!/usr/bin/env python3
"""Web search support for the GLM chat app.

Provides:
  - search_web(query): DuckDuckGo search returning normalized results.
  - format_results(): renders results as a compact context block for the LLM.
  - build_search_decision_messages(): prompt that asks the model whether the
    latest user question can be answered from its own knowledge or requires
    a web search.
  - parse_search_decision(): safely parses the model's decision JSON.
"""

from __future__ import annotations

import json
import re

from ddgs import DDGS

MAX_RESULTS = 5
DECISION_FORMAT = (
    'Respond with ONLY a JSON object in one of these two forms:\n'
    '{"needs_search": false, "reply": "<direct answer>"}\n'
    '{"needs_search": true, "query": "<search query to run>"}'
)


def search_web(query: str, max_results: int = MAX_RESULTS) -> list[dict]:
    """Search DuckDuckGo and return [{title, url, snippet}] entries."""
    if not query.strip():
        return []
    try:
        with DDGS() as ddgs:
            raw = ddgs.text(query, max_results=max_results)
            return [
                {
                    "title": item.get("title", ""),
                    "url": item.get("href", ""),
                    "snippet": item.get("body", ""),
                }
                for item in raw
            ]
    except Exception as exc:  # network/parse errors must not crash the chat
        return [{"title": "Search error", "url": "", "snippet": str(exc)}]


def format_results(results: list[dict]) -> str:
    """Render search results as a numbered context block for the model."""
    if not results:
        return "No web results were found for the query."
    lines = ["Web search results:"]
    for i, item in enumerate(results, start=1):
        lines.append(f"[{i}] {item['title']}\n    URL: {item['url']}\n    {item['snippet']}")
    return "\n".join(lines)


def build_search_decision_messages(recent_messages: list[dict]) -> list[dict]:
    """Build the pass-1 prompt: decide whether a web search is required."""
    transcript = "\n".join(
        f"{m['role']}: {m['content']}" for m in recent_messages[-6:]
    )
    return [
        {
            "role": "system",
            "content": (
                "You decide whether a user's question can be answered from your "
                "own knowledge or needs up-to-date web information. Search is "
                "needed for current events, recent releases, prices, statistics, "
                "or facts you are unsure about. " + DECISION_FORMAT
            ),
        },
        {"role": "user", "content": transcript},
    ]


def parse_search_decision(text: str) -> dict:
    """Parse the model's decision JSON; default to no-search on any failure."""
    default = {"needs_search": False, "reply": text}
    if not text or not text.strip():
        return default

    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        return default
    try:
        data = json.loads(match.group(0))
    except json.JSONDecodeError:
        return default
    if not isinstance(data, dict):
        return default

    if data.get("needs_search") is True and isinstance(data.get("query"), str):
        return {"needs_search": True, "query": data["query"].strip()}
    return default
