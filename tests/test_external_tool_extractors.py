"""Regression tests for HTMLCS and IBM Equal Access extractors.

These pin the behavior of the two open-source rule engines integrated
alongside axe-core. Bugs to guard against:

- Wrong SC attribution (e.g. an HCS message about 1.3.1 surfacing on
  a 1.4.3 check) → pollutes the wrong SC's prompt.
- Soft-no-op breakage (capture failed, returned {error: ...}) →
  extractor must return [] gracefully, not crash.
- Severity miscoding (HCS NOTICE flagged as HIGH, IBM POTENTIAL flagged
  as PASS) → either over-reports false positives or hides real signal.
- Source tag drift (extractor emits source="hcs" instead of "htmlcs",
  or "ibm" instead of "ibm_eac") → validator demotes legitimate
  findings to judge_inference.

Run with:

    python tests/test_external_tool_extractors.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from functions.htmlcs_extract import (  # noqa: E402
    _sc_id_from_code,
    extract_htmlcs_findings,
)
from functions.ibm_eac_extract import (  # noqa: E402
    IBM_RULE_TO_SC,
    _judgment_of,
    _scs_for_rule,
    extract_ibm_eac_findings,
)
from models import CaptureData, Severity  # noqa: E402


# ── HTMLCS: SC parsing ─────────────────────────────────────────────────


def test_sc_id_from_typical_hcs_code():
    """The standard pattern: WCAG2AA.PrincipleN.GuidelineX_Y.X_Y_Z.Technique"""
    code = "WCAG2AA.Principle1.Guideline1_1.1_1_1.H37"
    assert _sc_id_from_code(code) == "1.1.1"


def test_sc_id_from_two_digit_subtests():
    """SCs like 1.4.10, 2.4.11, 1.4.13 have two-digit final segments."""
    code = "WCAG2AA.Principle1.Guideline1_4.1_4_10.G193"
    assert _sc_id_from_code(code) == "1.4.10"
    code = "WCAG2AA.Principle2.Guideline2_4.2_4_11.G194"
    assert _sc_id_from_code(code) == "2.4.11"


def test_sc_id_from_unrelated_code_returns_empty():
    """A best-practice rule with no SC triple in its code should not
    be attributed to any SC -- attribution would be guesswork.
    """
    assert _sc_id_from_code("BestPractice.SomeCheck") == ""
    assert _sc_id_from_code("") == ""
    assert _sc_id_from_code(None) == ""


# ── HTMLCS: severity mapping ───────────────────────────────────────────


def test_htmlcs_error_maps_to_high_severity():
    cd = CaptureData(url="https://t/")
    cd.htmlcs_results = {
        "messages": [
            {
                "type": 1,  # ERROR
                "code": "WCAG2AA.Principle1.Guideline1_1.1_1_1.H37",
                "msg": "Image missing alt",
                "selector": "img.hero",
                "tag_name": "img",
            },
        ],
    }
    findings = extract_htmlcs_findings(cd, "1.1.1")
    assert len(findings) == 1
    assert findings[0].severity == Severity.HIGH
    assert findings[0].source == "htmlcs"
    assert findings[0].css_selector == "img.hero"


def test_htmlcs_warning_maps_to_medium():
    cd = CaptureData(url="https://t/")
    cd.htmlcs_results = {
        "messages": [
            {
                "type": 2,  # WARNING
                "code": "WCAG2AA.Principle1.Guideline1_3.1_3_1.H42",
                "msg": "Heading may be inappropriate",
                "selector": "h2.subtitle",
                "tag_name": "h2",
            },
        ],
    }
    findings = extract_htmlcs_findings(cd, "1.3.1")
    assert len(findings) == 1
    assert findings[0].severity == Severity.MEDIUM


def test_htmlcs_notice_maps_to_info():
    cd = CaptureData(url="https://t/")
    cd.htmlcs_results = {
        "messages": [
            {
                "type": 3,  # NOTICE
                "code": "WCAG2AAA.Principle3.Guideline3_1.3_1_3.H40",
                "msg": "Manual check needed",
                "selector": "abbr",
                "tag_name": "abbr",
            },
        ],
    }
    findings = extract_htmlcs_findings(cd, "3.1.3")
    assert len(findings) == 1
    assert findings[0].severity == Severity.INFO


# ── HTMLCS: SC filtering ───────────────────────────────────────────────


def test_htmlcs_filters_by_criterion():
    """A 1.3.1 message must NOT surface on a 1.1.1 check."""
    cd = CaptureData(url="https://t/")
    cd.htmlcs_results = {
        "messages": [
            {
                "type": 1,
                "code": "WCAG2AA.Principle1.Guideline1_3.1_3_1.H42",
                "msg": "1.3.1 issue",
                "selector": "h2",
                "tag_name": "h2",
            },
            {
                "type": 1,
                "code": "WCAG2AA.Principle1.Guideline1_1.1_1_1.H37",
                "msg": "1.1.1 issue",
                "selector": "img",
                "tag_name": "img",
            },
        ],
    }
    f111 = extract_htmlcs_findings(cd, "1.1.1")
    f131 = extract_htmlcs_findings(cd, "1.3.1")
    assert len(f111) == 1 and "1.1.1" in f111[0].issue
    assert len(f131) == 1 and "1.3.1" in f131[0].issue


def test_htmlcs_no_capture_returns_empty():
    """Soft-no-op when capture step didn't run."""
    cd = CaptureData(url="https://t/")
    # htmlcs_results stays None
    assert extract_htmlcs_findings(cd, "1.1.1") == []


def test_htmlcs_capture_error_returns_empty():
    """Soft-no-op when capture step set an error stub."""
    cd = CaptureData(url="https://t/")
    cd.htmlcs_results = {"error": "CDN blocked", "messages": []}
    assert extract_htmlcs_findings(cd, "1.1.1") == []


# ── IBM EAC: judgment parsing ──────────────────────────────────────────


def test_judgment_of_fail():
    assert _judgment_of({"value": ["VIOLATION", "FAIL"]}) == "FAIL"


def test_judgment_of_potential():
    assert _judgment_of({"value": ["VIOLATION", "POTENTIAL"]}) == "POTENTIAL"


def test_judgment_of_manual():
    assert _judgment_of({"value": ["RECOMMENDATION", "MANUAL"]}) == "MANUAL"


def test_judgment_of_missing_returns_empty():
    assert _judgment_of({}) == ""
    assert _judgment_of({"value": []}) == ""


# ── IBM EAC: rule-to-SC mapping ─────────────────────────────────────────


def test_known_rule_id_maps_to_sc():
    """Curated rule-id mapping must work for the most common rules."""
    assert "1.1.1" in _scs_for_rule("img_alt_valid", "", "1.1.1")
    assert "4.1.2" in _scs_for_rule("aria_role_valid", "", "4.1.2")
    assert "4.1.1" in _scs_for_rule("element_id_unique", "", "4.1.1")


def test_rule_can_attach_to_multiple_scs():
    """Some rules legitimately implicate multiple SCs (e.g. missing
    form label is 1.3.1 AND 3.3.2 AND 4.1.2).
    """
    scs = IBM_RULE_TO_SC["input_label_exists"]
    assert "1.3.1" in scs
    assert "3.3.2" in scs
    assert "4.1.2" in scs


def test_unknown_rule_with_sc_in_message_attaches_to_sc():
    """Defensive fallback: a new IBM rule not in the table still
    attaches to a finding when its message text mentions the SC.
    """
    scs = _scs_for_rule(
        "newly_added_rule_2026",
        "This element fails WCAG 1.4.3 contrast minimum.",
        "1.4.3",
    )
    assert scs == ["1.4.3"]


def test_unknown_rule_without_sc_match_returns_empty():
    """A rule we don't recognize and whose message doesn't mention the
    SC under test must NOT be guessed -- attribution would be wrong.
    """
    scs = _scs_for_rule("mystery_rule", "Generic message", "1.1.1")
    assert scs == []


# ── IBM EAC: severity mapping ──────────────────────────────────────────


def test_ibm_fail_maps_to_high():
    cd = CaptureData(url="https://t/")
    cd.ibm_eac_results = {
        "results": [
            {
                "ruleId": "img_alt_valid",
                "value": ["VIOLATION", "FAIL"],
                "path_dom": "img.hero",
                "path_aria": "",
                "message": "Image missing alt text",
                "snippet": "<img src='/h.jpg'>",
                "category": "Accessibility",
                "help": "Provide alt text",
            },
        ],
    }
    findings = extract_ibm_eac_findings(cd, "1.1.1")
    assert len(findings) == 1
    assert findings[0].severity == Severity.HIGH
    assert findings[0].source == "ibm_eac"
    assert findings[0].css_selector == "img.hero"


def test_ibm_potential_maps_to_medium():
    cd = CaptureData(url="https://t/")
    cd.ibm_eac_results = {
        "results": [
            {
                "ruleId": "text_contrast_sufficient",
                "value": ["VIOLATION", "POTENTIAL"],
                "path_dom": "p.lead",
                "message": "Contrast may be insufficient",
                "help": "Verify ratio",
            },
        ],
    }
    findings = extract_ibm_eac_findings(cd, "1.4.3")
    assert len(findings) == 1
    assert findings[0].severity == Severity.MEDIUM


def test_ibm_manual_maps_to_info():
    cd = CaptureData(url="https://t/")
    cd.ibm_eac_results = {
        "results": [
            {
                "ruleId": "media_alt_exists",
                "value": ["RECOMMENDATION", "MANUAL"],
                "path_dom": "video",
                "message": "Verify media has caption track",
            },
        ],
    }
    findings = extract_ibm_eac_findings(cd, "1.2.1")
    assert len(findings) == 1
    assert findings[0].severity == Severity.INFO


def test_ibm_pass_skipped():
    """PASS results are evidence the rule did NOT find an issue --
    they must NOT become findings.
    """
    cd = CaptureData(url="https://t/")
    cd.ibm_eac_results = {
        "results": [
            {
                "ruleId": "img_alt_valid",
                "value": ["VIOLATION", "PASS"],
                "path_dom": "img.hero",
                "message": "Image has valid alt",
            },
        ],
    }
    findings = extract_ibm_eac_findings(cd, "1.1.1")
    assert findings == []


def test_ibm_no_capture_returns_empty():
    cd = CaptureData(url="https://t/")
    assert extract_ibm_eac_findings(cd, "1.1.1") == []


# ── End-to-end: source tag matches the validator's expectations ────────


def test_source_tags_are_recognized_by_validator():
    """The new tags MUST appear in NON_JUDGE_SOURCE_TAGS in
    functions/parser.py. If they don't, the validator will demote
    every htmlcs/ibm_eac finding the judge consolidates.
    """
    from functions.parser import NON_JUDGE_SOURCE_TAGS
    assert "htmlcs" in NON_JUDGE_SOURCE_TAGS
    assert "ibm_eac" in NON_JUDGE_SOURCE_TAGS


def test_source_tags_in_judge_tool_schema_enum():
    """The JUDGE_TOOL.source enum gates what the model is *allowed* to
    emit. If htmlcs / ibm_eac aren't in the enum, the model can't
    legally tag a finding with them -- forcing the validator to demote.
    """
    from functions.tools import JUDGE_TOOL
    enum = (
        JUDGE_TOOL["function"]["parameters"]["properties"]
        ["final_findings"]["items"]["properties"]["source"]["enum"]
    )
    assert "htmlcs" in enum
    assert "ibm_eac" in enum


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
