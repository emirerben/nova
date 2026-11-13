"""Fleet report: what every agent and model costs, how fast it is, and how often it fails.

One SQL aggregation per section over a ``created_at`` window (``idx_agent_run_created_at``,
``idx_ai_cost_reservations_created_at``), so the report never loads rows. "Failed" is any
outcome outside ``SUCCESS_OUTCOMES``; ``ok_fallback`` counts as a success and, separately, as a
fallback.

Three scopes, because no single table has all of it:

- ``agents``: ``agent_run``, one row per agent invocation (attempts inside one run are folded
  into it). Owner-less runs are included.
- ``chat_turns``: ``edit_interaction_receipts``, which exist only for training-eligible
  creators, so they are a sample of chat turns, not all of them.
- ``ledger``: ``ai_cost_reservations``, every paid call that reserved, including the raw OpenAI
  calls that have no ``agent_run`` row (captions, lyrics Whisper). Compare its OpenAI total
  with the provider dashboard.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel
from sqlalchemy import bindparam, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents._runtime import SUCCESS_OUTCOMES

_WINDOW = "created_at >= now() - make_interval(days => :days)"
_APPLIED = ("applied", "proposed")
_CHAT_BUCKETS = ("applied", "clarification", "unsupported", "failed")


class AgentModelRow(BaseModel):
    agent_name: str
    model: str
    calls: int
    settled_cost_usd: float
    latency_p50_ms: float | None
    latency_p95_ms: float | None
    failed_calls: int
    failed_share: float
    fallback_calls: int
    fallback_share: float
    outcomes: dict[str, int]


class UnownedRuns(BaseModel):
    total: int
    by_agent: dict[str, int]


class ChatTurnRow(BaseModel):
    model: str
    total: int
    applied: int
    clarification: int
    unsupported: int
    failed: int
    other: int


class ChatTurns(BaseModel):
    scope: str
    total: int
    applied: int
    clarification: int
    unsupported: int
    failed: int
    other: int
    by_proposal_outcome: dict[str, int]
    by_model: list[ChatTurnRow]


class LedgerRow(BaseModel):
    provider: str
    feature: str
    reservations: int
    settled: int
    released: int
    unknown: int
    settled_cost_usd: float
    unsettled_estimate_usd: float


class FleetReport(BaseModel):
    days: int
    environment: str | None
    retention_days: int
    window_exceeds_retention: bool
    success_outcomes: list[str]
    agents: list[AgentModelRow]
    unowned_runs: UnownedRuns
    chat_turns: ChatTurns
    ledger: list[LedgerRow]
    ledger_settled_by_provider: dict[str, float]


def _share(part: int, whole: int) -> float:
    return round(part / whole, 4) if whole else 0.0


def _num(value: Any) -> float | None:
    return None if value is None else round(float(value), 1)


async def _agents(
    db: AsyncSession, days: int, environment: str | None
) -> tuple[list[AgentModelRow], UnownedRuns]:
    env = " AND environment = :environment" if environment else ""
    params: dict[str, Any] = {"days": days, "success": sorted(SUCCESS_OUTCOMES)}
    if environment:
        params["environment"] = environment

    totals = (
        await db.execute(
            text(
                f"""
                SELECT agent_name, model,
                       count(*) AS calls,
                       COALESCE(sum(settled_cost_usd), 0) AS settled_cost_usd,
                       percentile_cont(0.5) WITHIN GROUP (ORDER BY latency_ms) AS p50,
                       percentile_cont(0.95) WITHIN GROUP (ORDER BY latency_ms) AS p95,
                       count(*) FILTER (WHERE outcome NOT IN :success) AS failed_calls,
                       count(*) FILTER (WHERE outcome = 'ok_fallback') AS fallback_calls
                FROM agent_run
                WHERE {_WINDOW}{env}
                GROUP BY agent_name, model
                ORDER BY settled_cost_usd DESC, calls DESC, agent_name, model
                """  # noqa: S608 - fixed fragments only; every value is a bound parameter
            ).bindparams(bindparam("success", expanding=True)),
            params,
        )
    ).mappings()
    outcomes = (
        await db.execute(
            text(
                f"""
                SELECT agent_name, model, outcome, count(*) AS n
                FROM agent_run WHERE {_WINDOW}{env}
                GROUP BY agent_name, model, outcome
                """  # noqa: S608 - fixed fragments only; every value is a bound parameter
            ),
            {key: value for key, value in params.items() if key != "success"},
        )
    ).mappings()
    by_pair: dict[tuple[str, str], dict[str, int]] = {}
    for row in outcomes:
        by_pair.setdefault((row["agent_name"], row["model"]), {})[row["outcome"]] = int(row["n"])

    agents = [
        AgentModelRow(
            agent_name=row["agent_name"],
            model=row["model"],
            calls=int(row["calls"]),
            settled_cost_usd=round(float(row["settled_cost_usd"]), 6),
            latency_p50_ms=_num(row["p50"]),
            latency_p95_ms=_num(row["p95"]),
            failed_calls=int(row["failed_calls"]),
            failed_share=_share(int(row["failed_calls"]), int(row["calls"])),
            fallback_calls=int(row["fallback_calls"]),
            fallback_share=_share(int(row["fallback_calls"]), int(row["calls"])),
            outcomes=dict(sorted(by_pair.get((row["agent_name"], row["model"]), {}).items())),
        )
        for row in totals
    ]

    unowned = (
        await db.execute(
            text(
                f"""
                SELECT agent_name, count(*) AS n FROM agent_run
                WHERE {_WINDOW}{env} AND owner_label IS NOT NULL
                GROUP BY agent_name ORDER BY n DESC, agent_name
                """  # noqa: S608 - fixed fragments only; every value is a bound parameter
            ),
            {key: value for key, value in params.items() if key != "success"},
        )
    ).mappings()
    by_agent = {row["agent_name"]: int(row["n"]) for row in unowned}
    return agents, UnownedRuns(total=sum(by_agent.values()), by_agent=by_agent)


def _bucket(outcome: str) -> str:
    if outcome in _APPLIED:
        return "applied"
    return outcome if outcome in _CHAT_BUCKETS else "other"


async def _chat_turns(db: AsyncSession, days: int) -> ChatTurns:
    rows = (
        await db.execute(
            text(
                f"""
                SELECT model, proposal_outcome, count(*) AS n
                FROM edit_interaction_receipts
                WHERE event_kind = 'proposal' AND {_WINDOW}
                GROUP BY model, proposal_outcome
                """  # noqa: S608 - fixed fragments only; every value is a bound parameter
            ),
            {"days": days},
        )
    ).mappings()
    overall = dict.fromkeys((*_CHAT_BUCKETS, "other"), 0)
    per_model: dict[str, dict[str, int]] = {}
    raw: dict[str, int] = {}
    for row in rows:
        n = int(row["n"])
        bucket = _bucket(row["proposal_outcome"])
        overall[bucket] += n
        raw[row["proposal_outcome"]] = raw.get(row["proposal_outcome"], 0) + n
        per_model.setdefault(row["model"], dict.fromkeys((*_CHAT_BUCKETS, "other"), 0))[bucket] += n
    return ChatTurns(
        scope="training-eligible creators only: receipts are written only for them",
        total=sum(overall.values()),
        by_proposal_outcome=dict(sorted(raw.items())),
        by_model=[
            ChatTurnRow(model=model, total=sum(counts.values()), **counts)
            for model, counts in sorted(per_model.items())
        ],
        **overall,
    )


async def _ledger(db: AsyncSession, days: int) -> list[LedgerRow]:
    rows = (
        await db.execute(
            text(
                f"""
                SELECT provider, feature,
                       count(*) AS reservations,
                       count(*) FILTER (WHERE status = 'settled') AS settled,
                       count(*) FILTER (WHERE status = 'released') AS released,
                       count(*) FILTER (WHERE status = 'unknown') AS unknown,
                       COALESCE(sum(settled_cost_usd) FILTER (WHERE status = 'settled'), 0)
                           AS settled_cost_usd,
                       COALESCE(sum(estimated_cost_usd)
                           FILTER (WHERE status IN ('reserved', 'provider_started', 'unknown')), 0)
                           AS unsettled_estimate_usd
                FROM ai_cost_reservations
                WHERE {_WINDOW}
                GROUP BY provider, feature
                ORDER BY settled_cost_usd DESC, provider, feature
                """  # noqa: S608 - fixed fragments only; every value is a bound parameter
            ),
            {"days": days},
        )
    ).mappings()
    return [
        LedgerRow(
            provider=row["provider"],
            feature=row["feature"],
            reservations=int(row["reservations"]),
            settled=int(row["settled"]),
            released=int(row["released"]),
            unknown=int(row["unknown"]),
            settled_cost_usd=round(float(row["settled_cost_usd"]), 6),
            unsettled_estimate_usd=round(float(row["unsettled_estimate_usd"]), 6),
        )
        for row in rows
    ]


async def build_fleet_report(
    db: AsyncSession, *, days: int, environment: str | None, retention_days: int
) -> FleetReport:
    agents, unowned = await _agents(db, days, environment)
    ledger = await _ledger(db, days)
    by_provider: dict[str, float] = {}
    for row in ledger:
        by_provider[row.provider] = round(
            by_provider.get(row.provider, 0.0) + row.settled_cost_usd, 6
        )
    return FleetReport(
        days=days,
        environment=environment,
        retention_days=retention_days,
        window_exceeds_retention=days > retention_days,
        success_outcomes=sorted(SUCCESS_OUTCOMES),
        agents=agents,
        unowned_runs=unowned,
        chat_turns=await _chat_turns(db, days),
        ledger=ledger,
        ledger_settled_by_provider=dict(sorted(by_provider.items())),
    )
