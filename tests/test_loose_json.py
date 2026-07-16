"""Regression tests for ``functions.parser.loose_json_loads``.

These tests pin the exact malformed-output patterns we have observed from
local models in production. If any test starts failing, the parser
regressed and findings will silently disappear from result.json files.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

# Allow running this file directly with `python tests/test_loose_json.py`.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

try:
    import pytest
except ImportError:  # Tests must run without pytest installed.
    class _PytestStub:
        @staticmethod
        def raises(exc_type):
            class _Ctx:
                def __enter__(self_inner):
                    return self_inner
                def __exit__(self_inner, et, ev, tb):
                    if et is None:
                        raise AssertionError(f"expected {exc_type.__name__} but no exception was raised")
                    return issubclass(et, exc_type)
            return _Ctx()
    pytest = _PytestStub()  # type: ignore

from functions.parser import (  # noqa: E402
    loose_json_loads,
    parse_ai_response,
    parse_native_tool_call,
)


def test_strict_json_passthrough():
    assert loose_json_loads('{"a": 1, "b": "hi"}') == {"a": 1, "b": "hi"}


def test_unquoted_keys():
    assert loose_json_loads("{a:1,b:2}") == {"a": 1, "b": 2}


def test_unquoted_keys_with_string_values():
    out = loose_json_loads('{conformance_level:"Supports",confidence:0.9}')
    assert out == {"conformance_level": "Supports", "confidence": 0.9}


def test_string_value_contains_colon_does_not_become_a_key():
    """CSS selectors like ``div:nth-of-type(1)`` must stay inside the string."""
    out = loose_json_loads(
        '{element:"img > div:nth-of-type(1) > img",severity:"high"}'
    )
    assert out == {
        "element": "img > div:nth-of-type(1) > img",
        "severity": "high",
    }


def test_inner_empty_quotes_in_string_value():
    """``(alt="")`` -- an empty string literal inside a string value."""
    out = loose_json_loads(
        '{issue:"image marked as decorative (alt=\"\") but conveys content"}'
    )
    assert "decorative" in out["issue"]
    assert "alt=" in out["issue"]


def test_gemma_native_tool_call_with_inner_empty_quotes():
    """Real Gemma 26B output observed on a university run SC 1.1.1 visual AI call.

    The model wraps every string in ``<|"|>`` tokens. The cleaner strips
    those to ``"``, leaving ``(alt="")`` inside a longer string value.
    Without the state-aware repair, ``json.loads`` chokes on the empty
    ``""`` and ``parse_native_tool_call`` returns None, so the entire
    finding is silently dropped.
    """
    raw_content = (
        '<|tool_call>call:report_wcag_assessment{'
        'confidence:1,'
        'confidence_reasoning:<|"|>The hero image is marked decorative (alt=<|"|><|"|>) but conveys content.<|"|>,'
        'conformance_level:<|"|>Does Not Support<|"|>,'
        'findings:[{'
        'element:<|"|>img#hero > div:nth-of-type(1) > img (hero image at top of page)<|"|>,'
        'issue:<|"|>The hero image is marked decorative (alt=<|"|><|"|>) but conveys meaningful visual context.<|"|>,'
        'impact:<|"|>Screen reader users (JAWS, NVDA, VoiceOver) receive no description of this image.<|"|>,'
        'recommendation:<|"|>WCAG 1.1.1 requires meaningful images to have a text alternative.<|"|>,'
        'severity:<|"|>high<|"|>'
        '}],'
        'summary:<|"|>The page does not support 1.1.1: a meaningful hero image is marked decorative.<|"|>'
        '}<tool_call|>'
    )

    parsed = parse_native_tool_call(raw_content)
    assert parsed is not None, "parser dropped a recoverable Gemma tool call"
    assert parsed["function"] == "report_wcag_assessment"
    args = parsed["arguments"]
    assert args["conformance_level"] == "Does Not Support"
    assert args["confidence"] == 1
    assert len(args["findings"]) == 1
    f = args["findings"][0]
    assert "hero image" in f["element"].lower()
    assert "div:nth-of-type(1)" in f["element"]
    assert f["severity"] == "high"
    assert "JAWS" in f["impact"]


def test_full_pipeline_recovery_of_dropped_finding():
    """End-to-end: parse_ai_response must recover the finding too."""
    raw_content = (
        '<|tool_call>call:report_wcag_assessment{'
        'confidence:0.9,'
        'confidence_reasoning:<|"|>image with alt=<|"|><|"|> conveys content<|"|>,'
        'conformance_level:<|"|>Partially Supports<|"|>,'
        'findings:[{'
        'element:<|"|>#hero > img<|"|>,'
        'issue:<|"|>Decorative marking (alt=<|"|><|"|>) on a meaningful image<|"|>,'
        'impact:<|"|>Blind users miss visual context<|"|>,'
        'recommendation:<|"|>1.1.1 requires text alternatives<|"|>,'
        'severity:<|"|>medium<|"|>'
        '}],'
        'summary:<|"|>Hero image needs alt text<|"|>'
        '}<tool_call|>'
    )
    fake_response = {"choices": [{"message": {"content": raw_content}}]}
    result = parse_ai_response(fake_response)
    assert result["confidence"] == 0.9
    assert len(result["findings"]) == 1
    assert "hero" in result["findings"][0].element.lower()


def test_string_with_apostrophe_phrase():
    """Don't be fooled by single-quoted phrases inside string values."""
    out = loose_json_loads(
        '{summary:"The page heading \'FARTHER THAN EVER\' is meaningful"}'
    )
    assert "FARTHER THAN EVER" in out["summary"]


def test_gemma_native_with_inner_literal_quotes_in_value():
    """Gemma's <|"|> wrapping must be distinguishable from literal " inside
    the value content.

    Real failure observed on a university run 1 nav-section inventory audit: Gemma
    returned a CSS attribute selector ``button[data-bs-target="#navSearchCollapse"]``
    inside a <|"|>...<|"|> string. Naive replacement of <|"|> with bare "
    destroys the distinction and corrupts the JSON. The pair-aware splitter
    in ``_convert_gemma_quote_pairs`` preserves it.
    """
    raw = (
        '<|tool_call>call:report_element_inventory{'
        'priority_updates:[{'
        'selector:<|"|>button[data-bs-target="#navSearchCollapse"]<|"|>,'
        'exploration_priority:<|"|>high<|"|>'
        '}],'
        'elements:[],'
        'remove_selectors:[]'
        '}<tool_call|>'
    )
    parsed = parse_native_tool_call(raw)
    assert parsed is not None, "pair-aware cleaner failed"
    args = parsed["arguments"]
    assert args["elements"] == []
    assert args["remove_selectors"] == []
    assert len(args["priority_updates"]) == 1
    pu = args["priority_updates"][0]
    # The CSS selector must be preserved verbatim including the inner quotes
    assert pu["selector"] == 'button[data-bs-target="#navSearchCollapse"]'
    assert pu["exploration_priority"] == "high"


def test_gemma_data_attribute_selectors_round_trip():
    """Multiple CSS attribute selectors with literal quotes in one payload."""
    raw = (
        '{elements:[{'
        'selector:<|"|>button[data-meom-nav="sub-toggle"]<|"|>,'
        'tag:<|"|>button<|"|>,'
        'aria:{aria-controls:<|"|>menu<|"|>,aria-expanded:<|"|>false<|"|>}'
        '}]}'
    )
    obj = loose_json_loads(__import__("functions").parser.clean_tool_call_args(raw))
    assert obj["elements"][0]["selector"] == 'button[data-meom-nav="sub-toggle"]'
    assert obj["elements"][0]["aria"]["aria-controls"] == "menu"


def test_returns_decode_error_on_garbage():
    with pytest.raises(json.JSONDecodeError):
        loose_json_loads("this is not json at all { mismatched ]")


if __name__ == "__main__":
    import traceback
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
