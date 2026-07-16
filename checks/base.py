"""Base class for all WCAG check modules."""
from __future__ import annotations

import logging
import re
import time
from typing import Any

from models import (
    CaptureData,
    ConformanceLevel,
    Finding,
    Severity,
    TestResult,
    TTResult,
    TTSubTestResult,
)

# Shared helpers — extracted to functions/ so the judge, AT-sim
# summarizers, and other consumers can reuse the same vocabulary.
# Re-exported here because 30+ check files do
# `from checks.base import _make_finding_id` and similar.
from functions.dom_format import (  # noqa: F401 (re-exported)
    _LANDMARK_ENGLISH,
    _describe_location,
    _element_phrase,
    _landmark_phrase,
    _nearest_landmark,
    _nearest_section_heading,
    _vertical_zone,
)
from functions.finding_utils import (  # noqa: F401 (re-exported)
    _CONFORMANCE_ORDER,
    _make_finding_id,
    _worse,
    element_is_display_hidden,
)

# Single source of truth for the SC sets that drive both the DOM-context
# inclusion (in BaseCheck below) and the finding extractors (in
# functions/andi_extract.py / keyboard_extract.py). Imported here so a
# change in either place stays consistent.
from functions.andi_extract import _ANDI_HIDDEN_SCS, is_browser_handled  # noqa: F401
from functions.keyboard_extract import _KEYBOARD_ROUNDTRIP_SCS  # noqa: F401
from functions.keyboard_probe import widget_probe_errored

logger = logging.getLogger(__name__)


def _extract_readable_scripts(html: str, raw_script_content: str) -> str:
    """Extract human-readable JavaScript from the page for code AI analysis.

    Returns all inline <script> blocks that are readable (not minified).
    Minified bundles are detected by their lack of newlines — if a script
    block has >500 chars and <5 newlines, it's minified library code that
    the LLM cannot meaningfully parse.

    The AI still sees everything that matters for accessibility:
    - Event handlers, focus management, ARIA manipulation
    - DOMContentLoaded / load handlers
    - Form validation logic
    - Keyboard trap patterns

    Minified bundles (React core, jQuery, webpack output) are noted
    in a summary line so the AI knows they exist.
    """
    import re

    # Extract inline <script> blocks from the DOM
    script_blocks = re.findall(
        r'<script[^>]*>(.*?)</script>', html, re.DOTALL
    )

    readable_parts: list[str] = []
    skipped_count = 0
    skipped_chars = 0

    for i, block in enumerate(script_blocks):
        block = block.strip()
        if not block or len(block) < 10:
            continue

        # Detect minified code: long block with very few newlines
        newline_count = block.count('\n')
        is_minified = len(block) > 500 and newline_count < max(5, len(block) // 1000)

        if is_minified:
            skipped_count += 1
            skipped_chars += len(block)
        else:
            readable_parts.append(f"// --- Inline script block {i + 1} ---\n{block}")

    # Also include any non-minified content from raw_script_content.
    # Always included — size is the code-AI chunker's problem, not ours.
    # The chunker in functions.chunker.chunk_text splits at sentence
    # boundaries with no data loss, and run_code_analysis iterates
    # chunks, so arbitrarily large script_content is safe.
    if raw_script_content:
        readable_parts.append(f"// --- Extracted script content ---\n{raw_script_content}")

    result = "\n\n".join(readable_parts)

    if skipped_count:
        result += (
            f"\n\n// --- {skipped_count} minified library bundle(s) skipped "
            f"({skipped_chars:,} chars) ---\n"
            f"// These are compiled/minified frameworks (React, jQuery, webpack output)\n"
            f"// that the LLM cannot parse. All readable scripts are included above."
        )

    return result


class BaseCheck:
    """Abstract base for every WCAG success-criterion check.

    Subclasses MUST set the class-level attributes and implement at least
    ``run_programmatic``.
    """

    criterion_id: str = ""
    criterion_name: str = ""
    level: str = "A"
    wcag_versions: list[str] = ["2.0", "2.1", "2.2"]
    guideline: str = ""
    principle: str = ""
    ict_baseline: str = ""
    tt_tests: list[str] = []
    normative_text: str = ""
    off_scope_keywords: dict = {}
    # Set True on checks that require browser interactivity (keyboard, focus,
    # hover, timing, etc.) — these auto-skip for document file types.
    web_only: bool = False
    # Set to a list of file types this check applies to.
    # Empty = applies to all content types (web + all docs).
    # Example: ["pdf"] = only runs for PDF files.
    # Example: ["pdf", "docx", "xlsx", "pptx"] = all document types.
    doc_types: list[str] = []
    # Set True on success criteria that can only be evaluated by comparing
    # content across MULTIPLE pages (3.2.3 Consistent Navigation, 3.2.4
    # Consistent Identification, 3.2.6 Consistent Help). On a single-page
    # review these are deterministically marked Not Applicable for the
    # review's scope -- a one-page evaluation has nothing to compare, and
    # asking the judge to rule on them produced ungrounded "Supports"
    # verdicts (verified on fairfaxva.gov run 20260515_230613_ff643865).
    requires_multipage: bool = False

    # Set True on success criteria whose APPLICABILITY depends on page
    # meaning rather than mechanical element existence -- e.g. "does this
    # page have a time limit / a motion-actuated control / an
    # authentication step / a consequential transaction". Those checks'
    # is_applicable() inspects HTML/JS with keyword or regex scans, which
    # are brittle: a false negative makes is_applicable() return False,
    # the SC is auto-marked Not Applicable, and the AI never evaluates it
    # -- silently burying a real violation. When this flag is True a
    # False is_applicable() result is treated as ADVISORY, not a gate:
    # the SC still runs the full pipeline and the AI judge decides
    # applicability from the whole page (the judge already returns Not
    # Applicable when a criterion genuinely does not apply). Mechanical
    # element-existence checks (bool(capture_data.images), etc.) keep the
    # flag False -- those are cheap, unambiguous, and safe to gate on.
    ai_judged_applicability: bool = False

    # ── Programmatic-definitive criteria ────────────────────────────
    # Attribute/element EXISTENCE equals CORRECTNESS for these SCs.
    # Pipeline: programmatic + axe-core → judge VPAT synthesis only.
    # Skips visual AI, code AI, AT simulation.
    PROGRAMMATIC_DEFINITIVE: set[str] = {
        "4.1.1",   # Parsing — duplicate IDs, malformed HTML
        "3.1.1",   # Language of Page — lang attr + BCP 47
        "2.4.2",   # Page Titled — <title> element
        "1.3.5",   # Identify Input Purpose — autocomplete mapping
        "2.5.3",   # Label in Name — visible text ⊂ accessible name
        "3.3.2",   # Labels or Instructions — form label presence
        "2.3.1",   # Three Flashes — mathematical frame analysis (when data exists)
        # ── Promoted to definitive 2026-04-29 after a university + a community college verification ──
        # The AI repeatedly produced the SAME false-positive patterns on
        # both sites (carousel "no alternative" with prev/next visible,
        # orientation lock hallucinations, target-size flagging despite
        # 24px spacing exception). The deterministic data fully answers
        # each — AI input adds noise, not signal. PROGRAMMATIC_DEFINITIVE
        # skips visual_ai / code_ai / at_sim entirely, saving ~3 LLM
        # calls per SC per page. Only the judge VPAT-synthesis call
        # runs (unavoidable for VPAT report text generation).
        "1.3.4",   # Orientation — CSS @media (orientation:...) scan
        "2.5.7",   # Dragging Movements — drag patterns + carousel-control alternatives
        "2.5.8",   # Target Size (Minimum) — rect math + WCAG spacing exception
        # ── Second definitive sweep 2026-04-29 ──
        # Same audit logic: deterministic data fully answers, AI cannot
        # see anything beyond what programmatic already extracts.
        "1.4.4",   # Resize Text — viewport meta + overflow_200pct
        "1.4.10",  # Reflow — horizontal_scroll_320 + overflow_320px
        "2.4.5",   # Multiple Ways — landmark/search/sitemap detection (single-page bound)
        "2.4.11",  # Focus Not Obscured — focus_indicators.obscured + sticky/fixed CSS
        "2.5.4",   # Motion Actuation — script scan for DeviceMotion / Accelerometer / Gyroscope
    }

    # ── Deterministic-with-escalation criteria ──────────────────────
    # These SCs have strong deterministic programmatic checks that
    # produce a clean verdict MOST of the time. When the programmatic
    # check returns high confidence (>= DETERMINISTIC_CONFIDENCE_FLOOR),
    # we fast-path to the judge for VPAT synthesis only. When confidence
    # is lower (ambiguous edge cases, e.g. 1.4.3 contrast over a
    # background image where pixel sampling can't give a reliable
    # ratio), we fall through to the full AI pipeline.
    #
    # The deterministic logic for each of these lives in its own
    # check file:
    #   1.4.3 / 1.4.11   -> checks_1_4.py, uses functions.contrast
    #   2.4.7 / 2.4.11   -> checks_2_4.py, uses tab_walk + focus_indicators
    #   2.1.1 / 2.1.2 / 2.1.4 -> checks_2_1.py
    #   2.4.1 / 2.4.3 / 2.4.4 / 2.4.5 / 2.4.6 -> checks_2_4.py
    #   1.3.1            -> checks_1_3.py (heading/form/table/landmark)
    #   1.4.10 / 1.4.12  -> checks_1_4.py (overflow + text-spacing capture)
    #   3.3.1 / 3.3.3 / 3.3.4 -> checks_3_3.py
    #   4.1.2 / 4.1.3    -> checks_4_1.py + functions.aria_validator
    #
    # When the programmatic confidence falls below the floor, the full
    # AI stack runs so the judge can resolve the ambiguity. Nothing is
    # silently skipped -- escalation is the default when in doubt.
    DETERMINISTIC_WITH_ESCALATION: set[str] = {
        "1.1.1",
        "1.3.1",
        "1.4.3",
        "1.4.10",
        "1.4.11",
        "1.4.12",
        "2.1.1",
        "2.1.2",
        "2.1.4",
        "2.4.1",
        "2.4.3",
        "2.4.4",
        "2.4.5",
        "2.4.6",
        "2.4.7",
        "2.4.11",
        "3.1.2",
        "3.3.1",
        "3.3.3",
        "3.3.4",
        "4.1.2",
        "4.1.3",
    }

    # Minimum programmatic confidence required to fast-path a
    # DETERMINISTIC_WITH_ESCALATION SC. Below this, the full AI stack
    # runs so the judge can evaluate ambiguous cases with fuller context.
    DETERMINISTIC_CONFIDENCE_FLOOR: float = 0.85

    # Minimum per-source confidence to count a verdict in the
    # multi-source majority vote. A source returning a verdict below
    # this floor is effectively saying "I don't know" -- including it
    # as a vote is dishonest and lets low-confidence noise outvote
    # high-confidence honest verdicts (verified on fairfaxva.gov run
    # 20260514_205147_cb3b646c SC 3.2.6: Visual AI returned NA at 1.0
    # but Programmatic 0.3 + Code AI 0.75 overrode it to Supports).
    _VOTE_CONFIDENCE_FLOOR: float = 0.5

    # Per-SC deterministic measurement sources. Maps a metric name the
    # judge may cite in a finding's structured ``cited_measurements`` to
    # the (capture_data attribute, dict field) holding that metric's
    # ground truth. The post-judge claim validator
    # (functions/claim_validator.py) uses this to verify every cited
    # value against the captured data and demote unsupported claims.
    # Empty by default; SC subclasses with deterministic measurements
    # override it. Keeping the map in the SC module -- not centralized --
    # is what makes the validator generic and the design modular.
    measurement_sources: dict[str, tuple[str, str]] = {}

    # Per-SC AI source exclusion. When a specific source has been
    # observed to produce systematically wrong findings on a specific
    # SC, exclude it here. The source is not called, its findings never
    # reach the judge, and its tier of evidence is omitted from the
    # final source-agreement vote.
    #
    # Each entry must include a one-line justification anchored in
    # specific failed findings (which finding, what evidence the source
    # quoted that contradicted its own claim). Adding entries without
    # ground-truth verification costs real signal and creates false
    # negatives. Remove the entry once the bad pattern is fixed in the
    # source's prompt or by upgrading the model.
    EXCLUDED_AI_SOURCES_PER_SC: dict[str, set[str]] = {
        # SC 1.1.1: Code AI ignored the ARIA 1.2 name-cascade rule
        # despite the rule appearing in its system prompt. On
        # 20260428_162800_c2e62ddb it produced 2 findings whose
        # quoted evidence contradicted the claim:
        #   F2: <button class="navbar-toggler" aria-label="Open menu"
        #        aria-expanded="false"> — claimed "lacks accessible
        #        name" though aria-label resolves step 2 of the cascade.
        #   F3: <button id="manualConsentoptout">Manage my privacy
        #        settings</button> — claimed "lacks accessible name"
        #        though button text resolves step 3 of the cascade.
        # Visual AI correctly produced the only valid finding (the
        # filename-as-alt hero image). Excluding code_ai here keeps
        # the real verdict and removes the two false positives.
        "1.1.1": {"code_ai"},
    }

    # Maps criteria to required interactive capture tests.
    _REQUIRED_CAPTURES: dict[str, list[str]] = {
        "2.1.1": ["tab_walk", "keyboard_walkthrough"],
        "2.1.2": ["tab_walk", "keyboard_walkthrough"],
        "2.1.4": ["tab_walk"],
        "2.4.1": ["skip_links"],
        "2.4.3": ["tab_walk"],
        "2.4.7": ["focus_indicators", "keyboard_walkthrough"],
        "2.4.11": ["focus_indicators"],
        "1.4.2": ["media_playback", "audio_detection"],
        "1.4.12": ["text_spacing"],
        "1.4.13": ["hover_detection"],
        "2.2.2": ["media_playback"],
        "3.2.1": ["context_changes"],
        "3.2.2": ["context_changes"],
        "3.3.1": ["form_submission"],
        "1.2.1": ["media_recording"],
        "1.2.2": ["media_recording", "caption_toggle_recording"],
        "1.2.3": ["media_recording"],
        "1.2.4": ["media_recording", "caption_toggle_recording"],
        "1.2.5": ["media_recording"],
    }

    # Content-presence indicators for needs_review detection.
    _CONTENT_INDICATORS: dict[str, list[str]] = {
        "1.1.1": ["images", "background_images", "captchas"],
        "1.2.1": ["media"], "1.2.2": ["media"], "1.2.3": ["media"],
        "1.2.4": ["media"], "1.2.5": ["media"],
        "1.3.1": ["headings", "form_fields", "tables", "landmarks"],
        "1.3.5": ["form_fields"],
        "1.4.1": ["links", "images", "colors"],
        "1.4.3": ["computed_styles", "colors"],
        "1.4.13": ["hover_content"],
        "2.1.1": ["tab_walk"],
        "2.4.4": ["links"],
        "2.4.6": ["headings"],
        "3.3.1": ["form_fields", "form_errors"],
        "3.3.2": ["form_fields"],
        "4.1.2": ["form_fields", "links"],
    }

    # Common cross-criterion confusions — auto-filtered based on criterion prefix
    # Common cross-criterion confusions — auto-filtered based on criterion prefix.
    # The Code AI in particular tends to report onfocus="blur()" on every
    # criterion — it belongs to 2.1/2.4 only.
    # _AUTO_OFF_SCOPE removed — the judge AI handles cross-criterion
    # relevance filtering.  Rather than maintaining a brittle keyword
    # matrix, the judge reads each finding against the criterion
    # definition and rejects findings that belong to other criteria.
    # This is more accurate because the judge understands MEANING,
    # not just keyword presence (e.g., "contrast" in a finding about
    # focus indicators is NOT a 1.4.3 issue — the judge knows this).

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    async def run(
        self,
        capture_data: CaptureData,
        ai_client: Any | None = None,
    ) -> TestResult:
        """Execute the full check pipeline and return a TestResult."""
        start = time.monotonic()
        is_fast = self.criterion_id in self.PROGRAMMATIC_DEFINITIVE
        path_label = "FAST" if is_fast else "FULL"

        logger.info(
            "━━━ SC %s (%s) — Level %s — %s PATH ━━━",
            self.criterion_id, self.criterion_name, self.level, path_label,
        )

        result = TestResult(
            criterion_id=self.criterion_id,
            criterion_name=self.criterion_name,
            level=self.level,
            wcag_versions=list(self.wcag_versions),
            ict_baseline=self.ict_baseline,
        )

        try:
            _doc_types = {"pdf", "docx", "xlsx", "pptx"}
            _is_document = getattr(capture_data, "file_type", "") in _doc_types
            _review_type = getattr(capture_data, "review_type", "single")
            _multipage_unavailable = (
                self.requires_multipage and _review_type == "single"
            )

            # Applicability gate. A False is_applicable() result hard-gates
            # the SC to Not Applicable ONLY when applicability is a
            # mechanical element-existence check. For SCs whose
            # applicability depends on page meaning (ai_judged_applicability
            # = True), a False keyword/regex scan is advisory: the SC still
            # runs and the AI judge decides, so a brittle keyword miss
            # cannot silently bury a real violation.
            _applicable = self.is_applicable(capture_data)
            _keyword_gate_na = (
                (not _applicable) and (not self.ai_judged_applicability)
            )
            if (not _applicable) and self.ai_judged_applicability:
                logger.info(
                    "SC %s: keyword applicability scan was negative, but "
                    "applicability is AI-judged — running full pipeline so "
                    "the judge decides from the whole page.",
                    self.criterion_id,
                )

            if _multipage_unavailable:
                # Cross-page criterion on a single-page review: there is
                # nothing to compare across pages, so the criterion cannot
                # be evaluated within this review's scope. Deterministic
                # Not Applicable -- no judge call, no guessing.
                logger.info(
                    "SC %s: NOT APPLICABLE — cross-page criterion, "
                    "single-page review", self.criterion_id,
                )
                result.conformance_level = ConformanceLevel.NOT_APPLICABLE
                result.confidence = 1.0
                result.summary = (
                    f"SC {self.criterion_id} ({self.criterion_name}) requires "
                    f"comparing content across multiple pages of the site. "
                    f"This review evaluated a single page, so the criterion "
                    f"cannot be assessed and is marked Not Applicable for the "
                    f"scope of this review. A multi-page review is required to "
                    f"evaluate it."
                )
                result.programmatic_conformance = ConformanceLevel.NOT_APPLICABLE
                result.programmatic_confidence = 1.0
                result.ai_conformance = ConformanceLevel.NOT_APPLICABLE
                result.ai_confidence = 1.0
                result.tt_results = self._generate_tt_results([], capture_data)
            elif (self.web_only and _is_document) or _keyword_gate_na:
                logger.info("SC %s: NOT APPLICABLE", self.criterion_id)
                result.conformance_level = ConformanceLevel.NOT_APPLICABLE
                result.confidence = 1.0
                result.summary = (
                    f"SC {self.criterion_id} is not applicable: "
                    f"no relevant content detected on this page."
                )
                result.programmatic_conformance = ConformanceLevel.NOT_APPLICABLE
                result.programmatic_confidence = 1.0
                result.ai_conformance = ConformanceLevel.NOT_APPLICABLE
                result.ai_confidence = 1.0
                result.tt_results = self._generate_tt_results([], capture_data)
            else:
                result = await self.execute(capture_data, ai_client)
        except Exception as exc:
            logger.exception(
                "Error running check SC %s: %s", self.criterion_id, exc
            )
            result.conformance_level = ConformanceLevel.NOT_EVALUATED
            result.error = str(exc)
            result.summary = f"Check failed with error: {exc}"

        result.duration = round(time.monotonic() - start, 2)

        # Dynamic confidence adjustment + needs_review flagging
        self._adjust_confidence_and_flag_review(result, capture_data)

        logger.info(
            "━━━ SC %s DONE in %.1fs — %s (%.0f%%)%s — %d findings ━━━",
            self.criterion_id, result.duration,
            result.conformance_level.value if hasattr(result.conformance_level, 'value') else str(result.conformance_level),
            result.confidence * 100,
            " ⚠ NEEDS REVIEW" if result.needs_review else "",
            len(result.findings),
        )
        return result

    # ------------------------------------------------------------------
    # Applicability
    # ------------------------------------------------------------------

    def is_applicable(self, capture_data: CaptureData) -> bool:
        """Return True if the page has content relevant to this criterion.

        Subclasses should override for fine-grained applicability.
        """
        return True

    # ------------------------------------------------------------------
    # Execution pipeline
    # ------------------------------------------------------------------

    async def execute(
        self,
        capture_data: CaptureData,
        ai_client: Any | None = None,
    ) -> TestResult:
        """Run programmatic and (optionally) AI analysis, then reconcile.

        Two paths:
        - PROGRAMMATIC_DEFINITIVE: programmatic + axe → judge VPAT synthesis only
        - All others: full 4-source pipeline → judge arbitration
        """
        is_definitive = self.criterion_id in self.PROGRAMMATIC_DEFINITIVE
        result = TestResult(
            criterion_id=self.criterion_id,
            criterion_name=self.criterion_name,
            level=self.level,
            wcag_versions=list(self.wcag_versions),
            ict_baseline=self.ict_baseline,
        )

        # -- Capture completeness check ------------------------------------
        capture_gap = self._check_capture_completeness(capture_data)
        if capture_gap:
            logger.warning("SC %s: %s", self.criterion_id, capture_gap)

        # -- Programmatic analysis (always runs) ---------------------------
        logger.info("SC %s: running programmatic checks...", self.criterion_id)
        prog_conf, prog_confidence, prog_findings = await self.run_programmatic(
            capture_data
        )

        # Helper: when an extension layer (axe / ANDI / keyboard probe)
        # contributes findings, recompute conformance based on the WORST
        # severity in the *full* prog_findings list, not just the
        # extension's own batch. Earlier code only bumped severity when
        # prog_conf was already SUPPORTS / NE / NA, which left
        # PARTIALLY_SUPPORTS verdicts stranded even after a HIGH finding
        # arrived. SC 1.3.1 / 2.1.1 / 2.4.4 / 2.4.7 were miscategorised
        # this way on the prior university run. Always upgrade to the worst
        # outcome the findings warrant; never downgrade a stricter
        # verdict (a programmatic DNS stays DNS even if the layer only
        # adds info findings).
        def _bump_conformance(current: ConformanceLevel, all_findings: list[Finding]) -> ConformanceLevel:
            has_high = any(f.severity == Severity.HIGH for f in all_findings)
            has_med = any(f.severity == Severity.MEDIUM for f in all_findings)
            if has_high:
                return ConformanceLevel.DOES_NOT_SUPPORT
            if has_med:
                # Don't downgrade DNS to PARTIALLY when only mediums exist
                # in the full list — DNS may have come from earlier high
                # findings the helper hasn't seen, OR the prog check
                # explicitly returned DNS. The helper is monotonic-up.
                if current == ConformanceLevel.DOES_NOT_SUPPORT:
                    return current
                return ConformanceLevel.PARTIALLY_SUPPORTS
            # No high or medium findings; preserve the current verdict.
            return current

        # -- Axe-core integration ------------------------------------------
        axe_findings = self._extract_axe_findings(capture_data)
        if axe_findings:
            logger.info("SC %s: merged %d axe-core findings", self.criterion_id, len(axe_findings))
            prog_findings.extend(axe_findings)
            prog_conf = _bump_conformance(prog_conf, prog_findings)
            prog_confidence = max(prog_confidence, 0.9)

        # -- HTML_CodeSniffer (Squiz Labs, BSD-3) -------------------------
        # Second deterministic ruleset, independent of axe. The judge sees
        # both as input findings so cross-tool agreement strengthens its
        # verdict; one tool alone is a candidate the judge evaluates.
        htmlcs_findings = self._extract_htmlcs_findings(capture_data)
        if htmlcs_findings:
            logger.info("SC %s: merged %d HTMLCS findings", self.criterion_id, len(htmlcs_findings))
            prog_findings.extend(htmlcs_findings)
            prog_conf = _bump_conformance(prog_conf, prog_findings)
            prog_confidence = max(prog_confidence, 0.9)

        # -- IBM Equal Access (Apache 2.0) -------------------------------
        # Third deterministic ruleset, strongest on ARIA validity.
        ibm_eac_findings = self._extract_ibm_eac_findings(capture_data)
        if ibm_eac_findings:
            logger.info("SC %s: merged %d IBM EAC findings", self.criterion_id, len(ibm_eac_findings))
            prog_findings.extend(ibm_eac_findings)
            prog_conf = _bump_conformance(prog_conf, prog_findings)
            prog_confidence = max(prog_confidence, 0.9)

        # -- ANDI per-text-node contrast (SCs 1.4.3 and 1.4.6 only) ───────
        andi_findings = self._extract_andi_contrast_findings(capture_data)
        if andi_findings:
            logger.info("SC %s: merged %d ANDI contrast findings", self.criterion_id, len(andi_findings))
            prog_findings.extend(andi_findings)
            prog_conf = _bump_conformance(prog_conf, prog_findings)
            prog_confidence = max(prog_confidence, 0.9)

        # -- ANDI lang audit (SCs 3.1.1 and 3.1.2 only) ────────────────────
        andi_lang_findings = self._extract_andi_lang_findings(capture_data)
        if andi_lang_findings:
            logger.info(
                "SC %s: merged %d ANDI lang findings",
                self.criterion_id, len(andi_lang_findings),
            )
            prog_findings.extend(andi_lang_findings)
            prog_conf = _bump_conformance(prog_conf, prog_findings)
            prog_confidence = max(prog_confidence, 0.9)

        # -- ANDI hidden-content audit (focus-relevant SCs only) ──────────
        andi_hidden_findings = self._extract_andi_hidden_findings(capture_data)
        if andi_hidden_findings:
            logger.info(
                "SC %s: merged %d ANDI hidden findings",
                self.criterion_id, len(andi_hidden_findings),
            )
            prog_findings.extend(andi_hidden_findings)
            prog_conf = _bump_conformance(prog_conf, prog_findings)
            prog_confidence = max(prog_confidence, 0.9)

        # -- ANDI graphics audit (SCs 1.1.1 and 1.4.5 only) ───────────────
        andi_graphics_findings = self._extract_andi_graphics_findings(capture_data)
        if andi_graphics_findings:
            logger.info(
                "SC %s: merged %d ANDI graphics findings",
                self.criterion_id, len(andi_graphics_findings),
            )
            prog_findings.extend(andi_graphics_findings)
            prog_conf = _bump_conformance(prog_conf, prog_findings)
            prog_confidence = max(prog_confidence, 0.9)

        # -- ANDI tables audit (SC 1.3.1 only) ────────────────────────────
        andi_tables_findings = self._extract_andi_tables_findings(capture_data)
        if andi_tables_findings:
            logger.info(
                "SC %s: merged %d ANDI tables findings",
                self.criterion_id, len(andi_tables_findings),
            )
            prog_findings.extend(andi_tables_findings)
            prog_conf = _bump_conformance(prog_conf, prog_findings)
            prog_confidence = max(prog_confidence, 0.9)

        # -- Keyboard roundtrip probe (SCs 2.1.1, 2.1.2, 2.4.3, 1.4.13) ───
        kb_rt_findings = self._extract_keyboard_roundtrip_findings(capture_data)
        if kb_rt_findings:
            logger.info(
                "SC %s: merged %d keyboard roundtrip findings",
                self.criterion_id, len(kb_rt_findings),
            )
            prog_findings.extend(kb_rt_findings)
            prog_conf = _bump_conformance(prog_conf, prog_findings)
            prog_confidence = max(prog_confidence, 0.9)

        # -- ANDI links/buttons audit (SCs 2.4.4, 2.5.3, 4.1.2) ───────────
        andi_inter_findings = self._extract_andi_interactive_findings(capture_data)
        if andi_inter_findings:
            logger.info(
                "SC %s: merged %d ANDI interactive findings",
                self.criterion_id, len(andi_inter_findings),
            )
            prog_findings.extend(andi_inter_findings)
            prog_conf = _bump_conformance(prog_conf, prog_findings)
            prog_confidence = max(prog_confidence, 0.9)

        # Lower confidence when required capture data is missing
        if capture_gap and prog_conf == ConformanceLevel.SUPPORTS and not prog_findings:
            prog_confidence = min(prog_confidence, 0.4)
            logger.warning("SC %s: capture gap + 0 findings → confidence %.0f%%",
                           self.criterion_id, prog_confidence * 100)

        result.programmatic_conformance = prog_conf
        result.programmatic_confidence = prog_confidence
        result.programmatic_findings_count = len(prog_findings)
        logger.info(
            "SC %s programmatic: %s (%.0f%%) — %d findings",
            self.criterion_id, prog_conf.value, prog_confidence * 100, len(prog_findings),
        )

        # NB: do NOT force source="programmatic" here. prog_findings has
        # already absorbed ANDI / axe / keyboard extractor output above,
        # each tagged with its actual source ("andi", "axe", etc). The
        # validate_source_attribution check downstream uses these tags to
        # match judge-output findings against deterministic input findings;
        # a blanket override demotes every legitimate finding to
        # judge_inference (since the judge correctly retags ANDI findings
        # as "andi" based on issue text), which the FAST-PATH ENFORCEMENT
        # block then drops -- leaving Partial/DNS verdicts with 0 findings.
        # Observed on a university run f8d46924 SC 1.3.1 / 1.4.4 / 2.4.3 / 2.4.4 /
        # 2.4.7 / 2.5.3 / 4.1.1 / 4.1.2 (all 0-findings DNS verdicts).
        # The Finding dataclass default (`source = "programmatic"`) covers
        # checks that don't set source explicitly; we don't need to enforce
        # it here.

        # Emit info findings for capture-level gaps that every SC should
        # surface to the auditor: cross-origin iframes the tool could not
        # enter, and accessibility overlay widgets that may override
        # focus/ARIA on the page. These are not SC-specific failures --
        # they are notices that the automated evidence is incomplete and
        # manual review is recommended for this criterion.
        prog_findings.extend(self._emit_untraversable_iframe_findings(capture_data))
        prog_findings.extend(self._emit_overlay_widget_findings(capture_data))

        all_findings = list(prog_findings)

        # ── PROGRAMMATIC FAST PATH ────────────────────────────────────
        # If programmatic returned NOT_EVALUATED (e.g. 2.3.1 with no flash
        # data), fall through to full pipeline instead of fast-pathing.
        if is_definitive and prog_conf != ConformanceLevel.NOT_EVALUATED:
            return await self._execute_programmatic_fast_path(
                result, all_findings, prog_conf, prog_confidence,
                capture_data, ai_client,
            )
        elif is_definitive:
            logger.info("SC %s: definitive but programmatic returned NOT_EVALUATED — using full pipeline",
                        self.criterion_id)

        # ── DETERMINISTIC-WITH-ESCALATION FAST PATH ──────────────────
        # When the programmatic check for a DETERMINISTIC_WITH_ESCALATION
        # SC produces a high-confidence verdict (captured all required
        # data, no edge-case ambiguity), trust it and route to the judge
        # for VPAT-language rewriting only. Lower confidence means the
        # check wasn't sure -- fall through to the full AI stack so the
        # judge can resolve the ambiguity with fuller evidence.
        is_deterministic = self.criterion_id in self.DETERMINISTIC_WITH_ESCALATION
        if (
            is_deterministic
            and prog_conf != ConformanceLevel.NOT_EVALUATED
            and prog_confidence >= self.DETERMINISTIC_CONFIDENCE_FLOOR
        ):
            logger.info(
                "SC %s: deterministic-tier fast path (confidence %.0f%% >= %.0f%%)",
                self.criterion_id,
                prog_confidence * 100,
                self.DETERMINISTIC_CONFIDENCE_FLOOR * 100,
            )
            return await self._execute_programmatic_fast_path(
                result, all_findings, prog_conf, prog_confidence,
                capture_data, ai_client,
            )
        elif is_deterministic:
            logger.info(
                "SC %s: deterministic tier confidence %.0f%% below floor "
                "%.0f%% (or NOT_EVALUATED) -- escalating to full AI pipeline",
                self.criterion_id,
                prog_confidence * 100,
                self.DETERMINISTIC_CONFIDENCE_FLOOR * 100,
            )

        # ── FULL PIPELINE (non-definitive SCs continue below) ─────────

        # -- AI analysis (optional) ----------------------------------------
        ai_conf = ConformanceLevel.NOT_EVALUATED
        ai_confidence = 0.0
        ai_findings: list[Finding] = []

        excluded_sources = self.EXCLUDED_AI_SOURCES_PER_SC.get(self.criterion_id, set())
        # Accept both the legacy "ai" key and the canonical "visual_ai" key
        # in EXCLUDED_AI_SOURCES_PER_SC so existing entries still work while
        # new entries use the source tag the judge actually emits.
        if ai_client is not None and not (excluded_sources & {"ai", "visual_ai"}):
            try:
                logger.debug("SC %s: running visual AI analysis...", self.criterion_id)
                ai_conf, ai_confidence, ai_findings = await self.run_ai_analysis(
                    capture_data, ai_client, {
                        "conformance": prog_conf.value,
                        "confidence": prog_confidence,
                        "findings": [f.to_dict() for f in prog_findings],
                    }
                )
                # Tag findings with the canonical "visual_ai" source. The judge's
                # tool schema enum, the source-attribution validator's index, and
                # JUDGE_TOOL.source.description ALL expect "visual_ai". Tagging
                # them "ai" caused the validator's lookup to fail (empty
                # by_source["visual_ai"] bucket), demoting every visual-AI
                # finding to judge_inference and triggering needs_review across
                # ~30% of SCs on real runs (verified on 20260506_135324_f8765656).
                for f in ai_findings:
                    f.source = "visual_ai"

                # Drop landmark-order hallucinations BEFORE the judge sees
                # them. Verified failure mode on A11Y Project SC 1.3.2:
                # visual_ai claimed "main is placed AFTER navigation and
                # footer in the accessibility tree" -- captured a11y tree
                # showed main BEFORE both. The LLM had the correct data
                # in the prompt and inverted its conclusion. The cross-
                # check is pure-Python verification against
                # capture_data.a11y_tree -- if the direction in the claim
                # contradicts the captured order, drop. Conservative when
                # the claim isn't a landmark-order assertion or when the
                # a11y tree lacks the landmarks.
                try:
                    from functions.landmark_order_verifier import (
                        filter_landmark_order_hallucinations,
                    )
                    ai_findings, _dropped = filter_landmark_order_hallucinations(
                        ai_findings,
                        getattr(capture_data, "a11y_tree", None),
                        drop=True,
                        log_label=f"SC {self.criterion_id}",
                    )
                except Exception:
                    logger.warning(
                        "SC %s: landmark-order verifier raised; keeping all "
                        "visual_ai findings", self.criterion_id, exc_info=True,
                    )

                # Filter off-scope findings
                pre_filter = len(ai_findings)
                ai_findings = self._filter_off_scope_findings(ai_findings)
                filtered = pre_filter - len(ai_findings)
                # If all AI findings were off-scope, downgrade the AI verdict
                if filtered > 0 and len(ai_findings) == 0 and ai_conf in (
                    ConformanceLevel.DOES_NOT_SUPPORT,
                    ConformanceLevel.PARTIALLY_SUPPORTS,
                ):
                    logger.info(
                        "SC %s: all %d visual AI findings were off-scope — "
                        "overriding AI verdict to Supports",
                        self.criterion_id, filtered,
                    )
                    ai_conf = ConformanceLevel.SUPPORTS
                logger.info(
                    "SC %s visual AI: %s (%.0f%%) — %d findings%s",
                    self.criterion_id, ai_conf.value, ai_confidence * 100, len(ai_findings),
                    f" ({filtered} off-scope filtered)" if filtered else "",
                )
                all_findings.extend(ai_findings)
            except Exception as exc:
                logger.warning(
                    "AI analysis failed for SC %s: %s", self.criterion_id, exc
                )
                ai_conf = ConformanceLevel.NOT_EVALUATED
                ai_confidence = 0.0

        result.ai_conformance = ai_conf
        result.ai_confidence = ai_confidence
        result.ai_findings_count = len(ai_findings)

        # -- Brief pause between AI calls to let model free memory ----------
        if ai_client is not None:
            import asyncio as _asyncio
            await _asyncio.sleep(2)

        # -- Code AI analysis (reads HTML/CSS/JS source) --------------------
        code_ai_conf = ConformanceLevel.NOT_EVALUATED
        code_ai_confidence = 0.0
        code_ai_findings: list[Finding] = []

        if (ai_client is not None and capture_data.html
                and "code_ai" not in excluded_sources):
            try:
                logger.debug("SC %s: running code AI analysis...", self.criterion_id)
                code_ai_conf, code_ai_confidence, code_ai_findings = await self.run_code_analysis(
                    capture_data, ai_client,
                )
                # Tag findings
                for f in code_ai_findings:
                    f.source = "code_ai"
                # Filter off-scope
                pre_code_filter = len(code_ai_findings)
                code_ai_findings = self._filter_off_scope_findings(code_ai_findings)
                code_filtered = pre_code_filter - len(code_ai_findings)
                if code_filtered > 0 and len(code_ai_findings) == 0 and code_ai_conf in (
                    ConformanceLevel.DOES_NOT_SUPPORT,
                    ConformanceLevel.PARTIALLY_SUPPORTS,
                ):
                    logger.info(
                        "SC %s: all %d code AI findings were off-scope — "
                        "overriding code AI verdict to Supports",
                        self.criterion_id, code_filtered,
                    )
                    code_ai_conf = ConformanceLevel.SUPPORTS
                logger.info(
                    "SC %s code AI: %s (%.0f%%) — %d findings",
                    self.criterion_id, code_ai_conf.value, code_ai_confidence * 100, len(code_ai_findings),
                )
                all_findings.extend(code_ai_findings)
            except Exception as exc:
                logger.warning(
                    "Code AI analysis failed for SC %s: %s", self.criterion_id, exc
                )

        result.code_ai_conformance = code_ai_conf
        result.code_ai_confidence = code_ai_confidence
        result.code_ai_findings_count = len(code_ai_findings)

        # -- AT Simulation (screen reader / keyboard nav) ------------------
        at_sim_conf = ConformanceLevel.NOT_EVALUATED
        at_sim_confidence = 0.0
        at_sim_findings: list[Finding] = []

        if (capture_data.a11y_tree and capture_data.a11y_tree.get("nodes")
                and "at_sim" not in excluded_sources):
            try:
                from at_simulation.screen_reader import simulate_screen_reader

                logger.debug("SC %s: running AT simulation...", self.criterion_id)
                sim_conf_str, sim_confidence, sim_raw_findings = simulate_screen_reader(
                    capture_data.a11y_tree, self.criterion_id, capture_data,
                )

                # Map string conformance to enum
                conf_map = {v.value: v for v in ConformanceLevel}
                at_sim_conf = conf_map.get(sim_conf_str, ConformanceLevel.NOT_EVALUATED)
                at_sim_confidence = sim_confidence

                # Convert raw finding dicts to Finding objects
                for raw in sim_raw_findings:
                    sev_str = raw.get("severity", "medium")
                    sev_map = {v.value: v for v in Severity}
                    severity = sev_map.get(sev_str, Severity.MEDIUM)
                    at_sim_findings.append(Finding(
                        id=_make_finding_id(),
                        element=raw.get("element", ""),
                        issue=raw.get("issue", ""),
                        impact=raw.get("impact", ""),
                        recommendation=raw.get("recommendation", ""),
                        severity=severity,
                        source="at_sim",
                    ))

                # Filter off-scope
                pre_at_filter = len(at_sim_findings)
                at_sim_findings = self._filter_off_scope_findings(at_sim_findings)
                at_filtered = pre_at_filter - len(at_sim_findings)
                if at_filtered > 0 and len(at_sim_findings) == 0 and at_sim_conf in (
                    ConformanceLevel.DOES_NOT_SUPPORT,
                    ConformanceLevel.PARTIALLY_SUPPORTS,
                ):
                    at_sim_conf = ConformanceLevel.SUPPORTS

                logger.info(
                    "SC %s AT sim: %s (%.0f%%) — %d findings%s",
                    self.criterion_id, at_sim_conf.value, at_sim_confidence * 100,
                    len(at_sim_findings),
                    f" ({at_filtered} off-scope filtered)" if at_filtered else "",
                )
                all_findings.extend(at_sim_findings)
            except Exception as exc:
                logger.warning(
                    "AT simulation failed for SC %s: %s", self.criterion_id, exc
                )

        result.at_sim_conformance = at_sim_conf
        result.at_sim_confidence = at_sim_confidence
        result.at_sim_findings_count = len(at_sim_findings)

        # -- Enrich programmatic findings with page location context ----
        all_findings = self._enrich_finding_locations(
            all_findings, capture_data
        )

        # -- Drop findings contradicted by captured ground truth ---------
        all_findings = self._filter_findings_contradicted_by_capture(
            all_findings, capture_data
        )

        # -- Deduplicate findings ----------------------------------------
        all_findings = self._deduplicate_findings(all_findings)

        result.findings = all_findings

        # -- Judge AI (final arbiter) --------------------------------------
        # The judge ALWAYS runs when we have an AI client. It's not just
        # for tiebreaking — it produces ACR-quality output:
        #   - Rewrites findings in professional VPAT language
        #   - Adds precise page locations ("In the header, the search form...")
        #   - Deduplicates across sources
        #   - Rejects off-topic findings
        #   - Writes the VPAT summary text
        # Without the judge, findings are raw programmatic output — not
        # ready for a professional ACR.
        judge_ran = False
        if ai_client is not None and result.findings:
            try:
                import asyncio as _asyncio
                await _asyncio.sleep(2)

                from analysis.judge import judge_criterion

                source_verdicts = {
                    "Programmatic": {
                        "conformance": result.programmatic_conformance.value,
                        "confidence": result.programmatic_confidence,
                        "findings_count": result.programmatic_findings_count,
                    },
                    "Visual AI": {
                        "conformance": result.ai_conformance.value,
                        "confidence": result.ai_confidence,
                        "findings_count": result.ai_findings_count,
                    },
                    "Code AI": {
                        "conformance": result.code_ai_conformance.value,
                        "confidence": result.code_ai_confidence,
                        "findings_count": result.code_ai_findings_count,
                    },
                    "AT Simulation": {
                        "conformance": result.at_sim_conformance.value,
                        "confidence": result.at_sim_confidence,
                        "findings_count": result.at_sim_findings_count,
                    },
                }

                # Determine WCAG version from the result
                _wcag_ver = result.wcag_versions[0] if result.wcag_versions else "2.2"

                # Build DOM fact-check context for the judge
                _dom_context = self._build_dom_context(capture_data)

                # Give the judge the SAME images the visual AI received so it
                # can independently verify or reject visual_ai's findings
                # against actual pixels. Without images, the judge can only
                # text-match the prose visual_ai wrote, which means a
                # hallucinated "0px spacing" / "missing focus indicator" /
                # "image of text" claim cannot be visually corroborated and
                # the judge is forced to either trust prose or reject on
                # text DOM facts alone. Verified consequence on
                # 20260506_135324_f8765656 SC 1.1.1: visual_ai flagged radio-
                # button SVG dots as "informational background images"; the
                # text-only judge couldn't see they were 8x8 viewBox dots
                # and could not reject the claim. Sending pixels lets the
                # judge see what visual_ai saw.
                _judge_images = self._collect_judge_images(capture_data)

                judgment = await judge_criterion(
                    criterion_id=self.criterion_id,
                    criterion_name=self.criterion_name,
                    level=self.level,
                    normative_text=self.normative_text,
                    source_verdicts=source_verdicts,
                    all_findings=[f.to_dict() for f in result.findings],
                    wcag_version=_wcag_ver,
                    dom_context=_dom_context,
                    product_context=getattr(capture_data, "product_context", None),
                    code_findings=getattr(capture_data, "code_findings", None) or None,
                    code_findings_embeddings=(
                        getattr(capture_data, "code_findings_embeddings", None) or None
                    ),
                    images=_judge_images,
                )

                if judgment:
                    judge_ran = True

                    # Apply conformance decision
                    conf_map = {v.value: v for v in ConformanceLevel}
                    judge_conf = conf_map.get(
                        judgment.get("conformance_level", ""), None,
                    )
                    if judge_conf:
                        result.conformance_level = judge_conf
                    result.confidence = float(judgment.get("confidence", result.confidence))
                    result.confidence_reasoning = (
                        f"Judge AI: {judgment.get('reasoning', '')}"
                    )

                    # Use the Judge's rewritten findings as the FINAL output.
                    # These are already in VPAT language — no enrichment needed.
                    #
                    # Source-attribution integrity: the judge sometimes
                    # invents findings and labels them "programmatic" /
                    # "visual_ai" / etc. -- giving its own inferences the
                    # gravitas of measured evidence. Run the validator
                    # to demote unsupported source claims to
                    # "judge_inference". The judge keeps its autonomy to
                    # add findings the deterministic checks missed; it
                    # just has to be honest that those are inferences.
                    from functions.parser import validate_source_attribution
                    final_findings = judgment.get("final_findings", [])

                    # Log every rejection for the audit trail, regardless
                    # of whether final_findings is empty or not. The
                    # rejection record is informational; it does not
                    # drive the output set.
                    rejected = judgment.get("rejected_findings", []) or []
                    for rej in rejected:
                        if not isinstance(rej, dict):
                            continue
                        idx = rej.get("index")
                        if idx is None:
                            continue
                        logger.info(
                            "SC %s judge rejected [%s]: %s (-> %s)",
                            self.criterion_id, idx,
                            rej.get("reason", "") or "",
                            rej.get("correct_criterion", ""),
                        )

                    # CRITICAL: the judge's final_findings is the
                    # AUTHORITATIVE output set. If the judge returns
                    # final_findings=[], the criterion has no findings
                    # to report -- regardless of how many input findings
                    # came in. Replacing result.findings with the
                    # judge's output (empty or not) honours that
                    # contract.
                    #
                    # Past failure: when final_findings was [] AND
                    # rejected_findings was [], the old code's "filter
                    # by rejection" fallback would not enter its loop,
                    # silently leaving the original input findings on
                    # result.findings. The judge had said "Supports, no
                    # findings, no rejections" but result.findings still
                    # carried 2 original input findings. Verified failure
                    # on a university run 2026-05-09 SC 3.2.3.
                    #
                    # Now: trust the judge's empty list. If they said
                    # 0 findings, the report says 0 findings.
                    if final_findings:
                        _input_for_validation = [f.to_dict() for f in result.findings]
                        final_findings, flips = validate_source_attribution(
                            final_findings, _input_for_validation,
                        )
                        if flips:
                            logger.info(
                                "SC %s judge: source-attribution validator demoted "
                                "%d finding(s) to judge_inference (claims did not "
                                "trace back to any input source)",
                                self.criterion_id, flips,
                            )
                            # Expose to auditor in confidence_reasoning so the
                            # per-SC summary makes the inference-vs-measurement
                            # mix visible without burying it in finding source tags.
                            result.confidence_reasoning = (
                                (result.confidence_reasoning or "")
                                + f" | {flips} finding(s) recorded as judge_inference "
                                "(model added these beyond what input sources "
                                "produced — recommend human review)."
                            )
                        # Measurement-claim enforcement: demote findings that
                        # cite a contrast ratio no deterministic measurement
                        # backs. Prompt rules alone do not deter fabricated
                        # numbers on smaller models; this checks every cited
                        # ratio against the page's own captured measurements.
                        from functions.claim_validator import validate_measurement_claims
                        final_findings, claim_demotions = validate_measurement_claims(
                            final_findings, capture_data, self.measurement_sources,
                        )
                        if claim_demotions:
                            logger.info(
                                "SC %s judge: claim validator demoted %d finding(s) "
                                "citing unverified contrast measurements",
                                self.criterion_id, claim_demotions,
                            )
                            result.confidence_reasoning = (
                                (result.confidence_reasoning or "")
                                + f" | {claim_demotions} finding(s) cited a contrast "
                                "ratio not backed by deterministic measurement — "
                                "demoted to judge_inference and annotated."
                            )
                    sev_map = {v.value: v for v in Severity}
                    result.findings = []
                    for ff in final_findings:
                        if isinstance(ff, Finding):
                            # Already a Finding object (from judge parser)
                            result.findings.append(ff)
                        elif isinstance(ff, dict):
                            result.findings.append(Finding(
                                id=_make_finding_id(),
                                element=ff.get("element", ""),
                                css_selector=ff.get("css_selector", ""),
                                issue=ff.get("issue", ""),
                                impact=ff.get("impact", ""),
                                recommendation=ff.get("recommendation", ""),
                                severity=sev_map.get(ff.get("severity", "medium"), Severity.MEDIUM),
                                source=ff.get("source", "judge_inference"),
                                cited_measurements=ff.get("cited_measurements", []) or [],
                            ))

                    # Post-judge contradiction filter. The judge's
                    # tool-call accepts findings the upstream filter
                    # already saw, but it sometimes synthesises a NEW
                    # css_selector by combining tokens from the input
                    # — and that synthesis can introduce id literals
                    # that are not in the captured DOM (observed:
                    # `#carouselExampleControls` on SC 2.5.1 / 2.5.7,
                    # which appears as a string fragment in the DOM
                    # but has no element with id="carouselExampleControls").
                    # Re-running the same DOM-grounded filter on the
                    # post-judge findings closes that loophole.
                    pre_count = len(result.findings)
                    result.findings = self._filter_findings_contradicted_by_capture(
                        result.findings, capture_data,
                    )
                    if len(result.findings) < pre_count:
                        logger.info(
                            "SC %s post-judge filter dropped %d finding(s) "
                            "contradicted by captured data",
                            self.criterion_id, pre_count - len(result.findings),
                        )
                        # A conformance verdict worse than Supports must be
                        # backed by at least one finding. If the ground-truth
                        # contradiction filter removed every finding, the
                        # failing verdict was driven entirely by findings the
                        # captured data disproves — the only consistent
                        # verdict is Supports. Verified bug (loudoun.gov
                        # SC 1.4.5): 8 false image-of-text findings drove
                        # "Partially Supports"; all 8 are contradicted by
                        # background_images text_content.
                        if not result.findings and result.conformance_level in (
                            ConformanceLevel.PARTIALLY_SUPPORTS,
                            ConformanceLevel.DOES_NOT_SUPPORT,
                        ):
                            logger.info(
                                "SC %s: all findings dropped as contradicted by "
                                "captured data; verdict %s -> Supports.",
                                self.criterion_id, result.conformance_level.value,
                            )
                            result.confidence_reasoning = (
                                (result.confidence_reasoning or "")
                                + " | GROUND-TRUTH RECONCILIATION: every finding "
                                "contradicted captured data and was dropped; "
                                "verdict corrected to Supports (a failing verdict "
                                "cannot stand with 0 findings)."
                            )
                            result.conformance_level = ConformanceLevel.SUPPORTS

                    # Use the Judge's VPAT summary
                    vpat_summary = judgment.get("vpat_summary", "")
                    if vpat_summary:
                        result.summary = vpat_summary

                    logger.info(
                        "SC %s judge: %s (%.0f%%) — %d final findings, %d rejected. %s",
                        self.criterion_id,
                        result.conformance_level.value,
                        result.confidence * 100,
                        len(result.findings),
                        len(judgment.get("rejected_findings", [])),
                        judgment.get("reasoning", ""),
                    )

                    # Save judge response + DOM context for debugging
                    if capture_data.review_dir:
                        try:
                            import json as _json
                            from pathlib import Path as _Path
                            _judge_dir = _Path(capture_data.review_dir) / "tests" / self.criterion_id.replace(".", "_")
                            _judge_dir.mkdir(parents=True, exist_ok=True)
                            def _serialize_judge(obj):
                                if hasattr(obj, 'to_dict'):
                                    return obj.to_dict()
                                if hasattr(obj, 'value'):
                                    return obj.value
                                return str(obj)
                            (_judge_dir / "judge_response.json").write_text(
                                _json.dumps(judgment, indent=2, default=_serialize_judge),
                                encoding="utf-8",
                            )
                            # Save the DOM context the judge received
                            (_judge_dir / "judge_dom_context.txt").write_text(
                                _dom_context, encoding="utf-8",
                            )
                        except Exception:
                            logger.warning(
                                "SC %s: failed to save judge artifacts "
                                "(judge_response.json / judge_dom_context.txt). "
                                "The canonical request+response is still on "
                                "disk in llm_transcripts/; the per-SC sidecar "
                                "is missing.",
                                self.criterion_id, exc_info=True,
                            )

            except Exception as exc:
                logger.warning(
                    "Judge AI failed for SC %s: %s — falling back to algorithmic reconciliation",
                    self.criterion_id, exc,
                )

        # -- Sanity check: verdict must match findings (judge or not) ------
        # If verdict is Supports but there are high/medium findings, the
        # verdict is wrong. This catches both judge errors and reconciliation
        # errors. A professional ACR cannot say "Supports" while listing
        # real issues -- and conversely cannot say "Partially Supports" /
        # "Does Not Support" while listing zero findings (an empty
        # findings list with a failing verdict has nothing to remediate
        # and cannot be acted on by a developer).
        high_count = sum(1 for f in result.findings if f.severity == Severity.HIGH)
        med_count = sum(1 for f in result.findings if f.severity == Severity.MEDIUM)
        info_low_count = sum(
            1 for f in result.findings
            if f.severity in (Severity.LOW, Severity.INFO)
        )
        total_count = len(result.findings)
        if result.conformance_level == ConformanceLevel.SUPPORTS and high_count:
            logger.warning(
                "SC %s SANITY: Supports with %d HIGH findings → Does Not Support",
                self.criterion_id, high_count,
            )
            result.conformance_level = ConformanceLevel.DOES_NOT_SUPPORT
        elif result.conformance_level == ConformanceLevel.SUPPORTS and med_count:
            logger.warning(
                "SC %s SANITY: Supports with %d MEDIUM findings → Partially Supports",
                self.criterion_id, med_count,
            )
            result.conformance_level = ConformanceLevel.PARTIALLY_SUPPORTS
        elif (
            result.conformance_level in (
                ConformanceLevel.DOES_NOT_SUPPORT,
                ConformanceLevel.PARTIALLY_SUPPORTS,
            )
            and total_count == 0
        ):
            # Inverse: failing verdict with no findings is unactionable.
            # Either the judge dropped findings without recomputing the
            # verdict (verified failure on a university run #4 SC 2.5.1: verdict
            # stayed Partially Supports after final_findings emptied),
            # or all findings got post-judge-filtered by the DOM
            # contradiction check. Either way, there's nothing to
            # remediate -- downgrade verdict to Supports and add an
            # auditor note so this isn't silent.
            logger.warning(
                "SC %s SANITY: %s with 0 findings → Supports (no findings = "
                "nothing to remediate; downgraded automatically to keep the "
                "verdict consistent with the evidence list)",
                self.criterion_id,
                result.conformance_level.value,
            )
            old_verdict = result.conformance_level.value
            result.conformance_level = ConformanceLevel.SUPPORTS
            result.confidence_reasoning = (
                (result.confidence_reasoning or "")
                + f" | SANITY: original verdict was '{old_verdict}' but "
                f"the findings list was empty; downgraded to Supports "
                f"because there is no concrete issue to remediate. If "
                f"the findings were dropped in error, recheck the "
                f"upstream extractors."
            )
            # Mark for auditor attention -- empty-findings downgrade
            # often signals an extractor or post-judge-filter issue
            # worth investigating, even when the verdict reads clean.
            result.needs_review = True
            current_reasons = list(result.needs_review_reasons or [])
            if not any("auto-downgraded" in r for r in current_reasons):
                current_reasons.append(
                    f"Verdict auto-downgraded from {old_verdict} to "
                    f"Supports because findings list was empty"
                )
            result.needs_review_reasons = current_reasons

        # -- Algorithmic reconciliation (fallback if judge didn't run) ------
        if not judge_ran:
            result = self._reconcile_verdicts(result)

            # Enrichment only runs when judge didn't — judge already writes
            # VPAT-quality findings so enrichment is redundant.
            has_real_findings = any(
                f.severity in (Severity.HIGH, Severity.MEDIUM)
                for f in result.findings
            )
            if (
                ai_client is not None
                and has_real_findings
                and result.conformance_level not in (
                    ConformanceLevel.SUPPORTS,
                    ConformanceLevel.NOT_APPLICABLE,
                )
            ):
                import asyncio as _asyncio
                await _asyncio.sleep(2)
                try:
                    result.findings = await self._enrich_findings_with_ai(
                        result.findings, capture_data, ai_client,
                    )
                except Exception as exc:
                    logger.warning(
                        "AI finding enrichment failed for SC %s: %s",
                        self.criterion_id, exc,
                    )

        # -- TT results ----------------------------------------------------
        result.tt_results = self._generate_tt_results(
            result.findings, capture_data
        )

        return result

    # ------------------------------------------------------------------
    # Criterion-specific video for AI (override for motion/media criteria)
    # ------------------------------------------------------------------

    # Criteria that benefit from the keyboard walkthrough video
    _KEYBOARD_CRITERIA = {
        "2.1.1", "2.1.2", "2.1.3", "2.1.4",
        "2.4.3", "2.4.7", "2.4.11",
        "3.2.1", "3.2.2",
    }
    # Criteria that benefit from the page observation video
    _OBSERVATION_CRITERIA = {
        "1.4.2", "2.2.1", "2.2.2", "2.3.1", "2.3.2", "2.3.3",
    }

    # Criteria that benefit from specific video segment types
    _SEGMENT_CRITERIA: dict[str, set[str]] = {
        "FORM_INTERACTION": {"1.3.5", "3.3.1", "3.3.2", "3.3.3", "3.3.4", "3.3.7", "3.3.8"},
        "MENU_NAVIGATION": {"2.1.1", "2.4.3", "2.4.7", "2.4.11"},
        "MODAL_INTERACTION": {"2.1.2", "2.4.3", "2.4.7"},
        "MEDIA_PLAYBACK": {"1.2.1", "1.2.2", "1.2.3", "1.2.4", "1.2.5", "1.2.6", "1.2.7", "1.2.8"},
        "ACCORDION_INTERACTION": {"4.1.2", "2.1.1"},
        "CAROUSEL_INTERACTION": {"2.2.2", "4.1.2", "2.1.1"},
    }

    # Criteria where the AI needs to HEAR the video audio, not just see it.
    # These get routed to a model with native audio (Gemma 4 E4B).
    needs_audio: bool = False

    def get_video_path(self, capture_data: CaptureData) -> str | None:
        """Return a single video for backward compatibility."""
        paths = self.get_video_paths(capture_data)
        return paths[0] if paths else None

    def get_video_paths(self, capture_data: CaptureData) -> list[str]:
        """Return ALL relevant video paths for this SC.

        Includes the keyboard walkthrough, observation video, AND
        any Phase 3 video segments that match this criterion.
        Videos are sent to the vision AI one at a time.
        """
        paths = []
        cid = self.criterion_id

        # Keyboard walkthrough for keyboard criteria
        if cid in self._KEYBOARD_CRITERIA:
            if capture_data.keyboard_walkthrough_video:
                paths.append(capture_data.keyboard_walkthrough_video)

        # Observation video for motion/timing criteria
        if cid in self._OBSERVATION_CRITERIA:
            if capture_data.observation_video_path:
                paths.append(capture_data.observation_video_path)

        # Phase 3 video segments matching this SC
        for seg in getattr(capture_data, "video_segments", []) or []:
            if not isinstance(seg, dict):
                continue
            seg_type = seg.get("type", "")
            video_path = seg.get("video_path", "")
            if not video_path or not seg.get("completed"):
                continue
            # Check if this segment type is relevant to this SC
            relevant_scs = self._SEGMENT_CRITERIA.get(seg_type, set())
            if cid in relevant_scs:
                paths.append(video_path)

        return paths

    def _get_video_descriptions(self, capture_data: CaptureData) -> str:
        """Get pre-computed video descriptions relevant to this criterion.

        Returns a combined text string of all video observations that
        apply to this SC, or empty string if none available.
        """
        descriptions = getattr(capture_data, "video_descriptions", {})
        if not descriptions:
            return ""

        from capture.video_describer import VIDEO_QUESTIONS

        parts: list[str] = []
        cid = self.criterion_id

        for video_key, config in VIDEO_QUESTIONS.items():
            if cid not in config.get("serves_criteria", []):
                continue
            # Check for exact key or segment-prefixed key
            desc = descriptions.get(video_key)
            if not desc:
                # Check for segment keys like "segment_FORM_INTERACTION_xxx.webm"
                for dk, dv in descriptions.items():
                    if dk.startswith(f"segment_{video_key}"):
                        desc = dv
                        break
            if desc:
                label = config.get("description", video_key)
                parts.append(f"[{label.upper()}]\n{desc}")

        return "\n\n".join(parts)

    # ------------------------------------------------------------------
    # Capture completeness check
    # ------------------------------------------------------------------

    def _check_capture_completeness(self, capture_data: CaptureData) -> str | None:
        """Return None if required captures ran, or a description of failures."""
        required = self._REQUIRED_CAPTURES.get(self.criterion_id, [])
        if not required:
            return None
        completions = getattr(capture_data, "capture_completions", {})
        if not completions:
            return None
        failed = [
            f"{name} ({completions[name]})"
            for name in required
            if completions.get(name) and completions[name] != "ok"
        ]
        if failed:
            msg = f"Required capture(s) failed: {', '.join(failed)}"
            logger.warning("SC %s: %s", self.criterion_id, msg)
            return msg
        return None

    # ------------------------------------------------------------------
    # Programmatic fast path
    # ------------------------------------------------------------------

    async def _execute_programmatic_fast_path(
        self,
        result: TestResult,
        all_findings: list[Finding],
        prog_conf: ConformanceLevel,
        prog_confidence: float,
        capture_data: CaptureData,
        ai_client: Any | None,
    ) -> TestResult:
        """Fast path for PROGRAMMATIC_DEFINITIVE criteria.

        Accepts programmatic + axe-core as definitive, skips AI sources,
        sends to judge for VPAT language synthesis only.
        """
        logger.info("SC %s: fast path — %d findings, %s (%.0f%%)",
                     self.criterion_id, len(all_findings), prog_conf.value, prog_confidence * 100)

        # Mark AI sources as not evaluated (they weren't run)
        for attr in ("ai", "code_ai", "at_sim"):
            setattr(result, f"{attr}_conformance", ConformanceLevel.NOT_EVALUATED)
            setattr(result, f"{attr}_confidence", 0.0)
            setattr(result, f"{attr}_findings_count", 0)

        # Enrich + deduplicate
        all_findings = self._enrich_finding_locations(all_findings, capture_data)
        all_findings = self._deduplicate_findings(all_findings)
        result.findings = all_findings
        result.conformance_level = prog_conf
        result.confidence = prog_confidence

        # Judge: VPAT synthesis only
        if ai_client is not None and result.findings:
            try:
                import asyncio as _asyncio
                await _asyncio.sleep(1)
                from analysis.judge import judge_criterion

                judgment = await judge_criterion(
                    criterion_id=self.criterion_id,
                    criterion_name=self.criterion_name,
                    level=self.level,
                    normative_text=self.normative_text,
                    source_verdicts={"Programmatic": {
                        "conformance": prog_conf.value,
                        "confidence": prog_confidence,
                        "findings_count": len(all_findings),
                    }},
                    all_findings=[f.to_dict() for f in result.findings],
                    wcag_version=result.wcag_versions[0] if result.wcag_versions else "2.2",
                    dom_context=self._build_dom_context(capture_data),
                    product_context=getattr(capture_data, "product_context", None),
                    programmatic_only=True,
                )

                if judgment:
                    # Use judge's VPAT-rewritten findings.
                    #
                    # Source-attribution integrity (fast path): same rule
                    # as the slow path. The judge can rewrite findings
                    # for VPAT prose but cannot upgrade its own
                    # inferences to "programmatic". Where the deterministic
                    # check produced 0 findings (e.g. SC 2.5.8 spacing
                    # exception), the judge MAY still emit findings -- it
                    # just has to label them "judge_inference" so the
                    # auditor can see they are not measured. The
                    # validator below downgrades any "programmatic" claim
                    # whose selector / element / issue does not trace
                    # back to an input finding from the deterministic
                    # source.
                    from functions.parser import validate_source_attribution
                    final = judgment.get("final_findings", [])
                    if final:
                        _input_for_validation = [f.to_dict() for f in result.findings]
                        final, source_flips = validate_source_attribution(
                            final, _input_for_validation,
                        )
                        if source_flips:
                            logger.info(
                                "SC %s fast-path judge: source-attribution validator "
                                "demoted %d finding(s) to judge_inference (no "
                                "matching deterministic input)",
                                self.criterion_id, source_flips,
                            )
                        # Measurement-claim enforcement (fast path): same
                        # check as the slow path -- demote any finding citing
                        # a contrast ratio no deterministic measurement backs.
                        from functions.claim_validator import validate_measurement_claims
                        final, claim_demotions = validate_measurement_claims(
                            final, capture_data, self.measurement_sources,
                        )
                        if claim_demotions:
                            logger.info(
                                "SC %s fast-path judge: claim validator demoted %d "
                                "finding(s) citing unverified measurements",
                                self.criterion_id, claim_demotions,
                            )
                        # Both validators demote to judge_inference; the
                        # drop logic below treats the combined total.
                        total_demotions = source_flips + claim_demotions
                        if total_demotions:
                            # A judge_inference tag means the validator could
                            # not trace the finding's source tag back to a
                            # deterministic input finding (usually a selector
                            # mismatch) OR the finding cited a measurement the
                            # captured data does not back. By itself it does
                            # NOT mean the finding is false.
                            #
                            # Drop judge_inference-only findings ONLY when the
                            # deterministic check produced ZERO findings of its
                            # own — i.e. it measured the SC clean. On a
                            # PROGRAMMATIC_DEFINITIVE SC a clean deterministic
                            # measurement is authoritative, and an unmeasured
                            # judge inference cannot override it. This kills the
                            # SC 2.5.8 "0px spacing" hallucination (verified on a university
                            # run 20260502_162952_de12e630: 108 targets pass,
                            # 0 fail, judge invents failures).
                            #
                            # When the deterministic check DID produce findings,
                            # a judge_inference finding is a restatement or
                            # refinement of a real measured failure the
                            # validator merely could not selector-match — it
                            # MUST be kept. Verified bug (loudoun.gov run
                            # 20260518): the genuine SC 1.1.1 empty-alt+title
                            # findings and 5 SC 1.4.3 contrast findings were
                            # demoted for generic selectors, then wrongly
                            # dropped here, leaving only hedged speculation.
                            #
                            # The slow path never drops judge_inference findings
                            # at all — non-definitive SCs benefit from the judge
                            # spotting things the deterministic check cannot see.
                            deterministic_clean = not all_findings
                            dropped_unsupported = 0
                            if deterministic_clean:
                                kept = []
                                for ff in final:
                                    ff_src = ff.get("source", "") if isinstance(ff, dict) else getattr(ff, "source", "")
                                    src_tags = [t.strip() for t in str(ff_src).replace(",", " ").split() if t.strip()]
                                    if src_tags == ["judge_inference"] or src_tags == ["judge"]:
                                        dropped_unsupported += 1
                                        continue
                                    kept.append(ff)
                                if dropped_unsupported:
                                    final = kept
                            if dropped_unsupported:
                                logger.info(
                                    "SC %s fast-path: dropped %d judge_inference-only "
                                    "finding(s) — deterministic check measured the "
                                    "page clean (0 findings); an unmeasured inference "
                                    "cannot override a clean definitive measurement.",
                                    self.criterion_id, dropped_unsupported,
                                )
                                result.confidence_reasoning = (
                                    (result.confidence_reasoning or "")
                                    + f" | FAST-PATH ENFORCEMENT: dropped "
                                    f"{dropped_unsupported} judge inference(s) on a "
                                    f"PROGRAMMATIC_DEFINITIVE SC whose deterministic "
                                    f"check measured the page clean (0 findings)."
                                )
                            elif total_demotions:
                                logger.info(
                                    "SC %s fast-path: %d finding(s) demoted to "
                                    "judge_inference but kept — deterministic check "
                                    "produced %d finding(s), so these restate or "
                                    "refine measured failures.",
                                    self.criterion_id, total_demotions,
                                    len(all_findings),
                                )
                                result.confidence_reasoning = (
                                    (result.confidence_reasoning or "")
                                    + f" | FAST-PATH NOTE: {total_demotions} finding(s) "
                                    "recorded as judge_inference (source tag could "
                                    "not be traced to a deterministic input finding); "
                                    "kept because the deterministic check produced "
                                    "findings of its own — these restate or refine "
                                    "measured failures."
                                )
                        # If verdict was driven by now-dropped findings,
                        # restore the deterministic conformance level AND
                        # the original deterministic findings.
                        #
                        # Past failure (verified on fairfaxva.gov run
                        # 20260514_205147_cb3b646c SC 4.1.3): the judge
                        # consolidated 3 programmatic findings into 1
                        # judge-emitted finding, the validator demoted
                        # that 1 to judge_inference and dropped it. The
                        # conformance_level was correctly restored to
                        # prog_conf=Partially Supports but result.findings
                        # ended up empty -- producing a "Partially
                        # Supports with 0 findings" inconsistency where
                        # the auditor sees a non-Supports verdict with
                        # nothing to back it.
                        #
                        # The original input findings (all_findings) are
                        # the deterministic measurements the verdict is
                        # actually based on, so when the judge's edit
                        # collapses to empty, we restore them with their
                        # original sources intact.
                        verdict_findings_restored = False
                        if not final:
                            result.conformance_level = prog_conf
                            result.confidence = prog_confidence
                            if all_findings:
                                logger.info(
                                    "SC %s fast-path: judge findings all dropped; "
                                    "restoring %d original deterministic finding(s) "
                                    "so verdict (%s) has visible evidence.",
                                    self.criterion_id, len(all_findings),
                                    prog_conf.value,
                                )
                                result.confidence_reasoning = (
                                    (result.confidence_reasoning or "")
                                    + f" | RECONCILED: judge collapsed to 0 "
                                    f"findings; restored {len(all_findings)} "
                                    f"original deterministic finding(s) so "
                                    f"{prog_conf.value} verdict has visible "
                                    f"evidence."
                                )
                                verdict_findings_restored = True
                        sev_map = {v.value: v for v in Severity}
                        if verdict_findings_restored:
                            # Keep the original deterministic findings on
                            # result.findings; do not reset to the empty
                            # `final` list below.
                            pass
                        else:
                            result.findings = []
                        for ff in final:
                            if isinstance(ff, Finding):
                                result.findings.append(ff)
                            elif isinstance(ff, dict):
                                result.findings.append(Finding(
                                    id=_make_finding_id(),
                                    element=ff.get("element", ""),
                                    issue=ff.get("issue", ""),
                                    impact=ff.get("impact", ""),
                                    recommendation=ff.get("recommendation", ""),
                                    severity=sev_map.get(ff.get("severity", "medium"), Severity.MEDIUM),
                                    source=ff.get("source", "judge_inference"),
                                    css_selector=ff.get("css_selector", ""),
                                    cited_measurements=ff.get("cited_measurements", []) or [],
                                ))
                            elif isinstance(ff, str) and ff.startswith("Finding("):
                                # Stringified Finding object — skip, log warning
                                logger.warning("SC %s: judge returned stringified Finding, skipping", self.criterion_id)

                    # Post-judge DOM-grounded filter (matches the slow-
                    # path equivalent in run() above). Drops any final
                    # finding whose selector cites an id that is not
                    # present in captured_data.html.
                    pre_count = len(result.findings)
                    result.findings = self._filter_findings_contradicted_by_capture(
                        result.findings, capture_data,
                    )
                    if len(result.findings) < pre_count:
                        logger.info(
                            "SC %s fast-path post-judge filter dropped %d finding(s)",
                            self.criterion_id, pre_count - len(result.findings),
                        )
                        # A failing verdict cannot stand with 0 findings.
                        if not result.findings and result.conformance_level in (
                            ConformanceLevel.PARTIALLY_SUPPORTS,
                            ConformanceLevel.DOES_NOT_SUPPORT,
                        ):
                            logger.info(
                                "SC %s fast-path: all findings dropped as "
                                "contradicted by captured data; verdict %s -> "
                                "Supports.",
                                self.criterion_id, result.conformance_level.value,
                            )
                            result.confidence_reasoning = (
                                (result.confidence_reasoning or "")
                                + " | GROUND-TRUTH RECONCILIATION: every finding "
                                "contradicted captured data and was dropped; "
                                "verdict corrected to Supports."
                            )
                            result.conformance_level = ConformanceLevel.SUPPORTS

                    if judgment.get("vpat_summary"):
                        result.summary = judgment["vpat_summary"]
                    # Preserve any FAST-PATH ENFORCEMENT / FAST-PATH WARNING
                    # suffix added above by the source-attribution validator
                    # path. Earlier code unconditionally overwrote this field
                    # which silently erased the audit trail of why a judge
                    # finding was dropped — observed during SC 2.4.4
                    # diagnosis on example.com. Prepend the verdict-source
                    # label and keep the prior suffix.
                    _existing_reasoning = result.confidence_reasoning or ""
                    _judge_reason = judgment.get("reasoning", "")
                    result.confidence_reasoning = (
                        f"Programmatic definitive: {_judge_reason}"
                        + _existing_reasoning
                    )

                    logger.info("SC %s: fast path judge → %d VPAT findings",
                                self.criterion_id, len(result.findings))

                    # Save judge response + DOM context for human review.
                    # The full request payload (system prompt + user prompt
                    # + tool schema) AND the raw response are also saved
                    # in llm_transcripts/NNNNN_report_judgment.json by
                    # LLMClient — that is the canonical record. These
                    # per-SC files are the convenience artefacts an
                    # auditor opens first when investigating a verdict.
                    if capture_data.review_dir:
                        try:
                            import json as _json
                            from pathlib import Path as _Path
                            jdir = _Path(capture_data.review_dir) / "tests" / self.criterion_id.replace(".", "_")
                            jdir.mkdir(parents=True, exist_ok=True)
                            (jdir / "judge_response.json").write_text(
                                _json.dumps(judgment, indent=2, default=lambda o: o.value if hasattr(o, 'value') else str(o)),
                                encoding="utf-8",
                            )
                            # Save the DOM context the judge received —
                            # matches the slow path's per-SC layout so
                            # auditors don't have to know which path ran.
                            _dom_ctx = self._build_dom_context(capture_data)
                            (jdir / "judge_dom_context.txt").write_text(
                                _dom_ctx, encoding="utf-8",
                            )
                        except Exception:
                            logger.warning(
                                "SC %s fast-path: failed to save judge "
                                "artifacts (judge_response.json / "
                                "judge_dom_context.txt). The canonical "
                                "request+response is still on disk in "
                                "llm_transcripts/; the per-SC sidecar "
                                "is missing.",
                                self.criterion_id, exc_info=True,
                            )

            except Exception as exc:
                logger.warning("SC %s: fast path judge failed: %s", self.criterion_id, exc)

        if not result.summary:
            self._generate_summary(result)

        result.tt_results = self._generate_tt_results(result.findings, capture_data)
        logger.info("SC %s: fast path COMPLETE — %s (%.0f%%), %d findings",
                     self.criterion_id, result.conformance_level.value,
                     result.confidence * 100, len(result.findings))
        return result

    # ------------------------------------------------------------------
    # Dynamic confidence + needs_review
    # ------------------------------------------------------------------

    def _adjust_confidence_and_flag_review(
        self, result: TestResult, capture_data: CaptureData,
    ) -> None:
        """Adjust confidence based on data quality and flag for human review."""
        if result.conformance_level in (ConformanceLevel.NOT_APPLICABLE, ConformanceLevel.NOT_EVALUATED):
            return

        reasons: list[str] = []
        conf = result.confidence

        # Capture gap penalty
        if self._check_capture_completeness(capture_data):
            conf *= 0.6
            reasons.append(f"Incomplete capture: {self._check_capture_completeness(capture_data)}")

        # Source disagreement penalty
        prog_c = result.programmatic_conformance
        ai_c = result.ai_conformance
        if (prog_c not in (ConformanceLevel.NOT_EVALUATED, ConformanceLevel.NOT_APPLICABLE)
            and ai_c not in (ConformanceLevel.NOT_EVALUATED, ConformanceLevel.NOT_APPLICABLE)
            and prog_c != ai_c):
            conf *= 0.85
            reasons.append(f"Sources disagree: programmatic={prog_c.value}, AI={ai_c.value}")

        # Low source count penalty
        evaluated = sum(1 for c in [
            result.programmatic_conformance, result.ai_conformance,
            result.code_ai_conformance, result.at_sim_conformance,
        ] if c not in (ConformanceLevel.NOT_EVALUATED, ConformanceLevel.NOT_APPLICABLE))
        if evaluated <= 1 and self.criterion_id not in self.PROGRAMMATIC_DEFINITIVE:
            conf *= 0.9
            reasons.append(f"Only {evaluated} source(s) had a verdict")

        # TT sub-test coverage penalty. When a check declares
        # Trusted-Tester sub-tests (CAV's 1.A-1.D, etc.) but most of
        # them returned NOT_TESTED, the verdict rests on partial
        # evidence. Cap the confidence proportional to coverage so the
        # auditor sees the gap rather than a high-confidence verdict
        # backed by a single sub-test. Verified on fairfaxva.gov run
        # 20260514_205147_cb3b646c SC CAV: 1 of 4 sub-tests tested, 3
        # NOT_TESTED, consolidator published confidence=0.8 -- too high.
        try:
            from models import TTResult as _TTResult
            # A sub-test counts as "covered" when it produced a real
            # outcome: PASS, FAIL, or DNA (does-not-apply -- a definitive
            # determination). NOT_TESTED is the only non-covered state.
            # (TTResult has exactly these four members; do not reference
            # a NOT_APPLICABLE member -- it does not exist.)
            covered_states = {_TTResult.PASS, _TTResult.FAIL, _TTResult.DNA}
            total_tt = len(result.tt_results or [])
            tested = sum(
                1 for t in (result.tt_results or [])
                if getattr(t, "result", None) in covered_states
            )
            if total_tt > 0 and tested < total_tt:
                coverage = tested / total_tt
                # Cap confidence at (coverage + 0.25), so 1/4 coverage
                # caps at 0.5, 2/4 caps at 0.75, 3/4 caps at 1.0.
                cap = min(1.0, coverage + 0.25)
                if conf > cap:
                    not_tested = total_tt - tested
                    reasons.append(
                        f"TT sub-test coverage: {tested}/{total_tt} tested "
                        f"({not_tested} NOT TESTED); confidence capped at "
                        f"{cap:.0%}"
                    )
                    conf = cap
        except Exception:
            logger.debug(
                "SC %s: TT coverage cap computation failed",
                self.criterion_id, exc_info=True,
            )

        result.confidence = round(max(0.0, min(1.0, conf)), 3)

        # Flag: low confidence
        if result.confidence < 0.65:
            reasons.append(f"Low confidence ({result.confidence:.0%})")

        # Flag: Supports with 0 findings but relevant content exists
        if result.conformance_level == ConformanceLevel.SUPPORTS and not result.findings:
            for field_name in self._CONTENT_INDICATORS.get(self.criterion_id, []):
                value = getattr(capture_data, field_name, None)
                if value and isinstance(value, (list, dict)) and len(value) > 0:
                    reasons.append("Supports with 0 findings but relevant content exists")
                    break

        # Flag: all AI sources NOT_EVALUATED
        if (self.criterion_id not in self.PROGRAMMATIC_DEFINITIVE
            and result.ai_conformance == ConformanceLevel.NOT_EVALUATED
            and result.code_ai_conformance == ConformanceLevel.NOT_EVALUATED
            and result.at_sim_conformance == ConformanceLevel.NOT_EVALUATED):
            reasons.append("All AI sources returned Not Evaluated")

        if reasons:
            result.needs_review = True
            result.needs_review_reasons = reasons
            logger.info("SC %s: FLAGGED FOR REVIEW — %s", self.criterion_id, "; ".join(reasons))

    # ------------------------------------------------------------------
    # Axe-core findings extraction
    # ------------------------------------------------------------------

    def _build_capture_gaps_block(self, capture_data: CaptureData) -> str:
        """Render a CAPTURE GAPS block for the AI prompt.

        Lists every piece of content the automated tool could NOT see
        (cross-origin iframes, failed interactive tests, overlay widgets
        overriding native behaviour). The AI is explicitly told not to
        invent verdicts for content in this block; if evaluation depends
        on captured content of a blocked iframe, it should set
        conformance_level to Not Evaluated with an insufficient_evidence
        reason rather than guessing.
        """
        lines: list[str] = []
        completions = getattr(capture_data, "capture_completions", {}) or {}
        blocked_iframes = completions.get("cross_origin_iframes_blocked") or []
        if blocked_iframes:
            lines.append(
                f"  Cross-origin iframes ({len(blocked_iframes)}) that the "
                f"tool could not enter (browser same-origin restriction):"
            )
            for url in blocked_iframes:
                lines.append(f"    - {url}")

        failed_tests = []
        for name, status in completions.items():
            if name == "cross_origin_iframes_blocked":
                continue
            if isinstance(status, str) and status not in ("ok", "completed", "success"):
                failed_tests.append(f"{name}={status}")
        if failed_tests:
            lines.append(
                "  Interactive tests that did not complete successfully:"
            )
            for entry in failed_tests:
                lines.append(f"    - {entry}")

        overlays = getattr(capture_data, "overlay_widgets", None) or []
        if overlays:
            vendors = sorted({o.get("vendor", "?") for o in overlays})
            lines.append(
                f"  Accessibility overlay widgets detected ({', '.join(vendors)}). "
                f"These inject shadow-DOM and can intercept focus/ARIA. "
                f"Automated results reflect native-page PLUS overlay "
                f"behaviour combined -- native conformance may differ."
            )

        if not lines:
            return ""

        return (
            "[CAPTURE GAPS -- automated tool limits]\n"
            "The following content was not fully captured by the automated "
            "test. Do NOT invent verdicts about it. If your evaluation of "
            "this criterion depends on content listed here, set "
            "conformance_level to 'Not Evaluated' and populate "
            "insufficient_evidence_reason with the specific missing data.\n"
            + "\n".join(lines)
        )

    def _emit_untraversable_iframe_findings(
        self, capture_data: CaptureData,
    ) -> list[Finding]:
        """Surface cross-origin iframes the tool could not enter.

        Returns one info-severity finding per blocked iframe so every SC
        tells the auditor: this iframe was not traversable, manual review
        required. The entries appear in the VPAT with severity=info so
        they don't flip the verdict, but they are visible in the report.
        Only SCs likely affected by iframe content emit these; for other
        SCs the notice is captured in the DOM context instead.
        """
        completions = getattr(capture_data, "capture_completions", {}) or {}
        blocked = completions.get("cross_origin_iframes_blocked") or []
        if not blocked:
            return []

        # Only emit the finding on SCs that genuinely evaluate iframe content.
        # Other SCs still see the blocked list through _build_dom_context.
        iframe_relevant = {
            "1.1.1", "1.2.1", "1.2.2", "1.2.3", "1.2.4", "1.2.5",
            "1.2.6", "1.2.7", "1.2.8", "1.2.9",
            "1.3.1", "1.4.1", "1.4.3", "1.4.5", "1.4.11",
            "2.1.1", "2.1.2", "2.4.1", "2.4.4",
            "3.3.1", "3.3.2",
            "4.1.2", "4.1.3",
        }
        if self.criterion_id not in iframe_relevant:
            return []

        findings: list[Finding] = []
        for url in blocked:
            findings.append(Finding(
                id=_make_finding_id(),
                element=f"iframe src={url}",
                issue=(
                    f"Cross-origin iframe was not traversable by the automated "
                    f"tool: {url}. Browser same-origin policy prevents the "
                    f"tool from inspecting its contents, so SC {self.criterion_id} "
                    f"cannot be fully verified for material inside this iframe."
                ),
                impact=(
                    "The accessibility of content inside this iframe is unknown. "
                    "Users of assistive technology interact with the iframe's "
                    "contents directly, so any WCAG failures inside it affect "
                    "them even though the tool could not see them."
                ),
                recommendation=(
                    "Test this iframe's contents independently. For YouTube, "
                    "Kaltura, Panopto and similar media embeds, verify captions, "
                    "transcripts, keyboard access to player controls, and focus "
                    "indicators. For Qualtrics and other form embeds, verify "
                    "labels, error messages, and required-field indication."
                ),
                severity=Severity.INFO,
                source="programmatic",
                css_selector=f"iframe[src*='{url}']",
            ))
        return findings

    def _emit_overlay_widget_findings(
        self, capture_data: CaptureData,
    ) -> list[Finding]:
        """Surface accessibility overlay widgets as a universal info finding.

        Overlays (UserWay, AccessiBe, EqualWeb, AudioEye, Recite Me,
        UsableNet, Max Access, ...) inject shadow-DOM widgets that can
        override focus management and ARIA exposure. Every SC emits an
        info finding so the auditor knows the native page behaviour may
        be altered by the overlay and any SUPPORTS verdict should be
        double-checked with the overlay disabled.
        """
        overlays = getattr(capture_data, "overlay_widgets", None) or []
        if not overlays:
            return []

        vendors = sorted({o.get("vendor", "unknown") for o in overlays})
        vendor_list = ", ".join(vendors)
        sources = [o.get("src", "") for o in overlays if o.get("src")]
        src_summary = "; ".join(sources[:4])
        if len(sources) > 4:
            src_summary += f"; +{len(sources) - 4} more"

        return [Finding(
            id=_make_finding_id(),
            element="<page>",
            issue=(
                f"Accessibility overlay widget detected: {vendor_list}. "
                f"Overlays commonly inject shadow-DOM widgets that intercept "
                f"keyboard focus, override ARIA attributes, and introduce "
                f"their own WCAG failures. The automated test results for "
                f"this page reflect the combined behaviour of the native "
                f"content AND the overlay; native-page conformance may "
                f"differ once the overlay is disabled."
            ),
            impact=(
                "Users who rely on assistive technology may experience the "
                "overlay's behaviour instead of the page's native behaviour. "
                "Any SUPPORTS verdict for this page should be re-verified "
                "with the overlay disabled to confirm that conformance is "
                "provided by the native implementation, not a third-party "
                "script that the site owner does not control."
            ),
            recommendation=(
                "Re-test every Partially Supports / Does Not Support finding "
                "with the overlay disabled to confirm root cause. Plan to "
                "remediate native-page accessibility regardless of the "
                "overlay, because the overlay cannot fix semantic HTML, "
                "content structure, or authored alt text."
            ),
            severity=Severity.INFO,
            source="programmatic",
            evidence=src_summary,
        )]

    def _extract_axe_findings(self, capture_data: CaptureData) -> list[Finding]:
        """Delegates to functions.axe_extract.extract_axe_findings."""
        from functions.axe_extract import extract_axe_findings
        return extract_axe_findings(capture_data, self.criterion_id)

    def _extract_htmlcs_findings(self, capture_data: CaptureData) -> list[Finding]:
        """Delegates to functions.htmlcs_extract.extract_htmlcs_findings."""
        from functions.htmlcs_extract import extract_htmlcs_findings
        return extract_htmlcs_findings(capture_data, self.criterion_id)

    def _extract_ibm_eac_findings(self, capture_data: CaptureData) -> list[Finding]:
        """Delegates to functions.ibm_eac_extract.extract_ibm_eac_findings."""
        from functions.ibm_eac_extract import extract_ibm_eac_findings
        return extract_ibm_eac_findings(capture_data, self.criterion_id)

    def _extract_andi_contrast_findings(self, capture_data: CaptureData) -> list[Finding]:
        """Delegates to functions.andi_extract.extract_andi_contrast_findings."""
        from functions.andi_extract import extract_andi_contrast_findings
        return extract_andi_contrast_findings(capture_data, self.criterion_id)

    def _extract_andi_lang_findings(self, capture_data: CaptureData) -> list[Finding]:
        """Delegates to functions.andi_extract.extract_andi_lang_findings."""
        from functions.andi_extract import extract_andi_lang_findings
        return extract_andi_lang_findings(capture_data, self.criterion_id)

    def _extract_andi_interactive_findings(self, capture_data: CaptureData) -> list[Finding]:
        """Delegates to functions.andi_extract.extract_andi_interactive_findings."""
        from functions.andi_extract import extract_andi_interactive_findings
        return extract_andi_interactive_findings(capture_data, self.criterion_id)

    def _extract_andi_tables_findings(self, capture_data: CaptureData) -> list[Finding]:
        """Delegates to functions.andi_extract.extract_andi_tables_findings."""
        from functions.andi_extract import extract_andi_tables_findings
        return extract_andi_tables_findings(capture_data, self.criterion_id)

    def _extract_andi_graphics_findings(self, capture_data: CaptureData) -> list[Finding]:
        """Delegates to functions.andi_extract.extract_andi_graphics_findings."""
        from functions.andi_extract import extract_andi_graphics_findings
        return extract_andi_graphics_findings(capture_data, self.criterion_id)

    def _extract_keyboard_roundtrip_findings(self, capture_data: CaptureData) -> list[Finding]:
        """Delegates to functions.keyboard_extract.extract_keyboard_roundtrip_findings."""
        from functions.keyboard_extract import extract_keyboard_roundtrip_findings
        return extract_keyboard_roundtrip_findings(capture_data, self.criterion_id)

    # ── SC ↔ ANDI section relevance map ──────────────────────────────
    # Each ANDI section is shown to the judge only on SCs where it
    # could plausibly support or contradict a finding. This keeps the
    # judge prompt focused (avoids 6 × 50KB irrelevant ground-truth
    # blocks on every SC) and matches the per-SC source-of-truth
    # mapping in _extract_andi_*_findings — they accept/reject
    # findings on these SCs, so they're the SCs that need the data.
    #
    # _ANDI_HIDDEN_SCS and _KEYBOARD_ROUNDTRIP_SCS are imported at
    # module level from functions/ so the DOM-context gate and the
    # extractor function share one source of truth.
    _ANDI_CONTRAST_SCS = {"1.4.3", "1.4.6"}
    _ANDI_LANG_SCS = {"3.1.1", "3.1.2"}
    # Intentional: _ANDI_GRAPHICS_SCS is a SUPERSET of the extractor's
    # gate in functions/andi_extract.py. SC 4.1.2 receives the
    # andi_graphics DOM block (so the judge can reason about graphic
    # role/name issues) but the programmatic extractor only emits
    # findings for 1.1.1 / 1.4.5. Keep the asymmetry.
    _ANDI_GRAPHICS_SCS = {"1.1.1", "1.4.5", "4.1.2"}
    _ANDI_TABLES_SCS = {"1.3.1"}
    _ANDI_INTERACTIVE_SCS = {"2.4.4", "2.5.3", "4.1.2"}

    # SCs whose verdict depends on what the page TRANSACTS (legal
    # commitment, financial transaction, data modification, test
    # submission). The judge prompt gets a transaction-scope evidence
    # block; the judge itself decides applicability from those facts +
    # the screenshots -- it is a meaning judgment, not a heuristic.
    _TRANSACTION_SCOPE_SCS = {"3.3.4", "3.3.6"}

    # SCs whose visual-AI / judge prompts get per-image cropped screenshots
    # attached, with explicit IMG-N / BG-N binding in the user-prompt text.
    # Lets the model decide alt='' decorative-vs-meaningful and
    # background-image text questions from actual pixels rather than
    # guessing which screenshot region matches which DOM image entry.
    _IMAGE_BOUND_SCS = {"1.1.1", "1.4.5", "4.1.2"}

    def _extract_andi_hidden_findings(self, capture_data: CaptureData) -> list[Finding]:
        """Extract ANDI hidden-content findings. Delegates to functions/andi_extract.py."""
        from functions.andi_extract import extract_andi_hidden_findings
        return extract_andi_hidden_findings(capture_data, self.criterion_id)

    def _format_andi_image_context(self, capture_data: CaptureData, is_aaa: bool) -> str:
        """Render the ANDI per-text-node contrast table for the visual AI.

        Same pattern as the COMPUTED CONTRAST RATIOS block already used
        by Check_1_4_3.get_image_context: gives the visual model
        deterministic ratios so it doesn't have to estimate from
        screenshots, and tells it which entries it MUST manually
        inspect (the bg_image_present ones, where the resolved-via-
        walk-up colour isn't necessarily what the user actually sees).

        ``is_aaa`` controls the threshold reported in PASS/FAIL labels
        (SC 1.4.6 needs 7.0 normal / 4.5 large, SC 1.4.3 needs
        4.5 normal / 3.0 large).
        """
        results = getattr(capture_data, "andi_contrast_results", None) or []
        if not results:
            return ""

        sc_label = "1.4.6 (AAA, 7:1 normal / 4.5:1 large)" if is_aaa else \
                   "1.4.3 (AA, 4.5:1 normal / 3:1 large)"
        lines = [
            f"ANDI PER-TEXT-NODE CONTRAST (deterministic, SC {sc_label}):",
            "Walks every visible text node (including SVG <text>) and "
            "resolves the effective background by walking up to the "
            "nearest non-transparent ancestor. Use these ratios as "
            "ground truth — do NOT re-estimate them from screenshots.",
            "Entries marked [BG-IMAGE] need a manual visual check: "
            "the resolved-via-walk-up colour is not necessarily what "
            "the user actually sees over the image/gradient.",
            "",
        ]
        for r in results:
            ratio = r.get("ratio")
            req_aa = r.get("required_ratio") or 4.5
            is_large = bool(r.get("is_large_text"))
            if is_aaa:
                req = 4.5 if is_large else 7.0
            else:
                req = req_aa
            sel = r.get("selector", "?")
            tag = r.get("tag", "")
            text = r.get("text", "") or ""
            fg = r.get("fg_color_raw", "")
            bg = r.get("bg_color_raw", "")
            walk = r.get("bg_walk_depth", 0)
            bg_img = bool(r.get("bg_image_present"))
            svg = bool(r.get("is_svg_text"))

            tags = []
            if bg_img:
                tags.append("BG-IMAGE")
            if svg:
                tags.append("SVG")
            if is_large:
                tags.append("LARGE")
            tag_str = (" [" + ",".join(tags) + "]") if tags else ""

            if ratio is None:
                status = "UNMEASURABLE"
                ratio_str = "?:1"
            else:
                status = "PASS" if ratio >= req else "FAIL"
                ratio_str = f"{ratio:.2f}:1"

            lines.append(
                f"  <{tag}> \"{text}\" — {fg} on {bg} (walk_depth={walk}) "
                f"= {ratio_str} [need {req}:1 → {status}]{tag_str} "
                f"sel={sel}"
            )

        return "\n".join(lines)

    # ------------------------------------------------------------------
    # Criterion-specific images for AI (override for visual criteria)
    # ------------------------------------------------------------------

    def _build_dom_context(self, capture_data: CaptureData) -> str:
        """Build verified DOM facts for the judge to cross-check AI findings.

        Uses the STRUCTURED data extracted by Playwright during capture
        (not regex on raw HTML). This data is accurate and complete —
        Playwright evaluated JavaScript in the live browser DOM.

        The judge uses these facts to verify or reject AI claims.
        If an AI finding says 'radio not in fieldset' but DOM facts say
        'Radio inputs: YES, in <fieldset>: YES', the judge rejects it.
        """
        import re as _re
        parts: list[str] = []
        html = capture_data.html or ""

        if not html and not capture_data.headings and not capture_data.images:
            return ""

        parts.append(
            "The following facts were extracted directly from the "
            "browser DOM by Playwright. Use them to VERIFY or REJECT "
            "AI claims. If an AI finding contradicts these facts, "
            "REJECT that finding — these are ground truth."
        )

        # 1. Page title
        if capture_data.title:
            parts.append(f'PAGE TITLE: "{capture_data.title}"')

        # 2. Page language
        lang = capture_data.page_language
        if lang:
            parts.append(
                f'LANGUAGE: html lang="{lang.get("html_lang", "MISSING")}" '
                f'(valid={lang.get("lang_valid", "?")})'
            )
        else:
            lang_m = _re.search(r'<html[^>]*\blang=["\']([^"\']*)["\']',
                                html, _re.IGNORECASE)
            parts.append(f'LANGUAGE: html lang="{lang_m.group(1)}"' if lang_m
                         else 'LANGUAGE: html lang=MISSING')

        # 3. Headings — exact count per level + full list
        headings = capture_data.headings
        if headings:
            from collections import Counter as _Counter
            levels = _Counter(h.get("level", 0) for h in headings)
            heading_lines = []
            for h in headings:
                base_line = (
                    f'    <h{h.get("level", "?")}> "{h.get("text") or ""}"'
                )
                loc_label = (h.get("location_label") or "").strip()
                if loc_label:
                    heading_lines.append(f"{base_line}\n      LOCATION: {loc_label}")
                else:
                    heading_lines.append(base_line)
            parts.append(
                "HEADINGS:\n"
                "  Counts: " + ", ".join(f"h{lv}={c}" for lv, c in sorted(levels.items())) + "\n"
                "  List:\n" + "\n".join(heading_lines)
            )

        # 4. Images — alt text values + structural decorative signals.
        # When alt="" is combined with a parallax/overlay ancestor or
        # a picture tag that has a sibling prose container, the empty
        # alt is almost certainly correct -- the image is a decorative
        # background and the real content lives in the sibling. The
        # judge must weigh those signals BEFORE flagging a large alt=""
        # image as "meaningful content marked decorative".
        images = capture_data.images
        if images:
            img_lines = []
            any_crop = False
            for img in images:
                src = (img.get("src") or "?").split("/")[-1]
                alt = img.get("alt")
                role = (img.get("role") or "").lower()
                aria_hidden = img.get("aria_hidden") or img.get("aria-hidden")
                if alt is None:
                    alt_desc = "(NO alt ATTRIBUTE)"
                elif alt == "":
                    alt_desc = '(EMPTY alt="")'
                else:
                    alt_desc = f'"{alt}"'
                flags = []
                if role in ("presentation", "none"):
                    flags.append("decorative")
                if aria_hidden:
                    flags.append("aria-hidden")
                sig = img.get("decorative_signals") or {}
                structural = []
                if sig.get("inside_parallax_container"):
                    structural.append("parallax-ancestor")
                if sig.get("has_parallax_data_attr"):
                    structural.append("data-parallax-attr")
                if sig.get("inside_overlay_block"):
                    structural.append("overlay-block-ancestor")
                if sig.get("parent_has_content_sibling"):
                    structural.append("section-has-prose-sibling")
                if sig.get("inside_hero_video_layer"):
                    structural.append("hero-video-layer")
                if structural:
                    flags.append("structurally-decorative: " + ", ".join(structural))
                flag_str = f" [{'; '.join(flags)}]" if flags else ""
                loc_label = (img.get("location_label") or "").strip()
                loc_str = f"\n      LOCATION: {loc_label}" if loc_label else ""
                # Per-image crop binding for SCs 1.1.1 / 1.4.5 / 4.1.2.
                # When a crop_id is present, the visual-AI / judge call
                # has the corresponding cropped screenshot attached; the
                # auditor-facing block tells the model "look at the
                # actual image, don't guess." See functions/image_crops.py.
                crop_id = img.get("crop_id")
                crop_str = (
                    f"\n      VISUAL: cropped screenshot attached as {crop_id} "
                    f"(see attached images, in order)"
                ) if crop_id else ""
                if crop_id:
                    any_crop = True
                label = f"{crop_id}: " if crop_id else ""
                img_lines.append(
                    f"    {label}{src}: alt={alt_desc}{flag_str}{loc_str}{crop_str}"
                )
            crop_hint = (
                "  Each IMG-N below is bound to a cropped screenshot "
                "(image_img_<N>.png) attached to this call. When deciding "
                "alt='' is correct or alt is missing, INSPECT THE CROP and "
                "also check the structurally-decorative flags. If the crop "
                "shows non-decorative content (text, faces, identifiable "
                "scene) AND no structurally-decorative signal is present, "
                "alt='' is incorrect. If the crop shows pure texture / "
                "gradient / parallax background AND any decorative signal "
                "is present, alt='' is correct -- do NOT flag.\n"
            ) if any_crop else ""
            parts.append(
                "IMAGES (alt text + decorative signals -- if an image has\n"
                "  alt=\"\" AND any structurally-decorative signal, it is\n"
                "  almost certainly a legitimate decorative background, not\n"
                "  content; do NOT flag as missing alt):\n"
                + crop_hint
                + f"IMAGES ({len(images)} total):\n" + "\n".join(img_lines)
            )

        # 4b. Background images — CSS `background-image:url(...)` on
        # non-<img> elements. Previously absent from the prompt entirely,
        # which made SC 1.4.5 (Images of Text) judging blind to bg-image
        # text overlays. Now surfaced with the same crop-binding scheme
        # as inline images.
        #
        # Each entry is also classified as [UI-DECORATION] when the bg
        # is a small inline-SVG visual indicator on a form control (radio
        # dot, checkbox mark, toggle switch). Without this signal the
        # judge cannot distinguish a 16x16 radio-dot SVG from a real
        # informational background image, and visual_ai routinely
        # over-flags these as "CSS background image without text
        # alternative" for SC 1.1.1. Verified case on
        # 20260506_135324_f8765656: SC 1.1.1 produced 2 false-positive
        # findings for #edit-location-inperson and #edit-standing-undergrad
        # whose bg-image was a data:image/svg+xml URI containing a
        # viewBox='-4 -4 8 8' circle (an 8x8 radio-button dot).
        bg_images = capture_data.background_images
        if bg_images:
            bg_lines = []
            any_bg_crop = False
            any_ui_deco = False
            for bg in bg_images:
                sel = (bg.get("selector") or "?")
                bg_url = (bg.get("backgroundImage") or "").strip()
                if bg_url.startswith("url(") and bg_url.endswith(")"):
                    bg_url = bg_url[4:-1].strip("\"' ")
                aria_label = (bg.get("ariaLabel") or "").strip()
                role = (bg.get("role") or "").strip()
                inner_text = (bg.get("text_content") or "").strip()
                tag = (bg.get("tag") or "").lower()
                rect = bg.get("rect") or {}
                try:
                    rw = float(rect.get("width") or 0)
                    rh = float(rect.get("height") or 0)
                except (ValueError, TypeError):
                    rw = rh = 0.0
                # Classify as UI-DECORATION when ALL of:
                #   - bg-image is an inline data: URI (not a real file image)
                #   - the element has no DOM text content of its own
                #   - the element has no aria-label / role overriding decoration
                #   - the element is small (under 32x32 CSS px, the WCAG
                #     2.5.8 minimum target footprint -- below this the
                #     "image" cannot communicate informational content
                #     even if it tried) OR the element is a form-control
                #     tag (input / select / button / option) where the
                #     bg-image is by convention a visual indicator
                ui_deco = (
                    bg_url.startswith("data:")
                    and not inner_text
                    and not aria_label
                    and role not in ("img", "image", "graphics-symbol")
                    and (
                        (0 < rw < 32 and 0 < rh < 32)
                        or tag in ("input", "select", "button", "option")
                    )
                )
                if ui_deco:
                    any_ui_deco = True
                inner_text_preview = (
                    f' inner_text="{inner_text}"'
                ) if inner_text else ""
                attrs = []
                if aria_label:
                    attrs.append(f'aria-label="{aria_label}"')
                if role:
                    attrs.append(f"role={role}")
                attr_str = " " + " ".join(attrs) if attrs else ""
                rect_str = f" rect={rw:.0f}x{rh:.0f}" if (rw or rh) else ""
                crop_id = bg.get("crop_id")
                crop_str = (
                    f"\n      VISUAL: cropped screenshot attached as {crop_id}"
                ) if crop_id else ""
                if crop_id:
                    any_bg_crop = True
                label = f"{crop_id}: " if crop_id else ""
                deco_marker = "[UI-DECORATION] " if ui_deco else ""
                bg_lines.append(
                    f"    {label}{deco_marker}selector={sel}{rect_str} "
                    f"background-image-url="
                    f"{bg_url!r}{attr_str}{inner_text_preview}{crop_str}"
                )
            bg_hint_parts = []
            if any_bg_crop:
                bg_hint_parts.append(
                    "  Each BG-N below is a CSS `background-image` on a non-<img> "
                    "element. The crops let you SEE what each background actually "
                    "shows.\n"
                    "  CRITICAL SC 1.4.5 RULE: `inner_text` shown for each BG-N "
                    "below is the element's DOM textContent. If `inner_text` is "
                    "non-empty AND the visible text in the crop matches "
                    "(or is contained within) `inner_text`, the visible text is "
                    "RENDERED HTML on top of the image -- the bg-image is "
                    "decorative texture / photo / pattern. This is NOT an SC "
                    "1.4.5 failure. REJECT visual-AI / code-AI findings that "
                    "claim 'image-of-text' for these entries -- they fired on "
                    "the screenshot without checking inner_text.\n"
                    "  Only flag SC 1.4.5 when the crop shows visible text that "
                    "is NOT in inner_text and NOT a logotype (logos are exempt). "
                    "Pure image-of-text would have empty or unrelated inner_text.\n"
                    "  SC 1.1.1 failure (different criterion): the crop shows "
                    "informational content (logo with name, illustration "
                    "conveying meaning) and the element has no aria-label / "
                    "role=img / sibling text alternative.\n"
                )
            if any_ui_deco:
                bg_hint_parts.append(
                    "  Entries marked [UI-DECORATION] are form-control visual "
                    "indicators (radio dots, checkbox checkmarks, toggle-switch "
                    "knobs, dropdown arrows) drawn via tiny inline-SVG data URIs "
                    "on small elements (under 32x32 CSS px) or form-control tags "
                    "(<input>, <select>, <button>, <option>). These are pure UI "
                    "styling -- they are NOT informational content. The actual "
                    "label for the form control is rendered as sibling text "
                    "outside the element. DO NOT emit findings for [UI-DECORATION] "
                    "entries under SC 1.1.1 (Non-text Content) or SC 1.4.5 "
                    "(Images of Text); the WCAG name-cascade for the radio / "
                    "checkbox / control comes from its <label> or aria-label, "
                    "not from its background-image. REJECT visual-AI / code-AI "
                    "findings that claim a [UI-DECORATION] entry needs alt text "
                    "-- they mistook a 16x16 radio-button dot for a meaningful "
                    "background image.\n"
                )
            bg_hint = "".join(bg_hint_parts)
            parts.append(
                "BACKGROUND IMAGES (CSS `background-image` on non-<img> "
                "elements; relevant for SC 1.4.5 (Images of Text) and "
                "SC 1.1.1 when the bg image conveys content):\n"
                + bg_hint
                + f"BACKGROUND IMAGES ({len(bg_images)} total):\n"
                + "\n".join(bg_lines)
            )

        # 5. Landmarks — role, label, count
        landmarks = capture_data.landmarks
        if landmarks:
            lm_lines = []
            for lm in landmarks:
                role = lm.get("role", "?")
                label = lm.get("ariaLabel") or lm.get("aria-label") or "(no label)"
                tag = lm.get("tag", "?")
                lm_lines.append(f"    <{tag}> role={role} label=\"{label}\"")
            parts.append(
                f"LANDMARKS ({len(landmarks)}):\n" + "\n".join(lm_lines)
            )

        # 6. Form fields — type, label, name, required, autocomplete, grouping
        form_fields = capture_data.form_fields
        if form_fields:
            ff_lines = []
            has_fieldset = any(
                ff.get("in_fieldset") or ff.get("fieldset")
                for ff in form_fields
            )
            for ff in form_fields:
                ftype = ff.get("type", "?")
                label = ff.get("label", "")
                name = ff.get("name", "")
                required = ff.get("required", False)
                autocomplete = ff.get("autocomplete", "")
                aria_label = ff.get("aria_label") or ff.get("aria-label") or ""
                placeholder = ff.get("placeholder", "")
                in_fieldset = ff.get("in_fieldset", False)
                sel = ff.get("selector", "")

                label_desc = label or aria_label or f"placeholder=\"{placeholder}\"" if placeholder else "(NO LABEL)"
                group_label = (ff.get("group_label") or "").strip()
                group_str = f' group="{group_label}"' if group_label else ""
                flags = []
                if required:
                    flags.append("required")
                if autocomplete:
                    flags.append(f"autocomplete={autocomplete}")
                if in_fieldset:
                    flags.append("in-fieldset")
                flag_str = f" [{', '.join(flags)}]" if flags else ""
                loc_label = (ff.get("location_label") or "").strip()
                loc_str = f"\n      LOCATION: {loc_label}" if loc_label else ""
                ff_lines.append(
                    f"    {ftype} name=\"{name}\" label=\"{label_desc}\"{group_str}{flag_str}{loc_str}"
                )

            # Check radio grouping from HTML as fallback
            radio_fields = [ff for ff in form_fields if ff.get("type") == "radio"]
            radio_note = ""
            if radio_fields:
                in_fs = any(ff.get("in_fieldset") for ff in radio_fields)
                if not in_fs and html:
                    # Fallback: check raw HTML for fieldset wrapping radios
                    in_fs = bool(_re.search(
                        r"<fieldset\b[^>]*>.*?type=[\"']radio[\"'].*?</fieldset>",
                        html, _re.IGNORECASE | _re.DOTALL,
                    ))
                radio_note = f"\n  Radio inputs: {len(radio_fields)}, in <fieldset>: {'YES' if in_fs else 'NO'}"

            parts.append(
                f"FORM FIELDS ({len(form_fields)}):\n"
                + "\n".join(ff_lines) + radio_note
            )

        # 6b. Transaction-scope evidence (SC 3.3.4 / 3.3.6). Facts only;
        # the judge decides whether the page involves a legal/financial/
        # data transaction from this block + the screenshots.
        if self.criterion_id in self._TRANSACTION_SCOPE_SCS:
            parts.append("\n".join(self._format_transaction_signals(capture_data)))

        # 7. Links — count + samples
        links = capture_data.links
        if links:
            link_lines = []
            for lnk in links:
                text = (lnk.get("text") or "").strip()
                href = lnk.get("href") or ""
                aria_label = lnk.get("aria_label") or lnk.get("aria-label") or ""
                if not text and aria_label:
                    text = f"[aria-label: {aria_label}]"
                elif not text:
                    text = "(NO TEXT)"
                loc_label = (lnk.get("location_label") or "").strip()
                loc_str = f"\n      LOCATION: {loc_label}" if loc_label else ""
                link_lines.append(f'    "{text}" -> {href}{loc_str}')
            parts.append(
                f"LINKS ({len(links)} total):\n" + "\n".join(link_lines)
            )

        # 8. Tables — structure
        tables = capture_data.tables
        if tables:
            tbl_lines = []
            for tbl in tables:
                caption = tbl.get("caption", "")
                headers = tbl.get("has_headers") or tbl.get("headers")
                rows = tbl.get("row_count", "?")
                tbl_lines.append(
                    f'    caption="{caption or "(none)"}" headers={headers} rows={rows}'
                )
            parts.append(f"TABLES ({len(tables)}):\n" + "\n".join(tbl_lines))

        # 8b. Lists — counts + per-list itemCount. SC 1.3.1 (info &
        # relationships) cares about list semantics: groups of items that
        # belong together must be marked up with <ul>/<ol>/<dl>, not
        # divs. Without this section the AI never sees how many lists are
        # on the page or how many items each contains.
        lists = getattr(capture_data, "lists", None) or []
        if lists:
            list_lines = []
            for lst in lists:
                tag = lst.get("tag", "?")
                cnt = lst.get("itemCount", lst.get("item_count", 0))
                role = (lst.get("role") or "").strip()
                aria_label = (lst.get("aria_label") or lst.get("aria-label") or "").strip()
                role_str = f" role={role}" if role else ""
                label_str = f' aria-label="{aria_label}"' if aria_label else ""
                loc_label = (lst.get("location_label") or "").strip()
                loc_str = f"\n      LOCATION: {loc_label}" if loc_label else ""
                list_lines.append(f"    <{tag}>{role_str}{label_str} items={cnt}{loc_str}")
            parts.append(f"LISTS ({len(lists)}):\n" + "\n".join(list_lines))

        # 9. Skip links -- inventory AND deterministic verification
        # results. SC 2.4.1 requires a keyboard-operable bypass; a
        # skip link that only works on mouse click fails. We ship
        # keyboard_activates / click_activates / focus_landed_on_target
        # so the judge does not have to guess from the DOM whether the
        # link actually works.
        skip_links = capture_data.skip_links
        if skip_links:
            sl_lines = [
                f'    "{sl.get("text", "?")}" -> {sl.get("href", "?")}'
                for sl in skip_links
            ]
            parts.append(f"SKIP LINKS ({len(skip_links)}):\n" + "\n".join(sl_lines))
        skip_link_results = getattr(capture_data, "skip_link_results", None) or []
        if skip_link_results:
            slr_lines = []
            for sl in skip_link_results:
                text = sl.get("skip_link_text", "") or sl.get("text", "?")
                target = sl.get("target_href", "") or "?"
                kb = sl.get("keyboard_activates")
                click = sl.get("click_activates")
                landed = sl.get("focus_landed_on_target")
                first = sl.get("is_first_tabstop")
                err = sl.get("error") or ""
                slr_lines.append(
                    f'    "{text}" -> {target}: '
                    f'keyboard_activates={kb}, '
                    f'click_activates={click}, '
                    f'focus_landed_on_target={landed}, '
                    f'is_first_tabstop={first}'
                    + (f', error="{err}"' if err else "")
                )
            parts.append(
                "SKIP LINK VERIFICATION (deterministic end-to-end test --\n"
                "  keyboard_activates=True means the link MOVED FOCUS to the\n"
                "  target when activated via Enter; do NOT flag a skip link\n"
                "  as non-functional if keyboard_activates is True, even if\n"
                "  click_activates is False or error mentions click path):\n"
                + "\n".join(slr_lines)
            )

        # 10. Tab walk summary — keyboard reachability facts. Report the
        # AUTHORITATIVE reached count (from tab_coverage) so this line never
        # contradicts the TAB COVERAGE block below, and warn the judge when
        # the walk is unreliable so a degraded/interrupted walk cannot be read
        # as a real keyboard barrier.
        tab_walk = capture_data.tab_walk
        if tab_walk:
            from functions.keyboard_extract import assess_tab_walk_reliability
            _rel = assess_tab_walk_reliability(capture_data)
            parts.append(
                f"TAB WALK: {_rel['reached']} elements reached by keyboard "
                f"(of {_rel['total_interactive']} focusable; "
                f"{_rel['coverage_percent']:.0f}% coverage)"
            )
            if not _rel["reliable"]:
                parts.append(
                    "TAB WALK RELIABILITY WARNING: the keyboard walk is "
                    f"UNRELIABLE -- {_rel['reason']}. The low coverage reflects "
                    "the CAPTURE, not the page. DO NOT emit a 'keyboard "
                    "inaccessible', 'not reachable via keyboard', '0% coverage', "
                    "or 'N elements unreachable' finding under SC 2.1.1, 2.1.2, "
                    "2.1.3, 2.4.3, or 2.4.7 based on this walk -- the page "
                    "demonstrably has focusable elements the walk failed to "
                    "enumerate. Judge keyboard operability from the DOM/ARIA "
                    "evidence and per-element focus data instead."
                )

        # 11. Keyboard traps
        traps = capture_data.keyboard_traps
        if traps:
            trap_lines = [
                f'    {t.get("type", "?")} at {t.get("selector", "?")}'
                for t in traps
            ]
            parts.append(f"KEYBOARD TRAPS ({len(traps)}):\n" + "\n".join(trap_lines))

        # 12. Duplicate IDs (from HTML since this isn't in structured data)
        if html:
            all_ids = _re.findall(r'\bid\s*=\s*["\']([^"\']+)["\']', html, _re.IGNORECASE)
            if all_ids:
                from collections import Counter as _Counter2
                id_counts = _Counter2(all_ids)
                dupes = {k: v for k, v in id_counts.items() if v > 1}
                if dupes:
                    dupe_lines = [
                        f'    id="{did}" appears {cnt} times'
                        for did, cnt in dupes.items()
                    ]
                    parts.append(
                        f"DUPLICATE IDS ({len(dupes)}):\n" + "\n".join(dupe_lines)
                    )

        # 13. Media elements
        media = capture_data.media
        if media:
            audio_probe = getattr(capture_data, "audio_detection", None) or {}
            page_audio_type = str(audio_probe.get("audio_type") or "").lower()
            media_lines = []
            for m in media:
                mtype = m.get("tag") or m.get("type") or "?"
                autoplay = m.get("autoplay", False)
                controls = m.get("controls", False)
                muted = m.get("muted", False)
                loop = m.get("loop", False)
                tracks = m.get("tracks") or []
                captions = m.get("has_captions") or m.get("captions", False)
                sel = m.get("selector", "?")
                # Audio-track classification. A muted <video> with no
                # <track> elements, on a page the audio probe measured
                # silent, is VIDEO-ONLY content: SC 1.2.1 applies; the
                # synchronized-media SCs (1.2.2 / 1.2.3 / 1.2.5) do not.
                # Without this the judge wrongly demands captions or
                # audio descriptions for a silent video, and rejects a
                # valid scene-by-scene text description because it has
                # "no dialogue" (verified on a university SC 1.2.1 / 1.2.8).
                is_silent = bool(muted) or page_audio_type in (
                    "silence", "silent", "none"
                )
                if str(mtype).lower() == "video" and is_silent:
                    audio_note = (
                        "VIDEO-ONLY / SILENT (muted, no audio track) — "
                        "SC 1.2.1 applies. SC 1.2.8 (Media Alternative) "
                        "also applies and is satisfied by the same text "
                        "alternative: a scene-by-scene TEXT DESCRIPTION "
                        "of the visual content. The synchronized-media "
                        "SCs 1.2.2 / 1.2.3 / 1.2.5 / 1.2.7 do NOT apply "
                        "(there is no audio to caption, describe, or "
                        "transcribe). Do NOT demand dialogue, non-speech "
                        "audio, or a 'comprehensive transcript with "
                        "dialogue' for any of these SCs — none of that "
                        "content exists. The visual scene description "
                        "IS the complete media alternative for a silent "
                        "video-only file"
                    )
                elif str(mtype).lower() == "audio":
                    audio_note = "AUDIO-ONLY content — SC 1.2.1 applies"
                else:
                    audio_note = (
                        "appears to carry an audio track — the "
                        "synchronized-media SCs apply"
                    )
                media_lines.append(
                    f"    <{mtype}> @ {sel} — autoplay={autoplay} "
                    f"controls={controls} muted={muted} loop={loop} "
                    f"<track>-elements={len(tracks)} captions={captions}\n"
                    f"      {audio_note}"
                )
            # Scan the page for an in-page text alternative / transcript
            # / description mechanism so the judge does not wrongly
            # conclude one is absent. These are CANDIDATES — the judge
            # decides whether a candidate genuinely describes the media.
            alt_terms = (
                "video description", "audio description", "described video",
                "transcript", "text description", "text alternative",
                "view description", "media alternative", "video transcript",
            )
            alt_candidates: list[str] = []
            inv = getattr(capture_data, "element_inventory", None) or []
            if isinstance(inv, dict):
                inv = inv.get("elements") or []
            for coll, kind in (
                (capture_data.links or [], "link"),
                (capture_data.form_fields or [], "control"),
                (inv, "element"),
            ):
                for el in coll:
                    if not isinstance(el, dict):
                        continue
                    name = " ".join(
                        str(el.get(k) or "") for k in (
                            "text", "inner_text", "accessible_name",
                            "aria_label", "aria-label", "name", "label",
                        )
                    ).lower()
                    if any(t in name for t in alt_terms):
                        label = (
                            el.get("text") or el.get("accessible_name")
                            or el.get("aria_label") or ""
                        ).strip()
                        sel2 = el.get("selector") or el.get("href") or "?"
                        entry = f'{kind} "{label[:60]}" @ {sel2}'
                        if entry not in alt_candidates:
                            alt_candidates.append(entry)
            block = f"MEDIA ({len(media)}):\n" + "\n".join(media_lines)
            if alt_candidates:
                block += (
                    "\n  POSSIBLE IN-PAGE TEXT ALTERNATIVE(S) for the "
                    "media above — a transcript / description mechanism "
                    "exists on the page. The judge MUST check whether one "
                    "of these serves as the media's text alternative "
                    "before concluding none is provided:\n"
                    + "\n".join(f"    - {c}" for c in alt_candidates)
                )
            parts.append(block)

        # 14. Search field count (from form_fields)
        search_fields = [
            ff for ff in (capture_data.form_fields or [])
            if ff.get("type") == "search" or "search" in (ff.get("name") or "").lower()
        ]
        parts.append(f"SEARCH FIELDS: {len(search_fields)}")

        # 14b. Motion-pause control detection for SC 2.2.2 — same block
        # the visual_ai prompt receives via get_image_context. The judge
        # needs to see this so it does NOT manufacture "no pause
        # mechanism" findings when external pause buttons exist.
        if self.criterion_id == "2.2.2":
            parts.append(
                "\n".join(self._format_motion_pause_controls(capture_data))
            )

        # 14c. Target-size measurements for SC 2.5.8 — without this, the
        # judge has only screenshots + a list of links by name, and
        # it routinely hallucinates "0px spacing" / "below 24x24" for
        # elements the spacing exception lets pass. Surface the actual
        # rect dimensions, center-to-center distances, and deterministic
        # verdicts so the model is forced to reason from measurements
        # instead of from a small-rendered screenshot.
        if self.criterion_id in ("2.5.5", "2.5.8"):
            parts.append(
                "\n".join(self._format_target_size_measurements(capture_data))
            )

        # 14d. Link-styling measurements for SC 1.4.1 — without this, the
        # judge sees a small screenshot where the underline can be hard to
        # read at thumbnail resolution, and it falls back to its prior
        # "color-only links fail 1.4.1" without checking. Surface the per-
        # link computed text-decoration / border / icon / font-weight data
        # that Phase D already gathers, plus a deterministic PASS/FAIL.
        if self.criterion_id == "1.4.1":
            parts.append(
                "\n".join(self._format_link_styling_measurements(capture_data))
            )

        # 15. Axe-core summary
        axe = capture_data.axe_results
        if axe:
            v_count = len(axe.get("violations", []))
            p_count = len(axe.get("passes", []))
            parts.append(
                f"AXE-CORE: {v_count} violations, {p_count} passes"
            )

        # 16. Visible page text (full — no truncation)
        if html:
            text = _re.sub(r"<script[^>]*>.*?</script>", "", html,
                           flags=_re.IGNORECASE | _re.DOTALL)
            text = _re.sub(r"<style[^>]*>.*?</style>", "", text,
                           flags=_re.IGNORECASE | _re.DOTALL)
            text = _re.sub(r"<[^>]+>", " ", text)
            text = _re.sub(r"\s+", " ", text).strip()
            if text:
                parts.append("VISIBLE PAGE TEXT:\n" + text)

        # 17. Pixel-sampled contrast (per-element measured ratios)
        pixel_contrast = getattr(capture_data, "pixel_contrast", None) or []
        if pixel_contrast:
            pc_lines = []
            for entry in pixel_contrast:
                if not isinstance(entry, dict):
                    continue
                sel = entry.get("selector", "?")
                ratio = entry.get("contrast_ratio")
                fg = entry.get("fg_color")
                bg = entry.get("bg_color")
                method = entry.get("method", "")
                pc_lines.append(
                    f"    {sel}: ratio={ratio} fg={fg} bg={bg} method={method}"
                )
            parts.append(
                f"PIXEL CONTRAST ({len(pc_lines)} samples — deterministic "
                f"k-means measurement, reject any finding contradicting a "
                f"measured ratio):\n" + "\n".join(pc_lines)
            )

        # 18. Focused-state contrast.
        # Field names in capture_data.focus_contrast (set by
        # capture/interactive_capture.py:_capture_focus_contrast):
        #   contrast_ratio (NOT indicator_contrast)
        #   has_change     (NOT visible)
        #   indicator_color, bg_color, indicator_type, focused_styles,
        #   unfocused_styles, changed_properties.
        # Earlier formatter used "indicator_contrast" + "visible" —
        # both absent from the actual entries — so every line emitted
        # `visible=None indicator_contrast=None`. The judge then had no
        # measured contrast values for SC 1.4.11 / 2.4.7 and the
        # visual AI filled the gap by hallucinating ratios from
        # screenshots (observed: "2.36:1" / "2.7:1" claims with empty
        # evidence on SC 1.4.11). Renamed to the actual field names so
        # the deterministic ratio reaches the prompt.
        focus_contrast = getattr(capture_data, "focus_contrast", None) or []
        if focus_contrast:
            fc_lines = []
            for entry in focus_contrast:
                if not isinstance(entry, dict):
                    continue
                sel = entry.get("selector", "?")
                ratio = entry.get("contrast_ratio")
                color = entry.get("indicator_color")
                bg = entry.get("bg_color")
                ind_type = entry.get("indicator_type", "")
                has_change = entry.get("has_change")
                fc_lines.append(
                    f"    {sel}: has_change={has_change} contrast_ratio={ratio} "
                    f"indicator_type={ind_type!r} indicator={color} bg={bg}"
                )
            parts.append(
                f"FOCUS CONTRAST ({len(fc_lines)} elements — deterministic "
                f"focus-indicator measurement; reject any finding that "
                f"contradicts a measured ratio):\n" + "\n".join(fc_lines)
            )

        # 19. ANDI per-text-node contrast (SSA Section 508 methodology).
        # Walks every visible text node, including SVG <text>, and
        # reports each node's effective contrast ratio against its
        # walked-up background ancestor. Higher granularity than the
        # element-level "colors" extractor: catches per-text-node colour
        # overrides, SVG fill, and partial-text wrapping. The judge can
        # use this as ground truth for SC 1.4.3 and 1.4.6 — reject any
        # AI claim that contradicts a measured ratio here.
        andi_contrast = getattr(capture_data, "andi_contrast_results", None) or []
        if andi_contrast and self.criterion_id in self._ANDI_CONTRAST_SCS:
            # Entries with bg_image_present=True have a known-unreliable
            # background reading: the bg-color walk landed on a fallback
            # color (white) while the rendered backdrop is actually a
            # video, gradient, image, or overlay. The ratio computed for
            # these entries is meaningful ONLY when checked against the
            # rendered pixels (which the deterministic check cannot do).
            # Past behaviour reported these as ``passes=False`` and the
            # judge treated them as confirmed failures, producing
            # phantom "1.23:1" findings against a university's hero text. We now
            # explicitly mark these as UNCERTAIN so the judge knows the
            # ratio is informational only and must NOT be cited as
            # evidence of an SC 1.4.3 failure without visual
            # corroboration.
            # SPLIT into two sections — DETERMINISTIC (judge can use as
            # evidence) and MANUAL-REVIEW (judge must NOT cite as
            # evidence; included only as a count + sample so the judge
            # knows manual review is needed but cannot fabricate
            # findings from unreliable ratios).
            #
            # Earlier behaviour: kept all entries in one section with
            # the rule "DO NOT emit a HIGH finding citing UNCERTAIN
            # ratios" — but Gemini Flash Lite ignored the rule and
            # produced 3 HIGH findings citing 1.23:1 on hero text on
            # 2026-04-29 (the bg-color fallback ratio, not the actual
            # rendered contrast). The model is more compliant when the
            # unreliable data is physically removed from its evidence
            # block rather than just labelled "do not use".
            deterministic_lines = []
            uncertain_samples = []
            uncertain_count = 0
            for entry in andi_contrast:
                if not isinstance(entry, dict):
                    continue
                sel = entry.get("selector", "?")
                ratio = entry.get("ratio")
                req = entry.get("required_ratio")
                passes = entry.get("passes")
                large = entry.get("is_large_text")
                bg_img = entry.get("bg_image_present")
                walk = entry.get("bg_walk_depth")
                fg = entry.get("fg_color_raw", "")
                bg = entry.get("bg_color_raw", "")
                text = entry.get("text", "") or ""
                if bg_img:
                    uncertain_count += 1
                    if uncertain_count <= 5:
                        # Sample only — provide texture without showing
                        # the specific (unreliable) ratios that the model
                        # is tempted to cite as evidence.
                        uncertain_samples.append(
                            f"    {sel}: text=\"{text}\" (over "
                            f"video/image/gradient — manual review only, "
                            f"ratio NOT shown to prevent fabricated findings)"
                        )
                else:
                    deterministic_lines.append(
                        f"    {sel}: ratio={ratio} required={req} passes={passes} "
                        f"large={large} walk_depth={walk} "
                        f"fg={fg} bg={bg} text=\"{text}\""
                    )
            parts.append(
                f"ANDI CONTRAST — DETERMINISTIC ({len(deterministic_lines)} text "
                f"nodes with reliable bg colour readings, no background "
                f"image obscuring the resolved color). Trust these "
                f"measured ratios as ground truth. For SC 1.4.3 / 1.4.6, "
                f"reject any AI finding contradicting these values.\n"
                f"NO-FABRICATION RULE: Cite a numeric contrast ratio for an "
                f"element ONLY when that element's selector appears in the list "
                f"below. If a selector is NOT in this list, do NOT invent a "
                f"ratio for it under any source tag (programmatic / axe / andi "
                f"/ htmlcs / ibm_eac) — describe the issue qualitatively or "
                f"recommend manual review. Do NOT borrow numbers from the "
                f"PIXEL CONTRAST / FOCUS CONTRAST / NON-TEXT CONTRAST blocks "
                f"elsewhere in this prompt — those measure different things "
                f"(focus-indicator outlines, UI-component edges, sampled "
                f"pixels) and are NOT the text-on-background ratio that SC "
                f"1.4.3 evaluates. A finding that cites a number not in the "
                f"list below for the EXACT selector under discussion is a "
                f"fabrication and must be omitted.\n"
                + "\n".join(deterministic_lines)
            )
            if uncertain_count > 0:
                parts.append(
                    f"ANDI CONTRAST — MANUAL REVIEW ({uncertain_count} text "
                    f"nodes over a background image, gradient, or video). "
                    f"The bg-color walk landed on a fallback colour, so any "
                    f"computed ratio is UNRELIABLE and is INTENTIONALLY "
                    f"OMITTED from this prompt to prevent fabricated SC "
                    f"1.4.3 / 1.4.6 findings. Do NOT emit findings citing "
                    f"a contrast ratio for these elements. The "
                    f"deterministic check has already emitted INFO "
                    f"findings flagging them for manual review; do not "
                    f"duplicate.\n"
                    + "\n".join(uncertain_samples)
                )

        # 19b. ANDI language audit (sANDI). Document-level lang state
        # plus every per-segment lang attribute, validated against
        # BCP 47, with redundant + xml:lang mismatch flags. Use these
        # for SC 3.1.1 (page lang) and SC 3.1.2 (parts) — and reject
        # any finding that contradicts a state recorded here.
        andi_lang = getattr(capture_data, "andi_lang_results", None) or {}
        if andi_lang and self.criterion_id in self._ANDI_LANG_SCS:
            lang_lines = [
                f"  HTML lang: \"{andi_lang.get('html_lang','')}\" "
                f"valid={andi_lang.get('html_lang_valid')} "
                f"xml:lang=\"{andi_lang.get('html_xml_lang','')}\" "
                f"html_xml_match={andi_lang.get('html_lang_xml_lang_match')}",
                f"  Document issues: {andi_lang.get('issues') or []}",
            ]
            for s in andi_lang.get("segments") or []:
                if not isinstance(s, dict):
                    continue
                lang_lines.append(
                    f"    <{s.get('tag','?')}> lang=\"{s.get('lang','')}\" "
                    f"valid={s.get('lang_valid')} "
                    f"xml:lang=\"{s.get('xml_lang','')}\" "
                    f"xml_match={s.get('xml_lang_matches_lang')} "
                    f"inherited=\"{s.get('inherited_lang','')}\" "
                    f"redundant={s.get('redundant')} "
                    f"hidden={s.get('is_hidden')} "
                    f"sel={s.get('selector','?')} "
                    f"text=\"{s.get('text','')}\""
                )
            parts.append(
                f"ANDI LANG AUDIT ({len(andi_lang.get('segments') or [])} "
                f"explicit-lang segments — deterministic, reject any "
                f"finding contradicting a state recorded here):\n"
                + "\n".join(lang_lines)
            )

        # 19c. ANDI hidden-content audit (hANDI). Focusable elements that
        # are simultaneously hidden — phantom tab stops, ARIA-spec
        # violations. Use to corroborate or reject AI claims about
        # focus order, keyboard reachability, and focus visibility.
        #
        # Entries are annotated [BROWSER-HANDLED] when the browser will
        # not place them in the tab order regardless of tabindex. This
        # is the case for elements with rect=0x0 (no rendered box) or
        # `display:none`/`visibility:hidden`/`hidden`/`inert` reasons —
        # the focus order skips them. The judge MUST NOT flag these as
        # focus leaks across SC 2.1.1 / 2.4.3 / 2.4.7 / 4.1.2 / 1.3.1
        # because the focus leak does not actually occur. This is the
        # OneTrust / cookie-banner / hidden-modal pattern: the elements
        # exist with tabindex but are not reachable until the modal is
        # opened, at which point the hide CSS is removed.
        andi_hidden = getattr(capture_data, "andi_hidden_results", None) or []
        if andi_hidden and self.criterion_id in _ANDI_HIDDEN_SCS:
            h_lines = []
            browser_handled_count = 0
            for h in andi_hidden:
                if not isinstance(h, dict):
                    continue
                reasons = h.get("hidden_reasons") or []
                browser_skips = is_browser_handled(h)
                marker = "[BROWSER-HANDLED] " if browser_skips else ""
                if browser_skips:
                    browser_handled_count += 1
                h_lines.append(
                    f"    {marker}<{h.get('tag','?')} role=\"{h.get('role','')}\" "
                    f"tabindex={h.get('tabindex')!r} "
                    f"tab_reachable={h.get('tab_reachable')}> "
                    f"name=\"{h.get('accessible_name') or ''}\" "
                    f"reasons={reasons} "
                    f"sel={h.get('selector','?')}"
                )
            parts.append(
                f"ANDI HIDDEN-CONTENT AUDIT ({len(h_lines)} focusable-"
                f"but-hidden elements; {browser_handled_count} are "
                f"[BROWSER-HANDLED] — deterministic, reject any finding "
                f"contradicting a state recorded here):\n"
                f"  RULE: entries marked [BROWSER-HANDLED] are NOT in "
                f"any browser's tab order — the focus order skips them. "
                f"Cookie banners, preference modals, and tracking pixels "
                f"with display:none / visibility:hidden / inert / "
                f"rect=0x0 fall in this category. DO NOT emit findings "
                f"for [BROWSER-HANDLED] entries under SC 2.1.1, 2.1.2, "
                f"2.1.3, 2.4.3, 2.4.7, 4.1.2, or 1.3.1 — the focus leak the "
                f"finding would describe does not actually exist. The "
                f"only legitimate finding for these is INFO-severity, "
                f"and only when the auditor specifically asks about "
                f"hidden-content management practices.\n"
                + "\n".join(h_lines)
            )

        # 19d. ANDI graphics audit (gANDI). Per-image accessibility
        # state including SVG <title>/<desc>/role and link/button
        # context. Use to corroborate or reject AI claims about
        # alt text, image-only links, decorative classification.
        andi_graphics = getattr(capture_data, "andi_graphics_results", None) or []
        if andi_graphics and self.criterion_id in self._ANDI_GRAPHICS_SCS:
            g_lines = []
            for g in andi_graphics:
                if not isinstance(g, dict):
                    continue
                gtype = g.get("type", "?")
                if gtype == "bg-image":
                    g_lines.append(
                        f"    [bg-image] sel={g.get('selector','?')} "
                        f"src={(g.get('src','') or '')!r} "
                        f"text_overlay={g.get('has_text_overlay')} "
                        f"overlay_text=\"{g.get('text_overlay_text','')}\""
                    )
                else:
                    g_lines.append(
                        f"    [{gtype}] sel={g.get('selector','?')} "
                        f"alt={g.get('alt')!r} "
                        f"aria_label=\"{g.get('aria_label','')}\" "
                        f"role=\"{g.get('role','')}\" "
                        f"aria_hidden={g.get('aria_hidden')} "
                        f"decorative={g.get('decorative')} "
                        f"name_source={g.get('name_source','none')} "
                        f"name=\"{g.get('accessible_name') or ''}\" "
                        f"in_link={g.get('in_link_or_button')} "
                        f"anc_tag={g.get('ancestor_tag','')!r} "
                        f"anc_other_text={g.get('ancestor_has_other_text')} "
                        f"svg_title=\"{g.get('svg_title','')}\""
                    )
            parts.append(
                f"ANDI GRAPHICS AUDIT ({len(g_lines)} graphics — "
                f"deterministic per-image accessibility state, "
                f"reject any finding contradicting a state recorded "
                f"here):\n"
                + "\n".join(g_lines)
            )

        # 19e. ANDI tables audit (tANDI). Per-table classification +
        # structural validation. Use to corroborate or reject AI
        # claims about table semantics.
        andi_tables = getattr(capture_data, "andi_tables_results", None) or []
        if andi_tables and self.criterion_id in self._ANDI_TABLES_SCS:
            t_lines = []
            for t in andi_tables:
                if not isinstance(t, dict):
                    continue
                t_lines.append(
                    f"    {t.get('classification','?'):>9} "
                    f"{t.get('row_count','?')}x{t.get('col_count','?')} "
                    f"role=\"{t.get('role','')}\" "
                    f"caption=\"{t.get('caption_text') or ''}\" "
                    f"th={t.get('th_count','?')}/{t.get('th_with_scope_count','?')}_with_scope "
                    f"cells_w_headers={t.get('cells_with_headers_attr','?')} "
                    f"hdr_refs_valid={t.get('headers_id_pairs_valid')} "
                    f"nested={t.get('nested')} "
                    f"issues={t.get('issues') or []} "
                    f"sel={t.get('selector','?')}"
                )
            parts.append(
                f"ANDI TABLES AUDIT ({len(t_lines)} tables — "
                f"deterministic data-vs-layout classification + "
                f"scope/headers validation, reject any finding "
                f"contradicting a state recorded here):\n"
                + "\n".join(t_lines)
            )

        # 19f. ANDI links/buttons audit (lANDI). Per-control accessible
        # name vs visible text + ambiguous text detection. Use to
        # corroborate or reject AI claims about link purpose, button
        # naming, and Label-in-Name compliance.
        # 19g. Keyboard roundtrip probe — every probable trigger
        # (button, role=button, summary, hash-link, etc.) tested with
        # Enter / Space / Tab / Escape / Shift+Tab. Use to corroborate
        # or reject AI claims about keyboard operability, dismissibility,
        # and focus-return-to-trigger.
        kb_rt = getattr(capture_data, "keyboard_roundtrip_results", None) or []
        if kb_rt and self.criterion_id in _KEYBOARD_ROUNDTRIP_SCS:
            kb_lines = []
            for r in kb_rt:
                if not isinstance(r, dict):
                    continue
                kb_lines.append(
                    f"    [{r.get('tag','?')}] sel={r.get('selector','?')} "
                    f"text=\"{r.get('text') or ''}\" "
                    f"opens_enter={r.get('opens_on_enter')} "
                    f"opens_space={r.get('opens_on_space')} "
                    f"target=\"{r.get('opened_target_selector','')}\" "
                    f"tab_inside={r.get('tab_stays_inside')} "
                    f"esc_closes={r.get('escape_closes')} "
                    f"focus_returns={r.get('focus_returns_to_trigger')} "
                    f"shift_tab_back={r.get('shift_tab_exits_cleanly')}"
                )
            parts.append(
                f"KEYBOARD ROUNDTRIP PROBE ({len(kb_lines)} triggers — "
                f"behavior-verified Enter/Space/Tab/Escape/Shift+Tab "
                f"per trigger, reject any finding contradicting a "
                f"state recorded here):\n"
                + "\n".join(kb_lines)
            )

        andi_inter = getattr(capture_data, "andi_interactive_results", None) or []
        if andi_inter and self.criterion_id in self._ANDI_INTERACTIVE_SCS:
            # Cross-evidence: ANDI's accessible-name detector does NOT
            # honour visually-hidden text spans (sr-only / screen-reader-
            # text / visually-hidden). axe-core's button-name rule does.
            # When ANDI says has_no_name=True but axe button-name PASSES
            # the same selector, the element DOES have an accessible name
            # and ANDI's flag is unreliable. Surface this inline so the
            # judge sees both signals on the same line, not just ANDI's.
            # Verified bug (a university SC 4.1.2: ANDI flagged
            # .video-control--play-pause and .modal as no_name=True; axe
            # button-name PASSED both; the prompt surfaced only ANDI's
            # wrong flag and the judge followed it).
            axe_results = getattr(capture_data, "axe_results", None) or {}
            from functions.axe_extract import (
                accessible_name_corroboration, axe_confirms_named,
            )
            _name_summary = accessible_name_corroboration(axe_results)

            i_lines = []
            for r in andi_inter:
                if not isinstance(r, dict):
                    continue
                sel = r.get('selector', '?')
                cross = ""
                if r.get('has_no_name') and axe_confirms_named(
                    _name_summary, r.get('tag') or r.get('type') or '', sel
                ):
                    cross = (
                        "  [AXE NAME-RULE PASS for this element type — "
                        "ANDI's no_name=True is UNRELIABLE here (ANDI does "
                        "not honour sr-only / clip / screen-reader-text label "
                        "spans). axe's link-name/button-name/image-alt rule "
                        "verified the element HAS an accessible name. Do NOT "
                        "emit a 'no accessible name' finding on this selector.]"
                    )
                i_lines.append(
                    f"    [{r.get('type','?')}] sel={sel} "
                    f"visible=\"{r.get('visible_text') or ''}\" "
                    f"name=\"{r.get('accessible_name') or ''}\" "
                    f"src={r.get('name_source','none')!r} "
                    f"name_inc_visible={r.get('name_includes_visible')} "
                    f"mismatch={r.get('name_visible_mismatch')} "
                    f"ambiguous={r.get('is_ambiguous')} "
                    f"no_name={r.get('has_no_name')} "
                    f"image_only={r.get('image_only')}"
                    + cross
                )
            parts.append(
                f"ANDI INTERACTIVE AUDIT ({len(i_lines)} links/buttons "
                f"— deterministic accessible-name vs visible-text "
                f"comparison + ambiguous text detection). "
                f"Reject any finding contradicting a state recorded here. "
                f"For SC 2.5.3 specifically: when name_inc_visible=True "
                f"AND mismatch=False, the visible text IS part of the "
                f"accessible name -- htmlcs F96 warnings on that element "
                f"are known false-positives (htmlcs over-fires when "
                f"aria-labelledby is used even though the resolved name "
                f"correctly contains the visible label). Reject SC 2.5.3 "
                f"failure findings on selectors with name_inc_visible=True "
                f"under any source tag.\n"
                + "\n".join(i_lines)
            )

        # 20. Non-text contrast (SC 1.4.11 evidence).
        # Each line lists a UI-component / graphical-element ratio
        # measured against its background. A `ratio=None` entry means
        # the underlying colour walk landed on a fallback (image,
        # gradient, or transparent ancestor) and the ratio could not be
        # computed deterministically -- treat those as manual-review,
        # NOT as an evidence basis for a numeric claim.
        nontext_contrast = getattr(capture_data, "nontext_contrast", None) or []
        if nontext_contrast:
            nc_lines = []
            for entry in nontext_contrast:
                if not isinstance(entry, dict):
                    continue
                sel = entry.get("selector", "?")
                ratio = entry.get("contrast_ratio")
                nc_lines.append(f"    {sel}: ratio={ratio}")
            parts.append(
                f"NON-TEXT CONTRAST ({len(nc_lines)} UI-component / "
                f"graphical-element measurements -- deterministic). "
                f"NO-FABRICATION RULE for SC 1.4.11: cite a numeric "
                f"contrast ratio for a selector ONLY when that exact "
                f"selector appears in this list with a non-None ratio. "
                f"Do NOT borrow numbers from the FOCUS CONTRAST, PIXEL "
                f"CONTRAST, or ANDI CONTRAST blocks -- those measure "
                f"different things (focus indicators, sampled pixels, "
                f"text vs background) and are NOT the UI-component-vs-"
                f"adjacent-colour ratio that SC 1.4.11 evaluates. Entries "
                f"with ratio=None mean the colour walk failed; flag those "
                f"qualitatively for manual review, do not invent a number.\n"
                + "\n".join(nc_lines)
            )

        # 20. ARIA validation issues (from functions.aria_validator)
        aria_issues = getattr(capture_data, "aria_issues", None) or []
        if aria_issues:
            ai_lines = []
            for issue in aria_issues:
                if not isinstance(issue, dict):
                    continue
                sel = issue.get("element_selector", "?")
                attr = issue.get("attribute", "")
                desc = issue.get("issue", "")
                sev = issue.get("severity", "")
                ai_lines.append(f"    [{sev}] {sel} ({attr}): {desc}")
            parts.append(
                f"ARIA VALIDATION ({len(ai_lines)} deterministic spec "
                f"violations):\n" + "\n".join(ai_lines)
            )

        # 21. Capture completions (what tests ran vs failed)
        completions = getattr(capture_data, "capture_completions", None) or {}
        if completions:
            comp_lines = []
            for name, status in completions.items():
                if isinstance(status, list):
                    status_str = f"{len(status)} entries: {status}"
                else:
                    status_str = str(status)
                comp_lines.append(f"    {name}: {status_str}")
            parts.append(
                "CAPTURE COMPLETIONS (which tests ran; non-'ok' statuses "
                "mean the AI did not see this signal -- do not invent "
                "verdicts based on missing data):\n" + "\n".join(comp_lines)
            )

        # 22. Cross-origin iframe blocks (same list rendered in capture_completions
        # but called out separately so the judge can emit a needs_manual_review
        # finding for SCs that depend on iframe content)
        blocked_iframes = completions.get("cross_origin_iframes_blocked") or []
        if blocked_iframes:
            parts.append(
                f"CROSS-ORIGIN IFRAMES (not traversable — tool could not "
                f"read their contents; auditor must test these separately):\n"
                + "\n".join(f"    {u}" for u in blocked_iframes)
            )

        # 23. Iframe contents (same-origin iframes the tool DID extract)
        iframe_contents = (
            getattr(capture_data, "user_context", {}) or {}
        ).get("iframe_contents") or []
        if iframe_contents:
            ic_lines = []
            for entry in iframe_contents:
                if not isinstance(entry, dict):
                    continue
                title = entry.get("title") or "(no title)"
                url = entry.get("url", "?")
                html_len = len(entry.get("html", "") or "")
                ic_lines.append(
                    f"    \"{title}\" -> {url} ({html_len} chars of HTML extracted)"
                )
            parts.append(
                f"IFRAME CONTENTS ({len(ic_lines)} same-origin iframes "
                f"extracted):\n" + "\n".join(ic_lines)
            )

        # 24. Text spacing overflow (SC 1.4.12).
        # Emit unconditionally for SC 1.4.12 so a "Supports" verdict is
        # an EXPLICIT measured pass (no overflow detected after the WCAG
        # spacing override was applied), not a default-pass from an
        # empty data block. The injected CSS sets the required line-
        # height, letter-spacing, word-spacing, and paragraph-spacing
        # values; the JS walk then enumerates every element whose
        # content was clipped (overflow:hidden + scrollWidth/scrollHeight
        # exceeded clientWidth/clientHeight). The screenshot saved at
        # captures/text_spacing_override.png is the visual ground truth.
        tso = getattr(capture_data, "text_spacing_overflow", None) or []
        tso_attr = hasattr(capture_data, "text_spacing_overflow")
        if tso:
            tso_lines = []
            for entry in tso:
                if not isinstance(entry, dict):
                    continue
                sel = entry.get("selector", "?")
                issue = entry.get("issue") or entry.get("type", "?")
                sw = entry.get("scrollWidth")
                cw = entry.get("clientWidth")
                sh = entry.get("scrollHeight")
                ch = entry.get("clientHeight")
                ov = entry.get("overflow")
                tso_lines.append(
                    f"    {sel}: {issue} "
                    f"scrollWidth={sw} clientWidth={cw} "
                    f"scrollHeight={sh} clientHeight={ch} overflow={ov}"
                )
            parts.append(
                f"TEXT SPACING OVERFLOW ({len(tso_lines)} elements clipped "
                f"after the WCAG 1.4.12 spacing override was applied: "
                f"line-height >= 1.5x font-size, letter-spacing >= 0.12em, "
                f"word-spacing >= 0.16em, paragraph-spacing >= 2x font-size). "
                f"These are deterministic measurements -- reject any AI "
                f"finding contradicting them. The visual ground truth is "
                f"saved at captures/text_spacing_override.png. NO-FABRICATION "
                f"RULE: cite a clipping or overflow problem for SC 1.4.12 "
                f"ONLY for selectors in this list; describe other concerns "
                f"qualitatively as judge_inference, not as a measured "
                f"failure.\n" + "\n".join(tso_lines)
            )
        elif tso_attr and self.criterion_id == "1.4.12":
            # Empty list AND the capture actually ran (attribute exists).
            # Make the negative result explicit so the judge sees "PASS"
            # rather than missing data.
            parts.append(
                "TEXT SPACING OVERFLOW: 0 elements clipped after the WCAG "
                "1.4.12 spacing override was applied (line-height >= 1.5x "
                "font-size, letter-spacing >= 0.12em, word-spacing >= "
                "0.16em, paragraph-spacing >= 2x font-size). This is a "
                "deterministic PASS: every text container kept its content "
                "visible with the required spacing. The visual ground truth "
                "is saved at captures/text_spacing_override.png. Do NOT "
                "emit SC 1.4.12 failure findings without an entry in this "
                "(currently empty) list."
            )

        # 25. Overflow at 200% zoom
        overflow_200 = getattr(capture_data, "overflow_200pct", None) or []
        if overflow_200:
            ov_lines = []
            for entry in overflow_200:
                if not isinstance(entry, dict):
                    continue
                sel = entry.get("selector", "?")
                ov_lines.append(f"    {sel}")
            parts.append(
                f"OVERFLOW AT 200% ZOOM ({len(ov_lines)}):\n"
                + "\n".join(ov_lines)
            )

        # 26. Overflow at 320px viewport
        overflow_320 = getattr(capture_data, "overflow_320px", None) or []
        if overflow_320:
            ov_lines = []
            for entry in overflow_320:
                if not isinstance(entry, dict):
                    continue
                sel = entry.get("selector", "?")
                ov_lines.append(f"    {sel}")
            parts.append(
                f"OVERFLOW AT 320PX ({len(ov_lines)}):\n"
                + "\n".join(ov_lines)
            )
        if getattr(capture_data, "horizontal_scroll_320", False):
            parts.append(
                "HORIZONTAL SCROLL AT 320PX: YES (SC 1.4.10 violation)"
            )

        # 26b. Position fixed / sticky / absolute enumeration (SC 1.4.10).
        # Reads capture_data.positioned_elements -- a deterministic scan
        # (web_capture.py) of every element whose computed `position` is
        # fixed/sticky/absolute, with selector + position + rect. The judge
        # was inventing `position: fixed` claims and laundering them under
        # source=htmlcs (verified on fairfaxva.gov run
        # 20260514_205147_cb3b646c SC 1.4.10). This block is the ground
        # truth; the claim validator rejects position assertions absent
        # from it.
        positioned = getattr(capture_data, "positioned_elements", None) or []
        fixed_sticky = [
            p for p in positioned
            if isinstance(p, dict) and (p.get("position") or "").lower() in ("fixed", "sticky")
        ]
        if fixed_sticky:
            pos_lines = [
                f"    {p.get('selector', '?')}: position={p.get('position')}"
                for p in fixed_sticky
            ]
            parts.append(
                f"POSITION FIXED / STICKY ELEMENTS ({len(pos_lines)} "
                f"— deterministic computed-style scan of every element on "
                f"the page). For SC 1.4.10, the ONLY elements that may be "
                f"flagged as causing reflow / overlap problems via fixed "
                f"positioning are the ones in this list. NO-FABRICATION "
                f"RULE: do NOT cite `position: fixed` or `position: sticky` "
                f"for any selector not in this list under any source tag. "
                f"If you want to flag a reflow concern about an element "
                f"absent from this list, label it `judge_inference` and "
                f"describe the visible behaviour qualitatively rather than "
                f"asserting a CSS property.\n"
                + "\n".join(pos_lines)
            )
        elif self.criterion_id == "1.4.10":
            # The scan ran (positioned_elements is always populated by
            # web_capture, even if empty) and found no fixed/sticky
            # elements. State that explicitly so a Supports verdict is
            # well-grounded.
            parts.append(
                "POSITION FIXED / STICKY ELEMENTS: 0 detected by the "
                "deterministic full-page computed-style scan. No element "
                "on the page uses position:fixed or position:sticky. Do "
                "NOT claim `position: fixed` or `position: sticky` for any "
                "selector under any source tag."
            )

        # 27. Reduced motion capture (SC 2.3.3)
        reduced_motion = getattr(capture_data, "reduced_motion", None) or {}
        if reduced_motion:
            rm_lines = [f"    {k}: {v}" for k, v in reduced_motion.items()]
            if rm_lines:
                parts.append("REDUCED MOTION:\n" + "\n".join(rm_lines))

        # 28. Hover content (SC 1.4.13).
        # The deterministic probe records `reveals=False` when the
        # Phase 2 hover/focus sequence observed NO new content appear
        # at the element. The visual_ai run sometimes claims the
        # opposite from a screenshot alone -- usually because the
        # screenshot caught a CSS :hover state the explorer-mode probe
        # didn't trigger. The judge then took visual_ai's word over its
        # own deterministic data (verified on fairfaxva.gov run
        # 20260514_205147_cb3b646c SC 1.4.13). For SC 1.4.13 we tell
        # the judge to reject hover-content failure findings against
        # any selector marked reveals=False, since dismissibility /
        # hoverability / persistence questions are moot when nothing
        # was observed to reveal.
        hover_content = getattr(capture_data, "hover_content", None) or []
        if hover_content:
            hc_lines = []
            for entry in hover_content:
                if not isinstance(entry, dict):
                    continue
                sel = entry.get("selector", "?")
                has_content = entry.get("revealed_content", False) or entry.get("reveals", False)
                dismissible = entry.get("dismissible")
                persistent = entry.get("persistent")
                hc_lines.append(
                    f"    {sel}: reveals={has_content} "
                    f"dismissible={dismissible} persistent={persistent}"
                )
            parts.append(
                f"HOVER/FOCUS CONTENT ({len(hc_lines)} -- deterministic "
                f"Phase 2 hover/focus probe). For SC 1.4.13: when "
                f"reveals=False the probe observed NO hover/focus-triggered "
                f"content reveal, so SC 1.4.13 dismissibility / hoverability "
                f"/ persistence questions DO NOT APPLY to that selector. "
                f"Reject any visual_ai or judge_inference finding claiming "
                f"a 1.4.13 failure (popup not dismissible, etc.) against a "
                f"selector marked reveals=False here; the deterministic "
                f"measurement is authoritative.\n"
                + "\n".join(hc_lines)
            )

        # 29. Expanded tab walks (menu/dropdown items reachable via arrow keys)
        etw = getattr(capture_data, "expanded_tab_walks", None) or {}
        if etw:
            etw_lines = []
            for trigger, items in etw.items():
                etw_lines.append(
                    f"    {trigger} -> {len(items)} reachable via arrow keys"
                )
                for item in items:
                    if isinstance(item, dict):
                        sel = item.get("selector", "?")
                        text = item.get("text", "")
                        etw_lines.append(f"        {sel} \"{text}\"")
            parts.append(
                "EXPANDED TAB WALKS (elements reachable inside menus/"
                "dropdowns after Enter):\n" + "\n".join(etw_lines)
            )

        # 30. Tab coverage summary (Option A deterministic buckets)
        #
        # When the forward Tab walk reaches MAX_TAB_ITERATIONS without
        # returning to <body> -- the SPA re-renders focusable elements
        # on every Tab -- reached_by_tab / coverage_percent become a
        # LOWER BOUND, not ground truth. The judge must not derive a
        # violation percentage from those numbers in that case.
        #
        # The focusable_but_skipped and not_focusable_at_all buckets
        # are ALWAYS authoritative: they come from an
        # element.focus() probe against every interactive element in
        # the DOM, which is deterministic and independent of the Tab
        # walk. Violations for SC 2.1.1 / 2.4.3 / 2.4.11 should
        # reference those buckets exclusively.
        tab_coverage = getattr(capture_data, "tab_coverage", None) or {}
        if tab_coverage:
            walk_truncated = bool(tab_coverage.get("walk_truncated"))
            tc_lines = [
                f"    total_interactive: {tab_coverage.get('total_interactive', '?')}",
                f"    reached_by_tab: {tab_coverage.get('reached_by_tab', '?')}"
                + ("  (LOWER BOUND -- walk truncated at cap)" if walk_truncated else ""),
                f"    coverage_percent: {tab_coverage.get('coverage_percent', '?')}"
                + ("  (UNRELIABLE -- walk truncated, do NOT derive a violation ratio from this)" if walk_truncated else ""),
            ]
            if walk_truncated:
                tc_lines.append(
                    "    walk_truncated: true  -- the Tab walk hit its "
                    "iteration cap on an SPA that re-renders focusable "
                    "elements every Tab. Use ONLY the deterministic "
                    "focusable_but_skipped / not_focusable_at_all "
                    "buckets below when flagging violations."
                )
            focusable_but_skipped = tab_coverage.get("focusable_but_skipped") or []
            not_focusable = tab_coverage.get("not_focusable_at_all") or []
            roving_valid = tab_coverage.get("roving_tabindex_valid") or []
            custom_navigable = tab_coverage.get("custom_arrow_navigable") or []
            if custom_navigable:
                tc_lines.append(
                    f"    custom_arrow_navigable ({len(custom_navigable)} -- "
                    f"non-ARIA widgets where a real keyboard probe confirmed "
                    f"arrow-key navigation actually works; items_reached "
                    f"shows how many siblings were reachable in the coverage "
                    f"walk. NOT SC 2.1.1 violations):"
                )
                for e in custom_navigable:
                    sel = e.get("selector", "?") if isinstance(e, dict) else str(e)
                    reach = e.get("items_reached", "?") if isinstance(e, dict) else "?"
                    bi = e.get("bidirectional_ok", "?") if isinstance(e, dict) else "?"
                    tc_lines.append(
                        f"        {sel} (items_reached={reach}, "
                        f"bidirectional={bi})"
                    )
            if roving_valid:
                tc_lines.append(
                    f"    roving_tabindex_valid ({len(roving_valid)} -- "
                    f"elements in ARIA composite widgets (radiogroup, "
                    f"tablist, menu, tree, listbox, grid, toolbar); "
                    f"arrow-key reachable through a tab-focusable "
                    f"sibling; NOT SC 2.1.1 violations):"
                )
                for e in roving_valid:
                    sel = e.get("selector", "?") if isinstance(e, dict) else str(e)
                    role = (e.get("role") or "").strip() if isinstance(e, dict) else ""
                    grp = e.get("group_size", "?") if isinstance(e, dict) else "?"
                    tc_lines.append(
                        f"        {sel} (role={role or 'native'}, group_size={grp})"
                    )
            if focusable_but_skipped:
                tc_lines.append(
                    f"    focusable_but_skipped ({len(focusable_but_skipped)} "
                    f"-- element.focus() WORKS but Tab order skips them; "
                    f"authoritative SC 2.1.1 finding):"
                )
                for e in focusable_but_skipped:
                    sel = e.get("selector", "?") if isinstance(e, dict) else str(e)
                    tc_lines.append(f"        {sel}")
            if not_focusable:
                tc_lines.append(
                    f"    not_focusable_at_all ({len(not_focusable)} -- "
                    f"element.focus() does nothing, element is inert; "
                    f"authoritative SC 2.1.1 finding):"
                )
                for e in not_focusable:
                    sel = e.get("selector", "?") if isinstance(e, dict) else str(e)
                    tc_lines.append(f"        {sel}")
            if not focusable_but_skipped and not not_focusable:
                tc_lines.append(
                    "    focusable_but_skipped: []  -- no deterministic "
                    "SC 2.1.1 candidates"
                )
                tc_lines.append(
                    "    not_focusable_at_all: []  -- no inert "
                    "interactive elements"
                )
            parts.append("TAB COVERAGE:\n" + "\n".join(tc_lines))

        # 31. Form errors (captured during form submission)
        form_errors = getattr(capture_data, "form_errors", None) or []
        if form_errors:
            fe_lines = []
            for err in form_errors:
                if not isinstance(err, dict):
                    continue
                sel = err.get("selector", "?")
                has_text = err.get("has_text_description", False)
                identifies = err.get("identifies_field", False)
                progr = err.get("programmatic_association", False)
                aria_live = err.get("has_aria_live", False) or err.get("has_role_alert", False)
                fe_lines.append(
                    f"    {sel}: text={has_text} identifies_field={identifies} "
                    f"programmatic_association={progr} live_region={aria_live}"
                )
            parts.append(
                f"FORM ERRORS ({len(fe_lines)}):\n" + "\n".join(fe_lines)
            )

        # 32. Context changes (SC 3.2.1 / 3.2.2)
        context_changes = getattr(capture_data, "context_changes", None) or []
        if context_changes:
            cc_lines = []
            for cc in context_changes:
                if not isinstance(cc, dict):
                    continue
                sel = cc.get("selector", "?")
                trigger = cc.get("trigger", "?")
                change = cc.get("change", "?")
                cc_lines.append(f"    {sel} on {trigger}: {change}")
            parts.append(
                f"CONTEXT CHANGES ({len(cc_lines)}):\n" + "\n".join(cc_lines)
            )

        # 33. Transcript verifications (SC 1.2.x)
        tv = getattr(capture_data, "transcript_verifications", None) or []
        if tv:
            tv_lines = []
            for entry in tv:
                if not isinstance(entry, dict):
                    continue
                sel = entry.get("selector", "?")
                verified = entry.get("verified")
                tv_lines.append(f"    {sel}: verified={verified}")
            parts.append(
                f"TRANSCRIPT VERIFICATIONS ({len(tv_lines)}):\n"
                + "\n".join(tv_lines)
            )

        # 34. Video-embed caption availability (YouTube timedtext probe)
        vec = getattr(capture_data, "video_embed_captions", None) or {}
        if vec:
            vec_lines = []
            for vid_id, info in vec.items():
                if not isinstance(info, dict):
                    continue
                vec_lines.append(
                    f"    {vid_id}: has_captions="
                    f"{info.get('has_captions', False)} "
                    f"languages={info.get('caption_languages', [])}"
                )
            parts.append(
                f"VIDEO EMBED CAPTIONS ({len(vec_lines)}):\n"
                + "\n".join(vec_lines)
            )

        # 35. Shadow DOM element counts (how much came from shadow roots)
        shadow_elements = getattr(capture_data, "shadow_elements", None) or []
        if shadow_elements:
            parts.append(
                f"SHADOW DOM: {len(shadow_elements)} elements merged into "
                f"the main inventories from shadow roots. These appear in the "
                f"headings/links/images/form_fields lists above with a "
                f"shadow_host key identifying the light-DOM host."
            )

        # 36. Widget keyboard interactions
        widget_keyboard = getattr(capture_data, "widget_keyboard", None) or []
        if widget_keyboard:
            wk_lines = []
            for entry in widget_keyboard:
                if not isinstance(entry, dict):
                    continue
                sel = entry.get("selector", "?")
                wtype = entry.get("type", "widget")
                keys_tested = entry.get("keys_tested") or []
                any_resp = entry.get("any_key_responded")
                probe_errored = widget_probe_errored(entry)
                all_items = entry.get("all_items_reached")
                items_count = entry.get("items_count", 0)
                distinct = entry.get("distinct_items_reached")
                ai_disc = entry.get("ai_discovered", False)
                source_tag = " [AI-DISCOVERED]" if ai_disc else ""

                status_parts = []
                if probe_errored:
                    status_parts.append(
                        "KEY-RESPONSE PROBE ERRORED — keys were not actually "
                        "tested on this widget; result is INCONCLUSIVE, NOT a "
                        "failure. Do NOT emit a keyboard finding from this entry"
                    )
                elif any_resp is False:
                    status_parts.append("NO KEYS RESPONDED — keyboard inaccessible")
                elif any_resp is True:
                    status_parts.append("keys responded")
                if all_items is False and items_count > 1:
                    status_parts.append(
                        f"only {distinct}/{items_count} items reached"
                    )
                elif all_items is True and items_count > 1:
                    status_parts.append(f"all {items_count} items reached")

                status = "; ".join(status_parts) or "tested"
                keys_str = ", ".join(keys_tested) if keys_tested else "(none)"
                wk_lines.append(
                    f"    {wtype}{source_tag} @ {sel}: "
                    f"keys=[{keys_str}] — {status}"
                )
            parts.append(
                f"WIDGET KEYBOARD TESTING ({len(wk_lines)}):\n"
                "  An entry marked 'PROBE ERRORED' is INCONCLUSIVE — the "
                "keyboard test did not execute. Never treat it as a failure; "
                "cross-check keyboard operability against the behaviour-"
                "verified KEYBOARD ROUNDTRIP PROBE instead.\n"
                + "\n".join(wk_lines)
            )

        # 37. Autoplay media probe (SC 1.4.2 / 2.2.2)
        audio_detection = getattr(capture_data, "audio_detection", None) or {}
        if audio_detection:
            ad_lines = []
            for k, v in audio_detection.items():
                if k.startswith("_raw_"):
                    continue
                ad_lines.append(f"    {k}: {v}")
            if ad_lines:
                parts.append(
                    "AUTOPLAY MEDIA PROBE (deterministic DOM query):\n"
                    + "\n".join(ad_lines)
                )

        # 39. Pseudo-element content (::before / ::after)
        pseudo = getattr(capture_data, "pseudo_elements", None) or []
        if pseudo:
            ps_lines = []
            for p in pseudo:
                if not isinstance(p, dict):
                    continue
                sel = p.get("selector", "?")
                content = p.get("content", "")
                pseudo_type = p.get("pseudo", "::before")
                ps_lines.append(
                    f"    {sel}{pseudo_type}: content={content!r}"
                )
            parts.append(
                f"PSEUDO-ELEMENT CONTENT ({len(ps_lines)}):\n"
                + "\n".join(ps_lines)
            )

        # 40. CAPTCHAs
        captchas = getattr(capture_data, "captchas", None) or []
        if captchas:
            cp_lines = []
            for c in captchas:
                if not isinstance(c, dict):
                    continue
                sel = c.get("selector", "?")
                captcha_type = c.get("type", "?")
                alt = c.get("alt", "") or c.get("aria_label", "")
                has_alt = c.get("has_alternative")
                cp_lines.append(
                    f"    {sel}: type={captcha_type} alt=\"{alt}\" "
                    f"has_alternative={has_alt}"
                )
            parts.append(
                f"CAPTCHAS ({len(cp_lines)}):\n" + "\n".join(cp_lines)
            )

        # 41. Flash analysis (SC 2.3.1)
        flash_analysis = getattr(capture_data, "flash_analysis", None) or {}
        if flash_analysis:
            fa_lines = [
                f"    has_violation: {flash_analysis.get('has_violation', False)}",
                f"    max_flashes_per_second: {flash_analysis.get('max_flashes_per_second', 0)}",
                f"    general_flash_violations: {flash_analysis.get('general_flash_violations', 0)}",
                f"    max_luminance_delta: {flash_analysis.get('max_luminance_delta', 0)}",
                f"    red_flash_events: {len(flash_analysis.get('red_flash_events', []) or [])}",
            ]
            parts.append("FLASH ANALYSIS (SC 2.3.1):\n" + "\n".join(fa_lines))

        # 42. Dynamic content (auto-advancing carousels, timers, etc.)
        dynamic_content = getattr(capture_data, "dynamic_content", None) or {}
        if dynamic_content:
            dc_lines = [f"    {k}: {v}" for k, v in dynamic_content.items()]
            if dc_lines:
                parts.append(
                    "DYNAMIC CONTENT (movement/timing):\n"
                    + "\n".join(dc_lines)
                )

        return "\n\n".join(parts)

    # Criteria where the AI needs to see the tab order explicitly
    _TAB_ORDER_CRITERIA = {"2.4.3", "2.4.7", "2.4.11", "2.1.1", "2.1.2"}
    _AUDIO_PROBE_CRITERIA = {"1.4.2", "2.2.2"}

    def _format_transaction_signals(
        self, capture_data: CaptureData
    ) -> list[str]:
        """Surface the mechanical facts the judge needs to decide whether
        SC 3.3.4 / 3.3.6 applies — i.e. whether this page causes a legal
        commitment, financial transaction, data modification, or test
        submission.

        AI-first principle: this block contains FACTS ONLY, enumerated in
        full with no truncation — every form field's literal type and
        autocomplete token, every action-control label, the page-type
        classification. It does NOT decide applicability. The judgment
        ("is this a money / legal / data-changing page") is a meaning
        determination and stays with the AI, made from these facts plus
        the attached screenshots. Prior failure: the judge marked SC
        3.3.6 "Does Not Support" for a site-search box on the
        fairfaxva.gov run 20260515_230613_ff643865 because no
        applicability evidence or criteria were in its prompt.
        """
        lines = ["[TRANSACTION SCOPE EVIDENCE for SC 3.3.4 / 3.3.6]"]

        page_type = (getattr(capture_data, "page_type", "") or "").strip()
        if not page_type:
            inv = getattr(capture_data, "element_inventory", None) or {}
            if isinstance(inv, dict):
                page_type = (inv.get("page_type") or "").strip()
        lines.append(f"PAGE TYPE (AI classification): {page_type or 'not classified'}")

        fields = capture_data.form_fields or []
        lines.append(
            f"FORM FIELDS ON PAGE: {len(fields)} "
            f"(full per-field detail is in the FORM FIELDS block above)"
        )
        # Literal type= facts — unambiguous, mechanical.
        pw = [f for f in fields if (f.get("type") or "").lower() == "password"]
        files = [f for f in fields if (f.get("type") or "").lower() == "file"]
        lines.append(
            f"  password-type fields: {len(pw)}"
            + (f" — {', '.join(f.get('selector', '?') for f in pw)}" if pw else "")
        )
        lines.append(
            f"  file-upload fields: {len(files)}"
            + (f" — {', '.join(f.get('selector', '?') for f in files)}" if files else "")
        )
        # Declared autocomplete tokens — spec-defined facts, verbatim.
        tokens = sorted({
            (f.get("autocomplete") or "").strip().lower()
            for f in fields if (f.get("autocomplete") or "").strip()
        })
        lines.append(
            f"  declared autocomplete tokens across all fields: "
            f"{', '.join(tokens) if tokens else 'none'}"
        )
        # Action / submit controls with their literal labels.
        controls: list[tuple[str, str]] = []
        for f in fields:
            if (f.get("type") or "").lower() in ("submit", "button", "image", "reset"):
                controls.append((
                    f.get("selector", "?"),
                    str(f.get("value") or f.get("text") or f.get("type") or ""),
                ))
        for entry in getattr(capture_data, "tab_walk", None) or []:
            if isinstance(entry, dict) and (entry.get("tag") or "").lower() == "button":
                controls.append((
                    entry.get("selector", "?"),
                    str(entry.get("text") or ""),
                ))
        if controls:
            lines.append("ACTION / SUBMIT CONTROLS (literal labels, full list):")
            for sel, label in controls:
                lines.append(f"    {sel}: \"{label}\"")
        else:
            lines.append("ACTION / SUBMIT CONTROLS: none detected")

        lines.append(
            "\nAPPLICABILITY — YOU MUST JUDGE THIS, do not assume. SC 3.3.4 "
            "applies ONLY to pages that cause a legal commitment, a "
            "financial transaction, a modification or deletion of the "
            "user's data, or the submission of test responses. SC 3.3.6 "
            "applies to pages that require the user to submit information. "
            "Using the fields above, the control labels, the FORM FIELDS "
            "block, and the attached screenshots, decide what this page "
            "actually does:\n"
            "  - If the only submissions are site search, content "
            "filtering, or newsletter / email signup, those are trivially "
            "repeatable and reversible. They do NOT cause a legal, "
            "financial, or data commitment: SC 3.3.4 is Not Applicable, "
            "and SC 3.3.6 must NOT be Does Not Support for them (a "
            "trivially reversible submission already satisfies the "
            "criterion).\n"
            "  - If the page carries a checkout, payment, account "
            "creation or deletion, legal agreement, booking, or similar "
            "consequential submission, evaluate whether it provides at "
            "least one of: reversible, checked for input errors, or "
            "confirmed before finalizing.\n"
            "Judge from what the page IS, per the evidence and "
            "screenshots — not from the mere presence of a <form>."
        )
        return lines

    def _format_target_size_measurements(
        self, capture_data: CaptureData
    ) -> list[str]:
        """Render rect dimensions + nearest-neighbor distances + per-element
        deterministic verdict for SC 2.5.8.

        Without this block, the judge sees screenshots where small
        horizontal-nav links look squished and writes "0px spacing"
        from intuition. With this block, every interactive target
        has its measured rect AND its nearest-neighbor center distance
        AND a PASS/FAIL annotation per the WCAG spacing exception. The
        model cannot legitimately claim a finding that contradicts the
        listed measurements — they are right there in the prompt.

        The target list comes from the single canonical source
        (capture_data.target_size_measurements, computed by
        functions.target_size) so this prompt block and the claim
        validator measure the same elements.
        """
        is_enhanced = self.criterion_id == "2.5.5"
        MIN = 44.0 if is_enhanced else 24.0
        sc = "2.5.5" if is_enhanced else "2.5.8"
        sc_name = (
            "Target Size (Enhanced)" if is_enhanced
            else "Target Size (Minimum)"
        )
        targets = getattr(capture_data, "target_size_measurements", None) or []
        if not targets:
            # Resilience: compute on the fly if the capture-time step
            # did not run (older capture, resumed review).
            from functions.target_size import compute_target_size_measurements
            targets = compute_target_size_measurements(capture_data)

        if not targets:
            return [
                f"[TARGET SIZE MEASUREMENTS for SC {sc}] No interactive "
                "targets captured. SC may be Not Applicable."
            ]

        def nearest_other(t: dict) -> tuple[float, str]:
            best = float("inf")
            bsel = ""
            for o in targets:
                if o is t:
                    continue
                d = ((t["cx"] - o["cx"]) ** 2 + (t["cy"] - o["cy"]) ** 2) ** 0.5
                if d < best:
                    best = d
                    bsel = (o.get("name") or o["selector"])
            return best, bsel

        lines = [
            f"[TARGET SIZE MEASUREMENTS for SC {sc} — DETERMINISTIC, "
            "REJECT FINDINGS THAT CONTRADICT THESE NUMBERS]",
            "",
            f"WCAG {sc} {sc_name} requires interactive targets to be at "
            f"least {MIN:.0f}x{MIN:.0f} CSS pixels, EXCEPT when:",
        ]
        if not is_enhanced:
            lines.append(
                f"  - Spacing: target's centre is >={MIN:.0f}px from the "
                f"centre of any other target (the spacing exception),"
            )
        lines += [
            "  - Inline: link is part of a sentence in a paragraph / list "
            "/ cell (the inline exception),",
            "  - Equivalent: an equivalent target meeting the size exists,",
            "  - User-agent control / Essential.",
        ]
        if is_enhanced:
            lines.append(
                "  NOTE: SC 2.5.5 has NO spacing exception — only the "
                "inline / equivalent / user-agent / essential exceptions "
                "apply. Wide spacing does NOT excuse an undersized target."
            )
        lines += [
            "",
            "Per-element measurements + per-element deterministic verdict:",
        ]
        pass_count = 0
        fail_count = 0
        for t in targets:
            w, h = t["width"], t["height"]
            sp, near_name = nearest_other(t)
            inline = t.get("is_inline", False)
            below_min = (w < MIN or h < MIN)
            if not below_min:
                verdict = f"PASS (size {w:.0f}x{h:.0f} >= {MIN:.0f})"
                pass_count += 1
            elif inline:
                verdict = "PASS (inline exception applies — link is in a sentence)"
                pass_count += 1
            elif (not is_enhanced) and sp >= MIN:
                verdict = (
                    f"PASS (size {w:.0f}x{h:.0f} below {MIN:.0f} but "
                    f"spacing exception applies — centre is {sp:.0f}px "
                    f"from nearest target \"{near_name}\", >={MIN:.0f}px required)"
                )
                pass_count += 1
            else:
                if is_enhanced:
                    detail = f"no exception applies (nearest target {sp:.0f}px away)"
                else:
                    detail = (
                        f"spacing exception fails — centre only {sp:.0f}px "
                        f"from nearest target \"{near_name}\""
                    )
                verdict = (
                    f"FAIL (size {w:.0f}x{h:.0f} below {MIN:.0f} AND {detail})"
                )
                fail_count += 1
            name = t.get("name") or t["selector"]
            lines.append(
                f"  {t['kind']} \"{name}\" rect={w:.0f}x{h:.0f} "
                f"centre=({t['cx']:.0f},{t['cy']:.0f})  -> {verdict}"
            )

        lines.append("")
        lines.append(
            f"DETERMINISTIC VERDICT: {pass_count} target(s) pass, "
            f"{fail_count} target(s) fail. The judge MUST NOT emit "
            f"findings against any target listed as PASS — its "
            f"measurements satisfy the SC. Findings are only legitimate "
            f"for FAIL entries above (if any)."
        )
        return lines

    def _format_link_styling_measurements(
        self, capture_data: CaptureData
    ) -> list[str]:
        """Delegates to functions.dom_format.format_link_styling_measurements."""
        from functions.dom_format import format_link_styling_measurements
        return format_link_styling_measurements(capture_data.links or [])

    def _format_motion_pause_controls(
        self, capture_data: CaptureData
    ) -> list[str]:
        """Detect visible buttons whose accessible name contains
        'pause' / 'stop' / 'play' and format them as a prompt block.

        SC 2.2.2 (Pause, Stop, Hide) is met when the page provides any
        visible single-pointer mechanism to pause/stop/hide moving
        content — that mechanism is typically a separate <button>
        next to the moving content (a university's #pauseHeroVid for the hero
        video and #play-pause-toggle for the carousel), NOT
        a native <video controls> bar. The audio_detection probe
        (has_audio_pause_button) only checks native controls; it
        misses these external buttons. Without this block the AI saw
        only the audio probe's False signal and falsely concluded the
        page had no pause mechanism, producing 2.2.2 false positives
        on a university homepage.

        Used by both ``get_image_context`` (visual_ai prompt) and
        ``_build_dom_context`` (judge prompt) so both stages see the
        same evidence.
        """
        pause_kw = ("pause", "stop", "play")
        detected: list[str] = []
        seen: set = set()

        def _name_of(n: dict) -> str:
            return (
                n.get("accessible_name") or n.get("text")
                or n.get("inner_text") or n.get("aria_label")
                or n.get("aria-label") or n.get("name") or ""
            ).strip()

        # Scan every collection that can hold an interactive control —
        # the pause/play button is often a custom <button> that the
        # narrow nontext_contrast list misses. Verified bug
        # (a university SC 2.2.2): a real "Play Video / Pause Video"
        # button existed but, being absent from nontext_contrast, was
        # never surfaced, so the judge concluded "no pause mechanism".
        _inv = getattr(capture_data, "element_inventory", None) or []
        if isinstance(_inv, dict):
            _inv = _inv.get("elements") or []
        for coll in (
            getattr(capture_data, "nontext_contrast", None) or [],
            _inv,
            capture_data.links or [],
            capture_data.form_fields or [],
        ):
            for n in coll:
                if not isinstance(n, dict):
                    continue
                name = _name_of(n)
                if not name:
                    continue
                name_l = name.lower()
                if not any(kw in name_l for kw in pause_kw):
                    continue
                sel = (n.get("selector") or "").strip()
                rect = n.get("rect") or {}
                try:
                    w = float(rect.get("width") or 0)
                    h = float(rect.get("height") or 0)
                except (ValueError, TypeError):
                    w = h = 0.0
                # When a rect is present, require it non-trivial; entries
                # with no rect are still kept (better surfaced than lost).
                if rect and not (w >= 8 and h >= 8):
                    continue
                key = (sel, name_l)
                if key in seen:
                    continue
                seen.add(key)
                if rect:
                    detected.append(
                        f"  - {sel}: name=\"{name}\" "
                        f"rect=({rect.get('x', 0) or 0:.0f},"
                        f"{rect.get('y', 0) or 0:.0f}) {w:.0f}x{h:.0f}"
                    )
                else:
                    detected.append(f"  - {sel}: name=\"{name}\"")

        out = []
        out.append("[DETECTED MOTION-PAUSE CONTROLS]")
        out.append(
            "Visible interactive elements whose accessible name "
            "contains 'pause' / 'stop' / 'play'. These are candidate "
            "WCAG 2.2.2 pause / stop / hide mechanisms. If any one "
            "of these is present and visibly available alongside the "
            "moving content, the page MEETS SC 2.2.2 for that "
            "moving content. Do NOT flag 'no pause mechanism' if "
            "this list is non-empty."
        )
        if detected:
            out.extend(detected)
        else:
            out.append(
                "  (none detected — page likely has no visible "
                "pause/stop/play control)"
            )
        return out

    def get_image_context(self, capture_data: CaptureData) -> str:
        """Return a text description of the images being sent to the AI.

        Includes context about exploration screenshots so the AI knows
        what each image shows (initial state, hover state, click state, etc).
        For focus/navigation criteria, includes the full tab order so the AI
        can compare it against the visual layout in the screenshot.
        """
        lines = []

        # For focus/keyboard criteria, include the tab order
        if self.criterion_id in self._AUDIO_PROBE_CRITERIA:
            probe = getattr(capture_data, "audio_detection", None) or {}
            lines.append("[GROUND TRUTH -- AUTOPLAY MEDIA PROBE (DETERMINISTIC)]")
            lines.append(
                "A Playwright JS probe queried every <audio>, <video>, and "
                "embedded media <iframe> on the page and read their runtime "
                "state (autoplay attr, muted, paused, current_time, duration, "
                "controls, visibility).\n"
                "For SC 1.4.2 (Audio Control): the summary below is AUTHORITATIVE. "
                "If has_autoplay_audio=False, do NOT flag the page as violating "
                "audio autoplay. A <video autoplay muted> is NOT a 1.4.2 violation "
                "because muted video plays no audio.\n"
                "For SC 2.2.2 (Pause, Stop, Hide): the audio probe is NOT "
                "sufficient. has_audio_pause_button=False only means there is "
                "no native <video controls> bar. SC 2.2.2 also accepts an "
                "external visible button labelled 'Pause' / 'Stop' / 'Play' "
                "that controls the moving content. Use the [DETECTED MOTION-"
                "PAUSE CONTROLS] block below for that signal."
            )
            lines.append(
                f"SUMMARY: has_autoplay_audio={probe.get('has_autoplay_audio', False)}, "
                f"duration_over_3s={probe.get('duration_over_3s', False)}, "
                f"audio_type={probe.get('audio_type', 'silence')}, "
                f"has_audio_pause_button={probe.get('has_pause_button', False)}"
            )
            raw_media = probe.get("_raw_media", []) or []
            raw_iframes = probe.get("_raw_iframes", []) or []
            if raw_media:
                lines.append(f"Media elements ({len(raw_media)}):")
                for m in raw_media:
                    lines.append(
                        f"  - {m.get('kind','?')} autoplay={m.get('autoplay_attr')} "
                        f"muted={m.get('muted')} paused={m.get('paused')} "
                        f"duration={m.get('duration')} controls={m.get('controls')} "
                        f"src={m.get('src') or ''}"
                    )
            if raw_iframes:
                lines.append(f"Media iframes ({len(raw_iframes)}):")
                for f in raw_iframes:
                    lines.append(
                        f"  - autoplay_param={f.get('autoplay_param')} "
                        f"muted_param={f.get('muted_param')} "
                        f"src={f.get('src') or ''}"
                    )
            if not raw_media and not raw_iframes:
                lines.append("No <audio>, <video>, or recognized embed iframe found on the page.")

            # SC 2.2.2 motion-pause control detection — see
            # _format_motion_pause_controls. Renders the same block for
            # both the visual_ai prompt (here) and the judge prompt
            # (in _build_dom_context).
            if self.criterion_id == "2.2.2":
                lines.extend(self._format_motion_pause_controls(capture_data))
            lines.append("")

        if self.criterion_id in self._TAB_ORDER_CRITERIA and capture_data.tab_walk:
            no_focus_count = sum(
                1 for tw in capture_data.tab_walk
                if not tw.get("has_visible_indicator")
            )
            lines.append("[GROUND TRUTH — DETERMINISTIC TAB WALK]")
            lines.append(
                f"Playwright pressed Tab {len(capture_data.tab_walk)} times and "
                f"recorded which element received focus, the computed focus "
                f"indicator (outline width/color, box-shadow, border change), "
                f"and a screenshot of each focused state. The [VISIBLE] / "
                f"[NO FOCUS] label below is THIS deterministic measurement, "
                f"not a guess. For SC 2.4.7 / 2.4.11, treat this list as "
                f"AUTHORITATIVE -- do not claim 'no visible focus indicator' "
                f"on any element labeled [VISIBLE] here. The tab walk has "
                f"already done the per-element comparison; you only need to "
                f"interpret it. Order in the list is the keyboard focus "
                f"sequence -- compare against the screenshot's visual reading "
                f"flow (left→right, top→bottom)."
            )
            lines.append(
                f"SUMMARY: {len(capture_data.tab_walk)} elements walked, "
                f"{no_focus_count} marked NO FOCUS, "
                f"{len(capture_data.tab_walk) - no_focus_count} marked VISIBLE."
            )
            lines.append("")
            for i, tw in enumerate(capture_data.tab_walk, 1):
                tag = tw.get("tag", "?")
                text = tw.get("text") or ""
                sel = tw.get("selector", "?")
                vis = "VISIBLE" if tw.get("has_visible_indicator") else "NO FOCUS"
                itype = tw.get("indicator_type", "?")
                lines.append(f"  Tab {i}: <{tag}> \"{text}\" [{vis}/{itype}] — {sel}")
            lines.append("")

        # Add exploration context for any screenshots being sent
        prefix = ".".join(self.criterion_id.split(".")[:2])
        relevant_types = self._CRITERION_ELEMENT_TYPES.get(prefix, set())

        for result in getattr(capture_data, "exploration_results", []) or []:
            if not isinstance(result, dict):
                continue
            elem_type = result.get("type", "")
            if relevant_types and elem_type not in relevant_types:
                continue
            selector = result.get("selector", "?")
            response = result.get("interaction_response", "none")
            observations = result.get("accessibility_observations", [])
            screenshots = result.get("screenshots", [])
            if screenshots:
                lines.append(f"ELEMENT: {selector} ({elem_type})")
                lines.append(f"  Interaction result: {response}")
                for ss in screenshots:
                    if isinstance(ss, dict):
                        lines.append(f"  Screenshot: {ss.get('state', '?')} — {ss.get('description', '')}")
                if observations:
                    for obs in observations:
                        lines.append(f"  Observation: {obs}")
                lines.append("")

        return "\n".join(lines) if lines else ""

    # Element types relevant to each criterion group for screenshot selection
    _CRITERION_ELEMENT_TYPES: dict[str, set[str]] = {
        "1.1": {"image", "background_image"},
        "1.2": {"media"},
        "1.3": {"heading", "landmark", "list", "table", "form_field"},
        "1.4": {"link", "button", "form_field", "image"},
        "2.1": {"button", "link", "form_field", "menu", "dropdown", "modal_trigger"},
        "2.4": {"heading", "link", "landmark", "skip_link"},
        "3.2": {"form_field", "link", "button"},
        "3.3": {"form_field"},
        "4.1": {"button", "link", "form_field", "custom_control"},
    }

    # Subclasses that only need to iterate list fields on CaptureData and
    # extract named path keys can declare them here instead of overriding
    # ``get_extra_images``. Each entry is (list_field, path_key) for a
    # flat list of dicts, or (list_field, sub_dict_key, path_key) for
    # nested recording/result dicts. The base ``get_extra_images`` reads
    # this list in addition to the universal zoom + Phase 2 exploration
    # screenshots.
    _SCREENSHOT_FIELDS: list[tuple[str, ...]] = []

    def _collect_declared_screenshot_paths(
        self, capture_data: CaptureData,
    ) -> list[str]:
        """Resolve the ``_SCREENSHOT_FIELDS`` declaration into real paths."""
        paths: list[str] = []
        for entry in self._SCREENSHOT_FIELDS:
            if len(entry) == 2:
                list_field, path_key = entry
                items = getattr(capture_data, list_field, None) or []
                if isinstance(items, list):
                    for item in items:
                        if isinstance(item, dict):
                            p = item.get(path_key, "")
                            if p:
                                paths.append(p)
            elif len(entry) == 3:
                list_field, sub_key, path_key = entry
                items = getattr(capture_data, list_field, None) or []
                if isinstance(items, list):
                    for item in items:
                        if not isinstance(item, dict):
                            continue
                        sub = item.get(sub_key, {})
                        if isinstance(sub, dict):
                            p = sub.get(path_key, "")
                            if p:
                                paths.append(p)
                        elif isinstance(sub, list):
                            for p in sub:
                                if isinstance(p, str) and p:
                                    paths.append(p)
        return paths

    # When False, the judge runs text-only for this SC even when images
    # are available. Default True: every SC that reaches the slow path
    # (i.e. is not PROGRAMMATIC_DEFINITIVE) benefits from the judge
    # cross-checking visual_ai's claims against the actual pixels.
    # Subclasses with purely text-driven semantics (e.g. document language
    # detection, page-title presence) can opt out for token economy.
    judge_uses_images: bool = True

    def _collect_judge_images(self, capture_data: CaptureData) -> list[str]:
        """Pixel evidence for the judge to verify visual_ai's claims.

        Returns the SAME set of images visual_ai received during this SC's
        slow-path run: full-page + viewport screenshots plus everything
        ``get_extra_images`` produced (zoom variants, exploration crops,
        per-image crops on _IMAGE_BOUND_SCS, declared list-field
        screenshots).

        Returning [] disables multimodal judging for this call --
        ``LLMClient.call_with_tools`` falls back to the text-only model
        path. Used by document-level SCs that opted out via
        ``judge_uses_images = False``.
        """
        if not self.judge_uses_images:
            return []

        images: list[str] = []
        if capture_data.full_page_path:
            images.append(capture_data.full_page_path)
        if capture_data.viewport_path:
            images.append(capture_data.viewport_path)

        # get_extra_images is the same call run_ai_analysis makes when
        # building the visual_ai prompt; reusing it keeps the judge's
        # evidence aligned with what visual_ai actually saw, so the judge
        # is verifying the same pixels visual_ai wrote about.
        try:
            extra = self.get_extra_images(capture_data) or []
        except Exception:
            logger.warning(
                "SC %s: get_extra_images raised while collecting judge "
                "images -- judge will see only base screenshots",
                self.criterion_id, exc_info=True,
            )
            extra = []

        # Dedupe while preserving order (full_page / viewport may also
        # appear inside get_extra_images on some subclasses).
        seen: set[str] = set()
        out: list[str] = []
        for p in images + extra:
            if not p or p in seen:
                continue
            seen.add(p)
            out.append(p)
        return out

    def get_extra_images(self, capture_data: CaptureData) -> list[str]:
        """Return additional screenshot paths for the AI to review.

        Base pipeline: universal zoom screenshots + Phase 2 exploration
        screenshots for criterion-relevant element types + any paths
        declared via ``_SCREENSHOT_FIELDS``. Subclasses with more exotic
        needs (mixing multiple list sources, filtering by state, etc.)
        still override this method.
        """
        paths: list[str] = []
        if capture_data.viewport_200pct_path:
            paths.append(capture_data.viewport_200pct_path)
        if capture_data.viewport_320px_path:
            paths.append(capture_data.viewport_320px_path)

        # Add Phase 2 exploration screenshots for relevant element types
        prefix = ".".join(self.criterion_id.split(".")[:2])
        relevant_types = self._CRITERION_ELEMENT_TYPES.get(prefix, set())

        for result in getattr(capture_data, "exploration_results", []) or []:
            if not isinstance(result, dict):
                continue
            elem_type = result.get("type", "")
            if relevant_types and elem_type not in relevant_types:
                continue
            # Add screenshots from this exploration
            for ss in result.get("screenshots", []):
                if isinstance(ss, dict) and ss.get("path"):
                    paths.append(ss["path"])
                elif isinstance(ss, str):
                    paths.append(ss)

        # Declared list-field screenshots (subclass-configurable)
        paths.extend(self._collect_declared_screenshot_paths(capture_data))

        # Per-image crops for image-bearing SCs. The order MUST match the
        # IMG-N / BG-N labels rendered by _build_dom_context (images
        # first, then background_images, both in capture_data list order)
        # so the model can bind "IMG-3 in the prompt text" to "the 3rd
        # image attached." Without this binding the model reverts to
        # visual guessing on cluttered pages and fails outright on CSS
        # background-images.
        if self.criterion_id in self._IMAGE_BOUND_SCS:
            for img in capture_data.images or []:
                cp = img.get("crop_path")
                if cp:
                    paths.append(cp)
            for bg in capture_data.background_images or []:
                cp = bg.get("crop_path")
                if cp:
                    paths.append(cp)

        return paths

    # ------------------------------------------------------------------
    # Programmatic check (MUST be overridden)
    # ------------------------------------------------------------------

    async def run_programmatic(
        self, capture_data: CaptureData
    ) -> tuple[ConformanceLevel, float, list[Finding]]:
        """Run deterministic / DOM-based checks.

        Returns (conformance_level, confidence, findings).
        """
        return ConformanceLevel.NOT_EVALUATED, 0.0, []

    # ------------------------------------------------------------------
    # AI analysis (override for checks that benefit from AI)
    # ------------------------------------------------------------------

    async def run_ai_analysis(
        self,
        capture_data: CaptureData,
        ai_client: Any,
        programmatic_data: dict,
    ) -> tuple[ConformanceLevel, float, list[Finding]]:
        """Run AI-based analysis.

        Returns (conformance_level, confidence, findings).
        """
        from functions.prompt import (
            build_system_prompt,
            build_user_prompt,
            format_elements_for_prompt,
        )

        # Flatten effective off_scope_keywords (auto + check-specific) into a single list
        effective_off_scope = self._get_effective_off_scope_keywords()
        off_scope_kw_list: list[str] = []
        if effective_off_scope:
            for keywords in effective_off_scope.values():
                if isinstance(keywords, list):
                    off_scope_kw_list.extend(keywords)

        # Build dynamic page context hint (Feature D)
        from functions.prompt import build_page_context_hint
        context_hint = build_page_context_hint(capture_data)

        # Load product context if available on capture_data
        _product_ctx = getattr(capture_data, "product_context", None)

        system_prompt = build_system_prompt(
            criterion_id=self.criterion_id,
            criterion_name=self.criterion_name,
            level=self.level,
            normative_text=self.normative_text,
            ict_baseline=self.ict_baseline,
            off_scope_keywords=off_scope_kw_list or None,
            page_context_hint=context_hint,
            product_context=_product_ctx,
        )

        elements = format_elements_for_prompt(capture_data, self.criterion_id)

        # --- Bounding-box annotation (graceful if Pillow missing) ---
        _annotated_path: str | None = None
        try:
            from analysis.image_annotator import annotate_screenshot, assign_box_labels
            assign_box_labels(elements)
            if capture_data.full_page_path:
                from pathlib import Path as _AnnotPath
                _captures_dir = str(
                    _AnnotPath(capture_data.review_dir) / "captures"
                ) if capture_data.review_dir else None
                _annotated_path = annotate_screenshot(
                    capture_data.full_page_path,
                    elements,
                    self.criterion_id,
                    _captures_dir,
                )
        except Exception:
            # Pillow not installed or annotator not available — continue
            # with clean screenshots only.
            pass

        page_context = {
            "url": capture_data.url,
            "title": capture_data.title,
            "file_type": capture_data.file_type,
            "review_type": getattr(capture_data, "review_type", "single"),
        }

        # Load criterion-specific prompt template if available
        criterion_guidance = ""
        try:
            from prompts import load_criterion_prompt
            prompt_template = load_criterion_prompt(self.criterion_id)
            if prompt_template:
                parts = []
                visual = prompt_template.get("visual_checks", [])
                if visual:
                    parts.append("WHAT TO LOOK FOR:\n" + "\n".join(
                        f"  - {v}" for v in visual
                    ))
                # Conceptual scaffolding (new fields, 2026-04-15 refactor).
                # These render BEFORE pass/fail so the model has the
                # meaning + scope in mind before evaluating conditions.
                plain_meaning = prompt_template.get("plain_meaning")
                if plain_meaning:
                    parts.append(f"PLAIN MEANING\n{plain_meaning}")
                scope_in = prompt_template.get("scope_applies_to", [])
                if scope_in:
                    parts.append(
                        f"APPLIES TO (this criterion covers):\n"
                        + "\n".join(f"  - {s}" for s in scope_in)
                    )
                scope_out = prompt_template.get("scope_does_not_apply_to", [])
                if scope_out:
                    parts.append(
                        f"DOES NOT APPLY TO (out of scope for this criterion):\n"
                        + "\n".join(f"  - {s}" for s in scope_out)
                    )
                borderline = prompt_template.get("borderline_calibration", [])
                if borderline:
                    parts.append(
                        f"BORDERLINE CALIBRATION (how strict to be):\n"
                        + "\n".join(f"  - {b}" for b in borderline)
                    )
                pass_c = prompt_template.get("pass_conditions", [])
                if pass_c:
                    parts.append("SUPPORTS (pass) when:\n" + "\n".join(
                        f"  - {p}" for p in pass_c
                    ))
                fail_c = prompt_template.get("fail_conditions", [])
                if fail_c:
                    parts.append("DOES NOT SUPPORT (fail) when:\n" + "\n".join(
                        f"  - {f}" for f in fail_c
                    ))
                na_c = prompt_template.get("na_conditions", [])
                if na_c:
                    parts.append("NOT APPLICABLE when:\n" + "\n".join(
                        f"  - {n}" for n in na_c
                    ))
                mistakes = prompt_template.get("common_mistakes", [])
                if mistakes:
                    parts.append("COMMON MISTAKES TO WATCH FOR:\n" + "\n".join(
                        f"  - {m}" for m in mistakes
                    ))
                anti_patterns = prompt_template.get("auditor_anti_patterns", [])
                if anti_patterns:
                    parts.append(
                        "AUDITOR ANTI-PATTERNS (do NOT report these false positives):\n"
                        + "\n".join(f"  - {a}" for a in anti_patterns)
                    )
                # off_scope_topics -- per-SC rejection list. Prefer JSON
                # field, fall back to the central _OFF_SCOPE_KEYWORDS map.
                off_scope = prompt_template.get("off_scope_topics")
                if not off_scope:
                    try:
                        from functions.prompt import get_off_scope_keywords
                        off_scope = get_off_scope_keywords(self.criterion_id)
                    except Exception:
                        off_scope = []
                if off_scope:
                    parts.append(
                        f"OFF-SCOPE TOPICS FOR {self.criterion_id} "
                        f"(REJECT findings about these -- they belong to a different SC):\n"
                        + "\n".join(f"  - {t}" for t in off_scope)
                    )
                examples = prompt_template.get("examples", {})
                if examples:
                    ex_parts = []
                    for k, v in examples.items():
                        ex_parts.append(f"  {k.upper()}: {v}")
                    parts.append("EXAMPLES:\n" + "\n".join(ex_parts))
                criterion_guidance = "\n\n".join(parts)
        except Exception:
            logger.warning(
                "SC %s: failed to build criterion-guidance block from "
                "the per-SC prompts JSON; judge will run without the "
                "structured guidance scaffolding.",
                self.criterion_id, exc_info=True,
            )

        # Summarize a11y tree for the AI (Fix 6)
        a11y_summary = None
        if capture_data.a11y_tree:
            try:
                from functions.prompt import summarize_a11y_tree
                a11y_summary = summarize_a11y_tree(
                    capture_data.a11y_tree, self.criterion_id
                )
            except Exception:
                logger.warning(
                    "SC %s: failed to summarize the a11y_tree for the "
                    "judge prompt; judge will see no a11y_tree block.",
                    self.criterion_id, exc_info=True,
                )

        # Build AT simulation announcement data for meaning verification.
        # This tells the AI exactly what a screen reader would say for
        # each element, so the AI can compare visual content vs announcement.
        at_announcements = ""
        if capture_data.a11y_tree and capture_data.a11y_tree.get("nodes"):
            try:
                from at_simulation.announcements import render_announcement, _get_role, _SILENT_ROLES
                ann_lines = []
                count = 0
                for node in capture_data.a11y_tree["nodes"]:
                    role = _get_role(node)
                    if role in _SILENT_ROLES or not role:
                        continue
                    announcement = render_announcement(node)
                    if announcement:
                        ann_lines.append(f"  - {announcement}")
                        count += 1
                if ann_lines:
                    at_announcements = (
                        "[SCREEN READER ANNOUNCEMENTS]\n"
                        "This is exactly what a screen reader (JAWS/NVDA/VoiceOver) "
                        "would announce for each element on this page. Compare these "
                        "announcements against what you SEE in the screenshots — if "
                        "an image shows a sunset but the announcement says 'image, "
                        "photo.jpg', the alt text is wrong.\n"
                        + "\n".join(ann_lines)
                    )
            except Exception:
                logger.warning(
                    "SC %s: failed to append AT announcement evidence "
                    "to the judge prompt.",
                    self.criterion_id, exc_info=True,
                )

        # Combine criterion guidance with AT announcements
        combined_context = criterion_guidance or ""
        if at_announcements:
            combined_context = (combined_context + "\n\n" + at_announcements).strip()

        # Add image context so the AI knows which screenshot is which
        image_context = self.get_image_context(capture_data)
        if image_context:
            combined_context = (combined_context + "\n\n" + image_context).strip()

        # CAPTURE GAPS: explicitly tell the AI what the tool could NOT see
        # so it doesn't invent verdicts for un-captured content. Covers
        # cross-origin iframes, overlay widgets that may override native
        # behaviour, and any interactive test that failed during capture.
        capture_gaps = self._build_capture_gaps_block(capture_data)
        if capture_gaps:
            combined_context = (combined_context + "\n\n" + capture_gaps).strip()

        user_prompt = build_user_prompt(
            page_context=page_context,
            programmatic_data=programmatic_data,
            elements=elements,
            user_context=combined_context or None,
            a11y_tree_summary=a11y_summary,
        )

        # Save visual AI prompt for review
        if capture_data.review_dir:
            try:
                from pathlib import Path as _Path
                from storage.review_store import save_prompt
                save_prompt(
                    _Path(capture_data.review_dir),
                    self.criterion_id,
                    system_prompt,
                    user_prompt,
                )
            except Exception:
                logger.warning(
                    "SC %s: failed to save visual-AI prompt.txt; the "
                    "canonical request is still on disk in llm_transcripts/.",
                    self.criterion_id, exc_info=True,
                )

        # Gather image paths for vision -- base screenshots plus
        # criterion-specific images from get_extra_images()
        # Annotated screenshot goes FIRST so the AI sees box overlays
        # before the clean screenshots.
        base_images: list[str] = []
        if _annotated_path:
            base_images.append(_annotated_path)
        if capture_data.full_page_path:
            base_images.append(capture_data.full_page_path)
        if capture_data.viewport_path:
            base_images.append(capture_data.viewport_path)
        # Add criterion-specific screenshots (subclasses override)
        extra = self.get_extra_images(capture_data)

        # ── Video-to-text: inject pre-computed descriptions ─────────
        # Instead of sending raw video (which blows up context windows
        # and breaks tool calls on smaller models), inject the text
        # descriptions that were pre-processed after capture.
        # Fall back to raw video only if no description exists.
        video_text = self._get_video_descriptions(capture_data)
        video_paths = self.get_video_paths(capture_data)
        send_raw_video = None  # only set if no text description available

        if video_text:
            # Inject video observations into the user prompt as text
            user_prompt = (
                user_prompt + "\n\n"
                "═══════════════════════════════════════════════════════\n"
                "  VIDEO OBSERVATIONS (from recorded walkthrough/observation)\n"
                "═══════════════════════════════════════════════════════\n"
                "The following was observed by reviewing recorded video of\n"
                "this page. Use these observations as evidence — they describe\n"
                "real behavior captured during automated testing.\n\n"
                + video_text
            )
            logger.info(
                "SC %s: injected %d chars of video descriptions as text (no raw video sent)",
                self.criterion_id, len(video_text),
            )
        elif video_paths:
            # No pre-computed descriptions — fall back to raw video
            send_raw_video = video_paths[0]
            logger.info(
                "SC %s: no video descriptions available, sending raw video",
                self.criterion_id,
            )

        # ── Chunked analysis when prompt or images are too large ─────
        # Estimate prompt tokens (~4 chars per token)
        prompt_tokens = (len(system_prompt) + len(user_prompt)) // 4
        image_paths = base_images + (extra or [])
        IMAGE_BATCH = 20
        # Token budget: leave room for tool schema (~2K) + response (~8K)
        # Models: Qwen3.5-35B=128K, Qwen3-VL=32K, Gemma4=128K, Gemini=1M
        # Use 100K as default — fits all text models comfortably.
        # Vision models with images use more tokens per image (~1K each).
        MAX_PROMPT_TOKENS = 100_000

        needs_chunking = (
            prompt_tokens > MAX_PROMPT_TOKENS
            or (extra and len(extra) > IMAGE_BATCH)
        )

        if needs_chunking:
            logger.info(
                "SC %s: visual AI prompt too large (%d tokens est, %d images) — chunking",
                self.criterion_id, prompt_tokens, len(image_paths),
            )
            raw = await self._chunked_visual_analysis(
                ai_client, system_prompt, user_prompt, elements,
                base_images, extra or [], send_raw_video,
                programmatic_data, capture_data,
            )
        else:
            raw = await ai_client.analyze(
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                image_paths=image_paths if image_paths else None,
                video_path=send_raw_video,
                needs_audio=self.needs_audio,
            )

        # Save visual AI response
        if capture_data.review_dir:
            try:
                import json as _json
                from pathlib import Path as _Path
                _resp_dir = _Path(capture_data.review_dir) / "tests" / self.criterion_id.replace(".", "_")
                _resp_dir.mkdir(parents=True, exist_ok=True)
                # Serialize findings properly (not as Python repr)
                def _serialize(obj):
                    if hasattr(obj, 'to_dict'):
                        return obj.to_dict()
                    if hasattr(obj, 'value'):
                        return obj.value
                    return str(obj)
                (_resp_dir / "visual_ai_response.json").write_text(
                    _json.dumps(raw, indent=2, default=_serialize), encoding="utf-8",
                )
            except Exception:
                logger.warning(
                    "SC %s: failed to save visual_ai_response.json sidecar; "
                    "the canonical request+response is still on disk in "
                    "llm_transcripts/.",
                    self.criterion_id, exc_info=True,
                )

        # Parse AI response into our structures.
        conf_raw = raw.get("conformance_level", "Not Evaluated")
        if isinstance(conf_raw, ConformanceLevel):
            conformance = conf_raw
        else:
            conf_map = {v.value: v for v in ConformanceLevel}
            conformance = conf_map.get(str(conf_raw), ConformanceLevel.NOT_EVALUATED)
        confidence = float(raw.get("confidence", 0.0))

        # Log when the model signals insufficient evidence or conflicting info
        insuff = raw.get("insufficient_evidence_reason")
        conflict = raw.get("conflicting_information")
        if insuff:
            logger.warning(
                "SC %s VISUAL-AI: INSUFFICIENT EVIDENCE — %s",
                self.criterion_id, insuff,
            )
        if conflict:
            logger.warning(
                "SC %s VISUAL-AI: CONFLICTING INFORMATION — %s",
                self.criterion_id, conflict,
            )
        if (insuff or conflict) and capture_data.review_dir:
            self._save_evidence_issue(
                capture_data, "visual_ai", system_prompt, user_prompt,
                raw, insuff, conflict,
            )

        findings: list[Finding] = []
        for item in raw.get("findings", []):
            if isinstance(item, Finding):
                # Already a Finding object from result_parser
                item.source = "visual_ai"
                findings.append(item)
            elif isinstance(item, dict):
                sev_str = item.get("severity", "medium")
                sev_map = {v.value: v for v in Severity}
                severity = sev_map.get(sev_str, Severity.MEDIUM)
                findings.append(Finding(
                    id=_make_finding_id(),
                    element=item.get("element", ""),
                    issue=item.get("issue", ""),
                    impact=item.get("impact", ""),
                    recommendation=item.get("recommendation", ""),
                    severity=severity,
                    source="visual_ai",
                ))

        return conformance, confidence, findings

    # ------------------------------------------------------------------
    # Code AI analysis (consumes the per-page code-pattern cache)
    # ------------------------------------------------------------------

    async def run_code_analysis(
        self,
        capture_data: CaptureData,
        ai_client: Any,
    ) -> tuple[ConformanceLevel, float, list[Finding]]:
        """Evaluate ONE criterion against the cached code-pattern inventory.

        The full page source (HTML + readable JavaScript) is analyzed ONCE
        per review by ``functions.code_analyzer.analyze_page_code``. That
        pass produces a unified list of SC-tagged accessibility patterns
        on ``capture_data.code_findings``. This method:

          1. Ensures the cache is populated (lazy-builds it on first SC
             that needs it).
          2. Filters to patterns tagged for this criterion.
          3. Makes ONE LLM call that judges each filtered pattern against
             the SC's own pass/fail rules, producing the final per-SC
             findings.

        Big win vs. the prior design: a university's ~100 code chunks are read once,
        not 50+ times, and the per-SC call is a short judgment on a
        pre-extracted pattern list rather than a full-source re-analysis.
        """
        html = capture_data.html or ""
        if not html:
            return ConformanceLevel.NOT_EVALUATED, 0.0, []

        # -- Ensure the Phase 1 cache exists ------------------------------
        cache = getattr(capture_data, "code_findings", None)
        if not isinstance(cache, list) or not cache:
            readable_js = _extract_readable_scripts(
                html, capture_data.script_content or "",
            )
            from functions.code_analyzer import analyze_page_code
            cache = await analyze_page_code(
                html=html,
                script_content=readable_js,
                review_dir=capture_data.review_dir or "",
            )
            capture_data.code_findings = cache

        # Layer 3 embeddings ride along with the cache. build_pattern_
        # embeddings loads from <review>/code_findings_embeddings.json if
        # the file already exists (written by analyze_page_code), so this
        # is cheap on every SC after the first.
        if cache and not getattr(capture_data, "code_findings_embeddings", None):
            try:
                from functions.sc_retrieval import build_pattern_embeddings
                capture_data.code_findings_embeddings = await build_pattern_embeddings(
                    cache,
                    review_dir=capture_data.review_dir or "",
                )
            except Exception as exc:
                logger.warning(
                    "SC %s: pattern-embedding hydrate failed (%s) -- judge "
                    "retrieval will be empty but SC continues",
                    self.criterion_id, exc,
                )
                capture_data.code_findings_embeddings = []

        # -- Filter to this SC --------------------------------------------
        from functions.code_analyzer import findings_for_sc
        sc_patterns = findings_for_sc(cache, self.criterion_id)
        logger.info(
            "SC %s code-AI: %d patterns match this SC (out of %d cached total)",
            self.criterion_id, len(sc_patterns), len(cache),
        )

        if not sc_patterns:
            # Phase 1 ran and tagged nothing for this SC. That is positive
            # evidence, not mere absence. Report Supports with moderate
            # confidence so the judge weighs it against other sources.
            return ConformanceLevel.SUPPORTS, 0.75, []

        # -- Build the per-SC judgment prompt -----------------------------
        try:
            from prompts import load_criterion_prompt
            template = load_criterion_prompt(self.criterion_id) or {}
        except Exception:
            template = {}

        guidance = self._build_criterion_guidance_block(template)

        system_prompt = (
            "ROLE\n"
            f"You are a WCAG Level {self.level} accessibility auditor reviewing "
            f"a pre-extracted code-pattern inventory for ONE criterion. An "
            f"earlier pass enumerated every accessibility-relevant pattern in "
            f"the page source and tagged each with the WCAG SCs it could be "
            f"evidence of. You receive the patterns tagged for THIS criterion "
            f"and must decide which ones are real {self.criterion_id} "
            f"violations.\n\n"
            "CRITERION UNDER TEST\n"
            f"- ID: {self.criterion_id}\n"
            f"- Name: {self.criterion_name}\n"
            f"- Level: {self.level}\n"
            f"- Normative text: {self.normative_text}\n\n"
            "TASK\n"
            f"For each pre-extracted pattern below, read its raw_evidence "
            f"carefully. If the pattern is a real {self.criterion_id} violation "
            f"according to the CRITERION GUIDANCE, include it as a finding in "
            f"report_wcag_assessment. If the pattern was mis-tagged (not really "
            f"this SC) or matches an auditor_anti_pattern, DROP it silently -- "
            f"Phase 1 over-tags on purpose so you can filter.\n\n"
            "EVIDENCE GROUNDING (strict)\n"
            "Every finding you emit must reference raw_evidence from the "
            "pre-extracted patterns. Do NOT invent selectors or elements -- "
            "copy them verbatim from the pattern list. Do NOT invent patterns "
            "not present in the list. If raw_evidence does not support the "
            "pattern's claim, drop it.\n\n"
            "PER-CRITERION RULES ARE LAW\n"
            "Honor the CRITERION GUIDANCE below. A finding is valid ONLY if "
            "it matches a fail_condition. If it matches an auditor_anti_"
            "pattern or an off_scope_topic, DROP it. When your training "
            "conflicts with the guidance, OBEY the guidance.\n\n"
            "RESPONSE\n"
            "Call report_wcag_assessment exactly once. If no patterns "
            "survive filtering, return conformance_level='Supports' with a "
            "brief summary explaining why. Otherwise populate findings."
        )

        patterns_block = _format_patterns_for_judge(sc_patterns)
        user_prompt_parts = [
            f"CRITERION: {self.criterion_id} -- {self.criterion_name}",
        ]
        if guidance:
            user_prompt_parts.append(f"CRITERION GUIDANCE\n{guidance}")
        user_prompt_parts.append(
            f"PRE-EXTRACTED PATTERNS TAGGED FOR THIS SC ({len(sc_patterns)}):\n"
            f"{patterns_block}"
        )
        user_prompt_parts.append(
            f"For each pattern, decide whether it is a valid "
            f"{self.criterion_id} violation. Quote raw_evidence in your "
            f"finding's issue field. Call report_wcag_assessment."
        )
        user_prompt = "\n\n".join(user_prompt_parts)

        # Save prompt for audit
        if capture_data.review_dir:
            try:
                from pathlib import Path as _Path
                from storage.review_store import save_prompt
                save_prompt(
                    _Path(capture_data.review_dir),
                    self.criterion_id,
                    system_prompt,
                    user_prompt,
                    prefix="code_ai_",
                )
            except Exception:
                logger.warning(
                    "SC %s: failed to save code-AI prompt.txt; the "
                    "canonical request is still on disk in llm_transcripts/.",
                    self.criterion_id, exc_info=True,
                )

        # -- Single LLM call ----------------------------------------------
        # Text-only (no screenshots). Visual AI in run_ai_analysis already
        # owns the screenshot path; code AI's job is to interpret code.
        raw = await ai_client.analyze(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            image_paths=None,
            video_path=None,
            temperature=0.2,
        )

        # -- Save response for audit --------------------------------------
        if capture_data.review_dir:
            try:
                import json as _json
                from pathlib import Path as _Path
                _resp_dir = _Path(capture_data.review_dir) / "tests" / self.criterion_id.replace(".", "_")
                _resp_dir.mkdir(parents=True, exist_ok=True)
                def _serialize_code(obj):
                    if hasattr(obj, "to_dict"):
                        return obj.to_dict()
                    if hasattr(obj, "value"):
                        return obj.value
                    return str(obj)
                (_resp_dir / "code_ai_response.json").write_text(
                    _json.dumps(raw, indent=2, default=_serialize_code),
                    encoding="utf-8",
                )
            except Exception:
                logger.warning(
                    "SC %s: failed to save code_ai_response.json sidecar; "
                    "the canonical request+response is still on disk in "
                    "llm_transcripts/.",
                    self.criterion_id, exc_info=True,
                )

        # -- Parse response ----------------------------------------------
        conf_raw = raw.get("conformance_level", "Not Evaluated")
        if isinstance(conf_raw, ConformanceLevel):
            conformance = conf_raw
        else:
            conf_map = {v.value: v for v in ConformanceLevel}
            conformance = conf_map.get(str(conf_raw), ConformanceLevel.NOT_EVALUATED)
        confidence = float(raw.get("confidence", 0.0))

        insuff = raw.get("insufficient_evidence_reason")
        conflict = raw.get("conflicting_information")
        if insuff:
            logger.warning(
                "SC %s CODE-AI: INSUFFICIENT EVIDENCE -- %s",
                self.criterion_id, insuff,
            )
        if conflict:
            logger.warning(
                "SC %s CODE-AI: CONFLICTING INFORMATION -- %s",
                self.criterion_id, conflict,
            )
        if (insuff or conflict) and capture_data.review_dir:
            self._save_evidence_issue(
                capture_data, "code_ai", system_prompt, user_prompt,
                raw, insuff, conflict,
            )

        findings: list[Finding] = []
        for item in raw.get("findings", []):
            if isinstance(item, Finding):
                item.source = "code_ai"
                findings.append(item)
            elif isinstance(item, dict):
                sev_str = item.get("severity", "medium")
                sev_map = {v.value: v for v in Severity}
                severity = sev_map.get(sev_str, Severity.MEDIUM)
                findings.append(Finding(
                    id=_make_finding_id(),
                    element=item.get("element", ""),
                    issue=item.get("issue", ""),
                    impact=item.get("impact", ""),
                    recommendation=item.get("recommendation", ""),
                    severity=severity,
                    source="code_ai",
                ))

        return conformance, confidence, findings

    def _build_criterion_guidance_block(self, template: dict) -> str:
        """Render the per-SC guidance JSON as a prompt block.

        Same scaffolding used by the visual AI pipeline -- plain meaning,
        scope, borderline calibration, pass/fail/na conditions, common
        mistakes, auditor anti-patterns, off-scope topics, examples.
        Shared here so the code AI honors the same rules as the visual AI.
        """
        parts: list[str] = []
        plain_meaning = template.get("plain_meaning")
        if plain_meaning:
            parts.append(f"PLAIN MEANING\n{plain_meaning}")
        scope_in = template.get("scope_applies_to", [])
        if scope_in:
            parts.append(
                "APPLIES TO (this criterion covers):\n"
                + "\n".join(f"  - {s}" for s in scope_in)
            )
        scope_out = template.get("scope_does_not_apply_to", [])
        if scope_out:
            parts.append(
                "DOES NOT APPLY TO (out of scope for this criterion):\n"
                + "\n".join(f"  - {s}" for s in scope_out)
            )
        borderline = template.get("borderline_calibration", [])
        if borderline:
            parts.append(
                "BORDERLINE CALIBRATION (how strict to be):\n"
                + "\n".join(f"  - {b}" for b in borderline)
            )
        pass_conds = template.get("pass_conditions", [])
        if pass_conds:
            parts.append(
                "SUPPORTS (pass) when:\n"
                + "\n".join(f"  - {c}" for c in pass_conds)
            )
        fail_conds = template.get("fail_conditions", [])
        if fail_conds:
            parts.append(
                "DOES NOT SUPPORT (fail) when:\n"
                + "\n".join(f"  - {c}" for c in fail_conds)
            )
        na_conds = template.get("na_conditions", [])
        if na_conds:
            parts.append(
                "NOT APPLICABLE when:\n"
                + "\n".join(f"  - {c}" for c in na_conds)
            )
        anti_patterns = template.get("auditor_anti_patterns", [])
        if anti_patterns:
            parts.append(
                "AUDITOR ANTI-PATTERNS (do NOT report these false positives):\n"
                + "\n".join(f"  - {a}" for a in anti_patterns)
            )
        off_scope = template.get("off_scope_topics")
        if not off_scope:
            try:
                from functions.prompt import get_off_scope_keywords
                off_scope = get_off_scope_keywords(self.criterion_id)
            except Exception:
                off_scope = []
        if off_scope:
            parts.append(
                f"OFF-SCOPE TOPICS FOR {self.criterion_id} "
                f"(REJECT findings about these -- they belong to a different SC):\n"
                + "\n".join(f"  - {t}" for t in off_scope)
            )
        examples = template.get("examples", {})
        if examples:
            ex_parts = [f"  {k.upper()}: {v}" for k, v in examples.items()]
            parts.append("EXAMPLES:\n" + "\n".join(ex_parts))
        return "\n\n".join(parts)


    # ------------------------------------------------------------------
    # ------------------------------------------------------------------
    # Chunked visual AI analysis (prompt-aware)
    # ------------------------------------------------------------------

    async def _chunked_visual_analysis(
        self,
        ai_client: Any,
        system_prompt: str,
        full_user_prompt: str,
        elements: list[dict],
        base_images: list[str],
        extra_images: list[str],
        video_path: str | None,
        programmatic_data: dict,
        capture_data: CaptureData,
    ) -> dict:
        """Chunk large visual AI prompts so nothing is missed.

        When the element list, images, or prompt is too large for one call:
        1. Split elements into groups (by type or count)
        2. Each chunk gets the base screenshots + its elements
        3. Findings from all chunks are collected
        4. A final consolidation call merges everything into one verdict

        Every element is analyzed — no truncation, no summarization of raw data.
        """
        import asyncio as _asyncio
        from functions.prompt import build_user_prompt

        ELEMENT_CHUNK_SIZE = 50  # elements per chunk
        IMAGE_BATCH_SIZE = 15   # extra images per chunk

        # Split elements into chunks
        element_chunks = []
        if elements:
            for i in range(0, len(elements), ELEMENT_CHUNK_SIZE):
                element_chunks.append(elements[i:i + ELEMENT_CHUNK_SIZE])
        else:
            element_chunks = [[]]

        # Split extra images into batches
        image_batches = []
        if extra_images:
            for i in range(0, len(extra_images), IMAGE_BATCH_SIZE):
                image_batches.append(extra_images[i:i + IMAGE_BATCH_SIZE])

        # Determine total chunks (max of element chunks and image batches)
        total_chunks = max(len(element_chunks), len(image_batches), 1)

        logger.info(
            "SC %s: chunked visual AI — %d element chunks, %d image batches, %d total rounds",
            self.criterion_id, len(element_chunks), len(image_batches), total_chunks,
        )

        all_findings: list[dict] = []
        chunk_verdicts: list[str] = []
        chunk_summaries: list[str] = []

        for chunk_idx in range(total_chunks):
            # Get this chunk's elements and images
            chunk_elements = (
                element_chunks[chunk_idx]
                if chunk_idx < len(element_chunks)
                else []
            )
            chunk_extra = (
                image_batches[chunk_idx]
                if chunk_idx < len(image_batches)
                else []
            )

            # Build chunk-specific user prompt
            page_context = {
                "url": capture_data.url,
                "title": capture_data.title,
                "file_type": capture_data.file_type,
                "review_type": getattr(capture_data, "review_type", "single"),
            }
            chunk_prompt = build_user_prompt(
                page_context=page_context,
                programmatic_data=programmatic_data,
                elements=chunk_elements,
            )
            chunk_prompt = (
                f"[CHUNK {chunk_idx + 1}/{total_chunks}] "
                f"Analyzing elements {chunk_idx * ELEMENT_CHUNK_SIZE + 1}-"
                f"{min((chunk_idx + 1) * ELEMENT_CHUNK_SIZE, len(elements))} "
                f"of {len(elements)} total.\n\n"
                + chunk_prompt
            )

            chunk_images = base_images + chunk_extra

            if chunk_idx > 0:
                await _asyncio.sleep(2)

            last_exc: Exception | None = None
            chunk_raw: dict | None = None
            for attempt in range(1, 4):
                try:
                    chunk_raw = await ai_client.analyze(
                        system_prompt=system_prompt,
                        user_prompt=chunk_prompt,
                        image_paths=chunk_images if chunk_images else None,
                        video_path=video_path,
                        needs_audio=self.needs_audio,
                    )
                    last_exc = None
                    break
                except Exception as exc:
                    last_exc = exc
                    logger.warning(
                        "SC %s: chunk %d/%d attempt %d/3 failed: %s",
                        self.criterion_id, chunk_idx + 1, total_chunks, attempt, exc,
                    )
                    if attempt < 3:
                        await _asyncio.sleep(2 ** attempt)

            if last_exc is not None or chunk_raw is None:
                raise RuntimeError(
                    f"SC {self.criterion_id} chunk {chunk_idx + 1}/{total_chunks} "
                    f"({len(chunk_extra)} elements) failed after 3 attempts: "
                    f"{last_exc}. Refusing to drop element batch -- no gaps allowed."
                )

            insuff = chunk_raw.get("insufficient_evidence_reason")
            conflict = chunk_raw.get("conflicting_information")
            if insuff:
                logger.warning(
                    "SC %s CHUNK-%d CODE-AI: INSUFFICIENT EVIDENCE — %s",
                    self.criterion_id, chunk_idx + 1, insuff,
                )
            if conflict:
                logger.warning(
                    "SC %s CHUNK-%d CODE-AI: CONFLICTING INFORMATION — %s",
                    self.criterion_id, chunk_idx + 1, conflict,
                )
            if (insuff or conflict) and capture_data.review_dir:
                self._save_evidence_issue(
                    capture_data, f"code_ai_chunk{chunk_idx + 1}",
                    system_prompt, chunk_prompt,
                    chunk_raw, insuff, conflict,
                )

            for f in chunk_raw.get("findings", []):
                if isinstance(f, Finding):
                    all_findings.append(f.to_dict())
                elif isinstance(f, dict):
                    all_findings.append(f)

            conf = chunk_raw.get("conformance_level", "Not Evaluated")
            if hasattr(conf, "value"):
                conf = conf.value
            chunk_verdicts.append(str(conf))
            chunk_summaries.append(
                f"Chunk {chunk_idx + 1}: {conf}, {len(chunk_raw.get('findings', []))} findings"
            )

            logger.info(
                "SC %s: chunk %d/%d → %s, %d findings",
                self.criterion_id, chunk_idx + 1, total_chunks,
                conf, len(chunk_raw.get("findings", [])),
            )

        # Consolidation: worst verdict wins, all findings merge
        if "Does Not Support" in chunk_verdicts:
            final_conf = "Does Not Support"
        elif "Partially Supports" in chunk_verdicts:
            final_conf = "Partially Supports"
        elif "Supports" in chunk_verdicts:
            final_conf = "Supports"
        else:
            final_conf = "Not Evaluated"

        return {
            "conformance_level": final_conf,
            "confidence": 0.8,
            "findings": all_findings,
            "summary": f"Analyzed in {total_chunks} chunks: {'; '.join(chunk_summaries)}",
        }

    # ------------------------------------------------------------------
    # Chunked AI analysis for large image sets (legacy)
    # ------------------------------------------------------------------

    async def _chunked_ai_analysis(
        self,
        ai_client: Any,
        system_prompt: str,
        user_prompt: str,
        base_images: list[str],
        extra_images: list[str],
        video_path: str | None,
        batch_size: int,
    ) -> dict:
        """Split a large set of images into batches, analyze each, then
        consolidate.

        Each batch gets the base images + a subset of extra images.
        The AI produces partial findings for each batch.  A final
        consolidation call merges the batch summaries into one verdict.
        """
        batches = [
            extra_images[i : i + batch_size]
            for i in range(0, len(extra_images), batch_size)
        ]
        total_batches = len(batches)

        logger.info(
            "SC %s: splitting %d images into %d batches of ~%d",
            self.criterion_id, len(extra_images), total_batches, batch_size,
        )

        batch_summaries: list[str] = []
        all_findings: list[dict] = []
        conformance_levels: list[str] = []

        for idx, batch in enumerate(batches, 1):
            # Pause between batches to let model free memory
            if idx > 1:
                import asyncio as _asyncio
                await _asyncio.sleep(3)

            start_num = (idx - 1) * batch_size + 1
            end_num = min(idx * batch_size, len(extra_images))
            # Use "pages" for document checks, "images" for web checks
            item_label = "pages" if self.criterion_id.startswith("DOC-") else "images"
            batch_prompt = (
                f"{user_prompt}\n\n"
                f"[BATCH {idx}/{total_batches}] "
                f"Analyzing {item_label} {start_num}-{end_num} "
                f"of {len(extra_images)} total. "
                f"When reporting findings, specify which page number "
                f"the issue is on (page {start_num} through {end_num}). "
                f"Report findings for THIS batch only."
            )

            image_paths = base_images + batch
            try:
                raw = await ai_client.analyze(
                    system_prompt=system_prompt,
                    user_prompt=batch_prompt,
                    image_paths=image_paths,
                    video_path=video_path if idx == 1 else None,
                )
                summary = raw.get("summary", "")
                batch_summaries.append(
                    f"Batch {idx}/{total_batches}: {summary}"
                )
                for f in raw.get("findings", []):
                    if isinstance(f, Finding):
                        all_findings.append(f.to_dict())
                    elif isinstance(f, dict):
                        all_findings.append(f)
                    else:
                        all_findings.append({"issue": str(f)})
                cl = raw.get("conformance_level", "Not Evaluated")
                if hasattr(cl, "value"):
                    cl = cl.value
                conformance_levels.append(str(cl))
            except Exception as exc:
                logger.warning(
                    "SC %s batch %d/%d failed: %s",
                    self.criterion_id, idx, total_batches, exc,
                )

        # --- Consolidation call ---
        consolidation_prompt = (
            f"{user_prompt}\n\n"
            f"[CONSOLIDATION] You reviewed {len(extra_images)} items "
            f"across {total_batches} batches. Here are the batch "
            f"summaries:\n\n"
            + "\n".join(batch_summaries)
            + f"\n\nBatch verdicts: {', '.join(conformance_levels)}\n"
            f"Total findings so far: {len(all_findings)}\n\n"
            f"Now provide your FINAL overall assessment for SC "
            f"{self.criterion_id}, considering ALL batches together. "
            f"Include any additional findings or override earlier ones "
            f"if needed."
        )

        try:
            final = await ai_client.analyze(
                system_prompt=system_prompt,
                user_prompt=consolidation_prompt,
                image_paths=base_images if base_images else None,
            )
            # Merge: keep all batch findings + any new consolidation findings
            for f in final.get("findings", []):
                if isinstance(f, Finding):
                    all_findings.append(f.to_dict())
                elif isinstance(f, dict):
                    all_findings.append(f)
            final["findings"] = all_findings
            return final
        except Exception as exc:
            logger.warning(
                "SC %s consolidation failed: %s", self.criterion_id, exc,
            )
            # Fall back to worst batch result
            return {
                "conformance_level": (
                    "Does Not Support" if "Does Not Support"
                    in conformance_levels
                    else "Partially Supports" if "Partially Supports"
                    in conformance_levels
                    else "Not Evaluated"
                ),
                "confidence": 0.6,
                "confidence_reasoning": (
                    f"Consolidation failed; using worst batch result "
                    f"from {total_batches} batches."
                ),
                "findings": all_findings,
                "summary": " | ".join(batch_summaries),
            }

    # ------------------------------------------------------------------
    # Reconciliation
    # ------------------------------------------------------------------

    # Criteria where Code AI gets tiebreaker power because the issue
    # is fundamentally about code structure, not visual appearance.
    _CODE_AI_TIEBREAKER = {"2.4.3", "1.3.2"}

    # Criteria where the programmatic check is mathematically definitive.
    # ONLY truly computational checks belong here — where the programmatic
    # result is a mathematical fact that AI cannot meaningfully dispute.
    # All other criteria should go through the judge for full evaluation.
    _PROGRAMMATIC_AUTHORITATIVE = {
        "2.3.1",   # Flashing — ffmpeg frame-rate math
        "4.1.1",   # Parsing — duplicate ID detection (exact match)
    }

    def _reconcile_verdicts(self, result: TestResult) -> TestResult:
        """Determine the final verdict using majority vote with
        programmatic authority.

        Strategy:
        - FIRST: If this criterion is in _PROGRAMMATIC_AUTHORITATIVE and
          the programmatic check returned SUPPORTS with 0 findings,
          the programmatic verdict is final. AI cannot override a clean
          programmatic pass on deterministic criteria.
        - Otherwise: MAJORITY VOTE with the existing 3-source system.
        """
        prog_c = result.programmatic_conformance

        # ── Programmatic authority ──────────────────────────────────
        # For criteria where code-based checks are definitive:
        # 1. A clean programmatic PASS cannot be overridden by AI speculation.
        # 2. When programmatic finds issues but AI says N/A, programmatic wins
        #    (AI doesn't understand this criterion applies).
        if self.criterion_id in self._PROGRAMMATIC_AUTHORITATIVE:
            # Case 1: Clean programmatic pass — AI cannot override
            if (
                prog_c == ConformanceLevel.SUPPORTS
                and result.programmatic_findings_count == 0
                and result.programmatic_confidence >= 0.5
            ):
                ai_high = [
                    f for f in result.findings
                    if f.source in ("visual_ai", "ai", "code_ai", "at_sim")
                    and f.severity == Severity.HIGH
                ]
                if not ai_high:
                    result.conformance_level = prog_c
                    result.confidence = max(result.programmatic_confidence, 0.9)
                    result.confidence_reasoning = (
                        f"Programmatic authority: {self.criterion_id} passed all "
                        f"deterministic checks with 0 findings. AI found no "
                        f"high-severity issues to override."
                    )
                    logger.info(
                        "SC %s: programmatic authority — SUPPORTS (prog=PASS, "
                        "0 findings, no AI HIGH findings)",
                        self.criterion_id,
                    )
                    self._generate_summary(result)
                    return result

            # Case 2: Programmatic found issues but AI says N/A — trust
            # programmatic (AI doesn't realize this criterion applies)
            if (
                prog_c not in (ConformanceLevel.NOT_EVALUATED, ConformanceLevel.NOT_APPLICABLE)
                and result.programmatic_findings_count > 0
            ):
                ai_c_check = result.ai_conformance
                code_c_check = result.code_ai_conformance
                if (
                    ai_c_check == ConformanceLevel.NOT_APPLICABLE
                    and code_c_check == ConformanceLevel.NOT_APPLICABLE
                ):
                    result.conformance_level = prog_c
                    result.confidence = result.programmatic_confidence
                    result.confidence_reasoning = (
                        f"Programmatic authority: {self.criterion_id} — programmatic "
                        f"found {result.programmatic_findings_count} issue(s) but AI "
                        f"incorrectly marked as Not Applicable. Using programmatic verdict."
                    )
                    logger.info(
                        "SC %s: programmatic authority override — %s "
                        "(prog found %d issues, AI said N/A)",
                        self.criterion_id, prog_c.value,
                        result.programmatic_findings_count,
                    )
                    self._generate_summary(result)
                    return result
        ai_c = result.ai_conformance
        code_ai_c = result.code_ai_conformance
        at_sim_c = result.at_sim_conformance

        # Collect all evaluated sources: (conformance, confidence, label).
        #
        # Low-confidence floor: a source returning a verdict below
        # ``_VOTE_CONFIDENCE_FLOOR`` is effectively saying "I don't know",
        # so we exclude it from the vote. Without this floor a
        # 30%-confidence "Supports" from one source could outvote a
        # 100%-confidence "Not Applicable" from another (verified on
        # fairfaxva.gov run 20260514_205147_cb3b646c SC 3.2.6: Visual AI
        # correctly returned NA with confidence 1.0 on a single-page
        # review of a cross-page criterion, but Programmatic 0.3 +
        # Code AI 0.75 majority-voted to "Supports"). Sources below the
        # floor get logged as filtered-out so the audit trail still
        # records what every check produced.
        _FLOOR = self._VOTE_CONFIDENCE_FLOOR
        sources: list[tuple[ConformanceLevel, float, str]] = []
        filtered_out: list[tuple[ConformanceLevel, float, str]] = []
        for source_conf, source_confidence, source_label in (
            (prog_c, result.programmatic_confidence, "Programmatic"),
            (ai_c, result.ai_confidence, "Visual AI"),
            (code_ai_c, result.code_ai_confidence, "Code AI"),
            (at_sim_c, result.at_sim_confidence, "AT Simulation"),
        ):
            if source_conf == ConformanceLevel.NOT_EVALUATED:
                continue
            if source_confidence < _FLOOR:
                filtered_out.append((source_conf, source_confidence, source_label))
                continue
            sources.append((source_conf, source_confidence, source_label))
        if filtered_out:
            logger.info(
                "SC %s: low-confidence floor filtered %d source(s) below %.0f%%: %s",
                self.criterion_id, len(filtered_out), _FLOOR * 100,
                ", ".join(f"{l} ({c.value} @ {conf:.0%})" for c, conf, l in filtered_out),
            )

        if not sources:
            result.conformance_level = ConformanceLevel.NOT_EVALUATED
            result.confidence = 0.0
            result.confidence_reasoning = "No sources produced a verdict."

        elif len(sources) == 1:
            conf, confidence, label = sources[0]
            result.conformance_level = conf
            result.confidence = confidence
            result.confidence_reasoning = f"{label} only: {conf.value} ({confidence:.0%})."

        elif len(sources) == 2:
            conf_a, conf_a_c, label_a = sources[0]
            conf_b, conf_b_c, label_b = sources[1]
            if conf_a == conf_b:
                result.conformance_level = conf_a
                result.confidence = max(conf_a_c, conf_b_c)
                result.confidence_reasoning = (
                    f"Both agree: {conf_a.value}. "
                    f"{label_a}: {conf_a_c:.0%}, {label_b}: {conf_b_c:.0%}."
                )
            else:
                # When sources disagree, use confidence to arbitrate.
                # If one source has significantly higher confidence
                # (> 0.3 gap), trust it. Otherwise take the worse
                # (conservative for compliance).
                confidence_gap = abs(conf_a_c - conf_b_c)
                if confidence_gap > 0.3:
                    # Trust the higher-confidence source
                    if conf_a_c > conf_b_c:
                        winner_c, winner_conf, winner_label = conf_a, conf_a_c, label_a
                        loser_c, loser_conf, loser_label = conf_b, conf_b_c, label_b
                    else:
                        winner_c, winner_conf, winner_label = conf_b, conf_b_c, label_b
                        loser_c, loser_conf, loser_label = conf_a, conf_a_c, label_a
                    result.conformance_level = winner_c
                    result.confidence = winner_conf * 0.85  # Slight penalty for disagreement
                    result.confidence_reasoning = (
                        f"2 sources disagree, trusting higher confidence: "
                        f"{winner_label}: {winner_c.value} ({winner_conf:.0%}) vs "
                        f"{loser_label}: {loser_c.value} ({loser_conf:.0%}). "
                        f"Confidence gap: {confidence_gap:.0%}."
                    )
                else:
                    # Close confidence — take the worse (conservative)
                    result.conformance_level = _worse(conf_a, conf_b)
                    result.confidence = conf_a_c if _worse(conf_a, conf_b) == conf_a else conf_b_c
                    result.confidence_reasoning = (
                        f"2 sources disagree with similar confidence, taking worse: "
                        f"{result.conformance_level.value}. "
                        f"{label_a}: {conf_a.value} ({conf_a_c:.0%}), "
                        f"{label_b}: {conf_b.value} ({conf_b_c:.0%})."
                    )

        else:
            # Three or more sources — MAJORITY VOTE
            n_sources = len(sources)
            verdicts = [s[0] for s in sources]

            # Code AI tiebreaker: for structural criteria, if Code AI
            # has highest confidence and disagrees, it wins
            if (
                self.criterion_id in self._CODE_AI_TIEBREAKER
                and code_ai_c != ConformanceLevel.NOT_EVALUATED
                and result.code_ai_confidence > result.programmatic_confidence
                and result.code_ai_confidence > result.ai_confidence
            ):
                result.conformance_level = code_ai_c
                result.confidence = result.code_ai_confidence
                result.confidence_reasoning = (
                    f"Code AI tiebreaker (structural criterion {self.criterion_id}): "
                    f"{code_ai_c.value} ({result.code_ai_confidence:.0%}). "
                    f"Prog: {prog_c.value} ({result.programmatic_confidence:.0%}), "
                    f"Visual: {ai_c.value} ({result.ai_confidence:.0%})."
                )
                # Skip normal majority vote
                self._generate_summary(result)
                return result

            # Check if 2+ agree on the same verdict
            from collections import Counter
            vote_counts = Counter(verdicts)
            majority_verdict, majority_count = vote_counts.most_common(1)[0]

            if majority_count >= 2:
                # Majority wins
                result.conformance_level = majority_verdict
                # Confidence from the majority sources
                majority_sources = [(c, conf, l) for c, conf, l in sources if c == majority_verdict]
                result.confidence = max(conf for _, conf, _ in majority_sources)
                minority = [(c, conf, l) for c, conf, l in sources if c != majority_verdict]

                majority_str = ", ".join(f"{l}: {c.value} ({conf:.0%})" for c, conf, l in majority_sources)
                minority_str = ", ".join(f"{l}: {c.value} ({conf:.0%})" for c, conf, l in minority)

                if minority:
                    result.confidence_reasoning = (
                        f"Majority ({majority_count}/{n_sources}): {majority_verdict.value}. "
                        f"Agreed: {majority_str}. "
                        f"Dissent: {minority_str}."
                    )
                else:
                    result.confidence_reasoning = (
                        f"Unanimous ({n_sources}/{n_sources}): {majority_verdict.value}. {majority_str}."
                    )
            else:
                # No majority — take the median severity
                indexed = []
                for conf, confidence, label in sources:
                    idx = _CONFORMANCE_ORDER.index(conf) if conf in _CONFORMANCE_ORDER else 2
                    indexed.append((idx, conf, confidence, label))
                indexed.sort(key=lambda x: x[0])
                # Median (middle element; for even count, lean conservative = higher index)
                mid_idx = len(indexed) // 2
                _, mid_conf, mid_confidence, mid_label = indexed[mid_idx]
                result.conformance_level = mid_conf
                result.confidence = mid_confidence
                parts = ", ".join(f"{l}: {c.value} ({conf:.0%})" for _, c, conf, l in indexed)
                result.confidence_reasoning = (
                    f"No majority — {n_sources} sources disagree. Using median: {mid_conf.value}. {parts}."
                )

        # Post-reconciliation sanity check: if there are conformance-level
        # findings (HIGH, MEDIUM, or LOW — not INFO), the result cannot be
        # "Supports."  INFO findings are best-practice observations and do
        # not affect conformance.
        logger.info(
            "SC %s reconciled: %s with %d findings",
            self.criterion_id,
            result.conformance_level.value if hasattr(result.conformance_level, 'value') else str(result.conformance_level),
            len(result.findings),
        )
        if result.conformance_level == ConformanceLevel.SUPPORTS and result.findings:
            # Classify findings by severity — works with both Severity enums and strings
            high: list = []
            med: list = []
            low: list = []
            for f in result.findings:
                sev = f.severity
                sev_val = sev.value if hasattr(sev, 'value') else str(sev).lower()
                if sev_val == 'high':
                    high.append(f)
                elif sev_val == 'medium':
                    med.append(f)
                elif sev_val == 'low':
                    low.append(f)
            logger.info(
                "SC %s SANITY CHECK: Supports with %d findings (high=%d med=%d low=%d)",
                self.criterion_id, len(result.findings), len(high), len(med), len(low),
            )
            if high:
                result.conformance_level = ConformanceLevel.DOES_NOT_SUPPORT
                result.confidence_reasoning += (
                    f" OVERRIDE: Downgraded from Supports — "
                    f"{len(high)} high-severity finding(s) present."
                )
                logger.warning(
                    "SC %s: overriding Supports → Does Not Support (%d HIGH findings)",
                    self.criterion_id, len(high),
                )
            elif med:
                result.conformance_level = ConformanceLevel.PARTIALLY_SUPPORTS
                result.confidence_reasoning += (
                    f" OVERRIDE: Downgraded from Supports — "
                    f"{len(med)} medium-severity finding(s) present."
                )
                logger.warning(
                    "SC %s: overriding Supports → Partially Supports (%d MEDIUM findings)",
                    self.criterion_id, len(med),
                )
            elif low:
                # LOW findings are best-practice observations, not conformance
                # failures. Keep them in the report but don't downgrade.
                logger.info(
                    "SC %s: Supports verdict maintained — %d LOW findings "
                    "are best-practice observations, not conformance failures",
                    self.criterion_id, len(low),
                )

        # Sanity check (logging only, no behaviour change):
        #   A Partially-Supports / Does-Not-Support verdict with an empty
        #   findings list is internally inconsistent. It usually means a
        #   real finding was DROPPED downstream of the judge (in dedup,
        #   off-scope filtering, source-attribution validation, or a
        #   bespoke check filter). An earlier "upgrade verdict to Supports"
        #   override was tried here and reverted -- it would have HIDDEN
        #   the real bug (observed on SC 2.4.4 example.com run, where
        #   the judge correctly produced an ANDI ambiguous-link-text
        #   finding that disappeared before the report was written).
        #   We log the inconsistency loudly so the operator can find the
        #   filter that's losing findings, but we do not silently rewrite
        #   the verdict.
        if (
            result.conformance_level in (
                ConformanceLevel.PARTIALLY_SUPPORTS,
                ConformanceLevel.DOES_NOT_SUPPORT,
            )
            and not result.findings
        ):
            logger.warning(
                "SC %s: %s with 0 findings -- internally inconsistent. "
                "A finding was dropped after the verdict was set "
                "(programmatic_findings_count=%d). Investigate the "
                "filter chain. Original reasoning: %s",
                self.criterion_id,
                result.conformance_level.value,
                result.programmatic_findings_count,
                result.confidence_reasoning,
            )

        self._generate_summary(result)
        return result

    def _generate_summary(self, result: TestResult) -> None:
        """Generate the summary text from findings."""
        if not result.summary:
            high = sum(
                1 for f in result.findings
                if f.severity in (Severity.HIGH, "high")
            )
            med = sum(
                1 for f in result.findings
                if f.severity in (Severity.MEDIUM, "medium")
            )
            if result.conformance_level == ConformanceLevel.SUPPORTS:
                result.summary = (
                    f"SC {self.criterion_id} {self.criterion_name}: "
                    f"Page supports this criterion."
                )
            elif result.conformance_level == ConformanceLevel.NOT_APPLICABLE:
                result.summary = (
                    f"SC {self.criterion_id} {self.criterion_name}: "
                    f"Not applicable to this page."
                )
            else:
                result.summary = (
                    f"SC {self.criterion_id} {self.criterion_name}: "
                    f"{len(result.findings)} issue(s) found "
                    f"({high} high, {med} medium). "
                    f"Verdict: {result.conformance_level.value}."
                )

    # ------------------------------------------------------------------
    # Finding deduplication
    # ------------------------------------------------------------------

    def _save_evidence_issue(
        self,
        capture_data: CaptureData,
        source: str,
        system_prompt: str,
        user_prompt: str,
        raw_response: dict,
        insufficient: str | None,
        conflicting: str | None,
    ) -> None:
        """Save a dedicated file when the model flags insufficient or conflicting evidence.

        Writes ``<review>/tests/<sc>/evidence_issue_<source>.json`` containing
        the FULL system prompt, FULL user prompt, FULL model response, and
        the model's explanation of what went wrong. This makes it trivial to
        find and review these cases without digging through llm_transcripts/.
        """
        import json as _json
        from pathlib import Path as _Path

        try:
            sc_dir = (
                _Path(capture_data.review_dir)
                / "tests"
                / self.criterion_id.replace(".", "_")
            )
            sc_dir.mkdir(parents=True, exist_ok=True)

            def _ser(obj):
                if hasattr(obj, "to_dict"):
                    return obj.to_dict()
                if hasattr(obj, "value"):
                    return obj.value
                return str(obj)

            record = {
                "criterion_id": self.criterion_id,
                "source": source,
                "issue_type": (
                    "insufficient_evidence" if insufficient
                    else "conflicting_information"
                ),
                "insufficient_evidence_reason": insufficient,
                "conflicting_information": conflicting,
                "system_prompt_full": system_prompt,
                "user_prompt_full": user_prompt,
                "model_response_full": raw_response,
            }

            path = sc_dir / f"evidence_issue_{source}.json"
            path.write_text(
                _json.dumps(record, indent=2, ensure_ascii=False, default=_ser),
                encoding="utf-8",
            )
            logger.info(
                "SC %s: evidence issue saved to %s", self.criterion_id, path,
            )
        except Exception as exc:
            logger.error(
                "SC %s: failed to save evidence issue: %s",
                self.criterion_id, exc,
            )

    def _filter_findings_contradicted_by_capture(
        self, findings: list[Finding], capture_data: CaptureData
    ) -> list[Finding]:
        """Drop findings whose claims directly contradict captured data.

        This is the structured DOM-facts cross-check that the architecture
        documents but that previously only ran inside the judge prompt.
        Pulling it server-side guarantees a finding cannot be shipped if
        the captured deterministic data already proves it false. Each
        rule below corresponds to an observed false-positive class:

        - SC 4.1.2 / 1.3.1 / 3.3.2: "form control has no accessible name"
          findings against form_fields that DO have label/aria-label/
          aria-labelledby/title (observed on a university's search radios).
        - "Element X is not in the DOM": code-AI hallucinations of
          selectors that don't appear in the captured HTML (#szdebugarea
          on a university site). The captured DOM is the ground truth.
        """
        if not findings:
            return findings

        import re as _re

        # Build a quick index of form fields with names
        named_form_selectors: set[str] = set()
        for ff in capture_data.form_fields or []:
            sel = (ff.get("selector") or "").strip()
            if not sel:
                continue
            has_name = any([
                (ff.get("label") or "").strip(),
                (ff.get("aria_label") or ff.get("aria-label") or "").strip(),
                (ff.get("aria_labelledby") or ff.get("aria-labelledby") or "").strip(),
                (ff.get("title") or "").strip(),
            ])
            if has_name:
                named_form_selectors.add(sel)

        # Broaden the named-element index with element_inventory, whose
        # accessible names DO include text from nested sr-only spans and
        # <img alt> that ANDI's name computation can miss. Lets Rule 1
        # reject "no accessible name" findings on buttons/links that
        # genuinely have a name (verified 4.1.2: #mobile-menu-toggler and
        # the video-control buttons were flagged "no name" despite a
        # <span class="screen-reader-text"> label; axe passed them).
        named_selectors: set[str] = set(named_form_selectors)
        _inv = getattr(capture_data, "element_inventory", None) or []
        if isinstance(_inv, dict):
            _inv = _inv.get("elements") or []
        for _el in _inv:
            if not isinstance(_el, dict):
                continue
            _nm = (
                _el.get("accessible_name") or _el.get("text")
                or _el.get("aria_label") or _el.get("aria-label") or ""
            ).strip()
            _s = (_el.get("selector") or "").strip()
            if _nm and _s:
                named_selectors.add(_s)

        # Elements the browser keeps OUT of the tab order regardless of
        # authoring (tab_reachable=False — display:none, visibility:
        # hidden, the hidden attribute, inert, zero-rect). A keyboard or
        # focus finding on one of these describes a focus leak that does
        # not actually exist.
        browser_handled: set[str] = set()
        for _h in (getattr(capture_data, "andi_hidden_results", None) or []):
            if isinstance(_h, dict) and is_browser_handled(_h):
                _hs = (_h.get("selector") or "").strip()
                if _hs:
                    browser_handled.add(_hs)

        # axe accessible-name corroboration. ANDI's name detector ignores
        # clip/sr-only label spans; axe's link-name/button-name/image-alt
        # rules honour them. A 'no accessible name' finding contradicted by
        # an axe name-rule pass (or a page-clean axe name rule) is a false
        # positive (verified on a university 2026-05-28: ANDI flagged 4 named
        # nav/infographic links no-name; axe link-name = 67 pass / 0 violation).
        from functions.axe_extract import (
            accessible_name_corroboration, axe_confirms_named,
        )
        _axe_name_summary = accessible_name_corroboration(
            getattr(capture_data, "axe_results", None) or {}
        )
        # Whether the capture carries any event-listener data. We currently
        # capture none, so a judge claim about registered listeners is
        # ungrounded (only code-AI over script_content can support one).
        _has_event_listener_capture = bool(
            getattr(capture_data, "event_listeners", None)
            or getattr(capture_data, "event_listener_map", None)
        )

        # Link accessible-name -> set of destination hrefs. Used to apply the
        # WCAG H30 exception: links sharing the same name AND the same
        # destination need no unique differentiation (verified on a university
        # 2026-05-28: two 'Learn more about the ceremony' links both point to
        # the same article -- a 2.4.9 false positive -- whereas two 'Learn
        # more about this research' links go to two different articles and ARE
        # a real ambiguity).
        _link_name_to_hrefs: dict[str, set] = {}
        for _l in (getattr(capture_data, "links", None) or []):
            if not isinstance(_l, dict):
                continue
            _nm = (
                _l.get("text") or _l.get("accessible_name")
                or _l.get("aria_label") or _l.get("aria-label") or ""
            ).strip().lower()
            _hf = (_l.get("href") or "").strip().rstrip("/")
            if _nm:
                _link_name_to_hrefs.setdefault(_nm, set()).add(_hf)

        # SC 2.1.1 / 2.4.3: keyboard tab-coverage ground truth. When the
        # captured coverage is high, a finding claiming widespread
        # keyboard-unreachability for elements NOT in the deterministic
        # focusable_but_skipped list contradicts the measurement.
        _tabcov = getattr(capture_data, "tab_coverage", None) or {}
        try:
            coverage_pct = float(_tabcov.get("coverage_percent"))
        except (TypeError, ValueError):
            coverage_pct = None
        from functions.keyboard_extract import assess_tab_walk_reliability
        _walk_reliability = assess_tab_walk_reliability(capture_data)
        skipped_selectors: set[str] = set()
        for _sk in (_tabcov.get("focusable_but_skipped") or []):
            if isinstance(_sk, dict):
                _sks = (_sk.get("selector") or "").strip()
                if _sks:
                    skipped_selectors.add(_sks)

        # SC 1.4.13: elements whose hover/focus probe revealed NO new
        # content. Content on Hover or Focus cannot fail on an element
        # that reveals nothing.
        no_reveal_selectors: set[str] = set()
        for _hc in (getattr(capture_data, "hover_content", None) or []):
            if isinstance(_hc, dict) and _hc.get("new_elements_count") == 0:
                _hcs = (_hc.get("selector") or "").strip()
                if _hcs:
                    no_reveal_selectors.add(_hcs)
        # Extract id-literals from no-reveal selectors so the rule below
        # can also fire when the finding cites a comma-separated compound
        # of those elements.
        no_reveal_ids: set[str] = set()
        for _ns in no_reveal_selectors:
            for _idm in _re.findall(r"#([\w-]+)", _ns):
                no_reveal_ids.add(_idm)

        # SC 2.4.1: skip-link selectors the deterministic probe shows
        # actually activate via the keyboard. A finding calling such a
        # skip link "non-functional" contradicts the measurement.
        working_skip_links: set[str] = set()
        for _slr in (getattr(capture_data, "skip_link_results", None) or []):
            if not isinstance(_slr, dict):
                continue
            if _slr.get("keyboard_activates") is True:
                _sls = (_slr.get("skip_link_selector")
                        or _slr.get("selector") or "").strip()
                if _sls:
                    working_skip_links.add(_sls)
                _tgt = (_slr.get("target_href") or "").strip()
                if _tgt:
                    working_skip_links.add(f'a[href="{_tgt}"]')
                    working_skip_links.add(f"a[href='{_tgt}']")

        # SC 2.4.7: focus_contrast measurements showing the element DOES
        # have a measurable focus indicator. A "no visible focus
        # indicator" finding on such an element contradicts the
        # measurement.
        well_focused_selectors: set[str] = set()
        for _fc in (getattr(capture_data, "focus_contrast", None) or []):
            if not isinstance(_fc, dict):
                continue
            if _fc.get("has_change") is True:
                try:
                    cr = float(_fc.get("contrast_ratio") or 0)
                except (ValueError, TypeError):
                    cr = 0.0
                if cr >= 3.0:  # WCAG 1.4.11 non-text-contrast threshold
                    _fcs = (_fc.get("selector") or "").strip()
                    if _fcs:
                        well_focused_selectors.add(_fcs)

        # SC 2.2.4: was an auto-opening modal captured? An entry whose
        # kind/trigger indicates auto-open, or the explicit auto_open
        # flag, signals a real auto-modal. Empty means none.
        has_auto_modal = False
        for _mi in (getattr(capture_data, "modal_interactions", None) or []):
            if not isinstance(_mi, dict):
                continue
            kind = str(_mi.get("kind") or _mi.get("trigger") or "").lower()
            if "auto" in kind or "load" in kind or _mi.get("auto_open"):
                has_auto_modal = True
                break

        # Authoritative deterministic facts for the contradiction rules
        # below. headings is the captured heading inventory (it records
        # role="heading" aria-level elements as well as <h1>..<h6>);
        # positioned_elements is the full-page computed-style scan of
        # every fixed/sticky element — an empty list means the scan ran
        # and found none.
        page_has_h1 = any(
            (h.get("level") == 1 or str(h.get("tag") or "").lower() == "h1")
            for h in (capture_data.headings or [])
        )
        no_fixed_or_sticky = not (capture_data.positioned_elements or [])

        # SC 1.4.5: whitespace-stripped text that elements render as real
        # DOM text over a CSS background image. An element displaying
        # this text shows RENDERED HTML, not an image of text.
        bg_text_strings: set[str] = set()
        for bg in (capture_data.background_images or []):
            norm = "".join(str(bg.get("text_content") or "").split()).lower()
            if len(norm) >= 4:
                bg_text_strings.add(norm)

        # Keep DOM in original case for ID lookups (HTML id attribute
        # values are case-sensitive in HTML5 -- "asuHeader" != "asuheader").
        # The ``_lower`` variant is reserved for case-insensitive checks
        # like aria-label values where authors may inconsistently case
        # their labels.
        captured_html = capture_data.html or ""
        captured_html_lower = captured_html.lower()

        kept: list[Finding] = []
        for f in findings:
            sel = (getattr(f, "css_selector", "") or "").strip()
            issue_lower = (getattr(f, "issue", "") or "").lower()

            # Rule 1: drop "no accessible name" findings on already-
            # named elements. Recognise the claim shape generally: an
            # absence/negation word ("no", "lacks", "missing", "without",
            # "absence of", "has no", "have no", "don't have") combined
            # with a name/label keyword. This catches every variant
            # ("lack an accessible name", "lacks programmatic name",
            # "has no aria-label", "missing accessible name", ...)
            # without an exhaustive literal list.
            if sel and sel in named_selectors:
                has_negation = any(n in issue_lower for n in (
                    "no ", "lacks", "lacking", "lack ", "lack a",
                    "lack an", "lack any", "missing", "without",
                    "absence of", "has no ", "have no ",
                    "don't have", "do not have", "does not have",
                ))
                has_name_keyword = any(k in issue_lower for k in (
                    "accessible name", "programmatic name",
                    "programmatically determinable",
                    "programmatically associated",
                    "programmatic label", "accessible label",
                    "aria-label", " name ", " name.", " name,",
                    " name;", " name)", " name and ",
                    "label or instructions", "associated label",
                    "visible label",
                ))
                if has_negation and has_name_keyword:
                    logger.info(
                        "SC %s: dropping no-name finding on %s -- "
                        "captured data shows it has a name",
                        self.criterion_id, sel,
                    )
                    continue

            # Rule 2: drop findings whose css_selector contains an
            # id literal that does not appear in the captured HTML.
            # Code AI occasionally invents element IDs that do not
            # exist on the page (#szdebugarea on a university site was the canonical
            # case). HTML id attribute values are CASE-SENSITIVE per
            # HTML5, so we compare against the original-case DOM, not
            # a lowercased copy. (Earlier version of this rule
            # silently dropped real findings on every site that uses
            # camelCase IDs like #asuHeader by comparing case-mismatched
            # strings.) We only check ID-literal selectors because
            # class names are too short and frequent for this defense
            # to be safe on them.
            if sel and captured_html:
                id_literals = _re.findall(r"#([\w-]{4,})", sel)
                if id_literals:
                    unique_ids = set(id_literals)
                    missing = [
                        i for i in unique_ids
                        if f'id="{i}"' not in captured_html
                        and f"id='{i}'" not in captured_html
                        and f"id={i} " not in captured_html
                        and f"id={i}>" not in captured_html
                    ]
                    if missing and len(missing) == len(unique_ids):
                        logger.info(
                            "SC %s: dropping finding -- selector %r "
                            "references id(s) %s that are not present "
                            "in the captured DOM (code AI hallucination)",
                            self.criterion_id, sel, missing,
                        )
                        continue

            # Rule 3: drop findings whose css_selector is a bare
            # identifier (no #, ., [, *, :, or known tag prefix). Code
            # AI sometimes pastes a JavaScript variable name (e.g.
            # "alertNode") as the css_selector, which is not a real
            # CSS selector and would never match anything via
            # querySelector. Distinguish these from valid bare-tag
            # selectors (a, div, button, etc.) by checking against
            # the HTML5 element list.
            if sel:
                _HTML5_TAGS = {
                    "a", "abbr", "address", "area", "article", "aside",
                    "audio", "b", "base", "bdi", "bdo", "blockquote",
                    "body", "br", "button", "canvas", "caption", "cite",
                    "code", "col", "colgroup", "data", "datalist", "dd",
                    "del", "details", "dfn", "dialog", "div", "dl",
                    "dt", "em", "embed", "fieldset", "figcaption",
                    "figure", "footer", "form", "h1", "h2", "h3", "h4",
                    "h5", "h6", "head", "header", "hr", "html", "i",
                    "iframe", "img", "input", "ins", "kbd", "label",
                    "legend", "li", "link", "main", "map", "mark",
                    "menu", "meta", "meter", "nav", "noscript", "object",
                    "ol", "optgroup", "option", "output", "p", "param",
                    "picture", "pre", "progress", "q", "rp", "rt",
                    "ruby", "s", "samp", "script", "search", "section",
                    "select", "slot", "small", "source", "span",
                    "strong", "style", "sub", "summary", "sup", "svg",
                    "table", "tbody", "td", "template", "textarea",
                    "tfoot", "th", "thead", "time", "title", "tr",
                    "track", "u", "ul", "var", "video", "wbr",
                    # SVG-specific tags that show up in real selectors
                    "circle", "ellipse", "g", "line", "path", "polygon",
                    "polyline", "rect", "text", "tspan", "use",
                }
                # Bare-identifier check: the selector is one or more
                # word chars (no #/./[/*/:), case-insensitive, AND the
                # resulting tag is not in the HTML5 element list.
                if _re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]*", sel):
                    if sel.lower() not in _HTML5_TAGS:
                        logger.info(
                            "SC %s: dropping finding -- css_selector "
                            "%r is a bare identifier that is not a "
                            "valid HTML element (likely a JavaScript "
                            "variable name leaked from code AI)",
                            self.criterion_id, sel,
                        )
                        continue

            # Rule 4: drop findings whose attribute selectors target
            # values that don't appear in the DOM. Code AI sometimes
            # cites a non-existent aria-label / data-* / role value.
            # Example: section[aria-label="Alumni Spotlight"] when no
            # such section exists. Conservative: only check
            # [aria-label="..."] and [aria-labelledby="..."] since those
            # are the most commonly hallucinated.
            if sel and captured_html:
                aria_label_lits = _re.findall(
                    r'aria-label\s*=\s*"([^"]+)"', sel
                ) + _re.findall(
                    r"aria-label\s*=\s*'([^']+)'", sel
                )
                if aria_label_lits:
                    # aria-label values are user-facing strings and
                    # authors may inconsistently capitalize them. Use
                    # case-insensitive comparison here; for IDs we use
                    # case-sensitive (Rule 2). This is the right
                    # tradeoff for the two attribute types.
                    cap_lower = captured_html_lower
                    missing = [
                        v for v in aria_label_lits
                        if f'aria-label="{v}"'.lower() not in cap_lower
                        and f"aria-label='{v}'".lower() not in cap_lower
                    ]
                    if missing and len(missing) == len(aria_label_lits):
                        logger.info(
                            "SC %s: dropping finding -- selector %r "
                            "references aria-label value(s) %s not "
                            "present in the captured DOM",
                            self.criterion_id, sel, missing,
                        )
                        continue

            # Rule 5: drop findings claiming an element uses fixed or
            # sticky positioning when the deterministic full-page
            # computed-style scan found none. Verified bug (loudoun.gov
            # SC 1.4.10): 12 findings claimed "position: fixed" on
            # elements the scan measured as statically positioned —
            # stale HTML_CodeSniffer warnings that survived into the
            # report. positioned_elements is the ground truth, and the
            # judge prompt is already told "0 detected" for this page.
            if no_fixed_or_sticky:
                claim_text = issue_lower + " " + (getattr(f, "impact", "") or "").lower()
                fixed_phrases = (
                    "position: fixed", "position:fixed", "position fixed",
                    "fixed position", "fixed-position", "fixed positioning",
                    "position: sticky", "position:sticky", "position sticky",
                    "sticky position", "sticky-position", "sticky positioning",
                )
                if any(p in claim_text for p in fixed_phrases):
                    logger.info(
                        "SC %s: dropping finding claiming fixed/sticky "
                        "positioning -- the computed-style scan found 0 "
                        "fixed/sticky elements on the page",
                        self.criterion_id,
                    )
                    continue

            # Rule 6: drop findings claiming the page has no level-1
            # heading when the captured headings include one. Verified
            # bug (loudoun.gov SC 1.3.1): the judge emitted a "page
            # lacks an <h1>" finding contradicting both axe
            # (page-has-heading-one passes) and capture_data.headings,
            # which lists a level-1 heading.
            if page_has_h1:
                no_h1_phrases = (
                    "no h1", "no <h1>", "missing h1", "missing an h1",
                    "missing <h1>", "lacks an h1", "lacks a h1",
                    "lacks an <h1>", "without an h1", "does not have an h1",
                    "does not contain an h1", "absence of an h1",
                    "h1 is missing", "h1 element is missing",
                    "no level-1 heading", "no level 1 heading",
                    "no top-level heading", "lacks a level-1 heading",
                    "lacks a level 1 heading", "no heading level 1",
                    "heading level 1 is missing",
                )
                if any(p in issue_lower for p in no_h1_phrases):
                    logger.info(
                        "SC %s: dropping 'no h1' finding -- "
                        "capture_data.headings includes a level-1 heading",
                        self.criterion_id,
                    )
                    continue

            # Rule 7: SC 1.4.5 only — drop findings on elements that
            # render real DOM text over a CSS background image. Such an
            # element displays RENDERED HTML text, not an image of text,
            # so it cannot be a 1.4.5 failure (this is exactly the
            # CRITICAL SC 1.4.5 RULE the judge prompt already states).
            # Verified bug (loudoun.gov SC 1.4.5): 8 nav buttons with
            # real HTML text over a decorative photo were flagged as
            # images of text — the judge applied the rejection rule
            # inconsistently across batches.
            if self.criterion_id == "1.4.5" and bg_text_strings:
                finding_text = "".join((
                    (getattr(f, "element", "") or "")
                    + " " + (getattr(f, "issue", "") or "")
                ).split()).lower()
                if any(t in finding_text for t in bg_text_strings):
                    logger.info(
                        "SC 1.4.5: dropping image-of-text finding -- the "
                        "element renders real DOM text over a background "
                        "image (background_images.text_content is non-empty)",
                    )
                    continue

            # Rule 8: drop keyboard / focus / structure findings on
            # elements the browser keeps out of the tab order and off
            # the screen regardless of authoring (tab_reachable=False).
            # A focus leak or "not keyboard reachable" finding on a
            # display:none / inert / zero-rect element describes a
            # problem that does not exist. Scoped to the SCs where this
            # false-positive class occurs (verified on a university:
            # #modal-hero-video-caption flagged under 2.1.1/2.4.3/2.4.7).
            if (sel and sel in browser_handled and self.criterion_id in (
                "2.1.1", "2.1.2", "2.1.3", "2.4.3", "2.4.7", "2.4.11",
                "2.4.12", "4.1.2", "1.3.1",
            )):
                logger.info(
                    "SC %s: dropping finding on %s -- element is "
                    "browser-handled (tab_reachable=False); the focus "
                    "leak it describes does not exist",
                    self.criterion_id, sel,
                )
                continue

            # Rule 8b: drop a 'no accessible name' finding that axe
            # contradicts. ANDI does not honour clip/sr-only label spans;
            # axe's link-name/button-name/image-alt rules do. Only fires when
            # the finding claims a MISSING name (not a name-quality issue like
            # ambiguous/mismatched text) AND axe confirms a name for the
            # element (exact selector pass, or a page-clean axe name rule for
            # that element type).
            if self.criterion_id in ("1.1.1", "2.4.4", "2.4.9", "4.1.2", "2.5.3"):
                _ftxt = (
                    (getattr(f, "issue", "") or "") + " "
                    + (getattr(f, "recommendation", "") or "")
                ).lower()
                _claims_no_name = any(p in _ftxt for p in (
                    "no accessible name", "lacks an accessible name",
                    "lacks accessible name", "missing accessible name",
                    "empty accessible name", "without an accessible name",
                    "has no accessible name", "no accessible label",
                ))
                if _claims_no_name and axe_confirms_named(
                    _axe_name_summary, "", sel
                ):
                    logger.info(
                        "SC %s: dropping 'no accessible name' finding on %s -- "
                        "axe name-rule confirms an accessible name (ANDI misses "
                        "clip/sr-only label text); finding contradicted by axe",
                        self.criterion_id, sel or "(no selector)",
                    )
                    continue

            # Rule 8c: drop a judge_inference finding that asserts a specific
            # event-LISTENER registration fact ("a mousedown listener is
            # registered without a keydown equivalent") on a keyboard SC. We
            # capture no event-listener data, so the claim is ungrounded --
            # only code-AI over script_content could support it, and those
            # carry source=code_ai, not judge_inference. Verified on a university
            # 2026-05-28: a fabricated "mousedown without keydown" FP under
            # 2.1.1/2.1.3 (the page's script_content in fact has keydown).
            if (self.criterion_id in ("2.1.1", "2.1.2", "2.1.3", "2.1.4")
                    and getattr(f, "source", "") == "judge_inference"
                    and not _has_event_listener_capture):
                _kt = (
                    (getattr(f, "issue", "") or "") + " "
                    + (getattr(f, "recommendation", "") or "")
                ).lower()
                _asserts_listener = (
                    "event listener" in _kt or "event_listener" in _kt
                    or "mousedown" in _kt or "mouseup" in _kt
                    or "pointerdown" in _kt or "pointer event" in _kt
                )
                if _asserts_listener and (
                    "keydown" in _kt or "keyboard" in _kt
                    or "without" in _kt or "no corresponding" in _kt
                ):
                    logger.info(
                        "SC %s: dropping judge_inference finding asserting an "
                        "event-listener fact on %s -- no event-listener data was "
                        "captured, so the claim is ungrounded",
                        self.criterion_id, sel or "(no selector)",
                    )
                    continue

            # Rule 8d: WCAG logo/brand-name contrast exemption. Text (and
            # graphics) that are part of a logo or brand name have NO contrast
            # requirement (WCAG 1.4.3/1.4.6 incidental exception; 1.4.11
            # logo exemption). Drop contrast findings on logo/brand elements
            # (verified on a university 2026-05-28: visual_ai hallucinated 1.60:1 on
            # the maize-on-navy university brand mark in
            # #zone-branding > h1.logo; actual ~9:1 and exempt regardless).
            if self.criterion_id in ("1.4.3", "1.4.6", "1.4.11"):
                _le = (
                    (getattr(f, "css_selector", "") or "") + " "
                    + (getattr(f, "element", "") or "")
                ).lower()
                if "logo" in _le or "brand" in _le or "wordmark" in _le:
                    logger.info(
                        "SC %s: dropping contrast finding on logo/brand element "
                        "%s -- logo/brand-name text is exempt from WCAG contrast",
                        self.criterion_id, sel or "(no selector)",
                    )
                    continue

            # Rule 8e: WCAG H30 exception for SC 2.4.4 / 2.4.9 — links that
            # share the same accessible name AND the same destination need no
            # unique differentiation. Drop an "ambiguous / non-unique link
            # text" finding when every link carrying that name points to ONE
            # destination (verified on a university 2026-05-28: two 'Learn more
            # about the ceremony' links -> same article; a real ambiguity
            # ('this research' -> 2 different articles) still survives because
            # it maps to >1 href).
            if self.criterion_id in ("2.4.4", "2.4.9") and _link_name_to_hrefs:
                _itxt = (getattr(f, "issue", "") or "").lower()
                if any(p in _itxt for p in (
                    "same accessible name", "same name", "identical link",
                    "identical text", "non-unique", "not unique", "ambiguous",
                    "duplicate link",
                )):
                    _quoted = re.findall(r"['\"]([^'\"]{2,80})['\"]", getattr(f, "issue", "") or "")
                    _quoted += re.findall(r"['\"]([^'\"]{2,80})['\"]", getattr(f, "element", "") or "")
                    if _quoted and all(
                        len(_link_name_to_hrefs.get(q.strip().lower(), {None, ""})) <= 1
                        for q in _quoted
                        if q.strip().lower() in _link_name_to_hrefs
                    ) and any(q.strip().lower() in _link_name_to_hrefs for q in _quoted):
                        logger.info(
                            "SC %s: dropping ambiguous-link finding -- the links "
                            "share one destination (WCAG H30: same name + same "
                            "target needs no differentiation)",
                            self.criterion_id,
                        )
                        continue

            # Rule 8f: drop an "interactive control / lacks role-or-name"
            # finding on a DOM element that is display:none / visibility:
            # hidden / hidden. Such an element is not rendered, not
            # interactive, and not in the accessibility tree (verified
            # a university 2026-05-28: a display:none CrazyEgg tracking iframe was
            # flagged as an interactive control lacking a role/name).
            if (self.criterion_id in ("4.1.2", "2.1.1", "1.3.1", "2.4.3", "2.4.7")
                    and element_is_display_hidden(getattr(capture_data, "html", "") or "", sel)):
                _itxt2 = (getattr(f, "issue", "") or "").lower()
                if any(p in _itxt2 for p in (
                    "interactive control", "interactive element", "acts as",
                    "lacks an aria role", "lacks a role", "lacks an accessible name",
                    "no accessible name", "no role", "not programmatically",
                )):
                    logger.info(
                        "SC %s: dropping finding on %s -- element is display:none/"
                        "hidden in the DOM (not rendered, not interactive, not in "
                        "the a11y tree)",
                        self.criterion_id, sel or "(no selector)",
                    )
                    continue

            # Rule 9: SC 2.1.1 / 2.4.3 — drop a keyboard-unreachability
            # finding that contradicts a high captured tab-coverage. The
            # deterministic tab walk reached coverage_percent of all
            # interactive elements; only focusable_but_skipped entries
            # are genuinely unreachable. A finding claiming otherwise for
            # an element not on that list is fabricated (verified
            # a university SC 2.1.1: a "47 elements unreachable" finding
            # vs a measured 98.6% coverage / 1 skipped element).
            if (self.criterion_id in ("2.1.1", "2.4.3")
                    and coverage_pct is not None and coverage_pct >= 90.0):
                _unreach = (
                    "not reachable via the keyboard",
                    "not reachable via keyboard",
                    "unreachable via the keyboard", "unreachable via keyboard",
                    "not in the tab order", "not in the tab sequence",
                    "excluded from the tab order",
                    "cannot be reached via the keyboard",
                    "cannot be reached via keyboard", "not part of the tab",
                    "not keyboard reachable", "not reachable by keyboard",
                )
                if any(p in issue_lower for p in _unreach) and (
                    not sel or sel not in skipped_selectors
                ):
                    logger.info(
                        "SC %s: dropping keyboard-unreachability finding -- "
                        "captured tab-coverage is %.1f%% and the cited "
                        "element is not in focusable_but_skipped",
                        self.criterion_id, coverage_pct,
                    )
                    continue

            # Rule 9b: drop a keyboard-inaccessibility / low-coverage finding
            # when the tab walk is UNRELIABLE (reached ~0 of a page that
            # clearly has focusable elements, or got stuck on a bot-challenge).
            # The low coverage reflects the capture, not a keyboard barrier, so
            # a "0% coverage / not keyboard accessible / N unreachable" finding
            # is a capture artifact (verified on a university 2026-05-29: a Cloudflare-
            # truncated walk produced a bogus "reached 0 of 69" 2.1.1 finding).
            if (self.criterion_id in ("2.1.1", "2.1.2", "2.1.3", "2.4.3", "2.4.7")
                    and not _walk_reliability["reliable"]):
                _low_cov = (
                    "0% coverage", "0 of", "zero of", "none of the",
                    "not reachable via the keyboard", "not reachable via keyboard",
                    "unreachable via the keyboard", "unreachable via keyboard",
                    "not keyboard reachable", "not reachable by keyboard",
                    "cannot be reached via the keyboard", "not in the tab order",
                    "no elements", "no interactive elements were reached",
                    "keyboard inaccessible", "not accessible via keyboard",
                    "not operable", "low tab", "low keyboard coverage",
                )
                if any(p in issue_lower for p in _low_cov):
                    logger.info(
                        "SC %s: dropping low-coverage keyboard finding -- the tab "
                        "walk is unreliable (%s); coverage reflects capture, not "
                        "the page",
                        self.criterion_id, _walk_reliability["reason"],
                    )
                    continue

            # Rule 10: SC 2.4.11 / 2.4.12 — the focus-not-obscured
            # criteria can only fail when an author-placed fixed/sticky
            # element can overlap focused content. When the computed-
            # style scan found zero fixed/sticky elements, no obscuring
            # is possible (verified on a university: 2.4.11/2.4.12
            # "Partially Supports" rested on a misclassified missing-
            # focus-indicator finding, which belongs to SC 2.4.7).
            if (self.criterion_id in ("2.4.11", "2.4.12")
                    and no_fixed_or_sticky):
                logger.info(
                    "SC %s: dropping finding -- no fixed/sticky element "
                    "exists on the page, so focus cannot be obscured",
                    self.criterion_id,
                )
                continue

            # Rule 11: SC 1.4.13 — Content on Hover or Focus cannot fail
            # on an element whose hover/focus probe revealed no new
            # content. Also fires for compound (comma-separated)
            # selectors when every id-literal in the selector refers to
            # a no-reveal element — the judge often groups multiple
            # menu items in one finding's css_selector.
            if self.criterion_id == "1.4.13" and sel:
                if sel in no_reveal_selectors:
                    logger.info(
                        "SC 1.4.13: dropping finding on %s -- the "
                        "hover/focus probe revealed no content", sel,
                    )
                    continue
                _fids = set(_re.findall(r"#([\w-]+)", sel))
                if _fids and no_reveal_ids and _fids.issubset(no_reveal_ids):
                    logger.info(
                        "SC 1.4.13: dropping finding -- every id-literal in "
                        "selector %r refers to a no-reveal element", sel,
                    )
                    continue

            # Rule 12: SC 2.5.3 — Label in Name applies only to labels
            # presented VISUALLY. A finding on a screen-reader-only
            # element (sr-only / screen-reader-text / visually-hidden) is
            # a category error (verified on a university SC 2.5.3).
            if self.criterion_id == "2.5.3" and sel:
                _sl = sel.lower()
                if any(c in _sl for c in (
                    "sr-only", "screen-reader-text", "visually-hidden",
                    "visuallyhidden", "screenreader",
                )):
                    logger.info(
                        "SC 2.5.3: dropping finding on %s -- target is a "
                        "screen-reader-only label, not visually presented",
                        sel,
                    )
                    continue

            # Rule 13: SC 2.4.1 — drop "non-functional skip link"
            # findings on links the deterministic probe shows DO activate
            # via the keyboard (verified on a university: working skip links
            # called "non-functional" because click_activates was false
            # with a TimeoutError, even though keyboard_activates is True
            # — which is what 2.4.1 actually requires).
            if (self.criterion_id == "2.4.1"
                    and sel and sel in working_skip_links):
                _broken = (
                    "non-functional", "not functional", "fails to respond",
                    "does not function", "did not function",
                    "doesn't function", "doesn't activate",
                    "does not activate", "did not respond",
                    "failed to respond", "failed to activate",
                )
                if any(p in issue_lower for p in _broken):
                    logger.info(
                        "SC 2.4.1: dropping non-functional-skip-link finding "
                        "on %s -- skip_link_results shows keyboard_activates "
                        "True", sel,
                    )
                    continue

            # Rule 14: SC 2.4.7 — drop "no visible focus indicator"
            # findings on elements the focus_contrast probe MEASURED with
            # a visible indicator and >=3:1 contrast (verified
            # a university: a finding flagged nav-li-3 as having no focus
            # indicator, but focus_contrast measured has_change=True and
            # contrast_ratio=13.65).
            if (self.criterion_id == "2.4.7"
                    and sel and sel in well_focused_selectors):
                _nofocus = (
                    "no visible focus", "no focus indicator",
                    "lacks a visible focus", "lacks a focus indicator",
                    "missing focus indicator", "no visible indicator",
                    "without a focus indicator", "without focus indicator",
                    "lacks visible focus", "no discernible focus",
                )
                if any(p in issue_lower for p in _nofocus):
                    logger.info(
                        "SC 2.4.7: dropping no-focus-indicator finding on "
                        "%s -- focus_contrast measured has_change=True with "
                        "adequate contrast", sel,
                    )
                    continue

            # Rule 15: SC 2.2.4 — drop "auto-opening modal" findings when
            # the modal_interactions probe captured NO auto-open. The
            # judge sometimes infers an auto-modal from hidden-modal
            # textContent leaking into VISIBLE PAGE TEXT (verified
            # a university: the only modal in the DOM is class="hidden"
            # / tab_reachable:false and opens only on user click).
            if self.criterion_id == "2.2.4" and not has_auto_modal:
                _automodal = (
                    "automatically appears", "appears automatically",
                    "auto-opening", "auto-opens", "auto-open",
                    "opens automatically", "appears upon page load",
                    "appears on page load", "loads automatically",
                    "triggered automatically",
                    "automatic.*modal", "automatic.*popup",
                )
                if any(p in issue_lower for p in _automodal):
                    logger.info(
                        "SC 2.2.4: dropping auto-modal finding -- "
                        "modal_interactions captured no auto-open event",
                    )
                    continue

            kept.append(f)

        return kept

    def _deduplicate_findings(self, findings: list[Finding]) -> list[Finding]:
        """Remove duplicate findings based on element + issue similarity.

        Uses normalized element selectors and checks for substring overlap
        in issue text to catch near-duplicates from different analysis sources.
        """
        if len(findings) <= 1:
            return findings

        import re

        seen: dict[str, Finding] = {}
        severity_rank = {Severity.HIGH: 0, Severity.MEDIUM: 1, Severity.LOW: 2, Severity.INFO: 3}

        def _normalize_selector(s: str) -> str:
            s = s.lower().strip()
            s = re.sub(r':nth-(?:child|of-type)\(\d+\)', '', s)
            s = re.sub(r'\s*\(CSS:.*?\)', '', s)
            s = re.sub(r'\s+', ' ', s)
            return s

        def _normalize_issue(s: str) -> str:
            s = s.lower().strip()
            s = re.sub(r'["\'](.*?)["\']', 'VALUE', s)
            s = re.sub(r'\d+(\.\d+)?', 'NUM', s)
            s = re.sub(r'\s+', ' ', s)
            return s

        for f in findings:
            elem_key = _normalize_selector(f.element or "")
            issue_key = _normalize_issue(f.issue or "")
            key = f"{elem_key}|||{issue_key}"

            existing = seen.get(key)
            if existing is None:
                seen[key] = f
            else:
                existing_rank = severity_rank.get(existing.severity, 4)
                new_rank = severity_rank.get(f.severity, 4)
                if new_rank < existing_rank:
                    seen[key] = f
                elif new_rank == existing_rank and f.source == "programmatic":
                    seen[key] = f

        deduped = list(seen.values())
        removed = len(findings) - len(deduped)
        if removed > 0:
            logger.info(
                "SC %s: deduplicated %d -> %d findings (%d exact duplicates removed)",
                self.criterion_id, len(findings), len(deduped), removed,
            )

        return deduped

    # ------------------------------------------------------------------
    # Finding location enrichment
    # ------------------------------------------------------------------

    def _enrich_finding_locations(
        self,
        findings: list[Finding],
        capture_data: CaptureData,
    ) -> list[Finding]:
        """Attach a human-readable page-location sentence to every finding.

        Looks up each finding's element by its CSS selector (or by an
        embedded selector inside the element string) in the captured
        rect data, then asks `_describe_location` to compose a sentence
        like "Near the top of the page in the primary navigation menu,
        on the 'Mobile menu toggle' button."

        The sentence is written to ``f.location`` — a separate field
        from ``f.element`` — so the AI's existing element description
        is preserved and the location reads as additive context in the
        report and judge prompt.
        """
        if not findings:
            return findings

        # selector → {rect, text, tag} lookup from every captured source
        # that records bounding rects.
        rect_lookup: dict[str, dict] = {}
        for source_list in (
            capture_data.images,
            capture_data.links,
            capture_data.form_fields,
            capture_data.tab_walk,
            capture_data.tables,
            capture_data.headings,
            capture_data.media,
            capture_data.iframes,
            capture_data.skip_links,
        ):
            for item in source_list:
                sel = item.get("selector", "")
                if sel and sel not in rect_lookup:
                    rect_lookup[sel] = {
                        "rect": item.get("rect"),
                        "text": item.get("text", ""),
                        "tag": item.get("tag", ""),
                    }

        # Page height (for vertical-zone phrasing) — pull from the largest
        # captured rect's bottom edge as a proxy when full_page dimensions
        # aren't otherwise stored.
        page_height = 0.0
        for info in rect_lookup.values():
            r = info.get("rect")
            if not r:
                continue
            bottom = r.get("y", 0) + r.get("height", 0)
            if bottom > page_height:
                page_height = bottom

        for f in findings:
            if f.location:
                continue  # already enriched (e.g. on a previous pass)

            # Prefer the explicit css_selector field; fall back to scanning
            # the element string for a selector-shaped token.
            sel_candidates = []
            if f.css_selector:
                sel_candidates.append(f.css_selector)
            if f.element:
                sel_candidates.append(f.element)

            info: dict = {}
            for cand in sel_candidates:
                if cand in rect_lookup:
                    info = rect_lookup[cand]
                    break

            sentence = _describe_location(
                selector=f.css_selector or "",
                rect=info.get("rect"),
                text=info.get("text", ""),
                tag=info.get("tag", ""),
                landmarks=capture_data.landmarks,
                headings=capture_data.headings,
                page_height=page_height,
            )
            if sentence:
                f.location = sentence

        return findings

    # ------------------------------------------------------------------
    # AI finding enrichment (detail pass)
    # ------------------------------------------------------------------

    async def _enrich_findings_with_ai(
        self,
        findings: list[Finding],
        capture_data: CaptureData,
        ai_client: Any,
    ) -> list[Finding]:
        """Send all findings + screenshots to AI for detailed descriptions.

        This is the final pass — the AI sees the page screenshots AND
        the raw findings, and rewrites each finding with:
        - Exact visual location on the page (natural language)
        - What the element actually looks like / contains
        - Whether the finding is a real issue in context
        - Developer-friendly description of how to fix it
        - Specific code changes needed

        This produces Trusted Tester-grade output.
        """
        if not findings:
            return findings

        # Build the findings summary for the AI
        findings_text = []
        for i, f in enumerate(findings, 1):
            sev = f.severity.value if hasattr(f.severity, "value") else str(f.severity)
            findings_text.append(
                f"{i}. [{sev.upper()}] (source: {f.source})\n"
                f"   Element: {f.element}\n"
                f"   Issue: {f.issue}\n"
                f"   Impact: {f.impact}\n"
                f"   Recommendation: {f.recommendation}"
            )

        # Page context for the AI
        page_info = (
            f"PAGE: {capture_data.url or '(document)'}\n"
            f"TITLE: {capture_data.title or '(untitled)'}\n"
        )
        if capture_data.file_type:
            page_info += f"FILE TYPE: {capture_data.file_type}\n"

        prompt = (
            f"WCAG CRITERION: {self.criterion_id} — {self.criterion_name} "
            f"(Level {self.level})\n"
            f"NORMATIVE TEXT: {self.normative_text}\n\n"
            f"{page_info}\n"
            f"Below are {len(findings)} accessibility findings from "
            f"automated testing (both programmatic analysis and AI visual "
            f"analysis). You are seeing the page screenshots.\n\n"
            f"For EACH finding, rewrite ALL fields to be detailed enough "
            f"for a developer to locate and fix the issue using ONLY your "
            f"enriched description — they will NOT have access to the page.\n\n"
            f"REQUIREMENTS for each finding:\n"
            f"1. ELEMENT: Describe WHERE on the page this is using visual "
            f"location that a developer can find: 'In the top navigation "
            f"bar, the 3rd menu item labeled Products' or 'The hero image "
            f"showing a city skyline, below the main heading, spanning the "
            f"full width of the page'. Include the CSS selector AND an HTML "
            f"snippet showing the relevant markup.\n"
            f"2. ISSUE: State exactly what is wrong with specific values. "
            f"BAD: 'Insufficient contrast'. GOOD: 'Text \"Subscribe\" has "
            f"contrast ratio 2.3:1 (foreground #999 on background #fff), "
            f"which fails the 4.5:1 minimum required by WCAG 1.4.3 for "
            f"normal-size text (14px, weight 400).' Include the actual "
            f"attribute values, computed styles, or missing properties.\n"
            f"3. IMPACT: Name SPECIFIC disability groups AND assistive "
            f"technologies: 'Screen reader users using JAWS or NVDA will "
            f"hear the filename DSC_0042.jpg announced instead of a "
            f"description of the building photo.'\n"
            f"4. RECOMMENDATION: State what the WCAG passing condition is "
            f"and what needs to change — but do NOT provide specific code "
            f"fixes, HTML snippets, or implementation details. Just describe "
            f"what passing looks like. Example: 'This image needs a text "
            f"alternative that conveys the same information the image "
            f"communicates visually, or it must be marked as decorative if "
            f"it serves no informational purpose.'\n\n"
            f"If a finding is a FALSE POSITIVE based on what you see in "
            f"the screenshots (e.g., a decorative border image correctly "
            f"has no alt text, or a transparent background composites onto "
            f"a dark parent), set severity to 'info' and explain why it is "
            f"not a real issue.\n\n"
            f"Respond with a JSON array of objects, one per finding, with "
            f"keys: element, issue, impact, recommendation, severity "
            f"(high/medium/low/info). Maintain the same order as input.\n\n"
            f"FINDINGS:\n" + "\n\n".join(findings_text)
        )

        # Gather images — full page + viewport + criterion-specific extras
        image_paths: list[str] = []
        if capture_data.full_page_path:
            image_paths.append(capture_data.full_page_path)
        if capture_data.viewport_path:
            image_paths.append(capture_data.viewport_path)
        if capture_data.viewport_200pct_path:
            image_paths.append(capture_data.viewport_200pct_path)
        # Add criterion-specific extra images (subclass overrides)
        extra = self.get_extra_images(capture_data)
        if extra:
            image_paths.extend(extra)

        # Send all available images for VPAT enrichment
        enrichment_images = image_paths

        try:
            from functions.llm import LLMClient
            from functions.parser import get_content_text
            import json as _json
            import re as _re

            system_prompt = (
                "You are a certified DHS Trusted Tester writing the "
                "Remarks and Explanations column for a VPAT 2.5 "
                "Accessibility Conformance Report (ACR).\n\n"
                "Per the VPAT 2.5 standard, remarks for Partially "
                "Supports or Does Not Support must:\n"
                "1. Identify the functions or features with issues\n"
                "2. Describe how they do not fully support the criterion\n"
                "3. Note any workarounds if they exist\n\n"
                "Do NOT provide code fixes or implementation details. "
                "Describe what is wrong and what passing looks like.\n\n"
                "Respond ONLY with a JSON array."
            )

            llm = LLMClient()
            response = await llm.call(
                system_prompt=system_prompt,
                user_prompt=prompt,
                images=enrichment_images or None,
                temperature=0.2,
                label=f"finding_enrichment_{self.criterion_id.replace('.', '_')}",
            )
            content = get_content_text(response).strip()
            content = _re.sub(r"```(?:json)?\s*\n?", "", content)
            content = content.strip("`").strip()

            enriched_list = _json.loads(content)

            if isinstance(enriched_list, list) and len(enriched_list) == len(findings):
                for i, enriched in enumerate(enriched_list):
                    if not isinstance(enriched, dict):
                        continue
                    f = findings[i]
                    # For programmatic findings, preserve the original
                    # deterministic element/issue and only enrich impact
                    # and recommendation. For AI findings, allow full rewrite.
                    if f.source == "programmatic":
                        # Only enrich location context in element field —
                        # prepend AI location but keep original selector
                        ai_elem = str(enriched.get("element", ""))
                        if ai_elem and f.element and ai_elem != f.element:
                            f.element = f"{ai_elem} — {f.element}"
                        # Never overwrite programmatic issue — it's deterministic
                        # Only enrich impact and recommendation
                        if enriched.get("impact"):
                            f.impact = str(enriched["impact"])
                        if enriched.get("recommendation"):
                            f.recommendation = str(enriched["recommendation"])
                    else:
                        # AI/code_ai findings: allow full rewrite
                        if enriched.get("element"):
                            f.element = str(enriched["element"])
                        if enriched.get("issue"):
                            f.issue = str(enriched["issue"])
                        if enriched.get("impact"):
                            f.impact = str(enriched["impact"])
                        if enriched.get("recommendation"):
                            f.recommendation = str(enriched["recommendation"])
                    # Allow AI to downgrade false positives to info
                    new_sev = enriched.get("severity", "")
                    if new_sev:
                        sev_map = {v.value: v for v in Severity}
                        if new_sev.lower() in sev_map:
                            f.severity = sev_map[new_sev.lower()]

                logger.info(
                    "SC %s: AI enriched %d findings with detailed descriptions",
                    self.criterion_id, len(findings),
                )
            else:
                logger.warning(
                    "SC %s: AI enrichment returned %d items for %d findings — skipping",
                    self.criterion_id,
                    len(enriched_list) if isinstance(enriched_list, list) else 0,
                    len(findings),
                )

        except Exception as exc:
            logger.debug(
                "SC %s: AI finding enrichment failed: %s",
                self.criterion_id, exc,
            )

        return findings

    # ------------------------------------------------------------------
    # Off-scope keyword merging
    # ------------------------------------------------------------------

    def _get_effective_off_scope_keywords(self) -> dict[str, list[str]]:
        """Merge auto-generated off-scope keywords with check-specific ones.

        Auto keywords are derived from the criterion prefix (e.g. "1.1").
        The check's own ``off_scope_keywords`` overlay on top, so
        check-specific overrides always win.
        """
        # Determine prefix (first two segments, e.g. "1.1" from "1.1.1")
        parts = self.criterion_id.split(".")
        prefix = ".".join(parts[:2]) if len(parts) >= 2 else self.criterion_id

        # Only return check-specific keywords explicitly set by the
        # check author.  Auto off-scope filtering was removed — the
        # judge AI handles cross-criterion relevance.
        merged: dict[str, list[str]] = {}
        if self.off_scope_keywords:
            for category, keywords in self.off_scope_keywords.items():
                merged[category] = list(keywords) if isinstance(keywords, list) else keywords

        return merged

    # ------------------------------------------------------------------
    # Off-scope filtering
    # ------------------------------------------------------------------

    def _filter_off_scope_findings(
        self, findings: list[Finding]
    ) -> list[Finding]:
        """Remove AI findings that are primarily about off-scope topics.

        Filters when the off-scope keyword appears in the ISSUE text.
        For check-specific off-scope keywords (explicitly set by the
        check author), ALL severities are filtered including HIGH.
        For auto-generated off-scope keywords, HIGH severity findings
        are kept for human review.
        """
        effective = self._get_effective_off_scope_keywords()
        if not effective:
            return findings

        # Determine which keywords are check-specific vs auto-generated
        check_specific_keywords: set[str] = set()
        if self.off_scope_keywords:
            for keywords in self.off_scope_keywords.values():
                if isinstance(keywords, list):
                    check_specific_keywords.update(kw.lower() for kw in keywords)

        filtered: list[Finding] = []
        for f in findings:
            issue_text = f"{f.issue} {f.recommendation}".lower()
            is_off_scope = False
            matched_check_specific = False
            for category, keywords in effective.items():
                for kw in keywords:
                    if kw.lower() in issue_text:
                        is_off_scope = True
                        if kw.lower() in check_specific_keywords:
                            matched_check_specific = True
                        break
                if is_off_scope:
                    break

            if is_off_scope:
                # Check-specific keywords: always filter (the check
                # author explicitly said this topic is wrong criterion)
                if matched_check_specific:
                    logger.debug(
                        "Filtering off-scope finding for SC %s "
                        "(check-specific keyword, sev=%s): %s",
                        self.criterion_id, f.severity.value if hasattr(f.severity, 'value') else f.severity,
                        f.issue,
                    )
                    continue
                # Auto-generated keywords: keep HIGH for human review
                elif f.severity == Severity.HIGH:
                    filtered.append(f)
                    continue
                else:
                    logger.debug(
                        "Filtering off-scope AI finding for SC %s "
                        "(auto keyword, sev=%s): %s",
                        self.criterion_id,
                        f.severity.value if hasattr(f.severity, 'value') else f.severity,
                        f.issue,
                    )
                    continue
            # Not off-scope — keep the finding
            filtered.append(f)
        return filtered

    # ------------------------------------------------------------------
    # Conformance determination helper
    # ------------------------------------------------------------------

    def _determine_conformance(
        self,
        findings: list[Finding],
        total_elements: int = 0,
    ) -> ConformanceLevel:
        """Determine conformance level from a list of findings."""
        if not findings:
            return ConformanceLevel.SUPPORTS

        high_count = sum(
            1 for f in findings
            if f.severity in (Severity.HIGH, "high")
        )
        medium_count = sum(
            1 for f in findings
            if f.severity in (Severity.MEDIUM, "medium")
        )

        if high_count > 0:
            if total_elements > 0:
                # Proportional: most elements fail → Does Not Support
                if high_count >= total_elements * 0.5:
                    return ConformanceLevel.DOES_NOT_SUPPORT
                if high_count < total_elements * 0.3:
                    return ConformanceLevel.PARTIALLY_SUPPORTS
                return ConformanceLevel.DOES_NOT_SUPPORT
            else:
                # No element count: multiple highs = DNS, single = PS
                if high_count >= 3:
                    return ConformanceLevel.DOES_NOT_SUPPORT
                return ConformanceLevel.PARTIALLY_SUPPORTS

        if medium_count > 0:
            if total_elements > 0 and medium_count >= total_elements * 0.5:
                return ConformanceLevel.DOES_NOT_SUPPORT
            return ConformanceLevel.PARTIALLY_SUPPORTS

        # Only low/info findings — these are not conformance failures
        return ConformanceLevel.SUPPORTS

    # ------------------------------------------------------------------
    # Dynamic confidence computation
    # ------------------------------------------------------------------

    def _compute_confidence(
        self,
        findings: list[Finding],
        capture_data: CaptureData,
        total_elements: int = 0,
        has_deterministic_data: bool = True,
    ) -> float:
        """Compute confidence dynamically from available evidence.

        Factors:
        - More elements checked → higher confidence
        - Interactive data present (tab_walk, focus_indicators) → higher confidence
        - Screenshots available → higher confidence (AI can verify)
        - No relevant elements found → high confidence for N/A
        - Very few elements → lower confidence (small sample)
        """
        base = 0.5  # Start at 50%

        # Deterministic programmatic data available
        if has_deterministic_data:
            base = 0.7

        # Element coverage
        if total_elements > 20:
            base = min(base + 0.15, 1.0)  # Many elements checked
        elif total_elements > 5:
            base = min(base + 0.10, 1.0)
        elif total_elements > 0:
            base = min(base + 0.05, 1.0)

        # Interactive data boosts confidence for keyboard/focus criteria
        if capture_data.tab_walk:
            base = min(base + 0.05, 1.0)
        if capture_data.tab_coverage and capture_data.tab_coverage.get("total_interactive", 0) > 0:
            base = min(base + 0.05, 1.0)
        if capture_data.keyboard_walkthrough_video:
            base = min(base + 0.05, 1.0)

        # Screenshot availability
        if capture_data.full_page_path:
            base = min(base + 0.03, 1.0)
        if capture_data.viewport_200pct_path:
            base = min(base + 0.02, 1.0)

        # A11y tree available
        if capture_data.a11y_tree:
            base = min(base + 0.05, 1.0)

        return round(base, 3)

    # ------------------------------------------------------------------
    # TT sub-test result generation
    # ------------------------------------------------------------------

    def _generate_tt_results(
        self,
        findings: list[Finding],
        capture_data: CaptureData,
    ) -> list[TTSubTestResult]:
        """Map findings to Trusted Tester sub-test results.

        By default, if there are any high/medium findings, all TT tests
        fail; otherwise they pass. This is a SIMPLIFIED mapping — the
        actual conformance verdict is determined by the judge AI, not
        these TT results. Subclasses may override for more granular
        mapping.
        """
        if not self.tt_tests:
            return []

        has_failures = any(
            f.severity in (Severity.HIGH, Severity.MEDIUM, "high", "medium")
            for f in findings
        )

        results: list[TTSubTestResult] = []
        for tt_id in self.tt_tests:
            results.append(TTSubTestResult(
                tt_id=tt_id,
                name=f"TT {tt_id} for SC {self.criterion_id}",
                result=TTResult.FAIL if has_failures else TTResult.PASS,
            ))
        return results



def _format_patterns_for_judge(patterns: list) -> str:
    """Render the filtered pattern list as a compact prompt block."""
    lines = []
    for i, p in enumerate(patterns, 1):
        lines.append(
            f"[{i}] pattern_type={p.get('pattern_type', '')!s}"
        )
        lines.append(f"    element: {p.get('element', '')}")
        sel = p.get("css_selector", "")
        if sel:
            lines.append(f"    selector: {sel}")
        lines.append(f"    issue: {p.get('issue', '')}")
        lines.append(f"    severity: {p.get('severity', 'medium')}")
        sc_ids = p.get("sc_ids", []) or []
        lines.append(f"    sc_ids: {sc_ids}")
        src = p.get("source_chunk", "")
        if src:
            lines.append(f"    source: {src}")
        evidence = (p.get("raw_evidence", "") or "").rstrip()
        if evidence:
            lines.append(f"    raw_evidence:")
            for ev_line in evidence.split("\n"):
                lines.append(f"      | {ev_line}")
        lines.append("")
    return "\n".join(lines)
