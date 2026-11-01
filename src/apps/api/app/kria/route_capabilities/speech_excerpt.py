"""Phone spoken-excerpt montage (`services/phone_speech_montage_job.py`, KRI-282).

Excerpts of what the clips say, cut to the speaker and back to fast cuts in between; the cut
length is the planner's ``cut_s`` or a fixed default. It plays the clips' own sound, draws the
confirmed texts from the render contract and refuses a licensed sound effect (KRI-608).
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from app.kria.reply_language import say
from app.kria.route_capabilities import _lines as lines
from app.kria.route_capabilities._types import (
    Applied,
    AskDecline,
    Handling,
    NotApplied,
    RouteReaders,
)


def _music(value: Any, _s: Mapping[str, Any]) -> str | None:
    if value != "licensed_music":
        return None
    return say(
        en="I can't add music under the spoken lines on this video yet, so it plays the clips' "
        "own sound",
        tr="Bu videoda konuşmaların altına henüz müzik ekleyemiyorum, o yüzden kliplerin kendi "
        "sesi çalacak",
    )


def _sfx(value: Any, _s: Mapping[str, Any]) -> str | None:
    if not value:
        return None
    return say(
        en="I can't add sound effects to an edit cut from what you say yet",
        tr="Söylediklerinden kesilen bir düzenlemeye henüz ses efekti ekleyemiyorum",
    )


FIELD_HANDLING: dict[str, Handling] = {
    "intro_hook": NotApplied(lines.hook),
    "story_structure": NotApplied(lines.story),
    # The default cut between excerpts is already a fast cut.
    "pacing": NotApplied(
        lines.pacing_line(
            lambda value, _s: value == "fast",
            "the cuts between spoken lines keep a fixed length",
            "konuşmalar arasındaki kesimler sabit uzunlukta",
        )
    ),
    "direction": NotApplied(
        lines.direction_line(
            "fast_montage",
            "it is cut around the spoken lines",
            "konuşulan cümlelerin etrafında kesiliyor",
        )
    ),
    "caption_style": NotApplied(lines.caption_style),
    "audio_strategy": NotApplied(_music),
    "title_animation": NotApplied(lines.title_animation),
    "label_position": NotApplied(lines.label_position),
    "optional_treatments": NotApplied(lines.treatments),
    "archetype": NotApplied(lines.archetype),
    "hero_media_id": NotApplied(lines.hero),
    "overlay_display": NotApplied(lines.overlay),
    "licensed_sfx": NotApplied(_sfx),
    "font_family": Applied(),
    "text_color": Applied(),
    "target_duration_s": Applied(),
    "montage_audio": Applied(),
    # The render contract's exact texts (`_confirmed_text`).
    "opening_title": Applied(via=lines.CONTRACT),
    "opening_title_duration_s": Applied(via=lines.CONTRACT),
    "shot_labels": Applied(via=lines.CONTRACT),
    "edit_format": Applied(via=lines.DISPATCH),
    "media_scope": Applied(via=lines.SELECTION),
    "selected_media_ids": Applied(via=lines.SELECTION),
}

READERS = RouteReaders(
    files=(
        "services/phone_speech_montage_job.py",
        "pipeline/phone_speech_montage_plan.py",
        "services/speech_montage_planning.py",
    ),
)

ASK_DECLINES: tuple[AskDecline, ...] = (
    AskDecline(
        "text_animation",
        lambda req, _s: lines.asks_text_animation(req),
        lines.animation_decline,
    ),
    AskDecline("captions", lambda req, _s: lines.asks_captions_on(req), lines.captions_decline),
)
