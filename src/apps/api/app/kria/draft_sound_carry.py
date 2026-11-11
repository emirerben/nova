"""A correction turn that doesn't touch the sound keeps the draft's sound choice (KRI-942).

Don Julio walk-in M1: turn 1 named Atakan's clip as the one camera sound
(`montage_audio.source_media_ids = [m1_04]`); turn 2 only asked to add the title, yet the Main
Creator re-proposed the whole strategy with `source_media_ids: []` and `voice_mode:
"excerpts"`. The route flipped from the spoken-excerpt montage to the unified one and every clip
played its own sound.

The brief now carries the creator's on/off asks (`clip_sound_toggle_ask`), but the strategy's
sound choice still picks the route. So, like a confirmed label (KRI-766), the draft's
`montage_audio` / `voice_mode` stay unless this turn changed what the creator asked about sound:
the server compares the brief's live ``audio`` requirements before and after the turn's updates
(KRI-667: the model's structured updates decide, no wording is read here).
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from app.agents._schemas.creator_agent import CreativeStrategy
    from app.kria.brief import CreativeBrief


def _audio_asks(brief: CreativeBrief | None) -> list[str]:
    if brief is None:
        return []
    return sorted(
        json.dumps(
            {
                "id": req.id,
                "scope": req.scope,
                "literal": req.literal,
                "description": req.description,
                "facts": req.facts,
            },
            sort_keys=True,
            default=str,
        )
        for req in brief.live()
        if req.kind == "audio"
    )


def sound_asks_unchanged(before: CreativeBrief | None, after: CreativeBrief | None) -> bool:
    """True when the brief holds a sound ask and this turn's updates left every live ``audio``
    requirement as it was.

    A first turn (no brief before) is never "unchanged": there is nothing to keep. Neither is
    a brief with no ``audio`` requirement at all: the creator never said anything about the
    sound, so the strategy's sound design is the planner's to change (KRI-945's spoken-excerpt
    montage picks its camera voice on a text-only turn).
    """
    asks = _audio_asks(after)
    return before is not None and bool(asks) and _audio_asks(before) == asks


def has_sound_ask(brief: CreativeBrief | None) -> bool:
    """True when the brief holds any live ``audio`` requirement."""
    return bool(_audio_asks(brief))


def asks_camera_sound(brief: CreativeBrief | None) -> bool:
    """True when a live ask is to keep the clips' own sound (KRI-1365)."""
    from app.kria.brief_checks import _wants_source_sound_kept  # noqa: PLC0415

    return brief is not None and any(
        _wants_source_sound_kept(req) for req in brief.live() if req.kind == "audio"
    )


def carry_draft_audio_strategy(
    strategy: CreativeStrategy, draft_strategy: Mapping[str, Any] | None
) -> CreativeStrategy:
    """``strategy`` back on the draft's music route when a turn with no sound ask flipped it.

    The Main Creator re-emits the whole strategy on a pacing or length turn and may turn a
    ``licensed_music`` draft into ``original_audio`` (KRI-1365). With no ``audio`` ask in the
    brief the creator said nothing about sound, so the draft's route stays, with its
    `montage_audio`. Only that flip, and only within the same edit format.
    """
    if (
        not isinstance(draft_strategy, Mapping)
        or draft_strategy.get("audio_strategy") != "licensed_music"
        or strategy.audio_strategy != "original_audio"
        or draft_strategy.get("edit_format") != strategy.edit_format
    ):
        return strategy
    montage_audio = None
    if isinstance(draft_strategy.get("montage_audio"), Mapping):
        from app.schemas.edit_proposal import MontageAudioPlan  # noqa: PLC0415

        try:
            montage_audio = MontageAudioPlan.model_validate(dict(draft_strategy["montage_audio"]))
        except ValueError:
            return strategy
    return strategy.model_copy(
        update={"audio_strategy": "licensed_music", "montage_audio": montage_audio}
    )


def carry_draft_sound(
    strategy: CreativeStrategy, draft_strategy: Mapping[str, Any] | None
) -> CreativeStrategy:
    """``strategy`` with the draft's `montage_audio` and `voice_mode` when the model rewrote them.

    Only within the same audio lane and edit format, and only while every clip the draft named
    is still in the edit; anything else is a different edit, and the new proposal stands.
    """
    if not isinstance(draft_strategy, Mapping):
        return strategy
    draft_audio = draft_strategy.get("montage_audio")
    if not isinstance(draft_audio, Mapping):
        return strategy
    from app.schemas.edit_proposal import MontageAudioPlan  # noqa: PLC0415

    try:
        kept = MontageAudioPlan.model_validate(dict(draft_audio))
    except ValueError:
        return strategy
    draft_mode = draft_strategy.get("voice_mode")
    current = strategy.montage_audio
    if (
        current is not None
        and current.model_dump(mode="json") == kept.model_dump(mode="json")
        and strategy.voice_mode == draft_mode
    ):
        return strategy
    if (
        draft_strategy.get("audio_strategy") != strategy.audio_strategy
        or draft_strategy.get("edit_format") != strategy.edit_format
    ):
        return strategy
    if strategy.media_scope == "selected" and not set(kept.source_media_ids) <= set(
        strategy.selected_media_ids or []
    ):
        return strategy
    update: dict[str, Any] = {"montage_audio": kept}
    if draft_mode is None or isinstance(draft_mode, str):
        update["voice_mode"] = draft_mode
    return strategy.model_copy(update=update)


__all__ = [
    "asks_camera_sound",
    "carry_draft_audio_strategy",
    "carry_draft_sound",
    "has_sound_ask",
    "sound_asks_unchanged",
]
