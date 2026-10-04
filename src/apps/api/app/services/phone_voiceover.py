"""Resolve a plan item's voiceover into a pinned, hashed phone render asset.

`inspect_voiceover_asset` is called ONCE, at compile time
(`_resolve_phone_voiceover_bed` in `app.tasks.generative_build`): it mirrors
`app.services.render_library.inspect_library_asset`'s pin-then-hash pattern,
downloading the CURRENT bytes at the CURRENT generation and hashing them to
produce the `VoiceoverRenderAsset` baked into the recipe.

Unlike the library-catalog grant (which re-runs `inspect_library_asset` on
every device download to independently re-derive the fingerprint before
signing), the voiceover grant route (`_voiceover_download_url` in
`app.routes.device_render`) does NOT re-hash: it only re-checks that the
job's own `PlanItem` still carries the exact pinned `(path, generation)`,
then signs a URL for that generation directly (mirrors the Visuals-pool
asset grant's ownership pattern, not the library catalog's re-hash pattern).
The DEVICE re-hashes the downloaded bytes against the pinned SHA-256 itself.
Re-hashing server-side on every grant would cost a full download of up to
`MAX_VOICEOVER_BYTES` per poll; trusting the immutable generation (GCS never
lets two different byte sequences share one generation for one object) is
sufficient, same as for `VisualRenderAsset`.

Unlike `LibraryRenderAsset`'s shared, published catalog, a voiceover is
private creator media addressed by its owning `PlanItem`, so callers of
`inspect_voiceover_asset` are responsible for the ownership/path checks a
shared catalog lookup does not need (see `_resolve_phone_voiceover_bed` in
`app.tasks.generative_build` and `_voiceover_download_url` in
`app.routes.device_render`).
"""

from __future__ import annotations

import hashlib
import tempfile
from pathlib import Path

from app import storage
from app.kria.render_assets import RenderFingerprint, SongRenderAsset, VoiceoverRenderAsset

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


# A creator song is audio of a few minutes; the cap only guards the hash download.
MAX_SONG_BYTES = 200 * 1024 * 1024


def inspect_song_asset(
    path: str,
    *,
    asset_id: str,
    plan_item_id: str,
    expected_generation: str | int | None = None,
) -> SongRenderAsset:
    """Pin the song's generation before hashing (KRI-374).

    The clone of ``inspect_voiceover_asset`` for a creator-uploaded song, minus the
    cleaned-narration branch (a song has no derivative). ``expected_generation`` is
    the generation the plan was approved against: an object whose live generation
    differs was replaced since approval, so the receipt is refused rather than
    pinned to the new bytes.
    """
    metadata = storage.object_metadata(path)
    if not metadata.generation or not 0 < metadata.size <= MAX_SONG_BYTES:
        raise ValueError("invalid song asset size or generation")
    if expected_generation is not None and str(metadata.generation) != str(expected_generation):
        raise ValueError("song asset was replaced")
    with tempfile.TemporaryDirectory(prefix="kria_song_") as directory:
        local = Path(directory) / "asset"
        storage.download_generation_to_file(path, str(local), generation=str(metadata.generation))
        if local.stat().st_size != metadata.size:
            raise ValueError("song asset size changed")
        with local.open("rb") as source:
            digest = hashlib.file_digest(source, "sha256").hexdigest()
    return SongRenderAsset(
        id=asset_id,
        plan_item_id=plan_item_id,
        generation=str(metadata.generation),
        fingerprint=RenderFingerprint(sha256=digest, byte_count=metadata.size),
    )
