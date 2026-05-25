"""Orchestrator-level tests for the generative-edit pipeline.

These mock the heavy ingest/render helpers — the goal is to pin the orchestration
LOGIC: variant spec construction, the no-music audio-passthrough branch, the Skia
kill-switch guard, energy derivation, terminal-status calculation, and the
output_url-is-signed contract. The real render is verified separately (make
local-render) per CLAUDE.md.
"""

from __future__ import annotations

import types

import app.tasks.generative_build as gb


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
        lyrics_cached=lyrics_cached if lyrics_cached is not None else {"lines": [{"text": "hi"}]},
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
        tm, "match", lambda recipe, metas: types.SimpleNamespace(steps=[]), raising=False
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


def test_song_variant_calls_mix(monkeypatch, tmp_path):
    mix_calls: list = []
    _patch_render_helpers(monkeypatch, mix_calls)
    # generate_music_recipe is also lazily imported — stub it.
    import app.pipeline.music_recipe as mr

    monkeypatch.setattr(
        mr,
        "generate_music_recipe",
        lambda td: {
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
