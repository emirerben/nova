# Agent navigation and evidence

Use this when beginning an investigation, checking rollout claims, or handing
work to another coding session. Start from the [navigation map](../README.md)
for the symptom's source entrypoint and focused tests.

## Begin with the ticket and worktree

1. Read the matching Linear issue and relevant comments. Record acceptance
   criteria and existing PR/worktree links before opening broad repository searches.
2. For new implementation, follow the [worktree workflow](../../CLAUDE.md#session-workflow-isolate-in-a-worktree).
   Keep the selected worktree as the explicit cwd on every command. In-flight
   work continues in its existing worktree; verify branch and status first.
3. Read the relevant navigation-map row, then search within that subsystem.
   Expand repository-wide only when the entrypoint or caller evidence requires it.
4. Treat plans as intent and `agents/DECISIONS.md` as historical rationale.
   Current code and tests establish implemented behavior; live observations
   establish deployment state.

## Focused checks

The wrapper anchors itself to its own worktree, even when called from another
directory or through a symlink. It uses that worktree's `src/apps/api/.venv`
and starts Python from `src/apps/api`, so a shared editable install cannot
silently select the primary checkout's `app` package.

```bash
# From the selected worktree; test paths are relative to src/apps/api.
bash scripts/check-api.sh test tests/tasks/test_task_time_limits.py -q
bash scripts/check-api.sh lint check app/cli/runtime_profile.py
bash scripts/check-api.sh lint format --check app/cli/runtime_profile.py
bash scripts/check-api.sh profile

# From elsewhere, use the selected worktree's absolute script path.
bash /path/to/worktree/scripts/check-api.sh test tests/tasks/test_task_time_limits.py -q
```

If `.venv` is missing, follow `scripts/worktree-setup.sh` or create the API
environment described by the project setup. The setup script also runs
migrations. For web changes, run the `package.json` lint/test/typecheck commands
with cwd `src/apps/web`. Before a PR, run `bash scripts/preship-check.sh`.

## Runtime evidence

Keep these three claims distinct:

| Claim | Evidence |
| --- | --- |
| Default or implementation | Current commit, `app/config.py`, capability contracts, and focused tests. |
| Effective process settings | `python -m app.cli.runtime_profile` in the target environment. It reads that process's settings, exports boolean flags plus protocol/capability declarations, and labels the UTC capture time. |
| Observed production behavior | Deployed revision, process group/client build, UTC observation, and a relevant runtime/debug result. A local profile does not establish production state; an enabled flag does not prove a user is eligible. |

The profile uses no database or network and prints no credentials or account
allowlists. `--help` works without a configured environment. It is an on-demand
snapshot, not monitoring or deployment verification. API and worker processes
can differ; identify the process used for any production snapshot. Once the
revision containing the CLI is deployed, the existing Fly read-only workflow is:

```bash
fly ssh console -a nova-video -g api -C 'python -m app.cli.runtime_profile'
# Select worker or light instead when that process is the subject of investigation.
```

The command starts a fresh Python process using the selected Machine's environment;
it does not introspect the settings cached inside an already-running worker.

For job or chat investigation, use the existing read-only admin diagnostics
instead of ad hoc SQL or full model dumps:

```bash
python scripts/admin.py GET jobs/<job-id>/debug
python scripts/admin.py GET creation-threads/<thread-id>/turns
# Add --prod before GET only when investigating production.
```

See [admin job debug](admin-job-debug.md) and
[chat turn response contract](../../src/apps/api/app/routes/admin_creation_threads.py) for response contracts.
Debug responses can contain user text and media references. Summarize relevant
decision names and outcomes in tickets; keep transcripts, tokens, signed URLs,
and raw responses out of tracked files and shared handoffs.

When changing a default, capability contract, deployment stage, or entrypoint,
update the relevant runbook/map in the same PR. Date live-state claims and cite
the observation and revision; an undated “dark rollout” label is not evidence.

## Durable ticket handoff

Use this compact record in the authorized ticket update or PR. If ticket writes
were not requested, include it in the final response instead.

```text
Issue / acceptance criteria:
Plan / worktree / branch / base SHA:
Entrypoints and relevant symbols:
Implemented vs deployed evidence (UTC, revision, environment/process):
Changed files / PR:
Checks (exact commands, cwd, result):
Merge / deploy status and outstanding gates:
Remaining blocker or next action:
```

Link durable artifacts. A temporary `/tmp` output is not a completed handoff;
retain a safe summary in the issue or PR before ending the session.
