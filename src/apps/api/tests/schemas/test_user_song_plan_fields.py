"""KRI-374 lane D1: ``user_song`` on the proposal snapshot / execution plan / asset manifest."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.kria.render_assets import (
    RenderAssetManifest,
    RenderFingerprint,
    SongRenderAsset,
    VoiceoverRenderAsset,
)
from app.pipeline.guided_story import GuidedStoryExecutionPlan, plan_preserves_source_audio
from app.pipeline.unified_montage import plan_unified_montage
from app.schemas.edit_proposal import EditProposalSnapshot, NarrationTrack
from app.schemas.user_song import UserSongPlan
from tests.pipeline.test_lipsync_montage import _manual_plan
from tests.pipeline.test_unified_montage_song import clips, song_plan


def snapshot_payload() -> dict:
    return song_plan().snapshot.model_dump(mode="json")


def test_user_song_is_omitted_from_serialization_when_absent():
    plain = plan_unified_montage(clips()).snapshot
    assert plain.user_song is None
    assert "user_song" not in plain.model_dump(mode="json")
    assert "user_song" not in plain.model_dump(mode="json", exclude_none=False)
    assert "user_song" not in _manual_plan(10, 12).model_copy(
        update={"user_song": None}
    ).model_dump(mode="json", exclude_none=False)


def test_user_song_round_trips_through_the_snapshot():
    snapshot = song_plan().snapshot
    again = EditProposalSnapshot.model_validate(snapshot.model_dump(mode="json"))
    assert again.user_song == snapshot.user_song


def test_snapshot_window_must_equal_the_proposal_duration():
    payload = snapshot_payload()
    payload["user_song"]["window_end_s"] += 0.5
    with pytest.raises(ValidationError, match="song window must equal"):
        EditProposalSnapshot.model_validate(payload)


def test_snapshot_song_cannot_share_the_audio_lane_with_a_voiceover():
    payload = snapshot_payload()
    payload["narration"] = NarrationTrack(
        gcs_path="v/a.m4a", generation="1", duration_s=payload["duration_s"]
    ).model_dump(mode="json")
    with pytest.raises(ValidationError, match="voiceover"):
        EditProposalSnapshot.model_validate(payload)


def test_snapshot_song_needs_fast_montage_cuts():
    payload = snapshot_payload()
    payload["fast_cuts"] = None
    payload["direction"] = "guided_story"
    with pytest.raises(ValidationError):
        EditProposalSnapshot.model_validate(payload)


def plan_payload(**changes) -> dict:
    return _manual_plan(10, 12).model_dump(mode="json") | changes


def test_execution_plan_window_must_cover_the_resolved_duration():
    payload = plan_payload()
    payload["user_song"] = dict(payload["user_song"], window_end_s=33.0)
    with pytest.raises(ValidationError, match="song window must cover"):
        GuidedStoryExecutionPlan.model_validate(payload)


@pytest.mark.parametrize(
    "extra",
    [
        {
            "compiler_version": 5,
            "music": {
                "track_id": "t",
                "title": "t",
                "audio_gcs_path": "music/t.mp3",
                "generation": "1",
                "start_s": 0,
            },
        },
        {
            "song_reference": {"track_id": "t", "title": "t", "start_s": 0, "end_s": 10},
            "song_reference_track_duration_s": 60,
        },
        {
            "narration": NarrationTrack(
                gcs_path="v/a.m4a", generation="1", duration_s=10
            ).model_dump(mode="json")
        },
    ],
)
def test_execution_plan_song_is_exclusive_with_catalog_music_references_and_voiceover(extra):
    with pytest.raises(ValidationError, match="creator song cannot be combined"):
        GuidedStoryExecutionPlan.model_validate(plan_payload(**extra))


def test_a_creator_song_means_the_footage_audio_is_not_preserved():
    payload = plan_payload(montage_audio={"preserve_source_audio": True})
    assert plan_preserves_source_audio(payload) is False
    assert plan_preserves_source_audio(GuidedStoryExecutionPlan.model_validate(payload)) is False
    payload.pop("user_song")
    assert plan_preserves_source_audio(payload) is True


def test_song_render_asset_wire_shape_and_manifest_identity():
    fingerprint = RenderFingerprint(sha256="a" * 64, byte_count=1234)
    asset = SongRenderAsset(
        id="song-item-1", plan_item_id="item-1", generation="42", fingerprint=fingerprint
    )
    assert asset.model_dump(mode="json") == {
        "kind": "song",
        "id": "song-item-1",
        "plan_item_id": "item-1",
        "generation": "42",
        "fingerprint": {"sha256": "a" * 64, "byte_count": 1234},
    }
    manifest = RenderAssetManifest(assets=(asset,))
    assert RenderAssetManifest.model_validate_json(manifest.model_dump_json()) == manifest
    # A song and a voiceover on the same item/generation are different identities.
    voice = VoiceoverRenderAsset(
        id="voiceover-item-1",
        plan_item_id="item-1",
        generation="42",
        fingerprint=RenderFingerprint(sha256="b" * 64, byte_count=9),
    )
    RenderAssetManifest(assets=(asset, voice))
    other = asset.model_copy(
        update={"id": "song-again", "fingerprint": RenderFingerprint(sha256="c" * 64, byte_count=5)}
    )
    with pytest.raises(ValidationError, match="different bytes"):
        RenderAssetManifest(assets=(asset, other))


def test_user_song_plan_contract_is_reused_unchanged():
    assert UserSongPlan.model_fields["mode"].annotation is not None
