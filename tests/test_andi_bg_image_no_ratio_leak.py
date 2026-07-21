"""Regression tests for two structural fixes that prevent the
SC 1.4.3 false-positive cluster from re-emerging:

1. ANDI's bg_image_present finding must NOT include the unreliable
   computed ratio in its issue or evidence prose. The rendered prompt
   the fast-path judge reads exposes both fields; if the unreliable
   ratio appears anywhere there, the judge re-quotes it in HIGH-
   severity output (verified failure on a university-site 2026-05-09 SC 1.4.3:
   3 HIGH findings citing 1.23:1 emerged from INFO-only input).

2. ``_judge_vpat_synthesis`` must clamp output findings' severity
   to the max severity of input findings. The deterministic input is
   ground truth; the fast-path judge is rewriting prose, not
   inventing new evidence -- it cannot legitimately upgrade INFO
   inputs to HIGH outputs.

Run with:

    python tests/test_andi_bg_image_no_ratio_leak.py
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from functions.andi_extract import extract_andi_contrast_findings  # noqa: E402
from models import CaptureData, Severity  # noqa: E402


# ── Fix 1: no ratio in prose for bg_image_present entries ──────────────


def _make_bg_image_andi_entry(ratio=1.23, selector=".article-text"):
    return {
        "ratio": ratio,
        "selector": selector,
        "text": "Reimagining education for everyone",
        "fg_color": [240, 240, 240],
        "bg_color": [255, 255, 255],
        "fg_color_raw": "rgb(240, 240, 240)",
        "bg_color_raw": "rgb(255, 255, 255)",
        "is_large_text": True,
        "bg_image_present": True,
        "bg_walk_depth": 4,
        "is_svg_text": False,
        "passes": False,
    }


def test_bg_image_finding_issue_does_not_quote_ratio():
    """The issue field rendered to the fast-path judge must not
    contain the unreliable ratio number. Pre-fix: '1.23:1' appeared
    in the issue prose 3 times in the SC 1.4.3 prompt and the judge
    re-quoted it in HIGH-severity outputs.
    """
    cd = CaptureData(url="https://t/")
    cd.andi_contrast_results = [_make_bg_image_andi_entry(ratio=1.23)]
    findings = extract_andi_contrast_findings(cd, "1.4.3")
    assert len(findings) == 1
    f = findings[0]
    assert f.severity == Severity.INFO
    assert "1.23" not in f.issue, (
        f"unreliable ratio leaked into issue prose: {f.issue!r}"
    )
    assert "1.23" not in f.recommendation, (
        f"unreliable ratio leaked into recommendation: {f.recommendation!r}"
    )


def test_bg_image_finding_evidence_does_not_quote_ratio():
    """The evidence field is rendered into the FAST-path judge prompt
    via _judge_vpat_synthesis._render_findings (line 1184). Any number
    placed there is visible to the judge and can be re-quoted in
    output findings.
    """
    cd = CaptureData(url="https://t/")
    cd.andi_contrast_results = [_make_bg_image_andi_entry(ratio=1.23)]
    findings = extract_andi_contrast_findings(cd, "1.4.3")
    f = findings[0]
    # Evidence may describe what's KNOWN deterministically (fg, bg,
    # walk depth) but must NOT contain the unreliable ratio.
    assert "1.23" not in (f.evidence or ""), (
        f"unreliable ratio leaked into evidence: {f.evidence!r}"
    )


def test_bg_image_finding_still_explains_what_it_is():
    """Removing the number must not turn the finding into a useless
    stub -- it should still tell the auditor the element needs
    manual review and explain why the ratio is omitted.
    """
    cd = CaptureData(url="https://t/")
    cd.andi_contrast_results = [_make_bg_image_andi_entry(ratio=1.23)]
    findings = extract_andi_contrast_findings(cd, "1.4.3")
    f = findings[0]
    issue_lower = f.issue.lower()
    # Must explain the unreliability
    assert any(
        t in issue_lower for t in
        ("manual", "background image", "fallback", "unreliable", "cannot be reliably")
    ), f"issue prose lost its informational value: {f.issue!r}"
    assert "1.4.3" in f.issue or "1.4.6" in f.issue


def test_non_bg_image_finding_still_includes_ratio():
    """Don't over-correct: a real low-contrast finding (no bg image)
    SHOULD still cite the measured ratio because the measurement is
    reliable when the bg-color walk found a real solid colour.
    """
    cd = CaptureData(url="https://t/")
    cd.andi_contrast_results = [{
        "ratio": 2.5,
        "selector": "p.lead",
        "text": "real low contrast text",
        "fg_color": [128, 128, 128],
        "bg_color": [255, 255, 255],
        "fg_color_raw": "rgb(128, 128, 128)",
        "bg_color_raw": "rgb(255, 255, 255)",
        "is_large_text": False,
        "bg_image_present": False,  # reliable measurement
        "bg_walk_depth": 0,
        "is_svg_text": False,
        "passes": False,
    }]
    findings = extract_andi_contrast_findings(cd, "1.4.3")
    assert len(findings) == 1
    # Reliable measurement should be cited
    assert "2.5" in findings[0].issue, (
        "reliable measured ratio should still appear in issue prose"
    )


# ── Fix 2: VPAT synthesis severity ceiling ─────────────────────────────


def test_vpat_synthesis_caps_severity_to_input_max():
    """The fast-path judge cannot legitimately escalate INFO inputs
    to HIGH outputs -- it has no new evidence. Verified failure mode:
    SC 1.4.3 with 59 INFO inputs produced 3 HIGH outputs.
    """
    from analysis import judge as judge_mod

    # Stub _call_judge to return an artificially-escalated payload.
    # The cap should clamp it back down.
    async def fake_call_judge(*, system_prompt, user_prompt, images=None):
        return {
            "conformance_level": "Supports",  # ignored in fast path
            "confidence": 0.9,
            "reasoning": "stub",
            "final_findings": [
                {
                    "element": "elem-1", "css_selector": ".x",
                    "issue": "made-up high severity claim",
                    "impact": "stub", "recommendation": "stub",
                    "severity": "high",
                    "source": "andi",
                },
                {
                    "element": "elem-2", "css_selector": ".y",
                    "issue": "made-up medium severity claim",
                    "impact": "stub", "recommendation": "stub",
                    "severity": "medium",
                    "source": "andi",
                },
            ],
            "rejected_findings": [],
            "vpat_summary": "stub summary",
        }

    # Inputs are ALL info severity
    inputs = [
        {"severity": "info", "source": "andi", "element": "x",
         "issue": "info claim 1", "css_selector": ".x"},
        {"severity": "info", "source": "andi", "element": "y",
         "issue": "info claim 2", "css_selector": ".y"},
    ]
    source_verdicts = {"Programmatic": {
        "conformance": "Supports", "confidence": 0.9, "findings_count": 2,
    }}

    with patch.object(judge_mod, "_call_judge", fake_call_judge):
        result = asyncio.run(judge_mod._judge_vpat_synthesis(
            criterion_id="1.4.3",
            criterion_name="Contrast (Minimum)",
            level="AA",
            normative_text="...",
            source_verdicts=source_verdicts,
            all_findings=inputs,
            wcag_version="2.2",
            dom_context="",
            product_context=None,
            criterion_guidance="",
        ))

    assert result is not None
    severities = [f["severity"] for f in result["final_findings"]]
    # ALL outputs must be capped to "info" because input max was info
    assert all(s == "info" for s in severities), (
        f"severity ceiling regressed: outputs {severities!r} vs input max info"
    )


def test_vpat_synthesis_allows_severity_up_to_input_max():
    """The cap is a ceiling, not a floor: when input has MEDIUM
    findings, output can be MEDIUM. Pin that the fix doesn't
    over-clamp.
    """
    from analysis import judge as judge_mod

    async def fake_call_judge(*, system_prompt, user_prompt, images=None):
        return {
            "conformance_level": "Partially Supports",
            "confidence": 0.9, "reasoning": "stub",
            "final_findings": [{
                "element": "x", "css_selector": ".x",
                "issue": "ok", "impact": "ok", "recommendation": "ok",
                "severity": "medium",
                "source": "andi",
            }],
            "rejected_findings": [],
            "vpat_summary": "stub",
        }

    inputs = [
        {"severity": "medium", "source": "andi", "element": "x",
         "issue": "real medium claim", "css_selector": ".x"},
    ]
    source_verdicts = {"Programmatic": {
        "conformance": "Partially Supports", "confidence": 0.9,
        "findings_count": 1,
    }}

    with patch.object(judge_mod, "_call_judge", fake_call_judge):
        result = asyncio.run(judge_mod._judge_vpat_synthesis(
            criterion_id="1.4.3",
            criterion_name="Contrast",
            level="AA",
            normative_text="...",
            source_verdicts=source_verdicts,
            all_findings=inputs,
            wcag_version="2.2",
            dom_context="",
            product_context=None,
            criterion_guidance="",
        ))

    assert result is not None
    assert result["final_findings"][0]["severity"] == "medium", (
        "input had medium severity; output medium should be allowed"
    )


def test_vpat_synthesis_does_not_upgrade_low_inputs_to_high():
    """Mirror test for the high-input case but with low inputs.
    All outputs must be ≤ low.
    """
    from analysis import judge as judge_mod

    async def fake_call_judge(*, system_prompt, user_prompt, images=None):
        return {
            "conformance_level": "Supports", "confidence": 0.9,
            "reasoning": "stub",
            "final_findings": [
                {"element": "a", "css_selector": ".a",
                 "issue": "x", "impact": "x", "recommendation": "x",
                 "severity": "high", "source": "andi"},
                {"element": "b", "css_selector": ".b",
                 "issue": "x", "impact": "x", "recommendation": "x",
                 "severity": "low", "source": "andi"},
            ],
            "rejected_findings": [], "vpat_summary": "x",
        }

    inputs = [
        {"severity": "low", "source": "andi", "element": "a",
         "issue": "x", "css_selector": ".a"},
    ]
    source_verdicts = {"Programmatic": {
        "conformance": "Supports", "confidence": 0.9, "findings_count": 1,
    }}

    with patch.object(judge_mod, "_call_judge", fake_call_judge):
        result = asyncio.run(judge_mod._judge_vpat_synthesis(
            criterion_id="x", criterion_name="x", level="AA",
            normative_text="x", source_verdicts=source_verdicts,
            all_findings=inputs, wcag_version="2.2",
            dom_context="", product_context=None, criterion_guidance="",
        ))

    severities = [f["severity"] for f in result["final_findings"]]
    # Both outputs must be capped to "low" (the input max)
    assert all(s in ("low", "info") for s in severities), (
        f"severities {severities!r} exceeded low input ceiling"
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
