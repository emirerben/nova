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

from app.pipeline.guided_story import (
    GuidedStoryExecutionPlan,
    GuidedStoryMoment,
    _apply_guided_text_face_placement,
    _story_canvas,
)
from app.pipeline.render_geometry import NormalizedBox, ProtectedRegion, sample_face_regions
from app.services.phone_sources import PhoneSourceBinding

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


def _moment_at(plan: GuidedStoryExecutionPlan, at_s: float) -> GuidedStoryMoment | None:
    for moment in plan.story_timeline:
        if moment.output_start_s <= at_s < moment.output_end_s:
            return moment
    return None


def build_phone_face_sampler(
    plan: GuidedStoryExecutionPlan,
    bindings: Sequence[PhoneSourceBinding],
    *,
    workdir: str,
    download: ProxyDownloader,
    sample: FaceSampler = sample_face_regions,
) -> Callable[[list[float], float], tuple[list[ProtectedRegion], dict[str, Any]]]:
    """A ``sampler(output_anchors_s, timeout_s)`` over the plan's footage proxies.

    Only bound-footage video moments can be sampled (a pool photo/video has no
    analysis proxy); an anchor over anything else simply isn't decoded, so the
    chooser sees less coverage and, below its floor, keeps the authored spot.
    """

    canvas = _story_canvas(plan.output_orientation)
    by_media = {binding.media_id: binding for binding in bindings}
    local_by_media: dict[str, str | None] = {}

    def proxy_for(binding: PhoneSourceBinding) -> str | None:
        if binding.media_id not in local_by_media:
            target = os.path.join(workdir, f"proxy-{len(local_by_media)}.mp4")
            try:
                download(binding.proxy_path, target)
                local_by_media[binding.media_id] = target
            except Exception as exc:  # noqa: BLE001 - fail open: this source is unsampled
                log.warning(
                    "phone_guided_text_placement.proxy_download_failed",
                    media_id=binding.media_id,
                    error=str(exc)[:200],
                )
                local_by_media[binding.media_id] = None
        return local_by_media[binding.media_id]

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
            original = binding.original
            width, height = float(original.width or 0), float(original.height or 0)
            if original.orientation_degrees in (90, 270):
                width, height = height, width
            if width <= 0 or height <= 0:
                continue
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
                    crop=owner.source_crop,
                )
                if mapped is not None:
                    regions.append(
                        ProtectedRegion(owner.output_start_s, owner.output_end_s, mapped, "face")
                    )
        receipt["detected"] = len(regions)
        return regions, receipt

    return sampler


def place_guided_text_off_faces(
    plan: GuidedStoryExecutionPlan,
    text_element_rows: list[dict[str, Any]],
    bindings: Sequence[PhoneSourceBinding],
    *,
    job_id: str,
    download: ProxyDownloader,
    sample: FaceSampler = sample_face_regions,
) -> list[dict[str, Any]]:
    """``text_element_rows`` (the plan's raw rows) with title/chapter/closing
    rows moved off faces.

    Returns the rows unchanged (a copy) when there is nothing eligible, no
    footage to sample, or anything goes wrong.
    """

    rows = copy.deepcopy(text_element_rows)
    if not rows or not bindings or not plan.story_timeline:
        return rows
    try:
        with tempfile.TemporaryDirectory(prefix="phone-text-placement-") as workdir:
            sampler = build_phone_face_sampler(
                plan, bindings, workdir=workdir, download=download, sample=sample
            )
            return _apply_guided_text_face_placement(
                None,
                rows,
                job_id=job_id,
                canvas=_story_canvas(plan.output_orientation),
                duration_s=float(plan.resolved_duration_s),
                sampler=sampler,
            )
    except Exception as exc:  # noqa: BLE001 - never block a render on a placement nicety
        log.warning("phone_guided_text_placement.failed", job_id=job_id, error=str(exc)[:200])
        return copy.deepcopy(text_element_rows)
