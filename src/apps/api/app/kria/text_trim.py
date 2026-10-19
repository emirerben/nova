"""Shorten creator or model text for a question the creator reads, without cutting a word."""

from __future__ import annotations

ELLIPSIS = "…"


def trim_at_word(text: str, limit: int) -> str:
    """``text`` unchanged when it fits in ``limit``; else its whole words plus an ellipsis.

    The result is never longer than ``limit`` and never ends mid-word ("…restart the
    kilometer t", KRI-552). One word longer than ``limit`` is cut, since there is no
    word boundary to cut at.
    """
    if len(text) <= limit:
        return text
    room = max(limit - len(ELLIPSIS), 0)
    # The cut falls between words when the first dropped character is whitespace.
    cut = room
    while cut > 0 and not text[cut].isspace():
        cut -= 1
    kept = text[:cut].rstrip() if cut > 0 else text[:room]
    return kept.rstrip(",;:-") + ELLIPSIS
