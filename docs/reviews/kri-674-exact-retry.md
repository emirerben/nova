# KRI-674: retry the approved edit

Before: iPhone Retry sent “Try generating this edit again.” That created a new
creator turn, re-analyzed clips and could fail in Main Creator before dispatch.

After: a typed request targets the failed execution. When its complete native
recipe is saved, fresh consent permits exactly that recipe to run again under a
new device revision. The turn, draft, clips, text, timing and audio stay fixed.
Concurrent taps share one pending approval; lost queue acknowledgement resumes
the accepted attempt once. Expired unstarted consent can be renewed, but newer
requests, changed source/draft/generation or ownership block stale recovery.
An accepted stale retry receipt is retired without changing the newer session.

The physical test also exposed a UI issue: a failed native Job can remain
`awaiting_device`, which the project projects as “rendering.” Its pending retry
approval must take precedence over that old state. Ordinary in-flight renders
still retain their existing precedence over unrelated new plans.

## Scope and evidence

The original KRI-673/KRI-674 job failed before a complete recipe was saved. It
cannot be replayed truthfully. That path now explains the limitation and asks
for a new version plus approval. Frozen cloud execution adapters and preparing
all specialist output before approval remain KRI-722/KRI-723 work.

Automated evidence: real HTTP, Postgres and normal approval/dispatch/observer
code; no paid model calls. A failing planner sentinel forbids accidental new
creative planning. Cases cover duplicates/concurrent taps, consent expiry,
lost dispatch acknowledgement, foreign owner, stale draft/request/generation,
unknown work, unsupported recipe, missing original and missing saved program.
Red controls observed the missing endpoint (404), misleading observer retry,
expired consent replay, wrong-generation recovery and stale accepted receipt.

Hardware evidence is recorded below after inspecting the actual export.
Private source videos, full payloads and result bundles remain outside git.

Repeatable focused checks:

```bash
REDIS_URL=redis://127.0.0.1:16474/0 bash scripts/check-api.sh test \
  tests/kria/test_render_retry_postgres.py \
  tests/kria/test_runtime_v2.py tests/kria/test_runtime_tasks.py \
  tests/routes/test_kria_runtime.py tests/kria/test_stale_and_expired_approvals.py \
  tests/routes/test_lock_order.py -q
```

Use a disposable Redis and the worktree scratch DB. Keep physical QA in a
separate database: other integration tests intentionally truncate test tables.
