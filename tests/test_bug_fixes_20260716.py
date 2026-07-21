"""Regression tests for the 2026-07-16 capture-pipeline bug batch.

Covers the offline-testable fixes:
  1. structural_summary.json nav_links populated from in_nav (SC 3.2.3
     cross-page aggregation was permanently blind because no extractor
     ever set in_nav).
  3. Table structure (headers / row_count / summary) carried through
     InventoryElement -> mapper -> capture_data.tables with the
     snake_case keys checks/base.py reads (SC 1.3.1 saw headerless
     0-row tables on every v2 run).
  6. BCP-47 regex sent to page JS must carry a single backslash before
     "d" so the JS RegExp matches digits, not a literal backslash-d.
  9. One malformed artifact must not abort the whole piecemeal resume.
 11. Review-root resolution for auth-state lookups (page_NNN/doc_NNN
     dirs resolve to their parent; the review root resolves to itself).
"""
import json
import os

from capture.v2.element_inventory import (
    ElementInventory,
    InventoryElement,
    _elem_to_dict,
    map_inventory_to_capture_data,
)
from functions.file_io import get_review_root
from models import CaptureData


# ── Item 1: nav_links in structural_summary.json ────────────────────────────

def _summary_for(links, tmp_path):
    from capture.web_capture import _save_structural_summary
    captures_dir = str(tmp_path)
    cd = CaptureData(url="https://example.edu/", title="Example")
    cd.links = links
    _save_structural_summary(captures_dir, cd)
    with open(os.path.join(captures_dir, "structural_summary.json"), encoding="utf-8") as f:
        return json.load(f)


def test_structural_summary_nav_links_populated_from_in_nav(tmp_path):
    summary = _summary_for(
        [
            {"text": "Home", "href": "/", "in_nav": True},
            {"text": "About", "href": "/about", "in_nav": True},
            {"text": "Read more", "href": "/article", "in_nav": False},
        ],
        tmp_path,
    )
    assert summary["nav_links"] == [
        {"text": "Home", "href": "/"},
        {"text": "About", "href": "/about"},
    ]


def test_structural_summary_nav_links_empty_without_nav(tmp_path):
    summary = _summary_for(
        [{"text": "Read more", "href": "/article", "in_nav": False}],
        tmp_path,
    )
    assert summary["nav_links"] == []


def test_inventory_link_mapper_emits_in_nav():
    inv = ElementInventory.from_dict({
        "elements": [
            {"type": "link", "selector": "#nav-home", "tag": "a",
             "text": "Home", "href": "/", "in_nav": True},
            {"type": "link", "selector": "#body-link", "tag": "a",
             "text": "Read more", "href": "/a", "in_nav": False},
        ],
    })
    cd = CaptureData(url="https://example.edu/")
    map_inventory_to_capture_data(inv, cd)
    by_sel = {l["selector"]: l for l in cd.links}
    assert by_sel["#nav-home"]["in_nav"] is True
    assert by_sel["#body-link"]["in_nav"] is False


def test_v2_pipeline_produces_nonempty_nav_links(tmp_path):
    """End-to-end offline: inventory link with in_nav -> mapper ->
    structural-summary writer (the same writer the v2 orchestrator calls)."""
    from capture.web_capture import _save_structural_summary
    inv = ElementInventory.from_dict({
        "elements": [
            {"type": "link", "selector": "#nav-home", "tag": "a",
             "text": "Home", "href": "/", "in_nav": True},
        ],
    })
    cd = CaptureData(url="https://example.edu/", title="Example")
    map_inventory_to_capture_data(inv, cd)
    _save_structural_summary(str(tmp_path), cd)
    with open(os.path.join(str(tmp_path), "structural_summary.json"), encoding="utf-8") as f:
        summary = json.load(f)
    assert summary["nav_links"] == [{"text": "Home", "href": "/"}]


# ── Item 3: table structure survives the inventory round trip ───────────────

_TABLE_DICT = {
    "type": "table", "selector": "#enrollment", "tag": "table",
    "text": "Enrollment by year",
    "headers": [
        {"text": "Year", "scope": "col", "id": ""},
        {"text": "Students", "scope": "col", "id": ""},
    ],
    "rowCount": 12,
    "summary": "Yearly enrollment figures",
}


def test_inventory_element_keeps_table_structure():
    e = InventoryElement.from_dict(_TABLE_DICT)
    assert e.headers == _TABLE_DICT["headers"]
    assert e.row_count == 12
    assert e.summary == "Yearly enrollment figures"


def test_inventory_element_table_fields_survive_serialization():
    e = InventoryElement.from_dict(_TABLE_DICT)
    round_tripped = InventoryElement.from_dict(_elem_to_dict(e))
    assert round_tripped.headers == _TABLE_DICT["headers"]
    assert round_tripped.row_count == 12
    assert round_tripped.summary == "Yearly enrollment figures"


def test_table_mapper_emits_reader_keys():
    """checks/base.py reads has_headers / headers and row_count."""
    inv = ElementInventory.from_dict({"elements": [_TABLE_DICT]})
    cd = CaptureData(url="https://example.edu/")
    map_inventory_to_capture_data(inv, cd)
    assert len(cd.tables) == 1
    tbl = cd.tables[0]
    assert tbl["headers"] == _TABLE_DICT["headers"]
    assert tbl["has_headers"] is True
    assert tbl["row_count"] == 12
    assert tbl["rowCount"] == 12
    assert tbl["summary"] == "Yearly enrollment figures"
    assert tbl["caption"] == "Enrollment by year"


def test_table_mapper_headerless_table_reads_headerless():
    inv = ElementInventory.from_dict({"elements": [
        {"type": "table", "selector": "#layout", "tag": "table",
         "headers": [], "rowCount": 3},
    ]})
    cd = CaptureData(url="https://example.edu/")
    map_inventory_to_capture_data(inv, cd)
    tbl = cd.tables[0]
    assert tbl["has_headers"] is False
    assert tbl["headers"] == []
    assert tbl["row_count"] == 3


# ── Item 6: BCP-47 regex escaping ────────────────────────────────────────────

def test_bcp47_regex_has_single_backslash_d():
    from capture.web_capture import _BCP47_RE_JS
    assert "\\d" in _BCP47_RE_JS
    assert "\\\\d" not in _BCP47_RE_JS
    # The pattern itself must accept a numeric UN M49 region subtag.
    import re
    assert re.match(_BCP47_RE_JS, "es-419")
    assert re.match(_BCP47_RE_JS, "en-US")
    assert not re.match(_BCP47_RE_JS, "not a lang tag")


# ── Item 9: malformed artifact must not abort the piecemeal resume ──────────

def test_resume_continues_past_malformed_artifact(tmp_path):
    review_dir = str(tmp_path)
    captures_dir = os.path.join(review_dir, "captures")
    os.makedirs(captures_dir)
    with open(os.path.join(captures_dir, "tab_walk.json"), "w", encoding="utf-8") as f:
        f.write("{{{ not json")
    traps = [{"type": "consecutive", "selector": "#stuck"}]
    with open(os.path.join(captures_dir, "keyboard_traps.json"), "w", encoding="utf-8") as f:
        json.dump(traps, f)

    from capture.v2.state import reload_capture_data
    cd = reload_capture_data(review_dir)
    assert cd.keyboard_traps == traps
    assert cd.tab_walk == []


# ── Item 11: review-root resolution ──────────────────────────────────────────

def test_get_review_root_page_dir():
    assert get_review_root(os.path.join("reviews", "20260716_x", "page_003")) == \
        os.path.join("reviews", "20260716_x")


def test_get_review_root_doc_dir():
    assert get_review_root(os.path.join("reviews", "20260716_x", "doc_001")) == \
        os.path.join("reviews", "20260716_x")


def test_get_review_root_is_identity_for_review_root():
    assert get_review_root(os.path.join("reviews", "20260716_x")) == \
        os.path.join("reviews", "20260716_x")
