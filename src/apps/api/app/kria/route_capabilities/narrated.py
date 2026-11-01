"""Phone Voiceover writer (`_run_phone_narrated_job`): footage cut to a recorded voiceover.

Captions follow the item's style toggle (``kria.runtime``); it burns the confirmed title, styled
by the shared phone text pass, and keeps the clips' own sound the plan names (``montage_audio``).
The opening title is the one text it animates: the plan's ``title_animation`` and a title or
whole-video animation ask become its entrance (`phone_narrated_plan.narrated_title_edge`,
``NARRATED_ROUTE_FOLLOWS_PROMPT_ENABLED``). The cuts, the order and the length follow the voice.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from app.kria.brief import BriefRequirement
from app.kria.route_capabilities import _lines as lines
from app.kria.route_capabilities._types import (
    Applied,
    AskDecline,
    Handling,
    NotApplied,
    RenderEntry,
    RouteReaders,
    WithAsks,
)


def _title_edge(strategy: Mapping[str, Any], requirements: Sequence[Any]) -> str | None:
    """The entrance the opening title plays, or None: no confirmed title, nothing to animate."""
    title = strategy.get("opening_title")
    if not isinstance(title, str) or not title.split():
        return None
    from app.pipeline.phone_narrated_plan import narrated_title_edge  # noqa: PLC0415

    return narrated_title_edge(strategy, requirements)


def _title_animation(
    value: Any, strategy: Mapping[str, Any], requirements: Sequence[BriefRequirement]
) -> str | None:
    """No line when the title enters with the planned animation."""
    from app.schemas.text_style_intent import normalize_title_animation  # noqa: PLC0415

    edge = _title_edge(strategy, requirements)
    if edge is not None and edge == normalize_title_animation(value):
        return None
    return lines.title_animation(value, strategy)


def _title_takes(req: BriefRequirement, strategy: Mapping[str, Any]) -> bool:
    """The title enters as this title or whole-video animation ask asks (any entrance meets an
    ask that names none)."""
    from app.kria.animation_ask import animation_ask  # noqa: PLC0415

    ask = animation_ask(req, styles=True)
    if ask is None or ask.scope not in {"title", "global"}:
        return False
    edge = _title_edge(strategy, [req])
    return edge is not None and (not ask.named or ask.edge == edge)


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
    # The steps tile the whole recorded voice, so the voice sets the length.
    "target_duration_s": NotApplied(
        lines.stated_length(
            "its length follows your voiceover", "uzunluğu seslendirmeni takip ediyor"
        )
    ),
    "title_animation": NotApplied(WithAsks(_title_animation)),
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
        lambda req, strategy: lines.asks_text_animation(req) and not _title_takes(req, strategy),
        lines.animation_decline,
    ),
)
