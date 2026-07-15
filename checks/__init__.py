"""WCAG check modules for accessibility testing."""
from checks.base import BaseCheck
from checks.registry import (
    get_all_checks,
    get_check_by_id,
    get_check_count_summary,
    get_checks_by_guideline,
    get_checks_for_version,
)

__all__ = [
    "BaseCheck",
    "get_all_checks",
    "get_check_by_id",
    "get_check_count_summary",
    "get_checks_by_guideline",
    "get_checks_for_version",
]
