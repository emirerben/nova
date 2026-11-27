"""KRI-1615 stage B: her own Visuals photos as cards in an intro look, from the offer to the Save.

"Try intro styles" may lay two or four of her photos out with the title (`intro_scene`
arrangements, `intro_props_layout`). The offer stores nothing, so the editor posts the cards with
her normal Save. A Talking Save accepts only photos the job has pinned
(`phone_subtitled_editor._overlays_from_sections`), so a Save that places an intro card pins its
photo first, through the one receipt path (`bind_phone_visual_assets`), like a background
(KRI-1117) or an accepted generated image (KRI-1253). Any other photo stays refused there.

One rule says which of her pictures may be a card (`card_photo`), for the offer's query and the
Save's pin: an opaque photo is a ``photo`` card, a transparent PNG or WebP a ``sticker`` (KRI-1616;
one she generated with the generate-overlay flow included, never a new generation);
one rule says where her captions are (`caption_band`), for the offer's layout stage, the Save's
``caption_clear`` mark and the fit measurement (`app/cli/intro_cards_fit.py`). The failure modes
(M31-M45) and their guards are in `app/pipeline/intro_styles.py`.

``INTRO_CARDS_ENABLED`` off makes the pin a no-op, so a Save is what it was.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from app.config import settings
from app.models import PlanItemAsset

# Card ids are the slot's: applying any intro option replaces every card with this prefix, so a
# second apply, another look or "No intro style" converges instead of stacking cards.
INTRO_CARD_ID_PREFIX = "intro-card-"
NOT_IN_POOL = "intro_card_not_in_pool"
UNAVAILABLE = "intro_card_unavailable"
# For THIS request only (the `NEW_BACKGROUND_COLUMNS_ATTR` precedent): ``{card id: caption_clear}``
# the pin decided from server evidence. The Save's lane rebuild reads it; it is never persisted.
CAPTION_CLEAR_ATTR = "_kria_intro_card_caption_clear"
_CANVAS_RATIO = 1080.0 / 1920.0
# A caption layer's box is estimated from its font size, so it is padded; receipt boxes are not.
_LAYER_PAD = 0.01
PHOTO = "photo"
STICKER = "sticker"
# A sticker draws its own transparency: a format the phone decodes with alpha (a HEIC's
# browser-safe preview is a JPEG).
_STICKER_TYPES = {"image/png": ".png", "image/webp": ".webp"}


@dataclass(frozen=True)
class CardPhoto:
    """A picture of hers that may be an intro card: ``aspect`` is width over height, upright;
    ``kind`` `PHOTO` (opaque) or `STICKER` (transparent, `analysis.has_alpha`)."""

    media_id: str
    gcs_path: str
    generation: str
    aspect: float
    kind: str = PHOTO


def card_id(index: int, kind: str = PHOTO) -> str:
    """``intro-card-<n>`` for a photo, ``intro-card-sticker-<n>`` for a sticker: both keep the
    prefix every apply replaces, and a build before stickers drops a look holding the second (its
    parser takes numeric ids with ``prop_<n>`` slots only)."""
    return f"{INTRO_CARD_ID_PREFIX}{'sticker-' if kind == STICKER else ''}{index + 1}"


def is_intro_card_id(value: object) -> bool:
    return isinstance(value, str) and value.startswith(INTRO_CARD_ID_PREFIX)


def card_photo(row: Any, *, user_id: object, item_id: object) -> CardPhoto | None:
    """``row`` as a card picture, or None. What the Save's bind and its pool fence accept
    (owner, item, ready, a picture in this item's pool namespace at a known generation, decoded,
    with a browser-safe preview when it needs one), plus what a card needs: its upright shape and
    a measured transparency. `image_has_alpha` (a pixel under full opacity, so an opaque RGBA
    screenshot is a photo) makes it a `STICKER`, which must also be a PNG or WebP."""
    from app.services.phone_visuals import MAX_PHONE_VISUAL_BYTES  # noqa: PLC0415
    from app.services.pool_asset_preview import needs_preview  # noqa: PLC0415

    analysis = getattr(row, "analysis", None) or {}
    path = str(getattr(row, "gcs_path", "") or "")
    prefix = f"users/{user_id}/plan/{item_id}/pool/"
    size = getattr(row, "upload_size_bytes", None)
    aspect = getattr(row, "aspect", None)
    if (
        str(getattr(row, "user_id", "")) != str(user_id)
        or str(getattr(row, "plan_item_id", "")) != str(item_id)
        or getattr(row, "kind", None) != "image"
        or getattr(row, "status", None) != "ready"
        or getattr(row, "media_status", None) != "ready"
        or getattr(row, "deduplicated_to_asset_id", None) is not None
        or getattr(row, "library_source_id", None) is not None
        or not path.startswith(prefix)
        or any(part in {"", ".", ".."} for part in path[len(prefix) :].split("/"))
        or not str(getattr(row, "gcs_generation", "") or "")
        or (size is not None and not 0 < int(size) <= MAX_PHONE_VISUAL_BYTES)
        or not (isinstance(aspect, (int, float)) and aspect > 0)
        or analysis.get("dims_upright") is not True
        or analysis.get("has_alpha") not in (True, False)
        or (
            needs_preview("image", getattr(row, "upload_content_type", None), path)
            and not getattr(row, "preview_gcs_path", None)
        )
    ):
        return None
    kind = STICKER if analysis["has_alpha"] is True else PHOTO
    content_type = str(getattr(row, "upload_content_type", "") or "").lower()
    if kind == STICKER and _STICKER_TYPES.get(content_type) != path[path.rfind(".") :].lower():
        return None
    return CardPhoto(str(row.id), path, str(row.gcs_generation), float(aspect), kind)


def _statement(item_id: uuid.UUID, user_id: uuid.UUID):  # noqa: ANN202
    return (
        select(PlanItemAsset)
        .where(
            PlanItemAsset.plan_item_id == item_id,
            PlanItemAsset.user_id == user_id,
            PlanItemAsset.kind == "image",
            PlanItemAsset.status == "ready",
        )
        .order_by(PlanItemAsset.created_at, PlanItemAsset.id)
    )


def _photos(rows: Sequence[Any], item_id: object, user_id: object) -> list[CardPhoto]:
    found = (card_photo(row, user_id=user_id, item_id=item_id) for row in rows)
    return [photo for photo in found if photo is not None]


async def load_card_photos(
    db: AsyncSession, item_id: uuid.UUID, user_id: uuid.UUID
) -> list[CardPhoto]:
    """Her card photos, oldest first (the order the offer fills the slots in)."""
    rows = (await db.execute(_statement(item_id, user_id))).scalars().all()
    return _photos(rows, item_id, user_id)


def load_card_photos_sync(db: Session, item_id: uuid.UUID, user_id: uuid.UUID) -> list[CardPhoto]:
    return _photos(db.execute(_statement(item_id, user_id)).scalars().all(), item_id, user_id)


# --- where her captions are ---------------------------------------------------------------


def caption_band(
    variant: Mapping[str, Any], assembly: Mapping[str, Any], window: tuple[float, float]
) -> tuple[float, float] | None:
    """``(top, bottom)`` of her captions while ``window`` shows, canvas fractions, or None when
    nothing says where they are. The union of the boxes the worker measured the title against
    (its receipt) and the pinned recipe's caption layers in the window (estimated from their font
    size, so padded): the receipt goes stale after a Save that moves the captions, the layers are
    the current ones."""
    from app.pipeline.canvas import PORTRAIT  # noqa: PLC0415
    from app.pipeline.intro_styles import layer_box, recipe_layers  # noqa: PLC0415

    spans: list[tuple[float, float]] = []
    receipt = variant.get("opening_title_placement")
    for box in (receipt.get("caption_boxes") if isinstance(receipt, Mapping) else None) or []:
        try:
            spans.append((float(box["top"]), float(box["bottom"])))
        except (KeyError, TypeError, ValueError):
            continue
    start, end = window
    for layer in recipe_layers(assembly, str(variant.get("variant_id") or "")):
        if not str(layer.get("id") or "").startswith("caption"):
            continue
        if float(layer.get("end", 0)) <= start or float(layer.get("start", 0)) >= end:
            continue
        box = layer_box(layer, PORTRAIT)
        if box is not None:
            spans.append((max(0.0, box.top - _LAYER_PAD), min(1.0, box.bottom + _LAYER_PAD)))
    if not spans:
        return None
    return min(top for top, _ in spans), max(bottom for _, bottom in spans)


def card_box(x_frac: float, y_frac: float, scale: float, aspect: float) -> Any:
    """The box the phone draws a card in: ``scale`` wide, its photo's own shape, centred."""
    from app.pipeline.render_geometry import NormalizedBox  # noqa: PLC0415

    height = scale * _CANVAS_RATIO / aspect
    return NormalizedBox(
        x_frac - scale / 2, y_frac - height / 2, x_frac + scale / 2, y_frac + height / 2
    )


def clears_captions(box: Any, band: tuple[float, float] | None) -> bool:
    """True when ``box`` keeps off the caption row the band measures (a card then keeps its own
    place; the phone compile only lifts an unmarked card above `CAPTION_BAND_TOP_FRAC`)."""
    from app.pipeline.render_geometry import NormalizedBox  # noqa: PLC0415
    from app.services.phone_card_layout import boxes_intersect  # noqa: PLC0415

    return band is not None and not boxes_intersect(box, NormalizedBox(0.0, band[0], 1.0, band[1]))


def caption_clear_decisions(
    variant: Mapping[str, Any],
    assembly: Mapping[str, Any],
    posted: Sequence[Mapping[str, Any]],
    photos: Sequence[CardPhoto],
) -> dict[str, bool]:
    """``{card id: caption_clear}`` for every posted intro card on one of ``photos``: the
    server's mark from its own evidence (her captions, the photo's shape, the card's place),
    never the client's."""
    by_path = {photo.gcs_path: photo for photo in photos}
    out: dict[str, bool] = {}
    for card in posted:
        photo = by_path.get(str(card.get("src_gcs_path") or ""))
        if not is_intro_card_id(card.get("id")) or photo is None:
            continue
        pip = card.get("display_mode", "pip") == "pip"
        start, end = float(card.get("start_s", 0.0)), float(card.get("end_s", 3.0))
        box = card_box(
            float(card.get("x_frac", 0.5)),
            float(card.get("y_frac", 0.4)),
            float(card.get("scale", 0.35)),
            photo.aspect,
        )
        out[str(card["id"])] = pip and clears_captions(
            box, caption_band(variant, assembly, (start, end))
        )
    return out


# --- the Save -----------------------------------------------------------------------------


def _posted_intro_cards(job: Any, variant_id: str, payload: Any) -> tuple[dict, list[dict]]:
    """``(variant, posted intro cards)``; empty unless the switch is on and the Save sets cards
    on a phone Talking variant."""
    if not settings.intro_cards_enabled:
        return {}, []
    if "media_overlays" not in getattr(payload, "model_fields_set", ()):
        return {}, []
    from app.routes.generative_jobs import _find_variant  # noqa: PLC0415
    from app.services.phone_subtitled_editor import (  # noqa: PLC0415
        is_phone_subtitled_editor_variant,
    )

    variant = _find_variant(job, variant_id) or {}
    if not is_phone_subtitled_editor_variant(variant):
        return {}, []
    posted = [
        card if isinstance(card, dict) else card.model_dump()
        for card in getattr(payload, "media_overlays", None) or []
    ]
    return variant, [card for card in posted if is_intro_card_id(card.get("id"))]


def _pins_and_marks(
    job: Any, variant: dict, cards: list[dict], photos: Sequence[CardPhoto]
) -> list[CardPhoto]:
    """Record this request's marks on ``job``; return the photos still to pin."""
    from app.services.phone_subtitled_editor import pinned_visual  # noqa: PLC0415

    used = {str(card.get("src_gcs_path") or "") for card in cards}
    chosen = [photo for photo in photos if photo.gcs_path in used]
    setattr(
        job,
        CAPTION_CLEAR_ATTR,
        caption_clear_decisions(variant, job.assembly_plan or {}, cards, chosen),
    )
    return [p for p in chosen if pinned_visual(job.assembly_plan or {}, p.media_id) is None]


async def pin_intro_cards_for_commit(
    db: AsyncSession, job: Any, variant_id: str, payload: Any, *, plan_item_id: uuid.UUID
) -> None:
    """Pin the photos a Save places as intro cards; a no-op for any other Save. A card on a photo
    that is not one of her card photos is left for the Save to refuse, as before."""
    from app.services.phone_speaker_background import (  # noqa: PLC0415
        pin_pool_visual_for_background,
    )

    variant, cards = _posted_intro_cards(job, variant_id, payload)
    if not cards:
        return
    photos = await load_card_photos(db, plan_item_id, job.user_id)
    for photo in _pins_and_marks(job, variant, cards, photos):
        await pin_pool_visual_for_background(
            db,
            job,
            plan_item_id=plan_item_id,
            media_id=photo.media_id,
            kind="image",
            not_in_pool=NOT_IN_POOL,
            unavailable=UNAVAILABLE,
        )


def pin_intro_cards_for_commit_sync(
    db: Session, job: Any, variant_id: str, payload: Any, *, plan_item_id: uuid.UUID
) -> None:
    from app.services.phone_speaker_background import (  # noqa: PLC0415
        pin_pool_visual_for_background_sync,
    )

    variant, cards = _posted_intro_cards(job, variant_id, payload)
    if not cards:
        return
    photos = load_card_photos_sync(db, plan_item_id, job.user_id)
    for photo in _pins_and_marks(job, variant, cards, photos):
        pin_pool_visual_for_background_sync(
            db,
            job,
            plan_item_id=plan_item_id,
            media_id=photo.media_id,
            kind="image",
            not_in_pool=NOT_IN_POOL,
            unavailable=UNAVAILABLE,
        )
