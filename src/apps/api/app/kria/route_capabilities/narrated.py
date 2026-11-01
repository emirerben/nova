"""Phone Voiceover writer (`_run_phone_narrated_job`): footage cut to a recorded voiceover.

Captions follow the item's style toggle (``kria.runtime``); it burns the confirmed title, styled
by the shared phone text pass, and keeps the clips' own sound the plan names (``montage_audio``).
"""

from __future__ import annotations

from app.kria.route_capabilities import _lines as lines
from app.kria.route_capabilities._types import (
    Applied,
    AskDecline,
    Handling,
    NotApplied,
    RenderEntry,
    RouteReaders,
)

FIELD_HANDLING: dict[str, Handling] = {
    "intro_hook": NotApplied(lines.hook),
    # The cuts follow the voiceover's alignment, never the planned story order.
    "story_structure": NotApplied(lines.story),
    "pacing": NotApplied(
        lines.pacing_line(
            lines.pace_not_followed,
            "the cuts follow your voiceover",
            "kesimler seslendirmeni izliyor",
        )
    ),
    "direction": NotApplied(
        lines.direction_line(
            "fast_montage",
            "the video follows your voiceover",
            "video seslendirmeni izliyor",
        )
    ),
    "caption_style": NotApplied(lines.captions_always_on),
    "target_duration_s": NotApplied(
        lines.stated_length(
            "its length follows your voiceover", "uzunluğu seslendirmeni takip ediyor"
        )
    ),
    "title_animation": NotApplied(lines.title_animation),
    "label_position": NotApplied(lines.label_position),
    "optional_treatments": NotApplied(lines.treatments),
    "archetype": NotApplied(lines.archetype),
    "hero_media_id": NotApplied(lines.hero),
    "overlay_display": NotApplied(lines.overlay),
    "licensed_sfx": NotApplied(lines.sfx),
    "edit_format": Applied(via=lines.APPROVAL),
    "opening_title": Applied(),
    # Every phone writer styles its text through the shared pass (KRI-1024).
    "font_family": Applied(via=lines.TEXT_STYLE),
    "text_color": Applied(via=lines.TEXT_STYLE),
    "montage_audio": Applied(),
    "audio_strategy": Applied(via=lines.CONTRACT),
    "media_scope": Applied(via=lines.SELECTION),
    "selected_media_ids": Applied(via=lines.SELECTION),
}

READERS = RouteReaders(
    files=("pipeline/phone_narrated_plan.py", "pipeline/narrated_title.py"),
    functions=("_run_phone_narrated_job",),
)

RENDER_ENTRY = RenderEntry(
    "pipeline/phone_narrated_plan.py:compile_phone_narrated_plan",
    {
        "title_animation": "text_layers[id^=title-].animation_phases.entrance",
        "length_s": "duration",
    },
)

ASK_DECLINES: tuple[AskDecline, ...] = (
    AskDecline(
        "text_animation",
        lambda req, _s: lines.asks_text_animation(req),
        lines.animation_decline,
    ),
)
