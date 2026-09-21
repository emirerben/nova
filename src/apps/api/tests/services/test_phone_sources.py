import copy

import pytest
from pydantic import ValidationError

from app.services.phone_sources import (
    PHONE_VISUALS_FIELD,
    PhoneVisualBinding,
    bind_phone_sources,
    require_bound_moment,
    require_bound_visual,
)
from app.services.public_assembly_plan import _strip_private_state


def receipt(media_id="one"):
    return {
        "media_id": media_id,
        "manifest_identity": media_id,
        "gcs_path": f"user/raw/analysis-proxy-{media_id}.mp4",
        "storage_generation": "123",
        "duration_s": 10.0,
        "has_audio": True,
        "upload_contract": {
            "purpose": "analysis_proxy",
            "proxy": {
                "original": {
                    "sha256": "a" * 64,
                    "byte_count": 1000,
                    "duration_s": 10,
                    "width": 1920,
                    "height": 1080,
                    "has_audio": True,
                },
                "duration_s": 10,
                "width": 640,
                "height": 360,
                "frame_rate": 15,
            },
        },
    }


def test_order_and_original_identity_without_proxy_locations():
    first, second = receipt(), receipt("two")
    bound = bind_phone_sources([first, second], [second["gcs_path"], first["gcs_path"]])
    assert [x.media_id for x in bound] == ["two", "one"]
    assert bound[0].original.width == 1920
    asset = bound[0].render_asset().model_dump_json()
    assert "analysis-proxy" not in asset
    assert "123" not in asset
    assert bound[0].render_asset().fingerprint.byte_count == 1000
    assert (
        require_bound_moment(bound, media_id="one", path=first["gcs_path"], generation="123")
        == bound[1]
    )
    for changes in ({"media_id": "two"}, {"generation": "124"}, {"path": "different"}):
        with pytest.raises(ValueError):
            require_bound_moment(
                bound,
                **({"media_id": "one", "path": first["gcs_path"], "generation": "123"} | changes),
            )


@pytest.mark.parametrize(
    "change",
    [
        {"upload_contract": {}},
        {"manifest_identity": "another"},
        {"storage_generation": None},
        {"duration_s": 11},
        {"duration_s": float("nan")},
        {"duration_s": True},
        {"has_audio": False},
        {"has_audio": None},
        {"gcs_path": "user/raw/original.mp4"},
    ],
)
def test_incomplete_or_mismatched_receipts_reject(change):
    assignment = receipt() | change
    with pytest.raises(ValueError):
        bind_phone_sources([assignment], [assignment["gcs_path"]])


def test_missing_mixed_duplicate_and_conflicting_sources_reject():
    one = receipt()
    for paths in ([], [one["gcs_path"]] * 2, [one["gcs_path"], "original.mp4"]):
        with pytest.raises(ValueError):
            bind_phone_sources([one], paths)
    other = copy.deepcopy(one)
    other["upload_contract"]["proxy"]["original"]["sha256"] = "b" * 64
    with pytest.raises(ValueError, match="conflicting"):
        bind_phone_sources([one, other], [one["gcs_path"]])


def test_private_bindings_removed_at_every_public_nesting_level():
    assert _strip_private_state(
        {"_phone_sources_v1": [receipt()], "variants": [{"_phone_sources_v1": [receipt()]}]}
    ) == {"variants": [{}]}


def test_private_photo_receipts_removed_at_every_public_nesting_level():
    # Photo receipts hold pool storage paths; only the recipe identity is public.
    photo = {
        "media_id": "5b3f6a1e-8f1c-4c55-9a8e-2f7d1c9b0a11",
        "gcs_path": "users/u/plan/i/pool/photo.jpg",
        "generation": "77",
        "sha256": "c" * 64,
        "byte_count": 10,
    }
    assert _strip_private_state(
        {
            PHONE_VISUALS_FIELD: [photo],
            "variants": [{PHONE_VISUALS_FIELD: [photo], "variant_id": "guided_story"}],
        }
    ) == {"variants": [{"variant_id": "guided_story"}]}


# --- KRI-121 round 2: pool-video receipts -------------------------------------

PHOTO_RECEIPT = {
    "media_id": "5b3f6a1e-8f1c-4c55-9a8e-2f7d1c9b0a11",
    "gcs_path": "users/u/plan/i/pool/photo.jpg",
    "generation": "77",
    "sha256": "c" * 64,
    "byte_count": 10,
}
VIDEO_RECEIPT = {
    "media_id": "7d4f1b3a-0e5c-4a8d-b9f2-1c6e8a7b5d43",
    "gcs_path": "users/u/plan/i/pool/clip.mov",
    "generation": "88",
    "sha256": "d" * 64,
    "byte_count": 4096,
    "kind": "video",
    "duration_s": 8.5,
    "width": 1920,
    "height": 1080,
    "orientation_degrees": 90,
}


def test_photo_receipt_keeps_the_shape_earlier_jobs_persisted():
    # `_phone_visuals_v1` rows written in round 1 must decode, re-encode and
    # compare equal, or the worker's idempotent re-delivery check would trip.
    photo = PhoneVisualBinding.model_validate(PHOTO_RECEIPT)
    assert photo.kind == "image"
    assert photo.model_dump(mode="json") == PHOTO_RECEIPT
    assert photo.model_dump() == PHOTO_RECEIPT
    explicit = PhoneVisualBinding.model_validate(PHOTO_RECEIPT | {"kind": "image"})
    assert explicit == photo and explicit.model_dump(mode="json") == PHOTO_RECEIPT
    asset = photo.render_asset()
    assert asset.media_kind == "image"
    assert "media_kind" not in asset.model_dump(mode="json")


def test_video_receipt_round_trips_its_probe_and_pins_a_video_asset():
    video = PhoneVisualBinding.model_validate(VIDEO_RECEIPT)
    assert video.model_dump(mode="json") == VIDEO_RECEIPT
    assert PhoneVisualBinding.model_validate(video.model_dump(mode="json")) == video
    asset = video.render_asset()
    assert (asset.id, asset.media_kind) == (f"visual-{video.media_id}", "video")
    assert (asset.fingerprint.sha256, asset.fingerprint.byte_count) == ("d" * 64, 4096)
    # Probe facts and the pool path stay private job state.
    document = asset.model_dump_json()
    assert "/pool/" not in document and "duration" not in document
    # An unrotated video still says so explicitly: only photos drop the keys.
    upright = PhoneVisualBinding.model_validate(VIDEO_RECEIPT | {"orientation_degrees": 0})
    assert upright.model_dump(mode="json")["orientation_degrees"] == 0
    # Pool recordings may run past the 30-minute device-original bound.
    assert PhoneVisualBinding.model_validate(VIDEO_RECEIPT | {"duration_s": 2400}).duration_s


@pytest.mark.parametrize("field", ["duration_s", "width", "height"])
@pytest.mark.parametrize("how", ["missing", "null", "zero", "negative"])
def test_video_receipt_requires_every_probed_fact(field, how):
    receipt = dict(VIDEO_RECEIPT)
    if how == "missing":
        del receipt[field]
    else:
        receipt[field] = {"null": None, "zero": 0, "negative": -1}[how]
    with pytest.raises(ValidationError):
        PhoneVisualBinding.model_validate(receipt)


@pytest.mark.parametrize("value", [float("nan"), float("inf")], ids=["nan", "inf"])
def test_video_receipt_duration_must_be_finite(value):
    with pytest.raises(ValidationError):
        PhoneVisualBinding.model_validate(VIDEO_RECEIPT | {"duration_s": value})


@pytest.mark.parametrize("degrees", [0, 90, 180, 270])
def test_video_receipt_accepts_right_angle_rotation(degrees):
    video = PhoneVisualBinding.model_validate(VIDEO_RECEIPT | {"orientation_degrees": degrees})
    assert video.orientation_degrees == degrees


@pytest.mark.parametrize("degrees", [-90, 45, 91, 360])
def test_video_receipt_rotation_must_be_a_right_angle(degrees):
    with pytest.raises(ValidationError, match="right angle"):
        PhoneVisualBinding.model_validate(VIDEO_RECEIPT | {"orientation_degrees": degrees})


@pytest.mark.parametrize(
    "patch",
    [
        {"kind": "audio"},
        {"kind": "VIDEO"},
        {"has_audio": True},
        {"sha256": "D" * 64},
        {"byte_count": 0},
        {"generation": "8 8"},
        {"media_id": ""},
    ],
    ids=["audio", "case", "extra", "sha", "empty", "generation", "identity"],
)
def test_invalid_video_receipt_fails_closed(patch):
    with pytest.raises(ValidationError):
        PhoneVisualBinding.model_validate(VIDEO_RECEIPT | patch)


def test_bound_visual_lookup_matches_the_full_pinned_identity_for_either_kind():
    photo = PhoneVisualBinding.model_validate(PHOTO_RECEIPT)
    video = PhoneVisualBinding.model_validate(VIDEO_RECEIPT)
    for visual in (photo, video):
        pinned = {
            "media_id": visual.media_id,
            "path": visual.gcs_path,
            "generation": visual.generation,
        }
        assert require_bound_visual((photo, video), **pinned) == visual
        for change in ({"media_id": "other"}, {"path": "other"}, {"generation": "1"}):
            with pytest.raises(ValueError, match="approved media does not match"):
                require_bound_visual((photo, video), **(pinned | change))
    with pytest.raises(ValueError, match="approved media does not match"):
        require_bound_visual(
            (video, video),
            media_id=video.media_id,
            path=video.gcs_path,
            generation=video.generation,
        )


def test_private_video_receipts_removed_at_every_public_nesting_level():
    # Video receipts add probe facts beside the pool path; none of it is public.
    assert PHONE_VISUALS_FIELD == "_phone_visuals_v1"
    assert _strip_private_state(
        {
            PHONE_VISUALS_FIELD: [PHOTO_RECEIPT, VIDEO_RECEIPT],
            "variants": [
                {PHONE_VISUALS_FIELD: [VIDEO_RECEIPT], "variant_id": "guided_story"},
                {"nested": {PHONE_VISUALS_FIELD: [VIDEO_RECEIPT]}},
            ],
        }
    ) == {"variants": [{"variant_id": "guided_story"}, {"nested": {}}]}
