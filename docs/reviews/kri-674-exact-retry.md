# KRI-674: retry the approved edit

Before: iPhone Retry sent “Try generating this edit again.” That created a new
creator turn, re-analyzed clips and could fail in Main Creator before dispatch.

After: a typed request targets the failed execution. When its complete native
recipe is saved, fresh consent permits exactly that recipe to run again under a
new device revision. The turn, draft, clips, text, timing and audio stay fixed.
Concurrent taps share one pending approval; lost queue acknowledgement resumes
the accepted attempt once. Expired unstarted consent can be renewed, but newer
requests, changed source/draft/generation or ownership block stale recovery.
An accepted stale retry receipt is retired without changing the newer session.

The physical test also exposed a UI issue: a failed native Job can remain
`awaiting_device`, which the project projects as “rendering.” Its pending retry
approval must take precedence over that old state. Ordinary in-flight renders
still retain their existing precedence over unrelated new plans.

## Scope and evidence

The original KRI-673/KRI-674 job failed before a complete recipe was saved. It
cannot be replayed truthfully. That path now explains the limitation and asks
for a new version plus approval. Frozen cloud execution adapters and preparing
all specialist output before approval remain KRI-722/KRI-723 work.

Automated evidence: real HTTP, Postgres and normal approval/dispatch/observer
code; no paid model calls. A failing planner sentinel forbids accidental new
creative planning. Cases cover duplicates/concurrent taps, consent expiry,
lost dispatch acknowledgement, foreign owner, stale draft/request/generation,
unknown work, unsupported recipe, missing original and missing saved program.
Red controls observed the missing endpoint (404), misleading observer retry,
expired consent replay, wrong-generation recovery and stale accepted receipt.

Physical iPhone 13 Pro, iOS 27.0.1, 2026-10-17 05:08 UTC: a separate QA app
ran Retry → fresh approval → actual AVFoundation export → authenticated API
reservation → create-only upload → checksum/media-probe publication. The final
clean retry advanced revision 2 → 3, kept recipe digest
`16c3e9348e1a5634edeeb5812386d5e46464ff3c7616cb19401218c0a2eb3d96`,
retained one turn and recorded zero AgentRun rows across the dedicated database.
A stale revision-2 completion returned 409 and preserved the published output.
The normal runtime completion observer was invoked manually in place of its
production timer. No production account/DB or cloud-storage writes occurred.

The controlled fixture used two recent original account videos: flag then scarf,
2.5 seconds each, original audio and only “Gameday.” It retained the accepted
1.6-second Kria ending: actual H.264/AAC 1920×1080, 30 fps, **6.600 seconds**.
Frames were inspected; the first five seconds compared to the earlier native
export at SSIM 0.992945 and audio correlation 0.9999913 (240,000 mono samples).
This establishes matching content/windows/audio, not byte-identical encoding.
The recipe itself is exactly equal. Published MP4 SHA-256:
`0b4869bb9d524dabacb1b61f829e8d2a17a5746791f9e31249b51036eff92643`.

QA setup: isolated local API/Postgres/Redis/worker plus copied full-resolution
originals. DEBUG explicitly permits HTTP uploads only with the scratch-API flag,
matching private host/port and `/kri674-storage/` prefix; Release remains HTTPS.
The final build stayed installed throughout the clean attempt. Earlier fixture
setup runs were discarded after an app reinstall changed an upload fingerprint;
the normal immutable-byte fence correctly refused that upload.

Checks: 145 focused API tests; 44 workspace XCTest cases; 512 mapped preship
incident cases; 65 gate-manifest checks; mobile/OpenAPI contract drift check;
web TypeScript check; signed native build and physical UI journeys. All passed.
No live-model quality claim or production deployment is implied.
Private source videos, full payloads and result bundles remain outside git.

Repeatable focused checks:

```bash
REDIS_URL=redis://127.0.0.1:16474/0 bash scripts/check-api.sh test \
  tests/kria/test_render_retry_postgres.py \
  tests/kria/test_runtime_v2.py tests/kria/test_runtime_tasks.py \
  tests/routes/test_kria_runtime.py tests/kria/test_stale_and_expired_approvals.py \
  tests/routes/test_lock_order.py -q
```

Use a disposable Redis and the worktree scratch DB. Keep physical QA in a
separate database: other integration tests intentionally truncate test tables.
