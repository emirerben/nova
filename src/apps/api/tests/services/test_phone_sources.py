import copy

import pytest

from app.services.phone_sources import bind_phone_sources, require_bound_moment
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
