"""HTML attribute extraction for media and form fields.

Extracts HTML attributes for ``<video>``/``<audio>``/``<input>``/
``<select>``/``<textarea>`` directly from the live DOM, alongside
landmark and list metadata. These are HTML attributes (autoplay, muted,
loop, controls, in_fieldset, placeholder, etc.) — NOT ARIA attributes —
so the element-inventory mapper cannot infer them from ``e.aria``.

Result: ``capture_data.media``, ``capture_data.form_fields``,
``capture_data.landmarks``, and ``capture_data.lists`` are seeded with
HTML-attribute-correct entries. ``map_inventory_to_capture_data`` later
merges in AI-classified fields (role, label, location) on top via
``_merge_inventory_into_v1``, preserving these attributes.
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


async def _capture_html_media_and_form_attrs(page, capture_data) -> None:
    """Extract HTML attributes for <video>/<audio>/<input>/<select>/
    <textarea> directly from the live DOM.

    These are HTML attributes (autoplay, muted, loop, controls, in_fieldset,
    placeholder, etc.) — NOT ARIA attributes — so the element-inventory
    mapper cannot infer them from ``e.aria``. Earlier code tried, and on
    a university's hero ``<video autoplay muted loop>`` reported every attribute
    inverted because the inventory dict had no such keys.

    Result: ``capture_data.media`` and ``capture_data.form_fields`` are
    seeded with HTML-attribute-correct entries. ``map_inventory_to_capture_data``
    later merges in AI-classified fields (role, label, location) on top
    via ``_merge_inventory_into_v1``, preserving these attributes.
    """
    # Shared selector builder identical to inventory's getSelector
    # (capture/v2/phase1_code_analysis.py:139-201) so v2 entries collide on
    # the same key as inventory entries during _merge_inventory_into_v1.
    # Without alignment, an element with no id collapses to ``selector=tag``
    # in v1 while inventory produces the full ``a > b:nth-of-type(2) > c``
    # path → no match → either v1 entries get dropped (collapsed by repeated
    # key) or duplicate entries appear in the output. A university's six <ul> nodes
    # without ids and the six <nav> landmarks both hit this pattern.
    SELECTOR_HELPER = r"""
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
                        const idx = sibs.indexOf(current) + 1;
                        seg += ':nth-of-type(' + idx + ')';
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

    # Media: <audio> / <video> with their HTML attributes
    media_items = await page.evaluate("""() => {""" + SELECTOR_HELPER + """
        const items = [];
        for (const tag of ['audio', 'video']) {
            for (const el of document.querySelectorAll(tag)) {
                const tracks = Array.from(el.querySelectorAll('track')).map(t => ({
                    kind: t.getAttribute('kind') || '',
                    src: t.getAttribute('src') || '',
                    srclang: t.getAttribute('srclang') || '',
                    label: t.getAttribute('label') || '',
                }));
                const r = el.getBoundingClientRect();
                items.push({
                    tag: tag,
                    src: el.getAttribute('src') ||
                         (el.querySelector('source')
                          ? el.querySelector('source').getAttribute('src') || ''
                          : ''),
                    autoplay: el.hasAttribute('autoplay'),
                    loop: el.hasAttribute('loop'),
                    muted: el.hasAttribute('muted'),
                    controls: el.hasAttribute('controls'),
                    tracks: tracks,
                    aria_label: el.getAttribute('aria-label') || '',
                    selector: getSelector(el),
                    rect: { x: r.x, y: r.y, width: r.width, height: r.height },
                    duration: el.duration || 0,
                });
            }
        }
        return items;
    }""")
    capture_data.media = media_items
    if media_items:
        first = media_items[0]
        logger.info(
            "PHASE D: Media: %d (first: %s autoplay=%s muted=%s loop=%s controls=%s tracks=%d)",
            len(media_items), first.get("tag"), first.get("autoplay"),
            first.get("muted"), first.get("loop"), first.get("controls"),
            len(first.get("tracks") or []),
        )
    else:
        logger.info("PHASE D: Media: 0")

    # Form fields with full HTML / ARIA / fieldset context
    form_items = await page.evaluate("""() => {""" + SELECTOR_HELPER + """
        const SKIP_TYPES = new Set(['hidden', 'submit', 'button', 'reset', 'image']);
        const items = [];
        const seen = new WeakSet();
        for (const el of document.querySelectorAll('input, select, textarea')) {
            if (seen.has(el)) continue;
            seen.add(el);
            const tag = el.tagName.toLowerCase();
            const type = (el.getAttribute('type') || '').toLowerCase();
            if (tag === 'input' && SKIP_TYPES.has(type)) continue;

            // Resolve the canonical accessible name source: <label for>,
            // wrapping <label>, aria-label, aria-labelledby, title, or
            // placeholder (last-resort).
            let label = '';
            if (el.id) {
                const lbl = document.querySelector('label[for="' + el.id.replace(/"/g, '\\\\"') + '"]');
                if (lbl) label = (lbl.textContent || '').trim();
            }
            if (!label) {
                const wrap = el.closest('label');
                if (wrap) label = (wrap.textContent || '').trim();
            }

            // Fieldset + legend (radio/checkbox grouping). Drupal wraps
            // <legend> inside a <div class="card-header">, so the legend
            // is not a *direct* child of the fieldset — querySelector
            // walks descendants. We deliberately use the fieldset's own
            // querySelector (not document.querySelector) so a nested
            // fieldset's legend can't bleed into the outer one.
            const fieldset = el.closest('fieldset');
            const inFieldset = !!fieldset;
            let groupLabel = '';
            if (fieldset) {
                const legend = fieldset.querySelector('legend');
                if (legend) groupLabel = (legend.textContent || '').trim();
            }

            const r = el.getBoundingClientRect();
            items.push({
                tag: tag,
                type: type,
                name: el.getAttribute('name') || '',
                id: el.id || '',
                label: label,
                aria_label: el.getAttribute('aria-label') || '',
                aria_labelledby: el.getAttribute('aria-labelledby') || '',
                aria_describedby: el.getAttribute('aria-describedby') || '',
                placeholder: el.getAttribute('placeholder') || '',
                title: el.getAttribute('title') || '',
                required: el.hasAttribute('required'),
                autocomplete: el.getAttribute('autocomplete') || '',
                role: el.getAttribute('role') || '',
                selector: getSelector(el),
                in_fieldset: inFieldset,
                group_label: groupLabel,
                rect: { x: r.x, y: r.y, width: r.width, height: r.height },
            });
        }
        return items;
    }""")
    capture_data.form_fields = form_items
    n_radio_in_fs = sum(
        1 for f in form_items
        if (f.get("type") or "") == "radio" and f.get("in_fieldset")
    )
    logger.info(
        "PHASE D: Form fields: %d (radios in fieldset: %d)",
        len(form_items), n_radio_in_fs,
    )

    # Landmarks: explicit role= attributes AND implicit HTML5 sectioning
    # elements mapped to canonical ARIA landmark roles.
    # Without this v2 was relying on the AI inventory which reports the
    # tag name as ``role`` (so <header> got role='header' instead of
    # 'banner', <nav> got role='nav' instead of 'navigation'). The
    # SC 1.3.1 LANDMARK_ROLES check filters by canonical role, so all
    # implicit-role landmarks were silently skipped from duplicate
    # detection.
    landmark_items = await page.evaluate(r"""() => {""" + SELECTOR_HELPER + r"""
        const roles = ['banner', 'navigation', 'main', 'complementary',
                       'contentinfo', 'search', 'form', 'region'];
        const items = [];
        const seen = new WeakSet();
        for (const role of roles) {
            for (const el of document.querySelectorAll('[role="' + role + '"]')) {
                if (seen.has(el)) continue;
                seen.add(el);
                const r = el.getBoundingClientRect();
                items.push({
                    tag: el.tagName.toLowerCase(),
                    role: role,
                    aria_label: el.getAttribute('aria-label') || '',
                    'aria-label': el.getAttribute('aria-label') || '',
                    aria_labelledby: el.getAttribute('aria-labelledby') || '',
                    selector: getSelector(el),
                    rect: { x: r.x, y: r.y, width: r.width, height: r.height },
                });
            }
        }
        // Implicit landmark elements per HTML5/ARIA mapping.
        // <header>/<footer>/<aside> are scoped to top-level — when nested
        // inside <article>/<aside>/<main>/<nav>/<section> they are NOT
        // landmarks (they're section headers/footers). <nav>/<main> are
        // unconditional landmarks regardless of nesting.
        const SCOPED = new Set(['header', 'footer', 'aside']);
        const SECTIONING = new Set(['article', 'aside', 'main', 'nav', 'section']);
        const mapping = { header: 'banner', nav: 'navigation', main: 'main',
                          aside: 'complementary', footer: 'contentinfo' };
        const hasSectioningAncestor = (el) => {
            let p = el.parentElement;
            while (p) {
                if (SECTIONING.has(p.tagName.toLowerCase())) return true;
                p = p.parentElement;
            }
            return false;
        };
        for (const [tag, role] of Object.entries(mapping)) {
            for (const el of document.querySelectorAll(tag)) {
                if (seen.has(el)) continue;
                if (el.getAttribute('role')) continue;  // explicit role wins
                if (SCOPED.has(tag) && hasSectioningAncestor(el)) continue;
                seen.add(el);
                const r = el.getBoundingClientRect();
                items.push({
                    tag: tag,
                    role: role,  // canonical ARIA role, not tag name
                    aria_label: el.getAttribute('aria-label') || '',
                    'aria-label': el.getAttribute('aria-label') || '',
                    aria_labelledby: el.getAttribute('aria-labelledby') || '',
                    selector: getSelector(el),
                    rect: { x: r.x, y: r.y, width: r.width, height: r.height },
                });
            }
        }
        // Per ARIA spec, <section> and <form> elements promote to landmark
        // roles (`region` and `form` respectively) ONLY when they have an
        // accessible name via aria-label or aria-labelledby. Without a
        // name they're generic — not landmarks. A university's <section
        // id="main-slider" aria-label="University Rankings Highlights">
        // is a region landmark; the inventory misreports it as
        // role='section'.
        const NAMED_PROMOTION = { section: 'region', form: 'form' };
        for (const [tag, role] of Object.entries(NAMED_PROMOTION)) {
            for (const el of document.querySelectorAll(tag)) {
                if (seen.has(el)) continue;
                if (el.getAttribute('role')) continue;
                const name = el.getAttribute('aria-label') ||
                             el.getAttribute('aria-labelledby');
                if (!name) continue;  // no accessible name → not a landmark
                seen.add(el);
                const r = el.getBoundingClientRect();
                items.push({
                    tag: tag,
                    role: role,
                    aria_label: el.getAttribute('aria-label') || '',
                    'aria-label': el.getAttribute('aria-label') || '',
                    aria_labelledby: el.getAttribute('aria-labelledby') || '',
                    selector: getSelector(el),
                    rect: { x: r.x, y: r.y, width: r.width, height: r.height },
                });
            }
        }
        return items;
    }""")
    capture_data.landmarks = landmark_items
    from collections import Counter as _Counter
    role_counts = _Counter(l.get("role") for l in landmark_items)
    logger.info(
        "PHASE D: Landmarks: %d (%s)",
        len(landmark_items),
        ", ".join(f"{r}={n}" for r, n in role_counts.most_common()),
    )

    # Lists: <ul>/<ol>/<dl> with their child counts. The element-inventory
    # mapper sets itemCount=0 (it has no DOM access), so v2 was emitting
    # all-zero counts. Walk the DOM directly to count <li> / <dt>+<dd>.
    list_items = await page.evaluate(r"""() => {""" + SELECTOR_HELPER + r"""
        const items = [];
        for (const el of document.querySelectorAll('ul, ol, dl')) {
            const tag = el.tagName.toLowerCase();
            let count = 0;
            if (tag === 'ul' || tag === 'ol') {
                count = el.querySelectorAll(':scope > li').length;
            } else {
                count = el.querySelectorAll(':scope > dt, :scope > dd').length;
            }
            const r = el.getBoundingClientRect();
            items.push({
                tag: tag,
                itemCount: count,
                role: el.getAttribute('role') || '',
                aria_label: el.getAttribute('aria-label') || '',
                aria_labelledby: el.getAttribute('aria-labelledby') || '',
                selector: getSelector(el),
                rect: { x: r.x, y: r.y, width: r.width, height: r.height },
            });
        }
        return items;
    }""")
    capture_data.lists = list_items
    empty_lists = sum(1 for l in list_items if l.get("itemCount", 0) == 0)
    logger.info(
        "PHASE D: Lists: %d (empty: %d)", len(list_items), empty_lists,
    )
