"""Regression tests for judge multimodal plumbing.

The bug these tests pin: ``analysis/judge.py:_call_judge`` previously did
not accept or pass an ``images`` parameter to
``LLMClient.call_with_tools``. Verified on a real run
(``20260506_135324_f8765656`` transcript ``00228_report_judgment.json``):
``image_count: 0``, despite visual_ai having received the same
screenshots in the prior call. Without pixels the judge cannot
independently verify visual_ai's claims; it must trust prose or reject
on text-DOM facts alone.

The fix adds an ``images`` parameter from ``judge_criterion`` →
``_call_judge`` → ``LLMClient.call_with_tools``, plus a
``BaseCheck._collect_judge_images`` helper that returns the same image
set ``run_ai_analysis`` produced for visual_ai. The slow-path call site
in ``checks/base.py`` wires it; the fast-path call site does NOT (those
SCs are programmatic-definitive).

Run with:

    python tests/test_judge_images.py
"""
from __future__ import annotations

import asyncio
import inspect
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


# ── Signature checks (cheap, no I/O) ────────────────────────────────────


def test_judge_criterion_accepts_images_kwarg():
    """``judge_criterion`` must accept ``images=`` so callers can pass
    pixel evidence. A regression here breaks the entire multimodal-judge
    feature without any runtime error -- the kwarg would just be a
    TypeError on call.
    """
    from analysis.judge import judge_criterion
    sig = inspect.signature(judge_criterion)
    assert "images" in sig.parameters, (
        "judge_criterion lost its images kwarg -- multimodal judging "
        "regressed; the judge will be text-only again"
    )
    # Must be optional so existing callers (audit_sc.py, batch_review.py)
    # don't break.
    assert sig.parameters["images"].default is None


def test_call_judge_accepts_images_kwarg():
    from analysis.judge import _call_judge
    sig = inspect.signature(_call_judge)
    assert "images" in sig.parameters
    assert sig.parameters["images"].default is None


def test_call_judge_passes_images_to_llm_client():
    """``_call_judge`` must forward ``images`` to
    ``LLMClient.call_with_tools``. Without this, the parameter would be
    accepted but silently dropped -- the kind of regression that hides
    in plain sight until the next transcript inspection.
    """
    from analysis import judge as judge_mod

    captured = {}

    class StubLLMClient:
        def __init__(self, **kwargs):
            captured["init_kwargs"] = kwargs

        async def call_with_tools(self, **kwargs):
            captured["call_kwargs"] = kwargs
            return None  # treat as parse failure -- we only care about kwargs

    original_client = judge_mod.LLMClient
    judge_mod.LLMClient = StubLLMClient
    try:
        asyncio.run(judge_mod._call_judge(
            system_prompt="sys",
            user_prompt="user",
            images=["/path/a.png", "/path/b.png"],
        ))
    finally:
        judge_mod.LLMClient = original_client

    call_kwargs = captured.get("call_kwargs") or {}
    assert "images" in call_kwargs, (
        "_call_judge forgot to forward images to LLMClient.call_with_tools"
    )
    assert call_kwargs["images"] == ["/path/a.png", "/path/b.png"]


def test_call_judge_passes_none_when_no_images():
    """No images supplied → kwarg should be None (or absent), so the
    LLM gateway routes the call through its text path and doesn't
    accidentally pin a multimodal model on a text-only judge call.
    """
    from analysis import judge as judge_mod

    captured = {}

    class StubLLMClient:
        def __init__(self, **kwargs):
            pass

        async def call_with_tools(self, **kwargs):
            captured["call_kwargs"] = kwargs
            return None

    original_client = judge_mod.LLMClient
    judge_mod.LLMClient = StubLLMClient
    try:
        asyncio.run(judge_mod._call_judge(
            system_prompt="sys",
            user_prompt="user",
        ))
    finally:
        judge_mod.LLMClient = original_client

    call_kwargs = captured.get("call_kwargs") or {}
    # Either absent or explicitly None -- both are correct.
    if "images" in call_kwargs:
        assert call_kwargs["images"] is None or call_kwargs["images"] == []


# ── BaseCheck helper ────────────────────────────────────────────────────


def test_collect_judge_images_returns_full_page_and_viewport():
    """``_collect_judge_images`` is the bridge between capture data and
    the judge call. Returning the same images visual_ai received is the
    whole point; if this method ever returns less, the judge regresses
    to partial vision.
    """
    from checks.base import BaseCheck
    from models import CaptureData

    cd = CaptureData(url="https://test/")
    cd.full_page_path = "/captures/full_page.png"
    cd.viewport_path = "/captures/viewport.png"

    check = BaseCheck()
    check.criterion_id = "1.1.1"
    images = check._collect_judge_images(cd)

    assert "/captures/full_page.png" in images
    assert "/captures/viewport.png" in images


def test_collect_judge_images_dedupes():
    """``get_extra_images`` may include full_page or viewport paths in
    its output (some subclasses do). The helper must dedupe so we don't
    pay for the same base64 twice.
    """
    from checks.base import BaseCheck
    from models import CaptureData

    cd = CaptureData(url="https://test/")
    cd.full_page_path = "/captures/full_page.png"
    cd.viewport_path = "/captures/viewport.png"

    check = BaseCheck()
    check.criterion_id = "1.1.1"

    # Patch get_extra_images to return overlapping paths
    check.get_extra_images = lambda capture_data: [
        "/captures/full_page.png",  # duplicate of base
        "/captures/zoom_200.png",
    ]
    images = check._collect_judge_images(cd)

    assert images.count("/captures/full_page.png") == 1, (
        "duplicate paths must be collapsed -- the LLM gateway re-encodes "
        "every image, so duplicates are pure waste"
    )
    assert "/captures/zoom_200.png" in images


def test_collect_judge_images_respects_opt_out():
    """A subclass with ``judge_uses_images = False`` should get no
    images. Used by document-level SCs (page lang, page title) where
    pixels add no signal.
    """
    from checks.base import BaseCheck
    from models import CaptureData

    cd = CaptureData(url="https://test/")
    cd.full_page_path = "/captures/full_page.png"

    check = BaseCheck()
    check.criterion_id = "3.1.1"
    check.judge_uses_images = False
    assert check._collect_judge_images(cd) == []


def test_collect_judge_images_survives_get_extra_images_exception():
    """If a subclass override of ``get_extra_images`` raises (e.g. a
    KeyError on missing capture data), the judge call should still go
    out with the base screenshots. Losing the entire judge call because
    of a glitchy subclass would be a worse regression than missing some
    images.
    """
    from checks.base import BaseCheck
    from models import CaptureData

    cd = CaptureData(url="https://test/")
    cd.full_page_path = "/captures/full_page.png"
    cd.viewport_path = "/captures/viewport.png"

    check = BaseCheck()
    check.criterion_id = "1.1.1"

    def boom(_capture_data):
        raise RuntimeError("simulated subclass bug")
    check.get_extra_images = boom

    images = check._collect_judge_images(cd)
    assert "/captures/full_page.png" in images
    assert "/captures/viewport.png" in images


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
