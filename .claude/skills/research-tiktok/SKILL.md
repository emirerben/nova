---
name: research-tiktok
description: >-
  Weekly TikTok market-research pass. Fetches public account metadata locally
  (yt-dlp, no paid API), mines it into Nova's versioned persona/style/content-idea
  banks, bumps the coupled prompt versions, runs the guard tests, and opens a PR.
  Use when asked to "run the weekly tiktok research", "refresh the persona pool",
  "mine new content ideas", or when the weekly schedule fires.
---

# /research-tiktok — weekly market-research → agent training

You are the analyst. This skill turns public TikTok accounts into refreshed
few-shot banks that steer Nova's plan + generative agents. **No paid API**: the
fetch is `yt-dlp` metadata only; the *analysis* is you reasoning over it. The
output is always a **PR for human review** — never a direct push to prod prompts.

## What feeds what (the three banks)

| Bank file (`src/apps/api/prompts/`) | Trains | Coupled version to bump |
|---|---|---|
| `persona_archetypes.json` | `generate_persona.txt` (persona pool / style types) | `PERSONA_PROMPT_VERSION` in `app/agents/_schemas/persona.py` |
| `content_ideas.json` | `generate_content_plan.txt` (idea bank) | `CONTENT_PLAN_PROMPT_VERSION` in `app/agents/_schemas/content_plan.py` |
| `overlay_examples.json` | generative hook voice/form | `prompt_version` in BOTH `intro_writer.py` and `overlay_format_matcher.py` |

Schemas: `app/agents/_schemas/market_research.py` (`PersonaArchetype`, `ContentIdea`)
and `app/agents/overlay_examples.py` (`OverlayExample`). Loaders:
`app/agents/persona_examples.py`.

## Procedure

1. **Fresh worktree off `origin/main`** (CLAUDE.md rule):
   `bash scripts/new-session.sh tiktok-research-$(date +%Y%m%d)` then `cd` into it.

2. **Fetch** (free, local, no download):
   ```bash
   python scripts/research/fetch_tiktok.py --accounts research/tiktok/accounts.txt --limit 20
   ```
   Add `--enrich` for like/comment counts + upload dates (slower, more requests).
   Raw JSON lands in `research/tiktok/raw/` (gitignored). Fetch is best-effort —
   skipped accounts are fine; note them in the PR body.

3. **Mine** the raw JSON into bank entries. For each distinct, high-performing
   pattern you see (rank by `view_count`/engagement):
   - **PersonaArchetype** — a recognizable creator identity + its style (`tone`),
     `content_pillars`, `audience`, a few `sample_hooks`.
   - **ContentIdea** — a templated, niche-tagged concept with a `hook_pattern`
     and `filming_context`. Use `[brackets]` for the creator-specific slot.
   - **OverlayExample** — only when you see a genuinely new hook *voice/form*;
     match the existing schema (`effect`, `position`, `size_class`, colors).
   Rules: dedup by `id` against existing entries (append, don't churn). Keep the
   bank curated — add a handful of strong entries, not everything. **Strip all
   @handles and brand names from every body field**; put attribution in `source`
   only (`"tiktok:@handle (YYYY-MM)"`). These are STYLE references — never text to
   reproduce verbatim.

4. **Bump versions** (the coupling contract — a bank edit without the matching
   prompt_version bump is the "stale cache in prod" trap):
   - bumped a bank's `version` field → bump its coupled constant(s) above.
   - Use a fresh date string; if today's date is already the committed value, add
     a `.1`/`.2` suffix.

5. **Validate** (free, no network):
   ```bash
   cd src/apps/api && pytest tests/agents/test_market_research_banks.py -v
   ruff check . && ruff format --check .
   pytest tests/evals/test_persona_generator_evals.py tests/evals/test_content_plan_generator_evals.py
   ```
   The coupling-guard tests in `test_market_research_banks.py` assert the bank
   `version` and prompt_version constants match — update those asserts when you
   bump (they are the intended gate, not an obstacle).

6. **(Optional, paid — flag in the PR)** the CLAUDE.md prompt-change rule wants a
   live + judge eval for prompt edits. That costs Gemini (~$2–5). Run once if the
   change is large; otherwise rely on the structural evals + this PR's human review.

7. **Open the PR**: `chore(research): weekly TikTok artifact refresh <date>`.
   Body = accounts fetched (and any skipped), counts added per bank, versions
   bumped, eval status. A human merges — that review is the gate that keeps
   unvetted market data out of prod prompts.

## Guardrails

- Never commit `research/tiktok/raw/` (gitignored — reproducible intermediate).
- Never reproduce mined captions verbatim in a bank `text`/`idea`/`hook` field.
- Never push straight to `main`; always a reviewable PR.
- If `yt-dlp` returns nothing for every account (TikTok blocking), stop and
  report — do not invent entries.
