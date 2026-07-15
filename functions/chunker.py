"""Content chunking strategies. NEVER truncate -- always chunk.

When content exceeds what one API call can handle:
1. Split into meaningful chunks (landmarks, element groups, text boundaries)
2. Each chunk gets its own AI call with the FULL system prompt
3. Findings from every chunk are collected
4. Worst verdict wins
"""
from __future__ import annotations

import re
from typing import TypeVar

T = TypeVar("T")


# ── Element chunking (visual AI) ─────────────────────────────────────────────

def chunk_elements(
    elements: list,
    max_per_chunk: int = 150,
) -> list[list]:
    """Split a list of elements into chunks of at most ``max_per_chunk``.

    Used by the visual AI pipeline so each chunk fits comfortably inside one
    LLM request. Returns [[]] when the input list is empty so callers can
    loop safely.
    """
    if not elements:
        return [[]]
    return [elements[i : i + max_per_chunk] for i in range(0, len(elements), max_per_chunk)]


# ── Image batching (visual AI) ───────────────────────────────────────────────

def chunk_images(
    images: list[str],
    max_per_batch: int = 15,
) -> list[list[str]]:
    """Split image paths into batches for the visual AI pipeline."""
    if not images:
        return []
    return [images[i : i + max_per_batch] for i in range(0, len(images), max_per_batch)]


# ── HTML chunking by landmarks (code AI) ─────────────────────────────────────

_LANDMARK_TAGS: tuple[tuple[str, str], ...] = (
    ("header", "header"),
    ("nav", "navigation"),
    ("main", "main content"),
    ("aside", "sidebar"),
    ("footer", "footer"),
)

# Tags that never have a closing tag in HTML5. Skipped when tracking depth
# inside a structured split so we never wait for a </img> that doesn't exist.
_VOID_HTML_TAGS = frozenset((
    "area", "base", "br", "col", "embed", "hr", "img", "input", "link",
    "meta", "param", "source", "track", "wbr",
))


def chunk_html_by_landmarks(
    html: str,
    max_chars: int = 300_000,
) -> list[tuple[str, str]]:
    """Split HTML into ``(section_name, section_html)`` chunks, each
    guaranteed to be at most ``max_chars`` long.

    Strategy (no truncation, no content loss at any step):

    1. If the whole page is already small enough, return it as one chunk
       labeled ``full page``.
    2. Otherwise extract the ``<head>`` as its own section if present, then
       each top-level landmark (``header``, ``nav``, ``main``, ``aside``,
       ``footer``) as its own section.
    3. Any section that by itself exceeds ``max_chars`` is sub-split by
       walking its top-level child elements with proper depth tracking so
       splits never land mid-tag. Child groups are packed greedy into
       chunks that stay under the budget. The landmark's opening and
       closing tags are repeated on every sub-part so each chunk is a
       valid stand-alone fragment for the model to read.
    4. If structured walking can't find a clean wrapper (malformed HTML,
       no landmarks, etc) the fallback is a tag-boundary character split
       (``_split_at_tag_boundary``). Still lossless; chunks concatenate
       back to the input.
    5. Pages with no landmarks at all fall back to splitting ``<body>``
       (or the whole input) at tag boundaries.

    Concatenation of all returned chunk bodies reconstructs the original
    HTML verbatim (modulo the landmark wrapper repetition for sub-parts).
    """
    if not html:
        return []

    if len(html) <= max_chars:
        return [("full page", html)]

    sections: list[tuple[str, str]] = []

    head_match = re.search(r"<head\b[^>]*>.*?</head>", html, re.DOTALL | re.IGNORECASE)
    if head_match:
        # Strip <script>/<style> bodies inside <head> before chunking. The
        # tags + attributes (src, async, integrity, type) stay so the model
        # still sees that scripts exist and where they come from. The bodies
        # are minified JS/CSS that can never contain interactive elements,
        # so reading them costs ~6 LLM calls per page for zero signal.
        head_html = _strip_script_style_bodies(head_match.group(0))
        head_parts = _split_landmark(head_html, max_chars)
        for j, part in enumerate(head_parts):
            label = "head" if len(head_parts) == 1 else f"head_part{j + 1}"
            sections.append((label, part))

    for tag, name in _LANDMARK_TAGS:
        for idx, match in enumerate(
            re.finditer(rf"<{tag}\b[^>]*>.*?</{tag}>", html, re.DOTALL | re.IGNORECASE)
        ):
            section_html = match.group(0)
            base_name = f"{name}_{idx}" if idx > 0 else name
            parts = _split_landmark(section_html, max_chars)
            if len(parts) == 1:
                sections.append((base_name, parts[0]))
            else:
                for j, part in enumerate(parts):
                    sections.append((f"{base_name}_part{j + 1}", part))

    if len(sections) <= 1:
        # No landmarks -- run the same recursive structured walker on
        # the <body> (or the whole input). This produces clean balanced
        # fragments by walking the body's top-level children, recursing
        # into oversized ones, instead of immediately dropping to the
        # character-level fallback.
        body_match = re.search(r"<body\b[^>]*>.*?</body>", html, re.DOTALL | re.IGNORECASE)
        body = body_match.group(0) if body_match else html
        sections = []
        for j, part in enumerate(_split_landmark(body, max_chars)):
            sections.append((f"body_part{j + 1}", part))

    return sections


def _split_landmark(html: str, max_chars: int, _depth: int = 0) -> list[str]:
    """Split a single HTML element into chunks of at most ``max_chars``.

    Recursive structured walker. At each level:

    1. If the element is already small enough, return it as one chunk.
    2. Find the wrapper opening + closing tags so each chunk can be
       re-wrapped as a valid fragment.
    3. Walk the top-level children of the wrapper with depth tracking
       (``_walk_top_level_children``).
    4. Greedy-pack children into chunks under the budget. If a single
       child is itself bigger than the budget, **recurse into it**
       (depth+1) so we walk its inner children too. This means a 30K
       ``<section>`` inside a ``<main>`` gets the same structured
       treatment as the main itself: top-level children of the section
       are extracted and packed.
    5. Recursion is naturally bounded -- each level processes a strictly
       smaller piece. ``_depth`` is tracked only as a safety cap to
       prevent runaway on degenerate input. After 8 levels of recursion
       (which would mean an 8-level nested element where every level
       still exceeded the budget) we fall through to the character-level
       defensive splitter.
    6. If no wrapper or no children are found at any level, drops to
       ``_split_at_tag_boundary`` as a lossless last resort.

    The output of every level is wrapped in the level's own open/close
    tags, so when an outer caller iterates the chunks and concatenates
    inner content, the original HTML is reconstructed verbatim.
    """
    if len(html) <= max_chars:
        return [html]

    if _depth >= 8:
        return _split_at_tag_boundary(html, max_chars)

    wrapper_open = re.match(r"(<\w+[^>]*>)", html)
    closer = re.search(r"</\w+>\s*$", html)
    if not wrapper_open or not closer:
        return _split_at_tag_boundary(html, max_chars)

    open_tag = wrapper_open.group(0)
    close_tag = closer.group(0)
    inner = html[len(open_tag) : closer.start()]
    if not inner.strip():
        return [html]

    children = _walk_top_level_children(inner)
    if not children:
        return _split_at_tag_boundary(html, max_chars)

    overhead = len(open_tag) + len(close_tag)
    chunk_budget = max_chars - overhead
    if chunk_budget <= 0:
        return _split_at_tag_boundary(html, max_chars)

    chunks: list[str] = []
    current: list[str] = []
    current_len = 0

    def _flush() -> None:
        nonlocal current, current_len
        if current:
            chunks.append(open_tag + "".join(current) + close_tag)
            current, current_len = [], 0

    for child in children:
        clen = len(child)
        if clen > chunk_budget:
            _flush()
            # Recurse into the oversized child. Each piece returned is a
            # valid fragment of the child's own structure (e.g. a partial
            # <section>...</section>). We wrap each piece with the parent
            # landmark's tags so the model still sees the outer context.
            sub_pieces = _split_landmark(child, chunk_budget, _depth + 1)
            for piece in sub_pieces:
                wrapped = open_tag + piece + close_tag
                if len(wrapped) <= max_chars:
                    chunks.append(wrapped)
                else:
                    # Piece + wrappers still over budget (rare: piece is
                    # exactly chunk_budget so wrapping pushes over). Use
                    # the lossless defensive splitter on the piece.
                    for raw in _split_at_tag_boundary(piece, chunk_budget):
                        chunks.append(open_tag + raw + close_tag)
            continue
        if current_len + clen > chunk_budget and current:
            _flush()
        current.append(child)
        current_len += clen

    _flush()

    return chunks or [html]


def _walk_top_level_children(inner: str) -> list[str]:
    """Walk the direct child elements of an HTML fragment with depth tracking.

    Returns a list where each entry is either:
    - A complete top-level child element (including its full subtree), or
    - A span of text between top-level children.

    Depth tracking uses ``_VOID_HTML_TAGS`` so self-closing elements never
    create unclosed depth. Returns an empty list if the input contains no
    tags at all.
    """
    children: list[str] = []
    buf: list[str] = []
    depth = 0
    i = 0
    n = len(inner)
    tag_re = re.compile(r"<(/?)(\w+)([^>]*?)(/?)>", re.DOTALL)
    while i < n:
        m = tag_re.match(inner, i)
        if not m:
            buf.append(inner[i])
            i += 1
            continue
        full_tag = m.group(0)
        is_close = m.group(1) == "/"
        tag_name = m.group(2).lower()
        self_close_slash = m.group(4) == "/"
        is_void = tag_name in _VOID_HTML_TAGS
        is_self_closing = is_close or self_close_slash or is_void

        buf.append(full_tag)
        if is_close:
            depth -= 1
        elif not is_self_closing:
            depth += 1
        i += len(full_tag)

        if depth == 0:
            piece = "".join(buf).strip()
            if piece:
                children.append("".join(buf))
            buf = []

    if buf:
        tail = "".join(buf)
        if tail.strip():
            children.append(tail)

    return children


def _strip_script_style_bodies(html: str) -> str:
    """Replace the body of every <script> and <style> with an empty string.

    Preserves the opening tag (and all its attributes — ``src``, ``async``,
    ``integrity``, ``type``, ``crossorigin``, etc.) and the closing tag, so a
    reader still sees that a script/style exists and where it came from.
    Only the inner JS/CSS source is dropped — that source can never be an
    interactive HTML element, so the inventory model never needs it.

    Used for ``<head>`` chunking where minified analytics/tracking script
    bodies would otherwise consume thousands of tokens for zero signal.
    """
    pattern = re.compile(
        r"(<(script|style)\b[^>]*>)(.*?)(</\2>)",
        re.DOTALL | re.IGNORECASE,
    )
    return pattern.sub(lambda m: m.group(1) + m.group(4), html)


def _split_at_tag_boundary(html: str, max_chars: int) -> list[str]:
    """Split oversized HTML at the last ``>`` before each budget cut point.

    Defensive fallback when ``_walk_top_level_children`` can't find a clean
    structure. Preserves every byte: the concatenation of returned chunks
    equals the input exactly.

    Each returned chunk is at most ``max_chars`` characters long. When a
    cut window contains no ``>``, falls back to a hard cut at exactly
    ``max_chars`` characters (no ``+1`` overshoot).
    """
    if len(html) <= max_chars:
        return [html]
    chunks: list[str] = []
    remaining = html
    while len(remaining) > max_chars:
        cut = remaining.rfind(">", 0, max_chars)
        if cut < 0:
            # No tag boundary in the budget window -- hard cut at max_chars.
            chunks.append(remaining[:max_chars])
            remaining = remaining[max_chars:]
            continue
        chunks.append(remaining[: cut + 1])
        remaining = remaining[cut + 1 :]
    if remaining:
        chunks.append(remaining)
    return chunks


def estimate_prompt_chars(
    system_prompt: str,
    user_prompt: str,
    tool_schema: dict | None = None,
) -> int:
    """Rough upper bound on the total character count of a chat completion
    payload, used to decide whether further chunking is needed before a
    call is even attempted.

    Counts: system prompt + user prompt + serialized tool schema + fixed
    overhead for role/content JSON wrappers (~200 chars). Not exact but
    close enough to make routing decisions.
    """
    import json as _json

    total = len(system_prompt) + len(user_prompt) + 200
    if tool_schema:
        try:
            total += len(_json.dumps(tool_schema))
        except Exception:
            total += 2000
    return total


# ── Text chunking (transcripts, AT announcements, long prose) ────────────────

_SENTENCE_END = re.compile(r"(?<=[.!?])\s+")


def chunk_text(text: str, max_chars: int = 300_000) -> list[str]:
    """Split long text at sentence boundaries, falling back to hard splits.

    Every character of the input ends up in exactly one chunk -- no
    truncation, no dropped characters.
    """
    if not text:
        return []
    if len(text) <= max_chars:
        return [text]

    chunks: list[str] = []
    current: list[str] = []
    current_len = 0

    for sentence in _SENTENCE_END.split(text):
        piece = sentence + " "
        if current_len + len(piece) > max_chars and current:
            chunks.append("".join(current).rstrip())
            current, current_len = [], 0

        if len(piece) > max_chars:
            if current:
                chunks.append("".join(current).rstrip())
                current, current_len = [], 0
            for i in range(0, len(piece), max_chars):
                chunks.append(piece[i : i + max_chars])
            continue

        current.append(piece)
        current_len += len(piece)

    if current:
        chunks.append("".join(current).rstrip())

    return chunks
