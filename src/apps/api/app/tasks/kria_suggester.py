"""Rank the suggestion cards for one Kria creation thread (KRI-756, AI suggestion blocks).

``tasks.kria_suggester`` runs the suggestion agent (``nova.chat.suggestion_agent``, OpenAI
``KRIA_SUGGESTER_MODEL`` with a strict schema) over the kinds the server issued, and caches its
picks in Redis under ``kria:suggester:{thread}:{input_digest}`` (24 h). The agent only picks;
the GET route rebuilds every card from the registry and proves it with the kind's witness, so a
stored pick can never carry an action. Nothing is written to ``thread.state`` from here.

Triggers (all enqueue-only, see ``app/services/suggestion_serve.py``):

* clip understanding finished for a plan item (first_prompt picks),
* a render settled ready (ready picks; the run is wrapped in ``pipeline_trace_for(job_id)``),
* lazily from the GET on a cache miss (deduped by a Redis lock).

Contract: best-effort and NEVER fatal. Every exit that is not a cached agent result writes a
fallback marker, so the GET serves the deterministic set immediately instead of waiting out its
6 s window. Each run re-checks the flag, the per-thread run cap
(``KRIA_SUGGESTER_MAX_RUNS_PER_THREAD``), the per-call cap (``KRIA_SUGGESTER_MAX_USD``) and, in
``ai_cost_control``, the ``suggestions`` credit budget (``AI_OPENAI_SUGGESTIONS_BUDGET_USD``, 0 =
no spend) and the per-user daily limit (``KRIA_SUGGESTER_DAILY_LIMIT_PER_USER``). Kill switch:
``KRIA_SUGGESTION_CHIPS_ENABLED=false`` (everything) or ``KRIA_SUGGESTER_MAX_RUNS_PER_THREAD=0``
(agent only) + a worker restart.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from contextlib import nullcontext
from typing import Any

import structlog
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.agents._runtime import (
    AiBudgetExceededError,
    AiCostControlPolicyError,
    CostControlUnavailableError,
    ProviderOutcomeUnknownError,
    ProviderQuotaExceededError,
    RefusalError,
    RunContext,
    SchemaError,
    TerminalError,
    TerminalOutputTruncatedError,
)
from app.config import settings
from app.models import CreationThread
from app.services import suggestion_store as store
from app.services.pipeline_trace import pipeline_trace_for
from app.services.suggestion_chips import (
    PROMPT_VERSION,
    CandidatePlan,
    SuggestionContext,
    build_agent_input,
    build_candidates,
    load_suggestion_context,
    pick_stats,
)
from app.services.suggestion_serve import needs_wording, with_transcripts
from app.worker import celery_app

log = structlog.get_logger()

# Celery limits (literals in the decorator below, read by tests/tasks/test_task_time_limits.py):
# two 12 s model attempts + backoff + context load, far under the broker's visibility_timeout.
INFLIGHT_TTL_S = 90


def _fallback_marker(reason: str) -> dict[str, Any]:
    return store.fallback_result(reason, prompt_version=PROMPT_VERSION)


def _fallback_reason(exc: BaseException) -> str:
    """Why a run produced no picks. Closed set: budget | refusal | truncated | schema_error |
    provider_error | timeout. Every one of them serves the deterministic set; the slug is
    telemetry. The agent is ``sensitive_io``, so the runtime rewraps a refusal and any other
    uncategorised terminal failure into a plain ``TerminalError`` named by its message; an
    exhausted transient retry ("exhausted 1 model(s)") is the timeout / rate-limit case."""
    if isinstance(
        exc,
        (
            AiBudgetExceededError,
            AiCostControlPolicyError,
            CostControlUnavailableError,
            ProviderQuotaExceededError,
            ProviderOutcomeUnknownError,
        ),
    ):
        return "budget"
    if isinstance(exc, RefusalError) or str(exc).endswith(": refusal"):
        return "refusal"
    if isinstance(exc, TerminalOutputTruncatedError):
        return "truncated"
    if isinstance(exc, SchemaError) or (
        isinstance(exc, TerminalError) and "schema" in type(exc).__name__.lower()
    ):
        return "schema_error"
    if type(exc) is TerminalError and "exhausted" not in str(exc):
        return "provider_error"
    return "timeout"


async def _prepare(
    thread_id: str, language: str | None, protocol: int | None
) -> tuple[SuggestionContext, CandidatePlan] | str:
    """Load the thread and build its candidates. A str is a skip reason."""
    # Each run plans inside a fresh `asyncio.run` loop and asyncpg connections cannot cross
    # loops: use an unpooled engine that lives for this loop only (as run_kria_turn does).
    engine = create_async_engine(settings.asyncpg_database_url, poolclass=NullPool)
    try:
        async with async_sessionmaker(engine, expire_on_commit=False)() as db:
            thread = await db.get(CreationThread, uuid.UUID(thread_id))
            if thread is None:
                return "ignored"
            if not settings.suggestion_chips_for(thread.creator_id):
                return "flag_off"
            ctx = await load_suggestion_context(db, thread, None, protocol, language_hint=language)
            ctx = await with_transcripts(db, thread, ctx)
            return ctx, build_candidates(ctx)
    finally:
        await engine.dispose()


def _event(name: str, **fields: Any) -> None:
    log.info(f"suggestion_chips.{name}", **fields)


@celery_app.task(
    bind=True,
    name="tasks.kria_suggester",
    soft_time_limit=60,
    time_limit=90,
    max_retries=0,
)
def kria_suggester(  # noqa: ANN201
    self,  # noqa: ANN001
    thread_id: str,
    trigger: str = "lazy",
    expected_digest: str | None = None,
    language: str | None = None,
    protocol: int | None = None,
):
    """Rank the cards for one thread and cache the picks. Idempotent per digest."""
    if not (settings.kria_suggestion_chips_enabled or settings.kria_suggestion_chips_user_ids):
        return {"thread_id": thread_id, "status": "flag_off"}
    if settings.kria_suggester_max_runs_per_thread <= 0:
        return {"thread_id": thread_id, "status": "run_cap"}
    if language is None or protocol is None:
        hint = store.get_hint(thread_id)
        language = language or hint.get("language")
        protocol = protocol if protocol is not None else hint.get("protocol")
    try:
        prepared = asyncio.run(_prepare(thread_id, language, protocol))
    except Exception:  # noqa: BLE001 - best-effort background work
        log.warning(
            "suggestion_chips.error", stage="worker_load", thread_id=thread_id, exc_info=True
        )
        return {"thread_id": thread_id, "status": "failed"}
    if isinstance(prepared, str):
        return {"thread_id": thread_id, "status": prepared}
    ctx, plan = prepared
    if plan.state == "none" or not needs_wording(ctx, plan):
        return {"thread_id": thread_id, "status": "nothing_to_word"}
    if plan.state == "first_prompt" and ctx.understanding != "complete":
        return {"thread_id": thread_id, "status": "not_ready"}
    digest = plan.input_digest
    result: dict[str, Any] = {"thread_id": thread_id, "status": "ok", "digest": digest}
    held = False
    try:
        if expected_digest and expected_digest != digest:
            # The footage or edit moved on (or the GET saw another language/protocol) while this
            # run waited in the queue: the digest the creator is served will get its own run.
            _event(
                "agent_run",
                thread_id=thread_id,
                set_id=plan.set_id,
                trigger=trigger,
                fallback_reason="stale_digest",
            )
            return {**result, "status": "stale_digest"}
        if store.get_result(thread_id, digest) is not None:
            return {**result, "status": "cached"}
        if not store.try_lock(thread_id, digest, INFLIGHT_TTL_S, scope="inflight"):
            return {**result, "status": "busy"}
        held = True
        if store.bump_runs(thread_id) > settings.kria_suggester_max_runs_per_thread:
            store.put_result(thread_id, digest, _fallback_marker("run_cap"))
            _event(
                "agent_run",
                thread_id=thread_id,
                set_id=plan.set_id,
                trigger=trigger,
                fallback_reason="run_cap",
            )
            return {**result, "status": "run_cap"}
        return {**result, **_run_agent(thread_id, ctx, plan, trigger)}
    except store.StoreUnavailable:
        log.warning("suggestion_chips.error", stage="worker_store", thread_id=thread_id)
        return {**result, "status": "store_unavailable"}
    finally:
        if held:
            store.release_lock(thread_id, digest, scope="inflight")


def _run_agent(
    thread_id: str, ctx: SuggestionContext, plan: CandidatePlan, trigger: str
) -> dict[str, Any]:
    from app.agents._model_client import default_client  # noqa: PLC0415
    from app.agents.suggestion_agent import SuggestionAgent, SuggestionAgentInput  # noqa: PLC0415

    digest = plan.input_digest
    request_id = f"kria-suggest:{thread_id}:{digest}"
    agent = SuggestionAgent(default_client())
    run_ctx = RunContext(
        creator_id=ctx.creator_id,
        request_id=request_id,
        usage_purpose="optional_background",
        # A ready run belongs to the render it ranks cards for (admin job-debug view).
        job_id=ctx.job_id if plan.state == "ready" else None,
    )
    started = time.monotonic()
    trace = (
        pipeline_trace_for(ctx.job_id) if plan.state == "ready" and ctx.job_id else nullcontext()
    )

    def fail(reason: str, **extra: Any) -> dict[str, Any]:
        store.put_result(thread_id, digest, _fallback_marker(reason))
        _event(
            "agent_run",
            agent_run_id=request_id,
            thread_id=thread_id,
            set_id=plan.set_id,
            trigger=trigger,
            model=getattr(agent, "last_run", {}).get("model"),
            prompt_version=PROMPT_VERSION,
            latency_ms=int((time.monotonic() - started) * 1000),
            n_in=len(plan.pool),
            cost_usd=getattr(agent, "last_run", {}).get("cost_usd"),
            fallback_reason=reason,
            **extra,
        )
        return {"status": "fallback", "fallback_reason": reason}

    try:
        payload = SuggestionAgentInput.model_validate(build_agent_input(ctx, plan))
        # The per-call cap is checked before any ledger or provider contact: an oversized
        # payload is a deterministic set, not a spend.
        if agent.estimated_cost_usd(payload) > settings.kria_suggester_max_usd:
            return fail("budget", over_call_cap=True)
        with trace:
            output = agent.run(payload, ctx=run_ctx)
    except Exception as exc:  # noqa: BLE001 - a failed run is a deterministic set, never an error
        return fail(_fallback_reason(exc))

    stats = pick_stats(ctx, plan, output)
    if stats.n_out == 0:
        reason = "empty_ranking" if not output.ranked else "no_usable_picks"
        return fail(reason, n_out=0, n_dropped=stats.n_in, drop_reasons=stats.drops)
    store.put_result(
        thread_id,
        digest,
        store.ok_result(
            output, prompt_version=PROMPT_VERSION, model=getattr(agent, "last_run", {}).get("model")
        ),
    )
    _event(
        "agent_run",
        agent_run_id=request_id,
        thread_id=thread_id,
        set_id=plan.set_id,
        trigger=trigger,
        model=getattr(agent, "last_run", {}).get("model"),
        prompt_version=PROMPT_VERSION,
        latency_ms=int((time.monotonic() - started) * 1000),
        n_in=len(plan.pool),
        n_out=stats.n_out,
        n_dropped=stats.n_in - stats.n_out,
        drop_reasons=stats.drops,
        cost_usd=getattr(agent, "last_run", {}).get("cost_usd"),
    )
    return {"status": "ok"}
