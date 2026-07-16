# AccessAudit

An AI-powered accessibility auditing tool that runs comprehensive WCAG 2.x
checks (A / AA / AAA) against live web pages and documents, then produces
full Accessibility Conformance Reports (ACR / VPAT-style) in HTML, DOCX,
XLSX, and PDF.

It combines deterministic checks (axe-core, ANDI-style extraction, contrast
math, target-size measurement, tab-order walks) with multi-model AI analysis
(code analysis, visual inspection of screenshots, video/flash analysis, an
LLM judge that consolidates every evidence source per success criterion).

## Requirements

- **Python 3.11+**
- **ffmpeg** on your PATH (for audio/video checks) — `winget install ffmpeg`,
  `brew install ffmpeg`, or `sudo apt install ffmpeg`
- An **LLM API key** for at least one provider (OpenRouter, Google Gemini,
  OpenAI, or Anthropic), **or** your own local OpenAI-compatible model server
  (vLLM, llama.cpp, LM Studio, Ollama…)

## Quick start

```bash
git clone https://github.com/simkinselgazar-rgb/accessaudit.git
cd accessaudit

# One-time setup: creates a venv, installs deps + Playwright Chromium + ffmpeg
python run.py --setup

# Configure your AI provider
cp settings.example.json settings.json
# ...edit settings.json and add your API key
# (or skip this and use the in-app Settings page after starting)

# Start the app
python run.py
```

Then open **http://127.0.0.1:5050**, paste a URL, and start a review.

## Configuration

All settings resolve in this order: **environment variables → `settings.json`
→ built-in defaults**. See `settings.example.json` for the common setups:

1. **Single cloud provider** — one API key runs the whole stack.
2. **Mixed providers** — e.g. a cheap text model for code analysis plus a
   multimodal model for screenshots/video. Every role (vision, judge,
   reviewer, video, embeddings, whisper) can point at a different endpoint.
3. **Fully local** — point everything at your own OpenAI-compatible servers;
   no API key ever leaves your machine.

`settings.json` is **gitignored** because it contains your API keys. Never
commit it.

### Recommended models

Audit quality is only as good as the models you configure. Small or heavily
quantized models have been observed hallucinating findings (e.g. reporting
focus indicators on identical screenshots), which silently corrupts results.
These cloud models are known-good starting points:

| Provider | Text / code analysis | Vision (screenshots, video) |
|---|---|---|
| **OpenRouter** (one key, any model) | `google/gemini-2.5-flash` | `google/gemini-2.5-flash` |
| **Google Gemini** | `gemini-2.5-flash` | `gemini-2.5-flash` |
| **OpenAI** | `gpt-4o` | `gpt-4o` |
| **Anthropic** | `claude-sonnet-4-20250514` | `claude-sonnet-4-20250514` |

Guidance:

- **Vision matters most.** Screenshot-bearing checks (focus visibility,
  contrast context, reflow, target size) need a strong multimodal model —
  never point the vision role at a text-only or small model.
- **Budget option:** a cheap text model for code analysis plus a strong
  multimodal model for the vision/judge roles is the best cost/quality
  trade-off (that's what the mixed-provider example in
  `settings.example.json` shows).
- **A full single-page review makes hundreds of AI calls.** On flash-tier
  cloud pricing that's typically well under a dollar per page; on
  frontier-tier models it can be several dollars. Start with a flash-tier
  model and upgrade the judge/reviewer roles if you need deeper analysis.
- **Local models work** (vLLM, Ollama, LM Studio) but expect reduced
  accuracy below ~27B parameters, especially for vision.

Optional services (the tool degrades gracefully without them):

- **Embeddings** (cross-page dedup / consistency checks): local Ollama with
  `bge-m3`, or any OpenAI-compatible embeddings endpoint.
- **Whisper** (caption verification): local faster-whisper server, OpenAI
  transcription endpoint, or Gemini native audio.

## What a review produces

Each run creates `reviews/<timestamp>_<id>/` containing:

- `captures/` — full-page screenshots, DOM snapshot, observation video,
  extracted frames
- `tests/<sc>/` — per-success-criterion evidence and verdicts
- `llm_transcripts/` — the complete prompt + response for every AI call
  (full auditability; nothing is truncated)
- The final report, exportable as HTML / DOCX / XLSX / PDF

## Deployment note

This is a **Python server application** (FastAPI + Playwright browser
automation + ffmpeg). It must run somewhere that can execute long-lived
Python processes and headless Chromium — a VM (EC2, Droplet), a container
platform (App Runner, Fly.io, Render, ECS), or your own machine.

Static-site hosts (AWS Amplify static hosting, GitHub Pages, Netlify static)
**cannot run the backend**. For those, this repo ships a static landing page
in `site/` (served by the included `amplify.yml`) where visitors can download
the tool and find these setup instructions.

## Development

```bash
python -m pytest tests/          # run the test suite
python audit_sc.py <review_id> --all   # validate a completed review's artifacts
```

See `docs/ARCHITECTURE.md` for the full system spec and the working rules
that govern changes.

## License

MIT — see `LICENSE`.
