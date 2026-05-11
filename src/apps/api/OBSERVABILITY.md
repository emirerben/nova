# Observability — Langfuse tracing for LLM agents

Optional layer that posts one Langfuse trace per `Agent.run()` invocation. Sits next to the existing `structlog "agent_run"` event — same metadata, plus prompt/output capture and a session-id (`Job.id`) so all agents called for one Job cluster together in the Langfuse UI.

**Fail-open:** if `LANGFUSE_PUBLIC_KEY` + `LANGFUSE_SECRET_KEY` aren't set, OR the `langfuse` package isn't installed, every trace call is a no-op. Agent runs are never blocked or broken by tracing.

## Why

The existing eval harness (`tests/evals/`) covers offline quality: replay + judge + structural checks. It doesn't help with:

- **What's happening in prod?** — every job runs 6+ agents on real user uploads; today only structlog captures it.
- **Per-job cost** — `AgentSpec.cost_per_1k_*` is known; per-job total is logged but not aggregated anywhere queryable.
- **Trace-to-fixture pipeline** — pull any prod call into the eval harness as a new fixture with one click.
- **Non-engineer scoring** — let someone tag a clip's `platform_copy` as "bad hook" from the Langfuse UI without writing code.

Langfuse fills that gap. The harness stays in charge of CI gates; Langfuse adds prod visibility.

## Install + configure

```bash
# Install the optional dep
cd src/apps/api && .venv/bin/pip install -e ".[observability]"

# Configure for prod (Fly)
fly secrets set -a nova-video \
  LANGFUSE_PUBLIC_KEY="pk-lf-..." \
  LANGFUSE_SECRET_KEY="sk-lf-..." \
  LANGFUSE_HOST="https://cloud.langfuse.com"  # or self-hosted URL

# Or for local dev, add to .env
echo "LANGFUSE_PUBLIC_KEY=pk-lf-..." >> .env
echo "LANGFUSE_SECRET_KEY=sk-lf-..." >> .env
echo "LANGFUSE_HOST=https://cloud.langfuse.com" >> .env
```

Without those env vars, the app behaves exactly as it does today — no Langfuse calls, no SDK import, zero perf impact.

## What gets traced

For every `Agent.run()` invocation (rule-based and LLM both), one Langfuse trace is created with:

- **trace.name** = `agent.spec.name` (e.g. `nova.compose.template_recipe`)
- **trace.session_id** = `ctx.job_id` — all agents called during one Job cluster in the UI
- **trace.input** = `validated_input.model_dump()`
- **trace.output** = parsed `Output.model_dump()` (None on failure)
- **trace.tags** = `[outcome, agent_name]`
- **trace.metadata** = `{prompt_version, outcome, attempts, fallback_used, segment_idx, request_id}`

Each trace gets one **generation** child span with:
- `model` = chosen Gemini model
- `usage` = `{input: tokens_in, output: tokens_out, unit: "TOKENS"}`
- `metadata` = `{prompt_version, cost_usd, latency_ms}`
- `level` = `ERROR` on failure, `DEFAULT` on success
- `status_message` = exception string on failure

## What does NOT get traced

By design, this is intentionally minimal:

- **Rendered prompt text** — not captured. The prompt is rendered inside `_run_on_model()` and not threaded back up to `_log_outcome`. To replay a trace, run the agent locally against the captured `input_dict` — the prompt will reconstruct deterministically.
- **Raw model response (`raw_text`)** — not captured. Same reason. The parsed `output_dict` is captured instead; if you need the raw, run a live re-call.
- **Anything from the eval harness** — replay/judge runs do not log to Langfuse. Tracing is for prod traffic only.

If either gap becomes load-bearing, the fix is one parameter added to `_log_outcome` plus threading through `_run_on_model`.

## Architecture

```
Agent.run()  ──→  _log_outcome(input_dict, output_dict, ...)
                       │
                       ├──→ structlog.info("agent_run", ...)   [always]
                       │
                       └──→ _langfuse.trace_agent_run(...)     [if configured]
                                  │
                                  ├──→ _get_client() lazy singleton
                                  ├──→ client.trace(...)
                                  └──→ trace.generation(...)
```

Files:
- `app/agents/_langfuse.py` — lazy client + `trace_agent_run()` shim. 100 lines, no other deps.
- `app/agents/_runtime.py` — `_log_outcome` calls `trace_agent_run` after the structlog event. All 5 call sites thread `input_dict` and (when available) `output_dict`.
- `tests/agents/test_langfuse.py` — 6 tests covering the fail-open contract end-to-end.

## Verifying it works

```bash
# After install + env vars
cd src/apps/api && .venv/bin/python -c "
from app.agents._langfuse import _get_client
print('client:', _get_client())
"
# Should print something like: client: <langfuse.Langfuse object at 0x...>

# Then trigger any agent call (e.g. submit a template job) and check
# the Langfuse UI — you should see one trace per agent invocation,
# grouped by session_id == Job.id.
```

## Cost

Langfuse Cloud free tier: 50k observations/month. Each `Agent.run()` posts one trace + one generation = 2 observations. At Nova's current volume (~6 agents × ~50 jobs/day = 600/day), that's ~36k/month — fits in free tier with headroom.

## Follow-ups (not in this spike)

- Capture rendered prompt + raw_text. Requires adding `last_prompt` to `_RunStats` and `last_raw_text` to capture in `_run_on_model`.
- Wire Langfuse score-callback into the eval harness so judge scores flow back as `trace.score()` calls. Closes the loop between prod traffic and CI evals.
- Use Langfuse's "Datasets" feature as the source-of-truth for `prod_snapshots/` fixtures (instead of `scripts/export_eval_fixtures.py`).
- Online evals: configure Langfuse to score a sampled fraction of prod traffic with the existing rubrics from `tests/evals/rubrics/`.
