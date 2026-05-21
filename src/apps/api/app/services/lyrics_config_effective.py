"""Helpers for lyrics_config overrides and persistence."""

from __future__ import annotations

from copy import deepcopy
from typing import Any

from app.services.lyrics_config_validation import validate_lyrics_config_dict

_FLOAT_KEYS = {"pre_roll_s", "post_dwell_s", "next_line_gap_s"}
_INT_KEYS = {
    "fade_in_ms",
    "fade_out_ms",
    "hold_to_next_threshold_ms",
    "outline_px",
    "lines_per_screen",
}


def deep_merge_dict(base: dict | None, override: dict | None) -> dict:
    """Return a recursive merge without mutating either input."""
    merged = deepcopy(base or {})
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = deep_merge_dict(merged[key], value)
        else:
            merged[key] = deepcopy(value)
    return merged


def non_null_model_dict(model: Any) -> dict:
    if model is None:
        return {}
    if hasattr(model, "model_dump"):
        return model.model_dump(exclude_none=True)
    return {k: v for k, v in dict(model).items() if v is not None}


def effective_lyrics_config(
    track_config: dict | None,
    override: dict | None,
) -> dict:
    """Merge an unsaved override over a track's saved lyrics_config."""
    base = (track_config or {}).get("lyrics_config") or {}
    merged = deep_merge_dict(base, override or {})
    validate_lyrics_config_dict(merged)
    return merged


def normalize_lyrics_config(cfg: dict) -> dict:
    """Normalize numeric fields in a persisted lyrics_config response."""
    normalized = deepcopy(cfg)
    for key in _FLOAT_KEYS:
        if key in normalized and normalized[key] is not None:
            normalized[key] = round(float(normalized[key]), 3)
    for key in _INT_KEYS:
        if key in normalized and normalized[key] is not None:
            normalized[key] = int(round(float(normalized[key])))
    return normalized
