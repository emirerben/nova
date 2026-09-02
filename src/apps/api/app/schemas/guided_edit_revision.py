"""Story-native guided editor revision contract.

The approved guided proposal is immutable.  This module describes the small,
canonical JSON envelope that is layered on top of that approval for editor
changes.  It intentionally contains exact source identities so a revision can
never silently fall back to the live PlanItem pool.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.pipeline.look_presets import normalize_look_adjustments, normalize_look_preset
from app.schemas.edit_proposal import MAX_EDIT_PROPOSAL_MEDIA
from app.services.editor_limits import EDITOR_MAX_TIMELINE_SLOTS, MOTION_FPS

MAX_GUIDED_EDITOR_SEGMENTS = EDITOR_MAX_TIMELINE_SLOTS
MAX_GUIDED_EDITOR_DURATION_S = 60.0
MIN_GUIDED_EDITOR_SEGMENT_S = 0.1
GUIDED_EDITOR_FPS = MOTION_FPS
GUIDED_EDITOR_FRAME_S = 1.0 / GUIDED_EDITOR_FPS
GUIDED_EDITOR_SCHEMA_VERSION = 1
GUIDED_EDITOR_RENDERER_VERSION = "guided-story-editor-v2"

# Lane records are persisted as untyped JSON because each renderer owns a
# different payload shape.  Their IDs are nevertheless part of the guided
# revision's canonical identity contract.  Keep the limit aligned with the
# other guided editor identity fields (segment IDs and parent IDs).
GUIDED_EDITOR_LANES = (
    "text_elements",
    "sound_effects",
    "media_overlays",
    "visual_blocks",
    "motion_scenes",
    "custom_effects",
)
GUIDED_EDITOR_RECORD_ID_MAX_LENGTH = 100
MAX_GUIDED_EDITOR_TOMBSTONES = 200


class GuidedEditorSource(BaseModel):
    model_config = ConfigDict(extra="forbid")

    media_id: str = Field(min_length=1, max_length=100)
    lane: Literal["clip", "asset"] = "clip"
    gcs_path: str = Field(min_length=1, max_length=2048)
    generation: str = Field(min_length=1, max_length=200)
    kind: Literal["image", "video"]
    duration_s: float | None = Field(default=None, gt=0)


class GuidedEditorSegment(BaseModel):
    model_config = ConfigDict(extra="forbid")

    segment_id: str = Field(min_length=1, max_length=100)
    media_id: str = Field(min_length=1, max_length=100)
    source_start_s: float = Field(default=0.0, ge=0)
    source_end_s: float | None = Field(default=None, gt=0)
    duration_s: float = Field(gt=0, le=MAX_GUIDED_EDITOR_DURATION_S)
    transition_after: Literal["cut", "crossfade", "dip_to_black", "flash"] = "cut"
    transition_duration_s: float = Field(default=0.0, ge=0, le=0.3)
    look_preset: str = "none"
    look_adjustments: dict[str, float] | None = None
    output_start_s: float = Field(default=0.0, ge=0)
    output_end_s: float | None = Field(default=None, gt=0)
    parent_segment_id: str | None = Field(default=None, max_length=100)

    @model_validator(mode="after")
    def validate_segment(self) -> GuidedEditorSegment:
        if self.output_end_s is not None and self.output_end_s <= self.output_start_s:
            raise ValueError("segment output window must be ordered")
        if self.duration_s < MIN_GUIDED_EDITOR_SEGMENT_S:
            raise ValueError("segment duration is below the 0.1s editor floor")
        if self.transition_duration_s < 0.1:
            object.__setattr__(self, "transition_after", "cut")
            object.__setattr__(self, "transition_duration_s", 0.0)
        preset = normalize_look_preset(self.look_preset)
        controls = normalize_look_adjustments(preset, self.look_adjustments)
        object.__setattr__(self, "look_preset", preset)
        object.__setattr__(self, "look_adjustments", controls.model_dump() if controls else None)
        if self.source_end_s is not None and self.source_end_s <= self.source_start_s:
            raise ValueError("source window must be ordered")
        return self


class GuidedEditorAudio(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mode: Literal["none", "track"] = "none"
    removed: bool = False
    track_id: str | None = None
    title: str | None = None
    audio_gcs_path: str | None = None
    generation: str | None = None
    start_s: float = Field(default=0.0, ge=0)
    end_s: float | None = Field(default=None, gt=0)
    level: float = Field(default=1.0, ge=0.0, le=1.0)

    @model_validator(mode="after")
    def validate_audio(self) -> GuidedEditorAudio:
        fields = (self.track_id, self.title, self.audio_gcs_path, self.generation)
        if self.mode == "track" and any(not value for value in fields):
            raise ValueError("track audio requires an exact track identity")
        if self.mode == "track" and self.removed:
            raise ValueError("active track audio cannot be marked removed")
        if self.mode == "none" and any(value for value in fields):
            raise ValueError("silent audio cannot carry a track identity")
        if self.end_s is not None and self.end_s <= self.start_s:
            raise ValueError("audio window must be ordered")
        return self


class GuidedEditorRevision(BaseModel):
    """Canonical active post-approval editor state."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1] = GUIDED_EDITOR_SCHEMA_VERSION
    approval_proposal_version: int = Field(ge=1)
    approval_media_digest: str = Field(min_length=64, max_length=64)
    revision_number: int = Field(ge=1)
    state_hash: str = Field(default="", min_length=0, max_length=64)
    renderer_version: str = GUIDED_EDITOR_RENDERER_VERSION
    effect_schema_version: str = "custom-effects-v1"
    base_generation: str = ""
    orientation: Literal["portrait", "landscape"] = "portrait"
    sources: list[GuidedEditorSource] = Field(min_length=1, max_length=MAX_EDIT_PROPOSAL_MEDIA)
    segments: list[GuidedEditorSegment] = Field(min_length=1, max_length=MAX_GUIDED_EDITOR_SEGMENTS)
    audio: GuidedEditorAudio = Field(default_factory=GuidedEditorAudio)
    text_elements: list[dict[str, Any]] = Field(default_factory=list, max_length=50)
    sound_effects: list[dict[str, Any]] = Field(default_factory=list, max_length=100)
    media_overlays: list[dict[str, Any]] = Field(default_factory=list, max_length=100)
    visual_blocks: list[dict[str, Any]] = Field(default_factory=list, max_length=20)
    motion_scenes: list[dict[str, Any]] = Field(default_factory=list, max_length=100)
    custom_effects: list[dict[str, Any]] = Field(default_factory=list, max_length=20)
    lane_hashes: dict[str, str] = Field(default_factory=dict)
    tombstones: list[dict[str, Any]] = Field(
        default_factory=list, max_length=MAX_GUIDED_EDITOR_TOMBSTONES
    )

    @model_validator(mode="after")
    def validate_revision(self) -> GuidedEditorRevision:
        validate_guided_revision_lane_identities(self)
        source_ids = {source.media_id for source in self.sources}
        if len(source_ids) != len(self.sources):
            raise ValueError("guided editor source IDs must be unique")
        segment_ids = {segment.segment_id for segment in self.segments}
        if len(segment_ids) != len(self.segments):
            raise ValueError("guided editor segment IDs must be unique")
        if any(segment.media_id not in source_ids for segment in self.segments):
            raise ValueError("guided editor segment references an unapproved source")
        if any(
            segment.source_end_s is not None
            and segment.media_id in source_ids
            and next(source for source in self.sources if source.media_id == segment.media_id).kind
            == "video"
            and next(
                source for source in self.sources if source.media_id == segment.media_id
            ).duration_s
            is not None
            and segment.source_end_s
            > next(
                source for source in self.sources if source.media_id == segment.media_id
            ).duration_s
            + 0.05
            for segment in self.segments
        ):
            raise ValueError("guided editor source window exceeds source duration")
        total = max(
            float(segment.output_end_s or segment.output_start_s + segment.duration_s)
            for segment in self.segments
        )
        if total > MAX_GUIDED_EDITOR_DURATION_S + 1e-6:
            raise ValueError("guided editor output exceeds 60 seconds")
        if self.state_hash and self.state_hash != guided_editor_state_hash(
            self, include_hash=False
        ):
            raise ValueError("guided editor revision state hash is stale")
        return self


def validate_guided_revision_lane_identities(
    revision: GuidedEditorRevision | Mapping[str, Any],
) -> None:
    """Validate the cross-lane identity boundary of a guided revision.

    The lane payloads intentionally remain flexible JSON dictionaries, but
    their identity is not flexible: every active record must have one stable,
    bounded string ID, and IDs are globally unique across lanes.  Tombstones
    use the same global identity namespace and must point at one of the known
    lanes.  Keeping this check next to the canonical revision schema prevents
    route-specific validators from accepting subtly different identity rules.

    This function raises ``ValueError`` so callers that validate a plain JSON
    dict get the same failure semantics as ``GuidedEditorRevision``.  It does
    not normalize or mutate records; a caller must explicitly remove a
    tombstone when restoring its record, so an active record and its tombstone
    can never coexist in a normalized revision.
    """

    raw: Mapping[str, Any]
    if isinstance(revision, GuidedEditorRevision):
        raw = revision.model_dump(mode="python")
    elif isinstance(revision, Mapping):
        raw = revision
    else:  # pragma: no cover - public type is intentionally narrow
        raise TypeError("guided revision must be a mapping")

    def bounded_identity(value: object, *, field: str) -> str:
        if not isinstance(value, str):
            raise ValueError(f"{field} must be a string")
        identity = value.strip()
        if not identity:
            raise ValueError(f"{field} must be nonblank")
        if identity != value:
            raise ValueError(f"{field} must not contain surrounding whitespace")
        if len(identity) > GUIDED_EDITOR_RECORD_ID_MAX_LENGTH:
            raise ValueError(f"{field} exceeds {GUIDED_EDITOR_RECORD_ID_MAX_LENGTH} characters")
        return identity

    active_by_id: dict[str, tuple[str, int]] = {}
    for lane in GUIDED_EDITOR_LANES:
        records = raw.get(lane) or []
        if not isinstance(records, Sequence) or isinstance(records, (str, bytes)):
            raise ValueError(f"guided editor {lane} must be a list")
        for index, record in enumerate(records):
            if not isinstance(record, Mapping):
                raise ValueError(f"guided editor {lane}[{index}] must be an object")
            record_id = bounded_identity(record.get("id"), field=f"{lane}[{index}].id")
            prior = active_by_id.get(record_id)
            if prior is not None:
                prior_lane, prior_index = prior
                raise ValueError(
                    "guided editor record ID must be unique across lanes: "
                    f"{record_id!r} appears in {prior_lane}[{prior_index}] and "
                    f"{lane}[{index}]"
                )
            active_by_id[record_id] = (lane, index)

    tombstones = raw.get("tombstones") or []
    if not isinstance(tombstones, Sequence) or isinstance(tombstones, (str, bytes)):
        raise ValueError("guided editor tombstones must be a list")
    tombstone_by_id: dict[str, tuple[str, int]] = {}
    for index, tombstone in enumerate(tombstones):
        if not isinstance(tombstone, Mapping):
            raise ValueError(f"guided editor tombstones[{index}] must be an object")
        lane = tombstone.get("lane")
        if lane not in GUIDED_EDITOR_LANES:
            raise ValueError(
                f"guided editor tombstones[{index}].lane must be one of "
                f"{', '.join(GUIDED_EDITOR_LANES)}"
            )
        record_id = bounded_identity(
            tombstone.get("record_id"),
            field=f"tombstones[{index}].record_id",
        )
        if record_id in active_by_id:
            active_lane, active_index = active_by_id[record_id]
            raise ValueError(
                "guided editor tombstone collides with active record ID: "
                f"{record_id!r} ({active_lane}[{active_index}])"
            )
        prior = tombstone_by_id.get(record_id)
        if prior is not None:
            prior_lane, prior_index = prior
            raise ValueError(
                "guided editor tombstone record IDs must be unique: "
                f"{record_id!r} appears in tombstones[{prior_index}] ({prior_lane}) "
                f"and tombstones[{index}] ({lane})"
            )
        tombstone_by_id[record_id] = (str(lane), index)


def guided_editor_state_hash(
    revision: GuidedEditorRevision | dict[str, Any], *, include_hash: bool = False
) -> str:
    raw = (
        revision.model_dump(mode="json", exclude_none=False)
        if isinstance(revision, GuidedEditorRevision)
        else dict(revision)
    )
    if not include_hash:
        raw.pop("state_hash", None)
    payload = json.dumps(raw, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def normalize_guided_editor_revision(
    raw: GuidedEditorRevision | dict[str, Any],
    *,
    expected_approval_version: int | None = None,
    expected_media_digest: str | None = None,
    expected_revision_number: int | None = None,
) -> dict[str, Any]:
    """Validate and canonicalize a revision before storing it in JSONB."""

    revision = (
        raw if isinstance(raw, GuidedEditorRevision) else GuidedEditorRevision.model_validate(raw)
    )
    if (
        expected_approval_version is not None
        and revision.approval_proposal_version != expected_approval_version
    ):
        raise ValueError("guided editor approval version is stale")
    if (
        expected_media_digest is not None
        and revision.approval_media_digest != expected_media_digest
    ):
        raise ValueError("guided editor approval media digest is stale")
    if (
        expected_revision_number is not None
        and revision.revision_number != expected_revision_number
    ):
        raise ValueError("guided editor revision is stale")
    normalized = revision.model_dump(mode="json", exclude_none=False)

    def frame(value: float) -> float:
        return round(round(float(value) * GUIDED_EDITOR_FPS) / GUIDED_EDITOR_FPS, 6)

    cursor = 0.0
    for segment in normalized["segments"]:
        segment["source_start_s"] = frame(segment.get("source_start_s") or 0.0)
        segment["duration_s"] = max(MIN_GUIDED_EDITOR_SEGMENT_S, frame(segment["duration_s"]))
        segment["source_end_s"] = frame(
            segment.get("source_end_s")
            or float(segment["source_start_s"]) + float(segment["duration_s"])
        )
        requested_start = frame(segment.get("output_start_s") or cursor)
        start = max(cursor, requested_start)
        duration = float(segment["duration_s"])
        segment["output_start_s"] = frame(start)
        segment["output_end_s"] = frame(start + duration)
        overlap = (
            float(segment.get("transition_duration_s") or 0.0)
            if segment.get("transition_after") != "cut"
            else 0.0
        )
        cursor = max(segment["output_end_s"] - frame(overlap), segment["output_start_s"])
    total = max(float(row["output_end_s"]) for row in normalized["segments"])
    if total > MAX_GUIDED_EDITOR_DURATION_S + 1e-6:
        raise ValueError("guided editor output exceeds 60 seconds after frame quantization")
    audio = normalized["audio"]
    audio["start_s"] = frame(audio.get("start_s") or 0.0)
    if audio.get("mode") == "track":
        # start/end are offsets inside the pinned track, not output-clock
        # timestamps. Clamp the selected window's LENGTH to the revised story
        # duration while preserving a non-zero track offset.
        max_track_end = audio["start_s"] + total
        audio["end_s"] = frame(min(float(audio.get("end_s") or max_track_end), max_track_end))
        if audio["end_s"] <= audio["start_s"]:
            audio["end_s"] = frame(max_track_end)
    else:
        audio["end_s"] = None
    normalized["lane_hashes"] = {
        lane: hashlib.sha256(
            json.dumps(normalized.get(lane) or [], sort_keys=True, separators=(",", ":")).encode(
                "utf-8"
            )
        ).hexdigest()
        for lane in (
            "text_elements",
            "sound_effects",
            "media_overlays",
            "visual_blocks",
            "motion_scenes",
            "custom_effects",
            "tombstones",
        )
    }
    normalized["state_hash"] = guided_editor_state_hash(normalized)
    return normalized


def guided_editor_revision_from_approval(
    *,
    proposal_version: int,
    media_digest: str,
    snapshot: dict[str, Any],
    execution_plan: dict[str, Any],
    base_generation: str = "",
) -> dict[str, Any]:
    """Create the initial editable projection without mutating approval."""

    sources = [
        GuidedEditorSource(
            media_id=str(row["media_id"]),
            lane=str(row.get("lane") or "clip"),
            gcs_path=str(row["gcs_path"]),
            generation=str(row["generation"]),
            kind=str(row["kind"]),
            duration_s=row.get("duration_s"),
        )
        for row in snapshot.get("media") or []
    ]
    segments: list[GuidedEditorSegment] = []
    transition_policy = execution_plan.get("transition_policy") or {}
    approved_transition = "crossfade" if transition_policy.get("type") == "crossfade" else "cut"
    approved_transition_duration_s = (
        float(transition_policy.get("duration_s") or 0.0) if approved_transition != "cut" else 0.0
    )
    for index, moment in enumerate(execution_plan.get("story_timeline") or []):
        duration = float(moment.get("duration_s") or 0.0)
        start = float(moment.get("output_start_s") or 0.0)
        end = float(moment.get("output_end_s") or start + duration)
        segments.append(
            GuidedEditorSegment(
                segment_id=str(moment.get("moment_id") or f"segment-{index}"),
                media_id=str(moment["media_id"]),
                source_start_s=float(moment.get("source_start_s") or 0.0),
                source_end_s=float(moment.get("source_end_s") or duration),
                duration_s=max(MIN_GUIDED_EDITOR_SEGMENT_S, duration),
                transition_after=(
                    approved_transition
                    if index < len(execution_plan.get("story_timeline") or []) - 1
                    else "cut"
                ),
                transition_duration_s=approved_transition_duration_s
                if index < len(execution_plan.get("story_timeline") or []) - 1
                else 0.0,
                output_start_s=start,
                output_end_s=end,
            )
        )
    music = execution_plan.get("music")
    story_duration_s = max(
        (float(segment.output_end_s or 0.0) for segment in segments),
        default=0.0,
    )
    music_start_s = float(music.get("start_s") or 0.0) if music else 0.0
    audio = GuidedEditorAudio(
        mode="track" if music else "none",
        track_id=str(music["track_id"]) if music else None,
        title=str(music["title"]) if music else None,
        audio_gcs_path=str(music["audio_gcs_path"]) if music else None,
        generation=str(music["generation"]) if music else None,
        start_s=music_start_s,
        end_s=(float(music.get("end_s") or music_start_s + story_duration_s) if music else None),
    )
    return normalize_guided_editor_revision(
        GuidedEditorRevision(
            approval_proposal_version=proposal_version,
            approval_media_digest=media_digest,
            revision_number=1,
            base_generation=base_generation,
            orientation=execution_plan.get("output_orientation", "portrait"),
            sources=sources,
            segments=segments,
            audio=audio,
            text_elements=list(execution_plan.get("text_elements") or []),
        )
    )
