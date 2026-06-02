# Autonomous dev-loop — home-box setup runbook

How to stand up the Nova autonomous builder on an always-on machine (the
"home box") under OpenClaw, alongside the grader + queue pieces that ship in
this repo. Design rationale lives in the office-hours plan (not in git); this
file is the executable steps.

**Model:** OpenClaw orchestrates a Claude Code builder + a Codex reviewer on
the home box. Nova's `build_task` Postgres queue (prod) is the **source of
truth**; OpenClaw is the scheduler + observability + notification layer over
it — NOT a second task brain.

## Prerequisites
- An always-on machine, kept powered through the 7-7 work window.
- Claude Code logged in (`claude login`) on the Pro/Max subscription.
- Codex logged in (`codex login`) for the reviewer.
- This repo cloned, on a clean `main` that can `git fetch origin`.
- Nova test deps installed (venv + brew ffmpeg) so the builder can actually
  run tests during a chunk — see "Local dev" in `CLAUDE.md`.

## Track 1 — runnable now (no dependency on the queue PR)
1. Install + configure OpenClaw / Paperclip per the `openclaw-studio` repo's
   `SETUP.md`: `npm i -g openclaw paperclipai` → `openclaw configure` → start
   the Paperclip server → import the company → apply the `claude_local`
   adapter PATCH
   `{"adapterType":"claude_local","adapterConfig":{"command":"claude","args":["--dangerously-skip-permissions"]}}`.
2. Run `setup-cofounder.sh` (clones nova + nova-workspace into
   `~/.openclaw/workspace/startups/nova`).
3. `.env`: copy from `.env.example`, set `ADMIN_PROD_API_KEY` (the runner
   claims from prod via `scripts/admin.py --prod`). No local DB/Redis needed.

## Track 2 — after the queue PR merges + Fly deploys
The `build_task` table, the `build-tasks/*` admin endpoints, and
`scripts/cron/build_task_runner.sh` only exist post-merge.
1. `git pull` on the home box.
2. Point the OpenClaw/Paperclip scheduler at the builder tick:
   `bash scripts/cron/build_task_runner.sh` every ~45 min, Mon-Fri. The runner
   self-guards to UTC 11:00-18:59; set `NOVA_BUILDER_FORCE=1` to bypass for a
   manual test tick.
3. Add the Codex reviewer step (T8): `codex review` on each builder PR, posted
   as a PR comment. Exactly one reviewer, not a fleet.
4. Seed a task or two via `/admin/intake` (or the admin API), then verify a
   tick: `NOVA_BUILDER_FORCE=1 bash scripts/cron/build_task_runner.sh`.

## Verify
- A tick on an empty queue exits 0 cleanly ("queue empty — nothing to build").
- A seeded task is claimed (SKIP LOCKED), a `builder/<id>` branch opens, a WIP
  commit lands, and the task is checkpointed/released.
- The daily digest reports built/graded/escalated + a dead-man's-switch ping if
  the loop was silent during work hours.

## Safety
- The `claude_local` adapter runs with `--dangerously-skip-permissions`: the
  builder can do anything in the repo unprompted. The intake approval gate +
  provenance firewall are the only guardrails — untrusted signals must NOT mint
  build tasks in v1.
- One subscription, two machines: the work-hours guard + a task/day cap keep
  the builder from eating your daytime Claude allowance.
- The home box is now a single point of failure; the heartbeat dead-man's-switch
  pings your phone if it dies.
