import pytest
from pydantic import ValidationError

from app.kria.render_assets import RenderAssetManifest, VisualRenderAsset

VISUAL_ID = "5b2f9d1e-8c3a-4f6b-9e21-7a0d4c3b2a10"


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


def visual():
    return {
        "kind": "visual",
        "id": f"visual-{VISUAL_ID}",
        "visual_id": VISUAL_ID,
        "generation": "777",
        "fingerprint": {"sha256": "c" * 64, "byte_count": 2048},
    }


def visual_video():
    return visual() | {
        "id": "visual-video",
        "visual_id": "7d4f1b3a-0e5c-4a8d-b9f2-1c6e8a7b5d43",
        "generation": "888",
        "media_kind": "video",
        "fingerprint": {"sha256": "d" * 64, "byte_count": 4096},
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


def test_visual_round_trips_by_discriminator_beside_other_kinds():
    document = {"version": 1, "assets": [original(), library(), visual()]}
    manifest = RenderAssetManifest.model_validate(document)
    assert manifest.model_dump(mode="json") == document
    assert [type(asset).__name__ for asset in manifest.assets] == [
        "OriginalRenderAsset",
        "LibraryRenderAsset",
        "VisualRenderAsset",
    ]
    manifest.require_references({"clip-1", "music-1", f"visual-{VISUAL_ID}"})


@pytest.mark.parametrize("field", ["url", "gcs_path", "relative_path", "download_url"])
def test_visual_cannot_carry_storage_locations(field):
    # The pool path stays private job state; only the grant resolves it.
    with pytest.raises(ValidationError):
        RenderAssetManifest(assets=[visual() | {field: "users/u/plan/i/pool/photo.jpg"}])


@pytest.mark.parametrize(
    "patch",
    [
        {"id": ""},
        {"id": "visual 1"},
        {"visual_id": ""},
        {"visual_id": f" {VISUAL_ID}"},
        {"generation": ""},
        {"generation": "77 7"},
        {"catalog": "music"},
        {"media_id": VISUAL_ID},
        {"kind": "library"},
        {"fingerprint": {"sha256": "C" * 64, "byte_count": 2048}},
        {"fingerprint": {"sha256": "c" * 64, "byte_count": 0}},
    ],
)
def test_invalid_visual_identity_fails_closed(patch):
    with pytest.raises(ValidationError):
        RenderAssetManifest(assets=[visual() | patch])


def test_visual_without_a_pinned_generation_is_rejected():
    document = visual()
    del document["generation"]
    with pytest.raises(ValidationError):
        RenderAssetManifest(assets=[document])


def test_duplicate_visual_ids_and_conflicting_visual_bytes_are_rejected():
    with pytest.raises(ValidationError, match="unique"):
        RenderAssetManifest(assets=[visual(), visual()])
    with pytest.raises(ValidationError, match="different bytes"):
        RenderAssetManifest(
            assets=[
                visual(),
                visual() | {"id": "alias", "fingerprint": {"sha256": "d" * 64, "byte_count": 2048}},
            ]
        )


def test_visual_aliases_generations_and_namespaces_stay_distinct():
    manifest = RenderAssetManifest(
        assets=[
            visual(),
            visual() | {"id": "alias"},
            # A replaced pool photo is a new generation, so new bytes are valid.
            visual()
            | {
                "id": "replaced",
                "generation": "778",
                "fingerprint": {"sha256": "d" * 64, "byte_count": 4096},
            },
            # Identity keys are namespaced by kind: the same id string as an
            # original or a library item never collides with a visual.
            original() | {"id": "original", "media_id": VISUAL_ID},
            library() | {"id": "library", "catalog_id": VISUAL_ID, "generation": "777"},
        ]
    )
    assert sum(isinstance(asset, VisualRenderAsset) for asset in manifest.assets) == 3


# --- KRI-121 round 2: pool videos ---------------------------------------------


def test_photo_visual_keeps_its_round_one_wire_shape():
    # Already-issued photo recipes are pinned by digest: the default kind must
    # never appear in the document.
    asset = VisualRenderAsset.model_validate(visual())
    assert asset.media_kind == "image"
    assert asset.model_dump(mode="json") == visual()
    assert asset.model_dump() == visual()
    assert "media_kind" not in asset.model_dump_json()
    # An explicit "image" decodes to the same asset and the same bytes.
    explicit = VisualRenderAsset.model_validate(visual() | {"media_kind": "image"})
    assert explicit == asset
    assert explicit.model_dump_json() == asset.model_dump_json()


def test_video_visual_round_trips_its_media_kind():
    document = {"version": 1, "assets": [visual(), visual_video()]}
    manifest = RenderAssetManifest.model_validate(document)
    assert [asset.media_kind for asset in manifest.assets] == ["image", "video"]
    assert manifest.model_dump(mode="json") == document
    assert RenderAssetManifest.model_validate_json(manifest.model_dump_json()) == manifest


@pytest.mark.parametrize("media_kind", ["audio", "photo", "", None, "VIDEO"])
def test_unknown_visual_media_kind_fails_closed(media_kind):
    with pytest.raises(ValidationError):
        RenderAssetManifest(assets=[visual() | {"media_kind": media_kind}])


@pytest.mark.parametrize("kind", ["original", "library"])
def test_only_visuals_carry_a_media_kind(kind):
    document = original() if kind == "original" else library()
    with pytest.raises(ValidationError):
        RenderAssetManifest(assets=[document | {"media_kind": "video"}])
