"""V1-compatible deterministic extractions.

Runs all deterministic extractions ported from v1 web_capture.py.
These populate CaptureData fields that the check modules expect.
100% accurate — no AI dependency.
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


async def _run_v1_extractions(page, capture_data) -> None:
    """Run all deterministic extractions ported from v1 web_capture.py.

    These populate CaptureData fields that the check modules expect.
    100% accurate — no AI dependency.
    """

    # ── Computed styles (colors, fonts, opacity) for contrast analysis ──
    try:
        capture_data.computed_styles = await page.evaluate("""() => {
            const results = [];
            const els = document.querySelectorAll('body, body *');
            for (const el of els) {
                const cs = window.getComputedStyle(el);
                // Use innerText (visible text) not textContent (includes
                // SR-only / off-screen content). SR-only wrappers have
                // textContent but empty innerText — sampling them gives
                // garbage "contrast" between two background shades.
                const visible = (el.innerText || '').trim();
                if (!visible) continue;
                if (cs.display === 'none' || cs.visibility === 'hidden') continue;
                if (parseFloat(cs.opacity) === 0) continue;
                const rect = el.getBoundingClientRect();
                if (rect.width < 2 || rect.height < 2) continue;
                let bgColor = cs.backgroundColor;
                let bgEl = el;
                let reachedRoot = false;
                while (bgColor === 'rgba(0, 0, 0, 0)' || bgColor === 'transparent') {
                    bgEl = bgEl.parentElement;
                    if (!bgEl) { bgColor = 'rgb(255, 255, 255)'; reachedRoot = true; break; }
                    bgColor = window.getComputedStyle(bgEl).backgroundColor;
                }
                // Ancestor-chain backgroundImage check — walks only while
                // bg-color was still transparent (stops where we committed
                // to a background). Also detect if any ancestor in the
                // chain had a backgroundImage even after we found a color.
                let hasBgImage = false;
                let checkEl = el;
                while (checkEl) {
                    const bgImg = window.getComputedStyle(checkEl).backgroundImage;
                    if (bgImg && bgImg !== 'none') { hasBgImage = true; break; }
                    checkEl = checkEl.parentElement;
                }
                // If the bg-color walk ran all the way to the document root
                // without finding an explicit bg-color, we cannot trust the
                // fallback white. Gradients, positioned image siblings,
                // ::before pseudo-backgrounds, and mix-blend layers are
                // invisible to this walk. Mark as "unknown background" so
                // downstream checks emit INFO, not a false HIGH.
                if (reachedRoot) { hasBgImage = true; }
                let effectiveOpacity = 1;
                let opEl = el;
                while (opEl) {
                    effectiveOpacity *= parseFloat(window.getComputedStyle(opEl).opacity) || 1;
                    opEl = opEl.parentElement;
                }
                results.push({
                    tag: el.tagName.toLowerCase(), text: visible,
                    color: cs.color, backgroundColor: bgColor,
                    fontSize: cs.fontSize, fontWeight: cs.fontWeight,
                    hasBgImage: hasBgImage, effectiveOpacity: effectiveOpacity,
                });
            }
            return results;
        }""")
        logger.info("PHASE D: Computed styles: %d elements", len(capture_data.computed_styles or []))
    except Exception as e:
        logger.warning("PHASE D: Computed styles extraction failed: %s", e)

    # ── Unique color pairs for contrast checking ──
    try:
        capture_data.colors = await page.evaluate("""() => {
            // WCAG relative luminance + contrast ratio (computed in browser)
            function sRGBtoLinear(c) {
                c = c / 255;
                return c <= 0.04045 ? c / 12.92 : Math.pow((c + 0.055) / 1.055, 2.4);
            }
            function luminance(r, g, b) {
                return 0.2126 * sRGBtoLinear(r) + 0.7152 * sRGBtoLinear(g) + 0.0722 * sRGBtoLinear(b);
            }
            function contrastRatio(l1, l2) {
                const lighter = Math.max(l1, l2);
                const darker = Math.min(l1, l2);
                return (lighter + 0.05) / (darker + 0.05);
            }
            function parseRGB(str) {
                const m = str.match(/rgba?\\(\\s*(\\d+)\\s*,\\s*(\\d+)\\s*,\\s*(\\d+)/);
                if (m) return [parseInt(m[1]), parseInt(m[2]), parseInt(m[3])];
                return null;
            }

            const results = [];
            const seen = new Set();
            const els = document.querySelectorAll('body, body *');
            for (const el of els) {
                // Prefer innerText (visible) over textContent (includes
                // SR-only). See computed_styles gatherer for rationale.
                const visible = (el.innerText || '').trim();
                if (!visible) continue;
                // Skip non-leaf elements (check direct text nodes only)
                if (el.children.length > 3 && visible.length > 200) continue;
                const cs = window.getComputedStyle(el);
                if (cs.display === 'none' || cs.visibility === 'hidden') continue;
                if (parseFloat(cs.opacity) === 0) continue;
                const rect = el.getBoundingClientRect();
                if (rect.width < 2 || rect.height < 2) continue;

                const fg = cs.color;
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
                // Walk-reached-root → treat as unknown backdrop. See
                // computed_styles gatherer comment for rationale.
                if (reachedRoot) { hasBgImage = true; }

                // Build a selector
                let selector = el.tagName.toLowerCase();
                if (el.id) selector = '#' + el.id;
                else if (el.className && typeof el.className === 'string')
                    selector = el.tagName.toLowerCase() + '.' + el.className.trim().split(/\\s+/)[0];

                // Compute contrast ratio
                const fgRGB = parseRGB(fg);
                const bgRGB = parseRGB(bg);
                let ratio = null;
                if (fgRGB && bgRGB) {
                    const fgLum = luminance(...fgRGB);
                    const bgLum = luminance(...bgRGB);
                    ratio = Math.round(contrastRatio(fgLum, bgLum) * 100) / 100;
                }

                // Deduplicate by color pair but keep first selector
                const key = fg + '|' + bg;
                if (seen.has(key)) continue;
                seen.add(key);

                results.push({
                    color: fg, foreground: fg,
                    background_color: bg, background: bg,
                    fontSize: cs.fontSize, fontWeight: cs.fontWeight,
                    font_size: cs.fontSize, font_weight: cs.fontWeight,
                    sampleText: visible, text: visible,
                    tag: el.tagName.toLowerCase(),
                    selector: selector,
                    contrast_ratio: ratio,
                    hasBgImage: hasBgImage,
                    effectiveOpacity: 1.0,
                });
            }
            return results;
        }""")
        logger.info("PHASE D: Color pairs: %d unique", len(capture_data.colors or []))
    except Exception as e:
        logger.warning("PHASE D: Color extraction failed: %s", e)

    # ── Page language (SC 3.1.1, 3.1.2) ──
    try:
        capture_data.page_language = await page.evaluate("""() => {
            const html = document.documentElement;
            const lang = html.getAttribute('lang') || '';
            const xmlLang = html.getAttribute('xml:lang') || '';
            const contentLang = document.querySelector('meta[http-equiv="content-language"]');
            const metaLang = contentLang ? contentLang.getAttribute('content') || '' : '';
            const langParts = [];
            document.querySelectorAll('[lang]').forEach(el => {
                if (el === html) return;
                langParts.push({
                    tag: el.tagName.toLowerCase(),
                    lang: el.getAttribute('lang') || '',
                    text: (el.textContent || '').trim(),
                });
            });
            return {
                html_lang: lang, xml_lang: xmlLang,
                meta_content_language: metaLang,
                has_lang: lang !== '',
                lang_valid: /^[a-zA-Z]{2,3}(-[a-zA-Z0-9]+)*$/.test(lang),
                lang_parts: langParts,
            };
        }""")
        logger.info("PHASE D: Page language: %s (valid=%s, %d lang parts)",
                    (capture_data.page_language or {}).get("html_lang", "?"),
                    (capture_data.page_language or {}).get("lang_valid", "?"),
                    len((capture_data.page_language or {}).get("lang_parts", [])))
    except Exception as e:
        logger.warning("PHASE D: Page language extraction failed: %s", e)

    # ── Background images ──
    try:
        capture_data.background_images = await page.evaluate("""() => {
            const results = [];
            const els = document.querySelectorAll('body, body *');
            for (const el of els) {
                const cs = window.getComputedStyle(el);
                const bg = cs.backgroundImage;
                if (bg && bg !== 'none') {
                    let sel = el.tagName.toLowerCase();
                    if (el.id) sel = '#' + el.id;
                    // Rect is required for the per-image cropper to bind
                    // a CSS background-image to a visual region of the
                    // page. SC 1.4.5 (Images of Text) and SC 1.1.1
                    // judgment depend on the AI being able to see what
                    // the background image actually shows.
                    const r = el.getBoundingClientRect();
                    results.push({
                        selector: sel, tag: el.tagName.toLowerCase(),
                        backgroundImage: bg, role: el.getAttribute('role') || '',
                        ariaLabel: el.getAttribute('aria-label') || '',
                        text_content: (el.textContent || '').trim(),
                        rect: {
                            x: r.x + window.scrollX,
                            y: r.y + window.scrollY,
                            width: r.width, height: r.height,
                        },
                    });
                }
            }
            return results;
        }""")
        logger.info("PHASE D: Background images: %d", len(capture_data.background_images or []))
    except Exception as e:
        logger.warning("PHASE D: Background image extraction failed: %s", e)

    # ── CAPTCHAs ──
    try:
        capture_data.captchas = await page.evaluate("""() => {
            const selectors = [
                'iframe[src*="recaptcha"]', 'iframe[src*="hcaptcha"]',
                'iframe[src*="captcha"]', '[class*="captcha"]', '[id*="captcha"]',
                '[class*="CAPTCHA"]', '[id*="CAPTCHA"]', '.g-recaptcha', '.h-captcha',
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
        logger.info("PHASE D: CAPTCHAs: %d", len(capture_data.captchas or []))
    except Exception as e:
        logger.warning("PHASE D: CAPTCHA extraction failed: %s", e)

    # ── Skip links with target verification ──
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
                const href = a.getAttribute('href') || '';
                const targetId = href.replace('#', '');
                const isFirstLink = a === document.querySelector('a');
                const pointsToMain = /main|content|body|wrapper/i.test(targetId);
                if (isSkipLink || (isFirstLink && pointsToMain)) {
                    const target = targetId ? document.getElementById(targetId) : null;
                    results.push({
                        text: text, href: href,
                        targetExists: target !== null,
                        targetTag: target ? target.tagName.toLowerCase() : null,
                        targetRole: target ? (target.getAttribute('role') || '') : null,
                        selector: a.id ? '#' + a.id : 'a[href="' + href + '"]',
                        rect: (() => { const r = a.getBoundingClientRect(); return {x: r.x, y: r.y, width: r.width, height: r.height}; })(),
                    });
                }
            }
            return results;
        }""")
        logger.info("PHASE D: Skip links: %d", len(capture_data.skip_links or []))
    except Exception as e:
        logger.warning("PHASE D: Skip link extraction failed: %s", e)

    # ── Pseudo-element content (::before, ::after) ──
    try:
        capture_data.pseudo_elements = await page.evaluate("""() => {
            const results = [];
            const els = document.querySelectorAll('body *');
            for (const el of els) {
                for (const pseudo of ['::before', '::after']) {
                    const cs = window.getComputedStyle(el, pseudo === '::before' ? ':before' : ':after');
                    const content = cs.content;
                    if (content && content !== 'none' && content !== 'normal' && content !== '""' && content !== "''") {
                        let textContent = content.replace(/^["']|["']$/g, '');
                        if (textContent.length <= 1) continue;
                        let selector = el.tagName.toLowerCase();
                        if (el.id) selector = '#' + el.id;
                        results.push({
                            selector: selector, pseudo: pseudo,
                            content: textContent,
                            display: cs.display, visibility: cs.visibility,
                            ariaHidden: el.getAttribute('aria-hidden') || '',
                        });
                    }
                }
            }
            return results;
        }""")
        logger.info("PHASE D: Pseudo-elements: %d", len(capture_data.pseudo_elements or []))
    except Exception as e:
        logger.warning("PHASE D: Pseudo-element extraction failed: %s", e)

    # ── Overflow at 200% zoom (SC 1.4.4) ──
    # The correct test for SC 1.4.4 is whether text content is CLIPPED
    # (hidden by overflow:hidden/clip ancestors) at 200% zoom — NOT
    # whether elements extend past the viewport (that's normal scrolling).
    try:
        await page.evaluate("document.body.style.zoom = '2'")
        await page.wait_for_timeout(800)
        from functions.js_helpers import GET_SELECTOR_JS as _SEL_OV
        capture_data.overflow_200pct = await page.evaluate(r"""() => {""" + _SEL_OV + r"""
            // sr-only / visually-hidden detection — avoids 1px-clipped
            // accessibility links being reported as 200% overflow
            // (their 2x2 rect always overflows because that IS their
            // entire footprint by design).
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

            const results = [];
            const els = document.querySelectorAll('body *');
            for (const el of els) {
                if (isVisuallyHidden(el)) continue;
                const text = el.textContent?.trim();
                if (!text || text.length < 2) continue;
                if (el.children.length > 5) continue;

                const cs = window.getComputedStyle(el);
                if (cs.display === 'none' || cs.visibility === 'hidden') continue;

                // Skip elements whose own bounding box is sr-only sized
                // even though their text overflows -- those are the
                // visually-hidden-focusable links/spans that always
                // "overflow" by design.
                const ownRect = el.getBoundingClientRect();
                if (ownRect.width <= 2 && ownRect.height <= 2) continue;

                // Check if this element's content overflows its own box
                const isClipped = (
                    (el.scrollWidth > el.clientWidth + 2 &&
                     (cs.overflowX === 'hidden' || cs.overflowX === 'clip')) ||
                    (el.scrollHeight > el.clientHeight + 2 &&
                     (cs.overflowY === 'hidden' || cs.overflowY === 'clip'))
                );

                if (!isClipped) {
                    let ancestor = el.parentElement;
                    let ancestorClips = false;
                    while (ancestor && ancestor !== document.body) {
                        const acs = window.getComputedStyle(ancestor);
                        const aClipsX = (acs.overflowX === 'hidden' || acs.overflowX === 'clip');
                        const aClipsY = (acs.overflowY === 'hidden' || acs.overflowY === 'clip');
                        if (aClipsX || aClipsY) {
                            const aRect = ancestor.getBoundingClientRect();
                            const elRect = el.getBoundingClientRect();
                            if ((aClipsX && elRect.right > aRect.right + 2) ||
                                (aClipsY && elRect.bottom > aRect.bottom + 2)) {
                                ancestorClips = true;
                                break;
                            }
                        }
                        ancestor = ancestor.parentElement;
                    }
                    if (!ancestorClips) continue;
                }

                const rect = el.getBoundingClientRect();
                results.push({
                    tag: el.tagName.toLowerCase(),
                    selector: getSelector(el),
                    text: text,
                    rect: {x: rect.x, y: rect.y, width: rect.width, height: rect.height},
                    overflowX: el.scrollWidth > el.clientWidth + 2,
                    overflowY: el.scrollHeight > el.clientHeight + 2,
                    clippedBySelf: (
                        (el.scrollWidth > el.clientWidth + 2 &&
                         (cs.overflowX === 'hidden' || cs.overflowX === 'clip')) ||
                        (el.scrollHeight > el.clientHeight + 2 &&
                         (cs.overflowY === 'hidden' || cs.overflowY === 'clip'))
                    ),
                });
            }
            return results;
        }""")
        await page.evaluate("document.body.style.zoom = '1'")
        await page.wait_for_timeout(300)
        logger.info("PHASE D: Clipped content at 200%%: %d elements", len(capture_data.overflow_200pct or []))
    except Exception as e:
        logger.warning("PHASE D: 200%% overflow detection failed: %s", e)
        try:
            await page.evaluate("document.body.style.zoom = '1'")
        except Exception:
            pass  # cleanup — best-effort zoom reset, page state may be unrecoverable

    # ── Dynamic content detection ──
    try:
        capture_data.dynamic_content = await page.evaluate("""() => {
            return {
                hasAutoplayVideo: document.querySelectorAll('video[autoplay]').length > 0,
                hasAutoplayAudio: document.querySelectorAll('audio[autoplay]').length > 0,
                hasAnimations: document.getAnimations ? document.getAnimations().length > 0 : false,
                hasMarquee: document.querySelectorAll('marquee, [class*="carousel"], [class*="slider"], [class*="rotate"]').length > 0,
                hasAutoRefresh: !!document.querySelector('meta[http-equiv="refresh"]'),
            };
        }""")
        logger.info("PHASE D: Dynamic content: %s", capture_data.dynamic_content)
    except Exception as e:
        logger.warning("PHASE D: Dynamic content detection failed: %s", e)

    # ── Script content extraction (needed by ~15 checks for JS analysis) ──
    try:
        from capture.web_capture import _capture_script_content
        await _capture_script_content(page, capture_data)
        logger.info("PHASE D: Script content: %d chars",
                     len(capture_data.script_content or ""))
    except Exception as e:
        logger.warning("PHASE D: Script content extraction failed: %s", e)

    # ── 320px overflow detection (SC 1.4.10 Reflow) ──
    try:
        from capture.web_capture import _detect_overflow_narrow
        await _detect_overflow_narrow(page, page.context, capture_data.url, capture_data)
        logger.info("PHASE D: 320px overflow: %d elements, hscroll=%s",
                     len(capture_data.overflow_320px or []),
                     capture_data.horizontal_scroll_320)
    except Exception as e:
        logger.warning("PHASE D: 320px overflow detection failed: %s", e)
