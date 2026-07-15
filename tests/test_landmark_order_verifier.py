"""Regression tests for the landmark-order claim verifier.

Verified failure on A11Y Project run 20260511 SC 1.3.2: visual_ai
claimed *"main content is placed AFTER secondary navigation and
footer elements in the accessibility tree, violating WCAG 1.3.2"*.
The captured a11y tree showed main BEFORE both -- the LLM had the
right data and inverted the direction.

The fix is structural: when a visual_ai finding contains a landmark-
order claim ("X before Y" / "X after Y"), look up both landmarks in
the captured a11y tree and verify. Contradictions are dropped before
the judge sees the finding.

Run with:

    python tests/test_landmark_order_verifier.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from functions.landmark_order_verifier import (  # noqa: E402
    _detect_landmark_order_claim,
    _first_position_of_landmark,
    filter_landmark_order_hallucinations,
    verify_landmark_order_claim,
)
from models import Finding, Severity  # noqa: E402


def _make_a11y_tree(*roles: str) -> dict:
    """Build a minimal Playwright-shape a11y tree with the given
    roles in document order. Each role becomes one node.
    """
    return {"nodes": [{"role": {"value": r}} for r in roles]}


# ── Claim detection ────────────────────────────────────────────────────


def test_detect_after_claim_in_visual_ai_prose():
    """The exact ASU / A11Y Project failure-mode wording."""
    text = (
        "The main content (headings and sections) is placed AFTER "
        "secondary navigation and footer elements in the accessibility "
        "tree, violating WCAG 1.3.2"
    )
    claim = _detect_landmark_order_claim(text)
    assert claim is not None
    a, direction, b = claim
    assert a == "main"
    assert direction == "after"
    # First non-main landmark word after "after" is "navigation" (from
    # 'secondary navigation') -- we pick the first.
    assert b in ("navigation", "contentinfo")


def test_detect_before_claim():
    text = "The footer comes before the main content in the reading order."
    claim = _detect_landmark_order_claim(text)
    assert claim is not None
    a, direction, b = claim
    assert a == "contentinfo"  # footer -> contentinfo
    assert direction == "before"
    assert b == "main"


def test_no_claim_when_text_has_no_landmark_pair():
    text = "Image has missing alt attribute."
    assert _detect_landmark_order_claim(text) is None


def test_no_claim_when_landmarks_same():
    """'main before main' is not a meaningful directional claim."""
    text = "The main element appears before another main element."
    assert _detect_landmark_order_claim(text) is None


def test_synonym_normalization():
    """'footer' and 'nav' must map to the ARIA roles 'contentinfo' and
    'navigation' so the position lookup works against captured a11y tree.
    """
    text = "The nav block appears after the footer."
    claim = _detect_landmark_order_claim(text)
    assert claim is not None
    a, direction, b = claim
    assert a == "navigation"
    assert b == "contentinfo"


# ── Position lookup ────────────────────────────────────────────────────


def test_first_position_finds_role_in_dict_value():
    tree = _make_a11y_tree("banner", "main", "contentinfo")
    assert _first_position_of_landmark("main", tree) == 1
    assert _first_position_of_landmark("contentinfo", tree) == 2


def test_first_position_returns_none_when_role_absent():
    tree = _make_a11y_tree("banner", "main")
    assert _first_position_of_landmark("contentinfo", tree) is None


def test_first_position_handles_missing_tree():
    assert _first_position_of_landmark("main", None) is None
    assert _first_position_of_landmark("main", {}) is None
    assert _first_position_of_landmark("main", {"nodes": []}) is None


# ── Verify end-to-end ──────────────────────────────────────────────────


def test_verify_a11y_project_failure_mode_contradicted():
    """The exact A11Y Project SC 1.3.2 hallucination: claim says
    'main after navigation and footer'; captured tree has main BEFORE
    both. Verifier returns 'contradicted'.
    """
    issue = (
        "The main content is placed AFTER secondary navigation and "
        "footer elements in the accessibility tree."
    )
    # A11Y Project's actual captured a11y tree order:
    tree = _make_a11y_tree(
        "complementary",  # Black Lives Matter
        "banner",
        "main",           # main is THIRD
        "complementary",
        "contentinfo",    # footer is FIFTH
        "navigation",     # nav is SIXTH
        "navigation",
    )
    assert verify_landmark_order_claim(issue, tree) == "contradicted"


def test_verify_correct_claim_returns_verified():
    issue = "The banner is placed before the main content."
    tree = _make_a11y_tree("banner", "main", "contentinfo")
    assert verify_landmark_order_claim(issue, tree) == "verified"


def test_verify_no_claim_returns_no_claim():
    tree = _make_a11y_tree("main", "contentinfo")
    assert verify_landmark_order_claim("Missing alt attribute.", tree) == "no_claim"


def test_verify_unverifiable_when_landmark_missing():
    """Claim mentions footer, but captured tree has no contentinfo
    landmark. Return 'unverifiable' so the finding is KEPT (conservative).
    """
    issue = "The main content appears after the footer."
    tree = _make_a11y_tree("banner", "main")  # no contentinfo
    assert verify_landmark_order_claim(issue, tree) == "unverifiable"


# ── Filter integration ────────────────────────────────────────────────


def test_filter_drops_contradicted_findings():
    findings = [
        Finding(
            id="f1", element="body",
            issue="Main content placed after navigation in the a11y tree.",
            impact="x", recommendation="x",
            severity=Severity.MEDIUM, source="visual_ai",
        ),
        Finding(
            id="f2", element="img.hero",
            issue="Image missing alt text",
            impact="x", recommendation="x",
            severity=Severity.HIGH, source="visual_ai",
        ),
    ]
    tree = _make_a11y_tree("main", "navigation")  # main IS before nav
    kept, dropped = filter_landmark_order_hallucinations(findings, tree)
    assert dropped == 1
    assert len(kept) == 1
    assert kept[0].id == "f2"  # the unrelated finding survives


def test_filter_keeps_findings_when_no_a11y_tree():
    """Conservative: no captured a11y tree -> can't verify -> keep all."""
    findings = [
        Finding(
            id="f1", element="x",
            issue="Main placed after footer in a11y tree.",
            impact="x", recommendation="x",
            severity=Severity.HIGH, source="visual_ai",
        ),
    ]
    kept, dropped = filter_landmark_order_hallucinations(findings, None)
    assert dropped == 0
    assert len(kept) == 1


def test_filter_keeps_unverifiable_claims():
    """Claim made but landmark missing from tree -> keep (conservative)."""
    findings = [
        Finding(
            id="f1", element="x",
            issue="Main placed after footer in a11y tree.",
            impact="x", recommendation="x",
            severity=Severity.HIGH, source="visual_ai",
        ),
    ]
    # Tree missing contentinfo
    tree = _make_a11y_tree("banner", "main")
    kept, dropped = filter_landmark_order_hallucinations(findings, tree)
    assert dropped == 0
    assert len(kept) == 1


def test_filter_with_dicts_not_findings():
    """The filter accepts both Finding objects and plain dicts."""
    findings = [
        {"issue": "Main placed after footer in a11y tree.",
         "severity": "high", "source": "visual_ai"},
        {"issue": "Different unrelated finding.",
         "severity": "low", "source": "visual_ai"},
    ]
    tree = _make_a11y_tree("main", "contentinfo")  # main BEFORE contentinfo
    kept, dropped = filter_landmark_order_hallucinations(findings, tree)
    assert dropped == 1
    assert len(kept) == 1
    assert "Different" in kept[0]["issue"]


def test_filter_downgrade_mode_keeps_finding_with_marker():
    """drop=False mode: contradicted findings stay but get downgraded
    to info severity with a [CONTRADICTED] marker.
    """
    findings = [
        Finding(
            id="f1", element="x",
            issue="Main placed after footer in a11y tree.",
            impact="x", recommendation="x",
            severity=Severity.HIGH, source="visual_ai",
        ),
    ]
    tree = _make_a11y_tree("main", "contentinfo")  # main BEFORE contentinfo
    kept, dropped = filter_landmark_order_hallucinations(
        findings, tree, drop=False,
    )
    assert dropped == 1
    assert len(kept) == 1
    assert kept[0].severity == Severity.INFO
    assert "CONTRADICTED" in kept[0].issue


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
