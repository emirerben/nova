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


def test_public_job_omits_persona_key() -> None:
    # No persona args → the key must be ABSENT (not an empty dict) so the public
    # job's all_candidates shape is byte-identical to pre-persona behavior.
    job = build_generative_job(user_id=uuid.uuid4(), clip_paths=["slot-uploads/a.mp4"])
    assert "persona" not in job.all_candidates


def test_persona_stashed_in_all_candidates() -> None:
    job = build_generative_job(
        user_id=uuid.uuid4(),
        clip_paths=["users/u/plan/i/a.mp4"],
        mode="content_plan",
        persona_tone="no-excuses gym motivation",
        persona_pillars=["morning routines", "discipline"],
        item_theme="first 5am workout",
        item_idea="film the dark early start",
    )
    persona = job.all_candidates["persona"]
    assert persona["tone"] == "no-excuses gym motivation"
    assert persona["content_pillars"] == ["morning routines", "discipline"]
    assert persona["theme"] == "first 5am workout"
    assert persona["idea"] == "film the dark early start"


def test_persona_partial_fields_still_stashed() -> None:
    # Theme-only is enough to warrant the key (the hook can still cohere to it).
    job = build_generative_job(
        user_id=uuid.uuid4(),
        clip_paths=["users/u/plan/i/a.mp4"],
        item_theme="cheap rooftop nobody posts about",
    )
    assert job.all_candidates["persona"]["theme"] == "cheap rooftop nobody posts about"
    assert job.all_candidates["persona"]["content_pillars"] == []


def test_persona_all_empty_omits_key() -> None:
    # Empty strings / empty list collapse to "no persona" → key omitted.
    job = build_generative_job(
        user_id=uuid.uuid4(),
        clip_paths=["users/u/plan/i/a.mp4"],
        persona_tone="   ",
        persona_pillars=["", "  "],
        item_theme="",
        item_idea="",
    )
    assert "persona" not in job.all_candidates


def test_persona_pillars_capped_and_trimmed() -> None:
    job = build_generative_job(
        user_id=uuid.uuid4(),
        clip_paths=["users/u/plan/i/a.mp4"],
        persona_pillars=[f"  pillar{i}  " for i in range(20)],
    )
    pillars = job.all_candidates["persona"]["content_pillars"]
    assert len(pillars) == 8  # _MAX_PERSONA_PILLARS
    assert pillars[0] == "pillar0"  # trimmed


def test_edit_format_defaults_to_montage() -> None:
    # Public job with no declared format → montage (today's behavior), always present.
    job = build_generative_job(user_id=uuid.uuid4(), clip_paths=["slot-uploads/a.mp4"])
    assert job.all_candidates["edit_format"] == "montage"


def test_edit_format_passthrough_and_coercion() -> None:
    job = build_generative_job(
        user_id=uuid.uuid4(),
        clip_paths=["users/u/plan/i/a.mp4"],
        edit_format="talking_head",
    )
    assert job.all_candidates["edit_format"] == "talking_head"
    # An unknown/legacy value must coerce to montage, never reach the render path raw.
    job2 = build_generative_job(
        user_id=uuid.uuid4(),
        clip_paths=["users/u/plan/i/a.mp4"],
        edit_format="cinematic-banger",
    )
    assert job2.all_candidates["edit_format"] == "montage"


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
