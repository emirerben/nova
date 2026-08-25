"""Safe, display-only source filename metadata helpers."""

from __future__ import annotations

import re

_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")
_ROLE_MARKER = re.compile(r"(?i)^\s*(system|assistant|user|tool|developer)\s*[:>]\s*")
_MAX_SOURCE_FILENAME_LENGTH = 160


def safe_media_basename(value: object) -> str | None:
    """Return a bounded basename suitable for model context and UI display.

    Upload metadata is creator-provided and must never carry a storage path into
    a prompt.  Treat both POSIX and Windows separators as path delimiters,
    remove control characters, and reject dot-directory names.  ``None`` is
    returned for absent or unusable values.
    """

    if not isinstance(value, str):
        return None
    candidate = value.replace("\\", "/").rsplit("/", 1)[-1].strip()
    candidate = _CONTROL_CHARS.sub(" ", candidate)
    candidate = _ROLE_MARKER.sub("[label] ", candidate).replace("```", "'''")
    candidate = " ".join(candidate.split())
    if not candidate or candidate in {".", ".."}:
        return None
    return candidate[:_MAX_SOURCE_FILENAME_LENGTH]


__all__ = ["safe_media_basename"]
