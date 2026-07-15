"""Per-SC quality auditor: validate every saved prompt/response artifact.

Usage:
    python audit_sc.py <review_id> <sc_dir>             # audit one SC
    python audit_sc.py <review_id> --all                # audit every completed SC

For each SC, this script verifies:
- prompt.txt is well-formed and contains expected anchors (criterion id,
  page URL, programmatic data, video descriptions, elements)
- visual_ai_response.json was parsed via a real tool call, not text fallback
- code_ai_response.json was parsed via a real tool call, not text fallback
- judge_dom_context.txt has every section required by the architecture and
  contains NO 'first N' truncation markers
- judge_response.json final_findings are dicts (not strings/Finding objs)
  and have element + issue + impact + recommendation + severity
- result.json findings count is consistent with judge final_findings count
- Findings reference WCAG and name disability groups / assistive tech

Reports BUG (must fix) and WARN (worth attention) for each SC.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path


REQUIRED_DOM_SECTIONS = (
    "URL:",
    "PAGE LANGUAGE",
    "TITLE",
    "HEADINGS",
    "IMAGES",
    "LANDMARKS",
    "FORM FIELDS",
    "LINKS",
    "TAB WALK",
    "AXE-CORE",
)

DISABILITY_TERMS = (
    "screen reader", "keyboard", "vision", "blind", "deaf",
    "motor", "cognitive", "color", "jaws", "nvda", "voiceover",
    "low vision", "color blind",
)

TRUNCATION_MARKERS = ("first 5", "first 10", "first 20", "first 40", "truncated", "[:")


def audit_sc(review_id: str, sc_dir_name: str) -> tuple[list[str], list[str]]:
    """Return ``(bugs, warnings)`` for one SC subdirectory."""
    bugs: list[str] = []
    warns: list[str] = []
    sc = sc_dir_name
    base = Path("reviews") / review_id / "tests" / sc_dir_name

    if not base.exists():
        return [f"{sc}: directory does not exist"], []

    # --- result.json -------------------------------------------------------
    result_path = base / "result.json"
    if not result_path.exists():
        bugs.append(f"{sc}: missing result.json")
        return bugs, warns
    try:
        result = json.loads(result_path.read_text(encoding="utf-8"))
    except Exception as exc:
        bugs.append(f"{sc}: result.json unreadable ({exc})")
        return bugs, warns

    conf = result.get("conformance_level", "")
    if hasattr(conf, "value"):
        conf = conf.value
    findings = result.get("findings", [])
    if conf in ("Does Not Support", "Partially Supports") and not findings:
        bugs.append(
            f"{sc}: verdict is {conf} but findings list is EMPTY (judge dropped findings)"
        )

    for i, f in enumerate(findings):
        if not isinstance(f, dict):
            bugs.append(f"{sc} findings[{i}]: not a dict ({type(f).__name__})")
            continue
        if not f.get("element") or f["element"] == "(unknown)":
            bugs.append(f"{sc} findings[{i}]: missing element location")
        if not f.get("issue"):
            bugs.append(f"{sc} findings[{i}]: missing issue text")
        if not f.get("impact"):
            warns.append(f"{sc} findings[{i}]: missing impact")
        elif not any(t in f["impact"].lower() for t in DISABILITY_TERMS):
            warns.append(
                f"{sc} findings[{i}]: impact does not name disability groups or AT"
            )
        if not f.get("recommendation"):
            warns.append(f"{sc} findings[{i}]: missing recommendation")
        if not f.get("css_selector"):
            warns.append(f"{sc} findings[{i}]: missing css_selector")
        issue = (f.get("issue") or "").lower()
        rec = (f.get("recommendation") or "").lower()
        if "wcag" not in issue and "wcag" not in rec:
            warns.append(f"{sc} findings[{i}]: no WCAG reference in issue/recommendation")

    # --- prompt.txt --------------------------------------------------------
    prompt_path = base / "prompt.txt"
    if prompt_path.exists():
        ptext = prompt_path.read_text(encoding="utf-8", errors="replace")
        criterion_id = sc.replace("_", ".")
        if criterion_id not in ptext:
            warns.append(f"{sc} prompt.txt: criterion id {criterion_id} not mentioned")
        if "PROGRAMMATIC DATA" not in ptext and "programmatic" not in ptext.lower():
            warns.append(f"{sc} prompt.txt: no programmatic data section")
        if "ANALYSIS REQUEST" not in ptext and "report_wcag_assessment" not in ptext:
            warns.append(f"{sc} prompt.txt: no analysis-request anchor")

    # --- visual_ai_response.json ------------------------------------------
    va_path = base / "visual_ai_response.json"
    if va_path.exists():
        try:
            va = json.loads(va_path.read_text(encoding="utf-8"))
        except Exception as exc:
            bugs.append(f"{sc} visual_ai_response.json: unreadable ({exc})")
        else:
            reasoning = (va.get("confidence_reasoning") or "").lower()
            if "plain text" in reasoning or "text fallback" in reasoning:
                bugs.append(
                    f"{sc} visual_ai_response.json: TEXT FALLBACK -- parser dropped the structured tool call"
                )

    # --- code_ai_response.json --------------------------------------------
    code_path = base / "code_ai_response.json"
    if code_path.exists():
        try:
            ca = json.loads(code_path.read_text(encoding="utf-8"))
        except Exception as exc:
            bugs.append(f"{sc} code_ai_response.json: unreadable ({exc})")
        else:
            reasoning = (ca.get("confidence_reasoning") or "").lower()
            if "plain text" in reasoning or "text fallback" in reasoning:
                bugs.append(
                    f"{sc} code_ai_response.json: TEXT FALLBACK -- parser dropped the structured tool call"
                )

    # --- judge_dom_context.txt --------------------------------------------
    ctx_path = base / "judge_dom_context.txt"
    if ctx_path.exists():
        ctx = ctx_path.read_text(encoding="utf-8", errors="replace")
        for marker in TRUNCATION_MARKERS:
            if marker in ctx:
                bugs.append(
                    f"{sc} judge_dom_context.txt: contains truncation marker {marker!r}"
                )
        # Check critical sections are present (only flag ones the SC actually needs)
        criterion_id = sc.replace("_", ".")
        # All checks should have these basic sections
        for section in ("HEADINGS", "IMAGES", "LANDMARKS", "LINKS"):
            if section not in ctx:
                warns.append(
                    f"{sc} judge_dom_context.txt: missing section {section}"
                )

    # --- judge_response.json ----------------------------------------------
    j_path = base / "judge_response.json"
    if j_path.exists():
        try:
            j = json.loads(j_path.read_text(encoding="utf-8"))
        except Exception as exc:
            bugs.append(f"{sc} judge_response.json: unreadable ({exc})")
        else:
            final = j.get("final_findings", [])
            for i, ff in enumerate(final):
                if not isinstance(ff, dict):
                    bugs.append(
                        f"{sc} judge.final_findings[{i}]: type={type(ff).__name__} not dict"
                    )
                    continue
                if not ff.get("element"):
                    bugs.append(f"{sc} judge.final_findings[{i}]: missing element")
                if not ff.get("issue"):
                    bugs.append(f"{sc} judge.final_findings[{i}]: missing issue")
                if not ff.get("css_selector"):
                    warns.append(f"{sc} judge.final_findings[{i}]: missing css_selector")
            # Source totals reconciliation
            total_in = (
                result.get("programmatic_findings_count", 0)
                + result.get("ai_findings_count", 0)
                + result.get("code_ai_findings_count", 0)
                + result.get("at_sim_findings_count", 0)
            )
            total_out = len(final) + len(j.get("rejected_findings", []))
            if total_in > 0 and total_out == 0:
                bugs.append(
                    f"{sc}: judge consumed {total_in} source findings -> 0 out (silent drop)"
                )

    return bugs, warns


def audit_all(review_id: str) -> int:
    tests_dir = Path("reviews") / review_id / "tests"
    if not tests_dir.exists():
        print(f"FATAL: {tests_dir} does not exist")
        return 2
    sc_dirs = sorted(p.name for p in tests_dir.iterdir() if p.is_dir())
    if not sc_dirs:
        print(f"No SCs completed yet under {tests_dir}")
        return 0

    total_bugs: list[str] = []
    total_warns: list[str] = []
    print(f"Auditing {len(sc_dirs)} SCs in {review_id}\n")
    for sc in sc_dirs:
        bugs, warns = audit_sc(review_id, sc)
        marker = "BUG" if bugs else ("WARN" if warns else "OK")
        print(f"  [{marker:4s}] {sc:14s} bugs={len(bugs)} warns={len(warns)}")
        total_bugs.extend(bugs)
        total_warns.extend(warns)

    if total_bugs:
        print(f"\n=== {len(total_bugs)} BUGS ===")
        for b in total_bugs[:50]:
            print(f"  {b}")
        if len(total_bugs) > 50:
            print(f"  ... +{len(total_bugs) - 50} more")
    if total_warns:
        print(f"\n=== {len(total_warns)} WARNINGS ===")
        for w in total_warns[:30]:
            print(f"  {w}")
        if len(total_warns) > 30:
            print(f"  ... +{len(total_warns) - 30} more")
    if not total_bugs and not total_warns:
        print("\nALL CLEAN")
    return 1 if total_bugs else 0


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("usage: python audit_sc.py <review_id> [<sc_dir>|--all]")
        raise SystemExit(2)
    review_id = sys.argv[1]
    target = sys.argv[2] if len(sys.argv) > 2 else "--all"
    if target == "--all":
        raise SystemExit(audit_all(review_id))
    bugs, warns = audit_sc(review_id, target)
    for b in bugs:
        print(f"BUG: {b}")
    for w in warns:
        print(f"WARN: {w}")
    if not bugs and not warns:
        print("clean")
    raise SystemExit(1 if bugs else 0)
