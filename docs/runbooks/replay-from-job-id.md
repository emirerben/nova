# Replay a production job from its id (KRI-1375)

A creation bug is fixed when the same footage and request produce a finished video under
production settings with the real models. Unit tests and stubbed replays do not show that;
they passed while the founder hit the same failure again (KRI-1375 sessions 42606c80,
88c26d40). `make kria-replay-job` does the real run from a failed job id, so the PR can carry
the job id and the result.

```bash
make kria-replay-job JOB=<production job id>
```

## What it does

`src/apps/api/scripts/replay_job.py`:

1. `python scripts/admin.py --prod GET /admin/jobs/<id>/debug` and
   `GET /admin/plan-items/<item>/debug` (GET only), plus `gcloud storage cp gs://<bucket>/...`
   to copy the item's clips, pool images and voiceover to `/private/tmp/nova-replay-job/<id>/inputs`.
   The bucket comes from `STORAGE_BUCKET` in the repo-root `.env` or `--bucket`. Nothing is
   ever written to production.
2. Writes the source manifest and debug files `scripts/replay_creator.py` reads. The creator
   request is `all_candidates.creator_request` on the job.
3. Recreates the scratch database `nova_creator_fidelity_test` (migrated to head), starts a
   Celery worker on Redis database 14 under a private `NOVA_CELERY_QUEUE_NAMESPACE`
   (`replay-<job>-<time>`; it never consumes the shared dev queues), and runs
   `replay_creator.py --render --prod-flags tests/prod_flags.snapshot.json`.
4. Prints the replayed job status, the failure reason and error detail if any, and the local
   path of each rendered output (`<run dir>/storage/<object path>`), plus the receipt path.
   Exit code 0 only when a video exists.

Costs real model calls (clip analysis, Main Creator, text agents). Check the receipt and the
worker log (`<run dir>/worker.log`) for the agent runs.

## Safety

* Production contact is a GET through `scripts/admin.py --prod` and a read-only
  `gcloud storage cp` from the bucket to a local path.
* The replay subprocesses get a clean environment: `PATH`, `HOME`, and the `*_API_KEY` model
  keys from `.env` (not `ADMIN_*`, `INTERNAL_*`). No production database, storage, Redis or
  admin credential reaches them.
* `replay_creator.py` refuses anything but a local PostgreSQL database named
  `nova_creator_fidelity_test`, `STORAGE_PROVIDER=local` with `E2E_FIXTURES=true`, Redis
  `localhost:6379/14`, and an output directory below `/tmp` separate from the inputs.

## Production flag snapshot

`src/apps/api/tests/prod_flags.snapshot.json` holds the effective production value of every
boolean `Settings` field, every `*_user_ids` allowlist, every `*_rollout_percent` and
`phone_render_verified_features`. `tests/_prod_profile.py` (the `prod_profile` fixture),
`scripts/kria/score_route.py` and `replay_job.py` read it; nothing keeps a hand copy.
`scripts/prod_flag_snapshot.py` writes it. Each entry carries its evidence:

| Source | Meaning |
| --- | --- |
| `default` | The code default. Production may override it; that was not read. Unconfirmed. |
| `legacy-fly-digest-2026-10-08` | The flag shared `PHONE_RENDERING_ENABLED`'s Fly secret digest on that date (the former hand list). A digest comparison, no value read. Later changes are not in it. |
| `live:phone-render-config` | Read from production with `GET /admin/kria/phone-render-config` (names, no secret). |

What is approximated:

* A false flag whose entry says `default` may be on in production. `python
  scripts/prod_flag_snapshot.py unverified` lists the boolean flags still unconfirmed.
* Allowlists are Fly secrets. The snapshot holds the code default (empty, which most gates read
  as everyone), not production membership. A job whose account sits outside an allowlist
  takes a different path in production than in the replay.
* Values changed after 2026-10-08 that nobody recorded are missing.

Refresh without reading a Fly secret value:

```bash
cd src/apps/api
python scripts/prod_flag_snapshot.py refresh --live      # re-reads phone-render-config, keeps recorded evidence
python scripts/prod_flag_snapshot.py set KRIA_FOO_ENABLED=true \
  --source "claude-md:2026-11-13" --describe "CLAUDE.md says ON in prod"
python scripts/prod_flag_snapshot.py check                # exit 1 if Settings and snapshot disagree
```

Record a flag with `set` when a repo source states production's value (CLAUDE.md, DECISIONS.md,
a runbook that names the applied Fly secret) or when someone with secret access tells you; put
that source in `--source`. `tests/test_prod_flags_snapshot.py` fails when `Settings` gains a
flag the snapshot has not seen, so a new flag cannot silently run at its default in a
"production" test. Never paste a secret value into the snapshot.

## Fidelity limits

* The replay drives the Main Creator route and the real orchestrator task from the footage in
  storage (iPhone creations upload analysis proxies, not the originals) and the creator request
  recorded on the job. It does not replay the chat turns before the request.
* A device-render job (`native_device_only`) ends in a server-side plan, not an MP4. The replay
  reproduces failures that happen while that plan is built; for those the output is the replayed
  job's status and reason, not a video.
* Model output varies between runs; a different result on the same input is data, not a flake.
  State both outcomes in the PR.

## In a PR

A creation-bug fix PR names the production job id it replayed and what the run produced (see
`.github/PULL_REQUEST_TEMPLATE.md`).
