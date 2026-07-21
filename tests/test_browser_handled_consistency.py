"""Regression: the judge DOM-context annotation and the server-side finding
filter must agree on which ANDI hidden entries are [BROWSER-HANDLED].

Verified false-positive (a university site, 2026-05-28): a `visibility:hidden`
"Academic Calendar" quick-link whose ANDI `tab_reachable` heuristic wrongly
read True got annotated [BROWSER-HANDLED] but its findings were NOT dropped,
producing repeat focus-leak false positives across 1.3.1/2.1.1/2.1.3/2.4.3/
2.4.7/4.1.2. Both paths now route through `is_browser_handled`.
"""
from functions.andi_extract import is_browser_handled


def test_visibility_hidden_with_true_tab_reachable_is_browser_handled():
    # The exact FP shape: browser skips visibility:hidden regardless of
    # ANDI's tab_reachable heuristic.
    entry = {
        "selector": "ul#quick-links-content > li > a",
        "accessible_name": "Academic Calendar",
        "hidden_reasons": ["visibility:hidden"],
        "tab_reachable": True,
        "rect": {"width": 100, "height": 20},
    }
    assert is_browser_handled(entry) is True


def test_display_none_and_hidden_attr_and_inert_are_browser_handled():
    for reason in ("display:none", "hidden attribute", "inert (ancestor)"):
        assert is_browser_handled(
            {"hidden_reasons": [reason], "tab_reachable": True,
             "rect": {"width": 50, "height": 10}}
        ) is True


def test_zero_rect_is_browser_handled():
    assert is_browser_handled(
        {"hidden_reasons": ["aria-hidden=true (ancestor)"],
         "tab_reachable": True, "rect": {"width": 0, "height": 0}}
    ) is True


def test_tab_reachable_false_is_browser_handled():
    assert is_browser_handled(
        {"hidden_reasons": [], "tab_reachable": False,
         "rect": {"width": 100, "height": 20}}
    ) is True


def test_genuinely_visible_reachable_element_is_not_browser_handled():
    # A real focusable, visible element must NOT be suppressed.
    assert is_browser_handled(
        {"hidden_reasons": [], "tab_reachable": True,
         "rect": {"width": 100, "height": 20}}
    ) is False


def test_non_dict_is_not_browser_handled():
    assert is_browser_handled(None) is False
    assert is_browser_handled("nope") is False
