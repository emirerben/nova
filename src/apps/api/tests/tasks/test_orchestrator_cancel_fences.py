"""Terminal-cancellation races for the non-generative render workers."""

from __future__ import annotations

import copy
import importlib
import os
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest

from app.models import Job, JobClip, MusicTrack


class _FakeSession:
    def __init__(self, job, track=None, clip=None):
        self.job = job
        self.track = track
        self.clip = clip
        self.get_calls: list[tuple[object, object, dict]] = []
        self.commits = 0
        self.added: list[object] = []

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return None

    def get(self, model, key, **kwargs):
        self.get_calls.append((model, key, kwargs))
        if model is Job:
            return self.job
        if model is MusicTrack:
            return self.track
        if model is JobClip:
            return self.clip
        raise AssertionError(f"unexpected model lookup: {model}")

    def add(self, value):
        self.added.append(value)

    def commit(self):
        self.commits += 1


@pytest.mark.parametrize(
    ("module_name", "entry_name"),
    [
        ("app.tasks.template_orchestrate", "_run_template_job"),
        ("app.tasks.template_orchestrate", "_run_single_video_job_entry"),
        ("app.tasks.music_orchestrate", "_run_music_job"),
        ("app.tasks.music_orchestrate", "_run_templated_music_job"),
        ("app.tasks.auto_music_orchestrate", "_run_auto_music_job"),
    ],
)
def test_cancelled_job_is_noop_at_every_render_entry(
    monkeypatch,
    module_name: str,
    entry_name: str,
) -> None:
    module = importlib.import_module(module_name)
    job_id = str(uuid4())
    forensic_plan = {"cancelled_evidence": "preserve"}
    job = SimpleNamespace(
        id=UUID(job_id),
        status="cancelled",
        error_detail="cancelled by user",
        failure_reason="cancelled_by_user",
        assembly_plan=forensic_plan,
    )
    session = _FakeSession(job)
    monkeypatch.setattr(module, "_sync_session", lambda: session)

    getattr(module, entry_name)(job_id)

    assert session.commits == 0
    assert job.status == "cancelled"
    assert job.error_detail == "cancelled by user"
    assert job.failure_reason == "cancelled_by_user"
    assert job.assembly_plan is forensic_plan
    assert session.get_calls
    assert session.get_calls[0][2] == {"with_for_update": True}


def test_cancelled_lyrics_preview_is_noop_at_entry(monkeypatch) -> None:
    from app.tasks import lyrics_preview_task as task_mod

    job_id = str(uuid4())
    job = SimpleNamespace(
        id=UUID(job_id),
        status="cancelled",
        error_detail="cancel evidence",
        failure_reason="cancelled_by_user",
        assembly_plan={"preserve": True},
    )
    session = _FakeSession(job)
    render_calls: list[str] = []
    monkeypatch.setattr(task_mod, "_sync_session", lambda: session)
    monkeypatch.setattr(
        task_mod,
        "render_lyrics_preview",
        lambda *_args, **_kwargs: render_calls.append("render"),
    )

    task_mod.render_lyrics_preview_task.run(job_id)

    assert render_calls == []
    assert session.commits == 0
    assert job.status == "cancelled"
    assert job.assembly_plan == {"preserve": True}
    assert session.get_calls[0][2] == {"with_for_update": True}


def test_cancelled_legacy_orchestrator_and_clip_are_noops_at_entry(monkeypatch) -> None:
    from app.tasks import orchestrate as task_mod

    job_id = str(uuid4())
    clip_id = str(uuid4())
    forensic_plan = {"preserve": True}
    job = SimpleNamespace(
        id=UUID(job_id),
        status="cancelled",
        assembly_plan=forensic_plan,
        error_detail="cancel evidence",
        failure_reason="cancelled_by_user",
    )
    clip = SimpleNamespace(id=UUID(clip_id), render_status="pending")
    session = _FakeSession(job, clip=clip)
    monkeypatch.setattr(task_mod, "_sync_session", lambda: session)

    task_mod.orchestrate_job.run(job_id)
    render_result = task_mod.render_clip.run(job_id, clip_id)
    task_mod.finalize_job.run(
        [{"clip_id": clip_id, "success": True, "error": None}],
        job_id,
    )

    assert render_result == {"clip_id": clip_id, "success": False, "error": "cancelled"}
    assert session.commits == 0
    assert clip.render_status == "pending"
    assert job.status == "cancelled"
    assert job.assembly_plan is forensic_plan
    assert all(call[2] == {"with_for_update": True} for call in session.get_calls)


def test_legacy_orchestrator_late_failure_preserves_cancelled_tombstone(monkeypatch) -> None:
    from app.tasks import orchestrate as task_mod

    job_id = str(uuid4())
    forensic_plan = {"preserve": True}
    job = SimpleNamespace(
        id=UUID(job_id),
        status="queued",
        raw_storage_path="raw/source.mp4",
        assembly_plan=forensic_plan,
        error_detail=None,
        failure_reason=None,
    )
    session = _FakeSession(job)
    monkeypatch.setattr(task_mod, "_sync_session", lambda: session)

    def _cancel_then_fail(*_args):
        job.status = "cancelled"
        job.error_detail = "cancel evidence"
        job.failure_reason = "cancelled_by_user"
        raise RuntimeError("late download failure")

    monkeypatch.setattr(task_mod, "download_to_file", _cancel_then_fail)

    task_mod.orchestrate_job.run(job_id)

    assert session.commits == 1  # processing start only
    assert job.status == "cancelled"
    assert job.error_detail == "cancel evidence"
    assert job.failure_reason == "cancelled_by_user"
    assert job.assembly_plan is forensic_plan


def test_cancelled_legacy_chord_finalize_cleans_success_outputs_outside_lock(
    monkeypatch,
) -> None:
    from app.tasks import orchestrate as task_mod

    job_id = str(uuid4())
    job = SimpleNamespace(id=UUID(job_id), status="cancelled", assembly_plan={"keep": True})
    output_paths = [
        f"user/{job_id}/task-runs/run/clip_1.mp4",
        f"user/{job_id}/task-runs/run/thumb_1.jpg",
    ]
    deleted: list[str] = []
    session_open = False

    class _TrackedSession(_FakeSession):
        def __enter__(self):
            nonlocal session_open
            session_open = True
            return super().__enter__()

        def __exit__(self, *_exc):
            nonlocal session_open
            session_open = False
            return None

    tracked = _TrackedSession(job)
    monkeypatch.setattr(task_mod, "_sync_session", lambda: tracked)

    def _delete(_job_id, paths):
        assert session_open is False
        deleted.extend(paths)

    monkeypatch.setattr(task_mod, "delete_task_owned_outputs", _delete)

    task_mod.finalize_job.run(
        [
            {
                "clip_id": str(uuid4()),
                "success": True,
                "error": None,
                "output_paths": output_paths,
            }
        ],
        job_id,
    )

    assert deleted == output_paths
    assert tracked.commits == 0
    assert job.status == "cancelled"


@pytest.mark.parametrize(
    ("module_name", "failure_name", "failure_args"),
    [
        (
            "app.tasks.template_orchestrate",
            "_mark_failed",
            lambda job_id: (UUID(job_id), "unknown", "late failure"),
        ),
        (
            "app.tasks.music_orchestrate",
            "_fail_job",
            lambda job_id: (job_id, "late failure"),
        ),
        (
            "app.tasks.auto_music_orchestrate",
            "_fail_job",
            lambda job_id: (job_id, "late failure"),
        ),
        (
            "app.tasks.lyrics_preview_task",
            "_fail_preview_job",
            lambda job_id: (job_id, "late failure"),
        ),
    ],
)
def test_failure_handlers_cannot_overwrite_cancelled_job(
    monkeypatch,
    module_name: str,
    failure_name: str,
    failure_args,
) -> None:
    module = importlib.import_module(module_name)
    job_id = str(uuid4())
    forensic_plan = {"preserve": ["trace", "inputs"]}
    job = SimpleNamespace(
        id=UUID(job_id),
        status="cancelled",
        error_detail="cancel evidence",
        failure_reason="cancelled_by_user",
        assembly_plan=forensic_plan,
    )
    session = _FakeSession(job)
    monkeypatch.setattr(module, "_sync_session", lambda: session)
    if module_name == "app.tasks.template_orchestrate":
        phase_calls: list[str] = []
        monkeypatch.setattr(module, "mark_failed_phase", lambda *_: phase_calls.append("phase"))
    else:
        phase_calls = []

    getattr(module, failure_name)(*failure_args(job_id))

    assert session.commits == 0
    assert job.status == "cancelled"
    assert job.error_detail == "cancel evidence"
    assert job.failure_reason == "cancelled_by_user"
    assert job.assembly_plan is forensic_plan
    assert phase_calls == []
    assert session.get_calls[0][2] == {"with_for_update": True}


def test_lyrics_preview_cancellation_between_upload_and_finalize_cleans_exact_output(
    monkeypatch,
) -> None:
    from app.tasks import lyrics_preview_task as task_mod

    job_id = str(uuid4())
    track_id = uuid4()
    forensic_plan = {"before_cancel": True}
    job = SimpleNamespace(
        id=UUID(job_id),
        status="queued",
        music_track_id=track_id,
        all_candidates={"lyrics_config_effective": {"enabled": True}},
        assembly_plan=forensic_plan,
        error_detail=None,
        failure_reason=None,
    )
    track = SimpleNamespace(id=track_id, lyrics_cached={})
    session = _FakeSession(job, track)
    deleted: list[str] = []
    run_id = "preview-attempt"
    output_path = (
        f"music-lyrics-previews/{track_id}/line/{job_id}/task-runs/{run_id}/lyrics-preview.mp4"
    )

    def _render(*_args, **kwargs):
        assert kwargs["task_run_id"] == run_id
        # Simulate the cancellation transaction committing after upload and
        # before this worker reacquires the Job row for finalization.
        job.status = "cancelled"
        job.error_detail = "cancel evidence"
        job.failure_reason = "cancelled_by_user"
        return "https://signed/preview", {"output_gcs_path": output_path}

    monkeypatch.setattr(task_mod, "_sync_session", lambda: session)
    monkeypatch.setattr(
        task_mod,
        "ensure_fresh_lyrics_cached_for_render",
        lambda **_kwargs: {},
    )
    monkeypatch.setattr(task_mod, "new_task_run_id", lambda: run_id)
    monkeypatch.setattr(task_mod, "render_lyrics_preview", _render)
    monkeypatch.setattr(
        task_mod,
        "delete_task_owned_outputs",
        lambda _job_id, paths: deleted.extend(paths),
    )

    task_mod.render_lyrics_preview_task.run(job_id)

    assert deleted == [output_path]
    assert job.status == "cancelled"
    assert job.error_detail == "cancel evidence"
    assert job.failure_reason == "cancelled_by_user"
    assert job.assembly_plan is forensic_plan
    assert session.commits == 1  # start only; cancelled finalize is a no-op


def test_auto_music_late_cancel_deletes_uploaded_variant(monkeypatch, tmp_path) -> None:
    from app.tasks import auto_music_orchestrate as task_mod

    job_id = str(uuid4())
    job = SimpleNamespace(id=UUID(job_id), status="rendering")
    session = _FakeSession(job)
    track = SimpleNamespace(
        id="track-1",
        audio_gcs_path="music/track-1.m4a",
        beat_timestamps_s=[0.0, 1.0],
        track_config={},
        best_sections=None,
        duration_s=2.0,
        ai_labels={},
    )
    recipe = SimpleNamespace(beat_timestamps_s=[], color_grade=None)
    plan = SimpleNamespace(steps=[])
    deleted: list[str] = []
    run_id = "variant-attempt"

    def _mix(_assembled, _audio, final_path, _variant_dir):
        with open(final_path, "wb") as output:
            output.write(b"rendered")

    monkeypatch.setattr(task_mod, "_sync_session", lambda: session)
    monkeypatch.setattr(task_mod, "track_config_with_rank_one", lambda _track: {})
    monkeypatch.setattr(task_mod, "generate_music_recipe", lambda _data: {"slots": []})
    monkeypatch.setattr(task_mod, "TemplateRecipe", lambda **_data: recipe)
    monkeypatch.setattr(task_mod, "_enrich_slots_with_energy", lambda slots, _beats: slots)
    monkeypatch.setattr(task_mod, "consolidate_slots", lambda value, _metas: value)
    monkeypatch.setattr(task_mod, "match", lambda _recipe, _metas: plan)
    monkeypatch.setattr(task_mod, "_assemble_clips", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(task_mod, "_mix_template_audio", _mix)
    monkeypatch.setattr(task_mod, "new_task_run_id", lambda: run_id)
    monkeypatch.setattr("app.storage.upload_public_read", lambda *_args: "https://signed/out")
    monkeypatch.setattr(task_mod, "_write_variant_jobclip", lambda **_kwargs: False)
    monkeypatch.setattr(
        task_mod,
        "delete_task_owned_outputs",
        lambda _job_id, paths: deleted.extend(paths),
    )

    result = task_mod._render_one_variant(
        job_id=job_id,
        rank=1,
        track=track,
        match_score=8.0,
        match_rationale="fit",
        clip_metas=[],
        clip_id_to_local={},
        clip_id_to_gcs={},
        probe_map={},
        variant_dir=os.fspath(tmp_path),
    )

    expected = f"auto-music-jobs/{job_id}/task-runs/{run_id}/variant_1.mp4"
    assert result["cancelled"] is True
    assert deleted == [expected]
    assert session.commits == 0


def test_cancelled_auto_music_rejects_jobclip_insert(monkeypatch) -> None:
    from app.tasks import auto_music_orchestrate as task_mod

    job_id = str(uuid4())
    job = SimpleNamespace(id=UUID(job_id), status="cancelled", assembly_plan={"keep": True})
    session = _FakeSession(job)
    monkeypatch.setattr(task_mod, "_sync_session", lambda: session)

    persisted = task_mod._write_variant_jobclip(
        job_id=job_id,
        rank=1,
        track_id="track-1",
        match_score=8.0,
        match_rationale="fit",
        video_path=f"auto-music-jobs/{job_id}/task-runs/run/variant_1.mp4",
        assembly_plan={"new": True},
        render_status="ready",
    )

    assert persisted is False
    assert session.added == []
    assert session.commits == 0
    assert job.assembly_plan == {"keep": True}


def test_legacy_render_cancellation_after_upload_cleans_attempt_outputs(
    monkeypatch,
) -> None:
    from app.tasks import orchestrate as task_mod

    job_id = str(uuid4())
    clip_id = str(uuid4())
    job = SimpleNamespace(
        id=UUID(job_id),
        status="processing",
        raw_storage_path="raw/source.mp4",
        probe_metadata={"aspect_ratio": "9:16", "color_transfer": ""},
        selected_platforms=["instagram"],
        user_id=uuid4(),
        transcript={"low_confidence": True, "words": []},
        scene_cuts=[],
        error_detail=None,
        failure_reason=None,
    )
    clip = SimpleNamespace(
        id=UUID(clip_id),
        job_id=UUID(job_id),
        start_s=0.0,
        end_s=2.0,
        hook_text="",
        rank=1,
        render_status="pending",
        video_path=None,
        thumbnail_path=None,
    )
    session = _FakeSession(job, clip=clip)
    deleted: list[str] = []
    run_id = "legacy-attempt"

    def _download(_storage_path, local_path):
        with open(local_path, "wb") as output:
            output.write(b"raw")

    def _thumbnail(**kwargs):
        jpeg_path = os.path.join(kwargs["output_dir"], "thumb.jpg")
        with open(jpeg_path, "wb") as output:
            output.write(b"jpeg")
        return SimpleNamespace(jpeg_path=jpeg_path)

    def _reframe(**kwargs):
        with open(kwargs["output_path"], "wb") as output:
            output.write(b"video")

    def _upload_thumb(_data, _path):
        # Cancellation wins after both task-owned objects exist but before the
        # worker can reacquire the Job row for finalization.
        job.status = "cancelled"
        job.error_detail = "cancel evidence"
        job.failure_reason = "cancelled_by_user"
        return "https://signed/thumb"

    platform_copy = SimpleNamespace(model_dump=lambda: {"instagram": {}})
    monkeypatch.setattr(task_mod, "_sync_session", lambda: session)
    monkeypatch.setattr(task_mod, "download_to_file", _download)
    monkeypatch.setattr(
        task_mod,
        "generate_copy",
        lambda **_kwargs: (platform_copy, "generated"),
    )
    monkeypatch.setattr(task_mod, "select_thumbnail", _thumbnail)
    monkeypatch.setattr(task_mod, "_generate_captions", lambda *_args: None)
    monkeypatch.setattr(task_mod, "reframe_and_export", _reframe)
    monkeypatch.setattr(
        task_mod,
        "validate_output",
        lambda _path: SimpleNamespace(passed=True, errors=[]),
    )
    monkeypatch.setattr(task_mod, "new_task_run_id", lambda: run_id)
    monkeypatch.setattr(task_mod, "upload_public_read", lambda *_args: "https://signed/video")
    monkeypatch.setattr(task_mod, "upload_bytes_public_read", _upload_thumb)
    monkeypatch.setattr(
        task_mod,
        "delete_task_owned_outputs",
        lambda _job_id, paths: deleted.extend(paths),
    )

    result = task_mod.render_clip.run(job_id, clip_id)

    prefix = f"{job.user_id}/{job_id}/task-runs/{run_id}"
    assert result == {"clip_id": clip_id, "success": False, "error": "cancelled"}
    assert deleted == [f"{prefix}/clip_1.mp4", f"{prefix}/thumb_1.jpg"]
    assert job.status == "cancelled"
    assert job.error_detail == "cancel evidence"
    assert job.failure_reason == "cancelled_by_user"
    assert clip.render_status == "rendering"
    assert clip.video_path is None
    assert clip.thumbnail_path is None
    assert session.commits == 1  # rendering claim only


def _run_legacy_clip_redelivery_with_backfill_poster(
    monkeypatch,
    *,
    cleanup_error: Exception | None = None,
    finalize_clip_job_id: UUID | None = None,
    assembly_plan_override: object | None = None,
    finalize_commit_error: Exception | None = None,
    finalize_commit_outcome: str = "applied",
):
    from app.services.video_poster_cleanup import (
        VIDEO_POSTER_BACKFILL_CLEANUP_FIELD,
    )
    from app.tasks import orchestrate as task_mod

    job_id = str(uuid4())
    clip_id = str(uuid4())
    user_id = uuid4()
    old_thumbnail_path = f"{user_id}/{job_id}/legacy.mp4.poster.backfill-{uuid4()}.jpg"
    original_plan = (
        assembly_plan_override
        if assembly_plan_override is not None
        else {"preserve": {"nested": True}}
    )
    job = SimpleNamespace(
        id=UUID(job_id),
        status="clips_ready",
        raw_storage_path="raw/source.mp4",
        probe_metadata={"aspect_ratio": "9:16", "color_transfer": ""},
        selected_platforms=["instagram"],
        user_id=user_id,
        transcript={"low_confidence": True, "words": []},
        scene_cuts=[],
        assembly_plan=original_plan,
        error_detail=None,
        failure_reason=None,
    )
    # A ready row models a redelivered Celery render task replacing a poster
    # installed by the historical backfill.
    clip = SimpleNamespace(
        id=UUID(clip_id),
        job_id=UUID(job_id),
        start_s=0.0,
        end_s=2.0,
        hook_text="",
        rank=1,
        render_status="ready",
        video_path="https://signed/old-video",
        thumbnail_path=old_thumbnail_path,
    )

    class _FinalizeSession(_FakeSession):
        def __init__(self):
            super().__init__(job, clip=clip)
            self._job_snapshot: dict | None = None
            self._clip_snapshot: dict | None = None
            self._raised_finalize_error = False

        def commit(self):
            super().commit()
            if self.commits == 1:
                self._job_snapshot = copy.deepcopy(job.__dict__)
                self._clip_snapshot = copy.deepcopy(clip.__dict__)
                return
            if (
                self.commits == 2
                and finalize_commit_error is not None
                and not self._raised_finalize_error
            ):
                self._raised_finalize_error = True
                if finalize_commit_outcome in {"not_committed", "partial"}:
                    assert self._job_snapshot is not None
                    assert self._clip_snapshot is not None
                    job.__dict__.clear()
                    job.__dict__.update(copy.deepcopy(self._job_snapshot))
                    clip.__dict__.clear()
                    clip.__dict__.update(copy.deepcopy(self._clip_snapshot))
                if finalize_commit_outcome == "partial":
                    clip.video_path = "https://signed/video"
                elif finalize_commit_outcome not in {"applied", "not_committed"}:
                    raise AssertionError(
                        f"unknown finalize commit outcome: {finalize_commit_outcome}"
                    )
                raise finalize_commit_error

    session = _FinalizeSession()
    run_id = "legacy-redelivery"
    deleted: list[str] = []
    reconcile_calls: list[str] = []
    flagged: list[tuple[object, str]] = []

    def _download(_storage_path, local_path):
        with open(local_path, "wb") as output:
            output.write(b"raw")

    def _thumbnail(**kwargs):
        jpeg_path = os.path.join(kwargs["output_dir"], "thumb.jpg")
        with open(jpeg_path, "wb") as output:
            output.write(b"jpeg")
        return SimpleNamespace(jpeg_path=jpeg_path)

    def _reframe(**kwargs):
        with open(kwargs["output_path"], "wb") as output:
            output.write(b"video")

    def _reconcile(received_job_id):
        # The durable receipt must commit before any network/storage cleanup.
        assert session.commits == 2
        reconcile_calls.append(received_job_id)
        if cleanup_error is not None:
            raise cleanup_error

    platform_copy = SimpleNamespace(model_dump=lambda: {"instagram": {}})
    monkeypatch.setattr(task_mod, "_sync_session", lambda: session)
    monkeypatch.setattr(task_mod, "download_to_file", _download)
    monkeypatch.setattr(
        task_mod,
        "generate_copy",
        lambda **_kwargs: (platform_copy, "generated"),
    )
    monkeypatch.setattr(task_mod, "select_thumbnail", _thumbnail)
    monkeypatch.setattr(task_mod, "_generate_captions", lambda *_args: None)
    monkeypatch.setattr(task_mod, "reframe_and_export", _reframe)
    monkeypatch.setattr(
        task_mod,
        "validate_output",
        lambda _path: SimpleNamespace(passed=True, errors=[]),
    )
    monkeypatch.setattr(task_mod, "new_task_run_id", lambda: run_id)
    monkeypatch.setattr(task_mod, "upload_public_read", lambda *_args: "https://signed/video")

    def _upload_thumbnail(*_args):
        if finalize_clip_job_id is not None:
            # Simulate stale/mismatched task identity appearing after the valid
            # entry claim but before the worker reacquires both row locks.
            clip.job_id = finalize_clip_job_id

    monkeypatch.setattr(task_mod, "upload_bytes_public_read", _upload_thumbnail)
    monkeypatch.setattr(
        task_mod,
        "delete_task_owned_outputs",
        lambda _job_id, paths: deleted.extend(paths),
    )
    monkeypatch.setattr(task_mod, "flag_modified", lambda obj, key: flagged.append((obj, key)))
    monkeypatch.setattr(task_mod, "reconcile_video_poster_cleanup_receipts", _reconcile)

    result = task_mod.render_clip.run(job_id, clip_id)
    expected_thumbnail_path = f"{user_id}/{job_id}/task-runs/{run_id}/thumb_1.jpg"
    receipts = (
        job.assembly_plan.get(VIDEO_POSTER_BACKFILL_CLEANUP_FIELD, [])
        if isinstance(job.assembly_plan, dict)
        else []
    )
    return SimpleNamespace(
        result=result,
        job_id=job_id,
        job=job,
        clip=clip,
        session=session,
        original_plan=original_plan,
        old_thumbnail_path=old_thumbnail_path,
        expected_thumbnail_path=expected_thumbnail_path,
        receipts=receipts,
        reconcile_calls=reconcile_calls,
        deleted=deleted,
        flagged=flagged,
    )


def test_legacy_clip_redelivery_journals_backfill_poster_before_reconcile(
    monkeypatch,
) -> None:
    outcome = _run_legacy_clip_redelivery_with_backfill_poster(monkeypatch)

    assert outcome.result["success"] is True
    assert outcome.clip.render_status == "ready"
    assert outcome.clip.thumbnail_path == outcome.expected_thumbnail_path
    assert outcome.receipts == [
        {
            "old_path": outcome.old_thumbnail_path,
            "replacement_path": outcome.expected_thumbnail_path,
        }
    ]
    assert outcome.job.assembly_plan is not outcome.original_plan
    assert outcome.original_plan == {"preserve": {"nested": True}}
    assert outcome.flagged == [(outcome.job, "assembly_plan")]
    assert outcome.reconcile_calls == [outcome.job_id]
    assert outcome.deleted == []


def test_legacy_clip_cleanup_failure_leaves_committed_receipt(monkeypatch) -> None:
    outcome = _run_legacy_clip_redelivery_with_backfill_poster(
        monkeypatch,
        cleanup_error=RuntimeError("temporary storage outage"),
    )

    assert outcome.result["success"] is True
    assert outcome.session.commits == 2
    assert outcome.receipts == [
        {
            "old_path": outcome.old_thumbnail_path,
            "replacement_path": outcome.expected_thumbnail_path,
        }
    ]
    assert outcome.reconcile_calls == [outcome.job_id]
    assert outcome.deleted == []


def test_legacy_clip_cleanup_soft_timeout_keeps_committed_ready_output(monkeypatch) -> None:
    from app.tasks import orchestrate as task_mod

    outcome = _run_legacy_clip_redelivery_with_backfill_poster(
        monkeypatch,
        cleanup_error=task_mod.SoftTimeLimitExceeded(),
    )

    assert outcome.result["success"] is True
    assert outcome.clip.render_status == "ready"
    assert outcome.clip.video_path == "https://signed/video"
    assert outcome.clip.thumbnail_path == outcome.expected_thumbnail_path
    assert outcome.session.commits == 2
    assert outcome.receipts == [
        {
            "old_path": outcome.old_thumbnail_path,
            "replacement_path": outcome.expected_thumbnail_path,
        }
    ]
    assert outcome.reconcile_calls == [outcome.job_id]
    assert outcome.deleted == []


def test_legacy_clip_postcommit_soft_timeout_is_fresh_read_confirmed(
    monkeypatch,
) -> None:
    from app.tasks import orchestrate as task_mod

    outcome = _run_legacy_clip_redelivery_with_backfill_poster(
        monkeypatch,
        finalize_commit_error=task_mod.SoftTimeLimitExceeded(),
        finalize_commit_outcome="applied",
    )

    assert outcome.result["success"] is True
    assert outcome.clip.render_status == "ready"
    assert outcome.clip.video_path == "https://signed/video"
    assert outcome.clip.thumbnail_path == outcome.expected_thumbnail_path
    assert outcome.session.commits == 2
    assert outcome.reconcile_calls == [outcome.job_id]
    assert outcome.deleted == []


def test_legacy_clip_definitive_noncommit_deletes_attempt_outputs(monkeypatch) -> None:
    outcome = _run_legacy_clip_redelivery_with_backfill_poster(
        monkeypatch,
        finalize_commit_error=RuntimeError("commit rejected before apply"),
        finalize_commit_outcome="not_committed",
    )

    assert outcome.result == {
        "clip_id": str(outcome.clip.id),
        "success": False,
        "error": "commit rejected before apply",
    }
    assert outcome.clip.render_status == "failed"
    assert outcome.session.commits == 3
    assert outcome.reconcile_calls == []
    assert outcome.deleted == [
        outcome.expected_thumbnail_path.replace("thumb_1.jpg", "clip_1.mp4"),
        outcome.expected_thumbnail_path,
    ]


def test_legacy_clip_partial_commit_reference_fails_closed_without_delete(
    monkeypatch,
) -> None:
    outcome = _run_legacy_clip_redelivery_with_backfill_poster(
        monkeypatch,
        finalize_commit_error=RuntimeError("connection lost during commit"),
        finalize_commit_outcome="partial",
    )

    assert outcome.result == {
        "clip_id": str(outcome.clip.id),
        "success": False,
        "error": "finalization commit outcome is uncertain",
        "finalization_uncertain": True,
    }
    assert outcome.clip.render_status == "rendering"
    assert outcome.clip.video_path == "https://signed/video"
    assert outcome.clip.thumbnail_path == outcome.old_thumbnail_path
    assert outcome.session.commits == 2
    assert outcome.reconcile_calls == []
    assert outcome.deleted == []


def test_legacy_render_rejects_cross_job_clip_before_mutation(monkeypatch) -> None:
    from app.tasks import orchestrate as task_mod

    job_id = str(uuid4())
    clip_id = str(uuid4())
    job = SimpleNamespace(
        id=UUID(job_id),
        status="processing",
        error_detail=None,
        failure_reason=None,
    )
    clip = SimpleNamespace(
        id=UUID(clip_id),
        job_id=uuid4(),
        render_status="pending",
    )
    session = _FakeSession(job, clip=clip)
    download_calls: list[str] = []
    monkeypatch.setattr(task_mod, "_sync_session", lambda: session)
    monkeypatch.setattr(
        task_mod,
        "download_to_file",
        lambda *_args: download_calls.append("download"),
    )

    result = task_mod.render_clip.run(job_id, clip_id)

    assert result == {
        "clip_id": clip_id,
        "success": False,
        "error": "DB record not found",
    }
    assert clip.render_status == "pending"
    assert session.commits == 0
    assert download_calls == []


def test_legacy_render_finalize_rechecks_clip_owner_and_cleans_outputs(monkeypatch) -> None:
    foreign_job_id = uuid4()
    outcome = _run_legacy_clip_redelivery_with_backfill_poster(
        monkeypatch,
        finalize_clip_job_id=foreign_job_id,
    )

    assert outcome.result == {
        "clip_id": str(outcome.clip.id),
        "success": False,
        "error": "cancelled",
    }
    assert outcome.clip.job_id == foreign_job_id
    assert outcome.clip.render_status == "rendering"
    assert outcome.clip.video_path == "https://signed/old-video"
    assert outcome.clip.thumbnail_path == outcome.old_thumbnail_path
    assert outcome.job.assembly_plan is outcome.original_plan
    assert outcome.receipts == []
    assert outcome.reconcile_calls == []
    assert outcome.flagged == []
    assert outcome.deleted == [
        outcome.expected_thumbnail_path.replace("thumb_1.jpg", "clip_1.mp4"),
        outcome.expected_thumbnail_path,
    ]
    assert outcome.session.commits == 1


def test_legacy_render_fails_closed_on_non_object_plan_before_clip_mutation(
    monkeypatch,
) -> None:
    corrupt_plan = ["preserve", {"forensic": True}]
    outcome = _run_legacy_clip_redelivery_with_backfill_poster(
        monkeypatch,
        assembly_plan_override=corrupt_plan,
    )

    assert outcome.result == {
        "clip_id": str(outcome.clip.id),
        "success": False,
        "error": "invalid assembly plan",
    }
    assert outcome.job.assembly_plan is corrupt_plan
    assert outcome.job.assembly_plan == ["preserve", {"forensic": True}]
    assert outcome.clip.render_status == "rendering"
    assert outcome.clip.video_path == "https://signed/old-video"
    assert outcome.clip.thumbnail_path == outcome.old_thumbnail_path
    assert outcome.receipts == []
    assert outcome.reconcile_calls == []
    assert outcome.flagged == []
    assert outcome.deleted == [
        outcome.expected_thumbnail_path.replace("thumb_1.jpg", "clip_1.mp4"),
        outcome.expected_thumbnail_path,
    ]
    assert outcome.session.commits == 1


@pytest.mark.parametrize("terminal_kind", ["timeout", "error"])
def test_legacy_render_terminal_failure_never_mutates_reassigned_clip(
    monkeypatch,
    terminal_kind: str,
) -> None:
    from app.tasks import orchestrate as task_mod

    job_id = str(uuid4())
    clip_id = str(uuid4())
    foreign_job_id = uuid4()
    job = SimpleNamespace(
        id=UUID(job_id),
        status="processing",
        raw_storage_path="raw/source.mp4",
        probe_metadata={"aspect_ratio": "9:16", "color_transfer": ""},
        selected_platforms=["instagram"],
        user_id=uuid4(),
        transcript={"low_confidence": True, "words": []},
        scene_cuts=[],
        error_detail=None,
        failure_reason=None,
    )
    clip = SimpleNamespace(
        id=UUID(clip_id),
        job_id=UUID(job_id),
        start_s=0.0,
        end_s=2.0,
        hook_text="",
        rank=1,
        render_status="pending",
        error_detail="foreign evidence",
    )
    session = _FakeSession(job, clip=clip)

    def fail_after_reassignment(*_args):
        clip.job_id = foreign_job_id
        if terminal_kind == "timeout":
            raise task_mod.SoftTimeLimitExceeded()
        raise RuntimeError("render exploded")

    monkeypatch.setattr(task_mod, "_sync_session", lambda: session)
    monkeypatch.setattr(task_mod, "download_to_file", fail_after_reassignment)
    monkeypatch.setattr(task_mod, "delete_task_owned_outputs", lambda *_args: None)

    result = task_mod.render_clip.run(job_id, clip_id)

    assert result == {
        "clip_id": clip_id,
        "success": False,
        "error": "render timeout" if terminal_kind == "timeout" else "render exploded",
    }
    assert clip.job_id == foreign_job_id
    assert clip.render_status == "rendering"
    assert clip.error_detail == "foreign evidence"
    assert session.commits == 1


def test_cleanup_refuses_stable_or_user_paths(monkeypatch) -> None:
    from app.tasks import _job_cancel_fence as fence

    deleted: list[str] = []
    monkeypatch.setattr(
        "app.storage.delete_object_best_effort",
        lambda path: deleted.append(path) or True,
    )

    fence.delete_task_owned_outputs(
        "job-1",
        [
            "jobs/job-1/template_output.mp4",
            "user-uploads/job-1/source.mp4",
            "jobs/job-2/task-runs/run-1/template_output.mp4",
            "jobs/job-1/task-runs/run-1/template_output.mp4",
        ],
    )

    assert deleted == ["jobs/job-1/task-runs/run-1/template_output.mp4"]
