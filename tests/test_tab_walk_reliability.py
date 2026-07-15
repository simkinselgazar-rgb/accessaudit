"""Regression: keyboard SCs must not rest on a 0%-coverage tab walk when the
page clearly has focusable elements.

Root cause (umich.edu 2026-05-29): the Phase 3 video walk got stuck on a
Cloudflare interstitial (5 stops) and the orchestrator overwrote the good
72-stop v1 walk, so the judge saw "TAB WALK: 5 reached" beside "TAB COVERAGE:
72 reached (104%)" and reported a bogus 0%-coverage 2.1.1 failure.

These tests pin the reliability assessment that now drives the DOM-context
warning and the server-side filter (Rule 9b).
"""
from types import SimpleNamespace

from functions.keyboard_extract import assess_tab_walk_reliability
from models import CaptureData, Finding, Severity
from checks.base import BaseCheck

_filter = BaseCheck._filter_findings_contradicted_by_capture


def _ns(coverage=None, walk=None, links=0):
    return SimpleNamespace(
        tab_coverage=coverage or {},
        tab_walk=walk or [],
        links=[{}] * links, form_fields=[], buttons=[],
    )


def test_high_coverage_is_reliable_even_if_walk_list_is_degraded():
    # Authoritative coverage (104%) wins over a degraded 5-entry walk list.
    r = assess_tab_walk_reliability(_ns(
        coverage={"total_interactive": 69, "reached_by_tab": 72, "coverage_percent": 104.35},
        walk=[{"tag": "div", "selector": "#GZIfP3 > div"}] * 5,
    ))
    assert r["reliable"] is True
    assert r["reached"] == 72


def test_zero_coverage_on_focusable_page_is_unreliable():
    r = assess_tab_walk_reliability(_ns(
        coverage={"total_interactive": 69, "reached_by_tab": 0, "coverage_percent": 0.0},
        walk=[{"tag": "body"}],
    ))
    assert r["reliable"] is False


def test_challenge_dominated_walk_is_unreliable():
    r = assess_tab_walk_reliability(_ns(
        coverage={"total_interactive": 10, "reached_by_tab": 3, "coverage_percent": 30.0},
        walk=[{"tag": "div", "selector": "#cf-challenge > div"},
              {"tag": "iframe", "selector": "iframe.cloudflare-app"},
              {"tag": "div", "selector": "#GZIfP3 > div"}],
    ))
    assert r["reliable"] is False
    assert r["challenge_dominated"] is True


def test_small_page_with_few_focusables_not_flagged():
    # A page with <5 focusables and a low count is not a capture failure.
    r = assess_tab_walk_reliability(_ns(
        coverage={"total_interactive": 2, "reached_by_tab": 2, "coverage_percent": 100.0},
        walk=[{"tag": "a", "selector": "a.one"}, {"tag": "a", "selector": "a.two"}],
    ))
    assert r["reliable"] is True


# ── filter Rule 9b: drop low-coverage keyboard findings on an unreliable walk

def _cap(coverage, walk):
    cd = CaptureData()
    cd.tab_coverage = coverage
    cd.tab_walk = walk
    return cd


def _f(issue, sc="2.1.1"):
    return Finding(id="x", element="page", issue=issue, impact="i",
                   recommendation="r", severity=Severity.HIGH,
                   source="judge_inference", css_selector="")


def test_low_coverage_finding_dropped_when_walk_unreliable():
    cd = _cap({"total_interactive": 69, "reached_by_tab": 0, "coverage_percent": 0.0},
              [{"tag": "body"}])
    out = _filter(SimpleNamespace(criterion_id="2.1.1"),
                  [_f("The keyboard tab walk reached 0 of approximately 69 "
                      "interactive elements (0% coverage)")], cd)
    assert out == []


def test_low_coverage_finding_kept_when_walk_reliable():
    # Genuine keyboard finding on a RELIABLE mid-coverage walk must survive.
    # Coverage 50% is reliable (>=15%, >=3 reached) AND below the 90% threshold
    # of the separate high-coverage Rule 9, so neither rule should drop it.
    cd = _cap({"total_interactive": 20, "reached_by_tab": 10, "coverage_percent": 50.0,
               "focusable_but_skipped": []},
              [{"tag": "a", "selector": f"a.l{i}"} for i in range(10)])
    out = _filter(SimpleNamespace(criterion_id="2.1.1"),
                  [_f("A custom date-picker widget traps focus and is not operable")], cd)
    assert len(out) == 1
