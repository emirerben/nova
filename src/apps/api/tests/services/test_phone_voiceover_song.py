"""KRI-374 D2: ``inspect_song_asset`` pins the generation, then hashes exactly it."""

from __future__ import annotations

import hashlib
from types import SimpleNamespace

import pytest

from app.services import phone_voiceover as pv

BYTES = b"not really an m4a, but stable bytes"


@pytest.fixture
def storage(monkeypatch):
    live = SimpleNamespace(generation="42", size=len(BYTES), data=BYTES, downloaded=[])
    monkeypatch.setattr(
        pv.storage, "object_metadata", lambda _p: SimpleNamespace(generation="42", size=live.size)
    )

    def download(path, local, *, generation):
        live.downloaded.append((path, generation))
        with open(local, "wb") as out:
            out.write(live.data)

    monkeypatch.setattr(pv.storage, "download_generation_to_file", download)
    return live


def test_pins_the_live_generation_and_hashes_those_bytes(storage) -> None:
    asset = pv.inspect_song_asset("users/u/song.m4a", asset_id="song-1", plan_item_id="item-1")
    assert (asset.kind, asset.id, asset.plan_item_id, asset.generation) == (
        "song",
        "song-1",
        "item-1",
        "42",
    )
    assert asset.fingerprint.sha256 == hashlib.sha256(BYTES).hexdigest()
    assert asset.fingerprint.byte_count == len(BYTES)
    assert storage.downloaded == [("users/u/song.m4a", "42")]


def test_a_song_replaced_since_approval_is_refused_before_any_download(storage) -> None:
    with pytest.raises(ValueError, match="replaced"):
        pv.inspect_song_asset(
            "users/u/song.m4a", asset_id="s", plan_item_id="i", expected_generation=41
        )
    assert storage.downloaded == []


def test_the_expected_generation_may_be_an_int(storage) -> None:
    asset = pv.inspect_song_asset(
        "users/u/song.m4a", asset_id="s", plan_item_id="i", expected_generation=42
    )
    assert asset.generation == "42"


def test_an_oversized_or_empty_object_is_refused(storage, monkeypatch) -> None:
    monkeypatch.setattr(
        pv.storage,
        "object_metadata",
        lambda _p: SimpleNamespace(generation="42", size=pv.MAX_SONG_BYTES + 1),
    )
    with pytest.raises(ValueError, match="size or generation"):
        pv.inspect_song_asset("p", asset_id="s", plan_item_id="i")


def test_a_size_that_changes_mid_download_is_refused(storage) -> None:
    storage.data = BYTES + b"!"
    with pytest.raises(ValueError, match="size changed"):
        pv.inspect_song_asset("p", asset_id="s", plan_item_id="i")
