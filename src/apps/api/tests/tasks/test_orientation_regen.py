from __future__ import annotations

import types

import pytest

import app.tasks.generative_build as gb
from app.pipeline.canvas import LANDSCAPE, PORTRAIT


class _Meta:
    def __init__(self, clip_id: str):
        self.clip_id = clip_id
        self.hook_score = 5.0
        self.best_moments = []
        self.transcript = ""
        self.detected_subject = ""
        self.hook_text = ""


def test_regen_orientation_sticky_inherits_existing_variant() -> None:
    assert gb._resolve_variant_orientation({"orientation": "landscape"}) == "landscape"
    assert gb._resolve_variant_orientation({"orientation": "landscape"}, None) == "landscape"
    assert gb._resolve_variant_orientation({"orientation": "landscape"}, "portrait") == "portrait"
    assert gb._resolve_variant_orientation({}, None) == "portrait"
    assert gb._resolve_variant_orientation({"orientation": "square"}, None) == "portrait"


def test_orientation_change_is_not_fast_reburn_eligible(monkeypatch) -> None:
    monkeypatch.setattr(gb.settings, "GENERATIVE_FAST_REBURN_ENABLED", True, raising=False)
    existing = {
        "orientation": "landscape",
        "base_video_path": "generative-jobs/j/base.mp4",
        "text_mode": "agent_text",
    }

    assert (
        gb._is_fast_reburn_eligible(
            existing,
            new_track_id=None,
            mix_override=None,
            settings=gb.settings,
            orientation_override="landscape",
        )
        is True
    )
    assert (
        gb._is_fast_reburn_eligible(
            existing,
            new_track_id=None,
            mix_override=None,
            settings=gb.settings,
            orientation_override="portrait",
        )
        is False
    )


def test_render_variant_persists_portrait_orientation_by_default(monkeypatch, tmp_path) -> None:
    _patch_landscape_render_helpers(monkeypatch, assembled_canvases=[], validations=[])
    vdir = tmp_path / "v3"
    vdir.mkdir()

    result = gb._render_generative_variant(
        job_id="j",
        rank=3,
        spec={"variant_id": "original_text", "rank": 3, "text_mode": "none", "track": None},
        clip_metas=[_Meta("c1")],
        clip_id_to_local={"c1": "/x.mp4"},
        clip_id_to_gcs={"c1": "users/u/plan/i/x.mp4"},
        probe_map={},
        available_footage_s=12.0,
        agent_text=None,
        agent_form={},
        variant_dir=str(vdir),
    )

    assert result["ok"] is True
    assert result["orientation"] == "portrait"


def test_landscape_canvas_threads_to_assembly_and_validator(monkeypatch, tmp_path) -> None:
    assembled_canvases: list[object] = []
    validations: list[tuple[int, int] | None] = []
    _patch_landscape_render_helpers(monkeypatch, assembled_canvases, validations)
    vdir = tmp_path / "v3"
    vdir.mkdir()

    result = gb._render_generative_variant(
        job_id="j",
        rank=3,
        spec={"variant_id": "original_text", "rank": 3, "text_mode": "none", "track": None},
        clip_metas=[_Meta("c1")],
        clip_id_to_local={"c1": "/x.mp4"},
        clip_id_to_gcs={"c1": "users/u/plan/i/x.mp4"},
        probe_map={},
        available_footage_s=12.0,
        agent_text=None,
        agent_form={},
        variant_dir=str(vdir),
        orientation="landscape",
    )

    assert result["ok"] is True
    assert result["orientation"] == "landscape"
    assert assembled_canvases == [LANDSCAPE]
    assert validations == [(1920, 1080)]


def _patch_landscape_render_helpers(
    monkeypatch: pytest.MonkeyPatch,
    assembled_canvases: list[object],
    validations: list[tuple[int, int] | None],
) -> None:
    import app.pipeline.agents.gemini_analyzer as ga
    import app.pipeline.template_matcher as tm
    import app.pipeline.validator as validator
    import app.storage as storage
    import app.tasks.template_orchestrate as to

    monkeypatch.setattr(
        ga,
        "build_recipe",
        lambda d: types.SimpleNamespace(
            beat_timestamps_s=d.get("beat_timestamps_s", []),
            color_grade="none",
        ),
        raising=False,
    )
    monkeypatch.setattr(tm, "consolidate_slots", lambda recipe, metas: recipe, raising=False)
    monkeypatch.setattr(
        tm,
        "match",
        lambda recipe, metas, **kw: types.SimpleNamespace(steps=[]),
        raising=False,
    )
    monkeypatch.setattr(to, "_enrich_slots_with_energy", lambda slots, beats: slots, raising=False)

    def _fake_assemble(_steps, _c2l, _probe, out_path, _tmpdir, **kw):
        assembled_canvases.append(kw.get("canvas") or PORTRAIT)
        with open(out_path, "wb") as f:
            f.write(b"\x00" * 16)

    monkeypatch.setattr(to, "_assemble_clips", _fake_assemble, raising=False)
    monkeypatch.setattr(
        storage,
        "upload_public_read",
        lambda _local, gcs: f"https://signed/{gcs}",
        raising=False,
    )

    def _fake_validate(_path, *, expected_resolution=None):
        validations.append(expected_resolution)
        return types.SimpleNamespace(passed=True, errors=[])

    monkeypatch.setattr(validator, "validate_output", _fake_validate, raising=False)
