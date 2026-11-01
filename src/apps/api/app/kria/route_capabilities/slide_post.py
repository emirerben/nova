"""Slide post (`_run_slide_post_job`, plans/024): ordered photos and videos, no cuts.

Its source of truth is the item's slide draft, not the plan: every video-only strategy field
is left unused, and video slides keep their own sound with nothing added.
"""

from __future__ import annotations

from pathlib import Path

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
            lambda _value, _s: False,
            "a slide post shows each slide for its own time",
            "slayt gönderisi her slaytı kendi süresince gösteriyor",
        )
    ),
    "direction": NotApplied(
        lines.direction_line(
            None,
            "a slide post shows your slides in order",
            "slayt gönderisi slaytlarını sırayla gösteriyor",
        )
    ),
    "caption_style": NotApplied(lines.caption_style),
    "audio_strategy": NotApplied(lines.slide_audio),
    "target_duration_s": NotApplied(
        lines.stated_length(
            "a slide post's length follows its slides",
            "slayt gönderisinin uzunluğu slaytlarına bağlı",
        )
    ),
    "opening_title": NotApplied(lines.opening_title),
    "closing_title": NotApplied(lines.closing_title),
    "shot_labels": NotApplied(lines.shot_labels),
    "title_animation": NotApplied(lines.title_animation),
    "label_position": NotApplied(lines.label_position),
    "optional_treatments": NotApplied(lines.treatments),
    "archetype": NotApplied(lines.archetype),
    "hero_media_id": NotApplied(lines.hero),
    "overlay_display": NotApplied(lines.overlay),
    "licensed_sfx": NotApplied(lines.sfx),
    "edit_format": Applied(via="dispatch renders the slide post from it"),
}

_SLIDE_PIPELINE = Path(__file__).resolve().parents[2] / "pipeline" / "slide_post"

READERS = RouteReaders(
    files=(
        *sorted(f"pipeline/slide_post/{p.name}" for p in _SLIDE_PIPELINE.glob("*.py")),
        "services/slide_post_compose.py",
    ),
    functions=("_run_slide_post_job",),
)

ASK_DECLINES: tuple[AskDecline, ...] = (
    AskDecline(
        "text_animation",
        lambda req, _s: lines.asks_text_animation(req),
        lines.animation_decline,
    ),
    AskDecline("captions", lambda req, _s: lines.asks_captions_on(req), lines.captions_decline),
)
