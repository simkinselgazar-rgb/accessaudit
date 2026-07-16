"""V2 capture pipeline orchestrator.

The single entry point is `capture_web_page_v2`. It orchestrates the
five capture phases for one web page:

  Phase 0: Observation (video recording, flash analysis) — from v1
  Phase D: Deterministic capture (DOM, a11y tree, screenshots) — from v1
  Phase 1: Static Code AI (element inventory)
  Phase 2: Visual AI Explorer (screenshot on every state change)
  Phase 3: Video Segments (AI-planned recordings)
  Phase 4: AT Simulation (existing modules + cross-reference)

Phase 5 (SC Testing) runs separately via the check pipeline.

The two large helpers used inside this orchestrator live next door:
  - `_capture_html_media_and_form_attrs` -> `capture.v2.html_extraction`
  - `_run_v1_extractions`                -> `capture.v2.v1_compat`

Imported by name here so the existing call sites inside
`capture_web_page_v2` keep using bare names.
"""
from __future__ import annotations

import json
import logging
import os
import time
from typing import Any

from models import CaptureData

from capture.v2.html_extraction import _capture_html_media_and_form_attrs
from capture.v2.v1_compat import _run_v1_extractions

logger = logging.getLogger(__name__)


async def capture_web_page_v2(
    url: str,
    review_dir: str,
    user_context: dict | None = None,
    auth_callback=None,
    progress_callback=None,
) -> CaptureData:
    """V2 AI-driven capture pipeline.

    Args:
        url: Page URL to capture
        review_dir: Output directory for this review/page
        user_context: Optional user-provided context
        auth_callback: Async callback for auth progress messages
        progress_callback: Async callback for phase progress updates

    Returns:
        Fully populated CaptureData object
    """
    pipeline_start = time.monotonic()
    logger.info("=" * 70)
    logger.info("V2 CAPTURE PIPELINE START: %s", url)
    logger.info("=" * 70)

    captures_dir = os.path.join(review_dir, "captures")
    os.makedirs(captures_dir, exist_ok=True)

    log_dir = os.path.join(captures_dir, "capture_logs")
    os.makedirs(log_dir, exist_ok=True)

    capture_data = CaptureData(
        url=url,
        review_dir=review_dir,
        captures_dir=captures_dir,
        user_context=user_context or {},
        capture_pipeline_version="v2",
    )

    # Determine review root (for auth state lookups)
    review_root = review_dir
    if os.path.basename(review_dir).startswith("page_") or os.path.basename(review_dir).startswith("doc_"):
        review_root = os.path.dirname(review_dir)

    # ── Phase 0: Observation (v1 reuse) ──────────────────────────
    if progress_callback:
        await progress_callback("Phase 0: Recording page observation video...")

    logger.info("PHASE 0: Observation")
    try:
        from playwright.async_api import async_playwright
        async with async_playwright() as pw:
            from capture.web_capture import _phase0_observation
            await _phase0_observation(pw, url, captures_dir, capture_data)
            logger.info("PHASE 0 COMPLETE")
    except Exception as e:
        logger.warning("PHASE 0 FAILED: %s", e)

    capture_data.phase_timings["phase0"] = round(time.monotonic() - pipeline_start, 1)

    # ── Phase D: Deterministic capture ───────────────────────────
    if progress_callback:
        await progress_callback("Capturing: DOM, accessibility tree, screenshots...")

    logger.info("PHASE D: Deterministic capture")
    phase_d_start = time.monotonic()

    from playwright.async_api import async_playwright
    from config import PLAYWRIGHT_TIMEOUT, VIEWPORT_WIDTH, VIEWPORT_HEIGHT, ZOOM_FACTOR

    async with async_playwright() as pw:
        browser = await pw.chromium.launch()

        from capture.auth import get_auth_state_path
        auth_state = get_auth_state_path(review_root, url=url)

        ctx_kwargs = {"viewport": {"width": VIEWPORT_WIDTH, "height": VIEWPORT_HEIGHT}}
        if auth_state:
            ctx_kwargs["storage_state"] = auth_state
            logger.info("PHASE D: Using saved auth state")

        # Use a real user agent to avoid bot detection
        _USER_AGENT = (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
        )
        ctx_kwargs["user_agent"] = _USER_AGENT

        context = await browser.new_context(**ctx_kwargs)
        context.set_default_timeout(PLAYWRIGHT_TIMEOUT)
        page = await context.new_page()

        logger.info("PHASE D: Navigating to %s", url)
        nav_success = False
        for wait_strategy in ["networkidle", "domcontentloaded", "load", "commit"]:
            try:
                await page.goto(url, wait_until=wait_strategy, timeout=PLAYWRIGHT_TIMEOUT)
                # Check we didn't land on chrome error page
                current_url = page.url
                if "chrome-error" in current_url or "about:blank" in current_url:
                    logger.warning("PHASE D: Landed on error page with %s — retrying", wait_strategy)
                    # Fresh page for next attempt
                    await page.close()
                    page = await context.new_page()
                    continue
                nav_success = True
                logger.info("PHASE D: Navigation succeeded with wait_until=%s", wait_strategy)
                break
            except Exception as e:
                logger.warning("PHASE D: %s failed — %s", wait_strategy, e)
                # Fresh page for next attempt to avoid stale state
                try:
                    await page.close()
                    page = await context.new_page()
                except Exception:
                    pass  # cleanup — best-effort fresh page after nav failure, browser state may be unrecoverable

        if not nav_success:
            raise RuntimeError(f"Could not navigate to {url} after trying all wait strategies")

        # Post-navigation hydration stabilization. networkidle fires when
        # requests stop; SPA frameworks (React, Vue, Instructure-UI, etc.)
        # may still be mutating the DOM. Wait for mutations to quiet so
        # the capture sees the final rendered UI.
        try:
            from capture.web_capture import _wait_for_dom_stabilization
            await _wait_for_dom_stabilization(page)
        except Exception:
            pass  # best-effort — DOM stabilization may fail on long-running SPAs; capture proceeds anyway

        # Login detection
        from capture.auth import detect_login_page, authenticate_interactive
        if await detect_login_page(page):
            logger.info("PHASE D: Login detected — opening browser for auth")
            await page.close()
            await context.close()
            await browser.close()
            state_path = await authenticate_interactive(url, review_root, progress_callback=auth_callback)
            if state_path:
                auth_state = state_path
            browser = await pw.chromium.launch()
            if auth_state:
                ctx_kwargs["storage_state"] = auth_state
            context = await browser.new_context(**ctx_kwargs)
            context.set_default_timeout(PLAYWRIGHT_TIMEOUT)
            page = await context.new_page()
            try:
                await page.goto(url, wait_until="networkidle", timeout=PLAYWRIGHT_TIMEOUT)
            except Exception:
                logger.warning("PHASE D: networkidle timeout (post-auth) — retrying with domcontentloaded")
                await page.goto(url, wait_until="domcontentloaded", timeout=PLAYWRIGHT_TIMEOUT)

        capture_data.title = await page.title()
        logger.info("PHASE D: Title: \"%s\"", capture_data.title)

        # ── Fix 1: Dismiss cookie consent overlays ───────────────
        logger.info("PHASE D: Checking for cookie consent overlays...")
        try:
            dismissed = await page.evaluate("""
                () => {
                    // Common cookie consent selectors
                    const selectors = [
                        '[class*="cookie"] button[class*="accept"]',
                        '[class*="cookie"] button[class*="agree"]',
                        '[class*="consent"] button[class*="accept"]',
                        '[id*="cookie"] button',
                        '#onetrust-accept-btn-handler',
                        '.cc-accept', '.cc-dismiss',
                        '[data-cookieconsent="accept"]',
                        'button[aria-label*="cookie" i]',
                        'button[aria-label*="accept" i]',
                    ];
                    for (const sel of selectors) {
                        const btn = document.querySelector(sel);
                        if (btn && btn.offsetParent !== null) {
                            btn.click();
                            return sel;
                        }
                    }
                    // Try finding any modal/overlay with cookie in text
                    const overlays = document.querySelectorAll('[class*="cookie"], [class*="consent"], [class*="gdpr"]');
                    for (const o of overlays) {
                        const btn = o.querySelector('button');
                        if (btn && btn.offsetParent !== null) {
                            btn.click();
                            return 'generic overlay button';
                        }
                    }
                    return null;
                }
            """)
            if dismissed:
                logger.info("PHASE D: Cookie consent dismissed via: %s", dismissed)
                await page.wait_for_timeout(1000)
            else:
                logger.info("PHASE D: No cookie consent overlay found")
        except Exception as e:
            logger.debug("PHASE D: Cookie consent check failed: %s", e)

        # ── Fix 2: Force lazy-loaded content to load ─────────────
        logger.info("PHASE D: Scrolling to load lazy content...")
        try:
            # Scroll to bottom to trigger lazy loading
            # Hard limits to prevent infinite scroll pages from looping forever
            MAX_SCROLL_TIME = 10  # seconds
            MAX_SCROLL_CYCLES = 5  # times the page grows before we stop

            scroll_height = await page.evaluate("document.body.scrollHeight")
            viewport_height = await page.evaluate("window.innerHeight")
            position = 0
            scroll_start = time.monotonic()
            growth_count = 0

            while position < scroll_height:
                # Time limit
                if time.monotonic() - scroll_start > MAX_SCROLL_TIME:
                    logger.info("PHASE D: Scroll time limit reached (%.0fs)", MAX_SCROLL_TIME)
                    break

                position += viewport_height
                await page.evaluate(f"window.scrollTo(0, {position})")
                await page.wait_for_timeout(300)

                # Check if more content loaded
                new_height = await page.evaluate("document.body.scrollHeight")
                if new_height > scroll_height:
                    growth_count += 1
                    scroll_height = new_height
                    # Cycle limit for infinite scroll pages
                    if growth_count >= MAX_SCROLL_CYCLES:
                        logger.info("PHASE D: Scroll growth limit reached (%d cycles)", MAX_SCROLL_CYCLES)
                        break

            # Force all lazy images to eager load
            await page.evaluate("""
                () => {
                    document.querySelectorAll('img[loading="lazy"]').forEach(img => {
                        img.loading = 'eager';
                        if (img.dataset.src) img.src = img.dataset.src;
                        if (img.dataset.srcset) img.srcset = img.dataset.srcset;
                    });
                    // Also trigger IntersectionObserver entries
                    document.querySelectorAll('img[data-src], [data-lazy]').forEach(el => {
                        if (el.dataset.src) el.src = el.dataset.src;
                    });
                }
            """)
            await page.wait_for_timeout(1000)

            # Scroll back to top for screenshots
            await page.evaluate("window.scrollTo(0, 0)")
            await page.wait_for_timeout(500)
            logger.info("PHASE D: Lazy content loaded (scrolled %dpx)", scroll_height)
        except Exception as e:
            logger.debug("PHASE D: Lazy load scroll failed: %s", e)

        # Screenshots (after cookie dismiss + lazy load)
        fp_path = os.path.join(captures_dir, "full_page.png")
        await page.screenshot(path=fp_path, full_page=True)
        capture_data.full_page_path = fp_path

        vp_path = os.path.join(captures_dir, "viewport.png")
        await page.screenshot(path=vp_path, full_page=False)
        capture_data.viewport_path = vp_path

        vp200_path = os.path.join(captures_dir, "viewport_200pct.png")
        fp200_path = os.path.join(captures_dir, "full_page_200pct.png")
        await page.evaluate(f"document.body.style.zoom = '{ZOOM_FACTOR}'")
        await page.wait_for_timeout(800)
        await page.screenshot(path=vp200_path, full_page=False)
        await page.screenshot(path=fp200_path, full_page=True)
        await page.evaluate("document.body.style.zoom = '1'")
        capture_data.viewport_200pct_path = vp200_path
        capture_data.full_page_200pct_path = fp200_path

        vp320_path = os.path.join(captures_dir, "viewport_320px.png")
        narrow = await context.new_page()
        await narrow.set_viewport_size({"width": 320, "height": 720})
        await narrow.goto(url, wait_until="domcontentloaded", timeout=PLAYWRIGHT_TIMEOUT)
        await narrow.wait_for_timeout(1000)
        await narrow.screenshot(path=vp320_path, full_page=False)
        await narrow.close()
        capture_data.viewport_320px_path = vp320_path
        logger.info("PHASE D: 4 screenshots captured")

        # ── Fix 3: Pierce Shadow DOM before capturing HTML ───────
        logger.info("PHASE D: Extracting DOM (piercing Shadow DOM)...")
        try:
            capture_data.html = await page.evaluate("""
                () => {
                    // Recursively serialize DOM including shadow roots
                    function serializeNode(node) {
                        if (node.nodeType === Node.TEXT_NODE) return node.textContent;
                        if (node.nodeType !== Node.ELEMENT_NODE) return '';

                        const tag = node.tagName.toLowerCase();
                        let attrs = '';
                        for (const attr of node.attributes || []) {
                            attrs += ` ${attr.name}="${attr.value.replace(/"/g, '&quot;')}"`;
                        }

                        let inner = '';
                        // Check for shadow root
                        if (node.shadowRoot) {
                            inner += '<!-- shadow-root -->';
                            for (const child of node.shadowRoot.childNodes) {
                                inner += serializeNode(child);
                            }
                            inner += '<!-- /shadow-root -->';
                        }
                        // Regular children
                        for (const child of node.childNodes) {
                            inner += serializeNode(child);
                        }

                        const voidTags = new Set(['area','base','br','col','embed','hr','img','input','link','meta','param','source','track','wbr']);
                        if (voidTags.has(tag)) return `<${tag}${attrs}>`;
                        return `<${tag}${attrs}>${inner}</${tag}>`;
                    }
                    return serializeNode(document.documentElement);
                }
            """)
        except Exception as e:
            logger.warning("PHASE D: Shadow DOM pierce failed, using standard DOM: %s", e)
            capture_data.html = await page.content()

        dom_path = os.path.join(captures_dir, "dom.html")
        with open(dom_path, "w", encoding="utf-8") as f:
            f.write(capture_data.html)
        capture_data.dom_path = dom_path
        logger.info("PHASE D: DOM saved (%d chars, shadow roots included)", len(capture_data.html))

        # ── Fix 4: Capture iframe contents + elements ────────────
        logger.info("PHASE D: Extracting iframe contents...")
        try:
            iframe_data = []
            iframe_elements = []
            frames = page.frames
            for frame in frames:
                if frame == page.main_frame:
                    continue
                try:
                    frame_url = frame.url
                    frame_html = await frame.content()
                    iframe_data.append({
                        "url": frame_url,
                        "html": frame_html,
                        "title": await frame.title() if hasattr(frame, 'title') else "",
                    })

                    # Extract elements from inside the iframe with offset rects.
                    # The rect from getBoundingClientRect inside a frame is
                    # relative to the frame viewport. We need to offset by the
                    # iframe element's position on the main page.
                    try:
                        # Find the iframe element on the main page to get its offset
                        frame_element = await frame.frame_element()
                        iframe_box = await frame_element.bounding_box() if frame_element else None
                        offset_x = iframe_box["x"] if iframe_box else 0
                        offset_y = iframe_box["y"] if iframe_box else 0

                        # Extract key elements from inside the frame
                        frame_elems = await frame.evaluate("""() => {
                            const results = [];
                            // Images
                            document.querySelectorAll('img,[role="img"]').forEach(el => {
                                const r = el.getBoundingClientRect();
                                results.push({
                                    type: 'image', tag: el.tagName.toLowerCase(),
                                    src: el.src || '', alt: el.alt || '',
                                    text: '', visible: r.width > 0 && r.height > 0,
                                    rect: {x: r.x, y: r.y, width: r.width, height: r.height},
                                    in_iframe: true,
                                });
                            });
                            // Links
                            document.querySelectorAll('a[href]').forEach(el => {
                                const r = el.getBoundingClientRect();
                                results.push({
                                    type: 'link', tag: 'a',
                                    text: (el.textContent || '').trim(),
                                    href: el.href || '', visible: r.width > 0,
                                    rect: {x: r.x, y: r.y, width: r.width, height: r.height},
                                    in_iframe: true,
                                });
                            });
                            // Form fields
                            document.querySelectorAll('input,select,textarea,button').forEach(el => {
                                const r = el.getBoundingClientRect();
                                const tag = el.tagName.toLowerCase();
                                results.push({
                                    type: tag === 'button' ? 'button' : 'form_field',
                                    tag: tag, text: (el.textContent || el.value || '').trim(),
                                    visible: r.width > 0, in_iframe: true,
                                    rect: {x: r.x, y: r.y, width: r.width, height: r.height},
                                });
                            });
                            return results;
                        }""")

                        # Offset rects to main page coordinates
                        for fe in frame_elems:
                            r = fe.get("rect")
                            if r:
                                fe["rect"] = {
                                    "x": r["x"] + offset_x,
                                    "y": r["y"] + offset_y,
                                    "width": r["width"],
                                    "height": r["height"],
                                }
                            fe["selector"] = f"iframe[src*='{frame_url}'] >> {fe.get('tag', '?')}"
                        iframe_elements.extend(frame_elems)
                    except Exception as iframe_eval_err:
                        logger.warning("PHASE D: Cross-origin iframe blocked: %s (%s)",
                                       frame_url, iframe_eval_err)
                        completions = getattr(capture_data, "capture_completions", {})
                        blocked = completions.get("cross_origin_iframes_blocked", [])
                        blocked.append(frame_url)
                        completions["cross_origin_iframes_blocked"] = blocked
                        capture_data.capture_completions = completions

                except Exception as frame_err:
                    logger.warning("PHASE D: Failed to capture iframe: %s", frame_err)
            if iframe_data:
                capture_data.user_context["iframe_contents"] = iframe_data
                logger.info("PHASE D: Captured %d iframe contents", len(iframe_data))
            if iframe_elements:
                capture_data.user_context["iframe_elements"] = iframe_elements
                logger.info("PHASE D: Extracted %d elements from iframes", len(iframe_elements))
            if not iframe_data:
                logger.info("PHASE D: No accessible iframes found")
        except Exception as e:
            logger.debug("PHASE D: Iframe capture failed: %s", e)

        try:
            cdp = await page.context.new_cdp_session(page)
            tree = await cdp.send("Accessibility.getFullAXTree")
            capture_data.a11y_tree = tree
            with open(os.path.join(captures_dir, "a11y_tree.json"), "w", encoding="utf-8") as f:
                json.dump(tree, f, default=str)
            logger.info("PHASE D: A11y tree (%d nodes)", len(tree.get("nodes", [])))
        except Exception as e:
            logger.warning("PHASE D: A11y tree failed: %s", e)

        # Page language + viewport meta
        try:
            capture_data.page_language = await page.evaluate("""
                () => ({
                    html_lang: document.documentElement.getAttribute('lang') || '',
                    xml_lang: document.documentElement.getAttribute('xml:lang') || '',
                })
            """)
        except Exception:
            pass  # best-effort — leave page_language unset if eval fails

        try:
            vp_meta = await page.evaluate("""
                () => { const m = document.querySelector('meta[name="viewport"]'); return m ? {content: m.content} : null; }
            """)
            if vp_meta:
                capture_data.viewport_meta = vp_meta
        except Exception:
            pass  # best-effort — leave viewport_meta unset if eval fails

        # ── Overlay widget detection (UserWay, AccessiBe, etc.) ──────
        try:
            from capture.web_capture import _capture_overlay_widgets
            await _capture_overlay_widgets(page, capture_data)
        except Exception:
            logger.exception("Overlay widget detection failed")

        # ── v1 Deterministic Extractions (ported from web_capture.py) ──
        logger.info("PHASE D: Running v1 deterministic extractions...")
        if progress_callback:
            await progress_callback("Phase D: Extracting computed styles, colors, language, landmarks...")
        await _run_v1_extractions(page, capture_data)
        logger.info("PHASE D: v1 extractions complete")

        # ── HTML attributes for media + form_fields (v1 had a separate
        # _extract_elements pass for these; v2's _run_v1_extractions
        # only does computed_styles / colors / language / landmarks,
        # so media autoplay / muted / loop / controls and form_field
        # in_fieldset / group_label / placeholder were missing on
        # every v2 run. Without these, a university's <video autoplay muted loop>
        # was reported with all four attrs inverted, and radios inside
        # real <fieldset> elements all reported in_fieldset=None.
        # The element_inventory mapper merges these in via
        # _merge_inventory_into_v1, so we just need to populate them
        # BEFORE the inventory mapper runs.)
        try:
            await _capture_html_media_and_form_attrs(page, capture_data)
        except Exception:
            logger.exception("HTML media/form attribute extraction failed; continuing without them")

        # ── axe-core run (deterministic ground truth for many SCs) ──
        # The v2 pipeline previously skipped this entirely, leaving
        # capture_data.axe_results = None. SC checks that consume axe
        # violations (contrast, labels, ARIA misuse, page structure)
        # were running blind. Calling _capture_axe here populates
        # capture_data.axe_results with axe-core 4.9 findings tagged
        # by WCAG criterion. Same data shape as the v1 pipeline; SC
        # checks read from capture_data.axe_results either way.
        try:
            from capture.web_capture import _capture_axe
            if progress_callback:
                await progress_callback("Phase D: axe-core deterministic scan...")
            await _capture_axe(page, captures_dir, capture_data)
        except Exception:
            logger.exception("axe-core scan failed; continuing without it")

        # ── HTML_CodeSniffer (Squiz Labs, BSD-3) ─────────────────────
        # Second deterministic ruleset, independent of axe. Catches
        # heading-skip patterns and label-association cases axe misses.
        # Same v2-bypass story as axe above: this gets selectively
        # imported into the v2 orchestrator so the legacy entry point
        # in web_capture.py stays consistent with what runs in prod.
        try:
            from capture.web_capture import _capture_htmlcs
            if progress_callback:
                await progress_callback("Phase D: HTML_CodeSniffer scan...")
            await _capture_htmlcs(page, captures_dir, capture_data)
        except Exception:
            logger.exception("HTML_CodeSniffer scan failed; continuing without it")

        # ── IBM Equal Access (Apache 2.0) ────────────────────────────
        # Third deterministic ruleset, strongest on ARIA validity
        # (aria-controls/aria-owns reference resolution, role-required-
        # attr checks, custom-widget patterns). Three tools agreeing
        # is very strong evidence; one alone is a candidate the judge
        # evaluates.
        try:
            from capture.web_capture import _capture_ibm_eac
            if progress_callback:
                await progress_callback("Phase D: IBM Equal Access scan...")
            await _capture_ibm_eac(page, captures_dir, capture_data)
        except Exception:
            logger.exception("IBM Equal Access scan failed; continuing without it")

        # ── ARIA reference + role validation ─────────────────────────
        # Was wired in v1 (web_capture.py:159) but never ported to v2;
        # capture_data.aria_issues was empty on every v2 run, leaving
        # the SC 4.1.2 / 1.3.1 prompts without deterministic ARIA spec
        # ground truth (broken aria-labelledby / aria-describedby /
        # aria-controls references and invalid role values). Same gap
        # pattern as the pixel_contrast / nontext_contrast fixes above.
        try:
            from capture.web_capture import _capture_aria_validation
            if progress_callback:
                await progress_callback("Phase D: ARIA reference + role validation...")
            _capture_aria_validation(capture_data)
        except Exception:
            logger.exception("ARIA validation failed; continuing without it")

        # ── K-means pixel-sampled contrast (per-element rendered ratio) ──
        # Was wired in v1 but never ported to v2; capture_data.pixel_contrast
        # was empty on every v2 run. Provides a non-CSS contrast measurement
        # by sampling rendered pixels in each element's bounding rect, used
        # by the judge as ground truth alongside ANDI cANDI for SC 1.4.3 /
        # 1.4.6 verification. Some pages produce uniform-region false
        # positives at 1.23:1 — ANDI's bg_image_present flag is the
        # authoritative signal; pixel_contrast is supporting evidence.
        try:
            from capture.web_capture import _capture_pixel_contrast
            if progress_callback:
                await progress_callback("Phase D: K-means pixel contrast sampling...")
            await _capture_pixel_contrast(page, capture_data, captures_dir)
        except Exception:
            logger.exception("Pixel contrast sampling failed; continuing without it")

        # ── Non-text contrast (UI components, focus indicators) ──────────
        # SC 1.4.11 ground truth: measures the contrast of UI component
        # boundaries (buttons, form controls, focus rings) against their
        # backgrounds. Was also v1-only; this run's empty
        # capture_data.nontext_contrast forced the visual AI to estimate
        # ratios from screenshots, producing the unsupported 2.36:1 / 2.7:1
        # claims observed on SC 1.4.11.
        try:
            from capture.web_capture import _capture_nontext_contrast
            if progress_callback:
                await progress_callback("Phase D: Non-text contrast (UI components)...")
            await _capture_nontext_contrast(page, capture_data)
        except Exception:
            logger.exception("Non-text contrast failed; continuing without it")

        # ── ANDI-style per-text-node contrast analysis ───────────────
        # SSA Section 508 methodology: walks every visible text node
        # (including SVG <text>), resolves the effective background by
        # walking up the DOM, and records the WCAG ratio + threshold.
        # Complements axe's element-level pass and the K-means pixel
        # sampler. Same pattern as the axe wire-up above.
        try:
            from capture.web_capture import _capture_andi_contrast
            if progress_callback:
                await progress_callback("Phase D: ANDI contrast scan...")
            await _capture_andi_contrast(page, captures_dir, capture_data)
        except Exception:
            logger.exception("ANDI contrast scan failed; continuing without it")

        # ── ANDI-style language audit (sANDI) ────────────────────────
        try:
            from capture.web_capture import _capture_andi_lang
            if progress_callback:
                await progress_callback("Phase D: ANDI language audit...")
            await _capture_andi_lang(page, captures_dir, capture_data)
        except Exception:
            logger.exception("ANDI lang scan failed; continuing without it")

        # ── ANDI-style hidden-content audit (hANDI) ──────────────────
        try:
            from capture.web_capture import _capture_andi_hidden
            if progress_callback:
                await progress_callback("Phase D: ANDI hidden-content audit...")
            await _capture_andi_hidden(page, captures_dir, capture_data)
        except Exception:
            logger.exception("ANDI hidden scan failed; continuing without it")

        # ── ANDI-style graphics audit (gANDI) ────────────────────────
        try:
            from capture.web_capture import _capture_andi_graphics
            if progress_callback:
                await progress_callback("Phase D: ANDI graphics audit...")
            await _capture_andi_graphics(page, captures_dir, capture_data)
        except Exception:
            logger.exception("ANDI graphics scan failed; continuing without it")

        # ── ANDI-style tables audit (tANDI) ──────────────────────────
        try:
            from capture.web_capture import _capture_andi_tables
            if progress_callback:
                await progress_callback("Phase D: ANDI tables audit...")
            await _capture_andi_tables(page, captures_dir, capture_data)
        except Exception:
            logger.exception("ANDI tables scan failed; continuing without it")

        # ── ANDI-style links/buttons audit (lANDI) ───────────────────
        try:
            from capture.web_capture import _capture_andi_interactive
            if progress_callback:
                await progress_callback("Phase D: ANDI interactive audit...")
            await _capture_andi_interactive(page, captures_dir, capture_data)
        except Exception:
            logger.exception("ANDI interactive scan failed; continuing without it")

        # ── VLM image analysis (caption + OCR + alt-text similarity) ──
        # Runs AFTER v1 extractions because it reads per-image
        # screenshot_path that those extractions populated. Feeds
        # SC 1.1.1 semantic alt verification and SC 1.4.5 images-of-text.
        try:
            from capture.web_capture import _capture_vlm_image_analysis
            if progress_callback:
                await progress_callback("Phase D: VLM image analysis (Gemma 26B)...")
            await _capture_vlm_image_analysis(capture_data)
        except Exception:
            logger.exception("VLM image analysis failed; continuing")

        capture_data.phase_timings["phase_d"] = round(time.monotonic() - phase_d_start, 1)
        logger.info("PHASE D COMPLETE: %.1fs", capture_data.phase_timings["phase_d"])
        # Persist the omnibus capture_data.json now so a crash before
        # interactive-tests begin doesn't lose every Phase D engine
        # result (axe / htmlcs / ibm_eac / 6× ANDI / pixel & non-text
        # contrast / aria / page_language / viewport / overlay /
        # background_images / ...). reload_capture_data picks this up.
        from capture.v2.state import save_capture_data
        save_capture_data(capture_data, captures_dir, after_label="Phase D")

        # ── Phase 1: Static Code AI ──────────────────────────────
        from capture.v2.phase1_code_analysis import run_phase1

        ai_client = None
        try:
            from analysis.api_client import AIClient
            ai_client = AIClient()
        except Exception as e:
            logger.warning("AI client unavailable: %s", e)

        inventory = await run_phase1(page, capture_data, ai_client, captures_dir, progress_callback)

        # ── Per-image crops for SC 1.1.1 / 1.4.5 / 4.1.2 ───────────
        # Runs AFTER Phase 1 because that's when capture_data.images is
        # populated by element_inventory.map_inventory_to_capture_data.
        # capture_data.background_images was already populated by Phase D
        # (v1_compat.py); both lists get cropped together here.
        #
        # Crops each <img> rect and CSS background-image rect out of
        # full_page.png so the visual AI / judge can see exactly what
        # each DOM image entry shows. Without this the model has to
        # visually correlate "image entry N in DOM" with "pixels at
        # coordinate X,Y in screenshot," which fails on cluttered pages
        # and can never work for CSS background-images (no <img> to
        # point at). See functions/image_crops.py docstring.
        try:
            from functions.image_crops import crop_images_from_full_page
            crops_dir = os.path.join(captures_dir, "image_crops")
            crop_count = crop_images_from_full_page(
                capture_data,
                capture_data.full_page_path or "",
                crops_dir,
            )
            logger.info("Per-image crops (post-Phase 1): %d", crop_count)
        except Exception:
            logger.exception("Per-image crop pass failed; continuing without crops")
        # Persist after Phase 1 + per-image crops so the inventory
        # (element_inventory / images / background_images / links /
        # form_fields / etc. as mapped onto capture_data) survives a
        # crash before Phase 2 starts.
        save_capture_data(capture_data, captures_dir, after_label="Phase 1")

        # ── Phase 2: Visual AI Explorer ──────────────────────────
        from capture.v2.phase2_visual_explorer import run_phase2
        await run_phase2(page, inventory, capture_data, ai_client, captures_dir, progress_callback)

        # ── Fix 7: Scan for ARIA live regions after all interactions ─
        logger.info("PHASE D+: Scanning for ARIA live regions and dynamic content...")
        try:
            live_regions = await page.evaluate("""
                () => {
                    const regions = [];
                    // Explicit live regions
                    document.querySelectorAll('[aria-live], [role="alert"], [role="status"], [role="log"], [role="timer"]').forEach(el => {
                        regions.push({
                            selector: el.id ? '#' + el.id : el.tagName.toLowerCase() + '[role="' + (el.getAttribute('role') || el.getAttribute('aria-live')) + '"]',
                            role: el.getAttribute('role') || '',
                            ariaLive: el.getAttribute('aria-live') || '',
                            text: el.textContent.trim(),
                            visible: el.offsetParent !== null,
                        });
                    });
                    return regions;
                }
            """)
            if live_regions:
                capture_data.user_context["live_regions"] = live_regions
                logger.info("PHASE D+: Found %d ARIA live regions", len(live_regions))
        except Exception as e:
            logger.debug("PHASE D+: Live region scan failed: %s", e)
        # Persist after Phase 2 + ARIA live-region scan so visual
        # exploration results / live-region context survive a crash
        # before interactive tests start.
        save_capture_data(capture_data, captures_dir, after_label="Phase 2")

        # ── Interactive tests (hover, text spacing, keyboard walkthrough) ──
        try:
            from capture.interactive_capture import run_interactive_tests
            int_start = time.monotonic()
            if progress_callback:
                await progress_callback("Running interactive tests...")
            await run_interactive_tests(page, capture_data, review_dir)
            capture_data.phase_timings["interactive"] = round(
                time.monotonic() - int_start, 1
            )
            logger.info(
                "INTERACTIVE TESTS COMPLETE: %.1fs",
                capture_data.phase_timings["interactive"],
            )
        except Exception:
            logger.exception("Interactive tests failed (non-fatal)")

        await browser.close()

    # ── Phase 3: Video Segments ──────────────────────────────────
    from capture.v2.phase3_video_segments import run_phase3

    async def _form_pause(name, reason, page_url):
        if progress_callback:
            await progress_callback(f"PAUSED: {reason}")
        from capture.v2.form_pause import wait_for_user_resume
        rid = os.path.basename(review_root) if review_root else ""
        await wait_for_user_resume(rid, timeout=300)

    await run_phase3(
        url, inventory, capture_data.exploration_results,
        capture_data, ai_client, captures_dir,
        progress_callback=progress_callback,
        form_pause_callback=_form_pause,
    )

    # Adopt the Phase 3 tab-walk file (recorded during the video walkthrough)
    # ONLY when it is at least as complete as the authoritative
    # interactive-capture (v1) walk already in memory. The Phase 3 walk is a
    # side effect of video recording: it is capped at 300, stops on the first
    # <body> hit, and can get stuck on a bot-challenge interstitial. Letting a
    # truncated Phase 3 walk overwrite a good v1 walk made the judge see "TAB
    # WALK: 5 reached" next to "TAB COVERAGE: 72 reached (104%)" and report a
    # bogus 0%-coverage keyboard failure (verified on a university run 2026-05-29: a
    # 5-stop Cloudflare Phase 3 walk clobbered a 72-stop v1 walk).
    twp = os.path.join(captures_dir, "tab_walk.json")
    if os.path.exists(twp):
        try:
            with open(twp) as f:
                file_tab_walk = json.load(f)
            existing = capture_data.tab_walk or []
            if file_tab_walk and len(file_tab_walk) >= len(existing):
                capture_data.tab_walk = file_tab_walk
                logger.info("Tab walk: %d elements (from Phase 3 file)", len(file_tab_walk))
            elif file_tab_walk:
                logger.info(
                    "Tab walk: keeping interactive-capture walk (%d stops); Phase 3 "
                    "file had only %d (likely truncated/interrupted) -- not overwriting",
                    len(existing), len(file_tab_walk),
                )
        except Exception:
            logger.warning("Failed to load tab_walk.json from %s", twp, exc_info=True)
    if capture_data.tab_walk:
        logger.info("Tab walk: %d elements total", len(capture_data.tab_walk))

    # Load keyboard traps — merge file data with interactive_capture data
    ktp = os.path.join(captures_dir, "keyboard_traps.json")
    if os.path.exists(ktp):
        try:
            with open(ktp) as f:
                file_traps = json.load(f)
            if file_traps:
                # Merge without duplicates (by selector)
                existing = {t.get("selector") for t in capture_data.keyboard_traps}
                for trap in file_traps:
                    if trap.get("selector") not in existing:
                        capture_data.keyboard_traps.append(trap)
                logger.info("Keyboard traps: %d detected", len(capture_data.keyboard_traps))
        except Exception:
            logger.debug("Failed to load keyboard_traps.json from %s", ktp, exc_info=True)

    # Derive focus_indicators from tab_walk data so that checks_2_4
    # (SC 2.4.7 Focus Visible) and checks_2_4_22 (SC 2.4.11/13) can
    # use the per-element focus CSS captured during tab walkthrough.
    # In v1 this was populated by _focus_indicator_screenshots; in v2
    # the tab_walk entries already contain has_visible_indicator,
    # indicator_type, and CSS properties.
    if capture_data.tab_walk and not capture_data.focus_indicators:
        seen_selectors: set[str] = set()
        focus_indicators = []
        for tw in capture_data.tab_walk:
            sel = tw.get("selector", "")
            if not sel or sel in seen_selectors:
                continue
            seen_selectors.add(sel)
            # Map tab_walk fields to the focus_indicators schema expected
            # by checks_2_4.py and checks_2_4_22.py
            css = tw.get("css", {})
            fi = {
                "selector": sel,
                "tag": tw.get("tag", ""),
                "role": tw.get("role", ""),
                "text": tw.get("text", ""),
                "has_visible_indicator": tw.get("has_visible_indicator"),
                "indicator_type": tw.get("indicator_type", "none"),
                "outline_style": css.get("outlineStyle", ""),
                "outline_width": css.get("outlineWidth", ""),
                "outline_color": css.get("outlineColor", ""),
                "box_shadow": css.get("boxShadow", ""),
                "border_color": css.get("borderColor", ""),
                "border_width": css.get("borderWidth", ""),
                "background_color": css.get("backgroundColor", ""),
                "rect": tw.get("rect", {}),
            }
            focus_indicators.append(fi)
        capture_data.focus_indicators = focus_indicators
        logger.info("Focus indicators: %d derived from tab walk", len(focus_indicators))
    # Persist after Phase 3 + tab_walk/keyboard_traps merge +
    # focus_indicators derivation so all keyboard data is on disk
    # before Phase 4 begins.
    save_capture_data(capture_data, captures_dir, after_label="Phase 3")

    # ── Phase 4: AT Simulation ───────────────────────────────────
    from capture.v2.phase4_at_simulation import run_phase4
    await run_phase4(capture_data, inventory, captures_dir, progress_callback)
    # Persist after Phase 4 so the AT-simulation cross-reference state
    # survives any subsequent failure (timing.json write, etc.). The
    # caller in app/orchestrators.py also writes capture_data.json after
    # this returns, but having a checkpoint here means a crash before
    # that outer save still leaves complete state on disk.
    save_capture_data(capture_data, captures_dir, after_label="Phase 4")

    # Cross-page aggregation (crawl/aggregator.py) consumes a
    # lightweight per-page structural_summary.json. v1's
    # capture_web_page writes it at the end of capture; v2 was missing
    # this call entirely, leaving site-crawl reviews unable to
    # aggregate page structure. Same writer as v1 — no v2-specific
    # divergence.
    try:
        from capture.web_capture import _save_structural_summary
        _save_structural_summary(captures_dir, capture_data)
    except Exception:
        logger.warning(
            "structural_summary.json save failed -- cross-page "
            "aggregation will be missing this page.", exc_info=True,
        )

    # ── Pipeline complete ────────────────────────────────────────
    total = time.monotonic() - pipeline_start
    capture_data.phase_timings["total"] = round(total, 1)

    with open(os.path.join(log_dir, "timing.json"), "w", encoding="utf-8") as f:
        json.dump(capture_data.phase_timings, f, indent=2)

    logger.info("=" * 70)
    logger.info("V2 CAPTURE COMPLETE: %.1fs", total)
    for p, t in sorted(capture_data.phase_timings.items()):
        if p != "total":
            logger.info("  %-10s %.1fs", p, t)
    logger.info("  Elements:    %d inventory, %d explored, %d video segments",
                len(inventory.elements), len(capture_data.exploration_results),
                len(capture_data.video_segments))
    logger.info("=" * 70)

    return capture_data
