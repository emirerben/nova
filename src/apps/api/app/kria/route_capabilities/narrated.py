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
    RouteReaders,
)

_DISPATCH_SELECTION = "dispatch hands only the selected clips (_creator_selected_clip_paths)"
# Every phone writer styles its text through the shared pass (KRI-1024).
_TEXT_STYLE = "phone text style policy (requested_style)"

FIELD_HANDLING: dict[str, Handling] = {
    "intro_hook": NotApplied(lines.hook),
    "pacing": NotApplied(
        lines.pacing_line(
            lines.no_pace_chosen,
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
    "edit_format": Applied(),
    "opening_title": Applied(),
    "font_family": Applied(via=_TEXT_STYLE),
    "text_color": Applied(via=_TEXT_STYLE),
    "montage_audio": Applied(),
    "audio_strategy": Applied(via="dispatch picks this writer for a recorded voiceover"),
    "media_scope": Applied(via=_DISPATCH_SELECTION),
    "selected_media_ids": Applied(via=_DISPATCH_SELECTION),
}

READERS = RouteReaders(
    files=("pipeline/phone_narrated_plan.py", "pipeline/narrated_title.py"),
    functions=("_run_phone_narrated_job",),
)

ASK_DECLINES: tuple[AskDecline, ...] = (
    AskDecline(
        "text_animation",
        lambda req, _s: lines.asks_text_animation(req),
        lines.animation_decline,
    ),
)
