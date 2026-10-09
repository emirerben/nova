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

<!-- Do not edit VERSION, CHANGELOG.md, or root package version fields here.
     Post-merge automation publishes all release metadata after this PR lands. -->
