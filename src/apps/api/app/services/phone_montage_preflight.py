"""Run the phone montage plan before the approval card (KRI-775).

Merope café M2 (2026-10-21): the approval card went out for a draft that the phone
montage worker (``generative_build._run_phone_unified_montage_job``) then refused ten
seconds after approval: "This draft is about 4.7s; you asked for 14s." That refusal is
decided by a deterministic plan over the brief, strategy and media snapshot approval pins,
so the runtime can know it first. ``phone_montage_blockers`` runs that same plan (the
worker's own ``_unified_montage_clips`` + ``_plan_phone_unified_montage``) and returns
the receipts the worker would refuse on. The runtime folds them into the draft's receipts,
so the existing hold-back says why and asks "a different approach, or this simpler
version?" before any approval card.

What it judges, and why only that:

- ``order`` and ``timing`` requirements: the deterministic plan alone decides them.
- never ``text``: the worker's two model steps (landmark names for per-clip labels, the
  creation-text lane) only write text and are skipped here, so a text verdict would be a
  guess. The render's own receipts still judge text.

Cheap and safe: no model call, no storage call, no write. It returns ``None`` (nothing to
say) wherever the worker would not run this planner, and fails open: any error is logged
and the draft goes on exactly as before, with the worker's check as the last word.
Kill switch: ``KRIA_PHONE_MONTAGE_PREFLIGHT_ENABLED=false`` + restart (api + worker).
"""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping, Sequence
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

import structlog

from app.config import settings

if TYPE_CHECKING:
    from app.kria.brief import BriefRequirement, CreativeBrief
    from app.kria.contracts import RequirementReceipt

log = structlog.get_logger()

# The requirement kinds the deterministic phone plan settles on its own (see above).
PREFLIGHT_KINDS = frozenset({"order", "timing"})
_GENERATION = "approval-preflight"


def phone_montage_blockers(
    db: Any,
    *,
    creator_id: object,
    item: Any,
    brief: CreativeBrief | None,
    strategy: Mapping[str, Any] | None,
    media_snapshot: Mapping[str, Any] | None,
) -> list[RequirementReceipt]:
    """The receipts the phone montage worker would refuse this draft on ([] = none known).

    ``media_snapshot`` is the one approval will pin (``planned.media_snapshot``, with its
    length plan). ``db`` is only read, for the item's ready Visuals, and only when the
    draft renders through the unified phone montage.
    """
    if not settings.kria_phone_montage_preflight_enabled:
        return []
    from app.pipeline.phone_guided_plan import UnsupportedPhonePlan  # noqa: PLC0415

    started = time.monotonic()
    item_id = str(getattr(item, "id", "") or "")
    try:
        blockers = montage_blockers(
            creator_id=creator_id,
            edit_format=getattr(item, "edit_format", None),
            brief=brief,
            strategy=strategy,
            media_snapshot=media_snapshot,
            visuals=lambda: _ready_visuals(db, item=item, user_id=creator_id, strategy=strategy),
        )
    except UnsupportedPhonePlan as exc:
        # The planner itself declined; the worker would too. Recorded, not (yet) surfaced.
        log.info(
            "kria_phone_montage_preflight_planner_declined", plan_item_id=item_id, reason=str(exc)
        )
        return []
    except Exception:  # noqa: BLE001 - a broken check must never block a draft
        log.exception("kria_phone_montage_preflight_failed", plan_item_id=item_id)
        return []
    if blockers is None:
        return []
    log.info(
        "kria_phone_montage_preflight",
        plan_item_id=item_id,
        blocked_requirement_ids=[receipt.requirement_id for receipt in blockers],
        elapsed_ms=int((time.monotonic() - started) * 1000),
    )
    return blockers


def montage_blockers(
    *,
    creator_id: object,
    edit_format: object,
    brief: CreativeBrief | None,
    strategy: Mapping[str, Any] | None,
    media_snapshot: Mapping[str, Any] | None,
    visuals: Sequence[Any] | Callable[[], Sequence[Any]],
) -> list[RequirementReceipt] | None:
    """``None`` when this draft does not render through the unified phone montage, or
    nothing here can be judged; otherwise the checked ``order`` / ``timing`` receipts the
    worker would refuse on, in brief order, in the current reply language.

    Mirrors the dispatch + worker inputs: the clips dispatch hands the worker (an explicit
    selection, byte-identical uploads collapsed), their phone source receipts, the route the
    job would be stamped with, and the approved length plan.
    """
    from app.kria.brief_checks import (  # noqa: PLC0415
        build_receipts,
        defers_to_unified_montage,
        needs_creator_choice,
        plan_facts_from_unified_montage,
    )
    from app.kria.duplicate_uploads import collapse_strategy, montage_record_fields  # noqa: PLC0415
    from app.pipeline.unified_montage import brief_view  # noqa: PLC0415
    from app.services.creator_render_contract import (  # noqa: PLC0415
        PLAN_AUTHORITY_FIELD,
        build_render_contract,
    )
    from app.services.length_planning import LENGTH_PLAN_VERSION  # noqa: PLC0415
    from app.services.length_policy import creator_duration_s  # noqa: PLC0415
    from app.services.phone_destination import phone_drawable_visual_kinds  # noqa: PLC0415
    from app.services.phone_sources import bind_phone_sources  # noqa: PLC0415
    from app.services.render_route import (  # noqa: PLC0415
        Route,
        resolve_route,
        route_capabilities_from_settings,
        route_inputs_from_job,
    )
    from app.tasks.content_plan_build import (  # noqa: PLC0415
        _collapse_duplicate_uploads,
        _creator_intent_clip_order,
        _creator_selected_clip_paths,
    )
    from app.tasks.generative_build import (  # noqa: PLC0415
        _plan_phone_unified_montage,
        _unified_montage_clips,
    )

    # The worker only refuses a draft whose brief was bound at approval.
    if brief is None or not settings.brief_binding_for(creator_id):
        return None
    judged: dict[str, BriefRequirement] = {
        req.id: req for req in brief.live() if req.kind in PREFLIGHT_KINDS
    }
    if not judged or not isinstance(media_snapshot, Mapping):
        return None
    strategy = dict(strategy or {})
    media = dict(media_snapshot)
    edit_format = strategy.get("edit_format") or edit_format
    clip_paths = [str(path) for path in media.get("clip_paths") or []]
    if not clip_paths or media.get("song_path") or media.get("voiceover_path"):
        # No footage (a Visuals-only plan), a creator song or a recorded voice: another writer.
        return None
    if not defers_to_unified_montage(
        creator_id=creator_id,
        edit_format=edit_format,
        audio_strategy=strategy.get("audio_strategy"),
        clip_paths=clip_paths,
    ):
        return None
    envelope = media.get("length_plan")
    if envelope is not None and (
        not isinstance(envelope, Mapping)
        or envelope.get("version") != LENGTH_PLAN_VERSION
        or envelope.get("conflicts")
    ):
        # An open length choice is the gate's own question (KRI-476), never a second one.
        return None

    # The clips dispatch hands the worker (`content_plan_build.dispatch_item_render_for`).
    rows = [dict(row) for row in media.get("clip_assignments") or [] if isinstance(row, Mapping)]
    owner = SimpleNamespace(id=None, clip_assignments=rows)
    paths = _creator_selected_clip_paths(owner, clip_paths, strategy, without_proposal=True)
    paths, aliases = _collapse_duplicate_uploads(
        owner, paths, strategy, brief_binding={"brief": brief.model_dump(mode="json")}
    )
    planned_strategy = collapse_strategy(strategy, aliases) if aliases else strategy
    paths, _placed = _creator_intent_clip_order(owner, paths, planned_strategy)
    if not paths:
        return None
    try:
        sources = bind_phone_sources(rows, paths)
    except ValueError:
        return None  # dispatch refuses unverified or mixed sources itself

    # The route the job would be stamped with (`render_route.stamp_route`).
    contract = build_render_contract(
        strategy, generation_id=_GENERATION, brief=brief, media_snapshot=media
    )
    if contract is None:
        return None
    candidates: dict[str, Any] = {
        "clip_paths": paths,
        "edit_format": edit_format,
        "creator_strategy": strategy,
        PLAN_AUTHORITY_FIELD: 1,
    }
    inputs = route_inputs_from_job(
        {"creator_brief_binding": {"media_snapshot": media}},
        candidates,
        platform="phone",
        capabilities=route_capabilities_from_settings(),
        contract=contract,
    )
    resolution = resolve_route(inputs) if inputs is not None else None
    if resolution is None or resolution.route is not Route.UNIFIED_MONTAGE:
        return None

    placed_visuals = list(visuals() if callable(visuals) else visuals)
    drawable = phone_drawable_visual_kinds()
    if any(visual.kind not in drawable for visual in placed_visuals):
        return None  # the dispatch gate refuses this shape with its own words
    by_path = {str(row.get("gcs_path")): row for row in rows}
    clips = _unified_montage_clips(
        paths,
        [dict(by_path.get(path) or {}) for path in paths],
        {source.proxy_path: source for source in sources},
        facts_on=settings.clip_facts_for(creator_id),
    )
    plan = _plan_phone_unified_montage(
        clips,
        brief_view(brief),
        strategy=planned_strategy,
        visuals=placed_visuals,
        all_candidates=candidates,
        creator_duration_proven=creator_duration_s(brief=brief) is not None,
        approved_length_plan=envelope,
        contract=contract,
    )
    record = plan.record()
    record.update(montage_record_fields(aliases))
    receipts = build_receipts(
        brief.live(), plan_facts_from_unified_montage(record), include_unchecked=True
    )
    return [
        receipt
        for receipt in receipts
        if receipt.requirement_id in judged and needs_creator_choice(receipt)
    ]


def with_blocked_requirements(
    checked: Sequence[BriefRequirement],
    brief: CreativeBrief | None,
    blockers: Sequence[RequirementReceipt],
) -> list[BriefRequirement]:
    """``checked`` plus the requirements ``blockers`` judged, in brief order."""
    if not blockers or brief is None:
        return list(checked)
    wanted = {receipt.requirement_id for receipt in blockers} | {req.id for req in checked}
    return [req for req in brief.live() if req.id in wanted]


def with_blocker_receipts(
    receipts: Sequence[RequirementReceipt], blockers: Sequence[RequirementReceipt]
) -> list[RequirementReceipt]:
    """The draft's receipts with each blocked requirement judged by the phone plan."""
    if not blockers:
        return list(receipts)
    by_id = {receipt.requirement_id: receipt for receipt in blockers}
    merged = [by_id.pop(receipt.requirement_id, receipt) for receipt in receipts]
    return [*merged, *by_id.values()]


def _ready_visuals(
    db: Any, *, item: Any, user_id: object, strategy: Mapping[str, Any] | None
) -> list:
    """The ready Visuals approval would place: the worker's own rows, read-only."""
    from app.pipeline.unified_montage import selected_visual_ids  # noqa: PLC0415
    from app.tasks.generative_build import ready_visual_rows, unified_visual  # noqa: PLC0415

    rows = ready_visual_rows(
        db,
        plan_item_id=item.id,
        user_id=user_id,
        selected=selected_visual_ids(dict(strategy or {})),
    )
    return [unified_visual(row) for row in rows]
