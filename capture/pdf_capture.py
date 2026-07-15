"""PDF document capture for WCAG accessibility testing.

Uses PyMuPDF (fitz) to extract text, images, links, structure,
and render page screenshots.
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path

import fitz  # PyMuPDF

from models import CaptureData
from config import PDF_DPI

logger = logging.getLogger(__name__)


def capture_pdf(file_path: str, review_dir: str) -> CaptureData:
    """Capture a PDF document for WCAG accessibility testing.

    Renders pages to PNG, extracts text, links, images, headings,
    and builds a pseudo-HTML representation for downstream checks.

    Args:
        file_path: Path to the PDF file.
        review_dir: Path to the review output directory.

    Returns:
        A populated CaptureData instance.
    """
    captures_dir = os.path.join(review_dir, "captures")
    os.makedirs(captures_dir, exist_ok=True)

    capture_data = CaptureData(
        file_path=file_path,
        file_type="pdf",
        review_dir=review_dir,
        captures_dir=captures_dir,
    )

    try:
        doc = fitz.open(file_path)
    except Exception:
        logger.exception("Failed to open PDF: %s", file_path)
        return capture_data

    try:
        capture_data.title = doc.metadata.get("title", "") or Path(file_path).stem

        pages_dir = os.path.join(captures_dir, "pages")
        os.makedirs(pages_dir, exist_ok=True)

        all_text_parts: list[str] = []
        all_links: list[dict] = []
        all_images: list[dict] = []
        page_texts: list[dict] = []
        page_image_paths: list[str] = []
        scanned_page_count = 0

        # ── Per-page processing ──────────────────────────────────────
        for page_num in range(len(doc)):
            page = doc[page_num]

            # Render to PNG
            try:
                pix = page.get_pixmap(dpi=PDF_DPI)
                img_path = os.path.join(pages_dir, f"page_{page_num + 1}.png")
                pix.save(img_path)
                page_image_paths.append(img_path)
                if page_num == 0:
                    capture_data.full_page_path = img_path
                    capture_data.viewport_path = img_path
            except Exception:
                logger.exception("Failed to render page %d", page_num + 1)

            # Extract text
            try:
                text = page.get_text("text")
                all_text_parts.append(text)
                page_texts.append({
                    "page": page_num + 1,
                    "text": text,
                })
            except Exception:
                logger.exception("Failed to extract text from page %d", page_num + 1)

            # Extract links
            try:
                for link in page.get_links():
                    link_info: dict = {
                        "page": page_num + 1,
                        "kind": link.get("kind", 0),
                    }
                    if "uri" in link:
                        link_info["href"] = link["uri"]
                        link_info["text"] = link.get("uri", "")
                    elif "page" in link:
                        link_info["href"] = f"#page-{link['page'] + 1}"
                        link_info["text"] = f"Page {link['page'] + 1}"
                    rect = link.get("from")
                    if rect:
                        link_info["rect"] = {
                            "x": rect.x0 if hasattr(rect, "x0") else rect[0],
                            "y": rect.y0 if hasattr(rect, "y0") else rect[1],
                            "width": (rect.x1 - rect.x0) if hasattr(rect, "x1") else (rect[2] - rect[0]),
                            "height": (rect.y1 - rect.y0) if hasattr(rect, "y1") else (rect[3] - rect[1]),
                        }
                    all_links.append(link_info)
            except Exception:
                logger.exception("Failed to extract links from page %d", page_num + 1)

            # Extract embedded images
            try:
                for img_index, img in enumerate(page.get_images(full=True)):
                    xref = img[0]
                    try:
                        base_image = doc.extract_image(xref)
                        if base_image:
                            ext = base_image.get("ext", "png")
                            img_filename = f"page{page_num + 1}_img{img_index}.{ext}"
                            img_path = os.path.join(captures_dir, "images", img_filename)
                            os.makedirs(os.path.dirname(img_path), exist_ok=True)
                            with open(img_path, "wb") as f:
                                f.write(base_image["image"])
                            all_images.append({
                                "page": page_num + 1,
                                "path": img_path,
                                "width": base_image.get("width", 0),
                                "height": base_image.get("height", 0),
                                "alt": "",  # PDFs don't have alt text in image data
                            })
                    except Exception:
                        logger.debug("Failed to extract image xref %d on page %d", xref, page_num + 1)
            except Exception:
                logger.exception("Failed to extract images from page %d", page_num + 1)

            # Detect scanned pages: has images but very little extractable text
            page_text_len = len(all_text_parts[-1]) if all_text_parts else 0
            page_has_images = any(
                img.get("page") == page_num + 1 for img in all_images
            )
            if page_has_images and page_text_len < 50:
                scanned_page_count += 1

        # ── Document-level data ──────────────────────────────────────

        # Check for tagged PDF (accessibility structure)
        is_tagged = False
        has_struct_tree = False
        try:
            catalog = doc.pdf_catalog()
            if catalog:
                mark_info = doc.xref_get_key(catalog, "MarkInfo")
                if mark_info and mark_info[0] != "null":
                    is_tagged = True
                struct_tree = doc.xref_get_key(catalog, "StructTreeRoot")
                if struct_tree and struct_tree[0] != "null":
                    has_struct_tree = True
        except Exception:
            logger.debug("Could not check PDF tagging status")

        # Extract document language from catalog /Lang entry
        doc_language = ""
        try:
            catalog = doc.pdf_catalog()
            if catalog:
                lang_entry = doc.xref_get_key(catalog, "Lang")
                if lang_entry and lang_entry[0] != "null":
                    # Value is a PDF string like "(en-US)" — strip parens
                    doc_language = lang_entry[1].strip("()")
        except Exception:
            logger.debug("Could not extract PDF language")

        # Check if accessibility permissions are blocked
        has_a11y_permission = True
        try:
            # PyMuPDF permission flags: bit 10 = accessibility
            permissions = doc.permissions
            if permissions is not None and not (permissions & 0x200):
                has_a11y_permission = False
        except Exception:
            pass  # default to has_a11y_permission=True if permissions cannot be probed

        # Check bookmarks (outlines)
        has_bookmarks = False
        try:
            toc = doc.get_toc()
            has_bookmarks = len(toc) > 0
        except Exception:
            pass  # default to has_bookmarks=False if TOC cannot be read

        # Check tab order setting on pages
        tab_order_set = False
        try:
            for page_num in range(len(doc)):  # Check all pages
                page = doc[page_num]
                page_xref = page.xref
                tabs_entry = doc.xref_get_key(page_xref, "Tabs")
                if tabs_entry and tabs_entry[0] != "null" and tabs_entry[1] == "/S":
                    tab_order_set = True
                    break
        except Exception:
            pass  # default to tab_order_set=False if xref probing fails

        # Extract headings from TOC (table of contents)
        headings: list[dict] = []
        try:
            toc = doc.get_toc()
            for level, title, page_no in toc:
                headings.append({
                    "tag": f"h{min(level, 6)}",
                    "level": min(level, 6),
                    "text": title,
                    "page": page_no,
                })
        except Exception:
            logger.debug("Failed to extract TOC headings")

        # ── Build pseudo-HTML ────────────────────────────────────────
        pseudo_html = _build_pseudo_html(
            capture_data.title, page_texts, headings, all_links, all_images,
            is_tagged, has_struct_tree, doc_language,
        )

        # Populate capture data
        capture_data.html = pseudo_html
        capture_data.headings = headings
        capture_data.links = all_links
        capture_data.images = all_images
        # Store all page images so the AI can see every page.
        # The check pipeline's chunked analysis handles batching.
        capture_data.observation_frames = page_image_paths

        capture_data.user_context = {
            "is_tagged_pdf": is_tagged,
            "has_struct_tree": has_struct_tree,
            "page_count": len(doc),
            "page_texts": page_texts,
            "doc_language": doc_language,
            "has_bookmarks": has_bookmarks,
            "has_a11y_permission": has_a11y_permission,
            "tab_order_set": tab_order_set,
            "page_image_paths": page_image_paths,
            "scanned_page_count": scanned_page_count,
            "is_likely_scanned": scanned_page_count > len(doc) / 2,
        }

    except Exception:
        logger.exception("PDF capture failed for %s", file_path)
    finally:
        doc.close()

    return capture_data


def _build_pseudo_html(
    title: str,
    page_texts: list[dict],
    headings: list[dict],
    links: list[dict],
    images: list[dict],
    is_tagged: bool,
    has_struct_tree: bool,
    doc_language: str = "",
) -> str:
    """Build a pseudo-HTML document from extracted PDF data.

    This enables downstream WCAG checks that expect HTML input.
    """
    lang = doc_language or "en"
    parts = [
        "<!DOCTYPE html>",
        f"<html lang=\"{_escape(lang)}\">",
        "<head>",
        f"  <title>{_escape(title)}</title>",
        f"  <meta name=\"pdf-tagged\" content=\"{'true' if is_tagged else 'false'}\">",
        f"  <meta name=\"pdf-struct-tree\" content=\"{'true' if has_struct_tree else 'false'}\">",
        f"  <meta name=\"pdf-language\" content=\"{_escape(doc_language)}\">",
        "</head>",
        "<body>",
    ]

    # Headings from TOC
    if headings:
        parts.append("  <nav aria-label=\"Table of Contents\">")
        for h in headings:
            tag = h["tag"]
            parts.append(f"    <{tag}>{_escape(h['text'])}</{tag}>")
        parts.append("  </nav>")

    # Page content
    for pt in page_texts:
        page_num = pt["page"]
        text = pt["text"].strip()
        if text:
            parts.append(f"  <section aria-label=\"Page {page_num}\">")
            for paragraph in text.split("\n\n"):
                paragraph = paragraph.strip()
                if paragraph:
                    parts.append(f"    <p>{_escape(paragraph)}</p>")
            parts.append("  </section>")

    # Links
    if links:
        parts.append("  <nav aria-label=\"Links\">")
        for link in links:
            href = _escape(link.get("href", ""))
            text = _escape(link.get("text", href))
            parts.append(f"    <a href=\"{href}\">{text}</a>")
        parts.append("  </nav>")

    # Images
    for img in images:
        alt = _escape(img.get("alt", ""))
        parts.append(f"  <img src=\"{_escape(img.get('path', ''))}\" alt=\"{alt}\">")

    parts.extend(["</body>", "</html>"])
    return "\n".join(parts)


def _escape(text: str) -> str:
    """Minimal HTML entity escaping."""
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )
