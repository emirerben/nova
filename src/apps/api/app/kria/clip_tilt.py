"""The tilt a clip row carries: one angle and what shows in the corners it uncovers.

Row shape (``clips[i]["tilt"]``), the same as ``TimelineClip.transform.rotation_degrees`` and
``TimelineClip.backdrop`` on the recipe (``app/kria/recipes.py``):

    {"rotation_deg": 10.0, "backdrop": {"kind": "blur"}}

``rotation_deg`` is clockwise on screen, -180..180, 0 upright. ``backdrop`` is a
``ClipBackdrop`` (blank | color | blur | media) and is absent when blank. A ``media`` backdrop
is ``{"kind": "media", "asset_id": "<Visuals-pool visual id>"}``: the same value on the row and
in the recipe (the recipe's manifest declares the pool visual that id names). A row with neither a
turn nor a backdrop carries no tilt at all, so untouched rows stay byte-identical.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from app.kria.recipes import ClipBackdrop

HALF_TURN_DEG = 180.0


def wrap_degrees(degrees: float) -> float:
    """``degrees`` as the same turn in (-180, 180], so 270 is -90 and 190 is -170."""
    turned = degrees % 360.0
    return round(turned - 360.0 if turned > HALF_TURN_DEG else turned, 2)


class ClipTilt(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    rotation_deg: float = Field(default=0.0, ge=-HALF_TURN_DEG, le=HALF_TURN_DEG)
    backdrop: ClipBackdrop | None = None

    @model_validator(mode="after")
    def _blank_is_absent(self) -> ClipTilt:
        if self.backdrop is not None and self.backdrop.kind == "blank":
            self.backdrop = None
        return self

    @classmethod
    def from_row(cls, raw: object) -> ClipTilt:
        """The tilt a stored row carries; anything unreadable is upright."""
        if not isinstance(raw, dict):
            return cls()
        try:
            return cls.model_validate(raw)
        except ValidationError:
            return cls()

    def row(self) -> dict[str, Any] | None:
        """The canonical row value, or None when the clip is upright on a blank backdrop."""
        if self.rotation_deg == 0 and self.backdrop is None:
            return None
        out: dict[str, Any] = {"rotation_deg": float(self.rotation_deg)}
        if self.backdrop is not None:
            out["backdrop"] = self.backdrop.model_dump(mode="json")
        return out
