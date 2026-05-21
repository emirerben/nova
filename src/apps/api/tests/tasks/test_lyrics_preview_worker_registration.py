from app.worker import celery_app


def test_worker_imports_lyrics_preview_task_module() -> None:
    """Preview jobs are enqueueable only if the worker imports the task module."""
    assert "app.tasks.lyrics_preview_task" in celery_app.conf.include


def test_lyrics_preview_task_keeps_canonical_celery_name() -> None:
    from app.tasks.lyrics_preview_task import render_lyrics_preview_task

    assert render_lyrics_preview_task.name == "tasks.render_lyrics_preview_task"
