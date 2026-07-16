"""Element inventory — AI-generated page element catalog.

Phase 1 produces an ElementInventory from AI analysis of the DOM.
This module defines the data structures and provides a mapper that
converts the inventory into the legacy CaptureData fields that the
existing check modules expect.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


def _infer_type(tag: str, role: str, href: str, src: str, input_type: str) -> str:
    """Infer element type from tag/role when the AI didn't provide one."""
    tag = (tag or "").lower()
    role = (role or "").lower()

    if tag in ("h1", "h2", "h3", "h4", "h5", "h6") or role == "heading":
        return "heading"
    if tag == "a" or role == "link":
        return "link"
    if tag == "button" or role == "button" or input_type in ("button", "submit", "reset"):
        return "button"
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
    if tag == "nav" or role == "navigation":
        return "landmark"
    if tag == "main" or role == "main":
        return "landmark"
    if tag == "header" or role == "banner":
        return "landmark"
    if tag == "footer" or role == "contentinfo":
        return "landmark"
    if tag == "aside" or role == "complementary":
        return "landmark"
    if role in ("region", "search", "form"):
        return "landmark"
    if role in ("menu", "menubar"):
        return "menu"
    if role in ("tab", "tablist"):
        return "tab_panel"
    if role in ("dialog", "alertdialog"):
        return "modal_trigger"
    if href:
        return "link"
    if src:
        return "image"
    return "custom_control"


@dataclass
class InventoryElement:
    """A single element identified by the AI."""
    type: str = ""              # "link", "button", "form_field", "image", etc.
    selector: str = ""          # CSS selector (unique)
    tag: str = ""               # HTML tag name
    text: str = ""              # Visible text content
    aria: dict = field(default_factory=dict)  # All ARIA attributes
    role: str = ""              # Explicit or implicit ARIA role
    rect: dict | None = None    # Bounding box {x, y, width, height}
    visible: bool = True        # Currently visible on page
    interactive: bool = False   # Accepts user interaction
    parent_landmark: str = ""   # Nearest containing landmark
    exploration_priority: str = "low"  # "high", "medium", "low"
    exploration_actions: list = field(default_factory=list)  # ["hover", "click", etc.]

    # Type-specific fields
    href: str = ""
    alt: str = ""
    src: str = ""
    input_type: str = ""
    label: str = ""
    required: bool = False
    autocomplete: str = ""
    tracks: list = field(default_factory=list)  # For media elements
    level: int = 0              # For headings (1-6)
    target: str = ""            # For links (_blank, etc.)
    title: str = ""             # title attribute
    placeholder: str = ""       # For form fields
    name: str = ""              # name attribute
    id: str = ""                # id attribute
    parent_tag: str = ""        # Parent element tag
    parent_role: str = ""       # Parent element role
    # For links / buttons — does the element wrap an <img>/<svg>/[role=img]?
    # Set by the deterministic phase 1 extractor; the link mapper used to
    # hard-code False here, which made every logo link (e.g. a university's
    # endorsed-logo <a><img alt="Example University."></a>) look
    # like a nameless link. The image_alt field captures the wrapped
    # image's alt so per-SC checks can apply ARIA 1.2 step 5 (wrapper's
    # accessible name = wrapped img alt).
    has_image: bool = False
    image_alt: str = ""
    context: str = ""           # Surrounding sentence for SC 2.4.4 link-purpose
    # Structured location data from functions.element_labeler.describe():
    # visible_text, accessible_name, preceding_heading, landmark, spatial.
    # Fed to the judge prompt via a LOCATION: line under each element so
    # findings reference "the 'Read more' link under the 'Students showcase'
    # heading in the <main> landmark" instead of "div:nth-of-type(9)".
    location: dict = field(default_factory=dict)

    @classmethod
    def from_dict(cls, d: dict) -> "InventoryElement":
        known = {f.name for f in cls.__dataclass_fields__.values()}
        filtered = {k: v for k, v in d.items() if k in known}
        elem = cls(**filtered)
        # Ensure aria is always a dict (AI sometimes returns None)
        if elem.aria is None:
            elem.aria = {}
        # If type is missing, infer from tag and role
        if not elem.type:
            elem.type = _infer_type(elem.tag, elem.role, elem.href, elem.src, elem.input_type)
        return elem


@dataclass
class ElementInventory:
    """Complete AI-generated element inventory for a page."""
    elements: list[InventoryElement] = field(default_factory=list)
    page_summary: str = ""
    page_type: str = ""  # "form-heavy", "content", "application", etc.
    sections_needing_deep_analysis: list = field(default_factory=list)
    estimated_interaction_count: int = 0

    @classmethod
    def from_dict(cls, d: dict) -> "ElementInventory":
        elements = [
            InventoryElement.from_dict(e) if isinstance(e, dict) else e
            for e in d.get("elements", [])
        ]
        return cls(
            elements=elements,
            page_summary=d.get("page_summary", ""),
            page_type=d.get("page_type", ""),
            sections_needing_deep_analysis=d.get("sections_needing_deep_analysis", []),
            estimated_interaction_count=d.get("estimated_interaction_count", 0),
        )

    def get_by_type(self, element_type: str) -> list[InventoryElement]:
        return [e for e in self.elements if e.type == element_type]

    def get_explorable(self) -> list[InventoryElement]:
        """Elements that need Phase 2 exploration, sorted by priority.

        Only returns VISIBLE elements — invisible elements (hidden modals,
        collapsed mobile menus, off-screen content) cannot be interacted
        with and will just produce "Element not visible" errors.
        """
        priority_order = {"high": 0, "medium": 1, "low": 2}
        explorable = [
            e for e in self.elements
            if e.visible
            and e.exploration_priority in ("high", "medium")
            and e.exploration_actions
        ]
        explorable.sort(key=lambda e: priority_order.get(e.exploration_priority, 2))
        logger.info("get_explorable: %d visible high/medium-priority elements "
                     "(filtered out %d invisible)",
                     len(explorable),
                     len([e for e in self.elements
                          if not e.visible
                          and e.exploration_priority in ("high", "medium")
                          and e.exploration_actions]))
        return explorable


def _merge_inventory_into_v1(
    existing: list, new_list: list, key: str = "selector",
) -> list:
    """Merge an inventory-mapped list of dicts into a v1-extracted list.

    The v2 inventory only knows the AI-classified shape of each element
    (type, role, aria attributes, location, AI-derived text). The v1
    extraction in capture/web_capture.py captures deeper HTML-level
    detail per element type — e.g.:

      - form_fields: in_fieldset, group_label, placeholder, autocomplete
      - images: screenshot_path, decorative_signals, vlm_caption,
        vlm_alt_similarity, naturalWidth/naturalHeight
      - media: HTML attributes (autoplay/loop/muted/controls — read
        via ``el.hasAttribute()``, NOT inferred from ARIA)
      - iframes: ariaHidden, ariaLabel, name attributes
      - landmarks / lists / tables: structural classification fields

    Earlier code REPLACED capture_data.<field> with the inventory's
    stripped-down dicts, dropping all the v1-only fields. Per-SC
    checks then evaluated against incomplete data — observed bugs:

      - SC 1.3.1: every radio reported in_fieldset=None (v2 mapper
        had no such field) so "radio not in fieldset" findings fired
        on radios that ARE inside a real ``<fieldset>``.
      - SC 1.4.2 / 2.2.2: hero ``<video autoplay muted loop>`` was
        captured as ``autoplay=False muted=False loop=False
        controls=True`` because v2 read those keys from ``e.aria``
        (an ARIA dict, not HTML attributes), inverting reality.

    This helper merges by selector: every v1 entry survives and gets
    augmented with the inventory's fields, with v1 fields winning on
    overlap. Inventory entries without a v1 match are appended
    unchanged.
    """
    if not existing:
        return list(new_list)
    if not new_list:
        return list(existing)
    existing_by_key = {(d.get(key) or ""): d for d in existing if isinstance(d, dict)}
    merged: list = []
    seen_keys: set[str] = set()
    for inv in new_list:
        if not isinstance(inv, dict):
            merged.append(inv)
            continue
        k = inv.get(key) or ""
        if k and k in existing_by_key:
            v1 = dict(existing_by_key[k])
            # Inventory fills any keys v1 didn't capture; v1 fields
            # win on overlap so HTML attributes are not clobbered.
            for inv_k, inv_v in inv.items():
                if inv_k not in v1 or v1.get(inv_k) in (None, "", [], {}):
                    v1[inv_k] = inv_v
            merged.append(v1)
            seen_keys.add(k)
        else:
            merged.append(inv)
            if k:
                seen_keys.add(k)
    # Preserve v1 entries the inventory didn't cover at all
    for k, v1 in existing_by_key.items():
        if k not in seen_keys:
            merged.append(v1)
    return merged


def _compute_heading_accname(selector: str, dom_html: str) -> str:
    """Compute a heading's accessible name from the captured DOM.

    Mirrors the JS __nameOf walk used by v1's _extract_elements
    (capture/web_capture.py): prefer aria-label, then aria-labelledby,
    then textContent, then nested <img alt> / [aria-label] /
    <svg><title>. Used by the v2 inventory mapper to fix headings whose
    accessible name comes from a nested image alt (e.g.
    `<h1><a><img alt="Site name"></a></h1>` whose textContent is "" but
    whose accname is "Site name"). General — works on any heading
    pattern, not site-specific.

    Returns "" when no name can be resolved or when parsing fails.
    """
    if not dom_html or not selector:
        return ""
    try:
        from bs4 import BeautifulSoup
    except ImportError:
        logger.debug(
            "bs4 not available — heading accname recompute skipped",
        )
        return ""
    try:
        soup = BeautifulSoup(dom_html, "lxml")
    except Exception:
        try:
            soup = BeautifulSoup(dom_html, "html.parser")
        except Exception:
            logger.debug(
                "DOM parse failed for heading accname recompute",
                exc_info=True,
            )
            return ""
    try:
        matches = soup.select(selector)
    except Exception:
        # Some compound selectors from the inventory may not be valid
        # CSS for soup. Try a tag-id fallback.
        matches = []
        if "#" in selector:
            ident = selector.split("#")[-1].split()[0].split(":")[0]
            target = soup.find(attrs={"id": ident})
            if target is not None:
                matches = [target]
    if not matches:
        return ""
    return _accname_of_element(matches[0], soup)


def _accname_of_element(el, soup) -> str:
    """Recursively compute an element's accessible name.

    Order (matches the W3C accname algorithm and v1's JS):
      1. aria-label
      2. aria-labelledby (concatenated referent textContent)
      3. textContent if non-empty
      4. concatenated descendant names — for each descendant, prefer
         aria-label, then alt for <img>, then <title> inside <svg>.
    Returns "" when nothing resolves.
    """
    if el is None:
        return ""
    al = (el.get("aria-label") or "").strip() if hasattr(el, "get") else ""
    if al:
        return al
    lb = (el.get("aria-labelledby") or "").strip() if hasattr(el, "get") else ""
    if lb:
        parts = []
        for tid in lb.split():
            target = soup.find(attrs={"id": tid})
            if target is not None:
                t = target.get_text(separator=" ", strip=True)
                if t:
                    parts.append(t)
        if parts:
            return " ".join(parts).strip()
    tc = el.get_text(separator=" ", strip=True) if hasattr(el, "get_text") else ""
    if tc:
        return tc
    bits: list[str] = []
    for d in el.find_all(True):
        if hasattr(d, "get"):
            inner_al = (d.get("aria-label") or "").strip()
            if inner_al:
                bits.append(inner_al)
                continue
            if d.name == "img":
                alt = (d.get("alt") or "").strip()
                if alt:
                    bits.append(alt)
                continue
            if d.name == "svg":
                t = d.find("title")
                if t is not None:
                    tt = t.get_text(separator=" ", strip=True)
                    if tt:
                        bits.append(tt)
                continue
    return " ".join(bits).strip()


def map_inventory_to_capture_data(inventory: ElementInventory, capture_data: Any) -> None:
    """Populate legacy CaptureData fields from the AI-generated inventory.

    This is the bridge between v2's AI-driven element discovery and
    v1's check modules that expect specific CaptureData fields.

    Each mapping produces dicts with the EXACT same keys the checks
    expect, then **merges** them into the existing v1-extracted lists
    via ``_merge_inventory_into_v1`` so HTML-attribute fields the
    inventory doesn't carry (autoplay, in_fieldset, screenshot_path,
    etc.) are preserved.
    """
    logger.info("INVENTORY MAPPER: mapping %d elements to CaptureData fields", len(inventory.elements))

    # Store the full inventory for Phase 2/3 consumption
    capture_data.element_inventory = {
        "elements": [_elem_to_dict(e) for e in inventory.elements],
        "page_summary": inventory.page_summary,
        "page_type": inventory.page_type,
    }
    capture_data.page_type = inventory.page_type

    # Map headings — and fix the empty-text case where the AI inventory's
    # textContent-only read missed the heading's accessible name. The
    # canonical accname rule (ARIA accname §4.3.7): if textContent is
    # empty, walk descendants for [aria-label] / <img alt> / <svg><title>.
    # Reading raw textContent on <h1><a class="logo"><img alt="Example University
    # home"></a></h1> returns "" and produced fabricated "empty h1"
    # findings on 1.3.1/1.3.2 — this recompute closes that gap for the
    # v2 path. Mirrors v1's __headingName JS in capture/web_capture.py.
    dom_html = getattr(capture_data, "html", "") or ""
    headings = []
    for e in inventory.get_by_type("heading"):
        level = e.level if 1 <= e.level <= 6 else 2  # Default to h2 if invalid
        text = (e.text or "").strip()
        text_content = text
        if not text and dom_html:
            recomputed = _compute_heading_accname(e.selector or "", dom_html)
            if recomputed:
                text = recomputed
        headings.append({
            "tag": e.tag or f"h{level}",
            "level": level,
            "text": text,
            "text_content": text_content,
            "id": e.id,
            "selector": e.selector,
            "location": e.location or {},
        })
    capture_data.headings = _merge_inventory_into_v1(
        capture_data.headings or [], headings,
    )
    logger.info("  Headings: %d", len(capture_data.headings))

    # Map links
    links = []
    for e in inventory.get_by_type("link"):
        links.append({
            "text": e.text,
            "href": e.href,
            "target": e.target,
            "aria_label": e.aria.get("aria-label", ""),
            "aria-label": e.aria.get("aria-label", ""),
            "aria_labelledby": e.aria.get("aria-labelledby", ""),
            "aria-labelledby": e.aria.get("aria-labelledby", ""),
            "title": e.title,
            "role": e.role,
            "selector": e.selector,
            "has_image": bool(e.has_image),
            "image_alt": e.image_alt or "",
            "context": e.context or "",
            "rect": e.rect or {},
            "visible": e.visible,
            "location": e.location or {},
        })
    capture_data.links = _merge_inventory_into_v1(
        capture_data.links or [], links, key="selector",
    )
    logger.info("  Links: %d", len(capture_data.links))

    # Map images
    images = []
    for e in inventory.get_by_type("image"):
        images.append({
            "src": e.src,
            "alt": e.alt,
            "role": e.role,
            "ariaLabel": e.aria.get("aria-label", ""),
            "aria-label": e.aria.get("aria-label", ""),
            "ariaHidden": e.aria.get("aria-hidden", ""),
            "width": (e.rect or {}).get("width", 0),
            "height": (e.rect or {}).get("height", 0),
            "isDecorative": e.role in ("presentation", "none") or e.alt == "",
            "selector": e.selector,
            "index": len(images),
            "rect": e.rect or {},
            "parent_tag": e.parent_tag,
            "parent_role": e.parent_role,
            "location": e.location or {},
        })
    capture_data.images = _merge_inventory_into_v1(
        capture_data.images or [], images, key="selector",
    )
    logger.info("  Images: %d", len(capture_data.images))

    # Map form fields
    form_fields = []
    for e in inventory.get_by_type("form_field"):
        form_fields.append({
            "tag": e.tag,
            "type": e.input_type,
            "name": e.name,
            "id": e.id,
            "label": e.label,
            "aria_label": e.aria.get("aria-label", ""),
            "aria-label": e.aria.get("aria-label", ""),
            "aria_labelledby": e.aria.get("aria-labelledby", ""),
            "aria-labelledby": e.aria.get("aria-labelledby", ""),
            "aria_describedby": e.aria.get("aria-describedby", ""),
            "aria-describedby": e.aria.get("aria-describedby", ""),
            "required": e.required,
            "placeholder": e.placeholder,
            "role": e.role,
            "autocomplete": e.autocomplete,
            "selector": e.selector,
            "title": e.title,
            "rect": e.rect or {},
            "visible": e.visible,
            "location": e.location or {},
        })
    capture_data.form_fields = _merge_inventory_into_v1(
        capture_data.form_fields or [], form_fields, key="selector",
    )
    logger.info("  Form fields: %d", len(capture_data.form_fields))

    # Map media — autoplay / loop / muted / controls are HTML attributes
    # (NOT ARIA), so we MUST NOT read them from e.aria. The v2 Phase D
    # extractor in capture/v2/html_extraction.py
    # (_capture_html_media_and_form_attrs) already captured these correctly
    # via el.hasAttribute(), using the same getSelector() algorithm as the
    # inventory so keys align.
    # _merge_inventory_into_v1 keeps v1 (HTML-attribute) values on overlap
    # and adds inventory-only fields (location, AI-classified pattern).
    # Inventory entries with no v1 match (rare — would mean the inventory
    # detected a media element v1 didn't see) get appended verbatim.
    inv_media = []
    for e in inventory.get_by_type("media"):
        inv_media.append({
            "tag": e.tag,
            "src": e.src,
            "tracks": e.tracks,
            "aria_label": e.aria.get("aria-label", ""),
            "aria-label": e.aria.get("aria-label", ""),
            "selector": e.selector,
            "rect": e.rect or {},
            "location": e.location or {},
        })
    capture_data.media = _merge_inventory_into_v1(
        capture_data.media or [], inv_media, key="selector",
    )
    logger.info("  Media: %d", len(capture_data.media))

    # Map landmarks
    landmarks = []
    for e in inventory.get_by_type("landmark"):
        landmarks.append({
            "tag": e.tag,
            "role": e.role,
            "aria_label": e.aria.get("aria-label", ""),
            "aria-label": e.aria.get("aria-label", ""),
            "aria_labelledby": e.aria.get("aria-labelledby", ""),
            "selector": e.selector,
            "rect": e.rect or {},
        })
    capture_data.landmarks = _merge_inventory_into_v1(
        capture_data.landmarks or [], landmarks, key="selector",
    )
    logger.info("  Landmarks: %d", len(capture_data.landmarks))

    # Map tables
    tables = []
    for e in inventory.get_by_type("table"):
        tables.append({
            "caption": e.text,
            "role": e.role,
            "aria_label": e.aria.get("aria-label", ""),
            "aria-label": e.aria.get("aria-label", ""),
            "selector": e.selector,
            "headers": [],  # Phase 1 may provide these
            "rowCount": 0,
        })
    capture_data.tables = _merge_inventory_into_v1(
        capture_data.tables or [], tables, key="selector",
    )
    logger.info("  Tables: %d", len(capture_data.tables))

    # Map lists
    lists = []
    for e in inventory.get_by_type("list"):
        lists.append({
            "tag": e.tag,
            "itemCount": 0,
            "role": e.role,
            "aria_label": e.aria.get("aria-label", ""),
            "selector": e.selector,
            "rect": e.rect or {},
        })
    capture_data.lists = _merge_inventory_into_v1(
        capture_data.lists or [], lists, key="selector",
    )
    logger.info("  Lists: %d", len(capture_data.lists))

    # Map iframes
    iframes = []
    for e in inventory.get_by_type("iframe"):
        iframes.append({
            "src": e.src,
            "title": e.title,
            "aria_label": e.aria.get("aria-label", ""),
            "aria-label": e.aria.get("aria-label", ""),
            "aria_hidden": e.aria.get("aria-hidden", ""),
            "width": (e.rect or {}).get("width", 0),
            "height": (e.rect or {}).get("height", 0),
            "name": e.name,
            "selector": e.selector,
            "location": e.location or {},
        })
    capture_data.iframes = _merge_inventory_into_v1(
        capture_data.iframes or [], iframes, key="selector",
    )
    logger.info("  Iframes: %d", len(capture_data.iframes))

    # Compose location_label on every inventory list the judge renders.
    # Logs WARNING for any item that went through without a label so
    # silent drops are attributable. Enrichment only fills labels that
    # are missing -- never overwrites a pre-composed label.
    try:
        from functions.element_labeler import ensure_label_fields
    except Exception:
        logger.exception("element_labeler import failed; LOCATION labels will be missing")
    else:
        total_composed = 0
        for name, items, required in (
            ("headings", headings, False),
            ("links", links, True),
            ("images", images, True),
            ("form_fields", form_fields, True),
            ("iframes", iframes, True),
        ):
            composed = ensure_label_fields(items, warn_prefix=f"Inventory[{name}]", required=required)
            total_composed += composed
            if items:
                logger.info(
                    "  %s: %d item(s), %d location_label(s) composed",
                    name, len(items), composed,
                )
        logger.info("Inventory labelling: %d total location_label(s) composed", total_composed)

    # Check YouTube/Vimeo captions for video embeds
    video_caption_data: dict = {}
    for iframe in iframes:
        src = (iframe.get("src") or "").lower()
        if "youtube.com" in src or "youtube-nocookie.com" in src or "youtu.be" in src:
            import re
            match = re.search(r"(?:embed|v)[/=]([a-zA-Z0-9_-]{11})", iframe.get("src", ""))
            if match:
                vid_id = match.group(1)
                try:
                    import httpx
                    resp = httpx.get(
                        f"https://www.youtube.com/api/timedtext?type=list&v={vid_id}",
                        timeout=10,
                    )
                    if resp.status_code == 200:
                        tracks = re.findall(r'lang_code="([^"]+)"', resp.text)
                        video_caption_data[vid_id] = {
                            "video_id": vid_id,
                            "has_captions": bool(tracks),
                            "caption_languages": tracks,
                        }
                        logger.info("  YouTube %s: captions=%s langs=%s",
                                    vid_id, bool(tracks), tracks)
                except Exception as exc:
                    logger.debug("  YouTube caption check failed for %s: %s", vid_id, exc)
    capture_data.video_embed_captions = video_caption_data

    # Map background images
    bg_images = []
    for e in inventory.get_by_type("background_image"):
        bg_images.append({
            "selector": e.selector,
            "tag": e.tag,
            "backgroundImage": e.src,
            "role": e.role,
            "aria_label": e.aria.get("aria-label", ""),
            "text_content": e.text,
        })
    capture_data.background_images = _merge_inventory_into_v1(
        capture_data.background_images or [], bg_images, key="selector",
    )
    logger.info("  Background images: %d", len(capture_data.background_images))

    # Map CAPTCHAs
    captchas = []
    for e in inventory.get_by_type("captcha"):
        captchas.append({
            "tag": e.tag,
            "type": e.text or "unknown",
            "aria_label": e.aria.get("aria-label", ""),
            "title": e.title,
            "selector": e.selector,
        })
    capture_data.captchas = _merge_inventory_into_v1(
        capture_data.captchas or [], captchas, key="selector",
    )
    logger.info("  CAPTCHAs: %d", len(capture_data.captchas))

    # Map skip links. The v2 inventory may not detect visually-hidden-
    # focusable patterns (Bootstrap 5 `class="visually-hidden-focusable"`
    # skip-to-main-content links are zero-rect until focused, so the
    # AI inventory often omits them). Phase D already populated
    # capture_data.skip_links via the deterministic regex-based
    # extractor in web_capture.py -- if THAT found entries, keep them
    # rather than clobbering with an empty list.
    inventory_skip_links = []
    for e in inventory.get_by_type("skip_link"):
        inventory_skip_links.append({
            "text": e.text,
            "href": e.href,
            "targetExists": True,  # Will be verified in Phase 3
            "selector": e.selector,
            "rect": e.rect or {},
        })
    existing = list(getattr(capture_data, "skip_links", []) or [])
    if inventory_skip_links:
        # Merge: union by selector / href so we don't double-count
        seen_keys = {(s.get("selector"), s.get("href")) for s in existing}
        for sl in inventory_skip_links:
            k = (sl.get("selector"), sl.get("href"))
            if k not in seen_keys:
                existing.append(sl)
        capture_data.skip_links = existing
    elif not existing:
        # Both empty -- write the empty list explicitly so consumers
        # see a defined attribute.
        capture_data.skip_links = []
    # If existing is non-empty and inventory found 0, leave existing
    # in place (Phase D's regex extractor caught it).
    logger.info(
        "  Skip links: %d (Phase-D=%d, inventory=%d)",
        len(capture_data.skip_links or []),
        len(existing) - len(inventory_skip_links) if inventory_skip_links else len(existing),
        len(inventory_skip_links),
    )

    total = sum(len(getattr(capture_data, attr, []))
                for attr in ("headings", "links", "images", "form_fields",
                             "media", "landmarks", "tables", "lists",
                             "iframes", "captchas", "skip_links"))
    logger.info("INVENTORY MAPPER: %d total elements mapped to CaptureData", total)


def _elem_to_dict(e: InventoryElement) -> dict:
    """Convert an InventoryElement to a plain dict for JSON serialization."""
    d = {}
    for f in e.__dataclass_fields__:
        v = getattr(e, f)
        if v or v == 0 or v is False:  # Include falsy but meaningful values
            d[f] = v
    return d
