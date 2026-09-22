import copy

import pytest

from app.schemas.edit_proposal import MAX_EDIT_PROPOSAL_MEDIA
from app.services.phone_editor_sources import (
    EDITOR_SOURCES_FIELD,
    attempt_matches,
    begin_attempt,
    editor_sources_for_variant,
    lease_expired,
    merge_editor_sources,
)


def source(media_id="approved", **changes):
    return {
        "media_id": media_id,
        "lane": "clip",
        "gcs_path": f"users/u/{media_id}.mp4",
        "generation": "42",
        "kind": "video",
        "duration_s": 4.0,
        **changes,
    }


def admitted(media_id, index, **changes):
    return {
        **source(media_id, **changes),
        "source_index": index,
        "status": "ready",
        "source_binding": {"private": True},
    }


def test_catalog_preserves_indices_and_hides_receipts():
    variant = {EDITOR_SOURCES_FIELD: {"sources": [admitted("second", 2), admitted("first", 1)]}}
    before = copy.deepcopy(variant)
    assert editor_sources_for_variant(variant) == [source("first"), source("second")]
    assert merge_editor_sources([source()], variant) == [
        source(),
        source("first"),
        source("second"),
    ]
    assert variant == before


def test_exact_approved_identity_can_reuse_original_index():
    variant = {EDITOR_SOURCES_FIELD: {"sources": [admitted("approved", 0), admitted("new", 1)]}}
    assert merge_editor_sources([source()], variant) == [source(), source("new")]


@pytest.mark.parametrize(
    "change",
    [
        {"generation": "43"},
        {"lane": "asset"},
        {"duration_s": 5.0},
        {"gcs_path": "users/u/other.mp4"},
        {"kind": "image"},
        {"source_index": 1},
    ],
)
def test_conflicting_approved_identity_is_rejected(change):
    variant = {EDITOR_SOURCES_FIELD: {"sources": [{**admitted("approved", 0), **change}]}}
    with pytest.raises(ValueError):
        merge_editor_sources([source()], variant)


@pytest.mark.parametrize(
    "rows",
    [
        [admitted("first", 2)],
        [admitted("first", 1), admitted("second", 1)],
        [admitted("first", 1), admitted("first", 2)],
        [admitted("first", 1, lane="source")],
    ],
)
def test_invalid_catalog_never_projects(rows):
    with pytest.raises(ValueError):
        merge_editor_sources([source()], {EDITOR_SOURCES_FIELD: {"sources": rows}})


def test_catalog_schema_limit_is_enforced():
    with pytest.raises(ValueError, match="editor_source_limit"):
        merge_editor_sources([source(str(i)) for i in range(MAX_EDIT_PROPOSAL_MEDIA + 1)], {})


def test_retry_rotates_attempt_token_and_old_completion_cannot_match():
    record = {}
    original = begin_attempt(record, now=10)
    assert attempt_matches(record, original)
    assert not lease_expired(record, now=429)
    assert lease_expired(record, now=430)
    retry = begin_attempt(record, now=431)
    assert retry != original
    assert not attempt_matches(record, original)
    assert attempt_matches(record, retry)
    record["status"] = "ready"
    assert not attempt_matches(record, retry)
