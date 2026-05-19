"""Tests for `_build_preburn_inputs_and_graph` and the surrounding
`_pre_burn_curtain_slot_text` failure semantics.

Context: job b6540038 (Dimples Passport, location=Morocco) shipped without
its MOROCCO font-cycle reveal because:

1. Slot 5's font-cycle generated 57 PNG frames over 5.5 s; feeding all 57
   as separate `-i` inputs to ffmpeg on the 2 GB Fly worker SIGKILLed the
   subprocess (rc=-9).
2. `_pre_burn_curtain_slot_text` caught the non-zero return code and
   silently returned the textless input clip. `apply_curtain_close_tail`
   then encoded the curtain over a slot with no text.

The helper added here splits font-cycle PNGs (streamed via the concat
demuxer, constant-RAM) from static PNGs (one `-i` per PNG, byte-identical
to the pre-fix code). The wrapper now raises on non-zero return instead of
silently downgrading, matching the surrounding pattern for
`curtain_close_tail_failed` / `animated_overlay_failed`.

These tests pin both the new helper's structural contract AND the
no-behavior-change guarantee for templates that don't trigger the cycle
path (every existing curtain-close template at the time of this fix).
"""

from __future__ import annotations

import os
from unittest.mock import MagicMock, patch

import pytest

# ──────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────


def _static_cfg(name: str, start_s: float, end_s: float) -> dict:
    """Build a PNG config dict shaped like what generate_text_overlay_png returns
    for a static overlay (filename does NOT contain 'fontcycle')."""
    return {
        "png_path": f"/tmp/preburn_test/slot_0_overlay_{name}.png",
        "start_s": start_s,
        "end_s": end_s,
    }


def _cycle_cfg(idx: int, start_s: float, end_s: float, kind: str = "cycle") -> dict:
    """Build a PNG config dict shaped like what _render_font_cycle writes.

    `_render_font_cycle` names its frames `slot_{S}_fontcycle_{O}_{idx}.png`
    (or `_settle.png` / `_{idx}_gapfill.png`). The helper splits by the
    `fontcycle` substring in the basename, so any of these names enters the
    concat path.
    """
    if kind == "settle":
        name = "slot_0_fontcycle_0_settle.png"
    elif kind == "gapfill":
        name = f"slot_0_fontcycle_0_{idx}_gapfill.png"
    else:
        name = f"slot_0_fontcycle_0_{idx}.png"
    return {
        "png_path": f"/tmp/preburn_test/{name}",
        "start_s": start_s,
        "end_s": end_s,
    }


def _split_ffmpeg_inputs(ffmpeg_inputs: list[str]) -> dict:
    """Parse the `ffmpeg_inputs` list into a structured view for assertions.

    Returns ``{"clip": <clip path>, "concat_list": <list path or None>,
    "png_inputs": [<path>, ...]}``.
    """
    assert ffmpeg_inputs[0] == "-i", f"expected leading -i, got {ffmpeg_inputs[0]}"
    clip = ffmpeg_inputs[1]
    rest = ffmpeg_inputs[2:]

    concat_list = None
    png_inputs: list[str] = []
    i = 0
    while i < len(rest):
        if rest[i] == "-f":
            # -f concat -safe 0 -i <list>
            assert rest[i + 1] == "concat"
            assert rest[i + 2] == "-safe"
            assert rest[i + 3] == "0"
            assert rest[i + 4] == "-i"
            concat_list = rest[i + 5]
            i += 6
        elif rest[i] == "-i":
            png_inputs.append(rest[i + 1])
            i += 2
        else:
            raise AssertionError(f"unexpected token in ffmpeg_inputs at idx {i}: {rest[i]}")
    return {"clip": clip, "concat_list": concat_list, "png_inputs": png_inputs}


# ──────────────────────────────────────────────────────────────────────────
# Tests 1-4, 10-11: helper structural contract
# ──────────────────────────────────────────────────────────────────────────


class TestBuildPreburnInputsAndGraph:
    """`_build_preburn_inputs_and_graph` is the heart of the fix; these tests
    pin its structural contract so a future refactor can't accidentally
    re-introduce the 58-input chain that OOMed job b6540038."""

    def test_no_cycle_uses_legacy_path(self, tmp_path):
        """Test 1: when png_configs contains zero `fontcycle_` PNGs (every
        curtain-close template at the time of this fix that ISN'T Dimples
        Passport), the builder emits one `-i` per PNG and a chain of overlay
        links — byte-identical to the pre-fix code. No concat demuxer.

        This is the safety net for the user's hard constraint: 'all other
        templates are working so you shouldn't change anything in their
        pipeline.'"""
        from app.tasks.template_orchestrate import _build_preburn_inputs_and_graph

        statics = [
            _static_cfg("a", 0.0, 1.5),
            _static_cfg("b", 1.0, 3.0),
            _static_cfg("c", 2.5, 4.0),
        ]
        burned = str(tmp_path / "slot_X_textburned.mp4")
        ffmpeg_inputs, fc_parts, last_label = _build_preburn_inputs_and_graph(
            "/tmp/clip.mp4",
            statics,
            burned,
            str(tmp_path),
        )

        parsed = _split_ffmpeg_inputs(ffmpeg_inputs)
        assert parsed["clip"] == "/tmp/clip.mp4"
        assert parsed["concat_list"] is None, "legacy path must NOT use concat demuxer"
        assert parsed["png_inputs"] == [c["png_path"] for c in statics]

        # Filter graph: base + one overlay per PNG
        assert fc_parts[0] == "[0:v]null[base]"
        assert len(fc_parts) == 1 + len(statics)
        # Each overlay link references the correct -i index (1..N) and the
        # correct enable= timing
        for i, cfg in enumerate(statics):
            expected = (
                f"[{'base' if i == 0 else f'txt{i - 1}'}][{i + 1}:v]overlay=0:0"
                f":enable='between(t,{cfg['start_s']:.3f},{cfg['end_s']:.3f})'[txt{i}]"
            )
            assert fc_parts[i + 1] == expected, (
                f"filter part {i + 1} mismatch:\n"
                f"  expected: {expected}\n"
                f"  actual:   {fc_parts[i + 1]}"
            )
        assert last_label == f"txt{len(statics) - 1}"

        # Crucially: no `cycle_concat.txt` file should have been written.
        assert not (tmp_path / (os.path.basename(burned) + ".cycle_concat.txt")).exists()

    def test_cycle_uses_concat_demuxer(self, tmp_path):
        """Test 2: when 2+ font-cycle PNGs are present, the builder emits ONE
        `-f concat -safe 0 -i <list>` for the cycle stream, plus per-PNG `-i`
        for any statics. Filter graph has exactly one overlay for the cycle
        plus one per static.

        This is the path that survives the 2 GB Fly worker for job b6540038."""
        from app.tasks.template_orchestrate import _build_preburn_inputs_and_graph

        cycle = [
            _cycle_cfg(0, 0.000, 0.150),
            _cycle_cfg(1, 0.150, 0.300),
            _cycle_cfg(2, 0.300, 0.450),
        ]
        statics = [_static_cfg("welcome", 0.0, 1.0)]
        burned = str(tmp_path / "slot_5_textburned.mp4")

        ffmpeg_inputs, fc_parts, last_label = _build_preburn_inputs_and_graph(
            "/tmp/clip.mp4",
            cycle + statics,
            burned,
            str(tmp_path),
        )

        parsed = _split_ffmpeg_inputs(ffmpeg_inputs)
        assert parsed["clip"] == "/tmp/clip.mp4"
        # Exactly one concat list — the cycle stream
        assert parsed["concat_list"] is not None
        assert parsed["concat_list"].endswith(".cycle_concat.txt")
        # Statics stay on the per-PNG path
        assert parsed["png_inputs"] == [s["png_path"] for s in statics]

        # Filter graph: base, one overlay for cycle (input 1), one overlay
        # per static (input 2, 3, ...)
        assert fc_parts[0] == "[0:v]null[base]"
        # Cycle overlay uses cycle[0].start_s and cycle[-1].end_s
        assert "[1:v]overlay=0:0" in fc_parts[1]
        assert ":enable='between(t,0.000,0.450)'" in fc_parts[1]
        assert fc_parts[1].endswith("[fc]")
        # Static overlay uses input 2
        assert fc_parts[2].startswith("[fc][2:v]overlay=0:0")
        assert ":enable='between(t,0.000,1.000)'" in fc_parts[2]
        assert last_label == "txt0"

    def test_cycle_concat_list_format(self, tmp_path):
        """Test 3: the concat list file contains `file '<path>'` / `duration
        <secs>` pairs in start_s order, and the LAST file is re-listed so
        ffmpeg honors its duration. This is the ffmpeg-concat-demuxer quirk
        called out in https://trac.ffmpeg.org/wiki/Slideshow#Concatdemuxer
        — without the re-listing the final cycle frame collapses to 0 ms.

        The slow→fast cadence (0.15 s slow / 0.07 s fast, from
        _compute_font_cycle_frame_specs) is preserved bit-for-bit by these
        per-file `duration` lines, which is the user's load-bearing
        constraint for this fix."""
        from app.tasks.template_orchestrate import _build_preburn_inputs_and_graph

        # Slow phase + fast phase, mirroring the Dimples slot 5 inflection
        cycle = [
            _cycle_cfg(0, 0.000, 0.150),  # slow
            _cycle_cfg(1, 0.150, 0.300),  # slow
            _cycle_cfg(2, 0.300, 0.450),  # slow
            _cycle_cfg(3, 0.450, 0.520),  # fast (accel kicked in)
            _cycle_cfg(4, 0.520, 0.590),  # fast
        ]
        burned = str(tmp_path / "slot_5_textburned.mp4")
        ffmpeg_inputs, _, _ = _build_preburn_inputs_and_graph(
            "/tmp/clip.mp4",
            cycle,
            burned,
            str(tmp_path),
        )

        parsed = _split_ffmpeg_inputs(ffmpeg_inputs)
        list_path = parsed["concat_list"]
        assert list_path is not None
        content = open(list_path).read()
        lines = [ln for ln in content.splitlines() if ln.strip()]

        # 5 frames → 5 (file + duration) pairs = 10 lines, plus the final
        # re-listed file = 11 lines total.
        assert len(lines) == 11, f"expected 11 lines, got {len(lines)}:\n{content}"

        # First 10 lines are pair-wise file/duration
        for i, cfg in enumerate(cycle):
            file_line = lines[i * 2]
            dur_line = lines[i * 2 + 1]
            assert file_line == f"file '{cfg['png_path']}'", (
                f"line {i * 2} expected file '{cfg['png_path']}', got {file_line}"
            )
            expected_dur = cfg["end_s"] - cfg["start_s"]
            assert dur_line == f"duration {expected_dur:.6f}", (
                f"line {i * 2 + 1} expected duration {expected_dur:.6f}, got {dur_line}"
            )

        # Last line re-lists the final file (without a duration). This is
        # the ffmpeg-quirk fix — without this the 0.070s on frame 4 would
        # be silently ignored.
        assert lines[10] == f"file '{cycle[-1]['png_path']}'"

        # And the slow→fast inflection IS in the list (test 3's whole point)
        durations = [float(ln.split()[1]) for ln in lines if ln.startswith("duration ")]
        assert any(abs(d - 0.150) < 0.001 for d in durations), "expected a 0.150 slow duration"
        assert any(abs(d - 0.070) < 0.001 for d in durations), "expected a 0.070 fast duration"

    def test_single_cycle_frame_treated_as_static(self, tmp_path):
        """Test 4: when `_render_font_cycle` falls back to a single PNG
        (because `_resolve_cycle_fonts` returned fewer than 2 fonts), that
        PNG takes the per-PNG static path — NOT the concat demuxer. The
        concat demuxer's "last duration is ignored unless re-listed" quirk
        makes a single-entry list unsafe.

        The fallback path is exercised by _render_font_cycle:1158-1169."""
        from app.tasks.template_orchestrate import _build_preburn_inputs_and_graph

        # One cycle PNG (the fallback) + a static "Welcome to"
        configs = [
            _cycle_cfg(0, 0.0, 3.0),  # filename contains "fontcycle" but only 1 frame
            _static_cfg("welcome", 0.0, 1.5),
        ]
        burned = str(tmp_path / "slot_5_textburned.mp4")
        ffmpeg_inputs, fc_parts, last_label = _build_preburn_inputs_and_graph(
            "/tmp/clip.mp4",
            configs,
            burned,
            str(tmp_path),
        )

        parsed = _split_ffmpeg_inputs(ffmpeg_inputs)
        assert parsed["concat_list"] is None, (
            "single cycle frame must NOT trigger concat demuxer (avoids ffmpeg's "
            "last-duration-ignored quirk on a 1-entry list)"
        )
        # Both PNGs are per-input
        assert parsed["png_inputs"] == [c["png_path"] for c in configs]
        # Filter graph has base + 2 overlays
        assert len(fc_parts) == 3
        assert last_label == "txt1"

        # And no concat list file was written
        assert not (tmp_path / (os.path.basename(burned) + ".cycle_concat.txt")).exists()

    def test_cycle_only_no_statics_emits_concat_and_no_per_png(self, tmp_path):
        """Bonus coverage: when ALL configs are cycle frames (which is the
        common case — Dimples slot 5 was 57 cycle + 1 static, but a different
        template could be cycle-only), the builder emits ONE concat input
        and zero per-PNG inputs."""
        from app.tasks.template_orchestrate import _build_preburn_inputs_and_graph

        cycle = [_cycle_cfg(i, i * 0.15, (i + 1) * 0.15) for i in range(5)]
        burned = str(tmp_path / "slot_5_textburned.mp4")
        ffmpeg_inputs, fc_parts, last_label = _build_preburn_inputs_and_graph(
            "/tmp/clip.mp4",
            cycle,
            burned,
            str(tmp_path),
        )

        parsed = _split_ffmpeg_inputs(ffmpeg_inputs)
        assert parsed["concat_list"] is not None
        assert parsed["png_inputs"] == []  # no statics
        assert last_label == "fc"  # cycle overlay is the final label
        # Filter graph: base + just the cycle overlay
        assert len(fc_parts) == 2
        assert fc_parts[1].endswith("[fc]")

    def test_no_behavior_change_for_legacy_argv_snapshot(self, tmp_path):
        """Test 10: snapshot test — for an input that mirrors the previous
        behavior of every non-Dimples template (only static PNGs), the
        generated ffmpeg argv is identical to what the pre-fix code would
        have produced. Locks the no-other-templates-change guarantee.

        The snapshot is constructed programmatically from the inputs so it
        stays maintainable. The assertion is that the *shape* of the
        returned tuple matches the legacy pattern when no cycle PNGs exist.
        """
        from app.tasks.template_orchestrate import _build_preburn_inputs_and_graph

        statics = [
            _static_cfg("welcome", 0.0, 1.1),
            _static_cfg("subhook", 0.5, 3.0),
        ]
        burned = str(tmp_path / "slot_0_textburned.mp4")

        ffmpeg_inputs, fc_parts, last_label = _build_preburn_inputs_and_graph(
            "/tmp/clip.mp4",
            statics,
            burned,
            str(tmp_path),
        )

        # Legacy pre-fix shape: ["-i", clip, "-i", png0, "-i", png1, ...]
        expected_inputs = ["-i", "/tmp/clip.mp4"]
        for s in statics:
            expected_inputs.extend(["-i", s["png_path"]])
        assert ffmpeg_inputs == expected_inputs, (
            f"legacy argv inputs diverged from pre-fix shape:\n"
            f"  expected: {expected_inputs}\n"
            f"  actual:   {ffmpeg_inputs}"
        )

        # Legacy pre-fix filter shape:
        # ["[0:v]null[base]", "[base][1:v]overlay...[txt0]", "[txt0][2:v]...[txt1]"]
        expected_fc = ["[0:v]null[base]"]
        prev = "base"
        for j, cfg in enumerate(statics):
            expected_fc.append(
                f"[{prev}][{j + 1}:v]overlay=0:0"
                f":enable='between(t,{cfg['start_s']:.3f},{cfg['end_s']:.3f})'[txt{j}]"
            )
            prev = f"txt{j}"
        assert fc_parts == expected_fc
        assert last_label == f"txt{len(statics) - 1}"

    @pytest.mark.parametrize(
        "configs_factory,description",
        [
            (lambda: [_static_cfg("a", 0.0, 1.0)], "single static (other templates)"),
            (
                lambda: [_static_cfg("a", 0.0, 1.0), _static_cfg("b", 0.5, 2.0)],
                "two statics (other templates)",
            ),
            (lambda: [_cycle_cfg(0, 0.0, 0.15)], "single-frame cycle fallback"),
            (
                lambda: [_cycle_cfg(0, 0.0, 0.15), _static_cfg("welcome", 0.0, 1.0)],
                "single-frame cycle + static",
            ),
        ],
    )
    def test_no_concat_demuxer_for_legacy_inputs(
        self,
        tmp_path,
        configs_factory,
        description,
    ):
        """Test 11: parametrized lock that any input shape that does NOT
        contain 2+ cycle PNGs stays on the legacy per-PNG path. Mirrors
        every input mix the existing curtain-close templates produce."""
        from app.tasks.template_orchestrate import _build_preburn_inputs_and_graph

        configs = configs_factory()
        burned = str(tmp_path / "snap_burned.mp4")
        ffmpeg_inputs, _, _ = _build_preburn_inputs_and_graph(
            "/tmp/clip.mp4",
            configs,
            burned,
            str(tmp_path),
        )
        parsed = _split_ffmpeg_inputs(ffmpeg_inputs)
        assert parsed["concat_list"] is None, (
            f"{description}: should stay on legacy path (no concat demuxer)"
        )

    def test_concat_list_escapes_apostrophe_in_path(self, tmp_path):
        """Defense-in-depth: the concat-demuxer file directive uses
        POSIX-shell-style single-quoting. A literal apostrophe in a PNG path
        would otherwise terminate the quoted string early and corrupt the
        concat parse. The helper must escape `'` → `'\\''` so any future
        tmpdir or asset path containing an apostrophe is handled."""
        from app.tasks.template_orchestrate import _build_preburn_inputs_and_graph

        # Forge a config whose path contains an apostrophe
        weird_path = str(tmp_path / "it's_fontcycle_0.png")
        configs = [
            {"png_path": weird_path, "start_s": 0.0, "end_s": 0.15},
            {"png_path": weird_path.replace("_0", "_1"), "start_s": 0.15, "end_s": 0.30},
        ]
        burned = str(tmp_path / "apostrophe_burned.mp4")
        ffmpeg_inputs, _, _ = _build_preburn_inputs_and_graph(
            "/tmp/clip.mp4",
            configs,
            burned,
            str(tmp_path),
        )
        parsed = _split_ffmpeg_inputs(ffmpeg_inputs)
        assert parsed["concat_list"] is not None
        content = open(parsed["concat_list"]).read()
        # The literal apostrophe MUST be escaped — never appear as a bare `'`
        # inside the quoted value. The escape sequence is `'\''` (close quote,
        # backslash-quote, reopen quote).
        assert "it's_fontcycle" not in content.replace("'\\''", "<ESCAPED>"), (
            "raw apostrophe leaked into concat list — quote terminator risk"
        )
        # The escape sequence should be present for each path
        assert "it'\\''s_fontcycle" in content, (
            f"expected POSIX-style apostrophe escape in concat list, got:\n{content}"
        )

    def test_fontcycle_naming_contract_with_text_overlay(self, tmp_path):
        """Coupling contract: `_build_preburn_inputs_and_graph` detects cycle
        frames by checking 'fontcycle' is in the PNG basename. That convention
        comes from `_render_font_cycle` in text_overlay.py. If anyone renames
        the convention without updating the helper, cycle frames would silently
        fall into the per-PNG path and the OOM regression returns.

        Pin the contract by generating real cycle PNGs and asserting EVERY
        emitted PNG matches the helper's substring check."""
        from app.pipeline.text_overlay import generate_text_overlay_png

        overlays = [
            {
                "text": "TEST",
                "start_s": 0.0,
                "end_s": 1.5,
                "position": "center",
                "effect": "font-cycle",
                "font_style": "display",
                "text_size": "medium",
                "text_size_px": 120,
                "text_color": "#FFFFFF",
                "position_x_frac": None,
                "position_y_frac": 0.5,
                "font_cycle_accel_at_s": 0.9,
            }
        ]
        cfgs = generate_text_overlay_png(overlays, 1.5, str(tmp_path), slot_index=0)
        assert cfgs and len(cfgs) >= 2, (
            "font-cycle should produce 2+ frames for a 1.5s slot — env may be missing fonts"
        )
        for c in cfgs:
            base = os.path.basename(c["png_path"])
            assert "fontcycle" in base, (
                f"_render_font_cycle emitted '{base}' which does NOT contain "
                f"'fontcycle'. The helper relies on this substring to route "
                f"frames through the concat demuxer. Update "
                f"_build_preburn_inputs_and_graph if the naming convention "
                f"intentionally changed."
            )


# ──────────────────────────────────────────────────────────────────────────
# Tests 5-6: _pre_burn_curtain_slot_text failure semantics (Change 2)
# ──────────────────────────────────────────────────────────────────────────


class TestPreBurnCurtainSlotTextFailureSemantics:
    """`_pre_burn_curtain_slot_text` raises on ffmpeg failure instead of
    silently returning the textless input clip.

    Mirrors the existing pattern for `curtain_close_tail_failed`,
    `barn_door_open_head_failed`, `animated_overlay_failed`. A failed job
    with `failure_reason=ffmpeg_failed` is strictly better than a job that
    'succeeds' and ships a slot missing its hero overlay (the b6540038
    failure mode)."""

    def _make_step(self):
        step = MagicMock()
        step.clip_id = "clip_a"
        step.slot = {
            "position": 5,
            "target_duration_s": 5.5,
            "text_overlays": [
                {
                    "role": "label",
                    "start_s": 0.0,
                    "end_s": 5.5,
                    "position": "center",
                    "effect": "font-cycle",
                    "sample_text": "PERU",
                    "text_color": "#F4D03F",
                    "text_size_px": 170,
                    "font_cycle_accel_at_s": 2.8,
                }
            ],
        }
        return step

    def test_pre_burn_failure_raises(self, tmp_path):
        """Test 5: when ffmpeg returns non-zero, `_pre_burn_curtain_slot_text`
        raises RuntimeError carrying the slot index and rc. The previous
        behavior (return clip_path, log warning, continue) is the exact bug
        that let job b6540038 ship without MOROCCO."""
        from app.tasks.template_orchestrate import _pre_burn_curtain_slot_text

        clip_file = tmp_path / "slot.mp4"
        clip_file.write_bytes(b"fake mp4")

        # subprocess.run returns non-zero — what the OOM-killed ffmpeg
        # would have returned (rc=-9) or any other failure mode
        mock_result = MagicMock()
        mock_result.returncode = -9
        mock_result.stderr = b"some ffmpeg error"

        fake_pngs = [
            {"png_path": "/tmp/fake_overlay.png", "start_s": 0.0, "end_s": 3.0},
        ]

        inter = {"type": "curtain-close", "hold_s": 0.0, "animate_s": 1.6}

        with (
            patch(
                "app.tasks.template_orchestrate.subprocess.run",
                return_value=mock_result,
            ),
            patch(
                "app.pipeline.text_overlay.generate_text_overlay_png",
                return_value=fake_pngs,
            ),
        ):
            with pytest.raises(RuntimeError) as exc_info:
                _pre_burn_curtain_slot_text(
                    str(clip_file),
                    self._make_step(),
                    slot_dur=5.5,
                    clip_metas=None,
                    subject="Morocco",
                    slot_idx=4,
                    tmpdir=str(tmp_path),
                    inter=inter,
                )

        msg = str(exc_info.value)
        assert "slot 4" in msg, f"error should name the slot index, got: {msg}"
        assert "-9" in msg or "rc=" in msg, (
            f"error should carry the ffmpeg return code so failure_reason can be set, got: {msg}"
        )

    def test_pre_burn_success_returns_burned_path(self, tmp_path):
        """Test 6: on rc=0 the function returns the burned-path (the
        text-baked clip), NOT the original. This is the happy path that
        every existing curtain-close template should hit."""
        from app.tasks.template_orchestrate import _pre_burn_curtain_slot_text

        clip_file = tmp_path / "slot.mp4"
        clip_file.write_bytes(b"fake mp4")

        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stderr = b""

        fake_pngs = [
            {"png_path": "/tmp/fake_overlay.png", "start_s": 0.0, "end_s": 3.0},
        ]
        inter = {"type": "curtain-close", "hold_s": 0.0, "animate_s": 1.6}

        with (
            patch(
                "app.tasks.template_orchestrate.subprocess.run",
                return_value=mock_result,
            ),
            patch(
                "app.pipeline.text_overlay.generate_text_overlay_png",
                return_value=fake_pngs,
            ),
        ):
            result_path = _pre_burn_curtain_slot_text(
                str(clip_file),
                self._make_step(),
                slot_dur=5.5,
                clip_metas=None,
                subject="Morocco",
                slot_idx=4,
                tmpdir=str(tmp_path),
                inter=inter,
            )

        # Returns the new burned path under tmpdir, not the original clip
        assert result_path != str(clip_file)
        assert "textburned" in os.path.basename(result_path)
        assert os.path.dirname(result_path) == str(tmp_path)
