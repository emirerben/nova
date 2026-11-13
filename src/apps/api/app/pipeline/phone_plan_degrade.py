"""Degrade one bad row instead of refusing the whole phone video (KRI-1376, rule KRI-129).

Every late phone gate runs repair -> degrade -> fail. `phone_plan_repair` repairs: it keeps
the item and changes its look. This module degrades: when a typed reject names one row the
phone cannot draw, that row loses the part it cannot draw, the creator is told, and the rest
of the approved edit renders. Anything else (a reject that names no row, a row a degrade cannot
help, a creator-confirmed item) keeps failing with the original typed error.

A degrade here never touches timing, order, audio or text, so nothing the contract pins can
change: `verify_phone_recipe` and the pin-time check still run unchanged after it.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, NamedTuple

from app.kria.reply_language import say
from app.pipeline.phone_guided_plan import UnsupportedPhonePlan

_PLAIN_LOOKS = frozenset({"none", "golden_hour"})


class _MomentDegrade(NamedTuple):
    # The moment's changed fields, or None when no change makes this row drawable.
    patch: Callable[[Any], dict[str, Any] | None]
    en: str
    tr: str


def _plain_video(moment) -> dict[str, Any] | None:  # noqa: ANN001
    if moment.kind != "video":
        return None
    return {
        "layout": "fullscreen",
        "image_motion": None,
        "look_preset": moment.look_preset if moment.look_preset in _PLAIN_LOOKS else "none",
        "look_adjustments": None,
    }


def _plain_photo(moment) -> dict[str, Any]:  # noqa: ANN001
    card = moment.layout == "supporting_card"
    return {
        "image_motion": None,
        "look_preset": "none",
        "look_adjustments": None,
        "playback_rate": None,
        "source_crop": None if card else moment.source_crop,
    }


def _no_look(_moment) -> dict[str, Any]:  # noqa: ANN001
    return {"look_preset": "none"}


def _still_without_freeze(moment) -> dict[str, Any] | None:  # noqa: ANN001
    # A photo already holds for its whole slot; only a moving clip's freeze is a program.
    return {"freeze_hold_s": None} if moment.kind == "image" else None


def _no_transition_after(_moment) -> dict[str, Any]:  # noqa: ANN001
    return {"transition_after": None}


_DREW_PLAIN = (
    "Showed clip {n} without a look your iPhone can't draw yet",
    "{n}. klibi iPhone'unun henüz çizemediği bir görünüm olmadan gösterdim",
)
_NO_GRADE = (
    "Left the color look off clip {n}: your iPhone can't grade it yet",
    "{n}. klipteki renk görünümünü kaldırdım: iPhone'un bunu henüz uygulayamıyor",
)

# Reject reason (`UnsupportedPhonePlan.reason`) -> how its one moment still renders.
MOMENT_DEGRADES: dict[str, _MomentDegrade] = {
    "moment_treatment": _MomentDegrade(_plain_video, *_DREW_PLAIN),
    "photo_treatment": _MomentDegrade(_plain_photo, *_DREW_PLAIN),
    "look_source": _MomentDegrade(_no_look, *_NO_GRADE),
    "look_with_tilt": _MomentDegrade(_no_look, *_NO_GRADE),
    "freeze_speed": _MomentDegrade(
        _still_without_freeze,
        "Kept photo {n} on screen without a freeze effect",
        "{n}. fotoğrafı donma efekti olmadan ekranda tuttum",
    ),
    "final_transition": _MomentDegrade(
        _no_transition_after,
        "Ended your video on a cut instead of a transition",
        "Videonu geçiş yerine düz bir kesmeyle bitirdim",
    ),
}


def degrade_plan_for_phone(plan, exc: Exception) -> tuple[Any, list[str]]:  # noqa: ANN001
    """``(plan, notes)`` with the one row ``exc`` names made drawable; ``(plan, [])`` otherwise."""
    if not isinstance(exc, UnsupportedPhonePlan) or exc.reason not in MOMENT_DEGRADES:
        return plan, []
    position = next(
        (i for i, m in enumerate(plan.story_timeline) if m.moment_id == exc.item_id), None
    )
    if position is None:
        return plan, []
    degrade = MOMENT_DEGRADES[exc.reason]
    moment = plan.story_timeline[position]
    patch = degrade.patch(moment)
    if not patch or all(getattr(moment, key) == value for key, value in patch.items()):
        return plan, []
    timeline = list(plan.story_timeline)
    timeline[position] = moment.model_copy(update=patch)
    record_recovery(reason=exc.reason, lane=exc.lane, item_id=exc.item_id, action="dropped")
    note = say(en=degrade.en, tr=degrade.tr, n=position + 1)
    return plan.model_copy(update={"story_timeline": timeline}), [note]


def record_recovery(
    *, reason: str | None, lane: str | None, item_id: str | None, action: str
) -> None:
    """The admin trace of one degrade (``action``: ``"dropped"`` or ``"shrunk"``)."""
    from app.services.pipeline_trace import record_pipeline_event  # noqa: PLC0415

    record_pipeline_event(
        "phone_plan",
        "requirement_recovery",
        {
            "decision": "render_with_notes",
            "reason": reason,
            "lane": lane,
            "item_id": item_id,
            "action": action,
        },
    )
