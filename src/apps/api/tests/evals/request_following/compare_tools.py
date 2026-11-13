"""KRI-797: the chat copilot on typed tool calls (KRI-1393), per model, on request-following.

    cd src/apps/api
    python -m tests.evals.request_following.compare_tools --arm sol --price-only
    NOVA_EVAL_MODE=live python -m tests.evals.request_following.compare_tools --arm sol \\
        --out ../../../.dev/kri797 --usage-purpose live_eval --test-run-id kri797-sol-a \\
        --max-cost-usd 2 --approve-reservation [--only a,b] [--tag r2]
    python -m tests.evals.request_following.compare_tools --score --out ../../../.dev/kri797

Each arm records the authored threads of ``tests/fixtures/request_following/threads`` (74 threads:
30 conversation briefs and the per-action set; East Run's prod capture is not re-recorded) into
its own copy under ``--out/<arm>[-<tag>]/``. Turn 0 sees the exact input the JSON baseline saw;
a later turn is asked over the edit this arm's own earlier turns produced. Every turn runs the
first answer and the chat route's repair rounds (``routes._copilot._repaired``, two rounds inside
a 180 s turn budget), each its own paid call. A run stops before the turn whose three calls could
cross ``--max-cost-usd``; the next batch is a new ``--test-run-id`` and resumes at that turn.

The JSON-mode Gemini control is the committed baseline recording (``--score`` replays it).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .record_live import _unmetered, agent_input_for, run_spend_usd
from .runner import (
    THREAD_DIR,
    discover_fixture_paths,
    load_fixture,
    load_footage,
    replay_turns,
    run_thread,
)

ARMS: dict[str, tuple[str, str]] = {
    "gemini": ("gemini-3.1-pro-preview", "high"),
    "sol": ("gpt-6.1-sol", "medium"),
    "sonnet": ("claude-sonnet-5-5", "medium"),
}
# The chat route's repair loop runs another round only while a full copilot call still fits:
# the planner passes a 120 s copilot timeout (`planner.py`); this harness gives each turn 180 s.
_TURN_BUDGET_S = 180.0
_COPILOT_TIMEOUT_S = 120.0


def _authored_paths() -> list[Path]:
    paths = []
    for path in discover_fixture_paths():
        raw = json.loads(path.read_text(encoding="utf-8"))
        if raw.get("provenance") == "authored" and all(
            t.get("engine") == "v1_copilot" and "raw_text" in (t.get("copilot") or {})
            for t in raw["turns"]
        ):
            paths.append(path)
    return paths


def _arm_dir(out: Path, arm: str, tag: str | None) -> Path:
    return out / (f"{arm}-{tag}" if tag else arm) / "threads"


def _seed(source: Path, target: Path) -> None:
    """The arm's copy of a thread: turn 0 keeps the baseline's exact input, later turns are
    asked over this arm's own edit, and the baseline's pinned statuses do not apply."""
    if target.exists():
        return
    raw = json.loads(source.read_text(encoding="utf-8"))
    for index, turn in enumerate(raw["turns"]):
        turn["copilot"] = (
            {"agent_input": turn["copilot"]["agent_input"]} if index == 0 else {"unrecorded": True}
        )
    raw.pop("baseline", None)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(raw, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def _agent(model: str, effort: str, client: Any):
    from dataclasses import replace

    from app.agents.edit_copilot import EditCopilotAgent
    from app.services.creator_model_comparison import tools_agent

    agent = tools_agent(EditCopilotAgent(client))
    # The model under test answers alone: a fallback would hide its failures.
    agent.spec = replace(agent.spec, model=model, thinking_level=effort, fallback_models=())
    return agent


def _estimate(agent: Any, payload: dict[str, Any]) -> float:
    from app.agents.edit_copilot import EditCopilotInput
    from app.services.ai_cost_control import estimate_call_cost_usd

    validated = EditCopilotInput.model_validate(payload)
    request = agent.tool_request(validated)
    return estimate_call_cost_usd(
        model=agent.spec.model,
        prompt=agent.render_prompt(validated) + request.accounting_text(),
        max_output_tokens=agent.max_output_tokens,
        media_mime=None,
    )


def _record_turn(
    fixture_id: str, turn_id: str, payload: dict[str, Any], model: str, effort: str, run_id: str
) -> dict[str, Any]:
    from app.agents._model_client import default_client
    from app.agents._runtime import TerminalError
    from app.agents.edit_copilot import EditCopilotInput
    from app.routes._copilot import _repaired

    from ..runners.eval_runner import RecordingModelClient, build_eval_run_context

    client = RecordingModelClient(default_client())
    agent = _agent(model, effort, client)
    rounds: list[dict[str, Any]] = []
    run = agent.run

    def timed(agent_input: Any, *, ctx: Any) -> Any:
        before = len(client.invocations)
        started = time.perf_counter()
        try:
            return run(agent_input, ctx=ctx)
        finally:
            calls = client.invocations[before:]
            rounds.append(
                {
                    "latency_s": round(time.perf_counter() - started, 2),
                    "calls": [
                        {
                            "tokens_in": c.tokens_in,
                            "tokens_cached": c.tokens_cached,
                            "tokens_out": c.tokens_out,
                            "tokens_thoughts": c.tokens_thoughts,
                            "model_used": c.model_used,
                            "finish_status": c.finish_status,
                        }
                        for c in calls
                    ],
                    # Every answer of the round: the last is the one parsed (a schema retry
                    # leaves an earlier one). The provider's continuation is not kept.
                    "answers": [_recorded(c.raw_text) for c in calls],
                }
            )

    agent.run = timed  # type: ignore[method-assign]
    validated = EditCopilotInput.model_validate(payload)
    key = f"kri797:{run_id}:{fixture_id}:{turn_id}"

    def context(suffix: str = ""):
        return build_eval_run_context(key + suffix, is_live=True)

    started = time.perf_counter()
    record: dict[str, Any] = {
        "agent_input": payload,
        "output_mode": "tools",
        "model": model,
        "effort": effort,
        "prompt_version": agent.spec.prompt_version,
        "test_run_id": run_id,
        "recorded_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    try:
        output = agent.run(validated, ctx=context())
    except TerminalError as exc:
        record.update(error=f"{type(exc).__name__}: {str(exc)[:300]}", rounds=rounds)
        return record
    deadline = time.monotonic() - (time.perf_counter() - started) + _TURN_BUDGET_S
    asyncio.run(
        _repaired(
            agent,
            validated,
            output,
            context,
            deadline_monotonic=deadline,
            timeout_override_s=_COPILOT_TIMEOUT_S,
        )
    )
    answers = [r["answers"][-1] for r in rounds if r["answers"]]
    record.update(
        raw_text=answers[0],
        repairs=answers[1:],
        rounds=rounds,
        latency_s=rounds[0]["latency_s"],
        turn_latency_s=round(time.perf_counter() - started, 2),
        model_actual=next(
            (c["model_used"] for r in rounds for c in r["calls"] if c["model_used"]), model
        ),
    )
    return record


def _recorded(raw_text: str) -> str:
    from app.agents._tool_calls import ToolAnswer

    try:
        return ToolAnswer.decode(raw_text).recorded().envelope()
    except ValueError:
        return raw_text


def record_thread(path: Path, model: str, effort: str, run_id: str, cap_usd: float) -> str:
    """Record every unrecorded turn of one arm thread: "done", or "cap" (nothing spent on it)."""
    from app.agents.edit_copilot import EditCopilotInput

    while True:
        raw = json.loads(path.read_text(encoding="utf-8"))
        fixture = load_fixture(path)
        footage = load_footage(fixture.footage)
        todo = next(
            (
                i
                for i, t in enumerate(raw["turns"])
                if "raw_text" not in t["copilot"] and "error" not in t["copilot"]
            ),
            None,
        )
        if todo is None:
            return "done"
        with _unmetered():
            results = replay_turns(fixture) if todo else []
        plan_before = results[todo - 1].plan_after if todo else fixture.initial_plan
        replies = [r.reply for r in results[:todo]]
        payload = agent_input_for(fixture, footage, todo, plan_before, replies)
        EditCopilotInput.model_validate(payload)
        estimate = _estimate(_agent(model, effort, None), payload) * 2
        spent = run_spend_usd(run_id)
        if spent + estimate > cap_usd:
            print(f"cap: {fixture.fixture_id} turn {todo}: spent {spent:.3f} + est {estimate:.3f}")
            return "cap"
        before = spent
        record = _record_turn(
            fixture.fixture_id, raw["turns"][todo]["turn_id"], payload, model, effort, run_id
        )
        record["cost_usd"] = round(run_spend_usd(run_id) - before, 6)
        raw["turns"][todo]["copilot"] = record
        path.write_text(json.dumps(raw, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        print(
            f"{fixture.fixture_id}/{raw['turns'][todo]['turn_id']} "
            f"{record.get('turn_latency_s', '-')}s rounds={len(record.get('rounds', []))} "
            f"cost={record['cost_usd']:.4f}"
            + (f" ERROR {record['error']}" if "error" in record else "")
        )


def price(arm: str, only: list[str] | None) -> dict[str, Any]:
    """Zero-spend estimate of each thread's first turn on this arm (repairs not included)."""
    model, effort = ARMS[arm]
    agent = _agent(model, effort, None)
    rows = []
    for path in _authored_paths():
        if only and path.stem not in only:
            continue
        raw = json.loads(path.read_text(encoding="utf-8"))
        payload = raw["turns"][0]["copilot"]["agent_input"]
        rows.append(
            {
                "fixture": path.stem,
                "turns": len(raw["turns"]),
                "reservation_usd": round(_estimate(agent, payload), 4),
            }
        )
    return {
        "arm": arm,
        "model": model,
        "effort": effort,
        "threads": len(rows),
        "calls": sum(r["turns"] for r in rows),
        "reservation_total_usd": round(sum(r["reservation_usd"] * r["turns"] for r in rows), 2),
        "rows": rows,
    }


def _percentile(values: list[float], share: float) -> float | None:
    import math

    if not values:
        return None
    ordered = sorted(values)
    return ordered[max(0, math.ceil(share * len(ordered)) - 1)]


def _without_repairs(path: Path, target: Path) -> Path:
    raw = json.loads(path.read_text(encoding="utf-8"))
    for turn in raw["turns"]:
        if turn.get("copilot", {}).get("repairs"):
            turn["copilot"]["repairs"] = []
    target.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
    return target


def _first_answer_rejections(payload: dict[str, Any]) -> int:
    """How many ops of the first answer the parser refused (before any repair round)."""
    from app.agents.edit_copilot import EditCopilotAgent

    from ..runners.eval_runner import build_eval_run_context
    from .runner import _AnswerSequence

    client = _AnswerSequence([str(payload["raw_text"])])
    agent = EditCopilotAgent(client)
    if payload.get("output_mode") == "tools":
        from app.services.creator_model_comparison import tools_agent

        agent = tools_agent(agent)
    with _unmetered():
        output = agent.run(
            payload["agent_input"], ctx=build_eval_run_context("kri797:rejections", is_live=False)
        )
    return sum(1 for item in output.rejection_reasons if item.get("op") != "bundle")


def score_dir(directory: Path, *, first_answer_only: bool = False) -> dict[str, Any]:
    """Replay one arm's recordings (or the JSON baseline) through the real path and score."""
    import tempfile

    from .scorer import compute_kpi

    paths = sorted(directory.glob("*.json"))
    with tempfile.TemporaryDirectory() as scratch, _unmetered():
        results = []
        for path in paths:
            source = (
                _without_repairs(path, Path(scratch) / path.name) if first_answer_only else path
            )
            fixture = load_fixture(source)
            if fixture.provenance != "authored":
                continue
            results.append((fixture, run_thread(fixture)))
    kpi = compute_kpi(r for _, r in results)
    calls = [t.copilot or {} for f, _ in results for t in f.turns]
    first = [c["latency_s"] for c in calls if "latency_s" in c]
    turn = [c.get("turn_latency_s", c.get("latency_s")) for c in calls if "latency_s" in c]
    rounds = [r for c in calls for r in c.get("rounds", [])]
    invocations = [i for r in rounds for i in r["calls"]]
    first_calls = [c["rounds"][0]["calls"] for c in calls if c.get("rounds")]
    tokens_in = [sum(i["tokens_in"] for i in r[-1:]) for r in first_calls if r]
    cached = [sum(i["tokens_cached"] for i in r[-1:]) for r in first_calls if r]
    costs = [c["cost_usd"] for c in calls if "cost_usd" in c]
    repaired = [c for c in calls if c.get("repairs")]
    errors = [c for c in calls if c.get("error")]
    retried = [c for c in calls if c.get("rounds") and len(c["rounds"][0]["calls"]) > 1]
    rejected = [_first_answer_rejections(c) for c in calls if c.get("raw_text")]
    per_thread = {f.fixture_id: {s.requirement_id: s.status for s in r.scores} for f, r in results}
    return {
        "threads": len(results),
        "turns": len(calls),
        "requirements": kpi.overall.total,
        "met": kpi.overall.met,
        "partial": kpi.overall.partial,
        "unmet": kpi.overall.unmet,
        "met_share": kpi.met_share,
        "overclaims": kpi.overall.overclaims,
        "first_answer_latency_p50_s": _percentile(first, 0.5),
        "first_answer_latency_p95_s": _percentile(first, 0.95),
        "turn_latency_p50_s": _percentile([t for t in turn if t is not None], 0.5),
        "turn_latency_p95_s": _percentile([t for t in turn if t is not None], 0.95),
        "model_calls": len(invocations) or len(first),
        "first_answer_tokens_in_mean": (sum(tokens_in) / len(tokens_in)) if tokens_in else None,
        "first_answer_tokens_cached_mean": (sum(cached) / len(cached)) if cached else None,
        "cost_total_usd": round(sum(costs), 4) if costs else None,
        "cost_per_turn_usd": round(sum(costs) / len(costs), 4) if costs else None,
        "turns_with_repair_round": len(repaired),
        "turns_failed": len(errors),
        "failed": [c.get("error") for c in errors],
        "turns_with_schema_retry": len(retried),
        "turns_with_rejected_op": sum(1 for count in rejected if count),
        "rejected_ops": sum(rejected),
        "per_thread": per_thread,
    }


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--arm", choices=sorted(ARMS))
    parser.add_argument("--out", type=Path, default=None)
    parser.add_argument("--tag", default=None, help="a rerun's label (its own copy of the arm)")
    parser.add_argument("--price-only", action="store_true")
    parser.add_argument("--score", action="store_true")
    parser.add_argument("--only", default=None, help="comma-separated fixture ids")
    parser.add_argument("--usage-purpose", choices=["live_eval", "provider_smoke"], default=None)
    parser.add_argument("--test-run-id", default=None)
    parser.add_argument("--max-cost-usd", type=float, default=None)
    parser.add_argument("--approve-reservation", action="store_true")
    args = parser.parse_args(argv)
    for key, value in {
        "STORAGE_BUCKET": "nova-test",
        "STORAGE_PROVIDER": "gcs",
        "REDIS_URL": "redis://localhost:6379",
    }.items():
        os.environ.setdefault(key, value)
    only = args.only.split(",") if args.only else None
    if args.price_only:
        print(json.dumps(price(args.arm, only), indent=2))
        return
    if args.score:
        result: dict[str, Any] = {"json_gemini_control": score_dir(THREAD_DIR)}
        for directory in sorted(p for p in args.out.iterdir() if (p / "threads").is_dir()):
            threads = directory / "threads"
            result[directory.name] = score_dir(threads)
            result[directory.name + ":first_answer"] = score_dir(threads, first_answer_only=True)
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return
    from .report import _require_live_guards

    _require_live_guards(args, parser)
    os.environ["AI_COST_CONTROL_ENABLED"] = "true"
    os.environ.setdefault("AI_USAGE_ENVIRONMENT", "development")
    model, effort = ARMS[args.arm]
    target = _arm_dir(args.out, args.arm, args.tag)
    for source in _authored_paths():
        if only and source.stem not in only:
            continue
        path = target / source.name
        _seed(source, path)
        if record_thread(path, model, effort, args.test_run_id, args.max_cost_usd) == "cap":
            break
    print(f"run {args.test_run_id} spent ${run_spend_usd(args.test_run_id):.4f}")


if __name__ == "__main__":
    main()
