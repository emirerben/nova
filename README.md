# Nova

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
```

## Features

- **Template mode** — drop your clips into a viral template; Gemini analyzes each clip and matches it to the right slot
- **Music beat-sync** — browse a music gallery, pick a song, upload clips; every cut lands on a detected beat (`/music`)
- **Admin tools** — upload music tracks (YouTube/SoundCloud via yt-dlp), monitor beat analysis, publish/archive (`/admin/music`)

## Branch conventions

- `main` — protected, requires PR + 1 approval
- `dev` — integration branch
- `{initials}/{feature-slug}` — feature branches (e.g. `ee/upload-endpoint`)

## Cofounder setup

```bash
bash setup-cofounder.sh
```
