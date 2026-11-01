"""The opening (hook) title on a phone Talking (subtitled) edit (KRI-467).

A confirmed ``opening_title`` used to be refused for every Talking edit
("Talking edits show your words as captions, so I can't add a title on top
yet"). The phone Talking compiler now carries a text lane
(`phone_subtitled_plan.compile_phone_subtitled_plan(text_elements=...)`), and
the title is its first element: one ordinary, editable ``TextElement`` row on
the variant (``text_elements``). The worker builds it once, the recipe
compiles it, the status route serves it, and the iOS editor edits and saves it
like any other text row, so the server, the phone render and the editor
preview all read the same row.

Look and fit: the same as the narrated (voiceover) title
(`narrated_title.narrated_title_placement`, explicit fields): Playfair Display
at 120 px centred on y 0.15, a long title shrunk into the top band.

Timing: from 0 for the creator's ``opening_title_duration_s`` ("in the first
2 seconds" -> 2.0), else like the narrated intro, until a second after the
first spoken word (0.5-3 s). Both are OUTPUT seconds: the title is "the first
N seconds of the video", so it is anchored on the cut timeline the recipe
plays and a speech-cleanup cut never moves or shortens it (the first word is
read off the cut-timeline cues).

Placement: a talk-to-camera frame puts the speaker's face in the upper half,
right where the preset title sits, and the captions own the bottom band. So
`place_talking_title` samples the speaker clip's analysis proxy over the
title's window (mapped through the cut and the engine's cover-fit/letterbox),
and moves the title down below the face (still above the captions) when the
preset spot covers it. "Never assume there's no face" (KRI-183): when the
sampler cannot confirm the frame, a conservative centre-top talk-to-camera
face box is protected instead. The chosen ``y_frac`` is stored on the row, so
every renderer draws the same spot and the editor shows where it went.

Closing text (KRI-514): a confirmed ``closing_title`` ("end on the toast photo
with a 'MY PICK' badge") is a second row on the same lane, drawn as a tag --
dark TikTok Sans on the editor's caption lime -- from the moment the closing
photo appears (else the last 3 s) to the end. With a closing photo it sits on
the photo's lower edge like a sticker badge (`place_closing_on_photo`; the
photo is already off the face); without one it takes the title's top spot and
face check (`place_talking_title`).

Entrance (KRI-1097 wave 2): every title row the writer draws (the title, the text on a spoken
word, the closing tag) fades in unless the creator asked for another entrance;
`talking_title_entrances` applies the planned ``title_animation`` and the animation asks.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

import structlog

from app.pipeline.narrated_title import TITLE_MAX_CHARS, TITLE_Y_FRAC, narrated_title_placement
from app.pipeline.object_track import ObjectTrack, object_canvas_boxes, object_collision
from app.pipeline.phone_subtitled_lanes import CAPTION_BAND_TOP_FRAC
from app.pipeline.render_geometry import (
    NormalizedBox,
    ProtectedRegion,
    _dominant_face_cluster,
    _translate_centered_box_to_y,
    _union_box,
    sample_face_regions,
)

log = structlog.get_logger(__name__)

TALKING_TITLE_ELEMENT_ID = "opening-title"
TALKING_TITLE_SOURCE = "opening_title"
TALKING_CLOSING_ELEMENT_ID = "closing-title"
TALKING_CLOSING_SOURCE = "closing_title"

# KRI-514: the closing tag's look, and how long it holds with no closing photo
# (the photo's own default lookback,
# `phone_reaction_grounding._CLOSING_DEFAULT_LOOKBACK_S`).
_CLOSING_DEFAULT_S = 3.0
_CLOSING_FONT_FAMILY = "TikTok Sans Bold"
_CLOSING_SIZE_PX = 64
_CLOSING_TEXT_COLOR = "#111111"
_CLOSING_BACKGROUND = "#C5F82A"
# The tag spans at most this share of the photo's width and sits this far
# (canvas height) above the photo's bottom edge.
_CLOSING_PHOTO_WIDTH_SHARE = 0.9
_CLOSING_PHOTO_INSET_FRAC = 0.015
# The background pads the measured text block by 4 px above and below
# (`portable_text_layout`'s `TextBackground`).
_CLOSING_PILL_PAD_PX = 4

# Mirrors the narrated title (`phone_narrated_plan`): the cloud intro window.
_TITLE_MIN_S = 0.5
_TITLE_MAX_S = 3.0
_TITLE_AFTER_FIRST_WORD_S = 1.0

# The title block's top never goes above the platform header chrome (same 6%
# as `narrated_title`), and its bottom never reaches the caption band
# (`CAPTION_BAND_TOP_FRAC`, the line overlay cards are clamped to as well).
_TOP_MARGIN_FRAC = 0.06
# Where to try the title when the preset spot covers the face: below a
# talk-to-camera face first (the face sits in the upper half), then higher.
_FALLBACK_Y_FRACS = (0.5, 0.44, 0.38)
# KRI-706: a close selfie makes the face band cover most of the upper frame, so
# every row above fails. The last resort is a row just under the face: its top
# sits this far (canvas height) below the face band, and its bottom may go no
# lower than this (the captions move down off a face that fills the frame, the
# way the caption lane already does, so the title may use the space above them).
_BELOW_FACE_GAP_FRAC = 0.01
_BELOW_FACE_BOTTOM_MAX_FRAC = 0.86
# Same tolerance as caption and guided-story placement
# (`render_geometry._FACE_OVERLAP_MAX_COVERAGE`): at most 5% of the title box.
_MAX_FACE_COVERAGE = 0.05
# Below this many decoded frames the sampler could not really look.
_MIN_DECODED_ANCHORS = 3
_FACE_ANCHORS = 6
_FACE_TIMEOUT_BASE_S = 2.5
_FACE_TIMEOUT_PER_ANCHOR_S = 0.35
# KRI-183's conservative centre-top talk-to-camera face
# (`phone_overlay_grounding._FALLBACK_FACE_BOX`).
_FALLBACK_FACE_BOX = NormalizedBox(0.28, 0.02, 0.72, 0.52)
_Y_FRAC_DECIMALS = 4

FaceSampler = Callable[..., tuple[list[ProtectedRegion], dict[str, Any]]]


class _Canvas(Protocol):
    @property
    def width(self) -> int: ...

    @property
    def height(self) -> int: ...


def talking_title_end_s(first_word_end_s: float | None) -> float:
    """The default fade-out: a second after the first spoken word, held between
    0.5 s and 3 s (the narrated intro's rule); the full 3 s with no word."""
    if first_word_end_s is None:
        return _TITLE_MAX_S
    return max(_TITLE_MIN_S, min(_TITLE_MAX_S, float(first_word_end_s) + _TITLE_AFTER_FIRST_WORD_S))


def first_cue_word_end_s(cues: Sequence[dict]) -> float | None:
    """End of the first spoken word in caption cues (cut-timeline seconds)."""
    for cue in cues:
        words = cue.get("words") if isinstance(cue, dict) else None
        if isinstance(words, list):
            for word in words:
                if isinstance(word, dict) and word.get("end_s") is not None:
                    return float(word["end_s"])
        if isinstance(cue, dict) and cue.get("end_s") is not None:
            return float(cue["end_s"])
    return None


def talking_title_element(
    opening_title: str | None,
    *,
    duration_s: float | None,
    first_word_end_s: float | None,
    timeline_duration_s: float,
    canvas: _Canvas,
) -> dict | None:
    """The title as one editable TextElement row, or ``None`` without a title.

    ``duration_s`` is the creator's confirmed hold (``opening_title_duration_s``);
    without it the title fades a second after ``first_word_end_s``. Both are
    clamped to the speaker's own timeline.
    """
    from app.agents._schemas.text_element import TextElement  # noqa: PLC0415

    text = " ".join((opening_title or "").split())[:TITLE_MAX_CHARS]
    if not text:
        return None
    end_s = float(duration_s) if duration_s is not None else talking_title_end_s(first_word_end_s)
    end_s = round(min(end_s, float(timeline_duration_s)), 3)
    if end_s < _TITLE_MIN_S / 5:
        return None
    element = TextElement(
        id=TALKING_TITLE_ELEMENT_ID,
        text=text,
        start_s=0.0,
        end_s=end_s,
        role="generative_intro",
        **narrated_title_placement(text, canvas=canvas, explicit=True),
        effect="fade-in",
        source_params={"source": TALKING_TITLE_SOURCE},
    )
    return element.model_dump(mode="json", exclude_none=True)


def _row_source(row: Mapping[str, Any]) -> str | None:
    params = row.get("source_params")
    source = params.get("source") if isinstance(params, Mapping) else None
    return source if isinstance(source, str) else None


def _first_entrance(*asks: Any) -> str | None:
    """The first named edge among ``asks``, else the plain default when any is an ask."""
    present = [ask for ask in asks if ask is not None]
    named = next((ask.edge for ask in present if ask.named), None)
    return named or (present[0].resolved_edge() if present else None)


def talking_title_entrances(
    rows: Sequence[Mapping[str, Any]],
    *,
    title_animation: object,
    requirements: Iterable[Any] = (),
) -> Sequence[Mapping[str, Any]]:
    """The writer's title rows entering the way the creator asked (KRI-1097 wave 2).

    The opening title takes a title-scope animation ask, else the plan's ``title_animation``,
    else a whole-video ask ("animate the big text in": `animation_ask(req, styles=True)`). The
    text on a spoken word and the closing tag, drawn in the title's look, take a whole-video
    ask, and the title's ask and the plan's too when the edit has no opening title (that text
    is then the title it shows). A named entrance wins over an unnamed one, which plays the
    plain fade. Captions and any other row keep their own look.

    ``rows`` comes back as it was with ``TALKING_HEAD_ROUTE_FOLLOWS_PROMPT_ENABLED`` off, with
    nothing asked, or on a phone that plays no text entrance (`text_entrance_form`); a phone
    that only plays the legacy effects gets the closest one (`entrance_patch`).
    """
    from app.config import settings  # noqa: PLC0415
    from app.kria.animation_ask import (  # noqa: PLC0415
        AnimationAsk,
        animation_targets,
        entrance_patch,
        text_entrance_form,
    )
    from app.pipeline.phone_spoken_text import SPOKEN_TEXT_SOURCE  # noqa: PLC0415
    from app.schemas.text_style_intent import normalize_title_animation  # noqa: PLC0415

    if not settings.talking_head_route_follows_prompt_enabled:
        return rows
    form = text_entrance_form()
    if form is None:
        return rows
    asks = dict(animation_targets(requirements, styles=True))
    planned = normalize_title_animation(title_animation)
    plan_ask = AnimationAsk(planned) if planned is not None else None
    title_ask, whole_ask = asks.get("title"), asks.get("all")
    has_title = any(_row_source(row) == TALKING_TITLE_SOURCE for row in rows)
    other = _first_entrance(whole_ask, *(() if has_title else (title_ask, plan_ask)))
    edges = {
        TALKING_TITLE_SOURCE: _first_entrance(title_ask, plan_ask, whole_ask),
        SPOKEN_TEXT_SOURCE: other,
        TALKING_CLOSING_SOURCE: other,
    }
    if not any(edges.values()):
        return rows
    return [
        {**row, **entrance_patch(edge, form)} if (edge := edges.get(_row_source(row))) else row
        for row in rows
    ]


def _output_to_source_s(at_s: float, keep_segments: Sequence[tuple[float, float]]) -> float | None:
    """Source-clip time of cut-timeline ``at_s`` (``None`` past the last kept span)."""
    cursor = 0.0
    for start, end in keep_segments:
        length = float(end) - float(start)
        if length <= 0:
            continue
        if at_s < cursor + length:
            return float(start) + (at_s - cursor)
        cursor += length
    return None


def source_box_to_canvas(
    box: NormalizedBox,
    *,
    display_width: float,
    display_height: float,
    canvas: _Canvas,
    scale: float = 1.0,
    position_x: float = 0.0,
) -> NormalizedBox | None:
    """Map a box normalized to the upright source frame onto the canvas the way
    the phone engine draws the speaker: cover-fill centred, then
    ``transform.scale`` about the canvas centre (``scale < 1`` letterboxes,
    `phone_recipe_shared.fit_transform`), then ``transform.position_x`` canvas
    pixels to the right (KRI-547's face-filled crop shifts the speaker
    sideways, `phone_speaker_framing`). ``None`` when nothing of it shows."""
    if display_width <= 0 or display_height <= 0:
        return None
    cover = max(canvas.width / display_width, canvas.height / display_height)
    shown_w = display_width * cover / canvas.width
    shown_h = display_height * cover / canvas.height
    shift = position_x / canvas.width

    def x(value: float) -> float:
        return 0.5 + ((value - 0.5) * shown_w) * scale + shift

    def y(value: float) -> float:
        return 0.5 + ((value - 0.5) * shown_h) * scale

    mapped = NormalizedBox(
        max(0.0, x(box.left)),
        max(0.0, y(box.top)),
        min(1.0, x(box.right)),
        min(1.0, y(box.bottom)),
    )
    if mapped.right <= mapped.left or mapped.bottom <= mapped.top:
        return None
    return mapped


def _measure_title_box(row: dict, *, canvas: _Canvas) -> NormalizedBox:
    from app.agents._schemas.text_element import TextElement  # noqa: PLC0415
    from app.pipeline.generative_overlays import build_overlays_from_text_elements  # noqa: PLC0415
    from app.pipeline.text_overlay_skia import measure_text_overlay_box  # noqa: PLC0415

    [overlay] = build_overlays_from_text_elements(
        [TextElement.model_validate(row)],
        video_duration_s=max(float(row["end_s"]), 0.1),
        independent_box_alignment=True,
    )
    measured = measure_text_overlay_box(overlay, render_canvas=canvas)
    return NormalizedBox(measured["left"], measured["top"], measured["right"], measured["bottom"])


def choose_title_y_frac(
    probe_box: NormalizedBox,
    default_y: float,
    faces: Sequence[NormalizedBox],
    *,
    caption_top_frac: float = CAPTION_BAND_TOP_FRAC,
    avoid: Sequence[NormalizedBox] = (),
) -> tuple[float, dict[str, Any]]:
    """The first spot (preset, then lower) where the title covers no face and
    stays between the header margin and the caption band; else the spot that
    covers the least face. ``avoid`` (KRI-707: the captions and text on screen
    at the same time) is kept clear like a face, but never sets the "just under
    the face" row."""
    blocked = [*faces, *avoid]
    candidates = [default_y]
    for value in _FALLBACK_Y_FRACS:
        if all(abs(value - existing) > 0.01 for existing in candidates):
            candidates.append(value)
    evaluated: list[dict[str, Any]] = []
    for index, y_frac in enumerate(candidates):
        box = _translate_centered_box_to_y(probe_box, y_frac)
        # The preset spot is already fitted into the top band
        # (`narrated_title_placement`); only the lower spots need the check.
        in_band = index == 0 or (
            box.top >= _TOP_MARGIN_FRAC - 1e-6 and box.bottom <= caption_top_frac + 1e-6
        )
        coverage = round(max((box.coverage_by(face) for face in blocked), default=0.0), 5)
        evaluated.append({"y_frac": y_frac, "coverage": coverage, "in_band": in_band})
        if in_band and coverage <= _MAX_FACE_COVERAGE:
            return y_frac, {
                "status": "preset" if index == 0 else "moved",
                "chosen_y_frac": y_frac,
                "coverage": coverage,
                "evaluated": evaluated,
            }
    # KRI-706: no row in the top band clears the face. Try the spot just under it.
    below = _below_face_y_frac(probe_box, faces)
    if below is not None:
        box = _translate_centered_box_to_y(probe_box, below)
        coverage = round(max((box.coverage_by(face) for face in blocked), default=0.0), 5)
        evaluated.append(
            {"y_frac": below, "coverage": coverage, "in_band": False, "below_face": True}
        )
        if coverage <= _MAX_FACE_COVERAGE:
            return below, {
                "status": "moved_below_face",
                "chosen_y_frac": below,
                "coverage": coverage,
                "evaluated": evaluated,
            }
        evaluated[-1]["below_face"] = False  # tried, still covers the face
    best = min(
        range(len(candidates)),
        key=lambda i: (0 if evaluated[i]["in_band"] else 1, evaluated[i]["coverage"], i),
    )
    best_coverage = evaluated[best]["coverage"]
    return candidates[best], {
        "status": "best_effort",
        "chosen_y_frac": candidates[best],
        "coverage": best_coverage,
        # KRI-706: the title still sits on the face; the brief receipt reads this
        # so it never claims "text off the face" for it.
        "face_collision": best_coverage > _MAX_FACE_COVERAGE,
        "evaluated": evaluated,
    }


def _below_face_y_frac(probe_box: NormalizedBox, faces: Sequence[NormalizedBox]) -> float | None:
    """The title's centre y that puts its top just under the lowest face, or None
    when there is no face or the title would then run past the lower limit."""
    if not faces:
        return None
    height = probe_box.bottom - probe_box.top
    top = max(face.bottom for face in faces) + _BELOW_FACE_GAP_FRAC
    if top + height > _BELOW_FACE_BOTTOM_MAX_FRAC:
        return None
    return round(top + height / 2, _Y_FRAC_DECIMALS)


def _face_boxes(
    clip_path: str | None,
    *,
    window: tuple[float, float],
    keep_segments: Sequence[tuple[float, float]],
    display_width: float,
    display_height: float,
    canvas: _Canvas,
    scale: float,
    sample: FaceSampler,
    position_x: float = 0.0,
    all_faces: bool = False,
) -> tuple[list[NormalizedBox], dict[str, Any]]:
    """The speaker's face over ``window`` (cut-timeline seconds), on the canvas.

    ``all_faces`` (KRI-827, only when the creator named a face to keep clear) protects
    every detected face, not only the dominant cluster: "her face" in a two-person take
    may not be the biggest one.

    Returns the protected boxes and a receipt. Fail-safe: when the sampler
    did not confirm the frame (no clip, error, timeout, too few decoded
    frames), the conservative fallback face is protected instead."""
    start_s, end_s = window
    span = max(end_s - start_s, 0.0)
    anchors_out = [start_s + span * (index + 0.5) / _FACE_ANCHORS for index in range(_FACE_ANCHORS)]
    anchors = [
        round(source_at, 3)
        for at_s in anchors_out
        if (source_at := _output_to_source_s(at_s, keep_segments)) is not None
    ]
    receipt: dict[str, Any] = {"face_sampling": "skipped", "anchors": len(anchors)}
    if clip_path is None or not anchors:
        receipt["faces"] = "fallback"
        return [_FALLBACK_FACE_BOX], receipt
    try:
        regions, sampled = sample(
            clip_path,
            anchors,
            max_samples=len(anchors),
            timeout_s=_FACE_TIMEOUT_BASE_S + _FACE_TIMEOUT_PER_ANCHOR_S * len(anchors),
            count_decoded=True,
        )
    except Exception as exc:  # noqa: BLE001 - fail safe: protect the fallback face
        receipt.update(face_sampling="failed", error=str(exc)[:200], faces="fallback")
        return [_FALLBACK_FACE_BOX], receipt
    decoded = int(sampled.get("decoded") or 0)
    receipt.update(face_sampling="ok", decoded=decoded, detected=len(regions))
    confirmed = (
        not sampled.get("worker_error")
        and not (sampled.get("timed_out") and not sampled.get("partial"))
        and decoded >= _MIN_DECODED_ANCHORS
    )
    mapped = [
        box
        for region in regions
        if (
            box := source_box_to_canvas(
                region.box,
                display_width=display_width,
                display_height=display_height,
                canvas=canvas,
                scale=scale,
                position_x=position_x,
            )
        )
        is not None
    ]
    if mapped and all_faces:
        receipt["faces"] = "detected"
        receipt["face_boxes"] = len(mapped)
        return mapped, receipt
    if mapped:
        band = _union_box(_dominant_face_cluster(mapped))
        receipt["faces"] = "detected"
        receipt["face_band"] = band.as_dict()
        return [band], receipt
    if not confirmed:
        receipt["faces"] = "fallback"
        return [_FALLBACK_FACE_BOX], receipt
    receipt["faces"] = "none"
    return [], receipt


def place_talking_title(
    row: dict,
    *,
    clip_path: str | None,
    keep_segments: Sequence[tuple[float, float]],
    display_width: float,
    display_height: float,
    canvas: _Canvas,
    scale: float = 1.0,
    sample: FaceSampler | None = None,
    position_x: float = 0.0,
    avoid: Sequence[NormalizedBox] = (),
    objects: Sequence[ObjectTrack] = (),
    protect_all_faces: bool = False,
) -> tuple[dict, dict[str, Any]]:
    """``row`` moved off the speaker's face (and clear of the captions), plus
    a receipt for the job debug view. Never raises: anything that goes wrong
    while measuring leaves the row where it was. ``sample`` defaults to
    `render_geometry.sample_face_regions` (looked up per call). ``scale`` and
    ``position_x`` are the speaker clip's main-track transform (letterbox scale,
    KRI-547 face-filled crop shift), so the face is mapped where it is drawn.

    ``avoid`` (KRI-707) is other text on screen during the row (caption lines,
    the title): kept clear too when a spot clears both, else the face wins and
    the receipt's ``text_overlap`` says the row touches that text.

    ``objects`` (KRI-827) are the tracks of what the creator marked to keep text off ("no
    text on the plate"). With any, the face AND every object are hard limits for the
    row's whole window (`place_protected_title`): the row moves, then shrinks, and only
    when nothing clears them is it left at the least-covering spot with
    ``object_collision`` set -- a face never wins over the object. Without any, the
    placement is exactly what it was. ``protect_all_faces`` keeps every detected face
    clear, not only the dominant one (the creator named "her face")."""
    default_y = float(row.get("y_frac") or 0.15)
    if objects or protect_all_faces:
        return place_protected_title(
            row,
            clip_path=clip_path,
            keep_segments=keep_segments,
            display_width=display_width,
            display_height=display_height,
            canvas=canvas,
            scale=scale,
            sample=sample,
            position_x=position_x,
            avoid=avoid,
            objects=objects,
            protect_all_faces=protect_all_faces,
        )
    try:
        faces, receipt = _face_boxes(
            clip_path,
            window=(float(row["start_s"]), float(row["end_s"])),
            keep_segments=keep_segments,
            display_width=display_width,
            display_height=display_height,
            canvas=canvas,
            scale=scale,
            sample=sample or sample_face_regions,
            position_x=position_x,
        )
        probe = _translate_centered_box_to_y(_measure_title_box(row, canvas=canvas), default_y)
        chosen, decision = choose_title_y_frac(probe, default_y, faces, avoid=avoid)
        if avoid:
            if decision["status"] == "best_effort":
                # No spot clears the face and the text: text never covers a face.
                chosen, decision = choose_title_y_frac(probe, default_y, faces)
            box = _translate_centered_box_to_y(probe, chosen)
            decision["text_overlap"] = any(
                box.coverage_by(other) > _MAX_FACE_COVERAGE for other in avoid
            )
    except Exception as exc:  # noqa: BLE001 - a placement nicety never blocks a render
        log.warning("phone_subtitled_title.placement_failed", error=str(exc)[:200])
        return dict(row), {"status": "error", "error": str(exc)[:200], "default_y_frac": default_y}
    placed = dict(row)
    placed["y_frac"] = round(chosen, _Y_FRAC_DECIMALS)
    return placed, {**receipt, **decision, "default_y_frac": default_y}


# KRI-827: with a protected object the title may use any row in the band (every 0.02 of
# the canvas), and then shrinks in these steps before admitting a collision.
_PROTECTED_SCAN_Y = tuple(round(0.08 + 0.02 * index, 2) for index in range(40))
_PROTECTED_SHRINK = (1.0, 0.85, 0.72, 0.6)


def _object_boxes_for(
    objects: Sequence[ObjectTrack],
    *,
    window: tuple[float, float],
    keep_segments: Sequence[tuple[float, float]],
    display_width: float,
    display_height: float,
    canvas: _Canvas,
    scale: float,
    position_x: float,
) -> tuple[list[NormalizedBox], list[dict[str, Any]]]:
    boxes: list[NormalizedBox] = []
    receipts: list[dict[str, Any]] = []
    for track in objects:
        found, receipt = object_canvas_boxes(
            track,
            window=window,
            keep_segments=keep_segments,
            display_width=display_width,
            display_height=display_height,
            canvas=canvas,
            scale=scale,
            position_x=position_x,
        )
        boxes.extend(found)
        receipts.append({**receipt, "track_status": track.status})
    return boxes, receipts


def choose_protected_y_frac(
    probe_box: NormalizedBox,
    default_y: float,
    faces: Sequence[NormalizedBox],
    objects: Sequence[NormalizedBox],
    *,
    caption_top_frac: float = CAPTION_BAND_TOP_FRAC,
    avoid: Sequence[NormalizedBox] = (),
    relax_text: bool = True,
) -> tuple[float, dict[str, Any]] | None:
    """The first spot clear of every face and object (and, when one exists, of ``avoid``
    text), or ``None`` when none is. Faces and objects are never traded for each other.
    ``relax_text=False`` refuses any spot touching ``avoid`` text too."""
    candidates: list[tuple[float, float]] = []
    for value in (default_y, *_FALLBACK_Y_FRACS, *_PROTECTED_SCAN_Y):
        if all(abs(value - existing) > 0.005 for existing, _ in candidates):
            candidates.append((value, caption_top_frac))
    below = _below_face_y_frac(probe_box, faces)
    if below is not None:
        candidates.append((below, _BELOW_FACE_BOTTOM_MAX_FRAC))
    for keep_text_clear in (True, False) if avoid and relax_text else (True,):
        for y_frac, bottom_limit in candidates:
            box = _translate_centered_box_to_y(probe_box, y_frac)
            if box.top < _TOP_MARGIN_FRAC - 1e-6 or box.bottom > bottom_limit + 1e-6:
                continue
            face = max((box.coverage_by(f) for f in faces), default=0.0)
            obj = max((object_collision(box, o) for o in objects), default=0.0)
            text = max((box.coverage_by(t) for t in avoid), default=0.0)
            if face > _MAX_FACE_COVERAGE or obj > _MAX_FACE_COVERAGE:
                continue
            if keep_text_clear and text > _MAX_FACE_COVERAGE:
                continue
            return y_frac, {
                "status": "preset" if abs(y_frac - default_y) < 1e-6 else "moved",
                "chosen_y_frac": y_frac,
                "coverage": round(face, 5),
                "object_coverage": round(obj, 5),
                "text_overlap": text > _MAX_FACE_COVERAGE,
            }
    return None


def place_protected_title(
    row: dict,
    *,
    clip_path: str | None,
    keep_segments: Sequence[tuple[float, float]],
    display_width: float,
    display_height: float,
    canvas: _Canvas,
    objects: Sequence[ObjectTrack],
    scale: float = 1.0,
    sample: FaceSampler | None = None,
    position_x: float = 0.0,
    avoid: Sequence[NormalizedBox] = (),
    protect_all_faces: bool = False,
) -> tuple[dict, dict[str, Any]]:
    """KRI-827: ``row`` kept off every face and every marked object for its whole window.

    Ladder: the preset spot, the fallback rows, any row in the band, the row just under
    the face -- then the same at smaller sizes (`_PROTECTED_SHRINK`). Only when nothing
    clears them does the row stay at the spot covering the least object/face, and the
    receipt says ``object_collision: true``. ``objects_verified`` is False whenever any
    object sample was inferred or unknown (lost by the tracker, a timeout): the placement
    then kept clear of where the object probably was, never claiming it saw it."""
    default_y = float(row.get("y_frac") or 0.15)
    window = (float(row["start_s"]), float(row["end_s"]))
    try:
        faces, receipt = _face_boxes(
            clip_path,
            window=window,
            keep_segments=keep_segments,
            display_width=display_width,
            display_height=display_height,
            canvas=canvas,
            scale=scale,
            sample=sample or sample_face_regions,
            position_x=position_x,
            all_faces=protect_all_faces,
        )
        object_boxes, object_receipts = _object_boxes_for(
            objects,
            window=window,
            keep_segments=keep_segments,
            display_width=display_width,
            display_height=display_height,
            canvas=canvas,
            scale=scale,
            position_x=position_x,
        )
        verified = bool(object_receipts) and all(
            r["status"] == "seen" and r["track_status"] == "tracked" for r in object_receipts
        )
        base_size = row.get("size_px")
        best: tuple[float, float, float, dict] | None = None
        # Clear of the other text at every size first (a moved caption block included), and
        # only then a spot that touches it (``text_overlap`` in the receipt).
        ladder = [
            (strict, factor)
            for strict in ((True, False) if avoid else (False,))
            for factor in (_PROTECTED_SHRINK if base_size else (1.0,))
        ]
        for strict_text, factor in ladder:
            trial = dict(row)
            if base_size and factor != 1.0:
                trial["size_px"] = max(1, round(float(base_size) * factor))
            probe = _translate_centered_box_to_y(
                _measure_title_box(trial, canvas=canvas), default_y
            )
            chosen = choose_protected_y_frac(
                probe, default_y, faces, object_boxes, avoid=avoid, relax_text=not strict_text
            )
            if chosen is not None:
                y_frac, decision = chosen
                placed = dict(trial)
                placed["y_frac"] = round(y_frac, _Y_FRAC_DECIMALS)
                if factor != 1.0:
                    decision = {**decision, "status": "shrunk", "size_factor": factor}
                return placed, {
                    **receipt,
                    **decision,
                    "face_collision": False,
                    "object_collision": False,
                    "objects": object_receipts,
                    "objects_verified": verified,
                    "default_y_frac": default_y,
                }
            for y_frac in (default_y, *_FALLBACK_Y_FRACS, *_PROTECTED_SCAN_Y):
                box = _translate_centered_box_to_y(probe, y_frac)
                if box.top < _TOP_MARGIN_FRAC - 1e-6 or box.bottom > CAPTION_BAND_TOP_FRAC + 1e-6:
                    continue
                obj = max((object_collision(box, o) for o in object_boxes), default=0.0)
                face = max((box.coverage_by(f) for f in faces), default=0.0)
                if best is None or (obj + face, factor) < (best[1] + best[2], best[0]):
                    text = max((box.coverage_by(t) for t in avoid), default=0.0)
                    best = (
                        factor,
                        obj,
                        face,
                        {"y_frac": y_frac, "size_px": trial.get("size_px"), "text": text},
                    )
    except Exception as exc:  # noqa: BLE001 - a placement nicety never blocks a render
        log.warning("phone_subtitled_title.protected_placement_failed", error=str(exc)[:200])
        return dict(row), {
            "status": "error",
            "error": str(exc)[:200],
            "default_y_frac": default_y,
            "object_collision": True,
            "objects_verified": False,
        }
    placed = dict(row)
    if best is not None:
        placed["y_frac"] = round(best[3]["y_frac"], _Y_FRAC_DECIMALS)
        if best[3]["size_px"] is not None:
            placed["size_px"] = best[3]["size_px"]
    return placed, {
        **receipt,
        "status": "best_effort",
        "chosen_y_frac": placed.get("y_frac"),
        "coverage": round(best[2], 5) if best else None,
        "object_coverage": round(best[1], 5) if best else None,
        "face_collision": bool(best and best[2] > _MAX_FACE_COVERAGE),
        "object_collision": bool(best is None or best[1] > _MAX_FACE_COVERAGE),
        "text_overlap": bool(best and best[3]["text"] > _MAX_FACE_COVERAGE),
        "objects": object_receipts,
        "objects_verified": verified,
        "default_y_frac": default_y,
    }


@dataclass(frozen=True)
class ClosingPhoto:
    """The closing photo card on the canvas: its centre, its width as a
    fraction of the canvas width, and its width/height ratio (``None`` when
    unknown)."""

    x_frac: float
    y_frac: float
    width_frac: float
    aspect: float | None = None

    @classmethod
    def from_card(cls, card: Any, *, aspect: float | None) -> ClosingPhoto:
        """From a `SubtitledOverlayCard`, clamped like the compiler draws it."""
        return cls(
            x_frac=float(card.x_frac),
            y_frac=min(float(card.y_frac), CAPTION_BAND_TOP_FRAC),
            width_frac=float(card.scale),
            aspect=aspect,
        )

    def box(self, canvas: _Canvas) -> NormalizedBox | None:
        if not self.aspect or self.aspect <= 0:
            return None
        height = self.width_frac * canvas.width / canvas.height / self.aspect
        return NormalizedBox(
            self.x_frac - self.width_frac / 2,
            self.y_frac - height / 2,
            self.x_frac + self.width_frac / 2,
            self.y_frac + height / 2,
        )


def talking_closing_window(
    timeline_duration_s: float, photo_start_s: float | None = None
) -> tuple[float, float]:
    """From the closing photo's entrance, else the last 3 s, to the end."""
    end_s = float(timeline_duration_s)
    start_s = end_s - _CLOSING_DEFAULT_S if photo_start_s is None else float(photo_start_s)
    return round(max(0.0, min(start_s, end_s)), 3), round(end_s, 3)


def talking_closing_element(
    closing_title: str | None,
    *,
    start_s: float,
    end_s: float,
    photo: ClosingPhoto | None = None,
) -> dict | None:
    """The closing text as one editable TextElement row, or ``None`` without
    text or time for it. Centred on ``photo`` and kept inside its width when
    there is one, else at the title's top spot."""
    from app.agents._schemas.text_element import TextElement  # noqa: PLC0415

    text = " ".join((closing_title or "").split())[:TITLE_MAX_CHARS]
    if not text or end_s - start_s < _TITLE_MIN_S / 5:
        return None
    element = TextElement(
        id=TALKING_CLOSING_ELEMENT_ID,
        text=text,
        start_s=start_s,
        end_s=end_s,
        role="generative_intro",
        position="custom",
        x_frac=round(photo.x_frac, _Y_FRAC_DECIMALS) if photo is not None else 0.5,
        y_frac=round(photo.y_frac, _Y_FRAC_DECIMALS) if photo is not None else TITLE_Y_FRAC,
        font_family=_CLOSING_FONT_FAMILY,
        size_px=_CLOSING_SIZE_PX,
        color=_CLOSING_TEXT_COLOR,
        background_color=_CLOSING_BACKGROUND,
        shadow_enabled=False,
        max_width_frac=(
            round(photo.width_frac * _CLOSING_PHOTO_WIDTH_SHARE, _Y_FRAC_DECIMALS)
            if photo is not None
            else None
        ),
        effect="fade-in",
        source_params={"source": TALKING_CLOSING_SOURCE},
    )
    return element.model_dump(mode="json", exclude_none=True)


def place_closing_on_photo(
    row: dict, photo: ClosingPhoto, *, canvas: _Canvas
) -> tuple[dict, dict[str, Any]]:
    """``row`` set on the lower edge of the closing photo, like a sticker badge
    on it, and never in the caption band; plus a receipt. The photo was placed
    off the speaker's face already (`resolve_phone_card_geometry`), so a tag
    inside it needs no face check of its own. Never raises: with the photo's
    shape unknown, or anything going wrong while measuring, the tag stays on
    the photo's centre."""
    box = photo.box(canvas)
    if box is None:
        return dict(row), {"status": "photo_centre", "chosen_y_frac": row["y_frac"]}
    try:
        measured = _measure_title_box(row, canvas=canvas)
    except Exception as exc:  # noqa: BLE001 - a placement nicety never blocks a render
        log.warning("phone_subtitled_title.closing_placement_failed", error=str(exc)[:200])
        return dict(row), {"status": "error", "error": str(exc)[:200]}
    half = (measured.bottom - measured.top) / 2 + _CLOSING_PILL_PAD_PX / canvas.height
    y_frac = min(box.bottom - _CLOSING_PHOTO_INSET_FRAC - half, CAPTION_BAND_TOP_FRAC - half)
    y_frac = round(max(y_frac, _TOP_MARGIN_FRAC + half), _Y_FRAC_DECIMALS)
    placed = dict(row)
    placed["y_frac"] = y_frac
    return placed, {
        "status": "on_closing_photo",
        "chosen_y_frac": y_frac,
        "photo_box": box.as_dict(),
    }
