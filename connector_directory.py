#!/usr/bin/env python3
"""Finding connectors online (Settings → Connectors → Find connectors).

`ConnectorDirectory` is the interface; `McpRegistry` searches the official MCP Registry
(registry.modelcontextprotocol.io, API v0: GET /v0/servers?search=&limit=&cursor=). More
directories can be added to DIRECTORIES later.

Privacy: a search sends only the words the user typed (plus paging). No memory, names,
transcripts, project names or the list of installed connectors ever goes to a directory.
Results are cached for CACHE_SECONDS.
"""

from __future__ import annotations

import os
import re
import threading
import time
from typing import Protocol

import requests

CACHE_SECONDS = 600
USER_AGENT = "Quorum-connector-search/1.0"


class DirectoryError(Exception):
    """kind: offline | rate_limited | error"""

    def __init__(self, kind: str, message: str):
        """Stores the reason (`kind`: offline, rate_limited, error) next to the message."""
        super().__init__(message)
        self.kind = kind


class ConnectorDirectory(Protocol):
    """What a connector directory must offer (a Protocol, i.e. an interface): an id, a label and search()."""
    id: str
    label: str

    def search(self, query: str, cursor: str = "", limit: int = 20) -> dict:
        """Finds connectors matching `query`; returns {'servers': [...], 'next': cursor for more}."""
        ...


def publisher_for(name: str) -> str:
    """"io.github.acme/weather" → "github.com/acme"; "com.example/tool" → "example.com"."""
    namespace = (name or "").split("/", 1)[0]
    parts = namespace.split(".")
    if len(parts) >= 3 and parts[0] == "io" and parts[1] == "github":
        return "github.com/" + ".".join(parts[2:])
    if len(parts) >= 2:
        return ".".join(reversed(parts))
    return namespace


def _env_needs(env: list[dict]) -> list[dict]:
    """The environment variables (settings or keys) a package needs, as shown in the connector's setup form."""
    return [{"name": v.get("name", ""), "description": (v.get("description") or "")[:200],
             "required": bool(v.get("isRequired")), "secret": bool(v.get("isSecret")),
             "default": v.get("default") or ""} for v in env or [] if isinstance(v, dict) and v.get("name")]


def _positional(args: list[dict]) -> list[str]:
    """The fixed command-line arguments a package needs; arguments that need the user's own value are skipped
    (not supported yet).
    """
    out = []
    for arg in args or []:
        if not isinstance(arg, dict):
            continue
        value = arg.get("value") or arg.get("default") or ""
        if "{" in str(value):
            continue  # needs the user's input; not supported yet
        if arg.get("type") == "named" and arg.get("name"):
            out += [arg["name"]] + ([str(value)] if value else [])
        elif value:
            out.append(str(value))
    return out


def normalize(item: dict, directory: str = "mcp-registry") -> dict:
    """One registry entry → what the UI shows and what adding it needs."""
    server = item.get("server") or item
    meta = (item.get("_meta") or {}).get("io.modelcontextprotocol.registry/official") or {}
    remotes = []
    for remote in server.get("remotes") or []:
        if not isinstance(remote, dict) or remote.get("type") not in ("streamable-http", "sse"):
            continue
        headers = [{"name": h.get("name", ""), "description": (h.get("description") or "")[:200],
                    "required": bool(h.get("isRequired")), "secret": bool(h.get("isSecret")),
                    "template": h.get("value") or ""} for h in remote.get("headers") or [] if h.get("name")]
        remotes.append({"type": "http" if remote["type"] == "streamable-http" else "sse",
                        "url": remote.get("url", ""), "headers": headers})
    packages = []
    for pkg in server.get("packages") or []:
        if not isinstance(pkg, dict):
            continue
        rtype = pkg.get("registryType") or pkg.get("registry_type") or ""
        transport = (pkg.get("transport") or {}).get("type", "stdio")
        supported = rtype in ("npm", "pypi") and transport == "stdio"
        packages.append({"registry_type": rtype, "identifier": pkg.get("identifier") or pkg.get("name") or "",
                         "version": pkg.get("version") or server.get("version") or "",
                         "runtime": pkg.get("runtimeHint") or ("npx" if rtype == "npm" else "uvx" if rtype == "pypi" else ""),
                         "transport": transport, "supported": supported,
                         "args": _positional(pkg.get("packageArguments")),
                         "env": _env_needs(pkg.get("environmentVariables"))})
    needs = []
    if any(h["secret"] or "key" in h["name"].lower() or h["name"] == "Authorization" for r in remotes for h in r["headers"]):
        needs.append("API key")
    if any(e["required"] for p in packages for e in p["env"]):
        needs.append("environment variables")
    if remotes and not any(r["headers"] for r in remotes):
        needs.append("may ask you to sign in")
    repository = (server.get("repository") or {}).get("url", "")
    return {"id": f"{server.get('name', '')}@{server.get('version', '')}", "directory": directory,
            "name": server.get("name", ""), "title": server.get("title") or _title(server.get("name", "")),
            "description": (server.get("description") or "")[:400], "version": server.get("version", ""),
            "publisher": publisher_for(server.get("name", "")), "repository": repository,
            "website": server.get("websiteUrl") or "", "official": meta.get("status") == "active",
            "namespace_verified": True, "updated_at": meta.get("updatedAt") or meta.get("publishedAt") or "",
            "remotes": remotes, "packages": packages, "needs": needs,
            "transports": sorted({r["type"] for r in remotes} | ({"package"} if packages else set()))}


def _title(name: str) -> str:
    """A readable title from a registry name: 'io.github.acme/weather-tools' -> 'Weather Tools'."""
    short = name.split("/", 1)[-1]
    return re.sub(r"[-_]+", " ", short).strip().title() or name


class McpRegistry:
    """The official MCP Registry (registry.modelcontextprotocol.io).

    Only the search words are sent; results are cached for 10 minutes.
    """
    id = "mcp-registry"
    label = "Official MCP Registry"

    def __init__(self, base_url: str | None = None):
        """Uses MCP_REGISTRY_URL when set (tests point it at a fake registry)."""
        self.base_url = (base_url or os.environ.get("MCP_REGISTRY_URL") or "https://registry.modelcontextprotocol.io").rstrip("/")
        self._cache: dict[tuple, tuple[float, dict]] = {}
        self._lock = threading.Lock()

    def _get(self, path: str, params: dict) -> dict:
        """One GET request to the registry; network problems and errors become DirectoryError with a helpful
        message.
        """
        try:
            response = requests.get(f"{self.base_url}{path}", params=params, timeout=20,
                                    headers={"Accept": "application/json", "User-Agent": USER_AGENT})
        except (requests.ConnectionError, requests.Timeout) as exc:
            raise DirectoryError("offline", "Can't reach the MCP Registry. Check the internet connection.") from exc
        if response.status_code == 429:
            raise DirectoryError("rate_limited", "The MCP Registry is limiting requests. Try again in a minute.")
        if response.status_code >= 400:
            raise DirectoryError("error", f"The MCP Registry answered {response.status_code}.")
        try:
            return response.json()
        except ValueError as exc:
            raise DirectoryError("error", "The MCP Registry sent an unexpected reply.") from exc

    def search(self, query: str, cursor: str = "", limit: int = 20) -> dict:
        """Searches the registry (query trimmed to 100 characters), keeps the latest version of each server, and
        caches the result.
        """
        query = " ".join(str(query or "").split())[:100]
        key = (query.lower(), cursor, limit)
        with self._lock:
            hit = self._cache.get(key)
            if hit and time.time() - hit[0] < CACHE_SECONDS:
                return hit[1]
        params = {"search": query, "limit": max(1, min(int(limit), 50))}
        if cursor:
            params["cursor"] = cursor
        data = self._get("/v0/servers", params)
        latest: dict[str, dict] = {}
        for item in data.get("servers") or []:
            entry = normalize(item, self.id)
            meta = (item.get("_meta") or {}).get("io.modelcontextprotocol.registry/official") or {}
            if meta.get("isLatest") is False and entry["name"] in latest:
                continue
            latest[entry["name"]] = entry
        result = {"results": list(latest.values()), "next_cursor": (data.get("metadata") or {}).get("nextCursor") or "",
                  "directory": self.label}
        with self._lock:
            self._cache[key] = (time.time(), result)
        return result

    def latest_version(self, name: str) -> dict | None:
        """The newest published version of one server (for "update available")."""
        for entry in self.search(name.split("/", 1)[-1], limit=30)["results"]:
            if entry["name"] == name:
                return entry
        return None


DIRECTORIES: dict[str, ConnectorDirectory] = {}


def directory(directory_id: str = "mcp-registry") -> ConnectorDirectory:
    """The connector directory with this id (created on first use)."""
    if directory_id not in DIRECTORIES:
        if directory_id != "mcp-registry":
            raise KeyError(directory_id)
        DIRECTORIES[directory_id] = McpRegistry()
    return DIRECTORIES[directory_id]


def mark_installed(results: list[dict], installed: list[dict]) -> list[dict]:
    """Flag results that are already added (same registry name, or the same URL)."""
    names = {(c.get("source") or {}).get("server") for c in installed}
    urls = {c.get("url") for c in installed if c.get("url")}
    for result in results:
        result["installed"] = result["name"] in names or any(r["url"] in urls for r in result["remotes"])
    return results


def version_newer(new: str, old: str) -> bool:
    """True when version `new` is higher than `old` ('1.10.0' > '1.9.2'), comparing the numbers, not the text."""
    def parts(v: str) -> tuple:
        """The numbers of a version string as a tuple, e.g. '1.10.0' -> (1, 10, 0)."""
        return tuple(int(x) for x in re.findall(r"\d+", v or "")[:4]) or (0,)
    return parts(new) > parts(old)
