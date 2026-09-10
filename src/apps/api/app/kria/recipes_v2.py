"""Source-bound portable recipe. Additional rendering lanes remain gated work."""

from __future__ import annotations

from typing import Literal

from pydantic import Field, model_validator

from app.kria.portable_text import PortableTextLayer
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
    text_layers: list[PortableTextLayer] = Field(default_factory=list, max_length=500)

    @model_validator(mode="after")
    def validate_asset_manifest(self) -> EditRecipeV2:
        if self.duration > 1800 or any(
            clip.source_start > 1800 for track in self.tracks for clip in track.clips
        ):
            raise ValueError("portable timeline exceeds the device time budget")
        manifest = {asset.id: asset for asset in self.asset_manifest.assets}
        if set(manifest) != {asset.id for asset in self.assets}:
            raise ValueError("recipe and manifest must name exactly the same assets")
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
        for layer in self.text_layers:
            if layer.end > self.duration:
                raise ValueError("text layer exceeds the timeline")
            for run in layer.runs:
                font = manifest.get(run.font_asset_id)
                if font is None or font.kind != "library" or font.catalog != "font":
                    raise ValueError("text requires an exact library font")
        return self
