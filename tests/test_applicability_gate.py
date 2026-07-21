"""Regression tests for the applicability gate in BaseCheck.run().

A False is_applicable() result hard-gates an SC to Not Applicable only
when applicability is a mechanical element-existence check. For SCs whose
applicability depends on page meaning (ai_judged_applicability = True), a
False keyword/regex scan is advisory — the SC still runs and the AI judge
decides, so a brittle keyword miss cannot silently bury a real violation
(verified-broken pattern: SC 2.5.4 / 3.3.8 / 3.3.9 keyword-gated to N/A on
a municipal-government-site run 20260515_230613_ff643865).

Run with:
    python tests/test_applicability_gate.py
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from models import CaptureData, ConformanceLevel, TestResult  # noqa: E402
from checks.base import BaseCheck  # noqa: E402


_SENTINEL = "executed-pipeline"


class _MechanicalCheck(BaseCheck):
    """Mechanical applicability: a False is_applicable() must hard-gate to N/A."""
    criterion_id = "9.9.1"
    criterion_name = "Mechanical Test SC"
    level = "A"
    wcag_versions = ["2.2"]
    normative_text = ""
    ai_judged_applicability = False

    def is_applicable(self, capture_data):
        return False

    async def run_programmatic(self, capture_data):
        return (ConformanceLevel.SUPPORTS, 1.0, [])

    async def execute(self, capture_data, ai_client=None):
        # If this runs, the gate failed to short-circuit.
        r = TestResult(criterion_id=self.criterion_id,
                       criterion_name=self.criterion_name,
                       level=self.level, wcag_versions=self.wcag_versions)
        r.summary = _SENTINEL
        return r


class _AIJudgedCheck(BaseCheck):
    """AI-judged applicability: a False is_applicable() is advisory — the SC
    must still run the pipeline so the AI judge decides."""
    criterion_id = "9.9.2"
    criterion_name = "AI-Judged Test SC"
    level = "A"
    wcag_versions = ["2.2"]
    normative_text = ""
    ai_judged_applicability = True

    def is_applicable(self, capture_data):
        return False  # keyword scan said "no" — but this is advisory

    async def run_programmatic(self, capture_data):
        return (ConformanceLevel.SUPPORTS, 1.0, [])

    async def execute(self, capture_data, ai_client=None):
        r = TestResult(criterion_id=self.criterion_id,
                       criterion_name=self.criterion_name,
                       level=self.level, wcag_versions=self.wcag_versions)
        r.summary = _SENTINEL
        r.conformance_level = ConformanceLevel.SUPPORTS
        return r


def _cd() -> CaptureData:
    return CaptureData(url="https://x.test/", html="<html></html>", title="t")


def test_mechanical_false_is_applicable_hard_gates_to_na():
    """Default (ai_judged_applicability=False): False is_applicable() →
    auto Not Applicable, pipeline NOT run."""
    result = asyncio.run(_MechanicalCheck().run(_cd(), ai_client=None))
    assert result.conformance_level == ConformanceLevel.NOT_APPLICABLE
    assert result.summary != _SENTINEL, "execute() must NOT run for a mechanical N/A"


def test_ai_judged_false_is_applicable_runs_pipeline():
    """ai_judged_applicability=True: False is_applicable() is advisory —
    the pipeline runs and the AI judge decides applicability."""
    result = asyncio.run(_AIJudgedCheck().run(_cd(), ai_client=None))
    assert result.summary == _SENTINEL, (
        "execute() MUST run for an AI-judged SC even when the keyword "
        "applicability scan returned False"
    )
    assert result.conformance_level == ConformanceLevel.SUPPORTS


def test_base_default_is_mechanical():
    assert BaseCheck.ai_judged_applicability is False


def test_the_thirteen_semantic_scs_are_flagged():
    """Exactly the 13 SCs with keyword/regex is_applicable carry the flag."""
    import inspect
    import checks.checks_2_1, checks.checks_2_2, checks.checks_2_2_aaa
    import checks.checks_2_3_aaa, checks.checks_2_5, checks.checks_3_3
    import checks.checks_3_3_22, checks.checks_3_3_aaa, checks.checks_cav

    expected = {
        "2.1.4", "2.2.1", "2.2.2", "2.2.3", "2.2.4", "2.2.5", "2.2.6",
        "2.3.3", "2.5.1", "3.3.4", "3.3.8", "3.3.9", "CAV",
    }
    modules = [
        checks.checks_2_1, checks.checks_2_2, checks.checks_2_2_aaa,
        checks.checks_2_3_aaa, checks.checks_2_5, checks.checks_3_3,
        checks.checks_3_3_22, checks.checks_3_3_aaa, checks.checks_cav,
    ]
    found = set()
    for mod in modules:
        for _, obj in inspect.getmembers(mod, inspect.isclass):
            cid = getattr(obj, "criterion_id", None)
            if cid and getattr(obj, "ai_judged_applicability", False):
                found.add(cid)
    assert found == expected, f"missing={expected - found} extra={found - expected}"


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
