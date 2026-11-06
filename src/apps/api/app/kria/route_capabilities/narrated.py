"""Phone Voiceover writer (`_run_phone_narrated_job`): footage cut to a recorded voiceover.

Captions follow the item's style toggle (``kria.runtime``); it burns the confirmed title, styled
by the shared phone text pass, and keeps the clips' own sound the plan names (``montage_audio``).
The opening title is the one text it animates: the plan's ``title_animation`` and a title or
whole-video animation ask become its entrance (`phone_narrated_plan.narrated_title_motion`,
``NARRATED_ROUTE_FOLLOWS_PROMPT_ENABLED``), the creator's asks first. The card says which of
them the one entrance leaves out. The cuts, the order and the length follow the voice.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, Any

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

if TYPE_CHECKING:
    from app.pipeline.phone_narrated_plan import NarratedTitleMotion


def _motion(strategy: Mapping[str, Any], requirements: Sequence[Any]) -> NarratedTitleMotion | None:
    """How the opening title enters (`phone_narrated_plan.narrated_title_motion`), or None: no
    confirmed title, the lane switch off or a phone that plays no text entrance."""
    title = strategy.get("opening_title")
    if not isinstance(title, str) or not title.split():
        return None
    from app.pipeline.phone_narrated_plan import narrated_title_motion  # noqa: PLC0415

    return narrated_title_motion(strategy, requirements)


def _title_animation(
    value: Any, strategy: Mapping[str, Any], requirements: Sequence[BriefRequirement]
) -> str | None:
    """No line when the title enters with the planned animation; when it does not, the line
    says why: the creator's own ask set the entrance, or the phone plays it as the fade."""
    from app.schemas.text_style_intent import normalize_title_animation  # noqa: PLC0415

    planned = normalize_title_animation(value)
    motion = _motion(strategy, requirements)
    if motion is None or planned is None:
        return lines.title_animation(value, strategy)
    if motion.edge == planned:
        return None
    if motion.wanted == planned:
        return lines.title_animation_as_fade(value)
    return lines.title_animation_from_ask(value)


def _ask_outcome(
    req: BriefRequirement, strategy: Mapping[str, Any], live: Sequence[BriefRequirement]
) -> str | None:
    """What the title does for this animation ask among the brief's live asks: None when it
    plays what the ask says; "taken" when another of the creator's asks set its one entrance;
    "fade" when the phone plays the asked edge as the fade; "unanimated" when no text moves for
    it (no title, a per-clip ask, the lane switch off: the decline every route gives)."""
    if not lines.asks_text_animation(req):
        return None
    from app.pipeline.phone_narrated_plan import title_motion_ask  # noqa: PLC0415

    motion = _motion(strategy, live)
    asked = title_motion_ask(req)
    if motion is None or asked is None:
        return "unanimated"
    if motion.meets(req):
        return None
    return "fade" if asked[1] == motion.wanted else "taken"


def _declines(outcome: str) -> GivenAsks:
    """The decline test for one `_ask_outcome`. Asks it cannot read get the decline every route
    gives: a disclosure must never fail a draft."""

    def test(
        req: BriefRequirement, strategy: Mapping[str, Any], live: Sequence[BriefRequirement]
    ) -> bool:
        try:
            found = _ask_outcome(req, strategy, live)
        except Exception:  # noqa: BLE001 - a disclosure must never fail a draft
            found = "unanimated" if lines.asks_text_animation(req) else None
        return found == outcome

    return GivenAsks(test)


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
    AskDecline("text_animation", _declines("taken"), lines.title_entrance_taken_decline),
    AskDecline("text_animation", _declines("fade"), lines.title_entrance_fade_decline),
    AskDecline("text_animation", _declines("unanimated"), lines.animation_decline),
)
