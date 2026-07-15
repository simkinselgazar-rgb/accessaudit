"""Layer 2 of the cached-code-AI architecture: JavaScript prefilter.

Phase 1 (``functions.code_analyzer.analyze_page_code``) would otherwise
burn LLM calls reading React reconciler internals, webpack boilerplate,
Google Analytics setup, and other code that can never be an
accessibility violation. This module strips that noise before chunking,
keeping ONLY the nodes/expressions that can plausibly affect
accessibility. On a typical university homepage the reduction is
10-40x: ~2.2 MB of readable JS collapses to ~50-200 KB of
accessibility-relevant excerpts, which in turn drops the Phase 1 chunk
count from ~90 to ~5-10.

Strategy (two-stage with graceful degradation):

1. ``_esprima_filter`` -- if the optional ``esprima`` package is
   installed, parse the JS to an AST and walk it, keeping only nodes
   whose source range touches any of ``_A11Y_APIS``. This is precise:
   ``document.querySelector`` inside a comment or string literal does
   NOT trigger the keep rule. esprima is ES6 only, so it throws on
   modern syntax (``?.``, ``??``, private class fields, top-level
   await); when it throws we fall through.

2. ``_regex_filter`` -- deterministic text-based fallback. Splits the
   source into line windows and keeps any window that contains one of
   the ~40 accessibility API patterns. False positives are OK (e.g.
   ``focus`` inside a variable name) because the Phase 1 LLM still
   judges each kept window. False negatives are the real risk, so the
   pattern set is deliberately broad.

3. **Lossless safety net** -- if both stages produce less than 5% of the
   input size AND the input is under ``MAX_UNFILTERED_CHARS``, we
   return the raw source instead. This guards against the pathological
   case where a page uses an obscure accessibility idiom our filters
   don't recognise -- better to spend a few extra LLM calls than miss
   a real violation.

The filter is called by ``functions.code_analyzer.analyze_page_code``
BEFORE chunking so the benefit compounds: smaller input -> fewer
chunks -> fewer LLM calls -> faster Phase 1. Output is still
plain-text JavaScript suitable for the existing chunker.
"""
from __future__ import annotations

import logging
import re
from typing import Iterable

logger = logging.getLogger(__name__)


# Defaults for ``filter_accessibility_code``. Every one of these is
# overridable per call so nothing here is a hard cap.
#
# ``DEFAULT_SKIP_FILTER_UNDER_CHARS`` -- for inputs smaller than this
# we return the raw JS unchanged (passthrough). This only ever sends
# MORE data to the LLM, never less.
#
# ``DEFAULT_SUSPICIOUSLY_SMALL_RATIO`` -- when the filter's output is
# smaller than this fraction of its input, we treat it as a
# pathological parse and fall back to the raw input. Again, sends
# MORE data, never less. Pass ``None`` to disable the safety net.
#
# ``DEFAULT_CONTEXT_WINDOW_LINES`` -- number of neighbouring lines
# kept around every accessibility-API match. Larger = more enclosing
# context in the Phase 1 prompt. Pass a larger value if you want the
# LLM to see bigger scopes.
DEFAULT_SKIP_FILTER_UNDER_CHARS = 15_000
DEFAULT_SUSPICIOUSLY_SMALL_RATIO = 0.02
DEFAULT_CONTEXT_WINDOW_LINES = 15


# ─── Accessibility API patterns ──────────────────────────────────────────────
# Any line containing one of these substrings is kept. These strings
# are matched literally against source text -- the regex filter does
# not parse JS, it just grabs windows around interesting identifiers.
#
# This list is the authoritative definition of "accessibility-relevant"
# for the prefilter. Update it when you discover a new idiom that
# should reach the Phase 1 LLM. Order does not matter; duplicates are
# fine.
_A11Y_APIS: tuple[str, ...] = (
    # Event wiring -- keyboard/pointer/focus/form events that often have
    # accessibility implications. "keydown" alone is too narrow because
    # some sites use ".on('keydown', ...)" style, so match the quoted
    # string form too.
    "addEventListener(", "removeEventListener(",
    "'click'", '"click"',
    "'keydown'", '"keydown"',
    "'keyup'", '"keyup"',
    "'keypress'", '"keypress"',
    "'focus'", '"focus"',
    "'focusin'", '"focusin"',
    "'focusout'", '"focusout"',
    "'blur'", '"blur"',
    "'mousedown'", '"mousedown"',
    "'mouseup'", '"mouseup"',
    "'mouseover'", '"mouseover"',
    "'mouseenter'", '"mouseenter"',
    "'input'", '"input"',
    "'change'", '"change"',
    "'submit'", '"submit"',
    "'reset'", '"reset"',
    "'invalid'", '"invalid"',

    # Direct handler-attribute style
    "onclick", "onkeydown", "onkeyup", "onkeypress",
    "onfocus", "onblur", "onfocusin", "onfocusout",
    "oninput", "onchange", "onsubmit", "oninvalid",
    "onmousedown", "onmouseup",

    # Focus management
    ".focus(", ".blur(", "activeElement",
    "focusTrap", "trapFocus", "FocusTrap",
    "tabindex", "tabIndex",

    # ARIA / roles / accessible names / live regions
    "aria-", "ariaLabel", "ariaLabelledBy", "ariaDescribedBy",
    "role=", 'role:', "setAttribute('role'", 'setAttribute("role"',
    "setAttribute('aria-", 'setAttribute("aria-',
    "getAttribute('aria-", 'getAttribute("aria-',
    "hasAttribute('aria-", 'hasAttribute("aria-',
    "removeAttribute('aria-", 'removeAttribute("aria-',
    "aria-hidden", "aria-expanded", "aria-pressed", "aria-current",
    "aria-live", "aria-atomic", "aria-busy", "aria-describedby",
    "aria-labelledby", "aria-label", "aria-disabled", "aria-required",
    "aria-invalid", "aria-selected", "aria-checked",
    'role: "', "role: '",
    "document.activeElement",

    # Visibility / display mutations (hiding/showing content can trap AT)
    ".style.display", ".style.visibility", ".style.opacity",
    "hidden=", "hidden:", "hidden ",
    ".hidden =",
    "inert",

    # Keyboard trap / shortcut / navigation patterns
    "preventDefault(", "stopPropagation(", "stopImmediatePropagation(",
    "keyCode", "charCode", "which",
    ".key ", ".key==", ".key===", "event.key",
    "key ===", "key ==", "e.key",
    "KeyboardEvent", "Enter", "Escape", "Tab", "Space",
    "ArrowUp", "ArrowDown", "ArrowLeft", "ArrowRight",

    # DOM lookups / traversal used to wire the above
    "querySelector", "getElementById", "getElementsBy",
    "closest(", "matches(",

    # Animation / motion / media (SC 2.3.x, 1.4.2, 1.2.x)
    "prefers-reduced-motion", "matchMedia", "requestAnimationFrame",
    "autoplay", "muted", ".play(", ".pause(",
    "animation", "transition", "transform", "keyframes",

    # Form validation
    "validity.", "setCustomValidity", "checkValidity", "reportValidity",
    "required", "aria-required",

    # Dialog / modal / overlay
    "showModal(", ".show()", ".close()", "<dialog", "role=\"dialog\"",
    "role='dialog'",

    # Language / lang
    "document.documentElement.lang", "lang=",

    # Scrolling / viewport (zoom, reflow)
    "overflow", "scroll", "maximum-scale", "user-scalable",
)


# ─── Public entry point ──────────────────────────────────────────────────────

def filter_accessibility_code(
    js_source: str,
    *,
    strategy: str = "auto",
    skip_filter_under_chars: int = DEFAULT_SKIP_FILTER_UNDER_CHARS,
    suspiciously_small_ratio: float | None = DEFAULT_SUSPICIOUSLY_SMALL_RATIO,
    context_window_lines: int = DEFAULT_CONTEXT_WINDOW_LINES,
) -> tuple[str, dict]:
    """Return ``(filtered_js, stats)`` — keep only accessibility-relevant JS.

    ``strategy`` selects the pipeline:

    * ``"auto"`` (default) tries esprima, then regex, then lossless
      passthrough. This is what production callers want.
    * ``"esprima"`` forces the AST path; if esprima is unavailable or
      raises, returns the raw input.
    * ``"regex"`` skips the AST and uses the regex filter directly.
    * ``"none"`` returns the input unchanged. Useful for A/B testing.

    ``skip_filter_under_chars`` -- inputs smaller than this are
    returned verbatim (passthrough sends MORE data to the LLM, never
    less). Set to 0 to always filter.

    ``suspiciously_small_ratio`` -- if the filter's output is smaller
    than this fraction of its input, the function returns the raw
    input instead. Again, sends MORE data, never less. Set to
    ``None`` to disable the safety net (always trust the filter).

    ``context_window_lines`` -- number of neighbouring lines kept
    around every accessibility-API match in the regex path. Larger =
    more enclosing context for the Phase 1 LLM.

    Stats dict carries the input/output sizes, the path taken, and any
    fallback reason. Callers log this for the Phase 1 transcript so the
    reduction is auditable alongside the LLM calls.
    """
    stats: dict = {
        "input_chars": len(js_source or ""),
        "output_chars": 0,
        "path": "none",
        "ratio": 1.0,
    }

    if not js_source:
        stats["path"] = "empty"
        return "", stats

    if skip_filter_under_chars > 0 and len(js_source) < skip_filter_under_chars:
        stats["output_chars"] = len(js_source)
        stats["path"] = "passthrough_small"
        return js_source, stats

    if strategy == "none":
        stats["output_chars"] = len(js_source)
        stats["path"] = "passthrough_forced"
        return js_source, stats

    # ── AST path ──────────────────────────────────────────────────────
    # The filter is trusted. No passthrough safety net -- if the
    # filter returns a small result, that's the correct answer for
    # that input (most JS chunks genuinely have no accessibility
    # surface). When a chunk's output looks too small, the fix is to
    # widen _A11Y_APIS, not to paper over it with passthrough.
    if strategy in ("auto", "esprima"):
        ast_result = _esprima_filter(js_source)
        if ast_result is not None:
            stats["output_chars"] = len(ast_result)
            stats["path"] = "esprima"
            stats["ratio"] = len(ast_result) / max(1, len(js_source))
            return ast_result, stats

    # ── Regex path ────────────────────────────────────────────────────
    if strategy in ("auto", "regex"):
        regex_result = _regex_filter(js_source, context_window_lines=context_window_lines)
        stats["output_chars"] = len(regex_result)
        stats["path"] = "regex"
        stats["ratio"] = len(regex_result) / max(1, len(js_source))
        return regex_result, stats

    # Unknown strategy -- behave like passthrough, never silently drop.
    stats["output_chars"] = len(js_source)
    stats["path"] = "passthrough_unknown_strategy"
    return js_source, stats


# ─── AST path (esprima, optional) ────────────────────────────────────────────

def _esprima_filter(js_source: str) -> str | None:
    """Parse with esprima, walk the AST, keep only nodes touching the
    accessibility API surface. Returns None when esprima isn't
    available or can't parse the input -- the caller falls back to the
    regex path. Never raises.
    """
    try:
        import esprima  # type: ignore
    except ImportError:
        return None

    try:
        tree = esprima.parseScript(
            js_source,
            options={"loc": True, "range": True, "tolerant": True},
        )
    except Exception as exc:
        logger.info("js_ast_filter: esprima parse failed (%s) -- falling back", exc)
        return None

    # Walk the AST and collect the source ranges of every node whose
    # textual span contains any accessibility-API marker. Ranges are
    # merged so overlapping/adjacent keeps don't duplicate code in the
    # output. The tree is JS's program node -- we walk .body.
    keep_ranges: list[tuple[int, int]] = []
    for node in _walk_ast(tree):
        rng = getattr(node, "range", None)
        if not rng or len(rng) != 2:
            continue
        start, end = int(rng[0]), int(rng[1])
        if start < 0 or end > len(js_source) or start >= end:
            continue
        span = js_source[start:end]
        if _span_touches_a11y(span):
            keep_ranges.append((start, end))

    if not keep_ranges:
        return ""

    merged = _merge_ranges(keep_ranges)
    parts = [js_source[s:e] for s, e in merged]
    # Two blank lines between kept spans so the chunker has clean
    # sentence boundaries to split on.
    return "\n\n".join(parts)


def _walk_ast(node) -> Iterable:
    """Yield every child node of an esprima AST, depth-first.

    Esprima nodes are objects with ``.type`` and arbitrary child
    attributes. We iterate every attribute that is itself a Node or a
    list of Nodes. Anything else (strings, numbers, booleans) is
    skipped.
    """
    yield node
    for key in getattr(node, "__dict__", {}).keys() if hasattr(node, "__dict__") else dir(node):
        if key.startswith("_"):
            continue
        try:
            val = getattr(node, key)
        except Exception:
            continue
        if val is None:
            continue
        if isinstance(val, (str, int, float, bool)):
            continue
        if isinstance(val, (list, tuple)):
            for item in val:
                if item is not None and hasattr(item, "type"):
                    yield from _walk_ast(item)
        elif hasattr(val, "type"):
            yield from _walk_ast(val)


def _span_touches_a11y(span: str) -> bool:
    for marker in _A11Y_APIS:
        if marker in span:
            return True
    return False


def _merge_ranges(ranges: list[tuple[int, int]]) -> list[tuple[int, int]]:
    if not ranges:
        return []
    ranges = sorted(ranges)
    merged: list[tuple[int, int]] = [ranges[0]]
    for start, end in ranges[1:]:
        last_start, last_end = merged[-1]
        if start <= last_end:
            merged[-1] = (last_start, max(last_end, end))
        else:
            merged.append((start, end))
    return merged


# ─── Regex path (always available) ───────────────────────────────────────────

# Pre-compile into one big alternation for a single scan pass.
_A11Y_REGEX = re.compile(
    "|".join(re.escape(m) for m in _A11Y_APIS),
)


def _regex_filter(
    js_source: str,
    *,
    context_window_lines: int = DEFAULT_CONTEXT_WINDOW_LINES,
) -> str:
    """Keep line windows around every accessibility-API match.

    Splits the source into lines, marks lines that contain any of the
    ``_A11Y_APIS`` substrings, then expands each marked line into a
    window of ``context_window_lines`` above and below. Overlapping
    windows are merged. The surviving line indices are emitted in
    order, separated by blank-line gap markers so the Phase 1 chunker
    still has clean splits.
    """
    lines = js_source.splitlines()
    if not lines:
        return ""

    marked: list[int] = []
    for i, line in enumerate(lines):
        if _A11Y_REGEX.search(line):
            marked.append(i)

    if not marked:
        return ""

    keep_flags = [False] * len(lines)
    for idx in marked:
        lo = max(0, idx - context_window_lines)
        hi = min(len(lines) - 1, idx + context_window_lines)
        for k in range(lo, hi + 1):
            keep_flags[k] = True

    out: list[str] = []
    prev_kept = False
    for i, keep in enumerate(keep_flags):
        if keep:
            if not prev_kept and out:
                out.append("")
                out.append(f"// … {i} lines skipped …")
                out.append("")
            out.append(lines[i])
            prev_kept = True
        else:
            prev_kept = False

    return "\n".join(out)
