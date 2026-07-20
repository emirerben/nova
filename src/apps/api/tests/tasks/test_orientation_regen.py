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


def test_explicit_orientation_is_never_fast_reburn_eligible(monkeypatch) -> None:
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
        is False
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


def test_persisted_landscape_request_rebuilds_from_source_clips(monkeypatch) -> None:
    """Regression: the route persists landscape before this worker starts.

    A cached portrait base must still be ignored when the persisted orientation
    already matches the override; otherwise the fast reburn stretches that base
    into a nominally 1920x1080 output.
    """
    job_id = "12345678-1234-5678-1234-567812345678"
    existing = {
        "variant_id": "original_text",
        "rank": 3,
        "orientation": "landscape",
        "text_mode": "agent_text",
        "intro_text": "hook",
        "base_video_path": f"generative-jobs/{job_id}/portrait-base.mp4",
        "music_track_id": None,
    }
    job = types.SimpleNamespace(
        all_candidates={
            "clip_paths": [f"generative-jobs/{job_id}/sources/clip.mp4"],
            "landscape_fit": "fit",
        },
        assembly_plan={"variants": [existing]},
    )

    class _Session:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def get(self, model, _pk, **_kwargs):
            return job if model is gb.Job else None

    monkeypatch.setattr(gb, "_sync_session", lambda: _Session())
    update_patches: list[dict] = []

    def _capture_update(_job_id, _variant_id, patch, **_kwargs):
        update_patches.append(patch)
        return True

    monkeypatch.setattr(gb, "_update_variant_entry", _capture_update)
    monkeypatch.setattr(gb, "_fresh_variant_snapshot", lambda *_a, **_kw: existing)
    monkeypatch.setattr(gb, "_resolve_narrative_order", lambda *_a, **_kw: None)
    monkeypatch.setattr(gb, "_reapply_user_media_layers", lambda **_kw: False)
    monkeypatch.setattr(gb, "_free_retired_visual_blocks_base", lambda *_a, **_kw: None)
    monkeypatch.setattr(
        gb,
        "_reburn_text_on_base",
        lambda **_kw: pytest.fail("orientation request reused the cached portrait base"),
    )

    ingest_calls: list[str] = []

    def _fake_ingest(_paths, _tmpdir, *, job_id: str):
        ingest_calls.append(job_id)
        meta = _Meta("c1")
        return {
            "clip_metas": [meta],
            "clip_id_to_local": {"c1": "/tmp/c1.mp4"},
            "clip_id_to_gcs": {"c1": "users/u/plan/i/c1.mp4"},
            "probe_map": {"/tmp/c1.mp4": types.SimpleNamespace(duration_s=6.0)},
            "hero": meta,
        }

    monkeypatch.setattr(gb, "_ingest_clips", _fake_ingest)
    render_kwargs: list[dict] = []

    def _fake_render(**kwargs):
        render_kwargs.append(kwargs)
        return {
            "ok": True,
            "variant_id": "original_text",
            "render_status": "ready",
            "video_path": f"generative-jobs/{job_id}/landscape.mp4",
            "output_url": "https://signed/landscape",
            "base_video_path": f"generative-jobs/{job_id}/landscape-base.mp4",
        }

    monkeypatch.setattr(gb, "_render_generative_variant", _fake_render)

    gb._run_regenerate_variant(
        job_id,
        "original_text",
        None,
        None,
        False,
        orientation_override="landscape",
        force_full_render=True,
    )

    assert ingest_calls == [job_id]
    assert len(render_kwargs) == 1
    assert render_kwargs[0]["orientation"] == "landscape"
    assert render_kwargs[0]["landscape_fit"] == "fit"
    assert update_patches[-1]["base_video_path"].endswith("/landscape-base.mp4")
    assert update_patches[-1]["base_video_path"] != existing["base_video_path"]


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
    assembled_fits: list[str] = []
    validations: list[tuple[int, int] | None] = []
    duration_ranges: list[tuple[float, float] | None] = []
    _patch_landscape_render_helpers(
        monkeypatch,
        assembled_canvases,
        validations,
        assembled_fits=assembled_fits,
        duration_ranges=duration_ranges,
    )
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
        landscape_fit="fit",
    )

    assert result["ok"] is True
    assert result["orientation"] == "landscape"
    assert assembled_canvases == [LANDSCAPE]
    assert assembled_fits == ["fill"]
    assert validations == [(1920, 1080)]
    assert duration_ranges == [(0.1, gb.settings.output_max_duration_s)]


def test_portrait_canvas_preserves_landscape_fit_preference(monkeypatch, tmp_path) -> None:
    assembled_canvases: list[object] = []
    assembled_fits: list[str] = []
    _patch_landscape_render_helpers(
        monkeypatch,
        assembled_canvases,
        validations=[],
        assembled_fits=assembled_fits,
    )
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
        orientation="portrait",
        landscape_fit="fit",
    )

    assert result["ok"] is True
    assert assembled_canvases == [PORTRAIT]
    assert assembled_fits == ["fit"]


def _patch_landscape_render_helpers(
    monkeypatch: pytest.MonkeyPatch,
    assembled_canvases: list[object],
    validations: list[tuple[int, int] | None],
    *,
    assembled_fits: list[str] | None = None,
    duration_ranges: list[tuple[float, float] | None] | None = None,
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
        if assembled_fits is not None:
            assembled_fits.append(kw.get("landscape_fit"))
        with open(out_path, "wb") as f:
            f.write(b"\x00" * 16)

    monkeypatch.setattr(to, "_assemble_clips", _fake_assemble, raising=False)
    monkeypatch.setattr(
        storage,
        "upload_public_read",
        lambda _local, gcs: f"https://signed/{gcs}",
        raising=False,
    )

    def _fake_validate(_path, *, expected_resolution=None, expected_duration_range=None):
        validations.append(expected_resolution)
        if duration_ranges is not None:
            duration_ranges.append(expected_duration_range)
        return types.SimpleNamespace(passed=True, errors=[])

    monkeypatch.setattr(validator, "validate_output", _fake_validate, raising=False)
