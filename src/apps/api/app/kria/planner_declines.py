"""Card lines for what the Main Creator parse could not keep (KRI-1097 wave 2, planner boundary).

`MainCreatorAgent.parse` records each on the strategy (``planner_declines``): a title
animation or label position the renderer has no name for (the strategy validators read it as
unset), and a timing, cadence or reuse ask quoted from the creator that the manifest cannot
render (the parse fell back to the server's own reading). `route_disclosure.strategy_notices`
names them on the approval card on every route, so nothing the creator asked for disappears
silently. Pure and fail-open; nothing here refuses a render or blocks approval.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from app.kria.reply_language import say
from app.kria.route_capabilities._lines import clip


def _title_animation(value: object) -> str | None:
    if not str(value or "").strip():
        return None
    return say(
        en=f"I don't have a \"{clip(value, 40)}\" animation for the title yet, so it won't be used",
        tr=f'Başlık için henüz "{clip(value, 40)}" animasyonum yok, o yüzden kullanılmayacak',
    )


def _label_position(value: object) -> str | None:
    if not str(value or "").strip():
        return None
    return say(
        en=(
            f'I can\'t place the clip labels "{clip(value, 40)}" yet, so they stay where the '
            "layout puts them"
        ),
        tr=(
            f'Klip etiketlerini henüz "{clip(value, 40)}" konumuna yerleştiremiyorum, o yüzden '
            "düzenin koyduğu yerde kalıyorlar"
        ),
    )


def _timing(_value: object) -> str:
    return say(
        en=(
            "I can't keep the photo and video timing you asked for on this edit, so it uses "
            "the standard timing"
        ),
        tr=(
            "Bu düzenlemede istediğin fotoğraf ve video zamanlamasını koruyamıyorum, o yüzden "
            "standart zamanlama kullanılıyor"
        ),
    )


def _cadence(_value: object) -> str:
    return say(
        en=(
            "I can't keep the exact alternating cut you asked for on this edit, so it is cut "
            "as a regular montage"
        ),
        tr=(
            "Bu düzenlemede istediğin dönüşümlü kesimi aynen koruyamıyorum, o yüzden normal "
            "bir montaj olarak kesiliyor"
        ),
    )


def _reuse(_value: object) -> str:
    return say(
        en="I can't repeat or loop your footage on this edit, so no moment of it plays twice",
        tr=(
            "Bu düzenlemede çekimlerini tekrarlayamıyor ya da döngüye alamıyorum, o yüzden "
            "hiçbir an iki kez oynamıyor"
        ),
    )


_LINES: dict[str, Callable[[object], str | None]] = {
    "title_animation": _title_animation,
    "label_position": _label_position,
    "mixed_media_timing": _timing,
    "montage_cadence": _cadence,
    "video_reuse_policy": _reuse,
}


def decline_lines(strategy: Mapping[str, Any] | None) -> list[tuple[str, str]]:
    """``(subject, sentence)`` per recorded decline, in order; one line per field."""
    out: list[tuple[str, str]] = []
    seen: set[str] = set()
    for item in (strategy or {}).get("planner_declines") or ():
        if not isinstance(item, Mapping):
            continue
        field = str(item.get("field") or "")
        build = _LINES.get(field)
        if build is None or field in seen:
            continue
        try:
            text = build(item.get("value"))
        except Exception:  # noqa: BLE001 - a disclosure must never fail a draft
            text = None
        if text:
            seen.add(field)
            out.append((f"planner_{field}", text))
    return out
