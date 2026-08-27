from __future__ import annotations

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace

import scripts.backfill_video_posters as backfill
from app.services.template_poster import PosterExtractionError
from scripts.backfill_video_posters import (
    PosterCandidate,
    _candidates_for_job,
    _generate_poster,
    _make_candidate,
    _parse_cursor,
    _persist_poster,
    _source_candidate,
)


def _job() -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        status="variants_ready",
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


def test_generate_poster_uses_deterministic_sibling(monkeypatch) -> None:
    calls: dict[str, object] = {}
    monkeypatch.setattr(
        "scripts.backfill_video_posters.download_to_file",
        lambda source, local: calls.update(source=source, local=local),
    )
    monkeypatch.setattr(
        "scripts.backfill_video_posters.extract_poster_bytes",
        lambda local: b"jpeg",
    )
    monkeypatch.setattr(
        "scripts.backfill_video_posters.upload_bytes_public_read",
        lambda data, path, content_type: calls.update(
            data=data, path=path, content_type=content_type
        ),
    )

    poster, outcome = _generate_poster(f"jobs/{uuid.uuid4()}/output.mp4")

    assert outcome == "generated"
    assert poster and poster.endswith("output.mp4.poster.jpg")
    assert calls["data"] == b"jpeg"
    assert calls["content_type"] == "image/jpeg"


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


def test_generate_poster_classifies_missing_object_and_extraction_failures(monkeypatch) -> None:
    monkeypatch.setattr(
        "scripts.backfill_video_posters.download_to_file",
        lambda *_args: (_ for _ in ()).throw(RuntimeError("status code: 404")),
    )
    poster, outcome = _generate_poster("jobs/job/output.mp4")
    assert poster is None
    assert outcome == "expired_source"

    monkeypatch.setattr("scripts.backfill_video_posters.download_to_file", lambda *_args: None)
    monkeypatch.setattr(
        "scripts.backfill_video_posters.extract_poster_bytes",
        lambda *_args: (_ for _ in ()).throw(PosterExtractionError("bad video")),
    )
    poster, outcome = _generate_poster("jobs/job/output.mp4")
    assert poster is None
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


class _SessionContext:
    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def rollback(self):
        pass


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
    monkeypatch.setattr(backfill, "_generate_poster", lambda _source: (None, "failed"))

    assert backfill.main(["--batch-size", "2"]) == 1
    assert "failed=1" in capsys.readouterr().out


def test_main_retains_deterministic_poster_on_stale_race(monkeypatch, capsys) -> None:
    job = _job()
    source = f"generative-jobs/{job.id}/output.mp4"
    candidate = _main_candidate(job, source=source)
    deleted: list[str] = []
    monkeypatch.setattr(backfill, "sync_session", lambda: _SessionContext())
    monkeypatch.setattr(backfill, "_job_batches", lambda *_args, **_kwargs: iter([[job]]))
    monkeypatch.setattr(backfill, "_load_ready_clips", lambda *_args: {})
    monkeypatch.setattr(
        backfill, "_candidates_for_job", lambda *_args, **_kwargs: iter([candidate])
    )
    monkeypatch.setattr(
        backfill,
        "_generate_poster",
        lambda _source: (f"{source}.poster.jpg", "generated"),
    )
    monkeypatch.setattr(backfill, "_persist_poster", lambda *_args: "stale_race")
    monkeypatch.setattr(backfill, "delete_object_best_effort", deleted.append)

    assert backfill.main(["--batch-size", "2"]) == 0
    assert deleted == []
    assert "stale_race" in capsys.readouterr().out
