from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.schemas.guided_edit_revision import (
    GUIDED_EDITOR_LANES,
    GUIDED_EDITOR_RECORD_ID_MAX_LENGTH,
    GuidedEditorRevision,
    validate_guided_revision_lane_identities,
)


def _revision(**overrides: object) -> dict:
    revision: dict = {
        "approval_proposal_version": 1,
        "approval_media_digest": "a" * 64,
        "revision_number": 1,
        "sources": [
            {
                "media_id": "clip-1",
                "lane": "clip",
                "gcs_path": "users/u/clip.mp4",
                "generation": "generation-1",
                "kind": "video",
                "duration_s": 3.0,
            }
        ],
        "segments": [
            {
                "segment_id": "segment-1",
                "media_id": "clip-1",
                "duration_s": 1.0,
                "output_start_s": 0.0,
                "output_end_s": 1.0,
            }
        ],
    }
    revision.update(overrides)
    return revision


@pytest.mark.parametrize("lane", GUIDED_EDITOR_LANES)
def test_every_guided_lane_requires_a_nonblank_bounded_id(lane: str) -> None:
    with pytest.raises(ValidationError, match=f"{lane}.*(nonblank|characters)"):
        GuidedEditorRevision.model_validate(_revision(**{lane: [{"id": "   "}]}))

    with pytest.raises(ValidationError, match=f"{lane}.*characters"):
        GuidedEditorRevision.model_validate(
            _revision(**{lane: [{"id": "x" * (GUIDED_EDITOR_RECORD_ID_MAX_LENGTH + 1)}]})
        )

    with pytest.raises(ValidationError, match=f"{lane}.*string"):
        GuidedEditorRevision.model_validate(_revision(**{lane: [{"id": 123}]}))

    with pytest.raises(ValidationError, match=f"{lane}.*surrounding whitespace"):
        GuidedEditorRevision.model_validate(_revision(**{lane: [{"id": " padded-id "}]}))


@pytest.mark.parametrize("lane", GUIDED_EDITOR_LANES)
def test_every_guided_lane_accepts_a_valid_active_record(lane: str) -> None:
    revision = GuidedEditorRevision.model_validate(_revision(**{lane: [{"id": f"{lane}-1"}]}))
    assert revision.model_dump()[lane][0]["id"] == f"{lane}-1"


def test_active_ids_are_unique_within_and_across_lanes() -> None:
    with pytest.raises(ValidationError, match="unique across lanes"):
        GuidedEditorRevision.model_validate(
            _revision(
                text_elements=[{"id": "duplicate"}],
                motion_scenes=[{"id": "duplicate"}],
            )
        )

    with pytest.raises(ValidationError, match="unique across lanes"):
        GuidedEditorRevision.model_validate(
            _revision(text_elements=[{"id": "duplicate"}, {"id": "duplicate"}])
        )


@pytest.mark.parametrize("lane", GUIDED_EDITOR_LANES)
def test_tombstone_names_a_supported_lane_and_bounded_record_id(lane: str) -> None:
    revision = GuidedEditorRevision.model_validate(
        _revision(tombstones=[{"lane": lane, "record_id": f"deleted-{lane}"}])
    )
    assert revision.tombstones[0]["lane"] == lane

    with pytest.raises(ValidationError, match="must be one of"):
        GuidedEditorRevision.model_validate(
            _revision(tombstones=[{"lane": "unknown", "record_id": "deleted"}])
        )

    with pytest.raises(ValidationError, match="record_id.*nonblank"):
        GuidedEditorRevision.model_validate(
            _revision(tombstones=[{"lane": lane, "record_id": " "}])
        )

    with pytest.raises(ValidationError, match="record_id.*characters"):
        GuidedEditorRevision.model_validate(
            _revision(
                tombstones=[
                    {
                        "lane": lane,
                        "record_id": "x" * (GUIDED_EDITOR_RECORD_ID_MAX_LENGTH + 1),
                    }
                ]
            )
        )

    with pytest.raises(ValidationError, match="record_id.*surrounding whitespace"):
        GuidedEditorRevision.model_validate(
            _revision(tombstones=[{"lane": lane, "record_id": " deleted "}])
        )


def test_tombstone_ids_are_unique_globally_and_cannot_collide_with_active_records() -> None:
    with pytest.raises(ValidationError, match="tombstone record IDs must be unique"):
        GuidedEditorRevision.model_validate(
            _revision(
                tombstones=[
                    {"lane": "text_elements", "record_id": "deleted"},
                    {"lane": "text_elements", "record_id": "deleted"},
                ]
            )
        )

    with pytest.raises(ValidationError, match="tombstone collides with active record ID"):
        GuidedEditorRevision.model_validate(
            _revision(
                text_elements=[{"id": "same-id"}],
                tombstones=[{"lane": "text_elements", "record_id": "same-id"}],
            )
        )


def test_same_lane_delete_is_valid_until_the_record_is_restored() -> None:
    deleted = _revision(
        tombstones=[
            {
                "lane": "text_elements",
                "record_id": "title-1",
                "record": {"id": "title-1", "text": "Deleted"},
            }
        ]
    )
    assert GuidedEditorRevision.model_validate(deleted).tombstones[0]["record_id"] == "title-1"

    with pytest.raises(ValidationError, match="tombstone collides with active record ID"):
        GuidedEditorRevision.model_validate(
            {**deleted, "text_elements": [{"id": "title-1", "text": "Restored"}]}
        )


def test_plain_mapping_and_model_validation_share_the_same_identity_rules() -> None:
    malformed = _revision(text_elements=[{"id": "ok"}], sound_effects=[{"id": "ok"}])
    with pytest.raises(ValueError, match="unique across lanes"):
        validate_guided_revision_lane_identities(malformed)
