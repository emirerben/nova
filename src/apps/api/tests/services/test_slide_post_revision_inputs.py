"""Complete pinned input and immutable media identity controls (no rendering)."""

import pytest


def test_same_path_new_generation_has_distinct_fallback_identity():
    from app.services.slide_post_render_revisions import source_identity

    assert source_identity({"gcs_path": "same", "gcs_generation": "v2"}) != source_identity(
        {"gcs_path": "same", "gcs_generation": "v3"}
    )
    assert source_identity({"content_fingerprint": "trusted", "gcs_generation": "v2"}) == "trusted"


def test_generation_scoped_outputs_do_not_alias():
    from app.services.slide_post_render_revisions import artifact_prefix

    assert artifact_prefix("job", "v2") != artifact_prefix("job", "v3")
    assert artifact_prefix("job", None) == "generative-jobs/job/slides"


def test_pinned_sticker_download_failure_is_not_a_complete_result(monkeypatch, tmp_path):
    from app.tasks.generative_build import _download_slide_sticker_sources

    def unavailable(*_args, **_kwargs):
        raise RuntimeError("pinned generation unavailable")

    monkeypatch.setattr("app.storage.download_generation_to_file", unavailable)
    with pytest.raises(RuntimeError, match="pinned generation unavailable"):
        _download_slide_sticker_sources(
            {"sticker": {"gcs_path": "owned/sticker", "gcs_generation": "2"}},
            tmpdir=str(tmp_path),
            slide_index=0,
            strict=True,
        )


def test_keyed_first_input_failure_cannot_stage_a_partial_result(monkeypatch):
    import uuid
    from types import SimpleNamespace

    from app.services import slide_post_generation_delivery as delivery
    from app.tasks import generative_build

    job_id = uuid.uuid4()

    class Session:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    monkeypatch.setattr(generative_build, "_sync_session", Session)
    draft = SimpleNamespace(
        platform_profile="tiktok_photo", cover_index=0, caption="pinned", version=1, aspect=None
    )
    monkeypatch.setattr(
        generative_build,
        "_resolve_slide_post_draft_and_assets",
        lambda *_a, **_kw: (uuid.uuid4(), uuid.uuid4(), draft, [{}]),
    )

    def unavailable(_job, **kwargs):
        assert kwargs["strict_inputs"] is True
        assert kwargs["artifact_generation"] == str(job_id)
        raise RuntimeError("pinned sticker unavailable")

    monkeypatch.setattr(generative_build, "_build_slide_post_result", unavailable)
    monkeypatch.setattr(delivery, "stage_result", lambda *_: pytest.fail("partial result staged"))
    token = delivery.active_job.set(job_id)
    try:
        with pytest.raises(RuntimeError, match="pinned sticker unavailable"):
            generative_build._run_slide_post_job(str(job_id), render_trace_id="test")
    finally:
        delivery.active_job.reset(token)


def test_complete_pinned_derivatives_do_not_reuse_legacy_partial_sticker_cache():
    from app.services.slide_post_render_revisions import normalized_prefix

    assert normalized_prefix("job", strict=True) != normalized_prefix("job", strict=False)
    assert normalized_prefix("job", strict=False) == "generative-jobs/job/slides/normalized"
