from __future__ import annotations

import copy
import inspect
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest
from billiard.exceptions import SoftTimeLimitExceeded

from app.models import Job
from app.pipeline import image_clip, probe
from app.pipeline.agents import copy_writer, gemini_analyzer
from app.services.video_poster_cleanup import VIDEO_POSTER_BACKFILL_CLEANUP_FIELD
from app.tasks import music_orchestrate, template_orchestrate


def _poster(source: str, token: int) -> str:
    return f"{source}.poster.backfill-{uuid.UUID(int=token)}.jpg"


@pytest.mark.parametrize("module", [music_orchestrate, template_orchestrate])
def test_post_commit_cleanup_defers_celery_soft_timeout(monkeypatch, module) -> None:
    monkeypatch.setattr(
        module,
        "reconcile_video_poster_cleanup_receipts",
        lambda _job_id: (_ for _ in ()).throw(SoftTimeLimitExceeded()),
    )

    module._reconcile_retired_top_level_posters("job-id", ["poster.jpg"])


def _previous_plan(job_id: str) -> tuple[dict, dict[str, str]]:
    primary_first_source = f"jobs/{job_id}/old-primary-first.mp4"
    primary_current_source = f"jobs/{job_id}/old-primary-current.mp4"
    base_first_source = f"jobs/{job_id}/old-base-first.mp4"
    base_current_source = f"jobs/{job_id}/old-base-current.mp4"
    paths = {
        "primary_first": _poster(primary_first_source, 1),
        "primary_current": _poster(primary_current_source, 2),
        "base_first": _poster(base_first_source, 3),
        "base_current": _poster(base_current_source, 4),
    }
    return (
        {
            "output_path": primary_current_source,
            "poster_path": paths["primary_current"],
            "base_output_path": base_current_source,
            "base_poster_path": paths["base_current"],
            "preserved_marker": "prior-committed-plan",
            VIDEO_POSTER_BACKFILL_CLEANUP_FIELD: [
                {
                    "old_path": paths["primary_first"],
                    "replacement_path": paths["primary_current"],
                },
                {
                    "old_path": paths["base_first"],
                    "replacement_path": paths["base_current"],
                },
            ],
        },
        paths,
    )


class _Session:
    def __init__(
        self,
        resources: dict[type, object],
        events: list[str],
        *,
        job: object,
        finalize_commit_error: Exception | None = None,
        finalize_exit_error: Exception | None = None,
    ) -> None:
        self.resources = resources
        self.events = events
        self.job = job
        self.finalize_commit_error = finalize_commit_error
        self.finalize_exit_error = finalize_exit_error
        self._finalize_error_raised = False
        self._finalize_exit_error_raised = False

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        if (
            self.finalize_exit_error is not None
            and not self._finalize_exit_error_raised
            and getattr(self.job, "status", None) in {"template_ready", "music_ready"}
        ):
            self._finalize_exit_error_raised = True
            raise self.finalize_exit_error
        return False

    def get(self, model, _row_id, **_kwargs):
        if model is Job:
            return self.job
        return self.resources.get(model)

    def commit(self) -> None:
        self.events.append("commit")
        if (
            self.finalize_commit_error is not None
            and not self._finalize_error_raised
            and getattr(self.job, "status", None) in {"template_ready", "music_ready"}
        ):
            self._finalize_error_raised = True
            raise self.finalize_commit_error


def _install_finalize_boundary(
    monkeypatch,
    *,
    module,
    job,
    resources: dict[type, object],
    events: list[str],
    finalize_commit_error: Exception | None = None,
    finalize_exit_error: Exception | None = None,
) -> None:
    session = _Session(
        resources,
        events,
        job=job,
        finalize_commit_error=finalize_commit_error,
        finalize_exit_error=finalize_exit_error,
    )
    monkeypatch.setattr(module, "_sync_session", lambda: session)
    monkeypatch.setattr(module, "active_job_for_update", lambda *_args, **_kwargs: job)

    def fail_cleanup(job_id: str):
        assert job_id == str(job.id)
        assert job.status in {"template_ready", "music_ready"}
        assert VIDEO_POSTER_BACKFILL_CLEANUP_FIELD in job.assembly_plan
        events.append("cleanup_attempt")
        raise RuntimeError("temporary storage failure")

    monkeypatch.setattr(module, "reconcile_video_poster_cleanup_receipts", fail_cleanup)


def _assert_primary_chain(
    job,
    prior_paths: dict[str, str],
    new_primary: str,
    *,
    new_base: str | None,
    events: list[str],
) -> None:
    receipts = job.assembly_plan[VIDEO_POSTER_BACKFILL_CLEANUP_FIELD]
    expected = {
        (prior_paths["primary_first"], new_primary),
        (prior_paths["primary_current"], new_primary),
    }
    if new_base is None:
        expected.add((prior_paths["base_first"], prior_paths["base_current"]))
    else:
        expected.update(
            {
                (prior_paths["base_first"], new_base),
                (prior_paths["base_current"], new_base),
            }
        )
    assert {(receipt["old_path"], receipt["replacement_path"]) for receipt in receipts} == expected
    # The immediate cleanup raised, but only after the accepted plan and its
    # receipt chain were durably committed. The receipt remains Beat-retryable.
    assert events[-2:] == ["commit", "cleanup_attempt"]


def _template_recipe(slot: dict) -> SimpleNamespace:
    return SimpleNamespace(
        slots=[slot],
        beat_timestamps_s=[],
        color_grade=None,
        interstitials=[],
        copy_tone="casual",
        output_fit="crop",
        transition_duration_s=None,
        clip_filter_hint="",
    )


def test_template_job_finalizer_retargets_receipts_before_failed_cleanup(monkeypatch) -> None:
    job_id = str(uuid.uuid4())
    old_plan, prior_paths = _previous_plan(job_id)
    job = SimpleNamespace(
        id=uuid.UUID(job_id),
        status="queued",
        template_id="template-1",
        assembly_plan=copy.deepcopy(old_plan),
        all_candidates={"clip_paths": [f"jobs/{job_id}/input.mp4"], "preview_mode": True},
        selected_platforms=["tiktok"],
    )
    slot = {"position": 1, "target_duration_s": 1.0, "priority": 1, "slot_type": "body"}
    recipe = _template_recipe(slot)
    template = SimpleNamespace(
        analysis_status="ready",
        recipe_cached={"slots": [slot]},
        audio_gcs_path=None,
        voiceover_gcs_path=None,
        gcs_path=None,
        is_agentic=False,
        single_pass_enabled=False,
        music_track_id=None,
        lyrics_config=None,
    )
    meta = SimpleNamespace(clip_id="clip-a")
    step = SimpleNamespace(
        slot=slot,
        clip_id="clip-a",
        moment={"start_s": 0.0, "end_s": 1.0},
    )
    run_id = "run-new"
    new_source = f"jobs/{job_id}/task-runs/{run_id}/template_output.mp4"
    new_base_source = f"jobs/{job_id}/task-runs/{run_id}/template_base.mp4"
    new_poster = _poster(new_source, 10)
    new_base_poster = _poster(new_base_source, 11)
    events: list[str] = []
    _install_finalize_boundary(
        monkeypatch,
        module=template_orchestrate,
        job=job,
        resources={template_orchestrate.VideoTemplate: template},
        events=events,
        finalize_commit_error=SoftTimeLimitExceeded(),
    )
    monkeypatch.setattr(template_orchestrate, "mark_started", lambda *_args: None)
    monkeypatch.setattr(template_orchestrate, "mark_finished", lambda *_args: None)
    monkeypatch.setattr(template_orchestrate, "record_phase", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(template_orchestrate, "build_recipe", lambda _data: recipe)
    monkeypatch.setattr(
        template_orchestrate,
        "_download_clips_parallel",
        lambda *_args, **_kwargs: ["/tmp/clip-a.mp4"],
    )
    monkeypatch.setattr(
        template_orchestrate,
        "_analyze_clips_with_cache",
        lambda *_args, **_kwargs: ([meta], [meta], [None], {}, 0),
    )
    monkeypatch.setattr(template_orchestrate, "consolidate_slots", lambda value, _metas: value)
    monkeypatch.setattr(
        template_orchestrate,
        "match",
        lambda *_args, **_kwargs: SimpleNamespace(steps=[step]),
    )
    monkeypatch.setattr(template_orchestrate, "_add_locked_template_source", lambda *_args: None)
    monkeypatch.setattr(template_orchestrate, "_assemble_clips", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(template_orchestrate, "new_task_run_id", lambda: run_id)
    monkeypatch.setattr(
        template_orchestrate,
        "upload_public_read",
        lambda _local, remote: f"https://cdn.test/{remote}",
    )
    monkeypatch.setattr(
        template_orchestrate,
        "_try_upload_video_poster",
        lambda _local, remote, **_kwargs: {
            new_source: new_poster,
            new_base_source: new_base_poster,
        }[remote],
    )

    template_orchestrate._run_template_job(job_id)

    assert job.status == "template_ready"
    _assert_primary_chain(
        job,
        prior_paths,
        new_poster,
        new_base=new_base_poster,
        events=events,
    )


def test_template_rerender_finalizer_retargets_receipts_before_failed_cleanup(
    monkeypatch,
) -> None:
    job_id = str(uuid.uuid4())
    old_plan, prior_paths = _previous_plan(job_id)
    slot = {"position": 1, "target_duration_s": 1.0, "priority": 1, "slot_type": "body"}
    old_plan.update(
        {
            "locked": True,
            "steps": [
                {
                    "slot": slot,
                    "clip_id": "clip-a",
                    "clip_gcs_path": f"jobs/{job_id}/input.mp4",
                    "moment": {"start_s": 0.0, "end_s": 1.0},
                }
            ],
        }
    )
    job = SimpleNamespace(
        id=uuid.UUID(job_id),
        status="processing",
        assembly_plan=copy.deepcopy(old_plan),
    )
    template = SimpleNamespace(
        recipe_cached={"slots": [slot]},
        audio_gcs_path=None,
        gcs_path=None,
        single_pass_enabled=False,
        is_agentic=False,
        music_track_id=None,
    )
    recipe = _template_recipe(slot)
    run_id = "run-new"
    new_source = f"jobs/{job_id}/task-runs/{run_id}/template_output.mp4"
    new_base_source = f"jobs/{job_id}/task-runs/{run_id}/template_base.mp4"
    new_poster = _poster(new_source, 12)
    new_base_poster = _poster(new_base_source, 13)
    events: list[str] = []
    _install_finalize_boundary(
        monkeypatch,
        module=template_orchestrate,
        job=job,
        resources={template_orchestrate.VideoTemplate: template},
        events=events,
        finalize_exit_error=RuntimeError("connection lost while closing committed session"),
    )
    monkeypatch.setattr(template_orchestrate, "build_recipe", lambda _data: recipe)
    monkeypatch.setattr(
        template_orchestrate,
        "_download_clips_parallel",
        lambda *_args, **_kwargs: ["/tmp/clip-a.mp4"],
    )
    monkeypatch.setattr(template_orchestrate, "_probe_clips", lambda _paths: {})
    monkeypatch.setattr(template_orchestrate, "_add_locked_template_source", lambda *_args: None)
    monkeypatch.setattr(template_orchestrate, "_assemble_clips", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(template_orchestrate, "new_task_run_id", lambda: run_id)
    monkeypatch.setattr(
        template_orchestrate,
        "upload_public_read",
        lambda _local, remote: f"https://cdn.test/{remote}",
    )
    monkeypatch.setattr(
        template_orchestrate,
        "_try_upload_video_poster",
        lambda _local, remote, **_kwargs: {
            new_source: new_poster,
            new_base_source: new_base_poster,
        }[remote],
    )
    platform_copy = SimpleNamespace(model_dump=lambda: {})
    monkeypatch.setattr(copy_writer, "generate_copy", lambda **_kwargs: (platform_copy, "ok"))

    template_orchestrate._run_rerender(
        job_id,
        {
            "assembly_plan": copy.deepcopy(old_plan),
            "template_id": "template-1",
            "selected_platforms": ["tiktok"],
            "all_candidates": {},
        },
    )

    assert job.status == "template_ready"
    _assert_primary_chain(
        job,
        prior_paths,
        new_poster,
        new_base=new_base_poster,
        events=events,
    )


def test_single_video_finalizer_retargets_receipts_before_failed_cleanup(monkeypatch) -> None:
    job_id = str(uuid.uuid4())
    old_plan, prior_paths = _previous_plan(job_id)
    job = SimpleNamespace(
        id=uuid.UUID(job_id),
        status="processing",
        assembly_plan=copy.deepcopy(old_plan),
        error_detail=None,
    )
    template = SimpleNamespace(gcs_path="templates/template-1/source.mp4")
    run_id = "run-new"
    new_source = f"jobs/{job_id}/task-runs/{run_id}/template_output.mp4"
    new_base_source = f"jobs/{job_id}/task-runs/{run_id}/template_base.mp4"
    new_poster = _poster(new_source, 14)
    new_base_poster = _poster(new_base_source, 15)
    events: list[str] = []
    _install_finalize_boundary(
        monkeypatch,
        module=template_orchestrate,
        job=job,
        resources={template_orchestrate.VideoTemplate: template},
        events=events,
        finalize_commit_error=SoftTimeLimitExceeded(),
    )
    monkeypatch.setattr(template_orchestrate, "record_phase", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(template_orchestrate, "mark_finished", lambda *_args: None)
    monkeypatch.setattr(template_orchestrate, "download_to_file", lambda *_args: None)
    monkeypatch.setattr(
        template_orchestrate,
        "_select_body_window_peak_anchored",
        lambda *_args, **_kwargs: (0.0, 1.0),
    )
    monkeypatch.setattr(
        template_orchestrate, "_extract_intro_from_template_video", lambda *_args: None
    )
    monkeypatch.setattr(template_orchestrate, "_cut_body_segment", lambda *_args: None)
    monkeypatch.setattr(template_orchestrate, "_concat_silent", lambda *_args: None)
    monkeypatch.setattr(template_orchestrate, "new_task_run_id", lambda: run_id)
    monkeypatch.setattr(
        template_orchestrate,
        "upload_public_read",
        lambda _local, remote: f"https://cdn.test/{remote}",
    )
    monkeypatch.setattr(
        template_orchestrate,
        "copy_object_signed_url",
        lambda _source, remote: f"https://cdn.test/{remote}",
    )
    monkeypatch.setattr(
        template_orchestrate,
        "_try_upload_video_poster",
        lambda _local, remote, **_kwargs: new_poster if remote == new_source else new_base_poster,
    )
    monkeypatch.setattr(
        template_orchestrate,
        "poster_object_path",
        lambda remote: new_base_poster if remote == new_base_source else _poster(remote, 99),
    )
    monkeypatch.setattr(template_orchestrate, "copy_object", lambda *_args: None)

    template_orchestrate._run_single_video_job(
        job_id=job_id,
        template_id="template-1",
        recipe_data={"intro_duration_s": 1.0, "body": {"min_duration_s": 1.0}},
        clip_paths_gcs=[f"jobs/{job_id}/input.mp4"],
        audio_gcs_path=None,
        voiceover_gcs_path=None,
        user_subject="",
        selected_platforms=["tiktok"],
        preview_mode=True,
    )

    assert job.status == "template_ready"
    _assert_primary_chain(
        job,
        prior_paths,
        new_poster,
        new_base=new_base_poster,
        events=events,
    )


def _music_track(*, templated: bool, slot: dict) -> SimpleNamespace:
    return SimpleNamespace(
        analysis_status="ready",
        audio_gcs_path="music/track/audio.m4a",
        beat_timestamps_s=[],
        track_config={},
        duration_s=1.0,
        lyrics_cached=None,
        ai_labels=None,
        title="Track",
        recipe_cached={"slots": [slot]} if templated else None,
    )


def _patch_music_common(monkeypatch, job, track, events: list[str]) -> None:
    _install_finalize_boundary(
        monkeypatch,
        module=music_orchestrate,
        job=job,
        resources={music_orchestrate.MusicTrack: track},
        events=events,
        finalize_commit_error=SoftTimeLimitExceeded(),
    )
    monkeypatch.setattr(music_orchestrate, "effective_lyrics_config", lambda *_args: {})
    monkeypatch.setattr(
        music_orchestrate,
        "_maybe_select_lyric_style_set",
        lambda config, *_args: config,
    )
    monkeypatch.setattr(
        music_orchestrate,
        "ensure_fresh_lyrics_cached_for_render",
        lambda **kwargs: kwargs["lyrics_cached"],
    )
    monkeypatch.setattr(
        music_orchestrate,
        "inject_lyric_overlays",
        lambda recipe, *_args, **_kwargs: recipe,
    )
    monkeypatch.setattr(music_orchestrate, "new_task_run_id", lambda: "run-new")


def test_music_job_finalizer_retargets_receipts_before_failed_cleanup(monkeypatch) -> None:
    job_id = str(uuid.uuid4())
    old_plan, prior_paths = _previous_plan(job_id)
    job = SimpleNamespace(
        id=uuid.UUID(job_id),
        status="queued",
        music_track_id="track-1",
        assembly_plan=copy.deepcopy(old_plan),
        all_candidates={"clip_paths": [f"jobs/{job_id}/input.mp4"]},
    )
    slot = {"position": 1, "target_duration_s": 1.0, "priority": 1, "slot_type": "body"}
    track = _music_track(templated=False, slot=slot)
    recipe_dict = {
        "shot_count": 1,
        "total_duration_s": 1.0,
        "hook_duration_s": 1.0,
        "slots": [slot],
    }
    recipe = _template_recipe(slot)
    meta = SimpleNamespace(clip_id="files/clip-a")
    file_ref = SimpleNamespace(name="files/clip-a")
    step = SimpleNamespace(
        slot=slot,
        clip_id="files/clip-a",
        moment={"start_s": 0.0, "end_s": 1.0},
    )
    new_source = f"music-jobs/{job_id}/task-runs/run-new/output.mp4"
    new_poster = _poster(new_source, 16)
    events: list[str] = []
    _patch_music_common(monkeypatch, job, track, events)
    monkeypatch.setattr(music_orchestrate, "generate_music_recipe", lambda _data: recipe_dict)
    monkeypatch.setattr(
        music_orchestrate,
        "compute_snapped_slot_durations",
        lambda *_args, **_kwargs: [1.0],
    )
    monkeypatch.setattr(gemini_analyzer, "build_recipe", lambda _data: recipe)
    monkeypatch.setattr(
        music_orchestrate,
        "_download_clips_parallel",
        lambda *_args: ["/tmp/clip-a.mp4"],
    )
    monkeypatch.setattr(music_orchestrate, "_probe_clips", lambda _paths: {})
    monkeypatch.setattr(music_orchestrate, "_upload_clips_parallel", lambda _paths: [file_ref])
    monkeypatch.setattr(
        music_orchestrate,
        "_analyze_clips_parallel",
        lambda *_args, **_kwargs: ([meta], 0),
    )
    monkeypatch.setattr(
        music_orchestrate,
        "_enrich_slots_with_energy",
        lambda slots, _beats: slots,
    )
    monkeypatch.setattr(music_orchestrate, "consolidate_slots", lambda value, _metas: value)
    monkeypatch.setattr(
        music_orchestrate,
        "match",
        lambda *_args: SimpleNamespace(steps=[step]),
    )
    monkeypatch.setattr(music_orchestrate, "_assemble_clips", lambda *_args, **_kwargs: None)

    def mix(_source, _audio, output, *_args, **_kwargs):
        Path(output).write_bytes(b"video")

    monkeypatch.setattr(music_orchestrate, "_mix_template_audio", mix)
    monkeypatch.setattr(
        "app.storage.upload_public_read",
        lambda _local, remote: f"https://cdn.test/{remote}",
    )
    monkeypatch.setattr(
        music_orchestrate,
        "_try_upload_video_poster",
        lambda _local, remote, **_kwargs: new_poster if remote == new_source else None,
    )

    music_orchestrate._run_music_job(job_id)

    assert job.status == "music_ready"
    assert job.assembly_plan["preserved_marker"] == "prior-committed-plan"
    _assert_primary_chain(job, prior_paths, new_poster, new_base=None, events=events)


def test_templated_music_finalizer_retargets_receipts_before_failed_cleanup(
    monkeypatch,
) -> None:
    job_id = str(uuid.uuid4())
    old_plan, prior_paths = _previous_plan(job_id)
    job = SimpleNamespace(
        id=uuid.UUID(job_id),
        status="queued",
        music_track_id="track-1",
        assembly_plan=copy.deepcopy(old_plan),
        all_candidates={"clip_paths": [f"jobs/{job_id}/input.mp4"]},
    )
    slot = {
        "position": 1,
        "target_duration_s": 1.0,
        "slot_type": "user_upload",
    }
    track = _music_track(templated=True, slot=slot)
    new_source = f"music-jobs/{job_id}/task-runs/run-new/output.mp4"
    new_poster = _poster(new_source, 17)
    events: list[str] = []
    _patch_music_common(monkeypatch, job, track, events)

    def download(_remote, local):
        Path(local).write_bytes(b"input")

    def render_video(*, output_path, **_kwargs):
        Path(output_path).write_bytes(b"slot")

    monkeypatch.setattr(music_orchestrate, "download_to_file", download)
    monkeypatch.setattr(image_clip, "is_image_file", lambda _path: False)
    monkeypatch.setattr(image_clip, "render_video_to_clip", render_video)
    monkeypatch.setattr(
        probe,
        "probe_video",
        lambda _path: SimpleNamespace(duration_s=1.0, aspect_ratio="9:16"),
    )
    monkeypatch.setattr(
        music_orchestrate,
        "_concat_pre_rendered_clips",
        lambda _paths, output, _tmpdir: Path(output).write_bytes(b"assembled"),
    )
    monkeypatch.setattr(
        music_orchestrate, "_collect_absolute_overlays", lambda *_args, **_kwargs: []
    )

    def mix(_source, _audio, output, *_args, **_kwargs):
        Path(output).write_bytes(b"video")

    monkeypatch.setattr(music_orchestrate, "_mix_template_audio", mix)
    monkeypatch.setattr(
        "app.storage.upload_public_read",
        lambda _local, remote: f"https://cdn.test/{remote}",
    )
    monkeypatch.setattr(
        music_orchestrate,
        "_try_upload_video_poster",
        lambda _local, remote, **_kwargs: new_poster if remote == new_source else None,
    )

    music_orchestrate._run_templated_music_job(job_id)

    assert job.status == "music_ready"
    assert job.assembly_plan["preserved_marker"] == "prior-committed-plan"
    _assert_primary_chain(job, prior_paths, new_poster, new_base=None, events=events)


@pytest.mark.parametrize(
    "orchestrator",
    [
        template_orchestrate._run_template_job,
        music_orchestrate._run_music_job,
        music_orchestrate._run_templated_music_job,
    ],
)
def test_intermediate_plan_write_preserves_prior_output_and_receipts(orchestrator) -> None:
    source = inspect.getsource(orchestrator)

    assert "job.assembly_plan = {**(job.assembly_plan or {}), **plan_data}" in source


@pytest.mark.parametrize(
    "module",
    [template_orchestrate, music_orchestrate],
)
def test_immediate_cleanup_failure_never_fails_accepted_render(monkeypatch, module) -> None:
    def _raise(_job_id: str):
        raise RuntimeError("temporary storage failure")

    monkeypatch.setattr(module, "reconcile_video_poster_cleanup_receipts", _raise)

    module._reconcile_retired_top_level_posters("00000000-0000-0000-0000-000000000001", ["p"])
