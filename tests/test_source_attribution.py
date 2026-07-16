"""Regression tests for the source-attribution validator.

The bugs these tests pin all caused legitimate visual_ai findings to be
demoted to ``judge_inference`` (and on PROGRAMMATIC_DEFINITIVE SCs,
silently dropped). On real runs we measured ~26% of all findings tagged
``judge_inference`` because of the ``ai`` vs ``visual_ai`` tag mismatch
between ``checks/base.py`` (set ``f.source = "ai"``) and the validator's
lookup (``by_source.get("visual_ai")`` -- empty bucket).

If any test starts failing, validator coverage regressed. Run with:

    python tests/test_source_attribution.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

try:
    import pytest
except ImportError:
    class _PytestStub:
        @staticmethod
        def raises(exc_type):
            class _Ctx:
                def __enter__(self_inner):
                    return self_inner
                def __exit__(self_inner, et, ev, tb):
                    if et is None:
                        raise AssertionError(
                            f"expected {exc_type.__name__} but no exception was raised"
                        )
                    return issubclass(et, exc_type)
            return _Ctx()
    pytest = _PytestStub()  # type: ignore

from functions.parser import (  # noqa: E402
    _build_source_index,
    validate_source_attribution,
)


# ── Reverse mirror: legacy "ai" → canonical "visual_ai" ─────────────────


def test_legacy_ai_input_matches_visual_ai_judge_claim():
    """Input findings tagged with the legacy ``"ai"`` source must be
    matchable when the judge correctly claims the canonical ``"visual_ai"``
    tag. Without the reverse mirror in ``_build_source_index``, every
    visual AI finding got demoted to ``judge_inference`` after the judge
    consolidated them (~26% of findings on real runs).
    """
    inputs = [
        {
            "source": "ai",
            "css_selector": "img#hero",
            "element": "Hero banner",
            "issue": "Image marked decorative but conveys content",
        },
    ]
    judge_outputs = [
        {
            "source": "visual_ai",
            "css_selector": "img#hero",
            "element": "Hero banner",
            "issue": "Image marked decorative but conveys content",
            "severity": "medium",
        },
    ]
    validated, flips = validate_source_attribution(judge_outputs, inputs)
    assert flips == 0, "Legacy 'ai' tag should match 'visual_ai' claim"
    assert validated[0]["source"] == "visual_ai", (
        f"Expected source preserved as 'visual_ai', got {validated[0]['source']!r}"
    )


def test_canonical_visual_ai_input_matches_visual_ai_judge_claim():
    """The post-fix happy path: input correctly tagged ``"visual_ai"``
    matches the judge's ``"visual_ai"`` claim with no demotion.
    """
    inputs = [
        {
            "source": "visual_ai",
            "css_selector": ".btn-primary",
            "element": "Apply Now button",
            "issue": "Low contrast against background",
        },
    ]
    judge_outputs = [
        {
            "source": "visual_ai",
            "css_selector": ".btn-primary",
            "element": "Apply Now button",
            "issue": "Low contrast against background",
            "severity": "high",
        },
    ]
    validated, flips = validate_source_attribution(judge_outputs, inputs)
    assert flips == 0
    assert validated[0]["source"] == "visual_ai"


# ── Forward mirror is unchanged ─────────────────────────────────────────


def test_visual_ai_input_indexed_under_legacy_ai_bucket():
    """Forward mirror (visual_ai → ai) is preserved so legacy callers
    that ask for ``"ai"`` still work.
    """
    inputs = [
        {
            "source": "visual_ai",
            "css_selector": "#hero",
            "element": "",
            "issue": "Hero image alt missing",
        },
    ]
    index = _build_source_index(inputs)
    assert "visual_ai" in index
    assert "ai" in index, "Forward mirror visual_ai → ai must still populate"
    assert index["visual_ai"] == index["ai"]


def test_legacy_ai_input_indexed_under_canonical_bucket():
    """Reverse mirror (ai → visual_ai) is the new behavior: an input
    tagged ``"ai"`` must also be findable under ``"visual_ai"``.
    """
    inputs = [
        {
            "source": "ai",
            "css_selector": "#hero",
            "element": "",
            "issue": "Hero image alt missing",
        },
    ]
    index = _build_source_index(inputs)
    assert "ai" in index
    assert "visual_ai" in index, (
        "Reverse mirror ai → visual_ai must populate the canonical bucket "
        "so judge claims of 'visual_ai' match legacy 'ai' inputs"
    )
    assert index["visual_ai"] == index["ai"]


def test_legacy_ai_does_not_overmatch_to_code_ai():
    """The reverse mirror sends ``ai`` only to ``visual_ai``. A judge
    claim of ``code_ai`` against an ``ai``-tagged input must NOT match,
    because the legacy generic tag historically meant the visual AI
    run, never code AI or AT sim.
    """
    inputs = [
        {
            "source": "ai",
            "css_selector": ".btn",
            "element": "",
            "issue": "Some issue",
        },
    ]
    index = _build_source_index(inputs)
    assert "code_ai" not in index, (
        "ai → code_ai mirror would let the judge launder visual inferences "
        "as code-pattern findings"
    )
    assert "at_sim" not in index


# ── End-to-end: judge claims survive validation ─────────────────────────


def test_judge_consolidating_three_visual_ai_findings_keeps_source():
    """The exact pattern from the SC 1.1.1 audit on
    20260506_135324_f8765656: visual_ai produced 13 raw findings, the
    judge consolidated 4 of them, claimed source='visual_ai'. With the
    pre-fix index, all 4 got demoted. With the fix, all 4 stay.
    """
    inputs = [
        {
            "source": "ai",  # legacy tag from base.py:634 pre-fix
            "css_selector": "#skip-to-content > div > article > div:nth-of-type(9) > div > div > div > div > picture > img",
            "element": "",
            "issue": "WCAG 1.1.1 requires a text alternative for non-text content",
        },
        {
            "source": "ai",
            "css_selector": "div > div:nth-of-type(2) > picture > img",
            "element": "",
            "issue": "WCAG 1.1.1 requires a text alternative for non-text content",
        },
        {
            "source": "ai",
            "css_selector": "#edit-location-inperson",
            "element": "",
            "issue": "Element uses CSS background image without text alternative",
        },
        {
            "source": "ai",
            "css_selector": "#edit-standing-undergrad",
            "element": "",
            "issue": "Element uses CSS background image without text alternative",
        },
    ]
    judge_outputs = [
        {
            "source": "visual_ai",
            "css_selector": "#skip-to-content > div > article > div:nth-of-type(9) > div > div > div > div > picture > img",
            "element": "Carve your path image",
            "issue": "Image marked decorative but provides meaningful context",
            "severity": "medium",
        },
        {
            "source": "visual_ai",
            "css_selector": "div > div:nth-of-type(2) > picture > img",
            "element": "University for You image",
            "issue": "Image marked decorative but provides meaningful context",
            "severity": "medium",
        },
        {
            "source": "visual_ai",
            "css_selector": "#edit-location-inperson",
            "element": "In person radio button container",
            "issue": "Element uses CSS background image without text alternative",
            "severity": "medium",
        },
        {
            "source": "visual_ai",
            "css_selector": "#edit-standing-undergrad",
            "element": "Undergraduate radio button container",
            "issue": "Element uses CSS background image without text alternative",
            "severity": "medium",
        },
    ]
    validated, flips = validate_source_attribution(judge_outputs, inputs)
    assert flips == 0, (
        f"Pre-fix: 4 findings demoted to judge_inference. "
        f"Post-fix expectation: 0 demotions. Got {flips}."
    )
    for f in validated:
        assert f["source"] == "visual_ai", (
            f"Source should stay 'visual_ai' for legitimate consolidation; "
            f"got {f['source']!r}"
        )


# ── True judge inferences still get demoted ─────────────────────────────


def test_genuinely_invented_visual_ai_claim_still_demoted():
    """The validator must still catch genuine fabrications: a judge
    claiming visual_ai with a selector NO input source produced.
    Without this, the fix would let the model launder its own inferences
    as measurements.
    """
    inputs = [
        {
            "source": "visual_ai",
            "css_selector": "#real-element",
            "element": "",
            "issue": "Real visual issue",
        },
    ]
    judge_outputs = [
        {
            "source": "visual_ai",
            "css_selector": "#fabricated-id",  # not in any input
            "element": "Made up element",
            "issue": "Some claim the model invented",
            "severity": "high",
        },
    ]
    validated, flips = validate_source_attribution(judge_outputs, inputs)
    assert flips == 1
    assert validated[0]["source"] == "judge_inference"


def test_judge_inventing_programmatic_claim_demoted():
    """Pre-existing protection: judge claiming source='programmatic' for
    a finding no programmatic check produced. Verifies the original bug
    that motivated this validator (a university run 2.5.8) still gets caught.
    """
    inputs = [
        {
            "source": "programmatic",
            "css_selector": ".real-target",
            "element": "",
            "issue": "Target measured below 24x24",
        },
    ]
    judge_outputs = [
        {
            "source": "programmatic",
            "css_selector": ".invented",
            "element": "Targets the model thinks fail",
            "issue": "0px spacing between targets",  # fabricated
            "severity": "high",
        },
    ]
    validated, flips = validate_source_attribution(judge_outputs, inputs)
    assert flips == 1
    assert validated[0]["source"] == "judge_inference"


# ── Anti-laundering: issue-text overlap without selector/element anchor ──


def test_pure_issue_text_overlap_without_selector_anchor_demotes():
    """The leak the fairfaxva.gov 20260514 audit caught: judge claimed
    source='axe' with a fabricated selector and an issue string whose
    head matched an unrelated upstream axe finding by coincidence.

    Pre-fix: head-overlap (first 30 chars) was an independent match
    path, so the laundered claim passed validation.

    Post-fix: with no selector and no element anchor, issue-text head
    overlap alone is rejected, and the claim is correctly demoted.
    """
    inputs = [
        {
            "source": "axe",
            "css_selector": "#real-button",
            "element": "",
            "issue": "Element has insufficient contrast ratio of 3.21:1",
        },
    ]
    judge_outputs = [
        {
            "source": "axe",
            "css_selector": "#prefix-submitButton",  # different element entirely
            "element": "",
            # First 30 chars overlap with the input -- the leak.
            "issue": "Element has insufficient contrast ratio of 4.44:1",
            "severity": "high",
        },
    ]
    validated, flips = validate_source_attribution(judge_outputs, inputs)
    assert flips == 1, "Pure issue-text head overlap with no selector anchor must NOT match"
    assert validated[0]["source"] == "judge_inference"


def test_fabricated_selector_with_matching_source_tag_demotes():
    """SC 2.1.1 audit case: judge invented selector 'div.sc-search-panel'
    and labeled it source='htmlcs'. No htmlcs input had that selector
    or anything containing/contained by it. Must be demoted.
    """
    inputs = [
        {
            "source": "htmlcs",
            "css_selector": "#header-search > div > div:nth-of-type(1)",
            "element": "",
            "issue": "Search container is not keyboard focusable",
        },
    ]
    judge_outputs = [
        {
            "source": "htmlcs",
            "css_selector": "div.sc-search-panel",  # invented
            "element": "",
            "issue": "Search panel is not keyboard focusable",
            "severity": "medium",
        },
    ]
    validated, flips = validate_source_attribution(judge_outputs, inputs)
    assert flips == 1
    assert validated[0]["source"] == "judge_inference"


def test_invented_dom_order_claim_demotes():
    """SC 1.3.2 audit case: judge invented selector 'div[role=\"dialog\"]'
    with a DOM-order claim derived from a list-position inference, not
    from any deterministic landmark-order data. The visual_ai upstream
    had a different selector (a footer text block). Must be demoted.
    """
    inputs = [
        {
            "source": "visual_ai",
            "css_selector": "#footer-stay-connected-link",
            "element": "Stay Connected link in footer",
            "issue": "Footer link visible",
        },
    ]
    judge_outputs = [
        {
            "source": "visual_ai",
            "css_selector": 'div[role="dialog"]',  # invented from list ordinal
            "element": "Stay Connected dialog",
            "issue": "Dialog placed at end of DOM, after footer",
            "severity": "medium",
        },
    ]
    validated, flips = validate_source_attribution(judge_outputs, inputs)
    assert flips == 1
    assert validated[0]["source"] == "judge_inference"


def test_element_equality_alone_no_longer_overmatches():
    """Element-field equality used to match unconditionally. Two
    findings with the same human-readable label like 'Apply Now button'
    but completely different selectors and unrelated issue text would
    pass. Now element-equality requires issue-text co-occurrence.
    """
    inputs = [
        {
            "source": "programmatic",
            "css_selector": "#apply-btn",
            "element": "Apply Now button",
            "issue": "Color contrast 3.0:1 insufficient",
        },
    ]
    judge_outputs = [
        {
            "source": "programmatic",
            "css_selector": "#a-completely-different-button",
            "element": "Apply Now button",  # same label, different element
            "issue": "Button has no accessible name (aria-label missing)",
            "severity": "high",
        },
    ]
    validated, flips = validate_source_attribution(judge_outputs, inputs)
    assert flips == 1, "Element-label collision across SCs must NOT match without issue overlap"
    assert validated[0]["source"] == "judge_inference"


def test_element_equality_with_issue_overlap_still_matches():
    """Legitimate path preserved: when the judge consolidates an input
    by re-stating the same element + same issue but omits the selector,
    the element+issue match still validates.
    """
    inputs = [
        {
            "source": "programmatic",
            "css_selector": "",
            "element": "Apply Now button",
            "issue": "Color contrast measured at 3.0:1 below the 4.5:1 threshold",
        },
    ]
    judge_outputs = [
        {
            "source": "programmatic",
            "css_selector": "",
            "element": "Apply Now button",
            "issue": "Color contrast measured at 3.0:1 below the 4.5:1 threshold",
            "severity": "high",
        },
    ]
    validated, flips = validate_source_attribution(judge_outputs, inputs)
    assert flips == 0
    assert validated[0]["source"] == "programmatic"


# ── Runner ─────────────────────────────────────────────────────────────


if __name__ == "__main__":
    import inspect
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
