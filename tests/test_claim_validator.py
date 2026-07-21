"""Regression tests for the measurement-claim validator.

The validator verifies a finding's STRUCTURED ``cited_measurements`` entries
against the page's deterministic capture. There is no prose parsing and no
hard-coded threshold list -- the judge tool schema requires the structured
field, and each SC check module declares its own measurement sources.

Failure modes this pins (verified on municipal-government-site runs):
  - SC 1.4.3: judge cited a text-contrast ratio for an element ANDI never
    measured -- the number was borrowed from a focus-indicator ratio.
  - SC 1.4.11: judge cited UI-component ratios absent from nontext_contrast.
  - SC 1.4.10: judge asserted position:fixed for an element absent from the
    positioned-element scan.

Run with:
    python tests/test_claim_validator.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from functions.claim_validator import validate_measurement_claims  # noqa: E402


class _FakeCapture:
    """Minimal CaptureData stand-in carrying only the fields the validator reads."""
    def __init__(self, andi=None, nontext=None, positioned=None,
                 focus=None, targets=None, dynamic=None,
                 form_fields=None, andi_interactive=None):
        self.andi_contrast_results = andi or []
        self.nontext_contrast = nontext or []
        self.positioned_elements = positioned or []
        self.focus_contrast = focus or []
        self.target_size_measurements = targets or []
        # State-claim sources (Tier 1-C).
        self.dynamic_content = dynamic if dynamic is not None else {}
        self.form_fields = form_fields or []
        self.andi_interactive_results = andi_interactive or []


# The measurement_sources maps the SC modules declare.
_TEXT_CONTRAST = {"contrast_ratio": ("andi_contrast_results", "ratio")}
_NONTEXT_CONTRAST = {"contrast_ratio": ("nontext_contrast", "contrast_ratio")}
_POSITION = {"position": ("positioned_elements", "position")}
_FOCUS_CONTRAST = {"contrast_ratio": ("focus_contrast", "contrast_ratio")}
_TARGET_SIZE = {
    "target_width_px": ("target_size_measurements", "width"),
    "target_height_px": ("target_size_measurements", "height"),
}
# State-claim sources (Tier 1-C).
_PAGE_ANIMATION = {"hasAnimations": ("dynamic_content", "hasAnimations")}
_FIELD_REQUIRED = {"required": ("form_fields", "required")}
_NAME_INC_VISIBLE = {
    "name_inc_visible": ("andi_interactive_results", "name_includes_visible"),
}


# ── Contrast ratio claims (SC 1.4.3 shape) ──────────────────────────────


def test_measured_contrast_ratio_is_kept():
    cap = _FakeCapture(andi=[{"selector": "#hero-text", "ratio": 3.1}])
    findings = [{
        "css_selector": "#hero-text",
        "issue": "Text contrast is 3.1:1, below the 4.5:1 minimum",
        "source": "andi",
        "cited_measurements": [
            {"selector": "#hero-text", "metric": "contrast_ratio", "value": 3.1},
        ],
    }]
    validated, demoted = validate_measurement_claims(findings, cap, _TEXT_CONTRAST)
    assert demoted == 0
    assert validated[0]["source"] == "andi"


def test_fabricated_contrast_ratio_is_demoted():
    """The #prefix-submitButton / 4.44:1 case: ANDI never measured it."""
    cap = _FakeCapture(andi=[])
    findings = [{
        "css_selector": "#prefix-submitButton",
        "issue": "The text contrast ratio is 4.44:1, insufficient",
        "source": "axe",
        "cited_measurements": [
            {"selector": "#prefix-submitButton", "metric": "contrast_ratio", "value": 4.44},
        ],
    }]
    validated, demoted = validate_measurement_claims(findings, cap, _TEXT_CONTRAST)
    assert demoted == 1
    assert validated[0]["source"] == "judge_inference"
    assert "UNVERIFIED MEASUREMENT" in validated[0]["issue"]


def test_rounding_tolerance():
    """Measured 4.53, cited 4.5 -> within tolerance, kept."""
    cap = _FakeCapture(andi=[{"selector": "#t", "ratio": 4.53}])
    findings = [{
        "css_selector": "#t", "issue": "contrast 4.5:1", "source": "andi",
        "cited_measurements": [{"selector": "#t", "metric": "contrast_ratio", "value": 4.5}],
    }]
    validated, demoted = validate_measurement_claims(findings, cap, _TEXT_CONTRAST)
    assert demoted == 0


def test_threshold_in_prose_is_not_a_problem():
    """No threshold list needed: the judge only records MEASURED values in
    cited_measurements. The prose can freely say 'below the 4.5:1 minimum'
    -- the validator never looks at prose, so the threshold is irrelevant."""
    cap = _FakeCapture(andi=[{"selector": "#t", "ratio": 2.0}])
    findings = [{
        "css_selector": "#t",
        "issue": "Contrast 2.0:1, far below the 4.5:1 minimum and the 7:1 AAA bar",
        "source": "andi",
        "cited_measurements": [{"selector": "#t", "metric": "contrast_ratio", "value": 2.0}],
    }]
    validated, demoted = validate_measurement_claims(findings, cap, _TEXT_CONTRAST)
    assert demoted == 0, "only the measured 2.0 is in cited_measurements; thresholds in prose ignored"


# ── Non-text contrast (SC 1.4.11 shape) ─────────────────────────────────


def test_nontext_measured_ratio_kept():
    cap = _FakeCapture(nontext=[{"selector": "#btn", "contrast_ratio": 2.4}])
    findings = [{
        "css_selector": "#btn", "issue": "Component contrast 2.4:1", "source": "axe",
        "cited_measurements": [{"selector": "#btn", "metric": "contrast_ratio", "value": 2.4}],
    }]
    validated, demoted = validate_measurement_claims(findings, cap, _NONTEXT_CONTRAST)
    assert demoted == 0


def test_nontext_fabricated_ratio_demoted():
    cap = _FakeCapture(nontext=[{"selector": "#btn", "contrast_ratio": None}])
    findings = [{
        "css_selector": "#btn", "issue": "Border contrast 1.0:1", "source": "axe",
        "cited_measurements": [{"selector": "#btn", "metric": "contrast_ratio", "value": 1.0}],
    }]
    validated, demoted = validate_measurement_claims(findings, cap, _NONTEXT_CONTRAST)
    assert demoted == 1
    assert validated[0]["source"] == "judge_inference"


# ── Position assertions (SC 1.4.10 shape) ───────────────────────────────


def test_position_claim_demoted_when_not_in_scan():
    cap = _FakeCapture(positioned=[])
    findings = [{
        "css_selector": "div.overlay-container",
        "issue": "Element uses position: fixed which blocks reflow",
        "source": "htmlcs",
        "cited_measurements": [
            {"selector": "div.overlay-container", "metric": "position", "value": "fixed"},
        ],
    }]
    validated, demoted = validate_measurement_claims(findings, cap, _POSITION)
    assert demoted == 1
    assert validated[0]["source"] == "judge_inference"


def test_position_claim_kept_when_in_scan():
    cap = _FakeCapture(positioned=[{"selector": "#back-to-top", "position": "fixed"}])
    findings = [{
        "css_selector": "#back-to-top",
        "issue": "Element has position: fixed and overlaps content at 320px",
        "source": "htmlcs",
        "cited_measurements": [
            {"selector": "#back-to-top", "metric": "position", "value": "fixed"},
        ],
    }]
    validated, demoted = validate_measurement_claims(findings, cap, _POSITION)
    assert demoted == 0
    assert validated[0]["source"] == "htmlcs"


# ── Focus-indicator contrast (SC 2.4.7) ─────────────────────────────────


def test_focus_contrast_measured_ratio_kept():
    cap = _FakeCapture(focus=[{"selector": "#btn", "contrast_ratio": 4.6}])
    findings = [{
        "css_selector": "#btn", "issue": "focus outline contrast 4.6:1",
        "source": "programmatic",
        "cited_measurements": [{"selector": "#btn", "metric": "contrast_ratio", "value": "4.6"}],
    }]
    validated, demoted = validate_measurement_claims(findings, cap, _FOCUS_CONTRAST)
    assert demoted == 0


def test_focus_contrast_fabricated_ratio_demoted():
    cap = _FakeCapture(focus=[{"selector": "#btn", "contrast_ratio": 4.6}])
    findings = [{
        "css_selector": "#btn", "issue": "focus indicator contrast only 1.2:1",
        "source": "andi",
        "cited_measurements": [{"selector": "#btn", "metric": "contrast_ratio", "value": "1.2"}],
    }]
    validated, demoted = validate_measurement_claims(findings, cap, _FOCUS_CONTRAST)
    assert demoted == 1
    assert validated[0]["source"] == "judge_inference"


# ── Target size (SC 2.5.8 / 2.5.5) ──────────────────────────────────────


def test_target_size_measured_kept():
    cap = _FakeCapture(targets=[{"selector": "#b", "width": 18.0, "height": 18.0}])
    findings = [{
        "css_selector": "#b", "issue": "target is 18x18px, below 24px",
        "source": "axe",
        "cited_measurements": [
            {"selector": "#b", "metric": "target_width_px", "value": "18"},
            {"selector": "#b", "metric": "target_height_px", "value": "18"},
        ],
    }]
    validated, demoted = validate_measurement_claims(findings, cap, _TARGET_SIZE)
    assert demoted == 0


def test_target_size_fabricated_demoted():
    """SC 2.5.5 case: judge invents a target height the capture never measured."""
    cap = _FakeCapture(targets=[{"selector": "#b", "width": 96.0, "height": 18.0}])
    findings = [{
        "css_selector": "#b", "issue": "footer link is only 24px tall",
        "source": "judge_inference",
        "cited_measurements": [{"selector": "#b", "metric": "target_height_px", "value": "24"}],
    }]
    validated, demoted = validate_measurement_claims(findings, cap, _TARGET_SIZE)
    assert demoted == 1
    assert "UNVERIFIED MEASUREMENT" in validated[0]["issue"]


def test_target_size_not_in_capture_demoted():
    """A target cited but absent from the measurement scan entirely."""
    cap = _FakeCapture(targets=[{"selector": "#known", "width": 40.0, "height": 40.0}])
    findings = [{
        "css_selector": "#never-measured", "issue": "target 20x20px",
        "source": "axe",
        "cited_measurements": [{"selector": "#never-measured", "metric": "target_width_px", "value": "20"}],
    }]
    validated, demoted = validate_measurement_claims(findings, cap, _TARGET_SIZE)
    assert demoted == 1


# ── Page-level state claims — dict source (SC 2.2.2 / 2.3.3) ────────────


def test_page_animation_claim_contradicting_capture_demoted():
    """SC 2.2.2 case: judge claims the page has animation while the
    deterministic dynamic-content probe measured none."""
    cap = _FakeCapture(dynamic={"hasAnimations": False, "hasAutoRefresh": False})
    findings = [{
        "css_selector": "body",
        "issue": "CSS animations run indefinitely without a pause control",
        "source": "visual_ai",
        "cited_measurements": [
            {"selector": "body", "metric": "hasAnimations", "value": "true"},
        ],
    }]
    validated, demoted = validate_measurement_claims(findings, cap, _PAGE_ANIMATION)
    assert demoted == 1
    assert validated[0]["source"] == "judge_inference"
    assert "UNVERIFIED MEASUREMENT" in validated[0]["issue"]


def test_page_animation_claim_matching_capture_kept():
    cap = _FakeCapture(dynamic={"hasAnimations": False})
    findings = [{
        "css_selector": "body", "issue": "no moving content present",
        "source": "programmatic",
        "cited_measurements": [
            {"selector": "body", "metric": "hasAnimations", "value": "false"},
        ],
    }]
    validated, demoted = validate_measurement_claims(findings, cap, _PAGE_ANIMATION)
    assert demoted == 0


def test_page_fact_selector_is_ignored():
    """A page-level fact is selector-agnostic — any selector resolves to
    the same dynamic_content value."""
    cap = _FakeCapture(dynamic={"hasAnimations": True})
    findings = [{
        "css_selector": "#whatever", "issue": "page animates",
        "source": "visual_ai",
        "cited_measurements": [
            {"selector": "#whatever", "metric": "hasAnimations", "value": "true"},
        ],
    }]
    validated, demoted = validate_measurement_claims(findings, cap, _PAGE_ANIMATION)
    assert demoted == 0  # capture says hasAnimations=True, claim matches


# ── Element-level state claims (SC 3.3.2 / 2.5.3) ───────────────────────


def test_required_state_contradicting_capture_demoted():
    """SC 3.3.2 case: judge claims a field is not required when the
    captured form_fields[].required says it is."""
    cap = _FakeCapture(form_fields=[{"selector": "#email", "required": True}])
    findings = [{
        "css_selector": "#email",
        "issue": "the email field does not indicate it is required",
        "source": "programmatic",
        "cited_measurements": [
            {"selector": "#email", "metric": "required", "value": "false"},
        ],
    }]
    validated, demoted = validate_measurement_claims(findings, cap, _FIELD_REQUIRED)
    assert demoted == 1
    assert validated[0]["source"] == "judge_inference"


def test_required_state_matching_capture_kept():
    cap = _FakeCapture(form_fields=[{"selector": "#email", "required": True}])
    findings = [{
        "css_selector": "#email", "issue": "required field lacks visible indicator",
        "source": "programmatic",
        "cited_measurements": [
            {"selector": "#email", "metric": "required", "value": "true"},
        ],
    }]
    validated, demoted = validate_measurement_claims(findings, cap, _FIELD_REQUIRED)
    assert demoted == 0


def test_name_inc_visible_contradicting_andi_demoted():
    """SC 2.5.3 case: htmlcs F96 finding claims the visible label is not in
    the accessible name, but ANDI measured name_includes_visible=True."""
    cap = _FakeCapture(andi_interactive=[
        {"selector": "#lang-btn", "name_includes_visible": True},
    ])
    findings = [{
        "css_selector": "#lang-btn",
        "issue": "visible text 'English' is not part of the accessible name",
        "source": "htmlcs",
        "cited_measurements": [
            {"selector": "#lang-btn", "metric": "name_inc_visible", "value": "false"},
        ],
    }]
    validated, demoted = validate_measurement_claims(findings, cap, _NAME_INC_VISIBLE)
    assert demoted == 1
    assert validated[0]["source"] == "judge_inference"


# ── Non-interference cases ──────────────────────────────────────────────


def test_finding_with_empty_cited_measurements_untouched():
    cap = _FakeCapture(andi=[])
    findings = [{
        "css_selector": "#x", "issue": "Missing accessible name", "source": "axe",
        "cited_measurements": [],
    }]
    validated, demoted = validate_measurement_claims(findings, cap, _TEXT_CONTRAST)
    assert demoted == 0
    assert validated[0]["source"] == "axe"


def test_finding_with_no_cited_measurements_key_untouched():
    cap = _FakeCapture(andi=[])
    findings = [{"css_selector": "#x", "issue": "Missing label", "source": "axe"}]
    validated, demoted = validate_measurement_claims(findings, cap, _TEXT_CONTRAST)
    assert demoted == 0


def test_metric_with_no_declared_source_is_left_alone():
    """If the SC declares no source for a metric, the claim is not checked."""
    cap = _FakeCapture()
    findings = [{
        "css_selector": "#x", "issue": "target is 18px", "source": "axe",
        "cited_measurements": [{"selector": "#x", "metric": "target_width_px", "value": 18}],
    }]
    # _TEXT_CONTRAST only knows 'contrast_ratio', not 'target_width_px'.
    validated, demoted = validate_measurement_claims(findings, cap, _TEXT_CONTRAST)
    assert demoted == 0


def test_empty_measurement_sources_disables_enforcement():
    cap = _FakeCapture(andi=[])
    findings = [{
        "css_selector": "#x", "issue": "contrast 9:1", "source": "axe",
        "cited_measurements": [{"selector": "#x", "metric": "contrast_ratio", "value": 9.0}],
    }]
    validated, demoted = validate_measurement_claims(findings, cap, {})
    assert demoted == 0
    validated, demoted = validate_measurement_claims(findings, cap, None)
    assert demoted == 0


def test_finding_objects_accepted():
    """The validator coerces Finding-like objects, not just dicts."""
    class _F:
        css_selector = "#hero-text"
        element = "Hero text"
        issue = "Contrast 9.9:1 fails"
        impact = ""
        recommendation = ""
        severity = "high"
        source = "andi"
        cited_measurements = [
            {"selector": "#hero-text", "metric": "contrast_ratio", "value": 9.9},
        ]
    cap = _FakeCapture(andi=[{"selector": "#hero-text", "ratio": 3.0}])
    validated, demoted = validate_measurement_claims([_F()], cap, _TEXT_CONTRAST)
    assert demoted == 1
    assert validated[0]["source"] == "judge_inference"


# ── Runner ──────────────────────────────────────────────────────────────


if __name__ == "__main__":
    failures = 0
    tests = [
        (n, fn) for n, fn in sorted(globals().items())
        if n.startswith("test_") and callable(fn)
    ]
    for name, fn in tests:
        try:
            fn()
            print(f"  ok  {name}")
        except AssertionError as exc:
            failures += 1
            print(f"  FAIL  {name}: {exc}")
        except Exception as exc:
            failures += 1
            print(f"  ERROR {name}: {type(exc).__name__}: {exc}")
    print(f"\n{len(tests) - failures}/{len(tests)} tests passed")
    sys.exit(1 if failures else 0)
