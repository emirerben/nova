# KRI-1240: 10 Visuals took ~4.5 min to prepare

Parent incident: KRI-1234 (Qendresa Q1 talking test, 2026-11-06, plan item `3ca419c4`).
Evidence level: **offline, local benchmark** of the real Celery task through real
Postgres, Redis and the dev GCS bucket, with the Gemini wait replaced by a fixed sleep.
No live-model, device or production claim.

## What the creator saw (the expectation)

From the ticket's own timings: 10 Visuals (5 screenshots, 5 x 18 s screen recordings)
added at 20:40:14Z; 2 ready by 20:43:00, 4 by 20:43:28, 7 by 20:44:08, all by 20:44:48.
One file at a time. Qendresa's Q1 posts use 12-14 Visuals, and she can't send her prompt
until they are ready. Ticket target: 10 Visuals added together all ready in under 90 s.

## Cause

- Creator-facing Visuals analysis is `app.tasks.autoplace.analyze_pool_asset`, published
  to `settings.pool_asset_visuals_queue` = `visuals-analysis` in prod
  (`VISUALS_ANALYSIS_QUEUE` is a set Fly secret).
- Until 2026-11-02 that queue had its own `autoplace` process (`--concurrency=2`).
  Release 2 (#2055, cost cut) removed it, leaving the queue on the `worker` process:
  `--concurrency=1` across 9 queues, autostopped when idle.
- So every Visual waits for the one slot: behind the worker's cold start, behind the
  item's own footage/speech analysis, then one after another.
- The task is a GCS download, a decode/probe and preview, and a Gemini wait. The CPU
  part measured 0.03-0.10 s per file here; the rest is waiting.

## Change

- New Fly process group `visuals`: `celery ... --concurrency=6
  --max-memory-per-child=524288 -Q visuals-analysis`, shared-cpu-2x / 2048MB, always
  on, one machine.
- The `worker` keeps consuming `visuals-analysis` as the fallback. `autoplace-jobs`
  (poster repair, intent-review evidence, footage analysis) and `speech-analysis` stay
  on the worker only.
- The CLIP prewarm skips a process that consumes only `VISUALS_WORKER_QUEUES`
  (`app/services/queue_state.py`, gate in `app/worker.py`).

## Benchmark

Raw report: `docs/reviews/kri-1240-visuals-burst.json`. Harness (local and gitignored, it
has machine-specific paths): `.dev/kri1240-bench/run_bench.sh` and `run_extra.sh` in the
KRI-1240 worktree. Real worker processes started with the exact
prod command shapes; 10 `plan_item_assets` rows dispatched within ~1 s exactly like the
upload route (`begin_attempt` -> `analyze_pool_asset.apply_async(queue=visuals-analysis)`);
time-to-ready read from the DB every 0.5 s.

- Fixtures: 5 PNG 1170x2532 (~0.3 MB) and 5 H.264 MP4 886x1920, 18 s, ~11 MB.
- Gemini: workers run with an empty `GEMINI_API_KEY`; `_analyze_image` / `_analyze_video`
  run for real, then sleep 25 s in place of the Gemini call. No paid call is possible.
- Storage: real dev bucket `nova-videos-dev`, prefix `kri1240-bench/`.
- Machine: an M-series Mac with other sessions running (load averages recorded per run).

## Results (time from dispatch until every file is `ready`)

| Run | Processes consuming `visuals-analysis` | Files | First ready | All ready | Load avg at dispatch |
|---|---|---|---|---|---|
| before (control) | worker `--concurrency=1`, 9 queues (prod today) | 10 | 26.7 s | **276.6 s** | 89.88 80.54 80.69 |
| after, worker awake | worker + visuals `--concurrency=4` | 10 | 26.5 s | **65.1 s** | 52.13 49.02 63.91 |
| visuals alone, c=4 | visuals `--concurrency=4` (worker autostopped) | 10 | 26.2 s | 85.8 s | 9.56 33.45 55.57 |
| visuals alone, c=4 | same | 14 | 26.3 s | 120.2 s | 4.00 16.86 42.49 |
| **visuals alone, c=6** | visuals `--concurrency=6` (worker autostopped) | 10 | 26.6 s | **60.7 s** | 9.11 21.81 37.05 |
| **visuals alone, c=6** | same | 14 | 26.7 s | **83.0 s** | 54.47 30.90 42.47 |

All 68 files across the six runs reached `ready` (none `failed`). The shipped setting is
concurrency 6: it meets the 90 s target for 10 and for Qendresa's 14 even when the
worker is asleep and contributes no slot. With the worker awake it adds one more slot.

Per-file seconds, 10-file runs:

| File | before | after (c=4 + worker) | visuals alone c=4 | visuals alone c=6 |
|---|---|---|---|---|
| shot1.png | 26.7 | 27.0 | 26.2 | 26.6 |
| shot2.png | 51.8 | 27.0 | 26.2 | 26.6 |
| shot3.png | 77.2 | 27.0 | 26.2 | 26.6 |
| shot4.png | 102.4 | 26.5 | 26.2 | 26.6 |
| shot5.png | 127.6 | 27.5 | 53.0 | 26.6 |
| rec1.mp4 | 154.7 | 54.9 | 56.6 | 28.7 |
| rec2.mp4 | 182.0 | 60.5 | 57.1 | 60.1 |
| rec3.mp4 | 209.2 | 65.1 | 59.2 | 58.1 |
| rec4.mp4 | 239.2 | 60.5 | 80.2 | 60.6 |
| rec5.mp4 | 276.6 | 57.5 | 85.8 | 56.5 |

The `before` run reproduces the incident's shape: one file every ~25-30 s.

## Memory (visuals process, macOS RSS sampled every 0.5 s)

| Run | Idle parent | Idle tree | Peak child | Peak tree |
|---|---|---|---|---|
| c=4, 10 files | 329.8 MB | 425.6 MB | 120.8 MB | 768.8 MB |
| c=4, 14 files | 327.9 MB | 424.1 MB | 123.8 MB | 773.7 MB |
| c=6, 10 files | 311.9 MB | 439.7 MB | 118.5 MB | 946.4 MB |
| c=6, 14 files | 318.6 MB | 403.6 MB | 118.2 MB | 739.2 MB |

Prewarm line from the visuals process in every run: `[worker_ready] visuals-only worker (small VM) — skipping CLIP prewarm`.

macOS RSS is not Linux RSS: a forked Linux child also counts the parent's copy-on-write
pages it maps, so on Fly each child should read roughly parent (~300 MB) + its own
work (~120 MB). The 512MB per-child recycle threshold is meant to sit above that
routine peak and is pinned below half the VM. Not measured on Linux: if prod logs
billiard's "child process exceeding memory limit" after every task, raise it
(recycling is a cheap fork, so the cost of being wrong is small).

## Negative control

The `before` run is the control: same task, fixtures and Gemini delay on the prod
topology before this change. It reproduces the incident's shape (one file every
~25-30 s, last at 276.6 s for 10). The change is the only difference between `before`
and `after`.

## Gaps (not claimed)

- No production measurement yet. After deploy, re-run a 10-Visual burst on prod and
  record the ready times (ticket's done-when: under 90 s).
- Real Gemini latency varies (images are one inline call; videos upload + process +
  call). The 25 s sleep is the ticket's observed per-file time, not a provider SLA.
- macOS memory numbers understate Linux RSS (see above). The worker's own CLIP prewarm
  was disabled in the bench launcher (macOS fork + torch crash); it does not affect the
  visuals process, which ran the unmodified `app/worker.py`.
