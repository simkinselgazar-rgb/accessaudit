"""Document-specific WCAG accessibility checks.

Supplementary checks that apply ONLY to document file types (PDF, DOCX,
XLSX, PPTX).  Per Section 508 E205 and the ICT Testing Baseline for
Electronic Documents v1.0, documents must meet WCAG 2.0 AA with four
exempted criteria (2.4.1, 2.4.5, 3.2.3, 3.2.4).
"""
from __future__ import annotations

import os
import re
from pathlib import Path

from checks.base import BaseCheck, _make_finding_id
from models import (
    CaptureData,
    ConformanceLevel,
    Finding,
    Severity,
)

# Document file types handled by these checks
_DOC_TYPES = {"pdf", "docx", "xlsx", "pptx"}

# Patterns that indicate a filename rather than a meaningful title
_FILENAME_RE = re.compile(
    r"^[\w\-. ]+\.(pdf|docx?|xlsx?|pptx?)$", re.IGNORECASE
)

# Generic / default document titles
_GENERIC_TITLES = {
    "untitled", "document1", "document 1", "sheet1", "sheet 1",
    "presentation1", "presentation 1", "book1", "book 1",
    "slide1", "slide 1", "new document", "new presentation",
}

# File-extension-style alt text pattern
_FILE_EXT_ALT_RE = re.compile(
    r"^[\w\-. ]+\.(jpe?g|png|gif|svg|bmp|webp|ico|tiff?|emf|wmf)$",
    re.IGNORECASE,
)

# Default sheet name pattern
_DEFAULT_SHEET_RE = re.compile(r"^Sheet\s*\d+$", re.IGNORECASE)


# ═════════════════════════════════════════════════════════════════════════════
#  1. DocCheck_TaggedPDF  (SC 1.3.1)
# ═════════════════════════════════════════════════════════════════════════════

class DocCheck_TaggedPDF(BaseCheck):
    """Check that a PDF is tagged with a structure tree."""

    criterion_id = "DOC-1.3.1-TAGS"
    criterion_name = "Tagged PDF"
    level = "A"
    wcag_versions = ["2.0", "2.1", "2.2"]
    guideline = "1.3 Adaptable"
    principle = "1. Perceivable"
    normative_text = (
        "Information, structure, and relationships conveyed through "
        "presentation can be programmatically determined or are available "
        "in text.  For PDF documents this requires proper tagging with a "
        "structure tree."
    )
    doc_types = ["pdf"]

    def get_extra_images(self, capture_data: CaptureData) -> list[str]:
        """Send all page renders — chunked analysis handles batching."""
        ctx = capture_data.user_context or {}
        return ctx.get("page_image_paths") or []

    def is_applicable(self, capture_data: CaptureData) -> bool:
        return capture_data.file_type == "pdf"

    async def run_programmatic(
        self, capture_data: CaptureData
    ) -> tuple[ConformanceLevel, float, list[Finding]]:
        ctx = capture_data.user_context or {}
        is_tagged = ctx.get("is_tagged_pdf", False)
        has_struct_tree = ctx.get("has_struct_tree", False)

        findings: list[Finding] = []

        if not is_tagged:
            findings.append(Finding(
                id=_make_finding_id(),
                element="<pdf>",
                issue="PDF is not tagged. Screen readers cannot determine document structure.",
                impact=(
                    "Assistive technology users cannot navigate or understand "
                    "the document structure."
                ),
                recommendation=(
                    "Re-create the PDF from source with tagging enabled, or "
                    "use a PDF remediation tool to add tags."
                ),
                severity=Severity.HIGH,
                source="programmatic",
            ))
        elif not has_struct_tree:
            findings.append(Finding(
                id=_make_finding_id(),
                element="<pdf>",
                issue="PDF is tagged but missing structure tree.",
                impact=(
                    "Document has mark info but no structure tree root, so "
                    "assistive technology may not fully resolve the structure."
                ),
                recommendation=(
                    "Ensure the PDF authoring tool generates a complete "
                    "structure tree (StructTreeRoot)."
                ),
                severity=Severity.MEDIUM,
                source="programmatic",
            ))

        conformance = self._determine_conformance(findings, total_elements=1)
        return conformance, 1.0, findings


# ═════════════════════════════════════════════════════════════════════════════
#  2. DocCheck_DocumentTitle  (SC 2.4.2)
# ═════════════════════════════════════════════════════════════════════════════

class DocCheck_DocumentTitle(BaseCheck):
    """Check that the document has a meaningful title."""

    criterion_id = "DOC-2.4.2-TITLE"
    criterion_name = "Document Title"
    level = "A"
    wcag_versions = ["2.0", "2.1", "2.2"]
    guideline = "2.4 Navigable"
    principle = "2. Operable"
    normative_text = (
        "Documents have titles that describe topic or purpose."
    )
    doc_types = ["pdf", "docx", "xlsx", "pptx"]

    def is_applicable(self, capture_data: CaptureData) -> bool:
        return capture_data.file_type in _DOC_TYPES

    async def run_programmatic(
        self, capture_data: CaptureData
    ) -> tuple[ConformanceLevel, float, list[Finding]]:
        title = (capture_data.title or "").strip()
        findings: list[Finding] = []

        if not title:
            findings.append(Finding(
                id=_make_finding_id(),
                element="<title>",
                issue="Document has no title.",
                impact="Users relying on assistive technology cannot identify the document.",
                recommendation="Set a descriptive title in the document properties.",
                severity=Severity.HIGH,
                source="programmatic",
            ))
        elif title.lower() in _GENERIC_TITLES:
            findings.append(Finding(
                id=_make_finding_id(),
                element=f"<title>{title}</title>",
                issue=f'Document title is generic: "{title}".',
                impact="A generic title does not describe the document topic or purpose.",
                recommendation="Replace the default title with a meaningful description.",
                severity=Severity.HIGH,
                source="programmatic",
            ))
        elif _FILENAME_RE.match(title):
            # Title is just the filename
            findings.append(Finding(
                id=_make_finding_id(),
                element=f"<title>{title}</title>",
                issue=f'Document title appears to be a filename: "{title}".',
                impact=(
                    "A filename is not a meaningful title and does not help "
                    "users identify the document."
                ),
                recommendation="Set a descriptive title in the document properties.",
                severity=Severity.MEDIUM,
                source="programmatic",
            ))
        else:
            # Also compare against the actual filename stem
            file_stem = Path(capture_data.file_path).stem if capture_data.file_path else ""
            if file_stem and title.lower() == file_stem.lower():
                findings.append(Finding(
                    id=_make_finding_id(),
                    element=f"<title>{title}</title>",
                    issue=f'Document title matches the filename: "{title}".',
                    impact=(
                        "The title may simply be the default from the file "
                        "system rather than a purposeful description."
                    ),
                    recommendation="Set a descriptive title in the document properties.",
                    severity=Severity.MEDIUM,
                    source="programmatic",
                ))

        conformance = self._determine_conformance(findings, total_elements=1)
        return conformance, 1.0, findings


# ═════════════════════════════════════════════════════════════════════════════
#  3. DocCheck_DocumentLanguage  (SC 3.1.1)
# ═════════════════════════════════════════════════════════════════════════════

class DocCheck_DocumentLanguage(BaseCheck):
    """Check that the document language is properly set."""

    criterion_id = "DOC-3.1.1-LANG"
    criterion_name = "Document Language"
    level = "A"
    wcag_versions = ["2.0", "2.1", "2.2"]
    guideline = "3.1 Readable"
    principle = "3. Understandable"
    normative_text = (
        "The default human language of each document can be "
        "programmatically determined."
    )
    doc_types = ["pdf", "docx", "xlsx", "pptx"]

    def is_applicable(self, capture_data: CaptureData) -> bool:
        return capture_data.file_type in _DOC_TYPES

    async def run_programmatic(
        self, capture_data: CaptureData
    ) -> tuple[ConformanceLevel, float, list[Finding]]:
        findings: list[Finding] = []
        ctx = capture_data.user_context or {}
        file_type = capture_data.file_type

        if file_type == "pdf":
            doc_lang = ctx.get("doc_language", "")
            if not doc_lang:
                findings.append(Finding(
                    id=_make_finding_id(),
                    element="<pdf /Lang>",
                    issue="PDF has no language set in the document catalog (/Lang entry missing).",
                    impact=(
                        "Screen readers cannot determine the correct pronunciation "
                        "language and will use their default, which may be wrong."
                    ),
                    recommendation=(
                        "Set the document language in PDF properties. In Adobe Acrobat: "
                        "File > Properties > Advanced > Language."
                    ),
                    severity=Severity.HIGH,
                    source="programmatic",
                ))
                return ConformanceLevel.DOES_NOT_SUPPORT, 1.0, findings
            else:
                # Language is set — passes
                return ConformanceLevel.SUPPORTS, 1.0, []

        # For Office documents, the capture pipeline now extracts the
        # language property into user_context["doc_language"].
        doc_lang = ctx.get("doc_language", "")
        if doc_lang:
            return ConformanceLevel.SUPPORTS, 1.0, []
        else:
            findings.append(Finding(
                id=_make_finding_id(),
                element=f"<{file_type} language>",
                issue=f"Document language property is not set for this {file_type.upper()} file.",
                impact=(
                    "Screen readers cannot determine the correct pronunciation "
                    "language and will use their default, which may be wrong."
                ),
                recommendation=(
                    "Set the document language property. In Word: File > Options > "
                    "Language. In PowerPoint: File > Options > Language. "
                    "In Excel: File > Options > Language."
                ),
                severity=Severity.HIGH,
                source="programmatic",
            ))
            return ConformanceLevel.DOES_NOT_SUPPORT, 1.0, findings


# ═════════════════════════════════════════════════════════════════════════════
#  4. DocCheck_HeadingStructure  (SC 1.3.1)
# ═════════════════════════════════════════════════════════════════════════════

class DocCheck_HeadingStructure(BaseCheck):
    """Check heading structure in documents."""

    criterion_id = "DOC-1.3.1-HEADINGS"
    criterion_name = "Heading Structure"
    level = "A"
    wcag_versions = ["2.0", "2.1", "2.2"]
    guideline = "1.3 Adaptable"
    principle = "1. Perceivable"
    normative_text = (
        "Information, structure, and relationships conveyed through "
        "presentation can be programmatically determined.  Headings must "
        "be properly nested and reflect the document outline."
    )
    doc_types = ["pdf", "docx", "xlsx", "pptx"]

    def is_applicable(self, capture_data: CaptureData) -> bool:
        # Not applicable to spreadsheets
        return capture_data.file_type in {"pdf", "docx", "pptx"}

    async def run_programmatic(
        self, capture_data: CaptureData
    ) -> tuple[ConformanceLevel, float, list[Finding]]:
        headings = capture_data.headings or []
        findings: list[Finding] = []

        # Estimate whether there is significant text content
        has_significant_text = False
        html = capture_data.html or ""
        # Strip HTML tags for a rough char count
        text_content = re.sub(r"<[^>]+>", "", html)
        if len(text_content.strip()) > 200:
            has_significant_text = True

        # No headings at all on a document with significant text
        if not headings and has_significant_text:
            findings.append(Finding(
                id=_make_finding_id(),
                element="<body>",
                issue="Document has significant text content but no headings.",
                impact=(
                    "Users relying on headings to navigate will not be able "
                    "to find sections within the document."
                ),
                recommendation="Add headings to organize the document structure.",
                severity=Severity.MEDIUM,
                source="programmatic",
            ))
            conformance = self._determine_conformance(findings)
            return conformance, 0.8, findings

        if not headings:
            # No headings and no significant text -- nothing to check
            return ConformanceLevel.NOT_APPLICABLE, 1.0, []

        levels = [h.get("level", 1) for h in headings]

        # First heading not H1
        if levels and levels[0] != 1:
            findings.append(Finding(
                id=_make_finding_id(),
                element=f"<{headings[0].get('tag', 'h' + str(levels[0]))}>"
                        f"{headings[0].get('text', '')}</>",
                issue=(
                    f"First heading is level {levels[0]} instead of H1."
                ),
                impact="The document outline does not start at the top level.",
                recommendation="Ensure the first heading in the document is an H1.",
                severity=Severity.LOW,
                source="programmatic",
            ))

        # Multiple H1s
        h1_count = levels.count(1)
        if h1_count > 1:
            findings.append(Finding(
                id=_make_finding_id(),
                element="<h1>",
                issue=f"Document contains {h1_count} H1 headings.",
                impact=(
                    "Multiple H1 headings can confuse assistive technology "
                    "about the primary topic."
                ),
                recommendation=(
                    "Use a single H1 for the document title and H2+ for "
                    "sub-sections."
                ),
                severity=Severity.LOW,
                source="programmatic",
            ))

        # Skipped heading levels
        for i in range(1, len(levels)):
            prev = levels[i - 1]
            curr = levels[i]
            if curr > prev + 1:
                findings.append(Finding(
                    id=_make_finding_id(),
                    element=f"<{headings[i].get('tag', 'h' + str(curr))}>"
                            f"{headings[i].get('text', '')}</>",
                    issue=(
                        f"Heading level skipped: H{prev} to H{curr} "
                        f'("{headings[i].get("text", "")}").'
                    ),
                    impact="Skipped heading levels break the logical document outline.",
                    recommendation=(
                        "Ensure heading levels increase sequentially without "
                        "skipping (e.g. H1 -> H2 -> H3)."
                    ),
                    severity=Severity.MEDIUM,
                    source="programmatic",
                ))
                break  # Report only the first skip to avoid noise

        conformance = self._determine_conformance(
            findings, total_elements=len(headings)
        )
        confidence = 0.9 if headings else 0.7
        return conformance, confidence, findings


# ═════════════════════════════════════════════════════════════════════════════
#  5. DocCheck_ImageAltText  (SC 1.1.1)
# ═════════════════════════════════════════════════════════════════════════════

class DocCheck_ImageAltText(BaseCheck):
    """Check image alt text in documents."""

    criterion_id = "DOC-1.1.1-ALT"
    criterion_name = "Image Alt Text (Documents)"
    level = "A"
    wcag_versions = ["2.0", "2.1", "2.2"]
    guideline = "1.1 Text Alternatives"
    principle = "1. Perceivable"
    normative_text = (
        "All non-text content that is presented to the user has a text "
        "alternative that serves the equivalent purpose."
    )
    doc_types = ["pdf", "docx", "xlsx", "pptx"]

    def get_extra_images(self, capture_data: CaptureData) -> list[str]:
        """Send all page renders + all extracted images. Chunked analysis handles batching."""
        paths: list[str] = []
        ctx = capture_data.user_context or {}
        for p in ctx.get("page_image_paths") or []:
            paths.append(p)
        for img in capture_data.images or []:
            p = img.get("path", "") or img.get("screenshot_path", "")
            if p:
                paths.append(p)
        return paths

    def is_applicable(self, capture_data: CaptureData) -> bool:
        return (
            capture_data.file_type in _DOC_TYPES
            and bool(capture_data.images)
        )

    async def run_programmatic(
        self, capture_data: CaptureData
    ) -> tuple[ConformanceLevel, float, list[Finding]]:
        images = capture_data.images or []
        findings: list[Finding] = []
        is_pdf = capture_data.file_type == "pdf"

        for img in images:
            alt = img.get("alt")
            src = img.get("path", "")
            img_label = os.path.basename(src) if src else "<img>"

            if alt is None or alt == "":
                note = ""
                if is_pdf:
                    note = (
                        " Note: PyMuPDF cannot extract alt text from "
                        "untagged PDF images, so this may be a capture "
                        "limitation rather than a true missing alt."
                    )
                findings.append(Finding(
                    id=_make_finding_id(),
                    element=f'<img src=".../{img_label}" alt="">',
                    issue=f"Image has no alt text.{note}",
                    impact=(
                        "Screen reader users will not know the purpose or "
                        "content of this image."
                    ),
                    recommendation=(
                        "Add descriptive alt text, or mark the image as "
                        "decorative if it conveys no information."
                    ),
                    severity=Severity.HIGH,
                    source="programmatic",
                ))
            elif _FILE_EXT_ALT_RE.match(alt.strip()):
                findings.append(Finding(
                    id=_make_finding_id(),
                    element=f'<img src=".../{img_label}" alt="{alt}">',
                    issue=f'Image alt text appears to be a filename: "{alt}".',
                    impact=(
                        "A filename does not describe the image content and "
                        "is not useful to assistive technology users."
                    ),
                    recommendation=(
                        "Replace the filename with a meaningful description "
                        "of the image content."
                    ),
                    severity=Severity.MEDIUM,
                    source="programmatic",
                ))
            elif 0 < len(alt.strip()) < 5:
                findings.append(Finding(
                    id=_make_finding_id(),
                    element=f'<img src=".../{img_label}" alt="{alt}">',
                    issue=f'Image alt text is very short ({len(alt.strip())} chars): "{alt}".',
                    impact=(
                        "Very short alt text may not adequately describe the "
                        "image content."
                    ),
                    recommendation=(
                        "Review and expand the alt text to be more descriptive, "
                        "or confirm that the brief text is sufficient."
                    ),
                    severity=Severity.LOW,
                    source="programmatic",
                ))

        conformance = self._determine_conformance(
            findings, total_elements=len(images)
        )
        return conformance, 0.9, findings


# ═════════════════════════════════════════════════════════════════════════════
#  6. DocCheck_TableHeaders  (SC 1.3.1)
# ═════════════════════════════════════════════════════════════════════════════

class DocCheck_TableHeaders(BaseCheck):
    """Check that document tables have proper header cells."""

    criterion_id = "DOC-1.3.1-TABLES"
    criterion_name = "Table Headers (Documents)"
    level = "A"
    wcag_versions = ["2.0", "2.1", "2.2"]
    guideline = "1.3 Adaptable"
    principle = "1. Perceivable"
    normative_text = (
        "Information, structure, and relationships conveyed through "
        "presentation can be programmatically determined.  Data tables "
        "must identify header cells."
    )
    doc_types = ["pdf", "docx", "xlsx", "pptx"]

    def is_applicable(self, capture_data: CaptureData) -> bool:
        return (
            capture_data.file_type in _DOC_TYPES
            and bool(capture_data.tables)
        )

    async def run_programmatic(
        self, capture_data: CaptureData
    ) -> tuple[ConformanceLevel, float, list[Finding]]:
        tables = capture_data.tables or []
        findings: list[Finding] = []

        for tbl in tables:
            tbl_index = tbl.get("index", 0)
            rows = tbl.get("rows", [])
            row_count = tbl.get("rowCount", len(rows))
            caption = tbl.get("caption", "")
            tbl_label = f"Table {tbl_index + 1}"
            if caption:
                tbl_label += f' ("{caption}")'

            # Determine if the table has header cells.
            # In the pseudo-HTML the first row is rendered with <th> by the
            # capture modules (DOCX, XLSX, PPTX all do r_idx==0 -> <th>).
            # However, we check the actual HTML for <th> presence as well
            # because the capture might not always generate them.
            html = capture_data.html or ""
            has_th = "<th>" in html or "<th " in html

            # Check if there are scope attributes on headers
            has_scope = 'scope="' in html

            if not has_th:
                severity = Severity.HIGH if row_count > 10 else Severity.MEDIUM
                findings.append(Finding(
                    id=_make_finding_id(),
                    element=f"<table> ({tbl_label})",
                    issue=f"{tbl_label} has no header cells.",
                    impact=(
                        "Screen reader users cannot determine what each "
                        "column or row represents."
                    ),
                    recommendation=(
                        "Designate the first row (or column) as header cells "
                        "using proper table header markup."
                    ),
                    severity=severity,
                    source="programmatic",
                ))
            elif not has_scope:
                findings.append(Finding(
                    id=_make_finding_id(),
                    element=f"<table> ({tbl_label})",
                    issue=f"{tbl_label} has header cells but no scope attributes.",
                    impact=(
                        "Without scope attributes, the association between "
                        "headers and data cells may be ambiguous for complex "
                        "tables."
                    ),
                    recommendation=(
                        'Add scope="col" or scope="row" to header cells to '
                        "clarify their relationship to data cells."
                    ),
                    severity=Severity.LOW,
                    source="programmatic",
                ))

        conformance = self._determine_conformance(
            findings, total_elements=len(tables)
        )
        return conformance, 0.9, findings


# ═════════════════════════════════════════════════════════════════════════════
#  7. DocCheck_ScannedPDF  (SC 1.4.5)
# ═════════════════════════════════════════════════════════════════════════════

class DocCheck_ScannedPDF(BaseCheck):
    """Detect scanned / image-only PDFs without OCR text."""

    criterion_id = "DOC-1.4.5-SCAN"
    criterion_name = "Scanned PDF Detection"
    level = "AA"
    wcag_versions = ["2.0", "2.1", "2.2"]
    guideline = "1.4 Distinguishable"
    principle = "1. Perceivable"
    normative_text = (
        "If the technologies being used can achieve the visual "
        "presentation, text is used to convey information rather than "
        "images of text.  Scanned PDFs without OCR consist entirely of "
        "images of text and are inaccessible."
    )
    doc_types = ["pdf"]

    def is_applicable(self, capture_data: CaptureData) -> bool:
        return capture_data.file_type == "pdf"

    def get_extra_images(self, capture_data: CaptureData) -> list[str]:
        """Send all page renders — AI checks every page for scanned content."""
        ctx = capture_data.user_context or {}
        return ctx.get("page_image_paths") or []

    async def run_programmatic(
        self, capture_data: CaptureData
    ) -> tuple[ConformanceLevel, float, list[Finding]]:
        ctx = capture_data.user_context or {}
        page_texts = ctx.get("page_texts", [])
        images = capture_data.images or []
        page_image_paths = ctx.get("page_image_paths") or []
        findings: list[Finding] = []

        if not page_texts:
            # No page text data available -- cannot evaluate
            return ConformanceLevel.NOT_EVALUATED, 0.3, []

        # Check if there are images but very little extractable text
        has_images = bool(images)
        total_text_len = 0
        page_count = len(page_texts)

        for pt in page_texts:
            text = pt.get("text", "")
            total_text_len += len(text.strip())

        if page_count == 0:
            return ConformanceLevel.NOT_EVALUATED, 0.3, []

        avg_text_per_page = total_text_len / page_count

        if has_images and avg_text_per_page < 50:
            # Confirmed scanned-image PDF. Run VLM OCR on each page
            # render so downstream checks have actual text to evaluate
            # (and so the auditor sees what the PDF was supposed to say).
            ocr_pages = await _ocr_scanned_pdf_pages(page_image_paths)
            ocr_chars = sum(len(t) for t in ocr_pages)
            evidence = ""
            if ocr_chars:
                ctx["ocr_page_texts"] = ocr_pages
                capture_data.user_context = ctx
                first_page = ocr_pages[0] if ocr_pages else ""
                evidence = (
                    f"VLM OCR recovered {ocr_chars} chars across "
                    f"{len(ocr_pages)} page(s). First page: "
                    f"{first_page!r}"
                )
            findings.append(Finding(
                id=_make_finding_id(),
                element="<pdf>",
                issue=(
                    "PDF appears to be a scanned image without embedded "
                    "text. Content is inaccessible to screen readers even "
                    "though the words are visually present."
                ),
                impact=(
                    "All document content is locked inside images. Screen "
                    "reader users cannot read, search, or navigate the "
                    "content. Users who rely on text selection, translation, "
                    "or clipboard operations are also blocked."
                ),
                recommendation=(
                    "Re-create the PDF from its text-based source, or run a "
                    "PDF OCR pass (Adobe Acrobat -> Scan & OCR, or a server-"
                    "side tool) so the text becomes programmatically available. "
                    "The underlying text was successfully OCR'd by the "
                    "automated tool, confirming the visual content is "
                    "readable -- it just isn't exposed to assistive technology."
                ),
                severity=Severity.HIGH,
                source="programmatic",
                evidence=evidence,
            ))
        elif not has_images and avg_text_per_page < 10:
            # Essentially empty document
            return ConformanceLevel.NOT_APPLICABLE, 1.0, []

        conformance = self._determine_conformance(findings, total_elements=1)
        return conformance, 1.0, findings


async def _ocr_scanned_pdf_pages(page_image_paths: list[str]) -> list[str]:
    """Run the local VLM OCR over each page-render image.

    Skips gracefully when no page images are available (e.g. a pure-text
    PDF that didn't need per-page renders) or when the VLM call fails
    for any page -- the caller treats an empty list as "OCR not
    available" rather than dropping evidence.
    """
    if not page_image_paths:
        return []
    try:
        from functions.image_analysis import extract_text_from_image
    except Exception:
        return []
    out: list[str] = []
    for path in page_image_paths:
        try:
            text = await extract_text_from_image(path)
        except Exception:
            text = ""
        out.append(text)
    return out


# ═════════════════════════════════════════════════════════════════════════════
#  8. DocCheck_ReadingOrder  (SC 1.3.2)
# ═════════════════════════════════════════════════════════════════════════════

class DocCheck_ReadingOrder(BaseCheck):
    """Flag reading order concerns for PPTX presentations."""

    criterion_id = "DOC-1.3.2-ORDER"
    criterion_name = "Reading Order"
    level = "A"
    wcag_versions = ["2.0", "2.1", "2.2"]
    guideline = "1.3 Adaptable"
    principle = "1. Perceivable"
    normative_text = (
        "When the sequence in which content is presented affects its "
        "meaning, a correct reading sequence can be programmatically "
        "determined."
    )
    doc_types = ["pdf", "docx", "xlsx", "pptx"]

    def is_applicable(self, capture_data: CaptureData) -> bool:
        return capture_data.file_type == "pptx"

    def get_extra_images(self, capture_data: CaptureData) -> list[str]:
        """Provide full-page screenshot for AI vision analysis of reading order."""
        paths = []
        if capture_data.full_page_path:
            paths.append(capture_data.full_page_path)
        return paths

    async def run_programmatic(
        self, capture_data: CaptureData
    ) -> tuple[ConformanceLevel, float, list[Finding]]:
        findings: list[Finding] = []
        findings.append(Finding(
            id=_make_finding_id(),
            element="<pptx slides>",
            issue=(
                "Reading order in PowerPoint presentations is determined by "
                "the order of shapes in the slide XML, which may not match "
                "the visual layout.  This cannot be fully verified "
                "programmatically."
            ),
            impact=(
                "Screen reader users may encounter slide content in an "
                "illogical order if the reading order has not been manually "
                "arranged."
            ),
            recommendation=(
                "In PowerPoint, use the Selection Pane (Home > Arrange > "
                "Selection Pane) to review and adjust the reading order for "
                "each slide.  Items are read bottom-to-top in the pane."
            ),
            severity=Severity.INFO,
            source="programmatic",
        ))

        return ConformanceLevel.NOT_EVALUATED, 0.3, findings


# ═════════════════════════════════════════════════════════════════════════════
#  9. DocCheck_SlideTitle  (SC 2.4.2)
# ═════════════════════════════════════════════════════════════════════════════

class DocCheck_SlideTitle(BaseCheck):
    """Check that each slide in a PPTX has a title."""

    criterion_id = "DOC-2.4.2-SLIDES"
    criterion_name = "Slide Titles"
    level = "A"
    wcag_versions = ["2.0", "2.1", "2.2"]
    guideline = "2.4 Navigable"
    principle = "2. Operable"
    normative_text = (
        "Documents have titles that describe topic or purpose.  Each "
        "slide in a presentation should have a unique, descriptive title."
    )
    doc_types = ["pptx"]

    def is_applicable(self, capture_data: CaptureData) -> bool:
        return capture_data.file_type == "pptx"

    async def run_programmatic(
        self, capture_data: CaptureData
    ) -> tuple[ConformanceLevel, float, list[Finding]]:
        headings = capture_data.headings or []
        findings: list[Finding] = []

        # Count total slides from the pseudo-HTML sections
        html = capture_data.html or ""
        slide_sections = re.findall(r'aria-label="Slide (\d+)"', html)
        total_slides = len(slide_sections)

        if total_slides == 0:
            # Cannot determine slide count
            return ConformanceLevel.NOT_EVALUATED, 0.3, []

        # Headings from PPTX capture have a "slide" key
        titled_slides = set()
        slide_titles: list[str] = []
        for h in headings:
            slide_num = h.get("slide")
            if slide_num is not None:
                titled_slides.add(slide_num)
                slide_titles.append(h.get("text", "").strip())

        untitled_count = total_slides - len(titled_slides)

        if untitled_count > 0:
            findings.append(Finding(
                id=_make_finding_id(),
                element="<pptx slides>",
                issue=(
                    f"{untitled_count} of {total_slides} slide(s) have no title."
                ),
                impact=(
                    "Untitled slides cannot be identified by screen reader "
                    "users navigating by heading."
                ),
                recommendation=(
                    "Add a title placeholder to every slide.  If a visual "
                    "title is not desired, use a hidden title."
                ),
                severity=Severity.MEDIUM,
                source="programmatic",
            ))

        # Duplicate slide titles
        seen: dict[str, int] = {}
        for t in slide_titles:
            lower = t.lower()
            if lower:
                seen[lower] = seen.get(lower, 0) + 1
        duplicates = {t: c for t, c in seen.items() if c > 1}
        if duplicates:
            dup_list = ", ".join(
                f'"{t}" (x{c})' for t, c in duplicates.items()
            )
            findings.append(Finding(
                id=_make_finding_id(),
                element="<pptx slides>",
                issue=f"Duplicate slide titles found: {dup_list}.",
                impact=(
                    "Duplicate titles make it difficult for users to "
                    "distinguish between slides when navigating."
                ),
                recommendation="Give each slide a unique, descriptive title.",
                severity=Severity.LOW,
                source="programmatic",
            ))

        conformance = self._determine_conformance(
            findings, total_elements=total_slides
        )
        return conformance, 0.8, findings


# ═════════════════════════════════════════════════════════════════════════════
#  10. DocCheck_SpreadsheetStructure  (SC 1.3.1)
# ═════════════════════════════════════════════════════════════════════════════

class DocCheck_SpreadsheetStructure(BaseCheck):
    """Check spreadsheet structure and naming conventions."""

    criterion_id = "DOC-1.3.1-SHEETS"
    criterion_name = "Spreadsheet Structure"
    level = "A"
    wcag_versions = ["2.0", "2.1", "2.2"]
    guideline = "1.3 Adaptable"
    principle = "1. Perceivable"
    normative_text = (
        "Information, structure, and relationships conveyed through "
        "presentation can be programmatically determined.  Spreadsheets "
        "must use meaningful sheet names and defined table structures."
    )
    doc_types = ["xlsx"]

    def is_applicable(self, capture_data: CaptureData) -> bool:
        return capture_data.file_type == "xlsx"

    async def run_programmatic(
        self, capture_data: CaptureData
    ) -> tuple[ConformanceLevel, float, list[Finding]]:
        tables = capture_data.tables or []
        findings: list[Finding] = []

        if not tables:
            return ConformanceLevel.NOT_APPLICABLE, 1.0, []

        for tbl in tables:
            caption = tbl.get("caption", "")
            rows = tbl.get("rows", [])
            row_count = tbl.get("rowCount", len(rows))
            tbl_index = tbl.get("index", 0)
            sheet_label = f"Sheet {tbl_index + 1}"
            if caption:
                sheet_label = f'"{caption}"'

            # Default sheet name
            if caption and _DEFAULT_SHEET_RE.match(caption.strip()):
                findings.append(Finding(
                    id=_make_finding_id(),
                    element=f"<worksheet> ({sheet_label})",
                    issue=f'Sheet name is a default value: "{caption}".',
                    impact=(
                        "Default sheet names do not describe the content, "
                        "making navigation difficult for screen reader users."
                    ),
                    recommendation=(
                        "Rename the sheet tab to describe its content "
                        '(e.g. "Q4 Revenue" instead of "Sheet1").'
                    ),
                    severity=Severity.MEDIUM,
                    source="programmatic",
                ))

            # Check for merged cells from the capture pipeline data
            merged_cells = tbl.get("merged_cells", [])
            if merged_cells:
                range_list = ", ".join(str(r) for r in merged_cells)
                suffix = ""
                findings.append(Finding(
                    id=_make_finding_id(),
                    element=f"<worksheet> ({sheet_label})",
                    issue=(
                        f"{sheet_label} has {len(merged_cells)} merged cell "
                        f"range(s): {range_list}{suffix}. Merged cells break "
                        f"screen reader navigation."
                    ),
                    impact=(
                        "Merged cells can cause screen readers to misalign "
                        "header-to-data cell relationships and skip or "
                        "repeat content."
                    ),
                    recommendation=(
                        "Unmerge cells and use proper header rows/columns "
                        "instead of relying on merged cell layouts."
                    ),
                    severity=Severity.MEDIUM,
                    source="programmatic",
                ))
            else:
                # Fall back to HTML check for colspan/rowspan
                html = capture_data.html or ""
                if "colspan" in html or "rowspan" in html:
                    findings.append(Finding(
                        id=_make_finding_id(),
                        element=f"<worksheet> ({sheet_label})",
                        issue=f"Merged cells detected in {sheet_label} (from HTML structure).",
                        impact=(
                            "Merged cells can cause screen readers to misalign "
                            "header-to-data cell relationships."
                        ),
                        recommendation=(
                            "Unmerge cells and use proper header rows/columns "
                            "instead of relying on merged cell layouts."
                        ),
                        severity=Severity.MEDIUM,
                        source="programmatic",
                    ))

            # Large data range without explicit headers
            # The XLSX capture marks row 0 as <th> by default, but that is
            # a heuristic.  If there are many rows, flag for review.
            if row_count > 10:
                # Check if first row looks like a header (non-numeric values)
                has_likely_header = False
                if rows:
                    first_row = rows[0]
                    non_numeric = sum(
                        1 for cell in first_row
                        if cell and not cell.replace(".", "", 1).replace("-", "", 1).isdigit()
                    )
                    has_likely_header = non_numeric > len(first_row) * 0.5

                if not has_likely_header:
                    findings.append(Finding(
                        id=_make_finding_id(),
                        element=f"<worksheet> ({sheet_label})",
                        issue=(
                            f"{sheet_label} has {row_count} rows but the "
                            f"first row does not appear to be a header row."
                        ),
                        impact=(
                            "Without a clear header row, screen reader users "
                            "cannot determine what each column represents."
                        ),
                        recommendation=(
                            "Ensure the first row contains descriptive column "
                            "headers and format the data as a named Table "
                            "(Insert > Table) in Excel."
                        ),
                        severity=Severity.MEDIUM,
                        source="programmatic",
                    ))

            # No formal Excel Table structure (always flag as info since
            # the capture pipeline cannot detect named Tables)
            findings.append(Finding(
                id=_make_finding_id(),
                element=f"<worksheet> ({sheet_label})",
                issue=(
                    f"Cannot verify whether {sheet_label} uses a formal "
                    f"Excel Table structure (ListObject).  The capture "
                    f"pipeline reads raw cell data only."
                ),
                impact=(
                    "Named Tables in Excel provide programmatic header "
                    "associations that raw cell ranges do not."
                ),
                recommendation=(
                    "Format data ranges as named Tables in Excel "
                    "(Insert > Table) to ensure proper header associations."
                ),
                severity=Severity.LOW,
                source="programmatic",
            ))

        conformance = self._determine_conformance(
            findings, total_elements=len(tables)
        )
        return conformance, 0.7, findings


# ═════════════════════════════════════════════════════════════════════════════
#  11. DocCheck_PDFBookmarks  (SC 2.4.5 - best practice for documents)
# ═════════════════════════════════════════════════════════════════════════════

class DocCheck_PDFBookmarks(BaseCheck):
    """Check that long PDFs have bookmarks for navigation."""

    criterion_id = "DOC-2.4.5-BOOKMARKS"
    criterion_name = "PDF Bookmarks"
    level = "AA"
    wcag_versions = ["2.0", "2.1", "2.2"]
    guideline = "2.4 Navigable"
    principle = "2. Operable"
    normative_text = (
        "More than one way is available to locate a document within a "
        "set of documents.  For PDFs over ~20 pages, bookmarks provide "
        "an essential navigation mechanism for assistive technology users."
    )
    doc_types = ["pdf"]

    def is_applicable(self, capture_data: CaptureData) -> bool:
        if capture_data.file_type != "pdf":
            return False
        ctx = capture_data.user_context or {}
        page_count = ctx.get("page_count", 0)
        return page_count > 5  # Only check documents over 5 pages

    async def run_programmatic(
        self, capture_data: CaptureData
    ) -> tuple[ConformanceLevel, float, list[Finding]]:
        ctx = capture_data.user_context or {}
        has_bookmarks = ctx.get("has_bookmarks", False)
        page_count = ctx.get("page_count", 0)
        findings: list[Finding] = []

        if not has_bookmarks:
            severity = Severity.HIGH if page_count > 20 else Severity.MEDIUM
            findings.append(Finding(
                id=_make_finding_id(),
                element="<pdf /Outlines>",
                issue=(
                    f"PDF has {page_count} pages but no bookmarks (outlines). "
                    f"Long documents require bookmarks for navigation."
                ),
                impact=(
                    "Screen reader users cannot jump between sections and must "
                    "read the entire document sequentially."
                ),
                recommendation=(
                    "Add bookmarks that mirror the heading structure. In Adobe "
                    "Acrobat: use the Bookmarks panel, or auto-generate from "
                    "the tag structure."
                ),
                severity=severity,
                source="programmatic",
            ))

        conformance = self._determine_conformance(findings, total_elements=1)
        return conformance, 1.0, findings


# ═════════════════════════════════════════════════════════════════════════════
#  12. DocCheck_PDFAccessibilityPermission
# ═════════════════════════════════════════════════════════════════════════════

class DocCheck_PDFAccessibilityPermission(BaseCheck):
    """Check that PDF security settings don't block assistive technology."""

    criterion_id = "DOC-4.1.2-PERM"
    criterion_name = "PDF Accessibility Permission"
    level = "A"
    wcag_versions = ["2.0", "2.1", "2.2"]
    guideline = "4.1 Compatible"
    principle = "4. Robust"
    normative_text = (
        "PDF security settings must not block assistive technology access. "
        "Per PDF/UA, the accessibility flag in the document permissions must "
        "allow text extraction for screen readers."
    )
    doc_types = ["pdf"]

    def is_applicable(self, capture_data: CaptureData) -> bool:
        return capture_data.file_type == "pdf"

    async def run_programmatic(
        self, capture_data: CaptureData
    ) -> tuple[ConformanceLevel, float, list[Finding]]:
        ctx = capture_data.user_context or {}
        has_permission = ctx.get("has_a11y_permission", True)
        findings: list[Finding] = []

        if not has_permission:
            findings.append(Finding(
                id=_make_finding_id(),
                element="<pdf /Encrypt>",
                issue=(
                    "PDF security settings block assistive technology access. "
                    "The accessibility permission flag is not set."
                ),
                impact=(
                    "Screen readers may not be able to extract text from this "
                    "PDF, making it completely inaccessible."
                ),
                recommendation=(
                    "In the PDF security settings, enable 'Enable text access "
                    "for screen reader devices for the visually impaired'. "
                    "In Adobe Acrobat: File > Properties > Security."
                ),
                severity=Severity.HIGH,
                source="programmatic",
            ))

        conformance = self._determine_conformance(findings, total_elements=1)
        return conformance, 1.0, findings


# ═════════════════════════════════════════════════════════════════════════════
#  13. DocCheck_PDFTabOrder  (SC 2.4.3)
# ═════════════════════════════════════════════════════════════════════════════

class DocCheck_PDFTabOrder(BaseCheck):
    """Check that PDF tab order follows document structure."""

    criterion_id = "DOC-2.4.3-TAB"
    criterion_name = "PDF Tab Order"
    level = "A"
    wcag_versions = ["2.0", "2.1", "2.2"]
    guideline = "2.4 Navigable"
    principle = "2. Operable"
    normative_text = (
        "If a document can be navigated sequentially and the navigation "
        "sequences affect meaning or operation, focusable components "
        "receive focus in an order that preserves meaning and operability. "
        "For PDFs, the /Tabs /S setting ensures tab order follows the "
        "structure tree."
    )
    doc_types = ["pdf"]

    def is_applicable(self, capture_data: CaptureData) -> bool:
        return capture_data.file_type == "pdf"

    async def run_programmatic(
        self, capture_data: CaptureData
    ) -> tuple[ConformanceLevel, float, list[Finding]]:
        ctx = capture_data.user_context or {}
        tab_order_set = ctx.get("tab_order_set", False)
        has_links = bool(capture_data.links)
        findings: list[Finding] = []

        # Only relevant if the PDF has interactive elements (links, forms)
        if not has_links:
            return ConformanceLevel.NOT_APPLICABLE, 1.0, []

        if not tab_order_set:
            findings.append(Finding(
                id=_make_finding_id(),
                element="<pdf /Tabs>",
                issue=(
                    "PDF page tab order is not set to follow document "
                    "structure (/Tabs /S not found on page dictionaries)."
                ),
                impact=(
                    "Keyboard users tabbing through links and form fields "
                    "may encounter them in a different order than the visual "
                    "or logical reading order."
                ),
                recommendation=(
                    "Set the tab order to 'Use Document Structure' in "
                    "Adobe Acrobat: Pages panel > right-click > Page Properties "
                    "> Tab Order > Use Document Structure."
                ),
                severity=Severity.MEDIUM,
                source="programmatic",
            ))

        conformance = self._determine_conformance(findings, total_elements=1)
        return conformance, 1.0, findings


# ═════════════════════════════════════════════════════════════════════════════
#  14. DocCheck_MeaningfulContent  (Document Baseline 18.A)
# ═════════════════════════════════════════════════════════════════════════════

class DocCheck_MeaningfulContent(BaseCheck):
    """Check that meaningful content in headers/footers/watermarks is
    also present in the document body (Document Baseline 18.A)."""

    criterion_id = "DOC-1.3.1-BODY"
    criterion_name = "Meaningful Content in Body"
    level = "A"
    wcag_versions = ["2.0", "2.1", "2.2"]
    guideline = "1.3 Adaptable"
    principle = "1. Perceivable"
    normative_text = (
        "All meaningful content exists in the document body or is "
        "programmatically identified.  Headers, footers, and watermarks "
        "that convey meaningful information must also have that information "
        "available in the main content (Document Baseline 18.A)."
    )
    doc_types = ["pdf", "docx", "xlsx", "pptx"]

    def is_applicable(self, capture_data: CaptureData) -> bool:
        return capture_data.file_type in _DOC_TYPES

    async def run_programmatic(
        self, capture_data: CaptureData
    ) -> tuple[ConformanceLevel, float, list[Finding]]:
        findings: list[Finding] = []

        # This is primarily a manual review item, but we can flag the need
        # for verification.  For PDFs, headers/footers should be artifacts;
        # for Office docs, they should be in the built-in header/footer area.
        if capture_data.file_type == "pdf":
            ctx = capture_data.user_context or {}
            is_tagged = ctx.get("is_tagged_pdf", False)
            if is_tagged:
                findings.append(Finding(
                    id=_make_finding_id(),
                    element="<pdf headers/footers>",
                    issue=(
                        "Tagged PDF: verify that page headers, footers, and "
                        "watermarks are marked as Artifacts (not tagged as "
                        "real content) unless they convey meaningful information "
                        "that is also present in the document body."
                    ),
                    impact=(
                        "If headers/footers are tagged as real content, screen "
                        "readers will read them on every page, disrupting the "
                        "reading flow."
                    ),
                    recommendation=(
                        "Mark repeating headers, footers, page numbers, and "
                        "decorative watermarks as Artifacts.  If a watermark "
                        "conveys meaningful information (e.g., 'DRAFT'), ensure "
                        "that information also appears in the document body text."
                    ),
                    severity=Severity.INFO,
                    source="programmatic",
                ))
        else:
            findings.append(Finding(
                id=_make_finding_id(),
                element=f"<{capture_data.file_type} headers/footers>",
                issue=(
                    "Verify that any meaningful content in document headers, "
                    "footers, or watermarks is also present in the main "
                    "document body."
                ),
                impact=(
                    "Content only in headers/footers may not be accessible "
                    "in all reading contexts."
                ),
                recommendation=(
                    "Use built-in header/footer tools. If a watermark conveys "
                    "important information (e.g., 'CONFIDENTIAL', 'DRAFT'), "
                    "include that status in the body text as well."
                ),
                severity=Severity.INFO,
                source="programmatic",
            ))

        return ConformanceLevel.NOT_EVALUATED, 0.3, findings


# ═════════════════════════════════════════════════════════════════════════════
#  15. DocCheck_ListStructure  (Baseline 13.D)
# ═════════════════════════════════════════════════════════════════════════════

class DocCheck_ListStructure(BaseCheck):
    """Check that document lists use proper list formatting."""

    criterion_id = "DOC-1.3.1-LISTS"
    criterion_name = "List Structure (Documents)"
    level = "A"
    wcag_versions = ["2.0", "2.1", "2.2"]
    guideline = "1.3 Adaptable"
    principle = "1. Perceivable"
    normative_text = (
        "Content that is visually formatted as a list must use the "
        "appropriate programmatic list structure (bulleted, numbered, "
        "or description list)."
    )
    doc_types = ["pdf", "docx", "xlsx", "pptx"]

    def is_applicable(self, capture_data: CaptureData) -> bool:
        return capture_data.file_type in {"docx", "pptx"}

    async def run_programmatic(
        self, capture_data: CaptureData
    ) -> tuple[ConformanceLevel, float, list[Finding]]:
        findings: list[Finding] = []
        html = capture_data.html or ""

        # Detect visual list patterns in body text that aren't in
        # <ul>/<ol>/<dl> markup.  Look for lines starting with bullets,
        # dashes, asterisks, or sequential numbers outside of list elements.
        # This is a heuristic — the AI analysis provides deeper review.
        import re

        # Strip content inside proper list tags to avoid false positives
        html_no_lists = re.sub(
            r"<(?:ul|ol|dl)[\s>].*?</(?:ul|ol|dl)>",
            "",
            html,
            flags=re.DOTALL | re.IGNORECASE,
        )

        # Look for paragraphs that start with list-like markers
        list_patterns = re.findall(
            r"<p>\s*(?:[-•·●○■□▪▫–—]\s+|(?:\d{1,3}[.)]\s+)|(?:[a-zA-Z][.)]\s+))",
            html_no_lists,
        )

        if len(list_patterns) >= 3:
            findings.append(Finding(
                id=_make_finding_id(),
                element="<p> (visual list items)",
                issue=(
                    f"Detected {len(list_patterns)} paragraph(s) with "
                    f"list-like markers (bullets, numbers, dashes) that are "
                    f"not in proper list markup (<ul>, <ol>, <dl>)."
                ),
                impact=(
                    "Screen readers will not announce these as list items, "
                    "losing structural context for the user."
                ),
                recommendation=(
                    "Use built-in list styles (bulleted or numbered list) "
                    "instead of manually typing bullet characters or numbers."
                ),
                severity=Severity.MEDIUM,
                source="programmatic",
            ))

        conformance = self._determine_conformance(findings)
        return conformance, 0.6, findings


# ═════════════════════════════════════════════════════════════════════════════
#  16. DocCheck_AutoAdvancingSlides  (SC 2.2.1)
# ═════════════════════════════════════════════════════════════════════════════

class DocCheck_AutoAdvancingSlides(BaseCheck):
    """Check for auto-advancing slides in presentations."""

    criterion_id = "DOC-2.2.1-SLIDES"
    criterion_name = "Auto-Advancing Slides"
    level = "A"
    wcag_versions = ["2.0", "2.1", "2.2"]
    guideline = "2.2 Enough Time"
    principle = "2. Operable"
    normative_text = (
        "For each time limit that is set by the content, the user can "
        "turn off, adjust, or extend the time limit.  Auto-advancing "
        "slides must provide user controls."
    )
    doc_types = ["pptx"]

    def is_applicable(self, capture_data: CaptureData) -> bool:
        return capture_data.file_type == "pptx"

    async def run_programmatic(
        self, capture_data: CaptureData
    ) -> tuple[ConformanceLevel, float, list[Finding]]:
        ctx = capture_data.user_context or {}
        transitions = ctx.get("slide_transitions", [])
        findings: list[Finding] = []

        if not transitions:
            # No transition data available from capture pipeline
            findings.append(Finding(
                id=_make_finding_id(),
                element="<pptx transitions>",
                issue="Slide transition data was not available from the capture pipeline.",
                impact="Cannot determine if slides auto-advance.",
                recommendation="Verify manually that no slides auto-advance without user control.",
                severity=Severity.INFO,
                source="programmatic",
            ))
            return ConformanceLevel.NOT_EVALUATED, 0.3, findings

        has_uncontrolled_auto = False
        for tr in transitions:
            auto_ms = tr.get("auto_advance_ms", 0)
            advance_on_click = tr.get("advance_on_click", True)
            slide_num = tr.get("slide_number", tr.get("slide", "?"))

            if auto_ms and auto_ms > 0:
                seconds = round(auto_ms / 1000, 1)
                if not advance_on_click:
                    has_uncontrolled_auto = True
                    findings.append(Finding(
                        id=_make_finding_id(),
                        element=f"<slide {slide_num} transition>",
                        issue=(
                            f"Slide {slide_num} auto-advances after {seconds} seconds "
                            f"with no user control (advance_on_click disabled)."
                        ),
                        impact=(
                            "Users who need more time to read slide content "
                            "cannot prevent the slide from advancing."
                        ),
                        recommendation=(
                            "Remove auto-advance timing or enable advance on "
                            "click so users can control the pace."
                        ),
                        severity=Severity.HIGH,
                        source="programmatic",
                    ))
                else:
                    findings.append(Finding(
                        id=_make_finding_id(),
                        element=f"<slide {slide_num} transition>",
                        issue=(
                            f"Slide {slide_num} has auto-advance after {seconds} "
                            f"seconds but user can also click to advance."
                        ),
                        impact=(
                            "The slide will advance automatically, but users "
                            "retain the ability to click to advance manually."
                        ),
                        recommendation=(
                            "Consider removing auto-advance timing to give "
                            "users full control over pacing."
                        ),
                        severity=Severity.LOW,
                        source="programmatic",
                    ))

        if has_uncontrolled_auto:
            return ConformanceLevel.DOES_NOT_SUPPORT, 1.0, findings
        elif findings:
            # Only LOW findings (auto-advance with click enabled)
            return ConformanceLevel.PARTIALLY_SUPPORTS, 1.0, findings
        else:
            return ConformanceLevel.SUPPORTS, 1.0, findings


# ═════════════════════════════════════════════════════════════════════════════
#  17. DocCheck_DescriptiveFilename  (SC 2.4.2 / AED-COP requirement)
# ═════════════════════════════════════════════════════════════════════════════

# Generic filenames that indicate no thought was given to naming
_GENERIC_FILENAMES = {
    "document", "document1", "doc1", "file", "new document",
    "spreadsheet", "spreadsheet1", "book1", "workbook1",
    "presentation", "presentation1", "slide1", "slides",
    "untitled", "test", "temp", "copy", "draft",
}


class DocCheck_DescriptiveFilename(BaseCheck):
    """Check that the document has a descriptive filename."""

    criterion_id = "DOC-2.4.2-FILE"
    criterion_name = "Descriptive Filename"
    level = "A"
    wcag_versions = ["2.0", "2.1", "2.2"]
    guideline = "2.4 Navigable"
    principle = "2. Operable"
    normative_text = (
        "A descriptive filename that identifies the document or its "
        "purpose is required per Section 508 AED-COP guidance.  The "
        "filename helps users locate, open, and switch between documents."
    )
    doc_types = ["pdf", "docx", "xlsx", "pptx"]

    def is_applicable(self, capture_data: CaptureData) -> bool:
        return capture_data.file_type in _DOC_TYPES and bool(capture_data.file_path)

    async def run_programmatic(
        self, capture_data: CaptureData
    ) -> tuple[ConformanceLevel, float, list[Finding]]:
        findings: list[Finding] = []
        file_path = capture_data.file_path or ""
        if not file_path:
            return ConformanceLevel.NOT_EVALUATED, 0.3, []

        stem = Path(file_path).stem.strip()
        stem_lower = stem.lower().replace("_", " ").replace("-", " ").strip()

        if not stem:
            findings.append(Finding(
                id=_make_finding_id(),
                element=f"<filename>",
                issue="Document has no filename.",
                impact="Users cannot identify the document by its filename.",
                recommendation="Save the document with a descriptive filename.",
                severity=Severity.MEDIUM,
                source="programmatic",
            ))
        elif stem_lower in _GENERIC_FILENAMES:
            findings.append(Finding(
                id=_make_finding_id(),
                element=f"<filename>{Path(file_path).name}</filename>",
                issue=f'Filename is generic: "{Path(file_path).name}".',
                impact=(
                    "A generic filename does not help users identify the "
                    "document when browsing files or switching windows."
                ),
                recommendation=(
                    "Rename to describe the document content or purpose "
                    '(e.g., "Q4_Budget_Report_2026.xlsx" instead of "Book1.xlsx").'
                ),
                severity=Severity.MEDIUM,
                source="programmatic",
            ))

        conformance = self._determine_conformance(findings, total_elements=1)
        return conformance, 1.0, findings


# ═════════════════════════════════════════════════════════════════════════════
#  18. DocCheck_ExcelVitalInfoA1  (AED-COP XLSX Module 4)
# ═════════════════════════════════════════════════════════════════════════════

class DocCheck_ExcelVitalInfoA1(BaseCheck):
    """Flag that vital info in Excel headers/footers/watermarks must be in A1."""

    criterion_id = "DOC-1.3.1-A1"
    criterion_name = "Excel Vital Info in Cell A1"
    level = "A"
    wcag_versions = ["2.0", "2.1", "2.2"]
    guideline = "1.3 Adaptable"
    principle = "1. Perceivable"
    normative_text = (
        "Assistive technology cannot read Excel headers, footers, or "
        "watermarks.  Any vital information (response dates, security "
        "levels, distribution instructions) must be duplicated in cell A1 "
        "per Section 508 AED-COP guidance."
    )
    doc_types = ["xlsx"]

    def is_applicable(self, capture_data: CaptureData) -> bool:
        return capture_data.file_type == "xlsx"

    async def run_programmatic(
        self, capture_data: CaptureData
    ) -> tuple[ConformanceLevel, float, list[Finding]]:
        ctx = capture_data.user_context or {}
        a1_content = ctx.get("cell_a1_content", {})
        findings: list[Finding] = []

        if not a1_content:
            # No A1 data available — fall back to advisory note
            findings.append(Finding(
                id=_make_finding_id(),
                element="<xlsx cell A1>",
                issue=(
                    "Cell A1 content data was not available from the capture "
                    "pipeline. Cannot verify vital info placement."
                ),
                impact=(
                    "Screen readers cannot access headers, footers, or "
                    "watermarks in Excel."
                ),
                recommendation=(
                    "Place vital information (response dates, security levels, "
                    "distribution instructions) in cell A1 before the data begins."
                ),
                severity=Severity.INFO,
                source="programmatic",
            ))
            return ConformanceLevel.NOT_EVALUATED, 0.3, findings

        empty_sheets = []
        populated_sheets = []
        for sheet_name, content in a1_content.items():
            if not content or (isinstance(content, str) and not content.strip()):
                empty_sheets.append(sheet_name)
            else:
                populated_sheets.append(sheet_name)

        for sheet_name in empty_sheets:
            findings.append(Finding(
                id=_make_finding_id(),
                element=f'<xlsx cell A1 in "{sheet_name}">',
                issue=(
                    f"Sheet '{sheet_name}' has no content in cell A1. Per "
                    f"Section 508 AED-COP guidance, vital information from "
                    f"headers/footers/watermarks must be placed in A1."
                ),
                impact=(
                    "Screen readers cannot access headers, footers, or "
                    "watermarks. Any vital info in those areas is invisible "
                    "to assistive technology users."
                ),
                recommendation=(
                    f"If sheet '{sheet_name}' has headers, footers, or "
                    f"watermarks with vital information, duplicate that "
                    f"information in cell A1."
                ),
                severity=Severity.MEDIUM,
                source="programmatic",
            ))

        for sheet_name in populated_sheets:
            findings.append(Finding(
                id=_make_finding_id(),
                element=f'<xlsx cell A1 in "{sheet_name}">',
                issue=(
                    f"Sheet '{sheet_name}' has content in A1. Verify it "
                    f"includes any vital information from headers, footers, "
                    f"or watermarks."
                ),
                impact=(
                    "If headers/footers/watermarks contain vital info not "
                    "duplicated in A1, AT users will miss it."
                ),
                recommendation=(
                    "Confirm that all vital information from headers, "
                    "footers, and watermarks is present in cell A1."
                ),
                severity=Severity.INFO,
                source="programmatic",
            ))

        conformance = self._determine_conformance(findings, total_elements=len(a1_content))
        return conformance, 0.7, findings


# ═════════════════════════════════════════════════════════════════════════════
#  Registry
# ═════════════════════════════════════════════════════════════════════════════

def get_checks() -> list[BaseCheck]:
    return [
        DocCheck_TaggedPDF(),
        DocCheck_DocumentTitle(),
        DocCheck_DocumentLanguage(),
        DocCheck_HeadingStructure(),
        DocCheck_ImageAltText(),
        DocCheck_TableHeaders(),
        DocCheck_ScannedPDF(),
        DocCheck_ReadingOrder(),
        DocCheck_SlideTitle(),
        DocCheck_SpreadsheetStructure(),
        DocCheck_PDFBookmarks(),
        DocCheck_PDFAccessibilityPermission(),
        DocCheck_PDFTabOrder(),
        DocCheck_MeaningfulContent(),
        DocCheck_ListStructure(),
        DocCheck_AutoAdvancingSlides(),
        DocCheck_DescriptiveFilename(),
        DocCheck_ExcelVitalInfoA1(),
    ]
