"""Creator-song lip-sync montage (`pipeline/lipsync_montage.py`, KRI-374).

Takes are placed where the song says them; the plan's pace is recorded but the song sets where
the cuts fall, and a story direction is always cut as the fast montage (``creator_song``).
Captions are the song's lyrics, pinned at plan time from the caption style (``song_captions``).
With ``LIPSYNC_ROUTE_FOLLOWS_PROMPT_ENABLED`` the opening and closing titles enter the way the
plan or a typed title or all-text animation ask says (`lipsync_montage._title_entrance`), when
the plan has a title and the phone can play an entrance; otherwise the card still says the title
stays static, as it did before the switch.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from app.config import settings
from app.kria.brief import BriefRequirement
from app.kria.route_capabilities import _lines as lines
from app.kria.route_capabilities._types import (
    Applied,
    AskDecline,
    Handling,
    NotApplied,
    RecordedOnly,
    RenderEntry,
    RouteReaders,
    WithAsks,
)

CREATOR_SONG = "creator_song"


def _titles_enter(strategy: Mapping[str, Any], requirements: Sequence[Any]) -> bool:
    """The planner will give this plan's titles an entrance
    (`lipsync_montage.planned_title_entrance`). Any doubt reads as no: the card keeps its line."""
    from app.pipeline.lipsync_montage import planned_title_entrance  # noqa: PLC0415

    try:
        return planned_title_entrance(strategy, requirements) is not None
    except Exception:  # noqa: BLE001 - a disclosure is never dropped on an error
        return False


def _title_animation(
    value: Any, strategy: Mapping[str, Any], requirements: Sequence[BriefRequirement]
) -> str | None:
    """The plan's title animation, unless the titles play an entrance. With the lane switch off,
    with no opening or closing title, or on a phone with no verified text-entrance form, both
    titles stay static and the card says so, word for word as before the lane (the unified
    montage's pacing line follows the same "not applied unless it is" rule)."""
    if value and _titles_enter(strategy, requirements):
        return None
    return lines.title_animation(value, strategy)


FIELD_HANDLING: dict[str, Handling] = {
    "intro_hook": NotApplied(lines.hook),
    "story_structure": NotApplied(lines.story),
    "pacing": RecordedOnly(
        lines.pacing_line(
            lambda _value, _s: False,
            "your song sets where the cuts fall",
            "kesimlerin yerini şarkın belirliyor",
        )
    ),
    "direction": NotApplied(
        lines.direction_line("fast_montage", "it is cut to your song", "şarkına göre kesiliyor")
    ),
    "caption_style": NotApplied(lines.song_caption_style),
    "target_duration_s": NotApplied(
        lines.stated_length("its length follows your song", "uzunluğu şarkını takip ediyor")
    ),
    "title_animation": NotApplied(WithAsks(_title_animation)),
    "label_position": NotApplied(lines.label_position),
    "optional_treatments": NotApplied(lines.treatments),
    "archetype": NotApplied(lines.archetype),
    "hero_media_id": NotApplied(lines.hero),
    "overlay_display": NotApplied(lines.overlay),
    "licensed_sfx": NotApplied(lines.sfx),
    "opening_title": Applied(via=lines.TITLE),
    "opening_title_duration_s": Applied(),
    "closing_title": Applied(),
    "font_family": Applied(),
    "text_color": Applied(),
    "edit_format": Applied(via=lines.APPROVAL),
    "audio_strategy": Applied(via=lines.CONTRACT),
    "media_scope": Applied(via=lines.SELECTION),
    "selected_media_ids": Applied(via=lines.SELECTION),
}

READERS = RouteReaders(
    files=("pipeline/lipsync_montage.py", "services/song_order.py"),
    functions=("_plan_phone_user_song_core",),
)

RENDER_ENTRY = RenderEntry(
    "pipeline/lipsync_montage.py:plan_lipsync_montage",
    {
        "title_animation": "snapshot.title_animation",
        "pace": "snapshot.pace",
        "length_s": "duration_s",
    },
)


def _animation_not_played(req: BriefRequirement, strategy: Mapping[str, Any]) -> bool:
    """A typed animation ask the titles do not play: every one with the lane switch off; with
    it on, one on the clip labels or on one clip (this route draws no labels), one whose value
    names no animation ("none"), and one with no title to play on or on a phone with no verified
    text-entrance form (as the unified montage keeps its decline when nothing moves). An ask on
    the title or on all the text otherwise reaches both titles (`lipsync_montage._title_entrance`).

    A decline sees its own ask only, so a title written in another ask alone counts as no title
    here: the card can then say less than the render does, never more."""
    if not lines.asks_text_animation(req):
        return False
    if not settings.lipsync_route_follows_prompt_enabled:
        return True
    from app.kria.animation_ask import animation_ask  # noqa: PLC0415

    ask = animation_ask(req, styles=True)
    if ask is None or ask.scope == "per_clip" or ask.scope.startswith("clip:"):
        return True
    return not _titles_enter(strategy, [req])


ASK_DECLINES: tuple[AskDecline, ...] = (
    AskDecline("text_animation", _animation_not_played, lines.animation_decline),
)


def story_verdict(
    strategy: Mapping[str, Any],
    _requirements: Sequence[BriefRequirement] | None,
    _media: Mapping[str, Any] | None,
) -> tuple[str, str | None] | None:
    """A story direction on a creator song is cut as the fast montage: the song sets the cut
    points (`story_direction._plan_blocker` says ``creator_song`` for every song plan)."""
    from app.services.length_policy import story_direction  # noqa: PLC0415

    direction = story_direction(strategy)
    return None if direction is None else (direction, CREATOR_SONG)
