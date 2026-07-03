"""OverlaySuggestion — the AI-suggested placement envelope (plans/005, decision 5A).

A suggestion is "a MediaOverlay that isn't accepted yet, plus why". The envelope
EMBEDS the existing `MediaOverlay` (and optional `SoundEffectPlacement`) dumps
verbatim — one validator set; accepting = unwrap + copy through the existing
validated dispatch. No parallel field copies (renderer-parity drift class).

Persisted at `variants[i]["overlay_suggestions"]` alongside:
  variants[i]["overlay_suggest_status"]: "matching" | "ready" | "zero" | "failed"
  variants[i]["overlay_suggest_hash"]:   transcript hash the set was matched against
  variants[i]["overlay_suggest_wishlist"]: list[str] — unmatched-moment hints
"""

from __future__ import annotations

import uuid

from pydantic import BaseModel, Field, field_validator

from app.agents._schemas.media_overlay import MediaOverlay
from app.agents._schemas.sound_effect import SoundEffectPlacement

CONFIDENCE_TIERS = ("confident", "likely")
_MAX_REASON_LEN = 200
_MAX_ANCHOR_LEN = 120


class OverlaySuggestion(BaseModel):
    """One suggested placement: envelope {why} + embedded {what}."""

    id: str = Field(default_factory=lambda: uuid.uuid4().hex)
    asset_id: str
    # Two-tier signal (decision 10A): drives copy hedging only, never a gate.
    confidence_tier: str = "likely"
    # Transcript-grounded reason ("You say 'it builds a payload' — this diagram
    # shows it."). Shown verbatim in the rail — plain text only.
    reason: str = ""
    # The matched spoken words, for anchoring/debug ("it builds a payload").
    transcript_anchor: str | None = None
    # Embedded existing models (dumps). Accept = copy `overlay` into
    # media_overlays and `sfx` into sound_effects — zero translation.
    overlay: dict
    sfx: dict | None = None

    @field_validator("confidence_tier")
    @classmethod
    def _tier(cls, v: str) -> str:
        return v if v in CONFIDENCE_TIERS else "likely"

    @field_validator("reason")
    @classmethod
    def _reason(cls, v: str) -> str:
        return (v or "").strip()[:_MAX_REASON_LEN]

    @field_validator("transcript_anchor")
    @classmethod
    def _anchor(cls, v: str | None) -> str | None:
        return (v or "").strip()[:_MAX_ANCHOR_LEN] or None

    @field_validator("overlay")
    @classmethod
    def _overlay_valid(cls, v: dict) -> dict:
        # Validate through the REAL model so clamps stay single-sourced (5A).
        return MediaOverlay.model_validate(v).model_dump()

    @field_validator("sfx")
    @classmethod
    def _sfx_valid(cls, v: dict | None) -> dict | None:
        if v is None:
            return None
        return SoundEffectPlacement.model_validate(v).model_dump()


def coerce_overlay_suggestions(raw: list[dict] | None) -> list[OverlaySuggestion]:
    """Best-effort list coercion: invalid items are dropped (decision 6A —
    per-item degradation, never fail the whole set)."""
    out: list[OverlaySuggestion] = []
    for item in raw or []:
        try:
            out.append(OverlaySuggestion.model_validate(item))
        except Exception:  # noqa: BLE001 — per-item drop by design
            continue
    return out
