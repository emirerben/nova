#!/usr/bin/env bash
# scripts/brain/refresh-nova.sh — keep Nova's company brain fresh.
#
# Replaces the machine-local ~/.gbrain/refresh-nova.sh.
# Install via the launchd plist at scripts/brain/com.nova.gbrain-refresh.plist.
# Safe to run by hand at any time.
#
# What it does (in order):
#   1. Scoped code sync (incremental, no git-pull, handles BLOCKED state)
#   2. Refresh the todos concept page
#   3. Re-import docs/, agents/, and the Claude auto-memory dir (idempotent)
#   4. Incremental curated-memory ingest (learnings, timeline, reviews, retros)
#   5. Drain the gstack artifacts queue (push to github.com/emirerben/gstack-artifacts-emirerben)
#   6. Embed any stale chunks
#   7. Print a one-line status summary
#
# Machine-local prerequisites (never in the repo):
#   ~/.gbrain/config.json      — Supabase connection config
#   ~/.gbrain/supabase.env     — exports GBRAIN_DATABASE_URL + GBRAIN_DISABLE_DIRECT_POOL=1
#   ~/.bun/bin/gbrain           — the gbrain CLI
#   ~/.claude/skills/gstack/bin/gstack-memory-ingest.ts  — the JSONL→page converter
#   ~/.claude/skills/gstack/bin/gstack-brain-sync        — the artifacts queue drainer
#
# No-op-safe: exits 0 gracefully if gbrain isn't set up on this machine.
set -uo pipefail

# --- Setup ---
export PATH="$HOME/.bun/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:$PATH"

REPO="/Users/emirerben/Projects/nova"
LOG_DIR="$HOME/.gbrain/logs"
mkdir -p "$LOG_DIR"
LOG="$LOG_DIR/refresh-nova.log"
# Keep log bounded (last 500 lines)
if [ -f "$LOG" ]; then tail -n 500 "$LOG" > "$LOG.tmp" 2>/dev/null && mv "$LOG.tmp" "$LOG"; fi
exec >> "$LOG" 2>&1

echo "=== $(date '+%Y-%m-%d %H:%M:%S') refresh start ==="

# Guard: requires supabase.env (the Supabase connection pin)
if [ ! -f "$HOME/.gbrain/supabase.env" ]; then
  echo "no ~/.gbrain/supabase.env — gbrain not set up here, skipping."
  exit 0
fi
if ! command -v gbrain >/dev/null 2>&1; then
  echo "gbrain not on PATH, skipping."
  exit 0
fi
if [ ! -d "$REPO" ]; then
  echo "repo $REPO not found, skipping."
  exit 0
fi

# Pin the Supabase connection; keep Nova's DATABASE_URL out of gbrain's env.
source "$HOME/.gbrain/supabase.env"
unset DATABASE_URL

# Read the source ID from the repo's .gbrain-source pin (required for safe scoped sync)
SOURCE_ID_FILE="$REPO/.gbrain-source"
if [ ! -f "$SOURCE_ID_FILE" ]; then
  echo "ERROR: $SOURCE_ID_FILE not found — cannot safely scope the code sync. Aborting."
  exit 1
fi
SOURCE_ID=$(cat "$SOURCE_ID_FILE" | tr -d '[:space:]')
if [ -z "$SOURCE_ID" ]; then
  echo "ERROR: .gbrain-source is empty. Aborting."
  exit 1
fi

# --- Stage 1: Scoped incremental code sync ---
echo "--- [1/7] code sync (source=$SOURCE_ID, no-pull) ---"
SYNC_OUT=$(gbrain sync \
  --source "$SOURCE_ID" \
  --strategy code \
  --no-pull \
  --json 2>&1 || true)

# Check for BLOCKED state and acknowledge automatically (with a loud log entry)
if echo "$SYNC_OUT" | grep -q '"status":"blocked"\|BLOCKED'; then
  echo "WARN: sync is BLOCKED — a previous failure is unacknowledged. Auto-acknowledging with --skip-failed."
  echo "WARN: Check ~/.gbrain/sync-failures.jsonl and the gbrain doctor output to understand what was skipped."
  gbrain sync \
    --source "$SOURCE_ID" \
    --strategy code \
    --no-pull \
    --skip-failed \
    --json 2>&1 | tail -3 || true
else
  echo "$SYNC_OUT" | tail -3
fi

# --- Stage 2: Todos concept page ---
echo "--- [2/7] todos page ---"
if [ -f "$REPO/TODOS.md" ]; then
  # Run from ~ to target the default source (not the worktree-pinned code source)
  (cd ~ && gbrain put todos < "$REPO/TODOS.md" 2>&1 | head -3) || echo "todos put failed (non-fatal)"
else
  echo "TODOS.md absent"
fi

# --- Stage 3: Re-import docs, agents, Claude auto-memory (idempotent) ---
echo "--- [3/7] import docs/, agents/, claude-memory ---"
# Run from ~ so there is no .gbrain-source pin (imports target the default source)
CLAUDE_MEMORY_DIR="$HOME/.claude/projects/-Users-emirerben-Projects-nova/memory"

(cd ~ && \
  gbrain import "$REPO/docs" --source-id default --no-embed 2>&1 | tail -2 && \
  gbrain import "$REPO/agents" --source-id default --no-embed 2>&1 | tail -2 && \
  { [ -d "$CLAUDE_MEMORY_DIR" ] && \
    gbrain import "$CLAUDE_MEMORY_DIR" --source-id default --no-embed 2>&1 | tail -2 || \
    echo "claude memory dir not found (ok on other machines)"; } \
) || echo "import stage failed (non-fatal)"

# --- Stage 4: Incremental curated-memory ingest (learnings/timeline/reviews/retros) ---
echo "--- [4/7] memory ingest ---"
MEMORY_INGEST="$HOME/.claude/skills/gstack/bin/gstack-memory-ingest.ts"
if command -v bun >/dev/null 2>&1 && [ -f "$MEMORY_INGEST" ]; then
  # Run from ~ to avoid .gbrain-source pin routing to the code source
  (cd ~ && bun "$MEMORY_INGEST" \
    --incremental \
    --sources learning,timeline,review,retro,ceo-plan,design-doc \
    --quiet 2>&1 | tail -5) || echo "memory ingest failed (non-fatal)"
else
  echo "bun or gstack-memory-ingest not found, skipping."
fi

# --- Stage 5: Drain gstack artifacts queue ---
echo "--- [5/7] drain artifacts queue ---"
BRAIN_SYNC="$HOME/.claude/skills/gstack/bin/gstack-brain-sync"
if [ -f "$BRAIN_SYNC" ] && [ -d "$HOME/.gstack/.git" ]; then
  bash "$BRAIN_SYNC" --discover-new 2>&1 | tail -2 || true
  bash "$BRAIN_SYNC" --once 2>&1 | tail -3 || true
else
  echo "gstack-brain-sync or ~/.gstack git not found, skipping artifacts push."
fi

# --- Stage 6: Embed stale chunks ---
echo "--- [6/7] embed stale ---"
gbrain embed --stale 2>&1 | tail -3 || echo "embed failed (non-fatal)"

# --- Stage 7: One-line status summary ---
echo "--- [7/7] status ---"
STATS=$(gbrain stats --json 2>/dev/null || echo '{}')
PAGES=$(echo "$STATS" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('page_count', d.get('pages','?')))" 2>/dev/null || echo "?")
EMBD=$(echo "$STATS" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('embedded_count', d.get('embedded','?')))" 2>/dev/null || echo "?")
SYNC_TS=$(gbrain sources list --json 2>/dev/null | python3 -c "
import sys, json
sources = json.load(sys.stdin)
for s in sources:
    if s.get('id','').startswith('gstack-code'):
        print(s.get('last_sync','?')[:19])
        break
" 2>/dev/null || echo "?")
echo "DONE: pages=$PAGES embedded=$EMBD code-source-sync=$SYNC_TS"

echo "=== $(date '+%Y-%m-%d %H:%M:%S') refresh done ==="
