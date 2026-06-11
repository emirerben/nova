"""Clip timeline editor — orchestrator-level tests.

These mock the heavy ingest/render helpers (same approach as
test_generative_build.py) to pin the timeline LOGIC: durable source copies,
ai_timeline persistence from the post-resolution assembly, the user_timeline
override render path (skip ingest+Gemini+match), failure-patch hygiene, and
hook regrounding. The real exact-window render is verified separately
(make local-render) per CLAUDE.md.
"""

from __future__ import annotations

import types

import app.tasks.generative_build as gb

JOB_ID = "12345678-1234-5678-1234-567812345678"

CLIP_PATHS = [
    f"generative-jobs/{JOB_ID}/sources/000_a.mp4",
    f"generative-jobs/{JOB_ID}/sources/001_b.mp4",
    f"generative-jobs/{JOB_ID}/sources/002_c.mp4",
]


class _Meta:
    def __init__(self, clip_id, hook_score, best_moments=None, transcript="", detected_subject=""):
        self.clip_id = clip_id
        self.hook_score = hook_score
        self.best_moments = best_moments or []
        self.transcript = transcript
        self.detected_subject = detected_subject
        self.hook_text = ""


class _Probe:
    def __init__(self, duration_s):
        self.duration_s = duration_s


def _track(track_id="t1"):
    return types.SimpleNamespace(
        id=track_id,
        title="Song A",
        audio_gcs_path=f"music/{track_id}/audio.m4a",
        beat_timestamps_s=[0.5, 1.0, 1.5, 2.0],
        duration_s=60.0,
        track_config={"best_start_s": 0.0, "best_end_s": 30.0},
        ai_labels={"labels": {}},
        analysis_status="ready",
        lyrics_cached={"lines": [{"text": "hi"}]},
    )


def _tl_slot(clip_index, *, in_s=1.0, duration_s=2.0, removed=False, **extra):
    return {
        "slot_id": "a" * 32,
        "clip_index": clip_index,
        "source_gcs_path": CLIP_PATHS[clip_index] if 0 <= clip_index < len(CLIP_PATHS) else "x",
        "source_duration_s": 6.0,
        "in_s": in_s,
        "duration_s": duration_s,
        "duration_beats": None,
        "order": 0,
        "moment_energy": 6.0,
        "moment_description": "user slot",
        "removed": removed,
        **extra,
    }


def _existing_variant(variant_id="original_text", **extra):
    v = {
        "variant_id": variant_id,
        "rank": 3,
        "text_mode": "none",
        "music_track_id": None,
        "intro_text": None,
        "intro_highlight_word": None,
        "intro_text_size_px": None,
        "intro_size_source": None,
        "video_path": f"generative-jobs/{JOB_ID}/variant_3_{variant_id}.mp4",
        "output_url": "https://signed/last-good",
        "base_video_path": None,
        "style_set_id": None,
        "ok": True,
    }
    v.update(extra)
    return v


class _FakeJob:
    def __init__(self, clip_paths, variants):
        self.all_candidates = {"clip_paths": list(clip_paths)}
        self.assembly_plan = {"variants": list(variants)}
        self.status = "variants_ready"
        self.mode = "generative"


def _patch_sessions(monkeypatch, job, track=None):
    """gb._sync_session() → session dispatching Job vs MusicTrack lookups."""

    class _Sess:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def get(self, model, pk, **kw):
            if model is gb.Job:
                return job
            return track

        def commit(self):
            pass

    monkeypatch.setattr(gb, "_sync_session", lambda: _Sess())


def _capture_updates(monkeypatch):
    updates: list[dict] = []
    monkeypatch.setattr(
        gb,
        "_update_variant_entry",
        lambda jid, vid, patch: updates.append(dict(patch)),
        raising=False,
    )
    return updates


def _patch_render_helpers(monkeypatch, mix_calls: list, assembled_steps: list | None = None):
    """test_generative_build.py's `_patch_render_helpers`, extended: the fake
    `_assemble_clips` swallows new kwargs AND fills `resolved_plans_out` when
    passed (from each step's moment, index-aligned — the real sink's contract)
    so ai_timeline persistence is testable. Optionally records the steps it
    was handed via `assembled_steps`."""
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
        if assembled_steps is not None:
            assembled_steps.append(list(steps))
        sink = kw.get("resolved_plans_out")
        if sink is not None:
            for step in steps:
                moment = getattr(step, "moment", None) or {}
                start_s = float(moment.get("start_s", 0.0))
                end_s = float(moment.get("end_s", start_s))
                sink.append(
                    {
                        "clip_id": step.clip_id,
                        "start_s": start_s,
                        "end_s": end_s,
                        "duration_s": end_s - start_s,
                        "speed_factor": 1.0,
                    }
                )
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


def _patch_music_recipe(monkeypatch, beats):
    import app.pipeline.music_recipe as mr

    monkeypatch.setattr(
        mr,
        "generate_music_recipe",
        lambda td: {
            "slots": [{"position": 1, "target_duration_s": 1.0, "text_overlays": []}],
            "beat_timestamps_s": list(beats),
        },
        raising=False,
    )


def _patch_timeline_io(monkeypatch, *, duration_s=6.0):
    """Stub the download+probe-only leg of _prepare_timeline_assembly."""
    import os

    import app.tasks.template_orchestrate as to

    dl_calls: list[list[str]] = []

    def _fake_download(gcs_list, tmpdir):
        dl_calls.append(list(gcs_list))
        return [os.path.join(tmpdir, f"dl_{i}.mp4") for i in range(len(gcs_list))]

    monkeypatch.setattr(to, "_download_clips_parallel", _fake_download, raising=False)
    monkeypatch.setattr(
        to,
        "_probe_clips",
        lambda paths: {p: _Probe(duration_s) for p in paths},
        raising=False,
    )
    return dl_calls


def _regen_setup(
    monkeypatch,
    *,
    variants,
    clip_paths=None,
    track=None,
    assembled_steps=None,
    mix_calls=None,
):
    """Wire everything _run_regenerate_variant needs to run without DB/ffmpeg/LLM."""
    job = _FakeJob(clip_paths or CLIP_PATHS, variants)
    _patch_sessions(monkeypatch, job, track=track)
    updates = _capture_updates(monkeypatch)
    _patch_render_helpers(
        monkeypatch, mix_calls if mix_calls is not None else [], assembled_steps=assembled_steps
    )
    dl_calls = _patch_timeline_io(monkeypatch)
    # The fresh-match leg's text fallback must never hit a real LLM in tests.
    monkeypatch.setattr(gb, "_run_text_agents", lambda *a, **k: (None, None), raising=False)
    return job, updates, dl_calls


# ── ai_timeline persistence (full montage assembly) ─────────────────────────────


def test_ai_timeline_persisted_with_windows_clip_index_and_beat_grid(monkeypatch, tmp_path):
    """The defining persistence assertion: ai_timeline carries POST-resolution
    windows, clip_index reverse-mapped through clip_paths order, and the
    section-relative beat grid with whole-beat span counts."""
    import app.pipeline.template_matcher as tm

    assembled: list = []
    _patch_render_helpers(monkeypatch, [], assembled_steps=assembled)
    _patch_music_recipe(monkeypatch, [0.0, 1.0, 2.0, 3.0])

    steps = [
        types.SimpleNamespace(
            clip_id="c2",
            slot={"position": 1},
            moment={"start_s": 1.0, "end_s": 3.0, "energy": 7.0, "description": "sunset run"},
        ),
        types.SimpleNamespace(
            clip_id="c1",
            slot={"position": 2},
            moment={"start_s": 0.5, "end_s": 1.5, "energy": 4.0, "description": "walking"},
        ),
    ]
    monkeypatch.setattr(
        tm, "match", lambda recipe, metas, **kw: types.SimpleNamespace(steps=steps), raising=False
    )

    vdir = tmp_path / "v1"
    vdir.mkdir()
    spec = {"variant_id": "song_text", "rank": 1, "text_mode": "none", "track": _track()}
    res = gb._render_generative_variant(
        job_id="j",
        rank=1,
        spec=spec,
        clip_metas=[_Meta("c1", 5.0), _Meta("c2", 4.0)],
        clip_id_to_local={"c1": "/a.mp4", "c2": "/b.mp4"},
        clip_id_to_gcs={"c1": CLIP_PATHS[0], "c2": CLIP_PATHS[1]},
        probe_map={"/a.mp4": _Probe(10.0), "/b.mp4": _Probe(8.0)},
        available_footage_s=18.0,
        agent_text=None,
        agent_form={},
        variant_dir=str(vdir),
    )

    assert res["ok"] is True
    tl = res["ai_timeline"]
    assert tl["beat_grid"] == [0.0, 1.0, 2.0, 3.0]
    s0, s1 = tl["slots"]
    # clip_index = position in clip_paths (via clip_id_to_gcs values order).
    assert s0["clip_index"] == 1 and s0["source_gcs_path"] == CLIP_PATHS[1]
    assert s1["clip_index"] == 0 and s1["source_gcs_path"] == CLIP_PATHS[0]
    # Post-resolution windows (from the resolved_plans_out sink).
    assert s0["in_s"] == 1.0 and s0["duration_s"] == 2.0
    assert s1["in_s"] == 0.5 and s1["duration_s"] == 1.0
    # Whole-beat span counts: 2.0s over a 1s grid = 2 beats; 1.0s = 1 beat.
    assert s0["duration_beats"] == 2 and s1["duration_beats"] == 1
    assert s0["source_duration_s"] == 8.0 and s1["source_duration_s"] == 10.0
    assert s0["order"] == 0 and s1["order"] == 1
    assert s0["moment_energy"] == 7.0 and s0["moment_description"] == "sunset run"
    assert len(s0["slot_id"]) == 32 and s0["slot_id"] != s1["slot_id"]


def test_ai_timeline_no_music_has_empty_beat_grid_and_null_beats(monkeypatch, tmp_path):
    import app.pipeline.template_matcher as tm

    _patch_render_helpers(monkeypatch, [])
    steps = [
        types.SimpleNamespace(
            clip_id="c1",
            slot={"position": 1},
            moment={"start_s": 0.0, "end_s": 2.0, "energy": 5.0, "description": ""},
        ),
    ]
    monkeypatch.setattr(
        tm, "match", lambda recipe, metas, **kw: types.SimpleNamespace(steps=steps), raising=False
    )
    vdir = tmp_path / "v3"
    vdir.mkdir()
    spec = {"variant_id": "original_text", "rank": 3, "text_mode": "none", "track": None}
    res = gb._render_generative_variant(
        job_id="j",
        rank=3,
        spec=spec,
        clip_metas=[_Meta("c1", 5.0)],
        clip_id_to_local={"c1": "/a.mp4"},
        clip_id_to_gcs={"c1": CLIP_PATHS[0]},
        probe_map={"/a.mp4": _Probe(6.0)},
        available_footage_s=6.0,
        agent_text=None,
        agent_form={},
        variant_dir=str(vdir),
    )
    assert res["ok"] is True
    assert res["ai_timeline"]["beat_grid"] == []
    assert res["ai_timeline"]["slots"][0]["duration_beats"] is None


def test_ai_timeline_skipped_for_voiceover_variant(monkeypatch, tmp_path):
    """Voiceover spine variants carry NO timeline — the voice drives the layout."""
    import app.storage as storage
    import app.tasks.template_orchestrate as to

    _patch_render_helpers(monkeypatch, [])
    monkeypatch.setattr(storage, "download_to_file", lambda gcs, local: None, raising=False)
    monkeypatch.setattr(to, "_probe_duration", lambda p: 5.0, raising=False)

    def _fake_vo(video, voice, out, tmpdir, **kw):
        with open(out, "wb") as f:
            f.write(b"\x00" * 16)

    monkeypatch.setattr(to, "_mix_user_voiceover", _fake_vo, raising=False)

    vdir = tmp_path / "vv"
    vdir.mkdir()
    spec = {
        "variant_id": "voiceover_only",
        "rank": 1,
        "text_mode": "none",
        "track": None,
        "voiceover_gcs_path": "voiceover-uploads/abc/voice.webm",
        "mix": 1.0,
    }
    res = gb._render_generative_variant(
        job_id="j",
        rank=1,
        spec=spec,
        clip_metas=[_Meta("c1", 5.0)],
        clip_id_to_local={"c1": "/a.mp4"},
        clip_id_to_gcs={"c1": CLIP_PATHS[0]},
        probe_map={},
        available_footage_s=12.0,
        agent_text=None,
        agent_form={},
        variant_dir=str(vdir),
    )
    assert res["ok"] is True
    assert res["ai_timeline"] is None


def test_ai_timeline_skipped_when_kill_switch_off(monkeypatch, tmp_path):
    monkeypatch.setattr(gb.settings, "GENERATIVE_TIMELINE_EDITOR_ENABLED", False, raising=False)
    _patch_render_helpers(monkeypatch, [])
    vdir = tmp_path / "v3"
    vdir.mkdir()
    spec = {"variant_id": "original_text", "rank": 3, "text_mode": "none", "track": None}
    res = gb._render_generative_variant(
        job_id="j",
        rank=3,
        spec=spec,
        clip_metas=[_Meta("c1", 5.0)],
        clip_id_to_local={"c1": "/a.mp4"},
        clip_id_to_gcs={"c1": CLIP_PATHS[0]},
        probe_map={},
        available_footage_s=6.0,
        agent_text=None,
        agent_form={},
        variant_dir=str(vdir),
    )
    assert res["ok"] is True
    assert res["ai_timeline"] is None


# ── duration_beats derivation ────────────────────────────────────────────────────


def test_derive_duration_beats_walks_cursor():
    grid = [0.0, 1.0, 2.0, 3.0, 4.0]
    assert gb._derive_duration_beats([2.0, 1.0, 1.0], grid) == [2, 1, 1]


def test_derive_duration_beats_off_grid_is_none():
    grid = [0.0, 1.0, 2.0, 3.0]
    # 1.4s matches no consecutive span within 50ms.
    assert gb._derive_duration_beats([1.4], grid) == [None]


def test_derive_duration_beats_empty_grid_all_none():
    assert gb._derive_duration_beats([1.0, 2.0], []) == [None, None]


# ── Durable source copies ────────────────────────────────────────────────────────


def test_durable_sources_rewrites_order_preserving(monkeypatch):
    import app.storage as storage

    copies: list[tuple[str, str]] = []
    monkeypatch.setattr(
        storage, "copy_object", lambda src, dst: copies.append((src, dst)), raising=False
    )
    originals = ["music-uploads/a.mp4", "slot-uploads/b.mov"]
    job = _FakeJob(originals, [])
    _patch_sessions(monkeypatch, job)

    out = gb._persist_durable_sources(JOB_ID, list(originals))

    prefix = f"generative-jobs/{JOB_ID}/sources/"
    assert out == [f"{prefix}000_a.mp4", f"{prefix}001_b.mov"]  # strictly order-preserving
    assert copies == [(originals[0], out[0]), (originals[1], out[1])]
    assert job.all_candidates["clip_paths"] == out  # persisted on the job row


def test_durable_sources_copy_failure_keeps_all_originals(monkeypatch):
    """All-or-nothing: one failed copy → the ENTIRE original list survives (no mix)."""
    import app.storage as storage

    calls = {"n": 0}

    def _flaky_copy(src, dst):
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("gcs boom")

    monkeypatch.setattr(storage, "copy_object", _flaky_copy, raising=False)
    originals = ["music-uploads/a.mp4", "slot-uploads/b.mov", "music-uploads/c.mp4"]
    job = _FakeJob(originals, [])
    _patch_sessions(monkeypatch, job)

    out = gb._persist_durable_sources(JOB_ID, list(originals))

    assert out == originals  # never a mixed durable/original list
    assert job.all_candidates["clip_paths"] == originals  # DB untouched


def test_durable_sources_idempotent_when_already_durable(monkeypatch):
    """Acks_late re-run: an all-durable list copies nothing and stays identical."""
    import app.storage as storage

    def _boom(src, dst):
        raise AssertionError("already-durable paths must not be re-copied")

    monkeypatch.setattr(storage, "copy_object", _boom, raising=False)
    job = _FakeJob(CLIP_PATHS, [])
    _patch_sessions(monkeypatch, job)

    assert gb._persist_durable_sources(JOB_ID, list(CLIP_PATHS)) == CLIP_PATHS


def test_durable_sources_kill_switch_keeps_originals(monkeypatch):
    import app.storage as storage

    monkeypatch.setattr(gb.settings, "GENERATIVE_TIMELINE_EDITOR_ENABLED", False, raising=False)
    monkeypatch.setattr(
        storage,
        "copy_object",
        lambda *a: (_ for _ in ()).throw(AssertionError("no copies when disabled")),
        raising=False,
    )
    originals = ["music-uploads/a.mp4"]
    assert gb._persist_durable_sources(JOB_ID, list(originals)) == originals


# ── Override resolution order + override render path ───────────────────────────


def test_timeline_override_kwarg_beats_persisted_user_timeline(monkeypatch):
    """Explicit kwarg wins over the variant's persisted user_timeline."""
    assembled: list = []
    variant = _existing_variant(user_timeline={"slots": [_tl_slot(0)]})
    _job, updates, _dl = _regen_setup(monkeypatch, variants=[variant], assembled_steps=assembled)

    gb._run_regenerate_variant(
        JOB_ID,
        "original_text",
        None,
        None,
        False,
        timeline_override=[_tl_slot(1, in_s=2.0, duration_s=1.5)],
    )

    steps = assembled[-1]
    assert [s.clip_id for s in steps] == ["clip_1"]  # kwarg's clip, not persisted clip 0
    assert steps[0].slot["exact_window"] is True
    assert steps[0].slot["slot_type"] == "broll"
    assert steps[0].moment["start_s"] == 2.0 and steps[0].moment["end_s"] == 3.5
    assert updates[-1]["ok"] is True


def test_persisted_user_timeline_drops_removed_and_honors_order(monkeypatch):
    """No kwarg → persisted user_timeline drives assembly: removed slots are
    dropped and the remaining slots render in list order. The variant's
    persisted ai_timeline is CARRIED FORWARD — the override steps are the
    USER's cut, not an AI cut, so rebuilding ai_timeline here would make
    "Reset to AI cut" re-render the user's own edit (B3). Carry-forward =
    the key is ABSENT from the success patch ({**v, **patch} keeps the stored
    value); it must never be None (that would flip the variant uneditable)."""
    assembled: list = []
    ai_marker = {"beat_grid": [], "slots": [{"slot_id": "ai-s1", "clip_index": 0}]}
    variant = _existing_variant(
        ai_timeline=ai_marker,
        user_timeline={
            "slots": [
                _tl_slot(2, in_s=0.5, duration_s=2.0),
                _tl_slot(0, removed=True),
                _tl_slot(1, in_s=1.0, duration_s=1.0),
            ]
        },
    )
    _job, updates, _dl = _regen_setup(monkeypatch, variants=[variant], assembled_steps=assembled)

    gb._run_regenerate_variant(JOB_ID, "original_text", None, None, False)

    steps = assembled[-1]
    assert [s.clip_id for s in steps] == ["clip_2", "clip_1"]  # order honored, removed dropped
    assert [s.slot["position"] for s in steps] == [1, 2]

    final = updates[-1]
    assert final["ok"] is True
    assert "ai_timeline" not in final  # carried forward via merge — not rebuilt, not nulled
    # The stored AI plan is untouched on the variant entry.
    assert _job.assembly_plan["variants"][0]["ai_timeline"] == ai_marker


def test_override_path_never_calls_match_consolidate_or_gemini(monkeypatch):
    """The override path must skip the ENTIRE ingest+Gemini+match leg."""
    import app.pipeline.template_matcher as tm
    import app.tasks.template_orchestrate as to

    variant = _existing_variant(user_timeline={"slots": [_tl_slot(0)]})
    _job, updates, _dl = _regen_setup(monkeypatch, variants=[variant])

    def _sentinel(name):
        def _boom(*a, **k):
            raise AssertionError(f"{name} must not run on the timeline-override path")

        return _boom

    monkeypatch.setattr(tm, "match", _sentinel("match"), raising=False)
    monkeypatch.setattr(tm, "consolidate_slots", _sentinel("consolidate_slots"), raising=False)
    monkeypatch.setattr(to, "_upload_clips_parallel", _sentinel("Gemini upload"), raising=False)
    monkeypatch.setattr(gb, "_ingest_clips", _sentinel("_ingest_clips"), raising=False)

    gb._run_regenerate_variant(JOB_ID, "original_text", None, None, False)

    assert updates[-1]["ok"] is True


def test_prepare_timeline_assembly_clamps_windows_to_probe(monkeypatch, tmp_path):
    """M2: the route skips bounds checks on never-probed clips, promising "the
    worker's probe will clamp" — this is that clamp. in_s is pulled inside the
    probed duration and the window end never runs past it."""
    _patch_timeline_io(monkeypatch, duration_s=3.0)
    out = gb._prepare_timeline_assembly(
        [
            _tl_slot(0, in_s=2.0, duration_s=5.0),  # end 7.0 → clamped to 3.0
            _tl_slot(1, in_s=10.0, duration_s=2.0),  # in past probe → in 2.9, end 3.0
        ],
        CLIP_PATHS,
        str(tmp_path),
        job_id="j",
    )
    assert out is not None
    s0, s1 = out["steps"]
    assert s0.moment["start_s"] == 2.0 and s0.moment["end_s"] == 3.0
    assert s0.slot["target_duration_s"] == 1.0
    assert s1.moment["start_s"] == 2.9
    assert s1.moment["end_s"] == 3.0


def test_prepare_timeline_assembly_drops_collapsed_slots(monkeypatch, tmp_path):
    """Slots that clamp below 0.1s are dropped (warning); all-dropped → None
    (caller falls back to a fresh match). Survivors are renumbered."""
    _patch_timeline_io(monkeypatch, duration_s=3.0)
    out = gb._prepare_timeline_assembly(
        [
            _tl_slot(0, in_s=2.97, duration_s=0.05),  # collapses below 0.1s → dropped
            _tl_slot(1, in_s=0.0, duration_s=1.0),
        ],
        CLIP_PATHS,
        str(tmp_path),
        job_id="j",
    )
    assert out is not None
    assert [s.clip_id for s in out["steps"]] == ["clip_1"]
    assert out["steps"][0].slot["position"] == 1  # renumbered

    _patch_timeline_io(monkeypatch, duration_s=0.05)  # probe shorter than any cut
    assert (
        gb._prepare_timeline_assembly(
            [_tl_slot(0, in_s=1.0, duration_s=2.0)], CLIP_PATHS, str(tmp_path), job_id="j"
        )
        is None
    )


def test_corrupt_user_timeline_falls_back_to_fresh_match(monkeypatch):
    """Bad clip_index → log warning + fresh ingest+match, never a hard failure."""
    import app.pipeline.template_matcher as tm

    variant = _existing_variant(user_timeline={"slots": [_tl_slot(99)]})
    _job, updates, _dl = _regen_setup(monkeypatch, variants=[variant])

    called = {"ingest": False, "match": False}
    metas = [_Meta("g1", 5.0)]
    ingest = {
        "clip_metas": metas,
        "clip_id_to_gcs": {"g1": CLIP_PATHS[0]},
        "clip_id_to_local": {"g1": "/a.mp4"},
        "probe_map": {"/a.mp4": _Probe(6.0)},
        "hero": metas[0],
    }

    def _fake_ingest(*a, **k):
        called["ingest"] = True
        return ingest

    def _fake_match(recipe, m, **kw):
        called["match"] = True
        return types.SimpleNamespace(steps=[])

    monkeypatch.setattr(gb, "_ingest_clips", _fake_ingest, raising=False)
    monkeypatch.setattr(tm, "match", _fake_match, raising=False)

    gb._run_regenerate_variant(JOB_ID, "original_text", None, None, False)

    assert called["ingest"] and called["match"]
    assert updates[-1]["ok"] is True


def test_no_timeline_takes_fresh_match_as_today(monkeypatch):
    """Resolution order tail: no kwarg, no persisted timeline → fresh ingest+match."""
    import app.pipeline.template_matcher as tm

    variant = _existing_variant()
    _job, updates, _dl = _regen_setup(monkeypatch, variants=[variant])

    called = {"ingest": False, "match": False}
    metas = [_Meta("g1", 5.0)]
    ingest = {
        "clip_metas": metas,
        "clip_id_to_gcs": {"g1": CLIP_PATHS[0]},
        "clip_id_to_local": {"g1": "/a.mp4"},
        "probe_map": {"/a.mp4": _Probe(6.0)},
        "hero": metas[0],
    }
    monkeypatch.setattr(
        gb, "_ingest_clips", lambda *a, **k: called.update(ingest=True) or ingest, raising=False
    )
    monkeypatch.setattr(
        tm,
        "match",
        lambda r, m, **kw: called.update(match=True) or types.SimpleNamespace(steps=[]),
        raising=False,
    )

    gb._run_regenerate_variant(JOB_ID, "original_text", None, None, False)

    assert called["ingest"] and called["match"]
    assert updates[-1]["ok"] is True


def test_swap_song_clears_user_timeline_and_takes_fresh_match(monkeypatch):
    """M1: swap-song means a NEW beat grid — the user's cut no longer lines up.
    The persisted user_timeline is REMOVED from the variant entry, the override
    is ignored, a fresh ingest+match runs, and ai_timeline is rewritten from
    the new assembly (matches the ConfirmDialog copy: "your clip edits will be
    reset")."""
    import app.pipeline.template_matcher as tm

    variant = _existing_variant(
        variant_id="song_text",
        rank=1,
        music_track_id="t1",
        user_timeline={"slots": [_tl_slot(1, in_s=2.0, duration_s=1.0)]},
        ai_timeline={"beat_grid": [9.9], "slots": [{"clip_index": 0}]},  # stale marker
    )
    mix_calls: list = []
    job, updates, _dl = _regen_setup(
        monkeypatch, variants=[variant], track=_track("t2"), mix_calls=mix_calls
    )
    _patch_music_recipe(monkeypatch, [0.0, 1.0, 2.0])

    called = {"ingest": False, "match": False}
    metas = [_Meta("g1", 5.0)]
    ingest = {
        "clip_metas": metas,
        "clip_id_to_gcs": {"g1": CLIP_PATHS[0]},
        "clip_id_to_local": {"g1": "/a.mp4"},
        "probe_map": {"/a.mp4": _Probe(6.0)},
        "hero": metas[0],
    }
    monkeypatch.setattr(
        gb, "_ingest_clips", lambda *a, **k: called.update(ingest=True) or ingest, raising=False
    )
    fresh_steps = [
        types.SimpleNamespace(
            clip_id="g1",
            slot={"position": 1},
            moment={"start_s": 0.5, "end_s": 1.5, "energy": 5.0, "description": "fresh"},
        )
    ]
    monkeypatch.setattr(
        tm,
        "match",
        lambda r, m, **kw: called.update(match=True) or types.SimpleNamespace(steps=fresh_steps),
        raising=False,
    )

    # Explicit timeline_override included to prove it's ignored when a new track
    # is set (kwarg precedence does NOT apply across a song swap).
    gb._run_regenerate_variant(
        JOB_ID,
        "song_text",
        "t2",
        None,
        False,
        timeline_override=[_tl_slot(1, in_s=2.0, duration_s=1.0)],
    )

    assert called["ingest"] and called["match"], "swap-song must take the fresh-match leg"
    # Persisted user_timeline removed from the variant entry (row-locked pop).
    assert "user_timeline" not in job.assembly_plan["variants"][0]
    final = updates[-1]
    assert final["ok"] is True
    assert final["music_track_id"] == "t2"
    assert len(mix_calls) == 1  # new song mixed in
    tl = final["ai_timeline"]
    assert tl["beat_grid"] == [0.0, 1.0, 2.0]  # rewritten — not the stale marker
    # Fresh-match output, NOT the user's cut (clip 0 via g1, window 0.5-1.5).
    assert tl["slots"][0]["clip_index"] == 0
    assert tl["slots"][0]["in_s"] == 0.5 and tl["slots"][0]["duration_s"] == 1.0
    assert tl["slots"][0]["duration_beats"] == 1


def test_timeline_kill_switch_forces_fresh_match(monkeypatch):
    variant = _existing_variant(user_timeline={"slots": [_tl_slot(0)]})
    _job, updates, _dl = _regen_setup(monkeypatch, variants=[variant])
    monkeypatch.setattr(gb.settings, "GENERATIVE_TIMELINE_EDITOR_ENABLED", False, raising=False)

    called = {"ingest": False}
    metas = [_Meta("g1", 5.0)]
    ingest = {
        "clip_metas": metas,
        "clip_id_to_gcs": {"g1": CLIP_PATHS[0]},
        "clip_id_to_local": {"g1": "/a.mp4"},
        "probe_map": {"/a.mp4": _Probe(6.0)},
        "hero": metas[0],
    }
    monkeypatch.setattr(
        gb, "_ingest_clips", lambda *a, **k: called.update(ingest=True) or ingest, raising=False
    )

    gb._run_regenerate_variant(JOB_ID, "original_text", None, None, False)

    assert called["ingest"], "kill switch off → timeline ignored → fresh leg"
    assert updates[-1]["ok"] is True


# ── Failure-patch hygiene ────────────────────────────────────────────────────────


def test_failure_patch_preserves_last_good_render(monkeypatch):
    """A failed re-render must NOT overwrite video_path/output_url and must NOT
    null base_video_path/intro_text (the failure record spreads a base dict full
    of None values — those must be stripped from the merge patch)."""
    import app.tasks.template_orchestrate as to

    variant = _existing_variant(
        text_mode="agent_text",
        intro_text="My hook",
        intro_highlight_word="hook",
        base_video_path=f"generative-jobs/{JOB_ID}/base_3_original_text.mp4",
    )
    _job, updates, _dl = _regen_setup(monkeypatch, variants=[variant])
    # Force the FULL path (the cached base would otherwise take fast-reburn).
    monkeypatch.setattr(gb.settings, "GENERATIVE_FAST_REBURN_ENABLED", False, raising=False)

    metas = [_Meta("g1", 5.0)]
    ingest = {
        "clip_metas": metas,
        "clip_id_to_gcs": {"g1": CLIP_PATHS[0]},
        "clip_id_to_local": {"g1": "/a.mp4"},
        "probe_map": {"/a.mp4": _Probe(6.0)},
        "hero": metas[0],
    }
    monkeypatch.setattr(gb, "_ingest_clips", lambda *a, **k: ingest, raising=False)
    monkeypatch.setattr(
        to,
        "_assemble_clips",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("ffmpeg boom")),
        raising=False,
    )

    gb._run_regenerate_variant(JOB_ID, "original_text", None, None, False)

    patch = updates[-1]
    assert patch["ok"] is False and patch["render_status"] == "failed"
    # Last good render survives: output keys never overwritten on failure.
    assert "video_path" not in patch and "output_url" not in patch
    # None-valued base fields stripped: fast-reburn base + persisted text survive.
    assert "base_video_path" not in patch
    assert "ai_timeline" not in patch
    # Non-None fields still flow (persisted text was reused for the attempt).
    assert patch.get("intro_text") == "My hook"


# ── Hook regrounding ─────────────────────────────────────────────────────────────


def test_hook_regrounding_uses_timeline_slot0_clip(monkeypatch):
    """When a timeline is active but the variant still takes the full leg
    (song_lyrics), intro_writer grounds in the timeline's slot-0 clip — the clip
    that actually OPENS the edit — not the max-hook_score hero."""
    variant = _existing_variant(
        variant_id="song_lyrics",
        rank=1,
        text_mode="lyrics",
        music_track_id="t1",
        user_timeline={"slots": [_tl_slot(1)]},
    )
    mix_calls: list = []
    _job, updates, _dl = _regen_setup(
        monkeypatch, variants=[variant], track=_track("t1"), mix_calls=mix_calls
    )
    _patch_music_recipe(monkeypatch, [0.0, 1.0])
    # _inject_lyrics reaches the DB (lyrics-cache refresh) — stub it whole.
    monkeypatch.setattr(
        gb,
        "_inject_lyrics",
        lambda recipe_dict, track, style_set_id=None: recipe_dict,
        raising=False,
    )

    metas = [_Meta("g_a", 9.0), _Meta("g_b", 1.0)]  # g_a is the max-hook hero
    ingest = {
        "clip_metas": metas,
        "clip_id_to_gcs": {"g_a": CLIP_PATHS[0], "g_b": CLIP_PATHS[1]},
        "clip_id_to_local": {"g_a": "/a.mp4", "g_b": "/b.mp4"},
        "probe_map": {"/a.mp4": _Probe(6.0), "/b.mp4": _Probe(6.0)},
        "hero": metas[0],
    }
    monkeypatch.setattr(gb, "_ingest_clips", lambda *a, **k: ingest, raising=False)

    seen: dict = {}

    def _capture_text_agents(clip_metas, hero, **kw):
        seen["hero"] = hero
        return None, None

    monkeypatch.setattr(gb, "_run_text_agents", _capture_text_agents, raising=False)

    gb._run_regenerate_variant(JOB_ID, "song_lyrics", None, None, False)

    # Timeline slot-0 points at clip_index 1 → CLIP_PATHS[1] → clip_id "g_b".
    assert seen["hero"].clip_id == "g_b"
    assert updates[-1]["ok"] is True


# ── Task signature pass-through ──────────────────────────────────────────────────


def test_regenerate_task_threads_timeline_override(monkeypatch):
    import contextlib

    import app.services.pipeline_trace as pt

    monkeypatch.setattr(pt, "pipeline_trace_for", lambda job_id: contextlib.nullcontext())
    seen: dict = {}
    monkeypatch.setattr(
        gb,
        "_run_regenerate_variant",
        lambda *a, **k: seen.update(args=a, kwargs=k),
        raising=False,
    )

    timeline = [_tl_slot(0)]
    gb.regenerate_generative_variant.run("j", "song_text", timeline_override=timeline)

    assert seen["kwargs"]["timeline_override"] == timeline
