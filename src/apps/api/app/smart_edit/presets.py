"""Versioned editorial policy for Smart Captions.

The planner emits closed tokens. This module is the only place those tokens
become typography, geometry, density, transition, and audio policy. Preset JSON
is strict and loaded from the repository so a creator-style change is reviewed,
versioned, and covered by golden tests.
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CaptionPolicy(_Strict):
    min_words: int = Field(ge=1, le=7)
    max_words: int = Field(ge=3, le=10)
    max_lines: Literal[1, 2]
    max_chars: int = Field(ge=16, le=80)
    font_family: str = Field(min_length=1, max_length=80)
    font_size_px: int = Field(ge=36, le=96)
    y_frac: float = Field(ge=0.3, le=0.9)
    width_frac: float = Field(ge=0.4, le=0.95)
    color: str
    stroke_color: str
    stroke_width: int = Field(ge=0, le=12)

    @model_validator(mode="after")
    def _word_range(self) -> CaptionPolicy:
        if self.min_words > self.max_words:
            raise ValueError("caption min_words must not exceed max_words")
        return self


class TextStylePolicy(_Strict):
    font_family: str = Field(min_length=1, max_length=80)
    size_px: int = Field(ge=24, le=180)
    x_frac: float = Field(ge=0.0, le=1.0)
    y_frac: float = Field(ge=0.0, le=1.0)
    max_width_frac: float = Field(ge=0.1, le=1.0)
    color: str
    highlight_color: str
    stroke_width: int = Field(ge=0, le=12)
    alignment: Literal["left", "center", "right"]
    effect: Literal["static", "pop-in", "typewriter", "fade-in"]
    duration_s: float = Field(gt=0.1, le=8.0)


class VisualZonePolicy(_Strict):
    x_frac: float = Field(ge=0.0, le=1.0)
    y_frac: float = Field(ge=0.0, le=1.0)
    scale: float = Field(ge=0.05, le=1.0)
    z: int = Field(ge=0, le=200)
    display_mode: Literal["pip", "fullscreen"] = "pip"


class VisualAliasPolicy(_Strict):
    """Creator-style vocabulary that grounds a pool asset to spoken names.

    Asset analysis describes what pixels contain (for example, "robot wedding
    couple"), while a creator may refer to that character by a proper name
    ("Çeliknaz").  Keeping that durable vocabulary in the versioned preset
    avoids hard-coding one video's transcript in the planner.
    """

    asset_terms: list[str] = Field(min_length=1, max_length=8)
    transcript_terms: list[str] = Field(min_length=1, max_length=12)


class SfxRolePolicy(_Strict):
    asset_ids: list[str] = Field(default_factory=list, max_length=12)
    role_tags: list[str] = Field(min_length=1, max_length=8)
    name_fallback_tokens: list[str] = Field(default_factory=list, max_length=8)
    gain: float = Field(ge=0.0, le=1.0)
    min_spacing_ms: int = Field(ge=0, le=5000)
    max_vocal_probability: float = Field(ge=0.0, le=1.0)


class BoundaryPolicy(_Strict):
    effect: Literal["horizontal_motion_blur"]
    duration_ms: int = Field(ge=120, le=1200)
    blur_sigma: float = Field(ge=1.0, le=80.0)
    intensity: float = Field(ge=0.0, le=1.0)


class DensityPolicy(_Strict):
    hook_window_s: float = Field(gt=0.0, le=15.0)
    hook_max_visuals: int = Field(ge=1, le=6)
    hook_group_hold_s: float = Field(ge=0.5, le=5.0)
    normal_visual_duration_s: float = Field(ge=1.0, le=8.0)
    min_normal_gap_s: float = Field(ge=0.0, le=15.0)
    max_events: int = Field(ge=1, le=120)


class SmartEditPreset(_Strict):
    preset_id: str
    version: str
    caption: CaptionPolicy
    text_styles: dict[str, TextStylePolicy]
    visual_zones: dict[str, VisualZonePolicy]
    hook_zone_sequence: list[str] = Field(min_length=1, max_length=6)
    visual_aliases: list[VisualAliasPolicy] = Field(default_factory=list, max_length=40)
    sfx_roles: dict[str, SfxRolePolicy]
    boundary_effects: dict[str, BoundaryPolicy]
    density: DensityPolicy

    @field_validator("preset_id", "version")
    @classmethod
    def _safe_identifier(cls, value: str) -> str:
        if not value or any(ch not in "abcdefghijklmnopqrstuvwxyz0123456789-_" for ch in value):
            raise ValueError("preset identifiers must use lowercase safe tokens")
        return value

    @model_validator(mode="after")
    def _references_exist(self) -> SmartEditPreset:
        missing_zones = set(self.hook_zone_sequence) - set(self.visual_zones)
        if missing_zones:
            raise ValueError(f"hook zone sequence references unknown zones: {missing_zones}")
        return self


def _normalize_version(preset_id: str, version: str) -> str:
    prefix = f"{preset_id}-"
    return version[len(prefix) :] if version.startswith(prefix) else version


@lru_cache(maxsize=16)
def load_preset(preset_id: str, version: str) -> SmartEditPreset:
    safe_id = preset_id.strip().lower()
    safe_version = _normalize_version(safe_id, version.strip().lower())
    if any(part in {"", ".", ".."} for part in (safe_id, safe_version)):
        raise ValueError("invalid Smart preset identifier")
    path = Path(__file__).with_name("presets") / safe_id / f"{safe_version}.json"
    if not path.is_file():
        raise ValueError(f"unknown Smart preset: {safe_id}/{safe_version}")
    preset = SmartEditPreset.model_validate(json.loads(path.read_text(encoding="utf-8")))
    if preset.preset_id != safe_id or preset.version != safe_version:
        raise ValueError("Smart preset identity does not match its path")
    return preset
