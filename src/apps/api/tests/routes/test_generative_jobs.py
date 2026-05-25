"""Request-validation tests for the generative-job routes.

Pydantic field validators are tested directly (no DB / TestClient needed). The clip
prefix allowlist is the security-relevant one — an arbitrary bucket key must be rejected
before any download.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.routes.generative_jobs import (
    CreateGenerativeJobRequest,
    RetextRequest,
    SwapSongRequest,
)


def test_valid_request():
    req = CreateGenerativeJobRequest(
        clip_gcs_paths=["music-uploads/a.mp4", "slot-uploads/b.mp4"],
    )
    assert len(req.clip_gcs_paths) == 2


def test_target_duration_field_removed():
    # The slider is gone: output length is derived from footage, never user-set.
    # A stale frontend that still posts the field must be tolerated (ignored),
    # not rejected.
    assert "target_duration_s" not in CreateGenerativeJobRequest.model_fields
    req = CreateGenerativeJobRequest(
        clip_gcs_paths=["music-uploads/a.mp4"],
        target_duration_s=120.0,  # extra field — silently ignored
    )
    assert not hasattr(req, "target_duration_s")


def test_rejects_empty_clips():
    with pytest.raises(ValidationError):
        CreateGenerativeJobRequest(clip_gcs_paths=[])


def test_rejects_too_many_clips():
    with pytest.raises(ValidationError):
        CreateGenerativeJobRequest(clip_gcs_paths=["music-uploads/x.mp4"] * 21)


def test_rejects_arbitrary_bucket_prefix():
    # Security: only upload-endpoint prefixes allowed.
    with pytest.raises(ValidationError):
        CreateGenerativeJobRequest(clip_gcs_paths=["processed-outputs/secret.mp4"])


def test_rejects_path_traversal():
    with pytest.raises(ValidationError):
        CreateGenerativeJobRequest(clip_gcs_paths=["music-uploads/../../etc/passwd"])


def test_swap_song_request():
    assert SwapSongRequest(new_track_id="t1").new_track_id == "t1"


def test_retext_request_defaults():
    r = RetextRequest()
    assert r.text is None
    assert r.remove is False
