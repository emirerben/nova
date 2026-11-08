"""Face-aware placement of guided-story title/chapter text on phone renders (KRI-140, KRI-116).

The cloud burn moves a title off a face by sampling the assembled video
(`guided_story._apply_guided_text_face_placement`). A phone render never
assembles one: the device composes from the original files. So the same
decision is made at plan time from the analysis PROXIES, and the chosen
``y_frac`` is baked into the plan's text rows before the recipe compiles --
the device then renders the text exactly where the cloud would have put it.

Faces are sampled per moment (proxy time = ``source_start + offset`` into the
moment), then mapped through the moment's crop and the engine's cover-fit into
output-canvas coordinates so they can be compared with the measured text box.
Fail-open by design, exactly like the cloud pass: any download / sampling
failure leaves the authored position untouched.
"""

from __future__ import annotations

import copy
import os
import tempfile
from collections.abc import Callable, Mapping, Sequence
from typing import Any

import structlog

from app.kria.protected_targets import ProtectedRegionSelection, ProtectedTarget
from app.pipeline.guided_story import (
    GuidedStoryExecutionPlan,
    GuidedStoryMoment,
    GuidedTextProtection,
    _apply_guided_text_face_placement,
    _story_canvas,
)
from app.pipeline.object_track import STEP_S, ObjectTrack, track_object
from app.pipeline.phone_protected_text import ProtectedPlan, binding_ask, protected_text_receipt
from app.pipeline.render_geometry import NormalizedBox, ProtectedRegion, sample_face_regions
from app.services.phone_sources import PhoneSourceBinding
from app.text_fold import fold_text

log = structlog.get_logger(__name__)

FaceSampler = Callable[..., tuple[list[ProtectedRegion], dict[str, Any]]]
ProxyDownloader = Callable[[str, str], Any]


def face_box_to_canvas(
    box: NormalizedBox,
    *,
    source_width: float,
    source_height: float,
    canvas_width: float,
    canvas_height: float,
    crop: Mapping[str, float] | None = None,
) -> NormalizedBox | None:
    """Map a face box (normalized to the upright source frame) onto the canvas.

    Mirrors the device: the optional crop rectangle is taken first, then the
    result is cover-fitted (scaled to fill, centred, overflow trimmed) to the
    canvas. Returns ``None`` when the face lies wholly outside what is shown.
    """

    crop_x = float(crop["x"]) if crop else 0.0
    crop_y = float(crop["y"]) if crop else 0.0
    crop_w = float(crop["width"]) if crop else 1.0
    crop_h = float(crop["height"]) if crop else 1.0
    region_aspect = (crop_w * source_width) / (crop_h * source_height)
    canvas_aspect = canvas_width / canvas_height
    # Portion of the cropped region that survives the cover-fit, per axis.
    visible_w = min(1.0, canvas_aspect / region_aspect)
    visible_h = min(1.0, region_aspect / canvas_aspect)
    left = crop_x + crop_w * (1 - visible_w) / 2
    top = crop_y + crop_h * (1 - visible_h) / 2
    span_w = crop_w * visible_w
    span_h = crop_h * visible_h
    mapped = NormalizedBox(
        max(0.0, (box.left - left) / span_w),
        max(0.0, (box.top - top) / span_h),
        min(1.0, (box.right - left) / span_w),
        min(1.0, (box.bottom - top) / span_h),
    )
    if mapped.right <= mapped.left or mapped.bottom <= mapped.top:
        return None
    return mapped


def shown_crop(
    moment: GuidedStoryMoment,
    *,
    source_width: float,
    source_height: float,
    canvas_width: float,
    canvas_height: float,
) -> Mapping[str, float] | None:
    """The part of the source the device shows for ``moment``, as a crop rectangle.

    The moment's own ``source_crop``, else -- for a sideways clip slid to keep its
    face in a vertical crop (KRI-1027 ``fill_position_x``) -- the canvas-shaped
    window that shift shows, so faces map onto the canvas where the device draws
    them. None = the engine's centred cover fill."""
    if moment.source_crop is not None:
        return moment.source_crop
    shift = moment.fill_position_x
    if not shift or source_width <= 0 or source_height <= 0:
        return None
    cover = max(canvas_width / source_width, canvas_height / source_height)
    shown_w = source_width * cover
    window = min(1.0, canvas_width / shown_w)
    left = min(max(0.5 - window / 2 - float(shift) / shown_w, 0.0), 1.0 - window)
    return {"x": left, "y": 0.0, "width": window, "height": 1.0}


def _moment_at(plan: GuidedStoryExecutionPlan, at_s: float) -> GuidedStoryMoment | None:
    for moment in plan.story_timeline:
        if moment.output_start_s <= at_s < moment.output_end_s:
            return moment
    return None


class ProxyCache:
    """Each footage proxy downloaded at most once into ``workdir``; ``None`` when it could
    not be fetched (that source is then simply unsampled)."""

    def __init__(self, workdir: str, download: ProxyDownloader) -> None:
        self._workdir = workdir
        self._download = download
        self._local: dict[str, str | None] = {}

    def path(self, binding: PhoneSourceBinding) -> str | None:
        if binding.media_id not in self._local:
            target = os.path.join(self._workdir, f"proxy-{len(self._local)}.mp4")
            try:
                self._download(binding.proxy_path, target)
                self._local[binding.media_id] = target
            except Exception as exc:  # noqa: BLE001 - fail open: this source is unsampled
                log.warning(
                    "phone_guided_text_placement.proxy_download_failed",
                    media_id=binding.media_id,
                    error=str(exc)[:200],
                )
                self._local[binding.media_id] = None
        return self._local[binding.media_id]


def _upright_dims(binding: PhoneSourceBinding) -> tuple[float, float]:
    original = binding.original
    width, height = float(original.width or 0), float(original.height or 0)
    if original.orientation_degrees in (90, 270):
        width, height = height, width
    return width, height


def build_phone_face_sampler(
    plan: GuidedStoryExecutionPlan,
    bindings: Sequence[PhoneSourceBinding],
    *,
    workdir: str,
    download: ProxyDownloader,
    sample: FaceSampler = sample_face_regions,
    observe_frames=None,
    proxies: ProxyCache | None = None,
) -> Callable[[list[float], float], tuple[list[ProtectedRegion], dict[str, Any]]]:
    """A ``sampler(output_anchors_s, timeout_s)`` over the plan's footage proxies.

    Only bound-footage video moments can be sampled (a pool photo/video has no
    analysis proxy); an anchor over anything else simply isn't decoded, so the
    chooser sees less coverage and, below its floor, keeps the authored spot.
    """

    canvas = _story_canvas(plan.output_orientation)
    by_media = {binding.media_id: binding for binding in bindings}
    cache = proxies or ProxyCache(workdir, download)
    proxy_for = cache.path

    def sampler(anchors: list[float], timeout_s: float):
        grouped: dict[str, list[tuple[GuidedStoryMoment, float]]] = {}
        for at_s in anchors:
            moment = _moment_at(plan, at_s)
            if moment is None or moment.lane != "clip" or moment.kind != "video":
                continue
            if moment.media_id not in by_media:
                continue
            source_at = moment.source_start_s + (at_s - moment.output_start_s)
            grouped.setdefault(moment.media_id, []).append((moment, source_at))

        regions: list[ProtectedRegion] = []
        receipt: dict[str, Any] = {
            "attempted": len(anchors),
            "decoded": 0,
            "detected": 0,
            "timed_out": False,
            "partial": False,
            "sources_sampled": 0,
        }
        share = timeout_s / max(len(grouped), 1)
        for media_id, points in grouped.items():
            binding = by_media[media_id]
            path = proxy_for(binding)
            if path is None:
                receipt["worker_error"] = "proxy_unavailable"
                continue
            found, group_receipt = sample(
                path,
                [source_at for _moment, source_at in points],
                max_samples=max(len(points), 1),
                timeout_s=share,
                count_decoded=True,
            )
            receipt["sources_sampled"] += 1
            receipt["decoded"] += int(group_receipt.get("decoded") or 0)
            receipt["timed_out"] = receipt["timed_out"] or bool(group_receipt.get("timed_out"))
            receipt["partial"] = receipt["partial"] or bool(group_receipt.get("partial"))
            if group_receipt.get("worker_error"):
                receipt["worker_error"] = group_receipt["worker_error"]
            width, height = _upright_dims(binding)
            if width <= 0 or height <= 0:
                continue
            if observe_frames is not None:
                observe_frames(path, points)
            for region in found:
                # Regions come back keyed by proxy time; re-key to the moment
                # that owns the anchor so they read on the output timeline.
                owner = next(
                    (
                        moment
                        for moment, source_at in points
                        if region.start_s - 1e-3 <= source_at <= region.end_s + 1e-3
                    ),
                    points[0][0],
                )
                mapped = face_box_to_canvas(
                    region.box,
                    source_width=width,
                    source_height=height,
                    canvas_width=canvas.width,
                    canvas_height=canvas.height,
                    crop=shown_crop(
                        owner,
                        source_width=width,
                        source_height=height,
                        canvas_width=canvas.width,
                        canvas_height=canvas.height,
                    ),
                )
                if mapped is not None:
                    regions.append(
                        ProtectedRegion(owner.output_start_s, owner.output_end_s, mapped, "face")
                    )
        receipt["detected"] = len(regions)
        return regions, receipt

    return sampler


_UNMEASURED = {"objects_verified": False, "faces": "fallback"}


def _object_rows(
    plan: GuidedStoryExecutionPlan,
    bindings: Sequence[PhoneSourceBinding],
    targets: Sequence[ProtectedTarget],
    selections: Sequence[ProtectedRegionSelection],
    proxies: ProxyCache,
    track: Callable[..., ObjectTrack],
) -> tuple[list[dict[str, Any]], list[tuple[GuidedStoryMoment, ObjectTrack, PhoneSourceBinding]]]:
    """One protected-text receipt row per target, and the creator's marks followed through
    every footage moment of the clip they were drawn on.

    A mark counts only against the clip it was drawn on, at the generation it was drawn at:
    another clip is ``other_source``, replaced bytes are ``invalidated``, no mark is
    ``no_selection``, and a proxy that cannot be read is ``unverified`` (never clear)."""
    by_media = {binding.media_id: binding for binding in bindings}
    moments_of: dict[str, list[GuidedStoryMoment]] = {}
    for moment in plan.story_timeline:
        if moment.lane == "clip" and moment.kind == "video" and moment.media_id in by_media:
            moments_of.setdefault(moment.media_id, []).append(moment)
    rows: list[dict[str, Any]] = []
    tracks: list[tuple[GuidedStoryMoment, ObjectTrack, PhoneSourceBinding]] = []
    for target in targets:
        row: dict[str, Any] = {
            "kind": target.kind,
            "label": target.label,
            "requirement_ids": list(target.requirement_ids),
        }
        if target.kind == "face":
            rows.append({**row, "grounding": "face_sampler"})
            continue
        marks = [m for m in selections if fold_text(m.label) == fold_text(target.label)]
        if not marks:
            rows.append({**row, "status": "no_selection"})
            continue
        on_story = [m for m in marks if m.media_id in moments_of]
        if not on_story:
            other = {"selection": marks[-1].model_dump(mode="json"), "status": "other_source"}
            rows.append({**row, **other})
            continue
        row["selection"] = on_story[-1].model_dump(mode="json")
        found: list[ObjectTrack] = []
        status: str | None = None
        for mark in on_story:
            binding = by_media[mark.media_id]
            if binding.generation and mark.generation != binding.generation:
                status = "invalidated"
                row["current_generation"] = binding.generation
                break
            path = proxies.path(binding)
            if path is None:
                status = "unverified"
                row["reason"] = "proxy_unavailable"
                break
            for moment in moments_of[mark.media_id]:
                followed = track(
                    path,
                    label=target.label,
                    media_id=mark.media_id,
                    generation=binding.generation,
                    seed_t_s=mark.source_t_s,
                    seed_box=NormalizedBox(*mark.box),
                    start_s=moment.source_start_s,
                    end_s=moment.source_end_s,
                )
                found.append(followed)
                tracks.append((moment, followed, binding))
        if status is not None:
            rows.append({**row, "status": status})
            continue
        rows.append(
            {
                **row,
                "grounding": "creator_mark_tracked",
                "track_status": "tracked"
                if all(t.status == "tracked" for t in found)
                else "unverified",
                "track": [t.receipt for t in found],
                "provenance": found[0].provenance(),
            }
        )
    return rows, tracks


def _objects_on_canvas(
    plan: GuidedStoryExecutionPlan,
    tracks: Sequence[tuple[GuidedStoryMoment, ObjectTrack, PhoneSourceBinding]],
) -> Callable[[float, float], tuple[list[NormalizedBox], bool]]:
    canvas = _story_canvas(plan.output_orientation)

    def objects(start_s: float, end_s: float) -> tuple[list[NormalizedBox], bool]:
        boxes: list[NormalizedBox] = []
        seen = True
        for moment, followed, binding in tracks:
            low = max(start_s, moment.output_start_s)
            high = min(end_s, moment.output_end_s)
            if high <= low:
                continue
            width, height = _upright_dims(binding)
            if width <= 0 or height <= 0:
                seen = False
                continue
            crop = shown_crop(
                moment,
                source_width=width,
                source_height=height,
                canvas_width=canvas.width,
                canvas_height=canvas.height,
            )
            steps = max(1, round((high - low) / STEP_S))
            for index in range(steps + 1):
                at_s = low + (high - low) * index / steps
                source_s = moment.source_start_s + at_s - moment.output_start_s
                box, was_seen = followed.box_at(source_s)
                seen = seen and was_seen
                mapped = (
                    face_box_to_canvas(
                        box,
                        source_width=width,
                        source_height=height,
                        canvas_width=canvas.width,
                        canvas_height=canvas.height,
                        crop=crop,
                    )
                    if box is not None
                    else None
                )
                if mapped is not None:
                    boxes.append(mapped)
        return boxes, seen

    return objects


def _receipt(
    targets: Sequence[ProtectedTarget],
    target_rows: Sequence[dict[str, Any]],
    placements: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    plan = ProtectedPlan(
        targets=tuple(targets),
        target_rows=tuple(target_rows),
        protect_all_faces=any(t.kind == "face" for t in targets),
    )
    return protected_text_receipt(plan, placements)


def unplaced_guided_receipt(raw_binding: Mapping[str, Any] | None) -> dict[str, Any] | None:
    """The protected-text receipt of a guided phone render whose text this pass did not place
    (the text-style policy or the placement flag owns it): an ask is reported ``unverified``,
    never left out so the review reads it as met. ``None`` when nothing was asked."""
    ask = binding_ask(raw_binding)
    if ask is None:
        return None
    targets, selections = ask
    marked = {fold_text(m.label) for m in selections}
    rows = [
        {"kind": t.kind, "label": t.label, "requirement_ids": list(t.requirement_ids)}
        | (
            {"grounding": "face_sampler"}
            if t.kind == "face"
            else {"status": "unverified" if fold_text(t.label) in marked else "no_selection"}
        )
        for t in targets
    ]
    return _receipt(targets, rows, {"guided_text": _UNMEASURED})


def place_guided_text(
    plan: GuidedStoryExecutionPlan,
    text_element_rows: list[dict[str, Any]],
    bindings: Sequence[PhoneSourceBinding],
    *,
    job_id: str,
    download: ProxyDownloader,
    sample: FaceSampler = sample_face_regions,
    raw_binding: Mapping[str, Any] | None = None,
    track: Callable[..., ObjectTrack] = track_object,
) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    """``text_element_rows`` (the plan's raw rows) with title/chapter/closing rows moved
    off faces, and, when the approved binding asks to keep text off a face or a marked
    object (KRI-903), off every face and off each mark followed through the footage, plus
    the protected-text receipt saying per target whether every text stayed clear
    (``None`` when nothing was asked).

    Returns the rows unchanged (a copy) when there is nothing eligible, no footage to
    sample, or anything goes wrong; an ask is then reported ``unverified``.
    """

    rows = copy.deepcopy(text_element_rows)
    ask = binding_ask(raw_binding)
    unverified = unplaced_guided_receipt(raw_binding)
    if not rows or not bindings or not plan.story_timeline:
        return rows, (unverified if rows else None)
    protection: GuidedTextProtection | None = None
    target_rows: list[dict[str, Any]] = []
    placements: dict[str, dict[str, Any]] = {}
    try:
        with tempfile.TemporaryDirectory(prefix="phone-text-placement-") as workdir:
            proxies = ProxyCache(workdir, download)
            sampler = build_phone_face_sampler(
                plan, bindings, workdir=workdir, download=download, sample=sample, proxies=proxies
            )
            if ask is not None:
                targets, selections = ask
                target_rows, tracks = _object_rows(
                    plan, bindings, targets, selections, proxies, track
                )
                protection = GuidedTextProtection(
                    all_faces=any(t.kind == "face" for t in targets),
                    objects=_objects_on_canvas(plan, tracks) if tracks else None,
                )
            placed = _apply_guided_text_face_placement(
                None,
                rows,
                job_id=job_id,
                canvas=_story_canvas(plan.output_orientation),
                duration_s=float(plan.resolved_duration_s),
                sampler=sampler,
                protection=protection,
                placements_out=placements,
            )
    except Exception as exc:  # noqa: BLE001 - never block a render on a placement nicety
        log.warning("phone_guided_text_placement.failed", job_id=job_id, error=str(exc)[:200])
        return copy.deepcopy(text_element_rows), unverified
    if ask is None:
        return placed, None
    for row in placed:
        if row.get("text") and str(row.get("id", "")) not in placements:
            placements["other_text"] = dict(_UNMEASURED)
    return placed, _receipt(ask[0], target_rows, placements)


def place_guided_text_off_faces(
    plan: GuidedStoryExecutionPlan,
    text_element_rows: list[dict[str, Any]],
    bindings: Sequence[PhoneSourceBinding],
    *,
    job_id: str,
    download: ProxyDownloader,
    sample: FaceSampler = sample_face_regions,
) -> list[dict[str, Any]]:
    """`place_guided_text` for a render that asked for nothing: the rows only."""
    return place_guided_text(
        plan, text_element_rows, bindings, job_id=job_id, download=download, sample=sample
    )[0]
