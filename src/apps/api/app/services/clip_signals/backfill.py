"""Which stored clips still need signals. Pure; the CLI and a later live path share it.

Independent of understanding on purpose: ``kria_clip_understanding._needs_understanding`` skips a
clip that already has its understanding, so a clip understood before a signal existed would never
be revisited by that task. Selection here looks only at the ``signals`` entries.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from app.services.clip_signals.registry import missing_signals


@dataclass(frozen=True)
class Pending:
    media_id: str
    gcs_path: str
    kind: str
    missing: tuple[str, ...]


def rows_needing_signals(clip_assignments: Iterable[object]) -> list[Pending]:
    out: list[Pending] = []
    for row in clip_assignments or ():
        if not isinstance(row, Mapping):
            continue
        media_id, gcs_path = row.get("media_id"), row.get("gcs_path")
        kind = str(row.get("kind") or "video")
        if not media_id or not gcs_path or kind not in {"video", "image"}:
            continue
        analysis: Any = row.get("analysis")
        missing = missing_signals(analysis, kind)
        if missing:
            out.append(Pending(str(media_id), str(gcs_path), kind, tuple(missing)))
    return out
