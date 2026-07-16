"""Regression tests for the BG-image UI-DECORATION classifier in
``checks/base.py:_build_dom_context``.

The bug these tests pin: visual_ai + judge cannot distinguish a tiny
inline-SVG bg-image used as a radio-button visual indicator from a real
informational background image. On 20260506_135324_f8765656 SC 1.1.1
this caused 2 false-positive findings against ``#edit-location-inperson``
and ``#edit-standing-undergrad``, both of which had a 16x16 rect with a
``data:image/svg+xml ... viewBox='-4 -4 8 8' circle`` bg -- the radio
dot.

The fix marks such entries ``[UI-DECORATION]`` in the prompt and tells
the judge in the section header NOT to flag them for SC 1.1.1 / 1.4.5.
The classification is conservative: ``data:`` URI + no inner_text + no
aria_label + non-img role + (small element OR form-control tag).

Run with:

    python tests/test_dom_context_bg_decoration.py
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


def _make_capture_data(bg_images):
    """Stub a CaptureData with just background_images populated.

    Real CaptureData has 50+ fields; we set only what _build_dom_context
    reads to avoid pulling in the full capture pipeline. The early-return
    guard at the top of _build_dom_context requires html OR headings OR
    images to produce any output, so we set a tiny html stub to clear it.
    """
    from models import CaptureData
    cd = CaptureData(url="https://test/")
    cd.html = "<html><body></body></html>"
    cd.background_images = bg_images
    return cd


def _build_context(criterion_id: str, bg_images: list[dict]) -> str:
    """Run BaseCheck._build_dom_context with a stubbed capture and return
    the rendered prompt block. The check class does not need to be a
    specific subclass; BaseCheck is fine for prompt-rendering coverage.
    """
    from checks.base import BaseCheck
    check = BaseCheck()
    check.criterion_id = criterion_id
    check.criterion_name = "test"
    check.level = "AA"
    cd = _make_capture_data(bg_images)
    return check._build_dom_context(cd)


# ── The radio-dot pattern: small element + tiny inline-SVG ──────────────


def test_radio_dot_svg_bg_marked_ui_decoration():
    """The exact university radio-button pattern that produced false positives."""
    bg = [{
        "selector": "#edit-location-inperson",
        "tag": "input",
        "backgroundImage": (
            "url(\"data:image/svg+xml;charset=utf-8,"
            "%3Csvg xmlns='http://www.w3.org/2000/svg' "
            "viewBox='-4 -4 8 8'%3E"
            "%3Ccircle r='2' fill='%23fff'/%3E"
            "%3C/svg%3E\")"
        ),
        "role": "",
        "ariaLabel": "",
        "text_content": "",
        "rect": {"x": 65, "y": 1030, "width": 16, "height": 16},
    }]
    out = _build_context("1.1.1", bg)
    assert "[UI-DECORATION]" in out, (
        "Radio-dot SVG bg-image must be classified [UI-DECORATION] "
        "so the judge does not flag it for SC 1.1.1"
    )
    # The header rule must also be present so the judge knows what
    # the marker means.
    assert "DO NOT emit findings for [UI-DECORATION]" in out


def test_checkbox_check_svg_marked_ui_decoration():
    """Variant: checkbox checkmark SVG on a small <input>."""
    bg = [{
        "selector": "input[type='checkbox']:checked",
        "tag": "input",
        "backgroundImage": (
            "url(\"data:image/svg+xml,%3Csvg "
            "viewBox='0 0 12 12'%3E%3Cpath d='M2 6 L5 9 L10 3'/%3E%3C/svg%3E\")"
        ),
        "role": "",
        "ariaLabel": "",
        "text_content": "",
        "rect": {"x": 0, "y": 0, "width": 18, "height": 18},
    }]
    out = _build_context("1.1.1", bg)
    assert "[UI-DECORATION]" in out


# ── Non-decoration cases (must NOT be flagged) ─────────────────────────


def test_real_hero_bg_image_not_marked_ui_decoration():
    """Large bg-image with real photo content is NOT UI-decoration --
    it's the legitimate target SC 1.1.1 cares about. Marking these as
    decoration would silence real findings.
    """
    bg = [{
        "selector": "div.hero",
        "tag": "div",
        "backgroundImage": "url(\"https://www.example.edu/photos/hero-banner.jpg\")",
        "role": "",
        "ariaLabel": "",
        "text_content": "",
        "rect": {"x": 0, "y": 0, "width": 1920, "height": 600},
    }]
    out = _build_context("1.1.1", bg)
    assert "[UI-DECORATION]" not in out


def test_bg_image_with_inner_text_not_ui_decoration():
    """A bg-image on an element that ALSO has DOM inner_text is not a
    UI indicator -- the inner_text is a sibling overlay (the SC 1.4.5
    pattern). The existing 1.4.5 rule handles this; UI-decoration would
    be a wrong classification.
    """
    bg = [{
        "selector": "div.banner",
        "tag": "div",
        "backgroundImage": (
            "url(\"data:image/svg+xml,%3Csvg viewBox='0 0 4 4'/%3E\")"
        ),
        "role": "",
        "ariaLabel": "",
        "text_content": "Reimagining education for everyone",
        "rect": {"x": 0, "y": 0, "width": 24, "height": 24},
    }]
    out = _build_context("1.1.1", bg)
    assert "[UI-DECORATION]" not in out


def test_bg_image_with_aria_label_not_ui_decoration():
    """An aria-label on the element means the author has explicitly
    given it semantic meaning. Don't classify as decoration even if
    other heuristics would flag it.
    """
    bg = [{
        "selector": "button.close",
        "tag": "button",
        "backgroundImage": (
            "url(\"data:image/svg+xml,%3Csvg viewBox='0 0 8 8'/%3E\")"
        ),
        "role": "",
        "ariaLabel": "Close dialog",
        "text_content": "",
        "rect": {"x": 0, "y": 0, "width": 24, "height": 24},
    }]
    out = _build_context("1.1.1", bg)
    assert "[UI-DECORATION]" not in out


def test_bg_image_with_role_img_not_ui_decoration():
    """role='img' explicitly declares the element as graphical content.
    Author intent overrides the size heuristic.
    """
    bg = [{
        "selector": "span.icon",
        "tag": "span",
        "backgroundImage": (
            "url(\"data:image/svg+xml,%3Csvg viewBox='0 0 8 8'/%3E\")"
        ),
        "role": "img",
        "ariaLabel": "",
        "text_content": "",
        "rect": {"x": 0, "y": 0, "width": 16, "height": 16},
    }]
    out = _build_context("1.1.1", bg)
    assert "[UI-DECORATION]" not in out


def test_external_url_bg_on_form_control_not_ui_decoration():
    """data: URIs are the marker for inline UI-styling SVGs. A real
    external URL on a form control is unusual but not necessarily
    decoration -- don't auto-classify.
    """
    bg = [{
        "selector": "select.custom",
        "tag": "select",
        "backgroundImage": "url(\"https://cdn/dropdown-arrow.png\")",
        "role": "",
        "ariaLabel": "",
        "text_content": "",
        "rect": {"x": 0, "y": 0, "width": 200, "height": 32},
    }]
    out = _build_context("1.1.1", bg)
    # External URL is not a data: URI, so the conservative rule doesn't
    # mark it. (This may produce a noisy entry that the existing
    # crop-binding rule already handles; it's not the regression we're
    # fixing here.)
    assert "[UI-DECORATION]" not in out


# ── Header rule ─────────────────────────────────────────────────────────


def test_no_ui_decoration_block_when_none_classified():
    """When NO bg-image qualifies as UI-decoration, the explanation
    paragraph should not appear -- it would be noise.
    """
    bg = [{
        "selector": "div.hero",
        "tag": "div",
        "backgroundImage": "url(\"https://example/hero.jpg\")",
        "role": "",
        "ariaLabel": "",
        "text_content": "",
        "rect": {"x": 0, "y": 0, "width": 1920, "height": 600},
    }]
    out = _build_context("1.1.1", bg)
    assert "[UI-DECORATION]" not in out
    assert "DO NOT emit findings for [UI-DECORATION]" not in out


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
