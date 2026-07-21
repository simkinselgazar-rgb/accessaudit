"""Finding-related helpers shared by checks and judge.

Tiny utilities — kept here (not in `models.py`) because they encode
business rules about *how* findings are produced and compared, not just
the data shape.
"""
from __future__ import annotations

import re
import uuid

from models import ConformanceLevel


def element_is_display_hidden(html: str, selector: str) -> bool:
    """True when an id-selected element is rendered-hidden in the captured
    DOM (``display:none`` / ``visibility:hidden`` / the ``hidden`` attribute).

    Such an element is not rendered, not interactive, and not in the
    accessibility tree, so 'interactive control' / 'no role or name' findings
    on it are false positives (verified on a university site 2026-05-28:
    ``<iframe id="ce_proto_iframe" ... style="display: none;">`` was flagged
    as an interactive control lacking a role/name). Only handles id-based
    selectors (``#foo``); returns False for anything else or if not found.
    Note: ``aria-hidden`` alone is intentionally NOT treated as hidden here --
    aria-hidden on a *visible, focusable* element is itself a real defect.
    """
    if not html or not selector:
        return False
    m = re.search(r"#([\w-]+)", selector)
    if not m:
        return False
    eid = m.group(1)
    tag_m = re.search(
        rf'<[a-zA-Z][^>]*\bid\s*=\s*["\']{re.escape(eid)}["\'][^>]*>', html
    )
    if not tag_m:
        return False
    tag = tag_m.group(0).lower()
    return bool(
        re.search(r"display\s*:\s*none", tag)
        or re.search(r"visibility\s*:\s*hidden", tag)
        or re.search(r"\shidden(\s|=|>|/)", tag)
    )


# Conformance ordering: worse is higher index. Used by the judge,
# cross-source mergers, and per-criterion roll-up to pick the worst
# verdict across multiple sources.
_CONFORMANCE_ORDER = [
    ConformanceLevel.SUPPORTS,
    ConformanceLevel.NOT_APPLICABLE,
    ConformanceLevel.NOT_EVALUATED,
    ConformanceLevel.PARTIALLY_SUPPORTS,
    ConformanceLevel.DOES_NOT_SUPPORT,
]


def _worse(a: ConformanceLevel, b: ConformanceLevel) -> ConformanceLevel:
    """Return the worse of two conformance levels."""
    idx_a = _CONFORMANCE_ORDER.index(a) if a in _CONFORMANCE_ORDER else 2
    idx_b = _CONFORMANCE_ORDER.index(b) if b in _CONFORMANCE_ORDER else 2
    return _CONFORMANCE_ORDER[max(idx_a, idx_b)]


def _make_finding_id() -> str:
    """Short stable identifier for a Finding. 12 hex chars of a UUID4."""
    return uuid.uuid4().hex[:12]
