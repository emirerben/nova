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
   to copy the item's clips, pool images, pool videos and voiceover to
   `/private/tmp/nova-replay-job/<id>/inputs`. Pool assets come from the `gcs_path` and
   `gcs_generation` fields the item debug exposes (library assets live outside the item's pool
   prefix); a production API that predates those fields only yields assets under the item's pool
   prefix. Paths outside the job owner's `users/<id>/` prefix are refused. The bucket comes from `STORAGE_BUCKET` in the repo-root `.env` or `--bucket`. Nothing is
   ever written to production.
2. Writes the source manifest and debug files `scripts/replay_creator.py` reads. The creator
   request is `all_candidates.creator_request` on the job.
3. Creates a scratch database `nova_creator_fidelity_test_<run id>` (migrated to head; one per
   run, so two replays never wipe each other, dropped when the run ends), starts a
   Celery worker on Redis database 14 under a private `NOVA_CELERY_QUEUE_NAMESPACE`
   (`replay-<job>-<time>`; it never consumes the shared dev queues), and runs
   `replay_creator.py --render --prod-flags tests/prod_flags.snapshot.json`.
4. Prints an `outcome`: `video_rendered` (local path of each output, `<run dir>/storage/<object
   path>`), `device_plan_ready` (an iPhone job: the worker built and pinned the device render
   plan and the job sits `awaiting_device`; the plan is in `<run dir>/device-plan.json`; this is
   the success state of every phone-first job), `device_plan_incomplete` or `failed` (status,
   failure reason and error detail), or `main_creator_question` (the Main Creator asked the
   creator a clarifying question instead of proposing a plan; the question text and reason code
   are printed and the run stops at once). A replay does not answer it: production resolved that
   question in the chat, and the chat turns are not replayed, so a job whose request depends on
   a clarification may end here. Exit code 0 for the first two only.

The replayed footage is exactly the media the job's brief binding recorded (its clip
assignments and visuals), not whatever the plan item holds now: pool assets added after the job
ran are left out, and a recorded media id that can no longer be recovered refuses the run before
any model call. The same refusal applies to a creator song (`replay_creator.py` does not seed
songs) or a voiceover the item no longer holds. A job with no brief binding (older than the
binding) replays everything the item holds, and the printed coverage says
`bound_to_recorded_media: false`.

Costs real model calls (clip analysis, Main Creator, text agents). Check the receipt and the
worker log (`<run dir>/worker.log`) for the agent runs.

## Safety

* Production contact is a GET through `scripts/admin.py --prod` and a read-only
  `gcloud storage cp` from the bucket to a local path.
* The replay subprocesses get a clean environment: `PATH`, `HOME`, and the `*_API_KEY` model
  keys from `.env` (not `ADMIN_*`, `INTERNAL_*`). No production database, storage, Redis or
  admin credential reaches them.
* `replay_creator.py` refuses anything but a local PostgreSQL database named
  `nova_creator_fidelity_test` or `nova_creator_fidelity_test_<run id>` with no query string
  (libpq takes `host=` from the query, so a query could redirect the connection), `STORAGE_PROVIDER=local` with `E2E_FIXTURES=true`, Redis
  `localhost:6379/14`, and an output directory below `/tmp` separate from the inputs.

## Production flag snapshot

`src/apps/api/tests/prod_flags.snapshot.json` holds the effective production value of every
boolean `Settings` field, every `*_user_ids` allowlist, every `*_rollout_percent` and
`phone_render_verified_features`. `tests/_prod_profile.py` (the `prod_profile` fixture),
`scripts/kria/score_route.py` and `replay_job.py` read it. Older score and iOS E2E scripts
(`scripts/kria/score_29_ekim.py`, `scripts/ios/kri-1009-talking-title.py`,
`scripts/ios/qendresa-caption-shape-e2e.py`) still carry their own hand-copied lists; they are
not production-faithful.
`scripts/prod_flag_snapshot.py` writes it. Each entry carries its evidence:

| Source | Meaning |
| --- | --- |
| `default` | The code default. Production may override it; that was not read. Unconfirmed. |
| `legacy-fly-digest-2026-10-08` | The flag shared `PHONE_RENDERING_ENABLED`'s Fly secret digest on that date (the former hand list). A digest comparison, no value read. Later changes are not in it. |
| `live:phone-render-config` | Read from production with `GET /admin/kria/phone-render-config` (names, no secret). |
| `decisions-md:<date>`, `runbook:<name>` | A value the repo's own docs record as applied in production (for example `IOS_DEVICE_ONLY_MODE=true` and `CLOUD_RENDER_EXECUTION_ENABLED=false`, DECISIONS.md 2026-11-02). Only as current as the doc. |
| `job-data:<what>-<date>` | Inferred from production job records read with `GET /admin/jobs/<id>/debug` (for example every sampled job carries `creator_brief_binding`, so `KRIA_BRIEF_BINDING_ENABLED` is on). |

What is approximated:

* A false flag whose entry says `default` may be on in production. `python
  scripts/prod_flag_snapshot.py unverified` lists the boolean flags still unconfirmed.
* Allowlists are Fly secrets. The snapshot holds the code default, not production membership.
  The gates differ in what an empty list means: `phone_render_user_ids` and
  `kria_runtime_v2_phone_user_ids` read empty as everyone, while the brief-binding, approval-
  readiness, suggestion-chip, steer-now, creative-brief, clip-facts and editorial-taste
  allowlists only ever add accounts (empty admits nobody). A job whose account sits inside an
  allowlist in production but not in the replay takes a different path; where a job record
  proves the cohort is on (brief binding), the global flag is set instead.
* Values changed after 2026-10-08 that no doc records are missing. `check` fails when a doc
  line says a setting is "ON in prod" and the snapshot has it off (CLAUDE.md,
  agents/DECISIONS.md, `docs/runbooks`, `docs/pipelines`); other phrasings, such as an applied
  `fly secrets set` in a decision record, are only caught if someone runs `set` for them.

Recording the documented values moved the test profile too: production is device-only
(`IOS_DEVICE_ONLY_MODE=true`, `CLOUD_RENDER_EXECUTION_ENABLED=false`) with brief-binding writers
on, and about 45 existing tests across seven files were written against the cloud lane or the
unbound draft path. They take the `prod_profile_pre_cutover` fixture (or
`apply_prod_profile(..., hold_at_default=PRE_CUTOVER_SETTINGS)`), which holds those three
settings at the code default; they are the known places the "production profile" is not
production. Converting a file to the full profile means deciding what the device-only path should
assert there.

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
  reproduces everything up to and including that plan (`device_plan_ready` on success, the
  failure reason when the plan cannot be built). What the phone then renders is not replayed;
  use the iOS gates for that.
* Model output varies between runs; a different result on the same input is data, not a flake.
  State both outcomes in the PR.

## In a PR

A creation-bug fix PR names the production job id it replayed and what the run produced (see
`.github/PULL_REQUEST_TEMPLATE.md`).
