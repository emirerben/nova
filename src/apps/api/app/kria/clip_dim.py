"""How dark a clip's picture is drawn: the one strength a clip row carries in ``clips[i]["dim"]``.

The strength is the share of the way to black, 0.01..1 in hundredths (one per whole percent):
the phone draws every pixel of the clip's picture at ``1 - dim`` of its encoded value, under
text, captions, stickers and drawings. ``1`` is black. A value below 0.01 is no dim; anything
else rounds half up to hundredths. No dim is absent from the row and from the recipe
(``TimelineClip.dim``, the same number), so an undimmed clip stays byte-identical.
``tests/fixtures/clip-dim-row/v1.json`` pins these rules for ``Kria/Core/ClipDim.swift``.
"""

from __future__ import annotations

import math


def parse_row_dim(raw: object) -> float | None:
    """The strength a row value holds, ``None`` for no dim. Raises ``ValueError`` when malformed."""
    if raw is None:
        return None
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        raise ValueError("dim must be a number")
    value = float(raw)
    if not math.isfinite(value):
        raise ValueError("dim must be finite")
    if value < 0:
        raise ValueError("dim must not be negative")
    if value < 0.01:
        return None
    # The same IEEE steps as ClipDim.swift, so both sides land on the same hundredth.
    strength = math.floor(value * 100 + 0.5) / 100
    if strength > 1:
        raise ValueError("dim must be at most 1")
    return strength


def row_dim(row: object) -> float | None:
    """The dim a stored row (a mapping, or anything with a ``dim`` attribute) carries."""
    raw = row.get("dim") if isinstance(row, dict) else getattr(row, "dim", None)
    return parse_row_dim(raw)


def stored_row_dim(raw: object) -> float | None:
    """A stored row's dim for reading back; an unreadable value reads as no dim."""
    try:
        return parse_row_dim(raw)
    except ValueError:
        return None
