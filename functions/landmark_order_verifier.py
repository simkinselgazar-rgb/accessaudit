"""Cross-check visual_ai landmark-order claims against the captured
accessibility tree.

Verified failure mode on A11Y Project run 20260511 SC 1.3.2: visual_ai
claimed *"main content is placed AFTER secondary navigation and footer
elements in the accessibility tree, violating WCAG 1.3.2"*. The actual
captured a11y tree had ``[main]`` at position 3 of the landmark list,
with ``[contentinfo]`` and ``[navigation]`` appearing AFTER it. The
LLM consumed the correct data and inverted its directional conclusion --
the same hedge-removal / direction-flip pattern documented in CLAUDE.md
("tab walk reaches it but model says not reachable").

This module is the structural cross-check: when a visual_ai finding's
issue text contains a landmark-order claim ("X comes before Y in the
accessibility tree"), we programmatically locate both landmarks in the
captured a11y tree and verify the direction. Contradictions are dropped.

The function is intentionally narrow:
  - Only landmark-order claims trigger the check; other visual_ai
    claims (focus visibility, alt-text quality, contrast over images)
    are out of scope -- they're not verifiable from the a11y tree alone.
  - When the captured a11y tree is missing or both landmarks can't be
    located, we KEEP the finding (conservative: don't suppress on
    incomplete data).
"""
from __future__ import annotations

import logging
import re

logger = logging.getLogger(__name__)


# ARIA landmark roles + their HTML element synonyms. The mapping
# normalises author terminology (an author writing "footer" usually
# means the contentinfo landmark).
_LANDMARK_SYNONYMS: dict[str, str] = {
    "main": "main",
    "navigation": "navigation",
    "nav": "navigation",
    "footer": "contentinfo",
    "contentinfo": "contentinfo",
    "header": "banner",
    "banner": "banner",
    "aside": "complementary",
    "complementary": "complementary",
    "search": "search",
    "form": "form",
    "region": "region",
}


def _landmark_words_pattern() -> str:
    """Build a regex alternation of every landmark synonym."""
    return "|".join(sorted(_LANDMARK_SYNONYMS.keys(), key=len, reverse=True))


# Directional keywords that imply "before" or "after" ordering.
_BEFORE_WORDS = ("before", "preceding", "placed before", "comes before", "ahead of")
_AFTER_WORDS = ("after", "following", "placed after", "comes after", "behind")


def _detect_landmark_order_claim(issue_text: str) -> tuple[str, str, str] | None:
    """Find a landmark-order claim in finding prose.

    Returns ``(landmark_a, direction, landmark_b)`` where direction is
    either ``"before"`` or ``"after"``, both landmarks are normalised
    to ARIA role names, or ``None`` if no claim was found.

    Heuristic: looks for the first occurrence of pattern
    ``<LANDMARK_A> ... <BEFORE|AFTER> ... <LANDMARK_B>`` where both
    landmarks come from ``_LANDMARK_SYNONYMS``.
    """
    if not issue_text:
        return None
    lower = issue_text.lower()
    lw = _landmark_words_pattern()
    before_alt = "|".join(_BEFORE_WORDS)
    after_alt = "|".join(_AFTER_WORDS)

    # Try "after" first (the verified university / A11Y Project failure mode).
    # The non-greedy gap of up to ~120 chars allows for VPAT prose
    # ("the main content is placed after secondary navigation and
    # footer elements in the accessibility tree...") without
    # over-spanning into a second sentence.
    for direction, alt in (("after", after_alt), ("before", before_alt)):
        pattern = re.compile(
            rf"\b({lw})\b[^.]{{0,120}}?\b(?:{alt})\b[^.]{{0,120}}?\b({lw})\b",
            re.IGNORECASE,
        )
        m = pattern.search(lower)
        if m:
            a = _LANDMARK_SYNONYMS[m.group(1).lower()]
            b = _LANDMARK_SYNONYMS[m.group(2).lower()]
            if a == b:
                # Self-claim ("nav before nav") -- not interpretable
                # as a directional comparison.
                continue
            return (a, direction, b)
    return None


def _first_position_of_landmark(role: str, a11y_tree: dict | None) -> int | None:
    """Find the first node-index of the given ARIA landmark role in
    the captured accessibility tree.

    Returns the index (DOM document order — Playwright's a11y tree is
    flat-listed in document order) or ``None`` when the landmark is
    absent.
    """
    if not a11y_tree or not isinstance(a11y_tree, dict):
        return None
    nodes = a11y_tree.get("nodes") or []
    for i, node in enumerate(nodes):
        if not isinstance(node, dict):
            continue
        r_obj = node.get("role")
        if isinstance(r_obj, dict):
            r_val = (r_obj.get("value") or "").lower()
        else:
            r_val = (str(r_obj) if r_obj else "").lower()
        if r_val == role.lower():
            return i
    return None


def verify_landmark_order_claim(
    issue_text: str, a11y_tree: dict | None,
) -> str:
    """Verify a landmark-order claim against the captured a11y tree.

    Returns one of:
      - ``"no_claim"``: the finding makes no landmark-order assertion;
        leave it alone.
      - ``"verified"``: the claim matches the captured a11y tree
        positions; the finding stands.
      - ``"contradicted"``: the claim contradicts the captured a11y
        tree (the verified-on-real-data failure mode); the finding
        should be dropped or demoted.
      - ``"unverifiable"``: a claim was detected but either landmark
        could not be located in the captured a11y tree; conservative
        default — keep the finding for the judge to evaluate.
    """
    claim = _detect_landmark_order_claim(issue_text)
    if not claim:
        return "no_claim"
    a, direction, b = claim
    pos_a = _first_position_of_landmark(a, a11y_tree)
    pos_b = _first_position_of_landmark(b, a11y_tree)
    if pos_a is None or pos_b is None:
        return "unverifiable"
    claim_says_a_before_b = (direction == "before")
    actual_a_before_b = pos_a < pos_b
    if claim_says_a_before_b == actual_a_before_b:
        return "verified"
    return "contradicted"


def filter_landmark_order_hallucinations(
    findings: list,
    a11y_tree: dict | None,
    *,
    drop: bool = True,
    log_label: str = "",
) -> tuple[list, int]:
    """Drop findings whose landmark-order claim contradicts the
    captured a11y tree.

    Args:
        findings: list of Finding-like objects (must expose ``.issue``).
        a11y_tree: dict from ``capture_data.a11y_tree`` (Playwright's
            full accessibility tree, document order).
        drop: when True, contradicted findings are removed; when False,
            they're kept but downgraded to severity=info with the
            issue text appended with a [CONTRADICTED] marker. Default
            True (drop).
        log_label: optional label included in the log line so the
            operator can trace which SC the drops belonged to.

    Returns:
        ``(kept_findings, dropped_count)``.
    """
    if not findings or not a11y_tree:
        return list(findings), 0
    kept: list = []
    dropped = 0
    for f in findings:
        # Read ``issue`` from either a Finding object or a dict.
        if hasattr(f, "issue"):
            issue_text = getattr(f, "issue", "") or ""
        elif isinstance(f, dict):
            issue_text = f.get("issue", "") or ""
        else:
            kept.append(f)
            continue
        verdict = verify_landmark_order_claim(issue_text, a11y_tree)
        if verdict == "contradicted":
            dropped += 1
            if drop:
                continue
            # Non-drop mode: downgrade severity and annotate.
            if hasattr(f, "severity"):
                from models import Severity
                f.severity = Severity.INFO
                f.issue = (
                    f.issue
                    + " [CONTRADICTED by captured a11y tree -- "
                    "downgraded automatically]"
                )
            elif isinstance(f, dict):
                f["severity"] = "info"
                f["issue"] = (
                    (f.get("issue") or "")
                    + " [CONTRADICTED by captured a11y tree -- "
                    "downgraded automatically]"
                )
        kept.append(f)
    if dropped:
        logger.info(
            "Landmark-order verifier %s: dropped %d finding(s) whose "
            "directional claim contradicted the captured a11y tree",
            log_label or "", dropped,
        )
    return kept, dropped
