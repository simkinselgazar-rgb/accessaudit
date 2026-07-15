"""WCAG check registry -- discovers and indexes all check modules."""
from __future__ import annotations

import logging

from checks.base import BaseCheck

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Import all check modules
# ---------------------------------------------------------------------------

from checks.checks_1_1 import get_checks as _get_1_1
from checks.checks_1_2 import get_checks as _get_1_2
from checks.checks_1_2_aaa import get_checks as _get_1_2_aaa
from checks.checks_1_3 import get_checks as _get_1_3
from checks.checks_1_3_aaa import get_checks as _get_1_3_aaa
from checks.checks_1_4 import get_checks as _get_1_4
from checks.checks_1_4_aaa import get_checks as _get_1_4_aaa
from checks.checks_2_1 import get_checks as _get_2_1
from checks.checks_2_1_aaa import get_checks as _get_2_1_aaa
from checks.checks_2_2 import get_checks as _get_2_2
from checks.checks_2_2_aaa import get_checks as _get_2_2_aaa
from checks.checks_2_3 import get_checks as _get_2_3
from checks.checks_2_3_aaa import get_checks as _get_2_3_aaa
from checks.checks_2_4 import get_checks as _get_2_4
from checks.checks_2_4_22 import get_checks as _get_2_4_22
from checks.checks_2_4_aaa import get_checks as _get_2_4_aaa
from checks.checks_2_5 import get_checks as _get_2_5
from checks.checks_2_5_22 import get_checks as _get_2_5_22
from checks.checks_2_5_aaa import get_checks as _get_2_5_aaa
from checks.checks_3_1 import get_checks as _get_3_1
from checks.checks_3_1_aaa import get_checks as _get_3_1_aaa
from checks.checks_3_2 import get_checks as _get_3_2
from checks.checks_3_2_22 import get_checks as _get_3_2_22
from checks.checks_3_2_aaa import get_checks as _get_3_2_aaa
from checks.checks_3_3 import get_checks as _get_3_3
from checks.checks_3_3_22 import get_checks as _get_3_3_22
from checks.checks_3_3_aaa import get_checks as _get_3_3_aaa
from checks.checks_4_1 import get_checks as _get_4_1
from checks.checks_cav import get_checks as _get_cav
from checks.checks_doc import get_checks as _get_doc


# ---------------------------------------------------------------------------
# Registry builders
# ---------------------------------------------------------------------------

_ALL_GETTERS = [
    _get_1_1,
    _get_1_2,
    _get_1_2_aaa,
    _get_1_3,
    _get_1_3_aaa,
    _get_1_4,
    _get_1_4_aaa,
    _get_2_1,
    _get_2_1_aaa,
    _get_2_2,
    _get_2_2_aaa,
    _get_2_3,
    _get_2_3_aaa,
    _get_2_4,
    _get_2_4_22,
    _get_2_4_aaa,
    _get_2_5,
    _get_2_5_22,
    _get_2_5_aaa,
    _get_3_1,
    _get_3_1_aaa,
    _get_3_2,
    _get_3_2_22,
    _get_3_2_aaa,
    _get_3_3,
    _get_3_3_22,
    _get_3_3_aaa,
    _get_4_1,
    _get_cav,
    _get_doc,
]

# Cached flat list (built on first call)
_ALL_CHECKS: list[BaseCheck] | None = None

# Level ordering for filtering
_LEVEL_INCLUDES = {
    "A": {"A"},
    "AA": {"A", "AA"},
    "AAA": {"A", "AA", "AAA"},
}


def get_all_checks() -> list[BaseCheck]:
    """Return every registered check instance (all versions, all levels)."""
    global _ALL_CHECKS
    if _ALL_CHECKS is None:
        checks: list[BaseCheck] = []
        for getter in _ALL_GETTERS:
            try:
                checks.extend(getter())
            except Exception as exc:
                logger.error("Failed to load checks from %s: %s", getter, exc)
        _ALL_CHECKS = checks
        logger.info("Registered %d WCAG checks", len(_ALL_CHECKS))
    return list(_ALL_CHECKS)


def get_checks_for_version(
    version: str = "2.2",
    level: str = "AA",
    file_type: str | None = None,
) -> list[BaseCheck]:
    """Return checks applicable to a given WCAG *version*, *level*, and content type.

    Parameters
    ----------
    version:
        WCAG version string, e.g. ``"2.0"``, ``"2.1"``, or ``"2.2"``.
    level:
        Maximum conformance level to include: ``"A"``, ``"AA"``, or
        ``"AAA"``.
    file_type:
        Content type: ``None`` or ``"web"`` for web pages, or
        ``"pdf"``, ``"docx"``, ``"xlsx"``, ``"pptx"`` for documents.
        When set, only checks applicable to that content type are returned.
        Document checks (``doc_types`` set) only run for matching file types.
        Web-only checks (``web_only=True``) skip for documents.

    Returns
    -------
    list[BaseCheck]
        Checks applicable to the given version, level, and content type.
    """
    allowed_levels = _LEVEL_INCLUDES.get(level.upper(), {"A", "AA"})
    is_document = file_type in ("pdf", "docx", "xlsx", "pptx")
    is_web = not is_document

    result = []
    for check in get_all_checks():
        # Version and level filter
        if version not in check.wcag_versions:
            continue
        if check.level not in allowed_levels:
            continue

        # Content type filter
        if check.doc_types:
            # This is a document-specific check — only include if
            # the file type matches
            if not is_document or file_type not in check.doc_types:
                continue
        elif is_document and check.web_only:
            # Web-only check on a document — skip
            continue

        result.append(check)

    return result


def get_check_by_id(criterion_id: str) -> BaseCheck | None:
    """Look up a single check by its criterion ID (e.g. ``"1.1.1"``)."""
    for check in get_all_checks():
        if check.criterion_id == criterion_id:
            return check
    return None


def get_checks_by_guideline(guideline_prefix: str) -> list[BaseCheck]:
    """Return all checks whose criterion_id starts with *guideline_prefix*.

    For example, ``get_checks_by_guideline("1.4")`` returns all 1.4.x
    checks.
    """
    return [
        check
        for check in get_all_checks()
        if check.criterion_id.startswith(guideline_prefix)
    ]


def get_check_count_summary() -> dict:
    """Return a summary of check counts by level and version."""
    all_checks = get_all_checks()
    summary = {
        "total": len(all_checks),
        "by_level": {"A": 0, "AA": 0, "AAA": 0},
        "by_version": {"2.0": 0, "2.1": 0, "2.2": 0},
    }
    for check in all_checks:
        level = check.level
        if level in summary["by_level"]:
            summary["by_level"][level] += 1
        for ver in check.wcag_versions:
            if ver in summary["by_version"]:
                summary["by_version"][ver] += 1
    return summary
