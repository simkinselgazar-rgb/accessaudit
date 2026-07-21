# AccessAudit — Architecture

> Operational rules and coding conventions live in `CLAUDE.md`. This document
> describes the system's structure, data flow, and design decisions.
>
> Full technical reference (sections on the default model fleet, programmatic
> fast path, dynamic confidence, AT simulation details, known model behaviors,
> and integration guide): [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md)

---

## 1. Overview

AccessAudit is a multi-source accessibility auditing system that
combines deterministic analysis, AI vision, AI code review, assistive
technology simulation, and a consolidating judge to produce Accessibility
Conformance Reports (ACR/VPAT) against WCAG 2.x and Section 508.

The system is a single FastAPI process. Reviews run as async tasks behind an
in-process queue. A WebSocket channel streams progress to the browser UI. All
AI calls go through one centralized client (`functions/llm.py`).

---

## 2. Directory Structure

```
wcag-tester-v6/
├── app/                  # FastAPI app, queue, orchestrators, WebSocket manager
├── capture/              # Page capture pipelines (v1 legacy + v2 current)
│   └── v2/               # Current capture pipeline (phases 1–4)
├── checks/               # WCAG success criterion checks (one file per SC group)
├── functions/            # Shared utilities (LLM client, parser, tools, prompt builders, etc.)
├── analysis/             # Judge, final reviewer, caption verifier, synthesis
├── at_simulation/        # Screen reader + keyboard AT simulation
├── crawl/                # Site crawler + AI page selector
├── verification/         # Optional post-judge verification pass
├── report/               # ACR/VPAT generators (PDF, DOCX, XLSX)
├── storage/              # Review directory management + meta/result I/O
├── guidelines/           # WCAG SC definition data
├── prompts/              # Prompt templates
├── static/               # Web UI assets
├── templates/            # Jinja2 HTML templates
├── tests/                # Test suite (18 modules)
├── config.py             # Configuration loader (env > settings.json > defaults)
├── models.py             # Data models (Finding, TestResult, CaptureData, ReviewMeta)
├── run.py                # Entry-point launcher (venv setup, dependency install, uvicorn)
└── settings.json         # Active settings (AI backend, model names, API keys)
```

---

## 3. Configuration

**Resolution order (first match wins):**
1. Environment variable (prefixed `WCAG_`)
2. `settings.json` key
3. Built-in default in `config.py`

**Supported AI backends:** `vllm` | `gemini` | `openai` | `anthropic` | `openrouter`

Each backend has sane model defaults in `_BACKEND_DEFAULTS`. Override any
individual model role by setting the corresponding key in `settings.json`:

| Role | Key | Purpose |
|---|---|---|
| Main text/vision model | `ai_model` | SC checks, code AI, crawl page selection |
| Vision model | `ai_vision_model` | Image-heavy calls |
| Judge model | `ai_judge_model` | Final verdict arbiter per SC |
| Reviewer model | `ai_reviewer_model` | Pro-tier ACR review pass |
| Video model | `ai_video_model` | Video description + observation |
| Explorer model | `ai_explorer_model` | Phase 2 visual exploration (fast multimodal) |
| Local judge model | `ai_local_judge_model` | Image calls on local vLLM fleet |

**Concurrency:** `ai_max_concurrent` defaults to `1` (serial). Set to `10`+ for
cloud backends. Never set above `1` for local vLLM — it will OOM.

**Rate limiting:** `ai_rpm` sets requests-per-minute. Defaults to `10` for
Gemini backend; `0` (no limit) for others.

---

## 4. Data Flow

### 4.1 Single-page review

```
POST /api/start
  └─ create_review() → review_dir, meta.json
  └─ queue.enqueue(review_id)

queue_worker()
  └─ orchestrators.process_review(review_id)
       │
       ├─ Capture phase (capture/v2/orchestrator.py)
       │    ├─ Phase 1: Code analysis (page HTML + JS → SC-tagged code patterns)
       │    ├─ Phase 2: Visual exploration (screenshot → page context, element inventory)
       │    ├─ Phase 3: Video segments (observation video → accessibility events)
       │    └─ Phase 4: AT simulation (keyboard, screen reader, focus, forms)
       │
       ├─ Testing phase (checks/registry.py discovers and runs all enabled checks)
       │    └─ For each SC:
       │         ├─ checks/base.py:BaseCheck.run()
       │         │    ├─ Programmatic checks (deterministic)
       │         │    ├─ Visual AI (screenshot analysis)
       │         │    ├─ Code AI (functions/code_analyzer.py cache)
       │         │    ├─ AT simulation findings
       │         │    └─ Judge call (analysis/judge.py) → consolidated TestResult
       │         └─ validate_source_attribution() → demote mislabeled findings
       │
       ├─ Aggregation (crawl/aggregator.py for multi-page)
       │
       └─ Report generation (report/acr_generator.py → PDF/DOCX/XLSX)
```

### 4.2 Site review

```
POST /api/start (with crawl_enabled=true)
  └─ orchestrators.process_site_review()
       ├─ crawl/site_crawler.py  → page URLs
       ├─ crawl/page_selector.py → AI selects representative subset
       └─ [single-page review] × N pages  →  crawl/aggregator.py
```

---

## 5. LLM Client (`functions/llm.py`)

**The single gateway for every AI call in the system.** No module other than
`functions/llm.py` opens a `/chat/completions` connection.

### 5.1 Model routing (priority order)

1. Per-call `model_override` / `endpoint_override` (internal use only)
2. Audio-bearing video → `AI_EXPLORER_*` (local, processes audio track)
3. Any video → `AI_VIDEO_*`
4. Any image → `AI_LOCAL_JUDGE_*` (accuracy over speed; see CLAUDE.md).
   The localhost fleet defaults only apply on the `vllm` backend; on
   cloud backends the local-fleet URLs default to empty and image calls
   fall through to the configured vision endpoint.
5. Text-only → `self.model` / `self.base_url`

### 5.2 `call_with_tools` cascade (structured output)

Every structured-output call uses this strategy:

```
attempt 1: send prompt + tool schema, try to parse
attempt 2: resend with corrective note showing the model its rejected reply
attempt 3: same
→ if all fail and last response has prose:
    route prose to AI_FALLBACK_MODEL with "convert to tool call" instruction
→ if restructure also fails: return None, log bypass event
```

Parse paths attempted (in order by parser):
- OpenAI native tool_calls format
- Gemma native `<tool_call>` XML format
- Qwen native `✿FUNCTION✿` format
- Freeform JSON extraction (state-machine `loose_json_loads`)

### 5.3 Transcript logging

Every `LLMClient.call()` invocation writes a file to
`<review_dir>/llm_transcripts/NNNNN_<label>.json` containing:
- Full request payload (system prompt, user prompt, base64 media, tool schemas)
- Full raw response
- A text-only `summary` sidecar for human skimming

Error calls write `NNNNN_<label>_ERROR.json` with the exception recorded in
`error.type` / `error.message`. The prompt is always saved even when the model
never replies.

---

## 6. Checks Architecture (`checks/`)

### 6.1 BaseCheck (`checks/base.py`)

All SC checks inherit `BaseCheck`. A check's `run()` method:
1. Builds DOM context (`_build_dom_context`) — includes measurement evidence
   blocks for SCs with deterministic rules (target size, contrast, etc.)
2. Runs programmatic checks — deterministic pass/fail
3. Calls Visual AI, Code AI, AT simulation
4. Calls the judge (`analysis/judge.py`) with all findings + DOM context
5. Validates source attribution (`functions/parser.py:validate_source_attribution`)
6. Returns `TestResult`

### 6.2 DOM context construction

`_build_dom_context` is the most important prompt-quality lever in the system.
It assembles:

- Page URL, title, language
- Heading hierarchy
- Landmark structure (with `[BROWSER-HANDLED]` markers for hidden content)
- Full element inventory for the SC being tested
- Measurement evidence blocks (for SCs with numeric pass/fail thresholds)
- ANDI audit results (contrast, language, images, interactive elements)
- Axe + HTML_CodeSniffer corroborating findings
- Tab walk data (keyboard order, focus states)

**The measurement evidence block pattern:** SCs with deterministic criteria
(e.g. SC 2.5.8 target size, SC 1.4.3 contrast) include a labeled block
listing every relevant measurement + PASS/FAIL verdict per WCAG rules. The
judge prompt instructs the model to reject any finding that contradicts these
numbers. This prevents hallucination — see CLAUDE.md "Don't blame hallucination
without reading the prompt."

### 6.3 Source attribution

Every `Finding` has a `source` field:

| Tag | Meaning |
|---|---|
| `programmatic` | Deterministic check (duplicate IDs, parsing errors, etc.) |
| `axe` | axe-core engine |
| `andi` | ANDI-style audit (contrast, language, graphics, interactive) |
| `visual_ai` | Screenshot analysis by AI |
| `code_ai` | Code analysis by AI |
| `at_sim` | AT simulation (keyboard nav, screen reader) |
| `judge_inference` | Judge inferred this from evidence — no input source measured it |
| `cross_page` | Multi-page consistency check (`crawl/aggregator.py`) — injected after judging, bypasses the validator by design |

A merged finding may carry several tags comma-separated (e.g.
`axe, htmlcs, ibm_eac`) when multiple sources corroborated the same issue.

`validate_source_attribution` runs after every judge call. It demotes findings
whose claimed source has no matching input finding (by selector/element/text
overlap) to `judge_inference`. The demotion count is appended to
`confidence_reasoning`. Source tags are load-bearing — auditors read them to
know whether a finding was measured or inferred.

---

## 7. Finding Deduplication

`functions/finding_dedup.py` uses BGE-M3 embeddings (via the configured
`EMBEDDINGS_API_URL`) to cluster semantically identical findings across pages.
Used for:
- SC 3.2.3 / 3.2.4 consistency checks (cross-page nav/label comparison)
- Multi-page reviews: deduplicate "missing alt on logo" appearing 20× → one
  finding with 20 affected URLs

---

## 8. Storage Layout

```
reviews/
└── {review_id}/          # e.g. 20260512_143022_abc123
    ├── meta.json          # ReviewMeta (URL, status, WCAG version, etc.)
    ├── captures/
    │   ├── dom.html       # Raw captured page HTML
    │   ├── full_page.png  # Full-page screenshot
    │   ├── viewport.png   # Viewport screenshot
    │   ├── a11y_tree.json # Accessibility tree
    │   ├── tab_walk.json  # Keyboard tab order data
    │   ├── axe_results.json
    │   └── ...
    ├── tests/
    │   └── {sc_id}/       # e.g. 1_1_1/
    │       ├── result.json
    │       ├── judge_dom_context.txt
    │       └── ...
    ├── report/
    │   ├── acr_report.html / .json          # internal report
    │   ├── acr_report_client.html / .json   # client-mode (internals stripped)
    │   └── acr_report.pdf / .docx / .xlsx   # exports (generated on demand)
    ├── synthesis.json     # Cross-criterion synthesis (exec summary, VPAT remarks)
    ├── bypass_log.jsonl   # Data-loss / fallback telemetry
    └── llm_transcripts/   # One file per LLM call (full prompts + responses)
```

---

## 9. Capture Pipelines

### 9.1 V2 pipeline (current, `capture/v2/`)

**Phase 1 — Code analysis**
- Downloads page HTML + linked scripts
- Runs Code AI to tag patterns by SC (event handlers, ARIA patterns, form logic)
- Results cached per page and reused by all checks that request code analysis

**Phase 2 — Visual exploration**
- Screenshots at 100%, 200%, and 320px viewport
- AI describes page type, purpose, key UI regions
- Builds element inventory (interactive elements, images, headings, forms)

**Phase 3 — Video segments**
- Records a 60s observation video
- Extracts frames at configurable FPS
- AI interprets accessibility-relevant events (animations, popups, focus changes)

**Phase 4 — AT simulation**
- Playwright-driven keyboard navigation roundtrip (Tab, Shift+Tab, Enter, Escape)
- Screen reader announcement simulation (computes accessible names, roles, states)
- Focus indicator visibility checks
- Form interaction simulation (error message capture)

### 9.2 V1 pipeline (legacy, `capture/web_capture.py`)

Uses the same Playwright browser but captures in a single combined phase.
Enabled by setting `capture_pipeline: v1` in `settings.json`.

---

## 10. Report Generation

`report/acr_generator.py` produces an Accessibility Conformance Report
(VPAT 2.5 format). Exporters:

- `report/pdf_exporter.py` — ReportLab-based PDF
- `report/docx_exporter.py` — python-docx DOCX
- `report/xlsx_exporter.py` — openpyxl XLSX

The final reviewer (`analysis/final_reviewer.py`) runs once per review on
the complete ACR data to catch verdict contradictions, miscalibrated
confidence, citation errors, and prose-tone issues before export.

---

## 11. API Endpoints

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/` | Web UI (dashboard) |
| `POST` | `/review/start` | Create and queue a single-page/file review |
| `POST` | `/review/start-multi` | Create a multi-page review (explicit URL list) |
| `POST` | `/review/start-site` | Create a site-crawl review |
| `GET` | `/api/reviews` | List all reviews |
| `DELETE` | `/api/reviews` | Bulk-delete reviews |
| `GET` | `/api/review/{id}/results` | Get review details + results |
| `GET` | `/api/review/{id}/status` | Lightweight status poll |
| `POST` | `/api/review/{id}/cancel` | Cancel a running review |
| `POST` | `/api/review/{id}/resume` | Resume an interrupted/errored review |
| `DELETE` | `/api/review/{id}` | Delete a review |
| `GET` | `/api/queue/status` | Queue depth + running reviews |
| `GET` | `/review/{id}/report` | HTML report view |
| `GET` | `/review/{id}/report/json` | Report JSON |
| `GET` | `/review/{id}/export/{pdf,docx,xlsx}` | Download report (`/client` suffix for client-mode) |
| `GET` | `/review/{id}/download/evidence.zip` | Download captured evidence bundle |
| `GET` | `/review/{id}/captures` | Browse captured artifacts |
| `GET` | `/review/{id}/progress` | Progress page |
| `GET` | `/review/{id}/test/{criterion_id}` | Per-SC detail view |
| `POST` | `/api/review/{id}/test/{criterion_id}/finding/{finding_id}/decision` | Record auditor decision on a finding |
| `GET` | `/api/health` | Health check (AI backend connectivity) |
| `GET` | `/settings` | Settings UI |
| `POST` | `/api/settings` | Save settings |
| `WebSocket` | `/ws/{id}` | Real-time review progress stream |

---

## 12. Running the System

**First time setup:**
```bash
python run.py --setup
```

**Start server:**
```bash
python run.py                    # http://127.0.0.1:5050
python run.py --port 8080        # custom port
python run.py --host 0.0.0.0     # network-accessible
```

**Pre-flight checks (before submitting a review):**
```bash
curl http://127.0.0.1:5050/api/health
```

**Validate review artifacts after a completed run:**
```bash
python audit_sc.py <review_id> --all
```

**Run tests:**
```bash
pytest tests/
# After touching functions/parser.py:
python tests/test_loose_json.py
```

---

## 13. Adding a New Check

1. Create or add to an existing `checks/checks_N_N.py` file
2. Subclass `BaseCheck` from `checks/base.py`
3. Implement `criterion_id`, `criterion_name`, and `run()`
4. `run()` should call `self._build_dom_context()`, gather findings from
   each source, call `self._run_judge()`, and return `TestResult`
5. The check is auto-discovered by `checks/registry.py` — no registration needed
6. If the SC has numeric pass/fail rules, add a measurement evidence block
   in `_build_dom_context` (follow the SC 2.5.8 target-size pattern)

---

## 14. Key Design Decisions

**Why a single LLM gateway?**
Token usage, retry logic, rate limiting, transcript saving, and model routing
all live in one place. A scattered set of httpx calls would make these
cross-cutting concerns impossible to reason about.

**Why save every transcript?**
Accessibility audits are legal artifacts. When a client disputes a finding,
the exact prompt the model received (and the exact response) must be on disk.
Debugging hallucinations also requires seeing what evidence was — and wasn't —
in the prompt.

**Why is accuracy chosen over speed for image calls?**
Observed on a university-site Phase 2 test run: a 4B model ran 2× faster but hallucinated
focus indicators on 8 of 17 claimed `focus_visible` elements where before/after
screenshots were byte-for-byte identical. That directly feeds SC 2.4.7 findings.
Wrong audits are worse than slow audits.

**Why validate source attribution?**
An auditor reading "axe detected this" expects a measurement was taken.
"judge_inference" means the AI thinks this might be a problem. These are
different claims with different evidential weight. Mixing them silently would
make the report misleading.
