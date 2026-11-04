"""``music`` (module ``music_level``): "Turn the music down" when a voice competes with the song."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from app.services.suggestions.builders import (
    PHONE_AND_CLOUD,
    editor_op_action,
    editor_ops_witness,
    legacy_kind,
)
from app.services.suggestions.context import (
    NO_TARGET,
    SuggestionContext,
    Target,
    any_speech,
    families,
    music_level,
)
from app.services.suggestions.models import ChipKind, ChipKindSpec

_QUIET_AT = 0.3


def _present_track(ctx: SuggestionContext) -> bool:
    music = (ctx.snapshot or {}).get("music")
    return isinstance(music, Mapping) and music.get("current_track_id") is not None


def _available(ctx: SuggestionContext) -> bool:
    # Only worth offering when a voice competes with the song; a music-led, speech-free edit
    # is the song's showcase and "turn it down" would hurt it.
    return (
        ctx.state == "ready"
        and "music" in families(ctx)
        and _present_track(ctx)
        and any_speech(ctx)
    )


def _already_quiet(ctx: SuggestionContext) -> bool:
    level = music_level(ctx)
    return level is not None and level <= _QUIET_AT


def _ground(ctx: SuggestionContext) -> list[Target]:
    return [NO_TARGET]


def _op(ctx: SuggestionContext, target: Target) -> dict[str, Any] | None:
    level = music_level(ctx)
    quieter = 0.2 if level is None else max(0.05, round(level * 0.6, 2))
    return {"op": "set_mix", "music_level": quieter}


SPEC = ChipKindSpec(
    kind=ChipKind.MUSIC,
    states=frozenset({"ready"}),
    capability={"ready": "family:music"},
    available=_available,
    already_present=_already_quiet,
    ground=_ground,
    dry_run={"ready": "editor"},
    expects="a lower music level (mix.music_level)",
    op=_op,
    claimed_op="set_mix",
    witness_op="set_mix",
    edit_tag="quiet_music",
)

KIND = legacy_kind(
    SPEC,
    surfaces=PHONE_AND_CLOUD,
    build_action=editor_op_action(SPEC),
    witness=editor_ops_witness,
)
