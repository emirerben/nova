"""One composition of the draft commit's readiness gates, read by the serve and by the tap.

A restating tap (mute, filler) re-drafts the plan and asks for the render approval; the draft
commit (``tasks/kria_runtime._complete_draft_turn``) then refuses, writing nothing, in one of
three ways. The serve must withhold exactly those cards. Before this module each side held its
own copy of when each gate applies and how the three combine, and only
``tests/e2e_suggestions/test_r6_restate_drift.py`` kept the copies together.

Failure modes this exists to close (each is a place two copies had parted or could):

  F1  A gate that applies on one side only: the mark gate needs binding on, a render request, the
      mark route AND a missing mark; the requirements gate reads ``assess_readiness`` on a
      readiness account and the draft check's receipts elsewhere; the choice gate needs the choice
      switch or binding on, and a strategy. A copy that drops one condition serves a dead card or
      withholds a live one.
  F2  The same gate on different facts: the creator's marks, the choice-gate events and the mark
      route's inputs were each read by a query written twice.
  F3  Verdict order: the tap asks the mark first, then the requirements, then the choice. A reason
      is what the creator and the logs see, so the order is held here, once.
  F4  A fact that is not known yet read as "fine": a gate whose fact is ``None`` was not asked and
      does not refuse. The caller that has not loaded a fact says so; a gate never reads absence
      as a pass it did not check.

``commit_readiness`` is pure: facts in, verdict out. The loaders read the facts. What really
differs between the callers stays an explicit input: the event cutoff (``through_sequence``: the
tap's source event, the whole thread for the GET), the mark route's app build, and whether a
render approval is requested.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from app.config import settings

MARK_NEEDED = "mark_needed"
REQUIREMENTS_NOT_READY = "requirements_not_ready"
CHOICE_NEEDED = "choice_needed"


@dataclass(frozen=True)
class CommitReadinessInputs:
    """The facts the commit's gates read. ``None`` = not asked: that gate does not refuse.

    ``readiness_ready`` / ``readiness_unmet`` are ``assess_readiness``' ``ready`` and its
    requirements outside ``APPROVABLE``; ``receipts_unmet`` the draft check's requirements that
    need a creator choice. Only the pair the account's mode reads (``readiness``) is looked at.
    """

    render_requested: bool = True
    marks_route: bool = False
    missing_marks: bool | None = None
    readiness_ready: bool | None = None
    readiness_unmet: Sequence[str] | None = None
    receipts_unmet: Sequence[str] | None = None
    has_strategy: bool = False
    choice_blocked: bool | None = None


def mark_gate_open(*, binding_on: bool, render_requested: bool, marks_route: bool) -> bool:
    """Whether the commit can ask "Mark the plate" at all (KRI-901)."""
    return bool(binding_on and render_requested and marks_route)


def choice_gate_open(*, binding_on: bool, choice_questions_on: bool, has_strategy: bool) -> bool:
    """Whether the commit's choice gate (``_unresolved_choice_plan``) runs at all."""
    return bool((choice_questions_on or binding_on) and has_strategy)


def commit_readiness(
    inputs: CommitReadinessInputs,
    *,
    readiness: bool,
    binding_on: bool,
    choice_questions_on: bool,
) -> str | None:
    """Why the commit would refuse a draft with these facts, or None. Pure.

    ``readiness`` = the account is on approval readiness (``settings.approval_readiness_for``).
    """
    if inputs.missing_marks and mark_gate_open(
        binding_on=binding_on,
        render_requested=inputs.render_requested,
        marks_route=inputs.marks_route,
    ):
        return MARK_NEEDED
    if binding_on and _requirements_unmet(inputs, readiness=readiness):
        return REQUIREMENTS_NOT_READY
    if inputs.choice_blocked and choice_gate_open(
        binding_on=binding_on,
        choice_questions_on=choice_questions_on,
        has_strategy=inputs.has_strategy,
    ):
        return CHOICE_NEEDED
    return None


def _requirements_unmet(inputs: CommitReadinessInputs, *, readiness: bool) -> bool:
    if readiness:
        if inputs.readiness_ready is None:
            return False
        return not inputs.readiness_ready or bool(inputs.readiness_unmet)
    return bool(inputs.receipts_unmet)


def gate_settings(creator_id: Any) -> tuple[bool, bool, bool]:
    """``(readiness, binding_on, choice_questions_on)`` for the account: the three levers
    ``commit_readiness`` takes, read the way the commit reads them."""
    return (
        settings.approval_readiness_for(creator_id),
        settings.brief_binding_for(creator_id),
        bool(settings.kria_choice_questions_enabled),
    )


def client_marks(client_protocol: int | None) -> bool:
    """Whether the app build shows the mark card (``protected_region_mark_min_client_protocol``)."""
    return (
        client_protocol is not None
        and client_protocol >= settings.protected_region_mark_min_client_protocol
    )


def on_marks_route(
    *,
    client_marks: bool,
    creator_id: Any,
    strategy: Mapping[str, Any] | None,
    item: Any,
    fallback_format: str | None = None,
) -> bool:
    """``protected_targets.marks_route`` over the item's own format and clips. The format is the
    item's, else ``fallback_format`` (the tap's draft document), else the strategy's own."""
    from app.kria.protected_targets import marks_route  # noqa: PLC0415

    return marks_route(
        client_marks=client_marks,
        creator_id=creator_id,
        strategy=strategy,
        edit_format=getattr(item, "edit_format", None)
        or fallback_format
        or (strategy or {}).get("edit_format"),
        clip_paths=getattr(item, "clip_gcs_paths", None) or (),
        voiceover_path=getattr(item, "voiceover_gcs_path", None),
    )


def load_protected_regions_sync(
    session: Any, *, thread_id: Any, through_sequence: int | None = None
) -> list[dict]:
    """The creator's marks of what to keep text off (KRI-827), from their own turns only."""
    from sqlalchemy import select  # noqa: PLC0415

    from app.kria.protected_targets import fold_protected_regions  # noqa: PLC0415
    from app.models import CreationThreadEvent  # noqa: PLC0415

    query = select(CreationThreadEvent.role, CreationThreadEvent.payload).where(
        CreationThreadEvent.thread_id == thread_id, CreationThreadEvent.role == "user"
    )
    if through_sequence is not None:
        query = query.where(CreationThreadEvent.sequence <= through_sequence)
    return fold_protected_regions(
        session.execute(query.order_by(CreationThreadEvent.sequence)).all()
    )


def load_missing_marks_sync(
    session: Any,
    *,
    thread_id: Any,
    item: Any,
    brief: Any,
    through_sequence: int | None = None,
) -> tuple[Any, ...]:
    """Each object the live brief asks to keep text off that still needs a mark, in brief order
    (``protected_targets.missing_object_marks``), over the item's clips and the speaker clip a
    render of it already spoke from."""
    from app.kria.protected_targets import (  # noqa: PLC0415
        clip_generations,
        missing_object_marks,
        rendered_speaker_media_id,
        speaker_candidates,
    )
    from app.models import Job  # noqa: PLC0415

    assignments = getattr(item, "clip_assignments", None) or []
    job_id = getattr(item, "current_job_id", None)
    job = session.get(Job, job_id) if job_id else None
    variants = ((getattr(job, "assembly_plan", None) or {}).get("variants")) or []
    return missing_object_marks(
        brief,
        load_protected_regions_sync(
            session, thread_id=thread_id, through_sequence=through_sequence
        ),
        clip_generations(assignments),
        render_clips=speaker_candidates(
            assignments, rendered_speaker=rendered_speaker_media_id(variants)
        ),
    )


def plan_blockers_sync(
    session: Any,
    *,
    creator_id: Any,
    item: Any,
    brief: Any,
    strategy: Mapping[str, Any] | None,
    media: Mapping[str, Any],
    client_protocol: int | None,
) -> list[Any]:
    """A speed ask the phone montage plan can't place, said before the card."""
    from app.services.phone_montage_preflight import readiness_plan_blockers  # noqa: PLC0415

    return readiness_plan_blockers(
        session,
        creator_id=creator_id,
        item=item,
        brief=brief,
        strategy=strategy,
        media_snapshot=media,
        client_protocol=client_protocol,
    )


def assess_readiness_sync(
    session: Any,
    *,
    thread_id: Any,
    item: Any,
    brief: Any,
    strategy: Mapping[str, Any] | None,
    media: Mapping[str, Any],
    manifest: Any,
    creator_id: Any,
    client_protocol: int | None,
    marks_route: bool,
    protected_regions: Sequence[Mapping[str, Any]] | None = None,
    plan_blockers: Sequence[Any] | None = None,
    editor_payload: Any = None,
    editor_text_diff: Any = None,
    creator_request: str | None = None,
    prepared_collection_story: Any = None,
    prepared_research: Any = None,
) -> dict[str, Any]:
    """``assess_readiness`` over the facts both callers read the same way: the creator's marks,
    the compiled order preflight, the order sources and the phone plan's blockers.

    What the callers really differ in stays an argument: the tap hands the draft's editor
    payload, the request it pins, the collection story and the prepared research; the serve's
    strategy draft has none of them. ``protected_regions`` / ``plan_blockers`` left ``None`` are
    loaded here (the whole thread; the plan's own blockers); a caller that has read them at its
    own event cutoff passes them."""
    from app.kria.approval_readiness import assess_readiness  # noqa: PLC0415
    from app.kria.order_replacement_receipts import compiled_order_preflight_sync  # noqa: PLC0415
    from app.kria.order_replacement_sources import (  # noqa: PLC0415
        load_brief_order_source_context_sync,
    )

    if protected_regions is None:
        protected_regions = load_protected_regions_sync(session, thread_id=thread_id)
    if plan_blockers is None:
        plan_blockers = plan_blockers_sync(
            session,
            creator_id=creator_id,
            item=item,
            brief=brief,
            strategy=strategy,
            media=media,
            client_protocol=client_protocol,
        )
    strategy = strategy or {}
    return assess_readiness(
        brief,
        strategy,
        media=dict(media),
        manifest=manifest,
        editor_payload=editor_payload,
        editor_text_diff=editor_text_diff,
        creator_request=creator_request,
        protected_regions=list(protected_regions),
        protected_marks=marks_route,
        prepared_collection_story=prepared_collection_story,
        creator_id=creator_id,
        prepared_research=prepared_research,
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
        plan_blockers=list(plan_blockers),
    )


def unmet_requirement_ids(report: Mapping[str, Any]) -> tuple[str, ...]:
    """The report's requirements outside ``APPROVABLE``: what ``commit_readiness`` reads."""
    from app.kria.approval_readiness import APPROVABLE  # noqa: PLC0415

    return tuple(
        str(row["requirement_id"])
        for row in report["requirements"]
        if row["disposition"] not in APPROVABLE
    )


def load_choice_events_sync(session: Any, *, thread_id: Any) -> list[Any]:
    """The thread's creator and assistant events, tagged the way the choice gate counts asks."""
    from sqlalchemy import select  # noqa: PLC0415

    from app.models import CreationThreadEvent  # noqa: PLC0415
    from app.services.choice_questions import tag_event  # noqa: PLC0415

    return [
        tag_event(role, payload, event_type, content)
        for role, payload, event_type, content in session.execute(
            select(
                CreationThreadEvent.role,
                CreationThreadEvent.payload,
                CreationThreadEvent.event_type,
                CreationThreadEvent.content,
            )
            .where(
                CreationThreadEvent.thread_id == thread_id,
                CreationThreadEvent.role.in_({"user", "assistant"}),
            )
            .order_by(CreationThreadEvent.sequence)
        ).all()
    ]


def choice_blocked_sync(
    session: Any,
    *,
    thread_id: Any,
    strategy: Mapping[str, Any] | None,
    brief: Any,
    media: Mapping[str, Any] | None,
    has_draft: bool,
    creator_id: Any,
    client_protocol: int | None,
) -> tuple[Any, str] | None:
    """The commit's choice gate (``_unresolved_choice_plan``) over the strategy, brief, media
    and thread events: ``(plan, reason)`` when an open question refuses the draft, else None.
    ``brief`` is the one the caller judges (the latest, with the choices answered)."""
    from app.tasks.kria_runtime import _unresolved_choice_plan  # noqa: PLC0415

    _readiness, binding_on, choice_questions_on = gate_settings(creator_id)
    if not choice_gate_open(
        binding_on=binding_on, choice_questions_on=choice_questions_on, has_strategy=bool(strategy)
    ):
        return None
    contract_brief = brief if binding_on else None
    return _unresolved_choice_plan(
        strategy,
        contract_brief,
        media,
        contract_brief=contract_brief,
        events=load_choice_events_sync(session, thread_id=thread_id),
        has_draft=has_draft,
        creator_id=creator_id if binding_on else None,
        client_protocol=client_protocol,
        allow_choices=choice_questions_on,
    )
