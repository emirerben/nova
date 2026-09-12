"""Source-bound portable recipe. Additional rendering lanes remain gated work."""

from __future__ import annotations

from typing import Literal

from pydantic import Field, model_serializer, model_validator

from app.kria.portable_camera import CameraPulse
from app.kria.portable_motion import MotionSceneProgram
from app.kria.portable_text import PortableTextLayer
from app.kria.portable_visual import VisualCanvasFill
from app.kria.recipes import EditRecipeV1
from app.kria.render_assets import RenderAssetManifest


class EditRecipeV2(EditRecipeV1):
    """V2 assets are opaque aliases backed by an immutable source manifest.

    Retain the timeline metadata shape for the shared composition engine, but
    prohibit storage paths and require the fingerprint in both projections to
    agree. V1 remains decodable for saved projects; it cannot carry this field.
    """

    schema_version: Literal[2] = 2
    renderer_version: Literal["kria-ios-2"] = "kria-ios-2"
    asset_manifest: RenderAssetManifest
    motion_scenes: MotionSceneProgram | None = None
    visual_fills: list[VisualCanvasFill] = Field(default_factory=list, max_length=100)
    camera_pulses: list[CameraPulse] = Field(default_factory=list, max_length=100)
    text_layers: list[PortableTextLayer] = Field(default_factory=list, max_length=500)

    @model_serializer(mode="wrap")
    def preserve_legacy_camera_shape(self, handler):
        payload = handler(self)
        if not self.visual_fills:
            payload.pop("visual_fills", None)
        if self.motion_scenes is None:
            payload.pop("motion_scenes", None)
        if not self.camera_pulses:
            payload.pop("camera_pulses", None)
        return payload

    @model_validator(mode="after")
    def validate_asset_manifest(self) -> EditRecipeV2:
        if self.duration > 1800 or any(
            clip.source_start > 1800 for track in self.tracks for clip in track.clips
        ):
            raise ValueError("portable timeline exceeds the device time budget")
        if len({fill.id for fill in self.visual_fills}) != len(self.visual_fills) or any(
            fill.end > self.duration for fill in self.visual_fills
        ):
            raise ValueError("visual fills must be unique and within the timeline")
        if self.visual_fills:
            self.required_capabilities |= {"visualBlocks"}
        if len({pulse.id for pulse in self.camera_pulses}) != len(self.camera_pulses) or any(
            pulse.end > self.duration for pulse in self.camera_pulses
        ):
            raise ValueError("camera pulses must be unique and within the timeline")
        if self.camera_pulses:
            self.required_capabilities |= {"cameraEffects"}
        manifest = {asset.id: asset for asset in self.asset_manifest.assets}
        if set(manifest) != {asset.id for asset in self.assets}:
            raise ValueError("recipe and manifest must name exactly the same assets")
        if self.motion_scenes is not None:
            program = self.motion_scenes
            program.validated_instances(int(self.duration * self.frame_rate))
            font = manifest.get(program.font_asset_id)
            if font is None or font.kind != "library" or font.catalog != "font":
                raise ValueError("motion scenes require an exact library font")
            if not set(program.image_asset_ids.values()).issubset(manifest):
                raise ValueError("motion scene image aliases are missing from the manifest")
            self.required_capabilities |= {"motionScenes"}
        for asset in self.assets:
            expected = manifest[asset.id].fingerprint
            if asset.relative_path != asset.id:
                raise ValueError("V2 asset paths must be opaque asset aliases")
            if (
                asset.fingerprint is None
                or asset.fingerprint.algorithm != "sha256"
                or asset.fingerprint.hex != expected.sha256
                or asset.fingerprint.byte_count != expected.byte_count
            ):
                raise ValueError("recipe asset fingerprint differs from its manifest")
        if len({layer.id for layer in self.text_layers}) != len(self.text_layers):
            raise ValueError("text layer IDs must be unique")
        if any(
            any(run.font_variations for run in layer.runs)
            or layer.animation_phases is not None
            or layer.background is not None
            or layer.effect == "caption-pop"
            or (layer.karaoke is not None and layer.karaoke.active_only is not None)
            for layer in self.text_layers
        ):
            self.required_capabilities = self.required_capabilities | {"authoredText"}
        for layer in self.text_layers:
            if layer.end > self.duration:
                raise ValueError("text layer exceeds the timeline")
            cursor_runs = (
                [line.cursor_run for line in layer.discrete_reveal.lines]
                if layer.discrete_reveal
                else []
            )
            staggered_runs = (
                [glyph.run for glyph in layer.staggered.glyphs] if layer.staggered else []
            )
            for run in layer.runs + cursor_runs + staggered_runs:
                font = manifest.get(run.font_asset_id)
                if font is None or font.kind != "library" or font.catalog != "font":
                    raise ValueError("text requires an exact library font")
        return self
