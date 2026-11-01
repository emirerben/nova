"""Phone Talking writer (`_run_phone_subtitled_job`): a speaker's own speech with captions.

Covers ``subtitled`` and self-narrated Voiceover items (one clip or several). Captions follow
the item's style toggle (``kria.runtime`` maps clean/editorial to sentences, kinetic/karaoke to
word by word); it draws the confirmed title and closing title and the Visuals display. Its title
rows (the title, text on a spoken word, the closing tag) enter with the planned title animation
and the whole-video or title animation asks (`phone_subtitled_title.talking_title_entrances`,
``TALKING_HEAD_ROUTE_FOLLOWS_PROMPT_ENABLED``).
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from app.config import settings
from app.kria.brief import BriefRequirement
from app.kria.route_capabilities import _lines as lines
from app.kria.route_capabilities._types import (
    Applied,
    AskDecline,
    Handling,
    NotApplied,
    Notice,
    RenderEntry,
    RouteReaders,
)

_COMPILE = "pipeline/phone_subtitled_plan.py:compile_phone_subtitled_plan"

FIELD_HANDLING: dict[str, Handling] = {
    "intro_hook": NotApplied(lines.hook),
    "story_structure": NotApplied(lines.story),
    "pacing": NotApplied(
        lines.pacing_line(
            lines.pace_not_followed, "the video follows your speech", "video konuşmanı izliyor"
        )
    ),
    "direction": NotApplied(
        lines.direction_line(
            "fast_montage", "the video follows your speech", "video konuşmanı izliyor"
        )
    ),
    "caption_style": NotApplied(lines.captions_always_on),
    "target_duration_s": NotApplied(
        lines.stated_length("its length follows your speech", "uzunluğu konuşmanı takip ediyor")
    ),
    "title_animation": Applied(via=_COMPILE),
    "label_position": NotApplied(lines.label_position),
    "optional_treatments": NotApplied(lines.treatments),
    "archetype": NotApplied(lines.archetype),
    "hero_media_id": NotApplied(lines.hero),
    "licensed_sfx": NotApplied(lines.sfx),
    "edit_format": Applied(via=lines.APPROVAL),
    "opening_title": Applied(),
    "opening_title_duration_s": Applied(),
    "closing_title": Applied(),
    # The full-screen choice reaches the writer as a flat job key (KRI-297).
    "overlay_display": Applied(via=lines.OVERLAY_CANDIDATE),
    # Every phone writer styles its text through the shared pass (KRI-1024).
    "font_family": Applied(via=lines.TEXT_STYLE),
    "text_color": Applied(via=lines.TEXT_STYLE),
    "audio_strategy": Applied(via=lines.CONTRACT),
    "media_scope": Applied(via=lines.SELECTION),
    "selected_media_ids": Applied(via=lines.SELECTION),
}

READERS = RouteReaders(
    files=(
        "pipeline/phone_subtitled_plan.py",
        "pipeline/phone_subtitled_lanes.py",
        "pipeline/phone_subtitled_title.py",
    ),
    functions=("_run_phone_subtitled_job",),
)

RENDER_ENTRY = RenderEntry(
    _COMPILE,
    {
        "title_animation": "text_layers[id^=text-].animation_phases.entrance",
        "length_s": "duration",
    },
)


def _entrances_drawn() -> bool:
    """The title rows play an asked entrance: the lane switch is on and the phone plays one."""
    from app.kria.animation_ask import text_entrance_form  # noqa: PLC0415

    return bool(settings.talking_head_route_follows_prompt_enabled) and (
        text_entrance_form() is not None
    )


def _title_animation_not_drawn(strategy: Mapping[str, Any]) -> str | None:
    """With the lane switch off, or on a phone that plays no text entrance, the title rows
    show without the planned animation: the card says so, as before the lane."""
    if _entrances_drawn():
        return None
    return lines.title_animation(strategy.get("title_animation"), strategy)


def _ask_drawn(req: BriefRequirement) -> bool:
    """A whole-video or title animation ask the title rows play; this route draws no per-clip
    labels, so a per-clip or clip ask stays declined."""
    from app.kria.animation_ask import animation_ask  # noqa: PLC0415

    ask = animation_ask(req, styles=True)
    return (
        ask is not None
        and ask.scope != "per_clip"
        and not ask.scope.startswith("clip:")
        and _entrances_drawn()
    )


NOTICES: dict[str, Notice] = {"title_animation": _title_animation_not_drawn}

ASK_DECLINES: tuple[AskDecline, ...] = (
    AskDecline(
        "text_animation",
        lambda req, _s: lines.asks_text_animation(req) and not _ask_drawn(req),
        lines.animation_decline,
    ),
)
