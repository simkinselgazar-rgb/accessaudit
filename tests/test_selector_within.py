"""Regression test for the keyboard-roundtrip containment logic.

Bug: _probe_one_keyboard_roundtrip decided "focus stayed inside the
opened container" with raw substring matching
(``open_target in active or active in open_target``). An empty
active_selector (focus on <body>) therefore ALWAYS counted as inside,
and prefix-sharing selectors (#nav vs #nav2) false-matched. The
structural comparison now lives in functions.selectors.selector_within.

Run: python -m pytest tests/test_selector_within.py -q
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from functions.selectors import selector_within


def test_empty_active_selector_counts_as_escaped():
    # Focus on <body> produces active_selector == "" — that is ESCAPED,
    # not inside. The old substring test returned True here.
    assert selector_within("", "#modal") is False
    assert selector_within(None, "#modal") is False
    assert selector_within("   ", "#modal") is False


def test_body_and_html_count_as_escaped():
    assert selector_within("body", "#modal") is False
    assert selector_within("html", "#modal") is False
    assert selector_within("body.modal-open", "#modal") is False
    assert selector_within("body#page", "#modal") is False


def test_empty_container_never_matches():
    assert selector_within("#btn", "") is False
    assert selector_within("#btn", None) is False


def test_exact_match_is_inside():
    assert selector_within("#modal", "#modal") is True
    assert selector_within("div.menu.open", "div.menu.open") is True


def test_prefix_sharing_selectors_do_not_match():
    # The old test did `active in open_target` / `open_target in active`
    # — '#nav' is a substring of '#nav2' and vice-versa direction.
    assert selector_within("#nav2", "#nav") is False
    assert selector_within("#nav", "#nav2") is False
    assert selector_within("div.menu-wrapper", "div.menu") is False
    assert selector_within("div.menu", "div.menu-wrapper") is False


def test_path_extending_container_across_boundary_is_inside():
    # getSelector-style path rooted at the container id.
    assert selector_within("#modal > div > button", "#modal") is True
    # Compact combinator form.
    assert selector_within("#modal>button", "#modal") is True
    # Pseudo-class boundary (nth-of-type path).
    assert selector_within(
        "div:nth-of-type(2) > a", "div:nth-of-type(2)"
    ) is True
    # Extra class on the container element itself.
    assert selector_within("#modal.visible", "#modal") is True
    # Attribute selector boundary.
    assert selector_within("#modal[aria-hidden]", "#modal") is True


def test_unrelated_selectors_do_not_match():
    assert selector_within("#footer > a", "#modal") is False
    assert selector_within("button:nth-of-type(3)", "#modal") is False


if __name__ == "__main__":
    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"PASS {name}")
            except AssertionError as exc:
                failures += 1
                print(f"FAIL {name}: {exc}")
    sys.exit(1 if failures else 0)
