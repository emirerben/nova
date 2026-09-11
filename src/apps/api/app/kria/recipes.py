"""Renderer-neutral EditRecipeV1 shared by native clients and cloud adapters."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class _RecipeModel(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True, allow_inf_nan=False)


class Canvas(_RecipeModel):
    width: int = Field(ge=16, le=7680)
    height: int = Field(ge=16, le=7680)


class AssetFingerprint(_RecipeModel):
    algorithm: str = Field(default="sha256", min_length=1, max_length=32)
    hex: str = Field(min_length=1, max_length=256)
    byte_count: int = Field(ge=0)


class MediaSize(_RecipeModel):
    width: float = Field(gt=0)
    height: float = Field(gt=0)


class MediaAsset(_RecipeModel):
    id: str = Field(min_length=1, max_length=160)
    relative_path: str = Field(min_length=1, max_length=1024)
    fingerprint: AssetFingerprint | None = None
    duration: float | None = Field(default=None, gt=0, le=1800)
    natural_size: MediaSize | None = None
    frame_rate: float | None = Field(default=None, gt=0, le=240)
    orientation_degrees: int = Field(default=0, ge=-360, le=360)
    is_proxy_available: bool = False


class MediaTransform(_RecipeModel):
    scale: float = Field(default=1, gt=0, le=20)
    rotation_degrees: float = Field(default=0, ge=-3600, le=3600)
    position_x: float = Field(default=0, ge=-100000, le=100000)
    position_y: float = Field(default=0, ge=-100000, le=100000)


class Transition(_RecipeModel):
    kind: Literal["crossfade", "fade_black", "fade_white", "wipe_left", "wipe_right"] = "crossfade"
    duration: float = Field(default=0.35, gt=0, le=10)


class TextTreatment(_RecipeModel):
    text: str = Field(min_length=1, max_length=500)
    font_name: str = Field(default="Helvetica-Bold", min_length=1, max_length=160)
    font_size: float = Field(default=72, gt=0, le=1000)
    color_rgba: list[float] = Field(
        default_factory=lambda: [1, 1, 1, 1], min_length=4, max_length=4
    )
    anchor: Literal["top", "center", "bottom"] = "center"
    animation: Literal["none", "fade", "fade_scale"] = "fade_scale"

    @model_validator(mode="after")
    def _colors(self) -> TextTreatment:
        if any(channel < 0 or channel > 1 for channel in self.color_rgba):
            raise ValueError("color_rgba channels must be between 0 and 1")
        return self


class TimelineClip(_RecipeModel):
    id: str = Field(min_length=1, max_length=160)
    source_asset_id: str = Field(min_length=1, max_length=160)
    source_start: float = Field(ge=0)
    source_duration: float = Field(gt=0, le=1800)
    timeline_start: float = Field(ge=0)
    rate: float = Field(gt=0, le=20)
    transform: MediaTransform = Field(default_factory=MediaTransform)
    transition: Transition | None = None
    text: TextTreatment | None = None
    volume: float = Field(default=1, ge=0, le=2)


class TimelineTrack(_RecipeModel):
    id: str = Field(min_length=1, max_length=160)
    kind: Literal["video", "overlay", "audio"]
    clips: list[TimelineClip] = Field(default_factory=list, max_length=100)


class AudioMixRecipe(_RecipeModel):
    music_asset_id: str | None = Field(default=None, max_length=160)
    music_volume: float = Field(default=1, ge=0, le=2)
    original_volume: float = Field(default=1, ge=0, le=2)
    fade_in: float = Field(default=0, ge=0, le=60)
    fade_out: float = Field(default=0, ge=0, le=60)
    duck_original_during_music: bool = False


MediaCapability = Literal[
    "basicComposition",
    "positionedText",
    "animatedText",
    "crossfade",
    "clipTransitions",
    "audioMix",
    "variableSpeed",
    "alphaOverlay",
    "hevcDecode",
    "hdr",
    "local1080Export",
]


class EditRecipeV1(_RecipeModel):
    """Portable, validated edit description; it contains no storage internals."""

    schema_version: Literal[1] = 1
    renderer_version: str = Field(default="kria-ios-1", min_length=1, max_length=100)
    canvas: Canvas = Field(default_factory=lambda: Canvas(width=1080, height=1920))
    frame_rate: float = Field(default=30, gt=0, le=240)
    assets: list[MediaAsset] = Field(default_factory=list, max_length=100)
    tracks: list[TimelineTrack] = Field(default_factory=list, max_length=20)
    audio: AudioMixRecipe = Field(default_factory=AudioMixRecipe)
    required_capabilities: set[MediaCapability] = Field(default_factory=set)

    @model_validator(mode="after")
    def _references(self) -> EditRecipeV1:
        ids = {asset.id for asset in self.assets}
        if len(ids) != len(self.assets):
            raise ValueError("asset IDs must be unique")
        clips = [clip for track in self.tracks for clip in track.clips]
        if any(clip.transition and clip.transition.kind != "crossfade" for clip in clips):
            # New transition programs must not be offered as legacy crossfade
            # capability to clients that have not verified this implementation.
            self.required_capabilities = self.required_capabilities | {"clipTransitions"}
        if any(clip.source_asset_id not in ids for clip in clips):
            raise ValueError("timeline clip references an unknown asset")
        if self.audio.music_asset_id and self.audio.music_asset_id not in ids:
            raise ValueError("audio references an unknown asset")
        return self

    @property
    def duration(self) -> float:
        return max(
            (
                clip.timeline_start + clip.source_duration / clip.rate
                for track in self.tracks
                for clip in track.clips
            ),
            default=0,
        )


def adapt_editor_snapshot(snapshot: Mapping[str, Any]) -> EditRecipeV1:
    """Project an authorized editor/status snapshot without copying assembly_plan."""
    timeline = (
        snapshot.get("timeline") if isinstance(snapshot.get("timeline"), Mapping) else snapshot
    )
    raw_clips = timeline.get("clips") or timeline.get("slots") or []
    assets: list[dict[str, Any]] = []
    clips: list[dict[str, Any]] = []
    seen: set[str] = set()
    timeline_cursor = 0.0
    for index, raw in enumerate(raw_clips):
        if not isinstance(raw, Mapping) or raw.get("removed"):
            continue
        source = raw.get("media_id") or raw.get("source_ref") or raw.get("clip_id")
        source = source if source is not None else raw.get("clip_index")
        duration = raw.get("source_duration_s") or raw.get("duration_s")
        if source is None or duration is None or float(duration) <= 0:
            continue
        source_id = str(source)
        if source_id not in seen:
            assets.append(
                {"id": source_id, "relative_path": source_id, "duration": float(duration)}
            )
            seen.add(source_id)
        rate = float(raw.get("rate") or 1)
        timeline_start = raw.get("timeline_start_s")
        if timeline_start is None:
            timeline_start = timeline_cursor
        clips.append(
            {
                "id": str(raw.get("slot_id") or f"clip-{index}"),
                "source_asset_id": source_id,
                "source_start": float(raw.get("in_s") or 0),
                "source_duration": float(duration),
                "timeline_start": float(timeline_start),
                "rate": rate,
                "volume": float(raw.get("volume") if raw.get("volume") is not None else 1),
            }
        )
        timeline_cursor = max(timeline_cursor, float(timeline_start) + float(duration) / rate)
    raw_audio = snapshot.get("audio") if isinstance(snapshot.get("audio"), Mapping) else {}
    return EditRecipeV1(
        canvas=Canvas(
            width=int(snapshot.get("width") or 1080), height=int(snapshot.get("height") or 1920)
        ),
        frame_rate=float(snapshot.get("fps") or snapshot.get("frame_rate") or 30),
        assets=assets,
        tracks=[{"id": "video", "kind": "video", "clips": clips}] if clips else [],
        audio={
            "music_asset_id": raw_audio.get("music_asset_id"),
            "music_volume": raw_audio.get("music_volume", 1),
            "original_volume": raw_audio.get("original_volume", 1),
            "fade_in": raw_audio.get("fade_in", 0),
            "fade_out": raw_audio.get("fade_out", 0),
            "duck_original_during_music": raw_audio.get("duck_original_during_music", False),
        },
    )


def adapt_authoritative_job_snapshot(job: Any, variant_id: str | None = None) -> EditRecipeV1:
    """Bridge a loaded Job to the recipe without returning its internal JSON.

    This is intentionally the only seam that knows the current Job projection:
    callers receive opaque ``clip-N`` references and never ``assembly_plan`` or
    storage paths.  It is useful to API routes that already own authorization
    and have loaded the authoritative job row.
    """
    plan = getattr(job, "assembly_plan", None)
    if not isinstance(plan, Mapping):
        return EditRecipeV1()
    variants = plan.get("variants") or []
    variant = next(
        (
            item
            for item in variants
            if isinstance(item, Mapping) and str(item.get("variant_id")) == str(variant_id)
        ),
        next((item for item in variants if isinstance(item, Mapping)), {}),
    )
    timeline = variant.get("user_timeline") or variant.get("ai_timeline") or {}
    rows = []
    for index, row in enumerate(timeline.get("slots") or []):
        if not isinstance(row, Mapping):
            continue
        rows.append(
            {
                "slot_id": row.get("slot_id"),
                "clip_index": row.get("clip_index", index),
                "in_s": row.get("in_s", 0),
                "duration_s": row.get("duration_s"),
                "rate": row.get("rate", 1),
                "volume": row.get("volume", 1),
                "removed": row.get("removed", False),
            }
        )
    return adapt_editor_snapshot({"timeline": {"slots": rows}})
