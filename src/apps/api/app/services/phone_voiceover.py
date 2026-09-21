"""Resolve a plan item's voiceover into a pinned, hashed phone render asset.

Mirrors `app.services.render_library.inspect_library_asset`'s pin-then-hash
pattern: the worker's compile step downloads the CURRENT bytes at the CURRENT
generation and hashes them, and `app.routes.device_render`'s grant route
independently re-derives the same fingerprint before signing a download URL
for the device -- they must agree exactly, or the grant refuses (409)
instead of handing the device different bytes than the recipe pinned.

Unlike `LibraryRenderAsset`'s shared, published catalog, a voiceover is
private creator media addressed by its owning `PlanItem`, so callers of
`inspect_voiceover_asset` are responsible for the ownership/ path checks a
shared catalog lookup does not need (see `_resolve_phone_voiceover_bed` in
`app.tasks.generative_build` and `_voiceover_download_url` in
`app.routes.device_render`).
"""

from __future__ import annotations

import hashlib
import tempfile
from pathlib import Path

from app import storage
from app.kria.render_assets import RenderFingerprint, VoiceoverRenderAsset

# Mirrors `app.routes.plan_items._MAX_VOICEOVER_BYTES` (the upload-time cap).
# Duplicated rather than imported to keep this service free of a route-layer
# dependency; keep the two in lockstep if the upload cap ever changes.
MAX_VOICEOVER_BYTES = 200 * 1024 * 1024


def inspect_voiceover_asset(path: str, *, asset_id: str, plan_item_id: str) -> VoiceoverRenderAsset:
    """Pin the generation before hashing; a replacement recording cannot enter the receipt."""
    metadata = storage.object_metadata(path)
    if not metadata.generation or not 0 < metadata.size <= MAX_VOICEOVER_BYTES:
        raise ValueError("invalid voiceover asset size or generation")
    with tempfile.TemporaryDirectory(prefix="kria_voiceover_") as directory:
        local = Path(directory) / "asset"
        storage.download_generation_to_file(path, str(local), generation=str(metadata.generation))
        if local.stat().st_size != metadata.size:
            raise ValueError("voiceover asset size changed")
        with local.open("rb") as source:
            digest = hashlib.file_digest(source, "sha256").hexdigest()
    return VoiceoverRenderAsset(
        id=asset_id,
        plan_item_id=plan_item_id,
        generation=str(metadata.generation),
        fingerprint=RenderFingerprint(sha256=digest, byte_count=metadata.size),
    )
