"""Phone Talking writer (`_run_phone_subtitled_job`): a speaker's own speech with captions.

Covers ``subtitled`` and self-narrated Voiceover items (one clip or several). Captions follow
the item's style toggle (``kria.runtime`` maps clean/editorial to sentences, kinetic/karaoke to
word by word); it draws the confirmed title and closing title and the Visuals display.
"""

from __future__ import annotations

from app.kria.route_capabilities import _lines as lines
from app.kria.route_capabilities._types import (
    Applied,
    AskDecline,
    Handling,
    NotApplied,
    RouteReaders,
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
    "title_animation": NotApplied(lines.title_animation),
    "label_position": NotApplied(lines.label_position),
    "optional_treatments": NotApplied(lines.treatments),
    "archetype": NotApplied(lines.archetype),
    "hero_media_id": NotApplied(lines.hero),
    "licensed_sfx": NotApplied(lines.sfx),
    "edit_format": Applied(via=lines.DISPATCH),
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

ASK_DECLINES: tuple[AskDecline, ...] = (
    AskDecline(
        "text_animation",
        lambda req, _s: lines.asks_text_animation(req),
        lines.animation_decline,
    ),
)
