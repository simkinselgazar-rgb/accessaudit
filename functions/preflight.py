"""Preflight checks for every AI I/O dependency.

The system talks to several independent services: the text LLM, the vision/
video model, the embeddings host, and the whisper transcriber. Each is
configured separately (see CLAUDE.md "Provider-agnostic AI"). A misconfigured
or unreachable service is silent until a review wastes time on it -- a dead
embeddings host once degraded dedup for a whole run before anyone
noticed. This module probes each service so the failure is visible up front
(at startup and via /api/health).

Returns per-service status; never raises.
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


async def _check_text_llm() -> dict:
    try:
        from functions.llm import LLMClient
        health = await LLMClient().check_health()
        ok = health.get("status") == "ok"
        return {"ok": ok, "detail": health.get("status", "?")}
    except Exception as exc:
        return {"ok": False, "detail": f"{type(exc).__name__}: {exc}"}


async def _check_embeddings() -> dict:
    """Embed a tiny string and confirm a real (non-zero) vector comes back.
    A zero vector means the embeddings host is unreachable (embed() falls back
    to zeros) -- the exact dedup-degrading failure we want surfaced."""
    try:
        from functions.embeddings import embed
        from config import EMBEDDINGS_API_URL, EMBEDDINGS_MODEL
        vec = await embed("preflight healthcheck")
        if vec and any(vec):
            return {"ok": True, "detail": f"{EMBEDDINGS_MODEL} ({len(vec)}-dim)"}
        return {
            "ok": False,
            "detail": f"zero/empty vector from {EMBEDDINGS_API_URL} "
                      f"-- embeddings host unreachable",
        }
    except Exception as exc:
        return {"ok": False, "detail": f"{type(exc).__name__}: {exc}"}


def _check_vision_config() -> dict:
    """Vision/video calls are too costly to probe live on every health hit;
    verify the endpoint + key are configured and not pointing at a known-dead
    local fleet."""
    from config import AI_VISION_API_URL, AI_VISION_API_KEY, AI_API_KEY
    url = (AI_VISION_API_URL or "").strip()
    key = (AI_VISION_API_KEY or AI_API_KEY or "").strip()
    if not url:
        return {"ok": False, "detail": "AI_VISION_API_URL unset"}
    if not key:
        return {"ok": False, "detail": "no vision API key (AI_VISION_API_KEY/AI_API_KEY)"}
    return {"ok": True, "detail": url}


def _check_whisper_config() -> dict:
    """Whisper is only exercised on media-bearing pages; verify config rather
    than transcribe a probe clip on every health hit."""
    from config import WHISPER_API_URL, WHISPER_FORMAT, WHISPER_API_KEY, AI_API_KEY
    url = (WHISPER_API_URL or "").strip()
    fmt = (WHISPER_FORMAT or "").strip().lower()
    if not url:
        return {"ok": False, "detail": "WHISPER_API_URL unset"}
    if fmt in ("gemini", "openai") and not (WHISPER_API_KEY or AI_API_KEY):
        return {"ok": False, "detail": f"{fmt} whisper but no API key"}
    return {"ok": True, "detail": f"{fmt} @ {url}"}


async def preflight_ai_services(*, probe_embeddings: bool = True) -> dict:
    """Probe every AI I/O service. Returns
    ``{"all_ok": bool, "services": {name: {"ok": bool, "detail": str}}}``.
    Live probes: text LLM, embeddings. Config-only: vision, whisper.
    """
    services = {
        "text_llm": await _check_text_llm(),
        "vision": _check_vision_config(),
        "whisper": _check_whisper_config(),
    }
    services["embeddings"] = (
        await _check_embeddings() if probe_embeddings
        else {"ok": True, "detail": "skipped"}
    )
    all_ok = all(s["ok"] for s in services.values())
    return {"all_ok": all_ok, "services": services}


async def log_preflight() -> dict:
    """Run preflight and log each service's status. Called at startup so a
    dead/misconfigured AI service is visible before any review runs."""
    result = await preflight_ai_services()
    for name, s in result["services"].items():
        level = logging.INFO if s["ok"] else logging.ERROR
        logger.log(
            level, "PREFLIGHT %s: %s -- %s",
            name, "OK" if s["ok"] else "FAILED", s["detail"],
        )
    if not result["all_ok"]:
        logger.error(
            "PREFLIGHT: one or more AI services are not ready -- reviews will "
            "be degraded or fail until fixed (check settings.json endpoints/keys)"
        )
    return result
