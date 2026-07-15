"""Human-readable location labels for elements referenced in findings.

The judge's findings become actionable only if each referenced element
carries a label a developer can find on the page without opening
DevTools. Raw selectors like ``div:nth-of-type(9) > picture`` force the
reader to count siblings; phrasings like "the ninth section" are no
better. A good label says either "on the 'Find my degree program'
button" or "below the 'Stories of excellence' heading in the main
content area" -- it cites visible text, the nearest heading, and the
enclosing landmark so the reader can locate the element by sight.

This module is the single source of truth for that labelling:

  * ``LABELER_JS_BUNDLE`` -- a string of JavaScript that exposes
    ``window.__wcagLabeler.describe(el)`` inside the page. Each
    extractor that evaluates JS to build an inventory (images, links,
    headings, form fields, etc.) pastes this bundle at the top of its
    evaluation so every element in the inventory is emitted with a
    ``location_label`` dict attached. Injecting it once via
    ``page.add_init_script`` and referencing the global is also
    supported, for future navigations.

  * ``compose_location_label(data)`` -- a Python-side composer that
    turns the structured label dict into a human sentence. Used by
    the judge-prompt builder (``checks/base.py``) when rendering each
    element. Kept symmetric to the JS composer so cached captures
    can still be re-rendered by the Python side.

  * ``ensure_label_fields(items, warn_prefix)`` -- enrichment pass
    for lists where some entries may lack ``location_label`` (e.g. an
    older capture or a hand-constructed item). Logs missing entries
    so an empty output from the JS side is visible, not silent.

Logging policy: failures during JS evaluation are logged at WARNING
with the selector/role context so any drop is attributable. The
enrichment pass logs INFO with counts ("labelled 47 of 47 links")
so healthy runs are easy to confirm in the review log.
"""
from __future__ import annotations

import logging
from typing import Any, Iterable

logger = logging.getLogger(__name__)


# Maximum visible text length stored per element. Long enough to
# uniquely identify the element ("Click here to open the enrollment
# calculator for undergraduate programs at..."), short enough to keep
# the prompt size in check on pages with hundreds of elements.
VISIBLE_TEXT_MAX = 160

# ASCII-only composition to avoid mojibake in Windows consoles / older
# log handlers that default to cp1252.
_ABOVE_FOLD = "above the fold"
_BELOW_FOLD = "below the fold"


# ── JavaScript bundle ──────────────────────────────────────────────────────
# This is meant to be embedded inside any existing page.evaluate(() => {...})
# block: paste at the top of the IIFE, then call ``__wcagLabeler.describe(el)``
# per element.
#
# The ``describe`` function returns a dict shaped exactly like what
# ``compose_location_label`` expects -- keep the two sides in sync.

LABELER_JS_BUNDLE = r"""
    const __wcagLabeler = (() => {
        const MAX_TEXT = """ + str(VISIBLE_TEXT_MAX) + r""";
        const LANDMARK_SELECTOR = 'main, nav, header, footer, aside, section[aria-label], section[aria-labelledby], [role="main"], [role="navigation"], [role="banner"], [role="contentinfo"], [role="complementary"], [role="search"], [role="region"][aria-label], [role="region"][aria-labelledby]';
        const HEADING_TAGS = new Set(['H1','H2','H3','H4','H5','H6']);

        function trimText(s) {
            if (typeof s !== 'string') return '';
            const t = s.replace(/\s+/g, ' ').trim();
            return t.length > MAX_TEXT ? t.slice(0, MAX_TEXT - 1) + '…' : t;
        }

        function accessibleName(el) {
            if (!el) return '';
            const aria = el.getAttribute && el.getAttribute('aria-label');
            if (aria && aria.trim()) return trimText(aria);
            const labelledby = el.getAttribute && el.getAttribute('aria-labelledby');
            if (labelledby) {
                const parts = labelledby.split(/\s+/)
                    .map(id => document.getElementById(id))
                    .filter(Boolean)
                    .map(n => (n.textContent || '').trim())
                    .filter(Boolean);
                if (parts.length) return trimText(parts.join(' '));
            }
            if (el.tagName === 'IMG') {
                const alt = el.getAttribute('alt');
                // alt="" is deliberately decorative; alt missing is a bug.
                if (alt !== null && alt !== '') return trimText(alt);
                const title = el.getAttribute('title');
                if (title) return trimText(title);
                return '';
            }
            if (el.tagName === 'INPUT' || el.tagName === 'SELECT' || el.tagName === 'TEXTAREA') {
                if (el.id) {
                    const lbl = document.querySelector('label[for="' + CSS.escape(el.id) + '"]');
                    if (lbl) return trimText(lbl.textContent || '');
                }
                const wrapping = el.closest('label');
                if (wrapping) return trimText(wrapping.textContent || '');
                const placeholder = el.getAttribute('placeholder');
                if (placeholder) return trimText(placeholder);
                return '';
            }
            if (el.tagName === 'IFRAME') {
                const title = el.getAttribute('title');
                if (title) return trimText(title);
                const src = el.getAttribute('src') || '';
                if (src) return 'iframe from ' + src.slice(0, MAX_TEXT);
                return '';
            }
            return trimText(el.textContent || '');
        }

        function nearestLandmark(el) {
            let cur = el && el.parentElement;
            while (cur) {
                if (cur.matches && cur.matches(LANDMARK_SELECTOR)) {
                    const tag = cur.tagName.toLowerCase();
                    let role = cur.getAttribute('role') || '';
                    if (!role) {
                        // Map tag to its implicit ARIA role.
                        role = ({
                            main: 'main',
                            nav: 'navigation',
                            header: 'banner',
                            footer: 'contentinfo',
                            aside: 'complementary',
                            section: 'region',
                        })[tag] || tag;
                    }
                    const label = trimText(
                        cur.getAttribute('aria-label')
                        || (cur.getAttribute('aria-labelledby')
                            ? ((document.getElementById(cur.getAttribute('aria-labelledby')) || {}).textContent || '')
                            : '')
                        || ''
                    );
                    return { role: role, label: label, tag: tag };
                }
                cur = cur.parentElement;
            }
            return null;
        }

        function precedingHeading(el) {
            // Walk the document in reverse from ``el`` and return the
            // first heading we encounter. Uses a TreeWalker because
            // ``previousElementSibling`` only covers one level.
            if (!el || !document.body) return null;
            const walker = document.createTreeWalker(
                document.body,
                NodeFilter.SHOW_ELEMENT,
                {
                    acceptNode: (n) => HEADING_TAGS.has(n.tagName)
                        ? NodeFilter.FILTER_ACCEPT
                        : NodeFilter.FILTER_SKIP,
                }
            );
            let last = null;
            let node = walker.nextNode();
            while (node) {
                // DOCUMENT_POSITION_FOLLOWING means ``el`` comes AFTER
                // ``node`` -- keep node as a candidate. Stop as soon
                // as we pass ``el``.
                const pos = el.compareDocumentPosition(node);
                if (pos & Node.DOCUMENT_POSITION_FOLLOWING) break;
                if (pos & Node.DOCUMENT_POSITION_PRECEDING) last = node;
                // A node is neither preceding nor following the same
                // element; skip that case (shouldn't happen for
                // distinct headings in body).
                node = walker.nextNode();
            }
            if (!last) return null;
            return {
                level: parseInt(last.tagName.slice(1), 10),
                text: trimText(last.textContent || ''),
                id: last.id || '',
            };
        }

        function spatialHint(el) {
            if (!el || !el.getBoundingClientRect) return null;
            const r = el.getBoundingClientRect();
            const docH = Math.max(
                document.documentElement.scrollHeight,
                document.body ? document.body.scrollHeight : 0,
                window.innerHeight || 0
            );
            const pageY = r.top + (window.scrollY || 0);
            const viewportH = window.innerHeight || 1;
            let bucket;
            if (docH <= 0) bucket = 'unknown';
            else if (pageY < viewportH) bucket = 'top';
            else if (pageY < docH * 0.5) bucket = 'upper-middle';
            else if (pageY < docH * 0.85) bucket = 'lower-middle';
            else bucket = 'bottom';
            return {
                above_fold: pageY < viewportH,
                bucket: bucket,
                page_y: Math.round(pageY),
            };
        }

        function describe(el) {
            if (!el) {
                return {
                    visible_text: '',
                    accessible_name: '',
                    landmark: null,
                    preceding_heading: null,
                    section_hint: '',
                    spatial: null,
                    error: 'null element',
                };
            }
            try {
                // Section hint = the nearest ancestor whose class name
                // looks like a section wrapper (CMSes often use
                // ``block-*``, ``section-*``, etc.). Cheap, useful for
                // the overlay/parallax recognition the judge needs.
                let sectionHint = '';
                let cur = el.parentElement;
                let depth = 0;
                while (cur && depth < 6) {
                    const cls = (cur.className && typeof cur.className === 'string')
                        ? cur.className : '';
                    if (/\b(section|block-|layout__|hero|carousel|overlay|parallax|footer|header)/i.test(cls)) {
                        sectionHint = cls.split(/\s+/).slice(0, 3).join(' ');
                        break;
                    }
                    cur = cur.parentElement;
                    depth++;
                }
                return {
                    visible_text: trimText(el.textContent || ''),
                    accessible_name: accessibleName(el),
                    landmark: nearestLandmark(el),
                    preceding_heading: precedingHeading(el),
                    section_hint: sectionHint,
                    spatial: spatialHint(el),
                    error: '',
                };
            } catch (err) {
                return {
                    visible_text: '',
                    accessible_name: '',
                    landmark: null,
                    preceding_heading: null,
                    section_hint: '',
                    spatial: null,
                    error: String(err && err.message || err),
                };
            }
        }

        return { describe: describe, MAX_TEXT: MAX_TEXT };
    })();
"""


# ── Python-side composer ───────────────────────────────────────────────────

def compose_location_label(data: dict | None) -> str:
    """Turn a structured label dict (from ``__wcagLabeler.describe``) into
    a single human-readable sentence.

    The composition prefers the most specific identifier available:

      1. An accessible name or visible text (quoted) — "the 'Read more'
         link"
      2. The nearest heading — "below the 'Find my degree program'
         heading"
      3. The enclosing landmark — "in the main navigation"
      4. A spatial hint — "above the fold" / "in the bottom section"

    The output is always a single clause; callers concatenate it into
    their own sentence (e.g. ``f"{element_kind} {location_label}"``).
    Empty inputs return an empty string so callers can fall back to
    their current phrasing without a None check.
    """
    if not isinstance(data, dict):
        return ""

    parts: list[str] = []

    name = (data.get("accessible_name") or "").strip()
    text = (data.get("visible_text") or "").strip()
    label_text = name or text
    if label_text:
        parts.append(f"labelled \"{label_text}\"")

    heading = data.get("preceding_heading")
    if isinstance(heading, dict) and (heading.get("text") or "").strip():
        h_text = heading["text"].strip()
        h_level = heading.get("level") or "?"
        parts.append(f"under the <h{h_level}> heading \"{h_text}\"")

    landmark = data.get("landmark")
    if isinstance(landmark, dict):
        role = (landmark.get("role") or "").strip() or (landmark.get("tag") or "").strip()
        label = (landmark.get("label") or "").strip()
        if role:
            if label:
                parts.append(f"in the <{role} aria-label=\"{label}\">")
            else:
                parts.append(f"in the <{role}> landmark")

    spatial = data.get("spatial")
    if isinstance(spatial, dict):
        bucket = (spatial.get("bucket") or "").strip()
        if spatial.get("above_fold"):
            parts.append(_ABOVE_FOLD)
        elif bucket == "bottom":
            parts.append(_BELOW_FOLD + " (bottom of page)")
        elif bucket:
            parts.append(f"in the {bucket} of the page")

    return ", ".join(parts)


# ── Enrichment helper ──────────────────────────────────────────────────────

def ensure_label_fields(
    items: Iterable[dict[str, Any]],
    *,
    warn_prefix: str,
    required: bool = False,
) -> int:
    """Ensure every item has a ``location_label`` string derived from its
    ``location`` dict.

    Items lacking a structured ``location`` dict keep their original
    ``location_label`` (or get an empty one). Logs a WARNING with
    ``warn_prefix`` and the item identifier when ``required=True`` and
    a label is missing -- that lets callers mark specific inventories
    (images, links) as must-be-labelled without blowing up the run.

    Returns the number of labels composed on this pass, so the caller
    can log "labelled N of M" at the end of its extraction step.
    """
    composed = 0
    missing: list[str] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        if item.get("location_label"):
            continue
        loc = item.get("location")
        if isinstance(loc, dict):
            label = compose_location_label(loc)
            if label:
                item["location_label"] = label
                composed += 1
                continue
        if required:
            ident = (
                item.get("selector")
                or item.get("src")
                or item.get("href")
                or item.get("text")
                or "<unidentified>"
            )
            missing.append(str(ident))
        item["location_label"] = item.get("location_label", "")
    if missing:
        logger.warning(
            "%s: %d item(s) missing location_label, items: %s",
            warn_prefix, len(missing), missing,
        )
    return composed
