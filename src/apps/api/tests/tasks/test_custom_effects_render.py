"""apply_custom_effect execution task (PR6, effect-language train).

House style: no DB, no real ffmpeg/GCS — fake Job via `_sync_session`
monkeypatch, writes captured through `_update_variant_entry` (mirrors
test_render_generation_guard.py).

The single most important behavior under test: `_run_apply_custom_effect`
NEVER trusts the `effect` dict it receives — a spec that was valid when the
chat turn parsed it, or that a client/attacker tampered with between dispatch
and worker pickup, is re-validated from scratch at execution time via
`validate_effect_spec`. A rejected spec must never reach FFmpeg or touch the
variant's video.
"""

from __future__ import annotations

import contextlib
import types

import pytest

import app.tasks.custom_effects_render as cer
import app.tasks.generative_build as gb

JOB_ID = "12345678-1234-5678-1234-567812345678"

VALID_EFFECT = {
    "id": "vintage_1",
    "label": "Vintage film",
    "filters": [{"name": "curves", "params": {"preset": "vintage"}}],
    "start_s": 0.0,
    "end_s": 5.0,
    "target": "full_frame",
}

# Tampered: "drawtext" is not in ALLOWED_FILTERS — this is the shape a stored
# spec could take if something between dispatch and worker pickup mutated it
# (or a hand-crafted PATCH body reached the task directly).
TAMPERED_EFFECT = {**VALID_EFFECT, "filters": [{"name": "drawtext", "params": {}}]}


class _FakeJob:
    def __init__(self, variants: list[dict]) -> None:
        self.assembly_plan = {"variants": list(variants)}


def _variant(**overrides) -> dict:
    v = {
        "variant_id": "original_text",
        "rank": 3,
        "resolved_archetype": "montage",
        "render_status": "rendering",
        "video_path": f"generative-jobs/{JOB_ID}/variant_3_original_text.mp4",
        "base_video_path": f"generative-jobs/{JOB_ID}/base_3.mp4",
        "duration_s": 8.0,
    }
    v.update(overrides)
    return v


def _patch_session(monkeypatch: pytest.MonkeyPatch, job: _FakeJob) -> None:
    class _Sess:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def get(self, model, pk, **kw):
            return job if model is gb.Job else None

        def commit(self):
            pass

    monkeypatch.setattr(cer, "_sync_session", lambda: _Sess())


def _capture_updates(monkeypatch: pytest.MonkeyPatch, job: _FakeJob) -> list[dict]:
    updates: list[dict] = []

    def _fake_update(jid, vid, patch, *, expected_render_gen_id=None, **_kwargs):
        match = next(
            (
                (index, variant)
                for index, variant in enumerate(job.assembly_plan["variants"])
                if variant.get("variant_id") == vid
            ),
            None,
        )
        if match is None:
            return False
        index, variant = match
        update = {k: v for k, v in patch.items() if k != "variant_id"}
        job.assembly_plan["variants"][index] = {**variant, **update}
        updates.append(dict(patch))
        return True

    monkeypatch.setattr(gb, "_update_variant_entry", _fake_update, raising=False)
    return updates


def _stub_render_io(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """No-op every I/O call `_run_apply_custom_effect` can make once a spec
    passes validation, so a test can assert whether they ran at all."""
    calls: list[str] = []

    monkeypatch.setattr("app.storage.download_to_file", lambda *a, **kw: calls.append("download"))
    monkeypatch.setattr(
        "app.storage.upload_public_read",
        lambda *a, **kw: (calls.append("upload"), "https://signed/effected.mp4")[1],
    )
    monkeypatch.setattr(
        "app.storage.delete_object_best_effort", lambda *a, **kw: calls.append("delete")
    )
    monkeypatch.setattr(
        "app.pipeline.probe.probe_video",
        lambda *a, **kw: types.SimpleNamespace(duration_s=10.0),
    )
    monkeypatch.setattr(cer, "_run_ffmpeg_effect", lambda cmd: calls.append("ffmpeg"))
    return calls


def test_tampered_spec_rejected_at_execution_time_never_touches_render(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job = _FakeJob([_variant()])
    _patch_session(monkeypatch, job)
    updates = _capture_updates(monkeypatch, job)
    calls = _stub_render_io(monkeypatch)

    terminal_state = {"accepted": False}
    cer._run_apply_custom_effect(JOB_ID, "original_text", TAMPERED_EFFECT, None, terminal_state)

    # Rejected before render_status ever flips to "rendering" — no ffmpeg,
    # no download/upload, nothing touched.
    assert calls == []
    assert terminal_state["accepted"] is False
    assert len(updates) == 1
    assert updates[0]["render_status"] == "ready"
    assert "invalid effect" in updates[0]["render_error"]
    # The variant's real video is untouched.
    assert job.assembly_plan["variants"][0]["video_path"] == _variant()["video_path"]


def test_valid_spec_burns_and_persists_custom_effects(monkeypatch: pytest.MonkeyPatch) -> None:
    job = _FakeJob([_variant()])
    _patch_session(monkeypatch, job)
    _capture_updates(monkeypatch, job)
    calls = _stub_render_io(monkeypatch)

    terminal_state = {"accepted": False}
    cer._run_apply_custom_effect(JOB_ID, "original_text", VALID_EFFECT, None, terminal_state)

    assert calls == ["download", "ffmpeg", "upload", "delete"]
    assert terminal_state["accepted"] is True
    final = job.assembly_plan["variants"][0]
    assert final["render_status"] == "ready"
    assert final["output_url"] == "https://signed/effected.mp4"
    assert final["custom_effects"] == [
        {
            "id": "vintage_1",
            "label": "Vintage film",
            "filters": [{"name": "curves", "params": {"preset": "vintage"}}],
            "start_s": 0.0,
            "end_s": 5.0,
            "target": "full_frame",
        }
    ]
    # A second application replaces, never stacks (v1 single active effect).
    assert len(final["custom_effects"]) == 1


def test_valid_spec_replaces_not_stacks_prior_custom_effect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prior = {**VALID_EFFECT, "id": "prior_look"}
    job = _FakeJob([_variant(custom_effects=[prior])])
    _patch_session(monkeypatch, job)
    _capture_updates(monkeypatch, job)
    _stub_render_io(monkeypatch)

    terminal_state = {"accepted": False}
    cer._run_apply_custom_effect(JOB_ID, "original_text", VALID_EFFECT, None, terminal_state)

    final = job.assembly_plan["variants"][0]
    assert len(final["custom_effects"]) == 1
    assert final["custom_effects"][0]["id"] == "vintage_1"


def test_no_source_video_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    job = _FakeJob([_variant(video_path=None, base_video_path=None)])
    _patch_session(monkeypatch, job)
    _capture_updates(monkeypatch, job)
    _stub_render_io(monkeypatch)

    terminal_state = {"accepted": False}
    with pytest.raises(ValueError, match="no source video"):
        cer._run_apply_custom_effect(JOB_ID, "original_text", VALID_EFFECT, None, terminal_state)


def test_apply_custom_effect_reapplies_persisted_sfx_lane(monkeypatch: pytest.MonkeyPatch) -> None:
    """Symmetric direction: apply_custom_effect itself rebuilds from
    base_video_path, so a persisted SFX lane must be reapplied on top of the
    effected output — same as every caption/camera-effect reburn task.
    """
    job = _FakeJob([_variant(sound_effects=[{"effect_id": "pop", "at_s": 1.0}])])
    _patch_session(monkeypatch, job)
    _capture_updates(monkeypatch, job)
    _stub_render_io(monkeypatch)
    monkeypatch.setattr(gb, "_will_reapply_media_layers", lambda _v: True)
    reapply_calls: list[dict] = []

    def _fake_reapply(**kwargs):
        reapply_calls.append(kwargs)
        return True

    monkeypatch.setattr(gb, "_reapply_user_media_layers", _fake_reapply)

    terminal_state = {"accepted": False}
    cer._run_apply_custom_effect(JOB_ID, "original_text", VALID_EFFECT, None, terminal_state)

    assert len(reapply_calls) == 1
    assert reapply_calls[0]["job_id"] == JOB_ID
    assert reapply_calls[0]["variant_id"] == "original_text"
    # OV-7: render_status stays "rendering" — the reapply chain owns ready/failed.
    final = job.assembly_plan["variants"][0]
    assert final["render_status"] == "rendering"


# ── reapply_persisted_custom_effect: REAPPLY-ON-REBURN for the caption/
# camera-effect reburn family ─────────────────────────────────────────────
#
# House style mirrors test_render_generation_guard.py's caption-camera fixture:
# fake Job via a gb-scoped `_sync_session` monkeypatch, writes captured through
# `_update_variant_entry`, no real ffmpeg/GCS/DB.


class _FakeGbJob:
    def __init__(self, variants: list[dict]) -> None:
        self.assembly_plan = {"variants": list(variants)}


def _patch_gb_session(monkeypatch: pytest.MonkeyPatch, job: _FakeGbJob) -> None:
    class _Sess:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def get(self, model, pk, **kw):
            return job if model is gb.Job else None

        def commit(self):
            pass

    monkeypatch.setattr(gb, "_sync_session", lambda: _Sess())


def _capture_gb_updates(monkeypatch: pytest.MonkeyPatch, job: _FakeGbJob) -> list[dict]:
    updates: list[dict] = []

    def _fake_update(jid, vid, patch, *, expected_render_gen_id=None, **_kwargs):
        match = next(
            (
                (index, variant)
                for index, variant in enumerate(job.assembly_plan["variants"])
                if variant.get("variant_id") == vid
            ),
            None,
        )
        if match is None:
            return False
        index, variant = match
        update = {k: v for k, v in patch.items() if k != "variant_id"}
        job.assembly_plan["variants"][index] = {**variant, **update}
        updates.append(dict(patch))
        return True

    monkeypatch.setattr(gb, "_update_variant_entry", _fake_update, raising=False)
    return updates


def _camera_reburn_variant(**overrides) -> dict:
    v = {
        "variant_id": "subtitled",
        "rank": 1,
        "resolved_archetype": "subtitled",
        "render_status": "rendering",
        "base_video_path": f"generative-jobs/{JOB_ID}/base.mp4",
        "video_path": f"generative-jobs/{JOB_ID}/current.mp4",
        "camera_effects": None,
        "overlay_camera_rebuild_pending": False,
        "media_overlays_render_dirty": False,
    }
    v.update(overrides)
    return v


def _arm_camera_reburn(
    monkeypatch: pytest.MonkeyPatch, job: _FakeGbJob, ffmpeg_cmds: list[list[str]]
) -> None:

    _patch_gb_session(monkeypatch, job)
    _capture_gb_updates(monkeypatch, job)
    monkeypatch.setattr("app.storage.download_to_file", lambda *a, **k: None)
    monkeypatch.setattr(
        "app.storage.upload_public_read", lambda _local, _gcs: "https://signed/camera"
    )
    monkeypatch.setattr(
        "app.pipeline.probe.probe_video",
        lambda _path: types.SimpleNamespace(duration_s=5.0, has_audio=True),
    )
    monkeypatch.setattr(gb, "_should_compose_subtitled_final", lambda _v: False)
    monkeypatch.setattr(gb, "_burn_persisted_captions_onto_base", lambda *a, **k: None)
    monkeypatch.setattr(gb, "_rendered_duration_s", lambda _path: 5.0)
    monkeypatch.setattr(gb, "_will_reapply_media_layers", lambda _v: False)
    monkeypatch.setattr(gb, "_free_retired_visual_blocks_base", lambda *a, **k: None)
    monkeypatch.setattr(gb, "_free_retired_media_snapshots", lambda *a, **k: None)
    monkeypatch.setattr(
        "app.services.pipeline_trace.pipeline_trace_for",
        lambda _job_id: contextlib.nullcontext(),
    )
    # The ffmpeg burn itself — capture the command instead of running it, so
    # (a) verifies the filter chain landed and (b) verifies it never gets called.
    monkeypatch.setattr(cer, "_run_ffmpeg_effect", lambda cmd: ffmpeg_cmds.append(cmd))


def test_caption_camera_rerender_reapplies_persisted_custom_effect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """(a) A caption-reburn task (camera-effect-rerender) on a variant with a
    persisted custom effect re-applies it — the effect's filter chain lands
    in the ffmpeg command, burned onto the base BEFORE the caption compose
    step."""
    job = _FakeGbJob([_camera_reburn_variant(custom_effects=[VALID_EFFECT])])
    ffmpeg_cmds: list[list[str]] = []
    _arm_camera_reburn(monkeypatch, job, ffmpeg_cmds)
    trace_events: list[tuple] = []
    monkeypatch.setattr(
        "app.services.pipeline_trace.record_pipeline_event",
        lambda stage, event, data=None: trace_events.append((stage, event, data)),
    )

    terminal = {"accepted": False}
    gb._run_rerender_caption_camera_effects(
        JOB_ID, "subtitled", render_gen_id=None, terminal_state=terminal
    )

    assert terminal["accepted"] is True
    assert len(ffmpeg_cmds) == 1
    filter_chain = ffmpeg_cmds[0][ffmpeg_cmds[0].index("-vf") + 1]
    assert "curves=" in filter_chain and "preset=vintage" in filter_chain
    assert trace_events == []  # no failure event on a successful reapply
    final = job.assembly_plan["variants"][0]
    # custom_effects is untouched (still the persisted, valid entry) — not cleared.
    assert final.get("custom_effects") == [VALID_EFFECT]


def test_caption_camera_rerender_tampered_effect_fails_open_and_clears(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """(b) A tampered/invalid persisted spec is re-validated, rejected, the
    reburn proceeds WITHOUT the effect (fail open — never breaks the caption
    reburn), a ("render", "custom_effect_reapply_failed") trace event is
    recorded, and the persisted entry is cleared so the UI stops claiming an
    effect that isn't on the video."""
    tampered = {**VALID_EFFECT, "filters": [{"name": "drawtext", "params": {}}]}
    job = _FakeGbJob([_camera_reburn_variant(custom_effects=[tampered])])
    ffmpeg_cmds: list[list[str]] = []
    _arm_camera_reburn(monkeypatch, job, ffmpeg_cmds)
    trace_events: list[tuple] = []
    monkeypatch.setattr(
        "app.services.pipeline_trace.record_pipeline_event",
        lambda stage, event, data=None: trace_events.append((stage, event, data)),
    )

    terminal = {"accepted": False}
    gb._run_rerender_caption_camera_effects(
        JOB_ID, "subtitled", render_gen_id=None, terminal_state=terminal
    )

    assert terminal["accepted"] is True  # the reburn itself still succeeds
    assert ffmpeg_cmds == []  # never reached FFmpeg with the tampered spec
    assert len(trace_events) == 1
    stage, event, data = trace_events[0]
    assert (stage, event) == ("render", "custom_effect_reapply_failed")
    assert data["reason"] == "filter_not_allowed"
    final = job.assembly_plan["variants"][0]
    assert final["custom_effects"] == []


def test_caption_camera_rerender_without_custom_effect_is_unaffected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """(c) A variant with no persisted custom effect reburns exactly as before
    this feature existed — no effect ffmpeg call, no `custom_effects` key
    written to the patch at all (pin)."""
    job = _FakeGbJob([_camera_reburn_variant()])  # no "custom_effects" key
    ffmpeg_cmds: list[list[str]] = []
    _arm_camera_reburn(monkeypatch, job, ffmpeg_cmds)
    updates = _capture_gb_updates(monkeypatch, job)  # re-capture to inspect below

    terminal = {"accepted": False}
    gb._run_rerender_caption_camera_effects(
        JOB_ID, "subtitled", render_gen_id=None, terminal_state=terminal
    )

    assert terminal["accepted"] is True
    assert ffmpeg_cmds == []
    assert "custom_effects" not in updates[-1]
    final = job.assembly_plan["variants"][0]
    assert "custom_effects" not in final
