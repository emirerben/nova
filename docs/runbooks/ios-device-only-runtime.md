# iOS-device-only runtime cutover

This runbook retires new web and cloud video creation without deleting any
Job, database row, or stored output. Authentication, account deletion, project
history, signed playback, and the device upload/completion lifecycle stay live.

## KRI-563: iPhone-only cloud refusal

`IOS_NATIVE_DEVICE_ONLY_ENABLED` is a separate, default-off admission fence for
new videos created in the iPhone app. When enabled, every authenticated native
account receives `creation_mode=device_only` and the currently verified device
rendering capabilities, regardless of the former phone pilot allowlist. Web
creation remains in hybrid mode. An iPhone project that cannot render on the
device keeps its draft and receives a typed refusal before cloud footage upload
or Job creation. Existing cloud outputs remain readable and playable.

Deploy the API, worker, and iOS contract changes together with this flag off.
Before enabling it, verify the signed TestFlight build on an enrolled and a
previously unenrolled account: offered formats, proxy upload, device export,
published playback, editor controls, unsupported-format refusal, and an
existing cloud video's playback. Check the sanitized runtime profile on both
API and worker; their flag and verified feature lists must agree. Then enable
`IOS_NATIVE_DEVICE_ONLY_ENABLED=true` on Fly and repeat those canaries. Audit
new native Jobs for `render_destination=device` and check that no native cloud
task starts. Stop rollout on any unsupported format being offered, missing
voice/captions, stuck device export, or new native cloud render. Record UTC
time, deployed SHA, build, Job IDs, and outcomes without media URLs or user IDs.

This fence does not replace the broader `IOS_DEVICE_ONLY_MODE` / cloud executor
cutover below. That rollout also retires web creation and has its own signed
device, drain, and 24-hour hold points. Emergency rollback of the native-only
fence is `fly secrets set IOS_NATIVE_DEVICE_ONLY_ENABLED=false --app nova-video`;
new threads can then use the former native cloud route, so investigate the
failure first. Existing stamped threads retain their source-upload fence.

After Release 1 passes its own 24-hour acceptance window, Release 2 may
reduce the topology. The eventual end state has exactly three managed
`nova-video` process groups:

- `api`: one shared CPU/1 GB Machine, Fly autostop enabled, warm floor zero;
- `worker`: one shared 2 CPU/4 GB Machine consuming every media queue, stopped
  by the app-managed lifecycle after five continuously idle minutes; and
- `light`: one shared CPU/1 GB Machine, always started, running Beat and the
  worker wake/idle backstop.

`nova-db` and Upstash Redis remain. The estimated provisioned maximum is
approximately $42.64/month. With API and worker asleep, the fixed Fly baseline
is approximately $10.54/month, excluding Redis, bandwidth, external APIs, and
stopped-Machine rootfs storage.

## Non-negotiable release boundary

Use **two production releases**. Do not ship the final `fly.toml` topology in
the admission release.

1. The admission release contains the server/iOS contracts, admission and
   execution flags, task guards, and a `worker` command that also consumes
   `autoplace-jobs` and `speech-analysis`. It temporarily retains the existing
   `autoplace` and `speech_analysis` process definitions, services, VM blocks,
   counts, and the old deploy verifier.
2. The topology release contains the final checked-in `fly.toml`, strict
   three-group deploy/backfill verifier, and their tests. It is deployed only
   after the App Store build is proven, device-only admission is live, cloud
   work is drained, cloud execution is disabled, and Plan 025 has completed
   its 24-hour acceptance window.

If the implementation is presented as one branch, split it at this boundary
into two merge/deploy revisions. A squash of both stages followed by the normal
Fly deploy skips the safety window and is not an approved rollout.

Record the UTC time and Git SHA at every hold point. Never put an access token,
signed URL, Redis URL, media path, transcript, or user identifier into the
release record.

## 0. Immediate reversible reduction

This step may run before either release. First prove there is no broker work or
active render. Run Celery inspection from the always-on `light` Machine; it
broadcasts to every connected worker:

```bash
fly ssh console -a nova-video -g light -C \
  'celery -A app.worker inspect active --timeout=10'
fly ssh console -a nova-video -g light -C \
  'celery -A app.worker inspect reserved --timeout=10'
fly ssh console -a nova-video -g light -C \
  'celery -A app.worker inspect scheduled --timeout=10'
```

Every worker must return an empty list for all three commands. Also capture
queue depth without printing the Redis connection string:

```bash
fly ssh console -a nova-video -g light -C \
  'python -c "from app.worker import celery_app; from app.services.queue_state import get_queue_snapshot, render_worker_idle; print(get_queue_snapshot(celery_app)); print(\"render_worker_idle\", render_worker_idle(celery_app))"'
```

All reported queues must have depth zero and `render_worker_idle` must be
`True`; that second check scans every canonical worker queue even when its
consumer is asleep and therefore absent from the snapshot. If inspection is
unavailable, returns `None`/`unknown`, or any queue/task is non-empty, stop and
investigate; never infer idle from a stopped worker.

Then reduce only the redundant API and speech-analysis counts:

```bash
fly scale count api=1 speech_analysis=1 --app nova-video --yes
fly scale show --app nova-video
fly machine list --app nova-video
```

Expected temporary groups are `api=1`, `worker=1`, `light=1`, `autoplace=1`,
and `speech_analysis=1`. This removes about $26.09/month from the provisioned
ceiling and is reversible without a deploy.

## 1. Admission release and iOS compatibility

Deploy the first release described above. Leave both rollout flags in their
safe defaults:

```text
IOS_DEVICE_ONLY_MODE=false
CLOUD_RENDER_EXECUTION_ENABLED=true
KRIA_MINIMUM_CLIENT_PROTOCOL=2
```

Confirm the deployed revision and temporary five-group topology before changing
cohorts. The unified `worker` must already advertise all of these queues:

```text
celery,plan-jobs,overlay-jobs,creator-guided-jobs,creator-render-v2,
creator-fidelity-v1,autoplace-jobs,speech-analysis
```

The dedicated analysis workers still consume their queues during this overlap;
Celery delivers each message to only one consumer. Do not scale them to zero
yet.

Release the iOS build that sends `X-Kria-Client-Protocol: 2` on ordinary,
refresh, retry, and background-upload requests and that turns a typed 426
`native_update_required` response into the non-dismissible update screen.
Verify the build through TestFlight before App Store rollout.

Remove both mobile allowlists and enable the normal device/runtime controls
while admission is still hybrid:

```bash
fly secrets set --app nova-video \
  PHONE_RENDERING_ENABLED=true \
  KRIA_RUNTIME_V2_PHONE_ENABLED=true \
  PHONE_RENDER_USER_IDS='[]' \
  KRIA_RUNTIME_V2_PHONE_USER_IDS='[]'
```

### Hold point: format-specific signed-device qualification

Use [Plan 025](../../plans/025-ios-device-only-rollout-qualification.md) and its
[test/evidence matrix](../../plans/artifacts/025-ios-device-only-rollout-test-plan.md).
Automated implementation work stops before TestFlight and production operations.
The following gates apply both while admission is hybrid and again after step 2;
passing one montage does not waive any speech or voiceover case.

Before starting, re-derive the effective API and worker boolean/capability profile
without printing credentials. On 2026-10-02 the API's multi-clip Talking and
caption-editor gates were both **on**; both therefore require qualification.
Configuration names alone do not prove the worker agrees: confirm its profile too.

| Canary | Required proof before cloud disable |
|---|---|
| D0: protocol/update | Signed build sends protocol 2 on ordinary, refresh, retry and background upload; old protocol gets the blocking update screen. |
| D1–D2: existing playback and device lifecycle | Existing cloud output plays; a new device project reaches `awaiting_device` → export → upload/complete → `published`, with Play/Save/Share and relaunch playback. Run D2 on **each of the four active mobile accounts**. |
| D3–D4: single-clip Talking | Audible source speech, visible sentence and word captions, correct language, timing through the final word. |
| D5: caption editing | Unsaved caption/title/style/gain edits and saved replacement keep voice; full-rebuild trim/SFX control also keeps voice. KRI-241 needs physical-device evidence. |
| D6: multi-clip Talking | Continuous speaker audio, muted cutaways only in their intended windows, correctly timed captions. If deliberately disabled, verify actionable refusal before Job creation and record the accepted limitation. |
| D7: generic recorded voiceover | Audible narration and intended source/music gains; no invented caption promise. |
| D8: recorded narrated | Audible narration plus visible, timed narration captions. |
| D9: guided voiceover with Visuals | Audible narration, correct Visuals and promised text/caption layers, successful publication. |
| D10–D11: refusal and recovery | Unsupported work is absent or refused before Job creation; retryable failure offers recovery and a fresh attempt reaches publication. No silent cloud fallback. |
| D12: legacy mutation | After admission, a render-affecting mutation returns the typed refusal without changing the old playable output. |

Run the complete enabled-scope matrix on the oldest supported test phone and a
current phone, using the signed production/TestFlight build and the designated
canary account. D2 additionally runs across all four accounts. The oldest phone
also needs the 60-second performance case; a short render does not establish it.
Require sustained 30-fps preview, seek p95 ≤250 ms, 60-second export ≤120 seconds,
and no crash or critical thermal state.

Caption editing stays a **STOP** while KRI-241 lacks passing physical-device
verification. Disabling `PHONE_SUBTITLED_EDITOR_LANES_ENABLED` is an explicit
product downgrade, not a test waiver: record acceptance and prove controls close
with a reason before proceeding. Do not enable additional PiP/SFX/reaction lanes
as part of this rollout; already-enabled lanes still need their applicable checks.

For every case, retain a private record with UTC start/end, Git/deployed SHA,
iOS build, device/OS, boolean flag names and capability names, canary ID,
Job/variant/attempt IDs, state transitions, audio/caption/playback results,
export/wall time, memory/thermal observations, cloud-task check and PASS/FAIL.
Never include tokens, signed URLs, storage paths, transcripts or user identifiers.

Stop for missing protocol/capability evidence, any silent audio, missing or wrong
captions, stuck device work, failed completion/playback, advertised-but-refused
format, new cloud work, unknown/nonempty drain state, auth/history/account
regression, OOM/restarts or sustained queue growth. Fix and rerun the first
affected case; unknown and skipped required cases are failures.

## 2. Enable device-only admission

Record the cutover timestamp in UTC, then enable admission while cloud
execution remains available for already-queued work:

```bash
date -u +%Y-%m-%dT%H:%M:%SZ
fly secrets set IOS_DEVICE_ONLY_MODE=true --app nova-video
```

Run these non-mutating boundary probes. The fake Bearer value is deliberate;
the admission middleware answers before authentication, while the admitted
protocol-2 request continues to normal auth and returns 401:

```bash
base=https://nova-video.fly.dev

curl -sS -o /tmp/kria-web-retired.json -w '%{http_code}\n' \
  -X POST "$base/creation-threads"
# expect 410 and problem.code == web_creation_retired

curl -sS -o /tmp/kria-native-upgrade.json -w '%{http_code}\n' \
  -X POST -H 'Authorization: Bearer invalid-cutover-probe' \
  "$base/creation-threads"
# expect 426 and problem.code == native_update_required

curl -sS -o /tmp/kria-native-current.json -w '%{http_code}\n' \
  -X POST -H 'Authorization: Bearer invalid-cutover-probe' \
  -H 'X-Kria-Client-Protocol: 2' "$base/creation-threads"
# expect 401: admission passed and normal authentication rejected the fake token

curl -sS -o /tmp/kria-cloud-only.json -w '%{http_code}\n' \
  -X POST -H 'Authorization: Bearer invalid-cutover-probe' \
  -H 'X-Kria-Client-Protocol: 2' "$base/generative-jobs"
# expect 422 and problem.code == device_render_unsupported
```

Inspect the files locally with `jq . /tmp/kria-*.json`; do not attach them to a
public ticket because trace IDs are operational metadata.

Repeat the entire applicable matrix from the step-1 hold point with the signed
build. Require an `ios_device_only_admission` receipt with `client_protocol=2`
for each admitted flow. Exercise D12 against an existing cloud output and prove
that the original still plays after the typed mutation refusal. Keep the private
evidence record complete; do not proceed on generic montage-only evidence.

## 3. Drain legacy cloud work

Repeat the three Celery inspection commands and queue snapshot from step 0.
The authoritative broker condition is:

- every queue in `RENDER_WORKER_QUEUES` has depth zero;
- no worker has active or reserved work on one of those queues; and
- no scheduled render message remains.

Also audit database intent using the code's canonical device predicate. Open a
shell on the `light` Machine, then paste the script with the recorded admission
cutover timestamp:

```bash
fly ssh console -a nova-video -g light
export CUTOVER=2026-10-02T12:34:56+00:00  # replace with the recorded UTC value
python - <<'PY'
import os
from datetime import datetime
from sqlalchemy import select

from app.database import sync_session
from app.kria.media_sources import is_device_render_job
from app.models import Job

CUTOVER = datetime.fromisoformat(os.environ["CUTOVER"])
ACTIVE = {"queued", "processing", "matching", "rendering", "posting"}

with sync_session() as db:
    recent = list(db.scalars(select(Job).where(Job.created_at >= CUTOVER)))
    new_cloud = [job for job in recent if not is_device_render_job(job)]
    active_cloud = list(db.scalars(select(Job).where(Job.status.in_(ACTIVE))))
    active_cloud = [job for job in active_cloud if not is_device_render_job(job)]

print("new_cloud_since_cutover", [(str(j.id), j.status) for j in new_cloud])
print("active_cloud_rows", [(str(j.id), j.status) for j in active_cloud])
PY
exit
```

Replace the example with the recorded cutover. The first list
must be empty. For the second list, every recent row must be terminal before
continuing. An old stale row may predate this rollout and have no broker task;
record and diagnose it rather than deleting or rewriting it. Broker inspection,
worker heartbeat, and the admin job-debug view must agree that it is not live.

Only after every required signed-device canary and the broker/database drain
agree, turn off cloud execution:

```bash
fly secrets set CLOUD_RENDER_EXECUTION_ENABLED=false --app nova-video
```

Repeat the queue and database audits. A cloud task published by a race must be
terminalized as `processing_failed` with `failure_reason=cloud_render_disabled`;
it must not begin FFmpeg, Skia, provider generation, or upload. Device recipe
jobs must still progress to `awaiting_device`. Repeat D3 (Subtitled) and D7
(recorded voiceover) all the way to `published` and relaunch playback. Do not
continue if a new cloud Job appears after the admission timestamp.

## 4. Release 1 twenty-four-hour acceptance

Keep the temporary five-group topology throughout this gate. Do not merge or
deploy the topology reduction while Release 1 is still being qualified.

Watch continuously for the first hour, then at least hourly through 24 hours:

- Fly cost allocation and exact Machine counts/states;
- API and worker cold-start latency;
- queue depth, oldest-message age, active/reserved tasks, and wake failures;
- worker RSS, OOM exits, task timeouts, and unexpected restarts;
- device recipe latency, `awaiting_device` age, upload/complete failures, and
  published playback;
- structured `ios_device_only_admission` counts grouped by `code` (especially
  `web_creation_retired`, `native_update_required`, and
  `device_render_unsupported`); and
- `cloud_render_publish_blocked` / `generative_cloud_render_blocked` events.

Break device results down by Subtitled, multi-clip Talking, narrated, guided
voiceover, generic voiceover, protocol update, unsupported recipe, upload,
completion and playback. Require zero unexplained silent-audio, missing-caption,
cloud-start, OOM or stuck-device incidents. Mark Plan 025 DONE only when this
entire window and all signed-device evidence pass.

Hold or roll back immediately on any OOM, cloud work beginning after the kill
switch, unavailable auth/playback/account deletion, missing device completion,
or sustained queue growth. API memory pressure returns to 2 GB; worker memory
pressure returns to a larger VM before considering separate always-on workers.

## 5. Release 2 topology reduction (after Plan 025 is DONE)

The final `worker` `-Q` list must keep `visuals-analysis` next to
`autoplace-jobs`: once `VISUALS_ANALYSIS_QUEUE=visuals-analysis` is set,
creator-facing Visuals analysis is published there
(`docs/pipelines/clip-understanding.md`). `tests/test_worker_prewarm_gate.py`
fails if a process consumes one of the two queues without the other.

While the admission release still defines the old process groups, scale the
drained dedicated groups to zero. This avoids depending on `fly deploy` to
infer removal of groups that no longer exist in its target config:

```bash
fly scale count \
  api=1 worker=1 light=1 autoplace=0 speech_analysis=0 \
  --app nova-video --yes
fly scale show --app nova-video
```

Only now merge/deploy the second release containing the final `fly.toml` and
strict verifier. The normal Fly Deploy workflow must pass. Its contract is
exact:

- managed groups are only `api`, `worker`, and `light`;
- `light` is started;
- an API may be stopped only when its service advertises Fly autostop;
- a stopped worker is accepted only when its latest exit is requested,
  non-OOM, and non-restarting; and
- the public `/health` probe wakes an autostopped API before success.

Inspect the final counts and inventory after the deploy:

```bash
fly scale show --app nova-video
fly machine list --app nova-video --json | jq -r \
  '.[] | [(.config.metadata.fly_process_group // "unmanaged"), .state, .id] | @tsv'
```

The managed inventory must contain one Machine for each of `api`, `worker`, and
`light`, and none for `autoplace` or `speech_analysis`. Do not remove the
unmanaged production mutation guard; the deployment workflow owns it.

Exercise wake and scale-to-zero behavior:

1. `curl --fail --retry 5 https://nova-video.fly.dev/health` wakes the API.
2. Publish one bounded device recipe or analysis canary. The worker wake hook
   starts the stopped worker and the task completes.
3. Verify both `autoplace-jobs` and `speech-analysis` publication wake the same
   worker during controlled canaries.
4. After every media queue remains idle for five minutes, the lifecycle task
   requests a worker stop. Its latest exit must be requested, not OOM, and not
   restarting.
5. Leave a harmless controlled queue message only in a staging rehearsal; the
   two-minute Beat backstop must recover a deliberately missed wake. Do not
   manufacture stuck production work merely to test the backstop.

## Rollback

Restore capacity before reopening cloud admission. Use the last known-good
pre-topology revision/config and its VM sizes; then restore process counts:

```bash
fly scale count \
  api=2 worker=1 light=1 autoplace=1 speech_analysis=1 \
  --app nova-video --yes
fly scale show --app nova-video
```

Once the old consumers are started and healthy, reverse the flags in this
order:

```bash
fly secrets set CLOUD_RENDER_EXECUTION_ENABLED=true --app nova-video
fly secrets set IOS_DEVICE_ONLY_MODE=false --app nova-video
```

Re-run queue inspection, `/health`, authenticated project/playback, and one
bounded render canary. Restoring `CLOUD_RENDER_EXECUTION_ENABLED` first prevents
admitting legacy work into an executor that still refuses it. No database or
storage restoration is required because the cutover never deletes existing
rows or objects.
