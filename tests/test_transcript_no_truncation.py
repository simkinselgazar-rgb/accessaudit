"""Regression tests pinning the no-truncation rule for LLM transcripts.

CLAUDE.md is explicit: ``<review>/llm_transcripts/`` files must contain
the full request payload, full base64 media, full response, full error
body. Any silent truncation here would defeat the entire forensic
purpose of the transcripts (you can't debug a wrong audit if you can't
see what was actually sent / received).

These tests pin the contract by writing genuinely large payloads (multi-
MB system prompts, 10-image base64 batches, multi-KB response bodies)
through the public ``_save_llm_exchange`` API and then byte-comparing
the round-trip. If anything regressed (someone added a `[:N]` slice
to the writer, the summary sidecar, or the JSON dump), these tests will
fail loudly.

Run with:

    python tests/test_transcript_no_truncation.py
"""
from __future__ import annotations

import base64
import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from functions.llm import (  # noqa: E402
    _extract_text_summary,
    _save_llm_exchange,
)


def _huge_text(n_chars: int) -> str:
    """Generate a deterministic large string with non-ASCII characters
    so we also exercise the ``ensure_ascii=False`` path and prove that
    Unicode round-trips byte-for-byte (CLAUDE.md transcripts must
    preserve full multilingual prompts, not escape them).
    """
    # Mix ASCII + multi-byte so a UTF-16/cp1252 mistake would corrupt.
    block = "Spec — 中文 — العربية — emoji 🚀 — control: end of line\n"
    repeats = (n_chars // len(block)) + 1
    return (block * repeats)[:n_chars]


def _fake_base64_image(n_bytes: int) -> str:
    """Build a data URI with a base64 payload of the requested raw byte
    size. The b64 expansion is ~4/3, so the resulting URI string is a
    bit longer than n_bytes.
    """
    raw = bytes(((i * 31) & 0xFF) for i in range(n_bytes))
    b64 = base64.b64encode(raw).decode("ascii")
    return f"data:image/jpeg;base64,{b64}"


def _save_and_load(payload, response, env_dir, label="probe"):
    """Helper: save through the public writer and read the JSON back."""
    os.environ["WCAG_LLM_TRANSCRIPT_DIR"] = str(env_dir)
    try:
        path_str = _save_llm_exchange(
            request_payload=payload,
            raw_response=response,
            target_url="https://example.test/v1",
            label=label,
        )
    finally:
        os.environ.pop("WCAG_LLM_TRANSCRIPT_DIR", None)
    assert path_str, "writer returned None -- transcript NOT saved"
    p = Path(path_str)
    assert p.exists(), f"transcript file missing: {p}"
    return json.loads(p.read_text(encoding="utf-8"))


# ── Full system + user text round-trip ─────────────────────────────────


def test_huge_system_prompt_preserved_byte_for_byte():
    """A 200K-char system prompt must round-trip identically. The
    judge regularly produces 13K+ char system prompts and the dom
    context blocks add hundreds of K -- truncating at the writer
    would silently corrupt every saved transcript on the system.
    """
    sys_prompt = _huge_text(200_000)
    user_prompt = _huge_text(100_000)
    payload = {
        "model": "test/model",
        "messages": [
            {"role": "system", "content": sys_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "temperature": 0.2,
    }
    response = {
        "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
    }
    with tempfile.TemporaryDirectory() as td:
        record = _save_and_load(payload, response, td)
    assert record["request"]["messages"][0]["content"] == sys_prompt
    assert record["request"]["messages"][1]["content"] == user_prompt
    # Length-equal AND byte-equal -- catches silent UTF normalisation.
    assert len(record["request"]["messages"][0]["content"]) == len(sys_prompt)
    assert len(record["request"]["messages"][1]["content"]) == len(user_prompt)


def test_user_prompt_with_full_base64_images_preserved():
    """The user message in vision calls is a list of content parts:
    image_url entries with base64 data URIs and a final text part.
    Every image URL string must round-trip in full -- a `[:N]` slice
    here would silently corrupt every multimodal transcript on disk.
    """
    images = [_fake_base64_image(180_000) for _ in range(8)]
    user_text = _huge_text(50_000)
    parts = [{"type": "image_url", "image_url": {"url": u}} for u in images]
    parts.append({"type": "text", "text": user_text})
    payload = {
        "model": "test/vision",
        "messages": [
            {"role": "system", "content": "judge"},
            {"role": "user", "content": parts},
        ],
    }
    with tempfile.TemporaryDirectory() as td:
        record = _save_and_load(payload, {"choices": []}, td)
    saved_parts = record["request"]["messages"][1]["content"]
    assert len(saved_parts) == len(images) + 1
    for orig_url, saved_part in zip(images, saved_parts[:-1]):
        assert saved_part["type"] == "image_url"
        assert saved_part["image_url"]["url"] == orig_url, (
            "base64 image URL was modified between input and saved transcript"
        )
        assert len(saved_part["image_url"]["url"]) == len(orig_url)
    assert saved_parts[-1]["text"] == user_text


def test_response_body_preserved_in_full():
    """The model's raw response must round-trip in full, including a
    huge tool-call arguments string. Truncating here would lose the
    finding payload that downstream parsers depend on.
    """
    huge_args = json.dumps({"findings": [
        {"element": _huge_text(2_000), "issue": _huge_text(2_000)}
        for _ in range(50)
    ]})
    response = {
        "choices": [{
            "message": {
                "tool_calls": [{
                    "function": {
                        "name": "report_judgment",
                        "arguments": huge_args,
                    },
                }],
            },
            "finish_reason": "tool_calls",
        }],
    }
    payload = {"model": "x", "messages": [{"role": "user", "content": "go"}]}
    with tempfile.TemporaryDirectory() as td:
        record = _save_and_load(payload, response, td)
    saved_args = (
        record["response"]["choices"][0]["message"]["tool_calls"][0]
        ["function"]["arguments"]
    )
    assert saved_args == huge_args


# ── Error-path transcripts ─────────────────────────────────────────────


def test_error_transcript_carries_full_response_body():
    """When an HTTP call fails, the transcript still has the full
    request payload AND the full server error body. The fix that
    ensures this (writing inside the `finally` block + the explicit
    error save in the HTTP error handler) regressed once before -- the
    test pins it.
    """
    huge_error_body = _huge_text(120_000)
    payload = {
        "model": "x",
        "messages": [{"role": "user", "content": _huge_text(80_000)}],
    }
    error_response = {
        "error": True,
        "status_code": 500,
        "response_body": huge_error_body,
        "response_headers": {"x-trace": "abc"},
        "attempt": 2,
        "max_retries": 3,
    }
    # Build a synthetic error to attach to the record, with a long
    # message and a response_body attribute (mimics LLMError).
    class FakeError(Exception):
        status_code = 500
        response_body = huge_error_body

    err = FakeError(_huge_text(5_000))

    with tempfile.TemporaryDirectory() as td:
        record = _save_and_load(payload, error_response, td, label="http_500")
        # File should have suffix _ERROR when error= is set; we used the
        # no-error overload above, so re-save with the error variant.
        os.environ["WCAG_LLM_TRANSCRIPT_DIR"] = str(td)
        try:
            err_path = _save_llm_exchange(
                request_payload=payload,
                raw_response=error_response,
                target_url="https://example.test/v1",
                label="http_500",
                error=err,
            )
        finally:
            os.environ.pop("WCAG_LLM_TRANSCRIPT_DIR", None)
        err_record = json.loads(Path(err_path).read_text(encoding="utf-8"))

    # Filename suffix discipline
    assert err_path.endswith("_ERROR.json"), err_path
    # Full request preserved
    assert err_record["request"]["messages"][0]["content"] == payload["messages"][0]["content"]
    # Full response stub preserved
    assert err_record["response"]["response_body"] == huge_error_body
    # Full error metadata preserved
    assert err_record["error"]["status_code"] == 500
    assert err_record["error"]["response_body"] == huge_error_body
    assert err_record["error"]["message"] == str(err)
    assert len(err_record["error"]["message"]) == len(str(err))


# ── Summary sidecar ────────────────────────────────────────────────────


def test_summary_sidecar_captures_full_system_and_user_text():
    """The summary sidecar is supposed to make text prompts cheap to
    read without scrolling past base64. Its docstring promises FULL
    text -- if anyone ever adds a `[:N]` to the sidecar 'for
    readability', it defeats the only forensic shortcut auditors have.
    """
    sys_text = _huge_text(80_000)
    user_text = _huge_text(60_000)
    payload = {
        "messages": [
            {"role": "system", "content": sys_text},
            {"role": "user", "content": [
                {"type": "image_url", "image_url": {"url": _fake_base64_image(50_000)}},
                {"type": "text", "text": user_text},
            ]},
        ],
    }
    summary = _extract_text_summary(payload)
    assert summary["system_prompt"] == sys_text
    assert summary["user_text"] == user_text
    assert summary["image_count"] == 1
    assert len(summary["image_byte_sizes"]) == 1
    assert summary["image_byte_sizes"][0] > 50_000  # b64 expansion + prefix


def test_summary_sidecar_inside_saved_record_matches():
    """The summary inside the saved JSON record on disk must equal the
    summary the helper produces -- pin against silent divergence.
    """
    sys_text = _huge_text(40_000)
    user_text = _huge_text(40_000)
    payload = {
        "messages": [
            {"role": "system", "content": sys_text},
            {"role": "user", "content": user_text},
        ],
    }
    with tempfile.TemporaryDirectory() as td:
        record = _save_and_load(payload, {"choices": []}, td)
    assert record["summary"]["system_prompt"] == sys_text
    assert record["summary"]["user_text"] == user_text


# ── Multilingual / control characters ───────────────────────────────────


def test_unicode_and_control_chars_roundtrip():
    """ensure_ascii=False is set so multilingual prompts don't get
    expanded into \\uXXXX escapes that bloat files. But characters
    must still round-trip exactly: tabs, newlines, BOM, RTL marks,
    emoji, CJK, Arabic.
    """
    tricky = (
        "ASCII baseline\n"
        "‎ LTR mark, ‏ RTL mark\n"
        "Tab:\there\n"
        "BOM:﻿ at start\n"
        "Surrogate-pair emoji: 🇺🇸 🇯🇵\n"
        "Private use: \n"
        "Null escape: backslash u 0000 (literal)\n"
        "CJK: 中文测试 한국어 日本語\n"
        "Arabic: العربية مرحبا\n"
        "Combining: áéíóú\n"
    )
    payload = {"messages": [{"role": "user", "content": tricky * 100}]}
    with tempfile.TemporaryDirectory() as td:
        record = _save_and_load(payload, {"choices": []}, td)
    saved = record["request"]["messages"][0]["content"]
    assert saved == tricky * 100
    # Length check pins against any UTF normalization that silently
    # rewrites combining characters into precomposed forms.
    assert len(saved) == len(tricky * 100)


# ── No silent JSON encoder limits ──────────────────────────────────────


def test_no_default_size_limit_in_json_dump():
    """Sanity: a 5MB payload must persist without any cap. The writer
    uses indent=2 + ensure_ascii=False + default=str. None of those
    impose a size limit; this test exists so that if a future change
    adds a homemade encoder or wraps json.dump in a size-checking
    helper, it'll fail loudly here.
    """
    huge = _huge_text(5_000_000)  # 5 MB
    payload = {"messages": [{"role": "user", "content": huge}]}
    with tempfile.TemporaryDirectory() as td:
        record = _save_and_load(payload, {"choices": []}, td)
    saved = record["request"]["messages"][0]["content"]
    assert len(saved) == 5_000_000
    assert saved == huge


# ── Runner ─────────────────────────────────────────────────────────────


if __name__ == "__main__":
    failures = 0
    tests = [
        (n, fn) for n, fn in globals().items()
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
