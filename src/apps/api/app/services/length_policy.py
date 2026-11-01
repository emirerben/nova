"""One source of truth for what determines an edit's duration.

The owner says which source determines the rendered length.  It deliberately
does not turn a source-derived length into a target requirement: that requires
independent proof that the creator asked for a particular output duration.
"""

from __future__ import annotations

import math
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Literal

from app.kria.brief import BriefRequirement, CreativeBrief
from app.kria.brief_route import loose_text
from app.kria.freeze_ask import times_one_moment
from app.schemas.edit_proposal import recognize_total_duration_s
from app.services.choice_questions import (
    CONFLICT_DURATION_VS_COUNT,
    CONFLICT_HOLDS_VS_LENGTH,
    CONFLICT_LENGTH_VS_NATURAL,
    CONFLICT_VOICE_VS_DURATION,
)

LengthOwner = Literal["creator", "song", "voiceover", "take", "footage"]
# KRI-847: "exact" is a creator-named length (+/- 10 %); "at_most" is a ceiling ("at most 10
# seconds"): the same check without its lower side. The model records it on the timing
# requirement as `facts["duration_bound"]`; anything else, or nothing, is exact. Exclusive
# wording ("under N") is the strict whole-export cap below, which this fact never changes.
DurationBound = Literal["exact", "at_most"]


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
    matches = sorted(
        [*_EN_STRICT_DURATION.finditer(text), *_TR_STRICT_DURATION.finditer(text)],
        key=lambda match: match.start(),
    )
    for match in matches:
        # A rejected or piece-scoped clause is not the whole-export instruction.
        # Keep looking for the current positive clause, while retaining the
        # existing fact-match check for the first eligible whole-export limit.
        if _duration_clause_is_negated_or_scoped(text, match):
            continue
        if _TITLE_COPY_AFTER_DURATION.match(text[match.end() :]):
            continue
        if _TR_NEGATED_DURATION_AFTER.match(text[match.end() :]):
            continue
        stated = float(match.group("seconds").replace(",", "."))
        return limit if math.isclose(stated, limit, rel_tol=0.0, abs_tol=1e-9) else None
    return None


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


def duration_bound(facts: Mapping[str, object] | None) -> DurationBound:
    """The bound a timing requirement's facts record; anything unknown is exact."""
    return "at_most" if facts is not None and facts.get("duration_bound") == "at_most" else "exact"


def creator_duration_satisfied(
    actual_s: float, requested_s: float, bound: DurationBound = "exact"
) -> bool:
    """Whether a finite measured length meets the creator's length ask.

    ``exact`` is `creator_duration_matches`. ``at_most`` drops only its LOWER side
    (0 < actual <= N + 10 %), so it can relax the exact check and never tighten it: a wrong or
    stale fact cannot create a hard cap, and 7.5 s meets "at most 10 seconds".
    """
    if bound != "at_most":
        return creator_duration_matches(actual_s, requested_s)
    return bool(
        math.isfinite(actual_s)
        and math.isfinite(requested_s)
        and actual_s > 0
        and requested_s > 0
        and actual_s <= requested_s * 1.10 + 1e-9
    )


def ceiling_miss_message(requested_s: float, actual_s: float) -> str:
    """A render over a ceiling, naming the ceiling (KRI-492: say which ask was missed).

    English like the exact-length decline beside it: a Turkish chat never shows a worker
    sentence with numbers in it (`kria_runtime._worker_copy`).
    """
    return f"You asked for at most {requested_s:g} seconds; this edit is {actual_s:g} seconds."


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
    {
        CONFLICT_DURATION_VS_COUNT,
        CONFLICT_HOLDS_VS_LENGTH,
        CONFLICT_VOICE_VS_DURATION,
        CONFLICT_LENGTH_VS_NATURAL,
    }
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


PaceName = Literal["relaxed", "balanced", "fast"]
_PACE_NAMES: frozenset[str] = frozenset({"relaxed", "balanced", "fast"})
# The directions the phone montage route can build (hard cuts over fast_cuts). A strategy
# direction outside this set is built as the first one and the plan says so.
ROUTE_BUILDABLE_DIRECTIONS: tuple[str, ...] = ("fast_montage",)
FALLBACK_ROUTE_DIRECTION = "fast_montage"


def stated_pace(strategy: Mapping[str, object] | object) -> PaceName | None:
    """The strategy's pacing when pace may drive the montage at all, else None.

    None when the kill switch is off or the strategy carries no valid pacing; callers then
    keep today's behavior byte for byte.
    """

    from app.config import settings  # noqa: PLC0415

    if not settings.pace_drives_cuts_enabled:
        return None
    pacing = _value(strategy, "pacing")
    return pacing if isinstance(pacing, str) and pacing in _PACE_NAMES else None  # type: ignore[return-value]


def effective_pace(strategy: Mapping[str, object] | object) -> PaceName | None:
    """The pace that changes cut LENGTHS, or None when nothing was said.

    ``pacing`` defaults to "balanced", so a plain "balanced" is silence (today's lengths) unless
    the server recorded that the creator said it (``pacing_requested``, like
    ``target_duration_requested``). "relaxed" and "fast" are decisions: the creator's words or
    the planner's own choice, and either way they reach the cut.
    """

    pace = stated_pace(strategy)
    if pace is None:
        return None
    if pace == "balanced" and _value(strategy, "pacing_requested") is not True:
        return None
    return pace


def route_direction(strategy: Mapping[str, object] | object) -> str:
    """The direction the phone montage snapshot carries: the strategy's, when this route can
    build it, else the one it can (and the plan records the request)."""

    from app.config import settings  # noqa: PLC0415

    direction = _value(strategy, "direction")
    if (
        settings.pace_drives_cuts_enabled
        and isinstance(direction, str)
        and direction in ROUTE_BUILDABLE_DIRECTIONS
    ):
        return direction
    return FALLBACK_ROUTE_DIRECTION


def direction_not_built(strategy: Mapping[str, object] | object) -> str | None:
    """The strategy's direction when this route had to build another one, else None.

    None with the kill switch off, so every record stays byte-identical to before.
    """

    from app.config import settings  # noqa: PLC0415

    direction = _value(strategy, "direction")
    if (
        settings.pace_drives_cuts_enabled
        and isinstance(direction, str)
        and direction != route_direction(strategy)
    ):
        return direction
    return None


# The directions the phone montage builds as a story (beats with text, held crossfaded
# moments) when the footage and the creator's asks allow it (text-explainer unit).
STORY_DIRECTIONS: tuple[str, ...] = ("text_explainer", "guided_story")


def story_direction(strategy: Mapping[str, object] | object) -> str | None:
    """The story direction the phone montage should try to build for ``strategy``, else None.

    None with the kill switch off (``STORY_DIRECTIONS_ON_MONTAGE_ENABLED``) and for every
    other direction, so those plans stay exactly what they were.
    """

    from app.config import settings  # noqa: PLC0415

    if not settings.story_directions_on_montage_enabled:
        return None
    direction = _value(strategy, "direction")
    return direction if isinstance(direction, str) and direction in STORY_DIRECTIONS else None


def story_hold_floor_s(strategy: Mapping[str, object] | object) -> float | None:
    """The shortest a clip may be held under a story direction, else None.

    The guided compiler's own minimum moment for that direction (``_DIRECTION_POLICY``), read
    from there so planning, the render and the compile can never disagree; a relaxed pace
    raises it to the relaxed hold. A clip shorter than this simply plays whole
    (``guided_moment_floor_s``); this floor never drops one.
    """

    direction = story_direction(strategy)
    if direction is None:
        return None
    from app.pipeline.guided_story import _DIRECTION_POLICY  # noqa: PLC0415
    from app.schemas.edit_proposal import pace_hold_bounds  # noqa: PLC0415

    floor = float(_DIRECTION_POLICY[direction]["min_moment_s"])
    if effective_pace(strategy) == "relaxed":
        # A relaxed story reads slowly: its holds start at the relaxed pace's usual hold, not
        # at the direction's bare minimum (fast and balanced hold the minimum).
        floor = max(floor, pace_hold_bounds("relaxed").preferred_s)
    return floor


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


@dataclass(frozen=True)
class CreatorDuration:
    """A creator-proven length with how it is judged (KRI-847)."""

    seconds: float
    bound: DurationBound = "exact"


def creator_duration(
    *,
    creator_texts: Iterable[str] = (),
    brief: CreativeBrief | None = None,
    authoritative_answers: Iterable[Mapping[str, object] | object] = (),
    strategy: Mapping[str, object] | object | None = None,
) -> CreatorDuration | None:
    """Return a creator-proven target duration, never a model/client target.

    ``strategy`` is accepted only to make callers explicit about untrusted data;
    no model/client duration field or marker is inspected here.

    Creator text and choice answers name a concrete length (exact). Only the brief can
    record a ceiling, and only when every live length ask says so.
    """

    del strategy
    for text in reversed(tuple(creator_texts)):
        value = recognize_total_duration_s(text)
        if value is not None:
            return CreatorDuration(float(value))
    if brief is not None:
        # KRI-777: a freeze's or a moment's seconds ("freeze the frame for one second on the
        # bite") are not the video's length, so they never make a real length ambiguous.
        asks = [
            requirement
            for requirement in brief.live()
            if requirement.kind == "timing"
            and requirement.facts.get("duration_s") is not None
            and not times_one_moment(requirement)
        ]
        values = [float(requirement.facts["duration_s"]) for requirement in asks]
        if len(set(values)) == 1 and values:
            ceiling = all(duration_bound(requirement.facts) == "at_most" for requirement in asks)
            return CreatorDuration(values[0], "at_most" if ceiling else "exact")
    for answer in authoritative_answers:
        if _value(answer, "conflict") not in _DURATION_CONFLICTS:
            continue
        value = _value(answer, "duration_s")
        if isinstance(value, (int, float)) and value > 0:
            return CreatorDuration(float(value))
    return None


def creator_duration_s(
    *,
    creator_texts: Iterable[str] = (),
    brief: CreativeBrief | None = None,
    authoritative_answers: Iterable[Mapping[str, object] | object] = (),
    strategy: Mapping[str, object] | object | None = None,
) -> float | None:
    """The seconds of ``creator_duration`` (its bound is read separately)."""

    found = creator_duration(
        creator_texts=creator_texts,
        brief=brief,
        authoritative_answers=authoritative_answers,
        strategy=strategy,
    )
    return found.seconds if found is not None else None


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
