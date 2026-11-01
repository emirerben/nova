"""Phone voiceover montage (`_run_phone_voiceover_montage_job`): a montage under a recorded voice.

It reads the title, font, color, a fast or relaxed pace and the reuse policy; the intro text is
written by its own agent (``text_mode="agent_text"``), never from the plan's hook.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from app.config import settings
from app.kria.reply_language import say
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
    # `creator_pacing` keeps only "fast" and "relaxed"; anything else plays the preset's cuts.
    "pacing": NotApplied(
        lines.pacing_line(
            lambda value, _s: value in {"fast", "relaxed"},
            "the cuts keep their usual length",
            "kesimler her zamanki uzunluğunda",
        )
    ),
    "direction": NotApplied(
        lines.direction_line(
            "fast_montage",
            "it is cut as a montage under your voiceover",
            "seslendirmenin altında bir montaj olarak kesiliyor",
        )
    ),
    "caption_style": NotApplied(lines.caption_style),
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
    "font_family": Applied(),
    "text_color": Applied(),
    "video_reuse_policy": Applied(),
    "audio_strategy": Applied(via=lines.CONTRACT),
    "media_scope": Applied(via=lines.SELECTION),
    "selected_media_ids": Applied(via=lines.SELECTION),
}

READERS = RouteReaders(
    files=("pipeline/phone_voiceover_montage_plan.py",),
    functions=("_run_phone_voiceover_montage_job",),
)

ASK_DECLINES: tuple[AskDecline, ...] = (
    # The intro is drawn with ``cluster_style=None``: no entrance on any text.
    AskDecline(
        "text_animation",
        lambda req, _s: lines.asks_text_animation(req),
        lines.animation_decline,
    ),
    AskDecline("captions", lambda req, _s: lines.asks_captions_on(req), lines.captions_decline),
)


def _music_bed(_strategy: Mapping[str, Any]) -> str | None:
    """A matched track is dropped before compile when the phone has not verified ``musicBed``."""
    if "musicBed" in settings.phone_render_verified_features:
        return None
    return say(
        en="Music under your voiceover isn't available on this iPhone yet, so the video plays "
        "your voice without music",
        tr="Seslendirmenin altına müzik bu iPhone'da henüz eklenemiyor, o yüzden video müziksiz, "
        "sadece sesinle oynayacak",
    )


NOTICES = {"music_bed": _music_bed}
