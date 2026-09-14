# Codex agent routing (KRI-17)

Project configuration lives in `.codex/config.toml`, with role overlays in
`.codex/agents/`. Land this configuration on main before expecting newly created
worktrees to contain it. Start a fresh Codex task in a trusted checkout to load it;
existing tasks do not automatically acquire instructions added after they started.
Explicit user/session model choices take precedence over the Astra Low parent default.

This is instruction-driven task classification, not an automatic runtime router.
The named-role configuration supplies defaults only when that spawn interface uses
those roles. Desktop collaboration requires the explicit fallback below.

| Role | Model | Effort | Work |
| --- | --- | --- | --- |
| `default`, `cheap_worker` | `gpt-5.6-luna` | medium | Isolated UI/CSS, copy, mechanical edits, obvious local bugs, simple tests |
| `explorer` | `gpt-5.6-luna` | medium | Bounded read-only investigation to clarify scope |
| `implementer` | `gpt-5.6-terra` | medium | Normal features, multi-file state/data flow, moderate debugging |
| `debugger` | `gpt-6-astra` | medium | Ambiguous root causes, architecture, risky cross-system analysis |

## Handoff and escalation

Routine delegation is authorized by the project's early routing instructions.
Choose the cheapest capable role based on evidence; use an explorer when one
bounded investigation will resolve a classification unknown. Start normal features
at Terra and clearly ambiguous analysis at Astra without artificial lower-tier retries.
Do trivial one-step work locally when a handoff would cost more than it saves.

Each handoff includes objective, working directory, file ownership, acceptance
criteria, existing findings, prior attempts, focused checks, and read/write boundaries.
Workers return findings, changed files, attempted fixes, test results, and a specific
blocker or escalation reason. Workers do not recursively delegate.

The parent owns classification, integration, architecture, and final high-risk review.
Escalate Luna → Terra → Astra when scope exceeds the worker's role or two corrective
attempts yield no new evidence. A failing test with an understood local cause stays
with its worker. Missing access or unavailable tooling is an environment blocker,
not justification to upgrade models. Preserve findings on escalation.

Reuse workers and concise summaries. Parallelize only independent work within the
runtime's available slots, with isolated worktrees or disjoint file ownership.
Serialize edits to shared files. Use subagents rather than new user-owned tasks
unless the user explicitly requests a new task.

## Desktop fallback: explicit model selection

Before calling `collaboration.spawn_agent`, read the selected role's TOML file and
include its `developer_instructions` in the handoff. Pass all three fields explicitly:

```json
{
  "task_name": "bounded_exploration",
  "model": "gpt-5.6-luna",
  "reasoning_effort": "medium",
  "fork_turns": "none",
  "message": "Self-contained objective, directory, ownership, acceptance criteria, findings, checks, and role instructions."
}
```

Replace the model with the table's exact ID for the selected role. Full-history
forks cannot accept overrides in this interface. An omitted model inherits the
parent: `.codex/config.toml`'s generic `default` does not guarantee a Luna child
through the desktop collaboration API. Record the selected role/model and rationale
in the handoff; verify the actual child's model from runtime metadata when available.
Never claim cheaper delegation from a worker's self-reported model alone.

Reuse the same worker for routine corrective attempts. If overrides or a selected
model are unavailable, report the limitation and retain the task with the parent;
do not silently create an expensive substitute. Runtime restrictions take precedence.
The explorer's named role enforces `sandbox_mode="read-only"`; fallback read-only
instructions are behavioral constraints, not a separate sandbox. If enforced isolation
is needed, use a runtime that supports a read-only role sandbox.

## Verification and delivery

Run `python3 scripts/check_codex_agent_routing.py` for static configuration checks
(also run by the scoped Codex agent routing CI workflow)
and `python3 scripts/check_codex_agent_routing.py --runtime` for strict loading and
fresh prompt visibility with the installed CLI (Python 3.11+ required). Runtime
checks use a temporary trusted config home and do not alter global settings or call
models. This proves isolated CLI loading, not that the current desktop task reloaded.

Exercise actual desktop delegation using bounded read-only tasks: mechanical work
→ Luna Medium; normal implementation analysis → Terra Medium; ambiguous debugging
→ Astra Medium. Inspect child metadata and spawn arguments. Follow up with a routine
local test failure and verify the same Luna worker handles it without escalation.
Explicit overrides prove the fallback; they do not prove autonomous classification
or named-role spawning. To validate autonomous classification after landing, record
the fresh checkout SHA, give ordinary task prompts without role/model hints, keep
expected routes outside those prompts, and inspect actual child models/effort.
Named-role execution likewise needs an actual named-role spawn and child metadata.
Until those checks run, report these two capabilities as unverified. Record runtime
limitations in the validation report.

Run `bash scripts/check_claude_md_size.sh`, `git diff --check`, and
`bash scripts/preship-check.sh`. Commit config, instructions, and validation together,
push, and create a draft PR. A local smoke test without landing the files does not
activate routing for future worktrees. No application deployment is required.

## Agentic workflow (how to work fast here)

- **Default to subagents, not new sessions.** Spawn a subagent (Agent tool) per heavy subtask from ONE orchestrating session. Each subagent burns its own context window and returns only a summary. Do NOT open a new session per subtask.
- **Parallelize independent subtasks** in one message (multiple Agent calls). Use `isolation: "worktree"` on subagents that edit files in parallel.
- **For batchable work** across N items, run the decompose workflow: `Workflow({ scriptPath: ".claude/workflows/decompose.js", args: { subtasks: [{title, prompt}, ...] } })`. Running ANY workflow needs explicit opt-in — include the word "workflow" in the request.
- **Prefer gbrain over grep for semantic lookups.** `gbrain search "<intent>"`, `gbrain code-def <symbol>`, `gbrain code-callers <symbol>`. Grep is still right for exact strings and regex.
- **Only start a new session when** the work is genuinely unrelated, or after a deliberate `/context-save` → `/context-restore` handoff.
- **Project skills** live in `.agents/skills/`: `/improve`, `/motion-dev`, `/transitions-dev`, `/verify-editor-timeline`. Read each `SKILL.md` for its trigger and gate; external versions are pinned in `skills-lock.json`.


## Validation record — 2026-09-13

Tested from a fresh worktree based on `3eb36b5ab` using Codex CLI 0.140.0.
No application files or production settings changed.

- Static validator and strict isolated runtime loading passed for all registrations
  and overlays. Fresh CLI prompt contained the early delegation authorization and
  explicit desktop fallback. The CLI's `debug` command rejects `--strict-config`,
  so strict checks use `app-server`; prompt visibility uses `debug prompt-input`.
- Actual desktop children were observed in runtime metadata and turn contexts:

| Bounded task | Observed model | Effort | Context handoff |
| --- | --- | --- | --- |
| Mechanical config/path verification | `gpt-5.6-luna` | medium | `none` |
| Multi-file integration analysis | `gpt-5.6-terra` | medium | `none` |
| Ambiguous cross-runtime failure analysis | `gpt-6-astra` | medium | `none` |

- The same Luna worker handled a deliberate in-memory filename assertion failure
  (`cheap_worker.toml` versus `cheap-worker.toml`), diagnosed the typo, and passed
  the corrected assertion without escalation. Both recorded turns used Luna Medium.
- Negative fixtures rejected a missing role file, a costlier mechanical-worker
  model, a writable explorer sandbox, and missing delegation authorization.
- Scoped Ruff, the 38,000-byte documentation guard, diff checks, and preship checks
  passed. The main instruction file was 37,844 bytes at validation.

These are real explicit-override desktop checks, not automatic-classification or
named-role-spawn evidence. Those remain unverified until tested with ordinary
unlabelled prompts in a fresh post-landing task. The desktop model metadata is the
selection evidence; the CLI's bundled catalog is not proof of desktop availability.
CLI role checks verify configuration values, not that a named-role spawn applies them.
