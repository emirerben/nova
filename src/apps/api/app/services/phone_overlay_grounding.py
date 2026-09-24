"""phone_overlay_grounding -- transcript + Visuals pool -> phone PiP overlay
cards (KRI-176).

On a phone-rendered Talking (subtitled) edit, `_run_phone_subtitled_job`
(`app.tasks.generative_build`) already knows how to bind a hand-authored
`_phone_subtitled_lanes_v1` overlay request (KRI-174 Phase 1) into
`SubtitledOverlayCard` lanes. This module supplies the missing "grounding"
step for the common case where nobody authored one: it matches the speaker's
own transcript against the creator's Visuals pool -- reusing exactly the same
matching primitives the cloud `match_overlay_suggestions` task
(`app.tasks.autoplace`) already uses for talking-head overlays -- and turns
the result into face-aware, caption-safe picture-in-picture cards plus a
creator-readable receipt.

Every candidate image Visual ends up in exactly one of the receipt's
``placed``/``unplaced`` lists -- a Visual is never silently dropped. The
receipt is creator-safe: it never carries a ``gcs_path``, a generation, or
any agent/prompt text, only labels, timings, and short plain-text reasons.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Iterable
from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from typing import Any

import structlog
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.models import Job, PlanItemAsset
from app.pipeline.phone_subtitled_lanes import CAPTION_BAND_TOP_FRAC, SubtitledOverlayCard
from app.pipeline.render_geometry import (
    MediaFootprint,
    NormalizedBox,
    ProtectedRegion,
    arbitrate_media_overlays,
    sample_face_regions,
)
from app.services.overlay_autoplace import build_suggestions, heuristic_match

log = structlog.get_logger()

# Phone captions sit at y~=0.8; `CAPTION_BAND_TOP_FRAC` (0.62) is the
# compiler's own clamp on a card's y_frac. Protecting from 0.60 down gives a
# card's rendered bbox a little extra margin before that clamp ever has to
# bite defensively.
_CAPTION_PROTECTED_TOP_FRAC = 0.60

# Default upper-right corner card. The cloud slot machinery
# (`overlay_autoplace.resolve_slot`) is tuned for a 25%-tall caption band and
# isn't reused here -- a phone PiP card always starts life in this corner and
# `arbitrate_media_overlays` moves/shrinks/omits it against protected boxes.
_DEFAULT_CARD_X_FRAC = 0.74
_DEFAULT_CARD_Y_FRAC = 0.22
_DEFAULT_CARD_SCALE = 0.36
_MAX_ARBITRATION_IOU = 0.02

_MAX_FACE_ANCHORS_PER_CARD = 4
_MAX_FACE_ANCHORS_TOTAL = 12
_FACE_SAMPLE_TIMEOUT_BASE_S = 2.0
_FACE_SAMPLE_TIMEOUT_PER_ANCHOR_S = 0.3

_MAX_LABEL_LEN = 60
_MAX_REASON_LEN = 160
_MAX_WISHLIST = 5


@dataclass(frozen=True)
class GroundedOverlayCards:
    """The grounding result: ready-to-bind cards + a creator-safe receipt."""

    cards: list[SubtitledOverlayCard] = field(default_factory=list)
    receipt: dict[str, Any] = field(default_factory=dict)


def _label_for_asset(asset: dict) -> str:
    filename = str(asset.get("source_filename") or "").strip()
    if filename:
        return filename.rsplit("/", 1)[-1][:_MAX_LABEL_LEN]
    subject = str((asset.get("analysis") or {}).get("subject") or "").strip()
    if subject:
        return subject[:_MAX_LABEL_LEN]
    return f"visual {asset.get('id', '')}"[:_MAX_LABEL_LEN]


def _evenly_spaced_anchors(start_s: float, end_s: float, n: int) -> list[float]:
    """``n`` sample times centered in each ``1/n`` bucket of ``[start_s, end_s]``.

    Mirrors `app.pipeline.guided_story._evenly_spaced_window_anchors`.
    """
    span = max(0.0, end_s - start_s)
    if span <= 0 or n <= 0:
        return [round(max(0.0, start_s), 3)]
    return [round(start_s + span * (index + 0.5) / n, 3) for index in range(n)]


def _load_ready_pool_assets(
    open_session: Callable[[], AbstractContextManager[Session]], *, job_id: str
) -> list[dict]:
    """The job's plan item's `status == "ready"` pool assets, oldest first.

    A dedicated seam (rather than inlining the query in
    `ground_phone_subtitled_overlays`) so tests can monkeypatch it directly
    without standing up a real database.
    """

    with open_session() as db:
        job = db.get(Job, uuid.UUID(job_id))
        if job is None or job.content_plan_item_id is None:
            return []
        rows = (
            db.execute(
                select(PlanItemAsset)
                .where(
                    PlanItemAsset.plan_item_id == job.content_plan_item_id,
                    PlanItemAsset.status == "ready",
                )
                .order_by(PlanItemAsset.created_at, PlanItemAsset.id)
            )
            .scalars()
            .all()
        )
        return [
            {
                "id": str(row.id),
                "gcs_path": row.gcs_path,
                "gcs_generation": row.gcs_generation,
                "kind": row.kind,
                "source_filename": row.source_filename,
                "duration_s": row.duration_s,
                "aspect": row.aspect,
                "user_context": getattr(row, "user_context", None) or "",
                "analysis": row.analysis or {},
            }
            for row in rows
        ]


def resolve_phone_card_geometry(
    overlays: list[dict[str, Any]],
    *,
    clip_path: str | None,
    job_id: str,
    footprints_by_id: dict[str, MediaFootprint],
) -> tuple[dict[str, dict[str, Any]], dict[str, str], str]:
    """Face-aware, caption-safe geometry arbitration shared by every phone
    card-placement grounding module (KRI-176 overlay grounding, KRI-178
    reaction-beat grounding).

    ``overlays`` are candidate cards, each already carrying ``id``,
    ``start_s``, ``end_s`` and a STARTING ``x_frac``/``y_frac``/``scale`` --
    the caller decides that starting geometry per card kind (e.g. the photo
    vs. sticker default slot) before calling this. Sampling anchors are
    derived from every card's own window, and this function fails open on a
    face-sampling error exactly like `ground_phone_subtitled_overlays` always
    has (no face regions, ``face_sampling == "failed"``).

    Returns ``(resolved_by_id, reason_by_id, face_sampling)``:

    - ``resolved_by_id``: surviving card id -> arbitration's resolved overlay
      dict (its ``x_frac``/``y_frac``/``scale`` may have moved or shrunk).
    - ``reason_by_id``: omitted card id -> ``"no_safe_spot"`` or ``"duplicate"``.
    - ``face_sampling``: ``"ok"`` | ``"failed"`` | ``"skipped"``.
    """

    anchors: list[float] = []
    for overlay in overlays:
        start_s = float(overlay.get("start_s") or 0.0)
        end_s = float(overlay.get("end_s") or start_s)
        anchors.extend(_evenly_spaced_anchors(start_s, end_s, _MAX_FACE_ANCHORS_PER_CARD))
    anchors = anchors[:_MAX_FACE_ANCHORS_TOTAL]

    face_regions: list[ProtectedRegion] = []
    face_sampling = "skipped"
    if clip_path is not None and overlays:
        try:
            face_regions, _receipt = sample_face_regions(
                clip_path,
                anchors,
                max_samples=max(len(anchors), 1),
                timeout_s=(
                    _FACE_SAMPLE_TIMEOUT_BASE_S + _FACE_SAMPLE_TIMEOUT_PER_ANCHOR_S * len(anchors)
                ),
                count_decoded=False,
            )
            face_sampling = "ok"
        except Exception as exc:  # noqa: BLE001 - fail open: keep no face regions
            log.warning(
                "phone_overlay_grounding.face_sampling_failed",
                job_id=job_id,
                error=str(exc)[:200],
            )
            face_regions = []
            face_sampling = "failed"

    protected_boxes: list[ProtectedRegion] = [
        ProtectedRegion(
            0.0,
            float("inf"),
            NormalizedBox(0.0, _CAPTION_PROTECTED_TOP_FRAC, 1.0, 1.0),
            kind="captions",
        ),
        *face_regions,
    ]
    resolved, receipts = arbitrate_media_overlays(
        overlays,
        protected_boxes=protected_boxes,
        footprints_by_id=footprints_by_id,
        max_iou=_MAX_ARBITRATION_IOU,
    )
    resolved_by_id: dict[str, dict[str, Any]] = {str(o.get("id")): o for o in resolved}
    reason_by_id: dict[str, str] = {}
    for r in receipts:
        decision = r.get("decision")
        if decision == "omitted_no_safe_candidate":
            reason_by_id[str(r.get("id"))] = "no_safe_spot"
        elif decision == "omitted_duplicate_asset":
            reason_by_id[str(r.get("id"))] = "duplicate"
    return resolved_by_id, reason_by_id, face_sampling


def _match_placements(
    *,
    job_id: str,
    words: list[dict],
    image_assets: list[dict],
    duration_s: float,
    occupied: list[tuple[float, float]],
) -> tuple[list, list[str], str]:
    """Agent-first, heuristic-fallback matching -- mirrors
    `app.tasks.autoplace.match_overlay_suggestions`'s own matcher selection."""

    if settings.gemini_api_key:
        try:
            from app.agents._model_client import default_client  # noqa: PLC0415
            from app.agents._runtime import RunContext  # noqa: PLC0415
            from app.agents.overlay_placement import (  # noqa: PLC0415
                MAX_PLACEMENT_ASSETS,
                OverlayPlacementAgent,
                OverlayPlacementInput,
                PlacementAsset,
            )
            from app.tasks.autoplace import _shortlist_placement_assets  # noqa: PLC0415

            agent_assets = _shortlist_placement_assets(image_assets, limit=MAX_PLACEMENT_ASSETS)
            agent_out = OverlayPlacementAgent(default_client()).run(
                OverlayPlacementInput(
                    words=words,
                    assets=[
                        PlacementAsset(
                            asset_id=a["id"],
                            kind="image",
                            user_context=str(a.get("user_context") or ""),
                            subject=str((a.get("analysis") or {}).get("subject", "")),
                            description=str((a.get("analysis") or {}).get("description", "")),
                            on_screen_text=str((a.get("analysis") or {}).get("on_screen_text", "")),
                            brands=list((a.get("analysis") or {}).get("brands") or []),
                            duration_s=a.get("duration_s"),
                            aspect=a.get("aspect"),
                            width=(a.get("analysis") or {}).get("width"),
                            height=(a.get("analysis") or {}).get("height"),
                        )
                        for a in agent_assets
                    ],
                    occupied=[list(t) for t in occupied],
                    duration_s=duration_s,
                    archetype="talking_head",
                ),
                ctx=RunContext(job_id=job_id),
            )
            return list(agent_out.placements), list(agent_out.wishlist), "agent"
        except Exception as exc:  # noqa: BLE001 - fall back to the heuristic matcher
            log.warning("phone_overlay_grounding.agent_failed", job_id=job_id, error=str(exc)[:200])
    return heuristic_match(words, image_assets, duration_s=duration_s), [], "heuristic"


def ground_phone_subtitled_overlays(
    open_session: Callable[[], AbstractContextManager[Session]],
    *,
    job_id: str,
    words: list[dict],
    duration_s: float,
    clip_path: str | None,
    occupied: Iterable[tuple[float, float]] = (),
    used_media_ids: frozenset[str] = frozenset(),
) -> GroundedOverlayCards:
    """Match the speaker's transcript against the item's ready Visuals pool
    and resolve face-aware, caption-safe PiP card geometry (KRI-176).

    Fails open at every step: an LLM matcher failure falls back to the
    deterministic heuristic, and a face-sampling failure just means no face
    regions are protected -- this function never raises for those. A
    candidate image is always placed or explained in
    ``receipt["unplaced"]``, never dropped silently. A genuine caller-level
    error (a broken DB session, an unexpected exception) still propagates --
    the runner decides how job-level failures are handled.
    """

    occupied_list = [(float(s), float(e)) for s, e in occupied]
    assets = _load_ready_pool_assets(open_session, job_id=job_id)

    placed: list[dict] = []
    unplaced: list[dict] = []
    image_assets: list[dict] = []
    for asset in assets:
        media_id = str(asset["id"])
        if media_id in used_media_ids:
            continue
        if asset.get("kind") == "video":
            unplaced.append(
                {
                    "media_id": media_id,
                    "label": _label_for_asset(asset),
                    "reason": "video_not_supported",
                }
            )
            continue
        if asset.get("kind") != "image" or not asset.get("gcs_generation"):
            # Defensive: a pool row missing what binding requires is reported
            # rather than silently skipped or silently passed through to a
            # bind failure downstream.
            unplaced.append(
                {
                    "media_id": media_id,
                    "label": _label_for_asset(asset),
                    "reason": "missing_generation",
                }
            )
            continue
        image_assets.append(asset)

    if not image_assets or not words:
        if not words:
            for asset in image_assets:
                unplaced.append(
                    {
                        "media_id": str(asset["id"]),
                        "label": _label_for_asset(asset),
                        "reason": "no_spoken_match",
                    }
                )
        return GroundedOverlayCards(
            cards=[],
            receipt={
                "version": 1,
                "matcher": "none",
                "face_sampling": "skipped",
                "placed": [],
                "unplaced": unplaced,
                "wishlist": [],
            },
        )

    words_mapped = [
        {
            "word": str(w.get("text", "")),
            "start_s": float(w.get("start_s", 0.0)),
            "end_s": float(w.get("end_s", 0.0)),
        }
        for w in words
    ]

    raw, wishlist, matcher = _match_placements(
        job_id=job_id,
        words=words_mapped,
        image_assets=image_assets,
        duration_s=duration_s,
        occupied=occupied_list,
    )

    assets_by_id = {a["id"]: a for a in image_assets}
    drop_reasons: dict[str, str] = {}

    def _trace(event: str, **fields: Any) -> None:
        if event != "autoplace_item_dropped":
            return
        asset_id = str(fields.get("asset_id") or "")
        if asset_id and asset_id not in drop_reasons:
            drop_reasons[asset_id] = str(fields.get("reason") or "")

    suggestions = build_suggestions(
        raw,
        assets_by_id=assets_by_id,
        words=words_mapped,
        duration_s=duration_s,
        occupied=occupied_list,
        glossary=[],
        trace=_trace,
        fullscreen_enabled=False,
        caption_cues=None,
        stats=None,
    )

    # Default upper-right corner geometry for arbitration -- the cloud slot's
    # own x/y/scale (`resolve_slot`) targets a 25%-tall caption band and is
    # deliberately not reused (see module docstring).
    suggestion_by_id: dict[str, dict] = {}
    overlays_for_arbitration: list[dict[str, Any]] = []
    footprints_by_id: dict[str, MediaFootprint] = {}
    for suggestion in suggestions:
        overlay = suggestion["overlay"]
        oid = str(overlay["id"])
        suggestion_by_id[oid] = suggestion
        asset = assets_by_id.get(str(suggestion["asset_id"]))
        aspect = float(asset["aspect"]) if asset and asset.get("aspect") else 1.0
        footprints_by_id[oid] = MediaFootprint(aspect_ratio=aspect)
        start_s = float(overlay.get("start_s") or 0.0)
        end_s = float(overlay.get("end_s") or start_s)
        overlays_for_arbitration.append(
            {
                "id": oid,
                "asset_id": suggestion["asset_id"],
                "src_gcs_path": (asset or {}).get("gcs_path") or overlay.get("src_gcs_path"),
                "position": "custom",
                "x_frac": _DEFAULT_CARD_X_FRAC,
                "y_frac": _DEFAULT_CARD_Y_FRAC,
                "scale": _DEFAULT_CARD_SCALE,
                "start_s": start_s,
                "end_s": end_s,
            }
        )

    resolved_by_id, arbitration_reason_by_id, face_sampling = resolve_phone_card_geometry(
        overlays_for_arbitration,
        clip_path=clip_path,
        job_id=job_id,
        footprints_by_id=footprints_by_id,
    )

    cards: list[SubtitledOverlayCard] = []
    for oid, suggestion in suggestion_by_id.items():
        asset_id = str(suggestion["asset_id"])
        asset = assets_by_id.get(asset_id)
        label = _label_for_asset(asset) if asset else asset_id
        resolved_overlay = resolved_by_id.get(oid)
        if resolved_overlay is None:
            unplaced.append(
                {
                    "media_id": asset_id,
                    "label": label,
                    "reason": arbitration_reason_by_id.get(oid, "no_safe_spot"),
                }
            )
            continue
        # Defensive clamp -- arbitration's own candidate ladder already keeps
        # every accepted spot clear of the caption protected region, but the
        # compiler's own invariant is asserted here too rather than trusted.
        y_frac = min(float(resolved_overlay["y_frac"]), CAPTION_BAND_TOP_FRAC)
        card = SubtitledOverlayCard(
            id=f"pip-{len(cards)}",
            media_id=asset_id,
            gcs_path=asset["gcs_path"],
            generation=str(asset.get("gcs_generation")),
            start_s=float(resolved_overlay["start_s"]),
            end_s=float(resolved_overlay["end_s"]),
            x_frac=float(resolved_overlay["x_frac"]),
            y_frac=y_frac,
            scale=float(resolved_overlay["scale"]),
            fade=True,
            z=0,
        )
        cards.append(card)
        placed.append(
            {
                "media_id": asset_id,
                "label": label,
                "start_s": card.start_s,
                "end_s": card.end_s,
                "reason": str(suggestion.get("reason") or "")[:_MAX_REASON_LEN],
            }
        )

    accounted_for = {p["media_id"] for p in placed} | {u["media_id"] for u in unplaced}
    for asset in image_assets:
        asset_id = str(asset["id"])
        if asset_id in accounted_for:
            continue
        reason = drop_reasons.get(asset_id, "no_spoken_match")
        unplaced.append({"media_id": asset_id, "label": _label_for_asset(asset), "reason": reason})
        accounted_for.add(asset_id)

    receipt = {
        "version": 1,
        "matcher": matcher,
        "face_sampling": face_sampling,
        "placed": placed,
        "unplaced": unplaced,
        "wishlist": list(wishlist)[:_MAX_WISHLIST],
    }
    return GroundedOverlayCards(cards=cards, receipt=receipt)
