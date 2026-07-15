"""Per-criterion prompt templates for AI analysis.

Each criterion has a prompt file defining:
- Specific pass/fail/NA/NE conditions with examples
- What to look for visually
- Common mistakes the AI should catch
"""
from __future__ import annotations

import json
from pathlib import Path

PROMPTS_DIR = Path(__file__).parent


def load_criterion_prompt(criterion_id: str) -> dict | None:
    """Load the prompt template for a given criterion.

    Returns a dict with keys: pass_conditions, fail_conditions,
    na_conditions, ne_conditions, visual_checks, common_mistakes,
    examples, or None if no prompt file exists.
    """
    filename = criterion_id.replace(".", "_") + ".json"
    filepath = PROMPTS_DIR / filename
    if filepath.exists():
        return json.loads(filepath.read_text(encoding="utf-8"))
    return None
