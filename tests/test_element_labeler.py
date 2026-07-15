"""Unit tests for the element labeler.

Covers the Python-side composer and enrichment helper. The JavaScript
bundle is exercised end-to-end by the Playwright capture path -- a
pure-Python unit test can only confirm the Python side is correct.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from functions.element_labeler import (
    compose_location_label,
    ensure_label_fields,
)


def _fail(name: str, reason: str) -> None:
    print(f"FAIL  {name}: {reason}")
    sys.exit(1)


def _pass(name: str) -> None:
    print(f"PASS  {name}")


def test_composer_prefers_accessible_name_over_visible_text() -> None:
    # A button with aria-label but different visible text should label
    # with the aria-label -- that's what the screen reader announces.
    label = compose_location_label({
        "visible_text": "X",
        "accessible_name": "Close modal dialog",
        "preceding_heading": None,
        "landmark": None,
        "spatial": None,
    })
    if "Close modal dialog" not in label or '"X"' in label:
        _fail(
            "test_composer_prefers_accessible_name_over_visible_text",
            f"expected accessible name, got: {label!r}",
        )
    _pass("test_composer_prefers_accessible_name_over_visible_text")


def test_composer_includes_heading_and_landmark() -> None:
    label = compose_location_label({
        "visible_text": "Read more",
        "accessible_name": "",
        "preceding_heading": {"level": 3, "text": "Students showcase tech", "id": ""},
        "landmark": {"role": "main", "label": "", "tag": "main"},
        "spatial": {"above_fold": False, "bucket": "lower-middle", "page_y": 3400},
    })
    required_fragments = [
        "Read more",
        "Students showcase tech",
        "<h3>",
        "<main>",
        "lower-middle",
    ]
    for frag in required_fragments:
        if frag not in label:
            _fail(
                "test_composer_includes_heading_and_landmark",
                f"expected fragment {frag!r} in: {label!r}",
            )
    _pass("test_composer_includes_heading_and_landmark")


def test_composer_handles_empty_input() -> None:
    # None and {} should both return "" so the caller can fall back
    # cleanly without a None check.
    if compose_location_label(None) != "":
        _fail("test_composer_handles_empty_input", "None should return empty string")
    if compose_location_label({}) != "":
        _fail("test_composer_handles_empty_input", "{} should return empty string")
    _pass("test_composer_handles_empty_input")


def test_composer_above_fold_wins_over_bucket() -> None:
    # If above_fold is true, don't also say the bucket -- above-fold is
    # the more useful cue for a reader.
    label = compose_location_label({
        "visible_text": "Top thing",
        "accessible_name": "",
        "preceding_heading": None,
        "landmark": None,
        "spatial": {"above_fold": True, "bucket": "top", "page_y": 100},
    })
    if "above the fold" not in label:
        _fail(
            "test_composer_above_fold_wins_over_bucket",
            f"expected 'above the fold', got: {label!r}",
        )
    # The "top of the page" phrasing only triggers when above_fold is
    # false. Confirm we didn't dual-emit.
    if "in the top" in label:
        _fail(
            "test_composer_above_fold_wins_over_bucket",
            f"emitted both above-fold and bucket: {label!r}",
        )
    _pass("test_composer_above_fold_wins_over_bucket")


def test_ensure_label_fields_composes_from_location() -> None:
    items = [
        {
            "src": "a.png",
            "location": {
                "visible_text": "",
                "accessible_name": "Campus map",
                "preceding_heading": {"level": 2, "text": "Find us", "id": ""},
                "landmark": None,
                "spatial": None,
            },
        },
        {"src": "b.png"},  # no location -> missing
    ]
    composed = ensure_label_fields(items, warn_prefix="UnitTest", required=False)
    if composed != 1:
        _fail(
            "test_ensure_label_fields_composes_from_location",
            f"expected composed=1, got {composed}",
        )
    if "Campus map" not in items[0].get("location_label", ""):
        _fail(
            "test_ensure_label_fields_composes_from_location",
            f"first item not labelled: {items[0]!r}",
        )
    if items[1].get("location_label") != "":
        _fail(
            "test_ensure_label_fields_composes_from_location",
            f"second item should have empty label, got {items[1]!r}",
        )
    _pass("test_ensure_label_fields_composes_from_location")


def test_ensure_label_fields_preserves_existing_label() -> None:
    # An item that already has a non-empty location_label must not be
    # overwritten -- callers may have composed a custom label already.
    items = [{"src": "x.png", "location_label": "custom", "location": {"accessible_name": "other"}}]
    composed = ensure_label_fields(items, warn_prefix="UnitTest")
    if composed != 0:
        _fail(
            "test_ensure_label_fields_preserves_existing_label",
            f"expected composed=0 (preserved), got {composed}",
        )
    if items[0]["location_label"] != "custom":
        _fail(
            "test_ensure_label_fields_preserves_existing_label",
            f"existing label overwritten: {items[0]!r}",
        )
    _pass("test_ensure_label_fields_preserves_existing_label")


def test_composer_truncation_is_handled_on_composer_side_not_js_side() -> None:
    # The JS side truncates text to VISIBLE_TEXT_MAX with an ellipsis.
    # The Python composer accepts whatever the caller gives it (no
    # secondary truncation) -- confirms we don't double-truncate.
    long = "X" * 500
    label = compose_location_label({"accessible_name": long})
    if long not in label:
        _fail(
            "test_composer_truncation_is_handled_on_composer_side_not_js_side",
            "Python composer unexpectedly truncated input",
        )
    _pass("test_composer_truncation_is_handled_on_composer_side_not_js_side")


if __name__ == "__main__":
    test_composer_prefers_accessible_name_over_visible_text()
    test_composer_includes_heading_and_landmark()
    test_composer_handles_empty_input()
    test_composer_above_fold_wins_over_bucket()
    test_ensure_label_fields_composes_from_location()
    test_ensure_label_fields_preserves_existing_label()
    test_composer_truncation_is_handled_on_composer_side_not_js_side()
    print("\n7 passed, 0 failed")
