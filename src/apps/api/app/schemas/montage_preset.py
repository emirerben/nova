"""Shared montage-preset vocabulary for plan-item montage renders."""

from __future__ import annotations

from typing import Literal, get_args

MontagePreset = Literal["classic", "masonry", "polaroid_wall"]

DEFAULT_MONTAGE_PRESET: MontagePreset = "classic"
MASONRY_MONTAGE_PRESET: MontagePreset = "masonry"
POLAROID_WALL_MONTAGE_PRESET: MontagePreset = "polaroid_wall"

MONTAGE_PRESETS: tuple[str, ...] = get_args(MontagePreset)
COLLAGE_MONTAGE_PRESETS: frozenset[MontagePreset] = frozenset(
    {MASONRY_MONTAGE_PRESET, POLAROID_WALL_MONTAGE_PRESET}
)


def coerce_montage_preset(value: object) -> MontagePreset:
    """Normalize arbitrary input to a known montage preset."""
    if isinstance(value, str):
        normalized = value.strip().lower().replace("-", "_").replace(" ", "_")
        if normalized in MONTAGE_PRESETS:
            return normalized  # type: ignore[return-value]
    return DEFAULT_MONTAGE_PRESET


def is_collage_montage_preset(value: object) -> bool:
    """True when a preset renders through the collage-style compositor."""
    return coerce_montage_preset(value) in COLLAGE_MONTAGE_PRESETS
