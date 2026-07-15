"""Regression: ANDI 'no accessible name' findings must be cross-checked
against axe's name rules (link-name/button-name/image-alt). ANDI ignores
clip/sr-only label text; axe honours it.

Verified false positives (umich.edu 2026-05-28): ANDI flagged 4 named
nav/infographic links and a search submit button as 'no accessible name';
axe link-name had 67 passes / 0 violations and button-name 1 pass / 0
violations, so every such element IS named. axe_confirms_named must return
True for those (page-clean rule), and False when axe offers no corroboration.
"""
from functions.axe_extract import accessible_name_corroboration, axe_confirms_named


def _axe(passes=None, violations=None, incomplete=None):
    return {
        "passes": passes or [],
        "violations": violations or [],
        "incomplete": incomplete or [],
    }


def test_page_clean_link_rule_confirms_named_links():
    axe = _axe(passes=[{"id": "link-name", "nodes": [
        {"target": ["a[href$='about/']"]}, {"target": ["a[href$='research/']"]},
    ]}])
    s = accessible_name_corroboration(axe)
    # different selector dialect than axe's targets -> falls back to page-clean
    assert axe_confirms_named(s, "", "ul#infographics > li > a.infographic-two") is True
    assert axe_confirms_named(s, "", "div#main-nav > ul.clear > li > a") is True


def test_page_clean_button_rule_confirms_named_buttons():
    axe = _axe(passes=[{"id": "button-name", "nodes": [{"target": ["button"]}]}])
    s = accessible_name_corroboration(axe)
    assert axe_confirms_named(s, "", "div#zone-utility-bar > div.search > form > button") is True


def test_exact_selector_pass_confirms_named():
    axe = _axe(passes=[{"id": "link-name", "nodes": [{"target": [".special-link"]}]}],
               violations=[{"id": "link-name", "nodes": [{"target": ["a.broken"]}]}])
    s = accessible_name_corroboration(axe)
    # rule is NOT page-clean (has a violation), but exact target match wins
    assert axe_confirms_named(s, "", ".special-link") is True


def test_violation_present_does_not_confirm_unmatched_link():
    # If axe link-name has a violation, the rule is not page-clean, so an
    # unmatched link is NOT auto-confirmed (a real no-name finding survives).
    axe = _axe(passes=[{"id": "link-name", "nodes": [{"target": [".ok"]}]}],
               violations=[{"id": "link-name", "nodes": [{"target": ["a.bad"]}]}])
    s = accessible_name_corroboration(axe)
    assert axe_confirms_named(s, "", "a.some-other-link") is False


def test_no_axe_data_does_not_confirm():
    assert axe_confirms_named({}, "", "a.foo") is False
    assert axe_confirms_named(accessible_name_corroboration({}), "", "button") is False


def test_unknown_element_type_not_confirmed_by_page_clean_links():
    # A page-clean link rule must not confirm an <iframe> or <div>.
    axe = _axe(passes=[{"id": "link-name", "nodes": [{"target": ["a"]}]}])
    s = accessible_name_corroboration(axe)
    assert axe_confirms_named(s, "", "iframe#ce_proto_iframe") is False
