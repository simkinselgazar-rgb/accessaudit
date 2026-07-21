"""Regression: SC 1.4.4 200%-overflow must distinguish real text-content loss
from benign overflow (carousels, decorative grids, zero-area, full-width
carousel viewports).

Verified on a university site, 2026-05-28: all 6 programmatic 1.4.4 findings were FPs --
off-screen carousel slides (x=-1280/1280/3840), the full-width #large-features
carousel viewport, a height=0 wrapper, and uniform 304x304 gallery tiles.
"""
import asyncio

from functions.overflow import classify_overflow_loss
from models import CaptureData
from checks.checks_1_4 import Check_1_4_4


def _e(sel, x, w, h, ox=False, oy=False, clip=True):
    return {"selector": sel, "rect": {"x": x, "width": w, "height": h},
            "overflowX": ox, "overflowY": oy, "clippedBySelf": clip}


def test_university_site_carousel_and_gallery_all_dropped():
    entries = [
        _e("#large-features", 0, 1280, 4288, ox=True),                 # full-width carousel viewport
        _e("#large-features > ul > li:1", -1280, 2560, 2144, oy=True),  # off-screen left slide
        _e("#large-features > ul > li:2", 1280, 2560, 2144, oy=True),   # off-screen right slide
        _e("#large-features > ul > li:3", 3840, 2560, 2144, oy=True),   # off-screen right slide
        _e("#large-features > ul > li:4", -1280, 2560, 2144, oy=True),
        _e("#large-features > ul > li:5", 1280, 2560, 2144, oy=True),
        _e("li:2 > div", 1280, 2560, 0, oy=True, clip=False),           # zero-height, not self-clip
    ] + [
        _e(f"#gallery li:{i}", 7 + (i % 4) * 320, 304, 304, oy=True)    # uniform decorative tile grid
        for i in range(8)
    ]
    kept, skipped = classify_overflow_loss(entries)
    assert kept == [], f"expected all benign, kept: {[k['selector'] for k in kept]}"
    assert sum(skipped.values()) == len(entries)


def test_real_onscreen_text_clip_is_kept():
    # A genuine fixed-width container clipping its own text at 200%, on-screen,
    # not part of a grid -> a REAL 1.4.4 failure that must survive.
    entries = [
        _e("#article > p.fixed-width", 40, 300, 80, ox=True, clip=True),
        _e("#sidebar", 0, 1280, 2000, oy=False, ox=False),  # not overflowing -> ignored
    ]
    kept, skipped = classify_overflow_loss(entries)
    assert [k["selector"] for k in kept] == ["#article > p.fixed-width"]


def test_zero_and_nonclipping_dropped_but_real_kept():
    entries = [
        _e("a", 10, 100, 0, oy=True),                  # zero height
        _e("b", 10, 100, 50, oy=True, clip=False),     # not self-clipping
        _e("c", 10, 100, 50, oy=True, clip=True),      # real on-screen clip
    ]
    kept, skipped = classify_overflow_loss(entries)
    assert [k["selector"] for k in kept] == ["c"]
    assert skipped["zero-area box"] == 1
    assert skipped["does not clip its own content (no loss)"] == 1


def test_check_1_4_4_run_programmatic_executes_and_filters():
    # Exercises the real check run path end-to-end (would have caught the
    # missing-`logger` NameError that made 1.4.4 'Not Evaluated' on the
    # 2026-05-29 re-run). Carousel + zero-area overflow -> Supports, no
    # findings; one genuine on-screen text clip -> a finding survives.
    cap = CaptureData()
    cap.viewport_meta = {"content": "width=device-width, initial-scale=1.0"}
    cap.overflow_200pct = [
        {"selector": "#carousel", "tag": "div", "overflowX": True,
         "overflowY": False, "clippedBySelf": True,
         "rect": {"x": 0, "width": 1280, "height": 4000}},
        {"selector": "#carousel > li:1", "tag": "li", "overflowX": False,
         "overflowY": True, "clippedBySelf": True,
         "rect": {"x": -1280, "width": 2560, "height": 2000}},
    ]
    conf, confidence, findings = asyncio.run(Check_1_4_4().run_programmatic(cap))
    cv = conf.value if hasattr(conf, "value") else conf
    assert cv == "Supports"
    assert findings == []
