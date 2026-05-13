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


# ── Test 3: xfade lands in milestone 3 (placeholder for now) ─────────────────


def test_non_none_transition_raises_until_milestone_3():
    spec = SinglePassSpec(
        inputs=[_clip(), _clip()],
        transitions=["crossfade"],
    )
    with pytest.raises(NotImplementedError, match="milestone 3"):
        build_single_pass_command(spec, "/tmp/out.mp4")


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

    def test_env_flag_on_invokes_single_pass(self, tmp_path):
        """settings.single_pass_encode_enabled=True is sufficient (no kwarg needed)."""
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
            mock_single.assert_called_once()
            mock_reframe.assert_not_called()

    def test_xfade_template_falls_back_to_multi_pass(self, tmp_path):
        """force_single_pass=True + xfade transition_in → fall back to multi-pass.

        M2 doesn't support xfade. _build_single_pass_spec raises
        NotImplementedError, the gate catches it, structured warning is
        logged, and the multi-pass path runs as if the flag were off.

        We mock all downstream multi-pass helpers since the mocked
        reframe_and_export doesn't actually write slot files; this test
        is about gate-point routing, not full multi-pass execution.
        """
        from unittest.mock import patch

        from app.tasks.template_orchestrate import _assemble_clips

        # Two slots, second has a non-"none" transition_in.
        step1, probe1, file1 = self._make_step_and_probe(tmp_path, slot_idx=1)
        step2, probe2, file2 = self._make_step_and_probe(
            tmp_path, slot_idx=2, transition_in="crossfade",
        )
        with (
            patch("app.pipeline.reframe.reframe_and_export") as mock_reframe,
            patch("app.tasks.template_orchestrate.run_single_pass") as mock_single,
            patch("app.tasks.template_orchestrate.shutil.copy2"),
            patch("app.tasks.template_orchestrate._join_or_concat"),
            patch("app.tasks.template_orchestrate._burn_text_overlays"),
            patch("app.tasks.template_orchestrate._probe_duration", return_value=6.0),
        ):
            _assemble_clips(
                steps=[step1, step2],
                clip_id_to_local={step1.clip_id: str(file1), step2.clip_id: str(file2)},
                clip_probe_map={str(file1): probe1, str(file2): probe2},
                output_path=str(tmp_path / "out.mp4"),
                tmpdir=str(tmp_path),
                force_single_pass=True,
            )
            # Fell back to multi-pass — single-pass NOT called, reframe IS.
            mock_single.assert_not_called()
            assert mock_reframe.call_count == 2

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

    def test_xfade_transition_in_raises_unsupported(self):
        from app.tasks.template_orchestrate import _build_single_pass_spec
        with pytest.raises(SinglePassUnsupportedError, match="xfade lands"):
            _build_single_pass_spec(
                plans=[self._plan(slot_idx=0), self._plan(slot_idx=1)],
                interstitial_map={},
                steps=[
                    self._step(position=1),
                    self._step(position=2, transition_in="crossfade"),
                ],
            )

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
