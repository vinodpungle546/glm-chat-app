"""Cloud providers (Anthropic, OpenAI, OpenAI-compatible): config, keys in the secret store (never
in config files), request format, streaming, error kinds, and registry integration. Every request
goes to tests/fake_cloud.py, never to the real APIs."""

from __future__ import annotations

import json

import pytest

import cloud_providers as cp
import model_registry
import secrets_store
from tests import fake_cloud
from tests.fake_cloud import FakeCloud

MESSAGES = [{"role": "system", "content": "Be brief."}, {"role": "user", "content": "Hello there"},
            {"role": "assistant", "content": "Hi!", "model": "x · cloud"}, {"role": "user", "content": "What is Atlas?"}]


@pytest.fixture
def cloud(monkeypatch):
    with FakeCloud() as url:
        monkeypatch.setenv("ANTHROPIC_PROVIDER_URL", url)
        monkeypatch.setenv("OPENAI_PROVIDER_URL", f"{url}/openai/v1")
        yield url


def test_keys_go_to_the_secret_store_not_the_config(cloud, tmp_path):
    added = cp.add_provider("anthropic", key="sk-ant-test")
    assert added["has_key"] and added["key_hint"] == "••••test"
    config_text = cp.CONFIG_FILE.read_text()
    assert "sk-ant-test" not in config_text
    assert secrets_store.get(cp.key_name("anthropic")) == "sk-ant-test"
    assert "sk-ant-test" not in json.dumps(cp.list_providers())
    cp.remove_provider("anthropic")
    assert secrets_store.get(cp.key_name("anthropic")) == ""


def test_test_connection_lists_models_and_enables_defaults(cloud):
    cp.add_provider("anthropic", key="sk-ant-test")
    result = cp.test_provider("anthropic")
    assert result["ok"] and result["count"] == 3
    assert result["provider"]["enabled"] == ["claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-5-5"]
    ids = [e["id"] for e in model_registry.load_registry()["models"]]
    assert "cloud:anthropic:claude-opus-5-5" in ids
    entry = model_registry.find("cloud:anthropic:claude-opus-5-5")
    assert entry["label"] == "Claude Opus 5.5" and entry["group"] == "cloud" and entry["status"] == "ready"


def test_openai_model_list_skips_non_chat_models(cloud):
    cp.add_provider("openai", key="sk-openai-test")
    cp.test_provider("openai")
    assert [m["id"] for m in cp.get_config("openai")["models"]] == ["gpt-5.5", "gpt-5.5-mini"]


def test_invalid_and_missing_keys(cloud):
    cp.add_provider("anthropic", key="wrong")
    bad = cp.test_provider("anthropic")
    assert not bad["ok"] and bad["kind"] == "invalid_key" and cp.get_config("anthropic")["last_error"] == "invalid_key"
    cp.add_provider("openai")
    assert cp.test_provider("openai")["kind"] == "missing_key"


def test_anthropic_stream_request_format(cloud):
    cp.add_provider("anthropic", key="sk-ant-test")
    provider = model_registry.get_provider("cloud:anthropic:claude-opus-5-5") if cp.update_provider(
        "anthropic", {"enabled": ["claude-opus-5-5"]}) else None
    text = "".join(provider.stream(MESSAGES))
    assert text == "Fake reply about: What is Atlas?"
    sent = fake_cloud.REQUESTS[-1]
    body = sent["body"]
    assert "sk-ant-test" in (sent["headers"].get("x-api-key", "") + sent["headers"].get("Authorization", ""))
    assert body["model"] == "claude-opus-5-5" and body["stream"] is True and body["max_tokens"] == cp.STREAM_MAX_TOKENS
    assert body["system"] == "Be brief."
    assert body["messages"] == [{"role": "user", "content": "Hello there"}, {"role": "assistant", "content": "Hi!"},
                                {"role": "user", "content": "What is Atlas?"}]
    assert "temperature" not in body and "model" not in json.dumps(body["messages"])  # no UI metadata
    assert body["fallbacks"] == "default" and cp.FALLBACK_BETA in sent["headers"].get("anthropic-beta", "")
    assert provider.model_id == "cloud:anthropic:claude-opus-5-5" and not provider.is_local


def test_anthropic_complete_ignores_temperature_and_caps_max_tokens(cloud):
    cp.add_provider("anthropic", key="sk-ant-test")
    provider = cp.CloudChatProvider("anthropic", "claude-haiku-5-5")
    assert provider.complete(MESSAGES, max_tokens=1800, temperature=0) == "Fake reply about: What is Atlas?"
    body = fake_cloud.REQUESTS[-1]["body"]
    assert body["max_tokens"] == 1800 and "temperature" not in body and "stream" not in body
    assert "fallbacks" not in body  # only for models that support server-side fallbacks


def test_anthropic_refusal_is_reported(cloud):
    cp.add_provider("anthropic", key="sk-ant-test")
    assert "".join(cp.CloudChatProvider("anthropic", "claude-refuse").stream(MESSAGES)) == cp.REFUSAL_NOTE


@pytest.mark.parametrize("model,kind", [("claude-missing", "unknown_model"), ("claude-limit", "rate_limited"),
                                        ("claude-busy", "overloaded")])
def test_anthropic_error_kinds(cloud, model, kind):
    cp.add_provider("anthropic", key="sk-ant-test")
    with pytest.raises(cp.CloudProviderError) as err:
        "".join(cp.CloudChatProvider("anthropic", model).stream(MESSAGES))
    assert err.value.kind == kind


def test_offline_provider(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_PROVIDER_URL", "http://127.0.0.1:9")
    monkeypatch.setenv("OPENAI_PROVIDER_URL", "http://127.0.0.1:9/v1")
    cp.add_provider("anthropic", key="sk-ant-test")
    cp.add_provider("openai", key="sk-openai-test")
    for provider in (cp.CloudChatProvider("anthropic", "claude-opus-5-5"), cp.CloudChatProvider("openai", "gpt-5.5")):
        with pytest.raises(cp.CloudProviderError) as err:
            provider.complete(MESSAGES)
        assert err.value.kind == "offline"


def test_openai_stream_and_errors(cloud):
    cp.add_provider("openai", key="sk-openai-test")
    provider = cp.CloudChatProvider("openai", "gpt-5.5")
    assert "".join(provider.stream(MESSAGES)) == "Fake reply about: What is Atlas?"
    sent = fake_cloud.REQUESTS[-1]
    assert sent["headers"]["Authorization"] == "Bearer sk-openai-test"
    assert sent["body"]["messages"][0] == {"role": "system", "content": "Be brief."}
    assert "temperature" not in sent["body"] and "max_tokens" not in sent["body"]
    for model, kind in (("gpt-missing", "unknown_model"), ("gpt-limit", "rate_limited"), ("gpt-busy", "overloaded")):
        with pytest.raises(cp.CloudProviderError) as err:
            cp.CloudChatProvider("openai", model).complete(MESSAGES)
        assert err.value.kind == kind


def test_openai_compatible_endpoint_with_api_key_header(cloud):
    added = cp.add_provider("openai_compatible", "Azure test", f"{cloud}/openai/v1".replace("127.0.0.1", "localhost"),
                            "api-key", "sk-openai-test")
    assert added["id"] == "compat-azure-test"
    cp.update_provider(added["id"], {"manual_model": "gpt-5.5"})
    assert cp.CloudChatProvider(added["id"], "gpt-5.5").complete(MESSAGES).startswith("Fake reply")
    assert fake_cloud.REQUESTS[-1]["headers"]["api-key"] == "sk-openai-test"
    assert "cloud:compat-azure-test:gpt-5.5" in [e["id"] for e in model_registry.load_registry()["models"]]


def test_compatible_endpoint_needs_https_except_localhost():
    with pytest.raises(ValueError):
        cp.add_provider("openai_compatible", "x", "http://example.com/v1")
    with pytest.raises(ValueError):
        cp.add_provider("openai_compatible", "x", "not a url")


def test_to_anthropic_merges_roles_and_ends_with_user():
    system, msgs = cp.to_anthropic([{"role": "assistant", "content": "earlier"}, {"role": "user", "content": "a"},
                                    {"role": "user", "content": "b"}, {"role": "assistant", "content": "c"}])
    assert system == ""
    assert [m["role"] for m in msgs] == ["user", "assistant", "user", "assistant", "user"]
    assert msgs[2]["content"] == "a\n\nb"


def test_gaia_stays_the_builtin_provider():
    gaia = model_registry.get_provider("cloud:gaia")
    assert isinstance(gaia, model_registry.GaiaProvider) and gaia.provider_key == "gaia"
    assert cp.parse_model_id("cloud:gaia") is None
    assert cp.parse_model_id("cloud:anthropic:claude-opus-5-5") == ("anthropic", "claude-opus-5-5")
