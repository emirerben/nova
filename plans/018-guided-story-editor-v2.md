# 018 — Guided Story Editor V2

## Status

Implementation approved. Autoship branch: `codex/guided-story-editor-v2`.

## Goal

Make approved guided stories honestly and fully editable without mutating their
approval provenance. The editor must support clip add/remove/reorder/split/trim,
transitions, per-clip Looks, soundtrack swap/remove/level/window, text, SFX,
media overlays, visual blocks, motion blocks, orientation, and Nova operations.

Captions, lyrics, automatic speech cuts, intro controls, carousel, camera
effects, smart secondary background music, and original source audio remain
outside this archetype.

## What already exists

- The generic timeline editor already owns slot interaction, undo history,
  transitions, clip pool UI, commit serialization, and virtual preview.
- The generic commit path already validates timeline, music removal, SFX,
  overlays, visual/motion blocks, and atomic Save behavior.
- The guided renderer already pins exact approved media generations, renders
  images/videos, mixes a pinned song or silent AAC, burns text, and verifies a
  strict receipt.
- Copilot already supports clip timing, Looks, music swap/level, operation
  fingerprints, and atomic Director acceptance.

This implementation extends those contracts with a story-native revision and
runtime compiler. It must not route guided stories through montage rendering.

## Architecture

```text
immutable approval + exact approved media pool
                    |
                    v
       guided_story_execution_plan (unchanged)
                    |
Editor Save --------+------> guided_edit_revision v1
  base generation CAS          - revision number + state hash
  revision CAS                  - normalized segment timeline
  atomic validation             - soundtrack state + lane hashes
                    |
                    v
       compile_guided_runtime_plan()
                    |
       +------------+-----------------------+
       |            |                       |
       v            v                       v
 moments/Looks   transitions          projected timed lanes
       +------------+-----------------------+
                    v
 music/silent AAC -> visuals/overlays -> text -> SFX
                    |
                    v
       guided render receipt v2
 approval provenance + effective revision evidence
```

`guided_story_execution_plan` and the approved proposal stay byte-identical.
`guided_edit_revision` lives on the guided variant in `Job.assembly_plan` and is
reset by a new Generate Job. No database migration is needed.

## Canonical timeline contract

1. **Duration source:** the normalized guided revision timeline and its shared
   projection compiler own segment order, overlap, and final duration. UI
   components consume it and never recompute a competing duration.
2. **Ripple:** timed lane endpoints map from baseline segment/offset anchors to
   the draft timeline. Exact boundaries are right-biased. Surviving endpoints
   clamp to frames; fully removed intervals become visible undoable tombstones.
   Continuous music remains anchored to output time zero and is only cropped.
3. **Scrub bounds:** pointer scrubbing, playback, keyboard seeking, ruler ticks,
   playhead geometry, and inverse mapping all clamp to the same projected total.
4. **Resize:** inputs step by 0.1 seconds and normalize to 30 fps. Left trim
   preserves Out; right trim preserves In. Video never stretches/loops. Minimum
   segment duration is 0.1 seconds and final output remains at most 60 seconds.
5. **Undo:** one pointer gesture plus every derived ripple/tombstone is one
   snapshot. Pointer-move previews never write persisted timestamps.
6. **Parity:** shared JSON fixtures run through TypeScript preview projection
   and Python runtime compilation, plus deterministic desktop and FFmpeg tests.

New pool media appends as a 3-second cut, capped by video duration. A source may
be reused through distinct stable segment IDs. Video-only split produces two
valid source windows. At least one segment must remain. Transition overlap
reduces duration and is capped at `min(requested, 0.3s, 30% of each neighbor)`;
overlaps below 0.1 seconds become cuts.

## API and state changes

- Add default-false `GUIDED_STORY_EDITOR_V2_ENABLED`. It gates new writes, not
  reads or rendering of already-persisted revisions.
- Add operation-level capability objects for clips, music, lanes, orientation,
  and Nova while retaining legacy booleans for old clients.
- Extend guided timeline GET with revision number/hash and every exact-generation
  media ref in the approval snapshot, including unused media.
- Save validates the complete revision, checks revision + render-generation CAS,
  persists atomically, and queues exactly one full render.
- Revision v1 stores approval version/digest, monotonic number, canonical state
  hash, renderer/effect schema versions, normalized segments/audio, lane hashes,
  and base generation.
- Receipt v2 records approval provenance, revision evidence, exact sources and
  generations, effective timeline/transitions/Looks/music, lane hashes, output
  duration/canvas/codecs, and uploaded object generations. Receipt v1 remains
  valid for untouched legacy jobs.
- Stable errors cover stale revision, invalid/empty/out-of-bounds timeline,
  unapproved or replaced media, incompatible motion runtime, unavailable music,
  and guided render failure.

## Renderer and editor behavior

- Compile a typed runtime plan from immutable approval + validated revision.
- Apply server-authoritative Look presets to both image and video moments.
- Render in this order: revised assembly/Looks/transitions, main music or silent
  AAC, visual/motion/media overlays, guided text topmost, then SFX audio.
- Music removal is explicit and produces silent AAC. Music level controls only
  the main soundtrack; original source audio stays muted.
- Text reburn is fast only when the clean-base revision hash matches.
- Fix the dedicated guided orientation endpoint and reject legacy guided custom
  effects unless they are part of the hashed revision.
- Add manual transition controls and make every inspector control consume its
  specific capability. Presets come from the server, never a hardcoded list.
- Generalize virtual preview to the shared revision projection, including SFX.

## Nova behavior

- Add `trim_clip_start`, `trim_output_start`, and `remove_music`.
- `trim_output_start(N)` consumes assembled time from the front; explicit clip
  requests target that clip. `set_clip_in` remains a source-window slip.
- `remove_music` is distinct from `set_mix(0)`.
- Normalize before staging every operation. A no-op creates no state, history,
  or receipt and returns `no_effect`.
- Preserve the model explanation when no edit applies while stating that the
  draft stayed unchanged.
- Director treats zero capabilities, disabled route, stale revision,
  capability mismatch, and server/model failure as separate states.
- Bump `EDIT_COPILOT_PROMPT_VERSION`; add replay and live-with-judge goldens.

## Failure modes

| Failure | Handling | Verification |
|---|---|---|
| Second tab saves stale draft | Revision + render-generation CAS rejects atomically | Route concurrency tests |
| Approved object replaced/deleted | Exact generation/existence check fails before FFmpeg | Source identity tests |
| Trim deletes a timed element | Visible undoable tombstone; Save receipt lists it | Projection + EditorShell tests |
| Transition exceeds neighbors | Canonical compiler clamps or converts to cut | TS/Python parity fixtures |
| Music disappears after Save | Stable revision-scoped failure; no silent fallback track | Music render tests |
| Worker redelivery races newer Save | Generation token and revision hash prevent stale publish | Task redelivery tests |
| Text reburn uses old base | Base revision mismatch forces full render | Worker policy test |
| Feature gate rolls back | New writes lock honestly; persisted revisions still render | Flag compatibility tests |
| Director result is stale | Discard and refresh current revision | Hook tests |

## Test plan

```text
API Save -> validate/CAS -> persist revision -> worker -> receipt
  |           |             |                  |        |
  |           |             |                  |        + v1/v2 compatibility
  |           |             |                  + exact source/music generations
  |           |             + concurrent Save/redelivery
  |           + every timeline/music/lane error
  + full approved pool and granular capabilities

Editor -> gesture/Copilot -> projection -> undo -> Save -> refreshed variant
  |          |                |          |       |
  |          |                |          |       + stale/network/render UX
  |          |                |          + one gesture = one snapshot
  |          |                + every lane + music exclusion
  |          + trim/remove/no-op/stale operations
  + honest enabled/disabled desktop controls
```

- Backend: schema/CAS, approved source pool, add/remove/reorder/split/both trims,
  output trim, source bounds, transition math, Looks on images/videos, music
  swap/remove/level/window, lane ownership/runtime hashes, receipt/redelivery,
  legacy byte compatibility, and no montage fallback.
- Frontend: deterministic desktop guided fixture for all interactions,
  projection/scrub/preview, one-step undo/redo, payloads, honest locks, music
  exclusion, Nova receipts, and Director error taxonomy.
- E2E: desktop Playwright flow for trim, Look, music removal, Save, and stale CAS.
- Eval: replay plus live-with-judge cases for single/output trim, explicit clip
  trim, remove versus mute, no-op/stale, and compound edit.
- Gates: `make verify-editor-timeline`, guided FFmpeg tests in the production
  Docker runtime, targeted Jest/Playwright, Ruff, backend pytest, frontend lint
  and typecheck, and `scripts/preship-check.sh`.

## Rollout

Ship with the V2 gate off. Confirm API/worker and Vercel are healthy, test broad
operations on a fixture, enable the backend gate, then verify the supplied Dia
item: In `1.0`, Out `5.5`, duration `4.5`; apply a Look; remove music; repeat
trim/removal through Nova; Save and inspect the exact revision receipt/canary.

## NOT in scope

- Captions, lyrics, speech cuts, intro, carousel, and camera effects: separate
  archetype semantics and render paths.
- Original clip audio: guided stories intentionally use soundtrack or silence.
- Media uploaded after approval: requires a new approval snapshot.
- Persistence across a newly generated Job: new Generate deliberately resets the
  editor revision against its new approval/render baseline.

## Parallel implementation

| Lane | Modules | Depends on |
|---|---|---|
| A | API guided revision, validation, capabilities | — |
| B | Web capability, timeline projection, inspector controls | API contract |
| C | Copilot/Director prompt and operation contracts | capability shape |
| D | Guided runtime renderer and receipts | A |
| E | Integration and parity fixtures | A-D |

Luna agents may implement A/D, B, and C concurrently after the shared contracts
are fixed. Integration and verification remain sequential in the primary worktree.

## Implementation Tasks

- [x] **T1 (P1)** — Add guided revision schema, validator, CAS, source pool, and granular capabilities.
- [x] **T2 (P1)** — Add canonical runtime compiler, Looks/music/lane rendering, and receipt v2.
- [x] **T3 (P1)** — Add editor projection, honest controls, transitions, preview, and undo parity.
- [x] **T4 (P1)** — Add Nova trim/remove operations, no-op truthfulness, Director taxonomy, and evals.
- [x] **T5 (P1)** — Add backend/frontend/E2E/FFmpeg parity matrix and run required gates.
- [x] **T6 (P2)** — Update guided pipeline documentation, env reference, VERSION, and changelog during ship.

## GSTACK REVIEW REPORT

| Review | Trigger | Why | Runs | Status | Findings |
|--------|---------|-----|------|--------|----------|
| CEO Review | `/plan-ceo-review` | Scope & strategy | 0 | — | User directly selected full parity and immutable approval provenance |
| Codex Review | `/codex review` | Independent 2nd opinion | 1 | CLEAR | Three Luna traces agreed on the backend boundary and test requirements |
| Eng Review | `/plan-eng-review` | Architecture & tests (required) | 1 | CLEAR | Scope accepted; Job-local revision, separate runtime plan, shared projection |
| Design Review | `/plan-design-review` | UI/UX gaps | 0 | — | Honest disabled-state and desktop interaction requirements are included |
| DX Review | `/plan-devex-review` | Developer experience gaps | 0 | — | Existing editor/test infrastructure is reused |

**CROSS-MODEL:** Backend, frontend, and Nova reviews agreed that flipping the legacy capability alone would corrupt or ignore guided edits.
**VERDICT:** ENG CLEARED — ready to implement through autoship.
NO UNRESOLVED DECISIONS
