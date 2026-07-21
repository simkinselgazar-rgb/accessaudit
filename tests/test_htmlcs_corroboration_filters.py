"""Regression tests for HTMLCS rule corroboration filters.

Two HCS rules over-fire on real pages, producing the SC 1.3.1
false-positive cluster verified on a public accessibility-resource site run 20260511:

  - H48 "if this <p> contains navigation, mark it up as a list":
    fires on author-bio paragraphs and copyright lines that have
    inline links but are NOT navigation.

  - F92,ARIA4 "role=presentation contains semantic children":
    fires on SVG hero illustrations with only <path>/<circle>/<rect>
    primitives (no <title>, <desc>, <text>, or aria-bearing children).

Both fixes corroborate the HCS claim against the captured DOM before
the finding flows into the judge prompt. Same structural pattern as
the IBM EAC bg-image / focus-visible filters.

Run with:

    python tests/test_htmlcs_corroboration_filters.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from functions.htmlcs_extract import (  # noqa: E402
    _selector_class_inside_nav_ancestor,
    _svg_with_role_presentation_has_semantic_children,
    extract_htmlcs_findings,
)
from models import CaptureData, Severity  # noqa: E402


# ── Helper: class-inside-nav detection ─────────────────────────────────


def test_class_inside_nav_returns_true_when_class_in_nav():
    """The canonical positive case: <nav>...<p class='foo'> with class
    'foo' is inside a nav ancestor.
    """
    html = (
        "<html><body>"
        "<nav><ul><li><p class='nav-link'>Home</p></li></ul></nav>"
        "</body></html>"
    )
    assert _selector_class_inside_nav_ancestor("p.nav-link", html) is True


def test_class_inside_nav_returns_false_when_class_outside_nav():
    """The verified failure: a <p class='c-footer__copyright'>
    NOT inside any <nav> -- it's the copyright line in <footer>.
    """
    html = (
        "<html><body>"
        "<nav><a href='/'>Home</a></nav>"
        "<footer>"
        "  <p class='c-footer__copyright'>(c) 2024 by them</p>"
        "</footer>"
        "</body></html>"
    )
    assert _selector_class_inside_nav_ancestor(
        "p.c-footer__copyright", html
    ) is False


def test_class_inside_role_navigation_container():
    """role='navigation' on a non-<nav> element should also count as
    a navigation ancestor (the spec allows custom containers).
    """
    html = (
        "<html><body>"
        "<div role='navigation'><p class='link'>About</p></div>"
        "</body></html>"
    )
    assert _selector_class_inside_nav_ancestor("p.link", html) is True


def test_no_class_in_selector_returns_true_conservative():
    """When the selector has no class (e.g. tag-only or XPath-style),
    we can't class-match against nav regions. Keep the finding for
    the judge to evaluate -- don't suppress on missing data.
    """
    html = "<html><body><nav><p>x</p></nav></body></html>"
    assert _selector_class_inside_nav_ancestor("p", html) is True
    assert _selector_class_inside_nav_ancestor("", html) is True


def test_empty_html_returns_true_conservative():
    """No captured HTML -> can't verify; keep the finding."""
    assert _selector_class_inside_nav_ancestor("p.foo", "") is True
    assert _selector_class_inside_nav_ancestor("p.foo", None) is True


def test_class_with_dashes_and_underscores_handled():
    """Real CSS classes often have dashes / underscores (BEM style).
    The class extractor must handle them.
    """
    html = (
        "<html><body><nav>"
        "<p class='u-font-size-body-small'>x</p>"
        "</nav></body></html>"
    )
    assert _selector_class_inside_nav_ancestor(
        "p.u-font-size-body-small", html
    ) is True


def test_class_word_boundary_no_partial_match():
    """Class 'foo' must not match 'foo-bar' or 'bar-foo'."""
    html = "<html><body><nav><p class='foo-bar'>x</p></nav></body></html>"
    assert _selector_class_inside_nav_ancestor("p.foo", html) is False
    # but matches as direct
    html2 = "<html><body><nav><p class='foo bar'>x</p></nav></body></html>"
    assert _selector_class_inside_nav_ancestor("p.foo", html2) is True


# ── Helper: SVG semantic-children detection ────────────────────────────


def test_svg_with_only_paths_returns_false():
    """The verified hero-illustration case: SVG with role=presentation
    and only <path> primitives is correctly decoration.
    """
    html = (
        "<html><body>"
        "<svg role='presentation' viewBox='0 0 100 100'>"
        "<path d='M0 0 L100 100'/>"
        "<circle cx='50' cy='50' r='10'/>"
        "<rect x='0' y='0' width='10' height='10'/>"
        "</svg>"
        "</body></html>"
    )
    assert _svg_with_role_presentation_has_semantic_children(html) is False


def test_svg_with_title_returns_true():
    """An SVG with role=presentation but containing a <title>
    element is genuinely contradictory -- the title says 'I have an
    accessible name' while the role says 'I'm decoration'.
    """
    html = (
        "<html><body>"
        "<svg role='presentation'>"
        "<title>Heart icon</title>"
        "<path d='M0 0'/>"
        "</svg>"
        "</body></html>"
    )
    assert _svg_with_role_presentation_has_semantic_children(html) is True


def test_svg_with_text_element_returns_true():
    """<text> inside SVG is real semantic content (it's rendered
    text that users can read).
    """
    html = (
        "<html><body>"
        "<svg role='presentation'>"
        "<text x='10' y='10'>Click me</text>"
        "</svg>"
        "</body></html>"
    )
    assert _svg_with_role_presentation_has_semantic_children(html) is True


def test_svg_with_aria_label_on_child_returns_true():
    """A <path> with aria-label IS semantic, regardless of the parent's
    role=presentation.
    """
    html = (
        "<html><body>"
        "<svg role='presentation'>"
        "<path aria-label='Important' d='M0 0'/>"
        "</svg>"
        "</body></html>"
    )
    assert _svg_with_role_presentation_has_semantic_children(html) is True


def test_no_role_presentation_svg_returns_false():
    """When no SVG with role=presentation exists in the HTML, the
    HCS finding can't possibly be valid -- drop. Returns False because
    no matching SVG was found.
    """
    html = "<html><body><svg role='img'><title>X</title></svg></body></html>"
    assert _svg_with_role_presentation_has_semantic_children(html) is False


def test_empty_html_returns_true_conservative():
    """No HTML -> keep the finding for the judge."""
    assert _svg_with_role_presentation_has_semantic_children("") is True
    assert _svg_with_role_presentation_has_semantic_children(None) is True


# ── End-to-end: filter integration in extract_htmlcs_findings ──────────


def test_h48_filter_drops_finding_when_paragraph_not_in_nav():
    """Replay the verified SC 1.3.1 false positive: HCS reports
    H48 on a <p class='c-footer__copyright'> that's actually in the
    footer credits, not a nav. Post-fix the finding is dropped at
    extractor time.
    """
    cd = CaptureData(url="https://t/")
    cd.html = (
        "<html><body>"
        "<nav><a href='/'>Home</a></nav>"
        "<footer>"
        "  <p class='c-footer__copyright'>Copyright (c) 2024. APLv2.</p>"
        "</footer>"
        "</body></html>"
    )
    cd.htmlcs_results = {
        "messages": [
            {
                "type": 2,
                "code": "WCAG2AAA.Principle1.Guideline1_3.1_3_1.H48",
                "msg": "If this element contains a navigation section, "
                       "it is recommended that it be marked up as a list.",
                "selector": "p.c-footer__copyright",
                "tag_name": "p",
            },
        ],
    }
    findings = extract_htmlcs_findings(cd, "1.3.1")
    assert findings == [], (
        "Pre-fix: this finding flowed through and the judge rewrote it as "
        "'navigation items violate WCAG 1.3.1'. Post-fix: copyright "
        "paragraph not in <nav>, finding correctly dropped at extractor."
    )


def test_h48_filter_keeps_finding_when_paragraph_in_nav():
    """The legitimate H48 case: <p class='item'> inside <nav> is
    actually navigation-as-paragraph. Finding kept.
    """
    cd = CaptureData(url="https://t/")
    cd.html = (
        "<html><body>"
        "<nav>"
        "  <p class='item'><a href='/a'>A</a></p>"
        "  <p class='item'><a href='/b'>B</a></p>"
        "</nav>"
        "</body></html>"
    )
    cd.htmlcs_results = {
        "messages": [
            {
                "type": 2,
                "code": "WCAG2AAA.Principle1.Guideline1_3.1_3_1.H48",
                "msg": "If this element contains a navigation section...",
                "selector": "p.item",
                "tag_name": "p",
            },
        ],
    }
    findings = extract_htmlcs_findings(cd, "1.3.1")
    assert len(findings) == 1, "in-nav paragraphs are real H48 findings"
    assert findings[0].source == "htmlcs"


def test_f92_filter_drops_finding_when_svg_is_decorative_primitives():
    """Verified hero-illustration case: SVG with role=presentation and
    only <path> children. Pre-fix: HIGH severity false positive
    against generic 'svg' selector. Post-fix: dropped at extractor.
    """
    cd = CaptureData(url="https://t/")
    cd.html = (
        "<html><body>"
        "<svg viewBox='0 0 1440 825' role='presentation'>"
        "<path d='M0 0 L100 100'/>"
        "<rect x='0' y='0' width='10' height='10'/>"
        "</svg>"
        "</body></html>"
    )
    cd.htmlcs_results = {
        "messages": [
            {
                "type": 1,
                "code": "WCAG2AAA.Principle1.Guideline1_3.1_3_1.F92,ARIA4",
                "msg": "This element's role is 'presentation' but contains "
                       "child elements with semantic meaning.",
                "selector": "svg",
                "tag_name": "svg",
            },
        ],
    }
    findings = extract_htmlcs_findings(cd, "1.3.1")
    assert findings == [], (
        "SVG with only visual primitives is decoration; F92 is a "
        "false positive when no semantic children exist."
    )


def test_f92_filter_keeps_finding_when_svg_has_title():
    """The legitimate F92 case: SVG with role=presentation containing
    a <title> element. The <title> contradicts role=presentation -- a
    real WCAG issue.
    """
    cd = CaptureData(url="https://t/")
    cd.html = (
        "<html><body>"
        "<svg role='presentation'>"
        "<title>Important graphic</title>"
        "<path d='M0 0'/>"
        "</svg>"
        "</body></html>"
    )
    cd.htmlcs_results = {
        "messages": [
            {
                "type": 1,
                "code": "WCAG2AAA.Principle1.Guideline1_3.1_3_1.F92,ARIA4",
                "msg": "...",
                "selector": "svg",
                "tag_name": "svg",
            },
        ],
    }
    findings = extract_htmlcs_findings(cd, "1.3.1")
    assert len(findings) == 1, "SVG with <title> + role=presentation is real"


def test_h48_filter_reads_dom_from_disk_when_html_field_empty():
    """capture_data.html may be empty during SC-check time because the
    v2 orchestrator pipeline doesn't always keep the rendered DOM in
    the in-memory CaptureData object after capture phase. dom_path
    points to the on-disk file. The filter must fall back to reading
    dom_path when the html field is empty -- otherwise it gets the
    conservative "keep finding" path even when the disk has data that
    would clearly disprove the finding.

    Verified gap on a public accessibility-resource site run 20260511 SC 1.3.1 where 3 H48
    false positives flowed through because capture_data.html was 0
    chars even though dom.html on disk was 29k chars.
    """
    import os
    import tempfile

    cd = CaptureData(url="https://t/")
    cd.html = ""  # in-memory html cleared, simulating the production state

    # Write a real dom.html that does NOT have the cited class in any nav
    dom_html = (
        "<html><body>"
        "<nav><a href='/'>Home</a></nav>"
        "<footer>"
        "  <p class='c-footer__copyright'>Copyright (c) 2024.</p>"
        "</footer>"
        "</body></html>"
    )
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".html", encoding="utf-8", delete=False,
    ) as fh:
        fh.write(dom_html)
        dom_path = fh.name
    cd.dom_path = dom_path

    cd.htmlcs_results = {
        "messages": [
            {
                "type": 2,
                "code": "WCAG2AAA.Principle1.Guideline1_3.1_3_1.H48",
                "msg": "If this element contains a navigation section...",
                "selector": "p.c-footer__copyright",
                "tag_name": "p",
            },
        ],
    }
    try:
        findings = extract_htmlcs_findings(cd, "1.3.1")
    finally:
        os.unlink(dom_path)

    assert findings == [], (
        "Pre-fix: empty capture_data.html caused the H48 filter to "
        "conservatively keep findings. Post-fix: dom_path is read from "
        "disk, the c-footer__copyright class is found NOT inside <nav>, "
        "and the false positive is correctly dropped."
    )


def test_h48_filter_keeps_finding_when_disk_load_fails():
    """If dom_path is set but the file is unreadable, the filter must
    still fall back to conservative 'keep' behavior (don't crash, don't
    silently drop everything).
    """
    cd = CaptureData(url="https://t/")
    cd.html = ""
    cd.dom_path = "/nonexistent/path/dom.html"
    cd.htmlcs_results = {
        "messages": [
            {
                "type": 2,
                "code": "WCAG2AAA.Principle1.Guideline1_3.1_3_1.H48",
                "msg": "If this element contains a navigation section...",
                "selector": "p.something",
                "tag_name": "p",
            },
        ],
    }
    findings = extract_htmlcs_findings(cd, "1.3.1")
    # Unreadable dom_path -> html stays empty -> filter is conservative -> keep finding
    assert len(findings) == 1


def test_other_hcs_rules_pass_through_unchanged():
    """The filters only target H48 and F92,ARIA4. Other 1.3.1 HCS
    rules (e.g. H71 fieldset/legend) must still flow through.
    """
    cd = CaptureData(url="https://t/")
    cd.html = "<html><body><fieldset><input></fieldset></body></html>"
    cd.htmlcs_results = {
        "messages": [
            {
                "type": 1,
                "code": "WCAG2AAA.Principle1.Guideline1_3.1_3_1.H71.NoLegend",
                "msg": "Fieldset does not contain a legend element.",
                "selector": "fieldset.x",
                "tag_name": "fieldset",
            },
        ],
    }
    findings = extract_htmlcs_findings(cd, "1.3.1")
    assert len(findings) == 1
    assert findings[0].severity == Severity.HIGH
    assert findings[0].source == "htmlcs"


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
