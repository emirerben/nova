## What
<!-- One-line summary of this change -->

## Why
<!-- Context or reference to TASKS.md item -->

## Checklist
- [ ] TASKS.md updated (mark completed items, add follow-ups)
- [ ] No video files in diff (`git diff --stat` shows no .mp4/.mov/.mkv)
- [ ] .env.example updated if new env vars were added
- [ ] VIDEO_CONTEXT.md consulted for any video processing changes
- [ ] Applied release labels when needed: `release:major` or `release:minor`, plus one changelog category (`release:added`, `release:fixed`, `release:changed`, `release:deprecated`, `release:removed`, or `release:security`). No label means a patch-level `Changed` entry.
- [ ] If a Kria creation or edit journey is affected, the author linked the CI journey artifact for this HEAD, named the request and expected output, and recorded native render, model/prompt, cost, and any gaps. See `docs/reviews/kri-559-journey-verification.md`.
- [ ] Reviewer checked the journey case matches this change, required stages and native evidence passed on this HEAD, and any live-model result is labelled separately from offline replay.
- [ ] For prompt, model, or provider changes, link the protected `Agent evals` live run for this HEAD (maximum $2) and report its settled cost separately.

## Reliability gates (KRI-577; required when a Kria chat, render, receipt or device path changes)
<!-- Fill the five lines or write "n/a: <why>". See docs/runbooks/pr-acceptance-checklist.md -->
- Incident replay: <gate id(s) from `python3 scripts/reliability-gates.py select`, or the new fixture/test node that replays the original failure; it fails without the fix (red control) and passes with it>
- Neighbouring cases: <the sibling inputs/paths exercised (empty vs explicit selection, EN/TR, phone proxy vs full-res, draft vs render-ready, stale generation, cap boundary); which are still open>
- Platforms and evidence level: <offline replay | live model (cost) | native output (device/simulator export) | production (revision, UTC)>
- Model/prompt resource envelope: <n/a, or tokens / timeout / cost / call count before and after>
- Known environment failures vs regressions: <e.g. "9 DB-backed tests skipped locally; 0 regressions" — never "pre-existing" without the list>

<!-- Do not edit VERSION, CHANGELOG.md, or root package version fields here.
     Post-merge automation publishes all release metadata after this PR lands. -->
