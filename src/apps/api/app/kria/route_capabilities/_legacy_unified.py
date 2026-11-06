"""The unified montage's disclosures as they were before the route registry (origin/main
ff5f2adbe `route_disclosure.UNREAD_STRATEGY_FIELDS` and its one ask decline), FROZEN.

`route_disclosure` reads this table, and only this one, while ``ROUTE_REGISTRY_ENABLED`` is
off, so an entry a lane adds to `unified_montage` (or a sentence it rewords in `_lines`) can
never change what the switch-off card says. Never edit it; delete it with the switch.
Guard: `tests/kria/test_route_registry_flag_off.py` (recorded golden from ff5f2adbe).
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from typing import Any

from app.config import settings
from app.kria.brief import BriefRequirement
from app.kria.reply_language import say
from app.kria.route_capabilities._types import AskDecline, Handling, NotApplied

_ROUTE_DIRECTION = "fast_montage"
_ROUTE_PACE = "fast"


def _clip(value: object, limit: int = 70) -> str:
    text = " ".join(str(value or "").split()).replace('"', "'")
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _list(values: Iterable[object]) -> str:
    return ", ".join(_clip(v, 40) for v in values if str(v or "").strip())


def _hook(value: Any, _s: Mapping[str, Any]) -> str | None:
    if not str(value or "").strip():
        return None
    return say(
        en=(
            f'I can\'t use the opening hook idea ("{_clip(value)}") on this video yet, '
            "so it won't be shown as text"
        ),
        tr=(
            f'Açılış kanca fikrini ("{_clip(value)}") bu videoda henüz kullanamıyorum, '
            "o yüzden yazı olarak gösterilmeyecek"
        ),
    )


def _story(value: Any, _s: Mapping[str, Any]) -> str | None:
    items = [v for v in (value or []) if str(v or "").strip()]
    if not items:
        return None
    return say(
        en=(
            f"I can't follow the planned story structure ({_list(items)}) on this video yet, "
            "so it won't change the order of the clips"
        ),
        tr=(
            f"Planlanan hikâye yapısını ({_list(items)}) bu videoda henüz uygulayamıyorum, "
            "o yüzden kliplerin sırasını değiştirmeyecek"
        ),
    )


def _pacing(value: Any, strategy: Mapping[str, Any]) -> str | None:
    if not value or value == _ROUTE_PACE:
        return None
    from app.services.length_policy import effective_pace  # noqa: PLC0415

    if effective_pace(strategy) is not None:
        return None
    return say(
        en=(
            f"The {str(value).replace('_', ' ')} pacing isn't applied on this video yet: "
            "the cuts follow a fast pace"
        ),
        tr=(
            f"{str(value).replace('_', ' ')} tempo bu videoda henüz uygulanmıyor: "
            "kesimler hızlı tempoda"
        ),
    )


def _direction(value: Any, _s: Mapping[str, Any]) -> str | None:
    if not value or value == _ROUTE_DIRECTION:
        return None
    return say(
        en=(
            f"The {str(value).replace('_', ' ')} direction isn't applied on this video yet: "
            "it is cut as a fast montage"
        ),
        tr=(
            f"{str(value).replace('_', ' ')} yönü bu videoda henüz uygulanmıyor: "
            "hızlı bir montaj olarak kesiliyor"
        ),
    )


def _caption_style(value: Any, _s: Mapping[str, Any]) -> str | None:
    if not value or value in {"auto", "none"}:
        return None
    return say(
        en=f"I can't apply the {value} caption style on this video yet, so it won't be used",
        tr=f"{value} altyazı stilini bu videoda henüz uygulayamıyorum, kullanılmayacak",
    )


def _treatments(value: Any, _s: Mapping[str, Any]) -> str | None:
    items = [v for v in (value or []) if str(v or "").strip()]
    if not items:
        return None
    return say(
        en=(
            f"I can't add the treatments ({_list(items)}) on this video yet, "
            "so they won't be applied"
        ),
        tr=f"Şu efektleri ({_list(items)}) bu videoda henüz ekleyemiyorum, uygulanmayacak",
    )


def _archetype(value: Any, _s: Mapping[str, Any]) -> str | None:
    if not value:
        return None
    return say(
        en=f"I can't use the {str(value).replace('_', ' ')} story archetype on this video yet",
        tr=f"{str(value).replace('_', ' ')} hikâye kalıbını bu videoda henüz kullanamıyorum",
    )


def _hero(value: Any, _s: Mapping[str, Any]) -> str | None:
    if not value:
        return None
    return say(
        en="I can't feature one hero clip on this video yet, so every clip is cut the same way",
        tr=(
            "Bu videoda tek bir ana klibi öne çıkaramıyorum, "
            "bu yüzden bütün klipler aynı şekilde kesiliyor"
        ),
    )


def _overlay(value: Any, _s: Mapping[str, Any]) -> str | None:
    if not value:
        return None
    return say(
        en="I can't use the planned overlay display on this video yet, so it won't be shown",
        tr="Planlanan katman gösterimini bu videoda henüz kullanamıyorum, gösterilmeyecek",
    )


def _sfx(value: Any, _s: Mapping[str, Any]) -> str | None:
    if not value:
        return None
    return say(
        en="I can't add sound effects on this video yet, so none will be played",
        tr="Bu videoya henüz ses efekti ekleyemiyorum, hiçbiri çalmayacak",
    )


_BUILDERS: dict[str, Callable[[Any, Mapping[str, Any]], str | None]] = {
    "intro_hook": _hook,
    "story_structure": _story,
    "pacing": _pacing,
    "direction": _direction,
    "caption_style": _caption_style,
    "optional_treatments": _treatments,
    "archetype": _archetype,
    "hero_media_id": _hero,
    "overlay_display": _overlay,
    "licensed_sfx": _sfx,
}
FIELD_HANDLING: dict[str, Handling] = {
    field: NotApplied(build) for field, build in _BUILDERS.items()
}


def story_verdict(
    strategy: Mapping[str, Any],
    requirements: Sequence[BriefRequirement] | None,
    media: Mapping[str, Any] | None,
) -> tuple[str, str | None] | None:
    from app.services.length_policy import story_direction  # noqa: PLC0415

    direction = story_direction(strategy)
    if direction is None:
        return None
    try:
        from app.kria.brief import CreativeBrief  # noqa: PLC0415
        from app.pipeline.story_direction import (  # noqa: PLC0415
            crossfade_blocker,
            story_blocker,
            story_lines,
            story_media_blocker,
        )
        from app.pipeline.unified_montage import brief_view  # noqa: PLC0415

        view = brief_view(CreativeBrief(version=1, requirements=list(requirements or [])))
        reason = (
            story_blocker(strategy, view)
            or (None if story_lines(strategy) else "no_story_lines")
            or story_media_blocker(strategy, media)
            or crossfade_blocker(strategy, direction)
        )
    except Exception:  # noqa: BLE001 - a disclosure must never fail a draft
        return None
    return direction, reason


def story_lines_notice(strategy: Mapping[str, Any]) -> str | None:
    items = [v for v in (strategy.get("story_structure") or []) if str(v or "").strip()]
    if not items:
        return None
    return say(
        en=(
            f"The planned story structure ({_list(items)}) sets the chapters "
            "(which clips are grouped together), but it won't reorder your clips: they stay in "
            "the order you gave"
        ),
        tr=(
            f"Planlanan hikâye yapısı ({_list(items)}) bölümleri (hangi kliplerin birlikte "
            "gruplanacağını) belirliyor ama kliplerinin sırasını değiştirmeyecek: verdiğin sırada "
            "kalacaklar"
        ),
    )


_ANIMATION_DECLINE_EN = (
    "I can't animate on-screen text on this video yet, so the text shows without motion"
)
_ANIMATION_DECLINE_TR = (
    "Bu videoda ekrandaki yazıyı henüz canlandıramıyorum, o yüzden yazı hareketsiz görünecek"
)


def _asks_text_animation(req: BriefRequirement) -> bool:
    if req.kind not in {"text", "style"}:
        return False
    return bool(req.facts.get("animation"))


def _text_animation_consumed(req: BriefRequirement) -> bool:
    from app.kria.animation_ask import animation_ask, text_entrance_form  # noqa: PLC0415

    return (
        bool(settings.text_animation_ask_enabled)
        and animation_ask(req) is not None
        and text_entrance_form() is not None
    )


def _title_animation_consumed(req: BriefRequirement, strategy: Mapping[str, Any]) -> bool:
    from app.schemas.text_style_intent import normalize_title_animation  # noqa: PLC0415

    if req.scope != "title":
        return False
    return bool(
        normalize_title_animation(req.facts.get("animation"))
        or normalize_title_animation(strategy.get("title_animation"))
    )


ASK_DECLINES: tuple[AskDecline, ...] = (
    AskDecline(
        "text_animation",
        lambda req, strategy: (
            _asks_text_animation(req)
            and not _title_animation_consumed(req, strategy)
            and not _text_animation_consumed(req)
        ),
        lambda: say(en=_ANIMATION_DECLINE_EN, tr=_ANIMATION_DECLINE_TR),
    ),
)
