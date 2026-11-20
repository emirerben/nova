"""How long a source recording may be, per format and source kind (KRI-1554).

One table, one row per format and source kind. Every reader takes its number from here:

* the upload contract the iPhone checks when a creator picks a file (``/capabilities``
  ``formats[].max_duration_s`` and the thread's ``media_capabilities``);
* upload registration, which refuses an over-long file from a build that predates that check;
* the dispatch gate and the phone compilers, which refuse a plan that could never render;
* speech cleanup, whose analysis window is ``speech_analysis_window_s()``;
* slide validation and song registration.

Readers call ``max_duration_s(key)`` when they check, so one edited row changes every reader.
``tests/test_source_length_policy.py`` fails when a compiler, gate or preflight writes one of these
numbers itself.

A row "binds at pick" when the format and source kind are both known the moment the creator picks
a file, so the iPhone can refuse it before anything uploads. ``speech_cut_speaker`` binds at plan
time: a montage only becomes a speech-cut montage once the planner reads what its clips say, so the
same 6-minute clip is fine as montage footage and too long as the speaking clip.

This module imports nothing from the app: ``app.kria.media_sources`` reads it at class definition.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Final, Literal

SourceKind = Literal["any", "clip", "voiceover", "song", "visual_video", "speaker_clip", "speech"]
BindsAt = Literal["pick", "plan", "analysis"]
# The upload roles the iPhone checks at pick time, as `media_capabilities` names them.
PickRole = Literal["clip", "voiceover", "song", "visual_video"]

TALKING_FORMATS: Final = frozenset({"subtitled"})
NARRATED_FORMATS: Final = frozenset({"narrated", "narrated_planned", "narrated_ready"})
# The formats speech cleanup analyses (`active_narration_source.SUPPORTED_EDIT_FORMATS`).
SPEECH_CLEANUP_FORMATS: Final = frozenset({"subtitled", "talking_head", *NARRATED_FORMATS})
SLIDE_FORMATS: Final = frozenset({"slides"})
SPEECH_CUT_FORMATS: Final = frozenset({"montage"})


@dataclass(frozen=True, slots=True)
class SourceLengthRule:
    """One limit. ``formats`` None means every format."""

    key: str
    formats: frozenset[str] | None
    source_kind: SourceKind
    max_duration_s: float
    binds_at: BindsAt
    enforced_by: str

    def applies_to(self, edit_format: str | None) -> bool:
        return self.formats is None or (edit_format or "montage") in self.formats


# The one place each number is written. Keys are stable: readers and tests name them.
SOURCE_LENGTH_POLICY: dict[str, SourceLengthRule] = {
    rule.key: rule
    for rule in (
        SourceLengthRule(
            key="library_media",
            formats=None,
            source_kind="any",
            max_duration_s=1800.0,
            binds_at="pick",
            enforced_by="camera-roll intake, media library ingest, phone source bindings",
        ),
        SourceLengthRule(
            key="talking_clip",
            formats=TALKING_FORMATS,
            source_kind="clip",
            max_duration_s=300.0,
            binds_at="pick",
            enforced_by=(
                "upload registration, Talking dispatch gate, phone Talking compiler (speaker, "
                "extra speakers, inserts, ending), Talking worker, cloud Talking render"
            ),
        ),
        SourceLengthRule(
            key="speech_cut_speaker",
            formats=SPEECH_CUT_FORMATS,
            source_kind="speaker_clip",
            max_duration_s=300.0,
            binds_at="plan",
            enforced_by="speech-cut montage compiler",
        ),
        SourceLengthRule(
            key="speech_analysis_window",
            formats=SPEECH_CLEANUP_FORMATS,
            source_kind="speech",
            max_duration_s=300.0,
            binds_at="analysis",
            enforced_by=(
                "active_narration_source (the only owner of the window), cleanup worker, "
                "cleanup apply, talking-head spine cut"
            ),
        ),
        SourceLengthRule(
            key="slide_video",
            formats=SLIDE_FORMATS,
            source_kind="visual_video",
            max_duration_s=90.0,
            binds_at="pick",
            enforced_by="slide validation (Instagram carousel; TikTok photo posts take no video)",
        ),
        SourceLengthRule(
            key="creator_song",
            formats=None,
            source_kind="song",
            max_duration_s=600.0,
            binds_at="pick",
            enforced_by="upload registration",
        ),
    )
}

# A voiceover in a format speech cleanup analyses is refused at pick when cleanup could not
# analyse all of it: the voice track is the whole video, so the window is the voiceover limit.
_VOICEOVER_RULE_KEY: Final = "speech_analysis_window"


def rule(key: str) -> SourceLengthRule:
    return SOURCE_LENGTH_POLICY[key]


def max_duration_s(key: str) -> float:
    """The limit in seconds, read when the caller checks (never cached at import)."""

    return SOURCE_LENGTH_POLICY[key].max_duration_s


def library_max_s() -> float:
    return max_duration_s("library_media")


def speech_analysis_window_s() -> float:
    return max_duration_s("speech_analysis_window")


def exceeds(duration_s: float, key: str) -> bool:
    """Whether ``duration_s`` is over the limit. A non-finite duration is over every limit."""

    value = float(duration_s)
    return not math.isfinite(value) or value > max_duration_s(key)


def pick_rule(edit_format: str | None, role: PickRole) -> SourceLengthRule:
    """The tightest limit the iPhone can check when the creator picks a file for ``role``.

    Every source is bounded by the library limit; a format adds a tighter row when one binds at
    pick time for that role.
    """

    candidates = [rule("library_media")]
    if role == "clip" and rule("talking_clip").applies_to(edit_format):
        candidates.append(rule("talking_clip"))
    elif role == "voiceover" and rule(_VOICEOVER_RULE_KEY).applies_to(edit_format):
        candidates.append(rule(_VOICEOVER_RULE_KEY))
    elif role == "song":
        candidates.append(rule("creator_song"))
    elif role == "visual_video" and rule("slide_video").applies_to(edit_format):
        candidates.append(rule("slide_video"))
    return min(candidates, key=lambda item: item.max_duration_s)


def upload_role(content_type: str, role: str | None) -> PickRole:
    """The pick role of a project upload: a video is a clip, audio a song or the voiceover."""

    if content_type.startswith("audio/"):
        return "song" if role == "song" else "voiceover"
    return "clip"


def exceeded_pick_rule(
    duration_s: float, edit_format: str | None, role: PickRole
) -> SourceLengthRule | None:
    """The pick-time rule ``duration_s`` breaks, or None when the file may be used."""

    limit = pick_rule(edit_format, role)
    value = float(duration_s)
    return limit if not math.isfinite(value) or value > limit.max_duration_s else None


def pick_limit_s(edit_format: str | None, role: PickRole) -> float:
    return pick_rule(edit_format, role).max_duration_s


def format_pick_limits(edit_format: str | None) -> dict[str, float]:
    """Per-role pick limits for one format, as the upload contract serves them."""

    roles: tuple[PickRole, ...] = ("clip", "voiceover", "song", "visual_video")
    return {role: pick_limit_s(edit_format, role) for role in roles}


def limit_phrase(seconds: float) -> str:
    """The limit as a creator reads it: "5 minutes", "90 seconds", "1 minute"."""

    value = float(seconds)
    if value >= 60 and math.isclose(value % 60, 0.0, abs_tol=1e-6):
        minutes = round(value / 60)
        return f"{minutes} minute" if minutes == 1 else f"{minutes} minutes"
    whole = round(value)
    return f"{whole} second" if whole == 1 else f"{whole} seconds"


_ROLE_NOUN: Final[dict[str, str]] = {
    "clip": "Clips",
    "voiceover": "Voiceovers",
    "song": "Songs",
    "visual_video": "Slide videos",
}


def too_long_message(edit_format: str | None, role: PickRole) -> str:
    """The registration refusal for a build without the pick-time check, naming the limit."""

    limit = pick_rule(edit_format, role)
    noun = "Talking clips" if limit.key == "talking_clip" else _ROLE_NOUN[role]
    return (
        f"This file is too long. {noun} can be up to {limit_phrase(limit.max_duration_s)}. "
        "Trim it and add it again."
    )
