"""Compile the narrowly-qualified phone editor media visual layer."""

from __future__ import annotations

import math
from typing import Any

from app.agents._schemas.visual_block import MediaBlock
from app.kria.portable_visual import VisualMediaPlacement
from app.kria.recipes import AssetFingerprint, MediaAsset, MediaSize, TimelineClip, TimelineTrack
from app.kria.render_assets import VisualRenderAsset
from app.services.phone_sources import PhoneVisualBinding, require_bound_visual


class UnsupportedEditorMedia(ValueError):
    """The server/editor shape is valid, but lacks device-render parity."""


def _require_plain_media(block: MediaBlock) -> None:
    if (
        block.editor_style is not None
        or block.transition_in != "cut"
        or block.transition_out != "cut"
        or block.audio_policy.base != "continue"
        or block.audio_policy.sfx != "continue"
        or block.source_crop is not None
        or block.playback_rate not in {None, 1}
    ):
        raise UnsupportedEditorMedia("editor media treatment is not phone-qualified")


def compile_editor_media_track(
    raw_blocks: list[dict[str, Any]],
    *,
    visuals: tuple[PhoneVisualBinding, ...],
    timeline_duration_s: float,
    assets: dict[str, MediaAsset],
    manifest: dict[str, Any],
) -> TimelineTrack:
    """Compile plain image/video editor blocks to one silent overlay track.

    The resulting aliases contain no storage path. Every video window must fit
    its immutable trimmed source, so the device never invents a frozen hold.
    """
    blocks: list[MediaBlock] = []
    for raw in raw_blocks:
        if not isinstance(raw, dict) or raw.get("kind") != "media":
            raise UnsupportedEditorMedia("only media visual blocks are phone-qualified")
        try:
            blocks.append(MediaBlock.model_validate(raw))
        except Exception as exc:
            raise UnsupportedEditorMedia("editor visual blocks are invalid") from exc
    ordered = sorted(blocks, key=lambda block: (block.z, block.start_s, block.id))
    clips: list[TimelineClip] = []
    for order, block in enumerate(ordered, start=1):
        _require_plain_media(block)
        if block.start_s < 0 or block.end_s > timeline_duration_s + 1e-6:
            raise UnsupportedEditorMedia("editor media block is outside the timeline")
        window = block.end_s - block.start_s
        visual = require_bound_visual(
            visuals,
            media_id=block.asset_id,
            path=block.src_gcs_path,
            generation=next(
                (
                    candidate.generation
                    for candidate in visuals
                    if candidate.media_id == block.asset_id
                    and candidate.gcs_path == block.src_gcs_path
                ),
                "",
            ),
        )
        if visual.kind != block.media_kind:
            raise UnsupportedEditorMedia("editor media kind does not match its receipt")
        if block.media_kind == "video":
            if block.source_duration_s is None or not math.isclose(
                block.source_duration_s, visual.duration_s or 0, abs_tol=0.05
            ):
                raise UnsupportedEditorMedia("editor video duration does not match its receipt")
            trim_start = block.trim_start_s or 0.0
            trim_end = block.trim_end_s if block.trim_end_s is not None else visual.duration_s
            if trim_end is None or trim_end > visual.duration_s + 1e-6:
                raise UnsupportedEditorMedia("editor video trim exceeds its receipt")
            available = trim_end - trim_start
            if available <= 0 or window > available + 1e-6:
                raise UnsupportedEditorMedia("editor video window exceeds its trimmed source")
        else:
            trim_start = 0.0
        asset: VisualRenderAsset = visual.render_asset()
        manifest[asset.id] = asset
        assets[asset.id] = MediaAsset(
            id=asset.id,
            relative_path=asset.id,
            fingerprint=AssetFingerprint(hex=visual.sha256, byte_count=visual.byte_count),
            duration=(
                visual.duration_s
                if visual.kind == "video" and (visual.duration_s or 0) <= 1800
                else None
            ),
            natural_size=(
                None
                if visual.kind == "image"
                else MediaSize(width=visual.width or 1, height=visual.height or 1)
            ),
            orientation_degrees=visual.orientation_degrees,
        )
        source_duration = window
        if source_duration <= 0:
            raise UnsupportedEditorMedia("editor media has no playable source duration")
        clips.append(
            TimelineClip(
                id=f"editor-media-{block.id}",
                source_asset_id=asset.id,
                source_start=trim_start,
                source_duration=source_duration,
                timeline_start=block.start_s,
                rate=1,
                volume=0,
                visual_placement=VisualMediaPlacement(
                    order=order,
                    contain=block.transform.fit_mode == "contain",
                    focal_x=block.transform.focal_x,
                    focal_y=block.transform.focal_y,
                    zoom=block.transform.zoom,
                    width_fraction=block.scale if block.display_mode == "overlay" else None,
                    x_fraction=block.x_frac,
                    y_fraction=block.y_frac,
                    window_start=block.start_s,
                    window_end=block.end_s,
                ),
            )
        )
    return TimelineTrack(id="editor-media", kind="overlay", clips=clips)
