---
name: autoship
description: |
  End-to-end ship pipeline. Reviews the plan, implements it, reviews the diff,
  ships the PR, and lands + deploys — auto-deciding routine choices with the
  same 6 principles /autoplan uses. One human-blocking approval gate before
  the PR merges. Use when asked to "autoship", "ship it end-to-end", "do the
  whole thing", "full ship", or "run the entire pipeline".
allowed-tools:
  - Bash
  - Read
  - Write
  - Edit
  - Glob
  - Grep
  - Agent
  - AskUserQuestion
  - WebSearch
triggers:
  - autoship
  - auto ship
  - full ship
  - end to end ship
  - ship it end to end
  - do the whole thing
---

## Preamble (run first)

```bash
_UPD=$(~/.claude/skills/gstack/bin/gstack-update-check 2>/dev/null || .claude/skills/gstack/bin/gstack-update-check 2>/dev/null || true)
[ -n "$_UPD" ] && echo "$_UPD" || true
mkdir -p ~/.gstack/sessions
touch ~/.gstack/sessions/"$PPID"
_BRANCH=$(git branch --show-current 2>/dev/null || echo "unknown")
echo "BRANCH: $_BRANCH"
_BASE=$(git symbolic-ref refs/remotes/origin/HEAD 2>/dev/null | sed 's|refs/remotes/origin/||')
[ -z "$_BASE" ] && _BASE=$(git rev-parse --abbrev-ref @{u} 2>/dev/null | sed 's|origin/||')
[ -z "$_BASE" ] && _BASE=main
echo "BASE: $_BASE"
_DIFF_LINES=$(git diff --stat "origin/$_BASE"...HEAD 2>/dev/null | tail -1 || echo "no-diff")
echo "DIFF_STAT: $_DIFF_LINES"
_HAS_PR=$(gh pr view --json url -q .url 2>/dev/null || echo "")
echo "EXISTING_PR: ${_HAS_PR:-none}"
_SESSION_ID="$$-$(date +%s)"
echo "SESSION_ID: $_SESSION_ID"
mkdir -p ~/.gstack/analytics
echo '{"skill":"autoship","ts":"'$(date -u +%Y-%m-%dT%H:%M:%SZ)'","repo":"'$(basename "$(git rev-parse --show-toplevel 2>/dev/null)" 2>/dev/null || echo "unknown")'"}' >> ~/.gstack/analytics/skill-usage.jsonl 2>/dev/null || true
```

---

# /autoship — End-to-End Ship Pipeline

One command. Plan in (or in-conversation), shipped + deployed PR out.

`/autoship` reads each downstream skill file from disk and follows it at full depth —
same rigor, same sections, same methodology as running each skill manually. The only
difference: intermediate `AskUserQuestion` calls are auto-decided using the 6 principles
below. Only **two** points are human-blocking:

1. **User Challenges** during plan review (both Claude and Codex think your direction
   should change).
2. **The pre-merge approval gate** between ship and land — last chance to bail before
   the PR merges to main and deploys.

Everything else auto-decides. This skill never silently auto-merges or auto-deploys
without that gate.

---

## The 6 Decision Principles

Copied verbatim from `/autoplan`. These auto-answer every intermediate question:

1. **Choose completeness** — Ship the whole thing. Pick the approach that covers more edge cases.
2. **Boil lakes** — Fix everything in the blast radius (files modified by this plan + direct importers). Auto-approve expansions that are in blast radius AND < 1 day CC effort (< 5 files, no new infra).
3. **Pragmatic** — If two options fix the same thing, pick the cleaner one. 5 seconds choosing, not 5 minutes.
4. **DRY** — Duplicates existing functionality? Reject. Reuse what exists.
5. **Explicit over clever** — 10-line obvious fix > 200-line abstraction. Pick what a new contributor reads in 30 seconds.
6. **Bias toward action** — Merge > review cycles > stale deliberation. Flag concerns but don't block.

**Conflict tiebreakers per phase:**
- Plan Review: P5 (explicit) + P3 (pragmatic) dominate.
- Diff Review: P3 (pragmatic) + P6 (bias toward action) dominate. Block on correctness, allow non-blocking findings.
- Ship: P6 (bias toward action) dominates. Default to "yes" on routine ship questions (VERSION bump, CHANGELOG entry, base branch).
- Land & Deploy: P6 dominates pre-CI, P1 (completeness) dominates post-deploy verification.

---

## Decision Classification

Every auto-decision is classified the same way as `/autoplan`:

- **Mechanical** — one clearly right answer. Auto-decide silently. (e.g. "run tests", "bump VERSION", "open PR against the detected base.")
- **Taste** — reasonable people could disagree. Auto-decide with a recommendation, but log to the audit trail and surface at the final approval gate if non-trivial.
- **User Challenge** — both models (Claude + Codex when invoked) agree your stated direction should change. NEVER auto-decided. Surface immediately with the User-Challenge framing from `/autoplan` (what you said / what models recommend / why / what context we might be missing / if we're wrong, the cost).
- **Blocker** — a finding that makes the change unsafe to ship (security hole, broken migration, failing test). Stops the pipeline at the phase it surfaced in.

---

## Sequential Execution — MANDATORY

Phases MUST execute in strict order: Plan Review → Implement → Diff Review → Ship → Approval Gate → Land & Deploy.

Each phase MUST complete fully before the next begins. Between phases, emit a one-paragraph transition summary stating what the prior phase produced. Never run phases in parallel.

If a phase emits a **Blocker**, STOP. Do not advance. Surface to the user with: what was found, which file, why it blocks, recommended fix. Wait for user direction.

---

## Phase 0: Intake + Restore Point

### Step 1: Detect entry mode

`/autoship` can be invoked at three points:

- **Mode A — Plan in hand**: a plan file exists at `~/.claude/plans/<slug>.md` and no code has been written yet. Run all six phases.
- **Mode B — Code already written**: the working tree has staged/unstaged diff against the base branch. Skip Phase 1 + 2; start at Phase 3 (Diff Review).
- **Mode C — PR already open**: `gh pr view` returns a URL on this branch. Skip Phases 1–4; start at Phase 5 (Approval Gate).

Detect via the preamble outputs (`DIFF_STAT`, `EXISTING_PR`) plus `ls ~/.claude/plans/*.md 2>/dev/null`. If ambiguous (e.g. both a plan AND a diff exist), ask the user once which mode they want and remember the answer for the session.

### Step 2: Capture restore point

Before doing anything destructive, snapshot the workspace:

```bash
eval "$(~/.claude/skills/gstack/bin/gstack-slug 2>/dev/null)" && mkdir -p ~/.gstack/projects/$SLUG
BRANCH=$(git rev-parse --abbrev-ref HEAD 2>/dev/null | tr '/' '-')
DATETIME=$(date +%Y%m%d-%H%M%S)
RESTORE_PATH="$HOME/.gstack/projects/$SLUG/${BRANCH}-autoship-restore-${DATETIME}.md"
echo "RESTORE_PATH=$RESTORE_PATH"
{
  echo "# /autoship Restore Point"
  echo "Captured: $DATETIME | Branch: $BRANCH | Commit: $(git rev-parse --short HEAD)"
  echo ""
  echo "## How to recover"
  echo "1. \`git checkout $BRANCH\`"
  echo "2. \`git reset --hard $(git rev-parse HEAD)\`"
  echo "3. If a plan file was modified, copy 'Original Plan State' below back to it."
  echo ""
  echo "## Status at capture"
  git status --short
  echo ""
  echo "## Diff stat vs base"
  git diff --stat "origin/$_BASE"...HEAD 2>/dev/null || echo "(no diff)"
} > "$RESTORE_PATH"
```

If a plan file is in play (Mode A), append its full verbatim contents to the restore file under `## Original Plan State`.

### Step 3: Load skill files from disk

Read each skill file you'll execute, in order. Use the `Read` tool — do NOT call them via the `Skill` tool (they would re-enter the harness with no shared state and re-ask routine questions).

- Mode A only: `~/.claude/skills/gstack/plan-eng-review/SKILL.md`
- All modes that reach Phase 3: `~/.claude/skills/gstack/review/SKILL.md`
- All modes that reach Phase 4: `~/.claude/skills/gstack/ship/SKILL.md`
- All modes that reach Phase 6: `~/.claude/skills/gstack/land-and-deploy/SKILL.md`

**Section skip list — when following a loaded skill file, SKIP these (already handled by `/autoship`):**

- Preamble (run first) — `/autoship`'s preamble already fired.
- AskUserQuestion Format / question-tuning preamble.
- Update-check, telemetry, session tracking.
- Step 0: Detect base branch — already captured as `$_BASE`.
- Any "BENEFITS_FROM" / prerequisite-skill offer (we're committed to the pipeline).
- Plan-mode entry/exit dance (handled below in Phase 1.5).

Follow ONLY the review-specific methodology and required outputs of each loaded skill.

Output: `Mode: [A/B/C]. Plan: [path or 'none']. Diff: [stat]. PR: [url or 'none']. Loaded skills. Starting pipeline.`

---

## Phase 1: Plan Review (Mode A only)

**Skip if Mode B or C.**

Execute `plan-eng-review`'s loaded instructions against the plan file. Apply the 6 principles to every `AskUserQuestion` the skill would have asked, except:

- **Premise questions** (Phase 1 of plan-eng-review) — ask normally. You can't auto-pick the user's problem statement.
- **User Challenges** — surface immediately per the framing in the Decision Classification section above.

Write the plan-review output (architecture diagram, test plan, failure modes, etc.) into the plan file as plan-eng-review's instructions specify.

### Phase 1.5: Plan-mode exit

If `/autoship` was invoked inside plan mode (which is normal — the user is reviewing a plan), Phase 1 ends by:

1. Verifying the plan file now contains plan-eng-review's required outputs (architecture, tests, failure modes — see plan-eng-review's "Required outputs" checklist).
2. Calling `ExitPlanMode`.

`ExitPlanMode` is the user's approval of the reviewed plan. From here, code-writing tools become available and Phase 2 begins.

---

## Phase 2: Implement

**Skip if Mode B or C.**

Implement the plan. Use `TaskCreate` to mirror the plan's steps, mark each `in_progress` when started and `completed` when finished. Follow Nova's CLAUDE.md rules (worktree etiquette, no MoviePy, encoder policy, etc.) — they apply here exactly as in a manual session.

When implementation is complete:

1. Run the project's quality checks. For Nova:
   ```bash
   (cd src/apps/api && ruff check . && ruff format --check .)
   (cd src/apps/api && pytest -x)
   (cd src/apps/web && npm run lint)
   (cd src/apps/web && npx tsc --noEmit)
   ```
   (For other repos, run whatever `CLAUDE.md` documents under "Quality checks".)
2. If any check fails, fix and re-run. Do NOT advance to Phase 3 with red checks.
3. If a check failure reveals the plan was wrong, loop back: update the plan file with the correction and continue. Don't silently diverge from the plan.

**Stop condition:** if implementation reveals the plan is structurally wrong (not just a bug — actually the wrong design), stop. Surface to the user: "Implementation revealed [X]. The plan as written can't ship cleanly. Options: [revise plan] / [scope down] / [abort]." This is a Blocker, not auto-decided.

---

## Phase 3: Diff Review

Execute `review`'s loaded instructions against `git diff origin/$_BASE...HEAD`. Apply the 6 principles to every issue surfaced:

- **Correctness bugs** (SQL injection, broken migration, race condition, missing await, leak) → **Blocker**. Stop. Surface and wait.
- **Style / readability** → auto-fix if < 5-line change and within blast radius (P2 + P5). Otherwise note in the audit trail and continue.
- **Test gaps** → auto-add a test if the gap is in a directly-modified file (P1 + P5). Otherwise log as `TODOS.md` deferral.
- **Performance concerns** without a measurement → log + continue (P6). Don't pre-optimize.

Re-run the project's quality checks one more time before advancing. CI is going to run them anyway; catching it locally is cheaper than waiting for GitHub.

Output: "Diff review complete. [N] findings ([M] auto-fixed, [K] deferred, [J] blockers). Advancing to ship."

---

## Phase 4: Ship

Execute `ship`'s loaded instructions. Auto-decide all of:

- **Base branch detection** — `$_BASE` from preamble.
- **VERSION bump size** (patch/minor/major) — apply ship's own logic; default to patch for non-feature work, minor for new features, major never auto-decided.
- **CHANGELOG entry** — generate from commit messages + diff summary.
- **PR title** — auto-derive from the plan title (Mode A) or first commit subject (Modes B/C).
- **PR body** — use ship's template; auto-fill the test plan from Phase 3's quality-check output.

**Do NOT auto-decide:**
- A VERSION major bump — surface as a User Challenge with the framing "this is a breaking change; here's the diff that justifies major; confirm or downgrade?"
- A PR against any branch other than `$_BASE` — confirm with the user.

Output: "PR opened: [url]. Ready for approval gate."

---

## Phase 5: Approval Gate — the only human-blocking gate

**STOP here. Do not advance without explicit user input.**

Present:

```
## /autoship — Ready to Land

### PR
[url]

### Summary
[1-2 sentence summary of what changed]

### Phase Recap
- Plan Review: [N] decisions ([M] auto, [K] taste, [J] challenges) — see audit trail
- Implementation: [N] tasks completed, [M] quality checks green
- Diff Review: [N] findings ([M] auto-fixed, [K] deferred, [J] blockers — 0 blockers required to reach here)
- Ship: VERSION bumped [old → new], CHANGELOG updated, PR opened

### Taste decisions surfaced for your review
[For each non-trivial taste decision across all phases, formatted per /autoplan's gate]

### Deferred to TODOS.md
[Items deferred during diff review]

### Next: Land & Deploy
- Wait for CI on [url]
- Merge (squash, per CLAUDE.md)
- Wait for Fly.io deploy
- Run canary checks against https://nova-video.fly.dev/health
```

Then ask via `AskUserQuestion`:

- **A) Land now** — proceed to Phase 6.
- **B) Wait for human review first** — pause; exit cleanly; user resumes later by saying "land it" or invoking `/land-and-deploy`.
- **C) Revise** — there's something in the PR they want changed before landing.
- **D) Cancel** — abandon the pipeline. Leave the PR open; user decides what to do with it.

This gate is non-negotiable. Do not skip it even if the user previously said "fully unattended" in conversation — that's a separate `/autoship --yolo` mode (not yet built).

---

## Phase 6: Land & Deploy

Only reached if the user picked **A) Land now** in Phase 5.

Execute `land-and-deploy`'s loaded instructions. Auto-decide all of:

- **Merge method** — squash (Nova's CLAUDE.md mandates squash).
- **Wait for CI** — yes, always. Don't merge red.
- **Delete branch after merge** — yes (Nova convention).
- **Run canary after deploy** — yes.

**Do NOT auto-decide:**
- A merge against a failing required check — surface as a Blocker.
- A canary that reports degraded production health post-deploy — surface immediately; do not declare the pipeline complete.

If everything goes green, output the final summary:

```
## /autoship — Complete ✓
PR: [url] (merged)
Deploy: [version] live on [production url]
Canary: [pass/warn/fail with details]
Restore point (if you need to undo): [RESTORE_PATH]
```

If the canary warns, leave the pipeline in a non-success state and offer follow-up options (rollback / hotfix / investigate).

---

## What auto-decide does NOT do

Same exceptions as `/autoplan`:

1. **Replace your judgment on the problem** — premises in Phase 1 require human input.
2. **Replace your judgment on direction changes** — User Challenges always surface.
3. **Bypass correctness blockers** — security holes, broken migrations, failing tests stop the pipeline.
4. **Skip the approval gate** — Phase 5 is unconditional.

Auto-decide replaces your judgment on **routine** choices, not on **safety** choices.

---

## Failure modes — what to do if a phase blows up

- **Plan review can't find the plan file** → ask user for path; offer to read the most recent file under `~/.claude/plans/`.
- **Implementation diverges from plan** → loop back to plan; don't silently diverge.
- **Quality checks fail** → fix and re-run; do not advance.
- **Diff review finds a blocker** → stop, surface, wait. Do not open a PR with a known security issue.
- **`gh pr create` fails** (auth, branch protection, etc.) → surface the gh error verbatim; do not retry blindly.
- **CI fails in Phase 6** → do NOT merge. Surface failing-check logs; user decides whether to fix or cancel.
- **Deploy fails or canary degrades** → leave PR merged, surface immediately; offer rollback path. Do not pretend success.

In every failure case: the restore point captured in Phase 0 is the recovery path.

---

## Re-entry

`/autoship` is idempotent at phase boundaries:

- Mode B and Mode C detection let you re-invoke after a partial run.
- A second invocation while a PR is already open jumps to Phase 5.
- A second invocation after merge sees no diff and exits cleanly with "nothing to do."

If you need to retry a specific phase only, invoke that phase's underlying gstack skill directly (`/review`, `/ship`, `/land-and-deploy`) — `/autoship` is the chain, not a replacement for the individual skills.

---

## Telemetry (run last)

```bash
echo '{"skill":"autoship","ts":"'$(date -u +%Y-%m-%dT%H:%M:%SZ)'","outcome":"'${_OUTCOME:-complete}'","session":"'$_SESSION_ID'"}' >> ~/.gstack/analytics/skill-usage.jsonl 2>/dev/null || true
```
