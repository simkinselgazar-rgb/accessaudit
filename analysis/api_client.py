"""Thin backward-compatible wrapper around ``functions.llm.LLMClient``.

Check files call ``ai_client.analyze(system_prompt, user_prompt, ...)`` and
get back a normalized WCAG assessment dict. All retry, parsing, and
restructuring logic lives in ``LLMClient.call_with_tools`` so every
structured-output call site in the system shares the same recovery cascade.
"""
from __future__ import annotations

import logging
from typing import Any

from functions.llm import LLMClient, LLMError
from functions.media import encode_image as _encode_image  # noqa: F401 -- backward compat
from functions.parser import get_content_text, normalize_wcag_assessment
from functions.tools import WCAG_ASSESSMENT_TOOL

logger = logging.getLogger(__name__)


class AIClient:
    """Thin wrapper that exposes the pre-existing ``analyze`` surface."""

    def __init__(
        self,
        base_url: str | None = None,
        vision_base_url: str | None = None,
        model: str | None = None,
        vision_model: str | None = None,
        timeout: float | None = None,
        max_retries: int | None = None,
        max_tokens: int | None = None,
        max_images: int | None = None,
    ) -> None:
        from config import AI_MAX_IMAGES

        self._llm = LLMClient(
            base_url=base_url,
            vision_url=vision_base_url,
            model=model,
            vision_model=vision_model,
            timeout=timeout,
            max_retries=max_retries,
            max_tokens=max_tokens,
        )
        self.max_images = max_images or AI_MAX_IMAGES

        # Fields the check pipeline reads directly.
        self.base_url = self._llm.base_url
        self.vision_base_url = self._llm.vision_url
        self.model = self._llm.model
        self.vision_model = self._llm.vision_model
        self.timeout = self._llm.timeout
        self.max_retries = self._llm.max_retries
        self.max_tokens = self._llm.max_tokens

    async def close(self) -> None:
        await self._llm.close()

    async def check_health(self) -> dict[str, Any]:
        return await self._llm.check_health()

    async def analyze(
        self,
        system_prompt: str,
        user_prompt: str,
        image_paths: list[str] | None = None,
        video_path: str | None = None,
        temperature: float = 0.2,
        needs_audio: bool = False,
    ) -> dict[str, Any]:
        """Run a WCAG assessment call and return a normalized result dict.

        Delegates the full retry + parse + restructure cascade to
        ``LLMClient.call_with_tools``: parse first reply -> retry up to 3
        times with corrective note -> LLM-based prose restructuring as
        last resort. We just normalize the recovered payload.
        """
        try:
            payload = await self._llm.call_with_tools(
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                tool_name="report_wcag_assessment",
                tool_schema=WCAG_ASSESSMENT_TOOL,
                images=image_paths,
                video=video_path,
                temperature=temperature,
                needs_audio=needs_audio,
            )
        except LLMError as llm_err:
            logger.error(
                "LLM call failed (status=%s): %s",
                llm_err.status_code,
                llm_err,
            )
            if video_path:
                fallback = await self._video_prose_fallback(
                    user_prompt, video_path, temperature,
                )
                if fallback:
                    return fallback
            raise RuntimeError(f"LLM call failed: {llm_err}") from llm_err

        if payload is None:
            raise RuntimeError(
                "LLM returned no parseable tool call after retries and restructuring"
            )

        result = normalize_wcag_assessment(payload)
        logger.info(
            "LLM analysis complete: %s (confidence=%.2f, findings=%d)",
            result.get("conformance_level", "?"),
            result.get("confidence", 0),
            len(result.get("findings", [])),
        )
        return result

    async def _video_prose_fallback(
        self,
        user_prompt: str,
        video_path: str,
        temperature: float,
    ) -> dict[str, Any] | None:
        """Last resort when a video LLM call fails at the network level.

        Used only when ``LLMError`` is raised (HTTP failure, payload too
        large, etc) -- not for parse failures, which ``call_with_tools``
        handles internally via retries and restructuring. Sends the same
        video to the model WITHOUT the tool requirement so it can produce
        prose, then wraps the prose as a single INFO finding so the judge
        can still use the observations as evidence.
        """
        import uuid

        from models import ConformanceLevel, Finding, Severity

        logger.info("Video prose fallback: retrying video without tool requirement")
        try:
            raw = await self._llm.call(
                system_prompt=(
                    "You are watching a video of a website being tested for "
                    "keyboard accessibility. Describe EXACTLY what you see: "
                    "which elements receive focus, whether focus indicators "
                    "are visible, any keyboard traps, whether menus open and "
                    "close properly, and the tab order. Be specific about "
                    "element names and positions on the page."
                ),
                user_prompt=user_prompt,
                video=video_path,
                tools=None,
                tool_choice=None,
                temperature=temperature,
                label="keyboard_video_prose_fallback",
            )
            content = get_content_text(raw)
            if not content or len(content) < 50:
                return None

            return {
                "conformance_level": ConformanceLevel.NOT_EVALUATED,
                "confidence": 0.3,
                "confidence_reasoning": "Video analyzed via prose fallback (no structured tool call)",
                "findings": [Finding(
                    id=str(uuid.uuid4()),
                    element="[VIDEO OBSERVATIONS]",
                    issue=content,
                    impact=(
                        "Video analysis could not produce structured findings. "
                        "These observations should be reviewed by the judge."
                    ),
                    recommendation="",
                    severity=Severity.INFO,
                    source="visual_ai",
                )],
                "summary": "Video analyzed in prose mode (structured output unavailable)",
            }
        except Exception as exc:
            logger.warning("Video prose fallback also failed: %s", exc)
            return None
