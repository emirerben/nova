"""Phone-rendering contracts and pure revision fences.

Separate from Creator's historical ``native_render`` (the Python renderer).
A device recipe is immutable once approved; upload attempts never authorize edits.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.kria.recipes import EditRecipeV1
from app.kria.recipes_v2 import EditRecipeV2

DeviceRecipe = Annotated[EditRecipeV1 | EditRecipeV2, Field(discriminator="schema_version")]


class _DeviceModel(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class DeviceRenderIdentity(_DeviceModel):
    job_id: uuid.UUID
    variant_id: str = Field(min_length=1, max_length=160)
    recipe_revision: int = Field(ge=1)
    recipe_digest: str = Field(pattern=r"^[0-9a-f]{64}$")


class DeviceRenderRequest(_DeviceModel):
    identity: DeviceRenderIdentity
    recipe: DeviceRecipe

    @model_validator(mode="after")
    def check_digest(self) -> DeviceRenderRequest:
        if recipe_digest(self.recipe) != self.identity.recipe_digest:
            raise ValueError("recipe digest mismatch")
        if not self.recipe.tracks or self.recipe.duration <= 0:
            raise ValueError("device rendering requires a nonempty timeline")
        return self


class DeviceRenderCapabilities(_DeviceModel):
    enabled: bool = False
    recipe_versions: list[int] = Field(default_factory=list)
    verified_features: list[str] = Field(default_factory=list)


class DeviceRenderStatus(_DeviceModel):
    phase: Literal["awaiting_device", "syncing", "published", "needs_attention"]
    request: DeviceRenderRequest
    reason: str | None = None
    # Machine-stable taxonomy for a needs_attention phase — `reason` stays the
    # human-readable detail string. NULL for every pre-P0-1 record and for any
    # non-failure phase; see DeviceRenderFailureBody.reason_code for the enum.
    reason_code: str | None = None
    # Storage upload attempt that won publication; populated only for the
    # terminal published phase.
    published_generation: str | None = None


class DeviceAssetDownloadBody(_DeviceModel):
    identity: DeviceRenderIdentity
    asset_id: str = Field(min_length=1, max_length=160)


class DeviceAssetDownloadOut(_DeviceModel):
    asset_id: str
    download_url: str
    expires_at: datetime


# Brand furniture the phone appends to an export before uploading it.
#
# The phone declares WHICH tail it applied, never how long that tail is. The
# server owns the durations, so a tampered client cannot pad an upload with
# arbitrary trailing footage and still satisfy `_verify_export`'s duration gate
# — the worst it can do is claim the one length we already ship.
BrandTail = Literal["none", "standard"]

# Seconds each declared tail adds on top of the recipe's own duration.
# "standard" is the shipped outro (kria-outro-paper.mp4, 48 frames at 30fps).
# `tests/kria/test_brand_tail_contract.py` probes the checked-in asset and
# fails if this number drifts away from it.
BRAND_TAIL_SECONDS: dict[str, float] = {"none": 0.0, "standard": 1.6}


class DeviceExportReservationBody(_DeviceModel):
    identity: DeviceRenderIdentity
    attempt_id: uuid.UUID
    file_size_bytes: int = Field(gt=0, le=1024 * 1024 * 1024)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    # Defaulted so a phone build that predates branding keeps publishing.
    brand_tail: BrandTail = "none"


class DeviceExportReservationOut(_DeviceModel):
    attempt_id: uuid.UUID
    upload_url: str
    upload_headers: dict[str, str]
    expires_at: datetime


class DeviceExportCompleteBody(_DeviceModel):
    identity: DeviceRenderIdentity
    attempt_id: uuid.UUID


class DeviceExportCompleteOut(_DeviceModel):
    status: Literal["published"] = "published"
    identity: DeviceRenderIdentity


DeviceFailureReasonCode = Literal[
    "export_failed",
    "insufficient_storage",
    "thermal",
    "unsupported_recipe",
    "cancelled_by_user",
    "unknown",
]


class DeviceRenderFailureBody(_DeviceModel):
    """Phone-reported local failure: the device could not produce an export."""

    identity: DeviceRenderIdentity
    reason_code: DeviceFailureReasonCode
    detail: str = Field(default="", max_length=2000)


class DeviceRenderFailureOut(_DeviceModel):
    identity: DeviceRenderIdentity
    phase: Literal["needs_attention"]
    reason_code: DeviceFailureReasonCode


class DeviceRetryBody(_DeviceModel):
    """Identity of the ``needs_attention`` record to re-pin (wrapped like the other bodies)."""

    identity: DeviceRenderIdentity


class DeviceRetryOut(_DeviceModel):
    """A fresh, re-pinned identity (revision + 1 over the same recipe)."""

    identity: DeviceRenderIdentity
    phase: Literal["awaiting_device"]


def recipe_digest(recipe: DeviceRecipe) -> str:
    document = recipe.model_dump(mode="json")
    document["required_capabilities"] = sorted(document["required_capabilities"])
    payload = json.dumps(document, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(payload.encode()).hexdigest()


def make_device_request(
    *, job_id: uuid.UUID, variant_id: str, revision: int, recipe: DeviceRecipe
) -> DeviceRenderRequest:
    return DeviceRenderRequest(
        identity=DeviceRenderIdentity(
            job_id=job_id,
            variant_id=variant_id,
            recipe_revision=revision,
            recipe_digest=recipe_digest(recipe),
        ),
        recipe=recipe,
    )


def require_current_request(
    status: DeviceRenderStatus, identity: DeviceRenderIdentity
) -> DeviceRenderRequest:
    if status.request.identity != identity:
        raise ValueError("device recipe superseded")
    return status.request
