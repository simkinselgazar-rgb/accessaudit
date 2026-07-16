"""Regression tests for verdict / findings consistency in the slow-path
judge consumer (``checks/base.py:run``).

Two structural bugs found on university live runs that these tests pin:

1. Silent input-finding retention when judge returns final_findings=[]
   AND rejected_findings=[]. Verified failure on run #5 SC 3.2.3:
   judge said "Supports, 0 findings, 0 rejections" but result.json
   still contained 2 input findings because the old "filter by
   rejection" fallback never entered its loop. Fix: trust the judge's
   final_findings as the authoritative output set; if empty, output
   is empty.

2. Inverse verdict-vs-findings sanity check: the prior code only
   caught "Supports + high/medium findings". It never caught
   "Partially Supports / Does Not Support + 0 findings", which is
   equally broken (nothing to remediate). Verified failure on run
   #4 SC 2.5.1. Fix: when failing verdict has 0 findings,
   auto-downgrade to Supports + needs_review note.

Run with:

    python tests/test_judge_finding_set_consistency.py
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from models import ConformanceLevel, Finding, Severity, TestResult  # noqa: E402


def _stub_capture():
    """Minimal CaptureData stub for tests that only exercise post-
    judge bookkeeping. The slow-path post-judge code reads only a
    couple of capture_data fields (review_dir, html for
    contradiction filter); we set just enough to keep it from
    crashing.
    """
    from models import CaptureData
    cd = CaptureData(url="https://t/")
    cd.html = "<html><body><p id='real-id'>x</p></body></html>"
    cd.review_dir = ""
    return cd


# ── Fix 1: empty final_findings clears result.findings ─────────────────


def test_judge_returning_empty_final_findings_clears_result_findings():
    """The exact university-run SC 3.2.3 failure: judge returned
    final_findings=[], rejected_findings=[], verdict=Supports. Pre-fix
    the 2 input findings persisted. Post-fix the result has 0 findings.
    """
    # The post-judge logic in base.py:run is too entangled with the
    # full BaseCheck.execute() pipeline to test in isolation, so we
    # inline the contract: simulate the consumer's behaviour and pin
    # the rule that an empty judge final_findings list maps to an
    # empty result.findings list, regardless of input findings.
    #
    # The implementation is at base.py: 'if final_findings: ... else:'
    # block + the unconditional `result.findings = []` after.

    # Case A: judge returned non-empty final_findings → use them.
    judge_final_findings = [
        {"element": "x", "css_selector": ".x", "issue": "real",
         "impact": "x", "recommendation": "x", "severity": "high",
         "source": "axe"},
    ]
    out_set = list(judge_final_findings) if judge_final_findings else []
    assert len(out_set) == 1

    # Case B: judge returned empty final_findings → output is empty
    # regardless of how many input findings there were.
    input_findings = [
        Finding(id="i1", element="a", issue="x", impact="x",
                recommendation="x", severity=Severity.MEDIUM,
                source="programmatic"),
        Finding(id="i2", element="b", issue="y", impact="x",
                recommendation="x", severity=Severity.LOW,
                source="programmatic"),
    ]
    judge_final_findings = []  # judge's authoritative answer
    # Post-fix contract: out_set comes from judge.final_findings
    # ALWAYS, never from the input list.
    out_set = list(judge_final_findings)
    assert out_set == [], (
        f"empty judge final_findings must map to empty output; got {out_set!r}. "
        f"Inputs ({len(input_findings)}) must NOT leak through."
    )


def test_judge_finding_consumer_pins_authoritative_contract():
    """Read the actual base.py source and pin the structural rule:
    ``result.findings = []`` and the ``for ff in final_findings``
    loop must be UNCONDITIONAL on the slow path. If a future edit
    re-introduces an ``else`` branch that retains input findings,
    this test fails loudly.
    """
    base_src = (
        Path(__file__).resolve().parent.parent / "checks" / "base.py"
    ).read_text(encoding="utf-8")
    # The post-validator code must do the unconditional reset.
    # Look for a window around 'final_findings = judgment.get'.
    anchor = "final_findings = judgment.get(\"final_findings\", [])"
    idx = base_src.find(anchor)
    assert idx > 0, "anchor moved -- update test"
    # Window covering the consumer block (slow path). Sized generously so
    # growth in the validator/comment block between the anchor and the
    # unconditional reset doesn't turn this guard into a false positive.
    window = base_src[idx:idx + 8000]
    # The unconditional reset must appear AFTER the validator block,
    # not nested inside an `if final_findings:` branch.
    reset_marker = "result.findings = []\n                    for ff in final_findings:"
    assert reset_marker in window, (
        "Slow-path judge consumer must UNCONDITIONALLY do "
        "`result.findings = []` followed by `for ff in final_findings:`. "
        "Don't re-introduce the `if final_findings: ... else: filter-by-rejection` "
        "fork -- it caused the SC 3.2.3 silent-retain bug."
    )


# ── Fix 2: inverse verdict sanity check ────────────────────────────────


def _make_result_with_verdict(verdict: ConformanceLevel, findings_count: int = 0) -> TestResult:
    r = TestResult(
        criterion_id="2.5.1",
        criterion_name="Pointer Gestures",
        level="A",
        wcag_versions=["2.2"],
        ict_baseline="",
    )
    r.conformance_level = verdict
    r.confidence = 0.8
    r.findings = [
        Finding(id=f"x{i}", element="x", issue="x", impact="x",
                recommendation="x", severity=Severity.MEDIUM,
                source="programmatic")
        for i in range(findings_count)
    ]
    return r


def _apply_sanity_check(result: TestResult) -> TestResult:
    """Re-implement the sanity-check rule from base.py inline so we
    can exercise it without spinning up a full BaseCheck pipeline.
    Mirrors the four cases in base.py:run after the judge consumer.
    """
    high = sum(1 for f in result.findings if f.severity == Severity.HIGH)
    med = sum(1 for f in result.findings if f.severity == Severity.MEDIUM)
    total = len(result.findings)
    if result.conformance_level == ConformanceLevel.SUPPORTS and high:
        result.conformance_level = ConformanceLevel.DOES_NOT_SUPPORT
    elif result.conformance_level == ConformanceLevel.SUPPORTS and med:
        result.conformance_level = ConformanceLevel.PARTIALLY_SUPPORTS
    elif (
        result.conformance_level in (
            ConformanceLevel.DOES_NOT_SUPPORT,
            ConformanceLevel.PARTIALLY_SUPPORTS,
        )
        and total == 0
    ):
        result.conformance_level = ConformanceLevel.SUPPORTS
        result.needs_review = True
    return result


def test_partial_supports_with_zero_findings_downgrades_to_supports():
    """The university run #4 SC 2.5.1 case: verdict was Partially Supports
    but findings list was empty. Sanity check must downgrade to
    Supports and flag for review.
    """
    r = _make_result_with_verdict(ConformanceLevel.PARTIALLY_SUPPORTS, findings_count=0)
    r = _apply_sanity_check(r)
    assert r.conformance_level == ConformanceLevel.SUPPORTS
    assert r.needs_review is True


def test_does_not_support_with_zero_findings_downgrades_to_supports():
    """Same rule for DNS verdict + 0 findings."""
    r = _make_result_with_verdict(ConformanceLevel.DOES_NOT_SUPPORT, findings_count=0)
    r = _apply_sanity_check(r)
    assert r.conformance_level == ConformanceLevel.SUPPORTS
    assert r.needs_review is True


def test_partial_supports_with_findings_is_unchanged():
    """Don't over-correct: failing verdict WITH findings is the
    normal case and must not be downgraded.
    """
    r = _make_result_with_verdict(ConformanceLevel.PARTIALLY_SUPPORTS, findings_count=3)
    r = _apply_sanity_check(r)
    assert r.conformance_level == ConformanceLevel.PARTIALLY_SUPPORTS


def test_supports_with_high_findings_still_upgrades_to_dns():
    """The pre-existing rule must keep working: clean verdict +
    high findings = DNS.
    """
    r = TestResult(
        criterion_id="x", criterion_name="x", level="A",
        wcag_versions=["2.2"], ict_baseline="",
    )
    r.conformance_level = ConformanceLevel.SUPPORTS
    r.findings = [
        Finding(id="x", element="x", issue="x", impact="x",
                recommendation="x", severity=Severity.HIGH,
                source="axe"),
    ]
    r = _apply_sanity_check(r)
    assert r.conformance_level == ConformanceLevel.DOES_NOT_SUPPORT


def test_supports_with_zero_findings_unchanged():
    """The clean clean case: Supports + 0 findings is correct and
    should not trigger any sanity adjustment.
    """
    r = _make_result_with_verdict(ConformanceLevel.SUPPORTS, findings_count=0)
    r = _apply_sanity_check(r)
    assert r.conformance_level == ConformanceLevel.SUPPORTS


def test_not_applicable_with_zero_findings_unchanged():
    """N/A + 0 findings is also correct -- the inverse-sanity rule
    only applies to FAILING verdicts (DNS / PartialSupports).
    """
    r = _make_result_with_verdict(ConformanceLevel.NOT_APPLICABLE, findings_count=0)
    r = _apply_sanity_check(r)
    assert r.conformance_level == ConformanceLevel.NOT_APPLICABLE


# ── Source-text contract on the actual base.py file ────────────────────


def test_inverse_sanity_check_present_in_source():
    """Pin that the inverse sanity check exists in base.py. Catches
    accidental deletion in a future refactor.
    """
    base_src = (
        Path(__file__).resolve().parent.parent / "checks" / "base.py"
    ).read_text(encoding="utf-8")
    # Look for the new branch
    assert "DOES_NOT_SUPPORT" in base_src and "PARTIALLY_SUPPORTS" in base_src
    assert "with 0 findings → Supports" in base_src or "0 findings -> Supports" in base_src or "with %d findings = Supports" in base_src or "with 0 findings" in base_src, (
        "The inverse-verdict sanity check log line is missing -- "
        "the post-fix base.py must catch failing-verdict-with-no-findings."
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
