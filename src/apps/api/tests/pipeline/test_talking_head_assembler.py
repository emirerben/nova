"""Unit tests for the talking_head assembler (Lane C)."""

from __future__ import annotations

import os
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from app.pipeline import talking_head_assembler as tha
from app.pipeline.talking_head_assembler import (
    BrollSource,
    SpineExtractionError,
    assemble_talking_head,
    build_talking_head_command,
    schedule_broll,
    select_spine,
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
