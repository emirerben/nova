"""Shared prompt-data sanitization for untrusted creator and media text."""

from __future__ import annotations

import re

_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_ROLE_MARKERS = re.compile(r"(?im)^\s*(system|assistant|user|tool|developer)\s*[:>]\s*")
_FENCE = re.compile(r"```+")


def sanitize_prompt_text(value: str, *, limit: int) -> str:
    """Normalize prompt data and bound it without treating it as instructions."""

    if limit < 2:
        raise ValueError("prompt text limit must be at least 2")
    cleaned = _CONTROL_CHARS.sub(" ", value or "")
    cleaned = _ROLE_MARKERS.sub("[role-marker-stripped] ", cleaned)
    cleaned = _FENCE.sub("'''", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    if len(cleaned) > limit:
        return cleaned[: limit - 1].rstrip() + "…"
    return cleaned
