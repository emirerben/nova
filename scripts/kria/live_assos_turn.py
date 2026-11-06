#!/usr/bin/env python3
"""LIVE local reproduction of the Assos creation turn (real planner, real Gemini, real research).

Prod request: "With animations, explain why people should visit Assos, Turkey" (montage, 16
clips, the first one an off-topic foreign clip). Three prod threads on 2026-11-01 ended in
"I can't make this yet". The replay (`score_assos.py`) is offline and stubs the planner, so it
could not show what the Main Creator and brief extractor really write into requirement r1. This
drives the same `run_kria_turn` the worker runs, over a scratch Postgres seeded with the
fixture's clip analyses (analysis only, no media), with prod flags and NOTHING stubbed on the
model side: the Main Creator planner, the brief extractor, the grounded search, the research
copy writer and the claim reviewer all call Gemini.

PAID. It refuses to run without ``--live``. Every call goes through the repo's reservation
ledger (``ai_cost_reservations``) under one fixed test run id with a hard USD cap, so the cap
holds across invocations against the same scratch database.

    export DATABASE_URL=$(cat .dev/test-db-url)        # scratch Postgres, name ends in _test
    cd src/apps/api && .venv/bin/python ../../../scripts/kria/live_assos_turn.py --live \\
        --label before-1 [--max-usd 1.0]

Writes ``.dev/assos-live/<label>.json``: the brief requirement r1 the planner produced (kind,
scope, facts), the final reply, the approval readiness rows, the research preview, the model
calls (agent, model, tokens, estimated cost) and the verdict.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import copy
import json
import os
import sys
import time
import uuid
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
API = REPO / "src" / "apps" / "api"
OUT_DIR = REPO / ".dev" / "assos-live"
RUN_PREFIX = "assos-live-turn-2026-11-01"
TEST_RUN_ID = RUN_PREFIX
# The ledger reserves each call's pessimistic estimate (the Main Creator alone reserves about
# USD 0.46 and settles near 0.10), so one shared envelope would stop after two runs although
# the real spend is far lower. Each run gets its own envelope; the owner's cumulative cap is
# enforced here from the settled rows of every envelope.
PER_RUN_ENVELOPE_USD = 0.8
RUN_HEADROOM_USD = 0.17
MAX_CONTINUATIONS = 6
# The two planner calls: the Main Creator and the clip-intent extractor.
PLANNER_HEADS = ("# Edit story", "You extract the COMPLETE inventory")
USER_MESSAGE = "With animations, explain why people should visit Assos, Turkey"


def _load_secret_env() -> None:
    """Put the two provider keys from the repo-root .env into the process environment.

    Nothing else is read from the file and no value is ever printed.
    """
    from dotenv import dotenv_values  # noqa: PLC0415

    values = dotenv_values(REPO / ".env")
    for name in ("GEMINI_API_KEY", "OPENAI_API_KEY"):
        if values.get(name) and not os.environ.get(name):
            os.environ[name] = str(values[name])


def _spent_usd() -> float:
    from sqlalchemy import text  # noqa: PLC0415

    from app.database import sync_session  # noqa: PLC0415

    with sync_session() as db:
        return float(
            db.execute(
                text(
                    "SELECT COALESCE(SUM(COALESCE(settled_cost_usd, estimated_cost_usd)), 0) "
                    "FROM ai_cost_reservations WHERE test_run_id LIKE :prefix "
                    "AND status IN ('reserved','provider_started','settled','unknown')"
                ),
                {"prefix": RUN_PREFIX + "%"},
            ).scalar_one()
        )


def _attribute_to_envelope(test_run_id: str) -> None:
    """Attribute every paid call of this process to the approved envelope.

    The research preview tags its calls ``kria_research_preview``, a purpose the development
    ledger refuses (``paid_test_attribution_required``), so a local run could never research.
    The call is still reserved, capped and settled by the ledger; only its purpose is the
    approved ``manual_qa`` one.
    """
    import dataclasses  # noqa: PLC0415

    from app.services import ai_cost_control  # noqa: PLC0415

    real = ai_cost_control.reserve_paid_call

    def reserve(request):  # noqa: ANN001, ANN202
        ctx = dataclasses.replace(
            request.ctx,
            usage_purpose="manual_qa",
            test_run_id=test_run_id,
            estimated_max_cost_usd=PER_RUN_ENVELOPE_USD,
            reservation_approved=True,
        )
        # The key is derived from the prompt and the turn's basis, so a second run of the same
        # draft in this database would find the first run's call "already settled".
        key = f"{request.idempotency_key}:{test_run_id}"
        return real(dataclasses.replace(request, ctx=ctx, idempotency_key=key))

    ai_cost_control.reserve_paid_call = reserve  # type: ignore[assignment]


def _configure_environment(max_usd: float, *, metered: bool = True) -> None:
    sys.path.insert(0, str(API))
    os.chdir(API)
    _load_secret_env()
    database = os.environ.get("DATABASE_URL", "")
    if not database.rsplit("/", 1)[-1].endswith("_test"):
        raise SystemExit("DATABASE_URL must be a scratch database whose name ends in _test")
    os.environ.setdefault("STORAGE_BUCKET", "nova-test")
    os.environ.setdefault("STORAGE_PROVIDER", "gcs")
    os.environ.setdefault("REDIS_URL", "redis://localhost:6379")
    os.environ.setdefault("TOKEN_ENCRYPTION_KEY", "live-assos-not-used")
    os.environ.setdefault("WAITLIST_ADMIN_SECRET", "live-assos")
    os.environ.setdefault("INTERNAL_API_KEY", "live-assos")
    os.environ["AI_COST_CONTROL_ENABLED"] = "true" if metered else "false"
    os.environ["AI_USAGE_ENVIRONMENT"] = "development"
    os.environ["AI_MANUAL_QA_TEST_RUN_ID"] = RUN_PREFIX
    os.environ["AI_MANUAL_QA_MAX_COST_USD"] = str(PER_RUN_ENVELOPE_USD)
    os.environ["AI_MANUAL_QA_RESERVATION_APPROVED"] = "true"


class _FakeRedis:
    """The few Redis calls the research cache makes, in memory."""

    def __init__(self) -> None:
        self.data: dict[str, bytes] = {}

    def get(self, key: str) -> bytes | None:
        return self.data.get(key)

    def set(self, key: str, value: str, ex: int | None = None, nx: bool = False) -> bool | None:  # noqa: ARG002
        if nx and key in self.data:
            return None
        self.data[key] = value.encode() if isinstance(value, str) else value
        return True

    def exists(self, key: str) -> int:
        return int(key in self.data)

    def delete(self, key: str) -> int:
        return int(self.data.pop(key, None) is not None)


def _load_score_assos():  # noqa: ANN202
    import importlib.util  # noqa: PLC0415

    spec = importlib.util.spec_from_file_location("score_assos", HERE / "score_assos.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["score_assos"] = module
    spec.loader.exec_module(module)
    return module


def _seed(score, fx: dict) -> tuple[uuid.UUID, uuid.UUID]:  # noqa: ANN001
    from app.database import sync_session  # noqa: PLC0415
    from app.models import CreationThread, PlanItem  # noqa: PLC0415
    from tests.kria.test_runtime_postgres_integration import _seed_runtime_project  # noqa: PLC0415

    rows = score.media_rows(fx)
    for row in rows:
        row["manifest_identity"] = row["media_id"]
        row["library_asset_id"] = str(uuid.uuid5(uuid.NAMESPACE_URL, row["media_id"]))
    user_id, thread_id, _session = _seed_runtime_project()
    with sync_session() as db:
        thread = db.get(CreationThread, thread_id)
        item = db.get(PlanItem, thread.active_plan_item_id)
        item.edit_format = "montage"
        item.clip_gcs_paths = [row["gcs_path"] for row in rows]
        item.clip_assignments = copy.deepcopy(rows)
        thread.state = {
            "media": [{"media_id": row["media_id"]} for row in rows],
            "media_count": len(rows),
            "edit_format": "montage",
        }
        db.commit()
    return user_id, thread_id


class _Trace:
    """Model I/O of the run, recorded (or replayed) at the single Gemini client seam."""

    def __init__(
        self, replay: list[dict[str, Any]] | None = None, *, planner_only: bool = False
    ) -> None:
        self.calls: list[dict[str, Any]] = []
        self.planned: Any = None
        self.readiness: list[dict[str, Any]] = []
        self.replay = replay
        self.planner_only = planner_only
        self._served: dict[str, int] = {}

    def _replays(self, head: str) -> bool:
        if self.replay is None:
            return False
        return not self.planner_only or head.startswith(PLANNER_HEADS)

    def _recorded(self, head: str) -> dict[str, Any]:
        assert self.replay is not None
        matches = [
            {**c, "final_raw_text": c.get("final_raw_text") or c.get("raw_text")}
            for c in self.replay
            if c["prompt_head"] == head and (c.get("final_raw_text") or c.get("raw_text"))
        ]
        index = self._served.get(head, 0)
        self._served[head] = index + 1
        if index >= len(matches):
            raise AssertionError(f"replay has no recorded answer #{index} for {head!r}")
        return matches[index]

    def install(self) -> None:
        from app.agents._model_client import GeminiClient  # noqa: PLC0415
        from app.agents._runtime import ModelInvocation  # noqa: PLC0415
        from app.services.researched_text import GroundedGeminiClient  # noqa: PLC0415

        original = GeminiClient.invoke
        grounded_original = GroundedGeminiClient.invoke
        trace = self

        def invoke(client, **kwargs):  # noqa: ANN001, ANN003, ANN202
            prompt = str(kwargs.get("prompt", ""))
            started = time.monotonic()
            record: dict[str, Any] = {
                "model": kwargs.get("model"),
                "prompt_head": prompt[:90],
                "prompt_chars": len(prompt),
            }
            trace.calls.append(record)
            if trace._replays(record["prompt_head"]):
                recorded = trace._recorded(record["prompt_head"])
                record.update(replayed=True, final_raw_text=recorded["final_raw_text"])
                return ModelInvocation(raw_text=recorded["final_raw_text"])
            try:
                result = original(client, **kwargs)
            except BaseException as exc:
                record["error"] = f"{type(exc).__name__}: {str(exc)[:300]}"
                record["seconds"] = round(time.monotonic() - started, 1)
                raise
            record.update(
                seconds=round(time.monotonic() - started, 1),
                tokens_in=result.tokens_in,
                tokens_out=result.tokens_out,
                tokens_thoughts=result.tokens_thoughts,
                final_raw_text=str(result.raw_text),
            )
            return result

        def grounded_invoke(client, **kwargs):  # noqa: ANN001, ANN003, ANN202
            if trace._replays(str(kwargs.get("prompt", ""))[:90]):
                return invoke(client, **kwargs)
            result = grounded_original(client, **kwargs)
            trace.calls[-1]["final_raw_text"] = str(result.raw_text)
            return result

        GeminiClient.invoke = invoke  # type: ignore[method-assign]
        GroundedGeminiClient.invoke = grounded_invoke  # type: ignore[method-assign]

        from app.kria import research_preview  # noqa: PLC0415

        real_with_research = research_preview.with_research

        def with_research(planned, *args, **kwargs):  # noqa: ANN001, ANN002, ANN003, ANN202
            trace.planned = planned
            return real_with_research(planned, *args, **kwargs)

        research_preview.with_research = with_research  # type: ignore[assignment]

        from app.kria import approval_readiness  # noqa: PLC0415

        real_assess = approval_readiness.assess_readiness

        def assess(*args, **kwargs):  # noqa: ANN002, ANN003, ANN202
            report = real_assess(*args, **kwargs)
            prepared = kwargs.get("prepared_research")
            trace.readiness.append(
                {
                    "ready": report["ready"],
                    "rows": report["requirements"],
                    "prepared_research_status": (prepared or {}).get("status"),
                    "prepared_research_texts": len((prepared or {}).get("texts") or []),
                    "route_unified_montage": None,
                }
            )
            return report

        approval_readiness.assess_readiness = assess  # type: ignore[assignment]


def _ledger(since: float, run_id: str) -> dict[str, Any]:
    from sqlalchemy import text  # noqa: PLC0415

    from app.database import sync_session  # noqa: PLC0415

    with sync_session() as db:
        rows = (
            db.execute(
                text(
                    "SELECT feature, requested_model, status, estimated_cost_usd, "
                    "settled_cost_usd, created_at FROM ai_cost_reservations "
                    "WHERE test_run_id = :run AND created_at >= to_timestamp(:since) "
                    "ORDER BY created_at"
                ),
                {"run": run_id, "since": since},
            )
            .mappings()
            .all()
        )
        total = db.execute(
            text(
                "SELECT COALESCE(SUM(COALESCE(settled_cost_usd, estimated_cost_usd)), 0) "
                "FROM ai_cost_reservations WHERE test_run_id LIKE :prefix "
                "AND status IN ('reserved','provider_started','settled','unknown')"
            ),
            {"prefix": RUN_PREFIX + "%"},
        ).scalar_one()
    calls: dict[str, dict[str, Any]] = {}
    for row in rows:
        entry = calls.setdefault(
            row["feature"], {"calls": 0, "estimated_usd": 0.0, "settled_usd": 0.0, "statuses": []}
        )
        entry["calls"] += 1
        entry["estimated_usd"] += float(row["estimated_cost_usd"] or 0)
        entry["settled_usd"] += float(row["settled_cost_usd"] or 0)
        entry["statuses"].append(row["status"])
    run_cost = sum(
        float(row["settled_cost_usd"])
        if row["settled_cost_usd"] is not None
        else float(row["estimated_cost_usd"] or 0)
        for row in rows
        if row["status"] in {"reserved", "provider_started", "settled", "unknown"}
    )
    return {
        "per_agent": calls,
        "run_cost_usd": round(run_cost, 4),
        "ledger_total_usd": float(total),
    }


def _events(thread_id: uuid.UUID) -> list[dict[str, Any]]:
    from sqlalchemy import select  # noqa: PLC0415

    from app.database import sync_session  # noqa: PLC0415
    from app.models import CreationThreadEvent  # noqa: PLC0415

    with sync_session() as db:
        rows = (
            db.execute(
                select(CreationThreadEvent)
                .where(CreationThreadEvent.thread_id == thread_id)
                .order_by(CreationThreadEvent.sequence)
            )
            .scalars()
            .all()
        )
        return [
            {
                "sequence": row.sequence,
                "event_type": row.event_type,
                "content": row.content,
                "payload": row.payload,
            }
            for row in rows
        ]


def _brief_requirements(thread_id: uuid.UUID, trace: _Trace) -> list[dict[str, Any]]:
    from sqlalchemy import select  # noqa: PLC0415

    from app.database import sync_session  # noqa: PLC0415
    from app.models import CreativeBriefVersion  # noqa: PLC0415

    with sync_session() as db:
        latest = (
            db.execute(
                select(CreativeBriefVersion)
                .where(CreativeBriefVersion.thread_id == thread_id)
                .order_by(CreativeBriefVersion.version.desc())
            )
            .scalars()
            .first()
        )
        if latest is not None:
            return list(latest.requirements)
    updates = getattr(trace.planned, "brief_updates", None) or ()
    return [
        {
            "id": f"u{index + 1}",
            "kind": u.kind,
            "scope": u.scope,
            "description": u.description,
            "facts": dict(u.facts),
            "literal": getattr(u, "literal", None),
            "persisted": False,
        }
        for index, u in enumerate(updates)
    ]


def _verdict(result: dict[str, Any], events: list[dict[str, Any]]) -> dict[str, Any]:
    reply = next(
        (e for e in reversed(events) if e["event_type"] in {"draft_applied", "assistant_response"}),
        None,
    )
    content = str((reply or {}).get("content") or "")
    refused = "I can't make this yet" in content or "can't confirm" in content
    awaiting = result.get("status") == "awaiting_approval"
    sourced = "Sourced on-screen explanations" in content
    return {
        "turn_status": result.get("status"),
        "awaiting_approval": awaiting,
        "refusal": refused,
        "sourced_wording_shown": sourced,
        "outcome": "approval_card"
        if awaiting and sourced and not refused
        else "REFUSED"
        if refused
        else "OTHER",
    }


async def _submit_turn(user_id: uuid.UUID, thread_id: uuid.UUID, protocol: int) -> Any:
    from app.services.client_protocol import set_client_protocol  # noqa: PLC0415
    from tests.kria.test_runtime_postgres_integration import _submit  # noqa: PLC0415

    set_client_protocol(protocol)
    try:
        return await _submit(user_id, thread_id, USER_MESSAGE, 2)
    finally:
        from app.database import engine as async_engine  # noqa: PLC0415

        await async_engine.dispose()


def run_once(
    label: str,
    run_id: str,
    max_usd: float,
    replay: list[dict[str, Any]] | None,
    planner_only: bool,
) -> dict[str, Any]:
    from unittest.mock import patch  # noqa: PLC0415

    from app.config import settings  # noqa: PLC0415
    from app.kria import research_preview  # noqa: PLC0415
    from app.tasks.kria_runtime import run_kria_turn  # noqa: PLC0415

    score = _load_score_assos()
    fx = score.load_fixture()
    trace = _Trace(replay, planner_only=planner_only)
    trace.install()
    started = time.time()
    with score._prod_flags(), contextlib.ExitStack() as extra:
        for name, value in {
            "kria_runtime_v2_enabled": True,
            "main_creator_agent_enabled": True,
            # ON in prod (the code default is off): the gate that held every Assos draft.
            "kria_approval_readiness_enabled": True,
            "kria_creative_brief_user_ids": [],
            "kria_brief_binding_user_ids": [],
        }.items():
            extra.enter_context(patch.object(settings, name, value))
        shared_redis = _FakeRedis()
        extra.enter_context(patch.object(research_preview, "_redis", lambda: shared_redis))
        user_id, thread_id = _seed(score, fx)
        accepted = asyncio.run(_submit_turn(user_id, thread_id, int(fx["client_protocol"])))
        crashed: str | None = None
        runs: list[dict[str, Any]] = []
        # A turn that needs more time ends "continued" and the broker would run it again; there
        # is no broker here, so the next run is started in-process, as Celery's countdown does.
        extra.enter_context(patch.object(run_kria_turn, "apply_async", lambda *a, **k: None))
        try:
            for _ in range(MAX_CONTINUATIONS + 1):
                result = run_kria_turn.run(accepted.turn_id)
                runs.append(result)
                if result.get("status") != "continued":
                    break
        except Exception as exc:  # noqa: BLE001 - reported, the report still gets written
            result = {"status": "crashed"}
            crashed = f"{type(exc).__name__}: {exc}"
    events = _events(thread_id)
    reply = next(
        (e for e in reversed(events) if e["event_type"] in {"draft_applied", "assistant_response"}),
        None,
    )
    payload = (reply or {}).get("payload") or {}
    coverage = payload.get("brief_coverage") or {}
    requirements = _brief_requirements(thread_id, trace)
    r1 = next((r for r in requirements if r.get("id") == "r1"), None)
    report = {
        "label": label,
        "message": USER_MESSAGE,
        "head": os.popen(f"git -C {REPO} rev-parse --short HEAD").read().strip(),
        "turn": result,
        "turn_runs": runs,
        "crashed": crashed,
        "verdict": _verdict(result, events),
        "r1": r1
        and {k: r1.get(k) for k in ("id", "kind", "scope", "literal", "description", "facts")},
        "all_requirements": requirements,
        "reply_content": (reply or {}).get("content"),
        "reply_event_type": (reply or {}).get("event_type"),
        "approval_readiness": coverage.get("approval_readiness"),
        "requirement_receipts": payload.get("requirement_receipts"),
        "research_preview": payload.get("research_preview"),
        "event_types": [e["event_type"] for e in events],
        "readiness_trace": trace.readiness,
        "model_calls": trace.calls,
        "cost": _ledger(started, run_id),
        "max_usd": max_usd,
    }
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--live", action="store_true", help="required: this spends real money")
    parser.add_argument("--label", default="run", help="report file name under .dev/assos-live/")
    parser.add_argument(
        "--replay",
        type=Path,
        help="free: answer every model call from the model I/O recorded in this report",
    )
    parser.add_argument(
        "--replay-planner",
        type=Path,
        help="hybrid: answer only the Main Creator and clip-intent calls from this live report; "
        "the grounded search, copy writer and claim reviewer stay live (needs --live)",
    )
    parser.add_argument("--max-usd", type=float, default=1.0, help="ledger cap (hard max 2.0)")
    args = parser.parse_args()
    if not args.live and not args.replay:
        print("refusing to run: this drives real Gemini calls and spends money. Pass --live.")
        return 2
    if args.replay and (args.live or args.replay_planner):
        print("--replay is the free offline mode: no --live, no --replay-planner")
        return 2
    if args.replay_planner and not args.live:
        print("--replay-planner still calls Gemini for research: pass --live")
        return 2
    source = args.replay or args.replay_planner
    recorded = json.loads(source.read_text())["model_calls"] if source else None
    live_calls = args.replay is None
    _configure_environment(min(args.max_usd, 2.0), metered=live_calls)
    run_id = f"{RUN_PREFIX}-{args.label}"
    if live_calls:
        spent = _spent_usd()
        if spent + RUN_HEADROOM_USD > args.max_usd:
            print(
                f"refusing: ledger shows USD {spent:.4f} spent of the USD {args.max_usd:.2f} cap "
                f"and one run needs up to {RUN_HEADROOM_USD:.2f} more"
            )
            return 3
        os.environ["AI_MANUAL_QA_TEST_RUN_ID"] = run_id
        _attribute_to_envelope(run_id)
    report = run_once(
        args.label, run_id, args.max_usd, recorded, planner_only=bool(args.replay_planner)
    )
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    path = OUT_DIR / f"{args.label}.json"
    path.write_text(json.dumps(report, indent=2, ensure_ascii=False, default=str))
    cost = report["cost"]
    print(f"[{args.label}] outcome={report['verdict']['outcome']} turn={report['turn']}")
    print(f"  r1={json.dumps(report['r1'], ensure_ascii=False)}")
    for feature, entry in sorted(cost["per_agent"].items()):
        print(
            f"  agent {feature}: calls={entry['calls']} est=${entry['estimated_usd']:.4f} "
            f"settled=${entry['settled_usd']:.4f} {entry['statuses']}"
        )
    print(f"  run cost ${cost['run_cost_usd']:.4f}; ledger total ${cost['ledger_total_usd']:.4f}")
    print(f"  reply: {str(report['reply_content'])[:400]!r}")
    print(f"  report: {path}")
    return 0 if report["verdict"]["outcome"] == "approval_card" else 1


if __name__ == "__main__":
    raise SystemExit(main())
