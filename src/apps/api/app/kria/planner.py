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
from app.config import settings
from app.kria.contracts import KriaTurnPlan
from app.models import (
    ContentPlan,
    CreationThread,
    CreationThreadEvent,
    CreatorAgentSession,
    CreatorEditDraft,
    Job,
    Persona,
    PlanItem,
)
from app.routes._copilot import CopilotTurnBody, run_copilot_turn
from app.schemas.clip_intents import ClipIntent, ResolvedClipIntent
from app.services.clip_intent_answers import persist_clip_intent_vision_answers
from app.services.clip_intent_planning import plan_and_resolve_clip_intents
from app.services.creator_sessions import (
    creator_context,
    load_intent_clips_for_item,
    resolve_item_creator_context,
)
from app.services.kria_editor_ops import build_editor_snapshot, project_editor_draft


@dataclass(frozen=True)
class PlannedKriaTurn:
    plan: KriaTurnPlan
    manifest_hash: str | None
    context_hash: str | None


def adapt_creator_action(
    action: AskUser | ProposeStrategy | ReviewDecision,
    *,
    server_clip_intents: list[ClipIntent] | None = None,
    server_resolved_clip_intents: list[ResolvedClipIntent] | None = None,
) -> KriaTurnPlan:
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
    server_owned_intents = (
        server_clip_intents is not None or server_resolved_clip_intents is not None
    )
    requested_intents = (
        server_clip_intents
        if server_owned_intents
        else [
            intent
            for intent in (action.strategy.clip_intents or [])
            if intent.label_source == "transcript"
        ]
    )
    strategy_update = {
        "clip_intents": requested_intents or None,
        "resolved_clip_intents": server_resolved_clip_intents or None,
    }
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
                    # KRI-127: model-authored intent fields are untrusted.
                    # This path accepts values only from the server resolver.
                    "strategy": action.strategy.model_copy(update=strategy_update).model_dump(
                        mode="json", exclude_none=True
                    ),
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


def _full_creator_request(rows: list[CreationThreadEvent], *, current_message: str) -> str | None:
    """Return complete chronological user instruction text or fail closed.

    Intent extraction must see every creator instruction. Unlike the bounded
    model conversation, this input is never truncated: a request that exceeds
    the planner's safe limit gets a recovery turn instead of silently losing
    an earlier constraint.
    """
    messages = [
        str(row.content).strip()
        for row in rows
        if row.role == "user" and row.content and str(row.content).strip()
    ]
    current = current_message.strip()
    if current and messages and messages[-1] == current:
        messages.pop()
    if current:
        messages.append(current)
    request = "\n".join(messages)
    return request if len(request) <= 12_000 else None


def _clip_intent_resolution_plan(*, question: str | None, status: str) -> KriaTurnPlan:
    if status == "needs_creator":
        return KriaTurnPlan(
            mode="respond",
            turn_value="question",
            response=question or "Which clips should I use for that part?",
        )
    return KriaTurnPlan(
        mode="respond",
        turn_value="recovery",
        response="I couldn't reliably match that request to your clips. Please try again shortly.",
    )


def adapt_editor_action(
    *, reply: str, ops: list[dict], request_render: bool = False
) -> KriaTurnPlan:
    """Draft edits are reversible; rendering is a distinct, policy-gated action."""
    if not ops:
        return KriaTurnPlan(mode="respond", turn_value="question", response=reply)
    intents = [
        {
            "intent_id": "apply-editor-ops",
            "tool_name": "draft.apply_editor_ops",
            "tool_version": 1,
            "arguments": {"operations": ops, "summary": reply},
        },
    ]
    if request_render:
        intents.append(
            {
                "intent_id": "request-render",
                "tool_name": "render.request",
                "tool_version": 1,
                "arguments": {},
                "depends_on": ["apply-editor-ops"],
            }
        )
    return KriaTurnPlan(
        mode="act", turn_value="action", evidence_ids=["trusted-editor-snapshot"], intents=intents
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
    head = (
        await db.execute(
            select(CreatorEditDraft).where(
                CreatorEditDraft.item_id == session.plan_item_id,
                CreatorEditDraft.variant_key == session.target_variant_id,
                CreatorEditDraft.base_job_id == job.id,
                CreatorEditDraft.base_generation_id == session.target_generation_id,
                CreatorEditDraft.is_head.is_(True),
            )
        )
    ).scalar_one_or_none()
    if head is not None and (head.snapshot_json or {}).get("kind") == "editor":
        variant = project_editor_draft(variant, head.snapshot_json.get("editor_payload") or {})
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
        return adapt_editor_action(
            reply=response.reply,
            ops=response.ops,
            # This portable operation invokes server speech processing; ordinary
            # text/timeline/mix edits stay drafts until an explicit Save.
            request_render=any(op.get("op") == "apply_speech_cut_candidate" for op in response.ops),
        )
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
    intent_clips = []
    creator_request: str | None = None
    if settings.clip_intents_enabled:
        # This deliberately has no row limit or per-message truncation. The
        # inventory agent must see every creator instruction; a request over
        # the bound is rejected below rather than silently dropping context.
        creator_rows = list(
            (
                await db.execute(
                    select(CreationThreadEvent)
                    .where(
                        CreationThreadEvent.thread_id == thread_id,
                        CreationThreadEvent.role == "user",
                        CreationThreadEvent.content.is_not(None),
                    )
                    .order_by(CreationThreadEvent.sequence)
                )
            )
            .scalars()
            .all()
        )
        creator_request = _full_creator_request(creator_rows, current_message=user_message)
        if creator_request is None:
            return PlannedKriaTurn(
                plan=KriaTurnPlan(
                    mode="respond",
                    turn_value="recovery",
                    response=(
                        "Your edit instructions are too long for me to match safely. "
                        "Please start a new request with the key clip directions."
                    ),
                ),
                manifest_hash=manifest.manifest_hash,
                context_hash=manifest.context_hash,
            )
        # Capture DB-backed clip identity before releasing the transaction for
        # the external planner/resolver calls below.
        intent_clips = await load_intent_clips_for_item(db, item, persona)
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
    if settings.clip_intents_enabled and isinstance(output.action, ProposeStrategy):
        try:
            planned = await plan_and_resolve_clip_intents(
                creator_request=creator_request or user_message,
                latest_user_message=user_message,
                candidate_intents=output.action.strategy.clip_intents,
                clips=intent_clips,
                run_context=RunContext(
                    request_id=str(thread_id),
                    creator_id=str(creator_id),
                ),
            )
        except Exception:  # noqa: BLE001 - no provider failure may mint a draft
            return PlannedKriaTurn(
                plan=_clip_intent_resolution_plan(question=None, status="provider_unavailable"),
                manifest_hash=manifest.manifest_hash,
                context_hash=manifest.context_hash,
            )
        if any(intent.label_source == "transcript" for intent in planned.requested_intents) and (
            manifest.narration is None
            or output.action.strategy.execution_contract != "guided_voiceover_v1"
        ):
            return PlannedKriaTurn(
                plan=_clip_intent_resolution_plan(
                    question="Those labels need a recorded voiceover with guided visuals.",
                    status="needs_creator",
                ),
                manifest_hash=manifest.manifest_hash,
                context_hash=manifest.context_hash,
            )
        if planned.resolution.vision_answers:
            try:
                # `rollback()` before provider I/O expires ORM instances. Reload
                # the item before the cache writer reads its id, and fail closed
                # if the target disappeared while the provider was running.
                current_item = await db.get(PlanItem, item_id, populate_existing=True)
                if current_item is None:
                    return PlannedKriaTurn(
                        plan=_clip_intent_resolution_plan(
                            question=None, status="provider_unavailable"
                        ),
                        manifest_hash=manifest.manifest_hash,
                        context_hash=manifest.context_hash,
                    )
                await persist_clip_intent_vision_answers(
                    db,
                    current_item,
                    planned.resolution.vision_answers,
                    creator_id=creator_id,
                    strict=True,
                )
                await db.commit()
            except Exception:  # noqa: BLE001 - cache failure must not mint a draft
                await db.rollback()
                return PlannedKriaTurn(
                    plan=_clip_intent_resolution_plan(question=None, status="provider_unavailable"),
                    manifest_hash=manifest.manifest_hash,
                    context_hash=manifest.context_hash,
                )
        if (
            planned.resolution.status != "resolved"
            or planned.resolution.needs_creator
            or planned.resolution.deferred_queries
        ):
            return PlannedKriaTurn(
                plan=_clip_intent_resolution_plan(
                    question=planned.resolution.question,
                    status=(
                        "needs_creator"
                        if planned.resolution.needs_creator
                        else planned.resolution.status
                    ),
                ),
                manifest_hash=manifest.manifest_hash,
                context_hash=manifest.context_hash,
            )
        return PlannedKriaTurn(
            plan=adapt_creator_action(
                output.action,
                server_clip_intents=planned.requested_intents,
                server_resolved_clip_intents=planned.resolution.intents,
            ),
            manifest_hash=manifest.manifest_hash,
            context_hash=manifest.context_hash,
        )
    if (
        isinstance(output.action, ProposeStrategy)
        and any(
            intent.label_source == "transcript"
            for intent in output.action.strategy.clip_intents or []
        )
        and (
            manifest.narration is None
            or output.action.strategy.execution_contract != "guided_voiceover_v1"
        )
    ):
        return PlannedKriaTurn(
            plan=_clip_intent_resolution_plan(
                question="Those labels need a recorded voiceover with guided visuals.",
                status="needs_creator",
            ),
            manifest_hash=manifest.manifest_hash,
            context_hash=manifest.context_hash,
        )
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
