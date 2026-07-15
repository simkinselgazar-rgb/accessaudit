"""Vision-language-model helpers for image captioning and OCR.

Routes through ``LLMClient`` so image calls land on the local Gemma 26B
model (``AI_LOCAL_JUDGE_URL``) per the accuracy-first routing rule in
``functions/llm.py:_select_model``. Every call is saved to
``<review>/llm_transcripts/`` like every other model call.

Three public entry points:

- ``describe_image(path)`` -- one-sentence natural caption for alt-text
  semantic verification (SC 1.1.1).
- ``extract_text_from_image(path)`` -- read all rendered text out of an
  image. Empty string when the image is not text-bearing. Feeds the
  SC 1.4.5 "Images of Text" check and scanned-PDF OCR (DOC-1.4.5-SCAN).
- ``analyze_image(path)`` -- one VLM call that returns both a caption
  and extracted text, so per-image checks don't pay for two round trips.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from typing import Any

from functions.llm import LLMClient, LLMError
from functions.parser import get_content_text, loose_json_loads

logger = logging.getLogger(__name__)


# System prompts kept tight so Gemma 26B stays on task. No prose padding.
_CAPTION_SYSTEM_PROMPT = (
    "You are an accessibility auditor writing alt text for a blind user. "
    "Look at the image and produce ONE sentence that conveys what the "
    "image shows, in the same way a screen reader should announce it. "
    "Do NOT start with 'image of' or 'picture of'. Do NOT guess at "
    "hidden context. Describe only what is visible. 40 words max."
)

_OCR_SYSTEM_PROMPT = (
    "You are an OCR engine. Transcribe EVERY piece of visible text in the "
    "image, in reading order. Preserve line breaks. Do not translate. Do "
    "not describe the image. If the image contains no readable text at "
    "all (photographs, icons, logos with no words, decorative graphics), "
    "output the literal single word NONE and nothing else."
)

_COMBINED_SYSTEM_PROMPT = (
    "You are an accessibility auditor looking at a single image. Produce "
    "a JSON object with EXACTLY two fields:\n"
    "  caption: one-sentence description of what the image visually "
    "depicts, written the way a screen reader should announce it (no "
    "'image of' prefix, 40 words max, describe only what is visible).\n"
    "  extracted_text: every piece of readable text rendered inside the "
    "image, in reading order, preserving line breaks as \\n. If the "
    "image contains no readable text at all, use an empty string.\n"
    "Return ONLY the JSON object. No prose before or after."
)


async def describe_image(
    path: str,
    *,
    prompt: str | None = None,
    timeout_s: float = 180.0,
) -> str:
    """Return a one-sentence natural-language caption for an image.

    Uses the local Gemma 26B VLM via ``LLMClient``. Returns an empty
    string when the call fails or the image path does not exist --
    callers should treat empty as "no caption available" and skip
    verification rather than flagging false positives.
    """
    if not path or not os.path.exists(path):
        return ""
    user_prompt = prompt or (
        "Caption this image in one sentence for a blind screen reader user."
    )
    try:
        client = LLMClient()
        raw = await asyncio.wait_for(
            client.call(
                system_prompt=_CAPTION_SYSTEM_PROMPT,
                user_prompt=user_prompt,
                images=[path],
                temperature=0.1,
                label="image_caption",
            ),
            timeout=timeout_s,
        )
    except Exception as exc:
        logger.warning("describe_image failed for %s: %s", path, exc)
        return ""
    return _clean_prose(get_content_text(raw))


async def extract_text_from_image(
    path: str,
    *,
    timeout_s: float = 180.0,
) -> str:
    """Return every piece of text rendered inside the image.

    Returns an empty string when the image contains no readable text.
    Used for SC 1.4.5 (Images of Text) and for scanned-PDF OCR.
    Gemma 26B has strong OCR capability for printed text, weaker for
    handwriting -- treat empty output as "no text detected" rather than
    "definitely no text present" when the image is a handwritten scan.
    """
    if not path or not os.path.exists(path):
        return ""
    try:
        client = LLMClient()
        raw = await asyncio.wait_for(
            client.call(
                system_prompt=_OCR_SYSTEM_PROMPT,
                user_prompt="Transcribe the text in this image.",
                images=[path],
                temperature=0.0,
                label="image_ocr",
            ),
            timeout=timeout_s,
        )
    except Exception as exc:
        logger.warning("extract_text_from_image failed for %s: %s", path, exc)
        return ""
    text = _clean_prose(get_content_text(raw))
    # The prompt tells the model to return "NONE" when the image is
    # not text-bearing. Treat it (and common variants) as empty.
    if text.upper() in ("NONE", "NO TEXT", "(NONE)", "(NO TEXT)"):
        return ""
    return text


async def analyze_image(
    path: str,
    *,
    timeout_s: float = 240.0,
) -> dict[str, str]:
    """Combined caption + OCR in a single VLM call.

    Returns ``{"caption": "...", "extracted_text": "..."}``. Both fields
    are always strings (possibly empty). When the call fails returns
    ``{"caption": "", "extracted_text": ""}`` so callers can safely
    chain without None checks.

    Prefer this over ``describe_image`` + ``extract_text_from_image``
    when you need both signals for the same image -- it saves one VLM
    round trip, which matters when batch-processing a page of 50 images.
    """
    empty = {"caption": "", "extracted_text": ""}
    if not path or not os.path.exists(path):
        return empty
    try:
        client = LLMClient()
        raw = await asyncio.wait_for(
            client.call(
                system_prompt=_COMBINED_SYSTEM_PROMPT,
                user_prompt="Analyze this image.",
                images=[path],
                temperature=0.0,
                label="image_caption_ocr_combined",
            ),
            timeout=timeout_s,
        )
    except Exception as exc:
        logger.warning("analyze_image failed for %s: %s", path, exc)
        return empty

    content = _clean_prose(get_content_text(raw))
    if not content:
        return empty

    # Prefer strict JSON, fall back to loose JSON (handles Gemma's
    # control-token formatting), fall back to text splitting as last resort.
    payload: dict[str, Any] | None = None
    try:
        payload = json.loads(content)
    except json.JSONDecodeError:
        try:
            payload = loose_json_loads(content)
        except Exception:
            payload = None

    if isinstance(payload, dict):
        caption = str(payload.get("caption", "")).strip()
        extracted = str(payload.get("extracted_text", "")).strip()
        if extracted.upper() in ("NONE", "NO TEXT", "(NONE)"):
            extracted = ""
        return {"caption": caption, "extracted_text": extracted}

    # Last-resort heuristic: the model returned prose instead of the
    # requested JSON shape. Keep the FULL prose as the caption -- no
    # truncation. Downstream consumers (alt-text verifier, judge)
    # decide whether a long caption is useful; we don't second-guess
    # them by clipping signal here.
    logger.info("analyze_image: non-JSON response, using prose fallback")
    return {"caption": content, "extracted_text": ""}


async def verify_alt_text_semantic(
    image_path: str,
    alt_text: str,
    *,
    vlm_caption: str | None = None,
    similarity_threshold: float = 0.55,
) -> dict[str, Any]:
    """Cross-check author-supplied alt text against a VLM caption.

    When a caption is not supplied, this function calls ``describe_image``
    to generate one. The caption and the alt text are then embedded via
    bge-m3 and compared by cosine similarity:

    - sim >= threshold: alt matches the image
    - sim <  threshold: alt likely does not match what the image shows

    Returns a dict with:
      - ``match``: bool
      - ``similarity``: float in [-1, 1]
      - ``caption``: the VLM caption used for comparison
      - ``alt_text``: the author-supplied alt (unchanged)
      - ``reason``: short explanation of the verdict

    When either embedding or captioning fails the result is returned
    with ``match=True`` and ``reason="verification_unavailable"`` so
    the caller never flags a false positive just because the verifier
    was offline. Callers must treat a ``False`` match as a SIGNAL, not
    a definitive failure -- a human auditor should confirm.
    """
    alt_text = (alt_text or "").strip()
    caption = (vlm_caption or "").strip()

    if not alt_text:
        return {
            "match": False,
            "similarity": 0.0,
            "caption": caption,
            "alt_text": "",
            "reason": "no_alt_text",
        }

    if not caption:
        caption = await describe_image(image_path)
    if not caption:
        return {
            "match": True,
            "similarity": 0.0,
            "caption": "",
            "alt_text": alt_text,
            "reason": "verification_unavailable",
        }

    try:
        from functions.embeddings import cosine_similarity, embed_batch

        vectors = await embed_batch([alt_text, caption])
        if len(vectors) != 2 or not any(vectors[0]) or not any(vectors[1]):
            raise RuntimeError("empty embedding vector")
        sim = cosine_similarity(vectors[0], vectors[1])
    except Exception as exc:
        logger.info("verify_alt_text_semantic: embeddings unavailable (%s)", exc)
        return {
            "match": True,
            "similarity": 0.0,
            "caption": caption,
            "alt_text": alt_text,
            "reason": "verification_unavailable",
        }

    matched = sim >= similarity_threshold
    if matched:
        reason = "alt_matches_caption"
    elif sim >= similarity_threshold - 0.10:
        reason = "alt_marginal_match"
    else:
        reason = "alt_does_not_match_image"

    return {
        "match": matched,
        "similarity": round(sim, 4),
        "caption": caption,
        "alt_text": alt_text,
        "reason": reason,
    }


# ── Helpers ─────────────────────────────────────────────────────────────────

_MARKUP_RE = re.compile(r"^```(?:json|text)?\s*|\s*```$", re.DOTALL)


def _clean_prose(text: str) -> str:
    """Strip code fences, leading/trailing whitespace, stray quotes."""
    if not text:
        return ""
    cleaned = _MARKUP_RE.sub("", text).strip()
    # Strip a single pair of wrapping double-quotes if the model
    # over-quoted a single sentence.
    if len(cleaned) >= 2 and cleaned.startswith('"') and cleaned.endswith('"'):
        cleaned = cleaned[1:-1].strip()
    return cleaned
