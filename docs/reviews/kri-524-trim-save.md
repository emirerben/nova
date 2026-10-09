# KRI-524: compound trim and Save follow-up

Status: production Save rejection matched to the local regression; fixes verified locally.
Base: `0c25a307e` (`origin/main` at the delivery review).
Deployment: fixes are not deployed yet.

## Evidence and boundaries

The captured editor turn selected a two-second opening clip and twelve explicit
text windows ending at two seconds. Its execution completed and created a draft.
A read-only production draft lookup confirmed the first word's requested 0.15s
endpoint was changed to 0.2s by the compiler. A later retry timed out at the model
provider. A later client snapshot retained
the previous opening duration; whether the draft failed to apply or local edits
intervened has not been established.

The connected phone initially recorded HTTP 422 with diagnostic code `unknown`.
After Fly authentication, a fresh Save retry at 2026-10-09 09:05:50 UTC logged
`editor_commit_422`, code `unsupported_phone_edit`, and `text layer exceeds the
timeline`. Its sections were text and timeline plus request metadata. This
matches the independently reproduced stale server-owned context-label failure.
The original older `unknown` event's response body remains unavailable; the fresh
retry establishes the current blocker. Logs were filtered to this video only.
The phone was not restarted, reinstalled, or edited during investigation.
Raw conversations and device diagnostics remain outside the repository.

## Changes

- Explicitly authored text windows use the new output timeline. They are no longer
  projected a second time after a same-bundle clip trim or extended to the old
  0.2-second minimum. Unchanged clip labels still follow their clips. Nonfinite,
  reversed, and out-of-bounds explicit windows reject atomically.
- Server-owned context labels follow the surviving portions of their canonical
  moments. A shortened edit no longer carries a stale label past its endpoint.
  Split, reordered, deleted, repeated-source, source-offset-zero, and partial-label
  cases are covered. Newly inserted sources do not inherit unrelated labels.
- A clip/title duration is no longer compared to the duration of the whole video.
  Target-specific duration verification remains explicitly unverified; global
  duration checks remain active.

## Verification

- Regression-before-fix evidence: the original context-label Save raises HTTP 422
  with `text layer exceeds the timeline`; original explicit timing regressions fail;
  six original scoped-duration assertions fail. Patched regressions pass.
- `make verify-kria`: **2,318 passed, 6 skipped**, using an isolated local test
  database. The shared database had enough retained drafts to affect its bounded
  retention test; the isolated database removes that test-state interference.
- Affected compiler, timeline, and Save suites: **227 passed**. The updated
  twelve-word sequence case also passes with a gap in compiler-style sequence IDs.
- The five bulk timing regressions now cover selector targeting, explicit no-op
  authorship, output-clock projection of untouched labels, and atomic rejection
  of nonfinite values. The focused guided timeline file passes **35 tests**.
- iOS `ChatDraftStagingTests`: **46 passed** in the simulator. The new case applies
  flat clip/text draft fields over stale nested fields, refreshes again, and checks
  the outgoing Save request. Server transport is a fixture in this suite.
- Native export: **1 passed**. A real compiled Save recipe exports a four-second
  synthetic video: two-second opening plus two later one-second clips. Checks cover
  word identities, the two-second word endpoint, rendered text presence/absence,
  surviving footage, and audible source tone in all three clips.
- Scoped lint, format, and preship checks pass. Independent runtime review found
  no blocker in the local patch.
- The full backend gate completed with **2,318 passed, 6 skipped**. Creation
  native export completed successfully. The trim-save native result is now a
  required CI journey and its separate result bundle and render are uploaded;
  the existing immutable journey evidence still hashes the creation case only.
- No new paid model calls. Offline replay and synthetic export do not prove live
  model reliability. Production behavior after deployment still needs verification.

The first simulator test assertion compared entire slot dictionaries and was
corrected to allow metadata preserved by Save. The first export pixel test used
an incorrect rectangle endpoint; frame inspection confirmed visible text, and the
corrected rectangle passes. These were test setup errors, not runtime fixes.

## Repeatable native export

Run the API test environment's Python from the repository root:

```sh
src/apps/api/.venv/bin/python scripts/ios/kri-524-trim-save-e2e.py /private/tmp/kri524-trim-render
```

Run the iOS test with `TEST_RUNNER_KRIA_E2E_DIR=/private/tmp/kri524-trim-render`:
`KriaTests/DeviceMontageRenderE2ETests/testCompoundWordTrimSavedThenExportOnTheIPhone`.
The generator uses synthetic media and the captured operation shape, executes
Save and an unrelated subsequent Save, and makes no provider requests.

## Required before declaring the user's issue resolved

1. Finish delivery review and obtain merge/deploy approval for this follow-up.
2. Verify the deployed revision, then retry Save on the existing manual edit.
   No native runtime code changed, so this does not require reinstalling the app.
