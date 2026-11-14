# Linear workflow: features, ideas, and the roadmap

Linear (team **Kria**, key `KRI`) is the single tracker for features, ideas, bugs and follow-ups. `TODOS.md` is frozen (see the header in that file); do not add entries there.

Use the `/roadmap` skill (`.agents/skills/roadmap/SKILL.md`) to file ideas, triage, and run the weekly review. This page is the rule set the skill follows.

## Capacity limit (found 2026-10-04)

The Linear workspace is on a plan that caps active issues; the `TODOS.md` import hit `You've exceeded the free issue limit for this workspace` once and was finished after space was freed. If a create call fails with that error, stop and tell Emir. Do not archive or delete issues to make room, and do not silently fall back to `TODOS.md`.

## The model

| Thing | Linear object | Example |
| --- | --- | --- |
| A feature (anything that takes more than one PR or has its own outcome) | **Project** | `Slide & photo posts` |
| A task, bug or follow-up inside a feature | **Issue** in that project | `KRI-305` |
| A multi-PR train inside a feature | **Parent issue** with child issues, same project | `KRI-298` + lanes `KRI-299..304` |
| A strategic stage | **Milestone** on `Kria Creator AI Roadmap` | `1. Requested edit execution` |
| A feature idea nobody has started | **Project** in status `Backlog` | `Schedule posts to TikTok` |

The roadmap is the Projects view grouped by `Theme` or filtered by `Stage`. There is no separate roadmap file.

## Current feature projects

| Project | Theme | Stage | What it holds |
| --- | --- | --- | --- |
| Slide & photo posts | Creation | 1 | slide posts, capture facts, slide chat edit |
| Chat copilot editing | Editing | 1 | edit-by-chat, editor-op parity, receipts |
| Montage AI & request-following | AI quality | 1 | montage planner, brief, clip facts, prompt-following |
| Talking & captions | Editing | 1 | talk-to-camera, captions, speech cleanup |
| Voiceover videos | Creation | 1 | voiceover upload, trim, visuals, fit |
| On-device rendering | Platform | 1 | iPhone render, Plan 025 rollout, render reliability |
| iOS editor polish | Editing | 2 | cropping, animation, haptics, design passes |
| Evals & AI quality | AI quality | 1 | evals, harness, SFX library, clip intents |
| Dev platform & agent tooling | Platform | none | CI, flaky tests, agent workflow, this system |
| Kria Launch & Growth: October | Growth | none | 8 Oct launch, beta, TikTok integration |
| Kria Creator AI Roadmap | none | none | stage milestones 1 to 4 (strategy, not a feature) |

`IOS App` is the old catch-all. Do not file new issues there. It is closed after the 8 Oct launch.

## Filing rules

1. **Search first.** `list_issues` with a query, and `list_projects` with a query. Comment on an existing item instead of filing a duplicate.
2. **Pick the shape:**
   - New outcome that needs its own plan or several PRs: create a **Project** (`state: backlog`, one-paragraph problem and outcome in the description, `Theme` label, `Stage` label if it serves a roadmap stage).
   - Anything smaller: create an **Issue** in the matching project.
   - Cannot place it in 30 seconds: file the issue in `Evals & AI quality` or `Dev platform & agent tooling` only if it truly fits; otherwise create a Backlog project. Never leave an issue without a project.
3. **Every issue gets** a project, one `Domain` label (`AI`, `iOS`, `Editing`, `Web`, `Content`, `Other`; they are a single-select group, so only one) and one type label (`Bug`, `Improvement`, `Feature`). Add `Design` when it is a design task.
4. **Follow-ups inherit the parent's project.** Set `parentId` when the follow-up belongs to a train.
5. **Priority** follows `TODOS.md` conventions: Urgent = broken in prod or blocks a named launch; High = needed before a named event; Medium = planned work; Low = polish.
6. **Rollout-pending work** (code merged dark behind a flag): one issue titled `Rollout: <FLAG_NAME>` in the feature project, listing the flag, the Fly and Vercel steps, and the verification. Close it when the flag is on and verified in prod. This replaces tracking rollouts only in `plans/README.md`.
7. **Description format for engineering issues:** What / Why / How / Effort / Done when. Same content `TODOS.md` used.
8. Do not change status, assignee or priority on issues assigned to someone else. Comment instead.

## Status hygiene (unchanged, see `~/.claude/CLAUDE.md`)

In Progress on start, In Review when the PR opens, Done when the PR merges into `main`. A project is Completed when all its issues are Done or Canceled; post a status update first.

## Fix chains: the second-fix-PR rule

Trigger: a KRI ticket's **second `fix` PR has merged**. `scripts/preship-check.sh` step 8 and the `Fix chain` CI check (`.github/workflows/fix-chain.yml`, both running `scripts/ci/fix-chain-check.py`) **block** a third `fix` PR on that ticket until the list below exists.

How it counts:

- Merge-commit and rebase merges count once per PR, by the PR's own title from the `gh` index, never once per branch commit. A `fix: review nits` commit inside a `feat` PR is not a fix PR.
- Only merged PRs on `main` whose title starts with `fix` (`fix:`, `fix(scope):`, `hotfix`). `feat`, `test`, `chore`, `docs` and release-metadata PRs never count.
- A fix is charged to the **first** KRI id in its squash subject. A subject with no id is charged to the first id in its PR title or head branch name (PR index from `gh`). A child fix that names its parent second does not count against the parent.
- The PR being checked is a fix when its title starts with `fix` (locally: the open PR's title via `gh`, or `PR_TITLE=...`; before a PR exists, when a commit on the branch does). The ticket comes from the PR title, else the branch name.

Before a **third** fix PR on that ticket, write a failure-mode list in the PR body (or a changed `docs/` or `plans/` `.md` file); the ticket may keep a copy, but the gate does not read it. One line per way the feature can still fail, each either:

- names the test that already covers it, or
- is marked `uncovered - adding test X` (and X lands in the same PR).

Template:

```
Failure modes for KRI-<n> (after fix #2)
- <shape / input / state> -> <expected outcome> | covered by <test id>
- <shape / input / state> -> <expected outcome> | uncovered - adding <test id>
```

The gate accepts the block in the PR body, or in a `docs/` or `plans/` `.md` file changed by the PR. It needs the exact `Failure modes for KRI-<n>` heading for this ticket, at least two rows in the shape above, and no `<placeholder>` text left. The gate checks the shape, not whether the named test exists: reviewers read the rows.

Not a repeat of the same fault (umbrella ticket, unrelated typo)? Put one line in the PR body:

```
[skip-fix-chain] <one-line reason, at least 20 characters and 4 distinct words>
```

The marker must be on its own line in the PR body (not a commit message), short or placeholder reasons are rejected, and CI prints the reason in the job summary with a warning so the reviewer sees it. Using it to dodge the list on a real repeat is the failure this rule exists to stop; the reviewer rejects it.

Why: KRI-736 (editor Save) took five fix PRs in one day because each fix covered only the case that had just broken. A listed matrix, with new cases added as rows rather than new files, is the model (KRI-750). If the list cannot be written, the fix is not understood yet: stop and investigate before the third PR.

History: the list was a warning (KRI-751) and was written in 4 of 13 cases; KRI-1383 made it blocking.

## Weekly review (`/roadmap review`)

- Group projects by Theme; show status, open issue count, stale In Progress issues (no update in 7 days) and open `Rollout:` issues.
- Post a short `save_status_update` on every project that is In Progress.
- Suggest at most three next features from the Backlog projects, with one-line reasons.

## Things Linear's API cannot do (set up once in the UI)

Saved views: **Roadmap** (Projects, timeline, grouped by Theme), **Ideas inbox** (Projects, status Backlog), **Unhomed** (Issues with no project).
