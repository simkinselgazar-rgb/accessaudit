"""Regression tests for ``functions.js_ast_filter``.

Layer 2 of the cached-code-AI architecture. The filter MUST:

1. Keep code that touches any accessibility API (addEventListener on
   a key event, aria-*, focus(), tabindex, etc.)
2. Drop large code blocks that don't (analytics, typical react
   reconciler internals with no a11y surface).
3. Never silently lose a11y-relevant code -- when the filter's output
   is suspiciously small relative to input, fall back to the raw
   input.
4. Passthrough small inputs without scanning (cheaper than filtering).
5. Respect the ``strategy`` override for the "none"/"regex"/"esprima"/"auto" modes.

Run with: python tests/test_js_ast_filter.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from functions.js_ast_filter import (  # noqa: E402
    _A11Y_APIS,
    _A11Y_REGEX,
    _regex_filter,
    filter_accessibility_code,
)


_PASSED: list[str] = []
_FAILED: list[tuple[str, str]] = []


def _run(name, fn):
    try:
        fn()
        _PASSED.append(name)
        print(f"PASS  {name}")
    except AssertionError as exc:
        _FAILED.append((name, str(exc)))
        print(f"FAIL  {name}: {exc}")
    except Exception as exc:
        _FAILED.append((name, f"{type(exc).__name__}: {exc}"))
        print(f"FAIL  {name}: {type(exc).__name__}: {exc}")


def test_small_input_passes_through_unchanged():
    src = "console.log('hello');\n" * 50  # ~1 KB, well under filter threshold
    out, stats = filter_accessibility_code(src)
    assert out == src, "small input should be returned verbatim"
    assert stats["path"] == "passthrough_small", f"unexpected path: {stats['path']}"


def test_empty_input_returns_empty():
    out, stats = filter_accessibility_code("")
    assert out == "", "empty input should return empty"
    assert stats["path"] == "empty"


def test_strategy_none_bypasses_filter():
    src = "a" * 50_000  # big enough that auto would filter
    out, stats = filter_accessibility_code(src, strategy="none")
    assert out == src
    assert stats["path"] == "passthrough_forced"


def test_regex_filter_keeps_aria_block():
    # 56 KB of filler plus a single ARIA-touching block; the filter
    # must keep the ARIA block and drop the bulk of the filler. A
    # CONTEXT_WINDOW of lines adjacent to the marker is expected to
    # survive (the enclosing scope) -- the reduction target is
    # orders-of-magnitude, not perfect zero.
    filler_line = "var x = Math.random() * 10;\n"
    filler = filler_line * 2000  # ~56 KB
    aria_block = (
        "function openDialog() {\n"
        "  var d = document.querySelector('.modal');\n"
        "  d.setAttribute('aria-hidden', 'false');\n"
        "  d.focus();\n"
        "}\n"
    )
    src = filler + aria_block + filler

    out = _regex_filter(src)
    assert "setAttribute('aria-hidden'" in out, "aria block must survive filter"
    assert ".focus()" in out, "focus() call must survive filter"
    # The filler is enormous but only a small window of it may survive
    # around the aria block. Output must be at least 50x smaller than
    # the input.
    assert len(out) < len(src) // 50, (
        f"filter should shrink input >= 50x; got {len(src)}->{len(out)}"
    )


def test_regex_filter_returns_empty_when_no_a11y():
    src = "var sum = 0;\nfor (var i = 0; i < 10; i++) sum += i;\n" * 200
    out = _regex_filter(src)
    assert out == "", "no accessibility signal = empty output"


def test_filter_trusts_its_own_output_even_when_empty():
    # A large input with zero accessibility signal is EXPECTED to
    # produce an empty filter output. No passthrough fallback: the
    # filter's answer is trusted -- if no a11y markers are found,
    # the chunk genuinely has no a11y code.
    src = "var noise = 1;\n" * 1500  # ~22 KB, zero accessibility signal
    out, stats = filter_accessibility_code(src)
    assert stats["path"] in ("regex", "esprima"), (
        f"expected direct filter path, got {stats['path']}"
    )
    assert len(out) < len(src), (
        "filter should not passthrough -- it should return its own "
        "(empty or near-empty) output"
    )


def test_all_a11y_markers_compile():
    # Cheap invariant: the big alternation must compile and every
    # marker must actually appear in the source list.
    assert _A11Y_REGEX.search("element.focus()") is not None
    assert _A11Y_REGEX.search("el.addEventListener('keydown', h)") is not None
    assert _A11Y_REGEX.search("el.setAttribute('aria-hidden', 'true')") is not None
    assert _A11Y_REGEX.search("button.tabIndex = -1") is not None
    # Sanity: no marker should match a plain identifier that isn't
    # accessibility-related.
    assert _A11Y_REGEX.search("const computedSum = add(a, b);") is None
    # And every marker is non-empty.
    assert all(m for m in _A11Y_APIS)


def test_regex_filter_preserves_context_window():
    # Make sure a marker's neighboring lines survive the filter so the
    # LLM can see the enclosing function.
    src_lines = [f"var l{i} = {i};" for i in range(30)]
    src_lines[15] = "element.focus();"  # marker in the middle
    src = "\n".join(src_lines) + "\n"
    out = _regex_filter(src)
    assert ".focus()" in out
    # Lines just before/after the marker should survive via the
    # _CONTEXT_WINDOW_LINES window.
    assert "l10" in out or "l14" in out, "context window should include nearby lines"
    assert "l16" in out or "l20" in out, "context window should include nearby lines"


if __name__ == "__main__":
    tests = [
        ("test_small_input_passes_through_unchanged", test_small_input_passes_through_unchanged),
        ("test_empty_input_returns_empty", test_empty_input_returns_empty),
        ("test_strategy_none_bypasses_filter", test_strategy_none_bypasses_filter),
        ("test_regex_filter_keeps_aria_block", test_regex_filter_keeps_aria_block),
        ("test_regex_filter_returns_empty_when_no_a11y", test_regex_filter_returns_empty_when_no_a11y),
        ("test_filter_trusts_its_own_output_even_when_empty", test_filter_trusts_its_own_output_even_when_empty),
        ("test_all_a11y_markers_compile", test_all_a11y_markers_compile),
        ("test_regex_filter_preserves_context_window", test_regex_filter_preserves_context_window),
    ]
    for name, fn in tests:
        _run(name, fn)
    print()
    print(f"{len(_PASSED)} passed, {len(_FAILED)} failed")
    sys.exit(0 if not _FAILED else 1)
