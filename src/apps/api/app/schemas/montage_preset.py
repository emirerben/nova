"""Shared montage-preset vocabulary for plan-item montage renders."""

from __future__ import annotations

from typing import Literal, get_args

MontagePreset = Literal["classic", "masonry"]

DEFAULT_MONTAGE_PRESET: MontagePreset = "classic"
MASONRY_MONTAGE_PRESET: MontagePreset = "masonry"

MONTAGE_PRESETS: tuple[str, ...] = get_args(MontagePreset)


def coerce_montage_preset(value: object) -> MontagePreset:
    """Normalize arbitrary input to a known montage preset."""
    if isinstance(value, str):
        normalized = value.strip().lower().replace("-", "_").replace(" ", "_")
        if normalized in MONTAGE_PRESETS:
            return normalized  # type: ignore[return-value]
    return DEFAULT_MONTAGE_PRESET
