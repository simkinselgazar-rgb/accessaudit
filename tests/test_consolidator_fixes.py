"""Regression tests for two reconciliation fixes verified on the
fairfaxva.gov run 20260514_205147_cb3b646c:

  Fix A — Low-confidence vote floor (SC 3.2.6 case):
    Visual AI returned NA @ 1.0, Programmatic @ 0.3 + Code AI @ 0.75 voted
    Supports, the consolidator's majority vote published Supports and
    overrode the only source that called the scope correctly. With the
    floor, sub-floor verdicts no longer count.

  Fix B — Verdict / findings reconciliation in the fast path (SC 4.1.3 case):
    Judge consolidated 3 programmatic findings into 1, validator demoted
    that 1 to judge_inference and dropped it; conformance_level was
    correctly restored to prog_conf=Partially Supports, but result.findings
    was wiped to []. The auditor saw a non-Supports verdict with zero
    evidence. With the reconciliation, the original deterministic
    findings are preserved when the judge collapses to empty.

Run with:
    python tests/test_consolidator_fixes.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from models import ConformanceLevel  # noqa: E402


def test_low_confidence_vote_floor_constant_present():
    """The class constant must exist and be calibrated below 'sure' but
    above 'random guess' -- 0.5 is the inflection between the two."""
    from checks.base import BaseCheck
    assert hasattr(BaseCheck, "_VOTE_CONFIDENCE_FLOOR"), (
        "BaseCheck must expose _VOTE_CONFIDENCE_FLOOR for the consolidator's "
        "low-confidence filter"
    )
    floor = BaseCheck._VOTE_CONFIDENCE_FLOOR
    assert 0.4 <= floor <= 0.6, (
        f"Floor {floor} outside the safe band [0.4, 0.6]. Below 0.4 lets "
        f"noise back in; above 0.6 silences too many legitimate sources."
    )


def test_low_confidence_sources_get_filtered_out():
    """Drive the consolidator with the actual SC 3.2.6 shape:
        Programmatic Supports @ 0.3
        Visual AI Not Applicable @ 1.0
        Code AI Supports @ 0.75
    Pre-fix outcome: Supports (Programmatic + Code AI majority).
    Post-fix outcome: NA wins because Programmatic is filtered out and
    the remaining two-source disagreement falls through to either the
    confidence-gap branch or the conservative tie-break.
    """
    # Build a synthetic check instance to drive _reconcile_verdicts.
    from checks.base import BaseCheck
    from models import TestResult, Severity  # noqa: F401

    class _DummyCheck(BaseCheck):
        criterion_id = "3.2.6"
        criterion_name = "Consistent Help"
        level = "A"
        normative_text = ""
        wcag_versions = ["2.2"]

        def is_applicable(self, capture_data):
            return True

        async def run_programmatic(self, capture_data):
            return (ConformanceLevel.SUPPORTS, 0.3, [])

    check = _DummyCheck()
    result = TestResult(
        criterion_id="3.2.6",
        criterion_name="Consistent Help",
        level="A",
        wcag_versions=["2.2"],
    )
    result.programmatic_conformance = ConformanceLevel.SUPPORTS
    result.programmatic_confidence = 0.3
    result.programmatic_findings_count = 0
    result.ai_conformance = ConformanceLevel.NOT_APPLICABLE
    result.ai_confidence = 1.0
    result.ai_findings_count = 0
    result.code_ai_conformance = ConformanceLevel.SUPPORTS
    result.code_ai_confidence = 0.75
    result.code_ai_findings_count = 0
    result.at_sim_conformance = ConformanceLevel.NOT_EVALUATED
    result.at_sim_confidence = 0.0
    result.findings = []

    check._reconcile_verdicts(result)

    assert result.conformance_level != ConformanceLevel.SUPPORTS, (
        "SC 3.2.6 case: Programmatic 0.3 Supports must NOT outvote Visual "
        "AI 1.0 Not Applicable. Got %s." % result.conformance_level.value
    )
    # With Programmatic filtered out, the remaining two-source disagreement
    # is Visual AI NA @ 1.0 vs Code AI Supports @ 0.75 (gap 0.25 < 0.3),
    # which falls into the "conservative tie" branch -> _worse(NA, Supports) = NA.
    assert result.conformance_level == ConformanceLevel.NOT_APPLICABLE, (
        f"Expected Not Applicable after filtering low-confidence "
        f"Programmatic; got {result.conformance_level.value}"
    )


def test_high_confidence_sources_unaffected_by_floor():
    """Pure regression: when every source is above the floor, the
    majority vote behaves exactly as before.
    """
    from checks.base import BaseCheck
    from models import TestResult

    class _DummyCheck(BaseCheck):
        criterion_id = "1.4.3"
        criterion_name = "Contrast (Minimum)"
        level = "AA"
        normative_text = ""
        wcag_versions = ["2.2"]

        def is_applicable(self, capture_data):
            return True

        async def run_programmatic(self, capture_data):
            return (ConformanceLevel.SUPPORTS, 0.9, [])

    check = _DummyCheck()
    result = TestResult(
        criterion_id="1.4.3",
        criterion_name="Contrast (Minimum)",
        level="AA",
        wcag_versions=["2.2"],
    )
    result.programmatic_conformance = ConformanceLevel.PARTIALLY_SUPPORTS
    result.programmatic_confidence = 0.9
    result.programmatic_findings_count = 2
    result.ai_conformance = ConformanceLevel.PARTIALLY_SUPPORTS
    result.ai_confidence = 0.85
    result.ai_findings_count = 1
    result.code_ai_conformance = ConformanceLevel.SUPPORTS
    result.code_ai_confidence = 0.8
    result.code_ai_findings_count = 0
    result.at_sim_conformance = ConformanceLevel.NOT_EVALUATED
    result.at_sim_confidence = 0.0
    result.findings = []

    check._reconcile_verdicts(result)
    # Programmatic + Visual AI agree on Partially Supports (2 votes);
    # Code AI dissents with Supports (1 vote). Majority wins.
    assert result.conformance_level == ConformanceLevel.PARTIALLY_SUPPORTS, (
        f"Expected Partially Supports from 2-source majority; got "
        f"{result.conformance_level.value}"
    )


def test_floor_zeroes_out_all_sources_returns_not_evaluated():
    """When every source is below the floor, the consolidator must
    return Not Evaluated rather than crashing or silently picking one.
    """
    from checks.base import BaseCheck
    from models import TestResult

    class _DummyCheck(BaseCheck):
        criterion_id = "2.3.1"
        criterion_name = "Three Flashes or Below Threshold"
        level = "A"
        normative_text = ""
        wcag_versions = ["2.2"]

        def is_applicable(self, capture_data):
            return True

        async def run_programmatic(self, capture_data):
            return (ConformanceLevel.NOT_EVALUATED, 0.0, [])

    check = _DummyCheck()
    result = TestResult(
        criterion_id="2.3.1",
        criterion_name="Three Flashes or Below Threshold",
        level="A",
        wcag_versions=["2.2"],
    )
    result.programmatic_conformance = ConformanceLevel.SUPPORTS
    result.programmatic_confidence = 0.2
    result.programmatic_findings_count = 0
    result.ai_conformance = ConformanceLevel.SUPPORTS
    result.ai_confidence = 0.3
    result.ai_findings_count = 0
    result.code_ai_conformance = ConformanceLevel.NOT_EVALUATED
    result.code_ai_confidence = 0.0
    result.code_ai_findings_count = 0
    result.at_sim_conformance = ConformanceLevel.NOT_EVALUATED
    result.at_sim_confidence = 0.0
    result.findings = []

    check._reconcile_verdicts(result)
    assert result.conformance_level == ConformanceLevel.NOT_EVALUATED, (
        f"Expected Not Evaluated when every source is below the floor; got "
        f"{result.conformance_level.value}"
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
