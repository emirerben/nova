# 016 — Strict guided-story renderer and Corfu acceptance preview

Planned at `cd02bac45` on 2026-08-14. This is PR 3 of the serial Corfu guided-edit
work. PR 1 repaired the missing-title regression. PR 2 added the reviewable,
versioned proposal and snapshots an approved proposal into a render Job. This
plan makes that snapshot the authoritative render program.

## Outcome

When a content-plan Job contains `assembly_plan.guided_edit`, Nova renders one
`guided_story` result from the approved photos, videos, order, title, thoughts,
layouts, and target duration. It never asks the legacy montage path to interpret
the story. It publishes only after a strict receipt proves that every approved
beat, required text layer, and source identity survived.

The release stays dark until an exact read-only Corfu preview passes. No existing
production output is mutated by the preview.

## Existing contract we build on

- `PlanItem.edit_proposal` stores draft and last-approved snapshots using stable
  `MediaRef` and `StoryBeat` IDs.
- Generate revalidates storage ownership and generations, then copies the exact
  approved snapshot and identities to `Job.assembly_plan.guided_edit` under the
  established Plan → Persona → PlanItem → Job lock order.
- The item page gates Generate on proposal state when enforcement is enabled.
- The renderer already has bounded generation downloads, still-image motion,
  video crop/trim, final-quality concat, music mixing, Skia text burning,
  output upload, and editable `TextElement` support.
- `GUIDED_STORY_RENDERER_READY` is currently false, making enforcement
  impossible even if an operator mistypes the rollout flags.

An approved asset-only proposal is valid. Dispatch uses the first server-validated
selected proposal source as the Job's required seed/raw path when the legacy clip
lane is empty; it does not require `PlanItem.clip_gcs_paths` for guided work.

## Architecture and data flow

```text
approved Job snapshot
  │
  ├─ validate schema + proposal version + digest + media identity set
  ├─ download exact GCS generations (clips and asset-pool media)
  └─ compile deterministic story timeline
       │
       ├─ beat 1 → one or more image/video moments
       ├─ beat 2 → one or more image/video moments
       └─ beat N → one or more image/video moments
                │
                ├─ fullscreen: direct 9:16 moment
                └─ supporting_card: first-class composed story frame
                         │
                         ▼
              final-quality sequential story base
                         │
                whole-story music match + mix
                         │
              persist clean text-free base
                         │
              approved title/thought TextElements
                         │
                   Skia text burn
                         │
             strict render-receipt verification
                         │
                 upload and publish ready
```

Whenever capability is enabled, dispatch validates and snapshots a current
approved proposal even while enforcement remains off. Enforcement controls only
whether a missing/non-current approval blocks Generate. This prevents an approved
edit created during rollout from accidentally rendering through the legacy path.

The guided branch runs before legacy clip analysis, intro writing, archetype
selection, and variant selection. If the snapshot is present but invalid, the
Job fails with a guided-story code. It never falls through to montage.

## Core implementation decisions

### One task-owned capability

Add `app.pipeline.guided_story` as the owner of validation, timeline compilation,
media rendering, text materialization, receipt construction, and receipt
verification. The generative orchestrator only detects the approved snapshot,
invokes this capability, persists its one result, and finalizes the Job.

Use `variant_id="guided_story"`, `resolved_archetype="guided_story"`, and
`text_mode="agent_text"`. This gives the result a clear identity while reusing
the existing editable text lane and clean-base reburn behavior.

### Deterministic timeline

- Preserve approved beat order and media order.
- The top-level `duration_s` is the authoritative total. Beat durations are
  approved relative weights. The compiler scales them proportionally, persists
  both approved and resolved values, and rejects a plan if scaling violates the
  direction's readability floor or an available video window.
- Required media means the ordered unique union of
  `story_beats[*].media_ids`. Other snapshot media remains an authorized but
  unused catalog and is not expected in the render receipt.
- Divide each beat's resolved duration across its selected media references,
  with a direction-specific lower bound and deterministic frame rounding.
- A video moment is trimmed within its probed duration; it is never looped or
  silently stretched. An image moment uses subtle Ken Burns motion.
- Every selected media reference must render at least once. A media ID is never
  silently dropped to fit duration; an impossible proposal fails before FFmpeg.
- `fullscreen` produces a full-canvas sequential moment.
- `supporting_card` supports every valid one-to-four-source beat. Each selected
  source receives its own sequential interval as a readable inset over a
  blurred canvas derived from that same source. This is a first-class story
  composition, not the optional post-render overlay lane.
- Direction is compiled deterministically:
  - `guided_story`: 1.4s minimum source moments, approved title plus every
    non-empty thought, restrained crossfades for relaxed/balanced pace.
  - `fast_montage`: 0.8s minimum source moments, hard cuts, compact title and
    thought windows, whole-story music bed.
  - `text_explainer`: 1.8s minimum source moments, longest readable thought
    windows, restrained transitions, and the approved card/fullscreen layout.
- Approved `layout` always wins; direction changes timing and treatment rather
  than silently replacing it.
- Transitions are conservative hard cuts/crossfades selected from direction and pace only;
  no model call may reorder or replace the approved story.

### Text authority and editability

- The approved title becomes a stable `TextElement` spanning the opening beat.
- Every non-empty approved thought becomes a stable per-beat `TextElement`.
- Stable IDs derive from proposal/beat IDs so receipt verification and future
  edits can address them.
- The renderer chooses typography, placement, and motion only. It does not call
  the intro writer or rewrite approved wording.
- Upload the music-mixed, text-free story as `base_video_path` before burning
  text. Existing text editing/reburn routes can then safely update wording or
  style without rebuilding the story.

### Whole-story music and deterministic retries

Build matcher inputs from the ordered selected-media analyses, topics, goal,
direction, and pace. Music matching must see the complete story, not only the
first attached clip. If no confident match is available, the guided story gets
an explicit silent AAC bed and the receipt records `music_applied=false`; a
selected track that cannot be applied is a strict failure.

Before FFmpeg work, persist a versioned task-owned `guided_story_execution_plan`
under the Job lock. It pins compiler version, compiled source/output windows,
selected track and audio window (or explicit no-match), transition policy,
typography/style identity, proposal version, and media digest. Redelivery reuses
this execution plan and validates the pinned track instead of rematching against
a changed library.

Every normalized moment is video-only. Source-video audio is intentionally muted
for this travel-story product path. After story assembly, Nova adds either the
pinned music or a silent stereo AAC bed, so all successful outputs have one
uniform H.264/AAC contract and mixed image/video concat never depends on source
audio compatibility.

### Strict publication receipt

Persist the following on the variant:

- `proposal_version` and `media_digest`;
- `story_timeline` with beat ID, media ID, source window, output window, kind,
  layout, and required flag for each moment;
- authoritative `text_elements`;
- `render_receipt` with expected/actual beat IDs, text IDs, media IDs, duration,
  distinct source count, image/video counts, music result, output probe, and
  verification status.

Before upload/publication, verification requires:

- exact proposal version and digest;
- every approved beat ID exactly once in story order;
- every selected approved media ID in the rendered timeline;
- every required title/thought ID in the burned set;
- finite ordered source/output windows within media duration;
- output duration within a small frame-aware tolerance;
- one valid 1080×1920 H.264 video stream, one AAC audio stream, and non-empty output.

`actual_*` fields come only from completed stage receipts, never from the
compiler's intent. Each exact-generation download records identity and local
kind, each normalized/card moment records its input identity plus probed output
window, and assembly records the moment artifacts it consumed. The Skia guided
burn also emits per-element evidence from its actual generated alpha frames:
non-empty pixel bounds, peak alpha, sampled frame, and bounds inside the canvas
safe area. Zero-duration, transparent, clipped/off-canvas, or missing required
text fails `guided_story_text_missing`. The final output must also differ from
the clean base. Fault injection that drops any media, card, or text stage must
therefore make final receipt verification fail.

Failure uses plain language plus one of: `guided_story_snapshot_invalid`,
`guided_story_media_missing`, `guided_story_media_replaced`,
`guided_story_duration_impossible`, `guided_story_render_failed`,
`guided_story_text_missing`, or `guided_story_receipt_mismatch`.
No result reaches ready and no simple montage is substituted.

## Failure modes and containment

| Failure | Behavior |
|---|---|
| Snapshot absent | Legacy render path, unless enforcement already rejected Generate |
| Asset-only approved proposal | Seed Job identity from first selected validated asset; render all selected refs normally |
| Snapshot malformed or digest mismatch | Fail before any media work |
| Clip/asset object missing or generation changed | Exact-generation download fails; Job remains non-ready |
| Video duration shorter than its assigned moment | Rebalance before render or fail as impossible; never loop silently |
| Image/video FFmpeg step fails | Fail the strict guided variant; no partial publication |
| Supporting-card composition fails | Fail; do not downgrade to fullscreen or remove the card |
| Music match absent | Add silent AAC and record explicit no-music receipt |
| Selected track cannot be downloaded/mixed | Fail if selected; do not publish a silently songless result |
| Title or approved thought missing after compilation | Fail receipt verification |
| Output probe/duration/source receipt mismatch | Fail before upload/publication |
| Cancellation or ownership epoch changes | Existing row locks/tombstone fences reject writes and discard generation outputs |
| Retry after worker restart | Reuse the persisted execution plan; revalidate snapshot/media/track; no rematch or legacy reuse |

## Codepath and test coverage

```text
content_plan_build dispatch
  └─ tests/tasks/test_generative_dispatch.py
       verifies all exact proposal media identities are Job-authorized

generative_build guided branch
  ├─ tests/tasks/test_guided_story_build.py
  │    strict routing, no legacy agents, failure codes, finalization whitelist
  └─ app/pipeline/guided_story.py
       ├─ tests/pipeline/test_guided_story.py
       │    compiler, ordering, timing, IDs, impossible durations, receipts
       └─ tests/pipeline/test_guided_story_ffmpeg.py
            real mixed image/video render, crop, motion, card, text, music,
            duration and ffprobe receipt

status/editor API
  ├─ tests/routes/test_generative_jobs.py
  │    story fields and text/base capabilities survive response shaping
  └─ web Jest/types
       guided story is editable through the existing text lane

rollout/config
  └─ tests/test_config.py
       readiness permits capability+enforcement only after strict renderer ships
```

## Implementation tasks

1. Add typed guided-story compiler/receipt models and pure validation helpers.
2. Authorize all approved asset and clip media in the Job snapshot; keep the
   original storage lanes unchanged. Permit asset-only guided dispatch by using
   a selected validated proposal path as the Job seed instead of requiring a
   legacy attached clip.
3. Add exact-generation download and local-kind validation for every media ref.
4. Add deterministic fullscreen and supporting-card moment rendering using
   existing image/video/FFmpeg helpers and final-output encoder policy.
5. Assemble the ordered base and build whole-story matcher context, then persist
   the complete execution plan under the Job lock before rendering.
6. Mix the pinned music with `require_audio=True`, or add a uniform silent AAC
   bed for explicit no-match.
7. Materialize stable approved TextElements, burn with Skia, and persist the
   clean story base plus final output. Add per-element rendered-alpha visibility
   evidence and strict safe-area validation to the guided burn path.
8. Construct and verify the strict receipt before calling upload/publication.
9. Route guided Jobs before legacy analysis/writers and add strict failure
   mapping. Preserve cancellation, ownership, tracing, and finalization locks.
10. Extend the Python response schema, finalizer whitelist, TypeScript variant
    shape, editor eligibility/capabilities, text reburn preservation, and tests
    so story/receipt fields survive first render and later text edits.
11. Flip `GUIDED_STORY_RENDERER_READY` true only after the strict path and tests
    exist; keep all environment flags false in source and deployment.
12. Update pipeline docs, decision history, changelog, version, and rollout
    instructions.
13. Run focused and full scoped tests, lint/typecheck, prompt replay evals when
    applicable, preship, specialist/adversarial diff review, and open the PR.
14. Stop at the autoship pre-merge approval gate for explicit user approval.
15. After merge, deploy API/worker and frontend with all guided flags off; run
    health/canary checks.
16. Download the authorized Corfu production inputs read-only to a temporary
    directory, render through the production Docker image without production
    writes, save an MP4/contact sheet/decision trace for review, and delete the
    scratch inputs.
17. Enable capability → frontend → enforcement only if the Corfu preview passes
    every acceptance criterion; otherwise leave the release dark and report the
    exact failing receipt.

## Corfu acceptance criteria

- 15–30 seconds.
- At least seven distinct valid Corfu sources, with photos and videos.
- At least three supported topics: food, architecture/town, coast/beaches.
- A visible intro and at least three readable, editable thought moments.
- Every approved required beat and media reference is present in the receipt.
- No single sailboat source dominates the runtime.
- 1080×1920 H.264/AAC MP4, contact sheet, and basic-language decision trace.
- No production data mutation and no silent fallback.

## Not in scope

- Merging the clip-assignment and visual-pool storage lanes.
- Automatically regenerating a proposal after every upload.
- Generating or publishing unreviewed personal opinions.
- Replacing the existing production Corfu output.
- Generalizing the legacy overlay/SFX autoplace systems.
- Redesigning the full video editor or changing unrelated archetypes.

## Rollout and rollback

Ship code and migrations dark. Then enable in order:

1. `GUIDED_EDIT_CAPABILITY_ENABLED=true` on Fly (API restart).
2. `NEXT_PUBLIC_GUIDED_EDIT_ENABLED=true` on Vercel (rebuild).
3. `GUIDED_EDIT_ENFORCEMENT_ENABLED=true` on Fly (API + worker restart).

Rollback in reverse order. Existing approvals, receipts, and rendered Jobs remain
readable with switches off. If a strict render fails, the item exposes the
machine-readable reason and retry; it never receives a legacy fallback video.

## Parallelization strategy

Implementation is sequential because dispatch authorization, compiler output,
renderer receipts, and publication gating are one integrity chain. After the
first coherent diff exists, independent specialist reviews and test groups may
run in parallel. Real Corfu testing runs only after the exact code intended for
release has landed and deployed dark.

## GSTACK REVIEW REPORT

| Dimension | Result |
|---|---|
| Scope | Full PR3 only; storage-lane merge and unreviewed opinions excluded |
| Architecture | Approved snapshot is the render program; no dummy legacy montage |
| Data integrity | Exact generations, stable IDs, digest/version checks, locked finalization |
| Failure behavior | Strict fail-closed codes; never publish a simpler fallback |
| Product quality | Mixed media, approved narrative, editable thoughts, whole-story music |
| Testability | Pure compiler tests plus real FFmpeg and exact Corfu preview |
| Rollout | Dark deploy, explicit three-stage flags, reverse-order rollback |
| User decisions | The supplied Corfu plan authorizes guided story, review, ship, and test |

Verdict: APPROVED FOR IMPLEMENTATION. The design closes the original failure
mode by making completeness machine-verifiable before ready status.

NO UNRESOLVED DECISIONS

## Compatibility addendum — 2026-08-21

The original invariant “guided snapshot present → guided renderer” applies only when the current
PlanItem intent is guided-compatible. The strict guided renderer is intentionally audio-destructive:
it mutes source audio, selects a library track (or silent AAC), and burns approved title/thought
text. It must never be allowed to consume an explicit voiceover or an audio-led format.

The implementation therefore uses one shared, positive guided applicability policy. `montage`,
`day_vlog`, and `single_hero` without an uploaded voiceover may snapshot and render guided stories.
Any uploaded voiceover, plus `narrated*`, `subtitled`, and `talking_head`, selects the native
audio-led program. Route capability/enforcement gates, the lock-owning dispatcher, and the worker
all recompute that policy. Approved guided proposals remain dormant and byte-for-byte unchanged
while native intent is selected; switching back to a compatible intent reactivates them.

Mixed-version Jobs receive a worker defense-in-depth check. A dual-state Job with genuine legacy
clip input ignores the guided snapshot and enters the native resolver. An asset-only Job whose only
clip is the synthetic guided seed fails closed and asks the creator to Generate again, preventing a
partial render from silently dropping approved pool media. This compatibility fence supersedes the
older unconditional “snapshot wins” branch for audio-led intent; guided-only behavior and strict
failure semantics remain unchanged.
