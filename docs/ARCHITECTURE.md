# AccessAudit — Complete System Architecture

## THIS DOCUMENT IS LAW

Every developer, AI agent, or contributor MUST read this document in full before writing a single line of code. No shortcuts. No "I'll add that later." No truncation. No summarization of data that needs to be complete. This is a production-grade accessibility compliance tool used by real auditors for real Section 508 evaluations. Getting it wrong means real people with disabilities can't access websites, and real organizations face legal consequences.

---

## Table of Contents

1. [System Overview](#1-system-overview)
2. [Directory Structure](#2-directory-structure)
3. [The Functions Folder — Shared Reusable Code](#3-the-functions-folder)
4. [LLM API Calls — ONE Function](#4-llm-api-calls)
5. [Tool Call Parsing — ONE Function](#5-tool-call-parsing)
6. [Content Chunking — No Data Loss](#6-content-chunking)
7. [Data Models](#7-data-models)
8. [Configuration](#8-configuration)
8.5 [Default Model Fleet](#85-default-model-fleet)
9. [Capture Pipeline](#9-capture-pipeline)
10. [Video-to-Text Pre-Processing](#10-video-to-text)
11. [Check Pipeline — How Each SC Is Evaluated](#11-check-pipeline)
12. [The Judge — Final Arbiter](#12-the-judge)
13. [DOM Context — Ground Truth for the Judge](#13-dom-context)
14. [Programmatic Definitive Fast Path](#14-programmatic-fast-path)
15. [Dynamic Confidence & needs_review](#15-confidence-and-review)
16. [AT Simulation](#16-at-simulation)
17. [Cross-Page Aggregation](#17-aggregation)
18. [Report Generation](#18-reports)
19. [Web Application](#19-web-app)
20. [Batch Processing](#20-batch)
21. [Known Model Behaviors](#21-model-behaviors)
22. [The Check Files — What They Are and Why](#22-check-files)
23. [Error Handling & Debugging](#23-error-handling)
24. [Testing & Validation](#24-testing)
25. [Integration Guide — How Everything Connects](#25-integration)
26. [Rules — Non-Negotiable](#26-rules)

---

## 1. System Overview

AccessAudit is an AI-augmented accessibility conformance testing system. It combines:

- **Deterministic programmatic analysis** (axe-core, DOM parsing, attribute validation)
- **Multi-source AI evaluation** (visual AI, code AI, AT simulation)
- **An AI judge** that arbitrates between sources and writes VPAT-quality findings

### Philosophy

1. **Two layers at every stage**: Code-based deterministic analysis + AI semantic understanding
2. **NO truncation**: All data is seen. Large pages are chunked, not truncated. Every element is analyzed.
3. **NO artificial confidence caps**: AI reports true confidence. The system adjusts dynamically based on data quality.
4. **Source expertise mapping**: The judge knows which source is authoritative for each criterion.
5. **One function for each concern**: One LLM call function. One response parser. One chunking strategy per content type.

### Flow

```
URL
 |
 v
CAPTURE PIPELINE (Playwright)
 |-- Phase 0: Observation video
 |-- Phase D: DOM, a11y tree, screenshots, element extraction, axe-core
 |-- Phase 1: AI element inventory
 |-- Phase 2: Visual AI explorer (hover, click, focus screenshots)
 |-- Phase 3: AI-planned video segments
 |-- Phase 4: AT simulation cross-reference
 |-- Interactive tests: tab walk, keyboard traps, hover, forms, skip links
 |
 v
VIDEO-TO-TEXT (describe each video ONCE, reuse as text in all checks)
 |
 v
CHECK PIPELINE (per WCAG criterion)
 |-- Programmatic check (deterministic)
 |-- Axe-core finding extraction
 |-- [FAST PATH if programmatic-definitive] --> Judge VPAT synthesis --> Done
 |-- Visual AI analysis (with chunking if large)
 |-- Code AI analysis (with chunking if large)
 |-- AT simulation
 |-- Deduplicate + enrich findings
 |-- Judge AI (final arbiter)
 |-- Dynamic confidence adjustment
 |-- needs_review flagging
 |
 v
REPORT GENERATION (ACR/VPAT, DOCX, XLSX, PDF)
```

---

## 2. Directory Structure

```
wcag-tester-v6/
  functions/                  <-- ALL shared reusable code lives here
    llm.py                    <-- ONE function for all LLM calls (LLMClient)
    parser.py                 <-- ONE function for all response parsing
    prompt.py                 <-- Prompt building helpers + HIDDEN_FROM_AT_RULE
    tools.py                  <-- Tool schemas (assessment, judge, exploration)
    chunker.py                <-- Content chunking strategies (lossless)
    media.py                  <-- Image/video encoding
    embeddings.py             <-- Provider-agnostic embedding lookup
    sc_retrieval.py           <-- Per-criterion retrieval over guidelines + examples
    audio_probe.py            <-- Deterministic audio-track probe
    audio_transcriber.py      <-- Whisper-compatible transcription gateway
    image_analysis.py         <-- Pixel-level helpers (contrast, diff, focus delta)
    pixel_diff.py             <-- Image diffing primitives
    contrast.py               <-- WCAG luminance + contrast math
    aria_validator.py         <-- ARIA role/attribute validation
    shadow_dom.py             <-- Shadow-root piercing for DOM extraction
    code_analyzer.py          <-- Static-analysis hooks for Phase 1
    js_ast_filter.py          <-- JS AST filtering to a11y-relevant fragments
    js_helpers.py             <-- Reusable JS snippets injected during capture
    element_labeler.py        <-- Computed accessible-name + role labeling
    security.py               <-- URL allow/block list, SSRF guards
    bypass_log.py             <-- "Why we couldn't run check X" diagnostics

  models.py                   <-- Data classes (CaptureData, TestResult, Finding, etc.)
  config.py                   <-- Settings (backends, models, paths, timeouts)

  capture/                    <-- Page capture pipeline
    web_capture.py            <-- Main HTML/screenshot/axe orchestrator
    interactive_capture.py    <-- Keyboard / mouse / form / tooltip / hover tests
    video_describer.py        <-- Video-to-text pre-processing
    frame_extractor.py        <-- Frame extraction from video segments
    auth.py                   <-- Interactive auth (login detection + browser hand-off)
    pdf_capture.py            <-- PDF accessibility capture (PyMuPDF)
    office_capture.py         <-- DOCX / PPTX accessibility capture
    v2/
      __init__.py             <-- V2 pipeline orchestrator (phases 0–D–1–2–3–4)
      phase1_code_analysis.py <-- AI element inventory + code analysis
      phase2_visual_explorer.py  <-- Visual AI exploration
      phase3_video_segments.py   <-- AI-planned video recordings
      phase4_at_simulation.py    <-- AT cross-reference
      dom_chunker.py          <-- HTML safe splitting
      element_inventory.py    <-- Element mapping into CaptureData
      form_pause.py           <-- Form-interaction pause coordination

  checks/                     <-- WCAG success-criterion checks
    base.py                   <-- BaseCheck class (pipeline orchestration)
    registry.py               <-- Check discovery and filtering
    checks_1_1.py … checks_4_1.py        <-- WCAG 2.0/2.1 criteria (14 files)
    checks_*_aaa.py                       <-- AAA companions (11 files)
    checks_*_22.py                        <-- WCAG 2.2 additions (4 files)
    checks_doc.py             <-- Document accessibility (PDF/DOCX/PPTX)
    checks_cav.py             <-- Caption-and-audio verification

  analysis/                   <-- AI analysis (thin wrappers around functions/)
    judge.py                  <-- Judge AI (uses functions/llm.py)
    final_reviewer.py         <-- Whole-ACR Pro-tier pass for calibration
    synthesis.py              <-- Cross-source finding synthesis
    api_client.py             <-- AIClient wrapper around LLMClient
    caption_verifier.py       <-- SC 1.2.x caption-vs-audio comparison

  at_simulation/              <-- Screen-reader / keyboard simulation
    screen_reader.py          <-- A11y tree walkthrough
    keyboard_nav.py           <-- Keyboard quick-navigation
    announcements.py          <-- Announcement rendering

  prompts/                    <-- Criterion-specific prompt templates (JSON)
    1_1_1.json … 4_1_3.json

  guidelines/                 <-- WCAG normative-text reference data (used at runtime)

  crawl/                      <-- Site crawling and aggregation
    site_crawler.py
    page_selector.py
    aggregator.py

  report/                     <-- Report generation
    acr_generator.py          <-- HTML ACR report
    docx_exporter.py
    xlsx_exporter.py
    pdf_exporter.py

  storage/
    review_store.py           <-- Review file I/O

  verification/
    verifier.py               <-- Optional second-pass verification

  docs/                       <-- System documentation (this file lives here)
  templates/                  <-- Jinja2 HTML templates
  static/                     <-- CSS, JS, logos
  notes/                      <-- Scratch / per-review verification checklists
  tests/                      <-- Regression tests for parser, chunker, retrieval, ...

  app.py                      <-- FastAPI web server (entry point for the UI)
  run.py                      <-- CLI entry point
  audit_run.py                <-- Re-run a saved review
  audit_sc.py                 <-- Per-SC audit / verification helper
  batch_review.py             <-- Batch processing of multiple URLs
  send_outreach.py            <-- Outreach mailer (out-of-band utility)
```

### KEY RULE: The `functions/` folder

Every piece of reusable logic lives in `functions/`. No other file makes raw HTTP calls, parses tool responses, encodes images, or builds prompts. They import from `functions/`.

If you find yourself writing parsing logic in a check file, a capture phase, or the judge — STOP. Put it in `functions/` and import it.

---

## 3. The Functions Folder

### `functions/llm.py` — ONE function for ALL LLM calls

```python
class LLMClient:
    """The ONLY way to call an LLM in the entire system."""

    async def call(
        self, *,
        system_prompt: str,
        user_prompt: str,
        images: list[str] | None = None,
        video: str | None = None,
        tools: list[dict] | None = None,
        tool_choice: str | dict | None = None,
        temperature: float = 0.2,
        max_tokens: int | None = None,
        model_override: str | None = None,
        endpoint_override: str | None = None,
    ) -> dict:
        """Raw LLM call. Returns the chat/completions response dict."""

    async def call_with_tools(
        self, *,
        system_prompt: str,
        user_prompt: str,
        tool_name: str,
        tool_schema: dict,
        images: list[str] | None = None,
        video: str | None = None,
        temperature: float = 0.2,
    ) -> dict | None:
        """LLM call + response parsing. Returns parsed tool args or None."""
```

Every file that needs an AI response uses `LLMClient`. No exceptions:
- `checks/base.py` visual AI → `client.call_with_tools(tool_name="report_wcag_assessment")`
- `checks/base.py` code AI → `client.call_with_tools(tool_name="report_wcag_assessment")`
- `analysis/judge.py` → `client.call_with_tools(tool_name="report_judgment")`
- `capture/v2/phase2_explorer.py` → `client.call_with_tools(tool_name="report_exploration_result")`
- `capture/video_describer.py` → `client.call()` (no tools, just prose)
- `crawl/page_selector.py` → `client.call_with_tools(tool_name="select_pages")`

### Model routing inside LLMClient

```python
def _select_model(self, *, has_images, has_video, needs_audio,
                  model_override, endpoint_override) -> (model, url):
    """Pure settings-driven routing -- no caller-side pinning. Priority:
    1. Per-call model_override / endpoint_override
    2. has_video AND needs_audio:
         (a) prefer cloud AI_VIDEO_* if it points to a recognized cloud
             endpoint (googleapis, openai, anthropic, openrouter, ...) --
             Gemini Flash and similar actually process the audio track.
         (b) fall back to local AI_EXPLORER_* (Gemma E4B) ONLY if no
             cloud video model is configured. The local mlx-vlm server
             hosting E4B was empirically verified to STRIP audio from
             uploaded videos (returns 200 but the model only sees frames).
             A loud warning is logged.
    3. has_video (no audio) → AI_VIDEO_* if set, else self.vision_*
    4. has_images → AI_LOCAL_JUDGE_* (Gemma 26B for accuracy on vision)
    5. Text-only → self.model / self.base_url
    """
```

### Retry and fallback inside LLMClient

```python
async def _execute_with_retry(self, ...):
    """Retries 3 times with exponential backoff.
    On final failure, tries AI_FALLBACK_URL.
    For video calls that fail, tries video prose fallback.

    CancelledError handling: asyncio.CancelledError is BaseException, NOT
    Exception. The retry loop catches it explicitly, logs at WARNING with
    attempt info + target_url, and re-raises (cancellations must propagate
    to the parent task; retrying a cancelled task is wrong). Without this
    handler, cancellations silently bypass the retry loop and look like
    parser failures in the transcripts.
    """
```

### 413 Payload Too Large recovery

```python
async def _batch_split_images(self, ...):
    """When an image-bearing call returns 413, recursively split the
    image list in half and retry. Properties:
      - Video, when present, is attached to EVERY sub-batch (not just
        the first) so each batch keeps full temporal context.
      - Each batch retries 3 times with backoff before recursing.
      - On persistent failure, raises LLMError instead of silently
        dropping images. No gaps allowed.
      - Findings from successful batches are merged; worst verdict wins.
    """
```

### `functions/parser.py` — ONE function for ALL response parsing

```python
def parse_tool_response(
    response_data: dict,
    tool_name: str | None = None,
) -> dict | None:
    """Parse ANY LLM response and extract tool call arguments.

    Handles ALL formats:
    1. OpenAI standard tool_calls array (Gemini, OpenAI, local via vLLM)
    2. Gemma native: <|tool_call>call:name{json}<tool_call|>
    3. Qwen native: <function=name><parameter=key>value</parameter></function>
    4. Freeform JSON in content text
    5. Returns None if nothing parseable (caller decides fallback)

    ALWAYS runs clean_tool_call_args() to strip control tokens.
    ALWAYS runs strip_think_tags() to remove <think> blocks.
    """
```

This function is called by `LLMClient.call_with_tools()` internally. No other code parses tool responses.

### `functions/chunker.py` — Content chunking (LOSSLESS)

```python
def chunk_elements(elements: list[dict], max_per_chunk: int = 50) -> list[list[dict]]:
    """Split element lists for visual AI."""

def chunk_html_by_landmarks(html: str, max_chars: int = 15_000) -> list[tuple[str, str]]:
    """Split HTML into (section_name, section_html) chunks at landmark
    boundaries (head, header, nav, main, aside, footer). Each returned
    chunk is at most ``max_chars`` long.

    Strategy:
    1. <head> is extracted, then <script> and <style> BODIES are stripped
       (tag attributes -- src, async, integrity, type -- preserved). Bodies
       are minified JS/CSS that can never contain interactive elements,
       so reading them costs LLM calls for zero signal. Stripping collapses
       a 100k+ tracking-script <head> into a single inspectable chunk.
    2. Each landmark is extracted as its own section. Oversized landmarks
       are passed to _split_landmark which recursively walks top-level
       children with depth tracking, packs them greedily under budget,
       and recurses into any single child that is itself oversized
       (depth limit 8). Each chunk is wrapped in the parent landmark's
       opening/closing tags so it remains a valid stand-alone fragment.
    3. Pages with no landmarks fall back to running the same recursive
       walker on <body>.
    4. Defensive last resort: _split_at_tag_boundary cuts at the last >
       before the budget. Lossless even when structure gives out.

    Lossless invariant: every CONTENT byte (text, attributes, element
    data) appears in exactly one chunk. Wrapper tags may be intentionally
    duplicated across chunks. Pinned by tests/test_chunker.py with
    marker-count assertions.

    The default 15k is the function default; the per-SC code-AI pipeline
    overrides to 25k (see SECTION_MAX in checks/base.py:run_code_analysis).
    """

def chunk_text(text: str, max_chars: int = 200_000) -> list[str]:
    """Split text at sentence boundaries."""
```

### `functions/media.py` — Image/video encoding

```python
def encode_image(path: str, max_size: int = 1280, quality: int = 85) -> str:
    """Encode image to base64 data URI. Resizes if needed."""

def encode_video(path: str) -> str:
    """Encode video to base64 data URI."""
```

### `functions/tools.py` — Tool schemas

Single source of truth for every tool schema. No file may define a local
copy of any tool schema -- they import from here.

```python
WCAG_ASSESSMENT_TOOL = { ... }      # Visual AI + Code AI
JUDGE_TOOL = { ... }                 # Judge AI
EXPLORATION_TOOL = { ... }           # Phase 2 explorer
                                     # Field rules pinned in the schema:
                                     # - new_elements_found[].selector MUST
                                     #   be unique per child (no reusing the
                                     #   parent button selector for every
                                     #   dropdown link)
                                     # - new_elements_found[].should_explore
                                     #   is true ONLY for elements that
                                     #   themselves toggle further state.
                                     #   Plain <a href> nav links MUST be
                                     #   should_explore=false.
PAGE_SELECTOR_TOOL = { ... }         # Crawl page selection
VIDEO_SEGMENT_PLANNER_TOOL = { ... } # Phase 3 planning
```

### `functions/prompt.py` — Prompt building

```python
HIDDEN_FROM_AT_RULE: str
    """Universal exclusion -- elements removed from the accessibility
    tree are EXEMPT from WCAG content rules (alt text, accessible name,
    label, role). Embedded into the visual/programmatic system prompt
    AND the code-AI system prompt at checks/base.py:2013, so every SC
    inherits it.

    Triggers: aria-hidden=true (self or ancestor), hidden attr,
    display:none, visibility:hidden (self or ancestor), <template>
    contents, <input type=hidden>, FontAwesome/Bootstrap icons that
    have role=img + aria-hidden=true (the standard decorative-icon
    pattern). Without this rule, models flag every aria-hidden SVG
    as a missing-alt failure -- producing dozens of false positives
    per page on any site that uses an icon library.
    """

def build_system_prompt(criterion_id, criterion_name, level, ...) -> str:
    """Return a MINIMAL, UNIVERSAL system prompt (~2.5K chars).

    The system prompt contains ONLY rules that apply to every SC:
      - ROLE (WCAG auditor for a VPAT 2.5 ACR)
      - CRITERION UNDER TEST (id, name, level, normative text -- the
        only parameterized values)
      - TASK (evaluate ONLY this SC, never redirect findings)
      - PER-CRITERION RULES ARE LAW (tells the model the user-prompt
        CRITERION GUIDANCE block is authoritative and overrides its
        training on conflicts)
      - FINDING REQUIREMENTS (element, issue, impact, recommendation,
        severity -- the universal VPAT finding structure)
      - SELECTOR EVIDENCE (no invented class names / IDs)
      - RESPONSE FORMAT (tool call only, no prose)

    Everything SC-specific -- pass_conditions, fail_conditions,
    auditor_anti_patterns, off_scope_topics -- lives in ``prompts/<id>.json``
    and renders into the USER prompt via the check/judge pipelines'
    ``criterion_guidance`` block. This keeps the system prompt stable
    and makes SC behavior a JSON edit, not a code edit.
    """

def build_user_prompt(page_context, programmatic_data, elements, ...) -> str:
def format_elements_for_prompt(capture_data, criterion_id) -> list[dict]:
def build_page_context_hint(capture_data) -> str:
def load_criterion_prompt(criterion_id) -> dict | None:
    """Loads ``prompts/<id>.json`` if present. The check pipeline and
    the judge both render these fields into the user prompt's
    CRITERION GUIDANCE block:

      - pass_conditions: when the SC passes
      - fail_conditions: the only reasons a finding is valid
      - na_conditions: when the SC does not apply
      - common_mistakes: subtle traps (nudges, not hard rules)
      - auditor_anti_patterns: explicit false-positive documentation
        the model is told to DROP. SC-specific (e.g. SC 1.1.1 has
        the SVG-with-title rule, SC 1.4.3/1.4.11 have the visually-
        hidden rule, SC 2.2.2 has the muted-video rule).
      - off_scope_topics: keywords that belong to a different SC and
        must be rejected from final_findings.
      - examples: concrete illustrative cases (pass/fail/partial).

    Every per-SC behavior tweak is a JSON edit to this file -- no
    Python changes needed. The pre-refactor system prompt also
    contained per-SC rules inline; those have all been moved here.
    """
```

### `functions/audio_probe.py` — AI corroboration for audio SCs

Narrow helper imported ONLY by SC 1.4.2 (Audio Control) and SC 2.2.2
(Pause, Stop, Hide). Layered on top of the deterministic Playwright
DOM probe in capture/interactive_capture.py.

```python
async def corroborate_autoplay_audio(video_path: str | None) -> dict | None:
    """Send the observation video to a cloud video model (Gemini Flash)
    for a second opinion on autoplay audio. Returns None when:
    - no video available
    - no cloud video model is configured (mlx-vlm strips audio, so
      falling back to local would be a wasted call)
    - call fails or is cancelled (logged, not raised)
    """

def merge_audio_signals(deterministic, ai) -> dict:
    """Combine the deterministic DOM probe with the optional AI signal.
    Records which sources contributed in _signals + _agreement keys
    so the SC check can attribute the finding correctly."""
```

### The full functions/ inventory

The sections above cover the load-bearing pipeline modules. The
`functions/` folder additionally contains supporting utilities used
by checks, capture, and analysis. Anything used in more than one place
lives here per the rule in CLAUDE.md.

| File | Purpose |
|---|---|
| `llm.py` | The single LLM gateway — `LLMClient.call` / `call_with_tools`, retry, 413 split, transcript persistence |
| `parser.py` | Tool-call response parsing for all model formats; `loose_json_loads`; source-attribution validator |
| `prompt.py` | System / user prompt builders for judge, checks, video describer, page selector |
| `tools.py` | Tool schemas (`JUDGE_TOOL`, `WCAG_ASSESSMENT_TOOL`, `EXPLORATION_TOOL`, etc.) |
| `chunker.py` | Content chunking strategies for HTML, JS, image batches; merges results without truncation |
| `media.py` | Image / video encoding, base64 with MIME detection, frame extraction helpers |
| `embeddings.py` | Provider-agnostic embeddings (Ollama or OpenAI shape); used by `sc_retrieval.py` |
| `sc_retrieval.py` | Per-criterion retrieval over WCAG guidelines and prior-finding examples |
| `audio_transcriber.py` | Whisper-compatible transcription (gemini, openai, local, auto) for caption verification |
| `audio_probe.py` | Deterministic audio-track probe (does this video actually have audio?) |
| `image_analysis.py` | Pixel-level helpers: contrast sampling, diff masks, focus-indicator delta |
| `pixel_diff.py` | Image diffing primitives used by Phase 2 visual explorer and SC 2.4.7 |
| `contrast.py` | Contrast-ratio math (WCAG luminance formula, large-text exemption logic) |
| `aria_validator.py` | ARIA attribute / role validation, referenced-id existence checks |
| `shadow_dom.py` | Pierce shadow roots when extracting DOM context |
| `code_analyzer.py` | Static analysis hooks used by Phase 1 code analysis |
| `js_ast_filter.py` | Filter JavaScript AST to readable, accessibility-relevant fragments |
| `js_helpers.py` | Reusable JS snippets injected into pages during capture |
| `element_labeler.py` | Computed accessible-name + role labeling per ARIA spec |
| `security.py` | URL allow-list / block-list, SSRF guards |
| `bypass_log.py` | "Why we couldn't run check X" diagnostics surfaced into ACR appendix |

If a helper lives in any other directory and is used in more than one
place, it belongs here — see the "reusable code" rule in CLAUDE.md.

---

## 4. LLM API Calls — Exact Request/Response Formats

### Request format (OpenAI-compatible, used for ALL backends)

```json
POST /v1/chat/completions
{
  "model": "model-name",
  "messages": [
    {"role": "system", "content": "..."},
    {"role": "user", "content": "..." OR [multimodal parts]}
  ],
  "tools": [tool_schema],
  "tool_choice": "required" | "auto" | {"type":"function","function":{"name":"..."}},
  "temperature": 0.2,
  "max_tokens": 8192
}
```

### Multimodal user content

```json
{"role": "user", "content": [
  {"type": "image_url", "image_url": {"url": "data:image/png;base64,..."}},
  {"type": "image_url", "image_url": {"url": "data:video/webm;base64,..."}},
  {"type": "text", "text": "Analyze this page."}
]}
```

IMPORTANT:
- **Gemini**: Video is sent as `image_url` type (Gemini detects video from MIME type in data URI). Gemini rejects `video_url` type.
- **Local vLLM**: Video is sent as `video_url` type with `video_fps` and `video_max_frames` in payload.
- **Both**: Images always use `image_url` type.

### tool_choice values

| Backend | Accepted values |
|---------|----------------|
| Gemini OpenAI-compat | `"required"`, `"auto"`, `"none"` |
| Gemini native API | `"ANY"`, `"AUTO"`, `"NONE"`, `"VALIDATED"` |
| OpenAI | `"required"`, `"auto"`, `"none"`, `{"type":"function","function":{"name":"..."}}` |
| Local vLLM | `"auto"`, `{"type":"function","function":{"name":"..."}}` |

The system uses `"required"` for Gemini and `{"type":"function","function":{"name":"..."}}` for everything else.

---

## 5. Tool Call Parsing — Exact Response Formats

### Format 1: Gemini (OpenAI-compat endpoint)

```json
{
  "choices": [{
    "finish_reason": "tool_calls",
    "message": {
      "role": "assistant",
      "tool_calls": [{
        "function": {
          "arguments": "{\"conformance_level\":\"Supports\",\"confidence\":0.9}",
          "name": "report_wcag_assessment"
        },
        "id": "function-call-12217771426106287209",
        "type": "function"
      }]
    }
  }]
}
```

Key: `arguments` is a **JSON string** (not a dict). Must `json.loads()` it.

### Format 2: Qwen via vLLM (MLX-LM server)

```json
{
  "choices": [{
    "message": {
      "role": "assistant",
      "content": "<think>...reasoning...</think>\n\nHello!\n\n<tool_call>\n<function=test_tool>\n<parameter=message>Hello</parameter>\n<parameter=count>1</parameter>\n</function>"
    }
  }]
}
```

Key: Tool call is in `content` text, NOT in `tool_calls` array. Uses `<function=name><parameter=key>value</parameter></function>` format. Has `<think>` tags that must be stripped.

### Format 3: Gemma via vLLM (MLX-LM server)

```json
{
  "choices": [{
    "message": {
      "role": "assistant",
      "content": "<|tool_call>call:report_judgment{conformance_level:<|\"|>Supports<|\"|>,confidence:0.95}<tool_call|>"
    }
  }]
}
```

Key: Tool call is in `content` text. Uses `<|tool_call>call:name{json}<tool_call|>` format. JSON contains `<|"|>` control tokens instead of quote characters. Must run `clean_tool_call_args()` to replace these with real quotes before `json.loads()`. Keys may be unquoted.

### Format 4: Gemma E4B (sometimes ignores tools entirely)

```json
{
  "choices": [{
    "message": {
      "role": "assistant",
      "content": "Hello!"
    }
  }]
}
```

Key: No tool call at all. Model responds in plain text. Must handle gracefully — return None from parser, let caller decide fallback (e.g., send prose to a text model for structuring).

### The universal parser handles ALL of these

```python
def parse_tool_response(response_data, tool_name=None):
    # 1. Check tool_calls array (Format 1)
    # 2. Check content for native tool calls (Format 2, 3)
    #    - ALWAYS clean_tool_call_args() first
    # 3. Check content for freeform JSON
    # 4. Return None (Format 4)
```

---

## 6. Content Chunking — No Data Loss

### RULE: NEVER truncate. ALWAYS chunk.

When content is too large for one API call:
1. Split into meaningful chunks (by landmarks, by element type, by count)
2. Each chunk gets its own AI call with the FULL system prompt
3. Findings from ALL chunks are collected
4. Worst verdict wins
5. All findings go to the judge

### Visual AI chunking

Trigger: prompt > 100,000 tokens OR > 20 extra images

Strategy:
- Split elements into groups of 50
- Split extra images into batches of 15
- Each chunk gets: base screenshots + its elements + system prompt
- Video descriptions (text) included in every chunk
- Findings merge, worst verdict wins

### Code AI chunking

Trigger: HTML always chunked through `chunk_html_by_landmarks` with
`SECTION_MAX = 25_000` chars (set in `checks/base.py:run_code_analysis`).
Sized for Qwen 35B's reliable tool-call output window -- 103k prompts
push the model into extended-thinking mode and run it out of output
tokens; ~15-25k prompts parse cleanly on the first attempt.

Strategy (delegated to `functions.chunker.chunk_html_by_landmarks`):
- `<head>` is extracted once; `<script>`/`<style>` BODIES are stripped
  (tag attributes preserved). One inspectable head chunk instead of 6.
- Each landmark (header, nav, main, aside, footer) becomes its own
  section. Oversized landmarks are sub-split via `_split_landmark`,
  which recursively walks top-level children with depth tracking and
  packs them greedily under budget.
- Each chunk is wrapped in the parent landmark's tags so the model
  reads a balanced fragment with full context.
- No-landmark pages run the same recursive walker on `<body>`.
- Defensive last resort: `_split_at_tag_boundary` cuts at the last `>`
  before the budget. Lossless even when structure gives out.
- JavaScript is also chunked if large.
- Each section gets its own call.
- Findings merge across all sections.

**Lossless invariant:** every CONTENT byte appears in exactly one chunk.
Wrapper tags may be intentionally duplicated. Pinned by 16 tests in
`tests/test_chunker.py` with marker-count assertions.

### Judge DOM context

The judge receives the FULL DOM context — no truncation. All images, all form fields, all links, all headings, all landmarks. The context is built from Playwright's structured extraction, not regex on raw HTML. With 128K context models, this fits comfortably.

### Token budgets

| Component | Max prompt tokens | Model context |
|-----------|------------------|---------------|
| Visual AI | 100,000 | 128K (local) / 1M (Gemini) |
| Code AI | 200,000 chars (~50K tokens) | 128K (local) / 1M (Gemini) |
| Judge | No limit (full DOM context) | 128K (local) / 1M (Gemini) |
| AT announcements | No cap | Part of visual AI prompt |
| Visible page text | No truncation | Part of judge DOM context |

---

## 7. Data Models

### CaptureData

Holds ALL captured data for a page. Populated by the capture pipeline, consumed by checks.

Key fields:
- `html: str` — full DOM HTML (shadow DOM pierced)
- `a11y_tree: dict` — Chrome Accessibility tree
- `axe_results: dict` — axe-core violations, passes, incomplete
- `headings, links, images, form_fields, media, landmarks, tables, lists, skip_links` — structured element data
- `tab_walk, focus_indicators, hover_content, keyboard_traps` — interactive test results
- `video_descriptions: dict` — pre-processed video-to-text descriptions
- `capture_completions: dict` — which interactive tests succeeded/failed/timed out

### TestResult

Result for ONE success criterion on ONE page.

Key fields:
- `conformance_level: ConformanceLevel` — Supports, Partially Supports, Does Not Support, Not Applicable
- `confidence: float` — 0.0-1.0, dynamically adjusted
- `findings: list[Finding]` — specific issues found
- `programmatic_conformance/confidence`, `ai_conformance/confidence`, `code_ai_conformance/confidence`, `at_sim_conformance/confidence` — per-source verdicts
- `needs_review: bool` — flagged for human auditor attention
- `needs_review_reasons: list[str]` — why it needs review

### Finding

A single accessibility issue.

Key fields:
- `element: str` — human-readable location ("In the main navigation, the search button")
- `css_selector: str` — technical selector ("#search-btn")
- `issue: str` — what's wrong, referencing WCAG requirement
- `impact: str` — who is affected and how (disability groups + assistive technologies)
- `recommendation: str` — what the WCAG conformance requirement is (NOT how to fix)
- `severity: Severity` — high, medium, low, info
- `source: str` — which subsystem produced it. Allowed values:
  - `programmatic` — a deterministic check function (e.g. `Check_2_5_8.run_programmatic`) emitted this finding. Implies a measurement was taken.
  - `axe` — axe-core surfaced this.
  - `andi` — ANDI per-text-node analysis surfaced this (contrast, hidden, graphics, lang, interactive, tables variants).
  - `visual_ai` — the visual AI source flagged this from screenshots.
  - `code_ai` — the code-pattern AI source flagged this from script bundles.
  - `at_sim` — the assistive-technology simulation flagged this.
  - `judge_inference` — the judge added this finding from its own reasoning over the multi-source evidence; no input source produced it. The judge is allowed to add findings the deterministic checks missed; it just must label them honestly. The post-judge validator (`functions/parser.py:validate_source_attribution`) automatically downgrades any source claim that does not trace back to an input finding from that source.
  - Comma-separated combinations are valid when a finding is corroborated by multiple sources (e.g. `"programmatic, visual_ai"`).

Findings are written in ACR/VPAT language. They do NOT include remediation guidance. They describe the issue, the affected users, and the WCAG requirement that is not met.

---

## 8. Configuration

All settings are layered: environment variables > settings.json > built-in defaults.

```python
# Backend selection
AI_BACKEND = "vllm" | "gemini" | "openai" | "anthropic" | "openrouter"

# Primary model (text + code analysis)
AI_MODEL, AI_API_BASE_URL, AI_API_KEY

# Vision model (image analysis)
AI_VISION_MODEL, AI_VISION_API_URL

# Video model (can be different from vision — e.g. Gemini for video, local for images)
AI_VIDEO_MODEL, AI_VIDEO_API_URL, AI_VIDEO_API_KEY

# Judge model (can be stronger than primary)
AI_JUDGE_MODEL, AI_JUDGE_API_URL, AI_JUDGE_API_KEY

# Final reviewer (Pro-tier holistic pass over the completed ACR)
AI_REVIEWER_MODEL, AI_REVIEWER_API_URL, AI_REVIEWER_API_KEY

# Local model fleet (vLLM specific)
AI_FALLBACK_URL, AI_FALLBACK_MODEL          # Text fallback (Qwen 35B)
AI_EXPLORER_URL, AI_EXPLORER_MODEL          # Fast multimodal (Gemma E4B)
AI_LOCAL_JUDGE_URL, AI_LOCAL_JUDGE_MODEL    # Local judge (Gemma 26B)

# Embeddings (cross-page consistency, finding dedup, sc_retrieval)
EMBEDDINGS_API_URL, EMBEDDINGS_MODEL, EMBEDDINGS_DIM
EMBEDDINGS_FORMAT  # "ollama" | "openai" — payload + response shape
EMBEDDINGS_API_KEY # falls back to AI_API_KEY

# Audio transcription (caption verification, standalone audio)
WHISPER_API_URL
WHISPER_FORMAT  # "auto" | "local" | "openai" | "gemini"
WHISPER_API_KEY # falls back to AI_API_KEY
WHISPER_GEMINI_MODEL  # used when WHISPER_FORMAT="gemini"

# Concurrency + rate limiting (both settable via settings.json / UI)
AI_MAX_CONCURRENT = 1  # Max in-flight LLM calls across the whole process.
                       # 1 = serial (safe default, REQUIRED for local vLLM
                       #     which OOMs under concurrent load).
                       # N = up to N concurrent calls. ONLY safe with cloud
                       #     providers (Gemini, OpenAI, Anthropic,
                       #     OpenRouter). Phase 4/5 benefit most -- ~3x
                       #     wall-clock speedup at N=10 on a full run.
                       # Implemented as asyncio.Semaphore(N) in LLMClient.
AI_RPM = 0             # Token-bucket rate limit (requests per minute).
                       # 0 = unlimited. Gemini free tier = 15. Tier 1 = 4000.
                       # Bucket is concurrency-safe -- it throttles across
                       # all in-flight calls from all tasks.
```

**Settings page**: both `ai_max_concurrent` and `ai_rpm` are editable
via the `/settings` page under the Advanced section. Restart the server
after changing to pick up new values.

### API key cascade (single source of truth)

`config.py` only requires the master `api_key`. Every per-role key
falls back to it automatically:

```
AI_JUDGE_API_KEY    -> AI_API_KEY
AI_REVIEWER_API_KEY -> AI_JUDGE_API_KEY -> AI_API_KEY
AI_VIDEO_API_KEY    -> AI_API_KEY
```

So `settings.json` only needs `api_key` once; per-role key fields
should be left empty unless you genuinely need a different key for
that role (e.g. the judge runs on a different paid tier than the
primary). The `/api/settings` POST handler enforces this on save:
any per-role key whose value equals the master `api_key` is dropped
from the JSON before write, so the file stays the single source of
truth and the cascade in `config.py` does the rest.

**429 / rate-limit handling**: the LLM client's retry loop detects
`429 Too Many Requests` explicitly, honors the `Retry-After` header when
present, and adds jitter (up to `min(base, 2.0)` seconds) to every
backoff so N concurrent tasks that all hit 429 at the same moment don't
retry on the same tick. Same jitter applies to generic retry backoff.

### Full-Gemini-stack mode

A single Gemini API key powers chat, vision, embeddings, AND audio
transcription. The only local processes that remain are Playwright
(browser capture) and ffmpeg (audio/frame extraction) — neither has an
LLM equivalent. Configure via `settings.json`:

```json
{
  "ai_backend": "gemini",
  "api_key": "<gemini-key>",
  "ai_model": "gemini-3.1-flash-lite-preview",
  "ai_judge_model": "gemini-3-flash-preview",
  "ai_reviewer_model": "gemini-3-flash-preview",
  "embeddings_api_url": "https://generativelanguage.googleapis.com/v1beta/openai/embeddings",
  "embeddings_format": "openai",
  "embeddings_model": "gemini-embedding-001",
  "embeddings_dim": "3072",
  "whisper_api_url": "https://generativelanguage.googleapis.com/v1beta",
  "whisper_format": "gemini",
  "whisper_gemini_model": "gemini-2.5-flash"
}
```

The whisper Gemini path uses Gemini's NATIVE generateContent endpoint
(not OpenAI-compat) because audio understanding is not exposed via the
OpenAI-compat layer. Audio is sent as `inline_data` with mime_type
`audio/wav`. The 60-second sample cap in `audio_transcriber.py` keeps
each clip well under Gemini's ~20MB inline limit.

### Hybrid mode (recommended for batch processing)

```json
{
  "ai_backend": "vllm",
  "ai_model": "Qwen3.5-35B",
  "api_base_url": "http://local:11801/v1",
  "ai_vision_model": "Qwen3-VL-32B",
  "ai_vision_api_url": "http://local:11802/v1",
  "ai_video_model": "gemini-2.5-flash-lite",
  "ai_video_api_url": "https://generativelanguage.googleapis.com/v1beta/openai",
  "ai_video_api_key": "your-gemini-key"
}
```

Text, images, code → local models (free, no rate limits).
Video → Gemini (local models crash on large videos).

---

## 8.5 Default Model Fleet

The default model assignments are settings-driven and editable via the
front-end `/settings` page. Routing is done by `LLMClient._select_model`
in `functions/llm.py` based on call content type (text / image / video /
audio). No file pins a model directly -- everything flows through this
selector.

### Local mixed stack (the default — accuracy-first)

This is the recommended fleet for producing real audit reports. Each
model is sized to its job; switching everything to one model trades
accuracy for speed.

| Role | Setting | Model | Endpoint | Why |
|---|---|---|---|---|
| **Code AI** (HTML/JS analysis) | `AI_MODEL` / `api_base_url` | Qwen 3.5 35B (4-bit) | `localhost:11801` | Strong code reasoning. Catches semantic alt-text issues, ARIA conflicts, custom Bootstrap grids. |
| **Vision (Phase 2 explorer)** | `AI_EXPLORER_MODEL` / `ai_explorer_url` | Gemma 4 E4B (4-bit) | `localhost:11804` | Fast multimodal. Used for per-element screenshot diffing where speed matters more than depth. |
| **Vision (judge / per-SC visual)** | `AI_LOCAL_JUDGE_MODEL` / `ai_local_judge_url` | Gemma 4 26B (4-bit) | `localhost:11805` | Image-bearing calls always route here. **Accuracy over speed** rule: E4B was observed hallucinating `focus_visible` on byte-identical screenshots, so all image calls use 26B regardless of caller intent. |
| **Vision fallback** | `AI_FALLBACK_VISION_MODEL` / `ai_fallback_vision_url` | Qwen3-VL 32B (4-bit) | `localhost:11802` | Hot standby if 26B is unavailable. |
| **Text fallback** | `AI_FALLBACK_MODEL` / `ai_fallback_url` | Qwen 3.5 35B (4-bit) | `localhost:11801` | Same as primary code AI; used when other endpoints fail. |
| **Video (frames)** | `AI_VIDEO_MODEL` / `ai_video_api_url` | Gemini 3 Flash (cloud) | `googleapis.com/.../openai/` | Local mlx-vlm chokes on large videos; cloud handles them. **Required for SC 1.4.2 / 2.2.2 audio detection** -- local mlx-vlm strips audio tracks before inference. |
| **Speech-to-text (captions)** | `WHISPER_*` | faster-whisper large-v3-turbo | `localhost:11803` | Used by the video describer for caption transcription. Not an LLM. |
| **Final judge** | `AI_JUDGE_MODEL` / `ai_judge_api_url` | Same as primary (Qwen 35B) by default | `localhost:11801` | Settable separately if you want the judge on a stronger model. |
| **Final reviewer** (whole-ACR Pro pass) | `AI_REVIEWER_MODEL` / `ai_reviewer_api_url` | Defaults to `AI_JUDGE_MODEL` | (cascades from judge) | One holistic pass over the completed ACR for calibration / contradiction / citation / tone fixes. Point at a Pro-tier model (e.g. `gemini-3-pro-preview`) for production runs. |

### Single-model stacks (for fast iteration / structural validation)

When you need to prove the pipeline runs end-to-end without burning
hours, point every setting at a single model. Two common single-model
configurations:

**Cloud (Gemini 3 Flash Lite + 3 Flash judge)** — current default
configuration. Native OpenAI tool calls, 1M context window so
chunking pressure disappears, much better hallucination control than
local E4B. The Pro-tier reviewer pass runs once per review on the
completed ACR for calibration / contradiction / citation / tone fixes.

```jsonc
// settings.json -- master api_key only; per-role keys cascade via config.py
{
  "ai_backend": "gemini",
  "api_base_url": "https://generativelanguage.googleapis.com/v1beta/openai",
  "api_key": "<your gemini api key>",
  "ai_model": "gemini-3.1-flash-lite-preview",
  "ai_vision_model": "gemini-3.1-flash-lite-preview",
  "ai_vision_api_url": "https://generativelanguage.googleapis.com/v1beta/openai",
  "ai_local_judge_url": "https://generativelanguage.googleapis.com/v1beta/openai",
  "ai_local_judge_model": "gemini-3.1-flash-lite-preview",
  "ai_explorer_url": "https://generativelanguage.googleapis.com/v1beta/openai",
  "ai_explorer_model": "gemini-3.1-flash-lite-preview",
  "ai_judge_api_url": "https://generativelanguage.googleapis.com/v1beta/openai",
  "ai_judge_model": "gemini-3-flash-preview",
  "ai_reviewer_api_url": "https://generativelanguage.googleapis.com/v1beta/openai",
  "ai_reviewer_model": "gemini-3.1-pro-preview-customtools",
  "ai_video_api_url": "https://generativelanguage.googleapis.com/v1beta/openai",
  "ai_video_model": "gemini-3.1-flash-lite-preview"
}
```

Model names update over time. `config.py`'s built-in Gemini default
(`_BACKEND_DEFAULTS["gemini"]["model"]`) is the fallback when
`settings.json` doesn't override; keep that string aligned with whatever
preview/GA tag is current.

**Local Gemma 4 E4B only** — fastest free option, but expect
hallucinated focus indicators and shallower code analysis. Useful only
for "does the architecture run" smoke tests.

```jsonc
{
  "ai_backend": "vllm",
  "api_base_url": "http://localhost:11804/v1",
  "ai_model": "google/gemma-3n-E4B-it",
  "ai_vision_api_url": "http://localhost:11804/v1",
  "ai_vision_model": "google/gemma-3n-E4B-it",
  "ai_local_judge_url": "http://localhost:11804/v1",
  "ai_local_judge_model": "google/gemma-3n-E4B-it",
  "ai_judge_api_url": "http://localhost:11804/v1",
  "ai_judge_model": "google/gemma-3n-E4B-it"
}
```

### Routing priority (settings-driven, no caller pinning)

`_select_model` in `functions/llm.py:_select_model` makes one decision
per call based on content type:

1. Per-call `model_override` / `endpoint_override` (rare — most callers don't pass these)
2. `has_video AND needs_audio` → cloud `AI_VIDEO_*` if it's a recognized cloud endpoint, else local `AI_EXPLORER_*` with a loud warning that local audio is broken
3. `has_video` (no audio) → `AI_VIDEO_*` if set, else `self.vision_*`
4. `has_images` → `AI_LOCAL_JUDGE_*` (the **accuracy** judge, not E4B)
5. Text-only → `self.model` / `self.base_url`

There is no caller-side `_base_url_pinned` flag. All routing comes from settings.

### Default models are these — do NOT change without updating this section

The local mixed stack above is the **default and recommended** configuration.
Any change to the default model assignments must update this section in the
same commit so the doc stays the source of truth.

---

## 9. Capture Pipeline

### Phase 0: Observation

Records a video of the page loading and sitting idle.
- Duration: 15s for static pages, 60s for dynamic, 120s if JS timers detected
- Timer detection: checks for `setTimeout`, `setInterval`, `session/timeout/expire` keywords in scripts
- Output: `observation_video_path`, `observation_frames`, `flash_analysis`

### Phase D: Deterministic Capture

All non-AI extraction:
- Full DOM HTML (shadow DOM pierced)
- Accessibility tree (Chrome CDP)
- Screenshots: viewport, full-page, 200% zoom, 320px width
- Computed styles for contrast calculation
- Element extraction: headings, links, images, form fields, media, landmarks, tables, lists, iframes, skip links, background images, CAPTCHAs, pseudo-elements
- Viewport meta, page language
- Axe-core injection and analysis
- Structural summary for cross-page comparison
- **Autoplay media probe** -- deterministic Playwright JS query of every
  `<audio>`, `<video>`, and embedded-player `<iframe>` (YouTube, Vimeo,
  SoundCloud, Spotify, Wistia). Records each element's runtime state
  (autoplay attr, muted, paused, currentTime, duration, controls,
  visibility) and writes a summary onto `capture_data.audio_detection`
  in the schema SC 1.4.2 / SC 2.2.2 already consume. Replaces the prior
  Gemma E4B audio-LLM call -- mlx-vlm strips audio tracks before
  inference, and Playwright's recorded webm has no audio stream at all,
  so the LLM call was an architectural no-op. SC 1.4.2 / SC 2.2.2 layer
  AI corroboration on top via `functions/audio_probe.py` only when a
  cloud video model is configured.

### Phase 1: AI Element Inventory

AI reads the HTML (chunked by landmarks) and catalogs every interactive element with:
- Type, selector, text, exploration priority, suggested actions
- Removes false positives (hidden elements, SVG internals)
- Maps elements to CaptureData fields

### Phase 2: Visual AI Explorer

For each explorable element (up to 1500):
- Screenshot initial state
- Hover → screenshot
- Click/focus → screenshot
- Send screenshots to AI via `LLMClient.call_with_tools(tool_name="report_exploration_result")`
- AI reports: interaction type, new elements found, focus indicator visible, state changes, accessibility observations
- Recursively explores newly discovered elements (modals, submenus) up to depth 5
- Records exploration completeness in `capture_completions`

### Phase 3: AI-Planned Video Segments

AI examines element inventory and plans video recordings:
- Tab walkthrough, form interaction, menu navigation, modal interaction, media playback, accordion, carousel
- Each segment is a recorded browser session with scripted keyboard/mouse actions
- Output: video files + action logs

### Phase 4: AT Cross-Reference

Runs screen reader simulation on the accessibility tree. Cross-checks against Phase 1 inventory for missing elements.

### Interactive Tests

Separate from AI phases. Deterministic keyboard/mouse testing:
- **Tab walk**: 5000 Tab presses, records every focused element
- **Backward tab walk**: Shift+Tab from end
- **Keyboard walkthrough video**: Recorded video with Tab, Enter, ArrowDown, Escape, Space, Shift+Tab
- **Dropdown close verification**: After Escape, checks `aria-expanded` went back to `"false"` and no menus remain open
- **Focus indicators**: Screenshots focused/unfocused state for each element
- **Hover detection**: Hover over links/buttons, capture CSS changes
- **Text spacing**: Inject WCAG spacing CSS, detect overflow:hidden clipping
- **Form submission**: Clear required fields, submit, capture validation errors, then fill valid values and verify error state clears
- **Skip link verification**: Click skip links, verify focus lands on target
- **Context change detection**: Select options, check URL changes
- **Audio detection**: AI analyzes observation video for autoplay audio
- **Capture completions**: Every test records ok/timeout/error status

---

## 10. Video-to-Text Pre-Processing

After capture completes, BEFORE testing starts:

1. For each captured video (keyboard walkthrough, observation, video segments):
2. Send to vision model with SC-targeted questions (see VIDEO_QUESTIONS map)
3. Get prose description back
4. Store in `capture_data.video_descriptions`
5. During checks, inject text descriptions into prompts instead of raw video

This is critical because:
- Avoids sending 12MB video in every check call (keyboard walkthrough feeds 8+ SCs)
- Works with models that can't handle video + tool calls simultaneously
- Reduces context window pressure
- Each video is described ONCE, reused everywhere

### Question sets per video type

- **keyboard_walkthrough**: Focus indicators, tab order, traps, dropdown open/close, modal behavior, unreachable elements
- **observation**: Auto-playing media, moving/blinking content, flashing, timers, content changes
- **FORM_INTERACTION**: Error messages, labels, required indicators, autocomplete
- **MENU_NAVIGATION**: Open via keyboard, arrow key navigation, Escape close, focus return
- **MODAL_INTERACTION**: Focus trapped inside, Escape closes, focus returns to trigger
- **MEDIA_PLAYBACK**: Captions, audio descriptions, controls, transcript, auto-play

### Video model routing

Video always goes to `AI_VIDEO_MODEL` (configured in settings). For hybrid mode, this is Gemini even when everything else is local. Local vision models crash on videos >1MB.

---

## 11. Check Pipeline — How Each SC Is Evaluated

### BaseCheck.run() — Entry point

```
1. Log: "SC 1.1.1 (Non-text Content) — Level A — FULL PATH"
2. Check applicability (skip if N/A)
3. Call execute()
4. Adjust confidence dynamically
5. Flag for review if uncertain
6. Log: "SC 1.1.1 DONE in 45.3s — Does Not Support (85%) — 7 findings"
```

### BaseCheck.execute() — Two paths

#### Path 1: PROGRAMMATIC DEFINITIVE (7 criteria)

For SCs where attribute existence = correctness:
- 4.1.1 Parsing (duplicate IDs)
- 3.1.1 Language of Page (lang attribute)
- 2.4.2 Page Titled (title element)
- 1.3.5 Identify Input Purpose (autocomplete)
- 2.5.3 Label in Name (text comparison)
- 3.3.2 Labels or Instructions (label presence)
- 2.3.1 Three Flashes (mathematical frame analysis, when data exists)

Flow: programmatic + axe-core → judge VPAT synthesis only (no visual AI, code AI, or AT sim)

If programmatic returns NOT_EVALUATED (e.g. 2.3.1 with no flash data), falls through to full pipeline.

#### Path 2: FULL PIPELINE (all other criteria)

```
1. Programmatic check (run_programmatic)
2. Axe-core finding extraction
3. Capture completeness check (lower confidence if data missing)
4. Visual AI analysis (run_ai_analysis, with chunking if large)
5. Code AI analysis (run_code_analysis, with HTML section iteration)
6. AT simulation (screen reader walkthrough)
7. Deduplicate + enrich findings
8. Judge AI (final arbiter — reviews all sources, writes VPAT findings)
9. Fallback: algorithmic reconciliation if judge fails
```

### Visual AI prompt structure

```
SYSTEM: You are evaluating SC {id} {name} (Level {level}).
REQUIREMENT: {normative_text}
{criterion_guidance from prompts/{id}.json}
{off_scope_keywords}
{page_context_hint}

USER:
Page: {url}
Title: {title}
Programmatic found: {programmatic_data}
Elements: {formatted elements}
A11y tree summary: {a11y nodes}
Screen reader announcements: {announcement lines}
Image context: {exploration screenshot descriptions}
Video observations: {pre-computed video descriptions}
```

### Code AI prompt structure

```
SYSTEM: You are reviewing source code for ONE WCAG criterion.
CRITERION: {id} {name} (Level {level})
REQUIREMENT: {normative_text}
{pass/fail conditions from prompt template}
RULES: Evaluate ONLY {id}. Nothing else.

USER:
[HTML SECTION {n}/{total}: {section_name}]
```html
{section_html}
```
[JAVASCRIPT]
```javascript
{readable_scripts}
```
Does this code pass or fail WCAG {id}?
```

---

## 12. The Judge — Final Arbiter

The judge reviews ALL source verdicts and findings for ONE criterion and makes the final call.

### Judge system prompt key points

- Four independent systems evaluated the page
- Programmatic is deterministic and factual but can't judge quality
- AT simulation reports what screen readers announce
- Code AI reads source but sometimes confuses criteria
- Visual AI catches visual/semantic issues but may hallucinate
- Use DOM FACTS to verify or reject AI claims
- If DOM facts contradict a finding, REJECT it
- Rewrite accepted findings in VPAT language
- Write Remarks and Explanations summary

### Judge tool schema — DETAILED field requirements

```json
{
  "name": "report_judgment",
  "parameters": {
    "conformance_level": "Supports | Partially Supports | Does Not Support | Not Applicable",
    "confidence": 0.0-1.0,
    "reasoning": "Internal reasoning — explain WHY this conformance level. Which sources agreed/disagreed. Which findings accepted/rejected and why. For auditor review, NOT for the VPAT.",

    "final_findings": [
      {
        "element": "WHERE on the page — visual description for a human reader who is looking at the page. Example: 'In the top navigation bar, the search form input field' or 'The hero image below the main heading'. Must be specific enough for ANYONE to find the element by looking at the page.",

        "css_selector": "Technical CSS selector for developers. Pull from source finding data — programmatic and AT sim findings always have selectors. Example: '#UA_BrandBar_SearchBtn' or 'header > nav > ul > li:nth-child(3) > a'",

        "issue": "WHAT is wrong — specific, evidence-based, references the WCAG requirement by number. Include measured values when available (e.g., 'contrast ratio is 2.1:1, below the required 4.5:1'). Written for a VPAT — factual, not conversational.",

        "impact": "WHO is affected and HOW — name specific disability groups (blind users, low vision users, motor impaired users) and specific assistive technologies (JAWS, NVDA, VoiceOver, keyboard-only, switch access). Explain what they can't do.",

        "recommendation": "The WCAG conformance requirement that is not met. State what PASSING looks like. Do NOT provide code fixes or remediation — just describe what conformance requires. Example: 'WCAG 1.1.1 requires all non-text content to have a text alternative that serves the equivalent purpose.'",

        "severity": "high (blocks access entirely) | medium (significant barrier) | low (minor issue) | info (best practice observation)",

        "source": "Enum: programmatic | axe | andi | visual_ai | code_ai | at_sim | judge_inference. The tag MUST trace back to the input source that produced this finding. When the judge ADDS a finding that no input source produced — its own inference from DOM context or screenshots — the source MUST be 'judge_inference'. The post-judge validator (functions/parser.py:validate_source_attribution) downgrades any unsupported source claim to judge_inference automatically; mislabeling only makes the audit log noisier."
      }
    ],

    "rejected_findings": [
      {
        "index": "Integer index of the finding in the input list",
        "reason": "Why this finding was rejected — what criterion does it actually belong to?",
        "correct_criterion": "The SC this finding should be evaluated under, e.g. '4.1.2'"
      }
    ],

    "vpat_summary": "The VPAT 'Remarks and Explanations' text. 1-3 sentences. For Supports: confirm conformance briefly. For Partially/Does Not Support: describe the specific issues and affected user groups. Evidence-based, professional tone. NO remediation guidance. This goes directly into the ACR report."
  }
}
```

### The judge system prompt — EXACT requirements

The judge prompt MUST include these instructions:

1. **ROLE**: Senior WCAG auditor, final arbiter for Section 508 conformance
2. **FOUR SYSTEMS**: Explain what each source does and its limitations
3. **TASK**: Read criterion → review findings → reject off-criterion → rewrite in VPAT → determine conformance → write summary
4. **FINDING QUALITY**: Every finding MUST have:
   - A visual element location a human can find on the page
   - A CSS selector a developer can use to locate the element
   - A specific WCAG requirement reference
   - Named disability groups and assistive technologies affected
   - The conformance requirement (what passing looks like)
5. **DOM FACTS**: When provided, USE them to verify/reject AI claims. If DOM facts say "Radio in fieldset: YES" and AI says "radio not in fieldset" → REJECT
6. **MUST COMMIT**: Never return "Not Evaluated" — make a decision
7. **CROSS-CRITERION CONFUSION**: List common mistakes (icon without label = 4.1.2, not 1.3.3, etc.)
8. **SOURCE EXPERTISE**: Per-criterion guidance on which source to trust (e.g., "Visual AI is expert for 1.1.1 alt text quality")

### The judge prompt for VPAT synthesis mode (programmatic-definitive)

Simplified prompt for the 7 deterministic criteria:
- Accept ALL findings as verified truth — do NOT reject any
- Do NOT change the conformance level — it is a mathematical fact
- ONLY rewrite findings in VPAT language with proper element locations
- Generate Remarks and Explanations summary

### VPAT synthesis mode (for programmatic-definitive criteria)

Simplified judge prompt:
- Accept ALL findings as verified truth
- Do NOT override conformance level
- ONLY rewrite in VPAT language
- Generate Remarks and Explanations

### Judge returns DICTS not Finding objects

The judge parser (`_parse_judge_response`) must return `final_findings` as plain dicts, NOT as Finding objects. This ensures clean JSON serialization and prevents the `isinstance(ff, dict)` check in base.py from dropping them.

### Measurement evidence blocks in DOM context

For SCs with deterministic pass/fail rules, `_build_dom_context` inserts
a labeled measurement block before the judge runs. The judge cannot
legitimately produce a finding that contradicts the listed measurements.

**SC 2.5.8 — `_format_target_size_measurements`**: lists every
interactive target's rect (WxH), centre coordinates, and nearest-neighbor
distance, plus a per-element verdict per the WCAG 2.5.8 rule:
- size ≥ 24x24 → PASS
- inline (in a paragraph / list / cell) → PASS (inline exception)
- spacing ≥ 24px to nearest target → PASS (spacing exception)
- otherwise → FAIL

The block ends with a deterministic count (`N targets pass, M targets
fail`) and the rule "Findings are only legitimate for FAIL entries
above (if any)."

**Why this matters**: without this block, the judge for SC 2.5.8 sees
only screenshots + a list of links by name + their hrefs. Asked to
evaluate "Target Size (Minimum)," it writes plausible-sounding but
wrong claims like "0px of spacing between this target and its
neighbors." With the measurement block, the model has 24-pixel
distances explicitly listed and cannot legitimately invent "0px."

The pattern generalises: any SC with measurement-driven criteria
should surface measurements in the prompt. The corresponding helper
on `Check` should be named `_format_*_measurements` and called from
the criterion-id-specific branch in `_build_dom_context`.

### Browser-handled annotation on the ANDI HIDDEN block

The `ANDI HIDDEN-CONTENT AUDIT` block marks each entry
`[BROWSER-HANDLED]` when the browser correctly removes it from the
tab order regardless of tabindex:
- `rect=0x0` (no rendered box),
- `display:none` / `visibility:hidden` / `hidden` attribute,
- `inert` attribute on element or any ancestor,
- `tab_reachable=False` (capture's runtime probe says it's not in tab order).

The block header explicitly tells the judge: "DO NOT emit findings
for [BROWSER-HANDLED] entries under SC 2.1.1, 2.1.2, 2.4.3, 2.4.7,
4.1.2, or 1.3.1 — the focus leak the finding would describe does not
actually exist."

This eliminates the cross-SC false-positive cluster from cookie
banners / preference modals / tracking pixels. Observed on a university site: 76 of
76 hidden-content entries are marked browser-handled, so the judge
has no legitimate grounds to flag any of them as focus leaks.

### Source-attribution validation (post-judge)

After every judge call, the post-judge consumer in `checks/base.py` runs
`functions.parser.validate_source_attribution(judge_output, input_findings)`.
The validator enforces: every output finding's source claim must trace
back to an input finding from that source — otherwise the source is
downgraded to `judge_inference`.

Match criteria (lenient, ANY succeeds):
- Exact CSS selector match
- Selector substring overlap (≥ 5 chars, either direction — judge often
  shortens selectors when consolidating)
- Element-description exact match
- Issue-text prefix overlap (≥ 30 chars, either direction)

Why "lenient": the judge legitimately rewords inputs into VPAT prose,
shortens selectors, and merges related findings. The validator should
recognize the descendant-of-input case and only flag genuine fabrications.

Why this exists: observed on a university site SC 2.5.8 — the deterministic check
correctly applied the WCAG spacing exception and produced 0 findings,
but the judge looked at the screenshot of the utility menu, decided the
links were too small, and emitted 9 findings labeled
`source="programmatic"`. An auditor reading "9 programmatic target-size
failures" would assume the math measured them. With the validator, those
9 findings appear as `source="judge_inference"` and the per-SC
`confidence_reasoning` is annotated "9 finding(s) recorded as
judge_inference (model added these beyond what input sources produced —
recommend human review)".

The validator does NOT remove findings or change verdicts. The judge's
autonomy to spot real issues the deterministic checks missed is
preserved — those findings still drive the verdict via severity. Only
the **labeling integrity** is enforced.

For PROGRAMMATIC_DEFINITIVE SCs (the fast path), any judge_inference
finding triggers an explicit "FAST-PATH WARNING" in the
`confidence_reasoning`, because those SCs are supposed to be entirely
backed by deterministic measurement.

---

## 13. DOM Context — Ground Truth for the Judge

Built from Playwright's STRUCTURED extraction (not regex on raw HTML). Includes:

```
PAGE TITLE: "Example University"
LANGUAGE: html lang="en-us" (valid=True)

HEADINGS:
  Counts: h1=1, h2=12, h3=14
  List:
    <h1> "Farther Than Ever"
    <h2> "Start Here"
    ...

IMAGES (9 total):
    hero.jpg: alt=(EMPTY alt="")
    logo.svg: alt="University Logo"
    spacer.gif: alt=(EMPTY alt="") [decorative]

LANDMARKS (5):
    <header> role=banner label="(no label)"
    <nav> role=navigation label="Main navigation"
    <main> role=main label="(no label)"
    ...

FORM FIELDS (3):
    search name="q" label="Search"
    radio name="scope" label="Search this site" [in-fieldset]
    radio name="scope" label="Search Example University" [in-fieldset]
  Radio inputs: 2, in <fieldset>: YES

LINKS (138 total):
    "Apply Now" -> /apply
    "Academics" -> /academics
    ...

TABLES, SKIP LINKS, TAB WALK, KEYBOARD TRAPS, MEDIA,
DUPLICATE IDS, SEARCH FIELDS, ARIA ROLES, AXE-CORE SUMMARY,
VISIBLE PAGE TEXT (full, no truncation)
```

This lets the judge verify:
- "radio not in fieldset" → DOM says `in <fieldset>: YES` → REJECT
- "duplicate h1" → DOM says `h1=1` → REJECT
- "multiple search fields" → DOM says `SEARCH FIELDS: 1` → REJECT
- "alt text is empty" → DOM says `hero.jpg: alt=(EMPTY)` → ACCEPT

---

## 14. Programmatic Definitive Fast Path

For the 7 criteria where programmatic checks are 100% deterministic:

```
run_programmatic() + axe-core
    ↓
Accept verdict as truth
    ↓
Judge (VPAT synthesis only — rewrite in human language)
    ↓
SKIP visual AI, code AI, AT simulation
    ↓
Result
```

Saves ~80% of time per criterion. Only used when existence = correctness.

---

## 15. Dynamic Confidence & needs_review

After EVERY check, `_adjust_confidence_and_flag_review()` runs:

### Confidence adjustments (multiplicative)

| Condition | Multiplier |
|-----------|-----------|
| Required capture test failed/timed out | x0.6 |
| Programmatic and visual AI disagree | x0.85 |
| Only 1 source provided a verdict (non-definitive SC) | x0.9 |

### needs_review triggers

- Final confidence < 65%
- Supports with 0 findings but relevant content exists on the page
- All AI sources returned Not Evaluated
- Required capture data was incomplete

### Content indicators (for "Supports with 0 findings" detection)

Maps criterion → CaptureData fields that indicate relevant content:
- 1.1.1 → images, background_images, captchas
- 1.4.3 → computed_styles, colors
- 3.3.2 → form_fields
- etc.

---

## 16. AT Simulation

### Screen reader walkthrough

Traverses the accessibility tree node by node. For each node, generates what a screen reader (JAWS/NVDA/VoiceOver) would announce. Detects:

- Missing accessible names on images
- Misclassified decorative images
- Heading hierarchy issues
- Table structure problems
- Form label associations
- Landmark structure
- Link purpose quality
- Focus visibility
- Name/role/value completeness
- Live regions
- aria-describedby targets that don't exist
- aria-invalid without aria-errormessage
- aria-current missing on navigation
- Combobox pattern completeness

### Keyboard quick-navigation simulation

Simulates: H (headings), F (form fields), K/L (links), T (tables), D (landmarks)

---

## 17. Cross-Page Aggregation

For site crawls with multiple pages:

### Structural comparison

Saves `structural_summary.json` per page during capture:
- Navigation link text + order
- Heading hierarchy pattern
- Landmark roles + labels
- Form field names + labels

Aggregator loads summaries and compares:
- **3.2.3**: Navigation link order differs across pages
- **3.2.4**: Same form field labeled differently across pages
- Landmark structure inconsistencies

### Verdict aggregation

Per criterion across all pages:
- 0% fail → Supports
- >0% but <50% fail → Partially Supports
- >=50% fail → Does Not Support

---

## 18. Report Generation

Produces ACR/VPAT reports in multiple formats:
- HTML (for web viewing)
- DOCX (for client delivery)
- XLSX (for spreadsheet review)
- PDF (for formal delivery)

Each format includes:
- Per-criterion conformance level
- Remarks and Explanations (from judge VPAT summary)
- Findings with element location, issue description, impact, affected users

Client-facing exports omit: confidence scores, AI source details, internal reasoning.

---

## 19. Web Application

FastAPI server with:
- WebSocket progress updates during review
- Single-page review, site crawl, multi-URL review
- Review queue (one at a time)
- Resume support (skips already-completed criteria)
- Settings page (model configuration)
- Report viewing and export
- Test detail pages with findings
- Capture browser (screenshots, videos, JSON data)

---

## 20. Batch Processing

`batch_review.py`:
- Reads URLs from xlsx
- Submits one review at a time to the running server
- Polls for completion
- Supports `--skip-completed` for resume
- Configurable WCAG version and level
- Switches backend via settings.json

`send_outreach.py`:
- Reads contacts + scan results
- Builds personalized emails by role (ADA, IT, Legal, Risk)
- Injects actual violation counts and top findings
- Sends via Outlook SMTP

---

## 21. Known Model Behaviors

### Gemini 2.5 Flash Lite (cloud, preferred demo stack)
- Native OpenAI tool_calls, parser cascade rarely fires.
- 1M context window -- no chunking pressure at any practical prompt size.
- Fast: ~4-10s per call on modest prompts, ~20-30s on 30k+ judge prompts with images.
- **Weakness: instruction-following on negative rules.** Flash Lite will
  sometimes ignore "do NOT flag X" guidance in the user prompt even when
  the guidance is clearly present. Rules in the SYSTEM prompt get heavier
  weight. The current architecture puts universal exemption rules
  (`HIDDEN_FROM_AT_RULE`) in the system prompt, SC-specific
  anti-patterns in the per-SC JSON (user prompt), and a
  `PER-CRITERION RULES ARE LAW` directive in the system prompt telling
  the model to weight the user-prompt guidance as authoritative.
- **Weakness: invents plausible-sounding class names and IDs** when
  under-constrained. Observed: `BrandBar-Link` (none in DOM),
  `Nav-Link` (falsely claimed 170 duplicates, actual count 0),
  `#mobile-menu-icon` (selector doesn't exist). The `SELECTOR EVIDENCE`
  rule in the system prompt + `[VERIFIED DOM FACTS]` block in the judge
  user prompt fight this.
- **Weakness: video audio.** Flash Lite processes video frames but not
  audio tracks reliably. Use the deterministic Playwright
  `_probe_autoplay_media` DOM check for SC 1.4.2 / 2.2.2 and layer
  optional cloud AI corroboration only when `AI_VIDEO_*` points at a
  cloud model that actually handles audio (Gemini Flash non-lite, etc.).
- Estimated cost for a 51-SC run on a large-university-sized page: ~$2-3.

### Gemini 2.5 Flash (non-lite, optional upgrade for judge)
- Same context window + tool-call behavior as Lite.
- Noticeably stronger instruction following on "don't do X" rules
  and fewer hallucinated selectors. Trade-off: ~3x the per-call cost
  (~$7-10 per PSU-sized run).
- Swap via `settings.json` `ai_judge_model` / `ai_judge_api_url`
  without changing other routes.

### Qwen3.5-35B (local, 4-bit, via vLLM/mlx-vlm)
- Strong code reasoning and semantic understanding. Good default for
  the code-AI path on HTML chunks.
- Emits findings as OpenAI tool_calls, but with a quirk: `findings` is
  sometimes a **JSON-encoded string** instead of an array. The
  `_normalize_tool_args` helper in `functions/parser.py` detects
  strings starting with `[` or `{` and decodes them recursively.
- Numbers sometimes come back as strings (`"confidence": "0.9"`).
  Normalizer coerces them.
- Empty-args abdication bug: Qwen sometimes returns a `tool_calls` entry
  with `arguments="{}"` while the real analysis is in `content`. The
  parser's `_is_substantive_payload` check rejects empty dicts; the
  5-pass parse cascade then prefers the content path.
- Extended-thinking mode kicks in above ~100k prompt chars and runs the
  model out of output tokens mid-generation. Keep HTML chunks under
  ~25k chars -- see `SECTION_MAX` in `checks/base.py`.
- 128K context window.

### Qwen3-VL-32B (local, 4-bit)
- Good at image analysis + native tool calls.
- Crashes on videos >~1MB (HTTP 500 from the server). Use the video
  describer's ffmpeg chunking to stay small, or route video to a cloud
  model via `AI_VIDEO_*`.
- 32K context for vision.

### Gemma 4 E4B (local, 4-bit)
- Fast multimodal, 4B params.
- **Audio: does NOT process.** The mlx-vlm server hosting E4B strips
  audio tracks from video uploads before inference -- verified with a
  test file containing a 440Hz sine tone in AAC (model reported
  "completely silent"). Do not send audio-bearing video to E4B
  expecting audio analysis; route to cloud video instead.
- **Focus-visible hallucination.** Observed claiming `focus_visible` on
  8 of 17 elements where the `initial` and `focus_1` screenshots were
  byte-for-byte identical. The `_TAB_ORDER_CRITERIA` path in
  `checks/base.py` renders a `[GROUND TRUTH -- DETERMINISTIC TAB WALK]`
  block into the user prompt for SC 2.4.7 / 2.4.11 / 2.1.1 / 2.1.2 /
  2.4.3 that is marked AUTHORITATIVE -- the model is told to defer to
  the deterministic Playwright measurement instead of reading focus
  state from screenshots.
- Tool-call format: native Gemma `<|tool_call>call:name{...}<tool_call|>`
  with `<|"|>` string delimiters. Handled by
  `functions/parser.py:_convert_gemma_quote_pairs` which preserves
  embedded quotes in CSS selectors instead of naively replacing them.
- 128K context window.

### Gemma 4 26B (local, 4-bit)
- Image-bearing calls should route here by default -- set
  `AI_LOCAL_JUDGE_MODEL` = gemma-4-26b. The `_select_model` routing in
  `LLMClient` picks `AI_LOCAL_JUDGE_*` for any call with `has_images=True`.
- Uses the same native `<|tool_call>` format as E4B. Parser handles it.
- Solid reasoning for vision tasks, acceptable latency (~30-60s per
  call on a reasonable image set).
- 128K context window.

### Universal: asyncio.CancelledError handling
`CancelledError` is `BaseException`, not `Exception`. It bypasses the
`except (HTTPStatusError, TimeoutException, RequestError)` clauses in
the LLM client retry loop on purpose -- cancellations must propagate
to the parent task. The retry loop catches it explicitly, logs at
WARNING level with attempt info + target URL, saves the full request
payload to the transcript in the `finally` block (so the cancellation
is visible), then re-raises. Callers that need to ignore cancellation
(like `capture/interactive_capture._probe_autoplay_media`) catch
`CancelledError` separately from `Exception` and decide per-call.

---

## 22. The Check Files — What They Are and Why

### What they are

The `checks/` directory contains ~100 `BaseCheck` subclasses, one per WCAG success criterion, distributed across 30 module files. Each class encodes the domain knowledge for evaluating ONE specific accessibility requirement.

The module layout mirrors the WCAG spec, with versioned variants kept in
sibling files so the import surface stays narrow:

- `checks_1_1.py` … `checks_4_1.py` — base WCAG 2.0/2.1 criteria (14 files)
- `checks_*_aaa.py` — AAA-level companions (11 files: 1.2, 1.3, 1.4, 2.1, 2.2, 2.3, 2.4, 2.5, 3.1, 3.2, 3.3)
- `checks_*_22.py` — WCAG 2.2-only additions (4 files: 2.4, 2.5, 3.2, 3.3)
- `checks_doc.py` — non-web document accessibility (PDF, DOCX, PPTX) for Section 508 reviews
- `checks_cav.py` — caption-and-audio verification (transcribed-vs-spoken comparison for SC 1.2.x)
- `registry.py` + `base.py` — `BaseCheck` and the criterion → class lookup table

Example: `checks/checks_1_1.py` contains `Check_1_1_1` which knows:
- How to detect images without alt attributes (programmatic)
- How to identify suspicious alt text patterns ("image.jpg", pure numbers)
- How to tell decorative images from meaningful ones (heuristic)
- What elements to extract from CaptureData (images, background_images, SVGs, canvas)
- The exact WCAG normative text for SC 1.1.1
- Which DHS Trusted Tester test IDs map to this criterion
- When this criterion is not applicable (no images on page)
- What WCAG versions it applies to (2.0, 2.1, 2.2)

### Why they exist separately from the AI

The AI is good at interpreting screenshots and understanding context, but it doesn't know WCAG rules. The check files encode:
1. **What to check** — which DOM attributes, which elements, which patterns
2. **What constitutes a pass** — the exact structural conditions
3. **What constitutes a fail** — missing attributes, invalid values, structural violations
4. **What's not applicable** — when the criterion doesn't apply to this page
5. **Trusted Tester mapping** — which federal test procedures correspond

### Why they can't be recreated easily

Each check file contains hundreds of lines of accessibility domain knowledge that was built from:
- WCAG 2.0/2.1/2.2 normative requirements
- DHS Trusted Tester v5.1 methodology
- Section 508 conformance requirements
- Real-world testing experience (what actually breaks for AT users)

### How they integrate

Every check class follows the same pattern:
```python
class Check_X_Y_Z(BaseCheck):
    criterion_id = "X.Y.Z"
    criterion_name = "Criterion Name"
    level = "A"  # or "AA" or "AAA"
    wcag_versions = ["2.0", "2.1", "2.2"]
    normative_text = "The exact WCAG requirement text"
    web_only = False  # True for keyboard/interactive checks

    def is_applicable(self, capture_data) -> bool:
        # Return False if no relevant content on page
        return bool(capture_data.images)

    async def run_programmatic(self, capture_data):
        # Deterministic DOM checks
        # Returns: (ConformanceLevel, confidence, list[Finding])
        findings = []
        for img in capture_data.images:
            if not img.get("alt"):
                findings.append(Finding(...))
        return ConformanceLevel.DOES_NOT_SUPPORT, 0.9, findings
```

The `BaseCheck.execute()` method in `base.py` handles the pipeline orchestration — it calls `run_programmatic()`, then visual AI, code AI, AT sim, and the judge. Individual check files only need to implement `run_programmatic()` and optionally `is_applicable()`.

### The prompt templates (prompts/*.json)

Each check has a companion JSON file in `prompts/` with:
```json
{
  "criterion_id": "1.1.1",
  "visual_checks": ["Look at each image...", "Check if alt text matches..."],
  "pass_conditions": ["All meaningful images have descriptive alt text"],
  "fail_conditions": ["Images missing alt attribute entirely"],
  "na_conditions": ["Page contains no images"],
  "common_mistakes": ["Reporting decorative images as failures"],
  "source_guidance": "Visual AI is the expert for alt text QUALITY"
}
```

These templates feed into the visual AI prompt, code AI prompt, and judge prompt — giving the AI specific guidance for each criterion.

---

## 23. Error Handling & Debugging

### Every AI call MUST be wrapped in try/except

```python
try:
    result = await client.call_with_tools(...)
except Exception as exc:
    logger.warning("SC %s: visual AI failed: %s", criterion_id, exc)
    # Continue with other sources — don't crash the pipeline
```

### Every AI response MUST be saved to disk

For every criterion test, save:
- `visual_ai_response.json` — raw AI output
- `code_ai_response.json` — raw code AI output
- `judge_response.json` — judge's decision
- `judge_dom_context.txt` — what DOM facts the judge received
- `prompt.txt` — the visual AI prompt (for debugging)

This lets you audit EXACTLY what went wrong when a result is incorrect.

### Capture completions tracking

Every interactive test records its status:
```python
completions[test_name] = "ok" | "timeout" | "error"
capture_data.capture_completions = completions
```

Checks consult this to report NOT_EVALUATED instead of false SUPPORTS when data is missing.

### Research errors before guessing

When building this system, if you encounter:
- An API returning unexpected format → **test it manually** with a real call, print the raw response
- A model not following tool call instructions → **check which model/backend is active**, test that specific model
- A finding being dropped → **check the type** (dict vs Finding vs str), trace through the parser
- A criterion getting wrong results → **read the judge_response.json** and the DOM context to see why

NEVER assume. ALWAYS test. Print the raw response. Check the type. Read the logs.

---

## 24. Testing & Validation

### Unit tests for the functions/ folder

Every function in `functions/` must have tests:
- `test_parse_tool_response` — test all 4 response formats
- `test_clean_tool_call_args` — test Gemma control token cleaning
- `test_chunk_elements` — verify no elements are lost
- `test_chunk_html_by_landmarks` — verify all HTML is covered
- `test_encode_image` — verify data URI format
- `test_build_dom_context` — verify all CaptureData fields are represented

### Integration tests

- Test a real LLM call to each backend (Gemini, local Qwen, local Gemma)
- Test video encoding + sending to Gemini
- Test image encoding + sending to local vision model
- Test tool call round-trip: send schema → get response → parse → verify structure

### End-to-end test

Run the full pipeline on a known test page:
1. Capture completes without errors
2. All interactive tests record completion status
3. Video descriptions are generated
4. Each check produces a conformance level
5. Judge produces findings with element locations
6. Report generates correctly

### The test_backends.py script

Keep a test script that calls each model endpoint with a real prompt and verifies:
- Text model returns tool call ✓
- Vision model returns tool call with image ✓
- Video model returns prose description ✓
- Judge model returns structured judgment ✓
- Explorer model returns exploration result (possibly via Layer 2) ✓

Run this after ANY change to the functions/ folder or config.

---

## 25. Integration Guide — How Everything Connects

### Data flow through the system

```
settings.json
    ↓ (read by)
config.py
    ↓ (imported by)
functions/llm.py  ←── THE central hub
    ↓ (used by)
┌─────────────────────────────────────────┐
│ capture/v2/phase2_explorer.py           │
│ capture/video_describer.py              │
│ analysis/judge.py                       │
│ analysis/api_client.py (→ checks)       │
│ crawl/page_selector.py                  │
│ capture/v2/phase1_inventory.py          │
│ capture/v2/phase3_segments.py           │
│ capture/interactive.py (audio detect)   │
│ verification/verifier.py                │
└─────────────────────────────────────────┘
    ↓ (all responses parsed by)
functions/parser.py  ←── THE central parser
```

### How a single SC check flows

```
app.py calls check.run(capture_data, ai_client)
    ↓
checks/base.py BaseCheck.run()
    ↓
BaseCheck.execute()
    ├── run_programmatic()         → uses capture_data directly (no AI)
    ├── _extract_axe_findings()    → filters capture_data.axe_results
    ├── [FAST PATH if definitive]  → judge VPAT synthesis → done
    ├── run_ai_analysis()          → ai_client.analyze() → functions/llm.py → functions/parser.py
    ├── run_code_analysis()        → ai_client.analyze() → functions/llm.py → functions/parser.py
    ├── AT simulation              → at_simulation/screen_reader.py (no AI)
    ├── _deduplicate_findings()
    ├── judge_criterion()          → functions/llm.py → functions/parser.py
    └── _adjust_confidence_and_flag_review()
```

### How the web app connects

```
Browser → app.py (FastAPI)
    ├── POST /review/start → creates ReviewMeta → queues review_id
    ├── queue_worker() → process_review(review_id)
    │   ├── capture_web_page()           → capture pipeline
    │   ├── describe_all_videos()        → video-to-text
    │   ├── for check in checks:         → check pipeline
    │   │   └── check.run()              → see above
    │   ├── generate_acr_report()        → report generation
    │   └── broadcast(progress updates)  → WebSocket to browser
    └── GET /review/{id}/report → serves HTML report
```

### How batch processing connects

```
batch_review.py
    ├── Reads xlsx (URLs + institution names)
    ├── switch_backend("vllm")           → updates settings.json
    ├── For each URL:
    │   ├── POST /review/start           → to running server
    │   ├── Poll /api/review/{id}/status → wait for completion
    │   └── Log progress
    └── Summary

send_outreach.py
    ├── Reads xlsx (contacts + roles)
    ├── Loads scan results from reviews/
    ├── Builds personalized emails by role
    └── Sends via SMTP
```

### File dependencies (what imports what)

```
functions/llm.py      imports: config, functions/media, functions/parser
functions/parser.py   imports: models (Finding, Severity, ConformanceLevel)
functions/chunker.py  imports: nothing (pure utility)
functions/media.py    imports: nothing (pure utility)
functions/tools.py    imports: nothing (pure constants)
functions/prompt.py   imports: models, prompts/

checks/base.py        imports: functions/*, models, analysis/judge, at_simulation/*
checks/checks_*.py    imports: checks/base, models

analysis/judge.py     imports: functions/llm, functions/parser, functions/tools, config
analysis/api_client.py imports: functions/llm, functions/parser, functions/tools

capture/*.py          imports: functions/llm (for AI phases), models, config
```

No circular dependencies. `functions/` is at the bottom of the dependency tree.

---

## 26. AI Prompts — Complete Reference

### Philosophy (post-refactor, 2026-04-15)

Every system prompt in this codebase is **minimal and universal**. No
SC-specific rules live in a system prompt. Per-criterion behavior is
stored in ``prompts/<id>.json`` and rendered into the USER prompt as a
CRITERION GUIDANCE block. The system prompt points at that block and
tells the model to treat it as law.

**Why this split:**

- **System prompt = stable across every call.** Only parameterized
  values (criterion_id, name, level, normative text) change per-SC.
  Universal rules (role, task, finding requirements, selector
  evidence, response format) are the same bytes every time.
- **SC-specific rules live next to the criterion they describe.**
  Editing SC 1.1.1's handling of SVG `<title>` elements is a JSON
  edit to ``prompts/1_1_1.json``, not a code change.
- **The model is told exactly where the rules are.** The system prompt
  has a "PER-CRITERION RULES ARE LAW" directive telling the model to
  obey the guidance in the user prompt over its general training.

### Inventory of every prompt in the system (16 total)

| # | Prompt | Location | Purpose | Size (chars) |
|---|--------|----------|---------|-------------:|
| 1 | `build_system_prompt` | `functions/prompt.py` | Per-SC visual/code AI system prompt. Universal. | ~2,500 |
| 2 | Per-SC CRITERION GUIDANCE | `prompts/<id>.json` rendered by check pipeline | Pass/fail conditions, anti-patterns, off-scope topics, examples. | varies per SC |
| 3 | `JUDGE_SYSTEM_PROMPT` | `analysis/judge.py` | Final arbiter. Universal. Self-contained (no appended rule blocks). | ~2,200 |
| 4 | `_judge_vpat_synthesis` inline | `analysis/judge.py` | Fast-path judge for programmatic-definitive criteria (axe, dupe IDs). | ~1,400 |
| 5 | Code-AI per-chunk system prompt | `checks/base.py:2119` | Text-only HTML analysis, one landmark chunk at a time. | ~1,500 |
| 6 | Enrichment (VPAT remarks) system prompt | `checks/base.py:3288` | Rewrites findings into ACR Remarks language. Minimal. | ~500 |
| 7 | `_AUDIT_SYSTEM_PROMPT` | `capture/v2/phase1_code_analysis.py` | Phase 1 element inventory audit. | ~1,900 |
| 8 | Phase 2 visual explorer | `capture/v2/phase2_visual_explorer.py` | Screenshot-diff interaction analysis with explicit `interaction_response` enum and per-item selector rules. | ~2,100 |
| 9 | Phase 3 video segments | `capture/v2/phase3_video_segments.py` | Video segment planning. Minimal. | ~200 |
| 10 | `_VIDEO_SYSTEM_PROMPT` | `capture/video_describer.py` | Per-chunk video description with rolling context hand-off between chunks. | ~300 |
| 11 | `_SYSTEM_PROMPT` | `functions/audio_probe.py` | Optional cloud AI corroboration for SC 1.4.2 / 2.2.2 audio detection. | ~250 |
| 12 | `ai_classify` classify + classify_batch | `analysis/ai_classify.py` | Quick yes/no/uncertain classification decisions. | ~300 each |
| 13 | Video prose fallback | `analysis/api_client.py` | Retry video call without tool requirement when structured call fails. | ~400 |
| 14 | Site analysis | `crawl/page_selector.py` | Summarize a crawled site for product context. | ~250 |
| 15 | `SYNTHESIS_SYSTEM_PROMPT` | `analysis/synthesis.py` | Cross-SC executive summary + VPAT remarks. Template-driven. | ~2,100 |
| 16 | Prose restructurer | `functions/llm.py:597` | Internal cascade fallback: convert a prose LLM reply into a structured tool call. | ~350 |

### Structure of a finding-producing system prompt (1, 3, 5, 7)

The four prompts that produce WCAG findings (per-SC visual/code AI, main
judge, vpat-synthesis judge, per-chunk code AI) all share the same minimal
structure. They differ only in role framing and one or two extra rules:

```
ROLE
You are a WCAG {version} Level {level} accessibility auditor.
You are producing findings for a VPAT 2.5 Section 508 ACR.

CRITERION UNDER TEST
- ID: {criterion_id}
- Name: {criterion_name}
- Level: {level}
- Normative text: {normative_text}

TASK
Evaluate the page ONLY against WCAG {criterion_id}. Do NOT evaluate
any other criterion. If you notice an issue that belongs to a
different SC, IGNORE it. Never redirect a finding to another criterion.

PER-CRITERION RULES ARE LAW
The user prompt contains a CRITERION GUIDANCE block with this SC's
pass_conditions, fail_conditions, auditor_anti_patterns, and
off_scope_topics. Treat that block as authoritative law:
  - A finding is valid ONLY if it matches a fail_condition.
  - If a finding matches an auditor_anti_pattern, DROP it.
  - If a finding matches an off_scope_topic, DROP it.
  - When your training conflicts with the guidance, OBEY the guidance.

FINDING REQUIREMENTS (every finding needs all of these)
  - element: the specific element with a CSS selector or spatial locator.
  - issue: what is wrong, citing the WCAG clause that is unmet.
  - impact: which disability group and which assistive technology.
  - recommendation: the WCAG requirement. No code fixes.
  - severity: high / medium / low / info.

SELECTOR EVIDENCE (strict)
Every CSS selector in a finding MUST appear verbatim in the HTML,
programmatic data, or ground-truth blocks in the user prompt. Do NOT
invent class names or IDs.

RESPONSE FORMAT
Call the {tool_name} tool exactly once. No prose, no markdown.
```

That's the universal shape. The judge prompts add "0 findings = Supports"
verdict rules, the code-AI prompt adds "analyze only the HTML section
shown", but the bones are identical.

### Structure of a per-SC CRITERION GUIDANCE block (JSON field → user prompt text)

```jsonc
// prompts/1_1_1.json
{
  "criterion_id": "1.1.1",
  "criterion_name": "Non-text Content",
  "pass_conditions": [ "..." ],           // rendered as "SUPPORTS (pass) when: ..."
  "fail_conditions": [ "..." ],           // rendered as "DOES NOT SUPPORT (fail) when: ..."
  "na_conditions":   [ "..." ],           // rendered as "NOT APPLICABLE when: ..."
  "common_mistakes": [ "..." ],           // rendered as "COMMON MISTAKES: ..."
  "auditor_anti_patterns": [ "..." ],     // rendered as "AUDITOR ANTI-PATTERNS (do NOT report these false positives): ..."
  "off_scope_topics": [ "..." ],          // rendered as "OFF-SCOPE TOPICS FOR 1.1.1 (REJECT): ..."
  "examples": { "pass": "...", "fail": "...", "partial": "..." },
  "visual_checks": [ "..." ]              // rendered as "WHAT TO LOOK FOR: ..." (visual AI only)
}
```

**SCs that currently have `auditor_anti_patterns` populated:**

| SC | Count | What they cover |
|---|---|---|
| 1.1.1 Non-text Content | 6 | FontAwesome aria-hidden icons, SVG `<title>`, aria-labelledby → title, parent button covers child icon |
| 1.3.1 Info and Relationships | 4 | ARIA landmark roles, table scope/headers, role=list pseudo-lists, aria-level headings |
| 1.4.1 Use of Color | 1 | Visually-hidden text isn't rendered -- not a contrast concern |
| 1.4.3 Contrast (Minimum) | 1 | Same -- visually-hidden / sr-only |
| 1.4.6 Contrast (Enhanced) | 1 | Same |
| 1.4.8 Visual Presentation | 1 | Same |
| 1.4.11 Non-text Contrast | 1 | Same |
| 2.2.2 Pause, Stop, Hide | 4 | Muted autoplay video compliant, CSS animation belongs to 2.3.3, keyboard belongs to 2.1.1 |
| 2.4.4 Link Purpose (In Context) | 4 | Link name mechanisms, SVG title as name, aria-hidden child icon, context-from-paragraph |
| 2.4.7 Focus Visible | 4 | Defer to deterministic tab_walk.has_visible_indicator, don't read focus from static full-page screenshot |
| 2.5.3 Label in Name | 4 | No visible label = N/A, "starts with" rule, same-element aria-labelledby, input value matching |
| 3.3.2 Labels or Instructions | 4 | Label mechanisms, proximate visible instructions, hidden/submit inputs, visually-hidden search label |
| 4.1.2 Name, Role, Value | 6 | All W3C ARIA 1.2 name sources, parent name covers icon, SVG title, implicit HTML roles, FontAwesome icons, non-interactive wrappers out of scope |

### Ground-truth blocks that render into the user prompt

In addition to CRITERION GUIDANCE, the check/judge pipelines inject
deterministic ground-truth blocks for criteria that have them:

| Block | Populated by | Consumed by |
|---|---|---|
| `[GROUND TRUTH -- DETERMINISTIC TAB WALK]` | `capture_data.tab_walk` from Playwright | SC 2.1.1, 2.1.2, 2.4.3, 2.4.7, 2.4.11 |
| `[GROUND TRUTH -- AUTOPLAY MEDIA PROBE (DETERMINISTIC)]` | `capture_data.audio_detection` from Playwright JS probe in `interactive_capture._probe_autoplay_media` | SC 1.4.2, 2.2.2 |
| `[VERIFIED DOM FACTS]` | `_build_dom_context` in `checks/base.py` | Judge (all SCs) |
| `[PROGRAMMATIC DATA]` | per-SC programmatic check results (axe, attribute checks) | Visual/code AI (all SCs) |

All of these are marked AUTHORITATIVE in the prompt text so the model
defers to them instead of guessing from screenshots.

### Rolling context in video-chunk prompts

When `video_describer._split_video_to_chunks` cuts a long observation
video into ~60s pieces, each piece's prompt receives a
`CONTEXT FROM PREVIOUS CHUNKS` block containing the full descriptions
of every prior chunk in sequence. This keeps narrative continuity
across chunks (tab position, menu state, focused element) without
truncation. See `capture/video_describer.py:_describe_single_video`.

### Prompt backup convention

Every prompt refactor saves a full copy of the edited files into
``prompts/_backup_<timestamp>/<path>`` before making changes, so any
removed text can be restored with a single ``cp`` command if a future
run exposes a regression. The latest backup sits at
``prompts/_backup_20260415_182840/``.

---

## 27. Rules — Non-Negotiable

1. **ONE LLM call function** (`LLMClient.call()` and `call_with_tools()`). Nothing else makes HTTP calls to LLMs.

2. **ONE response parser** (`parse_tool_response()`). Nothing else parses tool calls.

3. **NO truncation** of data. No `[:100]`, no `[:500]`, no "first 50 elements". Everything is seen. If it's too large, CHUNK it.

4. **NO hardcoded Gemini URLs** in code. All endpoints come from config/settings.

5. **NO `isinstance(ff, dict)` without also handling `Finding` objects**. The judge may return either.

6. **ALL findings in ACR format**: element location, CSS selector, WCAG issue, impact on users, affected assistive technologies. NO remediation guidance.

7. **ALL AI calls go through `functions/llm.py`**. NO raw httpx calls anywhere else.

8. **ALL response parsing goes through `functions/parser.py`**. NO ad-hoc JSON parsing anywhere else.

9. **SAVE debug artifacts**: judge_response.json, judge_dom_context.txt, visual_ai_response.json, code_ai_response.json, video_descriptions.json, axe_results.json, structural_summary.json.

10. **EVERY interactive test records completion status** in `capture_completions`. Checks use this to report NOT_EVALUATED instead of false SUPPORTS when data is missing.

11. **The judge receives FULL DOM context** built from structured Playwright data. If the judge can't verify a claim, add the data source to `_build_dom_context`.

12. **System prompts contain ONLY universal rules.** Anything SC-specific (pass/fail conditions, anti-patterns, off-scope topics, examples) lives in `prompts/<id>.json` and is rendered into the USER prompt as a CRITERION GUIDANCE block by the check/judge pipelines. Editing SC behavior must be a JSON edit, not a code change. If you're about to put `if criterion_id == "1.1.1"` in a system prompt builder, STOP and put the text in `prompts/1_1_1.json` instead.

13. **Every prompt refactor saves a backup.** Before editing any prompt file, copy the full current version to `prompts/_backup_<timestamp>/<path>`. This way any removed text can be restored with a single `cp` if a future run exposes a regression. The most recent backup is the source of truth for what was removed and when.

14. **CancelledError must be caught explicitly and re-raised.** `asyncio.CancelledError` is `BaseException`, not `Exception`. The LLM retry loop catches it in its own handler, logs it loudly, saves the request payload to the transcript in the `finally` block, then re-raises. Any caller that wraps LLM calls in `try/except Exception` is hiding cancellations -- add a separate `except CancelledError: raise`.

15. **Chunked LLM calls must preserve all content across chunks.** When HTML is split for the code-AI pipeline or a video is split for the describer, the prompt must (a) tell the model which chunk it is and how many total, (b) pass rolling context from prior chunks when continuity matters (see `video_describer` rolling context), and (c) never drop or summarize chunk content to save tokens. If a chunk genuinely needs a summary (e.g. prior chunks for a very long video), the summary must be compression-style, not truncation-style.

16. **Video is described once, consumed as text everywhere.** No raw video in check calls.

17. **No function should exceed 100 lines.** If it does, break it into smaller functions in the functions/ folder.

18. **This is a production application.** Every error is logged. Every phase has timing. Every AI call is saved to disk. Every finding has a source attribution.

19. **Evidence issues are saved separately and completely.** When the model returns `insufficient_evidence_reason` or `conflicting_information`, a dedicated file `tests/<sc>/evidence_issue_<source>.json` is written containing the FULL system prompt, FULL user prompt, and FULL model response — no truncation. This makes it trivial to find and diagnose cases where the model couldn't evaluate a criterion. These are two distinct situations with different remediation actions (see Section 28).

## 28. Evidence Issue Tracing — Insufficient Data vs Conflicting Data

The WCAG assessment tool schema includes two optional fields that give the model
a structured way to signal problems instead of hallucinating findings:

### Two distinct situations

**1. `insufficient_evidence_reason` — "I don't have enough data"**

The model sets `conformance_level: "Not Evaluated"` and explains what evidence
is missing. This means the capture pipeline or prompt isn't providing enough
information for the model to make a determination.

- **Remediation**: Add more data to the prompt or capture pipeline. Check what
  the model says it needs and see if we can provide it.
- **Example**: "No tab walk data was provided to verify keyboard accessibility"
  or "Screenshots don't show the focused state for comparison."

**2. `conflicting_information` — "The data contradicts itself"**

The model describes where programmatic data and visual evidence disagree, or
where different data sources give conflicting signals. This means there may be
a bug in the capture pipeline or the prompt is presenting confusing information.

- **Remediation**: Investigate the prompt and data sources. Check if the
  programmatic data is stale, if screenshots don't match the DOM, or if
  the prompt framing is misleading.
- **Example**: "The element inventory shows alt='descriptive text' for this
  image but I cannot see any image content in the screenshot at this location"
  or "The tab walk marks this element as VISIBLE but the focus screenshots
  show no visual change between unfocused and focused states."

### Where evidence issues are saved (three layers, all full, no truncation)

| Layer | File | Contents |
|-------|------|----------|
| **LLM transcript** | `llm_transcripts/NNNNN_report_wcag_assessment.json` | Full request payload (system prompt, user prompt, images, tool schema) + full raw response. Written for EVERY call. |
| **Per-SC dedicated file** | `tests/<sc>/evidence_issue_<source>.json` | Full system prompt, full user prompt, full model response, plus the model's explanation. ONLY written when the model flags an issue. |
| **Console log** | — | `EVIDENCE[tool] INSUFFICIENT DATA — ...` or `EVIDENCE[tool] CONFLICTING DATA — ...` at WARNING level. |

### How to investigate

1. Check console logs for `EVIDENCE[` prefixed warnings
2. List flagged SCs: `find reviews/<id>/tests -name "evidence_issue_*.json"`
3. Open the evidence issue file — it has the full prompt and full response
4. The `issue_type` field tells you which situation: `insufficient_evidence` or `conflicting_information`
5. The model's explanation tells you what to fix

### System prompt guidance

The system prompt (`functions/prompt.py`) instructs the model:
- ALWAYS trust programmatic data over visual interpretation of screenshots
- If data conflicts: set Not Evaluated + fill `conflicting_information`
- If data is missing: set Not Evaluated + fill `insufficient_evidence_reason`
- Do NOT fabricate findings to fill a gap in evidence

### Finding deduplication

When the model produces many near-identical findings (e.g. 68 "missing alt text"
findings for different elements), the deduplication system in
`checks/base.py:_deduplicate_findings` runs two passes:

1. **Element+issue dedup** (existing): merges findings with identical normalized
   element selector AND normalized issue text.
2. **Issue-type dedup** (new): groups findings by normalized issue text alone.
   When 3+ findings share the same issue, keeps up to 3 highest-severity
   exemplars and annotates the first with the total count. This prevents
   per-element over-counting from flooding the judge while preserving the
   full issue text and element descriptions on the kept exemplars.
