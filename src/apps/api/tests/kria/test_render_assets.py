import pytest
from pydantic import ValidationError

from app.kria.render_assets import RenderAssetManifest


def original(**changes):
    return dict(
        kind="original",
        id="clip-1",
        media_id="analysis-proxy-source-1",
        fingerprint={"sha256": "a" * 64, "byte_count": 512},
        **changes,
    )


def library():
    return {
        "kind": "library",
        "id": "music-1",
        "catalog": "music",
        "catalog_id": "track-1",
        "generation": "123456",
        "fingerprint": {"sha256": "b" * 64, "byte_count": 1024},
    }


def test_manifest_preserves_original_and_generation_identity():
    document = {"version": 1, "assets": [original(), library()]}
    manifest = RenderAssetManifest.model_validate(document)
    assert manifest.model_dump(mode="json") == document
    manifest.require_references({"clip-1", "music-1"})
    with pytest.raises(ValueError, match="unknown asset"):
        manifest.require_references({"missing"})


@pytest.mark.parametrize("field", ["url", "gcs_path", "relative_path", "download_url"])
def test_manifest_cannot_carry_storage_locations(field):
    with pytest.raises(ValidationError):
        RenderAssetManifest(assets=[original(**{field: "https://storage.invalid/file"})])


@pytest.mark.parametrize(
    "patch",
    [
        {"kind": "generated"},
        {"catalog": "original"},
        {"generation": ""},
        {"media_id": "original"},
        {"fingerprint": {"sha256": "B" * 64, "byte_count": 1024}},
        {"fingerprint": {"sha256": "b" * 64, "byte_count": 0}},
    ],
)
def test_invalid_library_identity_fails_closed(patch):
    with pytest.raises(ValidationError):
        RenderAssetManifest(assets=[library() | patch])


def test_duplicate_ids_and_conflicting_source_identity_are_rejected():
    with pytest.raises(ValidationError, match="unique"):
        RenderAssetManifest(assets=[original(), original()])
    with pytest.raises(ValidationError, match="different bytes"):
        RenderAssetManifest(
            assets=[
                library(),
                library()
                | {"id": "alias", "fingerprint": {"sha256": "c" * 64, "byte_count": 1024}},
            ]
        )


def test_aliases_for_same_bytes_and_new_generations_are_valid():
    manifest = RenderAssetManifest(
        assets=[
            library(),
            library() | {"id": "alias"},
            library()
            | {
                "id": "new",
                "generation": "123457",
                "fingerprint": {"sha256": "c" * 64, "byte_count": 1024},
            },
        ]
    )
    assert len(manifest.assets) == 3
