"""DOM-context formatting helpers shared by checks and judge prompts.

These produce the human-readable location / element / landmark phrases
that appear in finding text and in the DOM context block sent to the
judge. Plain-English output by design — no CSS selectors, no
angle-bracketed tag names, no debug noise.

Originally lived in `checks/base.py` (lines 107–323). Extracted here so
multiple subsystems (checks, judge prompts, AT-sim summarizers) can
share the same vocabulary.
"""
from __future__ import annotations


_LANDMARK_ENGLISH: dict[str, str] = {
    "banner": "site header at the top of the page",
    "header": "site header at the top of the page",
    "navigation": "navigation menu",
    "nav": "navigation menu",
    "main": "main content area",
    "complementary": "sidebar",
    "aside": "sidebar",
    "contentinfo": "main site footer at the bottom of the page",
    "footer": "main site footer at the bottom of the page",
    "search": "site search area",
    "form": "form",
    "dialog": "dialog overlay",
    "alertdialog": "alert dialog",
    "region": "section",
}


def _landmark_phrase(landmark: dict) -> str | None:
    """Translate a captured landmark dict into a natural English phrase.

    Prefers the landmark's aria-label when it adds specificity (e.g.
    "Primary navigation menu" beats just "navigation menu"). Falls back to
    the role-to-English mapping above.
    """
    role = (landmark.get("role") or "").lower()
    tag = (landmark.get("tag") or "").lower()
    label = (landmark.get("ariaLabel") or landmark.get("aria-label") or "").strip()
    base = _LANDMARK_ENGLISH.get(role) or _LANDMARK_ENGLISH.get(tag)
    if not base:
        return None
    if label:
        # If the label already names the type (e.g. "Footer navigation"),
        # use it verbatim. Otherwise weave it in: "primary navigation menu".
        label_lower = label.lower()
        if any(word in label_lower for word in (
            "navigation", "menu", "header", "footer", "main", "sidebar",
            "search", "form", "dialog",
        )):
            return label.lower()
        return f"{label.lower()} {base}"
    return base


def _vertical_zone(cy: float, page_height: float) -> str:
    """Human label for an element's vertical position on the captured page."""
    if page_height <= 0:
        return ""
    ratio = cy / page_height
    if ratio < 0.20:
        return "near the top of the page"
    if ratio < 0.45:
        return "in the upper portion of the page"
    if ratio > 0.80:
        return "near the bottom of the page"
    if ratio > 0.60:
        return "in the lower portion of the page"
    return "in the middle of the page"


def _nearest_landmark(rect: dict, landmarks: list[dict]) -> dict | None:
    """Pick the landmark whose rect contains, or is closest to, the element."""
    cx = rect.get("x", 0) + rect.get("width", 0) / 2
    cy = rect.get("y", 0) + rect.get("height", 0) / 2
    containing: list[tuple[float, dict]] = []
    nearest: tuple[float, dict] | None = None
    for lm in landmarks or []:
        lm_rect = lm.get("rect")
        if not lm_rect:
            continue
        lx = lm_rect.get("x", 0)
        ly = lm_rect.get("y", 0)
        lw = lm_rect.get("width", 0)
        lh = lm_rect.get("height", 0)
        if lw <= 0 or lh <= 0:
            continue
        if lx <= cx <= lx + lw and ly <= cy <= ly + lh:
            # Containing landmark; smaller is more specific.
            containing.append((lw * lh, lm))
            continue
        lm_cy = ly + lh / 2
        dist = abs(cy - lm_cy)
        if nearest is None or dist < nearest[0]:
            nearest = (dist, lm)
    if containing:
        containing.sort(key=lambda t: t[0])
        return containing[0][1]
    return nearest[1] if nearest else None


def _nearest_section_heading(
    rect: dict, headings: list[dict] | None,
) -> str:
    """Find the most recent heading visually preceding the element.

    Walks the captured headings list (assumed in document order with rects)
    and picks the nearest one whose y-position is at or above the element.
    Returns the heading's visible text or "" if none qualifies.
    """
    if not headings:
        return ""
    elem_y = rect.get("y", 0)
    best_text = ""
    best_y = -1.0
    for h in headings:
        h_rect = h.get("rect")
        if not h_rect:
            continue
        hy = h_rect.get("y", 0)
        if hy > elem_y:
            continue
        if hy <= best_y:
            continue
        text = (h.get("text") or "").strip()
        if not text or len(text) > 80:
            continue
        best_y = hy
        best_text = text
    return best_text


def _element_phrase(text: str, tag: str) -> str:
    """Plain-English phrase for the element itself.

    Examples:
      ("Search", "button")     → 'on the "Search" button'
      ("Read more", "a")       → 'on the "Read more" link'
      ("", "img")              → "on an image"
      ("", "input")            → "on a form field"
    """
    role_map = {
        "a": "link", "button": "button", "input": "form field",
        "select": "dropdown", "textarea": "text area", "img": "image",
        "video": "video", "audio": "audio player", "iframe": "embedded frame",
        "h1": "heading", "h2": "heading", "h3": "heading",
        "h4": "heading", "h5": "heading", "h6": "heading",
        "li": "list item", "label": "label",
    }
    role = role_map.get((tag or "").lower(), "element")
    text_clean = (text or "").strip()
    if text_clean:
        snippet = text_clean
        article = "an" if snippet[0].lower() in "aeiou" else "a"
        if role in ("link", "button", "image", "heading"):
            return f'on the "{snippet}" {role}'
        return f"on the {role} labeled \"{snippet}\""
    article = "an" if role[0] in "aeiou" else "a"
    return f"on {article} {role}"


def _describe_location(
    selector: str,
    rect: dict | None = None,
    text: str = "",
    tag: str = "",
    landmarks: list[dict] | None = None,
    headings: list[dict] | None = None,
    page_height: float = 0,
) -> str:
    """Compose a single human-readable sentence describing where an element
    sits on the page.

    The sentence reads as if a Trusted Tester wrote it — no CSS selectors,
    no angle-bracketed tag names, no debug parts. Examples:

      "Near the top of the page in the primary navigation menu, on the
       'Mobile menu toggle' button."
      "In the main site footer at the bottom of the page, on the 'Facebook'
       link."
      "In the middle of the page inside the 'Find my degree program'
       section, on the 'In person' radio field."

    Returns "" if there's nothing locatable (no rect AND no landmarks AND
    no useful text) so callers can fall back to the existing element field
    rather than emit a meaningless sentence.
    """
    if not rect and not landmarks and not text and not tag:
        return ""

    pieces: list[str] = []

    landmark = _nearest_landmark(rect, landmarks) if rect and landmarks else None
    landmark_text = _landmark_phrase(landmark) if landmark else None

    if rect and page_height:
        cy = rect.get("y", 0) + rect.get("height", 0) / 2
        zone = _vertical_zone(cy, page_height)
        # Skip the zone phrase when the landmark already implies position
        # (e.g. "site header at the top" → don't also say "near the top").
        if landmark_text and ("top of the page" in landmark_text
                              or "bottom of the page" in landmark_text):
            zone = ""
        if zone:
            pieces.append(zone.capitalize())

    if landmark_text:
        if pieces:
            pieces.append(f"in the {landmark_text}")
        else:
            pieces.append(f"In the {landmark_text}")

    if rect:
        section = _nearest_section_heading(rect, headings)
        if section:
            pieces.append(f"inside the \"{section}\" section")

    elem_phrase = _element_phrase(text, tag)
    if elem_phrase:
        pieces.append(elem_phrase)

    if not pieces:
        return ""

    sentence = ", ".join(pieces).rstrip(", ")
    if not sentence.endswith("."):
        sentence += "."
    return sentence


def _font_weight_int(value) -> int:
    """Coerce a CSS font-weight (numeric string, keyword, or int) to int.

    Handles the keywords browsers accept (`normal`, `bold`, `bolder`,
    `lighter`) by mapping them to their numeric equivalents per the
    CSS Fonts spec. Unknown / missing values default to 400 (normal).
    """
    try:
        return int(value)
    except (ValueError, TypeError):
        return {
            "normal": 400, "bold": 700, "bolder": 700, "lighter": 300,
        }.get(str(value).lower(), 400)


def format_link_styling_measurements(links: list[dict]) -> list[str]:
    """Render per-link styling data as a labeled prompt block for SC 1.4.1.

    Without this block, the judge sees small screenshots where an
    underline on an in-paragraph link is hard to perceive at thumbnail
    resolution, and it falls back to its prior ("a coloured link
    without an obvious underline must be color-only"). With this
    block, every in-paragraph link has its computed text-decoration /
    border / icon presence / font-weight / font-style listed plus a
    deterministic PASS/FAIL.

    Deterministic rule: an in-paragraph link PASSES SC 1.4.1 when it
    has at least one non-colour distinguisher — has_underline OR
    has_border OR has_icon OR a font-weight numerically heavier than
    the surrounding paragraph OR a non-'normal' font-style. It FAILS
    only when colour is the sole differentiator. Links outside prose
    (nav menus, link lists, button-styled links) are not flagged —
    SC 1.4.1's in-text-link clause only applies to inline prose.

    Input: ``links`` is the list ``capture_data.links`` produced by the
    Phase D link extractor. Each entry is expected to carry the fields
    ``selector``, ``text``, ``in_paragraph``, ``has_underline``,
    ``has_border``, ``has_icon``, ``font_weight``, ``font_style``,
    ``color``, ``surrounding_color``.
    """
    # Only in-paragraph links are in scope for the link-colour clause
    # of SC 1.4.1. Phase D's extractor sets in_paragraph=true only
    # when the parent is p/li/td/th/dd/dt AND the parent text is
    # longer than the link text (so the link is one of multiple
    # text fragments, not a single-link list item).
    inline = [lk for lk in (links or []) if lk.get("in_paragraph")]
    if not inline:
        return [
            "[LINK STYLING for SC 1.4.1] No in-paragraph (in-text) "
            "links captured on this page. SC 1.4.1's in-text-link "
            "clause is therefore Not Applicable. The judge MUST NOT "
            "manufacture an in-paragraph link-colour finding."
        ]

    lines: list[str] = []
    lines.append(
        f"[LINK STYLING for SC 1.4.1] {len(inline)} in-paragraph "
        "link(s). For each, the deterministic verdict reflects whether "
        "the link is distinguished from surrounding text by ANY non-"
        "colour cue (underline, border, icon, heavier weight, or "
        "italic). REJECT FINDINGS THAT CONTRADICT THESE MEASUREMENTS — "
        "they were read directly from the live page's computed styles."
    )
    lines.append("")
    pass_count = 0
    fail_count = 0
    for lk in inline:
        sel = lk.get("selector", "a")
        text = (lk.get("text") or "").strip()
        has_underline = bool(lk.get("has_underline"))
        has_border = bool(lk.get("has_border"))
        has_icon = bool(lk.get("has_icon"))
        link_weight = _font_weight_int(lk.get("font_weight"))
        link_style = (lk.get("font_style") or "normal").lower()
        color = lk.get("color") or ""
        surrounding_color = lk.get("surrounding_color") or ""
        # Surrounding paragraph weight defaults to 400 (normal). The
        # link is distinguishably heavier when its weight is at least
        # 100 above the default.
        heavier_weight = link_weight >= 500
        non_normal_style = link_style not in ("", "normal")

        cues: list[str] = []
        if has_underline:
            cues.append("underline")
        if has_border:
            cues.append("border")
        if has_icon:
            cues.append("icon")
        if heavier_weight:
            cues.append(f"weight={link_weight}")
        if non_normal_style:
            cues.append(f"style={link_style}")

        if cues:
            verdict = f"PASS — non-colour cue(s): {', '.join(cues)}"
            pass_count += 1
        else:
            verdict = (
                "FAIL — distinguished only by colour "
                f"(link={color or '?'}, surrounding={surrounding_color or '?'}); "
                "no underline, border, icon, heavier weight, or italic"
            )
            fail_count += 1
        label = text or sel
        lines.append(
            f"  link \"{label}\" "
            f"underline={str(has_underline).lower()} "
            f"border={str(has_border).lower()} "
            f"icon={str(has_icon).lower()} "
            f"weight={link_weight} style={link_style}"
        )
        lines.append(f"    selector: {sel}")
        lines.append(f"    -> {verdict}")

    lines.append("")
    lines.append(
        f"DETERMINISTIC VERDICT: {pass_count} link(s) pass, "
        f"{fail_count} link(s) fail. The judge MUST NOT emit a "
        f"colour-only-link finding against any link listed as PASS — "
        f"the page already encodes a non-colour distinguisher. "
        f"Findings are only legitimate for FAIL entries above (if any)."
    )
    return lines
