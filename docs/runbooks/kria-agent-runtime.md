# Kria runtime v2 operator runbook

Runtime v2 is dark by default. Preserve runtime-v1 creation while introducing
the new tables, queue consumer, API reader, and web projection.

## Local setup and verification

```bash
bash scripts/worktree-setup.sh
make kria-replay FIXTURE=nermin-matcha-update
make kria-replay FIXTURE=render-approval-required
make kria-replay FIXTURE=ambiguous-render-dispatch
make kria-replay FIXTURE=stale-project-replan
make verify-kria
```

The replay must work without provider or storage credentials. For the real UI,
use the normal `./scripts/dev-auto.sh` and dev-login setup from `CLAUDE.md`.

Seed one empty local runtime-v2 project, then paste its returned thread ID into
the app or API workflow:

```bash
cd src/apps/api
.venv/bin/python -m app.cli.kria_dev seed --email kria-dev@local.test
.venv/bin/python -m app.cli.kria_dev reset --thread-id <thread-uuid>
.venv/bin/python -m app.cli.kria_dev reset --thread-id <thread-uuid> --apply
```

Reset is dry-run by default and never deletes history. `--apply` cancels only
unfinished turns, pending approvals, and unstarted executions for that exact
runtime-v2 thread, clears its active lease, and returns the session to feedback.
The CLI refuses remote and production-named databases.

## Diagnose from a creator report

Start with the thread or turn ID, not raw SQL. Resolve this chain:

```text
thread_id
  -> runtime_version + latest semantic sequence
  -> turn_id + lease epoch/status
  -> approval_id / execution receipt
  -> job_id / variant_id / generation_id / celery task_id
```

The terminal state determines the wording and safe action:

| State | Meaning | Safe action |
|---|---|---|
| `awaiting_approval` | nothing consequential executed | approve current pins or replan |
| `accepted` | editor state and Job committed | reconcile broker publication |
| `dispatched` | broker accepted the deterministic task | inspect Job/worker state |
| `completed` | exact observed target settled | show/play result |
| `outcome_unknown` | side effect may have happened | reconcile before any retry |
| `stale` | a revision or ownership pin changed | refresh and replan |

Never retry an ambiguous dispatch by creating a new Job. Reconcile the committed
Job and deterministic task identity first.

Use the admin helper so the token remains out of shell history:

```bash
python scripts/admin.py GET 'kria/trace?thread_id=<thread-uuid>'
python scripts/admin.py GET 'kria/trace?turn_id=<turn-uuid>'
python scripts/admin.py POST 'kria/reconcile/dry-run?thread_id=<thread-uuid>' --json '{}'
```

The trace is redacted by construction: it contains statuses, revisions, hashes,
timestamps, tool names, and opaque identities, but no creator message, prompt,
transcript, draft body, media path, or signed URL. The dry run lists exact safe
recovery actions and performs no mutation or broker publish.

The generic reply "I couldn't finish that step, but your project and saved draft
are safe" is an `assistant_error` event: `run_kria_turn` raised before any tool
receipt, so the turn ends `failed` with `error.code = runtime_turn_failed` and
the traceback sits in the `light` process group's worker log (it consumes
`agent-control`). A Main Creator attempt ending `terminal_output_truncated` ran
out of `max_output_tokens` (8,192), which Gemini 3 thinking shares with the
answer. A `kria_turn_lease_renewal_failed` warning is not a failure: the
heartbeat retries on its next 5-second tick and the turn keeps planning.

The same reply with `error.code = runtime_turn_claims_exhausted` means three
runs of the turn ended without a result: each was killed at `run_kria_turn`'s
time limit or lost its worker, so its planning lease lapsed. The reconciler (or
a redelivered task that reaches the lapsed lease first) counts each lapse once
in `creator_agent_turns.abandoned_claims`; a requeue after a thread-revision
conflict and the reconciler's backoff stamp on a waiting pending turn do not
count, so `lease_epoch` overstates it. The next claim fails the turn instead of
planning it again, promotes the queued follow-up in the same transaction, and
logs `kria_turn_claims_exhausted`. Look for the kills (`TimeLimitExceeded`,
`WorkerLostError`) in the `light` worker log. Guards: the real-Postgres
`test_turn_abandoned_three_times_fails_and_promotes_its_successor` and
`test_turn_requeued_past_the_cap_and_stamped_while_waiting_is_still_claimed`.

## Adding a tool or recovery

- Add tools through `app.kria.registry.KRIA_TOOLS`; do not add prompt-only names.
- Keep route functions to auth, input parsing, service call, and serialization.
- Do not hold database locks during model, evidence, storage, or broker calls.
- Each `run_kria_turn` plans inside a fresh `asyncio.run` loop. Open async DB
  sessions on a per-call `NullPool` engine, as `_plan_with_live_agent` does,
  never the shared `AsyncSessionLocal` pool: an asyncpg connection pooled by the
  previous turn in the same Celery child fails with "attached to a different
  loop". Guards: `test_consecutive_live_turns_in_one_worker_each_plan_on_their_own_engine`
  (`tests/kria/test_runtime_v2.py`) and the real-Postgres
  `test_live_planner_session_survives_consecutive_task_event_loops`.
- Growing the Main Creator's output (plan schema, reaction beats) eats the same
  budget as its thinking. Re-check `tests/agents/test_thinking_budget.py`, which
  pins that a heavy-thinking plan fits and that the budget runs out before
  `spec.timeout_s` (60 s).
- Extend the checked tool snapshot and whole-turn replay fixture.
- For prompt changes, bump the v2 `AgentSpec.prompt_version`, run structural
  replay evals, and run the required live judged fixtures before cohort rollout.

### Rendered follow-up routing

Once a draft has a rendered snapshot, the planner first runs the narrow
`nova.creator.brief_extractor` prompt (`2026-10-07-v2`) to turn the follow-up into
typed brief requirements. Text and style changes, plus timing scoped to a
title, can stay on the current snapshot through editor operations. Broad clip
timing, stale or unsupported targets, and clarifying outcomes remain in the
recovery path; they do not silently create a replacement video. An explicit
full replan or structured clip picker is the path that invokes Main Creator.

The follow-up binds to the current plan item and draft version before saving.
Keep that binding and save in the same atomic completion path so a stale turn
cannot attach its requirements to a newer draft.

Run `make verify-kria` before the wider backend/web suites. Renderer-affecting
changes still require their existing local-render and overlay gates.

The checked `tests/fixtures/kria_format_matrix.jsonl` contains 30 structural
cases for each canonical token. Regenerate it only through
`src/apps/api/scripts/generate_kria_format_matrix.py`, review the label diff,
then run the focused gate. These cases freeze requested intent and recovery
coverage; they do not substitute for the consented live or real-media gates.

## Incident corpus

Every production request-following failure becomes a permanent, redacted fixture
that CI re-checks (KRI-470 / KRI-480). Records live in
`src/apps/api/tests/fixtures/incidents/*.json`; the schema and loader are in
`src/apps/api/tests/incidents/`; the runner is `test_incident_corpus.py`:

```bash
cd src/apps/api && .venv/bin/python -m pytest tests/incidents -q
```

It runs in the normal test-api CI shards (every `tests/**/test_*.py` is picked up).

**What the corpus proves, and what it does not.** pytest can prove contract
construction (real `build_render_contract` over a real `BriefBinding`), that the
real verifiers decline (`verify_phone_recipe` on a recipe from the real speech
compiler, `preflight_cloud_contract`) and with which typed reason, and whether the
planner asks a question. It cannot hear or watch a render. `output` facts (voice
present, audio playing once, order on screen) are judged against recorded
*observations*: the incident's own (plan fields, creator report) and, after a fix,
a `post-fix` observation. Nothing renders in pytest, so an `output` record flips
only when the owning PR appends a post-fix observation whose `proof` resolves to a
real repro (a pytest node id that exists, a `make` target, a Swift filter or script
in the repo; free text is rejected by the loader) and removes the xfail. Real output
proof belongs to that repro (swift export proof / `make local-render`); a record whose
repro is `pending` names the ticket that owns it. Never describe the corpus as output
coverage it does not run.

**Kinds.** `output` (a real incident with output facts), `clarification` (conversation
turns; must-ask and must-not-ask), `routing`, and `contract_pin`. A `contract_pin` is
NOT incident replay: it pins what the contract builder does for a plan shaped like a
past incident, with expected values read off its own approved plan, so a later change
is noticed. Route assertions (`route`) are deliberately absent until PR-D's resolver. The corpus builds contracts with `CLIP_INTENTS_ENABLED` on (as prod), so a record's resolved `order` intents seat a described start/end clip in `order_ids` (KRI-503).

**Record format** (`tests/incidents/models.py`): `id`, `incident` (Linear id + one
line), `kind`, `approved` (strategy + brief as persisted, validated against
`CreativeStrategy`/`CreativeBrief`), `inputs` (redacted media: opaque id, duration,
capture time, speech facts; the conversation `turns`; optional `phone_recipe`,
`cloud_preflight`, `cloud_adapter` + `cloud_receipt`/`cloud_evidence`/`guided_plan`,
`synthetic` clips), `expect`
(`contract`, `refusal`, `cloud`, `question` or `no_question`, `output_facts`; structured
facts only, never copy), `observations`,
`xfail` (`{reason: "KRI-47x / PR-x", scope: contract|refusal|question|output}`; the
scope must be something the record really asserts), and `repro` (resolved against the
repo by `test_repro_command_points_at_something_real`, including the `::test` part and
`-k` ids). `inputs.voice_behind_footage` (KRI-479; optional `answers`: option keys replayed through the real
`resolve_choices`) composes the approved plan with the REAL voice-behind-footage composer and
verifier (`test_voice_behind_footage_record_composes_and_verifies`: plan-to-recipe proof, the
latest observation's facts must equal the compiled recipe's; the export proof is
`scripts/ios/phone-audio-parity.py --cases voice_behind_footage`). `synthetic` clips are deterministic ffmpeg colour/tone substitutes
regenerated on demand (`tests/incidents/synthetic.py`); generated media is never
committed. `refusal.reason`/`field_path` are the typed decline (KRI-476 / PR-A) and are
asserted unconditionally, so a record can stage them as a strict xfail.

**Cloud evidence records (KRI-470 / PR-E).** `expect.cloud` runs the real
`preflight_cloud_contract(adapter=inputs.cloud_adapter)` and `verify_cloud_variant`
(`preflight: passes|declines`; `plan_gate` for a guided plan the real compiler builds from
`inputs.guided_plan`; `publication: accepts|declines` with the typed `reason` /
`field_path`). Guided evidence is derived from that plan's timeline by the real evidence
builder; a hand-built `inputs.cloud_evidence` is used only where the point is wrong
evidence. Whether a renderer emits such evidence on real output is proven by
`tests/tasks/test_cloud_render_receipts.py`, `tests/pipeline/test_guided_cloud_evidence.py`
and the PR-E real-output evidence.

**Clarification harness.** It runs the real creator-output adapter and then the real
clarification gate (`planner._gate_unresolved_choices`), which `plan_live_turn` calls
once the approved media snapshot is attached (KRI-476 / PR-C). The harness attaches the
record's media snapshot, supplies the record's brief, and replays the record's
`choice_question` / `choice_selection` turns as the thread events. A `question`
expectation asserts the kind, the offered option keys and `min_options`; a
`no_question` record is the must-not-ask control. `kria_choice_questions_enabled` gates
the gate.

**Capture-fail-first workflow.**

1. Capture the failing case read-only and add the record first. For prod:
   `python scripts/admin.py --prod GET /admin/jobs?limit=200`, then
   `jobs/<id>/debug` and `creation-threads/<thread>/turns` (GET only; see
   [agent navigation](agent-navigation.md)).
2. Assert the creator's *request* (the expectation), not the bad behaviour. It must
   fail for the intended reason: run it with `--runxfail` and read the message.
   Mark it `xfail(strict=True)` with the owning ticket / PR letter and scope.
3. The fixing PR makes it pass and flips its own records: for `contract`, `refusal` and
   `question` records the product change makes them XPASS and strict mode forces the
   xfail's removal; for `output` records see above (post-fix observation with a real proof).
4. Keep a passing control next to the failing assertion (the same incident through a
   path that already works) and cover phone and cloud variants where both exist:
   `kri469-voice-clip-ignored` (phone) and `kri469-cloud-variant` (cloud) are the pattern.

**Redaction rules.** New prod captures never carry private media, credentials or
identity: no signed URLs, GCS paths, emails, user/job/thread ids, creator names,
captions or transcripts that identify a person, places, or tokens. Media ids are
opaque hashes, capture times are rebased to a synthetic epoch (order and gaps kept),
speech is reduced to `has_speech`/`to_camera`, and the creator's typed request is
paraphrased. Exemption: content that is already committed on `main` (for example the
East Run thread, the KRI-126/129 titles and the KRI-118 shapes) may be reused verbatim
in a record. Raw prod payloads stay out of git and out of tickets; summarize decision
names and outcomes.

**Owner and cadence.** Owner: Emir Erben. Review weekly, 15 minutes. Checklist:

- [ ] Contract-decline reasons seen this week (which `unresolved`/refusal messages fired, and for which creator ask)
- [ ] Escaped failures: bad outputs that shipped without a decline (new records, written failing first)
- [ ] New capability gaps (asks the product cannot yet honour, with an alternative offered)
- [ ] Unnecessary questions (asked when the request was already clear)
- [ ] Refusal dead ends (a refusal with no next step for the creator)
- [ ] Whether final outputs obeyed the creator's answers to questions

## Deployment order

1. Deploy additive migrations and queue/task support with runtime v2 disabled.
2. Deploy API dual-read support and the web client that understands both versions.
3. Run shadow planning for internal accounts with no side effects.
4. Enable internal durable execution and verify one real format end to end.
5. Enable the named all-format cohort only after every format matrix passes.

The controlling flags are `KRIA_RUNTIME_V2_ENABLED` on the API/worker and
`NEXT_PUBLIC_KRIA_RUNTIME_V2_ENABLED` in the web build. Set Fly first, verify API
readiness, then build Vercel with the frontend flag. Disable the frontend entry
first during rollback, then stop new backend claims after accepted work settles.
Creation-thread and existing editor/render feature flags remain independent.

The flag does not migrate or auto-upgrade threads. An internal v2 client must
explicitly create a thread with `runtime_version: 2` and no inline message, then
submit the first message through `/creation-threads/{id}/turns`. Keep both flags
off for user-visible traffic until all format acceptance matrices and the
consented Nermin evaluations have passed.

For API diagnosis, bootstrap the newest semantic page with
`GET /creation-threads/{id}`; continue forward with `after_sequence`, or
retrieve older bootstrap pages with `before_sequence`. Internal editor refreshes
request `projection=full` explicitly. The `/delta` path remains a compatibility
alias.
Before approving or denying, read
`GET /creation-threads/{id}/approvals/{approval_id}` and echo its fingerprint
with the displayed thread/draft revisions. A `KriaProblem` response is safe to
show to the creator; use its trace ID for operator correlation.

## Rollback

Disable new runtime-v2 thread/turn claims first. Let committed Jobs settle,
cancel queued turns and pending approvals, keep committed drafts/results readable,
and render the runtime-v1 compatibility projection where applicable. Do not
downgrade the additive migration during an incident.

## Alerts and release blockers

- expired lease on an active turn;
- accepted execution with no Job/task movement;
- old `outcome_unknown` receipt;
- more than one Job generation per approval;
- semantic observer lag or cursor projection failure;
- agent-control queue wait p95 above two seconds;
- useful assistant output p95 at or above 30 seconds;
- format completion/recovery regression or rising editor escape.

Any unsafe intent, duplicate render, cross-tenant lookup, false completion claim,
or silent unrecoverable state blocks cohort expansion.

The code and structural corpus are necessary but not sufficient to open the
cohort. Release also requires recorded live judged runs, three consented Nermin
projects (lifestyle, matcha business, travel), real-media results for all eight
tokens, accessibility/concurrency recovery evidence, and the latency SLOs. Do
not mark these external gates passed from synthetic fixtures.
