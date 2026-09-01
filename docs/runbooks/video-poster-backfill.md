# Video-poster backfill and mobile-preview acceptance

Use this runbook to repair retained real-user videos whose Library tile has no
usable poster. The workflow generates source-matched JPEGs without re-rendering
the videos, then proves that a second strict scan has no work left.

There are two actuators. This document leads with the batch **Video Poster
Backfill** workflow (bounded, audited, leaves a receipt); the in-product
on-demand repair that heals the long tail as users browse is documented under
[On-demand repair](#on-demand-repair-tasksrepair_job_poster).

## Safety contract

- Run only the GitHub **Video Poster Backfill** workflow on `main`.
- `expected_sha` must be the exact lowercase 40-character revision deployed on
  every managed `nova-video` Fly Machine. The workflow rejects a mixed or
  unhealthy fleet and checks out only that proven revision.
- The backfill and the GitHub **Fly Deploy** workflow acquire the same
  app-unique, unmanaged Fly Machine guard. Do not run bare `fly deploy`; it
  bypasses this serialization contract.
- The repair excludes the synthetic development user and runs in strict mode.
  It never treats a missing, malformed, foreign, or unverifiable asset as a
  successful repair.
- Never manually edit `_poster_backfill_cleanup_receipts` or destroy a retained
  failed backfill Machine before diagnosis. Those are recovery evidence.

## Run after the exact revision deploys

1. Confirm the **Fly Deploy** workflow for the merged `main` SHA is green. Its
   verification requires one managed image digest, the matching OCI revision
   label, and a healthy `/health` response. The process proof requires at least
   one started API Machine, started `light` and `autoplace` Machines, and a
   render worker that is either started or has a non-OOM, non-restarting
   requested-stop receipt from the app-managed idle lifecycle. An additional
   stopped API Machine is accepted only when its service config proves Fly
   autostop. Normal `starting`/`stopping` transitions are bounded-waited for
   380 seconds before the workflow fails closed; any other stable stopped
   topology is rejected immediately.
2. Resolve the exact SHA and dispatch the repair:

   ```bash
   git fetch origin main
   expected_sha="$(git rev-parse origin/main)"
   gh workflow run video-poster-backfill.yml \
     --ref main \
     -f expected_sha="$expected_sha"
   ```

3. Follow the run to completion:

   ```bash
   gh run list --workflow video-poster-backfill.yml --limit 1
   gh run watch "$(gh run list --workflow video-poster-backfill.yml --limit 1 --json databaseId --jq '.[0].databaseId')"
   ```

The exact-image Fly Machine executes both commands as one bounded receipt:

```bash
python -m scripts.backfill_video_posters \
  --exclude-synthetic --strict --batch-size 25
python -m scripts.backfill_video_posters \
  --dry-run --exclude-synthetic --strict --batch-size 25
```

The first command repairs all eligible historical outputs. The second is the
acceptance audit. Its final `Backfill complete:` line must report
`would_generate=0`; `orphan_cleanup_failed`, `failed`, and `skipped_not_owned`
must all be zero. `expired_source` and `unresolvable_legacy_url` are permanent
physical classifications, not failures: the source object was lifecycle-deleted
before this repair existed, or the row only ever stored a browser URL. The
2026-08-31 census counts roughly 293 expired sources and 62 unresolvable legacy
rows for pre-June history, so those counts are EXPECTED to be non-zero forever
and do not fail the run (they used to, which made the strict gate unachievable
and wedged the shared deploy guard on every attempt). Compare them against the
census to spot anomalies; only growth beyond known history warrants
investigation.
The Machine must exit cleanly and the launcher must remove it. A green workflow
is the durable receipt for both the repair and the zero-work audit.

## Recover a retained failed Machine

A non-clean exit intentionally leaves the exact stopped Machine in place and
the workflow error names its Machine ID. Diagnose its logs first. To acknowledge
and restart that same Machine once during the next workflow invocation, use the
same deployed SHA and the exact retained ID:

```bash
gh workflow run video-poster-backfill.yml \
  --ref main \
  -f expected_sha="$expected_sha" \
  -f retry_failed_machine_id="<exact-stopped-machine-id>"
```

The launcher rejects a different ID, revision, digest, command, metadata
contract, or Machine state. If the acknowledged retry fails again, it remains
stopped for another diagnosis.

### A guard that never ran needs no acknowledgement

A Machine parked *before it ever started* is a different case, and the
acknowledgement recovery above cannot clear it: that path requires a valid
non-clean exit receipt, and a Machine that never started never produced one.
Read its lifecycle with `fly machine status <id>` — `pending → created →
stopped` with **no `start` and no `exit` event** proves the VM never executed
its command, so no repair ran and no data was mutated.

Fly can answer `machine start` with `failed_precondition: unable to start
machine from current state: 'created'` while it is still preparing the ~1GB
image; the launcher now treats only that exact transient as retryable and
retries under its existing reconciliation deadline
(`POSTER_BACKFILL_NEVER_RAN_RESTARTS`, default 3, bounds the restarts).

Because this shape holds the stable guard name that **Fly Deploy** also CASes,
a parked guard would otherwise block every later deploy with no in-tool
recovery. `--acquire-deploy-guard` therefore destroys a guard with no start and
no exit event and proceeds, recording no reconciled revision — so a later
**Video Poster Backfill** run still performs the repair. A guard that actually
ran and stopped without a receipt is still retained for inspection, and a
deploy never restarts a backfill.

If diagnosis proves that the deployed revision itself must be fixed and the
same-image retry cannot succeed, merge the fix to `main`, then use the **Fly
Deploy** workflow's narrow recovery input:

```bash
gh workflow run fly-deploy.yml \
  --ref main \
  -f acknowledge_failed_backfill_machine_id="<exact-stopped-machine-id>"
```

This acknowledges an incomplete repair; it does not waive the failed audit.
The deploy launcher accepts only a manual `main` run and the exact stable guard
ID in `stopped` state with a valid non-clean exit receipt and the original
bounded backfill contract. It prints the Machine logs and receipt, proves its
image is still the exact healthy production image, destroys only that inactive
guard without `--force`, proves the guard is absent, and then acquires the
normal deploy guard for the merged fix. A wrong ID, feature branch, push event,
clean or active Machine, changed digest, unhealthy fleet, or malformed contract
fails closed. Never run bare `fly machine destroy` for this recovery.

After the fix deploy is green, run **Video Poster Backfill** again against its
new live SHA and require the strict zero-work audit.

Created or running receipts resume automatically. An abandoned deploy guard is
reclaimed only after its validated lease and grace period and only when the
managed production fleet is again one healthy digest.

## Durable poster cleanup

Backfilled posters use immutable
`job-posters/<job-id>/<sha1(source)>.poster.backfill-<uuid>.jpg` keys. The
`job-posters/` prefix matches no `infra/gcs-lifecycle.json` rule, so a poster
can no longer be deleted by the lifecycle rule of the video it was extracted
from (`music-jobs/*` at 24h, `jobs/*` at 30d). Runs made before that change
wrote `<source>.poster.backfill-<uuid>.jpg` siblings; both shapes are read,
and the key stored on the Job is authoritative. When a later render replaces one,
the renderer commits a cleanup receipt on the Job before attempting deletion.
Migration `0091` adds the sparse partial index used by the five-minute
`sweep_job_storage_deletions` Beat task. The sweep processes a bounded number of
Jobs and receipts, verifies both ownership and the committed replacement, and
retries transient storage failures. Account deletion also includes both sides
of every receipt.

## On-demand repair (`tasks.repair_job_poster`)

The batch workflow above is the bulk actuator. The second one runs in the
product: when a signed-in user opens the Library, `POST /me/jobs/posters/refresh`
enqueues `tasks.repair_job_poster` (`app/tasks/poster_repair.py`) for an owned
`ready` job whose preview has no poster, and the tile self-heals on the next
refresh. Use it for the long tail; use the workflow when you want a bounded,
audited sweep with a receipt.

### Enable it — queue first, then the flag

The task downloads the full source MP4 into the RAM-backed `/tmp` and runs
ffmpeg. That is the workload that OOM'd the 1GB `light`/Beat machine on
2026-08-02, so it must never land there, and it must never land on the default
`celery` queue where it would head-of-line-block the concurrency=1 render
worker. Set the queue **before** flipping the flag:

```bash
fly secrets set POSTER_REPAIR_QUEUE=autoplace-jobs --app nova-video
fly machine restart <api-machine-id>
fly machine restart <worker-machine-id>

# Only after the queue setting is live on both process groups:
fly secrets set POSTER_ONDEMAND_REPAIR_ENABLED=true --app nova-video
fly machine restart <api-machine-id>
fly machine restart <worker-machine-id>
```

`POSTER_REPAIR_QUEUE` defaults to `celery` in code so local `dev-auto.sh` keeps
working — it consumes no autoplace queue. `worker.py` pins the route in
`task_routes`, so the queue is a property of the task, not of each dispatcher.

### Rollback

```bash
fly secrets set POSTER_ONDEMAND_REPAIR_ENABLED=false --app nova-video
fly machine restart <id>   # api + worker
```

Off is byte-identical to the pure re-signer: no marker writes, no dispatch, and
an already-queued task re-checks the flag and drains as `disabled`. Persisted
terminal verdicts stop being honored too, so a storage incident that minted
`expired_source` in bulk is fully reverted by the flag alone.

### Read the outcome

Every run logs `poster_repair_outcome` with a short outcome string (`generated`
on success, plus `disabled`, `not_repairable`, `expired_source`, `bad_id`, …);
an unexpected exception logs
`poster_repair_failed` and the task returns `error` rather than raising — repair
is fail-open by contract.

State lives on `Job.assembly_plan["_poster_repair"]`: `video_path` (the source
the verdict is bound to, so a re-render voids it), `attempts` (incremented only
on a real extraction failure — never by a deploy or a worker death), `terminal`,
and `enqueued_at`. After `MAX_POSTER_REPAIR_ATTEMPTS` (3) real failures the
marker goes terminal as `attempts_exhausted`; a source object that no longer
exists goes terminal as `expired_source`. Either verdict surfaces to the tile as
`poster_status: "unavailable"` — an honest end state instead of a spinner. The
route dedupes to at most one repair per job per 10 minutes and caps enqueues per
request, so a page of posterless history is repaired over several refreshes
rather than in one burst.

## Production acceptance

After the backfill is green, validate a signed-in mobile-sized Library session:

- initial grid load mounts zero `<video src>` elements and makes no MP4 request;
- ready tiles return a decodable `image/jpeg` poster;
- missing or failed posters coalesce into `POST /me/jobs/posters/refresh`
  requests of at most 200 owner-scoped job IDs; the no-store response returns
  only poster URL, stable identity, and `ready` / `repairing` / `unavailable`
  status, and omits missing or foreign jobs;
- every response carrying a short-TTL signed URL reaches the browser with
  `Cache-Control: no-store` — `GET /me/jobs` and the two routes above set it,
  and `src/apps/web/src/lib/api-proxy.ts` forwards the upstream directive
  instead of dropping it. Check the header at the browser, not just at Fly: a
  proxy that strips it lets Safari heuristically cache expiring links;
- a posterless tile makes `GET /me/jobs/{job_id}/playback-url` only after Play;
- the returned fresh URL mounts exactly one preview, and starting another stops
  the previous one;
- a poster refresh that settles while a preview is playing does not unmount or
  replace that active video;
- Stop unmounts the video and restores focus; a failed or 15-second stalled
  attempt returns to Retry without reusing a list-time or prior signed URL.

Record the merged SHA, Fly image digest, workflow run URL, first-pass counters,
zero-work counters, and mobile network/DOM evidence in the release handoff.
