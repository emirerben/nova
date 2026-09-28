# Kria agent runtime v2

Kria runtime v2 turns the existing creation thread, Main Creator Agent, editor,
and render pipeline into one durable creator workflow. It is a domain controller
for Nova editing, not a general assistant and not a second renderer.

## Golden path

```text
user message
  -> append semantic thread event + durable turn
  -> async planner receives a trusted bounded snapshot
  -> deterministic policy validates tools, risk, evidence, and revisions
  -> read/reversible strategy or editor draft OR exact approval
  -> typed receipt
  -> observed assistant response
  -> existing editor commit or Job dispatcher/render/review when approved
```

The model may propose intent. Server code owns authorization, revisions, risk,
idempotency, storage resolution, execution, and completion language. A plan is
never evidence that work happened. Only a settled receipt can support “changed,”
“rendered,” or other completed-action copy.

## State ownership

| Concern | Authority |
|---|---|
| Creator-visible transcript | append-only `CreationThreadEvent` |
| Runtime selection | immutable `CreationThread.runtime_version` |
| One user message lifecycle | `CreatorAgentTurn` |
| Reversible working edit | immutable, revisioned `CreatorEditDraft` |
| Consequential consent | `CreatorAgentApproval` |
| Tool result and side-effect identity | `CreatorAgentExecution` |
| Rendered media | existing `PlanItem` and `Job` |

Runtime-v1 threads keep their existing controller and projections. A thread never
changes runtime owner because a flag changes during its lifetime.

New threads also remain runtime v1 when the field is omitted. During internal
testing only, a client may request `runtime_version: 2` with no inline message
after `KRIA_RUNTIME_V2_ENABLED=true`; its first creator message then goes through
the durable `/turns` endpoint. This explicit opt-in prevents an old web build
from silently entering the new controller during a staggered deploy. Legacy
`/messages` and `/actions` mutations reject v2 threads, so they cannot bypass
the new turn ledger or render-consent boundary.

## Tool contract

`app.kria.registry.KRIA_TOOLS` is the only runtime-v2 tool registry. Each entry
owns its argument/result schema, version, capability, risk, execution mode,
retry policy, failure class, and executor binding. The model manifest and checked
contract snapshot are generated from it.

To add a read-only tool:

1. Define strict Pydantic argument and result models.
2. Add one deterministic executor and one registry entry.
3. Add a whole-turn fixture and expected receipt/semantic response.
4. Run `make verify-kria`.

Reversible draft tools must additionally validate the authoritative draft and
write a new revision atomically. Approval-required tools must never execute from
the planner output; deterministic policy creates a pinned approval first.

After a playable generation exists, the planner uses the existing versioned
Edit Copilot parser against a server-derived snapshot. The snapshot contains
only bounded text, caption, timeline, capability, and opaque target metadata;
storage paths and signed URLs are excluded. `draft.apply_editor_ops` compiles
the portable launch subset into Nova's full-section `EditorCommitRequest`:

- timeline reorder, remove, trim, split, duration, transition, and look;
- text/title content, timing, and parity-verified style fields;
- caption text, replacement, timing, emphasis, and presentation settings;
- music removal and mix changes;
- add unused sources, image duration, and image stacking;
- separately rendered, revision-fenced reviewed speech-cut candidates.

The compiler is non-mutating. Approval consumption calls the existing editor
commit or speech-cut validator while the exact Job is locked, records the new
generation in the accepted execution, commits, and only then publishes. A lost
broker acknowledgement becomes `outcome_unknown`; it is never blindly retried.

## Conversation contract

Every assistant turn has one value class: decision, question, action, progress,
review, or recovery. Repeating the brief is not a value class. Internal planning,
tool selection, retries, and worker callbacks are projected into quiet status or
one generation lifecycle artifact, not conversational bubbles.

Failures use `KriaProblem`: stable code, phase, useful message, retryability,
recovery action, trace ID, current revision, and safe target identities. The
creator sees the problem, what stayed safe, and one next action. Operators can
follow `thread -> turn -> approval/execution -> Job/variant/generation/task`.

Runtime-v2 HTTP reads are bounded. `GET /creation-threads/{id}` returns the
newest page in chronological display order; `after_sequence=N`
returns later events and `before_sequence=N` pages backward. The `/delta` path
remains a compatibility alias, while internal editor refreshes request
`projection=full`. Pending approval cards fetch
`GET /creation-threads/{id}/approvals/{approval_id}` to obtain the current
server fingerprint, revisions, expiry, and consequence copy before deciding.
Framework validation, authentication, rate-limit, and unexpected failures on
these routes use the same `KriaProblem` envelope as runtime failures.

## Creative Brief, router and receipts (KRI-188)

Behind `KRIA_CREATIVE_BRIEF_ENABLED` (or a `KRIA_CREATIVE_BRIEF_USER_IDS`
allowlist entry); off is byte-identical. Code: `app/kria/brief.py` (ledger,
router, request rendering), `app/kria/brief_checks.py` (receipts, reply).

- **Ledger.** `creative_brief_versions` (migration 0111) is append-only, one
  version per turn (`UNIQUE(thread_id, version)`, idempotent per `source_turn_id`).
  Requirements have a server-assigned id (`r<n>`), `kind`, `scope`, `literal`
  (creator-written text only) or `description`, `facts`, and `status`. A later
  requirement with the same `(kind, scope)` supersedes the earlier one. The
  Main Creator (prompt v37) only proposes `brief_updates`; unparseable entries
  are dropped, never fatal. Versions are written by the turn-completion
  transaction under the thread lock, after the revision fence, so a requeued
  turn never persists one.
- **Router.** `route_requirements` is deterministic. `replan` when a new
  requirement is `order`/`select`, a per-clip text requirement arrives and the
  plan has no per-clip text lane, the kinds are mixed, there is no editable
  render, or the message asks to redo it ("do it again based on my prompt").
  Otherwise `editor_ops`. With a render present the Main Creator runs first (it
  extracts the requirements) and the copilot only when the router says
  `editor_ops`. `_validate_draft_plan` rejects an editor-ops-only plan when the
  route is `replan`.
- **Request.** With the flag on, the Main Creator and the clip-intent planner
  read `render_brief_request(brief)`, and the approval dispatch passes it as
  `creator_request` instead of the draft summary.
- **Receipts.** Each draft turn attaches `requirement_receipts`
  (`met | partial | not_possible`, reason, `inferred`) to its `draft_applied`
  event payload. Checks: per-clip text coverage, order basis
  (`ordering_basis` / `ordering_fallback_clip_ids` when present), duration
  within +/-10%, literal text (whole-word, Turkish-aware match). `clip:<id>`
  is checked against that clip only; editor payloads carry no per-clip
  structure, so per-clip text on an editor edit is `partial` ("can't verify"),
  never `not_possible`. A requirement with no checker is `partial`, and that
  neutral "can't verify" never flips the reply to a failure: the model's
  summary is dropped only when a requirement a checker actually judged is not
  met. `KriaObservedTurnResponse.requirement_receipts` is reserved for the
  observed-turn projection; today receipts ride on the `draft_applied` event
  payload and `GET /creation-threads/{id}/brief` returns the current
  brief with the newest receipt per requirement (empty when the flag is off).
- **Cost.** With a render present the flag adds one Main Creator call to
  editor-op turns (the planner extracts requirements before the router runs).
  If that call fails, a plain edit falls back to the legacy copilot-first path
  with no brief update, so a Main Creator outage never blocks a simple edit.
- **Known gaps (deferred).** No retraction ("drop the title") yet; a render
  dispatched later reads the latest brief version, not the version the approved
  draft was checked against; requirements past the 40-live cap are dropped
  silently; `read_creative_brief` scans only the newest 30 assistant events.

## One montage plan (KRI-190)

Behind `MONTAGE_UNIFIED_PLAN_ENABLED` (or a `MONTAGE_UNIFIED_PLAN_USER_IDS`
allowlist entry; comma-separated or JSON, same shape as the brief allowlist); off
is byte-identical. Code: `app/pipeline/unified_montage.py` (pure planner),
`_run_phone_unified_montage_job` in `app/tasks/generative_build.py` (worker),
`_unified_montage_review` in `app/tasks/kria_runtime.py` (reply).

**Why.** A runtime-v2 phone approval has no approved guided proposal, so the worker
fell to the plain phone-montage lane. That lane shows one intro hook, orders by
matcher score, ignores the brief, and rejects every portrait-canvas job at the
item's default `landscape_fit="fit"` (`phone_plan_unsupported: letterboxed landscape
fit ...`, job 94c4c865). It also 422'd every chat text edit (`unsupported_phone_edit`)
because it never wrote a `guided_story_execution_plan`.

**What happens with the flag on.** In `_run_generative_job_impl`, a phone job in the
montage family (`GUIDED_EDIT_FORMATS`), no recorded voiceover, no `guided_edit`,
plans a guided fast montage and then runs the existing `_run_phone_guided_job`:

1. Inputs: the job's clip order (`all_candidates.clip_paths`), the phone bindings
   (durations, dimensions), the item's clip assignments (capture time, place,
   stored landmark facts), the thread's latest Creative Brief, the strategy's
   confirmed copy. `CLIP_FACTS_*` gates the facts; landmark guesses use the
   existing `enrich_clip_facts` (45s cap, fail-open).
2. `plan_unified_montage` returns an `EditProposalSnapshot` (`fast_montage`, exact
   `fast_cuts`, `video_reuse_policy="once"`, new `clip_labels`) persisted as the
   job's immutable `guided_edit`, plus a small `unified_montage` record.
3. The guided compiler, its validators, the guided phone compiler and the guided
   phone editor apply unchanged; text-only chat saves recompile normally.

**Decisions the planner owns** (nothing else):

- *Order*: capture time only when the brief has an `order` requirement with a
  capture key and two clips carry a time; otherwise the creator's pinned order
  (`all_candidates.creator_clip_order`, a revision keeps a reordered timeline,
  basis `creator_order`), else attachment order. The basis and
  the clips that fell back are recorded (`ordering_basis`,
  `ordering_fallback_clip_ids`). A selection order is never silently overridden.
- *Per-clip text*: only when the brief (or confirmed shot labels / verified clip
  intents) asks for it. Grounding, in priority: creator words (`clip:<id>` literal,
  positional `shot_labels`, creator-text intent) > verified intent > clip fact
  (`creator` > `landmark` > `place`, most specific part) > brief `start`/`end` for
  the first/last clip. No grounding means no label; the receipt says partial.
  Landmarks are `inferred` and listed for correction.
- *Reading time*: `min_display_s = clamp(0.8 + 0.06 * chars, 1.2, 3.0)`; a labelled
  cut lasts at least that long, capped by the clip itself (then the receipt says
  the label is too short to read). Unlabelled cuts are 1.2s. A requested length
  (`brief timing.duration_s`, else `strategy.target_duration_s`) grows cuts (up to the
  clip's length; without a stated length, at most 3s each) or shrinks unlabelled
  cuts (not below 0.8s); readable text wins.
- *Title*: confirmed strategy title > brief title literal > brief global literal
  (+ route) > facts ("20K Run · Arnavutköy → Eminönü", `title_from_facts`) >
  `Montage`. Never a model hook, never place text nobody asked for. Creator-written
  labels keep up to 120 characters; fact/model labels are cut at 60.
  Text stays NFC; nothing is folded to ASCII.
- *Typography*: Fraunces has no "→" glyph and the phone lays out from exact glyph
  ids (a missing glyph fails the whole recipe), so `skia_font_covers` picks the
  first bundled font (creator font, Fraunces, DM Sans) that covers every string;
  only uncovered characters are dropped when none does.

**Not covered / known gaps.** The worker plans from the thread's *latest* brief, not
the approved version (a redelivery before the plan is pinned can pick up a newer
one; receipts are dropped from the reply when `brief_version` differs). With the
flag on, a phone montage renders source audio only (no matched music bed, beat-snap
or hero intro). A single clip under 3s fails as "too short to make a montage".

**Snapshot lane.** `EditProposalSnapshot.clip_labels` (omitted when `None`, so
stored snapshots and approval hashes are byte-identical) and the
`clip_labels is not None` branch of `guided_story._text_elements` draw the title at
the top and one label per cut at the bottom. `TimelineClip.text` exists in the
recipe schema but the guided compiler, like every guided plan, emits positioned
`text_layers`, which the iPhone engine already renders.

**Receipts.** The worker computes receipts from what it put in the plan
(`brief_checks.plan_facts_from_unified_montage`: clip ids, labels, inferred labels,
title, total length, ordering basis, too-short labels) and stores them on
`unified_montage.requirement_receipts`. When the render is ready the observer's
`assistant_review` event is composed from them (`reply_from_receipts`) and carries
`requirement_receipts`; with the brief off or no record it is the unchanged default
review.

**Planner prompt.** `main_creator` v39 always records route/distance/activity
`facts` and an `order` requirement when the creator names a sequence.

**Not in this change.** Deleting `compile_phone_montage_plan` and its intro-only
lane follows once a human has compared the new renders on a device. Cloud
(non-phone) montage is untouched.

Guards: `tests/pipeline/test_unified_montage.py`,
`tests/tasks/test_unified_montage_dispatch.py` (the East Run repro, flag-off pins,
chat edit), `tests/kria/test_unified_montage_receipts.py`,
`tests/evals/test_main_creator_evals.py::kri190_route_facts_and_order`.
Rollback: `fly secrets set MONTAGE_UNIFIED_PLAN_ENABLED=false MONTAGE_UNIFIED_PLAN_USER_IDS=
--app nova-video` + restart the worker; in-flight jobs keep their pinned plan.

## Render consent

Every initial render and rerender requires a separate, expiring approval pinned
to the current draft, manifest, ownership epoch, Job/variant/generation, and
expected revisions. “Make this” may prepare the approval but cannot consume it.
Approval consumption and exact editor/Job commit happen atomically; broker
publication follows the commit through the existing deterministic Job dispatcher
or editor rerender task. Accepted editor executions persist enough dispatch
metadata for the reconciler to publish the same generation after process death.

## Local proof

From a prepared worktree:

```bash
make kria-replay FIXTURE=nermin-matcha-update
make kria-replay FIXTURE=render-approval-required
make kria-replay FIXTURE=ambiguous-render-dispatch
make kria-replay FIXTURE=stale-project-replan
make verify-kria
```

The replay uses no model, storage, broker, or renderer. It proves snapshot,
planner contract, policy, typed tool execution, receipt, and observed semantic
response across happy, approval, ambiguous-dispatch, and stale-state paths. Real
model/media/render validation remains a separate rollout gate. `make verify-kria`
also checks the generated registry/API types, the 240-case structural all-format
corpus, editor reducer, runtime state machine, HTTP envelopes, admin trace, and
migrations. It writes duration/flakiness metadata to
`test-results/kria-verify.json`; feature flags stay off until the live gates pass.
