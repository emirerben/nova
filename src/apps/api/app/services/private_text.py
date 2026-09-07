"""Small, shared sanitization helpers for creator-authored private text.

Creator direction is used in prompts and durable memory.  Keep format
characters out of every boundary so a hidden bidi/zero-width character cannot
change what an operator, reviewer, or downstream classifier sees.
"""

from __future__ import annotations

import unicodedata


def normalize_private_text(value: str) -> str:
    """Normalize text while removing all Unicode format controls.

    ``Cf`` includes zero-width spaces, BOM/ZWNBSP, bidi embeddings and
    isolates, as well as newer format controls not covered by a static regex.
    C0/C1 controls are converted to spaces to preserve word boundaries;
    format controls are removed entirely because they are not visible text.
    """

    normalized = unicodedata.normalize("NFKC", value)
    safe: list[str] = []
    for char in normalized:
        category = unicodedata.category(char)
        if category == "Cf":
            continue
        if category == "Cc":
            safe.append(" ")
            continue
        safe.append(char)
    return "".join(safe)


__all__ = ["normalize_private_text"]
