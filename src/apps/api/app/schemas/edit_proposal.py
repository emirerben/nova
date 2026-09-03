"""Versioned, reviewable guided-edit proposal contract.

The proposal is deliberately stored as one JSONB envelope on ``PlanItem``.
Draft and approved snapshots live together so media changes can mark a plan
stale without erasing the creator's last approval.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, Field, field_validator, model_validator

ProposalStatus = Literal[
    "briefing",
    "analyzing",
    "drafting",
    "draft",
    "approved",
    "stale",
    "failed",
]
ProposalDirection = Literal["guided_story", "fast_montage", "text_explainer"]
ProposalPace = Literal["relaxed", "balanced", "fast"]
DirectionProvenance = Literal["creator_explicit", "ai_inferred", "creator_confirmed"]
DirectionGuidanceState = Literal["awaiting_direction_confirmation", "confirmed"]
TextDensity = Literal["minimal", "moderate", "dense"]
AudioRole = Literal["music_led", "original_audio", "voiceover", "mixed"]
OutputOrientation = Literal["portrait", "landscape"]
MediaLane = Literal["clip", "asset"]
MediaKind = Literal["image", "video"]
BeatLayout = Literal["fullscreen", "supporting_card"]
ImageGrouping = Literal["scattered", "runs"]
SequenceGrouping = Literal["none", "sport_context"]
MediaContextGroup = Literal[
    "football",
    "basketball",
    "beach_volleyball",
    "tennis",
    "track_and_field",
    "field_context",
    "beach_context",
    "sidelines",
    "court_context",
    "people_context",
    "other",
]
ThoughtSource = Literal["ai_draft", "user"]
ConversationRole = Literal["user", "agent"]
ConversationPhase = Literal["briefing", "review"]
ConversationSuggestion = Annotated[str, Field(min_length=1, max_length=100)]
EDIT_CONVERSATION_MAX_TURNS = 20
CREATOR_SELECTED_ORIENTATION_REASON = "The creator selected this output format."


class MontageTextBinding(BaseModel):
    """Text the montage should bind to every cut using one source."""

    media_id: str = Field(min_length=1, max_length=100)
    text: str = Field(min_length=1, max_length=120)


class MontageAudioPlan(BaseModel):
    """Generic audio intent for a source-aware montage timeline."""

    preserve_source_audio: bool = True
    preview_source_beds: bool = False
    source_media_ids: list[str] = Field(default_factory=list, max_length=12)

    @model_validator(mode="after")
    def validate_source_ids(self) -> MontageAudioPlan:
        if len(set(self.source_media_ids)) != len(self.source_media_ids):
            raise ValueError("montage audio source IDs must be unique")
        if any(not value.strip() for value in self.source_media_ids):
            raise ValueError("montage audio source IDs must not be empty")
        return self


class MontageCadenceConstraint(BaseModel):
    """Exact creator-authored source cadence for a fast montage."""

    mode: Literal["round_robin"] = "round_robin"
    source_media_ids: list[str] = Field(min_length=2, max_length=12)
    cut_duration_s: float = Field(ge=0.4, le=3.0)
    reuse_policy: Literal["no_repeat", "allow_repeat"] = "no_repeat"

    @field_validator("cut_duration_s")
    @classmethod
    def validate_frame_alignment(cls, value: float) -> float:
        frame_count = round(value * 30)
        frame_aligned = frame_count / 30
        if abs(value - frame_aligned) > 0.001:
            raise ValueError("montage cadence cut duration must align to 30fps frames")
        return round(frame_aligned, 6)

    @model_validator(mode="after")
    def validate_source_ids(self) -> MontageCadenceConstraint:
        if len(set(self.source_media_ids)) != len(self.source_media_ids):
            raise ValueError("montage cadence source IDs must be unique")
        if any(not value.strip() for value in self.source_media_ids):
            raise ValueError("montage cadence source IDs must not be empty")
        return self


def _recognized_frame_aligned_cadence(value: float) -> float | None:
    """Return a safe 30fps cadence value or leave ambiguous timing to the agent."""

    if not 0.4 <= value <= 3.0:
        return None
    frame_aligned = round(value * 30) / 30
    if abs(value - frame_aligned) > 0.001:
        return None
    return round(frame_aligned, 6)


def recognize_round_robin_cadence(text: str) -> float | None:
    """Recognize explicit alternation plus numeric cut timing without an LLM."""

    normalized = " ".join(str(text or "").casefold().split())
    alternates = bool(
        re.search(r"\b(?:alternate|alternating|back and forth)\b", normalized)
        or re.search(r"\bswitch\b.{0,40}\b(?:other|between)\b", normalized)
    )
    if not alternates:
        return None
    contextual_patterns = (
        r"\bevery\s+(?P<value>\d+(?:\.\d+)?)\s*(?:seconds?|secs?|s)\b",
        r"\b(?P<value>\d+(?:\.\d+)?)\s*(?:seconds?|secs?|s)\s+(?:from|of)\b",
    )
    for pattern in contextual_patterns:
        contextual = re.search(pattern, normalized)
        if contextual is not None:
            value = float(contextual.group("value"))
            return _recognized_frame_aligned_cadence(value)
    if re.search(r"\bevery\s+one\s+second\b|\bone\s+second\s+(?:from|of)\b", normalized):
        return 1.0
    matches = re.finditer(
        r"\b(?P<value>\d+(?:\.\d+)?)\s*(?:seconds?|secs?|s)\b",
        normalized,
    )
    for match in matches:
        value = float(match.group("value"))
        recognized = _recognized_frame_aligned_cadence(value)
        if recognized is not None:
            return recognized
    if re.search(r"\bone second\b", normalized):
        return 1.0
    return None


def rejects_round_robin_cadence(text: str) -> bool:
    """Recognize an explicit latest-turn request to stop alternating."""

    normalized = " ".join(str(text or "").casefold().split())
    return bool(
        re.search(
            r"\b(?:do not|don't|dont|stop|no longer|instead of)\b.{0,48}"
            r"\b(?:alternate|alternating|back and forth|switching)\b",
            normalized,
        )
    )


def recognize_total_duration_s(text: str) -> int | None:
    """Recognize an explicit whole-output duration without guessing from cut timing."""

    normalized = " ".join(str(text or "").casefold().split())
    patterns = (
        r"\b(?:for|lasting)\s+(\d{1,2})\s*(?:seconds?|secs?|s)\b",
        r"\b(?:total|length)\s+(?:of\s+)?(\d{1,2})\s*(?:seconds?|secs?|s)\b",
        r"\bmake\s+it\s+(\d{1,2})\s*(?:seconds?|secs?|s)\b",
        r"\bmake\s+(?:a\s+)?(\d{1,2})[-\s]*(?:second|sec|s)\s+(?:edit|video)\b",
        r"\b(?:want|need|prefer)\s+(?:a\s+)?(\d{1,2})[-\s]*(?:second|sec|s)\s+"
        r"(?:edit|video)\b",
    )
    for pattern in patterns:
        match = re.search(pattern, normalized)
        if match is not None:
            value = int(match.group(1))
            return value if 3 <= value <= 60 else None
    return None


def recognize_explicit_cadence_reuse_policy(
    text: str,
) -> Literal["no_repeat", "allow_repeat"] | None:
    """Recognize an explicit reuse decision, preserving prior intent when absent."""

    normalized = " ".join(str(text or "").casefold().split())
    if re.search(
        r"\b(?:do not|don't|dont|without|never|not)\b.{0,40}\b(?:repeat|reuse|loop)\b",
        normalized,
    ):
        return "no_repeat"
    if re.match(r"(?:please\s+)?(?:repeat|reuse|loop)\b", normalized) or re.search(
        r"\b(?:allow|okay to|ok to|can|may)\b.{0,64}\b(?:repeat|reuse|loop)\b",
        normalized,
    ):
        return "allow_repeat"
    return None


def recognize_cadence_reuse_policy(text: str) -> Literal["no_repeat", "allow_repeat"]:
    """Require an explicit opt-in before source windows may repeat."""

    return recognize_explicit_cadence_reuse_policy(text) or "no_repeat"


class MixedMediaTimingProfile(BaseModel):
    """Typed timing intent for mixed photo/video edits.

    The profile is optional so old proposals keep their exact timing behavior.
    Values describe preferences, not permission to exceed source duration.
    """

    image_hold: Literal["very_fast", "standard"] = "standard"
    # Optional exact still duration requested by the creator.  Omitted keeps
    # the legacy very_fast 0.5–0.8s bounds; numeric values are intentionally
    # narrow and never affect video minimums.
    image_hold_s: float | None = Field(
        default=None,
        ge=0.1,
        le=0.8,
        exclude_if=lambda value: value is None,
    )
    video_hold: Literal["longer", "standard"] = "standard"
    boundary_style: Literal["cut", "crossfade"] = "crossfade"
    image_grouping: ImageGrouping = Field(
        default="scattered",
        exclude_if=lambda value: value == "scattered",
    )
    sequence_grouping: SequenceGrouping = Field(
        default="none",
        exclude_if=lambda value: value == "none",
    )
    sequence_group_order: list[
        Literal["football", "basketball", "beach_volleyball", "tennis", "track_and_field"]
    ] = Field(default_factory=list, max_length=5, exclude_if=lambda value: not value)


@dataclass(frozen=True)
class MixedMediaHoldBounds:
    """One source of truth for the compiled quick-photo/long-video policy."""

    minimum_s: float
    preferred_s: float
    maximum_s: float


MIXED_MEDIA_IMAGE_HOLD = MixedMediaHoldBounds(0.5, 0.65, 0.8)
MIXED_MEDIA_VIDEO_HOLD = MixedMediaHoldBounds(1.5, 2.0, 3.0)


def mixed_media_hold_bounds(
    kind: MediaKind,
    profile: MixedMediaTimingProfile | None = None,
) -> MixedMediaHoldBounds:
    """Return the approved bounds for one media kind."""

    if kind == "image" and profile is not None and profile.image_hold_s is not None:
        return MixedMediaHoldBounds(
            profile.image_hold_s, profile.image_hold_s, profile.image_hold_s
        )
    return MIXED_MEDIA_IMAGE_HOLD if kind == "image" else MIXED_MEDIA_VIDEO_HOLD


def uses_quick_photo_long_video_timing(
    profile: MixedMediaTimingProfile | None,
) -> bool:
    """Return whether the profile authorizes the expanded per-kind bounds."""

    return bool(
        profile is not None
        and profile.image_hold == "very_fast"
        and profile.video_hold == "longer"
        and profile.boundary_style == "cut"
    )


def recognize_mixed_media_timing(text: str) -> MixedMediaTimingProfile | None:
    """Recognize an affirmative quick-photo/long-video request without trusting an LLM."""

    normalized = " ".join(str(text or "").casefold().split())
    photo = r"(?:photos?|images?|stills?|pictures?)"
    photo_fast = r"(?:very fast|faster|quick(?:ly)?|snappy|rapid|flash(?: by)?)"
    video = r"(?:videos?|clips?|footage)"
    video_long = r"(?:longer|more time|breathe|linger|hold|slower)"
    negation = r"(?:do not|don't|dont|never|should not|shouldn't|not)"

    image_grouping: ImageGrouping = "scattered"
    if re.search(
        rf"\bgroups?\s+of\s+{photo}\b"
        rf"|\b(?:group|grouped|grouping)\b.{{0,28}}\b{photo}\b"
        rf"|\b{photo}\b.{{0,28}}\b(?:group|grouped|grouping|sections?)\b",
        normalized,
    ):
        image_grouping = "runs"

    ordered_group_patterns: tuple[tuple[str, str], ...] = (
        ("football", r"\b(?:football|soccer)\b"),
        ("basketball", r"\bbasketball\b"),
        ("beach_volleyball", r"\b(?:beach\s+volleyball|volleyball)\b"),
        ("tennis", r"\btennis\b"),
        ("track_and_field", r"\b(?:track(?:\s+and\s+field)?|hurdles?)\b"),
    )
    ordered_matches = [
        (match.start(), group)
        for group, pattern in ordered_group_patterns
        if (match := re.search(pattern, normalized)) is not None
    ]
    ordered_matches.sort()
    sequence_group_order = list(dict.fromkeys(group for _, group in ordered_matches))
    grouping_language = bool(re.search(r"\b(?:group|grouped|grouping|sequentially)\b", normalized))
    sequence_grouping: SequenceGrouping = (
        "sport_context"
        if (
            re.search(
                r"\b(?:group|grouped|grouping)\b.{0,100}\b(?:sports?|context|background)\b"
                r"|\b(?:sports?|context|background)\b.{0,100}\b(?:group|grouped|grouping)\b",
                normalized,
            )
            or (grouping_language and len(sequence_group_order) >= 2)
        )
        else "none"
    )

    sequence_fields = {
        "image_grouping": image_grouping,
        "sequence_grouping": sequence_grouping,
        "sequence_group_order": sequence_group_order if sequence_grouping != "none" else [],
    }

    def paired(left: str, right: str, *, gap: int = 48) -> bool:
        return bool(
            re.search(rf"\b{left}\b.{{0,{gap}}}\b{right}\b", normalized)
            or re.search(rf"\b{right}\b.{{0,{gap}}}\b{left}\b", normalized)
        )

    def negated(media: str, timing: str) -> bool:
        return bool(
            re.search(
                rf"\b{negation}\b.{{0,24}}\b{media}\b.{{0,24}}\b{timing}\b",
                normalized,
            )
            or re.search(
                rf"\b{media}\b.{{0,24}}\b{negation}\b.{{0,24}}\b{timing}\b",
                normalized,
            )
        )

    # Preserve an explicit numeric still cadence while keeping generic
    # "very fast" requests on the established 0.5–0.8s contract. Parse this
    # before the generic paired form so a request containing both "very fast"
    # and an exact value does not lose the value.
    numeric_matches = list(
        re.finditer(
            rf"\b{photo}\b.{{0,80}}?\b(0?\.[0-9]+|[1-9][0-9]{{0,2}})\s*(?:s|sec(?:ond)?s?|ms|milliseconds?)\b",
            normalized,
        )
    )
    valid_numeric_matches: list[tuple[re.Match[str], float]] = []
    for match in numeric_matches:
        value = float(match.group(1))
        if "ms" in match.group(0) or "millisecond" in match.group(0):
            value /= 1000
        if 0.1 <= value <= 0.8:
            valid_numeric_matches.append((match, value))
    # Conversation history is passed oldest-to-newest. A correction such as
    # "make the images 0.2 seconds instead of 0.1" must override the earlier
    # 0.1-second request rather than silently reusing the first regex match.
    numeric_match, numeric_value = (
        valid_numeric_matches[-1] if valid_numeric_matches else (None, None)
    )
    has_video_context = bool(re.search(rf"\b{video}\b", normalized))
    selected_numeric_context = (
        normalized[max(0, numeric_match.start() - 40) : numeric_match.end()]
        if numeric_match is not None
        else ""
    )
    numeric_negated = bool(
        re.search(
            rf"\b{negation}\b.{{0,40}}\b{photo}\b",
            selected_numeric_context,
        )
    )
    if numeric_match and has_video_context and not numeric_negated:
        return MixedMediaTimingProfile(
            image_hold="very_fast",
            image_hold_s=numeric_value,
            video_hold="longer",
            boundary_style="cut",
            **sequence_fields,
        )

    if (
        paired(photo, photo_fast)
        and paired(video, video_long)
        and not negated(photo, photo_fast)
        and not negated(video, video_long)
    ):
        return MixedMediaTimingProfile(
            image_hold="very_fast",
            video_hold="longer",
            boundary_style="cut",
            **sequence_fields,
        )
    return None


def media_context_group(*values: object) -> MediaContextGroup:
    """Classify approved metadata into a bounded sequencing chapter.

    This never creates on-screen copy. It only groups source IDs using trusted
    analysis already attached to the proposal, so deterministic fallback can
    preserve an explicit sport/context ordering request.
    """

    normalized_values = [" ".join(str(value or "").casefold().split()) for value in values]
    # Fields are passed in trust/specificity order (creator context, concise
    # subject, then broader description). Honor the first explicit sport so a
    # verbose description mentioning a nearby court cannot override a subject
    # already identified as soccer, for example.
    sport_patterns: tuple[tuple[MediaContextGroup, str], ...] = (
        ("beach_volleyball", r"\b(?:beach\s+volleyball|volleyball|sand\s+court)\b"),
        ("basketball", r"\bbasketball\b"),
        ("football", r"\b(?:football|soccer)\b"),
        ("tennis", r"\btennis\b"),
        ("track_and_field", r"\b(?:track(?:\s+and\s+field)?|hurdles?)\b"),
    )
    for value in normalized_values:
        for group, pattern in sport_patterns:
            if re.search(pattern, value):
                return group
    normalized = " ".join(normalized_values)
    if re.search(r"\b(?:sand|beach)\b", normalized):
        return "beach_context"
    if re.search(r"\b(?:grass|field|lawn|mud|muddy)\b", normalized):
        return "field_context"
    if re.search(r"\b(?:bench|bleachers?|sidelines?)\b", normalized):
        return "sidelines"
    if re.search(r"\bcourt\b", normalized):
        return "court_context"
    if re.search(r"\b(?:group|friends?|people|person|man|woman|girl|boy)\b", normalized):
        return "people_context"
    return "other"


def recognize_image_layout(text: str) -> BeatLayout | None:
    """Recognize the creator's latest explicit photo fill/fit instruction.

    ``supporting_card`` is the existing guided-render contract that keeps the
    entire image visible against a blurred canvas. ``fullscreen`` keeps the
    established cover-crop behavior. Returning ``None`` preserves legacy
    proposals byte-for-byte when the creator did not state a preference.
    """

    normalized = " ".join(str(text or "").casefold().split())
    candidates: list[tuple[int, BeatLayout]] = []
    contain_patterns = (
        r"\b(?:do not|don't|dont|never|should not|shouldn't)\b.{0,24}"
        r"\b(?:photos?|images?|stills?|pictures?)\b.{0,24}\b(?:fill|cover)\b"
        r".{0,12}\b(?:the\s+)?screen\b",
        r"\b(?:fit|show)\b.{0,24}\b(?:whole|entire|full)?\s*"
        r"(?:photos?|images?|stills?|pictures?)\b.{0,32}"
        r"\b(?:completely|uncropped|without\s+cropping)\b",
        r"\b(?:photos?|images?|stills?|pictures?)\b.{0,32}"
        r"\b(?:fit\s+completely|without\s+cropping|uncropped|not\s+cropped)\b",
        r"\b(?:do not|don't|dont|never)\s+crop\b.{0,16}"
        r"\b(?:photos?|images?|stills?|pictures?)\b",
    )
    cover_patterns = (
        r"(?<!do not )(?<!don't )(?<!dont )(?<!never )(?<!shouldn't )"
        r"\b(?:make|let|have)\b.{0,16}\b(?:photos?|images?|stills?|pictures?)\b"
        r".{0,16}\b(?:fill|cover)\b.{0,12}\b(?:the\s+)?screen\b",
        r"\b(?:fullscreen|full-screen)\s+(?:photos?|images?|stills?|pictures?)\b",
        r"\b(?:photos?|images?|stills?|pictures?)\s+(?:fullscreen|full-screen)\b",
        r"\bcrop\b.{0,16}\b(?:photos?|images?|stills?|pictures?)\b"
        r".{0,16}\b(?:to\s+)?fill\b",
        r"\b(?:photos?|images?|stills?|pictures?)\b.{0,80}"
        r"\b(?:the\s+same\s+way\s+as|exactly\s+like|just\s+(?:like|how))\b.{0,32}"
        r"\b(?:landscape\s+)?videos?\b",
        r"\b(?:photos?|images?|stills?|pictures?)\b.{0,64}"
        r"\b(?:without|no)\b.{0,24}"
        r"\b(?:card|black\s+bars?|blur(?:red|ry)?\s+background)\b",
    )
    for pattern in contain_patterns:
        candidates.extend(
            (match.start(), "supporting_card") for match in re.finditer(pattern, normalized)
        )
    for pattern in cover_patterns:
        candidates.extend(
            (match.start(), "fullscreen") for match in re.finditer(pattern, normalized)
        )
    return max(candidates, key=lambda candidate: candidate[0])[1] if candidates else None


# A plan item may contain up to 50 source clips and 100 visual-pool assets.
# Guided-edit snapshots must preserve the complete deduplicated set so a
# Main Creator-confirmed plan never fails after the product has accepted it.
MAX_EDIT_PROPOSAL_MEDIA = 150
GUIDED_STORY_MIN_MOMENT_S = 1.4
MAIN_CREATOR_FAIL_CLOSED = "main_creator_fail_closed"
# Who/what approved a proposal — "auto" for AI-designs-by-default
# (GUIDED_AUTO_DESIGN_ENABLED); "user" for an explicit creator approval.
ApprovalMode = Literal["user", "auto"]


class MediaRef(BaseModel):
    """One exact media identity from either existing storage lane."""

    lane: MediaLane
    media_id: str = Field(min_length=1, max_length=100)
    gcs_path: str = Field(min_length=1)
    generation: str = Field(min_length=1)
    kind: MediaKind
    source_filename: str = ""
    duration_s: float | None = Field(default=None, gt=0)
    aspect: float | None = Field(default=None, gt=0)
    content_hash: str | None = None
    user_context: str = ""
    analysis: dict = Field(default_factory=dict)


class StoryBeat(BaseModel):
    beat_id: str = Field(min_length=1, max_length=100)
    topic: str = Field(min_length=1, max_length=80)
    thought: str = Field(default="", max_length=280)
    thought_source: ThoughtSource = "ai_draft"
    media_ids: list[str] = Field(min_length=1, max_length=4)
    layout: BeatLayout = "fullscreen"
    duration_s: float = Field(ge=1.0, le=12.0)


class FastMontageCut(BaseModel):
    """One enforceable, source-aware cut in a new fast-montage proposal.

    Legacy fast-montage snapshots omit ``fast_cuts`` and continue through the
    story-beat compiler. New planner output always supplies this contract.
    """

    cut_id: str = Field(min_length=1, max_length=100)
    media_id: str = Field(min_length=1, max_length=100)
    source_start_s: float = Field(ge=0)
    source_end_s: float = Field(gt=0)
    output_duration_s: float = Field(ge=0.1, le=3.0)
    role: Literal["hook", "build", "payoff"]
    transition: Literal["none"] = "none"
    beat_align: bool = False

    @model_validator(mode="after")
    def validate_source_window(self) -> FastMontageCut:
        if self.source_end_s <= self.source_start_s:
            raise ValueError("fast montage cut source_end_s must exceed source_start_s")
        source_duration_s = self.source_end_s - self.source_start_s
        if abs(self.output_duration_s - source_duration_s) > 0.001:
            raise ValueError("fast montage output duration must match its source window")
        return self


class DirectionHypothesis(BaseModel):
    direction: ProposalDirection
    pace: ProposalPace
    duration_s: int = Field(ge=3, le=60)
    text_density: TextDensity
    audio_role: AudioRole
    rationale: str = Field(min_length=1, max_length=600)
    buildability_warnings: list[str] = Field(default_factory=list, max_length=5)


class ProposalGuidance(BaseModel):
    state: DirectionGuidanceState
    provenance: DirectionProvenance
    hypothesis: DirectionHypothesis
    fingerprint: str = Field(min_length=64, max_length=64)


class EditProposalSnapshot(BaseModel):
    direction: ProposalDirection = "guided_story"
    goal: str = Field(default="", max_length=500)
    pace: ProposalPace = "balanced"
    # No artificial floor — see ProposalBrief.duration_s.
    duration_s: int = Field(ge=3, le=60)
    title: str = Field(min_length=1, max_length=100)
    # Confirmed Main Creator typography is part of the immutable proposal
    # snapshot, so the async worker cannot replace it with generated copy.
    opening_title: str | None = Field(
        default=None,
        max_length=280,
        exclude_if=lambda value: value is None,
    )
    font_family: str | None = Field(
        default=None,
        max_length=160,
        exclude_if=lambda value: value is None,
    )
    text_color: str | None = Field(
        default=None,
        max_length=16,
        exclude_if=lambda value: value is None,
    )
    image_layout: BeatLayout | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
        description=(
            "Confirmed image-only layout. supporting_card preserves the full image; "
            "fullscreen uses the established cover crop."
        ),
    )

    @field_validator("font_family")
    @classmethod
    def _validate_font_family(cls, value: str | None) -> str | None:
        if value is None:
            return None
        # Reuse the renderer's registry at validation time; this avoids a
        # second font allowlist drifting from TextElement.
        from app.agents._schemas.text_element import _ALLOWED_FONTS  # noqa: PLC0415

        candidate = value.strip()
        canonical = next(
            (font for font in _ALLOWED_FONTS if font.casefold() == candidate.casefold()),
            None,
        )
        if canonical is None:
            raise ValueError("font_family must be a known font registry key")
        return canonical

    @field_validator("text_color")
    @classmethod
    def _validate_text_color(cls, value: str | None) -> str | None:
        if value is None:
            return None
        candidate = value.strip().lower()
        aliases = {"yellow": "#FFD24A", "gold": "#F4D03F", "white": "#FFFFFF", "black": "#000000"}
        if candidate in aliases:
            return aliases[candidate]
        if not re.fullmatch(r"#[0-9a-f]{6}", candidate):
            raise ValueError("text_color must be a #RRGGBB hex string or supported color alias")
        return candidate.upper()

    media: list[MediaRef] = Field(min_length=1, max_length=MAX_EDIT_PROPOSAL_MEDIA)
    story_beats: list[StoryBeat] = Field(min_length=1, max_length=20)
    fast_cuts: list[FastMontageCut] | None = Field(default=None, min_length=1, max_length=80)
    mixed_media_timing: MixedMediaTimingProfile | None = None
    montage_text_bindings: list[MontageTextBinding] = Field(default_factory=list, max_length=12)
    montage_audio: MontageAudioPlan | None = None
    montage_cadence: MontageCadenceConstraint | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )
    output_orientation: OutputOrientation | None = None
    output_orientation_reason: str = Field(default="", max_length=240)

    @model_validator(mode="after")
    def validate_beat_media(self) -> EditProposalSnapshot:
        known = {m.media_id for m in self.media}
        if len(known) != len(self.media):
            raise ValueError("proposal media IDs must be unique")
        for beat in self.story_beats:
            missing = set(beat.media_ids) - known
            if missing:
                raise ValueError(f"beat {beat.beat_id} references missing media IDs")
        if len({b.beat_id for b in self.story_beats}) != len(self.story_beats):
            raise ValueError("story beat IDs must be unique")
        if self.fast_cuts:
            if self.direction != "fast_montage":
                raise ValueError("fast cuts are only valid for fast montage proposals")
            cut_ids = {cut.cut_id for cut in self.fast_cuts}
            if len(cut_ids) != len(self.fast_cuts):
                raise ValueError("fast montage cut IDs must be unique")
            missing = {cut.media_id for cut in self.fast_cuts} - known
            if missing:
                raise ValueError("fast montage cuts reference missing media IDs")
            by_id = {ref.media_id: ref for ref in self.media}
            quick_mixed_timing = uses_quick_photo_long_video_timing(self.mixed_media_timing)
            if (
                self.montage_cadence is None
                and not quick_mixed_timing
                and any(cut.output_duration_s > 1.2 + 0.001 for cut in self.fast_cuts)
            ):
                raise ValueError(
                    "fast montage cuts above 1.2s require the mixed-media timing profile"
                )
            cut_kinds = {by_id[cut.media_id].kind for cut in self.fast_cuts}
            # Only require both lanes when each has at least one source that
            # can honor the approved timing profile. A video shorter than the
            # 1.5s "longer" minimum cannot satisfy the user's cadence and must
            # not make an otherwise valid photo montage impossible.
            mixed_video_minimum_s = mixed_media_hold_bounds(
                "video", self.mixed_media_timing
            ).minimum_s
            available_kinds = {
                ref.kind
                for ref in self.media
                if ref.kind == "image"
                or (ref.duration_s is not None and ref.duration_s >= mixed_video_minimum_s - 0.001)
            }
            if quick_mixed_timing and len(available_kinds) > 1 and cut_kinds != available_kinds:
                raise ValueError("mixed-media timing must use both photos and videos")
            image_ids: set[str] = set()
            video_windows: dict[str, list[tuple[float, float, float]]] = {}
            for cut in self.fast_cuts:
                ref = by_id[cut.media_id]
                if ref.kind == "image":
                    bounds = mixed_media_hold_bounds(ref.kind, self.mixed_media_timing)
                    if quick_mixed_timing and not (
                        bounds.minimum_s - 0.001
                        <= cut.output_duration_s
                        <= bounds.maximum_s + 0.001
                    ):
                        raise ValueError(
                            "mixed-media photos must stay within the approved still hold"
                        )
                    if quick_mixed_timing and ref.media_id in image_ids:
                        raise ValueError("mixed-media photos may only be used once")
                    image_ids.add(ref.media_id)
                    continue
                minimum_video_s = 0.1 if quick_mixed_timing else 0.4
                if cut.output_duration_s < minimum_video_s - 0.001:
                    raise ValueError(
                        f"fast montage video cuts must be at least {minimum_video_s:g}s"
                    )
                if ref.duration_s is None or cut.source_end_s > ref.duration_s + 0.001:
                    raise ValueError("fast montage cut exceeds its server-owned video duration")
                video_windows.setdefault(ref.media_id, []).append(
                    (cut.source_start_s, cut.source_end_s, cut.output_duration_s)
                )
            for media_id, windows in video_windows.items():
                ordered = sorted(windows)
                if not (
                    self.montage_cadence is not None
                    and self.montage_cadence.reuse_policy == "allow_repeat"
                ) and any(
                    current[0] < previous[1] - 0.001
                    for previous, current in zip(ordered, ordered[1:], strict=False)
                ):
                    raise ValueError("fast montage video windows must not overlap")
                if quick_mixed_timing:
                    source_duration_s = float(by_id[media_id].duration_s or 0.0)
                    bounds = mixed_media_hold_bounds("video", self.mixed_media_timing)
                    total_used_s = sum(window[2] for window in windows)
                    for _start_s, _end_s, output_duration_s in windows:
                        remaining_for_cut_s = source_duration_s - (total_used_s - output_duration_s)
                        if (
                            remaining_for_cut_s >= bounds.minimum_s - 0.001
                            and output_duration_s < bounds.minimum_s - 0.001
                        ) or output_duration_s > bounds.maximum_s + 0.001:
                            raise ValueError(
                                "mixed-media videos must hold for 1.5-3.0s when source permits"
                            )
            available_image_count = sum(ref.kind == "image" for ref in self.media)
            required_image_count = min(3, available_image_count)
            if quick_mixed_timing and len(image_ids) < required_image_count:
                raise ValueError(
                    "mixed-media timing must use up to three distinct photos when available"
                )
            if self.fast_cuts[0].role != "hook":
                raise ValueError("fast montage must open with a hook cut")
            if any(cut.transition != "none" for cut in self.fast_cuts):
                raise ValueError("fast montage cuts must use hard cuts")
            cut_duration_s = sum(cut.output_duration_s for cut in self.fast_cuts)
            if abs(cut_duration_s - self.duration_s) > 0.15:
                raise ValueError("fast montage cut durations must match the proposal duration")
        if self.montage_cadence is not None:
            if self.direction != "fast_montage":
                raise ValueError("montage cadence is only valid for fast montage proposals")
            cadence = self.montage_cadence
            if not set(cadence.source_media_ids) <= known:
                raise ValueError("montage cadence references unknown media")
            if not self.fast_cuts:
                raise ValueError("montage cadence requires fast cuts")
            expected = cadence.source_media_ids
            if len(self.fast_cuts) % len(expected):
                raise ValueError("round-robin cadence must contain complete source cycles")
            for index, cut in enumerate(self.fast_cuts):
                if cut.media_id != expected[index % len(expected)]:
                    raise ValueError("fast montage cuts must preserve round-robin source order")
                if abs(cut.output_duration_s - cadence.cut_duration_s) > 0.001:
                    raise ValueError("fast montage cuts must preserve the exact cadence duration")
        text_ids = [binding.media_id for binding in self.montage_text_bindings]
        if len(text_ids) != len(set(text_ids)):
            raise ValueError("montage text bindings must use unique source IDs")
        if not set(text_ids) <= known:
            raise ValueError("montage text binding references unknown media")
        if self.montage_audio is not None:
            audio_ids = set(self.montage_audio.source_media_ids)
            if not audio_ids <= known:
                raise ValueError("montage audio references unknown media")
            if any(by_id[media_id].kind != "video" for media_id in audio_ids):
                raise ValueError("montage audio beds require video sources")
        if self.output_orientation is None:
            orientation, reason = infer_story_output_orientation(self)
            self.output_orientation = orientation
            self.output_orientation_reason = reason
        elif not self.output_orientation_reason:
            self.output_orientation_reason = CREATOR_SELECTED_ORIENTATION_REASON
        return self


def _media_aspect(ref: MediaRef) -> float | None:
    """Return analyzed DISPLAY width/height without guessing from filenames.

    display_width/display_height (autoplace ANALYSIS_VERSION 6+) are rotation
    aware; ref.aspect and analysis width/height persisted by older analyses are
    CODED pixels, so a -90 iPhone portrait clip reads there as 1.78 landscape.
    """
    analysis = ref.analysis if isinstance(ref.analysis, dict) else {}
    try:
        dw = float(analysis.get("display_width") or 0)
        dh = float(analysis.get("display_height") or 0)
        if dw > 0 and dh > 0:
            return dw / dh
    except (TypeError, ValueError):
        pass
    if ref.aspect is not None and math.isfinite(ref.aspect) and ref.aspect > 0:
        return float(ref.aspect)
    try:
        width = float(analysis.get("width") or 0)
        height = float(analysis.get("height") or 0)
    except (TypeError, ValueError):
        return None
    if width <= 0 or height <= 0:
        return None
    return width / height


def infer_story_output_orientation(
    snapshot: EditProposalSnapshot,
) -> tuple[OutputOrientation, str]:
    """Choose a canvas from approved story exposure, not unused uploaded media.

    Each beat's approved duration is split evenly across its selected sources.
    Landscape (>1.05) and portrait (<0.95) exposure vote by that duration;
    near-square sources are neutral. A tie follows the first selected non-square
    source so the opening remains the deterministic creative tie-breaker.
    """

    by_id = {ref.media_id: ref for ref in snapshot.media}
    landscape_s = 0.0
    portrait_s = 0.0
    first_non_square: OutputOrientation | None = None
    usable = 0
    for beat in snapshot.story_beats:
        weight = float(beat.duration_s) / len(beat.media_ids)
        for media_id in beat.media_ids:
            aspect = _media_aspect(by_id[media_id])
            if aspect is None or 0.95 <= aspect <= 1.05:
                continue
            orientation: OutputOrientation = "landscape" if aspect > 1.05 else "portrait"
            first_non_square = first_non_square or orientation
            usable += 1
            if orientation == "landscape":
                landscape_s += weight
            else:
                portrait_s += weight
    if landscape_s > portrait_s:
        selected: OutputOrientation = "landscape"
    elif portrait_s > landscape_s:
        selected = "portrait"
    elif first_non_square is not None:
        selected = first_non_square
    else:
        selected = "portrait"
    reason = (
        f"Auto-selected {selected} from approved story media: "
        f"{landscape_s:.1f}s landscape, {portrait_s:.1f}s portrait; "
        f"{usable} non-square source selections."
    )
    if usable == 0:
        reason = (
            "Auto-selected portrait because the approved story has no usable "
            "non-square aspect metadata."
        )
    return selected, reason


class ApprovedProposalSnapshot(BaseModel):
    proposal_version: int = Field(ge=1)
    media_digest: str = Field(min_length=64, max_length=64)
    approved_at: datetime
    snapshot: EditProposalSnapshot
    # Recorded distinctly from the envelope's mutable EditProposal.approval_mode
    # (which a later reservation can overwrite) so an approved-and-rendered
    # story permanently remembers whether a human or the auto-design flow
    # approved it. None = legacy approvals predating this field (treat as "user").
    approval_mode: ApprovalMode | None = None


class ProposalFailure(BaseModel):
    code: str = Field(min_length=1, max_length=100)
    message: str = Field(min_length=1, max_length=500)
    retryable: bool = True
    # Admin/debug-only diagnostic (exception type + short reason). Never shown
    # to end users — _edit_proposal_response() strips this key before the
    # public PlanItem response is built (routes/plan_items.py).
    detail: str | None = Field(default=None, max_length=2000)


class ProposalRenderFailure(BaseModel):
    """An APPROVED plan that the strict renderer could not execute.

    Scoped to the approved proposal_version that failed, so any new revision
    (which bumps last_approved.proposal_version) clears the block automatically
    with no extra bookkeeping.
    """

    proposal_version: int = Field(ge=1)
    code: str = Field(min_length=1, max_length=100)
    message: str = Field(min_length=1, max_length=500)
    attempts: int = Field(default=1, ge=1)
    failed_at: datetime


class ProposalBrief(BaseModel):
    direction: ProposalDirection = "guided_story"
    goal: str = Field(default="", max_length=500)
    pace: ProposalPace = "balanced"
    # No artificial floor: the planner adapts the story length to whatever
    # footage is actually available (draft_edit_proposal clamps this against
    # analyzed media before it reaches the agent). See agents/DECISIONS.md.
    duration_s: int = Field(default=24, ge=3, le=60)
    creator_request: str = Field(default="", max_length=1000)
    # Confirmed Main Creator fields copied into the immutable snapshot by the
    # async proposal worker.
    opening_title: str | None = Field(default=None, max_length=280)
    font_family: str | None = Field(default=None, max_length=160)
    text_color: str | None = Field(default=None, max_length=16)
    image_layout: BeatLayout | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )

    @field_validator("font_family")
    @classmethod
    def _validate_font_family(cls, value: str | None) -> str | None:
        if value is None:
            return None
        from app.agents._schemas.text_element import _ALLOWED_FONTS  # noqa: PLC0415

        candidate = value.strip()
        canonical = next(
            (font for font in _ALLOWED_FONTS if font.casefold() == candidate.casefold()),
            None,
        )
        if canonical is None:
            raise ValueError("font_family must be a known font registry key")
        return canonical

    @field_validator("text_color")
    @classmethod
    def _validate_text_color(cls, value: str | None) -> str | None:
        if value is None:
            return None
        candidate = value.strip().lower()
        aliases = {"yellow": "#FFD24A", "gold": "#F4D03F", "white": "#FFFFFF", "black": "#000000"}
        if candidate in aliases:
            return aliases[candidate]
        if not re.fullmatch(r"#[0-9a-f]{6}", candidate):
            raise ValueError("text_color must be a #RRGGBB hex string or supported color alias")
        return candidate.upper()

    mixed_media_timing: MixedMediaTimingProfile | None = None
    montage_audio: MontageAudioPlan | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )
    montage_cadence: MontageCadenceConstraint | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )
    # Main Creator can pin the short-form delivery canvas without changing
    # ordinary guided-edit orientation inference. None preserves legacy briefs.
    output_orientation: OutputOrientation | None = None


class EditConversationTurn(BaseModel):
    """One durable turn in the edit-direction conversation."""

    role: ConversationRole
    phase: ConversationPhase = "briefing"
    content: str = Field(min_length=1, max_length=1000)
    suggestions: list[ConversationSuggestion] = Field(default_factory=list, max_length=3)


class EditConversationAttempt(BaseModel):
    """Short-lived single-flight fence around one paid edit-guide call."""

    token: str = Field(min_length=1, max_length=100)
    expected_proposal_version: int = Field(ge=0)
    reserved_proposal_version: int = Field(ge=1)
    started_at: datetime
    placeholder: bool = False


class EditProposal(BaseModel):
    schema_version: Literal[1] = 1
    proposal_version: int = Field(ge=1)
    generation_attempt_id: str = Field(min_length=1, max_length=100)
    # Set when the heavy analysis/planning phase actually starts, after any
    # asset-readiness retries. Creator reconciliation uses this to avoid
    # expiring a queued attempt before its worker task budget has elapsed.
    planning_started_at: datetime | None = None
    media_digest: str | None = Field(default=None, min_length=64, max_length=64)
    status: ProposalStatus
    # Who/what approved this attempt — "auto" for AI-designs-by-default
    # (GUIDED_AUTO_DESIGN_ENABLED); None/"user" for an explicit creator
    # approval. Set when the attempt is reserved (begin_proposal_attempt) and
    # carried through to ApprovedProposalSnapshot.approval_mode on approval.
    approval_mode: ApprovalMode | None = None
    guidance: ProposalGuidance | None = None
    brief: ProposalBrief = Field(default_factory=ProposalBrief)
    conversation: list[EditConversationTurn] = Field(
        default_factory=list, max_length=EDIT_CONVERSATION_MAX_TURNS
    )
    brief_ready: bool = False
    conversation_attempt: EditConversationAttempt | None = None
    draft: EditProposalSnapshot | None = None
    last_approved: ApprovedProposalSnapshot | None = None
    failure: ProposalFailure | None = None
    # GUIDED_AUTO_DESIGN_ENABLED fallback disposition. Normally set after a
    # failure to the code that triggered a legacy montage. Main Creator sets
    # MAIN_CREATOR_FAIL_CLOSED before enqueue so both current and rolling old
    # workers see a non-null value and refuse the generic fallback.
    design_fallback: str | None = Field(default=None, max_length=100)
    # Set when an APPROVED proposal's strict render fails inside guided_story.py
    # (GuidedStoryError). Scoped to last_approved.proposal_version so a new
    # approval clears it automatically. See services/edit_proposals.py
    # (record_proposal_render_failure / guided_render_is_blocked).
    render_failure: ProposalRenderFailure | None = None


class MediaRefResponse(MediaRef):
    """Media identity plus its short-lived, response-only preview URL."""

    preview_url: str | None = None


class EditProposalSnapshotResponse(EditProposalSnapshot):
    media: list[MediaRefResponse] = Field(min_length=1, max_length=MAX_EDIT_PROPOSAL_MEDIA)


class ApprovedProposalSnapshotResponse(ApprovedProposalSnapshot):
    snapshot: EditProposalSnapshotResponse


class EditProposalResponse(EditProposal):
    """OpenAPI-visible proposal envelope returned by plan-item endpoints."""

    # Attempt tokens are internal write fences. Responses expose only safe UI
    # state so a browser can resume after a reload without learning the token.
    conversation_attempt: None = None
    conversation_in_progress: bool = False
    conversation_retry_required: bool = False
    draft: EditProposalSnapshotResponse | None = None
    last_approved: ApprovedProposalSnapshotResponse | None = None


def canonical_media_digest(media: list[MediaRef]) -> str:
    """Hash only immutable media identities; editorial order is not media state."""

    identities = sorted(
        (
            {
                "lane": ref.lane,
                "media_id": ref.media_id,
                "gcs_path": ref.gcs_path,
                "generation": ref.generation,
                "kind": ref.kind,
                "content_hash": ref.content_hash or "",
            }
            for ref in media
        ),
        key=lambda row: (row["lane"], row["media_id"]),
    )
    payload = json.dumps(identities, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def parse_edit_proposal(value: object) -> EditProposal | None:
    """Fail closed for legacy/corrupt JSONB instead of breaking item reads."""

    if not isinstance(value, dict):
        return None
    try:
        return EditProposal.model_validate(value)
    except Exception:  # noqa: BLE001 - corrupted JSONB is treated as no proposal
        return None
