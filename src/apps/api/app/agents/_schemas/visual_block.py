"""First-class visual layers for the plan-item editor.

Structured montage/text-card blocks replace the base picture for a bounded
window. Media blocks are stackable fullscreen or PiP layers below authored text
and captions; legacy ``MediaOverlay`` cards remain above those lanes.
"""

from __future__ import annotations

import math
import uuid
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, field_validator, model_validator

MAX_VISUAL_BLOCKS = 20
MAX_BLOCK_DURATION_S = 10.0
MIN_MEDIA_DURATION_S = 0.1
MIN_MONTAGE_SHOTS = 3
MAX_MONTAGE_SHOTS = 12
_FRAME_TOLERANCE_S = 1.0 / 24.0


class Crop(BaseModel):
    model_config = ConfigDict(extra="ignore")

    x_frac: float = Field(default=0.5, ge=0.0, le=1.0)
    y_frac: float = Field(default=0.5, ge=0.0, le=1.0)
    scale: float = Field(default=1.0, ge=1.0, le=4.0)


class SyncAnchor(BaseModel):
    model_config = ConfigDict(extra="ignore")

    type: Literal["sentence", "keyword", "beat", "manual"]
    time_s: float = Field(ge=0.0)
    label: str | None = Field(default=None, max_length=120)


class VisualShot(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str = Field(default_factory=lambda: uuid.uuid4().hex)
    asset_id: str = Field(min_length=1, max_length=80)
    src_gcs_path: str = Field(min_length=1, max_length=1024)
    kind: Literal["image", "video"]
    start_offset_s: float = Field(ge=0.0)
    duration_s: float = Field(gt=0.0, le=MAX_BLOCK_DURATION_S)
    trim_start_s: float | None = Field(default=None, ge=0.0)
    crop: Crop = Field(default_factory=Crop)
    motion: Literal["none", "zoom_in", "zoom_out", "pan_left", "pan_right"] = "none"
    sync_anchor: SyncAnchor | None = None


class AudioPolicy(BaseModel):
    model_config = ConfigDict(extra="ignore")

    base: Literal["continue", "mute"] = "continue"
    sfx: Literal["continue", "mute"] = "continue"


class MediaTransform(BaseModel):
    """Shared fullscreen transform mirrored by the editor preview."""

    model_config = ConfigDict(extra="ignore")

    fit_mode: Literal["contain", "cover"] = "contain"
    focal_x: float = Field(default=0.5, ge=0.0, le=1.0)
    focal_y: float = Field(default=0.5, ge=0.0, le=1.0)
    zoom: float = Field(default=1.0, ge=1.0, le=4.0)


class SolidBackground(BaseModel):
    type: Literal["solid"]
    color: str = "#111111"

    @field_validator("color")
    @classmethod
    def _color(cls, value: str) -> str:
        value = value.upper()
        if len(value) != 7 or not value.startswith("#"):
            raise ValueError("solid background color must be #RRGGBB")
        int(value[1:], 16)
        return value


class GradientBackground(BaseModel):
    type: Literal["gradient"]
    from_color: str = Field(alias="from")
    to: str
    angle_deg: float = Field(default=180.0, ge=0.0, le=360.0)

    model_config = ConfigDict(populate_by_name=True)

    @field_validator("from_color", "to")
    @classmethod
    def _color(cls, value: str) -> str:
        value = value.upper()
        if len(value) != 7 or not value.startswith("#"):
            raise ValueError("gradient colors must be #RRGGBB")
        int(value[1:], 16)
        return value


class BlurPreviousBackground(BaseModel):
    type: Literal["blur_previous"]
    blur_px: float = Field(default=24.0, ge=1.0, le=80.0)


class AssetBackground(BaseModel):
    type: Literal["asset"]
    shot: VisualShot


CardBackground = Annotated[
    SolidBackground | GradientBackground | BlurPreviousBackground | AssetBackground,
    Field(discriminator="type"),
]


class VisualBlockBase(BaseModel):
    model_config = ConfigDict(extra="ignore")

    version: Literal[1] = 1
    id: str = Field(default_factory=lambda: uuid.uuid4().hex)
    start_s: float = Field(ge=0.0)
    end_s: float = Field(gt=0.0)
    timing_mode: Literal["auto", "manual"] = "manual"
    origin: Literal["ai", "user"] = "user"
    rationale: str | None = Field(default=None, max_length=300)
    transition_in: Literal["cut", "fade"] = "cut"
    transition_out: Literal["cut", "fade"] = "cut"
    audio_policy: AudioPolicy = Field(default_factory=AudioPolicy)

    @model_validator(mode="after")
    def _duration(self) -> VisualBlockBase:
        duration = self.end_s - self.start_s
        if duration <= 0:
            raise ValueError("visual block end_s must be greater than start_s")
        return self


def _validate_structured_duration(block: VisualBlockBase) -> None:
    if block.end_s - block.start_s > MAX_BLOCK_DURATION_S + 1e-6:
        raise ValueError(f"structured visual blocks may not exceed {MAX_BLOCK_DURATION_S:g}s")


class MontageBlock(VisualBlockBase):
    kind: Literal["montage"]
    shots: list[VisualShot] = Field(min_length=MIN_MONTAGE_SHOTS, max_length=MAX_MONTAGE_SHOTS)

    @model_validator(mode="after")
    def _contiguous_shots(self) -> MontageBlock:
        _validate_structured_duration(self)
        expected = 0.0
        for shot in self.shots:
            if abs(shot.start_offset_s - expected) > _FRAME_TOLERANCE_S:
                raise ValueError("montage shots must be contiguous and start at offset zero")
            expected = shot.start_offset_s + shot.duration_s
        if abs(expected - (self.end_s - self.start_s)) > _FRAME_TOLERANCE_S:
            raise ValueError("montage shots must cover the complete block duration")
        return self


class TextCardBlock(VisualBlockBase):
    kind: Literal["text_card"]
    style_preset_id: str | None = Field(default=None, max_length=80)
    background: CardBackground

    @model_validator(mode="after")
    def _structured_duration(self) -> TextCardBlock:
        _validate_structured_duration(self)
        return self


class MediaBlock(VisualBlockBase):
    """User-authored image/video layer composed below text and captions."""

    kind: Literal["media"]
    asset_id: str = Field(min_length=1, max_length=80)
    src_gcs_path: str = Field(min_length=1, max_length=1024)
    preview_gcs_path: str | None = Field(default=None, max_length=1024)
    media_kind: Literal["image", "video"]
    source_duration_s: float | None = Field(default=None, gt=0.0)
    trim_start_s: float | None = Field(default=None, ge=0.0)
    trim_end_s: float | None = Field(default=None, gt=0.0)
    display_mode: Literal["fullscreen", "overlay"] = "fullscreen"
    transform: MediaTransform = Field(default_factory=MediaTransform)
    x_frac: float = Field(default=0.5, ge=0.0, le=1.0)
    y_frac: float = Field(default=0.5, ge=0.0, le=1.0)
    scale: float = Field(default=0.35, ge=0.05, le=1.0)
    z: int = Field(default=0, ge=0)
    source: str | None = Field(default=None, max_length=80)
    effect_group_id: str | None = Field(default=None, max_length=80)

    @model_validator(mode="after")
    def _media_window(self) -> MediaBlock:
        window = self.end_s - self.start_s
        if window + 1e-6 < MIN_MEDIA_DURATION_S:
            raise ValueError(f"media blocks must be at least {MIN_MEDIA_DURATION_S:g}s")
        if self.media_kind == "image":
            self.source_duration_s = None
            self.trim_start_s = None
            self.trim_end_s = None
            return self
        if self.source_duration_s is None:
            raise ValueError("video media blocks require source_duration_s")
        trim_start = self.trim_start_s or 0.0
        trim_end = self.trim_end_s if self.trim_end_s is not None else self.source_duration_s
        trim_end = min(trim_end, self.source_duration_s)
        if trim_start >= trim_end - 1e-6:
            raise ValueError("video media trim end must be greater than trim start")
        if window > trim_end - trim_start + _FRAME_TOLERANCE_S:
            raise ValueError("video media window exceeds the selected source footage")
        self.trim_start_s = trim_start
        self.trim_end_s = trim_end
        return self


VisualBlock = Annotated[MontageBlock | TextCardBlock | MediaBlock, Field(discriminator="kind")]
_VISUAL_BLOCK_LIST = TypeAdapter(list[VisualBlock])


def coerce_visual_blocks(raw: list[dict] | None) -> list[VisualBlock]:
    """Best-effort read/render coercion; invalid legacy entries are dropped."""
    if not raw:
        return []
    out: list[VisualBlock] = []
    for item in raw[:MAX_VISUAL_BLOCKS]:
        try:
            out.extend(_VISUAL_BLOCK_LIST.validate_python([item]))
        except Exception:  # noqa: BLE001 - tolerant read path by design
            continue
    return out


def validate_visual_blocks(raw: list[dict] | list[VisualBlock], *, duration_s: float) -> list[dict]:
    """Strict user/agent write validation, including bounds and overlap."""
    if len(raw) > MAX_VISUAL_BLOCKS:
        raise ValueError(f"Maximum {MAX_VISUAL_BLOCKS} visual blocks allowed")
    blocks = _VISUAL_BLOCK_LIST.validate_python(raw)
    ordered_structured = sorted(
        (block for block in blocks if not isinstance(block, MediaBlock)),
        key=lambda block: (block.start_s, block.end_s, block.id),
    )
    previous_end = 0.0
    ids: set[str] = set()
    for block in blocks:
        if block.id in ids:
            raise ValueError("visual block ids must be unique")
        ids.add(block.id)
        if block.end_s > duration_s + _FRAME_TOLERANCE_S:
            raise ValueError("visual block exceeds the variant duration")
    for block in ordered_structured:
        if block.start_s < previous_end - _FRAME_TOLERANCE_S:
            raise ValueError("structured visual blocks may not overlap")
        previous_end = block.end_s
    return [block.model_dump(by_alias=True, exclude_none=True) for block in blocks]


def validate_visual_block_text_links(blocks: list[dict], text_elements: list[dict]) -> None:
    """Validate one-to-many TextElement.visual_block_id relationships."""
    by_id = {str(block["id"]): block for block in blocks}
    link_counts = {block_id: 0 for block_id in by_id}
    for element in text_elements:
        block_id = element.get("visual_block_id")
        if not block_id:
            continue
        block = by_id.get(str(block_id))
        if block is None:
            raise ValueError("text element references an unknown visual block")
        start_s = float(element.get("start_s", 0.0))
        end_s = float(element.get("end_s", 0.0))
        if (
            start_s < float(block["start_s"]) - _FRAME_TOLERANCE_S
            or end_s > float(block["end_s"]) + _FRAME_TOLERANCE_S
        ):
            raise ValueError("linked text must stay inside its visual block")
        link_counts[str(block_id)] += 1
    for block_id, block in by_id.items():
        if block.get("kind") == "text_card" and link_counts[block_id] == 0:
            raise ValueError("text cards require at least one linked text element")


def retime_montage(block: MontageBlock, *, anchors: list[SyncAnchor] | None = None) -> MontageBlock:
    """Deterministically pace shots around sentence/keyword/beat anchors."""
    duration = block.end_s - block.start_s
    per_shot = duration / len(block.shots)
    candidates = sorted(
        (anchor for anchor in (anchors or []) if block.start_s < anchor.time_s < block.end_s),
        key=lambda anchor: anchor.time_s,
    )
    boundaries = [block.start_s]
    selected_anchors: list[SyncAnchor | None] = [None]
    for index in range(1, len(block.shots)):
        ideal = block.start_s + per_shot * index
        min_time = boundaries[-1] + 0.1
        max_time = block.end_s - 0.1 * (len(block.shots) - index)
        eligible = [anchor for anchor in candidates if min_time <= anchor.time_s <= max_time]
        if eligible:
            priority = {"sentence": 0.0, "keyword": 0.02, "beat": 0.05, "manual": 0.1}
            chosen = min(
                eligible,
                key=lambda anchor: (
                    abs(anchor.time_s - ideal) + priority[anchor.type],
                    anchor.time_s,
                ),
            )
            boundary = chosen.time_s
            candidates.remove(chosen)
        else:
            chosen = None
            boundary = max(min_time, min(max_time, ideal))
        boundaries.append(boundary)
        selected_anchors.append(chosen)
    boundaries.append(block.end_s)
    shots: list[VisualShot] = []
    for index, shot in enumerate(block.shots):
        offset = boundaries[index] - block.start_s
        shot_duration = boundaries[index + 1] - boundaries[index]
        shots.append(
            shot.model_copy(
                update={
                    "start_offset_s": round(offset, 6),
                    "duration_s": round(shot_duration, 6),
                    "sync_anchor": selected_anchors[index],
                }
            )
        )
    return block.model_copy(update={"timing_mode": "auto", "shots": shots})


def block_intervals_with_muted_base(blocks: list[VisualBlock]) -> list[tuple[float, float]]:
    return [
        (block.start_s, block.end_s)
        for block in blocks
        if block.audio_policy.base == "mute" and math.isfinite(block.start_s + block.end_s)
    ]


def iter_visual_shots(blocks: list[dict]) -> list[dict]:
    """Flatten asset-bearing shots from strict model dumps."""
    shots: list[dict] = []
    for block in blocks:
        if block.get("kind") == "montage":
            shots.extend(block.get("shots") or [])
        elif block.get("kind") == "media":
            shots.append(
                {
                    "asset_id": block.get("asset_id"),
                    "src_gcs_path": block.get("src_gcs_path"),
                    "kind": block.get("media_kind"),
                }
            )
        else:
            background = block.get("background") or {}
            if background.get("type") == "asset" and isinstance(background.get("shot"), dict):
                shots.append(background["shot"])
    return shots
