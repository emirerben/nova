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

## Weekly review (`/roadmap review`)

- Group projects by Theme; show status, open issue count, stale In Progress issues (no update in 7 days) and open `Rollout:` issues.
- Post a short `save_status_update` on every project that is In Progress.
- Suggest at most three next features from the Backlog projects, with one-line reasons.

## Things Linear's API cannot do (set up once in the UI)

Saved views: **Roadmap** (Projects, timeline, grouped by Theme), **Ideas inbox** (Projects, status Backlog), **Unhomed** (Issues with no project).
