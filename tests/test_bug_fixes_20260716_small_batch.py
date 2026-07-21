"""Regression tests for the 2026-07-16 small-bug batch.

Covers the offline-testable fixes:
  1. IBM EAC SC-id fallback must be boundary-aware: "1.4.1" must not
     substring-match "WCAG 1.4.10"/"1.4.11" (reflow/spacing rules were
     polluting the SC 1.4.1 judge prompt; same for 2.4.1 vs 2.4.11).
  2. gANDI graphics findings: data-URI srcs are summarised to their
     header + payload size (no base64 in the judge prompt), while
     normal http(s) URLs are kept in full.
  3. shadow_dom extraction JS: accessible-name computation resolves
     aria-labelledby within the same root and falls back to text
     content for name-from-content roles; failed style reads mark
     visible as null (unknown), not true.
"""
from functions.ibm_eac_extract import _scs_for_rule
from functions.andi_extract import extract_andi_graphics_findings
from functions.shadow_dom import _EXTRACT_JS
from models import CaptureData


# ── Item 1: boundary-aware SC-id fallback ────────────────────────────────────

def test_scs_for_rule_rejects_dotted_prefix_1_4_10():
    assert _scs_for_rule("brand_new_rule", "Content fails WCAG 1.4.10 Reflow", "1.4.1") == []


def test_scs_for_rule_rejects_dotted_prefix_1_4_11():
    assert _scs_for_rule("brand_new_rule", "See 1.4.11 Non-text Contrast", "1.4.1") == []


def test_scs_for_rule_rejects_2_4_11_for_2_4_1():
    assert _scs_for_rule("brand_new_rule", "WCAG 2.4.11 Focus Not Obscured", "2.4.1") == []


def test_scs_for_rule_accepts_exact_id():
    assert _scs_for_rule("brand_new_rule", "Violates WCAG 1.4.1 Use of Color", "1.4.1") == ["1.4.1"]


def test_scs_for_rule_accepts_sentence_ending_period():
    assert _scs_for_rule(
        "brand_new_rule", "Fails Success Criterion 1.4.1.", "1.4.1",
    ) == ["1.4.1"]


def test_scs_for_rule_rejects_longer_dotted_id():
    assert _scs_for_rule("brand_new_rule", "internal id 1.4.1.2 mapping", "1.4.1") == []


def test_scs_for_rule_curated_table_still_wins():
    assert _scs_for_rule("style_color_misuse", "unrelated message", "1.4.1") == ["1.4.1"]


# ── Item 2: data-URI src summary in graphics findings ────────────────────────

def _graphics_capture(src: str) -> CaptureData:
    cd = CaptureData(url="https://example.edu/", title="Example")
    cd.andi_graphics_results = [{
        "selector": "div.hero",
        "type": "bg-image",
        "src": src,
        "has_text_overlay": True,
        "text_overlay_text": "Apply now",
    }]
    return cd


def test_graphics_finding_summarises_data_uri():
    payload = "A" * 100_000
    cd = _graphics_capture(f"data:image/png;base64,{payload}")
    findings = extract_andi_graphics_findings(cd, "1.4.5")
    assert findings, "bg-image + text overlay must produce a 1.4.5 finding"
    issue = findings[0].issue
    assert "data:image/png;base64,[100000 chars]" in issue
    assert payload[:100] not in issue  # no base64 body in the judge prompt


def test_graphics_finding_keeps_full_http_url():
    url = "https://cdn.example.edu/assets/banners/2026/fall/" + "x" * 200 + "/hero.png?w=1600&q=80"
    cd = _graphics_capture(url)
    findings = extract_andi_graphics_findings(cd, "1.4.5")
    assert findings
    assert url in findings[0].issue  # full URL, not clipped at 60 chars


# ── Item 3: shadow DOM extraction JS contract ────────────────────────────────

def test_shadow_js_resolves_aria_labelledby():
    assert "aria-labelledby" in _EXTRACT_JS
    assert "getRootNode" in _EXTRACT_JS
    assert "resolveLabelledby" in _EXTRACT_JS


def test_shadow_js_marks_unknown_visibility_as_null():
    # Failed style reads yield styles === null, and visible must then be
    # null (unknown) — never a hard-coded true.
    assert "return null;" in _EXTRACT_JS  # getComputedProps failure path
    visible_expr = _EXTRACT_JS.split("visible: styles", 1)[1][:220]
    assert ": null" in visible_expr
