# Video-poster backfill and mobile-preview acceptance

Use this runbook to repair retained real-user videos whose Library tile has no
usable poster. The workflow generates source-matched JPEGs without re-rendering
the videos, then proves that a second strict scan has no work left.

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
`would_generate=0`; `expired_source`, `unresolvable_legacy_url`,
`orphan_cleanup_failed`, `failed`, and `skipped_not_owned` must all be zero.
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
`<source>.poster.backfill-<uuid>.jpg` keys. When a later render replaces one,
the renderer commits a cleanup receipt on the Job before attempting deletion.
Migration `0091` adds the sparse partial index used by the five-minute
`sweep_job_storage_deletions` Beat task. The sweep processes a bounded number of
Jobs and receipts, verifies both ownership and the committed replacement, and
retries transient storage failures. Account deletion also includes both sides
of every receipt.

## Production acceptance

After the backfill is green, validate a signed-in mobile-sized Library session:

- initial grid load mounts zero `<video src>` elements and makes no MP4 request;
- ready tiles return a decodable `image/jpeg` poster;
- missing or failed posters coalesce into `POST /me/jobs/posters/refresh`
  requests of at most 200 owner-scoped job IDs; the no-store response returns
  only poster URL, stable identity, and `ready` / `repairing` / `unavailable`
  status, and omits missing or foreign jobs;
- a posterless tile makes `GET /me/jobs/{job_id}/playback-url` only after Play;
- the returned fresh URL mounts exactly one preview, and starting another stops
  the previous one;
- a poster refresh that settles while a preview is playing does not unmount or
  replace that active video;
- Stop unmounts the video and restores focus; a failed or 15-second stalled
  attempt returns to Retry without reusing a list-time or prior signed URL.

Record the merged SHA, Fly image digest, workflow run URL, first-pass counters,
zero-work counters, and mobile network/DOM evidence in the release handoff.
