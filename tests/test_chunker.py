"""Regression tests for ``functions.chunker`` HTML chunking.

The single most important property: chunking is LOSSLESS. The concatenation
of all chunk bodies must reconstruct the original input verbatim (modulo
landmark wrapper repetition for sub-parts of the same landmark, which the
caller already accounts for).

Also pins:
- Every chunk respects the max_chars budget
- Splits land on tag boundaries, never inside a tag
- Landmarks are detected and become their own sections
- Oversized landmarks get sub-split with proper depth tracking
- Pages with no landmarks fall back gracefully

Run with: python tests/test_chunker.py
"""
from __future__ import annotations

import sys
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from functions.chunker import (  # noqa: E402
    _split_at_tag_boundary,
    _split_landmark,
    _strip_script_style_bodies,
    _walk_top_level_children,
    chunk_html_by_landmarks,
    estimate_prompt_chars,
)


def _reassemble_landmark_chunks(chunks: list[str]) -> str:
    """Strip the repeated wrapper tags from sub-parts and reassemble.

    Each chunk is wrapped in the same opening/closing landmark tags. To
    verify content preservation we strip the wrappers and concatenate.
    """
    if not chunks:
        return ""
    if len(chunks) == 1:
        return chunks[0]
    import re

    open_match = re.match(r"<\w+[^>]*>", chunks[0])
    close_match = re.search(r"</\w+>\s*$", chunks[0])
    if not open_match or not close_match:
        return "".join(chunks)
    open_tag = open_match.group(0)
    close_tag = close_match.group(0)
    inners = []
    for c in chunks:
        inner = c[len(open_tag) : -len(close_tag)] if c.startswith(open_tag) and c.endswith(close_tag) else c
        inners.append(inner)
    return open_tag + "".join(inners) + close_tag


# ═══════════════════════════════════════════════════════════════════════
# Lossless invariant
# ═══════════════════════════════════════════════════════════════════════

def test_small_html_is_returned_as_one_chunk():
    html = "<html><body><p>hi</p></body></html>"
    out = chunk_html_by_landmarks(html, max_chars=1000)
    assert out == [("full page", html)]


def test_large_landmark_split_preserves_content():
    """Splitting a single oversized <main> must preserve every byte."""
    children = "".join(
        f'<section id="s{i}"><h2>Section {i}</h2><p>Content for section {i}.</p></section>'
        for i in range(1000)
    )
    main = f"<main>{children}</main>"
    chunks = _split_landmark(main, max_chars=2000)
    assert len(chunks) > 1, "expected multiple chunks"
    # Every chunk wrapped in <main>...</main>
    for c in chunks:
        assert c.startswith("<main>") and c.endswith("</main>")
        assert len(c) <= 2000 + 50  # allow tiny overhead for an oversized child
    reassembled = _reassemble_landmark_chunks(chunks)
    assert reassembled == main, "content lost during split"


def test_walk_top_level_children_handles_void_tags():
    """Self-closing void tags must not push depth."""
    inner = '<img src="a.jpg"><p>after</p><br><div>nested</div>'
    children = _walk_top_level_children(inner)
    # Should split into 4 top-level children
    assert len(children) == 4, f"got {len(children)} children: {children}"
    assert children[0] == '<img src="a.jpg">'
    assert children[1] == "<p>after</p>"
    assert children[2] == "<br>"
    assert children[3] == "<div>nested</div>"


def test_walk_top_level_children_handles_nested_elements():
    inner = "<div><span><b>deep</b></span></div><p>sibling</p>"
    children = _walk_top_level_children(inner)
    assert len(children) == 2
    assert children[0] == "<div><span><b>deep</b></span></div>"
    assert children[1] == "<p>sibling</p>"


def test_split_at_tag_boundary_is_lossless():
    """The character-level fallback splitter must preserve every byte.

    It may produce HTML fragments with unbalanced opens/closes (it cuts
    at the last ``>``, which can be a mid-element opening-tag boundary),
    but concatenation of the chunks must reconstruct the input exactly.
    The structured splitter ``_split_landmark`` is the one that produces
    balanced fragments; this fallback only guarantees losslessness.
    """
    html = "".join(f"<div>item{i}</div>" for i in range(200))
    chunks = _split_at_tag_boundary(html, max_chars=300)
    assert "".join(chunks) == html, "content lost in defensive splitter"
    for c in chunks:
        # Sanity: cut should land on a `>` (end-of-tag), never inside `<`
        assert c.endswith(">") or c == chunks[-1], (
            f"chunk did not end on a tag boundary: ...{c[-30:]!r}"
        )


def test_chunk_html_by_landmarks_respects_max_chars():
    """All returned chunks must fit under the budget (with small overhead)."""
    body = "<header><h1>logo</h1></header>" + "<main>" + "<p>x</p>" * 5000 + "</main>"
    chunks = chunk_html_by_landmarks(body, max_chars=1500)
    for label, content in chunks:
        # main parts are wrapped so allow a little overhead beyond budget
        allowed = 1500 + 50
        assert len(content) <= allowed, f"chunk {label} exceeds budget: {len(content)} chars"


def test_no_landmarks_falls_back_to_body_split():
    """A page with no landmark elements must still get split, never dropped."""
    html = "<html><body>" + "<p>" + "x" * 10000 + "</p>" + "</body></html>"
    chunks = chunk_html_by_landmarks(html, max_chars=2000)
    assert len(chunks) > 1
    for label, content in chunks:
        assert content, "empty chunk produced"


def test_empty_input_returns_empty_list():
    assert chunk_html_by_landmarks("", max_chars=1000) == []


def test_estimate_prompt_chars_includes_tool_schema():
    sys_p = "you are an auditor"
    user_p = "evaluate this"
    schema = {"type": "function", "function": {"name": "x", "parameters": {}}}
    n = estimate_prompt_chars(sys_p, user_p, schema)
    # base lengths plus overhead plus tool schema serialization
    assert n > len(sys_p) + len(user_p) + 50


def test_landmark_with_oversized_single_child_does_not_lose_data():
    """If a single child is bigger than the chunk budget, we still keep it.

    Invariant: every content byte of the oversized child appears across
    the chunks (wrapper tags may be duplicated, content bytes never are).
    """
    huge_child = "<article>" + "x" * 10000 + "</article>"
    main = f"<main><h1>title</h1>{huge_child}<p>tail</p></main>"
    chunks = _split_landmark(main, max_chars=3000)
    # Every chunk wrapped in <main>
    for c in chunks:
        assert c.startswith("<main>") and c.endswith("</main>")
    # Count total x's across all chunks — must equal exactly 10000
    total_x = sum(c.count("x") for c in chunks)
    assert total_x == 10000, f"expected 10000 x's, got {total_x}"
    # Title and tail each appear exactly once
    assert sum(c.count("<h1>title</h1>") for c in chunks) == 1
    assert sum(c.count("<p>tail</p>") for c in chunks) == 1


def test_split_landmark_recurses_into_oversized_child():
    """When a child element is itself bigger than the budget, the splitter
    must recurse into ITS top-level children rather than immediately
    dropping to character-level splitting.

    Set-up: a <main> contains one <section> that's far over budget. The
    section contains many <article> children. With recursive descent the
    section gets split at <article> boundaries (clean balanced chunks).
    """
    articles = "".join(
        f'<article id="a{i}"><h2>Article {i}</h2><p>text {i}</p></article>'
        for i in range(200)
    )
    section = f'<section id="hero">{articles}</section>'
    main = f"<main><h1>title</h1>{section}<p>tail</p></main>"
    assert len(section) > 4000  # confirm section is over the budget below

    chunks = _split_landmark(main, max_chars=2000)
    # Every chunk wrapped in <main>...</main>
    for c in chunks:
        assert c.startswith("<main>") and c.endswith("</main>"), (
            f"chunk lost wrapper: {c[:100]!r}"
        )

    # Invariant: each article marker (unique content leaf) appears exactly
    # once across all chunks. Wrapper tags may be duplicated; content never.
    for i in range(200):
        marker = f'id="a{i}"'
        count = sum(c.count(marker) for c in chunks)
        assert count == 1, f"article {i} appears {count} times across chunks"


def test_split_landmark_recurses_through_multiple_levels():
    """Three-level nesting where each level is too big at one budget step.

    <main>
      <section>
        <article>      ← multiple of these
          <p>...        ← many paragraphs each
    """
    paras_per_article = 30
    articles_per_section = 20
    sections_per_main = 5

    def article(i: int) -> str:
        ps = "".join(f"<p>paragraph {i}-{j}</p>" for j in range(paras_per_article))
        return f'<article id="a{i}">{ps}</article>'

    def section(i: int) -> str:
        arts = "".join(article(i * articles_per_section + j) for j in range(articles_per_section))
        return f'<section id="s{i}">{arts}</section>'

    main = "<main>" + "".join(section(i) for i in range(sections_per_main)) + "</main>"

    chunks = _split_landmark(main, max_chars=3000)

    # Marker invariant: every unique paragraph appears exactly once across
    # all chunks. Wrapper tags (<main>, <section>, <article>) may repeat.
    total_articles = sections_per_main * articles_per_section
    for i in range(total_articles):
        for j in range(paras_per_article):
            marker = f"paragraph {i}-{j}</p>"
            count = sum(c.count(marker) for c in chunks)
            assert count == 1, f"paragraph {i}-{j} appears {count} times"

    # Verify EVERY chunk is under or close to budget
    for c in chunks:
        assert len(c) <= 3000 + 200, f"chunk over budget: {len(c)} chars"


def test_no_landmarks_uses_recursive_walker_for_body():
    """A page with no landmarks but real structure inside <body> should
    still get clean structured splits (not fall to character-level)."""
    # No <header>/<nav>/<main>/<aside>/<footer> -- just a body of divs.
    divs = "".join(f'<div id="d{i}"><p>content {i}</p></div>' for i in range(500))
    html = f"<html><body>{divs}</body></html>"

    chunks = chunk_html_by_landmarks(html, max_chars=2000)
    assert len(chunks) > 1
    for label, content in chunks:
        # Body parts should still be valid balanced HTML (because we now
        # use _split_landmark instead of _split_at_tag_boundary for the
        # no-landmark fallback).
        assert content.startswith("<body>") and content.endswith("</body>"), (
            f"body part lost wrapper: {label} → {content[:100]!r}"
        )

    # Every original div present exactly once across all chunks
    for i in range(500):
        marker = f'id="d{i}"'
        count = sum(c.count(marker) for _, c in chunks)
        assert count == 1, f"div {i} appears {count} times"


def test_strip_script_style_bodies_drops_inner_source_keeps_attributes():
    """Script/style bodies stripped, opening-tag attributes preserved."""
    html = (
        '<head>'
        '<script src="/a.js" async="" integrity="sha256-x" crossorigin="anonymous">'
        'window.foo = function(){ /* lots of minified js here */ };'
        '</script>'
        '<style type="text/css">.a{color:red}.b{color:blue}</style>'
        '<meta charset="utf-8">'
        '</head>'
    )
    out = _strip_script_style_bodies(html)
    # Inner source gone
    assert "window.foo" not in out
    assert ".a{color:red}" not in out
    # Attributes survive
    assert 'src="/a.js"' in out
    assert 'async=""' in out
    assert 'integrity="sha256-x"' in out
    assert 'crossorigin="anonymous"' in out
    assert 'type="text/css"' in out
    # Other tags untouched
    assert '<meta charset="utf-8">' in out
    # Closing tags preserved
    assert "</script>" in out
    assert "</style>" in out


def test_chunk_html_by_landmarks_strips_head_script_bodies():
    """A page with a huge tracking-script <head> shouldn't blow up into
    multiple head_part chunks. The bodies get stripped; tags + attributes
    remain so the model still sees what scripts loaded."""
    huge_js = "var x = 'y';" * 5000  # ~60k of minified JS
    head = (
        '<head>'
        '<meta charset="utf-8">'
        f'<script src="/tracker.js" async="">{huge_js}</script>'
        '<script src="/analytics.js" defer="">' + huge_js + '</script>'
        '</head>'
    )
    body = '<body><header><h1>x</h1></header><main><p>hi</p></main></body>'
    html = f'<html>{head}{body}</html>'

    chunks = chunk_html_by_landmarks(html, max_chars=25_000)
    head_chunks = [(l, c) for l, c in chunks if l == "head" or l.startswith("head_part")]
    # One head chunk, not six
    assert len(head_chunks) == 1, f"expected 1 head chunk, got {len(head_chunks)}: {[l for l, _ in head_chunks]}"
    label, head_chunk = head_chunks[0]
    assert label == "head"
    # Script tag attributes preserved so the model still sees them
    assert 'src="/tracker.js"' in head_chunk
    assert 'src="/analytics.js"' in head_chunk
    assert 'async=""' in head_chunk
    assert 'defer=""' in head_chunk
    # Bodies gone
    assert "var x = 'y'" not in head_chunk


def test_strip_script_style_does_not_touch_body_landmarks():
    """Body content (including any inline scripts attached to interactive
    elements) is NOT stripped — only the head call path strips."""
    html = (
        '<html><head></head>'
        '<body><main>'
        '<button onclick="doStuff()">Go</button>'
        '<script>realWork()</script>'
        '</main></body></html>'
    )
    # Force landmark splitting by padding so the page exceeds the budget
    pad = "<p>filler text " + "y" * 200 + "</p>"
    html_big = (
        '<html><head></head>'
        '<body><main>'
        '<button onclick="doStuff()">Go</button>'
        '<script>realWork()</script>'
        + pad * 200 +
        '</main></body></html>'
    )
    chunks = chunk_html_by_landmarks(html_big, max_chars=5_000)
    main_chunks = [c for l, c in chunks if l.startswith("main")]
    assert main_chunks, f"no main chunk produced, labels={[l for l,_ in chunks]}"
    joined = "".join(main_chunks)
    # Body-side script bodies must NOT be stripped (only head is)
    assert "realWork()" in joined
    assert 'onclick="doStuff()"' in joined


# ═══════════════════════════════════════════════════════════════════════
# Test runner
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
