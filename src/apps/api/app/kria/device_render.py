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


class DeviceExportReservationBody(_DeviceModel):
    identity: DeviceRenderIdentity
    attempt_id: uuid.UUID
    file_size_bytes: int = Field(gt=0, le=1024 * 1024 * 1024)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


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
