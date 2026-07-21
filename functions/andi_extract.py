"""ANDI-style finding extractors (contrast, lang, interactive, tables, graphics, hidden).

Extracted from `checks/base.py:BaseCheck` so the per-source extraction
logic is reusable and testable independently of the BaseCheck instance.
"""
from __future__ import annotations

from models import CaptureData, Finding, Severity
from functions.finding_utils import _make_finding_id


# SCs that legitimately care about focusable-but-hidden elements.
# hANDI findings only reach the run loop for these criteria.
_ANDI_HIDDEN_SCS = {
    "1.3.1",  # Info & Relationships — hidden/visible mismatch
    "2.1.1",  # Keyboard — phantom tab stops
    "2.4.3",  # Focus Order — order broken by hidden focusables
    "2.4.7",  # Focus Visible — focus on invisible element
    "4.1.2",  # Name/Role/Value — aria-hidden on focusable = spec violation
}


def is_browser_handled(hidden_entry: dict) -> bool:
    """True when the browser keeps this ANDI hidden-content entry OUT of the
    tab order regardless of authoring -- zero rect, display:none,
    visibility:hidden, the hidden attribute, or inert -- OR ANDI itself
    flagged it tab_reachable=False. A keyboard/focus finding on such an
    element describes a focus leak that does not exist.

    Single source of truth for both the judge DOM-context annotation and the
    server-side finding filter in checks/base.py. They previously used
    different criteria (the filter checked only tab_reachable), so a
    visibility:hidden element whose ANDI tab_reachable heuristic wrongly read
    True got annotated [BROWSER-HANDLED] but its findings were NOT dropped --
    producing repeat false positives across 1.3.1/2.1.1/2.1.3/2.4.3/2.4.7/4.1.2.
    """
    if not isinstance(hidden_entry, dict):
        return False
    rect = hidden_entry.get("rect") or {}
    try:
        w = float(rect.get("width") or 0)
        h = float(rect.get("height") or 0)
    except (ValueError, TypeError):
        w = h = 0.0
    if w == 0 and h == 0:
        return True
    if hidden_entry.get("tab_reachable") is False:
        return True
    reasons = hidden_entry.get("hidden_reasons") or []
    return any(
        r in ("display:none", "visibility:hidden", "hidden attribute")
        or (isinstance(r, str) and r.startswith("inert"))
        for r in reasons
    )


def extract_andi_contrast_findings(capture_data: CaptureData, criterion_id: str) -> list[Finding]:
    """Extract ANDI-style per-text-node contrast findings.

    Only emits findings for the two contrast SCs:

    - SC 1.4.3 Contrast (Minimum) (AA): 4.5:1 normal, 3.0:1 large
    - SC 1.4.6 Contrast (Enhanced) (AAA): 7.0:1 normal, 4.5:1 large

    For every other SC this returns an empty list. The ANDI extractor
    in ``capture/web_capture.py`` already computed the per-entry
    ``ratio`` against the AA threshold; for 1.4.6 we recompute pass /
    fail against the AAA threshold using the same recorded ratio.

    Severity:
    - HIGH when ratio < required * 0.5 (severely low — illegible)
    - MEDIUM when required * 0.5 <= ratio < required (sub-threshold)
    - INFO when ``bg_image_present`` is True (manual review — the
      resolved-via-walk-up ancestor colour likely isn't what the
      user actually sees)

    Entries with ``passes is None`` (fg or bg colour couldn't be
    parsed) are skipped — the ANDI logger already records counts
    of unmeasurable nodes.
    """
    if criterion_id not in ("1.4.3", "1.4.6"):
        return []

    results = getattr(capture_data, "andi_contrast_results", None) or []
    if not results:
        return []

    is_aaa = (criterion_id == "1.4.6")

    findings: list[Finding] = []
    for entry in results:
        ratio = entry.get("ratio")
        if ratio is None:
            continue

        # Skip visually-hidden text (icon-only links with sr-only
        # labels rendered in the same colour as their background, the
        # standard "University Name on Bluesky" / "Skip to
        # main content" pattern). When fg and bg are bit-identical
        # the user literally cannot see the text — it's intentionally
        # invisible for screen-reader-only consumption — so 1.4.3 /
        # 1.4.6 (which govern *visual* contrast of text) do not
        # apply. Without this guard every visually-hidden link text
        # gets flagged at 1.00:1.
        fg_rgb = entry.get("fg_color")
        bg_rgb = entry.get("bg_color")
        if (
            isinstance(fg_rgb, (list, tuple))
            and isinstance(bg_rgb, (list, tuple))
            and len(fg_rgb) >= 3 and len(bg_rgb) >= 3
            and tuple(fg_rgb[:3]) == tuple(bg_rgb[:3])
        ):
            continue

        is_large = bool(entry.get("is_large_text", False))
        if is_aaa:
            required = 4.5 if is_large else 7.0
        else:
            required = 3.0 if is_large else 4.5

        selector = entry.get("selector", "")
        text = entry.get("text", "") or ""
        text_preview = text
        fg = entry.get("fg_color_raw", "")
        bg = entry.get("bg_color_raw", "")
        walk_depth = entry.get("bg_walk_depth", 0)
        bg_image = bool(entry.get("bg_image_present", False))
        is_svg = bool(entry.get("is_svg_text", False))
        size_desc = "large" if is_large else "normal"
        sc_label = "1.4.6 (AAA)" if is_aaa else "1.4.3 (AA)"

        if bg_image:
            # CRITICAL: do NOT include the computed ratio number in this
            # finding's prose. The bg-color walk landed on a fallback
            # ancestor whose colour is NOT what the user actually sees
            # (the rendered backdrop is an image / video / gradient), so
            # the computed ratio is INFORMATIONAL ONLY -- the actual
            # rendered contrast can be wildly different.
            #
            # Past behaviour cited the unreliable ratio in prose
            # ("Computed ratio: 1.23:1") and the fast-path judge in
            # VPAT-synthesis mode re-quoted that number in HIGH-severity
            # output findings, treating the unreliable fallback ratio as
            # ground truth (verified on a university-site 2026-05-09 SC 1.4.3 run where
            # 3 HIGH findings citing 1.23:1 emerged from INFO-only
            # input). Removing the number from the prose physically
            # prevents that pattern: if the model never sees the
            # number, it cannot quote it. Mirrors the same physical
            # filtering already applied to the ANDI CONTRAST -- MANUAL
            # REVIEW block in checks/base.py:_build_dom_context which
            # also intentionally omits the unreliable ratios.
            findings.append(Finding(
                id=_make_finding_id(),
                element=selector,
                css_selector=selector,
                issue=(
                    f"ANDI: contrast cannot be reliably verified for "
                    f"{size_desc} text under WCAG SC {sc_label} -- the "
                    f"text sits over a background image, gradient, or "
                    f"video and the deterministic bg-color walk "
                    f"resolved to a fallback ancestor colour ({walk_depth} "
                    f"ancestor(s) up). The ratio computed against that "
                    f"fallback is unreliable and is intentionally not "
                    f"cited; use a manual visual contrast check against "
                    f"the worst-case region of the actual rendered "
                    f"backdrop."
                    + (f' Text: "{text_preview}"' if text_preview else "")
                ),
                impact=(
                    "Background-image brightness can vary across the "
                    "element; the worst-case region may fall below "
                    "the contrast threshold and become illegible to "
                    "low-vision users."
                ),
                recommendation=(
                    f"Manual review required. Visually verify text "
                    f"contrast over every region of the background. "
                    f"If any portion falls below the WCAG {sc_label} "
                    f"threshold, add a solid colour fill, darken the "
                    f"image, or apply a text shadow / scrim behind "
                    f"the text."
                ),
                severity=Severity.INFO,
                source="andi",
                evidence=(
                    # CRITICAL: evidence field MUST NOT contain the
                    # unreliable ratio number. The fast-path judge
                    # prompt (analysis/judge.py:_judge_vpat_synthesis)
                    # renders the evidence field into FINDINGS blocks
                    # the judge reads -- exposing the ratio there would
                    # let the judge re-quote it in HIGH-severity output
                    # findings (verified failure mode on SC 1.4.3).
                    # The auditor can read the raw ratio from
                    # capture_data.andi_contrast_results / the saved
                    # andi_contrast.json artifact when needed; the
                    # judge does not need it.
                    f"ANDI methodology. Foreground: {fg}. "
                    f"Effective background (resolved {walk_depth} "
                    f"ancestor[s] up): {bg}. SVG text: {is_svg}. "
                    f"Ratio against the fallback colour intentionally "
                    f"omitted -- backdrop is image / gradient / video, "
                    f"so the fallback ratio does not represent rendered "
                    f"contrast."
                ),
            ))
            continue

        if ratio >= required:
            continue

        if ratio < required * 0.5:
            severity = Severity.HIGH
        else:
            severity = Severity.MEDIUM

        findings.append(Finding(
            id=_make_finding_id(),
            element=selector,
            css_selector=selector,
            issue=(
                f"ANDI: insufficient contrast for {size_desc} text: "
                f"{ratio:.2f}:1 (required for SC {sc_label}: "
                f"{required}:1). Colours: {fg} on {bg}."
                + (f' Text: "{text_preview}"' if text_preview else "")
            ),
            impact=(
                f"Users with low vision, age-related vision changes, "
                f"or transient impairments (glare, low-end displays) "
                f"may not be able to read this {size_desc} text."
            ),
            recommendation=(
                f"Adjust foreground or background colour so the "
                f"text meets at least {required}:1."
            ),
            severity=severity,
            source="andi",
            evidence=(
                f"ANDI methodology. Computed ratio: {ratio:.2f}:1. "
                f"Foreground: {fg}. Effective background "
                f"(resolved {walk_depth} ancestor[s] up): {bg}. "
                f"SVG text: {is_svg}."
            ),
        ))

    return findings


def extract_andi_lang_findings(capture_data: CaptureData, criterion_id: str) -> list[Finding]:
    """Extract ANDI-style language audit findings.

    Emits findings only for the two language SCs:

    - SC 3.1.1 Language of Page (Level A): ``html_lang`` missing or
      invalid (HIGH), ``html_lang`` vs ``xml:lang`` mismatch
      (MEDIUM).
    - SC 3.1.2 Language of Parts (Level AA): per-segment ``lang``
      invalid BCP 47 (MEDIUM), per-segment ``xml:lang`` mismatch
      (MEDIUM), redundant lang declarations (INFO — not a failure
      but flagged for code quality).

    For every other SC this returns an empty list. Hidden segments
    are emitted with severity INFO so the auditor knows ANDI saw a
    ``lang`` attribute on visually-hidden content (often dead code
    but worth surfacing).
    """
    if criterion_id not in ("3.1.1", "3.1.2"):
        return []

    data = getattr(capture_data, "andi_lang_results", None) or {}
    if not data:
        return []

    findings: list[Finding] = []
    is_3_1_1 = (criterion_id == "3.1.1")

    if is_3_1_1:
        html_lang = data.get("html_lang", "")
        html_lang_valid = data.get("html_lang_valid", False)
        html_xml_lang = data.get("html_xml_lang", "")
        xml_match = data.get("html_lang_xml_lang_match")

        if not html_lang:
            findings.append(Finding(
                id=_make_finding_id(),
                element="<html>", css_selector="html",
                issue="ANDI: <html> element has no lang attribute.",
                impact=(
                    "Screen readers fall back to user-locale "
                    "pronunciation, which mangles names, headings "
                    "and content read aloud. Browsers cannot offer "
                    "translation."
                ),
                recommendation=(
                    'Add a valid BCP 47 lang attribute to <html> '
                    '(e.g. lang="en", lang="en-US", lang="es").'
                ),
                severity=Severity.HIGH,
                source="andi",
                evidence="ANDI sANDI: html lang attribute missing.",
            ))
        elif not html_lang_valid:
            findings.append(Finding(
                id=_make_finding_id(),
                element="<html>", css_selector="html",
                issue=(
                    f"ANDI: <html lang=\"{html_lang}\"> is not a "
                    "valid BCP 47 language tag."
                ),
                impact=(
                    "Screen readers and translation tools cannot "
                    "interpret an invalid language tag and will "
                    "fall back to the user locale."
                ),
                recommendation=(
                    'Use a valid BCP 47 language tag '
                    '(e.g. lang="en", lang="en-US", lang="zh-Hant").'
                ),
                severity=Severity.MEDIUM,
                source="andi",
                evidence=f"ANDI sANDI: html lang=\"{html_lang}\" failed BCP 47 regex.",
            ))

        if html_lang and html_xml_lang and xml_match is False:
            findings.append(Finding(
                id=_make_finding_id(),
                element="<html>", css_selector="html",
                issue=(
                    f"ANDI: <html> lang (\"{html_lang}\") and "
                    f"xml:lang (\"{html_xml_lang}\") declare "
                    "different primary languages."
                ),
                impact=(
                    "Conflicting language declarations cause "
                    "different consumers (HTML parsers vs XML "
                    "parsers) to read the page in different "
                    "languages."
                ),
                recommendation=(
                    "Make lang and xml:lang on <html> agree on the "
                    "primary language subtag."
                ),
                severity=Severity.MEDIUM,
                source="andi",
                evidence=f"ANDI sANDI: html lang=\"{html_lang}\" xml:lang=\"{html_xml_lang}\".",
            ))

        return findings

    # SC 3.1.2 — per-segment lang
    segments = data.get("segments") or []
    for seg in segments:
        lang = seg.get("lang") or ""
        xml_lang = seg.get("xml_lang") or ""
        valid = seg.get("lang_valid")
        xml_matches = seg.get("xml_lang_matches_lang")
        redundant = bool(seg.get("redundant"))
        hidden = bool(seg.get("is_hidden"))
        selector = seg.get("selector", "?")
        text = seg.get("text", "") or ""
        text_preview = text
        tag = seg.get("tag", "")
        inherited = seg.get("inherited_lang", "")

        if lang and valid is False:
            sev = Severity.LOW if hidden else Severity.MEDIUM
            findings.append(Finding(
                id=_make_finding_id(),
                element=selector, css_selector=selector,
                issue=(
                    f"ANDI: <{tag} lang=\"{lang}\"> is not a valid "
                    "BCP 47 language tag."
                    + (" (segment is visually hidden)" if hidden else "")
                    + (f' Text: "{text_preview}"' if text_preview else "")
                ),
                impact=(
                    "Screen readers cannot switch pronunciation "
                    "for this passage, so foreign-language content "
                    "is read with the wrong phonemes."
                ),
                recommendation=(
                    "Use a valid BCP 47 tag for the segment "
                    '(e.g. lang="fr", lang="es-MX", lang="zh-Hans").'
                ),
                severity=sev,
                source="andi",
                evidence=(
                    f"ANDI sANDI: lang=\"{lang}\" failed BCP 47 regex. "
                    f"Hidden={hidden}."
                ),
            ))

        if lang and xml_lang and xml_matches is False:
            findings.append(Finding(
                id=_make_finding_id(),
                element=selector, css_selector=selector,
                issue=(
                    f"ANDI: <{tag}> lang (\"{lang}\") and xml:lang "
                    f"(\"{xml_lang}\") declare different primary "
                    "languages on the same element."
                ),
                impact=(
                    "HTML- and XML-aware consumers will read this "
                    "passage in different languages."
                ),
                recommendation=(
                    "Align lang and xml:lang to the same primary "
                    "subtag on this segment."
                ),
                severity=Severity.MEDIUM,
                source="andi",
                evidence=f"ANDI sANDI: lang=\"{lang}\" xml:lang=\"{xml_lang}\".",
            ))

        if redundant and lang:
            findings.append(Finding(
                id=_make_finding_id(),
                element=selector, css_selector=selector,
                issue=(
                    f"ANDI: <{tag} lang=\"{lang}\"> is redundant — "
                    f"the inherited language is already \"{inherited}\"."
                ),
                impact=(
                    "Not a conformance failure, but a code-quality "
                    "signal: redundant lang declarations are often "
                    "leftover from translation tooling and indicate "
                    "the segment may not actually be in a different "
                    "language."
                ),
                recommendation=(
                    "Remove the redundant lang attribute, or change "
                    "it to the actual segment language if the text "
                    "is in a different language than its container."
                ),
                severity=Severity.INFO,
                source="andi",
                evidence=(
                    f"ANDI sANDI: segment lang=\"{lang}\" matches "
                    f"inherited \"{inherited}\"."
                ),
            ))

    return findings


def extract_andi_interactive_findings(capture_data: CaptureData, criterion_id: str) -> list[Finding]:
    """Extract ANDI lANDI links/buttons-audit findings.

    Routes to the SCs that depend on link/button text quality:

    - SC 2.4.4 Link Purpose (In Context) (Level A):
      * Ambiguous link/button text ("click here", "more", ...) →
        MEDIUM. Could be acceptable if the surrounding context
        makes the purpose clear; the visual AI / judge layer
        decides on context, this layer just surfaces the
        candidates.
      * Empty accessible name on a link → HIGH (link has no
        programmatic name).

    - SC 2.5.3 Label in Name (Level A):
      * Visible text not contained in accessible name → HIGH.
        Voice-input users speaking the visible label cannot
        activate the control.

    - SC 4.1.2 Name Role Value (Level A):
      * Empty accessible name on a button → HIGH (control has no
        programmatic name).

    For other SCs returns ``[]``.
    """
    if criterion_id not in ("2.4.4", "2.5.3", "4.1.2"):
        return []

    results = getattr(capture_data, "andi_interactive_results", None) or []
    if not results:
        return []

    findings: list[Finding] = []
    is_244 = (criterion_id == "2.4.4")
    is_253 = (criterion_id == "2.5.3")
    is_412 = (criterion_id == "4.1.2")

    for r in results:
        sel = r.get("selector", "?")
        etype = r.get("type", "?")
        tag = r.get("tag", "")
        visible = r.get("visible_text", "")
        visible_preview = visible
        acc = r.get("accessible_name", "")
        acc_preview = acc
        name_source = r.get("name_source", "none")
        ambiguous = bool(r.get("is_ambiguous"))
        no_name = bool(r.get("has_no_name"))
        mismatch = bool(r.get("name_visible_mismatch"))

        if no_name:
            if (is_244 and etype == "link") or (is_412 and etype == "button"):
                findings.append(Finding(
                    id=_make_finding_id(),
                    element=sel, css_selector=sel,
                    issue=(
                        f"ANDI: <{tag}> has no accessible name. "
                        "No aria-label, no aria-labelledby, no "
                        "visible text, no title, no image with alt."
                    ),
                    impact=(
                        f"Screen readers announce just \"{etype}\" "
                        "with no description. Users cannot tell "
                        "what activating it will do."
                    ),
                    recommendation=(
                        f"Give the <{tag}> a name via visible "
                        "text, aria-label, or (for image-only "
                        "controls) an alt attribute on the inner "
                        "image."
                    ),
                    severity=Severity.HIGH,
                    source="andi",
                    evidence=f"ANDI lANDI: <{tag}> name_source=none.",
                ))
            continue

        if is_244 and ambiguous:
            findings.append(Finding(
                id=_make_finding_id(),
                element=sel, css_selector=sel,
                issue=(
                    f"ANDI: <{tag}> uses ambiguous text "
                    f'"{visible_preview}". Without surrounding '
                    "context, the link purpose cannot be "
                    "determined."
                ),
                impact=(
                    "Screen reader users navigating links out of "
                    "context (via the rotor / links list) hear "
                    "only the ambiguous phrase and cannot tell "
                    "which one to follow."
                ),
                recommendation=(
                    "Replace with descriptive text (e.g. \"View "
                    "the 2024 annual report\" instead of \"View\"), "
                    "OR add aria-label / aria-labelledby giving "
                    "the full purpose, OR ensure the surrounding "
                    "sentence makes the purpose clear in context."
                ),
                severity=Severity.MEDIUM,
                source="andi",
                evidence=(
                    f"ANDI lANDI: visible_text=\"{visible}\" "
                    f'matches ambiguous list. acc_name="{acc}".'
                ),
            ))

        if is_253 and mismatch:
            findings.append(Finding(
                id=_make_finding_id(),
                element=sel, css_selector=sel,
                issue=(
                    f"ANDI: visible text \"{visible_preview}\" is "
                    f"not contained in accessible name "
                    f"\"{acc_preview}\" (source: {name_source}). "
                    "Voice-input users speaking the visible "
                    "label cannot activate this control."
                ),
                impact=(
                    "SC 2.5.3 (Label in Name) requires the "
                    "accessible name to start with or contain the "
                    "visible label text so speech-input users "
                    "(\"click submit\") can target controls "
                    "identified by their visible label."
                ),
                recommendation=(
                    "Make the accessible name include the visible "
                    "text: change aria-label to start with the "
                    "visible label, or remove aria-label and let "
                    "the visible text BE the accessible name."
                ),
                severity=Severity.HIGH,
                source="andi",
                evidence=(
                    f"ANDI lANDI: visible=\"{visible}\" "
                    f'name_source="{name_source}" name="{acc}".'
                ),
            ))

    return findings


def extract_andi_tables_findings(capture_data: CaptureData, criterion_id: str) -> list[Finding]:
    """Extract ANDI tANDI tables-audit findings.

    Routes to SC 1.3.1 Info and Relationships:

    - Data table without ``<th>`` → MEDIUM (assistive tech can't
      announce row/column context).
    - Data table where ``<th>`` lack ``scope`` AND no
      ``cell[headers]`` association exists → MEDIUM (scope or
      headers/id is required for unambiguous association).
    - Data table with broken ``cell[headers]="id"`` references →
      MEDIUM (referential integrity failure).
    - Layout table without ``role="presentation"`` /
      ``role="none"`` → MEDIUM (semantic confusion: AT announces
      rows/columns where there are none).
    - Data table without ``<caption>`` → INFO (recommended, not
      strictly required).
    - Use of deprecated ``summary`` attribute → INFO.
    - Nested ``<table>`` → INFO (anti-pattern).
    - Empty table → INFO.

    For other SCs returns ``[]``.
    """
    if criterion_id != "1.3.1":
        return []

    results = getattr(capture_data, "andi_tables_results", None) or []
    if not results:
        return []

    findings: list[Finding] = []
    for t in results:
        sel = t.get("selector", "?")
        cls = t.get("classification", "?")
        issues = t.get("issues") or []
        if not issues:
            continue
        cap = t.get("caption_text", "")
        n_th = t.get("th_count", 0)
        n_th_scope = t.get("th_with_scope_count", 0)
        n_cells_headers = t.get("cells_with_headers_attr", 0)
        broken_refs = t.get("broken_headers_refs") or []
        th_missing_sels = t.get("th_missing_scope_selectors") or []
        rows = t.get("row_count", 0)
        cols = t.get("col_count", 0)
        role = t.get("role", "")

        for issue in issues:
            if issue == "data_table_no_th":
                findings.append(Finding(
                    id=_make_finding_id(),
                    element=sel, css_selector=sel,
                    issue=(
                        f"ANDI: data table ({rows}x{cols}) has no "
                        "<th> elements — row/column headers cannot "
                        "be programmatically associated with data "
                        "cells."
                    ),
                    impact=(
                        "Screen readers cannot announce \"column "
                        "X, row Y\" header context. The table is "
                        "effectively unreadable structurally."
                    ),
                    recommendation=(
                        "Replace the appropriate <td> elements "
                        "with <th> and add scope=\"col\" or "
                        "scope=\"row\". If the table is purely "
                        'layout, add role="presentation".'
                    ),
                    severity=Severity.MEDIUM,
                    source="andi",
                    evidence=f"ANDI tANDI: data table has 0 <th>, classification={cls}.",
                ))
            elif issue == "th_missing_scope_and_no_headers_attr":
                sample = th_missing_sels
                findings.append(Finding(
                    id=_make_finding_id(),
                    element=sel, css_selector=sel,
                    issue=(
                        f"ANDI: data table has {n_th_scope}/{n_th} "
                        "<th> elements with scope, and no cells "
                        "use the headers attribute — ambiguous "
                        f"header association. Sample <th> missing "
                        f"scope: {sample}"
                    ),
                    impact=(
                        "Screen readers cannot reliably associate "
                        "data cells with their header. Especially "
                        "in tables with merged cells or multiple "
                        "header rows the wrong header gets "
                        "announced."
                    ),
                    recommendation=(
                        "Add scope=\"col\" or scope=\"row\" to "
                        "every <th>, OR (for complex tables) "
                        "give every <th> an id and reference it "
                        "from data cells via headers=\"id1 id2\"."
                    ),
                    severity=Severity.MEDIUM,
                    source="andi",
                    evidence=(
                        f"ANDI tANDI: th_with_scope={n_th_scope}/{n_th}, "
                        f"cells_with_headers={n_cells_headers}."
                    ),
                ))
            elif issue == "headers_attr_broken_refs":
                findings.append(Finding(
                    id=_make_finding_id(),
                    element=sel, css_selector=sel,
                    issue=(
                        "ANDI: cells reference non-existent ids in "
                        f"headers attribute: {broken_refs}."
                    ),
                    impact=(
                        "Broken header references mean assistive "
                        "tech cannot resolve the announced header "
                        "for the affected cells."
                    ),
                    recommendation=(
                        "Fix every headers attribute so each "
                        "space-separated id resolves to an actual "
                        "<th> id on the page."
                    ),
                    severity=Severity.MEDIUM,
                    source="andi",
                    evidence=f"ANDI tANDI: broken_headers_refs={broken_refs}.",
                ))
            elif issue == "layout_table_no_presentation_role":
                findings.append(Finding(
                    id=_make_finding_id(),
                    element=sel, css_selector=sel,
                    issue=(
                        f"ANDI: layout table ({rows}x{cols}) has "
                        "no role=\"presentation\" — assistive tech "
                        "announces it as a data table with "
                        "\"row, column\" navigation despite the "
                        "structure being purely visual."
                    ),
                    impact=(
                        "Screen reader users hear unnecessary "
                        "\"table with N columns and M rows\" "
                        "announcements and grid-navigation cues "
                        "for what is just a layout grid."
                    ),
                    recommendation=(
                        "Add role=\"presentation\" (or role=\"none\") "
                        "to mark the table as layout-only, or "
                        "replace with CSS layout (flexbox / grid)."
                    ),
                    severity=Severity.MEDIUM,
                    source="andi",
                    evidence=(
                        f"ANDI tANDI: classification=layout, role={role!r}, "
                        f"th_count={n_th}, has_caption=False."
                    ),
                ))
            elif issue == "data_table_no_caption":
                findings.append(Finding(
                    id=_make_finding_id(),
                    element=sel, css_selector=sel,
                    issue=(
                        "ANDI: data table has no <caption> — "
                        "users cannot tell what the table "
                        "contains before navigating into it."
                    ),
                    impact=(
                        "Screen reader users hear \"table\" with "
                        "no description. They have to read into "
                        "the table to understand what it covers."
                    ),
                    recommendation=(
                        "Add a <caption> as the first child of "
                        "<table> describing the table's purpose."
                    ),
                    severity=Severity.INFO,
                    source="andi",
                    evidence=f"ANDI tANDI: data table, no caption.",
                ))
            elif issue == "uses_deprecated_summary_attribute":
                findings.append(Finding(
                    id=_make_finding_id(),
                    element=sel, css_selector=sel,
                    issue=(
                        "ANDI: table uses the deprecated "
                        "summary attribute. HTML5 obsoleted this; "
                        f"summary=\"{t.get('summary_text','')}\""
                    ),
                    impact=(
                        "Some HTML5 parsers ignore the summary "
                        "attribute entirely; screen reader support "
                        "is inconsistent."
                    ),
                    recommendation=(
                        "Move the summary text into a <caption> "
                        "or aria-describedby reference."
                    ),
                    severity=Severity.INFO,
                    source="andi",
                    evidence="ANDI tANDI: deprecated summary attribute on <table>.",
                ))
            elif issue == "nested_table":
                findings.append(Finding(
                    id=_make_finding_id(),
                    element=sel, css_selector=sel,
                    issue=(
                        "ANDI: nested <table> inside <table>. "
                        "This is an anti-pattern: AT navigation "
                        "becomes confusing and header associations "
                        "rarely work as expected."
                    ),
                    impact=(
                        "Screen reader users get unexpected "
                        "\"table inside table\" announcements; "
                        "header context from the outer table does "
                        "not apply to inner cells."
                    ),
                    recommendation=(
                        "Flatten to a single table, or refactor "
                        "the inner content into list/CSS grid "
                        "layout."
                    ),
                    severity=Severity.INFO,
                    source="andi",
                    evidence="ANDI tANDI: nested <table>.",
                ))
            elif issue == "empty_table":
                findings.append(Finding(
                    id=_make_finding_id(),
                    element=sel, css_selector=sel,
                    issue=(
                        "ANDI: empty <table> with 0 rows — likely "
                        "rendered before data populated, or dead "
                        "markup."
                    ),
                    impact=(
                        "AT may announce \"empty table\" or "
                        "trigger a confusing navigation gesture."
                    ),
                    recommendation=(
                        "Remove the empty table, or render it "
                        "only after data is available."
                    ),
                    severity=Severity.INFO,
                    source="andi",
                    evidence="ANDI tANDI: 0 rows.",
                ))

    return findings


def extract_andi_graphics_findings(capture_data: CaptureData, criterion_id: str) -> list[Finding]:
    """Extract ANDI gANDI graphics-audit findings.

    Routes findings to the SCs that depend on graphics
    accessibility:

    - SC 1.1.1 Non-text Content (Level A):
      * ``<img>`` with no ``alt`` attribute → HIGH.
      * ``<img>`` with ``alt=""`` inside a link/button where the
        link/button has NO other accessible name source → HIGH
        (the LINK has no accessible name).
      * ``<svg>`` that is not ``aria-hidden`` and has no
        ``aria-label`` / ``aria-labelledby`` / ``<title>`` →
        MEDIUM (decorative SVG should explicitly opt out).
      * ``<svg role="img">`` with no name → HIGH (claims to be
        an image but has no name).
      * ``<input type="image">`` with no alt / aria-label /
        value / title → HIGH.
      * ``<area>`` with no alt / aria-label → HIGH.

    - SC 1.4.5 Images of Text (Level AA):
      * Background-image element with visible text overlay →
        INFO (manual check — could be HTML text on top of the
        image, or could be image-of-text baked in).

    For other SCs returns ``[]``.
    """
    if criterion_id not in ("1.1.1", "1.4.5"):
        return []

    results = getattr(capture_data, "andi_graphics_results", None) or []
    if not results:
        return []

    findings: list[Finding] = []
    is_111 = (criterion_id == "1.1.1")
    is_145 = (criterion_id == "1.4.5")

    for g in results:
        t = g.get("type", "")
        sel = g.get("selector", "?")
        src = g.get("src", "") or ""
        alt = g.get("alt")
        alt_present = bool(g.get("alt_present"))
        alt_empty = bool(g.get("alt_empty"))
        ariaLabel = g.get("aria_label", "")
        lbResolved = g.get("aria_labelledby_resolved", "")
        ariaHidden = bool(g.get("aria_hidden"))
        role = g.get("role", "") or ""
        decorative = bool(g.get("decorative"))
        in_link = bool(g.get("in_link_or_button"))
        other_text = bool(g.get("ancestor_has_other_text"))
        anc_has_name = bool(g.get("ancestor_link_or_button_has_name"))
        anc_tag = g.get("ancestor_tag", "")
        name_source = g.get("name_source", "none")
        acc_name = g.get("accessible_name", "")
        svg_title = g.get("svg_title", "")
        text_overlay = bool(g.get("has_text_overlay"))
        overlay_text = g.get("text_overlay_text", "")
        # Full src for real URLs (the tail-truncation used to clip filenames
        # off long query-string URLs). For inline data URIs, keep the header
        # (mime type + encoding) and note the payload size — a fidelity-
        # preserving summary, not a truncation: the base64 payload carries
        # no signal for the judge and would bloat the prompt by megabytes.
        if src.startswith("data:"):
            header, sep, payload = src.partition(",")
            if sep:
                short_src = f"{header},[{len(payload)} chars]"
            else:
                short_src = f"data:[inline data URI, {len(src)} chars]"
        else:
            short_src = src

        if is_145:
            if t == "bg-image" and text_overlay:
                findings.append(Finding(
                    id=_make_finding_id(),
                    element=sel, css_selector=sel,
                    issue=(
                        f"ANDI: element has background-image "
                        f"({short_src}) AND a visible text overlay. "
                        "Cannot determine deterministically if the "
                        "text is HTML rendered on top of the image "
                        "or baked into the image bitmap. Manual "
                        f"check required. Overlay text: \"{overlay_text}\"."
                    ),
                    impact=(
                        "If the text is part of the image bitmap "
                        "it cannot be resized, recoloured, or "
                        "translated, breaking SC 1.4.5 (Images of "
                        "Text)."
                    ),
                    recommendation=(
                        "Open DevTools and confirm the visible "
                        "text is HTML (an inline DOM text node), "
                        "not part of the background image. If it "
                        "is part of the image, replace with HTML "
                        "text styled via CSS or an SVG with text."
                    ),
                    severity=Severity.INFO,
                    source="andi",
                    evidence=(
                        f"ANDI gANDI: bg-image {short_src!r} on "
                        f"<{sel}>; text descendant length="
                        f"{len(overlay_text)}."
                    ),
                ))
            continue

        # SC 1.1.1 from here on
        if t == "img":
            if ariaHidden:
                continue  # aria-hidden=true exempts from 1.1.1
            if not alt_present and not ariaLabel and not lbResolved:
                findings.append(Finding(
                    id=_make_finding_id(),
                    element=sel, css_selector=sel,
                    issue=(
                        f"ANDI: <img> has no alt attribute and no "
                        f"aria-label/aria-labelledby. src={short_src!r}."
                    ),
                    impact=(
                        "Screen readers fall back to announcing the "
                        "filename, which is rarely a useful "
                        "description."
                    ),
                    recommendation=(
                        "Add a meaningful alt attribute, or alt=\"\" "
                        "if the image is purely decorative."
                    ),
                    severity=Severity.HIGH,
                    source="andi",
                    evidence=f"ANDI gANDI: <img src={short_src!r}> alt missing.",
                ))
                continue
            if alt_empty and in_link and not other_text and not anc_has_name:
                findings.append(Finding(
                    id=_make_finding_id(),
                    element=sel, css_selector=sel,
                    issue=(
                        f"ANDI: <img alt=\"\"> is the sole content "
                        f"of <{anc_tag}> which has no other "
                        f"accessible name source — the {anc_tag} "
                        f"has no accessible name. src={short_src!r}."
                    ),
                    impact=(
                        f"Screen readers announce the {anc_tag} as "
                        f"\"{anc_tag}\" with no description; users "
                        "cannot know what activating it will do."
                    ),
                    recommendation=(
                        f"Replace alt=\"\" with descriptive alt "
                        f"text describing the {anc_tag}'s "
                        "destination/action, OR add aria-label "
                        f"to the <{anc_tag}>, OR add visible text "
                        f"inside the <{anc_tag}>."
                    ),
                    severity=Severity.HIGH,
                    source="andi",
                    evidence=(
                        f"ANDI gANDI: <img alt=\"\"> inside "
                        f"<{anc_tag}> with other_text=False, "
                        f"ancestor_has_name=False."
                    ),
                ))

        elif t == "svg":
            if ariaHidden or decorative:
                continue
            has_name = bool(ariaLabel or lbResolved or svg_title)
            if role == "img" and not has_name:
                findings.append(Finding(
                    id=_make_finding_id(),
                    element=sel, css_selector=sel,
                    issue=(
                        "ANDI: <svg role=\"img\"> declares itself "
                        "as an image but has no accessible name "
                        "(no aria-label, no aria-labelledby, no "
                        "<title>)."
                    ),
                    impact=(
                        "Screen readers announce \"image\" with no "
                        "description, identical to a missing alt."
                    ),
                    recommendation=(
                        "Add aria-label, aria-labelledby, or a "
                        "<title> child element to the <svg>."
                    ),
                    severity=Severity.HIGH,
                    source="andi",
                    evidence="ANDI gANDI: <svg role=\"img\"> with no name source.",
                ))
            elif not has_name and not role:
                if in_link and not other_text and not anc_has_name:
                    findings.append(Finding(
                        id=_make_finding_id(),
                        element=sel, css_selector=sel,
                        issue=(
                            f"ANDI: <svg> is the sole content of "
                            f"<{anc_tag}> and has no accessible "
                            "name (no aria-label/aria-labelledby/"
                            f"title) — the {anc_tag} has no "
                            "accessible name."
                        ),
                        impact=(
                            f"Screen readers announce the {anc_tag} "
                            f"as just \"{anc_tag}\"; users cannot "
                            "know what activating it will do."
                        ),
                        recommendation=(
                            f"Add aria-label to the <{anc_tag}>, "
                            "or add a <title> inside the <svg>, "
                            f"or add visible text inside the "
                            f"<{anc_tag}>. If decorative, mark "
                            "aria-hidden=\"true\"."
                        ),
                        severity=Severity.HIGH,
                        source="andi",
                        evidence=(
                            f"ANDI gANDI: <svg> inside <{anc_tag}> "
                            f"with no name and no other text."
                        ),
                    ))
                else:
                    findings.append(Finding(
                        id=_make_finding_id(),
                        element=sel, css_selector=sel,
                        issue=(
                            "ANDI: <svg> has no accessible name "
                            "and no role/aria-hidden — its purpose "
                            "is undeclared."
                        ),
                        impact=(
                            "If the SVG is informative, screen "
                            "readers announce nothing. If it is "
                            "decorative, it should be marked "
                            "aria-hidden=\"true\"."
                        ),
                        recommendation=(
                            "If informative: add role=\"img\" plus "
                            "aria-label or <title>. If decorative: "
                            "add aria-hidden=\"true\"."
                        ),
                        severity=Severity.MEDIUM,
                        source="andi",
                        evidence=(
                            "ANDI gANDI: <svg> with no role, no "
                            "aria-hidden, no name source."
                        ),
                    ))

        elif t == "input-image":
            if name_source == "none":
                findings.append(Finding(
                    id=_make_finding_id(),
                    element=sel, css_selector=sel,
                    issue=(
                        "ANDI: <input type=\"image\"> has no alt, "
                        "aria-label, value, or title — the button "
                        "has no accessible name."
                    ),
                    impact=(
                        "Screen readers announce \"button, "
                        "submit\" with no description. The user "
                        "cannot tell what submitting will do."
                    ),
                    recommendation=(
                        'Add a descriptive alt attribute (e.g. '
                        'alt="Search").'
                    ),
                    severity=Severity.HIGH,
                    source="andi",
                    evidence="ANDI gANDI: <input type=\"image\"> with no name.",
                ))

        elif t == "area":
            if name_source == "none" and not ariaHidden:
                findings.append(Finding(
                    id=_make_finding_id(),
                    element=sel, css_selector=sel,
                    issue=(
                        "ANDI: <area> in image map has no alt or "
                        "aria-label — the map region has no "
                        "accessible name."
                    ),
                    impact=(
                        "Image map regions without names are "
                        "unusable to screen reader and "
                        "speech-input users."
                    ),
                    recommendation=(
                        "Add a descriptive alt attribute to every "
                        "<area>."
                    ),
                    severity=Severity.HIGH,
                    source="andi",
                    evidence="ANDI gANDI: <area> with no name.",
                ))

    return findings


def extract_andi_hidden_findings(capture_data: CaptureData, criterion_id: str) -> list[Finding]:
    """Extract ANDI hANDI hidden-content findings.

    Severity is keyed off the hidden reason and SC:

    - ``aria-hidden=true`` on a focusable element (or any ancestor)
      → HIGH for SC 4.1.2 (definite ARIA spec violation), MEDIUM
      for the other hidden-relevant SCs (creates phantom tab stops
      and screen-reader contradictions but is downstream of the
      spec violation).
    - ``display:none`` / ``visibility:hidden`` / ``hidden`` attr on
      a tab-reachable element → MEDIUM for SC 2.1.1 / 2.4.3 (the
      element is dead code that may be re-shown by JS); LOW for
      others. Browsers normally skip these in the tab order, so
      severity is moderated.
    - ``opacity:0`` / off-screen positioned focusable → LOW. The
      SC 2.4.1 skip-link pattern uses this legitimately, so we
      don't fail on it; we surface it for the auditor.

    For SCs not in ``_ANDI_HIDDEN_SCS`` this returns ``[]``.
    """
    if criterion_id not in _ANDI_HIDDEN_SCS:
        return []

    results = getattr(capture_data, "andi_hidden_results", None) or []
    if not results:
        return []

    findings: list[Finding] = []
    is_412 = (criterion_id == "4.1.2")
    is_keyboard = criterion_id in ("2.1.1", "2.4.3")

    for entry in results:
        reasons = entry.get("hidden_reasons") or []
        if not reasons:
            continue

        selector = entry.get("selector", "?")
        tag = entry.get("tag", "")
        role = entry.get("role", "")
        name = entry.get("accessible_name", "") or ""
        text = entry.get("text", "") or ""
        tabindex = entry.get("tabindex")
        tab_reachable = bool(entry.get("tab_reachable"))
        naturally = bool(entry.get("naturally_focusable"))
        ah_path = entry.get("aria_hidden_path", "")
        ah_anc_sel = entry.get("aria_hidden_ancestor_selector", "")

        # If the element has zero rendered dimensions, the browser
        # will not include it in the tab order regardless of what
        # tabindex / href says. The capture's tab_reachable flag
        # does not propagate display:none / 0-height from ancestor
        # elements, so a `<a>` inside a collapsed jQuery-UI accordion
        # panel (style="display:none; height:0") shows up here as
        # tab_reachable=True with rect={0,0,0,0}. Treat 0x0 rects
        # as not actually focusable.
        rect = entry.get("rect") or {}
        try:
            _w = float(rect.get("width") or 0)
            _h = float(rect.get("height") or 0)
        except (ValueError, TypeError):
            _w = _h = 0.0
        if _w == 0 and _h == 0 and tab_reachable:
            tab_reachable = False

        aria_hidden_violation = any("aria-hidden=true" in r for r in reasons)
        # display:none / visibility:hidden / [hidden] / [inert] all
        # have the same effect for focus order: the browser correctly
        # removes the element from the tab sequence. The `inert`
        # attribute additionally removes the subtree from the
        # accessibility tree (per the HTML Inert Subtrees spec), so
        # it's a stronger signal than the others — but for the
        # purpose of this check, all four mean "no real focus leak".
        display_hidden = any(
            r in ("display:none", "visibility:hidden", "hidden attribute")
            or r.startswith("inert")
            for r in reasons
        )
        opacity_off = any(r in ("opacity:0",) for r in reasons)
        off_screen = any("off-screen" in r or "beyond viewport" in r for r in reasons)

        label = name or text or "(no accessible name)"
        label_preview = label
        ti_str = f"tabindex={tabindex!r}" if tabindex is not None else "no tabindex"
        reasons_str = ", ".join(reasons)

        if aria_hidden_violation:
            # Only flag when the element is *actually* tab-reachable.
            # The ARIA spec violation is "aria-hidden=true on a
            # focusable element"; an element with `tabindex="-1"`
            # explicitly opts out of the tab order and so is not a
            # focus leak even if it would otherwise be natively
            # focusable. The Slick carousel pattern (and most
            # ARIA-aware widgets) sets aria-hidden=true on inactive
            # slide containers PLUS tabindex=-1 on every inner
            # link/button — the correct way to remove rotated-out
            # content. Without this guard, every well-implemented
            # slick/swiper/glide carousel produces dozens of
            # phantom violations.
            if not tab_reachable:
                continue
            if is_412:
                severity = Severity.HIGH
                impact = (
                    "ARIA spec violation: aria-hidden=\"true\" "
                    "MUST NOT be applied to elements that are "
                    "focusable. Screen readers receive contradictory "
                    "instructions — element is announced (because "
                    "focus moves to it) but with no accessible "
                    "information."
                )
            else:
                severity = Severity.MEDIUM
                impact = (
                    "Keyboard users tab to this element but screen "
                    "readers do not announce it. Sighted keyboard "
                    "users see focus on \"nothing\"; AT users hear "
                    "nothing where focus moved."
                )
            where = f"on the element itself" if ah_path == "self" else \
                    f"on ancestor {ah_anc_sel}"
            findings.append(Finding(
                id=_make_finding_id(),
                element=selector, css_selector=selector,
                issue=(
                    f"ANDI: focusable <{tag}> has aria-hidden=\"true\" "
                    f"{where}. ({ti_str}, "
                    f"tab-reachable={tab_reachable}, "
                    f'name="{label_preview}")'
                ),
                impact=impact,
                recommendation=(
                    "Remove aria-hidden from this element and any "
                    "ancestor, or remove tabindex / make the element "
                    "non-focusable if it should be hidden from AT."
                ),
                severity=severity,
                source="andi",
                evidence=(
                    f"ANDI hANDI: <{tag} role=\"{role}\" "
                    f"tabindex={tabindex!r}>; reasons=[{reasons_str}]."
                ),
            ))
            continue

        if display_hidden and tab_reachable:
            # Browsers correctly remove display:none /
            # visibility:hidden / [hidden] elements from the tab
            # order. The capture's tab_reachable flag here is a
            # static "would-be reachable if shown" check based on
            # tabindex / natively-focusable role, not a runtime
            # tab-order probe. Without this guard every cookie
            # banner (OneTrust .otFlat with display:none and
            # tabindex="0"), every tracking pixel iframe with
            # display:none, every preference modal with
            # visibility:hidden gets flagged as a phantom focus
            # leak — none of which the user can actually tab into
            # without an explicit JS show-on-focus pattern. We
            # keep it as INFO so the auditor still sees it during
            # manual review (in case JS does re-show the element
            # without removing tabindex), but do not push the
            # conformance verdict on it.
            #
            # Pure aria-hidden + focusable, opacity:0 + focusable,
            # and off-screen + focusable are still flagged below at
            # their original severities — those are real focus
            # leaks the browser does not block.
            # aria_hidden_violation is impossible here — the branch
            # above always `continue`s when it is set.
            if opacity_off or off_screen:
                # Combined with another real-leak signal — fall
                # through to be reported at the higher severity
                # below.
                pass
            else:
                findings.append(Finding(
                    id=_make_finding_id(),
                    element=selector, css_selector=selector,
                    issue=(
                        f"ANDI: <{tag}> is hidden via {reasons_str} "
                        f"and has tabindex/natively-focusable role "
                        f"({ti_str}). Browsers normally skip these "
                        f"in tab order, but a focus leak is possible "
                        f"if JS re-shows the element on focus. "
                        f"Name=\"{label_preview}\"."
                    ),
                    impact=(
                        "If the page later removes display:none / "
                        "visibility:hidden without also clearing "
                        "tabindex, this element will become a "
                        "phantom tab stop at that moment."
                    ),
                    recommendation=(
                        "When showing the element, ensure the "
                        "visible state has a real focus indicator. "
                        "When hiding it again, also remove any "
                        "explicit tabindex if the element should "
                        "stay non-focusable."
                    ),
                    severity=Severity.INFO,
                    source="andi",
                    evidence=f"ANDI hANDI: reasons=[{reasons_str}].",
                ))
                continue

        if opacity_off or off_screen:
            findings.append(Finding(
                id=_make_finding_id(),
                element=selector, css_selector=selector,
                issue=(
                    f"ANDI: focusable <{tag}> is invisible "
                    f"({reasons_str}) but accepts focus. "
                    f"Name=\"{label_preview}\"."
                ),
                impact=(
                    "Could be a legitimate visible-on-focus skip "
                    "link, or could be dead code that takes focus "
                    "with no visual indication."
                ),
                recommendation=(
                    "Verify the element is intentionally a "
                    "visible-on-focus skip link with a :focus rule "
                    "that brings it on-screen; if not, remove its "
                    "focusability."
                ),
                severity=Severity.INFO,
                source="andi",
                evidence=f"ANDI hANDI: reasons=[{reasons_str}].",
            ))

    return findings
