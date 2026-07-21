"""Regression for the AI-service preflight. A misconfigured / dead AI service
(the embeddings-fleet class of failure) must be detectable up front.

Covers the config-only probes without network. The live probes (text LLM,
embeddings) are exercised by the running server's startup log + /api/health.
"""
import asyncio

import config
from functions.preflight import _check_vision_config, _check_whisper_config


def _restore(mp_attrs, saved):
    for k, v in saved.items():
        setattr(config, k, v)


def test_vision_config_ok_when_url_and_key_present(monkeypatch):
    # A recognized cloud host (googleapis.com) takes the config-only path —
    # no network probe happens in this test.
    monkeypatch.setattr(config, "AI_VISION_API_URL",
                        "https://generativelanguage.googleapis.com/v1beta/openai")
    monkeypatch.setattr(config, "AI_VISION_API_KEY", "AIzaKEY")
    monkeypatch.setattr(config, "AI_API_KEY", "")
    r = asyncio.run(_check_vision_config())
    assert r["ok"] is True


def test_vision_config_fails_with_no_url(monkeypatch):
    monkeypatch.setattr(config, "AI_VISION_API_URL", "")
    r = asyncio.run(_check_vision_config())
    assert r["ok"] is False
    assert "unset" in r["detail"]


def test_vision_config_fails_with_no_key(monkeypatch):
    monkeypatch.setattr(config, "AI_VISION_API_URL",
                        "https://generativelanguage.googleapis.com/v1beta/openai")
    monkeypatch.setattr(config, "AI_VISION_API_KEY", "")
    monkeypatch.setattr(config, "AI_API_KEY", "")
    r = asyncio.run(_check_vision_config())
    assert r["ok"] is False


def test_whisper_gemini_requires_key(monkeypatch):
    monkeypatch.setattr(config, "WHISPER_API_URL", "https://gen.googleapis/v1beta")
    monkeypatch.setattr(config, "WHISPER_FORMAT", "gemini")
    monkeypatch.setattr(config, "WHISPER_API_KEY", "")
    monkeypatch.setattr(config, "AI_API_KEY", "")
    r = _check_whisper_config()
    assert r["ok"] is False


def test_whisper_local_needs_no_key(monkeypatch):
    monkeypatch.setattr(config, "WHISPER_API_URL", "http://localhost:8003/v1")
    monkeypatch.setattr(config, "WHISPER_FORMAT", "local")
    monkeypatch.setattr(config, "WHISPER_API_KEY", "")
    monkeypatch.setattr(config, "AI_API_KEY", "")
    r = _check_whisper_config()
    assert r["ok"] is True


def test_whisper_fails_with_no_url(monkeypatch):
    monkeypatch.setattr(config, "WHISPER_API_URL", "")
    r = _check_whisper_config()
    assert r["ok"] is False
