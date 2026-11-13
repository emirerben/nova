---
name: research-tiktok
description: >-
  Weekly TikTok market-research pass. Fetches public account metadata locally
  (yt-dlp, no paid API), mines it into Nova's versioned persona/style/content-idea
  banks, bumps the hand-numbered intro/matcher versions, runs the guard tests, and opens a PR.
  Use when asked to "run the weekly tiktok research", "refresh the persona pool",
  "mine new content ideas", or when the weekly schedule fires.
---

# /research-tiktok — weekly market-research → agent training

You are the analyst. This skill turns public TikTok accounts into refreshed
few-shot banks that steer Nova's plan + generative agents. **No paid API**: the
fetch is `yt-dlp` metadata only; the *analysis* is you reasoning over it. The
output is always a **PR for human review** — never a direct push to prod prompts.

## What feeds what (the four banks)

| Bank file (`src/apps/api/prompts/`) | Trains | Coupled version to bump (persona and plan versions are fingerprints of these files: never bump them) |
|---|---|---|
| `persona_archetypes.json` | `generate_persona.txt` (persona pool / style types) | none (automatic) |
| `content_ideas.json` | `generate_content_plan.txt` (idea bank) | none (automatic) |
| `tiktok_success_factors.json` | `generate_persona.txt` + `generate_content_plan.txt` + `write_intro_text.txt` (codified "why it performs" levers) | `intro_writer.py`'s `prompt_version` only |
| `overlay_examples.json` | generative hook voice/form | `prompt_version` in BOTH `intro_writer.py` and `overlay_format_matcher.py` |

Schemas: `app/agents/_schemas/market_research.py` (`PersonaArchetype`, `ContentIdea`,
`SuccessFactor`, `PerformanceSignal`) and `app/agents/overlay_examples.py`
(`OverlayExample`). Loaders: `app/agents/persona_examples.py`.

**Performance grounding.** `persona_archetypes.json` and `content_ideas.json`
entries carry an optional `performance` block (`PerformanceSignal`: `views`,
`engagement_rate`, `view_index`). The runtime ranks pillar fit first, then
performance — so mine these from the fetched numbers, don't eyeball. `view_index`
= a video's views ÷ the account's median views (outperformance vs the account's
own baseline; account-size independent and the strongest signal). The fetch
script pre-computes `engagement_rate` + `view_index` per video under `--enrich`
(step 2) — copy them straight across. Entries with no `performance` still load,
they just rank last within their fit tier.

**Success factors.** `tiktok_success_factors.json` codifies WHY short-form
content performs so the plan/hook agents cite strategy, not instinct. Every
factor is tagged `provenance`: `"corpus"` (observed in OUR fetched engagement
data — `evidence` cites the view_index you saw) or `"public"` (from TikTok's
published creator docs — `source` MUST cite where). Keep the two honest and
never conflated. Body fields (`factor`/`why`/`evidence`) carry NO @handles or
verbatim captions — attribution lives in `source`. `applies_to` is a subset of
`{persona, plan, hook, all}` and routes the factor to the right prompt(s).

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
   - **SuccessFactor** (`tiktok_success_factors.json`) — when a `--enrich` run
     surfaces a pattern that clearly outperformed (high `view_index`), add a
     `provenance:"corpus"` factor whose `evidence` cites the index you saw
     ("indexed ~Nx the account median"). Only add `provenance:"public"` factors
     from TikTok's own published creator guidance, each with a `source`.
   On PersonaArchetype/ContentIdea, attach the `performance` block from the
   fetched `view_index`/`engagement_rate` of the post you mined it from.
   Rules: dedup by `id` against existing entries (append, don't churn). Keep the
   bank curated — add a handful of strong entries, not everything. **Strip all
   @handles and brand names from every body field**; put attribution in `source`
   only (`"tiktok:@handle (YYYY-MM)"`). These are STYLE/strategy references —
   never text to reproduce verbatim.

4. **Bump versions** (the coupling contract — a bank edit without the matching
   prompt_version bump is the "stale cache in prod" trap; persona and plan
   versions need none, they are fingerprints):
   - bumped a bank's `version` field → bump its hand-numbered constant(s) above.
   - Use a fresh date string; if today's date is already the committed value, add
     a `.1`/`.2` suffix.

5. **Validate** (free, no network):
   ```bash
   cd src/apps/api && pytest tests/agents/test_market_research_banks.py -v
   ruff check . && ruff format --check .
   pytest tests/evals/test_persona_generator_evals.py tests/evals/test_content_plan_generator_evals.py
   ```
   The coupling-guard tests in `test_market_research_banks.py` assert the bank
   `version` and the hand-numbered prompt_version constants match — update those asserts when you
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
