from __future__ import annotations

import uuid
from datetime import UTC, datetime
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image

import app.services.video_poster_cleanup as poster_cleanup
import scripts.backfill_video_posters as backfill
from app.services.template_poster import PosterExtractionError
from app.storage import ObjectMetadata
from scripts.backfill_video_posters import (
    PosterCandidate,
    _candidate_has_usable_poster,
    _candidates_for_job,
    _extract_candidate_poster,
    _job_batches,
    _make_candidate,
    _parse_cursor,
    _persist_poster,
    _publish_reserved_poster,
    _source_candidate,
)


def _job() -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        status="variants_ready",
        job_type="generative",
        created_at=datetime(2026, 8, 25, tzinfo=UTC),
        assembly_plan={
            "variants": [
                {
                    "variant_id": "song_text",
                    "render_status": "ready",
                    "video_path": "generative-jobs/PLACEHOLDER/video.mp4",
                }
            ]
        },
    )


def _jpeg_bytes() -> bytes:
    buffer = BytesIO()
    Image.new("RGB", (2, 2), "white").save(buffer, format="JPEG")
    return buffer.getvalue()


@pytest.fixture(autouse=True)
def _strict_source_metadata_is_healthy_by_default(monkeypatch):
    """Existing strict tests focus on poster/DB races unless they override this."""

    def _metadata(path: str, *, timeout_s: float) -> ObjectMetadata:
        assert timeout_s == backfill._SOURCE_METADATA_TIMEOUT_S
        return ObjectMetadata(
            path=path,
            generation="11",
            etag="source-etag",
            size=1024,
            content_type="video/mp4",
        )

    monkeypatch.setattr(backfill, "object_metadata_once", _metadata)


def _mock_strict_poster(
    monkeypatch,
    *,
    poster: str | set[str],
    data: bytes,
    size: int | None = None,
    content_type: str = "image/jpeg",
) -> None:
    posters = {poster} if isinstance(poster, str) else poster

    def _metadata(path: str) -> ObjectMetadata:
        assert path in posters
        return ObjectMetadata(
            path=path,
            generation="7",
            etag="etag",
            size=len(data) if size is None else size,
            content_type=content_type,
        )

    monkeypatch.setattr(
        backfill,
        "object_metadata",
        _metadata,
    )

    def _download(path: str, local_path: str, *, generation: str) -> None:
        assert path in posters
        assert generation == "7"
        Path(local_path).write_bytes(data)

    monkeypatch.setattr(backfill, "download_generation_to_file", _download)


def test_cursor_round_trip_is_keyset_safe() -> None:
    created_at = datetime(2026, 8, 25, 12, 30, tzinfo=UTC)
    job_id = uuid.uuid4()
    assert _parse_cursor(f"{created_at.isoformat()}|{job_id}") == (created_at, job_id)


def test_candidate_discovery_skips_unrequested_preview_bases() -> None:
    job = _job()
    job.assembly_plan["variants"][0]["video_path"] = f"generative-jobs/{job.id}/video.mp4"
    clips = [
        SimpleNamespace(
            id=uuid.uuid4(),
            job_id=job.id,
            rank=1,
            created_at=job.created_at,
            render_status="ready",
            video_path=f"auto-music-jobs/{job.id}/variant.mp4",
            thumbnail_path=None,
        )
    ]

    candidates = list(_candidates_for_job(job, clips, include_preview_bases=False))

    assert [candidate.kind for candidate in candidates] == ["variant", "job_clip"]
    assert candidates[0].source_key == f"generative-jobs/{job.id}/video.mp4"


def test_candidate_discovery_excludes_admin_lyrics_previews() -> None:
    job = _job()
    job.job_type = "lyrics_preview"
    job.assembly_plan = {
        "output_url": (
            "https://storage.googleapis.com/nova-test/"
            f"music-lyrics-previews/track/{job.id}/lyrics-preview.mp4"
        )
    }

    assert list(_candidates_for_job(job, [], include_preview_bases=False)) == []


def test_candidate_discovery_excludes_non_library_ready_jobs() -> None:
    job = _job()
    job.status = "variants_failed"
    job.assembly_plan = {
        "output_path": f"generative-jobs/{job.id}/expired-output.mp4",
        "variants": [
            {
                "variant_id": "old-ready-variant",
                "render_status": "ready",
                "video_path": f"generative-jobs/{job.id}/expired-variant.mp4",
            }
        ],
    }
    clips = [
        SimpleNamespace(
            id=uuid.uuid4(),
            job_id=job.id,
            rank=1,
            created_at=job.created_at,
            render_status="ready",
            video_path=f"jobs/{job.id}/expired-clip.mp4",
            thumbnail_path=None,
        )
    ]

    assert list(_candidates_for_job(job, clips, include_preview_bases=True)) == []


def test_candidate_discovery_blocks_ready_variant_with_missing_source() -> None:
    job = _job()
    job.assembly_plan = {
        "variants": [
            {
                "variant_id": "blank",
                "render_status": "ready",
                "video_path": None,
            }
        ]
    }

    candidates = list(_candidates_for_job(job, [], include_preview_bases=False))

    assert len(candidates) == 1
    assert candidates[0].kind == "variant"
    assert candidates[0].source_key is None
    assert candidates[0].blocked_outcome == "failed"


def test_candidate_discovery_blocks_ready_job_with_no_preview_entries() -> None:
    job = _job()
    job.assembly_plan = {"variants": []}

    candidates = list(_candidates_for_job(job, [], include_preview_bases=False))

    assert len(candidates) == 1
    assert candidates[0].kind == "job_preview_missing"
    assert candidates[0].blocked_outcome == "failed"


def test_job_batches_adds_synthetic_owner_filter() -> None:
    class EmptyResult:
        def scalars(self):
            return self

        def all(self):
            return []

    class Session:
        statement = None

        def execute(self, statement):
            self.statement = statement
            return EmptyResult()

    db = Session()

    assert (
        list(
            _job_batches(
                db,
                batch_size=25,
                job_id=None,
                mode=None,
                cursor=None,
                exclude_synthetic=True,
            )
        )
        == []
    )
    assert db.statement is not None
    compiled = db.statement.compile()
    assert "jobs.user_id !=" in str(compiled)
    assert backfill.SYNTHETIC_USER_ID in compiled.params.values()


def test_extract_candidate_poster_creates_no_storage_object(monkeypatch) -> None:
    calls: dict[str, object] = {}
    monkeypatch.setattr(
        "scripts.backfill_video_posters.download_to_file",
        lambda source, local: calls.update(source=source, local=local),
    )
    monkeypatch.setattr(
        "scripts.backfill_video_posters.extract_poster_bytes",
        lambda local: b"jpeg",
    )

    source = f"jobs/{uuid.uuid4()}/output.mp4"
    poster_bytes, outcome = _extract_candidate_poster(source)

    assert outcome == "generated"
    assert poster_bytes == b"jpeg"
    poster_key = backfill._backfill_poster_object_path(source)
    assert backfill._is_backfill_poster_path(poster_key, source)


def test_source_candidate_classifies_legacy_and_unowned_sources() -> None:
    job = _job()

    assert _source_candidate(job, "https://storage.example/old.mp4") == (
        None,
        "unresolvable_legacy_url",
    )
    assert _source_candidate(job, "jobs/another-job/output.mp4") == (
        None,
        "skipped_not_owned",
    )


def test_blocked_candidate_recheck_tracks_raw_source_snapshot(monkeypatch) -> None:
    job = _job()
    legacy_source = "https://storage.example/legacy-output.mp4"
    job.assembly_plan = {"output_url": legacy_source}
    candidate = _make_candidate(
        job,
        kind="job_output",
        raw_source=legacy_source,
        poster_value=None,
        poster_field="poster_path",
    )
    assert candidate is not None
    assert candidate.blocked_outcome == "unresolvable_legacy_url"
    assert candidate.observed_source_value == legacy_source

    class Session:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def get(self, *_args, **_kwargs):
            return job

    monkeypatch.setattr(backfill, "sync_session", lambda: Session())

    assert backfill._candidate_is_still_current(candidate) is True

    job.assembly_plan = {"output_path": f"generative-jobs/{job.id}/replacement.mp4"}
    assert backfill._candidate_is_still_current(candidate) is False


def test_strict_candidate_rejects_poster_that_does_not_match_its_source(monkeypatch) -> None:
    job = _job()
    source = f"generative-jobs/{job.id}/output.mp4"
    wrong_poster = f"generative-jobs/{job.id}/different.mp4.poster.jpg"
    exists_calls: list[str] = []
    monkeypatch.setattr(backfill, "object_exists", exists_calls.append)

    candidate = _make_candidate(
        job,
        kind="job_output",
        raw_source=source,
        poster_value=wrong_poster,
        poster_field="poster_path",
        verify_existing=True,
    )

    assert candidate is not None
    assert candidate.already_present is False
    assert candidate.observed_poster_value == wrong_poster
    assert exists_calls == []


def test_strict_candidate_rejects_poster_missing_from_storage(monkeypatch) -> None:
    job = _job()
    source = f"generative-jobs/{job.id}/output.mp4"
    poster = f"{source}.poster.jpg"
    monkeypatch.setattr(
        backfill,
        "object_metadata",
        lambda _path: (_ for _ in ()).throw(FileNotFoundError(poster)),
    )

    candidate = _make_candidate(
        job,
        kind="job_output",
        raw_source=source,
        poster_value=poster,
        poster_field="poster_path",
        verify_existing=True,
    )

    assert candidate is not None
    assert candidate.already_present is True
    assert candidate.observed_poster_value == poster
    assert _candidate_has_usable_poster(candidate, verify_storage=True) is False


def test_strict_candidate_fails_closed_when_storage_verification_errors(monkeypatch) -> None:
    job = _job()
    source = f"generative-jobs/{job.id}/output.mp4"
    poster = f"{source}.poster.jpg"
    monkeypatch.setattr(
        backfill,
        "object_metadata",
        lambda _path: (_ for _ in ()).throw(RuntimeError("storage unavailable")),
    )

    candidate = _make_candidate(
        job,
        kind="job_output",
        raw_source=source,
        poster_value=poster,
        poster_field="poster_path",
        verify_existing=True,
    )

    assert candidate is not None
    assert _candidate_has_usable_poster(candidate, verify_storage=True) is None


def test_strict_candidate_accepts_existing_legacy_job_clip_thumbnail(monkeypatch) -> None:
    job = _job()
    source = f"{job.user_id}/{job.id}/task-runs/run/clip_1.mp4"
    thumbnail = f"{job.user_id}/{job.id}/task-runs/run/thumb_1.jpg"
    _mock_strict_poster(
        monkeypatch,
        poster=thumbnail,
        data=_jpeg_bytes(),
    )

    candidate = _make_candidate(
        job,
        kind="job_clip",
        raw_source=source,
        poster_value=thumbnail,
        poster_field="thumbnail_path",
        clip_id=uuid.uuid4(),
        verify_existing=True,
    )

    assert candidate is not None
    assert candidate.already_present is True
    assert candidate.existing_poster_key == thumbnail
    assert _candidate_has_usable_poster(candidate, verify_storage=True) is True


def test_strict_candidate_accepts_source_matched_poster_that_exists(monkeypatch) -> None:
    job = _job()
    source = f"generative-jobs/{job.id}/output.mp4"
    poster = backfill._backfill_poster_object_path(source)
    _mock_strict_poster(
        monkeypatch,
        poster=poster,
        data=_jpeg_bytes(),
    )

    candidate = _make_candidate(
        job,
        kind="job_output",
        raw_source=source,
        poster_value=poster,
        poster_field="poster_path",
        verify_existing=True,
    )

    assert candidate is not None
    assert candidate.already_present is True
    assert _candidate_has_usable_poster(candidate, verify_storage=True) is True


@pytest.mark.parametrize(
    ("data", "size", "content_type"),
    [
        (b"", 0, "image/jpeg"),
        (_jpeg_bytes(), None, "video/mp4"),
        (_jpeg_bytes(), None, "image/png"),
        (b"not-a-jpeg", None, "image/jpeg"),
    ],
    ids=["zero-byte", "non-image", "wrong-image-content-type", "corrupt-jpeg"],
)
def test_strict_candidate_rejects_invalid_poster_bytes_or_metadata(
    monkeypatch,
    data: bytes,
    size: int | None,
    content_type: str,
) -> None:
    job = _job()
    source = f"generative-jobs/{job.id}/output.mp4"
    poster = f"{source}.poster.jpg"
    _mock_strict_poster(
        monkeypatch,
        poster=poster,
        data=data,
        size=size,
        content_type=content_type,
    )
    candidate = _make_candidate(
        job,
        kind="job_output",
        raw_source=source,
        poster_value=poster,
        poster_field="poster_path",
        verify_existing=True,
    )

    assert candidate is not None
    assert _candidate_has_usable_poster(candidate, verify_storage=True) is False


@pytest.mark.parametrize(
    ("download_failure", "expected"),
    [
        (FileNotFoundError("generation disappeared"), False),
        (RuntimeError("download unavailable"), None),
    ],
    ids=["missing", "outage"],
)
def test_strict_candidate_classifies_generation_download_failure(
    monkeypatch,
    download_failure: Exception,
    expected: bool | None,
) -> None:
    job = _job()
    source = f"generative-jobs/{job.id}/output.mp4"
    poster = f"{source}.poster.jpg"
    jpeg = _jpeg_bytes()
    monkeypatch.setattr(
        backfill,
        "object_metadata",
        lambda path: ObjectMetadata(
            path=path,
            generation="7",
            etag="etag",
            size=len(jpeg),
            content_type="image/jpeg",
        ),
    )
    monkeypatch.setattr(
        backfill,
        "download_generation_to_file",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(download_failure),
    )
    candidate = _make_candidate(
        job,
        kind="job_output",
        raw_source=source,
        poster_value=poster,
        poster_field="poster_path",
        verify_existing=True,
    )

    assert candidate is not None
    assert _candidate_has_usable_poster(candidate, verify_storage=True) is expected


def test_candidate_discovery_includes_optional_variant_preview_bases() -> None:
    job = _job()
    source = f"generative-jobs/{job.id}"
    job.assembly_plan["variants"][0].update(
        {
            "base_video_path": f"{source}/base.mp4",
            "pre_media_overlay_video_path": f"{source}/pre-overlay.mp4",
        }
    )

    candidates = list(_candidates_for_job(job, [], include_preview_bases=True))

    assert [candidate.kind for candidate in candidates] == [
        "variant",
        "variant_base",
        "variant_pre_overlay",
    ]
    assert [candidate.poster_field for candidate in candidates] == [
        "poster_path",
        "base_poster_path",
        "pre_overlay_poster_path",
    ]


def test_extract_poster_classifies_missing_object_and_extraction_failures(monkeypatch) -> None:
    monkeypatch.setattr(
        "scripts.backfill_video_posters.download_to_file",
        lambda *_args: (_ for _ in ()).throw(RuntimeError("status code: 404")),
    )
    poster_bytes, outcome = _extract_candidate_poster("jobs/job/output.mp4")
    assert poster_bytes is None
    assert outcome == "expired_source"

    monkeypatch.setattr("scripts.backfill_video_posters.download_to_file", lambda *_args: None)
    monkeypatch.setattr(
        "scripts.backfill_video_posters.extract_poster_bytes",
        lambda *_args: (_ for _ in ()).throw(PosterExtractionError("bad video")),
    )
    poster_bytes, outcome = _extract_candidate_poster("jobs/job/output.mp4")
    assert poster_bytes is None
    assert outcome == "failed"


def test_persist_poster_rejects_stale_variant_without_commit(monkeypatch) -> None:
    job = _job()
    source = f"generative-jobs/{job.id}/output.mp4"
    job.assembly_plan["variants"][0]["video_path"] = source
    job.assembly_plan["variants"][0]["render_finished_at"] = "new"
    candidate = _make_candidate(
        job,
        kind="variant",
        raw_source=source,
        poster_value=None,
        poster_field="poster_path",
        variant_id="song_text",
        variant_index=0,
        render_identity="old",
    )
    assert candidate is not None

    class Session:
        commits = 0

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def get(self, *_args, **_kwargs):
            return job

        def commit(self):
            self.commits += 1

    session = Session()
    monkeypatch.setattr("scripts.backfill_video_posters.sync_session", lambda: session)

    assert _persist_poster(candidate, f"{source}.poster.jpg") == "stale_race"
    assert "poster_path" not in job.assembly_plan["variants"][0]
    assert session.commits == 0


def test_persist_poster_rejects_job_that_left_ready_state_before_reservation(
    monkeypatch,
) -> None:
    job = _job()
    source = f"generative-jobs/{job.id}/output.mp4"
    job.status = "processing"
    job.assembly_plan = {"output_path": source}
    candidate = _make_candidate(
        job,
        kind="job_output",
        raw_source=source,
        poster_value=None,
        poster_field="poster_path",
    )
    assert candidate is not None

    class Session:
        commits = 0

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def get(self, *_args, **_kwargs):
            return job

        def commit(self):
            self.commits += 1

    session = Session()
    monkeypatch.setattr(backfill, "sync_session", lambda: session)

    assert _persist_poster(candidate, backfill._backfill_poster_object_path(source)) == "stale_race"
    assert session.commits == 0


def test_persist_job_clip_fails_closed_on_non_object_plan_without_mutation(
    monkeypatch,
) -> None:
    job = _job()
    source = f"generative-jobs/{job.id}/legacy-clip.mp4"
    old_poster = f"{source}.poster.backfill-11111111-1111-4111-8111-111111111111.jpg"
    clip = SimpleNamespace(
        id=uuid.uuid4(),
        job_id=job.id,
        render_status="ready",
        video_path=source,
        thumbnail_path=old_poster,
    )
    candidate = _make_candidate(
        job,
        kind="job_clip",
        raw_source=source,
        poster_value=old_poster,
        poster_field="thumbnail_path",
        clip_id=clip.id,
    )
    assert candidate is not None
    corrupt_plan = ["preserve", {"forensic": True}]
    job.assembly_plan = corrupt_plan

    class Session:
        commits = 0

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def get(self, model, *_args, **_kwargs):
            return clip if model is backfill.JobClip else job

        def commit(self):
            self.commits += 1

    session = Session()
    monkeypatch.setattr(backfill, "sync_session", lambda: session)

    generated = backfill._backfill_poster_object_path(source)
    assert _persist_poster(candidate, generated) == "failed"
    assert job.assembly_plan is corrupt_plan
    assert job.assembly_plan == ["preserve", {"forensic": True}]
    assert clip.thumbnail_path == old_poster
    assert session.commits == 0


def test_persist_poster_treats_post_commit_job_deletion_as_stale_race(monkeypatch) -> None:
    job = _job()
    source = f"generative-jobs/{job.id}/output.mp4"
    job.assembly_plan = {"output_path": source}
    candidate = _make_candidate(
        job,
        kind="job_output",
        raw_source=source,
        poster_value=None,
        poster_field="poster_path",
    )
    assert candidate is not None

    class MissingResult:
        def scalar_one_or_none(self):
            return None

    class Session:
        commits = 0

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def get(self, *_args, **_kwargs):
            return job

        def commit(self):
            self.commits += 1

        def expire_all(self):
            pass

        def execute(self, *_args, **_kwargs):
            return MissingResult()

    session = Session()
    monkeypatch.setattr(backfill, "sync_session", lambda: session)
    monkeypatch.setattr(backfill, "flag_modified", lambda *_args: None)

    assert _persist_poster(candidate, f"{source}.poster.jpg") == "stale_race"
    assert session.commits == 1


def test_persist_poster_treats_post_commit_variant_supersession_as_stale_race(
    monkeypatch,
) -> None:
    job = _job()
    source = f"generative-jobs/{job.id}/output.mp4"
    next_source = f"generative-jobs/{job.id}/replacement.mp4"
    job.assembly_plan = {
        "variants": [
            {
                "variant_id": "song_text",
                "render_status": "ready",
                "render_generation_id": "generation-1",
                "video_path": source,
            }
        ]
    }
    candidate = _make_candidate(
        job,
        kind="variant",
        raw_source=source,
        poster_value=None,
        poster_field="poster_path",
        variant_id="song_text",
        variant_index=0,
        render_identity="generation-1",
    )
    assert candidate is not None

    class Result:
        def scalar_one_or_none(self):
            return job

    class Session:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def get(self, *_args, **_kwargs):
            return job

        def commit(self):
            job.assembly_plan = {
                "variants": [
                    {
                        "variant_id": "song_text",
                        "render_status": "ready",
                        "render_generation_id": "generation-2",
                        "video_path": next_source,
                        "poster_path": f"{next_source}.poster.jpg",
                    }
                ]
            }

        def expire_all(self):
            pass

        def execute(self, *_args, **_kwargs):
            return Result()

    monkeypatch.setattr(backfill, "sync_session", Session)
    monkeypatch.setattr(backfill, "flag_modified", lambda *_args: None)

    assert _persist_poster(candidate, backfill._backfill_poster_object_path(source)) == (
        "stale_race"
    )


def test_persist_poster_keeps_genuine_post_commit_jsonb_noop_as_failed(monkeypatch) -> None:
    job = _job()
    source = f"generative-jobs/{job.id}/output.mp4"
    original_plan = {
        "variants": [
            {
                "variant_id": "song_text",
                "render_status": "ready",
                "render_generation_id": "generation-1",
                "video_path": source,
            }
        ]
    }
    job.assembly_plan = original_plan
    candidate = _make_candidate(
        job,
        kind="variant",
        raw_source=source,
        poster_value=None,
        poster_field="poster_path",
        variant_id="song_text",
        variant_index=0,
        render_identity="generation-1",
    )
    assert candidate is not None

    class Result:
        def scalar_one_or_none(self):
            return job

    class Session:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def get(self, *_args, **_kwargs):
            return job

        def commit(self):
            job.assembly_plan = {"variants": [dict(original_plan["variants"][0])]}

        def expire_all(self):
            pass

        def execute(self, *_args, **_kwargs):
            return Result()

    monkeypatch.setattr(backfill, "sync_session", Session)
    monkeypatch.setattr(backfill, "flag_modified", lambda *_args: None)

    assert _persist_poster(candidate, backfill._backfill_poster_object_path(source)) == "failed"


def test_persist_poster_treats_post_commit_ready_state_transition_as_stale_race(
    monkeypatch,
) -> None:
    job = _job()
    source = f"generative-jobs/{job.id}/output.mp4"
    job.assembly_plan = {"output_path": source}
    candidate = _make_candidate(
        job,
        kind="job_output",
        raw_source=source,
        poster_value=None,
        poster_field="poster_path",
    )
    assert candidate is not None

    class Result:
        def scalar_one_or_none(self):
            return job

    class Session:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def get(self, *_args, **_kwargs):
            return job

        def commit(self):
            job.status = "processing"

        def expire_all(self):
            pass

        def execute(self, *_args, **_kwargs):
            return Result()

    monkeypatch.setattr(backfill, "sync_session", Session)
    monkeypatch.setattr(backfill, "flag_modified", lambda *_args: None)

    assert _persist_poster(candidate, backfill._backfill_poster_object_path(source)) == "stale_race"


def test_persist_poster_treats_post_commit_job_output_supersession_as_stale_race(
    monkeypatch,
) -> None:
    job = _job()
    source = f"generative-jobs/{job.id}/output.mp4"
    next_source = f"generative-jobs/{job.id}/replacement.mp4"
    job.assembly_plan = {"output_path": source}
    candidate = _make_candidate(
        job,
        kind="job_output",
        raw_source=source,
        poster_value=None,
        poster_field="poster_path",
    )
    assert candidate is not None

    class Result:
        def scalar_one_or_none(self):
            return job

    class Session:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def get(self, *_args, **_kwargs):
            return job

        def commit(self):
            job.assembly_plan = {
                "output_path": next_source,
                "poster_path": f"{next_source}.poster.jpg",
            }

        def expire_all(self):
            pass

        def execute(self, *_args, **_kwargs):
            return Result()

    monkeypatch.setattr(backfill, "sync_session", Session)
    monkeypatch.setattr(backfill, "flag_modified", lambda *_args: None)

    assert _persist_poster(candidate, backfill._backfill_poster_object_path(source)) == (
        "stale_race"
    )


def test_persist_poster_treats_post_commit_clip_supersession_as_stale_race(
    monkeypatch,
) -> None:
    job = _job()
    source = f"generative-jobs/{job.id}/clip.mp4"
    next_source = f"generative-jobs/{job.id}/replacement-clip.mp4"
    clip = SimpleNamespace(
        id=uuid.uuid4(),
        job_id=job.id,
        render_status="ready",
        video_path=source,
        thumbnail_path=None,
    )
    candidate = _make_candidate(
        job,
        kind="job_clip",
        raw_source=source,
        poster_value=None,
        poster_field="thumbnail_path",
        clip_id=clip.id,
    )
    assert candidate is not None

    class Result:
        def __init__(self, value):
            self.value = value

        def scalar_one_or_none(self):
            return self.value

    class Session:
        def __init__(self):
            self.results = iter((clip, job))

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def get(self, model, *_args, **_kwargs):
            return clip if model is backfill.JobClip else job

        def commit(self):
            clip.video_path = next_source
            clip.thumbnail_path = f"{next_source}.poster.jpg"

        def expire_all(self):
            pass

        def execute(self, *_args, **_kwargs):
            return Result(next(self.results))

    session = Session()
    monkeypatch.setattr(backfill, "sync_session", lambda: session)

    assert _persist_poster(candidate, backfill._backfill_poster_object_path(source)) == (
        "stale_race"
    )


def test_publish_uploads_only_while_committed_reservation_is_current(monkeypatch) -> None:
    job = _job()
    source = f"generative-jobs/{job.id}/output.mp4"
    poster = backfill._backfill_poster_object_path(source)
    job.assembly_plan = {"output_path": source, "poster_path": poster}
    candidate = _make_candidate(
        job,
        kind="job_output",
        raw_source=source,
        poster_value=None,
        poster_field="poster_path",
    )
    assert candidate is not None

    class Session:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def get(self, *_args, **_kwargs):
            return job

    uploads: list[tuple[bytes, str, str]] = []
    monkeypatch.setattr(backfill, "sync_session", lambda: Session())
    monkeypatch.setattr(
        backfill,
        "upload_bytes_public_read",
        lambda data, path, content_type: uploads.append((data, path, content_type)),
    )
    monkeypatch.setattr(backfill, "object_exists", lambda path: path == poster)

    assert _publish_reserved_poster(candidate, poster, b"jpeg") == "generated"
    assert uploads == [(b"jpeg", poster, "image/jpeg")]

    replacement = backfill._backfill_poster_object_path(source)
    job.assembly_plan["poster_path"] = replacement
    assert _publish_reserved_poster(candidate, poster, b"stale") == "stale_race"
    assert uploads == [(b"jpeg", poster, "image/jpeg")]


def test_publish_rejects_reservation_after_job_leaves_ready_state(monkeypatch) -> None:
    job = _job()
    source = f"generative-jobs/{job.id}/output.mp4"
    poster = backfill._backfill_poster_object_path(source)
    job.status = "processing"
    job.assembly_plan = {"output_path": source, "poster_path": poster}
    candidate = _make_candidate(
        job,
        kind="job_output",
        raw_source=source,
        poster_value=None,
        poster_field="poster_path",
    )
    assert candidate is not None

    class Session:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def get(self, *_args, **_kwargs):
            return job

    uploads: list[str] = []
    monkeypatch.setattr(backfill, "sync_session", lambda: Session())
    monkeypatch.setattr(
        backfill,
        "upload_bytes_public_read",
        lambda _data, path, **_kwargs: uploads.append(path),
    )

    assert _publish_reserved_poster(candidate, poster, b"jpeg") == "stale_race"
    assert uploads == []


def test_publish_failure_keeps_reserved_key_referenced_for_strict_retry(monkeypatch) -> None:
    job = _job()
    source = f"generative-jobs/{job.id}/output.mp4"
    poster = backfill._backfill_poster_object_path(source)
    job.assembly_plan = {"output_path": source, "poster_path": poster}
    candidate = _make_candidate(
        job,
        kind="job_output",
        raw_source=source,
        poster_value=None,
        poster_field="poster_path",
    )
    assert candidate is not None

    class Session:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def get(self, *_args, **_kwargs):
            return job

    monkeypatch.setattr(backfill, "sync_session", lambda: Session())
    monkeypatch.setattr(
        backfill,
        "upload_bytes_public_read",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("signing failed")),
    )

    assert _publish_reserved_poster(candidate, poster, b"jpeg") == "failed"
    assert job.assembly_plan["poster_path"] == poster


def test_cleanup_receipt_never_deletes_poster_still_shared_by_another_variant(
    monkeypatch,
) -> None:
    job = _job()
    source = f"generative-jobs/{job.id}/output.mp4"
    old_poster = backfill._backfill_poster_object_path(source)
    replacement = backfill._backfill_poster_object_path(source)
    job.assembly_plan = {
        "variants": [
            {"variant_id": "new", "poster_path": replacement},
            {"variant_id": "shared", "poster_path": old_poster},
        ],
        backfill._CLEANUP_RECEIPTS_FIELD: [
            {"old_path": old_poster, "replacement_path": replacement}
        ],
    }

    class EmptyResult:
        def scalars(self):
            return self

        def all(self):
            return []

    class Session:
        commits = 0

        def execute(self, *_args, **_kwargs):
            return EmptyResult()

        def commit(self):
            self.commits += 1

    deleted: list[str] = []
    monkeypatch.setattr(poster_cleanup, "flag_modified", lambda *_args: None)
    monkeypatch.setattr(
        "app.storage.delete_object_once",
        lambda path, *, timeout_s: deleted.append(path) or True,
    )
    session = Session()

    assert backfill._reconcile_cleanup_receipts_locked(session, job) is True
    assert deleted == []
    assert backfill._CLEANUP_RECEIPTS_FIELD not in job.assembly_plan
    assert session.commits == 1


def test_cleanup_receipt_never_deletes_a_deterministic_renderer_key(monkeypatch) -> None:
    job = _job()
    source = f"generative-jobs/{job.id}/output.mp4"
    deterministic_poster = f"{source}.poster.jpg"
    replacement = backfill._backfill_poster_object_path(source)
    job.assembly_plan = {
        "poster_path": replacement,
        backfill._CLEANUP_RECEIPTS_FIELD: [
            {
                "old_path": deterministic_poster,
                "replacement_path": replacement,
            }
        ],
    }

    class EmptyResult:
        def scalars(self):
            return self

        def all(self):
            return []

    class Session:
        def execute(self, *_args, **_kwargs):
            return EmptyResult()

        def commit(self):
            pass

    deleted: list[str] = []
    monkeypatch.setattr(poster_cleanup, "flag_modified", lambda *_args: None)
    monkeypatch.setattr(
        "app.storage.delete_object_once",
        lambda path, *, timeout_s: deleted.append(path) or True,
    )

    assert backfill._reconcile_cleanup_receipts_locked(Session(), job) is True
    assert deleted == []
    assert backfill._CLEANUP_RECEIPTS_FIELD not in job.assembly_plan


def test_cleanup_receipt_deletes_displaced_backfill_uuid_from_an_old_source(
    monkeypatch,
) -> None:
    job = _job()
    old_source = f"generative-jobs/{job.id}/old-output.mp4"
    new_source = f"generative-jobs/{job.id}/new-output.mp4"
    old_poster = backfill._backfill_poster_object_path(old_source)
    replacement = backfill._backfill_poster_object_path(new_source)
    job.assembly_plan = {
        "output_path": new_source,
        "poster_path": replacement,
        backfill._CLEANUP_RECEIPTS_FIELD: [
            {"old_path": old_poster, "replacement_path": replacement}
        ],
    }

    class EmptyResult:
        def scalars(self):
            return self

        def all(self):
            return []

    class Session:
        def execute(self, *_args, **_kwargs):
            return EmptyResult()

        def commit(self):
            pass

    deleted: list[str] = []
    monkeypatch.setattr(poster_cleanup, "flag_modified", lambda *_args: None)
    monkeypatch.setattr(
        "app.storage.object_exists_once",
        lambda path, *, timeout_s: path in {old_poster, replacement},
    )
    monkeypatch.setattr(
        "app.storage.delete_object_once",
        lambda path, *, timeout_s: deleted.append(path) or True,
    )

    assert backfill._reconcile_cleanup_receipts_locked(Session(), job) is True
    assert deleted == [old_poster]
    assert backfill._CLEANUP_RECEIPTS_FIELD not in job.assembly_plan


def test_cleanup_receipt_completes_when_old_uuid_object_is_already_absent(
    monkeypatch,
) -> None:
    job = _job()
    old_source = f"generative-jobs/{job.id}/old-output.mp4"
    new_source = f"generative-jobs/{job.id}/new-output.mp4"
    old_poster = backfill._backfill_poster_object_path(old_source)
    replacement = backfill._backfill_poster_object_path(new_source)
    job.assembly_plan = {
        "output_path": new_source,
        "poster_path": replacement,
        backfill._CLEANUP_RECEIPTS_FIELD: [
            {"old_path": old_poster, "replacement_path": replacement}
        ],
    }

    class EmptyResult:
        def scalars(self):
            return self

        def all(self):
            return []

    class Session:
        def execute(self, *_args, **_kwargs):
            return EmptyResult()

        def commit(self):
            pass

    deleted: list[str] = []
    monkeypatch.setattr(poster_cleanup, "flag_modified", lambda *_args: None)
    monkeypatch.setattr("app.storage.object_exists_once", lambda _path, *, timeout_s: False)
    monkeypatch.setattr(
        "app.storage.delete_object_once",
        lambda path, *, timeout_s: deleted.append(path) or True,
    )

    assert backfill._reconcile_cleanup_receipts_locked(Session(), job) is True
    assert deleted == []
    assert backfill._CLEANUP_RECEIPTS_FIELD not in job.assembly_plan


class _SessionContext:
    rolled_back = False

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def rollback(self):
        self.rolled_back = True


def test_cleanup_receipt_presence_is_read_from_current_locked_row(monkeypatch) -> None:
    job = _job()
    job.assembly_plan = {backfill._CLEANUP_RECEIPTS_FIELD: []}
    reads: list[tuple[object, uuid.UUID, bool]] = []

    class Session(_SessionContext):
        def get(self, model, job_id, *, with_for_update=False):
            reads.append((model, job_id, with_for_update))
            return job

    monkeypatch.setattr(backfill, "sync_session", lambda: Session())

    assert backfill._job_has_cleanup_receipts_current(job.id) is True
    assert reads == [(backfill.Job, job.id, True)]


@pytest.mark.parametrize("non_object_plan", [[], "corrupt", 7, False])
def test_cleanup_receipt_current_check_is_inconclusive_for_non_object_plan(
    monkeypatch,
    non_object_plan,
) -> None:
    job = _job()
    job.assembly_plan = non_object_plan

    class Session(_SessionContext):
        def get(self, *_args, **_kwargs):
            return job

    monkeypatch.setattr(backfill, "sync_session", lambda: Session())

    assert backfill._job_has_cleanup_receipts_current(job.id) is None


def _main_candidate(job: SimpleNamespace, *, source: str) -> PosterCandidate:
    return PosterCandidate(
        job_id=job.id,
        kind="job_output",
        source_key=source,
        poster_field="poster_path",
    )


def test_main_dry_run_reports_discovered_candidates_and_resume_cursor(monkeypatch, capsys) -> None:
    job = _job()
    source = f"generative-jobs/{job.id}/output.mp4"
    candidate = _main_candidate(job, source=source)
    monkeypatch.setattr(backfill, "sync_session", lambda: _SessionContext())
    monkeypatch.setattr(backfill, "_job_batches", lambda *_args, **_kwargs: iter([[job]]))
    monkeypatch.setattr(backfill, "_load_ready_clips", lambda *_args: {})
    monkeypatch.setattr(
        backfill, "_candidates_for_job", lambda *_args, **_kwargs: iter([candidate])
    )

    assert backfill.main(["--dry-run", "--batch-size", "2"]) == 0

    output = capsys.readouterr().out
    assert "[dry-run]" in output
    assert source in output
    assert f"resume_cursor={job.created_at.isoformat()}|{job.id}" in output


def test_main_strict_rejects_ready_job_with_blank_variant(monkeypatch, capsys) -> None:
    job = _job()
    job.assembly_plan = {
        "variants": [
            {
                "variant_id": "blank",
                "render_status": "ready",
                "video_path": None,
            }
        ]
    }
    monkeypatch.setattr(backfill, "sync_session", lambda: _SessionContext())
    monkeypatch.setattr(backfill, "_job_batches", lambda *_args, **_kwargs: iter([[job]]))
    monkeypatch.setattr(backfill, "_load_ready_clips", lambda *_args: {})
    monkeypatch.setattr(backfill, "_candidate_is_still_current", lambda _candidate: True)
    monkeypatch.setattr(backfill, "_job_has_cleanup_receipts_current", lambda _job_id: False)

    assert backfill.main(["--dry-run", "--strict"]) == 1
    output = capsys.readouterr().out
    assert "failed=1" in output
    assert "would_generate=0" in output


def test_main_strict_rejects_healthy_poster_when_source_is_missing(
    monkeypatch,
    capsys,
) -> None:
    job = _job()
    source = f"generative-jobs/{job.id}/output.mp4"
    poster = backfill._backfill_poster_object_path(source)
    job.assembly_plan = {"output_path": source, "poster_path": poster}
    candidate = _make_candidate(
        job,
        kind="job_output",
        raw_source=source,
        poster_value=poster,
        poster_field="poster_path",
        verify_existing=True,
    )
    assert candidate is not None
    _mock_strict_poster(monkeypatch, poster=poster, data=_jpeg_bytes())
    assert _candidate_has_usable_poster(candidate, verify_storage=True) is True
    monkeypatch.setattr(
        backfill,
        "object_metadata_once",
        lambda _path, *, timeout_s: (_ for _ in ()).throw(FileNotFoundError(source)),
    )
    monkeypatch.setattr(backfill, "sync_session", lambda: _SessionContext())
    monkeypatch.setattr(backfill, "_job_batches", lambda *_args, **_kwargs: iter([[job]]))
    monkeypatch.setattr(backfill, "_load_ready_clips", lambda *_args: {})
    monkeypatch.setattr(
        backfill,
        "_candidates_for_job",
        lambda *_args, **_kwargs: iter([candidate]),
    )
    monkeypatch.setattr(backfill, "_candidate_is_still_current", lambda _candidate: True)
    monkeypatch.setattr(backfill, "_job_has_cleanup_receipts_current", lambda _job_id: False)

    # A lifecycle-deleted source is a permanent CLASSIFICATION, not a repair
    # failure: nothing a rerun does can bring the object back, and a non-zero
    # exit would retain the Machine and wedge the deploy guard. The audit line
    # still reports it; the exit code no longer fails on it.
    assert backfill.main(["--dry-run", "--strict"]) == 0
    output = capsys.readouterr().out
    assert "expired_source=1" in output
    assert "already_present=0" in output


def test_strict_zero_candidate_job_still_reconciles_pending_receipt(monkeypatch) -> None:
    job = _job()
    source = f"generative-jobs/{job.id}/old.mp4"
    job.assembly_plan = {
        backfill._CLEANUP_RECEIPTS_FIELD: [
            {
                "old_path": backfill._backfill_poster_object_path(source),
                "replacement_path": f"generative-jobs/{job.id}/current.mp4",
            }
        ]
    }
    reconciled: list[uuid.UUID] = []
    monkeypatch.setattr(backfill, "sync_session", lambda: _SessionContext())
    monkeypatch.setattr(backfill, "_job_batches", lambda *_args, **_kwargs: iter([[job]]))
    monkeypatch.setattr(backfill, "_load_ready_clips", lambda *_args: {})
    monkeypatch.setattr(backfill, "_candidates_for_job", lambda *_args, **_kwargs: iter([]))
    monkeypatch.setattr(
        backfill,
        "_reconcile_cleanup_receipts",
        lambda job_id: reconciled.append(job_id) or False,
    )

    assert backfill.main(["--strict"]) == 1
    assert reconciled == [job.id]


def test_strict_dry_run_fails_closed_on_pending_job_receipt(monkeypatch) -> None:
    job = _job()
    source = f"generative-jobs/{job.id}/old.mp4"
    job.assembly_plan = {
        backfill._CLEANUP_RECEIPTS_FIELD: [
            {
                "old_path": backfill._backfill_poster_object_path(source),
                "replacement_path": f"generative-jobs/{job.id}/current.mp4",
            }
        ]
    }
    monkeypatch.setattr(backfill, "sync_session", lambda: _SessionContext())
    monkeypatch.setattr(backfill, "_job_batches", lambda *_args, **_kwargs: iter([[job]]))
    monkeypatch.setattr(backfill, "_load_ready_clips", lambda *_args: {})
    monkeypatch.setattr(backfill, "_candidates_for_job", lambda *_args, **_kwargs: iter([]))
    monkeypatch.setattr(backfill, "_job_has_cleanup_receipts_current", lambda _job_id: True)

    assert backfill.main(["--dry-run", "--strict"]) == 1


def test_strict_dry_run_fails_closed_on_empty_or_malformed_receipt_field(
    monkeypatch,
) -> None:
    job = _job()
    monkeypatch.setattr(backfill, "sync_session", lambda: _SessionContext())
    monkeypatch.setattr(backfill, "_job_batches", lambda *_args, **_kwargs: iter([[job]]))
    monkeypatch.setattr(backfill, "_load_ready_clips", lambda *_args: {})
    monkeypatch.setattr(backfill, "_candidates_for_job", lambda *_args, **_kwargs: iter([]))
    monkeypatch.setattr(backfill, "_job_has_cleanup_receipts_current", lambda _job_id: True)

    for invalid_receipts in ([], {"malformed": True}, "malformed"):
        job.assembly_plan = {backfill._CLEANUP_RECEIPTS_FIELD: invalid_receipts}
        assert backfill.main(["--dry-run", "--strict"]) == 1


def test_strict_dry_run_detects_receipt_added_after_discovery(monkeypatch, capsys) -> None:
    job = _job()
    job.assembly_plan = {}
    monkeypatch.setattr(backfill, "sync_session", lambda: _SessionContext())
    monkeypatch.setattr(backfill, "_job_batches", lambda *_args, **_kwargs: iter([[job]]))
    monkeypatch.setattr(backfill, "_load_ready_clips", lambda *_args: {})
    monkeypatch.setattr(backfill, "_candidates_for_job", lambda *_args, **_kwargs: iter([]))
    monkeypatch.setattr(backfill, "_job_has_cleanup_receipts_current", lambda _job_id: True)

    assert backfill.main(["--dry-run", "--strict"]) == 1
    output = capsys.readouterr().out
    assert "orphan_cleanup_failed=1" in output
    assert "failed=0" in output


def test_strict_dry_run_ignores_receipt_cleared_after_discovery(monkeypatch, capsys) -> None:
    job = _job()
    job.assembly_plan = {backfill._CLEANUP_RECEIPTS_FIELD: [{"stale": "snapshot"}]}
    monkeypatch.setattr(backfill, "sync_session", lambda: _SessionContext())
    monkeypatch.setattr(backfill, "_job_batches", lambda *_args, **_kwargs: iter([[job]]))
    monkeypatch.setattr(backfill, "_load_ready_clips", lambda *_args: {})
    monkeypatch.setattr(backfill, "_candidates_for_job", lambda *_args, **_kwargs: iter([]))
    monkeypatch.setattr(backfill, "_job_has_cleanup_receipts_current", lambda _job_id: False)

    assert backfill.main(["--dry-run", "--strict"]) == 0
    assert "orphan_cleanup_failed=0" in capsys.readouterr().out


def test_strict_dry_run_receipt_recheck_error_fails_closed(monkeypatch, capsys) -> None:
    job = _job()
    monkeypatch.setattr(backfill, "sync_session", lambda: _SessionContext())
    monkeypatch.setattr(backfill, "_job_batches", lambda *_args, **_kwargs: iter([[job]]))
    monkeypatch.setattr(backfill, "_load_ready_clips", lambda *_args: {})
    monkeypatch.setattr(backfill, "_candidates_for_job", lambda *_args, **_kwargs: iter([]))
    monkeypatch.setattr(backfill, "_job_has_cleanup_receipts_current", lambda _job_id: None)

    assert backfill.main(["--dry-run", "--strict"]) == 1
    assert "failed=1" in capsys.readouterr().out


def test_strict_counts_only_final_receipt_state_after_candidate_repair(monkeypatch, capsys) -> None:
    job = _job()
    source = f"generative-jobs/{job.id}/output.mp4"
    candidate = _main_candidate(job, source=source)
    old_poster = backfill._backfill_poster_object_path(source)
    job.assembly_plan = {
        "output_path": source,
        "poster_path": old_poster,
        backfill._CLEANUP_RECEIPTS_FIELD: [
            {
                "old_path": backfill._backfill_poster_object_path(source),
                "replacement_path": old_poster,
            }
        ],
    }
    reconciled: list[uuid.UUID] = []
    monkeypatch.setattr(backfill, "sync_session", lambda: _SessionContext())
    monkeypatch.setattr(backfill, "_job_batches", lambda *_args, **_kwargs: iter([[job]]))
    monkeypatch.setattr(backfill, "_load_ready_clips", lambda *_args: {})
    monkeypatch.setattr(
        backfill, "_candidates_for_job", lambda *_args, **_kwargs: iter([candidate])
    )
    monkeypatch.setattr(backfill, "_extract_candidate_poster", lambda _source: (b"jpg", ""))
    monkeypatch.setattr(backfill, "_persist_poster", lambda *_args: "generated")
    monkeypatch.setattr(
        backfill,
        "_publish_reserved_poster",
        lambda *_args: "orphan_cleanup_failed",
    )
    monkeypatch.setattr(
        backfill,
        "_reconcile_cleanup_receipts",
        lambda job_id: reconciled.append(job_id) or True,
    )

    assert backfill.main(["--strict"]) == 0
    assert reconciled == [job.id]
    output = capsys.readouterr().out
    assert "generated=1" in output
    assert "orphan_cleanup_failed=0" in output


def test_strict_keeps_publish_cleanup_failure_when_final_reconcile_fails(
    monkeypatch, capsys
) -> None:
    job = _job()
    source = f"generative-jobs/{job.id}/output.mp4"
    candidate = _main_candidate(job, source=source)
    monkeypatch.setattr(backfill, "sync_session", lambda: _SessionContext())
    monkeypatch.setattr(backfill, "_job_batches", lambda *_args, **_kwargs: iter([[job]]))
    monkeypatch.setattr(backfill, "_load_ready_clips", lambda *_args: {})
    monkeypatch.setattr(
        backfill, "_candidates_for_job", lambda *_args, **_kwargs: iter([candidate])
    )
    monkeypatch.setattr(backfill, "_extract_candidate_poster", lambda _source: (b"jpg", ""))
    monkeypatch.setattr(backfill, "_persist_poster", lambda *_args: "generated")
    monkeypatch.setattr(
        backfill,
        "_publish_reserved_poster",
        lambda *_args: "orphan_cleanup_failed",
    )
    monkeypatch.setattr(backfill, "_reconcile_cleanup_receipts", lambda _job_id: False)

    assert backfill.main(["--strict"]) == 1
    output = capsys.readouterr().out
    assert "orphan_cleanup_failed=1" in output
    assert "generated=0" in output


def test_main_returns_failure_status_without_aborting_remaining_candidates(
    monkeypatch, capsys
) -> None:
    job = _job()
    source = f"generative-jobs/{job.id}/output.mp4"
    candidate = _main_candidate(job, source=source)
    monkeypatch.setattr(backfill, "sync_session", lambda: _SessionContext())
    monkeypatch.setattr(backfill, "_job_batches", lambda *_args, **_kwargs: iter([[job]]))
    monkeypatch.setattr(backfill, "_load_ready_clips", lambda *_args: {})
    monkeypatch.setattr(
        backfill, "_candidates_for_job", lambda *_args, **_kwargs: iter([candidate])
    )
    monkeypatch.setattr(backfill, "_extract_candidate_poster", lambda _source: (None, "failed"))

    assert backfill.main(["--batch-size", "2"]) == 1
    assert "failed=1" in capsys.readouterr().out


def test_main_storage_head_error_never_replaces_an_existing_poster(monkeypatch, capsys) -> None:
    job = _job()
    source = f"generative-jobs/{job.id}/output.mp4"
    poster = f"{source}.poster.jpg"
    candidate = PosterCandidate(
        job_id=job.id,
        kind="job_output",
        source_key=source,
        poster_field="poster_path",
        already_present=True,
        existing_poster_key=poster,
    )
    monkeypatch.setattr(backfill, "sync_session", lambda: _SessionContext())
    monkeypatch.setattr(backfill, "_job_batches", lambda *_args, **_kwargs: iter([[job]]))
    monkeypatch.setattr(backfill, "_load_ready_clips", lambda *_args: {})
    monkeypatch.setattr(
        backfill, "_candidates_for_job", lambda *_args, **_kwargs: iter([candidate])
    )
    monkeypatch.setattr(
        backfill,
        "object_metadata",
        lambda _path: (_ for _ in ()).throw(RuntimeError("storage unavailable")),
    )
    extraction_calls: list[str] = []
    monkeypatch.setattr(
        backfill,
        "_extract_candidate_poster",
        lambda path: extraction_calls.append(path) or (b"jpeg", "generated"),
    )

    assert backfill.main(["--strict"]) == 1
    assert extraction_calls == []
    assert "failed=1" in capsys.readouterr().out


def test_main_strict_head_success_for_superseded_candidate_is_stale_race(
    monkeypatch, capsys
) -> None:
    job = _job()
    source = f"generative-jobs/{job.id}/output.mp4"
    poster = backfill._backfill_poster_object_path(source)
    candidate = PosterCandidate(
        job_id=job.id,
        kind="job_output",
        source_key=source,
        poster_field="poster_path",
        already_present=True,
        observed_source_value=source,
        observed_poster_value=poster,
        existing_poster_key=poster,
    )
    monkeypatch.setattr(backfill, "sync_session", lambda: _SessionContext())
    monkeypatch.setattr(backfill, "_job_batches", lambda *_args, **_kwargs: iter([[job]]))
    monkeypatch.setattr(backfill, "_load_ready_clips", lambda *_args: {})
    monkeypatch.setattr(
        backfill, "_candidates_for_job", lambda *_args, **_kwargs: iter([candidate])
    )
    _mock_strict_poster(monkeypatch, poster=poster, data=_jpeg_bytes())
    monkeypatch.setattr(backfill, "_candidate_is_still_current", lambda _candidate: False)
    monkeypatch.setattr(backfill, "_reload_candidates_for_job", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(backfill, "_reconcile_cleanup_receipts", lambda _job_id: True)

    assert backfill.main(["--strict"]) == 0
    output = capsys.readouterr().out
    assert "stale_race=1" in output
    assert "already_present=0" in output


def test_main_strict_head_success_db_recheck_error_fails_closed(monkeypatch, capsys) -> None:
    job = _job()
    source = f"generative-jobs/{job.id}/output.mp4"
    poster = backfill._backfill_poster_object_path(source)
    candidate = PosterCandidate(
        job_id=job.id,
        kind="job_output",
        source_key=source,
        poster_field="poster_path",
        already_present=True,
        observed_source_value=source,
        observed_poster_value=poster,
        existing_poster_key=poster,
    )
    monkeypatch.setattr(backfill, "sync_session", lambda: _SessionContext())
    monkeypatch.setattr(backfill, "_job_batches", lambda *_args, **_kwargs: iter([[job]]))
    monkeypatch.setattr(backfill, "_load_ready_clips", lambda *_args: {})
    monkeypatch.setattr(
        backfill, "_candidates_for_job", lambda *_args, **_kwargs: iter([candidate])
    )
    _mock_strict_poster(monkeypatch, poster=poster, data=_jpeg_bytes())
    monkeypatch.setattr(backfill, "_candidate_is_still_current", lambda _candidate: None)
    monkeypatch.setattr(backfill, "_reconcile_cleanup_receipts", lambda _job_id: True)

    assert backfill.main(["--strict"]) == 1
    output = capsys.readouterr().out
    assert "failed=1" in output
    assert "already_present=0" in output


def test_main_strict_head_race_rescans_current_missing_candidate(monkeypatch, capsys) -> None:
    job = _job()
    source_a = f"generative-jobs/{job.id}/output-a.mp4"
    source_b = f"generative-jobs/{job.id}/output-b.mp4"
    poster_a = backfill._backfill_poster_object_path(source_a)
    job.assembly_plan = {"output_path": source_a, "poster_path": poster_a}

    class CurrentJobSession(_SessionContext):
        def get(self, model, _row_id, *, with_for_update=False):
            assert model is backfill.Job
            assert with_for_update is True
            return job

    head_calls: list[str] = []

    jpeg = _jpeg_bytes()

    def supersede_after_head(path: str) -> ObjectMetadata:
        head_calls.append(path)
        job.assembly_plan = {"output_path": source_b}
        return ObjectMetadata(
            path=path,
            generation="7",
            etag="etag",
            size=len(jpeg),
            content_type="image/jpeg",
        )

    monkeypatch.setattr(backfill, "sync_session", lambda: CurrentJobSession())
    monkeypatch.setattr(backfill, "_job_batches", lambda *_args, **_kwargs: iter([[job]]))
    monkeypatch.setattr(backfill, "_load_ready_clips", lambda *_args: {})
    monkeypatch.setattr(backfill, "object_metadata", supersede_after_head)
    monkeypatch.setattr(
        backfill,
        "download_generation_to_file",
        lambda _path, local_path, *, generation: Path(local_path).write_bytes(jpeg),
    )

    assert backfill.main(["--dry-run", "--strict"]) == 1
    output = capsys.readouterr().out
    assert head_calls == [poster_a]
    assert "already_present=0" in output
    assert "would_generate=1" in output
    assert "stale_race=0" in output
    assert "failed=0" in output


def test_main_strict_rescan_is_bounded_and_second_race_fails_once(monkeypatch, capsys) -> None:
    job = _job()
    source_a = f"generative-jobs/{job.id}/output-a.mp4"
    source_b = f"generative-jobs/{job.id}/output-b.mp4"
    poster_a = backfill._backfill_poster_object_path(source_a)
    poster_b = backfill._backfill_poster_object_path(source_b)
    candidate_a = PosterCandidate(
        job_id=job.id,
        kind="job_output",
        source_key=source_a,
        poster_field="poster_path",
        already_present=True,
        observed_source_value=source_a,
        observed_poster_value=poster_a,
        existing_poster_key=poster_a,
    )
    candidate_b = PosterCandidate(
        job_id=job.id,
        kind="job_output",
        source_key=source_b,
        poster_field="poster_path",
        already_present=True,
        observed_source_value=source_b,
        observed_poster_value=poster_b,
        existing_poster_key=poster_b,
    )
    reload_calls: list[uuid.UUID] = []
    monkeypatch.setattr(backfill, "sync_session", lambda: _SessionContext())
    monkeypatch.setattr(backfill, "_job_batches", lambda *_args, **_kwargs: iter([[job]]))
    monkeypatch.setattr(backfill, "_load_ready_clips", lambda *_args: {})
    monkeypatch.setattr(
        backfill, "_candidates_for_job", lambda *_args, **_kwargs: iter([candidate_a])
    )
    _mock_strict_poster(
        monkeypatch,
        poster={poster_a, poster_b},
        data=_jpeg_bytes(),
    )
    monkeypatch.setattr(backfill, "_candidate_is_still_current", lambda _candidate: False)
    monkeypatch.setattr(
        backfill,
        "_reload_candidates_for_job",
        lambda job_id, **_kwargs: reload_calls.append(job_id) or [candidate_b],
    )
    monkeypatch.setattr(backfill, "_job_has_cleanup_receipts_current", lambda _job_id: False)

    assert backfill.main(["--dry-run", "--strict"]) == 1
    output = capsys.readouterr().out
    assert reload_calls == [job.id]
    assert "failed=1" in output
    assert "stale_race=0" in output
    assert "already_present=0" in output


def test_main_strict_live_rescan_repairs_fresh_candidate_exactly_once(
    monkeypatch,
    capsys,
) -> None:
    job = _job()
    source_a = f"generative-jobs/{job.id}/output-a.mp4"
    source_b = f"generative-jobs/{job.id}/output-b.mp4"
    candidate_a = _main_candidate(job, source=source_a)
    candidate_b = _main_candidate(job, source=source_b)

    for stale_stage in ("persist", "publish"):
        for cleanup_ok in (True, False):
            extract_calls: list[str] = []
            persist_calls: list[str] = []
            publish_calls: list[str] = []
            reload_calls: list[uuid.UUID] = []
            reconcile_calls: list[uuid.UUID] = []

            monkeypatch.setattr(backfill, "sync_session", lambda: _SessionContext())
            monkeypatch.setattr(backfill, "_job_batches", lambda *_args, **_kwargs: iter([[job]]))
            monkeypatch.setattr(backfill, "_load_ready_clips", lambda *_args: {})
            monkeypatch.setattr(
                backfill,
                "_candidates_for_job",
                lambda *_args, **_kwargs: iter([candidate_a]),
            )
            monkeypatch.setattr(
                backfill,
                "_reload_candidates_for_job",
                lambda job_id, **_kwargs: reload_calls.append(job_id) or [candidate_b],
            )
            monkeypatch.setattr(
                backfill,
                "_extract_candidate_poster",
                lambda source: extract_calls.append(source) or (b"jpeg", "generated"),
            )

            def persist(candidate, _poster_key):
                persist_calls.append(candidate.source_key)
                if stale_stage == "persist" and candidate.source_key == source_a:
                    return "stale_race"
                return "generated"

            def publish(candidate, _poster_key, poster_bytes):
                assert poster_bytes == b"jpeg"
                publish_calls.append(candidate.source_key)
                if stale_stage == "publish" and candidate.source_key == source_a:
                    return "stale_race"
                return "orphan_cleanup_failed"

            monkeypatch.setattr(backfill, "_persist_poster", persist)
            monkeypatch.setattr(backfill, "_publish_reserved_poster", publish)
            monkeypatch.setattr(
                backfill,
                "_reconcile_cleanup_receipts",
                lambda job_id: reconcile_calls.append(job_id) or cleanup_ok,
            )

            result = backfill.main(["--strict"])
            output = capsys.readouterr().out

            assert result == (0 if cleanup_ok else 1), (stale_stage, cleanup_ok, output)
            assert extract_calls == [source_a, source_b]
            assert persist_calls == [source_a, source_b]
            assert publish_calls == (
                [source_b] if stale_stage == "persist" else [source_a, source_b]
            )
            assert reload_calls == [job.id]
            assert reconcile_calls == [job.id]
            assert "stale_race=0" in output
            if cleanup_ok:
                assert "generated=1" in output
                assert "orphan_cleanup_failed=0" in output
            else:
                assert "generated=0" in output
                assert "orphan_cleanup_failed=1" in output


def test_main_strict_addresses_duplicate_variant_ids_by_snapshot_index(monkeypatch, capsys) -> None:
    job = _job()
    source_a = f"generative-jobs/{job.id}/variant-a.mp4"
    source_b = f"generative-jobs/{job.id}/variant-b.mp4"
    poster_a = backfill._backfill_poster_object_path(source_a)
    job.assembly_plan = {
        "variants": [
            {
                "variant_id": "duplicate",
                "render_status": "ready",
                "render_generation_id": "generation-a",
                "video_path": source_a,
                "poster_path": poster_a,
            },
            {
                "variant_id": "duplicate",
                "render_status": "ready",
                "render_generation_id": "generation-b",
                "video_path": source_b,
            },
        ]
    }

    class CurrentJobSession(_SessionContext):
        def get(self, model, _row_id, *, with_for_update=False):
            assert model is backfill.Job
            assert with_for_update is True
            return job

    monkeypatch.setattr(backfill, "sync_session", lambda: CurrentJobSession())
    monkeypatch.setattr(backfill, "_job_batches", lambda *_args, **_kwargs: iter([[job]]))
    monkeypatch.setattr(backfill, "_load_ready_clips", lambda *_args: {})
    _mock_strict_poster(monkeypatch, poster=poster_a, data=_jpeg_bytes())

    assert backfill.main(["--dry-run", "--strict"]) == 1
    output = capsys.readouterr().out
    assert "already_present=1" in output
    assert "would_generate=1" in output
    assert "stale_race=0" in output
    assert "failed=0" in output


def test_main_head_error_for_deleted_candidate_is_stale_race(monkeypatch, capsys) -> None:
    job = _job()
    source = f"generative-jobs/{job.id}/output.mp4"
    poster = f"{source}.poster.jpg"
    candidate = PosterCandidate(
        job_id=job.id,
        kind="job_output",
        source_key=source,
        poster_field="poster_path",
        already_present=True,
        observed_poster_value=poster,
        existing_poster_key=poster,
    )
    monkeypatch.setattr(backfill, "sync_session", lambda: _SessionContext())
    monkeypatch.setattr(backfill, "_job_batches", lambda *_args, **_kwargs: iter([[job]]))
    monkeypatch.setattr(backfill, "_load_ready_clips", lambda *_args: {})
    monkeypatch.setattr(
        backfill, "_candidates_for_job", lambda *_args, **_kwargs: iter([candidate])
    )
    monkeypatch.setattr(
        backfill,
        "object_metadata",
        lambda _path: (_ for _ in ()).throw(RuntimeError("storage unavailable")),
    )
    monkeypatch.setattr(backfill, "_candidate_is_still_current", lambda _candidate: False)
    monkeypatch.setattr(backfill, "_reload_candidates_for_job", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(backfill, "_reconcile_cleanup_receipts", lambda _job_id: True)

    assert backfill.main(["--strict"]) == 0
    output = capsys.readouterr().out
    assert "stale_race=1" in output
    assert "failed=0" in output


def test_main_missing_head_for_superseded_variant_skips_stale_download(monkeypatch, capsys) -> None:
    job = _job()
    source = f"generative-jobs/{job.id}/output.mp4"
    poster = f"{source}.poster.jpg"
    candidate = PosterCandidate(
        job_id=job.id,
        kind="variant",
        source_key=source,
        poster_field="poster_path",
        variant_id="song_text",
        variant_index=0,
        render_identity="generation-1",
        already_present=True,
        observed_poster_value=poster,
        existing_poster_key=poster,
    )
    monkeypatch.setattr(backfill, "sync_session", lambda: _SessionContext())
    monkeypatch.setattr(backfill, "_job_batches", lambda *_args, **_kwargs: iter([[job]]))
    monkeypatch.setattr(backfill, "_load_ready_clips", lambda *_args: {})
    monkeypatch.setattr(
        backfill, "_candidates_for_job", lambda *_args, **_kwargs: iter([candidate])
    )
    monkeypatch.setattr(
        backfill,
        "object_metadata",
        lambda _path: (_ for _ in ()).throw(FileNotFoundError(poster)),
    )
    monkeypatch.setattr(backfill, "_candidate_is_still_current", lambda _candidate: False)
    monkeypatch.setattr(backfill, "_reload_candidates_for_job", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(backfill, "_reconcile_cleanup_receipts", lambda _job_id: True)
    extraction_calls: list[str] = []
    monkeypatch.setattr(
        backfill,
        "_extract_candidate_poster",
        lambda path: extraction_calls.append(path) or (b"jpeg", "generated"),
    )

    assert backfill.main(["--strict"]) == 0
    assert extraction_calls == []
    assert "stale_race=1" in capsys.readouterr().out


def test_main_download_failure_for_deleted_job_is_stale_race(monkeypatch, capsys) -> None:
    job = _job()
    source = f"generative-jobs/{job.id}/output.mp4"
    candidate = _main_candidate(job, source=source)
    monkeypatch.setattr(backfill, "sync_session", lambda: _SessionContext())
    monkeypatch.setattr(backfill, "_job_batches", lambda *_args, **_kwargs: iter([[job]]))
    monkeypatch.setattr(backfill, "_load_ready_clips", lambda *_args: {})
    monkeypatch.setattr(
        backfill, "_candidates_for_job", lambda *_args, **_kwargs: iter([candidate])
    )
    monkeypatch.setattr(
        backfill, "_extract_candidate_poster", lambda _source: (None, "expired_source")
    )
    monkeypatch.setattr(backfill, "_candidate_is_still_current", lambda _candidate: False)
    monkeypatch.setattr(backfill, "_reload_candidates_for_job", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(backfill, "_reconcile_cleanup_receipts", lambda _job_id: True)

    assert backfill.main(["--strict"]) == 0
    output = capsys.readouterr().out
    assert "stale_race=1" in output
    assert "expired_source=0" in output


def test_main_releases_read_transaction_before_storage_work(monkeypatch) -> None:
    job = _job()
    source = f"generative-jobs/{job.id}/output.mp4"
    candidate = _main_candidate(job, source=source)
    session = _SessionContext()
    monkeypatch.setattr(backfill, "sync_session", lambda: session)
    monkeypatch.setattr(backfill, "_job_batches", lambda *_args, **_kwargs: iter([[job]]))
    monkeypatch.setattr(backfill, "_load_ready_clips", lambda *_args: {})
    monkeypatch.setattr(
        backfill, "_candidates_for_job", lambda *_args, **_kwargs: iter([candidate])
    )

    def assert_transaction_closed(_source: str) -> tuple[None, str]:
        assert session.rolled_back is True
        return None, "failed"

    monkeypatch.setattr(backfill, "_extract_candidate_poster", assert_transaction_closed)

    assert backfill.main(["--batch-size", "2"]) == 1


def test_main_stale_reservation_never_uploads_an_unreferenced_object(monkeypatch, capsys) -> None:
    job = _job()
    source = f"generative-jobs/{job.id}/output.mp4"
    candidate = _main_candidate(job, source=source)
    monkeypatch.setattr(backfill, "sync_session", lambda: _SessionContext())
    monkeypatch.setattr(backfill, "_job_batches", lambda *_args, **_kwargs: iter([[job]]))
    monkeypatch.setattr(backfill, "_load_ready_clips", lambda *_args: {})
    monkeypatch.setattr(
        backfill, "_candidates_for_job", lambda *_args, **_kwargs: iter([candidate])
    )
    monkeypatch.setattr(
        backfill, "_extract_candidate_poster", lambda _source: (b"jpeg", "generated")
    )
    monkeypatch.setattr(backfill, "_persist_poster", lambda *_args: "stale_race")
    publish_calls: list[tuple] = []
    monkeypatch.setattr(
        backfill,
        "_publish_reserved_poster",
        lambda *args: publish_calls.append(args) or "generated",
    )

    assert backfill.main(["--batch-size", "2"]) == 0
    assert publish_calls == []
    assert "stale_race=1" in capsys.readouterr().out


def test_main_publishes_only_after_uuid_reservation_commits(monkeypatch, capsys) -> None:
    job = _job()
    source = f"generative-jobs/{job.id}/output.mp4"
    candidate = _main_candidate(job, source=source)
    calls: list[tuple[str, str]] = []
    monkeypatch.setattr(backfill, "sync_session", lambda: _SessionContext())
    monkeypatch.setattr(backfill, "_job_batches", lambda *_args, **_kwargs: iter([[job]]))
    monkeypatch.setattr(backfill, "_load_ready_clips", lambda *_args: {})
    monkeypatch.setattr(
        backfill, "_candidates_for_job", lambda *_args, **_kwargs: iter([candidate])
    )
    monkeypatch.setattr(
        backfill, "_extract_candidate_poster", lambda _source: (b"jpeg", "generated")
    )

    def reserve(_candidate, poster_key):
        calls.append(("reserve", poster_key))
        return "generated"

    def publish(_candidate, poster_key, poster_bytes):
        assert poster_bytes == b"jpeg"
        calls.append(("publish", poster_key))
        return "generated"

    monkeypatch.setattr(backfill, "_persist_poster", reserve)
    monkeypatch.setattr(
        backfill,
        "_publish_reserved_poster",
        publish,
    )

    assert backfill.main(["--batch-size", "2"]) == 0
    assert [name for name, _key in calls] == ["reserve", "publish"]
    assert calls[0][1] == calls[1][1]
    # The run loop reserves a durable, job-scoped key for the exact source.
    assert calls[0][1].startswith(f"job-posters/{job.id}/")
    assert backfill._is_backfill_poster_path(calls[0][1], source, job.id)
    assert "generated=1" in capsys.readouterr().out


def test_main_publish_failure_keeps_the_db_reservation_recoverable(monkeypatch, capsys) -> None:
    job = _job()
    source = f"generative-jobs/{job.id}/output.mp4"
    candidate = _main_candidate(job, source=source)
    monkeypatch.setattr(backfill, "sync_session", lambda: _SessionContext())
    monkeypatch.setattr(backfill, "_job_batches", lambda *_args, **_kwargs: iter([[job]]))
    monkeypatch.setattr(backfill, "_load_ready_clips", lambda *_args: {})
    monkeypatch.setattr(
        backfill, "_candidates_for_job", lambda *_args, **_kwargs: iter([candidate])
    )
    monkeypatch.setattr(
        backfill, "_extract_candidate_poster", lambda _source: (b"jpeg", "generated")
    )
    monkeypatch.setattr(backfill, "_persist_poster", lambda *_args: "generated")
    monkeypatch.setattr(backfill, "_publish_reserved_poster", lambda *_args: "failed")

    assert backfill.main(["--strict", "--batch-size", "2"]) == 1
    output = capsys.readouterr().out
    assert "failed=1" in output


def test_main_strict_rejects_blocked_candidates(monkeypatch, capsys) -> None:
    job = _job()
    blocked = PosterCandidate(
        job_id=job.id,
        kind="job_output",
        source_key=None,
        poster_field="poster_path",
        blocked_outcome="skipped_not_owned",
    )
    monkeypatch.setattr(backfill, "sync_session", lambda: _SessionContext())
    monkeypatch.setattr(backfill, "_job_batches", lambda *_args, **_kwargs: iter([[job]]))
    monkeypatch.setattr(backfill, "_load_ready_clips", lambda *_args: {})
    monkeypatch.setattr(backfill, "_candidates_for_job", lambda *_args, **_kwargs: iter([blocked]))
    monkeypatch.setattr(backfill, "_candidate_is_still_current", lambda _candidate: True)

    assert backfill.main(["--strict"]) == 1
    assert "skipped_not_owned=1" in capsys.readouterr().out


def test_strict_treats_unresolvable_legacy_url_as_classification_not_failure(
    monkeypatch, capsys
) -> None:
    """Legacy signed-URL-only rows are a permanent state, not a fixable fault.

    The 2026-08-31 census counts ~62 of them, so an exit gate requiring zero
    can never pass again — and a failed run retains the Machine, wedging the
    stable guard name every deploy CASes. The count is still reported for the
    audit; it just no longer fails the run.
    """
    job = _job()
    blocked = PosterCandidate(
        job_id=job.id,
        kind="job_output",
        source_key=None,
        poster_field="poster_path",
        observed_source_value="https://legacy.invalid/output.mp4",
        blocked_outcome="unresolvable_legacy_url",
    )
    monkeypatch.setattr(backfill, "sync_session", lambda: _SessionContext())
    monkeypatch.setattr(backfill, "_job_batches", lambda *_args, **_kwargs: iter([[job]]))
    monkeypatch.setattr(backfill, "_load_ready_clips", lambda *_args: {})
    monkeypatch.setattr(backfill, "_candidates_for_job", lambda *_args, **_kwargs: iter([blocked]))
    monkeypatch.setattr(backfill, "_candidate_is_still_current", lambda _candidate: True)

    assert backfill.main(["--strict"]) == 0
    assert "unresolvable_legacy_url=1" in capsys.readouterr().out


def test_strict_still_requires_zero_would_generate_in_dry_run(monkeypatch, capsys) -> None:
    """Reclassifying the permanent states must not weaken the real audit.

    ``would_generate > 0`` means repairable work was left undone — that is the
    acceptance criterion the runbook's second pass exists to prove, and it
    still fails the run.
    """
    job = _job()
    source = f"generative-jobs/{job.id}/output.mp4"
    candidate = _main_candidate(job, source=source)
    monkeypatch.setattr(backfill, "sync_session", lambda: _SessionContext())
    monkeypatch.setattr(backfill, "_job_batches", lambda *_args, **_kwargs: iter([[job]]))
    monkeypatch.setattr(backfill, "_load_ready_clips", lambda *_args: {})
    monkeypatch.setattr(
        backfill, "_candidates_for_job", lambda *_args, **_kwargs: iter([candidate])
    )
    monkeypatch.setattr(backfill, "_candidate_is_still_current", lambda _candidate: True)
    monkeypatch.setattr(backfill, "_job_has_cleanup_receipts_current", lambda _job_id: False)

    assert backfill.main(["--dry-run", "--strict"]) == 1
    assert "would_generate=1" in capsys.readouterr().out


def test_main_blocked_candidate_superseded_while_scanning_is_stale_race(
    monkeypatch, capsys
) -> None:
    job = _job()
    blocked = PosterCandidate(
        job_id=job.id,
        kind="job_output",
        source_key=None,
        poster_field="poster_path",
        observed_source_value="https://legacy.invalid/output.mp4",
        blocked_outcome="unresolvable_legacy_url",
    )
    monkeypatch.setattr(backfill, "sync_session", lambda: _SessionContext())
    monkeypatch.setattr(backfill, "_job_batches", lambda *_args, **_kwargs: iter([[job]]))
    monkeypatch.setattr(backfill, "_load_ready_clips", lambda *_args: {})
    monkeypatch.setattr(backfill, "_candidates_for_job", lambda *_args, **_kwargs: iter([blocked]))
    monkeypatch.setattr(backfill, "_candidate_is_still_current", lambda _candidate: False)
    monkeypatch.setattr(backfill, "_reload_candidates_for_job", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(backfill, "_reconcile_cleanup_receipts", lambda _job_id: True)

    assert backfill.main(["--strict"]) == 0
    output = capsys.readouterr().out
    assert "stale_race=1" in output
    assert "unresolvable_legacy_url=0" in output


def test_main_strict_dry_run_requires_no_remaining_posters(monkeypatch, capsys) -> None:
    job = _job()
    source = f"generative-jobs/{job.id}/output.mp4"
    candidate = _main_candidate(job, source=source)
    monkeypatch.setattr(backfill, "sync_session", lambda: _SessionContext())
    monkeypatch.setattr(backfill, "_job_batches", lambda *_args, **_kwargs: iter([[job]]))
    monkeypatch.setattr(backfill, "_load_ready_clips", lambda *_args: {})
    monkeypatch.setattr(
        backfill, "_candidates_for_job", lambda *_args, **_kwargs: iter([candidate])
    )
    monkeypatch.setattr(backfill, "_candidate_is_still_current", lambda _candidate: True)
    monkeypatch.setattr(backfill, "_job_has_cleanup_receipts_current", lambda _job_id: False)

    assert backfill.main(["--dry-run", "--strict"]) == 1
    assert "would_generate=1" in capsys.readouterr().out


def test_main_strict_dry_run_ignores_superseded_missing_candidate(monkeypatch, capsys) -> None:
    job = _job()
    source = f"generative-jobs/{job.id}/output.mp4"
    candidate = _main_candidate(job, source=source)
    monkeypatch.setattr(backfill, "sync_session", lambda: _SessionContext())
    monkeypatch.setattr(backfill, "_job_batches", lambda *_args, **_kwargs: iter([[job]]))
    monkeypatch.setattr(backfill, "_load_ready_clips", lambda *_args: {})
    monkeypatch.setattr(
        backfill, "_candidates_for_job", lambda *_args, **_kwargs: iter([candidate])
    )
    monkeypatch.setattr(backfill, "_candidate_is_still_current", lambda _candidate: False)
    monkeypatch.setattr(backfill, "_reload_candidates_for_job", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(backfill, "_job_has_cleanup_receipts_current", lambda _job_id: False)

    assert backfill.main(["--dry-run", "--strict"]) == 0
    output = capsys.readouterr().out
    assert "stale_race=1" in output
    assert "would_generate=0" in output


def test_backfill_poster_key_uses_durable_prefix_and_keeps_uuid_marker() -> None:
    """Durable prefix + immutable UUID suffix + cleanup-service recognition."""
    job = _job()
    source = f"music-jobs/{job.id}/output.mp4"

    poster = backfill._backfill_poster_object_path(source, job.id)

    assert poster.startswith(f"job-posters/{job.id}/")
    assert backfill._BACKFILL_POSTER_MARKER in poster
    assert poster.endswith(".jpg")
    # Two derivations of the same source must never collide with each other,
    # so a concurrent renderer can never overwrite a reserved object.
    assert poster != backfill._backfill_poster_object_path(source, job.id)
    # The cleanup service still recognizes it as a deletable backfill object.
    assert poster_cleanup.is_uuid_backfill_poster(poster) is True
    assert backfill.owned_job_output_path(poster, job) == poster


def test_backfill_poster_key_falls_back_to_sibling_without_job_id() -> None:
    job = _job()
    source = f"music-jobs/{job.id}/output.mp4"

    poster = backfill._backfill_poster_object_path(source)

    assert poster.startswith(f"{source}{backfill._BACKFILL_POSTER_MARKER}")
    assert poster_cleanup.backfill_poster_source(poster) == source


def test_poster_matches_source_accepts_legacy_and_durable_shapes() -> None:
    job = _job()
    other = _job()
    source = f"music-jobs/{job.id}/output.mp4"

    legacy_sibling = f"{source}.poster.jpg"
    legacy_backfill = backfill._backfill_poster_object_path(source)
    durable_render = backfill.poster_object_path(source, job_id=str(job.id))
    durable_backfill = backfill._backfill_poster_object_path(source, job.id)

    assert backfill._poster_matches_source(legacy_sibling, source, job.id) is True
    assert backfill._poster_matches_source(legacy_backfill, source, job.id) is True
    assert backfill._poster_matches_source(durable_render, source, job.id) is True
    assert backfill._poster_matches_source(durable_backfill, source, job.id) is True
    # A durable key only matches under the job that owns it.
    assert backfill._poster_matches_source(durable_backfill, source, other.id) is False
    assert backfill._poster_matches_source(durable_backfill, source) is False
    # And never for a different source object under the same job.
    assert (
        backfill._poster_matches_source(
            durable_backfill,
            f"music-jobs/{job.id}/other.mp4",
            job.id,
        )
        is False
    )


def test_strict_candidate_accepts_durable_poster_that_exists(monkeypatch) -> None:
    job = _job()
    source = f"music-jobs/{job.id}/output.mp4"
    poster = backfill._backfill_poster_object_path(source, job.id)
    _mock_strict_poster(monkeypatch, poster=poster, data=_jpeg_bytes())

    candidate = _make_candidate(
        job,
        kind="job_output",
        raw_source=source,
        poster_value=poster,
        poster_field="poster_path",
        verify_existing=True,
    )

    assert candidate is not None
    assert candidate.already_present is True
    assert candidate.existing_poster_key == poster
    assert _candidate_has_usable_poster(candidate, verify_storage=True) is True


def test_persist_and_publish_accept_durable_poster_reservation(monkeypatch) -> None:
    job = _job()
    source = f"music-jobs/{job.id}/output.mp4"
    job.assembly_plan = {"output_path": source}
    candidate = _make_candidate(
        job,
        kind="job_output",
        raw_source=source,
        poster_value=None,
        poster_field="poster_path",
    )
    assert candidate is not None
    poster = backfill._backfill_poster_object_path(source, job.id)

    class Result:
        def scalar_one_or_none(self):
            return job

    class Session:
        commits = 0

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def get(self, *_args, **_kwargs):
            return job

        def commit(self):
            Session.commits += 1

        def expire_all(self):
            pass

        def execute(self, *_args, **_kwargs):
            return Result()

    monkeypatch.setattr(backfill, "sync_session", Session)
    monkeypatch.setattr(backfill, "flag_modified", lambda *_args: None)

    assert _persist_poster(candidate, poster) == "generated"
    assert job.assembly_plan["poster_path"] == poster

    uploads: list[tuple[bytes, str, str]] = []
    monkeypatch.setattr(
        backfill,
        "upload_bytes_public_read",
        lambda data, path, content_type: uploads.append((data, path, content_type)),
    )
    monkeypatch.setattr(backfill, "object_exists", lambda path: path == poster)

    assert _publish_reserved_poster(candidate, poster, b"jpeg") == "generated"
    assert uploads == [(b"jpeg", poster, "image/jpeg")]
