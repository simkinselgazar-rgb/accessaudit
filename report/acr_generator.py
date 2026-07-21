"""ACR (Accessibility Conformance Report) generator.

Generates VPAT-style reports in HTML and JSON formats for Section 508,
WCAG-only, and International (EN 301 549) report types.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, select_autoescape

from config import TEMPLATES_DIR, PROJECT_DIR
from models import TestResult, ConformanceLevel, ReviewMeta

logger = logging.getLogger(__name__)


from report.utils import (
    FPC_MAPPING,
    FPC_NAMES,
    ResultProxy as _ResultProxy,
    conformance_str as _conformance_value,
    criterion_sort_key as _sort_key,
    severity_str as _severity_value,
    wrap_results as _normalize_results,
)

# Template name lookup by report format.
_TEMPLATE_MAP: dict[str, str] = {
    "508": "acr_report_508.html",
    "acr": "acr_report_508.html",  # frontend ships "acr" — alias to 508
    "int": "acr_report_int.html",
    "wcag": "acr_report_wcag.html",
}


def _split_by_level(results) -> dict[str, list[dict]]:
    """Partition results into A / AA / AAA buckets.
    Accepts both TestResult objects and plain dicts (from site crawl aggregation).
    """
    buckets: dict[str, list[dict]] = {"A": [], "AA": [], "AAA": []}
    for r in results:
        if isinstance(r, dict):
            row = dict(r)
            key = row.get("level", "A").upper().strip()
            row["conformance_display"] = _conformance_value(row.get("conformance_level", "Not Evaluated"))
        else:
            key = r.level.upper().strip()
            row = r.to_dict()
            row["conformance_display"] = _conformance_value(r.conformance_level)
        if key not in buckets:
            key = "A"
        buckets[key].append(row)
    for key in buckets:
        buckets[key].sort(key=lambda x: _sort_key(x["criterion_id"]))
    return buckets


def _compute_summary(results: list[TestResult]) -> dict[str, Any]:
    """Compute aggregate statistics across all results."""
    total = len(results)
    supports = 0
    partially = 0
    does_not = 0
    na = 0
    ne = 0
    total_findings = 0
    confidences: list[float] = []

    for r in results:
        val = _conformance_value(r.conformance_level)
        if val == ConformanceLevel.SUPPORTS.value:
            supports += 1
        elif val == ConformanceLevel.PARTIALLY_SUPPORTS.value:
            partially += 1
        elif val == ConformanceLevel.DOES_NOT_SUPPORT.value:
            does_not += 1
        elif val == ConformanceLevel.NOT_APPLICABLE.value:
            na += 1
        elif val == ConformanceLevel.NOT_EVALUATED.value:
            ne += 1
        total_findings += len(r.findings)
        if r.confidence > 0:
            confidences.append(r.confidence)

    avg_confidence = round(sum(confidences) / len(confidences), 3) if confidences else 0.0

    return {
        "total": total,
        "supports": supports,
        "partially": partially,
        "does_not": does_not,
        "NA": na,
        "NE": ne,
        "total_findings": total_findings,
        "avg_confidence": avg_confidence,
    }


def _build_fpc_table(
    results: list[TestResult],
) -> list[dict[str, Any]]:
    """Build the FPC conformance table for Section 508 reports.

    For each FPC code the conformance is the *worst* conformance among the
    mapped criteria that are present in the results.
    """
    result_map: dict[str, TestResult] = {r.criterion_id: r for r in results}

    severity_order = {
        ConformanceLevel.DOES_NOT_SUPPORT.value: 0,
        ConformanceLevel.PARTIALLY_SUPPORTS.value: 1,
        ConformanceLevel.NOT_EVALUATED.value: 2,
        ConformanceLevel.SUPPORTS.value: 3,
        ConformanceLevel.NOT_APPLICABLE.value: 4,
    }

    rows: list[dict[str, Any]] = []
    for code in sorted(FPC_MAPPING):
        mapped_criteria = FPC_MAPPING[code]
        matched = [result_map[cid] for cid in mapped_criteria if cid in result_map]
        if not matched:
            conformance = ConformanceLevel.NOT_APPLICABLE.value
            remark = "No applicable criteria evaluated."
        else:
            worst = min(
                matched,
                key=lambda r: severity_order.get(
                    _conformance_value(r.conformance_level), 5
                ),
            )
            conformance = _conformance_value(worst.conformance_level)
            failing = [
                r for r in matched
                if _conformance_value(r.conformance_level)
                in (
                    ConformanceLevel.DOES_NOT_SUPPORT.value,
                    ConformanceLevel.PARTIALLY_SUPPORTS.value,
                )
            ]
            if failing:
                remark = "; ".join(
                    f"{r.criterion_id}: {r.summary}" for r in failing if r.summary
                )
            else:
                remark = ""
        rows.append({
            "code": code,
            "name": FPC_NAMES.get(code, code),
            "conformance": conformance,
            "criteria": mapped_criteria,
            "remark": remark,
        })
    return rows


def _collect_tt_results(results: list[TestResult]) -> list[dict[str, Any]]:
    """Flatten TT sub-test results across all criteria."""
    rows: list[dict[str, Any]] = []
    for r in results:
        tt_list = r.tt_results
        if not tt_list:
            continue
        for tt in tt_list:
            if isinstance(tt, dict):
                rows.append({
                    "criterion_id": r.criterion_id,
                    "criterion_name": r.criterion_name,
                    "tt_id": tt.get("tt_id", ""),
                    "name": tt.get("name", ""),
                    "result": tt.get("result", ""),
                })
            else:
                rows.append({
                    "criterion_id": r.criterion_id,
                    "criterion_name": r.criterion_name,
                    "tt_id": tt.tt_id if hasattr(tt, "tt_id") else "",
                    "name": tt.name if hasattr(tt, "name") else "",
                    "result": tt.result.value if hasattr(tt.result, "value") else str(tt.result),
                })
    rows.sort(key=lambda x: x.get("tt_id", ""))
    return rows


def _strip_finding(f) -> dict:
    """Remove internal-only fields from a finding for client-facing output."""
    d = dict(f) if isinstance(f, dict) else f
    d.pop("source", None)
    d.pop("decision", None)
    d.pop("decision_reason", None)
    d.pop("page_url", None)
    d.pop("internal_remediation_note", None)
    return d


def _strip_internal_result_keys(r: dict) -> dict:
    """Remove confidence/AI/programmatic internals from a result dict."""
    d = dict(r)
    for key in list(d.keys()):
        if "confidence" in key or "programmatic" in key or "ai_" in key or "code_ai" in key or "at_sim" in key:
            d.pop(key, None)
    return d


def _load_linked_documents(review_dir: Path, client_mode: bool) -> list[dict[str, Any]]:
    """Load document_results.json (linked documents tested alongside the
    page) for the 'Linked Documents Tested' report section. Returns
    ``[{url, criteria_count, results}, ...]`` — empty when absent."""
    doc_path = review_dir / "document_results.json"
    if not doc_path.exists():
        return []
    try:
        doc_results = json.loads(doc_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("Malformed JSON in %s: %s", doc_path, exc)
        return []
    if not isinstance(doc_results, list):
        logger.warning("Unexpected shape in %s: expected a list", doc_path)
        return []
    docs: list[dict[str, Any]] = []
    for entry in doc_results:
        if not isinstance(entry, dict):
            continue
        results = entry.get("results", []) or []
        if client_mode:
            cleaned = []
            for r in results:
                if isinstance(r, dict):
                    r = _strip_internal_result_keys(r)
                    r["findings"] = [_strip_finding(f) for f in r.get("findings", [])]
                cleaned.append(r)
            results = cleaned
        docs.append({
            "url": entry.get("url", ""),
            "criteria_count": len(results),
            "results": results,
        })
    return docs


def _collect_findings(results: list[TestResult]) -> list[dict[str, Any]]:
    """Flatten all findings across criteria."""
    findings: list[dict[str, Any]] = []
    for r in results:
        for f in r.findings:
            entry = f.to_dict() if hasattr(f, "to_dict") else dict(f)
            entry["criterion_id"] = r.criterion_id
            entry["criterion_name"] = r.criterion_name
            findings.append(entry)
    # Sort by severity weight then criterion
    sev_order = {"high": 0, "medium": 1, "low": 2, "info": 3}
    findings.sort(key=lambda x: (sev_order.get(x.get("severity", "info"), 4), x.get("criterion_id", "")))
    return findings


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def generate_acr_report(
    results: list[TestResult],
    meta: ReviewMeta,
    review_dir: str | Path,
    client_mode: bool = False,
) -> dict[str, str]:
    """Generate HTML and JSON ACR reports.

    Parameters
    ----------
    results : list[TestResult]
        Evaluated criteria results.
    meta : ReviewMeta
        Review metadata (format, product name, etc.).
    review_dir : str | Path
        Destination directory for the review.  Reports are written to
        ``<review_dir>/report/``.
    client_mode : bool
        If True, generate client-facing reports that omit confidence
        scores, AI methodology language, finding source attribution,
        and internal technical details.

    Returns
    -------
    dict[str, str]
        Mapping of format name to output file path, e.g.
        ``{"html": "/.../report/acr_report.html", "json": "/.../report/acr_report.json"}``.
    """
    review_dir = Path(review_dir)
    report_dir = review_dir / "report"

    # Normalize: accept both TestResult objects and plain dicts
    results = _normalize_results(results)
    report_dir.mkdir(parents=True, exist_ok=True)

    report_format = getattr(meta, "report_format", "508").lower()
    if report_format not in _TEMPLATE_MAP:
        logger.warning("Unknown report_format '%s'; falling back to '508'.", report_format)
        report_format = "508"

    # Compute context
    summary = _compute_summary(results)
    levels = _split_by_level(results)
    tt_rows = _collect_tt_results(results)
    all_findings = _collect_findings(results)
    fpc_rows = _build_fpc_table(results) if report_format in ("508", "acr") else []

    generated_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    # For client mode: strip source from findings, strip confidence, sanitize AI language
    if client_mode:
        all_findings = [_strip_finding(f) for f in all_findings]
        # The per-criterion dicts embed their own findings lists — strip
        # those too, or the client JSON ships the internals the flat
        # list just removed.
        levels = {
            lvl: [
                {**r, "findings": [_strip_finding(f) for f in r.get("findings", [])]}
                if isinstance(r, dict) else r
                for r in rows
            ]
            for lvl, rows in levels.items()
        }

        import re as _re
        _ai_patterns = [
            (r"AI-powered\s+", ""),
            (r"AI-driven\s+", ""),
            (r"ai-powered\s+", ""),
            (r",?\s*AI-powered visual analysis,?\s*", ", "),
            (r",?\s*and AI-powered[^,]*,?\s*", " "),
        ]
        eval_methods = getattr(meta, "evaluation_methods", "") or ""
        for pat, repl in _ai_patterns:
            eval_methods = _re.sub(pat, repl, eval_methods)
        eval_methods = _re.sub(r"\s+", " ", eval_methods).strip()
        eval_methods = _re.sub(r"^,\s*|,\s*$", "", eval_methods)
        if isinstance(meta, ReviewMeta):
            meta = ReviewMeta(**{**meta.__dict__, "evaluation_methods": eval_methods})

    suffix = "_client" if client_mode else ""

    # "Linked Documents Tested" — documents discovered on the page and
    # tested alongside it (document_results.json, single-page path).
    linked_documents = _load_linked_documents(review_dir, client_mode)

    context: dict[str, Any] = {
        "meta": meta.to_dict(),
        "summary": summary,
        "level_a": levels.get("A", []),
        "level_aa": levels.get("AA", []),
        "level_aaa": levels.get("AAA", []),
        "tt_results": tt_rows,
        "findings": all_findings,
        "fpc_table": fpc_rows,
        "generated_at": generated_at,
        "report_format": report_format,
        "client_mode": client_mode,
        "linked_documents": linked_documents,
        "documents_tested": len(linked_documents),
    }

    # ---- HTML report ----
    templates_dir = Path(__file__).parent / "templates"
    env = Environment(
        loader=FileSystemLoader(str(templates_dir)),
        autoescape=select_autoescape(["html"]),
    )
    template_name = _TEMPLATE_MAP[report_format]
    if client_mode and report_format in ("508", "acr"):
        template_name = "acr_report_508_client.html"
        try:
            from report.docx_exporter import _SEC508_REFS
            context["sec508_refs"] = _SEC508_REFS
        except ImportError:
            context["sec508_refs"] = {}
    html_path = report_dir / f"acr_report{suffix}.html"
    try:
        template = env.get_template(template_name)
        html_content = template.render(**context)
        html_path.write_text(html_content, encoding="utf-8")
        logger.info("HTML report written to %s", html_path)
    except Exception as html_err:
        # Never let a template problem block the JSON/XLSX exports --
        # reviewers still need the structured outputs even if the HTML
        # view is broken. Log loudly and continue.
        logger.error(
            "HTML report render failed (non-fatal, JSON still writes): %s",
            html_err,
        )
        html_path = None

    # ---- JSON report ----
    json_data: dict[str, Any] = {
        "generated_at": generated_at,
        "report_format": report_format,
        "meta": meta.to_dict(),
        "summary": summary,
        "criteria": {
            "level_a": levels.get("A", []),
            "level_aa": levels.get("AA", []),
            "level_aaa": levels.get("AAA", []),
        },
        "findings": all_findings,
    }
    if linked_documents:
        json_data["linked_documents"] = linked_documents
    if not client_mode:
        json_data["tt_results"] = tt_rows
    if report_format == "508" and not client_mode:
        json_data["fpc_table"] = fpc_rows

    # Strip internal fields from JSON in client mode
    if client_mode:
        json_data.pop("tt_results", None)
        json_data["summary"].pop("avg_confidence", None)
        meta_d = json_data["meta"]
        meta_d.pop("model_used", None)
        for level_results in json_data["criteria"].values():
            for r in level_results:
                d = r if isinstance(r, dict) else r.__dict__
                for key in list(d.keys()):
                    if "confidence" in key or "programmatic" in key or "ai_" in key or "code_ai" in key or "at_sim" in key:
                        d.pop(key, None)

    json_path = report_dir / f"acr_report{suffix}.json"
    json_path.write_text(json.dumps(json_data, indent=2, default=str), encoding="utf-8")
    logger.info("JSON report written to %s", json_path)

    return {"html": str(html_path), "json": str(json_path)}
