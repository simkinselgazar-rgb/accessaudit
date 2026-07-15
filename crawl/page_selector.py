"""AI-powered site analysis and page selection for crawl reviews.

Flow:
1. Fetch lightweight metadata for every discovered page (non-LLM HTTP).
2. AI reads ALL page summaries and determines WHAT this site is.
3. AI selects the most important pages for WCAG testing.

Every AI call goes through ``functions.llm.LLMClient``.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any

import httpx  # non-LLM HTTP only (crawl metadata fetch)

from functions.llm import LLMClient
from functions.parser import extract_json_from_text, get_content_text

logger = logging.getLogger(__name__)


_SITE_ANALYSIS_TOOL = {
    "type": "function",
    "function": {
        "name": "report_site_analysis",
        "description": "Determine what a website is, who uses it, and what it does.",
        "parameters": {
            "type": "object",
            "required": [
                "sector", "client_type", "primary_users",
                "critical_workflows", "additional_context",
            ],
            "properties": {
                "sector": {
                    "type": "string",
                    "enum": [
                        "education", "healthcare", "government",
                        "commerce", "nonprofit", "other",
                    ],
                },
                "client_type": {"type": "string"},
                "primary_users": {"type": "array", "items": {"type": "string"}},
                "critical_workflows": {"type": "array", "items": {"type": "string"}},
                "additional_context": {"type": "string"},
            },
        },
    },
}


_PAGE_SELECTOR_TOOL = {
    "type": "function",
    "function": {
        "name": "select_pages",
        "description": "Select the most important pages from a crawl for WCAG testing.",
        "parameters": {
            "type": "object",
            "required": ["selected", "rationale"],
            "properties": {
                "selected": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "required": ["url", "reason"],
                        "properties": {
                            "url": {"type": "string"},
                            "reason": {"type": "string"},
                        },
                    },
                },
                "rationale": {"type": "string"},
            },
        },
    },
}


# Multi-part public suffixes that need 3 segments to form a registrable
# domain (e.g. example.co.uk has registrable "example.co.uk", not "co.uk").
# Covers the common country-code second-level domains. Not exhaustive —
# the Public Suffix List has hundreds — but covers the cases we actually
# see. Sites under obscure ccTLDs fall back to 2-segment matching, which
# is conservative (more pages rejected) rather than permissive.
_MULTI_PART_SUFFIXES = frozenset({
    # UK, AU, NZ, IN, etc.
    "co.uk", "org.uk", "ac.uk", "gov.uk", "nhs.uk", "ltd.uk", "me.uk",
    "com.au", "net.au", "org.au", "edu.au", "gov.au", "asn.au", "id.au",
    "co.nz", "net.nz", "org.nz", "govt.nz", "ac.nz", "school.nz",
    "co.in", "net.in", "org.in", "gen.in", "firm.in", "ind.in", "ac.in",
    "co.jp", "or.jp", "ne.jp", "ac.jp", "ad.jp", "ed.jp", "go.jp",
    "co.za", "org.za", "web.za", "ac.za", "gov.za",
    "com.br", "net.br", "org.br", "gov.br", "edu.br",
    "com.cn", "net.cn", "org.cn", "gov.cn", "edu.cn", "ac.cn",
    "com.mx", "org.mx", "net.mx", "edu.mx", "gob.mx",
    "com.sg", "edu.sg", "gov.sg", "org.sg", "net.sg",
    "com.hk", "edu.hk", "gov.hk", "org.hk", "net.hk",
})


def _registrable_domain(host: str) -> str:
    """Return the public-suffix-aware registrable portion of a hostname.

    Same-organization pages often live on subdomains (login.example.com,
    policy.example.com, shop.example.com). The site-crawl audit scope
    should include those because a single organization is responsible
    for conformance across everything they publish, regardless of which
    subdomain renders each page. But redirects to a fundamentally
    different organization (a third-party jobs board, donation
    processor, CDN-hosted app) are out of scope.

    This returns the lowercase registrable domain -- the last two labels
    for common TLDs, the last three labels for known multi-part public
    suffixes. Comparing two hosts by their registrable domain is the
    right grain for "same organization" scoping.
    """
    host = (host or "").lower().strip().strip(".")
    if not host:
        return ""
    parts = host.split(".")
    if len(parts) < 2:
        return host
    last_two = ".".join(parts[-2:])
    if len(parts) >= 3:
        last_two_only = ".".join(parts[-2:])
        if last_two_only in _MULTI_PART_SUFFIXES:
            return ".".join(parts[-3:])
    return last_two


async def summarize_pages(
    urls: list[str],
    timeout: float = 15.0,
    progress_callback=None,
) -> list[dict[str, str]]:
    """Fetch lightweight page metadata for each URL (non-LLM HTTP).

    Drops pages whose redirect chain lands on a DIFFERENT registrable
    domain than the origin. Same-organization subdomain redirects are
    kept — e.g. if www.example.com/login redirects to
    weblogin.example.com/cas, that's still the same organization and
    still in scope. But if /apply redirects to jobs.icims.com or
    something.workday.com, that's a third-party system the audited
    organization doesn't control and it's excluded. No site-specific
    allow-list is used — the rule is purely "same registrable domain".
    """
    from urllib.parse import urlparse

    summaries: list[dict[str, str]] = []
    origin_domain = ""
    if urls:
        try:
            origin_domain = _registrable_domain(urlparse(urls[0]).netloc)
        except Exception:
            origin_domain = ""
    dropped_offdomain = 0

    async with httpx.AsyncClient(
        timeout=httpx.Timeout(timeout, connect=10.0),
        follow_redirects=True,
    ) as client:
        for i, url in enumerate(urls):
            summary: dict[str, Any] = {
                "url": url, "title": "", "description": "",
                "headings": "", "has_forms": False,
                "has_tables": False, "has_media": False,
                "has_nav": True,
            }
            try:
                resp = await client.get(url)
                # Reject pages that redirected to a different
                # registrable domain -- those are third-party systems
                # the audited organization doesn't own. Same-org
                # subdomain redirects ARE kept (login.site.com,
                # policy.site.com, etc. are still in scope).
                try:
                    final_domain = _registrable_domain(urlparse(str(resp.url)).netloc)
                except Exception:
                    final_domain = ""
                if origin_domain and final_domain and final_domain != origin_domain:
                    dropped_offdomain += 1
                    logger.info(
                        "Dropping off-domain page from selection pool: %s -> %s (%s vs origin %s)",
                        url, resp.url, final_domain, origin_domain,
                    )
                    if progress_callback:
                        await progress_callback(
                            i + 1,
                            f"Skipped {i + 1}/{len(urls)}: third-party redirect ({final_domain})",
                        )
                    continue
                html = resp.text

                title_m = re.search(
                    r"<title[^>]*>(.*?)</title>", html, re.IGNORECASE | re.DOTALL
                )
                if title_m:
                    summary["title"] = title_m.group(1).strip()

                desc_m = re.search(
                    r'<meta\s+name=["\']description["\']\s+content=["\']([^"\']*)["\']',
                    html, re.IGNORECASE,
                )
                if desc_m:
                    summary["description"] = desc_m.group(1).strip()

                headings = re.findall(
                    r"<h[1-3][^>]*>(.*?)</h[1-3]>", html, re.IGNORECASE | re.DOTALL
                )
                clean = [re.sub(r"<[^>]+>", "", h).strip() for h in headings]
                summary["headings"] = " | ".join(h for h in clean if h)

                html_lower = html.lower()
                summary["has_forms"] = "<form" in html_lower
                summary["has_tables"] = "<table" in html_lower
                summary["has_media"] = any(
                    tag in html_lower
                    for tag in ("<video", "<audio", "<iframe", "youtube", "vimeo")
                )
                summary["has_nav"] = "<nav" in html_lower

            except Exception as exc:
                logger.warning("Failed to summarize %s: %s", url, exc)
                summary["title"] = "(failed to load)"

            summaries.append(summary)
            if progress_callback:
                await progress_callback(
                    i + 1,
                    f"Summarized {i + 1}/{len(urls)}: {summary['title'] or url}",
                )

    if dropped_offdomain:
        logger.info(
            "Page summarization complete: %d kept, %d dropped as off-domain redirects",
            len(summaries), dropped_offdomain,
        )
    return summaries


def _format_summaries_for_prompt(summaries: list[dict]) -> str:
    """Render page summaries as text for an AI prompt."""
    lines = []
    for i, s in enumerate(summaries, 1):
        parts = [f"{i}. {s['url']}"]
        if s.get("title"):
            parts.append(f"   Title: {s['title']}")
        if s.get("description"):
            parts.append(f"   Description: {s['description']}")
        if s.get("headings"):
            parts.append(f"   Headings: {s['headings']}")
        features = []
        if s.get("has_forms"):
            features.append("forms")
        if s.get("has_tables"):
            features.append("data tables")
        if s.get("has_media"):
            features.append("multimedia")
        if features:
            parts.append(f"   Content: {', '.join(features)}")
        lines.append("\n".join(parts))
    return "\n\n".join(lines)


_SITE_ANALYSIS_DEFAULT = {
    "sector": "other",
    "client_type": "",
    "primary_users": [],
    "critical_workflows": [],
    "additional_context": "Site type could not be determined automatically.",
}


async def analyze_site(
    summaries: list[dict[str, str]],
    ai_client,
) -> dict[str, Any]:
    """AI determines what a site is, who uses it, and its critical workflows.

    Returns a ProductContext-compatible dict. Returns safe defaults if the
    AI call fails or no summaries are provided.
    """
    if not summaries:
        return dict(_SITE_ANALYSIS_DEFAULT)

    pages_text = _format_summaries_for_prompt(summaries)

    system_prompt = (
        "You are analyzing a website to understand what it is, who uses it, "
        "and what it does. This understanding will guide a WCAG accessibility "
        "conformance evaluation. Be specific to THIS site based on evidence "
        "in the page summaries. Call the report_site_analysis tool."
    )
    user_prompt = (
        f"The site has {len(summaries)} pages. Here are their summaries:\n\n"
        f"{pages_text}\n\n"
        "Based on the page titles, headings, descriptions, and content types, "
        "determine the sector, client type, primary user groups, critical "
        "user workflows, and a short context summary."
    )

    try:
        llm = _resolve_llm(ai_client)
        result = await llm.call_with_tools(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            tool_name="report_site_analysis",
            tool_schema=_SITE_ANALYSIS_TOOL,
            temperature=0.1,
        )
    except Exception as exc:
        logger.warning("AI site analysis failed: %s", exc)
        return dict(_SITE_ANALYSIS_DEFAULT)

    if not result:
        return dict(_SITE_ANALYSIS_DEFAULT)

    normalized = {**_SITE_ANALYSIS_DEFAULT, **result}
    for key in ("primary_users", "critical_workflows"):
        if isinstance(normalized.get(key), str):
            normalized[key] = [normalized[key]]
    normalized.pop("regulatory_drivers", None)
    logger.info(
        "Site analyzed: %s (%s)",
        normalized.get("client_type", ""),
        normalized.get("sector", ""),
    )
    return normalized


async def select_pages(
    summaries: list[dict[str, str]],
    ai_client,
    max_pages: int = 10,
    coverage_level: str = "AA",
    wcag_version: str = "2.2",
    site_context: dict | None = None,
) -> dict[str, Any]:
    """AI selects the most important pages for WCAG testing.

    Returns {"selected": [{"url": ..., "reason": ...}, ...], "rationale": "..."}.
    Falls back to a heuristic list when AI is unavailable.
    """
    if not summaries:
        return {"selected": [], "rationale": "No pages to select."}

    pages_text = _format_summaries_for_prompt(summaries)

    context_section = ""
    if site_context:
        ctx_parts = []
        if site_context.get("client_type"):
            ctx_parts.append(f"Organization: {site_context['client_type']}")
        if site_context.get("sector"):
            ctx_parts.append(f"Sector: {site_context['sector']}")
        if site_context.get("primary_users"):
            ctx_parts.append(f"Primary users: {', '.join(site_context['primary_users'])}")
        if site_context.get("critical_workflows"):
            ctx_parts.append(f"Critical workflows: {', '.join(site_context['critical_workflows'])}")
        if site_context.get("additional_context"):
            ctx_parts.append(f"Context: {site_context['additional_context']}")
        if ctx_parts:
            context_section = (
                "\nSITE ANALYSIS (determined from page content):\n"
                + "\n".join(f"  {p}" for p in ctx_parts)
                + "\n\nUse this understanding to select pages covering the most "
                "critical user workflows and content types for THIS specific "
                "type of organization.\n"
            )

    system_prompt = (
        "You are an accessibility testing specialist selecting pages for a "
        "WCAG conformance evaluation. Your goal is to choose a representative "
        "sample that gives maximum coverage of accessibility issues for the "
        "specific users of THIS site.\n\n"
        f"{context_section}"
        "YOU DECIDE HOW MANY PAGES TO TEST. A proper Section 508 ACR needs "
        "enough pages to credibly say you evaluated the product. Consider:\n"
        "- Every distinct page template/layout needs at least one representative\n"
        "- Every critical user workflow needs its key pages tested\n"
        "- Login, signup, account, search results, transactions, forms — if "
        "they exist, they MUST be in the sample\n"
        "- The homepage is always included\n"
        "- A small marketing site might need 3-5 pages\n"
        "- A large university or government site might need 10-20+\n"
        "- Do NOT pad with redundant pages that share the same template\n"
        "- Do NOT skip pages just to keep the count low — include every page "
        "that covers a unique template, workflow, or content type\n\n"
        "Selection criteria (in order of priority):\n"
        "1. CRITICAL USER PATHS — Pages the primary users MUST be able to use "
        "(login, registration, checkout, search, key transactions).\n"
        "2. TEMPLATE DIVERSITY — At least one of each distinct layout type.\n"
        "3. CONTENT TYPE COVERAGE — Forms, data tables, multimedia, interactive "
        "widgets, documents (PDFs).\n"
        "4. RISK-BASED PRIORITY — Complex interactive content is more likely to "
        "have accessibility issues.\n"
        "5. ALWAYS include the homepage.\n\n"
        f"WCAG version: {wcag_version}, Level: {coverage_level}\n"
        "Select as many pages as needed for a credible ACR. Call the select_pages tool."
    )
    user_prompt = (
        f"The site crawler discovered {len(summaries)} pages. Select the pages "
        f"needed for a credible WCAG {wcag_version} Level {coverage_level} ACR.\n\n"
        f"DISCOVERED PAGES:\n{pages_text}"
    )

    try:
        llm = _resolve_llm(ai_client)
        result = await llm.call_with_tools(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            tool_name="select_pages",
            tool_schema=_PAGE_SELECTOR_TOOL,
            temperature=0.3,
        )
    except Exception as exc:
        logger.error("AI page selection failed: %s", exc)
        result = None

    if result:
        selected = result.get("selected", [])
        rationale = result.get("rationale", "")

        known_urls = {s["url"] for s in summaries}
        selected_urls = {s.get("url") for s in selected}
        if summaries and summaries[0]["url"] not in selected_urls:
            selected.insert(0, {
                "url": summaries[0]["url"],
                "reason": "Homepage -- always included as the primary entry point",
            })
        selected = [s for s in selected if s.get("url") in known_urls]
        logger.info("AI selected %d pages for testing (from %d discovered)",
                     len(selected), len(summaries))
        return {"selected": selected, "rationale": rationale}

    # Heuristic fallback
    fallback = []
    for s in summaries[:max_pages]:
        reason = "Selected by default (AI selection unavailable)"
        if s.get("has_forms"):
            reason = "Contains forms -- high priority for accessibility testing"
        elif s.get("has_tables"):
            reason = "Contains data tables -- requires header/scope testing"
        elif s.get("has_media"):
            reason = "Contains multimedia -- requires caption/description testing"
        fallback.append({"url": s["url"], "reason": reason})
    return {
        "selected": fallback,
        "rationale": (
            f"Automatic selection of the first {len(fallback)} discovered pages "
            f"(AI-powered selection was unavailable)."
        ),
    }


def _resolve_llm(ai_client) -> LLMClient:
    """Return an LLMClient for AI calls, accepting either a wrapper or an LLMClient."""
    if isinstance(ai_client, LLMClient):
        return ai_client
    inner = getattr(ai_client, "_llm", None)
    if isinstance(inner, LLMClient):
        return inner
    return LLMClient()
