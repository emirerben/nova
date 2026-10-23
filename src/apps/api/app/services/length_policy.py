"""One source of truth for what determines an edit's duration.

The owner says which source determines the rendered length.  It deliberately
does not turn a source-derived length into a target requirement: that requires
independent proof that the creator asked for a particular output duration.
"""

from __future__ import annotations

import math
import re
from collections.abc import Iterable, Mapping
from typing import Literal

from app.kria.brief import BriefRequirement, CreativeBrief
from app.kria.brief_route import loose_text
from app.kria.freeze_ask import times_one_moment
from app.schemas.edit_proposal import recognize_total_duration_s
from app.services.choice_questions import (
    CONFLICT_DURATION_VS_COUNT,
    CONFLICT_LENGTH_VS_NATURAL,
    CONFLICT_VOICE_VS_DURATION,
)

LengthOwner = Literal["creator", "song", "voiceover", "take", "footage"]


def strict_duration_matches(actual_s: float, max_s: float) -> bool:
    """Whether a measured export satisfies an explicit exclusive upper bound."""

    return bool(
        math.isfinite(actual_s)
        and math.isfinite(max_s)
        and actual_s > 0
        and max_s > 0
        and actual_s < max_s
    )


_EN_STRICT_DURATION = re.compile(
    r"\b(?:under\s*|less than\s+|shorter than\s+)"
    r"(?P<seconds>\d{1,3}(?:\.\d+)?)\s*(?:seconds?|secs?|s)\b"
)
_TR_STRICT_DURATION = re.compile(
    r"\b(?P<seconds>\d{1,3}(?:[.,]\d+)?)\s+saniye(?:nin|den)?\s+"
    r"(?:altinda|kisa)\b"
)
_SCOPED_DURATION_CLAUSE = re.compile(
    r"\b(?:(?:each|every|per|her)\s+)?(?:clip|clips|moment|moments|shot|shots|cut|cuts|"
    r"klip(?:ler)?|an(?:lar)?|sahne|kesit)\s*$"
)
_NEGATED_DURATION_PREDICATE = re.compile(
    r"\b(?:dont|don't|do not|never)\b(?:\s+(?!and\b)\w+){0,4}\s+"
    r"(?:keep|make|stay|be|run)\b(?:\s+\w+){0,3}\s*$"
)
_TITLE_COPY_AFTER_DURATION = re.compile(r"^\s*(?:title|text|baslik|yazi)\b")
_TR_NEGATED_DURATION_AFTER = re.compile(r"^\s+(?:olmasin|olmasini\s+istemiyorum)\b")


def _unquoted_duration_text(text: str) -> str:
    """Remove quoted copy, where duration-like words are not an edit instruction."""

    from app.services.spoken_quotes import quoted_spans  # noqa: PLC0415

    out = str(text or "")
    for start, end, _phrase in reversed(quoted_spans(out)):
        out = out[:start] + " " + out[end:]
    return loose_text(out)


def _duration_clause_is_negated_or_scoped(text: str, match: re.Match[str]) -> bool:
    """Reject only the matched clause when it negates or scopes the bound to a piece."""

    clause_start = max(text.rfind(".", 0, match.start()), text.rfind(";", 0, match.start())) + 1
    before = text[clause_start : match.start()]
    return bool(
        re.search(r"\b(?:not|degil)\s*$", before)
        or _NEGATED_DURATION_PREDICATE.search(before)
        or _SCOPED_DURATION_CLAUSE.search(before)
    )


def strict_duration_limit_s(requirement: BriefRequirement) -> float | None:
    """Read a narrow, creator-recorded whole-export exclusive duration limit.

    Approximate and inclusive language remains deliberately outside this helper: it
    is guidance, not a strict render constraint. The numeric wording must agree with
    the structured fact so an inferred or stale fact cannot create a hard cap.
    """

    if (
        requirement.kind != "timing"
        or requirement.scope != "global"
        or times_one_moment(requirement)
    ):
        return None
    recorded = requirement.facts.get("duration_s")
    if isinstance(recorded, bool) or not isinstance(recorded, int | float):
        return None
    limit = float(recorded)
    if not math.isfinite(limit) or limit <= 0:
        return None
    # A timing requirement may store the creator's words in `literal` alone.
    # When it has a description, that is the instruction; its literal is copy.
    text = _unquoted_duration_text(requirement.description or requirement.literal or "")
    match = _EN_STRICT_DURATION.search(text) or _TR_STRICT_DURATION.search(text)
    if match is None:
        return None
    if _duration_clause_is_negated_or_scoped(text, match):
        return None
    if _TITLE_COPY_AFTER_DURATION.match(text[match.end() :]):
        return None
    if _TR_NEGATED_DURATION_AFTER.match(text[match.end() :]):
        return None
    stated = float(match.group("seconds").replace(",", "."))
    return limit if math.isclose(stated, limit, rel_tol=0.0, abs_tol=1e-9) else None


def creator_max_export_duration_s(*, brief: CreativeBrief | None) -> float | None:
    """The tightest active strict whole-export limit, if the creator set one."""

    if brief is None:
        return None
    limits = [
        limit
        for requirement in brief.live()
        if (limit := strict_duration_limit_s(requirement)) is not None
    ]
    return min(limits) if limits else None


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
        # KRI-777: a freeze's or a moment's seconds ("freeze the frame for one second on the
        # bite") are not the video's length, so they never make a real length ambiguous.
        values = [
            float(requirement.facts["duration_s"])
            for requirement in brief.live()
            if requirement.kind == "timing"
            and requirement.facts.get("duration_s") is not None
            and not times_one_moment(requirement)
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
