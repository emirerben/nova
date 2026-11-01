"""Phone Talking writer (`_run_phone_subtitled_job`): a speaker's own speech with captions.

Covers ``subtitled`` and self-narrated Voiceover items (one clip or several). Captions follow
the item's style toggle (``kria.runtime`` maps clean/editorial to sentences, kinetic/karaoke to
word by word); it draws the confirmed title and closing title and the Visuals display. Its title
rows (the title, text on a spoken word, the closing tag) enter with the planned title animation
and the whole-video or title animation asks (`phone_subtitled_title.talking_title_entrances`,
``TALKING_HEAD_ROUTE_FOLLOWS_PROMPT_ENABLED``); the card keeps the title animation line and the
animation decline whenever no such row will play it.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from app.kria.brief import BriefRequirement
from app.kria.route_capabilities import _lines as lines
from app.kria.route_capabilities._types import (
    Applied,
    AskDecline,
    GivenAsks,
    Handling,
    NotApplied,
    RenderEntry,
    RouteReaders,
    WithAsks,
)


def _rows_animate(strategy: Mapping[str, Any], requirements: Sequence[BriefRequirement]) -> bool:
    """A title row the writer draws for this plan and these asks plays the planned or asked
    entrance (`phone_subtitled_title.talking_title_rows_animate`). When that cannot be read the
    card keeps its line and its decline."""
    from app.pipeline.phone_subtitled_title import talking_title_rows_animate  # noqa: PLC0415

    try:
        return talking_title_rows_animate(strategy, requirements)
    except Exception:  # noqa: BLE001 - a disclosure must never fail a draft
        return False


def _title_animation(
    value: Any, strategy: Mapping[str, Any], requirements: Sequence[BriefRequirement]
) -> str | None:
    """No line when a title row the writer draws enters with the planned animation (or a named
    one the creator asked for); else the line from before the lane: the switch off, a phone that
    plays no text entrance, or no title, closing text or text on a spoken word to animate."""
    if value and _rows_animate(strategy, requirements):
        return None
    return lines.title_animation(value, strategy)


def _animation_not_played(
    req: BriefRequirement, strategy: Mapping[str, Any], requirements: Sequence[BriefRequirement]
) -> bool:
    """A typed text animation ask no title row plays: a per-clip or one-clip ask (this route
    draws no clip labels), or no title row to play it on (`_rows_animate`)."""
    if not lines.asks_text_animation(req):
        return False
    from app.kria.animation_ask import animation_ask  # noqa: PLC0415

    ask = animation_ask(req, styles=True)
    return (
        ask is None
        or ask.scope == "per_clip"
        or ask.scope.startswith("clip:")
        or not _rows_animate(strategy, requirements)
    )


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
    "title_animation": NotApplied(WithAsks(_title_animation)),
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
    "pipeline/phone_subtitled_plan.py:compile_phone_subtitled_plan",
    {
        "title_animation": "text_layers[id^=text-].animation_phases.entrance",
        "length_s": "duration",
    },
)

ASK_DECLINES: tuple[AskDecline, ...] = (
    AskDecline("text_animation", GivenAsks(_animation_not_played), lines.animation_decline),
)
