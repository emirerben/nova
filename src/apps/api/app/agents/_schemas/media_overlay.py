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

import uuid
from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator

from app.services.media_overlay_preview import nonblank_str

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

    # Server-assigned when the client omits it (mirrors TextElement.id). Clients
    # that round-trip existing cards keep their ids stable; a card sent without
    # an id gets a fresh one on parse instead of failing validation (prod
    # 2026-07-12: required-id cards were silently dropped by coerce, so a PUT
    # without ids 200'd while persisting an EMPTY list).
    id: str = Field(
        default_factory=lambda: uuid.uuid4().hex,
        description="Stable uuid hex, server-assigned when absent.",
    )
    kind: Literal["image", "video"] = Field(
        description="Determines ingest path: image uses -loop 1; video uses tpad clone."
    )
    # GCS object path — validated against _OVERLAY_GCS_PREFIX in the dispatch layer.
    src_gcs_path: str
    # Optional browser-displayable preview object. HEIC/HEIF uploads keep
    # src_gcs_path for the renderer, while preview_gcs_path points at a JPEG.
    preview_gcs_path: str | None = None

    # Display mode (plan 009). "pip" = floating scaled card (today's behavior);
    # "fullscreen" = cover-crop takeover of the whole 1080x1920 frame for the
    # card's window (speech continues — card audio is always dropped).
    # Fullscreen IGNORES position/x_frac/y_frac/scale at render time but keeps
    # them in the dict so toggling back to pip restores the prior layout.
    display_mode: Literal["pip", "fullscreen"] = "pip"

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

    # Trim bounds within the uploaded clip itself (video cards only).
    # None = use the full clip. clip_trim_start_s defaults to 0 when absent.
    clip_trim_start_s: float | None = Field(default=None, ge=0.0)
    clip_trim_end_s: float | None = Field(default=None, ge=0.0)

    # Source clip's total duration in seconds (video cards only).
    # Probed client-side at upload time and persisted so the trim UI can show
    # correct bounds without re-probing after Apply or page reload.
    clip_duration_s: float | None = Field(default=None, ge=0.0)

    # z-order (higher = rendered later = on top). Defaults to list position.
    z: int = Field(default=0, ge=0)

    @field_validator("display_mode", mode="before")
    @classmethod
    def _coerce_display_mode(cls, v: object) -> str:
        """Coerce unknown/missing display_mode to "pip" — version-skew safe.

        A card written by a newer client must never be DROPPED by an older
        server (or vice versa) over this field; worst case it renders as the
        long-standing pip behavior.
        """
        return v if v in ("pip", "fullscreen") else "pip"

    @field_validator("x_frac", "y_frac", mode="before")
    @classmethod
    def _clamp_frac(cls, v: object) -> float:
        """Silently clamp to [0,1]. Client rounding errors shouldn't hard-fail."""
        try:
            return max(0.0, min(1.0, float(v)))  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return _DEFAULT_X_FRAC

    @field_validator("preview_gcs_path", mode="before")
    @classmethod
    def _blank_preview_path_to_none(cls, v: object) -> str | None:
        return nonblank_str(v)

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

    @model_validator(mode="after")
    def _sanitize_trim_pair(self) -> MediaOverlay:
        """Cross-field trim guard (plan 006, decision B).

        An out-of-range trim pair reaches ffmpeg as `trim=start=X:end=Y` with
        an empty result stream and kills the WHOLE variant render — so every
        write path (agent, manual edit, edited suggestion envelope) sanitizes
        here. File philosophy is clamp-don't-fail: repairable values clamp to
        the clip duration; an irreparable pair (end ≤ start) drops to None
        (= play from 0:00), never a hard error.
        """
        start, end, dur = self.clip_trim_start_s, self.clip_trim_end_s, self.clip_duration_s
        if dur is not None:
            if end is not None:
                end = min(end, dur)
            if start is not None and start >= dur:
                start, end = None, None
        if start is not None and end is not None and end - start <= 0.05:
            start, end = None, None
        self.clip_trim_start_s, self.clip_trim_end_s = start, end
        return self

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
        raise ValueError(f"Overlay asset must be under '{_OVERLAY_GCS_PREFIX}', got: {path!r}")


def coerce_media_overlays(
    raw: list | None, *, dropped_indices: list[int] | None = None
) -> list[MediaOverlay] | None:
    """Parse + coerce a raw list into validated MediaOverlay objects.

    Returns None when the list is empty/None so callers can use the clean
    ``if media_overlays:`` idiom. The None return is what preserves the
    byte-identity invariant (the render path never fires when this is falsy).

    Non-raising on individual bad entries: they are dropped with a logged
    warning rather than failing the entire overlay set. This leniency is
    deliberate for agent-output / render paths; user-facing full-replace
    endpoints must NOT rely on it — pass ``dropped_indices`` (an empty list;
    the indices of dropped entries are appended to it) and fail loudly when
    it comes back non-empty (see validate_media_overlays_for_user).
    """
    if not raw:
        return None
    result: list[MediaOverlay] = []
    for idx, item in enumerate(raw):
        if not isinstance(item, dict):
            if dropped_indices is not None:
                dropped_indices.append(idx)
            continue
        try:
            result.append(MediaOverlay.model_validate(item))
        except Exception:  # noqa: BLE001 — bad overlay entry → skip
            if dropped_indices is not None:
                dropped_indices.append(idx)
    return result or None
