from __future__ import annotations

import copy
import uuid

import pytest

from app.services.speech_cleanup_identity import (
    CLIP_METADATA_IDENTITY_INDEX_VERSION,
    ClipSourceIdentityError,
    append_clip_source,
    build_clip_metadata_identity_index,
    provision_clip_source_instance_ids,
    remove_clip_source_at,
    reorder_clip_sources,
    replace_clip_source_at,
    speech_cleanup_rollout_fingerprint,
    speech_cleanup_source_tag,
    validate_clip_metadata_identity_index,
    validate_clip_source_identity,
)

_A = "00000000-0000-4000-8000-000000000001"
_B = "00000000-0000-4000-8000-000000000002"
_C = "00000000-0000-4000-8000-000000000003"


def _factory(*values: str):
    identities = iter(values)
    return lambda: next(identities)


def test_absent_identity_is_provisioned_once_without_mutating_input() -> None:
    original = {"clip_paths": ["a.mp4", "b.mp4"], "user_field": {"keep": True}}
    snapshot = copy.deepcopy(original)

    provisioned, identity = provision_clip_source_instance_ids(
        original,
        identity_factory=_factory(_A, _B),
    )
    second, second_identity = provision_clip_source_instance_ids(
        provisioned,
        identity_factory=_factory(_C),
    )

    assert original == snapshot
    assert identity.valid and identity.provisioned
    assert identity.pairs == (("a.mp4", _A), ("b.mp4", _B))
    assert second == provisioned
    assert second_identity.valid and not second_identity.provisioned


@pytest.mark.parametrize(
    ("source_ids", "expected"),
    [
        (None, "invalid_source_instance"),
        ([_A], "cardinality_mismatch"),
        ([_A, "not-a-uuid"], "invalid_source_instance"),
        ([_A, _A], "duplicate_source_instance"),
    ],
)
def test_present_bad_identity_is_never_silently_regenerated(source_ids, expected) -> None:
    candidates = {
        "clip_paths": ["a.mp4", "b.mp4"],
        "clip_source_instance_ids": source_ids,
    }

    unchanged, identity = provision_clip_source_instance_ids(
        candidates,
        identity_factory=_factory(_B, _C),
    )

    assert unchanged == candidates
    assert identity.status == expected
    with pytest.raises(ClipSourceIdentityError, match=expected):
        append_clip_source(candidates, "c.mp4")


def test_pairwise_mutations_keep_paths_and_source_ids_aligned() -> None:
    candidates = {
        "clip_paths": ["a.mp4", "b.mp4"],
        "clip_source_instance_ids": [_A, _B],
    }

    appended, slot, appended_id = append_clip_source(
        candidates,
        "c.mp4",
        identity_factory=_factory(_C),
    )
    assert slot == 2
    assert appended_id == _C
    assert validate_clip_source_identity(appended).pairs == (
        ("a.mp4", _A),
        ("b.mp4", _B),
        ("c.mp4", _C),
    )

    reordered = reorder_clip_sources(appended, [2, 0, 1])
    assert validate_clip_source_identity(reordered).pairs == (
        ("c.mp4", _C),
        ("a.mp4", _A),
        ("b.mp4", _B),
    )

    removed, removed_pair = remove_clip_source_at(
        reordered,
        1,
        expected_path="a.mp4",
    )
    assert removed_pair == ("a.mp4", _A)
    assert validate_clip_source_identity(removed).pairs == (
        ("c.mp4", _C),
        ("b.mp4", _B),
    )


def test_replacement_always_mints_a_fresh_identity() -> None:
    candidates = {
        "clip_paths": ["a.mp4", "b.mp4"],
        "clip_source_instance_ids": [_A, _B],
    }

    replaced, old_pair, new_id = replace_clip_source_at(
        candidates,
        0,
        "replacement.mp4",
        expected_path="a.mp4",
        identity_factory=_factory(_C),
    )

    assert old_pair == ("a.mp4", _A)
    assert new_id == _C
    assert validate_clip_source_identity(replaced).pairs == (
        ("replacement.mp4", _C),
        ("b.mp4", _B),
    )


def test_rollout_fingerprint_uses_job_and_source_identity_not_path_or_slot() -> None:
    value = speech_cleanup_rollout_fingerprint("job-1", _A)
    assert len(value) == 64
    assert value == speech_cleanup_rollout_fingerprint("job-1", _A.upper())
    assert value != speech_cleanup_rollout_fingerprint("job-2", _A)
    assert value != speech_cleanup_rollout_fingerprint("job-1", _B)


def test_source_tag_is_bounded_and_domain_separated_from_rollout_identity() -> None:
    fingerprint = speech_cleanup_rollout_fingerprint("job-1", _A)

    tag = speech_cleanup_source_tag(fingerprint)

    assert len(tag) == 16
    assert tag == speech_cleanup_source_tag(fingerprint)
    assert tag != fingerprint[:16]


def test_indexed_metadata_cache_survives_completion_order_and_explicit_failure() -> None:
    envelope = build_clip_metadata_identity_index(
        # Deliberately reverse completion order: source_slot remains authoritative.
        records=[(1, "clip-b", {"score": 2}), (0, "clip-a", {"score": 1})],
        failed_source_slots=[2],
        source_instance_ids=[_A, _B, _C],
    )

    result = validate_clip_metadata_identity_index(
        envelope,
        source_instance_ids=[_A, _B, _C],
    )

    assert envelope["version"] == CLIP_METADATA_IDENTITY_INDEX_VERSION
    assert result.available
    assert result.records_by_source_slot[0]["clip_id"] == "clip-a"
    assert result.records_by_source_slot[1]["clip_id"] == "clip-b"
    assert result.failed_source_slots == (2,)


def test_indexed_metadata_cache_fails_closed_on_identity_or_clip_ambiguity() -> None:
    envelope = build_clip_metadata_identity_index(
        records=[(0, "clip-a", {}), (1, "clip-b", {})],
        failed_source_slots=[],
        source_instance_ids=[_A, _B],
    )
    wrong_identity = copy.deepcopy(envelope)
    wrong_identity["records"][0]["source_instance_id"] = _C
    duplicate_clip_id = copy.deepcopy(envelope)
    duplicate_clip_id["records"][1]["clip_id"] = "clip-a"

    assert not validate_clip_metadata_identity_index(
        wrong_identity,
        source_instance_ids=[_A, _B],
    ).available
    ambiguous = validate_clip_metadata_identity_index(
        duplicate_clip_id,
        source_instance_ids=[_A, _B],
    )
    assert not ambiguous.available
    assert ambiguous.reason == "duplicate_clip_id"


def test_generated_default_ids_are_canonical_unique_uuids() -> None:
    provisioned, identity = provision_clip_source_instance_ids({"clip_paths": ["a.mp4", "b.mp4"]})

    assert identity.valid
    assert len(set(provisioned["clip_source_instance_ids"])) == 2
    assert all(uuid.UUID(value) for value in provisioned["clip_source_instance_ids"])
