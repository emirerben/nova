# Speech cleanup render contract fix

**Status:** Implemented locally; production `opt_in` cutover complete; code release and canaries pending
**Target:** `origin/main@e76befe2` (2026-08-25)
**Incident:** A mobile Talking to camera upload completed storage, probe, transcription,
music matching, and planning, but both render attempts ended in `variants_failed`
before reframe/encode.

## Outcome

A creator who leaves **Speech cleanup Off** always receives a normal render with
the original spoken timing. A creator who explicitly turns it **On** keeps the
strict promise: cleanup either produces a safe cut/no-op or returns the existing
structured `speech_cleanup_failed` recovery state. Historical `legacy_auto` jobs
remain readable and rerenderable, but no new creator job can be minted with that
contract.

The 40% maximum-removal safety rail stays unchanged. It correctly rejected a plan
that proposed deleting 58.0% of an 8.849-second clip. The bug was converting that
safety no-op into a render failure for a job whose audit snapshot said
`speech_cleanup_requested=false`.

## Root cause and invariant

The production item was created while the live rollout mode was still
`legacy_auto`. `contract_for_item()` therefore ignored the stored Off preference
and stamped `speech_cleanup_contract="legacy_auto"`. The subtitled renderer then
derived strictness from the live `SILENCE_CUT_ENABLED` flag, so
`max_removal_exceeded` raised before the first output encode. The Talking Head
assembler already treats legacy cleanup as best-effort, so the two speech paths
also disagree.

Prior learning applied: `legacy-auto-silence-cut-max-removal-fails-render`
(confidence 10/10, observed 2026-08-25).

```text
CURRENT FAILURE

PlanItem.speech_cleanup_enabled = false
        │
        ├─ live mode = legacy_auto
        ▼
Job snapshot: requested=false, contract=legacy_auto
        │
        ├─ subtitled renderer sees SILENCE_CUT_ENABLED=true
        ▼
strict_silence_cut=true
        │
        ├─ proposed removal = 5.135s / 8.849s = 58.0%
        ├─ safety rail returns max_removal_exceeded (correct)
        ▼
RuntimeError → variants_failed before reframe/encode (incorrect)
```

The fixed invariant is explicit:

```text
IMMUTABLE JOB CONTRACT              RENDER BEHAVIOR

off_v1       ─────────────────────> skip cleanup analysis; render original timing
required_v1  ─────────────────────> strict cleanup; unsafe/error => typed failure
legacy_auto  ─────────────────────> historical compatibility; attempt best-effort,
                                     any unsafe/error outcome => render uncut

Live dispatch mode:
opt_in | disabled only ───────────> no new legacy_auto snapshots
```

## Step 0: scope challenge

The repo already has the detector, safety rails, immutable contract field, typed
failure, Off short-circuit, creator recovery actions, and debug trace. The minimum
complete fix reuses those pieces. It does not add a service, table, endpoint,
feature flag, or media-processing pass.

- Expected implementation surface: 6 files, 0 new classes/services, 0 migrations.
- No new architectural pattern or infrastructure is introduced, so external
  framework research is not applicable. This is an in-repo contract correction.
- `TODOS.md` contains no dependency that blocks the fix.
- No new distributable artifact is introduced.
- Completeness choice: fix live dispatch, historical compatibility, tests, docs,
  and rollout together. A config-only flip would unstick new jobs but leave the
  same invalid state available for a future rollback.

## Engineering review findings and decisions

### Architecture

1. **[P1] (confidence: 10/10) New unrequested jobs can still become strict legacy jobs.**

   Evidence: `app/config.py:89` accepts and defaults to `legacy_auto`;
   `app/services/speech_cleanup.py:159-160` returns `legacy_auto` without reading
   creator consent; `app/tasks/content_plan_build.py:947-950` can persist
   `requested=false` next to that contract.

   Decision: separate live rollout modes from historical execution contracts.
   Live configuration becomes `opt_in | disabled`, defaulting to `opt_in` after
   the production cutover. `SpeechCleanupContract` continues accepting
   `legacy_auto` so old rows and rerenders remain compatible.

2. **[P1] (confidence: 10/10) Subtitled strictness is derived from a live flag,
   not explicit creator intent.**

   Evidence: `app/tasks/generative_build.py:14504-14511` sets
   `strict_silence_cut` when `SILENCE_CUT_ENABLED` is true even if the immutable
   contract is legacy. `app/tasks/generative_build.py:12769` passes strict mode to
   Talking Head only when `cleanup_required` is true.

   Decision: strictness is exactly `speech_cleanup_contract == "required_v1"` on
   every speech renderer. Live flags may decide whether a historical legacy job
   attempts cleanup, but they may never turn that attempt into a render
   requirement. This deliberately narrows PR #908: its strict behavior remains
   for explicit `required_v1` jobs and no longer applies to unrequested legacy
   jobs.

3. **[P1] (confidence: 9/10) Configuration activation was left as an operator-only
   promise, so rollout drift recreated automatic cleanup.**

   Evidence: `.env.example:488-490` still documents/defaults the hidden
   `legacy_auto` mode even though the opt-in UI and job contracts shipped in PR
   #914.

   Decision: perform a bounded production cutover, then make the invalid live
   state unrepresentable in code. `disabled` is the only rollback mode;
   `legacy_auto` remains a historical job value, not an operational setting.

### Code quality

1. **[P1] (confidence: 10/10) One boolean mixes three separate concepts:** creator
   consent, engine availability, and historical rollout compatibility.

   Decision: keep the existing explicit `cleanup_required` / `cleanup_off`
   variables and simplify `strict_silence_cut` to `cleanup_required`. Do not add
   a new abstraction. The change is small enough that another policy class would
   obscure rather than clarify it.

2. **[P2] (confidence: 10/10) Contract tests are disconnected from renderer tests.**

   Evidence: PR #914 added only `tests/services/test_speech_cleanup.py`; the
   subtitled/talking-head integration harness never passes `required_v1` or
   `off_v1`, so every old test silently exercises the default `legacy_auto`.

   Decision: parameterize the existing render harness with the immutable
   contract and test the full matrix in the existing test modules. No parallel
   test framework or duplicate fixture layer.

3. **[P2] (confidence: 10/10) Nearby documentation now contradicts both code and
   product intent.**

   Evidence: `plans/010-silence-filler-cut.md:410-419` says every analysis,
   safety, and FFmpeg failure renders uncut, while current subtitled code and
   tests require failure for all live-flag-eligible legacy jobs.

   Decision: add a dated supersession note to plan 010 and update renderer/test
   comments in the same change. The old plan remains historical; the note names
   which behavior is superseded and why.

### Tests

The current suite verifies the detector safety rail and legacy Talking Head
fail-open path, but does not verify the contract boundary that failed in
production.

```text
CODE PATHS                                             USER FLOW

Live mode
├── [GAP] reject legacy_auto as live config             Mobile Talking to camera
├── [GAP] opt_in + Off -> off_v1                        ├── [GAP] [→E2E] Off upload
└── [GAP] opt_in + On  -> required_v1                   │   -> render ready, original timing
                                                       ├── [GAP] [→E2E] On + unsafe plan
Subtitled renderer                                     │   -> structured cleanup failure
├── [REGRESSION] legacy + unsafe -> currently fails     └── [GAP] recovery -> Off -> ready
├── [REGRESSION] legacy + analysis error -> fails
├── [REGRESSION] legacy + apply error -> fails
├── [GAP] required + unsafe/error -> typed failure
├── [GAP] required + benign no-op -> ready
└── [GAP] off_v1 + live flags on -> zero cleanup calls

Talking Head renderer
├── [★★★ TESTED] legacy + unsafe/error -> ready uncut
├── [GAP] required + unsafe/error -> typed failure
└── [GAP] off_v1 + live flags on -> zero cleanup calls

Focused contract coverage today: 1/12 correct branches (8%)
Target after this plan: 12/12 deterministic branches + 3 representative E2E flows
No prompt/LLM files change, so no agent eval is required.
```

Required tests:

- **CRITICAL regression:** reproduce the incident geometry without production
  identifiers or transcript text: 8.849s duration, a synthetic 58% proposed
  removal, `speech_cleanup_requested=false`, historical `legacy_auto`; assert
  `ok=true`, no `keep_segments`, a bailout trace, and no
  `silence_cut_required_failed` event.
- Parameterize `_render()` in
  `tests/tasks/test_generative_build_silence_cut.py` with
  `speech_cleanup_contract`.
- Legacy subtitled matrix: unsafe plan, analysis failure, and cut-apply failure
  all render uncut; apply failure performs exactly one uncut retry and persists
  no lying cut summary.
- Required subtitled matrix: unsafe plan, analysis/no-plan failure, and apply
  failure return `error_class="speech_cleanup_failed"` with the closed reason;
  no words, short clip, zero removals, and positive removals remain successful.
- Off matrix: with both legacy live flags true, no cleanup cache, Whisper
  cleanup pass, silence detection, retake detection, or segmented encode runs.
- Talking Head parity: legacy fail-open stays green; required failures are typed;
  off performs zero analysis.
- Dispatch/config integration: after cutover, Off always snapshots `off_v1`, On
  snapshots `required_v1`, `legacy_auto` is rejected as a live setting, and an
  unstamped/new legacy PlanItem job is impossible.
- Finalizer guard: structured required failure reason survives job finalization;
  successful legacy fallback has no cleanup-failure receipt.
- **[→E2E]** Production-image canary with a real mobile Talking to camera clip:
  Off renders ready; On either safely cleans or exposes cleanup recovery;
  `Create without cleanup` produces an `off_v1` ready replacement.

### Performance

No performance issue is introduced. `off_v1` already exits before cleanup cache
creation, Whisper cleanup transcription, silence detection, retake analysis, and
segmented encoding. Making Off the default contract removes work from the common
path. Historical legacy jobs keep the existing cache and one-encode behavior. No
query, N+1, memory, or payload change is planned.

### Outside voice

The standard independent Codex plan review was attempted but blocked before
transmission by the platform security boundary because this document contains
production incident context, rollout instructions, and repository paths. No
external payload was sent and no workaround was attempted. There are therefore no
outside-voice findings or cross-model tensions to incorporate.

## Implementation plan

### Phase 0: contain production and recover the reported item

1. Verify the currently deployed Vercel and Fly revisions contain PR #914's web,
   API, and worker contract support.
2. Set `SPEECH_CLEANUP_MODE=opt_in` for the Fly app and restart both API and worker
   process groups. Do not change `MAX_REMOVAL_FRAC` or disable the engine globally.
3. Create a fresh Talking to camera render with Speech cleanup Off. In admin debug,
   verify `speech_cleanup_requested=false`, `speech_cleanup_contract=off_v1`, no
   cleanup analysis events, and a ready variant.
4. Retry the reported PlanItem through normal Create. This mints a new immutable
   `off_v1` job; do not mutate the two failed historical job snapshots.
5. Inventory failures since PR #914 where `requested=false`, contract is legacy,
   and `silence_cut_required_failed` is present. Do not bulk-requeue them; surface
   the count and recover only still-current items to avoid replacing newer edits.

### Phase 1: close the live-mode hole

1. In `app/services/speech_cleanup.py`, split the types:
   `SpeechCleanupMode = Literal["opt_in", "disabled"]` and
   `SpeechCleanupContract = Literal["legacy_auto", "required_v1", "off_v1"]`.
2. Remove the live `legacy_auto` branch from `contract_for_item()`. Historical
   worker contract parsing remains unchanged.
3. In `app/config.py`, narrow the setting type and default to `opt_in`.
4. Change `.env.example` to `SPEECH_CLEANUP_MODE=opt_in` and document `disabled`
   as the rollback. A stale environment containing `legacy_auto` must fail at
   startup with Pydantic's configuration error instead of silently re-enabling
   automatic cleanup.

### Phase 2: bind strictness to explicit intent

1. In `_render_subtitled_variant()`, set strict behavior only from
   `cleanup_required`. Keep the legacy attempt gate so historical jobs can still
   reuse the detector when live engine flags permit it.
2. For historical legacy jobs, preserve existing analysis/bailout/apply trace
   events, but take the existing uncut fallback paths. Never emit
   `silence_cut_required_failed` and never persist a cut summary for an uncut
   output.
3. Keep `required_v1` behavior byte-for-byte strict: `SpeechCleanupFailure` for
   unsafe analysis/application and truthful benign no-op outcomes.
4. Keep Talking Head behavior unchanged and add parity tests so a future refactor
   cannot reintroduce the subtitled-only divergence.

### Phase 3: tests and documentation

1. Land the complete contract matrix and the synthetic incident regression in
   the existing pytest modules.
2. Update the subtitled renderer docstring/test header to describe contract-based
   strictness.
3. Add a supersession note to `plans/010-silence-filler-cut.md`: fail-open remains
   correct for unrequested/historical cleanup; explicit `required_v1` is strict.
4. Run targeted tests, then the full backend suite and lint/format checks.

## Rollout and rollback

```text
verify #914 deployed
        │
        ▼
set live mode = opt_in ──> restart API + worker ──> Off canary = off_v1/ready
        │
        ▼
merge contract hardening PR ──> deploy ──> required + legacy canaries
        │
        ▼
24h monitor ──> no new legacy snapshots; no Off cleanup failures

Rollback: SPEECH_CLEANUP_MODE=disabled
Never rollback to legacy_auto
```

Canary gates:

- Zero new PlanItem jobs with `speech_cleanup_contract=legacy_auto` after cutover.
- Zero jobs with `speech_cleanup_requested=false` and
  `silence_cut_required_failed`.
- Off canary produces no silence-cut analysis calls/events.
- On unsafe-plan canary produces `speech_cleanup_failed/unsafe_plan` and exposes
  `Create without cleanup`.
- Historical legacy unsafe-plan canary renders ready and records only the bailout.

Use `SPEECH_CLEANUP_MODE=disabled` if the opt-in capability itself must be paused.
Use `SILENCE_CUT_ENABLED=false` only as the engine emergency switch; an in-flight
`required_v1` job will correctly fail `engine_unavailable` rather than publish an
uncleaned result.

## Failure modes

| Codepath | Production failure | Detection | Handling | User outcome | Test |
|---|---|---|---|---|---|
| Live cutover | API remains on `legacy_auto` | startup validation + post-cutover job query | deployment fails loudly; no silent auto mode | service stays on prior healthy revision | config + deploy smoke |
| Off dispatch | live flags accidentally remain true | contract snapshot + negative-call assertions | `off_v1` returns before cleanup analysis | original timing renders | unit + E2E |
| Required analysis | Whisper/detector fails | typed closed reason | variant fails without unclean fallback | Retry or Create without cleanup | unit + E2E |
| Required unsafe plan | removal exceeds safety rail | `unsafe_plan` + bailout trace | no cut is applied | actionable cleanup failure | exact boundary unit |
| Legacy unsafe plan | historical auto plan is unsafe | bailout trace | render uncut | video remains available | regression unit |
| Legacy apply | segmented FFmpeg fails | apply-failed trace | clear cut state and retry once uncut | video remains available | unit |
| Process-group drift | API and worker run different images | deployed revision check | block canary/rollout | no mixed contract activation | deploy checklist |
| Existing failed item | retry reuses immutable failed snapshot | new job contract inspection | normal Create mints new `off_v1` job | reported item renders | production smoke |

Critical silent gaps after the planned coverage: **0**.

## What already exists

- `app/pipeline/silence_cut.py`: detector, 40% safety rail, no-op plans, timing
  remap, and trace payloads. Reused unchanged.
- `app/services/speech_cleanup.py`: capability resolver, live mode, immutable job
  contract, and typed failure. Narrowed, not replaced.
- `app/tasks/content_plan_build.py`: common PlanItem job snapshot boundary. Reused;
  only its live mode input changes.
- `_render_subtitled_variant()`: both strict failure and uncut fallback mechanics
  already exist. The plan corrects which immutable contract selects each path.
- `talking_head_assembler.py`: already implements required strictness and legacy
  fail-open semantics. Used as the parity reference.
- Existing web recovery: `Retry speech cleanup` and `Create without cleanup`.
  No UI change is required.
- Admin job debug/pipeline trace: enough to verify requested intent, contract,
  bailout, and final outcome without adding telemetry infrastructure.

## NOT in scope

- Changing `MAX_REMOVAL_FRAC=0.4`; the safety rail prevented an over-aggressive edit.
- Rewriting Whisper, filler detection, silence detection, retake detection, or
  FFmpeg assembly; none caused the contract failure.
- Making explicit `required_v1` cleanup fail open; that would lie after a creator
  deliberately selected cleanup.
- A frontend redesign or new recovery component; the required actions already ship.
- Database migration/backfill; existing snapshots remain immutable and new jobs use
  the corrected live contract.
- Automatic bulk retry of historical failures; it could overwrite newer creator
  work. Recovery is limited to still-current items.
- Broad cleanup of the opt-in implementation's unrelated test/UI debt; only paths
  necessary to prevent this render failure are included.

## Worktree and sequencing strategy

Sequential implementation, no parallelization opportunity. Config types, renderer
behavior, and their tests share the same backend contract and must land atomically.
The production cutover precedes the hard startup guard; the code PR follows; canary
verification follows deployment. Use one fresh worktree and remove it immediately
after the PR merges.

## Verification commands

```bash
cd src/apps/api
pytest \
  tests/services/test_speech_cleanup.py \
  tests/tasks/test_generative_build_silence_cut.py \
  tests/pipeline/test_talking_head_assembler.py \
  tests/tasks/test_content_plan_build.py -q
ruff check app tests
ruff format --check app tests
pytest
```

Production acceptance uses one real mobile Talking to camera clip under Off, On,
and Create-without-cleanup recovery. No prompt files change, so no live agent eval
is required.

## Implementation Tasks

Synthesized from this review's findings. Each task derives from a verified issue.

- [ ] **T1 (P1, human: ~30m / CC: ~10m)** — Operations — Activate `opt_in`,
  restart API/worker together, verify an Off canary, and recover the reported item.
  - Surfaced by: Architecture finding 1 — live `legacy_auto` ignored explicit Off.
  - Files: no repo edit; Fly configuration and admin job debug.
  - Verify: new job snapshot is `requested=false`, `contract=off_v1`, variant ready.
  - Progress (2026-08-25): `SPEECH_CLEANUP_MODE` was updated, Fly release #986
    completed, no secret changes remain pending, and `/health` returned `{"status":"ok"}`.
    The real Off canary and reported-item recovery remain pending.
- [x] **T2 (P1, human: ~1h / CC: ~15m)** — Contract policy — Remove
  `legacy_auto` from live modes while retaining it as a historical job contract.
  - Surfaced by: Architecture findings 1 and 3.
  - Files: `src/apps/api/app/services/speech_cleanup.py`,
    `src/apps/api/app/config.py`, `.env.example`.
  - Verify: settings reject live legacy; opt-in Off/On resolve off/required.
- [x] **T3 (P1, human: ~1h / CC: ~15m)** — Renderer — Make subtitled strictness
  derive only from `required_v1`; legacy unsafe/error paths render uncut.
  - Surfaced by: Architecture finding 2 and Code Quality finding 1.
  - Files: `src/apps/api/app/tasks/generative_build.py`.
  - Verify: legacy/required/off contract matrix.
- [x] **T4 (P1, human: ~3h / CC: ~35m)** — Regression coverage — Add the exact
  geometry regression, full speech-renderer matrix, dispatch guard, and finalizer pin.
  - Surfaced by: Test review — 11 of 12 focused branches are missing or assert the
    wrong legacy behavior.
  - Files: `src/apps/api/tests/tasks/test_generative_build_silence_cut.py`,
    `src/apps/api/tests/pipeline/test_talking_head_assembler.py`,
    `src/apps/api/tests/services/test_speech_cleanup.py`, dispatch/finalizer tests.
  - Verify: targeted pytest command plus full backend suite.
  - Progress (2026-08-25): unsafe/error, Off, dispatch, finalizer, and all
    Talking Head strict branches are covered, including required benign no-op
    cases and typed probe/pre-cap/analysis/apply failures.
- [x] **T5 (P2, human: ~30m / CC: ~10m)** — Documentation — Amend plan 010 and
  renderer/test comments with the final contract table and rollback rule.
  - Surfaced by: Code Quality finding 3.
  - Files: `plans/010-silence-filler-cut.md`, renderer/test comments.
  - Verify: docs name Off, required, legacy, and `disabled` consistently.
- [ ] **T6 (P1, human: ~1h / CC: ~15m)** — Release gate — Deploy, run three
  production canaries, inspect 24h metrics, and prove no new legacy snapshots.
  - Surfaced by: Architecture finding 3 and failure-mode review.
  - Files: no repo edit; Fly/Vercel deployment and admin debug.
  - Verify: all canary gates in Rollout and rollback are green.

## Completion Summary

```text
+====================================================================+
|        ENGINEERING PLAN REVIEW — COMPLETION SUMMARY                |
+====================================================================+
| Step 0 Scope Challenge | scope accepted; 6 files, no new service   |
| Architecture Review    | 3 issues; all resolved                    |
| Code Quality Review    | 3 issues; all resolved                    |
| Test Review            | diagram; targeted gaps closed             |
| Performance Review     | 0 issues; Off path gets cheaper           |
| Outside Voice          | blocked before transmission by security  |
+--------------------------------------------------------------------+
| NOT in scope           | written (7 items)                         |
| What already exists    | written                                   |
| TODOS.md updates       | 0 proposed                                |
| Failure modes          | 0 critical silent gaps after coverage     |
| Parallelization        | sequential                                |
| Lake Score             | 4/4 complete recommendations chosen       |
| Unresolved decisions   | 0                                         |
+====================================================================+
```

## Addendum 2026-08-31: explicit-consent budget clamp

The strict promise above ("unsafe/error ⇒ typed failure") shipped, and prod
immediately proved its blind spot: three deterministic `unsafe_plan` failures
(jobs ca380890/12ccbd80/5c798650, one 10.0s talking-to-camera clip with ~6.1s
of pauses + fillers, Aug 25–31). A clip whose dead air exceeds the 40% auto
rail is exactly the clip the feature exists for, and "Retry speech cleanup"
could never succeed because the analysis is content-deterministic.

Decision (approved by Yasin, option A of the 2026-08-31 investigation): under
`required_v1`, `build_cut_plan(over_budget_policy="clamp")` now clamps the
removal set to `min(MAX_REMOVAL_FRAC_REQUIRED·dur, dur − MIN_OUTPUT_S) −
CLAMP_BUDGET_SLACK_S` (0.55 / 1 ms) — largest removals kept whole first, the
first non-fitting removal trimmed (edge-anchored for lead/trail cuts,
symmetric mid-clip), remainder dropped. The clamp is traced
(`silence_cut_clamped` event: proposed vs delivered vs budget) and persisted
additively on the summary (`clamped`/`proposed_removed_s`/`clamp_budget_s`).
`legacy_auto`/`off_v1` and the default bailout policy stay byte-identical;
the "never silently publish uncleaned output" rule holds — the render is
CLEANED, up to budget. Kill switch `SPEECH_CLEANUP_BUDGET_CLAMP_ENABLED=false`
restores this plan's original typed-failure contract.

Three hardenings from the adversarial review of the first implementation:
1. **1 ms budget slack** — trimmed spans recompute with ~1 ulp of float error;
   on the binding `dur − MIN_OUTPUT_S` leg (5.0–6.67 s clips) that tripped the
   epsilon-free output rail and resurrected `unsafe_plan` (~18 % of short
   over-budget clips in the reviewer's fuzz run).
2. **Word-snapped trims** — a trim boundary landing strictly inside a word
   would resurrect a partial vocalization; boundaries now snap out of word
   interiors (always shrinking, + PAD_S), preserving remap_words' precondition
   that removals never intrude into kept words.
3. **Forced removals are protected** — `_validate_speech_cut_publication`
   requires every forced/manual-review interval to stay covered by the
   rendered plan, so protected removals are charged against the budget first
   and never dropped or trimmed (a pathological forced set can still hit the
   MIN_OUTPUT_S rail, which correctly bails rather than shipping a <3 s clip).

A second adversarial round (ship-stage red team + fresh-context subagent)
hardened three more edges before landing:
4. **Edge cuts are charged first** — the duration-ordered greedy could spend
   the whole budget on mid/trail blocks and drop the LEAD cut, shipping a
   dead-air opening against the hook-window rule; lead/trail cuts (classified
   by kept words, not raw coordinates, so a silencedetect range closing inside
   the container still counts) now outrank interior cuts.
5. **Forced protection covers per-span intersections, never their hull** —
   two far-apart forced cuts in one merged carrier no longer drag the dead
   gap between them into the protected set. (The protected branch is armed
   but unreachable today: required_v1 runs `include_retakes=False`, so review
   candidates — the only forced_removals writer — cannot exist on clamp jobs.)
6. **Snap guard zones include the PAD_S flank** — a trim boundary landing
   just after a formerly-removed word resurrected it with sub-pad clearance
   to the jump cut; the boundary now clears word + PAD_S.
Deferred with TODO entries: trim boundaries inside tokenless acoustic-filler
regions (P2) and merged-carrier flank recovery (P3).

Pins: `TestOverBudgetClamp` (pure module: greedy/anchors/snap/protection/
float-band/lead-priority/intersections/pad-clearance) and the
required/kill-switch/parity/flag-default tests in
`tests/tasks/test_generative_build_silence_cut.py`.

## GSTACK REVIEW REPORT

| Review | Trigger | Why | Runs | Status | Findings |
|--------|---------|-----|------|--------|----------|
| CEO Review | `/plan-ceo-review` | Scope & strategy | 0 | — | Backend incident fix; no product-scope review required |
| Codex Review | `/codex review` | Independent 2nd opinion | 0 | BLOCKED (security) | Outside-voice transmission was rejected before any plan data was sent |
| Eng Review | `/plan-eng-review` | Architecture & tests (required) | 1 | CLEAR (PLAN) | 17 issues/gaps, 0 critical silent gaps, 0 unresolved |
| Design Review | `/plan-design-review` | UI/UX gaps | 0 | — | No UI change planned; existing recovery UI is reused |
| DX Review | `/plan-devex-review` | Developer experience gaps | 0 | — | Not required for this backend contract fix |

- **VERDICT:** ENG CLEARED — ready to implement in the isolated worktree.

NO UNRESOLVED DECISIONS
