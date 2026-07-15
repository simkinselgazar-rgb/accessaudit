"""Shadow DOM element extraction for Playwright pages.

Modern web components (Angular Material, Salesforce Lightning,
custom elements) render content inside shadow roots that are
invisible to normal DOM queries. This module recursively walks
all shadow roots and extracts element data so every SC check
sees the full page content.
"""
from __future__ import annotations
import logging
from typing import Any

logger = logging.getLogger(__name__)

_EXTRACT_JS = r"""
() => {
    const results = [];

    function buildSelector(el) {
        if (el.id) return '#' + CSS.escape(el.id);
        const tag = el.tagName.toLowerCase();
        const parent = el.parentNode;
        if (!parent || parent === document || parent === document.documentElement) {
            return tag;
        }
        const siblings = Array.from(parent.children).filter(
            c => c.tagName === el.tagName
        );
        if (siblings.length === 1) return tag;
        const idx = siblings.indexOf(el) + 1;
        return tag + ':nth-of-type(' + idx + ')';
    }

    function selectorPath(el) {
        const parts = [];
        let cur = el;
        while (cur && cur !== document.documentElement) {
            parts.unshift(buildSelector(cur));
            if (cur.parentNode instanceof ShadowRoot) break;
            cur = cur.parentNode;
        }
        return parts.join(' > ');
    }

    function hostSelector(host) {
        const parts = [];
        let cur = host;
        while (cur && cur !== document.documentElement) {
            parts.unshift(buildSelector(cur));
            cur = cur.parentNode;
            if (cur instanceof ShadowRoot) {
                cur = cur.host;
            }
        }
        return parts.join(' > ');
    }

    function getRect(el) {
        try {
            const r = el.getBoundingClientRect();
            return {x: r.x, y: r.y, width: r.width, height: r.height};
        } catch (_) {
            return {x: 0, y: 0, width: 0, height: 0};
        }
    }

    function getComputedProps(el) {
        try {
            const cs = window.getComputedStyle(el);
            return {
                color: cs.color,
                backgroundColor: cs.backgroundColor,
                borderColor: cs.borderColor,
                outline: cs.outline,
                fontSize: cs.fontSize,
                fontWeight: cs.fontWeight,
                display: cs.display,
                visibility: cs.visibility,
                opacity: cs.opacity,
            };
        } catch (_) {
            return {};
        }
    }

    function getAriaAttrs(el) {
        const aria = {};
        for (const attr of el.attributes || []) {
            if (attr.name.startsWith('aria-')) {
                aria[attr.name] = attr.value;
            }
        }
        return aria;
    }

    function extractElement(el, hostSel, depth) {
        const tag = el.tagName.toLowerCase();
        const role = el.getAttribute('role') || '';
        const text = (el.textContent || '').trim();
        const aria = getAriaAttrs(el);

        const innerSelector = selectorPath(el);
        const fullSelector = hostSel + ' >>> ' + innerSelector;

        const rect = getRect(el);
        const styles = getComputedProps(el);

        const data = {
            tag: tag,
            role: role,
            text: text,
            accessible_name: el.ariaLabel || aria['aria-label'] || el.getAttribute('alt') || el.getAttribute('title') || '',
            aria: aria,
            selector: fullSelector,
            shadow_host: hostSel,
            shadow_depth: depth,
            rect: rect,
            computed_styles: styles,
            visible: styles.display !== 'none' && styles.visibility !== 'hidden' && parseFloat(styles.opacity || '1') > 0,
            id: el.id || '',
            name: el.getAttribute('name') || '',
        };

        if (tag === 'a' || role === 'link') {
            data.href = el.getAttribute('href') || '';
            data.target = el.getAttribute('target') || '';
            data.title = el.getAttribute('title') || '';
        }

        if (tag === 'img' || role === 'img' || role === 'image') {
            data.src = el.getAttribute('src') || '';
            data.alt = el.getAttribute('alt') || '';
        }

        if (tag === 'input' || tag === 'select' || tag === 'textarea') {
            data.input_type = el.getAttribute('type') || '';
            data.required = el.required || el.getAttribute('aria-required') === 'true';
            data.placeholder = el.getAttribute('placeholder') || '';
            data.autocomplete = el.getAttribute('autocomplete') || '';
            data.label = '';
            if (el.id) {
                const lbl = (el.getRootNode() || document).querySelector('label[for="' + CSS.escape(el.id) + '"]');
                if (lbl) data.label = (lbl.textContent || '').trim();
            }
        }

        if (tag === 'button' || role === 'button' || (tag === 'input' && ['button','submit','reset'].includes(data.input_type))) {
            data.input_type = data.input_type || '';
        }

        if (/^h[1-6]$/.test(tag) || role === 'heading') {
            const match = tag.match(/^h(\d)$/);
            data.level = match ? parseInt(match[1]) : parseInt(el.getAttribute('aria-level') || '2');
        }

        if (tag === 'video' || tag === 'audio') {
            data.src = el.getAttribute('src') || '';
            data.autoplay = el.hasAttribute('autoplay');
            data.loop = el.hasAttribute('loop');
            data.muted = el.hasAttribute('muted');
            data.controls = el.hasAttribute('controls');
            const tracks = [];
            el.querySelectorAll('track').forEach(t => {
                tracks.push({
                    kind: t.getAttribute('kind') || '',
                    src: t.getAttribute('src') || '',
                    srclang: t.getAttribute('srclang') || '',
                    label: t.getAttribute('label') || '',
                });
            });
            data.tracks = tracks;
        }

        if (tag === 'table' || role === 'table') {
            data.caption = '';
            const cap = el.querySelector('caption');
            if (cap) data.caption = (cap.textContent || '').trim();
        }

        if (tag === 'iframe') {
            data.src = el.getAttribute('src') || '';
            data.title = el.getAttribute('title') || '';
        }

        return data;
    }

    function walkShadow(host, depth) {
        const root = host.shadowRoot;
        if (!root) return;

        const hostSel = hostSelector(host);
        const children = root.querySelectorAll('*');

        for (const child of children) {
            results.push(extractElement(child, hostSel, depth));
            if (child.shadowRoot) {
                walkShadow(child, depth + 1);
            }
        }
    }

    const allElements = document.querySelectorAll('*');
    for (const el of allElements) {
        if (el.shadowRoot) {
            walkShadow(el, 1);
        }
    }

    return results;
}
"""


async def extract_shadow_elements(page) -> list[dict]:
    """Recursively extract all elements inside shadow DOM roots.

    Walks every element on the page, checks for shadowRoot,
    and if found, extracts all child elements with their:
    - tag, role, text, accessible name
    - aria-* attributes (label, labelledby, describedby, expanded, checked, etc.)
    - computed styles (color, backgroundColor, borderColor, outline)
    - bounding rect
    - form field properties (type, required, placeholder, autocomplete)
    - shadow host selector (for tracing back to the light DOM)

    Returns a flat list of element dicts, each marked with
    shadow_host so callers know they came from shadow DOM.
    """
    try:
        elements = await page.evaluate(_EXTRACT_JS)
        if not isinstance(elements, list):
            elements = []
    except Exception as exc:
        logger.warning("Shadow DOM extraction failed: %s", exc)
        return []

    logger.info("Shadow DOM extraction: found %d elements across all shadow roots", len(elements))
    return elements


def _classify_element(el: dict) -> str:
    """Map a shadow element to a CaptureData field category."""
    tag = (el.get("tag") or "").lower()
    role = (el.get("role") or "").lower()
    input_type = (el.get("input_type") or "").lower()

    if tag in ("h1", "h2", "h3", "h4", "h5", "h6") or role == "heading":
        return "heading"
    if tag == "a" or role == "link":
        return "link"
    if tag == "img" or role in ("img", "image"):
        return "image"
    if tag in ("input", "select", "textarea"):
        return "form_field"
    if tag in ("video", "audio"):
        return "media"
    if tag == "table" or role == "table":
        return "table"
    if tag in ("ul", "ol", "dl") or role == "list":
        return "list"
    if tag == "iframe":
        return "iframe"
    if tag in ("nav", "main", "header", "footer", "aside") or role in (
        "navigation", "main", "banner", "contentinfo", "complementary",
        "region", "search", "form",
    ):
        return "landmark"
    if tag == "button" or role == "button" or input_type in ("button", "submit", "reset"):
        return "button"
    return "other"


def _to_heading(el: dict) -> dict:
    level = el.get("level", 2)
    if not (1 <= level <= 6):
        level = 2
    return {
        "tag": el.get("tag") or f"h{level}",
        "level": level,
        "text": el.get("text", ""),
        "id": el.get("id", ""),
        "selector": el.get("selector", ""),
        "shadow_host": el.get("shadow_host", ""),
    }


def _to_link(el: dict) -> dict:
    aria = el.get("aria", {})
    return {
        "text": el.get("text", ""),
        "href": el.get("href", ""),
        "target": el.get("target", ""),
        "aria_label": aria.get("aria-label", ""),
        "aria-label": aria.get("aria-label", ""),
        "aria_labelledby": aria.get("aria-labelledby", ""),
        "aria-labelledby": aria.get("aria-labelledby", ""),
        "title": el.get("title", ""),
        "role": el.get("role", ""),
        "selector": el.get("selector", ""),
        "has_image": False,
        "image_alt": "",
        "context": "",
        "rect": el.get("rect", {}),
        "visible": el.get("visible", True),
        "shadow_host": el.get("shadow_host", ""),
    }


def _to_image(el: dict) -> dict:
    aria = el.get("aria", {})
    rect = el.get("rect", {})
    return {
        "src": el.get("src", ""),
        "alt": el.get("alt", ""),
        "role": el.get("role", ""),
        "ariaLabel": aria.get("aria-label", ""),
        "aria-label": aria.get("aria-label", ""),
        "ariaHidden": aria.get("aria-hidden", ""),
        "width": rect.get("width", 0),
        "height": rect.get("height", 0),
        "isDecorative": el.get("role") in ("presentation", "none") or el.get("alt") == "",
        "selector": el.get("selector", ""),
        "rect": rect,
        "parent_tag": "",
        "parent_role": "",
        "shadow_host": el.get("shadow_host", ""),
    }


def _to_form_field(el: dict) -> dict:
    aria = el.get("aria", {})
    return {
        "tag": el.get("tag", ""),
        "type": el.get("input_type", ""),
        "name": el.get("name", ""),
        "id": el.get("id", ""),
        "label": el.get("label", ""),
        "aria_label": aria.get("aria-label", ""),
        "aria-label": aria.get("aria-label", ""),
        "aria_labelledby": aria.get("aria-labelledby", ""),
        "aria-labelledby": aria.get("aria-labelledby", ""),
        "aria_describedby": aria.get("aria-describedby", ""),
        "aria-describedby": aria.get("aria-describedby", ""),
        "required": el.get("required", False),
        "placeholder": el.get("placeholder", ""),
        "role": el.get("role", ""),
        "autocomplete": el.get("autocomplete", ""),
        "selector": el.get("selector", ""),
        "title": el.get("title", el.get("accessible_name", "")),
        "rect": el.get("rect", {}),
        "visible": el.get("visible", True),
        "shadow_host": el.get("shadow_host", ""),
    }


def _to_media(el: dict) -> dict:
    aria = el.get("aria", {})
    return {
        "tag": el.get("tag", ""),
        "src": el.get("src", ""),
        "autoplay": el.get("autoplay", False),
        "loop": el.get("loop", False),
        "muted": el.get("muted", False),
        "controls": el.get("controls", True),
        "tracks": el.get("tracks", []),
        "aria_label": aria.get("aria-label", ""),
        "selector": el.get("selector", ""),
        "rect": el.get("rect", {}),
        "shadow_host": el.get("shadow_host", ""),
    }


def _to_landmark(el: dict) -> dict:
    aria = el.get("aria", {})
    return {
        "tag": el.get("tag", ""),
        "role": el.get("role", ""),
        "aria_label": aria.get("aria-label", ""),
        "aria-label": aria.get("aria-label", ""),
        "aria_labelledby": aria.get("aria-labelledby", ""),
        "selector": el.get("selector", ""),
        "rect": el.get("rect", {}),
        "shadow_host": el.get("shadow_host", ""),
    }


def _to_table(el: dict) -> dict:
    aria = el.get("aria", {})
    return {
        "caption": el.get("caption", el.get("text", "")),
        "role": el.get("role", ""),
        "aria_label": aria.get("aria-label", ""),
        "aria-label": aria.get("aria-label", ""),
        "selector": el.get("selector", ""),
        "headers": [],
        "rowCount": 0,
        "shadow_host": el.get("shadow_host", ""),
    }


def _to_list(el: dict) -> dict:
    aria = el.get("aria", {})
    return {
        "tag": el.get("tag", ""),
        "itemCount": 0,
        "role": el.get("role", ""),
        "aria_label": aria.get("aria-label", ""),
        "selector": el.get("selector", ""),
        "rect": el.get("rect", {}),
        "shadow_host": el.get("shadow_host", ""),
    }


def _to_iframe(el: dict) -> dict:
    aria = el.get("aria", {})
    rect = el.get("rect", {})
    return {
        "src": el.get("src", ""),
        "title": el.get("title", ""),
        "aria_label": aria.get("aria-label", ""),
        "aria-label": aria.get("aria-label", ""),
        "aria_hidden": aria.get("aria-hidden", ""),
        "width": rect.get("width", 0),
        "height": rect.get("height", 0),
        "name": el.get("name", ""),
        "selector": el.get("selector", ""),
        "shadow_host": el.get("shadow_host", ""),
    }


_CONVERTERS = {
    "heading": _to_heading,
    "link": _to_link,
    "image": _to_image,
    "form_field": _to_form_field,
    "media": _to_media,
    "landmark": _to_landmark,
    "table": _to_table,
    "list": _to_list,
    "iframe": _to_iframe,
}

_FIELD_MAP = {
    "heading": "headings",
    "link": "links",
    "image": "images",
    "form_field": "form_fields",
    "media": "media",
    "landmark": "landmarks",
    "table": "tables",
    "list": "lists",
    "iframe": "iframes",
}


async def merge_shadow_into_capture(page, capture_data: Any) -> None:
    """Extract shadow DOM elements and merge them into CaptureData fields.

    Adds shadow DOM elements to the appropriate CaptureData lists:
    - form fields -> capture_data.form_fields
    - links -> capture_data.links
    - images -> capture_data.images
    - headings -> capture_data.headings
    - All elements -> capture_data.shadow_elements (new field)

    Each merged element is tagged with shadow_host so it's clear
    it came from shadow DOM, not the light DOM.
    """
    elements = await extract_shadow_elements(page)
    if not elements:
        logger.info("Shadow DOM merge: no shadow elements to merge")
        return

    if hasattr(capture_data, "shadow_elements"):
        capture_data.shadow_elements = elements

    counts: dict[str, int] = {}
    for el in elements:
        category = _classify_element(el)
        converter = _CONVERTERS.get(category)
        field_name = _FIELD_MAP.get(category)
        if not converter or not field_name:
            continue
        converted = converter(el)
        target_list = getattr(capture_data, field_name, None)
        if target_list is not None:
            target_list.append(converted)
            counts[field_name] = counts.get(field_name, 0) + 1

    for field_name, count in sorted(counts.items()):
        logger.info("Shadow DOM merge: added %d elements to %s", count, field_name)
    logger.info("Shadow DOM merge: %d total elements merged into CaptureData", sum(counts.values()))
