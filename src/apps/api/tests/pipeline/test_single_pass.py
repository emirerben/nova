"""Tests for app.pipeline.single_pass (milestone 2 — basic builder).

These run on every PR (mocked subprocess, fast). They assert filter-graph
STRUCTURE — input ordering, lavfi color holds, concat fragment, final encode
args. Pixel parity vs the multi-pass branch is the job of the SSIM/VMAF
script in tests/quality/ (manual / workflow_dispatch only).

Test list maps to milestones 2-6 in the plan. M3-M6 tests are written but
skipped here; they'll be unskipped as each milestone lands.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from app.pipeline.single_pass import (
    SinglePassInput,
    SinglePassSpec,
    SinglePassUnsupportedError,
    build_single_pass_command,
)


def _clip(path: str = "/tmp/clip.mp4", start: float = 0.0, end: float = 2.0,
          **overrides) -> SinglePassInput:
    return SinglePassInput(
        kind="clip",
        clip_path=path,
        start_s=start,
        end_s=end,
        **overrides,
    )


def _hold(color: str = "black", duration: float = 0.5) -> SinglePassInput:
    return SinglePassInput(kind="color_hold", hold_color=color, hold_s=duration)


# ── Spec-level invariants ────────────────────────────────────────────────────


def test_spec_rejects_empty_inputs():
    with pytest.raises(ValueError, match="at least one input"):
        SinglePassSpec(inputs=[])


def test_spec_rejects_transitions_length_mismatch():
    with pytest.raises(ValueError, match="transitions length"):
        SinglePassSpec(
            inputs=[_clip(), _clip()],
            transitions=["none", "none", "none"],  # 3 transitions for 2 inputs
        )


def test_spec_allows_empty_transitions_for_concat_only():
    # Empty transitions == all hard cuts. No error.
    SinglePassSpec(inputs=[_clip(), _clip(), _clip()])


# ── Test 1: one -i per slot ──────────────────────────────────────────────────


def test_single_pass_one_input_per_slot():
    """3-slot template → exactly 3 video `-i` for clips (+ silent audio)."""
    spec = SinglePassSpec(inputs=[_clip(), _clip(), _clip()])
    cmd = build_single_pass_command(spec, "/tmp/out.mp4")
    # Count -i occurrences that name a clip path (not lavfi anullsrc / color).
    clip_input_count = sum(
        1
        for i, arg in enumerate(cmd)
        if arg == "-i" and cmd[i + 1].endswith(".mp4")
    )
    assert clip_input_count == 3, cmd


# ── Test 2: color holds use lavfi, not files ─────────────────────────────────


def test_color_holds_use_lavfi_not_files():
    """Hold slots emit `-f lavfi -i color=...`; no temp .mp4 generated."""
    spec = SinglePassSpec(inputs=[_clip(), _hold(color="black", duration=1.0), _clip()])
    cmd = build_single_pass_command(spec, "/tmp/out.mp4")
    cmd_str = " ".join(cmd)
    assert "-f lavfi" in cmd_str
    assert "color=c=black" in cmd_str
    assert ":s=1080x1920" in cmd_str
    assert ":d=1.0" in cmd_str
    # Only one clip-mp4 path means the hold isn't a pre-rendered file.
    clip_paths = [
        cmd[i + 1] for i, a in enumerate(cmd)
        if a == "-i" and cmd[i + 1].endswith(".mp4")
    ]
    assert len(clip_paths) == 2  # the two clip inputs, not the hold


# ── Test 3: xfade chain emission (M3) ────────────────────────────────────────


def _argv_filter_complex(argv: list[str]) -> str:
    """Pull the -filter_complex value out of the argv. Tests reach into the
    filter graph string; isolating the extraction here keeps assertions
    legible if the argv layout shifts."""
    idx = argv.index("-filter_complex")
    return argv[idx + 1]


def test_xfade_crossfade_emits_xfade_filter_node():
    """M3: a single crossfade boundary must emit one xfade=transition=fade
    filter node and route its output to [vout]. The M2 single-concat path
    is bypassed."""
    spec = SinglePassSpec(
        inputs=[_clip(), _clip()],
        transitions=["crossfade"],
    )
    argv = build_single_pass_command(spec, "/tmp/out.mp4")
    fc = _argv_filter_complex(argv)
    assert "xfade=transition=fade" in fc
    # The xfade emits [vout] directly because it is the last (and only) one.
    assert "[vout]" in fc
    # No naked concat=n=2 filter — that's the M2 path we deliberately took.
    assert "concat=n=2" not in fc


def test_xfade_whip_pan_emits_wipeleft():
    """The transition.translate_transition map collapses whip-pan→wipe_left
    and xfade uses 'wipeleft' as the transition= parameter. Catches drift
    in either the translation map or the xfade type mapping."""
    spec = SinglePassSpec(
        inputs=[_clip(), _clip()],
        transitions=["wipe_left"],
    )
    fc = _argv_filter_complex(build_single_pass_command(spec, "/tmp/out.mp4"))
    assert "xfade=transition=wipeleft" in fc


def test_xfade_offset_math_two_slots():
    """For two 3-second slots, xfade offset = 3.0 - 0.3 = 2.7s. duration
    is the default 0.3s. Pins the offset/duration math against drift —
    these numbers are derived from the same formula used by
    multi-pass _build_xfade_filter, so any divergence is a parity bug."""
    spec = SinglePassSpec(
        inputs=[_clip(start=0.0, end=3.0), _clip(start=0.0, end=3.0)],
        transitions=["crossfade"],
    )
    fc = _argv_filter_complex(build_single_pass_command(spec, "/tmp/out.mp4"))
    assert "duration=0.300" in fc
    assert "offset=2.700" in fc


def test_xfade_duration_clamped_to_30_pct_of_shorter_slot():
    """A 0.5s outgoing slot would only allow 0.15s of xfade (30% of 0.5),
    not the default 0.3. Multi-pass clamps the same way so single-pass
    matches the multi-pass output frame-for-frame at the boundary."""
    spec = SinglePassSpec(
        inputs=[_clip(start=0.0, end=0.5), _clip(start=0.0, end=3.0)],
        transitions=["crossfade"],
    )
    fc = _argv_filter_complex(build_single_pass_command(spec, "/tmp/out.mp4"))
    assert "duration=0.150" in fc


def test_mixed_none_and_xfade_groups_consecutive_none_into_concat():
    """transitions=[none, crossfade, none] over 4 slots → slots 0+1 concat
    into [g0], slots 2+3 concat into [g1], one xfade boundary bridges
    [g0][g1] into [vout]. Catches the bug class where every "none"
    boundary becomes its own degenerate xfade — the multi-pass code calls
    this out explicitly (long chains of duration=0.001 xfades truncate
    output to ~3-4s, see transitions.py:74)."""
    spec = SinglePassSpec(
        inputs=[_clip(), _clip(), _clip(), _clip()],
        transitions=["none", "crossfade", "none"],
    )
    fc = _argv_filter_complex(build_single_pass_command(spec, "/tmp/out.mp4"))
    # Two concat sub-batches.
    assert "concat=n=2:v=1:a=0[g0]" in fc
    assert "concat=n=2:v=1:a=0[g1]" in fc
    # Exactly one xfade boundary.
    assert fc.count("xfade=") == 1
    # Final output is xfade'd, not concatenated.
    assert "[g0][g1]xfade" in fc
    assert "[vout]" in fc


def test_xfade_chain_three_visual_transitions_offsets_accumulate():
    """For three 4-second slots joined by two crossfades:
      boundary 0: offset = 4.0 - 0.3 = 3.7
      boundary 1: offset = (4 + 4) - 0.3 - 0.3 = 7.4
    Catches off-by-one in cumulative_trans accounting."""
    spec = SinglePassSpec(
        inputs=[
            _clip(start=0.0, end=4.0),
            _clip(start=0.0, end=4.0),
            _clip(start=0.0, end=4.0),
        ],
        transitions=["crossfade", "crossfade"],
    )
    fc = _argv_filter_complex(build_single_pass_command(spec, "/tmp/out.mp4"))
    assert "offset=3.700" in fc
    assert "offset=7.400" in fc


def test_mixed_xfade_types_in_one_spec():
    """Saygimdan-shape: same template uses dissolve + whip-pan boundaries.
    Each gets the correct xfade= type. Catches drift where a global
    transition type leaks across boundaries instead of being per-pair."""
    spec = SinglePassSpec(
        inputs=[
            _clip(start=0.0, end=3.0),
            _clip(start=0.0, end=3.0),
            _clip(start=0.0, end=3.0),
        ],
        transitions=["crossfade", "wipe_left"],
    )
    fc = _argv_filter_complex(build_single_pass_command(spec, "/tmp/out.mp4"))
    assert "xfade=transition=fade" in fc
    assert "xfade=transition=wipeleft" in fc


def test_xfade_offset_accounts_for_speed_factor():
    """A 6-second source clip at speed_factor=2.0 occupies 3.0s of output
    timeline; xfade offset must use the OUTPUT duration, not the source.
    For [slot0 speed=2.0 0-6s][slot1 1.0x 0-3s] with crossfade between:
        output_dur(slot0) = (6-0)/2 = 3.0s
        offset = 3.0 - 0.3 = 2.700s
    If the offset math used source duration it would emit 5.700, the
    xfade would never trigger inside the timeline, and the second clip
    would hard-cut on top of the first."""
    spec = SinglePassSpec(
        inputs=[
            _clip(start=0.0, end=6.0, speed_factor=2.0),
            _clip(start=0.0, end=3.0),
        ],
        transitions=["crossfade"],
    )
    fc = _argv_filter_complex(build_single_pass_command(spec, "/tmp/out.mp4"))
    assert "offset=2.700" in fc


def test_all_none_transitions_uses_m2_concat_path():
    """spec.transitions=[none, none] should not route through xfade chain.
    The M2 single concat=n=N filter is faster and avoids degenerate xfade
    nodes — guard against accidentally always-on-M3-path regression."""
    spec = SinglePassSpec(
        inputs=[_clip(), _clip(), _clip()],
        transitions=["none", "none"],
    )
    fc = _argv_filter_complex(build_single_pass_command(spec, "/tmp/out.mp4"))
    assert "concat=n=3:v=1:a=0[vout]" in fc
    assert "xfade=" not in fc


# ── M6: absolute-timestamp overlays ──────────────────────────────────────────


def test_abs_png_overlay_chains_after_concat():
    """A spec with one abs_png produces: concat → [base], then overlay
    → [vout]. The base label is the bridge — without it the concat would
    emit [vout] and the overlay chain would have nowhere to attach."""
    spec = SinglePassSpec(
        inputs=[_clip(), _clip()],
        abs_pngs=[
            {"png_path": "/tmp/over.png", "start_s": 1.0, "end_s": 2.0},
        ],
    )
    argv = build_single_pass_command(spec, "/tmp/out.mp4")
    fc = _argv_filter_complex(argv)
    assert "concat=n=2:v=1:a=0[base]" in fc
    # Overlay node: previous label is [base], png input idx is 2
    # (two clip inputs), enable carries the absolute timestamps.
    assert "[base][2:v]overlay=0:0:enable='between(t,1.000,2.000)'[vout]" in fc
    # PNG path is a discrete -i input.
    assert "-i" in argv and "/tmp/over.png" in argv


def test_abs_png_input_args_inserted_between_clips_and_silent_audio():
    """For 2 clips + 1 abs_png: argv has -i clip0, -i clip1, -i png,
    then the lavfi anullsrc inputs for silent audio. Silent audio is
    mapped at len(clips) + len(pngs) = 3."""
    spec = SinglePassSpec(
        inputs=[_clip(path="/tmp/a.mp4"), _clip(path="/tmp/b.mp4")],
        abs_pngs=[{"png_path": "/tmp/over.png", "start_s": 0.5, "end_s": 1.5}],
    )
    argv = build_single_pass_command(spec, "/tmp/out.mp4")
    # Silent audio input is the lavfi anullsrc — map argument is 3:a:0
    map_idx = argv.index("-map", argv.index("-map") + 1) + 1  # second -map
    assert argv[map_idx] == "3:a:0"


def test_abs_ass_overlay_uses_subtitles_filter():
    """ASS files chain via the subtitles=path:fontsdir=dir filter rather
    than overlay=, because libass renders the .ass timeline natively.
    Mirrors _burn_text_overlays at template_orchestrate.py:3236."""
    spec = SinglePassSpec(
        inputs=[_clip()],
        abs_ass_paths=["/tmp/anim.ass"],
        fonts_dir="/opt/fonts",
    )
    fc = _argv_filter_complex(build_single_pass_command(spec, "/tmp/out.mp4"))
    assert "subtitles='/tmp/anim.ass'" in fc
    assert "fontsdir='/opt/fonts'" in fc
    assert "[vout]" in fc


def test_abs_png_then_ass_chain_order():
    """PNGs paint first, ASS files paint on top. Same layering as
    _burn_text_overlays (PNG loop → ASS loop). Pins the order so libass-
    animated text doesn't get covered by a later PNG layer."""
    spec = SinglePassSpec(
        inputs=[_clip()],
        abs_pngs=[{"png_path": "/tmp/p.png", "start_s": 0.0, "end_s": 1.0}],
        abs_ass_paths=["/tmp/a.ass"],
        fonts_dir="/opt/fonts",
    )
    fc = _argv_filter_complex(build_single_pass_command(spec, "/tmp/out.mp4"))
    png_pos = fc.find("overlay=0:0")
    ass_pos = fc.find("subtitles=")
    assert 0 <= png_pos < ass_pos


def test_no_overlays_emits_vout_directly_from_concat():
    """spec without overlays must skip the [base] bridge. Regression
    guard — if M6 left [base] in place when overlays are empty, the
    -map [vout] in argv would have no source."""
    spec = SinglePassSpec(inputs=[_clip(), _clip()])
    fc = _argv_filter_complex(build_single_pass_command(spec, "/tmp/out.mp4"))
    assert "[vout]" in fc
    assert "[base]" not in fc


def test_xfade_plus_overlay_routes_via_base_label():
    """Combined M3 + M6: xfade chain emits [base] (not [vout]), and the
    overlay layer consumes [base] to emit [vout]. Catches the integration
    bug where the final_label kwarg on _build_xfade_chain wasn't
    threaded through and overlays got applied to nothing."""
    spec = SinglePassSpec(
        inputs=[_clip(start=0.0, end=3.0), _clip(start=0.0, end=3.0)],
        transitions=["crossfade"],
        abs_pngs=[{"png_path": "/tmp/p.png", "start_s": 1.0, "end_s": 2.0}],
    )
    fc = _argv_filter_complex(build_single_pass_command(spec, "/tmp/out.mp4"))
    # xfade emits [base], not [vout].
    assert "xfade=transition=fade" in fc
    assert "[base]" in fc
    # Overlay chain consumes [base] and emits [vout].
    assert "[base][2:v]overlay=" in fc
    assert "[vout]" in fc


def test_dual_output_when_base_path_and_overlays_set():
    """M6 dual-output: when base_output_path is provided AND overlays
    exist, argv emits TWO outputs sharing the filter graph — base from
    [base], full from [vout]. One decode, two encodes in the same ffmpeg
    process. Half the cost of two separate run_single_pass calls."""
    spec = SinglePassSpec(
        inputs=[_clip(), _clip()],
        abs_pngs=[{"png_path": "/tmp/p.png", "start_s": 0.0, "end_s": 1.0}],
    )
    argv = build_single_pass_command(
        spec, "/tmp/out.mp4", base_output_path="/tmp/base.mp4",
    )
    # Two -map [base] / -map [vout] pairs (each followed by audio map).
    base_idx = argv.index("[base]")
    vout_idx = argv.index("[vout]")
    assert base_idx < vout_idx
    # Both output paths appear in argv.
    assert "/tmp/base.mp4" in argv
    assert "/tmp/out.mp4" in argv
    # base output appears before vout output (encoder maps base first).
    assert argv.index("/tmp/base.mp4") < argv.index("/tmp/out.mp4")


def test_no_dual_output_when_base_path_set_but_no_overlays():
    """M6: when base_output_path is set but the spec has no overlays,
    [base] doesn't exist in the filter graph — the dual-output branch is
    skipped and the caller is expected to shutil.copy2 post-process.
    Catches the bug class where the dual-output map appears unconditionally
    and ffmpeg fails with 'Unable to find output stream for label base'."""
    spec = SinglePassSpec(inputs=[_clip(), _clip()])
    argv = build_single_pass_command(
        spec, "/tmp/out.mp4", base_output_path="/tmp/base.mp4",
    )
    # Only [vout] map; no [base] map.
    assert "[base]" not in argv
    assert "[vout]" in argv
    # base path never appears in argv (no second output).
    assert "/tmp/base.mp4" not in argv


def test_special_chars_in_ass_path_escaped():
    """The subtitles filter parses : and ' as option separators on
    macOS/Linux. Escape them so a path like /private/var/foo:bar/x.ass
    doesn't truncate the filter and fail with a cryptic libass error."""
    spec = SinglePassSpec(
        inputs=[_clip()],
        abs_ass_paths=["/path:with:colons/file.ass"],
        fonts_dir="/fonts:dir",
    )
    fc = _argv_filter_complex(build_single_pass_command(spec, "/tmp/out.mp4"))
    # Each ":" inside path/fontsdir must be backslash-escaped.
    assert "subtitles='/path\\:with\\:colons/file.ass'" in fc
    assert "fontsdir='/fonts\\:dir'" in fc


# ── Test 6: final encode args ────────────────────────────────────────────────


def test_single_pass_final_encode_args():
    """Argv ends with the canonical final-output encoder contract.

    preset=fast crf=18 yuv420p bt709 — same as _concat_demuxer,
    _pre_burn_curtain_slot_text, _burn_text_overlays.
    """
    spec = SinglePassSpec(inputs=[_clip()])
    cmd = build_single_pass_command(spec, "/tmp/out.mp4")
    cmd_str = " ".join(cmd)
    assert "-c:v libx264" in cmd_str
    assert "-preset fast" in cmd_str
    assert "-crf 18" in cmd_str
    assert "-pix_fmt yuv420p" in cmd_str
    assert "-colorspace bt709" in cmd_str
    # x264-params (scenecut/keyint) only on preset=fast path
    assert "scenecut=40:keyint=90:open_gop=0:bframes=0" in cmd_str
    # Output path is the last arg.
    assert cmd[-1] == "/tmp/out.mp4"


# ── Test 7: audio passthrough (silent body track for stage-7 mix) ────────────


def test_silent_audio_input_appended_after_video_inputs():
    """The anullsrc silent track is the LAST input, after all video sources.

    Otherwise -ss/-t on the clip inputs would apply to the lavfi source and
    corrupt timing (mirrors the comment in reframe_and_export:252-256).
    """
    spec = SinglePassSpec(inputs=[_clip(), _hold(), _clip()])
    cmd = build_single_pass_command(spec, "/tmp/out.mp4")
    # Find every -i and its source argument.
    i_args = [cmd[i + 1] for i, a in enumerate(cmd) if a == "-i"]
    # Last -i must be anullsrc (silent body track).
    assert "anullsrc" in i_args[-1]
    # Silent-audio index in the map must equal (number of video inputs).
    map_idx = cmd.index("-map", cmd.index("-map") + 1) + 1  # second -map
    assert cmd[map_idx].startswith(f"{len(spec.inputs)}:a")


# ── Test 8: label uniqueness in filter_complex ───────────────────────────────


def test_filter_complex_label_uniqueness():
    """No duplicate [vN] labels in the filter graph (silent corruption otherwise)."""
    spec = SinglePassSpec(inputs=[_clip(), _clip(), _hold(), _clip()])
    cmd = build_single_pass_command(spec, "/tmp/out.mp4")
    fc_idx = cmd.index("-filter_complex")
    graph = cmd[fc_idx + 1]
    # Extract everything between brackets — both [N:v] input refs and [vN] outputs.
    import re
    labels = re.findall(r"\[([a-zA-Z_0-9]+)\]", graph)
    # Output labels are v0..v3 + vout — each must appear exactly twice (once
    # as the chain's output, once as the concat's input). vout appears once
    # (chain output only — never consumed inside the graph).
    output_labels = [lbl for lbl in labels if lbl.startswith("v") and lbl != "vout"]
    counts = {lbl: output_labels.count(lbl) for lbl in set(output_labels)}
    # Each [vN] appears exactly twice (output of per-slot chain + input to concat).
    for lbl, count in counts.items():
        assert count == 2, f"label {lbl!r} appears {count} times, expected 2: {graph}"


# ── Test 9: slot order matches recipe ────────────────────────────────────────


def test_slot_order_matches_recipe():
    """Input order in argv == order in spec.inputs (off-by-one swap protection)."""
    spec = SinglePassSpec(inputs=[
        _clip(path="/tmp/A.mp4"),
        _hold(),
        _clip(path="/tmp/B.mp4"),
        _clip(path="/tmp/C.mp4"),
    ])
    cmd = build_single_pass_command(spec, "/tmp/out.mp4")
    # Pull the clip paths from -i flags, in argv order.
    clip_paths = [
        cmd[i + 1] for i, a in enumerate(cmd)
        if a == "-i" and cmd[i + 1].endswith(".mp4")
    ]
    assert clip_paths == ["/tmp/A.mp4", "/tmp/B.mp4", "/tmp/C.mp4"]


# ── Concat fragment is correct ───────────────────────────────────────────────


def test_concat_fragment_present_with_correct_n():
    """Hard-cut path uses `concat=n=K:v=1:a=0`."""
    spec = SinglePassSpec(inputs=[_clip(), _hold(), _clip(), _clip()])
    cmd = build_single_pass_command(spec, "/tmp/out.mp4")
    fc_idx = cmd.index("-filter_complex")
    graph = cmd[fc_idx + 1]
    assert "concat=n=4:v=1:a=0[vout]" in graph


# ── -ss before -i (fast keyframe seek) ──────────────────────────────────────


def test_ss_before_i_for_each_clip():
    """`-ss <start>` precedes `-i <path>` for each clip (matches reframe:250)."""
    spec = SinglePassSpec(inputs=[
        _clip(path="/tmp/A.mp4", start=3.0, end=5.0),
        _clip(path="/tmp/B.mp4", start=12.5, end=14.0),
    ])
    cmd = build_single_pass_command(spec, "/tmp/out.mp4")
    # For clip A: find "-ss 3.0" then "-t" then "-i /tmp/A.mp4".
    idx = cmd.index("/tmp/A.mp4")
    assert cmd[idx - 1] == "-i"
    assert cmd[idx - 2] == str(2.0)  # -t duration
    assert cmd[idx - 3] == "-t"
    assert cmd[idx - 4] == str(3.0)  # -ss start
    assert cmd[idx - 5] == "-ss"


# ── Bad inputs are rejected at build time, not runtime ──────────────────────


def test_clip_without_path_raises():
    spec = SinglePassSpec(inputs=[SinglePassInput(kind="clip")])
    with pytest.raises(ValueError, match="missing required"):
        build_single_pass_command(spec, "/tmp/out.mp4")


def test_zero_duration_clip_raises():
    spec = SinglePassSpec(
        inputs=[_clip(path="/tmp/a.mp4", start=5.0, end=5.0)],
    )
    with pytest.raises(ValueError, match="non-positive duration"):
        build_single_pass_command(spec, "/tmp/out.mp4")


def test_zero_duration_hold_raises():
    spec = SinglePassSpec(inputs=[_hold(duration=0.0)])
    with pytest.raises(ValueError, match="non-positive duration"):
        build_single_pass_command(spec, "/tmp/out.mp4")


# ── Per-clip filter chain wires _build_video_filter ──────────────────────────


def test_per_clip_chain_includes_setpts_for_speed_factor():
    """Speed-factored clips emit a setpts segment (_build_video_filter contract)."""
    spec = SinglePassSpec(inputs=[
        _clip(speed_factor=1.5, aspect_ratio="16:9"),
    ])
    cmd = build_single_pass_command(spec, "/tmp/out.mp4")
    fc_idx = cmd.index("-filter_complex")
    graph = cmd[fc_idx + 1]
    assert "setpts=PTS/1.5" in graph


def test_per_clip_chain_terminates_with_yuv420p():
    """Every per-slot chain MUST end with format=yuv420p so concat is safe.

    Mixed pixel formats break the concat filter silently.
    """
    spec = SinglePassSpec(inputs=[_clip(), _hold(), _clip()])
    cmd = build_single_pass_command(spec, "/tmp/out.mp4")
    fc_idx = cmd.index("-filter_complex")
    graph = cmd[fc_idx + 1]
    # Both clip chains and the hold chain must end in format=yuv420p before
    # the closing label [vN]. We assert by counting yuv420p in the graph —
    # at least one per slot.
    yuv_count = graph.count("yuv420p")
    assert yuv_count >= len(spec.inputs)


# ── Gate-point integration tests (flag on/off / force_single_pass kwarg) ────


class TestGatePoint:
    """Verify the _assemble_clips gate routes correctly across flag states.

    These tests mock the heavy ffmpeg subprocess calls and just assert WHICH
    branch runs (single-pass vs multi-pass).
    """

    @staticmethod
    def _make_step_and_probe(tmp_path, slot_idx: int = 1, transition_in: str = "none"):
        from unittest.mock import MagicMock

        from app.pipeline.probe import VideoProbe

        clip_file = tmp_path / f"clip_{slot_idx}.mp4"
        clip_file.write_bytes(b"fake")
        probe = VideoProbe(
            duration_s=10.0, fps=30.0, width=1920, height=1080,
            has_audio=True, codec="h264", aspect_ratio="16:9", file_size_bytes=4,
        )
        step = MagicMock()
        step.clip_id = f"clip_{slot_idx}"
        step.moment = {"start_s": 0.0, "end_s": 3.0}
        step.slot = {
            "position": slot_idx,
            "target_duration_s": 3.0,
            "transition_in": transition_in,
        }
        return step, probe, clip_file

    def test_flag_off_does_not_invoke_single_pass(self, tmp_path):
        """force_single_pass=False + env flag False → multi-pass path runs."""
        from unittest.mock import patch

        from app.tasks.template_orchestrate import _assemble_clips

        step, probe, clip_file = self._make_step_and_probe(tmp_path)
        with (
            patch("app.pipeline.reframe.reframe_and_export") as mock_reframe,
            patch("app.tasks.template_orchestrate.run_single_pass") as mock_single,
            patch("app.tasks.template_orchestrate.shutil.copy2"),
        ):
            _assemble_clips(
                steps=[step],
                clip_id_to_local={step.clip_id: str(clip_file)},
                clip_probe_map={str(clip_file): probe},
                output_path=str(tmp_path / "out.mp4"),
                tmpdir=str(tmp_path),
                force_single_pass=False,
            )
            mock_reframe.assert_called_once()
            mock_single.assert_not_called()

    def test_force_single_pass_invokes_single_pass(self, tmp_path):
        """force_single_pass=True overrides the env flag and runs single-pass."""
        from unittest.mock import patch

        from app.tasks.template_orchestrate import _assemble_clips

        step, probe, clip_file = self._make_step_and_probe(tmp_path)
        with (
            patch("app.pipeline.reframe.reframe_and_export") as mock_reframe,
            patch("app.tasks.template_orchestrate.run_single_pass") as mock_single,
            patch("app.tasks.template_orchestrate.shutil.copy2"),
        ):
            _assemble_clips(
                steps=[step],
                clip_id_to_local={step.clip_id: str(clip_file)},
                clip_probe_map={str(clip_file): probe},
                output_path=str(tmp_path / "out.mp4"),
                tmpdir=str(tmp_path),
                force_single_pass=True,
            )
            mock_single.assert_called_once()
            mock_reframe.assert_not_called()

    @pytest.mark.parametrize(
        "force,env,template,expected",
        [
            # (force_single_pass, settings.single_pass_encode_enabled, template.single_pass_enabled, expected)
            (False, False, False, False),  # default state — multi-pass
            (False, True, False, False),   # env flipped alone — no-op (template not allow-listed)
            (False, False, True, False),   # template flipped alone — no-op (env off)
            (False, True, True, True),     # both flags True — single-pass (the rollout case)
            (True, False, False, True),    # force overrides env=False
            (True, True, False, True),     # force overrides template=False
            (True, False, True, True),     # force overrides env=False (with template=True)
            (True, True, True, True),      # everything True
        ],
    )
    def test_effective_single_pass_truth_table(self, force, env, template, expected):
        """The two-flag AND-gate is the structural rollout guarantee — env
        flag alone or template flag alone must NOT enable single-pass; force
        must always win. Walks the full 8-row truth table so any future
        regression (swapping OR/AND, dropping a clause, changing precedence)
        fails loudly here instead of silently in production."""
        from unittest.mock import patch

        from app.tasks.template_orchestrate import _resolve_effective_single_pass

        with patch(
            "app.tasks.template_orchestrate.settings.single_pass_encode_enabled", env,
        ):
            assert _resolve_effective_single_pass(force, template) is expected

    def test_env_flag_alone_does_NOT_invoke_single_pass(self, tmp_path):
        """settings.single_pass_encode_enabled=True alone is NO LONGER
        sufficient at the _assemble_clips boundary. Per the per-template
        allow-list rollout, the env flag check moved UPSTREAM to
        _run_template_job / _run_rerender where it's AND-gated with
        ``template.single_pass_enabled``. _assemble_clips now treats
        force_single_pass as the sole gate. This regression-tests that
        setting the env flag without flipping a template still routes
        through multi-pass."""
        from unittest.mock import patch

        from app.tasks.template_orchestrate import _assemble_clips

        step, probe, clip_file = self._make_step_and_probe(tmp_path)
        with (
            patch("app.pipeline.reframe.reframe_and_export") as mock_reframe,
            patch("app.tasks.template_orchestrate.run_single_pass") as mock_single,
            patch("app.tasks.template_orchestrate.shutil.copy2"),
            patch("app.tasks.template_orchestrate.settings.single_pass_encode_enabled", True),
        ):
            _assemble_clips(
                steps=[step],
                clip_id_to_local={step.clip_id: str(clip_file)},
                clip_probe_map={str(clip_file): probe},
                output_path=str(tmp_path / "out.mp4"),
                tmpdir=str(tmp_path),
                force_single_pass=False,
            )
            mock_single.assert_not_called()
            mock_reframe.assert_called_once()

    def test_xfade_template_runs_single_pass(self, tmp_path):
        """force_single_pass=True + xfade transition_in → single-pass.

        M3 lifted the M2 xfade gate. _build_single_pass_spec now
        translates the transition_in vocabulary via
        ``translate_transition`` into spec.transitions and
        ``build_single_pass_command`` emits xfade filter nodes. This test
        replaces the prior ``test_xfade_template_falls_back_to_multi_pass``
        which asserted the opposite — correct for M2, wrong for M3+.
        """
        from unittest.mock import patch

        from app.tasks.template_orchestrate import _assemble_clips

        step1, probe1, file1 = self._make_step_and_probe(tmp_path, slot_idx=1)
        step2, probe2, file2 = self._make_step_and_probe(
            # Real Gemini vocabulary value; translate_transition() maps it
            # to the internal "crossfade" xfade family. Using the internal
            # name directly would fall through to "none" — the
            # _GEMINI_TO_INTERNAL map only knows the Gemini-side keys.
            tmp_path, slot_idx=2, transition_in="dissolve",
        )
        with (
            patch("app.pipeline.reframe.reframe_and_export") as mock_reframe,
            patch("app.tasks.template_orchestrate.run_single_pass") as mock_single,
            patch("app.tasks.template_orchestrate.shutil.copy2"),
        ):
            _assemble_clips(
                steps=[step1, step2],
                clip_id_to_local={step1.clip_id: str(file1), step2.clip_id: str(file2)},
                clip_probe_map={str(file1): probe1, str(file2): probe2},
                output_path=str(tmp_path / "out.mp4"),
                tmpdir=str(tmp_path),
                force_single_pass=True,
            )
            mock_single.assert_called_once()
            mock_reframe.assert_not_called()
            # The spec passed to run_single_pass must carry the translated
            # internal transition name, not the raw Gemini vocabulary.
            call_args = mock_single.call_args
            spec = call_args.args[0] if call_args.args else call_args.kwargs["spec"]
            assert spec.transitions == ["crossfade"]

    def test_unrelated_not_implemented_error_propagates(self, tmp_path):
        """Bare NotImplementedError from unrelated code MUST propagate.

        Lock the narrower exception catch — only SinglePassUnsupportedError
        triggers the fallback. A future library throwing NotImplementedError
        would crash the job loudly instead of being silently swallowed.
        """
        from unittest.mock import patch

        from app.tasks.template_orchestrate import _assemble_clips

        step, probe, clip_file = self._make_step_and_probe(tmp_path)
        with (
            patch("app.pipeline.reframe.reframe_and_export") as mock_reframe,
            patch("app.tasks.template_orchestrate.run_single_pass") as mock_single,
            patch("app.tasks.template_orchestrate.shutil.copy2"),
        ):
            mock_single.side_effect = NotImplementedError("hypothetical drift")
            with pytest.raises(NotImplementedError, match="hypothetical drift"):
                _assemble_clips(
                    steps=[step],
                    clip_id_to_local={step.clip_id: str(clip_file)},
                    clip_probe_map={str(clip_file): probe},
                    output_path=str(tmp_path / "out.mp4"),
                    tmpdir=str(tmp_path),
                    force_single_pass=True,
                )
            # The fake NotImplementedError should NOT have triggered fallback.
            mock_reframe.assert_not_called()

    def test_curtain_close_template_falls_back_to_multi_pass(self, tmp_path):
        """force_single_pass=True + curtain-close interstitial → multi-pass."""
        from unittest.mock import patch

        from app.tasks.template_orchestrate import _assemble_clips

        step, probe, clip_file = self._make_step_and_probe(tmp_path)
        interstitials = [
            {"after_slot": 1, "type": "curtain-close", "animate_s": 4.0},
        ]
        with (
            patch("app.pipeline.reframe.reframe_and_export") as mock_reframe,
            patch("app.tasks.template_orchestrate.run_single_pass") as mock_single,
            patch("app.tasks.template_orchestrate.shutil.copy2"),
            patch("app.pipeline.interstitials.apply_curtain_close_tail"),
            patch("app.tasks.template_orchestrate._pre_burn_curtain_slot_text") as mock_preburn,
            patch("app.tasks.template_orchestrate._join_or_concat"),
            patch("app.tasks.template_orchestrate._burn_text_overlays"),
            patch("app.tasks.template_orchestrate._probe_duration", return_value=4.0),
        ):
            mock_preburn.side_effect = lambda *a, **kw: a[0]  # passthrough
            _assemble_clips(
                steps=[step],
                clip_id_to_local={step.clip_id: str(clip_file)},
                clip_probe_map={str(clip_file): probe},
                output_path=str(tmp_path / "out.mp4"),
                tmpdir=str(tmp_path),
                force_single_pass=True,
                interstitials=interstitials,
            )
            mock_single.assert_not_called()
            mock_reframe.assert_called_once()

    def test_barn_door_open_template_falls_back_to_multi_pass(self, tmp_path):
        """force_single_pass=True + barn-door-open interstitial → multi-pass.

        Latent-bug guard. Multi-pass renders barn-door-open as a color hold
        AT the interstitial position PLUS a PNG-bar opening animation on the
        head of the NEXT slot. M2 single-pass only models the color hold,
        which would silently drop the slot-head animation. The gate at
        _build_single_pass_spec raises SinglePassUnsupportedError to force
        fallback until M4 lands; this test pins that behavior.
        """
        from unittest.mock import patch

        from app.tasks.template_orchestrate import _assemble_clips

        # Two slots so the barn-door head animation has somewhere to land.
        step1, probe1, file1 = self._make_step_and_probe(tmp_path, slot_idx=1)
        step2, probe2, file2 = self._make_step_and_probe(tmp_path, slot_idx=2)
        interstitials = [
            {"after_slot": 1, "type": "barn-door-open", "animate_s": 4.0, "hold_s": 0.5,
             "hold_color": "#000000"},
        ]
        with (
            patch("app.pipeline.reframe.reframe_and_export") as mock_reframe,
            patch("app.tasks.template_orchestrate.run_single_pass") as mock_single,
            patch("app.tasks.template_orchestrate.shutil.copy2"),
            patch("app.pipeline.interstitials.apply_barn_door_open_head"),
            patch("app.tasks.template_orchestrate._join_or_concat"),
            patch("app.tasks.template_orchestrate._burn_text_overlays"),
            patch("app.tasks.template_orchestrate._insert_interstitial"),
            patch("app.tasks.template_orchestrate._probe_duration", return_value=4.0),
        ):
            _assemble_clips(
                steps=[step1, step2],
                clip_id_to_local={step1.clip_id: str(file1), step2.clip_id: str(file2)},
                clip_probe_map={str(file1): probe1, str(file2): probe2},
                output_path=str(tmp_path / "out.mp4"),
                tmpdir=str(tmp_path),
                force_single_pass=True,
                interstitials=interstitials,
            )
            mock_single.assert_not_called()
            assert mock_reframe.call_count == 2


# ── Direct unit tests for _build_single_pass_spec ────────────────────────────


class TestBuildSinglePassSpec:
    """Lock the SlotPlan + interstitial_map → SinglePassSpec contract.

    The spec builder is the trickiest part of the M2 diff: position-keyed
    interstitial lookup, transition gating, grid params passthrough,
    duration math (including the speed_factor divide). Direct unit tests
    catch off-by-one issues without needing the full orchestrator harness.
    """

    @staticmethod
    def _plan(slot_idx: int = 0, **overrides):
        from app.tasks.template_orchestrate import SlotPlan
        defaults = dict(
            slot_idx=slot_idx,
            clip_path=f"/tmp/clip_{slot_idx}.mp4",
            start_s=0.0, end_s=3.0,
            speed_factor=1.0,
            slot_target_dur=3.0,
            cumulative_s=0.0,
            aspect_ratio="16:9",
            color_hint="none",
            reframed_path=f"/tmp/slot_{slot_idx}.mp4",
            color_transfer="",
            color_trc="bt709",
            has_audio=True,
            output_fit="crop",
            has_grid=False,
        )
        defaults.update(overrides)
        return SlotPlan(**defaults)

    @staticmethod
    def _step(position: int = 1, transition_in: str = "none"):
        step = MagicMock()
        step.clip_id = f"clip_{position}"
        step.slot = {"position": position, "transition_in": transition_in}
        return step

    def test_single_clip_produces_one_input(self):
        from app.tasks.template_orchestrate import _build_single_pass_spec
        spec = _build_single_pass_spec(
            plans=[self._plan(slot_idx=0)],
            interstitial_map={},
            steps=[self._step(position=1)],
        )
        assert len(spec.inputs) == 1
        assert spec.inputs[0].kind == "clip"
        assert spec.inputs[0].clip_path == "/tmp/clip_0.mp4"
        assert spec.transitions == []

    def test_three_clips_in_slot_order(self):
        from app.tasks.template_orchestrate import _build_single_pass_spec
        spec = _build_single_pass_spec(
            plans=[
                self._plan(slot_idx=0, clip_path="/tmp/A.mp4"),
                self._plan(slot_idx=1, clip_path="/tmp/B.mp4"),
                self._plan(slot_idx=2, clip_path="/tmp/C.mp4"),
            ],
            interstitial_map={},
            steps=[self._step(1), self._step(2), self._step(3)],
        )
        assert [i.clip_path for i in spec.inputs] == [
            "/tmp/A.mp4", "/tmp/B.mp4", "/tmp/C.mp4",
        ]

    def test_color_hold_interstitial_interleaved(self):
        """Non-curtain interstitial after slot 1 → color_hold input between clips."""
        from app.tasks.template_orchestrate import _build_single_pass_spec
        interstitial_map = {1: {"type": "fade-black-hold", "hold_s": 0.75, "hold_color": "#000000"}}
        spec = _build_single_pass_spec(
            plans=[self._plan(slot_idx=0), self._plan(slot_idx=1)],
            interstitial_map=interstitial_map,
            steps=[self._step(position=1), self._step(position=2)],
        )
        assert [i.kind for i in spec.inputs] == ["clip", "color_hold", "clip"]
        assert spec.inputs[1].hold_color == "black"
        assert spec.inputs[1].hold_s == 0.75

    def test_white_color_hold_mapped_correctly(self):
        from app.tasks.template_orchestrate import _build_single_pass_spec
        interstitial_map = {1: {"type": "fade-white-hold", "hold_s": 0.5, "hold_color": "#FFFFFF"}}
        spec = _build_single_pass_spec(
            plans=[self._plan(slot_idx=0)],
            interstitial_map=interstitial_map,
            steps=[self._step(position=1)],
        )
        assert spec.inputs[1].hold_color == "white"

    def test_curtain_close_raises_unsupported(self):
        from app.tasks.template_orchestrate import _build_single_pass_spec
        interstitial_map = {1: {"type": "curtain-close", "animate_s": 4.0}}
        with pytest.raises(SinglePassUnsupportedError, match="curtain-close"):
            _build_single_pass_spec(
                plans=[self._plan(slot_idx=0)],
                interstitial_map=interstitial_map,
                steps=[self._step(position=1)],
            )

    def test_barn_door_open_raises_unsupported(self):
        """barn-door-open has a slot-head PNG-bar animation that M2 cannot
        model. The gate must reject it like curtain-close — otherwise it
        silently falls through the color-hold branch and ships output that's
        missing the hero reveal animation. No production template uses
        barn-door-open today (PR #134, May 2026); this is the future-proof."""
        from app.tasks.template_orchestrate import _build_single_pass_spec
        interstitial_map = {1: {"type": "barn-door-open", "animate_s": 4.0}}
        with pytest.raises(SinglePassUnsupportedError, match="barn-door-open"):
            _build_single_pass_spec(
                plans=[self._plan(slot_idx=0)],
                interstitial_map=interstitial_map,
                steps=[self._step(position=1)],
            )

    def test_xfade_transition_in_translates_to_internal_name(self):
        """M3: non-"none" transition_in is translated via the Gemini
        vocabulary map, not raised on. ``crossfade`` is the internal
        xfade family used in _XFADE_MAP. ``dissolve`` and ``zoom-in``
        from Gemini both translate to ``crossfade`` per
        transitions._GEMINI_TO_INTERNAL — covered here so spec.transitions
        accurately reflects what xfade filter type will be emitted."""
        from app.tasks.template_orchestrate import _build_single_pass_spec
        spec = _build_single_pass_spec(
            plans=[self._plan(slot_idx=0), self._plan(slot_idx=1)],
            interstitial_map={},
            steps=[
                self._step(position=1),
                self._step(position=2, transition_in="dissolve"),
            ],
        )
        assert spec.transitions == ["crossfade"]

    def test_whip_pan_translates_to_wipe_left(self):
        """Gemini ``whip-pan`` → internal ``wipe_left`` → xfade wipeleft.
        Pins the translation chain so the wipe direction doesn't drift."""
        from app.tasks.template_orchestrate import _build_single_pass_spec
        spec = _build_single_pass_spec(
            plans=[self._plan(slot_idx=0), self._plan(slot_idx=1)],
            interstitial_map={},
            steps=[
                self._step(position=1),
                self._step(position=2, transition_in="whip-pan"),
            ],
        )
        assert spec.transitions == ["wipe_left"]

    def test_hold_forces_none_transition_after_interstitial(self):
        """When a fade-black-hold interstitial sits between slot 0 and
        slot 1, the transition INTO slot 1 must be "none" — the hold IS
        the transition effect. Mirrors multi-pass behavior at
        template_orchestrate.py:2204-2209 where prev_had_interstitial
        forces transition_types[i] = "none". Without this, single-pass
        would emit an xfade ON TOP OF the hold, double-stacking effects."""
        from app.tasks.template_orchestrate import _build_single_pass_spec
        interstitial_map = {1: {"type": "fade-black-hold", "hold_s": 0.5, "hold_color": "#000000"}}
        spec = _build_single_pass_spec(
            plans=[self._plan(slot_idx=0), self._plan(slot_idx=1)],
            interstitial_map=interstitial_map,
            steps=[
                self._step(position=1),
                # The recipe asks for crossfade INTO slot 1, but the
                # preceding hold overrides it.
                self._step(position=2, transition_in="crossfade"),
            ],
        )
        assert [i.kind for i in spec.inputs] == ["clip", "color_hold", "clip"]
        # 3 inputs → 2 gaps. Both must be "none" — clip→hold and hold→clip.
        assert spec.transitions == ["none", "none"]

    def test_first_slot_transition_in_is_ignored(self):
        """slot_idx=0 never has a transition (nothing precedes it)."""
        from app.tasks.template_orchestrate import _build_single_pass_spec
        spec = _build_single_pass_spec(
            plans=[self._plan(slot_idx=0)],
            interstitial_map={},
            # Even an absurd transition_in on slot 0 is ignored.
            steps=[self._step(position=1, transition_in="whip-pan")],
        )
        assert len(spec.inputs) == 1

    def test_grid_params_passthrough_when_has_grid(self):
        from app.tasks.template_orchestrate import _build_single_pass_spec
        plan = self._plan(
            slot_idx=0,
            has_grid=True,
            grid_color="#FF0000",
            grid_opacity=0.5,
            grid_thickness=4,
            grid_highlight_intersection="top-left",
            grid_highlight_color="#00FF00",
            grid_highlight_windows=[(0.0, 1.0)],
        )
        spec = _build_single_pass_spec(
            plans=[plan],
            interstitial_map={},
            steps=[self._step(position=1)],
        )
        gp = spec.inputs[0].grid_params
        assert spec.inputs[0].has_grid is True
        assert gp["grid_color"] == "#FF0000"
        assert gp["grid_opacity"] == 0.5
        assert gp["grid_thickness"] == 4
        assert gp["grid_highlight_intersection"] == "top-left"
        assert gp["grid_highlight_windows"] == [(0.0, 1.0)]

    def test_grid_params_empty_when_no_grid(self):
        from app.tasks.template_orchestrate import _build_single_pass_spec
        plan = self._plan(slot_idx=0, has_grid=False)
        spec = _build_single_pass_spec(
            plans=[plan],
            interstitial_map={},
            steps=[self._step(position=1)],
        )
        assert spec.inputs[0].grid_params == {}

    def test_output_duration_includes_speed_factor(self):
        """duration = (end_s - start_s) / speed_factor — speed-2x halves it."""
        from app.tasks.template_orchestrate import _build_single_pass_spec
        spec = _build_single_pass_spec(
            plans=[
                self._plan(slot_idx=0, start_s=0.0, end_s=4.0, speed_factor=1.0),
                self._plan(slot_idx=1, start_s=0.0, end_s=4.0, speed_factor=2.0),
            ],
            interstitial_map={},
            steps=[self._step(1), self._step(2)],
        )
        # slot 0: 4.0s @ 1x = 4.0s, slot 1: 4.0s @ 2x = 2.0s → total 6.0s
        assert spec.output_duration_s == pytest.approx(6.0)

    def test_output_duration_includes_hold_s(self):
        from app.tasks.template_orchestrate import _build_single_pass_spec
        inter = {"type": "fade-black-hold", "hold_s": 1.5, "hold_color": "#000000"}
        spec = _build_single_pass_spec(
            plans=[self._plan(slot_idx=0, start_s=0.0, end_s=3.0)],
            interstitial_map={1: inter},
            steps=[self._step(position=1)],
        )
        assert spec.output_duration_s == pytest.approx(4.5)

    def test_position_from_slot_dict_drives_interstitial_lookup(self):
        """Interstitial map is keyed by step.slot['position'], NOT slot_idx.

        If a recipe has gaps in slot positions, the interstitial map's keys
        match positions, not loop indices. Confirm we look it up correctly.
        """
        from app.tasks.template_orchestrate import _build_single_pass_spec
        # Slot at position 5 (not 1!) has interstitial after_slot=5.
        interstitial_map = {5: {"type": "fade-black-hold", "hold_s": 0.5, "hold_color": "#000000"}}
        spec = _build_single_pass_spec(
            plans=[self._plan(slot_idx=0)],
            interstitial_map=interstitial_map,
            steps=[self._step(position=5)],
        )
        # Should have BOTH clip and hold (lookup by position=5 hit).
        assert [i.kind for i in spec.inputs] == ["clip", "color_hold"]
