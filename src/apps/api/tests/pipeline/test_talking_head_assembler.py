"""Unit tests for the talking_head assembler (Lane C + plans/010 T6 spine cut)."""

from __future__ import annotations

import os
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from app.pipeline import talking_head_assembler as tha
from app.pipeline.silence_cut import (
    KEEP_SEGMENTS_PUNCH_IN,
    CutPlan,
    Removal,
    no_op_plan,
)
from app.pipeline.talking_head_assembler import (
    BrollSource,
    SpineExtractionError,
    assemble_talking_head,
    build_talking_head_command,
    schedule_broll,
    select_spine,
    spine_cut_cap_s,
)


def _meta(clip_id: str, content_type: str = "broll", audio_type: str = "ambient"):
    return SimpleNamespace(clip_id=clip_id, content_type=content_type, audio_type=audio_type)


# ── select_spine ────────────────────────────────────────────────────────────


def test_spine_is_highest_speech_coverage():
    metas = [_meta("a"), _meta("b"), _meta("c")]
    paths = {"a": "a.mp4", "b": "b.mp4", "c": "c.mp4"}
    cov = {"a.mp4": 0.2, "b.mp4": 0.9, "c.mp4": 0.5}
    sel = select_spine(metas, paths, coverage_fn=lambda p: cov[p])
    assert sel.spine_clip_id == "b"
    assert sel.broll_clip_ids == ["a", "c"]  # original order, spine removed


def test_talking_head_label_boost_wins_near_tie():
    # 'b' has lower raw coverage but the talking_head + dialogue labels push it past 'a'.
    metas = [_meta("a", "broll", "ambient"), _meta("b", "talking_head", "dialogue")]
    paths = {"a": "a.mp4", "b": "b.mp4"}
    cov = {"a.mp4": 0.6, "b.mp4": 0.4}  # 0.6 vs 0.4+0.5+0.25 = 1.15
    sel = select_spine(metas, paths, coverage_fn=lambda p: cov[p])
    assert sel.spine_clip_id == "b"


def test_explicit_override_wins_regardless_of_coverage():
    metas = [_meta("a"), _meta("b")]
    paths = {"a": "a.mp4", "b": "b.mp4"}
    sel = select_spine(metas, paths, spine_clip_id="a", coverage_fn=lambda p: 0.0)
    assert sel.spine_clip_id == "a"
    assert sel.broll_clip_ids == ["b"]


def test_select_spine_ignores_metas_without_paths():
    metas = [_meta("a"), _meta("ghost")]
    paths = {"a": "a.mp4"}
    sel = select_spine(metas, paths, coverage_fn=lambda p: 0.5)
    assert sel.spine_clip_id == "a"
    assert sel.broll_clip_ids == []


# ── schedule_broll ──────────────────────────────────────────────────────────


def _src(clip_id: str, dur: float) -> BrollSource:
    return BrollSource(clip_id=clip_id, reframed_path=f"{clip_id}.mp4", source_dur_s=dur)


def test_schedule_evenly_spaced_within_bounds():
    windows = schedule_broll(10.0, [_src("a", 5), _src("b", 5), _src("c", 5)])
    assert len(windows) == 3
    assert windows[0].start_s == pytest.approx(1.5)  # lead-in
    # strictly increasing, all inside [0, usable]
    for w in windows:
        assert 0 <= w.start_s < w.end_s <= 10.0
    assert windows[0].start_s < windows[1].start_s < windows[2].start_s


def test_window_length_clamped_to_short_source():
    # A 0.8s B-roll yields a 0.8s window even though the segment is larger.
    windows = schedule_broll(20.0, [_src("a", 0.8)])
    assert len(windows) == 1
    assert windows[0].end_s - windows[0].start_s == pytest.approx(0.8)


def test_no_broll_is_empty():
    assert schedule_broll(10.0, []) == []


def test_too_short_spine_yields_no_cutaways():
    # usable below lead-in + min window → show the speaker alone.
    assert schedule_broll(1.5, [_src("a", 5)]) == []


# ── schedule_broll: cut-point anchors (plans/010 T6, 12A) ───────────────────


def test_schedule_without_anchors_is_byte_identical():
    # REGRESSION PIN: anchors=None / [] must not perturb today's schedule.
    broll = [_src("a", 5), _src("b", 5), _src("c", 5)]
    legacy = schedule_broll(10.0, broll)
    assert schedule_broll(10.0, broll, anchors=None) == legacy
    assert schedule_broll(10.0, broll, anchors=[]) == legacy


def test_anchored_window_slides_to_cover_anchor():
    # usable 20, one 5s b-roll → 3s window; anchor 10.0 gets centered.
    (w,) = schedule_broll(20.0, [_src("a", 5.0)], anchors=[10.0])
    assert w.start_s <= 10.0 <= w.end_s
    assert w.start_s == pytest.approx(8.5)  # 10.0 − length/2
    assert w.end_s - w.start_s == pytest.approx(3.0)  # length rule unchanged


def test_anchor_coverage_clamps_to_slot_no_overlap():
    # 2 b-rolls, usable 10 → slots [1.5,5.75]/[5.75,10], 3s windows. Anchor 5.0
    # near slot-0's end: window slides to its max start and still covers; the
    # slot boundary is never crossed (no window overlap, cadence intact).
    w0, w1 = schedule_broll(10.0, [_src("a", 5), _src("b", 5)], anchors=[5.0])
    assert w0.start_s == pytest.approx(2.75)
    assert w0.end_s == pytest.approx(5.75)
    assert w0.start_s <= 5.0 <= w0.end_s
    assert w0.end_s <= w1.start_s  # no overlap
    assert w1.start_s == pytest.approx(5.75)  # slot-1 keeps its cadence start


def test_uncoverable_anchor_is_not_forced():
    # A cut inside the lead-in has no legal covering window → cadence schedule.
    broll = [_src("a", 5), _src("b", 5)]
    assert schedule_broll(10.0, broll, anchors=[0.4]) == schedule_broll(10.0, broll)


def test_covered_anchor_not_chased_by_later_window():
    # Window 0 slides to cover 7.0 and incidentally covers 8.0 too; window 1
    # therefore has no pending anchor left and keeps its cadence position.
    w0, w1 = schedule_broll(20.0, [_src("a", 5), _src("b", 5)], anchors=[7.0, 8.0])
    assert w0.start_s <= 7.0 <= w0.end_s
    assert w0.start_s <= 8.0 <= w0.end_s
    assert w1.start_s == pytest.approx(10.75)  # 1.5 + seg (anchor-less position)


def test_anchors_never_violate_window_rules():
    # Cadence, lead-in, per-clip length clamps, count, and ordering all hold
    # regardless of where anchors fall (incl. uncoverable + boundary anchors).
    broll = [_src("a", 5), _src("b", 0.8), _src("c", 5)]
    plain = schedule_broll(20.0, broll)
    anchored = schedule_broll(20.0, broll, anchors=[2.0, 5.0, 9.0, 11.0, 19.9])
    assert len(anchored) == len(plain)  # window count unchanged
    for w, p in zip(anchored, plain):
        assert w.clip_id == p.clip_id
        assert w.end_s - w.start_s == pytest.approx(p.end_s - p.start_s)  # lengths
        assert w.start_s >= tha._LEAD_IN_S
        assert w.end_s <= 20.0
    for prev, nxt in zip(anchored, anchored[1:]):
        assert prev.end_s <= nxt.start_s + 1e-9  # ordered, non-overlapping


# ── spine_cut_cap_s (plans/010 T6, 14A) ─────────────────────────────────────


def test_spine_cut_cap_arithmetic():
    assert spine_cut_cap_s(60.0) == pytest.approx(120.0)  # 2×60 above the floor
    assert spine_cut_cap_s(200.0) == pytest.approx(300.0)  # 2×200 clamped to max
    assert spine_cut_cap_s(30.0) == pytest.approx(120.0)  # floor wins for short targets
    assert spine_cut_cap_s(150.0) == pytest.approx(300.0)  # 2×150 hits the max exactly
    assert spine_cut_cap_s(None) == pytest.approx(300.0)  # no target → subtitled's 300s
    assert spine_cut_cap_s(0) == pytest.approx(300.0)


# ── build_talking_head_command ──────────────────────────────────────────────


def test_command_spine_only_has_no_overlay():
    cmd = build_talking_head_command("spine.mp4", [], "out.mp4", usable_s=8.0)
    joined = " ".join(cmd)
    assert "overlay" not in joined
    assert "[0:v]trim=0:8.000" in joined
    assert "-map" in cmd and "[base]" in cmd  # final video is the trimmed spine
    # final-output encode is preset=fast (also locked by test_encoder_policy)
    assert "-preset" in cmd and cmd[cmd.index("-preset") + 1] == "fast"
    # spine audio muxed, resampled to 48k
    assert "aresample=48000" in joined
    assert "-map" in cmd and "[outa]" in cmd


def test_command_with_broll_has_pts_shift_enable_and_eof_pass():
    windows = [
        tha.BrollWindow(clip_id="a", reframed_path="a.mp4", start_s=1.5, end_s=4.0),
        tha.BrollWindow(clip_id="b", reframed_path="b.mp4", start_s=4.5, end_s=7.0),
    ]
    cmd = build_talking_head_command("spine.mp4", windows, "out.mp4", usable_s=10.0)
    joined = " ".join(cmd)
    # PTS shift so the cutaway plays from its own start during the window.
    assert "setpts=PTS-STARTPTS+1.500/TB" in joined
    # enable gates visibility to the window; eof_action=pass lets spine show through.
    assert "enable='between(t,1.500,4.000)'" in joined
    assert "eof_action=pass" in joined
    # both B-roll inputs are present (-i spine + 2 windows)
    assert cmd.count("-i") == 3


# ── assemble_talking_head (orchestration) ───────────────────────────────────


def _probe_map(*clip_ids: str, dur: float = 10.0):
    return {cid: SimpleNamespace(duration_s=dur, has_audio=True) for cid in clip_ids}


def test_assemble_raises_spine_extraction_error_on_bad_spine():
    metas = [_meta("a"), _meta("b")]
    paths = {"a": "a.mp4", "b": "b.mp4"}
    with (
        patch.object(tha, "speech_coverage", lambda p: 0.5),
        patch.object(tha, "reframe_and_export", side_effect=RuntimeError("corrupt")),
    ):
        with pytest.raises(SpineExtractionError):
            assemble_talking_head(
                clip_paths=paths,
                clip_metas=metas,
                probe_map=_probe_map("a", "b"),
                output_path="out.mp4",
                tmpdir="/tmp",
            )


def test_assemble_drops_failed_broll_but_completes():
    metas = [_meta("a", "talking_head", "dialogue"), _meta("b"), _meta("c")]
    paths = {"a": "a.mp4", "b": "b.mp4", "c": "c.mp4"}

    # Spine ('a') reframes fine; B-roll 'b' fails, 'c' succeeds.
    def fake_reframe(input_path, *args, **kwargs):
        if input_path == "b.mp4":
            raise RuntimeError("bad broll")

    captured = {}

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        return SimpleNamespace(returncode=0, stderr=b"")

    with (
        patch.object(tha, "speech_coverage", lambda p: 1.0 if p == "a.mp4" else 0.0),
        patch.object(tha, "reframe_and_export", side_effect=fake_reframe),
        patch.object(tha.subprocess, "run", side_effect=fake_run),
    ):
        out = assemble_talking_head(
            clip_paths=paths,
            clip_metas=metas,
            probe_map=_probe_map("a", "b", "c"),
            output_path="out.mp4",
            tmpdir="/tmp",
        )
    assert out == "out.mp4"
    # Only 'c' survived as a cutaway → one B-roll input beyond the spine.
    assert captured["cmd"].count("-i") == 2


def test_safe_stem_strips_path_separators():
    # Generative clip_ids are Gemini ref names like "files/abc" — the stem must be
    # a single writable filename component.
    assert tha._safe_stem("files/t1eq2y5u9km4") == "files_t1eq2y5u9km4"
    assert "/" not in tha._safe_stem("a/b/c")
    assert tha._safe_stem("") == "clip"


def test_assemble_sanitizes_slashed_clip_ids_in_reframe_paths():
    # Regression (caught by make local-render, 2026-05-31): generative clip_ids are
    # Gemini ref names containing "/", so f"{tmpdir}/spine_{clip_id}.mp4" pointed into
    # a phantom subdir and ffmpeg's output-open failed → SpineExtractionError → the
    # whole talking_head job silently degraded to montage. Every reframe intermediate
    # must sit DIRECTLY under tmpdir with a slash-free basename.
    metas = [_meta("files/spine1", "talking_head", "dialogue"), _meta("files/broll1")]
    paths = {"files/spine1": "s.mp4", "files/broll1": "b.mp4"}
    out_paths: list[str] = []

    def fake_reframe(input_path, *args, **kwargs):
        out_paths.append(args[-1])  # output_path is the last positional arg

    with (
        patch.object(tha, "speech_coverage", lambda p: 1.0 if p == "s.mp4" else 0.0),
        patch.object(tha, "reframe_and_export", side_effect=fake_reframe),
        patch.object(tha.subprocess, "run", return_value=SimpleNamespace(returncode=0, stderr=b"")),
    ):
        assemble_talking_head(
            clip_paths=paths,
            clip_metas=metas,
            probe_map=_probe_map("files/spine1", "files/broll1"),
            output_path="out.mp4",
            tmpdir="/tmp/thtest",
        )
    assert out_paths, "reframe_and_export should have been called for spine + broll"
    for p in out_paths:
        assert os.path.dirname(p) == "/tmp/thtest", f"phantom subdir in {p!r}"
        assert "/" not in os.path.basename(p)


def test_assemble_raises_on_composite_ffmpeg_failure():
    metas = [_meta("a", "talking_head", "dialogue"), _meta("b")]
    paths = {"a": "a.mp4", "b": "b.mp4"}
    with (
        patch.object(tha, "speech_coverage", lambda p: 1.0 if p == "a.mp4" else 0.0),
        patch.object(tha, "reframe_and_export"),
        patch.object(
            tha.subprocess, "run", return_value=SimpleNamespace(returncode=1, stderr=b"ffmpeg boom")
        ),
    ):
        with pytest.raises(tha.TalkingHeadAssemblyError):
            assemble_talking_head(
                clip_paths=paths,
                clip_metas=metas,
                probe_map=_probe_map("a", "b"),
                output_path="out.mp4",
                tmpdir="/tmp",
            )


def test_assemble_emits_trace_events():
    metas = [_meta("a", "talking_head", "dialogue"), _meta("b")]
    paths = {"a": "a.mp4", "b": "b.mp4"}
    events = []
    with (
        patch.object(tha, "speech_coverage", lambda p: 1.0 if p == "a.mp4" else 0.0),
        patch.object(tha, "reframe_and_export"),
        patch.object(tha.subprocess, "run", return_value=SimpleNamespace(returncode=0, stderr=b"")),
        patch.object(
            tha,
            "record_pipeline_event",
            side_effect=lambda stage, event, data: events.append(event),
        ),
    ):
        assemble_talking_head(
            clip_paths=paths,
            clip_metas=metas,
            probe_map=_probe_map("a", "b"),
            output_path="out.mp4",
            tmpdir="/tmp",
        )
    assert "archetype_selected" in events
    assert "spine_selected" in events
    assert "broll_scheduled" in events


def test_assemble_runs_without_a_trace_context():
    # record_pipeline_event no-ops outside a pipeline_trace_for(job_id) block —
    # the assembler must not require one (Lane D supplies it in prod).
    metas = [_meta("a", "talking_head", "dialogue")]
    paths = {"a": "a.mp4"}
    with (
        patch.object(tha, "speech_coverage", lambda p: 1.0),
        patch.object(tha, "reframe_and_export"),
        patch.object(tha.subprocess, "run", return_value=SimpleNamespace(returncode=0, stderr=b"")),
    ):
        out = assemble_talking_head(
            clip_paths=paths,
            clip_metas=metas,
            probe_map=_probe_map("a"),
            output_path="out.mp4",
            tmpdir="/tmp",
        )
    assert out == "out.mp4"


# ── assemble_talking_head: spine silence-cut (plans/010 T6) ──────────────────


def _cut_plan_6_5() -> CutPlan:
    """Two removals over a 6.5s spine — mirrors the T5 fixture plan."""
    return CutPlan(
        keep_segments=[(0.0, 0.88), (1.42, 2.5), (4.4, 6.5)],
        removed=[
            Removal(start_s=0.88, end_s=1.42, reason="filler_lexical"),
            Removal(start_s=2.5, end_s=4.4, reason="silence"),
        ],
        time_saved_s=2.44,
    )


def _entry(plan, *, failed=False, retakes=0) -> dict:
    """A `_silence_cut_analysis`-shaped cache entry."""
    return {
        "failed": failed,
        "words": [],
        "language": "en",
        "plan": plan,
        "retake_span_count": retakes,
        "cut_video_path": None,
    }


def _run_assemble_with_cut(
    *,
    silence_cut_fn,
    probe_map,
    target_duration_s=None,
    reframe=None,
    cut_probe_dur=4.06,
    tmpdir="/tmp",
    run=None,
    probe=None,
):
    """Drive assemble_talking_head with everything ffmpeg-shaped stubbed.

    Returns (reframe_calls, subprocess_cmds, events, silence_cut_out).
    Spine is 'a'; 'b' is the single b-roll. `select_spine` is stubbed directly
    (its `coverage_fn` default binds the real speech_coverage at import time,
    so patching the module attribute would not divert it). `run` overrides the
    subprocess result per command; `probe` overrides the probe_video stub.
    """
    metas = [_meta("a", "talking_head", "dialogue"), _meta("b")]
    paths = {"a": "a.mp4", "b": "b.mp4"}
    selection = tha.SpineSelection(spine_clip_id="a", spine_score=1.0, broll_clip_ids=["b"])
    reframe_calls: list[dict] = []

    def _default_reframe(input_path, start_s, end_s, aspect, ass, output_path, **kw):
        reframe_calls.append({"input": input_path, "start": start_s, "end": end_s, **kw})

    cmds: list[list] = []

    def _fake_run(cmd, **kwargs):
        cmds.append(cmd)
        if run is not None:
            return run(cmd)
        return SimpleNamespace(returncode=0, stderr=b"")

    events: list[tuple] = []
    out_ctx: dict = {}
    with (
        patch.object(tha, "select_spine", lambda *a, **k: selection),
        patch.object(tha, "reframe_and_export", side_effect=reframe or _default_reframe),
        patch.object(tha.subprocess, "run", side_effect=_fake_run),
        patch.object(
            tha,
            "probe_video",
            probe or (lambda p: SimpleNamespace(duration_s=cut_probe_dur, has_audio=True)),
        ),
        patch.object(
            tha,
            "record_pipeline_event",
            side_effect=lambda stage, event, data: events.append((stage, event, data)),
        ),
    ):
        assemble_talking_head(
            clip_paths=paths,
            clip_metas=metas,
            probe_map=probe_map,
            target_duration_s=target_duration_s,
            output_path="out.mp4",
            tmpdir=tmpdir,
            silence_cut_fn=silence_cut_fn,
            silence_cut_out=out_ctx,
        )
    return reframe_calls, cmds, events, out_ctx


def test_assemble_spine_cut_happy_path():
    # Spine 6.5s < cap (target 6.5 → cap 120) → analysis on the ORIGINAL spine
    # path, no pre-cap extraction ("shorter than the cap ⇒ nothing changes").
    plan = _cut_plan_6_5()
    seen: dict = {}

    def _fn(path, dur, *, cache_key=None):
        seen["analysis"] = (path, dur, cache_key)
        return _entry(plan)

    reframe_calls, cmds, events, out_ctx = _run_assemble_with_cut(
        silence_cut_fn=_fn,
        probe_map=_probe_map("a", "b", dur=6.5),
        target_duration_s=6.5,
        cut_probe_dur=4.06,  # re-probe of the CUT spine
    )

    # Uncapped analysis keys the per-job cache by the spine path itself (P1).
    assert seen["analysis"] == ("a.mp4", 6.5, "a.mp4")
    # The cut executes inside the spine reframe with the plan's exact segments
    # and the punch-in CONSTANT (never a literal).
    spine = reframe_calls[0]
    assert spine["input"] == "a.mp4"
    assert spine["end"] == pytest.approx(6.5)
    assert spine["keep_segments"] == pytest.approx([(0.0, 0.88), (1.42, 2.5), (4.4, 6.5)])
    assert spine["keep_segments_punch_in"] == KEEP_SEGMENTS_PUNCH_IN
    # B-roll is NEVER cut.
    assert "keep_segments" not in reframe_calls[1]
    # usable_s derives from the re-probed CUT duration (4.06 < target 6.5) —
    # the composite trims to it and every b-roll window sits inside it.
    (composite,) = cmds  # exactly ONE subprocess call: no pre-cap extraction
    joined = " ".join(composite)
    assert "trim=0:4.060" in joined
    assert "enable='between(t,1.500,4.060)'" in joined
    # Summary handed back for variant persistence (plan_summary shape, M2 —
    # original_duration_s is the spine's ANALYSIS duration) + the plan event
    # emitted with the exact shared plan_event_payload fields.
    assert out_ctx["summary"] == {
        "removed": [
            {"start_s": 0.88, "end_s": 1.42, "reason": "filler_lexical"},
            {"start_s": 2.5, "end_s": 4.4, "reason": "silence"},
        ],
        "time_saved_s": 2.44,
        "version": 1,
        "original_duration_s": 6.5,
    }
    plan_events = [e for e in events if e[1] == "silence_cut_plan"]
    assert len(plan_events) == 1
    assert plan_events[0][2] == {
        "variant_id": "talking_head",
        "removed_count": 2,
        "time_saved_s": 2.44,
        "reasons": {"filler_lexical": 1, "silence": 1},
        "retake_spans": 0,
        "applied": True,
        "cut_reused": False,
        "broll_anchors": 2,  # 0.88 and 1.96 (cut timeline)
    }


def test_assemble_spine_cut_no_audio_gate_short_circuits():
    # has_audio=False on the spine probe → the analysis fn NEVER runs (3A) and
    # the spine renders uncut with the skip event.
    probe_map = {
        "a": SimpleNamespace(duration_s=10.0, has_audio=False),
        "b": SimpleNamespace(duration_s=10.0, has_audio=True),
    }

    def _bomb(path, dur, *, cache_key=None):
        raise AssertionError("analysis must not run for an audio-less spine")

    reframe_calls, _cmds, events, out_ctx = _run_assemble_with_cut(
        silence_cut_fn=_bomb, probe_map=probe_map
    )

    assert [e[1] for e in events if e[0] == "silence_cut"] == ["silence_cut_skipped_no_audio"]
    assert "keep_segments" not in reframe_calls[0]
    assert reframe_calls[0]["end"] == pytest.approx(10.0)  # full uncut spine
    assert "summary" not in out_ctx


def test_assemble_spine_cut_analysis_failure_renders_uncut():
    reframe_calls, _cmds, events, out_ctx = _run_assemble_with_cut(
        silence_cut_fn=lambda p, d, **kw: _entry(None, failed=True),
        probe_map=_probe_map("a", "b", dur=10.0),
    )
    assert "keep_segments" not in reframe_calls[0]
    assert reframe_calls[0]["end"] == pytest.approx(10.0)
    assert not [e for e in events if e[1] == "silence_cut_plan"]
    assert "summary" not in out_ctx


def test_assemble_spine_cut_bailout_renders_uncut():
    # A tripped safety rail yields a no-op plan → uncut spine; bailouts are
    # event-only upstream (in the shared analysis), so no plan event here.
    bail = no_op_plan(10.0, bailout_reason="max_removal_exceeded")
    reframe_calls, _cmds, events, out_ctx = _run_assemble_with_cut(
        silence_cut_fn=lambda p, d, **kw: _entry(bail),
        probe_map=_probe_map("a", "b", dur=10.0),
    )
    assert "keep_segments" not in reframe_calls[0]
    assert reframe_calls[0]["end"] == pytest.approx(10.0)
    assert not [e for e in events if e[1] == "silence_cut_plan"]
    assert "summary" not in out_ctx


def test_assemble_spine_cut_ffmpeg_failure_falls_back_to_uncut():
    # A CUT reframe failure must NOT degrade the job to montage — retry uncut
    # (plans/010 failure table), record applied=False, persist no summary.
    plan = _cut_plan_6_5()
    reframe_calls: list[dict] = []

    def _reframe(input_path, start_s, end_s, aspect, ass, output_path, **kw):
        reframe_calls.append({"input": input_path, "start": start_s, "end": end_s, **kw})
        if "keep_segments" in kw:
            raise RuntimeError("cut filtergraph boom")

    calls, _cmds, events, out_ctx = _run_assemble_with_cut(
        silence_cut_fn=lambda p, d, **kw: _entry(plan),
        probe_map=_probe_map("a", "b", dur=6.5),
        reframe=_reframe,
    )
    spine_calls = [c for c in reframe_calls if c["input"] == "a.mp4"]
    assert len(spine_calls) == 2  # cut attempt, then the uncut retry
    assert "keep_segments" in spine_calls[0]
    assert "keep_segments" not in spine_calls[1]
    assert spine_calls[1]["end"] == pytest.approx(6.5)  # today's full spine
    plan_events = [e for e in events if e[1] == "silence_cut_plan"]
    assert plan_events and plan_events[0][2]["applied"] is False
    assert "cut filtergraph boom" in plan_events[0][2]["apply_error"]
    # The video shipped uncut — persisting removed[] would lie to the viewer.
    assert "summary" not in out_ctx


def test_assemble_spine_cut_uncut_reframe_failure_still_degrades():
    # Only an UNCUT spine failure is a real spine-extraction signal.
    plan = _cut_plan_6_5()

    def _reframe(input_path, *args, **kw):
        if input_path == "a.mp4":
            raise RuntimeError("corrupt spine")

    selection = tha.SpineSelection(spine_clip_id="a", spine_score=1.0, broll_clip_ids=["b"])
    with (
        patch.object(tha, "select_spine", lambda *a, **k: selection),
        patch.object(tha, "reframe_and_export", side_effect=_reframe),
        patch.object(tha.subprocess, "run", return_value=SimpleNamespace(returncode=0, stderr=b"")),
        patch.object(tha, "record_pipeline_event", lambda *a, **k: None),
    ):
        with pytest.raises(SpineExtractionError):
            assemble_talking_head(
                clip_paths={"a": "a.mp4", "b": "b.mp4"},
                clip_metas=[_meta("a", "talking_head", "dialogue"), _meta("b")],
                probe_map=_probe_map("a", "b", dur=6.5),
                output_path="out.mp4",
                tmpdir="/tmp",
                silence_cut_fn=lambda p, d, **kw: _entry(plan),
            )


def test_assemble_spine_precap_bounds_detection_and_cut(tmp_path):
    # Spine 400s, target 60 → cap 120 (14A): analysis runs on a 120s capped
    # 16k mono WAV, the plan applies inside the capped reframe window, and
    # usable_s clamps to the target.
    plan = CutPlan(
        keep_segments=[(0.0, 50.0), (60.0, 120.0)],
        removed=[Removal(start_s=50.0, end_s=60.0, reason="silence")],
        time_saved_s=10.0,
    )
    seen: dict = {}

    def _fn(path, dur, *, cache_key=None):
        seen["analysis"] = (path, dur, cache_key)
        return _entry(plan)

    reframe_calls, cmds, events, out_ctx = _run_assemble_with_cut(
        silence_cut_fn=_fn,
        probe_map=_probe_map("a", "b", dur=400.0),
        target_duration_s=60.0,
        cut_probe_dur=110.0,  # cut spine re-probe
        tmpdir=str(tmp_path),
    )

    analysis_path, analysis_dur, cache_key = seen["analysis"]
    assert analysis_path.endswith(".wav") and analysis_path.startswith(str(tmp_path))
    assert analysis_dur == pytest.approx(120.0)
    # Capped analyses key the per-job cache by ORIGINAL path + cap (P1) — never
    # by the per-variant WAV path.
    assert cache_key == "a.mp4::cap=120.0"
    # First subprocess call is the precise -t re-encode to whisper-native WAV.
    extract = cmds[0]
    assert extract[extract.index("-t") + 1] == "120.000"
    assert extract[extract.index("-ar") + 1] == "16000"
    assert extract[extract.index("-ac") + 1] == "1"
    # The cut reframe operates on the CAPPED window, not the 400s spine.
    spine = reframe_calls[0]
    assert spine["end"] == pytest.approx(120.0)
    assert spine["keep_segments"] == pytest.approx([(0.0, 50.0), (60.0, 120.0)])
    # usable_s = min(cut duration 110, target 60) → the composite trims to 60.
    assert "trim=0:60.000" in " ".join(cmds[1])
    assert out_ctx["summary"]["time_saved_s"] == 10.0
    assert out_ctx["summary"]["original_duration_s"] == 120.0  # the ANALYSIS window


def test_assemble_spine_cut_capped_cache_key_shared_across_variants(tmp_path):
    # P1: two cut-capable variants share one job cache. The capped analysis is
    # keyed by the ORIGINAL spine path + cap — NOT the per-variant WAV path —
    # so the second variant reuses the cached entry instead of re-paying
    # whisper + the retake LLM. (The WAV re-extract per variant is accepted
    # cost; it is only consumed on the first compute.)
    plan = CutPlan(
        keep_segments=[(0.0, 50.0), (60.0, 120.0)],
        removed=[Removal(start_s=50.0, end_s=60.0, reason="silence")],
        time_saved_s=10.0,
    )
    keys_seen: list[str] = []
    cache: dict[str, dict] = {}
    computed_paths: list[str] = []

    def _fn(path, dur, *, cache_key=None):
        keys_seen.append(cache_key)
        if cache_key not in cache:
            computed_paths.append(path)  # the expensive whisper+LLM compute
            cache[cache_key] = _entry(plan)
        return cache[cache_key]

    for variant in ("variant_a", "variant_b"):
        _run_assemble_with_cut(
            silence_cut_fn=_fn,
            probe_map=_probe_map("a", "b", dur=400.0),
            target_duration_s=60.0,
            cut_probe_dur=110.0,
            tmpdir=str(tmp_path / variant),
        )

    # Same stable key from both variants despite per-variant WAV tmpdirs …
    assert keys_seen == ["a.mp4::cap=120.0", "a.mp4::cap=120.0"]
    # … so the analysis computed exactly once, on the FIRST variant's WAV.
    assert len(computed_paths) == 1
    assert computed_paths[0].startswith(str(tmp_path / "variant_a"))


def test_assemble_spine_precap_extraction_failure_renders_uncut():
    # T3: spine 400s > cap 120 and the WAV pre-cap ffmpeg FAILS (returncode 1)
    # → analysis-failed event, the analysis fn never runs, spine renders
    # full-length uncut, and no summary is persisted (fail-open).
    def _bomb(path, dur, *, cache_key=None):
        raise AssertionError("analysis must not run when the pre-cap extract fails")

    def _run(cmd):
        if str(cmd[-1]).endswith(".wav"):  # the pre-cap extract writes the WAV
            return SimpleNamespace(returncode=1, stderr=b"wav boom")
        return SimpleNamespace(returncode=0, stderr=b"")

    reframe_calls, cmds, events, out_ctx = _run_assemble_with_cut(
        silence_cut_fn=_bomb,
        probe_map=_probe_map("a", "b", dur=400.0),
        target_duration_s=60.0,
        run=_run,
    )

    sc_events = [e for e in events if e[0] == "silence_cut"]
    assert [e[1] for e in sc_events] == ["silence_cut_analysis_failed"]
    assert "wav boom" in sc_events[0][2]["error"]
    assert "keep_segments" not in reframe_calls[0]
    assert reframe_calls[0]["end"] == pytest.approx(400.0)  # full uncut spine
    assert "summary" not in out_ctx
    assert len(cmds) == 2  # failed WAV extract + the composite (job still renders)


def test_assemble_spine_cut_probe_failure_skips_stage():
    # T3: probe_map carries no has_audio for the spine AND probe_video raises
    # → the whole stage is skipped (fail-open): the analysis fn never runs and
    # the spine renders uncut with no silence_cut events.
    probe_map = {
        "a": SimpleNamespace(duration_s=10.0),  # no has_audio attribute
        "b": SimpleNamespace(duration_s=10.0),
    }

    def _bomb(path, dur, *, cache_key=None):
        raise AssertionError("analysis must not run when the audio probe fails")

    def _probe(path):
        raise RuntimeError("probe boom")

    reframe_calls, _cmds, events, out_ctx = _run_assemble_with_cut(
        silence_cut_fn=_bomb, probe_map=probe_map, probe=_probe
    )

    assert not [e for e in events if e[0] == "silence_cut"]
    assert "keep_segments" not in reframe_calls[0]
    assert reframe_calls[0]["end"] == pytest.approx(10.0)  # full uncut spine
    assert "summary" not in out_ctx


def test_assemble_spine_cut_garbage_entry_renders_uncut():
    # T3: a non-dict analysis entry must never crash the render — uncut flow,
    # no plan event, no summary.
    reframe_calls, _cmds, events, out_ctx = _run_assemble_with_cut(
        silence_cut_fn=lambda p, d, **kw: "garbage",
        probe_map=_probe_map("a", "b", dur=10.0),
    )
    assert "keep_segments" not in reframe_calls[0]
    assert reframe_calls[0]["end"] == pytest.approx(10.0)
    assert not [e for e in events if e[1] == "silence_cut_plan"]
    assert "summary" not in out_ctx


def test_assemble_without_cut_fn_is_uncut_flow():
    # silence_cut_fn=None (kill switch / gated) ⇒ pre-T6 flow: full-length
    # spine reframe, no cut kwargs, no WAV extraction, no silence_cut events.
    reframe_calls, cmds, events, out_ctx = _run_assemble_with_cut(
        silence_cut_fn=None,
        probe_map=_probe_map("a", "b", dur=10.0),
    )
    assert "keep_segments" not in reframe_calls[0]
    assert reframe_calls[0]["end"] == pytest.approx(10.0)
    assert len(cmds) == 1  # composite only — no pre-cap extraction
    assert not [e for e in events if e[0] == "silence_cut"]
    assert out_ctx == {}
