"""Regression tests for ``functions.sc_retrieval``.

Layer 3 of the cached-code-AI architecture. These tests pin the
public shape of the retrieval module without requiring bge-m3 to be
reachable (so CI can run offline). The network-bound pieces
(``retrieve_for_sc`` + ``build_pattern_embeddings``) are covered by
injecting deterministic vectors; the cosine math and top-K logic live
in ``functions.embeddings`` and are already trusted.

Run with: python tests/test_sc_retrieval.py
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from functions import sc_retrieval  # noqa: E402
from functions.sc_retrieval import (  # noqa: E402
    _pattern_text,
    _sc_query_text,
    format_retrieved_patterns,
    retrieve_for_sc,
)


_PASSED: list[str] = []
_FAILED: list[tuple[str, str]] = []


def _run(name, fn):
    try:
        fn()
        _PASSED.append(name)
        print(f"PASS  {name}")
    except AssertionError as exc:
        _FAILED.append((name, str(exc)))
        print(f"FAIL  {name}: {exc}")
    except Exception as exc:
        _FAILED.append((name, f"{type(exc).__name__}: {exc}"))
        print(f"FAIL  {name}: {type(exc).__name__}: {exc}")


def test_pattern_text_includes_raw_evidence():
    pattern = {
        "pattern_type": "image_no_alt",
        "element": "hero image in header",
        "issue": "img tag has no alt attribute",
        "sc_ids": ["1.1.1"],
        "raw_evidence": "<img src='/hero.jpg'>",
    }
    text = _pattern_text(pattern)
    assert "image_no_alt" in text
    assert "hero image in header" in text
    assert "1.1.1" in text
    assert "<img src='/hero.jpg'>" in text, "raw_evidence must be embedded"


def test_sc_query_text_combines_id_and_guidance():
    q = _sc_query_text(
        "1.1.1",
        "Non-text Content",
        "PLAIN MEANING\nEvery image needs an alt attribute.",
    )
    assert "1.1.1" in q
    assert "Non-text Content" in q
    assert "alt attribute" in q


def test_format_retrieved_patterns_empty_returns_empty_string():
    out = format_retrieved_patterns([], "1.1.1")
    assert out == "", "empty retrieval = empty block"


def test_format_retrieved_patterns_renders_score_and_evidence():
    retrieved = [
        {
            "pattern_type": "image_no_alt",
            "element": "hero image",
            "css_selector": "img.hero",
            "issue": "no alt",
            "sc_ids": ["1.1.1"],
            "raw_evidence": "<img class='hero'>",
            "source_chunk": "html:header",
            "retrieval_score": 0.87,
        }
    ]
    out = format_retrieved_patterns(retrieved, "1.1.1")
    assert "CODE EVIDENCE" in out
    assert "1.1.1" in out
    assert "0.87" in out, "score must render"
    assert "<img class='hero'>" in out, "raw_evidence must render"
    assert "phase1_sc_tags" in out
    assert "html:header" in out


def _async(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


class _FixedVectorEmbeddings:
    """Mimic functions.sc_retrieval's dependency on functions.embeddings.embed.

    Maps a handful of known query texts to fixed vectors so cosine
    math is deterministic. Only ``embed`` is swapped out during the
    retrieve test -- ``cosine_similarity`` comes from the real module.
    """

    def __init__(self, mapping):
        self.mapping = mapping

    async def embed(self, text, **kwargs):
        # Return the vector for the nearest known key by simple
        # substring match; tests only ever pass exact or near-exact
        # queries so this is deterministic.
        for key, vec in self.mapping.items():
            if key in text:
                return vec
        return [0.0] * 8


def test_retrieve_for_sc_returns_top_k_over_threshold():
    # Patterns: 3 semantically distinct. Their embeddings are
    # hand-crafted so cosine math has a clean order: pattern 0 is the
    # closest to the query, 2 is the furthest.
    patterns = [
        {"pattern_type": "image_no_alt", "sc_ids": ["1.1.1"], "raw_evidence": "<img>", "issue": "no alt"},
        {"pattern_type": "missing_fieldset", "sc_ids": ["1.3.1"], "raw_evidence": "<input>", "issue": "no fieldset"},
        {"pattern_type": "missing_track", "sc_ids": ["1.2.2"], "raw_evidence": "<video>", "issue": "no track"},
    ]
    # Orthogonal one-hot-ish vectors. Query aligns with pattern 0.
    embeddings = [
        [1.0, 0.0, 0.0, 0.0],
        [0.0, 1.0, 0.0, 0.0],
        [0.0, 0.0, 1.0, 0.0],
    ]
    query_vec = [0.95, 0.0, 0.1, 0.0]

    fake = _FixedVectorEmbeddings({"1.1.1": query_vec})

    # Monkey-patch: the retriever calls functions.sc_retrieval.embed
    # (imported at module scope via ``from functions.embeddings import embed``).
    original_embed = sc_retrieval.embed
    sc_retrieval.embed = fake.embed
    try:
        out = _async(retrieve_for_sc(
            criterion_id="1.1.1",
            criterion_name="Non-text Content",
            criterion_guidance="images need alt",
            code_findings=patterns,
            pattern_embeddings=embeddings,
            top_k=2,
            min_similarity=0.3,
        ))
    finally:
        sc_retrieval.embed = original_embed

    assert out, "should retrieve at least one pattern"
    assert out[0]["pattern_type"] == "image_no_alt", "closest pattern must rank first"
    assert "retrieval_score" in out[0]
    assert 0.0 <= out[0]["retrieval_score"] <= 1.0


def test_retrieve_for_sc_handles_length_mismatch_gracefully():
    patterns = [{"pattern_type": "x", "raw_evidence": "", "sc_ids": []}]
    embeddings = []  # wrong length
    out = _async(retrieve_for_sc(
        criterion_id="1.1.1",
        criterion_name="Non-text Content",
        criterion_guidance="",
        code_findings=patterns,
        pattern_embeddings=embeddings,
    ))
    assert out == [], "mismatched lengths must return empty, not crash"


def test_retrieve_for_sc_skips_when_cache_empty():
    out = _async(retrieve_for_sc(
        criterion_id="1.1.1",
        criterion_name="Non-text Content",
        criterion_guidance="",
        code_findings=[],
        pattern_embeddings=[],
    ))
    assert out == []


if __name__ == "__main__":
    tests = [
        ("test_pattern_text_includes_raw_evidence", test_pattern_text_includes_raw_evidence),
        ("test_sc_query_text_combines_id_and_guidance", test_sc_query_text_combines_id_and_guidance),
        ("test_format_retrieved_patterns_empty_returns_empty_string", test_format_retrieved_patterns_empty_returns_empty_string),
        ("test_format_retrieved_patterns_renders_score_and_evidence", test_format_retrieved_patterns_renders_score_and_evidence),
        ("test_retrieve_for_sc_returns_top_k_over_threshold", test_retrieve_for_sc_returns_top_k_over_threshold),
        ("test_retrieve_for_sc_handles_length_mismatch_gracefully", test_retrieve_for_sc_handles_length_mismatch_gracefully),
        ("test_retrieve_for_sc_skips_when_cache_empty", test_retrieve_for_sc_skips_when_cache_empty),
    ]
    for name, fn in tests:
        _run(name, fn)
    print()
    print(f"{len(_PASSED)} passed, {len(_FAILED)} failed")
    sys.exit(0 if not _FAILED else 1)
