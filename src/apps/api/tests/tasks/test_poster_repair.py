"""Unit tests for the `tasks.repair_job_poster` body (library poster actuator).

All offline — the sync session, GCS existence/delete probes and the poster
extractor are monkeypatched, so this exercises the task's orchestration
(resolution order, lock/verify discipline, marker semantics) without Postgres,
GCS or FFmpeg. Style mirrors `test_grade_final_video.py`.

The regression-critical cases are the kill-switch pin (flag off must not touch
the database at all) and the marker rebind (a stale terminal verdict must never
mask a freshly re-rendered video's poster).
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest

import app.tasks.poster_repair as pr
from app.config import settings
from app.models import Job, JobClip


class _FakeJob:
    def __init__(
        self,
        *,
        status: str = "variants_ready",
        assembly_plan: dict | None = None,
        job_id: uuid.UUID | None = None,
        user_id: uuid.UUID | None = None,
    ) -> None:
        self.id = job_id or uuid.uuid4()
        self.user_id = user_id or uuid.uuid4()
        self.status = status
        self.assembly_plan = assembly_plan


class _FakeClip:
    def __init__(
        self,
        *,
        job_id: uuid.UUID,
        video_path: str | None,
        thumbnail_path: str | None = None,
        rank: int = 0,
        render_status: str = "ready",
    ) -> None:
        self.id = uuid.uuid4()
        self.job_id = job_id
        self.rank = rank
        self.render_status = render_status
        self.video_path = video_path
        self.thumbnail_path = thumbnail_path


class _Result:
    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows

    def scalars(self) -> _Result:
        return self

    def all(self) -> list[Any]:
        return list(self._rows)


class _FakeSession:
    """One shared session stand-in across all three locked phases."""

    def __init__(self, job: _FakeJob | None, clips: list[_FakeClip] | None = None) -> None:
        self.job = job
        self.clips = clips or []
        self.commits = 0
        self.locks: list[tuple[str, Any]] = []

    def __enter__(self) -> _FakeSession:
        return self

    def __exit__(self, *exc: object) -> bool:
        return False

    def get(self, model: Any, pk: Any, **kwargs: Any) -> Any:
        assert kwargs.get("with_for_update") is True, "every row read must hold a row lock"
        if model is Job:
            self.locks.append(("job", pk))
            return self.job
        if model is JobClip:
            self.locks.append(("clip", pk))
            return next((clip for clip in self.clips if clip.id == pk), None)
        raise AssertionError(f"unexpected model {model!r}")

    def execute(self, _statement: Any) -> _Result:
        return _Result(self.clips)

    def commit(self) -> None:
        self.commits += 1


@pytest.fixture(autouse=True)
def _no_orm_flags(monkeypatch: pytest.MonkeyPatch) -> None:
    """`flag_modified` needs a real ORM instance; the fakes are plain objects."""
    monkeypatch.setattr("sqlalchemy.orm.attributes.flag_modified", lambda *a, **k: None)


@pytest.fixture(autouse=True)
def _enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "poster_ondemand_repair_enabled", True)


def _wire(
    monkeypatch: pytest.MonkeyPatch,
    session: _FakeSession,
    *,
    source_exists: bool = True,
    poster_key: str | None = "job-posters/x/abc.poster.jpg",
    on_generate=None,
) -> dict[str, list]:
    """Patch the session factory, storage probes and the poster extractor."""
    calls: dict[str, list] = {"generate": [], "deleted": [], "exists": []}

    def _exists(path: str) -> bool:
        calls["exists"].append(path)
        return source_exists

    def _generate(video_object_path: str, **kwargs: Any) -> str | None:
        calls["generate"].append((video_object_path, kwargs))
        if on_generate is not None:
            on_generate()
        return poster_key

    monkeypatch.setattr("app.database.sync_session", lambda: session)
    monkeypatch.setattr("app.storage.object_exists", _exists)
    monkeypatch.setattr(
        "app.storage.delete_object_best_effort",
        lambda path: calls["deleted"].append(path) or True,
    )
    monkeypatch.setattr(
        "app.services.template_poster.generate_and_upload_from_gcs",
        _generate,
    )
    return calls


def _variant_plan(job_id: uuid.UUID, **overrides: Any) -> dict:
    variant = {
        "variant_id": "song_text",
        "render_status": "ready",
        "rank": 0,
        "render_generation_id": "gen-1",
        "video_path": f"generative-jobs/{job_id}/song-text.mp4",
    }
    variant.update(overrides)
    return {"variants": [variant]}


# ── Persistence branches ──────────────────────────────────────────────────────


def test_persists_relative_key_on_the_matching_variant(monkeypatch: pytest.MonkeyPatch) -> None:
    job = _FakeJob()
    job.assembly_plan = _variant_plan(job.id)
    session = _FakeSession(job)
    calls = _wire(monkeypatch, session)

    assert pr._run_repair(str(job.id)) == "generated"

    variant = job.assembly_plan["variants"][0]
    assert variant["poster_path"] == "job-posters/x/abc.poster.jpg"
    assert "://" not in variant["poster_path"], "a signed URL must never be persisted"
    # The cross-lane contract: job_id is always threaded through so the poster
    # lands on the lifecycle-durable prefix.
    source, kwargs = calls["generate"][0]
    assert source == f"generative-jobs/{job.id}/song-text.mp4"
    assert kwargs == {"job_id": str(job.id), "source_kind": "poster_repair"}
    assert session.commits == 1
    assert not calls["deleted"]


def test_every_persisting_path_flags_the_jsonb_column_dirty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`flag_modified` is the difference between a commit and a silent no-op.

    The autouse `_no_orm_flags` fixture stubs it away for every other test (the
    fakes are not ORM instances), so without this one, deleting the
    `flag_modified` call from `_stage_plan` would leave the suite green while
    every persisted poster key and marker silently vanished in production.
    """
    for label, build, expected in (
        ("generated", lambda job: _variant_plan(job.id), "generated"),
        ("expired_source", lambda job: _variant_plan(job.id), pr.TERMINAL_EXPIRED_SOURCE),
    ):
        job = _FakeJob()
        job.assembly_plan = build(job)
        session = _FakeSession(job)
        _wire(monkeypatch, session, source_exists=expected != pr.TERMINAL_EXPIRED_SOURCE)

        flagged: list[tuple[object, str]] = []
        monkeypatch.setattr(
            "sqlalchemy.orm.attributes.flag_modified",
            lambda obj, key: flagged.append((obj, key)),
        )

        assert pr._run_repair(str(job.id)) == expected, label
        assert flagged, f"{label}: assembly_plan was never flagged dirty"
        assert flagged[-1][1] == "assembly_plan", label


def test_persists_relative_key_on_top_level_plan_output(monkeypatch: pytest.MonkeyPatch) -> None:
    job = _FakeJob(status="template_ready")
    job.assembly_plan = {"output_path": f"jobs/{job.id}/output.mp4"}
    session = _FakeSession(job)
    _wire(monkeypatch, session, poster_key="job-posters/y/def.poster.jpg")

    assert pr._run_repair(str(job.id)) == "generated"
    assert job.assembly_plan["poster_path"] == "job-posters/y/def.poster.jpg"


def test_persists_relative_key_on_lowest_ranked_ready_job_clip(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job = _FakeJob(status="clips_ready", assembly_plan=None)
    best = _FakeClip(job_id=job.id, rank=0, video_path=f"{job.user_id}/{job.id}/run/a.mp4")
    worse = _FakeClip(job_id=job.id, rank=5, video_path=f"{job.user_id}/{job.id}/run/b.mp4")
    session = _FakeSession(job, clips=[worse, best])
    _wire(monkeypatch, session, poster_key="job-posters/z/ghi.poster.jpg")

    assert pr._run_repair(str(job.id)) == "generated"
    assert best.thumbnail_path == "job-posters/z/ghi.poster.jpg"
    assert worse.thumbnail_path is None
    # The clip row is locked before it is written.
    assert ("clip", best.id) in session.locks


def test_skips_when_a_poster_is_already_present(monkeypatch: pytest.MonkeyPatch) -> None:
    job = _FakeJob()
    job.assembly_plan = _variant_plan(
        job.id, poster_path="generative-jobs/PLACEHOLDER/song-text.mp4.poster.jpg"
    )
    job.assembly_plan["variants"][0]["poster_path"] = (
        f"generative-jobs/{job.id}/song-text.mp4.poster.jpg"
    )
    session = _FakeSession(job)
    calls = _wire(monkeypatch, session)

    assert pr._run_repair(str(job.id)) == "already_present"
    assert calls["generate"] == []
    assert session.commits == 0


# ── Terminal states ───────────────────────────────────────────────────────────


def test_missing_source_object_settles_expired_source(monkeypatch: pytest.MonkeyPatch) -> None:
    job = _FakeJob(status="music_ready")
    job.assembly_plan = {"output_path": f"music-jobs/{job.id}/output.mp4"}
    session = _FakeSession(job)
    calls = _wire(monkeypatch, session, source_exists=False)

    assert pr._run_repair(str(job.id)) == "expired_source"

    marker = job.assembly_plan[pr.POSTER_REPAIR_MARKER_FIELD]
    assert marker["terminal"] == "expired_source"
    assert marker["video_path"] == f"music-jobs/{job.id}/output.mp4"
    assert calls["generate"] == [], "never download a source that does not exist"
    assert session.commits == 1


def test_third_extraction_failure_settles_attempts_exhausted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job = _FakeJob()
    job.assembly_plan = _variant_plan(job.id)
    source = f"generative-jobs/{job.id}/song-text.mp4"
    job.assembly_plan[pr.POSTER_REPAIR_MARKER_FIELD] = {
        "video_path": source,
        "attempts": 2,
        "terminal": None,
        "enqueued_at": "2026-08-30T00:00:00+00:00",
    }
    session = _FakeSession(job)
    _wire(monkeypatch, session, poster_key=None)

    assert pr._run_repair(str(job.id)) == "attempts_exhausted"

    marker = job.assembly_plan[pr.POSTER_REPAIR_MARKER_FIELD]
    assert marker["attempts"] == pr.MAX_POSTER_REPAIR_ATTEMPTS
    assert marker["terminal"] == "attempts_exhausted"
    assert "poster_path" not in job.assembly_plan["variants"][0]


def test_first_extraction_failure_only_increments_attempts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job = _FakeJob()
    job.assembly_plan = _variant_plan(job.id)
    session = _FakeSession(job)
    _wire(monkeypatch, session, poster_key=None)

    assert pr._run_repair(str(job.id)) == "extract_failed"

    marker = job.assembly_plan[pr.POSTER_REPAIR_MARKER_FIELD]
    assert marker == {
        "video_path": f"generative-jobs/{job.id}/song-text.mp4",
        "attempts": 1,
        "terminal": None,
        "enqueued_at": None,
    }


def test_terminal_marker_bound_to_the_same_source_is_a_no_op(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job = _FakeJob()
    job.assembly_plan = _variant_plan(job.id)
    job.assembly_plan[pr.POSTER_REPAIR_MARKER_FIELD] = {
        "video_path": f"generative-jobs/{job.id}/song-text.mp4",
        "attempts": 3,
        "terminal": "attempts_exhausted",
        "enqueued_at": "2026-08-30T00:00:00+00:00",
    }
    session = _FakeSession(job)
    calls = _wire(monkeypatch, session)

    assert pr._run_repair(str(job.id)) == "terminal"
    assert calls["generate"] == []
    assert session.commits == 0


# ── Races and rebinds ─────────────────────────────────────────────────────────


def test_stale_race_after_upload_discards_and_deletes_the_uploaded_object(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job = _FakeJob()
    job.assembly_plan = _variant_plan(job.id)
    session = _FakeSession(job)

    def _concurrent_rerender() -> None:
        job.assembly_plan["variants"][0]["video_path"] = f"generative-jobs/{job.id}/rerender.mp4"
        job.assembly_plan["variants"][0]["render_generation_id"] = "gen-2"

    calls = _wire(monkeypatch, session, on_generate=_concurrent_rerender)

    assert pr._run_repair(str(job.id)) == "stale_race"
    assert calls["deleted"] == ["job-posters/x/abc.poster.jpg"]
    assert "poster_path" not in job.assembly_plan["variants"][0]
    assert session.commits == 0


def test_duplicate_variant_ids_discard_the_upload_instead_of_guessing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job = _FakeJob()
    job.assembly_plan = _variant_plan(job.id)
    session = _FakeSession(job)

    def _duplicate_variant() -> None:
        job.assembly_plan["variants"].append(dict(job.assembly_plan["variants"][0]))

    calls = _wire(monkeypatch, session, on_generate=_duplicate_variant)

    assert pr._run_repair(str(job.id)) == "stale_race"
    assert calls["deleted"] == ["job-posters/x/abc.poster.jpg"]


def test_marker_rebinds_and_resets_attempts_when_the_video_path_changes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job = _FakeJob()
    job.assembly_plan = _variant_plan(job.id)
    job.assembly_plan[pr.POSTER_REPAIR_MARKER_FIELD] = {
        "video_path": f"generative-jobs/{job.id}/OLD-render.mp4",
        "attempts": 3,
        "terminal": "attempts_exhausted",
        "enqueued_at": "2026-08-01T00:00:00+00:00",
    }
    session = _FakeSession(job)
    calls = _wire(monkeypatch, session)

    # The stale terminal verdict must NOT short-circuit the fresh source.
    assert pr._run_repair(str(job.id)) == "generated"
    assert len(calls["generate"]) == 1

    marker = job.assembly_plan[pr.POSTER_REPAIR_MARKER_FIELD]
    assert marker["video_path"] == f"generative-jobs/{job.id}/song-text.mp4"
    assert marker["attempts"] == 0
    assert marker["terminal"] is None


# ── Kill switch + non-repairable rows ─────────────────────────────────────────


def test_flag_off_is_a_no_op_that_never_touches_the_database(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "poster_ondemand_repair_enabled", False)

    def _boom() -> None:
        raise AssertionError("the disabled task must not open a session")

    monkeypatch.setattr("app.database.sync_session", _boom)

    assert pr._run_repair(str(uuid.uuid4())) == "disabled"


@pytest.mark.parametrize("status", ["cancelled", "processing_failed", "rendering"])
def test_non_ready_job_is_a_no_op(monkeypatch: pytest.MonkeyPatch, status: str) -> None:
    job = _FakeJob(status=status)
    job.assembly_plan = _variant_plan(job.id)
    session = _FakeSession(job)
    calls = _wire(monkeypatch, session)

    assert pr._run_repair(str(job.id)) == "not_repairable"
    assert calls["generate"] == []
    assert session.commits == 0


def test_deleted_job_is_a_no_op(monkeypatch: pytest.MonkeyPatch) -> None:
    session = _FakeSession(None)
    calls = _wire(monkeypatch, session)

    assert pr._run_repair(str(uuid.uuid4())) == "not_repairable"
    assert calls["generate"] == []


def test_job_with_no_owned_preview_is_a_no_op(monkeypatch: pytest.MonkeyPatch) -> None:
    job = _FakeJob(status="template_ready")
    job.assembly_plan = {"output_url": "https://storage.example/legacy.mp4?expired=1"}
    session = _FakeSession(job)
    calls = _wire(monkeypatch, session)

    assert pr._run_repair(str(job.id)) == "no_preview"
    assert calls["generate"] == []


def test_marker_field_matches_the_route_constant() -> None:
    """The task cannot import the route (layering), so pin the two constants."""
    from app.routes import me

    assert me._POSTER_REPAIR_MARKER_FIELD == pr.POSTER_REPAIR_MARKER_FIELD
