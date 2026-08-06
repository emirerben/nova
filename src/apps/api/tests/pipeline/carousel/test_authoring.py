"""Carousel-moment AUTHORING policy (`_author_carousel_moments` in
generative_build.py): attaches `spec["carousel_moment"]` to one eligible
montage-path spec per job so the already-merged render hook
(`_insert_carousel_moment_step`) actually fires on real generative jobs,
instead of only on manually-crafted specs.

Also covers the two additive pieces that ride along with authoring:
  - interest/labels wiring: `_direct_auto_carousel_spec` now maps a clip's
    Gemini analysis (`ClipMeta.hook_score` / `best_moments[].energy` /
    `detected_subject`) onto `director.ClipInfo`'s `interest`/`labels`.
  - observability: `_insert_carousel_moment_step` now emits
    `carousel_moment_inserted` / `carousel_moment_skipped` trace events.

Kill-switch discipline mirrors `test_segment_kill_switch.py`: both
`carousel_effects_enabled` (render) and `carousel_auto_author_enabled`
(this policy) default False here regardless of the shipped default, so
this file stays order-independent.
"""

from __future__ import annotations

import types
from typing import Any

import pytest

import app.pipeline.carousel.director as director_mod
import app.services.pipeline_trace as pt
import app.tasks.generative_build as gb
from app.pipeline.agents.gemini_analyzer import AssemblyStep


def _step(clip_id: str) -> AssemblyStep:
    return AssemblyStep(slot={}, clip_id=clip_id, moment={"start_s": 0.0, "end_s": 1.0})


def _montage_spec(variant_id: str, **extra: Any) -> dict[str, Any]:
    spec: dict[str, Any] = {"variant_id": variant_id, "text_mode": "agent_text", "track": None}
    spec.update(extra)
    return spec


@pytest.fixture(autouse=True)
def _flags_off_by_default(monkeypatch):
    monkeypatch.setattr(gb.settings, "carousel_effects_enabled", False, raising=False)
    monkeypatch.setattr(gb.settings, "carousel_auto_author_enabled", False, raising=False)


def _capture_events(monkeypatch) -> list[tuple[str, str, dict]]:
    events: list[tuple[str, str, dict]] = []
    monkeypatch.setattr(
        pt,
        "record_pipeline_event",
        lambda stage, event, data=None: events.append((stage, event, data or {})),
    )
    return events


# ── Flag combinations ────────────────────────────────────────────────────────


def test_both_flags_off_is_noop(monkeypatch):
    events = _capture_events(monkeypatch)
    specs = [_montage_spec("song_text"), _montage_spec("original_text")]
    before = [dict(s) for s in specs]

    gb._author_carousel_moments(specs, job_id="job-1", n_clips=5)

    assert specs == before
    assert events == []


def test_only_render_flag_on_is_noop(monkeypatch):
    monkeypatch.setattr(gb.settings, "carousel_effects_enabled", True, raising=False)
    events = _capture_events(monkeypatch)
    specs = [_montage_spec("song_text"), _montage_spec("original_text")]
    before = [dict(s) for s in specs]

    gb._author_carousel_moments(specs, job_id="job-1", n_clips=5)

    assert specs == before
    assert events == []


def test_only_author_flag_on_is_noop(monkeypatch):
    monkeypatch.setattr(gb.settings, "carousel_auto_author_enabled", True, raising=False)
    events = _capture_events(monkeypatch)
    specs = [_montage_spec("song_text"), _montage_spec("original_text")]
    before = [dict(s) for s in specs]

    gb._author_carousel_moments(specs, job_id="job-1", n_clips=5)

    assert specs == before
    assert events == []


def test_both_flags_on_authors_exactly_one_spec(monkeypatch):
    monkeypatch.setattr(gb.settings, "carousel_effects_enabled", True, raising=False)
    monkeypatch.setattr(gb.settings, "carousel_auto_author_enabled", True, raising=False)
    events = _capture_events(monkeypatch)
    specs = [
        _montage_spec("song_lyrics"),
        _montage_spec("song_text"),
        _montage_spec("original_text"),
    ]

    gb._author_carousel_moments(specs, job_id="job-1", n_clips=5)

    authored = [s for s in specs if "carousel_moment" in s]
    assert len(authored) == 1
    moment = authored[0]["carousel_moment"]
    assert moment["auto"] is True
    assert isinstance(moment["seed"], int)
    assert moment["position"] in ("intro", "middle", "outro")

    assert len(events) == 1
    stage, event, data = events[0]
    assert (stage, event) == ("assembly", "carousel_moment_authored")
    assert data["variant"] == authored[0]["variant_id"]
    assert data["position"] == moment["position"]


# ── Determinism ──────────────────────────────────────────────────────────────


def test_same_job_id_picks_same_variant_and_position(monkeypatch):
    monkeypatch.setattr(gb.settings, "carousel_effects_enabled", True, raising=False)
    monkeypatch.setattr(gb.settings, "carousel_auto_author_enabled", True, raising=False)
    _capture_events(monkeypatch)

    def _run():
        specs = [
            _montage_spec("song_lyrics"),
            _montage_spec("song_text"),
            _montage_spec("original_text"),
        ]
        gb._author_carousel_moments(specs, job_id="job-repro", n_clips=5)
        authored = next(s for s in specs if "carousel_moment" in s)
        return authored["variant_id"], authored["carousel_moment"]["position"]

    first = _run()
    second = _run()
    assert first == second


def test_different_job_ids_vary_position_across_samples(monkeypatch):
    monkeypatch.setattr(gb.settings, "carousel_effects_enabled", True, raising=False)
    monkeypatch.setattr(gb.settings, "carousel_auto_author_enabled", True, raising=False)
    _capture_events(monkeypatch)

    positions: set[str] = set()
    for i in range(20):
        specs = [
            _montage_spec("song_lyrics"),
            _montage_spec("song_text"),
            _montage_spec("original_text"),
        ]
        gb._author_carousel_moments(specs, job_id=f"job-{i}", n_clips=5)
        authored = next(s for s in specs if "carousel_moment" in s)
        positions.add(authored["carousel_moment"]["position"])

    assert len(positions) >= 2


# ── Eligibility ──────────────────────────────────────────────────────────────


def test_fewer_than_three_clips_is_noop(monkeypatch):
    monkeypatch.setattr(gb.settings, "carousel_effects_enabled", True, raising=False)
    monkeypatch.setattr(gb.settings, "carousel_auto_author_enabled", True, raising=False)
    events = _capture_events(monkeypatch)
    specs = [_montage_spec("song_text"), _montage_spec("original_text")]
    before = [dict(s) for s in specs]

    gb._author_carousel_moments(specs, job_id="job-1", n_clips=2)

    assert specs == before
    assert events == []


def test_specs_with_archetype_key_never_chosen(monkeypatch):
    monkeypatch.setattr(gb.settings, "carousel_effects_enabled", True, raising=False)
    monkeypatch.setattr(gb.settings, "carousel_auto_author_enabled", True, raising=False)
    events = _capture_events(monkeypatch)
    specs = [
        _montage_spec("talking_head", archetype="talking_head"),
        _montage_spec("voiceover_only", archetype="voiceover"),
        _montage_spec("subtitled", archetype="subtitled"),
    ]
    before = [dict(s) for s in specs]

    gb._author_carousel_moments(specs, job_id="job-1", n_clips=5)

    assert specs == before  # no archetype-bearing spec ever gets carousel_moment
    assert events == []


def test_pre_existing_carousel_moment_not_overwritten_or_double_authored(monkeypatch):
    monkeypatch.setattr(gb.settings, "carousel_effects_enabled", True, raising=False)
    monkeypatch.setattr(gb.settings, "carousel_auto_author_enabled", True, raising=False)
    events = _capture_events(monkeypatch)
    preset_moment = {"auto": False, "effect": "cover_flow", "position": "outro"}
    specs = [
        _montage_spec("song_text", carousel_moment=dict(preset_moment)),
    ]

    gb._author_carousel_moments(specs, job_id="job-1", n_clips=5)

    # The only montage spec already had carousel_moment -> no eligible specs,
    # so it must be untouched (not overwritten) and no second spec was authored.
    assert specs[0]["carousel_moment"] == preset_moment
    assert events == []


def test_pre_existing_carousel_moment_excluded_but_sibling_spec_still_eligible(monkeypatch):
    monkeypatch.setattr(gb.settings, "carousel_effects_enabled", True, raising=False)
    monkeypatch.setattr(gb.settings, "carousel_auto_author_enabled", True, raising=False)
    events = _capture_events(monkeypatch)
    preset_moment = {"auto": False, "effect": "cover_flow", "position": "outro"}
    specs = [
        _montage_spec("song_text", carousel_moment=dict(preset_moment)),
        _montage_spec("original_text"),
    ]

    gb._author_carousel_moments(specs, job_id="job-1", n_clips=5)

    assert specs[0]["carousel_moment"] == preset_moment  # untouched
    assert specs[1]["carousel_moment"]["auto"] is True  # the only eligible spec
    assert len(events) == 1
    assert events[0][2]["variant"] == "original_text"


# ── Interest/labels wiring (`_direct_auto_carousel_spec`) ───────────────────


def test_direct_auto_carousel_spec_maps_clip_meta_to_interest_and_labels(monkeypatch):
    captured: dict[str, Any] = {}

    def _fake_direct(clips, *, seed, target_duration_s):
        captured["clips"] = clips
        return object()

    monkeypatch.setattr(director_mod, "direct_carousel_moment", _fake_direct)
    monkeypatch.setattr(gb, "_apply_moment_overrides", lambda spec, cfg: spec)

    meta_hook = types.SimpleNamespace(
        clip_path="/a.mp4",
        hook_score=8.0,
        best_moments=[{"energy": 2.0}],  # hook_score wins over best_moments when > 0
        detected_subject="beach sunset",
    )
    meta_energy_fallback = types.SimpleNamespace(
        clip_path="/b.mp4",
        hook_score=0.0,
        best_moments=[{"energy": 3.0}, {"energy": 7.0}],
        detected_subject="",
    )
    meta_no_signal = types.SimpleNamespace(
        clip_path="/c.mp4",
        hook_score=None,
        best_moments=[],
        detected_subject="",
    )
    probe_map = {
        "/a.mp4": types.SimpleNamespace(duration_s=3.0),
        "/b.mp4": types.SimpleNamespace(duration_s=3.0),
        "/c.mp4": types.SimpleNamespace(duration_s=3.0),
    }

    gb._direct_auto_carousel_spec(
        {},
        clip_paths=["/a.mp4", "/b.mp4", "/c.mp4"],
        probe_map=probe_map,
        variant_id="v1",
        clip_metas=[meta_hook, meta_energy_fallback, meta_no_signal],
    )

    clips = captured["clips"]
    # hook_score 8.0 / 10 -> 0.8; ClipMeta.hook_score is 0..10 per
    # app/agents/clip_metadata.py's Field(ge=0, le=10).
    assert clips[0].interest == pytest.approx(0.8)
    assert clips[0].labels == ("beach sunset",)
    # hook_score is 0 (falsy) -> fall back to max best_moments energy (7.0/10).
    assert clips[1].interest == pytest.approx(0.7)
    assert clips[1].labels == ()
    # No usable signal at all -> ClipInfo's own neutral default.
    assert clips[2].interest == pytest.approx(0.5)
    assert clips[2].labels == ()


def test_direct_auto_carousel_spec_without_clip_metas_uses_defaults(monkeypatch):
    captured: dict[str, Any] = {}

    def _fake_direct(clips, *, seed, target_duration_s):
        captured["clips"] = clips
        return object()

    monkeypatch.setattr(director_mod, "direct_carousel_moment", _fake_direct)
    monkeypatch.setattr(gb, "_apply_moment_overrides", lambda spec, cfg: spec)

    gb._direct_auto_carousel_spec(
        {},
        clip_paths=["/a.mp4"],
        probe_map={"/a.mp4": types.SimpleNamespace(duration_s=3.0)},
        variant_id="v1",
        # clip_metas omitted entirely — must be byte-identical to pre-wiring.
    )

    clip = captured["clips"][0]
    assert clip.interest == 0.5
    assert clip.labels == ()


# ── Trace events: insert / skip (`_insert_carousel_moment_step`) ────────────


def test_insert_emits_inserted_event_with_effect_mode_duration(monkeypatch):
    monkeypatch.setattr(gb.settings, "carousel_effects_enabled", True, raising=False)
    events = _capture_events(monkeypatch)

    def _fake_maybe_render(moment_cfg, *, render_meta=None, **kwargs):
        if render_meta is not None:
            render_meta["effect"] = "cover_flow"
            render_meta["mode"] = "focus"
        return "/tmp/variant/carousel_moment.mp4"

    monkeypatch.setattr(gb, "_maybe_render_carousel_moment", _fake_maybe_render)

    import app.pipeline.probe as probe_mod

    monkeypatch.setattr(
        probe_mod, "probe_video", lambda path: types.SimpleNamespace(duration_s=4.25)
    )

    spec = {"variant_id": "v-insert", "carousel_moment": {"auto": True, "position": "middle"}}
    gb._insert_carousel_moment_step(
        [_step("clip_1"), _step("clip_2")],
        spec,
        clip_id_to_local={"clip_1": "/a", "clip_2": "/b"},
        clip_id_to_gcs={"clip_1": "gs://a", "clip_2": "gs://b"},
        probe_map={},
        variant_dir="/tmp/variant",
    )

    assert len(events) == 1
    stage, event, data = events[0]
    assert (stage, event) == ("assembly", "carousel_moment_inserted")
    assert data == {
        "variant_id": "v-insert",
        "position": "middle",
        "effect": "cover_flow",
        "mode": "focus",
        "duration_s": 4.25,
    }


def test_skip_event_when_render_returns_none(monkeypatch):
    monkeypatch.setattr(gb.settings, "carousel_effects_enabled", True, raising=False)
    events = _capture_events(monkeypatch)
    monkeypatch.setattr(gb, "_maybe_render_carousel_moment", lambda *a, **kw: None)

    spec = {"variant_id": "v-skip", "carousel_moment": {"effect": "scale_sweep"}}
    result = gb._insert_carousel_moment_step(
        [_step("clip_1")],
        spec,
        clip_id_to_local={"clip_1": "/a"},
        clip_id_to_gcs={"clip_1": "gs://a"},
        probe_map={},
        variant_dir="/tmp/variant",
    )

    assert result == [_step("clip_1")]
    assert len(events) == 1
    stage, event, data = events[0]
    assert (stage, event) == ("assembly", "carousel_moment_skipped")
    assert data == {"variant_id": "v-skip", "reason": "render_unavailable"}


def test_skip_event_when_probe_raises(monkeypatch):
    monkeypatch.setattr(gb.settings, "carousel_effects_enabled", True, raising=False)
    events = _capture_events(monkeypatch)
    monkeypatch.setattr(gb, "_maybe_render_carousel_moment", lambda *a, **kw: "/tmp/moment.mp4")

    import app.pipeline.probe as probe_mod

    def _boom(path):
        raise RuntimeError("ffprobe failed")

    monkeypatch.setattr(probe_mod, "probe_video", _boom)

    spec = {"variant_id": "v-probe-fail", "carousel_moment": {"effect": "scale_sweep"}}
    gb._insert_carousel_moment_step(
        [_step("clip_1")],
        spec,
        clip_id_to_local={"clip_1": "/a"},
        clip_id_to_gcs={"clip_1": "gs://a"},
        probe_map={},
        variant_dir="/tmp/variant",
    )

    assert len(events) == 1
    stage, event, data = events[0]
    assert (stage, event) == ("assembly", "carousel_moment_skipped")
    assert data == {"variant_id": "v-probe-fail", "reason": "probe_failed"}


def test_skip_event_when_probed_duration_non_positive(monkeypatch):
    monkeypatch.setattr(gb.settings, "carousel_effects_enabled", True, raising=False)
    events = _capture_events(monkeypatch)
    monkeypatch.setattr(gb, "_maybe_render_carousel_moment", lambda *a, **kw: "/tmp/moment.mp4")

    import app.pipeline.probe as probe_mod

    monkeypatch.setattr(
        probe_mod, "probe_video", lambda path: types.SimpleNamespace(duration_s=0.0)
    )

    spec = {"variant_id": "v-zero-dur", "carousel_moment": {"effect": "scale_sweep"}}
    gb._insert_carousel_moment_step(
        [_step("clip_1")],
        spec,
        clip_id_to_local={"clip_1": "/a"},
        clip_id_to_gcs={"clip_1": "gs://a"},
        probe_map={},
        variant_dir="/tmp/variant",
    )

    assert len(events) == 1
    stage, event, data = events[0]
    assert (stage, event) == ("assembly", "carousel_moment_skipped")
    assert data == {"variant_id": "v-zero-dur", "reason": "non_positive_duration"}


def test_insert_flag_off_emits_no_events(monkeypatch):
    events = _capture_events(monkeypatch)

    def _boom(*a, **kw):
        raise AssertionError("must not render when the flag is off")

    monkeypatch.setattr(gb, "_maybe_render_carousel_moment", _boom)

    spec = {"variant_id": "v-flag-off", "carousel_moment": {"effect": "scale_sweep"}}
    gb._insert_carousel_moment_step(
        [_step("clip_1")],
        spec,
        clip_id_to_local={"clip_1": "/a"},
        clip_id_to_gcs={"clip_1": "gs://a"},
        probe_map={},
        variant_dir="/tmp/variant",
    )

    assert events == []


# ── Persist-and-inherit: an authored moment must survive re-render ──────────
#
# BLOCKER regression found in review: an authored carousel_moment reached
# neither the initial pending `_upsert_variant_entry` write nor the `base`
# dict `_render_generative_variant` returns, and `_run_regenerate_variant`'s
# fresh spec build never read `existing.get("carousel_moment")` — so a
# render -> retext/swap-song -> the moment silently vanished (no event, no
# log). These tests pin the fix: the moment is seeded onto BOTH persistence
# points (mirroring how `music_start_s`/`intro_text` are persisted) and
# threaded forward on regen (mirroring `music_start_s`/`user_style_knobs`).


def test_initial_pending_upsert_persists_authored_carousel_moment(monkeypatch):
    """The FIRST (pending, pre-render) `_upsert_variant_entry` write for the
    authored variant must already carry `carousel_moment` — not just the
    post-render write — so a crash between authoring and the first real
    persist doesn't leave `_run_regenerate_variant`'s `existing.get(...)`
    read with nothing to find."""
    from tests.tasks.conftest import FakeJob as _FakeJob
    from tests.tasks.conftest import patch_job_session as _patch_job_session
    from tests.tasks.test_generative_build import _Meta as _GBMeta
    from tests.tasks.test_generative_build import _Probe as _GBProbe

    monkeypatch.setattr(gb.settings, "carousel_effects_enabled", True, raising=False)
    monkeypatch.setattr(gb.settings, "carousel_auto_author_enabled", True, raising=False)

    job = _FakeJob(assembly_plan={})
    job.status = "queued"
    job.mode = "generative"
    job.all_candidates = {
        "clip_paths": ["u/c1.mp4", "u/c2.mp4", "u/c3.mp4"],
        "edit_format": "montage",
    }
    _patch_job_session(monkeypatch, job)

    monkeypatch.setattr(gb, "record_phase", lambda *a, **k: None, raising=False)
    monkeypatch.setattr(pt, "record_pipeline_event", lambda *a, **k: None, raising=False)
    monkeypatch.setattr(gb, "_persist_durable_sources", lambda _job_id, paths: paths)
    metas = [_GBMeta("c1", 5.0), _GBMeta("c2", 4.0), _GBMeta("c3", 6.0)]
    monkeypatch.setattr(
        gb,
        "_ingest_clips",
        lambda *a, **k: {
            "clip_metas": metas,
            "clip_id_to_gcs": {"c1": "u/c1.mp4", "c2": "u/c2.mp4", "c3": "u/c3.mp4"},
            "clip_id_to_local": {"c1": "/tmp/c1.mp4", "c2": "/tmp/c2.mp4", "c3": "/tmp/c3.mp4"},
            "probe_map": {
                "/tmp/c1.mp4": _GBProbe(6.0),
                "/tmp/c2.mp4": _GBProbe(6.0),
                "/tmp/c3.mp4": _GBProbe(6.0),
            },
            "hero": metas[0],
        },
    )
    monkeypatch.setattr(gb, "_pretonemap_hdr_clips", lambda *a, **k: 0)
    monkeypatch.setattr(gb, "_run_text_agents", lambda *a, **k: ("Text", {}))
    monkeypatch.setattr(gb, "_select_generative_style_set", lambda *a, **k: "default")
    monkeypatch.setattr(gb, "_match_best_track", lambda *a, **k: None)
    monkeypatch.setattr(gb, "_resolve_archetype", lambda *a, **k: ("montage", None, None))
    monkeypatch.setattr(gb, "_set_status", lambda *a, **k: None)
    monkeypatch.setattr(gb, "_persist_archetype_fallback", lambda *a, **k: None)
    monkeypatch.setattr(gb, "_existing_variants", lambda *a, **k: [])
    monkeypatch.setattr(gb, "_update_variant_entry", lambda *a, **k: None)
    monkeypatch.setattr(gb, "_maybe_add_text_elements_snapshot", lambda *a, **k: None)
    monkeypatch.setattr(gb, "_finalize_job", lambda *a, **k: None)
    monkeypatch.setattr(gb, "_maybe_autoplace_after_finalize", lambda *a, **k: None)
    monkeypatch.setattr(
        gb,
        "_render_generative_variant",
        lambda **kw: {
            "ok": True,
            "variant_id": kw["spec"]["variant_id"],
            "rank": kw["rank"],
            "render_status": "ready",
            "output_url": "https://signed/out.mp4",
            # Mirrors the real `base` dict: seeded straight from spec.
            "carousel_moment": kw["spec"].get("carousel_moment"),
        },
    )

    upserts: list[dict] = []
    monkeypatch.setattr(gb, "_upsert_variant_entry", lambda jid, entry: upserts.append(dict(entry)))

    gb._run_generative_job("55555555-5555-5555-5555-555555555555")

    pending = [u for u in upserts if u.get("render_status") == "pending"]
    assert len(pending) == 1  # single montage spec: no track matched
    authored_moment = pending[0]["carousel_moment"]
    assert authored_moment is not None
    assert authored_moment["auto"] is True
    assert authored_moment["position"] in ("intro", "middle", "outro")

    ready = [u for u in upserts if u.get("render_status") == "ready"]
    assert ready[-1]["carousel_moment"] == authored_moment


def test_render_generative_variant_persists_carousel_moment_on_success(monkeypatch, tmp_path):
    """The `base` dict `_render_generative_variant` returns must carry
    `spec["carousel_moment"]` through to the persisted result on success."""
    from tests.tasks.test_generative_build import _Meta as _GBMeta
    from tests.tasks.test_generative_build import _patch_render_helpers as _gb_patch_render_helpers

    mix_calls: list = []
    _gb_patch_render_helpers(monkeypatch, mix_calls)
    vdir = tmp_path / "v1"
    vdir.mkdir()
    moment = {"auto": True, "seed": 777, "position": "middle"}
    spec = {
        "variant_id": "original_text",
        "rank": 1,
        "text_mode": "none",
        "track": None,
        "carousel_moment": moment,
    }
    res = gb._render_generative_variant(
        job_id="j",
        rank=1,
        spec=spec,
        clip_metas=[_GBMeta("c1", 5.0)],
        clip_id_to_local={"c1": "/x.mp4"},
        clip_id_to_gcs={"c1": "music-uploads/x.mp4"},
        probe_map={},
        available_footage_s=12.0,
        agent_text=None,
        agent_form={},
        variant_dir=str(vdir),
    )

    assert res["ok"] is True
    assert res["carousel_moment"] == moment


def test_render_generative_variant_persists_carousel_moment_on_failure(monkeypatch, tmp_path):
    """Failure-patch hygiene (see `_run_regenerate_variant`'s `failure_patch`
    filter, which drops only `None` values): `carousel_moment` is set on
    `base` BEFORE the try block, so a mid-render crash still returns it —
    same lifecycle as `music_start_s`."""
    from tests.tasks.test_generative_build import _Meta as _GBMeta
    from tests.tasks.test_generative_build import _patch_render_helpers as _gb_patch_render_helpers

    mix_calls: list = []
    _gb_patch_render_helpers(monkeypatch, mix_calls)

    import app.storage as storage

    def _boom(*a, **k):
        raise RuntimeError("upload blew up")

    monkeypatch.setattr(storage, "upload_public_read", _boom, raising=False)

    vdir = tmp_path / "v2"
    vdir.mkdir()
    moment = {"auto": True, "seed": 888, "position": "outro"}
    spec = {
        "variant_id": "original_text",
        "rank": 1,
        "text_mode": "none",
        "track": None,
        "carousel_moment": moment,
    }
    res = gb._render_generative_variant(
        job_id="j",
        rank=1,
        spec=spec,
        clip_metas=[_GBMeta("c1", 5.0)],
        clip_id_to_local={"c1": "/x.mp4"},
        clip_id_to_gcs={"c1": "music-uploads/x.mp4"},
        probe_map={},
        available_footage_s=12.0,
        agent_text=None,
        agent_form={},
        variant_dir=str(vdir),
    )

    assert res["ok"] is False
    assert res["carousel_moment"] == moment


def test_regen_inherits_existing_carousel_moment_and_reinserts_step(monkeypatch):
    """BLOCKER regression: a retext/swap-song/restyle re-render must not
    silently drop an authored carousel_moment. Drives the REAL
    `_run_regenerate_variant` path (spec build -> `_render_generative_variant`
    -> `_insert_carousel_moment_step`) with only the ffmpeg-level primitives
    monkeypatched, so the spec-threading AND the carousel splice itself are
    both exercised for real, not asserted in isolation."""
    from tests.tasks.test_generative_build import _Meta as _GBMeta
    from tests.tasks.test_generative_build import _Probe as _GBProbe
    from tests.tasks.test_generative_timeline_render import CLIP_PATHS as _REGEN_CLIP_PATHS
    from tests.tasks.test_generative_timeline_render import JOB_ID as _REGEN_JOB_ID
    from tests.tasks.test_generative_timeline_render import (
        _existing_variant,
        _patch_music_recipe,
        _regen_setup,
    )
    from tests.tasks.test_generative_timeline_render import _track as _regen_track

    monkeypatch.setattr(gb.settings, "carousel_effects_enabled", True, raising=False)

    moment = {"auto": True, "seed": 424242, "position": "outro"}
    variant = _existing_variant(
        variant_id="song_text",
        rank=1,
        text_mode="agent_text",
        music_track_id="t1",
        carousel_moment=dict(moment),
    )
    assembled_steps: list = []
    _job, updates, _dl = _regen_setup(
        monkeypatch, variants=[variant], track=_regen_track("t1"), assembled_steps=assembled_steps
    )
    _patch_music_recipe(monkeypatch, [0.0, 1.0])

    metas = [_GBMeta("g_a", 8.0), _GBMeta("g_b", 6.0), _GBMeta("g_c", 5.0)]
    ingest = {
        "clip_metas": metas,
        "clip_id_to_gcs": {
            "g_a": _REGEN_CLIP_PATHS[0],
            "g_b": _REGEN_CLIP_PATHS[1],
            "g_c": _REGEN_CLIP_PATHS[2],
        },
        "clip_id_to_local": {"g_a": "/a.mp4", "g_b": "/b.mp4", "g_c": "/c.mp4"},
        "probe_map": {
            "/a.mp4": _GBProbe(6.0),
            "/b.mp4": _GBProbe(6.0),
            "/c.mp4": _GBProbe(6.0),
        },
        "hero": metas[0],
    }
    monkeypatch.setattr(gb, "_ingest_clips", lambda *a, **k: ingest, raising=False)

    # Real assembly steps (not the `_patch_render_helpers` default empty
    # list) so `_insert_carousel_moment_step` has clip_paths to work with.
    import app.pipeline.template_matcher as tm

    match_steps = [
        types.SimpleNamespace(
            clip_id="g_a",
            slot={"position": 1},
            moment={"start_s": 0.0, "end_s": 2.0, "energy": 5.0, "description": "d"},
        ),
        types.SimpleNamespace(
            clip_id="g_b",
            slot={"position": 2},
            moment={"start_s": 0.0, "end_s": 2.0, "energy": 5.0, "description": "d"},
        ),
    ]
    monkeypatch.setattr(
        tm,
        "match",
        lambda recipe, metas, **kw: types.SimpleNamespace(steps=match_steps),
        raising=False,
    )

    import app.pipeline.carousel.segment as segment_mod
    import app.pipeline.probe as probe_mod

    monkeypatch.setattr(
        segment_mod, "render_carousel_moment", lambda spec, work_dir: "/tmp/moment.mp4"
    )
    monkeypatch.setattr(
        probe_mod, "probe_video", lambda path: types.SimpleNamespace(duration_s=3.0)
    )

    gb._run_regenerate_variant(_REGEN_JOB_ID, "song_text", None, None, False)

    assert updates[-1]["ok"] is True
    assert updates[-1]["carousel_moment"] == moment

    spliced = assembled_steps[-1]
    assert any(step.clip_id.startswith("__carousel_") for step in spliced)
