# Restored clips: preview and Save handoff

Physical production build 1688 retained the restored Troya car and the next chat
turn moved and trimmed it correctly. Save then displayed “This save was rejected.
Your edits are still here.” Preview displayed unsupported content. Research was
not submitted, and the original saved project was preserved.

## Failure cases recorded before the repair

- A restored placement without a slot ID must be a typed timeline row, not both
  a typed row and opaque content. Opaque records block native preview.
- Restore followed by another chat reorder/trim must remain acceptable to the
  actual Save validator. `client_state_payload` invented `cs-*` slot IDs, which
  the persisted-timeline validator correctly rejected with `TIMELINE_STALE`.
- A supplied unknown existing ID, including a `cs-*` ID, must still be rejected.
- Existing guided timeline projection, text rebasing, and saved-slot handling
  must remain intact.

The backend regression reproduced the real `TIMELINE_STALE` failure before the
change. The repaired path leaves new placement IDs absent until Save assigns
permanent IDs. The actual Save validator and stale-ID protection are unchanged.

## Verification

Backend: 136 focused editor-state, stale-head, guided timeline and timeline Save
checks pass. The native regression failed before the fix for both missing and
null IDs, then all 287 staging, document and editor-session checks passed.
Physical replay,
researched captions, Sources, save/reopen and final native export remain pending;
this record is not evidence of a completed production journey.

Local evidence is under `/private/tmp/troya-live-ui`: the physical
`restored-source-save-only-1792432099.xcresult`,
`restored-source-state-images`, and `restored-save-api-green.log`.

## CI scrub gesture correction

CI 39298590464 and a local reproduction failed the slow-scrub UI scenario.
The test used 5% of the scroll viewport height as a supposed ruler position.
The recorded pointer was at y=576.65 with the clip starting at y=585: this
selected the second clip instead of reliably scrubbing, and subsequent drags
could trim its duration to 0.226 seconds (0.212 on CI). Diagnostic snapshots
showed unchanged timeline positions across the supposed ruler drags.

The test now uses the visible Original audio lane for every scrub, reusing the
same observed-geometry helper already used for its selected-clip checks. All
preview color, outro, reverse-scrub, pause, and unchanged-duration assertions
are retained; no timeout, retry, threshold, or coverage exclusion was added.
The corrected scenario passed with zero failures in 52.760 seconds. Evidence:
`restored-save-scrub-repro.xcresult`, `restored-save-scrub-trace.xcresult`, and
`restored-save-scrub-audio.xcresult` under the same local evidence directory.
This is a test targeting correction; it does not establish physical-device
research/export success.
