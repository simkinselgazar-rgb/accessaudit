"""Pure-Python selector matching helpers shared by capture probes.

These operate on the selector strings the JS extractors emit
(``functions.js_helpers.GET_SELECTOR_JS`` format: ``#id`` or a
structural ``tag > tag:nth-of-type(n)`` path). They exist so
containment/identity decisions are testable without a browser.
"""
from __future__ import annotations

# Characters that may legally follow a container selector when the
# active element's selector path extends it: a descendant separator
# (' ' as in ' > '), a combinator ('>'), a pseudo-class (':'), an extra
# class ('.'), or an attribute selector ('['). Anything else — e.g.
# '#nav' followed by '2' — means a DIFFERENT element whose selector
# merely shares a prefix.
_BOUNDARY_CHARS = frozenset(" >:.[")


def selector_within(active_selector: str, container_selector: str) -> bool:
    """Structural test: does *active_selector* denote an element that is
    the container itself or inside it?

    Used as the fallback when DOM-level containment can't be probed
    (container selector no longer resolves). Rules:

    - Empty active selector, or focus parked on <body>/<html>, counts
      as ESCAPED (False). The old raw-substring test (``'' in target``)
      counted a lost focus as "inside" and manufactured trap suspects.
    - Exact match counts as inside (focus on the container itself).
    - A path that starts with the container selector followed by a CSS
      boundary character counts as inside (e.g. ``#modal > div > button``
      within ``#modal``). ``#nav2`` does NOT match container ``#nav``.
    """
    active = (active_selector or "").strip()
    container = (container_selector or "").strip()
    if not active or not container:
        return False
    if active in ("body", "html") or active.startswith(
        ("body.", "body#", "body:", "html.", "html#", "html:")
    ):
        return False
    if active == container:
        return True
    if active.startswith(container) and active[len(container)] in _BOUNDARY_CHARS:
        return True
    return False
