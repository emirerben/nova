"""Strict first-class renderer for approved guided-edit proposals.

The approved proposal is the render program. This module never calls the legacy
montage matcher and never drops a selected source or text layer as a fallback.
"""

from __future__ import annotations

import hashlib
import math
import os
import shutil
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Literal

import structlog
from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.agents._schemas.text_element import CAPTION_CUE_SOURCE, TextElement
from app.config import settings
from app.pipeline.canvas import LANDSCAPE, PORTRAIT, Canvas
from app.pipeline.duration_contract import (
    STRICT_MIXED_MEDIA_DURATION_TOLERANCE_S,
    STRICT_MIXED_MEDIA_MAX_CFR_OVERRUN_S,
)
from app.pipeline.probe import probe_video
from app.schemas.edit_proposal import (
    FAST_MONTAGE_TITLE_HOLD_S,
    GUIDED_STORY_MIN_MOMENT_S,
    GUIDED_TITLE_HOLD_S,
    EditProposalSnapshot,
    MixedMediaTimingProfile,
    MontageCadenceConstraint,
    NarrationTrack,
    canonical_media_digest,
    canonical_narration_duration_s,
    closing_title_hold_s,
    mixed_media_hold_bounds,
    uses_quick_photo_long_video_timing,
)

log = structlog.get_logger()

# v7: a fast montage carries the creator's `montage_audio` choice into the plan
# (v6 dropped it, so "keep original audio" rendered muted). Stored plans are
# re-validated by recompiling at their own version, so this is a new version
# rather than a change to v6.
COMPILER_VERSION = 7
VOICEOVER_COMPILER_VERSION = 7
# Only snapshots carrying an approved frame schedule opt into v8.
SCHEDULED_COMPILER_VERSION = 8
VARIANT_ID = "guided_story"
_FRAME_S = 1.0 / 30.0
_ALLOCATION_EPSILON_S = 0.0005
_FRAME_FLOOR_EPSILON_S = 1e-9
_DURATION_MATCH_TOLERANCE_S = 0.001
# Shortest window beat copy is trimmed to when it would otherwise overlap the
# creator's opening/closing title; below this the full beat window is kept so
# confirmed copy is never dropped.
_MIN_CLEAR_TEXT_WINDOW_S = 0.5
_MEDIA_PREP_MAX_WORKERS = 3
# Transparent photos are matted over this opaque colour before FFmpeg sees
# them. The iPhone engine mirrors it (StillFrame.flattened in
# src/apps/ios/Packages/KriaMediaEngine) — change both or cloud and phone
# renders of the same photo diverge.
_GUIDED_IMAGE_MATTE_RGB = (0, 0, 0)
_DIRECTION_POLICY = {
    "guided_story": {
        "min_moment_s": GUIDED_STORY_MIN_MOMENT_S,
        "transition": "crossfade",
        "text_effect": "fade-in",
    },
    "fast_montage": {"min_moment_s": 0.8, "transition": "none", "text_effect": "static"},
    "text_explainer": {
        "min_moment_s": 1.8,
        "transition": "crossfade",
        "text_effect": "fade-in",
    },
}


def _story_canvas(orientation: str | None) -> Canvas:
    return LANDSCAPE if orientation == "landscape" else PORTRAIT


class GuidedStoryError(RuntimeError):
    """Plain-language strict-render failure with a stable machine code."""

    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


class GuidedStoryMoment(BaseModel):
    model_config = ConfigDict(extra="forbid")

    moment_id: str = Field(min_length=1)
    beat_id: str = Field(min_length=1)
    topic: str = Field(min_length=1)
    media_id: str = Field(min_length=1)
    lane: Literal["clip", "asset"]
    kind: Literal["image", "video"]
    gcs_path: str = Field(min_length=1)
    generation: str = Field(min_length=1)
    layout: Literal["fullscreen", "supporting_card"]
    source_start_s: float = Field(ge=0)
    source_end_s: float = Field(gt=0)
    source_crop: dict[str, float] | None = None
    playback_rate: float | None = Field(default=None, ge=0.25, le=4.0)
    output_start_s: float = Field(ge=0)
    output_end_s: float = Field(gt=0)
    duration_s: float = Field(gt=0)
    image_motion: Literal["subtle_zoom_in"] | None = None
    look_preset: str = "none"
    look_adjustments: dict[str, float] | None = None
    # None preserves legacy approved plans' global transition policy. Editor
    # revisions always materialize an explicit per-boundary value.
    transition_after: Literal["cut", "crossfade", "dip_to_black", "flash"] | None = None
    transition_duration_s: float | None = Field(default=None, ge=0, le=0.3)
    beat_align: bool = False
    beat_time_s: float | None = Field(default=None, ge=0)
    required: bool = True


class GuidedStoryBeatWindow(BaseModel):
    model_config = ConfigDict(extra="forbid")

    beat_id: str = Field(min_length=1)
    approved_duration_s: float = Field(gt=0)
    resolved_duration_s: float = Field(gt=0)
    start_s: float = Field(ge=0)
    end_s: float = Field(gt=0)


class GuidedStoryTransitionPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["none", "crossfade"]
    duration_s: float = Field(ge=0, le=1)


class GuidedStoryTypography(BaseModel):
    model_config = ConfigDict(extra="forbid")

    style_id: Literal["guided_story_v1", "guided_story_v2"]
    font: str = Field(min_length=1)


class GuidedStoryMusic(BaseModel):
    model_config = ConfigDict(extra="forbid")

    track_id: str = Field(min_length=1)
    title: str = Field(min_length=1)
    audio_gcs_path: str = Field(min_length=1)
    generation: str = Field(min_length=1)
    start_s: float = Field(ge=0)
    end_s: float | None = Field(default=None, gt=0)
    level: float = Field(default=1.0, ge=0, le=1.0)


class GuidedStorySongReference(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1] = 1
    delivery: Literal["external_platform"] = "external_platform"
    track_id: str = Field(min_length=1)
    title: str = Field(min_length=1)
    artist: str | None = None
    start_s: float = Field(ge=0)
    end_s: float = Field(gt=0)

    @model_validator(mode="after")
    def validate_interval(self) -> GuidedStorySongReference:
        if (
            not math.isfinite(self.start_s)
            or not math.isfinite(self.end_s)
            or self.end_s <= self.start_s
        ):
            raise ValueError("song reference interval must be finite and ordered")
        return self


class GuidedStoryExecutionPlan(BaseModel):
    """Strict JSONB contract reused verbatim on worker redelivery."""

    model_config = ConfigDict(extra="forbid")

    compiler_version: Literal[1, 2, 3, 4, 5, 6, 7, 8]
    proposal_version: int = Field(ge=1)
    media_digest: str = Field(min_length=64, max_length=64)
    direction: Literal["guided_story", "fast_montage", "text_explainer"]
    goal: str
    pace: Literal["relaxed", "balanced", "fast"]
    approved_duration_s: float = Field(gt=0)
    resolved_duration_s: float = Field(gt=0)
    output_orientation: Literal["portrait", "landscape"] = "portrait"
    output_orientation_reason: str = "Legacy guided stories used the portrait canvas."
    selected_media_ids: list[str] = Field(min_length=1)
    story_timeline: list[GuidedStoryMoment] = Field(min_length=1)
    beat_windows: list[GuidedStoryBeatWindow] = Field(min_length=1)
    text_elements: list[TextElement]
    # Server-derived contextual labels are kept in their own lane. They are
    # not editor-authored text and therefore must not become part of the
    # approved text identity set, but they are still receipt-verified pixels.
    # Keep the historical null key in compiler receipts so v1-v7 replay hashes
    # remain byte-identical. Read adapters discard old values; no render lane
    # accepts this retired intent.
    context_label_intent: None = None
    context_label_text_elements: list[TextElement] = Field(default_factory=list)
    narration_label_text_elements: list[TextElement] = Field(default_factory=list)
    narration_label_receipt: dict[str, Any] | None = None
    licensed_sfx_intent: dict[str, Any] | None = None
    transition_policy: GuidedStoryTransitionPolicy
    mixed_media_timing: MixedMediaTimingProfile | None = None
    montage_cadence: MontageCadenceConstraint | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )
    montage_text_bindings: list[dict[str, Any]] = Field(default_factory=list)
    montage_audio: dict[str, Any] | None = None
    typography: GuidedStoryTypography
    music: GuidedStoryMusic | None = None
    song_reference: GuidedStorySongReference | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    # Internal replay fence; deliberately excluded from the public reference
    # payload so clients only receive the external-platform identity/timing.
    song_reference_track_duration_s: float | None = Field(
        default=None, gt=0, exclude_if=lambda value: value is None
    )
    # Optional generation-pinned creator narration. Legacy plans omit this
    # field and retain their existing audio path exactly.
    narration: NarrationTrack | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )
    # Optional post-approval runtime projection.  Canonical approved plans
    # leave these unset; v2 revisions carry them without changing approval.
    editor_revision_number: int | None = Field(default=None, ge=1)
    editor_revision_hash: str | None = None
    editor_sound_effects: list[dict[str, Any]] = Field(default_factory=list)
    editor_media_overlays: list[dict[str, Any]] = Field(default_factory=list)
    editor_visual_blocks: list[dict[str, Any]] = Field(default_factory=list)
    editor_motion_scenes: list[dict[str, Any]] = Field(default_factory=list)
    editor_custom_effects: list[dict[str, Any]] = Field(default_factory=list)
    editor_caption_meta: dict[str, Any] | None = None
    editor_audio_level: float = Field(default=1.0, ge=0, le=1)
    editor_music_removed: bool = False
    editor_lane_hashes: dict[str, str] = Field(default_factory=dict)
    editor_tombstones: list[dict[str, Any]] = Field(default_factory=list)
    editor_source_pool: list[dict[str, Any]] = Field(default_factory=list)
    editor_base_generation: str | None = None
    editor_renderer_version: str | None = None
    editor_effect_schema_version: str | None = None
    editor_approved_text_ids: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_internal_receipt_contract(self) -> GuidedStoryExecutionPlan:
        if self.compiler_version >= 6 and self.music is not None:
            raise ValueError("reference-only song plans cannot carry mixed music")
        if self.compiler_version < 6 and (
            self.song_reference is not None or self.song_reference_track_duration_s is not None
        ):
            raise ValueError("legacy song plans cannot carry an external reference")
        if (self.song_reference is None) != (self.song_reference_track_duration_s is None):
            raise ValueError("song reference requires its pinned catalog duration")
        if self.song_reference is not None:
            catalog_duration = self.song_reference_track_duration_s
            if catalog_duration is None or not math.isfinite(catalog_duration):
                raise ValueError("song reference requires a finite catalog duration")
            if self.song_reference.end_s > catalog_duration + 0.001:
                raise ValueError("song reference exceeds the pinned catalog duration")
            if (
                abs(
                    self.song_reference.end_s
                    - self.song_reference.start_s
                    - self.resolved_duration_s
                )
                > 0.001
            ):
                raise ValueError("song reference must cover the resolved video duration")
        if self.editor_revision_number is None and not self.text_elements:
            raise ValueError("approved guided stories require at least one text element")
        if len(self.selected_media_ids) != len(set(self.selected_media_ids)):
            raise ValueError("selected media IDs must be unique")
        label_ids = [element.id for element in self.narration_label_text_elements]
        if len(label_ids) != len(set(label_ids)):
            raise ValueError("narration label IDs must be unique")
        timeline_media: list[str] = []
        for moment in self.story_timeline:
            if moment.media_id not in timeline_media:
                timeline_media.append(moment.media_id)
            if moment.source_end_s <= moment.source_start_s:
                raise ValueError("source windows must be ordered")
            if moment.output_end_s <= moment.output_start_s:
                raise ValueError("output windows must be ordered")
        if timeline_media != self.selected_media_ids:
            raise ValueError("timeline media must exactly match selected media")
        if (
            self.editor_revision_number is None
            and self.narration is not None
            and abs(
                float(self.resolved_duration_s)
                - canonical_narration_duration_s(self.narration.duration_s)
            )
            > 0.001
        ):
            raise ValueError("voiceover-led plans must use the narration frame budget")
        beat_ids = [window.beat_id for window in self.beat_windows]
        actual_beats: list[str] = []
        for moment in self.story_timeline:
            if moment.beat_id not in actual_beats:
                actual_beats.append(moment.beat_id)
        if beat_ids != actual_beats:
            raise ValueError("timeline beats must exactly match beat windows")
        return self


class GuidedStoryOutputReceipt(BaseModel):
    model_config = ConfigDict(extra="forbid")

    width: int = Field(gt=0)
    height: int = Field(gt=0)
    video_codec: Literal["h264"]
    audio_codec: Literal["aac"]
    sha256: str = Field(min_length=64, max_length=64)


class GuidedStoryStorageReceipt(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str = Field(min_length=1)
    generation: str = Field(min_length=1)
    size: int = Field(ge=1)
    md5_hash: str | None = None


class GuidedStoryRenderReceipt(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # v1 remains valid for immutable approved renders. Revisions add lane
    # hashes and provenance under v2 without changing the approval payload.
    schema_version: Literal[1, 2]
    verified: Literal[True]
    proposal_version: int = Field(ge=1)
    media_digest: str = Field(min_length=64, max_length=64)
    expected_beat_ids: list[str]
    actual_beat_ids: list[str]
    expected_moment_ids: list[str]
    actual_moment_ids: list[str]
    expected_media_ids: list[str]
    actual_media_ids: list[str]
    expected_text_ids: list[str]
    actual_text_ids: list[str]
    expected_context_label_ids: list[str] = Field(default_factory=list)
    actual_context_label_ids: list[str] = Field(default_factory=list)
    expected_narration_label_ids: list[str] = Field(default_factory=list)
    actual_narration_label_ids: list[str] = Field(default_factory=list)
    approved_text_ids: list[str] | None = None
    text_edited_after_approval: bool = False
    media_count: int = Field(ge=1)
    image_count: int = Field(ge=0)
    video_count: int = Field(ge=0)
    expected_duration_s: float = Field(gt=0)
    actual_duration_s: float = Field(gt=0)
    output_orientation: Literal["portrait", "landscape"] = "portrait"
    output_orientation_reason: str = "Legacy guided stories used the portrait canvas."
    music_applied: bool
    music: GuidedStoryMusic | None
    song_reference: GuidedStorySongReference | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    music_window_applied: dict[str, float] | None = None
    output: GuidedStoryOutputReceipt
    base_storage: GuidedStoryStorageReceipt | None = None
    output_storage: GuidedStoryStorageReceipt | None = None
    media_stages: list[dict[str, Any]]
    moment_stages: list[dict[str, Any]]
    text_stages: list[dict[str, Any]]
    revision_number: int | None = None
    revision_hash: str | None = None
    lane_hashes: dict[str, str] = Field(default_factory=dict)
    tombstones: list[dict[str, Any]] = Field(default_factory=list)
    source_pool: list[dict[str, Any]] = Field(default_factory=list)
    segment_order: list[str] = Field(default_factory=list)
    music_removed: bool = False
    base_render_generation: str | None = None
    renderer_version: str | None = None
    effect_schema_version: str | None = None
    source_audio_options: list[dict[str, Any]] = Field(default_factory=list)
    source_audio_preserved: bool | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    narration: NarrationTrack | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )
    narration_applied: bool = False
    narration_label_receipt: dict[str, Any] | None = None

    @model_validator(mode="after")
    def validate_strict_equality(self) -> GuidedStoryRenderReceipt:
        pairs = (
            (self.expected_beat_ids, self.actual_beat_ids),
            (self.expected_moment_ids, self.actual_moment_ids),
            (self.expected_media_ids, self.actual_media_ids),
            (self.expected_text_ids, self.actual_text_ids),
            (self.expected_context_label_ids, self.actual_context_label_ids),
            (self.expected_narration_label_ids, self.actual_narration_label_ids),
        )
        if any(expected != actual for expected, actual in pairs):
            raise ValueError("receipt expected/actual identities must match exactly")
        if self.media_count != len(self.actual_media_ids):
            raise ValueError("receipt media count does not match its identities")
        if self.image_count + self.video_count != self.media_count:
            raise ValueError("receipt media kinds do not add up to its media count")
        if self.music_applied != (self.music is not None):
            raise ValueError("receipt music identity does not match application state")
        if self.music is not None and self.song_reference is not None:
            raise ValueError("receipt cannot carry both mixed music and an external reference")
        if self.narration_applied != (self.narration is not None):
            raise ValueError("receipt narration identity does not match application state")
        if self.music is not None and self.music_window_applied is not None:
            expected_window = max(
                0.0,
                float(self.music.end_s or self.expected_duration_s)
                - float(self.music.start_s or 0.0),
            )
            if (
                abs(float(self.music_window_applied.get("duration_s", -1.0)) - expected_window)
                > 1e-6
            ):
                raise ValueError("receipt music window does not match applied music")
        return self


def _round_frame(seconds: float) -> float:
    return round(max(_FRAME_S, round(seconds / _FRAME_S) * _FRAME_S), 3)


def _quantize_quick_mixed_timeline(
    moments: list[dict[str, Any]],
    beat_windows: list[dict[str, Any]],
    *,
    target_s: float,
    mixed_media_timing: MixedMediaTimingProfile | None = None,
) -> float:
    """Compile quick mixed-media holds to an exact, source-safe frame budget."""

    fps = int(round(1.0 / _FRAME_S))
    target_frames = max(1, int(round(float(target_s) * fps)))
    frame_counts: list[int] = []
    image_headroom: list[tuple[float, int]] = []
    video_headroom: list[tuple[float, int, int]] = []
    original_video_end_s: dict[int, float] = {}
    floor_video_frames: dict[int, int] = {}
    for index, moment in enumerate(moments):
        desired_frames = float(moment["duration_s"]) * fps
        frames = max(1, int(math.floor(desired_frames + 1e-6)))
        if moment["kind"] == "image":
            bounds = mixed_media_hold_bounds("image", mixed_media_timing)
            minimum_frames = int(math.ceil(bounds.minimum_s * fps - 1e-6))
            maximum_frames = int(math.floor(bounds.maximum_s * fps + 1e-6))
            frames = min(maximum_frames, max(minimum_frames, frames))
            image_headroom.append((desired_frames - math.floor(desired_frames), index))
        else:
            original_video_end_s[index] = float(moment["source_end_s"])
            bounds = mixed_media_hold_bounds("video", mixed_media_timing)
            floor_video_frames[index] = frames
            if bounds.minimum_s - 0.001 <= float(moment["duration_s"]) < bounds.minimum_s:
                # Proposal source/output windows allow 1ms numeric tolerance.
                # The near-minimum source still contains the 45th frame.
                frames = int(math.ceil(bounds.minimum_s * fps - 1e-6))
            source_span_s = max(
                0.0,
                float(moment["source_end_s"]) - float(moment["source_start_s"]),
            )
            source_max_frames = int(math.floor((source_span_s + 0.001) * fps + 1e-6))
            maximum_frames = min(
                int(math.floor(bounds.maximum_s * fps + 1e-6)),
                source_max_frames,
            )
            video_headroom.append(
                (desired_frames - math.floor(desired_frames), index, maximum_frames)
            )
        frame_counts.append(frames)

    remaining_frames = target_frames - sum(frame_counts)
    if remaining_frames < 0:
        raise GuidedStoryError(
            "guided_story_duration_impossible",
            "The approved mixed-media timing exceeds its frame budget.",
        )
    # Videos stay floored to their approved source spans. Assign fractional
    # frame remainder only to photos, where a longer hold cannot stretch or
    # overlap source footage. Largest fractional remainders stay closest to
    # the creator-approved durations.
    for _fraction, index in sorted(image_headroom, reverse=True):
        if remaining_frames <= 0:
            break
        maximum_frames = int(
            math.floor(mixed_media_hold_bounds("image", mixed_media_timing).maximum_s * fps + 1e-6)
        )
        addition = min(remaining_frames, maximum_frames - frame_counts[index])
        frame_counts[index] += addition
        remaining_frames -= addition
    # Rounded source windows can leave a safe fractional-frame remainder even
    # after every still has reached its approved ceiling. Consume that
    # remainder on video headroom, never below the existing video minimum and
    # never beyond the source-owned window (plus the established 1ms tolerance).
    for _fraction, index, maximum_frames in sorted(video_headroom, reverse=True):
        if remaining_frames <= 0:
            break
        addition = min(remaining_frames, maximum_frames - frame_counts[index])
        if addition <= 0:
            continue
        frame_counts[index] += addition
        remaining_frames -= addition
    if remaining_frames:
        raise GuidedStoryError(
            "guided_story_duration_impossible",
            "The approved mixed-media timing cannot fit an exact source-safe frame budget.",
        )

    cursor_frames = 0
    for index, (moment, frames) in enumerate(zip(moments, frame_counts, strict=True)):
        duration_s = frames / fps
        start_s = cursor_frames / fps
        cursor_frames += frames
        end_s = cursor_frames / fps
        moment["duration_s"] = round(duration_s, 6)
        moment["output_start_s"] = round(start_s, 6)
        moment["output_end_s"] = round(end_s, 6)
        if moment["kind"] == "video" and frames > floor_video_frames[index]:
            moment["source_end_s"] = original_video_end_s[index]
        else:
            moment["source_end_s"] = round(float(moment["source_start_s"]) + duration_s, 6)

    windows_by_id = {window["beat_id"]: window for window in beat_windows}
    moments_by_beat: dict[str, list[dict[str, Any]]] = {}
    for moment in moments:
        moments_by_beat.setdefault(str(moment["beat_id"]), []).append(moment)
    for beat_id, beat_moments in moments_by_beat.items():
        window = windows_by_id[beat_id]
        start_s = float(beat_moments[0]["output_start_s"])
        end_s = float(beat_moments[-1]["output_end_s"])
        window["start_s"] = round(start_s, 6)
        window["end_s"] = round(end_s, 6)
        window["resolved_duration_s"] = round(end_s - start_s, 6)
    return cursor_frames / fps


def _selected_media_ids(snapshot: EditProposalSnapshot) -> list[str]:
    selected = list(
        dict.fromkeys(
            [cut.media_id for cut in snapshot.fast_cuts]
            if snapshot.fast_cuts
            else [media_id for beat in snapshot.story_beats for media_id in beat.media_ids]
        )
    )
    required = (
        [ref.media_id for ref in snapshot.media]
        if snapshot.media_scope == "all"
        else snapshot.selected_media_ids
    )
    if required is not None and set(selected) != set(required):
        raise GuidedStoryError(
            "guided_story_snapshot_invalid", "The timeline does not cover the selected media."
        )
    # Selection is a coverage set; approved cuts or story beats own order.
    return selected


def _fast_montage_output_windows(
    cuts: list[Any],
    *,
    duration_s: float,
    track: dict[str, Any] | None,
    video_media_ids: set[str],
    mixed_media_timing: MixedMediaTimingProfile | None = None,
    preserve_exact_cadence: bool = False,
) -> list[tuple[float, float, float | None]]:
    """Allocate hard-cut output windows, optionally snapping marked boundaries.

    The approved cut durations remain the baseline.  A marked boundary is
    snapped only when a nearby track beat keeps both adjacent cuts in the
    renderer's supported 0.4–1.2s range; the final boundary always remains
    the approved total duration. A video may be shortened inside its approved
    source window to meet a beat, but is never lengthened beyond that window.
    """

    durations = [float(cut.output_duration_s) for cut in cuts]
    if abs(sum(durations) - float(duration_s)) > 0.15:
        raise GuidedStoryError(
            "guided_story_duration_impossible",
            "Fast montage cut durations do not match the approved duration.",
        )
    nominal_boundaries: list[float] = []
    cursor = 0.0
    for cut_duration in durations:
        cursor = round(cursor + cut_duration, 3)
        nominal_boundaries.append(cursor)
    boundaries = list(nominal_boundaries)
    beat_times: list[float] = []
    # The typed profile is already an exact, creator-approved per-kind timing
    # program. Legacy beat snapping can move a 0.5s photo below its minimum,
    # so preserve those approved windows byte-for-byte and use hard cuts.
    if (
        track
        and not preserve_exact_cadence
        and not uses_quick_photo_long_video_timing(mixed_media_timing)
    ):
        music_start_s = float(track.get("start_s") or 0.0)
        beat_times = sorted(
            round(float(raw_beat) - music_start_s, 3)
            for raw_beat in (track.get("beat_timestamps_s") or [])
            if float(raw_beat) >= music_start_s
        )

    for index, cut in enumerate(cuts[:-1]):
        if not cut.beat_align or not beat_times:
            continue
        previous_boundary = 0.0 if index == 0 else boundaries[index - 1]
        nominal_boundary = nominal_boundaries[index]
        candidates = [
            beat
            for beat in beat_times
            if previous_boundary + 0.4 <= beat <= previous_boundary + 1.2
            and abs(beat - nominal_boundary) <= 0.15
            and beat < float(duration_s)
        ]
        if not candidates:
            continue
        snapped = min(candidates, key=lambda beat: (abs(beat - nominal_boundary), beat))
        next_boundary = nominal_boundaries[index + 1]
        left_duration = snapped - previous_boundary
        right_duration = next_boundary - snapped
        if not 0.4 <= left_duration <= 1.2:
            continue
        if not 0.4 <= right_duration <= 1.2:
            continue
        if cut.media_id in video_media_ids and left_duration > durations[index] + 0.001:
            continue
        if (
            cuts[index + 1].media_id in video_media_ids
            and right_duration > durations[index + 1] + 0.001
        ):
            continue
        boundaries[index] = snapped

    windows: list[tuple[float, float, float | None]] = []
    previous = 0.0
    for index, boundary in enumerate(boundaries):
        boundary = round(boundary, 3)
        windows.append(
            (
                round(previous, 3),
                boundary,
                round(boundary, 3) if boundary != nominal_boundaries[index] else None,
            )
        )
        previous = boundary
    return windows


def _music_payload(track: dict[str, Any] | None, *, duration_s: float) -> dict[str, Any] | None:
    """Drop compiler-only beat metadata before validating the music receipt."""

    if track is None:
        return None
    payload = {
        key: track[key]
        for key in (
            "track_id",
            "title",
            "audio_gcs_path",
            "generation",
            "start_s",
            "end_s",
            "level",
        )
        if key in track
    }
    if payload.get("end_s") is None:
        payload["end_s"] = round(float(payload.get("start_s") or 0.0) + duration_s, 3)
    return payload


def song_reference_variant_fields(plan: dict[str, Any]) -> dict[str, Any]:
    """Public metadata for new plans; legacy soundtrack contracts stay untouched."""
    if plan.get("compiler_version", 0) < 6:
        return {}
    return {
        "music_playback_mode": "reference_only",
        "song_reference": plan.get("song_reference"),
        "music_track_id": None,
        "source_audio_preserved": bool(
            (plan.get("montage_audio") or {}).get("preserve_source_audio")
        ),
    }


def _song_reference(track: dict[str, Any] | None, *, duration_s: float) -> dict[str, Any] | None:
    """Persist matched-song identity and timing without making it render input."""
    if track is None:
        return None
    track_id = str(track.get("track_id") or "")
    title = str(track.get("title") or "")
    artist = str(track.get("artist") or "")
    if not track_id or not title or not math.isfinite(duration_s) or duration_s <= 0:
        return None
    catalog_raw = track.get("catalog_duration_s")
    if catalog_raw is None:
        catalog_raw = track.get("duration_s")
    try:
        catalog_duration = float(catalog_raw)
        requested_start = float(track.get("start_s") if track.get("start_s") is not None else 0.0)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(catalog_duration) or catalog_duration <= 0:
        return None
    if not math.isfinite(requested_start) or requested_start < 0:
        return None
    if requested_start + duration_s > catalog_duration + 0.001:
        return None
    start_s = requested_start
    end_s = start_s + duration_s
    if end_s > catalog_duration + 0.001:
        return None
    return GuidedStorySongReference(
        track_id=track_id,
        title=title,
        artist=artist or None,
        start_s=round(start_s, 3),
        end_s=round(end_s, 3),
    ).model_dump(mode="json")


def validate_guided_snapshot(raw: object) -> tuple[int, str, EditProposalSnapshot]:
    """Validate the immutable Job snapshot and return its typed proposal."""

    if not isinstance(raw, dict):
        raise GuidedStoryError(
            "guided_story_snapshot_invalid", "The approved edit snapshot is missing."
        )
    try:
        proposal_version = int(raw["proposal_version"])
        media_digest = str(raw["media_digest"])
        snapshot = EditProposalSnapshot.model_validate(raw["approved_proposal"])
    except Exception as exc:  # noqa: BLE001
        raise GuidedStoryError(
            "guided_story_snapshot_invalid", "The approved edit snapshot is incomplete."
        ) from exc
    if (
        proposal_version < 1
        or canonical_media_digest(snapshot.media, snapshot.narration) != media_digest
    ):
        raise GuidedStoryError(
            "guided_story_snapshot_invalid", "The approved edit no longer matches its media."
        )
    identities = raw.get("media_identities")
    expected = {
        (ref.lane, ref.media_id, ref.gcs_path, ref.generation, ref.kind) for ref in snapshot.media
    }
    actual = {
        (
            str(row.get("lane")),
            str(row.get("media_id")),
            str(row.get("gcs_path")),
            str(row.get("generation")),
            str(row.get("kind")),
        )
        for row in identities or []
        if isinstance(row, dict)
    }
    if actual != expected:
        raise GuidedStoryError(
            "guided_story_snapshot_invalid", "The render media identities are incomplete."
        )
    return proposal_version, media_digest, snapshot


def matcher_clip_metas(snapshot: EditProposalSnapshot) -> list[Any]:
    """Build whole-story matcher inputs from ordered selected media analysis."""

    from app.pipeline.agents.gemini_analyzer import ClipMeta  # noqa: PLC0415

    by_id = {ref.media_id: ref for ref in snapshot.media}
    rows: list[ClipMeta] = []
    for media_id in _selected_media_ids(snapshot):
        ref = by_id[media_id]
        analysis = ref.analysis or {}
        description = str(analysis.get("description") or "")
        subject = str(analysis.get("subject") or "")
        context = " · ".join(
            value
            for value in (
                snapshot.goal.strip(),
                snapshot.direction.replace("_", " "),
                snapshot.pace,
                ref.user_context.strip(),
                description,
            )
            if value
        )
        raw_moments = analysis.get("best_moments")
        moments = (
            [moment for moment in raw_moments if isinstance(moment, dict)]
            if isinstance(raw_moments, list)
            else []
        )
        if not moments and ref.kind == "video" and ref.duration_s:
            moments = [
                {
                    "start_s": 0.0,
                    "end_s": float(ref.duration_s),
                    "energy": "medium",
                    "description": description or subject or ref.source_filename,
                }
            ]
        rows.append(
            ClipMeta(
                clip_id=ref.media_id,
                transcript="",
                hook_text=snapshot.title,
                hook_score=7.0,
                best_moments=moments,
                detected_subject=subject or context,
                clip_path=ref.gcs_path,
            )
        )
    return rows


def _source_window(ref, duration_s: float) -> tuple[float, float]:  # noqa: ANN001
    if ref.kind == "image":
        return 0.0, duration_s
    source_duration = float(ref.duration_s or 0.0)
    if source_duration <= 0:
        raise GuidedStoryError(
            "guided_story_duration_impossible",
            f"Video {ref.source_filename or ref.media_id} has no usable duration.",
        )
    if source_duration + _FRAME_S < duration_s:
        raise GuidedStoryError(
            "guided_story_duration_impossible",
            f"Video {ref.source_filename or ref.media_id} is too short for the approved beat.",
        )
    raw_moments = (ref.analysis or {}).get("best_moments")
    moments = raw_moments if isinstance(raw_moments, list) else []
    for moment in moments:
        if not isinstance(moment, dict):
            continue
        try:
            start = max(0.0, float(moment.get("start_s", 0.0)))
            end = min(source_duration, float(moment.get("end_s", source_duration)))
        except (TypeError, ValueError):
            continue
        if end - start + _FRAME_S >= duration_s:
            return round(start, 3), round(start + duration_s, 3)
    start = max(0.0, (source_duration - duration_s) / 2.0)
    return round(start, 3), round(start + duration_s, 3)


def guided_transition_params(
    direction: str,
    pace: str,
    mixed_media_timing: MixedMediaTimingProfile | None = None,
) -> tuple[str, float]:
    """Crossfade policy for a guided/text_explainer/fast_montage direction+pace.

    Single source of truth for the boundary transition
    `_compile_execution_plan_version` applies at render time, so pre-render
    capacity estimates (`edit_direction_planner.deterministic_guided_beats`,
    `guided_story_capacity_s`) charge the exact same overlap the strict
    compiler (`_allocate_beat_windows`) will -- keeping the two capacity
    models from drifting apart the way job b2242487 exposed.
    """

    quick_mixed_timing = uses_quick_photo_long_video_timing(mixed_media_timing)
    policy = _DIRECTION_POLICY[direction]
    transition_type = (
        "none" if quick_mixed_timing else policy["transition"] if pace != "fast" else "none"
    )
    transition_duration_s = 0.0 if quick_mixed_timing else 0.2 if pace == "relaxed" else 0.12
    return transition_type, transition_duration_s


# A video shorter than this after paying its transition overlap has no frames
# left to show; anything longer plays for its own length (see
# `guided_moment_floor_s`).
_MIN_RENDERABLE_MOMENT_S = 2 * _FRAME_S


def guided_moment_floor_s(min_moment_s: float, capacity_s: float) -> float:
    """Minimum screen time one source needs: `min_moment_s`, or its own usable
    length when it is shorter. A short clip is never excluded for being short;
    it simply plays in full."""

    return min(min_moment_s, capacity_s)


def guided_moment_capacity_s(ref: Any, *, overlap_s: float) -> float:
    """Usable duration a single beat moment (one media source) can supply.

    A video's capacity is its source duration minus the transition overlap it
    pays into the next moment (pass ``0.0`` for a moment that pays none, e.g.
    the story's globally last moment); a still image is an unbounded hold.
    This is the plain (non quick-mixed-timing) capacity model
    `_allocate_beat_windows` uses per moment -- factored out so the
    deterministic guided-story fallback selects sources against the exact
    same capacity the strict compiler will enforce.
    """

    if ref.kind == "image":
        return math.inf
    return max(0.0, float(ref.duration_s or 0.0) - overlap_s)


def _allocate_beat_windows(
    snapshot,
    *,
    by_id: dict,
    policy: dict,
    transition_type: str,
    transition_duration_s: float,
    mixed_media_timing: MixedMediaTimingProfile | None = None,
) -> list[float]:
    """Water-fill each beat's resolved duration against ITS OWN clips' capacity,
    not just the approved weight ratio, so a rounding-inflated beat can never be
    asked to exceed what its selected videos can actually supply (guided_story_
    duration_impossible after approval -- job 0be72363).
    """

    target_duration_s = (
        canonical_narration_duration_s(snapshot.narration.duration_s)
        if snapshot.narration is not None
        else float(snapshot.duration_s)
    )
    weight_total = sum(float(beat.duration_s) for beat in snapshot.story_beats)
    moment_count = sum(len(beat.media_ids) for beat in snapshot.story_beats)
    min_moment_s = float(policy["min_moment_s"])

    floors: list[float] = []
    caps: list[float] = []
    ideals: list[float] = []
    moment_index = 0
    for beat in snapshot.story_beats:
        beat_refs = [by_id[media_id] for media_id in beat.media_ids]
        overlaps_s = [
            transition_duration_s
            if transition_type != "none" and moment_index + offset != moment_count - 1
            else 0.0
            for offset in range(len(beat_refs))
        ]
        quick_mixed_timing = uses_quick_photo_long_video_timing(mixed_media_timing)
        capacities_b = [
            mixed_media_hold_bounds(ref.kind, mixed_media_timing).maximum_s
            if quick_mixed_timing and ref.kind == "image"
            else min(3.0, max(0.0, float(ref.duration_s or 0.0) - overlap))
            if quick_mixed_timing
            else guided_moment_capacity_s(ref, overlap_s=overlap)
            for ref, overlap in zip(beat_refs, overlaps_s, strict=True)
        ]
        if not quick_mixed_timing:
            floors.append(
                sum(guided_moment_floor_s(min_moment_s, capacity) for capacity in capacities_b)
            )
        else:
            floors.append(
                sum(
                    mixed_media_hold_bounds("image", mixed_media_timing).minimum_s
                    if ref.kind == "image"
                    else min(
                        mixed_media_hold_bounds("video", mixed_media_timing).minimum_s,
                        float(ref.duration_s or 0.0),
                    )
                    for ref in beat_refs
                )
            )
        caps.append(sum(capacities_b))
        ideals.append(target_duration_s * float(beat.duration_s) / weight_total)
        moment_index += len(beat_refs)

    allocated = [
        min(max(ideal, floor), cap) for ideal, floor, cap in zip(ideals, floors, caps, strict=True)
    ]
    deficit = target_duration_s - sum(allocated)
    if deficit > _ALLOCATION_EPSILON_S:
        active = [
            index
            for index in range(len(allocated))
            if math.isinf(caps[index]) or caps[index] - allocated[index] > _ALLOCATION_EPSILON_S
        ]
        remaining = deficit
        while remaining > _ALLOCATION_EPSILON_S:
            if not active:
                raise GuidedStoryError(
                    "guided_story_duration_impossible",
                    "The approved story is longer than its approved videos can support.",
                )
            share = remaining / len(active)
            consumed = 0.0
            next_active: list[int] = []
            for index in active:
                headroom = caps[index] - allocated[index]
                addition = share if math.isinf(headroom) else min(share, max(0.0, headroom))
                allocated[index] += addition
                consumed += addition
                if math.isinf(headroom) or headroom - addition > _ALLOCATION_EPSILON_S:
                    next_active.append(index)
            if consumed <= _ALLOCATION_EPSILON_S:
                raise GuidedStoryError(
                    "guided_story_duration_impossible",
                    "The approved story is longer than its approved videos can support.",
                )
            remaining -= consumed
            active = next_active
    elif deficit < -_ALLOCATION_EPSILON_S:
        active = [
            index
            for index in range(len(allocated))
            if allocated[index] - floors[index] > _ALLOCATION_EPSILON_S
        ]
        remaining = -deficit
        while remaining > _ALLOCATION_EPSILON_S:
            if not active:
                raise GuidedStoryError(
                    "guided_story_duration_impossible",
                    "The approved story is too short to show all approved media clearly.",
                )
            share = remaining / len(active)
            consumed = 0.0
            next_active = []
            for index in active:
                headroom = allocated[index] - floors[index]
                reduction = min(share, max(0.0, headroom))
                allocated[index] -= reduction
                consumed += reduction
                if headroom - reduction > _ALLOCATION_EPSILON_S:
                    next_active.append(index)
            if consumed <= _ALLOCATION_EPSILON_S:
                raise GuidedStoryError(
                    "guided_story_duration_impossible",
                    "The approved story is too short to show all approved media clearly.",
                )
            remaining -= consumed
            active = next_active

    resolved: list[float] = []
    for value, cap in zip(allocated, caps, strict=True):
        rounded_value = _round_frame(value)
        if not math.isinf(cap):
            frame_cap = math.floor((cap + _FRAME_FLOOR_EPSILON_S) / _FRAME_S) * _FRAME_S
            rounded_value = min(rounded_value, round(frame_cap, 3))
        resolved.append(rounded_value)

    residual = round(target_duration_s - sum(resolved), 3)
    if residual > 0:
        for index in reversed(range(len(resolved))):
            headroom = caps[index] - resolved[index]
            addition = residual if math.isinf(headroom) else min(residual, headroom)
            if addition <= 0:
                continue
            resolved[index] = round(resolved[index] + addition, 3)
            residual = round(residual - addition, 3)
            if residual <= 0:
                break
    elif residual < 0:
        for index in reversed(range(len(resolved))):
            reduction = min(-residual, resolved[index] - floors[index])
            if reduction <= 0:
                continue
            resolved[index] = round(resolved[index] - reduction, 3)
            residual = round(residual + reduction, 3)
            if residual >= 0:
                break
    if abs(residual) > _DURATION_MATCH_TOLERANCE_S:
        raise GuidedStoryError(
            "guided_story_duration_impossible",
            "The approved story timing could not be allocated safely.",
        )
    return resolved


def _allocate_beat_durations(
    refs: list[Any],
    *,
    beat_duration_s: float,
    min_moment_s: float,
    overlaps_s: list[float],
    beat_topic: str,
    mixed_media_timing: MixedMediaTimingProfile | None = None,
) -> list[float]:
    """Water-fill a beat while respecting the usable length of short videos."""

    quick_mixed_timing = uses_quick_photo_long_video_timing(mixed_media_timing)
    floor_total = (
        sum(
            guided_moment_floor_s(min_moment_s, guided_moment_capacity_s(ref, overlap_s=overlap))
            for ref, overlap in zip(refs, overlaps_s, strict=True)
        )
        if not quick_mixed_timing
        else sum(
            mixed_media_hold_bounds(ref.kind, mixed_media_timing).minimum_s
            if ref.kind == "image"
            else min(
                mixed_media_hold_bounds(ref.kind, mixed_media_timing).minimum_s,
                float(ref.duration_s or 0.0),
            )
            for ref in refs
        )
    )
    if beat_duration_s + _FRAME_S < floor_total:
        raise GuidedStoryError(
            "guided_story_duration_impossible",
            f"Beat {beat_topic} is too short to show all approved media clearly.",
        )

    capacities: list[float] = []
    for ref, overlap in zip(refs, overlaps_s, strict=True):
        if ref.kind == "image":
            capacities.append(
                mixed_media_hold_bounds(ref.kind, mixed_media_timing).maximum_s
                if quick_mixed_timing
                else math.inf
            )
            continue
        source_duration = float(ref.duration_s or 0.0)
        if source_duration <= 0:
            raise GuidedStoryError(
                "guided_story_duration_impossible",
                f"Video {ref.source_filename or ref.media_id} has no usable duration.",
            )
        capacity = max(0.0, source_duration - overlap)
        if capacity < _MIN_RENDERABLE_MOMENT_S:
            raise GuidedStoryError(
                "guided_story_duration_impossible",
                f"Video {ref.source_filename or ref.media_id} has no frames left to show.",
            )
        capacities.append(
            min(mixed_media_hold_bounds(ref.kind, mixed_media_timing).maximum_s, capacity)
            if quick_mixed_timing
            else capacity
        )

    if not quick_mixed_timing:
        allocated = [guided_moment_floor_s(min_moment_s, capacity) for capacity in capacities]
    else:
        allocated = [
            mixed_media_hold_bounds(ref.kind, mixed_media_timing).preferred_s
            if ref.kind == "image"
            else min(
                mixed_media_hold_bounds(ref.kind, mixed_media_timing).preferred_s,
                float(ref.duration_s or 0.0),
            )
            for ref in refs
        ]
        allocated = [
            max(
                mixed_media_hold_bounds(ref.kind, mixed_media_timing).minimum_s
                if ref.kind == "image"
                else min(
                    mixed_media_hold_bounds(ref.kind, mixed_media_timing).minimum_s,
                    float(ref.duration_s or 0.0),
                ),
                value,
            )
            for ref, value in zip(refs, allocated, strict=True)
        ]
    remaining = max(0.0, beat_duration_s - sum(allocated))
    # A mixed profile uses available headroom in videos first. Photos remain
    # quick unless the approved total cannot fit without using them.
    active = list(range(len(refs)))
    while remaining > _ALLOCATION_EPSILON_S:
        if quick_mixed_timing:
            video_active = [
                index
                for index in range(len(refs))
                if refs[index].kind == "video"
                and capacities[index] - allocated[index] > _ALLOCATION_EPSILON_S
            ]
            active = video_active or [
                index
                for index, ref in enumerate(refs)
                if ref.kind == "image"
                and capacities[index] - allocated[index] > _ALLOCATION_EPSILON_S
            ]
        if not active:
            raise GuidedStoryError(
                "guided_story_duration_impossible",
                f"Beat {beat_topic} is longer than its approved videos can support.",
            )
        share = remaining / len(active)
        consumed = 0.0
        next_active: list[int] = []
        for index in active:
            headroom = capacities[index] - allocated[index]
            addition = share if math.isinf(headroom) else min(share, max(0.0, headroom))
            allocated[index] += addition
            consumed += addition
            if math.isinf(headroom) or headroom - addition > _ALLOCATION_EPSILON_S:
                next_active.append(index)
        if consumed <= _ALLOCATION_EPSILON_S:
            raise GuidedStoryError(
                "guided_story_duration_impossible",
                f"Beat {beat_topic} is longer than its approved videos can support.",
            )
        remaining -= consumed
        active = next_active

    rounded: list[float] = []
    for duration, capacity in zip(allocated, capacities, strict=True):
        value = _round_frame(duration)
        if not math.isinf(capacity):
            frame_capacity = math.floor((capacity + _FRAME_FLOOR_EPSILON_S) / _FRAME_S) * _FRAME_S
            value = min(value, round(frame_capacity, 3))
        rounded.append(value)

    difference = round(beat_duration_s - sum(rounded), 3)
    if difference > 0:
        for index in reversed(range(len(rounded))):
            headroom = capacities[index] - rounded[index]
            addition = difference if math.isinf(headroom) else min(difference, headroom)
            if addition <= 0:
                continue
            rounded[index] = round(rounded[index] + addition, 3)
            difference = round(difference - addition, 3)
            if difference <= 0:
                break
    elif difference < 0:
        for index in reversed(range(len(rounded))):
            floor = (
                min_moment_s
                if not quick_mixed_timing
                else mixed_media_hold_bounds(refs[index].kind, mixed_media_timing).minimum_s
                if refs[index].kind == "image"
                else min(
                    mixed_media_hold_bounds(refs[index].kind, mixed_media_timing).minimum_s,
                    float(refs[index].duration_s or 0.0),
                )
            )
            reduction = min(-difference, rounded[index] - floor)
            if reduction <= 0:
                continue
            rounded[index] = round(rounded[index] - reduction, 3)
            difference = round(difference + reduction, 3)
            if difference >= 0:
                break
    if abs(difference) > _DURATION_MATCH_TOLERANCE_S:
        raise GuidedStoryError(
            "guided_story_duration_impossible",
            f"Beat {beat_topic} timing could not be allocated safely.",
        )
    return rounded


def _text_elements(
    snapshot: EditProposalSnapshot,
    beat_windows: list[dict],
    policy: dict,
    *,
    compiler_version: Literal[1, 2, 3, 4, 5, 6, 7, 8],
) -> list[dict]:
    total_s = (
        canonical_narration_duration_s(snapshot.narration.duration_s)
        if snapshot.narration
        else float(snapshot.duration_s)
    )
    title_end = min(
        total_s,
        float(snapshot.opening_title_duration_s)
        if snapshot.opening_title_duration_s is not None
        else GUIDED_TITLE_HOLD_S
        if snapshot.direction != "fast_montage"
        else FAST_MONTAGE_TITLE_HOLD_S,
    )
    # Confirmed creator copy beyond the title (shot labels, closing title, a
    # typed title hold). Snapshots without it keep the legacy projection.
    typed_copy = bool(
        snapshot.shot_labels
        or snapshot.closing_title
        or snapshot.opening_title_duration_s is not None
    )
    # A labeled edit shows only confirmed creator copy: no generated title
    # unless the creator supplied one.
    show_title = bool(snapshot.opening_title) or not snapshot.shot_labels
    closing_start = (
        round(max(0.0, total_s - closing_title_hold_s(total_s)), 3)
        if snapshot.closing_title
        else None
    )

    def clear_window(start_s: float, end_s: float, *, after_title: bool) -> tuple[float, float]:
        """Keep beat copy clear of the typed title/closing title, never dropping it."""

        if not typed_copy:
            return start_s, end_s
        clear_start = max(start_s, title_end) if after_title and show_title else start_s
        clear_end = min(end_s, closing_start) if closing_start is not None else end_s
        if clear_end - clear_start >= _MIN_CLEAR_TEXT_WINDOW_S:
            return round(clear_start, 3), round(clear_end, 3)
        return start_s, end_s

    def closing_elements(*, fast: bool, effect: str) -> list[dict]:
        if not snapshot.closing_title or closing_start is None or compiler_version < 3:
            return []
        return [
            TextElement(
                id="guided-closing-title",
                text=snapshot.closing_title,
                start_s=closing_start,
                end_s=total_s,
                role="generative_intro",
                position="custom",
                x_frac=0.5,
                y_frac=0.5,
                font_family=snapshot.font_family or "Fraunces",
                size_px=92 if fast else 104,
                color=snapshot.text_color or "#FFF8F0",
                highlight_color="#D9FF70",
                stroke_width=0,
                shadow_enabled=True,
                shadow_style="standard",
                effect=effect,
                alignment="center",
                letter_spacing=-0.025,
                line_spacing=1.0,
                max_width_frac=0.8,
            ).model_dump(mode="json", exclude_none=True)
        ]

    # Explicit Main Creator copy is immutable. Specialist montage bindings are
    # advisory and must not replace a confirmed title with generated words.
    # Narrated plans use grounded labels and timed captions. Advisory montage
    # copy can invent participant names and collide with those dedicated lanes.
    if (
        snapshot.montage_text_bindings
        and snapshot.fast_cuts
        and not snapshot.opening_title
        and snapshot.narration is None
    ):
        text_by_source = {entry.media_id: entry.text for entry in snapshot.montage_text_bindings}
        elements: list[dict] = []
        for cut, window in zip(snapshot.fast_cuts, beat_windows, strict=True):
            text = text_by_source.get(cut.media_id)
            if not text:
                continue
            binding_start_s, binding_end_s = clear_window(
                float(window["start_s"]), float(window["end_s"]), after_title=False
            )
            elements.append(
                TextElement(
                    id=f"montage-text-{cut.cut_id}",
                    text=text,
                    start_s=binding_start_s,
                    end_s=binding_end_s,
                    role="generative_intro",
                    position="custom" if compiler_version >= 3 else "bottom",
                    x_frac=0.5 if compiler_version >= 3 else None,
                    y_frac=0.78 if compiler_version >= 3 else None,
                    font_family=snapshot.font_family
                    or ("Fraunces" if compiler_version >= 3 else "Inter-Bold"),
                    size_px=58 if compiler_version >= 3 else 50,
                    color=snapshot.text_color
                    or ("#FFF8F0" if compiler_version >= 3 else "#FFFFFF"),
                    highlight_color="#D9FF70" if compiler_version >= 3 else "#6FE7F7",
                    stroke_width=0 if compiler_version >= 3 else 4,
                    shadow_enabled=True,
                    shadow_style="standard" if compiler_version >= 3 else None,
                    effect="static",
                    alignment="center",
                    max_width_frac=0.82,
                ).model_dump(mode="json", exclude_none=True)
            )
        return [
            *elements,
            *closing_elements(fast=True, effect="static"),
            *_narration_caption_elements(snapshot),
        ]
    # New fast-montage proposals carry their own dense cut list. Keep only the
    # short hook/title; generated chapter thoughts would turn a music-led cut
    # back into an information card edit. Legacy fast snapshots have no
    # ``fast_cuts`` and retain the old text projection below.
    if snapshot.direction == "fast_montage" and snapshot.fast_cuts:
        return [
            TextElement(
                id="guided-title",
                text=snapshot.title,
                start_s=0.0,
                end_s=title_end,
                role="generative_intro",
                position="custom" if compiler_version >= 3 else "top",
                x_frac=0.5 if compiler_version >= 3 else None,
                y_frac=0.16 if compiler_version >= 3 else None,
                font_family=snapshot.font_family
                or ("Fraunces" if compiler_version >= 3 else "Inter-Bold"),
                size_px=(92 if compiler_version >= 3 else 78),
                color=snapshot.text_color or ("#FFF8F0" if compiler_version >= 3 else "#FFFFFF"),
                highlight_color="#D9FF70" if compiler_version >= 3 else "#6FE7F7",
                stroke_width=0 if compiler_version >= 3 else 5,
                shadow_enabled=True,
                shadow_style="standard" if compiler_version >= 3 else None,
                effect="static",
                alignment="center",
                max_width_frac=0.8 if compiler_version >= 3 else 0.86,
            ).model_dump(mode="json", exclude_none=True)
        ] + [
            *closing_elements(fast=True, effect="static"),
            *_narration_caption_elements(snapshot),
        ]
    if compiler_version < 3:
        elements = [
            TextElement(
                id="guided-title",
                text=snapshot.title,
                start_s=0.0,
                end_s=title_end,
                role="generative_intro",
                position="top",
                font_family=snapshot.font_family or "Inter-Bold",
                size_px=78 if snapshot.direction == "fast_montage" else 84,
                color=snapshot.text_color or "#FFFFFF",
                highlight_color="#6FE7F7",
                stroke_width=5,
                shadow_enabled=True,
                effect=policy["text_effect"],
                alignment="center",
                max_width_frac=0.86,
            ).model_dump(mode="json", exclude_none=True)
        ]
        for beat, window in zip(snapshot.story_beats, beat_windows, strict=True):
            thought = beat.thought.strip()
            if not thought:
                continue
            elements.append(
                TextElement(
                    id=f"guided-thought-{beat.beat_id}",
                    text=thought,
                    start_s=max(0.0, round(float(window["start_s"]), 3)),
                    end_s=float(window["end_s"]),
                    role="generative_intro",
                    position="bottom",
                    font_family="Inter-Bold",
                    size_px=54 if snapshot.direction == "text_explainer" else 50,
                    color="#FFFFFF",
                    highlight_color="#6FE7F7",
                    stroke_width=4,
                    shadow_enabled=True,
                    effect=policy["text_effect"],
                    alignment="center",
                    max_width_frac=0.84,
                ).model_dump(mode="json", exclude_none=True)
            )
        return [*elements, *_narration_caption_elements(snapshot)]

    # A labeled edit shows only confirmed creator copy (see show_title).
    elements = (
        [
            TextElement(
                id="guided-title",
                text=snapshot.title,
                start_s=0.0,
                end_s=title_end,
                role="generative_intro",
                position="custom",
                x_frac=0.5,
                y_frac=0.16,
                font_family=snapshot.font_family or "Fraunces",
                size_px=92 if snapshot.direction == "fast_montage" else 104,
                color=snapshot.text_color or "#FFF8F0",
                highlight_color="#D9FF70",
                stroke_width=0,
                shadow_enabled=True,
                shadow_style="standard",
                effect=policy["text_effect"],
                alignment="center",
                letter_spacing=-0.025,
                line_spacing=1.0,
                max_width_frac=0.8,
            ).model_dump(mode="json", exclude_none=True)
        ]
        if show_title
        else []
    )
    for beat, window in zip(snapshot.story_beats, beat_windows, strict=True):
        thought = beat.thought.strip()
        if not thought:
            continue
        start_s, end_s = clear_window(
            float(window["start_s"]), float(window["end_s"]), after_title=True
        )
        elements.append(
            TextElement(
                id=f"guided-thought-{beat.beat_id}",
                text=thought,
                start_s=max(0.0, round(start_s, 3)),
                end_s=end_s,
                role="generative_intro",
                position="custom",
                x_frac=0.5,
                y_frac=0.8,
                font_family="DM Sans",
                size_px=64 if snapshot.direction == "text_explainer" else 60,
                color="#FFF8F0",
                highlight_color="#D9FF70",
                stroke_width=0,
                shadow_enabled=True,
                shadow_style="standard",
                effect=policy["text_effect"],
                alignment="center",
                line_spacing=1.08,
                max_width_frac=0.76,
            ).model_dump(mode="json", exclude_none=True)
        )
    return [
        *elements,
        *closing_elements(fast=snapshot.direction == "fast_montage", effect=policy["text_effect"]),
        *_narration_caption_elements(snapshot),
    ]


def _narration_caption_elements(snapshot: EditProposalSnapshot) -> list[dict]:
    """Project pinned speech words into the existing text renderer's caption lane."""

    narration = snapshot.narration
    if narration is None or not narration.words:
        return []
    # Whisper legitimately emits point timestamps. Retain those tokens in a
    # neighboring caption instead of dropping words or inventing speech time.
    groups: list[list] = []
    pending: list = []
    for word in narration.words:
        if round(word.end_s - word.start_s, 3) <= 0:
            if groups and groups[-1][-1].end_s >= word.start_s - 0.001:
                groups[-1].append(word)
            else:
                pending.append(word)
            continue
        groups.append([*pending, word])
        pending = []
    if pending and groups:
        groups[-1].extend(pending)
    elements: list[dict] = []
    for index, words in enumerate(groups):
        start_s = max(0.0, round(min(word.start_s for word in words), 3))
        end_s = min(float(narration.duration_s), round(max(word.end_s for word in words), 3))
        if end_s <= start_s:
            continue
        elements.append(
            TextElement(
                id=f"narration-caption-{index + 1}",
                text=" ".join(word.text.strip() for word in words),
                start_s=start_s,
                end_s=end_s,
                role="generative_sequence",
                position="custom",
                x_frac=0.5,
                y_frac=0.82,
                font_family=snapshot.font_family or "Inter-Bold",
                size_px=58,
                color="#FFFFFF",
                highlight_color="#FFFFFF",
                stroke_width=6,
                shadow_enabled=True,
                effect="static",
                alignment="center",
                max_width_frac=0.84,
                word_timings=[word.model_dump(mode="json") for word in words],
                source_params={
                    "source": CAPTION_CUE_SOURCE,
                    "key": str(index),
                    "identity": f"pinned-narration-caption-{index}",
                },
            ).model_dump(mode="json", exclude_none=True)
        )
    return elements


def validate_frame_schedule(snapshot: EditProposalSnapshot) -> None:
    """Fence server-authored frame timing against the approved editorial contract."""

    from app.schemas.edit_frame_schedule import EditFrameSchedule

    schedule = snapshot.frame_schedule
    if schedule is None:
        return
    # model_copy() is intentionally not validation; all writers must pass this
    # fence before the copied snapshot can become an approval/render contract.
    try:
        schedule = EditFrameSchedule.model_validate(schedule.model_dump())
    except ValueError as exc:
        raise GuidedStoryError(
            "guided_story_snapshot_invalid", "The frame schedule is invalid."
        ) from exc
    if (
        schedule.direction != snapshot.direction
        or abs(schedule.total_frames / 30 - float(snapshot.duration_s)) > 0.000001
    ):
        raise GuidedStoryError(
            "guided_story_snapshot_invalid",
            "The schedule no longer matches the edit length or direction.",
        )
    if (
        snapshot.narration is not None
        and abs(
            schedule.total_frames / 30
            - canonical_narration_duration_s(snapshot.narration.duration_s)
        )
        > 0.000001
    ):
        raise GuidedStoryError(
            "guided_story_duration_impossible", "The schedule must preserve the full narration."
        )
    transition_type, transition_s = guided_transition_params(
        snapshot.direction, snapshot.pace, snapshot.mixed_media_timing
    )
    expected_overlap = round(transition_s * 30) if transition_type != "none" else 0
    if len(schedule.moments) == 1:
        expected_overlap = 0
    if schedule.transition_frames != expected_overlap:
        raise GuidedStoryError(
            "guided_story_snapshot_invalid",
            "The schedule no longer matches the approved transitions.",
        )
    by_id = {ref.media_id: ref for ref in snapshot.media}
    seen_ids: set[str] = set()
    windows: dict[str, list[tuple[int, int]]] = {}
    for moment in schedule.moments:
        ref = by_id.get(moment.media_id)
        if ref is None or moment.moment_id in seen_ids:
            raise GuidedStoryError(
                "guided_story_snapshot_invalid",
                "The schedule has missing media or duplicate moments.",
            )
        seen_ids.add(moment.moment_id)
        if ref.kind == "video":
            if (
                ref.duration_s is None
                or moment.source_end_frame / 30 > float(ref.duration_s) + 0.000001
            ):
                raise GuidedStoryError(
                    "guided_story_duration_impossible",
                    "A scheduled source window exceeds the uploaded video.",
                )
            windows.setdefault(ref.media_id, []).append(
                (moment.source_start_frame, moment.source_end_frame)
            )
        expected_layout = (
            snapshot.image_layout if ref.kind == "image" and snapshot.image_layout else None
        )
        if expected_layout is not None and moment.layout != expected_layout:
            raise GuidedStoryError(
                "guided_story_snapshot_invalid",
                "The schedule no longer matches the approved photo layout.",
            )
    reuse = snapshot.video_reuse_policy or "once"
    for source_windows in windows.values():
        if reuse == "once" and len(source_windows) != 1:
            raise GuidedStoryError("guided_story_snapshot_invalid", "A video may appear only once.")
        if reuse != "allow_repeat":
            ordered = sorted(source_windows)
            if any(right[0] < left[1] for left, right in zip(ordered, ordered[1:])):
                raise GuidedStoryError(
                    "guided_story_snapshot_invalid",
                    "Scheduled source windows overlap without permission.",
                )
    if snapshot.direction == "fast_montage":
        cuts = snapshot.fast_cuts or []
        if len(cuts) != len(schedule.moments):
            raise GuidedStoryError(
                "guided_story_snapshot_invalid", "The schedule no longer matches the approved cuts."
            )
        for cut, moment in zip(cuts, schedule.moments, strict=True):
            if (cut.cut_id, cut.media_id) != (
                moment.moment_id,
                moment.media_id,
            ) or moment.beat_id != cut.cut_id:
                raise GuidedStoryError(
                    "guided_story_snapshot_invalid",
                    "The schedule no longer matches the approved cut order.",
                )
            pairs = (
                (cut.source_start_s, moment.source_start_frame),
                (cut.source_end_s, moment.source_end_frame),
                (cut.output_duration_s, moment.output_end_frame - moment.output_start_frame),
            )
            if any(abs(seconds - frames / 30) > 0.000001 for seconds, frames in pairs):
                raise GuidedStoryError(
                    "guided_story_snapshot_invalid",
                    "The schedule no longer matches the approved source windows.",
                )
    else:
        expected = [
            (beat.beat_id, media_id, beat.layout)
            for beat in snapshot.story_beats
            for media_id in beat.media_ids
        ]
        actual = [(moment.beat_id, moment.media_id, moment.layout) for moment in schedule.moments]
        # The global image layout is applied at final normalization.
        expected = [
            (
                beat_id,
                media_id,
                snapshot.image_layout
                if by_id[media_id].kind == "image" and snapshot.image_layout
                else layout,
            )
            for beat_id, media_id, layout in expected
        ]
        if expected != actual:
            raise GuidedStoryError(
                "guided_story_snapshot_invalid",
                "The schedule no longer matches the approved chapters.",
            )
    selected = set(_selected_media_ids(snapshot))
    if selected != {moment.media_id for moment in schedule.moments}:
        raise GuidedStoryError(
            "guided_story_snapshot_invalid", "The schedule dropped approved media."
        )


def _compile_scheduled_execution_plan(
    snapshot: EditProposalSnapshot,
    *,
    proposal_version: int,
    media_digest: str,
    track: dict[str, Any] | None,
) -> dict[str, Any]:
    """Compiler v8 is a projection of approved frames, never another allocator."""

    if snapshot.frame_schedule is None:
        raise GuidedStoryError(
            "guided_story_snapshot_invalid", "Compiler v8 needs an approved frame schedule."
        )
    validate_frame_schedule(snapshot)
    schedule = snapshot.frame_schedule
    by_id = {ref.media_id: ref for ref in snapshot.media}
    beats = {beat.beat_id: beat for beat in snapshot.story_beats}
    moments: list[dict[str, Any]] = []
    for moment in schedule.moments:
        ref = by_id[moment.media_id]
        beat = beats.get(moment.beat_id)
        moments.append(
            {
                "moment_id": moment.moment_id,
                "beat_id": moment.beat_id,
                "topic": beat.topic if beat is not None else moment.role,
                "media_id": moment.media_id,
                "lane": ref.lane,
                "kind": ref.kind,
                "gcs_path": ref.gcs_path,
                "generation": ref.generation,
                "layout": moment.layout,
                "source_start_s": moment.source_start_frame / 30,
                "source_end_s": moment.source_end_frame / 30,
                "output_start_s": moment.output_start_frame / 30,
                "output_end_s": moment.output_end_frame / 30,
                "duration_s": (moment.output_end_frame - moment.output_start_frame) / 30,
                "image_motion": None,
                "required": True,
            }
        )
    beat_windows: list[dict[str, Any]] = []
    groups: list[tuple[str, int, int]] = []
    for moment in schedule.moments:
        if groups and groups[-1][0] == moment.beat_id:
            groups[-1] = (moment.beat_id, groups[-1][1], moment.output_end_frame)
        else:
            groups.append((moment.beat_id, moment.output_start_frame, moment.output_end_frame))
    for index, (beat_id, start, _end) in enumerate(groups):
        end = groups[index + 1][1] if index + 1 < len(groups) else schedule.total_frames
        beat_windows.append(
            {
                "beat_id": beat_id,
                "approved_duration_s": (end - start) / 30,
                "resolved_duration_s": (end - start) / 30,
                "start_s": start / 30,
                "end_s": end / 30,
            }
        )
    policy = _DIRECTION_POLICY[snapshot.direction]
    text_elements = _text_elements(snapshot, beat_windows, policy, compiler_version=8)
    if snapshot.direction == "fast_montage" and snapshot.montage_text_bindings:
        # v1-7 chose either title or labels. Both are named requirements for a
        # scheduled edit; preserve the title, exact labels, and narration lane.
        title_snapshot = snapshot.model_copy(update={"montage_text_bindings": []})
        text_elements = _text_elements(title_snapshot, beat_windows, policy, compiler_version=8)
        binding_snapshot = snapshot.model_copy(update={"opening_title": None, "narration": None})
        bindings = _text_elements(binding_snapshot, beat_windows, policy, compiler_version=8)
        text_elements.extend(
            element for element in bindings if element["id"].startswith("montage-text-")
        )
    total_s = schedule.total_frames / 30
    song_reference = _song_reference(track, duration_s=total_s)
    compiled = GuidedStoryExecutionPlan(
        compiler_version=8,
        proposal_version=proposal_version,
        media_digest=media_digest,
        direction=snapshot.direction,
        goal=snapshot.goal,
        pace=snapshot.pace,
        approved_duration_s=float(snapshot.duration_s),
        resolved_duration_s=total_s,
        output_orientation=snapshot.output_orientation or "portrait",
        output_orientation_reason=snapshot.output_orientation_reason,
        selected_media_ids=_selected_media_ids(snapshot),
        story_timeline=moments,
        beat_windows=beat_windows,
        text_elements=text_elements,
        transition_policy={
            "type": "crossfade" if schedule.transition_frames else "none",
            "duration_s": schedule.transition_frames / 30,
        },
        mixed_media_timing=snapshot.mixed_media_timing,
        montage_cadence=snapshot.montage_cadence,
        montage_text_bindings=[
            binding.model_dump(mode="json") for binding in snapshot.montage_text_bindings
        ],
        montage_audio=snapshot.montage_audio.model_dump(mode="json")
        if snapshot.montage_audio
        else None,
        licensed_sfx_intent=snapshot.licensed_sfx.model_dump(mode="json")
        if snapshot.licensed_sfx
        else None,
        typography={"style_id": "guided_story_v2", "font": snapshot.font_family or "Fraunces"},
        music=None,
        song_reference=song_reference,
        song_reference_track_duration_s=float(
            track.get("catalog_duration_s") or track.get("duration_s")
        )
        if song_reference and track
        else None,
        narration=snapshot.narration,
    )
    return compiled.model_dump(mode="json", exclude_none=False)


def _compile_execution_plan_version(
    guided_snapshot: object,
    *,
    track: dict[str, Any] | None,
    compiler_version: Literal[1, 2, 3, 4, 5, 6, 7, 8],
) -> dict[str, Any]:
    """Compile a deterministic plan with an explicitly versioned allocator."""

    proposal_version, media_digest, snapshot = validate_guided_snapshot(guided_snapshot)
    if compiler_version == SCHEDULED_COMPILER_VERSION:
        return _compile_scheduled_execution_plan(
            snapshot, proposal_version=proposal_version, media_digest=media_digest, track=track
        )
    if snapshot.frame_schedule is not None:
        raise GuidedStoryError(
            "guided_story_snapshot_invalid", "A scheduled edit needs compiler v8."
        )
    policy = _DIRECTION_POLICY[snapshot.direction]
    mixed_timing = snapshot.mixed_media_timing
    selected_ids = _selected_media_ids(snapshot)
    by_id = {ref.media_id: ref for ref in snapshot.media}
    selected_kinds = {by_id[media_id].kind for media_id in selected_ids if media_id in by_id}
    quick_mixed_timing = uses_quick_photo_long_video_timing(mixed_timing)
    if quick_mixed_timing and selected_kinds != {"image", "video"}:
        # The typed contract describes a relationship between photos and
        # videos. If the retained timeline has only one kind, preserve the
        # legacy renderer instead of enforcing a false mixed-media receipt.
        mixed_timing = None
        quick_mixed_timing = False
    transition_type, transition_duration_s = guided_transition_params(
        snapshot.direction, snapshot.pace, mixed_timing
    )
    if not selected_ids:
        raise GuidedStoryError("guided_story_snapshot_invalid", "The approved edit has no media.")
    if compiler_version >= 3:
        output_orientation = snapshot.output_orientation or "portrait"
        output_orientation_reason = snapshot.output_orientation_reason
    else:
        output_orientation = "portrait"
        output_orientation_reason = "Legacy guided stories used the portrait canvas."

    target_duration_s = (
        canonical_narration_duration_s(snapshot.narration.duration_s)
        if snapshot.narration is not None
        else float(snapshot.duration_s)
    )
    weight_total = sum(float(beat.duration_s) for beat in snapshot.story_beats)
    if weight_total <= 0:
        raise GuidedStoryError(
            "guided_story_duration_impossible", "The approved story has no usable timing."
        )
    beat_windows: list[dict[str, Any]] = []
    moments: list[dict[str, Any]] = []
    if snapshot.direction == "fast_montage" and snapshot.fast_cuts:
        cursor = 0.0
        output_windows = _fast_montage_output_windows(
            snapshot.fast_cuts,
            duration_s=target_duration_s,
            track=track,
            video_media_ids={ref.media_id for ref in snapshot.media if ref.kind == "video"},
            mixed_media_timing=snapshot.mixed_media_timing,
            preserve_exact_cadence=snapshot.montage_cadence is not None,
        )
        for cut, (start_s, end_s, beat_time_s) in zip(
            snapshot.fast_cuts, output_windows, strict=True
        ):
            ref = by_id.get(cut.media_id)
            if ref is None:
                raise GuidedStoryError(
                    "guided_story_snapshot_invalid", "A fast montage cut references missing media."
                )
            if ref.kind == "video" and cut.source_end_s > float(ref.duration_s or 0.0) + 0.05:
                raise GuidedStoryError(
                    "guided_story_duration_impossible",
                    f"Fast montage source window exceeds {ref.source_filename or ref.media_id}.",
                )
            resolved_duration_s = round(end_s - start_s, 3)
            resolved_source_end_s = float(cut.source_end_s)
            if ref.kind == "video":
                resolved_source_end_s = float(cut.source_start_s) + resolved_duration_s
                if resolved_source_end_s > float(cut.source_end_s) + 0.001:
                    raise GuidedStoryError(
                        "guided_story_duration_impossible",
                        "Beat alignment cannot lengthen an approved video source window.",
                    )
            moments.append(
                {
                    "moment_id": cut.cut_id,
                    "beat_id": cut.cut_id,
                    "topic": cut.role,
                    "media_id": cut.media_id,
                    "lane": ref.lane,
                    "kind": ref.kind,
                    "gcs_path": ref.gcs_path,
                    "generation": ref.generation,
                    "layout": (
                        snapshot.image_layout
                        if ref.kind == "image" and snapshot.image_layout is not None
                        else "fullscreen"
                    ),
                    "source_start_s": round(float(cut.source_start_s), 3),
                    "source_end_s": round(resolved_source_end_s, 3),
                    "output_start_s": start_s,
                    "output_end_s": end_s,
                    "duration_s": resolved_duration_s,
                    "image_motion": None,
                    "beat_align": bool(cut.beat_align),
                    "beat_time_s": beat_time_s,
                    "required": True,
                }
            )
            beat_windows.append(
                {
                    "beat_id": cut.cut_id,
                    "approved_duration_s": round(float(cut.output_duration_s), 3),
                    "resolved_duration_s": resolved_duration_s,
                    "start_s": start_s,
                    "end_s": end_s,
                }
            )
            cursor = end_s
        if abs(cursor - target_duration_s) > 0.15:
            raise GuidedStoryError(
                "guided_story_duration_impossible",
                "Fast montage cut durations do not match the approved duration.",
            )
        if quick_mixed_timing:
            cursor = _quantize_quick_mixed_timeline(
                moments,
                beat_windows,
                target_s=target_duration_s,
                mixed_media_timing=mixed_timing,
            )
        normalized_track = (
            _music_payload(track, duration_s=cursor) if compiler_version < 6 else None
        )
        song_reference = (
            _song_reference(track, duration_s=cursor) if compiler_version >= 6 else None
        )
        try:
            compiled = GuidedStoryExecutionPlan(
                compiler_version=compiler_version,
                proposal_version=proposal_version,
                media_digest=media_digest,
                direction=snapshot.direction,
                goal=snapshot.goal,
                pace=snapshot.pace,
                approved_duration_s=float(snapshot.duration_s),
                resolved_duration_s=round(cursor, 3),
                output_orientation=output_orientation,
                output_orientation_reason=output_orientation_reason,
                selected_media_ids=selected_ids,
                story_timeline=moments,
                beat_windows=beat_windows,
                mixed_media_timing=mixed_timing,
                montage_cadence=snapshot.montage_cadence,
                # The renderers read the creator's source-audio choice from the
                # plan, not the snapshot. Without it a fast montage reports
                # source_audio_preserved=False and every clip is muted.
                montage_audio=(
                    snapshot.montage_audio.model_dump(mode="json")
                    if compiler_version >= 7 and snapshot.montage_audio is not None
                    else None
                ),
                licensed_sfx_intent=(
                    snapshot.licensed_sfx.model_dump(mode="json")
                    if snapshot.licensed_sfx is not None
                    else None
                ),
                text_elements=_text_elements(
                    snapshot, beat_windows, policy, compiler_version=compiler_version
                ),
                transition_policy={"type": "none", "duration_s": 0.0},
                typography=(
                    {"style_id": "guided_story_v2", "font": snapshot.font_family or "Fraunces"}
                    if compiler_version >= 3
                    else {
                        "style_id": "guided_story_v1",
                        "font": snapshot.font_family or "Inter-Bold",
                    }
                ),
                music=normalized_track,
                song_reference=song_reference,
                song_reference_track_duration_s=(
                    float(track.get("catalog_duration_s") or track.get("duration_s"))
                    if song_reference is not None and track is not None
                    else None
                ),
                narration=snapshot.narration,
            )
        except Exception as exc:  # noqa: BLE001
            raise GuidedStoryError(
                "guided_story_snapshot_invalid", "The fast montage could not be compiled safely."
            ) from exc
        return compiled.model_dump(mode="json", exclude_none=False)
    moment_count = sum(len(beat.media_ids) for beat in snapshot.story_beats)
    cursor = 0.0
    planned_beats = (
        _allocate_beat_windows(
            snapshot,
            by_id=by_id,
            policy=policy,
            transition_type=transition_type,
            transition_duration_s=transition_duration_s,
            mixed_media_timing=mixed_timing,
        )
        if compiler_version >= 4
        else None
    )
    for beat_index, beat in enumerate(snapshot.story_beats):
        if planned_beats is not None:
            resolved_beat_s = planned_beats[beat_index]
        elif beat_index == len(snapshot.story_beats) - 1:
            resolved_beat_s = round(target_duration_s - cursor, 3)
        else:
            resolved_beat_s = _round_frame(
                target_duration_s * float(beat.duration_s) / weight_total
            )
        beat_refs = [by_id[media_id] for media_id in beat.media_ids]
        overlaps_s = [
            transition_duration_s
            if transition_type != "none" and len(moments) + offset != moment_count - 1
            else 0.0
            for offset in range(len(beat_refs))
        ]
        if compiler_version == 1:
            per_media = resolved_beat_s / len(beat.media_ids)
            if per_media + _FRAME_S < float(policy["min_moment_s"]):
                raise GuidedStoryError(
                    "guided_story_duration_impossible",
                    f"Beat {beat.topic} is too short to show all approved media clearly.",
                )
            legacy_cursor = cursor
            moment_durations = []
            for media_index in range(len(beat.media_ids)):
                if media_index == len(beat.media_ids) - 1:
                    moment_s = round(cursor + resolved_beat_s - legacy_cursor, 3)
                else:
                    moment_s = _round_frame(per_media)
                moment_durations.append(moment_s)
                legacy_cursor = round(legacy_cursor + moment_s, 3)
        else:
            moment_durations = _allocate_beat_durations(
                beat_refs,
                beat_duration_s=resolved_beat_s,
                min_moment_s=float(policy["min_moment_s"]),
                overlaps_s=overlaps_s,
                beat_topic=beat.topic,
                mixed_media_timing=mixed_timing,
            )
        beat_start = cursor
        for media_index, media_id in enumerate(beat.media_ids):
            ref = by_id[media_id]
            moment_s = moment_durations[media_index]
            # Xfade consumes the overlap from the joined result. Extend every
            # input except the last by exactly that overlap so the approved
            # top-level duration remains authoritative in the final output.
            render_s = round(moment_s + overlaps_s[media_index], 3)
            source_start, source_end = _source_window(ref, render_s)
            moments.append(
                {
                    "moment_id": f"{beat.beat_id}:{media_index + 1}",
                    "beat_id": beat.beat_id,
                    "topic": beat.topic,
                    "media_id": media_id,
                    "lane": ref.lane,
                    "kind": ref.kind,
                    "gcs_path": ref.gcs_path,
                    "generation": ref.generation,
                    "layout": (
                        snapshot.image_layout
                        if ref.kind == "image" and snapshot.image_layout is not None
                        else beat.layout
                    ),
                    "source_start_s": source_start,
                    "source_end_s": source_end,
                    "output_start_s": round(cursor, 3),
                    "output_end_s": round(cursor + render_s, 3),
                    "duration_s": round(render_s, 3),
                    "image_motion": None,
                    "required": True,
                }
            )
            cursor = round(cursor + moment_s, 3)
        beat_windows.append(
            {
                "beat_id": beat.beat_id,
                "approved_duration_s": float(beat.duration_s),
                "resolved_duration_s": round(resolved_beat_s, 3),
                "start_s": round(beat_start, 3),
                "end_s": round(cursor, 3),
            }
        )
    if abs(cursor - target_duration_s) > 0.05:
        raise GuidedStoryError(
            "guided_story_duration_impossible", "The approved story timing could not be resolved."
        )
    if quick_mixed_timing:
        cursor = _quantize_quick_mixed_timeline(
            moments,
            beat_windows,
            target_s=cursor,
            mixed_media_timing=mixed_timing,
        )

    normalized_track = _music_payload(track, duration_s=cursor) if compiler_version < 6 else None
    song_reference = _song_reference(track, duration_s=cursor) if compiler_version >= 6 else None

    try:
        compiled = GuidedStoryExecutionPlan(
            compiler_version=compiler_version,
            proposal_version=proposal_version,
            media_digest=media_digest,
            direction=snapshot.direction,
            goal=snapshot.goal,
            pace=snapshot.pace,
            approved_duration_s=float(snapshot.duration_s),
            resolved_duration_s=round(cursor, 3),
            output_orientation=output_orientation,
            output_orientation_reason=output_orientation_reason,
            selected_media_ids=selected_ids,
            story_timeline=moments,
            beat_windows=beat_windows,
            mixed_media_timing=mixed_timing,
            montage_cadence=snapshot.montage_cadence,
            montage_text_bindings=[
                binding.model_dump(mode="json") for binding in snapshot.montage_text_bindings
            ],
            montage_audio=(
                snapshot.montage_audio.model_dump(mode="json")
                if snapshot.montage_audio is not None
                else None
            ),
            licensed_sfx_intent=(
                snapshot.licensed_sfx.model_dump(mode="json")
                if snapshot.licensed_sfx is not None
                else None
            ),
            text_elements=_text_elements(
                snapshot,
                beat_windows,
                policy,
                compiler_version=compiler_version,
            ),
            transition_policy={
                "type": transition_type,
                "duration_s": transition_duration_s,
            },
            typography=(
                {"style_id": "guided_story_v2", "font": snapshot.font_family or "Fraunces"}
                if compiler_version >= 3
                else {"style_id": "guided_story_v1", "font": snapshot.font_family or "Inter-Bold"}
            ),
            music=normalized_track,
            song_reference=song_reference,
            song_reference_track_duration_s=(
                float(track.get("catalog_duration_s") or track.get("duration_s"))
                if song_reference is not None and track is not None
                else None
            ),
            narration=snapshot.narration,
        )
    except Exception as exc:  # noqa: BLE001
        raise GuidedStoryError(
            "guided_story_snapshot_invalid", "The approved edit could not be compiled safely."
        ) from exc
    return compiled.model_dump(mode="json", exclude_none=False)


def compile_execution_plan(
    guided_snapshot: object,
    *,
    track: dict[str, Any] | None,
) -> dict[str, Any]:
    """Compile a deterministic task-owned plan with the current compiler."""

    _proposal_version, _media_digest, snapshot = validate_guided_snapshot(guided_snapshot)
    return _compile_execution_plan_version(
        guided_snapshot,
        track=track,
        compiler_version=(
            SCHEDULED_COMPILER_VERSION
            if snapshot.frame_schedule is not None
            else VOICEOVER_COMPILER_VERSION
            if snapshot.narration is not None
            else COMPILER_VERSION
        ),
    )


def validate_proposal_timing(snapshot: EditProposalSnapshot) -> None:
    """Reject an editorial revision that the strict renderer cannot allocate."""

    if snapshot.fast_cuts:
        media_by_id = {ref.media_id: ref for ref in snapshot.media}
        previous_id: str | None = None
        windows_by_media: dict[str, list[tuple[float, float]]] = {}
        for cut in snapshot.fast_cuts:
            if cut.media_id == previous_id and snapshot.video_reuse_policy != "allow_repeat":
                raise GuidedStoryError(
                    "guided_story_snapshot_invalid",
                    "Fast montage cuts cannot repeat the same media adjacently.",
                )
            previous_id = cut.media_id
            ref = media_by_id.get(cut.media_id)
            if ref is not None and ref.kind == "video":
                windows_by_media.setdefault(cut.media_id, []).append(
                    (float(cut.source_start_s), float(cut.source_end_s))
                )
        if not (
            snapshot.video_reuse_policy == "allow_repeat"
            or (
                snapshot.montage_cadence is not None
                and snapshot.montage_cadence.reuse_policy == "allow_repeat"
            )
        ):
            for windows in windows_by_media.values():
                windows.sort()
                for previous, current in zip(windows, windows[1:]):
                    if current[0] < previous[1] - _DURATION_MATCH_TOLERANCE_S:
                        raise GuidedStoryError(
                            "guided_story_snapshot_invalid",
                            "Fast montage cuts cannot reuse overlapping video footage.",
                        )

    validate_proposal_compiles(snapshot)


def validate_proposal_compiles(snapshot: EditProposalSnapshot) -> None:
    """Dry-run the strict compiler: can the renderer allocate this proposal at all?

    Narrower than `validate_proposal_timing`, which also applies editorial
    fast-cut rules meant for creator revisions. Every freshly planned draft is
    checked with this before it is saved, so a plan the renderer cannot
    allocate fails at planning time instead of after approval (KRI-129).
    """

    media_digest = canonical_media_digest(snapshot.media, snapshot.narration)
    compile_execution_plan(
        {
            "proposal_version": 1,
            "media_digest": media_digest,
            "approved_proposal": snapshot.model_dump(mode="json"),
            "media_identities": [
                {
                    "lane": ref.lane,
                    "media_id": ref.media_id,
                    "gcs_path": ref.gcs_path,
                    "generation": ref.generation,
                    "kind": ref.kind,
                }
                for ref in snapshot.media
            ],
        },
        track=None,
    )


def validate_execution_plan(plan: object, guided_snapshot: object) -> dict[str, Any]:
    proposal_version, media_digest, snapshot = validate_guided_snapshot(guided_snapshot)
    try:
        # Old approved plans can retain the retired raw intent alongside their
        # final rendered label snapshots. Ignore only that obsolete input so
        # retries preserve the snapshots without reviving the legacy lane.
        plan_payload = dict(plan) if isinstance(plan, dict) else plan
        if isinstance(plan_payload, dict):
            plan_payload.pop("context_label_intent", None)
        validated = GuidedStoryExecutionPlan.model_validate(plan_payload)
    except Exception as exc:  # noqa: BLE001
        raise GuidedStoryError(
            "guided_story_snapshot_invalid", "The saved render plan is incomplete."
        ) from exc
    if validated.proposal_version != proposal_version or validated.media_digest != media_digest:
        raise GuidedStoryError(
            "guided_story_snapshot_invalid", "The saved render plan no longer matches approval."
        )
    validation_track = (
        validated.music.model_dump(mode="json") if validated.music is not None else None
    )
    if validated.song_reference is not None:
        reference = validated.song_reference.model_dump(mode="json")
        reference["catalog_duration_s"] = (
            validated.song_reference_track_duration_s or reference["end_s"]
        )
        reference["beat_timestamps_s"] = []
        validation_track = reference
    elif (
        validated.compiler_version >= 6
        and validation_track is None
        and validated.direction == "fast_montage"
        and any(moment.beat_time_s is not None for moment in validated.story_timeline)
    ):
        # Older test/fixture match payloads may omit catalog identity while
        # still supplying beat data. Preserve the deterministic snap inputs
        # without manufacturing a persisted song reference.
        validation_track = {"start_s": 0.0, "beat_timestamps_s": []}
    if validation_track is not None and validated.direction == "fast_montage":
        # Beat timestamps are compiler-only input and intentionally absent from
        # the persisted music receipt. Reconstruct only the exact beats that
        # affected persisted cut boundaries so canonical validation can replay
        # the original deterministic snap instead of falsely rejecting it.
        music_start_s = float(validation_track.get("start_s") or 0.0)
        snapped_beats = [
            round(music_start_s + float(moment.beat_time_s), 3)
            for moment in validated.story_timeline
            if moment.beat_time_s is not None
        ]
        if snapped_beats:
            validation_track["beat_timestamps_s"] = snapped_beats
    canonical = _compile_execution_plan_version(
        guided_snapshot,
        track=validation_track,
        compiler_version=validated.compiler_version,
    )
    normalized = validated.model_dump(mode="json", exclude_none=False)
    # Context-label TextElements are a server-derived projection of the
    # approved metadata and the current output timeline. They are deliberately
    # not part of the proposal compiler's editor-authored text contract and are
    # rematerialized by the worker on each render/revision.
    normalized.pop("context_label_text_elements", None)
    canonical.pop("context_label_text_elements", None)
    normalized.pop("narration_label_text_elements", None)
    canonical.pop("narration_label_text_elements", None)
    normalized.pop("narration_label_receipt", None)
    canonical.pop("narration_label_receipt", None)
    runtime_sfx = list(normalized.pop("editor_sound_effects", []) or [])
    canonical.pop("editor_sound_effects", None)
    if normalized != canonical:
        raise GuidedStoryError(
            "guided_story_snapshot_invalid", "The saved render plan was changed after approval."
        )
    normalized["context_label_text_elements"] = [
        element.model_dump(mode="json") for element in validated.context_label_text_elements
    ]
    normalized["narration_label_text_elements"] = [
        element.model_dump(mode="json") for element in validated.narration_label_text_elements
    ]
    normalized["narration_label_receipt"] = validated.narration_label_receipt
    if runtime_sfx and validated.editor_revision_number is None:
        from app.agents._schemas.sound_effect import (  # noqa: PLC0415
            SoundEffectPlacement,
            validate_sfx_gcs_path,
        )

        intent = snapshot.licensed_sfx
        if intent is None or len(runtime_sfx) > intent.max_placements:
            raise GuidedStoryError(
                "guided_story_sfx_invalid",
                "The licensed sound-effect plan no longer matches the confirmed request.",
            )
        try:
            placements = [SoundEffectPlacement.model_validate(row) for row in runtime_sfx]
            placements.sort(key=lambda row: row.at_s)
            for placement in placements:
                validate_sfx_gcs_path(placement.src_gcs_path)
                if (
                    str(placement.sound_effect_id or "").strip().casefold()
                    != str(intent.effect_id).strip().casefold()
                ):
                    raise ValueError("licensed effect identity changed")
                if placement.at_s >= max(0.0, validated.resolved_duration_s - 0.5):
                    raise ValueError("licensed effect violates end keepout")
            if any(
                current.at_s - previous.at_s < 1.5
                for previous, current in zip(placements, placements[1:], strict=False)
            ):
                raise ValueError("licensed effects are too closely spaced")
        except Exception as exc:  # noqa: BLE001
            raise GuidedStoryError(
                "guided_story_sfx_invalid",
                "The licensed sound-effect plan no longer matches the confirmed request.",
            ) from exc
    normalized["editor_sound_effects"] = runtime_sfx
    return normalized


def execution_plan_with_editor_state(
    plan: object,
    *,
    output_orientation: Literal["portrait", "landscape"],
    text_elements: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Create a strict render-time plan for an approved editor canvas change.

    Media, timing, music, proposal version, and digest remain pinned. Only the
    output canvas and already-validated text document may differ from approval.
    """

    try:
        typed = GuidedStoryExecutionPlan.model_validate(plan)
        label_ids = {element.id for element in typed.narration_label_text_elements}
        next_text_elements = typed.text_elements
        next_label_elements = typed.narration_label_text_elements
        if text_elements is not None:
            supplied = [TextElement.model_validate(row) for row in text_elements]
            supplied_by_id = {element.id: element for element in supplied}
            approved_text_ids = {element.id for element in typed.text_elements}
            supplied_text_ids = {
                element.id for element in supplied if element.id in approved_text_ids
            }
            if supplied_text_ids != approved_text_ids:
                raise ValueError("edited text identities must match approval")
            if label_ids.intersection(supplied_by_id):
                if label_ids - supplied_by_id.keys():
                    raise ValueError("edited narration label identities must match approval")
                next_label_elements = [
                    supplied_by_id[element.id] for element in typed.narration_label_text_elements
                ]
            next_text_elements = [
                element
                if (
                    typed.narration is not None
                    and (element.source_params or {}).get("source") == CAPTION_CUE_SOURCE
                )
                else supplied_by_id[element.id]
                if element.id in supplied_by_id
                else element
                for element in typed.text_elements
            ]
        updated = typed.model_copy(
            update={
                "output_orientation": output_orientation,
                "output_orientation_reason": (
                    "The creator selected this output format in the editor."
                ),
                "text_elements": next_text_elements,
                "narration_label_text_elements": next_label_elements,
            }
        )
        approved_ids = [element.id for element in typed.text_elements]
        if [element.id for element in updated.text_elements] != approved_ids:
            raise ValueError("edited text identities must match approval")
        return GuidedStoryExecutionPlan.model_validate(updated).model_dump(
            mode="json", exclude_none=False
        )
    except Exception as exc:  # noqa: BLE001
        raise GuidedStoryError(
            "guided_story_snapshot_invalid",
            "The approved story could not be safely resized.",
        ) from exc


def _project_source_bound_narration_labels(
    labels: list[TextElement], moments: list[dict[str, Any]]
) -> list[TextElement]:
    """Move participant labels with their authored timeline/asset anchor.

    Labels without one of these source parameters retain their absolute
    narration times, which is required for score and sport annotations.
    """

    by_timeline_id = {str(moment.get("moment_id")): moment for moment in moments}
    by_asset_id: dict[str, dict[str, Any]] = {}
    for moment in moments:
        by_asset_id.setdefault(str(moment.get("media_id")), moment)
    projected: list[TextElement] = []
    for label in labels:
        params = dict(label.source_params or {})
        anchor = None
        timeline_id = str(params.get("source_timeline_id") or "")
        asset_id = str(params.get("source_asset_id") or "")
        if timeline_id:
            anchor = by_timeline_id.get(timeline_id)
        elif asset_id:
            anchor = by_asset_id.get(asset_id)
        if anchor is None:
            projected.append(label)
            continue
        projected.append(
            label.model_copy(
                update={
                    "start_s": float(anchor["output_start_s"]),
                    "end_s": float(anchor["output_end_s"]),
                }
            )
        )
    return projected


def compile_guided_runtime_plan(
    canonical_plan: object,
    guided_snapshot: object,
    revision: object,
    *,
    admitted_sources: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Compile a validated v2 revision without mutating the approval plan.

    The canonical plan remains the provenance fence.  This projection only
    changes the effective moments/timing/audio and carries the revision hash
    into the receipt; every selected source still comes from the approved
    snapshot's exact-generation pool.
    """

    from app.schemas.guided_edit_revision import (
        GuidedEditorSource,
        normalize_guided_editor_revision,
    )

    try:
        canonical_payload = (
            dict(canonical_plan) if isinstance(canonical_plan, dict) else canonical_plan
        )
        if isinstance(canonical_payload, dict):
            canonical_payload.pop("context_label_intent", None)
        canonical = GuidedStoryExecutionPlan.model_validate(canonical_payload)
        proposal_version, media_digest, snapshot = validate_guided_snapshot(guided_snapshot)
        normalized_revision = normalize_guided_editor_revision(
            revision,
            expected_approval_version=proposal_version,
            expected_media_digest=media_digest,
        )
        # Only trusted server receipts may extend the immutable approval. The
        # client revision itself can never authorize a new path or generation.
        source_by_id = {ref.media_id: ref for ref in snapshot.media}
        for row in admitted_sources or []:
            source = GuidedEditorSource.model_validate(row)
            previous = source_by_id.get(source.media_id)
            if previous is not None and any(
                getattr(previous, field) != getattr(source, field)
                for field in GuidedEditorSource.model_fields
            ):
                raise ValueError("admitted source conflicts with approved identity")
            source_by_id[source.media_id] = source
        approved_sources = {
            (
                ref.media_id,
                ref.lane,
                ref.gcs_path,
                ref.generation,
                ref.kind,
                ref.duration_s,
            )
            for ref in source_by_id.values()
        }
        revision_sources = {
            (
                row["media_id"],
                row["lane"],
                row["gcs_path"],
                row["generation"],
                row["kind"],
                row.get("duration_s"),
            )
            for row in normalized_revision["sources"]
        }
        if revision_sources != approved_sources:
            raise GuidedStoryError(
                "guided_story_revision_invalid",
                "The revision source pool no longer matches the approved snapshot.",
            )
        approved_label_ids = [element.id for element in canonical.narration_label_text_elements]
        approved_text_ids = [element.id for element in canonical.text_elements] + approved_label_ids
        revision_text_ids = [
            str(row.get("id")) for row in normalized_revision.get("text_elements") or []
        ]
        tombstoned_text_ids = [
            str(row.get("record_id"))
            for row in normalized_revision.get("tombstones") or []
            if row.get("lane") == "text_elements" and row.get("record_id")
        ]
        revision_text_id_set = set(revision_text_ids)
        tombstoned_text_id_set = set(tombstoned_text_ids)
        if (
            len(revision_text_ids) != len(revision_text_id_set)
            or len(tombstoned_text_ids) != len(tombstoned_text_id_set)
            or revision_text_id_set.intersection(tombstoned_text_id_set)
            or not set(approved_text_ids).issubset(revision_text_id_set | tombstoned_text_id_set)
        ):
            raise GuidedStoryError(
                "guided_story_revision_invalid",
                "Approved text identities must remain active or tombstoned exactly once.",
            )
        # Music swaps are post-approval editor revisions. The route pins the
        # selected ready track's exact object generation; the worker then
        # downloads that generation. The immutable proposal is provenance, not
        # an allowlist that would make every legitimate swap fail at render.
        base_by_media: dict[str, dict[str, Any]] = {}
        base_by_moment_id: dict[str, dict[str, Any]] = {}
        for moment in canonical.story_timeline:
            dumped = moment.model_dump(mode="json")
            base_by_media.setdefault(moment.media_id, dumped)
            base_by_moment_id[moment.moment_id] = dumped
        moments: list[dict[str, Any]] = []
        beat_windows: list[dict[str, Any]] = []
        for index, segment in enumerate(normalized_revision["segments"]):
            source = source_by_id.get(segment["media_id"])
            if source is None:
                raise ValueError("revision source is not in the approved snapshot")
            base = {}
            for identity in (segment.get("segment_id"), segment.get("parent_segment_id")):
                if identity is not None:
                    base = dict(base_by_moment_id.get(str(identity)) or {})
                    if base:
                        break
            if not base:
                base = dict(base_by_media.get(segment["media_id"]) or {})
            if not base:
                # Unused media in the immutable approval is part of the V2
                # source pool but has no canonical story moment to inherit.
                base = {
                    "topic": "Edited story moment",
                    "layout": "fullscreen",
                    "image_motion": None,
                    "required": True,
                }
            segment_layout = segment.get("layout")
            if isinstance(segment_layout, str) and segment_layout in {
                "fullscreen",
                "supporting_card",
            }:
                base["layout"] = segment_layout
            start = float(segment["output_start_s"])
            end = float(segment["output_end_s"])
            # A source may be reused by multiple split segments.  Runtime
            # beat IDs therefore belong to the revision segment, not the
            # original approved beat, so the strict plan validator remains
            # deterministic for repeated media.
            beat_id = f"guided-edit-beat-{index}"
            moment_id = str(segment["segment_id"])
            moments.append(
                {
                    **base,
                    "moment_id": moment_id,
                    "beat_id": beat_id,
                    "topic": str(base.get("topic") or "Edited story moment"),
                    "media_id": source.media_id,
                    "lane": source.lane,
                    "kind": source.kind,
                    "gcs_path": source.gcs_path,
                    "generation": source.generation,
                    "source_start_s": float(segment["source_start_s"]),
                    "source_end_s": float(
                        segment.get("source_end_s")
                        or float(segment["source_start_s"]) + float(segment["duration_s"])
                    ),
                    **(
                        {"source_crop": segment["source_crop"]}
                        if segment.get("source_crop")
                        else {}
                    ),
                    **(
                        {"playback_rate": float(segment["playback_rate"])}
                        if segment.get("playback_rate") is not None
                        else {}
                    ),
                    "output_start_s": start,
                    "output_end_s": end,
                    "duration_s": float(segment["duration_s"]),
                    "look_preset": segment.get("look_preset", "none"),
                    "look_adjustments": segment.get("look_adjustments"),
                    "transition_after": segment.get("transition_after", "cut"),
                    "transition_duration_s": float(segment.get("transition_duration_s") or 0.0),
                }
            )
            beat_windows.append(
                {
                    "beat_id": beat_id,
                    "approved_duration_s": float(segment["duration_s"]),
                    "resolved_duration_s": float(segment["duration_s"]),
                    "start_s": start,
                    "end_s": end,
                }
            )
        selected_ids = list(dict.fromkeys(moment["media_id"] for moment in moments))
        audio = normalized_revision.get("audio") or {"mode": "none"}
        music = None
        song_reference = (
            canonical.song_reference.model_dump(mode="json") if canonical.song_reference else None
        )
        if canonical.compiler_version >= 6:
            if audio.get("mode") == "track":
                if (
                    song_reference is None
                    or str(audio.get("track_id")) != song_reference["track_id"]
                ):
                    raise GuidedStoryError(
                        "guided_story_revision_invalid",
                        "The revision song does not match the approved reference.",
                    )
                start_s = float(
                    audio["start_s"]
                    if audio.get("start_s") is not None
                    else song_reference["start_s"]
                )
            else:
                start_s = float(song_reference["start_s"]) if song_reference is not None else 0.0
            if song_reference is not None:
                revised_duration_s = max(
                    float(segment["output_end_s"]) for segment in normalized_revision["segments"]
                )
                end_s = start_s + revised_duration_s
                # The public reference intentionally carries no audio URL or
                # catalog duration. The internal catalog-duration fence is the
                # upper bound; reject an edit that cannot fit rather than
                # silently shifting or clamping its timing.
                catalog_end = float(
                    canonical.song_reference_track_duration_s
                    if canonical.song_reference_track_duration_s is not None
                    else song_reference["end_s"]
                )
                if start_s < 0 or end_s > catalog_end + 0.001:
                    raise GuidedStoryError(
                        "guided_story_revision_invalid",
                        "The revised story duration does not fit the pinned song reference.",
                    )
                song_reference = GuidedStorySongReference(
                    **{**song_reference, "start_s": start_s, "end_s": end_s}
                ).model_dump(mode="json")
        elif audio.get("mode") == "track":
            music = {
                "track_id": audio["track_id"],
                "title": audio["title"],
                "audio_gcs_path": audio["audio_gcs_path"],
                "generation": audio["generation"],
                "start_s": float(audio.get("start_s") or 0.0),
                "end_s": float(
                    audio.get("end_s") or normalized_revision["segments"][-1]["output_end_s"]
                ),
                "level": float(audio.get("level", 1.0)),
            }
        canonical_text_by_id = {element.id: element for element in canonical.text_elements}
        revision_elements = [
            TextElement.model_validate(row)
            for row in normalized_revision.get("text_elements") or []
        ]
        revision_by_id = {element.id: element for element in revision_elements}
        if canonical.narration is not None:
            revised_elements: list[TextElement] = []
            for element in revision_elements:
                canonical_element = canonical_text_by_id.get(element.id)
                if (
                    canonical_element is not None
                    and (canonical_element.source_params or {}).get("source") == CAPTION_CUE_SOURCE
                ):
                    # Caption copy and styling are editor-owned. Their timing
                    # and timed-word identity remain pinned to the approved
                    # narration so a text edit cannot move a cue on the audio.
                    element = element.model_copy(
                        update={
                            "start_s": canonical_element.start_s,
                            "end_s": canonical_element.end_s,
                            "word_timings": canonical_element.word_timings,
                            "source_params": canonical_element.source_params,
                        }
                    )
                revised_elements.append(element)
            revision_elements = revised_elements
        active_labels = [
            revision_by_id[label_id]
            for label_id in approved_label_ids
            if label_id in revision_by_id
        ]
        projected_labels = _project_source_bound_narration_labels(active_labels, moments)
        runtime_payload = canonical.model_dump(mode="json", exclude_none=False)
        runtime_payload.update(
            {
                "proposal_version": proposal_version,
                "media_digest": media_digest,
                "approved_duration_s": float(canonical.approved_duration_s),
                "resolved_duration_s": round(
                    max(
                        max(moment["output_end_s"] for moment in moments),
                        canonical_narration_duration_s(canonical.narration.duration_s)
                        if canonical.narration is not None
                        else 0.0,
                    ),
                    3,
                ),
                "output_orientation": normalized_revision.get("orientation", "portrait"),
                "output_orientation_reason": (
                    "The creator selected this output format in the editor."
                ),
                "selected_media_ids": selected_ids,
                "story_timeline": moments,
                "beat_windows": beat_windows,
                "text_elements": [
                    element.model_dump(mode="json")
                    for element in revision_elements
                    if element.id not in set(approved_label_ids)
                ],
                "narration_label_text_elements": [
                    element.model_dump(mode="json") for element in projected_labels
                ],
                "music": music,
                "song_reference": song_reference,
                "editor_revision_number": normalized_revision["revision_number"],
                "editor_revision_hash": normalized_revision["state_hash"],
                "editor_sound_effects": list(normalized_revision.get("sound_effects") or []),
                "editor_media_overlays": list(normalized_revision.get("media_overlays") or []),
                "editor_visual_blocks": list(normalized_revision.get("visual_blocks") or []),
                "editor_motion_scenes": list(normalized_revision.get("motion_scenes") or []),
                "editor_custom_effects": list(normalized_revision.get("custom_effects") or []),
                "editor_caption_meta": normalized_revision.get("caption_meta"),
                "editor_audio_level": float(audio.get("level", 1.0)),
                "editor_music_removed": bool(audio.get("removed", False)),
                "editor_lane_hashes": dict(normalized_revision.get("lane_hashes") or {}),
                "editor_tombstones": list(normalized_revision.get("tombstones") or []),
                "editor_source_pool": list(normalized_revision.get("sources") or []),
                "editor_base_generation": str(normalized_revision.get("base_generation") or ""),
                "editor_renderer_version": str(normalized_revision.get("renderer_version") or ""),
                "editor_effect_schema_version": str(
                    normalized_revision.get("effect_schema_version") or ""
                ),
                "editor_approved_text_ids": approved_text_ids,
            }
        )
        # A timeline revision can split, reorder, or reuse sources. Rebuild
        # grounded clip labels against its output windows so a label never leaks
        # into a neighboring segment.
        grounded_intents = [
            intent
            for intent in (snapshot.clip_intents or [])
            if intent.op == "label"
            and intent.status == "resolved"
            and getattr(intent, "label_source", "clip") == "clip"
        ]
        if not settings.clip_intents_enabled:
            grounded_intents = []
        if grounded_intents:
            from app.tasks.generative_build import (  # noqa: PLC0415
                _CONTEXT_LABEL_COMBINED_MAX_CHARS,
                _compact_context_sport_text_elements,
                _grounded_context_labels,
            )

            clip_id_to_gcs = {ref.media_id: ref.gcs_path for ref in snapshot.media}
            labels = _grounded_context_labels(
                grounded_intents,
                clip_id_to_gcs,
                matcher_clip_metas(snapshot),
                media_refs=list(snapshot.media),
            )
            by_clip_rows: dict[str, list[dict[str, Any]]] = {}
            for row in labels:
                clip_id = row.get("clip_id") if isinstance(row, dict) else None
                if isinstance(clip_id, str) and row.get("source") == "grounded_label":
                    by_clip_rows.setdefault(clip_id, []).append(row)
            context_elements: list[dict[str, Any]] = []
            for index, moment in enumerate(moments):
                clip_id = str(moment.get("media_id") or "")
                rendered_rows: list[dict[str, Any]] = []
                seen_texts: set[str] = set()
                for row in by_clip_rows.get(clip_id, []):
                    text = row.get("sport")
                    if not isinstance(text, str) or not text or text in seen_texts:
                        continue
                    seen_texts.add(text)
                    rendered_rows.append(row)
                sport = " · ".join(str(row["sport"]) for row in rendered_rows)
                start_s = float(moment.get("output_start_s") or 0.0)
                end_s = float(moment.get("output_end_s") or 0.0)
                if not sport or end_s <= start_s:
                    continue
                if len(sport) > _CONTEXT_LABEL_COMBINED_MAX_CHARS:
                    raise ValueError(
                        "Combined context labels for clip "
                        f"{clip_id!r} exceed the "
                        f"{_CONTEXT_LABEL_COMBINED_MAX_CHARS}-character limit"
                    )
                row = rendered_rows[0]
                source_params: dict[str, Any] = {
                    "source": "context_sport",
                    "key": f"{clip_id}:{index}:{start_s:.3f}:{end_s:.3f}",
                    "identity": f"context_sport:{clip_id}:{index}:{start_s:.3f}:{end_s:.3f}",
                    "source_clip_id": clip_id,
                    "grounding": row.get("grounding"),
                    "confidence": row.get("confidence"),
                    "intent_id": row.get("intent_id") or "",
                }
                if len(rendered_rows) > 1:
                    source_params["context_label_provenance"] = [
                        {
                            "text": candidate["sport"],
                            "grounding": candidate.get("grounding"),
                            "confidence": candidate.get("confidence"),
                            "intent_id": candidate.get("intent_id") or "",
                        }
                        for candidate in rendered_rows
                    ]
                context_elements.append(
                    TextElement(
                        id=f"context-sport-{index}-{str(moment.get('moment_id') or '')}",
                        text=sport,
                        start_s=start_s,
                        end_s=end_s,
                        role="generative_sequence",
                        position="custom",
                        x_frac=0.86,
                        # 0.86 leaves enough bottom margin for the same label
                        # on both portrait and landscape canvases.
                        y_frac=0.86,
                        font_family="Inter",
                        size_class="small",
                        color="#FFFFFF",
                        highlight_color="#FFFFFF",
                        alignment="right",
                        effect="static",
                        z=8,
                        source_params=source_params,
                    ).model_dump(mode="json", exclude_none=True)
                )
            runtime_payload["context_label_text_elements"] = _compact_context_sport_text_elements(
                context_elements
            )
        runtime = GuidedStoryExecutionPlan.model_validate(runtime_payload)
        return runtime.model_dump(mode="json", exclude_none=False)
    except GuidedStoryError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise GuidedStoryError(
            "guided_story_revision_invalid", "The guided editor revision could not be compiled."
        ) from exc


def validate_guided_source_pool_generations(guided_snapshot: object) -> None:
    """Fail unless every approved source, including unused media, still exists.

    Rendering only downloads selected segments. Editor V2 deliberately exposes
    the complete approval pool, so Save and worker redelivery must also fence
    unused references against object replacement/deletion.
    """

    from app.services.edit_proposals import media_generations_match_sync

    _proposal_version, _media_digest, snapshot = validate_guided_snapshot(guided_snapshot)
    if not media_generations_match_sync(snapshot.media):
        raise GuidedStoryError(
            "guided_story_media_missing",
            "One or more approved media files changed or are no longer available.",
        )
    if snapshot.narration is not None:
        try:
            from app.storage import object_metadata

            current = object_metadata(snapshot.narration.gcs_path)
            if str(current.generation) != snapshot.narration.generation:
                raise ValueError("narration generation changed")
        except Exception as exc:  # noqa: BLE001 — exact audio identity is a render fence
            raise GuidedStoryError(
                "guided_story_media_missing",
                "The approved voiceover changed or is no longer available.",
            ) from exc


def _sha256(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _audio_codec(path: str) -> str | None:
    result = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "a:0",
            "-show_entries",
            "stream=codec_name",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            path,
        ],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    codec = result.stdout.strip().lower()
    return codec or None


def _attach_silent_aac(source: str, output: str) -> None:
    """Keep a uniform H.264/AAC contract when xfade removes moment audio."""

    result = subprocess.run(
        [
            "ffmpeg",
            "-i",
            source,
            "-f",
            "lavfi",
            "-i",
            "anullsrc=channel_layout=stereo:sample_rate=44100",
            "-map",
            "0:v:0",
            "-map",
            "1:a:0",
            "-c:v",
            "copy",
            "-c:a",
            "aac",
            "-b:a",
            "192k",
            "-shortest",
            "-movflags",
            "+faststart",
            "-y",
            output,
        ],
        capture_output=True,
        timeout=180,
        check=False,
    )
    if result.returncode != 0 or not os.path.exists(output) or os.path.getsize(output) == 0:
        raise GuidedStoryError(
            "guided_story_render_failed", "The story audio track could not be finalized."
        )


def _mix_pinned_narration(
    source: str,
    output: str,
    narration: NarrationTrack,
    *,
    tmpdir: str,
    duration_s: float,
) -> None:
    """Mux the generation-pinned creator recording over the visual timeline."""

    from app.storage import download_generation_to_file, object_metadata  # noqa: PLC0415

    try:
        metadata = object_metadata(narration.gcs_path)
        if str(metadata.generation) != narration.generation:
            raise ValueError("voiceover generation changed")
        suffix = Path(narration.gcs_path).suffix.lower() or ".m4a"
        voiceover_path = os.path.join(tmpdir, f"guided_narration{suffix}")
        download_generation_to_file(
            narration.gcs_path,
            voiceover_path,
            generation=narration.generation,
        )
    except Exception as exc:  # noqa: BLE001 — narration identity is an approval fence
        raise GuidedStoryError(
            "guided_story_media_missing",
            "The approved voiceover changed or is no longer available.",
        ) from exc

    result = subprocess.run(
        [
            "ffmpeg",
            "-i",
            source,
            "-i",
            voiceover_path,
            "-map",
            "0:v:0",
            "-map",
            "1:a:0",
            "-t",
            f"{duration_s:.6f}",
            "-c:v",
            "copy",
            "-c:a",
            "aac",
            "-b:a",
            "192k",
            "-movflags",
            "+faststart",
            "-y",
            output,
        ],
        capture_output=True,
        timeout=180,
        check=False,
    )
    if (
        result.returncode != 0
        or not os.path.exists(output)
        or os.path.getsize(output) == 0
        or abs(float(probe_video(output).duration_s) - duration_s)
        > STRICT_MIXED_MEDIA_DURATION_TOLERANCE_S
    ):
        raise GuidedStoryError(
            "guided_story_receipt_mismatch",
            "The recorded voiceover could not be applied at the approved duration.",
        )


def _enforce_strict_story_duration(source: str, output: str, *, target_s: float) -> str:
    """Clamp only a bounded positive mux/CFR overrun; never stretch content."""

    actual_s = float(probe_video(source).duration_s)
    delta_s = actual_s - float(target_s)
    if abs(delta_s) <= STRICT_MIXED_MEDIA_DURATION_TOLERANCE_S:
        return source
    if delta_s < 0 or delta_s > STRICT_MIXED_MEDIA_MAX_CFR_OVERRUN_S:
        raise GuidedStoryError(
            "guided_story_receipt_mismatch",
            "The rendered story duration no longer matches its approved plan.",
        )
    result = subprocess.run(
        [
            "ffmpeg",
            "-i",
            source,
            "-t",
            f"{target_s:.6f}",
            "-map",
            "0",
            "-c",
            "copy",
            "-movflags",
            "+faststart",
            "-y",
            output,
        ],
        capture_output=True,
        timeout=180,
        check=False,
    )
    if (
        result.returncode != 0
        or not os.path.exists(output)
        or os.path.getsize(output) == 0
        or abs(float(probe_video(output).duration_s) - float(target_s))
        > STRICT_MIXED_MEDIA_DURATION_TOLERANCE_S
    ):
        raise GuidedStoryError(
            "guided_story_receipt_mismatch",
            "The rendered story duration no longer matches its approved plan.",
        )
    return output


def _enforce_scheduled_video_frames(source: str, output: str, *, frame_count: int) -> str:
    """v8-only fence for Skia/AAC mux tail frames; never stretches video."""
    target_s = frame_count / 30
    probe = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            source,
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    try:
        actual_s = float(probe.stdout.strip())
    except ValueError as exc:
        raise GuidedStoryError(
            "guided_story_receipt_mismatch", "The scheduled video stream is unreadable."
        ) from exc
    delta_s = actual_s - target_s
    if abs(delta_s) <= 1e-6:
        return source
    if delta_s < 0 or delta_s > STRICT_MIXED_MEDIA_MAX_CFR_OVERRUN_S:
        raise GuidedStoryError(
            "guided_story_receipt_mismatch",
            "The scheduled video frames no longer match the approved plan.",
        )
    result = subprocess.run(
        [
            "ffmpeg",
            "-i",
            source,
            "-map",
            "0",
            "-frames:v",
            str(frame_count),
            "-t",
            f"{target_s:.6f}",
            "-c",
            "copy",
            "-movflags",
            "+faststart",
            "-y",
            output,
        ],
        capture_output=True,
        timeout=180,
        check=False,
    )
    if result.returncode != 0 or not os.path.exists(output):
        raise GuidedStoryError(
            "guided_story_receipt_mismatch", "The scheduled video frame fence failed."
        )
    return output


def _hold_final_frame_to_duration(source: str, output: str, *, target_s: float) -> str:
    """Extend a short visual timeline by cloning its final frame.

    Voiceover timing is immutable. A revision may exhaust its available
    footage after retiming, but it must never truncate the pinned narration.
    """

    actual_s = float(probe_video(source).duration_s)
    if actual_s >= target_s - _DURATION_MATCH_TOLERANCE_S:
        return source
    hold_s = target_s - actual_s
    result = subprocess.run(
        [
            "ffmpeg",
            "-i",
            source,
            "-vf",
            f"tpad=stop_mode=clone:stop_duration={hold_s:.6f}",
            "-t",
            f"{target_s:.6f}",
            "-an",
            "-c:v",
            "libx264",
            "-preset",
            "fast",
            "-crf",
            "18",
            "-pix_fmt",
            "yuv420p",
            "-movflags",
            "+faststart",
            "-y",
            output,
        ],
        capture_output=True,
        timeout=180,
        check=False,
    )
    if result.returncode != 0 or not os.path.exists(output) or os.path.getsize(output) == 0:
        raise GuidedStoryError(
            "guided_story_render_failed", "The short footage could not be held to narration."
        )
    return output


def _download_selected(plan: dict[str, Any], tmpdir: str) -> tuple[dict[str, str], list[dict]]:
    from PIL import Image, ImageOps  # noqa: PLC0415

    from app.storage import download_generation_to_file  # noqa: PLC0415

    first_by_id = {row["media_id"]: row for row in plan["story_timeline"]}
    try:
        import pillow_heif  # type: ignore[import-not-found]  # noqa: PLC0415

        pillow_heif.register_heif_opener()
    except Exception:  # noqa: BLE001
        pass

    def normalize_image_for_render(source: str) -> str:
        """Decode once with Pillow and hand FFmpeg an image2-safe input.

        HEIC/HEIF is decoded successfully by Pillow, but FFmpeg selects its
        dedicated HEIF demuxer for the original path. That demuxer rejects the
        image2-only ``-loop`` option used by the story motion renderer. Keep the
        downloaded source untouched for the identity receipt and create a
        separate, EXIF-corrected JPEG/PNG solely for rendering.

        Transparency never reaches FFmpeg: the fullscreen graphs drop alpha
        (exposing whatever RGB hides under it) while the card overlay keeps
        it, so the same file rendered differently per layout. Alpha images are
        matted over ``_GUIDED_IMAGE_MATTE_RGB`` on encoded 8-bit sRGB values,
        the exact rule the iPhone engine applies, and stay lossless PNG.
        """
        with Image.open(source) as opened:
            image = ImageOps.exif_transpose(opened)
            image.load()
            has_alpha = "A" in image.getbands() or "transparency" in image.info
            if has_alpha:
                render_path = f"{os.path.splitext(source)[0]}_render.png"
                rgba = image.convert("RGBA")
                matte = Image.new("RGBA", rgba.size, (*_GUIDED_IMAGE_MATTE_RGB, 255))
                flat = Image.alpha_composite(matte, rgba).convert("RGB")
                flat.save(render_path, format="PNG", optimize=False)
            else:
                render_path = f"{os.path.splitext(source)[0]}_render.jpg"
                image.convert("RGB").save(
                    render_path,
                    format="JPEG",
                    quality=95,
                    subsampling=0,
                    optimize=False,
                )
        return render_path

    def prepare(entry: tuple[int, str]) -> tuple[str, str, dict]:
        index, media_id = entry
        row = first_by_id[media_id]
        suffix = Path(row["gcs_path"]).suffix.lower() or (
            ".jpg" if row["kind"] == "image" else ".mp4"
        )
        local = os.path.join(tmpdir, f"source_{index:02d}{suffix}")
        try:
            download_generation_to_file(row["gcs_path"], local, generation=row["generation"])
        except Exception as exc:  # noqa: BLE001
            raise GuidedStoryError(
                "guided_story_media_missing", f"Approved media {media_id} could not be loaded."
            ) from exc
        # Identity receipt hashes the UNTOUCHED download (bytes as approved in
        # GCS) — compute before any normalization mutates the local file.
        size_bytes = os.path.getsize(local)
        sha256 = _sha256(local)
        if row["kind"] == "video":
            # Phone clips ship landscape pixels + a Display-Matrix rotation
            # flag. FFmpeg autorotates at decode, but probe_video classifies
            # by stored dims, so a rotated portrait clip reads as "16:9" and
            # reframe builds a crop wider than the decoded frame → instant
            # ffmpeg failure (prod jobs ca168a9f/4467f18a/d9e4833c, 2026-08-19).
            # The montage path normalizes at ingest (Stage 0.5); guided
            # stories must too. In-place; kill switch
            # ORIENTATION_NORMALIZE_ENABLED honored inside.
            from app.pipeline.orientation import normalize_orientation  # noqa: PLC0415

            try:
                normalize_orientation(local)
            except Exception as exc:  # noqa: BLE001
                raise GuidedStoryError(
                    "guided_story_render_failed",
                    f"Approved media {media_id} could not be orientation-normalized.",
                ) from exc
        try:
            if row["kind"] == "video":
                probe = probe_video(local)
                actual_kind = "video"
                duration_s = float(probe.duration_s)
            else:
                with Image.open(local) as image:
                    image.verify()
                actual_kind = "image"
                duration_s = None
        except Exception as exc:  # noqa: BLE001
            raise GuidedStoryError(
                "guided_story_media_replaced", f"Approved media {media_id} has the wrong format."
            ) from exc
        if actual_kind != row["kind"]:
            raise GuidedStoryError(
                "guided_story_media_replaced", f"Approved media {media_id} changed kind."
            )
        return (
            media_id,
            local,
            {
                "media_id": media_id,
                "gcs_path": row["gcs_path"],
                "generation": row["generation"],
                "kind": actual_kind,
                "bytes": size_bytes,
                "sha256": sha256,
                "duration_s": duration_s,
            },
        )

    selected = list(enumerate(plan["selected_media_ids"]))
    if not selected:
        raise GuidedStoryError(
            "guided_story_media_missing", "The approved story does not contain any media."
        )
    # GCS downloads and ffprobe/image verification are independent per source.
    # A small bound shortens seven-source stories without flooding the worker's
    # disk, decoder, or storage connection pool. executor.map preserves the
    # approved media order in the receipt.
    with ThreadPoolExecutor(max_workers=min(_MEDIA_PREP_MAX_WORKERS, len(selected))) as pool:
        prepared = list(pool.map(prepare, selected))
    local_by_id: dict[str, str] = {}
    # Decode images serially after the bounded parallel download/probe phase.
    # Large phone photos can occupy tens of megabytes when decoded, so this
    # avoids multiplying peak worker memory by the download concurrency.
    for media_id, local, receipt in prepared:
        if receipt["kind"] == "image":
            try:
                local = normalize_image_for_render(local)
            except Exception as exc:  # noqa: BLE001
                raise GuidedStoryError(
                    "guided_story_media_replaced",
                    f"Approved media {media_id} has the wrong format.",
                ) from exc
        local_by_id[media_id] = local
    receipts = [receipt for _media_id, _local, receipt in prepared]
    return local_by_id, receipts


def _render_image_moment(
    source: str,
    output: str,
    *,
    duration_s: float,
    layout: str,
    canvas: Canvas,
    image_motion: Literal["subtle_zoom_in"] | None = None,
    look_preset: str = "none",
    look_adjustments: dict[str, float] | None = None,
) -> None:
    from app.pipeline.reframe import _encoding_args  # noqa: PLC0415

    width, height, fps = canvas.width, canvas.height, settings.output_fps
    total_frames = max(1, int(round(duration_s * fps)))
    zoom = f"1.0+(0.06*on/{max(1, total_frames - 1)})"
    zoom_xy = "x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)'"
    # Use the same allowlisted grade compiler as video moments so every
    # bounded adjustment (warmth/contrast/grain/vignette/intensity) has image
    # and video parity. The builder emits a valid single-input graph here.
    from app.pipeline.look_presets import look_preset_filter  # noqa: PLC0415

    look_filter = look_preset_filter(
        look_preset,
        width=width,
        height=height,
        label_prefix="guided_image",
        adjustments=look_adjustments,
    )
    look_suffix = f",{look_filter}" if look_filter else ""
    if layout == "supporting_card" and image_motion == "subtle_zoom_in":
        card_width = int(width * 0.82)
        card_height = int(height * 0.72)
        vf = (
            f"[0:v]split=2[bg0][fg0];"
            f"[bg0]scale={width * 2}:{height * 2}:force_original_aspect_ratio=increase,"
            f"crop={width * 2}:{height * 2},"
            f"zoompan=z='{zoom}':{zoom_xy}:d={total_frames}:fps={fps}:s={width}x{height},"
            f"boxblur=30:2[blur];"
            f"[fg0]scale={card_width * 2}:{card_height * 2}:"
            f"force_original_aspect_ratio=decrease,"
            f"pad={card_width * 2}:{card_height * 2}:(ow-iw)/2:(oh-ih)/2:color=black,"
            f"zoompan=z='{zoom}':{zoom_xy}:d={total_frames}:fps={fps}:"
            f"s={card_width}x{card_height}[card];"
            f"[blur][card]overlay=(W-w)/2:(H-h)/2:shortest=1,"
            f"setsar=1,fps={fps}{look_suffix},format=yuv420p[v]"
        )
    elif layout == "supporting_card":
        card_width = int(width * 0.82)
        card_height = int(height * 0.72)
        vf = (
            f"[0:v]split=2[bg0][fg0];"
            f"[bg0]scale={width * 2}:{height * 2}:force_original_aspect_ratio=increase,"
            f"crop={width * 2}:{height * 2},scale={width}:{height}:flags=lanczos,"
            f"boxblur=30:2[blur];"
            f"[fg0]scale={card_width * 2}:{card_height * 2}:"
            f"force_original_aspect_ratio=decrease,"
            f"pad={card_width * 2}:{card_height * 2}:(ow-iw)/2:(oh-ih)/2:color=black,"
            f"scale={card_width}:{card_height}:flags=lanczos[card];"
            f"[blur][card]overlay=(W-w)/2:(H-h)/2:shortest=1,"
            f"setsar=1,fps={fps}{look_suffix},format=yuv420p[v]"
        )
    elif image_motion == "subtle_zoom_in":
        vf = (
            f"[0:v]scale={width * 2}:{height * 2}:force_original_aspect_ratio=increase,"
            f"crop={width * 2}:{height * 2},"
            f"zoompan=z='{zoom}':{zoom_xy}:d={total_frames}:fps={fps}:s={width}x{height},"
            f"setsar=1{look_suffix},format=yuv420p[v]"
        )
    else:
        vf = (
            f"[0:v]scale={width * 2}:{height * 2}:force_original_aspect_ratio=increase,"
            f"crop={width * 2}:{height * 2},scale={width}:{height}:flags=lanczos,"
            f"setsar=1,fps={fps}{look_suffix},format=yuv420p[v]"
        )
    cmd = [
        "ffmpeg",
        "-loop",
        "1",
        "-framerate",
        str(fps),
        "-i",
        source,
        "-f",
        "lavfi",
        "-i",
        "anullsrc=channel_layout=stereo:sample_rate=44100",
        "-t",
        f"{duration_s:.3f}",
        "-filter_complex",
        vf,
        "-map",
        "[v]",
        "-map",
        "1:a:0",
        "-shortest",
        *_encoding_args(output, preset="ultrafast", crf="14", canvas=canvas),
    ]
    result = subprocess.run(cmd, capture_output=True, timeout=300, check=False)
    if result.returncode != 0:
        raise GuidedStoryError(
            "guided_story_render_failed",
            f"A story photo could not be rendered: {result.stderr.decode(errors='replace')[-300:]}",
        )


def _render_video_moment(
    source: str,
    output: str,
    *,
    start_s: float,
    end_s: float,
    layout: str,
    canvas: Canvas = PORTRAIT,
    look_preset: str = "none",
    look_adjustments: dict[str, float] | None = None,
    exact_duration: bool = False,
    preserve_audio: bool = False,
    source_crop: dict[str, float] | None = None,
    playback_rate: float = 1.0,
) -> None:
    from app.pipeline.reframe import reframe_and_export  # noqa: PLC0415

    probe = probe_video(source)
    if end_s > float(probe.duration_s) + 0.05:
        raise GuidedStoryError(
            "guided_story_duration_impossible", "An approved video window is out of bounds."
        )
    try:
        reframe_and_export(
            source,
            start_s,
            end_s,
            probe.aspect_ratio,
            None,
            output,
            output_fit="letterbox_blur" if layout == "supporting_card" else "crop",
            color_trc=probe.color_trc,
            has_audio=preserve_audio,
            canvas=canvas,
            look_preset=look_preset,
            look_adjustments=look_adjustments,
            exact_duration=exact_duration,
            source_crop=source_crop,
            speed_factor=playback_rate,
        )
    except Exception as exc:  # noqa: BLE001
        raise GuidedStoryError(
            "guided_story_render_failed", "An approved story video could not be rendered."
        ) from exc


def _render_moments(
    plan: dict[str, Any], local_by_id: dict[str, str], tmpdir: str
) -> tuple[list[str], list[dict]]:
    outputs: list[str] = []
    receipts: list[dict] = []
    canvas = _story_canvas(str(plan["output_orientation"]))
    raw_timing = plan.get("mixed_media_timing")
    mixed_timing = (
        MixedMediaTimingProfile.model_validate(raw_timing) if raw_timing is not None else None
    )
    exact_mixed_duration = uses_quick_photo_long_video_timing(mixed_timing) or bool(
        plan.get("montage_cadence")
    )
    for index, moment in enumerate(plan["story_timeline"]):
        output = os.path.join(tmpdir, f"moment_{index:02d}.mp4")
        if moment["kind"] == "image":
            _render_image_moment(
                local_by_id[moment["media_id"]],
                output,
                duration_s=float(moment["duration_s"]),
                layout=moment["layout"],
                canvas=canvas,
                image_motion=moment.get("image_motion"),
                look_preset=moment.get("look_preset", "none"),
                look_adjustments=moment.get("look_adjustments"),
            )
        else:
            _render_video_moment(
                local_by_id[moment["media_id"]],
                output,
                start_s=float(moment["source_start_s"]),
                end_s=float(moment["source_end_s"]),
                layout=moment["layout"],
                canvas=canvas,
                look_preset=moment.get("look_preset", "none"),
                look_adjustments=moment.get("look_adjustments"),
                exact_duration=exact_mixed_duration,
                preserve_audio=bool((plan.get("montage_audio") or {}).get("preserve_source_audio")),
                source_crop=moment.get("source_crop"),
                playback_rate=float(moment.get("playback_rate") or 1.0),
            )
        probe = probe_video(output)
        if (
            probe.width != canvas.width
            or probe.height != canvas.height
            or abs(probe.duration_s - float(moment["duration_s"])) > 0.15
        ):
            raise GuidedStoryError(
                "guided_story_receipt_mismatch", "A rendered story moment failed verification."
            )
        outputs.append(output)
        receipts.append(
            {
                "moment_id": moment["moment_id"],
                "beat_id": moment["beat_id"],
                "media_id": moment["media_id"],
                "generation": moment["generation"],
                "kind": moment["kind"],
                "layout": moment["layout"],
                "image_motion": moment.get("image_motion"),
                "source_start_s": round(float(moment.get("source_start_s", 0.0)), 3),
                "source_end_s": round(float(moment.get("source_end_s", moment["duration_s"])), 3),
                **({"source_crop": moment["source_crop"]} if moment.get("source_crop") else {}),
                **(
                    {"playback_rate": float(moment["playback_rate"])}
                    if moment.get("playback_rate") is not None
                    else {}
                ),
                "output_duration_s": round(float(probe.duration_s), 3),
                "width": probe.width,
                "height": probe.height,
                "codec": probe.codec,
                "sha256": _sha256(output),
            }
        )
    return outputs, receipts


def _mux_guided_source_audio(
    assembled: str,
    plan: dict[str, Any],
    local_by_id: dict[str, str],
    output: str,
) -> str:
    """Restore approved source audio after cloud's video-only transition join."""
    if not bool((plan.get("montage_audio") or {}).get("preserve_source_audio")):
        return assembled
    inputs: list[str] = []
    branches: list[str] = []
    branch_labels: list[str] = []
    for index, moment in enumerate(plan.get("story_timeline") or []):
        source = local_by_id.get(moment.get("media_id"))
        if moment.get("kind") != "video":
            continue
        if not source:
            raise GuidedStoryError(
                "guided_story_render_failed", "An approved video source is unavailable."
            )
        try:
            probe = subprocess.run(
                [
                    "ffprobe",
                    "-v",
                    "error",
                    "-select_streams",
                    "a:0",
                    "-show_entries",
                    "stream=index",
                    "-of",
                    "csv=p=0",
                    source,
                ],
                check=True,
                capture_output=True,
                text=True,
                timeout=30,
            )
        except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
            raise GuidedStoryError(
                "guided_story_render_failed", "Approved source audio could not be inspected."
            ) from exc
        if not probe.stdout.strip():
            continue
        input_index = len(inputs) + 1
        inputs.append(source)
        branch_labels.append(f"sa{index}")
        start = float(moment.get("source_start_s") or 0.0)
        end = float(moment.get("source_end_s") or start)
        rate = float(moment.get("playback_rate") or 1.0)
        filters = [f"[{input_index}:a]atrim=start={start}:end={end}", "asetpts=PTS-STARTPTS"]
        if rate != 1.0:
            from app.pipeline.reframe import _atempo_filter  # noqa: PLC0415

            filters.append(_atempo_filter(rate))
        delay = max(0, round(float(moment.get("output_start_s") or 0.0) * 1000))
        filters.append(f"adelay={delay}:all=1")
        branches.append(",".join(filters) + f"[sa{index}]")
    if not branches:
        _attach_silent_aac(assembled, output)
        return output
    duration = float(plan["resolved_duration_s"])
    mix_inputs = "".join(f"[{label}]" for label in branch_labels)
    raw_level = plan.get("editor_audio_level")
    level = float(raw_level if raw_level is not None else 1.0)
    filter_complex = ";".join(branches) + (
        f";{mix_inputs}amix=inputs={len(branches)}:duration=longest:normalize=0,"
        f"asetpts=N/SR/TB,atrim=duration={duration},apad=whole_dur={duration},"
        f"asetpts=PTS-STARTPTS,volume={level}[aout]"
    )
    command = ["ffmpeg", "-y", "-i", assembled]
    for source in inputs:
        command.extend(["-i", source])
    command.extend(
        [
            "-filter_complex",
            filter_complex,
            "-map",
            "0:v:0",
            "-map",
            "[aout]",
            "-c:v",
            "copy",
            "-c:a",
            "aac",
            output,
        ]
    )
    try:
        subprocess.run(command, check=True, capture_output=True, text=True, timeout=120)
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        raise GuidedStoryError(
            "guided_story_render_failed", "Approved source audio could not be restored."
        ) from exc
    return output


def _verify_receipt(
    plan: dict[str, Any],
    media_receipts: list[dict],
    moment_receipts: list[dict],
    text_receipts: list[dict],
    final_path: str,
    *,
    music_applied: bool,
    narration_applied: bool = False,
    text_stage_input_path: str | None = None,
    text_stage_output_path: str | None = None,
) -> dict[str, Any]:
    expected_beats = [row["beat_id"] for row in plan["beat_windows"]]
    expected_moments = [row["moment_id"] for row in plan["story_timeline"]]
    actual_moments = [row["moment_id"] for row in moment_receipts]
    actual_beats: list[str] = []
    for row in moment_receipts:
        if row["beat_id"] not in actual_beats:
            actual_beats.append(row["beat_id"])
    expected_media = list(plan["selected_media_ids"])
    actual_media: list[str] = []
    for row in moment_receipts:
        if row["media_id"] not in actual_media:
            actual_media.append(row["media_id"])
    hidden_caption_ids = set(plan.get("editor_hidden_caption_ids") or [])
    expected_text = [
        row["id"] for row in plan["text_elements"] if row["id"] not in hidden_caption_ids
    ]
    expected_context = [row["id"] for row in plan.get("context_label_text_elements") or []]
    expected_narration_labels = [
        row["id"] for row in plan.get("narration_label_text_elements") or []
    ]
    visible_text = [row["element_id"] for row in text_receipts if row.get("visible")]
    actual_text = [element_id for element_id in visible_text if element_id in expected_text]
    actual_context = [element_id for element_id in visible_text if element_id in expected_context]
    actual_narration_labels = [
        element_id for element_id in visible_text if element_id in expected_narration_labels
    ]
    probe = probe_video(final_path)
    canvas = _story_canvas(str(plan["output_orientation"]))
    audio_codec = _audio_codec(final_path)
    raw_timing = plan.get("mixed_media_timing")
    mixed_timing = (
        MixedMediaTimingProfile.model_validate(raw_timing) if raw_timing is not None else None
    )
    duration_tolerance_s = (
        STRICT_MIXED_MEDIA_DURATION_TOLERANCE_S
        if uses_quick_photo_long_video_timing(mixed_timing)
        else max(0.2, len(moment_receipts) * 0.04)
    )
    actual_video_duration_s = float(
        getattr(probe, "video_stream_duration_s", None) or probe.duration_s
    )
    duration_ok = (
        abs(actual_video_duration_s - float(plan["resolved_duration_s"])) <= duration_tolerance_s
    )
    # A strict text burn is expected whenever either text lane has an element.
    # Keep this check at the receipt boundary as a second fence: even if a
    # renderer is mocked or an older worker silently copies the clean base,
    # that artifact can never become a verified guided result.
    text_artifact_ok = True
    if text_stage_input_path and (expected_text or expected_context or expected_narration_labels):
        try:
            text_artifact = text_stage_output_path or final_path
            text_artifact_ok = _sha256(text_artifact) != _sha256(text_stage_input_path)
        except OSError:
            text_artifact_ok = False
    verified = bool(
        expected_beats == actual_beats
        and expected_moments == actual_moments
        and expected_media == actual_media
        and set(expected_text) == set(actual_text)
        and expected_context == actual_context
        and expected_narration_labels == actual_narration_labels
        and len(media_receipts) == len(expected_media)
        and duration_ok
        and probe.width == canvas.width
        and probe.height == canvas.height
        and probe.codec == "h264"
        and audio_codec == "aac"
        and narration_applied == (plan.get("narration") is not None)
        and text_artifact_ok
        and os.path.getsize(final_path) > 0
    )
    receipt_data = {
        "schema_version": 2 if plan.get("editor_revision_number") is not None else 1,
        "verified": verified,
        "proposal_version": plan["proposal_version"],
        "media_digest": plan["media_digest"],
        "expected_beat_ids": expected_beats,
        "actual_beat_ids": actual_beats,
        "expected_moment_ids": expected_moments,
        "actual_moment_ids": actual_moments,
        "expected_media_ids": expected_media,
        "actual_media_ids": actual_media,
        "expected_text_ids": expected_text,
        "actual_text_ids": actual_text,
        "expected_context_label_ids": expected_context,
        "actual_context_label_ids": actual_context,
        "expected_narration_label_ids": expected_narration_labels,
        "actual_narration_label_ids": actual_narration_labels,
        "media_count": len(actual_media),
        "image_count": len({r["media_id"] for r in moment_receipts if r["kind"] == "image"}),
        "video_count": len({r["media_id"] for r in moment_receipts if r["kind"] == "video"}),
        "expected_duration_s": plan["resolved_duration_s"],
        "actual_duration_s": round(actual_video_duration_s, 3),
        "output_orientation": plan["output_orientation"],
        "output_orientation_reason": plan["output_orientation_reason"],
        "music_applied": music_applied,
        "music": plan.get("music") if music_applied else None,
        "song_reference": plan.get("song_reference"),
        "music_window_applied": (
            {
                "start_s": float(plan["music"].get("start_s") or 0.0),
                "end_s": float(plan["music"].get("end_s") or plan["resolved_duration_s"]),
                "duration_s": max(
                    0.0,
                    float(plan["music"].get("end_s") or plan["resolved_duration_s"])
                    - float(plan["music"].get("start_s") or 0.0),
                ),
            }
            if music_applied and plan.get("music")
            else None
        ),
        "narration": plan.get("narration") if narration_applied else None,
        "narration_applied": narration_applied,
        "narration_label_receipt": plan.get("narration_label_receipt"),
        "output": {
            "width": probe.width,
            "height": probe.height,
            "video_codec": probe.codec,
            "audio_codec": audio_codec,
            "sha256": _sha256(final_path),
        },
        "media_stages": media_receipts,
        "moment_stages": moment_receipts,
        "text_stages": text_receipts,
        "source_audio_options": list(plan.get("source_audio_options") or []),
        "source_audio_preserved": (
            bool((plan.get("montage_audio") or {}).get("preserve_source_audio"))
            if plan.get("compiler_version", 0) >= 6
            else None
        ),
    }
    if plan.get("editor_revision_number") is not None:
        receipt_data["approved_text_ids"] = list(
            plan.get("editor_approved_text_ids") or expected_text
        )
        receipt_data["revision_number"] = plan["editor_revision_number"]
        receipt_data["revision_hash"] = plan.get("editor_revision_hash")
        receipt_data["lane_hashes"] = dict(plan.get("editor_lane_hashes") or {})
        receipt_data["tombstones"] = list(plan.get("editor_tombstones") or [])
        receipt_data["source_pool"] = list(plan.get("editor_source_pool") or [])
        receipt_data["segment_order"] = [row["moment_id"] for row in plan["story_timeline"]]
        receipt_data["music_removed"] = bool(plan.get("editor_music_removed", False))
        receipt_data["base_render_generation"] = plan.get("editor_base_generation")
        receipt_data["renderer_version"] = plan.get("editor_renderer_version")
        receipt_data["effect_schema_version"] = plan.get("editor_effect_schema_version")
    if not verified:
        if set(expected_text) != set(actual_text):
            raise GuidedStoryError(
                "guided_story_text_missing", "One or more approved text moments disappeared."
            )
        if expected_context != actual_context:
            raise GuidedStoryError(
                "guided_story_context_label_missing",
                "One or more approved context labels disappeared.",
            )
        if expected_narration_labels != actual_narration_labels:
            raise GuidedStoryError(
                "guided_story_narration_label_missing",
                "One or more approved participant labels disappeared.",
            )
        raise GuidedStoryError(
            "guided_story_receipt_mismatch", "The finished video did not match the approved edit."
        )
    try:
        return GuidedStoryRenderReceipt.model_validate(receipt_data).model_dump(mode="json")
    except Exception as exc:  # noqa: BLE001
        raise GuidedStoryError(
            "guided_story_receipt_mismatch", "The finished video receipt was incomplete."
        ) from exc


def verify_guided_text_reburn(
    existing_receipt: object,
    text_elements: list[dict[str, Any]],
    text_evidence: list[dict[str, Any]],
    final_path: str,
    clean_base_path: str,
) -> dict[str, Any]:
    """Verify a text-only guided reburn before its bytes are uploaded."""

    try:
        previous = GuidedStoryRenderReceipt.model_validate(existing_receipt)
        elements = [TextElement.model_validate(row) for row in text_elements]
    except Exception as exc:  # noqa: BLE001
        raise GuidedStoryError(
            "guided_story_receipt_mismatch", "The guided story receipt is incomplete."
        ) from exc
    duration_s = float(previous.expected_duration_s)
    for element in elements:
        if (
            element.start_s < 0
            or element.end_s <= element.start_s
            or element.start_s >= duration_s - _FRAME_S
            or element.end_s > duration_s + _FRAME_S
        ):
            raise GuidedStoryError(
                "guided_story_text_missing",
                "One or more text moments fall outside the finished story.",
            )
    expected_ids = [element.id for element in elements]
    actual_ids = [str(row.get("element_id")) for row in text_evidence if row.get("visible")]
    probe = probe_video(final_path)
    audio_codec = _audio_codec(final_path)
    output_hash = _sha256(final_path)
    canvas = _story_canvas(previous.output_orientation)
    actual_video_duration_s = float(
        getattr(probe, "video_stream_duration_s", None) or probe.duration_s
    )
    verified = bool(
        expected_ids == actual_ids
        and abs(actual_video_duration_s - duration_s) <= 0.2
        and probe.width == canvas.width
        and probe.height == canvas.height
        and probe.codec == "h264"
        and audio_codec == "aac"
        and (not expected_ids or output_hash != _sha256(clean_base_path))
        and os.path.getsize(final_path) > 0
    )
    if not verified:
        if set(expected_ids) != set(actual_ids):
            raise GuidedStoryError(
                "guided_story_text_missing", "One or more edited text moments disappeared."
            )
        raise GuidedStoryError(
            "guided_story_receipt_mismatch", "The edited video no longer matches the story."
        )
    updated = previous.model_dump(mode="json")
    updated.update(
        {
            "verified": True,
            "expected_text_ids": expected_ids,
            "actual_text_ids": actual_ids,
            "approved_text_ids": previous.approved_text_ids or previous.expected_text_ids,
            "text_stages": text_evidence,
            "text_edited_after_approval": True,
            "actual_duration_s": round(actual_video_duration_s, 3),
            "output": {
                "width": probe.width,
                "height": probe.height,
                "video_codec": probe.codec,
                "audio_codec": audio_codec,
                "sha256": output_hash,
            },
        }
    )
    return GuidedStoryRenderReceipt.model_validate(updated).model_dump(mode="json")


def validate_ready_result(
    plan: object,
    result: object,
    *,
    job_id: str,
    verify_storage: bool,
) -> dict[str, Any]:
    """Reject a supposedly ready result unless it still proves the strict plan."""

    try:
        typed_plan = GuidedStoryExecutionPlan.model_validate(plan)
        if not isinstance(result, dict):
            raise TypeError("result must be an object")
        receipt = GuidedStoryRenderReceipt.model_validate(result.get("render_receipt"))
    except Exception as exc:  # noqa: BLE001
        raise GuidedStoryError(
            "guided_story_receipt_mismatch", "The saved guided story receipt is incomplete."
        ) from exc

    expected_beats = [row.beat_id for row in typed_plan.beat_windows]
    expected_moments = [row.moment_id for row in typed_plan.story_timeline]
    hidden_caption_ids: set[str] = set()
    if (typed_plan.editor_caption_meta or {}).get("enabled") is False:
        hidden_caption_ids = {
            row.id
            for row in typed_plan.text_elements
            if (row.source_params or {}).get("source") == CAPTION_CUE_SOURCE
        }
    expected_text = [row.id for row in typed_plan.text_elements if row.id not in hidden_caption_ids]
    expected_context = [row.id for row in typed_plan.context_label_text_elements]
    expected_narration_labels = [row.id for row in typed_plan.narration_label_text_elements]
    current_text = [str(row.get("id")) for row in list(result.get("text_elements") or [])]
    # Hidden caption pixels are intentionally absent from the receipt, but the
    # canonical revision still retains every editable identity for a later
    # re-enable/save round trip.
    expected_editable_text = [
        *[row.id for row in typed_plan.text_elements],
        *expected_narration_labels,
    ]
    approved_text = receipt.approved_text_ids or receipt.expected_text_ids
    timeline_by_media: dict[str, GuidedStoryMoment] = {}
    for moment in typed_plan.story_timeline:
        timeline_by_media.setdefault(moment.media_id, moment)
    expected_media_stages = [
        {
            "media_id": media_id,
            "gcs_path": timeline_by_media[media_id].gcs_path,
            "generation": timeline_by_media[media_id].generation,
            "kind": timeline_by_media[media_id].kind,
        }
        for media_id in typed_plan.selected_media_ids
    ]
    staged_media = [
        {
            "media_id": str(row.get("media_id")),
            "gcs_path": str(row.get("gcs_path")),
            "generation": str(row.get("generation")),
            "kind": str(row.get("kind")),
        }
        for row in receipt.media_stages
    ]
    expected_moment_stages = [
        {
            "moment_id": moment.moment_id,
            "beat_id": moment.beat_id,
            "media_id": moment.media_id,
            "generation": moment.generation,
            "kind": moment.kind,
            "layout": moment.layout,
            "image_motion": moment.image_motion,
        }
        for moment in typed_plan.story_timeline
    ]
    staged_moments = [
        {
            "moment_id": str(row.get("moment_id")),
            "beat_id": str(row.get("beat_id")),
            "media_id": str(row.get("media_id")),
            "generation": str(row.get("generation")),
            "kind": str(row.get("kind")),
            "layout": str(row.get("layout")),
            "image_motion": row.get("image_motion"),
        }
        for row in receipt.moment_stages
    ]
    cadence_receipt_ok = True
    if typed_plan.montage_cadence is not None:
        expected_cadence_stages = [
            {
                "moment_id": moment.moment_id,
                "media_id": moment.media_id,
                "source_start_s": round(moment.source_start_s, 3),
                "source_end_s": round(moment.source_end_s, 3),
                "output_duration_s": round(moment.duration_s, 3),
            }
            for moment in typed_plan.story_timeline
        ]
        staged_cadence_stages = [
            {
                "moment_id": str(row.get("moment_id")),
                "media_id": str(row.get("media_id")),
                "source_start_s": round(float(row.get("source_start_s", -1)), 3),
                "source_end_s": round(float(row.get("source_end_s", -1)), 3),
                "output_duration_s": round(float(row.get("output_duration_s", -1)), 3),
            }
            for row in receipt.moment_stages
        ]
        cadence_receipt_ok = staged_cadence_stages == expected_cadence_stages
    staged_visible_text = [
        str(row.get("element_id")) for row in receipt.text_stages if row.get("visible")
    ]
    staged_text = [element_id for element_id in staged_visible_text if element_id in expected_text]
    staged_context = [
        element_id for element_id in staged_visible_text if element_id in expected_context
    ]
    staged_narration_labels = [
        element_id for element_id in staged_visible_text if element_id in expected_narration_labels
    ]
    exact_story = [row.model_dump(mode="json") for row in typed_plan.story_timeline]
    prefix = f"generative-jobs/{job_id}/"
    base_path = str(result.get("base_video_path") or "")
    video_path = str(result.get("video_path") or "")
    revision_contract_ok = True
    if typed_plan.editor_revision_number is not None:
        revision_contract_ok = bool(
            receipt.schema_version == 2
            and receipt.revision_number == typed_plan.editor_revision_number
            and receipt.revision_hash == typed_plan.editor_revision_hash
            and receipt.lane_hashes == typed_plan.editor_lane_hashes
            and receipt.tombstones == typed_plan.editor_tombstones
            and receipt.source_pool == typed_plan.editor_source_pool
            and receipt.segment_order == [moment.moment_id for moment in typed_plan.story_timeline]
            and receipt.music_removed == typed_plan.editor_music_removed
            and receipt.base_render_generation == typed_plan.editor_base_generation
            and receipt.renderer_version == typed_plan.editor_renderer_version
            and receipt.effect_schema_version == typed_plan.editor_effect_schema_version
        )
    structurally_valid = bool(
        result.get("variant_id") == VARIANT_ID
        and result.get("resolved_archetype") == VARIANT_ID
        and result.get("render_status") == "ready"
        and result.get("ok") is True
        and result.get("proposal_version") == typed_plan.proposal_version
        and result.get("media_digest") == typed_plan.media_digest
        and result.get("story_timeline") == exact_story
        and current_text == expected_editable_text
        and approved_text == (typed_plan.editor_approved_text_ids or expected_text)
        and receipt.proposal_version == typed_plan.proposal_version
        and receipt.media_digest == typed_plan.media_digest
        and receipt.expected_beat_ids == expected_beats
        and receipt.expected_moment_ids == expected_moments
        and receipt.expected_media_ids == typed_plan.selected_media_ids
        and receipt.music == typed_plan.music
        and receipt.song_reference == typed_plan.song_reference
        and all(
            result.get(key) == value
            for key, value in song_reference_variant_fields(
                typed_plan.model_dump(mode="json")
            ).items()
        )
        and (
            typed_plan.compiler_version < 6
            or receipt.source_audio_preserved
            == bool((typed_plan.montage_audio or {}).get("preserve_source_audio"))
        )
        and (typed_plan.compiler_version < 6 or receipt.music_applied is False)
        and staged_media == expected_media_stages
        and staged_moments == expected_moment_stages
        and cadence_receipt_ok
        and staged_text == receipt.actual_text_ids
        and receipt.expected_context_label_ids == expected_context
        and receipt.actual_context_label_ids == staged_context
        and receipt.expected_narration_label_ids == expected_narration_labels
        and receipt.actual_narration_label_ids == staged_narration_labels
        and receipt.narration == typed_plan.narration
        and receipt.narration_applied == (typed_plan.narration is not None)
        and receipt.narration_label_receipt == typed_plan.narration_label_receipt
        and receipt.expected_duration_s == typed_plan.resolved_duration_s
        and abs(receipt.actual_duration_s - typed_plan.resolved_duration_s)
        <= (
            STRICT_MIXED_MEDIA_DURATION_TOLERANCE_S
            if uses_quick_photo_long_video_timing(typed_plan.mixed_media_timing)
            else 0.2
        )
        and receipt.output_orientation == typed_plan.output_orientation
        and receipt.output.width == _story_canvas(typed_plan.output_orientation).width
        and receipt.output.height == _story_canvas(typed_plan.output_orientation).height
        and base_path.startswith(prefix)
        and video_path.startswith(prefix)
        and base_path.endswith(".mp4")
        and video_path.endswith(".mp4")
        and base_path != video_path
        and receipt.base_storage is not None
        and receipt.output_storage is not None
        and receipt.base_storage.path == base_path
        and receipt.output_storage.path == video_path
        and revision_contract_ok
    )
    if not structurally_valid:
        raise GuidedStoryError(
            "guided_story_receipt_mismatch", "The saved guided story no longer matches its plan."
        )
    if verify_storage:
        from app.storage import object_metadata  # noqa: PLC0415

        try:
            base_metadata = object_metadata(base_path)
            output_metadata = object_metadata(video_path)
        except Exception as exc:  # noqa: BLE001
            raise GuidedStoryError(
                "guided_story_receipt_mismatch",
                "One or more verified guided story files are no longer available.",
            ) from exc
        for expected, current in (
            (receipt.base_storage, base_metadata),
            (receipt.output_storage, output_metadata),
        ):
            if (
                expected is None
                or expected.generation != current.generation
                or expected.size != current.size
                or (expected.md5_hash and expected.md5_hash != current.md5_hash)
            ):
                raise GuidedStoryError(
                    "guided_story_receipt_mismatch",
                    "A verified guided story file was replaced after rendering.",
                )
    return dict(result)


def _upload_verified_outputs(
    clean_base: str,
    final_path: str,
    *,
    base_key: str,
    output_key: str,
) -> tuple[str, dict[str, Any], dict[str, Any]]:
    """Upload only verified bytes and compensate any partial publication."""

    from app.storage import (  # noqa: PLC0415
        delete_object_best_effort,
        object_metadata,
        upload_public_read,
    )

    try:
        upload_public_read(clean_base, base_key)
        output_url = upload_public_read(final_path, output_key)
        base_metadata = object_metadata(base_key)
        output_metadata = object_metadata(output_key)
        return (
            output_url,
            {
                "path": base_metadata.path,
                "generation": base_metadata.generation,
                "size": base_metadata.size,
                "md5_hash": base_metadata.md5_hash,
            },
            {
                "path": output_metadata.path,
                "generation": output_metadata.generation,
                "size": output_metadata.size,
                "md5_hash": output_metadata.md5_hash,
            },
        )
    except Exception:  # noqa: BLE001
        # An upload provider can fail after creating the destination object.
        # Delete both exact task-owned keys even when the call raised before it
        # returned, so an uncommitted base/final pair cannot leak indefinitely.
        delete_object_best_effort(base_key)
        delete_object_best_effort(output_key)
        raise


def _build_montage_audio_options(
    plan: dict[str, Any],
    local_by_id: dict[str, str],
    assembled: str,
    *,
    job_id: str,
    tmpdir: str,
    attempt_id: str | None,
) -> list[dict[str, Any]]:
    """Prepare reusable audio-only choices for an authored montage timeline."""

    audio_plan = plan.get("montage_audio")
    if not isinstance(audio_plan, dict) or not audio_plan.get("preview_source_beds"):
        return []
    source_ids = list(audio_plan.get("source_media_ids") or [])
    if not source_ids:
        source_ids = list(
            dict.fromkeys(
                moment["media_id"]
                for moment in plan.get("story_timeline", [])
                if moment.get("kind") == "video"
            )
        )
    if not source_ids:
        return []
    from app.storage import upload_public_read  # noqa: PLC0415

    duration_s = float(plan["resolved_duration_s"])
    attempt_suffix = hashlib.sha256(str(attempt_id or "preview").encode()).hexdigest()[:16]
    options: list[dict[str, Any]] = []
    interleaved = os.path.join(tmpdir, "montage_audio_interleaved.m4a")
    command = [
        "ffmpeg",
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        assembled,
        "-map",
        "0:a:0",
        "-vn",
        "-t",
        f"{duration_s:.3f}",
        "-c:a",
        "copy",
        interleaved,
    ]
    result = subprocess.run(command, capture_output=True, timeout=120, check=False)
    if result.returncode == 0 and os.path.exists(interleaved):
        options.append({"mix": "interleaved", "local_path": interleaved, "label": "Interleaved"})

    for index, source_id in enumerate(source_ids):
        source = local_by_id.get(source_id)
        if not source:
            continue
        output = os.path.join(tmpdir, f"montage_audio_source_{index}.m4a")
        command = [
            "ffmpeg",
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-stream_loop",
            "-1",
            "-i",
            source,
            "-map",
            "0:a:0",
            "-vn",
            "-t",
            f"{duration_s:.3f}",
            "-ar",
            "44100",
            "-ac",
            "2",
            "-c:a",
            "aac",
            "-b:a",
            "192k",
            output,
        ]
        result = subprocess.run(command, capture_output=True, timeout=120, check=False)
        # Keep source_a/source_b for the existing two-source API contract;
        # additional sources use stable positional IDs without any editorial
        # assumption about their meaning.
        mix = f"source_{chr(ord('a') + index)}" if index < 26 else f"source_{index + 1}"
        if result.returncode == 0 and os.path.exists(output):
            label = f"Source {index + 1}"
            options.append(
                {
                    "mix": mix,
                    "local_path": output,
                    "label": label,
                    "source_media_id": source_id,
                }
            )

    published: list[dict[str, Any]] = []
    for option in options:
        mix = str(option["mix"])
        path = str(option["local_path"])
        object_path = f"generative-jobs/{job_id}/montage_audio_{mix}_{attempt_suffix}.m4a"
        url = upload_public_read(path, object_path, content_type="audio/mp4")
        published.append(
            {
                "mix": mix,
                "audio_path": object_path,
                "audio_url": url,
                "duration_s": duration_s,
                **({"label": option["label"]} if option.get("label") else {}),
                **(
                    {"source_media_id": option["source_media_id"]}
                    if option.get("source_media_id")
                    else {}
                ),
            }
        )
    return published


def _mix_pinned_music(
    assembled: str,
    clean_base: str,
    tmpdir: str,
    music: dict[str, Any],
    track: Any,
    *,
    output_duration_s: float | None = None,
    strict_duration: bool = False,
) -> None:
    """Mix only the immutable track object captured in the execution plan."""

    if (
        track is None
        or str(track.id) != str(music.get("track_id"))
        or str(track.audio_gcs_path) != str(music.get("audio_gcs_path"))
        or str(track.generation) != str(music.get("generation"))
    ):
        raise GuidedStoryError(
            "guided_story_music_missing", "The approved story music is no longer available."
        )
    from app.tasks.template_orchestrate import _mix_template_audio  # noqa: PLC0415

    window_duration_s = max(
        0.0,
        float(music.get("end_s") or 0.0) - float(music.get("start_s") or 0.0),
    )
    if window_duration_s <= 0 and output_duration_s is not None:
        window_duration_s = max(0.0, float(output_duration_s))
    if window_duration_s <= 0:
        raise GuidedStoryError(
            "guided_story_music_missing", "The approved story music window is invalid."
        )
    try:
        _mix_template_audio(
            assembled,
            str(music["audio_gcs_path"]),
            clean_base,
            tmpdir,
            audio_start_offset_s=float(music.get("start_s") or 0.0),
            validated_window_duration_s=window_duration_s,
            audio_window_duration_s=window_duration_s,
            require_audio=True,
            audio_generation=str(music["generation"]),
            force_video_duration=True,
            target_video_duration_s=output_duration_s if strict_duration else None,
            audio_gain=float(music.get("level", 1.0)),
        )
    except Exception as exc:  # noqa: BLE001
        raise GuidedStoryError(
            "guided_story_music_missing",
            "The exact approved music file is no longer available.",
        ) from exc


def _compose_guided_pretext_lanes(
    base_local: str,
    plan: dict[str, Any],
    *,
    job_id: str,
    attempt_id: str | None,
    tmpdir: str,
) -> str:
    """Compose visual/motion/media lanes below guided text, in strict order.

    Existing lane compositors are GCS-oriented. This adapter gives the strict
    renderer a durable temporary hand-off between them, then downloads the
    composed clean base for the Skia text pass. It never invokes montage.
    """
    from app import storage  # noqa: PLC0415

    lanes = (
        ("visual_blocks", plan.get("editor_visual_blocks") or []),
        ("motion_scenes", plan.get("editor_motion_scenes") or []),
        ("media_overlays", plan.get("editor_media_overlays") or []),
    )
    if not any(values for _, values in lanes):
        return base_local
    key_root = f"generative-jobs/{job_id}/guided-lanes/{attempt_id or 'preview'}"
    current_key = f"{key_root}/base.mp4"
    created = [current_key]
    try:
        storage.upload_local_file(base_local, current_key, "video/mp4")
        if plan.get("editor_visual_blocks"):
            from app.agents._schemas.visual_block import coerce_visual_blocks  # noqa: PLC0415
            from app.pipeline.visual_blocks import apply_visual_blocks  # noqa: PLC0415

            blocks = coerce_visual_blocks(plan["editor_visual_blocks"])
            next_key = f"{key_root}/visual.mp4"
            apply_visual_blocks(
                base_gcs_path=current_key,
                blocks=blocks,
                output_gcs_path=next_key,
                job_id=job_id,
            )
            current_key = next_key
            created.append(next_key)
        if plan.get("editor_motion_scenes"):
            from app.pipeline.motion_scene import apply_motion_scenes  # noqa: PLC0415

            next_key = f"{key_root}/motion.mp4"
            apply_motion_scenes(
                base_gcs_path=current_key,
                instances=list(plan["editor_motion_scenes"]),
                output_gcs_path=next_key,
                job_id=job_id,
            )
            current_key = next_key
            created.append(next_key)
        if plan.get("editor_media_overlays"):
            from app.agents._schemas.media_overlay import coerce_media_overlays  # noqa: PLC0415
            from app.pipeline.media_overlay import apply_media_overlays  # noqa: PLC0415

            cards = coerce_media_overlays(plan["editor_media_overlays"])
            next_key = f"{key_root}/media.mp4"
            apply_media_overlays(
                current_key,
                cards,
                next_key,
                job_id=job_id,
                canvas=_story_canvas(plan.get("output_orientation")),
            )
            current_key = next_key
            created.append(next_key)
        composed_local = os.path.join(tmpdir, "guided_story_lanes_base.mp4")
        storage.download_to_file(current_key, composed_local)
        return composed_local
    finally:
        for key in created:
            storage.delete_object_best_effort(key)


def _compose_guided_sfx(
    text_path: str,
    plan: dict[str, Any],
    *,
    job_id: str,
    attempt_id: str | None,
    tmpdir: str,
) -> str:
    """Apply SFX after text, preserving the strict z/audio order."""
    effects = plan.get("editor_sound_effects") or []
    if not effects:
        return text_path
    from app import storage  # noqa: PLC0415
    from app.agents._schemas.sound_effect import coerce_sound_effects  # noqa: PLC0415
    from app.pipeline.sound_effects import apply_sound_effects  # noqa: PLC0415

    root = f"generative-jobs/{job_id}/guided-lanes/{attempt_id or 'preview'}"
    base_key = f"{root}/text.mp4"
    output_key = f"{root}/sfx.mp4"
    try:
        storage.upload_local_file(text_path, base_key, "video/mp4")
        apply_sound_effects(
            base_key,
            coerce_sound_effects(effects),
            output_key,
            job_id=job_id,
        )
        output_local = os.path.join(tmpdir, "guided_story_final_sfx.mp4")
        storage.download_to_file(output_key, output_local)
        return output_local
    finally:
        storage.delete_object_best_effort(base_key)
        storage.delete_object_best_effort(output_key)


def _resolved_transition_boundaries(plan: dict[str, Any]) -> list[str]:
    """Materialize renderer boundaries without turning `none` into a fade."""

    transition = plan["transition_policy"]
    return [
        "cut" if value == "none" else value
        for value in (
            str(row.get("transition_after") or transition["type"])
            for row in plan["story_timeline"][:-1]
        )
    ]


def _tag_guided_text_overlays(
    compiled: list[dict[str, Any]], source_elements: list[TextElement]
) -> list[dict[str, Any]]:
    """Attach strict receipt IDs and isolate pinned narration captions.

    The Skia renderer coalesces ``generative_sequence`` overlays for speed.
    Captions are individually receipt-backed, so they must use an ordinary
    renderer role just like the separately-rendered narration-label lane.
    """

    by_timing = {
        (element.text, element.start_s, element.end_s): element.id for element in source_elements
    }
    by_id = {element.id: element for element in source_elements}
    for index, overlay in enumerate(compiled):
        # Static context and caption elements compile one-to-one. Positional
        # tagging survives renderer timestamp normalization and avoids matching
        # on mutable display text.
        if len(compiled) == len(source_elements) and index < len(source_elements):
            element = source_elements[index]
            overlay["element_id"] = element.id
        else:
            key = (
                str(overlay.get("text") or ""),
                float(overlay.get("start_s") or 0.0),
                float(overlay.get("end_s") or 0.0),
            )
            overlay["element_id"] = by_timing.get(key)
            element = by_id.get(overlay.get("element_id"))
        source_params = element.source_params if element is not None else None
        if (source_params or {}).get("source") == CAPTION_CUE_SOURCE:
            overlay["role"] = "generative_narration_caption"
    return compiled


def _apply_guided_caption_meta(
    elements: list[TextElement], meta: dict[str, Any] | None
) -> tuple[list[TextElement], set[str]]:
    """Project caption metadata without mutating pinned cue timing or identity."""

    if not meta:
        return elements, set()
    hidden: set[str] = set()
    revised: list[TextElement] = []
    for element in elements:
        if (element.source_params or {}).get("source") != CAPTION_CUE_SOURCE:
            revised.append(element)
            continue
        if meta.get("enabled") is False:
            hidden.add(element.id)
            revised.append(element)
            continue
        patch: dict[str, Any] = {}
        appearance = meta.get("appearance") if isinstance(meta.get("appearance"), dict) else {}
        if appearance.get("alignment") in {"left", "center", "right"}:
            patch["alignment"] = appearance["alignment"]
        for key in ("stroke_color", "shadow_color", "shadow_opacity"):
            if appearance.get(key) is not None:
                patch[key] = appearance[key]
        if meta.get("y_frac") is not None:
            patch.update({"position": "custom", "y_frac": float(meta["y_frac"])})
        if meta.get("font_set") and meta.get("font"):
            patch["font_family"] = meta["font"]
        for key in ("size_px", "color", "highlight_color", "stroke_width", "shadow_enabled"):
            if meta.get(key) is not None:
                patch[key] = meta[key]
        if meta.get("style") == "word":
            patch["effect"] = "pop-in"
        elif meta.get("style") == "sentence":
            patch["effect"] = "static"
        revised.append(element.model_copy(update=patch) if patch else element)
    return revised, hidden


# Guided-story text ids eligible for face-aware repositioning (KRI-116): the
# opening title, chapter/beat lines, and the closing card — the confirmed-copy
# elements that hold a fixed position across the whole shot. Narration
# captions and context/narration labels already move per-cue and are excluded.
_GUIDED_FACE_PLACEMENT_FIXED_IDS = frozenset({"guided-title", "guided-closing-title"})
_GUIDED_FACE_PLACEMENT_PREFIX = "guided-thought-"
_GUIDED_FACE_PLACEMENT_ANCHORS = 6
_GUIDED_FACE_PLACEMENT_TIMEOUT_BASE_S = 2.5
_GUIDED_FACE_PLACEMENT_TIMEOUT_PER_ANCHOR_S = 0.35


def _is_guided_face_placement_eligible(element_id: str) -> bool:
    return element_id in _GUIDED_FACE_PLACEMENT_FIXED_IDS or element_id.startswith(
        _GUIDED_FACE_PLACEMENT_PREFIX
    )


def _guided_text_y_candidates(default_y: float) -> tuple[float, ...]:
    """Ladder of bands to try when the authored default collides with a face.

    ``default_y`` is always tried first (unmoved is the common case). The rest
    spans top / upper-third / center / lower-third / bottom so a title
    (defaults near the top) and a chapter line (defaults near the bottom) both
    have somewhere else to go; ``choose_guided_text_y_frac`` still rejects any
    band that fails the TikTok/Reels chrome safe-area check.
    """

    ladder = [default_y, 0.16, 0.34, 0.5, 0.66, 0.8]
    kept: list[float] = []
    for value in ladder:
        if all(abs(value - existing) > 0.01 for existing in kept):
            kept.append(value)
    return tuple(kept)


def _evenly_spaced_window_anchors(start_s: float, end_s: float, n: int) -> list[float]:
    """``n`` sample times centered in each 1/n bucket of ``[start_s, end_s]``."""

    span = end_s - start_s
    if span <= 0 or n <= 0:
        return [round(start_s, 3)]
    return [round(start_s + span * (index + 0.5) / n, 3) for index in range(n)]


def _apply_guided_text_face_placement(
    base_path: str,
    text_element_rows: list[dict[str, Any]],
    *,
    job_id: str,
    canvas: Canvas,
) -> list[dict[str, Any]]:
    """Move the guided-story title/chapter/closing text off any face in its shot.

    Reuses the same face-sampling + candidate-ladder primitives Smart Captions
    already uses (``sample_face_regions`` / ``choose_guided_text_y_frac`` in
    ``render_geometry.py``), resolved once here at compile time so every
    renderer (Skia, phone/native-editor recipe) burns the same stored
    ``y_frac`` — see KRI-116's renderer-parity requirement. Fail-open by
    design: any sampling/measurement error leaves that element's authored
    position untouched, only logging a trace event so the collision (or the
    failure to check) stays visible in ``/admin/jobs/<id>/debug``.
    """

    from app.pipeline.generative_overlays import (  # noqa: PLC0415
        build_overlays_from_text_elements,
    )
    from app.pipeline.render_geometry import (  # noqa: PLC0415
        NormalizedBox,
        choose_guided_text_y_frac,
        sample_face_regions,
    )
    from app.pipeline.text_overlay_skia import measure_text_overlay_box  # noqa: PLC0415
    from app.services.pipeline_trace import record_pipeline_event  # noqa: PLC0415

    eligible = [
        (index, row)
        for index, row in enumerate(text_element_rows)
        if row.get("position") == "custom"
        and row.get("y_frac") is not None
        and _is_guided_face_placement_eligible(str(row.get("id", "")))
    ]
    if not eligible:
        return text_element_rows

    try:
        duration_s = float(probe_video(base_path).duration_s)
    except Exception as exc:  # noqa: BLE001 - fail open, nothing to sample against
        log.warning("guided_text_face_placement_probe_failed", job_id=job_id, error=str(exc))
        return text_element_rows
    if duration_s <= 0:
        return text_element_rows

    updated = list(text_element_rows)
    for index, row in eligible:
        element_id = str(row.get("id"))
        default_y = float(row["y_frac"])
        start_s = max(0.0, min(duration_s, float(row.get("start_s", 0.0))))
        end_s = max(start_s + _FRAME_S, min(duration_s, float(row.get("end_s", duration_s))))
        anchors = _evenly_spaced_window_anchors(start_s, end_s, _GUIDED_FACE_PLACEMENT_ANCHORS)
        try:
            face_regions, face_receipt = sample_face_regions(
                base_path,
                anchors,
                max_samples=max(len(anchors), 1),
                timeout_s=(
                    _GUIDED_FACE_PLACEMENT_TIMEOUT_BASE_S
                    + _GUIDED_FACE_PLACEMENT_TIMEOUT_PER_ANCHOR_S * len(anchors)
                ),
                count_decoded=True,
            )
            [overlay] = build_overlays_from_text_elements(
                [TextElement.model_validate(row)],
                video_duration_s=duration_s,
                independent_box_alignment=True,
            )
            measured = measure_text_overlay_box(overlay, render_canvas=canvas)
            probe_box = NormalizedBox(
                measured["left"], measured["top"], measured["right"], measured["bottom"]
            )
            chosen_y, receipt = choose_guided_text_y_frac(
                face_regions,
                face_receipt,
                probe_box,
                [],
                _guided_text_y_candidates(default_y),
            )
        except Exception as exc:  # noqa: BLE001 - fail open to the authored default
            log.warning(
                "guided_text_face_placement_failed",
                job_id=job_id,
                element_id=element_id,
                error=str(exc),
            )
            try:
                record_pipeline_event(
                    "overlay",
                    "guided_text_placement_error",
                    {
                        "element_id": element_id,
                        "default_y_frac": default_y,
                        "error": str(exc),
                    },
                )
            except Exception as trace_exc:  # noqa: BLE001 - instrumentation must never break render
                log.warning("guided_text_placement_event_emit_failed", error=str(trace_exc))
            continue

        try:
            record_pipeline_event(
                "overlay",
                "guided_text_placement_chosen",
                {
                    "element_id": element_id,
                    "default_y_frac": default_y,
                    "chosen_y_frac": chosen_y,
                    "status": receipt.get("status"),
                    "reason": receipt.get("reason"),
                    "coverage": receipt.get("coverage"),
                    "face_presence": receipt.get("face_presence"),
                    "decoded": receipt.get("decoded"),
                },
            )
        except Exception as trace_exc:  # noqa: BLE001 - instrumentation must never break render
            log.warning("guided_text_placement_event_emit_failed", error=str(trace_exc))
        if chosen_y != default_y:
            new_row = dict(row)
            new_row["y_frac"] = chosen_y
            updated[index] = new_row
    return updated


def render_execution_plan(
    plan: dict[str, Any],
    *,
    job_id: str,
    tmpdir: str,
    track: Any | None,
    attempt_id: str | None = None,
) -> dict[str, Any]:
    """Render and verify one strict guided-story variant."""

    from app.pipeline.generative_overlays import (  # noqa: PLC0415
        build_overlays_from_text_elements,
    )
    from app.pipeline.text_overlay_skia import (  # noqa: PLC0415
        burn_text_overlays_skia_with_evidence,
    )
    from app.tasks.template_orchestrate import _concat_demuxer  # noqa: PLC0415

    raw_timing = plan.get("mixed_media_timing")
    mixed_timing = (
        MixedMediaTimingProfile.model_validate(raw_timing) if raw_timing is not None else None
    )
    strict_mixed_duration = uses_quick_photo_long_video_timing(mixed_timing) or bool(
        plan.get("montage_cadence")
    )
    local_by_id, media_receipts = _download_selected(plan, tmpdir)
    moment_paths, moment_receipts = _render_moments(plan, local_by_id, tmpdir)
    assembled = os.path.join(tmpdir, "guided_story_assembled.mp4")
    canvas = _story_canvas(plan.get("output_orientation"))
    transition = plan["transition_policy"]
    per_boundary = _resolved_transition_boundaries(plan)
    per_durations = [
        float(row.get("transition_duration_s") or transition["duration_s"])
        for row in plan["story_timeline"][:-1]
    ]
    has_crossfade = any(value != "cut" for value in per_boundary)
    if len(moment_paths) > 1 and has_crossfade:
        from app.pipeline.transitions import join_with_transitions  # noqa: PLC0415

        try:
            transition_map = {
                "crossfade": "crossfade",
                "dip_to_black": "fade_black",
                "flash": "fade_white",
            }
            # Split at hard cuts. Each visual-transition run is xfade-joined;
            # runs are then concatenated without inventing crossfades at cuts.
            chunks: list[str] = []
            start = 0
            for boundary, value in enumerate(per_boundary + ["cut"]):
                if value == "cut":
                    end = boundary
                    paths = moment_paths[start : end + 1]
                    if len(paths) == 1:
                        chunks.append(paths[0])
                    else:
                        chunk = os.path.join(tmpdir, f"guided_transition_{start}.mp4")
                        join_with_transitions(
                            paths,
                            [
                                transition_map.get(per_boundary[i], "crossfade")
                                for i in range(start, end)
                            ],
                            [
                                float(row["duration_s"])
                                for row in plan["story_timeline"][start : end + 1]
                            ],
                            chunk,
                            transition_duration_s=float(transition["duration_s"]),
                            transition_durations_s=per_durations[start:end] or None,
                            canvas=canvas,
                        )
                        chunks.append(chunk)
                    start = end + 1
            _concat_demuxer(
                chunks,
                assembled,
                tmpdir,
                expected_duration_s=float(plan["resolved_duration_s"]),
                canvas=canvas,
            )
        except Exception as exc:  # noqa: BLE001
            raise GuidedStoryError(
                "guided_story_render_failed",
                "The approved story transitions could not be rendered.",
            ) from exc
    else:
        _concat_demuxer(
            moment_paths,
            assembled,
            tmpdir,
            expected_duration_s=float(plan["resolved_duration_s"]),
            canvas=canvas,
        )
    if plan.get("compiler_version", 0) >= 6:
        assembled = _mux_guided_source_audio(
            assembled,
            plan,
            local_by_id,
            os.path.join(tmpdir, "guided_story_source_audio.mp4"),
        )
    if (plan.get("montage_audio") or {}).get("preview_source_beds"):
        plan["source_audio_options"] = _build_montage_audio_options(
            plan,
            local_by_id,
            assembled,
            job_id=job_id,
            tmpdir=tmpdir,
            attempt_id=attempt_id,
        )
    music = plan.get("music")
    narration = plan.get("narration")
    if music is not None and narration is not None:
        raise GuidedStoryError(
            "guided_story_snapshot_invalid",
            "A recorded voiceover plan cannot also select a music track.",
        )
    if narration is not None:
        assembled = _hold_final_frame_to_duration(
            assembled,
            os.path.join(tmpdir, "guided_story_assembled_held.mp4"),
            target_s=float(plan["resolved_duration_s"]),
        )
        clean_base = os.path.join(tmpdir, "guided_story_base.mp4")
        _mix_pinned_narration(
            assembled,
            clean_base,
            NarrationTrack.model_validate(narration),
            tmpdir=tmpdir,
            duration_s=float(plan["resolved_duration_s"]),
        )
        music_applied = False
        narration_applied = True
    elif music is not None:
        clean_base = os.path.join(tmpdir, "guided_story_base.mp4")
        _mix_pinned_music(
            assembled,
            clean_base,
            tmpdir,
            music,
            track,
            output_duration_s=float(plan["resolved_duration_s"]),
            strict_duration=strict_mixed_duration,
        )
        music_applied = True
        narration_applied = False
    else:
        if _audio_codec(assembled) == "aac":
            clean_base = assembled
        else:
            clean_base = os.path.join(tmpdir, "guided_story_base.mp4")
            _attach_silent_aac(assembled, clean_base)
        music_applied = False
        narration_applied = False

    clean_base = _compose_guided_pretext_lanes(
        clean_base,
        plan,
        job_id=job_id,
        attempt_id=attempt_id,
        tmpdir=tmpdir,
    )
    if strict_mixed_duration:
        clean_base = _enforce_strict_story_duration(
            clean_base,
            os.path.join(tmpdir, "guided_story_base_duration_capped.mp4"),
            target_s=float(plan["resolved_duration_s"]),
        )

    if settings.guided_text_face_placement_enabled:
        plan["text_elements"] = _apply_guided_text_face_placement(
            clean_base, plan["text_elements"], job_id=job_id, canvas=canvas
        )

    final_path = os.path.join(tmpdir, "guided_story_final.mp4")
    elements = [TextElement.model_validate(row) for row in plan["text_elements"]]
    elements, hidden_caption_ids = _apply_guided_caption_meta(
        elements, plan.get("editor_caption_meta")
    )
    plan["editor_hidden_caption_ids"] = sorted(hidden_caption_ids)
    context_elements = [
        TextElement.model_validate(row) for row in plan.get("context_label_text_elements") or []
    ]
    narration_label_elements = [
        TextElement.model_validate(row) for row in plan.get("narration_label_text_elements") or []
    ]
    render_elements = [
        *[element for element in elements if element.id not in hidden_caption_ids],
        *context_elements,
        *narration_label_elements,
    ]
    if render_elements:
        # Context labels historically use ``generative_sequence`` so the editor
        # can project them as a separate lane. The Skia renderer coalesces that
        # role into one full-canvas sequence, however, which loses per-element
        # receipt identity (and makes every label look absent to the strict
        # verifier). Keep the lane projection but render these server-derived
        # labels as independent burn sequences in the authoritative pass.
        def compile_and_tag(source_elements: list[TextElement]) -> list[dict]:
            return _tag_guided_text_overlays(
                build_overlays_from_text_elements(
                    source_elements,
                    video_duration_s=float(plan["resolved_duration_s"]),
                    independent_box_alignment=True,
                ),
                source_elements,
            )

        from app.pipeline.guided_caption_presentation import (  # noqa: PLC0415
            project_guided_caption_overlays,
        )

        overlays = project_guided_caption_overlays(
            compile_and_tag(
                [element for element in elements if element.id not in hidden_caption_ids]
            ),
            plan.get("editor_caption_meta"),
        )
        context_overlays = compile_and_tag(context_elements)
        narration_label_overlays = compile_and_tag(narration_label_elements)
        for overlay in context_overlays:
            if overlay.get("role") == "generative_sequence":
                overlay["role"] = "generative_context_label"
        overlays.extend(context_overlays)
        for overlay in narration_label_overlays:
            if overlay.get("role") == "generative_sequence":
                overlay["role"] = "generative_narration_label"
        overlays.extend(narration_label_overlays)
        text_receipts = burn_text_overlays_skia_with_evidence(
            clean_base,
            overlays,
            final_path,
            tmpdir,
            required_element_ids=[element.id for element in render_elements],
            canvas=canvas,
        )
    else:
        # A timeline edit may legitimately tombstone every approved text
        # interval. Preserve the already-composed clean base byte-for-byte.
        shutil.copyfile(clean_base, final_path)
        text_receipts = []
    final_path = _compose_guided_sfx(
        final_path,
        plan,
        job_id=job_id,
        attempt_id=attempt_id,
        tmpdir=tmpdir,
    )
    if strict_mixed_duration:
        final_path = _enforce_strict_story_duration(
            final_path,
            os.path.join(tmpdir, "guided_story_final_duration_capped.mp4"),
            target_s=float(plan["resolved_duration_s"]),
        )
    if plan.get("compiler_version") == 8 and plan.get("editor_revision_number") is None:
        final_path = _enforce_scheduled_video_frames(
            final_path,
            os.path.join(tmpdir, "guided_story_final_scheduled_frames.mp4"),
            frame_count=round(float(plan["resolved_duration_s"]) * 30),
        )
    receipt = _verify_receipt(
        plan,
        media_receipts,
        moment_receipts,
        text_receipts,
        final_path,
        music_applied=music_applied,
        narration_applied=narration_applied,
        text_stage_input_path=clean_base,
        text_stage_output_path=(
            os.path.join(tmpdir, "guided_story_final.mp4") if render_elements else None
        ),
    )

    attempt_suffix = hashlib.sha256(str(attempt_id or "preview").encode()).hexdigest()[:16]
    base_key = f"generative-jobs/{job_id}/base_1_{VARIANT_ID}_{attempt_suffix}.mp4"
    output_key = f"generative-jobs/{job_id}/variant_1_{VARIANT_ID}_{attempt_suffix}.mp4"
    output_url, base_storage, output_storage = _upload_verified_outputs(
        clean_base,
        final_path,
        base_key=base_key,
        output_key=output_key,
    )
    receipt = GuidedStoryRenderReceipt.model_validate(
        {
            **receipt,
            "base_storage": base_storage,
            "output_storage": output_storage,
        }
    ).model_dump(mode="json")
    return {
        "variant_id": VARIANT_ID,
        "rank": 1,
        "text_mode": "agent_text",
        "resolved_archetype": VARIANT_ID,
        "music_track_id": str(track.id) if track is not None else None,
        "track_title": str(music["title"]) if music else None,
        **song_reference_variant_fields(plan),
        "music_start_s": float(music.get("start_s") or 0.0) if music else None,
        "style_set_id": plan["typography"]["style_id"],
        "intro_text": plan["text_elements"][0]["text"] if plan["text_elements"] else "",
        "intro_mode": "linear",
        "intro_layout": "linear",
        "base_video_path": base_key,
        "video_path": output_key,
        "output_url": output_url,
        "orientation": plan["output_orientation"],
        "orientation_reason": plan["output_orientation_reason"],
        "duration_s": plan["resolved_duration_s"],
        # The editor's existing text projection is the public editable surface.
        # Keep the canonical narration-label lane alongside it so runtime
        # validation can still distinguish server-derived labels from ordinary
        # creator copy without hiding the labels from the editor.
        "text_elements": [
            *plan["text_elements"],
            *(plan.get("narration_label_text_elements") or []),
        ],
        "context_label_text_elements": plan.get("context_label_text_elements") or [],
        "narration_label_text_elements": plan.get("narration_label_text_elements") or [],
        "sound_effects": list(plan.get("editor_sound_effects") or []),
        "media_overlays": list(plan.get("editor_media_overlays") or []),
        "visual_blocks": list(plan.get("editor_visual_blocks") or []),
        "motion_scenes": list(plan.get("editor_motion_scenes") or []),
        "custom_effects": list(plan.get("editor_custom_effects") or []),
        "text_elements_user_edited": False,
        "story_timeline": plan["story_timeline"],
        "proposal_version": plan["proposal_version"],
        "media_digest": plan["media_digest"],
        "render_receipt": receipt,
        "source_audio_mix": "interleaved" if plan.get("source_audio_options") else None,
        "source_audio_options": list(plan.get("source_audio_options") or []),
        "ok": True,
        "render_status": "ready",
    }
