"""Data models for the AccessAudit application."""
from __future__ import annotations

import enum
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


# ── Enums ────────────────────────────────────────────────────────────────────

class Severity(str, enum.Enum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"
    INFO = "info"


class ConformanceLevel(str, enum.Enum):
    SUPPORTS = "Supports"
    PARTIALLY_SUPPORTS = "Partially Supports"
    DOES_NOT_SUPPORT = "Does Not Support"
    NOT_APPLICABLE = "Not Applicable"
    NOT_EVALUATED = "Not Evaluated"


class ReviewStatus(str, enum.Enum):
    QUEUED = "queued"
    CRAWLING = "crawling"
    CAPTURING = "capturing"
    TESTING = "testing"
    AGGREGATING = "aggregating"
    GENERATING_REPORT = "generating_report"
    COMPLETE = "complete"
    ERROR = "error"


class TTResult(str, enum.Enum):
    PASS = "PASS"
    FAIL = "FAIL"
    DNA = "DNA"
    NOT_TESTED = "NOT TESTED"


# ── Finding ──────────────────────────────────────────────────────────────────

@dataclass
class Finding:
    """A single accessibility issue found during testing."""
    id: str
    element: str
    issue: str
    impact: str
    recommendation: str
    severity: Severity
    source: str = "programmatic"
    css_selector: str = ""
    screenshot_path: str = ""
    evidence: str = ""
    decision: str = "undecided"
    decision_reason: str = ""
    # Human-readable sentence describing where the element sits on the page.
    # Populated by checks.base._enrich_finding_locations using captured rects,
    # landmarks, and section headings. Reads like a sentence a Trusted Tester
    # would write, e.g. "Near the bottom of the page in the main site footer,
    # on the Facebook social media link."
    location: str = ""
    # Structured measured values the finding cites: list of
    # {selector, metric, value}. Carried from the judge's tool output so
    # the claim validator's verdict (which values were verified vs.
    # demoted) stays inspectable in the saved report.
    cited_measurements: list = field(default_factory=list)

    def to_dict(self) -> dict:
        d = {
            "id": self.id,
            "element": self.element,
            "issue": self.issue,
            "impact": self.impact,
            "recommendation": self.recommendation,
            "severity": self.severity.value if isinstance(self.severity, Severity) else self.severity,
            "source": self.source,
            "decision": self.decision,
            "decision_reason": self.decision_reason,
        }
        if self.css_selector:
            d["css_selector"] = self.css_selector
        if self.screenshot_path:
            d["screenshot_path"] = self.screenshot_path
        if self.evidence:
            d["evidence"] = self.evidence
        if self.location:
            d["location"] = self.location
        if self.cited_measurements:
            d["cited_measurements"] = self.cited_measurements
        return d


# ── Trusted Tester sub-test ──────────────────────────────────────────────────

@dataclass
class TTSubTestResult:
    tt_id: str
    name: str
    result: TTResult

    def to_dict(self) -> dict:
        return {
            "tt_id": self.tt_id,
            "name": self.name,
            "result": self.result.value if isinstance(self.result, TTResult) else self.result,
        }


# ── Test result per criterion ────────────────────────────────────────────────

@dataclass
class TestResult:
    __test__ = False  # Prevent pytest collection

    criterion_id: str
    criterion_name: str
    level: str
    wcag_versions: list[str]

    conformance_level: ConformanceLevel = ConformanceLevel.NOT_EVALUATED
    confidence: float = 0.0
    confidence_reasoning: str = ""
    findings: list[Finding] = field(default_factory=list)
    tt_results: list[TTSubTestResult] = field(default_factory=list)
    summary: str = ""
    duration: float = 0.0

    # Per-source verdicts
    programmatic_conformance: ConformanceLevel = ConformanceLevel.NOT_EVALUATED
    programmatic_confidence: float = 0.0
    programmatic_findings_count: int = 0

    ai_conformance: ConformanceLevel = ConformanceLevel.NOT_EVALUATED
    ai_confidence: float = 0.0
    ai_findings_count: int = 0

    code_ai_conformance: ConformanceLevel = ConformanceLevel.NOT_EVALUATED
    code_ai_confidence: float = 0.0
    code_ai_findings_count: int = 0

    at_sim_conformance: ConformanceLevel = ConformanceLevel.NOT_EVALUATED
    at_sim_confidence: float = 0.0
    at_sim_findings_count: int = 0

    # Verification
    verified: bool | None = None
    verification_status: str = "not_verified"

    # Human review flag — set when confidence is too low to trust the verdict,
    # relevant content exists but no issues were found, or capture data was
    # incomplete.  Surfaced in the UI so auditors know where to focus.
    needs_review: bool = False
    needs_review_reasons: list[str] = field(default_factory=list)

    # ICT baseline
    ict_baseline: str = ""

    # Error tracking
    error: str | None = None

    def to_dict(self) -> dict:
        return {
            "criterion_id": self.criterion_id,
            "criterion_name": self.criterion_name,
            "level": self.level,
            "wcag_versions": self.wcag_versions,
            "conformance_level": _enum_val(self.conformance_level),
            "confidence": round(self.confidence, 3),
            "confidence_reasoning": self.confidence_reasoning,
            "findings": [f.to_dict() for f in self.findings],
            "tt_results": [t.to_dict() for t in self.tt_results],
            "summary": self.summary,
            "duration": round(self.duration, 2),
            "programmatic_conformance": _enum_val(self.programmatic_conformance),
            "programmatic_confidence": round(self.programmatic_confidence, 3),
            "programmatic_findings_count": self.programmatic_findings_count,
            "ai_conformance": _enum_val(self.ai_conformance),
            "ai_confidence": round(self.ai_confidence, 3),
            "ai_findings_count": self.ai_findings_count,
            "code_ai_conformance": _enum_val(self.code_ai_conformance),
            "code_ai_confidence": round(self.code_ai_confidence, 3),
            "code_ai_findings_count": self.code_ai_findings_count,
            "at_sim_conformance": _enum_val(self.at_sim_conformance),
            "at_sim_confidence": round(self.at_sim_confidence, 3),
            "at_sim_findings_count": self.at_sim_findings_count,
            "verified": self.verified,
            "verification_status": self.verification_status,
            "needs_review": self.needs_review,
            "needs_review_reasons": self.needs_review_reasons,
            "ict_baseline": self.ict_baseline,
            "error": self.error,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "TestResult":
        """Rebuild a TestResult from its to_dict() output. Used by the
        review resume path so reloaded results are real TestResult
        instances (not anonymous-class shells), keeping ``isinstance``
        checks and ``.to_dict()`` round-trips correct.

        Coerces enum-valued fields back into their enum members and
        rebuilds nested Finding + TTSubTestResult lists. Unknown keys
        in the input dict are silently ignored so future additions to
        to_dict() don't break older serializations.
        """
        def _to_conformance(v):
            if isinstance(v, ConformanceLevel):
                return v
            try:
                return ConformanceLevel(str(v))
            except Exception:
                return ConformanceLevel.NOT_EVALUATED

        def _to_finding(f):
            if isinstance(f, Finding):
                return f
            if not isinstance(f, dict):
                return None
            sev = f.get("severity", "medium")
            if not isinstance(sev, Severity):
                try:
                    sev = Severity(str(sev))
                except Exception:
                    sev = Severity.MEDIUM
            return Finding(
                id=str(f.get("id", "")),
                element=str(f.get("element", "")),
                issue=str(f.get("issue", "")),
                impact=str(f.get("impact", "")),
                recommendation=str(f.get("recommendation", "")),
                severity=sev,
                source=str(f.get("source", "")),
                css_selector=str(f.get("css_selector", "")),
            )

        def _to_tt(t):
            if isinstance(t, TTSubTestResult):
                return t
            if not isinstance(t, dict):
                return None
            res = t.get("result", "PASS")
            if isinstance(res, TTResult):
                pass
            else:
                try:
                    res = TTResult[str(res).upper()]
                except Exception:
                    try:
                        res = TTResult(str(res))
                    except Exception:
                        res = TTResult.PASS
            return TTSubTestResult(
                tt_id=str(t.get("tt_id", "")),
                name=str(t.get("name", "")),
                result=res,
            )

        findings = [f for f in (_to_finding(x) for x in (d.get("findings") or [])) if f]
        tt = [t for t in (_to_tt(x) for x in (d.get("tt_results") or [])) if t]

        return cls(
            criterion_id=str(d.get("criterion_id", "")),
            criterion_name=str(d.get("criterion_name", "")),
            level=str(d.get("level", "")),
            wcag_versions=list(d.get("wcag_versions") or []),
            conformance_level=_to_conformance(d.get("conformance_level")),
            confidence=float(d.get("confidence", 0) or 0),
            confidence_reasoning=str(d.get("confidence_reasoning", "")),
            findings=findings,
            tt_results=tt,
            summary=str(d.get("summary", "")),
            duration=float(d.get("duration", 0) or 0),
            programmatic_conformance=_to_conformance(d.get("programmatic_conformance")),
            programmatic_confidence=float(d.get("programmatic_confidence", 0) or 0),
            programmatic_findings_count=int(d.get("programmatic_findings_count", 0) or 0),
            ai_conformance=_to_conformance(d.get("ai_conformance")),
            ai_confidence=float(d.get("ai_confidence", 0) or 0),
            ai_findings_count=int(d.get("ai_findings_count", 0) or 0),
            code_ai_conformance=_to_conformance(d.get("code_ai_conformance")),
            code_ai_confidence=float(d.get("code_ai_confidence", 0) or 0),
            code_ai_findings_count=int(d.get("code_ai_findings_count", 0) or 0),
            at_sim_conformance=_to_conformance(d.get("at_sim_conformance")),
            at_sim_confidence=float(d.get("at_sim_confidence", 0) or 0),
            at_sim_findings_count=int(d.get("at_sim_findings_count", 0) or 0),
            verified=d.get("verified"),
            verification_status=str(d.get("verification_status", "not_verified")),
            needs_review=bool(d.get("needs_review", False)),
            needs_review_reasons=list(d.get("needs_review_reasons") or []),
            ict_baseline=str(d.get("ict_baseline", "")),
            error=d.get("error"),
        )


# ── Capture data ─────────────────────────────────────────────────────────────

@dataclass
class CaptureData:
    """All captured data for a page."""
    url: str = ""
    file_path: str = ""
    file_type: str | None = None
    title: str = ""
    html: str = ""

    # Screenshots
    full_page_path: str = ""
    viewport_path: str = ""
    viewport_200pct_path: str = ""
    full_page_200pct_path: str = ""
    viewport_320px_path: str = ""

    # DOM data
    dom_path: str = ""
    a11y_tree: dict = field(default_factory=dict)
    computed_styles: list = field(default_factory=list)
    nontext_contrast: list = field(default_factory=list)
    # Elements with CSS position fixed/sticky/absolute — selector + position
    # + rect. Authoritative ground truth for SC 1.4.10 reflow / overlap
    # findings: a finding may only assert position:fixed/sticky for an
    # element that appears here.
    positioned_elements: list[dict] = field(default_factory=list)
    # Interactive-target dimensions — selector + width + height + centre.
    # Authoritative ground truth for SC 2.5.8 / 2.5.5 target-size findings;
    # populated by functions.target_size.compute_target_size_measurements.
    target_size_measurements: list[dict] = field(default_factory=list)

    # Extracted elements
    headings: list[dict] = field(default_factory=list)
    links: list[dict] = field(default_factory=list)
    images: list[dict] = field(default_factory=list)
    form_fields: list[dict] = field(default_factory=list)
    media: list[dict] = field(default_factory=list)
    landmarks: list[dict] = field(default_factory=list)
    colors: list[dict] = field(default_factory=list)
    tables: list[dict] = field(default_factory=list)
    lists: list[dict] = field(default_factory=list)
    iframes: list[dict] = field(default_factory=list)
    background_images: list[dict] = field(default_factory=list)
    shadow_elements: list[dict] = field(default_factory=list)
    captchas: list[dict] = field(default_factory=list)
    skip_links: list[dict] = field(default_factory=list)
    viewport_meta: dict | None = None
    script_content: str = ""
    pseudo_elements: list[dict] = field(default_factory=list)
    axe_results: dict | None = None
    pixel_contrast: list[dict] = field(default_factory=list)
    # ANDI-style per-text-node contrast results. Walks every visible text
    # node, resolves the effective background by walking up the DOM until
    # a non-transparent ancestor is found (recording walk depth and any
    # background-image hit along the way), classifies large vs normal
    # text per WCAG 1.4.3, and records the contrast ratio. SVG <text>
    # nodes use fill/stroke rather than color/background-color. Each
    # entry: {selector, text, fg_color, bg_color, bg_image_present,
    # bg_walk_depth, font_size_px, font_weight, is_large_text, ratio,
    # required_ratio, passes, is_svg_text, rect, method}.
    andi_contrast_results: list[dict] = field(default_factory=list)
    # ANDI-style language audit (sANDI). Validates the document lang,
    # every element with an explicit `lang` attribute, BCP 47 validity
    # of each, redundant lang attributes (segment lang same as inherited
    # ancestor lang), xml:lang mismatches per segment, and hidden lang
    # segments. Shape:
    #   {
    #     "html_lang": str, "html_lang_valid": bool,
    #     "html_xml_lang": str, "html_lang_xml_lang_match": bool|None,
    #     "issues": list[str],   # rolled-up document-level issues
    #     "segments": list[{
    #         "selector", "tag", "lang", "lang_valid", "xml_lang",
    #         "xml_lang_matches_lang", "inherited_lang", "redundant",
    #         "text" (sample), "is_hidden", "rect"
    #     }]
    #   }
    # Consumed by SC 3.1.1 (page lang) and SC 3.1.2 (parts lang).
    andi_lang_results: dict = field(default_factory=dict)
    # ANDI-style hidden-content audit (hANDI). Catches focusable
    # elements that are simultaneously hidden — a high-yield bug class
    # (phantom tab stops, screen-reader contradictions, ARIA-spec
    # violations). Each entry: {selector, tag, role, accessible_name,
    # tabindex, naturally_focusable, tab_reachable, hidden_reasons,
    # aria_hidden_path, rect, text}.
    andi_hidden_results: list[dict] = field(default_factory=list)
    # ANDI-style graphics audit (gANDI). Per-image accessibility
    # state. Each entry: {selector, type ("img"|"svg"|"bg-image"|
    # "input-image"|"area"), src, alt, alt_present, alt_empty,
    # aria_label, aria_labelledby_resolved, role, decorative,
    # in_link_or_button, ancestor_has_other_text,
    # ancestor_link_or_button_has_name, svg_title, svg_desc,
    # svg_role, name_source, accessible_name, rect, text_overlay}.
    andi_graphics_results: list[dict] = field(default_factory=list)
    # ANDI-style tables audit (tANDI). Per-table classification (data
    # vs layout), caption/summary presence, scope/headers validation,
    # nested-table detection. Each entry: {selector, classification,
    # has_caption, caption_text, has_summary, summary_text, has_thead,
    # th_count, th_with_scope_count, th_missing_scope_selectors,
    # cells_with_headers_attr, headers_id_pairs_valid,
    # row_count, col_count, role, nested, issues}.
    andi_tables_results: list[dict] = field(default_factory=list)
    # ANDI-style links/buttons audit (lANDI). Per-element: visible
    # text, accessible name (aria-labelledby > aria-label > text >
    # title), name-vs-visible mismatch (SC 2.5.3 Label in Name),
    # ambiguous-text detection ("click here", "more", "read more"),
    # empty-name detection. Each entry: {selector, type, visible_text,
    # accessible_name, name_source, name_includes_visible,
    # name_visible_mismatch, is_ambiguous, has_no_name, image_only,
    # rect}.
    andi_interactive_results: list[dict] = field(default_factory=list)
    # Behavior-verified keyboard roundtrip per probable trigger.
    # Complements the existing modal_interactions probe (which uses a
    # strict aria-haspopup / aria-controls→dialog inventory). This
    # captures the broader case: any visible button / [role=button] /
    # hash-link / <summary> is focused, pressed with Enter (Space as
    # fallback), and if anything changed the probe verifies Escape
    # dismissibility, Tab-stays-inside trap behaviour, focus-returns-
    # to-trigger after dismiss, and Shift+Tab exits cleanly. Each
    # entry: {selector, tag, role, text, opens_on_enter,
    # opens_on_space, opened_target_selector, tab_stays_inside,
    # escape_closes, focus_returns_to_trigger, shift_tab_exits,
    # before_screenshot, after_screenshot, errors}.
    # Feeds SC 2.1.1 (operable), SC 2.1.2 (no trap), SC 2.4.3 (focus
    # order resumes), and SC 1.4.13 (content on hover/focus dismiss-
    # ible by Escape).
    keyboard_roundtrip_results: list[dict] = field(default_factory=list)
    aria_issues: list[dict] = field(default_factory=list)
    # HTML_CodeSniffer (Squiz Labs, BSD-3) WCAG conformance check results.
    # Different ruleset from axe — catches heading-skip patterns,
    # label-association cases, and Section 508 specifics axe misses.
    # Shape (matches HTMLCS_RUNNER messages):
    #   {
    #     "messages": [
    #       {"type": int, "code": str, "msg": str, "selector": str,
    #        "wcag_criterion": str},
    #       ...
    #     ],
    #     "engine": "HTML_CodeSniffer",
    #     "version": str,
    #     "standard": str,    # e.g. "WCAG2AA"
    #   }
    # Populated by ``capture/web_capture.py:_capture_htmlcs``.
    htmlcs_results: dict | None = None
    # IBM Equal Access Accessibility Checker (Apache 2.0) results. ~165
    # rules with strong ARIA-validity coverage (aria-controls/aria-owns
    # reference validity, custom-widget patterns, role-required-attr
    # checks). Shape (matches the engine's report.results array):
    #   {
    #     "results": [
    #       {"ruleId": str, "value": [level, judgment],
    #        "path": {"dom": str, "aria": str},
    #        "message": str, "snippet": str,
    #        "category": str, "help": str},
    #       ...
    #     ],
    #     "engine": "ibm-equal-access",
    #     "version": str,
    #     "policy": str,    # e.g. "IBM_Accessibility"
    #   }
    # Populated by ``capture/web_capture.py:_capture_ibm_eac``.
    ibm_eac_results: dict | None = None
    # Accessibility overlay widgets detected on the page (UserWay, AccessiBe,
    # EqualWeb, etc). These widgets inject shadow DOM, intercept keyboard,
    # and frequently violate WCAG themselves. Populated by the capture
    # pipeline; consumed by every SC check to emit an info-level finding
    # warning the auditor that focus/ARIA behaviour may be overridden.
    overlay_widgets: list[dict] = field(default_factory=list)

    # Capture phase completion tracking — records which interactive tests
    # succeeded vs failed/skipped so checks can report NOT_EVALUATED
    # instead of a false SUPPORTS when data is missing.
    capture_completions: dict = field(default_factory=dict)

    # Standalone audio-file transcripts (SC 1.2.1 Audio-only Prerecorded).
    # Each entry carries the discovered audio URL, its type (<audio> tag
    # vs link), the Whisper transcript text, segment list if available,
    # the sampled duration, and any error that occurred.
    # NOT CURRENTLY POPULATED by the capture pipeline -- the
    # transcribe_all_audio() helper exists in functions/audio_transcriber.py
    # but no capture step invokes it. SC 1.2.1 consumes capture_data.media
    # and the in-page text-alternative scan instead. Field kept so a future
    # capture step can populate it without a schema change.
    audio_transcripts: list[dict] = field(default_factory=list)

    # Dynamic aria-live / role=status / role=alert announcements captured
    # while interactive tests run. Each entry: timestamp, region
    # selector, aria-live politeness, role, new text that appeared,
    # and which interactive test was active.
    # NOT CURRENTLY POPULATED -- the at_simulation.live_observer
    # install/drain hooks the docstring references do not exist in
    # the codebase. SC 4.1.3 consumes the in-prompt VISIBLE PAGE TEXT
    # + dynamic_content + ARIA live-region scan instead. Field kept
    # for a future implementation.
    status_announcements: list[dict] = field(default_factory=list)

    # Interactive data
    tab_walk: list[dict] = field(default_factory=list)
    focus_indicators: list[dict] = field(default_factory=list)
    hover_content: list[dict] = field(default_factory=list)
    text_spacing_overflow: list[dict] = field(default_factory=list)
    text_spacing_screenshot: str = ""
    form_errors: list[dict] = field(default_factory=list)
    context_changes: list[dict] = field(default_factory=list)
    keyboard_traps: list[dict] = field(default_factory=list)
    backward_tab_walk: list[dict] = field(default_factory=list)
    tab_coverage: dict = field(default_factory=dict)
    # True when the Tab walk hit MAX_TAB_ITERATIONS without reaching <body>,
    # indicating an SPA that re-renders focusable elements on every Tab.
    # Consumers MUST treat tab_walk / reached_by_tab / coverage_percent as
    # partial data and rely on tab_coverage.focusable_but_skipped and
    # tab_coverage.not_focusable_at_all (deterministic) for findings.
    tab_walk_truncated: dict = field(default_factory=dict)
    keyboard_walkthrough_video: str = ""
    keyboard_walkthrough_log: list[dict] = field(default_factory=list)
    expanded_tab_walks: dict = field(default_factory=dict)
    audio_detection: dict = field(default_factory=dict)
    video_embed_captions: dict = field(default_factory=dict)
    skip_link_results: list[dict] = field(default_factory=list)
    # Context produced by _skip_link_verification: what the FIRST tab
    # stop after body is, and whether it's a skip link. Feeds SC 2.4.1
    # (skip link must be early in tab order -- standard practice is
    # first tab stop).
    skip_link_first_tabstop: dict = field(default_factory=dict)
    transcript_buttons: list[dict] = field(default_factory=list)
    transcript_verifications: list[dict] = field(default_factory=list)
    focus_contrast: list[dict] = field(default_factory=list)
    widget_keyboard: list[dict] = field(default_factory=list)
    # Modal open/trap/close roundtrip results from
    # capture.interactive_capture._capture_modal_interactions. Each entry:
    # trigger selector + text, whether Enter/Space opened the modal,
    # whether Tab stayed trapped inside, whether Escape closed it, and
    # whether focus returned to the trigger afterwards. Consumed by
    # SC 2.1.1 (trigger must open modal via keyboard), SC 2.1.2
    # (Escape must close -- otherwise modal is a keyboard trap), and
    # SC 2.4.3 (focus must return to trigger).
    modal_interactions: list[dict] = field(default_factory=list)
    reduced_motion: dict = field(default_factory=dict)

    # Observation
    observation_video_path: str = ""
    observation_frames: list[str] = field(default_factory=list)
    flash_analysis: dict = field(default_factory=dict)
    dynamic_content: dict = field(default_factory=dict)

    # Zoom data
    overflow_200pct: list[dict] = field(default_factory=list)
    overflow_320px: list[dict] = field(default_factory=list)
    horizontal_scroll_320: bool = False

    # Language data
    page_language: dict = field(default_factory=dict)

    # User / product context
    user_context: dict = field(default_factory=dict)
    product_context: Any = None
    # Review scope: "single" (one page), "site" (crawl), or "multi".
    # Cross-page success criteria cannot be evaluated when this is
    # "single" -- the per-SC checks gate on it.
    review_type: str = "single"

    # v2 pipeline fields
    element_inventory: dict = field(default_factory=dict)
    page_type: str = ""
    ai_removed_elements: list = field(default_factory=list)
    exploration_results: list = field(default_factory=list)
    exploration_screenshots: dict = field(default_factory=dict)
    video_segments: list = field(default_factory=list)
    form_pause_results: list = field(default_factory=list)
    at_missing_elements: list = field(default_factory=list)

    # Video-to-text descriptions (pre-processed once, reused by all checks)
    # Maps video type → text description from vision model
    video_descriptions: dict = field(default_factory=dict)

    # Code AI per-page cache (Layer 1 of the cached-code architecture).
    # code_findings: one-shot inventory of every accessibility-relevant
    # code pattern on this page, produced ONCE by
    # functions.code_analyzer.analyze_page_code and consumed by every
    # SC's run_code_analysis via functions.code_analyzer.findings_for_sc.
    # Each entry has sc_ids (list[str]), element, css_selector, issue,
    # raw_evidence (verbatim code snippet), severity, source_chunk.
    # code_findings_embeddings: bge-m3 vector per pattern, built once so
    # the judge's Layer 3 retrieval can run top-K similarity without a
    # new embedding pass per SC.
    code_findings: list[dict] = field(default_factory=list)
    code_findings_embeddings: list[list[float]] = field(default_factory=list)

    # Pipeline metadata
    capture_pipeline_version: str = "v1"
    phase_timings: dict = field(default_factory=dict)
    ai_call_count: int = 0
    review_dir: str = ""
    captures_dir: str = ""

    def to_serializable_dict(self) -> dict:
        """Serialize all capture data to a dict for saving to JSON.

        Includes every field except the raw HTML (saved separately as
        dom.html) and product_context (non-serializable object, saved
        in meta.json). File paths are kept as-is since they're relative
        to the review directory.
        """
        import dataclasses
        result = {}
        for f in dataclasses.fields(self):
            val = getattr(self, f.name)
            if f.name == "html":
                continue
            if f.name == "product_context":
                if val and hasattr(val, "to_dict"):
                    result[f.name] = val.to_dict()
                else:
                    result[f.name] = None
                continue
            result[f.name] = val
        return result

    @classmethod
    def from_serialized_dict(cls, data: dict, review_dir: str = "") -> "CaptureData":
        """Rebuild CaptureData from a saved dict.

        Loads the raw HTML from dom.html if available. All other fields
        are restored directly from the dict.
        """
        import dataclasses
        import os

        known = {f.name for f in dataclasses.fields(cls)}
        kwargs = {}
        for key, val in data.items():
            if key in known and key not in ("html", "product_context"):
                kwargs[key] = val

        cd = cls(**kwargs)

        if review_dir:
            cd.review_dir = review_dir
            cd.captures_dir = os.path.join(review_dir, "captures")
            dom_path = os.path.join(cd.captures_dir, "dom.html")
            if os.path.exists(dom_path):
                cd.html = open(dom_path, encoding="utf-8", errors="replace").read()

        pc = data.get("product_context")
        if pc and isinstance(pc, dict):
            try:
                cd.product_context = ProductContext.from_dict(pc)
            except Exception:
                # malformed legacy product_context; leave field at default
                pass

        return cd


# ── Page result (for site crawl) ─────────────────────────────────────────────

@dataclass
class PageResult:
    url: str
    results: list[TestResult] = field(default_factory=list)
    summary: dict = field(default_factory=dict)
    error: str | None = None


# ── Product context ──────────────────────────────────────────────────────────

@dataclass
class ProductContext:
    """Client/product context that flows through the entire AI pipeline."""
    sector: str = ""
    client_type: str = ""
    primary_users: list = field(default_factory=list)
    critical_workflows: list = field(default_factory=list)
    additional_context: str = ""

    def to_prompt(self) -> str:
        if not self.sector and not self.client_type and not self.additional_context:
            return ""
        lines = ["SITE CONTEXT (affects severity and priority):"]
        if self.client_type:
            lines.append(f"  Organization: {self.client_type}")
        if self.sector:
            lines.append(f"  Sector: {self.sector}")
        if self.primary_users:
            lines.append(f"  Primary users: {', '.join(self.primary_users)}")
        if self.critical_workflows:
            lines.append(f"  Critical user workflows: {', '.join(self.critical_workflows)}")
        if self.additional_context:
            lines.append(f"  Notes: {self.additional_context}")
        lines.append(
            "  Adjust severity based on this context -- issues that block "
            "critical workflows for the primary user population are more "
            "severe than issues on supplementary content."
        )
        return "\n".join(lines)

    def to_dict(self) -> dict:
        return {k: v for k, v in self.__dict__.items()}

    @classmethod
    def from_dict(cls, d: dict) -> ProductContext:
        if not d:
            return cls()
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


# ── Review metadata ──────────────────────────────────────────────────────────

@dataclass
class ReviewMeta:
    review_id: str = ""
    source_url: str = ""
    source_file: str | None = None
    file_type: str | None = None
    created_at: str = ""
    model_used: str = ""
    status: str = "queued"
    report_format: str = "VPAT"
    coverage_level: str = "AA"
    wcag_version: str = "2.2"
    company_name: str = ""
    product_name: str = ""
    company_logo_path: str = ""
    review_type: str = "single"

    # VPAT 2.5 fields
    product_description: str = ""
    contact_name: str = ""
    contact_email: str = ""
    notes: str = ""
    evaluation_methods: str = (
        "Combination of automated programmatic testing, AI-powered visual "
        "analysis, and interactive keyboard/focus testing using the "
        "DHS Trusted Tester methodology."
    )

    # Summary statistics
    overall_summary: dict = field(default_factory=dict)
    total_criteria: int = 0
    supports: int = 0
    partially_supports: int = 0
    does_not_support: int = 0
    not_applicable: int = 0
    not_evaluated: int = 0

    # Multi-page / crawl
    pages_discovered: int | None = None
    pages_tested: int | None = None
    max_pages: int | None = None
    per_page_summary: list | None = None
    page_rationale: str = ""
    page_sample: list | None = None

    error: str | None = None
    product_context: dict = field(default_factory=dict)
    user_context: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {k: v for k, v in self.__dict__.items()}

    @classmethod
    def generate_id(cls) -> str:
        now = datetime.now(timezone.utc)
        return f"{now.strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:8]}"


# ── Caption models ───────────────────────────────────────────────────────────

@dataclass
class CaptionSegment:
    start: float
    end: float
    text: str


@dataclass
class CaptionComparisonResult:
    overall_accuracy: float
    matched_segments: int
    total_segments: int
    missing_segments: list[dict] = field(default_factory=list)
    inaccurate_segments: list[dict] = field(default_factory=list)


# ── Helpers ──────────────────────────────────────────────────────────────────

def _enum_val(v: Any) -> Any:
    """Extract .value from an enum, or return as-is."""
    return v.value if isinstance(v, enum.Enum) else v
