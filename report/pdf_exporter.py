"""Export an HTML report to PDF using Playwright."""
from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger(__name__)


async def export_pdf(
    html_path: str | Path,
    output_path: str | Path,
) -> str:
    """Render *html_path* to a PDF at *output_path*.

    Uses Playwright's Chromium engine for high-fidelity rendering.
    The output is US Letter format (8.5 x 11 in) with 0.5-inch margins.

    Parameters
    ----------
    html_path : str | Path
        Path to the source HTML file.
    output_path : str | Path
        Destination path for the generated PDF.

    Returns
    -------
    str
        The absolute path of the written PDF file.
    """
    html_path = Path(html_path).resolve()
    output_path = Path(output_path).resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)

    if not html_path.exists():
        raise FileNotFoundError(f"HTML source not found: {html_path}")

    try:
        from playwright.async_api import async_playwright
    except ImportError as exc:
        raise ImportError(
            "Playwright is required for PDF export. "
            "Install it with: pip install playwright && playwright install chromium"
        ) from exc

    file_url = html_path.as_uri()

    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        page = await browser.new_page()

        await page.goto(file_url, wait_until="networkidle")

        await page.pdf(
            path=str(output_path),
            format="Letter",
            margin={
                "top": "0.5in",
                "right": "0.5in",
                "bottom": "0.5in",
                "left": "0.5in",
            },
            print_background=True,
        )

        await browser.close()

    logger.info("PDF exported to %s", output_path)
    return str(output_path)
