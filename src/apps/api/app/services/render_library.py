"""Resolve only published catalog bytes for portable render manifests."""

from __future__ import annotations

import hashlib
import tempfile
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession

from app import storage
from app.kria.render_assets import LibraryRenderAsset, RenderFingerprint
from app.models import MusicTrack, SoundEffect


def bundled_font_asset(filename: str, *, asset_id: str) -> LibraryRenderAsset:
    """The iOS bundle and cloud renderer share these checked-in font bytes."""
    if (
        not filename
        or "/" in filename
        or "\\" in filename
        or Path(filename).suffix.lower() not in {".ttf", ".otf"}
    ):
        raise ValueError("invalid bundled font")
    directory = Path(__file__).resolve().parents[2] / "assets" / "fonts"
    path = (directory / filename).resolve()
    if path.parent != directory.resolve():
        raise ValueError("invalid bundled font path")
    with path.open("rb") as source:
        digest = hashlib.file_digest(source, "sha256").hexdigest()
    return LibraryRenderAsset(
        id=asset_id,
        catalog="font",
        catalog_id=filename,
        generation=digest,
        fingerprint=RenderFingerprint(sha256=digest, byte_count=path.stat().st_size),
    )


async def catalog_path(db: AsyncSession, catalog: str, catalog_id: str) -> str:
    model = {"music": MusicTrack, "sound_effect": SoundEffect}.get(catalog)
    if model is None:
        raise ValueError("unsupported render catalog")
    row = await db.get(model, catalog_id, populate_existing=True)
    if (
        row is None
        or row.published_at is None
        or row.archived_at is not None
        or getattr(row, "analysis_status" if catalog == "music" else "status") != "ready"
        or not row.audio_gcs_path
    ):
        raise FileNotFoundError("render catalog asset unavailable")
    # Even corrupt catalog rows cannot grant access to creator-owned media.
    prefix = "music" if catalog == "music" else "sound-effects"
    path = row.audio_gcs_path
    if (
        not path.startswith(f"{prefix}/{catalog_id}/")
        or any(part in {"", ".", ".."} for part in path.split("/"))
        or "\\" in path
    ):
        raise ValueError("invalid render catalog path")
    return path


def inspect_library_asset(
    path: str, *, asset_id: str, catalog: str, catalog_id: str
) -> LibraryRenderAsset:
    """Pin the generation before hashing; replacement bytes cannot enter the receipt."""
    metadata = storage.object_metadata(path)
    if not metadata.generation or not 0 < metadata.size <= 256 * 1024**2:
        raise ValueError("invalid library asset size or generation")
    with tempfile.TemporaryDirectory(prefix="kria_library_") as directory:
        local = Path(directory) / "asset"
        storage.download_generation_to_file(path, str(local), generation=str(metadata.generation))
        if local.stat().st_size != metadata.size:
            raise ValueError("library asset size changed")
        with local.open("rb") as source:
            digest = hashlib.file_digest(source, "sha256").hexdigest()
    return LibraryRenderAsset(
        id=asset_id,
        catalog=catalog,
        catalog_id=catalog_id,
        generation=str(metadata.generation),
        fingerprint=RenderFingerprint(sha256=digest, byte_count=metadata.size),
    )
