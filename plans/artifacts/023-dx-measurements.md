# Kria developer-loop measurements

Baseline recorded locally on 2026-09-07 from a prepared worktree with warm
dependencies. These are engineering feedback-loop measurements, not production
latency SLOs.

| Loop | Command | Wall time | Result |
|---|---|---:|---|
| Whole-turn happy replay | `.venv/bin/python -m app.cli.kria_replay nermin-matcha-update` | 0.16s | PASS |
| Contract/tool-add drift check | `.venv/bin/python -m app.cli.kria_contracts --check` | 0.15s | PASS |
| Injected ambiguous-dispatch diagnosis | `.venv/bin/python -m app.cli.kria_replay ambiguous-render-dispatch --json` | 0.15s | PASS |
| Focused Kria gate | `make verify-kria` | 2.30s | PASS, 155 passed, 1 environment skip, 0 retries |

The tool-add path is one strict argument model, one strict result model, one
registry entry/executor, one replay/test fixture, then the generated contract
check. Drift fails before wider tests. The diagnosis path exposes snapshot,
plan, policy, receipts, and the recovery message without model, storage, broker,
or renderer credentials.

Re-record this table quarterly and after a material registry/replay expansion.
The target remains under 60 seconds for replay and the focused gate, and under
10 minutes from a thread/turn identity to a safe operator action. Production
queue, model, and render latency are tracked separately and remain rollout gates.
