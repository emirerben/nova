"""Creator-song lip-sync montage (`pipeline/lipsync_montage.py`, KRI-374).

Takes are placed where the song says them; the plan's pace is recorded but the song sets where
the cuts fall, and a story direction is always cut as the fast montage (``creator_song``).
Captions are the song's lyrics, pinned at plan time from the caption style (``song_captions``).
With ``LIPSYNC_ROUTE_FOLLOWS_PROMPT_ENABLED`` the opening and closing titles enter the way the
plan or a typed title or all-text animation ask says (`lipsync_montage._title_entrance`).
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
)

CREATOR_SONG = "creator_song"
_PLAN = "pipeline/lipsync_montage.py:plan_lipsync_montage"

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
    "title_animation": Applied(via=_PLAN),
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


def _animation_not_played(req: BriefRequirement, _strategy: Mapping[str, Any]) -> bool:
    """A typed animation ask the titles do not play: every one with the lane switch off; with
    it on, one on the clip labels or on one clip (this route draws no labels), or one whose
    value names no animation ("none"), as before. An ask on the title or on all the text
    reaches both titles (`lipsync_montage._title_entrance`)."""
    if not lines.asks_text_animation(req):
        return False
    if not settings.lipsync_route_follows_prompt_enabled:
        return True
    from app.kria.animation_ask import animation_ask  # noqa: PLC0415

    ask = animation_ask(req, styles=True)
    return ask is None or ask.scope == "per_clip" or ask.scope.startswith("clip:")


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
