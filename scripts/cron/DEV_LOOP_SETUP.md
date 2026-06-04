# Autonomous dev-loop — home-box setup & runbook

Wires the merged Phase 2 ship-gate (PR #432) to a schedule so it actually runs.
The builder + gate ticks are already in `scripts/cron/`; this is the launchd
scheduler + intake glue. Rollout is **manual-trigger-first**: install the timer
disabled, prove one task end-to-end by hand, then enable it.

```
queued ─builder tick─> in_progress ─> gating ─gate tick─> awaiting_approval ─you merge─> done
```

Work hours are enforced by the runners themselves (`work_hours_guard_or_exit`):
**UTC Mon-Fri 11:00-18:59**. Off-hours ticks exit quietly. `NOVA_BUILDER_FORCE=1`
bypasses the guard for a manual run.

## Pieces

| File | Role |
| --- | --- |
| `scripts/cron/dev_loop_tick.sh` | launchd wrapper: env/PATH, secrets, dedicated checkout, overlap lock, runs builder→gate sequentially |
| `infra/launchd/com.nova.dev-loop.plist` | 30-min timer template (substituted by the installer) |
| `scripts/cron/install-dev-loop.sh` | one-time idempotent setup |
| `scripts/cron/build_task_runner.sh`, `gate_runner.sh`, `_dev_loop_lib.sh` | the merged ticks (PR #432) |

## 1. One-time install

```bash
bash scripts/cron/install-dev-loop.sh
```

This:
- clones a **dedicated checkout** at `~/.nova/loop/nova` (override with
  `NOVA_DEV_LOOP_REPO`). The loop owns it — the builder runs `git checkout -B
  builder/<id>`, which must never touch your interactive repo.
- scaffolds `~/.nova/dev-loop.env` (chmod 600).
- renders `~/Library/LaunchAgents/com.nova.dev-loop.plist` (timer **not** loaded).

## 2. Secrets (`~/.nova/dev-loop.env`)

Only one key is mandatory. `claude` and `gh` use their existing logins on the box.

```sh
ADMIN_PROD_API_KEY=...        # required; from `fly secrets list -a nova-video` / your vault
# GH_TOKEN=...                # only if `gh` isn't logged in here
# NOVA_BUILDER_TIMEOUT_S=900  # optional per-run caps
# NOVA_GATE_TIMEOUT_S=2400
```

`ADMIN_PROD_API_KEY` lives here, **never** in the checkout's `.env` — the headless
builder runs `--permission-mode bypassPermissions` and could read `.env`, so the
runners refuse to start if the prod key is in it. `scripts/admin.py` picks the key
up from the environment (`{**.env, **os.environ}`).

## 3. Intake — enqueue a task

No new tool: `scripts/admin.py` is the intake CLI. Provenance defaults to
`trusted` (only trusted signals may mint in v1).

```bash
cd ~/.nova/loop/nova
ADMIN_PROD_API_KEY=$(grep -E '^ADMIN_PROD_API_KEY=' ~/.nova/dev-loop.env | cut -d= -f2-) \
  python scripts/admin.py --prod POST build-tasks \
  --json '{"title":"Tidy a docstring in app/services/build_gate.py","body":"Small, low-risk: improve one docstring; run pytest for that module."}'
```

Keep the first dogfood task **low-risk** (a docstring / a single unit test) so the
gate can pass cleanly.

## 4. Prove a tick (manual, before enabling the timer)

```bash
# builder: queued -> in_progress -> gating (or checkpointed)
NOVA_BUILDER_FORCE=1 bash ~/.nova/loop/nova/scripts/cron/dev_loop_tick.sh builder
# gate: gating -> awaiting_approval, opens a PR
NOVA_BUILDER_FORCE=1 bash ~/.nova/loop/nova/scripts/cron/dev_loop_tick.sh gate
```

Watch the queue and logs:

```bash
cd ~/.nova/loop/nova && python scripts/admin.py --prod GET build-tasks
tail -f ~/.nova/logs/dev-loop-*.log
```

A PR should appear on the repo; its body carries the gate evidence table. You are
the merge gate.

> Ops: the gate runs the full `pytest` + `npm test`. Bring up local infra in the
> dedicated checkout first — `docker-compose up -d redis db` — or DB-backed tests
> will (correctly) fail the gate.

## 5. Enable the recurring timer

Only after step 4 passes:

```bash
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.nova.dev-loop.plist
```

Disable / pause:

```bash
launchctl bootout gui/$(id -u)/com.nova.dev-loop
```

The timer fires every 30 min; the work-hours guard keeps it to UTC Mon-Fri 11-18.

## Notes

- **Before this PR merges to main**, the dedicated checkout (cloned from
  `origin/main`) has the runners but not `dev_loop_tick.sh`. Run the wrapper from
  your worktree pointed at the checkout:
  `NOVA_DEV_LOOP_REPO=~/.nova/loop/nova NOVA_BUILDER_FORCE=1 bash scripts/cron/dev_loop_tick.sh builder`.
  After merge, re-run the installer so the checkout has the wrapper.
- Builder + gate never run concurrently: the wrapper runs them sequentially under
  `/tmp/nova-dev-loop-tick.lock` (distinct from the gate's own
  `/tmp/nova-dev-loop.lock`).
- Deferred (unchanged): Phase 3 phone-approval surface, Phase 4 auto-ship, Phase 5
  TASKS.md / Telegram intake.
