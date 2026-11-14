"""Degrade one bad row instead of refusing the whole phone video (KRI-1376, rule KRI-129).

Every late phone gate runs repair -> degrade -> fail. `phone_plan_repair` repairs: it keeps
the item and changes its look. This module degrades: when a typed reject names one row the
phone cannot draw, that row loses the part it cannot draw, the creator is told exactly what it
lost, and the rest of the approved edit renders. Anything else (a reject that names no row, a
row a degrade cannot help, an animation the creator chose) keeps failing with the original
typed error.

A degrade never touches timing, order, audio or words, so nothing the contract pins can
change: `verify_phone_recipe` and the pin-time check still run unchanged after it.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Any, Literal, NamedTuple

from app.kria.reply_language import say
from app.pipeline.phone_guided_plan import UnsupportedPhonePlan

_PLAIN_LOOKS = frozenset({"none", "golden_hour"})
_TEXT_LANES = ("text_elements", "context_label_text_elements", "narration_label_text_elements")
# More clip numbers than this in one note read as a count ("8 clips").
_LISTED_NUMBERS = 4


class Recovery(NamedTuple):
    """One row kept drawable by a degrade (``"dropped"``) or a contract text shrink.

    The worker records it once it keeps the compile it came from (``record_recoveries``);
    ``lost`` (keys of ``_LOST``) is what the row gave up, so its note says exactly that.
    """

    reason: str
    lane: str | None
    item_id: str | None
    action: Literal["dropped", "shrunk"]
    lost: tuple[str, ...] = ()
    subject: Literal["clip", "photo", "text", "ending"] | None = None
    number: int | None = None
    label: str | None = None


class _Degrade(NamedTuple):
    rows: Literal["moment", "text"]
    # The row's new field values, or None when no change makes this row drawable.
    patch: Callable[[Any], dict[str, Any] | None]


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


def _plain_entrance(element) -> dict[str, Any] | None:  # noqa: ANN001
    # The words stay; only a planner-picked animation gives way. One the creator chose refuses.
    chosen = (element.source_params or {}).get("creator_style_fields") or ()
    return None if "effect" in chosen else {"effect": "fade-in"}


# Reject reason (`UnsupportedPhonePlan.reason`) -> how its one row still renders.
DEGRADES: dict[str, _Degrade] = {
    "moment_treatment": _Degrade("moment", _plain_video),
    "photo_treatment": _Degrade("moment", _plain_photo),
    "look_source": _Degrade("moment", _no_look),
    "look_with_tilt": _Degrade("moment", _no_look),
    "freeze_speed": _Degrade("moment", _still_without_freeze),
    "final_transition": _Degrade("moment", _no_transition_after),
    "text_effect": _Degrade("text", _plain_entrance),
}

# A changed field -> what the creator is told the row lost (note order).
_LOST = {
    "layout": "card",
    "image_motion": "zoom",
    "look_preset": "look",
    "look_adjustments": "color",
    "source_crop": "crop",
    "playback_rate": "speed",
    "freeze_hold_s": "freeze",
    "transition_after": "transition",
    "effect": "animation",
}


def degrade_plan_for_phone(plan, exc: Exception) -> tuple[Any, list[Recovery]]:  # noqa: ANN001
    """``(plan, [recovery])`` with the one row ``exc`` names made drawable; ``(plan, [])``
    when the reject names no row this module can help."""
    if not isinstance(exc, UnsupportedPhonePlan) or exc.item_id is None:
        return plan, []
    degrade = DEGRADES.get(exc.reason or "")
    if degrade is None:
        return plan, []
    if degrade.rows == "text":
        return _degrade_text(plan, exc, degrade)
    return _degrade_moment(plan, exc, degrade)


def _changed(row, patch: dict[str, Any] | None) -> dict[str, Any]:  # noqa: ANN001
    return {key: value for key, value in (patch or {}).items() if getattr(row, key) != value}


def _lost(patch: dict[str, Any]) -> tuple[str, ...]:
    return tuple(part for key, part in _LOST.items() if key in patch)


def _degrade_moment(plan, exc: UnsupportedPhonePlan, degrade: _Degrade):  # noqa: ANN001, ANN202
    timeline = list(plan.story_timeline)
    position = next((i for i, m in enumerate(timeline) if m.moment_id == exc.item_id), None)
    if position is None:
        return plan, []
    moment = timeline[position]
    patch = _changed(moment, degrade.patch(moment))
    if not patch:
        return plan, []
    timeline[position] = moment.model_copy(update=patch)
    subject = (
        "ending"
        if exc.reason == "final_transition"
        else "photo"
        if moment.kind == "image"
        else "clip"
    )
    recovery = Recovery(
        reason=exc.reason or "",
        lane=exc.lane,
        item_id=exc.item_id,
        action="dropped",
        lost=_lost(patch),
        subject=subject,
        number=position + 1,
    )
    return plan.model_copy(update={"story_timeline": timeline}), [recovery]


def _degrade_text(plan, exc: UnsupportedPhonePlan, degrade: _Degrade):  # noqa: ANN001, ANN202
    from app.pipeline.phone_plan_repair import _quoted  # noqa: PLC0415

    for lane in _TEXT_LANES:
        elements = list(getattr(plan, lane))
        index = next((i for i, e in enumerate(elements) if e.id == exc.item_id), None)
        if index is None:
            continue
        element = elements[index]
        patch = _changed(element, degrade.patch(element))
        if not patch:
            return plan, []
        elements[index] = element.model_copy(update=patch)
        recovery = Recovery(
            reason=exc.reason or "",
            lane=exc.lane,
            item_id=exc.item_id,
            action="dropped",
            lost=_lost(patch),
            subject="text",
            label=_quoted(element.text),
        )
        return plan.model_copy(update={lane: elements}), [recovery]
    return plan, []


def degrade_notes(recoveries: Sequence[Recovery]) -> list[str]:
    """One note per kind of change, in the bound chat language.

    A row degraded twice (a zoom, then a look) is one row that lost both; rows that lost the
    same parts share a line, so a look every clip carried is one note, not one per clip. A row
    is its item, so two texts with the same words are two rows.
    """
    rows_lost: dict[tuple[str | None, str | None], tuple[Recovery, tuple[str, ...]]] = {}
    for recovery in recoveries:
        if recovery.action != "dropped":
            continue
        row = (recovery.subject, recovery.item_id)
        first, lost = rows_lost.get(row, (recovery, ()))
        merged = {*lost, *recovery.lost}
        rows_lost[row] = (first, tuple(part for part in _LOST.values() if part in merged))
    groups: dict[tuple[str | None, tuple[str, ...]], list[tuple[int | None, str | None]]] = {}
    for (subject, _item_id), (first, lost) in rows_lost.items():
        groups.setdefault((subject, lost), []).append((first.number, first.label))
    notes = []
    for (subject, lost), rows in groups.items():
        if subject == "ending":
            notes.append(
                say(
                    en="Ended your video on a cut instead of a transition",
                    tr="Videonu geçiş yerine düz bir kesmeyle bitirdim",
                )
            )
        elif subject == "text":
            notes.append(_text_note([label or "" for _number, label in rows]))
        else:
            numbers = sorted(number for number, _label in rows if number is not None)
            parts = _and([_part(part) for part in lost])
            owned = (
                say(en="its {parts}", tr="{parts}", parts=parts)
                if len(numbers) == 1
                else say(en="their {parts}", tr="{parts}", parts=parts)
            )
            notes.append(
                say(
                    en="Showed {which} without {lost}: your iPhone can't draw that yet",
                    tr="{which} {lost} olmadan gösterdim: iPhone'un bunu henüz çizemiyor",
                    which=_which(subject, numbers),
                    lost=owned,
                )
            )
    return notes


def _text_note(labels: Sequence[str]) -> str:
    if len(labels) == 1:
        return say(
            en="Showed the text {text} with a simple fade-in: your iPhone can't play its "
            "animation yet",
            tr="{text} yazısını basit bir belirmeyle gösterdim: iPhone'un animasyonunu henüz "
            "oynatamıyor",
            text=labels[0],
        )
    return say(
        en="Showed {count} texts with a simple fade-in: your iPhone can't play their "
        "animations yet",
        tr="{count} yazıyı basit bir belirmeyle gösterdim: iPhone'un animasyonlarını henüz "
        "oynatamıyor",
        count=len(labels),
    )


def _which(subject: str | None, numbers: Sequence[int]) -> str:
    photo = subject == "photo"
    if len(numbers) > _LISTED_NUMBERS:
        if photo:
            return say(en="{count} photos", tr="{count} fotoğrafı", count=len(numbers))
        return say(en="{count} clips", tr="{count} klibi", count=len(numbers))
    listed = _and([str(number) for number in numbers])
    if len(numbers) == 1:
        if photo:
            return say(en="photo {n}", tr="{n}. fotoğrafı", n=listed)
        return say(en="clip {n}", tr="{n}. klibi", n=listed)
    if photo:
        return say(en="photos {n}", tr="{n}. fotoğrafları", n=listed)
    return say(en="clips {n}", tr="{n}. klipleri", n=listed)


def _part(part: str) -> str:
    words = {
        "card": lambda: say(en="card layout", tr="kart yerleşimi"),
        "zoom": lambda: say(en="zoom", tr="yakınlaştırma"),
        "look": lambda: say(en="look", tr="görünüm"),
        "color": lambda: say(en="color adjustments", tr="renk ayarları"),
        "crop": lambda: say(en="crop", tr="kırpma"),
        "speed": lambda: say(en="speed change", tr="hız değişikliği"),
        "freeze": lambda: say(en="freeze effect", tr="donma efekti"),
    }
    return words[part]()


def _and(items: Sequence[str]) -> str:
    if len(items) < 2:
        return "".join(items)
    return say(
        en="{rest} and {last}", tr="{rest} ve {last}", rest=", ".join(items[:-1]), last=items[-1]
    )


def shrink_sizes(size_px: float | None, *, chosen: bool = False) -> list[float | None]:
    """The sizes a text may take to find a clear spot, largest first: its own, then 2 px
    smaller each step down to the readable floor. A size the creator ``chosen`` never shrinks.
    """
    from app.pipeline.phone_text_style import _READABLE_FLOOR_PX  # noqa: PLC0415

    if size_px is None or chosen:
        return [size_px]
    return [size_px, *(float(px) for px in range(int(size_px) - 2, _READABLE_FLOOR_PX - 1, -2))]


def shrunk_on_screen_note(text: str) -> str:
    from app.pipeline.phone_plan_repair import _quoted  # noqa: PLC0415

    return say(
        en="Made your text {text} smaller so it fits clearly on screen",
        tr="{text} yazını ekrana net sığsın diye küçülttüm",
        text=_quoted(text),
    )


def captions_dropped_note() -> str:
    return say(
        en="Left out the captions: your iPhone can't draw them yet",
        tr="Altyazıları çıkardım: iPhone'un onları henüz çizemiyor",
    )


def record_recoveries(recoveries: Sequence[Recovery]) -> None:
    """The admin trace of the degrades and shrinks the worker kept, one event each."""
    from app.services.pipeline_trace import record_pipeline_event  # noqa: PLC0415

    for recovery in recoveries:
        record_pipeline_event(
            "phone_plan",
            "requirement_recovery",
            {
                "decision": "render_with_notes",
                "reason": recovery.reason,
                "lane": recovery.lane,
                "item_id": recovery.item_id,
                "action": recovery.action,
            },
        )
