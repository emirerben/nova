"""Immutable upload provenance for local originals and cloud analysis proxies."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

PROXY_MEDIA_PREFIX = "analysis-proxy-"


class OriginalMediaDescriptor(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    byte_count: int = Field(gt=0, le=16 * 1024**3)
    duration_s: float = Field(gt=0, le=1800)
    width: int = Field(gt=0, le=32768)
    height: int = Field(gt=0, le=32768)
    orientation_degrees: Literal[0, 90, 180, 270] = 0
    has_audio: bool


class AnalysisProxyDescriptor(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    original: OriginalMediaDescriptor
    # V1 proxies cover the complete original with orientation baked into pixels.
    # Analysis timestamps are original timestamps; no trim or retiming is allowed.
    timing_version: Literal[1] = 1
    duration_s: float = Field(gt=0, le=1800)
    width: int = Field(gt=0, le=640)
    height: int = Field(gt=0, le=640)
    frame_rate: float = Field(ge=1, le=30)
    orientation_degrees: Literal[0] = 0

    @model_validator(mode="after")
    def validate_timing(self) -> AnalysisProxyDescriptor:
        if abs(self.duration_s - self.original.duration_s) > 0.1:
            raise ValueError("analysis proxy must preserve the complete original timeline")
        return self

    def verify_registered(self, duration_s: float, has_audio: bool) -> None:
        if abs(duration_s - self.duration_s) > 0.1:
            raise ValueError("uploaded proxy duration differs from its descriptor")
        if has_audio != self.original.has_audio:
            raise ValueError("analysis proxy must preserve source audio for transcription")


class MediaUploadContract(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    purpose: Literal["cloud_render_source", "analysis_proxy"] = "cloud_render_source"
    proxy: AnalysisProxyDescriptor | None = None

    @model_validator(mode="after")
    def validate_purpose(self) -> MediaUploadContract:
        if (self.purpose == "analysis_proxy") != (self.proxy is not None):
            raise ValueError("only analysis proxy uploads require an original binding")
        return self


def is_analysis_proxy_path(path: str) -> bool:
    return path.rsplit("/", 1)[-1].startswith(PROXY_MEDIA_PREFIX)


def require_cloud_source_paths(paths: list[str]) -> None:
    if any(is_analysis_proxy_path(path) for path in paths):
        raise ValueError("analysis proxies cannot be used as cloud render sources")
