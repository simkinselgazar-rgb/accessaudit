"""Site crawler for multi-page WCAG testing."""
from __future__ import annotations

import asyncio
import logging
import re
from collections import deque
from urllib.parse import urljoin, urlparse, urlunparse

import httpx
from playwright.async_api import async_playwright

from functions.security import UnsafeURLError, validate_public_url

logger = logging.getLogger(__name__)

SKIP_EXTENSIONS = {
    ".css", ".js", ".json", ".xml", ".rss", ".atom",
    ".png", ".jpg", ".jpeg", ".gif", ".svg", ".ico", ".webp", ".avif",
    ".zip", ".tar", ".gz", ".rar", ".7z",
    ".mp3", ".mp4", ".avi", ".mov", ".webm", ".ogg", ".wav",
    ".woff", ".woff2", ".ttf", ".eot",
    ".map", ".min.js", ".min.css",
}

# Documents are collected separately — not crawled as web pages
# but downloaded and tested with document-specific checks.
DOCUMENT_EXTENSIONS = {
    ".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx",
}


def normalize_url(url: str) -> str:
    """Normalize a URL: strip fragment, strip trailing slash.

    Also refuses URLs that resolve to private/loopback/link-local IPs —
    SSRF defense in depth. The caller (app.py routes) already validates
    at request time; this second check blocks URLs discovered mid-crawl
    via <a href> links into internal hosts.
    """
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        return ""
    normalized = urlunparse((
        parsed.scheme,
        parsed.netloc.lower(),
        parsed.path.rstrip("/") or "/",
        parsed.params,
        parsed.query,
        "",  # strip fragment
    ))
    try:
        validate_public_url(normalized)
    except UnsafeURLError:
        logger.warning("Refusing to crawl non-public URL %s", normalized)
        return ""
    return normalized


def is_same_domain(url: str, base_url: str) -> bool:
    """Check if URL is on the same domain as base_url."""
    return urlparse(url).netloc.lower() == urlparse(base_url).netloc.lower()


def should_skip_url(url: str) -> bool:
    """Check if URL should be skipped (non-page, non-document)."""
    parsed = urlparse(url)
    path = parsed.path.lower()
    if any(path.endswith(ext) for ext in SKIP_EXTENSIONS):
        return True
    # Documents are NOT skipped — they're collected separately
    return False


def is_document_url(url: str) -> bool:
    """Check if URL points to a document (PDF, DOCX, etc.)."""
    path = urlparse(url).path.lower()
    return any(path.endswith(ext) for ext in DOCUMENT_EXTENSIONS)


CRAWLER_USER_AGENT = (
    "Mozilla/5.0 (compatible; WCAG-Trusted-Tester/4.0; "
    "+https://github.com/simkinselgazar-rgb/accessaudit) accessibility-audit-bot"
)
_CRAWLER_HEADERS = {"User-Agent": CRAWLER_USER_AGENT}


async def parse_robots_txt(base_url: str) -> list[str]:
    """Parse robots.txt for Disallow rules."""
    disallowed = []
    robots_url = urljoin(base_url, "/robots.txt")
    try:
        async with httpx.AsyncClient(
            timeout=10, follow_redirects=True, headers=_CRAWLER_HEADERS,
        ) as client:
            resp = await client.get(robots_url)
            if resp.status_code == 200:
                current_agent = False
                for line in resp.text.splitlines():
                    line = line.strip()
                    if line.lower().startswith("user-agent:"):
                        agent = line.split(":", 1)[1].strip()
                        current_agent = agent == "*"
                    elif current_agent and line.lower().startswith("disallow:"):
                        path = line.split(":", 1)[1].strip()
                        if path:
                            disallowed.append(path)
    except Exception:
        logger.warning("robots.txt fetch/parse failed for %s", robots_url, exc_info=True)
    return disallowed


async def parse_sitemap(base_url: str) -> list[str]:
    """Parse sitemap.xml for URLs."""
    urls = []
    sitemap_url = urljoin(base_url, "/sitemap.xml")
    try:
        async with httpx.AsyncClient(
            timeout=10, follow_redirects=True, headers=_CRAWLER_HEADERS,
        ) as client:
            resp = await client.get(sitemap_url)
            if resp.status_code == 200:
                # Check for sitemap index
                if "<sitemapindex" in resp.text:
                    sitemap_locs = re.findall(r"<loc>(.*?)</loc>", resp.text)
                    for sub_url in sitemap_locs:
                        try:
                            sub_resp = await client.get(sub_url.strip())
                            if sub_resp.status_code == 200:
                                urls.extend(re.findall(r"<loc>(.*?)</loc>", sub_resp.text))
                        except Exception:
                            logger.debug("Skipping sub-sitemap %s", sub_url, exc_info=True)
                            continue
                else:
                    urls = re.findall(r"<loc>(.*?)</loc>", resp.text)
    except Exception:
        logger.warning("sitemap.xml fetch/parse failed for %s", sitemap_url, exc_info=True)
    return urls


async def crawl_site(
    start_url: str,
    max_pages: int = 10,
    max_depth: int = 3,
    progress_callback=None,
) -> dict[str, list[str]]:
    """BFS crawl a site to discover pages AND linked documents.

    Returns ``{"pages": [...], "documents": [...]}``

    Pages are HTML pages to test with web checks.  Documents are PDFs,
    DOCX, XLSX, PPTX files linked from the site — tested separately
    with document-specific checks but included in the same ACR.

    Args:
        start_url: Starting URL
        max_pages: Advisory limit — discovery continues beyond this so
            the AI page selector has a full pool to choose from.
        max_depth: Maximum crawl depth
        progress_callback: async callback(discovered, message)

    Returns:
        List of ALL discovered page URLs (may exceed max_pages).
    """
    # Discover up to this many pages total (well beyond max_pages)
    _DISCOVERY_CEILING = 500

    original_url = start_url
    start_url = normalize_url(start_url)
    if not start_url:
        return {"pages": [original_url] if original_url else [], "documents": []}

    logger.info("CRAWL START: %s (max_pages=%d, max_depth=%d)", start_url, max_pages, max_depth)

    visited: set[str] = set()
    discovered: list[str] = [start_url]
    discovered_docs: list[str] = []
    visited.add(start_url)

    # Parse robots.txt
    disallowed = await parse_robots_txt(start_url)
    logger.info("CRAWL robots.txt: %d disallow rules found", len(disallowed))

    def _is_allowed(norm: str) -> bool:
        path = urlparse(norm).path
        return not any(path.startswith(rule) for rule in disallowed)

    # ── Sitemap discovery ─────────────────────────────────────────
    logger.info("CRAWL: checking sitemap.xml...")
    sitemap_urls = await parse_sitemap(start_url)
    logger.info("CRAWL sitemap: %d URLs found", len(sitemap_urls))
    for url in sitemap_urls:
        norm = normalize_url(url)
        if (
            norm
            and norm not in visited
            and is_same_domain(norm, start_url)
            and not should_skip_url(norm)
            and _is_allowed(norm)
        ):
            visited.add(norm)
            if is_document_url(norm):
                discovered_docs.append(norm)
            else:
                discovered.append(norm)
            if len(discovered) >= _DISCOVERY_CEILING:
                break

    if progress_callback and (len(discovered) > 1 or discovered_docs):
        await progress_callback(
            len(discovered),
            f"Found {len(discovered)} pages + {len(discovered_docs)} documents from sitemap",
        )

    # ── BFS crawl with Playwright (handles JS-rendered navigation) ─
    queue: deque[tuple[str, int]] = deque([(start_url, 0)])

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(
            viewport={"width": 1280, "height": 720},
            ignore_https_errors=True,
        )

        logger.info("CRAWL BFS: starting with %d URLs in queue", len(queue))
        while queue and len(discovered) < _DISCOVERY_CEILING:
            url, depth = queue.popleft()
            if depth > max_depth:
                continue

            logger.info("CRAWL BFS [depth=%d]: visiting %s (%d pages, %d docs so far)",
                        depth, url, len(discovered), len(discovered_docs))
            try:
                page = await context.new_page()
                await page.goto(url, wait_until="domcontentloaded", timeout=20000)
                # Wait longer for JS-heavy sites (SPAs, React, etc.)
                await page.wait_for_timeout(2500)

                # Extract links from <a href>, onclick, data-href,
                # and navigation buttons — covers JS-heavy CMS platforms
                links = await page.evaluate("""
                    () => {
                        const urls = new Set();

                        // Standard <a> links
                        document.querySelectorAll('a[href]').forEach(a => {
                            if (a.href && a.href.startsWith('http')) urls.add(a.href);
                        });

                        // Elements with data-href or data-url attributes
                        document.querySelectorAll('[data-href], [data-url]').forEach(el => {
                            const u = el.getAttribute('data-href') || el.getAttribute('data-url');
                            if (u && u.startsWith('http')) urls.add(u);
                        });

                        // Links inside <nav> that might be buttons/spans with onclick
                        document.querySelectorAll('nav a[href], [role="navigation"] a[href]').forEach(a => {
                            if (a.href && a.href.startsWith('http')) urls.add(a.href);
                        });

                        return Array.from(urls);
                    }
                """)

                await page.close()

                for link in links:
                    norm = normalize_url(link)
                    if not norm or norm in visited:
                        continue
                    if not is_same_domain(norm, start_url):
                        continue
                    if should_skip_url(norm):
                        continue
                    if not _is_allowed(norm):
                        continue

                    visited.add(norm)
                    if is_document_url(norm):
                        discovered_docs.append(norm)
                        # Don't queue documents for BFS crawling
                    else:
                        discovered.append(norm)
                        queue.append((norm, depth + 1))

                    if progress_callback:
                        await progress_callback(
                            len(discovered),
                            f"Discovered {len(discovered)} pages (depth {depth + 1})",
                        )

                    if len(discovered) >= _DISCOVERY_CEILING:
                        break

            except Exception as e:
                logger.warning("Error crawling %s: %s", url, e)
                continue

        # ── AI fallback: if BFS found very few links, ask the AI ──
        # Modern CMS platforms hide navigation in JS frameworks,
        # custom elements, or dynamically-loaded menus that BFS misses.
        # Rather than hardcoding every pattern, let the LLM read the
        # rendered page and extract what it can see.
        if len(discovered) < 5 and len(discovered) < _DISCOVERY_CEILING:
            try:
                homepage = await context.new_page()
                await homepage.goto(start_url, wait_until="networkidle", timeout=30000)
                await homepage.wait_for_timeout(3000)

                # Get the full rendered HTML for the AI
                rendered_html = await homepage.content()
                await homepage.close()

                ai_links = await _ai_extract_links(rendered_html, start_url)
                for link in ai_links:
                    norm = normalize_url(link)
                    if (
                        norm
                        and norm not in visited
                        and is_same_domain(norm, start_url)
                        and not should_skip_url(norm)
                        and _is_allowed(norm)
                    ):
                        visited.add(norm)
                        discovered.append(norm)
                        if progress_callback:
                            await progress_callback(
                                len(discovered),
                                f"AI found: {norm}",
                            )
                        if len(discovered) >= _DISCOVERY_CEILING:
                            break

                if progress_callback and ai_links:
                    await progress_callback(
                        len(discovered),
                        f"AI extracted {len(ai_links)} additional navigation links",
                    )
            except Exception as e:
                logger.warning("AI link extraction fallback failed: %s", e)

        await browser.close()

    logger.info(
        "Crawl complete: %d pages + %d documents discovered (max_pages=%d)",
        len(discovered), len(discovered_docs), max_pages,
    )
    return {"pages": discovered, "documents": discovered_docs}


async def _ai_extract_links(html: str, base_url: str) -> list[str]:
    """Ask the LLM to extract internal navigation URLs from rendered HTML.

    Handles JS-heavy sites where standard link extraction fails. Goes
    through ``LLMClient.call_with_tools`` so the 3-attempt retry +
    prose-restructure cascade handles malformed model output.
    """
    from functions.llm import LLMClient
    from functions.tools import LINK_EXTRACTOR_TOOL

    system_prompt = (
        "You are a web crawler. Extract ALL internal navigation URLs from the "
        "HTML provided. Look at navigation menus, headers, footers, sidebars, "
        "links hidden in JavaScript/onclick handlers/data attributes, dropdown "
        "and hamburger menus, and links to subpages or important sections."
    )
    user_prompt = f"Base URL: {base_url}\n\nHTML:\n{html}"

    try:
        llm = LLMClient()
        payload = await llm.call_with_tools(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            tool_name="extract_navigation_links",
            tool_schema=LINK_EXTRACTOR_TOOL,
            temperature=0.0,
        )
        if payload is None:
            return []
        urls = payload.get("urls", []) or []
        if not isinstance(urls, list):
            return []
        cleaned = [u for u in urls if isinstance(u, str) and u.startswith("http")]
        logger.info("AI extracted %d navigation URLs", len(cleaned))
        return cleaned
    except Exception as exc:
        logger.warning("AI link extraction failed: %s", exc)

    return []
