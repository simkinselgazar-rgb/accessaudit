"""Regression tests for IBM EAC + HTMLCS extractor quality fixes.

Three issue categories these tests pin (all discovered during the
2026-05-09 university live run):

A. IBM EAC ``text_contrast_sufficient`` and ``element_textwithin_color_sufficient``
   produce 1.23:1 fallback ratios when text sits over a background
   image. ANDI's per-text-node walk already marks the same elements
   ``bg_image_present=True``; the fix uses that signal to drop the
   IBM EAC false-positive findings before they reach the judge prompt.
   Verified gap on SC 1.4.3 where 8 IBM_EAC findings all cited
   1.23:1 against `<article>` text over a video background.

B. IBM EAC ``style_focus_visible`` over-fires on any element with
   ``outline:none`` regardless of alternative indicators. The
   deterministic byte-identical-screenshot probe already records
   ``has_change=True`` when focus IS visible; the fix uses that to
   filter the false positives. Verified gap on SC 2.4.7 where 40
   IBM_EAC findings stacked vs 3 from the deterministic probe.

C. HTMLCS + IBM EAC ``impact`` field previously contained generic
   prose with no disability groups or AT names, triggering 25+
   audit-script warnings. The fix supplies per-rule (IBM) and per-SC
   (HCS) impact prose that names specific groups and AT.

Run with:

    python tests/test_external_tool_quality.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from functions.htmlcs_extract import (  # noqa: E402
    _GENERIC_HCS_IMPACT,
    _SC_IMPACT_PROSE,
    extract_htmlcs_findings,
)
from functions.ibm_eac_extract import (  # noqa: E402
    _RULE_IMPACT_PROSE,
    extract_ibm_eac_findings,
)
from models import CaptureData, Severity  # noqa: E402


# ── Issue A: IBM EAC contrast bg-image filter ───────────────────────────


def test_ibm_contrast_dropped_when_andi_says_bg_image():
    """The exact university-run SC 1.4.3 false positive: text in `<article>` over
    a video background. ANDI marks the element bg_image_present=True
    (its bg-color walk hit a fallback). IBM EAC measures 1.23:1
    against the same fallback. The filter should drop it.
    """
    cd = CaptureData(url="https://t/")
    cd.andi_contrast_results = [
        {
            "selector": ".article-content",
            "text": "Reimagining education for everyone",
            "bg_image_present": True,
            "ratio": 1.23,
        },
    ]
    cd.ibm_eac_results = {
        "results": [
            {
                "ruleId": "text_contrast_sufficient",
                "value": ["VIOLATION", "FAIL"],
                "path_dom": "/html[1]/body[1]/main[1]/article[1]/.article-content",
                "snippet": "<p>Reimagining education for everyone</p>",
                "message": "The text contrast ratio is 1.23:1, below 4.5:1.",
            },
        ],
    }
    findings = extract_ibm_eac_findings(cd, "1.4.3")
    assert findings == [], (
        "Pre-fix: 1.23:1 fallback finding flowed through. "
        "Post-fix: ANDI bg_image_present should drop it."
    )


def test_ibm_contrast_kept_when_andi_says_clean_bg():
    """Don't over-filter: a real contrast finding on text WITH a
    reliably-resolved background must still flow through.
    """
    cd = CaptureData(url="https://t/")
    cd.andi_contrast_results = [
        {
            "selector": "p.lead",
            "text": "real low contrast text",
            "bg_image_present": False,
            "ratio": 2.5,
        },
    ]
    cd.ibm_eac_results = {
        "results": [
            {
                "ruleId": "text_contrast_sufficient",
                "value": ["VIOLATION", "FAIL"],
                "path_dom": "p.lead",
                "snippet": "<p class=\"lead\">real low contrast text</p>",
                "message": "Text contrast 2.5:1 is below 4.5:1.",
            },
        ],
    }
    findings = extract_ibm_eac_findings(cd, "1.4.3")
    assert len(findings) == 1
    assert findings[0].severity == Severity.HIGH


def test_ibm_contrast_kept_when_andi_silent():
    """Conservative default: when ANDI didn't sample the element at
    all (e.g. SVG text node ANDI couldn't resolve), the IBM EAC
    finding should be KEPT so the judge can evaluate against
    screenshots.
    """
    cd = CaptureData(url="https://t/")
    cd.andi_contrast_results = []  # ANDI didn't run / no samples
    cd.ibm_eac_results = {
        "results": [
            {
                "ruleId": "text_contrast_sufficient",
                "value": ["VIOLATION", "FAIL"],
                "path_dom": "svg text.label",
                "snippet": "<text class=\"label\">data</text>",
                "message": "Contrast may be insufficient.",
            },
        ],
    }
    findings = extract_ibm_eac_findings(cd, "1.4.3")
    assert len(findings) == 1


# ── Issue B: IBM EAC focus-visible corroboration ────────────────────────


def test_ibm_focus_visible_dropped_when_pixels_show_visible_focus():
    """Pre-fix university-run SC 2.4.7: 40 ibm_eac findings vs 3 from the
    byte-identical screenshot probe. The fix: if the deterministic
    focus-contrast probe records has_change=True for the element
    (pixels prove focus IS visible), drop the IBM EAC over-report.
    """
    cd = CaptureData(url="https://t/")
    cd.focus_contrast = [
        {
            "selector": ".btn-primary",
            "has_change": True,  # pixels prove focus visible
            "indicator_type": "outline",
        },
    ]
    cd.ibm_eac_results = {
        "results": [
            {
                "ruleId": "style_focus_visible",
                "value": ["VIOLATION", "POTENTIAL"],
                "path_dom": "button.btn-primary",
                "message": "outline:none used without alternative indicator",
            },
        ],
    }
    findings = extract_ibm_eac_findings(cd, "2.4.7")
    assert findings == [], (
        "Pre-fix: 40 over-reports. Post-fix: pixels prove focus "
        "visible, IBM EAC false positive must be dropped."
    )


def test_ibm_focus_visible_kept_when_pixels_show_no_change():
    """The other side: when the byte-identical probe confirms there
    IS no visible focus state, the IBM EAC finding stands.
    """
    cd = CaptureData(url="https://t/")
    cd.focus_contrast = [
        {
            "selector": ".invisible-focus",
            "has_change": False,  # no visible state change
        },
    ]
    cd.ibm_eac_results = {
        "results": [
            {
                "ruleId": "style_focus_visible",
                "value": ["VIOLATION", "POTENTIAL"],
                "path_dom": "button.invisible-focus",
                "message": "outline:none with no alternative",
            },
        ],
    }
    findings = extract_ibm_eac_findings(cd, "2.4.7")
    assert len(findings) == 1


def test_ibm_focus_visible_kept_when_no_focus_data():
    """Conservative: when focus_contrast didn't capture this element,
    keep the finding so the judge can decide.
    """
    cd = CaptureData(url="https://t/")
    cd.focus_contrast = []  # no probe data
    cd.ibm_eac_results = {
        "results": [
            {
                "ruleId": "style_focus_visible",
                "value": ["VIOLATION", "POTENTIAL"],
                "path_dom": "button.uncaptured",
                "message": "outline:none",
            },
        ],
    }
    findings = extract_ibm_eac_findings(cd, "2.4.7")
    assert len(findings) == 1


# ── Issue C: per-rule / per-SC impact prose ─────────────────────────────


def test_ibm_impact_passes_audit_script_rule():
    """Every entry in _RULE_IMPACT_PROSE must include at least one term
    from ``audit_sc.py:DISABILITY_TERMS`` (the actual rule the audit
    script uses to grade impact prose). Aligns this test with the
    real-world enforcement check so passing here means the audit
    script will not flag the finding.
    """
    from audit_sc import DISABILITY_TERMS
    failures = []
    for rule, prose in _RULE_IMPACT_PROSE.items():
        prose_lower = prose.lower()
        if not any(t in prose_lower for t in DISABILITY_TERMS):
            failures.append(f"{rule}: no audit-script-recognised term")
    assert not failures, (
        f"IBM EAC impact prose missing audit-recognised vocabulary:\n  "
        + "\n  ".join(failures[:10])
    )


def test_htmlcs_impact_passes_audit_script_rule():
    """Same audit-aligned rule for HCS per-SC impact prose."""
    from audit_sc import DISABILITY_TERMS
    failures = []
    for sc, prose in _SC_IMPACT_PROSE.items():
        prose_lower = prose.lower()
        if not any(t in prose_lower for t in DISABILITY_TERMS):
            failures.append(f"SC {sc}: no audit-script-recognised term")
    assert not failures, (
        f"HTMLCS impact prose missing audit-recognised vocabulary:\n  "
        + "\n  ".join(failures[:10])
    )


def test_ibm_finding_uses_per_rule_impact_when_available():
    cd = CaptureData(url="https://t/")
    cd.ibm_eac_results = {
        "results": [
            {
                "ruleId": "img_alt_valid",
                "value": ["VIOLATION", "FAIL"],
                "path_dom": "img.hero",
                "message": "Image missing alt",
            },
        ],
    }
    findings = extract_ibm_eac_findings(cd, "1.1.1")
    assert findings[0].impact == _RULE_IMPACT_PROSE["img_alt_valid"]
    # Not the generic fallback
    assert "JAWS" in findings[0].impact


def test_ibm_finding_falls_back_to_generic_for_unknown_rule():
    cd = CaptureData(url="https://t/")
    cd.ibm_eac_results = {
        "results": [
            {
                "ruleId": "newly_added_rule_2026",
                "value": ["VIOLATION", "FAIL"],
                "path_dom": "x",
                "message": "WCAG 1.1.1 violation: some new rule",
            },
        ],
    }
    findings = extract_ibm_eac_findings(cd, "1.1.1")
    assert len(findings) == 1
    # Generic fallback still names AT terms
    assert "JAWS" in findings[0].impact or "screen reader" in findings[0].impact.lower()


def test_htmlcs_finding_uses_per_sc_impact_prose():
    cd = CaptureData(url="https://t/")
    cd.htmlcs_results = {
        "messages": [
            {
                "type": 1,
                "code": "WCAG2AA.Principle1.Guideline1_1.1_1_1.H37",
                "msg": "Image missing alt",
                "selector": "img.hero",
                "tag_name": "img",
            },
        ],
    }
    findings = extract_htmlcs_findings(cd, "1.1.1")
    assert findings[0].impact == _SC_IMPACT_PROSE["1.1.1"]
    assert "JAWS" in findings[0].impact


def test_htmlcs_issue_text_includes_wcag_citation():
    """Every HCS finding's issue field must cite the SC explicitly so
    the audit script's "no WCAG reference" warning doesn't fire on
    valid extractor output.
    """
    cd = CaptureData(url="https://t/")
    cd.htmlcs_results = {
        "messages": [
            {
                "type": 1,
                "code": "WCAG2AA.Principle2.Guideline2_4.2_4_4.H77",
                "msg": "Link with no text",
                "selector": "a.empty",
                "tag_name": "a",
            },
        ],
    }
    findings = extract_htmlcs_findings(cd, "2.4.4")
    assert "WCAG 2.4.4" in findings[0].issue


def test_ibm_issue_text_includes_wcag_citation():
    cd = CaptureData(url="https://t/")
    cd.ibm_eac_results = {
        "results": [
            {
                "ruleId": "img_alt_valid",
                "value": ["VIOLATION", "FAIL"],
                "path_dom": "img.hero",
                "message": "Missing alt",
            },
        ],
    }
    findings = extract_ibm_eac_findings(cd, "1.1.1")
    assert "WCAG 1.1.1" in findings[0].issue


# ── Issue D: SC 2.4.4 duplicate-text check selector population ──────────


def test_sc_2_4_4_duplicate_link_text_emits_selectors():
    """Pre-fix: 10 findings with empty css_selector caused 35 audit-
    script warnings. Post-fix: every finding lists the selectors of
    every matching link.
    """
    from checks.checks_2_4 import Check_2_4_4
    check = Check_2_4_4()
    cd = CaptureData(url="https://example.test/page")
    cd.links = [
        {"text": "Learn more", "href": "/programs", "selector": "#hero a"},
        {"text": "Learn more", "href": "/about",    "selector": "#sidebar a"},
        {"text": "Learn more", "href": "/contact",  "selector": "footer a"},
        {"text": "Apply now",  "href": "/apply",    "selector": "#cta a"},
    ]
    import asyncio
    _, _, findings = asyncio.run(check.run_programmatic(cd))
    dup_findings = [f for f in findings if "identical text" in (f.issue or "")]
    assert len(dup_findings) >= 1, "duplicate-text check should fire"
    f = dup_findings[0]
    assert f.css_selector, "css_selector must NOT be empty"
    # Every matching selector must appear
    assert "#hero a" in f.css_selector
    assert "#sidebar a" in f.css_selector
    assert "footer a" in f.css_selector
    # Issue must cite WCAG explicitly
    assert "WCAG 2.4.4" in f.issue
    # Impact must name AT
    impact_lower = (f.impact or "").lower()
    assert any(t in impact_lower for t in ("jaws", "nvda", "voiceover")), (
        f"impact must name AT; got: {f.impact!r}"
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
