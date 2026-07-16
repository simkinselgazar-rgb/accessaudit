"""Web page capture using Playwright.

Captures screenshots, DOM, accessibility tree, element data,
and page observation video for WCAG testing.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from pathlib import Path

from playwright.async_api import async_playwright, Page, BrowserContext

from models import CaptureData
from config import (
    PAGE_OBSERVATION_DURATION,
    AI_FRAME_INTERVAL,
    FLASH_DETECTION_FPS,
    PLAYWRIGHT_TIMEOUT,
    VIEWPORT_WIDTH,
    VIEWPORT_HEIGHT,
    VIEWPORT_NARROW,
    ZOOM_FACTOR,
)
from functions.element_labeler import LABELER_JS_BUNDLE, ensure_label_fields

logger = logging.getLogger(__name__)

# Maximum size for external scripts to download (bytes)
SCRIPT_MAX_SIZE = 500_000


async def capture_web_page(
    url: str,
    review_dir: str,
    user_context: dict | None = None,
    auth_callback=None,
) -> CaptureData:
    """Capture a web page for WCAG accessibility testing.

    Phase 0 - Page observation: record video, extract AI / flash frames.
    Phase 1 - Main capture: screenshots, DOM, a11y tree, element extraction.

    If the page requires authentication (login page detected), opens a
    visible browser for the user to log in, saves the session, and
    reuses it for this and all subsequent captures in the review.

    Args:
        url: The URL to capture.
        review_dir: Path to the review output directory.
        user_context: Optional user-provided context about the page.
        auth_callback: Optional async callback for auth progress messages.

    Returns:
        A populated CaptureData instance.
    """
    captures_dir = os.path.join(review_dir, "captures")
    os.makedirs(captures_dir, exist_ok=True)

    capture_data = CaptureData(
        url=url,
        review_dir=review_dir,
        captures_dir=captures_dir,
        user_context=user_context or {},
    )

    # Check for saved auth state (from a previous page in this review)
    from capture.auth import get_auth_state_path
    # review root is one level up from page_NNN dirs
    review_root = review_dir
    if os.path.basename(review_dir).startswith("page_") or os.path.basename(review_dir).startswith("doc_"):
        review_root = os.path.dirname(review_dir)
    auth_state = get_auth_state_path(review_root, url=url)

    async with async_playwright() as pw:
        # ── Phase 0: Page observation ────────────────────────────────
        try:
            await _phase0_observation(pw, url, captures_dir, capture_data)
        except Exception:
            logger.exception("Phase 0 (observation) failed for %s", url)

        # ── Phase 1: Main capture ────────────────────────────────────
        browser = await pw.chromium.launch()
        try:
            # Use saved auth state if available
            ctx_kwargs = {"viewport": {"width": VIEWPORT_WIDTH, "height": VIEWPORT_HEIGHT}}
            if auth_state:
                ctx_kwargs["storage_state"] = auth_state

            context = await browser.new_context(**ctx_kwargs)
            context.set_default_timeout(PLAYWRIGHT_TIMEOUT)
            page = await context.new_page()
            await page.goto(url, wait_until="networkidle", timeout=PLAYWRIGHT_TIMEOUT)
            # networkidle settles when network goes quiet; SPAs can still be
            # mutating the DOM. Wait for DOM mutations to quiet too so the
            # capture phase sees the final hydrated UI, not mid-hydration.
            await _wait_for_dom_stabilization(page)

            # ── Login detection ──────────────────────────────────────
            # If this looks like a login page, open a visible browser
            # for the user to authenticate, then reload with the session.
            from capture.auth import detect_login_page, authenticate_interactive
            if await detect_login_page(page):
                logger.info("Login page detected at %s — requesting authentication", url)
                await page.close()
                await browser.close()

                state_path = await authenticate_interactive(
                    url, review_root, progress_callback=auth_callback,
                )
                if state_path:
                    auth_state = state_path
                    # Relaunch with authenticated state
                    browser = await pw.chromium.launch()
                    context = await browser.new_context(
                        viewport={"width": VIEWPORT_WIDTH, "height": VIEWPORT_HEIGHT},
                        storage_state=auth_state,
                    )
                    context.set_default_timeout(PLAYWRIGHT_TIMEOUT)
                    page = await context.new_page()
                    await page.goto(url, wait_until="networkidle", timeout=PLAYWRIGHT_TIMEOUT)
                else:
                    logger.warning("Authentication skipped/timed out — capturing login page as-is")
                    browser = await pw.chromium.launch()
                    context = await browser.new_context(
                        viewport={"width": VIEWPORT_WIDTH, "height": VIEWPORT_HEIGHT},
                    )
                    context.set_default_timeout(PLAYWRIGHT_TIMEOUT)
                    page = await context.new_page()
                    await page.goto(url, wait_until="networkidle", timeout=PLAYWRIGHT_TIMEOUT)

            capture_data.title = await page.title()

            # Screenshots
            await _capture_screenshots(page, context, captures_dir, capture_data)

            # DOM / Accessibility tree
            await _capture_dom(page, captures_dir, capture_data)

            # Computed styles for contrast
            await _capture_computed_styles(page, capture_data)

            # Non-text contrast for UI components (1.4.11)
            await _capture_nontext_contrast(page, capture_data)

            # Pixel-level contrast from full-page screenshot (1.4.3, 1.4.6, 1.4.11)
            await _capture_pixel_contrast(page, capture_data, captures_dir)

            # Shadow DOM element extraction
            await _capture_shadow_dom(page, capture_data)

            # Script content
            await _capture_script_content(page, capture_data)

            # ARIA reference and role validation
            _capture_aria_validation(capture_data)

            # Accessibility overlay widget detection (UserWay, AccessiBe, etc.)
            await _capture_overlay_widgets(page, capture_data)

            # Axe core programmatic analysis
            await _capture_axe(page, captures_dir, capture_data)

            # HTML_CodeSniffer (Squiz Labs, BSD-3) — second deterministic
            # ruleset, different rule logic from axe. Catches heading-skip
            # patterns and label-association cases axe misses; the judge
            # weighs cross-tool agreement when consolidating.
            await _capture_htmlcs(page, captures_dir, capture_data)

            # IBM Equal Access Accessibility Checker (Apache 2.0) — third
            # ruleset, strongest on ARIA validity (aria-controls/aria-owns
            # references, role-required-attr coverage, custom-widget
            # patterns). Three independent deterministic tools agreeing
            # is very strong evidence; one alone is a candidate for the
            # judge to evaluate visually.
            await _capture_ibm_eac(page, captures_dir, capture_data)

            # ANDI-style per-text-node contrast analysis (SSA Section 508
            # methodology). Complements axe's element-level pass and the
            # K-means pixel sampler with text-node granularity + SVG text.
            await _capture_andi_contrast(page, captures_dir, capture_data)

            # ANDI-style language audit (sANDI). Validates html lang +
            # every per-segment lang attr against BCP 47, detects
            # redundant declarations and xml:lang mismatches.
            await _capture_andi_lang(page, captures_dir, capture_data)

            # ANDI-style hidden-content audit (hANDI). Catches focusable
            # elements that are simultaneously hidden — phantom tab
            # stops, ARIA-spec violations.
            await _capture_andi_hidden(page, captures_dir, capture_data)

            # ANDI-style graphics audit (gANDI). Per-image accessibility:
            # img/svg/input-image/area/bg-image, with link/button
            # context (image-only link severity uplift).
            await _capture_andi_graphics(page, captures_dir, capture_data)

            # ANDI-style tables audit (tANDI). Data vs layout
            # classification, scope/headers validation.
            await _capture_andi_tables(page, captures_dir, capture_data)

            # ANDI-style links/buttons audit (lANDI). Accessible-name
            # vs visible-text comparison, ambiguous text detection.
            await _capture_andi_interactive(page, captures_dir, capture_data)

            # Element extraction (populates capture_data.images with
            # screenshot_path entries — prerequisite for the VLM pass
            # below, which reads those screenshot paths).
            await _extract_elements(page, captures_dir, capture_data)

            # VLM image analysis: caption + OCR + alt-text semantic
            # verification for every visible image. Feeds SC 1.1.1
            # (semantic alt verification) and SC 1.4.5 (images-of-text
            # detection). Uses local Gemma 26B + bge-m3 embeddings.
            try:
                await _capture_vlm_image_analysis(capture_data)
            except Exception:
                logger.exception("VLM image analysis failed; continuing")

            # Overflow detection at 200% zoom
            await _detect_overflow_zoom(page, capture_data)

            # Overflow detection at 320px width
            await _detect_overflow_narrow(page, context, url, capture_data)

            # ── Phase 2: Interactive capture ────────────────────────
            try:
                from capture.interactive_capture import run_interactive_tests
                await run_interactive_tests(page, capture_data, review_dir)
            except Exception:
                logger.exception("Phase 2 (interactive) failed for %s", url)

        except Exception:
            logger.exception("Phase 1 (main capture) failed for %s", url)
        finally:
            await browser.close()

    # Save structural summary for cross-page comparison (3.2.3, 3.2.4)
    _save_structural_summary(captures_dir, capture_data)

    return capture_data


def _save_structural_summary(captures_dir: str, capture_data: CaptureData) -> None:
    """Save lightweight structural summary for cross-page aggregation."""
    try:
        summary = {
            "url": capture_data.url,
            "title": capture_data.title,
            "nav_links": [
                {"text": lnk.get("text", ""), "href": lnk.get("href", "")}
                for lnk in capture_data.links
                if lnk.get("in_nav") or "nav" in (lnk.get("parent_tag", "") or "").lower()
            ],
            "heading_pattern": [
                {"level": h.get("level", 0), "text": h.get("text", "")}
                for h in capture_data.headings
            ],
            "landmarks": [
                {"role": lm.get("role", ""), "label": lm.get("ariaLabel", "") or lm.get("aria-label", "")}
                for lm in capture_data.landmarks
            ],
            "form_labels": [
                {"type": ff.get("type", ""), "label": ff.get("label", ""), "name": ff.get("name", "")}
                for ff in capture_data.form_fields
            ],
        }
        summary_path = os.path.join(captures_dir, "structural_summary.json")
        with open(summary_path, "w", encoding="utf-8") as f:
            json.dump(summary, f, indent=2)
        logger.debug("Structural summary saved to %s", summary_path)
    except Exception as e:
        logger.warning("Failed to save structural summary: %s", e)


# ─── Phase 0 helpers ─────────────────────────────────────────────────────────

async def _phase0_observation(
    pw,
    url: str,
    captures_dir: str,
    capture_data: CaptureData,
) -> None:
    """Record observation video, extract AI frames and flash frames."""
    from capture.frame_extractor import extract_frames, analyze_flash_rate

    video_dir = os.path.join(captures_dir, "observation_video")
    os.makedirs(video_dir, exist_ok=True)

    browser = await pw.chromium.launch()
    context = await browser.new_context(
        viewport={"width": VIEWPORT_WIDTH, "height": VIEWPORT_HEIGHT},
        record_video_dir=video_dir,
        record_video_size={"width": VIEWPORT_WIDTH, "height": VIEWPORT_HEIGHT},
    )
    context.set_default_timeout(PLAYWRIGHT_TIMEOUT)
    page = await context.new_page()

    try:
        await page.goto(url, wait_until="networkidle", timeout=PLAYWRIGHT_TIMEOUT)
        # Check if the page has dynamic content that warrants full observation
        dynamic_info = await page.evaluate("""() => {
            const hasVideo = document.querySelectorAll('video[autoplay], video[src]').length > 0;
            const hasAudio = document.querySelectorAll('audio[autoplay]').length > 0;
            const hasAnimations = document.getAnimations ? document.getAnimations().length > 0 : false;
            const hasMarquee = document.querySelectorAll('marquee, [class*="carousel"], [class*="slider"], [class*="rotate"], [class*="scroll"]').length > 0;
            const hasAutoRefresh = !!document.querySelector('meta[http-equiv="refresh"]');

            // Detect JS timer patterns (session timeouts, auto-updates)
            let hasTimers = false;
            let timerHint = '';
            const scripts = Array.from(document.querySelectorAll('script'));
            const allScript = scripts.map(s => s.textContent || '').join(' ').toLowerCase();
            if (allScript.includes('settimeout')) { hasTimers = true; timerHint = 'setTimeout'; }
            if (allScript.includes('setinterval')) { hasTimers = true; timerHint += (timerHint ? '+' : '') + 'setInterval'; }
            const timerKw = ['session', 'timeout', 'expire', 'countdown', 'timer'];
            if (timerKw.some(kw => allScript.includes(kw))) {
                hasTimers = true; timerHint += (timerHint ? '+' : '') + 'session/timeout';
            }
            return {
                hasDynamic: hasVideo || hasAudio || hasAnimations || hasMarquee || hasAutoRefresh,
                hasTimers: hasTimers,
                timerHint: timerHint,
            };
        }""")
        has_dynamic = dynamic_info.get("hasDynamic", False)
        has_timers = dynamic_info.get("hasTimers", False)

        if has_timers:
            observe_duration = min(PAGE_OBSERVATION_DURATION * 2, 120)
            logger.info("Observing %ds (timers: %s): %s", observe_duration, dynamic_info.get("timerHint", ""), url)
        elif has_dynamic:
            observe_duration = PAGE_OBSERVATION_DURATION
            logger.info("Observing %ds (dynamic content): %s", observe_duration, url)
        else:
            observe_duration = min(PAGE_OBSERVATION_DURATION, 15)
            logger.info("Observing %ds (static): %s", observe_duration, url)
        await asyncio.sleep(observe_duration)
    except Exception:
        logger.exception("Error during page observation navigation")
    finally:
        await page.close()
        await context.close()
        await browser.close()

    # Locate the saved video file
    video_files = list(Path(video_dir).glob("*.webm"))
    if not video_files:
        logger.warning("No observation video was recorded")
        return

    video_path = str(video_files[0])
    capture_data.observation_video_path = video_path

    # Extract AI frames (one per AI_FRAME_INTERVAL seconds)
    ai_frames_dir = os.path.join(captures_dir, "ai_frames")
    os.makedirs(ai_frames_dir, exist_ok=True)
    try:
        ai_fps = 1.0 / AI_FRAME_INTERVAL
        ai_frames = await extract_frames(video_path, ai_fps, ai_frames_dir)
        capture_data.observation_frames = ai_frames
    except Exception:
        logger.exception("AI frame extraction failed")

    # Extract flash frames at FLASH_DETECTION_FPS
    flash_frames_dir = os.path.join(captures_dir, "flash_frames")
    os.makedirs(flash_frames_dir, exist_ok=True)
    try:
        await extract_frames(video_path, FLASH_DETECTION_FPS, flash_frames_dir)
        flash_result = await analyze_flash_rate(flash_frames_dir)
        capture_data.flash_analysis = flash_result
    except Exception:
        logger.exception("Flash analysis failed")


# ─── Phase 1 helpers ─────────────────────────────────────────────────────────

async def _capture_screenshots(
    page: Page,
    context: BrowserContext,
    captures_dir: str,
    capture_data: CaptureData,
) -> None:
    """Capture full-page and viewport screenshots at various sizes."""
    # Full page screenshot
    full_page_path = os.path.join(captures_dir, "full_page.png")
    try:
        await page.screenshot(path=full_page_path, full_page=True)
        capture_data.full_page_path = full_page_path
    except Exception:
        logger.exception("Full-page screenshot failed")

    # Viewport screenshot (default size)
    viewport_path = os.path.join(captures_dir, "viewport.png")
    try:
        await page.screenshot(path=viewport_path, full_page=False)
        capture_data.viewport_path = viewport_path
    except Exception:
        logger.exception("Viewport screenshot failed")

    # Viewport at 200% zoom
    viewport_200_path = os.path.join(captures_dir, "viewport_200pct.png")
    try:
        original_factor = ZOOM_FACTOR
        await page.evaluate(f"document.body.style.zoom = '{original_factor}'")
        await page.wait_for_timeout(500)
        await page.screenshot(path=viewport_200_path, full_page=False)
        capture_data.viewport_200pct_path = viewport_200_path
        await page.evaluate("document.body.style.zoom = '1'")
        await page.wait_for_timeout(300)
    except Exception:
        logger.exception("200%% zoom screenshot failed")

    # Viewport at 320px width
    viewport_320_path = os.path.join(captures_dir, "viewport_320px.png")
    try:
        await page.set_viewport_size({"width": VIEWPORT_NARROW, "height": VIEWPORT_HEIGHT})
        await page.wait_for_timeout(500)
        await page.screenshot(path=viewport_320_path, full_page=False)
        capture_data.viewport_320px_path = viewport_320_path
        # Restore default viewport
        await page.set_viewport_size({"width": VIEWPORT_WIDTH, "height": VIEWPORT_HEIGHT})
        await page.wait_for_timeout(300)
    except Exception:
        logger.exception("320px viewport screenshot failed")
        try:
            await page.set_viewport_size({"width": VIEWPORT_WIDTH, "height": VIEWPORT_HEIGHT})
        except Exception:
            pass  # cleanup — best-effort viewport reset, page state may be unrecoverable


async def _capture_dom(
    page: Page,
    captures_dir: str,
    capture_data: CaptureData,
) -> None:
    """Save DOM HTML and accessibility tree."""
    # DOM HTML
    dom_path = os.path.join(captures_dir, "dom.html")
    try:
        html = await page.content()
        capture_data.html = html
        with open(dom_path, "w", encoding="utf-8") as f:
            f.write(html)
        capture_data.dom_path = dom_path
    except Exception:
        logger.exception("DOM capture failed")

    # Accessibility tree via CDP
    try:
        cdp = await page.context.new_cdp_session(page)
        result = await cdp.send("Accessibility.getFullAXTree")
        capture_data.a11y_tree = result
        a11y_path = os.path.join(captures_dir, "a11y_tree.json")
        with open(a11y_path, "w", encoding="utf-8") as f:
            json.dump(result, f, indent=2)
        await cdp.detach()
    except Exception:
        logger.exception("Accessibility tree capture failed")


async def _capture_computed_styles(page: Page, capture_data: CaptureData) -> None:
    """Extract computed colour styles for contrast analysis."""
    try:
        styles = await page.evaluate("""() => {
            const results = [];
            const selector = 'body, body *';
            const elements = document.querySelectorAll(selector);
            for (const el of elements) {
                const cs = window.getComputedStyle(el);
                // innerText only — skips SR-only / off-screen content
                const text = (el.innerText || '').trim();
                if (!text) continue;
                if (cs.display === 'none' || cs.visibility === 'hidden') continue;
                if (parseFloat(cs.opacity) === 0) continue;
                const rect = el.getBoundingClientRect();
                if (rect.width < 2 || rect.height < 2) continue;
                // Walk up DOM to find effective background color
                let bgColor = cs.backgroundColor;
                let bgEl = el;
                let reachedRoot = false;
                while (bgColor === 'rgba(0, 0, 0, 0)' || bgColor === 'transparent') {
                    bgEl = bgEl.parentElement;
                    if (!bgEl) { bgColor = 'rgb(255, 255, 255)'; reachedRoot = true; break; }
                    bgColor = window.getComputedStyle(bgEl).backgroundColor;
                }
                // Check for background-image on this or parent elements
                let hasBgImage = false;
                let checkEl = el;
                while (checkEl) {
                    const bgImg = window.getComputedStyle(checkEl).backgroundImage;
                    if (bgImg && bgImg !== 'none') { hasBgImage = true; break; }
                    checkEl = checkEl.parentElement;
                }
                // Reached document root with no explicit bg-color — could be
                // over a gradient, image, positioned sibling, or pseudo-
                // element backdrop that this walk can't see. Treat as
                // unknown-backdrop so downstream checks emit INFO, not HIGH.
                if (reachedRoot) { hasBgImage = true; }
                // Check opacity
                let effectiveOpacity = 1;
                let opEl = el;
                while (opEl) {
                    effectiveOpacity *= parseFloat(window.getComputedStyle(opEl).opacity) || 1;
                    opEl = opEl.parentElement;
                }
                results.push({
                    tag: el.tagName.toLowerCase(),
                    text: text,
                    color: cs.color,
                    backgroundColor: bgColor,
                    fontSize: cs.fontSize,
                    fontWeight: cs.fontWeight,
                    selector: _buildSelector(el),
                    hasBgImage: hasBgImage,
                    effectiveOpacity: effectiveOpacity,
                });
            }
            function _buildSelector(el) {
                if (el.id) return '#' + el.id;
                let path = el.tagName.toLowerCase();
                if (el.className && typeof el.className === 'string') {
                    path += '.' + el.className.trim().split(/\\s+/).join('.');
                }
                return path;
            }
            return results;
        }""")
        capture_data.computed_styles = styles
    except Exception:
        logger.exception("Computed styles capture failed")


async def _capture_nontext_contrast(page: Page, capture_data: CaptureData) -> None:
    """Capture border, outline, and icon contrast for SC 1.4.11.

    Evaluates every UI component (buttons, inputs, selects, links, custom
    controls) and extracts their boundary colors against adjacent backgrounds.
    Also captures focus indicator colors by briefly focusing each element.
    Results go into capture_data.nontext_contrast as a list of dicts with
    pre-computed contrast ratios so the check and AI don't have to guess.
    """
    from functions.js_helpers import EFFECTIVE_BG_JS
    try:
        results = await page.evaluate("() => {" + EFFECTIVE_BG_JS + """
            function luminance(r, g, b) {
                const [rs, gs, bs] = [r, g, b].map(c => {
                    c = c / 255;
                    return c <= 0.03928 ? c / 12.92 : Math.pow((c + 0.055) / 1.055, 2.4);
                });
                return 0.2126 * rs + 0.7152 * gs + 0.0722 * bs;
            }
            function contrastRatio(l1, l2) {
                const lighter = Math.max(l1, l2);
                const darker = Math.min(l1, l2);
                return (lighter + 0.05) / (darker + 0.05);
            }
            function parseRGB(str) {
                if (!str) return null;
                const m = str.match(/rgba?\\((\\d+),\\s*(\\d+),\\s*(\\d+)/);
                if (!m) return null;
                return [parseInt(m[1]), parseInt(m[2]), parseInt(m[3])];
            }
            function buildSelector(el) {
                if (el.id) return '#' + el.id;
                let s = el.tagName.toLowerCase();
                if (el.className && typeof el.className === 'string')
                    s += '.' + el.className.trim().split(/\\s+/)[0];
                return s;
            }
            function getAccessibleName(el) {
                return el.getAttribute('aria-label')
                    || el.getAttribute('aria-labelledby') && (() => {
                        const ids = el.getAttribute('aria-labelledby').split(/\\s+/);
                        return ids.map(id => {
                            const ref = document.getElementById(id);
                            return ref ? ref.textContent.trim() : '';
                        }).join(' ');
                    })()
                    || el.getAttribute('alt')
                    || el.getAttribute('title')
                    || '';
            }
            function getNearestLandmark(el) {
                let cur = el;
                while (cur) {
                    const role = cur.getAttribute('role');
                    if (role && ['banner','navigation','main','contentinfo',
                        'search','complementary','form','region'].includes(role))
                        return role;
                    const tag = cur.tagName.toLowerCase();
                    if (tag === 'header') return 'banner';
                    if (tag === 'nav') return 'navigation';
                    if (tag === 'main') return 'main';
                    if (tag === 'footer') return 'contentinfo';
                    if (tag === 'aside') return 'complementary';
                    cur = cur.parentElement;
                }
                return '';
            }

            const UI_SELECTORS = 'button, input, select, textarea, a[href], ' +
                '[role="button"], [role="checkbox"], [role="radio"], [role="switch"], ' +
                '[role="slider"], [role="tab"], [role="menuitem"], [role="link"], ' +
                'summary, [tabindex="0"]';
            const elements = document.querySelectorAll(UI_SELECTORS);
            const results = [];
            const seen = new Set();

            for (const el of elements) {
                const rect = el.getBoundingClientRect();
                if (rect.width < 2 || rect.height < 2) continue;
                if (el.closest('[aria-hidden="true"]')) continue;
                const cs = window.getComputedStyle(el);
                if (cs.display === 'none' || cs.visibility === 'hidden') continue;

                const sel = buildSelector(el);
                if (seen.has(sel)) continue;
                seen.add(sel);

                const bg = effectiveBg(el);
                const bgRGB = parseRGB(bg);
                const bgLum = bgRGB ? luminance(...bgRGB) : null;

                const accessibleName = getAccessibleName(el);
                const landmark = getNearestLandmark(el);
                const entry = {
                    selector: sel,
                    tag: el.tagName.toLowerCase(),
                    role: el.getAttribute('role') || '',
                    text: (el.textContent || '').trim(),
                    accessible_name: accessibleName,
                    landmark: landmark,
                    rect: {
                        x: Math.round(rect.x),
                        y: Math.round(rect.y),
                        width: Math.round(rect.width),
                        height: Math.round(rect.height),
                    },
                    background: bg,
                };

                // Border contrast
                const borderColor = cs.borderColor;
                const borderWidth = parseInt(cs.borderWidth) || 0;
                if (borderWidth > 0 && borderColor && borderColor !== 'rgba(0, 0, 0, 0)') {
                    const bRGB = parseRGB(borderColor);
                    if (bRGB && bgRGB) {
                        const bLum = luminance(...bRGB);
                        entry.border_color = borderColor;
                        entry.border_width = borderWidth + 'px';
                        entry.border_contrast = Math.round(contrastRatio(bLum, bgLum) * 100) / 100;
                    }
                }

                // Outline contrast (unfocused state)
                const outlineColor = cs.outlineColor;
                const outlineWidth = parseInt(cs.outlineWidth) || 0;
                const outlineStyle = cs.outlineStyle;
                if (outlineWidth > 0 && outlineStyle !== 'none' && outlineColor) {
                    const oRGB = parseRGB(outlineColor);
                    if (oRGB && bgRGB) {
                        const oLum = luminance(...oRGB);
                        entry.outline_color = outlineColor;
                        entry.outline_contrast = Math.round(contrastRatio(oLum, bgLum) * 100) / 100;
                    }
                }

                // Box-shadow (often used as focus indicator)
                if (cs.boxShadow && cs.boxShadow !== 'none') {
                    entry.box_shadow = cs.boxShadow;
                }

                // Focus indicator: briefly focus the element to capture focus styles
                const prevFocus = document.activeElement;
                try {
                    el.focus({preventScroll: true});
                    const fcs = window.getComputedStyle(el);
                    const fOutlineColor = fcs.outlineColor;
                    const fOutlineWidth = parseInt(fcs.outlineWidth) || 0;
                    const fOutlineStyle = fcs.outlineStyle;
                    const fBorderColor = fcs.borderColor;
                    const fBoxShadow = fcs.boxShadow;

                    if (fOutlineWidth > 0 && fOutlineStyle !== 'none' && fOutlineColor) {
                        const foRGB = parseRGB(fOutlineColor);
                        if (foRGB && bgRGB) {
                            const foLum = luminance(...foRGB);
                            entry.focus_outline_color = fOutlineColor;
                            entry.focus_outline_width = fOutlineWidth + 'px';
                            entry.focus_outline_contrast = Math.round(contrastRatio(foLum, bgLum) * 100) / 100;
                        }
                    }
                    if (fBorderColor !== borderColor) {
                        const fbRGB = parseRGB(fBorderColor);
                        if (fbRGB && bgRGB) {
                            const fbLum = luminance(...fbRGB);
                            entry.focus_border_color = fBorderColor;
                            entry.focus_border_contrast = Math.round(contrastRatio(fbLum, bgLum) * 100) / 100;
                        }
                    }
                    if (fBoxShadow && fBoxShadow !== 'none' && fBoxShadow !== cs.boxShadow) {
                        entry.focus_box_shadow = fBoxShadow;
                    }
                } catch(e) {}
                // Restore previous focus
                try {
                    if (prevFocus && prevFocus !== el) prevFocus.focus({preventScroll: true});
                    else el.blur();
                } catch(e) {}

                // Only include if we have something to report
                if (entry.border_contrast !== undefined ||
                    entry.outline_contrast !== undefined ||
                    entry.focus_outline_contrast !== undefined ||
                    entry.focus_border_contrast !== undefined ||
                    entry.box_shadow || entry.focus_box_shadow) {
                    results.push(entry);
                }
            }
            return results;
        }""")

        capture_data.nontext_contrast = results
        logger.info("Non-text contrast: captured %d UI components with contrast data", len(results))
    except Exception:
        logger.exception("Non-text contrast capture failed")


async def _capture_pixel_contrast(
    page: Page, capture_data: CaptureData, captures_dir: str,
) -> None:
    """Sample rendered pixel colors for contrast analysis (1.4.3, 1.4.6, 1.4.11).

    Instead of parsing CSS color values (which miss gradients, images,
    overlays, transparency), this reads the actual rendered pixels from
    the full-page screenshot. The browser already composited all layers.
    """
    from functions.contrast import sample_element_colors, is_large_text

    t0 = time.monotonic()

    screenshot_path = capture_data.full_page_path
    if not screenshot_path or not os.path.isfile(screenshot_path):
        logger.warning("Pixel contrast: no full-page screenshot available, skipping")
        return

    try:
        element_data = await page.evaluate(r"""() => {
            const results = [];
            const els = document.querySelectorAll('body *');
            function getEffBg(el) {
                let bg = getComputedStyle(el).backgroundColor;
                let cur = el;
                while (bg === 'rgba(0, 0, 0, 0)' || bg === 'transparent') {
                    cur = cur.parentElement;
                    if (!cur) return 'rgb(255, 255, 255)';
                    bg = getComputedStyle(cur).backgroundColor;
                }
                return bg;
            }
            // sr-only / visually-hidden detection. Returns true for the
            // standard 1px-clipped pattern (Bootstrap .visually-hidden,
            // .sr-only, etc.). Bootstrap's .visually-hidden-focusable
            // applies this style by default and only un-hides on :focus,
            // so the text exists in textContent but is invisible to
            // sighted users — measuring contrast on it gives white-on-
            // white at the parent ancestor and produces 74 of 324
            // pixel_contrast entries with ratio=1.0 false positives.
            const isVisuallyHidden = (el) => {
                if (!el || el.nodeType !== 1) return false;
                const cs = window.getComputedStyle(el);
                if (cs.display === 'none' || cs.visibility === 'hidden') return true;
                if (parseFloat(cs.opacity) === 0) return true;
                if (cs.position === 'absolute' || cs.position === 'fixed') {
                    if (cs.clip === 'rect(0px, 0px, 0px, 0px)' ||
                        cs.clip === 'rect(0, 0, 0, 0)' ||
                        (cs.clipPath && cs.clipPath !== 'none' && cs.clipPath !== 'auto' &&
                         (cs.clipPath.includes('inset(100%)') || cs.clipPath.includes('inset(50%)')))) {
                        return true;
                    }
                    const r = el.getBoundingClientRect();
                    if (r.width <= 1 && r.height <= 1) return true;
                    if (r.right < 0 || r.bottom < 0) return true;
                    if (r.left < -1000 || r.top < -1000) return true;
                }
                return false;
            };
            // Visible-only text — recursively collect text from non-hidden
            // descendants. If empty, the element has no actually-visible
            // text and contributes nothing measurable to contrast.
            const collectVisibleText = (el) => {
                if (!el) return '';
                if (isVisuallyHidden(el)) return '';
                let out = '';
                for (const node of el.childNodes) {
                    if (node.nodeType === 3) {
                        out += node.textContent || '';
                    } else if (node.nodeType === 1) {
                        out += collectVisibleText(node);
                    }
                }
                return out;
            };
            // Same selector algorithm as inventory / overflow / forms.
            function getSelector(el) {
                if (el.id) return '#' + CSS.escape(el.id);
                const parts = [];
                let current = el;
                while (current && current !== document.body && current !== document.documentElement) {
                    let seg = current.tagName.toLowerCase();
                    if (current.id) {
                        parts.unshift('#' + CSS.escape(current.id));
                        break;
                    }
                    if (current.parentElement) {
                        const sibs = Array.from(current.parentElement.children)
                            .filter(s => s.tagName === current.tagName);
                        if (sibs.length > 1) {
                            seg += ':nth-of-type(' + (sibs.indexOf(current) + 1) + ')';
                        }
                    }
                    parts.unshift(seg);
                    current = current.parentElement;
                    if (parts.length >= 4) break;
                }
                return parts.join(' > ');
            }
            for (const el of els) {
                if (isVisuallyHidden(el)) continue;
                const text = collectVisibleText(el).trim();
                if (!text) continue;
                // Only process leaf-ish text containers — elements where
                // the text is directly rendered (not containers of containers).
                const blockChildren = Array.from(el.children).filter(c => {
                    const d = getComputedStyle(c).display;
                    return d === 'block' || d === 'flex' || d === 'grid' || d === 'list-item';
                });
                if (blockChildren.length > 2) continue;
                const cs = window.getComputedStyle(el);
                if (cs.display === 'none' || cs.visibility === 'hidden') continue;
                if (parseFloat(cs.opacity) === 0) continue;
                const rect = el.getBoundingClientRect();
                if (rect.width < 2 || rect.height < 2) continue;

                const cssBg = getEffBg(el);
                const hasBgImage = cs.backgroundImage !== 'none';

                results.push({
                    selector: getSelector(el),
                    text: text,
                    rect: {
                        x: Math.round(rect.x + window.scrollX),
                        y: Math.round(rect.y + window.scrollY),
                        width: Math.round(rect.width),
                        height: Math.round(rect.height),
                    },
                    fontSize: parseFloat(cs.fontSize) || 16,
                    fontWeight: cs.fontWeight || '400',
                    cssColor: cs.color,
                    cssBg: cssBg,
                    hasBgImage: hasBgImage,
                });
            }
            return results;
        }""")
    except Exception:
        logger.exception("Pixel contrast: failed to query text element rects")
        return

    results = []
    for el in element_data:
        rect = el.get("rect", {})
        if rect.get("width", 0) < 4 or rect.get("height", 0) < 4:
            continue

        font_size = el.get("fontSize", 16)
        font_weight = el.get("fontWeight", "400")
        has_bg_image = el.get("hasBgImage", False)

        try:
            sample = sample_element_colors(screenshot_path, rect)
        except Exception:
            continue

        fg = sample.get("fg_color")
        bg = sample.get("bg_color")
        cr = sample.get("contrast_ratio")
        method = sample.get("method", "pixel_sample")

        if fg is None or bg is None or cr is None:
            continue

        results.append({
            "selector": el.get("selector", ""),
            "text": el.get("text", ""),
            "fg_rgb": list(fg),
            "bg_rgb": list(bg),
            "ratio": cr,
            "is_large": is_large_text(font_size, font_weight),
            "font_size": font_size,
            "font_weight": font_weight,
            "method": method,
            "has_bg_image": has_bg_image,
        })

    capture_data.pixel_contrast = results
    elapsed = time.monotonic() - t0
    logger.info(
        "Pixel contrast: sampled %d text elements in %.1fs",
        len(results), elapsed,
    )


async def _capture_shadow_dom(page: Page, capture_data: CaptureData) -> None:
    """Extract elements from shadow DOM roots and merge into capture data."""
    t0 = time.monotonic()
    try:
        from functions.shadow_dom import merge_shadow_into_capture
        await merge_shadow_into_capture(page, capture_data)
    except Exception:
        logger.exception("Shadow DOM capture failed")
    elapsed = time.monotonic() - t0
    logger.info("Shadow DOM capture completed in %.1fs", elapsed)


def _capture_aria_validation(capture_data: CaptureData) -> None:
    """Validate ARIA attribute references and role usage."""
    t0 = time.monotonic()
    try:
        from functions.aria_validator import run_all_validations

        elements = []
        for field_name in ['form_fields', 'links', 'images', 'shadow_elements']:
            elements.extend(getattr(capture_data, field_name, []))

        issues = run_all_validations(capture_data.html, elements)
        capture_data.aria_issues = issues
        elapsed = time.monotonic() - t0
        logger.info(
            "ARIA validation: %d issues found in %.1fs",
            len(issues), elapsed,
        )
    except Exception:
        logger.exception("ARIA validation failed")


async def _capture_script_content(page: Page, capture_data: CaptureData) -> None:
    """Extract inline and small external script content."""
    try:
        content = await page.evaluate("""() => {
            const parts = [];
            const scripts = document.querySelectorAll('script');
            for (const s of scripts) {
                if (s.textContent && s.textContent.trim()) {
                    parts.push('// inline script\\n' + s.textContent.trim());
                }
            }
            return parts.join('\\n\\n');
        }""")
        # Fetch small external scripts
        ext_scripts = await page.evaluate("""() => {
            const urls = [];
            const scripts = document.querySelectorAll('script[src]');
            for (const s of scripts) urls.push(s.src);
            return urls;
        }""")
        for src_url in ext_scripts:
            try:
                resp = await page.context.request.get(src_url)
                body = await resp.text()
                if len(body) <= SCRIPT_MAX_SIZE:
                    content += f"\n\n// external: {src_url}\n{body}"
                else:
                    # Don't silently lose a large bundle: downstream code
                    # analysis (4.1.2 / 2.1.1 widget handlers) reasons over
                    # this. Log so the gap is visible.
                    logger.warning(
                        "external script over %d bytes skipped from script_content: "
                        "%s (%d bytes) -- custom-widget handlers in this bundle "
                        "will not reach code analysis",
                        SCRIPT_MAX_SIZE, src_url, len(body),
                    )
            except Exception:
                logger.warning("external script fetch/decode failed, skipped: %s", src_url, exc_info=True)
        capture_data.script_content = content
    except Exception:
        logger.exception("Script content extraction failed")


# ── Accessibility overlay widget detection ─────────────────────────────────
# Marketing-installed overlays (UserWay, AccessiBe, EqualWeb, AudioEye,
# UserWay, Recite Me, etc.) are a recurring pattern on university sites.
# They promise "one-click accessibility" but in practice inject shadow-DOM
# widgets that intercept keyboard focus, override ARIA attributes, and
# produce their own WCAG failures. The tool scans for known vendor
# <script src> URLs and records matches so every SC check can emit an
# info finding alerting auditors that focus/ARIA behaviour on the page
# may be overridden by the overlay.
_OVERLAY_VENDOR_PATTERNS: list[tuple[str, str]] = [
    ("cdn.userway.org", "UserWay"),
    ("userway.org/widget", "UserWay"),
    ("accessibe.com/access", "AccessiBe"),
    ("acsbapp.com", "AccessiBe"),
    ("acsbap.com", "AccessiBe"),
    ("equalweb.com", "EqualWeb"),
    ("pojo-ac.com", "EqualWeb"),
    ("audioeye.com", "AudioEye"),
    ("ae.audioeye.com", "AudioEye"),
    ("reciteme.com", "Recite Me"),
    ("usablenet.com", "UsableNet"),
    ("onlineada.com", "Online ADA"),
    ("accessibility.com/wcag", "Accessibility.com Widget"),
    ("maxaccess.io", "Max Access"),
]


async def _capture_overlay_widgets(page: Page, capture_data: CaptureData) -> None:
    """Detect accessibility overlay widgets injected into the page.

    Populates ``capture_data.overlay_widgets`` with one entry per
    detected vendor: ``{"vendor": str, "src": str, "selector": str}``.
    """
    try:
        patterns_js = "[" + ", ".join(
            f'["{pat}", "{vendor}"]' for pat, vendor in _OVERLAY_VENDOR_PATTERNS
        ) + "]"
        overlays = await page.evaluate(
            f"""() => {{
                const patterns = {patterns_js};
                const hits = [];
                const seen = new Set();
                for (const script of document.querySelectorAll('script[src]')) {{
                    const src = script.src || '';
                    for (const [pat, vendor] of patterns) {{
                        if (src.indexOf(pat) !== -1) {{
                            const key = vendor + '|' + src;
                            if (seen.has(key)) continue;
                            seen.add(key);
                            hits.push({{
                                vendor: vendor,
                                src: src,
                                selector: 'script[src*="' + pat + '"]',
                            }});
                        }}
                    }}
                }}
                // Also look for iframe widgets (AccessiBe renders an
                // iframe in the corner after script load)
                for (const iframe of document.querySelectorAll('iframe')) {{
                    const src = iframe.src || '';
                    for (const [pat, vendor] of patterns) {{
                        if (src.indexOf(pat) !== -1) {{
                            const key = vendor + '|' + src;
                            if (seen.has(key)) continue;
                            seen.add(key);
                            hits.push({{
                                vendor: vendor,
                                src: src,
                                selector: 'iframe[src*="' + pat + '"]',
                            }});
                        }}
                    }}
                }}
                return hits;
            }}"""
        )
        if isinstance(overlays, list) and overlays:
            capture_data.overlay_widgets = overlays
            vendors = sorted({o.get("vendor", "?") for o in overlays})
            logger.warning(
                "OVERLAY WIDGET DETECTED: %s -- accessibility behaviour on "
                "this page may be altered by the overlay; every SC finding "
                "should be flagged for manual verification",
                ", ".join(vendors),
            )
    except Exception:
        logger.exception("Overlay widget detection failed")


# ── Post-navigation hydration stabilization ─────────────────────────────────

async def _wait_for_dom_stabilization(
    page: Page,
    quiet_ms: int = 500,
    max_wait_ms: int = 5000,
) -> None:
    """Wait for DOM mutations to quiet down after initial navigation.

    ``networkidle`` only means network is quiet — it does NOT mean the
    page has finished hydrating. SPAs (Canvas Insights, late React
    renders, Tableau embeds) continue mutating the DOM after network
    settles. This helper watches with a MutationObserver and returns
    when no mutations have been observed for ``quiet_ms`` milliseconds,
    or after ``max_wait_ms`` elapsed. Never blocks forever.
    """
    try:
        await page.evaluate(
            f"""() => new Promise(resolve => {{
                const quietMs = {quiet_ms};
                const maxMs = {max_wait_ms};
                let lastMutation = Date.now();
                const observer = new MutationObserver(() => {{
                    lastMutation = Date.now();
                }});
                observer.observe(document.body, {{
                    childList: true,
                    subtree: true,
                    attributes: true,
                    characterData: true,
                }});
                const start = Date.now();
                function check() {{
                    const now = Date.now();
                    if (now - lastMutation >= quietMs) {{
                        observer.disconnect();
                        resolve(true);
                        return;
                    }}
                    if (now - start >= maxMs) {{
                        observer.disconnect();
                        resolve(false);
                        return;
                    }}
                    setTimeout(check, 100);
                }}
                setTimeout(check, 100);
            }})"""
        )
    except Exception:
        # Don't let a stabilization failure block capture
        pass


async def _capture_vlm_image_analysis(capture_data: CaptureData) -> None:
    """Run every visible image through the local Gemma VLM.

    Populates per-image fields on each ``capture_data.images`` entry:
      - ``vlm_caption`` -- one-sentence description (used by SC 1.1.1
        semantic alt verification)
      - ``vlm_extracted_text`` -- text transcribed from inside the
        image (used by SC 1.4.5 to flag images-of-text)
      - ``vlm_alt_similarity`` -- bge-m3 cosine similarity between the
        author-supplied alt text and the VLM caption. Only populated
        when both the embedding host AND the VLM produced output and
        an alt text exists.

    Skips images that:
      - Have no per-image screenshot_path (can't send anything to VLM)
      - Are marked aria-hidden or role="presentation" / role="none"
        (decorative by author intent; evaluation is off-scope)
      - Have rect width or height <= 16 px (icon-sized; too small for
        meaningful VLM analysis, would just add noise)

    Runs sequentially through LLMClient's in-flight gate so it respects
    the local model's concurrency setting. No cap on image count --
    every qualifying image is analyzed.
    """
    from functions.embeddings import cosine_similarity, embed_batch
    from functions.image_analysis import analyze_image

    images = capture_data.images or []
    if not images:
        return

    to_analyze: list[tuple[dict, str]] = []
    for img in images:
        screenshot = img.get("screenshot_path") or ""
        if not screenshot or not os.path.exists(screenshot):
            continue
        role = (img.get("role") or "").lower()
        if role in ("presentation", "none"):
            continue
        aria_hidden = img.get("aria_hidden") or img.get("aria-hidden")
        if aria_hidden in (True, "true", "True"):
            continue
        rect = img.get("rect") or {}
        if isinstance(rect, dict):
            w = rect.get("width", 0) or 0
            h = rect.get("height", 0) or 0
            if w and h and (w < 16 or h < 16):
                continue
        to_analyze.append((img, screenshot))

    if not to_analyze:
        return

    logger.info(
        "VLM image analysis: running Gemma 26B on %d image(s)",
        len(to_analyze),
    )

    captions: list[tuple[dict, str, str]] = []  # (img, caption, alt)

    for img, screenshot in to_analyze:
        try:
            result = await analyze_image(screenshot)
        except Exception as exc:
            logger.warning("VLM analysis failed for %s: %s", screenshot, exc)
            continue
        caption = result.get("caption", "")
        extracted = result.get("extracted_text", "")
        if caption:
            img["vlm_caption"] = caption
        if extracted:
            img["vlm_extracted_text"] = extracted
        alt = (img.get("alt") or "").strip()
        if caption and alt:
            captions.append((img, caption, alt))

    # Compute cosine similarity between alt and caption for every image
    # that has both. Batch-embed in one pass so we amortize the Ollama
    # round-trip cost.
    if captions:
        try:
            texts: list[str] = []
            for _, caption, alt in captions:
                texts.append(alt)
                texts.append(caption)
            vectors = await embed_batch(texts)
            for idx, (img, _caption, _alt) in enumerate(captions):
                alt_vec = vectors[idx * 2]
                cap_vec = vectors[idx * 2 + 1]
                if any(alt_vec) and any(cap_vec):
                    img["vlm_alt_similarity"] = round(
                        cosine_similarity(alt_vec, cap_vec), 4
                    )
        except Exception as exc:
            logger.info(
                "VLM alt-similarity scoring skipped (embeddings unavailable): %s",
                exc,
            )

    logger.info(
        "VLM image analysis complete: %d captioned, %d with extracted text, "
        "%d with alt-similarity scores",
        sum(1 for img, _ in to_analyze if img.get("vlm_caption")),
        sum(1 for img, _ in to_analyze if img.get("vlm_extracted_text")),
        sum(1 for img, _ in to_analyze if "vlm_alt_similarity" in img),
    )


async def _capture_axe(page: Page, captures_dir: str, capture_data: CaptureData) -> None:
    """Run axe-core for programmatic accessibility testing.

    Explicit configuration:
      - ``iframes: true`` — instructs axe to recurse into same-origin
        iframes. Cross-origin iframes remain blocked by the browser.
      - ``runOnly`` with every WCAG rule tag so axe runs every relevant
        WCAG 2.0/2.1/2.2 A/AA/AAA check plus best-practice rules. The
        previous default omitted explicit tag filtering, which can skip
        AAA checks on some axe versions.
      - Shadow DOM is traversed by axe's native walker when the rule
        engine reaches a custom element; no extra config needed.

    After the run we log the engine version and the number of iframes
    axe actually inspected so operators can detect when same-origin
    iframe scanning failed.
    """
    try:
        await page.add_script_tag(url="https://cdnjs.cloudflare.com/ajax/libs/axe-core/4.9.0/axe.min.js")
        axe_results = await page.evaluate(
            """async () => {
                const result = await axe.run(document, {
                    iframes: true,
                    resultTypes: ['violations', 'passes', 'incomplete', 'inapplicable'],
                    runOnly: {
                        type: 'tag',
                        values: [
                            'wcag2a', 'wcag2aa', 'wcag2aaa',
                            'wcag21a', 'wcag21aa',
                            'wcag22aa',
                            'best-practice',
                            'section508',
                            'ACT',
                            'EN-301-549',
                        ],
                    },
                });
                return result;
            }"""
        )
        capture_data.axe_results = axe_results

        # Save raw results
        axe_path = os.path.join(captures_dir, "axe_results.json")
        with open(axe_path, "w", encoding="utf-8") as f:
            json.dump(axe_results, f, indent=2)

        # Log engine diagnostics so operators can see what axe actually
        # inspected. Frames that axe couldn't read will be absent from
        # its testEngine output on some builds; we log both the engine
        # version and the frame IDs it reports seeing.
        engine = axe_results.get("testEngine") or {}
        engine_version = engine.get("version", "?")
        test_runner = (axe_results.get("testRunner") or {}).get("name", "?")
        logger.info(
            "AXE ran: engine=%s runner=%s violations=%d passes=%d incomplete=%d",
            engine_version,
            test_runner,
            len(axe_results.get("violations") or []),
            len(axe_results.get("passes") or []),
            len(axe_results.get("incomplete") or []),
        )
    except Exception:
        logger.exception("Axe-core extraction failed")


# CDN-hosted bundles for the two extra deterministic engines. Pinned to
# specific versions so engine output stays stable across runs (a moving
# "@latest" would invalidate stored transcripts every time the tool
# author cuts a release). Both are open-source: HTML_CodeSniffer is
# BSD-3 (Squiz Labs), accessibility-checker-engine is Apache 2.0 (IBM).
_HTMLCS_CDN = "https://cdn.jsdelivr.net/npm/html_codesniffer@2.5.1/build/HTMLCS.js"
_IBM_EAC_CDN = "https://cdn.jsdelivr.net/npm/accessibility-checker-engine@3.1.79/ace.js"


async def _capture_htmlcs(page: Page, captures_dir: str, capture_data: CaptureData) -> None:
    """Run HTML_CodeSniffer (Squiz Labs) for a second deterministic
    accessibility check independent of axe-core.

    HCS implements WCAG 2.0/2.1/2.2 + Section 508 with a separate rule
    engine; its messages cover patterns axe doesn't (heading-skip
    sequences, several label-association cases, some role/state checks).
    Three message ``type`` levels exist: 1=ERROR (definite failure),
    2=WARNING (likely failure, manual review), 3=NOTICE (informational).
    The extractor maps these to Severity HIGH/MEDIUM/INFO.

    The bundle is loaded from a pinned jsdelivr URL. If the network
    blocks the CDN (offline laptop, restricted CI), this capture is a
    soft-no-op — it logs a warning and the SCs that depended on
    htmlcs_results just don't see those findings. Other deterministic
    sources (axe, ANDI, IBM EAC) keep flowing.
    """
    try:
        await page.add_script_tag(url=_HTMLCS_CDN)
        # HCS runs asynchronously — it walks the DOM and invokes a callback
        # when done. We wrap in a Promise the page.evaluate can await.
        # Standard "WCAG2AAA" runs every WCAG rule HCS implements; the
        # criterion-level filter happens in functions/htmlcs_extract.py
        # (mirroring the axe extractor's tag filter).
        htmlcs_results = await page.evaluate(
            """async () => {
                return await new Promise((resolve) => {
                    if (typeof HTMLCS === "undefined") {
                        resolve({error: "HTMLCS bundle did not load",
                                 messages: []});
                        return;
                    }
                    HTMLCS.process("WCAG2AAA", document, () => {
                        const raw = HTMLCS.getMessages();
                        const messages = raw.map(m => {
                            // m.element is a live DOM node; serialize it
                            // to a stable selector so the extractor can
                            // compare against the captured DOM.
                            let selector = "";
                            const el = m.element;
                            if (el && el.nodeType === 1) {
                                if (el.id) {
                                    selector = "#" + el.id;
                                } else {
                                    const tag = el.tagName.toLowerCase();
                                    const cls = (el.className && typeof el.className === "string")
                                        ? "." + el.className.trim().split(/\\s+/).join(".")
                                        : "";
                                    selector = tag + cls;
                                }
                            }
                            return {
                                type: m.type,
                                code: m.code || "",
                                msg: m.msg || "",
                                selector: selector,
                                tag_name: (el && el.nodeType === 1)
                                    ? el.tagName.toLowerCase() : "",
                            };
                        });
                        resolve({
                            messages: messages,
                            standard: "WCAG2AAA",
                            engine: "HTML_CodeSniffer",
                            version: (HTMLCS.version || "2.5.1"),
                        });
                    });
                });
            }"""
        )
        capture_data.htmlcs_results = htmlcs_results

        path = os.path.join(captures_dir, "htmlcs_results.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(htmlcs_results, f, indent=2)

        msgs = htmlcs_results.get("messages") or []
        # type 1 ERROR, 2 WARNING, 3 NOTICE
        errors = sum(1 for m in msgs if m.get("type") == 1)
        warnings = sum(1 for m in msgs if m.get("type") == 2)
        notices = sum(1 for m in msgs if m.get("type") == 3)
        logger.info(
            "HTMLCS ran: engine=%s standard=%s errors=%d warnings=%d notices=%d",
            htmlcs_results.get("engine", "?"),
            htmlcs_results.get("standard", "?"),
            errors, warnings, notices,
        )
    except Exception:
        logger.warning(
            "HTML_CodeSniffer capture failed -- other deterministic "
            "sources (axe, ANDI, IBM EAC) still ran. Findings that "
            "would have come from HCS will be absent for this review.",
            exc_info=True,
        )


async def _capture_ibm_eac(page: Page, captures_dir: str, capture_data: CaptureData) -> None:
    """Run IBM Equal Access Accessibility Checker engine for ARIA-heavy
    rule coverage.

    IBM's engine has the strongest open-source ARIA validity coverage:
    ``aria-controls`` / ``aria-owns`` reference resolution, required-
    children-for-role checks (e.g. ``listbox`` must contain ``option``),
    role-conflict detection, and custom-widget keyboard expectations.
    Each result carries a ``value`` of [level, judgment] where the
    judgment is one of ``PASS`` / ``FAIL`` / ``POTENTIAL`` / ``MANUAL``;
    the extractor maps FAIL to severity HIGH, POTENTIAL/MANUAL to INFO.

    Same soft-no-op semantics as HCS: if the CDN is blocked, log and
    move on. Cross-source corroboration is still possible from axe + HCS.
    """
    try:
        await page.add_script_tag(url=_IBM_EAC_CDN)
        ibm_results = await page.evaluate(
            """async () => {
                if (typeof ace === "undefined" || !ace.Checker) {
                    return {error: "IBM EAC bundle did not load",
                            results: []};
                }
                const checker = new ace.Checker();
                // 'IBM_Accessibility' is the recommended policy and
                // covers WCAG 2.1 AA + IBM's additional checks. Other
                // policies available: WCAG_2_1, WCAG_2_2, EN_301_549.
                const report = await checker.check(document, ['IBM_Accessibility']);
                const results = (report.results || []).map(r => {
                    return {
                        ruleId: r.ruleId || "",
                        value: r.value || [],
                        path_dom: (r.path && r.path.dom) ? r.path.dom : "",
                        path_aria: (r.path && r.path.aria) ? r.path.aria : "",
                        message: r.message || "",
                        snippet: r.snippet || "",
                        category: r.category || "",
                        help: r.help || "",
                    };
                });
                return {
                    results: results,
                    engine: "ibm-equal-access",
                    policy: "IBM_Accessibility",
                    version: (ace.version || "3.x"),
                    summary: (report.summary || {}),
                };
            }"""
        )
        capture_data.ibm_eac_results = ibm_results

        path = os.path.join(captures_dir, "ibm_eac_results.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(ibm_results, f, indent=2)

        results = ibm_results.get("results") or []
        # value is [level, judgment] e.g. ["VIOLATION", "FAIL"]
        fails = sum(1 for r in results
                    if (r.get("value") or [""])[-1] == "FAIL")
        potentials = sum(1 for r in results
                         if (r.get("value") or [""])[-1] == "POTENTIAL")
        manuals = sum(1 for r in results
                      if (r.get("value") or [""])[-1] == "MANUAL")
        logger.info(
            "IBM_EAC ran: engine=%s policy=%s fails=%d potentials=%d manuals=%d",
            ibm_results.get("engine", "?"),
            ibm_results.get("policy", "?"),
            fails, potentials, manuals,
        )
    except Exception:
        logger.warning(
            "IBM Equal Access capture failed -- other deterministic "
            "sources (axe, ANDI, HTMLCS) still ran. Findings that "
            "would have come from IBM EAC will be absent for this review.",
            exc_info=True,
        )


async def _capture_andi_contrast(
    page: Page, captures_dir: str, capture_data: CaptureData,
) -> None:
    """ANDI-style per-text-node contrast measurement.

    The official ANDI bookmarklet from SSA's Section 508 office (https://
    www.ssa.gov/accessibility/andi/) iterates every visible text-bearing
    node, resolves the computed foreground colour (``color`` for HTML or
    ``fill``/``stroke`` for SVG ``<text>``), walks up the DOM to find the
    first ancestor with a non-transparent ``background-color`` (recording
    whether any ancestor used ``background-image``), classifies the text
    as large-text vs normal-text per WCAG 1.4.3, and reports the WCAG
    contrast ratio plus the threshold it must meet.

    This complements the existing ``capture_data.colors`` extractor (which
    keys on element tag and dedups by fg|bg) and the K-means
    ``capture_data.pixel_contrast`` (which samples the rendered pixels):
    ANDI's per-text-node walk catches per-text-node colour overrides and
    SVG text that the element-level walk misses, and it records the
    walk-up depth so consumers can tell when an effective background was
    deeply inherited (often a sign of a complex layered context where
    ANDI's text-only resolution may diverge from what the user sees).

    JS side returns raw rgba/rgb strings + metadata; Python composes them
    through the canonical WCAG math in ``functions/contrast.py`` so every
    contrast value in the captured artifacts uses the same formula.

    Saves to ``<captures_dir>/andi_contrast.json`` and to
    ``capture_data.andi_contrast_results``.
    """
    try:
        raw = await page.evaluate("""() => {
            const out = [];

            const isVisuallyHidden = (el) => {
                if (!el || el.nodeType !== 1) return false;
                const cs = window.getComputedStyle(el);
                if (cs.display === 'none' || cs.visibility === 'hidden') return true;
                if (parseFloat(cs.opacity) === 0) return true;
                if (cs.position === 'absolute' || cs.position === 'fixed') {
                    if (cs.clip === 'rect(0px, 0px, 0px, 0px)' ||
                        cs.clip === 'rect(0, 0, 0, 0)' ||
                        (cs.clipPath && cs.clipPath !== 'none' && cs.clipPath !== 'auto' &&
                         (cs.clipPath.includes('inset(100%)') || cs.clipPath.includes('inset(50%)')))) {
                        return true;
                    }
                    const r = el.getBoundingClientRect();
                    if (r.width <= 1 && r.height <= 1) return true;
                    if (r.right < 0 || r.bottom < 0) return true;
                    if (r.left < -1000 || r.top < -1000) return true;
                }
                return false;
            };

            const hasHiddenAncestor = (el) => {
                let p = el;
                while (p) {
                    if (p.nodeType === 1 && isVisuallyHidden(p)) return true;
                    if (p.nodeType === 1) {
                        if (p.getAttribute('aria-hidden') === 'true') return true;
                        if (p.hasAttribute('hidden')) return true;
                    }
                    p = p.parentNode;
                }
                return false;
            };

            const buildSelector = (el) => {
                if (!el || el.nodeType !== 1) return '';
                if (el.id) return el.tagName.toLowerCase() + '#' + el.id;
                let path = el.tagName.toLowerCase();
                if (el.className && typeof el.className === 'string') {
                    const cls = el.className.trim().split(/\\s+/).filter(c => c).slice(0, 2);
                    if (cls.length) path += '.' + cls.join('.');
                }
                let parent = el.parentElement;
                while (parent && parent.tagName.toLowerCase() !== 'body' && path.length < 200) {
                    let seg = parent.tagName.toLowerCase();
                    if (parent.id) {
                        path = seg + '#' + parent.id + ' > ' + path;
                        return path;
                    }
                    if (parent.className && typeof parent.className === 'string') {
                        const cls = parent.className.trim().split(/\\s+/).filter(c => c).slice(0, 1);
                        if (cls.length) seg += '.' + cls.join('.');
                    }
                    path = seg + ' > ' + path;
                    parent = parent.parentElement;
                }
                return path;
            };

            // ANDI's published methodology only walks parent backgroundColor /
            // backgroundImage. That misses three CSS patterns common on
            // modern sites — and produced a 1.23:1 false positive on text
            // over a hero image (off-white text against a walked-up white
            // root, because the actual hero image was on a ::before, not on
            // an ancestor's background-image).
            //
            // Three augmentations, all generalised:
            //   (a) For each ancestor, also inspect ::before / ::after
            //       pseudo-element backgrounds. Common pattern:
            //         .hero { position: relative; }
            //         .hero::before {
            //           content:''; position:absolute; inset:0;
            //           background-image: url(hero.jpg); z-index: -1;
            //         }
            //   (b) Use document.elementsFromPoint at the text's centre
            //       to detect overlay layers that aren't in the ancestor
            //       chain — e.g. an absolutely-positioned sibling carrying
            //       the image. The browser knows what is *actually painted*
            //       behind the text; the parent walk doesn't.
            //   (c) Both checks set bg_image_present=true; the ratio still
            //       resolves from the walked-up bg-color, but the consumer
            //       (BaseCheck._extract_andi_contrast_findings) downgrades
            //       any HIGH/MEDIUM finding on a bg_image_present element
            //       to INFO because the resolved colour is unreliable.
            const elementHasBgImage = (el) => {
                if (!el || el.nodeType !== 1) return '';
                const cs = window.getComputedStyle(el);
                if (cs.backgroundImage && cs.backgroundImage !== 'none') return 'self';
                try {
                    const before = window.getComputedStyle(el, '::before');
                    if (before && before.backgroundImage && before.backgroundImage !== 'none') {
                        return '::before';
                    }
                } catch (e) { /* some browsers throw on no-pseudo */ }
                try {
                    const after = window.getComputedStyle(el, '::after');
                    if (after && after.backgroundImage && after.backgroundImage !== 'none') {
                        return '::after';
                    }
                } catch (e) { /* same */ }
                return '';
            };

            const isAncestor = (el, maybeAnc) => {
                let p = el;
                while (p) { if (p === maybeAnc) return true; p = p.parentElement; }
                return false;
            };

            const checkPaintStack = (textHostEl) => {
                // Returns a non-empty string when an overlay layer painted
                // BEHIND the text host carries a background-image that the
                // parent walk wouldn't see. The browser's elementsFromPoint
                // honours z-index / position / pseudo-element layering, so
                // this catches sibling-overlay and absolutely-positioned
                // backdrop patterns.
                try {
                    const r = textHostEl.getBoundingClientRect();
                    const cx = Math.min(Math.max(r.left + r.width/2, 0), window.innerWidth - 1);
                    const cy = Math.min(Math.max(r.top + r.height/2, 0), window.innerHeight - 1);
                    const stack = document.elementsFromPoint(cx, cy) || [];
                    for (const layer of stack) {
                        if (layer === textHostEl) continue;
                        if (isAncestor(textHostEl, layer)) continue;  // ancestors already covered
                        const via = elementHasBgImage(layer);
                        if (via) {
                            const id = layer.id ? '#' + layer.id : '';
                            return 'paint-stack:' + layer.tagName.toLowerCase() + id + ':' + via;
                        }
                    }
                } catch (e) { /* defensive */ }
                return '';
            };

            const resolveBg = (startEl) => {
                let depth = 0;
                let bgImage = false;
                let bgImageVia = '';
                let cur = startEl;
                while (cur && cur.nodeType === 1) {
                    if (!bgImage) {
                        const via = elementHasBgImage(cur);
                        if (via) {
                            bgImage = true;
                            bgImageVia = (cur === startEl ? 'self' : 'ancestor[' + depth + ']') + ':' + via;
                        }
                    }
                    const cs = window.getComputedStyle(cur);
                    const bg = cs.backgroundColor;
                    if (bg && bg !== 'rgba(0, 0, 0, 0)' && bg !== 'transparent') {
                        // Even with a solid bg-color, an overlay sibling may
                        // sit between this element and the text — check the
                        // paint stack before declaring the colour reliable.
                        if (!bgImage) {
                            const stackVia = checkPaintStack(startEl);
                            if (stackVia) {
                                bgImage = true;
                                bgImageVia = stackVia;
                            }
                        }
                        return { color: bg, depth: depth, bg_image_present: bgImage, bg_image_via: bgImageVia, reached_root: false };
                    }
                    cur = cur.parentElement;
                    depth += 1;
                }
                if (!bgImage) {
                    const stackVia = checkPaintStack(startEl);
                    if (stackVia) {
                        bgImage = true;
                        bgImageVia = stackVia;
                    }
                }
                return { color: 'rgb(255, 255, 255)', depth: depth, bg_image_present: bgImage, bg_image_via: bgImageVia, reached_root: true };
            };

            const walker = document.createTreeWalker(
                document.body || document.documentElement,
                NodeFilter.SHOW_TEXT,
                {
                    acceptNode: (n) => {
                        const t = (n.nodeValue || '').trim();
                        if (!t) return NodeFilter.FILTER_REJECT;
                        return NodeFilter.FILTER_ACCEPT;
                    }
                }
            );

            let node;
            while ((node = walker.nextNode())) {
                const text = (node.nodeValue || '').trim();
                if (!text) continue;
                const parent = node.parentElement;
                if (!parent) continue;
                if (hasHiddenAncestor(parent)) continue;

                // Skip script/style/noscript text
                const tag = parent.tagName.toLowerCase();
                if (tag === 'script' || tag === 'style' || tag === 'noscript' || tag === 'template') continue;

                const rect = parent.getBoundingClientRect();
                if (rect.width < 2 || rect.height < 2) continue;

                const cs = window.getComputedStyle(parent);
                const isSvgText = (parent.namespaceURI === 'http://www.w3.org/2000/svg') ||
                    (tag === 'text' && parent.closest && parent.closest('svg'));

                let fg;
                if (isSvgText) {
                    const fill = cs.fill || cs.color || 'rgb(0, 0, 0)';
                    fg = (fill === 'none' || !fill) ? cs.color : fill;
                } else {
                    fg = cs.color;
                }

                const bgInfo = resolveBg(parent);

                const fontSize = parseFloat(cs.fontSize) || 0;
                const fontWeight = cs.fontWeight || '400';

                out.push({
                    selector: buildSelector(parent),
                    tag: tag,
                    text: text,
                    fg_color_raw: fg,
                    bg_color_raw: bgInfo.color,
                    bg_walk_depth: bgInfo.depth,
                    bg_image_present: bgInfo.bg_image_present,
                    bg_image_via: bgInfo.bg_image_via || '',
                    bg_reached_root: bgInfo.reached_root,
                    font_size_px: fontSize,
                    font_weight: fontWeight,
                    is_svg_text: !!isSvgText,
                    rect: {
                        x: rect.x, y: rect.y,
                        width: rect.width, height: rect.height,
                    },
                });
            }

            return out;
        }""")

        from functions.contrast import (
            parse_rgb,
            contrast_ratio_rgb,
            is_large_text,
        )

        results: list[dict] = []
        for entry in raw or []:
            fg_raw = entry.get("fg_color_raw") or ""
            bg_raw = entry.get("bg_color_raw") or ""
            fg = parse_rgb(fg_raw, bg_raw)
            bg = parse_rgb(bg_raw)
            font_size = entry.get("font_size_px") or 0.0
            font_weight = entry.get("font_weight") or "400"
            large = is_large_text(font_size, font_weight)
            required = 3.0 if large else 4.5

            ratio: float | None = None
            if fg is not None and bg is not None:
                try:
                    ratio = round(contrast_ratio_rgb(fg, bg), 2)
                except Exception:
                    ratio = None

            passes: bool | None = None
            if ratio is not None:
                passes = ratio >= required

            results.append({
                "selector": entry.get("selector", ""),
                "tag": entry.get("tag", ""),
                "text": entry.get("text", ""),
                "fg_color_raw": fg_raw,
                "bg_color_raw": bg_raw,
                "fg_color": list(fg) if fg is not None else None,
                "bg_color": list(bg) if bg is not None else None,
                "bg_walk_depth": entry.get("bg_walk_depth", 0),
                "bg_image_present": bool(entry.get("bg_image_present", False)),
                "bg_image_via": entry.get("bg_image_via", ""),
                "bg_reached_root": bool(entry.get("bg_reached_root", False)),
                "font_size_px": font_size,
                "font_weight": font_weight,
                "is_large_text": large,
                "is_svg_text": bool(entry.get("is_svg_text", False)),
                "rect": entry.get("rect", {}),
                "ratio": ratio,
                "required_ratio": required,
                "passes": passes,
                "method": "andi_style_methodology",
            })

        capture_data.andi_contrast_results = results

        out_path = os.path.join(captures_dir, "andi_contrast.json")
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(results, f, indent=2)

        fail_count = sum(1 for r in results if r["passes"] is False)
        unmeasurable = sum(1 for r in results if r["passes"] is None)
        bg_image_count = sum(1 for r in results if r["bg_image_present"])
        logger.info(
            "ANDI contrast: %d text nodes scanned, %d failures, %d unmeasurable, "
            "%d behind background-image",
            len(results), fail_count, unmeasurable, bg_image_count,
        )
    except Exception:
        logger.exception("ANDI contrast extraction failed")


# BCP 47 — primary subtag is 2-3 ASCII letters; optional script (4 letters);
# optional region (2 letters or 3 digits); plus extended subtags 1-8 alnum.
# This is the same pattern used by ANDI's lang validator and aligns with
# the simpler _LANG_RE in checks/checks_3_1.py — keep them in sync.
_BCP47_RE_JS = (
    r"^[a-zA-Z]{2,3}(-[a-zA-Z]{4})?(-[a-zA-Z]{2}|-\\d{3})?(-[a-zA-Z0-9]{1,8})*$"
)


async def _capture_andi_lang(
    page: Page, captures_dir: str, capture_data: CaptureData,
) -> None:
    """ANDI-style language audit (sANDI methodology).

    Walks every element with an explicit ``lang`` attribute, validates
    each value against BCP 47, detects redundant declarations (segment
    lang same as the inherited ancestor lang), surfaces ``xml:lang``
    mismatches per segment, and notes whether the segment is hidden.
    Document-level issues (missing/empty/invalid html lang,
    html-vs-xml:lang mismatch) are rolled up in ``issues``.

    Distinct from the existing ``capture_data.page_language`` extraction
    in this file: that one feeds the legacy regex-fallback path in
    ``Check_3_1_1`` and ``Check_3_1_2``. The richer ANDI shape lets the
    judge / visual AI cross-check specific segment selectors.

    Saves to ``<captures_dir>/andi_lang.json`` and to
    ``capture_data.andi_lang_results``.
    """
    try:
        result = await page.evaluate(
            "(BCP47_RE_SRC) => {\n"
            "    const BCP47 = new RegExp(BCP47_RE_SRC);\n"
            "    const html = document.documentElement;\n"
            "    const htmlLang = (html.getAttribute('lang') || '').trim();\n"
            "    const htmlXmlLang = (html.getAttribute('xml:lang') || '').trim();\n"
            "    const issues = [];\n"
            "    if (!htmlLang) {\n"
            "        issues.push('html_lang_missing');\n"
            "    } else if (!BCP47.test(htmlLang)) {\n"
            "        issues.push('html_lang_invalid:' + htmlLang);\n"
            "    }\n"
            "    let htmlMatch = null;\n"
            "    if (htmlLang && htmlXmlLang) {\n"
            "        const a = htmlLang.toLowerCase().split('-')[0];\n"
            "        const b = htmlXmlLang.toLowerCase().split('-')[0];\n"
            "        htmlMatch = (a === b);\n"
            "        if (!htmlMatch) {\n"
            "            issues.push('html_lang_xml_lang_mismatch:' + htmlLang + ' vs ' + htmlXmlLang);\n"
            "        }\n"
            "    }\n"
            "    const isVisuallyHidden = (el) => {\n"
            "        if (!el || el.nodeType !== 1) return false;\n"
            "        const cs = window.getComputedStyle(el);\n"
            "        if (cs.display === 'none' || cs.visibility === 'hidden') return true;\n"
            "        if (parseFloat(cs.opacity) === 0) return true;\n"
            "        if (cs.position === 'absolute' || cs.position === 'fixed') {\n"
            "            if (cs.clip === 'rect(0px, 0px, 0px, 0px)' ||\n"
            "                cs.clip === 'rect(0, 0, 0, 0)' ||\n"
            "                (cs.clipPath && cs.clipPath !== 'none' && cs.clipPath !== 'auto' &&\n"
            "                 (cs.clipPath.includes('inset(100%)') || cs.clipPath.includes('inset(50%)')))) {\n"
            "                return true;\n"
            "            }\n"
            "        }\n"
            "        return false;\n"
            "    };\n"
            "    const hasHiddenAncestor = (el) => {\n"
            "        let p = el;\n"
            "        while (p) {\n"
            "            if (p.nodeType === 1 && isVisuallyHidden(p)) return true;\n"
            "            if (p.nodeType === 1 && p.getAttribute('aria-hidden') === 'true') return true;\n"
            "            if (p.nodeType === 1 && p.hasAttribute('hidden')) return true;\n"
            "            p = p.parentElement;\n"
            "        }\n"
            "        return false;\n"
            "    };\n"
            "    const buildSelector = (el) => {\n"
            "        if (!el || el.nodeType !== 1) return '';\n"
            "        if (el.id) return el.tagName.toLowerCase() + '#' + el.id;\n"
            "        let path = el.tagName.toLowerCase();\n"
            "        if (el.className && typeof el.className === 'string') {\n"
            "            const cls = el.className.trim().split(/\\s+/).filter(c => c).slice(0, 2);\n"
            "            if (cls.length) path += '.' + cls.join('.');\n"
            "        }\n"
            "        let parent = el.parentElement;\n"
            "        while (parent && parent.tagName.toLowerCase() !== 'body' && path.length < 200) {\n"
            "            let seg = parent.tagName.toLowerCase();\n"
            "            if (parent.id) { return seg + '#' + parent.id + ' > ' + path; }\n"
            "            if (parent.className && typeof parent.className === 'string') {\n"
            "                const cls = parent.className.trim().split(/\\s+/).filter(c => c).slice(0, 1);\n"
            "                if (cls.length) seg += '.' + cls.join('.');\n"
            "            }\n"
            "            path = seg + ' > ' + path;\n"
            "            parent = parent.parentElement;\n"
            "        }\n"
            "        return path;\n"
            "    };\n"
            "    const inheritedLang = (el) => {\n"
            "        let p = el.parentElement;\n"
            "        while (p) {\n"
            "            const v = (p.getAttribute && p.getAttribute('lang')) || '';\n"
            "            if (v) return v.trim();\n"
            "            p = p.parentElement;\n"
            "        }\n"
            "        return htmlLang;\n"
            "    };\n"
            "    const segments = [];\n"
            "    const langEls = document.querySelectorAll('[lang], [xml\\\\:lang]');\n"
            "    for (const el of langEls) {\n"
            "        if (el === html) continue;\n"
            "        const langAttr = (el.getAttribute('lang') || '').trim();\n"
            "        const xmlLangAttr = (el.getAttribute('xml:lang') || '').trim();\n"
            "        if (!langAttr && !xmlLangAttr) continue;\n"
            "        const inherited = inheritedLang(el);\n"
            "        const langValid = langAttr ? BCP47.test(langAttr) : null;\n"
            "        let xmlLangMatches = null;\n"
            "        if (langAttr && xmlLangAttr) {\n"
            "            xmlLangMatches = (langAttr.toLowerCase().split('-')[0] ===\n"
            "                              xmlLangAttr.toLowerCase().split('-')[0]);\n"
            "        }\n"
            "        const redundant = !!(langAttr && inherited &&\n"
            "                             langAttr.toLowerCase() === inherited.toLowerCase());\n"
            "        const text = (el.textContent || '').trim();\n"
            "        const rect = el.getBoundingClientRect();\n"
            "        segments.push({\n"
            "            selector: buildSelector(el),\n"
            "            tag: el.tagName.toLowerCase(),\n"
            "            lang: langAttr,\n"
            "            lang_valid: langValid,\n"
            "            xml_lang: xmlLangAttr,\n"
            "            xml_lang_matches_lang: xmlLangMatches,\n"
            "            inherited_lang: inherited,\n"
            "            redundant: redundant,\n"
            "            text: text,\n"
            "            text_length: text.length,\n"
            "            is_hidden: hasHiddenAncestor(el),\n"
            "            rect: { x: rect.x, y: rect.y, width: rect.width, height: rect.height },\n"
            "        });\n"
            "    }\n"
            "    return {\n"
            "        html_lang: htmlLang,\n"
            "        html_lang_valid: htmlLang ? BCP47.test(htmlLang) : false,\n"
            "        html_xml_lang: htmlXmlLang,\n"
            "        html_lang_xml_lang_match: htmlMatch,\n"
            "        issues: issues,\n"
            "        segments: segments,\n"
            "    };\n"
            "}",
            _BCP47_RE_JS,
        )

        capture_data.andi_lang_results = result or {}

        out_path = os.path.join(captures_dir, "andi_lang.json")
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(capture_data.andi_lang_results, f, indent=2)

        seg = capture_data.andi_lang_results.get("segments") or []
        invalid = sum(1 for s in seg if s.get("lang") and s.get("lang_valid") is False)
        redundant = sum(1 for s in seg if s.get("redundant"))
        xml_mismatch = sum(1 for s in seg if s.get("xml_lang_matches_lang") is False)
        hidden = sum(1 for s in seg if s.get("is_hidden"))
        logger.info(
            "ANDI lang: html=%r valid=%s | %d segments (invalid=%d, "
            "redundant=%d, xml_lang_mismatch=%d, hidden=%d) | doc issues=%d",
            capture_data.andi_lang_results.get("html_lang", ""),
            capture_data.andi_lang_results.get("html_lang_valid"),
            len(seg), invalid, redundant, xml_mismatch, hidden,
            len(capture_data.andi_lang_results.get("issues") or []),
        )
    except Exception:
        logger.exception("ANDI lang extraction failed")


async def _capture_andi_hidden(
    page: Page, captures_dir: str, capture_data: CaptureData,
) -> None:
    """ANDI-style hidden-content audit (hANDI methodology).

    Walks every potentially-focusable element and flags ones that are
    simultaneously hidden. Specifically:

    - ``aria-hidden="true"`` on the element or any ancestor (HIGH —
      ARIA spec violation: focusable descendants of aria-hidden are
      illegal).
    - ``display:none`` / ``visibility:hidden`` / ``hidden`` attribute
      on a tab-reachable element (MEDIUM — browser usually skips
      these, but JS that toggles visibility on focus can let them
      receive focus anyway).
    - ``opacity:0`` (LOW — invisible but accepts focus).
    - Positioned far off-screen (LOW — common skip-link pattern, not
      always a bug, but a focusable element at -10000px without a
      visible-on-focus rule is broken).

    Saves to ``<captures_dir>/andi_hidden.json`` and to
    ``capture_data.andi_hidden_results``.
    """
    try:
        results = await page.evaluate("""() => {
            const sel = 'a[href], button, input, select, textarea, ' +
                '[tabindex], details, summary, audio[controls], ' +
                'video[controls], iframe, object, embed, [contenteditable=""], ' +
                '[contenteditable="true"]';

            const naturalSet = new Set([
                'a','button','input','select','textarea','details',
                'summary','iframe','object','embed','audio','video',
            ]);

            const buildSelector = (el) => {
                if (!el || el.nodeType !== 1) return '';
                if (el.id) return el.tagName.toLowerCase() + '#' + el.id;
                let path = el.tagName.toLowerCase();
                if (el.className && typeof el.className === 'string') {
                    const cls = el.className.trim().split(/\\s+/).filter(c => c).slice(0, 2);
                    if (cls.length) path += '.' + cls.join('.');
                }
                let parent = el.parentElement;
                while (parent && parent.tagName.toLowerCase() !== 'body' && path.length < 200) {
                    let seg = parent.tagName.toLowerCase();
                    if (parent.id) { return seg + '#' + parent.id + ' > ' + path; }
                    if (parent.className && typeof parent.className === 'string') {
                        const cls = parent.className.trim().split(/\\s+/).filter(c => c).slice(0, 1);
                        if (cls.length) seg += '.' + cls.join('.');
                    }
                    path = seg + ' > ' + path;
                    parent = parent.parentElement;
                }
                return path;
            };

            // Lightweight accessible-name calc: aria-labelledby > aria-label
            // > native label (for inputs) > value/text > title.
            const accName = (el) => {
                const ll = el.getAttribute('aria-labelledby');
                if (ll) {
                    const ids = ll.split(/\\s+/).filter(Boolean);
                    const parts = [];
                    for (const id of ids) {
                        const r = document.getElementById(id);
                        if (r) parts.push((r.textContent || '').trim());
                    }
                    if (parts.length) return parts.join(' ').trim();
                }
                const al = el.getAttribute('aria-label');
                if (al) return al.trim();
                const tag = el.tagName.toLowerCase();
                if (tag === 'input' || tag === 'select' || tag === 'textarea') {
                    if (el.id) {
                        const lbl = document.querySelector('label[for="' + el.id.replace(/"/g, '\\\\"') + '"]');
                        if (lbl) return (lbl.textContent || '').trim();
                    }
                    const wrapLbl = el.closest('label');
                    if (wrapLbl) return (wrapLbl.textContent || '').trim();
                    if (el.getAttribute('placeholder')) return el.getAttribute('placeholder').trim();
                    if (el.value) return String(el.value).trim();
                }
                const t = (el.textContent || '').trim();
                if (t) return t;
                if (el.getAttribute('title')) return el.getAttribute('title').trim();
                if (el.getAttribute('alt')) return el.getAttribute('alt').trim();
                return '';
            };

            const results = [];
            const seen = new WeakSet();
            for (const el of document.querySelectorAll(sel)) {
                if (seen.has(el)) continue;
                seen.add(el);

                const tag = el.tagName.toLowerCase();
                if (tag === 'a' && !el.hasAttribute('href') && !el.hasAttribute('tabindex')) continue;
                if (el.disabled) continue;

                const tiAttr = el.getAttribute('tabindex');
                const tiNum = tiAttr === null ? null : parseInt(tiAttr);
                const tiValid = tiNum !== null && !isNaN(tiNum);
                const naturallyFocusable = naturalSet.has(tag);
                if (!naturallyFocusable && !tiValid) continue;

                let tabReachable = tiValid ? tiNum >= 0 : naturallyFocusable;

                const cs = window.getComputedStyle(el);
                const reasons = [];
                let ariaHiddenPath = '';
                let ariaHiddenSelector = '';

                // The HTML `inert` attribute removes the element and its
                // entire subtree from the focus order and accessibility
                // tree (HTML spec — Inert Subtrees). Closest() walks
                // ancestors; presence is what matters per the boolean-
                // attribute spec, so we match `[inert]` regardless of
                // value. Modern modals (HTML <dialog>, React-Aria,
                // Lit-based UIs) rely on this — without static detection
                // here the andi_hidden walker would still flag focusable
                // descendants of an inert subtree as tab-reachable.
                const inertEl = el.closest('[inert]');
                if (inertEl) {
                    reasons.push('inert (' + (inertEl === el ? 'self' : 'ancestor') + ')');
                    tabReachable = false;
                }

                if (cs.display === 'none') reasons.push('display:none');
                if (cs.visibility === 'hidden') reasons.push('visibility:hidden');
                if (parseFloat(cs.opacity) === 0) reasons.push('opacity:0');
                if (el.hasAttribute('hidden')) reasons.push('hidden attribute');

                let p = el;
                while (p) {
                    if (p.nodeType === 1 && p.getAttribute && p.getAttribute('aria-hidden') === 'true') {
                        ariaHiddenPath = (p === el) ? 'self' : 'ancestor';
                        ariaHiddenSelector = buildSelector(p);
                        reasons.push('aria-hidden=true (' + ariaHiddenPath + ')');
                        break;
                    }
                    p = p.parentElement;
                }

                if (cs.position === 'absolute' || cs.position === 'fixed') {
                    const r = el.getBoundingClientRect();
                    if (r.left < -1000 || r.top < -1000) reasons.push('positioned off-screen');
                    if (r.right < 0 || r.bottom < 0) reasons.push('positioned beyond viewport');
                }

                if (reasons.length === 0) continue;

                const r = el.getBoundingClientRect();
                results.push({
                    selector: buildSelector(el),
                    tag: tag,
                    role: el.getAttribute('role') || '',
                    accessible_name: accName(el),
                    tabindex: tiAttr,
                    naturally_focusable: naturallyFocusable,
                    tab_reachable: tabReachable,
                    hidden_reasons: reasons,
                    aria_hidden_path: ariaHiddenPath,
                    aria_hidden_ancestor_selector: ariaHiddenSelector,
                    rect: { x: r.x, y: r.y, width: r.width, height: r.height },
                    text: (el.textContent || '').trim(),
                });
            }
            return results;
        }""")

        capture_data.andi_hidden_results = results or []

        out_path = os.path.join(captures_dir, "andi_hidden.json")
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(capture_data.andi_hidden_results, f, indent=2)

        aria_hidden = sum(
            1 for r in capture_data.andi_hidden_results
            if any("aria-hidden=true" in reason for reason in (r.get("hidden_reasons") or []))
        )
        tab_reachable = sum(1 for r in capture_data.andi_hidden_results if r.get("tab_reachable"))
        logger.info(
            "ANDI hidden: %d focusable-but-hidden elements (%d aria-hidden, "
            "%d tab-reachable, %d programmatic-only)",
            len(capture_data.andi_hidden_results), aria_hidden, tab_reachable,
            len(capture_data.andi_hidden_results) - tab_reachable,
        )
    except Exception:
        logger.exception("ANDI hidden extraction failed")


async def _capture_andi_graphics(
    page: Page, captures_dir: str, capture_data: CaptureData,
) -> None:
    """ANDI-style graphics audit (gANDI methodology).

    Walks every <img>, <svg>, <input type="image">, <area>, and
    background-image-bearing element with visible text overlay.
    Reports per-image accessibility state: alt presence/emptiness,
    aria-label, role, SVG <title>/<desc>, and crucially the
    *context*: is the image the sole content of a link or button?
    Does the link/button have another accessible name source?

    These context flags drive SC 1.1.1 severity: an empty alt on
    an image inside a link that has no other text is a HIGH-severity
    bug because the LINK has no accessible name; the same empty alt
    on an image inside a link with surrounding text is correct
    (decorative).

    Background-image elements with visible text overlay flag for
    SC 1.4.5 (Images of Text) — manual review since we can't
    deterministically tell if the text is part of the image or
    rendered as HTML on top of it.

    Saves to ``<captures_dir>/andi_graphics.json`` and to
    ``capture_data.andi_graphics_results``.
    """
    try:
        results = await page.evaluate("""() => {
            const buildSelector = (el) => {
                if (!el || el.nodeType !== 1) return '';
                if (el.id) return el.tagName.toLowerCase() + '#' + el.id;
                let path = el.tagName.toLowerCase();
                if (el.className && typeof el.className === 'string') {
                    const cls = el.className.trim().split(/\\s+/).filter(c => c).slice(0, 2);
                    if (cls.length) path += '.' + cls.join('.');
                }
                let parent = el.parentElement;
                while (parent && parent.tagName.toLowerCase() !== 'body' && path.length < 200) {
                    let seg = parent.tagName.toLowerCase();
                    if (parent.id) { return seg + '#' + parent.id + ' > ' + path; }
                    if (parent.className && typeof parent.className === 'string') {
                        const cls = parent.className.trim().split(/\\s+/).filter(c => c).slice(0, 1);
                        if (cls.length) seg += '.' + cls.join('.');
                    }
                    path = seg + ' > ' + path;
                    parent = parent.parentElement;
                }
                return path;
            };

            const resolveLabelledby = (el) => {
                const ll = el.getAttribute('aria-labelledby');
                if (!ll) return '';
                const ids = ll.split(/\\s+/).filter(Boolean);
                const parts = [];
                for (const id of ids) {
                    const r = document.getElementById(id);
                    if (r) parts.push((r.textContent || '').trim());
                }
                return parts.join(' ').trim();
            };

            const linkOrButtonAncestor = (el) => {
                let p = el.parentElement;
                while (p) {
                    const t = p.tagName.toLowerCase();
                    if ((t === 'a' && p.hasAttribute('href')) || t === 'button' ||
                        p.getAttribute('role') === 'link' ||
                        p.getAttribute('role') === 'button') {
                        return p;
                    }
                    p = p.parentElement;
                }
                return null;
            };

            // Other-text presence: walk the link/button subtree and
            // collect text from non-image descendants. If non-empty,
            // the link/button has a non-image source of accessible
            // name and a decorative alt="" on an image inside it is
            // correct.
            const ancestorHasOtherText = (anc, currentImg) => {
                if (!anc) return false;
                const own = (anc.getAttribute('aria-label') || '').trim();
                if (own) return true;
                const lb = resolveLabelledby(anc);
                if (lb) return true;
                const t = anc.getAttribute('title');
                if (t && t.trim()) return true;
                // Walk descendants, skipping the image subtree itself
                const walk = document.createTreeWalker(anc, NodeFilter.SHOW_TEXT);
                let n;
                while ((n = walk.nextNode())) {
                    const text = (n.nodeValue || '').trim();
                    if (!text) continue;
                    let p = n.parentElement;
                    let inImg = false;
                    while (p && p !== anc) {
                        if (p === currentImg) { inImg = true; break; }
                        const tn = p.tagName.toLowerCase();
                        if (tn === 'img' || tn === 'svg' || tn === 'picture') { inImg = true; break; }
                        p = p.parentElement;
                    }
                    if (!inImg) return true;
                }
                return false;
            };

            const ancestorHasName = (anc) => {
                if (!anc) return false;
                if ((anc.getAttribute('aria-label') || '').trim()) return true;
                if (resolveLabelledby(anc)) return true;
                if ((anc.getAttribute('title') || '').trim()) return true;
                const text = (anc.textContent || '').trim();
                return !!text;
            };

            const isVisible = (el) => {
                const cs = window.getComputedStyle(el);
                if (cs.display === 'none' || cs.visibility === 'hidden') return false;
                if (parseFloat(cs.opacity) === 0) return false;
                const r = el.getBoundingClientRect();
                if (r.width < 2 || r.height < 2) return false;
                return true;
            };

            const out = [];

            // ── <img> ────────────────────────────────────────────
            for (const el of document.querySelectorAll('img')) {
                if (!isVisible(el)) continue;
                const alt = el.getAttribute('alt');
                const role = el.getAttribute('role') || '';
                const ariaLabel = el.getAttribute('aria-label') || '';
                const lbResolved = resolveLabelledby(el);
                const ariaHidden = el.getAttribute('aria-hidden') === 'true';
                const anc = linkOrButtonAncestor(el);
                const otherText = ancestorHasOtherText(anc, el);
                const ancHasName = ancestorHasName(anc);
                const r = el.getBoundingClientRect();

                let nameSource = 'none';
                let accName = '';
                if (ariaLabel) { nameSource = 'aria-label'; accName = ariaLabel; }
                else if (lbResolved) { nameSource = 'aria-labelledby'; accName = lbResolved; }
                else if (alt && alt.trim()) { nameSource = 'alt'; accName = alt.trim(); }
                else if ((el.getAttribute('title') || '').trim()) {
                    nameSource = 'title'; accName = el.getAttribute('title').trim();
                }

                const decorative = (alt === '' || role === 'presentation' || role === 'none' ||
                                    ariaHidden);

                out.push({
                    selector: buildSelector(el), type: 'img',
                    src: el.getAttribute('src') || '',
                    alt: alt, alt_present: alt !== null, alt_empty: alt === '',
                    aria_label: ariaLabel,
                    aria_labelledby_resolved: lbResolved,
                    aria_hidden: ariaHidden,
                    role: role, decorative: decorative,
                    in_link_or_button: !!anc,
                    ancestor_tag: anc ? anc.tagName.toLowerCase() : '',
                    ancestor_has_other_text: otherText,
                    ancestor_link_or_button_has_name: ancHasName,
                    accessible_name: accName, name_source: nameSource,
                    svg_title: '', svg_desc: '', svg_role: '',
                    rect: { x: r.x, y: r.y, width: r.width, height: r.height },
                    has_text_overlay: false,
                    text_overlay_text: '',
                });
            }

            // ── <svg> ────────────────────────────────────────────
            for (const el of document.querySelectorAll('svg')) {
                if (!isVisible(el)) continue;
                const role = el.getAttribute('role') || '';
                const ariaLabel = el.getAttribute('aria-label') || '';
                const lbResolved = resolveLabelledby(el);
                const ariaHidden = el.getAttribute('aria-hidden') === 'true';
                const titleEl = el.querySelector(':scope > title');
                const descEl = el.querySelector(':scope > desc');
                const svgTitle = titleEl ? (titleEl.textContent || '').trim() : '';
                const svgDesc = descEl ? (descEl.textContent || '').trim() : '';
                const anc = linkOrButtonAncestor(el);
                const otherText = ancestorHasOtherText(anc, el);
                const ancHasName = ancestorHasName(anc);
                const r = el.getBoundingClientRect();

                let nameSource = 'none';
                let accName = '';
                if (ariaLabel) { nameSource = 'aria-label'; accName = ariaLabel; }
                else if (lbResolved) { nameSource = 'aria-labelledby'; accName = lbResolved; }
                else if (svgTitle) { nameSource = 'svg-title'; accName = svgTitle; }

                const decorative = (role === 'presentation' || role === 'none' || ariaHidden);

                out.push({
                    selector: buildSelector(el), type: 'svg',
                    src: '', alt: null, alt_present: false, alt_empty: false,
                    aria_label: ariaLabel,
                    aria_labelledby_resolved: lbResolved,
                    aria_hidden: ariaHidden,
                    role: role, decorative: decorative,
                    in_link_or_button: !!anc,
                    ancestor_tag: anc ? anc.tagName.toLowerCase() : '',
                    ancestor_has_other_text: otherText,
                    ancestor_link_or_button_has_name: ancHasName,
                    accessible_name: accName, name_source: nameSource,
                    svg_title: svgTitle, svg_desc: svgDesc, svg_role: role,
                    rect: { x: r.x, y: r.y, width: r.width, height: r.height },
                    has_text_overlay: false,
                    text_overlay_text: '',
                });
            }

            // ── <input type="image"> ─────────────────────────────
            for (const el of document.querySelectorAll('input[type="image"]')) {
                if (!isVisible(el)) continue;
                const alt = el.getAttribute('alt');
                const role = el.getAttribute('role') || '';
                const ariaLabel = el.getAttribute('aria-label') || '';
                const lbResolved = resolveLabelledby(el);
                const ariaHidden = el.getAttribute('aria-hidden') === 'true';
                const r = el.getBoundingClientRect();

                let nameSource = 'none';
                let accName = '';
                if (ariaLabel) { nameSource = 'aria-label'; accName = ariaLabel; }
                else if (lbResolved) { nameSource = 'aria-labelledby'; accName = lbResolved; }
                else if (alt && alt.trim()) { nameSource = 'alt'; accName = alt.trim(); }
                else if ((el.value || '').trim()) { nameSource = 'value'; accName = el.value.trim(); }
                else if ((el.getAttribute('title') || '').trim()) {
                    nameSource = 'title'; accName = el.getAttribute('title').trim();
                }

                out.push({
                    selector: buildSelector(el), type: 'input-image',
                    src: el.getAttribute('src') || '',
                    alt: alt, alt_present: alt !== null, alt_empty: alt === '',
                    aria_label: ariaLabel,
                    aria_labelledby_resolved: lbResolved,
                    aria_hidden: ariaHidden,
                    role: role, decorative: false,
                    in_link_or_button: false, ancestor_tag: '',
                    ancestor_has_other_text: false,
                    ancestor_link_or_button_has_name: false,
                    accessible_name: accName, name_source: nameSource,
                    svg_title: '', svg_desc: '', svg_role: '',
                    rect: { x: r.x, y: r.y, width: r.width, height: r.height },
                    has_text_overlay: false,
                    text_overlay_text: '',
                });
            }

            // ── <area> in image map ──────────────────────────────
            for (const el of document.querySelectorAll('area')) {
                const alt = el.getAttribute('alt');
                const ariaLabel = el.getAttribute('aria-label') || '';
                const lbResolved = resolveLabelledby(el);
                let nameSource = 'none';
                let accName = '';
                if (ariaLabel) { nameSource = 'aria-label'; accName = ariaLabel; }
                else if (lbResolved) { nameSource = 'aria-labelledby'; accName = lbResolved; }
                else if (alt && alt.trim()) { nameSource = 'alt'; accName = alt.trim(); }

                out.push({
                    selector: buildSelector(el), type: 'area',
                    src: el.getAttribute('href') || '',
                    alt: alt, alt_present: alt !== null, alt_empty: alt === '',
                    aria_label: ariaLabel,
                    aria_labelledby_resolved: lbResolved,
                    aria_hidden: el.getAttribute('aria-hidden') === 'true',
                    role: el.getAttribute('role') || '', decorative: false,
                    in_link_or_button: true, ancestor_tag: 'area',
                    ancestor_has_other_text: false,
                    ancestor_link_or_button_has_name: false,
                    accessible_name: accName, name_source: nameSource,
                    svg_title: '', svg_desc: '', svg_role: '',
                    rect: { x: 0, y: 0, width: 0, height: 0 },
                    has_text_overlay: false,
                    text_overlay_text: '',
                });
            }

            // ── background-image with text overlay (SC 1.4.5) ────
            // Find every element whose computed bg-image is non-none
            // AND that has a non-trivial visible text descendant.
            // Skip purely-decorative bg gradients (gradient() but no
            // url()).
            for (const el of document.querySelectorAll('body, body *')) {
                if (!isVisible(el)) continue;
                const cs = window.getComputedStyle(el);
                const bgImg = cs.backgroundImage;
                if (!bgImg || bgImg === 'none') continue;
                if (!/url\\(/.test(bgImg)) continue;  // gradient-only, skip

                const text = (el.textContent || '').trim();
                if (text.length < 4) continue;

                // Skip when the bg image is on an element whose text
                // is also explicitly on a child with the same content
                // (avoid double-counting <body> with all its text).
                if (el === document.body) continue;
                if (el === document.documentElement) continue;

                // Skip if any descendant element ALSO has its own bg
                // url() and visible text — let the inner-most layer
                // take responsibility for the overlay.
                let innerHasBg = false;
                for (const ch of el.querySelectorAll('*')) {
                    const ccs = window.getComputedStyle(ch);
                    if (ccs.backgroundImage && /url\\(/.test(ccs.backgroundImage)) {
                        const ct = (ch.textContent || '').trim();
                        if (ct.length >= 4) { innerHasBg = true; break; }
                    }
                }
                if (innerHasBg) continue;

                const r = el.getBoundingClientRect();
                if (r.width < 30 || r.height < 30) continue;

                // Extract the first url() target as src
                const srcMatch = bgImg.match(/url\\(['"]?([^'"\\)]+)['"]?\\)/);
                const src = srcMatch ? srcMatch[1] : '';

                out.push({
                    selector: buildSelector(el), type: 'bg-image',
                    src: src, alt: null, alt_present: false, alt_empty: false,
                    aria_label: el.getAttribute('aria-label') || '',
                    aria_labelledby_resolved: resolveLabelledby(el),
                    aria_hidden: el.getAttribute('aria-hidden') === 'true',
                    role: el.getAttribute('role') || '', decorative: false,
                    in_link_or_button: !!linkOrButtonAncestor(el),
                    ancestor_tag: '',
                    ancestor_has_other_text: false,
                    ancestor_link_or_button_has_name: false,
                    accessible_name: '', name_source: 'bg-image-no-alt',
                    svg_title: '', svg_desc: '', svg_role: '',
                    rect: { x: r.x, y: r.y, width: r.width, height: r.height },
                    has_text_overlay: true,
                    text_overlay_text: text,
                });
            }

            return out;
        }""")

        capture_data.andi_graphics_results = results or []

        out_path = os.path.join(captures_dir, "andi_graphics.json")
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(capture_data.andi_graphics_results, f, indent=2)

        by_type: dict[str, int] = {}
        no_name = 0
        bg_overlay = 0
        for g in capture_data.andi_graphics_results:
            by_type[g.get("type", "?")] = by_type.get(g.get("type", "?"), 0) + 1
            if g.get("name_source", "none") == "none" and not g.get("decorative"):
                no_name += 1
            if g.get("has_text_overlay"):
                bg_overlay += 1
        logger.info(
            "ANDI graphics: %d total (%s) | %d non-decorative without "
            "accessible name | %d background-image text overlays",
            len(capture_data.andi_graphics_results),
            ", ".join(f"{k}={v}" for k, v in by_type.items()),
            no_name, bg_overlay,
        )
    except Exception:
        logger.exception("ANDI graphics extraction failed")


async def _capture_andi_tables(
    page: Page, captures_dir: str, capture_data: CaptureData,
) -> None:
    """ANDI-style tables audit (tANDI methodology).

    Walks every ``<table>`` and classifies it as data vs layout via
    structural heuristics:

    - Has ``<th>``, ``<thead>``, ``<caption>``, ``role="grid"``,
      ``role="treegrid"``, or ``role="table"`` → DATA.
    - Has ``role="presentation"`` / ``role="none"`` → LAYOUT.
    - 1 row OR 1 column with no semantic markers → LAYOUT.
    - Otherwise → AMBIGUOUS (treated as data for SC 1.3.1 strictness
      — better to over-flag than miss broken data tables).

    Per-table reports caption/summary presence, ``<th>`` scope
    coverage, ``cell[headers]`` referential integrity (every id in
    headers attr resolves to an existing element on the page),
    nested-table flag, and a list of issues.

    Saves to ``<captures_dir>/andi_tables.json`` and to
    ``capture_data.andi_tables_results``.
    """
    try:
        results = await page.evaluate("""() => {
            const buildSelector = (el) => {
                if (!el || el.nodeType !== 1) return '';
                if (el.id) return el.tagName.toLowerCase() + '#' + el.id;
                let path = el.tagName.toLowerCase();
                if (el.className && typeof el.className === 'string') {
                    const cls = el.className.trim().split(/\\s+/).filter(c => c).slice(0, 2);
                    if (cls.length) path += '.' + cls.join('.');
                }
                let parent = el.parentElement;
                while (parent && parent.tagName.toLowerCase() !== 'body' && path.length < 200) {
                    let seg = parent.tagName.toLowerCase();
                    if (parent.id) { return seg + '#' + parent.id + ' > ' + path; }
                    if (parent.className && typeof parent.className === 'string') {
                        const cls = parent.className.trim().split(/\\s+/).filter(c => c).slice(0, 1);
                        if (cls.length) seg += '.' + cls.join('.');
                    }
                    path = seg + ' > ' + path;
                    parent = parent.parentElement;
                }
                return path;
            };

            const out = [];

            for (const table of document.querySelectorAll('table')) {
                const role = table.getAttribute('role') || '';
                const captionEl = table.querySelector(':scope > caption');
                const captionText = captionEl ? (captionEl.textContent || '').trim() : '';
                const summary = (table.getAttribute('summary') || '').trim();
                const theadEl = table.querySelector(':scope > thead');
                const ths = Array.from(table.querySelectorAll('th'));
                const rows = table.rows ? table.rows.length : 0;
                const cols = (table.rows && table.rows[0]) ? table.rows[0].cells.length : 0;
                const cells = Array.from(table.querySelectorAll('td, th'));

                // Nested = this table has another <table> descendant
                const nested = !!table.querySelector('table');

                // <th> scope coverage
                const thMissingScope = [];
                let thWithScope = 0;
                for (const th of ths) {
                    const scope = (th.getAttribute('scope') || '').trim().toLowerCase();
                    if (scope === 'col' || scope === 'row' ||
                        scope === 'colgroup' || scope === 'rowgroup') {
                        thWithScope += 1;
                    } else {
                        thMissingScope.push(buildSelector(th));
                    }
                }

                // headers/id referential integrity
                let cellsWithHeaders = 0;
                let headersAllValid = true;
                const brokenHeadersRefs = [];
                for (const c of cells) {
                    const h = (c.getAttribute('headers') || '').trim();
                    if (!h) continue;
                    cellsWithHeaders += 1;
                    const ids = h.split(/\\s+/).filter(Boolean);
                    for (const id of ids) {
                        const ref = document.getElementById(id);
                        if (!ref) {
                            headersAllValid = false;
                            brokenHeadersRefs.push(id);
                        }
                    }
                }

                // Classification
                let classification = 'ambiguous';
                if (role === 'presentation' || role === 'none') {
                    classification = 'layout';
                } else if (ths.length > 0 || theadEl ||
                           role === 'grid' || role === 'treegrid' ||
                           role === 'table') {
                    classification = 'data';
                } else if (rows <= 1 || cols <= 1) {
                    classification = 'layout';
                } else if (captionText) {
                    classification = 'data';
                }

                // Issues
                const issues = [];
                if (classification === 'data') {
                    if (!captionText) issues.push('data_table_no_caption');
                    if (ths.length === 0) issues.push('data_table_no_th');
                    else if (thWithScope < ths.length && cellsWithHeaders === 0) {
                        issues.push('th_missing_scope_and_no_headers_attr');
                    }
                    if (!headersAllValid) issues.push('headers_attr_broken_refs');
                }
                if (classification === 'layout' && !role) {
                    issues.push('layout_table_no_presentation_role');
                }
                if (nested) issues.push('nested_table');
                if (rows === 0) issues.push('empty_table');
                if (summary) issues.push('uses_deprecated_summary_attribute');

                out.push({
                    selector: buildSelector(table),
                    classification: classification,
                    role: role,
                    has_caption: !!captionText,
                    caption_text: captionText,
                    has_summary: !!summary,
                    summary_text: summary,
                    has_thead: !!theadEl,
                    th_count: ths.length,
                    th_with_scope_count: thWithScope,
                    th_missing_scope_selectors: thMissingScope,
                    cells_with_headers_attr: cellsWithHeaders,
                    headers_id_pairs_valid: headersAllValid,
                    broken_headers_refs: brokenHeadersRefs,
                    row_count: rows,
                    col_count: cols,
                    nested: nested,
                    issues: issues,
                });
            }

            return out;
        }""")

        capture_data.andi_tables_results = results or []

        out_path = os.path.join(captures_dir, "andi_tables.json")
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(capture_data.andi_tables_results, f, indent=2)

        n_data = sum(1 for t in capture_data.andi_tables_results if t.get("classification") == "data")
        n_layout = sum(1 for t in capture_data.andi_tables_results if t.get("classification") == "layout")
        n_amb = sum(1 for t in capture_data.andi_tables_results if t.get("classification") == "ambiguous")
        n_issues = sum(len(t.get("issues") or []) for t in capture_data.andi_tables_results)
        logger.info(
            "ANDI tables: %d total (data=%d, layout=%d, ambiguous=%d) — %d total issues",
            len(capture_data.andi_tables_results), n_data, n_layout, n_amb, n_issues,
        )
    except Exception:
        logger.exception("ANDI tables extraction failed")


# Ambiguous link/button text per ANDI lANDI: words that fail SC 2.4.4
# Link Purpose if used without surrounding context. Conservative list —
# matches the literal token only (no substring match), so "Click here for
# more" is flagged but "More about Foo" is not.
_AMBIGUOUS_INTERACTIVE_TEXT = {
    "click here", "click", "here", "more", "read more", "learn more",
    "details", "more details", "more info", "info", "this", "this page",
    "this link", "link", "go", "open", "view", "read", "see more",
    "continue", "next", "previous", "back", "submit",
}


async def _capture_andi_interactive(
    page: Page, captures_dir: str, capture_data: CaptureData,
) -> None:
    """ANDI-style links/buttons audit (lANDI methodology).

    Walks every visible ``<a href>``, ``<button>``,
    ``[role="link"]``, and ``[role="button"]`` and computes:

    - **Visible text**: ``textContent`` minus visually-hidden subtree
      content (sr-only labels are NOT visible to sighted users so
      they don't count toward the visible name).
    - **Accessible name**: aria-labelledby > aria-label > visible text
      > title — matching the WCAG accessible-name calculation order.
    - **name_includes_visible**: SC 2.5.3 Label in Name requires the
      accessible name to *contain* the visible text (case-insensitive,
      substring match). When false, voice-input users speaking the
      visible label cannot activate the control.
    - **is_ambiguous**: visible text is one of the standard ambiguous
      tokens (``"click here"``, ``"more"``, ``"read more"``, ...) —
      SC 2.4.4 Link Purpose flags these unless surrounding context
      makes the purpose clear (the AI judge handles the context check).
    - **has_no_name**: accessible name is empty — SC 4.1.2 / 2.4.4
      definite failure.
    - **image_only**: control's only descendant is an image; the
      accessible name comes solely from alt/svg-title.

    Saves to ``<captures_dir>/andi_interactive.json`` and to
    ``capture_data.andi_interactive_results``.
    """
    try:
        results = await page.evaluate("""(ambiguousList) => {
            const ambiguousSet = new Set(ambiguousList);

            const buildSelector = (el) => {
                if (!el || el.nodeType !== 1) return '';
                if (el.id) return el.tagName.toLowerCase() + '#' + el.id;
                let path = el.tagName.toLowerCase();
                if (el.className && typeof el.className === 'string') {
                    const cls = el.className.trim().split(/\\s+/).filter(c => c).slice(0, 2);
                    if (cls.length) path += '.' + cls.join('.');
                }
                let parent = el.parentElement;
                while (parent && parent.tagName.toLowerCase() !== 'body' && path.length < 200) {
                    let seg = parent.tagName.toLowerCase();
                    if (parent.id) { return seg + '#' + parent.id + ' > ' + path; }
                    if (parent.className && typeof parent.className === 'string') {
                        const cls = parent.className.trim().split(/\\s+/).filter(c => c).slice(0, 1);
                        if (cls.length) seg += '.' + cls.join('.');
                    }
                    path = seg + ' > ' + path;
                    parent = parent.parentElement;
                }
                return path;
            };

            const isVisuallyHidden = (el) => {
                if (!el || el.nodeType !== 1) return false;
                const cs = window.getComputedStyle(el);
                if (cs.display === 'none' || cs.visibility === 'hidden') return true;
                if (parseFloat(cs.opacity) === 0) return true;
                if (cs.position === 'absolute' || cs.position === 'fixed') {
                    if (cs.clip === 'rect(0px, 0px, 0px, 0px)' ||
                        cs.clip === 'rect(0, 0, 0, 0)' ||
                        (cs.clipPath && (cs.clipPath.includes('inset(100%)') || cs.clipPath.includes('inset(50%)')))) {
                        return true;
                    }
                    const r = el.getBoundingClientRect();
                    if (r.width <= 1 && r.height <= 1) return true;
                    if (r.left < -1000 || r.top < -1000) return true;
                }
                return false;
            };

            // Visible text only — collects text content while skipping
            // visually-hidden subtrees and image alt text. Image alt is
            // part of the *accessible* name but NOT part of the *visible*
            // text — SC 2.5.3 distinguishes between them.
            const visibleText = (el) => {
                if (!el) return '';
                if (isVisuallyHidden(el)) return '';
                let out = '';
                for (const node of el.childNodes) {
                    if (node.nodeType === 3) {
                        out += node.textContent || '';
                    } else if (node.nodeType === 1) {
                        const tn = node.tagName.toLowerCase();
                        if (tn === 'img' || tn === 'svg' || tn === 'picture') continue;
                        out += visibleText(node);
                    }
                }
                return out;
            };

            // Accessible name calc (simplified, matching lANDI):
            // aria-labelledby > aria-label > visible text + alt-text
            // contributions > title.
            const resolveLabelledby = (el) => {
                const ll = el.getAttribute('aria-labelledby');
                if (!ll) return '';
                const ids = ll.split(/\\s+/).filter(Boolean);
                const parts = [];
                for (const id of ids) {
                    const r = document.getElementById(id);
                    if (r) parts.push((r.textContent || '').trim());
                }
                return parts.join(' ').trim();
            };

            const elementName = (el) => {
                let out = '';
                if (isVisuallyHidden(el)) return '';
                for (const node of el.childNodes) {
                    if (node.nodeType === 3) {
                        out += node.textContent || '';
                    } else if (node.nodeType === 1) {
                        const tn = node.tagName.toLowerCase();
                        if (tn === 'img') {
                            const a = node.getAttribute('alt');
                            if (a) out += ' ' + a;
                        } else if (tn === 'svg') {
                            const t = node.querySelector(':scope > title');
                            if (t) out += ' ' + (t.textContent || '');
                            const al = node.getAttribute('aria-label');
                            if (al) out += ' ' + al;
                        } else {
                            out += elementName(node);
                        }
                    }
                }
                return out.replace(/\\s+/g, ' ').trim();
            };

            const accessibleName = (el) => {
                const lb = resolveLabelledby(el);
                if (lb) return { source: 'aria-labelledby', name: lb };
                const al = el.getAttribute('aria-label');
                if (al && al.trim()) return { source: 'aria-label', name: al.trim() };
                const en = elementName(el);
                if (en) return { source: 'content', name: en };
                const tt = el.getAttribute('title');
                if (tt && tt.trim()) return { source: 'title', name: tt.trim() };
                return { source: 'none', name: '' };
            };

            const isImageOnly = (el) => {
                let foundImg = false;
                let foundOther = false;
                for (const node of el.childNodes) {
                    if (node.nodeType === 3 && (node.textContent || '').trim()) {
                        foundOther = true;
                    } else if (node.nodeType === 1) {
                        const tn = node.tagName.toLowerCase();
                        if (tn === 'img' || tn === 'svg' || tn === 'picture') {
                            foundImg = true;
                        } else {
                            const sub = visibleText(node).trim();
                            if (sub) foundOther = true;
                            if (node.querySelector('img, svg, picture')) foundImg = true;
                        }
                    }
                }
                return foundImg && !foundOther;
            };

            const out = [];
            const sel = 'a[href], button, [role="link"], [role="button"]';
            const seen = new WeakSet();

            for (const el of document.querySelectorAll(sel)) {
                if (seen.has(el)) continue;
                seen.add(el);
                if (isVisuallyHidden(el)) continue;

                const tag = el.tagName.toLowerCase();
                const role = el.getAttribute('role') || '';
                const type = (tag === 'a' || role === 'link') ? 'link' :
                             (tag === 'button' || role === 'button') ? 'button' : 'unknown';

                const visible = visibleText(el).replace(/\\s+/g, ' ').trim();
                const accInfo = accessibleName(el);
                const accName = accInfo.name;

                const visLower = visible.toLowerCase();
                const nameLower = accName.toLowerCase();
                const nameIncludesVisible = visLower
                    ? nameLower.includes(visLower)
                    : null;
                const nameVisibleMismatch = visible && accName &&
                    !nameLower.includes(visLower);

                const isAmbiguous = visible &&
                    ambiguousSet.has(visLower);

                const hasNoName = !accName;
                const imageOnly = isImageOnly(el);

                const r = el.getBoundingClientRect();

                out.push({
                    selector: buildSelector(el),
                    tag: tag, role: role, type: type,
                    visible_text: visible,
                    accessible_name: accName,
                    name_source: accInfo.source,
                    name_includes_visible: nameIncludesVisible,
                    name_visible_mismatch: nameVisibleMismatch,
                    is_ambiguous: isAmbiguous,
                    has_no_name: hasNoName,
                    image_only: imageOnly,
                    rect: { x: r.x, y: r.y, width: r.width, height: r.height },
                });
            }

            return out;
        }""", sorted(_AMBIGUOUS_INTERACTIVE_TEXT))

        capture_data.andi_interactive_results = results or []

        out_path = os.path.join(captures_dir, "andi_interactive.json")
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(capture_data.andi_interactive_results, f, indent=2)

        no_name = sum(1 for r in capture_data.andi_interactive_results if r.get("has_no_name"))
        ambiguous = sum(1 for r in capture_data.andi_interactive_results if r.get("is_ambiguous"))
        mismatch = sum(1 for r in capture_data.andi_interactive_results if r.get("name_visible_mismatch"))
        image_only = sum(1 for r in capture_data.andi_interactive_results if r.get("image_only"))
        logger.info(
            "ANDI interactive: %d links/buttons (no_name=%d, ambiguous=%d, "
            "name_visible_mismatch=%d, image_only=%d)",
            len(capture_data.andi_interactive_results),
            no_name, ambiguous, mismatch, image_only,
        )
    except Exception:
        logger.exception("ANDI interactive extraction failed")


async def _extract_elements(
    page: Page,
    captures_dir: str,
    capture_data: CaptureData,
) -> None:
    """Extract all accessibility-relevant elements from the page."""
    # Headings
    try:
        capture_data.headings = await page.evaluate(
            "() => { " + LABELER_JS_BUNDLE + """
            function __nameOf(node) {
                // A general accname-style recursive walk that resolves
                // the name of any element from its descendants. Handles
                // text nodes, nested <img alt>, [aria-label], <svg><title>,
                // and falls back to [title]. General — works for any
                // accname pattern, not just headings, not site-specific.
                if (!node) return '';
                if (node.nodeType === 3) return (node.nodeValue || '').replace(/\\s+/g, ' ');
                if (node.nodeType !== 1) return '';
                var get = function(k) {
                    return node.getAttribute ? (node.getAttribute(k) || '').trim() : '';
                };
                var al = get('aria-label');
                if (al) return al;
                var alt = get('alt');
                if (alt) return alt;
                if (node.tagName && node.tagName.toLowerCase() === 'svg') {
                    var t = node.querySelector && node.querySelector('title');
                    if (t) {
                        var tt = (t.textContent || '').trim();
                        if (tt) return tt;
                    }
                }
                var pieces = [];
                for (var i = 0; i < node.childNodes.length; i++) {
                    var s = __nameOf(node.childNodes[i]);
                    if (s) pieces.push(s);
                }
                var combined = pieces.join(' ').replace(/\\s+/g, ' ').trim();
                if (combined) return combined;
                return get('title');
            }
            function __headingName(e) {
                // aria-labelledby takes precedence per the ARIA spec.
                var lb = e.getAttribute('aria-labelledby');
                if (lb) {
                    var parts = lb.split(/\\s+/).map(function(id) {
                        var r = document.getElementById(id);
                        return r ? __nameOf(r) : '';
                    }).filter(Boolean);
                    if (parts.length) return parts.join(' ').trim();
                }
                return __nameOf(e);
            }
            return Array.from(document.querySelectorAll('h1,h2,h3,h4,h5,h6')).map(el => ({
                tag: el.tagName.toLowerCase(),
                level: parseInt(el.tagName[1]),
                text: __headingName(el),
                text_content: (el.textContent || '').trim(),
                id: el.id || null,
                selector: el.tagName.toLowerCase() + (el.id ? '#'+el.id : ''),
                location: __wcagLabeler.describe(el),
            }));
        }"""
        )
        composed = ensure_label_fields(
            capture_data.headings, warn_prefix="Headings", required=False,
        )
        logger.info(
            "Heading extraction: %d heading(s), %d location_label(s) composed",
            len(capture_data.headings), composed,
        )
    except Exception:
        logger.exception("Heading extraction failed")

    # Links
    try:
        capture_data.links = await page.evaluate(
            "() => { " + LABELER_JS_BUNDLE + """
            // SC 1.4.1 (Use of Color) needs to know whether each link is
            // distinguished from surrounding text by something other than
            // colour: an underline, a border, an icon (svg/img), or a
            // weight/style change. The previous capture didn't gather
            // these, so the deterministic check fed empty data and
            // always returned 0 findings even when the page had real
            // colour-only-link violations. The block below extracts:
            //   - has_underline: does the link itself, or any descendant,
            //     have text-decoration containing 'underline'?
            //   - color / surrounding_color: rgb of link text vs the
            //     paragraph it sits in (for contrast measurement);
            //   - has_border: any visible border;
            //   - has_icon: contains <svg> / <img> / <i class*="icon">;
            //   - in_paragraph: parent is p/li/td/th/dd/dt AND the
            //     parent text is longer than the link text (so it's a
            //     link inline in prose, not a stand-alone link in a list
            //     of links);
            //   - font_weight / font_style: bold or italic also count
            //     as a non-colour distinction.
            const _isUnderlined = (el) => {
                const cs = window.getComputedStyle(el);
                const td = (cs.textDecoration || '') + ' ' + (cs.textDecorationLine || '');
                if (td.includes('underline')) return true;
                // Check children — Bootstrap and Drupal frequently wrap
                // link text in a span that carries the underline.
                for (const child of el.querySelectorAll('*')) {
                    const ccs = window.getComputedStyle(child);
                    const ctd = (ccs.textDecoration || '') + ' ' + (ccs.textDecorationLine || '');
                    if (ctd.includes('underline')) return true;
                }
                return false;
            };
            const _hasBorder = (el) => {
                const cs = window.getComputedStyle(el);
                for (const side of ['Top','Right','Bottom','Left']) {
                    const w = parseFloat(cs['border'+side+'Width'] || '0');
                    const sty = cs['border'+side+'Style'] || 'none';
                    if (w > 0 && sty !== 'none') return true;
                }
                return false;
            };
            const _hasIcon = (el) => {
                if (el.querySelector('svg, img')) return true;
                if (el.querySelector('i[class*="icon" i], span[class*="icon" i]')) return true;
                return false;
            };
            return Array.from(document.querySelectorAll('a[href]')).map(el => {
                // Gather surrounding context for SC 2.4.4 link purpose
                const parent = el.parentElement;
                let context = '';
                let in_paragraph = false;
                let surrounding_color = '';
                if (parent) {
                    const parentTag = parent.tagName.toLowerCase();
                    if (['p','li','td','th','dd','dt'].includes(parentTag)) {
                        context = (parent.textContent || '').trim();
                        const ownText = (el.textContent || '').trim();
                        // "In a paragraph" only when the paragraph
                        // contains text *outside* the link too — so
                        // the link is one of multiple text fragments,
                        // not a single-link list item.
                        if (context.length > ownText.length + 5) {
                            in_paragraph = true;
                        }
                        const pcs = window.getComputedStyle(parent);
                        surrounding_color = pcs.color || '';
                    }
                }
                const cs = window.getComputedStyle(el);
                const link_color = cs.color || '';
                const has_underline = _isUnderlined(el);
                const has_border = _hasBorder(el);
                const has_icon = _hasIcon(el);
                const font_weight = cs.fontWeight || '';
                const font_style = cs.fontStyle || '';
                return {
                    text: (el.textContent || '').trim(),
                    href: el.getAttribute('href') || '',
                    target: el.getAttribute('target') || '',
                    ariaLabel: el.getAttribute('aria-label') || '',
                    ariaLabelledby: el.getAttribute('aria-labelledby') || '',
                    title: el.getAttribute('title') || '',
                    role: el.getAttribute('role') || '',
                    hasImage: el.querySelector('img') !== null,
                    imgAlt: el.querySelector('img') ? el.querySelector('img').getAttribute('alt') || '' : '',
                    context: context,
                    in_paragraph: in_paragraph,
                    has_underline: has_underline,
                    has_border: has_border,
                    has_icon: has_icon,
                    color: link_color,
                    surrounding_color: surrounding_color,
                    font_weight: font_weight,
                    font_style: font_style,
                    rect: (() => { const r = el.getBoundingClientRect(); return {x: r.x, y: r.y, width: r.width, height: r.height}; })(),
                    location: __wcagLabeler.describe(el),
                };
            });
        }"""
        )
        composed = ensure_label_fields(
            capture_data.links, warn_prefix="Links", required=True,
        )
        logger.info(
            "Link extraction: %d link(s), %d location_label(s) composed",
            len(capture_data.links), composed,
        )
    except Exception:
        logger.exception("Link extraction failed")

    # Images (with screenshots of each)
    try:
        raw_images = await page.evaluate(
            "() => { " + LABELER_JS_BUNDLE + """
            // Walk ancestors to spot structural signals that an <img> is
            // intentionally decorative: parallax layers, overlay
            // backgrounds, CMS picture-with-sibling-content patterns.
            // These are what a human auditor checks before accusing a
            // large alt="" image of being "meaningful content".
            function decorativeSignals(el) {
                const sig = {
                    inside_parallax_container: false,
                    has_parallax_data_attr: false,
                    inside_overlay_block: false,
                    parent_has_content_sibling: false,
                    inside_hero_video_layer: false,
                };
                // Element-level data-parallax-* attrs
                for (const a of el.attributes || []) {
                    if (a.name && a.name.startsWith('data-parallax')) {
                        sig.has_parallax_data_attr = true;
                        break;
                    }
                }
                // Ancestor walk (bounded so we don't chase to <html>)
                let cur = el.parentElement;
                let depth = 0;
                while (cur && depth < 8) {
                    const cls = (cur.className && typeof cur.className === 'string') ? cur.className : '';
                    if (/parallax/i.test(cls)) sig.inside_parallax_container = true;
                    if (/\\boverlay\\b/i.test(cls)) sig.inside_overlay_block = true;
                    if (/hero.*video|video.*hero/i.test(cls)) sig.inside_hero_video_layer = true;
                    // The CMS pattern: <picture> inside a section whose
                    // siblings carry the real prose.
                    if (cur.tagName === 'SECTION' || /block-inline|layout__region/i.test(cls)) {
                        const sibs = cur.querySelectorAll(':scope > .content-holder, :scope > .content-bg, :scope * .content-holder, :scope * .content-bg');
                        if (sibs && sibs.length > 0) sig.parent_has_content_sibling = true;
                    }
                    cur = cur.parentElement;
                    depth++;
                }
                return sig;
            }
            return Array.from(document.querySelectorAll('img')).map((el, i) => {
                const sig = decorativeSignals(el);
                return {
                    src: el.getAttribute('src') || '',
                    alt: el.getAttribute('alt'),
                    role: el.getAttribute('role') || '',
                    ariaLabel: el.getAttribute('aria-label') || '',
                    ariaHidden: el.getAttribute('aria-hidden') || '',
                    width: el.naturalWidth,
                    height: el.naturalHeight,
                    isDecorative: el.getAttribute('role') === 'presentation' || el.getAttribute('role') === 'none' || el.getAttribute('alt') === '',
                    decorative_signals: sig,
                    index: i,
                    rect: (() => { const r = el.getBoundingClientRect(); return {x: r.x, y: r.y, width: r.width, height: r.height}; })(),
                    location: __wcagLabeler.describe(el),
                };
            });
        }"""
        )
        img_dir = os.path.join(captures_dir, "images")
        os.makedirs(img_dir, exist_ok=True)
        # Query all img elements ONCE to avoid race condition on dynamic pages
        img_els = await page.query_selector_all("img")

        # Track which images we filter out as analytics/tracking beacons
        # so the audit trail shows what was excluded and why. WCAG 1.1.1
        # applies to non-text content "presented to the user" -- 1x1
        # invisible pixels and known analytics beacons are not perceived
        # by anyone, so excluding them upstream means EVERY downstream
        # SC sees the same correctly-scoped image set (not just SC 1.1.1).
        _TRACKING_HOSTS = (
            "doubleclick.net", "googletagmanager", "google-analytics",
            "adswizz", "scorecardresearch", "facebook.com/tr",
            "linkedin.com/px", "bat.bing.com", "px.ads.linkedin",
            "criteo.com", "amplitude.com/api", "segment.io/v1",
            "mixpanel.com/track", "pinterest.com/v3",
        )

        def _is_tracking_pixel(info: dict) -> bool:
            r = info.get("rect") or {}
            try:
                w = float(r.get("width", 0) or 0)
                h = float(r.get("height", 0) or 0)
            except (TypeError, ValueError):
                w = h = 0
            # Both dimensions <= 1 CSS-px = analytics beacon
            if w and h and w <= 1 and h <= 1:
                return True
            src = (info.get("src") or "").lower()
            if any(host in src for host in _TRACKING_HOSTS):
                return True
            return False

        excluded_tracking = []
        images_out = []
        for img_info in raw_images:
            if _is_tracking_pixel(img_info):
                excluded_tracking.append(img_info.get("src", ""))
                continue
            img_path = os.path.join(img_dir, f"img_{img_info['index']}.png")
            try:
                if img_info["index"] < len(img_els):
                    el = img_els[img_info["index"]]
                    if await el.is_visible():
                        await el.screenshot(path=img_path)
                        img_info["screenshot_path"] = img_path
                    else:
                        img_info["screenshot_path"] = ""
                else:
                    img_info["screenshot_path"] = ""
            except Exception:
                img_info["screenshot_path"] = ""
            images_out.append(img_info)
        capture_data.images = images_out
        composed = ensure_label_fields(
            capture_data.images, warn_prefix="Images", required=True,
        )
        if excluded_tracking:
            logger.info(
                "Image extraction: filtered %d analytics/tracking pixel(s) "
                "out of scope for WCAG 1.1.1 (sample: %s)",
                len(excluded_tracking),
                excluded_tracking[0] if excluded_tracking else "",
            )
        logger.info(
            "Image extraction: %d image(s), %d location_label(s) composed",
            len(capture_data.images), composed,
        )
    except Exception:
        logger.exception("Image extraction failed")

    # Form fields
    try:
        capture_data.form_fields = await page.evaluate(
            "() => { " + LABELER_JS_BUNDLE + """
            const fields = document.querySelectorAll('input, select, textarea');
            return Array.from(fields).map(el => {
                const id = el.id || '';
                let labelText = '';
                if (id) {
                    const lbl = document.querySelector('label[for="' + id + '"]');
                    if (lbl) labelText = lbl.textContent.trim();
                }
                if (!labelText) {
                    const parent = el.closest('label');
                    if (parent) labelText = parent.textContent.trim();
                }
                // Fieldset / legend detection for SC 1.3.1 grouped-control
                // checks. Without this, every radio/checkbox in a properly
                // marked-up <fieldset> shows in_fieldset=None and SC 1.3.1
                // emits a false-positive "not in fieldset" finding. The
                // legend lookup uses :scope > legend so we read THIS
                // fieldset's legend, not a nested fieldset's.
                const fieldset = el.closest('fieldset');
                const inFieldset = !!fieldset;
                let groupLabel = '';
                if (fieldset) {
                    const legend = fieldset.querySelector(':scope > legend');
                    if (legend) groupLabel = legend.textContent.trim();
                }
                return {
                    tag: el.tagName.toLowerCase(),
                    type: el.getAttribute('type') || '',
                    name: el.getAttribute('name') || '',
                    id: id,
                    label: labelText,
                    ariaLabel: el.getAttribute('aria-label') || '',
                    ariaLabelledby: el.getAttribute('aria-labelledby') || '',
                    ariaDescribedby: el.getAttribute('aria-describedby') || '',
                    required: el.hasAttribute('required'),
                    placeholder: el.getAttribute('placeholder') || '',
                    role: el.getAttribute('role') || '',
                    autocomplete: el.getAttribute('autocomplete') || '',
                    in_fieldset: inFieldset,
                    group_label: groupLabel,
                    rect: (() => { const r = el.getBoundingClientRect(); return {x: r.x, y: r.y, width: r.width, height: r.height}; })(),
                    location: __wcagLabeler.describe(el),
                };
            });
        }"""
        )
        composed = ensure_label_fields(
            capture_data.form_fields, warn_prefix="FormFields", required=True,
        )
        logger.info(
            "Form field extraction: %d field(s), %d location_label(s) composed",
            len(capture_data.form_fields), composed,
        )
    except Exception:
        logger.exception("Form field extraction failed")

    # Media (audio/video with track info)
    try:
        capture_data.media = await page.evaluate("""() => {
            const items = [];
            for (const tag of ['audio', 'video']) {
                document.querySelectorAll(tag).forEach(el => {
                    const tracks = Array.from(el.querySelectorAll('track')).map(t => ({
                        kind: t.getAttribute('kind') || '',
                        src: t.getAttribute('src') || '',
                        srclang: t.getAttribute('srclang') || '',
                        label: t.getAttribute('label') || '',
                    }));
                    items.push({
                        tag: tag,
                        src: el.getAttribute('src') || el.querySelector('source')?.getAttribute('src') || '',
                        autoplay: el.hasAttribute('autoplay'),
                        loop: el.hasAttribute('loop'),
                        muted: el.hasAttribute('muted'),
                        controls: el.hasAttribute('controls'),
                        tracks: tracks,
                        ariaLabel: el.getAttribute('aria-label') || '',
                        duration: el.duration || 0,
                    });
                });
            }
            return items;
        }""")
    except Exception:
        logger.exception("Media extraction failed")

    # Landmarks
    try:
        capture_data.landmarks = await page.evaluate("""() => {
            const roles = ['banner', 'navigation', 'main', 'complementary',
                           'contentinfo', 'search', 'form', 'region'];
            const items = [];
            // Explicit ARIA roles
            for (const role of roles) {
                document.querySelectorAll('[role="' + role + '"]').forEach(el => {
                    items.push({
                        tag: el.tagName.toLowerCase(),
                        role: role,
                        ariaLabel: el.getAttribute('aria-label') || '',
                        ariaLabelledby: el.getAttribute('aria-labelledby') || '',
                        selector: el.id ? '#' + el.id : el.tagName.toLowerCase() + '[role="' + role + '"]',
                        rect: (() => { const r = el.getBoundingClientRect(); return {x: r.x, y: r.y, width: r.width, height: r.height}; })(),
                    });
                });
            }
            // Implicit landmark elements.
            // Per HTML5/ARIA Landmark Roles, <header> is a banner ONLY when
            // NOT a descendant of <article>, <aside>, <main>, <nav>, or
            // <section>. Same exemption for <footer> -> contentinfo and
            // <aside> -> complementary. <nav> and <main> are unconditional.
            // Without this gate, nested section headers/footers get
            // miscounted as duplicate banner/contentinfo landmarks (real
            // SC 1.3.1 false positive observed on a university homepage).
            const SECTIONING_ANCESTORS = ['article', 'aside', 'main', 'nav', 'section'];
            const hasSectioningAncestor = (el) => {
                let p = el.parentElement;
                while (p) {
                    if (SECTIONING_ANCESTORS.includes(p.tagName.toLowerCase())) {
                        return true;
                    }
                    p = p.parentElement;
                }
                return false;
            };
            const mapping = {header: 'banner', nav: 'navigation', main: 'main',
                             aside: 'complementary', footer: 'contentinfo'};
            const SCOPED_TO_TOPLEVEL = new Set(['header', 'footer', 'aside']);
            for (const [tag, role] of Object.entries(mapping)) {
                document.querySelectorAll(tag).forEach(el => {
                    if (el.getAttribute('role')) return;
                    if (SCOPED_TO_TOPLEVEL.has(tag) && hasSectioningAncestor(el)) {
                        return;  // section header/footer/aside, not a landmark
                    }
                    items.push({
                        tag: tag,
                        role: role,
                        ariaLabel: el.getAttribute('aria-label') || '',
                        ariaLabelledby: el.getAttribute('aria-labelledby') || '',
                        selector: el.id ? '#' + el.id : el.tagName.toLowerCase(),
                        rect: (() => { const r = el.getBoundingClientRect(); return {x: r.x, y: r.y, width: r.width, height: r.height}; })(),
                    });
                });
            }
            return items;
        }""")
    except Exception:
        logger.exception("Landmark extraction failed")

    # Colors (foreground / background with contrast ratios)
    try:
        capture_data.colors = await page.evaluate("""() => {
            const results = [];
            const seen = new Set();
            const els = document.querySelectorAll('body, body *');

            // A text-bearing element is "visually hidden" when its
            // computed style matches the standard sr-only pattern:
            // 1px size + clip + position:absolute, OR transformed off
            // screen, OR explicitly transparent. These elements render
            // text only to assistive tech; measuring contrast on them
            // produces meaningless ratios and creates SC 1.4.3 false
            // positives (observed on a university's nav sub-toggle buttons whose
            // ".visually-hidden" descendants gave a 1.23:1 reading).
            const isVisuallyHidden = (el) => {
                if (!el || el.nodeType !== 1) return false;
                const cs = window.getComputedStyle(el);
                if (cs.display === 'none' || cs.visibility === 'hidden') return true;
                if (parseFloat(cs.opacity) === 0) return true;
                if (cs.position === 'absolute' || cs.position === 'fixed') {
                    // sr-only common pattern: 1px clipped
                    if (cs.clip === 'rect(0px, 0px, 0px, 0px)' ||
                        cs.clip === 'rect(0, 0, 0, 0)' ||
                        (cs.clipPath && cs.clipPath !== 'none' && cs.clipPath !== 'auto' &&
                         (cs.clipPath.includes('inset(100%)') || cs.clipPath.includes('inset(50%)')))) {
                        return true;
                    }
                    const r = el.getBoundingClientRect();
                    if (r.width <= 1 && r.height <= 1) return true;
                }
                // Off-screen positioning
                if (cs.position === 'absolute' || cs.position === 'fixed') {
                    const r = el.getBoundingClientRect();
                    if (r.right < 0 || r.bottom < 0) return true;
                    if (r.left < -1000 || r.top < -1000) return true;
                }
                return false;
            };

            // Returns visible text only -- recursively collects text
            // from the element's children, skipping any subtree that is
            // visually hidden. If the result is empty, the element has
            // no actual visible text and should be excluded from the
            // contrast pass entirely.
            const collectVisibleText = (el) => {
                if (!el) return '';
                if (isVisuallyHidden(el)) return '';
                let out = '';
                for (const node of el.childNodes) {
                    if (node.nodeType === 3) {
                        out += node.textContent || '';
                    } else if (node.nodeType === 1) {
                        out += collectVisibleText(node);
                    }
                }
                return out;
            };

            for (const el of els) {
                if (isVisuallyHidden(el)) continue;
                const text = collectVisibleText(el).trim();
                if (!text) continue;
                const cs = window.getComputedStyle(el);
                if (cs.display === 'none' || cs.visibility === 'hidden') continue;
                if (parseFloat(cs.opacity) === 0) continue;
                const rect = el.getBoundingClientRect();
                if (rect.width < 2 || rect.height < 2) continue;
                // Resolve effective background color by walking up
                let bg = cs.backgroundColor;
                let bgEl = el;
                let hasBgImage = false;
                let reachedRoot = false;
                while (bg === 'rgba(0, 0, 0, 0)' || bg === 'transparent') {
                    bgEl = bgEl.parentElement;
                    if (!bgEl) { bg = 'rgb(255, 255, 255)'; reachedRoot = true; break; }
                    bg = window.getComputedStyle(bgEl).backgroundColor;
                    if (window.getComputedStyle(bgEl).backgroundImage !== 'none') {
                        hasBgImage = true;
                    }
                }
                if (reachedRoot) { hasBgImage = true; }
                const fg = cs.color;
                const key = fg + '|' + bg;
                if (seen.has(key)) continue;
                seen.add(key);
                results.push({
                    foreground: fg,
                    background: bg,
                    background_color: bg,
                    fontSize: cs.fontSize,
                    fontWeight: cs.fontWeight,
                    sampleText: text,
                    text: text,
                    tag: el.tagName.toLowerCase(),
                    hasBgImage: hasBgImage,
                });
            }
            return results;
        }""")
    except Exception:
        logger.exception("Color extraction failed")

    # Tables
    try:
        capture_data.tables = await page.evaluate("""() => {
            return Array.from(document.querySelectorAll('table')).map(tbl => {
                const headers = Array.from(tbl.querySelectorAll('th')).map(th => ({
                    text: th.textContent.trim(),
                    scope: th.getAttribute('scope') || '',
                    id: th.id || '',
                }));
                const caption = tbl.querySelector('caption');
                return {
                    caption: caption ? caption.textContent.trim() : '',
                    summary: tbl.getAttribute('summary') || '',
                    role: tbl.getAttribute('role') || '',
                    headers: headers,
                    rowCount: tbl.rows ? tbl.rows.length : 0,
                    ariaLabel: tbl.getAttribute('aria-label') || '',
                    ariaLabelledby: tbl.getAttribute('aria-labelledby') || '',
                };
            });
        }""")
    except Exception:
        logger.exception("Table extraction failed")

    # Lists
    try:
        capture_data.lists = await page.evaluate("""() => {
            return Array.from(document.querySelectorAll('ul, ol, dl')).map(el => ({
                tag: el.tagName.toLowerCase(),
                itemCount: el.children.length,
                role: el.getAttribute('role') || '',
                ariaLabel: el.getAttribute('aria-label') || '',
                selector: el.id ? '#' + el.id : el.tagName.toLowerCase() + (el.getAttribute('role') ? '[role="' + el.getAttribute('role') + '"]' : ''),
                rect: (() => { const r = el.getBoundingClientRect(); return {x: r.x, y: r.y, width: r.width, height: r.height}; })(),
            }));
        }""")
    except Exception:
        logger.exception("List extraction failed")

    # Iframes
    try:
        capture_data.iframes = await page.evaluate(
            "() => { " + LABELER_JS_BUNDLE + """
            return Array.from(document.querySelectorAll('iframe')).map(el => ({
                src: el.getAttribute('src') || '',
                'data-src': el.getAttribute('data-src') || '',
                title: el.getAttribute('title') || '',
                ariaLabel: el.getAttribute('aria-label') || '',
                ariaHidden: el.getAttribute('aria-hidden') || '',
                width: el.getAttribute('width') || '',
                height: el.getAttribute('height') || '',
                name: el.getAttribute('name') || '',
                allow: el.getAttribute('allow') || '',
                allowfullscreen: el.hasAttribute('allowfullscreen') ? 'true' : '',
                location: __wcagLabeler.describe(el),
            }));
        }"""
        )
        composed = ensure_label_fields(
            capture_data.iframes, warn_prefix="Iframes", required=True,
        )
        logger.info(
            "Iframe extraction: %d iframe(s), %d location_label(s) composed",
            len(capture_data.iframes), composed,
        )
    except Exception:
        logger.exception("Iframe extraction failed")

    # Background images
    try:
        capture_data.background_images = await page.evaluate("""() => {
            const results = [];
            const els = document.querySelectorAll('body, body *');
            for (const el of els) {
                const cs = window.getComputedStyle(el);
                const bg = cs.backgroundImage;
                if (bg && bg !== 'none') {
                    results.push({
                        tag: el.tagName.toLowerCase(),
                        backgroundImage: bg,
                        role: el.getAttribute('role') || '',
                        ariaLabel: el.getAttribute('aria-label') || '',
                        text: (el.textContent || '').trim(),
                    });
                }
            }
            return results;
        }""")
    except Exception:
        logger.exception("Background image extraction failed")

    # CAPTCHAs
    try:
        capture_data.captchas = await page.evaluate("""() => {
            const selectors = [
                'iframe[src*="recaptcha"]',
                'iframe[src*="hcaptcha"]',
                'iframe[src*="captcha"]',
                '[class*="captcha"]',
                '[id*="captcha"]',
                '[class*="CAPTCHA"]',
                '[id*="CAPTCHA"]',
                '.g-recaptcha',
                '.h-captcha',
            ];
            const found = [];
            const seen = new Set();
            for (const sel of selectors) {
                document.querySelectorAll(sel).forEach(el => {
                    const key = el.outerHTML;
                    if (seen.has(key)) return;
                    seen.add(key);
                    found.push({
                        tag: el.tagName.toLowerCase(),
                        type: el.getAttribute('src') || el.getAttribute('class') || '',
                        ariaLabel: el.getAttribute('aria-label') || '',
                        title: el.getAttribute('title') || '',
                    });
                });
            }
            return found;
        }""")
    except Exception:
        logger.exception("CAPTCHA extraction failed")

    # Skip links
    try:
        capture_data.skip_links = await page.evaluate("""() => {
            const links = document.querySelectorAll('a[href^="#"]');
            const results = [];
            const skipPatterns = [
                /skip/i, /jump/i, /main.content/i, /go.to.content/i,
                /navigate.to/i, /bypass/i, /aller.au.contenu/i,
                /zum.inhalt/i, /ir.al.contenido/i, /saltar/i,
            ];
            for (const a of links) {
                const text = (a.textContent || '').trim();
                const isSkipLink = skipPatterns.some(p => p.test(text));
                // Also check if it's the first or second link on the page
                // and points to an ID that looks like main content
                const href = a.getAttribute('href') || '';
                const targetId = href.replace('#', '');
                const isFirstLink = a === document.querySelector('a');
                const pointsToMain = /main|content|body|wrapper/i.test(targetId);
                if (isSkipLink || (isFirstLink && pointsToMain)) {
                    const target = targetId ? document.getElementById(targetId) : null;
                    results.push({
                        text: text,
                        href: href,
                        targetExists: target !== null,
                        targetTag: target ? target.tagName.toLowerCase() : null,
                        targetRole: target ? (target.getAttribute('role') || '') : null,
                        selector: 'a[href="' + href + '"]',
                        rect: (() => { const r = a.getBoundingClientRect(); return {x: r.x, y: r.y, width: r.width, height: r.height}; })(),
                    });
                }
            }
            return results;
        }""")
    except Exception:
        logger.exception("Skip link extraction failed")

    # Positioned elements (SC 1.4.10 reflow ground truth).
    # Enumerates every element whose computed `position` is fixed, sticky,
    # or absolute, with a stable selector + the position value + rect.
    # The judge may only assert position:fixed/sticky for an element that
    # appears in this list -- the claim validator enforces that.
    try:
        capture_data.positioned_elements = await page.evaluate(r"""() => {
            function getSelector(el) {
                if (el.id) return '#' + CSS.escape(el.id);
                const parts = [];
                let current = el;
                while (current && current !== document.body && current !== document.documentElement) {
                    let seg = current.tagName.toLowerCase();
                    if (current.id) {
                        parts.unshift('#' + CSS.escape(current.id));
                        break;
                    }
                    if (current.parentElement) {
                        const sibs = Array.from(current.parentElement.children)
                            .filter(s => s.tagName === current.tagName);
                        if (sibs.length > 1) {
                            seg += ':nth-of-type(' + (sibs.indexOf(current) + 1) + ')';
                        }
                    }
                    parts.unshift(seg);
                    current = current.parentElement;
                    if (parts.length >= 4) break;
                }
                return parts.join(' > ');
            }
            const results = [];
            for (const el of document.querySelectorAll('body *')) {
                const cs = window.getComputedStyle(el);
                const pos = cs.position;
                if (pos === 'fixed' || pos === 'sticky' || pos === 'absolute') {
                    const r = el.getBoundingClientRect();
                    results.push({
                        tag: el.tagName.toLowerCase(),
                        selector: getSelector(el),
                        position: pos,
                        rect: {x: r.x, y: r.y, width: r.width, height: r.height},
                    });
                }
            }
            return results;
        }""")
    except Exception:
        logger.exception("Positioned-element extraction failed")

    # Viewport meta
    try:
        capture_data.viewport_meta = await page.evaluate("""() => {
            const meta = document.querySelector('meta[name="viewport"]');
            if (!meta) return null;
            const content = meta.getAttribute('content') || '';
            const parsed = {};
            content.split(',').forEach(part => {
                const [k, v] = part.split('=').map(s => s.trim());
                if (k) parsed[k] = v || '';
            });
            return {
                content: content,
                parsed: parsed,
            };
        }""")
    except Exception:
        logger.exception("Viewport meta extraction failed")

    # HTML lang attribute and document language metadata (SC 3.1.1, 3.1.2)
    try:
        capture_data.page_language = await page.evaluate("""() => {
            const html = document.documentElement;
            const lang = html.getAttribute('lang') || '';
            const xmlLang = html.getAttribute('xml:lang') || '';
            const contentLang = document.querySelector('meta[http-equiv="content-language"]');
            const metaLang = contentLang ? contentLang.getAttribute('content') || '' : '';
            // Find elements with explicit lang attributes (SC 3.1.2)
            const langParts = [];
            const langEls = document.querySelectorAll('[lang]');
            for (const el of langEls) {
                if (el === html) continue;
                langParts.push({
                    tag: el.tagName.toLowerCase(),
                    lang: el.getAttribute('lang') || '',
                    text: (el.textContent || '').trim(),
                });
                // No limit — collect all lang parts
            }
            return {
                html_lang: lang,
                xml_lang: xmlLang,
                meta_content_language: metaLang,
                has_lang: lang !== '',
                lang_valid: /^[a-zA-Z]{2,3}(-[a-zA-Z0-9]+)*$/.test(lang),
                lang_parts: langParts,
            };
        }""")
    except Exception:
        logger.exception("Page language extraction failed")

    # Pseudo-element content (::before, ::after with meaningful content)
    try:
        capture_data.pseudo_elements = await page.evaluate("""() => {
            const results = [];
            const els = document.querySelectorAll('body *');
            let count = 0;
            for (const el of els) {
                // No limit — check all elements
                for (const pseudo of ['::before', '::after']) {
                    const cs = window.getComputedStyle(el, pseudo === '::before' ? ':before' : ':after');
                    const content = cs.content;
                    if (content && content !== 'none' && content !== 'normal' && content !== '""' && content !== "''") {
                        let textContent = content.replace(/^["']|["']$/g, '');
                        if (textContent.length <= 1) continue;
                        let selector = el.tagName.toLowerCase();
                        if (el.id) selector = '#' + el.id;
                        results.push({
                            selector: selector,
                            pseudo: pseudo,
                            content: textContent,
                            display: cs.display,
                            visibility: cs.visibility,
                            ariaHidden: el.getAttribute('aria-hidden') || '',
                        });
                        count++;
                    }
                }
            }
            return results;
        }""")
    except Exception:
        logger.exception("Pseudo-element extraction failed")


async def _detect_overflow_zoom(page: Page, capture_data: CaptureData) -> None:
    """Detect elements that overflow at 200% zoom."""
    try:
        await page.evaluate(f"document.body.style.zoom = '{ZOOM_FACTOR}'")
        await page.wait_for_timeout(500)
        overflow_data = await page.evaluate("""() => {
            const results = [];
            const vw = window.innerWidth;
            const vh = window.innerHeight;
            const elements = document.querySelectorAll('body *');
            for (const el of elements) {
                const rect = el.getBoundingClientRect();
                if (rect.right > vw || rect.bottom > vh) {
                    const cs = window.getComputedStyle(el);
                    if (cs.overflow === 'visible' || cs.overflowX === 'visible') {
                        results.push({
                            tag: el.tagName.toLowerCase(),
                            selector: el.id ? '#' + el.id : el.tagName.toLowerCase(),
                            rect: {x: rect.x, y: rect.y, width: rect.width, height: rect.height},
                            overflowX: rect.right > vw,
                            overflowY: rect.bottom > vh,
                        });
                    }
                }
                // No limit — collect all overflows
            }
            return results;
        }""")
        capture_data.overflow_200pct = overflow_data
        await page.evaluate("document.body.style.zoom = '1'")
        await page.wait_for_timeout(300)
    except Exception:
        logger.exception("200%% overflow detection failed")
        try:
            await page.evaluate("document.body.style.zoom = '1'")
        except Exception:
            pass  # cleanup — best-effort zoom reset, page state may be unrecoverable


async def _detect_overflow_narrow(
    page: Page,
    context: BrowserContext,
    url: str,
    capture_data: CaptureData,
) -> None:
    """Detect elements that overflow and horizontal scroll at 320px width."""
    try:
        await page.set_viewport_size({"width": VIEWPORT_NARROW, "height": VIEWPORT_HEIGHT})
        await page.wait_for_timeout(500)

        result = await page.evaluate(r"""() => {
            // Same getSelector algorithm as the rest of the v2 pipeline
            // so SC checks can correlate overflow entries with inventory
            // entries. Without this, all overflowing <img> elements
            // collapse to selector="img" (18 entries, useless to the AI).
            function getSelector(el) {
                if (el.id) return '#' + CSS.escape(el.id);
                const parts = [];
                let current = el;
                while (current && current !== document.body && current !== document.documentElement) {
                    let seg = current.tagName.toLowerCase();
                    if (current.id) {
                        parts.unshift('#' + CSS.escape(current.id));
                        break;
                    }
                    if (current.parentElement) {
                        const sibs = Array.from(current.parentElement.children)
                            .filter(s => s.tagName === current.tagName);
                        if (sibs.length > 1) {
                            seg += ':nth-of-type(' + (sibs.indexOf(current) + 1) + ')';
                        }
                    }
                    parts.unshift(seg);
                    current = current.parentElement;
                    if (parts.length >= 4) break;
                }
                return parts.join(' > ');
            }

            const docWidth = document.documentElement.scrollWidth;
            const vpWidth = window.innerWidth;
            const hasHScroll = docWidth > vpWidth;
            const overflows = [];
            const elements = document.querySelectorAll('body *');
            for (const el of elements) {
                const rect = el.getBoundingClientRect();
                if (rect.right > vpWidth + 1) {
                    overflows.push({
                        tag: el.tagName.toLowerCase(),
                        selector: getSelector(el),
                        rect: {x: rect.x, y: rect.y, width: rect.width, height: rect.height},
                    });
                }
                // No limit — collect all overflows
            }
            return {hasHScroll, overflows};
        }""")
        capture_data.overflow_320px = result.get("overflows", [])
        capture_data.horizontal_scroll_320 = result.get("hasHScroll", False)

        # Restore viewport
        await page.set_viewport_size({"width": VIEWPORT_WIDTH, "height": VIEWPORT_HEIGHT})
        await page.wait_for_timeout(300)
    except Exception:
        logger.exception("320px overflow detection failed")
        try:
            await page.set_viewport_size({"width": VIEWPORT_WIDTH, "height": VIEWPORT_HEIGHT})
        except Exception:
            pass  # cleanup — best-effort viewport reset, page state may be unrecoverable
