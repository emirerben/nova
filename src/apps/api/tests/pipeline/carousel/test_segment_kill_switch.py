"""Lane G: `carousel_effects_enabled` kill-switch wiring in generative_build.

Pins `_insert_carousel_moment_step` — the entire kill switch lives there: an
additive-only splice of a rendered Blossom-carousel moment into a montage's
`steps` list, called right before `_assemble_clips` (see the call site in
`_render_generative_variant`). No test in this file touches skia or ffmpeg for
real — `_maybe_render_carousel_moment`'s `render_carousel_moment` call is
monkeypatched at the `app.pipeline.carousel.segment` boundary (the function is
still a `NotImplementedError` stub as of Lane G; another agent fills it in).

- flag off -> zero-arg no-op, `_maybe_render_carousel_moment` never called,
  `steps` and the clip/probe maps come back untouched
- flag on, spec has no "carousel_moment" -> same no-op, same reason
- flag on, spec present, render returns None -> steps unchanged (the
  never-raise contract honored even when nothing actually raised)
- flag on, render raises unexpectedly -> `_maybe_render_carousel_moment`
  contains it and returns None (belt-and-braces around the contract)
- flag on, render succeeds -> synthetic clip registered in clip_id_to_local /
  clip_id_to_gcs / probe_map, exact_window AssemblyStep spliced at the
  requested position (intro/middle/outro/default)
- `_maybe_render_carousel_moment` builds `clip_paths` from `steps` in order,
  deduped, capped at 5
"""

from __future__ import annotations

import types
from typing import Any

import pytest

import app.tasks.generative_build as gb
from app.pipeline.agents.gemini_analyzer import AssemblyStep


def _step(clip_id: str) -> AssemblyStep:
    return AssemblyStep(slot={}, clip_id=clip_id, moment={"start_s": 0.0, "end_s": 1.0})


@pytest.fixture(autouse=True)
def _flag_off_by_default(monkeypatch):
    # Match the shipped default (False). Tests that need it on flip it
    # explicitly, so this file stays order-independent.
    monkeypatch.setattr(gb.settings, "carousel_effects_enabled", False, raising=False)


# ── _insert_carousel_moment_step: kill switch ───────────────────────────────


def test_flag_off_is_noop_never_renders(monkeypatch):
    def _boom(*args, **kwargs):
        raise AssertionError("_maybe_render_carousel_moment must not run when the flag is off")

    monkeypatch.setattr(gb, "_maybe_render_carousel_moment", _boom)

    steps = [_step("clip_1"), _step("clip_2")]
    spec = {"variant_id": "v1", "carousel_moment": {"effect": "scale_sweep"}}
    clip_id_to_local = {"clip_1": "/tmp/a.mp4", "clip_2": "/tmp/b.mp4"}
    clip_id_to_gcs = {"clip_1": "gs://a", "clip_2": "gs://b"}
    probe_map: dict[str, Any] = {}

    result = gb._insert_carousel_moment_step(
        steps,
        spec,
        clip_id_to_local=clip_id_to_local,
        clip_id_to_gcs=clip_id_to_gcs,
        probe_map=probe_map,
        variant_dir="/tmp/variant",
    )

    assert result is steps
    assert result == [_step("clip_1"), _step("clip_2")]
    assert clip_id_to_local == {"clip_1": "/tmp/a.mp4", "clip_2": "/tmp/b.mp4"}
    assert clip_id_to_gcs == {"clip_1": "gs://a", "clip_2": "gs://b"}
    assert probe_map == {}


def test_flag_on_spec_absent_is_noop(monkeypatch):
    monkeypatch.setattr(gb.settings, "carousel_effects_enabled", True, raising=False)

    def _boom(*args, **kwargs):
        raise AssertionError("must not render when the spec has no carousel_moment")

    monkeypatch.setattr(gb, "_maybe_render_carousel_moment", _boom)

    steps = [_step("clip_1")]
    spec = {"variant_id": "v2"}  # no "carousel_moment" key at all

    result = gb._insert_carousel_moment_step(
        steps,
        spec,
        clip_id_to_local={"clip_1": "/tmp/a.mp4"},
        clip_id_to_gcs={"clip_1": "gs://a"},
        probe_map={},
        variant_dir="/tmp/variant",
    )

    assert result is steps
    assert result == [_step("clip_1")]


def test_render_returns_none_leaves_steps_unchanged(monkeypatch):
    """End-to-end through the real `_maybe_render_carousel_moment`, with only
    `render_carousel_moment` itself monkeypatched at its source module."""
    monkeypatch.setattr(gb.settings, "carousel_effects_enabled", True, raising=False)

    import app.pipeline.carousel.segment as segment_mod

    monkeypatch.setattr(segment_mod, "render_carousel_moment", lambda spec, work_dir: None)

    steps = [_step("clip_1"), _step("clip_2")]
    spec = {"variant_id": "v3", "carousel_moment": {"effect": "cover_flow", "duration_s": 3.0}}
    clip_id_to_local = {"clip_1": "/tmp/a.mp4", "clip_2": "/tmp/b.mp4"}
    clip_id_to_gcs = {"clip_1": "gs://a", "clip_2": "gs://b"}
    probe_map: dict[str, Any] = {}

    result = gb._insert_carousel_moment_step(
        steps,
        spec,
        clip_id_to_local=clip_id_to_local,
        clip_id_to_gcs=clip_id_to_gcs,
        probe_map=probe_map,
        variant_dir="/tmp/variant",
    )

    assert result == [_step("clip_1"), _step("clip_2")]
    assert clip_id_to_local == {"clip_1": "/tmp/a.mp4", "clip_2": "/tmp/b.mp4"}
    assert clip_id_to_gcs == {"clip_1": "gs://a", "clip_2": "gs://b"}
    assert probe_map == {}


def test_render_success_splices_step_and_registers_synthetic_clip(monkeypatch):
    monkeypatch.setattr(gb.settings, "carousel_effects_enabled", True, raising=False)
    monkeypatch.setattr(
        gb, "_maybe_render_carousel_moment", lambda *a, **kw: "/tmp/variant/carousel_moment.mp4"
    )

    import app.pipeline.probe as probe_mod

    monkeypatch.setattr(
        probe_mod, "probe_video", lambda path: types.SimpleNamespace(duration_s=3.5)
    )

    steps = [_step("clip_1"), _step("clip_2"), _step("clip_3")]
    spec = {"variant_id": "v4", "carousel_moment": {"effect": "cards_stack", "position": "middle"}}
    clip_id_to_local = {"clip_1": "/tmp/a.mp4", "clip_2": "/tmp/b.mp4", "clip_3": "/tmp/c.mp4"}
    clip_id_to_gcs = {"clip_1": "gs://a", "clip_2": "gs://b", "clip_3": "gs://c"}
    probe_map: dict[str, Any] = {}

    result = gb._insert_carousel_moment_step(
        steps,
        spec,
        clip_id_to_local=clip_id_to_local,
        clip_id_to_gcs=clip_id_to_gcs,
        probe_map=probe_map,
        variant_dir="/tmp/variant",
    )

    synthetic_id = "__carousel_v4"
    assert len(result) == 4
    # "middle" of 3 pre-insertion steps == index 3 // 2 == 1
    assert result[1].clip_id == synthetic_id
    assert result[1].slot == {"exact_window": True}
    assert result[1].moment == {"start_s": 0.0, "end_s": 3.5}
    assert [s.clip_id for s in result] == ["clip_1", synthetic_id, "clip_2", "clip_3"]

    assert clip_id_to_local[synthetic_id] == "/tmp/variant/carousel_moment.mp4"
    assert clip_id_to_gcs[synthetic_id] == "/tmp/variant/carousel_moment.mp4"
    assert probe_map["/tmp/variant/carousel_moment.mp4"].duration_s == 3.5


@pytest.mark.parametrize(
    "position,expected_index",
    [
        ("intro", 0),
        ("middle", 1),
        ("outro", 2),
        (None, 0),  # absent -> default "intro"
        ("bogus", 0),  # unrecognized -> falls back to "intro"
    ],
)
def test_position_controls_splice_index(monkeypatch, position, expected_index):
    monkeypatch.setattr(gb.settings, "carousel_effects_enabled", True, raising=False)
    monkeypatch.setattr(gb, "_maybe_render_carousel_moment", lambda *a, **kw: "/tmp/moment.mp4")

    import app.pipeline.probe as probe_mod

    monkeypatch.setattr(
        probe_mod, "probe_video", lambda path: types.SimpleNamespace(duration_s=2.0)
    )

    moment_cfg: dict[str, Any] = {"effect": "flipbook"}
    if position is not None:
        moment_cfg["position"] = position
    spec = {"variant_id": "v5", "carousel_moment": moment_cfg}
    steps = [_step("clip_1"), _step("clip_2")]

    result = gb._insert_carousel_moment_step(
        steps,
        spec,
        clip_id_to_local={"clip_1": "/a", "clip_2": "/b"},
        clip_id_to_gcs={"clip_1": "gs://a", "clip_2": "gs://b"},
        probe_map={},
        variant_dir="/tmp",
    )

    assert result[expected_index].clip_id == "__carousel_v5"
    assert len(result) == 3


# ── _maybe_render_carousel_moment: clip-path selection + never-raise wrap ───


def test_maybe_render_carousel_moment_builds_ordered_deduped_clip_paths(monkeypatch):
    captured: dict[str, Any] = {}

    def _fake_render(spec, work_dir):
        captured["spec"] = spec
        captured["work_dir"] = work_dir
        return "/tmp/rendered.mp4"

    import app.pipeline.carousel.segment as segment_mod

    monkeypatch.setattr(segment_mod, "render_carousel_moment", _fake_render)

    # clip_1 repeats (must dedup, keep first-seen order); clip_6 is the 7th
    # distinct clip and must be dropped by the 5-clip cap.
    steps = [
        _step("clip_1"),
        _step("clip_2"),
        _step("clip_1"),
        _step("clip_3"),
        _step("clip_4"),
        _step("clip_5"),
        _step("clip_6"),
    ]
    clip_id_to_local = {
        "clip_1": "/a",
        "clip_2": "/b",
        "clip_3": "/c",
        "clip_4": "/d",
        "clip_5": "/e",
        "clip_6": "/f",
    }

    result = gb._maybe_render_carousel_moment(
        {"effect": "cover_flow", "duration_s": 2.5},
        clip_id_to_local=clip_id_to_local,
        steps=steps,
        variant_dir="/tmp/variant",
    )

    assert result == "/tmp/rendered.mp4"
    assert captured["work_dir"] == "/tmp/variant"
    spec = captured["spec"]
    assert spec.effect == "cover_flow"
    assert spec.duration_s == 2.5
    assert spec.clip_paths == ("/a", "/b", "/c", "/d", "/e")


def test_maybe_render_carousel_moment_defaults_effect_to_scale_sweep(monkeypatch):
    captured: dict[str, Any] = {}

    def _fake_render(spec, work_dir):
        captured["spec"] = spec
        return "/tmp/rendered.mp4"

    import app.pipeline.carousel.segment as segment_mod

    monkeypatch.setattr(segment_mod, "render_carousel_moment", _fake_render)

    gb._maybe_render_carousel_moment(
        {},  # no "effect" or "duration_s" -> defaults
        clip_id_to_local={"clip_1": "/a"},
        steps=[_step("clip_1")],
        variant_dir="/tmp/variant",
    )

    assert captured["spec"].effect == "scale_sweep"
    assert captured["spec"].duration_s == 4.0


def test_maybe_render_carousel_moment_contains_unexpected_exception(monkeypatch):
    import app.pipeline.carousel.segment as segment_mod

    def _boom(spec, work_dir):
        raise RuntimeError("should never happen per contract, but just in case")

    monkeypatch.setattr(segment_mod, "render_carousel_moment", _boom)

    result = gb._maybe_render_carousel_moment(
        {"effect": "scale_sweep"},
        clip_id_to_local={"clip_1": "/a"},
        steps=[_step("clip_1")],
        variant_dir="/tmp/variant",
    )

    assert result is None


def test_maybe_render_carousel_moment_no_local_paths_returns_none(monkeypatch):
    def _boom(*args, **kwargs):
        raise AssertionError("must not call render_carousel_moment with zero clip_paths")

    import app.pipeline.carousel.segment as segment_mod

    monkeypatch.setattr(segment_mod, "render_carousel_moment", _boom)

    result = gb._maybe_render_carousel_moment(
        {"effect": "scale_sweep"},
        clip_id_to_local={},  # nothing resolvable
        steps=[_step("clip_1")],
        variant_dir="/tmp/variant",
    )

    assert result is None
