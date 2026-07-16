"""Phase 1: Static Code AI — reads DOM/CSS/JS and produces element inventory.

The AI analyzes the page structure and identifies EVERY element relevant
to accessibility testing.  No hardcoded selectors — the AI decides what
exists on the page based on the actual HTML, CSS, and a screenshot.

For large pages (>80K tokens), the DOM is chunked and analyzed in
multiple calls, then results are merged.
"""
from __future__ import annotations

import json
import logging
import os
import time
from typing import Any

from playwright.async_api import Page

from capture.v2.dom_chunker import chunk_dom, extract_css_summary, extract_js_event_summary, DOMChunks
from capture.v2.element_inventory import ElementInventory, InventoryElement, map_inventory_to_capture_data
from functions.element_labeler import LABELER_JS_BUNDLE

logger = logging.getLogger(__name__)

# Token threshold for switching to multi-call mode
_SINGLE_CALL_TOKEN_LIMIT = 80_000


def _parse_native_tool_call(content: str) -> dict | None:
    """Parse native tool call format from content text.

    Models via vLLM return tool calls in native format:
        <tool_call>
        <function=report_element_inventory>
        <parameter=page_summary>...</parameter>
        <parameter=elements>[...JSON...]</parameter>
        </function>
        </tool_call>

    Handles incomplete responses (truncated at token limit).
    """
    from functions.parser import parse_native_tool_call
    return parse_native_tool_call(content)


def _clean_provider_tokens(text: str) -> str:
    """Import from shared location."""
    from functions.parser import clean_tool_call_args
    return clean_tool_call_args(text)


def _assign_exploration_priority(elem: dict) -> None:
    """Deterministically set exploration_priority and exploration_actions.

    Based on element type and ARIA attributes — no AI needed.
    Phase 2 uses these to decide what to hover/click/screenshot.
    Phase 3 uses these to plan form fill, media playback, etc.
    """
    aria = elem.get("aria") or {}
    elem_type = elem.get("type", "")
    tag = elem.get("tag", "")

    has_haspopup = "aria-haspopup" in aria
    has_expanded = "aria-expanded" in aria
    has_controls = "aria-controls" in aria
    haspopup_val = aria.get("aria-haspopup", "")

    # Reclassify type based on ARIA attributes — code knows better than labels
    if has_haspopup and haspopup_val in ("dialog", "true") and elem_type in ("link", "button"):
        if haspopup_val == "dialog":
            elem["type"] = "modal_trigger"
        else:
            elem["type"] = "dropdown"
        elem_type = elem["type"]
    elif has_expanded and elem_type in ("link", "button"):
        text_lower = (elem.get("text") or "").lower()
        if "toggle" in text_lower or "submenu" in text_lower or "menu" in text_lower:
            elem["type"] = "dropdown"
        else:
            elem["type"] = "dropdown"
        elem_type = elem["type"]

    # HIGH: anything that changes state on interaction
    if elem_type in ("dropdown", "menu", "modal_trigger", "accordion",
                      "carousel", "tab_panel", "media"):
        elem["exploration_priority"] = "high"
        elem["exploration_actions"] = ["click", "hover", "focus", "arrow_keys", "escape"]
    elif has_haspopup or has_expanded:
        elem["exploration_priority"] = "high"
        elem["exploration_actions"] = ["click", "hover", "focus"]
    elif elem_type == "form_field":
        elem["exploration_priority"] = "high"
        elem["exploration_actions"] = ["focus", "type"]
    elif elem_type == "button":
        elem["exploration_priority"] = "medium"
        elem["exploration_actions"] = ["hover", "focus", "click"]
    elif elem_type == "link":
        # Determine if this link needs exploration beyond just a simple nav link
        selector = (elem.get("selector") or "").lower()
        text = (elem.get("text") or "").lower()
        has_image = elem.get("has_image", False)
        # Links that are likely interactive cards, buttons, or special elements
        is_special = (
            has_controls
            or elem.get("target") == "_blank"
            or has_image  # Image links need alt text verification
            or "btn" in selector or "button" in selector  # Styled as button
            or "card" in selector  # Card-style link
            or "register" in text or "sign up" in text  # CTAs
            or "learn more" in text or "read more" in text
            or "download" in text or "apply" in text
            or not text.strip()  # Empty text links — need exploration to see what they are
        )
        if is_special:
            elem["exploration_priority"] = "medium"
            elem["exploration_actions"] = ["hover", "focus"]
        else:
            elem["exploration_priority"] = "low"
            elem["exploration_actions"] = ["hover", "focus"]
    elif elem_type in ("image", "heading", "landmark", "table", "list", "iframe"):
        elem["exploration_priority"] = "low"
        elem["exploration_actions"] = []
    else:
        elem["exploration_priority"] = "low"
        elem["exploration_actions"] = []


async def _extract_elements_deterministic(page: Page) -> list[dict]:
    """Extract every accessibility-relevant element from the live DOM.

    Uses Playwright page.evaluate() — 100% accurate, zero AI dependency.
    Returns a list of dicts matching the InventoryElement schema.
    """
    return await page.evaluate(
        "() => {" + LABELER_JS_BUNDLE + """
            const results = [];

            function getSelector(el) {
                // Try ID first — guaranteed unique
                if (el.id) return '#' + CSS.escape(el.id);

                // Build path walking up until we hit an ID or body
                const parts = [];
                let current = el;
                while (current && current !== document.body && current !== document.documentElement) {
                    let seg = current.tagName.toLowerCase();

                    if (current.id) {
                        // Found an ancestor with an ID — anchor here
                        parts.unshift('#' + CSS.escape(current.id));
                        break;
                    }

                    // Add nth-of-type for uniqueness among siblings
                    if (current.parentElement) {
                        const siblings = Array.from(current.parentElement.children)
                            .filter(s => s.tagName === current.tagName);
                        if (siblings.length > 1) {
                            const idx = siblings.indexOf(current) + 1;
                            seg += ':nth-of-type(' + idx + ')';
                        }
                    }

                    parts.unshift(seg);
                    current = current.parentElement;

                    // Stop after 4 levels to keep selectors readable
                    if (parts.length >= 4) break;
                }

                const selector = parts.join(' > ');

                // Verify uniqueness — if multiple matches, add more context
                try {
                    const matches = document.querySelectorAll(selector);
                    if (matches.length === 1) return selector;
                    // Not unique — use full nth-of-type path from body
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
                } catch (e) {
                    return selector;
                }
            }

            function getAria(el) {
                const aria = {};
                for (const attr of el.attributes) {
                    if (attr.name.startsWith('aria-') || attr.name === 'role') {
                        aria[attr.name] = attr.value;
                    }
                }
                return Object.keys(aria).length > 0 ? aria : {};
            }

            function textOf(el) {
                return (el.textContent || '').trim();
            }

            function isVisible(el) {
                const style = window.getComputedStyle(el);
                return style.display !== 'none' && style.visibility !== 'hidden' && style.opacity !== '0';
            }

            // ── Headings ──
            document.querySelectorAll('h1,h2,h3,h4,h5,h6,[role="heading"]').forEach(el => {
                const tag = el.tagName.toLowerCase();
                const level = tag.startsWith('h') ? parseInt(tag[1]) : parseInt(el.getAttribute('aria-level') || '2');
                results.push({
                    type: 'heading', selector: getSelector(el), tag: tag,
                    text: textOf(el), level: level, aria: getAria(el),
                    role: el.getAttribute('role') || '', visible: isVisible(el),
                    interactive: false, id: el.id || '',
                });
            });

            // ── Links — all <a> tags (with or without href) + role="link" ──
            document.querySelectorAll('a,[role="link"]').forEach(el => {
                // Get surrounding context for SC 2.4.4 (link purpose)
                let context = '';
                const parentP = el.closest('p,li,td,th,dd,figcaption');
                if (parentP) context = parentP.textContent.trim();

                results.push({
                    type: 'link', selector: getSelector(el), tag: el.tagName.toLowerCase(),
                    text: textOf(el), href: el.getAttribute('href') || '',
                    target: el.getAttribute('target') || '', title: el.getAttribute('title') || '',
                    aria: getAria(el), role: el.getAttribute('role') || '',
                    visible: isVisible(el), interactive: true, id: el.id || '',
                    context: context,
                    has_image: !!el.querySelector('img,svg,[role="img"]'),
                    image_alt: (el.querySelector('img') || {}).alt || '',
                });
            });

            // ── Buttons — all button-like elements ──
            document.querySelectorAll('button,[role="button"],input[type="submit"],input[type="button"],input[type="reset"],[tabindex="0"][onclick]').forEach(el => {
                results.push({
                    type: 'button', selector: getSelector(el), tag: el.tagName.toLowerCase(),
                    text: textOf(el) || el.value || '', aria: getAria(el),
                    role: el.getAttribute('role') || 'button', visible: isVisible(el),
                    interactive: true, id: el.id || '',
                });
            });

            // ── Form fields — all input types + contenteditable ──
            document.querySelectorAll('input:not([type="hidden"]):not([type="submit"]):not([type="button"]):not([type="reset"]),select,textarea,output,progress,meter,[contenteditable="true"]').forEach(el => {
                // Find label through multiple methods
                let label = '';
                if (el.id) {
                    const labelEl = document.querySelector('label[for="' + CSS.escape(el.id) + '"]');
                    if (labelEl) label = textOf(labelEl);
                }
                if (!label) {
                    const closestLabel = el.closest('label');
                    if (closestLabel) label = textOf(closestLabel);
                }
                if (!label) label = el.getAttribute('aria-label') || '';
                if (!label) {
                    const labelledby = el.getAttribute('aria-labelledby');
                    if (labelledby) {
                        const lblEl = document.getElementById(labelledby);
                        if (lblEl) label = textOf(lblEl);
                    }
                }

                results.push({
                    type: 'form_field', selector: getSelector(el), tag: el.tagName.toLowerCase(),
                    text: textOf(el), input_type: el.getAttribute('type') || '',
                    name: el.getAttribute('name') || '', id: el.id || '',
                    label: label,
                    placeholder: el.getAttribute('placeholder') || '',
                    required: el.hasAttribute('required') || el.getAttribute('aria-required') === 'true',
                    autocomplete: el.getAttribute('autocomplete') || '',
                    aria: getAria(el), role: el.getAttribute('role') || '',
                    visible: isVisible(el), interactive: true, title: el.getAttribute('title') || '',
                });
            });

            // ── Images — img, svg with content, picture, figure ──
            document.querySelectorAll('img,svg[role="img"],[role="img"],picture').forEach(el => {
                const tag = el.tagName.toLowerCase();
                const isDecorative = el.getAttribute('role') === 'presentation'
                    || el.getAttribute('role') === 'none'
                    || el.getAttribute('aria-hidden') === 'true'
                    || (tag === 'img' && el.getAttribute('alt') === '');

                const parent = el.parentElement;
                results.push({
                    type: 'image', selector: getSelector(el), tag: tag,
                    text: '', src: el.getAttribute('src') || el.getAttribute('data-src') || '',
                    alt: el.getAttribute('alt') || '',
                    aria: getAria(el), role: el.getAttribute('role') || '',
                    visible: isVisible(el), interactive: false, id: el.id || '',
                    title: el.getAttribute('title') || '',
                    isDecorative: isDecorative,
                    parent_tag: parent ? parent.tagName.toLowerCase() : '',
                    parent_role: parent ? (parent.getAttribute('role') || '') : '',
                });
            });

            // ── Figures with captions ──
            document.querySelectorAll('figure').forEach(el => {
                const caption = el.querySelector('figcaption');
                if (caption) {
                    results.push({
                        type: 'image', selector: getSelector(el), tag: 'figure',
                        text: textOf(caption), aria: getAria(el),
                        role: el.getAttribute('role') || 'figure',
                        visible: isVisible(el), interactive: false, id: el.id || '',
                    });
                }
            });

            // ── Landmarks — all semantic + ARIA + section with label ──
            document.querySelectorAll('nav,main,header,footer,aside,form[aria-label],form[aria-labelledby],section[aria-label],section[aria-labelledby],[role="navigation"],[role="main"],[role="banner"],[role="contentinfo"],[role="complementary"],[role="search"],[role="region"],[role="form"]').forEach(el => {
                results.push({
                    type: 'landmark', selector: getSelector(el), tag: el.tagName.toLowerCase(),
                    text: '', aria: getAria(el),
                    role: el.getAttribute('role') || el.tagName.toLowerCase(),
                    visible: isVisible(el), interactive: false, id: el.id || '',
                });
            });

            // ── Iframes + embedded media iframes ──
            document.querySelectorAll('iframe,object,embed').forEach(el => {
                const tag = el.tagName.toLowerCase();
                const src = el.getAttribute('src') || el.getAttribute('data') || '';
                const isMedia = /youtube|vimeo|dailymotion|wistia|brightcove/i.test(src);
                results.push({
                    type: isMedia ? 'media' : 'iframe',
                    selector: getSelector(el), tag: tag,
                    text: '', src: src,
                    title: el.getAttribute('title') || '', name: el.getAttribute('name') || '',
                    aria: getAria(el), role: el.getAttribute('role') || '',
                    visible: isVisible(el), interactive: isMedia, id: el.id || '',
                });
            });

            // ── Media — video, audio, source elements ──
            document.querySelectorAll('video,audio').forEach(el => {
                const tracks = [];
                el.querySelectorAll('track').forEach(t => {
                    tracks.push({kind: t.kind, src: t.src, srclang: t.srclang, label: t.label});
                });
                const sources = [];
                el.querySelectorAll('source').forEach(s => {
                    sources.push({src: s.src, type: s.type});
                });
                results.push({
                    type: 'media', selector: getSelector(el), tag: el.tagName.toLowerCase(),
                    text: '', src: el.getAttribute('src') || (sources[0] || {}).src || '',
                    aria: getAria(el), role: el.getAttribute('role') || '',
                    visible: isVisible(el), interactive: true, tracks: tracks, id: el.id || '',
                });
            });

            // ── Tables — with full structure ──
            document.querySelectorAll('table').forEach(el => {
                const headers = [];
                el.querySelectorAll('th').forEach(th => {
                    headers.push({
                        text: textOf(th), scope: th.getAttribute('scope') || '',
                        id: th.id || '',
                    });
                });
                results.push({
                    type: 'table', selector: getSelector(el), tag: 'table',
                    text: el.querySelector('caption')?.textContent?.trim() || '',
                    aria: getAria(el), role: el.getAttribute('role') || '',
                    visible: isVisible(el), interactive: false, id: el.id || '',
                    headers: headers,
                    rowCount: el.querySelectorAll('tr').length,
                    summary: el.getAttribute('summary') || '',
                });
            });

            // ── Lists ──
            document.querySelectorAll('ul,ol,dl,[role="list"]').forEach(el => {
                const tag = el.tagName.toLowerCase();
                const itemTag = tag === 'dl' ? 'dt' : 'li';
                results.push({
                    type: 'list', selector: getSelector(el), tag: tag,
                    text: '', aria: getAria(el), role: el.getAttribute('role') || '',
                    visible: isVisible(el), interactive: false, id: el.id || '',
                    itemCount: el.querySelectorAll(itemTag).length,
                });
            });

            // ── Skip links ──
            document.querySelectorAll('a[href^="#"]').forEach(el => {
                const text = textOf(el).toLowerCase();
                if (text.includes('skip') || text.includes('main content') || text.includes('jump to')) {
                    results.push({
                        type: 'skip_link', selector: getSelector(el), tag: 'a',
                        text: textOf(el), href: el.getAttribute('href') || '',
                        aria: getAria(el), role: '', visible: isVisible(el),
                        interactive: true, id: el.id || '',
                    });
                }
            });

            // Location labelling post-pass. Re-resolve each selector back
            // to its element and attach the structured ``location`` dict
            // (visible text, accessible name, nearest heading, enclosing
            // landmark, spatial bucket). A null resolution -- selector
            // stale because a later extractor mutated the DOM, etc. --
            // falls through with location: {} so the Python composer
            // just emits an empty LOCATION: line for that item.
            for (const r of results) {
                try {
                    const node = r.selector ? document.querySelector(r.selector) : null;
                    r.location = __wcagLabeler.describe(node);
                } catch (_) {
                    r.location = {};
                }
            }

            return results;
        }
    """
    )


# Tool schema for the AI to report the element inventory
_INVENTORY_TOOL = {
    "type": "function",
    "function": {
        "name": "report_element_inventory",
        "description": (
            "Report the complete element inventory for a web page. "
            "Every interactive element, content element, hidden element, "
            "and accessibility-relevant element must be cataloged."
        ),
        "parameters": {
            "type": "object",
            "required": ["elements", "page_summary", "page_type"],
            "properties": {
                "page_summary": {
                    "type": "string",
                    "description": "1-2 sentence description of what this page is and does.",
                },
                "page_type": {
                    "type": "string",
                    "enum": ["content", "form-heavy", "application", "media-rich",
                             "navigation-hub", "dashboard", "landing", "other"],
                    "description": "The primary page type based on its content and purpose.",
                },
                "elements": {
                    "type": "array",
                    "description": "Complete list of every accessibility-relevant element.",
                    "items": {
                        "type": "object",
                        "required": ["type", "selector", "tag", "visible", "interactive"],
                        "properties": {
                            "type": {
                                "type": "string",
                                "enum": [
                                    "heading", "link", "button", "form_field", "image",
                                    "background_image", "media", "table", "list",
                                    "landmark", "iframe", "captcha", "skip_link",
                                    "menu", "dropdown", "accordion", "tab_panel",
                                    "modal_trigger", "tooltip_trigger", "carousel",
                                    "custom_control", "live_region", "decorative",
                                ],
                            },
                            "selector": {
                                "type": "string",
                                "description": "CSS selector that uniquely identifies this element.",
                            },
                            "tag": {"type": "string"},
                            "text": {
                                "type": "string",
                                "description": "Visible text content (first 200 chars).",
                            },
                            "aria": {
                                "type": "object",
                                "description": "All ARIA attributes as key-value pairs.",
                            },
                            "role": {"type": "string"},
                            "visible": {"type": "boolean"},
                            "interactive": {"type": "boolean"},
                            "parent_landmark": {"type": "string"},
                            "exploration_priority": {
                                "type": "string",
                                "enum": ["high", "medium", "low"],
                                "description": (
                                    "high: menus, dropdowns, modals, forms, media — "
                                    "MUST explore in Phase 2. medium: buttons, links "
                                    "with titles. low: static content."
                                ),
                            },
                            "exploration_actions": {
                                "type": "array",
                                "items": {"type": "string"},
                                "description": (
                                    "Interactions to try: hover, click, focus, expand, "
                                    "type, arrow_keys, escape, enter, space"
                                ),
                            },
                            # Type-specific fields
                            "href": {"type": "string"},
                            "alt": {"type": "string"},
                            "src": {"type": "string"},
                            "input_type": {"type": "string"},
                            "label": {"type": "string"},
                            "required": {"type": "boolean"},
                            "autocomplete": {"type": "string"},
                            "level": {"type": "integer"},
                            "target": {"type": "string"},
                            "title": {"type": "string"},
                            "placeholder": {"type": "string"},
                            "name": {"type": "string"},
                            "id": {"type": "string"},
                            "tracks": {
                                "type": "array",
                                "items": {"type": "object"},
                            },
                        },
                    },
                },
                "remove_selectors": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "CSS selectors of elements the programmatic extraction got WRONG. "
                        "These will be removed from the inventory. Use for: elements inside "
                        "<template> tags, CMS artifacts never rendered, duplicate shadow DOM "
                        "elements, SVG internals that aren't real navigation, or any element "
                        "that exists in the DOM but is not a real accessible element."
                    ),
                },
                "priority_updates": {
                    "type": "array",
                    "description": (
                        "Update exploration_priority on existing elements. Use when code "
                        "set 'low' but the element actually needs visual exploration."
                    ),
                    "items": {
                        "type": "object",
                        "properties": {
                            "selector": {"type": "string"},
                            "exploration_priority": {
                                "type": "string",
                                "enum": ["high", "medium", "low"],
                            },
                            "exploration_actions": {
                                "type": "array",
                                "items": {"type": "string"},
                            },
                        },
                    },
                },
                "sections_needing_deep_analysis": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Section names from the DOM skeleton that need "
                        "full HTML analysis in follow-up calls."
                    ),
                },
                "estimated_interaction_count": {
                    "type": "integer",
                    "description": "How many elements need Phase 2 visual exploration.",
                },
            },
        },
    },
}

_AUDIT_SYSTEM_PROMPT = r"""\
<role>
You audit a programmatic element extraction. A deterministic scanner
has already enumerated the standard HTML elements (links, buttons,
headings, images, form fields, landmarks, lists, tables, iframes,
media). Your job is to ADD elements the scanner cannot detect by tag
alone (custom-semantic widgets) and to REMOVE elements the scanner
wrongly included (template/script/shadow-duplicate noise). You do not
produce WCAG findings; you only refine the inventory the downstream
auditor will evaluate.
</role>

<task>
Work through the HTML section provided in the user prompt and call
the report_element_inventory tool exactly once. Follow these steps in
order:

  1. Read the HTML section. Identify every element that BEHAVES like
     an interactive control but is not a native <a>/<button>/<input>/
     <select>/<textarea>. These are the candidates for the elements
     list — the scanner already has the natives.
  2. For each candidate decide: does its role/aria/event semantics
     show interactivity (role="button", aria-expanded, aria-haspopup,
     onclick, tabindex="0", etc.)? If yes, copy the selector from
     the markup and add it to elements.
  3. Identify elements the scanner WRONGLY included: anything inside
     <template>, <script>, <style>, shadow-DOM duplicates, SVG
     <path>/<g> internals, and elements with aria-hidden="true" on
     themselves or an ancestor. Add their selectors to remove_selectors.
  4. Set exploration_priority on every element you add (see <rules>).
  5. Call report_element_inventory exactly once with both lists. No
     prose, no markdown, no commentary outside the tool call.
</task>

<rules>
  <add_only_what_scanner_missed>
    Do NOT pad the response. If the page is plain HTML and the scanner
    already has every relevant element, return empty arrays. Adding
    obvious natives just to look thorough creates duplicate work for
    the downstream auditor.
  </add_only_what_scanner_missed>

  <selector_must_be_real>
    Every selector in elements or remove_selectors must point at an
    element actually shown in the HTML section. Do NOT invent class
    names, IDs, or attribute values you have not seen in the source.
    If you cannot copy the selector verbatim from the HTML, omit the
    element. The downstream pipeline runs querySelectorAll(selector)
    on the live page; an invented selector silently matches nothing
    and produces zero findings while costing tool-call budget.
  </selector_must_be_real>

  <scope_is_section_only>
    Only audit the HTML section in the user prompt. Do not make claims
    about other parts of the page. The pipeline calls you once per
    section so each call covers a bounded region.
  </scope_is_section_only>

  <exploration_priority>
    Set the exploration_priority on every element you add:
      high   — element changes state on interaction (menus, modals,
               accordions, carousels, tabs, expand/collapse triggers).
      medium — element might change state (buttons with hover effects,
               clickable cards, custom links with no href).
      low    — static decoration, text, or labelled navigation that
               does not toggle anything.
  </exploration_priority>

  <remove_categories>
    Add to remove_selectors when the element is:
      - inside <template> (never rendered to users)
      - inside <script> or <style> (not real DOM)
      - a shadow-DOM duplicate of an element already in the inventory
      - SVG <path>/<g>/<defs> internals that aren't interactive
      - aria-hidden="true" on itself or an ancestor (intentionally
        removed from the accessibility tree)
  </remove_categories>
</rules>

<examples>
  <example>
    <scenario>
      The HTML section contains a Bootstrap-style accordion: a
      `<div class="accordion-toggle" role="button" aria-expanded="false"
      aria-controls="panel-1" tabindex="0">FAQ</div>` followed by a
      hidden panel.
    </scenario>
    <correct_output>
      Add elements: [{"selector": ".accordion-toggle[aria-controls=\"panel-1\"]",
      "tag": "div", "type": "button", "role": "button",
      "exploration_priority": "high", "actions": ["click", "focus", "enter"]}]
      remove_selectors: []
    </correct_output>
  </example>

  <example>
    <scenario>
      The HTML section contains an `<input type="text" id="email">`
      with a wrapping `<label for="email">Email</label>`. Both are
      already standard HTML elements the scanner finds by tag.
    </scenario>
    <correct_output>
      Add elements: []
      remove_selectors: []
      Page summary mentions a contact form but does not duplicate the
      native input — the scanner already has it.
    </correct_output>
  </example>

  <example>
    <scenario>
      The section includes a `<template id="modal-shell">…</template>`
      whose contents include a `<button class="close">×</button>`. The
      scanner reported `template button.close`.
    </scenario>
    <correct_output>
      Add elements: []
      remove_selectors: ["#modal-shell button.close"]
      Reasoning: <template> contents are never rendered until cloned
      into the live DOM; the scanner should not include them.
    </correct_output>
  </example>
</examples>

<output_format>
  Respond ONLY by calling report_element_inventory once. No prose, no
  markdown. The tool call returns:
    page_summary: 1-2 sentences describing what this section is.
    elements: list of NEW elements not findable by native tag alone.
    remove_selectors: selectors of mis-included elements.
  An empty result is valid and preferred to padding.
</output_format>
"""


async def run_phase1(
    page: Page,
    capture_data: Any,
    ai_client: Any,
    captures_dir: str,
    progress_callback=None,
) -> ElementInventory:
    """Run Phase 1: AI analyzes the page and produces an element inventory.

    Args:
        page: Live Playwright page (already navigated)
        capture_data: CaptureData being populated
        ai_client: AIClient for API calls
        captures_dir: Directory for saving capture data
        progress_callback: async callback for progress updates

    Returns:
        ElementInventory with all discovered elements
    """
    phase_start = time.monotonic()
    logger.info("=" * 60)
    logger.info("PHASE 1: Static Code AI Analysis")
    logger.info("=" * 60)

    if progress_callback:
        await progress_callback("Phase 1: Analyzing page structure with AI...")

    # Step 1: Extract raw data from the page
    logger.info("PHASE 1: Extracting DOM, CSS, and JS event handlers...")

    html = await page.content()
    capture_data.html = html
    logger.info("PHASE 1: DOM extracted (%d chars)", len(html))

    # Save DOM to disk
    dom_path = os.path.join(captures_dir, "dom.html")
    with open(dom_path, "w", encoding="utf-8") as f:
        f.write(html)
    capture_data.dom_path = dom_path

    # Extract CSS
    css_summary = extract_css_summary(html)
    logger.info("PHASE 1: CSS summary extracted (%d chars)", len(css_summary))

    # Extract JS event handlers
    js_events_raw = await page.evaluate("""
        () => {
            const results = [];
            const all = document.querySelectorAll('*');
            for (const el of all) {
                const events = [];
                // Check inline handlers
                const attrs = el.attributes;
                for (let i = 0; i < attrs.length; i++) {
                    const name = attrs[i].name.toLowerCase();
                    if (name.startsWith('on')) events.push(name);
                }
                // Check aria attributes that imply interactivity
                if (el.hasAttribute('aria-haspopup')) events.push('aria-haspopup');
                if (el.hasAttribute('aria-expanded')) events.push('aria-expanded');
                if (el.hasAttribute('aria-controls')) events.push('aria-controls');
                if (el.hasAttribute('tabindex')) events.push('tabindex=' + el.getAttribute('tabindex'));

                if (events.length > 0) {
                    let selector = el.tagName.toLowerCase();
                    if (el.id) selector = '#' + el.id;
                    else if (el.className && typeof el.className === 'string')
                        selector = el.tagName.toLowerCase() + '.' + el.className.trim().split(/\\s+/).join('.');

                    results.push({
                        selector: selector,
                        tag: el.tagName.toLowerCase(),
                        events: events,
                    });
                }
            }
            return results;
        }
    """)
    js_summary = extract_js_event_summary(js_events_raw)
    logger.info("PHASE 1: JS event handlers extracted (%d elements with events)", len(js_events_raw))

    # Step 2: Deterministic element extraction — 100% accurate for what's in the DOM.
    logger.info("PHASE 1: Running deterministic element extraction...")
    if progress_callback:
        await progress_callback("Phase 1: Extracting all page elements programmatically...")

    deterministic_elements = await _extract_elements_deterministic(page)
    logger.info("PHASE 1: Deterministic extraction found %d elements", len(deterministic_elements))

    # Step 3: Build inventory from deterministic results (deduped by selector)
    # Also assign exploration priorities based on element attributes
    inventory = ElementInventory()
    seen_selectors = set()
    for elem_dict in deterministic_elements:
        _assign_exploration_priority(elem_dict)
        elem = InventoryElement.from_dict(elem_dict)
        if elem.selector and elem.selector not in seen_selectors:
            seen_selectors.add(elem.selector)
            inventory.elements.append(elem)

    det_count = len(inventory.elements)
    logger.info("PHASE 1: Seeded inventory with %d deterministic elements", det_count)

    # Count by type for logging
    type_counts = {}
    for e in inventory.elements:
        type_counts[e.type] = type_counts.get(e.type, 0) + 1
    for t, c in sorted(type_counts.items(), key=lambda x: -x[1]):
        logger.info("PHASE 1:   %s: %d", t, c)

    # Step 4: Chunk the DOM for AI audit
    chunks = chunk_dom(html, css_summary, js_summary)

    # Step 5: Get the full-page screenshot for the AI
    screenshot_path = capture_data.full_page_path or os.path.join(captures_dir, "full_page.png")

    # Step 6: AI audit — one call per section.
    #
    # The AI gets the section HTML + what deterministic found in it.
    # Its job: find custom controls, modals, menus, wrong semantics —
    # anything the code missed. Single call, fresh context, no loop.
    if ai_client:
        logger.info("PHASE 1: Starting AI audit of %d sections...", len(chunks.sections))
        existing_selectors = {e.selector for e in inventory.elements}

        sections_to_audit = chunks.sections
        if len(html) <= 120_000:
            # Small page — audit as one section
            from capture.v2.dom_chunker import DOMSection
            sections_to_audit = [DOMSection(name="full_page", html=html)]

        for sec_idx, section in enumerate(sections_to_audit):
            logger.info("PHASE 1: AI audit section %d/%d '%s' (%d chars)",
                         sec_idx + 1, len(sections_to_audit),
                         section.name, len(section.html))
            if progress_callback:
                await progress_callback(
                    f"Phase 1: AI auditing section '{section.name}' "
                    f"[{sec_idx + 1}/{len(sections_to_audit)}]..."
                )

            ai_additions, ai_removals, ai_priorities = await _audit_section(
                ai_client, section.html, section.name,
                existing_selectors, screenshot_path, captures_dir,
            )

            # Add new elements. AI additions arrive as fully-formed
            # InventoryElement instances but WITHOUT having passed
            # through ``_assign_exploration_priority`` (the
            # deterministic rule function that looks at type/ARIA and
            # sets priority + actions). Run that rule now so every
            # AI-added element gets the same treatment as deterministic
            # ones. Without this, AI additions default to
            # exploration_priority="low" and Phase 2 skips them --
            # observed on a university site 2026-04-23 where 167/240 elements
            # had no priority tag because the AI didn't emit one.
            added = 0
            for elem in ai_additions:
                if elem.selector and elem.selector not in existing_selectors:
                    # Round-trip through a dict so the deterministic
                    # priority rule can apply. Only fill missing bits:
                    # if the AI explicitly set a priority (stored on
                    # elem.exploration_priority) and it's already
                    # "high" or "medium", keep it; otherwise let the
                    # deterministic rule assign.
                    tmp = {
                        "type": elem.type, "tag": elem.tag,
                        "role": elem.role, "aria": elem.aria or {},
                        "text": elem.text, "selector": elem.selector,
                        "href": elem.href, "target": elem.target,
                    }
                    _assign_exploration_priority(tmp)
                    det_priority = tmp.get("exploration_priority", "low")
                    det_actions = tmp.get("exploration_actions") or []
                    # If AI gave a stronger priority already, keep it.
                    rank = {"low": 0, "medium": 1, "high": 2}
                    if rank.get(det_priority, 0) > rank.get(elem.exploration_priority, 0):
                        elem.exploration_priority = det_priority
                    if not elem.exploration_actions:
                        elem.exploration_actions = det_actions
                    # Type may have been reclassified (link -> modal_trigger
                    # via aria-haspopup). Honor the deterministic rule.
                    if tmp.get("type") and tmp["type"] != elem.type:
                        elem.type = tmp["type"]
                    existing_selectors.add(elem.selector)
                    inventory.elements.append(elem)
                    added += 1

            # Remove flagged elements — but save them for reference.
            # Removed elements may indicate accessibility issues themselves
            # (e.g., a div acting as a button without role="button").
            removed = 0
            if ai_removals:
                remove_set = set(ai_removals)
                kept = []
                for e in inventory.elements:
                    if e.selector in remove_set:
                        if not hasattr(capture_data, 'ai_removed_elements'):
                            capture_data.ai_removed_elements = []
                        capture_data.ai_removed_elements.append({
                            "selector": e.selector, "type": e.type,
                            "tag": e.tag, "text": e.text,
                            "reason": "AI flagged as not a real accessible element",
                        })
                        removed += 1
                        logger.info("PHASE 1: AI removed: %s (%s)", e.selector, e.type)
                    else:
                        kept.append(e)
                inventory.elements = kept
                existing_selectors -= remove_set

            # Apply priority updates — AI can promote low→high
            # so Phase 2 explores elements code marked as static.
            # If AI sets high/medium but no actions, assign defaults.
            _DEFAULT_ACTIONS = {
                "link": ["hover", "focus"],
                "button": ["hover", "focus", "click"],
                "form_field": ["focus", "type"],
                "dropdown": ["click", "hover", "focus", "arrow_keys", "escape"],
                "modal_trigger": ["click", "hover", "focus"],
                "accordion": ["click", "focus"],
                "carousel": ["click", "arrow_keys"],
                "tab_panel": ["click", "arrow_keys"],
                "menu": ["click", "hover", "focus", "arrow_keys", "escape"],
            }
            updated = 0
            if ai_priorities:
                selector_to_elem = {e.selector: e for e in inventory.elements}
                for upd in ai_priorities:
                    sel = upd.get("selector", "")
                    elem = selector_to_elem.get(sel)
                    if elem:
                        old_pri = elem.exploration_priority
                        elem.exploration_priority = upd.get("exploration_priority", elem.exploration_priority)
                        new_actions = upd.get("exploration_actions")
                        if new_actions:
                            elem.exploration_actions = new_actions
                        # If promoted but no actions, assign defaults by type
                        if elem.exploration_priority in ("high", "medium") and not elem.exploration_actions:
                            elem.exploration_actions = _DEFAULT_ACTIONS.get(elem.type, ["hover", "focus"])
                        if old_pri != elem.exploration_priority:
                            updated += 1
                            logger.info("PHASE 1: AI updated priority: %s %s→%s",
                                       sel, old_pri, elem.exploration_priority)

            logger.info("PHASE 1: Section '%s' → +%d added, -%d removed, %d priorities updated (total: %d)",
                         section.name, added, removed, updated, len(inventory.elements))
    else:
        logger.warning("PHASE 1: No AI client — skipping AI audit")

    # Step 5: Validate selectors against the live page
    logger.info("PHASE 1: Validating %d selectors against live page...", len(inventory.elements))
    validated = []
    invalid_count = 0
    for elem in inventory.elements:
        if not elem.selector:
            invalid_count += 1
            continue
        try:
            found = await page.query_selector(elem.selector)
            if found:
                rect = await found.bounding_box()
                if rect:
                    elem.rect = {"x": rect["x"], "y": rect["y"],
                                 "width": rect["width"], "height": rect["height"]}
                elem.visible = await found.is_visible()
                validated.append(elem)
            else:
                invalid_count += 1
                logger.debug("PHASE 1: Selector not found: %s", elem.selector)
        except Exception:
            invalid_count += 1
            logger.debug("PHASE 1: Invalid selector: %s", elem.selector)

    inventory.elements = validated
    logger.info("PHASE 1: %d elements validated, %d invalid selectors removed",
                len(validated), invalid_count)

    # Final pass: guarantee every element carries a priority + actions.
    # Any element that slipped through without a priority (AI audit
    # emitted a raw dict, subclass path, etc.) gets the deterministic
    # rule applied here. Phase 2 filters by priority to decide what
    # to explore, so a missing priority silently drops the element.
    no_priority_count = 0
    for elem in inventory.elements:
        if not elem.exploration_priority or elem.exploration_priority not in (
            "high", "medium", "low",
        ):
            tmp = {
                "type": elem.type, "tag": elem.tag,
                "role": elem.role, "aria": elem.aria or {},
                "text": elem.text, "selector": elem.selector,
                "href": elem.href, "target": elem.target,
            }
            _assign_exploration_priority(tmp)
            elem.exploration_priority = tmp.get("exploration_priority", "low")
            if not elem.exploration_actions:
                elem.exploration_actions = tmp.get("exploration_actions") or []
            no_priority_count += 1
    if no_priority_count:
        logger.info(
            "PHASE 1: final pass assigned priority to %d element(s) that "
            "the audit left unlabeled",
            no_priority_count,
        )

    # Step 7: Map to CaptureData legacy fields
    map_inventory_to_capture_data(inventory, capture_data)

    # Step 7: Save inventory to disk for debugging
    inventory_path = os.path.join(captures_dir, "element_inventory.json")
    with open(inventory_path, "w", encoding="utf-8") as f:
        json.dump({
            "page_summary": inventory.page_summary,
            "page_type": inventory.page_type,
            "element_count": len(inventory.elements),
            "estimated_interaction_count": inventory.estimated_interaction_count,
            "elements": [_elem_to_log_dict(e) for e in inventory.elements],
        }, f, indent=2, default=str)

    elapsed = time.monotonic() - phase_start
    capture_data.phase_timings["phase1"] = round(elapsed, 1)
    logger.info("PHASE 1 COMPLETE: %d elements in %.1fs", len(inventory.elements), elapsed)

    return inventory


# Tool for AI to signal it's done discovering elements
_DONE_TOOL = {
    "type": "function",
    "function": {
        "name": "inventory_complete",
        "description": (
            "Call this when you have reported ALL elements on the page. "
            "Do NOT call this until every heading, link, image, form field, "
            "button, landmark, table, list, iframe, and interactive element "
            "has been reported via report_element_inventory."
        ),
        "parameters": {
            "type": "object",
            "required": ["page_summary", "page_type", "total_elements_reported"],
            "properties": {
                "page_summary": {"type": "string"},
                "page_type": {
                    "type": "string",
                    "enum": ["content", "form-heavy", "application", "media-rich",
                             "navigation-hub", "dashboard", "landing", "other"],
                },
                "total_elements_reported": {"type": "integer"},
            },
        },
    },
}


async def _audit_section(
    ai_client,
    section_html: str,
    section_name: str,
    existing_selectors: set[str],
    screenshot_path: str,
    captures_dir: str,
) -> tuple[list[InventoryElement], list[str], list[dict]]:
    """Single AI call to audit one DOM section.

    The AI receives the section HTML + a summary of what deterministic
    extraction already found.  It can ADD missed elements, REMOVE wrong
    ones, and UPDATE exploration priorities on existing elements.

    Returns (additions, removals, priority_updates).
    """
    from functions.llm import LLMClient

    det_summary = f"{len(existing_selectors)} elements already found programmatically."

    user_prompt = (
        f"═══════════════════════════════════════════════════════\n"
        f"  AUDIT: Section '{section_name}'\n"
        f"═══════════════════════════════════════════════════════\n\n"
        f"{det_summary}\n\n"
        f"Review the HTML below. Report ONLY elements the code missed:\n"
        f"- Custom controls (divs acting as buttons/sliders/tabs)\n"
        f"- Modal/dialog triggers\n"
        f"- Dropdown menus, accordions, carousels\n"
        f"- Hidden dynamic content (off-canvas, toasts)\n"
        f"- Elements with aria-expanded, aria-haspopup\n\n"
        f"Also set exploration_priority for ANY element that changes\n"
        f"state on interaction (high), might change (medium), or is static (low).\n\n"
        f"If the code caught everything, return an EMPTY elements array.\n\n"
        f"═══════════════════════════════════════════════════════\n"
        f"  SECTION HTML\n"
        f"═══════════════════════════════════════════════════════\n\n"
        f"{section_html}\n\n"
        f"═══════════════════════════════════════════════════════\n"
        f"  ACTION\n"
        f"═══════════════════════════════════════════════════════\n\n"
        f"Call report_element_inventory with any missed elements.\n"
    )

    image_paths = (
        [screenshot_path] if screenshot_path and os.path.exists(screenshot_path) else None
    )

    removals: list[str] = []
    priority_updates: list[dict] = []

    # call_with_tools handles parser recovery + LLM-based prose
    # restructuring internally. No per-file Layer 2 logic needed.
    try:
        llm = _resolve_llm(ai_client)
        args_raw = await llm.call_with_tools(
            system_prompt=_AUDIT_SYSTEM_PROMPT,
            user_prompt=user_prompt,
            tool_name="report_element_inventory",
            tool_schema=_INVENTORY_TOOL,
            images=image_paths,
            temperature=0.1,
        )
    except Exception as e:
        logger.warning("PHASE 1 AUDIT [%s]: AI call failed: %s", section_name, e)
        return [], [], []

    if not isinstance(args_raw, dict):
        return [], [], []

    try:
        debug_dir = os.path.join(captures_dir, "capture_logs")
        os.makedirs(debug_dir, exist_ok=True)
        safe_name = section_name.replace(" ", "_")
        with open(
            os.path.join(debug_dir, f"phase1_{safe_name}_audit_response.json"),
            "w",
            encoding="utf-8",
        ) as f:
            json.dump(args_raw, f, indent=2, default=str, ensure_ascii=False)
    except Exception:
        logger.debug("Failed to write phase1 audit_response debug file", exc_info=True)

    batch = ElementInventory.from_dict(args_raw)
    elements = [elem for elem in batch.elements if isinstance(elem, InventoryElement)]
    for sel in args_raw.get("remove_selectors", []) or []:
        if isinstance(sel, str) and sel.strip():
            removals.append(sel.strip())
    for upd in args_raw.get("priority_updates", []) or []:
        if isinstance(upd, dict) and upd.get("selector"):
            priority_updates.append(upd)

    logger.info(
        "PHASE 1 AUDIT [%s]: AI added %d, removed %d, updated %d priorities",
        section_name,
        len(elements),
        len(removals),
        len(priority_updates),
    )
    return elements, removals, priority_updates


def _resolve_llm(ai_client):
    """Return an LLMClient from whatever the caller passed in."""
    from functions.llm import LLMClient

    if isinstance(ai_client, LLMClient):
        return ai_client
    inner = getattr(ai_client, "_llm", None)
    if isinstance(inner, LLMClient):
        return inner
    return LLMClient()


# Dead code removed: _run_inventory_loop, _single_call_analysis,
# _multi_call_analysis, _call_inventory_ai — replaced by deterministic
# extraction + single-call AI audit per section.



def _elem_to_log_dict(e: InventoryElement) -> dict:
    """Convert element to a compact dict for logging."""
    d = {"type": e.type, "selector": e.selector, "tag": e.tag, "visible": e.visible}
    if e.text:
        d["text"] = e.text
    if e.role:
        d["role"] = e.role
    if e.rect:
        d["rect"] = e.rect
    if e.exploration_priority != "low":
        d["priority"] = e.exploration_priority
    if e.exploration_actions:
        d["actions"] = e.exploration_actions
    return d
