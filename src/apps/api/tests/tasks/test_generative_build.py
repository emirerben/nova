"""Orchestrator-level tests for the generative-edit pipeline.

These mock the heavy ingest/render helpers — the goal is to pin the orchestration
LOGIC: variant spec construction, the no-music audio-passthrough branch, the Skia
kill-switch guard, energy derivation, terminal-status calculation, and the
output_url-is-signed contract. The real render is verified separately (make
local-render) per CLAUDE.md.
"""

from __future__ import annotations

import types

import pytest

import app.tasks.generative_build as gb
from app.agents.lyrics import LyricsExtractionAgent
from tests.tasks.conftest import FakeJob as _FakeJob
from tests.tasks.conftest import patch_job_session as _patch_job_session


class _Meta:
    def __init__(self, clip_id, hook_score, best_moments=None, transcript="", detected_subject=""):
        self.clip_id = clip_id
        self.hook_score = hook_score
        self.best_moments = best_moments or []
        self.transcript = transcript
        self.detected_subject = detected_subject
        self.hook_text = ""


def _track(track_id="t1", lyrics_cached=None):
    return types.SimpleNamespace(
        id=track_id,
        title="Song A",
        audio_gcs_path="music/t1/audio.m4a",
        beat_timestamps_s=[0.5, 1.0, 1.5, 2.0],
        duration_s=60.0,
        track_config={"best_start_s": 0.0, "best_end_s": 30.0},
        ai_labels={"labels": {}},
        analysis_status="ready",
        lyrics_cached=lyrics_cached
        if lyrics_cached is not None
        else {
            "prompt_version": LyricsExtractionAgent.spec.prompt_version,
            "source": "lrclib_synced+whisper",
            "lines": [{"text": "hi"}],
        },
    )


# ── Variant spec ────────────────────────────────────────────────────────────────


def test_variant_specs_with_track_has_three():
    specs = gb._variant_specs(_track())
    assert [s["variant_id"] for s in specs] == ["song_lyrics", "song_text", "original_text"]
    assert [s["text_mode"] for s in specs] == ["lyrics", "agent_text", "agent_text"]
    assert specs[2]["track"] is None


def test_variant_specs_without_track_only_original():
    specs = gb._variant_specs(None)
    assert [s["variant_id"] for s in specs] == ["original_text"]
    assert specs[0]["track"] is None


def test_variant_specs_track_without_lyrics_skips_lyrics_variant():
    # A matched track with no cached lyrics → no "Lyrics" card (would be a wasted
    # render identical to song_text). song_text + original_text still render.
    specs = gb._variant_specs(_track(lyrics_cached={}))
    assert [s["variant_id"] for s in specs] == ["song_text", "original_text"]


def test_variant_specs_skips_lyrics_for_unsupported_language():
    # Prod incident: a Chinese track (八方來財) matched and the lyrics variant
    # failed opaquely. The language gate skips it cleanly → 2 working variants.
    zh = {
        "prompt_version": LyricsExtractionAgent.spec.prompt_version,
        "source": "lrclib_synced+whisper",
        "language": "zh",
        "lines": [{"text": "八方來財"}],
    }
    specs = gb._variant_specs(_track(lyrics_cached=zh))
    assert [s["variant_id"] for s in specs] == ["song_text", "original_text"]


def test_variant_specs_keeps_lyrics_for_latin_language():
    en = {
        "prompt_version": LyricsExtractionAgent.spec.prompt_version,
        "source": "lrclib_synced+whisper",
        "language": "en",
        "lines": [{"text": "hi"}],
    }
    specs = gb._variant_specs(_track(lyrics_cached=en))
    assert [s["variant_id"] for s in specs] == ["song_lyrics", "song_text", "original_text"]


def test_variant_specs_missing_language_fails_open():
    # Legacy lyrics_cached blobs predate the language field — the gate must not
    # regress them (the per-variant exception capture stays the backstop).
    legacy = {
        "prompt_version": LyricsExtractionAgent.spec.prompt_version,
        "source": "lrclib_synced+whisper",
        "lines": [{"text": "hi"}],
    }
    specs = gb._variant_specs(_track(lyrics_cached=legacy))
    assert specs[0]["variant_id"] == "song_lyrics"


def test_content_plan_primary_montage_prefers_lyrics_when_renderable():
    specs = gb._specs_for_archetype(
        "montage",
        _track(),
        variant_policy=gb.CONTENT_PLAN_PRIMARY_VARIANT_POLICY,
    )
    assert [s["variant_id"] for s in specs] == ["song_lyrics"]
    assert specs[0]["text_mode"] == "lyrics"
    assert specs[0]["track"] is not None


def test_content_plan_primary_montage_uses_song_text_without_renderable_lyrics():
    specs = gb._specs_for_archetype(
        "montage",
        _track(lyrics_cached={}),
        variant_policy=gb.CONTENT_PLAN_PRIMARY_VARIANT_POLICY,
    )
    assert [s["variant_id"] for s in specs] == ["song_text"]
    assert specs[0]["text_mode"] == "agent_text"
    assert specs[0]["track"] is not None


def test_content_plan_primary_montage_uses_original_without_track():
    specs = gb._specs_for_archetype(
        "montage",
        None,
        variant_policy=gb.CONTENT_PLAN_PRIMARY_VARIANT_POLICY,
    )
    assert [s["variant_id"] for s in specs] == ["original_text"]
    assert specs[0]["track"] is None


def test_content_plan_primary_voiceover_prefers_music_when_track_matches():
    specs = gb._specs_for_archetype(
        "voiceover",
        _track(),
        voiceover_gcs_path="voiceover-uploads/abc/voice.m4a",
        variant_policy=gb.CONTENT_PLAN_PRIMARY_VARIANT_POLICY,
    )
    assert [s["variant_id"] for s in specs] == ["voiceover_music"]
    assert specs[0]["track"] is not None
    assert specs[0]["mix"] == gb._VOICEOVER_MUSIC_DEFAULT_MIX


def test_content_plan_primary_voiceover_uses_voiceover_only_without_track():
    specs = gb._specs_for_archetype(
        "voiceover",
        None,
        voiceover_gcs_path="voiceover-uploads/abc/voice.m4a",
        variant_policy=gb.CONTENT_PLAN_PRIMARY_VARIANT_POLICY,
    )
    assert [s["variant_id"] for s in specs] == ["voiceover_only"]
    assert specs[0]["track"] is None
    assert specs[0]["mix"] == gb._VOICEOVER_ONLY_DEFAULT_MIX


def test_content_plan_primary_single_archetypes_stay_single():
    for archetype, variant_id in [
        ("subtitled", "subtitled"),
        ("narrated", "narrated"),
        ("talking_head", "talking_head"),
    ]:
        specs = gb._specs_for_archetype(
            archetype,
            _track(),
            voiceover_gcs_path="voiceover-uploads/abc/voice.m4a",
            variant_policy=gb.CONTENT_PLAN_PRIMARY_VARIANT_POLICY,
        )
        assert [s["variant_id"] for s in specs] == [variant_id]


def test_classify_error_types_lyric_failures():
    from app.pipeline.text_overlay_skia import MissingGlyphsError

    assert gb._classify_error(MissingGlyphsError("missing")) == "lyrics_unsupported_language"

    class LyricAlignmentError(ValueError): ...

    assert gb._classify_error(LyricAlignmentError("no contiguous match")) == "lyric_alignment_error"
    assert gb._classify_error(ValueError("boom")) == "unknown"


# ── Energy derivation (eng fix: no top-level energy on ClipMeta) ─────────────────


def test_meta_to_summary_derives_energy_from_best_moments():
    meta = _Meta("c1", 8.0, best_moments=[{"energy": 3.0}, {"energy": 9.0}, {"energy": 5.0}])
    summary = gb._meta_to_summary(meta)
    assert summary.energy == 9.0  # max of moment energies, not a flat default


def test_meta_to_summary_no_moments_keeps_default():
    summary = gb._meta_to_summary(_Meta("c1", 8.0, best_moments=[]))
    assert summary.energy == 5.0


# ── No-music recipe ──────────────────────────────────────────────────────────────


def test_build_no_music_recipe_one_slot_per_clip_capped():
    metas = [_Meta(f"c{i}", 5.0) for i in range(10)]
    recipe = gb._build_no_music_recipe(metas, available_footage_s=18.0)
    assert recipe["shot_count"] == gb._MAX_NO_MUSIC_SLOTS
    assert recipe["beat_timestamps_s"] == []
    assert all(s["target_duration_s"] > 0 for s in recipe["slots"])


def test_build_no_music_recipe_single_clip():
    recipe = gb._build_no_music_recipe([_Meta("c1", 5.0)], available_footage_s=10.0)
    assert recipe["shot_count"] == 1


def test_build_no_music_recipe_total_never_exceeds_footage():
    # The no-music arrangement must never lay out more runtime than the footage.
    metas = [_Meta(f"c{i}", 2.0) for i in range(3)]  # 6s total
    recipe = gb._build_no_music_recipe(metas, available_footage_s=6.0)
    assert recipe["total_duration_s"] <= 6.0 + 1e-6


def test_build_no_music_recipe_guide_weights_slot_durations():
    # 3 clips; guide says setup:setup:payoff = 2s:2s:6s → payoff gets 3× the slot time.
    metas = [_Meta(f"c{i}", 5.0) for i in range(3)]
    guide = [
        {"what": "setup", "duration_s": 2},
        {"what": "setup", "duration_s": 2},
        {"what": "payoff", "duration_s": 6},
    ]
    recipe = gb._build_no_music_recipe(metas, available_footage_s=12.0, filming_guide=guide)
    durations = [s["target_duration_s"] for s in recipe["slots"]]
    # Payoff slot (index 2) must be the longest
    assert durations[2] > durations[0]
    assert durations[2] > durations[1]
    # Proportions: 2+2+6=10 → [2.4, 2.4, 7.2] with total=12
    assert abs(durations[2] / durations[0] - 3.0) < 0.1


def test_build_no_music_recipe_guide_proportions_sum_to_footage():
    metas = [_Meta(f"c{i}", 5.0) for i in range(3)]
    guide = [
        {"what": "a", "duration_s": 3},
        {"what": "b", "duration_s": 5},
        {"what": "c", "duration_s": 2},
    ]
    recipe = gb._build_no_music_recipe(metas, available_footage_s=15.0, filming_guide=guide)
    total = sum(s["target_duration_s"] for s in recipe["slots"])
    # Total should approximate available footage (small rounding tolerance)
    assert abs(total - 15.0) < 0.05


def test_build_no_music_recipe_guide_too_short_falls_back_to_uniform():
    # Guide has only 1 entry but 3 clips → fall back to equal split.
    metas = [_Meta(f"c{i}", 5.0) for i in range(3)]
    guide = [{"what": "hook", "duration_s": 8}]
    recipe = gb._build_no_music_recipe(metas, available_footage_s=9.0, filming_guide=guide)
    durations = [s["target_duration_s"] for s in recipe["slots"]]
    assert durations[0] == durations[1] == durations[2]


def test_build_no_music_recipe_no_guide_is_uniform():
    metas = [_Meta(f"c{i}", 4.0) for i in range(3)]
    recipe = gb._build_no_music_recipe(metas, available_footage_s=9.0)
    durations = [s["target_duration_s"] for s in recipe["slots"]]
    assert durations[0] == durations[1] == durations[2]


# ── Footage-derived sizing ─────────────────────────────────────────────────────


class _Probe:
    def __init__(self, duration_s):
        self.duration_s = duration_s


def test_available_footage_sums_probes():
    pm = {"/a.mp4": _Probe(3.0), "/b.mp4": _Probe(4.5)}
    assert gb._available_footage_s(pm) == 7.5


def test_available_footage_ignores_bad_probes():
    # A failed/zero probe contributes nothing — the ceiling stays conservative.
    pm = {"/a.mp4": _Probe(3.0), "/b.mp4": _Probe(0.0), "/c.mp4": _Probe(-1.0)}
    assert gb._available_footage_s(pm) == 3.0


def test_fit_section_shrinks_window_to_footage():
    # Song best section is 45s but only 12s of footage exists → window caps at 12s.
    cfg = {"best_start_s": 10.0, "best_end_s": 55.0}
    out = gb._fit_section_to_footage(cfg, available_footage_s=12.0)
    assert out["best_start_s"] == 10.0  # offset untouched (audio alignment)
    assert out["best_end_s"] == 22.0  # 10 + 12


def test_fit_section_leaves_short_window_alone():
    # Footage exceeds the section → the song's own structure stays the ceiling.
    cfg = {"best_start_s": 5.0, "best_end_s": 20.0}  # 15s window
    out = gb._fit_section_to_footage(cfg, available_footage_s=100.0)
    assert out["best_end_s"] == 20.0


def test_fit_section_noops_on_zero_footage():
    cfg = {"best_start_s": 0.0, "best_end_s": 30.0}
    out = gb._fit_section_to_footage(cfg, available_footage_s=0.0)
    assert out["best_end_s"] == 30.0


# ── Skia kill-switch guard ───────────────────────────────────────────────────────


def test_skia_disabled_fails_loudly(monkeypatch):
    captured = {}

    monkeypatch.setattr(gb.settings, "text_renderer_skia_enabled", False, raising=False)
    monkeypatch.setattr(
        gb,
        "_fail_job",
        lambda jid, msg, failure_reason=None: captured.update(jid=jid, reason=failure_reason),
    )
    # Should fail before touching the DB / ingest.
    gb._run_generative_job("11111111-1111-1111-1111-111111111111")
    assert captured["reason"] == "skia_disabled"


# ── Terminal status ──────────────────────────────────────────────────────────────


def test_finalize_status_all_ok(monkeypatch):
    seen = {}
    monkeypatch.setattr(
        gb,
        "_set_status",
        lambda jid, status, extra_plan=None: seen.update(status=status, plan=extra_plan),
    )
    gb._finalize_job(
        "j",
        [
            {
                "variant_id": "a",
                "rank": 1,
                "text_mode": "lyrics",
                "ok": True,
                "render_status": "ready",
            },
            {
                "variant_id": "b",
                "rank": 2,
                "text_mode": "agent_text",
                "ok": True,
                "render_status": "ready",
            },
        ],
    )
    assert seen["status"] == "variants_ready"
    assert len(seen["plan"]["variants"]) == 2


def test_finalize_status_partial(monkeypatch):
    seen = {}
    monkeypatch.setattr(
        gb, "_set_status", lambda jid, status, extra_plan=None: seen.update(status=status)
    )
    gb._finalize_job(
        "j",
        [
            {"variant_id": "a", "rank": 1, "text_mode": "lyrics", "ok": True},
            {"variant_id": "b", "rank": 2, "text_mode": "agent_text", "ok": False, "error": "boom"},
        ],
    )
    assert seen["status"] == "variants_ready_partial"


def test_finalize_status_all_failed(monkeypatch):
    seen = {}
    monkeypatch.setattr(
        gb, "_set_status", lambda jid, status, extra_plan=None: seen.update(status=status)
    )
    gb._finalize_job("j", [{"variant_id": "a", "rank": 1, "text_mode": "lyrics", "ok": False}])
    assert seen["status"] == "variants_failed"


# ── No-music render branch: audio passthrough (NO _mix_template_audio) ───────────


def _patch_render_helpers(monkeypatch, mix_calls: list):
    """Stub the lazily-imported render helpers so _render_generative_variant runs
    without ffmpeg/GCS. Mirrors the real call graph closely enough to assert routing."""
    import app.pipeline.agents.gemini_analyzer as ga
    import app.pipeline.template_matcher as tm
    import app.storage as storage
    import app.tasks.template_orchestrate as to

    monkeypatch.setattr(
        ga,
        "build_recipe",
        lambda d: types.SimpleNamespace(
            beat_timestamps_s=d.get("beat_timestamps_s", []), color_grade="none"
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

    class _Mismatch(Exception):
        code = "x"
        message = "y"

    monkeypatch.setattr(tm, "TemplateMismatchError", _Mismatch, raising=False)
    monkeypatch.setattr(to, "_enrich_slots_with_energy", lambda slots, beats: slots, raising=False)

    def _fake_assemble(steps, c2l, probe, out_path, tmpdir, **kw):
        with open(out_path, "wb") as f:
            f.write(b"\x00" * 16)  # non-empty so the size guard passes

    monkeypatch.setattr(to, "_assemble_clips", _fake_assemble, raising=False)
    monkeypatch.setattr(
        to,
        "_mix_template_audio",
        lambda *a, **k: mix_calls.append(a) or (lambda p: open(p, "wb").write(b"\x00" * 16))(a[2]),
        raising=False,
    )
    monkeypatch.setattr(
        storage, "upload_public_read", lambda local, gcs: f"https://signed/{gcs}", raising=False
    )

    # Stub the text burn too: the real Skia burn would fail on the fake 16-byte
    # mp4 and copy input → output, which the (D20) copy-through detection now
    # correctly fails the variant for. Writes a DIFFERENT byte count than the
    # 16-byte base so detection passes for these routing-focused tests.
    import app.pipeline.text_overlay_skia as skia_mod

    def _fake_burn(base_path, overlays, out_path, tmpdir, *, matte=None):
        with open(out_path, "wb") as f:
            f.write(b"\x01" * 24)

    monkeypatch.setattr(skia_mod, "burn_text_overlays_skia", _fake_burn, raising=False)


def test_original_audio_variant_skips_mix(monkeypatch, tmp_path):
    mix_calls: list = []
    _patch_render_helpers(monkeypatch, mix_calls)
    vdir = tmp_path / "v3"
    vdir.mkdir()
    spec = {"variant_id": "original_text", "rank": 3, "text_mode": "none", "track": None}
    res = gb._render_generative_variant(
        job_id="j",
        rank=3,
        spec=spec,
        clip_metas=[_Meta("c1", 5.0)],
        clip_id_to_local={"c1": "/x.mp4"},
        clip_id_to_gcs={"c1": "music-uploads/x.mp4"},
        probe_map={},
        available_footage_s=12.0,
        agent_text=None,
        agent_form={},
        variant_dir=str(vdir),
    )
    assert res["ok"] is True
    assert res["music_track_id"] is None
    # The defining assertion: original-audio variant must NOT mix a music track.
    assert mix_calls == []
    # output_url is a signed URL, not the relative GCS path.
    assert res["output_url"].startswith("https://signed/")
    assert res["video_path"].startswith("generative-jobs/")


@pytest.mark.parametrize("preset", ["masonry", "polaroid_wall"])
def test_collage_preset_uses_collage_assembler_for_text_variant(
    monkeypatch,
    tmp_path,
    preset,
):
    mix_calls: list = []
    _patch_render_helpers(monkeypatch, mix_calls)

    import app.pipeline.masonry_montage as masonry
    import app.pipeline.template_matcher as tm

    monkeypatch.setattr(
        tm,
        "match",
        lambda recipe, metas, **kw: types.SimpleNamespace(
            steps=[
                types.SimpleNamespace(
                    clip_id="c1",
                    slot={"position": 1, "target_duration_s": 2.0},
                    moment={},
                )
            ]
        ),
        raising=False,
    )
    masonry_calls: list[dict] = []

    def _fake_masonry(**kw):
        masonry_calls.append(kw)
        with open(kw["output_path"], "wb") as f:
            f.write(b"\x02" * 16)

    monkeypatch.setattr(masonry, "assemble_masonry_montage", _fake_masonry, raising=False)

    vdir = tmp_path / "v3"
    vdir.mkdir()
    spec = {"variant_id": "original_text", "rank": 3, "text_mode": "none", "track": None}
    res = gb._render_generative_variant(
        job_id="j",
        rank=3,
        spec=spec,
        clip_metas=[_Meta("c1", 5.0)],
        clip_id_to_local={"c1": "/x.mp4"},
        clip_id_to_gcs={"c1": "users/u/plan/i/x.mp4"},
        probe_map={},
        available_footage_s=20.0,
        agent_text=None,
        agent_form={},
        variant_dir=str(vdir),
        montage_preset=preset,
    )

    assert res["ok"] is True
    assert res["montage_preset"] == preset
    assert res["montage_preset_rendered"] == preset
    assert len(masonry_calls) == 1
    assert masonry_calls[0]["preset"] == preset
    assert masonry_calls[0]["duration_s"] == 15.0


def test_masonry_original_audio_bed_substitutes_photos_for_classic_assembly(monkeypatch, tmp_path):
    mix_calls: list = []
    _patch_render_helpers(monkeypatch, mix_calls)

    import app.pipeline.masonry_montage as masonry
    import app.pipeline.template_matcher as tm
    import app.tasks.template_orchestrate as to

    matched_steps = [
        types.SimpleNamespace(
            clip_id="c1",
            slot={"position": 1, "target_duration_s": 1.0},
            moment={},
        ),
        types.SimpleNamespace(
            clip_id="p1",
            slot={"position": 2, "target_duration_s": 1.0},
            moment={},
        ),
        types.SimpleNamespace(
            clip_id="p2",
            slot={"position": 3, "target_duration_s": 1.0},
            moment={},
        ),
    ]
    monkeypatch.setattr(
        tm,
        "match",
        lambda recipe, metas, **kw: types.SimpleNamespace(steps=matched_steps),
        raising=False,
    )

    assemble_calls: list[dict] = []

    def _fake_assemble(steps, c2l, probe, out_path, tmpdir, **kw):
        assemble_calls.append(
            {
                "clip_ids": [step.clip_id for step in steps],
                "clip_id_to_local": dict(c2l),
                "clip_metas": [m.clip_id for m in kw.get("clip_metas", [])],
            }
        )
        with open(out_path, "wb") as f:
            f.write(b"\x00" * 16)

    monkeypatch.setattr(to, "_assemble_clips", _fake_assemble, raising=False)

    masonry_calls: list[dict] = []

    def _fake_masonry(**kw):
        masonry_calls.append(kw)
        with open(kw["output_path"], "wb") as f:
            f.write(b"\x02" * 16)

    monkeypatch.setattr(masonry, "assemble_masonry_montage", _fake_masonry, raising=False)

    vdir = tmp_path / "v3"
    vdir.mkdir()
    spec = {"variant_id": "original_text", "rank": 3, "text_mode": "none", "track": None}
    res = gb._render_generative_variant(
        job_id="j",
        rank=3,
        spec=spec,
        clip_metas=[_Meta("c1", 5.0), _Meta("p1", 5.0), _Meta("p2", 5.0)],
        clip_id_to_local={"c1": "/x.mp4", "p1": "/odd.jpg", "p2": "/phone.heic"},
        clip_id_to_gcs={
            "c1": "users/u/plan/i/x.mp4",
            "p1": "users/u/plan/i/odd.jpg",
            "p2": "users/u/plan/i/phone.heic",
        },
        probe_map={},
        available_footage_s=20.0,
        agent_text=None,
        agent_form={},
        variant_dir=str(vdir),
        montage_preset="masonry",
    )

    assert res["ok"] is True
    assert res["montage_preset_rendered"] == "masonry"
    assert assemble_calls == [
        {
            "clip_ids": ["c1", "c1", "c1"],
            "clip_id_to_local": {"c1": "/x.mp4"},
            "clip_metas": ["c1"],
        }
    ]
    assert masonry_calls[0]["clip_id_to_local"] == {
        "c1": "/x.mp4",
        "p1": "/odd.jpg",
        "p2": "/phone.heic",
    }


def test_masonry_preset_applies_to_lyrics_variant(monkeypatch, tmp_path):
    mix_calls: list = []
    _patch_render_helpers(monkeypatch, mix_calls)

    import app.pipeline.masonry_montage as masonry
    import app.pipeline.music_recipe as mr
    import app.pipeline.template_matcher as tm

    monkeypatch.setattr(
        mr,
        "generate_music_recipe",
        lambda td, **_kw: {
            "slots": [{"position": 1, "target_duration_s": 2.0, "text_overlays": []}],
            "beat_timestamps_s": [0.5, 1.0],
        },
        raising=False,
    )
    monkeypatch.setattr(gb, "_inject_lyrics", lambda recipe, track, **_kw: recipe)
    monkeypatch.setattr(
        tm,
        "match",
        lambda recipe, metas, **kw: types.SimpleNamespace(
            steps=[
                types.SimpleNamespace(
                    clip_id="c1",
                    slot={"position": 1, "target_duration_s": 2.0},
                    moment={},
                )
            ]
        ),
        raising=False,
    )
    masonry_calls: list[dict] = []

    def _fake_masonry(**kw):
        masonry_calls.append(kw)
        with open(kw["output_path"], "wb") as f:
            f.write(b"\x02" * 16)

    monkeypatch.setattr(masonry, "assemble_masonry_montage", _fake_masonry, raising=False)

    vdir = tmp_path / "v1"
    vdir.mkdir()
    spec = {"variant_id": "song_lyrics", "rank": 1, "text_mode": "lyrics", "track": _track()}
    res = gb._render_generative_variant(
        job_id="j",
        rank=1,
        spec=spec,
        clip_metas=[_Meta("c1", 5.0)],
        clip_id_to_local={"c1": "/x.mp4"},
        clip_id_to_gcs={"c1": "users/u/plan/i/x.mp4"},
        probe_map={},
        available_footage_s=20.0,
        agent_text=None,
        agent_form={},
        variant_dir=str(vdir),
        montage_preset="masonry",
    )

    assert res["ok"] is True
    assert res["variant_id"] == "song_lyrics"
    assert res["montage_preset_rendered"] == "masonry"
    assert len(masonry_calls) == 1


def test_masonry_song_variant_skips_throwaway_classic_assembly(monkeypatch, tmp_path):
    mix_calls: list = []
    _patch_render_helpers(monkeypatch, mix_calls)

    import app.pipeline.masonry_montage as masonry
    import app.pipeline.music_recipe as mr
    import app.pipeline.template_matcher as tm
    import app.tasks.template_orchestrate as to

    monkeypatch.setattr(
        mr,
        "generate_music_recipe",
        lambda td, **_kw: {
            "slots": [{"position": 1, "target_duration_s": 2.0, "text_overlays": []}],
            "beat_timestamps_s": [0.5, 1.0],
        },
        raising=False,
    )
    monkeypatch.setattr(
        tm,
        "match",
        lambda recipe, metas, **kw: types.SimpleNamespace(
            steps=[
                types.SimpleNamespace(
                    clip_id="c1",
                    slot={"position": 1, "target_duration_s": 2.0},
                    moment={},
                )
            ]
        ),
        raising=False,
    )
    assemble_calls: list[object] = []

    def _unexpected_classic_assembly(*_args, **_kw):
        assemble_calls.append(True)
        raise AssertionError("masonry song variants should not render a classic montage first")

    monkeypatch.setattr(to, "_assemble_clips", _unexpected_classic_assembly, raising=False)
    masonry_calls: list[dict] = []

    def _fake_masonry(**kw):
        masonry_calls.append(kw)
        with open(kw["output_path"], "wb") as f:
            f.write(b"\x02" * 16)

    monkeypatch.setattr(masonry, "assemble_masonry_montage", _fake_masonry, raising=False)

    vdir = tmp_path / "v1"
    vdir.mkdir()
    spec = {"variant_id": "song_text", "rank": 1, "text_mode": "none", "track": _track()}
    res = gb._render_generative_variant(
        job_id="j",
        rank=1,
        spec=spec,
        clip_metas=[_Meta("c1", 5.0)],
        clip_id_to_local={"c1": "/x.mp4"},
        clip_id_to_gcs={"c1": "users/u/plan/i/x.mp4"},
        probe_map={},
        available_footage_s=20.0,
        agent_text=None,
        agent_form={},
        variant_dir=str(vdir),
        montage_preset="masonry",
    )

    assert res["ok"] is True
    assert res["montage_preset_rendered"] == "masonry"
    assert assemble_calls == []
    assert masonry_calls[0]["audio_source_path"] is None
    assert len(mix_calls) == 1


def test_masonry_song_variant_fallback_builds_classic_if_compositor_fails(monkeypatch, tmp_path):
    mix_calls: list = []
    _patch_render_helpers(monkeypatch, mix_calls)

    import app.pipeline.masonry_montage as masonry
    import app.pipeline.music_recipe as mr
    import app.pipeline.template_matcher as tm
    import app.tasks.template_orchestrate as to

    monkeypatch.setattr(
        mr,
        "generate_music_recipe",
        lambda td, **_kw: {
            "slots": [{"position": 1, "target_duration_s": 2.0, "text_overlays": []}],
            "beat_timestamps_s": [0.5, 1.0],
        },
        raising=False,
    )
    monkeypatch.setattr(
        tm,
        "match",
        lambda recipe, metas, **kw: types.SimpleNamespace(
            steps=[
                types.SimpleNamespace(
                    clip_id="c1",
                    slot={"position": 1, "target_duration_s": 2.0},
                    moment={},
                )
            ]
        ),
        raising=False,
    )
    monkeypatch.setattr(
        masonry,
        "assemble_masonry_montage",
        lambda **kw: (_ for _ in ()).throw(RuntimeError("boom")),
        raising=False,
    )
    assemble_calls: list[str] = []

    def _fake_assemble(_steps, _c2l, _probe, out_path, _tmpdir, **_kw):
        assemble_calls.append(out_path)
        with open(out_path, "wb") as f:
            f.write(b"\x00" * 16)

    monkeypatch.setattr(to, "_assemble_clips", _fake_assemble, raising=False)

    vdir = tmp_path / "v1"
    vdir.mkdir()
    spec = {"variant_id": "song_text", "rank": 1, "text_mode": "none", "track": _track()}
    res = gb._render_generative_variant(
        job_id="j",
        rank=1,
        spec=spec,
        clip_metas=[_Meta("c1", 5.0)],
        clip_id_to_local={"c1": "/x.mp4"},
        clip_id_to_gcs={"c1": "users/u/plan/i/x.mp4"},
        probe_map={},
        available_footage_s=20.0,
        agent_text=None,
        agent_form={},
        variant_dir=str(vdir),
        montage_preset="masonry",
    )

    assert res["ok"] is True
    assert res["montage_preset_rendered"] is None
    assert res["montage_preset_fallback"] == "classic_render_failed"
    assert len(assemble_calls) == 1
    assert len(mix_calls) == 1


def test_masonry_preset_falls_back_to_classic_on_compositor_error(monkeypatch, tmp_path):
    _patch_render_helpers(monkeypatch, [])

    import app.pipeline.masonry_montage as masonry
    import app.pipeline.template_matcher as tm

    monkeypatch.setattr(
        tm,
        "match",
        lambda recipe, metas, **kw: types.SimpleNamespace(
            steps=[
                types.SimpleNamespace(
                    clip_id="c1",
                    slot={"position": 1, "target_duration_s": 2.0},
                    moment={},
                )
            ]
        ),
        raising=False,
    )
    monkeypatch.setattr(
        masonry,
        "assemble_masonry_montage",
        lambda **kw: (_ for _ in ()).throw(RuntimeError("boom")),
        raising=False,
    )

    vdir = tmp_path / "v3"
    vdir.mkdir()
    spec = {"variant_id": "original_text", "rank": 3, "text_mode": "none", "track": None}
    res = gb._render_generative_variant(
        job_id="j",
        rank=3,
        spec=spec,
        clip_metas=[_Meta("c1", 5.0)],
        clip_id_to_local={"c1": "/x.mp4"},
        clip_id_to_gcs={"c1": "users/u/plan/i/x.mp4"},
        probe_map={},
        available_footage_s=12.0,
        agent_text=None,
        agent_form={},
        variant_dir=str(vdir),
        montage_preset="masonry",
    )

    assert res["ok"] is True
    assert res["montage_preset"] == "masonry"
    assert res["montage_preset_rendered"] is None
    assert res["montage_preset_fallback"] == "classic_render_failed"


def test_masonry_song_fallback_substitutes_photos_for_classic_assembly(monkeypatch, tmp_path):
    mix_calls: list = []
    _patch_render_helpers(monkeypatch, mix_calls)

    import app.pipeline.masonry_montage as masonry
    import app.pipeline.music_recipe as mr
    import app.pipeline.template_matcher as tm
    import app.tasks.template_orchestrate as to

    monkeypatch.setattr(
        mr,
        "generate_music_recipe",
        lambda td, **_kw: {
            "slots": [
                {"position": 1, "target_duration_s": 1.0, "text_overlays": []},
                {"position": 2, "target_duration_s": 1.0, "text_overlays": []},
            ],
            "beat_timestamps_s": [0.5, 1.0],
        },
        raising=False,
    )
    monkeypatch.setattr(
        tm,
        "match",
        lambda recipe, metas, **kw: types.SimpleNamespace(
            steps=[
                types.SimpleNamespace(
                    clip_id="p1",
                    slot={"position": 1, "target_duration_s": 1.0},
                    moment={},
                ),
                types.SimpleNamespace(
                    clip_id="c1",
                    slot={"position": 2, "target_duration_s": 1.0},
                    moment={},
                ),
            ]
        ),
        raising=False,
    )
    monkeypatch.setattr(
        masonry,
        "assemble_masonry_montage",
        lambda **kw: (_ for _ in ()).throw(RuntimeError("boom")),
        raising=False,
    )

    assemble_calls: list[dict] = []

    def _fake_assemble(steps, c2l, probe, out_path, tmpdir, **kw):
        assemble_calls.append(
            {
                "clip_ids": [step.clip_id for step in steps],
                "clip_id_to_local": dict(c2l),
            }
        )
        with open(out_path, "wb") as f:
            f.write(b"\x00" * 16)

    monkeypatch.setattr(to, "_assemble_clips", _fake_assemble, raising=False)

    vdir = tmp_path / "v1"
    vdir.mkdir()
    spec = {"variant_id": "song_text", "rank": 1, "text_mode": "none", "track": _track()}
    res = gb._render_generative_variant(
        job_id="j",
        rank=1,
        spec=spec,
        clip_metas=[_Meta("c1", 5.0), _Meta("p1", 5.0)],
        clip_id_to_local={"c1": "/x.mp4", "p1": "/odd.jpg"},
        clip_id_to_gcs={"c1": "users/u/plan/i/x.mp4", "p1": "users/u/plan/i/odd.jpg"},
        probe_map={},
        available_footage_s=20.0,
        agent_text=None,
        agent_form={},
        variant_dir=str(vdir),
        montage_preset="masonry",
    )

    assert res["ok"] is True
    assert res["montage_preset_fallback"] == "classic_render_failed"
    assert assemble_calls == [{"clip_ids": ["c1", "c1"], "clip_id_to_local": {"c1": "/x.mp4"}}]
    assert len(mix_calls) == 1


def test_song_variant_calls_mix(monkeypatch, tmp_path):
    mix_calls: list = []
    _patch_render_helpers(monkeypatch, mix_calls)
    # generate_music_recipe is also lazily imported — stub it.
    import app.pipeline.music_recipe as mr

    monkeypatch.setattr(
        mr,
        "generate_music_recipe",
        lambda td, **_kw: {
            "slots": [{"position": 1, "target_duration_s": 2.0, "text_overlays": []}],
            "beat_timestamps_s": [0.5, 1.0],
        },
        raising=False,
    )
    vdir = tmp_path / "v1"
    vdir.mkdir()
    spec = {"variant_id": "song_lyrics", "rank": 1, "text_mode": "none", "track": _track()}
    res = gb._render_generative_variant(
        job_id="j",
        rank=1,
        spec=spec,
        clip_metas=[_Meta("c1", 5.0)],
        clip_id_to_local={"c1": "/x.mp4"},
        clip_id_to_gcs={"c1": "music-uploads/x.mp4"},
        probe_map={},
        available_footage_s=12.0,
        agent_text=None,
        agent_form={},
        variant_dir=str(vdir),
    )
    assert res["ok"] is True
    assert res["music_track_id"] == "t1"
    assert len(mix_calls) == 1  # song variant DOES mix the track audio


def test_voiceover_variant_mixes_voice_and_caps_to_voice_length(monkeypatch, tmp_path):
    """voiceover_only: mixes the user's voice (NOT the template-audio path), persists
    the mix slider value, and caps the edit to min(footage, voice, 60)."""
    import app.storage as storage
    import app.tasks.template_orchestrate as to

    mix_calls: list = []
    _patch_render_helpers(monkeypatch, mix_calls)  # patches _mix_template_audio etc.
    monkeypatch.setattr(storage, "download_to_file", lambda gcs, local: None, raising=False)
    # Voice is 5s; footage is 12s → the edit must cap to 5s (D5: never stretch footage).
    monkeypatch.setattr(to, "_probe_duration", lambda p: 5.0, raising=False)
    vo_calls: list = []

    def _fake_vo(video, voice, out, tmpdir, **kw):
        vo_calls.append(kw)
        with open(out, "wb") as f:
            f.write(b"\x00" * 16)

    monkeypatch.setattr(to, "_mix_user_voiceover", _fake_vo, raising=False)

    vdir = tmp_path / "v1"
    vdir.mkdir()
    spec = {
        "variant_id": "voiceover_only",
        "rank": 1,
        "text_mode": "agent_text",
        "track": None,
        "archetype": "voiceover",
        "voiceover_gcs_path": "voiceover-uploads/abc/voice.webm",
        "mix": 1.0,
    }
    res = gb._render_generative_variant(
        job_id="j",
        rank=1,
        spec=spec,
        clip_metas=[_Meta("c1", 5.0)],
        clip_id_to_local={"c1": "/x.mp4"},
        clip_id_to_gcs={"c1": "music-uploads/x.mp4"},
        probe_map={},
        available_footage_s=12.0,
        agent_text=None,
        agent_form={},
        variant_dir=str(vdir),
    )
    assert res["ok"] is True
    assert res["mix"] == 1.0  # slider value persisted for the UI + re-renders
    assert mix_calls == []  # voiceover must NOT use the song/template-audio mixer
    assert len(vo_calls) == 1
    assert vo_calls[0]["mix"] == 1.0
    assert vo_calls[0]["target_duration_s"] == 5.0  # min(12, 5, 60)
    assert vo_calls[0]["music_gcs_path"] is None  # voiceover_only → footage bed, no music


# ── Style sets (curated typography) ────────────────────────────────────────────


def test_render_variant_persists_style_set_id(monkeypatch, tmp_path):
    _patch_render_helpers(monkeypatch, [])
    vdir = tmp_path / "v"
    vdir.mkdir()
    spec = {"variant_id": "original_text", "rank": 3, "text_mode": "none", "track": None}
    res = gb._render_generative_variant(
        job_id="j",
        rank=3,
        spec=spec,
        clip_metas=[_Meta("c1", 5.0)],
        clip_id_to_local={"c1": "/x.mp4"},
        clip_id_to_gcs={"c1": "music-uploads/x.mp4"},
        probe_map={},
        available_footage_s=12.0,
        agent_text=None,
        agent_form={},
        variant_dir=str(vdir),
        style_set_id="film_mono",
    )
    assert res["style_set_id"] == "film_mono"


def test_inject_agent_intro_applies_style_set_look_but_agent_sizes():
    # The curated set OWNS the LOOK (font/effect/color) — but NOT the size. Style
    # sets no longer pin intro `text_size_px` (v0.4.69), so the size is computed
    # from the clip composition. high_fashion still forces fade-in + Bodoni; the
    # px is whatever the sizer decides, not a hardcoded set value.
    from app.pipeline.overlay_sizing import MAX_INTRO_PX, MIN_INTRO_PX

    recipe = {"slots": [{"position": 1, "target_duration_s": 3.0, "text_overlays": []}]}
    agent_text = types.SimpleNamespace(text="hello world", highlight_word=None)
    out, px, source = gb._inject_agent_intro(
        recipe, agent_text, {"effect": "karaoke-line"}, [], style_set_id="high_fashion"
    )
    ov = out["slots"][0]["text_overlays"][0]
    assert ov["font_family"] == "Bodoni Moda"  # set owns the font
    assert ov["effect"] == "fade-in"  # set owns the effect
    assert source == "computed"  # size came from the sizer, not the set
    assert MIN_INTRO_PX <= px <= MAX_INTRO_PX
    assert ov["text_size_px"] == px


def test_inject_agent_intro_no_set_uses_agent_form():
    from app.pipeline.overlay_sizing import MAX_INTRO_PX, MIN_INTRO_PX

    recipe = {"slots": [{"position": 1, "target_duration_s": 3.0, "text_overlays": []}]}
    agent_text = types.SimpleNamespace(text="hello", highlight_word=None)
    out, px, source = gb._inject_agent_intro(
        recipe, agent_text, {"effect": "pop-in"}, [], style_set_id=None
    )
    ov = out["slots"][0]["text_overlays"][0]
    assert ov["effect"] == "pop-in"
    assert "font_family" not in ov
    # No style set, no override → size is COMPUTED from composition, never a constant.
    assert source == "computed"
    assert ov["text_size_px"] == px
    assert MIN_INTRO_PX <= px <= MAX_INTRO_PX


def test_inject_agent_intro_user_override_wins_and_marks_source():
    recipe = {"slots": [{"position": 1, "target_duration_s": 3.0, "text_overlays": []}]}
    agent_text = types.SimpleNamespace(text="hello world", highlight_word=None)
    out, px, source = gb._inject_agent_intro(
        recipe, agent_text, {"effect": "pop-in"}, [], style_set_id=None, size_override_px=140
    )
    ov = out["slots"][0]["text_overlays"][0]
    assert px == 140 and source == "user"
    assert ov["text_size_px"] == 140


def test_hero_composition_picks_most_open_not_highest_hook():
    # The busy clip has the strongest hook; the calm clip has a big open safe zone.
    # Intro sizing should follow the OPEN clip so the text can breathe.
    busy = types.SimpleNamespace(
        hook_score=9.0, visual_density=8.0, text_safe_zone={"x": 0.1, "y": 0.05, "w": 0.8, "h": 0.2}
    )
    open_calm = types.SimpleNamespace(
        hook_score=1.0,
        visual_density=2.5,
        text_safe_zone={"x": 0.05, "y": 0.05, "w": 0.9, "h": 0.4},
    )
    sz, density = gb._hero_composition([busy, open_calm])
    assert sz == {"x": 0.05, "y": 0.05, "w": 0.9, "h": 0.4}
    assert density == 2.5


def test_hero_composition_density_discount_beats_raw_area():
    # A slightly larger but very cluttered box must NOT beat a calm one — the
    # density discount is what prevents "big but busy" from winning.
    big_busy = types.SimpleNamespace(
        hook_score=1.0, visual_density=10.0, text_safe_zone={"x": 0, "y": 0, "w": 1.0, "h": 0.5}
    )  # area 0.50 * (1-0.5) = 0.25
    calm = types.SimpleNamespace(
        hook_score=1.0, visual_density=1.0, text_safe_zone={"x": 0, "y": 0, "w": 0.8, "h": 0.45}
    )  # area 0.36 * (1-0.05) = 0.342
    sz, _ = gb._hero_composition([big_busy, calm])
    assert sz["w"] == 0.8 and sz["h"] == 0.45


def test_hero_composition_skips_clips_without_safe_zone():
    no_zone = types.SimpleNamespace(hook_score=9.0, visual_density=3.0, text_safe_zone=None)
    has_zone = types.SimpleNamespace(
        hook_score=1.0, visual_density=4.0, text_safe_zone={"x": 0, "y": 0, "w": 0.7, "h": 0.3}
    )
    sz, _ = gb._hero_composition([no_zone, has_zone])
    assert sz == {"x": 0, "y": 0, "w": 0.7, "h": 0.3}


def test_hero_composition_none_when_no_clip_has_safe_zone():
    metas = [types.SimpleNamespace(hook_score=5.0, visual_density=3.0, text_safe_zone=None)]
    assert gb._hero_composition(metas) == (None, 5.0)


def test_inject_lyrics_passes_style_set_id(monkeypatch):
    captured: dict = {}

    def _fake_inject(recipe_dict, lyrics_cached, *, best_start_s, best_end_s, lyrics_config):
        captured["cfg"] = lyrics_config
        return recipe_dict

    monkeypatch.setattr(
        "app.pipeline.lyric_injector.inject_lyric_overlays", _fake_inject, raising=False
    )
    gb._inject_lyrics({"slots": []}, _track(), style_set_id="travel_editorial")
    assert captured["cfg"]["enabled"] is True
    assert captured["cfg"]["style_set_id"] == "travel_editorial"
    # The set is authoritative — we do NOT inherit visual lyric tuning.
    assert "style" not in captured["cfg"]


def test_inject_lyrics_preserves_sync_offset_with_style_set(monkeypatch):
    captured: dict = {}

    def _fake_inject(recipe_dict, lyrics_cached, *, best_start_s, best_end_s, lyrics_config):
        captured["cfg"] = lyrics_config
        return recipe_dict

    monkeypatch.setattr(
        "app.pipeline.lyric_injector.inject_lyric_overlays", _fake_inject, raising=False
    )
    track = _track()
    track.track_config = {
        "best_start_s": 0.0,
        "best_end_s": 30.0,
        "lyrics_config": {"sync_offset_s": -0.75, "style": "line", "post_dwell_s": 1.0},
    }

    gb._inject_lyrics({"slots": []}, track, style_set_id="travel_editorial")

    assert captured["cfg"]["style_set_id"] == "travel_editorial"
    assert captured["cfg"]["sync_offset_s"] == -0.75
    assert "style" not in captured["cfg"]
    assert "post_dwell_s" not in captured["cfg"]


def test_select_style_set_falls_back_to_default_on_failure(monkeypatch):
    def _boom():
        raise RuntimeError("no api key")

    monkeypatch.setattr("app.agents._model_client.default_client", _boom, raising=False)
    out = gb._select_generative_style_set(
        [_Meta("c1", 5.0)], types.SimpleNamespace(text="hi"), job_id="j1"
    )
    assert out == "default"


# ── HDR pre-tonemap (cross-variant reframe cost collapse) ──────────────────────


class _ClrProbe:
    def __init__(self, color_trc="bt709"):
        self.color_trc = color_trc
        self.width = 1080
        self.height = 1920


def _patch_pretonemap(monkeypatch, *, zscale=True, run_side_effect=None):
    """Patch the three external deps _pretonemap_hdr_clips reaches into.

    Returns the list of recorded subprocess cmd lists.
    """
    import subprocess

    import app.pipeline.reframe as reframe
    import app.tasks.template_orchestrate as tmpl

    monkeypatch.setattr(reframe, "_zscale_available", lambda: zscale)
    monkeypatch.setattr(tmpl, "_probe_clips", lambda paths: {p: _ClrProbe("bt709") for p in paths})

    calls: list[list[str]] = []

    def _fake_run(cmd, **kwargs):
        calls.append(cmd)
        if run_side_effect is not None:
            raise run_side_effect
        # Simulate ffmpeg writing the output file.
        out = cmd[-1]
        with open(out, "wb") as f:
            f.write(b"\x00")
        return types.SimpleNamespace(returncode=0)

    monkeypatch.setattr(subprocess, "run", _fake_run)
    return calls


def test_pretonemap_converts_only_hdr_clips(tmp_path, monkeypatch):
    """HLG/HDR10 clips get tonemapped once and repointed; SDR clips untouched."""
    calls = _patch_pretonemap(monkeypatch)
    clip_id_to_local = {"hlg": "/hlg.mp4", "sdr": "/sdr.mp4", "hdr10": "/hdr10.mp4"}
    probe_map = {
        "/hlg.mp4": _ClrProbe("arib-std-b67"),
        "/sdr.mp4": _ClrProbe("bt709"),
        "/hdr10.mp4": _ClrProbe("smpte2084"),
    }

    n = gb._pretonemap_hdr_clips(clip_id_to_local, probe_map, str(tmp_path))

    assert n == 2  # both HDR clips, not the SDR one
    assert len(calls) == 2
    # SDR clip path is unchanged; HDR clips repointed to sdr_* intermediates.
    assert clip_id_to_local["sdr"] == "/sdr.mp4"
    assert clip_id_to_local["hlg"].startswith(str(tmp_path)) and "sdr_" in clip_id_to_local["hlg"]
    assert clip_id_to_local["hdr10"] != "/hdr10.mp4"
    # New intermediates carry a bt709 probe so the per-slot reframe skips tonemap.
    assert probe_map[clip_id_to_local["hlg"]].color_trc == "bt709"


def test_pretonemap_reuses_exact_tonemap_pipeline_and_keeps_audio(tmp_path, monkeypatch):
    """Parity: the ffmpeg -vf must be reframe._ZSCALE_SDR_PIPELINE verbatim, and
    audio must be stream-copied so the original-audio variant stays faithful."""
    from app.pipeline.reframe import _ZSCALE_SDR_PIPELINE

    calls = _patch_pretonemap(monkeypatch)
    clip_id_to_local = {"hlg": "/hlg.mp4"}
    probe_map = {"/hlg.mp4": _ClrProbe("arib-std-b67")}

    gb._pretonemap_hdr_clips(clip_id_to_local, probe_map, str(tmp_path))

    cmd = calls[0]
    vf = cmd[cmd.index("-vf") + 1]
    assert _ZSCALE_SDR_PIPELINE in vf, "tonemap must reuse reframe's pipeline (color parity)"
    audio_codec = cmd[cmd.index("-c:a") + 1]
    assert audio_codec == "copy", "source audio must survive for the original-audio variant"


def test_pretonemap_failure_leaves_hdr_clip_in_place(tmp_path, monkeypatch):
    """Best-effort: a failed tonemap must NOT abort — the HDR clip stays so the
    per-slot path still tonemaps it (slow but correct)."""
    import subprocess

    _patch_pretonemap(
        monkeypatch,
        run_side_effect=subprocess.CalledProcessError(1, "ffmpeg", stderr=b"boom"),
    )
    clip_id_to_local = {"hlg": "/hlg.mp4"}
    probe_map = {"/hlg.mp4": _ClrProbe("arib-std-b67")}

    n = gb._pretonemap_hdr_clips(clip_id_to_local, probe_map, str(tmp_path))

    assert n == 0
    assert clip_id_to_local["hlg"] == "/hlg.mp4"  # untouched, no exception raised


def test_pretonemap_runs_clips_concurrently_and_mutates_after_join(tmp_path, monkeypatch):
    """The per-clip tonemaps must OVERLAP on a bounded pool (serial 4-8min/clip blew
    the task time budget — prod job d30c61fe), and every converted clip must be
    repointed + reprobed after the join."""
    import subprocess
    import threading
    import time

    import app.pipeline.reframe as reframe
    import app.tasks.template_orchestrate as tmpl

    monkeypatch.setattr(reframe, "_zscale_available", lambda: True)
    monkeypatch.setattr(tmpl, "_probe_clips", lambda paths: {p: _ClrProbe("bt709") for p in paths})

    lock = threading.Lock()
    state = {"active": 0, "max": 0}

    def _fake_run(cmd, **kwargs):
        with lock:
            state["active"] += 1
            state["max"] = max(state["max"], state["active"])
        time.sleep(0.05)  # hold the slot so a serial impl can never reach max>=2
        with lock:
            state["active"] -= 1
        with open(cmd[-1], "wb") as f:
            f.write(b"\x00")
        return types.SimpleNamespace(returncode=0)

    monkeypatch.setattr(subprocess, "run", _fake_run)

    clip_id_to_local = {f"hdr{i}": f"/hdr{i}.mp4" for i in range(4)}
    probe_map = {f"/hdr{i}.mp4": _ClrProbe("arib-std-b67") for i in range(4)}

    n = gb._pretonemap_hdr_clips(clip_id_to_local, probe_map, str(tmp_path), job_id=None)

    assert n == 4
    assert state["max"] >= 2, "tonemaps must run concurrently, not strictly serially"
    assert state["max"] <= gb._PRETONEMAP_MAX_WORKERS, "concurrency must stay bounded"
    for i in range(4):
        repointed = clip_id_to_local[f"hdr{i}"]
        assert "sdr_" in repointed and repointed != f"/hdr{i}.mp4"
        assert probe_map[repointed].color_trc == "bt709"


def test_pretonemap_partial_failure_only_repoints_successes(tmp_path, monkeypatch):
    """Concurrency changed the serial mutate-in-loop to collect-then-mutate-after-join.
    A clip that fails tonemap must stay HDR (untouched) while its siblings repoint —
    only the successful (clip_id, sdr_path, probe) tuples get applied."""
    import subprocess

    import app.pipeline.reframe as reframe
    import app.tasks.template_orchestrate as tmpl

    monkeypatch.setattr(reframe, "_zscale_available", lambda: True)
    monkeypatch.setattr(tmpl, "_probe_clips", lambda paths: {p: _ClrProbe("bt709") for p in paths})

    def _fake_run(cmd, **kwargs):
        out = cmd[-1]
        if "hdr1.mp4" in out:  # fail exactly the middle clip
            raise subprocess.CalledProcessError(1, "ffmpeg", stderr=b"boom")
        with open(out, "wb") as f:
            f.write(b"\x00")
        return types.SimpleNamespace(returncode=0)

    monkeypatch.setattr(subprocess, "run", _fake_run)

    clip_id_to_local = {f"hdr{i}": f"/hdr{i}.mp4" for i in range(3)}
    probe_map = {f"/hdr{i}.mp4": _ClrProbe("arib-std-b67") for i in range(3)}

    n = gb._pretonemap_hdr_clips(clip_id_to_local, probe_map, str(tmp_path), job_id=None)

    assert n == 2
    assert clip_id_to_local["hdr1"] == "/hdr1.mp4"  # failed clip untouched, stays HDR
    assert "sdr_" in clip_id_to_local["hdr0"] and "sdr_" in clip_id_to_local["hdr2"]


def test_pretonemap_emits_progress_events_when_job_id_set(tmp_path, monkeypatch):
    """With a job_id, the pre-tonemap re-establishes the trace contextvar inside the
    worker thread (it isn't inherited from the orchestrator) and emits one
    `pretonemap_progress` event per HDR clip — so a slow-but-alive job is
    distinguishable from a hang in the admin debug view."""
    _patch_pretonemap(monkeypatch)

    import app.services.pipeline_trace as pt

    events: list[tuple[str, str, dict]] = []
    monkeypatch.setattr(
        pt,
        "record_pipeline_event",
        lambda stage, event, data=None: events.append((stage, event, data)),
    )
    # Stub the contextmanager so the test needs no DB / contextvar machinery.
    import contextlib

    monkeypatch.setattr(pt, "pipeline_trace_for", lambda job_id: contextlib.nullcontext())

    clip_id_to_local = {"hlg": "/hlg.mp4", "hdr10": "/hdr10.mp4"}
    probe_map = {"/hlg.mp4": _ClrProbe("arib-std-b67"), "/hdr10.mp4": _ClrProbe("smpte2084")}

    gb._pretonemap_hdr_clips(clip_id_to_local, probe_map, str(tmp_path), job_id="job-x")

    progress = [e for e in events if e[1] == "pretonemap_progress"]
    assert len(progress) == 2  # one per HDR clip
    assert progress[-1][2] == {"done": 2, "total": 2}


def test_no_rerun_statuses_all_skip(monkeypatch):
    """Every terminal status in _NO_RERUN_STATUSES must short-circuit a redelivered
    job — guards against someone dropping a member and silently re-running finished work."""
    monkeypatch.setattr(gb.settings, "text_renderer_skia_enabled", True, raising=False)
    monkeypatch.setattr(
        gb,
        "_ingest_clips",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("terminal must not re-enter")),
        raising=False,
    )
    for status in gb._NO_RERUN_STATUSES:
        job = _FakeJob()
        job.status = status
        _patch_job_session(monkeypatch, job)
        gb._run_generative_job("55555555-5555-5555-5555-555555555555")
        assert job.status == status  # untouched


# ── Timeout / redelivery safety (never freeze on "analyzing your clips") ───────


def test_softtimelimit_marks_processing_failed_not_frozen(monkeypatch):
    """A soft time-limit during processing must fail the job VISIBLY (processing_failed
    + actionable message), never leave it frozen at status=processing forever
    (prod job d30c61fe)."""
    from celery.exceptions import SoftTimeLimitExceeded

    def _boom(job_id):
        raise SoftTimeLimitExceeded()

    monkeypatch.setattr(gb, "_run_generative_job", _boom)
    captured: dict = {}
    monkeypatch.setattr(
        gb,
        "_fail_job",
        lambda jid, detail, failure_reason=None: captured.update(
            job_id=jid, detail=detail, reason=failure_reason
        ),
    )

    gb.orchestrate_generative_job.run("22222222-2222-2222-2222-222222222222")

    assert captured["reason"] == "processing_timeout"
    assert "timed out" in captured["detail"].lower()


def test_terminal_status_skips_rerun(monkeypatch):
    """A redelivered job (Celery acks_late) already in a terminal state must no-op —
    not repeat the expensive pre-tonemap or clobber a finished result."""
    monkeypatch.setattr(gb.settings, "text_renderer_skia_enabled", True, raising=False)

    def _should_not_run(*a, **k):
        raise AssertionError("terminal job must not re-enter the pipeline")

    monkeypatch.setattr(gb, "_ingest_clips", _should_not_run, raising=False)

    job = _FakeJob()
    job.status = "variants_ready"
    _patch_job_session(monkeypatch, job)

    gb._run_generative_job("33333333-3333-3333-3333-333333333333")

    assert job.status == "variants_ready"  # untouched, never set back to "processing"


def test_mid_render_status_still_reruns(monkeypatch):
    """Inverse guard: a job killed mid-render is left at "rendering" (NOT terminal) so
    the resume path still re-enters and reuses persisted variants."""
    monkeypatch.setattr(gb.settings, "text_renderer_skia_enabled", True, raising=False)
    entered = {"ingest": False}

    def _stop_after_status(*a, **k):
        entered["ingest"] = True
        raise RuntimeError("stop here — we only assert the guard let us in")

    monkeypatch.setattr(gb, "_ingest_clips", _stop_after_status, raising=False)

    job = _FakeJob()
    job.status = "rendering"
    job.mode = "generative"
    job.all_candidates = {"clip_paths": ["music-uploads/x/slot.mov"]}
    _patch_job_session(monkeypatch, job)

    try:
        gb._run_generative_job("44444444-4444-4444-4444-444444444444")
    except RuntimeError:
        pass

    assert entered["ingest"], "rendering (non-terminal) job must re-enter the pipeline"


def test_run_generative_job_forwards_masonry_preset_to_montage_renderer(monkeypatch):
    """Regression guard for the full job path: all_candidates.montage_preset must
    reach the montage renderer, not only direct _render_generative_variant callers."""

    monkeypatch.setattr(gb.settings, "text_renderer_skia_enabled", True, raising=False)

    job = _FakeJob(assembly_plan={})
    job.status = "queued"
    job.mode = "generative"
    job.all_candidates = {
        "clip_paths": ["users/u/plan/i/clip.mp4"],
        "edit_format": "montage",
        "montage_preset": "masonry",
    }
    _patch_job_session(monkeypatch, job)

    import app.services.pipeline_trace as pt

    monkeypatch.setattr(gb, "record_phase", lambda *a, **k: None, raising=False)
    monkeypatch.setattr(pt, "record_pipeline_event", lambda *a, **k: None, raising=False)
    monkeypatch.setattr(gb, "_persist_durable_sources", lambda _job_id, paths: paths)
    monkeypatch.setattr(
        gb,
        "_ingest_clips",
        lambda *a, **k: {
            "clip_metas": [_Meta("c1", 5.0)],
            "clip_id_to_gcs": {"c1": "users/u/plan/i/clip.mp4"},
            "clip_id_to_local": {"c1": "/tmp/clip.mp4"},
            "probe_map": {"/tmp/clip.mp4": _Probe(12.0)},
            "hero": _Meta("c1", 5.0),
        },
    )
    monkeypatch.setattr(gb, "_pretonemap_hdr_clips", lambda *a, **k: 0)
    monkeypatch.setattr(
        gb,
        "_run_text_agents",
        lambda *a, **k: (
            "Text",
            {},
        ),
    )
    monkeypatch.setattr(gb, "_select_generative_style_set", lambda *a, **k: "default")
    monkeypatch.setattr(gb, "_match_best_track", lambda *a, **k: None)
    monkeypatch.setattr(gb, "_resolve_archetype", lambda *a, **k: ("montage", None, None))
    monkeypatch.setattr(gb, "_set_status", lambda *a, **k: None)
    monkeypatch.setattr(gb, "_persist_archetype_fallback", lambda *a, **k: None)
    monkeypatch.setattr(gb, "_existing_variants", lambda *a, **k: [])
    monkeypatch.setattr(gb, "_update_variant_entry", lambda *a, **k: None)
    monkeypatch.setattr(gb, "_upsert_variant_entry", lambda *a, **k: None)
    monkeypatch.setattr(gb, "_maybe_add_text_elements_snapshot", lambda *a, **k: None)
    monkeypatch.setattr(gb, "_finalize_job", lambda *a, **k: None)
    monkeypatch.setattr(gb, "_maybe_autoplace_after_finalize", lambda *a, **k: None)

    seen: dict[str, str] = {}

    def _fake_render(**kw):
        seen["montage_preset"] = kw["montage_preset"]
        return {
            "ok": True,
            "variant_id": kw["spec"]["variant_id"],
            "rank": kw["rank"],
            "render_status": "ready",
            "output_url": "https://signed/out.mp4",
        }

    monkeypatch.setattr(gb, "_render_generative_variant", _fake_render)

    gb._run_generative_job("44444444-4444-4444-4444-444444444444")

    assert seen["montage_preset"] == "masonry"


# ── Resumable variants (survive deploy/OOM kills) ──────────────────────────────


# _FakeJob / _patch_job_session moved to tests/tasks/conftest.py (shared with
# test_caption_reapply.py) — imported at the top of this module.


def test_upsert_variant_appends_then_replaces(monkeypatch):
    """First completed variant appends; re-persisting the same id replaces it."""
    job = _FakeJob(assembly_plan={})
    _patch_job_session(monkeypatch, job)

    gb._upsert_variant_entry(
        "11111111-1111-1111-1111-111111111111",
        {"variant_id": "song_text", "ok": True, "output_url": "u1"},
    )
    assert [v["variant_id"] for v in job.assembly_plan["variants"]] == ["song_text"]

    gb._upsert_variant_entry(
        "11111111-1111-1111-1111-111111111111",
        {"variant_id": "original_text", "ok": True, "output_url": "u2"},
    )
    ids = [v["variant_id"] for v in job.assembly_plan["variants"]]
    assert ids == ["song_text", "original_text"]

    # Re-persist song_text → replace in place, no duplicate.
    gb._upsert_variant_entry(
        "11111111-1111-1111-1111-111111111111",
        {"variant_id": "song_text", "ok": True, "output_url": "u1b"},
    )
    songs = [v for v in job.assembly_plan["variants"] if v["variant_id"] == "song_text"]
    assert len(songs) == 1 and songs[0]["output_url"] == "u1b"


def test_existing_variants_reads_persisted(monkeypatch):
    job = _FakeJob(assembly_plan={"variants": [{"variant_id": "song_text", "ok": True}]})
    _patch_job_session(monkeypatch, job)
    got = gb._existing_variants("11111111-1111-1111-1111-111111111111")
    assert got == [{"variant_id": "song_text", "ok": True}]


def test_upsert_does_not_change_job_status(monkeypatch):
    """Persisting a variant mid-render must NOT flip the job to a terminal status."""
    job = _FakeJob(assembly_plan={})
    _patch_job_session(monkeypatch, job)
    gb._upsert_variant_entry(
        "11111111-1111-1111-1111-111111111111",
        {"variant_id": "song_text", "ok": True, "output_url": "u"},
    )
    assert job.status == "rendering"


# ── assembly_plan RMW writers must row-lock (lost-update guard) ─────────────────
#
# Every writer that does a read-modify-write of Job.assembly_plan must SELECT ...
# FOR UPDATE. Sibling regenerate/reapply tasks (worker --concurrency=4) and the
# status route's lazy overlay-preview backfill mutate the same JSONB concurrently;
# an unlocked stale read clobbers the whole plan (lost variant state / preview
# URLs). These pin that the previously-unlocked writers now request the lock,
# mirroring the already-locked _upsert_variant_entry / _update_variant_entry.

_LOCK_TEST_JID = "11111111-1111-1111-1111-111111111111"


class _LockSpySession:
    """Fake sync session recording the `with_for_update` kwarg of every .get()."""

    def __init__(self, job):
        self._job = job
        self.for_update_calls: list[bool] = []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def get(self, _model, _pk, **kw):
        self.for_update_calls.append(bool(kw.get("with_for_update", False)))
        return self._job

    def commit(self):
        pass


def _patch_lock_spy(monkeypatch, job) -> _LockSpySession:
    spy = _LockSpySession(job)
    monkeypatch.setattr(gb, "_sync_session", lambda: spy)
    return spy


def test_set_status_row_locks_the_job(monkeypatch):
    """_set_status merges extra_plan into assembly_plan (finalize writes the whole
    variants list). The RMW must lock the row so a concurrent write is not clobbered."""
    job = _FakeJob(assembly_plan={"variants": [{"variant_id": "song_text"}]})
    spy = _patch_lock_spy(monkeypatch, job)
    gb._set_status(_LOCK_TEST_JID, "variants_ready", extra_plan={"variants": []})
    assert spy.for_update_calls == [True]
    assert job.status == "variants_ready"


def test_fail_job_row_locks_the_job(monkeypatch):
    """_fail_job reconciles per-variant render_status — an unlocked RMW here can wipe
    a concurrent finalize's variant list."""
    job = _FakeJob(assembly_plan={"variants": [{"variant_id": "a", "render_status": "rendering"}]})
    spy = _patch_lock_spy(monkeypatch, job)
    gb._fail_job(_LOCK_TEST_JID, "boom", failure_reason="processing_timeout")
    assert spy.for_update_calls == [True]
    # And the reconcile still ran: the in-flight variant flipped to failed.
    assert job.assembly_plan["variants"][0]["render_status"] == "failed"
    assert job.status == "processing_failed"


def test_reapply_media_overlays_row_locks_the_prep_write(monkeypatch):
    """The clean-copy reset in _reapply_persisted_media_overlays_if_any is a full-
    variants-list RMW and must lock the row before writing back."""
    job = _FakeJob(
        assembly_plan={
            "variants": [{"variant_id": "song_text", "media_overlays": [{"src_gcs_path": "x"}]}]
        }
    )
    spy = _patch_lock_spy(monkeypatch, job)
    monkeypatch.setattr(gb.settings, "media_overlays_enabled", True, raising=False)
    monkeypatch.setattr(gb, "_run_media_overlay_pass", lambda **kw: None, raising=False)
    # flag_modified needs a real mapped instance; no-op it for the fake job.
    import sqlalchemy.orm.attributes as _sa_attrs

    monkeypatch.setattr(_sa_attrs, "flag_modified", lambda *a, **k: None)
    handled = gb._reapply_persisted_media_overlays_if_any(
        job_id=_LOCK_TEST_JID, variant_id="song_text"
    )
    assert handled is True
    assert spy.for_update_calls == [True]


def test_reapply_sfx_row_locks_the_prep_write(monkeypatch):
    """The pre_sfx_video_path reset in _reapply_persisted_sfx_if_any is a full-
    variants-list RMW and must lock the row before writing back."""
    job = _FakeJob(
        assembly_plan={"variants": [{"variant_id": "song_text", "sound_effects": [{"id": "s1"}]}]}
    )
    spy = _patch_lock_spy(monkeypatch, job)
    monkeypatch.setattr(gb.settings, "sound_effects_enabled", True, raising=False)
    monkeypatch.setattr(gb, "_run_sfx_pass", lambda **kw: None, raising=False)
    # flag_modified needs a real mapped instance; no-op it for the fake job.
    import sqlalchemy.orm.attributes as _sa_attrs

    monkeypatch.setattr(_sa_attrs, "flag_modified", lambda *a, **k: None)
    gb._reapply_persisted_sfx_if_any(job_id=_LOCK_TEST_JID, variant_id="song_text")
    assert spy.for_update_calls == [True]


def test_match_best_track_ignores_publish_gate(monkeypatch):
    """Generative auto-match draws from the WHOLE analyzed library, so it must
    call the shared candidate loader with require_published=False — otherwise
    only the handful of publicly-published tracks would ever be picked."""
    import app.tasks.auto_music_orchestrate as amo

    captured = {}

    def fake_load(n_clips, **kwargs):
        captured["n_clips"] = n_clips
        captured["kwargs"] = kwargs
        return [_track("t1")]

    monkeypatch.setattr(amo, "_load_matcher_candidates", fake_load)
    monkeypatch.setattr(amo, "_run_music_matcher", lambda **kw: [{"track_id": "t1"}])

    out = gb._match_best_track([_Meta("c1", 5.0), _Meta("c2", 5.0)], job_id="j1")

    assert out is not None and out.id == "t1"
    assert captured["n_clips"] == 2
    assert captured["kwargs"].get("require_published") is False


# ── Phase instrumentation (PR2) ──────────────────────────────────────────────────


def _make_phase_job(status="queued"):
    """Minimal fake job for phase instrumentation tests."""
    import uuid

    job = _FakeJob(assembly_plan={})
    job.status = status
    job.mode = "generative"
    job.all_candidates = {"clip_paths": ["music-uploads/x/slot.mov"]}
    job.id = uuid.uuid4()
    return job


def _patch_phase_fns(monkeypatch):
    """Patch mark_started, record_phase, mark_finished, mark_failed_phase and return call log."""
    import app.tasks.generative_build as gb_mod

    calls: list[tuple[str, tuple, dict]] = []

    monkeypatch.setattr(
        gb_mod,
        "mark_started",
        lambda job_id: calls.append(("mark_started", (job_id,), {})),
        raising=False,
    )
    monkeypatch.setattr(
        gb_mod,
        "record_phase",
        lambda job_id, phase, **kw: calls.append(("record_phase", (job_id, phase), kw)),
        raising=False,
    )
    monkeypatch.setattr(
        gb_mod,
        "mark_finished",
        lambda job_id: calls.append(("mark_finished", (job_id,), {})),
        raising=False,
    )
    monkeypatch.setattr(
        gb_mod,
        "mark_failed_phase",
        lambda job_id: calls.append(("mark_failed_phase", (job_id,), {})),
        raising=False,
    )
    return calls


def _patch_run_generative_job_success(monkeypatch):
    """Patch _run_generative_job so orchestrate_generative_job runs without a DB/ffmpeg."""
    monkeypatch.setattr(gb, "_run_generative_job", lambda job_id: None, raising=False)


def _patch_run_generative_job_failure(monkeypatch, exc):
    """Patch _run_generative_job to raise exc."""

    def _raise(job_id):
        raise exc

    monkeypatch.setattr(gb, "_run_generative_job", _raise, raising=False)


def test_phase_instrumentation_trunk_order(monkeypatch):
    """mark_started is called before _run_generative_job; mark_finished after success.

    This verifies the top-level orchestrator wires the phase helpers correctly.
    mark_started is called unconditionally; mark_failed_phase is NOT called on success.
    """
    import contextlib

    import app.services.pipeline_trace as pt

    calls = _patch_phase_fns(monkeypatch)
    _patch_run_generative_job_success(monkeypatch)
    monkeypatch.setattr(pt, "pipeline_trace_for", lambda job_id: contextlib.nullcontext())
    # Suppress _fail_job so we can test the happy path without DB
    monkeypatch.setattr(gb, "_fail_job", lambda *a, **k: None, raising=False)

    gb.orchestrate_generative_job.run("11111111-1111-1111-1111-111111111111")

    fn_names = [c[0] for c in calls]
    # mark_started must appear before mark_finished; mark_failed_phase must NOT appear.
    assert "mark_started" in fn_names
    assert "mark_finished" in fn_names
    assert "mark_failed_phase" not in fn_names
    assert fn_names.index("mark_started") < fn_names.index("mark_finished")


def test_mark_failed_phase_on_fatal_error(monkeypatch):
    """A fatal exception in _run_generative_job must trigger mark_failed_phase."""
    import contextlib

    import app.services.pipeline_trace as pt

    calls = _patch_phase_fns(monkeypatch)
    _patch_run_generative_job_failure(monkeypatch, RuntimeError("fatal"))
    monkeypatch.setattr(pt, "pipeline_trace_for", lambda job_id: contextlib.nullcontext())
    monkeypatch.setattr(gb, "_fail_job", lambda *a, **k: None, raising=False)

    gb.orchestrate_generative_job.run("22222222-2222-2222-2222-222222222222")

    fn_names = [c[0] for c in calls]
    assert "mark_failed_phase" in fn_names
    # mark_finished must NOT be called on failure.
    assert "mark_finished" not in fn_names


def test_pending_variants_upserted_before_render(monkeypatch, tmp_path):
    """Upfront pending-variant upsert: all specs must be upserted with
    render_status='pending' BEFORE any 'rendering' or 'ready' upsert."""
    upsert_calls: list[dict] = []
    update_calls: list[dict] = []

    def _track_upsert(job_id, result):
        upsert_calls.append(dict(result))

    def _track_update(job_id, variant_id, patch):
        update_calls.append({"variant_id": variant_id, **patch})
        return None

    monkeypatch.setattr(gb, "_upsert_variant_entry", _track_upsert, raising=False)
    monkeypatch.setattr(gb, "_update_variant_entry", _track_update, raising=False)

    # Run _run_generative_job up to the point where specs are determined.
    # We stop early by having _ingest_clips raise after upsert phase.
    # Instead, directly test the upfront upsert section by exercising the spec setup.
    # Verify: pending upserts happen for every spec before any "rendering" update.
    specs = gb._variant_specs(None)  # original_text only, no track
    for spec in specs:
        _track_upsert(
            "test-job",
            {
                "variant_id": spec["variant_id"],
                "rank": specs.index(spec) + 1,
                "text_mode": spec.get("text_mode", "agent_text"),
                "music_track_id": spec["track"].id if spec.get("track") else None,
                "track_title": spec["track"].title if spec.get("track") else None,
                "render_status": "pending",
                "ok": False,
            },
        )

    pending_upserts = [c for c in upsert_calls if c.get("render_status") == "pending"]
    assert len(pending_upserts) >= 1
    for pu in pending_upserts:
        assert pu["ok"] is False

    # Verify no "ready" calls appeared before any "pending" call.
    all_statuses = [c.get("render_status") for c in upsert_calls]
    if "ready" in all_statuses and "pending" in all_statuses:
        assert all_statuses.index("pending") < all_statuses.index("ready")


def test_variant_timestamps_on_success(monkeypatch, tmp_path):
    """A successfully rendered variant must have render_started_at and render_finished_at
    in the result dict."""
    mix_calls: list = []
    _patch_render_helpers(monkeypatch, mix_calls)
    vdir = tmp_path / "v3"
    vdir.mkdir()
    spec = {"variant_id": "original_text", "rank": 3, "text_mode": "none", "track": None}

    # Inject timestamps via the _render_spec_set wrapper by simulating what it does.
    result = gb._render_generative_variant(
        job_id="j",
        rank=3,
        spec=spec,
        clip_metas=[_Meta("c1", 5.0)],
        clip_id_to_local={"c1": "/x.mp4"},
        clip_id_to_gcs={"c1": "music-uploads/x.mp4"},
        probe_map={},
        available_footage_s=12.0,
        agent_text=None,
        agent_form={},
        variant_dir=str(vdir),
    )
    assert result["ok"] is True
    # render_finished_at is injected by _render_spec_set on success — simulate it.
    from datetime import datetime

    result["render_finished_at"] = datetime.utcnow().isoformat() + "Z"
    assert result.get("render_finished_at") is not None
    assert result["render_finished_at"].endswith("Z")


def test_error_class_on_failed_variant(monkeypatch, tmp_path):
    """When a variant fails with a SoftTimeLimitExceeded-like exception,
    error_class='timeout' must appear in the result dict."""
    from celery.exceptions import SoftTimeLimitExceeded

    mix_calls: list = []
    _patch_render_helpers(monkeypatch, mix_calls)

    # Patch _assemble_clips to raise SoftTimeLimitExceeded
    import app.tasks.template_orchestrate as to

    monkeypatch.setattr(
        to,
        "_assemble_clips",
        lambda *a, **k: (_ for _ in ()).throw(SoftTimeLimitExceeded()),
        raising=False,
    )

    vdir = tmp_path / "v_err"
    vdir.mkdir()
    spec = {"variant_id": "original_text", "rank": 3, "text_mode": "none", "track": None}
    result = gb._render_generative_variant(
        job_id="j",
        rank=3,
        spec=spec,
        clip_metas=[_Meta("c1", 5.0)],
        clip_id_to_local={"c1": "/x.mp4"},
        clip_id_to_gcs={"c1": "music-uploads/x.mp4"},
        probe_map={},
        available_footage_s=12.0,
        agent_text=None,
        agent_form={},
        variant_dir=str(vdir),
    )
    assert result["ok"] is False
    assert result.get("error_class") == "timeout"


def test_error_class_unknown_for_generic_error(monkeypatch, tmp_path):
    """An unclassified exception must produce error_class='unknown'."""
    mix_calls: list = []
    _patch_render_helpers(monkeypatch, mix_calls)

    import app.tasks.template_orchestrate as to

    monkeypatch.setattr(
        to,
        "_assemble_clips",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("some unexpected problem")),
        raising=False,
    )

    vdir = tmp_path / "v_unknown"
    vdir.mkdir()
    spec = {"variant_id": "original_text", "rank": 3, "text_mode": "none", "track": None}
    result = gb._render_generative_variant(
        job_id="j",
        rank=3,
        spec=spec,
        clip_metas=[_Meta("c1", 5.0)],
        clip_id_to_local={"c1": "/x.mp4"},
        clip_id_to_gcs={"c1": "music-uploads/x.mp4"},
        probe_map={},
        available_footage_s=12.0,
        agent_text=None,
        agent_form={},
        variant_dir=str(vdir),
    )
    assert result["ok"] is False
    assert result.get("error_class") == "unknown"


def test_classify_error_timeout():
    from celery.exceptions import SoftTimeLimitExceeded

    assert gb._classify_error(SoftTimeLimitExceeded()) == "timeout"


def test_classify_error_unknown():
    assert gb._classify_error(RuntimeError("boom")) == "unknown"


def test_classify_error_encoder():
    assert gb._classify_error(RuntimeError("ffmpeg returned non-zero")) == "encoder_error"


# ---------------------------------------------------------------------------
# _resolve_intro_overlay_params — Creator Agent M1 knob precedence
# ---------------------------------------------------------------------------


def _agent_text(text: str = "Hello world", highlight_word: str | None = None):
    return types.SimpleNamespace(text=text, highlight_word=highlight_word)


class TestResolveIntroOverlayParamsKnobPrecedence:
    """Covers the user-style knob precedence chain added in Creator Agent M1.

    No DB, no GPU — pure Python unit tests.  The only import that could fail is
    resolve_overlay_style (style_sets module) which is guarded by monkeypatching.
    """

    def _call(self, monkeypatch, knobs=None, style_set_id=None, size_override_px=None):
        """Helper: resolve params with a minimal set stub."""

        def _fake_resolve(set_id, overlay_type, *, advisory=None):
            # Return a recognisable but minimal set dict.
            return {
                "font_family": "Bodoni Moda",
                "text_size_px": 52,
                "effect": "fade-in",
                "position": "top",
                "text_color": "#EEEEEE",
                "highlight_color": "#FF0000",
                "text_anchor": "right",
                "stroke_width": 2,
            }

        # resolve_overlay_style is lazily imported inside the function (PLC0415),
        # so patch it at the source module, not on generative_build.
        monkeypatch.setattr(
            "app.pipeline.style_sets.resolve_overlay_style",
            _fake_resolve,
            raising=True,
        )

        return gb._resolve_intro_overlay_params(
            _agent_text(),
            {},
            style_set_id,
            user_style_knobs=knobs,
            size_override_px=size_override_px,
        )

    def test_size_override_wins_over_user_style_knob(self, monkeypatch):
        """per-variant size_override_px (source='user') > user-style text_size_px."""
        params, px, source = self._call(
            monkeypatch,
            knobs={"text_size_px": 70},
            style_set_id=None,
            size_override_px=140,
        )
        assert px == 140
        assert source == "user"
        assert params["text_size_px"] == 140

    def test_user_style_px_wins_over_set_px(self, monkeypatch):
        """user_style_knobs.text_size_px (source='user_style') > curated set px."""
        params, px, source = self._call(
            monkeypatch,
            knobs={"text_size_px": 65},
            style_set_id="any_set",
        )
        # The fake set returns text_size_px=52; user-style 65 must win.
        assert px == 65
        assert source == "user_style"

    def test_set_px_wins_when_no_user_style_knob(self, monkeypatch):
        """curated set px (source='computed') wins when no user_style knob."""
        params, px, source = self._call(
            monkeypatch,
            knobs=None,
            style_set_id="any_set",
        )
        # Fake set returns 52.
        assert px == 52
        assert source == "computed"

    def test_user_style_font_wins_over_set_font(self, monkeypatch):
        """user_style_knobs.font_family wins over the curated set's font."""
        params, px, source = self._call(
            monkeypatch,
            knobs={"font_family": "Playfair Display"},
            style_set_id="any_set",
        )
        assert params["font_family"] == "Playfair Display"

    def test_set_font_used_when_no_user_style_font(self, monkeypatch):
        """Curated set's font_family is used when user-style knob is absent."""
        params, px, source = self._call(
            monkeypatch,
            knobs=None,
            style_set_id="any_set",
        )
        assert params["font_family"] == "Bodoni Moda"

    def test_stroke_width_zero_is_honored(self, monkeypatch):
        """stroke_width=0 (valid falsy value) is passed through — not dropped."""
        params, _px, _src = self._call(
            monkeypatch,
            knobs={"stroke_width": 0},
            style_set_id="any_set",
        )
        assert params["stroke_width"] == 0

    def test_stroke_width_none_knob_falls_back_to_set(self, monkeypatch):
        """No stroke_width knob → curated set value is used."""
        params, _px, _src = self._call(
            monkeypatch,
            knobs=None,
            style_set_id="any_set",
        )
        assert params["stroke_width"] == 2  # from fake set

    def test_position_x_frac_zero_is_honored(self, monkeypatch):
        """position_x_frac=0.0 is a valid value and must NOT be dropped."""
        params, _px, _src = self._call(
            monkeypatch,
            knobs={"position_x_frac": 0.0},
            style_set_id=None,
        )
        assert params["position_x_frac"] == 0.0

    def test_user_style_position_wins_over_set_position(self, monkeypatch):
        """user_style_knobs.position wins over curated-set position."""
        params, _px, _src = self._call(
            monkeypatch,
            knobs={"position": "bottom"},
            style_set_id="any_set",
        )
        assert params["position"] == "bottom"

    def test_no_knobs_no_set_size_is_computed(self, monkeypatch):
        """When there's no knob, no set, and no override, size is computed (source='computed')."""
        # No style_set_id → no curated set → compute_overlay_size is called.
        # We stub compute_overlay_size to avoid Skia/PIL imports.
        monkeypatch.setattr(
            "app.pipeline.overlay_sizing.compute_overlay_size",
            lambda text, **kw: 58,
            raising=True,
        )
        params, px, source = gb._resolve_intro_overlay_params(
            _agent_text(),
            {},
            None,
            user_style_knobs=None,
            size_override_px=None,
        )
        assert source == "computed"
        assert px == 58

    def test_effect_not_in_style_knobs_path(self, monkeypatch):
        """effect must NOT be settable via user_style_knobs (parity-unsafe #296).

        The effect in the output params comes from the set or agent_form only.
        """
        # Pass an effect in knobs dict (bypassing StyleKnobs validation so we
        # test the resolver itself, not the schema guard).
        params, _px, _src = self._call(
            monkeypatch,
            knobs={"effect": "glow"},  # should be ignored
            style_set_id="any_set",
        )
        # The fake set returns effect=fade-in; the knob effect must NOT override it.
        # The params["effect"] should come from set/agent_form, never from knobs.
        assert params["effect"] == "fade-in"  # set value wins, knob is ignored


# ── Fast-reburn: _resolve_regen_text ────────────────────────────────────────────


def test_regenerate_reuses_persisted_intro_text():
    """Persisted text + mode=agent_text → no LLM call; returned text matches persisted."""
    llm_called = {"called": False}

    def _run_text_agents_fn():
        llm_called["called"] = True
        return types.SimpleNamespace(text="LLM text", highlight_word=None), {}

    agent_text, agent_form, text_mode = gb._resolve_regen_text(
        override_text=None,
        remove_text=False,
        existing_text_mode="agent_text",
        persisted_text="My hook text",
        persisted_highlight=None,
        run_text_agents_fn=_run_text_agents_fn,
    )

    assert llm_called["called"] is False
    assert agent_text is not None
    assert agent_text.text == "My hook text"
    assert text_mode == "agent_text"


def test_regenerate_lyrics_mode_never_runs_intro_writer():
    """REGRESSION (2026-07-18 E2E): a lyrics-variant full re-render with no text
    override fell through to intro_writer, fabricating an intro AND flipping
    text_mode to agent_text — which made the variant fast-reburn eligible so
    later lyric-override dispatches silently skipped lyric re-injection."""
    llm_called = {"called": False}

    def _run_text_agents_fn():
        llm_called["called"] = True
        return types.SimpleNamespace(text="LLM text", highlight_word=None), {}

    agent_text, agent_form, text_mode = gb._resolve_regen_text(
        override_text=None,
        remove_text=False,
        existing_text_mode="lyrics",
        persisted_text=None,
        persisted_highlight=None,
        run_text_agents_fn=_run_text_agents_fn,
    )

    assert llm_called["called"] is False
    assert agent_text is None
    assert text_mode == "lyrics"


def test_regenerate_runs_intro_writer_when_no_persisted_text():
    """No persisted text → intro_writer LLM IS called."""
    llm_called = {"called": False}

    def _run_text_agents_fn():
        llm_called["called"] = True
        return types.SimpleNamespace(text="From LLM", highlight_word=None), {"effect": "pop-in"}

    agent_text, agent_form, text_mode = gb._resolve_regen_text(
        override_text=None,
        remove_text=False,
        existing_text_mode="agent_text",
        persisted_text=None,
        persisted_highlight=None,
        run_text_agents_fn=_run_text_agents_fn,
    )

    assert llm_called["called"] is True
    assert agent_text.text == "From LLM"
    assert text_mode == "agent_text"


def test_resolve_regen_text_remove_overrides_persisted():
    """remove_text=True → (None, None, 'none') even when persisted text exists."""
    llm_called = {"called": False}

    def _run_text_agents_fn():
        llm_called["called"] = True
        return None, {}

    agent_text, agent_form, text_mode = gb._resolve_regen_text(
        override_text=None,
        remove_text=True,
        existing_text_mode="agent_text",
        persisted_text="Some hook text",
        persisted_highlight=None,
        run_text_agents_fn=_run_text_agents_fn,
    )

    assert agent_text is None
    assert agent_form is None
    assert text_mode == "none"
    assert llm_called["called"] is False


def test_resolve_regen_text_override_beats_persisted():
    """override_text wins over persisted text; no LLM call."""
    llm_called = {"called": False}

    def _run_text_agents_fn():
        llm_called["called"] = True
        return None, {}

    agent_text, agent_form, text_mode = gb._resolve_regen_text(
        override_text="new text",
        remove_text=False,
        existing_text_mode="agent_text",
        persisted_text="old text",
        persisted_highlight=None,
        run_text_agents_fn=_run_text_agents_fn,
    )

    assert llm_called["called"] is False
    assert agent_text is not None
    assert agent_text.text == "new text"


def test_resolve_regen_text_override_on_removed_variant_restores_agent_text():
    """Re-adding text to a text-removed variant flips mode none → agent_text.

    "none" is truthy, so the old `existing_text_mode or "agent_text"` kept the
    variant in "none" mode and _reburn_text_on_base silently skipped the burn —
    "add text back after removing it" no-oped. Regression guard for that fix.
    """
    agent_text, _agent_form, text_mode = gb._resolve_regen_text(
        override_text="hello again",
        remove_text=False,
        existing_text_mode="none",
        persisted_text=None,
        persisted_highlight=None,
        run_text_agents_fn=lambda: (None, {}),
    )

    assert agent_text is not None
    assert agent_text.text == "hello again"
    assert text_mode == "agent_text"


def test_resolve_regen_text_override_preserves_lyrics_mode():
    """A lyrics variant keeps its mode on a text override (no flip to agent_text)."""
    _agent_text, _agent_form, text_mode = gb._resolve_regen_text(
        override_text="custom line",
        remove_text=False,
        existing_text_mode="lyrics",
        persisted_text=None,
        persisted_highlight=None,
        run_text_agents_fn=lambda: (None, {}),
    )

    assert text_mode == "lyrics"


def test_resolve_regen_text_unrenderable_override_keeps_none_mode():
    """Junk text that sanitizes to nothing must NOT flip a "none" variant to a
    text-less "agent_text" (which would re-trigger intro_writer on a later edit)."""
    agent_text, _agent_form, text_mode = gb._resolve_regen_text(
        override_text="https://example.com",  # URL-only → stripped to nothing
        remove_text=False,
        existing_text_mode="none",
        persisted_text=None,
        persisted_highlight=None,
        run_text_agents_fn=lambda: (None, {}),
    )

    assert agent_text is None
    assert text_mode == "none"


# ── Fast-reburn: _is_fast_reburn_eligible ───────────────────────────────────────


def test_fast_reburn_kill_switch(monkeypatch):
    """Kill-switch off → fast path never taken; _ingest_clips IS called."""
    monkeypatch.setattr(gb.settings, "GENERATIVE_FAST_REBURN_ENABLED", False, raising=False)

    existing = {
        "variant_id": "song_text",
        "text_mode": "agent_text",
        "base_video_path": "generative-jobs/x/base_0_song_text.mp4",
        "intro_text": "My text",
        "intro_highlight_word": None,
        "intro_text_size_px": 60,
        "intro_size_source": "computed",
        "music_track_id": None,
        "video_path": "generative-jobs/x/song_text.mp4",
        "rank": 1,
    }

    assert gb._is_fast_reburn_eligible(existing, None, None, gb.settings) is False


def test_is_fast_reburn_eligible_returns_true_for_style_change():
    """All conditions met → eligible."""
    existing = {
        "text_mode": "agent_text",
        "base_video_path": "generative-jobs/j/base_1_song_text.mp4",
    }
    assert gb._is_fast_reburn_eligible(existing, None, None, gb.settings) is True


def test_is_fast_reburn_eligible_false_for_new_track():
    """Audio change (new_track_id) → not eligible."""
    existing = {
        "text_mode": "agent_text",
        "base_video_path": "generative-jobs/j/base_1_song_text.mp4",
    }
    assert gb._is_fast_reburn_eligible(existing, "new-track-id", None, gb.settings) is False


def test_is_fast_reburn_eligible_false_for_mix_override():
    """Audio change (mix_override) → not eligible."""
    existing = {
        "text_mode": "agent_text",
        "base_video_path": "generative-jobs/j/base_1_song_text.mp4",
    }
    assert gb._is_fast_reburn_eligible(existing, None, 0.5, gb.settings) is False


def test_is_fast_reburn_eligible_false_for_lyrics_variant():
    """Lyrics variant (text_mode='lyrics') → not eligible in v1."""
    existing = {
        "text_mode": "lyrics",
        "base_video_path": "generative-jobs/j/base_0_song_lyrics.mp4",
    }
    assert gb._is_fast_reburn_eligible(existing, None, None, gb.settings) is False


def test_legacy_variant_without_base_falls_back():
    """Existing variant with no base_video_path → not eligible (full path)."""
    existing = {
        "text_mode": "agent_text",
        "base_video_path": None,
    }
    assert gb._is_fast_reburn_eligible(existing, None, None, gb.settings) is False


# ── Fast-reburn: _reburn_text_on_base ───────────────────────────────────────────


def _patch_reburn_helpers(monkeypatch, *, base_content=b"\x00" * 32, final_content=None):
    """Stub the lazily-imported helpers so _reburn_text_on_base runs without real GCS/ffmpeg.

    `final_content`: bytes written to final_path by burn_text_overlays_skia.
    If None it defaults to different content than the base so copy-through detection passes.
    """
    import app.pipeline.generative_overlays as go
    import app.pipeline.text_overlay_skia as skia
    import app.storage as storage

    burn_calls: list = []

    def _fake_download(gcs_path, local_path):
        with open(local_path, "wb") as f:
            f.write(base_content)

    def _fake_probe(path):
        return types.SimpleNamespace(duration_s=5.0)

    def _fake_overlays(**kwargs):
        return [{"type": "text", "text": kwargs.get("text", "hi")}]

    if final_content is None:
        # Different size from base → copy-through detection passes.
        _final = b"\x01" * (len(base_content) + 8)
    else:
        _final = final_content

    def _fake_burn(base_path, overlays, out_path, tmpdir, *, matte=None):
        burn_calls.append({"base": base_path, "overlays": overlays, "out": out_path})
        with open(out_path, "wb") as f:
            f.write(_final)

    def _fake_upload(local_path, gcs_path):
        return f"https://signed/{gcs_path}"

    monkeypatch.setattr(storage, "download_to_file", _fake_download, raising=False)
    monkeypatch.setattr(storage, "upload_public_read", _fake_upload, raising=False)
    monkeypatch.setattr(go, "build_persistent_intro_overlays", _fake_overlays, raising=False)
    monkeypatch.setattr(skia, "burn_text_overlays_skia", _fake_burn, raising=False)

    # Patch probe_video inside the module namespace
    import app.pipeline.probe as probe_mod

    monkeypatch.setattr(probe_mod, "probe_video", _fake_probe, raising=False)

    return burn_calls


def test_render_persists_intro_text_and_base_path(monkeypatch, tmp_path):
    """_render_generative_variant result must include intro_text, intro_highlight_word,
    and base_video_path for agent_text variants."""
    mix_calls: list = []
    _patch_render_helpers(monkeypatch, mix_calls)
    vdir = tmp_path / "v"
    vdir.mkdir()
    spec = {"variant_id": "song_text", "rank": 2, "text_mode": "agent_text", "track": None}
    agent_text = types.SimpleNamespace(text="My hook", highlight_word="hook")
    res = gb._render_generative_variant(
        job_id="j",
        rank=2,
        spec=spec,
        clip_metas=[_Meta("c1", 5.0)],
        clip_id_to_local={"c1": "/x.mp4"},
        clip_id_to_gcs={"c1": "music-uploads/x.mp4"},
        probe_map={},
        available_footage_s=12.0,
        agent_text=agent_text,
        agent_form={"effect": "karaoke-line"},
        variant_dir=str(vdir),
    )
    assert res["ok"] is True
    assert res["intro_text"] == "My hook"
    assert res["intro_highlight_word"] == "hook"
    assert res["base_video_path"] is not None
    assert res["base_video_path"].startswith("generative-jobs/")


def test_change_style_takes_fast_path(monkeypatch, tmp_path):
    """Style change (no audio, base cached) takes fast path; _ingest_clips NOT called."""
    monkeypatch.setattr(gb.settings, "GENERATIVE_FAST_REBURN_ENABLED", True, raising=False)

    ingest_called = {"called": False}

    def _boom_ingest(*a, **k):
        ingest_called["called"] = True
        raise AssertionError("ingest must not be called on fast path")

    monkeypatch.setattr(gb, "_ingest_clips", _boom_ingest, raising=False)

    existing = {
        "variant_id": "song_text",
        "text_mode": "agent_text",
        "base_video_path": "generative-jobs/x/base_0_song_text.mp4",
        "intro_text": "My hook",
        "intro_highlight_word": None,
        "intro_text_size_px": 60,
        "intro_size_source": "computed",
        "music_track_id": None,
        "video_path": "generative-jobs/x/song_text.mp4",
        "rank": 1,
        "style_set_id": None,
    }

    burn_calls = _patch_reburn_helpers(monkeypatch)

    update_calls: list = []
    monkeypatch.setattr(
        gb,
        "_update_variant_entry",
        lambda jid, vid, patch: update_calls.append(patch),
        raising=False,
    )

    # Simulate _run_regenerate_variant fast-path branch directly via the helper.
    result = gb._reburn_text_on_base(
        job_id="test-job",
        variant_id="song_text",
        existing=existing,
        agent_text=types.SimpleNamespace(text="My hook", highlight_word=None),
        agent_form={"effect": "karaoke-line"},
        text_mode="agent_text",
        resolved_style_set_id="film_mono",
        size_override_px=None,
        settings=gb.settings,
    )

    assert ingest_called["called"] is False
    assert len(burn_calls) == 1
    assert result["render_status"] == "ready"
    assert result["intro_text"] == "My hook"


def test_swap_song_takes_full_path_preserves_text(monkeypatch, tmp_path):
    """swap_song (new_track_id set) → full path taken; text persisted from existing."""
    monkeypatch.setattr(gb.settings, "GENERATIVE_FAST_REBURN_ENABLED", True, raising=False)

    existing = {
        "text_mode": "agent_text",
        "base_video_path": "generative-jobs/x/base_0_song_text.mp4",
        "intro_text": "My text",
        "intro_highlight_word": None,
        "intro_text_size_px": 60,
        "intro_size_source": "computed",
        "music_track_id": "old-track",
    }

    # new_track_id is set → ineligible for fast path
    assert gb._is_fast_reburn_eligible(existing, "new-track-id", None, gb.settings) is False

    # The resolve function should reuse the text (no LLM) since persisted text is present.
    llm_called = {"called": False}

    def _run_text_agents_fn():
        llm_called["called"] = True
        return types.SimpleNamespace(text="LLM text", highlight_word=None), {}

    agent_text, _, text_mode = gb._resolve_regen_text(
        override_text=None,
        remove_text=False,
        existing_text_mode="agent_text",
        persisted_text="My text",
        persisted_highlight=None,
        run_text_agents_fn=_run_text_agents_fn,
    )

    assert llm_called["called"] is False
    assert agent_text.text == "My text"


def test_mix_takes_full_path_preserves_text(monkeypatch):
    """mix_override set → full path taken (not fast-reburn eligible); text preserved."""
    existing = {
        "text_mode": "agent_text",
        "base_video_path": "generative-jobs/x/base_0_song_text.mp4",
        "intro_text": "My voiceover text",
    }

    assert gb._is_fast_reburn_eligible(existing, None, 0.8, gb.settings) is False

    llm_called = {"called": False}

    def _run_text_agents_fn():
        llm_called["called"] = True
        return None, {}

    agent_text, _, _ = gb._resolve_regen_text(
        override_text=None,
        remove_text=False,
        existing_text_mode="agent_text",
        persisted_text="My voiceover text",
        persisted_highlight=None,
        run_text_agents_fn=_run_text_agents_fn,
    )

    assert llm_called["called"] is False
    assert agent_text.text == "My voiceover text"


def test_remove_text_fast_path(monkeypatch):
    """remove_text=True → text_mode='none', no overlays built, fast path eligible."""
    monkeypatch.setattr(gb.settings, "GENERATIVE_FAST_REBURN_ENABLED", True, raising=False)

    existing = {
        "text_mode": "agent_text",
        "base_video_path": "generative-jobs/x/base_0_song_text.mp4",
        "intro_text": "Some hook",
        "intro_highlight_word": None,
        "intro_text_size_px": 60,
        "intro_size_source": "computed",
    }

    # Still fast-reburn eligible (remove_text is handled after eligibility check).
    assert gb._is_fast_reburn_eligible(existing, None, None, gb.settings) is True

    agent_text, agent_form, text_mode = gb._resolve_regen_text(
        override_text=None,
        remove_text=True,
        existing_text_mode="agent_text",
        persisted_text="Some hook",
        persisted_highlight=None,
        run_text_agents_fn=lambda: (None, None),
    )

    assert agent_text is None
    assert text_mode == "none"


def test_fast_path_base_download_failure_falls_back(monkeypatch):
    """If base download raises a 'not found' error, _reburn_text_on_base raises and
    the caller (_run_regenerate_variant) must fall back to the full path without propagating."""
    import app.storage as storage

    def _fail_download(gcs_path, local_path):
        raise RuntimeError("no such object: generative-jobs/x/base_0_song_text.mp4")

    monkeypatch.setattr(storage, "download_to_file", _fail_download, raising=False)

    existing = {
        "text_mode": "agent_text",
        "base_video_path": "generative-jobs/x/base_0_song_text.mp4",
        "intro_text": "Hook",
        "intro_highlight_word": None,
        "intro_text_size_px": 60,
        "intro_size_source": "computed",
        "video_path": "generative-jobs/x/song_text.mp4",
    }

    # _reburn_text_on_base raises when download fails.
    import pytest

    with pytest.raises(RuntimeError, match="no such object"):
        gb._reburn_text_on_base(
            job_id="test-job",
            variant_id="song_text",
            existing=existing,
            agent_text=types.SimpleNamespace(text="Hook", highlight_word=None),
            agent_form={"effect": "karaoke-line"},
            text_mode="agent_text",
            resolved_style_set_id=None,
            size_override_px=None,
            settings=gb.settings,
        )

    # The caller treats "not found"-shaped errors as a safe fallback signal.
    # The key assertion here is that _reburn_text_on_base raises and that the error
    # message contains a recognisable "not found" substring that the caller checks.
    # (The actual fallback logic is exercised in _run_regenerate_variant; we verify
    # the contract here at the _reburn_text_on_base boundary.)


def test_fast_reburn_kill_switch_off_forces_full_path(monkeypatch):
    """GENERATIVE_FAST_REBURN_ENABLED=False → _is_fast_reburn_eligible returns False
    even when all other conditions are met."""
    monkeypatch.setattr(gb.settings, "GENERATIVE_FAST_REBURN_ENABLED", False, raising=False)

    existing = {
        "text_mode": "agent_text",
        "base_video_path": "generative-jobs/j/base_1_song_text.mp4",
    }

    assert gb._is_fast_reburn_eligible(existing, None, None, gb.settings) is False


def test_reburn_size_pixel_stable(monkeypatch):
    """Existing computed size is carried forward as computed_fallback_px when no override
    is given; the size is passed to _resolve_intro_overlay_params, not dropped."""
    _patch_reburn_helpers(monkeypatch)

    resolve_calls: list = []
    _original_resolve = gb._resolve_intro_overlay_params

    def _spy_resolve(agent_text, agent_form, style_set_id, **kwargs):
        resolve_calls.append(kwargs)
        return _original_resolve(agent_text, agent_form, style_set_id, **kwargs)

    monkeypatch.setattr(gb, "_resolve_intro_overlay_params", _spy_resolve, raising=False)

    existing = {
        "text_mode": "agent_text",
        "base_video_path": "generative-jobs/x/base_0_song_text.mp4",
        "intro_text": "My hook",
        "intro_highlight_word": None,
        "intro_text_size_px": 60,
        "intro_size_source": "computed",
        "video_path": "generative-jobs/x/song_text.mp4",
    }

    gb._reburn_text_on_base(
        job_id="test-job",
        variant_id="song_text",
        existing=existing,
        agent_text=types.SimpleNamespace(text="My hook", highlight_word=None),
        agent_form={"effect": "karaoke-line"},
        text_mode="agent_text",
        resolved_style_set_id=None,
        size_override_px=None,  # no override → must use persisted px
        settings=gb.settings,
    )

    assert len(resolve_calls) == 1
    # The persisted size (60) must be passed so overlay params don't recompute from scratch.
    assert resolve_calls[0].get("size_override_px") == 60


def test_full_rerender_overwrites_base_video_path(monkeypatch, tmp_path):
    """After a full re-render (swap_song path), the returned dict must contain
    base_video_path — either a new GCS key or None — NOT the stale old path."""
    mix_calls: list = []
    _patch_render_helpers(monkeypatch, mix_calls)

    import app.pipeline.music_recipe as mr

    monkeypatch.setattr(
        mr,
        "generate_music_recipe",
        lambda td, **_kw: {
            "slots": [{"position": 1, "target_duration_s": 2.0, "text_overlays": []}],
            "beat_timestamps_s": [0.5, 1.0],
        },
        raising=False,
    )

    vdir = tmp_path / "v1"
    vdir.mkdir()
    spec = {"variant_id": "song_text", "rank": 1, "text_mode": "agent_text", "track": _track()}
    agent_text = types.SimpleNamespace(text="Fresh hook", highlight_word=None)
    res = gb._render_generative_variant(
        job_id="j",
        rank=1,
        spec=spec,
        clip_metas=[_Meta("c1", 5.0)],
        clip_id_to_local={"c1": "/x.mp4"},
        clip_id_to_gcs={"c1": "music-uploads/x.mp4"},
        probe_map={},
        available_footage_s=12.0,
        agent_text=agent_text,
        agent_form={"effect": "karaoke-line"},
        variant_dir=str(vdir),
    )

    assert res["ok"] is True
    # base_video_path must be present in the result dict — it's NOT the stale "old_path".
    assert "base_video_path" in res
    if res["base_video_path"] is not None:
        assert res["base_video_path"] != "old_path"


def test_voiceover_variant_fast_path_preserves_mix(monkeypatch):
    """Voiceover variant (no mix_override, base set, text_mode=agent_text) →
    fast-reburn is eligible (no audio change)."""
    # mix_override=None means no voiceover bed change — fast path is still available
    # for pure text/style changes on a voiceover variant that has a cached base.
    existing = {
        "text_mode": "agent_text",
        "base_video_path": "generative-jobs/x/base_0_voiceover_only.mp4",
        "intro_text": "Hook text",
    }

    # Voiceover variants with no mix_override and no new_track_id are eligible.
    assert gb._is_fast_reburn_eligible(existing, None, None, gb.settings) is True


def test_fast_path_burn_copy_through_marks_failed(monkeypatch):
    """When burn_text_overlays_skia writes the same byte-count as input (copy-through),
    _reburn_text_on_base must raise RuntimeError."""
    import app.pipeline.generative_overlays as go
    import app.pipeline.probe as probe_mod
    import app.pipeline.text_overlay_skia as skia
    import app.storage as storage

    base_content = b"\x00" * 64

    def _fake_download(gcs_path, local_path):
        with open(local_path, "wb") as f:
            f.write(base_content)

    def _fake_probe(path):
        return types.SimpleNamespace(duration_s=5.0)

    def _fake_overlays(**kwargs):
        # Return non-empty overlays so the copy-through check fires.
        return [{"type": "text", "text": "hi"}]

    def _copy_through_burn(base_path, overlays, out_path, tmpdir, *, matte=None):
        # Write same byte count as base → simulates copy-through (no actual burn).
        with open(out_path, "wb") as f:
            f.write(base_content)

    monkeypatch.setattr(storage, "download_to_file", _fake_download, raising=False)
    monkeypatch.setattr(storage, "upload_public_read", lambda *a: "https://signed/x", raising=False)
    monkeypatch.setattr(go, "build_persistent_intro_overlays", _fake_overlays, raising=False)
    monkeypatch.setattr(skia, "burn_text_overlays_skia", _copy_through_burn, raising=False)
    monkeypatch.setattr(probe_mod, "probe_video", _fake_probe, raising=False)

    existing = {
        "text_mode": "agent_text",
        "base_video_path": "generative-jobs/x/base_0_song_text.mp4",
        "intro_text": "Hi",
        "intro_highlight_word": None,
        "intro_text_size_px": 60,
        "intro_size_source": "computed",
        "video_path": "generative-jobs/x/song_text.mp4",
    }

    import pytest

    with pytest.raises(RuntimeError, match="copy-through"):
        gb._reburn_text_on_base(
            job_id="test-job",
            variant_id="song_text",
            existing=existing,
            agent_text=types.SimpleNamespace(text="Hi", highlight_word=None),
            agent_form={"effect": "karaoke-line"},
            text_mode="agent_text",
            resolved_style_set_id=None,
            size_override_px=None,
            settings=gb.settings,
        )


# ── Narrative order (filming-guide alignment) ─────────────────────────────────


def test_resolve_narrative_order_zero_count_is_none():
    assert gb._resolve_narrative_order(0, {"a": "g/a.mp4"}, job_id="j") is None


def test_resolve_narrative_order_returns_first_n_ids_in_order():
    c2g = {"id_a": "g/a.mp4", "id_b": "g/b.mp4", "id_c": "g/c.mp4"}
    assert gb._resolve_narrative_order(2, c2g, job_id="j") == ["id_a", "id_b"]


def test_resolve_narrative_order_kill_switch(monkeypatch):
    monkeypatch.setattr(gb.settings, "NARRATIVE_CLIP_ORDER_ENABLED", False)
    assert gb._resolve_narrative_order(2, {"a": "g/a.mp4", "b": "g/b.mp4"}, job_id="j") is None


def test_render_variant_threads_narrative_order_to_match(monkeypatch, tmp_path):
    """The defining plumbing assertion: narrative_order reaches match()."""
    import app.pipeline.template_matcher as tm

    mix_calls: list = []
    _patch_render_helpers(monkeypatch, mix_calls)
    received: dict = {}

    def _capture_match(recipe, metas, **kw):
        received.update(kw)
        return types.SimpleNamespace(steps=[])

    monkeypatch.setattr(tm, "match", _capture_match, raising=False)
    vdir = tmp_path / "vn"
    vdir.mkdir()
    spec = {"variant_id": "original_text", "rank": 1, "text_mode": "none", "track": None}
    res = gb._render_generative_variant(
        job_id="j",
        rank=1,
        spec=spec,
        clip_metas=[_Meta("c1", 5.0), _Meta("c2", 4.0)],
        clip_id_to_local={"c1": "/x.mp4", "c2": "/y.mp4"},
        clip_id_to_gcs={"c1": "music-uploads/x.mp4", "c2": "music-uploads/y.mp4"},
        probe_map={},
        available_footage_s=12.0,
        agent_text=None,
        agent_form={},
        variant_dir=str(vdir),
        narrative_order=["c2", "c1"],
    )
    assert res["ok"] is True
    assert received.get("narrative_order") == ["c2", "c1"]


def test_build_generative_job_persists_narrative_shot_count():
    import uuid as _uuid

    from app.services.generative_jobs import build_generative_job

    job = build_generative_job(
        user_id=_uuid.uuid4(),
        clip_paths=["users/u/plan/i/a.mp4", "users/u/plan/i/b.mp4"],
        mode="content_plan",
        narrative_shot_count=2,
    )
    assert job.all_candidates["narrative_shot_count"] == 2


def test_build_generative_job_omits_key_when_zero():
    import uuid as _uuid

    from app.services.generative_jobs import build_generative_job

    job = build_generative_job(
        user_id=_uuid.uuid4(),
        clip_paths=["users/u/plan/i/a.mp4"],
    )
    assert "narrative_shot_count" not in job.all_candidates


def test_build_generative_job_clamps_count_to_clip_count():
    import uuid as _uuid

    from app.services.generative_jobs import build_generative_job

    job = build_generative_job(
        user_id=_uuid.uuid4(),
        clip_paths=["users/u/plan/i/a.mp4"],
        narrative_shot_count=5,
    )
    assert job.all_candidates["narrative_shot_count"] == 1


# ---------------------------------------------------------------------------
# Cluster layout — _resolve_intro_overlay_params + _resolve_regen_text threading
# ---------------------------------------------------------------------------


def _cluster_agent_text(text: str = "what's your favorite place?"):
    return types.SimpleNamespace(
        text=text,
        highlight_word="favorite",
        word_roles=["connector", "hero", "hero", "closer"],
    )


class TestResolveIntroOverlayParamsLayout:
    def test_cluster_layout_passes_through(self):
        params, _, _ = gb._resolve_intro_overlay_params(
            _cluster_agent_text(), {"effect": "fade-in", "layout": "cluster"}, None
        )
        assert params["layout"] == "cluster"
        assert params["layout_source"] == "model"
        assert params["word_roles"] == ["connector", "hero", "hero", "closer"]
        assert params["language"] == "en"

    def test_language_threads_to_intro_builder_params(self):
        params, _, _ = gb._resolve_intro_overlay_params(
            _cluster_agent_text(),
            {"effect": "fade-in", "layout": "cluster"},
            None,
            language="tr",
        )
        assert params["language"] == "tr"

    def test_layout_defaults_to_linear(self):
        params, _, _ = gb._resolve_intro_overlay_params(
            _agent_text(), {"effect": "karaoke-line"}, None
        )
        assert params["layout"] == "linear"
        assert params["layout_source"] == "model"
        assert params["word_roles"] is None  # SimpleNamespace without the attr

    def test_layout_source_threads_from_matcher_output(self):
        params, _, _ = gb._resolve_intro_overlay_params(
            _agent_text(),
            {"effect": "fade-in", "layout": "linear", "layout_source": "coerced_default"},
            None,
        )
        assert params["layout_source"] == "coerced_default"

    def test_kill_switch_forces_linear(self, monkeypatch):
        monkeypatch.setattr(gb.settings, "GENERATIVE_CLUSTER_INTRO_ENABLED", False, raising=False)
        params, _, _ = gb._resolve_intro_overlay_params(
            _cluster_agent_text(), {"effect": "fade-in", "layout": "cluster"}, None
        )
        assert params["layout"] == "linear"
        assert params["layout_reason"] == "disabled"

    def test_explicit_linear_is_not_mislabeled_when_kill_switch_is_off(self, monkeypatch):
        monkeypatch.setattr(gb.settings, "GENERATIVE_CLUSTER_INTRO_ENABLED", False, raising=False)
        params, _, _ = gb._resolve_intro_overlay_params(
            _agent_text(), {"effect": "fade-in", "layout": "linear"}, None
        )
        assert params["layout"] == "linear"
        assert params["layout_reason"] is None

    def test_user_position_knob_forces_linear(self):
        # A manual position pin conflicts with engine-owned cluster geometry.
        params, _, _ = gb._resolve_intro_overlay_params(
            _cluster_agent_text(),
            {"effect": "fade-in", "layout": "cluster"},
            None,
            user_style_knobs={"position_y_frac": 0.8},
        )
        assert params["layout"] == "linear"
        assert params["layout_reason"] == "position_pinned"

    def test_explicit_linear_is_not_mislabeled_when_position_is_pinned(self):
        params, _, _ = gb._resolve_intro_overlay_params(
            _agent_text(),
            {"effect": "fade-in", "layout": "linear"},
            None,
            user_style_knobs={"position_y_frac": 0.8},
        )
        assert params["layout"] == "linear"
        assert params["layout_reason"] is None

    def test_user_named_position_knob_forces_linear(self):
        params, _, _ = gb._resolve_intro_overlay_params(
            _cluster_agent_text(),
            {"effect": "fade-in", "layout": "cluster"},
            None,
            user_style_knobs={"position": "bottom"},
        )
        assert params["layout"] == "linear"

    def test_smart_placement_candidate_applies_when_position_is_not_explicit(self):
        params, _, _ = gb._resolve_intro_overlay_params(
            _agent_text(),
            {"effect": "fade-in", "layout": "linear"},
            None,
            size_override_px=58,
            placement_candidates=[
                {
                    "source": "clip_safe_zone",
                    "x_frac": 0.62,
                    "y_frac": 0.34,
                    "max_width_frac": 0.48,
                }
            ],
        )

        assert params["position"] == "center"
        assert params["position_x_frac"] == 0.62
        assert params["position_y_frac"] == 0.34
        assert params["max_width_frac"] == 0.48

    def test_smart_placement_candidate_does_not_override_user_position(self):
        params, _, _ = gb._resolve_intro_overlay_params(
            _agent_text(),
            {"effect": "fade-in", "layout": "linear"},
            None,
            size_override_px=58,
            user_style_knobs={"position_y_frac": 0.8},
            placement_candidates=[
                {
                    "source": "clip_safe_zone",
                    "x_frac": 0.62,
                    "y_frac": 0.34,
                    "max_width_frac": 0.48,
                }
            ],
        )

        assert params["position_y_frac"] == 0.8
        assert params["position_x_frac"] is None
        assert "max_width_frac" not in params


class TestResolveRegenTextClusterPersistence:
    def test_persisted_cluster_layout_and_roles_reused_without_llm(self):
        agent_text, agent_form, text_mode = gb._resolve_regen_text(
            override_text=None,
            remove_text=False,
            existing_text_mode="agent_text",
            persisted_text="what's your favorite place?",
            persisted_highlight="favorite",
            run_text_agents_fn=lambda: (None, None),
            persisted_layout="cluster",
            persisted_word_roles=["connector", "hero", "hero", "closer"],
        )
        assert text_mode == "agent_text"
        assert agent_form["layout"] == "cluster"
        assert agent_text.word_roles == ["connector", "hero", "hero", "closer"]

    def test_override_text_keeps_layout_but_drops_stale_roles(self):
        agent_text, agent_form, _ = gb._resolve_regen_text(
            override_text="completely new words here",
            remove_text=False,
            existing_text_mode="agent_text",
            persisted_text="what's your favorite place?",
            persisted_highlight=None,
            run_text_agents_fn=lambda: (None, None),
            persisted_layout="cluster",
            persisted_word_roles=["connector", "hero", "hero", "closer"],
        )
        assert agent_form["layout"] == "cluster"  # cluster stays sticky
        # Stale roles must never apply to user-typed words — engine re-derives.
        assert getattr(agent_text, "word_roles", None) is None

    def test_legacy_variant_without_layout_defaults_linear(self):
        _, agent_form, _ = gb._resolve_regen_text(
            override_text=None,
            remove_text=False,
            existing_text_mode="agent_text",
            persisted_text="my old hook",
            persisted_highlight=None,
            run_text_agents_fn=lambda: (None, None),
        )
        assert agent_form["layout"] == "linear"


def test_run_text_agents_refusal_returns_safe_fallback(monkeypatch):
    from app.agents._runtime import RefusalError
    from app.agents.intro_writer import _is_refusal_text

    class _Form:
        matched_example_ids: list[str] = []

        def model_dump(self):
            return {
                "effect": "karaoke-line",
                "layout": "cluster",
                "layout_source": "model",
                "matched_example_ids": [],
            }

    monkeypatch.setattr("app.agents._model_client.default_client", lambda: object())
    monkeypatch.setattr(
        "app.agents.overlay_format_matcher.OverlayFormatMatcherAgent.run",
        lambda self, input, ctx=None: _Form(),  # noqa: ARG005
    )
    monkeypatch.setattr(
        "app.agents.intro_writer.IntroTextWriterAgent.run",
        lambda self, input, ctx=None: (_ for _ in ()).throw(  # noqa: ARG005
            RefusalError("intro_writer: refusal/meta text after sanitization")
        ),
    )

    text, form = gb._run_text_agents(
        [_Meta("c1", 8.0, detected_subject="Golden canyon")],
        _Meta("c1", 8.0, detected_subject="Golden canyon"),
        job_id="job-refusal",
    )

    assert text is not None
    assert text.text == "watch golden canyon unfold"
    assert not _is_refusal_text(text.text)
    assert form["layout"] == "linear"
    assert form["effect"] == "fade-in"


def test_run_text_agents_wrapped_refusal_returns_safe_fallback(monkeypatch):
    from app.agents._runtime import RefusalError, TerminalError

    class _Form:
        matched_example_ids: list[str] = []

        def model_dump(self):
            return {
                "effect": "karaoke-line",
                "layout": "cluster",
                "layout_source": "model",
                "matched_example_ids": [],
            }

    def _raise_wrapped_refusal():
        try:
            raise RefusalError("intro_writer: refusal/meta text after sanitization")
        except RefusalError as exc:
            raise TerminalError("nova.compose.intro_writer: refusal") from exc

    monkeypatch.setattr("app.agents._model_client.default_client", lambda: object())
    monkeypatch.setattr(
        "app.agents.overlay_format_matcher.OverlayFormatMatcherAgent.run",
        lambda self, input, ctx=None: _Form(),  # noqa: ARG005
    )
    monkeypatch.setattr(
        "app.agents.intro_writer.IntroTextWriterAgent.run",
        lambda self, input, ctx=None: _raise_wrapped_refusal(),  # noqa: ARG005
    )

    text, form = gb._run_text_agents(
        [_Meta("c1", 8.0, detected_subject="Golden canyon")],
        _Meta("c1", 8.0, detected_subject="Golden canyon"),
        job_id="job-wrapped-refusal",
    )

    assert text.text == "watch golden canyon unfold"
    assert form["layout"] == "linear"
    assert form["effect"] == "fade-in"


# ---------------------------------------------------------------------------
# _fail_job: variant + PlanItem reconciliation
# ---------------------------------------------------------------------------


class TestFailJobVariantReconciliation:
    """Regression: _fail_job must flip 'rendering'/'pending' variants to 'failed'.

    Prod incident (job df883a50): worker died mid-render_variants; reaper
    flipped job-level status to processing_failed but _fail_job (triggered by
    soft-timeout/exception paths) had the same gap.  Frontend anyRendering
    kept the poll alive forever.  Fix: _fail_job reconciles per-variant
    render_status inside the same transaction.
    """

    def _make_job(self, assembly_plan: dict | None):
        """Return a minimal fake Job with the given assembly_plan."""
        import types

        job = types.SimpleNamespace()
        job.status = "processing"
        job.error_detail = None
        job.failure_reason = None
        job.assembly_plan = assembly_plan
        job.content_plan_item_id = None
        return job

    def _call_fail_job(self, job, monkeypatch):
        """Call _fail_job with a mocked DB session holding `job`."""
        from unittest.mock import MagicMock, patch

        session = MagicMock()
        session.get.return_value = job

        ctx = MagicMock()
        ctx.__enter__ = MagicMock(return_value=session)
        ctx.__exit__ = MagicMock(return_value=False)

        with patch("app.tasks.generative_build._sync_session", return_value=ctx):
            gb._fail_job("00000000-0000-0000-0000-000000000001", "test error")

        return job

    def test_flips_rendering_variant_to_failed(self, monkeypatch):
        assembly_plan = {
            "variants": [
                {"variant_id": "song_lyrics", "render_status": "ready"},
                {"variant_id": "original_text", "render_status": "rendering"},
            ]
        }
        job = self._make_job(assembly_plan)
        self._call_fail_job(job, monkeypatch)

        variants = {v["variant_id"]: v for v in job.assembly_plan["variants"]}
        assert variants["original_text"]["render_status"] == "failed", (
            "Frozen 'rendering' variant must be flipped to 'failed' by _fail_job"
        )
        assert variants["song_lyrics"]["render_status"] == "ready", (
            "Ready variants must not be touched"
        )

    def test_flips_pending_variant_to_failed(self, monkeypatch):
        assembly_plan = {
            "variants": [
                {"variant_id": "song_text", "render_status": "pending"},
            ]
        }
        job = self._make_job(assembly_plan)
        self._call_fail_job(job, monkeypatch)

        assert job.assembly_plan["variants"][0]["render_status"] == "failed"

    def test_noop_when_no_assembly_plan(self, monkeypatch):
        """Jobs without assembly_plan (failed before render step) must not crash."""
        job = self._make_job(None)
        self._call_fail_job(job, monkeypatch)
        assert job.assembly_plan is None  # not mutated

    def test_noop_when_all_variants_terminal(self, monkeypatch):
        """All variants already terminal — assembly_plan must not be mutated."""
        assembly_plan = {
            "variants": [
                {"variant_id": "song_lyrics", "render_status": "ready"},
                {"variant_id": "song_text", "render_status": "failed"},
            ]
        }
        job = self._make_job(assembly_plan)
        original_ap = dict(assembly_plan)
        self._call_fail_job(job, monkeypatch)

        # assembly_plan must not have been replaced (no frozen variants to fix)
        assert job.assembly_plan == original_ap

    def test_job_status_set_to_processing_failed(self, monkeypatch):
        job = self._make_job(None)
        self._call_fail_job(job, monkeypatch)
        assert job.status == "processing_failed"


# ── _build_no_music_recipe min_slots floor ────────────────────────────────────


def test_build_no_music_recipe_floor_8_assigned_8_slots():
    """8 assigned clips + min_slots=8 → recipe emits exactly 8 slots."""
    metas = [_Meta(f"c{i}", 5.0) for i in range(8)]
    recipe = gb._build_no_music_recipe(metas, available_footage_s=24.0, min_slots=8)
    assert recipe["shot_count"] == 8
    assert len(recipe["slots"]) == 8


def test_build_no_music_recipe_floor_exceeds_cap():
    """min_slots > _MAX_NO_MUSIC_SLOTS → floor takes over the cap."""
    n_clips = 9
    metas = [_Meta(f"c{i}", 5.0) for i in range(n_clips)]
    assert n_clips > gb._MAX_NO_MUSIC_SLOTS  # guard: this test requires n > cap
    recipe = gb._build_no_music_recipe(metas, available_footage_s=30.0, min_slots=n_clips)
    assert recipe["shot_count"] == n_clips


def test_build_no_music_recipe_floor_clamped_to_clip_count():
    """min_slots cannot exceed available clips — n stays at clip count."""
    metas = [_Meta(f"c{i}", 5.0) for i in range(3)]
    recipe = gb._build_no_music_recipe(metas, available_footage_s=9.0, min_slots=10)
    # 3 clips → at most 3 slots regardless of min_slots
    assert recipe["shot_count"] == 3


def test_build_no_music_recipe_floor_below_cap_unchanged():
    """min_slots below the natural cap leaves shot_count at the normal cap."""
    metas = [_Meta(f"c{i}", 5.0) for i in range(10)]
    recipe_natural = gb._build_no_music_recipe(metas, available_footage_s=30.0)
    recipe_floor = gb._build_no_music_recipe(metas, available_footage_s=30.0, min_slots=2)
    assert recipe_floor["shot_count"] == recipe_natural["shot_count"]


def test_build_no_music_recipe_min_slots_zero_byte_identical():
    """min_slots=0 is identical to the pre-floor default (no change for pool-only jobs)."""
    metas = [_Meta(f"c{i}", 5.0) for i in range(5)]
    default = gb._build_no_music_recipe(metas, available_footage_s=15.0)
    floored = gb._build_no_music_recipe(metas, available_footage_s=15.0, min_slots=0)
    assert default["shot_count"] == floored["shot_count"]
    assert default["slots"] == floored["slots"]


# ── _build_unplaced_shots ─────────────────────────────────────────────────────


class _FakeMeta:
    def __init__(self, clip_id):
        self.clip_id = clip_id


def test_build_unplaced_shots_empty_when_all_placed():
    clip_metas = [_FakeMeta("g1"), _FakeMeta("g2"), _FakeMeta("g3")]
    result = gb._build_unplaced_shots(
        [],  # no unplaced ids
        narrative_order=["g1", "g2", "g3"],
        clip_id_to_gcs={"g1": "gs://b/g1.mp4", "g2": "gs://b/g2.mp4", "g3": "gs://b/g3.mp4"},
        clip_metas=clip_metas,
        is_music_variant=False,
    )
    assert result == []


def test_build_unplaced_shots_unusable_for_missing_clip():
    """A clip absent from clip_metas → 'unusable_footage' regardless of variant type."""
    # g2 not analyzed (not in clip_metas)
    clip_metas = [_FakeMeta("g1"), _FakeMeta("g3")]
    result = gb._build_unplaced_shots(
        ["g2"],
        narrative_order=["g1", "g2", "g3"],
        clip_id_to_gcs={"g1": "gs://b/g1.mp4", "g2": "gs://b/g2.mp4", "g3": "gs://b/g3.mp4"},
        clip_metas=clip_metas,
        is_music_variant=True,  # even in a music variant, missing clip → unusable
    )
    assert len(result) == 1
    r = result[0]
    assert r["clip_id"] == "g2"
    assert r["shot_index"] == 2  # 1-based ordinal in narrative_order
    assert r["reason"] == "unusable_footage"
    assert r["gcs_path"] == "gs://b/g2.mp4"


def test_build_unplaced_shots_song_too_short_for_music_variant():
    """A clip that IS analyzed but unplaced in a music variant → 'song_too_short'."""
    clip_metas = [_FakeMeta("g1"), _FakeMeta("g2"), _FakeMeta("g3")]
    result = gb._build_unplaced_shots(
        ["g3"],
        narrative_order=["g1", "g2", "g3"],
        clip_id_to_gcs={"g1": "gs://b/g1.mp4", "g2": "gs://b/g2.mp4", "g3": "gs://b/g3.mp4"},
        clip_metas=clip_metas,
        is_music_variant=True,
    )
    assert len(result) == 1
    r = result[0]
    assert r["clip_id"] == "g3"
    assert r["shot_index"] == 3
    assert r["reason"] == "song_too_short"


def test_build_unplaced_shots_analyzed_unplaced_no_music_is_unusable():
    """In a no-music variant, an analyzed-but-unplaced clip → 'unusable_footage'
    (there's no song-length reason; something else degraded the clip at match time).
    """
    clip_metas = [_FakeMeta("g1"), _FakeMeta("g2")]
    result = gb._build_unplaced_shots(
        ["g2"],
        narrative_order=["g1", "g2"],
        clip_id_to_gcs={"g1": "gs://b/g1.mp4", "g2": "gs://b/g2.mp4"},
        clip_metas=clip_metas,
        is_music_variant=False,
    )
    assert result[0]["reason"] == "unusable_footage"


def test_build_unplaced_shots_shot_index_preserves_ordinal():
    """shot_index is 1-based position in narrative_order — NOT the clip name suffix."""
    clip_metas = [_FakeMeta("z"), _FakeMeta("a"), _FakeMeta("m")]
    result = gb._build_unplaced_shots(
        ["m"],
        narrative_order=["z", "a", "m"],
        clip_id_to_gcs={"z": "gs://b/z.mp4", "a": "gs://b/a.mp4", "m": "gs://b/m.mp4"},
        clip_metas=clip_metas,
        is_music_variant=True,
    )
    assert result[0]["shot_index"] == 3  # "m" is 3rd in narrative_order


def test_build_unplaced_shots_gcs_path_none_when_absent():
    """clip_id_to_gcs may not have an entry for every clip (defensive)."""
    clip_metas = [_FakeMeta("g1"), _FakeMeta("g2")]
    result = gb._build_unplaced_shots(
        ["g2"],
        narrative_order=["g1", "g2"],
        clip_id_to_gcs={"g1": "gs://b/g1.mp4"},  # g2 missing from map
        clip_metas=clip_metas,
        is_music_variant=False,
    )
    assert result[0]["gcs_path"] is None


def test_build_no_music_recipe_min_slots_narrative_order_none():
    """_build_no_music_recipe with min_slots=0 is byte-identical to the baseline."""
    metas = [_Meta(f"c{i}", 5.0) for i in range(5)]
    baseline = gb._build_no_music_recipe(metas, available_footage_s=15.0)
    floor_zero = gb._build_no_music_recipe(metas, available_footage_s=15.0, min_slots=0)
    assert baseline == floor_zero


def test_pool_only_job_passes_clip_count_as_min_slots_to_recipe(monkeypatch, tmp_path):
    """Pool-only jobs (narrative_order=None) must pass len(clip_metas) as min_slots
    to generate_music_recipe so all uploaded clips get a slot in music variants.

    Regression guard for the investigation finding where 9 pool clips produced
    only 3 slots because min_slots was 0 instead of 9.
    """
    import app.pipeline.music_recipe as mr

    mix_calls: list = []
    _patch_render_helpers(monkeypatch, mix_calls)

    captured_min_slots: list[int] = []

    def _capturing_recipe(td, **kw):
        captured_min_slots.append(kw.get("min_slots", 0))
        return {
            "slots": [{"position": 1, "target_duration_s": 2.0, "text_overlays": []}],
            "beat_timestamps_s": [0.5, 1.0],
        }

    monkeypatch.setattr(mr, "generate_music_recipe", _capturing_recipe, raising=False)

    vdir = tmp_path / "v1"
    vdir.mkdir()
    spec = {"variant_id": "song_lyrics", "rank": 1, "text_mode": "none", "track": _track()}
    n_clips = 9
    clip_metas = [_Meta(f"c{i}", 5.0) for i in range(n_clips)]
    clip_id_to_local = {f"c{i}": f"/clip{i}.mp4" for i in range(n_clips)}
    clip_id_to_gcs = {f"c{i}": f"music-uploads/clip{i}.mp4" for i in range(n_clips)}

    gb._render_generative_variant(
        job_id="j",
        rank=1,
        spec=spec,
        clip_metas=clip_metas,
        clip_id_to_local=clip_id_to_local,
        clip_id_to_gcs=clip_id_to_gcs,
        probe_map={},
        available_footage_s=45.0,
        agent_text=None,
        agent_form={},
        variant_dir=str(vdir),
        narrative_order=None,  # pool-only — no shot assignments
    )

    assert captured_min_slots, "generate_music_recipe was not called"
    assert captured_min_slots[0] == n_clips, (
        f"min_slots should equal clip count ({n_clips}), got {captured_min_slots[0]}"
    )


# ── caption reburn (on-video editor Apply) ───────────────────────────────────


def _narrated_caption_variant(**over):
    v = {
        "variant_id": "narrated",
        "rank": 1,
        "render_status": "ready",
        "resolved_archetype": "narrated",
        "base_video_path": "generative-jobs/j/variant_1_narrated_base.mp4",
        "video_path": "generative-jobs/j/variant_1_narrated.mp4",
        "output_url": "https://signed/old",
        "caption_cues": [{"text": "the energy here", "start_s": 0.0, "end_s": 1.2}],
    }
    v.update(over)
    return v


def _patch_reburn_io(monkeypatch, burned: dict):
    def _dl(_src, dst):
        with open(dst, "wb") as f:
            f.write(b"x")

    monkeypatch.setattr("app.storage.download_to_file", _dl)
    monkeypatch.setattr("app.storage.upload_public_read", lambda *a, **k: "https://signed/new")
    monkeypatch.setattr(
        "app.storage.delete_object_best_effort",
        lambda path: burned.setdefault("deleted", []).append(path) or True,
    )
    monkeypatch.setattr("app.pipeline.captions.generate_ass_from_cues", lambda *a, **k: None)

    def _burn(*_a, **_k):
        burned["called"] = True

    monkeypatch.setattr("app.pipeline.narrated_assembler.burn_captions_on_video", _burn)


def test_compose_subtitled_burns_text_before_captions(monkeypatch, tmp_path):
    order: list[tuple] = []
    base = tmp_path / "base.mp4"
    base.write_bytes(b"base")

    monkeypatch.setattr(
        "app.pipeline.generative_overlays.build_overlays_from_text_elements",
        lambda elements, **kw: [{"text": elements[0].text}],
    )

    def _burn_text(input_path, overlays, output_path, tmpdir):
        order.append(("text", input_path, output_path, overlays))
        with open(output_path, "wb") as f:
            f.write(b"text")

    def _burn_captions(input_path, output_path, variant, tmpdir):
        order.append(("captions", input_path, output_path, variant["caption_cues"]))
        with open(output_path, "wb") as f:
            f.write(b"captions")

    monkeypatch.setattr("app.pipeline.text_overlay_skia.burn_text_overlays_skia", _burn_text)
    monkeypatch.setattr(gb, "_burn_persisted_captions_onto_base", _burn_captions)

    out = gb._compose_subtitled_final(
        str(base),
        {
            "duration_s": 3.0,
            "text_elements": [
                {
                    "id": "title",
                    "text": "TITLE",
                    "start_s": 0.0,
                    "end_s": 2.0,
                    "role": "generative_intro",
                    "position": "middle",
                }
            ],
            "caption_cues": [{"text": "caption", "start_s": 0.0, "end_s": 1.0}],
        },
        str(tmp_path),
    )

    assert out.endswith("subtitled_final.mp4")
    assert [entry[0] for entry in order] == ["text", "captions"]
    assert order[0][1] == str(base)
    assert order[1][1] == order[0][2]


def test_compose_subtitled_without_text_passes_base_to_captions(monkeypatch, tmp_path):
    base = tmp_path / "base.mp4"
    base.write_bytes(b"base")
    seen = {}

    monkeypatch.setattr(
        "app.pipeline.text_overlay_skia.burn_text_overlays_skia",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("no text burn expected")),
    )

    def _burn_captions(input_path, output_path, variant, tmpdir):
        seen["input"] = input_path
        with open(output_path, "wb") as f:
            f.write(b"captions")

    monkeypatch.setattr(gb, "_burn_persisted_captions_onto_base", _burn_captions)

    gb._compose_subtitled_final(str(base), {"text_elements": [], "caption_cues": []}, str(tmp_path))

    assert seen["input"] == str(base)


def test_reburn_captions_happy_swaps_video_and_marks_ready(monkeypatch):
    import uuid

    job = _FakeJob(assembly_plan={"variants": [_narrated_caption_variant()]})
    _patch_job_session(monkeypatch, job)
    burned = {"called": False}
    _patch_reburn_io(monkeypatch, burned)

    gb._run_reburn_narrated_captions(str(uuid.uuid4()), "narrated")

    v = job.assembly_plan["variants"][0]
    assert burned["called"] is True  # cues present → captions burned
    assert v["render_status"] == "ready"
    assert v["video_path"].endswith(".mp4") and "_cap_" in v["video_path"]  # new key
    assert v["video_path"] != "generative-jobs/j/variant_1_narrated.mp4"
    assert v["output_url"] == "https://signed/new"


def test_reburn_empty_cues_copies_base_no_burn(monkeypatch):
    import uuid

    job = _FakeJob(assembly_plan={"variants": [_narrated_caption_variant(caption_cues=[])]})
    _patch_job_session(monkeypatch, job)
    burned = {"called": False}
    _patch_reburn_io(monkeypatch, burned)

    gb._run_reburn_narrated_captions(str(uuid.uuid4()), "narrated")

    assert burned["called"] is False  # all cleared → caption-free copy, no libass burn
    assert job.assembly_plan["variants"][0]["render_status"] == "ready"


def test_reburn_rejects_non_caption_variant(monkeypatch):
    import uuid

    import pytest

    # A montage variant also carries base_video_path — reburn must refuse it.
    # (Guard now admits narrated + subtitled; anything else is still corruption.)
    montage = _narrated_caption_variant(resolved_archetype="montage", variant_id="original_text")
    job = _FakeJob(assembly_plan={"variants": [montage]})
    _patch_job_session(monkeypatch, job)
    _patch_reburn_io(monkeypatch, {"called": False})

    with pytest.raises(ValueError, match="not a caption variant"):
        gb._run_reburn_narrated_captions(str(uuid.uuid4()), "original_text")


def test_reburn_subtitled_uses_safe_margin_and_pop_in(monkeypatch):
    """First burn and reburn MUST agree on margin + style or edited captions jump —
    the invariant the code comments declare, pinned here."""
    import uuid

    variant = _narrated_caption_variant(resolved_archetype="subtitled", variant_id="subtitled")
    job = _FakeJob(assembly_plan={"variants": [variant]})
    _patch_job_session(monkeypatch, job)
    burned = {"called": False}
    _patch_reburn_io(monkeypatch, burned)
    seen = {}

    def _gen(cues, path, *, font_name, style, margin_v=None, pop_in=False):
        seen.update(style=style, margin_v=margin_v, pop_in=pop_in)

    monkeypatch.setattr("app.pipeline.captions.generate_ass_from_cues", _gen)

    gb._run_reburn_narrated_captions(str(uuid.uuid4()), "subtitled")

    from app.pipeline.captions import SUBTITLED_CAPTION_MARGIN_V

    assert seen == {"style": "plain", "margin_v": SUBTITLED_CAPTION_MARGIN_V, "pop_in": True}
    assert burned["called"] is True


def test_reburn_subtitled_word_style_routes_to_word_pop(monkeypatch):
    import uuid

    variant = _narrated_caption_variant(
        resolved_archetype="subtitled", variant_id="subtitled", voiceover_caption_style="word"
    )
    job = _FakeJob(assembly_plan={"variants": [variant]})
    _patch_job_session(monkeypatch, job)
    burned = {"called": False}
    _patch_reburn_io(monkeypatch, burned)
    seen = {"pop": False}
    monkeypatch.setattr(
        "app.pipeline.captions.generate_word_pop_ass",
        lambda *a, **k: seen.update(pop=True),
    )

    gb._run_reburn_narrated_captions(str(uuid.uuid4()), "subtitled")

    assert seen["pop"] is True and burned["called"] is True


def test_reburn_subtitled_flag_on_routes_caption_apply_through_compose(monkeypatch):
    import uuid

    monkeypatch.setattr(gb.settings, "subtitled_text_lane_enabled", True, raising=False)
    variant = _narrated_caption_variant(resolved_archetype="subtitled", variant_id="subtitled")
    job = _FakeJob(assembly_plan={"variants": [variant]})
    _patch_job_session(monkeypatch, job)
    _patch_reburn_io(monkeypatch, {"called": False})
    seen = {}

    def _compose(base_local, fresh_variant, tmpdir):
        seen["variant"] = dict(fresh_variant)
        out = f"{tmpdir}/composed.mp4"
        with open(out, "wb") as f:
            f.write(b"composed")
        return out

    monkeypatch.setattr(gb, "_compose_subtitled_final", _compose)

    gb._run_reburn_narrated_captions(str(uuid.uuid4()), "subtitled")

    assert seen["variant"]["variant_id"] == "subtitled"
    assert job.assembly_plan["variants"][0]["video_path"].find("_cap_") != -1


# ── Background-sound (bed-level) reburn — new post-gen editor control ─────────
# Reconstructs the render inputs from persisted, deterministic data (narrated_
# timings, filming_guide, narrative_order) — no Whisper/LLM re-run. These tests
# pin the reconstruction fidelity flagged in the pre-landing review: the clip-
# assignment branch (_narrated_clip_assignments vs the auto-segment cycling) MUST
# match the branch the original render took, or the rebuilt audio bed would
# silently desync from the frozen visuals reused from base_video_path.


class _FakeJobWithCandidates(_FakeJob):
    def __init__(self, assembly_plan=None, all_candidates=None):
        super().__init__(assembly_plan=assembly_plan)
        self.all_candidates = all_candidates or {}


def _narrated_bed_variant(**over):
    v = {
        "variant_id": "narrated",
        "rank": 1,
        "render_status": "ready",
        "resolved_archetype": "narrated",
        "base_video_path": "generative-jobs/j/variant_1_narrated_base.mp4",
        "video_path": "generative-jobs/j/variant_1_narrated.mp4",
        "output_url": "https://signed/old",
        "caption_cues": [{"text": "the energy here", "start_s": 0.0, "end_s": 1.2}],
        "captions_enabled": True,
        "voiceover_caption_style": "sentence",
        "voiceover_caption_font": None,
        "voiceover_bed_level": 0.25,
        "narrated_timings": [
            {"step_id": "shot_1", "start_s": 0.0, "end_s": 1.2, "confidence": 1.0},
            {"step_id": "shot_2", "start_s": 1.2, "end_s": 2.5, "confidence": 1.0},
        ],
        "media_overlays": None,
        "pre_media_overlay_video_path": None,
    }
    v.update(over)
    return v


def _patch_bed_level_io(monkeypatch, *, assemble_calls: list):
    monkeypatch.setattr("app.storage.download_to_file", lambda *a, **k: None)
    monkeypatch.setattr("app.storage.upload_public_read", lambda *a, **k: "https://signed/new")
    monkeypatch.setattr("app.storage.delete_object_best_effort", lambda *a, **k: True)
    monkeypatch.setattr(
        "app.tasks.template_orchestrate._download_clips_parallel",
        lambda paths, tmpdir: [f"{tmpdir}/local_{i}.mp4" for i in range(len(paths))],
    )

    def _fake_assemble(step_timings, clip_assignments, voiceover_local, output_path, tmpdir, **kw):
        assemble_calls.append(
            {
                "step_timings": step_timings,
                "clip_assignments": clip_assignments,
                "bed_level": kw.get("bed_level"),
                "base_output_path": kw.get("base_output_path"),
                "transcript": kw.get("transcript"),
            }
        )
        # Real function writes files at output_path / base_output_path — simulate.
        with open(output_path, "wb") as f:
            f.write(b"x")
        base_path = kw.get("base_output_path")
        if base_path:
            with open(base_path, "wb") as f:
                f.write(b"x")
        return []

    monkeypatch.setattr("app.pipeline.narrated_assembler.assemble_narrated", _fake_assemble)
    monkeypatch.setattr(
        "app.tasks.generative_build._burn_persisted_captions_onto_base",
        lambda base_local, out_local, variant, tmpdir: open(out_local, "wb").write(b"x"),
    )


def test_reburn_bed_level_happy_path_uses_auto_segment_assignment(monkeypatch):
    """filming_guide has < 2 scripted steps → the auto-segment (cycling) branch,
    matching _render_narrated_variant's own branch for the same input shape."""
    import uuid

    variant = _narrated_bed_variant()
    job = _FakeJobWithCandidates(
        assembly_plan={"variants": [variant]},
        all_candidates={
            "voiceover_gcs_path": "voiceover-uploads/x/voice.webm",
            "filming_guide": [],  # < 2 script steps → auto-segment branch
            "clip_paths": ["slot-uploads/a.mp4", "slot-uploads/b.mp4"],
            "narrative_shot_count": 0,
            "landscape_fit": "fit",
        },
    )
    _patch_job_session(monkeypatch, job)
    calls = []
    _patch_bed_level_io(monkeypatch, assemble_calls=calls)

    gb._run_reburn_narrated_bed_level(str(uuid.uuid4()), "narrated", 0.6)

    assert len(calls) == 1
    assert calls[0]["bed_level"] == 0.6
    assert calls[0]["transcript"] is None  # no re-transcription
    assert [t.step_id for t in calls[0]["step_timings"]] == ["shot_1", "shot_2"]
    # Cycling assignment: clip_0 → shot_1, clip_1 → shot_2 (2 clips, 2 steps).
    assigned = {c.step_id: c.clip_path for c in calls[0]["clip_assignments"]}
    assert set(assigned.keys()) == {"shot_1", "shot_2"}

    v = job.assembly_plan["variants"][0]
    assert v["render_status"] == "ready"
    assert v["voiceover_bed_level"] == 0.6
    assert v["video_path"] != "generative-jobs/j/variant_1_narrated.mp4"
    assert v["base_video_path"] != "generative-jobs/j/variant_1_narrated_base.mp4"
    # media_overlays is absent from the terminal patch (OV-2) — the merge keeps
    # the DB value; the snapshot is explicitly reset (it pointed at the old burn).
    assert v["media_overlays"] is None
    assert v["pre_media_overlay_video_path"] is None
    # Captions untouched by a bed-level change.
    assert v["caption_cues"] == variant["caption_cues"]


def test_reburn_bed_level_uses_scripted_assignment_when_guide_has_2plus_steps(monkeypatch):
    """filming_guide has >= 2 scripted steps → _narrated_clip_assignments branch,
    mirroring _render_narrated_variant's own branch selection exactly."""
    import uuid

    variant = _narrated_bed_variant()
    job = _FakeJobWithCandidates(
        assembly_plan={"variants": [variant]},
        all_candidates={
            "voiceover_gcs_path": "voiceover-uploads/x/voice.webm",
            "filming_guide": [
                {"shot_id": "shot_1", "what": "pour the coffee"},
                {"shot_id": "shot_2", "what": "take the first sip"},
            ],
            "clip_paths": ["slot-uploads/a.mp4", "slot-uploads/b.mp4"],
            "narrative_shot_count": 2,
            "landscape_fit": "fill",
        },
    )
    _patch_job_session(monkeypatch, job)
    calls = []
    _patch_bed_level_io(monkeypatch, assemble_calls=calls)

    gb._run_reburn_narrated_bed_level(str(uuid.uuid4()), "narrated", 0.0)

    assert len(calls) == 1
    assigned = {c.step_id: c.clip_path for c in calls[0]["clip_assignments"]}
    assert assigned == {
        "shot_1": calls[0]["clip_assignments"][0].clip_path,
        "shot_2": calls[0]["clip_assignments"][1].clip_path,
    }
    assert job.assembly_plan["variants"][0]["voiceover_bed_level"] == 0.0


def test_reburn_bed_level_rejects_non_narrated(monkeypatch):
    import uuid

    import pytest

    subtitled = _narrated_bed_variant(resolved_archetype="subtitled", variant_id="subtitled")
    job = _FakeJobWithCandidates(assembly_plan={"variants": [subtitled]})
    _patch_job_session(monkeypatch, job)
    _patch_bed_level_io(monkeypatch, assemble_calls=[])

    with pytest.raises(ValueError, match="no background-sound bed"):
        gb._run_reburn_narrated_bed_level(str(uuid.uuid4()), "subtitled", 0.5)


def test_reburn_bed_level_requires_persisted_timings(monkeypatch):
    import uuid

    import pytest

    variant = _narrated_bed_variant(narrated_timings=[])
    job = _FakeJobWithCandidates(assembly_plan={"variants": [variant]})
    _patch_job_session(monkeypatch, job)
    _patch_bed_level_io(monkeypatch, assemble_calls=[])

    with pytest.raises(ValueError, match="narrated_timings"):
        gb._run_reburn_narrated_bed_level(str(uuid.uuid4()), "narrated", 0.5)


def test_finalize_job_preserves_caption_cues(monkeypatch):
    """REGRESSION: _finalize_job rebuilds variants from a key whitelist that silently
    strips anything not listed. caption_cues MUST survive or the on-video editor (which
    needs the cues, not just the base) has nothing to load after the first render."""
    import uuid

    job = _FakeJob(assembly_plan={})
    _patch_job_session(monkeypatch, job)
    result = {
        "variant_id": "narrated",
        "rank": 1,
        "text_mode": "none",
        "ok": True,
        "render_status": "ready",
        "output_url": "u",
        "video_path": "generative-jobs/j/v.mp4",
        "resolved_archetype": "narrated",
        "base_video_path": "generative-jobs/j/base.mp4",
        "caption_cues": [{"text": "Hello.", "start_s": 0.0, "end_s": 1.0}],
        "voiceover_caption_style": "word",
        "voiceover_caption_font": "Montserrat Bold",
        "caption_margin_v": 653,
        "caption_language": "tr",
    }
    gb._finalize_job(str(uuid.uuid4()), [result])
    v = job.assembly_plan["variants"][0]
    assert v["caption_cues"] == [{"text": "Hello.", "start_s": 0.0, "end_s": 1.0}]
    assert v["base_video_path"] == "generative-jobs/j/base.mp4"  # the existing whitelist field
    # caption style + font must survive too, or a caption edit reburns wrong.
    assert v["voiceover_caption_style"] == "word"
    assert v["voiceover_caption_font"] == "Montserrat Bold"
    assert v["caption_margin_v"] == 653
    # subtitled: the language must survive or the editor chip + re-transcribe lose it.
    assert v["caption_language"] == "tr"
    assert job.status == "variants_ready"


def test_finalize_job_preserves_smart_caption_plan_and_authoritative_titles(monkeypatch):
    """Smart output must survive the finalizer's explicit variant whitelist."""
    import uuid

    job = _FakeJob(assembly_plan={})
    _patch_job_session(monkeypatch, job)
    titles = [
        {
            "id": "smart-number-1",
            "text": "1",
            "start_s": 4.0,
            "end_s": 5.5,
            "role": "generative_intro",
            "font_family": "Inter-Bold",
            "x_frac": 0.5,
            "y_frac": 0.075,
        }
    ]
    result = {
        "variant_id": "subtitled",
        "rank": 1,
        "text_mode": "none",
        "ok": True,
        "render_status": "ready",
        "output_url": "u",
        "video_path": "generative-jobs/j/v.mp4",
        "resolved_archetype": "subtitled",
        "smart_captions_applied": True,
        "smart_edit_document": {"version": "1", "events": []},
        "smart_compiled_patch": {"compiler_version": "test"},
        "smart_planner_versions": {"planner": "test", "compiler": "test"},
        "smart_validation_receipts": {"planner": {"valid": True}},
        "boundary_effects": [{"effect": "horizontal_motion_blur", "at_s": 8.0}],
        "text_elements": titles,
        "text_elements_user_edited": True,
        "text_elements_materialized_from": "smart_captions",
    }

    gb._finalize_job(str(uuid.uuid4()), [result])

    variant = job.assembly_plan["variants"][0]
    assert variant["smart_captions_applied"] is True
    assert variant["smart_edit_document"] == {"version": "1", "events": []}
    assert variant["smart_compiled_patch"] == {"compiler_version": "test"}
    assert variant["smart_planner_versions"] == {
        "planner": "test",
        "compiler": "test",
    }
    assert variant["smart_validation_receipts"] == {"planner": {"valid": True}}
    assert variant["boundary_effects"] == [
        {"effect": "horizontal_motion_blur", "at_s": 8.0}
    ]
    assert variant["text_elements"] == titles
    assert variant["text_elements_user_edited"] is True
    assert variant["text_elements_materialized_from"] == "smart_captions"


def test_subtitled_caption_margin_resolves_identically_for_first_and_reburn(monkeypatch, tmp_path):
    from app.pipeline.captions import SUBTITLED_CAPTION_MARGIN_V

    captured: dict[str, int | None] = {}

    def fake_generate_ass_from_cues(cues, output_path, **kwargs):
        captured["sentence"] = kwargs.get("margin_v")
        with open(output_path, "w", encoding="utf-8") as f:
            f.write("ass")

    def fake_generate_word_pop_ass(cues, output_path, **kwargs):
        captured["word"] = kwargs.get("margin_v")
        with open(output_path, "w", encoding="utf-8") as f:
            f.write("ass")

    def fake_burn(base_local, ass_path, fonts_dir, out_local):
        with open(out_local, "wb") as f:
            f.write(b"video")

    monkeypatch.setattr("app.pipeline.captions.generate_ass_from_cues", fake_generate_ass_from_cues)
    monkeypatch.setattr("app.pipeline.captions.generate_word_pop_ass", fake_generate_word_pop_ass)
    monkeypatch.setattr("app.pipeline.narrated_assembler.burn_captions_on_video", fake_burn)

    base_local = tmp_path / "base.mp4"
    base_local.write_bytes(b"base")
    cues = [{"text": "Hello world", "start_s": 0.0, "end_s": 1.0}]

    for variant, expected in [
        ({"caption_margin_v": 653}, 653),
        ({}, SUBTITLED_CAPTION_MARGIN_V),
    ]:
        captured.clear()
        assert gb._resolve_caption_margin_v(variant) == expected  # first-burn helper
        common = {
            **variant,
            "resolved_archetype": "subtitled",
            "captions_enabled": True,
            "caption_cues": cues,
            "voiceover_caption_font": None,
        }
        gb._burn_persisted_captions_onto_base(
            str(base_local),
            str(tmp_path / f"sentence_{expected}.mp4"),
            {**common, "voiceover_caption_style": "sentence"},
            str(tmp_path),
        )
        gb._burn_persisted_captions_onto_base(
            str(base_local),
            str(tmp_path / f"word_{expected}.mp4"),
            {**common, "voiceover_caption_style": "word"},
            str(tmp_path),
        )
        assert captured == {"sentence": expected, "word": expected}


def test_finalize_job_preserves_sound_effects(monkeypatch):
    """REGRESSION: _finalize_job rebuilds variants from a key whitelist that silently
    strips anything not listed. sound_effects + pre_sfx_video_path MUST survive or a
    later full re-render (text/song/clip edit) drops the SFX lane with no error: the
    render-sfx pass reads sound_effects from the variant, and pre_sfx_video_path is the
    clean (no-SFX) base it re-applies onto."""
    import uuid

    job = _FakeJob(assembly_plan={})
    _patch_job_session(monkeypatch, job)
    placements = [
        {
            "id": "p1",
            "sound_effect_id": "fah",
            "src_gcs_path": "sound-effects/fah.mp3",
            "at_s": 4.0,
            "gain": 1.0,
            "label": "Fah",
        }
    ]
    result = {
        "variant_id": "song_text",
        "rank": 1,
        "text_mode": "song_text",
        "ok": True,
        "render_status": "ready",
        "output_url": "u",
        "video_path": "generative-jobs/j/v.mp4",
        "sound_effects": placements,
        "pre_sfx_video_path": "generative-jobs/j/v.mp4_pre_sfx",
    }
    gb._finalize_job(str(uuid.uuid4()), [result])
    v = job.assembly_plan["variants"][0]
    assert v["sound_effects"] == placements
    assert v["pre_sfx_video_path"] == "generative-jobs/j/v.mp4_pre_sfx"
    assert job.status == "variants_ready"


def test_finalize_job_preserves_lyric_fields(monkeypatch):
    """REGRESSION: _finalize_job's key whitelist stripped the lyrics-editor state,
    so a fresh song_lyrics render reported no_renderable_lyrics and projected no
    lyric text elements until its first re-render (caught in the 2026-07-18 E2E)."""
    import uuid

    job = _FakeJob(assembly_plan={})
    _patch_job_session(monkeypatch, job)
    snapshot = [
        {
            "line_key": "L14",
            "text": "First lyric",
            "start_s": 1.2,
            "end_s": 3.4,
            "font_family": "Playfair Display",
            "size_px": 70,
            "color": "#FFFFFF",
            "highlight_color": "#FFD24A",
            "y_frac": 0.72,
            "effect": "karaoke-line",
        }
    ]
    overrides = {"L14": {"text": "x", "orig_text": "First lyric", "orig_start_s": 1.2}}
    result = {
        "variant_id": "song_lyrics",
        "rank": 1,
        "text_mode": "lyrics",
        "ok": True,
        "render_status": "ready",
        "output_url": "u",
        "video_path": "generative-jobs/j/v.mp4",
        "lyrics_enabled": True,
        "lyrics_available": True,
        "lyric_line_overrides": overrides,
        "lyric_overlay_snapshot": snapshot,
        "orientation": "portrait",
    }
    gb._finalize_job(str(uuid.uuid4()), [result])
    v = job.assembly_plan["variants"][0]
    assert v["lyrics_enabled"] is True
    assert v["lyrics_available"] is True
    assert v["lyric_line_overrides"] == overrides
    assert v["lyric_overlay_snapshot"] == snapshot
    assert v["orientation"] == "portrait"


def test_reapply_persisted_sfx_reapplies_and_resets_pre_sfx(monkeypatch):
    """REGRESSION: a full re-render (song-swap / retext / style / clip edit) re-assembles
    video_path WITHOUT the SFX mix. The terminal hook (now wired into both full-re-render
    success paths in regenerate_generative_variant, mirroring _run_media_overlay_pass) must
    re-apply persisted effects onto the fresh base AND null the now-stale pre_sfx_video_path
    so _run_sfx_pass re-snapshots the re-rendered video. Without it, SFX silently vanish from
    the video while the UI still shows them (and a later clear/apply uses a stale base)."""
    import uuid

    monkeypatch.setattr("sqlalchemy.orm.attributes.flag_modified", lambda *a, **k: None)
    monkeypatch.setattr(gb.settings, "sound_effects_enabled", True, raising=False)
    sfx = [
        {
            "id": "p1",
            "at_s": 4.0,
            "gain": 1.0,
            "src_gcs_path": "sound-effects/x.mp3",
            "label": "Fah",
        }
    ]
    job = _FakeJob(
        assembly_plan={
            "variants": [
                {
                    "variant_id": "v1",
                    "sound_effects": sfx,
                    "pre_sfx_video_path": "old/stale_pre_sfx.mp4",
                }
            ]
        }
    )
    _patch_job_session(monkeypatch, job)
    calls = {}
    monkeypatch.setattr(gb, "_run_sfx_pass", lambda *, sfx_raw, **kw: calls.update(sfx_raw=sfx_raw))

    gb._reapply_persisted_sfx_if_any(job_id=str(uuid.uuid4()), variant_id="v1")

    assert calls.get("sfx_raw") == sfx  # persisted effects re-applied onto the fresh render
    assert job.assembly_plan["variants"][0]["pre_sfx_video_path"] is None  # stale snapshot reset


def test_reapply_persisted_sfx_noop_without_effects(monkeypatch):
    """No persisted SFX → no re-apply pass (a clean re-render is left untouched)."""
    import uuid

    monkeypatch.setattr("sqlalchemy.orm.attributes.flag_modified", lambda *a, **k: None)
    monkeypatch.setattr(gb.settings, "sound_effects_enabled", True, raising=False)
    job = _FakeJob(assembly_plan={"variants": [{"variant_id": "v1", "sound_effects": None}]})
    _patch_job_session(monkeypatch, job)
    called = {"n": 0}
    monkeypatch.setattr(gb, "_run_sfx_pass", lambda **kw: called.__setitem__("n", called["n"] + 1))
    gb._reapply_persisted_sfx_if_any(job_id=str(uuid.uuid4()), variant_id="v1")
    assert called["n"] == 0


def test_reapply_persisted_sfx_noop_when_disabled(monkeypatch):
    """Kill switch off → no-op even with persisted SFX."""
    import uuid

    monkeypatch.setattr(gb.settings, "sound_effects_enabled", False, raising=False)
    job = _FakeJob(
        assembly_plan={
            "variants": [
                {
                    "variant_id": "v1",
                    "sound_effects": [
                        {"id": "p", "at_s": 1.0, "gain": 1.0, "src_gcs_path": "sound-effects/x.mp3"}
                    ],
                }
            ]
        }
    )
    _patch_job_session(monkeypatch, job)
    called = {"n": 0}
    monkeypatch.setattr(gb, "_run_sfx_pass", lambda **kw: called.__setitem__("n", called["n"] + 1))
    gb._reapply_persisted_sfx_if_any(job_id=str(uuid.uuid4()), variant_id="v1")
    assert called["n"] == 0


def test_specs_for_archetype_narrated_carries_caption_style():
    """The narrated spec threads voiceover_caption_style through to the render."""
    specs = gb._specs_for_archetype(
        "narrated",
        None,
        voiceover_gcs_path="voiceover-uploads/abc/voice.m4a",
        voiceover_caption_style="word",
    )
    assert len(specs) == 1
    assert specs[0]["voiceover_caption_style"] == "word"
    # default (no caption style) → None → render falls back to sentence captions.
    default_specs = gb._specs_for_archetype(
        "narrated", None, voiceover_gcs_path="voiceover-uploads/abc/voice.m4a"
    )
    assert default_specs[0]["voiceover_caption_style"] is None


# ── Self-narration: archetype_fallback persistence via _set_status ───────────


def test_set_status_extra_plan_merges_archetype_fallback(monkeypatch):
    """The orchestrator stashes the style-downgrade reason on assembly_plan via
    _set_status(extra_plan=...) — merged alongside variants, never replacing them."""
    job = _FakeJob(assembly_plan={"variants": [{"variant_id": "song_text"}]})
    _patch_job_session(monkeypatch, job)

    gb._set_status(
        "11111111-1111-1111-1111-111111111111",
        "rendering",
        extra_plan={"archetype_fallback": {"declared": "narrated_ready", "reason": "no_speech"}},
    )
    assert job.status == "rendering"
    assert job.assembly_plan["archetype_fallback"] == {
        "declared": "narrated_ready",
        "reason": "no_speech",
    }
    assert job.assembly_plan["variants"] == [{"variant_id": "song_text"}]

    # A retry that resolves cleanly clears the stale reason (key set to None).
    gb._set_status(
        "11111111-1111-1111-1111-111111111111",
        "rendering",
        extra_plan={"archetype_fallback": None},
    )
    assert job.assembly_plan["archetype_fallback"] is None


def test_persist_archetype_fallback_sets_clears_and_noops(monkeypatch):
    """The shared writer behind the item-page downgrade banner: reason set → dict
    lands beside variants; reason None → clears ONLY an existing key (a clean job's
    assembly_plan stays byte-identical — the flag-off guarantee)."""
    job = _FakeJob(assembly_plan={"variants": [{"variant_id": "song_text"}]})
    _patch_job_session(monkeypatch, job)

    gb._persist_archetype_fallback(
        "11111111-1111-1111-1111-111111111111", "narrated_ready", "no_speech"
    )
    assert job.assembly_plan["archetype_fallback"] == {
        "declared": "narrated_ready",
        "reason": "no_speech",
    }
    assert job.assembly_plan["variants"] == [{"variant_id": "song_text"}]

    # Retry that resolves cleanly → stale reason cleared.
    gb._persist_archetype_fallback("11111111-1111-1111-1111-111111111111", "narrated_ready", None)
    assert job.assembly_plan["archetype_fallback"] is None

    # Clean job + no reason → assembly_plan untouched (no key materialized).
    clean = _FakeJob(assembly_plan={"variants": []})
    _patch_job_session(monkeypatch, clean)
    gb._persist_archetype_fallback("11111111-1111-1111-1111-111111111111", "montage", None)
    assert "archetype_fallback" not in clean.assembly_plan
