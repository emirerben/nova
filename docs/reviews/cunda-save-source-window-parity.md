# Save source-window parity with the iPhone preview

Save-contract follow-up: KRI-750 (the existing editor Save failure-mode matrix).
That historical issue is Done; this PR does not reopen it or claim that the current
Cunda production job has recovered. Original-incident acceptance remains pending
in this report. Its exact saved payload and installed build were unavailable in
this executor.

## Acceptance and boundaries

A source window the native editor can preview and the phone compiler can render
must survive saved-render preparation. Preserve source identity, selected start
and end, playback rate, output duration, text, audio and the stored draft. Refuse
real missing footage and genuinely impossible pinned windows.

Three discrepancies are reproduced on main `379b09304a4123472c76b00dc326d112e7cf5ef6`:

1. The saved duration `6.2 - 5` is `1.2000000000000002`. The native compiler's
   source-window boundary tolerates one microsecond, but the backend's strict
   comparison reports that a 1.2-second source needs more footage. The backend
   now uses the same tolerance and never re-anchors to a negative source start.
2. `_source_end_for_authored_slot` admits an exact source end and rounds the
   output clock to 30 fps. `compile_phone_authored_timeline` consumes that exact
   source window and, when needed, holds the last frame for the rounded tail.
   `prepare_saved_render_windows`, called later in the Save route, ignored the
   exact endpoint and recomputed a longer source span from duration × rate. It
   could mark the already-compiled recipe failed. Device rows with an exact
   endpoint retain their admission and compiler verdict. Missing-source checks
   still run first; legacy cloud-window validation is unchanged.
3. The pilot gate treated a subframe main-video hold as unsupported editor media,
   although both the recipe schema and native engine classify at most one output
   frame as rounding padding, not a freeze. The gate now matches that distinction.
   Longer holds still require `freezeFrame`; overlay holds are still rejected.

There are no changes to editing gestures, ripple, scrub, Undo, draft persistence,
source grants, render destinations, prompts or provider calls. No user media,
project, account state or production setting was changed.

## Failure modes and verification

Tests were written and run before implementation. Eleven cases failed with the
reported error, while fifteen controls passed. The final matrix covers:

- subtraction noise at 0.5x, 1x, 2x and 4x, locked and unlocked;
- true one-millisecond overrun and nonnegative re-anchoring;
- real source resolution → phone compilation → saved-render preparation;
- exact endpoints at 3.5x with a rounded tail, preserved source identity and
  output clock, repeated preparation, and unchanged authoring rows;
- actual version-2 prepared Save through the pilot gate and pinned device request
  at 0.5x, 1x, 1.3x, 3.5x and 4x;
- rounding padding at 24/30/60 fps, longer unqualified freezes, qualified freezes
  and short overlay-hold rejection;
- out-of-bounds endpoints rejected during admission, malformed endpoints
  rejected by the wire schema, unavailable source, and legacy cloud behavior.

Run from the worktree root:

```bash
bash scripts/check-api.sh test tests/pipeline/test_editor_render_window_intent.py tests/pipeline/test_authored_timeline.py tests/services/test_editor_empty_drafts.py tests/routes/test_editor_save_contract_matrix.py tests/routes/test_authored_playback_speed.py tests/pipeline/test_phone_guided_speed.py tests/routes/test_phone_editor_commit.py tests/services/test_phone_rollout.py tests/kria/test_kri828_freeze_frame_m2.py tests/kria/test_kri828_freeze_review_fixes.py -q
```

The independent native expectation is
`src/apps/ios/Tests/KriaTests/SavedDraftPreviewRangeTests.swift` and the existing
`NativeEditorRenderCompiler` source-window rule. These were read, not executed
on this Linux executor. The existing `editor-durable-save` journey includes the
changed regression module. Before the pilot-gate fix, thirteen new cases
failed and four freeze/overlay controls passed; this included the actual prepared
Save regression missed by the earlier narrower compiler test.

## Remaining proof

This is offline synthetic source-window and recipe evidence, not an original
media replay or inspected native output. There is no connected desktop or saved
coding environment in the available catalog. PostgreSQL setup is unavailable
locally, so the durable database Save/reopen suite is not run. The generic
`make verify-editor-timeline` command is web-only and stops at missing Jest;
no web-editor implementation or browser work was performed.

Before claiming the incident solved, obtain the current Cunda saved revision,
identify its failing row and deployed API revision, replay the exact stored
inputs, then verify Save, reopen and a Photos export on the iPhone after an
authorized rollout. Preserve the saved timeline and original media throughout.
KRI-750 supplies the existing Save-contract scope for this follow-up; its Done
status is not evidence of current Cunda recovery. Historical KRI-984 is the closed
song-section compilation issue, and KRI-1063 concerns finished-export publication.
Neither is the current source-window failure, and neither issue is changed here.
