"""Route capability registry: what each render route does with a plan and with the asks.

One module per route (``<route>.py``), each exporting:

* ``FIELD_HANDLING``: CreativeStrategy field -> ``Applied`` | ``RecordedOnly`` | ``NotApplied``.
  A ``NotApplied``/``RecordedOnly`` builder is the approval-card line for a value the route does
  not follow (None when the value is the route's own behaviour or empty).
* ``ASK_DECLINES``: typed brief asks the route cannot do (a route-decline receipt).
* ``READERS``: where the route's code lives, for the consumer gate
  (`scripts/kria/score_route.py`): a field marked ``Applied`` that no reader touches is flagged.
* optionally ``NOTICES`` (route limits no single field owns) and ``story_verdict``.

`resolve_route_id` picks the route for a draft from the predicates the worker's dispatch uses
(`tasks/generative_build.py` phone dispatch, `services.render_route.resolve_route`,
`brief_checks.defers_to_*`). `kria.route_disclosure` turns the table into card lines.
Owner rule: nothing here refuses a render, raises to the creator or blocks approval.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from enum import StrEnum
from types import ModuleType
from typing import Any

from app.kria.route_capabilities import (
    lipsync_song,
    narrated,
    slide_post,
    speech_excerpt,
    talking_head,
    unified_montage,
    voiceover_montage,
)
from app.kria.route_capabilities._types import (
    Applied,
    AskDecline,
    Handling,
    NotApplied,
    RecordedOnly,
    RouteReaders,
    registry_enabled,
)

__all__ = [
    "Applied",
    "AskDecline",
    "Handling",
    "NotApplied",
    "RecordedOnly",
    "RouteId",
    "RouteReaders",
    "registry_enabled",
    "resolve_route_id",
    "table_for",
]


class RouteId(StrEnum):
    UNIFIED_MONTAGE = "unified_montage"
    VOICEOVER_MONTAGE = "voiceover_montage"
    TALKING_HEAD = "talking_head"
    NARRATED = "narrated"
    LIPSYNC_SONG = "lipsync_song"
    SPEECH_EXCERPT = "speech_excerpt"
    SLIDE_POST = "slide_post"


ROUTE_TABLES: dict[RouteId, ModuleType] = {
    RouteId.UNIFIED_MONTAGE: unified_montage,
    RouteId.VOICEOVER_MONTAGE: voiceover_montage,
    RouteId.TALKING_HEAD: talking_head,
    RouteId.NARRATED: narrated,
    RouteId.LIPSYNC_SONG: lipsync_song,
    RouteId.SPEECH_EXCERPT: speech_excerpt,
    RouteId.SLIDE_POST: slide_post,
}


def table_for(route: object) -> ModuleType | None:
    """The route's table module, or None for a route the registry does not describe."""
    try:
        return ROUTE_TABLES[RouteId(str(route))]
    except ValueError:
        return None


def _montage_family_route(strategy: Mapping[str, Any]) -> RouteId | None:
    """Which phone montage writer takes a draft that leaves the voiceover lane.

    The worker's resolver (`render_route.resolve_route`) over the facts the approved contract
    will carry (`creator_render_contract.build_render_contract`: camera-audio sources from
    ``montage_audio`` while it keeps the source sound, ``require_voiceover`` from
    ``audio_strategy``). Its order is the dispatch's: spoken excerpts, then the creator song.
    One clip's voice straight through (``voice_behind_footage``) has no table yet: None.
    """
    from app.services.render_route import (  # noqa: PLC0415
        ContractFacts,
        Route,
        RouteInputs,
        plan_facts,
        resolve_route,
    )

    audio = strategy.get("montage_audio")
    keeps_sound = isinstance(audio, Mapping) and bool(audio.get("preserve_source_audio"))
    sources = [m for m in (audio.get("source_media_ids") or []) if m] if keeps_sound else []
    original_audio = (
        ("require" if keeps_sound else "forbid")
        if isinstance(audio, Mapping)
        else ("require" if strategy.get("audio_strategy") == "original_audio" else None)
    )
    # The plan's own song or voice counts as attached: without it the draft asks for the upload
    # before approval (`needs_choice`), which is not a route.
    resolution = resolve_route(
        RouteInputs(
            platform="phone",
            contract=ContractFacts(
                require_voiceover=strategy.get("audio_strategy") == "voiceover",
                has_audio_sources=bool(sources),
                audio_source_count=len(sources),
                original_audio=original_audio,
            ),
            edit_format=str(strategy.get("edit_format") or "montage"),
            plan=plan_facts(strategy),
            voiceover_present=strategy.get("audio_strategy") == "voiceover",
            song_present=strategy.get("audio_strategy") == "user_song",
        )
    )
    return {
        Route.UNIFIED_MONTAGE: RouteId.UNIFIED_MONTAGE,
        Route.USER_SONG_MONTAGE: RouteId.UNIFIED_MONTAGE,
        Route.LIPSYNC_MONTAGE: RouteId.LIPSYNC_SONG,
        Route.SPEECH_MONTAGE: RouteId.SPEECH_EXCERPT,
    }.get(resolution.route)


def resolve_route_id(
    *,
    creator_id: object,
    edit_format: object,
    strategy: Mapping[str, Any] | None,
    clip_paths: Iterable[object] = (),
) -> RouteId | None:
    """The route that will render this strategy draft, or None (a cloud route, or a shape the
    worker would refuse). Pure and fail-open: any trouble answers None (no lines)."""
    strategy = dict(strategy or {})
    fmt = str(strategy.get("edit_format") or edit_format or "")
    strategy["edit_format"] = fmt
    paths = [str(path) for path in clip_paths or ()]
    if fmt == "slides":
        return RouteId.SLIDE_POST  # dispatched before any clip check (`_run_slide_post_job`)
    try:
        from app.agents._schemas.edit_format import GUIDED_EDIT_FORMATS  # noqa: PLC0415
        from app.config import settings  # noqa: PLC0415
        from app.kria.brief_checks import (  # noqa: PLC0415
            defers_caption_words_to_phone_render,
            defers_text_to_phone,
            defers_to_narrated_render,
            defers_to_unified_montage,
        )

        audio = strategy.get("audio_strategy")
        common = {"creator_id": creator_id, "edit_format": fmt, "clip_paths": paths}
        if defers_to_unified_montage(audio_strategy=audio, **common):
            return _montage_family_route(strategy)
        if fmt in GUIDED_EDIT_FORMATS:
            # The voiceover lane: the dispatch gate refuses it unless the phone plays narration.
            on_phone = (
                audio == "voiceover"
                and defers_text_to_phone(creator_id=creator_id, clip_paths=paths)
                and settings.phone_narration_rendering_enabled
                and "narrationAudio" in settings.phone_render_verified_features
            )
            return RouteId.VOICEOVER_MONTAGE if on_phone else None
        if defers_to_narrated_render(audio_strategy=audio, **common):
            return RouteId.NARRATED
        if defers_caption_words_to_phone_render(audio_strategy=audio, **common):
            return RouteId.TALKING_HEAD
    except Exception:  # noqa: BLE001 - a disclosure must never fail a draft
        return None
    return None
