"""Regression tests for cross-criterion verdict reconciliation.

Pins the behaviour verified-broken on fairfaxva.gov run
20260515_230613_ff643865: SC 2.1.3 "Keyboard (No Exception)" returned
Supports while SC 2.1.1 "Keyboard" returned Does Not Support -- an
impossible pair, since 2.1.3 is a strict superset of 2.1.1.

Run with:
    python tests/test_sc_consistency.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from models import ConformanceLevel, TestResult, Finding, Severity  # noqa: E402
from functions.sc_consistency import (  # noqa: E402
    reconcile_cross_sc_verdicts,
    SC_CANNOT_EXCEED,
)


def _r(criterion_id: str, level: ConformanceLevel,
       findings: list | None = None) -> TestResult:
    t = TestResult(
        criterion_id=criterion_id,
        criterion_name=criterion_id,
        level="A",
        wcag_versions=["2.2"],
        conformance_level=level,
    )
    t.findings = findings or []
    return t


def _f(issue: str) -> Finding:
    return Finding(id="orig", element="e", issue=issue, impact="i",
                   recommendation="r", severity=Severity.HIGH,
                   source="programmatic")


def test_stricter_sc_downgraded_when_it_exceeds_base():
    """The exact 2.1.3 / 2.1.1 case: 2.1.3 Supports, 2.1.1 Does Not Support
    -> 2.1.3 must be downgraded to Does Not Support."""
    results = [
        _r("2.1.1", ConformanceLevel.DOES_NOT_SUPPORT),
        _r("2.1.3", ConformanceLevel.SUPPORTS),
    ]
    downgrades = reconcile_cross_sc_verdicts(results)
    assert downgrades == 1
    by = {r.criterion_id: r for r in results}
    assert by["2.1.3"].conformance_level == ConformanceLevel.DOES_NOT_SUPPORT
    assert "CROSS-SC RECONCILIATION" in by["2.1.3"].confidence_reasoning
    # The base criterion is never touched.
    assert by["2.1.1"].conformance_level == ConformanceLevel.DOES_NOT_SUPPORT


def test_stricter_downgraded_to_partial():
    """Base = Partially Supports, stricter = Supports -> stricter becomes
    Partially Supports (matches the base, not worse than it)."""
    results = [
        _r("1.4.3", ConformanceLevel.PARTIALLY_SUPPORTS),
        _r("1.4.6", ConformanceLevel.SUPPORTS),
    ]
    downgrades = reconcile_cross_sc_verdicts(results)
    assert downgrades == 1
    by = {r.criterion_id: r for r in results}
    assert by["1.4.6"].conformance_level == ConformanceLevel.PARTIALLY_SUPPORTS


def test_consistent_pair_untouched():
    """Stricter already no better than base -> no change."""
    results = [
        _r("2.1.1", ConformanceLevel.SUPPORTS),
        _r("2.1.3", ConformanceLevel.SUPPORTS),
    ]
    downgrades = reconcile_cross_sc_verdicts(results)
    assert downgrades == 0
    by = {r.criterion_id: r for r in results}
    assert by["2.1.3"].conformance_level == ConformanceLevel.SUPPORTS


def test_stricter_allowed_to_be_worse_than_base():
    """An enhanced criterion CAN fail while its easier sibling passes --
    that is the entire point of enhanced criteria. Base = Supports,
    stricter = Does Not Support -> NO change."""
    results = [
        _r("1.4.3", ConformanceLevel.SUPPORTS),
        _r("1.4.6", ConformanceLevel.DOES_NOT_SUPPORT),
    ]
    downgrades = reconcile_cross_sc_verdicts(results)
    assert downgrades == 0
    by = {r.criterion_id: r for r in results}
    assert by["1.4.6"].conformance_level == ConformanceLevel.DOES_NOT_SUPPORT


def test_not_applicable_pair_skipped():
    """When either side is Not Applicable the pair is not comparable;
    no reconciliation, no crash."""
    results = [
        _r("2.5.8", ConformanceLevel.NOT_APPLICABLE),
        _r("2.5.5", ConformanceLevel.SUPPORTS),
    ]
    downgrades = reconcile_cross_sc_verdicts(results)
    assert downgrades == 0
    by = {r.criterion_id: r for r in results}
    assert by["2.5.5"].conformance_level == ConformanceLevel.SUPPORTS


def test_not_evaluated_pair_skipped():
    results = [
        _r("2.4.11", ConformanceLevel.NOT_EVALUATED),
        _r("2.4.12", ConformanceLevel.SUPPORTS),
    ]
    downgrades = reconcile_cross_sc_verdicts(results)
    assert downgrades == 0


def test_missing_sc_skipped():
    """If one side of a pair is absent from the results, skip cleanly."""
    results = [_r("2.1.3", ConformanceLevel.SUPPORTS)]  # no 2.1.1
    downgrades = reconcile_cross_sc_verdicts(results)
    assert downgrades == 0
    assert results[0].conformance_level == ConformanceLevel.SUPPORTS


def test_all_five_pairs_reconcile():
    """Every declared superset pair downgrades when violated."""
    results = []
    for stricter, easier in SC_CANNOT_EXCEED.items():
        results.append(_r(easier, ConformanceLevel.DOES_NOT_SUPPORT))
        results.append(_r(stricter, ConformanceLevel.SUPPORTS))
    downgrades = reconcile_cross_sc_verdicts(results)
    assert downgrades == len(SC_CANNOT_EXCEED), (
        f"expected all {len(SC_CANNOT_EXCEED)} pairs to downgrade, "
        f"got {downgrades}"
    )
    by = {r.criterion_id: r for r in results}
    for stricter in SC_CANNOT_EXCEED:
        assert by[stricter].conformance_level == ConformanceLevel.DOES_NOT_SUPPORT


def test_empty_results_no_crash():
    assert reconcile_cross_sc_verdicts([]) == 0


# ── Evidence inheritance: a downgraded verdict must carry findings ──────


def test_downgraded_sc_inherits_findings_when_it_had_none():
    """The exact bug from run 20260517_071114_168a32ee: 2.1.3 was
    Supports/0-findings, downgraded to Does Not Support to match 2.1.1 —
    leaving a failing verdict with no evidence. The downgraded SC must
    inherit the base criterion's findings so the verdict is backed."""
    base = _r("2.1.1", ConformanceLevel.DOES_NOT_SUPPORT,
              [_f(f"keyboard failure {i}") for i in range(7)])
    stricter = _r("2.1.3", ConformanceLevel.SUPPORTS, [])
    downgrades = reconcile_cross_sc_verdicts([base, stricter])
    assert downgrades == 1
    by = {r.criterion_id: r for r in [base, stricter]}
    assert by["2.1.3"].conformance_level == ConformanceLevel.DOES_NOT_SUPPORT
    assert len(by["2.1.3"].findings) == 7, "must inherit so verdict has evidence"
    assert all("Inherited by SC 2.1.3" in f.issue for f in by["2.1.3"].findings)
    assert all(f.id != "orig" for f in by["2.1.3"].findings), "fresh ids"
    # The base criterion is never mutated.
    assert len(by["2.1.1"].findings) == 7
    assert by["2.1.1"].findings[0].id == "orig"
    assert "Inherited by" not in by["2.1.1"].findings[0].issue


def test_downgraded_sc_keeps_its_own_findings_if_it_has_them():
    """If the stricter SC already surfaced its own findings, the
    reconciler downgrades the verdict but does NOT overwrite them."""
    base = _r("1.4.3", ConformanceLevel.DOES_NOT_SUPPORT, [_f("contrast fail")])
    stricter = _r("1.4.6", ConformanceLevel.SUPPORTS, [_f("its own AAA finding")])
    reconcile_cross_sc_verdicts([base, stricter])
    by = {r.criterion_id: r for r in [base, stricter]}
    assert by["1.4.6"].conformance_level == ConformanceLevel.DOES_NOT_SUPPORT
    assert len(by["1.4.6"].findings) == 1
    assert by["1.4.6"].findings[0].issue == "its own AAA finding"


def test_no_empty_failing_verdict_after_reconciliation():
    """Invariant: after reconciliation no SC has a worse-than-Supports
    verdict with zero findings (the audit_sc bug condition)."""
    base = _r("2.5.8", ConformanceLevel.PARTIALLY_SUPPORTS, [_f("target too small")])
    stricter = _r("2.5.5", ConformanceLevel.SUPPORTS, [])
    reconcile_cross_sc_verdicts([base, stricter])
    for r in (base, stricter):
        failing = r.conformance_level in (
            ConformanceLevel.PARTIALLY_SUPPORTS,
            ConformanceLevel.DOES_NOT_SUPPORT,
        )
        if failing:
            assert r.findings, (
                f"{r.criterion_id}: {r.conformance_level.value} with 0 findings"
            )


# ── Runner ──────────────────────────────────────────────────────────────


if __name__ == "__main__":
    failures = 0
    tests = [
        (n, fn) for n, fn in sorted(globals().items())
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
