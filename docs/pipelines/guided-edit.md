# Guided edit proposal pipeline

Guided edit separates creative approval from rendering. It is the review contract between all
uploaded plan-item media and the strict story renderer. Planning and approval create the contract;
the story assembler consumes the approved Job snapshot directly.

## Product flow

The primary clip-only path is **Upload clips → Create video → AI analyzes and builds in the
background → rendered video**. Direction confirmation is not part of that path; **Plan with Kria**
is an optional advanced tool for creators who want to shape the edit before building it.

1. The creator uploads main clips and any supporting photos/videos.
2. **Plan with Kria** opens an optional editorial conversation. The creator can describe the result in ordinary
   language (for example, “a reflective diary about the food and old town” or “fast highlights,
   little text”). `EditGuideAgent` reflects what it understood, asks at most one useful follow-up
   at a time, and persists a typed direction, goal, pace, and target length.
   When footage exists but the creator has supplied no brief, Nova instead uses one concrete,
   media-aware direction for automatic planning. The hypothesis includes direction, pace, target
   duration, text density, audio role, rationale, and buildability warnings. The primary Generate
   path does not pause for creator confirmation; the optional planner may still expose the
   hypothesis for creators who want to shape the edit before building it.
3. `POST /plan-items/{id}/edit-proposal/draft` assigns stable IDs to legacy clip assignments,
   creates a token-fenced attempt, and queues `draft_edit_proposal`.
4. The task waits for existing visual-pool analysis, analyzes every attached clip without current
   metadata, and resolves the immutable storage generation for every source.
5. The server keeps the complete accepted media set in the proposal snapshot, then gives
   `EditProposalAgent` a deterministic render-capable shortlist of at most 32 sources plus
   creator-written context. Prompt-visible sources use short per-call aliases (`m001`, `m002`, …),
   which the parser resolves back to exact owned IDs; opaque storage identities never need to pass
   through the model. The agent proposes an ordered title and story beats. It must use every source
   when there are at most three, may leave one out when there are four to six, and must use at least
   seven when more are available. A confirmed quick-photo/long-video timing profile makes that
   source floor target-capacity-aware, so a short target never requires more distinct sources than
   its 0.5s photo and 1.5s usable-video minimums can fit; eligible photos and videos are still both
   required when both kinds are available. It may not invent personal experiences. There is no artificial
   duration floor: `draft_edit_proposal`
   computes a feasible-duration estimate from the analyzed media (`feasible_guided_duration_s` —
   real video durations summed, plus a fixed per-image credit) and clamps the brief's requested
   duration down to it (`adapt_target_duration_s`) before calling the agent, so a short clip still
   yields an edit instead of failing schema validation trying to stretch it. Footage under 3s is
   infeasible for a guided story — the agent is never called; the attempt fails with
   `guided_edit_infeasible` naming the actual footage length.
   If guided-story or fast-montage planning terminates after its bounded retries, the task builds a
   neutral deterministic proposal from the complete owned set and asks the strict renderer to
   validate it before saving or auto-approving it. Fast-cut recovery uses distinct,
   non-overlapping source windows. Guided recovery uses metadata-free server copy so generated
   analysis cannot become visible text. `text_explainer` does not use this recovery because its
   semantic copy cannot be safely rebound to arbitrary footage.
6. The item page shows combined photo/video thumbnails. The creator can continue the same
   conversation with requests such as “put food first,” “make it slower,” or “use less text.”
   Conversational revisions may reorder and rewrite editorial fields. They may also move the
   already-assigned media between existing beats through short aliases; the server validates that
   every assigned source remains present exactly once, then rejoins the real identities.
   Creator-written thoughts remain authoritative. Direction, goal, pace, title, order, layouts,
   and thoughts also remain manually editable. AI-written thoughts carry an **AI draft** label.
7. **Approve plan** saves corrections and approves them using compare-and-swap against
   `expected_proposal_version`.
8. Generate revalidates every storage generation and snapshots the exact approved proposal and
   media identities into `Job.assembly_plan.guided_edit` while holding the established
   Plan → Persona → PlanItem → Job locks.
   The web and API generation gates treat selected media in that approved snapshot as footage,
   including asset-lane-only stories; neither requires a duplicate legacy clip attachment. The
   lock-owning dispatcher still revalidates every object before it creates the Job.

## Stored envelope

`PlanItem.edit_proposal` is nullable JSONB with `schema_version=1`:

- `proposal_version`: increments on every user-visible state mutation.
- `generation_attempt_id`: prevents an older analysis task from overwriting a newer attempt.
- `planning_started_at`: records when heavy analysis actually begins so Creator reconciliation
  does not expire a queued or still-within-budget proposal attempt.
- `media_digest`: SHA-256 of canonical lane, stable ID, object path, storage generation, kind,
  and content hash. Editorial ordering is intentionally excluded.
- `status`: `briefing`, `analyzing`, `drafting`, `draft`, `approved`, `stale`, or `failed`.
- `guidance`: optional direction provenance and confirmation state. Provenance is
  `creator_explicit`, `ai_inferred`, or `creator_confirmed`; the optional planner may store
  inferred guidance as `awaiting_direction_confirmation` until version-safe confirmation.
  Automatic Generate does not pause at this state; legacy automatic attempts are resumed by
  the next Generate request.
- `approval_mode`: `"user"` (explicit approval, the default/`null`) or `"auto"` — set at reservation
  time (`begin_proposal_attempt`) and carried onto `last_approved.approval_mode` by `approve_proposal`
  so it survives a later reservation overwriting the envelope. See "AI-designs-by-default" below.
- `design_fallback`: set to the failure code that triggered a clip-only legacy-montage fallback
  (auto-design only); `null` otherwise, including a normal failure with pool assets present.
- `brief`: requested direction, goal, pace, duration, confirmed `creator_request`, and optional
  typed `mixed_media_timing` profile.
- `conversation`: up to ten durable creator/Kria exchanges, including reply suggestions. The
  thread survives reloads and proposal-generation retries.
- `brief_ready`: Kria's signal that it has enough direction to plan. **Build this edit plan** is
  available whenever a conversation reply is not in flight. Once ready, the route returns
  `409 brief_already_ready` for additional briefing turns, enforcing the two-follow-up ceiling;
  the creator can build the plan and revise the resulting draft instead.
- `draft`: the current editable proposal.
- `last_approved`: immutable approval metadata plus the approved snapshot. It is retained when
  media changes so the creator can compare before planning again.
- `failure`: plain-language code, message, and retryability.

Each draft and approval also persists `output_orientation` plus a plain-language
`output_orientation_reason`. When the creator has not chosen a format, Kria considers only media
selected by approved story beats. Each source votes portrait or landscape by its approved screen
time; near-square or missing aspect metadata is neutral. A tie follows the first selected
non-square source, and a story with no usable aspect metadata stays portrait. Unused uploads do not
affect the decision.

The two storage lanes remain independent:

- `clip` refs point to `PlanItem.clip_assignments[*].media_id`.
- `asset` refs point to `PlanItemAsset.id`.

Both become the same proposal-level `MediaRef`; no storage migration or duplicate object is needed.
Pool assets promoted into the clip lane are de-duplicated by object path for story selection.

## Staleness and concurrency

Adding, removing, or replacing a clip or pool asset marks the proposal stale. Changing creator
context does too because the draft may have relied on that context. Staleness retains both draft
and last approval but Generate refuses it.

Draft edits and approvals require `expected_proposal_version`. A stale browser tab receives
`proposal_conflict` instead of silently overwriting newer work. Analysis writes are additionally
fenced by attempt ID, status, media digest, and a second storage-generation check after analysis.
The analysis task also carries the plan ownership epoch, so transferring a plan invalidates queued
work. Repeated **Plan edit** clicks reuse an active attempt, and new attempts are limited to three
per minute per client IP. Conversation turns are limited to twelve per minute per authenticated
creator.

The mutation contracts are:

- `POST /plan-items/{id}/edit-proposal/conversation` with `expected_proposal_version` and a natural
  language `message` returns the updated item (`200`). Before analysis it updates the typed brief.
  During review it may return a revised draft, which always requires approval again.
- `POST /plan-items/{id}/edit-proposal/confirm-direction` with `expected_proposal_version`, the
  hypothesis fingerprint, and optional direction/pace/duration overrides confirms an inferred
  hypothesis and continues proposal planning. A stale version or fingerprint returns `409` and
  never dispatches work.
- `PATCH /plan-items/{id}/edit-proposal` with `expected_proposal_version` and a complete `snapshot`
  returns the updated item (`200`) or a structured `409` conflict/stale response.
- `POST /plan-items/{id}/edit-proposal/approve` with `expected_proposal_version` returns the approved
  item (`200`) or a structured `409` draft/conflict/stale response.

Media identities and analysis are server-owned. PATCH may change direction, goal, pace, duration,
title, beat order, layouts, and thoughts, but it cannot replace media metadata.

The conversation endpoint follows the same trust boundary. Under the item lock it first persists a
short-lived, token-fenced single-flight reservation, then releases the transaction before calling
the model. Duplicate tabs therefore cannot spend a second model call. The final write reloads under
lock and must own both the token and proposal version. Responses expose only safe `thinking` or
`retry required` state—not the token—so a reload disables generic planning while the creator's
words are still being interpreted. An abandoned reservation expires after 60 seconds (`EDIT_CONVERSATION_ATTEMPT_TTL_S`, matching the
Next.js proxy's `maxDuration=60` so a client-visible timeout and the server reservation expire
together) and can be reclaimed by resending the direction. The conversation endpoint additionally
requires media before reserving an attempt — `clip_assignments` non-empty or a registered pool asset
in `queued`/`analyzing`/`ready` — otherwise it returns `409 media_required` rather than spending a
model call on advice the item page can't act on yet. Draft revisions must preserve every existing beat ID exactly
once; the route rejoins server-owned media IDs and retains creator-authored thoughts verbatim.

## Conversational rollout

`GUIDED_EDIT_CONVERSATION_ENABLED` is a separate API-read writer gate. Keep it false while this
release rolls across every API and worker: the web continues to show the existing direction, goal,
pace, and duration form. After every backend reader understands the `briefing` status, enable the
flag and restart the API. `PlanItem.guided_edit_conversation_available` then switches the already
deployed web UI to conversation without a second build. Rolling the flag back restores the form;
existing conversation history remains readable and is preserved by later planning attempts.

Do not roll the application binary back past `0.33.3.0` while `briefing` rows exist. The reviewed
compatibility conversion is in `docs/runbooks/conversational-edit-rollback.md`.

When enforcement is enabled, Generate returns one explicit 409 code:

- `proposal_required`
- `proposal_draft`
- `proposal_stale`
- `proposal_analyzing`
- `proposal_failed` — the last attempt ended in `status="failed"`. Previously fell through to the
  generic `proposal_draft` message ("Approve the edit plan before generating"), which was wrong for
  a plan that was never actually drafted.

The same checks run in the synchronous dispatch helper, so direct or delayed task delivery cannot
bypass the route. With `GUIDED_AUTO_DESIGN_ENABLED` on and the guided capability available,
Generate reserves and drafts instead of requiring a proposal review (see below). Plain 409s remain
for missing media and the independent voiceover/photo business rules.

## AI-designs-by-default (GUIDED_AUTO_DESIGN_ENABLED)

Product decision (2026-08-18): asking the creator for direction stays optional. If they never open
the planner, Kria designs the edit and Generate still works in one click, as long as media exists.

`GUIDED_AUTO_DESIGN_ENABLED` defaults **true**. When it is on, the guided capability is available,
and the item has media (`clip_gcs_paths` non-empty or at least one `ready` pool asset), Generate
uses the automatic design path instead of requiring a proposal review:

1. `generate_item` runs its two hard, guided-edit-independent business-rule checks FIRST — the
   narrated-voiceover requirement and the photos-need-collage-preset requirement — before auto-design
   gets a chance to intercept the request. Auto-design's own clip-only montage fallback (step 3)
   dispatches through the exact same legacy path these checks guard, so letting auto-design run
   first would silently re-open both: a narrated item with no voiceover could montage-fallback
   captionless, and raw photos could skip the collage requirement (2026-08-18 adversarial review,
   P1-2).
2. `generate_item` (`routes/plan_items.py::_maybe_auto_design_generate`) re-locks the item and
   branches on the CURRENT proposal state under that lock — it does not unconditionally clobber
   whatever is there:
   - a live `conversation_attempt` (creator mid-reply with Kria, regardless of proposal status) →
     idempotent current-state response. Generate racing a live turn must never void it (P2-3).
   - `analyzing`/`drafting` (an attempt — auto or manual — already in flight) → idempotent
     current-state response, never a duplicate reservation/task.
   - `approved` (reached by a race with a manual approval, or an already-approved auto-design
     attempt, landing between the caller's initial unlocked read and this lock) → never reset —
     dispatches directly from this fresh, locked read. Returning "no error" and letting the caller
     fall through to its own stale pre-lock `item` would incorrectly re-raise the ORIGINAL 409 for a
     proposal that is actually approved now (P2-2d).
   - `draft` (a reviewable draft already exists — the creator's own edits, or an earlier auto-design
     draft) → **auto-finalizes THAT draft** instead of discarding it for a redraft: revalidates its
     media identity (`_proposal_media_is_current`, the same check `approve_item_edit_proposal` uses),
     approves it under `expected_proposal_version` CAS, then dispatches synchronously — never spends
     a second agent call on a story already worth approving (P2-2c).
   - `None`/`failed`/`stale`/`briefing` → reserves a **fresh** attempt
     (`begin_proposal_attempt(item, brief=current.brief if current else None, approval_mode="auto")`)
     and enqueues `draft_edit_proposal` — but PRESERVES the creator's already-stated
     direction/goal/pace/duration from that prior attempt rather than resetting it to
     `ProposalBrief()` defaults (P2-2a/b). No media at all on the item → falls through to the
     ordinary 409 (nothing for auto-design to work from).
3. `draft_edit_proposal` no longer takes an `auto_finalize` kwarg — it reads the intent straight off
   the persisted envelope (`_attempt_wants_auto_finalize`: `approval_mode == "auto"` on the matching
   `generation_attempt_id`) before running its normal analyze → draft flow. A successful draft is
   then immediately approved (`approve_proposal`; `approval_mode` stays `"auto"` on the resulting
   `last_approved` snapshot). See "Celery deploy-skew" below for why the kwarg was removed.
4. `_dispatch_after_auto_design` runs exactly once after the attempt settles (success or any
   failure), **after** the approval commit and outside the PlanItem row lock — dispatch never
   publishes while holding it:
   - `approved` → dispatch normally through `dispatch_item_render_for`.
   - `failed` with `design_fallback="main_creator_fail_closed"` → **no fallback**, even when the
     item has clips and no pool assets. Main Creator persists this sentinel before enqueue so old
     and new workers in a rolling deploy both preserve the confirmed creative direction. The next
     Creator session poll reconciles that exact generation attempt to the matching failed execution
     receipt instead of waiting for the dispatch lease to expire.
   - `failed`, zero registered pool assets, `clip_gcs_paths` non-empty → dispatch anyway with
     `bypass_guided_edit_gate=True` (a new escape hatch on `_dispatch_item_render` /
     `dispatch_item_render_for`, used **only** by this caller) to route around the very enforcement
     check that would otherwise 409 a `"failed"` proposal — the legacy clip render path, exactly as
     if guided edit had never been approved. `design_fallback` is set to the failure code that
     triggered it. `_dispatch_item_render` independently RE-COUNTS registered pool assets under the
     lock it already holds before honoring the bypass — the caller's zero-pool check ran in a
     separate transaction, so a pool asset registered in that gap must still be caught, not silently
     dropped behind the montage (`guided_edit_bypass_unsafe` outcome; P2-4).
   - `failed`, pool assets present → **no fallback**. Registered pool media is never silently
     dropped behind a clip-only render (the 2026-08-15 pool-media incident invariant) — the failure
     stays exactly as persisted, retryable.
   - dispatch itself fails (`publish_failed`, `guided_edit_bypass_unsafe`, etc.) → the proposal stays
     in whatever state already committed (`approved`, or `failed`+`design_fallback`). The next manual
     Generate click either dispatches directly (approved reads identically regardless of
     `approval_mode`) or re-triggers auto-design (failed) — never wedged.

**Duration feasibility is renderer-aware end to end** (P2-1). `feasible_guided_duration_s` credits a
video its own probed duration only when that duration clears the renderer's own per-moment minimum
(`GUIDED_STORY_MIN_MOMENT_S = 1.4`, shared with guided_story.py's direction policy); a video
with no duration, a zero duration, or a duration below that minimum earns ZERO credit — it previously
fell into the image-credit branch and was overestimated. `guided_feasibility_threshold_s(media_count)`
scales the infeasibility gate with how much media is being asked to share the story
(`GUIDED_STORY_MIN_MOMENT_S * min(3, media_count)`, floored at `MIN_GUIDED_DURATION_S`) instead of a flat
constant. The agent's own `+/-5s` output tolerance (`EditProposalAgent.parse`) checks its output
against the TARGET it was given, not against real footage — with a small target (e.g. the
`MIN_GUIDED_DURATION_S=3` floor) that tolerance window alone could still accept an output nobody
validated against the true footage cap, so `draft_edit_proposal` independently rejects
`output.duration_s > floor(feasible_duration_s)` rather than trusting the agent's tolerance alone.

**Celery deploy-skew (P2-6):** `draft_edit_proposal` does not accept an `auto_finalize` kwarg — a task
kwarg is a rolling-deploy hazard (an old worker consuming a new producer's message, or the reverse,
either crashes on an unknown kwarg or silently drops the intent). The intent is read straight off the
DB row instead. A worker mid-deploy that predates auto-finalize just produces a normal, unapproved
draft (degraded: the creator manually approves it) instead of either crashing or silently never
auto-finalizing.

`PlanItemResponse.guided_edit_auto_design` mirrors the flag (same pattern as
`guided_edit_available`/`guided_edit_conversation_available`) so the frontend can gate the Generate
button's enabled state and hint copy on it; absent/false on an old API keeps today's strict-gate
behavior on a newer frontend build (deploy-skew safe). The frontend's own media check
(`plan-generate-gate.ts`'s `hasGenerateMedia`) additionally counts a `status === "ready"` pool asset
reported up from `AssetPool` (via its `onAssetsChanged` callback) whenever auto-design is available —
otherwise a pool-only item (no `clip_gcs_paths`, no approval yet) was unreachable from the Generate
button even though the backend would happily design from pool media alone (P2-5); this only applies
when `guided_edit_auto_design` is true, so a pool-only item with the flag off/undefined keeps today's
exact gating.

Large mixed-media planning has a 1,440s soft task limit and 1,500s hard limit. Clip analysis is
checkpointed after each completed source with generation and attempt fences, so retries reuse ready
analysis rather than restarting the complete upload set. The Creator execution receipt lease is
1,650s, below the 1,900s broker visibility timeout; a still-valid proposal task is reconciled rather
than expired while it remains within its task budget.

Typed mixed-media attempts run on the `creator-guided-jobs` Celery queue. Production's worker and
both local development worker commands must drain that queue alongside the existing queues; this
keeps long Creator planning available during rolling deploys without competing with unrelated jobs.

The render-registration watchdog (`RENDER_REGISTER_TIMEOUT_MS`, 15 min) re-arms itself continuously
while `edit_proposal.status` is `analyzing`/`drafting` — the design phase alone can legitimately run
past 15 minutes under transient-analysis retries, and no render Job even exists yet to register. It
only starts its real countdown once design settles and a render is actually expected to appear.

**Kill switch:** `GUIDED_AUTO_DESIGN_ENABLED=false` restores strict enforcement for new Generate
calls (they just 409 again) — NOT byte-identical to pre-auto-design rollback: the `proposal_failed`
409 mapping is unconditional regardless of this flag, and it does **not** retroactively un-approve
proposals an earlier auto-design attempt already approved; see
`docs/runbooks/conversational-edit-rollback.md`.

## Rollout

The four established proposal switches, the new confirmation gate, and the V2 editor write gate
default false:

1. `GUIDED_EDIT_CAPABILITY_ENABLED` exposes proposal and visual-pool APIs (API restart).
2. `NEXT_PUBLIC_GUIDED_EDIT_ENABLED` exposes the item-page flow (Vercel rebuild).
3. `GUIDED_EDIT_CONVERSATION_ENABLED` switches the compatible item page from the typed brief form
   to conversation after every API and worker can read `briefing` proposals (API restart).
4. `GUIDED_EDIT_ENFORCEMENT_ENABLED` requires an approval at Generate (API + worker restart).
5. `GUIDED_EDIT_DIRECTION_CONFIRMATION_ENABLED` only exposes the optional planner's legacy
   confirmation state; it never blocks the primary Generate path (API + worker restart).
6. `GUIDED_STORY_EDITOR_V2_ENABLED` permits story-native timeline and Copilot writes after the
   compatible API, worker, and web are deployed (API restart).

After merge, deploy the API/worker and frontend with all six switches still off. Then download the
authorized Corfu inputs read-only into temporary storage, render them through the production Docker
image without production writes, review the MP4, contact sheet, decision trace, and strict receipt,
and delete the scratch inputs. Only after that preview passes should rollout enable capability, then
the frontend, conversation writes, the V2 editor, confirmation, and finally enforcement. The code-owned
`GUIDED_STORY_RENDERER_READY` pin is true only because guided Jobs now render from their approved
snapshot and verify stage receipts before publication; startup still rejects enforcement without
capability. Roll back in reverse order. Existing approvals, conversations, and rendered Jobs remain
readable with every switch off.

## Strict story rendering

When capability is enabled, a current approved proposal is snapshotted even before enforcement is
enabled. Enforcement controls only whether Generate rejects an item without current approval. This
prevents staged rollout from sending an already-approved story through the legacy montage path.

The worker pins `guided_story_execution_plan` before FFmpeg work. It contains the compiler version,
proposal version and digest, ordered source windows, direction/pace policy, exact music object
path/generation and window (or explicit no-match), output orientation and its explanation,
typography identity, and approved text. Redelivery reuses this plan rather than rematching a changed
music library.

Compiler version 3 adds the approved output canvas while retaining version 2's timing allocator.
Version 2 gives every approved moment its direction-specific minimum, caps each video at
its real usable duration (including any transition overlap), and redistributes the rest of the beat
to surrounding photos or longer videos. It still preserves the approved beat and total duration and
fails before FFmpeg only when the complete selected set cannot fill the requested time. Persisted
version 1 and 2 plans keep their original portrait canvas, typography, and timing rules on
redelivery, so a rolling deploy cannot reinterpret queued or already-rendered work.

New fast-montage proposals use `fast_cuts` as their authoritative planning contract rather than
story chapters. Each cut pins the analyzed source window, output duration, hook/build/payoff role,
and a hard-cut transition. The planner defaults to the strongest visual first, roughly 0.8–1.2
seconds per cut, minimal generated text, and optional beat alignment when analyzed beat timestamps
are available. A strong source may appear more than once, but its selected video windows may not
overlap. Validation fits otherwise-valid cuts to the server-owned target duration with bounded
tail-first adjustments and fails closed if the requested runtime cannot be reached without reusing
source time. Source variety remains required. When the typed `mixed_media_timing` profile is present,
photos hold for 0.5–0.8s, usable videos hold for 1.5–3.0s when their source permits, boundaries are
hard cuts, both eligible media kinds are included, and the total remains within 0.15s of the target.
Without that profile, legacy fast-montage cuts remain capped at 1.2s and retain their previous timing
behavior. Legacy fast-montage snapshots without `fast_cuts`
remain readable and render through the older story-shaped contract.

The renderer exact-generation downloads every source selected by a beat. Unselected catalog media
remains authorized but is not required in the output. Photos and videos become sequential full-screen
or supporting-card moments. The downloaded bytes stay untouched for the source receipt; every photo
is separately decoded, EXIF-corrected, and normalized to a render-safe JPEG or alpha-preserving PNG
before FFmpeg applies its still-image loop. This is required for HEIC/HEIF, whose dedicated FFmpeg
demuxer rejects the image2-only loop option. Photos receive a subtle zoom, relaxed/balanced story directions use
duration-compensated crossfades, and fast montages use hard cuts. Video source audio is muted and the
finished base receives either the pinned track or silent stereo AAC. The approved title and thoughts
become editable TextElements on a clean text-free base. New plans use Fraunces for titles and DM Sans
for thoughts, warm-white text with a lime accent, a soft shadow, and zero stroke. A text-only edit
reburns from that base and refreshes its rendered-alpha evidence. The editor may also request an
orientation-only rebuild: the worker reuses the pinned story media, timing, music, and current
validated text, renders a new clean base and final output on the requested canvas, and issues a new
strict receipt. It never enters the legacy montage path. Other legacy editor operations fail closed.
Approved title/thought IDs must remain present exactly once; changing wording is allowed and a V2
direction replacement may blank an unchanged AI-authored thought while preserving its identity.
Creator-authored or creator-rewritten text is never cleared by the replacement.

## Render-program compatibility

An approved proposal is a dormant editorial envelope, not an unconditional render-program switch.
The server computes one applicability policy from the current item intent at every boundary:

```text
explicit voiceover OR narrated/subtitled/talking_head format
        └── native audio-led renderer (guided snapshot ignored)
montage/day_vlog/single_hero with no voiceover
        └── strict guided-story renderer
```

This is required because the guided renderer deliberately removes source audio and replaces it
with a matched library track (or silent AAC), while its visible title/thought layers are editorial
observations, not a speech transcript. Audio-led items therefore hide guided capability flags,
skip proposal enforcement/auto-design, require real clip inputs, and preserve the approved proposal
unchanged so it can become applicable again if the creator later chooses a guided-compatible intent.
The lock-owning dispatcher recomputes the policy before validating or stamping a snapshot, closing
the route/dispatch race. A worker receiving an older dual-state Job skips the guided snapshot and
uses the native resolver; an asset-only synthetic guided seed fails closed with an actionable
"Generate again" reason rather than rendering an incomplete native edit. No proposal migration or
digest change is needed.

For narrated-family content-plan items with uploaded voiceover, the existing narrated assembler is
selected even if a stale/mismatched format token says `montage`; it transcribes the uploaded audio,
force-aligns captions, and keeps music/text prework disabled. Generic non-narrated legacy formats
with a voiceover retain the existing generic voiceover behavior; the product must use a narrated
format when transcript captions are required.

## Guided Story Editor V2

This section applies only when the render-program compatibility policy selects the strict
guided-story renderer. Audio-led items remain on their native renderer and ignore guided editor
revisions.

`GUIDED_STORY_EDITOR_V2_ENABLED` is a default-false write gate for story-native editing. Legacy
stories without a `guided_edit_revision` remain byte-compatible and continue through the strict
approved-story renderer. Turning the gate off blocks new V2 writes but does not prevent a worker
from finishing or redelivering a revision that was already persisted.

The approved proposal and `guided_story_execution_plan` remain immutable. A V2 Save atomically
validates and stores one normalized `guided_edit_revision` on the guided variant, compares both the
revision number and base render generation, and enqueues exactly one render. The revision pins its
approval digest, stable segment/media IDs, every approved source generation (including unused
sources), normalized timeline and music state, lane hashes, renderer/effect schema versions, and a
canonical state hash. Direct timeline and orientation writes use the same CAS tokens; raw revision
JSON is not a public write surface.

The story compiler owns trim, split, add/remove/reorder, transitions, Looks, orientation, and music
swap/remove/window/level. It never adds guided stories to the montage allowlist. Video windows do
not stretch or loop; images are timed stills; all segments normalize to 30 fps; and transition
overlap is capped by the smaller of the requested duration, 0.3 seconds, or 30 percent of either
neighbor. The approved source pool is revalidated against owned, ready media records and exact
storage generations on Save and render. New uploads require a new approval.

Timed text, SFX, media overlays, visual blocks, and motion blocks are projected from stable segment
anchors into the revised output clock. Fully removed intervals become persisted, restorable
tombstones and are reported in the Save receipt. Music stays anchored to output time zero and is
cropped to the revised duration. Explicit music removal produces silent stereo AAC and records
`music_removed`; it is distinct from retaining the track at zero gain.

V2 outputs carry receipt schema 2: immutable approval provenance, revision/state hashes, exact
source order and generations, effective timing/transitions/Looks/music, lane hashes, tombstones,
duration/canvas/codecs, and output storage generations. Receipt schema 1 remains valid for untouched
legacy stories. Text remains topmost, and text-only fast reburn is allowed only when a clean base is
proven to match the active revision hash; otherwise the worker performs a full story render.

Ready status requires a verified `render_receipt`: exact beat/media IDs, per-moment FFmpeg evidence,
per-text rendered-alpha bounds inside the canvas, duration, the selected 1080×1920 or 1920×1080
H.264 canvas, AAC audio, and the exact uploaded base/output object generations. Live render attempts
use a row-locked heartbeat lease;
duplicate deliveries neither render concurrently nor mark the owning task finished. Any missing
approved layer fails with a guided-story reason. There is no montage fallback.

Content-plan collection responses expose no proposal snapshots or signed preview URLs. The item
detail and proposal mutation responses carry the full review payload, keeping list reads bounded.

## Verification

- State/CAS/digest tests: `tests/services/test_edit_proposals.py`
- API Generate codes + auto-design kill-switch pin + idempotency:
  `tests/routes/test_plan_item_generation.py`
- Draft-attempt crash regressions, duration-adaptation clamp, large mixed-media
  snapshot preservation, renderer-validated deterministic recovery, and the
  `auto_finalize` state machine (approve+dispatch, dispatch failure, clip-only
  montage fallback, pool-assets-present no-fallback):
  `tests/tasks/test_edit_proposal_build.py`
- Source-diversity, target-capacity-aware mixed-media floors, 32-source alias
  shortlisting/resolution, distinct-chapter, non-overlapping fast-cut repair,
  unprobed-video kind handling, and observation-only draft guards:
  `tests/agents/test_edit_proposal_agent.py`
- Direction confirmation CAS, explicit-direction bypass, and fast-montage proposal routes:
  `tests/routes/test_edit_proposal_routes.py`
- Fast-cut schema and persisted compiler compatibility:
  `tests/schemas/test_edit_proposal_fast_montage.py` and `tests/pipeline/test_guided_story.py`
- Honest Copilot outcomes, title aliases, and server-planned direction replacement:
  `tests/test_edit_copilot.py` and `src/__tests__/edit-copilot/`
- Replay/live+judge travel cases: `tests/evals/test_edit_proposal_evals.py`
- Frontend review flow: `src/__tests__/plan/edit-proposal-card.test.tsx`
- Conversation agent/evals: `tests/agents/test_edit_guide_agent.py` and
  `tests/evals/test_edit_guide_evals.py`
- Strict compiler/fault injection: `tests/pipeline/test_guided_story.py`
- Real mixed-media FFmpeg render: `tests/pipeline/test_guided_story_ffmpeg.py`
- V2 revision/timeline compiler: `tests/schemas/test_guided_edit_revision.py`
- Shared web/runtime projection parity: `tests/fixtures/guided-story-parity/`,
  `tests/schemas/test_guided_story_parity.py`, and
  `src/__tests__/guided-story-parity.test.ts`
- Atomic editor commit and CAS: `tests/routes/test_editor_commit.py`
- Desktop acceptance fixture: `src/apps/web/e2e/guided-story-editor.spec.ts`

Run the editor parity gate before shipping timeline changes:

```bash
make verify-editor-timeline
```

Run the focused eval in replay mode:

```bash
cd src/apps/api
pytest tests/evals/test_edit_proposal_evals.py tests/evals/test_edit_guide_evals.py -v
```

Run it against Gemini and the judge before changing the prompt:

```bash
NOVA_EVAL_MODE=live pytest tests/evals/test_edit_proposal_evals.py \
  tests/evals/test_edit_guide_evals.py -v --with-judge --allow-cost
```
