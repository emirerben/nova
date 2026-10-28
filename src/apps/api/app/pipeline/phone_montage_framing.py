"""Vertical framing of sideways clips in a phone montage the creator asked about (KRI-1027).

"Some of the clips are horizontal and some vertical, make it all vertical": the
confirm screen now preselects crop for that ask (`render_shape.apply_creator_ask`),
and the phone guided/unified montage writer frames each sideways clip here before
it compiles -- when, and only when, the approved brief carries a bars/crop ask
(`app.kria.landscape_fit_ask`). Nothing is sampled for any other job.

On a "fill" (crop) render each sideways footage moment is framed like the Talking
face fill (`phone_speaker_framing.decide_speaker_framing`, KRI-547) over that
moment's own source window, on the analysis proxy:

* ``face_fill`` -- a recurring face is found and one static 9:16 window holds it:
  the clip slides sideways by the window's ``position_x`` (the same main-track
  shift Talking uses; no new device capability). A montage close-up (a selfie)
  fills most of a 9:16 crop, so a window whose every sampled face keeps its eyes,
  nose and mouth (`phone_speaker_framing.face_core_box`) is accepted even when
  the cheek edge of the widest box touches the crop (``face_core_in_window``);
  a montage has no caption block, so the Talking caption-band limit does not apply.
* ``letterbox`` -- a recurring face moves further than one crop can hold: that
  clip alone keeps black bars, so the crop never cuts a face (``face_moves_too_much``).
* ``centre_fill`` -- no recurring face (B-roll), or the faces could not be
  confirmed: the engine's centre crop, as every "fill" render does today.

The second person at the edge of a two-shot (smaller than the recurring face, or
in too few frames) is not a recurring face; a 9:16 crop of a 16:9 clip shows under
a third of its width, so it is cropped out, and the receipt says the crop follows
the main face. On a "fit" (bars) render nothing is sampled: every sideways clip is
letterboxed, and the receipt says whether the creator asked for that in chat or
chose it on the confirm screen.

The moments carry the decision (``fill_position_x`` / ``fill_letterbox`` on
`guided_story.GuidedStoryMoment`), so the compiler, the face-aware text placement
and every later editor Save recompile see the same crop. The receipt
(`MontageFraming.receipt`, persisted on the variant as ``landscape_framing``) is
what `app.kria.brief_checks` answers the creator's ask from.
"""

from __future__ import annotations

import copy
import os
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

import structlog

from app.pipeline.guided_story import GuidedStoryExecutionPlan, _story_canvas
from app.pipeline.phone_recipe_shared import display_dims
from app.pipeline.phone_speaker_framing import (
    REASON_CREATOR_CHOSE_BARS,
    REASON_FACE_IN_WINDOW,
    REASON_FACE_MOVES,
    REASON_FACE_UNDER_CAPTIONS,
    FaceWindow,
    decide_speaker_framing,
    face_core_box,
    speaker_face_boxes,
)
from app.pipeline.render_geometry import ProtectedRegion, sample_face_regions

log = structlog.get_logger(__name__)

LANDSCAPE_FRAMING_FIELD = "landscape_framing"
_RECEIPT_VERSION = 1

ClipMode = Literal["face_fill", "centre_fill", "letterbox"]
CLIP_MODES: frozenset[str] = frozenset({"face_fill", "centre_fill", "letterbox"})

# Why a sideways clip is framed the way it is, beyond the Talking reasons.
REASON_FACE_CORE_IN_WINDOW = "face_core_in_window"
REASON_ASKED_BARS = "asked_bars"

FaceSampler = Callable[..., tuple[list[ProtectedRegion], dict[str, Any]]]
ProxyDownloader = Callable[[str, str], Any]


@dataclass(frozen=True)
class ClipFraming:
    """How one sideways moment is framed, why, and the face evidence."""

    moment_id: str
    media_id: str
    mode: ClipMode
    reason: str
    window: FaceWindow | None = None
    faces: Mapping[str, Any] = field(default_factory=dict)

    @property
    def position_x(self) -> float | None:
        if self.mode != "face_fill" or self.window is None:
            return None
        return self.window.position_x

    def receipt(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "moment_id": self.moment_id,
            "media_id": self.media_id,
            "mode": self.mode,
            "reason": self.reason,
        }
        if self.window is not None:
            out["window"] = self.window.as_dict()
        if self.faces:
            out["faces"] = dict(self.faces)
        return out


@dataclass(frozen=True)
class MontageFraming:
    """Every sideways moment's framing plus what was asked and rendered."""

    ask: str
    asked_by: tuple[str, ...]
    landscape_fit: str
    output_orientation: str
    clips: tuple[ClipFraming, ...] = ()

    def moment_fields(self) -> dict[str, dict[str, Any]]:
        """``moment_id -> {fill_position_x | fill_letterbox}`` for a "fill" render."""
        if self.landscape_fit != "fill":
            return {}
        fields: dict[str, dict[str, Any]] = {}
        for clip in self.clips:
            if clip.position_x:
                fields[clip.moment_id] = {"fill_position_x": clip.position_x}
            elif clip.mode == "letterbox":
                fields[clip.moment_id] = {"fill_letterbox": True}
        return fields

    def receipt(self) -> dict[str, Any]:
        return {
            "version": _RECEIPT_VERSION,
            "ask": self.ask,
            "asked_by": list(self.asked_by),
            "landscape_fit": self.landscape_fit,
            "output_orientation": self.output_orientation,
            "clips": [clip.receipt() for clip in self.clips],
        }


def _core_inside(boxes: Sequence[Any], window: FaceWindow) -> bool:
    """Every face box's eyes-nose-mouth core lies inside ``window`` (source-normalized)."""
    eps = 1e-6
    return bool(boxes) and all(
        (core := face_core_box(box)).left >= window.left - eps and core.right <= window.right + eps
        for box in boxes
    )


def frame_sideways_clip(
    clip_path: str | None,
    *,
    moment_id: str,
    media_id: str,
    source_start_s: float,
    source_end_s: float,
    display_width: float,
    display_height: float,
    canvas: Any,
    sample: FaceSampler | None = None,
) -> ClipFraming:
    """Frame one sideways moment on a vertical "fill" render. Never raises."""
    seen: list[ProtectedRegion] = []

    def recording(*args: Any, **kwargs: Any) -> tuple[list[ProtectedRegion], dict[str, Any]]:
        regions, receipt = (sample or sample_face_regions)(*args, **kwargs)
        seen.extend(regions)
        return regions, receipt

    speaker = decide_speaker_framing(
        clip_path,
        keep_segments=[(float(source_start_s), float(source_end_s))],
        display_width=display_width,
        display_height=display_height,
        canvas=canvas,
        landscape_fit="fill",
        sample=recording,
    )
    window = speaker.window
    base = {"moment_id": moment_id, "media_id": media_id, "faces": speaker.faces}
    if speaker.mode == "face_fill" or (
        # A montage has no caption block for the face to sit under.
        speaker.reason == REASON_FACE_UNDER_CAPTIONS and window is not None and window.fits
    ):
        return ClipFraming(mode="face_fill", reason=REASON_FACE_IN_WINDOW, window=window, **base)
    if speaker.reason == REASON_FACE_MOVES and window is not None:
        if _core_inside(speaker_face_boxes([region.box for region in seen]), window):
            return ClipFraming(
                mode="face_fill", reason=REASON_FACE_CORE_IN_WINDOW, window=window, **base
            )
        return ClipFraming(mode="letterbox", reason=REASON_FACE_MOVES, window=window, **base)
    # no_face / face_unconfirmed: the engine's centre crop, as every "fill" render.
    return ClipFraming(mode="centre_fill", reason=speaker.reason, **base)


def frame_montage(
    plan: GuidedStoryExecutionPlan,
    bindings: Sequence[Any],
    *,
    ask: str,
    asked_by: Sequence[str],
    landscape_fit: str,
    workdir: str,
    download: ProxyDownloader,
    sample: FaceSampler | None = None,
) -> MontageFraming:
    """Frame every sideways footage moment of ``plan`` for the creator's ``ask``.

    Only bound footage videos on a vertical canvas count: photos, Visuals-pool
    videos, a creator's own re-frame (``source_crop``) and a golden-hour look keep
    whatever the compiler already does with them. A proxy that cannot be fetched
    leaves that clip centre-cropped (``face_unconfirmed``)."""
    canvas = _story_canvas(plan.output_orientation)
    framing = MontageFraming(
        ask=ask,
        asked_by=tuple(asked_by),
        landscape_fit=landscape_fit,
        output_orientation=plan.output_orientation or "portrait",
    )
    if canvas.height <= canvas.width:
        return framing  # a landscape video never has bars, and nothing is cropped sideways
    by_media = {binding.media_id: binding for binding in bindings}
    proxies: dict[str, str | None] = {}

    def proxy_for(binding: Any) -> str | None:
        if binding.media_id not in proxies:
            target = os.path.join(workdir, f"proxy-{len(proxies)}.mp4")
            try:
                download(binding.proxy_path, target)
                proxies[binding.media_id] = target
            except Exception as exc:  # noqa: BLE001 - that clip is centre-cropped
                log.warning(
                    "phone_montage_framing.proxy_download_failed",
                    media_id=binding.media_id,
                    error=str(exc)[:200],
                )
                proxies[binding.media_id] = None
        return proxies[binding.media_id]

    clips: list[ClipFraming] = []
    for moment in plan.story_timeline:
        binding = by_media.get(moment.media_id)
        if (
            binding is None
            or moment.lane != "clip"
            or moment.kind != "video"
            or moment.source_crop is not None
            or moment.look_preset == "golden_hour"
        ):
            continue
        width, height = display_dims(binding.original)
        if width <= height:
            continue
        if landscape_fit != "fill":
            reason = REASON_ASKED_BARS if ask == "fit" else REASON_CREATOR_CHOSE_BARS
            clips.append(ClipFraming(moment.moment_id, moment.media_id, "letterbox", reason))
            continue
        clips.append(
            frame_sideways_clip(
                proxy_for(binding),
                moment_id=moment.moment_id,
                media_id=moment.media_id,
                source_start_s=moment.source_start_s,
                source_end_s=moment.source_end_s,
                display_width=width,
                display_height=height,
                canvas=canvas,
                sample=sample,
            )
        )
    return MontageFraming(
        ask=framing.ask,
        asked_by=framing.asked_by,
        landscape_fit=framing.landscape_fit,
        output_orientation=framing.output_orientation,
        clips=tuple(clips),
    )


def apply_montage_framing(raw_plan: dict[str, Any], framing: MontageFraming) -> dict[str, Any]:
    """``raw_plan`` with each framed moment's ``fill_position_x`` / ``fill_letterbox``
    set (a copy; ``raw_plan`` itself when nothing changes)."""
    fields = framing.moment_fields()
    if not fields:
        return raw_plan
    updated = copy.deepcopy(raw_plan)
    for row in updated.get("story_timeline") or []:
        if isinstance(row, dict) and row.get("moment_id") in fields:
            row.update(fields[row["moment_id"]])
    return updated


__all__ = [
    "CLIP_MODES",
    "LANDSCAPE_FRAMING_FIELD",
    "REASON_ASKED_BARS",
    "REASON_FACE_CORE_IN_WINDOW",
    "ClipFraming",
    "MontageFraming",
    "apply_montage_framing",
    "frame_montage",
    "frame_sideways_clip",
]
