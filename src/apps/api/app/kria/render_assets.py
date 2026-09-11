"""Immutable assets for portable render programs.

Recipes carry identities, never download URLs or storage paths. Original bytes
resolve on the owning device. Library bytes require a separately authorized,
short-lived download grant for the exact catalog generation and fingerprint.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class _AssetModel(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False, frozen=True)


class RenderFingerprint(_AssetModel):
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    byte_count: int = Field(gt=0, le=16 * 1024**3)


class OriginalRenderAsset(_AssetModel):
    kind: Literal["original"] = "original"
    id: str = Field(min_length=1, max_length=160, pattern=r"^\S+$")
    media_id: str = Field(min_length=1, max_length=160, pattern=r"^\S+$")
    fingerprint: RenderFingerprint


class LibraryRenderAsset(_AssetModel):
    kind: Literal["library"] = "library"
    id: str = Field(min_length=1, max_length=160, pattern=r"^\S+$")
    catalog: Literal["music", "sound_effect", "font", "overlay"]
    catalog_id: str = Field(min_length=1, max_length=160, pattern=r"^\S+$")
    generation: str = Field(min_length=1, max_length=160, pattern=r"^\S+$")
    fingerprint: RenderFingerprint


RenderAsset = Annotated[OriginalRenderAsset | LibraryRenderAsset, Field(discriminator="kind")]


class RenderAssetManifest(_AssetModel):
    version: Literal[1] = 1
    assets: tuple[RenderAsset, ...] = Field(max_length=500)

    @model_validator(mode="after")
    def validate_identities(self) -> RenderAssetManifest:
        if len({asset.id for asset in self.assets}) != len(self.assets):
            raise ValueError("render asset IDs must be unique")
        sources: dict[tuple[str, ...], RenderFingerprint] = {}
        for asset in self.assets:
            key = (
                ("original", asset.media_id)
                if isinstance(asset, OriginalRenderAsset)
                else ("library", asset.catalog, asset.catalog_id, asset.generation)
            )
            if key in sources and sources[key] != asset.fingerprint:
                raise ValueError("one asset identity cannot describe different bytes")
            sources[key] = asset.fingerprint
        return self

    def require_references(self, ids: set[str]) -> None:
        if ids - {asset.id for asset in self.assets}:
            raise ValueError("render program references an unknown asset")
