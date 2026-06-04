# Nova Company Brain — Architecture, Ops, and Onboarding

The "company brain" is a **gbrain** (v0.40+) instance backed by Supabase Postgres. It makes the full codebase, engineering decisions, past learnings, and curated knowledge searchable by Claude agents, `/investigate`, `/plan-eng-review`, and similar gstack skills.

## Architecture and data flows

```
  ┌──────────────────────────────────────────────────────────────────────┐
  │  SUPABASE (aws-eu-west-2 — shared Postgres for all team members)     │
  │                                                                      │
  │   source: gstack-code-nova-*         source: default (federated)     │
  │   ┌─────────────────────────┐        ┌───────────────────────────┐  │
  │   │  932 code pages (today) │        │  71 curated pages:        │  │
  │   │  All .py .ts .tsx files │        │  • 36 Claude auto-memory  │  │
  │   │  + tests + scripts      │        │  • 14 docs/ pipeline docs │  │
  │   │  gbrain code-def, refs, │        │  • 4  agents/ context     │  │
  │   │  callers, callees work  │        │  • 1  TODOS concept       │  │
  │   │  against this source    │        │  • 1  learning batch      │  │
  │   └──────────┬──────────────┘        │  • 1  timeline batch      │  │
  │              │                       │  • 1  design-doc          │  │
  │              │                       └─────────────┬─────────────┘  │
  └──────────────┼─────────────────────────────────────┼────────────────┘
                 │                                     │
    ┌────────────▼──────────────┐         ┌────────────▼──────────────┐
    │  gbrain sync              │         │  gbrain import / put      │
    │  --strategy code --no-pull│         │  (from ~ to avoid pin)    │
    │  Twice daily via launchd  │         │  docs/, agents/,          │
    │  (see Stage 1 in script)  │         │  ~/.claude/.../memory/,   │
    └───────────────────────────┘         │  gstack-memory-ingest     │
                                          │  (learnings/timeline/     │
                                          │   reviews/retros)         │
                                          └───────────────────────────┘

  Writers (day-to-day):
    /learn, /review, /ship → ~/.gstack/projects/emirerben-nova/*.jsonl
    gstack-brain-sync --once → pushes to github.com/emirerben/gstack-artifacts-emirerben
    Claude auto-memory → ~/.claude/projects/-Users-emirerben-Projects-nova/memory/*.md
```

### The `.gbrain-source` worktree pin
Every worktree created by `scripts/new-session.sh` gets a `.gbrain-source` file containing the code-source ID (e.g. `gstack-code-nova-05af17bf`). This file pins `gbrain` commands run from inside that directory to the code source. **Important consequences:**
- `gbrain code-def`, `code-refs`, `code-callers`, `code-callees` work correctly from any worktree.
- `gbrain search` and `gbrain query` from inside a worktree only search the code source. To include docs, memory, or learnings, pass `--source default` explicitly:
  ```bash
  gbrain search "lyric stacking" --source default
  gbrain query "what did we decide about the CFR invariant"   # code source only
  gbrain query "what did we decide about the CFR invariant" --source default
  ```
- All machine-local refresh operations (`gbrain put`, `gbrain import`, `gstack-memory-ingest`) that target the `default` source must be run from `~` (home dir, no pin) or pass `--source-id default` explicitly.

## Refresh script

The canonical refresh job lives at `scripts/brain/refresh-nova.sh`. It is version-controlled and should replace any machine-local `~/.gbrain/refresh-nova.sh`.

**7-stage pipeline (run in order):**
1. Scoped code sync (`--strategy code --no-pull`) — safe, never git-pulls, never touches other sources
2. Todos concept page update
3. Idempotent import of `docs/`, `agents/`, and Claude auto-memory dir
4. Incremental curated-memory ingest via `gstack-memory-ingest` (learnings, timeline, reviews, retros)
5. Drain the gstack artifacts queue (`gstack-brain-sync`) → pushes to the private artifacts GitHub repo
6. Embed stale chunks
7. One-line status summary

### Install (one-time per machine)
```bash
# Copy the template plist and load it
cp scripts/brain/com.nova.gbrain-refresh.plist ~/Library/LaunchAgents/
# Edit the ProgramArguments path if your checkout isn't at ~/Projects/nova
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.nova.gbrain-refresh.plist
# Verify
launchctl list | grep nova.gbrain-refresh

# Run manually to verify it works
bash scripts/brain/refresh-nova.sh
```

### Update (when repo script changes)
```bash
launchctl bootout gui/$(id -u)/com.nova.gbrain-refresh
cp scripts/brain/com.nova.gbrain-refresh.plist ~/Library/LaunchAgents/
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.nova.gbrain-refresh.plist
```

## Ops: common tasks

### Unblock a frozen code sync
The sync bookmark freezes when a file fails to parse (check `~/.gbrain/sync-failures.jsonl` for the cause). The refresh script auto-acknowledges and logs a WARN; to fix manually:
```bash
source ~/.gbrain/supabase.env; unset DATABASE_URL
# See what failed
cat ~/.gbrain/sync-failures.jsonl | tail -3

# Acknowledge and advance the bookmark (after fixing or deciding to skip the offending file)
SOURCE_ID=$(cat /Users/emirerben/Projects/nova/.gbrain-source)
gbrain sync --source "$SOURCE_ID" --strategy code --no-pull --skip-failed

# Verify the doctor warning is gone
gbrain doctor --fast
```

**NEVER run `gbrain sync --repo` without `--source` and `--strategy code`.** Without explicit scoping, the default sync strategy treats code pages as un-syncable and **deletes them**. Always scope:
```bash
gbrain sync --source "$(cat .gbrain-source)" --strategy code --no-pull
```

### Re-sync the full code index (e.g. after a long pause)
```bash
source ~/.gbrain/supabase.env; unset DATABASE_URL
SOURCE_ID=$(cat /Users/emirerben/Projects/nova/.gbrain-source)
gbrain sync --source "$SOURCE_ID" --strategy code --no-pull --json
gbrain sources list  # verify timestamp updated
```

### Verify brain health
```bash
source ~/.gbrain/supabase.env; unset DATABASE_URL
gbrain doctor --fast          # health score + warnings
gbrain stats                  # page/chunk/type breakdown
gbrain sources list           # source IDs + last-sync timestamps

# Test: can we find a known engineering memo from inside the repo?
cd /Users/emirerben/Projects/nova
gbrain search "lyric stacking recurring bug" --source default
# should return the claude-memory/lyric-stacking-recurring-bug page

# Test: can we find code symbols?
gbrain code-def inject_lyric_overlays
```

### Manual one-off imports
```bash
source ~/.gbrain/supabase.env; unset DATABASE_URL
cd ~  # required — avoids the .gbrain-source pin routing to the code source

# Import a docs directory
gbrain import /Users/emirerben/Projects/nova/docs --source-id default

# Import a specific markdown file as a page
gbrain put my-page-slug < /path/to/file.md

# Ingest the Claude auto-memory dir (idempotent)
gbrain import /Users/emirerben/.claude/projects/-Users-emirerben-Projects-nova/memory \
  --source-id default

# Run the full memory ingest (learnings + timeline + reviews + retros)
bun ~/.claude/skills/gstack/bin/gstack-memory-ingest.ts \
  --incremental --sources learning,timeline,review,retro,ceo-plan,design-doc
```

### Drain the artifacts queue manually
```bash
source ~/.gbrain/supabase.env; unset DATABASE_URL
bash ~/.claude/skills/gstack/bin/gstack-brain-sync --discover-new
bash ~/.claude/skills/gstack/bin/gstack-brain-sync --once
# Verify
cat ~/.gstack/.brain-last-push   # should show a recent timestamp
cat ~/.gstack/.brain-sync-status.json
```

### Pooler `CONNECTION_ENDED` write-back warnings
`[last-retrieved] write-back failed (best-effort): write CONNECTION_ENDED ...pooler.supabase.com:5432`
This is a benign best-effort write to the session-pooler's Supabase connection. Search results are not affected. If it becomes frequent, check the Supabase dashboard for connection pressure.

---

## Cofounder (Yasin) onboarding — connecting a second machine

The brain is shared: Emir and Yasin both connect to the **same Supabase Postgres** instance. No data is duplicated; both machines read from the same 1000+ pages.

### Prerequisites
```bash
# 1. Install gbrain (requires bun)
curl -fsSL https://bun.sh/install | bash
bun install -g gbrain

# 2. Create the machine-local config (credentials shared out-of-band via 1Password/Signal)
mkdir -p ~/.gbrain
# Ask Emir for the contents of ~/.gbrain/config.json and ~/.gbrain/supabase.env
# They contain the Supabase connection string + OpenAI key for embeddings.
# NEVER commit these files — they contain secrets.

# config.json shape (fill in real values from Emir):
cat > ~/.gbrain/config.json << 'EOF'
{
  "engine": "postgres",
  "embedding_model": "openai:text-embedding-3-large",
  "embedding_dimensions": 1536,
  "expansion_model": "openai:gpt-5.2",
  "chat_model": "openai:gpt-5.2",
  "database_url": "FILL_IN_FROM_EMIR"
}
EOF
chmod 600 ~/.gbrain/config.json

# supabase.env shape:
cat > ~/.gbrain/supabase.env << 'EOF'
export GBRAIN_DATABASE_URL="FILL_IN_FROM_EMIR"
export GBRAIN_DISABLE_DIRECT_POOL=1
EOF
chmod 600 ~/.gbrain/supabase.env

# Add to ~/.zshrc so gbrain always finds the right DB in a Nova shell:
echo 'source ~/.gbrain/supabase.env && unset DATABASE_URL' >> ~/.zshrc
source ~/.zshrc
```

### Verify connectivity
```bash
source ~/.gbrain/supabase.env; unset DATABASE_URL
gbrain doctor --fast      # health check
gbrain stats              # should show ~1000 pages
gbrain search "lyric injector"   # should return code results
```

### Register the MCP server
```bash
# Add to Claude Code's user MCP config:
claude mcp add gbrain -- gbrain serve
# Verify
claude mcp list | grep gbrain
```

### Get the .gbrain-source pin for new worktrees
The `scripts/new-session.sh` script automatically copies the `.gbrain-source` file into new worktrees. On a second machine, the file will point to the same source ID (`gstack-code-nova-*`) because the Supabase brain is shared. No extra setup needed once connectivity is verified.

### Trigger a full code sync on the new machine
```bash
source ~/.gbrain/supabase.env; unset DATABASE_URL
# Register the source (first time only)
SOURCE_ID=$(cat /path/to/nova/.gbrain-source)
gbrain sources add "$SOURCE_ID" --path /path/to/nova

# Sync
gbrain sync --source "$SOURCE_ID" --strategy code --no-pull
```

### Alternative: remote-MCP (no secrets on the second machine)
If Yasin prefers not to store the Supabase URL locally, gbrain can run as an HTTP MCP server on Emir's machine and Yasin's Claude connects remotely:
```bash
# On Emir's machine (ensure ~/.gbrain/supabase.env is sourced):
source ~/.gbrain/supabase.env; unset DATABASE_URL
gbrain serve --http --port 7777 --public-url https://your-tunnel-url

# On Yasin's machine — add to MCP config:
# "url": "https://your-tunnel-url"
```
This is the no-credentials-second-machine path. Set up a Cloudflare Tunnel or ngrok for the public URL.

---

## Known gaps and deferred work

| Gap | Status |
|-----|--------|
| Review ledger slug-normalization produces duplicate pages (`feat-X` vs `featX`) | Upstream gstack bug; pages are ingestible, just doubled. Merge manually or wait for upstream fix. |
| `skillpack-harvest` resolver routing miss (7× in gbrain doctor) | gstack RESOLVER.md issue, not brain data. Low priority. |
| Nightly quality probe (gbrain autopilot feature) | Opt-in; enable with `gbrain config set autopilot.nightly_quality_probe.enabled true` |
| gbrain Timeline stat shows 0 | Stats count refers to brain-internal timeline events (via `gbrain timeline-add`), not pages of type `timeline`. The 17 timeline entries exist as a `timeline`-type page; this is not a bug. |
| Yasin remote-MCP setup | Documented above; implement when Yasin's machine is provisioned. |
