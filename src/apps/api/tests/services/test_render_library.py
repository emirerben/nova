import hashlib
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.services import render_library as library


@pytest.mark.asyncio
@pytest.mark.parametrize("catalog,prefix", [("music", "music"), ("sound_effect", "sound-effects")])
async def test_only_published_ready_catalog_audio(catalog, prefix):
    row = SimpleNamespace(
        published_at=1,
        archived_at=None,
        analysis_status="ready",
        status="ready",
        audio_gcs_path=f"{prefix}/track/audio.mp3",
    )
    db = SimpleNamespace(get=AsyncMock(return_value=row))
    assert await library.catalog_path(db, catalog, "track") == row.audio_gcs_path
    for field, value in [
        ("published_at", None),
        ("archived_at", 1),
        ("analysis_status" if catalog == "music" else "status", "failed"),
    ]:
        old = getattr(row, field)
        setattr(row, field, value)
        with pytest.raises(FileNotFoundError):
            await library.catalog_path(db, catalog, "track")
        setattr(row, field, old)
    for path in ["user/private.mp4", f"{prefix}/track/../private", f"{prefix}/track//a"]:
        row.audio_gcs_path = path
        with pytest.raises(ValueError):
            await library.catalog_path(db, catalog, "track")


def test_pin_hashes_exact_generation(monkeypatch):
    monkeypatch.setattr(
        library.storage, "object_metadata", lambda _: SimpleNamespace(size=3, generation="42")
    )

    def download(path, local, *, generation):
        assert generation == "42"
        Path(local).write_bytes(b"abc")

    monkeypatch.setattr(library.storage, "download_generation_to_file", download)
    result = library.inspect_library_asset(
        "music/track/audio.mp3", asset_id="a", catalog="music", catalog_id="track"
    )
    assert result.generation == "42"
    assert result.fingerprint.sha256 == hashlib.sha256(b"abc").hexdigest()
    assert result.fingerprint.byte_count == 3


def test_pin_rejects_metadata_size_mismatch(monkeypatch):
    monkeypatch.setattr(
        library.storage, "object_metadata", lambda _: SimpleNamespace(size=4, generation="42")
    )
    monkeypatch.setattr(
        library.storage,
        "download_generation_to_file",
        lambda path, local, **kw: Path(local).write_bytes(b"abc"),
    )
    with pytest.raises(ValueError, match="size changed"):
        library.inspect_library_asset(
            "music/track/audio.mp3", asset_id="a", catalog="music", catalog_id="track"
        )


def test_bundled_font_identity_uses_exact_shared_bytes():
    asset = library.bundled_font_asset("Inter-Regular.ttf", asset_id="font")
    assert asset.catalog == "font"
    assert asset.generation == asset.fingerprint.sha256
    assert asset.fingerprint.byte_count > 1000


@pytest.mark.parametrize(
    "name",
    ["../Inter-Regular.ttf", "folder/font.ttf", "font\\name.ttf", "handwriting-strokes.json"],
)
def test_font_catalog_rejects_paths_and_nonfonts(name):
    with pytest.raises(ValueError):
        library.bundled_font_asset(name, asset_id="font")
