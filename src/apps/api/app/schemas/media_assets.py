"""Additive native library API contracts; source hashes never stand for originals."""

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.kria.media_sources import MediaUploadContract


class MediaAssetUploadBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    client_upload_id: str = Field(min_length=1, max_length=160, pattern=r"^[A-Za-z0-9_-]+$")
    filename: str = Field(min_length=1, max_length=255)
    content_type: str = Field(min_length=1, max_length=100)
    file_size_bytes: int = Field(gt=0, le=4 * 1024**3)
    kind: Literal["video", "image", "audio"]
    upload_contract: MediaUploadContract = Field(default_factory=MediaUploadContract)
    repair_asset_id: UUID | None = None


class MediaAssetUploadOut(BaseModel):
    reservation_id: UUID
    object_path: str
    upload_url: str | None
    expires_at: datetime
    upload_contract: MediaUploadContract
    upload_headers: dict[str, str] = Field(default_factory=dict)
    asset_id: UUID | None = None


class MediaAssetFinalizeBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    client_event_id: str = Field(min_length=1, max_length=160)


class MediaAssetSourceOut(BaseModel):
    id: UUID
    role: Literal["original", "analysis_proxy", "thumbnail"]
    status: Literal["available", "missing", "deleting", "deleted"]
    byte_count: int
    content_type: str
    uploaded_sha256: str | None
    generation: str


class MediaAssetUsageOut(BaseModel):
    creation_thread_id: UUID | None = None
    plan_item_id: UUID | None = None
    job_id: UUID | None = None
    source_id: UUID | None = None
    created_at: datetime


class MediaAssetStorageWarning(BaseModel):
    source_id: UUID
    warned_at: datetime
    eligible_at: datetime


class MediaAssetOut(BaseModel):
    id: UUID
    kind: Literal["video", "image", "audio"]
    status: Literal["pending", "processing", "ready", "failed"]
    archived_at: datetime | None
    created_at: datetime
    original_declared_sha256: str | None
    original_byte_count: int | None
    original_availability: Literal["device_only", "cloud_backed"]
    metadata_json: dict[str, Any]
    creator_context: dict[str, Any]
    sources: list[MediaAssetSourceOut]
    analysis: dict[str, Any] | None = None
    usage: list[MediaAssetUsageOut] = Field(default_factory=list)
    storage_warnings: list[MediaAssetStorageWarning] = Field(default_factory=list)


class MediaAssetListOut(BaseModel):
    assets: list[MediaAssetOut]
    next_cursor: str | None
    active_count: int
    limit: int = 500


class MediaAssetAccessOut(BaseModel):
    url: str
    expires_at: datetime
    role: Literal["original", "analysis_proxy", "thumbnail"]
    generation: str
