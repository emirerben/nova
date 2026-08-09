# Kria

AI-powered tool that transforms raw real-life videos into viral-ready short-form content (TikTok, Reels, YouTube Shorts).

## Quick start

```bash
cp .env.example .env    # fill in your values
docker-compose up        # starts web + api + worker + redis + db
```

- Frontend: http://localhost:3000
- API: http://localhost:8000

## Structure

```
src/apps/web/   — Next.js frontend
src/apps/api/   — Python FastAPI + Celery
agents/         — agent context (read before working on video processing)
docs/           — pipeline internals, runbooks, specs, designs (start at docs/pipelines/)
CLAUDE.md       — working agreements, invariants, key paths, env vars
DESIGN.md       — design-system tokens, loading rules, anti-slop rules, a11y baseline
TODOS.md        — deferred work backlog, grouped by the PR that deferred it
```

## Features

- **Template mode** — drop your clips into a viral template; Gemini analyzes each clip and matches it to the right slot
- **Music beat-sync** — browse a music gallery, pick a song, upload clips; every cut lands on a detected beat (`/music`)
- **Creator Blocks** — add, customize, time, and ask Nova to edit eight deterministic animated text and image blocks in the video editor ([runtime and rollout guide](docs/pipelines/motion-runtime.md))
- **TikTok publishing beta** — connect TikTok, publish an approved final render, and sync public performance behind server-side rollout gates ([operator runbook](docs/runbooks/tiktok-direct-publishing.md))
- **Admin tools** — upload music tracks (YouTube/SoundCloud via yt-dlp), monitor beat analysis, publish/archive (`/admin/music`)

## Branch conventions

- `main` — protected, requires PR + 1 approval
- `dev` — integration branch
- `{initials}/{feature-slug}` — feature branches (e.g. `ee/upload-endpoint`)

## Cofounder setup

```bash
bash setup-cofounder.sh
```
