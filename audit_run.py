"""Post-run audit for a completed review.

Callable as a library (via ``audit_review``) from the queue worker, or as
a CLI:

    python audit_run.py <review_id>

The audit walks every SC subdirectory under ``reviews/<id>/tests/`` and
returns/prints a structured report of bugs (must fix) and warnings (worth
attention). This catches classes of failure the orchestration cannot:

- A failing verdict shipped with an empty findings list (judge silently
  dropped everything).
- Findings missing element / issue / impact / recommendation fields.
- Judge context containing ``first N`` truncation markers.
- Parser text-fallback (structured tool call was dropped).

The library return shape is stable and JSON-serializable; the queue
worker writes it to ``reviews/<id>/audit.json`` after every run.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any


def audit_review(review_id: str, *, reviews_root: Path | None = None) -> dict[str, Any]:
    """Audit one review and return a structured report.

    ``reviews_root`` lets callers point at an alternate reviews directory
    (useful from tests). Defaults to ``reviews/`` under the CWD.
    """
    if reviews_root is None:
        reviews_root = Path("reviews")
    review_dir = reviews_root / review_id
    report: dict[str, Any] = {
        "review_id": review_id,
        "stats": {
            "completed": 0, "supports": 0, "partial": 0, "dns": 0, "na": 0, "ne": 0,
            "needs_review": 0, "total_findings": 0,
        },
        "bugs": [],
        "warns": [],
        "fatal": None,
    }

    # Bypass telemetry summary -- always attempt; empty file means clean run.
    # Runs that died before tests still surface their bypass log here.
    try:
        from functions.bypass_log import summarize_bypasses
        bypass_summary = summarize_bypasses(review_dir)
        report["bypasses"] = {
            "total": bypass_summary.get("total", 0),
            "data_lost_count": bypass_summary.get("data_lost_count", 0),
            "by_category": bypass_summary.get("by_category", {}),
            "by_severity": bypass_summary.get("by_severity", {}),
            "by_source": bypass_summary.get("by_source", {}),
        }
    except Exception as exc:
        report["bypasses"] = {"total": 0, "summary_error": str(exc)}

    data_lost = report["bypasses"].get("data_lost_count", 0)
    if data_lost:
        # Categorize by source so the message reflects severity: a lost
        # judge/vision/text LLM call (all route through functions/llm.py)
        # means a VERDICT was built on missing evidence; embeddings /
        # sc_retrieval losses only degrade dedup + per-SC guidance retrieval
        # (quality, not the verdict); a code chunk skip degrades code analysis.
        by_source = report["bypasses"].get("by_source", {}) or {}
        verdict_loss = sum(n for s, n in by_source.items() if "llm.py" in s)
        retrieval_loss = sum(
            n for s, n in by_source.items()
            if "embeddings" in s or "sc_retrieval" in s
        )
        code_loss = sum(n for s, n in by_source.items() if "code_analyzer" in s)
        other = data_lost - verdict_loss - retrieval_loss - code_loss
        parts_msg = []
        if verdict_loss:
            parts_msg.append(
                f"{verdict_loss} LLM call(s) fully lost (judge/vision/audio via the "
                f"gateway) -- any verdict relying on those was produced with missing "
                f"evidence and is NOT trustworthy"
            )
        if retrieval_loss:
            parts_msg.append(
                f"{retrieval_loss} embedding/retrieval loss(es) -- finding dedup and "
                f"per-SC guidance retrieval degraded (quality, not verdicts; usually a "
                f"down embeddings host)"
            )
        if code_loss:
            parts_msg.append(f"{code_loss} code-analysis chunk(s) skipped")
        if other:
            parts_msg.append(f"{other} other data-loss event(s)")
        report["bugs"].append(
            f"{data_lost} high-severity data-loss bypasses recorded -- "
            + "; ".join(parts_msg)
            + ". Inspect bypass_log.jsonl."
        )

    if not review_dir.exists():
        report["fatal"] = f"review dir not found: {review_dir}"
        return report

    tests_dir = review_dir / "tests"
    if not tests_dir.exists():
        report["fatal"] = f"no tests/ dir in {review_dir}"
        return report

    sc_dirs = sorted(p for p in tests_dir.iterdir() if p.is_dir())
    if not sc_dirs:
        report["fatal"] = "no SC subdirectories under tests/ -- run probably did not reach checks"
        return report

    bugs = report["bugs"]
    warns = report["warns"]
    stats = report["stats"]

    for sc_dir in sc_dirs:
        result_path = sc_dir / "result.json"
        if not result_path.exists():
            warns.append(f"{sc_dir.name}: no result.json (incomplete?)")
            continue

        try:
            result = json.loads(result_path.read_text(encoding="utf-8"))
        except Exception as exc:
            bugs.append(f"{sc_dir.name}: result.json unreadable ({exc})")
            continue

        stats["completed"] += 1
        conf = result.get("conformance_level", "")
        if hasattr(conf, "value"):
            conf = conf.value
        findings = result.get("findings", [])
        n = len(findings)
        stats["total_findings"] += n

        if conf == "Supports":
            stats["supports"] += 1
        elif conf == "Partially Supports":
            stats["partial"] += 1
        elif conf == "Does Not Support":
            stats["dns"] += 1
        elif conf == "Not Applicable":
            stats["na"] += 1
        else:
            stats["ne"] += 1

        if result.get("needs_review"):
            stats["needs_review"] += 1

        if conf in ("Does Not Support", "Partially Supports") and n == 0:
            bugs.append(
                f"{sc_dir.name}: {conf} but ZERO findings -- judge findings dropped"
            )

        for i, f in enumerate(findings):
            if not isinstance(f, dict):
                bugs.append(f"{sc_dir.name} finding {i}: not a dict, got {type(f).__name__}")
                continue
            element = f.get("element", "")
            if not element or element == "(unknown)":
                bugs.append(f"{sc_dir.name} finding {i}: no element location")
            if not f.get("css_selector"):
                warns.append(f"{sc_dir.name} finding {i}: no css_selector")
            issue = f.get("issue", "")
            if not issue:
                bugs.append(f"{sc_dir.name} finding {i}: no issue text")
            elif "wcag" not in issue.lower() and "wcag" not in (f.get("recommendation") or "").lower():
                warns.append(f"{sc_dir.name} finding {i}: issue/recommendation does not reference WCAG")
            impact = (f.get("impact") or "").lower()
            disability_terms = (
                "screen reader", "keyboard", "vision", "blind", "deaf",
                "motor", "cognitive", "color", "jaws", "nvda", "voiceover",
            )
            if not any(t in impact for t in disability_terms):
                warns.append(
                    f"{sc_dir.name} finding {i}: impact doesn't name disability groups or AT"
                )

        judge_path = sc_dir / "judge_response.json"
        if judge_path.exists():
            try:
                judge = json.loads(judge_path.read_text(encoding="utf-8"))
            except Exception as exc:
                bugs.append(f"{sc_dir.name} judge_response.json: unreadable ({exc})")
            else:
                final = judge.get("final_findings", [])
                rejected = judge.get("rejected_findings", [])
                for i, f in enumerate(final):
                    if not isinstance(f, dict):
                        bugs.append(
                            f"{sc_dir.name} judge.final_findings[{i}]: type={type(f).__name__}"
                        )
                        continue
                    if not f.get("element"):
                        bugs.append(f"{sc_dir.name} judge.final_findings[{i}]: no element")
                    if not f.get("issue"):
                        bugs.append(f"{sc_dir.name} judge.final_findings[{i}]: no issue")

                total_in = (
                    result.get("programmatic_findings_count", 0)
                    + result.get("ai_findings_count", 0)
                    + result.get("code_ai_findings_count", 0)
                    + result.get("at_sim_findings_count", 0)
                )
                total_out = len(final) + len(rejected)
                if total_in > 0 and total_out == 0:
                    warns.append(
                        f"{sc_dir.name}: judge consumed {total_in} findings -> 0 out (silent drop)"
                    )

        ctx_path = sc_dir / "judge_dom_context.txt"
        if ctx_path.exists():
            ctx = ctx_path.read_text(encoding="utf-8", errors="replace")
            lowered = ctx.lower()
            first_idx = lowered.find("first ")
            if first_idx != -1 and "links" not in lowered[:first_idx]:
                warns.append(f"{sc_dir.name}: judge_dom_context contains 'first N' truncation marker")

    return report


def _print_report(report: dict[str, Any]) -> None:
    if report.get("fatal"):
        print(f"FATAL: {report['fatal']}")
        return

    stats = report["stats"]
    bugs = report["bugs"]
    warns = report["warns"]

    print(f"=== AUDIT: {report['review_id']} ===\n")
    print("--- STATS ---")
    print(f"  Completed:        {stats['completed']}")
    print(f"  Supports:         {stats['supports']}")
    print(f"  Partially:        {stats['partial']}")
    print(f"  Does Not Support: {stats['dns']}")
    print(f"  Not Applicable:   {stats['na']}")
    print(f"  Not Evaluated:    {stats['ne']}")

    bp = report.get("bypasses") or {}
    bp_total = bp.get("total", 0)
    if bp_total:
        dl = bp.get("data_lost_count", 0)
        print(f"\n--- BYPASSES (per-run telemetry) ---")
        print(f"  Total:       {bp_total}")
        print(f"  Data lost:   {dl}  {'(CLEAN)' if dl == 0 else '(CHECK bypass_log.jsonl)'}")
        by_cat = bp.get("by_category") or {}
        for cat, n in sorted(by_cat.items(), key=lambda kv: -kv[1]):
            print(f"  - {cat}: {n}")
    else:
        print(f"\n--- BYPASSES ---  (none — clean run)")
    print(f"  Needs review:     {stats['needs_review']}")
    print(f"  Total findings:   {stats['total_findings']}")

    print(f"\n--- BUGS ({len(bugs)}) ---")
    for b in bugs[:50]:
        print(f"  BUG: {b}")
    if len(bugs) > 50:
        print(f"  ... +{len(bugs) - 50} more")

    print(f"\n--- WARNINGS ({len(warns)}) ---")
    for w in warns[:30]:
        print(f"  WARN: {w}")
    if len(warns) > 30:
        print(f"  ... +{len(warns) - 30} more")


def main(review_id: str) -> int:
    report = audit_review(review_id)
    _print_report(report)
    if report.get("fatal"):
        return 2
    return 0 if not report["bugs"] else 1


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("usage: python audit_run.py <review_id>")
        raise SystemExit(2)
    raise SystemExit(main(sys.argv[1]))
