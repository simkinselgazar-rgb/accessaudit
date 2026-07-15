"""Axe-core finding extraction.

Extracted from `checks/base.py:BaseCheck` so the per-source extraction
logic is reusable and testable independently of the BaseCheck instance.
"""
from __future__ import annotations

from models import CaptureData, Finding, Severity
from functions.finding_utils import _make_finding_id


# axe-core rules that assert an element HAS an accessible name. When one of
# these has zero violations/incomplete and >=1 pass, axe found no unnamed
# element of that kind on the page.
_AXE_NAME_RULES = {
    "a": "link-name",
    "button": "button-name",
    "img": "image-alt",
    "input": "input-button-name",
}


def accessible_name_corroboration(axe_results: dict) -> dict:
    """Summarize axe's accessible-name rules so ANDI 'no accessible name'
    findings can be cross-checked against the more reliable axe verdict.

    Returns ``{rule_id: {"pass": int, "violation": int, "pass_targets":
    set[str]}}`` for link-name / button-name / image-alt / input-button-name.
    ``violation`` counts both ``violations`` and ``incomplete`` nodes. A rule
    with ``violation == 0 and pass > 0`` means axe found NO unnamed element of
    that kind anywhere on the page -- so an ANDI 'no accessible name' finding
    for that element type is contradicted by axe (verified umich.edu
    2026-05-28: ANDI flagged 4 named nav/infographic links as no-name; axe
    link-name had 67 passes and 0 violations).
    """
    out: dict = {}
    if not isinstance(axe_results, dict):
        return out
    passes = axe_results.get("passes") or []
    fails = (axe_results.get("violations") or []) + (axe_results.get("incomplete") or [])
    for rule in set(_AXE_NAME_RULES.values()):
        rec = {"pass": 0, "violation": 0, "pass_targets": set()}
        for r in passes:
            if r.get("id") == rule:
                nodes = r.get("nodes") or []
                rec["pass"] += len(nodes)
                for n in nodes:
                    for t in n.get("target") or []:
                        rec["pass_targets"].add(str(t).strip())
        for r in fails:
            if r.get("id") == rule:
                rec["violation"] += len(r.get("nodes") or [])
        out[rule] = rec
    return out


def _leaf_tag(selector: str) -> str:
    """HTML tag of the selector's last (leaf) segment: 'a' for
    '...> li > a.infographic-two', 'button' for 'div#x > button'."""
    import re
    seg = re.split(r"[>\s+~]+", (selector or "").strip())[-1]
    m = re.match(r"([a-zA-Z][\w-]*)", seg)
    return m.group(1).lower() if m else ""


def axe_confirms_named(name_summary: dict, tag: str, selector: str) -> bool:
    """True when axe corroborates that this element HAS an accessible name --
    either its exact selector is in an axe name-rule pass set, or axe's rule
    for this element type is page-clean (0 violations, >=1 pass). Element type
    comes from ``tag`` or, failing that, the selector's leaf tag. Used to
    suppress ANDI 'no accessible name' false positives (ANDI does not honour
    clip/sr-only hidden label text; axe does)."""
    if not name_summary:
        return False
    sel = (selector or "").strip()
    if sel:
        for rec in name_summary.values():
            if sel in rec.get("pass_targets", ()):
                return True
    tag = (tag or "").lower() or _leaf_tag(sel)
    rule = _AXE_NAME_RULES.get(tag)
    rec = name_summary.get(rule) if rule else None
    if rec and rec.get("violation", 0) == 0 and rec.get("pass", 0) > 0:
        return True
    return False


def extract_axe_findings(capture_data: CaptureData, criterion_id: str) -> list[Finding]:
    """Extract axe-core findings relating to this criterion.

    Severity mapping:
    - ``violations``: axe determined the rule FAILS. Severity follows
      axe's own impact field (critical/serious -> HIGH, moderate ->
      MEDIUM, minor -> LOW, ?? -> INFO).
    - ``incomplete``: axe could NOT determine compliance (most often
      because the element is over a background gradient/image where
      contrast cannot be sampled, or because computed styles were
      unavailable). These are MANUAL-REVIEW signals, not failures.
      Always severity INFO regardless of what axe reported as
      ``impact`` for the underlying rule. Without this distinction
      the WVU + ASU runs surface 30+ "couldn't measure" entries
      at HIGH severity, bloating the report and hiding real issues.
    """
    if not getattr(capture_data, "axe_results", None):
        return []

    axe_findings = []
    target_tag = f"wcag{criterion_id.replace('.', '')}"

    for category in ["violations", "incomplete"]:
        for rule in capture_data.axe_results.get(category, []):
            # Check if rule tags include our target wcag tag
            tags = rule.get("tags", [])
            if not any(t.startswith(target_tag) for t in tags):
                continue

            is_incomplete = (category == "incomplete")
            issue = f"Axe-core {'incomplete check (needs manual review)' if is_incomplete else 'violation'}: {rule.get('description', '')}"
            recommendation = rule.get('help', '')
            help_url = rule.get('helpUrl', '')
            if help_url:
                recommendation += f" (See: {help_url})"

            # Severity mapping. INCOMPLETE always INFO; violations
            # follow axe impact rating.
            if is_incomplete:
                severity = Severity.INFO
            else:
                impact = rule.get("impact", "moderate")
                if impact in ("critical", "serious"):
                    severity = Severity.HIGH
                elif impact == "moderate":
                    severity = Severity.MEDIUM
                elif impact == "minor":
                    severity = Severity.LOW
                else:
                    severity = Severity.INFO

            for node in rule.get("nodes", []):
                failure_summary = node.get("failureSummary", "")
                node_issue = f"{issue}\\nDetails: {failure_summary}" if failure_summary else issue

                css_selector = " ".join(node.get("target", []))
                axe_findings.append(Finding(
                    id=_make_finding_id(),
                    element=css_selector,
                    issue=node_issue,
                    impact="Axe-core programmatic rule flagged this element.",
                    recommendation=recommendation,
                    severity=severity,
                    source="axe",
                    css_selector=css_selector,
                ))

    return axe_findings
