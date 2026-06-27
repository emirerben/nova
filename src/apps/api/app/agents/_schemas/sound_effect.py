"""Schema for user-authored sound-effect placements.

A sound-effect placement is a timed audio "pin" applied on top of a finished
plan-item variant's audio track. Placements are stored per-variant in
`Job.assembly_plan["variants"][i]["sound_effects"]`. The feature is additive and
kill-switched (`SOUND_EFFECTS_ENABLED`); when absent the variant bytes are untouched.

GCS path allowlist:
  - Curated glossary effects: `sound-effects/` prefix (persistent, admin-managed).
  - User-uploaded effects: `users/{user_id}/` prefix (persistent, not lifecycle-swept).
  Any other prefix is rejected.

Coordinate convention: `at_s` is the placement point in the absolute timeline of
the final rendered variant (0 = start). Unlike media overlays there is no spatial
component — audio is dimensionless.
"""

from __future__ import annotations

from pydantic import BaseModel, Field, field_validator

# GCS path prefixes that SFX assets must start with.
_SFX_GCS_PREFIXES = ("sound-effects/", "users/")


class SoundEffectPlacement(BaseModel):
    """One sound-effect pin placed at a specific second of the variant.

    All numeric fields are clamped silently on parse so a slightly-out-of-range
    value from a stale client doesn't 422 a render path.
    """

    id: str = Field(description="Stable uuid hex, server-assigned on first write.")
    # Reference to the glossary effect (if from the curated library). When set,
    # `src_gcs_path` is resolved server-side from the SoundEffect row.
    sound_effect_id: str | None = Field(default=None)
    # GCS object path — validated against _SFX_GCS_PREFIXES in the dispatch layer.
    src_gcs_path: str
    # Placement point: the second in the variant timeline where the effect fires.
    at_s: float = Field(default=0.0, ge=0.0)
    # Per-placement volume multiplier. 1.0 = source level. Clamped to [0, 2].
    gain: float = Field(default=1.0, ge=0.0, le=2.0)
    # Trim bounds within the source clip itself (seconds from the clip start).
    # None = use from the beginning / to the end.
    trim_start_s: float | None = Field(default=None, ge=0.0)
    trim_end_s: float | None = Field(default=None, ge=0.0)
    # Source clip's total duration (probed client-side at upload; persisted so the
    # editor can show correct bounds without re-probing after Apply / page reload).
    duration_s: float | None = Field(default=None, ge=0.0)
    # Human-readable label for the admin UI / editor (e.g. "Fah").
    label: str | None = Field(default=None)

    @field_validator("at_s", mode="before")
    @classmethod
    def _clamp_at_s(cls, v: object) -> float:
        """Silently clamp to >= 0. Client rounding errors shouldn't hard-fail."""
        try:
            return max(0.0, float(v))  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return 0.0

    @field_validator("gain", mode="before")
    @classmethod
    def _clamp_gain(cls, v: object) -> float:
        try:
            return max(0.0, min(2.0, float(v)))  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return 1.0


def validate_sfx_gcs_path(path: str) -> None:
    """Raise ValueError if the path is not under a persistent SFX prefix."""
    if not any(path.startswith(p) for p in _SFX_GCS_PREFIXES):
        allowed = ", ".join(f"'{p}'" for p in _SFX_GCS_PREFIXES)
        raise ValueError(f"SFX asset must be under one of {allowed}, got: {path!r}")


def coerce_sound_effects(raw: list | None) -> list[SoundEffectPlacement] | None:
    """Parse + coerce a raw list into validated SoundEffectPlacement objects.

    Returns None when the list is empty/None so callers can use the clean
    ``if sound_effects:`` idiom. The None return preserves the byte-identity
    invariant (the render path never fires when this is falsy).

    Non-raising on individual bad entries: they are dropped rather than failing
    the entire placement set.
    """
    if not raw:
        return None
    result: list[SoundEffectPlacement] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        try:
            result.append(SoundEffectPlacement.model_validate(item))
        except Exception:  # noqa: BLE001 — bad entry → skip
            pass
    return result or None
