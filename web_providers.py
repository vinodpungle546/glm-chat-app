#!/usr/bin/env python3
"""Pluggable web search providers: Tavily (default), Brave Search, Exa, DuckDuckGo (no key).

Each adapter returns [{title, url, snippet, date}] or raises WebSearchError with a `kind`:
  missing_key | invalid_key | rate_limited | offline | error
Callers fall back to internal-only search and show the message.

Base URLs can be overridden for tests (TAVILY_URL, BRAVE_URL, EXA_URL).
"""

from __future__ import annotations

import os

import requests

import secrets_store

TIMEOUT = 15
PROVIDERS = {
    "tavily": {"label": "Tavily", "needs_key": True, "signup": "https://app.tavily.com",
               "note": "Built for AI agents: clean snippets with publish dates. Free tier: 1,000 searches/month."},
    "brave": {"label": "Brave Search", "needs_key": True, "signup": "https://api-dashboard.search.brave.com",
              "note": "Independent web index. Free tier available."},
    "exa": {"label": "Exa", "needs_key": True, "signup": "https://dashboard.exa.ai",
            "note": "Neural (meaning-based) search; good for research and companies."},
    "duckduckgo": {"label": "DuckDuckGo", "needs_key": False, "signup": "",
                   "note": "No key needed, but results are thinner and requests are often rate-limited."},
}
DEFAULT_PROVIDER = "tavily"


class WebSearchError(Exception):
    """A web search failed. `kind` says why, so the app can react: 'missing_key', 'invalid_key', 'rate_limited',
    'offline' or 'error'.
    """
    def __init__(self, kind: str, message: str):
        """Stores the reason (`kind`) next to the readable message."""
        super().__init__(message)
        self.kind = kind


def key_name(provider: str) -> str:
    """The secrets_store name of a provider's API key, e.g. 'tavily_api_key'."""
    return f"{provider}_api_key"


def has_key(provider: str) -> bool:
    """True when an API key for this provider is stored."""
    return bool(secrets_store.get(key_name(provider)))


def _http_error(provider: str, response: requests.Response) -> WebSearchError:
    """Turns an HTTP error reply into a WebSearchError with a helpful message (401/403 = bad key, 429 = rate
    limit).
    """
    label = PROVIDERS[provider]["label"]
    if response.status_code in (401, 403):
        return WebSearchError("invalid_key", f"{label} rejected the API key. Check it in Settings → Search.")
    if response.status_code == 429 or response.status_code == 432:
        return WebSearchError("rate_limited", f"{label} rate limit reached. Try again later, or switch provider.")
    return WebSearchError("error", f"{label} returned an error ({response.status_code}).")


def _post_or_get(provider: str, method: str, url: str, **kwargs) -> dict:
    """Sends one request to a search provider and returns its JSON, translating network and HTTP problems into
    WebSearchError.
    """
    try:
        response = requests.request(method, url, timeout=TIMEOUT, **kwargs)
    except (requests.ConnectionError, requests.Timeout) as exc:
        raise WebSearchError("offline", f"Can't reach {PROVIDERS[provider]['label']} — you may be offline.") from exc
    if not response.ok:
        raise _http_error(provider, response)
    try:
        return response.json()
    except ValueError as exc:
        raise WebSearchError("error", f"{PROVIDERS[provider]['label']} sent an unreadable reply.") from exc


def _tavily(query: str, limit: int, key: str) -> list[dict]:
    """Search with Tavily; results as [{title, url, snippet, date}]."""
    data = _post_or_get("tavily", "POST", os.environ.get("TAVILY_URL", "https://api.tavily.com/search"),
                        json={"query": query, "max_results": limit, "search_depth": "basic"},
                        headers={"Authorization": f"Bearer {key}"})
    return [{"title": r.get("title", ""), "url": r.get("url", ""), "snippet": r.get("content", ""),
             "date": r.get("published_date") or ""} for r in data.get("results", [])]


def _brave(query: str, limit: int, key: str) -> list[dict]:
    """Search with Brave Search; results as [{title, url, snippet, date}]."""
    data = _post_or_get("brave", "GET", os.environ.get("BRAVE_URL", "https://api.search.brave.com/res/v1/web/search"),
                        params={"q": query, "count": limit},
                        headers={"X-Subscription-Token": key, "Accept": "application/json"})
    return [{"title": r.get("title", ""), "url": r.get("url", ""), "snippet": r.get("description", ""),
             "date": r.get("page_age") or r.get("age") or ""} for r in (data.get("web") or {}).get("results", [])]


def _exa(query: str, limit: int, key: str) -> list[dict]:
    """Search with Exa; results as [{title, url, snippet, date}]."""
    data = _post_or_get("exa", "POST", os.environ.get("EXA_URL", "https://api.exa.ai/search"),
                        json={"query": query, "numResults": limit, "contents": {"text": {"maxCharacters": 400}}},
                        headers={"x-api-key": key})
    return [{"title": r.get("title") or r.get("url", ""), "url": r.get("url", ""),
             "snippet": (r.get("text") or r.get("summary") or "")[:400], "date": r.get("publishedDate") or ""}
            for r in data.get("results", [])]


def _duckduckgo(query: str, limit: int, _key: str) -> list[dict]:
    """Search with DuckDuckGo (no API key needed); results as [{title, url, snippet, date}]."""
    try:
        from ddgs import DDGS
        from ddgs.exceptions import RatelimitException
    except ImportError as exc:  # pragma: no cover
        raise WebSearchError("error", "DuckDuckGo search isn't installed.") from exc
    try:
        with DDGS() as ddgs:
            raw = ddgs.text(query, max_results=limit) or []
    except RatelimitException as exc:
        raise WebSearchError("rate_limited", "DuckDuckGo rate limit reached. Try again later.") from exc
    except Exception as exc:
        raise WebSearchError("offline", f"DuckDuckGo search failed: {exc}") from exc
    return [{"title": r.get("title", ""), "url": r.get("href", ""), "snippet": r.get("body", ""), "date": ""}
            for r in raw]


ADAPTERS = {"tavily": _tavily, "brave": _brave, "exa": _exa, "duckduckgo": _duckduckgo}


def search(provider: str, query: str, limit: int = 5) -> list[dict]:
    """Searches the web with the chosen provider and returns up to `limit` results that have a URL. Raises
    WebSearchError when the key is missing or the provider fails.
    """
    if provider not in ADAPTERS:
        raise WebSearchError("error", f"Unknown search provider “{provider}”.")
    key = secrets_store.get(key_name(provider)) if PROVIDERS[provider]["needs_key"] else ""
    if PROVIDERS[provider]["needs_key"] and not key:
        raise WebSearchError("missing_key", f"No {PROVIDERS[provider]['label']} API key. "
                                            "Add one in Settings → Search to include web results.")
    results = ADAPTERS[provider](query.strip(), max(1, min(int(limit), 20)), key)
    return [r for r in results if r["url"]][:limit]
