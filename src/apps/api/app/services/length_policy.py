"""One source of truth for what determines an edit's duration.

The owner says which source determines the rendered length.  It deliberately
does not turn a source-derived length into a target requirement: that requires
independent proof that the creator asked for a particular output duration.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping
from typing import Literal

from app.kria.brief import CreativeBrief
from app.schemas.edit_proposal import recognize_total_duration_s
from app.services.choice_questions import (
    CONFLICT_DURATION_VS_COUNT,
    CONFLICT_LENGTH_VS_NATURAL,
    CONFLICT_VOICE_VS_DURATION,
)

LengthOwner = Literal["creator", "song", "voiceover", "take", "footage"]


def creator_duration_matches(actual_s: float, requested_s: float) -> bool:
    """Whether a finite measured length meets the creator's ten-percent target."""
    return bool(
        math.isfinite(actual_s)
        and math.isfinite(requested_s)
        and actual_s > 0
        and requested_s > 0
        and abs(actual_s - requested_s) <= requested_s * 0.10 + 1e-9
    )


# This is intentionally a complete, hand-declared matrix rather than a
# fallback rule over EDIT_FORMATS. Adding an edit format must name its natural
# duration source here before it can render.
NATURAL_OWNER_DECLARATION: dict[str, dict[str, LengthOwner]] = {
    "montage": {
        "licensed_music": "footage",
        "original_audio": "footage",
        "voiceover": "voiceover",
        "user_song": "song",
    },
    "talking_head": {
        "licensed_music": "footage",
        "original_audio": "take",
        "voiceover": "voiceover",
        "user_song": "song",
    },
    "day_vlog": {
        "licensed_music": "footage",
        "original_audio": "footage",
        "voiceover": "voiceover",
        "user_song": "song",
    },
    "single_hero": {
        "licensed_music": "footage",
        "original_audio": "footage",
        "voiceover": "voiceover",
        "user_song": "song",
    },
    "subtitled": {
        "licensed_music": "footage",
        "original_audio": "take",
        "voiceover": "voiceover",
        "user_song": "song",
    },
    "narrated": {
        "licensed_music": "footage",
        "original_audio": "take",
        "voiceover": "voiceover",
        "user_song": "song",
    },
    "narrated_planned": {
        "licensed_music": "footage",
        "original_audio": "take",
        "voiceover": "voiceover",
        "user_song": "song",
    },
    "narrated_ready": {
        "licensed_music": "footage",
        "original_audio": "take",
        "voiceover": "voiceover",
        "user_song": "song",
    },
    "slides": {
        "licensed_music": "footage",
        "original_audio": "footage",
        "voiceover": "voiceover",
        "user_song": "song",
    },
}
KNOWN_EDIT_FORMATS: frozenset[str] = frozenset(NATURAL_OWNER_DECLARATION)
KNOWN_AUDIO_SOURCES: frozenset[str] = frozenset(
    {source for sources in NATURAL_OWNER_DECLARATION.values() for source in sources}
)
_DURATION_CONFLICTS: frozenset[str] = frozenset(
    {CONFLICT_DURATION_VS_COUNT, CONFLICT_VOICE_VS_DURATION, CONFLICT_LENGTH_VS_NATURAL}
)


def _value(strategy: Mapping[str, object] | object, name: str, default: object = None) -> object:
    if isinstance(strategy, Mapping):
        return strategy.get(name, default)
    return getattr(strategy, name, default)


def natural_length_owner(strategy: Mapping[str, object] | object) -> LengthOwner:
    """Return the source that naturally determines an edit's length.

    A creator's explicit target is deliberately absent here so capacity and
    clarification code can still ask what source has to be shortened.
    """

    audio = _value(strategy, "audio_strategy", "licensed_music")
    edit_format = _value(strategy, "edit_format", "montage")
    if not isinstance(edit_format, str) or edit_format not in NATURAL_OWNER_DECLARATION:
        raise ValueError(f"no natural length owner declared for edit format {edit_format!r}")
    if not isinstance(audio, str) or audio not in NATURAL_OWNER_DECLARATION[edit_format]:
        raise ValueError(f"no natural length owner declared for audio source {audio!r}")
    # A continuous voice track may be represented by the audio source or by
    # the assembled execution/voice mode.  The latter keeps old snapshots
    # truthful while they migrate to the explicit audio vocabulary.
    if bool(_value(strategy, "has_voiceover", False)):
        return "voiceover"
    if _value(strategy, "voice_mode") == "continuous":
        return "take"
    # Licensed music has no duration authority until the server freezes a
    # concrete catalog section. This keeps legacy/default silent montages
    # footage-owned while making an approved background section authoritative.
    if audio == "licensed_music" and _selected_music_section(strategy):
        return "song"
    return NATURAL_OWNER_DECLARATION[edit_format][audio]


def _selected_music_section(strategy: Mapping[str, object] | object) -> bool:
    section = _value(strategy, "selected_music_section")
    if not isinstance(section, Mapping):
        section = _value(strategy, "music_section")
    if not isinstance(section, Mapping):
        return False
    start = section.get("start_s")
    end = section.get("end_s")
    return isinstance(start, (int, float)) and isinstance(end, (int, float)) and end > start


def length_owner(
    strategy: Mapping[str, object] | object, *, creator_duration_proven: bool
) -> LengthOwner:
    """Return the effective owner; a proven creator target overrides the natural one."""

    return "creator" if creator_duration_proven else natural_length_owner(strategy)


def creator_duration_s(
    *,
    creator_texts: Iterable[str] = (),
    brief: CreativeBrief | None = None,
    authoritative_answers: Iterable[Mapping[str, object] | object] = (),
    strategy: Mapping[str, object] | object | None = None,
) -> float | None:
    """Return a creator-proven target duration, never a model/client target.

    ``strategy`` is accepted only to make callers explicit about untrusted data;
    no model/client duration field or marker is inspected here.
    """

    del strategy
    for text in reversed(tuple(creator_texts)):
        value = recognize_total_duration_s(text)
        if value is not None:
            return float(value)
    if brief is not None:
        values = [
            float(requirement.facts["duration_s"])
            for requirement in brief.live()
            if requirement.kind == "timing" and requirement.facts.get("duration_s") is not None
        ]
        if len(set(values)) == 1 and values:
            return values[0]
    for answer in authoritative_answers:
        if _value(answer, "conflict") not in _DURATION_CONFLICTS:
            continue
        value = _value(answer, "duration_s")
        if isinstance(value, (int, float)) and value > 0:
            return float(value)
    return None


def has_creator_duration_proof(
    *,
    creator_texts: Iterable[str] = (),
    brief: CreativeBrief | None = None,
    authoritative_answers: Iterable[Mapping[str, object] | object] = (),
    strategy: Mapping[str, object] | object | None = None,
) -> bool:
    """Whether trusted creator evidence establishes an output-duration request."""

    return (
        creator_duration_s(
            creator_texts=creator_texts,
            brief=brief,
            authoritative_answers=authoritative_answers,
            strategy=strategy,
        )
        is not None
    )
