# KRI-524 retry routing follow-up

## Failure and correction

After PR #1496 deployed, a request to retry splitting and animating a title still
produced one static title. The lexical `try again` rule selected full recreation
before the narrow extractor or editor ran. Production records showed Main Creator
running, followed by a new render; the timeline retained one static 0–2.2-second
title. The edit-composition fix was bypassed.

Rendered brief-enabled follow-ups now classify scope in the existing narrow
extractor call. `edit` keeps the capability-aware editing route, `rebuild` reaches
full planning without requiring the old editor target, and `clarify` returns a
question without actions or brief mutations. The same decision is preserved
through later routing and deferred extraction. No phrase-specific exception,
public API, migration, renderer change, or authorization change was added.

## Evidence and limitations

- Tests failed before runtime changes: three extractor-contract regressions and
  the full planner regression for the original retry construction. The latter
  selected `replan` before extraction.
- Offline full-turn tests use the real planner, extractor/editor parsers, editor
  helper, compiler and draft projection. They check 13 sequential fading words,
  preservation of source slots/audio/labels, and a faster follow-up against the
  resulting sequence. Database/context providers and model responses are injected;
  this is not a live conversation, Save or phone export.
- Genuine rebuilds with and without a target, clarification without actions,
  batch consensus/conflict, legacy fixture compatibility, and deferred semantic
  routing have separate regressions. Existing real-Postgres planner tests remain
  in `make verify-kria`.
- Live authored synthetic probes found a v5 failure: bare retry without history
  guessed rebuild. v6 specifies affirmative whole-remake intent and clarification
  for unresolved context. **All eight v6 scope cases passed**: specific retry,
  compound edit, negative instruction, rebuild, ambiguity, Turkish, contextual bare
  retry and quoted rebuild wording. These check scope, not rendered results or
  statistical model reliability. No private conversation was sent in these probes.
- Thirteen calls including the failed v5 experiment cost **$0.115122**. The shared
  envelope settled **$0.377236 / $0.50**; cumulative KRI-524 evaluations total
  **$2.537412 / $5**. No unmetered judges.

Verification commands are `make verify-kria`, the affected agent/eval/compiler and
phone-save suites, scoped Ruff and `scripts/preship-check.sh`. Current counts and
commit are recorded in the PR evidence. Release metadata is owned by post-merge
automation.

Merge/deployment remain gated. After deployment, verify a new real turn uses the
scope-aware extractor and `draft.apply_editor_ops`, then inspect the saved timeline
and phone preview/export. Deployment health alone does not prove the edit works.
