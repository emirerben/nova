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

LYRICS_STYLES = {"karaoke", "per-word-pop", "line"}
LYRICS_POSITIONS = {
    "top",
    "bottom",
    "center",
    "center-above",
    "center-below",
    "center-label",
}


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
    if "style" in cfg and cfg["style"] not in LYRICS_STYLES:
        raise ValueError(f"lyrics_config.style must be one of {sorted(LYRICS_STYLES)}")
    if "position" in cfg and cfg["position"] not in LYRICS_POSITIONS:
        raise ValueError(f"lyrics_config.position must be one of {sorted(LYRICS_POSITIONS)}")
    for hex_key in ("text_color", "highlight_color"):
        v = cfg.get(hex_key)
        if v is not None and not is_valid_hex_color(v):
            raise ValueError(f"lyrics_config.{hex_key} must be a #RRGGBB hex string")
