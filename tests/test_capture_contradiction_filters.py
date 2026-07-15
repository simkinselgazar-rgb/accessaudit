"""Regression for the server-side false-positive filters in
BaseCheck._filter_findings_contradicted_by_capture.

Each rule kills a verified umich.edu 2026-05-28 false positive while leaving
genuine findings intact. The method only reads self.criterion_id, so we drive
it with a SimpleNamespace stub.
"""
from types import SimpleNamespace

from models import CaptureData, Finding, Severity
from checks.base import BaseCheck

_filter = BaseCheck._filter_findings_contradicted_by_capture


def _f(issue, source="judge_inference", css="", element="", sev=Severity.HIGH):
    return Finding(id="x", element=element or css, issue=issue, impact="i",
                   recommendation="r", severity=sev, source=source, css_selector=css)


def _capture(axe=None, hidden=None, links=None, html=None):
    cd = CaptureData()
    cd.axe_results = axe or {
        "passes": [
            {"id": "link-name", "nodes": [{"target": ["a"]}]},
            {"id": "button-name", "nodes": [{"target": ["button"]}]},
        ],
        "violations": [], "incomplete": [],
    }
    cd.andi_hidden_results = hidden or [{
        "selector": "ul#quick-links-content > li > a",
        "hidden_reasons": ["visibility:hidden"], "tab_reachable": True,
        "rect": {"width": 100, "height": 20},
    }]
    cd.links = links or []
    cd.html = html or ""
    return cd


def _run(cid, findings, cd=None):
    return _filter(SimpleNamespace(criterion_id=cid), findings, cd or _capture())


def test_logo_contrast_exemption_dropped():
    out = _run("1.4.6", [_f("logo text has insufficient contrast", "visual_ai",
                            "div#zone-branding > h1.logo > a", "U-M logo link")])
    assert out == []


def test_non_logo_contrast_finding_survives():
    out = _run("1.4.6", [_f("body paragraph text contrast 4.6:1", "andi",
                            "div#michigan-time > p")])
    assert len(out) == 1


def test_event_listener_fabrication_dropped():
    out = _run("2.1.1", [_f(
        "A mousedown event listener is registered without a corresponding "
        "keydown handler", "judge_inference", "")])
    assert out == []


def test_code_ai_keyboard_finding_not_dropped():
    # A grounded code_ai finding (from script_content) must survive.
    out = _run("2.1.1", [_f(
        "mousedown handler with no keyboard equivalent", "code_ai", "div.widget")])
    assert len(out) == 1


def test_andi_no_name_dropped_when_axe_link_name_clean():
    out = _run("4.1.2", [_f("Link has no accessible name", "andi",
                            "ul#infographics > li > a.infographic-two")])
    assert out == []


def test_real_no_name_survives_when_axe_has_violation():
    cd = _capture(axe={
        "passes": [{"id": "link-name", "nodes": [{"target": [".ok"]}]}],
        "violations": [{"id": "link-name", "nodes": [{"target": ["a.bad"]}]}],
        "incomplete": [],
    })
    out = _run("4.1.2", [_f("Link has no accessible name", "andi",
                            "a.some-unnamed-link")], cd)
    assert len(out) == 1


def test_browser_handled_visibility_hidden_dropped():
    out = _run("2.4.7", [_f("focus leak on visibility:hidden link", "andi",
                            "ul#quick-links-content > li > a")])
    assert out == []


# ── H30: same name + same destination needs no differentiation ──────────────

def _links_capture():
    return _capture(links=[
        {"text": "Learn more about the ceremony",
         "href": "https://record.umich.edu/articles/1-million-memories/"},
        {"text": "Learn more about the ceremony",
         "href": "https://record.umich.edu/articles/1-million-memories/"},
        {"text": "Learn more about this research",
         "href": "https://news.umich.edu/studying-bird-flu/"},
        {"text": "Learn more about this research",
         "href": "https://news.umich.edu/microplastics/"},
    ])


def test_same_name_same_destination_dropped_h30():
    cd = _links_capture()
    out = _run("2.4.9", [_f(
        "Multiple links have the same accessible name "
        "('Learn more about the ceremony') and fail to provide unique "
        "identification", "visual_ai", element="'Learn more about the ceremony' links")], cd)
    assert out == []


def test_same_name_different_destinations_survives():
    cd = _links_capture()
    out = _run("2.4.9", [_f(
        "Multiple links have the same accessible name ('Learn more about this "
        "research') but lead to different destinations", "visual_ai",
        element="'Learn more about this research' links")], cd)
    assert len(out) == 1


# ── display:none element flagged as an interactive control ──────────────────

def test_display_none_iframe_interactive_finding_dropped():
    html = ('<iframe id="ce_proto_iframe" title="CrazyEgg Tracking iframe" '
            'aria-hidden="true" style="display: none;"></iframe>')
    cd = _capture(html=html)
    out = _run("4.1.2", [_f(
        "This custom iframe element acts as an interactive control but lacks "
        "an ARIA role and an accessible name", "programmatic", "#ce_proto_iframe")], cd)
    assert out == []


def test_visible_element_interactive_finding_survives():
    html = '<div id="real-widget" role="button">Click</div>'
    cd = _capture(html=html)
    out = _run("4.1.2", [_f(
        "This element acts as an interactive control but lacks an accessible "
        "name", "andi", "#real-widget")], cd)
    assert len(out) == 1
