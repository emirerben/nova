"""Ask the Kria editor copilot one thing (or a battery) against a LOCAL thread.

    python -m app.cli.kria_ask --thread <id> "make all labels say Arnavutkoy"
    python -m app.cli.kria_ask --thread <id> --battery tests/fixtures/kria_battery/asks.yaml
    python -m app.cli.kria_ask --thread <id> --commit "make it 20s"   # real turn
    python -m app.cli.kria_ask --thread <id> --record name "ask"      # kria_turns fixture

Default is a DRY RUN: live copilot -> parser -> ``compile_editor_ops`` ->
receipts, printed only. No draft, job, item or thread row is written (the
agent runtime still logs its own ``agent_run`` row, as for any Agent.run).
``--commit`` submits a real Kria turn and runs it inline so a simulator pointed
at the local API sees the change. Refuses any non-local database.
"""

from __future__ import annotations

import argparse
import asyncio
import copy
import json
import sys
import uuid
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

DEFAULT_FIXTURES = Path(__file__).parents[2] / "tests" / "fixtures" / "kria_turns"


# --------------------------------------------------------------------------- data


@dataclass
class AskResult:
    ask: str
    intent: str = ""
    outcome: str = ""
    reply: str = ""
    ops: list[dict[str, Any]] = field(default_factory=list)
    changes: list[str] = field(default_factory=list)
    error: str | None = None  # compiler/copilot failure text
    text_diff: list[str] = field(default_factory=list)
    slot_diff: list[str] = field(default_factory=list)
    receipts: list[dict[str, Any]] = field(default_factory=list)
    unmet_requests: list[dict[str, str]] = field(default_factory=list)

    @property
    def op_names(self) -> list[str]:
        return [str(op.get("op")) for op in self.ops]


@dataclass
class BatteryCase:
    ask: str
    expect_ops: list[str] = field(default_factory=list)
    expect_error: str | None = None
    id: str = ""
    turns: list[dict[str, str]] = field(default_factory=list)


@dataclass
class CaseVerdict:
    case: BatteryCase
    passed: bool
    detail: str
    result: AskResult | None = None


# --------------------------------------------------------------------- pure logic


def load_battery(path: Path) -> list[BatteryCase]:
    try:
        import yaml  # noqa: PLC0415
    except ImportError as exc:  # pragma: no cover - dev tool
        raise SystemExit("--battery needs PyYAML (pip install pyyaml)") from exc
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    rows = raw.get("asks") if isinstance(raw, dict) else raw
    if not isinstance(rows, list) or not rows:
        raise SystemExit(f"{path}: expected a non-empty list of asks")
    cases: list[BatteryCase] = []
    for index, row in enumerate(rows):
        if not isinstance(row, dict) or not str(row.get("ask") or "").strip():
            raise SystemExit(f"{path}: entry {index} needs an `ask`")
        expect_ops = row.get("expect_ops") or []
        if not isinstance(expect_ops, list):
            raise SystemExit(f"{path}: entry {index} expect_ops must be a list")
        cases.append(
            BatteryCase(
                ask=str(row["ask"]).strip(),
                expect_ops=[str(name) for name in expect_ops],
                expect_error=(str(row["expect_error"]) if row.get("expect_error") else None),
                id=str(row.get("id") or f"ask-{index + 1}"),
                turns=[dict(t) for t in row.get("turns") or [] if isinstance(t, dict)],
            )
        )
    return cases


def evaluate_case(case: BatteryCase, result: AskResult) -> CaseVerdict:
    """Pass when every expected op name was produced and no error occurred.

    ``expect_error`` inverts the error rule: the compile/copilot error or the
    reply must contain that substring (case-insensitive), e.g. zero-match
    honesty. ``expect_ops: []`` with no ``expect_error`` means "no ops".
    """
    if case.expect_error:
        haystack = f"{result.error or ''} {result.reply}".lower()
        # `a|b` = any alternative (replies are model-worded, sometimes Turkish).
        ok = any(
            alt.strip().lower() in haystack for alt in case.expect_error.split("|") if alt.strip()
        )
        if ok and not result.error and result.ops:
            # An honesty case that still emitted ops claimed success it cannot deliver.
            return CaseVerdict(case, False, f"honesty case produced ops {result.op_names}", result)
        return CaseVerdict(
            case, ok, "" if ok else f"missing error text {case.expect_error!r}", result
        )
    if result.error:
        return CaseVerdict(case, False, f"error: {result.error}", result)
    if not case.expect_ops:
        ok = not result.ops
        return CaseVerdict(case, ok, "" if ok else f"unexpected ops {result.op_names}", result)
    missing = [name for name in case.expect_ops if name not in result.op_names]
    if missing:
        return CaseVerdict(case, False, f"missing ops {missing}; got {result.op_names}", result)
    return CaseVerdict(case, True, "", result)


AskRunner = Callable[[str, list[dict[str, str]]], Awaitable[AskResult]]


async def run_battery(cases: Sequence[BatteryCase], run_one: AskRunner) -> list[CaseVerdict]:
    verdicts: list[CaseVerdict] = []
    for case in cases:
        try:
            result = await run_one(case.ask, case.turns)
        except Exception as exc:  # noqa: BLE001 - one bad ask must not stop the battery
            result = AskResult(ask=case.ask, error=f"{type(exc).__name__}: {exc}")
        verdicts.append(evaluate_case(case, result))
    return verdicts


def format_table(verdicts: Sequence[CaseVerdict]) -> str:
    width = max((len(v.case.id) for v in verdicts), default=2)
    lines = [f"{'ID'.ljust(width)}  RESULT  ASK / DETAIL"]
    for verdict in verdicts:
        mark = "PASS" if verdict.passed else "FAIL"
        lines.append(f"{verdict.case.id.ljust(width)}  {mark}    {verdict.case.ask[:70]}")
        if not verdict.passed:
            lines.append(f"{' ' * width}          -> {verdict.detail}")
    passed = sum(v.passed for v in verdicts)
    lines.append(f"\n{passed}/{len(verdicts)} passed")
    return "\n".join(lines)


def diff_text(before: list[dict[str, Any]], after: list[dict[str, Any]]) -> list[str]:
    """Before/after of the text lane, keyed by bar id (falls back to position)."""

    def key(row: dict[str, Any], index: int) -> str:
        return str(row.get("id") or f"#{index}")

    old = {key(r, i): r for i, r in enumerate(before)}
    new = {key(r, i): r for i, r in enumerate(after)}
    lines: list[str] = []
    for bar_id, row in old.items():
        if bar_id not in new:
            lines.append(f"- {bar_id}: removed ({row.get('text')!r})")
            continue
        now = new[bar_id]
        changed = [
            f"{field}: {row.get(field)!r} -> {now.get(field)!r}"
            for field in sorted(set(row) | set(now))
            if row.get(field) != now.get(field)
        ]
        if changed:
            lines.append(f"~ {bar_id}: " + "; ".join(changed))
    for bar_id, row in new.items():
        if bar_id not in old:
            lines.append(
                f"+ {bar_id}: added {row.get('text')!r} [{row.get('start_s')}-{row.get('end_s')}s]"
            )
    return lines


def _slot_brief(row: dict[str, Any]) -> str:
    return (
        f"clip={row.get('clip_index')} in={row.get('in_s')} dur={row.get('duration_s')} "
        f"removed={bool(row.get('removed'))} trans={row.get('transition_after')}"
    )


def diff_slots(before: list[dict[str, Any]], after: list[dict[str, Any]]) -> list[str]:
    lines: list[str] = []
    for index in range(max(len(before), len(after))):
        old = _slot_brief(before[index]) if index < len(before) else "(none)"
        new = _slot_brief(after[index]) if index < len(after) else "(none)"
        if old != new:
            lines.append(f"slot {index}: {old}  ->  {new}")
    return lines


def render_result(result: AskResult) -> str:
    lines = [f"ASK    {result.ask}", f"INTENT {result.intent}  OUTCOME {result.outcome}"]
    lines.append(f"REPLY  {result.reply}")
    if result.unmet_requests:
        lines.append(f"UNMET  {json.dumps(result.unmet_requests, ensure_ascii=False)}")
    lines.append("OPS")
    lines += [f"  {json.dumps(op, ensure_ascii=False, sort_keys=True)}" for op in result.ops] or [
        "  (none)"
    ]
    if result.error:
        lines.append(f"ERROR  {result.error}")
    if result.changes:
        lines.append("CHANGES " + " | ".join(result.changes))
    if result.text_diff:
        lines.append("TEXT DIFF")
        lines += [f"  {line}" for line in result.text_diff]
    if result.slot_diff:
        lines.append("SLOT DIFF")
        lines += [f"  {line}" for line in result.slot_diff]
    if result.receipts:
        lines.append("RECEIPTS")
        lines += [f"  {json.dumps(r, ensure_ascii=False, sort_keys=True)}" for r in result.receipts]
    return "\n".join(lines)


# ----------------------------------------------------------------- dry-run pipeline

CopilotFn = Callable[..., Awaitable[Any]]


async def dry_run_on_target(
    *,
    job: Any,
    variant: dict[str, Any],
    snapshot: dict[str, Any],
    conversation: list[dict[str, str]],
    ask: str,
    copilot: CopilotFn | None = None,
    receipts_fn: Callable[..., list[dict[str, Any]]] | None = None,
) -> AskResult:
    """Copilot -> parser -> compile for one ask. Pure with respect to storage."""
    from app.routes._copilot import CopilotTurnBody, run_copilot_turn  # noqa: PLC0415
    from app.services.kria_editor_ops import (  # noqa: PLC0415
        KriaEditorOpError,
        _variant_slots,
        compile_editor_ops,
    )

    copilot = copilot or run_copilot_turn
    response = await copilot(
        CopilotTurnBody(
            message=ask,
            turns=conversation[-12:],
            snapshot=snapshot,
            client_contract_version=2,
        ),
        job_id=getattr(job, "id", None) or uuid.uuid4(),
    )
    result = AskResult(
        ask=ask,
        intent=response.intent,
        outcome=response.outcome,
        reply=response.reply,
        ops=list(response.ops),
        unmet_requests=list(response.unmet_requests),
    )
    if not response.ops:
        return result
    try:
        compiled = compile_editor_ops(job, copy.deepcopy(variant), list(response.ops))
    except KriaEditorOpError as exc:
        result.error = str(exc)
        return result
    payload = compiled.payload
    result.changes = list(compiled.changes)
    if not hasattr(payload, "model_dump"):  # speech-cut style dict payloads
        return result
    dumped = payload.model_dump(mode="json", exclude_none=True)
    before_text = [r for r in variant.get("text_elements") or [] if isinstance(r, dict)]
    after_text = dumped.get("text_elements")
    if after_text is not None:
        result.text_diff = diff_text(before_text, after_text)
    after_slots = dumped.get("timeline_slots")
    if after_slots is not None:
        result.slot_diff = diff_slots(_variant_slots(variant, job), after_slots)
    if receipts_fn is not None:
        result.receipts = receipts_fn(dumped, compiled.text_diff)
    return result


# ------------------------------------------------------------------ DB-backed layer


@dataclass
class ThreadContext:
    job: Any
    variant: dict[str, Any]
    snapshot: dict[str, Any]
    conversation: list[dict[str, str]]
    creator_id: uuid.UUID
    thread_revision: int
    brief: Any | None


async def load_context(db: Any, thread_id: uuid.UUID) -> ThreadContext:
    from app.kria import planner  # noqa: PLC0415
    from app.kria.brief import load_latest_brief  # noqa: PLC0415
    from app.models import CreationThread, Job, PlanItem  # noqa: PLC0415

    thread = await db.get(CreationThread, thread_id)
    if thread is None or thread.active_plan_item_id is None:
        raise SystemExit("Thread not found or it has no active plan item")
    item = await db.get(PlanItem, thread.active_plan_item_id)
    target = await planner._load_editor_target(db, thread_id=thread_id, item=item)
    if target is None or target.variant is None:
        raise SystemExit("Thread has no ready editor target (render a variant first)")
    job = await db.get(Job, target.job_id)
    brief = await load_latest_brief(db, thread_id)
    return ThreadContext(
        job=job,
        variant=target.variant,
        snapshot=target.snapshot,
        conversation=target.conversation,
        creator_id=thread.creator_id,
        thread_revision=int(thread.revision),
        brief=brief,
    )


def make_receipts_fn(brief: Any | None) -> Callable[..., list[dict[str, Any]]]:
    def receipts(
        payload: dict[str, Any] | None, text_diff: list[dict[str, Any]] | None = None
    ) -> list[dict[str, Any]]:
        if brief is None:
            return []
        from app.kria.brief_checks import (  # noqa: PLC0415
            build_receipts,
            plan_facts_from_editor_payload,
        )

        # Latest brief's live requirements vs this dry-run payload (the real
        # runtime checks only requirements stated in the same turn).
        checked = list(brief.live())
        if not checked:
            return []
        facts = plan_facts_from_editor_payload(payload, text_diff)
        return [r.model_dump(mode="json") for r in build_receipts(checked, facts)]

    return receipts


async def _dry(thread_id: uuid.UUID, case_turns: list[dict[str, str]], ask: str) -> AskResult:
    from app.database import AsyncSessionLocal  # noqa: PLC0415

    async with AsyncSessionLocal() as db:
        ctx = await load_context(db, thread_id)
        return await dry_run_on_target(
            job=ctx.job,
            variant=ctx.variant,
            snapshot=ctx.snapshot,
            conversation=[*ctx.conversation, *case_turns],
            ask=ask,
            receipts_fn=make_receipts_fn(ctx.brief),
        )


async def commit_turn(thread_id: uuid.UUID, ask: str) -> dict[str, Any]:
    """Submit a real Kria turn and run it inline (worker not required)."""
    from app.config import settings  # noqa: PLC0415
    from app.database import AsyncSessionLocal  # noqa: PLC0415
    from app.kria.api_schemas import SubmitTurnBody  # noqa: PLC0415
    from app.kria.runtime import submit_turn  # noqa: PLC0415
    from app.models import CreationThread  # noqa: PLC0415
    from app.tasks.kria_runtime import run_kria_turn  # noqa: PLC0415

    if not settings.kria_runtime_v2_enabled:
        raise SystemExit("KRIA_RUNTIME_V2_ENABLED is false in this environment")
    async with AsyncSessionLocal() as db:
        thread = await db.get(CreationThread, thread_id)
        if thread is None:
            raise SystemExit("Thread not found")
        accepted, _replayed = await submit_turn(
            db,
            thread_id=thread_id,
            creator_id=thread.creator_id,
            body=SubmitTurnBody(
                message=ask,
                client_event_id=f"kria-ask-{uuid.uuid4().hex}",
                expected_thread_revision=int(thread.revision),
            ),
        )
        await db.commit()
    # Celery's eager path: same task body, this process, no broker for the turn itself.
    outcome = await asyncio.to_thread(lambda: run_kria_turn.apply(args=[accepted.turn_id]).get())
    return {"turn_id": accepted.turn_id, "result": outcome}


def record_fixture(name: str, result: AskResult, snapshot: dict[str, Any], root: Path) -> Path:
    from app.kria.planner import adapt_editor_action  # noqa: PLC0415
    from app.kria.replay import KriaReplayFixture  # noqa: PLC0415

    plan = adapt_editor_action(reply=result.reply, ops=result.ops)
    fixture = KriaReplayFixture(
        fixture_id=name,
        user_message=result.ask,
        snapshot=snapshot,
        plan=plan,
        expected_message=None,
    )
    path = root / f"{name}.json"
    path.write_text(
        json.dumps(fixture.model_dump(mode="json"), indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return path


# ------------------------------------------------------------------------- CLI


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m app.cli.kria_ask",
        description="Dry-run (default) or commit one ask against a local Kria thread.",
    )
    parser.add_argument("ask", nargs="?", help="what the creator would type")
    parser.add_argument("--thread", required=True, type=uuid.UUID, help="local thread id")
    parser.add_argument("--commit", action="store_true", help="run a REAL turn (writes)")
    parser.add_argument("--battery", type=Path, help="YAML of {ask, expect_ops, expect_error?}")
    parser.add_argument(
        "--record", metavar="NAME", help="write tests/fixtures/kria_turns/NAME.json"
    )
    parser.add_argument("--fixtures-root", type=Path, default=DEFAULT_FIXTURES)
    parser.add_argument("--json", action="store_true", help="print machine-readable results")
    return parser


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.battery and (args.commit or args.record):
        parser.error("--battery is dry-run only; drop --commit/--record")
    if not args.battery and not args.ask:
        parser.error("give an ask, or --battery FILE")
    if args.commit and args.record:
        parser.error("--record records a dry run; use it without --commit")
    return args


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    from app.cli.kria_dev import require_local_database  # noqa: PLC0415
    from app.config import settings  # noqa: PLC0415

    require_local_database(settings.database_url)

    if args.battery:
        cases = load_battery(args.battery)

        async def one(ask: str, turns: list[dict[str, str]]) -> AskResult:
            return await _dry(args.thread, turns, ask)

        verdicts = asyncio.run(run_battery(cases, one))
        print(format_table(verdicts))
        return 0 if all(v.passed for v in verdicts) else 1

    if args.commit:
        print(json.dumps(asyncio.run(commit_turn(args.thread, args.ask)), indent=2, default=str))
        return 0

    result = asyncio.run(_dry(args.thread, [], args.ask))
    print(
        json.dumps(result.__dict__, indent=2, default=str) if args.json else render_result(result)
    )
    if args.record:
        from app.database import AsyncSessionLocal  # noqa: PLC0415

        async def snap() -> dict[str, Any]:
            async with AsyncSessionLocal() as db:
                return (await load_context(db, args.thread)).snapshot

        path = record_fixture(args.record, result, asyncio.run(snap()), args.fixtures_root)
        print(f"recorded {path}", file=sys.stderr)
    return 0 if not result.error else 1


if __name__ == "__main__":
    raise SystemExit(main())
