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

from app.agents._schemas.text_element import TextElement
from app.config import settings
from app.pipeline.canvas import LANDSCAPE, PORTRAIT, Canvas
from app.pipeline.duration_contract import (
    STRICT_MIXED_MEDIA_DURATION_TOLERANCE_S,
    STRICT_MIXED_MEDIA_MAX_CFR_OVERRUN_S,
)
from app.pipeline.probe import probe_video
from app.schemas.edit_proposal import (
    GUIDED_STORY_MIN_MOMENT_S,
    EditProposalSnapshot,
    MixedMediaTimingProfile,
    MontageCadenceConstraint,
    canonical_media_digest,
    mixed_media_hold_bounds,
    uses_quick_photo_long_video_timing,
)

log = structlog.get_logger()

COMPILER_VERSION = 4
VARIANT_ID = "guided_story"
_FRAME_S = 1.0 / 30.0
_ALLOCATION_EPSILON_S = 0.0005
_FRAME_FLOOR_EPSILON_S = 1e-9
_DURATION_MATCH_TOLERANCE_S = 0.001
_MEDIA_PREP_MAX_WORKERS = 3
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


class GuidedStoryExecutionPlan(BaseModel):
    """Strict JSONB contract reused verbatim on worker redelivery."""

    model_config = ConfigDict(extra="forbid")

    compiler_version: Literal[1, 2, 3, 4]
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
    # Optional post-approval runtime projection.  Canonical approved plans
    # leave these unset; v2 revisions carry them without changing approval.
    editor_revision_number: int | None = Field(default=None, ge=1)
    editor_revision_hash: str | None = None
    editor_sound_effects: list[dict[str, Any]] = Field(default_factory=list)
    editor_media_overlays: list[dict[str, Any]] = Field(default_factory=list)
    editor_visual_blocks: list[dict[str, Any]] = Field(default_factory=list)
    editor_motion_scenes: list[dict[str, Any]] = Field(default_factory=list)
    editor_custom_effects: list[dict[str, Any]] = Field(default_factory=list)
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
        if self.editor_revision_number is None and not self.text_elements:
            raise ValueError("approved guided stories require at least one text element")
        if len(self.selected_media_ids) != len(set(self.selected_media_ids)):
            raise ValueError("selected media IDs must be unique")
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

    @model_validator(mode="after")
    def validate_strict_equality(self) -> GuidedStoryRenderReceipt:
        pairs = (
            (self.expected_beat_ids, self.actual_beat_ids),
            (self.expected_moment_ids, self.actual_moment_ids),
            (self.expected_media_ids, self.actual_media_ids),
            (self.expected_text_ids, self.actual_text_ids),
        )
        if any(expected != actual for expected, actual in pairs):
            raise ValueError("receipt expected/actual identities must match exactly")
        if self.media_count != len(self.actual_media_ids):
            raise ValueError("receipt media count does not match its identities")
        if self.image_count + self.video_count != self.media_count:
            raise ValueError("receipt media kinds do not add up to its media count")
        if self.music_applied != (self.music is not None):
            raise ValueError("receipt music identity does not match application state")
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
) -> float:
    """Compile quick mixed-media holds to an exact, source-safe frame budget."""

    fps = int(round(1.0 / _FRAME_S))
    target_frames = max(1, int(round(float(target_s) * fps)))
    frame_counts: list[int] = []
    image_headroom: list[tuple[float, int]] = []
    original_video_end_s: dict[int, float] = {}
    floor_video_frames: dict[int, int] = {}
    for index, moment in enumerate(moments):
        desired_frames = float(moment["duration_s"]) * fps
        frames = max(1, int(math.floor(desired_frames + 1e-6)))
        if moment["kind"] == "image":
            minimum_frames = int(math.ceil(0.5 * fps - 1e-6))
            maximum_frames = int(math.floor(0.8 * fps + 1e-6))
            frames = min(maximum_frames, max(minimum_frames, frames))
            image_headroom.append((desired_frames - math.floor(desired_frames), index))
        else:
            original_video_end_s[index] = float(moment["source_end_s"])
            floor_video_frames[index] = frames
            if 1.5 - 0.001 <= float(moment["duration_s"]) < 1.5:
                # Proposal source/output windows allow 1ms numeric tolerance.
                # The near-minimum source still contains the 45th frame.
                frames = int(math.ceil(1.5 * fps - 1e-6))
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
        maximum_frames = int(math.floor(0.8 * fps + 1e-6))
        addition = min(remaining_frames, maximum_frames - frame_counts[index])
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
    if snapshot.fast_cuts:
        selected: list[str] = []
        for cut in snapshot.fast_cuts:
            if cut.media_id not in selected:
                selected.append(cut.media_id)
        return selected
    selected: list[str] = []
    for beat in snapshot.story_beats:
        for media_id in beat.media_ids:
            if media_id not in selected:
                selected.append(media_id)
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
    if proposal_version < 1 or canonical_media_digest(snapshot.media) != media_digest:
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
            0.8
            if quick_mixed_timing and ref.kind == "image"
            else min(3.0, max(0.0, float(ref.duration_s or 0.0) - overlap))
            if quick_mixed_timing
            else math.inf
            if ref.kind == "image"
            else max(0.0, float(ref.duration_s or 0.0) - overlap)
            for ref, overlap in zip(beat_refs, overlaps_s, strict=True)
        ]
        if not quick_mixed_timing:
            floors.append(min_moment_s * len(beat.media_ids))
        else:
            floors.append(
                sum(
                    0.5 if ref.kind == "image" else min(1.5, float(ref.duration_s or 0.0))
                    for ref in beat_refs
                )
            )
        caps.append(sum(capacities_b))
        ideals.append(float(snapshot.duration_s) * float(beat.duration_s) / weight_total)
        moment_index += len(beat_refs)

    allocated = [
        min(max(ideal, floor), cap) for ideal, floor, cap in zip(ideals, floors, caps, strict=True)
    ]
    deficit = float(snapshot.duration_s) - sum(allocated)
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

    residual = round(float(snapshot.duration_s) - sum(resolved), 3)
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
        min_moment_s * len(refs)
        if not quick_mixed_timing
        else sum(
            mixed_media_hold_bounds(ref.kind).minimum_s
            if ref.kind == "image"
            else min(
                mixed_media_hold_bounds(ref.kind).minimum_s,
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
                mixed_media_hold_bounds(ref.kind).maximum_s if quick_mixed_timing else math.inf
            )
            continue
        source_duration = float(ref.duration_s or 0.0)
        if source_duration <= 0:
            raise GuidedStoryError(
                "guided_story_duration_impossible",
                f"Video {ref.source_filename or ref.media_id} has no usable duration.",
            )
        capacity = max(0.0, source_duration - overlap)
        if capacity + _FRAME_S < min_moment_s:
            raise GuidedStoryError(
                "guided_story_duration_impossible",
                f"Video {ref.source_filename or ref.media_id} is too short to show clearly.",
            )
        capacities.append(
            min(mixed_media_hold_bounds(ref.kind).maximum_s, capacity)
            if quick_mixed_timing
            else capacity
        )

    if not quick_mixed_timing:
        allocated = [min_moment_s for _ref in refs]
    else:
        allocated = [
            mixed_media_hold_bounds(ref.kind).preferred_s
            if ref.kind == "image"
            else min(
                mixed_media_hold_bounds(ref.kind).preferred_s,
                float(ref.duration_s or 0.0),
            )
            for ref in refs
        ]
        allocated = [
            max(
                mixed_media_hold_bounds(ref.kind).minimum_s
                if ref.kind == "image"
                else min(
                    mixed_media_hold_bounds(ref.kind).minimum_s,
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
                else mixed_media_hold_bounds(refs[index].kind).minimum_s
                if refs[index].kind == "image"
                else min(
                    mixed_media_hold_bounds(refs[index].kind).minimum_s,
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
    compiler_version: Literal[1, 2, 3, 4],
) -> list[dict]:
    total_s = float(snapshot.duration_s)
    title_end = min(total_s, 3.2 if snapshot.direction != "fast_montage" else 2.2)
    if snapshot.montage_text_bindings and snapshot.fast_cuts:
        text_by_source = {entry.media_id: entry.text for entry in snapshot.montage_text_bindings}
        elements: list[dict] = []
        for cut, window in zip(snapshot.fast_cuts, beat_windows, strict=True):
            text = text_by_source.get(cut.media_id)
            if not text:
                continue
            elements.append(
                TextElement(
                    id=f"montage-text-{cut.cut_id}",
                    text=text,
                    start_s=float(window["start_s"]),
                    end_s=float(window["end_s"]),
                    role="generative_intro",
                    position="custom" if compiler_version >= 3 else "bottom",
                    x_frac=0.5 if compiler_version >= 3 else None,
                    y_frac=0.78 if compiler_version >= 3 else None,
                    font_family="Fraunces" if compiler_version >= 3 else "Inter-Bold",
                    size_px=58 if compiler_version >= 3 else 50,
                    color="#FFF8F0" if compiler_version >= 3 else "#FFFFFF",
                    highlight_color="#D9FF70" if compiler_version >= 3 else "#6FE7F7",
                    stroke_width=0 if compiler_version >= 3 else 4,
                    shadow_enabled=True,
                    shadow_style="standard" if compiler_version >= 3 else None,
                    effect="static",
                    alignment="center",
                    max_width_frac=0.82,
                ).model_dump(mode="json", exclude_none=True)
            )
        return elements
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
                font_family="Fraunces" if compiler_version >= 3 else "Inter-Bold",
                size_px=(92 if compiler_version >= 3 else 78),
                color="#FFF8F0" if compiler_version >= 3 else "#FFFFFF",
                highlight_color="#D9FF70" if compiler_version >= 3 else "#6FE7F7",
                stroke_width=0 if compiler_version >= 3 else 5,
                shadow_enabled=True,
                shadow_style="standard" if compiler_version >= 3 else None,
                effect="static",
                alignment="center",
                max_width_frac=0.8 if compiler_version >= 3 else 0.86,
            ).model_dump(mode="json", exclude_none=True)
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
                font_family="Inter-Bold",
                size_px=78 if snapshot.direction == "fast_montage" else 84,
                color="#FFFFFF",
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
        return elements

    elements = [
        TextElement(
            id="guided-title",
            text=snapshot.title,
            start_s=0.0,
            end_s=title_end,
            role="generative_intro",
            position="custom",
            x_frac=0.5,
            y_frac=0.16,
            font_family="Fraunces",
            size_px=92 if snapshot.direction == "fast_montage" else 104,
            color="#FFF8F0",
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
    for beat, window in zip(snapshot.story_beats, beat_windows, strict=True):
        thought = beat.thought.strip()
        if not thought:
            continue
        start_s = float(window["start_s"])
        elements.append(
            TextElement(
                id=f"guided-thought-{beat.beat_id}",
                text=thought,
                start_s=max(0.0, round(start_s, 3)),
                end_s=float(window["end_s"]),
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
    return elements


def _compile_execution_plan_version(
    guided_snapshot: object,
    *,
    track: dict[str, Any] | None,
    compiler_version: Literal[1, 2, 3, 4],
) -> dict[str, Any]:
    """Compile a deterministic plan with an explicitly versioned allocator."""

    proposal_version, media_digest, snapshot = validate_guided_snapshot(guided_snapshot)
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
    transition_type = (
        "none"
        if quick_mixed_timing
        else policy["transition"]
        if snapshot.pace != "fast"
        else "none"
    )
    transition_duration_s = (
        0.0 if quick_mixed_timing else 0.2 if snapshot.pace == "relaxed" else 0.12
    )
    if not selected_ids:
        raise GuidedStoryError("guided_story_snapshot_invalid", "The approved edit has no media.")
    if compiler_version >= 3:
        output_orientation = snapshot.output_orientation or "portrait"
        output_orientation_reason = snapshot.output_orientation_reason
    else:
        output_orientation = "portrait"
        output_orientation_reason = "Legacy guided stories used the portrait canvas."

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
            duration_s=float(snapshot.duration_s),
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
                    "layout": "fullscreen",
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
        if abs(cursor - float(snapshot.duration_s)) > 0.15:
            raise GuidedStoryError(
                "guided_story_duration_impossible",
                "Fast montage cut durations do not match the approved duration.",
            )
        if quick_mixed_timing:
            cursor = _quantize_quick_mixed_timeline(
                moments,
                beat_windows,
                target_s=cursor,
            )
        normalized_track = _music_payload(track, duration_s=cursor)
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
                text_elements=_text_elements(
                    snapshot, beat_windows, policy, compiler_version=compiler_version
                ),
                transition_policy={"type": "none", "duration_s": 0.0},
                typography=(
                    {"style_id": "guided_story_v2", "font": "Fraunces"}
                    if compiler_version >= 3
                    else {"style_id": "guided_story_v1", "font": "Inter-Bold"}
                ),
                music=normalized_track,
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
            resolved_beat_s = round(float(snapshot.duration_s) - cursor, 3)
        else:
            resolved_beat_s = _round_frame(
                float(snapshot.duration_s) * float(beat.duration_s) / weight_total
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
                    "layout": beat.layout,
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
    if abs(cursor - float(snapshot.duration_s)) > 0.05:
        raise GuidedStoryError(
            "guided_story_duration_impossible", "The approved story timing could not be resolved."
        )
    if quick_mixed_timing:
        cursor = _quantize_quick_mixed_timeline(
            moments,
            beat_windows,
            target_s=cursor,
        )

    normalized_track = _music_payload(track, duration_s=cursor)

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
                {"style_id": "guided_story_v2", "font": "Fraunces"}
                if compiler_version >= 3
                else {"style_id": "guided_story_v1", "font": "Inter-Bold"}
            ),
            music=normalized_track,
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

    return _compile_execution_plan_version(
        guided_snapshot,
        track=track,
        compiler_version=COMPILER_VERSION,
    )


def validate_proposal_timing(snapshot: EditProposalSnapshot) -> None:
    """Reject an editorial revision that the strict renderer cannot allocate."""

    if snapshot.fast_cuts:
        media_by_id = {ref.media_id: ref for ref in snapshot.media}
        previous_id: str | None = None
        windows_by_media: dict[str, list[tuple[float, float]]] = {}
        for cut in snapshot.fast_cuts:
            if cut.media_id == previous_id:
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
            snapshot.montage_cadence is not None
            and snapshot.montage_cadence.reuse_policy == "allow_repeat"
        ):
            for windows in windows_by_media.values():
                windows.sort()
                for previous, current in zip(windows, windows[1:]):
                    if current[0] < previous[1] - _DURATION_MATCH_TOLERANCE_S:
                        raise GuidedStoryError(
                            "guided_story_snapshot_invalid",
                            "Fast montage cuts cannot reuse overlapping video footage.",
                        )

    media_digest = canonical_media_digest(snapshot.media)
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
    proposal_version, media_digest, _snapshot = validate_guided_snapshot(guided_snapshot)
    try:
        validated = GuidedStoryExecutionPlan.model_validate(plan)
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
    if normalized != canonical:
        raise GuidedStoryError(
            "guided_story_snapshot_invalid", "The saved render plan was changed after approval."
        )
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
        updated = typed.model_copy(
            update={
                "output_orientation": output_orientation,
                "output_orientation_reason": (
                    "The creator selected this output format in the editor."
                ),
                "text_elements": (
                    [TextElement.model_validate(row) for row in text_elements]
                    if text_elements is not None
                    else typed.text_elements
                ),
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


def compile_guided_runtime_plan(
    canonical_plan: object,
    guided_snapshot: object,
    revision: object,
) -> dict[str, Any]:
    """Compile a validated v2 revision without mutating the approval plan.

    The canonical plan remains the provenance fence.  This projection only
    changes the effective moments/timing/audio and carries the revision hash
    into the receipt; every selected source still comes from the approved
    snapshot's exact-generation pool.
    """

    from app.schemas.guided_edit_revision import normalize_guided_editor_revision

    try:
        canonical = GuidedStoryExecutionPlan.model_validate(canonical_plan)
        proposal_version, media_digest, snapshot = validate_guided_snapshot(guided_snapshot)
        normalized_revision = normalize_guided_editor_revision(
            revision,
            expected_approval_version=proposal_version,
            expected_media_digest=media_digest,
        )
        approved_sources = {
            (
                ref.media_id,
                ref.lane,
                ref.gcs_path,
                ref.generation,
                ref.kind,
                ref.duration_s,
            )
            for ref in snapshot.media
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
        approved_text_ids = [element.id for element in canonical.text_elements]
        revision_text_ids = [
            str(row.get("id")) for row in normalized_revision.get("text_elements") or []
        ]
        tombstoned_text_ids = [
            str(row.get("record_id"))
            for row in normalized_revision.get("tombstones") or []
            if row.get("lane") == "text_elements" and row.get("record_id")
        ]
        if set(revision_text_ids) | set(tombstoned_text_ids) != set(approved_text_ids):
            raise GuidedStoryError(
                "guided_story_revision_invalid",
                "Active and tombstoned text identities must match the approved story text.",
            )
        # Music swaps are post-approval editor revisions. The route pins the
        # selected ready track's exact object generation; the worker then
        # downloads that generation. The immutable proposal is provenance, not
        # an allowlist that would make every legitimate swap fail at render.
        source_by_id = {ref.media_id: ref for ref in snapshot.media}
        base_by_media: dict[str, dict[str, Any]] = {}
        for moment in canonical.story_timeline:
            base_by_media.setdefault(moment.media_id, moment.model_dump(mode="json"))
        moments: list[dict[str, Any]] = []
        beat_windows: list[dict[str, Any]] = []
        for index, segment in enumerate(normalized_revision["segments"]):
            source = source_by_id.get(segment["media_id"])
            if source is None:
                raise ValueError("revision source is not in the approved snapshot")
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
        if audio.get("mode") == "track":
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
        runtime_payload = canonical.model_dump(mode="json", exclude_none=False)
        runtime_payload.update(
            {
                "proposal_version": proposal_version,
                "media_digest": media_digest,
                "approved_duration_s": float(canonical.approved_duration_s),
                "resolved_duration_s": round(max(moment["output_end_s"] for moment in moments), 3),
                "output_orientation": normalized_revision.get("orientation", "portrait"),
                "output_orientation_reason": (
                    "The creator selected this output format in the editor."
                ),
                "selected_media_ids": selected_ids,
                "story_timeline": moments,
                "beat_windows": beat_windows,
                "text_elements": list(normalized_revision.get("text_elements") or []),
                "music": music,
                "editor_revision_number": normalized_revision["revision_number"],
                "editor_revision_hash": normalized_revision["state_hash"],
                "editor_sound_effects": list(normalized_revision.get("sound_effects") or []),
                "editor_media_overlays": list(normalized_revision.get("media_overlays") or []),
                "editor_visual_blocks": list(normalized_revision.get("visual_blocks") or []),
                "editor_motion_scenes": list(normalized_revision.get("motion_scenes") or []),
                "editor_custom_effects": list(normalized_revision.get("custom_effects") or []),
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
        """
        with Image.open(source) as opened:
            image = ImageOps.exif_transpose(opened)
            image.load()
            has_alpha = "A" in image.getbands() or "transparency" in image.info
            if has_alpha:
                render_path = f"{os.path.splitext(source)[0]}_render.png"
                image.convert("RGBA").save(render_path, format="PNG", optimize=False)
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
                "output_duration_s": round(float(probe.duration_s), 3),
                "width": probe.width,
                "height": probe.height,
                "codec": probe.codec,
                "sha256": _sha256(output),
            }
        )
    return outputs, receipts


def _verify_receipt(
    plan: dict[str, Any],
    media_receipts: list[dict],
    moment_receipts: list[dict],
    text_receipts: list[dict],
    final_path: str,
    *,
    music_applied: bool,
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
    expected_text = [row["id"] for row in plan["text_elements"]]
    actual_text = [row["element_id"] for row in text_receipts if row.get("visible")]
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
    verified = bool(
        expected_beats == actual_beats
        and expected_moments == actual_moments
        and expected_media == actual_media
        and set(expected_text) == set(actual_text)
        and len(media_receipts) == len(expected_media)
        and duration_ok
        and probe.width == canvas.width
        and probe.height == canvas.height
        and probe.codec == "h264"
        and audio_codec == "aac"
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
        "media_count": len(actual_media),
        "image_count": len({r["media_id"] for r in moment_receipts if r["kind"] == "image"}),
        "video_count": len({r["media_id"] for r in moment_receipts if r["kind"] == "video"}),
        "expected_duration_s": plan["resolved_duration_s"],
        "actual_duration_s": round(actual_video_duration_s, 3),
        "output_orientation": plan["output_orientation"],
        "output_orientation_reason": plan["output_orientation_reason"],
        "music_applied": music_applied,
        "music": plan.get("music") if music_applied else None,
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
        and output_hash != _sha256(clean_base_path)
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
    expected_text = [row.id for row in typed_plan.text_elements]
    current_text = [str(row.get("id")) for row in list(result.get("text_elements") or [])]
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
    staged_text = [str(row.get("element_id")) for row in receipt.text_stages if row.get("visible")]
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
        and current_text == receipt.expected_text_ids
        and approved_text == (typed_plan.editor_approved_text_ids or expected_text)
        and receipt.proposal_version == typed_plan.proposal_version
        and receipt.media_digest == typed_plan.media_digest
        and receipt.expected_beat_ids == expected_beats
        and receipt.expected_moment_ids == expected_moments
        and receipt.expected_media_ids == typed_plan.selected_media_ids
        and receipt.music == typed_plan.music
        and staged_media == expected_media_stages
        and staged_moments == expected_moment_stages
        and cadence_receipt_ok
        and staged_text == receipt.actual_text_ids
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
    if music is not None:
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
    else:
        if _audio_codec(assembled) == "aac":
            clean_base = assembled
        else:
            clean_base = os.path.join(tmpdir, "guided_story_base.mp4")
            _attach_silent_aac(assembled, clean_base)
        music_applied = False

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

    final_path = os.path.join(tmpdir, "guided_story_final.mp4")
    elements = [TextElement.model_validate(row) for row in plan["text_elements"]]
    if elements:
        overlays = build_overlays_from_text_elements(
            elements,
            video_duration_s=float(plan["resolved_duration_s"]),
            independent_box_alignment=True,
        )
        element_by_text = {
            (element.text, element.start_s, element.end_s): element.id for element in elements
        }
        for overlay in overlays:
            key = (
                str(overlay.get("text") or ""),
                float(overlay.get("start_s") or 0.0),
                float(overlay.get("end_s") or 0.0),
            )
            overlay["element_id"] = element_by_text.get(key)
        text_receipts = burn_text_overlays_skia_with_evidence(
            clean_base,
            overlays,
            final_path,
            tmpdir,
            required_element_ids=[row["id"] for row in plan["text_elements"]],
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
    receipt = _verify_receipt(
        plan,
        media_receipts,
        moment_receipts,
        text_receipts,
        final_path,
        music_applied=music_applied,
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
        "text_elements": plan["text_elements"],
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
