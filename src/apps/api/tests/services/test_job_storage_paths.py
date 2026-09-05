"""Ownership/normalization checks for persisted job storage keys.

Posters moved off the video prefixes onto the lifecycle-exempt
``job-posters/{job_id}/`` prefix, so both the legacy sibling keys and the
durable keys must resolve here — and neither shape may widen the ownership
boundary.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest

from app.config import settings
from app.services.job_storage_paths import (
    JOB_OUTPUT_PREFIXES,
    job_input_path_matches_owner,
    job_output_path,
    normalize_job_storage_path,
    owned_job_output_path,
)
from app.services.template_poster import poster_object_path


def _job() -> SimpleNamespace:
    return SimpleNamespace(id=uuid.uuid4(), user_id=uuid.uuid4())


def test_durable_poster_prefix_is_allowlisted() -> None:
    assert "job-posters/{job_id}/" in JOB_OUTPUT_PREFIXES


def test_job_input_path_owner_validation_preserves_legacy_and_rejects_foreign_namespaces() -> None:
    owner = uuid.uuid4()
    foreign = uuid.uuid4()

    assert job_input_path_matches_owner(f"users/{owner}/plan/item/clip.mp4", owner)
    assert job_input_path_matches_owner(f"voiceover-uploads/direct/{owner}/voice.m4a", owner)
    assert job_input_path_matches_owner(f"dev-user/{owner}/generative/clip.mp4", owner)
    assert job_input_path_matches_owner(f"dev-user/{uuid.uuid4()}/raw.mp4", owner)
    assert job_input_path_matches_owner("slot-uploads/legacy/clip.mp4", owner)
    assert not job_input_path_matches_owner(f"users/{foreign}/plan/item/clip.mp4", owner)
    assert not job_input_path_matches_owner(f"voiceover-uploads/direct/{foreign}/voice.m4a", owner)
    assert not job_input_path_matches_owner(f"dev-user/{foreign}/generative/clip.mp4", owner)
    assert not job_input_path_matches_owner(f"{foreign}/{uuid.uuid4()}/raw.mp4", owner)


def test_owned_job_output_path_accepts_durable_poster_key() -> None:
    job = _job()
    key = poster_object_path(f"music-jobs/{job.id}/output.mp4", job_id=str(job.id))

    assert key.startswith(f"job-posters/{job.id}/")
    assert owned_job_output_path(key, job) == key
    assert job_output_path(key, job.id) == key


def test_owned_job_output_path_still_accepts_legacy_sibling_poster_keys() -> None:
    """Mixed keys are normal during the transition; the stored value rules."""
    job = _job()
    for prefix in ("generative-jobs", "jobs", "music-jobs", "auto-music-jobs"):
        legacy = poster_object_path(f"{prefix}/{job.id}/output.mp4")
        assert legacy == f"{prefix}/{job.id}/output.mp4.poster.jpg"
        assert owned_job_output_path(legacy, job) == legacy

    default_uploader = f"{job.user_id}/{job.id}/output.mp4.poster.jpg"
    assert owned_job_output_path(default_uploader, job) == default_uploader


def test_owned_job_output_path_rejects_another_jobs_durable_poster() -> None:
    job = _job()
    other = _job()
    foreign = poster_object_path(f"music-jobs/{other.id}/output.mp4", job_id=str(other.id))

    assert owned_job_output_path(foreign, job) is None


@pytest.mark.parametrize(
    "path",
    [
        "job-posters/../secrets/key.jpg",
        "job-posters/{job_id}/../../secrets/key.jpg",
        "../job-posters/{job_id}/poster.jpg",
        "job-postersX/{job_id}/poster.jpg",
        "job-posters/poster.jpg",
        "users/{job_id}/poster.jpg",
    ],
    ids=[
        "traversal-root",
        "traversal-inside-prefix",
        "traversal-prefixed",
        "prefix-lookalike",
        "no-job-segment",
        "foreign-prefix",
    ],
)
def test_owned_job_output_path_rejects_traversal_and_foreign_prefixes(path: str) -> None:
    job = _job()

    assert owned_job_output_path(path.format(job_id=job.id), job) is None


@pytest.mark.parametrize(
    "url",
    [
        "https://evil.example.com/{bucket}/job-posters/{job_id}/poster.jpg",
        "http://storage.googleapis.com/{bucket}/job-posters/{job_id}/poster.jpg",
        "https://storage.googleapis.com/other-bucket/job-posters/{job_id}/poster.jpg",
    ],
    ids=["foreign-host", "plain-http", "foreign-bucket"],
)
def test_normalize_rejects_untrusted_urls_for_durable_posters(url: str) -> None:
    job = _job()
    candidate = url.format(bucket=settings.storage_bucket, job_id=job.id)

    assert normalize_job_storage_path(candidate) is None
    assert owned_job_output_path(candidate, job) is None


def test_owned_job_output_path_accepts_signed_browser_url_for_durable_poster() -> None:
    job = _job()
    key = poster_object_path(f"jobs/{job.id}/output.mp4", job_id=str(job.id))
    url = f"https://storage.googleapis.com/{settings.storage_bucket}/{key}"

    assert owned_job_output_path(url, job) == key
