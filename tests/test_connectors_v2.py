"""Connectors found online: directory search (only the query is sent), adding remote servers
(API key, OAuth, SSE) and local packages (pinned, confirmed), secrets kept out of the config,
privacy routing, and older connectors still working. Everything runs against tests/fake_mcp.py."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest
import requests

import agent
import connector_directory as cd
import connectors
import secrets_store
from tests import fake_mcp
from tests.fake_mcp import FakeMcp
from tests.test_api import client, events  # noqa: F401  (fixture)

STDIO_SCRIPT = str(Path(__file__).with_name("fake_mcp_stdio.py"))


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(connectors, "CONNECTORS_FILE", tmp_path / "connectors" / "connectors.json")
    yield tmp_path
    connectors.STDIO.stop_all()


@pytest.fixture
def mcp(store, monkeypatch):
    with FakeMcp() as base:
        monkeypatch.setenv("MCP_REGISTRY_URL", base)
        cd.DIRECTORIES.clear()
        yield base


# ---------------------------------------------------------------------------
# Directory
# ---------------------------------------------------------------------------

def test_search_sends_only_the_query(mcp):
    result = cd.directory().search("echo")
    assert [r["name"] for r in result["results"]] == ["io.github.example/echo-remote", "com.example/echo-oauth",
                                                      "io.github.example/echo-local"]
    assert fake_mcp.SEARCHES == [{"search": "echo", "limit": "20"}]
    cd.directory().search("echo")
    assert len(fake_mcp.SEARCHES) == 1  # cached


def test_results_describe_what_adding_needs(mcp):
    found = {r["name"]: r for r in cd.directory().search("example")["results"]}
    remote = found["io.github.example/echo-remote"]
    assert remote["publisher"] == "github.com/example" and remote["official"] and remote["version"] == "1.2.0"
    assert remote["needs"] == ["API key"] and remote["transports"] == ["http"]
    assert remote["repository"] == "https://github.com/example/echo-remote"
    local = found["io.github.example/echo-local"]["packages"][0]
    assert local["supported"] and local["registry_type"] == "npm" and local["version"] == "2.0.1"
    assert [e["name"] for e in local["env"] if e["secret"]] == ["ECHO_TOKEN"]
    assert found["com.example/echo-oauth"]["needs"] == ["may ask you to sign in"]
    assert not found["io.github.example/docker-only"]["packages"][0]["supported"]


def test_installed_results_are_marked(mcp):
    connectors.add_remote("Echo", f"{mcp}/mcp", "http", source={"type": "directory", "server": "com.example/echo-oauth"})
    marked = cd.mark_installed(cd.directory().search("echo")["results"], connectors.load_connectors())
    assert [r["installed"] for r in marked] == [False, True, False]


def test_directory_errors(mcp):
    with pytest.raises(cd.DirectoryError) as err:
        cd.directory().search("offline-please")
    assert err.value.kind == "error"
    with pytest.raises(cd.DirectoryError) as err:
        cd.directory().search("slow-down")
    assert err.value.kind == "rate_limited"
    with pytest.raises(cd.DirectoryError) as err:
        cd.McpRegistry("http://127.0.0.1:9").search("x")
    assert err.value.kind == "offline"


# ---------------------------------------------------------------------------
# Remote connectors
# ---------------------------------------------------------------------------

def test_remote_with_api_key(mcp):
    added = connectors.add_remote("Echo Remote", f"{mcp}/mcp-key", "http", auth_type="header",
                                  header_value="key-123")
    assert "key-123" not in connectors.CONNECTORS_FILE.read_text()
    assert secrets_store.get(connectors.secret_name(added["id"], "Authorization")) == "key-123"
    result = connectors.test_connector(added["id"])
    assert result["ok"] and [t["name"] for t in result["tools"]] == ["echo", "add"]
    connector = connectors.get_connector(added["id"])
    assert connectors.execute_connector(connector, '{"a": 2, "b": 3}', "add") == "5.0"
    connectors.remove_connector(added["id"])
    assert secrets_store.get(connectors.secret_name(added["id"], "Authorization")) == ""


def test_remote_without_the_key_needs_sign_in(mcp):
    added = connectors.add_remote("Echo Remote", f"{mcp}/mcp-key", "http")
    assert connectors.test_connector(added["id"])["needs_sign_in"]


def test_oauth_sign_in_and_refresh(mcp):
    added = connectors.add_remote("Echo OAuth", f"{mcp}/mcp-oauth", "http", auth_type="oauth")
    assert connectors.probe_tools(f"{mcp}/mcp-oauth")["needs_sign_in"]
    url = connectors.start_oauth(added["id"], "http://127.0.0.1:8000/api/connectors/oauth/callback")
    assert "code_challenge_method=S256" in url and "resource=" in url
    redirect = requests.get(url, allow_redirects=False).headers["Location"]
    params = dict(p.split("=") for p in redirect.split("?", 1)[1].split("&"))
    connectors.finish_oauth(params["state"], params["code"])
    assert connectors.test_connector(added["id"])["ok"]
    stored = json.loads(secrets_store.get(connectors.secret_name(added["id"], "oauth")))
    assert stored["access_token"].startswith("tok-") and "tok-" not in connectors.CONNECTORS_FILE.read_text()
    stored["expires_at"] = 0  # expired: the refresh token gets a new one
    secrets_store.set(connectors.secret_name(added["id"], "oauth"), json.dumps(stored))
    assert connectors.call_tool(connectors.get_connector(added["id"]), "echo", {"text": "hi"}) == "echo: hi"
    with pytest.raises(RuntimeError):
        connectors.finish_oauth(params["state"], params["code"])  # a sign-in link works once


def test_sse_transport(mcp):
    added = connectors.add_remote("Echo SSE", f"{mcp}/sse", "sse")
    assert connectors.test_connector(added["id"])["ok"]
    assert connectors.execute_connector(connectors.get_connector(added["id"]), "hello", "echo") == "echo: hello"


def test_remote_urls_must_be_https_unless_local():
    with pytest.raises(ValueError):
        connectors.add_remote("x", "http://example.com/mcp")
    assert connectors.is_remote({"url": "https://example.com/mcp", "transport": "http"})
    assert not connectors.is_remote({"url": "http://localhost:9000/mcp", "transport": "http"})
    assert not connectors.is_remote({"url": "", "transport": "stdio"})


# ---------------------------------------------------------------------------
# Local packages
# ---------------------------------------------------------------------------

def test_package_commands_are_pinned():
    assert connectors.stdio_command("npm", "@example/echo-mcp", "2.0.1") == ["npx", "-y", "@example/echo-mcp@2.0.1"]
    assert connectors.stdio_command("pypi", "echo-mcp", "v1.4") == ["uvx", "echo-mcp==1.4"]
    for version in ("latest", "", "^2.0.0", "*"):
        with pytest.raises(ValueError):
            connectors.stdio_command("npm", "@example/echo-mcp", version)
    with pytest.raises(ValueError):
        connectors.stdio_command("oci", "example/docker", "1.0.0")
    with pytest.raises(ValueError):
        connectors.stdio_command("npm", "evil; rm -rf /", "1.0.0")


def test_package_needs_an_explicit_confirm(store):
    with pytest.raises(PermissionError):
        connectors.add_stdio("Echo Local", "npm", "@example/echo-mcp", "2.0.1")
    assert connectors.load_connectors() == []


@pytest.fixture
def fake_package(monkeypatch):
    """The pinned npx command is validated, then the fake stdio server runs instead (nothing installed)."""
    real = connectors.stdio_command

    def command(registry_type, identifier, version, args=None):
        real(registry_type, identifier, version, args)
        return [sys.executable, STDIO_SCRIPT]

    monkeypatch.setattr(connectors, "stdio_command", command)


def test_package_runs_over_stdio_with_secret_env(store, fake_package):
    added = connectors.add_stdio("Echo Local", "npm", "@example/echo-mcp", "2.0.1", env={"ECHO_MODE": "loud"},
                                 secret_env={"ECHO_TOKEN": "s3cret"}, confirmed=True,
                                 source={"type": "directory", "server": "io.github.example/echo-local", "version": "2.0.1"})
    text = connectors.CONNECTORS_FILE.read_text()
    assert "s3cret" not in text and '"ECHO_MODE": "loud"' in text
    assert connectors.test_connector(added["id"])["ok"]
    connector = connectors.get_connector(added["id"])
    assert connectors.execute_connector(connector, '{"text": "hi"}', "echo") == "echo: hi [token set]"
    assert connectors.execute_connector(connector, "{}", "whoami") == "loud"
    assert connectors.STDIO.running(added["id"])
    connectors.update_connector(added["id"], {"enabled": False})
    assert not connectors.STDIO.running(added["id"])
    assert connectors.active_connectors() == []
    connectors.remove_connector(added["id"])
    assert secrets_store.get(connectors.secret_name(added["id"], "ECHO_TOKEN")) == ""


def test_update_to_a_new_version_needs_confirm(store, fake_package):
    added = connectors.add_stdio("Echo Local", "npm", "@example/echo-mcp", "2.0.1", confirmed=True,
                                 source={"type": "directory", "server": "x", "version": "2.0.1"})
    with pytest.raises(PermissionError):
        connectors.update_version(added["id"], "2.1.0")
    updated = connectors.update_version(added["id"], "2.1.0", confirmed=True)
    assert updated["stdio"]["version"] == "2.1.0" and updated["source"]["version"] == "2.1.0"
    assert cd.version_newer("2.10.0", "2.9.1") and not cd.version_newer("1.0.0", "1.0.0")


# ---------------------------------------------------------------------------
# Older connectors, the agent and routing
# ---------------------------------------------------------------------------

def test_older_connectors_still_load_and_run(store):
    connectors.save_connectors([{"id": "old", "kind": "mcp", "name": "gh", "description": "", "url": "http://x/mcp",
                                 "created_at": "2026-01-01"}])
    assert connectors.active_connectors()[0]["name"] == "gh"
    with patch.object(connectors, "call_mcp_tool", return_value="42") as call:
        assert connectors.execute_connector(connectors.get_connector("old"), '{"q": "stars"}') == "42"
    call.assert_called_once_with("http://x/mcp", "gh", {"q": "stars"})


def test_agent_lists_tools_and_skips_remote_connectors_in_confidential_chats(mcp):
    # a remote server (https; listed with the tools it reported) and a local package
    connectors.save_connectors([
        {"id": "rem", "kind": "mcp", "name": "Echo Remote", "description": "Echo service", "transport": "http",
         "url": "https://mcp.example.com/mcp", "tools": fake_mcp.TOOLS},
        {"id": "loc", "kind": "mcp", "name": "Local echo", "url": "", "transport": "stdio", "tools": [{"name": "echo", "description": ""}],
         "stdio": {"command": ["true"]}}])
    block = agent.connectors_block(connectors.active_connectors())
    assert "Echo Remote: Echo service. Tools: echo (Repeat the text back); add (Add two numbers)" in block
    assert '"tool": "<tool name>"' in block
    call = agent.parse_connector_call('{"connector": "Echo Remote", "tool": "add", "input": {"a": 1, "b": 2}}',
                                      connectors.active_connectors())
    assert call[0]["_tool"] == "add" and json.loads(call[1]) == {"a": 1, "b": 2}

    import model_registry
    project = {"id": "p", "name": "P", "memory": [], "skills": [], "tools": [], "documents": []}
    cloud_state = agent.TurnState(project=project, messages=[], provider=model_registry.GaiaProvider())
    names = [c["name"] for c in agent.build_turn_context(cloud_state)["connectors"]]
    assert names == ["Echo Remote", "Local echo"]
    confidential = agent.TurnState(project=project, messages=[], provider=model_registry.GaiaProvider(),
                                   remote_connectors=False)
    assert [c["name"] for c in agent.build_turn_context(confidential)["connectors"]] == ["Local echo"]
    local = agent.TurnState(project=project, messages=[], provider=model_registry.OllamaProvider("gemma4:e4b"))
    assert [c["name"] for c in agent.build_turn_context(local)["connectors"]] == ["Local echo"]


def test_api_search_and_add_flow(client, mcp, monkeypatch):  # noqa: F811
    from api import main
    monkeypatch.setattr(main.connectors, "CONNECTORS_FILE", connectors.CONNECTORS_FILE)
    found = client.get("/api/connector-directory/search", params={"q": "echo"}).json()
    assert len(found["results"]) == 3 and fake_mcp.SEARCHES[-1] == {"search": "echo", "limit": "20"}
    remote = found["results"][0]
    added = client.post("/api/connectors", json={
        "kind": "mcp", "name": remote["title"], "url": remote["remotes"][0]["url"], "transport": "http",
        "auth_type": "header", "header_value": "key-123",
        "source": {"type": "directory", "server": remote["name"], "version": remote["version"]}}).json()
    assert [t["name"] for t in added["tools"]] == ["echo", "add"] and added["secrets_set"]["Authorization"]
    assert "key-123" not in json.dumps(client.get("/api/connectors").json())
    again = client.get("/api/connector-directory/search", params={"q": "echo"}).json()
    assert again["results"][0]["installed"]
    assert client.post("/api/connectors/stdio", json={"name": "X", "registry_type": "npm", "identifier": "@example/echo-mcp",
                                                      "version": "2.0.1"}).status_code == 428
    preview = client.post("/api/connectors/preview-command", json={"name": "X", "registry_type": "npm",
                                                                   "identifier": "@example/echo-mcp", "version": "2.0.1"}).json()
    assert preview["command"] == ["npx", "-y", "@example/echo-mcp@2.0.1"]
    assert client.get("/api/connector-directory/search", params={"q": "slow-down"}).status_code == 429
    assert client.delete(f"/api/connectors/{added['id']}").json() == {"ok": True}
