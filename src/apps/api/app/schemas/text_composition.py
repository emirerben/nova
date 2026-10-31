"""Internal, server-validated text program persisted with a creation snapshot."""

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.text_style_intent import TitleAnimation
from app.services.editor_limits import MAX_EDITOR_OPS


class TextLookEntry(BaseModel):
    """One entrance default: which rows get it and which edge plays."""

    model_config = ConfigDict(extra="forbid")
    # "all" | "title" | "labels" | "clip:<media id>"
    target: str = Field(min_length=3, max_length=200)
    entrance: TitleAnimation
    # True when the creator named the edge; False = the server's tasteful default.
    named: bool = False


class TextLook(BaseModel):
    """The creator's "with animations" ask, applied to every text row it names (prompt fidelity).

    Applied after the composed operations, only to rows that play no entrance yet, so a row the
    composer or the creator already animated is never overwritten. ``form`` is decided once, at
    compose time, from what the phone can play (`animation_ask.text_entrance_form`), so replay
    never reads settings and the same snapshot always compiles to the same plan.
    """

    model_config = ConfigDict(extra="forbid")
    form: Literal["phases", "effect"]
    entries: list[TextLookEntry] = Field(min_length=1, max_length=40)


class TextCompositionProgram(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: Literal[1] = 1
    base_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    operations: list[dict[str, Any]] = Field(default_factory=list, max_length=MAX_EDITOR_OPS)
    # Server-owned copy, keyed by original bar id; sequence children inherit it.
    copy_constraints: dict[str, str] = Field(
        default_factory=dict, exclude_if=lambda value: not value
    )
    # None is omitted so every earlier program keeps its hash.
    text_look: TextLook | None = Field(default=None, exclude_if=lambda value: value is None)
