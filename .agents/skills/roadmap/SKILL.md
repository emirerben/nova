---
name: roadmap
description: |
  Feature roadmap and idea intake for Kria in Linear. Use whenever Emir shares a new feature idea, product idea, "we should build X", a pasted list of ideas/feedback to track, asks to triage the backlog, asks "what should we build next", "where are we on the roadmap", "weekly review", or wants an idea saved so it is not lost. Files each idea in the right place in Linear (new Backlog project for a feature, issue in an existing project for anything smaller), with project, labels and priority set. Never writes to TODOS.md (frozen).
allowed-tools:
  - Read
  - Grep
  - Glob
  - Bash
  - AskUserQuestion
---

# /roadmap: Linear as the feature roadmap

Read `docs/runbooks/linear-workflow.md` first. It owns the model (feature = Project, task = Issue), the project list, the label rules and the weekly review. This skill is the procedure.

Linear tools (Linear MCP): `list_projects`, `list_issues` (use `query`), `save_project`, `save_issue`, `save_status_update`, `list_issue_labels`, `list_project_labels`.
Team is always `Kria`.

Modes: `idea`, `triage`, `review`. Pick from the request; ask only if truly ambiguous.

## Mode: idea

Input: one idea or a pasted list. Split a paste into atomic ideas (one per feature or per PR-sized task).

For each idea:

1. **Search for duplicates.** `list_issues` with `query` using 2-3 key nouns, and `list_projects` with `query`. If a close match exists, say so and comment on it (`save_comment`) instead of filing.
2. **Decide the shape** (runbook "Filing rules"):
   - A feature, meaning a new outcome that needs its own plan or several PRs: **new Project**.
   - Anything smaller: **Issue** in the best-fit existing project.
3. **Write it so someone can pick it up cold.** Short, plain language:
   - Project description: Problem, Outcome, Who it is for, Why now (or "not urgent"), Open questions.
   - Issue description: What / Why / How (name real files if you checked) / Effort / Done when.
   - Look at the code only enough to name the right files. Do not design the solution.
4. **Create it:**
   - Project: `save_project` with `addTeams: ["Kria"]`, `lead: "me"`, `state: "backlog"`, `summary` (max 255 chars), `labels` = one `Theme` label (Creation, Editing, AI quality, Platform, Growth) plus a `Stage` label (Stage 1 to 4) if it serves a roadmap stage.
   - Issue: `save_issue` with `team: "Kria"`, `project`, exactly one `Domain` label (AI, iOS, Editing, Web, Content, Other) and one type label (Bug, Improvement, Feature), `priority`, and `parentId` if it is a follow-up inside a train. Leave `state` at Backlog unless Emir says to start now.
5. **Report back** in a short list: each item, what you created (link), and anything you assumed. Do not ask for approval before creating Backlog items; the cost of a wrong guess is a quick move in Linear.

If Emir pastes feedback from a user rather than his own idea, keep the user's words in the description and note the source.

## Mode: triage

1. `list_issues` for non-Done issues; find ones with no project, no `Domain` label, or no type label.
2. `list_projects` with `state: "backlog"`; find projects with no description, no `Theme` label, or no `Stage` label where one clearly applies.
3. Propose fixes in one table, apply the clear ones, ask about the rest in a single `AskUserQuestion`.
4. Never change status, assignee or priority on issues assigned to someone else. Project and labels only.
5. Flag: parent issues that are Done while children are open, and `Rollout:` issues older than 14 days.

## Mode: review (weekly)

1. `list_projects` (do not request milestones; the query is too heavy). For each In Progress project, `list_issues` with `project` and `state` filters to count open, in progress, and in review.
2. Report grouped by Theme: project, status, open count, stale In Progress (no update in 7 days), open `Rollout:` issues.
3. Post one short `save_status_update` (type `project`) per In Progress project: what moved this week, what is blocked, health (`onTrack`, `atRisk`, `offTrack`). Base it only on Linear data and merged PRs you can see; do not invent progress.
4. List Backlog projects and suggest at most three to start next, with a one-line reason each.
5. Keep the whole report under one screen.

## Rules

- Linear is the only tracker. Do not write to `TODOS.md`.
- One project per issue. No issue without a project.
- Do not file into `IOS App` (retired catch-all).
- Wording for Emir: plain language, short, outcome first.
