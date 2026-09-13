# Agent-run Slack recap

KRI-49 records successful agent-run finishes locally and can send one small Slack recap on weekdays at 06:00 local time. It is opt-in: installation writes the LaunchAgent file but does not load it.

## Lifecycle reporting

Nova’s checked-in Codex Desktop and Claude Code hooks call the same reporter on
`UserPromptSubmit` and `Stop`. Start a run with a ticket in its **initial**
prompt, for example `KRI-49: validate automatic run reporting`. The reporter
ignores sessions whose first prompt lacks a `KRI-xx` key, even if a later prompt
mentions one. A normal tracked run posts one start and one finish entry; the
finish supplies elapsed duration and outcome.

To smoke-test both integrations after adding the webhook, start and finish one
Codex Desktop run and one Claude Code run with an initial `KRI-49` prompt, then
confirm four metadata-only entries in private `#agent-runs`.

## Set up

From the Nova checkout, run:

```bash
bash scripts/hooks/install-agent-runs.sh
```

The installer creates `~/.nova/agent-runs/` with mode `700`, scaffolds `~/.nova/agent-runs.env` with mode `600`, and renders `~/Library/LaunchAgents/com.nova.agent-runs-recap.plist`. Add a Slack incoming-webhook URL locally:

```bash
NOVA_AGENT_RUNS_WEBHOOK_URL='https://hooks.slack.com/services/...'
```

Keep that file out of git and do not put the webhook in shell history, tickets, or agent prompts. A non-default config path is supported with `NOVA_AGENT_RUNS_ENV_FILE`; a non-default state directory is supported with `NOVA_AGENT_RUNS_STATE_DIR`.

## Validate, then enable

First run the recap manually after an agent run has been recorded:

```bash
bash scripts/hooks/agent-runs-recap.sh
```

For a deterministic local-date test, set `NOVA_AGENT_RUNS_RECAP_DATE=YYYY-MM-DD` for that invocation. A successful post creates `~/.nova/agent-runs/recap-YYYY-MM-DD.sent`; repeated executions for that date do not post again. Failed Slack delivery does not write a marker, so a later run can retry.

After manual validation, enable the weekday timer:

```bash
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.nova.agent-runs-recap.plist
launchctl print gui/$(id -u)/com.nova.agent-runs-recap
```

Disable it without deleting configuration:

```bash
launchctl bootout gui/$(id -u)/com.nova.agent-runs-recap
```

To remove the local feature, disable it first, then remove `~/Library/LaunchAgents/com.nova.agent-runs-recap.plist`, `~/.nova/agent-runs.env`, and (only if no longer wanted) `~/.nova/agent-runs/`. The last directory contains local completion metadata and idempotency markers.

## Privacy and failure behavior

The ledger is local at `~/.nova/agent-runs/completed-runs.jsonl`. Inclusion in that ledger means a finish was reported successfully; its `outcome` describes the run (`completed`, `failed`, or `interrupted`). The recap includes every ledger row whose `finished_at` falls on the local calendar date. Slack receives only founder, tool, ticket identifier, elapsed duration, outcome, and the total count. It never receives prompts, transcripts, command output, file paths, source video details, or raw ledger JSON.

The config is sourced only when it is owned by and readable by the current user. Missing configuration, an empty ledger, malformed local records, or a Slack failure are fail-open: they do not interrupt agent-run reporting. Check `~/.nova/logs/launchd-agent-runs-recap.err.log` if a scheduled recap is missing.
