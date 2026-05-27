"""Shared lyrics_config dict validator + per-template resolution helper.

Used by the music-track update endpoint (where lyrics_config is nested
inside `track_config`) and by the template lyrics-config PATCH endpoint
(where it sits at the top level). Keeping the allow-list in one place
means both endpoints stay in lock-step when a new animation style or
position lands.

Caller-side semantics:
- ``None`` is rejected by ``validate_lyrics_config_dict`` — callers that
  allow ``None`` (e.g. "reset to inherit" on the template endpoint) must
  guard with ``if cfg is not None`` before invoking the validator.
"""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

LYRICS_STYLES = {"karaoke", "per-word-pop", "line"}
LYRICS_POSITIONS = {
    "top",
    "bottom",
    "center",
    "center-above",
    "center-below",
    "center-label",
}
LYRICS_FONT_STYLES = {"display", "sans", "serif", "serif_italic", "script"}
LYRICS_TEXT_SIZES = {"small", "medium", "large", "xlarge", "xxlarge", "jumbo"}
LYRICS_CONFIG_KEYS = {
    "enabled",
    "style",
    "position",
    "text_color",
    "highlight_color",
    "font_style",
    "text_size",
    "outline_px",
    "lines_per_screen",
    "pre_roll_s",
    "post_dwell_s",
    "next_line_gap_s",
    "max_overlap_s",
    "fade_in_s",
    "fade_out_s",
    "fade_in_ms",
    "fade_out_ms",
    "hold_to_next_threshold_ms",
    "font_family",
}

LINE_ONLY_KEYS = frozenset(
    {
        "pre_roll_s",
        "post_dwell_s",
        "next_line_gap_s",
        "max_overlap_s",
        "fade_in_s",
        "fade_out_s",
        "fade_in_ms",
        "fade_out_ms",
        "hold_to_next_threshold_ms",
    }
)
# Backwards-compat alias for callers that imported the private name. New
# callers (admin_music.py, future style routes) should import LINE_ONLY_KEYS.
_LINE_ONLY_KEYS = LINE_ONLY_KEYS

_FONT_REGISTRY_PATH = (
    Path(__file__).resolve().parents[2] / "assets" / "fonts" / "font-registry.json"
)
try:
    _FONT_REGISTRY: dict[str, Any] = json.loads(_FONT_REGISTRY_PATH.read_text())
except Exception:
    _FONT_REGISTRY = {"fonts": {}}


def is_valid_hex_color(s: object) -> bool:
    """Return True iff ``s`` is a ``#RRGGBB`` 7-char hex string."""
    if not isinstance(s, str):
        return False
    candidate = s.strip().lstrip("#")
    if len(candidate) != 6:
        return False
    try:
        int(candidate, 16)
    except ValueError:
        return False
    return True


def resolve_effective_lyrics_config(
    template_lyrics_config: dict | None,
    track_lyrics_config: dict | None,
) -> dict | None:
    """Pick the lyrics config that wins at render time.

    Resolution rule: template's own override wins **when explicitly set**,
    including the empty dict ``{}`` (which carries the meaningful "lyrics
    explicitly off" state). Only ``None`` on the template falls back to
    the linked track.

    Critically, do NOT use ``template_lyrics_config or track_lyrics_config``
    here — Python's ``or`` treats ``{}`` as falsy, so a user's explicit
    "off" would silently fall back to the track's "on" config and lyrics
    would render against the user's intent.
    """
    if template_lyrics_config is not None:
        return template_lyrics_config
    return track_lyrics_config


def validate_lyrics_config_dict(cfg: object) -> None:
    """Raise ValueError if ``cfg`` isn't a valid lyrics_config payload.

    Validates the same set of fields whether the dict came from a track
    update or a template lyrics-config PATCH. Does not coerce — callers
    persist exactly what they pass in.
    """
    if not isinstance(cfg, dict):
        raise ValueError("lyrics_config must be an object")
    unknown = set(cfg) - LYRICS_CONFIG_KEYS
    if unknown:
        raise ValueError(f"lyrics_config contains unknown key(s): {sorted(unknown)}")
    style = cfg.get("style")
    if style != "line":
        misplaced = sorted(k for k in _LINE_ONLY_KEYS if k in cfg)
        if misplaced:
            raise ValueError(
                f"lyrics_config keys {misplaced} are only valid for style='line'; "
                f"got style={style!r} (absent style defaults to 'karaoke' downstream)"
            )
    if "enabled" in cfg and not isinstance(cfg["enabled"], bool):
        raise ValueError("lyrics_config.enabled must be a boolean")
    if style is not None and style not in LYRICS_STYLES:
        raise ValueError(f"lyrics_config.style must be one of {sorted(LYRICS_STYLES)}")
    if "position" in cfg and cfg["position"] not in LYRICS_POSITIONS:
        raise ValueError(f"lyrics_config.position must be one of {sorted(LYRICS_POSITIONS)}")
    if "font_style" in cfg and cfg["font_style"] not in LYRICS_FONT_STYLES:
        raise ValueError(f"lyrics_config.font_style must be one of {sorted(LYRICS_FONT_STYLES)}")
    if "text_size" in cfg and cfg["text_size"] not in LYRICS_TEXT_SIZES:
        raise ValueError(f"lyrics_config.text_size must be one of {sorted(LYRICS_TEXT_SIZES)}")
    if "font_family" in cfg and cfg["font_family"] is not None:
        font_family = cfg["font_family"]
        if not isinstance(font_family, str) or font_family not in _FONT_REGISTRY.get("fonts", {}):
            raise ValueError("lyrics_config.font_family must be a known font registry key")
    for hex_key in ("text_color", "highlight_color"):
        v = cfg.get(hex_key)
        if v is not None and not is_valid_hex_color(v):
            raise ValueError(f"lyrics_config.{hex_key} must be a #RRGGBB hex string")
    _validate_number(cfg, "outline_px", min_value=0, max_value=20, integer=True)
    _validate_number(cfg, "lines_per_screen", min_value=1, max_value=4, integer=True)
    _validate_number(cfg, "pre_roll_s", min_value=0.0, max_value=2.0)
    _validate_number(cfg, "post_dwell_s", min_value=0.0, max_value=5.0)
    _validate_number(cfg, "next_line_gap_s", min_value=0.0, max_value=2.0)
    _validate_number(cfg, "max_overlap_s", min_value=0.0, max_value=2.0)
    _validate_number(cfg, "fade_in_s", min_value=0.0, max_value=2.0)
    _validate_number(cfg, "fade_out_s", min_value=0.0, max_value=2.0)
    _validate_number(cfg, "fade_in_ms", min_value=0, max_value=2000, integer=True)
    _validate_number(cfg, "fade_out_ms", min_value=0, max_value=2000, integer=True)
    _validate_number(cfg, "hold_to_next_threshold_ms", min_value=0, max_value=5000, integer=True)


def _validate_number(
    cfg: dict,
    key: str,
    *,
    min_value: float,
    max_value: float,
    integer: bool = False,
) -> None:
    if key not in cfg or cfg[key] is None:
        return
    value = cfg[key]
    if integer:
        if not isinstance(value, int) or isinstance(value, bool):
            raise ValueError(f"lyrics_config.{key} must be an integer")
        number = float(value)
    elif not isinstance(value, (int, float)) or isinstance(value, bool):
        raise ValueError(f"lyrics_config.{key} must be a number")
    else:
        number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"lyrics_config.{key} must be a finite number")
    if not (min_value <= number <= max_value):
        raise ValueError(f"lyrics_config.{key} must be between {min_value} and {max_value}")
