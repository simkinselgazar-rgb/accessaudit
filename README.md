# WCAG Trusted Tester

An AI-powered accessibility auditing tool that runs the DHS Trusted Tester–style
WCAG 2.x checks (A / AA / AAA) against live web pages and documents, then
produces full Accessibility Conformance Reports (ACR / VPAT-style) in HTML,
DOCX, XLSX, and PDF.

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
git clone <this-repo>
cd wcag-tester

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
**cannot run the backend** — they can only serve a landing page that links to
these setup instructions.

## Development

```bash
python -m pytest tests/          # run the test suite
python audit_sc.py <review_id> --all   # validate a completed review's artifacts
```

See `docs/ARCHITECTURE.md` for the full system spec and `CLAUDE.md` for the
working rules that govern changes.

## License

MIT — see `LICENSE`.
