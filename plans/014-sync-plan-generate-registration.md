# 014 — Synchronous job registration for plan-item Generate

**Status:** DONE — shipped v0.23.2.0 (2026-08-04)
**Owner:** Yasin
**Incident:** 2026-08-04 ~11:17Z — mobile user stuck on a frozen "Starting…" button for ~8 minutes after tapping Generate (usekria.com, plan item `f2b9201d`, job `4fb8fa0f`).

## Problem

Tapping **Generate video** on a plan item can leave the user on the setup page
with a disabled "Starting…" button for minutes (observed: ~8 min; frontend
tolerates up to 15 min — `RENDER_REGISTER_TIMEOUT_MS`) with zero feedback,
then a misleading "The render didn't register — give it another go" error if
the window expires. The user expects the desktop-normal behavior: flip to the
progress view within seconds and wait there.

## Confirmed root cause (prod-verified 2026-08-04)

`POST /plan-items/{id}/generate` does **not** create the Job row. It only
enqueues the Celery task `generate_plan_item_videos` on the **default
`celery` queue** and returns the item unchanged
([plan_items.py](../src/apps/api/app/routes/plan_items.py), `generate_item`).
The task mints the Job (`_dispatch_item_render` in
[content_plan_build.py](../src/apps/api/app/tasks/content_plan_build.py)),
and only once that row exists does `derive_item_status()` return
`"generating"` — the signal the frontend needs to switch to the progress
view.

The render worker is a **single machine, `--concurrency=1`, draining
`celery` + `plan-jobs` + `overlay-jobs`** (fly.toml). So the tiny Job-minting
dispatch task queues **behind in-flight renders** (8–20 min each) and
clip-analysis tasks. Until the slot frees, no Job row → no status change →
frozen "Starting…".

Evidence (prod admin API, 2026-08-04):

| UTC | Event |
|---|---|
| 11:17:29 | Job `61c4d859` (another item) takes the single worker slot |
| ~11:17 | User taps Generate → dispatch task queued behind it |
| 11:25:35.99 | `61c4d859` finishes |
| 11:25:36.23 | User's Job `4fb8fa0f` created — **244 ms after the slot freed** (FIFO drain signature) |
| 11:31:23 | Orchestrator starts (waited behind clip-analysis tasks; this phase WAS visible as "queued") |
| 11:32:18 | `variants_ready` |

Corroboration: content_plan jobs routinely show multi-minute `created→started`
gaps (3 min on 08-02, 100 min on 08-01), while **public generative jobs'
`created_at` is the click time** because `POST /generative-jobs` builds the
Job synchronously in the route
([generative_jobs.py](../src/apps/api/app/routes/generative_jobs.py),
`create_generative_job`) — exactly the pattern this plan ports.

Ruled out: render-worker autostop (`RENDER_AUTOSTOP_ENABLED` not set in Fly
secrets; worker machine up continuously through the incident), mobile-specific
frontend behavior (no divergent code path), stale caching on polls.

## Fix design (single PR, API-only)

### Flow after the change

```
BEFORE                                      AFTER
user taps Generate                          user taps Generate
  │ POST /generate                            │ POST /generate
  │  └─ .delay() ──► [celery queue]           │  ├─ thread: dispatch_item_render_for(item)
  │     (returns; item unchanged)             │  │   ├─ build_generative_job → Job(queued) COMMIT
  ▼                                           │  │   ├─ item.current_job_id = job.id
"Starting…" (frozen, minutes —               │  │   └─ enqueue orchestrator → [plan-jobs]
 Job minted only when the single             │  │       └─ publish FAILS? → job=processing_failed,
 worker slot frees)                          │  │                            route 502, retry works
                                             │  └─ response: status="generating", new job id
                                             ▼
                                            next 2s poll → progress view, honest "queued" phase
```

### Core: mint the Job synchronously in `POST /plan-items/{id}/generate`

Reuse `_dispatch_item_render` **unchanged** as the single source of truth,
called in-request via `anyio.to_thread.run_sync` with its own `sync_session`
— the same wrapping `generate_plan_item_videos` does, minus Celery.

**Shared helper (DRY — eng review Q1):** extract one module-level function in
`content_plan_build.py`:

```
dispatch_item_render_for(plan_item_id: str) -> DispatchResult
# DispatchResult: {job_id} | already_active{job_id} | invalid_clips
#                 | missing_row | publish_failed
```

Both the Celery task body AND the route thread call this helper — zero drift
in guards (missing item/plan), logging, or dispatch semantics. The route maps
the typed result to accurate responses: `invalid_clips` → 422 with a real
message, `missing_row` → 404, `publish_failed` → 502.

**Concurrency lock (outside voice C1):** `jobs.content_plan_item_id` has no
uniqueness constraint — by design (retries mint new Job rows; prod item
`9e916fd3` has two). So an unlocked status check cannot prevent two
concurrent POSTs from minting two Jobs. The helper loads the PlanItem with
`SELECT … FOR UPDATE` and re-checks for an active (non-terminal) current job
INSIDE the lock; if one exists it returns `already_active{job_id}` instead of
minting. Pinned by a genuinely concurrent test (two real sessions), not a
sequential one.

**Idempotent duplicate handling (outside voice C2, decision D5):** on the
sync path, `already_active` (and the route's pre-flight "already generating"
check) returns **200 with the item's current state** — NOT 409. Rationale: on
flaky mobile, a POST whose response is lost leaves the user retrying; a 409
lands in the frontend's error path (no refetch) and re-strands them on the
setup view while the render actually runs. A 200 rides the existing success
path → immediate refetch → page flips. The C1 lock is what prevents duplicate
minting; the 409 no longer protects anything. Kill-switch fallback mode keeps
the legacy 409 (byte-identical contract when the flag is off).

**Publish-failure containment (eng review A1 + outside voice C3/C4):** the
reaper deliberately never reaps `queued` jobs
([reaper.py](../src/apps/api/app/tasks/reaper.py), `_NON_TERMINAL_STATUSES`
comment), and `_dispatch_item_render` commits the Job BEFORE
`enqueue_orchestrator_sync`. A broker-publish failure therefore strands a
forever-`queued` ghost — and this is TRUE TODAY on the task path as well:
`generate_plan_item_videos` declares `max_retries=1` but has no
`autoretry_for` and never calls `self.retry`, so a publish exception just
fails the task and leaves the ghost (outside voice C4 — the plan's earlier
"re-raise preserves autoretry" claim was wrong; there is no autoretry).
Fix inside the helper, uniform for every caller: wrap the enqueue — on
failure, mark the just-minted Job `processing_failed` +
`failure_reason="dispatch_publish_failed"`, commit, and return
`publish_failed`. No re-raise in any context.

Ambiguity note (C3): `apply_async` raising does not prove Redis rejected the
message. If it was actually delivered, the orchestrator's existing redelivery
guard ([generative_build.py](../src/apps/api/app/tasks/generative_build.py),
`_NO_RERUN_STATUSES` skip) sees the terminal job and skips — worst case is
one false 502 and a manual retry, never a double render. That is the safe
side of the ambiguity; an outbox is deliberately not warranted here.

Blast-radius bonus (C7): with the helper never raising, a publish failure
during `activate_content_plan`'s multi-item loop no longer aborts the
remaining items or the activation-state persistence — behavior improves.
Pinned by a continuation test.

**Async-session staleness (eng review A2, calibrated by outside voice C5):**
the UI flip is driven by `handleGenerate`'s immediate `refetch()` (a fresh
GET/session) — the frontend discards the POST body, so the flip works even
without it. `db.expire_all()` before the route's reload is still required so
the POST *response contract* is correct (fresh `current_job_id` /
`status: "generating"` for any client or test that reads it). Pinned by a
real two-session DB test — not an `AsyncMock` call-assertion (C8; see prior
learning `raising-spy-swallowed-by-fail-open` for why mock spies here are
fake coverage).

What changes for the user: the Job row exists when the POST returns, so the
immediate refetch (and every poll after it) sees `status: "generating"` and
the page flips to the progress view (which already renders the "queued"
phase) right after the click — matching desktop expectations and making the
queue wait *visible* instead of a frozen button.

Two failure-semantics improvements fall out for free:

1. **Validation errors surface immediately.** Today the task's `invalid_clips`
   path silently returns None and the user stares at "Starting…" for 15 min.
   Now it's an immediate 422 with a real message.
2. **The double-Generate hole closes for real.** The old guard
   (`derive_item_status == "generating"`) was ineffective during the
   registration window (no Job row yet) and unlocked against concurrent
   requests. The FOR-UPDATE lock + `already_active` → 200 makes duplicate
   requests both safe and self-healing.

Kill switch (house style): `PLAN_SYNC_DISPATCH_ENABLED` (default `true`).
`false` → route falls back to `generate_plan_item_videos.delay()`,
byte-identical to today. Pin with a flag-off test.

**Deliberately NO frontend change (eng review D2):** with sync minting the
15-minute `RENDER_REGISTER_TIMEOUT_MS` window is dead code on the happy path
and only governs the kill-switch fallback mode — where multi-minute queue
waits are legitimate (8 min observed). Shrinking it would break the only mode
that uses it (premature "didn't register" error → retry → duplicate
dispatch). The window stays 15 min as the fallback safety net.

Unchanged call sites — deliberately out of this PR's blast radius:
- `activate_content_plan` calls `_dispatch_item_render` inline in its own task (no change).
- `POST /content-plans/{id}/generate-first-week`
  ([content_plans.py](../src/apps/api/app/routes/content_plans.py)) keeps
  the `.delay()` bulk path (up to 7 dispatches; latency not user-blocking there —
  the dashboard doesn't sit on a "Starting…" button).
- `generate_plan_item_videos` task stays (bulk path + kill-switch fallback),
  its body reduced to a thin call into `dispatch_item_render_for`.

### Test plan

- `test_generate_item_mints_job_synchronously` — POST response has
  `status == "generating"` and a fresh `current_job_id`; Job row exists with
  `celery_task_id == str(job.id)`; orchestrator enqueued once with
  `queue="plan-jobs"` (capture `apply_async`).
- `test_generate_response_carries_fresh_job_id` — **identity-map pin (A2/C8):**
  REAL two-session DB test (no `AsyncMock`): async session loads the item,
  a second sync session commits the Job, the route response must carry the
  new job id; fails if `db.expire_all()` is dropped.
- `test_generate_item_duplicate_post_returns_active_job` — **D5 pin:** second
  POST while a job is active ⇒ 200, same `current_job_id`, exactly ONE Job
  row for the item.
- `test_generate_item_concurrent_posts_mint_single_job` — **C1 pin:** two
  concurrent requests against a real DB ⇒ FOR-UPDATE serializes them; one
  mints, the other gets `already_active`; exactly one Job row.
- `test_generate_item_invalid_clips_422` — silent-failure path now surfaces.
- `test_generate_item_publish_failure_marks_job_failed` — **A1 pin:**
  `apply_async` raising ⇒ Job flips `processing_failed` /
  `dispatch_publish_failed`, route 502, no stranded `queued` row.
- `test_activation_continues_after_publish_failure` — **C7 pin:** publish
  failure on item N of the activation loop ⇒ item N marked failed, items
  N+1… still dispatched, activation state persisted.
- `test_generate_item_kill_switch_falls_back_to_task` — flag off ⇒ `.delay()`
  called, no Job row minted in-request, legacy 409 contract intact
  (byte-identical legacy).
- Existing guards that must stay green untouched:
  `tests/services/test_job_dispatch.py` (orchestrator routing source-grep),
  activation-seed tests, generate-first-week tests, `TestNoGeminiTextLeaks`.

### Checklist

- [x] Extract `dispatch_item_render_for` (typed result) in `content_plan_build.py`; task body delegates to it
- [x] FOR-UPDATE item lock + in-lock active-job re-check → `already_active` (C1)
- [x] Publish-failure containment inside the helper (mark failed + commit; NO re-raise in any context — C4)
- [x] Route: flag-gated sync dispatch via `anyio.to_thread.run_sync` + `db.expire_all()` + typed-result → HTTP mapping (`already_active` ⇒ 200 current state — D5)
- [x] `PLAN_SYNC_DISPATCH_ENABLED` in `config.py` (default `true`, kill-switch description per house style)
- [x] All 8 tests above
- [x] `plans/README.md`: add 014 row
- [x] `CLAUDE.md` env-var list: one line for the kill switch

## Rollout

1. Land the PR. Deploy Fly (api + worker restart — both import `content_plan_build`).
2. Verify in prod via `scripts/admin.py --prod GET jobs?limit=5`: next plan-item
   generate shows `created_at` ≈ click time even while another render is mid-flight.
3. Version-skew safe: old web + new API just sees faster registration (no FE change shipped).

## NOT in scope (deferred with rationale)

- **Worker topology** (second render machine / moving clip-analysis off the
  render worker / per-queue concurrency) — the queue wait itself remains; this
  plan makes it *visible and honest*, not shorter. Cost decision with Emir →
  captured in TODOS.md (eng review D3).
- **Fetch timeout on mutating plan-api calls** — real mobile edge (hung POST
  ⇒ silent forever-"Starting…", polling stopped pre-click), but not this
  incident's cause and touches every mutation → TODOS.md (eng review D4).
- **Frontend registration-window change** — dropped entirely (eng review D2, see above).
- **`light` machine observed stopped in prod (2026-08-04)** — sweeper/digest/
  TikTok polls not running. Ops follow-up with Emir; not a render blocker,
  unrelated to this fix.
- **The 6-min orchestrator queue wait** (11:25→11:31) — visible as "queued"
  after this fix; acceptable until the topology discussion.
- **Client idempotency-key header** (outside voice C2's "ideally") — the
  FOR-UPDATE lock + 200-on-active covers the only consumer (our frontend);
  a header protocol earns its keep only if third-party clients appear.
- **Bounding the in-request broker publish** (outside voice C6) — rebutted:
  the current route ALREADY publishes in-request (`.delay()` at the same call
  site), so broker exposure is unchanged by this plan; Celery's default
  connect timeout + retry policy bounds it, and the proxy's 60s cap is the
  existing backstop.

## What already exists (reused, not rebuilt)

- `build_generative_job` + sync in-route minting — proven in
  `POST /generative-jobs` (public flow); this plan ports the pattern.
- `_dispatch_item_render` — stays the single source of truth for the
  PlanItem→render contract; reused verbatim via the extracted helper.
- `enqueue_orchestrator_sync(queue="plan-jobs")` — dispatch contract and
  `celery_task_id == job.id` correlation unchanged (job_dispatch guard tests
  keep passing untouched).
- Progress view's "queued" phase + retry-on-failed controls (#768/#769) —
  the UI needs zero changes to benefit.

## Post-implementation code review deltas (2026-08-04, /review)

The review army (5 specialists + Claude adversarial + 2 Codex passes) ran on
the implemented diff; these fixes were folded in beyond the plan text:

- **MissingGreenlet 500 (perf specialist, CRITICAL — verified):** `user` is an
  ORM row on the request session; `db.expire_all()` made the subsequent
  `user.id` access a sync lazy-load → 500 on EVERY successful generate. Fixed
  by capturing `owner_id` pre-expire. Tests now authenticate via the REAL
  header path (not a dependency override) so this class reproduces.
- **Conditional publish-fail UPDATE (Codex CX1):** containment now writes
  `processing_failed` only `WHERE status='queued'`; rowcount 0 ⇒ the worker
  claimed the message despite the raise ⇒ report `dispatched` (running render
  never clobbered, no duplicate invite). Pinned by
  `test_publish_raised_but_worker_claimed_reports_dispatched`.
- **Activation loop now locks + skips active items (Claude-adv CA2 / Codex
  CX2):** the seed-activation path minted without the FOR-UPDATE re-check and
  could clobber user clips mid-activation. Same guard as the helper now.
- **`DispatchOutcome` Literal + unknown-outcome ⇒ 500 (CA3/M1):** the route no
  longer falls through to 200 on an unrecognized outcome.
- **Job-status buckets truly single-sourced (Codex CX3 + M3):**
  `template_ready`/`music_ready` added to `PLAN_ITEM_JOB_READY` (add-to-plan
  items no longer read "generating" forever / block re-generate) and
  routes/me.py now derives from the shared constants. Drift guard:
  `test_plan_terminal_covers_orchestrator_no_rerun_statuses`.
- **Test hardening:** module skip-guard + `*_test` DB-name rail,
  thread-liveness asserts, missing_row branch coverage, add-to-plan terminal
  pin.
- **Deferred to TODOS.md (with sketches):** crash-window `dispatching` interim
  status; sync-pool/broker publish budgets for the in-request path.
- Suppressed as cosmetic/pre-existing: 502-vs-proxy-502 ambiguity, 409/422
  no-clips split across the race window, kill-switch fallback's on-loop
  `.delay()` (rollback-only), generate-first-week conversion (documented NOT
  in scope above).

## GSTACK REVIEW REPORT

| Review | Trigger | Why | Runs | Status | Findings |
|--------|---------|-----|------|--------|----------|
| CEO Review | `/plan-ceo-review` | Scope & strategy | 0 | — | — |
| Codex Review | `/codex review` | Independent 2nd opinion | 1 | RAN (outside voice) | 8 findings: 6 absorbed, 2 rebutted with code evidence |
| Eng Review | `/plan-eng-review` | Architecture & tests (required) | 1 | CLEAR | 4 issues, 0 critical gaps (A1 publish-ghost, A2 staleness, Q1 DRY, D2 scope cut) |
| Design Review | `/plan-design-review` | UI/UX gaps | 0 | — | — |
| DX Review | `/plan-devex-review` | Developer experience gaps | 0 | — | — |

- **CODEX:** outside voice found the FOR-UPDATE concurrency gap (C1), the 409-strands-lost-response retry (C2→D5: idempotent 200), the inert autoretry claim (C4), the refetch-not-response UI mechanism (C5), and mock-coverage risk (C8) — all folded into the plan. C3 (compensation race) and C6 (broker freeze) rebutted: the orchestrator's `_NO_RERUN_STATUSES` redelivery guard makes mark-failed the safe side of the ambiguity, and the route already publishes to the broker in-request today.
- **CROSS-MODEL:** agreement on 6/8 findings; the 2 disagreements resolved against code (generative_build.py redelivery guard; plan_items.py `.delay()` call site), not opinion.
- **VERDICT:** ENG CLEARED — ready to implement (single API-only PR; decisions D2–D5 all resolved in-session).

NO UNRESOLVED DECISIONS
