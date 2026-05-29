"""Unit tests for the shared build_generative_job service (plan T4, no DB).

Locks the single-source-of-truth Job shape + clip-path validation that both the
public generative route and the content-plan per-item task depend on.
"""

from __future__ import annotations

import uuid

import pytest

from app.services.generative_jobs import build_generative_job


def test_build_default_generative_job() -> None:
    uid = uuid.uuid4()
    job = build_generative_job(user_id=uid, clip_paths=["slot-uploads/a.mp4"])
    assert job.user_id == uid
    assert job.job_type == "generative"
    assert job.mode == "generative"
    assert job.status == "queued"
    assert job.raw_storage_path == "slot-uploads/a.mp4"
    assert job.all_candidates["clip_paths"] == ["slot-uploads/a.mp4"]
    assert job.all_candidates["language"] == "en"
    assert job.content_plan_item_id is None


def test_content_plan_mode_sets_reverse_link() -> None:
    item_id = uuid.uuid4()
    job = build_generative_job(
        user_id=uuid.uuid4(),
        clip_paths=["users/u/plan/i/a.mp4"],
        mode="content_plan",
        content_plan_item_id=item_id,
    )
    assert job.mode == "content_plan"
    assert job.content_plan_item_id == item_id


def test_users_prefix_is_allowlisted() -> None:
    # The whole point of the Phase 5 allowlist change — plan uploads must pass.
    job = build_generative_job(user_id=uuid.uuid4(), clip_paths=["users/u/plan/i/clip.mp4"])
    assert job.raw_storage_path.startswith("users/")


def test_rejects_unallowlisted_prefix() -> None:
    with pytest.raises(ValueError):
        build_generative_job(user_id=uuid.uuid4(), clip_paths=["secret-bucket/key.mp4"])


def test_rejects_empty_clip_list() -> None:
    with pytest.raises(ValueError):
        build_generative_job(user_id=uuid.uuid4(), clip_paths=[])


def test_rejects_path_traversal() -> None:
    with pytest.raises(ValueError):
        build_generative_job(user_id=uuid.uuid4(), clip_paths=["users/../../etc/passwd"])
