"""Internal, server-validated text program persisted with a creation snapshot."""

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.services.editor_limits import MAX_EDITOR_OPS


class TextCompositionProgram(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: Literal[1] = 1
    base_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    operations: list[dict[str, Any]] = Field(default_factory=list, max_length=MAX_EDITOR_OPS)
