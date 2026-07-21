"""Aggregate results across multiple pages for site crawl.

When the bge-m3 embedding host is reachable, uses cosine similarity to:
  - Group semantically-identical findings across pages into one entry
    with many affected URLs (replaces naive string dedup).
  - Detect cross-page navigation and identification inconsistency by
    comparing nav-link and label embeddings instead of exact strings,
    so "Contact Us" vs "Get in Touch" stops being flagged as different.

When embeddings are unreachable, the string-matching fallback still
runs so the aggregator always produces SOME cross-page output.
"""
from __future__ import annotations

import logging
from models import ConformanceLevel

logger = logging.getLogger(__name__)

# Severity ordering for worst-case
_SEVERITY_ORDER = {
    "Supports": 0,
    "Partially Supports": 1,
    "Does Not Support": 2,
    "Not Applicable": -1,
    "Not Evaluated": -2,
}

# Cosine-similarity threshold above which two findings are considered
# "the same finding said two ways". 0.80 is conservative enough to avoid
# merging genuinely different issues, loose enough to bridge wording
# variation ("missing alt text" vs "image has no alt attribute").
_FINDING_DEDUP_SIM = 0.80

# Cosine-similarity threshold above which two navigation menus count as
# "the same menu". 0.75 is calibrated against live bge-m3 runs:
#   - Identical nav with one wording change: ~0.98
#   - Moderate rewording ("About Us" -> "Get in Touch"): ~0.80
#   - Different topic entirely: <0.60
# A 0.75 threshold accepts rewording as consistent, flags only structural
# differences or wholesale different menus. Pairs this with the same
# threshold for form-field label consistency (SC 3.2.4).
_NAV_CONSISTENCY_SIM = 0.75


async def aggregate_results(page_results: list[dict]) -> list[dict]:
    """Aggregate test results across pages using worst-case per criterion.

    Semantically de-duplicates findings across pages when the embedding
    host is reachable. Falls back to string-level dedup on failure.

    Args:
        page_results: List of dicts, each with 'url' and 'results' (list
            of test result dicts)

    Returns:
        Aggregated list of test result dicts
    """
    if not page_results:
        return []

    # Group by criterion_id
    criteria_map: dict[str, list[dict]] = {}
    for page in page_results:
        for result in page.get("results", []):
            cid = result.get("criterion_id", "")
            if cid not in criteria_map:
                criteria_map[cid] = []
            criteria_map[cid].append({**result, "_page_url": page.get("url", "")})

    aggregated: list[dict] = []
    for criterion_id, results in sorted(criteria_map.items()):
        agg = await _aggregate_criterion(criterion_id, results)
        aggregated.append(agg)

    return aggregated


async def _aggregate_criterion(criterion_id: str, results: list[dict]) -> dict:
    """Aggregate a single criterion across pages using proportion-based
    conformance aligned with VPAT 2.5 methodology.

    Instead of pure worst-case:
    - 0 % of applicable pages fail  → Supports
    - >0 % but <50 % fail           → Partially Supports
    - ≥50 % fail                     → Does Not Support
    """
    if not results:
        return {}

    # Use the *worst-performing* result as the template so the summary
    # text describes the most severe failure (not a passing page).
    results_by_sev = sorted(
        results,
        key=lambda r: _SEVERITY_ORDER.get(
            r.get("conformance_level", "Not Evaluated"), -2
        ),
        reverse=True,
    )
    base = dict(results_by_sev[0])
    base.pop("_page_url", None)

    # Per-page tracking
    page_levels: list[tuple[str, str]] = []  # (url, conformance_level)
    confidences: list[float] = []
    raw_findings: list[dict] = []
    all_tt_results: dict[str, dict] = {}
    pages_tested = 0
    pages_applicable = 0  # pages where criterion is Supports/PS/DNS
    pages_affected = 0    # pages with PS or DNS

    for r in results:
        page_url = r.get("_page_url", "")
        level = r.get("conformance_level", "Not Evaluated")
        sev = _SEVERITY_ORDER.get(level, -2)
        pages_tested += 1
        page_levels.append((page_url, level))

        if sev >= 0:
            pages_applicable += 1
            if sev >= 1:
                pages_affected += 1

        conf = r.get("confidence", 0)
        if conf > 0:
            confidences.append(conf)

        for f in r.get("findings", []):
            finding = dict(f)
            finding["page_url"] = page_url
            raw_findings.append(finding)

        # TT results: worst per test ID (FAIL overrides PASS)
        for tt in r.get("tt_results", []):
            tt_id = tt.get("tt_id", "")
            existing = all_tt_results.get(tt_id)
            if existing is None:
                all_tt_results[tt_id] = dict(tt)
            elif tt.get("result") == "FAIL":
                all_tt_results[tt_id] = dict(tt)

    # ── Finding dedup across pages (semantic, with fallback) ─────
    all_findings = await _dedupe_findings_across_pages(raw_findings)

    # ── Proportion-based conformance ──────────────────────────────
    if pages_applicable == 0:
        has_na = any(l == "Not Applicable" for _, l in page_levels)
        conformance = "Not Applicable" if has_na else "Not Evaluated"
    elif pages_affected == 0:
        conformance = "Supports"
    else:
        fail_ratio = pages_affected / pages_applicable
        dns_count = sum(1 for _, l in page_levels if l == "Does Not Support")
        if dns_count > 0 and (dns_count / pages_applicable) >= 0.5:
            conformance = "Does Not Support"
        elif fail_ratio >= 0.5:
            conformance = "Does Not Support"
        else:
            conformance = "Partially Supports"

    # ── Prevalence string ─────────────────────────────────────────
    if pages_tested <= 1:
        prevalence = ""
    elif pages_applicable > 0 and pages_affected > 0:
        prevalence = (
            f"Found on {pages_affected} of {pages_tested} tested "
            f"page{'s' if pages_tested != 1 else ''}."
        )
    elif pages_applicable > 0:
        prevalence = (
            f"Passes on all {pages_tested} tested "
            f"page{'s' if pages_tested != 1 else ''}."
        )
    else:
        prevalence = ""

    # ── Enhanced summary for multi-page ───────────────────────────
    if pages_tested > 1 and prevalence:
        base_summary = base.get("summary", "")
        affected_urls = [
            url for url, lvl in page_levels
            if _SEVERITY_ORDER.get(lvl, -2) >= 1
        ]
        if affected_urls:
            refs = ", ".join(affected_urls)
            base["summary"] = (
                f"{prevalence} Affected: {refs}. {base_summary}"
            ).strip()
        else:
            base["summary"] = f"{prevalence} {base_summary}".strip()

    base["conformance_level"] = conformance
    base["confidence"] = (
        sum(confidences) / len(confidences) if confidences else 0
    )
    base["findings"] = all_findings
    base["tt_results"] = list(all_tt_results.values())
    base["pages_tested"] = pages_tested
    base["pages_applicable"] = pages_applicable
    base["pages_affected"] = pages_affected
    base["prevalence"] = prevalence

    return base


async def _dedupe_findings_across_pages(findings: list[dict]) -> list[dict]:
    """Cluster semantically-identical findings across pages.

    Findings from different URLs that describe the same issue (e.g. a
    site-wide "missing alt on logo" problem) merge into one entry with
    an ``affected_pages`` list. When embeddings are unavailable, falls
    back to exact (element, issue) string matching.
    """
    if not findings:
        return []

    # Attempt semantic dedup via bge-m3 embeddings
    try:
        from functions.embeddings import cluster_by_similarity, embed_batch

        texts = [
            f"{f.get('element', '')} -- {f.get('issue', '')}"
            for f in findings
        ]
        vectors = await embed_batch(texts)
        paired = list(zip(findings, vectors))
        clusters = cluster_by_similarity(paired, threshold=_FINDING_DEDUP_SIM)
        return [_merge_finding_cluster(cluster) for cluster in clusters]
    except Exception as exc:
        logger.info(
            "Finding dedup: embeddings unavailable, using string fallback (%s)",
            exc,
        )

    # Fallback: exact (element, issue) matching
    deduped: list[dict] = []
    seen: dict[tuple[str, str], dict] = {}
    for f in findings:
        key = (f.get("element", ""), f.get("issue", ""))
        if key in seen:
            existing = seen[key]
            affected = existing.setdefault("affected_pages", [existing.get("page_url", "")])
            url = f.get("page_url", "")
            if url and url not in affected:
                affected.append(url)
        else:
            entry = dict(f)
            seen[key] = entry
            deduped.append(entry)
    return deduped


def _merge_finding_cluster(cluster: list[dict]) -> dict:
    """Merge a list of semantically-similar findings into one entry.

    Keeps the highest-severity finding as the representative; lists every
    affected page under ``affected_pages``. Preserves per-page element
    text under ``affected_page_elements`` so the auditor can still see
    the element variations across pages without losing evidence.
    """
    if not cluster:
        return {}
    if len(cluster) == 1:
        only = dict(cluster[0])
        url = only.get("page_url", "")
        if url:
            only["affected_pages"] = [url]
        return only

    sev_order = {"high": 3, "medium": 2, "low": 1, "info": 0}
    representative = max(
        cluster,
        key=lambda f: sev_order.get(str(f.get("severity", "")).lower(), -1),
    )
    merged = dict(representative)
    urls: list[str] = []
    elements_per_url: dict[str, str] = {}
    for entry in cluster:
        url = entry.get("page_url", "")
        if url and url not in urls:
            urls.append(url)
        if url:
            elem = entry.get("element", "")
            if elem and url not in elements_per_url:
                elements_per_url[url] = elem
    merged["affected_pages"] = urls
    if len(urls) > 1:
        merged["affected_page_elements"] = elements_per_url
        # Re-render element so the report makes it clear this is a
        # site-wide issue
        merged["element"] = f"Site-wide on {len(urls)} pages: {merged.get('element', '')}"
    return merged


async def check_cross_page_consistency(
    page_results: list[dict],
    review_dir: str = "",
) -> list[dict]:
    """Check navigation and identification consistency across pages.

    Loads structural_summary.json from each page's capture directory and
    compares actual nav structure, landmarks, and form labels. When the
    embedding host is reachable, uses cosine similarity so wording
    variations don't count as inconsistency; otherwise falls back to
    exact-string comparison.
    """
    import json as _json
    from collections import Counter
    from pathlib import Path

    if len(page_results) < 2:
        return []

    findings: list[dict] = []

    # ── Load structural summaries ─────────────────────────────────
    summaries: list[dict] = []
    if review_dir:
        for page_dir in sorted(Path(review_dir).glob("page_*")):
            sf = page_dir / "captures" / "structural_summary.json"
            if sf.exists():
                try:
                    summaries.append(_json.loads(sf.read_text(encoding="utf-8")))
                except Exception:
                    logger.exception("Skipping malformed structural summary %s", sf)

    if len(summaries) >= 2:
        findings.extend(await _nav_consistency_findings(summaries))
        findings.extend(_landmark_consistency_findings(summaries))
        findings.extend(await _label_consistency_findings(summaries))

    # ── Fallback if no structural data ────────────────────────────
    if not summaries:
        logger.info("No structural summaries — using finding-based comparison")
        nav_per_page = {}
        for page in page_results:
            url = page.get("url", "")
            nav = [
                f.get("issue", "")
                for r in page.get("results", [])
                for f in r.get("findings", [])
                if "nav" in f.get("element", "").lower()
            ]
            nav_per_page[url] = nav
        counts = [len(v) for v in nav_per_page.values()]
        if counts and max(counts) > 0:
            has_nav = sum(1 for c in counts if c > 0)
            no_nav = sum(1 for c in counts if c == 0)
            if has_nav > 0 and no_nav > 0:
                findings.append({
                    "element": "[cross-page consistency]",
                    "issue": f"Navigation findings vary: {has_nav} pages have issues, {no_nav} do not.",
                    "impact": "Inconsistent navigation across pages.",
                    "severity": "medium",
                    "recommendation": "WCAG 3.2.3 requires consistent navigation order.",
                    "source": "cross_page",
                    "criterion_id": "3.2.3",
                })

    if findings:
        logger.info("Cross-page consistency: %d issue(s) found", len(findings))
    return findings


async def _nav_consistency_findings(summaries: list[dict]) -> list[dict]:
    """SC 3.2.3 -- semantic nav comparison with exact-string fallback."""
    from collections import Counter

    nav_seqs: list[tuple[str, list[str]]] = [
        (
            s.get("url", "?"),
            [
                (l.get("text") or "").strip()
                for l in s.get("nav_links", [])
                if (l.get("text") or "").strip()
            ],
        )
        for s in summaries
    ]
    pages_with_nav = [(u, links) for u, links in nav_seqs if links]
    if len(pages_with_nav) < 2:
        return []

    # Try embedding-based comparison first
    try:
        from functions.embeddings import cosine_similarity, embed_batch

        nav_texts = [" | ".join(links) for _, links in pages_with_nav]
        vectors = await embed_batch(nav_texts)

        # Majority vote via centroid: compute centroid across all, then
        # score each against centroid. Pages far below threshold deviate.
        dim = len(vectors[0]) if vectors and vectors[0] else 0
        if dim and all(len(v) == dim and any(v) for v in vectors):
            centroid = [sum(v[i] for v in vectors) / len(vectors) for i in range(dim)]
            deviating = [
                u for (u, _), v in zip(pages_with_nav, vectors)
                if cosine_similarity(v, centroid) < _NAV_CONSISTENCY_SIM
            ]
            if deviating and len(deviating) < len(pages_with_nav):
                return [{
                    "element": "[cross-page navigation]",
                    "issue": (
                        f"Navigation menu differs materially on "
                        f"{len(deviating)} of {len(pages_with_nav)} pages. "
                        f"Deviating: {', '.join(deviating)}"
                    ),
                    "impact": (
                        "Users who learn the navigation on one page find a "
                        "different set or order of links on these pages."
                    ),
                    "severity": "medium",
                    "recommendation": "WCAG 3.2.3 requires consistent navigation order across pages.",
                    "source": "cross_page",
                    "criterion_id": "3.2.3",
                }]
            return []
    except Exception as exc:
        logger.info("Cross-page nav: embeddings unavailable, using string fallback (%s)", exc)

    # String-match fallback (exact sequence)
    seq_strs = ["|".join(l.lower() for l in links) for _, links in pages_with_nav]
    majority_seq, majority_count = Counter(seq_strs).most_common(1)[0]
    deviating = [
        u for (u, links), s in zip(pages_with_nav, seq_strs) if s != majority_seq
    ]
    if deviating and majority_count >= 2:
        return [{
            "element": "[cross-page navigation]",
            "issue": (
                f"Navigation order differs on {len(deviating)} of "
                f"{len(pages_with_nav)} pages. Deviating: {', '.join(deviating)}"
            ),
            "impact": "Users who learn nav order on one page find a different order elsewhere.",
            "severity": "medium",
            "recommendation": "WCAG 3.2.3 requires consistent navigation order across pages.",
            "source": "cross_page",
            "criterion_id": "3.2.3",
        }]
    return []


def _landmark_consistency_findings(summaries: list[dict]) -> list[dict]:
    """Report pages missing landmarks that the majority of pages have."""
    from collections import Counter

    lm_sets = [
        (
            s.get("url", "?"),
            frozenset(
                l.get("role", "")
                for l in s.get("landmarks", [])
                if l.get("role")
            ),
        )
        for s in summaries
    ]
    if not lm_sets:
        return []
    majority_lm = Counter(r for _, r in lm_sets).most_common(1)[0][0]
    missing_pages: list[tuple[str, set]] = []
    for url, roles in lm_sets:
        if roles and roles != majority_lm and majority_lm - roles:
            missing_pages.append((url, majority_lm - roles))
    if not missing_pages:
        return []
    all_missing: set[str] = set()
    for _, m in missing_pages:
        all_missing.update(m)
    if not all_missing:
        return []
    return [{
        "element": "[cross-page landmarks]",
        "issue": (
            f"{len(missing_pages)} page(s) missing landmarks: "
            f"{', '.join(sorted(all_missing))}"
        ),
        "impact": "Screen reader users find different landmark structure across pages.",
        "severity": "low",
        "recommendation": "Use consistent landmark roles across all pages.",
        "source": "cross_page",
        "criterion_id": "3.2.3",
    }]


async def _label_consistency_findings(summaries: list[dict]) -> list[dict]:
    """SC 3.2.4 -- semantic comparison of form-field labels for same name."""
    findings: list[dict] = []
    field_labels: dict[str, dict[str, str]] = {}
    for s in summaries:
        for ff in s.get("form_labels", []):
            name = ff.get("name", "")
            label = (ff.get("label") or "").strip()
            if name and label:
                field_labels.setdefault(name, {})[label] = s.get("url", "?")

    for name, labels in field_labels.items():
        if len(labels) <= 1:
            continue

        label_texts = list(labels.keys())
        # Try embedding-based similarity: if all labels are semantically
        # close (cosine > threshold pairwise), it's a branding variation,
        # not an identification inconsistency.
        semantically_consistent = False
        try:
            from functions.embeddings import cosine_similarity, embed_batch

            vectors = await embed_batch(label_texts)
            if vectors and all(any(v) for v in vectors):
                semantically_consistent = all(
                    cosine_similarity(vectors[0], v) >= _NAV_CONSISTENCY_SIM
                    for v in vectors[1:]
                )
        except Exception as exc:
            logger.info(
                "Cross-page label: embeddings unavailable, using strict matching (%s)",
                exc,
            )

        if semantically_consistent:
            continue

        findings.append({
            "element": f"[form field name='{name}']",
            "issue": (
                f"Field '{name}' labeled differently: "
                + ", ".join(repr(l) for l in labels)
            ),
            "impact": "Users see different labels for the same form field across pages.",
            "severity": "medium",
            "recommendation": "WCAG 3.2.4 requires consistent identification of same-function components.",
            "source": "cross_page",
            "criterion_id": "3.2.4",
        })

    return findings


def generate_per_page_summary(page_results: list[dict]) -> list[dict]:
    """Generate summary for each page."""
    summaries = []
    for page in page_results:
        url = page.get("url", "")
        results = page.get("results", [])

        counts = {
            "supports": 0,
            "partially_supports": 0,
            "does_not_support": 0,
            "not_applicable": 0,
            "not_evaluated": 0,
            "total_findings": 0,
        }

        for r in results:
            level = r.get("conformance_level", "Not Evaluated")
            if level == "Supports":
                counts["supports"] += 1
            elif level == "Partially Supports":
                counts["partially_supports"] += 1
            elif level == "Does Not Support":
                counts["does_not_support"] += 1
            elif level == "Not Applicable":
                counts["not_applicable"] += 1
            else:
                counts["not_evaluated"] += 1
            counts["total_findings"] += len(r.get("findings", []))

        summaries.append({"url": url, **counts})

    return summaries
