# AccessAudit — Documentation

Everything that explains the system lives in this folder. The two
load-bearing documents:

| Document | What it is |
|---|---|
| [`ARCHITECTURE.md`](ARCHITECTURE.md) | The full system spec (26 sections, ~2,200 lines). Pipeline, LLM gateway, parser, capture phases, check registry, judge, reports, and the non-negotiable rules. Read this first when onboarding or before non-trivial changes. |
| [`../CLAUDE.md`](../CLAUDE.md) | Working rules for every code change. Loaded automatically by Claude Code; humans should read it too. Overrides anything in `ARCHITECTURE.md` if they conflict. |

## Where to start

- **New to the project?** Read `ARCHITECTURE.md` sections 1–5 (overview,
  directory layout, the `functions/` folder, LLM gateway, parser). That is
  enough to navigate the codebase. Sections 9–14 cover the runtime pipeline
  in depth when you need it.
- **Making a code change?** Read `../CLAUDE.md` end-to-end. It is short
  and every rule has bitten us at least once.
- **Debugging a check or judge call?** Section 23 in `ARCHITECTURE.md`
  ("Error Handling & Debugging") plus the saved transcripts under
  `<review>/llm_transcripts/` are the entry points.
- **Adding a new WCAG check?** Section 22 ("The Check Files") shows the
  `BaseCheck` contract, file layout, and how AAA / 2.2 variants are split.

## What's deliberately not in this folder

- **`prompts/`** holds JSON prompt templates per WCAG SC. Those are runtime
  data, not docs — they're loaded by `functions/prompt.py` at audit time.
- **`guidelines/`** holds WCAG normative-text reference content used by
  retrieval. Same story — runtime data, not docs.
- **`notes/`** holds scratch / per-review verification checklists. They
  are working artifacts, not authoritative documentation.

## Keeping this folder honest

Anyone editing the code is expected to update the doc when behavior or
structure changes:

- New module in `functions/` → add a row to the inventory in Section 3.
- Renamed capture phase or new phase → update Section 2 (directory tree),
  Section 9 (capture pipeline), and the relevant phase section.
- New check file (AAA / 2.2 variant, document or media check) → update
  Section 22.
- Default model change in `config.py` → reconcile Section 8.5 example.

Doc drift is a real bug, not a stylistic concern. If you ship a behavior
change without a doc update, the next contributor (human or AI) will
debug from a wrong map.
