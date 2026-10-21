# Camera-roll recommendations: acceptance before implementation

Issues: KRI-729–732. Baseline: `b3a2a9edb`, 19 October 2026.

## Failure cases and evidence

- Indexing must never enumerate unselected Photos assets or load file/provider data.
  Measure only the creator's explicit selection, with capture context disabled as
  well as enabled. Device-local identifiers never leave the device.
- Full, limited, picker-only, denied and revoked access differ. Missing metadata
  and unresolved identifiers are unknown, never proof that footage was inspected.
- Cancellation/relaunch and selection additions/removals must retain completed
  eligible metadata and manual group corrections, without re-reading everything.
- Mixed photos/videos, missing dates, exact duplicates, changed assets and
  unavailable iCloud originals must not corrupt local-original render bindings.
- Collection writes must fence owner, revision and immutable source identities.
  Concurrent corrections must not overwrite one another; unavailable/archived
  sources cannot silently switch to another source generation.
- Moment search must find late spoken sentences and late visual moments without
  using the prompt's truncated projection. Unsupported/missing timings, source
  changes, another owner's bytes and no-match queries must never invent results.
- Selection is distinct from approval. Correcting one decision must preserve
  unrelated decisions; the same approved snapshot must reach actual export.
- Rollback hides discovery without modifying historical posts or accepted exports.

## Evidence sequence

1. Native metadata-only benchmark of the creator-selected collection. Record app
   revision, device/OS, selected/resolved/unresolved counts, latency, process peak
   resident memory, network/media bytes and provider calls. Metadata resolution
   does not establish original-file availability. Choose final work limits only
   after inspecting this evidence.
2. Real PostgreSQL HTTP journeys for ownership, collection corrections, source
   invalidation, interval indexing and repeated-query reuse. Offline provider
   substitutes prove wiring only, not retrieval quality or model behavior.
3. Prelabel private creator/event holdouts before tuning. Include lit-house/ferry,
   long-take endings, speech beyond the excerpt boundary, occlusion, story-relevant
   darkness/stillness and no-match controls. Compare text/metadata and semantic
   retrieval. Initial proposed bar: accepted interval in top 10 for >=90% of
   answerable queries; report no-match precision and unusable returns separately.
4. Native recommendation → correction → separate approval → export → reopen
   evidence, including picture/audio inspection against exact source windows.
5. Consented pilot under the protocol; no promotion until prerequisite reliability
   evidence and the pilot's frozen decision bars pass.

## Execution and study scope

The existing guided snapshot/compiler/approval path is the source-window authority for
this slice; a server-owned binding freezes exact choices before export. Broader cloud,
slide and effects adapters remain unpromoted. Ticket status alone is not release proof.
Discovery remains off pending reviewed device and human-evaluation evidence.

The creator limited the study to their account and subsequently permitted model calls
when needed for the real test, under the discussed $2 total ceiling. No other participants
are authorized. All added collection items remain eligible;
limits belong to internal metadata/index/network/model passes, never a second per-item opt-in.

## Recommendation montage timing decisions (19 October continuation)

1. Duration is owned by the existing `EditProposalSnapshot.fast_cuts` and guided
   compiler/frame schedule: ordered source windows at rate 1, hard cuts, total
   duration equal to the sum on the shared 30fps frame grid, before consent. No second timeline or recommendation renderer.
2. This creates a new draft. Replacing a selected moment changes only that cut;
   there are no authored timed lanes to ripple. Generated on-screen copy/music
   are absent. Later timeline edits use the existing guided editor policies.
3. Playback/scrubbing uses the existing guided editor and compiled recipe duration.
4. Source replacement chooses a complete indexed interval; no new drag/resize
   interaction. Invalid/over-budget windows are refused before approval.
5. Each explicit correction creates one story revision. Selection binds that exact
   revision into the existing saved draft; ordinary draft undo remains in charge.
6. Use the existing fast-cut compiler and phone-guided parity/export fixtures;
   verify the chosen windows, order, rate and source audio through actual output.
   Cached analysis and compiler checks alone never satisfy the export gate.
