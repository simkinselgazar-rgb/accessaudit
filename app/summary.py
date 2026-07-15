"""Summary computation helpers.

Both helpers tally conformance verdicts and finding counts off a list of
results — one tolerates either ``TestResult`` objects or dicts, the
other is dict-only. They are leaf utilities with no orchestrator
dependencies.
"""
from __future__ import annotations


def _compute_summary(results) -> dict:
    """Compute summary statistics from a mix of ``TestResult`` objects
    and/or result dicts. The resume path's ``type("R", (), ...)()``
    fallback has historically leaked raw dicts into this list; the
    function now accepts either without crashing."""
    s = {"supports": 0, "partially_supports": 0, "does_not_support": 0,
         "not_applicable": 0, "not_evaluated": 0, "total_findings": 0,
         "total": len(results), "avg_confidence": 0}
    confs = []
    for r in results:
        # Tolerate dict entries (resume path) alongside TestResult objects.
        if isinstance(r, dict):
            level = r.get("conformance_level", "Not Evaluated")
            findings = r.get("findings", []) or []
            confidence = float(r.get("confidence", 0) or 0)
        else:
            level = getattr(r, "conformance_level", "Not Evaluated")
            findings = getattr(r, "findings", []) or []
            confidence = float(getattr(r, "confidence", 0) or 0)
        if hasattr(level, 'value'):
            level = level.value
        if level == "Supports":
            s["supports"] += 1
        elif level == "Partially Supports":
            s["partially_supports"] += 1
        elif level == "Does Not Support":
            s["does_not_support"] += 1
        elif level == "Not Applicable":
            s["not_applicable"] += 1
        else:
            s["not_evaluated"] += 1
        s["total_findings"] += len(findings)
        if confidence > 0:
            confs.append(confidence)
    s["avg_confidence"] = round(sum(confs) / len(confs), 3) if confs else 0
    return s


def _compute_summary_from_dicts(results: list[dict]) -> dict:
    """Compute summary from result dicts."""
    s = {"supports": 0, "partially_supports": 0, "does_not_support": 0,
         "not_applicable": 0, "not_evaluated": 0, "total_findings": 0,
         "total": len(results), "avg_confidence": 0}
    confs = []
    for r in results:
        level = r.get("conformance_level", "Not Evaluated")
        if level == "Supports":
            s["supports"] += 1
        elif level == "Partially Supports":
            s["partially_supports"] += 1
        elif level == "Does Not Support":
            s["does_not_support"] += 1
        elif level == "Not Applicable":
            s["not_applicable"] += 1
        else:
            s["not_evaluated"] += 1
        s["total_findings"] += len(r.get("findings", []))
        c = r.get("confidence", 0)
        if c > 0:
            confs.append(c)
    s["avg_confidence"] = round(sum(confs) / len(confs), 3) if confs else 0
    return s
