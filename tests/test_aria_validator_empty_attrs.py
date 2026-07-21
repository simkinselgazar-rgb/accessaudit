"""Regression tests for ``functions.aria_validator._build_selector``
edge cases that crashed the ARIA validation pass on real sites.

Verified crash on a municipal-government-site run 20260512_103712: a page with
``class=""`` (empty class attribute) raised ``IndexError: list index
out of range`` from ``attrs["class"].split()[0]`` because
``"".split()`` is ``[]``. The exception was caught at the
``_capture_aria_validation`` boundary but silently killed the entire
ARIA validation pass for the page, leaving ``capture_data.aria_issues``
empty and depriving the judge of structural ARIA evidence.

Run with:

    python tests/test_aria_validator_empty_attrs.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from functions.aria_validator import _build_selector  # noqa: E402


def test_empty_class_attribute_does_not_crash():
    """The exact a municipal-government-site crash case: <div class=""> has a present
    class attribute, but the value is empty. ``"".split()`` returns
    ``[]``; the old code's ``[0]`` raised IndexError.
    """
    result = _build_selector("div", {"class": ""})
    assert result == "div", (
        f"empty class attribute should fall back to bare tag selector; "
        f"got {result!r}"
    )


def test_whitespace_only_class_does_not_crash():
    """Variant: <div class="   "> has whitespace-only class. Same
    IndexError trigger because ``"   ".split()`` is ``[]``.
    """
    result = _build_selector("div", {"class": "   "})
    assert result == "div"


def test_empty_id_attribute_skipped():
    """<div id="" class="foo"> shouldn't produce 'div#' (invalid CSS).
    Fall through to class selection.
    """
    result = _build_selector("div", {"id": "", "class": "foo"})
    assert result == "div.foo"


def test_whitespace_only_id_skipped():
    result = _build_selector("div", {"id": "   ", "class": "foo"})
    assert result == "div.foo"


def test_empty_id_and_empty_class_returns_bare_tag():
    result = _build_selector("button", {"id": "", "class": ""})
    assert result == "button"


def test_normal_id_attribute_works():
    result = _build_selector("div", {"id": "main", "class": "wrapper"})
    assert result == "div#main"  # id wins over class


def test_normal_class_attribute_works():
    result = _build_selector("div", {"class": "wrapper container"})
    assert result == "div.wrapper"


def test_tag_only_when_no_attrs():
    result = _build_selector("body", {})
    assert result == "body"


def test_missing_attrs_dict_keys():
    """attrs dict missing the keys entirely (vs present-but-empty) --
    the .get() fallback path."""
    result = _build_selector("section", {"role": "main"})
    assert result == "section"  # neither id nor class -> bare tag


# ── End-to-end: validate_id_references doesn't crash on bad HTML ──────


def test_validate_id_references_survives_empty_class_in_html():
    """Run the upstream caller against HTML that contains the crash
    pattern. Pre-fix this raised IndexError; post-fix it returns
    cleanly (possibly with valid findings, possibly empty list).
    """
    from functions.aria_validator import validate_id_references
    html = (
        "<html><body>"
        "<div class=\"\" aria-labelledby=\"nonexistent\">x</div>"
        "<div class=\"\">y</div>"
        "</body></html>"
    )
    # Just calling without exception is the win.
    results = validate_id_references(html)
    assert isinstance(results, list), (
        "validate_id_references must return a list even when HTML has "
        "empty class attributes -- pre-fix this raised IndexError and "
        "silently killed the whole ARIA pass."
    )


# ── Runner ─────────────────────────────────────────────────────────────


if __name__ == "__main__":
    failures = 0
    tests = [
        (n, fn) for n, fn in globals().items()
        if n.startswith("test_") and callable(fn)
    ]
    for name, fn in tests:
        try:
            fn()
            print(f"  ok  {name}")
        except AssertionError as exc:
            failures += 1
            print(f"  FAIL  {name}: {exc}")
        except Exception as exc:
            failures += 1
            print(f"  ERROR {name}: {type(exc).__name__}: {exc}")
    print(f"\n{len(tests) - failures}/{len(tests)} tests passed")
    sys.exit(1 if failures else 0)
