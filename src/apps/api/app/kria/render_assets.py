"""Immutable assets for portable render programs.

Recipes carry identities, never download URLs or storage paths. Original bytes
resolve on the owning device. Library bytes and creator Visuals-pool photos
require a separately authorized, short-lived download grant for the exact
generation and fingerprint.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SerializerFunctionWrapHandler,
    model_serializer,
    model_validator,
)


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


class VisualRenderAsset(_AssetModel):
    """A photo or video from the job owner's Visuals pool (``PlanItemAsset``),
    pinned to one storage generation. The grant re-checks ownership, kind and
    the generation; the device re-hashes the downloaded bytes against
    ``fingerprint``."""

    kind: Literal["visual"] = "visual"
    id: str = Field(min_length=1, max_length=160, pattern=r"^\S+$")
    visual_id: str = Field(min_length=1, max_length=160, pattern=r"^\S+$")
    generation: str = Field(min_length=1, max_length=160, pattern=r"^\S+$")
    # The device prepares photos and videos differently before compositing.
    media_kind: Literal["image", "video"] = "image"
    fingerprint: RenderFingerprint

    @model_serializer(mode="wrap")
    def _photo_shape(self, handler: SerializerFunctionWrapHandler) -> dict:
        # Photo manifests keep their original wire shape and recipe digests.
        payload = handler(self)
        if self.media_kind == "image":
            payload.pop("media_kind", None)
        return payload


RenderAsset = Annotated[
    OriginalRenderAsset | LibraryRenderAsset | VisualRenderAsset, Field(discriminator="kind")
]


class RenderAssetManifest(_AssetModel):
    version: Literal[1] = 1
    assets: tuple[RenderAsset, ...] = Field(max_length=500)

    @model_validator(mode="after")
    def validate_identities(self) -> RenderAssetManifest:
        if len({asset.id for asset in self.assets}) != len(self.assets):
            raise ValueError("render asset IDs must be unique")
        sources: dict[tuple[str, ...], RenderFingerprint] = {}
        for asset in self.assets:
            if isinstance(asset, OriginalRenderAsset):
                key = ("original", asset.media_id)
            elif isinstance(asset, VisualRenderAsset):
                key = ("visual", asset.visual_id, asset.generation)
            else:
                key = ("library", asset.catalog, asset.catalog_id, asset.generation)
            if key in sources and sources[key] != asset.fingerprint:
                raise ValueError("one asset identity cannot describe different bytes")
            sources[key] = asset.fingerprint
        return self

    def require_references(self, ids: set[str]) -> None:
        if ids - {asset.id for asset in self.assets}:
            raise ValueError("render program references an unknown asset")
