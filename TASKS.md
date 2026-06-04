# Nova dev-loop backlog

The autonomous dev-loop mints one **build_task** per unchecked item below.

- **Add an item:** `- [ ] Title :: optional one-line spec/body`
  (everything before `::` is the title; everything after is the body the agent reads)
- **Queue them:** `scripts/queue.sh sync`
  (idempotent — skips any title already in the queue; `- [x]` items are ignored)
- **Review:** `scripts/queue.sh ls` (or `ls awaiting_approval` / `ls blocked`)
- Items stay here as a record. Dedup is by **title** against existing build_tasks,
  so re-running `sync` never double-mints. Check an item off (`- [x]`) to stop the
  loop from ever considering it.

> Priority: prefix a title with `(p<N>)` to set priority (lower = sooner), e.g.
> `- [ ] (p10) Fix the flaky upload test :: ...`. Default priority is 100.

> Note: large multi-file features (a whole frontend page) are bigger than the
> current builder reliably does in bounded chunks — treat the items below as a
> tracked backlog for a focused session until the builder handles larger scope.

## Backlog

- [ ] (p80) Web review page for the dev-loop queue :: Next.js admin page at src/apps/web/src/app/admin/build-tasks/page.tsx listing build_tasks by status, showing each gate_report (per-gate pass/fail table) and linking the PRs. Mirror src/apps/web/src/app/admin/jobs/page.tsx; add a typed client src/apps/web/src/lib/admin-build-tasks-api.ts over the /api/admin proxy; re-queue/block buttons (PATCH action). Inherits ADMIN_BASIC_AUTH middleware. Add a Jest test under src/apps/web/src/__tests__/admin/. Verify the real admin patterns first.
- [ ] (p80) Gate advisory: run /review on the diff :: In scripts/cron/gate_runner.sh, replace the placeholder `add_result qa 0 1 "advisory /qa not yet wired headless"` with a real time-bounded headless `claude --print "/review"` against the rebased diff, capturing a short verdict into the advisory gate result + PR body. Stays NON-blocking (advisory only).

<!-- Add `- [ ] Title :: body` items above. `scripts/queue.sh sync` mints them. -->
