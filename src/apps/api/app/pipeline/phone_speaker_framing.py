"""Face-filled vertical crop for a sideways phone Talking speaker clip (KRI-547).

A creator filmed a talk-to-camera take sideways (1920x1080) and asked for a
vertical video that keeps their face in frame. The phone Talking compiler
letterboxes a landscape speaker clip at the item's default ``landscape_fit="fit"``
(`phone_recipe_shared.fit_transform`, KRI-283), and its ``"fill"`` alternative is
a blind centre crop that cuts a speaker standing in the left third out of the
picture.

When -- and only when -- the creator asked (`app.kria.speaker_framing_ask`), the
worker calls `decide_speaker_framing`:

1. The speaker's face is sampled across the whole kept take (`sample_anchors`,
   ~one frame every 1.25 s, 6..24 frames), on the analysis proxy, with the same
   killable OpenCV sampler every face-aware placement uses
   (`render_geometry.sample_face_regions`, raw detector boxes).
2. The speaker is every detection the size of the recurring face
   (`speaker_face_boxes`: sized against `render_geometry._dominant_face_cluster`,
   so a head that moved still counts while a small background face or a giant
   background merge does not), and must be present on most frames.
3. `face_fill_window` finds ONE static window -- the engine's cover-fill of the
   clip, slid sideways -- centred on the union of every sampled face box. It is
   accepted when each box keeps at least `MIN_FACE_VISIBLE_FRACTION` of its width
   inside (a Haar box already runs past the cheeks, so that is the face's own
   margin; when the union is narrower than the window, centring it splits the
   spare width into equal margins on both sides).
4. The face must also stay above the caption block (`FACE_BOTTOM_LIMIT_FRAC`):
   letterboxed captions sit on the black bar, but on a face-filled crop they sit
   on the picture, and text never covers a face.

The outcome is a `SpeakerFraming`: ``face_fill`` (identity scale plus a
``MediaTransform.position_x`` shift, which the iOS engine already applies to
main-track clips -- no new capability, no app update), or today's framing
(``letterbox`` for "fit", ``centre_fill`` for "fill") with the reason the face
crop was not used. Its `SpeakerFraming.receipt` is persisted on the variant as
``speaker_framing``: `app.kria.brief_checks` answers the creator's ask from it at
render-ready, and the phone editor Save (`editor_speaker_framing`) keeps the crop,
or resets it when the creator picks black bars.

Everything that places text or cards against the speaker's face on this canvas
must map the face through the same crop (`face_core_mapper` for cards, and
`phone_subtitled_title.place_talking_title(position_x=...)`).
"""

from __future__ import annotations

import math
import statistics
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

from app.pipeline.phone_recipe_shared import max_cover_shift_px
from app.pipeline.phone_subtitled_title import _output_to_source_s, source_box_to_canvas
from app.pipeline.render_geometry import (
    NormalizedBox,
    ProtectedRegion,
    _dominant_face_cluster,
    _union_box,
    clamp_face_width,
    sample_face_regions,
)

SPEAKER_FRAMING_FIELD = "speaker_framing"
_RECEIPT_VERSION = 1

FramingMode = Literal["face_fill", "letterbox", "centre_fill"]
FRAMING_MODES: frozenset[str] = frozenset({"face_fill", "letterbox", "centre_fill"})

# Why the speaker was framed the way it was (the receipt's ``reason``).
REASON_FACE_IN_WINDOW = "face_in_window"
REASON_FACE_MOVES = "face_moves_too_much"
REASON_NO_FACE = "no_face"
REASON_FACE_UNCONFIRMED = "face_unconfirmed"
REASON_FACE_UNDER_CAPTIONS = "face_under_captions"
REASON_NOT_LANDSCAPE = "not_landscape"
REASON_CREATOR_CHOSE_BARS = "creator_chose_bars"
REASON_CREATOR_CHOSE_CROP = "creator_chose_crop"

# Every sampled face box keeps at least this share of its width inside the
# window. A Haar frontal box spans cheek to cheek plus a little; its outer 5% is
# the edge of the cheek or background, never an eye or the mouth (measured on the
# Kadıköy take: the worst frame keeps 98.8%, ~11 canvas px of hair at the edge).
MIN_FACE_VISIBLE_FRACTION = 0.95
# The caption block is centred at 0.80 of the canvas height
# (`phone_captions`: MarginV 384 of 1920); up to three 78 px lines put its top
# at ~0.72. A face reaching below that would sit under the captions.
FACE_BOTTOM_LIMIT_FRAC = 0.72

# Sampling budget: one frame per ~1.25 s of kept take, 6..24 frames, the same
# per-anchor time budget as the title placement sampler.
_ANCHOR_SPACING_S = 1.25
_MIN_ANCHORS = 6
_MAX_ANCHORS = 24
_TIMEOUT_BASE_S = 2.5
_TIMEOUT_PER_ANCHOR_S = 0.35
# A frame decision needs the sampler to have actually looked: at least 80% of
# the anchors decoded (a timeout that kept only some of them is not "the whole
# clip"), and the speaker's face on at least 60% of the decoded frames
# (`render_geometry._DOMINANT_FACE_MIN_PRESENCE`), never fewer than 3.
_MIN_DECODED_SHARE = 0.8
_MIN_PRESENCE = 0.6
_MIN_FACE_SAMPLES = 3
# KRI-547 follow-up: the part of a RAW Haar frontal-face box that holds the eyes,
# nose and mouth -- the only part a beat/PiP card may never cover on a
# face-filled close-up. The OpenCV frontal cascade is trained on square windows
# framed brow-to-chin with the eyes about a third of the way down and the mouth
# about four fifths; measured on the Kadıköy take (3 frames, 8.3-12.1 s): brows at
# 0.27 of the box height, eyes 0.35-0.42, mouth 0.75-0.85, outer eye corners at
# 0.22-0.26 and 0.73-0.75 of its width. The core runs from just above the brows
# to below the lower lip, and keeps >= 0.07 box widths of cheek beyond each outer
# eye corner; the canvas margin adds ~22 px across and ~38 px down on 1080x1920.
FACE_CORE_X = (0.15, 0.85)
FACE_CORE_Y = (0.20, 0.92)
FACE_CORE_MARGIN_FRAC = 0.02
# The speaker's face is the recurring detection's size: a detection more than
# 1.5x its median width is a detector merge with the background, one under 0.6x
# is a face in the background (the sampler keeps each frame's largest face, so
# that only happens on a frame where the speaker was missed). Anything in between
# is the speaker, wherever it moved -- and the window must hold all of it.
_MAX_WIDTH_RATIO = 1.5
_MIN_WIDTH_RATIO = 0.6

FaceSampler = Callable[..., tuple[list[ProtectedRegion], dict[str, Any]]]


class _Canvas(Protocol):
    @property
    def width(self) -> int: ...

    @property
    def height(self) -> int: ...


@dataclass(frozen=True, slots=True)
class FaceWindow:
    """One static 9:16 window over the source (normalized to its width) and the
    ``position_x`` (canvas px, +right) that puts it on screen."""

    left: float
    right: float
    position_x: float
    # The smallest share of any sampled face box's width inside the window.
    worst_visible: float
    # The tightest gap between a face box and a window edge, canvas px
    # (negative = that box is clipped by this much).
    margin_px: float

    @property
    def fits(self) -> bool:
        return self.worst_visible >= MIN_FACE_VISIBLE_FRACTION - 1e-9

    def as_dict(self) -> dict[str, Any]:
        return {
            "left": self.left,
            "right": self.right,
            "position_x": self.position_x,
            "worst_visible": self.worst_visible,
            "margin_px": self.margin_px,
            "fits": self.fits,
        }


@dataclass(frozen=True)
class SpeakerFraming:
    """How the speaker clip is framed, why, and the evidence (the receipt)."""

    mode: FramingMode
    reason: str
    window: FaceWindow | None = None
    # True when ``window`` passed every check: the face crop the creator gets
    # if they choose crop over bars in the editor later.
    eligible: bool = False
    faces: Mapping[str, Any] = field(default_factory=dict)
    asked_by: tuple[str, ...] = ()

    @property
    def position_x(self) -> float | None:
        """The main-track shift to compile with; ``None`` unless face-filled."""
        if self.mode != "face_fill" or self.window is None:
            return None
        return self.window.position_x

    def receipt(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "version": _RECEIPT_VERSION,
            "mode": self.mode,
            "reason": self.reason,
            "eligible": self.eligible,
        }
        if self.asked_by:
            out["asked_by"] = list(self.asked_by)
        if self.window is not None:
            out["window"] = self.window.as_dict()
        if self.faces:
            out["faces"] = dict(self.faces)
        return out


def today_mode(landscape_fit: str) -> FramingMode:
    """What a landscape speaker gets without the face crop: bars for "fit",
    the engine's centre crop for "fill" (`phone_recipe_shared.fit_transform`)."""
    return "letterbox" if landscape_fit == "fit" else "centre_fill"


def _shown_size(
    display_width: float, display_height: float, canvas: _Canvas
) -> tuple[float, float]:
    """The cover-filled source size on the canvas, in canvas px."""
    cover = max(canvas.width / display_width, canvas.height / display_height)
    return display_width * cover, display_height * cover


def face_fill_window(
    face_boxes: Sequence[NormalizedBox],
    *,
    display_width: float,
    display_height: float,
    canvas: _Canvas,
) -> FaceWindow | None:
    """The static window that best holds every face box (source-normalized).

    Pure math, no thresholds: the window is the canvas width of the engine's
    cover-fill, centred on the union of ``face_boxes`` and clamped inside the
    source; `FaceWindow.fits` says whether it holds them. ``None`` without a
    usable box or source size."""
    boxes = [box for box in face_boxes if box.width > 0]
    if not boxes or display_width <= 0 or display_height <= 0:
        return None
    shown_w, _shown_h = _shown_size(display_width, display_height, canvas)
    window = min(1.0, canvas.width / shown_w)
    union = _union_box(boxes)
    centre = (union.left + union.right) / 2
    left = min(max(centre - window / 2, 0.0), 1.0 - window)
    right = left + window
    worst = min(max(0.0, min(box.right, right) - max(box.left, left)) / box.width for box in boxes)
    margin = min(min(box.left - left, right - box.right) for box in boxes)
    limit = max_cover_shift_px(display_width, display_height, canvas)
    position_x = max(-limit, min(limit, (0.5 - (left + window / 2)) * shown_w))
    return FaceWindow(
        left=round(left, 5),
        right=round(right, 5),
        position_x=round(position_x, 2) + 0.0,
        worst_visible=round(worst, 4),
        margin_px=round(margin * shown_w, 1) + 0.0,
    )


def sample_anchors(keep_segments: Sequence[tuple[float, float]]) -> list[float]:
    """Source-clip seconds to sample, spread evenly over the kept take (the
    cleanup cut's kept spans, else the whole clip)."""
    spans = [(float(start), float(end)) for start, end in keep_segments if end > start]
    total = sum(end - start for start, end in spans)
    if total <= 0:
        return []
    count = max(_MIN_ANCHORS, min(_MAX_ANCHORS, math.ceil(total / _ANCHOR_SPACING_S)))
    anchors = {
        round(source_at, 3)
        for index in range(count)
        if (source_at := _output_to_source_s(total * (index + 0.5) / count, spans)) is not None
    }
    return sorted(anchors)


def speaker_face_boxes(boxes: Sequence[NormalizedBox]) -> list[NormalizedBox]:
    """The detections that are the speaker: sized like the recurring face.

    The recurring face (`_dominant_face_cluster`) sets the reference size only.
    Membership is by size, not overlap: a speaker who leans out of the cluster's
    area is still the speaker, and the window has to hold that frame too."""
    reference = _dominant_face_cluster([box for box in boxes if box.width > 0])
    if not reference:
        return []
    median = statistics.median(box.width for box in reference)
    return [
        box for box in boxes if _MIN_WIDTH_RATIO * median <= box.width <= _MAX_WIDTH_RATIO * median
    ]


def decide_speaker_framing(
    clip_path: str | None,
    *,
    keep_segments: Sequence[tuple[float, float]],
    display_width: float,
    display_height: float,
    canvas: _Canvas,
    landscape_fit: str,
    asked_by: Sequence[str] = (),
    sample: FaceSampler | None = None,
) -> SpeakerFraming:
    """Frame a speaker clip whose creator asked for a vertical, face-in-frame video.

    Never raises: a sampler failure is a ``face_unconfirmed`` fallback. Anything
    short of a confirmed face window keeps today's framing for ``landscape_fit``.
    ``sample`` defaults to `render_geometry.sample_face_regions`."""
    asked = tuple(asked_by)
    fallback = today_mode(landscape_fit)
    if canvas.height <= canvas.width or display_width <= display_height:
        # Portrait or square footage on the story canvas: nothing to letterbox
        # and nothing a sideways shift could improve; the engine's cover fill.
        return SpeakerFraming("centre_fill", REASON_NOT_LANDSCAPE, asked_by=asked)
    anchors = sample_anchors(keep_segments)
    faces: dict[str, Any] = {"face_sampling": "skipped", "anchors": len(anchors)}
    if clip_path is None or not anchors:
        return SpeakerFraming(fallback, REASON_FACE_UNCONFIRMED, faces=faces, asked_by=asked)
    try:
        regions, sampled = (sample or sample_face_regions)(
            clip_path,
            anchors,
            max_samples=len(anchors),
            timeout_s=_TIMEOUT_BASE_S + _TIMEOUT_PER_ANCHOR_S * len(anchors),
            count_decoded=True,
            raw_boxes=True,
        )
    except Exception as exc:  # noqa: BLE001 - a framing nicety never fails a render
        faces.update(face_sampling="failed", error=str(exc)[:200])
        return SpeakerFraming(fallback, REASON_FACE_UNCONFIRMED, faces=faces, asked_by=asked)
    decoded = int(sampled.get("decoded") or 0)
    faces.update(face_sampling="ok", decoded=decoded, detected=len(regions))
    if sampled.get("worker_error"):
        faces["worker_error"] = str(sampled["worker_error"])[:120]
    if sampled.get("timed_out"):
        faces["timed_out"] = True
    confirmed = (
        not sampled.get("worker_error")
        and not sampled.get("timed_out")
        and decoded >= max(_MIN_FACE_SAMPLES, math.ceil(_MIN_DECODED_SHARE * len(anchors)))
    )
    if not confirmed:
        return SpeakerFraming(fallback, REASON_FACE_UNCONFIRMED, faces=faces, asked_by=asked)
    kept = speaker_face_boxes([region.box for region in regions])
    faces.update(used=len(kept), ignored=len(regions) - len(kept))
    if len(kept) < max(_MIN_FACE_SAMPLES, math.ceil(_MIN_PRESENCE * decoded)):
        return SpeakerFraming(fallback, REASON_NO_FACE, faces=faces, asked_by=asked)
    union = _union_box(kept)
    faces["union"] = union.as_dict()
    window = face_fill_window(
        kept, display_width=display_width, display_height=display_height, canvas=canvas
    )
    if window is None or not window.fits:
        return SpeakerFraming(
            fallback, REASON_FACE_MOVES, window=window, faces=faces, asked_by=asked
        )
    _shown_w, shown_h = _shown_size(display_width, display_height, canvas)
    face_bottom = 0.5 + (union.bottom - 0.5) * shown_h / canvas.height
    faces["bottom_on_canvas"] = round(face_bottom, 4)
    if face_bottom > FACE_BOTTOM_LIMIT_FRAC:
        return SpeakerFraming(
            fallback, REASON_FACE_UNDER_CAPTIONS, window=window, faces=faces, asked_by=asked
        )
    return SpeakerFraming(
        "face_fill",
        REASON_FACE_IN_WINDOW,
        window=window,
        eligible=True,
        faces=faces,
        asked_by=asked,
    )


def face_core_box(raw: NormalizedBox) -> NormalizedBox:
    """The eyes-nose-mouth core of a RAW Haar face box, in the box's own frame.

    `FACE_CORE_X` / `FACE_CORE_Y` of the detector box: see their comment for the
    measurement. The background-merge clamp runs first (a 70%-wide "face" is a
    detector merge, and its core would blank the whole frame)."""
    box = clamp_face_width(raw)
    width, height = box.width, box.height
    return NormalizedBox(
        box.left + FACE_CORE_X[0] * width,
        box.top + FACE_CORE_Y[0] * height,
        box.left + FACE_CORE_X[1] * width,
        box.top + FACE_CORE_Y[1] * height,
    )


def face_core_mapper(
    *,
    display_width: float,
    display_height: float,
    canvas: _Canvas,
    position_x: float,
) -> Callable[[NormalizedBox], NormalizedBox | None]:
    """A RAW source face box -> the protected face CORE on the face-filled canvas.

    Cards only (KRI-547 follow-up): on a face-filled close-up the speaker's head
    fills most of the frame, so the padded protection box every other placement
    uses leaves no corner for a PiP card. A card may cover hair, ears and
    background, never the eyes, nose or mouth: the core (`face_core_box`) is
    mapped through the crop and grown by `FACE_CORE_MARGIN_FRAC` of the canvas;
    `resolve_phone_card_geometry` then allows NO overlap with it. Titles,
    closing text and captions keep the full protection. ``None`` when the core
    is outside the window."""

    def mapped(raw: NormalizedBox) -> NormalizedBox | None:
        box = source_box_to_canvas(
            face_core_box(raw),
            display_width=display_width,
            display_height=display_height,
            canvas=canvas,
            position_x=position_x,
        )
        if box is None:
            return None
        return NormalizedBox(
            max(0.0, box.left - FACE_CORE_MARGIN_FRAC),
            max(0.0, box.top - FACE_CORE_MARGIN_FRAC),
            min(1.0, box.right + FACE_CORE_MARGIN_FRAC),
            min(1.0, box.bottom + FACE_CORE_MARGIN_FRAC),
        )

    return mapped


def editor_speaker_framing(
    receipt: object, *, landscape_fit: str
) -> tuple[float | None, dict[str, Any] | None]:
    """``(speaker_position_x, updated receipt)`` for a phone editor Save.

    Without a ``speaker_framing`` receipt (no framing ask) this is ``(None,
    None)``: the Save compiles exactly as before. With one:

    * bars ("fit") -> letterbox, no shift; a face crop becomes ``letterbox`` /
      ``creator_chose_bars`` and keeps its window for a later switch back;
    * crop ("fill") -> the receipt's window when it passed every check
      (``eligible``), else the engine's centre crop (``creator_chose_crop`` when
      that replaces a letterbox fallback).

    A portrait/square speaker (``not_landscape``) is never touched."""
    if not isinstance(receipt, Mapping) or receipt.get("mode") not in FRAMING_MODES:
        return None, None
    updated = dict(receipt)
    if receipt.get("reason") == REASON_NOT_LANDSCAPE:
        return None, updated
    if landscape_fit == "fit":
        if receipt.get("mode") != "letterbox":
            updated.update(mode="letterbox", reason=REASON_CREATOR_CHOSE_BARS)
        return None, updated
    window = receipt.get("window")
    position = window.get("position_x") if isinstance(window, Mapping) else None
    if (
        receipt.get("eligible") is True
        and isinstance(position, int | float)
        and not isinstance(position, bool)
        and math.isfinite(float(position))
    ):
        updated.update(mode="face_fill", reason=REASON_FACE_IN_WINDOW)
        return float(position), updated
    if receipt.get("mode") == "letterbox":
        updated.update(mode="centre_fill", reason=REASON_CREATOR_CHOSE_CROP)
    return None, updated


__all__ = [
    "FACE_BOTTOM_LIMIT_FRAC",
    "FRAMING_MODES",
    "MIN_FACE_VISIBLE_FRACTION",
    "REASON_CREATOR_CHOSE_BARS",
    "REASON_CREATOR_CHOSE_CROP",
    "REASON_FACE_IN_WINDOW",
    "REASON_FACE_MOVES",
    "REASON_FACE_UNCONFIRMED",
    "REASON_FACE_UNDER_CAPTIONS",
    "REASON_NOT_LANDSCAPE",
    "REASON_NO_FACE",
    "SPEAKER_FRAMING_FIELD",
    "FaceWindow",
    "SpeakerFraming",
    "decide_speaker_framing",
    "editor_speaker_framing",
    "FACE_CORE_MARGIN_FRAC",
    "FACE_CORE_X",
    "FACE_CORE_Y",
    "face_core_box",
    "face_core_mapper",
    "face_fill_window",
    "sample_anchors",
    "speaker_face_boxes",
    "today_mode",
]
