# Chat-First Automatic Speech Cleanup

**Status:** implementation complete; production rollout pending
**Date:** 2026-09-05
**Depends on:** chat-first creation (#956), mixed-gap detector V2 (#960), chat/editor durability (#962)
**Incident sample:** `generative-jobs/2d0178f1-c6c3-4136-8002-c2f1cced4ea9/variant_1_subtitled.mp4`

## Outcome

Analyze eligible speech automatically in chat-first creation, independent of what
the user types, and require one explicit pre-render choice when cleanup candidates
exist. The chosen action and the exact analyzed source must be committed atomically
with render dispatch so Nova never promises cleanup in prose while executing an
`off_v1` job.

The user-facing contract is:

1. Kria checks the active foreground narration as soon as its source and format are
   stable. This includes embedded speech in a video and an uploaded or recorded
   Narrated voiceover.
2. If cleanup moments are found, Kria shows one consequential decision in the
   existing confirmation artifact before output begins.
3. The user chooses either **Clean up and create** or **Keep speech and create**.
4. The selected choice, source fingerprint, detector version, and cut plan are
   snapshotted into the Job.
5. Ready output reports what actually happened. A required cleanup failure never
   degrades silently into an uncleaned success.

An audio-only Narrated project receives the same automatic analysis and boxed
evidence artifact. Its decision remains `null` and the create choices are not sent or
shown until at least one video clip exists. Because Nova remains a video creator,
this project does not invent an audio-to-video visual treatment.

## Confirmed Root Cause

The supplied production Job did not miss a filler inside the V2 detector. Cleanup
never ran:

- `speech_cleanup_requested=false`
- `speech_cleanup_contract=off_v1`
- `silence_cut_disabled=true`
- no cleanup trace or outcome

Chat-first PlanItems default `speech_cleanup_enabled` to `false`; chat creation,
format selection, and the generic `generate` action do not expose or set explicit
cleanup consent. The dispatcher therefore stamps `off_v1`, and the worker skips the
detector. Production also has mixed-gap treatment disabled (`mode=off`, rollout 0%),
so merging #960 alone could not activate V2.

The exact #960 synthetic incident is green: legacy misses the two tokenless islands;
V2 removes them atomically. The bug is the missing chat-to-render contract and dark
rollout, not a reproduced regression in its detector logic.

## Product Invariants

- Chat prose is never executable consent. The same offer appears for equivalent
  eligible media whether the user says “remove ums” or never mentions speech.
- Neither choice is preselected.
- A render with detected candidates cannot start until a current explicit choice is
  supplied, except the deliberate recovery action **Create without cleanup**.
- Consent binds to the current cleanup-policy fingerprint, not just a PlanItem ID.
- Any mutation that changes the resolved active narration identity, media generation,
  timing window, format/audio policy, or detector policy invalidates the analysis
  and choice. Adding or reordering visual-only footage while an unchanged voiceover
  remains active does not invalidate it.
- Every chat and editor mutation that can change the active narration source goes
  through one row-locked PlanItem media mutation service. No route, controller, or
  task may assign clip, voiceover, format, or audio-mode fields directly.
- Metadata-only edits and same-source chat revisions preserve the choice.
- Cleanup targets exactly one server-resolved active foreground narration source:
  the active uploaded/recorded voiceover, otherwise the pinned embedded-audio speech
  spine used by the renderer. Background and inactive audio are never folded into
  the finding count or cut plan.
- Standalone narration audio is eligible for analysis and the boxed evidence UI, but render
  dispatch remains blocked until at least one video clip is present. Until then no
  choice/generate request is sent and the persisted decision remains `null`.
- `required_v1` fails closed. No Ready state may imply cleanup was applied when it
  was skipped or failed.
- Captions are generated from the same timing/cut contract and remain synchronized.
- Once an analysis is presented, its timed words and CutPlan are authoritative.
  Rendering validates and applies that snapshot and must not call Whisper,
  `silencedetect`, or the cleanup detector again.

## Information Architecture

```text
/plan chat workspace
├── latest confirmed creative direction
├── speech analysis / one unresolved cleanup decision   ← single visual anchor
│   ├── status or evidence summary
│   └── create action paired with the explicit choice
├── quiet chat composer                                 ← always available, tertiary
└── after render
    ├── authoritative cleanup receipt
    └── Play · Download · Open editor
```

Constraint rule: before render, show only (1) the direction being created, (2) the
unresolved speech decision, and (3) the actions that resolve it. Do not repeat the
user prompt, assistant paraphrase, and render narration around the same card.

## Primary Decision Artifact

Use `ChatArtifactCard` inside the existing pre-render confirmation position in
`ChatCreationWorkspace`. The card earns its boundary because it contains the only
decision that changes the spoken performance.

```text
┌────────────────────────────────────────────────────┐
│ Speech cleanup                                     │
│                                                    │
│ Clean up 5 speech moments?                         │
│ Kria found 4 filler sounds and 1 long pause.       │
│ Cleaning them removes about 2.8 seconds, and       │
│ captions stay in sync.                             │
│                                                    │
│ ┌─────────────────────┐ ┌────────────────────────┐ │
│ │ Clean up and create │ │ Keep speech and create │ │
│ └─────────────────────┘ └────────────────────────┘ │
└────────────────────────────────────────────────────┘
```

Visual treatment:

- Light product surface: white canvas, zinc text/borders, restrained lime accent.
- Match the approved format-selection artifact: one `rounded-2xl` white container,
  existing zinc border and subtle `shadow-sm`, with two equal bordered option boxes.
  Do not add another decorative wrapper, gradient, amber warning block, or
  ornamental icon tile.
- The title carries primary hierarchy; the category count and estimated duration are
  supporting evidence, never transcript snippets or confidence theater.
- Buttons use outcome language, not **Yes**, **No**, or **Render this direction**.
- Neither option is preselected. Both start as equal neutral boxes; focus, hover,
  pressed, saving, and disabled states use the existing chat control language.
- Major page headings may use Fraunces; this utility decision card stays in the
  existing sans-serif product type.

## Interaction State Matrix

| State | What the user sees | Available action |
|---|---|---|
| Not eligible | No cleanup artifact; normal confirmation remains | Create video |
| Queued/running | **Checking for filler sounds…** with truthful indeterminate motion; no fake percentage | Keep chatting; creation waits |
| Findings | Candidate count, category summary, and estimated removed duration | Clean up and create / Keep speech and create |
| No findings | Quiet line: **Speech checked. No cleanup suggested.** | Create video; current analysis ID is sent with choice omitted |
| Audio-only Narrated | Normal analysis/no-findings evidence plus **Add at least one video clip to make your video.** | Add video; choice/create actions are hidden and decision stays `null` |
| Retryable analysis failure | **Kria couldn’t check the speech.** No claim about whether fillers exist | Retry speech check / Create without cleanup when video is otherwise eligible |
| Nonretryable analysis failure | Specific replace/record guidance; no fake retry | Replace narration / Create without cleanup when video is otherwise eligible |
| Analysis stale | Prior local choice clears; checking state resumes for the changed source | Wait or keep chatting |
| Saving choice | Chosen button shows **Starting cleanup…** or **Creating video…**; both actions disable | Idempotent retry/reconcile after ambiguity |
| Rendering with cleanup | Existing progress artifact may say **Cleaning speech…** only when backed by server state | Existing cancel/retry policy |
| Transient cleanup application failure | Failure artifact says cleanup did not complete; no uncleaned Ready card | Retry the same immutable cleanup snapshot / Create without cleanup |
| Snapshot mismatch | Failure says the source changed; no uncleaned Ready card | Invalidate and analyze current source; do not retry or reanalyze the accepted snapshot |
| Ready, applied | Existing Ready card plus **Speech cleanup applied · 5 moments removed** | Play / Download / Open editor |
| Ready, checked no change | **Speech checked · no cleanup suggested** | Play / Download / Open editor |
| Ready, declined | Quiet **Speech kept as recorded** receipt | Play / Download / Open editor |
| Ready, unchecked bypass | Quiet **Created without checking speech cleanup** receipt; never “checked” | Play / Download / Open editor |

## User Journey

| Step | User does | Intended feeling | Design support |
|---|---|---|---|
| 1 | Chooses a speech-bearing format and attaches media | Momentum | Analysis starts automatically; no special wording required |
| 2 | Shapes the creative direction in chat | In control | Composer stays usable while the speech check runs |
| 3 | Reaches confirmation | Informed, not blocked by mystery | One card explains findings and the exact consequence |
| 4 | Chooses clean or keep | Agency | Two direct outcome actions; no preselected default |
| 5 | Waits for output | Confidence | Progress only claims backend-confirmed work |
| 6 | Reviews the cut | Trust | Ready receipt distinguishes applied, no-change, declined, and failure |
| 7 | Revises in editor | Continuity | Same-source rerenders inherit the immutable contract without nagging |

Time horizons:

- **5 seconds:** the user sees one clear choice and knows creation has not begun.
- **5 minutes:** the result and captions match the choice, and failures offer a safe
  recovery instead of a misleading success.
- **5 years:** Nova has an auditable consent and analysis record that can evolve with
  detector versions without rewriting old Jobs.

## Responsive and Accessibility Contract

- Desktop keeps the existing workspace hierarchy. Before Ready, the decision card
  sits in the conversation column; after Ready it remains in the 420px chat rail.
- At 390px and at 200% zoom, actions stack vertically, full width, in decision order.
  At wider widths they may share a row with `flex-wrap` and a 160px minimum width.
- Every action is at least 44px high and respects the existing bottom safe area.
- Give the artifact a semantic heading or `fieldset`/`legend`; current visual
  `CardTitle` alone is not a heading.
- `ChatCreationWorkspace` owns one visually hidden polite status announcer for
  checking, completion, and failure transitions. The card itself has no nested live
  region. Do not steal focus or repeat every polling update.
- Keyboard order follows reading order. Visible focus is required on both actions.
- State is communicated by text and structure, never color alone. Body text is at
  least 16px with WCAG AA contrast.
- Reduced-motion users receive a static checking treatment.

## Backend Architecture

```text
eligible PlanItem mutation
        │
        ├── cleanup-policy fingerprint
        ▼
SpeechCleanupAnalysis row ── dedicated speech-analysis worker ── analysis engine
        │                                      │
        │ current projection                   └── timed words + silences + V2 CutPlan
        ▼
chat confirmation artifact
        │ exact analysis ID + explicit choice + expected revision
        ▼
row-locked atomic action
        ├── persist decision
        ├── mirror PlanItem compatibility flag
        └── dispatch Job with immutable analysis snapshot
                                               │
                                               ▼
                               render validates and applies snapshot
                               (no Whisper/detector rerun)
                                               │
                                               ▼
                                      authoritative outcome receipt
```

### Durable analysis state

Add a dedicated `SpeechCleanupAnalysis` table scoped to a PlanItem. Do not store the
analysis lifecycle or private timed-word payload in `PlanItem` JSONB. The record
contains:

- active narration source kind (`voiceover | embedded_spine`), stable media identity,
  object generation, applicable trim/window, source/policy fingerprint, and detector
  version
- `queued | running | ready | no_findings | failed`
- attempt/lease token and timestamps so retries are idempotent and late workers
  cannot overwrite a newer claim
- `dispatched_at` and `next_dispatch_at` make post-commit publication recoverable
- `superseded_at` marks historical rows without deleting their audit record
- private, versioned analysis payload: exact timed words and safety signals,
  serialized CutPlan, and detector diagnostics
- bounded public result: candidate count, category counts, estimated removed
  milliseconds, and sanitized diagnostic receipt
- stable failure code
- explicit `clean | keep_original | create_without_cleanup | null` decision and
  decision timestamp; bypass is never overloaded onto `keep_original`
- uniqueness across PlanItem, source/policy fingerprint, and engine version

Database access stays indexed and bounded:

- A unique index on `(plan_item_id, source_policy_fingerprint, engine_version)` is
  the idempotency boundary for analysis creation.
- A partial unique current-detail index on `(plan_item_id) WHERE superseded_at IS
  NULL` both enforces and resolves at most one current analysis row per item without
  scanning history; invalidation supersedes the prior row in the same media-mutation
  transaction.
- A partial lease-sweeper index covers `lease_expires_at` for only `queued`/`running`
  rows, allowing bounded batches to reclaim abandoned work without scanning settled
  history.
- A partial dispatch-reconciler index on `(next_dispatch_at, created_at)` for
  unpublished `queued` rows lets Celery Beat recover commit-before-publish failures
  in bounded order.

`PlanItem.speech_cleanup_enabled` remains the compatibility/execution mirror. Its
existing `false` default must not mean the user declined; only the current analysis
record can distinguish “not asked” from “keep original.” Raw paths and transcript
content remain private and are omitted from ordinary PlanItem/thread projections.

### Active narration source

Introduce one server-owned `resolve_active_narration_source()` boundary shared by
eligibility, fingerprinting, analysis dispatch, confirmation, and render dispatch:

- When an uploaded or recorded voiceover is active, it is the sole cleanup source.
- Otherwise, resolve and pin the embedded-audio speech spine that the applicable
  Talking-to-camera or Narrated renderer will use.
- Do not analyze background, ducked, inactive, or supporting-clip speech.
- An audio-only voiceover can reach `ready`, `no_findings`, or `failed` analysis
  state and appear in chat. It cannot dispatch a Job until a video clip exists.
- Media registration verifies and captures the storage generation immediately,
  including `voiceover_generation` and the clip assignment/manifest identity. The
  task signs that captured generation; it never performs a task-time “latest object”
  lookup.
- Any change that alters the resolved source identity or generation creates a new
  fingerprint and clears the current decision.

### One analysis engine

Extract analysis and application into separate reusable boundaries. Create a typed,
Job-independent speech-cleanup analysis engine with explicit input/output models
(source identity and local media, timing window, policy/version in; timed words,
safety signals, CutPlan, diagnostics, and bounded receipt out). The engine must not
import `generative_build`, Celery tasks, ORM models, or Job state. Instead, move the
current reusable transcription/silence/V2 orchestration out of task internals; thin
preflight and legacy-shadow adapters call the engine and own persistence.

The preflight adapter alone runs the authoritative analysis. The render-side
application service hydrates the immutable Job snapshot, validates its source/policy
fingerprint, and applies that exact CutPlan. It must fail closed on a missing or
mismatched required snapshot and must never silently re-detect under a new rollout
assignment. Delete or fence direct render-task entry points so future render paths
cannot accidentally bypass this boundary.

### Automatic scheduling and invalidation

- Persist `queued` before enqueueing. Enqueue retry uses the same unique row.
- Schedule after eligible media attach/remove, format selection, audio-mode changes,
  voiceover upload/recording/replacement, and embedded-spine changes. Do not write
  or enqueue from a GET response.
- Expand the existing shared clip setter into one row-locked PlanItem media/source
  mutation service used by both chat and editor paths. It owns clip attach, detach,
  replacement and reorder; voiceover attach, detach and replacement; format changes;
  and audio-mode changes.
- Inside the same transaction, the service computes before/after active-source
  fingerprints, updates the media fields, invalidates stale cleanup analysis and
  consent, and returns a typed preflight scheduling intent. Its shared post-commit
  dispatcher—not individual chat/editor call sites—publishes that intent only after
  the caller's transaction commits; no queue publish occurs while the row lock or
  transaction is open. The durable Beat reconciler below closes the
  commit-before-enqueue crash window.
- Add a sole-writer regression guard that fails when protected PlanItem media/source
  fields are assigned outside the mutation service's explicit allowlist. Pair it
  with chat/editor parity tests so new mutation paths cannot skip invalidation or
  post-commit scheduling.
- A late task may finish its historical row but cannot become current after the
  PlanItem fingerprint changes.
- Celery Beat runs a bounded reconciler over unpublished `queued` rows and expired
  `running` leases using `FOR UPDATE SKIP LOCKED`. It republishes due queued work or
  reclaims abandoned work without concurrent sweepers duplicating ownership.
- Claim and terminal writes are compare-and-swap operations over analysis ID,
  attempt token, and current source/policy identity. The lease duration is derived to
  exceed the task hard limit plus clock-skew margin; tasks use `acks_late`, so worker
  loss before acknowledgement converges through redelivery or the indexed sweeper.
- Existing eligible chat threads enter `not_started` lazily; no eager backfill.

### Dedicated execution lane

Add a dedicated `speech-analysis` Celery queue and Fly worker process group rather
than sharing either the concurrency-one render queue or the existing visual-analysis
worker. Route every preflight task explicitly.

The worker uses a generation-aware storage signing primitive to resolve a short-lived
read URL pinned to the exact captured object generation and streams that URL directly
into FFprobe/FFmpeg. Local storage performs an equivalent generation/identity check
before opening bytes. It must not download
the full video first and must not compute a bytewise hash of the video; the trusted
storage identity/generation plus the exact renderer-owned narration window forms the
media identity. FFmpeg extracts only that window into a bounded 16 kHz mono analysis
artifact. A central policy defines and tests the maximum analyzable duration, output
size derived from duration/sample format, probe/extraction/silence subprocess
deadlines, transcription deadline, and Celery soft/hard limits. The analyzed window
must exactly match the renderer's pinned narration policy, and task limits stay below
broker visibility timeout.

Every subprocess runs without an unbounded shell, terminates its process group on a
deadline, and captures bounded diagnostics. Signed URLs are never persisted or
logged. The temporary audio artifact and any partial output are removed in `finally`
on success, typed failure, unexpected exception, cancellation, and soft timeout.
The worker reports queue delay, run duration, input window/output bytes, memory/high-
water failures, retries, and stuck leases. Start with conservative concurrency and
scale this worker independently from rendering.

### Typed failure and recovery contract

Only explicitly typed operational failures are converted into persisted analysis
failure state. Each exposes a stable public code and server-owned `retryable` flag;
provider messages, stack traces, and exception text remain private.

| Public code | Retryable | Meaning / next action |
|---|---:|---|
| `source_temporarily_unavailable` | yes | Retry the same current analysis |
| `audio_extraction_timeout` | yes | Retry; alert if the bounded worker repeatedly times out |
| `transcription_unavailable` | yes | Retry after provider/transport recovery |
| `analysis_timeout` | yes | Retry under the same fingerprint and a fresh lease |
| `unsupported_media` | no | Replace the narration source |
| `no_speech_track` | no | Replace/record narration or Create without cleanup |
| `snapshot_mismatch` | no | Fail closed, invalidate, and analyze the current source |
| `internal_error` | yes | Show generic recovery copy while engineering investigates |

Catch only a typed `SpeechCleanupOperationalError` family at the worker boundary.
Unexpected exceptions—including programming errors such as `TypeError`,
`AttributeError`, `AssertionError`, `ImportError`, and `NameError`—must be reported and
re-raised so Celery/error monitoring records a task failure; never normalize them
into a successful no-findings result or silently swallow them behind a generic
business-state fallback. An outer fenced task-failure hook or lease reconciler may
publish the opaque `internal_error` projection after that exception escapes; it must
not consume or replace the exception. Retryability is server-owned policy, not a UI
guess derived from the code string.

Recovery depends on the failed boundary:

- Retryable preflight failure offers **Retry speech check**; retry idempotently
  reclaims or creates the current-fingerprint analysis and never revives a stale
  source.
- Nonretryable preflight failure asks the user to replace/record the source; explicit
  **Create without cleanup** remains available only when rendering is otherwise
  eligible.
- **Create without cleanup** after a failed/unfinished check stamps `off_v1` with a
  `bypassed_unchecked` receipt. It is not `declined` and must never claim speech was
  checked.
- A transient render-application failure retries the same immutable Job snapshot;
  accepted cleanup is never reanalyzed. A snapshot mismatch invalidates the stale
  analysis and schedules the current source for preflight instead of retrying it.
- If the user accepted cleanup, any snapshot or application failure under
  `required_v1` remains fail-closed: no uncleaned Ready output is permitted. The user
  may explicitly choose the same-snapshot retry or Create without cleanup.

## API and Dispatch Contract

Expose a typed, content-minimized `speech_cleanup` projection only on active thread
detail responses:

```text
applicable
unavailable_reason
analysis: id, status, detector_version, has_findings,
          candidate_count, category_counts, estimated_removed_ms,
          error: { code, retryable } | null
decision: clean | keep_original | create_without_cleanup | null
requires_choice
render_blocker: video_required | null
outcome: {
  job_id, render_generation_id,
  status: applied | checked_no_change | declined | bypassed_unchecked | failed,
  removal_count, removed_ms,
  error: { code, retryable } | null
} | null
```

The creation-thread list/rail summary does not join or load analysis rows; it omits
the field or returns `speech_cleanup: null` according to the tolerant summary schema.
Never expose raw source fingerprints, GCS paths, signed URLs, timed words,
transcripts, cut intervals, detector diagnostics, or private error details. The
detail projection is bounded to scalar/enumerated fields and category counts.
The outcome receipt is generation-bound and is projected only from the active Job
and its current render generation. Older Job/generation outcomes never win by recency
or fill a missing current receipt. Public `/me`, generative Job, and creation-thread
serializers must omit `_speech_cleanup_internal`, timed words, and cut intervals;
admin receives only an explicitly sanitized operator projection.

The revision-fenced `generate` action always accepts the exact current analysis ID.
Findings require `clean | keep_original`; `no_findings` requires choice omitted and
snapshots the checked zero-cut result. Under one row lock and idempotency key, the
backend verifies ownership, current fingerprint, capability, analysis status, and
thread revision, then atomically persists the decision and immutable Job row. Broker
publication happens after commit and is recovered by the durable reconciler.

- `clean` stamps `speech_cleanup_requested=true` and `required_v1`.
- `keep_original` after findings stamps `false`, `off_v1`, and `declined`.
- `no_findings` with omitted choice snapshots the exact zero-cut analysis and emits
  `checked_no_change`; it does not manufacture a consent choice.
- the distinct `create_without_cleanup` recovery action stamps `false`, `off_v1`,
  and `bypassed_unchecked`; it never claims analysis completed.
- stale/missing/running analysis returns a typed recoverable conflict and no Job.
- audio-only Narrated analysis remains visible, but the client sends no choice or
  generate action and decision remains `null` until video attaches. A typed
  `video_required` backend rejection remains defense in depth.
- a required snapshot that cannot be validated fails as cleanup-specific recovery,
  never as a successful uncleaned render.
- retry and create-without-cleanup are explicit revision-fenced actions with stable
  idempotency keys; neither is inferred from chat prose or a generic generate retry.

Editor rerenders retain the original Job's immutable contract for unchanged media.
The private immutable payload lives at
`Job.assembly_plan["_speech_cleanup_internal"]["preflight_snapshot"]` and contains
the analysis/source/policy identity, exact timed words/safety signals, and CutPlan.
No public serializer may pass through that namespace.

## Frontend Contract

- Type the projection and action in `creation-thread-api.ts`.
- Render a deterministic artifact at the existing confirmation seam in
  `ChatCreationWorkspace.tsx`; do not let assistant prose manufacture the card.
- Submit cleanup choice and `generate` in one request. For `no_findings`, submit the
  analysis ID with choice omitted. Avoid PATCH-then-render races.
- Clear optimistic choice on stale analysis and hydrate all states after refresh.
- Show the same boxed evidence for active embedded-video speech and uploaded or
  recorded Narrated audio. For an audio-only project, keep decision `null`, hide the
  choice/create actions, and show **Add at least one video clip to make your video**;
  do not imply analysis failure. Reveal choices after attachment without reanalysis
  when the foreground voiceover fingerprint is unchanged.
- Keep Play, Download, and Open editor unavailable until a Job is genuinely Ready.
- Use backend-supplied, pluralized counts/duration only. If authoritative detail is
  absent, use the simpler truthful copy.
- Map stable failure code plus `retryable` to approved copy/actions; never branch on
  provider exception strings. Unexpected server failures use generic truthful copy
  while remaining visible to error monitoring.

Generalize the existing thread-detail poll instead of adding a cleanup poll. Preserve
`creationThreadInProgress()` as the render/Creator semantic so analysis-only state is
never labeled **Rendering**. Add `creationThreadNeedsPolling(thread)` as
`creationThreadInProgress(thread) || speechCleanupPending(thread)` and use it only to
control the existing poll. The one poll keeps the current 2.5-second success cadence
and 5-second reconnect cadence. `creationThreadProgressKey()` adds the bounded
cleanup analysis ID/status alongside existing render generations, so a cleanup-to-
render transition continues through the same effect without overlapping timers.

Add `AbortSignal` plumbing to the thread-detail API and own one `AbortController` in
the effect: abort any prior fetch before the next one and abort on project switch or
unmount. Preserve the request-sequence acceptance check and active-project ID fence
as defense in depth. A stale response from a prior project or sequence cannot
overwrite current state, and there is literally at most one in-flight detail fetch,
timer, effect, and status endpoint.

`ChatCreationWorkspace` owns one visually hidden `aria-live="polite"` announcer for
meaningful cleanup/render transitions. `SpeechCleanupDecisionCard` renders semantic
structure but no live region, preventing nested or duplicate announcements.

## Rollout and Operations

Introduce an independent chat-preflight mode (`off | shadow | enforce`) plus stable
source-cohort rollout. Treat production activation through 100% as part of this
feature, not an optional follow-up. Required sequence:

1. Additive schema/API/task deployment, including the dedicated `speech-analysis`
   Fly worker and queue, with behavior off.
2. Internal shadow analysis; compare persisted preflight CutPlans with the legacy
   render analyzer without allowing the legacy result to replace the snapshot.
3. Ship frontend tolerant of old responses and disabled enforcement.
4. Enforce for internal/allowlisted traffic.
5. Ramp enforcement and mixed-gap apply 1% → 10% → 25% → 50% → 100%, gated at
   every stage by preflight/render agreement, cleanup failure rate, queue latency,
   invalidation correctness, and sampled cut precision.
6. At 100%, verify and persist mandatory production receipts for API configuration,
   dedicated worker consumption, one embedded-video analysis, one uploaded/recorded
   Narrated analysis, one audio-only analysis followed by video attachment and then
   choice, plus `applied`, `checked_no_change`, `declined`, `bypassed_unchecked`, and
   failed output receipts.
7. Remove temporary overrides that can leave production at `off/0`; the release is
   incomplete until the 100% receipts and rollback drill are recorded.

Kill-switching preflight restores the old chat path without rewriting existing Jobs
or decisions. API deploys before web because action schemas are strict.

Track dedicated queue/run latency, stuck tasks/leases, streamed input window and
artifact bytes, subprocess/provider deadline failures, temp cleanup failures,
findings rate, accept/decline/bypass rate, invalidation rate, stable failure codes, duplicate
suppression, plan reuse/mismatch, applied/no-change/failure outcomes, and removed
milliseconds. Track active-thread detail payload size/query count and list-summary
query count so the feature cannot introduce row-history loading or N+1 reads. Admin
Job Debug shows analysis ID, active narration source kind, detector version, explicit
choice, preflight reuse, and final removal outcome.

## Test Plan

Use a full pyramid. Lower layers exhaust deterministic branches; the browser layer
proves layout and accessibility; one real synthetic integration is the release-
blocking proof of the full contract.

### Backend schema and static contracts

- New `src/apps/api/tests/test_speech_cleanup_analysis_schema.py` verifies the
  `SpeechCleanupAnalysis` columns, enum/status constraints, uniqueness over current
  source/policy/engine identity, lease fields, private payload round-trip, bounded
  public projection, migration upgrade/downgrade shape, unique identity/current-
  lookup indexes, dispatch timestamps, partial unpublished-queue dispatch index, and
  partial queued/running lease-sweeper index. Query-plan assertions prove current
  lookup and bounded reconciler/sweeper batches use those indexes.
- New `src/apps/api/tests/test_speech_cleanup_worker_contract.py` verifies the task is
  routed only to `speech-analysis`, the Fly process consumes that queue, task soft/
  hard limits are explicit, the typed engine cannot import ORM/Celery/Job/
  `generative_build`, the limits remain below broker visibility timeout, and the
  render queue does not consume analysis tasks. It also verifies `acks_late`, Beat
  reconciler registration, bounded `FOR UPDATE SKIP LOCKED` scans, and that the lease
  duration exceeds hard task time plus clock-skew margin.
- Extend
  `src/apps/api/tests/routes/test_creation_threads_plan_item_parity.py` with the
  sole-writer guard. It rejects direct assignments to protected clip, voiceover,
  format, and audio-mode fields outside the media/source mutation service and checks
  both chat and editor call sites use the same row-lock/invalidation boundary.

### Backend service units

- New `src/apps/api/tests/services/test_active_narration_source.py` covers: active
  uploaded voiceover wins; active recorded voiceover wins; otherwise the exact
  embedded-audio speech spine is pinned; background/ducked/inactive tracks are
  ignored; missing/unsupported audio is unavailable; source generation, timing,
  format, audio-mode, and detector-policy changes alter the fingerprint; metadata
  and visual-only mutations under an unchanged active voiceover do not. Registration
  captures verified voiceover generation and clip assignment/manifest identity;
  task-time latest-object lookup is forbidden.
- New `src/apps/api/tests/services/test_speech_cleanup_preflight.py` covers: chat text
  never affects eligibility; one row per unique fingerprint; queued/running/ready/
  no-findings/failed transitions; duplicate enqueue suppression; crash-between-
  commit-and-enqueue recovery through the Beat reconciler; unpublished queued and
  expired-running `SKIP LOCKED` scans; lease expiry/reclaim; analysis-ID + attempt-
  token + current-source CAS fencing; killed-before-ack and killed-after-claim
  convergence; exact timed words and CutPlan persistence; every stable public failure
  code and retryability; private error redaction; and unexpected programming errors
  re-raised rather than converted to `no_findings`.

### Backend task, route, and dispatch tests

- New `src/apps/api/tests/tasks/test_speech_cleanup_analysis.py` drives the task
  adapter through ready, no-findings, typed operational failure, timeout, retry,
  worker-loss, and late-completion branches. It asserts bounded mono extraction,
  explicit deadlines, fenced persistence, and that an unexpected `TypeError`,
  `AttributeError`, `AssertionError`, `ImportError`, or `NameError` escapes the task.
  It also asserts the exact-generation signed URL is streamed into FFmpeg; no full
  video download or bytewise hash occurs; only the exact renderer narration window
  is decoded; output size is bounded from duration/sample format; subprocess process
  groups terminate on deadlines; URL/error logs are redacted; and temporary/partial
  artifacts are removed across success, typed failure, unexpected error,
  cancellation, and soft timeout.
  A replacement-after-schedule case proves the generation-aware signer/local check
  consumes the registered bytes or fails typed—it can never read replacement bytes
  from the same path. Claim and terminal updates must fail when analysis ID, attempt
  token, or current-source predicate differs.
- New `src/apps/api/tests/routes/test_creation_threads_speech_cleanup.py` covers all
  public projections and revision-fenced actions: automatic scheduling independent
  of prose; running/findings/no-findings/failed/stale hydration; findings clean/keep;
  no-findings with current analysis ID and omitted choice; distinct
  `create_without_cleanup`; Retry speech check; stale, cross-owner, missing, and
  duplicate analysis/action IDs; audio-only `video_required`; and no raw transcript,
  path, signed URL, fingerprint, timed word, interval, detector diagnostic, or
  provider error leakage. Detail loads at most one current bounded projection plus
  an outcome only from the active Job/current generation; stale generations never
  win. `/me`, generative, and creation public projections cannot expose the private
  snapshot; admin sees only sanitized operator fields. List summaries omit/null the
  projection without an analysis join or N+1 queries.
- Extend
  `src/apps/api/tests/routes/test_creation_threads_plan_item_parity.py` with attach,
  detach, replacement, reorder, format, audio-mode, and voiceover mutations. Assert
  invalidation commits atomically, queue publication occurs only after commit, an
  unchanged active source preserves analysis/decision, and a source change clears
  both.
- Extend `src/apps/api/tests/tasks/test_content_plan_build.py` with dispatch branches:
  clean stamps `required_v1` plus the exact immutable analysis snapshot under
  `_speech_cleanup_internal.preflight_snapshot`; keep stamps `off_v1`/`declined`;
  no-findings with omitted choice snapshots zero-cut `checked_no_change`; distinct
  Create without cleanup stamps `off_v1`/`bypassed_unchecked`. Decision and Job row
  commit atomically before broker publish; recovery republishes the same Job.
  Missing/stale/mismatched snapshots create no Job; audio-only creates no Job and
  leaves decision null until video exists; active narration identity is pinned;
  duplicate dispatch is idempotent; editor rerender inherits the immutable contract
  and #962 generation fencing.
- Refactor and extend
  `src/apps/api/tests/tasks/test_generative_build_silence_cut.py`: snapshot-apply
  success, `off_v1` bypass, missing/mismatched/invalid snapshot, FFmpeg application
  failure, receipt persistence, and caption synchronization. Required branches fail
  closed with no uncleaned Ready output. Transient application retry reuses the same
  immutable snapshot; mismatch invalidates/reanalyzes current source; neither path
  reanalyzes accepted cleanup. Replace tests that expect render-time analysis/cache
  behavior with assertions that render consumes only the snapshot. Public Job
  serializers are asserted free of `_speech_cleanup_internal`, words, and cuts.

### CRITICAL cross-layer proof

Add `src/apps/api/tests/integration/test_chat_speech_cleanup.py`. This test is
release-blocking and must use the deterministic synthetic #960 mixed-gap media plus
real FFmpeg:

1. Create the thread and PlanItem through the chat API, select an eligible format,
   and attach the source through the real media mutation boundary.
2. Execute the dedicated analysis task and assert the projected card reports the
   known #960 filler/mixed-gap findings and persists exact timed words/CutPlan.
3. Parameterize the confirmation branch over `clean` and `keep_original`. Assert
   `clean` creates a `required_v1` Job with the exact analysis ID/fingerprint/version/
   CutPlan snapshot, while `keep_original` creates an `off_v1` Job with an explicit
   declined receipt.
   Add the `no_findings` branch with current analysis ID, omitted choice, zero-cut
   snapshot, and `checked_no_change`, plus failed-analysis bypass with the distinct
   `bypassed_unchecked` receipt.
4. After preflight, replace Whisper, FFmpeg `silencedetect`, mixed-gap detection, and
   retake detection entry points with call-counting hard failures. Run render through
   real FFmpeg and assert all four analysis call counts remain exactly zero.
5. For `clean`, assert the expected #960 atomic interval is removed, captions use the
   same remap, and the applied receipt matches the approved snapshot. For
   `keep_original`, assert duration/speech timing remains uncut and no cleanup is
   claimed.
6. Add a standalone-audio Narrated case: upload/record narration without video, run
   analysis and expose evidence plus `video_required`; assert decision is null and no
   choice/generate request is accepted; attach a video through the API; assert the
   analysis ID/fingerprint remains current because the foreground voiceover did not
   change; only then parameterize clean and keep through Job dispatch and snapshot-
   only render.

The test fails if any render path invokes Whisper, `silencedetect`, mixed-gap, or
retake analysis, even if the final pixels happen to look correct.

### Frontend component and API tests

- Extend `src/apps/web/src/__tests__/lib/creation-thread-api.test.ts` for the optional
  typed projection, generation-bound `applied | checked_no_change | declined |
  bypassed_unchecked | failed` outcome, stable `{code, retryable}` failures, old-
  response compatibility, detail-versus-null/omitted-list shapes,
  `creationThreadNeedsPolling`, AbortSignal forwarding, and exact atomic generate/
  retry/bypass payloads with stable action IDs. `creationThreadInProgress` must remain
  false for analysis-only states.
- Add
  `src/apps/web/src/__tests__/plan/SpeechCleanupDecisionCard.test.tsx` for checking,
  findings, no-findings, failed, stale, saving, cleanup-failed, ready-applied, ready-
  no-change, ready-declined, and audio-only/video-required states. Assert no default
  choice, exact clean/keep/retry/bypass callbacks, disabled/double-click behavior,
  semantic heading or fieldset/legend, absence of a nested live region, keyboard order, visible
  focus classes, 44px targets, and truthful degraded copy when counts/duration are
  absent.
- Extend `src/apps/web/src/__tests__/plan/ChatCreationWorkspace.test.tsx` for
  deterministic card placement at the confirmation seam, no prose-derived card,
  automatic polling, atomic choice+generate, stale-choice clearing, refresh
  hydration, Talking-to-camera and Narrated source parity, standalone audio followed
  by video attachment without reanalysis or a premature choice/generate call,
  no-findings generate with omitted choice, distinct bypass/declined receipts,
  recovery split by preflight retryability/application transient/snapshot mismatch,
  one workspace-owned live announcer, and truthful Ready
  receipts with no output actions before Ready. Fake-timer tests prove one generalized
  detail poll covers cleanup and render without broadening render-progress semantics,
  uses 2.5-second success/5-second reconnect
  backoff, changes phase without overlapping timers, includes cleanup in the stable
  progress key, rejects stale request sequences/projects, and cancels on project
  switch or unmount. Pass a real AbortSignal, abort before replacement fetch and on
  switch/unmount, and assert at most one literal in-flight request. Assert no cleanup-
  specific poll effect or endpoint is called.

### Playwright visual and accessibility coverage

- Extend
  `src/apps/web/src/app/dev-qa/chat-first-creation/ChatFirstCreationFixture.tsx` and
  its page with deterministic queryable states for checking, findings, no-findings,
  failed retryable/non-retryable, stale, saving, rendering, cleanup-failed, ready-
  applied, ready-no-change, ready-declined, ready-bypassed-unchecked, and audio-only/
  video-required. The fixture must import and render the production
  `SpeechCleanupDecisionCard`; it must not maintain a duplicate fixture-only card.
- Extend `src/apps/web/e2e/chat-first-creation.spec.ts` to exercise both clean and keep
  flows at 1440×900 and 1280×720, plus 390×844 and 375px mobile layouts and 200% zoom.
  Assert the box matches the format-card hierarchy, options stack without horizontal
  overflow, the composer remains usable, focus/tab order follows reading order,
  Enter/Space activates exactly one choice, focus is visible, status announcements
  are polite and non-repeating, state is not color-only, targets are at least 44px,
  and reduced-motion removes nonessential animation. Assert the production card (or
  a real `/plan` flow), exactly one visually hidden live-region owner, and no nested
  announcer. The checking→findings→choice→
  rendering→Ready fixture flow also asserts one in-flight detail request/timer at a
  time and the existing reconnect copy/backoff behavior.

### Commands and gates

- Focused backend pyramid: `cd src/apps/api && python3 -m pytest` with every backend
  file named above plus
  `tests/pipeline/test_silence_cut_mixed_gap_golden.py` and existing speech-cleanup
  service suites.
- Full backend: `python3 -m pytest`, `ruff check .`, and `ruff format --check .`.
- Frontend: focused Jest files above, then `npm test`, `npm run lint`, and
  `npx tsc --noEmit`.
- Browser: `npm run e2e -- e2e/chat-first-creation.spec.ts` at the stated viewports,
  followed by targeted real-app browser QA.
- Repository: `bash scripts/preship-check.sh`.
- No prompt or agent contract changes are in scope, so no live LLM call or agent eval
  is required. If implementation changes any prompt or `render_prompt()`, that
  exception triggers the repository's prompt-version bump and live eval rule.

## What Already Exists

- `ChatArtifactCard` and the current confirmation/progress/failure/Ready artifacts
- durable creation-thread revisions and idempotent action dispatch
- PlanItem cleanup capability, consent reconciliation, and explicit render contracts
- V2 lexical/acoustic mixed-gap detector, atomic clamp, cache, and golden fixtures
- shared PlanItem clip setter and cleanup input fingerprint helpers
- immutable Job cleanup contract inherited by editor rerenders
- Job pipeline trace, admin debug surface, and cleanup outcome fields
- Nova's `DESIGN.md`, light `/plan` language, and `docs/UX_COPY.md`

## NOT in Scope

- Inferring consent from user or assistant messages.
- Candidate-by-candidate transcript editing or exposing raw transcript snippets.
- Cleanup for Montage, music-led edits, or formats without a resolved foreground
  narration source.
- Cleanup of background, supporting, ducked, or otherwise inactive audio tracks;
  only the resolved foreground narration is in scope.
- Generating a complete video from narration audio without at least one video clip,
  including a new kinetic-caption, stock-footage, or branded-canvas generator.
- A new ASR/VAD/LLM pass beyond the signals already used by the cleanup engine.
- Downloading or hashing an entire source video for preflight when exact-generation
  storage identity and a bounded streamed narration window are available.
- A second frontend polling loop, cleanup-only status endpoint, or analysis payload
  in project-list summaries.
- Broad exception swallowing or a best-effort fallback that turns an accepted
  cleanup failure into an uncleaned Ready result.
- Rewriting the mixed-gap detector before canary evidence shows a new detector defect.
- Changing EditorShell or re-prompting unchanged-source editor saves.
- Backfilling every historical PlanItem or rewriting completed Jobs.
- Shipping enforcement before production flags, observability, and rollback are tested.

## Locked Design and Product Decisions

- Use the approved single outer box in the same visual format as the chat format
  selector, with two equal neutral choice boxes and no preselected option.
- The same feature launches for Talking to camera and Narrated. Narrated covers the
  active embedded-video speech and uploaded or recorded narration; standalone audio
  is analyzed and shown but keeps decision null and offers no create choice until
  video is attached.
- No-findings is a quiet receipt in the normal confirmation card. Ready keeps a
  truthful generation-bound receipt for `applied`, `checked_no_change`, `declined`,
  `bypassed_unchecked`, or `failed`; unchecked bypass never claims speech was checked.
- Category counts and estimated duration appear only when authoritative; copy
  degrades to the simpler truthful finding statement when either is absent.
- The dedicated `SpeechCleanupAnalysis` table, authoritative preflight snapshot,
  foreground-only generation-pinned source resolver, CAS/lease + Beat reconciliation,
  private Job snapshot, dedicated worker, and measured rollout through mandatory
  100% production receipts are accepted architecture constraints.

## Locked Code Quality Decisions

- One row-locked PlanItem media/source mutation service is the sole writer for chat
  and editor clip, voiceover, format, and audio-mode changes. It owns transactional
  invalidation plus a shared post-commit preflight dispatcher; a regression guard
  prevents call sites from writing or publishing around the boundary.
- The speech-cleanup analysis engine is typed and independent of Job, ORM, Celery,
  and `generative_build` task internals. Adapters own persistence and task behavior.
- Expected operational failures use a stable typed taxonomy and explicit server-owned
  retryability. Unexpected programming errors escape to monitoring. Preflight retry,
  source replacement, same-snapshot application retry, mismatch reanalysis, and
  unchecked bypass are distinct paths; accepted cleanup always fails closed and is
  never reanalyzed.
- Testing uses the named full pyramid above. The synthetic #960 chat-to-real-FFmpeg
  integration is release-blocking and proves zero render-time analysis calls for
  clean and keep, including standalone audio with null decision followed by video
  attachment, and distinguishes checked-no-change/declined/unchecked receipts. No
  prompt or LLM eval is required unless implementation changes a prompt boundary.

## Locked Performance Decisions

- Preflight streams an exact-generation signed source directly into FFmpeg and emits
  only a bounded 16 kHz mono artifact for the renderer's exact narration window. It
  never downloads or byte-hashes a full video; centrally tested duration, derived
  output-size, operation, task, and broker ceilings bound work and all temporary
  files/processes are cleaned up.
- Only active thread detail loads one bounded public cleanup projection. Project-list
  summaries omit/null it and never load private analysis payloads or introduce an
  analysis join/N+1 query. Unique identity/current lookup, unpublished-dispatch, and
  lease-sweeper indexes keep database work bounded.
- Cleanup and rendering share the existing generalized thread-detail poll via
  `creationThreadNeedsPolling`; `creationThreadInProgress` retains render-only
  semantics. The current 2.5-second success and 5-second reconnect cadence,
  AbortController, request sequencing, progress key, project-switch, and unmount
  fences permit one literal in-flight fetch; no second poll or status endpoint is
  added.

## Approved Mockups

| State | Repo-local artifact | Binding implementation note |
|---|---|---|
| Pre-render decision | `.design/chat-speech-cleanup-20260905/direction-A-box-decision.png` | One outer box in the same visual grammar as the existing format selector; two equal neutral options; no preselection |
| Ready receipt | `.design/chat-speech-cleanup-20260905/direction-A-aligned-ready.png` | Preserve the current Ready hierarchy and add only the generation-bound speech receipt |
| Inspectable source | `.design/chat-speech-cleanup-20260905/aligned-a.html` | Desktop alignment reference; responsive behavior remains governed by this plan's accessibility contract |

## Failure Modes

Every new boundary has an explicit failure state, an owning test, and a truthful
user outcome.

| Codepath and realistic failure | Named test | Handling | User-visible result |
|---|---|---|---|
| Two chat/editor mutations race and the loser resolves an obsolete narration source | `test_concurrent_media_mutations_lock_and_invalidate_once` in `tests/routes/test_creation_threads_plan_item_parity.py` | Lock PlanItem, serialize mutation, recompute after lock, supersede old analysis atomically | Latest media wins; prior choice clears and **Checking for filler sounds…** resumes only if the active source changed |
| A new route assigns a protected media field directly and skips invalidation | `test_protected_plan_item_media_fields_have_one_writer` in `tests/routes/test_creation_threads_plan_item_parity.py` | Static sole-writer allowlist fails CI | No shipped user impact; merge is blocked |
| Database commit succeeds but broker publication fails | `test_committed_media_mutation_recovers_missed_preflight_enqueue` in `tests/services/test_speech_cleanup_preflight.py` | Persist unpublished `queued` + `next_dispatch_at`; Beat claims a bounded `FOR UPDATE SKIP LOCKED` batch and republishes exactly once | Checking may be delayed; refresh/retry recovers without duplicate analysis or lost media |
| A late worker completes after the source was replaced | `test_superseded_worker_completion_cannot_become_current` in `tests/tasks/test_speech_cleanup_analysis.py` | Attempt token and current-fingerprint fence keep the completion historical | Current card never regresses or shows findings for the removed source |
| Worker dies before acknowledgement or after claim | `test_worker_loss_converges_before_ack_and_after_claim` in `tests/tasks/test_speech_cleanup_analysis.py` | `acks_late`; lease exceeds hard limit + skew; Beat reclaims expired running rows; claim/finalize CAS analysis ID + attempt token + current source | Checking continues; repeated failure becomes retryable analysis failure |
| Signed source expires, disappears, or path bytes are replaced after scheduling | `test_registered_generation_never_reads_replacement_bytes` in `tests/tasks/test_speech_cleanup_analysis.py` | Registration captures verified generation/manifest identity; signer/local check reads only that generation or fails typed; never resolve latest by path | **Kria couldn’t check the speech.** Retry is offered; replacement bytes can never inherit old consent |
| FFprobe/FFmpeg exceeds a deadline, emits too much output, or leaves a partial artifact | `test_extraction_limits_kill_process_and_cleanup_every_exit` in `tests/tasks/test_speech_cleanup_analysis.py` | Kill process group, bound diagnostics, unlink all temporary outputs in `finally`, persist typed retryability | Truthful failed-check card with **Retry speech check** / **Create without cleanup** |
| Source has no usable speech track or unsupported media | `test_missing_or_unsupported_narration_is_nonretryable` in `tests/services/test_active_narration_source.py` | Return stable nonretryable capability/failure code; never invoke ASR | Ask to replace/record narration; explicit bypass remains available when video is renderable |
| Transcription provider is unavailable or times out | `test_transcription_failure_is_typed_redacted_and_retryable` in `tests/services/test_speech_cleanup_preflight.py` | Persist stable code and retryability; redact provider text; retain current fingerprint | Generic truthful failure plus Retry/bypass; no provider details leak |
| Silence or mixed-gap analysis returns an expected operational failure | `test_analysis_operational_failure_never_becomes_no_findings` in `tests/services/test_speech_cleanup_preflight.py` | Convert only typed operational error to failed state; no empty-success fallback | Failure card, never **No cleanup suggested** |
| A programming error is raised inside extraction/transcription/detection | `test_unexpected_programming_errors_escape_worker` in `tests/tasks/test_speech_cleanup_analysis.py` | Report and re-raise; fenced failure hook may expose opaque `internal_error` without consuming exception | Generic retryable failure; engineering receives a real failed task/trace |
| Active thread detail resolves a stale Job generation or tries to expose private payload | `test_detail_outcome_uses_only_active_job_generation_and_is_private` in `tests/routes/test_creation_threads_speech_cleanup.py` | At most one indexed analysis; outcome joins only active Job/current generation; strict serializers omit `_speech_cleanup_internal` everywhere public | Current card/receipt waits or reconnects; stale receipts, words, paths, intervals, and diagnostics never appear |
| Project-list request accidentally joins analysis history or creates N+1 queries | `test_thread_list_omits_cleanup_without_analysis_queries` in `tests/routes/test_creation_threads_speech_cleanup.py` | Separate lightweight summary schema; query-count assertion | Project rail remains fast and shows no cleanup detail |
| Generate receives stale/cross-owner analysis, stale revision, or changed payload under a reused action ID | `test_generate_rejects_stale_cross_owner_or_changed_cleanup_action` in `tests/routes/test_creation_threads_speech_cleanup.py` | Row-locked ownership/fingerprint/revision verification; idempotent exact replay only; no Job on conflict | Refresh current card; no render starts from stale consent |
| User double-clicks clean/keep or loses the response after dispatch | `test_cleanup_choice_double_submit_replays_one_job` in `tests/routes/test_creation_threads_speech_cleanup.py` | Stable action ID and execution receipt return the same authoritative result | One render begins; buttons remain disabled while saving |
| No-findings generate is mistaken for a consent choice, or failed-check bypass is mistaken for decline | `test_no_findings_and_bypass_have_distinct_choice_and_receipts` in `tests/routes/test_creation_threads_speech_cleanup.py` | No-findings requires current analysis ID + omitted choice and emits `checked_no_change`; recovery stores `create_without_cleanup`/`bypassed_unchecked`; keep-after-findings alone emits `declined` | Ready copy honestly says checked/no change, kept as recorded, or created without checking |
| Standalone narration is analyzed before any video exists | `test_audio_only_analysis_survives_later_video_attachment` in `tests/integration/test_chat_speech_cleanup.py` | Persist analysis with decision null; client sends no choice/generate; backend `video_required` defends; visual-only attach preserves fingerprint | Findings evidence remains with **Add at least one video clip to make your video**; choices appear only after attachment |
| Required Job snapshot is missing, malformed, or mismatched | `test_required_snapshot_mismatch_fails_before_render_analysis` in `tests/tasks/test_generative_build_silence_cut.py` | Fail closed; never invoke Whisper/detectors; invalidate and schedule current-source analysis | No misleading Ready output; current speech is checked again before a new decision |
| FFmpeg cannot apply the accepted CutPlan or caption remap fails transiently | `test_required_snapshot_apply_failure_retries_same_snapshot` in `tests/tasks/test_generative_build_silence_cut.py` | Required variant fails atomically; retry same `_speech_cleanup_internal.preflight_snapshot`; never reanalyze accepted cleanup | Cleanup failure artifact offers same-cleanup retry or explicit unchecked bypass; original is not silently shipped |
| Keep-original render accidentally analyzes or cuts speech | `test_keep_original_snapshot_path_makes_zero_analysis_calls` in `tests/integration/test_chat_speech_cleanup.py` | Stamp `off_v1`; hard-fail spies assert zero Whisper/silencedetect/mixed-gap/retake calls | Output keeps recorded speech and shows only the truthful declined receipt |
| Thread-detail polling hits a network error or receives an out-of-order/prior-project response | `test_one_poll_aborts_reconnects_and_rejects_stale_sequences` in `ChatCreationWorkspace.test.tsx` | `creationThreadNeedsPolling` drives same poll; abort previous request; 2.5s→5s backoff; sequence/project fences remain | **Reconnecting…** appears without labeling analysis as Rendering or losing current card/composer text |
| Component unmount/project switch leaves a timer or fetch able to mutate the next project | `test_cleanup_render_poll_aborts_on_switch_and_unmount` in `ChatCreationWorkspace.test.tsx` | AbortController, timer cleanup, project fence; literally one in-flight request and generalized poll | New project never flashes the old project's cleanup/render state |
| Card and workspace both announce one state, or dev-QA tests a duplicate implementation | `test_workspace_owns_one_live_announcer_and_fixture_uses_production_card` in `ChatCreationWorkspace.test.tsx` | Production card has no live region; workspace owns one hidden polite announcer; fixture imports production component | Screen reader hears each meaningful transition once; QA matches shipped UI |
| Old API omits cleanup, counts are absent, or an unknown failure code arrives | `test_decision_card_degrades_truthfully_for_tolerant_contracts` in `SpeechCleanupDecisionCard.test.tsx` | Optional typed projection and generic copy; never infer findings, receipt, or retryability | Normal legacy confirmation or simplified truthful card remains usable |
| Production configuration is on but the dedicated worker is absent, canary thresholds regress, or a rollout receipt is missing | `test_rollout_halts_without_worker_canary_and_receipts` in `tests/test_speech_cleanup_worker_contract.py` | Health/queue canary blocks next cohort; kill switch restores old path; 100% is incomplete until receipts exist | No broader cohort is exposed; in-flight immutable Jobs finish under their stamped contract |

**Critical silent gaps: 0.** Every critical failure either prevents dispatch, fails a
required render closed, presents explicit recovery, or blocks rollout with an
observable receipt. No path may silently turn accepted cleanup into an uncleaned
Ready output or display findings from a stale source.

## Inline ASCII Diagram Comments

The implementation PR must place these small diagrams immediately above the named
facades. They are architecture comments, not decorative documentation: any PR that
changes an arrow, owner, transaction boundary, or state must update the adjacent
diagram and its contract test.

`src/apps/api/app/services/plan_item_media.py` above
`mutate_plan_item_media()`:

```text
chat/editor command
       |
       v
lock PlanItem -> resolve old source -> mutate -> resolve new source
       |                                  |
       +---------- same transaction ------+
                         |
              preserve OR supersede consent
              capture verified object generation
                         |
                       COMMIT
                         |
          post-commit publish OR Beat reconcile
```

`src/apps/api/app/services/speech_cleanup_preflight.py` and
`src/apps/api/app/tasks/speech_cleanup_analysis.py` above claim/run/finalize:

```text
unpublished queued --Beat/SKIP LOCKED--> publish
        |
        +--CAS claim(id, attempt, source, lease)--> running
                                                     |
                     CAS terminal(same predicates) --+--> ready | no_findings | failed
                                                     |
                       expired lease / acks_late -----+--> reclaim

captured generation -> exact-generation sign/check -> FFmpeg mono window -> typed engine
```

`src/apps/api/app/routes/creation_threads.py` above cleanup action handling,
`src/apps/api/app/tasks/content_plan_build.py` above dispatch, and
`src/apps/api/app/pipeline/speech_cleanup_apply.py` above render application:

```text
detail card -> generate(analysis_id, choice?, revision)
                    |
       lock + validate + atomic decision/Job row
                    |
        no_findings + null -> checked_no_change
        keep -> off_v1 / declined
        bypass -> off_v1 / bypassed_unchecked
        clean -> required_v1 Job
                    + _speech_cleanup_internal.preflight_snapshot
                    |
                  COMMIT
                    |
         post-commit broker publish OR reconcile
                    |
             validate -> FFmpeg apply
                    |
        generation-bound receipt OR fail closed
```

`src/apps/web/src/lib/creation-thread-api.ts` above progress helpers and
`src/apps/web/src/app/plan/_components/workspace/ChatCreationWorkspace.tsx` above
the polling effect:

```text
speechCleanupPending ------+
creationThreadInProgress --+--> creationThreadNeedsPolling
                                  |
                           ONE thread-detail poll
                                  | 2.5s success / 5s reconnect
                                  | AbortController: one fetch
                                  v
                       sequence + project + unmount fences
                                  |
                     DecisionCard | Progress | Ready
```

`src/apps/api/app/worker.py` and `fly.toml` should carry short pointers to the queue
diagram rather than duplicate it. The model itself must keep this compact state
diagram beside `SpeechCleanupAnalysis` because its CAS and supersession rules are
otherwise easy to misread:

```text
queued --claim(token + lease)--> running --> ready | no_findings | failed
  ^                                  |
  +------ publish/redelivery --------+ expired lease -> new token

any current row --source/policy change--> superseded (history only)
terminal write requires: id + attempt token + current fingerprint
```

`SpeechCleanupDecisionCard.tsx` should point to the interaction-state matrix in this
plan.

## Worktree Parallelization

Use a fresh worktree per lane off the same `origin/main` base. Land or rebase in the
wave order below; do not let two lanes edit a high-conflict file concurrently.

| Lane | Tasks / module ownership | Depends on | Can run with | Conflict flag |
|---|---|---|---|---|
| A — Data contract | T1: `models.py`, migration `0095`, schema/index tests | None | B | Low; one model/migration owner |
| B — Pure analysis | T2: active-source resolver, typed analysis engine, pure service tests | None | A | Low until extraction from `generative_build.py`; Lane D owns that hot file |
| C — Mutation + preflight | T3–T4: PlanItem media facade, persistence service, analysis task, queue routing | A + B interfaces | E fixture scaffolding | High in `creation_threads.py`, `plan_items.py`, `worker.py`; single lane owner |
| D — Dispatch + render | T5–T6: Job snapshot, content dispatch, render application | A + B; C contract frozen | E | High in `content_plan_build.py` and especially `generative_build.py`; single lane owner |
| E — Web product | T7: API types, DecisionCard, workspace poll, dev-QA fixture, Playwright | T5 response/action schema; may start against frozen mock types | C + D | High in `ChatCreationWorkspace.tsx`; one frontend owner |
| F — Integration | T8: critical #960 test and cross-layer gap closure | A–E landed/rebased | G prep | Medium in existing test modules; test-only owner coordinates final assertions |
| G — Operations | T9: rollout flags, observability, canary, receipts | C + D + F green | None during final wave | Medium in `config.py`, `worker.py`, `fly.toml`; rebase after Lane C |

Execution order:

```text
Wave 0:  A(T1) || B(T2)
               |
Wave 1:  C(T3 -> T4)
               |
Wave 2:  D(T5 -> T6) || E(T7 after API contract freezes)
               |
Wave 3:  F(T8 critical integration + full pyramid)
               |
Wave 4:  G(T9 rollout/canary/100% receipts)
```

Integration ownership rules:

- Lane D performs the only production edit to `generative_build.py`; Lane B supplies
  the pure engine API without touching that file.
- Lane C owns production changes in `creation_threads.py`; Lane E consumes the API
  and does not modify backend routes.
- Lane E is the only owner of `ChatCreationWorkspace.tsx`.
- Lane C lands queue wiring before Lane G adjusts rollout/operational configuration.
- Lane F may amend tests across modules after owners land, but does not redesign
  production interfaces. Resolve interface failures with the owning lane.

## Implementation Tasks

Synthesized from this review's findings. Checkbox each task as it ships.

- [x] **T1 (P1, human: 1–1.5 days / CC: 2–3 hours)** — data — Add durable analysis and source identity
  - **Outcome:** Add `SpeechCleanupAnalysis`, attempt/lease/supersession state, private
  payload/public receipt separation, dispatch timestamps, unique identity/current
    lookup/unpublished-dispatch/lease-sweeper indexes, verified voiceover generation,
    and migration.
  - **Surfaced by:** D3 durable table; D12 immutable generation; D13 bounded
    projection/query performance.
  - **Likely files:** `src/apps/api/app/models.py`,
  `src/apps/api/app/migrations/versions/0095_speech_cleanup_analysis.py`,
  `src/apps/api/tests/test_speech_cleanup_analysis_schema.py`.
  - **Verify:** `cd src/apps/api && python3 -m pytest tests/test_speech_cleanup_analysis_schema.py -v`

- [x] **T2 (P1, human: 2–3 days / CC: 4–6 hours)** — analysis — Extract the active-source resolver and typed engine
  - **Outcome:** Resolve voiceover-or-embedded spine deterministically; extract a
  Job/ORM/Celery-independent engine and typed models; preserve exact #960 behavior.
  - **Surfaced by:** D4 authoritative CutPlan, D5 foreground narration, D9 engine
  boundary, D12 bounded exact-generation analysis.
  - **Likely files:** `src/apps/api/app/services/active_narration_source.py`,
  `src/apps/api/app/pipeline/speech_cleanup_analysis.py`,
  `src/apps/api/app/pipeline/silence_cut.py`,
  `src/apps/api/tests/services/test_active_narration_source.py`,
  `src/apps/api/tests/services/test_speech_cleanup_preflight.py`.
  - **Verify:** `cd src/apps/api && python3 -m pytest tests/services/test_active_narration_source.py tests/services/test_speech_cleanup_preflight.py tests/pipeline/test_silence_cut.py tests/pipeline/test_silence_cut_mixed_gap_golden.py -v`

- [x] **T3 (P1, human: 1.5–2 days / CC: 3–5 hours)** — media mutation — Enforce one row-locked writer and invalidation owner
  - **Outcome:** Route chat/editor clip, voiceover, format, and audio-mode changes
  through one row-locked facade; invalidate only on active-source/policy change and
  return service-owned post-commit scheduling intent. Capture verified voiceover
  generation and clip assignment/manifest identity at registration.
  - **Surfaced by:** D8 sole writer and the confirmed direct-mutation root cause.
  - **Likely files:** rename/expand
  `src/apps/api/app/services/plan_clips.py` to
  `src/apps/api/app/services/plan_item_media.py` as one service, plus
  `src/apps/api/app/routes/creation_threads.py`,
  `src/apps/api/app/routes/plan_items.py`,
  `src/apps/api/app/routes/creator_agent.py`,
  `src/apps/api/tests/routes/test_creation_threads_plan_item_parity.py`.
  - **Verify:** `cd src/apps/api && python3 -m pytest tests/routes/test_creation_threads_plan_item_parity.py tests/routes/test_creation_threads.py tests/routes/test_creator_agent.py -v`

- [x] **T4 (P1, human: 2–3 days / CC: 5–8 hours)** — worker — Build recoverable preflight execution on its own queue
  - **Outcome:** Idempotent create/claim/finalize/reclaim lifecycle; exact-generation
  signed streaming/local generation checks; bounded mono artifact; typed failures;
  CAS terminal writes; `acks_late`; Beat `SKIP LOCKED` dispatch/lease reconciliation;
  cleanup; dedicated Fly worker/queue with tested deadlines and isolation.
  - **Surfaced by:** D3 lifecycle, D6 intentional dedicated worker, D10 error contract,
  D12 data movement and ceilings.
  - **Likely files:** `src/apps/api/app/services/speech_cleanup_preflight.py`,
  `src/apps/api/app/tasks/speech_cleanup_analysis.py`,
  `src/apps/api/app/storage.py`, `src/apps/api/app/tasks/maintenance.py`,
  `src/apps/api/app/worker.py`,
  `src/apps/api/app/config.py`, `fly.toml`, `.env.example`,
  `src/apps/api/tests/tasks/test_speech_cleanup_analysis.py`,
  `src/apps/api/tests/test_speech_cleanup_worker_contract.py`.
  - **Verify:** `cd src/apps/api && python3 -m pytest tests/tasks/test_speech_cleanup_analysis.py tests/test_speech_cleanup_worker_contract.py -v`

- [x] **T5 (P1, human: 1.5–2 days / CC: 4–6 hours)** — API/dispatch — Commit explicit choice and a private immutable Job snapshot
  - **Outcome:** Bounded detail-only projection; null/omitted list summary; atomic
  findings clean/keep, no-findings omitted-choice, and distinct unchecked-bypass
  actions/receipts; generation-bound active outcome; audio-only null decision and
  blocker; atomic decision + Job row; private
  `_speech_cleanup_internal.preflight_snapshot`; post-commit publish recovery; editor-
  rerender inheritance; no private payload in any public Job projection.
  - **Surfaced by:** D2 audio-only contract, D3 ownership, D4 snapshot, D10 recovery,
  D13 projection performance.
  - **Likely files:** `src/apps/api/app/routes/creation_threads.py`,
  `src/apps/api/app/routes/generative_jobs.py`, `src/apps/api/app/routes/me.py`,
  `src/apps/api/app/services/speech_cleanup.py`,
  `src/apps/api/app/services/public_assembly_plan.py`,
  `src/apps/api/app/tasks/content_plan_build.py`,
  `src/apps/api/tests/routes/test_creation_threads_speech_cleanup.py`,
  `src/apps/api/tests/tasks/test_content_plan_build.py`.
  - **Verify:** `cd src/apps/api && python3 -m pytest tests/routes/test_creation_threads_speech_cleanup.py tests/tasks/test_content_plan_build.py -v`

- [x] **T6 (P1, human: 2–3 days / CC: 6–10 hours)** — renderer — Apply the snapshot without second detection
  - **Outcome:** Apply the reviewed CutPlan and caption remap without Whisper,
  silencedetect, mixed-gap, or retake calls; fail accepted cleanup closed; preserve
  truthful generation-bound terminal receipts. Retry transient application failure
  with the same snapshot; invalidate/reanalyze mismatch; never reanalyze accepted
  cleanup.
  - **Surfaced by:** D4 consent parity, D9 task decoupling, D10 fail-closed recovery,
  production incident #960.
  - **Likely files:** `src/apps/api/app/pipeline/speech_cleanup_apply.py`,
  `src/apps/api/app/tasks/generative_build.py`,
  `src/apps/api/app/pipeline/talking_head_assembler.py`,
  `src/apps/api/app/services/speech_cleanup_outcome.py`,
  `src/apps/api/tests/tasks/test_generative_build_silence_cut.py`.
  - **Verify:** `cd src/apps/api && python3 -m pytest tests/tasks/test_generative_build_silence_cut.py tests/services/test_speech_cleanup_outcome.py -v`

- [x] **T7 (P1, human: 1.5–2 days / CC: 4–6 hours)** — web — Build the approved box and one truthful poller
  - **Outcome:** Build the exact format-card-aligned DecisionCard for Talking to camera
  and Narrated; all states and distinct checked/declined/unchecked receipts; audio-
  only evidence with no premature action; one workspace-owned live announcer;
  production-card dev fixture; `creationThreadNeedsPolling` without changing render
  semantics; one AbortController-fenced detail fetch; full responsive/accessibility
  coverage.
  - **Surfaced by:** approved Design A, D2 Narrated/audio-only scope, D10 recovery, D14
  single-poll performance.
  - **Likely files:** `src/apps/web/src/lib/creation-thread-api.ts`,
  `src/apps/web/src/app/plan/_components/workspace/SpeechCleanupDecisionCard.tsx`,
  `src/apps/web/src/app/plan/_components/workspace/ChatCreationWorkspace.tsx`,
  `src/apps/web/src/__tests__/lib/creation-thread-api.test.ts`,
  `src/apps/web/src/__tests__/plan/SpeechCleanupDecisionCard.test.tsx`,
  `src/apps/web/src/__tests__/plan/ChatCreationWorkspace.test.tsx`,
  `src/apps/web/src/app/dev-qa/chat-first-creation/ChatFirstCreationFixture.tsx`,
  `src/apps/web/e2e/chat-first-creation.spec.ts`.
  - **Verify:** `cd src/apps/web && npm test -- --runInBand src/__tests__/lib/creation-thread-api.test.ts src/__tests__/plan/SpeechCleanupDecisionCard.test.tsx src/__tests__/plan/ChatCreationWorkspace.test.tsx && npx tsc --noEmit && npm run e2e -- e2e/chat-first-creation.spec.ts`

- [x] **T8 (P1, human: 1.5–2 days / CC: 4–7 hours)** — verification — Prove the #960 path from chat to real FFmpeg
  - **Outcome:** Prove chat/API -> dedicated worker -> exact Job snapshot -> real
  FFmpeg for #960, clean and keep, plus standalone audio then video attachment; hard
  assert zero render-time analysis calls; cover checked-no-change, declined,
  bypassed-unchecked, active-generation receipt precedence, registered-byte
  immutability, worker-loss convergence, and close all 30 uncovered branches.
  - **Surfaced by:** D11 test audit: 8 of 38 branches currently covered, 30 gaps.
  - **Likely files:** `src/apps/api/tests/integration/test_chat_speech_cleanup.py` and
  every named backend/frontend test file in the Test Plan.
  - **Verify:** `cd src/apps/api && python3 -m pytest tests/integration/test_chat_speech_cleanup.py -v && python3 -m pytest && ruff check . && ruff format --check .`; then `cd src/apps/web && npm test && npm run lint && npx tsc --noEmit`.

- [ ] **T9 (P2, human: 2 operational days / CC: 3–5 hours)** — operations — Ramp only with measured production receipts
  - **Outcome:** Add off/shadow/enforce controls, stable cohorts, queue/source/outcome
  metrics, admin trace, canary thresholds, rollback drill, and signed receipts for
  every outcome including unchecked bypass at 1% -> 10% -> 25% -> 50% -> 100%.
  General availability is not complete until this
  P2 operational task and the 100% receipts are complete.
  - **Surfaced by:** D7 measured rollout and D12–D14 performance review.
  - **Likely files:** `src/apps/api/app/config.py`, `src/apps/api/app/worker.py`,
  `fly.toml`, `.env.example`, `src/apps/api/app/services/pipeline_trace.py`,
  `src/apps/api/app/routes/admin_jobs.py`,
  `src/apps/api/scripts/audit_speech_cleanup_shadow.py`,
  `src/apps/api/tests/scripts/test_audit_speech_cleanup_shadow.py`,
  `src/apps/api/tests/test_speech_cleanup_worker_contract.py`.
  - **Verify:** `cd src/apps/api && python3 -m pytest tests/scripts/test_audit_speech_cleanup_shadow.py tests/test_speech_cleanup_worker_contract.py -v`; run `bash scripts/preship-check.sh`, then execute and archive each rollout/canary receipt before advancing cohorts.
  - **Implementation note:** off/shadow/enforce controls, the isolated worker,
    bounded audit, signed receipt tooling, and rollback runbook are complete. T9
    remains open until real production cohorts, archived receipts, and the 50% →
    100% rollback drill are completed.

## Review Completion Summary

| Review dimension | Final result |
|---|---|
| Scope | Full scope accepted: Talking to camera and Narrated; embedded speech, uploaded/recorded voiceover, and standalone-audio analysis with video required for render |
| Architecture | 6 issues reviewed and resolved through D2–D7 |
| Code quality | 3 issues reviewed and resolved through D8–D10 |
| Tests | All required branches implemented across backend, frontend, and browser coverage; full suites pass |
| Performance | 3 issues reviewed and resolved through D12–D14 |
| Final verification | 12 unique frontend/backend sub-gaps found and folded; 54 total issues/gaps resolved |
| TODO audit | 1 P3/XL future item added to `TODOS.md`: generate video from standalone narration without user-supplied visuals |
| Failure audit | 0 critical silent gaps after the failure-mode matrix above |
| Outside voice | Skipped under Codex review policy |
| Parallelization | 7 module-derived lanes over 5 dependency waves; three high-conflict files have single owners |
| Lake Score | 13/14 engineering-review recommendations accepted (93%); the dedicated worker was the user's intentional nonrecommended choice C |

The design, product contract, architecture, code-quality boundaries, test pyramid,
performance constraints, failure handling, and rollout gates are implemented. T1–T8
are complete; T9 remains the live production rollout and evidence phase.

## GSTACK REVIEW REPORT

| Review | Trigger | Why | Runs | Status | Findings |
|--------|---------|-----|------|--------|----------|
| CEO Review | `/plan-ceo-review` | Scope & strategy | 0 | — | User explicitly chose the full Talking-to-camera + Narrated scope |
| Codex Review | `/codex review` | Independent 2nd opinion | 0 | skipped | Current host is Codex; nested Codex pass skipped by policy |
| Eng Review | `/plan-eng-review` | Architecture & tests (required) | 1 | CLEAR (PLAN) | 54 issues/gaps resolved, 0 critical gaps, 0 unresolved decisions |
| Design Review | `/plan-design-review` | UI/UX gaps | 1 | CLEAR (FULL) | score: 6/10 → 9/10, 4 product/design decisions; boxed Direction A approved |
| DX Review | `/plan-devex-review` | Developer experience gaps | 0 | — | — |

**VERDICT:** IMPLEMENTATION COMPLETE — production rollout and evidence pending.

NO UNRESOLVED DECISIONS
