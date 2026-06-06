from types import SimpleNamespace
from uuid import UUID, uuid4

from app.worker import celery_app


def test_worker_imports_lyrics_preview_task_module() -> None:
    """Preview jobs are enqueueable only if the worker imports the task module."""
    assert "app.tasks.lyrics_preview_task" in celery_app.conf.include


def test_lyrics_preview_task_keeps_canonical_celery_name() -> None:
    from app.tasks.lyrics_preview_task import render_lyrics_preview_task

    assert render_lyrics_preview_task.name == "tasks.render_lyrics_preview_task"


def test_lyrics_preview_task_refreshes_stale_cache_before_render(monkeypatch) -> None:
    from app.tasks import lyrics_preview_task as task_mod

    job_id = uuid4()
    track_id = uuid4()
    stale = {"prompt_version": "old", "source": "lrclib_synced+whisper", "lines": []}
    fresh = {"prompt_version": "live", "source": "lrclib_synced+whisper", "lines": [{"text": "ok"}]}
    effective_cfg = {"enabled": True, "style": "line"}
    job = SimpleNamespace(
        id=job_id,
        music_track_id=track_id,
        all_candidates={"lyrics_config_effective": effective_cfg},
        assembly_plan={},
        status="queued",
    )
    track = SimpleNamespace(id=track_id, lyrics_cached=stale)
    commits = 0
    calls: list[str] = []

    class FakeSession:
        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return None

        def get(self, model, key):
            if model is task_mod.Job:
                assert key == UUID(str(job_id))
                return job
            if model is task_mod.MusicTrack:
                assert key == track_id
                return track
            raise AssertionError(f"unexpected model lookup: {model}")

        def commit(self):
            nonlocal commits
            commits += 1

    def fake_refresh(*, track_id: str, lyrics_cached: dict, lyrics_config: dict, reason: str):
        calls.append("refresh")
        assert track_id == str(track.id)
        assert lyrics_cached is stale
        assert lyrics_config is effective_cfg
        assert reason == "lyrics_preview"
        return fresh

    def fake_render(render_track, lyrics_config, job_id: str):
        calls.append("render")
        assert render_track is track
        assert render_track.lyrics_cached is fresh
        assert lyrics_config is effective_cfg
        assert job_id == str(job.id)
        return "https://example.test/preview.mp4", {"preview_start_s": 41.7}

    monkeypatch.setattr(task_mod, "_sync_session", lambda: FakeSession())
    monkeypatch.setattr(task_mod, "ensure_fresh_lyrics_cached_for_render", fake_refresh)
    monkeypatch.setattr(task_mod, "render_lyrics_preview", fake_render)

    task_mod.render_lyrics_preview_task.run(str(job_id))

    assert calls == ["refresh", "render"]
    assert commits == 2
    assert job.status == "music_ready"
    assert job.assembly_plan == {
        "preview_start_s": 41.7,
        "output_url": "https://example.test/preview.mp4",
        "lyrics_config_effective": effective_cfg,
    }
