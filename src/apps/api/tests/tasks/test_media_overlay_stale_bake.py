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
    monkeypatch.setattr("app.storage.signed_get_url", lambda path, **kw: "gs://bucket/signed")
    monkeypatch.setattr("app.services.pipeline_trace.record_pipeline_event", lambda *a, **k: None)
    monkeypatch.setattr("sqlalchemy.orm.attributes.flag_modified", lambda obj, key: None)


def _run(job):
    gb._run_media_overlay_pass(
        job_id="00000000-0000-0000-0000-000000000001",
        variant_id="v1",
        overlays_raw=[_card()],
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


def test_autosave_during_bake_survives_write_back(monkeypatch):
    """The exact E5 race: toggle to fullscreen persisted mid-bake → not clobbered."""
    variant = _variant([_card()])
    job = _FakeJob(variant)
    toggled = _card(display_mode="fullscreen", scale=1.0)

    def _mutate():
        job.assembly_plan["variants"][0]["media_overlays"] = [toggled]

    _patch_common(monkeypatch, job, mutate_during_apply=_mutate)
    _run(job)
    v = job.assembly_plan["variants"][0]
    # The user's toggle wins; the bake still publishes its video + terminal state.
    assert v["media_overlays"] == [toggled]
    assert v["render_status"] == "ready"
    assert v["output_url"] == "gs://bucket/v1.mp4?sig=overlaid"
    assert v["pre_media_overlay_video_path"]
