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

import dataclasses
import sys
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


# ── _apply_moment_overrides: precedence, schema-missing degrades gracefully ─


@dataclasses.dataclass(frozen=True)
class _FullSpec:
    """Stand-in for a post-landing `CarouselMomentSpec` (has mode/focus_moments/
    seed) — lets override precedence be pinned without depending on the
    concurrent lane's schema having landed yet."""

    effect: str
    clip_paths: tuple
    duration_s: float = 4.0
    mode: str = "stills"
    focus_moments: tuple = ()
    seed: int = 0


@dataclasses.dataclass(frozen=True)
class _PreLandingSpec:
    """Stand-in for TODAY's `CarouselMomentSpec` (no mode/focus_moments/seed)
    — exercises the schema-missing no-op path."""

    effect: str
    clip_paths: tuple
    duration_s: float = 4.0


def test_apply_moment_overrides_noop_when_moment_cfg_has_no_override_keys():
    spec = _FullSpec(effect="cover_flow", clip_paths=("/a",), duration_s=3.0)
    result = gb._apply_moment_overrides(spec, {"position": "outro"})
    assert result is spec  # unchanged object, not just equal


def test_apply_moment_overrides_effect_and_duration_win_over_base_spec():
    spec = _FullSpec(effect="cover_flow", clip_paths=("/a",), duration_s=3.0)
    result = gb._apply_moment_overrides(spec, {"effect": "flipbook", "duration_s": 5.5})
    assert result.effect == "flipbook"
    assert result.duration_s == 5.5
    assert result.clip_paths == ("/a",)  # untouched fields preserved


def test_apply_moment_overrides_mode_and_seed_win_when_schema_supports_them():
    spec = _FullSpec(effect="cover_flow", clip_paths=("/a",), mode="rolling", seed=1)
    result = gb._apply_moment_overrides(spec, {"mode": "stills", "seed": 99})
    assert result.mode == "stills"
    assert result.seed == 99


def test_apply_moment_overrides_focus_override_parses_via_choreography(monkeypatch):
    @dataclasses.dataclass(frozen=True)
    class _FocusMomentStub:
        card_index: int
        hold_s: float = 2.0
        zoom_s: float = 0.6

    stub_mod = types.ModuleType("app.pipeline.carousel.choreography")
    stub_mod.FocusMoment = _FocusMomentStub
    monkeypatch.setitem(sys.modules, "app.pipeline.carousel.choreography", stub_mod)

    spec = _FullSpec(effect="cover_flow", clip_paths=("/a", "/b"), mode="focus")
    result = gb._apply_moment_overrides(spec, {"focus": [{"card_index": 1, "hold_s": 2.5}]})
    assert result.focus_moments == (_FocusMomentStub(card_index=1, hold_s=2.5),)


def test_apply_moment_overrides_mode_override_noop_on_pre_landing_schema(caplog):
    """The current (pre-landing) CarouselMomentSpec has no `mode` field.
    Overriding it must be a logged no-op, not a crash — this is the "schema
    hasn't landed yet" degrade path."""
    spec = _PreLandingSpec(effect="cover_flow", clip_paths=("/a",))
    result = gb._apply_moment_overrides(spec, {"mode": "focus"})
    assert result is spec
    assert not hasattr(result, "mode")


def test_apply_moment_overrides_focus_override_noop_on_pre_landing_schema():
    spec = _PreLandingSpec(effect="cover_flow", clip_paths=("/a",))
    result = gb._apply_moment_overrides(spec, {"focus": [{"card_index": 0}]})
    assert result is spec


# ── _stable_seed_from_variant: deterministic, not Python's randomized hash() ─


def test_stable_seed_from_variant_is_deterministic():
    assert gb._stable_seed_from_variant("variant-a") == gb._stable_seed_from_variant("variant-a")


def test_stable_seed_from_variant_differs_across_variants():
    assert gb._stable_seed_from_variant("variant-a") != gb._stable_seed_from_variant("variant-b")


def test_stable_seed_from_variant_handles_none():
    # Must not raise, and must stay deterministic (used when neither an
    # explicit moment_cfg["seed"] nor a variant_id is available).
    assert gb._stable_seed_from_variant(None) == gb._stable_seed_from_variant(None)


# ── _direct_auto_carousel_spec: auto-mode ClipInfo/seed wiring ─────────────


def test_direct_auto_carousel_spec_uses_probe_map_durations_with_fallback(monkeypatch):
    captured: dict[str, Any] = {}

    def _fake_direct(clips, *, seed, target_duration_s):
        captured["clips"] = clips
        captured["seed"] = seed
        captured["target_duration_s"] = target_duration_s
        return "SPEC"

    import app.pipeline.carousel.director as director_mod

    monkeypatch.setattr(director_mod, "direct_carousel_moment", _fake_direct)

    probe_map = {"/a": types.SimpleNamespace(duration_s=5.0)}  # "/b" missing -> fallback
    result = gb._direct_auto_carousel_spec(
        {},  # no explicit overrides
        clip_paths=["/a", "/b"],
        probe_map=probe_map,
        variant_id="variant-x",
    )

    assert result == "SPEC"
    clips = captured["clips"]
    assert [c.path for c in clips] == ["/a", "/b"]
    assert clips[0].duration_s == 5.0
    assert clips[1].duration_s == gb._AUTO_CAROUSEL_FALLBACK_DURATION_S
    assert captured["seed"] == gb._stable_seed_from_variant("variant-x")
    assert captured["target_duration_s"] == director_mod.DEFAULT_TARGET_DURATION_S


def test_direct_auto_carousel_spec_explicit_seed_and_duration_override_defaults(monkeypatch):
    captured: dict[str, Any] = {}

    def _fake_direct(clips, *, seed, target_duration_s):
        captured["seed"] = seed
        captured["target_duration_s"] = target_duration_s
        # A real (albeit pre-landing-shape) spec — `_apply_moment_overrides`
        # runs on whatever this returns, and needs a real dataclass instance.
        return _PreLandingSpec(effect="scale_sweep", clip_paths=("/a",))

    import app.pipeline.carousel.director as director_mod

    monkeypatch.setattr(director_mod, "direct_carousel_moment", _fake_direct)

    result = gb._direct_auto_carousel_spec(
        {"seed": 777, "duration_s": 9.5},
        clip_paths=["/a"],
        probe_map={},
        variant_id="variant-x",
    )

    assert captured["seed"] == 777
    assert captured["target_duration_s"] == 9.5
    assert result.duration_s == 9.5  # duration_s override applied on top


# ── _maybe_render_carousel_moment: auto-mode passthrough ───────────────────


def test_maybe_render_carousel_moment_auto_uses_director(monkeypatch):
    """Flag-on, moment_cfg["auto"] truthy -> `_maybe_render_carousel_moment`
    must route through `_direct_auto_carousel_spec` (not build a plain
    CarouselMomentSpec directly) and hand its result straight to
    `render_carousel_moment`."""
    sentinel_spec = object()
    captured: dict[str, Any] = {}

    def _fake_direct_auto(moment_cfg, *, clip_paths, probe_map, variant_id, **kwargs):
        captured["moment_cfg"] = moment_cfg
        captured["clip_paths"] = clip_paths
        captured["probe_map"] = probe_map
        captured["variant_id"] = variant_id
        return sentinel_spec

    monkeypatch.setattr(gb, "_direct_auto_carousel_spec", _fake_direct_auto)

    import app.pipeline.carousel.segment as segment_mod

    def _fake_render(spec, work_dir):
        captured["spec"] = spec
        captured["work_dir"] = work_dir
        return "/tmp/rendered.mp4"

    monkeypatch.setattr(segment_mod, "render_carousel_moment", _fake_render)

    probe_map = {"/a": types.SimpleNamespace(duration_s=4.0)}
    result = gb._maybe_render_carousel_moment(
        {"auto": True},
        clip_id_to_local={"clip_1": "/a"},
        steps=[_step("clip_1")],
        variant_dir="/tmp/variant",
        probe_map=probe_map,
        variant_id="variant-x",
    )

    assert result == "/tmp/rendered.mp4"
    assert captured["spec"] is sentinel_spec
    assert captured["clip_paths"] == ["/a"]
    assert captured["probe_map"] is probe_map
    assert captured["variant_id"] == "variant-x"


def test_maybe_render_carousel_moment_non_auto_still_builds_plain_spec_directly(monkeypatch):
    """Belt-and-braces: a non-auto moment_cfg must NOT go anywhere near the
    director, confirming the auto/non-auto branch really is a branch."""

    def _boom(*args, **kwargs):
        raise AssertionError("must not call the director for a non-auto moment_cfg")

    monkeypatch.setattr(gb, "_direct_auto_carousel_spec", _boom)

    import app.pipeline.carousel.segment as segment_mod

    monkeypatch.setattr(
        segment_mod, "render_carousel_moment", lambda spec, work_dir: "/tmp/rendered.mp4"
    )

    result = gb._maybe_render_carousel_moment(
        {"effect": "cover_flow"},
        clip_id_to_local={"clip_1": "/a"},
        steps=[_step("clip_1")],
        variant_dir="/tmp/variant",
    )

    assert result == "/tmp/rendered.mp4"


def test_maybe_render_carousel_moment_auto_render_failure_returns_none(monkeypatch):
    """The never-raise contract must still hold when the director path itself
    blows up (e.g. the concurrent lane's schema hasn't landed yet)."""

    def _boom(*args, **kwargs):
        raise TypeError("CarouselMomentSpec.__init__() got an unexpected keyword argument 'mode'")

    monkeypatch.setattr(gb, "_direct_auto_carousel_spec", _boom)

    result = gb._maybe_render_carousel_moment(
        {"auto": True},
        clip_id_to_local={"clip_1": "/a"},
        steps=[_step("clip_1")],
        variant_dir="/tmp/variant",
    )

    assert result is None


def test_insert_carousel_moment_step_threads_probe_map_and_variant_id(monkeypatch):
    """`_insert_carousel_moment_step` must pass its own `probe_map` and the
    spec's `variant_id` down into `_maybe_render_carousel_moment` — auto mode
    needs both (durations + a stable per-variant seed fallback)."""
    captured: dict[str, Any] = {}

    def _fake_maybe_render(
        moment_cfg, *, clip_id_to_local, steps, variant_dir, probe_map, variant_id, **kwargs
    ):
        captured["probe_map"] = probe_map
        captured["variant_id"] = variant_id
        return None  # steps unchanged path is already covered elsewhere

    monkeypatch.setattr(gb.settings, "carousel_effects_enabled", True, raising=False)
    monkeypatch.setattr(gb, "_maybe_render_carousel_moment", _fake_maybe_render)

    probe_map_sentinel: dict[str, Any] = {"marker": "probe-map"}
    gb._insert_carousel_moment_step(
        [_step("clip_1")],
        {"variant_id": "variant-xyz", "carousel_moment": {"auto": True}},
        clip_id_to_local={"clip_1": "/a"},
        clip_id_to_gcs={"clip_1": "gs://a"},
        probe_map=probe_map_sentinel,
        variant_dir="/tmp/variant",
    )

    assert captured["probe_map"] is probe_map_sentinel
    assert captured["variant_id"] == "variant-xyz"
