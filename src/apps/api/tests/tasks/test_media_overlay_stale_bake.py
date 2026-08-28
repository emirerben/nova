"""Plan 009 E5: stale-bake detection in _run_media_overlay_pass.

The bake runs for minutes; render:false autosaves land meanwhile (e.g. a
PiP↔fullscreen toggle). The write-back must re-read fresh and NEVER clobber a
list that changed since the task started — the user's metadata wins; the video
carries this bake's cards until the next Download.
"""

from __future__ import annotations

import app.tasks.generative_build as gb


def _card(**over):
    c = {
        "id": "ov1",
        "kind": "image",
        "src_gcs_path": "users/u1/plan/i1/overlays/card.png",
        "position": "center",
        "x_frac": 0.5,
        "y_frac": 0.5,
        "scale": 0.35,
        "display_mode": "pip",
        "start_s": 0.0,
        "end_s": 3.0,
        "z": 0,
    }
    c.update(over)
    return c


class _FakeJob:
    def __init__(self, variant):
        self.assembly_plan = {"variants": [variant]}
        self.status = "variants_ready"
        self.mode = "generative"


def _variant(media_overlays):
    return {
        "variant_id": "v1",
        "video_path": "gs://bucket/v1.mp4",
        "output_url": "gs://bucket/v1.mp4?sig=old",
        "render_status": "rendering",
        "media_overlays": media_overlays,
        "pre_media_overlay_video_path": "gs://bucket/v1.mp4_pre_overlay",
        "sound_effects": None,
        "pre_sfx_video_path": None,
        "media_overlays_render_dirty": True,
    }


def _patch_common(monkeypatch, job, *, mutate_during_apply=None):
    class _Sess:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def get(self, model, pk, **kw):
            return job

        def expire_all(self):
            pass

        def commit(self):
            pass

    def _apply(**kw):
        if mutate_during_apply is not None:
            mutate_during_apply()
        return "gs://bucket/v1.mp4?sig=overlaid"

    monkeypatch.setattr(gb, "_sync_session", lambda: _Sess())
    monkeypatch.setattr(gb.settings, "sound_effects_enabled", False, raising=False)
    monkeypatch.setattr("app.pipeline.media_overlay.apply_media_overlays", _apply)
    monkeypatch.setattr("app.storage.copy_object", lambda src, dst: None)
    monkeypatch.setattr("app.storage.object_exists", lambda _path: False)
    monkeypatch.setattr("app.storage.signed_get_url", lambda path, **kw: "gs://bucket/signed")
    monkeypatch.setattr(
        gb,
        "generate_and_upload_from_gcs",
        lambda path, **kw: f"{path}.poster.jpg",
    )
    monkeypatch.setattr("app.services.pipeline_trace.record_pipeline_event", lambda *a, **k: None)
    monkeypatch.setattr("sqlalchemy.orm.attributes.flag_modified", lambda obj, key: None)


def _run(job, *, overlays_raw=None, render_gen_id=None):
    gb._run_media_overlay_pass(
        job_id="00000000-0000-0000-0000-000000000001",
        variant_id="v1",
        overlays_raw=[_card()] if overlays_raw is None else overlays_raw,
        expected_render_gen_id=render_gen_id,
    )


def test_unchanged_list_is_written_back(monkeypatch):
    variant = _variant([_card()])
    job = _FakeJob(variant)
    _patch_common(monkeypatch, job)
    _run(job)
    v = job.assembly_plan["variants"][0]
    # Coercion materialises defaulted keys — assert the load-bearing bits.
    assert v["media_overlays"][0]["id"] == "ov1"
    assert v["media_overlays"][0]["display_mode"] == "pip"
    assert v["render_status"] == "ready"
    assert v["output_url"] == "gs://bucket/v1.mp4?sig=overlaid"
    assert v["poster_path"] == "gs://bucket/v1.mp4.poster.jpg"
    assert v["pre_overlay_poster_path"] == ("gs://bucket/v1.mp4_pre_overlay.poster.jpg")
    assert v["media_overlays_render_dirty"] is False


def test_autosave_during_bake_survives_write_back(monkeypatch):
    """The exact E5 race: toggle to fullscreen persisted mid-bake → not clobbered."""
    variant = _variant([_card()])
    job = _FakeJob(variant)
    toggled = _card(display_mode="fullscreen", scale=1.0)

    def _mutate():
        job.assembly_plan["variants"][0]["media_overlays"] = [toggled]
        job.assembly_plan["variants"][0]["media_overlays_render_dirty"] = True

    _patch_common(monkeypatch, job, mutate_during_apply=_mutate)
    _run(job)
    v = job.assembly_plan["variants"][0]
    # The user's toggle wins; the bake still publishes its video + terminal state.
    assert v["media_overlays"] == [toggled]
    assert v["render_status"] == "ready"
    assert v["output_url"] == "gs://bucket/v1.mp4?sig=overlaid"
    assert v["pre_media_overlay_video_path"]
    assert v["media_overlays_render_dirty"] is True


def test_clear_only_clears_dirty_when_desired_state_is_unchanged(monkeypatch):
    variant = _variant(None)
    job = _FakeJob(variant)
    _patch_common(monkeypatch, job)

    _run(job, overlays_raw=[])

    v = job.assembly_plan["variants"][0]
    assert v["media_overlays"] is None
    assert v["media_overlays_render_dirty"] is False


def test_stale_generation_leaves_dirty_state_untouched(monkeypatch):
    variant = _variant([_card()])
    variant["render_generation_id"] = "winner"
    job = _FakeJob(variant)
    _patch_common(monkeypatch, job)

    _run(job, render_gen_id="stale")

    v = job.assembly_plan["variants"][0]
    assert v["media_overlays_render_dirty"] is True
    assert v["render_generation_id"] == "winner"


def test_stale_generation_cleans_unreferenced_generated_posters(monkeypatch):
    variant = _variant([_card()])
    variant["render_generation_id"] = "winner"
    job = _FakeJob(variant)
    _patch_common(monkeypatch, job)
    deleted: list[str] = []
    monkeypatch.setattr(
        gb,
        "_delete_generated_poster_objects_if_unreferenced",
        lambda _job_id, paths, **_kwargs: deleted.extend(paths),
    )

    _run(job, render_gen_id="stale")

    assert deleted == [
        "gs://bucket/v1.mp4.poster.jpg",
        "gs://bucket/v1.mp4_pre_overlay.poster.jpg",
    ]


def test_in_place_overlay_swap_retires_uuid_backfill_poster(monkeypatch):
    variant = _variant([_card()])
    old_poster = (
        "generative-jobs/00000000-0000-0000-0000-000000000001/v1.mp4"
        ".poster.backfill-11111111-1111-4111-8111-111111111111.jpg"
    )
    variant["poster_path"] = old_poster
    job = _FakeJob(variant)
    _patch_common(monkeypatch, job)
    journaled: list[str] = []
    monkeypatch.setattr(
        gb,
        "_reconcile_retired_variant_posters",
        lambda _job_id, paths: journaled.extend(paths),
    )

    _run(job)

    assert job.assembly_plan["variants"][0]["video_path"] == variant["video_path"]
    assert job.assembly_plan["variants"][0]["poster_path"] != old_poster
    assert old_poster in journaled
    assert job.assembly_plan[gb.VIDEO_POSTER_BACKFILL_CLEANUP_FIELD] == [
        {
            "old_path": old_poster,
            "replacement_path": job.assembly_plan["variants"][0]["poster_path"],
        }
    ]
