"""Cross-criterion verdict consistency reconciliation.

Some WCAG success criteria are strict supersets of others: the stricter
criterion's pass condition is contained in the easier one's. When that
holds, the stricter criterion CANNOT be more conformant than the easier
one -- if a page fails the easier criterion, it necessarily fails the
stricter one too.

The per-SC judge evaluates each criterion in isolation and has no view of
sibling verdicts, so it occasionally produces an impossible pair (verified
on a municipal-government-site run 20260515_230613_ff643865: SC 2.1.3 "Keyboard, No
Exception" came back Supports while SC 2.1.1 "Keyboard" came back Does Not
Support -- 2.1.3 is a superset of 2.1.1 and cannot pass when 2.1.1 fails).

This module runs one deterministic pass after every SC is evaluated and
downgrades any stricter criterion that out-ranks the easier one it depends
on. It is site-agnostic: the dependency table encodes WCAG structure,
which is identical for every page.
"""
from __future__ import annotations

import logging
from typing import Any

from models import ConformanceLevel

logger = logging.getLogger(__name__)


# Stricter SC -> the easier SC it is a strict superset of. The stricter
# criterion's conformance may NOT be better (more conformant) than the
# easier one's. Each pair is a verified strict-superset relationship:
#
#   2.1.3 / 2.1.1  Keyboard (No Exception) drops the timing exception
#                  that 2.1.1 allows -- 2.1.1 failing means functionality
#                  is not keyboard operable at all, so 2.1.3 fails too.
#   1.4.6 / 1.4.3  Contrast (Enhanced) requires 7:1 vs 1.4.3's 4.5:1 --
#                  text below 4.5:1 is necessarily below 7:1.
#   1.4.9 / 1.4.5  Images of Text (No Exception) drops the "customizable"
#                  allowance 1.4.5 grants.
#   2.4.12 / 2.4.11 Focus Not Obscured (Enhanced) forbids ANY obscuring;
#                  2.4.11 only forbids ENTIRE obscuring.
#   2.5.5 / 2.5.8  Target Size (Enhanced) requires 44px vs 2.5.8's 24px --
#                  a target below 24px is necessarily below 44px.
SC_CANNOT_EXCEED: dict[str, str] = {
    "2.1.3": "2.1.1",
    "1.4.6": "1.4.3",
    "1.4.9": "1.4.5",
    "2.4.12": "2.4.11",
    "2.5.5": "2.5.8",
}

# Conformance ranking for the comparison. Higher = more conformant.
# Not Applicable / Not Evaluated are deliberately absent: a pair where
# either side has no real verdict is not comparable and is skipped.
_CONFORMANCE_RANK: dict[ConformanceLevel, int] = {
    ConformanceLevel.DOES_NOT_SUPPORT: 0,
    ConformanceLevel.PARTIALLY_SUPPORTS: 1,
    ConformanceLevel.SUPPORTS: 2,
}


def _result_by_id(results: list[Any]) -> dict[str, Any]:
    """Index TestResult-like objects by their criterion_id."""
    index: dict[str, Any] = {}
    for r in results:
        cid = getattr(r, "criterion_id", None)
        if cid:
            index[cid] = r
    return index


def _is_failing(level: Any) -> bool:
    """True for a worse-than-Supports verdict that must be backed by findings."""
    return level in (
        ConformanceLevel.PARTIALLY_SUPPORTS,
        ConformanceLevel.DOES_NOT_SUPPORT,
    )


def _inherit_findings(
    source_findings: list[Any], stricter_id: str, easier_id: str,
) -> list[Any]:
    """Copy a base criterion's findings onto a stricter superset criterion.

    The failures that fail the easier criterion genuinely fail the
    stricter one too (strict-superset relationship). Each copy gets a
    fresh id and an inheritance note appended to its issue text so the
    report shows where the evidence came from. Original findings are
    left untouched.
    """
    import dataclasses
    from functions.finding_utils import _make_finding_id

    inherited: list[Any] = []
    note = (
        f" [Inherited by SC {stricter_id}: this failure fails SC "
        f"{easier_id}, and SC {stricter_id} is a strict superset of "
        f"SC {easier_id}, so it fails here too.]"
    )
    for f in source_findings:
        if not dataclasses.is_dataclass(f):
            continue
        try:
            inherited.append(dataclasses.replace(
                f,
                id=_make_finding_id(),
                issue=str(getattr(f, "issue", "")) + note,
            ))
        except Exception:
            # Non-replaceable finding shape -- skip rather than crash.
            logger.debug(
                "Cross-SC reconciliation: could not inherit a finding "
                "for SC %s", stricter_id, exc_info=True,
            )
    return inherited


def reconcile_cross_sc_verdicts(results: list[Any]) -> int:
    """Downgrade any stricter SC that out-ranks the easier SC it depends on.

    Args:
        results: the full list of per-SC TestResult objects for one review.

    Returns:
        The number of verdicts that were downgraded. Each downgraded result
        is mutated in place: ``conformance_level`` is lowered to match the
        easier criterion and ``confidence_reasoning`` records why. The
        caller is responsible for re-persisting any mutated result.
    """
    index = _result_by_id(results)
    downgrades = 0

    for stricter_id, easier_id in SC_CANNOT_EXCEED.items():
        stricter = index.get(stricter_id)
        easier = index.get(easier_id)
        if stricter is None or easier is None:
            continue

        stricter_level = getattr(stricter, "conformance_level", None)
        easier_level = getattr(easier, "conformance_level", None)
        stricter_rank = _CONFORMANCE_RANK.get(stricter_level)
        easier_rank = _CONFORMANCE_RANK.get(easier_level)

        # Skip unless BOTH have a real, comparable verdict.
        if stricter_rank is None or easier_rank is None:
            continue

        # Consistent already: the stricter criterion is no more conformant
        # than the easier one.
        if stricter_rank <= easier_rank:
            continue

        # Impossible pair -- the stricter criterion out-ranks its subset.
        # Downgrade the stricter criterion to the easier one's level.
        stricter.conformance_level = easier_level
        note = (
            f" | CROSS-SC RECONCILIATION: {stricter_id} downgraded to "
            f"'{easier_level.value}' to match SC {easier_id} "
            f"('{easier_level.value}'). {stricter_id} is a strict superset "
            f"of {easier_id}; it cannot be more conformant than the "
            f"criterion it contains."
        )
        existing = getattr(stricter, "confidence_reasoning", "") or ""
        stricter.confidence_reasoning = existing + note

        # Carry the evidence with the verdict. A worse-than-Supports
        # verdict must have findings backing it; downgrading the verdict
        # alone would leave "Does Not Support with 0 findings", which is
        # internally inconsistent (flagged by audit_sc). The failures
        # that fail the easier criterion genuinely fail the stricter one
        # too -- it is a strict superset -- so when the stricter check
        # surfaced no findings of its own, inherit the easier criterion's
        # findings (fresh ids, an inheritance note appended to each).
        if _is_failing(easier_level) and not getattr(stricter, "findings", None):
            inherited = _inherit_findings(
                getattr(easier, "findings", None) or [],
                stricter_id, easier_id,
            )
            if inherited:
                stricter.findings = inherited
                logger.info(
                    "Cross-SC reconciliation: SC %s inherited %d finding(s) "
                    "from SC %s so the downgraded verdict has evidence",
                    stricter_id, len(inherited), easier_id,
                )

        downgrades += 1
        logger.info(
            "Cross-SC reconciliation: SC %s downgraded %s -> %s "
            "(cannot exceed SC %s)",
            stricter_id,
            stricter_level.value if stricter_level else "?",
            easier_level.value,
            easier_id,
        )

    if downgrades:
        logger.info(
            "Cross-SC reconciliation: %d verdict(s) downgraded for "
            "superset consistency.", downgrades,
        )
    return downgrades
