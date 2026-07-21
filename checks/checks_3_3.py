"""WCAG Guideline 3.3 - Input Assistance (A/AA) checks."""
from __future__ import annotations

import re

from checks.base import BaseCheck, _make_finding_id
from models import (
    CaptureData,
    ConformanceLevel,
    Finding,
    Severity,
)


class Check_3_3_1(BaseCheck):
    """SC 3.3.1 Error Identification (Level A)."""

    criterion_id = "3.3.1"
    criterion_name = "Error Identification"
    level = "A"
    wcag_versions = ["2.0", "2.1", "2.2"]
    guideline = "3.3 Input Assistance"
    principle = "3. Understandable"
    ict_baseline = "20"
    tt_tests = ["20.A"]
    normative_text = (
        "If an input error is automatically detected, the item that is "
        "in error is identified and the error is described to the user "
        "in text."
    )

    def is_applicable(self, capture_data: CaptureData) -> bool:
        # 3.3.1 only applies when there are automatically detected input
        # errors.  That requires either captured form_errors OR form fields
        # with validation constraints (required, pattern, type-based
        # validation).
        if capture_data.form_errors:
            return True
        _validating_types = {
            "email", "url", "number", "tel", "date", "time",
            "datetime-local", "month", "week",
        }
        for field in capture_data.form_fields:
            ft = (field.get("type") or "").lower()
            if ft in ("hidden", "submit", "button", "reset", "text"):
                continue
            if (field.get("required", False)
                    or field.get("aria_required", field.get("aria-required", ""))
                    or field.get("pattern", "")
                    or ft in _validating_types):
                return True
        return False

    async def run_programmatic(
        self, capture_data: CaptureData
    ) -> tuple[ConformanceLevel, float, list[Finding]]:
        findings: list[Finding] = []

        # Check form fields for proper error identification
        for field in capture_data.form_fields:
            selector = field.get("selector", "input")
            field_type = (field.get("type") or "").lower()
            required = field.get("required", False) or field.get("aria_required", field.get("aria-required", ""))
            aria_invalid = field.get("aria_invalid", field.get("aria-invalid", ""))
            has_error = field.get("has_error", False)
            error_message = (field.get("error_message") or "").strip()

            if field_type in ("hidden", "submit", "button", "reset"):
                continue

            # If field has required but no validation mechanism
            if required and not aria_invalid:
                # Check if there's a pattern for custom validation
                pattern = field.get("pattern", "")
                has_validation = bool(pattern or field.get("has_validation_handler", False))
                if not has_validation:
                    findings.append(Finding(
                        id=_make_finding_id(),
                        element=selector,
                        issue=(
                            "Required field lacks aria-invalid for "
                            "error state indication"
                        ),
                        impact=(
                            "Screen reader users may not be informed when "
                            "this field has an error."
                        ),
                        recommendation=(
                            "Set aria-invalid=\"true\" when the field has an "
                            "error, and associate an error message via "
                            "aria-describedby."
                        ),
                        severity=Severity.MEDIUM,
                    ))

        # Check raw validation_messages from form submission testing.
        # The capture phase submits forms with empty required fields and
        # records native browser validation messages + aria-invalid state.
        for error in capture_data.form_errors:
            validation_msgs = error.get("validation_messages")
            if isinstance(validation_msgs, list):
                for vm in validation_msgs:
                    msg = (vm.get("validationMessage") or "").strip()
                    aria_invalid = (vm.get("ariaInvalid") or "").strip()
                    aria_errormsg = (vm.get("ariaErrormessage") or "").strip()
                    field_name = vm.get("name", "")
                    field_tag = vm.get("tag", "input")

                    # If native validation message exists but no aria-invalid
                    if msg and aria_invalid not in ("true", "spelling", "grammar"):
                        findings.append(Finding(
                            id=_make_finding_id(),
                            element=f"{field_tag}[name=\"{field_name}\"]" if field_name else field_tag,
                            issue=(
                                f"Required field triggers browser validation "
                                f"(\"{msg}\") but does not set "
                                f"aria-invalid=\"true\""
                            ),
                            impact=(
                                "Screen readers rely on aria-invalid to announce "
                                "the error state. Without it, AT users may not "
                                "know the field has an error."
                            ),
                            recommendation=(
                                "Set aria-invalid=\"true\" on the field when "
                                "validation fails, and provide a text error "
                                "message associated via aria-describedby."
                            ),
                            severity=Severity.MEDIUM,
                        ))

                    # If no aria-errormessage is set for custom error messaging
                    if msg and not aria_errormsg and aria_invalid == "true":
                        findings.append(Finding(
                            id=_make_finding_id(),
                            element=f"{field_tag}[name=\"{field_name}\"]" if field_name else field_tag,
                            issue=(
                                f"Field has aria-invalid=\"true\" but no "
                                f"aria-errormessage or aria-describedby for "
                                f"custom error text"
                            ),
                            impact=(
                                "Screen reader users know the field is invalid "
                                "but may not hear the specific error description."
                            ),
                            recommendation=(
                                "Add aria-errormessage or aria-describedby "
                                "pointing to an element containing the error text."
                            ),
                            severity=Severity.LOW,
                        ))

        # Check enriched form errors (pre-processed data from analysis
        # phase). A MISSING key means "not measured" and must never be
        # treated as a failure — findings only fire on an explicit False.
        for error in capture_data.form_errors:
            selector = error.get("selector") or error.get("form_selector") or "form"
            has_text_description = error.get("has_text_description")
            identifies_field = error.get("identifies_field")
            programmatic_association = error.get("programmatic_association")

            if has_text_description is False:
                findings.append(Finding(
                    id=_make_finding_id(),
                    element=selector,
                    issue="Error detected but not described in text",
                    impact="Users cannot understand what went wrong.",
                    recommendation="Provide a text description of the error.",
                    severity=Severity.HIGH,
                ))
            elif has_text_description and identifies_field is False:
                findings.append(Finding(
                    id=_make_finding_id(),
                    element=selector,
                    issue="Error message does not identify the field in error",
                    impact="Users with many form fields cannot locate the error.",
                    recommendation=(
                        "Include the field label in the error message, "
                        "or use aria-describedby to associate error with field."
                    ),
                    severity=Severity.MEDIUM,
                ))

            if programmatic_association is False:
                findings.append(Finding(
                    id=_make_finding_id(),
                    element=selector,
                    issue="Error message is not programmatically associated with the field",
                    impact="Screen readers may not announce the error to users.",
                    recommendation=(
                        "Use aria-describedby or aria-errormessage to "
                        "associate the error message with the input field."
                    ),
                    severity=Severity.MEDIUM,
                ))

            # Check if error container uses role="alert" or aria-live.
            # Only fire when at least one of the two was measured and
            # neither is True — both missing means "not measured".
            live_measured = ("has_role_alert" in error) or ("has_aria_live" in error)
            has_live_region = bool(
                error.get("has_role_alert") or error.get("has_aria_live"),
            )
            if live_measured and not has_live_region and has_text_description:
                findings.append(Finding(
                    id=_make_finding_id(),
                    element=selector,
                    issue=(
                        "Error message container lacks role=\"alert\" or "
                        "aria-live for dynamic announcement"
                    ),
                    impact=(
                        "Screen readers may not announce dynamically "
                        "appearing error messages."
                    ),
                    recommendation=(
                        "Add role=\"alert\" to the error message container, "
                        "or use aria-live=\"assertive\" for immediate "
                        "announcement."
                    ),
                    severity=Severity.MEDIUM,
                ))

            # Check if aria-invalid is set on the errored field
            has_aria_invalid = error.get("has_aria_invalid")
            if has_aria_invalid is False:
                findings.append(Finding(
                    id=_make_finding_id(),
                    element=selector,
                    issue="Field in error state does not have aria-invalid=\"true\"",
                    impact="Assistive technology cannot identify the field as invalid.",
                    recommendation="Set aria-invalid=\"true\" on the field when it has an error.",
                    severity=Severity.MEDIUM,
                ))

        conformance = self._determine_conformance(findings, len(capture_data.form_fields))
        confidence = 0.7
        return conformance, confidence, findings


class Check_3_3_2(BaseCheck):
    """SC 3.3.2 Labels or Instructions (Level A)."""

    criterion_id = "3.3.2"
    criterion_name = "Labels or Instructions"
    # A finding asserting a field's required state is checked against the
    # captured form_fields[].required boolean.
    measurement_sources = {"required": ("form_fields", "required")}
    level = "A"
    wcag_versions = ["2.0", "2.1", "2.2"]
    guideline = "3.3 Input Assistance"
    principle = "3. Understandable"
    ict_baseline = "10"
    tt_tests = ["10.F"]
    normative_text = (
        "Labels or instructions are provided when content requires "
        "user input."
    )
    off_scope_keywords = {
        "navigation_images": [
            "navigation links are presented as images",
            "navigation image", "nav image",
            "link is implemented as an image",
        ],
        "link_purpose": [
            "link text is an email", "link purpose",
            "link destination",
        ],
    }

    def is_applicable(self, capture_data: CaptureData) -> bool:
        return bool(capture_data.form_fields)

    async def run_programmatic(
        self, capture_data: CaptureData
    ) -> tuple[ConformanceLevel, float, list[Finding]]:
        findings: list[Finding] = []

        for field in capture_data.form_fields:
            # Skip hidden/collapsed form fields
            if field.get("visible") is False:
                continue
            rect = field.get("rect", {})
            if rect and (rect.get("width", 0) <= 0 or rect.get("height", 0) <= 0):
                continue

            selector = field.get("selector", "input")
            field_type = (field.get("type") or "").lower()
            tag = (field.get("tag") or field.get("tagName") or "").lower()
            label = (field.get("label") or "").strip()
            aria_label = (field.get("aria_label") or field.get("aria-label") or "").strip()
            aria_labelledby = field.get("aria_labelledby", field.get("aria-labelledby", ""))
            title = (field.get("title") or "").strip()
            placeholder = (field.get("placeholder") or "").strip()
            required = field.get("required", False)

            if field_type in ("hidden", "submit", "button", "reset", "image"):
                continue

            has_label = bool(label or aria_label or aria_labelledby or title)

            if not has_label:
                if placeholder:
                    findings.append(Finding(
                        id=_make_finding_id(),
                        element=selector,
                        issue=(
                            f"Form field uses only placeholder as label: "
                            f"\"{placeholder}\""
                        ),
                        impact=(
                            "Placeholder text disappears when user starts "
                            "typing, leaving no visible label."
                        ),
                        recommendation=(
                            "Add a persistent <label> element. Placeholder "
                            "alone is not a sufficient label."
                        ),
                        severity=Severity.MEDIUM,
                    ))
                else:
                    findings.append(Finding(
                        id=_make_finding_id(),
                        element=selector,
                        issue=f"Form field ({field_type or tag}) has no label or instructions",
                        impact="Users do not know what information to enter.",
                        recommendation="Add a <label> element or aria-label to the field.",
                        severity=Severity.HIGH,
                    ))

            # Check required field indication
            if required:
                required_indicated = field.get("required_indicated", False)
                aria_required = field.get("aria_required", field.get("aria-required", ""))
                if not required_indicated and not aria_required:
                    findings.append(Finding(
                        id=_make_finding_id(),
                        element=selector,
                        issue="Required field does not indicate required status",
                        impact="Users may skip the field and encounter errors.",
                        recommendation=(
                            "Add aria-required=\"true\" and/or visual required "
                            "indicator (asterisk with explanation)."
                        ),
                        severity=Severity.LOW,
                    ))

            # Check for format instructions when pattern is set
            pattern = field.get("pattern", "")
            if pattern and not field.get("pattern_description", ""):
                findings.append(Finding(
                    id=_make_finding_id(),
                    element=selector,
                    issue="Input has pattern validation but no format instructions",
                    impact="Users do not know the expected input format.",
                    recommendation=(
                        "Add instructions or aria-describedby explaining "
                        "the expected format."
                    ),
                    severity=Severity.MEDIUM,
                ))

        conformance = self._determine_conformance(findings, len(capture_data.form_fields))
        confidence = 0.8
        return conformance, confidence, findings


class Check_3_3_3(BaseCheck):
    """SC 3.3.3 Error Suggestion (Level AA)."""

    criterion_id = "3.3.3"
    criterion_name = "Error Suggestion"
    level = "AA"
    wcag_versions = ["2.0", "2.1", "2.2"]
    guideline = "3.3 Input Assistance"
    principle = "3. Understandable"
    ict_baseline = "20"
    tt_tests = ["20.B"]
    normative_text = (
        "If an input error is automatically detected and suggestions for "
        "correction are known, then the suggestions are provided to the "
        "user, unless it would jeopardize the security or purpose of "
        "the content."
    )

    def is_applicable(self, capture_data: CaptureData) -> bool:
        return bool(capture_data.form_fields or capture_data.form_errors)

    async def run_programmatic(
        self, capture_data: CaptureData
    ) -> tuple[ConformanceLevel, float, list[Finding]]:
        findings: list[Finding] = []

        for error in capture_data.form_errors:
            selector = error.get("selector", "form")
            has_suggestion = error.get("has_suggestion", False)
            error_text = error.get("text", "")
            field_type = error.get("field_type", "")

            # Password fields are exempt for security
            if field_type == "password":
                continue

            if not has_suggestion:
                findings.append(Finding(
                    id=_make_finding_id(),
                    element=selector,
                    issue=(
                        f"Error message does not provide correction suggestion: "
                        f"\"{error_text}\""
                    ),
                    impact="Users may not know how to fix the error.",
                    recommendation=(
                        "Include a specific suggestion in the error message "
                        "(e.g., 'Enter email in format: name@example.com')."
                    ),
                    severity=Severity.MEDIUM,
                ))

        # Check form fields with validation constraints
        for field in capture_data.form_fields:
            selector = field.get("selector", "input")
            field_type = (field.get("type") or "").lower()
            pattern = field.get("pattern", "")
            min_val = field.get("min", "")
            max_val = field.get("max", "")
            minlength = field.get("minlength", "")
            maxlength = field.get("maxlength", "")

            if field_type in ("hidden", "submit", "button", "reset"):
                continue

            constraints = []
            if pattern:
                constraints.append(f"pattern={pattern}")
            if min_val:
                constraints.append(f"min={min_val}")
            if max_val:
                constraints.append(f"max={max_val}")
            if minlength:
                constraints.append(f"minlength={minlength}")
            if maxlength:
                constraints.append(f"maxlength={maxlength}")

            if constraints and not field.get("has_error_suggestion_handler", False):
                help_text = field.get("help_text", "")
                if not help_text:
                    findings.append(Finding(
                        id=_make_finding_id(),
                        element=selector,
                        issue=(
                            f"Field has validation constraints ({', '.join(constraints)}) "
                            f"but no visible help text or error suggestion mechanism"
                        ),
                        impact="Users who make errors may not receive helpful suggestions.",
                        recommendation=(
                            "Add help text describing the expected input, and "
                            "provide specific suggestions in error messages."
                        ),
                        severity=Severity.LOW,
                    ))

        conformance = self._determine_conformance(findings, len(capture_data.form_errors))
        confidence = 0.6
        return conformance, confidence, findings


class Check_3_3_4(BaseCheck):
    """SC 3.3.4 Error Prevention (Legal, Financial, Data) (Level AA)."""

    criterion_id = "3.3.4"
    criterion_name = "Error Prevention (Legal, Financial, Data)"
    # Applicability (does the page cause a legal/financial/data
    # transaction?) is a meaning judgment — the judge decides it from the
    # transaction-scope evidence block and the screenshots.
    ai_judged_applicability = True
    level = "AA"
    wcag_versions = ["2.0", "2.1", "2.2"]
    guideline = "3.3 Input Assistance"
    principle = "3. Understandable"
    ict_baseline = ""
    tt_tests = []
    normative_text = (
        "For Web pages that cause legal commitments or financial "
        "transactions for the user to occur, that modify or delete "
        "user-controllable data in data storage systems, or that submit "
        "user test responses, at least one of the following is true: "
        "Reversible, Checked, Confirmed."
    )

    def is_applicable(self, capture_data: CaptureData) -> bool:
        html_lower = (capture_data.html or "").lower()
        # Require strong financial/legal/data-deletion signals, not just
        # generic words like "submit" or "order" that appear on most forms
        financial_signals = (
            "checkout" in html_lower
            or "payment" in html_lower
            or "purchase" in html_lower
            or "credit card" in html_lower
            or "billing" in html_lower
        )
        legal_signals = (
            "terms of service" in html_lower
            or "terms and conditions" in html_lower
            or "agreement" in html_lower
            or "legal" in html_lower
            or "contract" in html_lower
        )
        data_deletion_signals = (
            "delete account" in html_lower
            or "deactivate account" in html_lower
            or "remove account" in html_lower
            or "cancel subscription" in html_lower
        )
        return bool(financial_signals or legal_signals or data_deletion_signals)

    async def run_programmatic(
        self, capture_data: CaptureData
    ) -> tuple[ConformanceLevel, float, list[Finding]]:
        findings: list[Finding] = []
        html_lower = (capture_data.html or "").lower()

        # Detect financial/legal form patterns -- use the same strict
        # signals as is_applicable to avoid false positives on regular
        # links like "Read More" or generic "submit" buttons.
        is_financial = bool(
            re.search(r"(?:payment|checkout|purchase|credit.card|billing)", html_lower)
        )
        is_legal = bool(
            re.search(r"(?:terms of service|terms and conditions|legal agreement|contract)", html_lower)
        )
        has_delete = bool(
            re.search(r"(?:delete account|deactivate account|remove account|cancel subscription)", html_lower)
        )

        if is_financial or is_legal or has_delete:
            # Check for confirmation mechanism
            has_confirmation = bool(
                re.search(r"(?:confirm|review|verify|are.you.sure)", html_lower)
            )
            has_review = bool(
                re.search(r"(?:review.order|review.details|summary|preview)", html_lower)
            )
            has_checkbox_confirm = bool(
                re.search(r"(?:i.agree|i.accept|i.confirm|i.understand)", html_lower)
            )
            has_undo = bool(
                re.search(r"(?:undo|revert|cancel|go.back)", html_lower)
            )

            if not has_confirmation and not has_review and not has_checkbox_confirm and not has_undo:
                context = "financial" if is_financial else "legal" if is_legal else "data deletion"
                findings.append(Finding(
                    id=_make_finding_id(),
                    element="form",
                    issue=(
                        f"Page contains {context} action without apparent "
                        f"confirmation, review, or undo mechanism"
                    ),
                    impact=(
                        "Users may accidentally commit to legal/financial "
                        "obligations or delete data with no recourse."
                    ),
                    recommendation=(
                        "Add a review/confirmation step, checkbox confirmation, "
                        "or undo mechanism before finalizing the action."
                    ),
                    severity=Severity.MEDIUM,
                ))

        conformance = self._determine_conformance(findings)
        confidence = 0.4  # Detection is heuristic
        return conformance, confidence, findings


def get_checks() -> list[BaseCheck]:
    return [
        Check_3_3_1(),
        Check_3_3_2(),
        Check_3_3_3(),
        Check_3_3_4(),
    ]
