"""Dispatch-routing and renderer-version-fence tests for slide posts (plans/024).

Mirrors the guided_story dispatch tests in test_guided_story_build.py: mock
`_lock_owned_entry_job` to hand `_run_generative_job_impl` a bare
`SimpleNamespace` job directly, so the real preamble runs but no DB is needed.
"""

from __future__ import annotations

import uuid
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

import app.tasks.generative_build as gb
from app.agents._schemas.edit_format import SLIDES_RENDERER_VERSION


@contextmanager
def _session():
    yield SimpleNamespace(commit=lambda: None)


def _slides_job(**all_candidates_overrides) -> SimpleNamespace:
    all_candidates = {
        "edit_format": "slides",
        "slides_renderer_version": SLIDES_RENDERER_VERSION,
        "clip_paths": [],  # deliberately empty — slides never uses clip_paths
        **all_candidates_overrides,
    }
    return SimpleNamespace(
        id=uuid.uuid4(),
        status="queued",
        mode="content_plan",
        assembly_plan={},
        all_candidates=all_candidates,
        content_plan_item_id=uuid.uuid4(),
        user_id=uuid.uuid4(),
    )


def test_slide_intent_routes_before_the_clip_paths_check(monkeypatch) -> None:
    """A slide-post job with zero clip_paths must still dispatch (risk #6):
    build_generative_job/build_generative_job's caller synthesizes no "clip"
    concept for slides at all, so the legacy `if not clip_paths_gcs: raise`
    guard must never be reached."""
    job = _slides_job()
    calls: list[tuple[str, object]] = []
    monkeypatch.setattr(gb, "_sync_session", _session)
    monkeypatch.setattr(gb, "_lock_owned_entry_job", lambda _db, _job_id: (job, None))
    monkeypatch.setattr(gb, "mark_started", lambda job_id: calls.append(("started", job_id)))
    monkeypatch.setattr(gb, "record_phase", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        gb,
        "_run_slide_post_job",
        lambda job_id, **kwargs: calls.append(("slides", (job_id, kwargs))),
    )
    monkeypatch.setattr(
        gb,
        "_ingest_clips",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("legacy ingest ran")),
    )

    gb._run_generative_job_impl(str(job.id))

    assert calls[0] == ("started", str(job.id))
    assert calls[1][0] == "slides"
    assert calls[1][1][0] == str(job.id)


def test_slide_intent_renderer_version_mismatch_fails_closed(monkeypatch) -> None:
    """A mixed API/worker deploy must never silently coerce a stale-fenced
    slides job to montage (risk #5) — it must raise, not fall through."""
    job = _slides_job(slides_renderer_version=SLIDES_RENDERER_VERSION - 1)
    monkeypatch.setattr(gb, "_sync_session", _session)
    monkeypatch.setattr(gb, "_lock_owned_entry_job", lambda _db, _job_id: (job, None))
    monkeypatch.setattr(
        gb,
        "_run_slide_post_job",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("slide render must not run")
        ),
    )

    with pytest.raises(gb.SlidePostPolicyError) as exc_info:
        gb._run_generative_job_impl(str(job.id))
    assert exc_info.value.reason == "renderer_version_mismatch"


def test_slide_intent_flag_disabled_fails_closed(monkeypatch) -> None:
    job = _slides_job()
    monkeypatch.setattr(gb, "_sync_session", _session)
    monkeypatch.setattr(gb, "_lock_owned_entry_job", lambda _db, _job_id: (job, None))
    monkeypatch.setattr(gb.settings, "slide_posts_enabled", False)
    monkeypatch.setattr(
        gb,
        "_run_slide_post_job",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("slide render must not run")
        ),
    )

    with pytest.raises(gb.SlidePostPolicyError) as exc_info:
        gb._run_generative_job_impl(str(job.id))
    assert exc_info.value.reason == "flag_disabled"


def test_slide_intent_montage_default_untouched_when_flag_absent() -> None:
    """Sanity: an ordinary montage job's edit_format is unaffected — the new
    fence only fires when render_intent_value == 'slides'."""
    from app.agents._schemas.edit_format import coerce_edit_format

    assert coerce_edit_format("montage") == "montage"
    assert coerce_edit_format(None) == "montage"
