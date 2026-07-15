"""Assistive technology simulation for WCAG testing.

Simulates how real screen readers (JAWS, NVDA, VoiceOver) and
keyboard navigation experience a page by walking the browser's
computed accessibility tree.
"""
from at_simulation.announcements import (
    is_meaningless_name,
    render_announcement,
    render_announcement_issues,
)
from at_simulation.screen_reader import simulate_screen_reader
from at_simulation.keyboard_nav import (
    simulate_heading_navigation,
    simulate_form_navigation,
    simulate_landmark_navigation,
    simulate_link_navigation,
    simulate_table_navigation,
    simulate_tab_order_comparison,
)

__all__ = [
    "is_meaningless_name",
    "render_announcement",
    "render_announcement_issues",
    "simulate_screen_reader",
    "simulate_heading_navigation",
    "simulate_form_navigation",
    "simulate_landmark_navigation",
    "simulate_link_navigation",
    "simulate_table_navigation",
    "simulate_tab_order_comparison",
]
