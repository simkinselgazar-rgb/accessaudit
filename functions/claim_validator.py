"""Post-judge validation of structured measurement claims against capture.

WHY THIS EXISTS
---------------
The judge sometimes states a specific measured value -- most often a
contrast ratio -- that the deterministic capture never produced. It borrows
a number from an unrelated measurement block, or invents one. Showing the
model the correct data and a "do not fabricate" rule does not reliably stop
this on smaller models: it was verified to recur with the rule present in
the prompt.

Prompt rules are advice. This module is enforcement.

HOW IT WORKS (no hard-coded thresholds, no central SC map)
----------------------------------------------------------
The judge tool schema requires every finding to carry a structured
``cited_measurements`` array: one entry per measured value its prose cites,
each entry ``{selector, metric, value}``. The judge is told NOT to record
WCAG threshold/requirement values there -- only measured values -- so there
is nothing to parse out of prose and no threshold list to maintain.

Each SC check module declares its own ``measurement_sources`` -- a mapping
from a metric name to the (capture attribute, field) holding that metric's
deterministic ground truth. The validator is generic: for every cited
measurement whose metric the SC knows a source for, it looks the value up
against the captured data and demotes the finding if it does not match.
Knowledge lives in the per-SC modules, where the project's modular design
puts it.
"""
from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

# Rounding slack for numeric comparisons: the judge may round 4.53 -> 4.5.
_NUMERIC_TOLERANCE = 0.5


def _normalize(selector: str) -> str:
    """Selector normalization shared with the source-attribution validator."""
    from functions.parser import _normalize_selector
    return _normalize_selector((selector or "").strip().lower())


def _selectors_match(a: str, b: str) -> bool:
    """Lenient selector match: exact equality, or substring containment once
    both sides are specific enough. Same rule the other validators use."""
    if not a or not b:
        return False
    if a == b:
        return True
    return len(a) >= 5 and len(b) >= 5 and (a in b or b in a)


def _coerce_finding_view(ff: Any) -> dict[str, Any] | None:
    """Return a mutable dict view of a finding, or None if unreadable.

    Mirrors validate_source_attribution's coercion so both validators
    accept the same inputs (judge tool-call dicts OR Finding objects).
    """
    if isinstance(ff, dict):
        return ff
    if hasattr(ff, "to_dict"):
        try:
            return dict(ff.to_dict())
        except Exception:
            return None
    if hasattr(ff, "issue"):
        return {
            "element": getattr(ff, "element", ""),
            "css_selector": getattr(ff, "css_selector", ""),
            "issue": getattr(ff, "issue", ""),
            "impact": getattr(ff, "impact", ""),
            "recommendation": getattr(ff, "recommendation", ""),
            "severity": getattr(getattr(ff, "severity", ""), "value",
                                getattr(ff, "severity", "")),
            "source": getattr(ff, "source", ""),
            "cited_measurements": getattr(ff, "cited_measurements", []),
        }
    return None


def _measured_values(
    capture_data: Any, attr: str, field: str, selector: str,
) -> list[Any]:
    """Every value the named capture source recorded for this claim.

    Two source shapes are supported:

      * Page-level fact -- the capture attribute is a DICT of named
        facts (e.g. dynamic_content: {hasAnimations: False, ...}). The
        fact is selector-agnostic; its single value is returned.
      * Element-level measurement -- the capture attribute is a LIST of
        {selector, <field>} dicts; entries whose selector matches the
        finding's selector are returned.

    Empty list means the claim was never measured -- any value the
    finding cites for it is therefore unsupported.
    """
    source = getattr(capture_data, attr, None)

    # Page-level fact (dict source): selector is irrelevant.
    if isinstance(source, dict):
        val = source.get(field)
        return [] if val is None else [val]

    # Element-level measurement (list source): match on selector.
    sel_n = _normalize(selector)
    if not sel_n:
        return []
    out: list[Any] = []
    for entry in source or []:
        if not isinstance(entry, dict):
            continue
        if not _selectors_match(sel_n, _normalize(entry.get("selector") or "")):
            continue
        val = entry.get(field)
        if val is not None:
            out.append(val)
    return out


def _as_number(v: Any) -> float | None:
    """Coerce a value to float, or None if it is not numeric.

    The judge reports cited values as strings ("4.44", "fixed") because the
    tool schema gives the field one stable type. Capture data stores numbers
    natively. This bridges the two: a numeric metric compares numerically
    regardless of which side is a string.
    """
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, str):
        try:
            return float(v.strip())
        except ValueError:
            return None
    return None


def _value_supported(claimed: Any, measured: list[Any]) -> bool:
    """True if the claimed value matches some measured value.

    Numeric values (on either side, including numeric strings) compare
    within a rounding tolerance; non-numeric values compare
    case-insensitively as strings.
    """
    if not measured:
        return False
    claimed_num = _as_number(claimed)
    if claimed_num is not None:
        for m in measured:
            m_num = _as_number(m)
            if m_num is not None and abs(claimed_num - m_num) <= _NUMERIC_TOLERANCE:
                return True
        return False
    claimed_s = str(claimed).strip().lower()
    return any(str(m).strip().lower() == claimed_s for m in measured)


def validate_measurement_claims(
    findings: list[Any],
    capture_data: Any,
    measurement_sources: dict[str, tuple[str, str]] | None,
) -> tuple[list[dict[str, Any]], int]:
    """Demote findings whose structured measurement claims are unsupported.

    Args:
        findings: judge output findings (dicts or Finding objects). Each
            may carry a ``cited_measurements`` list of {selector, metric,
            value} entries.
        capture_data: the CaptureData for the page under test.
        measurement_sources: the SC check's own metric -> (capture attr,
            field) map (``BaseCheck.measurement_sources``). When a metric
            is not in this map the SC has no deterministic ground truth
            for it and the claim is left alone.

    Returns:
        (validated_findings, demoted_count). A demoted finding has its
        ``source`` rewritten to ``judge_inference`` and its ``issue``
        annotated, so the auditor sees the value is a model inference.
    """
    sources = measurement_sources or {}
    validated: list[dict[str, Any]] = []
    demoted = 0

    for ff in findings:
        view = _coerce_finding_view(ff)
        if view is None:
            validated.append(ff)
            continue

        cited = view.get("cited_measurements")
        if not isinstance(cited, list) or not cited:
            validated.append(view)
            continue

        unverified: list[str] = []
        for m in cited:
            if not isinstance(m, dict):
                continue
            metric = str(m.get("metric") or "").strip()
            source = sources.get(metric)
            if not source:
                # This SC declares no ground-truth source for this metric;
                # nothing to verify against, leave the claim alone.
                continue
            attr, field = source
            sel = m.get("selector") or view.get("css_selector") or ""
            claimed = m.get("value")
            measured = _measured_values(capture_data, attr, field, sel)
            if not _value_supported(claimed, measured):
                if measured:
                    detail = (
                        f"cited {metric}={claimed} for {sel!r}, but the "
                        f"deterministic measurement(s) are {measured}"
                    )
                else:
                    detail = (
                        f"cited {metric}={claimed} for {sel!r}, but this "
                        f"element has no deterministic {metric} measurement"
                    )
                unverified.append(detail)

        if not unverified:
            validated.append(view)
            continue

        detail = "; ".join(unverified)
        view["source"] = "judge_inference"
        view["issue"] = (
            str(view.get("issue") or "")
            + f" [UNVERIFIED MEASUREMENT: {detail}; treated as a model "
            f"inference, not a measured value]"
        )
        demoted += 1
        logger.info(
            "claim-validator: demoted finding to judge_inference -- %s",
            detail,
        )
        validated.append(view)

    return validated, demoted
