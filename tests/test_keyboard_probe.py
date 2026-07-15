"""Unit tests for the widget keyboard-behavior probe classification logic.

The probe itself requires a live Playwright page (see
``capture/interactive_capture.py::_probe_widget_keyboard_behavior``).
These tests cover the PYTHON classification rules that consume the
probe's output dict:

  * When does an element count as ``custom_arrow_navigable`` (and move
    out of ``focusable_but_skipped``)?
  * When does the probe flag a ``custom_widget_trap`` for SC 2.1.2?

Pure-function tests on well-formed probe dicts. The live probe is
exercised end-to-end by the capture pipeline on every real run.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def _fail(name: str, reason: str) -> None:
    print(f"FAIL  {name}: {reason}")
    sys.exit(1)


def _pass(name: str) -> None:
    print(f"PASS  {name}")


def _reclassify(focusable_but_skipped: list[dict]) -> tuple[list[dict], list[dict], list[dict]]:
    """Mirror the Python logic in _tab_coverage_comparison so we can test
    the reclassification rules without spinning up a browser.

    Returns (kept_in_violations, promoted_to_custom_navigable, trap_findings).
    """
    custom: list[dict] = []
    traps: list[dict] = []
    for cand in focusable_but_skipped:
        probe = cand.get("keyboard_probe") or {}
        if probe.get("arrow_navigable") or probe.get("items_reached", 0) > 1:
            custom.append({
                "selector": cand.get("selector", ""),
                "items_reached": probe.get("items_reached", 0),
                "bidirectional_ok": probe.get("bidirectional_ok", False),
            })
        if probe.get("is_trap"):
            traps.append({
                "type": "custom_widget_trap",
                "selector": cand.get("selector", ""),
            })
    reclass = {c["selector"] for c in custom}
    kept = [c for c in focusable_but_skipped if c.get("selector") not in reclass]
    return kept, custom, traps


def test_arrow_navigable_element_reclassified() -> None:
    # A flagged element whose probe showed arrow-key movement should
    # drop out of focusable_but_skipped into custom_arrow_navigable.
    candidates = [{
        "selector": "#slide-2",
        "keyboard_probe": {
            "arrow_navigable": True,
            "items_reached": 6,
            "bidirectional_ok": True,
            "is_trap": False,
        },
    }]
    kept, custom, traps = _reclassify(candidates)
    if kept:
        _fail("test_arrow_navigable_element_reclassified",
              f"expected reclassified, still in kept: {kept!r}")
    if len(custom) != 1 or custom[0]["selector"] != "#slide-2":
        _fail("test_arrow_navigable_element_reclassified",
              f"custom bucket wrong: {custom!r}")
    if traps:
        _fail("test_arrow_navigable_element_reclassified",
              f"unexpected trap: {traps!r}")
    _pass("test_arrow_navigable_element_reclassified")


def test_items_reached_alone_triggers_reclass() -> None:
    # If the probe reached multiple items during its coverage walk
    # even without flagging arrow_navigable (edge case: first press
    # moved but second press didn't move), reclassify anyway.
    candidates = [{
        "selector": "#custom-tab",
        "keyboard_probe": {
            "arrow_navigable": False,
            "items_reached": 4,
            "bidirectional_ok": False,
            "is_trap": False,
        },
    }]
    kept, custom, _ = _reclassify(candidates)
    if kept:
        _fail("test_items_reached_alone_triggers_reclass",
              f"expected reclassified: {kept!r}")
    if len(custom) != 1:
        _fail("test_items_reached_alone_triggers_reclass",
              f"custom bucket wrong: {custom!r}")
    _pass("test_items_reached_alone_triggers_reclass")


def test_non_navigable_element_stays_in_violations() -> None:
    # Probe confirmed no arrow navigation and no items reached -- this
    # is a real SC 2.1.1 violation, must stay in kept.
    candidates = [{
        "selector": "#orphan-button",
        "keyboard_probe": {
            "arrow_navigable": False,
            "items_reached": 1,  # only itself
            "bidirectional_ok": True,
            "is_trap": False,
        },
    }]
    kept, custom, _ = _reclassify(candidates)
    if len(kept) != 1:
        _fail("test_non_navigable_element_stays_in_violations",
              f"expected stayed in kept: {kept!r}")
    if custom:
        _fail("test_non_navigable_element_stays_in_violations",
              f"unexpectedly promoted: {custom!r}")
    _pass("test_non_navigable_element_stays_in_violations")


def test_trap_emitted_for_2_1_2() -> None:
    # A widget where none of Escape / Tab / Shift+Tab exits must emit
    # a SC 2.1.2 trap finding even if the widget IS arrow-navigable.
    candidates = [{
        "selector": "#modal-custom",
        "keyboard_probe": {
            "arrow_navigable": True,
            "items_reached": 5,
            "bidirectional_ok": True,
            "escape_exits": False,
            "tab_exits": False,
            "shift_tab_exits": False,
            "is_trap": True,
        },
    }]
    kept, custom, traps = _reclassify(candidates)
    if len(traps) != 1 or traps[0]["selector"] != "#modal-custom":
        _fail("test_trap_emitted_for_2_1_2",
              f"expected trap finding: {traps!r}")
    # Still reclassified out of 2.1.1 violations (arrow works) but trap
    # reported for 2.1.2.
    if kept:
        _fail("test_trap_emitted_for_2_1_2",
              f"arrow-nav element should leave 2.1.1 bucket: {kept!r}")
    _pass("test_trap_emitted_for_2_1_2")


def test_probe_error_keeps_element_in_violations() -> None:
    # If the probe errored out (URL changed, timeout), the default
    # zero values mean arrow_navigable=False and items_reached=0 --
    # the element stays in focusable_but_skipped, and the auditor sees
    # ``error`` on the probe record for manual follow-up.
    candidates = [{
        "selector": "#navigated-away",
        "keyboard_probe": {
            "arrow_navigable": False,
            "items_reached": 0,
            "bidirectional_ok": True,
            "is_trap": False,
            "error": "url changed",
        },
    }]
    kept, custom, _ = _reclassify(candidates)
    if not kept:
        _fail("test_probe_error_keeps_element_in_violations",
              "error should have left the element in kept")
    if custom:
        _fail("test_probe_error_keeps_element_in_violations",
              f"unexpectedly promoted: {custom!r}")
    _pass("test_probe_error_keeps_element_in_violations")


if __name__ == "__main__":
    test_arrow_navigable_element_reclassified()
    test_items_reached_alone_triggers_reclass()
    test_non_navigable_element_stays_in_violations()
    test_trap_emitted_for_2_1_2()
    test_probe_error_keeps_element_in_violations()
    print("\n5 passed, 0 failed")
