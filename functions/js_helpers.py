"""Shared JS snippets used inside Playwright page.evaluate() calls.

The pipeline has 20+ separate JS extractors (capture/web_capture.py,
capture/interactive_capture.py, capture/v2/__init__.py,
capture/v2/phase1_code_analysis.py) that historically each computed
``selector`` differently — some fell back to ``el.tagName.toLowerCase()``
when the element had no id, others used ``tag + '.' + first-class``.
Both produce non-unique selectors: ASU's homepage has 22 ``<a>`` elements
without ids that all collapsed to selector="a" in focus_contrast,
making the AI prompt's contrast lines indistinguishable.

This module exports the canonical algorithm. Every JS evaluate() that
emits an element selector should prepend ``GET_SELECTOR_JS`` and use
``getSelector(el)`` so the resulting strings collide on the same key as
the inventory's selectors and the AI sees one entry per real element.
"""

GET_SELECTOR_JS = r"""
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
    const selector = parts.join(' > ');
    try {
        if (document.querySelectorAll(selector).length === 1) return selector;
        const fullParts = [];
        let node = el;
        while (node && node !== document.body && node !== document.documentElement) {
            let s = node.tagName.toLowerCase();
            if (node.id) {
                fullParts.unshift('#' + CSS.escape(node.id));
                break;
            }
            if (node.parentElement) {
                const sibs = Array.from(node.parentElement.children)
                    .filter(c => c.tagName === node.tagName);
                if (sibs.length > 1) {
                    s += ':nth-of-type(' + (sibs.indexOf(node) + 1) + ')';
                }
            }
            fullParts.unshift(s);
            node = node.parentElement;
        }
        return fullParts.join(' > ');
    } catch (e) { return selector; }
}
"""


EFFECTIVE_BG_JS = r"""
// Resolve the rendered background visible BEHIND an element.
//
// An element's own backgroundColor is often rgba(0,0,0,0) (transparent --
// the default for <a>, <button>, <span>, <li>, etc.). Computing
// outline / border / focus-indicator contrast against the transparent
// own-bg yields garbage (e.g. focus_contrast measured 1.1:1 for
// example.com's <a> outline because parse_rgb('rgba(0,0,0,0)') →
// rgb(0,0,0), which is not what the user actually sees).
//
// The user sees the first non-transparent ancestor. Walk up the parent
// chain until we find one. Falls back to white -- the browser default
// page background.
//
// Use this whenever a JS extractor needs to compute contrast of a
// boundary / outline / focus indicator against the page background.
function effectiveBg(el) {
    let cur = el;
    while (cur) {
        const bg = window.getComputedStyle(cur).backgroundColor;
        if (bg && bg !== 'rgba(0, 0, 0, 0)' && bg !== 'transparent') {
            return bg;
        }
        cur = cur.parentElement;
    }
    return 'rgb(255, 255, 255)';
}
"""
