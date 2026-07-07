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

- [ ] (p60) PATCH /plan-items shots: return 404 for unknown shot id, not 422 :: XS. See the TODOS.md entry — one-line fix near src/apps/api/app/routes/plan_items.py:714 + a test asserting 404 on a bogus shot id.
- [ ] (p60) Rate-limit POST /generate-guide :: XS. Add the existing @limiter.limit decorator (limiter is already wired for sibling routes) to the generate-guide endpoint per the TODOS.md entry; add a test.
- [ ] (p70) Keyboard a11y for admin music section bands :: XS. src/apps/web/src/app/admin/music AudioPlayer.tsx ~line 260 — keyboard focus/activation for the ranked section bands; the TODOS.md entry includes the test spec.
- [ ] (p70) Expose edit_format in PlanItemResponse + pill on the item page :: XS. Field already exists server-side; thread into the response schema and render a pill on src/apps/web/src/app/plan items detail page per TODOS.md.
- [ ] (p70) Shot-count badge on plan calendar card :: XS. Render filming_guide.length as a badge on the calendar card per the TODOS.md entry.
- [ ] (p90) Extract useNextFrameCallback hook (T-MOTION-1) :: XS (~10 min per TODOS.md). Pull the repeated next-frame callback pattern into a shared hook + swap call sites.

<!-- Add `- [ ] Title :: body` items above. `scripts/queue.sh sync` mints them. -->
