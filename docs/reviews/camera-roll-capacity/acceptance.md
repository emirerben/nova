# Larger collections and complete-footage suggestions (KRI-731 / KRI-730)

Original request: allow substantially more uploads and stop proposing basic
six-second edits after the creator uploaded considerably more footage.

Production metadata, 22 October 2026: one owned collection had 33 members
(28 videos, five photos), but its stored idea selected three intervals totaling
6.5 seconds. Eight intervals were indexed when the idea was requested. Cached
analysis also had visual end timestamps beyond actual short-clip durations; the
index discarded those entire windows. Exact personal copy stays outside Git.

## Acceptance before implementation

- A collection accepts and restores 500 items, rejects 501, and indexes through
  its final item in passes of at most 32. Upload/media loading remains sequential
  or batched; no 500-original memory load. Only selected story media is attached
  to a project, preserving the existing rendering adapter's limits.
- Visual windows beginning inside a clip may be shortened to its actual end;
  never extend footage, move a start, preserve out-of-range starts, or truncate
  speech/words. Evidence remains fallible, source-pinned cached analysis.
- Story generation refuses incomplete video coverage before acquiring a paid
  call or returning an older partial cached idea. Photos do not block video ideas.
  The native screen keeps the collection and explains the missing analysis.
  An explicit “Use ready videos” action may opt into partial coverage; that
  result keeps missing-video counts visible and never satisfies a later full
  request, even from cache. This avoids permanently blocking a collection on
  unsupported or failed analysis without silently omitting footage.
- Once coverage is complete, planning sees a source-time coverage summary and
  chooses a developed idea/sequence. Substantial multi-stop footage should support
  an explanatory edit around 20–45 seconds when relevant; no minimum-duration
  padding, invented actions, automatic repetition or mandatory inclusion of all
  footage. Explicitly short/mood requests stay valid. Live quality is reported
  separately from HTTP/replay wiring.
- Candidate selection stays bounded to 100 moments after querying all collection
  sources. Query-relevant footage from the final source must remain selectable.
- Account ownership, cache-only behavior, exact source generations, separate
  creative-copy/render approvals and existing original bindings remain enforced.

## Failure catalogue and case matrix

500/501 selection and resume; final-page indexing; last-source relevant search;
partial -> ready retry with no early provider call; mixed photos/videos; endpoint
overshoot versus entirely out-of-bounds or reversed intervals; timed speech
overshoot; disjoint versus overlapping source coverage; source deletion and stale
cache; short explicit request versus a developed multi-stop guide.

## Timeline decisions

Canonical duration remains the ordered source windows in `moments_json` compiled
by `prepare_story_snapshot` / `compile_proposal_execution_plan`; no new retiming
or timeline model. Ripple, resize, scrub and undo behavior are unchanged because
this change selects bounded source windows before the editor exists. Opening and
closing text retain the shared schedule. Existing request/approval/export checks
cover compilation; new index tests independently assert original source bounds.
No new native-export or end-to-end creative-quality claim follows from replay.

## Follow-up: multiple ideas

The creator also requested more than one idea. The separate
[pre-implementation criteria](multiple-ideas-acceptance.md) cover three distinct
choices, shared-footage ideas, copy clones and independent review state.
