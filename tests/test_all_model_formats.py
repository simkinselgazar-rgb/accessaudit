"""Parser coverage tests for every LLM in the fleet.

Each test here uses the exact response shape that was captured by manually
probing the model with a common tool schema. If a test starts failing, a
parser regression would silently drop findings from that model's output
in production.

Models covered:
- Qwen3.5-35B-A3B-4bit         (text, via vLLM at port 8001)
- Qwen3-VL-32B-Instruct-4bit   (vision, via vLLM at port 8002)
- gemma-4-e4b-it-4bit          (explorer, via vLLM at port 8004)
- gemma-4-26b-a4b-it-4bit      (local judge, via vLLM at port 8005)
- gemini-2.5-flash-lite        (video, via OpenAI-compat endpoint)

Run with:  python tests/test_all_model_formats.py
"""
from __future__ import annotations

import json
import sys
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from functions.parser import (  # noqa: E402
    describe_response_shape,
    parse_tool_response,
)

EXPECTED_SELECTOR = 'button[data-bs-target="#modal"]'


# ═══════════════════════════════════════════════════════════════════════
# Real response shapes captured by manually probing each model with the
# same tool schema. Any change to these responses must come from a new
# probe, not hand editing.
# ═══════════════════════════════════════════════════════════════════════

QWEN_35B_TEXT = {
    "choices": [{
        "message": {
            "role": "assistant",
            "content": (
                "The user wants me to evaluate a fake HTML element and call "
                "the report_probe tool with specific parameters..."
            ),
            "tool_calls": [{
                "id": "call_abc",
                "function": {
                    "name": "report_probe",
                    # Qwen wraps arguments as a JSON STRING and also returns
                    # numbers AS STRINGS: confidence="0.9" not 0.9
                    "arguments": (
                        '{"verdict": "fail", "confidence": "0.9", '
                        '"css_selector": "button[data-bs-target=\\"#modal\\"]", '
                        '"issue": "The button has no accessible name"}'
                    ),
                },
            }],
        }
    }]
}


QWEN_VL_32B_VISION = {
    "choices": [{
        "message": {
            "role": "assistant",
            # Qwen3-VL embeds the tool call twice: once in tool_calls AND
            # once inline in content as <tool_call>{json}</tool_call>.
            "content": (
                "<tool_call>\n"
                '{"name": "report_probe", "arguments": '
                '{"verdict": "fail", "confidence": 0.9, '
                '"css_selector": "button[data-bs-target=\\"#modal\\"]", '
                '"issue": "The button has no accessible name."}}\n'
                "</tool_call>"
            ),
            "tool_calls": [{
                "id": "call_xyz",
                "function": {
                    "name": "report_probe",
                    "arguments": (
                        '{"verdict": "fail", "confidence": 0.9, '
                        '"css_selector": "button[data-bs-target=\\"#modal\\"]", '
                        '"issue": "The button has no accessible name."}'
                    ),
                },
            }],
        }
    }]
}


GEMMA_4_E4B_EXPLORER = {
    "choices": [{
        "message": {
            "role": "assistant",
            # Gemma native format: <|tool_call>call:name{...}<tool_call|>.
            # Keys unquoted, numbers bare, string values wrapped in <|"|>.
            # CSS selector contains literal " characters inside the wrap.
            "content": (
                '<|tool_call>call:report_probe{'
                'confidence:0.9,'
                'css_selector:<|"|>button[data-bs-target="#modal"]<|"|>,'
                'issue:<|"|>The button has no accessible name.<|"|>,'
                'verdict:<|"|>fail<|"|>'
                '}<tool_call|>'
            ),
        }
    }]
}


GEMMA_4_26B_JUDGE = {
    # Gemma 26B emits the exact same shape as Gemma 4 E4B.
    "choices": [{
        "message": {
            "role": "assistant",
            "content": (
                '<|tool_call>call:report_probe{'
                'confidence:0.9,'
                'css_selector:<|"|>button[data-bs-target="#modal"]<|"|>,'
                'issue:<|"|>The button has no accessible name.<|"|>,'
                'verdict:<|"|>fail<|"|>'
                '}<tool_call|>'
            ),
        }
    }]
}


GEMINI_2_5_FLASH_LITE = {
    "choices": [{
        "message": {
            "role": "assistant",
            "tool_calls": [{
                "id": "function-call-123",
                "function": {
                    "name": "report_probe",
                    # Gemini OpenAI-compat returns clean JSON with proper escaping
                    "arguments": (
                        '{"verdict":"fail","confidence":0.9,'
                        '"css_selector":"button[data-bs-target=\\"#modal\\"]",'
                        '"issue":"The button has no accessible name."}'
                    ),
                },
            }],
        }
    }]
}


# ═══════════════════════════════════════════════════════════════════════
# Tests -- one per model, plus cross-cutting tests for specific bugs.
# ═══════════════════════════════════════════════════════════════════════

def _assert_probe_payload(parsed: dict, *, model_label: str) -> None:
    assert parsed is not None, f"{model_label}: parse_tool_response returned None"
    assert parsed["verdict"] == "fail", f"{model_label}: verdict mismatch"
    assert parsed["confidence"] == 0.9, (
        f"{model_label}: confidence is {parsed['confidence']!r} "
        f"({type(parsed['confidence']).__name__}), expected 0.9 (float)"
    )
    assert parsed["css_selector"] == EXPECTED_SELECTOR, (
        f"{model_label}: css_selector mismatch: {parsed['css_selector']!r}"
    )
    assert "button" in parsed["issue"].lower()


def test_qwen_35b_text_tool_call():
    assert describe_response_shape(QWEN_35B_TEXT) == "openai_tool_calls"
    parsed = parse_tool_response(QWEN_35B_TEXT, "report_probe")
    _assert_probe_payload(parsed, model_label="Qwen3.5-35B-text")


def test_qwen_35b_returns_number_as_string_is_coerced():
    """Qwen 35B returns ``"confidence": "0.9"`` -- parser must coerce to float."""
    parsed = parse_tool_response(QWEN_35B_TEXT, "report_probe")
    assert isinstance(parsed["confidence"], float)
    assert parsed["confidence"] == 0.9


def test_qwen_vl_32b_vision_tool_call():
    assert describe_response_shape(QWEN_VL_32B_VISION) == "openai_tool_calls"
    parsed = parse_tool_response(QWEN_VL_32B_VISION, "report_probe")
    _assert_probe_payload(parsed, model_label="Qwen3-VL-32B-vision")


def test_gemma_4_e4b_explorer_native():
    assert describe_response_shape(GEMMA_4_E4B_EXPLORER) == "gemma_native"
    parsed = parse_tool_response(GEMMA_4_E4B_EXPLORER, "report_probe")
    _assert_probe_payload(parsed, model_label="Gemma-4-E4B-explorer")


def test_gemma_4_26b_judge_native():
    assert describe_response_shape(GEMMA_4_26B_JUDGE) == "gemma_native"
    parsed = parse_tool_response(GEMMA_4_26B_JUDGE, "report_probe")
    _assert_probe_payload(parsed, model_label="Gemma-4-26B-judge")


def test_gemini_flash_lite_tool_call():
    assert describe_response_shape(GEMINI_2_5_FLASH_LITE) == "openai_tool_calls"
    parsed = parse_tool_response(GEMINI_2_5_FLASH_LITE, "report_probe")
    _assert_probe_payload(parsed, model_label="Gemini-2.5-flash-lite")


def test_qwen_double_encoded_inner_array_is_auto_decoded():
    """Qwen restructure sometimes returns nested arrays as JSON strings.

    Observed shape: ``{"elements": "[{\\"type\\": \\"button\\"}]"}``
    The parser must decode that inner string back into a real array
    so downstream consumers don't have to care.
    """
    raw = {
        "choices": [{
            "message": {
                "tool_calls": [{
                    "function": {
                        "name": "report_element_inventory",
                        "arguments": (
                            '{"elements": "[{\\"type\\": \\"button\\", '
                            '\\"selector\\": \\"#go\\"}]", '
                            '"remove_selectors": []}'
                        ),
                    },
                }],
            }
        }]
    }
    parsed = parse_tool_response(raw, "report_element_inventory")
    assert parsed is not None
    assert isinstance(parsed["elements"], list), (
        "Qwen double-encoded elements string must be decoded back to a list"
    )
    assert len(parsed["elements"]) == 1
    assert parsed["elements"][0]["type"] == "button"
    assert parsed["elements"][0]["selector"] == "#go"


def test_gemma_native_with_inner_literal_quotes_in_css_selector():
    """The bug I found on run 1: CSS selector with literal " inside a Gemma
    <|"|>-wrapped string was being corrupted by naive cleaning."""
    raw = {
        "choices": [{
            "message": {
                "content": (
                    '<|tool_call>call:report_element_inventory{'
                    'priority_updates:[{'
                    'selector:<|"|>button[data-bs-target="#mainNavSearchCollapse"]<|"|>,'
                    'exploration_priority:<|"|>high<|"|>'
                    '}],'
                    'elements:[],'
                    'remove_selectors:[]'
                    '}<tool_call|>'
                ),
            }
        }]
    }
    parsed = parse_tool_response(raw, "report_element_inventory")
    assert parsed is not None
    assert len(parsed["priority_updates"]) == 1
    pu = parsed["priority_updates"][0]
    assert pu["selector"] == 'button[data-bs-target="#mainNavSearchCollapse"]'
    assert pu["exploration_priority"] == "high"


def test_qwen_native_function_parameter_format():
    """Qwen sometimes emits the old <function=name><parameter=key>value</parameter></function> style."""
    raw = {
        "choices": [{
            "message": {
                "content": (
                    "<function=report_probe>"
                    "<parameter=verdict>fail</parameter>"
                    "<parameter=confidence>0.9</parameter>"
                    "<parameter=css_selector>button[data-bs-target=\"#modal\"]</parameter>"
                    "<parameter=issue>The button has no accessible name</parameter>"
                    "</function>"
                ),
            }
        }]
    }
    parsed = parse_tool_response(raw, "report_probe")
    assert parsed is not None, "Qwen native parameter format not parsed"
    assert parsed["verdict"] == "fail"
    # Qwen native format puts parameters as strings; coercion should turn 0.9 into a float
    assert parsed["confidence"] == 0.9, f"got {parsed['confidence']!r}"
    assert parsed["css_selector"] == EXPECTED_SELECTOR


def test_truncated_gemma_tool_call_recovers_partial_data():
    """Gemma sometimes runs out of tokens mid-tool-call. We should still
    recover whatever fields did make it."""
    raw = {
        "choices": [{
            "message": {
                "content": (
                    '<|tool_call>call:report_probe{'
                    'verdict:<|"|>fail<|"|>,'
                    'confidence:0.9,'
                    'css_selector:<|"|>button[data-bs-target="#modal"]<|"|>,'
                    'issue:<|"|>The button has no'  # truncated mid-string
                ),
            }
        }]
    }
    parsed = parse_tool_response(raw, "report_probe")
    # We may or may not recover the issue field, but the earlier fields
    # must come through. The key assertion: no exception, result is
    # either a dict with the recovered fields or None -- never crashes.
    if parsed is not None:
        assert parsed.get("verdict") == "fail"
        assert parsed.get("confidence") == 0.9


def test_empty_content_returns_none():
    raw = {"choices": [{"message": {"role": "assistant", "content": ""}}]}
    assert parse_tool_response(raw, "report_probe") is None


def test_empty_choices_returns_none():
    raw = {"choices": []}
    assert parse_tool_response(raw, "report_probe") is None


def test_qwen_invents_wrong_tool_name_with_empty_args_is_rejected():
    """Real Qwen 35B restructure failure observed on a university-site run 1 main section.

    Qwen returned a tool_calls entry with ``function.name="link"`` (made
    up, not in our schema) and ``arguments="{}"`` (empty). The actual
    analysis lived in the ``content`` field as 13k chars of prose.

    The parser must NOT treat the empty-args tool_calls entry as a parse
    success. Accepting ``{}`` silently drops the real data.
    """
    raw = {
        "choices": [{
            "message": {
                "content": (
                    "The user wants me to convert a prose analysis into a "
                    "structured tool call. I need to extract all the elements "
                    "from the provided HTML."
                ),
                "tool_calls": [{
                    "id": "call_bad",
                    "function": {"name": "link", "arguments": "{}"},
                }],
            }
        }]
    }
    parsed = parse_tool_response(raw, "report_element_inventory")
    assert parsed is None, (
        f"Empty-args Qwen hallucination was accepted: {parsed!r}"
    )


def test_wrong_tool_name_with_substantive_args_is_accepted():
    """Distinct from the above: if the name is wrong but the args contain
    real schema-shaped data, accept it. The model clearly understood the
    task, it just picked the wrong function label."""
    raw = {
        "choices": [{
            "message": {
                "tool_calls": [{
                    "function": {
                        "name": "report_probe_mistyped",
                        "arguments": (
                            '{"verdict":"fail","confidence":0.9,'
                            '"css_selector":"#go","issue":"missing name"}'
                        ),
                    },
                }],
            }
        }]
    }
    parsed = parse_tool_response(raw, "report_probe")
    assert parsed is not None
    assert parsed["verdict"] == "fail"
    assert parsed["css_selector"] == "#go"


def test_empty_dict_from_correct_name_is_also_rejected():
    """Even with the right function name, an empty args dict is not data."""
    raw = {
        "choices": [{
            "message": {
                "tool_calls": [{
                    "function": {
                        "name": "report_probe",
                        "arguments": "{}",
                    },
                }],
            }
        }]
    }
    assert parse_tool_response(raw, "report_probe") is None


def test_plain_prose_returns_none():
    """Pure prose with no tool call shape must return None, not fabricate data."""
    raw = {
        "choices": [{
            "message": {
                "role": "assistant",
                "content": "I think the button is missing accessibility attributes.",
            }
        }]
    }
    assert parse_tool_response(raw, "report_probe") is None


# ═══════════════════════════════════════════════════════════════════════
# Test runner (no pytest required)
# ═══════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    passed = failed = 0
    for t in tests:
        try:
            t()
            print(f"PASS  {t.__name__}")
            passed += 1
        except Exception as exc:
            print(f"FAIL  {t.__name__}: {type(exc).__name__}: {exc}")
            traceback.print_exc()
            failed += 1
    print(f"\n{passed} passed, {failed} failed")
    sys.exit(0 if failed == 0 else 1)
