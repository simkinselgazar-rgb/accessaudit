"""DOM chunking for token-safe AI analysis.

Large pages (500KB+ HTML) exceed LLM context windows. This module
creates a compact "skeleton" DOM that preserves structure but strips
content, enabling Phase 1 to analyze any page regardless of size.

Strategy:
1. Parse HTML into a skeleton — strip text > 50 chars, collapse
   repetitive siblings, remove inline styles, strip script/style bodies
2. Split into sections by landmark/semantic structure
3. Estimate token count per section
4. Route to single-call or multi-call based on total size
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from html.parser import HTMLParser

logger = logging.getLogger(__name__)

# Rough token estimation: ~4 chars per token for English HTML
_CHARS_PER_TOKEN = 4


@dataclass
class DOMSection:
    """A semantic section of the page (header, nav, main, footer, etc.)."""
    name: str
    html: str
    landmark_role: str = ""
    token_estimate: int = 0


@dataclass
class DOMChunks:
    """Chunked DOM ready for AI analysis."""
    skeleton_html: str
    sections: list[DOMSection] = field(default_factory=list)
    css_summary: str = ""
    js_event_summary: str = ""
    total_tokens_estimate: int = 0
    original_size: int = 0
    skeleton_size: int = 0


def chunk_dom(html: str, css: str = "", js_events: str = "") -> DOMChunks:
    """Break a page's HTML into token-safe chunks for AI analysis.

    Args:
        html: Full page HTML
        css: Extracted CSS rules (optional)
        js_events: Summary of JS event handlers (optional)

    Returns:
        DOMChunks with skeleton + sections
    """
    original_size = len(html)
    logger.info("DOM CHUNKER: input size %d chars (%d estimated tokens)",
                original_size, original_size // _CHARS_PER_TOKEN)

    # Step 1: Split into semantic sections (full HTML, no truncation)
    sections = _split_into_sections(html)

    # Step 2: Estimate tokens
    css_tokens = len(css) // _CHARS_PER_TOKEN
    js_tokens = len(js_events) // _CHARS_PER_TOKEN
    section_tokens = sum(len(s.html) // _CHARS_PER_TOKEN for s in sections)

    logger.info("DOM CHUNKER: css=%d tokens, js=%d tokens, sections=%d (%d tokens total)",
                css_tokens, js_tokens, len(sections), section_tokens)

    for s in sections:
        s.token_estimate = len(s.html) // _CHARS_PER_TOKEN

    result = DOMChunks(
        skeleton_html="",  # No longer used
        sections=sections,
        css_summary=css,
        js_event_summary=js_events,
        total_tokens_estimate=css_tokens + js_tokens + section_tokens,
        original_size=original_size,
        skeleton_size=0,
    )

    logger.info("DOM CHUNKER: %d chars, %d sections (%d tokens total)",
                original_size, len(sections), section_tokens)

    return result


# Any single landmark section larger than this gets sub-split so the
# Phase 1 inventory audit model does not have to emit a massive tool call
# in a single generation. Tuned for the local Qwen 35B + Gemma 26B output
# windows -- larger chunks make the model run out of output tokens
# mid-tool-call. Always read together with the canonical splitter in
# functions/chunker.py:chunk_html_by_landmarks which does the actual work.
MAX_SECTION_CHARS = 25_000


def _split_into_sections(html: str) -> list[DOMSection]:
    """Split HTML into semantic sections based on landmarks and structure.

    Delegates the actual splitting to ``functions.chunker.chunk_html_by_landmarks``
    so this code path and the check pipeline's code AI splitter share a
    single implementation. The canonical splitter walks landmarks, sub-
    splits oversized landmarks at top-level child boundaries with proper
    depth tracking, falls back to tag-boundary splits, and never truncates.
    """
    from functions.chunker import chunk_html_by_landmarks

    chunks = chunk_html_by_landmarks(html, max_chars=MAX_SECTION_CHARS)
    sections: list[DOMSection] = []
    role_for_name = {
        "header": "banner",
        "navigation": "navigation",
        "main content": "main",
        "sidebar": "complementary",
        "footer": "contentinfo",
        "head": "",
    }
    for label, chunk_html in chunks:
        # Map the canonical splitter's labels back to the legacy role
        # field that downstream code expects on DOMSection.
        base = label.split("_part")[0]
        sections.append(DOMSection(
            name=label,
            html=chunk_html,
            landmark_role=role_for_name.get(base, ""),
        ))

    logger.info(
        "DOM CHUNKER: split into %d sections: %s",
        len(sections),
        ", ".join(s.name for s in sections),
    )
    return sections


def extract_css_summary(html: str) -> str:
    """Extract CSS rules relevant to accessibility from inline styles and style tags."""
    css_parts = []

    # Inline <style> tags
    style_blocks = re.findall(r"<style\b[^>]*>(.*?)</style>", html, re.DOTALL | re.IGNORECASE)
    for block in style_blocks:
        css_parts.append(block)

    if not css_parts:
        return ""

    full_css = "\n".join(css_parts)

    # Filter to accessibility-relevant rules
    relevant_patterns = [
        r"[^}]*(?:display\s*:\s*none|visibility\s*:\s*hidden|opacity\s*:\s*0)[^}]*",
        r"[^}]*(?:outline\s*:\s*(?:none|0)|outline-width\s*:\s*0)[^}]*",
        r"[^}]*(?:overflow\s*:\s*hidden)[^}]*",
        r"[^}]*(?:color|background|font-size|line-height)[^}]*",
        r"[^}]*:focus[^}]*",
        r"[^}]*:hover[^}]*",
        r"[^}]*@media[^{]*\{[^}]*\}",
    ]

    relevant_rules = []
    for pattern in relevant_patterns:
        matches = re.findall(pattern, full_css, re.IGNORECASE)
        relevant_rules.extend(matches)

    summary = "\n".join(relevant_rules)
    return summary


def extract_js_event_summary(page_evaluate_result: list[dict]) -> str:
    """Format JS event handler data from Playwright page.evaluate().

    The evaluate result should be a list of dicts with:
    {selector, tag, events: [event_name, ...], hasOnclick, hasOnfocus, ...}
    """
    if not page_evaluate_result:
        return ""

    lines = ["JavaScript Event Handlers:"]
    for item in page_evaluate_result:
        selector = item.get("selector", "?")
        tag = item.get("tag", "?")
        events = item.get("events", [])
        if events:
            lines.append(f"  <{tag}> {selector}: {', '.join(events)}")

    return "\n".join(lines)


def split_html_safely(html: str, max_chunk_size: int = 40_000) -> list[str]:
    """Backward-compat wrapper. Use ``functions.chunker._split_at_tag_boundary``
    or ``chunk_html_by_landmarks`` directly in new code.
    """
    from functions.chunker import _split_at_tag_boundary

    return _split_at_tag_boundary(html, max_chunk_size)
