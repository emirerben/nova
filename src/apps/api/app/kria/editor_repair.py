"""Select a timing-only candidate outside locks; fence it again before draft mutation."""

from __future__ import annotations

import copy
import hashlib
import json
import time
import uuid
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any

from sqlalchemy import select

from app.config import settings
from app.database import sync_session
from app.kria.brief import apply_updates, load_latest_brief_sync
from app.kria.contracts import KriaTurnPlan
from app.kria.reply_language import say
from app.models import (
    ContentPlan,
    CreationThread,
    CreationThreadEvent,
    CreatorAgentSession,
    CreatorAgentTurn,
    CreatorEditDraft,
    Job,
    PlanItem,
)
from app.services.kria_editor_ops import (
    KriaEditorOpError,
    build_editor_snapshot,
    compile_editor_ops,
    parse_editor_state,
    resolve_editor_base,
)
from app.services.kria_editor_ops_text import validate_timing_only_text

if TYPE_CHECKING:
    from app.kria.planner import PlannedKriaTurn

COMPLETION_RESERVE_S = 5.0

_TIMING = frozenset({"retime_text_sequence", "set_total_duration"})
_DISCARDABLE = frozenset({"rewrite_text", "remove_texts", "add_text"})


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def _digest(value: Any) -> str:
    return hashlib.sha256(_json(value).encode()).hexdigest()


def candidate_operations(planned: PlannedKriaTurn) -> list[dict] | None:
    intents = planned.plan.intents
    if (
        planned.plan.mode != "act"
        or not intents
        or intents[0].tool_name != "draft.apply_editor_ops"
    ):
        return None
    if not planned.brief_updates or any(
        u.kind != "timing"
        or u.operation == "remove"
        or u.literal is not None
        or u.facts.get("editor_timing_only") is not True
        or u.facts.get("preserve_editor_text") is True
        for u in planned.brief_updates
    ):
        return None
    ops = intents[0].arguments.get("operations", [])
    if any(
        not isinstance(op, dict)
        or op.get("preserve_text") is True
        or op.get("op") not in _TIMING | _DISCARDABLE
        for op in ops
    ):
        return None
    kept = [copy.deepcopy(op) for op in ops if op.get("op") in _TIMING]
    if sum(op.get("op") == "retime_text_sequence" for op in kept) != 1 or len(kept) == len(ops):
        return None
    return kept


@dataclass(frozen=True)
class PreparedEditorRepair:
    basis_digest: str
    candidate_id: str
    operations_json: str
    authority_digest: str
    provenance_json: str | None = None
    evidence_ids: tuple[str, ...] = ()
    compiled_payload_digest: str = ""


def _authority(planned: PlannedKriaTurn) -> str:
    return _digest([u.model_dump(mode="json") for u in planned.brief_updates])


def basis_digest(
    *, job: Any, variant: dict, head: Any, client_state: Any, projected: dict, brief: Any
) -> str:
    return _digest(
        {
            "job": str(job.id),
            "variant": variant,
            "projected": projected,
            "head": None
            if head is None
            else [str(head.id), head.draft_revision, head.snapshot_json, head.base_generation_id],
            "client": client_state.model_dump(mode="json")
            if hasattr(client_state, "model_dump")
            else client_state,
            "brief": brief.model_dump(mode="json") if brief else None,
        }
    )


def verify_editor_repair(
    planned: PlannedKriaTurn,
    *,
    job: Any,
    variant: dict,
    head: Any,
    client_state: Any,
    projected: dict,
    brief: Any,
) -> None:
    repair = planned.editor_repair
    if repair is None:
        return
    if (
        repair.basis_digest
        != basis_digest(
            job=job,
            variant=variant,
            head=head,
            client_state=client_state,
            projected=projected,
            brief=brief,
        )
        or repair.authority_digest != _authority(planned)
        or repair.operations_json != _json(planned.plan.intents[0].arguments["operations"])
    ):
        raise KriaEditorOpError(
            "The draft changed during the timing review. Your current preview is unchanged; "
            "review the current draft first."
        )


def with_editor_review(
    planned: PlannedKriaTurn, *, turn_id: uuid.UUID, deadline: float
) -> PlannedKriaTurn:
    candidate = candidate_operations(planned)
    ctx = planned.editor_repair_run_context
    if (
        not settings.creator_repair_reviewer_enabled
        or ctx is None
        or candidate is None
        or time.monotonic() >= deadline - COMPLETION_RESERVE_S
    ):
        return planned
    from app.services.editor_repair_reviewer import (
        EditorRepairEvidence,
        EditorRepairReviewContext,
        review_editor_repair,
    )

    try:
        with sync_session() as db:
            turn = db.get(CreatorAgentTurn, turn_id)
            thread = db.get(CreationThread, turn.thread_id) if turn else None
            session = db.get(CreatorAgentSession, turn.session_id) if turn else None
            item = db.get(PlanItem, thread.active_plan_item_id) if thread else None
            job = db.get(Job, item.current_job_id) if item else None
            plan_row = db.get(ContentPlan, item.content_plan_id) if item else None
            if (
                not plan_row
                or not job
                or not session
                or session.target_job_id != job.id
                or session.creator_id != thread.creator_id
                or thread.active_creator_agent_session_id != session.id
                or session.plan_item_id != item.id
                or job.content_plan_item_id != item.id
                or plan_row.user_id != thread.creator_id
                or job.user_id != thread.creator_id
                or int(plan_row.ownership_epoch or 0) != int(session.ownership_epoch)
                or int(job.content_plan_ownership_epoch or 0) != int(session.ownership_epoch)
            ):
                return planned
            variant = next(
                (
                    v
                    for v in (job.assembly_plan or {}).get("variants", [])
                    if v.get("variant_id") == session.target_variant_id
                ),
                None,
            )
            if variant is None:
                return planned
            head = db.scalar(
                select(CreatorEditDraft).where(
                    CreatorEditDraft.item_id == item.id,
                    CreatorEditDraft.variant_key == str(session.target_variant_id or "initial"),
                    CreatorEditDraft.is_head.is_(True),
                )
            )
            if head is None:
                return planned
            client_state = parse_editor_state(turn.editor_state)
            base = resolve_editor_base(job, variant, head, client_state)
            brief = load_latest_brief_sync(db, thread.id)
            effective_brief = apply_updates(
                brief, planned.brief_updates, source_turn_id=str(turn_id)
            )
            if any(
                r.facts.get("preserve_editor_text") is True
                or (
                    isinstance(r.facts.get("sequence_timing"), dict)
                    and r.facts["sequence_timing"].get("mode") == "absolute"
                )
                for r in effective_brief.live()
            ):
                return planned
            from app.kria.editor_sequence_timing import sequence_timing_plan_from_operations

            if (
                sequence_timing_plan_from_operations(
                    candidate, build_editor_snapshot(job, base.projected)
                )
                is None
            ):
                return planned
            compiled = compile_editor_ops(job, base.projected, candidate)
            compiled_digest = _digest(compiled.payload.model_dump(mode="json"))
            validate_timing_only_text(
                base.projected.get("text_elements") or [], compiled.payload.text_elements or []
            )
            basis = basis_digest(
                job=job,
                variant=variant,
                head=head,
                client_state=client_state,
                projected=base.projected,
                brief=brief,
            )
            candidate_id = _digest([basis, candidate, _authority(planned)])
            source = db.get(CreationThreadEvent, turn.source_event_id)
            context = EditorRepairReviewContext(
                original_request=source.content or "",
                turn_id=str(turn_id),
                job_id=str(job.id),
                session_id=str(session.id),
                editor_state_digest=basis,
                editor_timing_only=True,
                planned_operations_json=_json(planned.plan.intents[0].arguments["operations"]),
                candidate_id=candidate_id,
                candidate_operations_json=_json(candidate),
                evidence=(
                    EditorRepairEvidence(
                        evidence_id="timing-only-compiler",
                        source="compiler",
                        observation=(
                            "The existing retime operation compiles while preserving every "
                            "original text, style, and source; the other text operations "
                            "violate the current timing-only request."
                        ),
                    ),
                ),
            )
        review_deadline = deadline - COMPLETION_RESERVE_S
        if time.monotonic() >= review_deadline:
            return planned
        ctx = replace(
            ctx,
            deadline_monotonic=min(review_deadline, ctx.deadline_monotonic)
            if ctx.deadline_monotonic is not None
            else review_deadline,
        )
        result = review_editor_repair(context, run_context=ctx)
        if time.monotonic() >= deadline:
            return planned
    except (ValueError, TypeError, KeyError, TimeoutError, KriaEditorOpError):
        return planned
    if result.status == "requires_confirmation":
        return replace(
            planned,
            plan=KriaTurnPlan(
                mode="respond",
                turn_value="question",
                response=say(
                    en="Changing the wording needs your confirmation. Should I keep "
                    "the exact original words and adjust only their timing?",
                    tr="Metni değiştirmek için onayın gerekiyor. Aynı kelimeleri koruyup "
                    "yalnızca zamanlamalarını ayarlayayım mı?",
                ),
            ),
        )
    if (
        result.status != "accept_candidate"
        or result.candidate_id != candidate_id
        or not result.evidence_ids
        or any(e != "timing-only-compiler" for e in result.evidence_ids)
    ):
        return planned
    intent = planned.plan.intents[0]
    updated = intent.model_copy(
        update={
            "arguments": {
                **intent.arguments,
                "operations": candidate,
                "summary": say(
                    en="Prepared adjusted text timing while preserving the original "
                    "wording and appearance.",
                    tr="Özgün kelimeleri ve görünümü koruyarak "
                    "yazı zamanlaması taslağını hazırladım.",
                ),
                "notes": None,
            }
        }
    )
    plan = planned.plan.model_copy(update={"intents": [updated, *planned.plan.intents[1:]]})
    return replace(
        planned,
        plan=plan,
        editor_repair=PreparedEditorRepair(
            basis,
            candidate_id,
            _json(candidate),
            _authority(planned),
            result.provenance.model_dump_json() if getattr(result, "provenance", None) else None,
            tuple(result.evidence_ids),
            compiled_digest,
        ),
    )


def verify_compiled_repair(planned: PlannedKriaTurn, payload: Any) -> None:
    repair = planned.editor_repair
    if repair is not None and (
        not repair.compiled_payload_digest
        or repair.compiled_payload_digest != _digest(payload.model_dump(mode="json"))
    ):
        raise KriaEditorOpError(
            say(
                en="The allowed sections changed the reviewed timing candidate. "
                "Your current draft is unchanged; review the selected sections first.",
                tr="İzin verilen bölümler incelenen zamanlama taslağını değiştirdi. "
                "Mevcut taslağın değişmedi; önce seçili bölümleri gözden geçir.",
            )
        )


def applied_repair_receipt(planned: PlannedKriaTurn) -> dict:
    repair = planned.editor_repair
    if repair is None:
        return {}
    return {
        "editor_repair": {
            "status": "draft_applied",
            "candidate_id": repair.candidate_id,
            "basis_digest": repair.basis_digest,
            "compiled_payload_digest": repair.compiled_payload_digest,
            "authority_digest": repair.authority_digest,
            "evidence_ids": list(repair.evidence_ids),
            "provenance": json.loads(repair.provenance_json) if repair.provenance_json else None,
            "reason": "Removed unrequested copy changes from the current timing-only plan.",
        }
    }
