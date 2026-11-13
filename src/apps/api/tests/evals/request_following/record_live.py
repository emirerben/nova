"""Price and record request-following threads against the real chat copilot.

    cd src/apps/api
    python -m tests.evals.request_following.record_live --price-only
    NOVA_EVAL_MODE=live python -m tests.evals.request_following.record_live \\
        --only harbor_title_from_brief,food_exact_title \\
        --usage-purpose live_eval --test-run-id rf-baseline-<date>-a \\
        --max-cost-usd 2 --approve-reservation

A thread is recorded turn by turn. Turn N is asked over the edit the recorded turns before it
produced (the same snapshot builder the iPhone chat uses), the model's raw text is stored beside
the exact input it saw, and the fixture is rewritten, so a run that stops on the cap resumes at
the next turn and replay afterwards needs no network. Every call goes through the paid-call
ledger: the run stops cleanly before the call that could cross ``--max-cost-usd`` and the next
batch is a new ``--test-run-id``.

What is measured is the copilot agent, the honest-outcome gate and the op compiler
(``runner.replay_v1_copilot``). The planner's routing in front of the copilot and the repair
retry behind it are not in this path.
"""

from __future__ import annotations

import argparse
import json
import os
import time
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .models import FinalPlan, Footage, RFFixture
from .phone_snapshot import phone_snapshot, seed_plan
from .runner import (
    discover_fixture_paths,
    load_fixture,
    load_footage,
    replay_turns,
    stamp_baseline,
)

# Pessimistic reservation inputs: see app.services.ai_cost_control.estimate_call_cost_usd.
_TYPICAL_THOUGHT_AND_ANSWER_TOKENS = 7_000


@contextmanager
def _unmetered():
    """Replaying a recording makes no paid call, but a cassette client still passes the ledger
    gate, which refuses an unattributed call while live cost control is on."""
    from app.config import settings

    before = settings.ai_cost_control_enabled
    settings.ai_cost_control_enabled = False
    try:
        yield
    finally:
        settings.ai_cost_control_enabled = before


def _needs_recording(path: Path) -> bool:
    raw = json.loads(path.read_text(encoding="utf-8"))
    return any("raw_text" not in (t.get("copilot") or {}) for t in raw["turns"]) and not any(
        (t.get("kria") or {}).get("cassette") or (t.get("kria") or {}).get("strategy_cassette")
        for t in raw["turns"]
    )


def _prior_turns(turns: list[tuple[str, str | None]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for message, reply in turns:
        rows.append({"role": "user", "content": message})
        if reply:
            rows.append({"role": "assistant", "content": reply})
    return rows


def agent_input_for(
    fixture: RFFixture,
    footage: Footage,
    index: int,
    plan_before: FinalPlan,
    replies: list[str | None],
) -> dict[str, Any]:
    turn = fixture.turns[index]
    existing = (turn.copilot or {}).get("agent_input")
    if existing:
        return existing
    history = [(fixture.turns[i].user_message, replies[i]) for i in range(index)]
    payload: dict[str, Any] = {
        "utterance": turn.user_message,
        "prior_turns": _prior_turns(history),
        "variant_snapshot": phone_snapshot(footage, plan_before),
    }
    if index:
        payload["original_request"] = fixture.turns[0].user_message
    return payload


def rendered_prompt_chars(agent_input: dict[str, Any]) -> int:
    from app.agents._model_client import default_client
    from app.agents.edit_copilot import EditCopilotAgent, EditCopilotInput

    agent = EditCopilotAgent(default_client())
    return len(agent.render_prompt(EditCopilotInput.model_validate(agent_input)).encode("utf-8"))


def reservation_estimate_usd(model: str, prompt: str) -> float:
    from app.agents.edit_copilot import EditCopilotAgent
    from app.services.ai_cost_control import estimate_call_cost_usd

    return estimate_call_cost_usd(
        model=model,
        prompt=prompt,
        max_output_tokens=EditCopilotAgent.max_output_tokens,
        media_mime=None,
    )


def price(paths: list[Path]) -> dict[str, Any]:
    """Zero-spend cost estimate for the first turn of every thread still to record."""
    from app.agents.edit_copilot import EditCopilotAgent, EditCopilotInput
    from app.config import settings

    model = settings.edit_copilot_model
    agent = EditCopilotAgent(None)  # render only
    rows = []
    for path in paths:
        fixture = load_fixture(path)
        footage = load_footage(fixture.footage)
        plan = fixture.initial_plan or seed_plan(footage)
        payload = agent_input_for(fixture, footage, 0, plan, [])
        prompt = agent.render_prompt(EditCopilotInput.model_validate(payload))
        in_tokens = (len(prompt.encode("utf-8")) + 2) // 3
        expected = (in_tokens * 2.0 + _TYPICAL_THOUGHT_AND_ANSWER_TOKENS * 12.0) / 1e6
        rows.append(
            {
                "fixture": fixture.fixture_id,
                "turns": len(fixture.turns),
                "prompt_tokens_upper": in_tokens,
                "reservation_usd": round(reservation_estimate_usd(model, prompt), 4),
                "expected_usd_per_turn": round(expected, 4),
            }
        )
    calls = sum(r["turns"] for r in rows)
    expected_total = sum(r["expected_usd_per_turn"] * r["turns"] for r in rows)
    return {
        "model": model,
        "prompt_version": EditCopilotAgent.spec.prompt_version,
        "threads": len(rows),
        "calls": calls,
        "expected_total_usd": round(expected_total, 2),
        "max_reservation_usd": max((r["reservation_usd"] for r in rows), default=0.0),
        "rows": rows,
    }


def run_spend_usd(test_run_id: str) -> float:
    from app.database import sync_engine
    from app.services.ai_cost_control import _test_run_spend

    with sync_engine.connect() as conn:
        return float(_test_run_spend(conn, test_run_id=test_run_id))


def _record_turn(
    fixture: RFFixture, index: int, agent_input: dict[str, Any], test_run_id: str
) -> dict[str, Any]:
    from app.agents._model_client import default_client
    from app.agents.edit_copilot import EditCopilotAgent, EditCopilotInput

    from ..runners.eval_runner import RecordingModelClient, build_eval_run_context

    client = RecordingModelClient(default_client())
    # One request id per fixture and turn: the ledger de-duplicates a paid call by it.
    ctx = build_eval_run_context(
        f"request-following:{fixture.fixture_id}:{fixture.turns[index].turn_id}", is_live=True
    )
    started = time.perf_counter()
    EditCopilotAgent(client).run(EditCopilotInput.model_validate(agent_input), ctx=ctx)
    latency_s = time.perf_counter() - started
    last = client.invocations[-1]
    return {
        "agent_input": agent_input,
        "raw_text": last.raw_text,
        "prompt_version": EditCopilotAgent.spec.prompt_version,
        "model_actual": last.model_used or EditCopilotAgent.spec.model,
        "recorded_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "latency_s": round(latency_s, 2),
        "model_calls": len(client.invocations),
        "tokens_in": last.tokens_in,
        "tokens_out": last.tokens_out,
        "tokens_thoughts": last.tokens_thoughts,
        "test_run_id": test_run_id,
    }


def record_thread(path: Path, test_run_id: str, cap_usd: float) -> str:
    """Record every unrecorded turn of one thread. Returns "done", or "cap" when the next call
    could cross the run's cap (nothing is spent on it)."""
    from app.config import settings

    while True:
        raw = json.loads(path.read_text(encoding="utf-8"))
        fixture = RFFixture.model_validate(raw)
        footage = load_footage(fixture.footage)
        todo = next(
            (i for i, t in enumerate(raw["turns"]) if "raw_text" not in (t.get("copilot") or {})),
            None,
        )
        if todo is None:
            return "done"
        if fixture.initial_plan is None:
            raw["initial_plan"] = seed_plan(footage).model_dump(mode="json", exclude_none=True)
            fixture = RFFixture.model_validate(raw)
        with _unmetered():
            results = replay_turns(fixture) if todo else []
        plan_before = results[todo - 1].plan_after if todo else fixture.initial_plan
        replies = [r.reply for r in results[:todo]]
        payload = agent_input_for(fixture, footage, todo, plan_before, replies)
        from app.agents.edit_copilot import EditCopilotAgent, EditCopilotInput

        prompt = EditCopilotAgent(None).render_prompt(EditCopilotInput.model_validate(payload))
        estimate = reservation_estimate_usd(settings.edit_copilot_model, prompt)
        spent = run_spend_usd(test_run_id)
        if spent + estimate > cap_usd:
            print(f"cap: {fixture.fixture_id} turn {todo}: spent {spent:.3f} + est {estimate:.3f}")
            return "cap"
        recorded = _record_turn(fixture, todo, payload, test_run_id)
        turn = raw["turns"][todo]
        turn["engine"] = "v1_copilot"
        turn["copilot"] = recorded
        path.write_text(json.dumps(raw, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        print(
            f"recorded {fixture.fixture_id}/{turn['turn_id']} "
            f"{recorded['latency_s']}s model={recorded['model_actual']} "
            f"spent={run_spend_usd(test_run_id):.4f}"
        )
        if todo == len(raw["turns"]) - 1:
            with _unmetered():
                stamp_baseline(path)
            return "done"


def _paths(only: list[str] | None, include_recorded: bool) -> list[Path]:
    paths = [p for p in discover_fixture_paths() if p.stem != "east_run"]
    if only:
        paths = [p for p in paths if p.stem in only]
    return [p for p in paths if include_recorded or _needs_recording(p)]


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--price-only", action="store_true")
    parser.add_argument("--only", default=None, help="comma-separated fixture ids")
    parser.add_argument("--usage-purpose", choices=["live_eval", "provider_smoke"], default=None)
    parser.add_argument("--test-run-id", default=None)
    parser.add_argument("--max-cost-usd", type=float, default=None)
    parser.add_argument("--approve-reservation", action="store_true")
    args = parser.parse_args(argv)
    os.environ.setdefault("STORAGE_BUCKET", "nova-test")
    os.environ.setdefault("STORAGE_PROVIDER", "gcs")
    os.environ.setdefault("REDIS_URL", "redis://localhost:6379")
    os.environ.setdefault("OPENAI_API_KEY", "sk-test")
    only = args.only.split(",") if args.only else None
    paths = _paths(only, include_recorded=False)
    if args.price_only:
        print(json.dumps(price(paths), indent=2))
        return
    from .report import _require_live_guards

    _require_live_guards(args, parser)
    os.environ["AI_COST_CONTROL_ENABLED"] = "true"
    os.environ.setdefault("AI_USAGE_ENVIRONMENT", "development")
    for path in paths:
        if record_thread(path, args.test_run_id, args.max_cost_usd) == "cap":
            break
    print(f"run {args.test_run_id} spent ${run_spend_usd(args.test_run_id):.4f}")


if __name__ == "__main__":
    main()
