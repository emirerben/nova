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
