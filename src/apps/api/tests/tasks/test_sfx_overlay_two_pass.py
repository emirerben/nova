"""Two-pass observability for the overlay → SFX-reapply sequence.

When a variant has persisted sound effects, an overlay-edit render runs TWO
FFmpeg passes: the overlay composite, then a terminal SFX re-mix on top
(`_reapply_persisted_sfx_if_any`). The bug this guards: the overlay pass used to
flip `render_status="ready"` BEFORE the SFX re-mix, so the frontend download
poll could grab the intermediate overlay-only file (missing its SFX), and a
failed re-mix was swallowed (variant stuck looking "ready" without SFX).

Fix (plan-eng-review T3): the overlay pass stays "rendering" until the SFX pass
sets the terminal state, and a failed re-mix surfaces as "failed".

R1-3 sibling: when the deferred SFX hook reports NO ownership (the lane was
cleared in the DB mid-bake), the overlay pass must finalize "ready" itself —
never strand the variant in "rendering".
"""

import pytest

import app.tasks.generative_build as gb

JOB_ID = "12345678-1234-5678-1234-567812345678"


def _placement(**over):
    p = {
        "id": "sfx1",
        "sound_effect_id": None,
        "src_gcs_path": "sound-effects/boom/audio.mp3",
        "at_s": 4.0,
        "gain": 1.0,
        "trim_start_s": None,
        "trim_end_s": None,
        "duration_s": 0.5,
        "label": "Boom",
    }
    p.update(over)
    return p


def _card(**over):
    c = {
        "id": "ov1",
        "kind": "image",
        "src_gcs_path": "users/u1/plan/i1/overlay/card.png",
        "position": "center",
        "scale": 0.35,
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


def _variant(*, sound_effects, pre_sfx=None):
    return {
        "variant_id": "v1",
        "video_path": "gs://bucket/v1.mp4",
        "output_url": "gs://bucket/v1.mp4?sig=old",
        "render_status": "ready",
        "render_finished_at": "2026-06-01T00:00:00Z",
        "media_overlays": None,
        "pre_media_overlay_video_path": "gs://bucket/v1.mp4_pre_overlay",
        "sound_effects": sound_effects,
        "pre_sfx_video_path": pre_sfx,
    }


def _patch_common(monkeypatch, job, *, sfx_apply, sound_effects_enabled=True):
    """Wire _sync_session, the FFmpeg apply fns, storage, and flag_modified."""

    class _Sess:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def get(self, model, pk, **kw):
            return job

        def expire_all(self):
            # Plan 009 E5: the overlay pass re-reads fresh under a row lock
            # before write-back; the fake session has one shared job object.
            pass

        def commit(self):
            pass

    monkeypatch.setattr(gb, "_sync_session", lambda: _Sess())
    monkeypatch.setattr(gb.settings, "sound_effects_enabled", sound_effects_enabled, raising=False)
    # Call-time-imported symbols are patched at their source modules.
    monkeypatch.setattr(
        "app.pipeline.media_overlay.apply_media_overlays",
        lambda **kw: "gs://bucket/v1.mp4?sig=overlaid",
    )
    monkeypatch.setattr("app.pipeline.sound_effects.apply_sound_effects", sfx_apply)
    monkeypatch.setattr("app.storage.copy_object", lambda src, dst: None)
    monkeypatch.setattr("app.storage.signed_get_url", lambda path, **kw: "gs://bucket/signed")
    monkeypatch.setattr("app.services.pipeline_trace.record_pipeline_event", lambda *a, **k: None)
    # FakeJob is not an ORM-mapped instance; neutralise the dirty-flag call.
    monkeypatch.setattr("sqlalchemy.orm.attributes.flag_modified", lambda obj, key: None)


def test_overlay_pass_stays_rendering_until_sfx_remix_finishes(monkeypatch):
    """The variant must NOT be 'ready' at the moment the SFX re-mix runs —
    otherwise the download poll could serve the overlay-only intermediate."""
    job = _FakeJob(_variant(sound_effects=[_placement()], pre_sfx="gs://bucket/v1.mp4_pre_sfx"))
    seen_status: dict = {}

    def _sfx_apply(**kw):
        seen_status["at_remix"] = job.assembly_plan["variants"][0]["render_status"]
        return "gs://bucket/v1.mp4?sig=sfx"

    _patch_common(monkeypatch, job, sfx_apply=_sfx_apply)

    gb._run_media_overlay_pass(job_id=JOB_ID, variant_id="v1", overlays_raw=[_card()])

    v = job.assembly_plan["variants"][0]
    # While the SFX re-mix ran, the variant was still "rendering" (deferred).
    assert seen_status["at_remix"] == "rendering"
    # The SFX pass set the single terminal "ready".
    assert v["render_status"] == "ready"
    assert v["render_finished_at"] != "2026-06-01T00:00:00Z"


def test_failed_sfx_remix_surfaces_as_failed_not_stuck_rendering(monkeypatch):
    """A failed post-overlay SFX re-mix must surface as 'failed', never leave
    the variant stranded in 'rendering' or falsely 'ready' without SFX."""
    job = _FakeJob(_variant(sound_effects=[_placement()], pre_sfx="gs://bucket/v1.mp4_pre_sfx"))

    def _sfx_boom(**kw):
        raise RuntimeError("ffmpeg sfx mix failed")

    _patch_common(monkeypatch, job, sfx_apply=_sfx_boom)

    gb._run_media_overlay_pass(job_id=JOB_ID, variant_id="v1", overlays_raw=[_card()])

    v = job.assembly_plan["variants"][0]
    assert v["render_status"] == "failed"


def test_overlay_pass_is_terminal_when_no_sfx_persisted(monkeypatch):
    """No persisted SFX → no re-mix → the overlay pass owns the terminal state
    (byte-unchanged from before the two-pass fix)."""
    job = _FakeJob(_variant(sound_effects=None))
    calls: list = []

    def _sfx_apply(**kw):
        calls.append(kw)
        return "gs://bucket/v1.mp4?sig=sfx"

    _patch_common(monkeypatch, job, sfx_apply=_sfx_apply)

    gb._run_media_overlay_pass(job_id=JOB_ID, variant_id="v1", overlays_raw=[_card()])

    v = job.assembly_plan["variants"][0]
    assert v["render_status"] == "ready"
    assert v["render_finished_at"] != "2026-06-01T00:00:00Z"
    assert calls == []  # SFX re-mix never ran


def test_flag_off_keeps_overlay_pass_terminal(monkeypatch):
    """SOUND_EFFECTS_ENABLED=False → reapply is a no-op even with persisted SFX;
    overlay pass stays terminal (byte-identical to pre-feature)."""
    job = _FakeJob(_variant(sound_effects=[_placement()], pre_sfx="gs://bucket/v1.mp4_pre_sfx"))
    calls: list = []

    _patch_common(
        monkeypatch,
        job,
        sfx_apply=lambda **kw: calls.append(kw) or "gs://x",
        sound_effects_enabled=False,
    )

    gb._run_media_overlay_pass(job_id=JOB_ID, variant_id="v1", overlays_raw=[_card()])

    v = job.assembly_plan["variants"][0]
    assert v["render_status"] == "ready"
    assert calls == []


@pytest.mark.parametrize("render_gen_id", [None, "tok-1"], ids=["tokenless", "tokened"])
def test_sfx_cleared_midbake_finalizes_ready_never_stranded_rendering(monkeypatch, render_gen_id):
    """Stranded-rendering lockout (R1-3 sibling of the caption terminals'
    lanes_cleared_midrun tests): SFX are persisted at pass start, so the overlay
    pass defers its terminal to the SFX hook (writes "rendering"). The lane is
    then cleared in the DB during the overlay bake (persist-only save), so
    `_reapply_persisted_sfx_if_any` no-ops and reports no ownership — the pass
    must write the token-gated finalize "ready", never leave "rendering"."""
    variant = _variant(sound_effects=[_placement()], pre_sfx="gs://bucket/v1.mp4_pre_sfx")
    if render_gen_id is not None:
        variant["render_generation_id"] = render_gen_id
    job = _FakeJob(variant)
    sfx_calls: list = []

    def _clear_sfx_midbake(**kw):
        # Replace (don't mutate) the entry — a DB-side clear never touches the
        # task's in-memory snapshot, so will_reapply_sfx stays True (deferred).
        plan = job.assembly_plan
        cleared = {**plan["variants"][0], "sound_effects": None}
        plan["variants"] = [cleared]
        return "gs://bucket/v1.mp4?sig=overlaid"

    _patch_common(monkeypatch, job, sfx_apply=lambda **kw: sfx_calls.append(kw) or "gs://x")
    monkeypatch.setattr("app.pipeline.media_overlay.apply_media_overlays", _clear_sfx_midbake)

    gb._run_media_overlay_pass(
        job_id=JOB_ID,
        variant_id="v1",
        overlays_raw=[_card()],
        expected_render_gen_id=render_gen_id,
    )

    v = job.assembly_plan["variants"][0]
    assert sfx_calls == []  # the hook no-oped: the lane was gone
    assert v["render_status"] == "ready"
    assert v["render_finished_at"] != "2026-06-01T00:00:00Z"
