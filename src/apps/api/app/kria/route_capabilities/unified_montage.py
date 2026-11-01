"""Phone unified montage (`pipeline/unified_montage.py`): what it reads of a plan and the asks.

The route stamps ``direction="fast_montage"`` and ``pace="fast"`` on every plan it compiles,
unless it builds a story direction (`pipeline.story_direction`) or the pace unit holds cuts as
the plan's pace says (`PACE_DRIVES_CUTS_ENABLED`).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from app.config import settings
from app.kria.brief import BriefRequirement
from app.kria.reply_language import say
from app.kria.route_capabilities import _lines as lines
from app.kria.route_capabilities._types import (
    Applied,
    AskDecline,
    Handling,
    NotApplied,
    RouteReaders,
)

ROUTE_DIRECTION = "fast_montage"
ROUTE_PACE = "fast"
CREATOR_SONG = "creator_song"


def _pace_applied(value: Any, strategy: Mapping[str, Any]) -> bool:
    if value == ROUTE_PACE:
        return True
    from app.services.length_policy import effective_pace  # noqa: PLC0415

    # The pace unit applies it: the route holds each clip as long as this pace says
    # (`PACE_DRIVES_CUTS_ENABLED`). Only a silent "balanced" default is still not applied.
    return effective_pace(strategy) is not None


def _caption_style(value: Any, strategy: Mapping[str, Any]) -> str | None:
    if strategy.get("audio_strategy") == "user_song":
        return lines.song_caption_style(value, strategy)
    return lines.caption_style(value, strategy)


_PLAN = "pipeline/unified_montage.py:plan_unified_montage"

# Source of truth for "unread": `scripts/kria/score_route.py` (consumer gate) scans ``READERS``
# for reads of every CreativeStrategy field. The entries that disclose come first, in card order.
FIELD_HANDLING: dict[str, Handling] = {
    "intro_hook": NotApplied(lines.hook),
    "story_structure": NotApplied(lines.story),
    "pacing": NotApplied(
        lines.pacing_line(
            _pace_applied,
            "the cuts follow a fast pace",
            "kesimler hızlı tempoda",
        )
    ),
    "direction": NotApplied(
        lines.direction_line(
            ROUTE_DIRECTION,
            "it is cut as a fast montage",
            "hızlı bir montaj olarak kesiliyor",
        )
    ),
    "caption_style": NotApplied(_caption_style),
    "optional_treatments": NotApplied(lines.treatments),
    "archetype": NotApplied(lines.archetype),
    "hero_media_id": NotApplied(lines.hero),
    "overlay_display": NotApplied(lines.overlay),
    "licensed_sfx": NotApplied(lines.sfx),
    "editorial_plan": Applied(),
    "title_animation": Applied(),
    "label_position": Applied(),
    "image_layout": Applied(),
    "mixed_media_timing": Applied(),
    "montage_cadence": Applied(),
    # Read outside the route's files (the dispatch, the contract) or through a helper the
    # read scan does not see: each names the symbol that reads it, which the gate checks.
    "edit_format": Applied(via=lines.DISPATCH),
    "render_program": Applied(via="agents/_schemas/creator_policy.py:effective_render_program"),
    "audio_strategy": Applied(via=lines.CONTRACT),
    "media_scope": Applied(via="pipeline/unified_montage.py:selected_visual_ids"),
    "selected_media_ids": Applied(via="pipeline/unified_montage.py:selected_visual_ids"),
    # Gated by creator proof (`length_policy`): partial by design.
    "target_duration_s": Applied(via=_PLAN),
    "font_family": Applied(via=_PLAN),
    "text_color": Applied(via=_PLAN),
    "opening_title": Applied(via="pipeline/unified_montage.py:_title"),
    "opening_title_duration_s": Applied(via=_PLAN),
    "closing_title": Applied(via=_PLAN),
    "shot_labels": Applied(via=_PLAN),
    "video_reuse_policy": Applied(via=lines.DISPATCH),
    "montage_audio": Applied(via="pipeline/unified_montage.py:_keeps_source_audio"),
    "execution_contract": Applied(via="agents/_schemas/creator_policy.py:effective_render_program"),
}

READERS = RouteReaders(
    files=(
        "pipeline/unified_montage.py",
        "pipeline/best_parts.py",
        "pipeline/story_direction.py",
        "services/length_policy.py",
        "services/length_planning.py",
        "kria/duplicate_uploads.py",
        "services/phone_montage_preflight.py",
        "pipeline/phone_plan_repair.py",
    ),
    functions=(
        "_plan_phone_unified_montage",
        "_run_phone_unified_montage_job",
        "_creator_selected_clip_paths",
    ),
)


# --- the story verdict ------------------------------------------------------------------------


def story_verdict(
    strategy: Mapping[str, Any],
    requirements: Sequence[BriefRequirement] | None,
    media: Mapping[str, Any] | None,
) -> tuple[str, str | None] | None:
    """``(direction, reason)`` for a planner direction the route builds as a story, else None.

    ``reason`` None means the story will be built (the route reads ``direction``,
    ``story_structure`` and ``pacing``); a reason means the route keeps the fast montage and
    says why. The same pure rules the render applies (`pipeline.story_direction`), over what
    the draft knows before approval: the strategy, the live asks and the pinned length plan.
    A creator song sets the cut points (the render's `_plan_blocker`), so it is checked first.
    Fail-open: any trouble returns None and the generic lines stand.
    """
    from app.services.length_policy import story_direction  # noqa: PLC0415

    direction = story_direction(strategy)
    if direction is None:
        return None
    try:
        from app.kria.brief import CreativeBrief  # noqa: PLC0415
        from app.pipeline.story_direction import (  # noqa: PLC0415
            crossfade_blocker,
            story_blocker,
            story_lines,
            story_media_blocker,
        )
        from app.pipeline.unified_montage import brief_view  # noqa: PLC0415

        view = brief_view(CreativeBrief(version=1, requirements=list(requirements or [])))
        song = strategy.get("audio_strategy") == "user_song"
        reason = (
            (CREATOR_SONG if song else None)
            or story_blocker(strategy, view)
            or (None if story_lines(strategy) else "no_story_lines")
            or story_media_blocker(strategy, media)
            or crossfade_blocker(strategy, direction)
        )
    except Exception:  # noqa: BLE001 - a disclosure must never fail a draft
        return None
    return direction, reason


def story_lines_notice(strategy: Mapping[str, Any]) -> str | None:
    """The outline sets the chapters (clip grouping), but the clips keep their order."""
    items = [v for v in (strategy.get("story_structure") or []) if str(v or "").strip()]
    if not items:
        return None
    return say(
        en=(
            f"The planned story structure ({lines.listed(items)}) sets the chapters "
            "(which clips are grouped together), but it won't reorder your clips: they stay in "
            "the order you gave"
        ),
        tr=(
            f"Planlanan hikâye yapısı ({lines.listed(items)}) bölümleri (hangi kliplerin birlikte "
            "gruplanacağını) belirliyor ama kliplerinin sırasını değiştirmeyecek: verdiğin sırada "
            "kalacaklar"
        ),
    )


# --- asks -------------------------------------------------------------------------------------


def _text_animation_consumed(req: BriefRequirement) -> bool:
    """The creation text pass plays this ask (prompt fidelity, animation unit).

    Only a ``text`` ask the phone can play an entrance for: when the phone has not verified any
    text-entrance form (``text_entrance_form`` is None) nothing moves, so the decline stays and
    says so. A ``style`` ask is not read by that pass."""
    from app.kria.animation_ask import animation_ask, text_entrance_form  # noqa: PLC0415

    return (
        bool(settings.text_animation_ask_enabled)
        and animation_ask(req) is not None
        and text_entrance_form() is not None
    )


def _title_animation_consumed(req: BriefRequirement, strategy: Mapping[str, Any]) -> bool:
    """The route animates exactly one text: the opening title, from a typed id
    (`unified_montage.brief_view` reads ``facts.animation`` for ``scope == "title"``; the
    strategy's ``title_animation`` is the other typed source)."""
    from app.schemas.text_style_intent import normalize_title_animation  # noqa: PLC0415

    if req.scope != "title":
        return False
    return bool(
        normalize_title_animation(req.facts.get("animation"))
        or normalize_title_animation(strategy.get("title_animation"))
    )


TEXT_ANIMATION = AskDecline(
    "text_animation",
    lambda req, strategy: (
        lines.asks_text_animation(req)
        and not _title_animation_consumed(req, strategy)
        and not _text_animation_consumed(req)
    ),
    lines.animation_decline,
)
# The route's only captions are a creator song's lyrics (KRI-569).
CAPTIONS_ON = AskDecline(
    "captions",
    lambda req, strategy: (
        lines.asks_captions_on(req) and strategy.get("audio_strategy") != "user_song"
    ),
    lines.captions_decline,
)
TITLE_POSITION = AskDecline(
    "title_position",
    lambda req, _strategy: lines.asks_title_position(req),
    lines.title_position_decline,
)

ASK_DECLINES: tuple[AskDecline, ...] = (TEXT_ANIMATION, CAPTIONS_ON, TITLE_POSITION)


# --- route limits no single field owns --------------------------------------------------------


def _crossfades(strategy: Mapping[str, Any]) -> str | None:
    """A photo/video timing profile always cuts as the fast montage (story blocker
    ``photo_timing``), and that layout hard-cuts: its crossfade boundary is never drawn."""
    timing = strategy.get("mixed_media_timing")
    boundary = timing.get("boundary_style") if isinstance(timing, Mapping) else None
    if boundary != "crossfade":
        return None
    return say(
        en="I can't crossfade between clips on this video yet, so each clip cuts straight to "
        "the next",
        tr="Bu videoda klipler arasında henüz yumuşak geçiş yapamıyorum, o yüzden her klip bir "
        "sonrakine doğrudan kesiliyor",
    )


NOTICES = {"transitions": _crossfades}
