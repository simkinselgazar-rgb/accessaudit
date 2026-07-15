"""Helpers for interpreting the widget-keyboard capture probe.

``capture/_capture_widget_keyboard`` drives each ARIA widget with its
expected keys and records per-key responses. The probe can fail to run
for a widget (stale element handle, mid-probe navigation, timeout). When
it does, the entry still carries ``any_key_responded: false`` — but that
``false`` means "the probe never executed", not "the widget ignored the
keys". Treating a crashed probe as a measured failure fabricates a
keyboard-inaccessible verdict.

Verified bug (loudoun.gov run 20260518_190643_88c447ff): all 8 native
widget-keyboard probes errored — every ``key_results`` entry was
``{"error": true}`` with an empty ``keys_tested`` — yet SC 2.1.1 / 2.1.2
/ 2.1.3 reported the widgets keyboard-inaccessible. The behavior-verified
``keyboard_roundtrip_results`` showed the same widgets DO respond.
"""
from __future__ import annotations

from typing import Any


def widget_probe_errored(entry: Any) -> bool:
    """True when a widget_keyboard entry recorded no real key test.

    A crashed probe is INCONCLUSIVE — callers must neither emit a
    keyboard-failure finding from it nor describe it to the judge as a
    measured failure. Signals:
      - an explicit top-level ``error`` value, or
      - a ``key_results`` list whose every item is flagged ``error``.
    """
    if not isinstance(entry, dict):
        return False
    if entry.get("error"):
        return True
    key_results = entry.get("key_results") or []
    if key_results and all(
        isinstance(k, dict) and k.get("error") for k in key_results
    ):
        return True
    return False
