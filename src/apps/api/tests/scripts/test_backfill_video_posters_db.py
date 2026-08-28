"""Real-Postgres regression tests for video-poster backfill persistence.

The production incident this locks down cannot be reproduced with a fake
session: mutating a dict nested inside a JSONB value can leave SQLAlchemy with
no dirty top-level value, so ``commit()`` succeeds without issuing an UPDATE.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import OperationalError

import scripts.backfill_video_posters as backfill
from app.config import settings
from app.database import sync_session
from app.models import Job, JobClip, User
from app.services.video_poster_cleanup import (
    VIDEO_POSTER_BACKFILL_CLEANUP_FIELD,
    jobs_with_video_poster_cleanup_receipts,
)
from scripts.backfill_video_posters import _make_candidate, _persist_poster

_db_name = make_url(settings.database_url).database or ""
if not _db_name.endswith("_test"):
    pytest.skip(
        f"refusing to write to non-test database {_db_name!r}",
        allow_module_level=True,
    )
try:
    with sync_session() as _probe:
        _probe.execute(text("select 1"))
except OperationalError:
    pytest.skip("nova_test Postgres not reachable", allow_module_level=True)


def test_cleanup_receipt_selector_uses_key_presence_order_and_limit() -> None:
    user_id = uuid.uuid4()
    valid_id, empty_id, malformed_id, absent_id = sorted(uuid.uuid4() for _ in range(4))
    old = datetime(2026, 8, 28, 8, 0, tzinfo=UTC)
    tied = old + timedelta(minutes=1)
    plans = {
        absent_id: {},
        malformed_id: {VIDEO_POSTER_BACKFILL_CLEANUP_FIELD: "malformed"},
        empty_id: {VIDEO_POSTER_BACKFILL_CLEANUP_FIELD: []},
        valid_id: {
            VIDEO_POSTER_BACKFILL_CLEANUP_FIELD: [
                {
                    "old_path": "jobs/job/output.mp4.poster.backfill-"
                    "00000000-0000-0000-0000-000000000010.jpg",
                    "replacement_path": "jobs/job/replacement.mp4",
                }
            ]
        },
    }

    try:
        with sync_session() as db:
            db.add(User(id=user_id, email=f"poster-cleanup-selector-{user_id}@test.local"))
            for job_id, plan in plans.items():
                db.add(
                    Job(
                        id=job_id,
                        user_id=user_id,
                        status="done",
                        job_type="template",
                        raw_storage_path=f"{user_id}/{job_id}/raw.mp4",
                        assembly_plan=plan,
                    )
                )
            db.commit()
            db.execute(
                text("update jobs set updated_at = :updated_at where id = :job_id"),
                [
                    {"job_id": absent_id, "updated_at": old - timedelta(minutes=1)},
                    {"job_id": malformed_id, "updated_at": old},
                    {"job_id": empty_id, "updated_at": tied},
                    {"job_id": valid_id, "updated_at": tied},
                ],
            )
            db.commit()

        with sync_session() as db:
            assert jobs_with_video_poster_cleanup_receipts(db, limit=10) == [
                malformed_id,
                valid_id,
                empty_id,
            ]
            assert jobs_with_video_poster_cleanup_receipts(db, limit=2) == [
                malformed_id,
                valid_id,
            ]
            assert jobs_with_video_poster_cleanup_receipts(db, limit=0) == []
            assert jobs_with_video_poster_cleanup_receipts(db, limit=-1) == []
    finally:
        with sync_session() as cleanup:
            cleanup.execute(
                text("delete from jobs where user_id = :user_id"),
                {"user_id": user_id},
            )
            cleanup.execute(text("delete from users where id = :user_id"), {"user_id": user_id})
            cleanup.commit()


def test_non_object_limit_plus_one_rows_cannot_starve_valid_cleanup_receipt() -> None:
    user_id = uuid.uuid4()
    page_limit = 2
    array_ids = [uuid.uuid4() for _ in range(page_limit + 1)]
    malformed_object_id = uuid.uuid4()
    valid_id = uuid.uuid4()
    ordered_ids = [*array_ids, malformed_object_id, valid_id]
    base_time = datetime(2026, 8, 28, 9, 0, tzinfo=UTC)
    plans: dict[uuid.UUID, object] = {
        job_id: [{VIDEO_POSTER_BACKFILL_CLEANUP_FIELD: []}] for job_id in array_ids
    }
    plans[malformed_object_id] = {VIDEO_POSTER_BACKFILL_CLEANUP_FIELD: "malformed"}
    plans[valid_id] = {
        VIDEO_POSTER_BACKFILL_CLEANUP_FIELD: [
            {
                "old_path": "jobs/job/output.mp4.poster.backfill-"
                "00000000-0000-0000-0000-000000000011.jpg",
                "replacement_path": "jobs/job/replacement.mp4",
            }
        ]
    }

    try:
        with sync_session() as db:
            db.add(User(id=user_id, email=f"poster-cleanup-json-type-{user_id}@test.local"))
            for job_id in ordered_ids:
                db.add(
                    Job(
                        id=job_id,
                        user_id=user_id,
                        status="done",
                        job_type="template",
                        raw_storage_path=f"{user_id}/{job_id}/raw.mp4",
                        assembly_plan=plans[job_id],
                    )
                )
            db.commit()
            db.execute(
                text("update jobs set updated_at = :updated_at where id = :job_id"),
                [
                    {
                        "job_id": job_id,
                        "updated_at": base_time + timedelta(seconds=index),
                    }
                    for index, job_id in enumerate(ordered_ids)
                ],
            )
            db.commit()

        with sync_session() as db:
            assert jobs_with_video_poster_cleanup_receipts(db, limit=page_limit) == [
                malformed_object_id,
                valid_id,
            ]
    finally:
        with sync_session() as cleanup:
            cleanup.execute(
                text("delete from jobs where user_id = :user_id"),
                {"user_id": user_id},
            )
            cleanup.execute(text("delete from users where id = :user_id"), {"user_id": user_id})
            cleanup.commit()


def test_variant_poster_survives_a_fresh_database_session(monkeypatch) -> None:
    user_id = uuid.uuid4()
    job_id = uuid.uuid4()
    source = f"generative-jobs/{job_id}/variant.mp4"
    old_poster = backfill._backfill_poster_object_path(source)
    poster = backfill._backfill_poster_object_path(source)
    assert old_poster != poster

    with sync_session() as db:
        db.add(User(id=user_id, email=f"poster-backfill-{user_id}@test.local"))
        job = Job(
            id=job_id,
            user_id=user_id,
            status="variants_ready",
            job_type="generative",
            raw_storage_path=f"{user_id}/{job_id}/raw.mp4",
            assembly_plan={
                "variants": [
                    {
                        "variant_id": "guided_story",
                        "render_status": "ready",
                        "render_generation_id": "generation-1",
                        "video_path": source,
                        "poster_path": old_poster,
                    }
                ]
            },
        )
        db.add(job)
        db.commit()
        candidate = _make_candidate(
            job,
            kind="variant",
            raw_source=source,
            poster_value=old_poster,
            poster_field="poster_path",
            variant_id="guided_story",
            variant_index=0,
            render_identity="generation-1",
        )

    assert candidate is not None
    monkeypatch.setattr(backfill, "sync_session", sync_session)
    deleted: list[str] = []
    monkeypatch.setattr(backfill, "upload_bytes_public_read", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(backfill, "object_exists", lambda path: path == poster)
    monkeypatch.setattr(
        "app.storage.object_exists_once",
        lambda path, *, timeout_s: path in {old_poster, poster},
    )
    monkeypatch.setattr(
        "app.storage.delete_object_once",
        lambda path, *, timeout_s: deleted.append(path) or True,
    )

    try:
        assert _persist_poster(candidate, poster) == "generated"

        with sync_session() as verify:
            persisted = verify.get(Job, job_id)
            assert persisted is not None
            assert persisted.assembly_plan["variants"][0]["poster_path"] == poster
            assert persisted.assembly_plan[backfill._CLEANUP_RECEIPTS_FIELD] == [
                {"old_path": old_poster, "replacement_path": poster}
            ]

        assert backfill._publish_reserved_poster(candidate, poster, b"jpeg") == "generated"
        assert deleted == [old_poster]

        with sync_session() as verify:
            persisted = verify.get(Job, job_id)
            assert persisted is not None
            assert backfill._CLEANUP_RECEIPTS_FIELD not in persisted.assembly_plan
    finally:
        with sync_session() as cleanup:
            # Raw cleanup avoids traversing ORM relationships whose tables may
            # not exist in a developer's partially migrated local test DB.
            cleanup.execute(text("delete from jobs where id = :job_id"), {"job_id": job_id})
            cleanup.execute(text("delete from users where id = :user_id"), {"user_id": user_id})
            cleanup.commit()


def test_owned_legacy_output_url_is_canonicalized_with_its_poster(monkeypatch) -> None:
    user_id = uuid.uuid4()
    job_id = uuid.uuid4()
    source = f"jobs/{job_id}/clip-1.mp4"
    signed_url = f"https://storage.googleapis.com/nova-test/{source}?X-Goog-Signature=old"
    poster = f"{source}.poster.jpg"

    with sync_session() as db:
        db.add(User(id=user_id, email=f"poster-backfill-url-{user_id}@test.local"))
        job = Job(
            id=job_id,
            user_id=user_id,
            status="done",
            job_type="template",
            raw_storage_path=f"{user_id}/{job_id}/raw.mp4",
            assembly_plan={"output_url": signed_url},
        )
        db.add(job)
        db.commit()
        candidate = _make_candidate(
            job,
            kind="job_output",
            raw_source=signed_url,
            poster_value=None,
            poster_field="poster_path",
        )

    assert candidate is not None
    assert candidate.source_key == source
    monkeypatch.setattr(backfill, "sync_session", sync_session)

    try:
        assert _persist_poster(candidate, poster) == "generated"

        with sync_session() as verify:
            persisted = verify.get(Job, job_id)
            assert persisted is not None
            assert persisted.assembly_plan["output_path"] == source
            assert persisted.assembly_plan["poster_path"] == poster
    finally:
        with sync_session() as cleanup:
            cleanup.execute(text("delete from jobs where id = :job_id"), {"job_id": job_id})
            cleanup.execute(text("delete from users where id = :user_id"), {"user_id": user_id})
            cleanup.commit()


def test_owned_legacy_variant_url_is_canonicalized_with_its_poster(monkeypatch) -> None:
    user_id = uuid.uuid4()
    job_id = uuid.uuid4()
    source = f"generative-jobs/{job_id}/variant.mp4"
    signed_url = f"https://storage.googleapis.com/nova-test/{source}?X-Goog-Signature=old"
    poster = backfill._backfill_poster_object_path(source)

    with sync_session() as db:
        db.add(User(id=user_id, email=f"poster-backfill-variant-url-{user_id}@test.local"))
        job = Job(
            id=job_id,
            user_id=user_id,
            status="variants_ready",
            job_type="generative",
            raw_storage_path=f"{user_id}/{job_id}/raw.mp4",
            assembly_plan={
                "variants": [
                    {
                        "variant_id": "guided_story",
                        "render_status": "ready",
                        "render_generation_id": "generation-1",
                        "output_url": signed_url,
                    }
                ]
            },
        )
        db.add(job)
        db.commit()
        candidate = _make_candidate(
            job,
            kind="variant",
            raw_source=signed_url,
            poster_value=None,
            poster_field="poster_path",
            variant_id="guided_story",
            variant_index=0,
            render_identity="generation-1",
        )

    assert candidate is not None
    assert candidate.source_key == source
    monkeypatch.setattr(backfill, "sync_session", sync_session)

    try:
        assert _persist_poster(candidate, poster) == "generated"

        with sync_session() as verify:
            persisted = verify.get(Job, job_id)
            assert persisted is not None
            variant = persisted.assembly_plan["variants"][0]
            assert variant["video_path"] == source
            assert variant["poster_path"] == poster
    finally:
        with sync_session() as cleanup:
            cleanup.execute(text("delete from jobs where id = :job_id"), {"job_id": job_id})
            cleanup.execute(text("delete from users where id = :user_id"), {"user_id": user_id})
            cleanup.commit()


def test_owned_legacy_job_clip_url_is_preserved_when_thumbnail_is_added(monkeypatch) -> None:
    user_id = uuid.uuid4()
    job_id = uuid.uuid4()
    clip_id = uuid.uuid4()
    source = f"jobs/{job_id}/clip-1.mp4"
    signed_url = f"https://storage.googleapis.com/nova-test/{source}?X-Goog-Signature=old"
    poster = f"{source}.poster.jpg"

    with sync_session() as db:
        db.add(User(id=user_id, email=f"poster-backfill-clip-{user_id}@test.local"))
        job = Job(
            id=job_id,
            user_id=user_id,
            status="done",
            job_type="template",
            raw_storage_path=f"{user_id}/{job_id}/raw.mp4",
            assembly_plan={},
        )
        clip = JobClip(
            id=clip_id,
            job_id=job_id,
            rank=1,
            hook_score=0.9,
            engagement_score=0.8,
            combined_score=0.85,
            start_s=0.0,
            end_s=10.0,
            video_path=signed_url,
            thumbnail_path=None,
            render_status="ready",
        )
        db.add_all([job, clip])
        db.commit()
        candidate = _make_candidate(
            job,
            kind="job_clip",
            raw_source=signed_url,
            poster_value=None,
            poster_field="thumbnail_path",
            clip_id=clip_id,
        )

    assert candidate is not None
    assert candidate.source_key == source
    monkeypatch.setattr(backfill, "sync_session", sync_session)

    try:
        assert _persist_poster(candidate, poster) == "generated"

        with sync_session() as verify:
            persisted = verify.get(JobClip, clip_id)
            assert persisted is not None
            assert persisted.video_path == signed_url
            assert persisted.thumbnail_path == poster
    finally:
        with sync_session() as cleanup:
            cleanup.execute(text("delete from job_clips where id = :clip_id"), {"clip_id": clip_id})
            cleanup.execute(text("delete from jobs where id = :job_id"), {"job_id": job_id})
            cleanup.execute(text("delete from users where id = :user_id"), {"user_id": user_id})
            cleanup.commit()


def test_strict_repair_replaces_observed_stale_variant_poster(monkeypatch) -> None:
    user_id = uuid.uuid4()
    job_id = uuid.uuid4()
    source = f"generative-jobs/{job_id}/variant.mp4"
    stale_poster = f"generative-jobs/{job_id}/old.mp4.poster.jpg"
    expected_poster = backfill._backfill_poster_object_path(source)

    with sync_session() as db:
        db.add(User(id=user_id, email=f"poster-backfill-stale-{user_id}@test.local"))
        job = Job(
            id=job_id,
            user_id=user_id,
            status="variants_ready",
            job_type="generative",
            raw_storage_path=f"{user_id}/{job_id}/raw.mp4",
            assembly_plan={
                "variants": [
                    {
                        "variant_id": "guided_story",
                        "render_status": "ready",
                        "render_generation_id": "generation-1",
                        "video_path": source,
                        "poster_path": stale_poster,
                    }
                ]
            },
        )
        db.add(job)
        db.commit()
        monkeypatch.setattr(backfill, "object_exists", lambda _path: False)
        candidate = _make_candidate(
            job,
            kind="variant",
            raw_source=source,
            poster_value=stale_poster,
            poster_field="poster_path",
            variant_id="guided_story",
            variant_index=0,
            render_identity="generation-1",
            verify_existing=True,
        )

    assert candidate is not None
    assert candidate.already_present is False
    assert candidate.observed_poster_value == stale_poster
    monkeypatch.setattr(backfill, "sync_session", sync_session)

    try:
        assert _persist_poster(candidate, expected_poster) == "generated"

        with sync_session() as verify:
            persisted = verify.get(Job, job_id)
            assert persisted is not None
            assert persisted.assembly_plan["variants"][0]["poster_path"] == expected_poster
            assert backfill._CLEANUP_RECEIPTS_FIELD not in persisted.assembly_plan
    finally:
        with sync_session() as cleanup:
            cleanup.execute(text("delete from jobs where id = :job_id"), {"job_id": job_id})
            cleanup.execute(text("delete from users where id = :user_id"), {"user_id": user_id})
            cleanup.commit()


def test_strict_repair_does_not_overwrite_concurrent_variant_poster(monkeypatch) -> None:
    user_id = uuid.uuid4()
    job_id = uuid.uuid4()
    source = f"generative-jobs/{job_id}/variant.mp4"
    stale_poster = f"generative-jobs/{job_id}/old.mp4.poster.jpg"
    concurrent_poster = f"generative-jobs/{job_id}/winner.mp4.poster.jpg"

    with sync_session() as db:
        db.add(User(id=user_id, email=f"poster-backfill-race-{user_id}@test.local"))
        job = Job(
            id=job_id,
            user_id=user_id,
            status="variants_ready",
            job_type="generative",
            raw_storage_path=f"{user_id}/{job_id}/raw.mp4",
            assembly_plan={
                "variants": [
                    {
                        "variant_id": "guided_story",
                        "render_status": "ready",
                        "render_generation_id": "generation-1",
                        "video_path": source,
                        "poster_path": stale_poster,
                    }
                ]
            },
        )
        db.add(job)
        db.commit()
        monkeypatch.setattr(backfill, "object_exists", lambda _path: False)
        candidate = _make_candidate(
            job,
            kind="variant",
            raw_source=source,
            poster_value=stale_poster,
            poster_field="poster_path",
            variant_id="guided_story",
            variant_index=0,
            render_identity="generation-1",
            verify_existing=True,
        )

    assert candidate is not None
    with sync_session() as concurrent:
        current = concurrent.get(Job, job_id)
        assert current is not None
        plan = dict(current.assembly_plan)
        variants = [dict(variant) for variant in plan["variants"]]
        variants[0]["poster_path"] = concurrent_poster
        plan["variants"] = variants
        current.assembly_plan = plan
        concurrent.commit()

    monkeypatch.setattr(backfill, "sync_session", sync_session)
    try:
        assert _persist_poster(candidate, f"{source}.poster.jpg") == "stale_race"

        with sync_session() as verify:
            persisted = verify.get(Job, job_id)
            assert persisted is not None
            assert persisted.assembly_plan["variants"][0]["poster_path"] == concurrent_poster
    finally:
        with sync_session() as cleanup:
            cleanup.execute(text("delete from jobs where id = :job_id"), {"job_id": job_id})
            cleanup.execute(text("delete from users where id = :user_id"), {"user_id": user_id})
            cleanup.commit()


def test_strict_repair_does_not_overwrite_concurrent_job_output_poster(monkeypatch) -> None:
    user_id = uuid.uuid4()
    job_id = uuid.uuid4()
    source = f"jobs/{job_id}/output.mp4"
    concurrent_poster = f"{source}.poster.jpg"

    with sync_session() as db:
        db.add(User(id=user_id, email=f"poster-backfill-output-race-{user_id}@test.local"))
        job = Job(
            id=job_id,
            user_id=user_id,
            status="done",
            job_type="template",
            raw_storage_path=f"{user_id}/{job_id}/raw.mp4",
            assembly_plan={"output_path": source},
        )
        db.add(job)
        db.commit()
        candidate = _make_candidate(
            job,
            kind="job_output",
            raw_source=source,
            poster_value=None,
            poster_field="poster_path",
            verify_existing=True,
        )

    assert candidate is not None
    with sync_session() as concurrent:
        current = concurrent.get(Job, job_id)
        assert current is not None
        current.assembly_plan = {
            **current.assembly_plan,
            "poster_path": concurrent_poster,
        }
        concurrent.commit()

    monkeypatch.setattr(backfill, "sync_session", sync_session)
    try:
        generated = backfill._backfill_poster_object_path(source)
        assert _persist_poster(candidate, generated) == "stale_race"

        with sync_session() as verify:
            persisted = verify.get(Job, job_id)
            assert persisted is not None
            assert persisted.assembly_plan["poster_path"] == concurrent_poster
    finally:
        with sync_session() as cleanup:
            cleanup.execute(text("delete from jobs where id = :job_id"), {"job_id": job_id})
            cleanup.execute(text("delete from users where id = :user_id"), {"user_id": user_id})
            cleanup.commit()


def test_strict_repair_does_not_overwrite_concurrent_job_clip_thumbnail(monkeypatch) -> None:
    user_id = uuid.uuid4()
    job_id = uuid.uuid4()
    clip_id = uuid.uuid4()
    source = f"jobs/{job_id}/clip.mp4"
    concurrent_poster = f"{source}.poster.jpg"

    with sync_session() as db:
        db.add(User(id=user_id, email=f"poster-backfill-clip-race-{user_id}@test.local"))
        job = Job(
            id=job_id,
            user_id=user_id,
            status="done",
            job_type="default",
            raw_storage_path=f"{user_id}/{job_id}/raw.mp4",
            assembly_plan={},
        )
        clip = JobClip(
            id=clip_id,
            job_id=job_id,
            rank=1,
            hook_score=0.9,
            engagement_score=0.8,
            combined_score=0.85,
            start_s=0.0,
            end_s=10.0,
            video_path=source,
            thumbnail_path=None,
            render_status="ready",
        )
        db.add_all([job, clip])
        db.commit()
        candidate = _make_candidate(
            job,
            kind="job_clip",
            raw_source=source,
            poster_value=None,
            poster_field="thumbnail_path",
            clip_id=clip_id,
            verify_existing=True,
        )

    assert candidate is not None
    with sync_session() as concurrent:
        current = concurrent.get(JobClip, clip_id)
        assert current is not None
        current.thumbnail_path = concurrent_poster
        concurrent.commit()

    monkeypatch.setattr(backfill, "sync_session", sync_session)
    try:
        generated = backfill._backfill_poster_object_path(source)
        assert _persist_poster(candidate, generated) == "stale_race"

        with sync_session() as verify:
            persisted = verify.get(JobClip, clip_id)
            assert persisted is not None
            assert persisted.thumbnail_path == concurrent_poster
    finally:
        with sync_session() as cleanup:
            cleanup.execute(text("delete from job_clips where id = :clip_id"), {"clip_id": clip_id})
            cleanup.execute(text("delete from jobs where id = :job_id"), {"job_id": job_id})
            cleanup.execute(text("delete from users where id = :user_id"), {"user_id": user_id})
            cleanup.commit()


def test_failed_storage_recheck_classifies_concurrent_job_deletion_as_stale(
    monkeypatch,
) -> None:
    user_id = uuid.uuid4()
    job_id = uuid.uuid4()
    source = f"jobs/{job_id}/output.mp4"

    with sync_session() as db:
        db.add(User(id=user_id, email=f"poster-backfill-deleted-{user_id}@test.local"))
        job = Job(
            id=job_id,
            user_id=user_id,
            status="done",
            job_type="template",
            raw_storage_path=f"{user_id}/{job_id}/raw.mp4",
            assembly_plan={"output_path": source},
        )
        db.add(job)
        db.commit()
        candidate = _make_candidate(
            job,
            kind="job_output",
            raw_source=source,
            poster_value=None,
            poster_field="poster_path",
        )
    assert candidate is not None

    with sync_session() as concurrent:
        concurrent.execute(text("delete from jobs where id = :job_id"), {"job_id": job_id})
        concurrent.commit()

    monkeypatch.setattr(backfill, "sync_session", sync_session)
    try:
        assert backfill._candidate_is_still_current(candidate) is False
    finally:
        with sync_session() as cleanup:
            cleanup.execute(text("delete from users where id = :user_id"), {"user_id": user_id})
            cleanup.commit()


def test_failed_storage_recheck_classifies_variant_source_and_generation_change_as_stale(
    monkeypatch,
) -> None:
    user_id = uuid.uuid4()
    job_id = uuid.uuid4()
    source = f"generative-jobs/{job_id}/variant-generation-1.mp4"
    next_source = f"generative-jobs/{job_id}/variant-generation-2.mp4"

    with sync_session() as db:
        db.add(User(id=user_id, email=f"poster-backfill-generation-{user_id}@test.local"))
        job = Job(
            id=job_id,
            user_id=user_id,
            status="variants_ready",
            job_type="generative",
            raw_storage_path=f"{user_id}/{job_id}/raw.mp4",
            assembly_plan={
                "variants": [
                    {
                        "variant_id": "guided_story",
                        "render_status": "ready",
                        "render_generation_id": "generation-1",
                        "video_path": source,
                    }
                ]
            },
        )
        db.add(job)
        db.commit()
        candidate = _make_candidate(
            job,
            kind="variant",
            raw_source=source,
            poster_value=None,
            poster_field="poster_path",
            variant_id="guided_story",
            variant_index=0,
            render_identity="generation-1",
        )
    assert candidate is not None

    with sync_session() as concurrent:
        current = concurrent.get(Job, job_id)
        assert current is not None
        current.assembly_plan = {
            "variants": [
                {
                    "variant_id": "guided_story",
                    "render_status": "ready",
                    "render_generation_id": "generation-2",
                    "video_path": next_source,
                }
            ]
        }
        concurrent.commit()

    monkeypatch.setattr(backfill, "sync_session", sync_session)
    try:
        assert backfill._candidate_is_still_current(candidate) is False
    finally:
        with sync_session() as cleanup:
            cleanup.execute(text("delete from jobs where id = :job_id"), {"job_id": job_id})
            cleanup.execute(text("delete from users where id = :user_id"), {"user_id": user_id})
            cleanup.commit()


def test_main_strict_multi_page_repair_is_idempotent(monkeypatch, capsys) -> None:
    user_id = uuid.uuid4()
    mode = f"poster_backfill_test_{uuid.uuid4().hex}"
    job_ids = [uuid.uuid4(), uuid.uuid4()]
    sources = {job_id: f"jobs/{job_id}/output.mp4" for job_id in job_ids}
    uploaded: set[str] = set()

    with sync_session() as db:
        db.add(User(id=user_id, email=f"poster-backfill-paging-{user_id}@test.local"))
        for job_id in job_ids:
            db.add(
                Job(
                    id=job_id,
                    user_id=user_id,
                    status="done",
                    job_type=mode,
                    mode=mode,
                    raw_storage_path=f"{user_id}/{job_id}/raw.mp4",
                    assembly_plan={"output_path": sources[job_id]},
                )
            )
        db.commit()

    monkeypatch.setattr(
        backfill,
        "_extract_candidate_poster",
        lambda _source: (b"jpeg", "generated"),
    )
    monkeypatch.setattr(
        backfill,
        "upload_bytes_public_read",
        lambda _data, path, content_type: uploaded.add(path),
    )
    monkeypatch.setattr(backfill, "object_exists", lambda path: path in uploaded)
    # This test owns pagination + DB idempotence. Keep the now-stricter source
    # and JPEG verification hermetic; their storage contracts have dedicated
    # unit coverage in test_backfill_video_posters.py.
    monkeypatch.setattr(
        backfill,
        "_candidate_has_usable_source",
        lambda candidate: candidate.source_key in sources.values(),
    )
    monkeypatch.setattr(
        backfill,
        "_candidate_has_usable_poster",
        lambda candidate, *, verify_storage: bool(
            candidate.existing_poster_key
            and candidate.existing_poster_key in uploaded
            and verify_storage
        ),
    )

    try:
        assert (
            backfill.main(["--mode", mode, "--batch-size", "1", "--exclude-synthetic", "--strict"])
            == 0
        )
        first_output = capsys.readouterr().out
        assert "generated=2" in first_output

        assert (
            backfill.main(
                [
                    "--dry-run",
                    "--mode",
                    mode,
                    "--batch-size",
                    "1",
                    "--exclude-synthetic",
                    "--strict",
                ]
            )
            == 0
        )
        second_output = capsys.readouterr().out
        assert "already_present=2" in second_output
        assert "would_generate=0" in second_output
    finally:
        with sync_session() as cleanup:
            cleanup.execute(text("delete from jobs where user_id = :user_id"), {"user_id": user_id})
            cleanup.execute(text("delete from users where id = :user_id"), {"user_id": user_id})
            cleanup.commit()
