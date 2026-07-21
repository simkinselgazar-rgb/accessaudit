"""Configuration for the AccessAudit application.

Loads settings from: environment variables > settings.json > built-in defaults.
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

# ── Paths ────────────────────────────────────────────────────────────────────
PROJECT_DIR = Path(os.environ.get("WCAG_PROJECT_DIR", str(Path(__file__).parent)))
GUIDELINES_DIR = PROJECT_DIR / "guidelines"
REVIEWS_DIR = Path(os.environ.get("WCAG_REVIEWS_DIR", str(PROJECT_DIR / "reviews")))
TEMPLATES_DIR = PROJECT_DIR / "templates"
STATIC_DIR = PROJECT_DIR / "static"

# ── Settings file ────────────────────────────────────────────────────────────
_SETTINGS_FILE = PROJECT_DIR / "settings.json"


def _load_settings_file() -> dict:
    """Load settings.json if it exists."""
    if _SETTINGS_FILE.exists():
        try:
            return json.loads(_SETTINGS_FILE.read_text())
        except Exception:
            logger.exception("Failed to parse %s, using built-in defaults", _SETTINGS_FILE)
    return {}


_saved: dict = _load_settings_file()


def _setting(key: str, env_var: str, default: str) -> str:
    """Resolve a setting value: env var > settings.json > default."""
    env_val = os.environ.get(env_var, "")
    if env_val:
        return env_val
    saved_val = _saved.get(key, "")
    if saved_val:
        return str(saved_val)
    return default


# ── Backend defaults by provider ─────────────────────────────────────────────
_BACKEND_DEFAULTS: dict[str, dict[str, str]] = {
    # Self-hosted OpenAI-compatible stack (vLLM, llama.cpp, LM Studio,
    # Ollama, MLX...). No default URLs are assumed -- set api_base_url /
    # ai_vision_api_url in settings.json to your own endpoints.
    "vllm": {
        "base_url": "",
        "model": "Qwen/Qwen3-32B",
        "vision_model": "Qwen/Qwen2.5-VL-32B-Instruct",
        "vision_url": "",
    },
    "gemini": {
        "base_url": "https://generativelanguage.googleapis.com/v1beta/openai",
        "model": "gemini-3.0-flash",
        "vision_model": "gemini-3.0-flash",
        "vision_url": "https://generativelanguage.googleapis.com/v1beta/openai",
    },
    "openai": {
        "base_url": "https://api.openai.com/v1",
        "model": "gpt-4o",
        "vision_model": "gpt-4o",
        "vision_url": "https://api.openai.com/v1",
    },
    "anthropic": {
        "base_url": "https://api.anthropic.com/v1",
        "model": "claude-sonnet-4-20250514",
        "vision_model": "claude-sonnet-4-20250514",
        "vision_url": "https://api.anthropic.com/v1",
    },
    "openrouter": {
        "base_url": "https://openrouter.ai/api/v1",
        "model": "google/gemini-2.5-flash",
        "vision_model": "google/gemini-2.5-flash",
        "vision_url": "https://openrouter.ai/api/v1",
    },
}

# ── AI backend ───────────────────────────────────────────────────────────────
# Default backend is an API provider: a fresh install only needs one
# API key. Self-hosting is fully supported -- set ai_backend to "vllm"
# and point api_base_url (and any per-role URLs) at your own
# OpenAI-compatible servers in settings.json.
AI_BACKEND = _setting("ai_backend", "WCAG_AI_BACKEND", "openrouter")
AI_API_KEY = _setting("api_key", "WCAG_AI_API_KEY", "")

_defaults = _BACKEND_DEFAULTS.get(AI_BACKEND, _BACKEND_DEFAULTS["vllm"])

AI_API_BASE_URL = _setting("api_base_url", "WCAG_AI_API_BASE_URL", _defaults["base_url"])
AI_MODEL = _setting("ai_model", "WCAG_AI_MODEL", _defaults["model"])
AI_VISION_MODEL = _setting("ai_vision_model", "WCAG_AI_VISION_MODEL", _defaults["vision_model"])
AI_VISION_API_URL = _setting("ai_vision_api_url", "WCAG_AI_VISION_API_URL", _defaults["vision_url"])
AI_VISION_API_KEY = _setting("ai_vision_api_key", "WCAG_AI_VISION_API_KEY", "") or AI_API_KEY

AI_TIMEOUT = int(_setting("ai_timeout", "WCAG_AI_TIMEOUT", "1200"))
AI_MAX_RETRIES = int(_setting("ai_max_retries", "WCAG_AI_MAX_RETRIES", "3"))
AI_MAX_TOKENS = int(_setting("ai_max_tokens", "WCAG_AI_MAX_TOKENS", "16384"))

# Max concurrent in-flight LLM calls across the whole process.
#
#   1  = serial (safe default, required for local vLLM/mlx-vlm which OOM
#        under concurrent load; identical to the prior asyncio.Lock behavior)
#   N  = up to N calls in flight at once. ONLY safe with cloud providers
#        (Gemini, OpenAI, Anthropic, OpenRouter). Phase 4 SC checks +
#        Phase 5 judge benefit most -- ~3x wall-clock speedup at N=10
#        on large-university-site-sized runs. Set via settings.json key
#        "ai_max_concurrent" or env var WCAG_AI_MAX_CONCURRENT.
#
# Defaults to 1 for ALL backends until operator opts in, because
# getting concurrency wrong on a local stack crashes the model servers.
AI_MAX_CONCURRENT = int(_setting("ai_max_concurrent", "WCAG_AI_MAX_CONCURRENT", "1"))

# ── Judge model (separate, potentially stronger model) ───────────────────────
AI_JUDGE_MODEL = _setting("ai_judge_model", "WCAG_AI_JUDGE_MODEL", "") or AI_MODEL
AI_JUDGE_API_URL = _setting("ai_judge_api_url", "WCAG_AI_JUDGE_API_URL", "") or AI_API_BASE_URL
AI_JUDGE_API_KEY = _setting("ai_judge_api_key", "WCAG_AI_JUDGE_API_KEY", "") or AI_API_KEY

# ── Final reviewer model (Pro tier — runs once per review on the complete ACR
#    to catch contradictions, miscalibrated verdicts, citation errors, and
#    prose-tone issues. Defaults to AI_JUDGE_MODEL; set explicitly to point
#    at a stronger model like gemini-3-pro-preview for production reviews.)
AI_REVIEWER_MODEL = _setting("ai_reviewer_model", "WCAG_AI_REVIEWER_MODEL", "") or AI_JUDGE_MODEL
AI_REVIEWER_API_URL = _setting("ai_reviewer_api_url", "WCAG_AI_REVIEWER_API_URL", "") or AI_JUDGE_API_URL
AI_REVIEWER_API_KEY = _setting("ai_reviewer_api_key", "WCAG_AI_REVIEWER_API_KEY", "") or AI_JUDGE_API_KEY

# ── Video model (for video analysis — local models often can't handle large
# videos, so this can point to a cloud provider while everything else stays local)
AI_VIDEO_MODEL = _setting("ai_video_model", "WCAG_AI_VIDEO_MODEL", "")
AI_VIDEO_API_URL = _setting("ai_video_api_url", "WCAG_AI_VIDEO_API_URL", "")
AI_VIDEO_API_KEY = _setting("ai_video_api_key", "WCAG_AI_VIDEO_API_KEY", "") or AI_API_KEY

# ── Local model fleet (vLLM specific) ────────────────────────────────────────
#
# Fallback / restructure routing. Honors settings.json (ai_fallback_url /
# ai_fallback_model) and env vars. Defaults to empty -- no fallback configured.
# When unset, _try_fallback short-circuits and the caller raises a normal
# LLMError instead of churning on a dead endpoint. Set a real secondary
# endpoint here (or in settings.json) only if you actually have one running.
AI_FALLBACK_URL = _setting("ai_fallback_url", "WCAG_AI_FALLBACK_URL", "")
AI_FALLBACK_MODEL = _setting("ai_fallback_model", "WCAG_AI_FALLBACK_MODEL", "")
AI_FALLBACK_VISION_URL = _setting(
    "ai_fallback_vision_url", "WCAG_AI_FALLBACK_VISION_URL", "",
)
AI_FALLBACK_VISION_MODEL = _setting(
    "ai_fallback_vision_model", "WCAG_AI_FALLBACK_VISION_MODEL", "",
)

# Optional dedicated endpoints for specialist roles. These default to
# empty -- when unset, model routing falls through to the configured
# vision/text endpoints. Operators running a multi-model self-hosted
# stack set them explicitly in settings.json.

# Fast multimodal explorer (Phase 2 exploration, audio detection)
AI_EXPLORER_URL = _setting(
    "ai_explorer_url", "WCAG_AI_EXPLORER_URL", "",
)
AI_EXPLORER_MODEL = _setting(
    "ai_explorer_model", "WCAG_AI_EXPLORER_MODEL",
    "google/gemma-3n-E4B-it",
)

# Gemma 4 26B -- local image analysis + local judge
AI_LOCAL_JUDGE_URL = _setting(
    "ai_local_judge_url", "WCAG_AI_LOCAL_JUDGE_URL", "",
)
AI_LOCAL_JUDGE_MODEL = _setting(
    "ai_local_judge_model", "WCAG_AI_LOCAL_JUDGE_MODEL",
    "google/gemma-3-27b-it",
)
AI_LOCAL_JUDGE_API_KEY = _setting(
    "ai_local_judge_api_key", "WCAG_AI_LOCAL_JUDGE_API_KEY", "",
) or AI_VISION_API_KEY

# ── Rate limiting ────────────────────────────────────────────────────────────
_rpm = int(_setting("ai_rpm", "WCAG_AI_RPM", "0"))
if not _rpm and AI_BACKEND == "gemini":
    _rpm = 10
AI_RPM = _rpm
AI_MAX_IMAGES = int(_setting("ai_max_images", "WCAG_AI_MAX_IMAGES", "10"))

# ── Image encoding for vision calls ──────────────────────────────────────────
# Target dimension for images sent to vision models (see
# functions/media.py:encode_image for how elongated screenshots are
# handled). Raise for stronger models / lower for tight local memory.
AI_IMAGE_MAX_DIM = int(_setting("ai_image_max_dim", "WCAG_AI_IMAGE_MAX_DIM", "1280"))
AI_IMAGE_QUALITY = int(_setting("ai_image_quality", "WCAG_AI_IMAGE_QUALITY", "85"))

# Allow auditing private/loopback/tailnet URLs (intranet apps, staging
# servers, local fixtures). Default off: it re-opens SSRF, so enable it
# only on a trusted single-operator deployment.
ALLOW_PRIVATE_URLS = _setting(
    "allow_private_urls", "WCAG_ALLOW_PRIVATE_URLS", "",
).lower() in ("true", "1", "yes")

# ── Verification ─────────────────────────────────────────────────────────────
VERIFICATION_ENABLED = os.environ.get("WCAG_VERIFICATION_ENABLED", "false").lower() in ("true", "1", "yes")
VERIFICATION_TIMEOUT = int(os.environ.get("WCAG_VERIFICATION_TIMEOUT", "180"))

# ── Video / audio capture ────────────────────────────────────────────────────
VIDEO_CAPTURE_DURATION = int(os.environ.get("WCAG_VIDEO_CAPTURE_DURATION", "15"))
PAGE_OBSERVATION_DURATION = int(os.environ.get("WCAG_PAGE_OBSERVATION_DURATION", "60"))
FLASH_DETECTION_FPS = int(os.environ.get("WCAG_FLASH_DETECTION_FPS", "6"))
AI_FRAME_INTERVAL = int(os.environ.get("WCAG_AI_FRAME_INTERVAL", "5"))

# ── Whisper (audio transcription) ────────────────────────────────────────────
# ── Ollama embeddings (bge-m3 for cross-page consistency + dedup) ──────
# The embeddings host runs bge-m3 on Ollama at port 11434. Used for:
#   - SC 3.2.3 Consistent Navigation: compare nav menu text across
#     crawled pages by cosine similarity of menu-text embeddings
#   - SC 3.2.4 Consistent Identification: compare label text for the
#     same component across pages
#   - Cross-page finding deduplication: cluster semantically identical
#     findings (e.g. "missing alt on logo" reported 10 times -> one
#     entry with 10 affected URLs) without requiring identical strings
# No default endpoint: when unset, embedding-based dedup/consistency
# degrade gracefully (zero-vector fallback, logged + bypass-logged).
# Point at any OpenAI-compatible /embeddings endpoint
# (embeddings_format=openai) or an Ollama /api/embeddings endpoint
# (embeddings_format=ollama).
EMBEDDINGS_API_URL = _setting(
    "embeddings_api_url", "WCAG_EMBEDDINGS_API_URL", "",
)
EMBEDDINGS_MODEL = _setting("embeddings_model", "WCAG_EMBEDDINGS_MODEL", "bge-m3")
EMBEDDINGS_DIM = int(_setting("embeddings_dim", "WCAG_EMBEDDINGS_DIM", "1024"))
# Embedding API format. Two payload/response shapes are supported:
#   "ollama" (default): payload {"model","prompt"} -> body["embedding"]
#   "openai":           payload {"model","input"}  -> body["data"][0]["embedding"]
# Set "openai" to point EMBEDDINGS_API_URL at Gemini's OpenAI-compatible
# /embeddings endpoint (text-embedding-004, gemini-embedding-001), OpenAI's
# embeddings, or any other OpenAI-compatible embedding host.
EMBEDDINGS_FORMAT = _setting(
    "embeddings_format", "WCAG_EMBEDDINGS_FORMAT", "ollama",
).lower()
# Embedding API key. Falls back to the VISION/multimodal provider key
# (AI_VISION_API_KEY, itself falling back to AI_API_KEY) so embeddings track
# whatever provider you point the multimodal stack at -- when vision runs on
# Gemini and text on OpenRouter, embeddings pointed at Gemini get the Gemini
# key, not the OpenRouter text key. Set embeddings_api_key explicitly to
# override. Empty string is legal for Ollama (no auth).
EMBEDDINGS_API_KEY = _setting(
    "embeddings_api_key", "WCAG_EMBEDDINGS_API_KEY", "",
) or AI_VISION_API_KEY

# No default endpoint: when unset, audio transcription (caption
# verification) is skipped with a logged capture gap.
WHISPER_API_URL = _setting("whisper_api_url", "WCAG_WHISPER_API_URL", "")
# Whisper API format. Three transcription paths are supported:
#   "local"  (default): faster-whisper HTTP at {WHISPER_API_URL}/transcribe
#   "openai": OpenAI Whisper at {WHISPER_API_URL}/audio/transcriptions
#   "gemini": Gemini native multimodal generateContent with inline_data
#             audio. Uses {WHISPER_API_URL} as the Gemini API base.
# "auto" tries local then openai (legacy behavior).
WHISPER_FORMAT = _setting("whisper_format", "WCAG_WHISPER_FORMAT", "auto").lower()
# Whisper key follows the VISION/multimodal provider (Gemini "gemini" format
# and any cloud whisper share that stack). Falls back to AI_VISION_API_KEY ->
# AI_API_KEY. Set whisper_api_key explicitly to override.
WHISPER_API_KEY = _setting(
    "whisper_api_key", "WCAG_WHISPER_API_KEY", "",
) or AI_VISION_API_KEY
WHISPER_GEMINI_MODEL = _setting(
    "whisper_gemini_model", "WCAG_WHISPER_GEMINI_MODEL", "gemini-2.5-flash",
)
# Default: faster-whisper large-v3-turbo for caption verification accuracy.
# large-v3-turbo is a distilled version of large-v3 -- near-large-v3
# accuracy with meaningful speedup. Much better than medium on accented
# English, lecture audio, and overlapping-speaker panels (common on
# lecture recordings and event videos). Override the env var if your
# Whisper endpoint hosts a different model size.
WHISPER_MODEL_SIZE = os.environ.get("WCAG_WHISPER_MODEL_SIZE", "large-v3-turbo")
WHISPER_COMPUTE_TYPE = os.environ.get("WCAG_WHISPER_COMPUTE_TYPE", "int8")
WHISPER_DEVICE = os.environ.get("WCAG_WHISPER_DEVICE", "cpu")
MEDIA_DOWNLOAD_TIMEOUT = int(os.environ.get("WCAG_MEDIA_DOWNLOAD_TIMEOUT", "30"))

# ── Report defaults ──────────────────────────────────────────────────────────
REPORT_FORMAT = os.environ.get("WCAG_REPORT_FORMAT", "508")
COVERAGE_LEVEL = os.environ.get("WCAG_COVERAGE_LEVEL", "AA")
WCAG_VERSION = os.environ.get("WCAG_WCAG_VERSION", "2.2")

# ── Company branding ─────────────────────────────────────────────────────────
COMPANY_NAME = os.environ.get("WCAG_COMPANY_NAME", "")
COMPANY_LOGO_PATH = os.environ.get("WCAG_COMPANY_LOGO", str(STATIC_DIR / "company_logo.svg"))

# ── Web server ───────────────────────────────────────────────────────────────
WEB_HOST = os.environ.get("WCAG_WEB_HOST", "127.0.0.1")
WEB_PORT = int(os.environ.get("WCAG_WEB_PORT", "5050"))

# ── Capture pipeline ─────────────────────────────────────────────────────────
CAPTURE_PIPELINE = _setting("capture_pipeline", "WCAG_CAPTURE_PIPELINE", "v2")
PLAYWRIGHT_TIMEOUT = int(os.environ.get("WCAG_PLAYWRIGHT_TIMEOUT", "60000"))
VIEWPORT_WIDTH = int(os.environ.get("WCAG_VIEWPORT_WIDTH", "1280"))
VIEWPORT_HEIGHT = int(os.environ.get("WCAG_VIEWPORT_HEIGHT", "720"))
VIEWPORT_NARROW = int(os.environ.get("WCAG_VIEWPORT_NARROW", "320"))
ZOOM_FACTOR = float(os.environ.get("WCAG_ZOOM_FACTOR", "2.0"))

# ── PDF settings ─────────────────────────────────────────────────────────────
PDF_DPI = int(os.environ.get("WCAG_PDF_DPI", "150"))
