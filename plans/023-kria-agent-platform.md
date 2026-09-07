<!-- /autoplan restore point: /private/tmp/detached-autoplan-restore-20260906-110329.md -->
# Kria Domain Agent Platform

Status: APPROVED — `/autoplan` complete
Planned against: `origin/main` at `41c876d03c2ce107c8b28bced30c4d18f1087fde`
Product design: `docs/designs/kria-agent-platform.md`

## Outcome

Turn Nova's existing chat-first creation flow and editor copilot into one durable, domain-bounded Kria agent that owns the workflow from uploaded footage through a publishable first cut and user-directed revisions. Every assistant turn must add interpretation, a creative decision, a high-value question, a receipt-backed action, useful progress, review evidence, or a concrete recovery path; it may never merely restate the brief.

The implementation extends the current `CreationThread` → `CreatorAgentSession` → `PlanItem`/`Job` graph and existing editor/render contracts. It does not create a second project model, expose storage capabilities to the model, or replace the render pipelines.

## Product Contract

- One continuous conversation spans format selection, footage interpretation, initial edit, render review, and revisions. The editor is an inspectable workspace for the same state, not a separate assistant or memory.
- Reversible, server-validated draft edits execute immediately. Editor commit, render/rerender, media removal, and other difficult-to-reverse actions require approval pinned to exact revisions and expiring after 30 minutes.
- The agent releases its control-plane lease after async dispatch. During a render it may answer status/help and interpret one replaceable queued successor intent, but cannot mutate the in-flight generation; it replans that intent after the render settles.
- V1 stops at first cut plus user-directed revisions. Each generation receives one evidence review; Kria may propose one improvement but cannot autonomously start another render.
- External publishing and general-purpose non-video assistance are out of scope.

## Canonical State and Public Contracts

| State | Authority | Contract |
|---|---|---|
| Conversation | `CreationThreadEvent` | Append-only ordered semantic events; `CreationThread.state` is a rebuildable UI projection; unified threads store no second assistant transcript in `CreatorAgentEvent` |
| Runtime ownership | `CreationThread.runtime_version` | Immutable at thread creation: `1 = legacy`, `2 = unified`; flags affect new threads only |
| Workflow | `CreatorAgentSession` | Long-lived active target, ownership epoch, and question/agent/render/iteration budgets; status is a compatibility projection for unified threads |
| Turn | New `CreatorAgentTurn` | One row per user turn: source event, lifecycle, plan/observation links, cancel state, lease owner/epoch/expiry; partial unique indexes allow one active mutating turn and one queued successor per thread |
| Working edit | New `CreatorEditDraft` | Item+variant scoped immutable full snapshots, exact base job/generation, monotonic revision, parent/source-execution IDs, snapshot hash, and one partial-unique head; writes serialize on the owning `PlanItem` and head-only Undo creates a new revision |
| Rendered edit | Current `Job` variant selected by `PlanItem` | Exact `render_generation_id`; existing editor-commit and creator-craft validation remain authoritative |
| Side effects | `CreatorAgentExecution` | Idempotent typed receipts pinned to thread/session/draft/job/generation/manifest/ownership identities |
| Approval | New `CreatorAgentApproval` | One pinned consequential bundle with expiry and terminal audit state; approval never stores or trusts model-authored risk/pins |

Add versioned inert schemas:

- `KriaTurnPlan`: `schema_version`, `mode = respond|act`, optional direct response, evidence references, ordered tool intents with dependencies, and prospective approval summary. It cannot author pins, risk, idempotency keys, storage references, or completion claims.
- `KriaObservedTurnResponse`: receipt/evidence-backed user-facing outcome with `turn_value`, message, and next actions; it contains no unexecuted tool intents.
- `KriaToolDefinition`: name, version, typed arguments/results, capability requirements, risk class, target kind, execution mode, retry policy, and user-facing failure class.
- `KriaApproval`: grouped intent IDs, exact target pins, consequence/cost copy, expiry, and `pending|approved|denied|expired|cancelled|consumed` status.
- `KriaProblem`: stable code, phase, plain-language message, retryability, recovery class/action, trace ID, and relevant current revision/target identities. Runtime-v2 routes use this envelope instead of mixing bare `detail` strings and ad hoc dictionaries.
- Extend execution receipts with dependencies, exact target pins, risk, the lifecycle below, typed result/error, and observed event linkage.

Execution lifecycle is explicit: `pending → running → awaiting_approval → accepted → dispatched → completed`, with terminal `failed|cancelled|stale|duplicate|outcome_unknown`. `accepted` means the exact edit and queued Job committed; `dispatched` means broker publication returned; neither means the render completed. Synchronous draft tools may move directly from `running` to `completed`. Only `completed` receipts support completed-action language; accepted/dispatched render receipts support queued/rendering language with exact job, variant, and generation IDs.

Public API contract for runtime-v2 threads:

- `POST /creation-threads/{id}/turns` accepts `{message, client_event_id, expected_thread_revision}` and returns `202` with `{turn_id, thread_revision, status}`. A duplicate key with the same body replays; a different body returns `409`.
- `POST /creation-threads/{id}/turns/{turn_id}/cancel` cancels only planning, a queued successor, or a pending approval; render cancellation remains the existing exact Job action and is best-effort.
- `POST /creation-threads/{id}/approvals/{approval_id}/{approve|deny}` accepts the expected approval and draft revisions. Stale/expired/consumed requests are idempotent `409` projections with Replan metadata, never execution.
- `GET /creation-threads/{id}?after_sequence=N` returns the compact thread projection plus only later semantic events. Omit the cursor for bounded newest-page bootstrap; paginate older history separately.
- `GET /creation-threads/{id}/draft` returns the current authoritative draft, revision, hash, base generation, and ETag; `PUT` is reserved for validated editor Save with `If-Match`/revision CAS.

Existing creation actions and `/copilot/turn` remain compatibility paths for runtime-v1 threads. Runtime-v2 projections remain readable by the current client, but old clients cannot mutate v2 drafts and receive an upgrade-required action instead of silently taking authority.

## Runtime and Tool Policy

1. In one short transaction, sequence the user event, create/replay its `CreatorAgentTurn`, and enqueue the idempotent `run_kria_turn(turn_id)` task on the new `agent-control` queue. The API returns `202`; it never holds a database lock across broker or model I/O.
2. The task claims the turn with a 15-second database lease and 5-second heartbeat, then resolves a trusted snapshot and versioned capability/tool manifest from server-owned state. No row lock remains held during evidence or model network calls.
3. Ask the runtime-v2 Main Creator agent for a `KriaTurnPlan`; reacquire locks in the global order `ContentPlan → Persona → PlanItem → Job → CreatorAgentSession → CreatorAgentTurn → CreatorEditDraft(head) → CreatorAgentApproval → CreatorAgentExecution → CreationThread`, verify lease epoch and revisions, then validate schema, evidence IDs, tools, ordered dependency groups, budgets, and risk. A transaction that starts by locking `CreationThread` may append/project only and must commit before acquiring any earlier graph row.
4. For `respond`, append the direct value-adding response. For safe draft work, prevalidate and atomically apply the entire server-side operation bundle, persist the new draft plus receipts, refresh state, then generate an observed response. For consequential work, persist a pinned approval and display prospective copy only.
5. On approval, call a shared service extracted from the current editor-commit route. In one transaction it locks in the global order, verifies ownership and every approval/draft/generation pin, stages the exact canonical edit and queued Job generation, consumes the approval, and sets the execution to `accepted`; then it commits and calls the existing `enqueue_orchestrator` dispatch surface. The deterministic Job task ID plus a maintenance reconciler closes crashes before/after broker publication; do not implement this as internal HTTP and do not add a generic outbox table.
6. The render task's terminal write schedules idempotent `resume_kria_turn(turn_id)` on `agent-control`; normal thread refresh and the maintenance sweep are backstops. Resume builds bounded exact-generation evidence, appends the observed result, then claims and replans any queued successor from a fresh snapshot.

One mutating turn runs per thread. A newer message may supersede planning before a receipt commits; after that it becomes the single queued successor, replacing the prior queued turn only through an explicit user action. Draft/device conflicts refresh and replan rather than merge. Duplicate turns, approvals, dispatches, and callbacks replay the existing receipt. Expired leases are taken over only after reconciling `running`/`outcome_unknown` executions. Terminal capability/budget exhaustion uses the existing session status `failed` with a typed `last_error`; do not add a second `blocked` state.

Recovery order is fixed: refresh stale state → reconcile ambiguous outcome → retry one proven-transient failure → select an exposed intent-preserving alternative that does not add cost or quality loss → ask one decision-changing question → report the blocked capability with the closest valid manual handoff. Additional renders and quality-reducing alternatives always require approval. Per turn: at most two model continuations, eight tool calls, one transient retry, one render dispatch per approval. The agent-control task uses configurable per-call timeouts, a 90-second soft limit and 120-second hard limit; the UI shows durable progress immediately and the release SLO requires first useful assistant output p95 under 30 seconds. Lease TTL/heartbeat are configurable, use database time, and are finalized from shadow p95/p99 measurements; the starting 15s/5s values may ship only if fewer than 0.1% of valid turns lose a live lease. The light worker consumes `maintenance,agent-control`; if agent queue wait p95 exceeds two seconds or maintenance misses its SLO, split it into a dedicated process group before cohort rollout.

Runtime v2 uses new versioned planner/observer schemas and prompts; it does not widen the legacy `AskUser|ProposeStrategy|ReviewDecision` union in place. The observer gets only refreshed state, settled receipts, and bounded evidence. If observation model output fails validation or times out, a deterministic receipt formatter emits the honest result/recovery so tool success is never hidden by a second model failure.

## Existing Capability Reuse and Portability Audit

Freeze the current Edit Copilot operation catalog and classify every operation:

- `portable`: deterministic from the authoritative snapshot and executable/validatable server-side; expose as a tool.
- `adapter_required`: dependent on client calculation; produce a client proposal only. Promote to authoritative success only when Save lets the server reconstruct and validate the exact post-state.
- `unsupported`: omit from tools and record a privacy-safe capability-gap category.

Launch-critical operations cannot remain adapter-required. Noncritical adapters open the same server draft in the editor, return to the same thread after validated Save, and do not count as agent completion.

The server is the only executor for portable runtime-v2 operations. Extract the current editor-commit validation/persistence sequence into a service shared by the existing route and Kria; extend the existing `creator_craft` compiler and operation schemas for the audited subset. Do not copy the TypeScript `apply-ops` switch into Python. The web hydrates and displays the returned authoritative draft/post-state; browser reducers remain runtime-v1 compatibility until removed.

The minimum universal tools are project/media/edit inspection, format/strategy selection, atomic draft bundle, head-only Undo, approval, editor commit/render, status/review/reconcile/retry/cancel, and variant selection. Required edit subsets:

| Canonical formats | Minimum edit operations |
|---|---|
| `montage` | timeline reorder/remove/trim/split/duration, text/title, music/mix, transition, look |
| `day_vlog`, `single_hero` | guided timeline, text/title, transition, look |
| `talking_head`, `subtitled` | caption text/timing/style, speech-cut candidate, output trim, mix |
| `narrated`, `narrated_planned`, `narrated_ready` | voiceover readiness, supporting timeline, text/title, voice/background-music mix |
| mixed image/video inputs | unused-source insertion, image duration, image stacking plus the parent format subset |

Optional feature-flagged effects do not gate launch unless enabled for the cohort. New render primitives are a separately prioritized roadmap unless an existing-format acceptance scenario cannot complete without one.

## Conversation and UI

- Replace generic bubble-per-event rendering with semantic transcript rows: user messages and value-adding assistant responses are bubbles; planning is transient/quiet status; draft results use compact change cards with inspect/Undo; approval is one exact-state action card; progress is one updating card; ready/review combines playable result, evidence, and next move.
- Remove any UI copy derived by echoing `thread.state.intent`, including the current confirmation card description. Approval copy must describe the proposed editorial decision and consequence.
- Old approval cards disable on expiry or state change and offer Replan. Late events update their historical generation card without stealing focus.
- While rendering, status/help remains responsive and one queued intent appears with an “after this render” marker plus replace/cancel controls.
- Keep the existing desktop split workspace, mobile Chat/Editor tabs, composer attachment affordances, accessibility baseline, and embedded editor. The editor hydrates from `CreatorEditDraft`; browser state is a cache, not authority.
- Failures state the problem, cause when known, what remained unchanged or did complete, and one valid next action. Internal codes never appear as primary user copy.
- Append-only lifecycle events carry a stable artifact key `{job_id, variant_id, generation_id}`; the read model/UI coalesces them into one mutable-looking card. Event rows themselves are never updated.

## Security and Privacy

- Each resolver/executor rechecks creator ownership and coherent thread/session/item/job linkage plus manifest, context, draft, generation, and ownership pins.
- Model-visible media and catalog references are opaque IDs. URLs, paths, credentials, arbitrary code, renderer graphs, and direct queue/storage capabilities remain excluded.
- Treat all transcript, OCR, filename, metadata, and footage-derived text as opaque untrusted data. Tool selection policy and authorization are deterministic outside the model.
- Observability stores minimized/redacted intent categories, selected tools, receipt/failure class, latency, recovery, editor escape, abandonment, and outcome. Do not store raw prompts, media, frames, or transcripts by default.
- Real replay media requires explicit consent, access control, purpose, retention date, and revocable deletion of source plus derivatives.

## Delivery Milestones

0. **Outcome and feasibility baseline:** observe Nermin's three uncoached projects plus 5–10 additional target-user sessions when available; measure accepted/exported cuts, manual corrections, hands-on time, render wait, abandonment, and editor/CapCut escape. Freeze an independent request corpus before capability resolution. Audit the top 10–15 intents for evidence readiness and server portability. If workflow fragmentation/recovery is not a top-two failure cause, or fewer than 80% of wedge intents are portable without duplicating the editor engine, resequence toward media understanding or a server-generated full-commit proposal before authorizing generic runtime work.
1. **Architecture spike, contracts, and golden slice:** prove one non-rendering vertical path first: accept a turn → choose a registry-backed read tool → write a receipt → append an observed semantic event → reproduce the same trace from a checked-in fixture. Then add the minimum required v2 records: immutable runtime version on thread; `CreatorAgentTurn`; `CreatorEditDraft`; `CreatorAgentApproval`; receipt extensions; and nullable `CreationThreadEvent.source_agent_event_id` with a per-thread partial unique index for bounded legacy projection. Reuse `Job` plus `enqueue_orchestrator` and its deterministic task ID as the render dispatch ledger; add a targeted pending-dispatch reconciler, not a generic DAG/outbox framework. Freeze the tool registry, two-envelope protocol, error contract, risk policy, migrations, state-machine tests, and cross-format eval fixtures before parallel implementation.
2. **Shadow runtime:** side-effect-free tool planning against production-like snapshots; no user-visible output or extra paid media analysis. Promote only with zero unsafe intents, 100% valid turn-value evidence, and ≥90% exact intent+target and tool-selection match on ≥30 cases for every format token.
3. **Internal durable execution:** server draft where proven necessary, portable tool executors, approval, receipt-backed dispatch/reconciliation, async continuation, render evidence, semantic transcript UI, and legacy compatibility adapters.
4. **All-format named cohort:** no external user enters the unified path until every canonical format passes its required operation subset, failure/concurrency/cancel/rollback matrix, and real-product QA. Nermin's three consented projects are mandatory acceptance cases.
5. **Cutover:** server draft is canonical for agent-authored edits; session-storage copilot history stops owning conversation state; keep versioned old-client projections, reconciliation tooling, and per-phase kill switches until stable.

## Test and Release Gates

- Add structural and behavioral evals for the two-envelope protocol, turn-value evidence, no-parrot behavior, tool/evidence validity, intent/target selection, format-specific tool choice, injection resistance, and recovery decisions. Any Main Creator prompt change bumps its prompt version and runs the live eval loop.
- Backend tests cover migrations/backfill, authority projections, lease expiry/takeover, CAS conflicts, head-only Undo, approval lifecycle, multi-tool atomicity/partial groups, Job dispatch/reconciliation, duplicate callbacks, cancellation races, queued intent after render completion/failure, evidence cache keys/failure, auth/tenant isolation, and flag rollback.
- Frontend tests cover semantic event projection, no intent echo, draft change/Undo cards, approvals and expiry, render-time status/help/queued-intent controls, refresh/cross-device conflict, partial success, legacy fallback, editor draft hydration, keyboard/screen-reader order, 44px targets, 200% zoom, and reduced motion.
- Each of the eight canonical format tokens gets ≥30 labeled scenarios. All safety/recovery scenarios pass; execution completes on ≥27/30; every failure has a specific valid recovery.
- Across ≥200 supported requests, execution completion is ≥90% with Wilson 95% lower bound ≥85%. A separate ≥40-case injected-failure corpus has ≥95% specific-recovery coverage with lower bound ≥85%. Generic terminal limit/error endings are <5% with upper bound <8%.
- Latency corpus: ≥50 renders, ≥8 per format family, warm/cold evidence cache and nominal/elevated queue load. Standard input is 3–10 uploaded clips, each ≤2 minutes and total source ≤10 minutes. From upload completion, first playable cut p50 <5 minutes and p90 <10 minutes; report control-plane, queue, evidence, and render components.
- Nermin acceptance: lifestyle, matcha-business, and travel projects each reach a creator-selected publishable result in <30 minutes hands-on time with no required CapCut escape.
- Outcome gate, measured against the frozen pre-capability corpus and current-Nova/CapCut baselines: first-cut acceptance without manual-editor escape, median manual corrections before export, hands-on minutes saved, export/post decision within 24 hours, and 14-day repeat creation. Tool correctness and recovery are guardrails; they cannot substitute for a creator outcome improvement.

## Rollout and Rollback

Add separate backend and `NEXT_PUBLIC_` frontend unified-agent flags plus an immutable per-thread owner/version so deploy skew cannot switch authority mid-thread. Deploy migration/API/worker first, then web. Shadow mode writes only redacted diagnostic records. Internal and cohort allowlists precede percentages.

Rollback stops new unified planning/execution, lets committed jobs settle, cancels pending approvals/queued intents, and renders old-client-compatible projections from committed Job state plus versioned drafts. Reconciliation must report orphaned turns/leases, approvals, pending dispatches, executions, drafts, and generation mismatches before each rollout increase.

## NOT in Scope

- General-purpose conversation outside short-form video.
- Direct social-platform publishing.
- Autonomous multi-render polishing or unbounded self-critique.
- Rewriting FFmpeg/render pipelines or replacing the transactional editor-commit path.
- Exposing every Copilot operation at launch; only the frozen cross-format minimum must be server-portable.
- Retiring legacy routes/models before cutover and rollback evidence pass.
- A generic workflow engine, general tool DAG framework, or reusable agent SDK not demanded by Kria's bounded video-editing scenarios.
- Render-worker topology/capacity changes; the existing queue-latency P2 in `TODOS.md` is an external launch dependency if the latency corpus misses its SLO.

## What Already Exists

- Durable creation threads/events, linked plan/item/job/session graph, revision fencing, upload/media attachment, render progress, and thread UI.
- Main Creator manifests, typed creative strategies, confirmation controller, execution receipts, render review, and bounded craft commands.
- Rich Edit Copilot parser/operation catalog and goldens, client apply/Undo behavior, proposal/execution receipts, and the atomic server editor-commit endpoint.
- Current format-aware renderers, variant generation IDs, capability flags, quality review/auto-iteration policy, and extensive pipeline invariants.

<!-- AUTONOMOUS DECISION LOG -->
## Decision Audit Trail

| # | Phase | Decision | Classification | Principle | Rationale | Rejected |
|---|---|---|---|---|---|---|
| 1 | CEO | Reuse the thread/session/job graph and shared editor contracts | Mechanical | DRY | The repository already owns identity, revisions, execution receipts, and render state | Greenfield agent/project model |
| 2 | CEO | Add Milestone 0 outcome baseline and independent request corpus | Auto-decided | Completeness | Prevents manifest-defined success from hiding creator failure | Begin migrations before observing workflows |
| 3 | CEO | Make evidence readiness and operation portability go/no-go gates | Auto-decided | Bias toward action | Finds the actual bottleneck before building generic orchestration | Treat both as downstream implementation details |
| 4 | CEO | Prove missing invariants before adding each new persistence/runtime abstraction | Auto-decided | Explicit over clever | Existing session/execution/editor-commit records may cover the first slice | Pre-commit to a generic DAG/outbox platform |
| 5 | CEO | Extend admin job-debug with the full Kria turn chain | Auto-decided | Boil lakes | It is inside the operational blast radius and makes failures reconstructable | Logs-only operations |
| 6 | CEO | Keep external publishing and autonomous multi-render refinement out | Mechanical | Pragmatic | Neither is needed to fix the observed build-and-polish workflow | Expand the agent beyond approved outcome |
| 7 | CEO | Keep the all-format external cohort gate pending the final user-challenge decision | User challenge | User sovereignty | The user explicitly chose all formats; one independent reviewer recommends a narrower learning cohort | Silently narrow launch |
| 8 | CEO | Keep separate approval before every render pending the final taste decision | Taste | Explicit over clever | Exact approval is safe, but the originating request may already express consent for one bounded first render | Silently relax approval |
| 9 | CEO | Treat render capacity as a measured external dependency, not hidden platform scope | Auto-decided | Pragmatic | Existing queue topology can violate the SLO, but solving capacity is a distinct infrastructure program | Pretend orchestration controls render latency |
| 10 | CEO | Defer longitudinal creator-preference memory | Auto-decided | Bias toward action | It may become moat, but first-cut execution and evidence must work first | Add profile-learning infrastructure now |
| 11 | Design | Make the thread an outcome ledger, not a bubble stream | Auto-decided | Hierarchy | Persistent UI should show user intent, observed results, actionable artifacts, and recoveries; planning chatter is transient | Render every durable event as a bubble/card |
| 12 | Design | Use one mutable lifecycle artifact per generation | Mechanical | Calm technology | Coalescing approval, progress, partial, and ready state prevents transcript churn and preserves exact-generation history | Append one card per callback |
| 13 | Design | Show no pre-execution assistant completion claim | Mechanical | Honest feedback | Prospective language belongs only on approval; completed-change language requires an observed receipt | Display the model plan before tools settle |
| 14 | Design | Preserve reading position unless the creator is already at the live edge | Auto-decided | User control | Late worker updates must not pull a creator away from history or the editor | Always scroll to the newest event |
| 15 | Design | Keep the composer available during renders with a single visible queued-intent chip | Auto-decided | Continuity | Status questions and the next request should remain possible without implying in-flight mutation | Disable conversation until the render ends |
| 16 | Design | Keep cards only for media, state-changing actions, receipts, and recovery | Auto-decided | Cards earn existence | Plain conversational outcomes should remain lightweight; stacked generic cards recreate dashboard slop | Put every assistant response in `ChatArtifactCard` |
| 17 | Design | Keep the current desktop split and mobile Chat/Editor tabs | Mechanical | Existing-system coherence | These responsive shells already satisfy the creator workflow and reduce migration risk | Invent a new navigation/layout model |
| 18 | Eng | Add an explicit per-message `CreatorAgentTurn` and keep session state as compatibility projection | Auto-decided | Explicit over clever | Cancellation, leasing, queued successor, and observation need a durable identity not overloaded onto project budgets | Put every new lifecycle field on `CreatorAgentSession` |
| 19 | Eng | Use committed `Job` + `enqueue_orchestrator` as the render dispatch ledger | Mechanical | DRY | Nova already enforces deterministic task IDs, central dispatch, cancellation, and recovery around Job rows | Add a second generic render outbox |
| 20 | Eng | Consume approval, persist the edit/Job generation, and accept the execution in one transaction | Mechanical | Correctness | A split transaction can consume consent while losing the edit or vice versa | Call the editor route after consuming approval |
| 21 | Eng | Split accepted, dispatched, and completed execution states | Mechanical | Honest state | Broker acceptance is not rendered completion and must not license “I rendered it” copy | Keep legacy `succeeded` for every phase |
| 22 | Eng | Write runtime-v2 semantic events directly to the thread | Auto-decided | Single authority | The current `_sync_agent` mirror has a split-commit window and a 100-ID replay bound | Maintain two conversation logs for v2 |
| 23 | Eng | Run turns on an `agent-control` queue consumed initially by the light worker | Auto-decided | Durable async work | A 202/poll contract survives API restarts and long model/tool turns without blocking the render worker | Continue long model work inside HTTP requests |
| 24 | Eng | Isolate v2 planner/observer schemas and prompts from the legacy agent union | Auto-decided | Reversibility | Runtime-v1 threads must remain byte-compatible during rollout and rollback | Expand the legacy response union in place |
| 25 | Eng | Add cursor event reads and separate authoritative draft fetch | Auto-decided | Performance | Long-lived polling must not repeatedly hydrate full transcript and editor JSON | Return all history and draft state on every poll |
| 26 | Eng | Use append-only lifecycle events with projection-time coalescing | Mechanical | Data integrity | The database forbids event mutation while the UI needs one mutable-looking generation card | Update old event rows |

## Phase 1 — CEO Review

### Step 0: Strategy and Scope

**0A — Premise challenge.** The user pain is observed and consequential: a working creator returns to CapCut, loses hours, and sometimes does not post. The strongest premise is that a conversational surface must own a video outcome rather than collect a brief. The weakest premises were that orchestration is the first bottleneck, that existing media evidence is sufficient for precise editorial choices, and that all eight backend format tokens are the right commercial launch unit; Milestone 0 now tests the first two, while the third remains an explicit user challenge.

**0B — existing-code leverage.** The plan reuses the durable thread/event projection (`models.py`, `creation_threads.py`), controller and budgets (`CreatorAgentSession`, `creator_sessions.py`), manifest/compiler (`creator_capabilities.py`), exact-generation craft executor (`creator_agent.py`, `creator_craft.py`), rich operation grammar (`edit_copilot.py` and web apply helpers), transactional save/render boundary (`editor-commit`), render/review workers, and admin pipeline tracing. New code is restricted to the missing unified protocol, authoritative working-draft delta where proven necessary, orchestration policy, and projections/adapters.

**0C — dream-state delta.** The plan is directional infrastructure for the right 12-month outcome, but it deliberately stops before preference learning and publishing:

```text
CURRENT
two partial assistants; ask/propose before render; browser-local edits after render;
limits become terminal; user carries intent and recovery
   |
   v
THIS PLAN
one durable Kria thread; evidence-grounded typed tools; safe draft action;
receipt-backed claims; confirmed renders; exact recovery; all current formats
   |
   v
12-MONTH IDEAL
Kria remembers approved creative preferences, anticipates the next useful edit,
learns from accepted outputs, and reliably carries footage to channel-ready delivery
without exposing editor mechanics
```

**0C-bis — implementation alternatives.** The full platform remains the destination, but it must earn abstractions through the spike:

| Approach | Effort / risk | Strengths | Weaknesses | Existing reuse | Decision |
|---|---|---|---|---|---|
| A. Durable editor copilot | M / medium | Fastest path to persistent revision chat; reuses local apply and Save | Does not own pre-render creation; preserves client-trust split | Copilot parser, session storage migration, editor-commit | Rejected as incomplete (7/10) |
| B. Full-commit compiler | L / medium | One model plan produces a complete validated editor commit; smaller runtime | Weaker multi-turn/tool recovery and incremental draft feedback | Creator planner, editor-commit, execution receipts | Spike fallback if portability <80% (8/10) |
| C. Unified Kria runtime | XL / high | One mental model and durable control plane across creation/revision; extensible | Highest migration and evidence risk | All current graph and operation contracts | Chosen, gated by Milestone 0 (10/10) |

**0D — selective expansion.** Accepted: baseline workflow capture, independent outcome corpus, evidence/portability gates, and a Kria chain in the existing admin job-debug surface. These are within the blast radius and prevent invisible failure. Deferred: longitudinal preference/outcome memory, because it needs a separate consent/data strategy; skipped: generic agent SDK/tool marketplace, because it duplicates no current user need.

**0E — temporal interrogation.** In foundations, implementers need canonical ownership, protocol phases, migration precedence, and a proved need for each new table. In core logic, they need exact tool portability, risk metadata, bundle atomicity, lease fencing, and the distinction between planned and observed language. In integration, the surprises are browser-local calculations, async broker ambiguity, generation changes during render, and feature-flag skew. In polish, they need deterministic fixtures, real consented media, outcome denominators, accessibility state restoration, admin reconstruction, and rollback rehearsals.

**0F — mode.** `SELECTIVE_EXPANSION` is fixed by autoplan. Scope remains the approved full agent platform; small blast-radius protections were added, while preference memory, publishing, generic framework work, and render-capacity implementation remain outside.

### CEO Dual Voice

The Claude strategic voice confirmed the customer problem, but challenged scope calibration, alternatives, competitive evidence, footage evidence depth, operation portability, outcome metrics, all-format launch gating, and mandatory repeat render confirmation. Codex is `[codex-unavailable]`: CLI `0.140.0` is authenticated but cannot run the configured review model, confirmed during preflight.

| Dimension | Claude subagent | Codex | Consensus |
|---|---|---|---|
| Premises valid? | Concern | N/A | Not confirmed; feasibility gates added |
| Right problem to solve? | Confirmed | N/A | Single-voice confirmed |
| Scope calibration correct? | Disagree | N/A | User challenge: all-format gate |
| Alternatives sufficiently explored? | Disagree | N/A | Fixed with three real architectures |
| Competitive/market risks covered? | Disagree | N/A | Benchmark task added |
| Six-month trajectory sound? | Concern | N/A | Protected by 30/60/90-day outcome gates |

### Section 1 — Architecture Review

The architecture is coherent only if the new runtime remains an orchestrator over existing authorities. A generalized workflow engine, arbitrary DAG, second render state machine, or duplicated editor validator would create the exact split-brain this work is meant to remove. The spike therefore proves one end-to-end turn using current events, session state, receipts, and full editor commit before adding a separate draft table or lease fields.

```text
Browser /plan + embedded Editor
        |
        v
CreationThread API -- sequence/CAS --> Kria Turn Runtime
        |                                  |
        |                            resolve snapshot
        |                                  v
        |                        Capability + Tool Registry
        |                                  |
        |                      policy/pins/risk/budgets
        |                                  v
        +--> CreationThreadEvent <-- Observed Response
                                           |
                              +------------+------------+
                              |                         |
                       safe draft bundle          approval record
                              |                         |
                              v                         v
                    CreatorEditDraft*          existing editor-commit /
                    (*only if spike proves)      creator-craft executor
                              |                         |
                              +----------+--------------+
                                         v
                               receipt-backed dispatch
                                         |
                                         v
                              Job / render / exact review
                                         |
                                         +--> pipeline trace + admin debug
```

Data shadows are explicit:

```text
USER INPUT -> validate/sequence -> snapshot -> plan -> policy -> persist/execute -> observe -> UI
   nil       422/no event          missing   typed       conflict           unknown      stale
   empty     value question       deleted    schema      duplicate          partial      recover
   huge      length reject        stale      refusal     DB failure         timeout      exact card
```

At 10x load, model calls and DB lock contention fail before storage; use short leases, bounded context, indexed lookups, and per-user rate/budget limits. At 100x, render capacity and evidence generation dominate; they need admission/queue observability and may block rollout, but do not justify coupling Fly machine management into the Kria runtime. The model provider remains a single logical planning dependency; direct deterministic status/retry/cancel paths keep the thread useful during provider failure.

### Section 2 — Error & Rescue Registry

| Method/codepath | Failure class | Rescued? | Rescue action | User sees |
|---|---|---:|---|---|
| append user turn | validation / duplicate / DB unavailable | Yes | reject invalid; replay duplicate; retry DB once then preserve composer | Specific invalid field or “message not sent—retry” |
| acquire/renew lease | busy / expired / lost epoch | Yes | return current work; takeover after TTL and reconciliation | Current progress; retry only if needed |
| resolve snapshot | not found / ownership mismatch / stale target | Yes | 404 fail-closed or refresh/replan once | Project unavailable or refreshed plan |
| build evidence | missing / stale / producer failure | Yes | omit unsupported claim; request needed media or degrade review | What evidence is missing and next action |
| model planning | timeout / 429 | Yes | one bounded retry/backoff | Quiet progress, then retry action |
| model planning | empty / malformed schema / refusal | Yes | no tools execute; retry once or deterministic recovery | “I couldn’t form a safe edit plan” + next action |
| validate tool plan | unknown/unavailable tool / bad dependency | Yes | reject intent and replan once within manifest | Closest supported action or clarification |
| apply draft bundle | invalid op / CAS conflict / DB failure | Yes | validation-first atomic abort; refresh/replan | Nothing changed; precise conflict/fix |
| create/consume approval | expired / denied / stale / duplicate | Yes | no-op or idempotent replay; offer Replan | Disabled exact card and reason |
| commit edit | 422 validation / 409 generation | Yes | preserve draft; refresh/rebase or ask | Nothing rendered; exact correction |
| persist dispatch receipt | DB commit uncertainty | Yes | re-read idempotency key before any publish | Checking whether work started |
| publish render task | broker timeout / rejected publish | Yes | reconcile task/job identity; mark retryable without duplicate | Current edit unchanged or render may still complete |
| reconcile callback | duplicate / out of order / wrong generation | Yes | ignore nonmatching callback; idempotent update | No duplicate bubble; historical status only |
| render | parent failure / variant partial | Yes | exact retry or playable partial + failed variants | What is usable and what can retry |
| cancel | worker wins race / already terminal | Yes | keep unselected output; report actual outcome | Cancelled, or “finished before cancellation” |
| client adapter Save | untrusted/stale post-state | Yes | server reconstructs+validates or refuses success claim | Manual Save needed or precise conflict |
| quality review | evidence/model failure | Yes | ready result remains usable; no invented judgment | Video ready; review unavailable |

No catch-all may turn an exception into an apparent success. Boundary catch-alls may classify an unknown failure only after recording full operation, user/thread-safe IDs, pins, provider/job identity, and traceback; they emit `outcome_unknown` and enter reconciliation rather than swallowing.

### Section 3 — Security & Threat Model

| Threat | Likelihood / impact | Mitigation in plan |
|---|---|---|
| IDOR through thread/item/job/draft IDs | Medium / High | Recheck creator ownership and coherent graph at every resolver and executor; opaque IDs alone grant nothing |
| Prompt injection from transcript/OCR/filename | High / High | Quote as untrusted evidence; deterministic policy, authorization, tool registry, and risk outside model |
| Model emits storage URL/path or arbitrary renderer graph | Medium / High | Strict schemas forbid capabilities; server resolves opaque refs; fail closed before lookup |
| Approval replay after state change | Medium / High | Exact revision/generation/manifest/ownership pins, expiry, consumed status, idempotency key |
| Cross-device lost update | Medium / Medium | Draft CAS; refresh and replan; no v1 merge |
| Tool-argument resource abuse | Medium / High | Existing size/duration/count limits plus per-tool bounds and session budgets |
| Sensitive media in logs/evals | Medium / High | Redacted structural telemetry; consented fixture store with access, retention, deletion |
| Client forges adapter completion | High / Medium | Client receipt is proposal only; server reconstructs/validates canonical post-state |
| Denial of service via turns/status polling | Medium / Medium | Rate limits, bounded turns/context, coalesced progress, indexed queries |

No new secret or third-party dependency is required for the platform foundation. Model/evidence provider keys keep existing env/rotation patterns. Every consequential receipt and approval is audit logged; model-visible text is HTML-escaped by React and never becomes direct renderer code.

### Section 4 — Data and Interaction Edge Cases

```text
INPUT -> VALIDATE -> SNAPSHOT -> PLAN -> EXECUTE/PERSIST -> OBSERVE -> RENDER
 nil      422        404          invalid  atomic abort       none      none
 empty    ask value  empty edit   respond  no-op prohibited  value     none
 long     422        bounded      timeout  budget stop        rescue    none
 stale    accept     refresh      replan   CAS/409             stale     exact retry
 dup      replay     same         same     idempotent receipt existing  existing
```

Double submits reuse client event and tool idempotency keys. Navigation/refresh rebuilds from thread events, active draft, approvals, and exact Job state. Deploys may interrupt planning but not committed receipts; expired leases reconcile. One queued render-time intent is replaceable/cancellable, render completion always forces fresh planning, and late older-generation progress cannot update the active card.

Partial operation groups report successful and failed receipts separately; a failed later group never rewrites the earlier result into “nothing happened.” Empty media, no editable variant, expired signed playback URL, disabled format/operation flags, and insufficient evidence each get a specific next action. A two-hour queue remains a visible queued state with cancel/status, while rollout SLOs fail rather than rebranding that delay as success.

### Section 5 — Code Quality Review

The main code-quality risk is copying the large browser operation switch into Python. Shared declarative operation metadata and existing editor-commit validators should be the seam; each portable executor must be small and format-independent where the underlying contract is shared. `creation_threads.py` and `creator_agent.py` are already large, so orchestration, policy, evidence packaging, and draft application belong in focused services with route functions limited to auth/locking/serialization.

Naming is fixed around intent (`KriaTurnPlan`, `KriaObservedTurnResponse`, `CreatorEditDraft`) rather than mechanics. Avoid a generic `WorkflowEngine`, `ToolGraph`, or `AgentMemory`; each would imply unsupported generality. Any method with model result parsing, policy branching, execution, and response composition combined must be split before merge; branch-heavy behavior is represented by explicit enums/tables and independently tested transition functions.

### Section 6 — Test Review

```text
NEW UX FLOWS
  unified message -> direct answer | draft result | approval | progress | review | recovery
  draft inspect/Undo; approval expiry; render-time queued intent; cancel; cross-device refresh
NEW DATA FLOWS
  turn -> snapshot/evidence -> plan -> policy -> receipt -> observed response
  approval -> commit -> dispatch -> worker callback -> reconciliation -> review
NEW CODEPATHS
  lease acquire/renew/takeover; CAS; dependency groups; adapter promotion; rollback gate
NEW ASYNC WORK
  render dispatch continuation; evidence build/review; stale receipt reconciler
NEW EXTERNAL CALLS
  planning model; existing media/model evidence producers; Celery broker
NEW RESCUES
  schema/refusal/timeout; stale/duplicate; DB/broker ambiguity; render partial/fail; cancel race
```

Unit tests cover schemas, policy/risk, transition tables, recovery selection, operation portability, evidence references, and turn-value validation. Integration tests cover DB locks/CAS/idempotency/approval/dispatch and existing editor/render boundaries. System tests exercise each format without live model variance via recorded fixtures; a few local end-to-end runs cover real model+media+render. Hostile tests inject instructions through filenames/transcripts, swap ownership IDs, replay approvals/callbacks, expire leases mid-call, and kill API processes after DB commit but before broker acknowledgement.

The Friday-2am test is a crash/restart scenario that proves one user message produces at most one draft revision and one render, then reconstructs the same chat result. The chaos test kills the API or worker at every persistence/dispatch boundary and verifies convergence. Prompt changes to `main_creator.txt` require a prompt-version bump, structural evals, the new multi-turn/tool/recovery fixtures, and live judged evaluation against current fixtures per `CLAUDE.md`.

### Section 7 — Performance Review

The three slowest paths are initial evidence readiness, model planning/observation, and render queue+encode. Context must reference bounded summaries rather than raw timelines/media; contact sheets cap at 12 frames and evidence caches key exact producer/reviewer versions. Thread/event/session/draft/receipt reads require composite indexes for owner/thread sequence, active session target, draft item+variant+revision, execution session+idempotency, pending approval expiry, and dispatch reconciliation status.

Avoid N+1 loading of media, catalog, events, executions, or variants by resolving one compact snapshot query/service. Progress events are coalesced rather than appended per worker tick. The platform adds no heavy payload to Celery; jobs carry opaque IDs and reload state. Render capacity is the likely first production limit and is measured separately; failure of p50/p90 blocks cohort rollout and activates the existing queue-topology backlog rather than causing hidden synchronous waits.

### Section 8 — Observability and Debuggability

Extend the existing `/admin/jobs/{id}` pipeline trace rather than create a second dashboard. One timeline must connect user event, turn plan version, evidence IDs, policy decisions, approval, each execution receipt, dispatch identity, Job generation, review, observed response, queued successor intent, and cancellation. Content is redacted; admins see safe summaries, hashes, timings, enums, and linked internal IDs.

Day-one metrics: turns by value class; pure-paraphrase eval failures; plan schema/refusal/timeout; tool selection and rejection class; draft/approval/render success; stale/CAS/lease takeovers; broker unknown outcomes; recovery coverage; manual editor/CapCut escape; first-cut acceptance; hands-on time; and latency components. Alerts cover authorization invariant failure, duplicate render per approval, outcome-unknown age, orphaned lease/approval/dispatch, sharp generic-ending increase, render SLO breach, and per-format completion regression. Add a runbook for each alert and reconciliation/admin actions.

### Section 9 — Deployment and Rollout

```text
1 additive DB migration + indexes
        v
2 API readers/writers dark + legacy owner default
        v
3 worker understands new receipts/events but unified dispatch off
        v
4 web understands both projections
        v
5 shadow allowlist -> internal execution -> all-format named cohort -> percentages
```

Migrations are additive and must not rewrite large Job JSON or lock event tables for backfill; old rows lazily gain draft/owner projections. Thread owner/version is immutable at creation, so mixed web/API versions cannot switch authority mid-project. Each deploy verifies API health, migration head, old chat flow, new shadow no-side-effects, receipt/job reconciliation, and one format smoke before increasing the gate.

```text
ROLLBACK
disable web entry -> disable new thread ownership -> stop new plans/approvals
      -> let committed jobs settle -> reconcile receipts/Job dispatch
      -> render legacy projection from Job + compatible draft -> verify old editor Save
```

The migration itself remains forward-compatible on rollback; destructive downgrade is not required. A dedicated agent control queue may be used only if existing API/request execution cannot meet the 30-second budget; render topology remains separate.

### Section 10 — Long-Term Trajectory

Reversibility is 4/5: flags and immutable thread ownership protect execution rollback, additive records are backward-readable, and render pipelines remain untouched; the persistent draft schema is the only meaningful path dependency. The architecture creates a reusable internal domain-tool contract for Kria, not a generic agent framework. Versioned schemas, tool registry, evidence cache, and receipts support future preference memory and publishing without making them launch dependencies.

The largest debt risk is a permanent client-adapter tier. Every adapter needs an owner, reason, launch criticality, and removal gate; no adapter can claim action success. The second risk is format-token coupling, so behavior/evals group by product job while retaining token-specific renderer invariants. In 12 months a new engineer should find one architecture doc/runbook explaining ownership and one generated registry, not reverse-engineer policy from route conditionals.

### Section 11 — Design and UX Review

```text
PROJECT/THREAD
   -> user message
      -> direct useful response
      -> draft change card -> inspect / Undo / render approval
      -> approval card -> render progress (status/help + queued next intent)
      -> playable result + evidence review -> revise / accept / editor
      -> failure/partial -> exact recovery -> resumed same thread
```

The hierarchy is outcome first: what Kria decided or changed, the video/draft artifact, and one next action. Internal planning and worker chatter are not speech bubbles. Empty footage, missing evidence, disabled format, loading, stale approval, partial render, cancellation race, and ready states are all explicit; mobile retains the same semantic order in Chat/Editor tabs.

The UI aligns with `DESIGN.md` by keeping quiet loading, progressive disclosure, minimum 44px targets, keyboard order, screen-reader announcements for status changes, reduced motion, and no ornamental chat chrome. The inevitable-feeling detail is continuity: every action card is inspectable and state-pinned, and every failure returns the user to the same goal rather than a new form or assistant.

### Error Flow

```text
failure
  |
  +-- invalid/user-correctable -> no mutation -> exact field/action
  +-- stale/conflict ----------> refresh -> one replan -> outcome
  +-- transient ---------------> one retry -> outcome
  +-- ambiguous side effect ---> reconcile receipt/job identity -> outcome
  +-- partial -----------------> keep succeeded receipts -> Undo/Retry/Continue
  +-- unsupported -------------> valid lower-cost path or manual handoff
  `-- budget/nonrecoverable ----> terminal preserved state + restart action
```

### Failure Modes Registry

| Codepath | Failure mode | Rescued? | Test? | User sees | Logged? |
|---|---|---:|---:|---|---:|
| message sequencing | duplicate/out-of-order | Yes | Yes | existing result/current state | Yes |
| turn lease | owner dies/epoch stolen | Yes | Yes | continued/reconciled turn | Yes |
| evidence | missing/stale/failed | Yes | Yes | bounded claim or missing-evidence action | Yes |
| model | timeout/429/schema/refusal | Yes | Yes | retry or safe recovery | Yes |
| policy | unavailable/unsafe tool | Yes | Yes | alternative or question | Yes |
| draft | invalid bundle/CAS conflict | Yes | Yes | nothing changed + refresh | Yes |
| approval | replay/expiry/state drift | Yes | Yes | disabled + Replan | Yes |
| DB→broker | publish outcome ambiguous | Yes | Yes | reconciling, no duplicate render | Yes |
| callback | duplicate/wrong generation | Yes | Yes | historical/no visible duplicate | Yes |
| render | terminal/partial failure | Yes | Yes | playable partial or exact retry | Yes |
| cancel | completion wins race | Yes | Yes | actual completed-unselected outcome | Yes |
| adapter | forged/stale client claim | Yes | Yes | manual/validation failure | Yes |
| review | evidence/model unavailable | Yes | Yes | playable video, no fake judgment | Yes |
| rollout | flag/version skew | Yes | Yes | thread stays on immutable owner | Yes |

No row is an unrescued silent path; any implementation discovery that creates one is a P1 release blocker.

### What Already Exists

The existing-code inventory in the main plan is confirmed. In particular, current code already has append-only thread and agent events, strong ownership/revision/generation fences, idempotent creator executions, atomic editor commit, typed craft commands, a rich Copilot grammar, render reconciliation, and admin pipeline traces. The new runtime must capture these outputs and call these validators rather than reimplement their semantics.

### NOT in Scope

The main plan's exclusions are confirmed: no general assistant, direct publishing, unbounded autonomous refinement, renderer rewrite, all-operation launch, early legacy deletion, generic workflow engine, or in-plan render-capacity program. Longitudinal preference/outcome memory is deferred to `TODOS.md`; it becomes valuable only after accepted outputs and consent boundaries exist.

### Stale Diagram Audit

`plans/021-chat-first-creation.md` accurately describes the current creation-thread architecture but its statement that executable chat operations are out of scope is superseded by this plan under a new gate. `docs/designs/kria-agent-platform.md` matches this plan's current ownership and runtime direction. No implementation-source diagram is modified by the plan; the implementation must update `docs/runbooks/chat-first-creation.md` when authority changes.

### CEO Implementation Tasks

- [ ] **CEO-T1 (P1, human: ~1d / CC: ~1h)** — Product evidence — Run and encode Milestone 0 workflows before schema work.
  - Surfaced by: premise and outcome review; files: consented external fixtures + `src/apps/api/tests/evals/fixtures/`; verify: signed-off baseline report and frozen request corpus.
- [ ] **CEO-T2 (P1, human: ~1d / CC: ~1h)** — Media evidence — Prove timecoded evidence supports the top observed edit intents.
  - Surfaced by: architecture feasibility; files: creator context/evidence services and eval fixtures; verify: grounding eval on Nermin projects.
- [x] **CEO-T3 (P1, human: ~1d / CC: ~1h)** — Editor operations — Produce the portability inventory and 80% go/no-go result.
  - Surfaced by: current client-owned Copilot; files: `edit_copilot.py`, web op/apply modules, `editor-commit`; verify: each top intent has authority, validator, executor, and render support.
- [x] **CEO-T4 (P1, human: ~1d / CC: ~1h)** — Runtime spike — Prove one durable turn with existing records before adding abstractions.
  - Surfaced by: minimality review; files: creation/creator services and tests; verify: crash-boundary replay produces one draft/one render.
- [ ] **CEO-T5 (P1, human: ~1d / CC: ~1h)** — Outcome evaluation — Add frozen-corpus and creator-outcome gates independent of the manifest.
  - Surfaced by: metric-denominator review; files: eval harness/docs; verify: current Nova and CapCut baselines compared.
- [x] **CEO-T6 (P2, human: ~1d / CC: ~1h)** — Operations — Extend admin job-debug with the Kria turn/receipt chain and alerts.
  - Surfaced by: observability review; files: admin job-debug API/UI and pipeline trace; verify: reconstruct one success, partial failure, stale replan, and cancel race.
- [ ] **CEO-T7 (P2, human: ~1d / CC: ~1h)** — Competitive benchmark — Run the frozen workflows against relevant incumbent/AI editors and state Nova's wedge.
  - Surfaced by: competitive-risk review; files: design/benchmark artifact; verify: time, quality, edit breadth, and recovery comparison.

### CEO Completion Summary

| Review item | Result |
|---|---|
| Mode | SELECTIVE EXPANSION |
| System audit | Two strong partial agents; missing unified authority, tool loop, and recovery; evidence/portability unproved |
| Architecture | 3 issues; spike and reuse rules resolve them |
| Error map | 16 paths, 0 planned gaps |
| Security | 9 threats, all mitigated in contract |
| Data/UX | 14 adversarial cases mapped, 0 intentionally silent |
| Code quality | 4 guardrails: no copied validator, large-route growth, generic framework, branch-heavy engine |
| Tests | 6 flow families; hostile and chaos tests required |
| Performance | 3 slow paths; render capacity is external launch dependency |
| Observability | 1 accepted expansion: unified admin trace + metrics/alerts/runbooks |
| Deployment | Additive staged rollout; immutable per-thread owner; forward-compatible rollback |
| Future | Reversibility 4/5; preference memory deferred |
| Design | 8 state families specified; dedicated design review follows |
| Scope proposals | 5 proposed; 4 accepted; 1 deferred; generic framework skipped |
| Outside voice | Claude subagent ran; Codex unavailable |
| Final-gate decisions | Resolved: all-format cohort retained; separate approval required for every render |

## Phase 2 — Design Review

### Step 0: Design Scope and Existing Leverage

The plan entered at **7/10 design completeness**: it had the correct semantic event types, desktop/mobile shell, accessibility baseline, and recovery principle, but it did not yet specify visual priority, transcript compaction, scroll/focus behavior, copy grammar, or the complete state-to-control mapping. A 10/10 plan makes each server state render deterministically, keeps the current editor and Paper tokens, and leaves no implementer to decide whether an event is speech, status, proof, or action.

`DESIGN.md` is authoritative. Reuse the existing light editorial surface; 260px desktop project rail; full-width pre-render conversation; 420px post-render conversation rail; embedded `EditorShell`; mobile Projects sheet and Chat/Editor tabs; safe-area composer; `ChatBubble`, `ChatThinking`, `Card`, `Button`, `Badge`, `Tabs`, `Dropzone`, and `ProgressTheater` primitives. The gstack design binary is unavailable in this environment, so no generated visual mockup was produced; this review uses the required text wireframe fallback.

### Interaction Architecture

```text
DESKTOP BEFORE FIRST CUT                  DESKTOP AFTER FIRST CUT
+---------+---------------------------+  +---------+---------------+------------------+
|Projects | Project · format · clips  |  |Projects | Conversation  | Editor / video   |
|         |                           |  |         | 420px          | same draft state |
|         | user request              |  |         | result/evidence|                  |
|         | Kria decision/result      |  |         | approval/undo  |                  |
|         | [one active artifact]     |  |         |                |                  |
|         |                           |  |         |                |                  |
|         | [attach][message][send]   |  |         | [composer]     |                  |
+---------+---------------------------+  +---------+---------------+------------------+

MOBILE: project header -> Chat | Editor tabs when a cut exists -> one active pane
        transcript -> sticky safe-area composer; Projects remains a left sheet
```

The transcript is an **outcome ledger**. Its durable visual order is: the creator's request; Kria's value-adding decision/result/recovery; the artifact proving or enabling the next state. Internal planning, tool selection, retries, and worker callbacks never become conversational turns. Each generation owns one lifecycle artifact that changes state in place from approval to queued/rendering to partial/ready/failure; a superseded generation remains historical and inert.

Only four things earn a card: playable/media artifacts; a state-changing approval; an inspectable draft receipt; and a recovery requiring action. Direct answers, evidence-backed observations, and concise creative decisions stay in assistant bubbles. Upload and format collection remain setup artifacts. This avoids a generic stacked-card app while preserving the current component vocabulary.

### Pass 1 — Information Architecture: 7/10 → 10/10

- Primary: the latest editorial outcome or playable video. Secondary: what changed/evidence. Tertiary: one next action. Project metadata and raw operational detail remain quiet chrome.
- A draft result is one compact row/card with a plain-language outcome and up to three highest-impact changes; “View all changes” opens the same editor state. Undo appears only when head-eligible.
- Approval titles name the action and result, e.g. “Render the tighter matcha launch cut?” The body names the editorial delta, expected consequence, and that a render will start; it never contains `thread.state.intent` or a generic “ready to make.”
- Ready state combines the player/poster, Kria's bounded evidence review, variant selection when needed, and one primary action. “Open editor,” download, and retry are secondary actions, not equal-weight buttons.

### Pass 2 — Interaction State Coverage: 6/10 → 10/10

| State | Durable treatment | Primary control / focus rule |
|---|---|---|
| New thread / no format | One setup artifact plus welcoming decision prompt | Format choice; focus remains in natural reading order |
| Format selected / no footage | Format-specific upload artifact | Attach footage; preserve format-changing escape |
| Insufficient or unsupported media | Inline recovery with exact requirement and preserved uploads | Add/replace media or choose valid format |
| Planning <1.5s / longer | Quiet, then one transient `role=status` line with task-specific copy | Cancel appears only when planning becomes cancellable; never persist dots as history |
| Draft applied | Receipt-backed assistant result + compact change artifact | Inspect; head-only Undo; request render |
| Draft operation partial | Name succeeded and failed groups separately | Undo successful group, retry failed group, or continue |
| Approval pending | Exact-state action card with editorial delta, cost/consequence, expiry | Approve / Keep editing; approving locks to current pins |
| Approval expired/stale/denied | Historical disabled card with reason | Replan from current state |
| Render queued/running | Same generation card updates phase and true elapsed time | Cancel render; composer remains active |
| Successor intent queued | One chip immediately above composer: “After this render: …” | Replace / Cancel; no second queued item |
| Reconnecting/offline | Composer text persists; quiet visible connectivity line | Retry automatically only where idempotent; manual resend otherwise |
| Cancel requested/race | Lifecycle card says cancelling until terminal truth is known | No optimistic “cancelled” claim |
| Partial render | Play successful variant; name failed variant | Use playable cut or retry exact failed variant |
| Terminal failure | Preserve project/draft; say what failed and what did not change | One valid retry, adjust, or manual handoff |
| Ready but review unavailable | Playable result without invented judgment | Review manually / continue editing |
| Cross-device/CAS conflict | Refresh projection and explain which draft is current | Reapply/replan; never merge silently |
| Old generation callback | Update only its historical card | Never auto-scroll or steal focus |
| Legacy/read-only thread | Existing projected history and playable result | Open supported editor or start a new unified thread |

### Pass 3 — User Journey and Emotional Arc: 7/10 → 10/10

- **First 5 seconds — skepticism to orientation:** the composer is the visual anchor and the opening prompt makes a concrete promise: add footage and describe the outcome; no mode maze or bot introduction.
- **First useful turn — orientation to relief:** Kria responds with an editorial judgment, applies a reversible change when possible, or asks one decision-changing question. It never says “I understand you want…” or leads with a limitation.
- **First 5 minutes — relief to trust:** the creator sees inspectable draft receipts, exact approval, honest elapsed progress, and a playable first cut without leaving the conversation. The interface clearly separates waiting from working and cost from reversible exploration.
- **Failure — concern to recovery:** the same thread preserves direction and completed work, names the failed layer, and offers a valid next action. A user never has to repeat the brief to a new surface.
- **Long-term — trust to reliance:** refresh, device change, revision, and manual editor inspection all resolve to the same project state. Preference memory is intentionally deferred, so Kria must not imply it “knows your style” across projects in v1.

### Pass 4 — AI Slop Risk: 6/10 → 10/10

Classification: task-focused **App UI**. Hard-rejection risk exists if semantic events become a vertical stack of rounded cards. The card-eligibility rule above removes it.

| Litmus check | Result after fixes |
|---|---|
| Product unmistakable in first screen | YES — Kria creation, current project, footage/chat composer |
| One strong visual anchor | YES — composer before a cut; playable video after |
| Understandable by scanning | YES — outcome, proof, next action |
| Each section has one job | YES |
| Cards are necessary | YES — only artifacts/actions/receipts/recovery |
| Motion improves hierarchy | YES — transient state change only; no decorative travel |
| Premium without shadows | YES — border, type, spacing, media hierarchy carry the UI |

Forbidden copy: paraphrase acknowledgements; “Great!” filler; “As an AI”; raw capability/error codes; invented completion; generic “Something went wrong”; and policy lectures before a rescue. Assistant turns lead with `decision`, `result`, `question`, `progress`, `review`, or `recovery`, use at most one short evidence sentence when helpful, and end with at most one recommended next move. Buttons use concrete verbs: Apply, Undo, Render this cut, Cancel render, Replan, Open editor.

### Pass 5 — Design-System Alignment: 8/10 → 10/10

- Keep pure-white Paper canvas, ink/foreground, quiet zinc borders/text, lime selection/success, Fraunces only for project-level headings, and Inter for transcript and utility copy. Do not introduce gradients, avatars, floating glass, bespoke status colors, or a second card language.
- Extend `ChatArtifactCard` by semantic variants or compose existing `Card` primitives; do not create a parallel visual system. Use `Badge` for terse state only, never as the main explanation.
- Reuse `ProgressTheater`/true elapsed-stage semantics for render detail, `sonner` only for transient confirmation, and visible inline content for warnings/errors/disabled reasons.
- Motion is limited to the existing accordion/reveal tokens and state cross-fades; reduced motion removes travel and looping decoration while preserving state transitions.

### Pass 6 — Responsive and Accessibility: 7/10 → 10/10

- Desktop keeps the established widths. At widths below `lg`, Chat and Editor are mutually exclusive tabs; opening a result moves to Editor only after an explicit user action, never automatically on render completion.
- The composer stays safe-area pinned and supports Enter to send / Shift+Enter for newline; its draft survives offline, tab changes, project-sheet use, and recoverable errors. Attach/send/cancel/undo controls have 44px targets.
- The transcript remains `role=log`, but only new semantic turns use polite announcements. Mutable progress uses one `role=status`; phase changes announce once. Updating an old generation is not announced unless it changes the active outcome.
- Auto-scroll only when the creator is within 80px of the live edge or just sent a message. Otherwise show a “New update” control; never move focus. After approve/undo/cancel, focus returns to the invoking control's logical successor; expired approval focus moves to Replan.
- Every card has one heading, controls follow reading order, disabled state has visible text, media has captions/transcript access where available, and focus remains visible at 200% zoom. Color never carries status alone.

### Pass 7 — Resolved Design Decisions

| Decision | Resolution |
|---|---|
| Transcript versus activity feed | One transcript/outcome ledger; internal activity is projected into semantic artifacts |
| Completion language timing | Only `ObservedTurnResponse` may claim work completed |
| Lifecycle card identity | One mutable card per exact generation; historical generations remain inert |
| Safe-edit proof | Compact change artifact, maximum three highlights plus inspect/Undo |
| Render-time input | Composer remains active; one replaceable/cancellable successor intent |
| Scroll policy | Follow only at live edge or after own send; otherwise show New update |
| Automatic editor opening | Never; creator explicitly opens/switches to Editor |
| Error density | One failure explanation and one recommended recovery; details stay in admin trace |

No design decision remains unresolved. The two product-policy challenges from the CEO phase—cohort breadth and whether the originating request can authorize the first render—remain reserved for the final autoplan gate.

### What Already Exists

The light Paper workspace, project rail, full-width-to-split transition, mobile sheet/tabs, embedded editor, chat primitives, upload/voiceover controls, generation progress/result cards, shadcn component vocabulary, accessibility guard tests, and durable event projection already exist. Implementation refactors their semantics and state identity; it does not redesign the shell.

### NOT in Scope

- A new visual brand, theme, or navigation system; the existing light editorial system is the constraint.
- Decorative agent avatars, personality animation, token-stream theatre, or internal chain-of-thought display; agency is demonstrated by useful outcomes.
- A separate activity/debug drawer for creators; detailed receipts belong in the existing admin trace.
- Automatic mobile editor takeover on completion; explicit navigation preserves control.
- Cross-project preference UI; preference memory is deferred until outcome and privacy gates pass.

### Design Implementation Tasks

- [x] **DES-T1 (P1, human: ~1d / CC: ~1h)** — Event projection — Replace bubble-per-event rendering with semantic rows and stable generation artifact identities.
  - Surfaced by: Information Architecture and AI Slop; files: creation-thread projection and workspace chat; verify: refresh/replay produces one card per lifecycle generation and no internal-planning bubbles.
- [x] **DES-T2 (P1, human: ~4h / CC: ~30m)** — Conversation copy — Remove state-intent echo and enforce prospective-versus-observed copy grammar.
  - Surfaced by: Hierarchy and honesty; files: turn schemas/prompts and confirmation UI; verify: no assistant output/card description mirrors the latest brief or claims an unreceipted action.
- [x] **DES-T3 (P1, human: ~1d / CC: ~1h)** — State components — Implement draft receipt, pinned approval, mutable lifecycle, queued-intent, partial, and recovery treatments.
  - Surfaced by: State coverage; files: shared chat artifacts and workspace; verify: component tests cover every state row and valid controls.
- [x] **DES-T4 (P1, human: ~4h / CC: ~30m)** — Transcript behavior — Add live-edge-aware scrolling, New update affordance, coalesced announcements, and deterministic focus restoration.
  - Surfaced by: Responsive/a11y; files: workspace transcript shell and tests; verify: keyboard/screen-reader tests plus manual 200% zoom and reduced-motion pass.
- [x] **DES-T5 (P2, human: ~4h / CC: ~30m)** — Responsive continuity — Persist composer/queued intent across mobile tabs, project sheet, offline recovery, and editor opening.
  - Surfaced by: Mobile journey; files: workspace state hooks and mobile shell tests; verify: 375x667, tablet, and desktop scenarios preserve input and never auto-switch panes.
- [x] **DES-T6 (P2, human: ~4h / CC: ~30m)** — Design contract — Add the semantic transcript, card-eligibility, copy, and scroll/focus rules to `DESIGN.md` with guard references.
  - Surfaced by: Design-system alignment; files: `DESIGN.md`; verify: design review after implementation finds no parallel primitive or token drift.

### Design Completion Summary

| Review item | Result |
|---|---|
| System audit | UI scope confirmed; `DESIGN.md` authoritative; current intent echo verified |
| Step 0 | 7/10; hierarchy, state identity, scroll/focus, and copy grammar were missing |
| Pass 1 — Information architecture | 7/10 → 10/10 |
| Pass 2 — Interaction states | 6/10 → 10/10 |
| Pass 3 — Journey | 7/10 → 10/10 |
| Pass 4 — AI slop | 6/10 → 10/10 |
| Pass 5 — Design system | 8/10 → 10/10 |
| Pass 6 — Responsive/a11y | 7/10 → 10/10 |
| Pass 7 — Decisions | 8 resolved, 0 design decisions deferred |
| Mockups | 0 generated; design binary unavailable, text wireframe used |
| Outside voice | Claude subagent requested; Codex unavailable |
| Overall | 7/10 → 10/10; design-complete for implementation |

## Phase 3 — Engineering Review

### Step 0: Scope and System Diagnosis

Nova is simultaneously repaying control-plane debt and innovating. The current system already has strong local invariants, but the relevant files are large (`creation_threads.py` 3,160 lines, `creator_agent.py` 3,633, `plan_items.py` 8,747, `edit_copilot.py` 4,024), and the chat bridges two controllers after separate commits. The full platform remains the approved scope, gated behind Milestone 0 and the architecture spike. Engineering scope is reduced only by rejecting a generic workflow engine/outbox and reusing the Job dispatcher.

The minimum complete architecture adds three durable concepts—turn, draft revision, approval—plus receipt extensions and immutable thread runtime ownership. It extracts existing editor-commit logic and adds one lightweight queue. It does not add a new renderer, media store, Job state machine, arbitrary DAG engine, streaming transport, or second transcript.

### Architecture Review — 8 issues found, all folded

1. **[P1] (confidence 10/10) Dual transcript authority.** `creation_threads.py::_sync_agent` scans `CreatorAgentEvent`, remembers only 100 copied IDs, and mirrors after the creator controller's separate commit. Runtime v2 now writes semantic `CreationThreadEvent` rows directly; legacy mirroring uses a nullable source event ID with a database uniqueness guard.
2. **[P1] (confidence 9/10) Render dispatch authority.** `job_dispatch.py` already requires a committed Job and uses `task_id = job.id`; a second outbox would create two retry owners. The plan selects Job + `enqueue_orchestrator` and adds only a targeted accepted/pending-dispatch sweep.
3. **[P1] (confidence 9/10) Approval/edit atomicity.** The current editor route validates and persists Job/PlanItem state in one transaction. The shared service must consume approval, persist the exact generation, and mark execution accepted in that same transaction, then dispatch after commit.
4. **[P1] (confidence 9/10) Receipt semantics.** The legacy receipt marks `succeeded` before render completion. V2 has accepted/dispatched/completed phases and exact target evidence; completion copy requires `completed`.
5. **[P1] (confidence 9/10) Global lock order.** Existing code documents deadlock-sensitive `Plan → Persona → PlanItem → Job → Session` ordering. The expanded invariant is fixed in Runtime step 3, with a special rule that thread-first projection transactions acquire no upstream row.
6. **[P1] (confidence 9/10) Durable long model work.** `MainCreatorAgent` has a 35-second call timeout and currently runs via `asyncio.to_thread` from the request. V2 persists then enqueues a turn, returning 202; no database lock or HTTP lifetime spans inference.
7. **[P2] (confidence 10/10) Append-only UI mutation.** Migration 0092 forbids event updates. Stable artifact keys and projection-time coalescing produce one mutable-looking lifecycle card without mutating history.
8. **[P2] (confidence 9/10) Full-history polling.** Current projection repeatedly returns bounded history and scans agent events. V2 adds sequence cursors, compact active state, and a separate draft fetch.

### Detailed Persistence and Transaction Contract

`CreatorAgentTurn` fields: `id`, `thread_id`, `session_id`, `source_event_id`, `client_event_id`, `status`, `plan_json`, `observed_event_id`, `queued_replaces_turn_id`, `cancel_requested_at`, `lease_owner`, `lease_epoch`, `lease_expires_at`, timestamps, and typed error. Unique `(thread_id, client_event_id)` provides replay; partial unique indexes allow one status in `planning|executing|observing` and one in `queued` per thread. Database time owns leases.

`CreatorEditDraft` fields: `id`, creator/thread/item IDs, normalized `variant_key`, `base_job_id`, `base_generation_id`, `draft_revision`, `parent_draft_id`, `snapshot_json`, `snapshot_hash`, `source_execution_id`, `is_head`, and timestamps. Unique `(item_id, variant_key, draft_revision)` and partial unique `(item_id, variant_key) WHERE is_head`; every write holds the PlanItem lock, clears the previous head, and inserts a new head. Snapshot JSON is schema-versioned and capped at 2 MiB. Keep every head and render-approved snapshot; after 30 days, prune bodies of superseded unapproved revisions while retaining IDs, hashes, parent links, and receipts.

`CreatorAgentApproval` fields: creator/thread/session/turn IDs, exact draft/job/variant/generation/manifest/ownership pins, grouped execution IDs, consequence/cost summary, status, expiry, consumed timestamp, and timestamps. Approval risk and pins come only from deterministic policy. State change expires it before execution.

`CreatorAgentExecution` adds `turn_id`, `tool_name/version`, `risk`, dependency group/order, all target pins, lifecycle timestamps, external Job/task identity, and observed event link. Tool plans are a bounded ordered list of dependency groups, not a general graph engine: maximum eight intents; dependencies may point only backward; cycles/unknown references reject the whole plan before execution.

```text
POST TURN                         AGENT-CONTROL TASK
lock Thread only                  claim Turn lease (short tx)
append user event                 load trusted snapshot
create/replay Turn                release all locks
commit                            evidence/model call
publish task                      reacquire global-order locks
return 202                        validate -> draft | approval | response

APPROVAL                          RENDER CONTINUATION
global-order locks                terminal Job write
verify pins                       enqueue resume(turn_id), idempotent
stage editor state + Job          GET/Beat reconciler is backstop
consume approval                  evidence + observer
execution=accepted                append observed semantic event
commit -> enqueue_orchestrator    claim queued successor from fresh state
```

Migration is expand/lazy-bootstrap/contract. First deploy nullable columns, tables, partial indexes, queue consumer, and dual-readable API with v2 creation disabled. Historical threads stay runtime v1 and receive no synthesized draft. A v2 draft bootstraps only from an exact current Job variant/generation; absent or ambiguous source returns a recovery action. Constraint tightening follows shadow validation. Legacy columns/routes are removed only after rollback support expires.

### Code Quality Review — 4 issues found, all folded

- Extract orchestration into focused `kria_turns`, `kria_policy`, `kria_drafts`, and shared `editor_commit` services. Route modules own auth, request validation, locking entry, and serialization only; do not add the new loop to the existing 3k–8k-line route files.
- Port only audited portable reducers into server services by extending `creator_craft` and shared request schemas. The browser consumes authoritative post-state; it does not independently compute a claimed v2 result. Existing TypeScript reducers remain legacy compatibility.
- Add a versioned v2 planner and observer alongside the legacy Main Creator types/prompt. Runtime owner selects once per thread; no flag read mid-thread changes schemas or authority.
- Keep error classification, transition tables, risk policy, and tool metadata declarative and exhaustively enum-tested. Never catch-and-success; unknown side-effect outcomes become `outcome_unknown` and reconcile.

Inline ASCII comments are required in the new turn model (state transitions and partial-index invariants), turn service (lock/release/reacquire flow), shared editor commit service (approval/commit/dispatch boundary), and concurrency tests (crash points). Update the existing lock-order comments in creator/session/thread routes when the shared service lands.

### Test Review

```text
CODE PATHS                                             USER FLOWS
[★★★ EXISTING] thread event idempotency/append-only    [GAP →E2E] message -> draft -> inspect/Undo
[★★★ EXISTING] creator session/receipt constraints     [GAP →E2E] approval -> one Job -> playable review
[★★★ EXISTING] editor commit atomicity/generation CAS  [GAP →E2E] render-time question + queued successor
[★★★ EXISTING] Job deterministic dispatch/recovery     [GAP →E2E] refresh/device conflict -> replan
[GAP] turn state machine + partial unique indexes      [GAP →E2E] cancel before/after dispatch race
[GAP] lease heartbeat/takeover/fencing                 [GAP →E2E] partial render -> use/retry
[GAP] draft CAS/head-only Undo/pruning                  [GAP →E2E] runtime-v1 fallback/rollback
[GAP] approval expiry/consume atomicity                 [GAP →EVAL] no-parrot + tool/target/recovery quality
[GAP] accepted/dispatched/completed receipts            [GAP →EVAL] media prompt injection cannot select policy
[GAP] pending-dispatch/resume reconcilers               [GAP →QA] mobile/keyboard/live-edge/focus behavior
[GAP] cursor projection/coalescing                      [GAP →QA] all format matrices on real media
[GAP] v2 planner/observer fallback

PLANNED COVERAGE: 23/23 path families. New implementation starts at 4/23 existing
foundations and must add 19 named families before rollout; 0 gaps may be waived.
```

Required backend tests cover schema/migration/downgrade guards; runtime owner immutability; same-key replay/body mismatch; one-active/one-queued constraints; state transition matrix; DB-time lease renewal/loss/takeover; no lock across model/storage/broker; exact lock-order concurrency; draft snapshot validation/size/CAS/Undo/prune; approval stale/expiry/deny/double-tap; multi-tool prevalidation and partial dependency groups; observer fallback; source-event dedupe beyond 100 legacy events; cursor gaps/pagination/coalescing; cross-tenant IDOR; prompt injection; and all failure rows below.

Crash-boundary integration tests kill/restart after user-event commit, after turn publish, during model inference, after draft receipt, after approval consume/edit commit, before render publish, after broker acceptance, after worker terminal write, and before observed event. In every case the invariant is one semantic user event, at most one draft head per revision, one execution identity, one Job generation per approval, and one observed completion.

Prompt/tool schema changes require a new v2 `AgentSpec.prompt_version`, structural replay evals in `tests/evals`, ≥30 scenarios per canonical token, injected failures, and live judged runs on the consented Nermin fixtures before cohort. Compare v2 against current Nova and the frozen independent request labels; never tune labels after seeing model output.

### Failure Modes Registry

| Codepath | Production failure | Test | Handling | User-visible |
|---|---|---:|---|---|
| turn accept | duplicate or reused key/body mismatch | Yes | replay / 409 | Existing result / conflict |
| task publish | API dies before/after broker call | Yes | unpublished-turn sweep / idempotent task | Still working, then result |
| lease | heartbeat lost during inference | Yes | epoch fence + reconcile before takeover | Progress, no duplicate claim |
| planner | timeout/schema/refusal | Yes | one retry then deterministic recovery | Specific retry/action |
| evidence | stale/missing/provider fail | Yes | omit unsupported claim or request source | Missing evidence + next action |
| draft | invalid, >2 MiB, CAS conflict | Yes | atomic abort / refresh-replan | Nothing changed + fix |
| approval | stale/expired/double tap | Yes | no-op/409 projection | Disabled + Replan |
| commit | validator/storage lookup fails | Yes | transaction rollback | Nothing rendered + correction |
| dispatch | accepted row but no task / ambiguous ack | Yes | deterministic task ID + sweep/CAS | Checking/queued, never duplicate |
| render | partial/terminal/worker death | Yes | existing reaper + exact retry | Playable part or recovery |
| callback | duplicate/out-of-order generation | Yes | exact fence/no-op | Historical card only |
| observer | model fails after successful tool | Yes | deterministic receipt formatter | Honest result still appears |
| queue | agent-control starves maintenance | Yes | SLO alert; split process gate | Delayed response, no lost state |
| cursor | gap/duplicate/out-of-order merge | Yes | sequence dedupe + full refresh | Stable transcript |
| legacy bridge | >100 mirrored source events | Yes | DB unique source ID | No repeated bubbles |
| rollback | web/API flag or version skew | Yes | immutable runtime owner | Upgrade/fallback action |

Every row has a planned test, handler, and explicit user state; **0 critical silent gaps** remain in the plan.

### Performance Review — 4 issues found, all folded

- **Polling/history:** cursor deltas avoid repeated transcript scans; bootstrap is capped and older history paginates. Active Job/session/turn/approval metadata loads in bounded batch queries.
- **Draft size:** full snapshots avoid complex patch replay but are capped at 2 MiB, fetched separately with ETag, excluded from ordinary thread polls/model context, and pruned as specified.
- **Model context:** include a deterministic structured project summary, the last 24 semantic user/assistant turns, current draft digest, and referenced evidence only. Never send raw event/activity history or full editor JSON.
- **Queue isolation:** route v2 turns to `agent-control` on the light worker with 90s/120s limits. Measure queue wait, planning, tool, observation, and total useful-response p50/p95/p99. Split process group on the fixed two-second queue-wait or maintenance-SLO gate; render capacity remains separately measured.

Required indexes: turn `(thread_id, created_at/id)`, active/queued partials and lease expiry; event `(thread_id, sequence)` cursor; draft head/item+variant+revision; approval pending expiry/thread; execution `(turn_id, group_order)`, idempotency, pending-dispatch age; and Job generation/status indexes already used by render reconciliation. Query-count tests pin a thread delta refresh to a constant number of queries independent of event count.

### Deployment, Rollback, and Operational Ownership

API/model migration and worker queue support deploy first with v2 off. Next deploy the web dual reader. Enable shadow for internal accounts, then durable execution internally, then the user-approved cohort gate. Each step verifies old v1 creation, v2 turn replay, queue consumption, pending-dispatch sweep, one real format smoke, and rollback projection. Rollback disables new v2 thread creation and new turn claims, lets accepted Jobs settle, cancels queued turns/approvals, and retains read access to committed drafts/results.

Day-one alerts: active turn with expired lease; accepted execution without task/Job movement; `outcome_unknown` age; duplicate generation per approval; observer lag; agent-control queue p95; maintenance lateness; cursor projection errors; and per-format terminal recovery/creator escape. The existing admin job trace receives turn, plan version, policy decision, approval, receipt phases, Job task ID, evidence, observation, and successor links.

### Worktree Parallelization

| Step | Modules | Depends on |
|---|---|---|
| A. Persistence + runtime protocol | API models/migrations/services | Milestone 0 + frozen contracts |
| B. Editor commit extraction + portable reducers | API generative/editor services | Milestone 0 portability audit |
| C. Event projection + cursor API | API creation-thread services; web API client | Frozen event contracts |
| D. Planner/observer + evals | API agents/prompts/evals | Frozen schemas + evidence contract |
| E. Semantic UI | Web workspace/chat components/tests | C plus draft/approval response schemas |
| F. Dispatch/resume/ops trace | API tasks/maintenance/admin | A + B |
| G. End-to-end/chaos/format QA | API/web/evals | A–F merged |

After Milestone 0, land the non-rendering golden vertical slice in one worktree and freeze the exercised registry/event/error contracts. Only then launch A, B, C, and D in separate worktrees. B and C are independent; A and D coordinate only through the proven schemas. Merge A+B before F; merge C before E; then merge all and run G. A/F both touch runtime services and B/F touch editor dispatch, so keep F later rather than resolving avoidable conflicts.

### Engineering Implementation Tasks

- [x] **ENG-T1 (P1, human: ~2d / CC: ~3h)** — Persistence — Add runtime ownership, turn/draft/approval schemas, receipt lifecycle, indexes, lazy bootstrap, and migration guards.
- [x] **ENG-T2 (P1, human: ~2d / CC: ~3h)** — Runtime — Implement v2 turn acceptance, agent-control task, leases, policy, ordered tools, cancellation, and deterministic observer fallback.
- [x] **ENG-T3 (P1, human: ~2d / CC: ~3h)** — Editor authority — Extract shared editor commit service and server-side portable draft reducers; atomically consume approval and stage one generation.
- [x] **ENG-T4 (P1, human: ~1d / CC: ~1h)** — Dispatch — Reuse Job dispatcher; add accepted/pending-dispatch and terminal-resume reconciliation plus crash tests.
- [x] **ENG-T5 (P1, human: ~1d / CC: ~1h)** — Events/API — Write v2 events directly, dedupe the legacy bridge, implement cursor reads/draft ETag, and preserve v1 clients.
- [ ] **ENG-T6 (P1, human: ~2d / CC: ~3h)** — Agent/evals — Add versioned planner/observer prompts, turn-value/tool/evidence validation, replay/injection/failure cases, and live Nermin evaluation.
- [x] **ENG-T7 (P1, human: ~2d / CC: ~3h)** — Verification — Implement unit, integration, concurrency, crash-boundary, cross-tenant, and format matrix suites plus the QA artifact.
- [x] **ENG-T8 (P2, human: ~1d / CC: ~1h)** — Operations — Extend admin traces, reconciler actions, metrics, alerts, and runbook.
- [x] **ENG-T9 (P2, human: ~4h / CC: ~30m)** — Performance — Add bounded context, snapshot pruning, query-count tests, queue/latency telemetry, and split-process gate.

### Engineering Completion Summary

| Review item | Result |
|---|---|
| Step 0 | Full platform retained behind evidence/portability gates; generic engine/outbox removed |
| Architecture | 8 issues found, 8 folded |
| Code quality | 4 issues found, 4 folded |
| Test review | Diagram produced; 19 new path families specified |
| Performance | 4 issues found, 4 folded |
| Failure modes | 16 mapped; 0 critical silent gaps |
| Existing leverage | Thread/session/event models, creator craft, editor commit, Job dispatcher/reaper, renderers, traces |
| NOT in scope | Existing exclusions retained; generic outbox, DAG engine, and streaming transport explicitly rejected |
| TODO updates | 0 new deferred items; preference memory already recorded |
| Outside voice | Claude subagent ran; 8 findings agreed or folded; Codex unavailable |
| Parallelization | 4 initial lanes, 2 dependent implementation lanes, 1 final verification lane |
| Lake score | 16/16 findings took complete option |
| Status | DONE — engineering plan complete, subject to final product-policy gate |

## Phase 3.5 — Developer Experience Review

### Developer Persona and Product Type

```text
TARGET DEVELOPER PERSONA
Who:       Nova product engineer extending or operating the internal Kria runtime
Context:   Adds one tool/recovery, diagnoses a creator failure, or changes a v1/v2 contract
Tolerance: 15 minutes to prove the system locally; 10 minutes to find a known failure
Expects:   one golden path, deterministic fixtures, typed contracts, exact trace IDs, safe defaults
```

Primary type: **internal API/service and agent platform**. This is not a public SDK or generic plugin ecosystem. The DX mode is **POLISH**: make the approved platform safe and fast to change without expanding it into a framework.

### Developer Perspective

> I open the README and see `docker-compose up`, while `CLAUDE.md` tells me `dev-auto.sh` is the preferred loop. The current chat behavior spans creation threads, creator sessions, the editor commit route, Copilot reducers, Job dispatch, and several runbooks. I can run per-agent replay tests and a deterministic UI fixture, but I cannot reproduce one whole Kria turn without live state and provider knowledge. If a creator gives me only a thread ID, the admin view is Job-first, so I search large route files or query the database to connect the turn, approval, execution, and render. The plan gives me a sound state machine, but unless it also gives me a golden replay, one tool registry, generated contracts, and a single trace path, my first change will take hours and parallel teams will learn different versions of the architecture. I need one command that proves the full non-rendering loop, shows what happened, and tells me exactly where to add the next safe tool.

### Benchmark, Magical Moment, and Time to Hello World

| Reference | TTHW | Useful pattern | Kria application |
|---|---:|---|---|
| Stripe-style API DX | <2 min | one complete example, idempotency, structured errors | one replay command, stable IDs, `KriaProblem` |
| Vercel-style platform DX | ~2 min | one golden path and visible result | prepared-worktree replay with readable trace |
| Nova agent evals | current, agent-level | credential-free structural replay | reuse fixture/eval adapters |
| Nova chat dev fixture | current, UI-level | deterministic state URLs | extend with runtime-v2 lifecycle states |
| Kria platform today | ~2–4 h whole-turn discovery | no complete turn replay | target ≤15 min from prepared worktree |

The magical moment is a developer running `make kria-replay FIXTURE=nermin-matcha-update` without provider/storage credentials or a render and seeing the same user intent become a registry-selected read tool, validated receipt, and observed semantic event in both readable and JSON traces. From a prepared worktree the command must finish in under 60 seconds; setup plus first success must be ≤15 minutes. A read-only tool must be addable and tested in ≤30 minutes, a reversible draft tool in ≤2 hours, and a thread/turn production failure diagnosable to a safe action in ≤10 minutes.

### Golden Developer Journey

| Stage | Developer does | Planned resolution | Acceptance |
|---|---|---|---|
| Discover | Starts from README/CLAUDE | Link one Kria runtime guide and one command; remove competing quick-start implication | Correct entrypoint found in <2 min |
| Install | Runs worktree setup | Reuse `scripts/worktree-setup.sh`; replay checks prerequisites and prints exact fix | No provider/storage keys required |
| Hello world | Runs the Nermin matcha fixture | Whole-turn fake model/evidence/broker/render replay in rollback-scoped local state | Readable + JSON trace in <60s |
| Real usage | Adds a typed tool | One registry owns schema/version/risk/retry/executor and generates manifest/reference/types | Implementation + registration + fixture only |
| Debug | Starts with thread or turn ID | Admin and CLI trace lookup follows thread → turn → approval/execution → Job/generation/task | Root cause and recovery in ≤10 min |
| Upgrade | Changes a schema/tool version | Snapshot/drift checks, v1/v2 fixtures, expand/lazy-bootstrap/contract guide | Old client behavior remains pinned |
| Test | Runs one focused gate | `make verify-kria` runs contracts, registry, state machine, replay, API/type drift | Deterministic, timed, no render |
| Deploy | Follows staged flags/queues | One runbook owns deploy order, reconciliation, rollback, and prompt/live-eval rule | Internal smoke before cohort |
| Improve | Reviews CI/incident evidence | Track replay time/flakes, tool-add time, diagnosis time, schema/docs drift | Quarterly DX exercise |

First-time roleplay after these changes: T+0 finds the runtime guide; T+2 runs the fixture; T+3 sees the exact phase/receipt/event trace; T+6 changes a read-tool fixture; T+10 runs the focused gate; T+15 knows the production trace and rollback path. The previous confusion points—two setup stories, no whole-turn runner, Job-first lookup, scattered errors, and hand-mirrored types—are all assigned launch-blocking fixes.

### Eight-Pass DX Review

| Dimension | Before | Planned | What closes the gap |
|---|---:|---:|---|
| Getting Started | 4/10 | 9/10 | one credential-free replay and canonical guide |
| API/tool design | 6/10 | 9/10 | one registry, stable envelopes, progressive read→draft→approval path |
| Errors/debugging | 4/10 | 9/10 | `KriaProblem`, full correlation chain, lookup and dry-run reconcile |
| Documentation | 5/10 | 9/10 | runtime guide, operator runbook, generated tool reference, real fixtures |
| Upgrade path | 7/10 | 10/10 | immutable runtime owner, schema snapshots, v1/v2 compatibility tests |
| Developer environment | 6/10 | 9/10 | existing worktree/dev stack plus fake adapters and focused gate |
| Community/ecosystem | 7/10 | 8/10 | explicit internal ownership and extension recipe; public ecosystem excluded |
| Measurement | 3/10 | 9/10 | timed CI gate and quarterly add-tool/diagnose exercises |

Overall: **5/10 → 9/10**. Target tier: competitive internal-platform DX. The remaining point is deliberate: a credential-free clone-to-success under five minutes is not realistic for Nova's private monorepo and is not required for the prepared-worktree persona.

### Developer Contracts and Tooling

- Create focused packages for turn runtime, tool registry/executors, drafts/approvals, semantic projection, and reconciliation. HTTP routes/tasks stay thin; package docs and tests own the lock order, transaction boundaries, and state machines.
- Make one server registry the source for `KriaToolDefinition`, model manifest, executor binding, generated reference, and schema snapshot. Adding a tool must not require prompt, API, and UI switch edits.
- Generate or centrally derive runtime-v2 TypeScript types from the checked server contract and fail CI on OpenAPI/schema drift. Keep explicit v1/v2 compatibility fixtures.
- Standardize all runtime-v2 failures as `KriaProblem`. The UI uses its recovery class/action; operators use its trace ID. No new endpoint returns a bare string or asks engineers to infer whether work committed.
- Extend the current admin trace and `scripts/admin.py` path with lookup by thread or turn, redacted export/import, and dry-run reconcile. Add local reset only for one explicit dev thread/turn; never add a broad reset command.
- Add `make verify-kria` for schema/registry/state-machine/replay/API-type checks. Full cross-format render QA remains a separate, explicit expensive gate.
- Write one `docs/pipelines/kria-agent-runtime.md` mental model and one `docs/runbooks/kria-agent-runtime.md` operator path covering setup, replay, tool/recovery extension, migrations, flags, queues, prompt-version/live-eval rules, tracing, reconciliation, and rollback.

### Error Trace Standard

Three current patterns are replaced, not carried into v2:

| Current developer signal | Runtime-v2 signal | Recovery |
|---|---|---|
| `409: Creation thread changed` | `stale_thread_revision`, current/expected revision, turn trace | refresh and replan |
| `503: Render queue unavailable` | `render_dispatch_unavailable`, accepted/dispatched state, Job/task identity, retryability | dry-run reconcile or retry exact dispatch |
| bare capability/format limit detail | `capability_unavailable`, capability reason and supported alternative IDs | ask the one material choice or open supported path |

Every trace correlates `thread_id → turn_id → approval_id/execution_id → job_id/variant_id/generation_id/task_id`. Errors lead with problem, known cause, unchanged/committed state, and exact next action; framework stacks and raw internal codes remain behind operator detail.

### What Already Exists

- `scripts/worktree-setup.sh`, `dev-auto.sh`, dev-login, scoped admin CLI, hot reload, and worktree isolation.
- Credential-free structural agent evals, checked fixtures, deterministic chat UI states, and strong thread/session/Job/editor tests.
- Existing Job dispatch/reaper/maintenance authority and admin pipeline traces.
- Creator Agent architecture/rollout docs, chat-first runbook, and prompt-version/live-eval policy.

### Not in DX Scope

- Public SDK, external plugin marketplace, hosted playground, free tier, community program, or multi-language clients: this is a private product runtime.
- A new local dev service, generic workflow debugger, or generic code generator: reuse current scripts, admin surfaces, schemas, and fixtures.
- Making full media renders part of the sub-minute hello world: fake adapters prove control flow; real format QA stays mandatory before rollout.

### DX Implementation Tasks

- [x] **DX-T1 (P1, human: ~1d / CC: ~2h)** — Replay — Add credential-free whole-turn replay with readable/JSON traces and happy, stale, approval, and ambiguous-dispatch fixtures.
- [x] **DX-T2 (P1, human: ~1d / CC: ~2h)** — Tool registry — Establish the single typed registry and generate manifest/reference/schema snapshots from it.
- [x] **DX-T3 (P1, human: ~1d / CC: ~2h)** — Errors/tracing — Add `KriaProblem`, full correlation, thread/turn lookup, redacted export, and dry-run reconcile.
- [x] **DX-T4 (P1, human: ~4h / CC: ~45m)** — Module ownership — Freeze thin route/task boundaries and package-level state/transaction documentation before parallel work.
- [x] **DX-T5 (P1, human: ~1d / CC: ~2h)** — Contract parity — Derive v2 web types, add OpenAPI/schema drift checks, and pin v1/v2 fixtures.
- [x] **DX-T6 (P2, human: ~1d / CC: ~2h)** — Docs — Write the runtime guide and operator runbook; make README/CLAUDE point to one golden path.
- [x] **DX-T7 (P2, human: ~1d / CC: ~2h)** — Focused gate — Add `make verify-kria`, safe seeded dev state, scoped reset, and deterministic UI lifecycle fixture.
- [x] **DX-T8 (P2, human: ~4h / CC: ~45m)** — Measurement — Record gate duration/flakes and quarterly tool-add and injected-failure diagnosis times.

### DX Completion Summary

| Review item | Result |
|---|---|
| Persona | Nova product engineer implementing/operating Kria |
| Product type | Internal API/service and agent platform |
| Mode | DX POLISH |
| TTHW | ~2–4h discovery → ≤15m first replay; replay itself <60s |
| Magical moment | Whole-turn Nermin fixture with no provider/storage/render dependency |
| Journey | 9 stages mapped; 5 existing friction points receive launch-blocking fixes |
| Passes | 8/8 completed; overall 5/10 → 9/10 |
| Outside voice | Fresh Claude subagent found 5 P1 and 3 P2 issues; all folded pending final approval |
| Strategic correction | Land one non-rendering golden vertical slice before parallel work |
| Final-gate decision | Resolved: approved without DX changes |
| Status | DONE — DX plan complete and approved |

## Final Approval

Approved on 2026-09-06 with the following product-policy decisions locked:

- **Launch cohort:** retain the all-format contract. No creator enters runtime v2 until every canonical format passes its required operation, recovery, concurrency, accessibility, and real-media acceptance matrix.
- **Render consent:** every initial render and rerender requires a separate, exact-state, expiring approval. An explicit request such as “make this” may prepare the approval but never consumes it implicitly.
- **Implementation contract:** the CEO, design, engineering, DX, test, rollout, rollback, measurement, and task artifacts are approved as one plan. Any implementation change to authority, consent, format scope, or success gates requires a plan amendment before code changes.
- **Task rollup:** 30 review tasks are aggregated in `plans/artifacts/023-all-tasks.jsonl`; the detailed validation matrix remains in `plans/artifacts/023-test-plan.md`.

## GSTACK REVIEW REPORT

| Review | Trigger | Why | Runs | Status | Findings |
|---|---|---|---:|---|---|
| CEO Review | `/plan-ceo-review` | Scope and strategy | 1 | CLEAR | 5 proposals; 4 accepted, 1 deferred; both final policy challenges resolved by user |
| Codex Review | `/codex review` | Independent second opinion | 1 attempted | UNAVAILABLE | Installed Codex CLI could not run the configured review model; no Codex findings claimed |
| Eng Review | `/plan-eng-review` | Architecture and tests | 1 | CLEAR | 8 architecture issues and 4 code-quality issues folded; 19 new path families specified |
| Design Review | `/plan-design-review` | UI/UX gaps | 1 | CLEAR | 7/10 → 10/10; 8 interaction decisions locked; text wireframe used because design binary/outside voice were unavailable |
| DX Review | `/plan-devex-review` | Developer experience gaps | 1 | CLEAR | 5/10 → 9/10; TTHW ~2–4h → ≤15m; 8 independent findings folded |

**VERDICT:** CEO + DESIGN + ENG + DX CLEARED — user approved the all-format gate and separate approval for every render; ready to implement from the golden vertical slice.

NO UNRESOLVED DECISIONS
