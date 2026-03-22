# Nova — Preliminary Stack

> Working hypothesis before ARCHITECTURE.md is written. Superseded by ARCHITECTURE.md once approved.

## Frontend
- **Next.js 14** + TypeScript + React
- Deployment target: Vercel (or Cloud Run)

## Backend (API)
- **Python FastAPI** — async-native, auto-generates OpenAPI docs, fast
- **Celery** — distributed task queue for video processing jobs
- Why FastAPI over Flask: async support critical for job status streaming

## Job Queue
- **Redis + Celery** — industry-proven for CPU-bound background workers
- Celery concurrency: 2 workers per node at MVP (FFmpeg is CPU-heavy)

## Storage
- **GCS** (preferred) or S3 — raw uploads + processed outputs only
- Retention policy: TBD (delete raw after 48h? keep processed indefinitely?)

## Database
- **PostgreSQL** — job metadata, user accounts, output history
- Why not Firebase: job queue patterns (polling, locking, retry counts) are SQL-native

## Video Processing
- **FFmpeg** via subprocess — direct, streaming, memory-efficient
- ❌ NOT MoviePy/VideoFileClip — buffers entire video to RAM

## Local Dev
- docker-compose: web + api + worker + redis + db
