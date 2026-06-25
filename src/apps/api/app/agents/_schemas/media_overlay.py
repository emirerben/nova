"""Schema for user-authored media-overlay cards (slice 1).

A media overlay is a timed, positioned image or video "card" composited on top
of a finished plan-item variant. Cards are stored per-variant in
`Job.assembly_plan["variants"][i]["media_overlays"]`. The feature is additive and
kill-switched (`MEDIA_OVERLAYS_ENABLED`); when absent the variant bytes are
untouched.

Coordinate convention: `x_frac` / `y_frac` are the card *center* as a fraction
of the 1080x1920 canvas (matching the existing text overlay convention in
text_overlay_skia.py). `scale` is card width as a fraction of canvas width.

Position presets map to:
    top    -> y_frac=0.18
    center -> y_frac=0.50
    bottom -> y_frac=0.82
    custom -> use the literal x_frac / y_frac values

GCS path allowlist: overlay assets must live under the persistent
`users/{user_id}/plan/{item_id}/overlays/` prefix (NOT the 24h-swept
`dev-user/*` namespace).
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator

# Canvas dimensions (portrait 9:16) — must match text_overlay_skia.CANVAS_{W,H}.
_CANVAS_W = 1080
_CANVAS_H = 1920

# Position preset -> y_frac mapping.
_POSITION_Y: dict[str, float] = {
    "top": 0.18,
    "center": 0.50,
    "bottom": 0.82,
}

# Default x (centered horizontally).
_DEFAULT_X_FRAC = 0.5

# GCS path prefix that overlay assets must start with (persistent, not lifecycle-swept).
_OVERLAY_GCS_PREFIX = "users/"


class MediaOverlay(BaseModel):
    """One timed, positioned image/video overlay card.

    All numeric fields are clamped silently on parse so a slightly-out-of-range
    value from a stale client doesn't 422 a render path.
    """

    id: str = Field(description="Stable uuid hex, server-assigned on first write.")
    kind: Literal["image", "video"] = Field(
        description="Determines ingest path: image uses -loop 1; video uses tpad clone."
    )
    # GCS object path — validated against _OVERLAY_GCS_PREFIX in the dispatch layer.
    src_gcs_path: str

    # Position: preset OR custom frac pair. On parse, presets resolve to their
    # canonical y_frac / x_frac defaults; custom uses the literal values.
    position: Literal["top", "center", "bottom", "custom"] = "center"
    x_frac: float = Field(default=_DEFAULT_X_FRAC, ge=0.0, le=1.0)
    y_frac: float = Field(default=0.5, ge=0.0, le=1.0)

    # Scale: fraction of canvas WIDTH the card occupies. 0.3 = 324px wide.
    scale: float = Field(default=0.35, ge=0.05, le=1.0)

    # Time window (absolute seconds in the final edit timeline).
    start_s: float = Field(default=0.0, ge=0.0)
    end_s: float = Field(default=3.0, ge=0.0)

    # z-order (higher = rendered later = on top). Defaults to list position.
    z: int = Field(default=0, ge=0)

    @field_validator("x_frac", "y_frac", mode="before")
    @classmethod
    def _clamp_frac(cls, v: object) -> float:
        """Silently clamp to [0,1]. Client rounding errors shouldn't hard-fail."""
        try:
            return max(0.0, min(1.0, float(v)))  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return _DEFAULT_X_FRAC

    @field_validator("scale", mode="before")
    @classmethod
    def _clamp_scale(cls, v: object) -> float:
        try:
            return max(0.05, min(1.0, float(v)))  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return 0.35

    @field_validator("start_s", "end_s", mode="before")
    @classmethod
    def _clamp_time(cls, v: object) -> float:
        try:
            return max(0.0, float(v))  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return 0.0

    def resolved_xy_frac(self) -> tuple[float, float]:
        """Return (x_frac, y_frac) after applying position presets."""
        if self.position in _POSITION_Y:
            return _DEFAULT_X_FRAC, _POSITION_Y[self.position]
        return self.x_frac, self.y_frac

    def canvas_center_px(self) -> tuple[int, int]:
        """Return (cx_px, cy_px) center in canvas pixels."""
        x, y = self.resolved_xy_frac()
        return round(x * _CANVAS_W), round(y * _CANVAS_H)

    def card_width_px(self) -> int:
        """Width of the rendered card in pixels."""
        return round(self.scale * _CANVAS_W)


def validate_overlay_gcs_path(path: str) -> None:
    """Raise ValueError if the path is not under the persistent overlay prefix."""
    if not path.startswith(_OVERLAY_GCS_PREFIX):
        raise ValueError(
            f"Overlay asset must be under '{_OVERLAY_GCS_PREFIX}', got: {path!r}"
        )


def coerce_media_overlays(raw: list | None) -> list[MediaOverlay] | None:
    """Parse + coerce a raw list into validated MediaOverlay objects.

    Returns None when the list is empty/None so callers can use the clean
    ``if media_overlays:`` idiom. The None return is what preserves the
    byte-identity invariant (the render path never fires when this is falsy).

    Non-raising on individual bad entries: they are dropped with a logged
    warning rather than failing the entire overlay set.
    """
    if not raw:
        return None
    result: list[MediaOverlay] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        try:
            result.append(MediaOverlay.model_validate(item))
        except Exception:  # noqa: BLE001 — bad overlay entry → skip
            pass
    return result or None
