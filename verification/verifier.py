"""Optional second-pass AI verification of test results.

Every AI call routes through ``functions.llm.LLMClient``.
"""
from __future__ import annotations

import logging

from config import VERIFICATION_ENABLED
from functions.llm import LLMClient
from models import ConformanceLevel, TestResult

logger = logging.getLogger(__name__)


_CONFORMANCE_SEVERITY = {
    ConformanceLevel.DOES_NOT_SUPPORT: 0,
    ConformanceLevel.PARTIALLY_SUPPORTS: 1,
    ConformanceLevel.SUPPORTS: 2,
    ConformanceLevel.NOT_APPLICABLE: 3,
    ConformanceLevel.NOT_EVALUATED: 4,
}


_VERIFICATION_TOOL = {
    "type": "function",
    "function": {
        "name": "report_verification",
        "description": (
            "Report the result of independently verifying a WCAG accessibility "
            "assessment produced by another auditor."
        ),
        "parameters": {
            "type": "object",
            "required": ["agrees", "conformance_level", "confidence_delta", "reasoning"],
            "properties": {
                "agrees": {
                    "type": "boolean",
                    "description": (
                        "True if you agree with the original auditor's conformance "
                        "level AND their specific findings. False if you disagree "
                        "with either the verdict or any finding."
                    ),
                },
                "conformance_level": {
                    "type": "string",
                    "enum": [
                        "Supports",
                        "Partially Supports",
                        "Does Not Support",
                        "Not Applicable",
                        "Not Evaluated",
                    ],
                    "description": (
                        "Your INDEPENDENT conformance assessment. This may match "
                        "the original or differ from it."
                    ),
                },
                "confidence_delta": {
                    "type": "number",
                    "minimum": 0.0,
                    "maximum": 0.2,
                    "description": (
                        "How much additional confidence your agreement provides "
                        "(0.0 = no boost, 0.2 = maximum boost when fully agreeing)."
                    ),
                },
                "reasoning": {
                    "type": "string",
                    "description": (
                        "Verification rationale. Explicitly address: (a) which "
                        "findings you verified as correct, (b) any findings you "
                        "reject and why, (c) any missing findings the original "
                        "auditor should have caught, (d) whether the evidence "
                        "supports the stated conformance level."
                    ),
                },
            },
        },
    },
}


def build_verification_prompt(
    result: TestResult, capture_data=None
) -> tuple[str, str, list[str]]:
    """Build system/user prompts and image list for verification."""
    system_prompt = (
        "You are an independent WCAG accessibility review auditor performing "
        "a second-pass verification of another auditor's work. Your job is to "
        "review their assessment and either confirm it or challenge it with "
        "specific evidence.\n\n"
        "VERIFICATION CRITERIA -- explicitly check each of these:\n"
        "1. FINDING ACCURACY: Does each finding correctly describe a real "
        "   issue visible in the attached screenshots or the source data?\n"
        "2. CRITERION RELEVANCE: Are all findings actually about THIS success "
        "   criterion, or do some belong to a different criterion?\n"
        "3. CONFORMANCE LOGIC: Does the conformance level follow from the "
        "   findings? (e.g. one 'high' severity finding -> Does Not Support)\n"
        "4. COMPLETENESS: Are there obvious issues visible in the screenshots "
        "   that the original auditor missed?\n"
        "5. SEVERITY CALIBRATION: Is each finding's severity proportionate "
        "   to its actual user impact?\n\n"
        "Call the report_verification tool with your evaluation. Set agrees=true "
        "only if ALL five criteria check out. Otherwise set agrees=false and "
        "explain specifically what the original auditor got wrong."
    )

    findings_summary = ""
    for f in result.findings:
        sev = f.severity.value if hasattr(f.severity, "value") else f.severity
        findings_summary += f"- [{sev}] {f.element}: {f.issue}\n"

    conf_val = (
        result.conformance_level.value
        if hasattr(result.conformance_level, "value")
        else result.conformance_level
    )

    user_prompt = (
        f"## Criterion: {result.criterion_id} - {result.criterion_name} (Level {result.level})\n\n"
        f"## Original Assessment\n"
        f"- Conformance Level: {conf_val}\n"
        f"- Confidence: {result.confidence:.1%}\n"
        f"- Reasoning: {result.confidence_reasoning}\n\n"
        f"## Findings ({len(result.findings)} total)\n{findings_summary}\n"
        f"## Summary\n{result.summary}\n\n"
        "Review this assessment against the verification criteria in the "
        "system prompt, then call the report_verification tool."
    )

    image_paths: list[str] = []
    if capture_data:
        if getattr(capture_data, "full_page_path", ""):
            image_paths.append(capture_data.full_page_path)
        if getattr(capture_data, "viewport_path", ""):
            image_paths.append(capture_data.viewport_path)

    return system_prompt, user_prompt, image_paths


async def verify_result(result: TestResult, ai_client, capture_data=None) -> TestResult:
    """Run a second-pass verification on a test result.

    Args:
        result: The test result to verify.
        ai_client: Ignored (kept for backward compatibility). LLMClient is
            instantiated internally so verification routes through the unified
            AI client.
        capture_data: Optional capture data for attaching screenshots.
    """
    if not VERIFICATION_ENABLED:
        result.verification_status = "not_verified"
        return result

    try:
        system_prompt, user_prompt, image_paths = build_verification_prompt(result, capture_data)

        llm = LLMClient()
        response = await llm.call_with_tools(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            tool_name="report_verification",
            tool_schema=_VERIFICATION_TOOL,
            images=image_paths or None,
            temperature=0.2,
        )

        if not response:
            result.verification_status = "verification_failed"
            return result

        agrees = bool(response.get("agrees", True))

        if agrees:
            result.verified = True
            result.verification_status = "verified"
            delta = max(0.0, min(float(response.get("confidence_delta", 0.1)), 0.2))
            result.confidence = min(result.confidence + delta, 1.0)
        else:
            result.verified = False
            result.verification_status = "disputed"

            verifier_level_str = response.get("conformance_level", "")
            if verifier_level_str:
                try:
                    verifier_level = ConformanceLevel(verifier_level_str)
                    orig_sev = _CONFORMANCE_SEVERITY.get(result.conformance_level, 4)
                    ver_sev = _CONFORMANCE_SEVERITY.get(verifier_level, 4)
                    if ver_sev < orig_sev:
                        result.conformance_level = verifier_level
                except ValueError:
                    pass

            result.confidence = min(result.confidence, 0.6)

        logger.info(
            "Verification %s: %s",
            result.criterion_id,
            "agreed" if agrees else "disputed",
        )

    except Exception as exc:
        logger.error("Verification failed for %s: %s", result.criterion_id, exc)
        result.verification_status = "verification_error"

    return result
