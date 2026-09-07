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

## Adding a tool or recovery

- Add tools through `app.kria.registry.KRIA_TOOLS`; do not add prompt-only names.
- Keep route functions to auth, input parsing, service call, and serialization.
- Do not hold database locks during model, evidence, storage, or broker calls.
- Extend the checked tool snapshot and whole-turn replay fixture.
- For prompt changes, bump the v2 `AgentSpec.prompt_version`, run structural
  replay evals, and run the required live judged fixtures before cohort rollout.

Run `make verify-kria` before the wider backend/web suites. Renderer-affecting
changes still require their existing local-render and overlay gates.

The checked `tests/fixtures/kria_format_matrix.jsonl` contains 30 structural
cases for each canonical token. Regenerate it only through
`src/apps/api/scripts/generate_kria_format_matrix.py`, review the label diff,
then run the focused gate. These cases freeze requested intent and recovery
coverage; they do not substitute for the consented live or real-media gates.

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
