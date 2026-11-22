#!/usr/bin/env python3
"""Local model discovery and selection (Ollama mocked)."""

from unittest.mock import MagicMock, patch

import pytest
import requests

import local_models as lm

TAGS = {"models": [
    {"name": "qwen3:8b", "size": 5_200_000_000,
     "details": {"family": "qwen3", "parameter_size": "8.2B", "quantization_level": "Q4_K_M"}},
    {"name": "nomic-embed-text:latest", "size": 274_000_000,
     "details": {"family": "nomic-bert", "families": ["nomic-bert"], "parameter_size": "137M"}},
    {"name": "gemma3:27b", "size": 17_000_000_000,
     "details": {"family": "gemma3", "parameter_size": "27B", "quantization_level": "Q4_K_M"}},
]}


def json_resp(data):
    resp = MagicMock()
    resp.json.return_value = data
    resp.raise_for_status.return_value = None
    return resp


def test_list_models_excludes_embeddings_and_flags_big_models():
    with patch.object(lm.requests, "get", return_value=json_resp(TAGS)):
        models = lm.list_models()
    assert [m["name"] for m in models] == ["gemma3:27b", "qwen3:8b"]
    by = {m["name"]: m for m in models}
    assert by["gemma3:27b"]["too_big"] and not by["qwen3:8b"]["too_big"]
    assert lm.label(by["qwen3:8b"]) == "qwen3:8b · 8.2B · Q4_K_M · 5.2 GB"
    assert "may be slow" in lm.label(by["gemma3:27b"])


def test_list_models_ollama_down():
    with patch.object(lm.requests, "get", side_effect=requests.ConnectionError()):
        with pytest.raises(lm.OllamaUnavailable, match="ollama serve"):
            lm.list_models()


def test_model_info_effective_context():
    data = {"model_info": {"qwen3.context_length": 40960}, "capabilities": ["completion", "thinking"]}
    with patch.object(lm.requests, "post", return_value=json_resp(data)):
        info = lm.model_info("qwen3:8b")
    assert info["native_context"] == 40960
    assert info["context_length"] == min(40960, lm.SERVER_CONTEXT_LENGTH)
    assert "thinking" in info["capabilities"]
    assert lm.effective_context(8192) == 8192
    assert lm.effective_context(None) == lm.SERVER_CONTEXT_LENGTH


