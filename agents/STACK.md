# Kria architecture

This describes the checked-in architecture, not the active production rollout.
For effective settings and deployed-state evidence, use the
[agent navigation runbook](../docs/runbooks/agent-navigation.md#runtime-evidence).
For entrypoints and focused tests, use the [navigation map](../docs/README.md).

| Area | Implementation and source of truth |
| --- | --- |
| Native client | SwiftUI + AVFoundation, iOS 18; `src/apps/ios/`. Build and module boundaries: [iOS development](../docs/runbooks/ios-development.md). |
| Web and admin | Next.js + React + TypeScript; `src/apps/web/`. Dependency versions/scripts: [package.json](../src/apps/web/package.json). Vercel serves the web app. |
| API | FastAPI + PostgreSQL; `src/apps/api/app/`. Dependencies: [pyproject.toml](../src/apps/api/pyproject.toml). Fly serves the API. |
| Agent runtime | `app/kria/`, with HTTP/task adapters in `routes/kria_runtime.py` and `tasks/kria_runtime.py`; [runtime architecture](../docs/pipelines/kria-agent-runtime.md). |
| Media execution | Native `KriaMediaEngine` plus server FFmpeg/Skia pipelines. Availability is gated; [phone rendering](../docs/runbooks/phone-rendering.md) and [device-only cutover](../docs/runbooks/ios-device-only-runtime.md) describe qualification and release stages. |
| Background work | Celery + Redis; task routing/concurrency and machine sizing are defined in [worker.py](../src/apps/api/app/worker.py) and [fly.toml](../fly.toml). |
| Object storage | GCS/S3 abstraction in [storage.py](../src/apps/api/app/storage.py). Configured retention rules: [gcs-lifecycle.json](../infra/gcs-lifecycle.json); confirm applied bucket configuration before claiming live retention. Raw media and outputs stay outside Git. |

Use subprocess FFmpeg for server video work; `VideoFileClip` buffers too much
media in memory. See [video context](VIDEO_CONTEXT.md) for rendering patterns.

## Local development

`scripts/dev-auto.sh` starts database/Redis containers, runs migrations, and
launches API, workers, and Next.js natively with hot reload. Follow
[README quick start](../README.md#quick-start) and the
[cwd-safe check commands](../docs/runbooks/agent-navigation.md#focused-checks).
Full-stack Docker Compose is an alternative, not the default dev workflow.
