"""Shared helpers for report exporters (HTML/ACR, DOCX, XLSX, PDF).

Keep serialization helpers, the Section 508 FPC tables, and the
``ResultProxy`` adapter here so every exporter renders consistent
strings without carrying its own copy. Every exporter imports from
this module.
"""
from __future__ import annotations

from typing import Any

from models import ConformanceLevel, TestResult


def conformance_str(level: ConformanceLevel | str) -> str:
    """Return a conformance level as a plain string, whatever the input type."""
    if isinstance(level, ConformanceLevel):
        return level.value
    return str(level)


def severity_str(sev: Any) -> str:
    """Return a severity as a plain string."""
    if hasattr(sev, "value"):
        return sev.value
    return str(sev)


def criterion_sort_key(criterion_id: str) -> tuple:
    """Turn '1.4.3' into (1, 4, 3) so criteria sort numerically, not lexically."""
    parts: list[int] = []
    for segment in criterion_id.split("."):
        try:
            parts.append(int(segment))
        except ValueError:
            parts.append(0)
    return tuple(parts)


# ── Section 508 Functional Performance Criteria ─────────────────────────────
# Maps FPC codes to the WCAG success-criteria IDs they encompass.

FPC_MAPPING: dict[str, list[str]] = {
    "302.1": [
        "1.1.1", "1.2.1", "1.2.2", "1.2.3", "1.2.5",
        "1.3.1", "1.3.2", "1.3.3", "1.4.1", "1.4.2",
    ],
    "302.2": [
        "1.2.1", "1.2.2", "1.2.4", "1.2.6",
        "1.4.2",
    ],
    "302.3": [
        "1.4.1", "1.4.3", "1.4.4", "1.4.6", "1.4.8",
        "1.4.10", "1.4.11", "1.4.12",
    ],
    "302.4": [
        "1.1.1", "1.2.1", "1.2.2", "1.2.4", "1.2.5",
        "1.4.2",
    ],
    "302.5": [
        "1.2.2", "1.2.4",
        "1.4.2",
    ],
    "302.6": [
        "2.1.1", "2.1.2", "2.1.4",
        "2.4.1", "2.4.3", "2.4.7",
        "3.2.1", "3.2.2",
    ],
    "302.7": [
        "1.3.1", "1.3.2", "1.3.3",
        "2.4.6", "2.4.10",
        "3.3.1", "3.3.2", "3.3.3", "3.3.4",
    ],
    "302.8": [
        "2.2.1", "2.2.2", "2.3.1",
    ],
    "302.9": [
        "1.3.4", "1.3.5", "2.5.1", "2.5.2", "2.5.4",
    ],
}

FPC_NAMES: dict[str, str] = {
    "302.1": "Without Vision",
    "302.2": "With Limited Vision",
    "302.3": "Without Perception of Color",
    "302.4": "Without Hearing",
    "302.5": "With Limited Hearing",
    "302.6": "Without Speech",
    "302.7": "With Limited Manipulation",
    "302.8": "With Limited Reach and Strength",
    "302.9": "With Limited Language, Cognitive, and Learning Abilities",
}


# ── Result proxy (uniform attribute access over dict OR TestResult) ─────────

class ResultProxy:
    """Wraps a dict or TestResult so exporters can access fields uniformly.

    Lets every exporter accept both live ``TestResult`` objects (from a
    single-page run) and plain dicts (from cross-page aggregation) without
    branching on the type at each field access.
    """

    _LIST_FIELDS = frozenset({"findings", "tt_results", "wcag_versions"})

    def __init__(self, data):
        if isinstance(data, dict):
            self._d = data
        else:
            # TestResult / similar object -- convert to dict. Keep the
            # original Finding / TTSubTestResult objects under _raw keys
            # so exporters that want object access can still get it.
            self._d = data.to_dict() if hasattr(data, "to_dict") else vars(data)
            self._d["_findings_raw"] = getattr(data, "findings", [])
            self._d["_tt_results_raw"] = getattr(data, "tt_results", [])

    def __getattr__(self, name):
        if name == "_d":
            raise AttributeError
        default: Any = [] if name in self._LIST_FIELDS else ""
        return self._d.get(name, default)

    def to_dict(self) -> dict:
        return {k: v for k, v in self._d.items() if not k.startswith("_")}


def wrap_results(results) -> list[ResultProxy]:
    """Wrap every result in a ``ResultProxy`` for uniform attribute access.

    Works for list input containing a mix of dicts (aggregated multi-page
    runs) and ``TestResult`` objects (single-page runs).
    """
    return [ResultProxy(r) for r in results]


# ── Functional Performance Criteria row builder ─────────────────────────────

_FPC_SEVERITY_ORDER: dict[str, int] = {
    ConformanceLevel.DOES_NOT_SUPPORT.value: 0,
    ConformanceLevel.PARTIALLY_SUPPORTS.value: 1,
    ConformanceLevel.NOT_EVALUATED.value: 2,
    ConformanceLevel.SUPPORTS.value: 3,
    ConformanceLevel.NOT_APPLICABLE.value: 4,
}


def build_fpc_rows(results: list[TestResult]) -> list[dict[str, Any]]:
    """Build Section 508 FPC conformance rows from SC-level results.

    For each FPC code we look at every SC it encompasses and pick the
    worst conformance as the FPC verdict. Remarks concatenate the
    summaries of any failing SCs so an auditor can see at a glance
    which criteria drive the FPC verdict.
    """
    result_map = {r.criterion_id: r for r in results}
    rows: list[dict[str, Any]] = []
    for code in sorted(FPC_MAPPING):
        mapped = FPC_MAPPING[code]
        matched = [result_map[cid] for cid in mapped if cid in result_map]
        if not matched:
            conformance = ConformanceLevel.NOT_APPLICABLE.value
            remark = "No applicable criteria evaluated."
        else:
            worst = min(
                matched,
                key=lambda r: _FPC_SEVERITY_ORDER.get(
                    conformance_str(r.conformance_level), 5,
                ),
            )
            conformance = conformance_str(worst.conformance_level)
            failing = [
                r for r in matched
                if conformance_str(r.conformance_level) in (
                    ConformanceLevel.DOES_NOT_SUPPORT.value,
                    ConformanceLevel.PARTIALLY_SUPPORTS.value,
                )
            ]
            remark = "; ".join(
                f"{r.criterion_id}: {r.summary}" for r in failing if r.summary
            ) if failing else ""
        rows.append({
            "code": code,
            "name": FPC_NAMES.get(code, code),
            "conformance": conformance,
            "remark": remark,
        })
    return rows
