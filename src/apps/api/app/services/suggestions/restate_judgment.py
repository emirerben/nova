"""One question for a restating card, asked by the serve and by the tap: would it be approved?

A mute or filler tap re-drafts the thread's current plan with one more requirement and asks for the
render approval. The draft commit (``tasks/kria_runtime._complete_draft_turn``) then judges that
draft and refuses, writing nothing, when it is not ready: the requirement receipts (or, on an
approval-readiness account, ``assess_readiness``), the choice gate. A card served over a refusal is
a dead tap, so the GET asks the same questions before it serves the card.

``restate_problem`` is that question. It reads the commit's own facts on the commit's own inputs and
combines them with ``commit_readiness`` (``services/suggestions/commit_readiness.py``), the one
composition the tap calls too; ``plan_media`` is the one piece the
planner (the tap) and the serve both call, so the plan is judged on the same media in both. Where
a fact cannot be rebuilt exactly the answer is a reason, never "fine": a card the server cannot
prove is not offered. ``tests/e2e_suggestions/test_r6_restate_drift.py`` runs both on the same
threads and fails when they part.

The media (readiness accounts only). The live planner judges a plan on its media plus the length
plan it computed (``planner._enrich_best_parts_snapshot``), and the draft it saves pins that
snapshot in its brief binding. A restate does not plan: it judges the plan the head draft pinned,
on that draft's pinned length plan. The bare snapshot of the item has no length plan, so a stated
length is "evidence missing" and every tap would be refused. A head that pinned no plan, or one
pinned on other footage, leaves the length plan out, and a requirement that needs it is not ready.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from typing import Any

from pydantic import ValidationError

from app.config import settings
from app.services.suggestions.commit_readiness import (
    CHOICE_NEEDED,
    REQUIREMENTS_NOT_READY,
    CommitReadinessInputs,
    choice_blocked_sync,
    client_marks,
    commit_readiness,
    gate_settings,
    load_protected_regions_sync,
    on_marks_route,
)

__all__ = ["CHOICE_NEEDED", "REQUIREMENTS_NOT_READY"]

# What the live planner adds to the media (``planner._enrich_best_parts_snapshot``); a test pins
# that it adds these keys and no others.
PLAN_MEDIA_KEYS = (
    "length_plan",
    "best_parts",
    "best_parts_capacity_s",
    "best_parts_conflicts",
    "still_hold_conflict",
)

BRIEF_UNAVAILABLE = "brief_unavailable"
NO_CARD = "no_card"
NO_PERSONA = "no_persona"


def plan_media(current: Mapping[str, Any], pinned: Mapping[str, Any] | None) -> dict[str, Any]:
    """``current`` (the item's media now) plus the length plan a head draft pinned, when the
    pinned plan is for these very sources. Pure.

    The identity check is the commit's own (``brief_binding.media_identity``): a pin made on other
    clips, other generations or another song is not this footage's plan, and is not borrowed.
    """
    from app.kria.brief_binding import media_identity  # noqa: PLC0415

    out = dict(current)
    if (
        isinstance(pinned, Mapping)
        and isinstance(pinned.get("length_plan"), Mapping)
        and media_identity(dict(pinned)) == media_identity(out)
    ):
        out.update({key: pinned[key] for key in PLAN_MEDIA_KEYS if key in pinned})
    return out


async def restate_problem(
    db: Any, thread: Any, ctx: Any, item: Any, *, card: Any | None
) -> str | None:
    """Why the tap of ``card`` (a restating card) would be refused at the commit, or None.

    ``card`` is the server-side ``Suggestion`` (its action is what the tap writes). Anything this
    cannot establish is a reason: the caller leaves the card out.
    """
    from app.kria.brief import load_latest_brief  # noqa: PLC0415
    from app.services.suggestions import pinned  # noqa: PLC0415

    if card is None:
        return NO_CARD
    creator_id = getattr(thread, "creator_id", None)
    base = await pinned.restate_base(db, thread_id=thread.id, ctx=ctx)
    if base.strategy is None:
        return base.reason or "no_plan"
    prior = await load_latest_brief(db, thread.id)
    readiness = settings.approval_readiness_for(creator_id)
    if readiness:
        strategy, manifest = await _readiness_stand(db, item, base.strategy, card, creator_id)
    else:
        strategy, manifest = base.strategy, None
    media = _stand_media(item, base, readiness=readiness)
    brief, problem = _brief_after_tap(prior, card, strategy, creator_id)
    if problem is not None:
        return problem
    if readiness:
        ready, unmet = await db.run_sync(
            lambda session: _readiness_facts(
                session,
                thread=thread,
                item=item,
                brief=brief,
                strategy=strategy,
                media=media,
                manifest=manifest,
                client_protocol=ctx.client_protocol,
            )
        )
        inputs = CommitReadinessInputs(readiness_ready=ready, readiness_unmet=unmet)
    else:
        problem, unmet = await _receipts_facts(db, thread, ctx, item, base, prior, card)
        if problem is not None:
            return problem
        inputs = CommitReadinessInputs(receipts_unmet=unmet)
    _, binding_on, choice_questions_on = gate_settings(creator_id)
    levers = {
        "readiness": readiness,
        "binding_on": binding_on,
        "choice_questions_on": choice_questions_on,
    }
    reason = commit_readiness(inputs, **levers)
    if reason is not None:
        return reason
    blocked = await db.run_sync(
        lambda session: choice_blocked_sync(
            session,
            thread_id=thread.id,
            strategy=strategy,
            brief=brief,
            media=media,
            has_draft=base.head_hash is not None,
            creator_id=creator_id,
            client_protocol=ctx.client_protocol,
        )
    )
    return commit_readiness(
        replace(inputs, has_strategy=bool(strategy), choice_blocked=blocked is not None), **levers
    )


def _stand_media(item: Any, base: Any, *, readiness: bool) -> dict[str, Any]:
    """The media the tap's draft is judged on: what ``plan_pinned_suggestion_turn`` hands it."""
    from app.kria.brief_binding import snapshot_media  # noqa: PLC0415

    current = snapshot_media(item)
    return plan_media(current, base.media) if readiness else current


def _brief_after_tap(
    prior: Any, card: Any, strategy: Mapping[str, Any], creator_id: Any
) -> tuple[Any, str | None]:
    """The brief the commit judges: the latest one plus the card's own typed requirement."""
    from app.kria.brief import (  # noqa: PLC0415
        BriefCoverageError,
        BriefUpdateBatchError,
        apply_updates,
    )
    from app.services.choice_questions import answered_brief  # noqa: PLC0415
    from app.services.suggestions import pinned  # noqa: PLC0415
    from app.services.suggestions.models import BriefUpdateAction  # noqa: PLC0415

    if not settings.creative_brief_for(creator_id):
        # No brief is written: a typed brief update has nowhere to go (the tap refuses it).
        return prior, BRIEF_UNAVAILABLE if isinstance(card.action, BriefUpdateAction) else None
    try:
        brief = apply_updates(prior, pinned.restate_updates(card, card.action), source_turn_id=None)
    except (BriefCoverageError, BriefUpdateBatchError, ValidationError, ValueError):
        return prior, BRIEF_UNAVAILABLE
    if settings.kria_choice_questions_enabled:
        brief = answered_brief(brief, strategy)
    return brief, None


async def _readiness_stand(
    db: Any, item: Any, base_strategy: Mapping[str, Any], card: Any, creator_id: Any
) -> tuple[dict[str, Any], Any]:
    """The strategy and the renderer the tap judges: ``_plan_pinned_turn`` runs the very same
    ``_with_approval_readiness`` over the restated plan, so this is its answer, not a copy."""
    from app.kria.planner import PlannedKriaTurn, _restated_strategy_plan  # noqa: PLC0415
    from app.tasks.kria_runtime import _with_approval_readiness  # noqa: PLC0415

    planned = await _with_approval_readiness(
        db,
        PlannedKriaTurn(
            plan=_restated_strategy_plan(dict(base_strategy), card.prompt),
            manifest_hash=None,
            context_hash=None,
        ),
        creator_id=creator_id,
        item_id=item.id,
    )
    return dict(planned.plan.intents[0].arguments["strategy"]), planned.readiness_manifest


def _readiness_facts(
    session: Any,
    *,
    thread: Any,
    item: Any,
    brief: Any,
    strategy: Mapping[str, Any],
    media: Mapping[str, Any],
    manifest: Any,
    client_protocol: int | None,
) -> tuple[bool, tuple[str, ...]]:
    """``assess_readiness`` on the inputs the commit hands it for a restate (strategy draft:
    no editor payload, no collection story, no prepared research): ``(ready, unmet ids)``."""
    from app.kria.approval_readiness import APPROVABLE, assess_readiness  # noqa: PLC0415
    from app.kria.order_replacement_receipts import compiled_order_preflight_sync  # noqa: PLC0415
    from app.kria.order_replacement_sources import (  # noqa: PLC0415
        load_brief_order_source_context_sync,
    )
    from app.services.phone_montage_preflight import readiness_plan_blockers  # noqa: PLC0415

    creator_id = getattr(thread, "creator_id", None)
    on_route = on_marks_route(
        client_marks=client_marks(client_protocol),
        creator_id=creator_id,
        strategy=strategy,
        item=item,
    )
    regions = load_protected_regions_sync(session, thread_id=thread.id)
    report = assess_readiness(
        brief,
        strategy,
        media=dict(media),
        manifest=manifest,
        protected_regions=regions,
        protected_marks=on_route,
        creator_id=creator_id,
        order_execution=compiled_order_preflight_sync(
            session,
            item=item,
            creator_id=creator_id,
            brief=brief,
            strategy=strategy,
            media_snapshot=media,
            client_protocol=client_protocol,
        ),
        order_source_context=load_brief_order_source_context_sync(
            session, brief=brief, item_id=item.id, creator_id=creator_id
        ),
        plan_blockers=readiness_plan_blockers(
            session,
            creator_id=creator_id,
            item=item,
            brief=brief,
            strategy=strategy,
            media_snapshot=media,
            client_protocol=client_protocol,
        ),
    )
    unmet = tuple(
        str(row["requirement_id"])
        for row in report["requirements"]
        if row["disposition"] not in APPROVABLE
    )
    return bool(report["ready"]), unmet


async def _receipts_facts(
    db: Any, thread: Any, ctx: Any, item: Any, base: Any, brief: Any, card: Any
) -> tuple[str | None, tuple[str, ...]]:
    """An account off approval readiness: the draft check's receipts for the requirements the
    card did not write (``plan_facts_from_strategy`` -> ``build_receipts`` ->
    ``needs_creator_choice``). It reads only the plan and the brief; facts that need the phone's
    montage preflight or the research step stay unknown, and an unknown fact is an unchecked
    receipt, which never holds a draft. ``(problem, unmet ids)``."""
    from app.kria.brief_checks import (  # noqa: PLC0415
        build_receipts,
        needs_creator_choice,
        plan_facts_from_strategy,
        requirements_to_check_at_draft,
    )
    from app.models import ContentPlan, Persona  # noqa: PLC0415
    from app.services.choice_questions import answered_brief  # noqa: PLC0415
    from app.services.creator_sessions import resolve_item_creator_context  # noqa: PLC0415
    from app.services.suggestions.models import ItemSettingAction  # noqa: PLC0415

    creator_id = getattr(thread, "creator_id", None)
    if brief is None or not brief.live():
        return None, ()
    if settings.kria_choice_questions_enabled:
        brief = answered_brief(brief, base.strategy)
    plan = await db.get(ContentPlan, item.content_plan_id)
    persona = await db.get(Persona, plan.persona_id) if plan is not None else None
    if persona is None:
        return NO_PERSONA, ()
    manifest, _media = await resolve_item_creator_context(
        db, item, persona=persona, runs_guided_voiceover=False
    )
    cleanup = (
        card.action.value
        if isinstance(card.action, ItemSettingAction)
        and card.action.field == "speech_cleanup_enabled"
        else bool(getattr(item, "speech_cleanup_enabled", False))
    )
    facts = plan_facts_from_strategy(
        base.strategy,
        clip_ids=[str(media.media_id) for media in manifest.media],
        manifest=manifest,
        # The tap flips the setting before it judges the draft; mute leaves it alone.
        speech_cleanup_enabled=cleanup,
    )
    checked = requirements_to_check_at_draft(
        brief.live(),
        creator_id=creator_id,
        strategy=base.strategy,
        item_edit_format=item.edit_format,
        clip_paths=item.clip_gcs_paths or (),
        client_protocol=ctx.client_protocol,
    )
    bound = settings.brief_binding_for(creator_id)
    receipts = build_receipts(checked, facts, include_unchecked=bound, strict_order=bound)
    return None, tuple(r.requirement_id for r in receipts if needs_creator_choice(r))
