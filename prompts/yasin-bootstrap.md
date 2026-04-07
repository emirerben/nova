# Yasin — Nova template work bootstrap prompt

Paste everything below this line into a fresh Claude Code session. Claude will set up an isolated git worktree, install dependencies, start the dev environment with hot reload, and get you oriented on the template pipeline. You shouldn't have to manage terminals manually — Claude handles it.

---

You are Claude Code helping **Yasin (ybyesilyurt)** improve Nova's template pipeline and add new templates. Nova is at `github.com/emirerben/nova`.

## Step 1 — Create an isolated worktree

Check if `~/projects/nova` exists. If it does NOT:

```bash
mkdir -p ~/projects
git clone git@github.com:emirerben/nova.git ~/projects/nova
```

Then create a fresh worktree on a new feature branch, isolated from main. Use a branch name like `yasin/template-work-$(date +%Y%m%d)` so multiple sessions don't collide:

```bash
cd ~/projects/nova
git fetch origin
BRANCH="yasin/template-work-$(date +%Y%m%d)"
git worktree add -b "$BRANCH" ../nova-$BRANCH origin/main
cd ../nova-$BRANCH
```

All further work happens in `../nova-$BRANCH`. The main checkout at `~/projects/nova` stays clean.

## Step 2 — Environment setup

Copy the env file from the main checkout (it has the secrets already filled in):

```bash
cp ~/projects/nova/.env .env
```

If that file doesn't exist yet, ask the user (Yasin) to provide it — do not invent values.

Install API dependencies into a venv:

```bash
cd src/apps/api
python3 -m venv .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -e ".[dev]"
cd ../../..
```

Install web dependencies:

```bash
(cd src/apps/web && npm install)
```

## Step 3 — Start the dev environment with hot reload

Start everything via the dev-auto script. Run it in the background so you can keep working; the script itself exits quickly once all services are launched:

```bash
./scripts/dev-auto.sh
```

Then verify the services are up:

```bash
sleep 5
curl -sf http://localhost:8000/health && echo ""
tail -5 .dev/api.log
tail -5 .dev/worker.log
tail -5 .dev/web.log
```

If any of those fail, read the relevant log file in full and fix the issue before continuing. Common problems:
- `.env` missing required keys (GEMINI_API_KEY, GOOGLE_APPLICATION_CREDENTIALS, STORAGE_BUCKET)
- Docker not running (redis/postgres won't start)
- Port 3000 or 8000 already in use by another process (the script frees them, but kill leftover `next dev` or `uvicorn` if needed)
- Migrations failed — check `.dev/migrate.log`

## Step 4 — Read the mission brief

Read these files in order, then summarize the mission back to Yasin in 3 bullets:

1. `agents/YASIN_CONTEXT.md` — mission + automation rules (**this is your most important file — it tells you how to manage the dev env**)
2. `agents/VIDEO_CONTEXT.md` — FFmpeg patterns, anti-patterns
3. `CLAUDE.md` — project overview, especially the "Template pipeline" section

## Step 5 — Automation rules you must follow for this project

These come from `agents/YASIN_CONTEXT.md`, repeated here so you don't forget:

- **Never restart servers manually.** uvicorn has `--reload` and the Celery worker has `watchfiles`. All Python edits auto-reload. Next.js has HMR. Telling Yasin to restart something is wrong.
- **When Yasin asks "does it work" / "why isn't it working", read logs first.** Never speculate. Tail `.dev/api.log`, `.dev/worker.log`, `.dev/web.log`.
- **Gemini prompt files are plain text** (`src/apps/api/prompts/*.txt`). Editing them needs no restart — the next `analyze_template` call picks them up.
- **Never use MoviePy / VideoFileClip.** Subprocess ffmpeg only. See `agents/VIDEO_CONTEXT.md`.
- **Never merge to main.** Push to the feature branch only. Emil reviews.
- **Never bump VERSION or edit CHANGELOG.md.** Emil handles releases.
- **If the dev env is broken**, run `./scripts/dev-stop.sh && ./scripts/dev-auto.sh`. Do not kill individual processes.

## Step 6 — Ready to work

Tell Yasin you're set up and list the two workflows from `YASIN_CONTEXT.md`:

- **Workflow A** — improve an existing template (edit pipeline code, re-run a test job, compare)
- **Workflow B** — add a new template (upload reference video, trigger analyze_template, validate recipe, test)

Then ask which one he wants to start with, or whether he has a specific template quality issue in mind.
