"""Runtime-v2 planner adapter over Nova's production creative intelligence.

The planner returns inert intents only.  It never receives storage paths and
cannot author risk, target pins, idempotency identities, or completion claims.
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents._model_client import default_client
from app.agents._runtime import RunContext, TerminalError
from app.agents._schemas.creator_agent import AskUser, ProposeStrategy, ReviewDecision
from app.agents.main_creator import MainCreatorAgent, MainCreatorInput
from app.kria.contracts import KriaTurnPlan
from app.models import (
    ContentPlan,
    CreationThread,
    CreationThreadEvent,
    CreatorAgentSession,
    Job,
    Persona,
    PlanItem,
)
from app.routes._copilot import CopilotTurnBody, run_copilot_turn
from app.services.creator_sessions import creator_context, resolve_item_creator_context
from app.services.kria_editor_ops import build_editor_snapshot


@dataclass(frozen=True)
class PlannedKriaTurn:
    plan: KriaTurnPlan
    manifest_hash: str | None
    context_hash: str | None


def adapt_creator_action(action: AskUser | ProposeStrategy | ReviewDecision) -> KriaTurnPlan:
    if isinstance(action, AskUser):
        return KriaTurnPlan(
            mode="respond",
            turn_value="question",
            response=action.question,
        )
    if isinstance(action, ReviewDecision):
        return KriaTurnPlan(
            mode="respond",
            turn_value="review",
            response=action.summary or "The current cut is ready for your review.",
        )
    summary = action.summary.strip() or action.strategy.rationale.strip()
    if not summary:
        summary = "I shaped a focused draft around the strongest available footage."
    return KriaTurnPlan(
        mode="act",
        turn_value="action",
        evidence_ids=["trusted-project-manifest"],
        intents=[
            {
                "intent_id": "apply-strategy",
                "tool_name": "draft.apply_strategy",
                "tool_version": 1,
                "arguments": {
                    "strategy": action.strategy.model_dump(mode="json", exclude_none=True),
                    "summary": summary,
                },
            },
            {
                "intent_id": "request-render",
                "tool_name": "render.request",
                "tool_version": 1,
                "arguments": {},
                "depends_on": ["apply-strategy"],
            },
        ],
    )


def adapt_editor_action(*, reply: str, ops: list[dict]) -> KriaTurnPlan:
    if not ops:
        return KriaTurnPlan(mode="respond", turn_value="question", response=reply)
    return KriaTurnPlan(
        mode="act",
        turn_value="action",
        evidence_ids=["trusted-editor-snapshot"],
        intents=[
            {
                "intent_id": "apply-editor-ops",
                "tool_name": "draft.apply_editor_ops",
                "tool_version": 1,
                "arguments": {"operations": ops, "summary": reply},
            },
            {
                "intent_id": "request-render",
                "tool_name": "render.request",
                "tool_version": 1,
                "arguments": {},
                "depends_on": ["apply-editor-ops"],
            },
        ],
    )


async def _plan_editor_revision(
    db: AsyncSession,
    *,
    thread_id: uuid.UUID,
    item: PlanItem,
    user_message: str,
) -> KriaTurnPlan | None:
    if item.current_job_id is None:
        return None
    thread = await db.get(CreationThread, thread_id)
    if thread is None or thread.active_creator_agent_session_id is None:
        return None
    session = await db.get(CreatorAgentSession, thread.active_creator_agent_session_id)
    job = await db.get(Job, item.current_job_id)
    if session is None or job is None or session.target_job_id != job.id:
        return None
    variants = [
        row for row in (job.assembly_plan or {}).get("variants") or [] if isinstance(row, dict)
    ]
    variant = next(
        (
            row
            for row in variants
            if row.get("variant_id") == session.target_variant_id
            and row.get("render_status") == "ready"
        ),
        None,
    )
    if variant is None:
        return None
    snapshot = build_editor_snapshot(job, variant)
    if not snapshot["allowed_op_families"]:
        return None
    rows = list(
        (
            await db.execute(
                select(CreationThreadEvent)
                .where(
                    CreationThreadEvent.thread_id == thread_id,
                    CreationThreadEvent.role.in_({"user", "assistant"}),
                    CreationThreadEvent.content.is_not(None),
                )
                .order_by(CreationThreadEvent.sequence.desc())
                .limit(12)
            )
        )
        .scalars()
        .all()
    )
    rows.reverse()
    conversation = [
        {"role": row.role, "content": str(row.content)[:1000]} for row in rows if row.content
    ]
    job_id = job.id
    # Release the read transaction before Copilot model I/O. The response is
    # derived only from the immutable snapshot and copied conversation rows.
    await db.rollback()
    response = await run_copilot_turn(
        CopilotTurnBody(
            message=user_message,
            turns=conversation,
            snapshot=snapshot,
            client_contract_version=2,
        ),
        job_id=job_id,
    )
    if response.ops:
        return adapt_editor_action(reply=response.reply, ops=response.ops)
    if response.outcome in {"clarification", "unsupported", "stale", "failed", "no_effect"}:
        return KriaTurnPlan(
            mode="respond",
            turn_value=("question" if response.outcome == "clarification" else "recovery"),
            response=response.reply,
        )
    return None


async def plan_live_turn(
    db: AsyncSession,
    *,
    thread_id: uuid.UUID,
    item_id: uuid.UUID,
    creator_id: uuid.UUID,
    user_message: str,
) -> PlannedKriaTurn:
    item = await db.get(PlanItem, item_id)
    if item is None:
        raise RuntimeError("Kria target item is unavailable")
    plan = await db.get(ContentPlan, item.content_plan_id)
    if plan is None or plan.user_id != creator_id:
        raise RuntimeError("Kria target item ownership changed")
    persona = await db.get(Persona, plan.persona_id)
    if persona is None or persona.user_id != creator_id:
        raise RuntimeError("Kria creator context is unavailable")
    manifest, media_context = await resolve_item_creator_context(db, item, persona=persona)
    editor_plan = await _plan_editor_revision(
        db,
        thread_id=thread_id,
        item=item,
        user_message=user_message,
    )
    if editor_plan is not None:
        return PlannedKriaTurn(
            plan=editor_plan,
            manifest_hash=manifest.manifest_hash,
            context_hash=manifest.context_hash,
        )
    if not manifest.capabilities["dispatch_render"].available:
        return PlannedKriaTurn(
            plan=KriaTurnPlan(
                mode="respond",
                turn_value="question",
                response=(
                    "Add at least one clip and I can shape the edit around what is actually there."
                ),
            ),
            manifest_hash=manifest.manifest_hash,
            context_hash=manifest.context_hash,
        )
    rows = list(
        (
            await db.execute(
                select(CreationThreadEvent)
                .where(
                    CreationThreadEvent.thread_id == thread_id,
                    CreationThreadEvent.role.in_({"user", "assistant"}),
                    CreationThreadEvent.content.is_not(None),
                )
                .order_by(CreationThreadEvent.sequence.desc())
                .limit(24)
            )
        )
        .scalars()
        .all()
    )
    rows.reverse()
    creator_summary, item_summary = creator_context(persona, item)
    agent_input = MainCreatorInput(
        user_message=user_message,
        creator_context=creator_summary,
        item_context=item_summary,
        media_context=media_context,
        conversation=[
            {"role": row.role, "content": str(row.content)[:1000]} for row in rows if row.content
        ],
        capability_manifest=manifest,
    )
    # Do not pin an async DB connection or block the event loop that renews the
    # durable turn lease while the synchronous model client is in flight.
    await db.rollback()

    def _run_agent():  # noqa: ANN202 - inferred MainCreatorOutput
        return MainCreatorAgent(default_client()).run(
            agent_input,
            ctx=RunContext(
                request_id=str(thread_id),
                creator_id=str(creator_id),
            ),
        )

    try:
        output = await asyncio.to_thread(_run_agent)
    except TerminalError as exc:
        raise RuntimeError("Kria could not produce a reliable editorial plan") from exc
    return PlannedKriaTurn(
        plan=adapt_creator_action(output.action),
        manifest_hash=manifest.manifest_hash,
        context_hash=manifest.context_hash,
    )


__all__ = [
    "PlannedKriaTurn",
    "adapt_creator_action",
    "adapt_editor_action",
    "plan_live_turn",
]
