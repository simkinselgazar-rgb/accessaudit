"""Regression tests for the six bugs found auditing the loudoun.gov
WCAG 2.2 AAA run 20260518_190643_88c447ff.

Each test pins one verified failure so it cannot silently return:

  Bug 1  Media-applicability gate ignored iframe-embedded video, so 7
         media SCs (1.2.x) were auto-marked Not Applicable and the
         judge never ran. The repd.us video iframe is the canonical
         miss — it is not a hardcoded video host.
  Bug 3  validate_source_attribution demoted a real finding to
         judge_inference when the judge labeled it with the wrong
         deterministic source; it must retag to the true source.
  Bug 4  Findings claiming the page has no <h1> when a level-1 heading
         exists must be dropped (contradicted by capture_data.headings).
  Bug 5  SC 1.4.5 image-of-text findings on elements that render real
         DOM text over a background image must be dropped.
  Bug 6  Findings claiming position:fixed/sticky must be dropped when
         the computed-style scan found zero fixed/sticky elements.

Run with:
    python tests/test_bug_fixes_20260518.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from models import CaptureData, Finding, Severity  # noqa: E402
from checks.checks_1_2 import (  # noqa: E402
    _iframe_is_media_candidate,
    _page_has_media_iframe,
    Check_1_2_1,
)
from checks.checks_1_2_aaa import Check_1_2_6  # noqa: E402
from checks.checks_1_3 import Check_1_3_1  # noqa: E402
from checks.checks_1_4 import Check_1_4_5  # noqa: E402
from functions.parser import validate_source_attribution  # noqa: E402


def _f(issue: str, *, element: str = "e", impact: str = "i",
       css_selector: str = "", source: str = "programmatic") -> Finding:
    return Finding(
        id="t", element=element, issue=issue, impact=impact,
        recommendation="r", severity=Severity.MEDIUM, source=source,
        css_selector=css_selector,
    )


# ── Bug 1: media gate must see iframe-embedded video ────────────────────


def test_repd_style_iframe_detected_by_title():
    """The verified miss: a video iframe on a non-hardcoded host whose
    title carries a media word."""
    iframe = {
        "src": "https://embed.repd.us/loudoun-county?clientId=292",
        "title": "Repd Video Guide Embed for Loudoun County",
    }
    assert _iframe_is_media_candidate(iframe) is True


def test_iframe_detected_by_allow_attribute():
    """A player's `allow` attribute grants media-playback permissions."""
    iframe = {
        "src": "https://some-unknown-host.example/embed/x",
        "title": "",
        "allow": "autoplay; encrypted-media; picture-in-picture",
    }
    assert _iframe_is_media_candidate(iframe) is True


def test_iframe_detected_by_allowfullscreen():
    iframe = {"src": "https://x.example/e", "allowfullscreen": "true"}
    assert _iframe_is_media_candidate(iframe) is True


def test_iframe_detected_by_known_domain():
    iframe = {"src": "https://www.youtube.com/embed/abcdefghijk"}
    assert _iframe_is_media_candidate(iframe) is True


def test_non_media_iframe_not_detected():
    """A map / form embed with no media signal must NOT be flagged."""
    for iframe in (
        {"src": "https://www.google.com/maps/embed?pb=x", "title": "Map"},
        {"src": "https://forms.example/survey", "title": "Document Viewer"},
        {"src": "https://x.example/voting", "title": "voting"},
    ):
        assert _iframe_is_media_candidate(iframe) is False, iframe


def test_page_has_media_iframe():
    cd = CaptureData()
    cd.iframes = [
        {"src": "https://x.example/maps", "title": "Map"},
        {"src": "https://embed.repd.us/x", "title": "Repd Video Guide"},
    ]
    assert _page_has_media_iframe(cd) is True
    cd2 = CaptureData()
    cd2.iframes = [{"src": "https://x.example/maps", "title": "Map"}]
    assert _page_has_media_iframe(cd2) is False


def test_media_scs_applicable_with_video_iframe():
    """The end-to-end fix: with a video iframe present (and media[]
    empty, as on loudoun.gov), the media SCs must report applicable so
    the judge runs — A-level 1.2.1 and AAA-level 1.2.6 alike."""
    cd = CaptureData()
    cd.media = []
    cd.iframes = [{
        "src": "https://embed.repd.us/loudoun-county",
        "title": "Repd Video Guide Embed for Loudoun County",
    }]
    assert Check_1_2_1().is_applicable(cd) is True
    assert Check_1_2_6().is_applicable(cd) is True


def test_media_scs_not_applicable_without_any_media():
    """No media and no media-ish iframe -> still correctly Not
    Applicable (the fix must not make every page run media SCs)."""
    cd = CaptureData()
    cd.media = []
    cd.iframes = [{"src": "https://x.example/maps", "title": "Map"}]
    assert Check_1_2_1().is_applicable(cd) is False
    assert Check_1_2_6().is_applicable(cd) is False


# ── Bug 3: mislabeled source is retagged, not demoted ───────────────────


def test_source_retagged_to_true_source_not_demoted():
    """A judge finding labeled 'programmatic' that actually matches an
    'andi' input finding must be retagged 'andi', not demoted to
    judge_inference (verified SC 1.4.3 data loss)."""
    judge = [{
        "css_selector": "#date-26", "element": "calendar date 26",
        "issue": "contrast ratio 2.2:1 on the calendar date",
        "impact": "low vision users cannot read it", "source": "programmatic",
    }]
    inputs = [{
        "css_selector": "#date-26", "element": "calendar date 26",
        "issue": "contrast ratio 2.2:1 on the calendar date",
        "source": "andi",
    }]
    out, flips = validate_source_attribution(judge, inputs)
    assert flips == 1, "the source label did change"
    assert out[0]["source"] == "andi", (
        f"expected retag to andi, got {out[0]['source']!r}"
    )
    assert "judge_inference" not in out[0]["source"]


def test_source_demoted_when_no_input_matches_any_source():
    """A judge finding that matches no input finding from any source
    is still honestly demoted to judge_inference."""
    judge = [{
        "css_selector": "#nowhere", "element": "invented element",
        "issue": "a problem no deterministic source reported",
        "source": "programmatic",
    }]
    inputs = [{
        "css_selector": "#other", "element": "unrelated",
        "issue": "completely different issue text", "source": "andi",
    }]
    out, flips = validate_source_attribution(judge, inputs)
    assert flips == 1
    assert out[0]["source"] == "judge_inference"


# ── Bug 4 / 5 / 6: ground-truth contradiction filter ────────────────────


def test_bug6_position_fixed_dropped_when_scan_found_none():
    cd = CaptureData()
    cd.positioned_elements = []  # computed-style scan: 0 fixed/sticky
    findings = [
        _f("This element has position: fixed which breaks reflow",
           css_selector="#divAjaxProgress"),
        _f("Real reflow issue: content overflows at 320px",
           css_selector="#newsCarousel"),
    ]
    kept = Check_1_3_1()._filter_findings_contradicted_by_capture(findings, cd)
    issues = [k.issue for k in kept]
    assert len(kept) == 1, issues
    assert "320px" in issues[0]


def test_bug6_position_fixed_kept_when_fixed_elements_exist():
    cd = CaptureData()
    cd.positioned_elements = [{"selector": "#realFixedBar", "position": "fixed"}]
    findings = [_f("Header uses position: fixed and obscures content",
                   css_selector="#realFixedBar")]
    kept = Check_1_3_1()._filter_findings_contradicted_by_capture(findings, cd)
    assert len(kept) == 1, "must not drop when the page truly has fixed elements"


def test_bug4_no_h1_finding_dropped_when_h1_exists():
    cd = CaptureData()
    cd.headings = [
        {"tag": "span", "level": 1, "text": "Loudoun County Virginia"},
        {"tag": "h2", "level": 2, "text": "News"},
    ]
    findings = [_f("The page contains headings but lacks an <h1> element")]
    kept = Check_1_3_1()._filter_findings_contradicted_by_capture(findings, cd)
    assert kept == [], "a 'no h1' finding must be dropped when an h1 exists"


def test_bug4_no_h1_finding_kept_when_truly_no_h1():
    cd = CaptureData()
    cd.headings = [{"tag": "h2", "level": 2, "text": "News"}]
    findings = [_f("The page lacks an <h1> element")]
    kept = Check_1_3_1()._filter_findings_contradicted_by_capture(findings, cd)
    assert len(kept) == 1, "a genuine 'no h1' finding must survive"


def test_bug5_image_of_text_dropped_for_real_dom_text():
    """SC 1.4.5: a nav button rendering real HTML text over a background
    image is not an image of text."""
    cd = CaptureData()
    cd.background_images = [
        {"selector": "a", "text_content": "ANIMAL SERVICES"},
        {"selector": "span", "text_content": "JOBS"},
    ]
    findings = [
        _f("The 'ANIMAL SERVICES' button uses a background image with text",
           element="'ANIMAL SERVICES' button"),
        _f("The 'JOBS' button uses an image of text instead of actual text",
           element="The 'JOBS' button in the main navigation area"),
    ]
    kept = Check_1_4_5()._filter_findings_contradicted_by_capture(findings, cd)
    assert kept == [], "both image-of-text findings contradict real DOM text"


def test_bug5_rule7_only_applies_to_1_4_5():
    """The image-of-text rule is SC 1.4.5-scoped — it must not strip a
    finding on a different SC just because the text matches."""
    cd = CaptureData()
    cd.background_images = [{"selector": "a", "text_content": "ANIMAL SERVICES"}]
    findings = [_f("The 'ANIMAL SERVICES' link has a contrast problem",
                   element="'ANIMAL SERVICES' button")]
    kept = Check_1_3_1()._filter_findings_contradicted_by_capture(findings, cd)
    assert len(kept) == 1, "Rule 7 must not fire outside SC 1.4.5"


def test_bug5_image_of_text_kept_for_genuine_image():
    """A genuine image-of-text element (no real DOM text) is not in
    background_images with text_content -> finding survives."""
    cd = CaptureData()
    cd.background_images = []
    findings = [_f("The banner renders its text as a rasterized image",
                   element="hero banner graphic")]
    kept = Check_1_4_5()._filter_findings_contradicted_by_capture(findings, cd)
    assert len(kept) == 1


# ── Bug 8: a crashed widget-keyboard probe is inconclusive ──────────────


def test_bug8_crashed_probe_detected_by_key_results():
    """The verified failure shape: keys_tested empty, every key_result
    flagged error, any_key_responded false."""
    from functions.keyboard_probe import widget_probe_errored
    entry = {
        "type": "tablist", "selector": "#tabs", "keys_tested": [],
        "key_results": [
            {"key": "ArrowRight", "error": True, "responded": False},
            {"key": "ArrowLeft", "error": True, "responded": False},
        ],
        "any_key_responded": False,
    }
    assert widget_probe_errored(entry) is True


def test_bug8_top_level_error_detected():
    from functions.keyboard_probe import widget_probe_errored
    assert widget_probe_errored({"selector": "#w", "error": "stale handle"}) is True


def test_bug8_successful_probe_not_errored():
    """A genuine probe where a key responded must NOT be flagged errored."""
    from functions.keyboard_probe import widget_probe_errored
    entry = {
        "type": "accordion", "selector": "#acc", "keys_tested": ["Enter"],
        "key_results": [{"key": "Enter", "responded": True, "error": ""}],
        "any_key_responded": True,
    }
    assert widget_probe_errored(entry) is False


def test_bug8_genuine_no_response_not_errored():
    """A widget that was really tested and really ignored its keys is a
    genuine failure — it must NOT be masked as an errored probe."""
    from functions.keyboard_probe import widget_probe_errored
    entry = {
        "type": "menu", "selector": "#m", "keys_tested": ["ArrowDown"],
        "key_results": [{"key": "ArrowDown", "responded": False, "error": False}],
        "any_key_responded": False,
    }
    assert widget_probe_errored(entry) is False


# ── Bug 10: a can_exit trap is not a 2.1.2 failure ──────────────────────


def test_bug10_can_exit_trap_not_a_failing_finding():
    """SC 2.1.2: a trap entry whose capture description says it passes
    2.1.2 (can_exit true) must not yield a failing-severity finding."""
    import asyncio
    from checks.checks_2_1 import Check_2_1_2
    cd = CaptureData()
    cd.keyboard_traps = [{
        "type": "non_standard_exit",
        "selector": "#mainNavGovernment",
        "can_exit": True,
        "exit_instructions": "",
        "description": ("Dropdown '#mainNavGovernment' did not close on "
                        "Escape; focus escaped via Tab after 5 press(es). "
                        "Passes 2.1.2 but the non-standard exit path is "
                        "not advertised to the user."),
    }]
    _conf, _confidence, findings = asyncio.run(
        Check_2_1_2().run_programmatic(cd)
    )
    bad = [f for f in findings
           if f.severity in (Severity.HIGH, Severity.MEDIUM)]
    assert not bad, (
        "a can_exit trap that the capture says passes 2.1.2 must not "
        f"produce a failing finding: {[f.issue for f in bad]}"
    )


def test_bug10_genuine_cycle_trap_still_flagged():
    """The fix must not suppress real traps — a cycle trap is still HIGH."""
    import asyncio
    from checks.checks_2_1 import Check_2_1_2
    cd = CaptureData()
    cd.keyboard_traps = [{
        "type": "cycle",
        "selectors": ["#modal", "#overlay"],
        "description": "Focus cycles between #modal and #overlay",
    }]
    _conf, _confidence, findings = asyncio.run(
        Check_2_1_2().run_programmatic(cd)
    )
    assert any(f.severity == Severity.HIGH for f in findings), (
        "a genuine cycle trap must still be flagged HIGH"
    )


# ── Bug 11: MEDIA evidence block must be complete ───────────────────────


def test_bug11_media_block_classifies_silent_video_only():
    """The MEDIA block must tell the judge a muted video with no audio
    track is VIDEO-ONLY (so it does not demand captions/audio)."""
    from checks.checks_1_2 import Check_1_2_1
    cd = CaptureData()
    cd.html = "<html><body><video muted></video></body></html>"
    cd.media = [{
        "tag": "video", "muted": True, "tracks": [], "autoplay": True,
        "controls": False, "selector": "header > video",
    }]
    cd.audio_detection = {"audio_type": "silence", "has_autoplay_audio": False}
    ctx = Check_1_2_1()._build_dom_context(cd)
    assert "VIDEO-ONLY" in ctx, (
        "MEDIA block must classify a muted/silent video as video-only"
    )


def test_bug11_media_block_surfaces_text_alternative_candidate():
    """An in-page 'video description' mechanism must be surfaced so the
    judge does not wrongly conclude no text alternative exists."""
    from checks.checks_1_2 import Check_1_2_1
    cd = CaptureData()
    cd.html = "<html><body><video muted></video></body></html>"
    cd.media = [{"tag": "video", "muted": True, "tracks": [],
                 "selector": "header > video"}]
    cd.audio_detection = {"audio_type": "silence"}
    cd.element_inventory = [
        {"text": "View Video Description", "selector": "#btn-desc"},
    ]
    ctx = Check_1_2_1()._build_dom_context(cd)
    assert "View Video Description" in ctx, (
        "MEDIA block must surface the in-page text-alternative candidate"
    )


# ── Cluster C: browser-handled hidden elements ──────────────────────────


def test_clusterC_browser_handled_finding_dropped():
    """A keyboard/focus finding on a tab_reachable=False element must be
    dropped — the focus leak it describes does not exist."""
    from checks.checks_2_1 import Check_2_1_1
    cd = CaptureData()
    cd.html = "<html><body><div id='modal-x'></div></body></html>"
    cd.andi_hidden_results = [
        {"selector": "#modal-x", "tab_reachable": False,
         "hidden_reasons": ["display:none"]},
    ]
    findings = [_f("Focusable element #modal-x is a focus leak",
                   css_selector="#modal-x")]
    kept = Check_2_1_1()._filter_findings_contradicted_by_capture(findings, cd)
    assert kept == [], "finding on a browser-handled element must be dropped"


def test_clusterC_reachable_element_finding_kept():
    """A finding on a genuinely tab-reachable element must survive."""
    from checks.checks_2_1 import Check_2_1_1
    cd = CaptureData()
    cd.html = "<html><body><a id='real'>x</a></body></html>"
    cd.andi_hidden_results = [
        {"selector": "#skiplink", "tab_reachable": True,
         "hidden_reasons": ["display:none"]},
    ]
    findings = [_f("Real keyboard issue on #real", css_selector="#real")]
    kept = Check_2_1_1()._filter_findings_contradicted_by_capture(findings, cd)
    assert len(kept) == 1


# ── Cluster A: 'no accessible name' on a named element ──────────────────


def test_clusterA_no_name_finding_dropped_when_inventory_has_name():
    """A 'no accessible name' finding on a button that element_inventory
    records with a real name (e.g. from an sr-only span) is dropped."""
    from checks.checks_4_1 import Check_4_1_2
    cd = CaptureData()
    cd.html = "<html><body><button id='mt'></button></body></html>"
    cd.element_inventory = [
        {"selector": "#mt", "text": "Toggle navigation"},
    ]
    findings = [_f("Button #mt has no accessible name", css_selector="#mt")]
    kept = Check_4_1_2()._filter_findings_contradicted_by_capture(findings, cd)
    assert kept == [], "must drop a no-name finding on a named element"


# ── Cluster D: SC 2.5.5 target-size block uses 44px, no spacing ─────────


def test_clusterD_target_size_block_enhanced_uses_44px():
    from checks.checks_2_5_aaa import Check_2_5_5
    cd = CaptureData()
    cd.target_size_measurements = [
        {"selector": "#btn", "kind": "button", "name": "Go",
         "width": 30.0, "height": 30.0, "cx": 100.0, "cy": 100.0,
         "is_inline": False},
    ]
    block = "\n".join(Check_2_5_5()._format_target_size_measurements(cd))
    assert "2.5.5" in block and "44x44" in block, "must use the 44px threshold"
    assert "NO spacing exception" in block, (
        "2.5.5 block must state it has no spacing exception"
    )


# ── Cluster E: TAB COVERAGE inversion ───────────────────────────────────


def test_clusterE_unreachable_finding_dropped_when_coverage_high():
    """A 2.1.1 'not reachable via the keyboard' finding is dropped when
    captured tab-coverage is high and the element is not in the
    deterministic focusable_but_skipped list."""
    from checks.checks_2_1 import Check_2_1_1
    cd = CaptureData()
    cd.html = "<html><body><a id='nav1'>x</a></body></html>"
    cd.tab_coverage = {
        "total_interactive": 72, "reached_by_tab": 71,
        "coverage_percent": 98.6,
        "focusable_but_skipped": [{"selector": "header > video"}],
    }
    findings = [_f("Element #nav1 is not reachable via the keyboard",
                   css_selector="#nav1")]
    kept = Check_2_1_1()._filter_findings_contradicted_by_capture(findings, cd)
    assert kept == [], "must drop unreachability finding contradicting coverage"


def test_clusterE_genuinely_skipped_element_finding_kept():
    """A finding about an element that IS in focusable_but_skipped must
    survive."""
    from checks.checks_2_1 import Check_2_1_1
    cd = CaptureData()
    cd.html = "<html><body><video id='v'></video></body></html>"
    cd.tab_coverage = {
        "coverage_percent": 98.6,
        "focusable_but_skipped": [{"selector": "#v"}],
    }
    findings = [_f("Element #v is not reachable via the keyboard",
                   css_selector="#v")]
    kept = Check_2_1_1()._filter_findings_contradicted_by_capture(findings, cd)
    assert len(kept) == 1, "a genuinely-skipped element finding must survive"


# ── Cluster G: criterion-misapplication filters ─────────────────────────


def test_clusterG_focus_obscured_dropped_when_no_fixed_elements():
    from checks.checks_2_4_22 import Check_2_4_11
    cd = CaptureData()
    cd.html = "<html><body><a id='x'>x</a></body></html>"
    cd.positioned_elements = []  # no fixed/sticky elements
    findings = [_f("Focused element may be obscured by a sticky header",
                   css_selector="#x")]
    kept = Check_2_4_11()._filter_findings_contradicted_by_capture(findings, cd)
    assert kept == [], "focus cannot be obscured with zero fixed/sticky elements"


def test_clusterG_hover_finding_dropped_when_nothing_revealed():
    from checks.checks_1_4 import Check_1_4_13
    cd = CaptureData()
    cd.html = "<html><body><button id='m1'>x</button></body></html>"
    cd.hover_content = [{"selector": "#m1", "new_elements_count": 0}]
    findings = [_f("Hover content on #m1 is not dismissible",
                   css_selector="#m1")]
    kept = Check_1_4_13()._filter_findings_contradicted_by_capture(findings, cd)
    assert kept == [], "1.4.13 cannot fail on an element that reveals nothing"


def test_clusterG_label_in_name_dropped_for_sr_only_label():
    from checks.checks_2_5 import Check_2_5_3
    cd = CaptureData()
    cd.html = "<html><body><label class='screen-reader-text'>x</label></body></html>"
    findings = [_f("Label-in-name mismatch on the search label",
                   css_selector="label.screen-reader-text")]
    kept = Check_2_5_3()._filter_findings_contradicted_by_capture(findings, cd)
    assert kept == [], "2.5.3 does not apply to a screen-reader-only label"


# ── Cluster F: htmlcs advisory notices ──────────────────────────────────


def test_clusterF_type3_notice_marked_advisory():
    """An HTML_CodeSniffer type-3 notice must be labelled as an advisory
    manual-check reminder, not a detected violation."""
    from functions.htmlcs_extract import extract_htmlcs_findings
    cd = CaptureData()
    cd.htmlcs_results = {"messages": [{
        "type": 3, "code": "WCAG2AAA.Principle4.Guideline4_1.4_1_3.G199",
        "msg": "Check that status messages can be programmatically determined.",
        "selector": "body", "tag_name": "body",
    }]}
    findings = extract_htmlcs_findings(cd, "4.1.3")
    assert findings, "a type-3 message still produces an (advisory) finding"
    adv = [f for f in findings if "ADVISORY" in f.issue]
    assert adv, "type-3 notice must carry the [ADVISORY ...] marker"
    assert adv[0].severity == Severity.INFO, "advisory notice stays INFO severity"


# ── Round 2 refinements ─────────────────────────────────────────────────


def test_round2_no_name_pattern_catches_lack_an_accessible_name():
    """Rule 1 broadened: 'lack an accessible name' must match (was missed)."""
    from checks.checks_4_1 import Check_4_1_2
    cd = CaptureData()
    cd.html = "<html><body><button id='mt'></button></body></html>"
    cd.element_inventory = [{"selector": "#mt", "text": "Toggle navigation"}]
    for issue in (
        "Button lacks an accessible name",
        "lacks accessible name",
        "has no accessible name",
        "without an accessible name",
        "Does not have an accessible name",
    ):
        kept = Check_4_1_2()._filter_findings_contradicted_by_capture(
            [_f(issue, css_selector="#mt")], cd,
        )
        assert kept == [], f"phrase not caught: {issue!r}"


def test_round2_skip_link_keyboard_activates_finding_dropped():
    """Rule 13: a finding calling a skip link 'non-functional' is dropped
    when skip_link_results shows keyboard_activates True."""
    from checks.checks_2_4 import Check_2_4_1
    cd = CaptureData()
    cd.html = "<html><body><a id='content'>x</a></body></html>"
    cd.skip_link_results = [{
        "skip_link_selector": 'a[href="#content"]',
        "target_href": "#content",
        "keyboard_activates": True,
        "focus_landed_on_target": True,
    }]
    findings = [_f("This skip link is non-functional",
                   css_selector='a[href="#content"]')]
    kept = Check_2_4_1()._filter_findings_contradicted_by_capture(findings, cd)
    assert kept == []


def test_round2_focus_contrast_well_measured_dropped():
    """Rule 14: drop 'no focus indicator' findings on elements the
    focus_contrast probe measured with adequate contrast."""
    from checks.checks_2_4 import Check_2_4_7
    cd = CaptureData()
    cd.html = "<html><body><a id='x'>x</a></body></html>"
    cd.focus_contrast = [
        {"selector": "#x", "has_change": True, "contrast_ratio": 13.65},
    ]
    findings = [_f("Element #x has no visible focus indicator",
                   css_selector="#x")]
    kept = Check_2_4_7()._filter_findings_contradicted_by_capture(findings, cd)
    assert kept == []


def test_round2_no_auto_modal_finding_dropped():
    """Rule 15: drop 'auto-opening modal' findings when modal_interactions
    captured nothing."""
    from checks.checks_2_2_aaa import Check_2_2_4
    cd = CaptureData()
    cd.html = "<html><body></body></html>"
    cd.modal_interactions = []
    findings = [_f("A modal appears automatically upon page load",
                   css_selector="body")]
    kept = Check_2_2_4()._filter_findings_contradicted_by_capture(findings, cd)
    assert kept == []


def test_round2_ibm_potential_marked_advisory():
    """IBM EAC POTENTIAL judgments must carry the [ADVISORY ...] marker."""
    from functions.ibm_eac_extract import extract_ibm_eac_findings
    cd = CaptureData()
    cd.ibm_eac_results = {"results": [{
        "ruleId": "input_label_visible",
        "value": ["VIOLATION", "potential"],
        "message": "Confirm visible label is associated with input",
        "path": {"dom": "/html/body/input"},
        "category": "Accessibility",
    }]}
    findings = extract_ibm_eac_findings(cd, "3.3.2")
    if findings:
        assert any("ADVISORY" in f.issue for f in findings), (
            "IBM POTENTIAL must be advisory-tagged"
        )


def test_round2_loading_attribute_doesnt_trigger_status_finding():
    """The 4.1.3 status-message heuristic must not match HTML attribute
    values — a `<img loading="eager">` is a browser hint, not a status
    indicator."""
    import re as _re
    html = '<html><body><img src="x" loading="eager"></body></html>'
    html_lower = html.lower()
    # Mirror the fixed stripping logic
    html_text_only = _re.sub(
        r'\s+[\w-]+\s*=\s*"[^"]*"', '', html_lower,
    )
    html_text_only = _re.sub(
        r"\s+[\w-]+\s*=\s*'[^']*'", '', html_text_only,
    )
    assert "loading" not in html_text_only, (
        "loading attribute value must be stripped before heuristic match"
    )


# ── 2026-05-21 audit fixes ──────────────────────────────────────────────


def test_audit_save_capture_data_atomic_writes_and_reloads():
    """Gap 1: save_capture_data writes capture_data.json atomically so a
    crash mid-write can't leave a half-flushed file. The symmetric
    reload_capture_data must read it back."""
    import tempfile
    import os as _os
    from capture.v2.state import save_capture_data, reload_capture_data
    with tempfile.TemporaryDirectory() as td:
        review_dir = _os.path.join(td, "review")
        captures_dir = _os.path.join(review_dir, "captures")
        _os.makedirs(captures_dir, exist_ok=True)
        cd = CaptureData()
        cd.url = "https://example.com/"
        cd.title = "Example"
        cd.html = "<html><body><h1>Hello</h1></body></html>"
        cd.headings = [{"tag": "h1", "level": 1, "text": "Hello",
                         "selector": "h1"}]
        ok = save_capture_data(cd, captures_dir, after_label="unit-test")
        assert ok, "save must succeed on a writable dir"
        cd_path = _os.path.join(captures_dir, "capture_data.json")
        assert _os.path.exists(cd_path), "capture_data.json must exist"
        # No temp file left behind
        assert not _os.path.exists(cd_path + ".tmp")
        # Round-trip via reload
        reloaded = reload_capture_data(review_dir)
        assert reloaded.title == "Example"
        assert reloaded.headings and reloaded.headings[0]["text"] == "Hello"


def test_audit_v2_heading_accname_recompute_nested_img_alt():
    """Task 28: the v2 inventory mapper must compute the heading
    accessible name when the AI inventory's textContent was empty but
    the heading contains a nested <img alt>. Mirrors v1's __headingName JS."""
    from capture.v2.element_inventory import _compute_heading_accname
    html = (
        '<html><body>'
        '<h1 id="logo-heading">'
        '<a class="logo" href="/"><img src="x.svg" alt="Site name"></a>'
        '</h1>'
        '</body></html>'
    )
    assert _compute_heading_accname("h1", html) == "Site name"
    assert _compute_heading_accname("#logo-heading", html) == "Site name"
    # aria-label wins over content
    html2 = '<h1 aria-label="Custom"><span>ignored</span></h1>'
    assert _compute_heading_accname("h1", html2) == "Custom"
    # genuinely empty stays empty
    assert _compute_heading_accname("h1", "<h1></h1>") == ""
    # aria-labelledby resolves IDs
    html3 = '<div id="lbl">Page Heading</div><h2 aria-labelledby="lbl"></h2>'
    assert _compute_heading_accname("h2", html3) == "Page Heading"


def test_audit_numeric_re_single_anchored_definition():
    """Task 29: _NUMERIC_RE must be defined exactly once and anchored so
    .match() doesn't accept '12abc' as numeric."""
    import functions.parser as _p
    src = open(_p.__file__, encoding="utf-8").read()
    n_defs = src.count("_NUMERIC_RE = re.compile(")
    assert n_defs == 1, f"expected one _NUMERIC_RE definition, found {n_defs}"
    # The anchored regex must reject a leading-digit-with-trailing-junk.
    assert _p._NUMERIC_RE.match("12abc") is None
    assert _p._NUMERIC_RE.match("12") is not None
    assert _p._NUMERIC_RE.match("-3.14e+2") is not None
    assert _p._NUMERIC_RE.fullmatch("3.14") is not None


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
