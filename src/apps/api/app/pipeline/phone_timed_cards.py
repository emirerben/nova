"""Ground exact-timed small cards onto a phone edit's OUTPUT timeline.

"Show the menu photo as a small card at 3 seconds for 2 seconds" / "put the receipt in the
corner during the second clip" on a phone Montage (guided / unified montage, montage-family
voiceover) or narrated Voiceover edit. The creator's request is a
`app.agents._schemas.reaction_beats.TimedCard` (already repaired against the manifest by
`app.services.creator_capabilities.repair_creator_timed_cards`); this module turns each one
into a `SubtitledOverlayCard` the existing lane compiler
(`app.pipeline.phone_subtitled_plan._compile_overlay_track`) draws, and a creator-safe
receipt the brief check reads (`phone_timed_card_receipt`).

Everything here is pure: no settings, no storage, no model. The worker supplies the compiled
clip windows (`clip_windows_from_recipe`: where each clip REALLY starts on the output clock,
after transitions and refits), the bound Visuals and -- where it sampled them -- the faces.

Rules (each pinned in ``tests/pipeline/test_phone_timed_cards.py``):

- the anchor is ``at_s`` on the output timeline, or ``clip_id`` + ``offset_s`` after that
  clip's compiled start; a clip not in the edit is ``clip_not_in_edit``.
- a card starting at or inside the last ``END_GUARD_S`` of the video is ``past_end``; one that
  would outlast the video is shortened to its end.
- a card never covers the caption band: every candidate spot keeps its full height above
  `CAPTION_BAND_TOP_FRAC` (and `_compile_overlay_track` clamps once more).
- a card never covers a face: spots are tried in order (right corner, left corner, right
  middle, left middle); when every spot meets a face shown inside the card's window the card
  is ``face_collision``, never drawn over the face.
- cards are a sequence, not a stack: a later card overlapping an earlier one is
  ``overlaps_card``.
- a Visuals video needs ``videos_allowed`` (``visualVideos`` verified), else
  ``video_not_supported``; it plays muted from its start.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from pydantic import ValidationError

from app.agents._schemas.reaction_beats import TimedCard
from app.pipeline.phone_subtitled_lanes import CAPTION_BAND_TOP_FRAC, SubtitledOverlayCard
from app.services.phone_sources import PhoneSourceBinding, PhoneVisualBinding

if TYPE_CHECKING:
    from app.pipeline.render_geometry import NormalizedBox

# Variant row keys the workers persist and the editor / brief check read.
TIMED_CARD_RECEIPT_FIELD = "phone_timed_card_receipt"
TIMED_CARDS_FIELD = "phone_timed_cards"

# Width fraction of the canvas a small card occupies (the Talking default is 0.35; a
# little wider so a menu or receipt stays legible on a phone screen).
CARD_SCALE = 0.38
# Portrait canvas: a card roughly as tall as it is wide in pixels is this tall in canvas
# fractions (width * 9/16). Used only for the face / caption-band geometry checks.
CARD_ASPECT = 9 / 16
# Candidate (x_frac, y_frac) centres, tried in order: the top-right corner (the Talking
# "photo" slot), then top-left, then the middle band on either side. All keep the whole
# card above the caption band.
CARD_SPOTS: tuple[tuple[float, float], ...] = (
    (0.74, 0.20),
    (0.26, 0.20),
    (0.74, 0.42),
    (0.26, 0.42),
)
# A card that would start inside the final guard (or after the end) is not shown.
END_GUARD_S = 0.3
MIN_CARD_S = 0.3
_ID_PREFIX = "timed-"
_ASSET_PREFIX = "asset-"


@dataclass(frozen=True)
class FaceWindow:
    """A face box (canvas fractions) visible from ``start_s`` to ``end_s`` of the output."""

    start_s: float
    end_s: float
    box: NormalizedBox


@dataclass(frozen=True)
class GroundedTimedCards:
    cards: tuple[SubtitledOverlayCard, ...]
    receipt: dict[str, Any]


def clip_windows_from_recipe(
    recipe: Any,
    *,
    video_track_id: str,
    bindings: Iterable[PhoneSourceBinding],
    by_clip_id: bool = False,
) -> dict[str, tuple[float, float]]:
    """Where each footage clip plays on the compiled OUTPUT clock.

    Keyed by the binding's ``media_id`` (its FIRST appearance when one source plays twice),
    or -- ``by_clip_id`` -- by the recipe clip id (one entry per moment). Pool visuals and
    non-footage assets are left out: a card anchors to a clip the creator filmed.
    """
    track = next((t for t in recipe.tracks if t.id == video_track_id), None)
    if track is None:
        return {}
    asset_to_media: dict[str, str] = {}
    for binding in bindings:
        try:
            asset_to_media[binding.render_asset().id] = binding.media_id
        except Exception:  # noqa: BLE001 - a binding that cannot name its asset anchors nothing
            continue
    windows: dict[str, tuple[float, float]] = {}
    for clip in track.clips:
        start = float(clip.timeline_start)
        rate = float(getattr(clip, "rate", 1.0) or 1.0)
        end = start + float(clip.source_duration) / rate
        if by_clip_id:
            windows[clip.id] = (start, end)
            continue
        media_id = asset_to_media.get(clip.source_asset_id)
        if media_id is None or media_id in windows:
            continue
        windows[media_id] = (start, end)
    return windows


def _visual_for(visual_id: str, visuals: Sequence[PhoneVisualBinding]) -> PhoneVisualBinding | None:
    needle = visual_id.casefold()
    bare = needle[len(_ASSET_PREFIX) :] if needle.startswith(_ASSET_PREFIX) else needle
    for visual in visuals:
        if visual.media_id.casefold() in {needle, bare}:
            return visual
    return None


def _card_box(x_frac: float, y_frac: float) -> NormalizedBox:
    # render_geometry imports skia at module import; keep that off the API/CLI import
    # path (ios-contracts runs without GL libraries) by importing it where it is used.
    from app.pipeline.render_geometry import NormalizedBox  # noqa: PLC0415

    half_w = CARD_SCALE / 2
    half_h = CARD_SCALE * CARD_ASPECT / 2
    return NormalizedBox(x_frac - half_w, y_frac - half_h, x_frac + half_w, y_frac + half_h)


def _overlaps(a: NormalizedBox, b: NormalizedBox) -> bool:
    return a.left < b.right and b.left < a.right and a.top < b.bottom and b.top < a.bottom


def _spot_for(
    start_s: float, end_s: float, faces: Sequence[FaceWindow]
) -> tuple[float, float] | None:
    shown = [f.box for f in faces if f.start_s < end_s and start_s < f.end_s]
    for x_frac, y_frac in CARD_SPOTS:
        box = _card_box(x_frac, y_frac)
        if box.bottom > CAPTION_BAND_TOP_FRAC + 1e-9:
            continue
        if any(_overlaps(box, face) for face in shown):
            continue
        return x_frac, y_frac
    return None


def _round(value: float) -> float:
    return round(float(value), 3)


def ground_timed_cards(
    cards: Sequence[Mapping[str, Any] | TimedCard],
    *,
    clip_windows: Mapping[str, tuple[float, float]],
    timeline_end: float,
    visuals: Sequence[PhoneVisualBinding],
    videos_allowed: bool,
    faces: Sequence[FaceWindow] = (),
) -> GroundedTimedCards:
    """Ground ``cards`` (strategy rows or ``TimedCard``s) -> ``GroundedTimedCards``.

    Never raises for a bad card: each failure lands on ``receipt["unplaced"]`` with a stable
    ``reason`` (``invalid``, ``clip_not_in_edit``, ``past_end``, ``visual_not_bound``,
    ``video_not_supported``, ``overlaps_card``, ``face_collision``).
    """
    timeline_end = max(0.0, float(timeline_end))
    placed: list[dict[str, Any]] = []
    unplaced: list[dict[str, Any]] = []
    resolved: list[tuple[TimedCard, float, float, PhoneVisualBinding, str]] = []

    for index, raw in enumerate(cards):
        try:
            card = raw if isinstance(raw, TimedCard) else TimedCard.model_validate(dict(raw))
        except (ValidationError, TypeError, ValueError):
            card_id = raw.get("card_id") if isinstance(raw, Mapping) else None
            unplaced.append(
                {"card_id": str(card_id or f"card-{index}"), "reason": "invalid", "at_s": None}
            )
            continue
        base: dict[str, Any] = {
            "card_id": card.card_id,
            "media_id": None,
            "visual_id": card.visual_id,
            "label": card.label,
        }
        visual = _visual_for(card.visual_id, visuals)
        if visual is not None:
            base["media_id"] = visual.media_id
        if card.at_s is not None:
            start_s = float(card.at_s)
            anchor = "at_s"
        else:
            window = clip_windows.get(str(card.clip_id))
            if window is None:
                unplaced.append(
                    {**base, "at_s": None, "reason": "clip_not_in_edit", "clip_id": card.clip_id}
                )
                continue
            start_s = float(window[0]) + float(card.offset_s)
            anchor = "clip"
        start_s = _round(start_s)
        if start_s >= timeline_end - END_GUARD_S:
            unplaced.append(
                {**base, "at_s": start_s, "reason": "past_end", "timeline_end_s": timeline_end}
            )
            continue
        end_s = _round(min(start_s + float(card.hold_s), timeline_end))
        if end_s - start_s < MIN_CARD_S:
            unplaced.append(
                {**base, "at_s": start_s, "reason": "past_end", "timeline_end_s": timeline_end}
            )
            continue
        if visual is None:
            unplaced.append({**base, "at_s": start_s, "reason": "visual_not_bound"})
            continue
        if visual.kind == "video" and not videos_allowed:
            unplaced.append({**base, "at_s": start_s, "reason": "video_not_supported"})
            continue
        resolved.append((card, start_s, end_s, visual, anchor))

    resolved.sort(key=lambda row: (row[1], row[2], row[0].card_id))
    compiled: list[SubtitledOverlayCard] = []
    previous: tuple[str, float] | None = None
    for card, start_s, end_s, visual, anchor in resolved:
        base = {
            "card_id": card.card_id,
            "media_id": visual.media_id,
            "visual_id": card.visual_id,
            "label": card.label,
        }
        if previous is not None and start_s < previous[1] - 1e-6:
            unplaced.append(
                {**base, "at_s": start_s, "reason": "overlaps_card", "overlaps": previous[0]}
            )
            continue
        spot = _spot_for(start_s, end_s, faces)
        if spot is None:
            unplaced.append({**base, "at_s": start_s, "reason": "face_collision"})
            continue
        x_frac, y_frac = spot
        compiled.append(
            SubtitledOverlayCard(
                id=f"{_ID_PREFIX}{card.card_id}",
                media_id=visual.media_id,
                gcs_path=visual.gcs_path,
                generation=visual.generation,
                start_s=start_s,
                end_s=end_s,
                x_frac=x_frac,
                y_frac=y_frac,
                scale=CARD_SCALE,
                fade=True,
                z=0,
                kind="video" if visual.kind == "video" else "image",
                source_start_s=0.0,
            )
        )
        placed.append(
            {
                **base,
                "anchor": anchor,
                "at_s": start_s,
                "end_s": end_s,
                "hold_s": _round(end_s - start_s),
                "kind": "video" if visual.kind == "video" else "image",
                "x_frac": x_frac,
                "y_frac": y_frac,
            }
        )
        previous = (card.card_id, end_s)

    receipt = {
        "version": 1,
        "matcher": "timed",
        "timeline_end_s": timeline_end,
        "placed": placed,
        "unplaced": unplaced,
    }
    return GroundedTimedCards(cards=tuple(compiled), receipt=receipt)


def timed_card_rows(cards: Iterable[SubtitledOverlayCard]) -> list[dict[str, Any]]:
    """The grounded cards as the variant row persists them (`TIMED_CARDS_FIELD`)."""
    return [card.model_dump(mode="json") for card in cards]


def timed_cards_from_rows(rows: object) -> tuple[SubtitledOverlayCard, ...]:
    """Rows back to cards; an unreadable row list yields ``()`` (a Save never 500s on it)."""
    if not isinstance(rows, list):
        return ()
    cards: list[SubtitledOverlayCard] = []
    for row in rows:
        if not isinstance(row, Mapping):
            return ()
        try:
            cards.append(SubtitledOverlayCard.model_validate(dict(row)))
        except ValidationError:
            return ()
    return tuple(cards)


__all__ = [
    "CARD_SCALE",
    "CARD_SPOTS",
    "END_GUARD_S",
    "TIMED_CARD_RECEIPT_FIELD",
    "TIMED_CARDS_FIELD",
    "FaceWindow",
    "GroundedTimedCards",
    "clip_windows_from_recipe",
    "ground_timed_cards",
    "timed_card_rows",
    "timed_cards_from_rows",
]
