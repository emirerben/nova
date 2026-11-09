"""KRI-720: keep an older app build from previewing a several-take Talking edit uncut.

A several-take edit cuts each take on its own, and the cut of every take after the first lives in
the variant's `phone_talking_multi_clip.speaker_removed` rows. The editor preview of a build below
`SPEAKER_REMOVED_PROTOCOL` cuts only the first take and plays the others whole, so its captions,
cards and sounds land on the wrong words and a lane edit saved from it carries wrong times.

Every build since protocol 11 plays the finished video instead when the receipt has no
`speaker_media_ids` (`NativePhoneTalkingSource.sourceIndices`), so that is what the HTTP response
carries for such a build. Only the served copy changes; the stored receipt, the pinned recipe and
the Save path (which reads the recipe, so every take keeps its cut) are untouched. The gate reads
the stored cut, never `TALKING_MULTI_TAKE_CLEANUP_ENABLED`: a variant rendered with cuts stays
protected after the flag is turned off.

A caller that sends no protocol header counts as old. Outside an HTTP request (workers, the agent
runtime) nothing is previewed, so nothing changes.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from app.services.client_protocol import (
    SPEAKER_REMOVED_PROTOCOL,
    current_client_protocol,
    in_http_request,
)

RECEIPT_FIELD = "phone_talking_multi_clip"


def _has_extra_take_cut(receipt: dict[str, Any]) -> bool:
    rows = receipt.get("speaker_removed")
    return isinstance(rows, list) and any(
        isinstance(row, dict) and row.get("removed") for row in rows
    )


def caller_previews_every_take_cut() -> bool:
    if not in_http_request():
        return True
    protocol = current_client_protocol()
    return protocol is not None and protocol >= SPEAKER_REMOVED_PROTOCOL


def hide_extra_take_cuts_from_old_builds(variants: Iterable[Any]) -> None:
    """Mutates the served variant copies in place; pass response copies, never stored rows."""
    if caller_previews_every_take_cut():
        return
    for variant in variants:
        receipt = variant.get(RECEIPT_FIELD) if isinstance(variant, dict) else None
        if isinstance(receipt, dict) and _has_extra_take_cut(receipt):
            variant[RECEIPT_FIELD] = {**receipt, "speaker_media_ids": []}
