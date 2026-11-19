"""Slide post (`_run_slide_post_job`, plans/024): ordered photos and videos, no cuts.

Its source of truth is the item's slide draft, not the plan: every video-only strategy field
is left unused, and video slides keep their own sound with nothing added.

No planner draft reaches this table today: `creator_capabilities._format_availability` offers
no ``slides``, so the Main Creator's slides strategy is refused at its parse
(``policy_edit_format_unavailable``) and slide posts are composed on the Plan page. The lines
are the backstop for when slides become a strategy format (agents/DECISIONS.md "Prompt
fidelity wave 2: slide").
"""

from __future__ import annotations

from pathlib import Path

from app.kria.route_capabilities import _lines as lines
from app.kria.route_capabilities._lines import direction_line, pacing_line, stated_length
from app.kria.route_capabilities._types import (
    EDITORIAL_PLAN_NOT_RUN,
    KNOWN_GAP,
    Applied,
    AskDecline,
    Handling,
    NotApplied,
    RenderEntry,
    RouteReaders,
)

FIELD_HANDLING: dict[str, Handling] = {
    "intro_hook": NotApplied(lines.hook),
    "story_structure": NotApplied(lines.story),
    "pacing": NotApplied(
        pacing_line(
            lambda _value, _s: False,
            "a slide post shows each slide for its own time",
            "slayt gönderisi her slaytı kendi süresince gösteriyor",
        )
    ),
    "direction": NotApplied(
        direction_line(
            None,
            "a slide post shows your slides in order",
            "slayt gönderisi slaytlarını sırayla gösteriyor",
        )
    ),
    "caption_style": NotApplied(lines.caption_style),
    "audio_strategy": NotApplied(lines.slide_audio),
    "target_duration_s": NotApplied(
        stated_length(
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
    "edit_format": Applied(via=lines.APPROVAL),
}

_SLIDE_PIPELINE = Path(__file__).resolve().parents[2] / "pipeline" / "slide_post"

READERS = RouteReaders(
    files=(
        *sorted(f"pipeline/slide_post/{p.name}" for p in _SLIDE_PIPELINE.glob("*.py")),
        "services/slide_post_compose.py",
    ),
    functions=("_run_slide_post_job", "_build_slide_post_result"),
)

RENDER_ENTRY = RenderEntry("tasks/generative_build.py:_build_slide_post_result", {})

ASK_DECLINES: tuple[AskDecline, ...] = (
    AskDecline(
        "text_animation",
        lambda req, _s: lines.asks_text_animation(req),
        lines.animation_decline,
    ),
    AskDecline("captions", lambda req, _s: lines.asks_captions_on(req), lines.captions_decline),
)

# Planner-settable fields this route's code never reads and its table does not mention: today a
# value the planner sets here is dropped without a word (KRI-1429). Kept equal to the code scan by
# `tests/kria/test_route_field_coverage.py`; delete an entry when the field is read or listed.
UNREAD_DECLARED: dict[str, str] = {
    "editorial_plan": EDITORIAL_PLAN_NOT_RUN,
    "execution_contract": KNOWN_GAP,
    "font_family": KNOWN_GAP,
    "image_layout": KNOWN_GAP,
    "media_scope": KNOWN_GAP,
    "mixed_media_timing": KNOWN_GAP,
    "montage_audio": KNOWN_GAP,
    "montage_cadence": KNOWN_GAP,
    "opening_title_duration_s": KNOWN_GAP,
    "render_program": KNOWN_GAP,
    "selected_media_ids": KNOWN_GAP,
    "split_screen": KNOWN_GAP,
    "text_color": KNOWN_GAP,
    "video_reuse_policy": KNOWN_GAP,
}
