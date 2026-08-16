"""Strict first-class renderer for approved guided-edit proposals.

The approved proposal is the render program. This module never calls the legacy
montage matcher and never drops a selected source or text layer as a fallback.
"""

from __future__ import annotations

import hashlib
import math
import os
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Literal

import structlog
from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.agents._schemas.text_element import TextElement
from app.config import settings
from app.pipeline.canvas import PORTRAIT
from app.pipeline.probe import probe_video
from app.schemas.edit_proposal import EditProposalSnapshot, canonical_media_digest

log = structlog.get_logger()

COMPILER_VERSION = 2
VARIANT_ID = "guided_story"
_FRAME_S = 1.0 / 30.0
_ALLOCATION_EPSILON_S = 0.0005
_FRAME_FLOOR_EPSILON_S = 1e-9
_DURATION_MATCH_TOLERANCE_S = 0.001
_MEDIA_PREP_MAX_WORKERS = 3
_DIRECTION_POLICY = {
    "guided_story": {"min_moment_s": 1.4, "transition": "crossfade", "text_effect": "fade-in"},
    "fast_montage": {"min_moment_s": 0.8, "transition": "none", "text_effect": "static"},
    "text_explainer": {
        "min_moment_s": 1.8,
        "transition": "crossfade",
        "text_effect": "fade-in",
    },
}


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

    style_id: Literal["guided_story_v1"]
    font: str = Field(min_length=1)


class GuidedStoryMusic(BaseModel):
    model_config = ConfigDict(extra="forbid")

    track_id: str = Field(min_length=1)
    title: str = Field(min_length=1)
    audio_gcs_path: str = Field(min_length=1)
    generation: str = Field(min_length=1)
    start_s: float = Field(ge=0)


class GuidedStoryExecutionPlan(BaseModel):
    """Strict JSONB contract reused verbatim on worker redelivery."""

    model_config = ConfigDict(extra="forbid")

    compiler_version: Literal[1, 2]
    proposal_version: int = Field(ge=1)
    media_digest: str = Field(min_length=64, max_length=64)
    direction: Literal["guided_story", "fast_montage", "text_explainer"]
    goal: str
    pace: Literal["relaxed", "balanced", "fast"]
    approved_duration_s: float = Field(gt=0)
    resolved_duration_s: float = Field(gt=0)
    selected_media_ids: list[str] = Field(min_length=1)
    story_timeline: list[GuidedStoryMoment] = Field(min_length=1)
    beat_windows: list[GuidedStoryBeatWindow] = Field(min_length=1)
    text_elements: list[TextElement] = Field(min_length=1)
    transition_policy: GuidedStoryTransitionPolicy
    typography: GuidedStoryTypography
    music: GuidedStoryMusic | None = None

    @model_validator(mode="after")
    def validate_internal_receipt_contract(self) -> GuidedStoryExecutionPlan:
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

    schema_version: Literal[1]
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
    music_applied: bool
    music: GuidedStoryMusic | None
    output: GuidedStoryOutputReceipt
    base_storage: GuidedStoryStorageReceipt | None = None
    output_storage: GuidedStoryStorageReceipt | None = None
    media_stages: list[dict[str, Any]]
    moment_stages: list[dict[str, Any]]
    text_stages: list[dict[str, Any]]

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
        return self


def _round_frame(seconds: float) -> float:
    return round(max(_FRAME_S, round(seconds / _FRAME_S) * _FRAME_S), 3)


def _selected_media_ids(snapshot: EditProposalSnapshot) -> list[str]:
    selected: list[str] = []
    for beat in snapshot.story_beats:
        for media_id in beat.media_ids:
            if media_id not in selected:
                selected.append(media_id)
    return selected


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


def _allocate_beat_durations(
    refs: list[Any],
    *,
    beat_duration_s: float,
    min_moment_s: float,
    overlaps_s: list[float],
    beat_topic: str,
) -> list[float]:
    """Water-fill a beat while respecting the usable length of short videos."""

    if beat_duration_s + _FRAME_S < min_moment_s * len(refs):
        raise GuidedStoryError(
            "guided_story_duration_impossible",
            f"Beat {beat_topic} is too short to show all approved media clearly.",
        )

    capacities: list[float] = []
    for ref, overlap in zip(refs, overlaps_s, strict=True):
        if ref.kind == "image":
            capacities.append(math.inf)
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
        capacities.append(capacity)

    allocated = [min_moment_s for _ref in refs]
    remaining = max(0.0, beat_duration_s - sum(allocated))
    active = list(range(len(refs)))
    while remaining > _ALLOCATION_EPSILON_S:
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
            reduction = min(-difference, rounded[index] - min_moment_s)
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
    snapshot: EditProposalSnapshot, beat_windows: list[dict], policy: dict
) -> list[dict]:
    total_s = float(snapshot.duration_s)
    title_end = min(total_s, 3.2 if snapshot.direction != "fast_montage" else 2.2)
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
        start_s = float(window["start_s"])
        if start_s == 0:
            start_s = min(float(window["end_s"]) - _FRAME_S, title_end)
        elements.append(
            TextElement(
                id=f"guided-thought-{beat.beat_id}",
                text=thought,
                start_s=max(0.0, round(start_s, 3)),
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


def _compile_execution_plan_version(
    guided_snapshot: object,
    *,
    track: dict[str, Any] | None,
    compiler_version: Literal[1, 2],
) -> dict[str, Any]:
    """Compile a deterministic plan with an explicitly versioned allocator."""

    proposal_version, media_digest, snapshot = validate_guided_snapshot(guided_snapshot)
    policy = _DIRECTION_POLICY[snapshot.direction]
    transition_type = policy["transition"] if snapshot.pace != "fast" else "none"
    transition_duration_s = 0.2 if snapshot.pace == "relaxed" else 0.12
    selected_ids = _selected_media_ids(snapshot)
    by_id = {ref.media_id: ref for ref in snapshot.media}
    if not selected_ids:
        raise GuidedStoryError("guided_story_snapshot_invalid", "The approved edit has no media.")

    weight_total = sum(float(beat.duration_s) for beat in snapshot.story_beats)
    if weight_total <= 0:
        raise GuidedStoryError(
            "guided_story_duration_impossible", "The approved story has no usable timing."
        )
    beat_windows: list[dict[str, Any]] = []
    moments: list[dict[str, Any]] = []
    moment_count = sum(len(beat.media_ids) for beat in snapshot.story_beats)
    cursor = 0.0
    for beat_index, beat in enumerate(snapshot.story_beats):
        if beat_index == len(snapshot.story_beats) - 1:
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
                    "image_motion": "subtle_zoom_in" if ref.kind == "image" else None,
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
            selected_media_ids=selected_ids,
            story_timeline=moments,
            beat_windows=beat_windows,
            text_elements=_text_elements(snapshot, beat_windows, policy),
            transition_policy={
                "type": transition_type,
                "duration_s": transition_duration_s,
            },
            typography={"style_id": "guided_story_v1", "font": "Inter-Bold"},
            music=track,
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
    canonical = _compile_execution_plan_version(
        guided_snapshot,
        track=validated.music.model_dump(mode="json") if validated.music is not None else None,
        compiler_version=validated.compiler_version,
    )
    normalized = validated.model_dump(mode="json", exclude_none=False)
    if normalized != canonical:
        raise GuidedStoryError(
            "guided_story_snapshot_invalid", "The saved render plan was changed after approval."
        )
    return normalized


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
                "bytes": os.path.getsize(local),
                "sha256": _sha256(local),
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


def _render_image_moment(source: str, output: str, *, duration_s: float, layout: str) -> None:
    from app.pipeline.reframe import _encoding_args  # noqa: PLC0415

    width, height, fps = settings.output_width, settings.output_height, settings.output_fps
    total_frames = max(1, int(round(duration_s * fps)))
    zoom = f"1.0+(0.06*on/{max(1, total_frames - 1)})"
    zoom_xy = "x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)'"
    if layout == "supporting_card":
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
            f"setsar=1,fps={fps},format=yuv420p[v]"
        )
    else:
        vf = (
            f"[0:v]scale={width * 2}:{height * 2}:force_original_aspect_ratio=increase,"
            f"crop={width * 2}:{height * 2},"
            f"zoompan=z='{zoom}':{zoom_xy}:d={total_frames}:fps={fps}:s={width}x{height},"
            f"setsar=1,format=yuv420p[v]"
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
        *_encoding_args(output, preset="ultrafast", crf="14", canvas=PORTRAIT),
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
            has_audio=False,
            canvas=PORTRAIT,
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
    for index, moment in enumerate(plan["story_timeline"]):
        output = os.path.join(tmpdir, f"moment_{index:02d}.mp4")
        if moment["kind"] == "image":
            _render_image_moment(
                local_by_id[moment["media_id"]],
                output,
                duration_s=float(moment["duration_s"]),
                layout=moment["layout"],
            )
        else:
            _render_video_moment(
                local_by_id[moment["media_id"]],
                output,
                start_s=float(moment["source_start_s"]),
                end_s=float(moment["source_end_s"]),
                layout=moment["layout"],
            )
        probe = probe_video(output)
        if (
            probe.width != settings.output_width
            or probe.height != settings.output_height
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
    audio_codec = _audio_codec(final_path)
    duration_ok = abs(probe.duration_s - float(plan["resolved_duration_s"])) <= max(
        0.2, len(moment_receipts) * 0.04
    )
    verified = bool(
        expected_beats == actual_beats
        and expected_moments == actual_moments
        and expected_media == actual_media
        and set(expected_text) == set(actual_text)
        and len(media_receipts) == len(expected_media)
        and duration_ok
        and probe.width == settings.output_width
        and probe.height == settings.output_height
        and probe.codec == "h264"
        and audio_codec == "aac"
        and os.path.getsize(final_path) > 0
    )
    receipt_data = {
        "schema_version": 1,
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
        "actual_duration_s": round(float(probe.duration_s), 3),
        "music_applied": music_applied,
        "music": plan.get("music") if music_applied else None,
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
    }
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
    verified = bool(
        expected_ids == actual_ids
        and abs(float(probe.duration_s) - duration_s) <= 0.2
        and probe.width == settings.output_width
        and probe.height == settings.output_height
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
            "actual_duration_s": round(float(probe.duration_s), 3),
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
    staged_text = [str(row.get("element_id")) for row in receipt.text_stages if row.get("visible")]
    exact_story = [row.model_dump(mode="json") for row in typed_plan.story_timeline]
    prefix = f"generative-jobs/{job_id}/"
    base_path = str(result.get("base_video_path") or "")
    video_path = str(result.get("video_path") or "")
    structurally_valid = bool(
        result.get("variant_id") == VARIANT_ID
        and result.get("resolved_archetype") == VARIANT_ID
        and result.get("render_status") == "ready"
        and result.get("ok") is True
        and result.get("proposal_version") == typed_plan.proposal_version
        and result.get("media_digest") == typed_plan.media_digest
        and result.get("story_timeline") == exact_story
        and current_text == receipt.expected_text_ids
        and approved_text == expected_text
        and receipt.proposal_version == typed_plan.proposal_version
        and receipt.media_digest == typed_plan.media_digest
        and receipt.expected_beat_ids == expected_beats
        and receipt.expected_moment_ids == expected_moments
        and receipt.expected_media_ids == typed_plan.selected_media_ids
        and receipt.music == typed_plan.music
        and staged_media == expected_media_stages
        and staged_moments == expected_moment_stages
        and staged_text == receipt.actual_text_ids
        and receipt.expected_duration_s == typed_plan.resolved_duration_s
        and abs(receipt.actual_duration_s - typed_plan.resolved_duration_s) <= 0.2
        and receipt.output.width == settings.output_width
        and receipt.output.height == settings.output_height
        and base_path.startswith(prefix)
        and video_path.startswith(prefix)
        and base_path.endswith(".mp4")
        and video_path.endswith(".mp4")
        and base_path != video_path
        and receipt.base_storage is not None
        and receipt.output_storage is not None
        and receipt.base_storage.path == base_path
        and receipt.output_storage.path == video_path
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


def _mix_pinned_music(
    assembled: str,
    clean_base: str,
    tmpdir: str,
    music: dict[str, Any],
    track: Any,
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

    try:
        _mix_template_audio(
            assembled,
            str(music["audio_gcs_path"]),
            clean_base,
            tmpdir,
            audio_start_offset_s=float(music.get("start_s") or 0.0),
            require_audio=True,
            audio_generation=str(music["generation"]),
            force_video_duration=True,
        )
    except Exception as exc:  # noqa: BLE001
        raise GuidedStoryError(
            "guided_story_music_missing",
            "The exact approved music file is no longer available.",
        ) from exc


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

    local_by_id, media_receipts = _download_selected(plan, tmpdir)
    moment_paths, moment_receipts = _render_moments(plan, local_by_id, tmpdir)
    assembled = os.path.join(tmpdir, "guided_story_assembled.mp4")
    transition = plan["transition_policy"]
    if len(moment_paths) > 1 and transition["type"] != "none":
        from app.pipeline.transitions import join_with_transitions  # noqa: PLC0415

        try:
            join_with_transitions(
                moment_paths,
                [transition["type"]] * (len(moment_paths) - 1),
                [float(row["duration_s"]) for row in plan["story_timeline"]],
                assembled,
                transition_duration_s=float(transition["duration_s"]),
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
        )
    music = plan.get("music")
    if music is not None:
        clean_base = os.path.join(tmpdir, "guided_story_base.mp4")
        _mix_pinned_music(assembled, clean_base, tmpdir, music, track)
        music_applied = True
    else:
        if _audio_codec(assembled) == "aac":
            clean_base = assembled
        else:
            clean_base = os.path.join(tmpdir, "guided_story_base.mp4")
            _attach_silent_aac(assembled, clean_base)
        music_applied = False

    elements = [TextElement.model_validate(row) for row in plan["text_elements"]]
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
    final_path = os.path.join(tmpdir, "guided_story_final.mp4")
    text_receipts = burn_text_overlays_skia_with_evidence(
        clean_base,
        overlays,
        final_path,
        tmpdir,
        required_element_ids=[row["id"] for row in plan["text_elements"]],
        canvas=PORTRAIT,
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
        "intro_text": plan["text_elements"][0]["text"],
        "intro_mode": "linear",
        "intro_layout": "linear",
        "base_video_path": base_key,
        "video_path": output_key,
        "output_url": output_url,
        "orientation": "portrait",
        "duration_s": plan["resolved_duration_s"],
        "text_elements": plan["text_elements"],
        "text_elements_user_edited": False,
        "story_timeline": plan["story_timeline"],
        "proposal_version": plan["proposal_version"],
        "media_digest": plan["media_digest"],
        "render_receipt": receipt,
        "ok": True,
        "render_status": "ready",
    }
