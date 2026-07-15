"""WCAG Guideline 1.1 - Text Alternatives checks."""
from __future__ import annotations

import re

from checks.base import BaseCheck, _make_finding_id
from models import (
    CaptureData,
    ConformanceLevel,
    Finding,
    Severity,
    TTResult,
    TTSubTestResult,
)

# File-extension-only alt text (deterministic — always wrong)
_FILE_EXT_RE = re.compile(
    r"^[\w\-]+\.(jpe?g|png|gif|svg|bmp|webp|ico|tiff?)$", re.IGNORECASE
)


def _is_suspicious_alt(alt: str) -> bool:
    """Fast deterministic check for obviously non-meaningful alt text.

    Only catches the clearest failures (empty, whitespace-only, pure
    filename).  All other alt text quality judgments are handled by the
    AI during visual/code analysis — the AI sees the image and its
    context and can determine if 'logo' is appropriate (company logo
    in header) or not (decorative image labeled 'logo').
    """
    alt = alt.strip()
    if not alt:
        return True
    if _FILE_EXT_RE.match(alt):
        return True
    # Pure numbers or single punctuation — clearly not descriptive
    if re.match(r"^\d+$", alt) or re.match(r"^\s*$", alt):
        return True
    return False


def _is_decorative(img: dict) -> bool:
    """Heuristic: image is intentionally decorative."""
    # Explicit role=presentation or role=none
    role = (img.get("role") or "").lower()
    if role in ("presentation", "none"):
        return True
    # alt="" is an explicit decorative marker
    alt = img.get("alt")
    if alt is not None and alt == "":
        # Check if aria-hidden is also set
        if img.get("aria_hidden") or img.get("aria-hidden"):
            return True
        return True
    return False


# Known decorative filename patterns (spacers, separators, backgrounds)
_DECORATIVE_FILENAME_PATTERNS = {
    "spacer", "pixel", "blank", "transparent", "separator", "divider",
    "bg", "background", "shadow", "gradient", "wave", "border",
    "arrow", "bullet", "dot", "line", "rule",
}


def _likely_misclassified_decorative(img: dict) -> str | None:
    """Check if an image marked as decorative (alt='') is likely meaningful content.

    Returns a reason string if the image looks like it should have alt text,
    or None if it genuinely appears decorative.
    """
    src = img.get("src", "")
    rect = img.get("rect", {})
    width = rect.get("width", 0)
    height = rect.get("height", 0)
    parent_tag = (img.get("parent_tag") or "").lower()
    parent_role = (img.get("parent_role") or "").lower()

    # Small images are likely decorative icons/spacers
    if width > 0 and height > 0 and (width < 50 or height < 50):
        return None

    # SVG patterns (wave, shadow, etc.) are typically decorative
    if src.endswith(".svg"):
        return None

    # Known decorative filename patterns
    filename = src.rsplit("/", 1)[-1].rsplit("\\", 1)[-1].lower()
    filename_base = filename.rsplit(".", 1)[0] if "." in filename else filename
    # Strip numbers and hyphens for matching
    parts = filename_base.replace("-", " ").replace("_", " ").split()
    if any(p in _DECORATIVE_FILENAME_PATTERNS for p in parts):
        return None

    reasons = []

    # Large content image with alt="" — likely meaningful
    if width >= 100 and height >= 100:
        reasons.append(f"large image ({width}x{height}px)")

    # Image inside a link — it IS the link content, needs alt
    if parent_tag == "a" or parent_role == "link":
        reasons.append("inside a link (serves as link content)")

    # Content image formats (jpg, avif, webp usually = photos/content)
    content_extensions = {".jpg", ".jpeg", ".png", ".avif", ".webp"}
    ext = "." + filename.rsplit(".", 1)[-1] if "." in filename else ""
    if ext in content_extensions and width >= 100:
        reasons.append(f"content image format ({ext})")

    if reasons:
        return "; ".join(reasons)
    return None


class Check_1_1_1(BaseCheck):
    """SC 1.1.1 Non-text Content (Level A)."""

    criterion_id = "1.1.1"
    criterion_name = "Non-text Content"
    level = "A"
    wcag_versions = ["2.0", "2.1", "2.2"]
    guideline = "1.1 Text Alternatives"
    principle = "1. Perceivable"
    ict_baseline = "6"
    tt_tests = ["6.A", "6.B"]
    normative_text = (
        "All non-text content that is presented to the user has a text "
        "alternative that serves the equivalent purpose, except for the "
        "situations listed below: Controls/Input, Time-Based Media, Test, "
        "Sensory, CAPTCHA, Decoration/Formatting/Invisible."
    )
    off_scope_keywords = {
        "contrast": ["contrast ratio", "color contrast"],
        "keyboard": ["keyboard accessible", "tab order"],
        "focus": ["focus indicator", "focus visible"],
    }

    def is_applicable(self, capture_data: CaptureData) -> bool:
        if (
            capture_data.images
            or capture_data.background_images
            or capture_data.media
            or capture_data.iframes
            or capture_data.captchas
        ):
            return True
        # SVG elements stored in images with tag="svg"
        if any(
            (img.get("tag") or "").lower() == "svg" for img in capture_data.images
        ):
            return True
        # input[type=image] in form_fields
        if any(
            (ff.get("type") or "").lower() == "image"
            for ff in capture_data.form_fields
        ):
            return True
        # <canvas> in the HTML
        if "<canvas" in (capture_data.html or ""):
            return True
        return False

    # Send ALL per-image screenshot paths so the AI can visually verify
    # whether each image is decorative vs meaningful.
    _SCREENSHOT_FIELDS = [("images", "screenshot_path")]

    async def run_programmatic(
        self, capture_data: CaptureData
    ) -> tuple[ConformanceLevel, float, list[Finding]]:
        findings: list[Finding] = []
        total_images = len(capture_data.images)
        # Tracks whether any finding was produced by a HEURISTIC rule
        # (e.g. "image marked decorative but is large + .webp = probably
        # meaningful"). Heuristics are guesses, not facts; when present
        # they must lower confidence below DETERMINISTIC_CONFIDENCE_FLOOR
        # so the full AI pipeline escalates and the visual AI can verify
        # the image instead of the judge rubber-stamping the heuristic
        # in fast-path VPAT-synthesis mode.
        has_heuristic_finding = False

        for img in capture_data.images:
            # Tracking pixels / analytics beacons are filtered upstream
            # in capture/web_capture.py -- they are out of scope for
            # WCAG 1.1.1 (not presented to the user). If you ever see
            # one slip through here, fix the upstream filter, not this
            # loop.
            selector = img.get("selector") or img.get("src") or f"img[index={img.get('index', '?')}]"
            src = img.get("src", "")
            alt = img.get("alt")
            role = img.get("role", "")
            aria_label = img.get("aria_label", img.get("aria-label", ""))
            aria_labelledby = img.get("aria_labelledby", img.get("aria-labelledby", ""))
            title = img.get("title", "")

            # Check decorative images for possible misclassification.
            # This is a HEURISTIC (size + extension based), not a fact:
            # a hero photograph with redundant overlay text legitimately
            # uses alt="". We surface it for visual AI verification by
            # dropping confidence below the fast-path floor.
            if _is_decorative(img):
                misclass_reason = _likely_misclassified_decorative(img)
                if misclass_reason:
                    has_heuristic_finding = True
                    findings.append(Finding(
                        id=_make_finding_id(),
                        element=selector,
                        issue=(
                            f"Image is marked as decorative (alt=\"\") and may "
                            f"or may not need alt text -- needs visual review "
                            f"to confirm. Heuristic flag: {misclass_reason}. "
                            f"src=\"{src or ''}\""
                        ),
                        impact=(
                            "If this image conveys information that the "
                            "surrounding text does not, screen reader users "
                            "will miss it. If the image is purely decorative "
                            "or its content is duplicated in adjacent text, "
                            "alt=\"\" is the correct choice and no impact exists."
                        ),
                        recommendation=(
                            "Visual review required. If the image conveys "
                            "information not present in adjacent text, add "
                            "descriptive alt text. If decorative or redundant "
                            "to surrounding text, alt=\"\" is correct."
                        ),
                        severity=Severity.LOW,
                    ))
                continue

            # Missing-alt detection moved to ANDI gANDI
            # (BaseCheck._extract_andi_graphics_findings) and axe-core's
            # image-alt rule. ANDI is the authoritative source here
            # because it also derives the in-link / in-button context
            # (image-only link with no other text → the LINK has no name,
            # severity uplift) which neither axe nor a simple alt-presence
            # check provides. This block previously duplicated both, so it
            # was producing a third copy of the same finding for the judge
            # to dedup. The remaining checks (suspicious alt, filename-as-
            # alt, VLM semantic mismatch) are unique to our pipeline and
            # stay.
            if alt is None and not aria_label and not aria_labelledby and not title:
                continue

            # Check 2: Suspicious / non-meaningful alt text
            effective_alt = alt if alt is not None else (aria_label or title or "")
            if effective_alt and _is_suspicious_alt(effective_alt):
                findings.append(Finding(
                    id=_make_finding_id(),
                    element=selector,
                    issue=(
                        f"Image has suspicious alt text: \"{effective_alt}\" "
                        f"(src=\"{src or ''}\")"
                    ),
                    impact=(
                        "Screen reader users receive non-meaningful text that "
                        "does not convey the image content."
                    ),
                    recommendation=(
                        "Replace with descriptive text that conveys the purpose "
                        "and content of the image, or use alt=\"\" if decorative."
                    ),
                    severity=Severity.MEDIUM,
                ))
                continue

            # Check 3: Alt text that is just the filename
            if effective_alt and src:
                filename = src.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
                if effective_alt.strip().lower() == filename.lower():
                    findings.append(Finding(
                        id=_make_finding_id(),
                        element=selector,
                        issue=f"Image alt text is the filename: \"{effective_alt}\"",
                        impact=(
                            "Screen reader users hear a filename instead of "
                            "a meaningful description."
                        ),
                        recommendation=(
                            "Replace with descriptive text that conveys the "
                            "image content."
                        ),
                        severity=Severity.MEDIUM,
                    ))
                    continue

            # Note: alt text length is NOT checked with a hardcoded
            # threshold.  Whether alt text is "too long" depends on the
            # image complexity — a chart may need 300 chars while a
            # decorative icon needs 0.  The AI evaluates this in context
            # during visual analysis.

            # ── Semantic alt-vs-caption verification ──────────────────
            # When the capture pipeline already produced a VLM caption for
            # this image, compare it against the author-supplied alt via
            # bge-m3 embeddings. Low cosine similarity means the alt
            # probably does not describe what the image actually shows.
            # This catches the "alt='company logo' on a photo of a
            # sunset" class of failure that programmatic filename checks
            # miss entirely.
            vlm_caption = (img.get("vlm_caption") or "").strip()
            effective_for_semantic = alt or aria_label or title
            if (
                vlm_caption
                and effective_for_semantic
                and effective_for_semantic.strip()
                and not _is_suspicious_alt(effective_for_semantic)
            ):
                similarity = img.get("vlm_alt_similarity")
                if isinstance(similarity, (int, float)):
                    sim = float(similarity)
                    # 0.55 is the same threshold verify_alt_text_semantic
                    # uses; below this, alt demonstrably doesn't match
                    # what the image depicts.
                    if sim < 0.45:
                        findings.append(Finding(
                            id=_make_finding_id(),
                            element=selector,
                            issue=(
                                f"Alt text does not semantically match the "
                                f"image. Alt says: \"{effective_for_semantic}\". "
                                f"VLM caption of the actual image: "
                                f"\"{vlm_caption}\". Similarity {sim:.2f} is "
                                f"below the 0.45 mismatch threshold."
                            ),
                            impact=(
                                "Screen reader users hear text that does not "
                                "describe what sighted users see. They get a "
                                "wrong mental model of the page."
                            ),
                            recommendation=(
                                "Replace the alt text with a description that "
                                "actually matches the image content, or mark "
                                "the image decorative if it no longer conveys "
                                "information."
                            ),
                            severity=Severity.HIGH,
                            evidence=(
                                f"VLM caption: {vlm_caption} | "
                                f"Similarity: {sim:.4f}"
                            ),
                        ))
                    elif sim < 0.55:
                        findings.append(Finding(
                            id=_make_finding_id(),
                            element=selector,
                            issue=(
                                f"Alt text only weakly matches the image. "
                                f"Alt: \"{effective_for_semantic}\". VLM "
                                f"caption: \"{vlm_caption}\". Similarity "
                                f"{sim:.2f} (flagged when < 0.55)."
                            ),
                            impact=(
                                "Screen reader users may get an incomplete "
                                "mental model of the image."
                            ),
                            recommendation=(
                                "Verify the alt text accurately describes "
                                "what is shown in the image."
                            ),
                            severity=Severity.MEDIUM,
                            evidence=(
                                f"VLM caption: {vlm_caption} | "
                                f"Similarity: {sim:.4f}"
                            ),
                        ))

        # Check background images that may convey information
        for bg in capture_data.background_images:
            selector = bg.get("selector", "unknown element")
            text_content = (bg.get("text_content") or "").strip()
            role = bg.get("role", "")
            aria_label = bg.get("aria_label", bg.get("aria-label", ""))

            # If a background image element has no text and no aria-label,
            # it may be conveying information without a text alternative
            if not text_content and not aria_label and role not in ("presentation", "none"):
                findings.append(Finding(
                    id=_make_finding_id(),
                    element=selector,
                    issue=(
                        "Element with CSS background image has no text content "
                        "or accessible name"
                    ),
                    impact=(
                        "If this background image conveys information, screen "
                        "reader users will miss it."
                    ),
                    recommendation=(
                        "If the background image conveys information, add a "
                        "text alternative via aria-label, visually-hidden text, "
                        "or role=\"img\" with alt text. If decorative, no action "
                        "needed."
                    ),
                    severity=Severity.LOW,
                ))

        # Check CAPTCHAs for text alternatives
        for captcha in capture_data.captchas:
            selector = captcha.get("selector", "CAPTCHA")
            alt = captcha.get("alt", "")
            aria_label = captcha.get("aria_label", captcha.get("aria-label", ""))
            has_alternative = captcha.get("has_alternative", False)

            if not alt and not aria_label:
                findings.append(Finding(
                    id=_make_finding_id(),
                    element=selector,
                    issue="CAPTCHA element has no text alternative",
                    impact=(
                        "Users who cannot perceive the CAPTCHA have no way to "
                        "identify what it is or complete it."
                    ),
                    recommendation=(
                        "Provide a text alternative that describes the purpose "
                        "of the CAPTCHA and offer an alternative CAPTCHA type "
                        "for different disabilities."
                    ),
                    severity=Severity.HIGH,
                ))

        # Check SVG elements for accessible names
        for img in capture_data.images:
            if (img.get("tag") or "").lower() != "svg":
                continue
            selector = img.get("selector", "svg")
            role = (img.get("role") or "").lower()
            aria_label = img.get("aria_label", img.get("aria-label", ""))
            aria_labelledby = img.get(
                "aria_labelledby", img.get("aria-labelledby", "")
            )
            title_el = img.get("title", "")

            if role != "img":
                findings.append(Finding(
                    id=_make_finding_id(),
                    element=selector,
                    issue=(
                        "SVG element is missing role=\"img\""
                    ),
                    impact=(
                        "Without role=\"img\", assistive technologies may not "
                        "recognise the SVG as an image and may skip it or "
                        "expose its internal markup."
                    ),
                    recommendation=(
                        "Add role=\"img\" to the <svg> element so assistive "
                        "technologies treat it as a single image."
                    ),
                    severity=Severity.MEDIUM,
                ))

            if not aria_label and not aria_labelledby and not title_el:
                findings.append(Finding(
                    id=_make_finding_id(),
                    element=selector,
                    issue=(
                        "SVG element has no accessible name (no aria-label, "
                        "aria-labelledby, or <title>)"
                    ),
                    impact=(
                        "Screen reader users will not know the purpose of "
                        "this SVG graphic."
                    ),
                    recommendation=(
                        "Add an aria-label, aria-labelledby, or a <title> "
                        "child element to provide a text alternative for the "
                        "SVG."
                    ),
                    severity=Severity.HIGH,
                ))

        # Check input[type=image] for alt text
        for ff in capture_data.form_fields:
            if (ff.get("type") or "").lower() != "image":
                continue
            selector = ff.get("selector", "input[type=image]")
            alt = ff.get("alt")
            aria_label = ff.get("aria_label", ff.get("aria-label", ""))
            aria_labelledby = ff.get(
                "aria_labelledby", ff.get("aria-labelledby", "")
            )
            title = ff.get("title", "")

            if not alt and not aria_label and not aria_labelledby and not title:
                findings.append(Finding(
                    id=_make_finding_id(),
                    element=selector,
                    issue=(
                        "input[type=\"image\"] has no alt attribute or "
                        "accessible name"
                    ),
                    impact=(
                        "Screen reader users will not know the purpose of "
                        "this image button."
                    ),
                    recommendation=(
                        "Add a descriptive alt attribute that conveys the "
                        "button action (e.g. alt=\"Search\" or alt=\"Submit\")."
                    ),
                    severity=Severity.HIGH,
                ))
            elif alt and _is_suspicious_alt(alt):
                findings.append(Finding(
                    id=_make_finding_id(),
                    element=selector,
                    issue=(
                        f"input[type=\"image\"] has suspicious alt text: "
                        f"\"{alt}\""
                    ),
                    impact=(
                        "Screen reader users receive non-meaningful text for "
                        "this image button."
                    ),
                    recommendation=(
                        "Replace with descriptive text that conveys the "
                        "button action."
                    ),
                    severity=Severity.MEDIUM,
                ))

        # Check <object> elements (stored in iframes) for accessible names
        for iframe in capture_data.iframes:
            tag = (iframe.get("tag") or "").lower()
            if tag != "object":
                continue
            selector = iframe.get("selector", "object")
            title = iframe.get("title", "")
            aria_label = iframe.get("ariaLabel", iframe.get("aria-label", ""))
            aria_labelledby = iframe.get(
                "ariaLabelledby", iframe.get("aria-labelledby", "")
            )
            text_content = (iframe.get("text_content") or "").strip()

            if not title and not aria_label and not aria_labelledby and not text_content:
                findings.append(Finding(
                    id=_make_finding_id(),
                    element=selector,
                    issue=(
                        "<object> element has no text alternative (no title, "
                        "aria-label, or fallback text content)"
                    ),
                    impact=(
                        "Users of assistive technologies will not know the "
                        "purpose of this embedded object."
                    ),
                    recommendation=(
                        "Add a title attribute, aria-label, or include "
                        "fallback text content inside the <object> element."
                    ),
                    severity=Severity.HIGH,
                ))

        # Check <canvas> elements for fallback content
        html = capture_data.html or ""
        if "<canvas" in html:
            # Match <canvas ...> ... </canvas> and check for fallback content
            canvas_pattern = re.compile(
                r"<canvas\b([^>]*)>(.*?)</canvas>", re.DOTALL | re.IGNORECASE
            )
            for match in canvas_pattern.finditer(html):
                attrs_str = match.group(1)
                inner = match.group(2).strip()

                # Try to extract a selector-like identifier from attributes
                id_match = re.search(r'id\s*=\s*["\']([^"\']+)["\']', attrs_str)
                canvas_id = id_match.group(1) if id_match else None
                selector = f"canvas#{canvas_id}" if canvas_id else "canvas"

                # Check for role and aria-label in attributes
                has_role = re.search(
                    r'role\s*=\s*["\']img["\']', attrs_str, re.IGNORECASE
                )
                has_aria_label = re.search(
                    r'aria-label\s*=\s*["\'][^"\']+["\']', attrs_str
                )
                has_aria_labelledby = re.search(
                    r'aria-labelledby\s*=\s*["\'][^"\']+["\']', attrs_str
                )

                if not inner and not has_aria_label and not has_aria_labelledby:
                    findings.append(Finding(
                        id=_make_finding_id(),
                        element=selector,
                        issue=(
                            "<canvas> element has no fallback content or "
                            "accessible name"
                        ),
                        impact=(
                            "Canvas content is not accessible to screen readers. "
                            "Without fallback content, users of assistive "
                            "technologies receive no information."
                        ),
                        recommendation=(
                            "Provide fallback content inside the <canvas> "
                            "element, or add role=\"img\" with an aria-label "
                            "that describes the canvas content."
                        ),
                        severity=Severity.HIGH,
                    ))

        # Determine conformance – count all non-text elements checked
        total_nontext = (
            total_images
            + sum(
                1 for img in capture_data.images
                if (img.get("tag") or "").lower() == "svg"
            )
            + sum(
                1 for ff in capture_data.form_fields
                if (ff.get("type") or "").lower() == "image"
            )
        )
        conformance = self._determine_conformance(findings, total_nontext)
        # Heuristic findings (alt="" might be wrong) lower confidence
        # below the DETERMINISTIC_CONFIDENCE_FLOOR (0.85) so the full AI
        # pipeline runs and visual AI verifies the image. Pure
        # deterministic findings (missing alt, suspicious-pattern alt)
        # stay at 0.9 so fast-path VPAT-synthesis applies.
        if has_heuristic_finding:
            confidence = 0.7
        else:
            confidence = 0.9 if total_nontext > 0 else 0.7
        return conformance, confidence, findings

    def _generate_tt_results(
        self, findings: list[Finding], capture_data: CaptureData
    ) -> list[TTSubTestResult]:
        has_missing_alt = any(
            "no alt attribute" in f.issue.lower() or "no accessible name" in f.issue.lower()
            for f in findings
            if f.severity in (Severity.HIGH, Severity.MEDIUM)
        )
        has_bad_alt = any(
            "suspicious" in f.issue.lower() or "filename" in f.issue.lower()
            for f in findings
            if f.severity in (Severity.HIGH, Severity.MEDIUM)
        )

        not_applicable = not self.is_applicable(capture_data)

        return [
            TTSubTestResult(
                tt_id="6.A",
                name="Images have accessible names",
                result=(
                    TTResult.DNA if not_applicable
                    else TTResult.FAIL if has_missing_alt
                    else TTResult.PASS
                ),
            ),
            TTSubTestResult(
                tt_id="6.B",
                name="Alt text is meaningful and equivalent",
                result=(
                    TTResult.DNA if not_applicable
                    else TTResult.FAIL if has_bad_alt
                    else TTResult.PASS
                ),
            ),
        ]


def get_checks() -> list[BaseCheck]:
    return [Check_1_1_1()]
