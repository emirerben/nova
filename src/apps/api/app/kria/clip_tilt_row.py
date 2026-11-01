"""Reading a clip row's ``tilt`` from the wire (KRI-1136), on top of ``clip_tilt.ClipTilt``.

``ClipTilt`` is the one model. This module only decides what a Save or a stored row may
hold: an object with ``rotation_deg`` (a real, finite number, wrapped to ``(-180, 180]``)
and an optional backdrop, nothing else. ``tests/fixtures/clip-tilt-row/v1.json`` pins the
same rules for ``Kria/Core/ClipTilt.swift``.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from typing import Any

from pydantic import ValidationError

from app.kria.clip_tilt import ClipTilt, wrap_degrees


def parse_row_tilt(raw: object) -> ClipTilt | None:
    """The tilt a row value holds, ``None`` for no tilt. Raises ``ValueError`` when malformed."""
    if raw is None:
        return None
    if isinstance(raw, ClipTilt):
        return raw
    if not isinstance(raw, Mapping):
        raise ValueError("tilt must be an object")
    fields = dict(raw)
    if "rotation_deg" in fields:
        degrees = fields["rotation_deg"]
        if isinstance(degrees, bool) or not isinstance(degrees, (int, float)):
            raise ValueError("rotation_deg must be a number")
        if not math.isfinite(degrees):
            raise ValueError("rotation_deg must be finite")
        fields["rotation_deg"] = wrap_degrees(float(degrees))
    try:
        return ClipTilt.model_validate(fields)
    except ValidationError as exc:
        raise ValueError(str(exc)) from exc


def canonical_row_tilt(raw: object) -> dict[str, Any] | None:
    """``raw`` as the stored row value (``ClipTilt.row()``), validated; ``None`` when upright."""
    tilt = parse_row_tilt(raw)
    return tilt.row() if tilt is not None else None


def row_tilt_from_clip(clip: Any) -> dict[str, Any] | None:
    """The row value a recipe clip's ``transform.rotation_degrees`` and ``backdrop`` stand for."""
    return ClipTilt(
        rotation_deg=wrap_degrees(float(clip.transform.rotation_degrees)),
        backdrop=getattr(clip, "backdrop", None),
    ).row()
