"""Tests for reframe_and_export(keep_segments=...) — plans/010 T4.

Four layers:

1. Command-construction pins (subprocess mocked, no ffmpeg needed):
   - IRON RULE: keep_segments=None produces a command byte-identical to the
     pre-change builder (expected lists are hardcoded arg-by-arg, including
     the full encoding tail — NOT rebuilt from production helpers).
   - keep_segments provided: per-segment frame-grid trim + setpts, audio cut
     on the same frame grid, a qsin acrossfade at every cut between kept
     segments (each segment's widened atrim split into its body and the
     crossfade windows beside it; one acrossfade per cut whose halves end
     and start the neighbouring pieces), 12ms afade declick only at a
     leading/trailing trim, per-segment concat, silent-audio contract for
     has_audio=False sources, alternating punch-in on odd segments when
     keep_segments_punch_in is set, filter graph within one argv string.
   - fail-loud ValueError on malformed segment lists.
2. Micro-e2e [real ffmpeg]: lavfi-generated fixture (media files are NEVER
   committed to git), 3 keep_segments, ffprobe duration == sum(segments).
3. Drift stress e2e [real ffmpeg]: 60s fixture, 31 segments (30 cuts),
   terminal |video_duration - audio_duration| < 40ms (eng review 11A).
4. Cut-audio e2e [real ffmpeg]: room tone keeps its level across off-grid
   cuts (no declick dip, no concat silence padding), every audio piece
   lines up with its own picture, and the edge graph shapes (capped
   handles, plain joins, leading/trailing trims) render whole and level.

No uuid4()/nondeterminism in parametrize (xdist collection must agree).
"""

from __future__ import annotations

import json
import math
import shutil
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import numpy as np
import pytest

from app.config import settings
from app.pipeline.reframe import reframe_and_export
from app.pipeline.silence_cut import MAX_REMOVALS

# ---------------------------------------------------------------------------
# Shared expected-command fragments (hardcoded pins, NOT derived from the
# production helpers — if reframe.py drifts, these tests must fail).
# ---------------------------------------------------------------------------

# _build_video_filter output for the representative call: bt709 SDR source,
# 9:16 aspect, no speed ramp / grading / windows / grid / captions.
EXPECTED_VF = (
    "colorspace=all=bt709:iall=bt709"
    f",framerate=fps={settings.output_fps}"
    f",scale={settings.output_width}:{settings.output_height}"
    ":force_original_aspect_ratio=increase"
    f",crop={settings.output_width}:{settings.output_height}"
)

EXPECTED_SILENT_AUDIO_INPUT = [
    "-f",
    "lavfi",
    "-i",
    "anullsrc=channel_layout=stereo:sample_rate=44100",
]


def _expected_encoding_tail(output_path: str) -> list[str]:
    """The full intermediate (ultrafast, crf=14) encoding tail, arg by arg."""
    maxrate = settings.output_video_bitrate
    # "16M" -> "32M" (bufsize = 2x maxrate) without calling _double_rate.
    bufsize = f"{int(maxrate.rstrip('M')) * 2}M"
    return [
        "-c:v",
        "libx264",
        "-profile:v",
        "high",
        "-preset",
        "ultrafast",
        "-crf",
        "14",
        "-pix_fmt",
        "yuv420p",
        "-bf",
        "0",
        "-x264-params",
        "scenecut=0:open_gop=0:bframes=0",
        "-color_primaries",
        "bt709",
        "-color_trc",
        "bt709",
        "-colorspace",
        "bt709",
        "-maxrate",
        maxrate,
        "-bufsize",
        bufsize,
        "-r",
        str(settings.output_fps),
        "-c:a",
        "aac",
        "-b:a",
        "192k",
        "-ar",
        "44100",
        "-ac",
        "2",
        "-s",
        f"{settings.output_width}x{settings.output_height}",
        "-movflags",
        "+faststart",
        "-y",
        output_path,
    ]


def _base_kwargs(**overrides) -> dict:
    kwargs = dict(
        input_path="/fake/in.mp4",
        start_s=0.0,
        end_s=12.0,
        aspect_ratio="9:16",
        ass_subtitle_path=None,
        output_path="/fake/out.mp4",
        color_trc="bt709",  # skip the probe_video fallback
        has_audio=True,
    )
    kwargs.update(overrides)
    return kwargs


def _capture_cmd(**kwargs) -> list[str]:
    """Run reframe_and_export with subprocess mocked; return the ffmpeg cmd."""
    with (
        patch("app.pipeline.reframe.subprocess.run") as mock_run,
        patch("app.pipeline.reframe.os.path.exists", return_value=True),
        patch("app.pipeline.reframe.os.path.getsize", return_value=1024),
    ):
        mock_run.return_value = MagicMock(returncode=0, stderr=b"")
        reframe_and_export(**kwargs)
        assert mock_run.call_count == 1
        return mock_run.call_args[0][0]


# ---------------------------------------------------------------------------
# IRON RULE — keep_segments=None commands are byte-identical to pre-change.
# ---------------------------------------------------------------------------


class TestNonePathByteIdentical:
    def test_default_call_with_audio(self) -> None:
        cmd = _capture_cmd(**_base_kwargs())
        assert cmd == [
            "ffmpeg",
            "-ss",
            "0.0",
            "-t",
            "12.0",
            "-i",
            "/fake/in.mp4",
            "-vf",
            EXPECTED_VF,
            *_expected_encoding_tail("/fake/out.mp4"),
        ]

    def test_explicit_none_matches_omitted(self) -> None:
        cmd_omitted = _capture_cmd(**_base_kwargs())
        cmd_none = _capture_cmd(**_base_kwargs(keep_segments=None))
        assert cmd_none == cmd_omitted

    def test_default_call_without_audio(self) -> None:
        cmd = _capture_cmd(**_base_kwargs(has_audio=False))
        assert cmd == [
            "ffmpeg",
            "-ss",
            "0.0",
            "-t",
            "12.0",
            "-i",
            "/fake/in.mp4",
            *EXPECTED_SILENT_AUDIO_INPUT,
            "-map",
            "0:v:0",
            "-map",
            "1:a:0",
            "-shortest",
            "-vf",
            EXPECTED_VF,
            *_expected_encoding_tail("/fake/out.mp4"),
        ]


# ---------------------------------------------------------------------------
# keep_segments command construction.
# ---------------------------------------------------------------------------

# First segment starts at the clip's true start; last segment ends at the
# clip's true end; the middle one sits between two cuts. duration =
# end_s - start_s = 12.0. Every boundary is on the 30 fps frame grid.
SEGMENTS = [(0.0, 4.0), (5.5, 8.5), (10.0, 12.0)]


def _filter_chains(cmd: list[str]) -> dict[str, str]:
    """Map each filter_complex chain's first input label to the chain."""
    fc = cmd[cmd.index("-filter_complex") + 1]
    return {p.split("]", 1)[0].lstrip("["): p for p in fc.split(";")}


class TestKeepSegmentsCommand:
    def test_full_command_with_audio(self) -> None:
        # The hardcoded frame-grid times below assume the 30 fps output.
        assert settings.output_fps == 30
        cmd = _capture_cmd(**_base_kwargs(keep_segments=SEGMENTS))
        expected_fc = (
            f"[0:v]{EXPECTED_VF}[base]"
            ";[base]split=3[vs0][vs1][vs2]"
            # Frames [0,120), [165,255), [300,360), trimmed a quarter frame
            # before each boundary frame.
            ";[vs0]trim=start=0.000000:end=3.991667,setpts=PTS-STARTPTS,setsar=1[v0]"
            ";[vs1]trim=start=5.491667:end=8.491667,setpts=PTS-STARTPTS,setsar=1[v1]"
            ";[vs2]trim=start=9.991667:end=11.991667,setpts=PTS-STARTPTS,setsar=1[v2]"
            # Each segment trims its span widened by 25 ms per cut, then
            # splits off its body and the crossfade windows beside it. No
            # declick anywhere (true start, two crossfaded cuts, true end).
            ";[0:a]aresample=first_pts=0:min_hard_comp=0.005,asplit=3[as0][as1][as2]"
            ";[as0]atrim=start=0.000000:end=4.025000,asetpts=PTS-STARTPTS,asplit=2[wb0][wo0]"
            ";[wb0]atrim=start=0.000000:end=3.975000,asetpts=PTS-STARTPTS[b0]"
            ";[wo0]atrim=start=3.975000,asetpts=PTS-STARTPTS"
            ",afade=t=out:st=0:d=0.050000:curve=qsin[xo1]"
            ";[as1]atrim=start=5.475000:end=8.525000,asetpts=PTS-STARTPTS"
            ",asplit=3[wb1][wi1][wo1]"
            ";[wb1]atrim=start=0.050000:end=3.000000,asetpts=PTS-STARTPTS[b1]"
            ";[wi1]atrim=end=0.050000,asetpts=PTS-STARTPTS"
            ",afade=t=in:st=0:d=0.050000:curve=qsin[xi1]"
            ";[wo1]atrim=start=3.000000,asetpts=PTS-STARTPTS"
            ",afade=t=out:st=0:d=0.050000:curve=qsin[xo2]"
            ";[as2]atrim=start=9.975000:end=12.000000,asetpts=PTS-STARTPTS,asplit=2[wb2][wi2]"
            ";[wb2]atrim=start=0.050000:end=2.025000,asetpts=PTS-STARTPTS[b2]"
            ";[wi2]atrim=end=0.050000,asetpts=PTS-STARTPTS"
            ",afade=t=in:st=0:d=0.050000:curve=qsin[xi2]"
            # Each cut sums its two faded sides on its own; the halves end
            # one piece and start the next.
            ";[xi1][xo1]amix=inputs=2:normalize=0,asetpts=N/SR/TB,asplit=2[xa1][xb1]"
            ";[xa1]atrim=end=0.025000,asetpts=PTS-STARTPTS[t0]"
            ";[xb1]atrim=start=0.025000,asetpts=PTS-STARTPTS[h1]"
            ";[xi2][xo2]amix=inputs=2:normalize=0,asetpts=N/SR/TB,asplit=2[xa2][xb2]"
            ";[xa2]atrim=end=0.025000,asetpts=PTS-STARTPTS[t1]"
            ";[xb2]atrim=start=0.025000,asetpts=PTS-STARTPTS[h2]"
            # Pieces of 4 s, 3 s and 2 s: exactly each segment's picture.
            ";[b0][t0]concat=n=2:v=0:a=1[a0]"
            ";[h1][b1][t1]concat=n=3:v=0:a=1[a1]"
            ";[h2][b2]concat=n=2:v=0:a=1[a2]"
            ";[v0][a0][v1][a1][v2][a2]concat=n=3:v=1:a=1[vout][aout]"
        )
        assert cmd == [
            "ffmpeg",
            "-ss",
            "0.0",
            "-t",
            "12.0",
            "-i",
            "/fake/in.mp4",
            "-filter_complex",
            expected_fc,
            "-map",
            "[vout]",
            "-map",
            "[aout]",
            *_expected_encoding_tail("/fake/out.mp4"),
        ]

    def test_declick_only_at_leading_and_trailing_trims(self) -> None:
        # A leading trim (first segment after 0) and a trailing trim (last
        # segment before the end) have no kept audio beyond them to crossfade
        # with: they keep the 12 ms declick, on the body itself so the
        # fade-out ends exactly at the piece end. The cut between crossfades.
        cmd = _capture_cmd(**_base_kwargs(keep_segments=[(0.5, 4.0), (5.5, 11.5)]))
        chains = _filter_chains(cmd)
        assert chains["wb0"] == (
            "[wb0]atrim=start=0.000000:end=3.475000,asetpts=PTS-STARTPTS"
            ",afade=t=in:st=0:d=0.012[b0]"
        )
        # Body 5.975 s (the 6 s piece minus its 25 ms crossfade head).
        assert chains["wb1"] == (
            "[wb1]atrim=start=0.050000:end=6.025000,asetpts=PTS-STARTPTS"
            ",afade=t=out:st=5.963000:d=0.012[b1]"
        )
        assert chains["xi1"] == (
            "[xi1][xo1]amix=inputs=2:normalize=0,asetpts=N/SR/TB,asplit=2[xa1][xb1]"
        )
        assert chains["h1"] == "[h1][b1]concat=n=2:v=0:a=1[a1]"

    def test_crossfade_handle_capped_by_half_the_removed_span(self) -> None:
        # (4.04, 12) starts on frame 121: one removed frame (1/30 s), so each
        # side may borrow only half of it (1/60 s) instead of 25 ms — the
        # borrowed audio never reaches past the removed span's midpoint.
        cmd = _capture_cmd(**_base_kwargs(keep_segments=[(0.0, 4.0), (4.04, 12.0)]))
        chains = _filter_chains(cmd)
        assert chains["as0"] == (
            "[as0]atrim=start=0.000000:end=4.016667,asetpts=PTS-STARTPTS,asplit=2[wb0][wo0]"
        )
        assert chains["as1"] == (
            "[as1]atrim=start=4.016667:end=12.000000,asetpts=PTS-STARTPTS,asplit=2[wb1][wi1]"
        )
        assert chains["wo0"] == (
            "[wo0]atrim=start=3.983333,asetpts=PTS-STARTPTS"
            ",afade=t=out:st=0:d=0.033333:curve=qsin[xo1]"
        )

    def test_crossfade_handle_capped_by_a_quarter_of_the_segment(self) -> None:
        # (5.0, 5.06) holds two frames (1/15 s): both of its cuts may borrow
        # only a quarter of it (1/60 s), so the two crossfades never overlap.
        cmd = _capture_cmd(**_base_kwargs(keep_segments=[(0.0, 4.0), (5.0, 5.06), (7.0, 12.0)]))
        chains = _filter_chains(cmd)
        assert chains["as1"] == (
            "[as1]atrim=start=4.983333:end=5.083333,asetpts=PTS-STARTPTS,asplit=3[wb1][wi1][wo1]"
        )
        assert chains["wb1"] == "[wb1]atrim=start=0.033333:end=0.066667,asetpts=PTS-STARTPTS[b1]"
        assert chains["wi1"].endswith(",afade=t=in:st=0:d=0.033333:curve=qsin[xi1]")
        assert chains["wo1"] == (
            "[wo1]atrim=start=0.066667,asetpts=PTS-STARTPTS"
            ",afade=t=out:st=0:d=0.033333:curve=qsin[xo2]"
        )
        assert chains["h1"] == "[h1][b1][t1]concat=n=3:v=0:a=1[a1]"

    def test_off_grid_boundaries_cut_audio_on_the_video_frames(self) -> None:
        # 2.013 s snaps to frame 60 and 2.531 s to frame 76, so the first
        # picture is frames [0, 60) = 2.0 s and its audio piece matches that
        # exactly. Cutting audio at the raw 2.013 against a 61-frame picture
        # left concat to pad 20 ms of digital silence.
        cmd = _capture_cmd(**_base_kwargs(keep_segments=[(0.0, 2.013), (2.531, 12.0)]))
        chains = _filter_chains(cmd)
        assert "trim=start=0.000000:end=1.991667," in chains["vs0"]
        assert "trim=start=2.525000:end=11.991667," in chains["vs1"]
        assert chains["as0"] == (
            "[as0]atrim=start=0.000000:end=2.025000,asetpts=PTS-STARTPTS,asplit=2[wb0][wo0]"
        )
        assert chains["as1"] == (
            "[as1]atrim=start=2.508333:end=12.000000,asetpts=PTS-STARTPTS,asplit=2[wb1][wi1]"
        )
        # 1.975 s of body + the 25 ms crossfade tail = 2.0 s.
        assert chains["wb0"] == "[wb0]atrim=start=0.000000:end=1.975000,asetpts=PTS-STARTPTS[b0]"

    def test_boundaries_snap_to_the_nearest_frame(self) -> None:
        # 1.501 s snaps down to frame 45 (1 ms early) and 2.49 s up to frame
        # 75 (10 ms late): every audio cut stays within half a frame of the
        # plan. Always snapping up let up to a frame of removed audio (a
        # discarded retake's onset) play at full level before the fade.
        cmd = _capture_cmd(**_base_kwargs(end_s=6.0, keep_segments=[(0.0, 1.501), (2.49, 6.0)]))
        chains = _filter_chains(cmd)
        assert "trim=start=0.000000:end=1.491667," in chains["vs0"]
        assert "trim=start=2.491667:end=5.991667," in chains["vs1"]
        assert chains["as0"].startswith("[as0]atrim=start=0.000000:end=1.525000,")
        assert chains["as1"].startswith("[as1]atrim=start=2.475000:end=6.000000,")

    def test_segment_starting_in_the_first_frame_plays_from_zero_unfaded(self) -> None:
        # 0.01 s snaps to frame 0: the audio starts at the true clip start,
        # which gets no declick.
        cmd = _capture_cmd(**_base_kwargs(keep_segments=[(0.01, 4.0), (5.5, 12.0)]))
        chains = _filter_chains(cmd)
        assert chains["as0"].startswith("[as0]atrim=start=0.000000:end=4.025000,")
        assert "afade=t=in" not in chains["wb0"]

    def test_segment_ending_in_the_last_frame_plays_to_the_end_unfaded(self) -> None:
        # 11.99 s snaps to the clip end (frame 360 = 12.0 s): the audio runs
        # to the true end, which gets no declick.
        cmd = _capture_cmd(**_base_kwargs(keep_segments=[(0.0, 4.0), (5.5, 11.99)]))
        chains = _filter_chains(cmd)
        assert chains["as1"].startswith("[as1]atrim=start=5.475000:end=12.000000,")
        assert "afade" not in chains["wb1"]

    def test_last_segment_audio_stops_at_an_off_grid_clip_end(self) -> None:
        # The clip ends at 12.013 s, inside frame 360: the last picture frame
        # runs past the clip end, its audio does not.
        cmd = _capture_cmd(**_base_kwargs(end_s=12.013, keep_segments=[(0.0, 4.0), (5.5, 12.013)]))
        chains = _filter_chains(cmd)
        assert chains["as1"] == (
            "[as1]atrim=start=5.475000:end=12.013000,asetpts=PTS-STARTPTS,asplit=2[wb1][wi1]"
        )
        assert "afade" not in chains["wb1"]

    def test_contiguous_frames_join_without_fade(self) -> None:
        # (0, 4.01) and (4.012, 12) both snap to frame 120: no frame is
        # removed, so the audio is continuous there and joins plainly.
        cmd = _capture_cmd(**_base_kwargs(keep_segments=[(0.0, 4.01), (4.012, 12.0)]))
        chains = _filter_chains(cmd)
        assert chains["as0"] == "[as0]atrim=start=0.000000:end=4.000000,asetpts=PTS-STARTPTS[a0]"
        assert chains["as1"] == "[as1]atrim=start=4.000000:end=12.000000,asetpts=PTS-STARTPTS[a1]"
        fc = cmd[cmd.index("-filter_complex") + 1]
        assert "amix" not in fc
        assert "afade" not in fc

    def test_single_segment_needs_no_join(self) -> None:
        cmd = _capture_cmd(**_base_kwargs(keep_segments=[(1.0, 11.0)]))
        chains = _filter_chains(cmd)
        assert chains["as0"] == (
            "[as0]atrim=start=1.000000:end=11.000000,asetpts=PTS-STARTPTS"
            ",afade=t=in:st=0:d=0.012,afade=t=out:st=9.988000:d=0.012[a0]"
        )
        assert chains["v0"] == "[v0][a0]concat=n=1:v=1:a=1[vout][aout]"

    def test_sub_frame_segment_is_dropped(self) -> None:
        # (5.02, 5.04) snaps to no frame (both ends snap to frame 151).
        cmd = _capture_cmd(**_base_kwargs(keep_segments=[(0.0, 4.0), (5.02, 5.04), (6.0, 12.0)]))
        chains = _filter_chains(cmd)
        assert chains["base"] == "[base]split=2[vs0][vs1]"
        assert chains["v0"] == "[v0][a0][v1][a1]concat=n=2:v=1:a=1[vout][aout]"

    def test_filter_graph_fits_one_argv_string_at_max_removals(self) -> None:
        # The whole graph is one argv string; Linux caps each at 128 KiB
        # (MAX_ARG_STRLEN). MAX_REMOVALS cuts is the most a plan can hold.
        segments = []
        t = 0.123
        for _ in range(MAX_REMOVALS + 1):
            segments.append((round(t, 3), round(t + 1.9, 3)))
            t += 2.9
        cmd = _capture_cmd(**_base_kwargs(end_s=t + 1.0, keep_segments=segments))
        fc = cmd[cmd.index("-filter_complex") + 1]
        assert fc.count("amix=") == MAX_REMOVALS
        assert len(fc.encode()) < 100_000

    def test_punch_in_alternates_odd_segments_and_restores_dims(self) -> None:
        cmd = _capture_cmd(**_base_kwargs(keep_segments=SEGMENTS, keep_segments_punch_in=1.08))
        fc = cmd[cmd.index("-filter_complex") + 1]
        chains = {p.split("]", 1)[0].lstrip("["): p for p in fc.split(";")}
        # Even segments keep plain trim chains — no punch.
        assert "scale=" not in chains["vs0"]
        assert "scale=" not in chains["vs2"]
        # Odd segment: scale up 1.08 (even-rounded 1166x2074) then crop back
        # to the chain's exact output dims so concat sees identical geometry.
        expected_w = int(round(settings.output_width * 1.08 / 2)) * 2
        expected_h = int(round(settings.output_height * 1.08 / 2)) * 2
        assert (
            f",scale={expected_w}:{expected_h}"
            f",crop={settings.output_width}:{settings.output_height},setsar=1[v1]"
        ) in chains["vs1"]
        assert all(",setsar=1[v" in chains[f"vs{i}"] for i in range(3))

    def test_punch_in_16x9_uses_portrait_output_dims(self) -> None:
        # T4s geometry pin (review 2026-07-11): aspect_ratio describes the
        # INPUT — the 16:9 vf chain still outputs portrait W:H (safe
        # fill+crop, since 2026-08-19 the same generic form as every other
        # crop-mode source), so punch dims must be the SAME portrait pair as
        # the 9:16 case. The earlier swapped-dims branch emitted landscape
        # odd segments against portrait even ones → concat abort on
        # landscape sources; this pins the corrected geometry.
        cmd = _capture_cmd(
            **_base_kwargs(
                aspect_ratio="16:9",
                keep_segments=SEGMENTS,
                keep_segments_punch_in=1.08,
            )
        )
        fc = cmd[cmd.index("-filter_complex") + 1]
        chains = {p.split("]", 1)[0].lstrip("["): p for p in fc.split(";")}
        expected_vf_16x9 = (
            "colorspace=all=bt709:iall=bt709"
            f",framerate=fps={settings.output_fps}"
            f",scale={settings.output_width}:{settings.output_height}"
            ":force_original_aspect_ratio=increase"
            f",crop={settings.output_width}:{settings.output_height}"
        )
        assert chains["0:v"] == f"[0:v]{expected_vf_16x9}[base]"
        # Even segments keep plain trim chains — no punch.
        assert "scale=" not in chains["vs0"]
        assert "scale=" not in chains["vs2"]
        # Odd segment: portrait punch dims — identical to the 9:16 case, and
        # crop returns to the vf chain's actual output geometry.
        expected_w = int(round(settings.output_width * 1.08 / 2)) * 2
        expected_h = int(round(settings.output_height * 1.08 / 2)) * 2
        assert (
            f",scale={expected_w}:{expected_h}"
            f",crop={settings.output_width}:{settings.output_height},setsar=1[v1]"
        ) in chains["vs1"]

    def test_punch_in_default_none_leaves_segment_chains_plain(self) -> None:
        cmd = _capture_cmd(**_base_kwargs(keep_segments=SEGMENTS))
        fc = cmd[cmd.index("-filter_complex") + 1]
        # No scale/crop anywhere after [base] — segments are pure trims.
        after_base = fc.split("[base];", 1)[1]
        assert ",scale=" not in after_base
        assert ",crop=" not in after_base

    def test_cut_sits_after_fps_normalization(self) -> None:
        # Eng review 1A/13A: the trim stage must run on the CFR-normalized
        # stream — the framerate filter appears in [0:v]...[base] BEFORE any
        # trim in the graph.
        cmd = _capture_cmd(**_base_kwargs(keep_segments=SEGMENTS))
        fc = cmd[cmd.index("-filter_complex") + 1]
        assert fc.index(f"framerate=fps={settings.output_fps}") < fc.index("trim=start=")

    def test_full_command_without_audio(self) -> None:
        assert settings.output_fps == 30
        cmd = _capture_cmd(**_base_kwargs(has_audio=False, keep_segments=SEGMENTS))
        expected_fc = (
            f"[0:v]{EXPECTED_VF}[base]"
            ";[base]split=3[vs0][vs1][vs2]"
            ";[vs0]trim=start=0.000000:end=3.991667,setpts=PTS-STARTPTS,setsar=1[v0]"
            ";[vs1]trim=start=5.491667:end=8.491667,setpts=PTS-STARTPTS,setsar=1[v1]"
            ";[vs2]trim=start=9.991667:end=11.991667,setpts=PTS-STARTPTS,setsar=1[v2]"
            ";[v0][v1][v2]concat=n=3:v=1:a=0[vout]"
        )
        assert cmd == [
            "ffmpeg",
            "-ss",
            "0.0",
            "-t",
            "12.0",
            "-i",
            "/fake/in.mp4",
            *EXPECTED_SILENT_AUDIO_INPUT,
            "-filter_complex",
            expected_fc,
            "-map",
            "[vout]",
            "-map",
            "1:a:0",
            "-shortest",
            *_expected_encoding_tail("/fake/out.mp4"),
        ]
        assert "atrim" not in expected_fc and "afade" not in expected_fc


# ---------------------------------------------------------------------------
# Fail-loud validation (callers own the uncut fallback).
# ---------------------------------------------------------------------------


class TestKeepSegmentsValidation:
    def _assert_raises(self, match: str, **overrides) -> None:
        with (
            patch("app.pipeline.reframe.subprocess.run") as mock_run,
            pytest.raises(ValueError, match=match),
        ):
            reframe_and_export(**_base_kwargs(**overrides))
        assert mock_run.call_count == 0  # rejected before any ffmpeg spawn

    def test_empty_list(self) -> None:
        self._assert_raises("at least one segment", keep_segments=[])

    def test_zero_length_segment(self) -> None:
        self._assert_raises("non-positive length", keep_segments=[(2.0, 2.0)])

    def test_inverted_segment(self) -> None:
        self._assert_raises("non-positive length", keep_segments=[(3.0, 1.0)])

    def test_negative_start(self) -> None:
        self._assert_raises("out of bounds", keep_segments=[(-0.5, 2.0)])

    def test_end_past_duration(self) -> None:
        self._assert_raises("out of bounds", keep_segments=[(0.0, 12.5)])

    def test_unsorted(self) -> None:
        self._assert_raises(
            "sorted and non-overlapping",
            keep_segments=[(5.0, 6.0), (1.0, 2.0)],
        )

    def test_overlapping(self) -> None:
        self._assert_raises(
            "sorted and non-overlapping",
            keep_segments=[(0.0, 5.0), (4.0, 6.0)],
        )

    def test_no_segment_holds_a_frame(self) -> None:
        # Both ends snap to frame 31: nothing would render.
        self._assert_raises("hold no video frame", keep_segments=[(1.02, 1.04)])

    def test_combined_with_png_overlays(self) -> None:
        self._assert_raises(
            "cannot be combined",
            keep_segments=SEGMENTS,
            text_overlay_pngs=[{"png_path": "/fake/ov.png", "start_s": 0.0, "end_s": 1.0}],
        )

    def test_combined_with_ass_overlays(self) -> None:
        self._assert_raises(
            "cannot be combined",
            keep_segments=SEGMENTS,
            ass_overlay_paths=["/fake/ov.ass"],
        )


# ---------------------------------------------------------------------------
# E2E — real ffmpeg on lavfi-generated fixtures. Skipped when ffmpeg is not
# on PATH (CI installs it). Media files are NEVER committed to git.
# ---------------------------------------------------------------------------


def _ffmpeg_available() -> bool:
    return shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None


needs_ffmpeg = pytest.mark.skipif(
    not _ffmpeg_available(),
    reason="ffmpeg/ffprobe not installed",
)


def _make_fixture(
    out_path: Path,
    duration_s: int,
    audio: str = "sine=frequency=440",
) -> Path:
    """Generate a tiny H.264+AAC talk-clip stand-in at test time."""
    cmd = [
        "ffmpeg",
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
        "-f",
        "lavfi",
        "-i",
        f"testsrc2=duration={duration_s}:size=320x568:rate=30",
        "-f",
        "lavfi",
        "-i",
        f"{audio}:duration={duration_s}",
        "-c:v",
        "libx264",
        "-preset",
        "ultrafast",
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        "-shortest",
        str(out_path),
    ]
    subprocess.run(cmd, check=True, capture_output=True, timeout=120)
    return out_path


def _probe(path: Path) -> dict:
    result = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-print_format",
            "json",
            "-show_streams",
            "-show_format",
            str(path),
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    return json.loads(result.stdout)


def _small_output(monkeypatch: pytest.MonkeyPatch) -> None:
    """Render at fixture size instead of 1080x1920 to keep e2e runtime sane."""
    monkeypatch.setattr(settings, "output_width", 320)
    monkeypatch.setattr(settings, "output_height", 568)


@needs_ffmpeg
def test_micro_e2e_three_segments(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _small_output(monkeypatch)
    fixture = _make_fixture(tmp_path / "src.mp4", 12)
    out = tmp_path / "cut.mp4"
    segments = [(0.0, 4.0), (5.5, 8.5), (10.0, 12.0)]

    reframe_and_export(
        input_path=str(fixture),
        start_s=0.0,
        end_s=12.0,
        aspect_ratio="9:16",
        ass_subtitle_path=None,
        output_path=str(out),
        keep_segments=segments,
    )

    data = _probe(out)
    assert {s["codec_type"] for s in data["streams"]} == {"video", "audio"}
    expected = sum(b - a for a, b in segments)  # 9.0s
    assert abs(float(data["format"]["duration"]) - expected) <= 0.05


@needs_ffmpeg
def test_micro_e2e_punch_segments_keep_square_pixels(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Prod parity: rounded punch dimensions must not make concat reject SAR."""
    _small_output(monkeypatch)
    fixture = _make_fixture(tmp_path / "src-punch.mp4", 12)
    out = tmp_path / "cut-punch.mp4"
    segments = [(0.0, 4.0), (5.5, 8.5), (10.0, 12.0)]

    reframe_and_export(
        input_path=str(fixture),
        start_s=0.0,
        end_s=12.0,
        aspect_ratio="9:16",
        ass_subtitle_path=None,
        output_path=str(out),
        keep_segments=segments,
        keep_segments_punch_in=1.08,
    )

    data = _probe(out)
    video = next(stream for stream in data["streams"] if stream["codec_type"] == "video")
    assert video["sample_aspect_ratio"] == "1:1"
    expected = sum(b - a for a, b in segments)
    assert abs(float(data["format"]["duration"]) - expected) <= 0.05


@needs_ffmpeg
def test_drift_stress_30_cuts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Eng review 11A: cumulative A/V drift bound across ~30 cuts.

    Per-segment concat re-syncs A/V at every joint, so the terminal offset
    must stay below one video frame regardless of cut count. Segment
    boundaries are deliberately NOT frame-aligned to exercise rounding.
    """
    _small_output(monkeypatch)
    fixture = _make_fixture(tmp_path / "src60.mp4", 60)
    out = tmp_path / "cut60.mp4"

    segments: list[tuple[float, float]] = []
    t = 0.123
    for i in range(31):
        seg_len = 0.777 + (i % 5) * 0.111
        segments.append((round(t, 3), round(t + seg_len, 3)))
        t += seg_len + 0.9
    assert segments[-1][1] < 60.0

    reframe_and_export(
        input_path=str(fixture),
        start_s=0.0,
        end_s=60.0,
        aspect_ratio="9:16",
        ass_subtitle_path=None,
        output_path=str(out),
        keep_segments=segments,
    )

    data = _probe(out)
    durations = {s["codec_type"]: float(s["duration"]) for s in data["streams"] if "duration" in s}
    assert {"video", "audio"} <= set(durations)
    assert abs(durations["video"] - durations["audio"]) < 0.040


def _decode_mono(path: Path, sample_rate: int) -> np.ndarray:
    pcm = subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-i",
            str(path),
            "-f",
            "f32le",
            "-ac",
            "1",
            "-ar",
            str(sample_rate),
            "-",
        ],
        check=True,
        capture_output=True,
        timeout=60,
    ).stdout
    return np.frombuffer(pcm, dtype=np.float32).astype(np.float64)


def _level_db(audio: np.ndarray, sample_rate: int) -> tuple[np.ndarray, float]:
    """5 ms RMS level in dB at 1 ms hops, and its median."""
    window = sample_rate // 200  # 5 ms
    hop = sample_rate // 1000  # 1 ms
    energy = np.concatenate([[0.0], np.cumsum(audio**2)])
    starts = np.arange(0, len(audio) - window, hop)
    level_db = 10 * np.log10(np.maximum((energy[starts + window] - energy[starts]) / window, 1e-18))
    median_db = float(np.median(level_db))
    return level_db, median_db


@needs_ffmpeg
def test_steady_noise_keeps_its_level_across_cuts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Room tone (rain, traffic) must run through every cut at its own level.

    Fading each side of a cut to silence dipped this steady-noise fixture 17-22 dB
    for ~25 ms, and cut boundaries off the frame grid let concat pad up to a
    frame of digital silence; both read as jumpy cuts on noisy takes. The
    boundaries here are deliberately off the 30 fps grid.
    """
    _small_output(monkeypatch)
    fixture = _make_fixture(
        tmp_path / "noise.mp4",
        20,
        audio="anoisesrc=color=pink:amplitude=0.05:seed=7:sample_rate=48000",
    )
    out = tmp_path / "noise_cut.mp4"
    segments = [
        (0.0, 2.013),
        (2.531, 4.007),
        (5.123, 7.488),
        (8.051, 10.017),
        (11.209, 14.111),
        (15.07, 20.0),
    ]

    reframe_and_export(
        input_path=str(fixture),
        start_s=0.0,
        end_s=20.0,
        aspect_ratio="9:16",
        ass_subtitle_path=None,
        output_path=str(out),
        keep_segments=segments,
    )

    sample_rate = 48000
    audio = _decode_mono(out, sample_rate)
    level_db, median_db = _level_db(audio, sample_rate)
    # Skip 50 ms at each end: AAC priming and the clip's own edges.
    inner = level_db[50:-50]
    quietest = int(np.argmin(inner))
    assert inner[quietest] > median_db - 8.0, (
        f"5 ms level fell to {inner[quietest]:.1f} dB at {(quietest + 50) / 1000:.3f}s "
        f"against a {median_db:.1f} dB median"
    )

    data = _probe(out)
    durations = {s["codec_type"]: float(s["duration"]) for s in data["streams"] if "duration" in s}
    # The picture is 469 whole frames (each boundary on its nearest frame),
    # and the audio matches it instead of carrying concat padding.
    assert abs(durations["video"] - 469 / 30) < 1e-3
    assert abs(durations["audio"] - durations["video"]) < 0.025


def _frames(segments: list[tuple[float, float]], end_s: float) -> list[tuple[int, int]]:
    """The 30 fps frame range each segment's picture renders (nearest frame;
    a segment reaching the clip end keeps every frame up to it)."""
    return [
        (
            math.floor(a * 30 + 0.5),
            math.ceil(b * 30 - 1e-6) if b >= end_s else math.floor(b * 30 + 0.5),
        )
        for a, b in segments
    ]


@needs_ffmpeg
def test_each_audio_piece_stays_with_its_segment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every audio piece plays exactly the source span its frames show.

    Total length and steady level would both survive a piece that slid by a
    handle against its picture; cross-correlating each piece's middle with
    the source catches it.
    """
    _small_output(monkeypatch)
    fixture = _make_fixture(
        tmp_path / "white.mp4",
        20,
        audio="anoisesrc=color=white:amplitude=0.2:seed=3:sample_rate=48000",
    )
    out = tmp_path / "white_cut.mp4"
    segments = [
        (0.0, 2.013),
        (2.531, 4.007),
        (5.123, 7.488),
        (8.051, 10.017),
        (11.209, 14.111),
        (15.07, 20.0),
    ]
    reframe_and_export(
        input_path=str(fixture),
        start_s=0.0,
        end_s=20.0,
        aspect_ratio="9:16",
        ass_subtitle_path=None,
        output_path=str(out),
        keep_segments=segments,
    )

    sample_rate = 44100
    source = _decode_mono(fixture, sample_rate)
    cut = _decode_mono(out, sample_rate)
    window = int(0.2 * sample_rate)
    joint_frames = 0
    for first, end in _frames(segments, 20.0):
        out_at = round((joint_frames / 30 + 0.3) * sample_rate)
        src_at = round((first / 30 + 0.3) * sample_rate)
        piece = cut[out_at : out_at + window]
        lag = max(
            range(-200, 201),
            key=lambda k: float(np.dot(piece, source[src_at + k : src_at + k + window])),
        )
        assert abs(lag) <= 2, f"piece at frame {first} is {lag} samples off its picture"
        joint_frames += end - first


@needs_ffmpeg
@pytest.mark.parametrize(
    "segments",
    [
        # Capped by a quarter of a two-frame segment, on both of its cuts.
        [(0.0, 4.0), (5.0, 5.06), (7.0, 12.0)],
        # Capped by half of a one-frame removed span.
        [(0.0, 4.0), (4.04, 12.0)],
        # A contiguous (plain) join next to a crossfaded cut.
        [(0.0, 4.01), (4.012, 6.0), (7.0, 12.0)],
        # Leading and trailing trims around a crossfaded cut.
        [(0.5, 4.0), (5.5, 11.5)],
    ],
    ids=["quarter-cap", "half-cap", "contiguous-and-cut", "leading-trailing-trims"],
)
def test_edge_graphs_render_whole_and_level(
    segments: list[tuple[float, float]], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Short crossfade inputs, plain joins and trims all render without a dip.

    A rejected graph fails open into an uncut render upstream, and FFmpeg
    builds differ between dev, CI and prod, so the edge shapes run for real.
    """
    _small_output(monkeypatch)
    fixture = _make_fixture(
        tmp_path / "noise.mp4",
        12,
        audio="anoisesrc=color=pink:amplitude=0.05:seed=11:sample_rate=48000",
    )
    out = tmp_path / "cut.mp4"
    reframe_and_export(
        input_path=str(fixture),
        start_s=0.0,
        end_s=12.0,
        aspect_ratio="9:16",
        ass_subtitle_path=None,
        output_path=str(out),
        keep_segments=segments,
    )

    data = _probe(out)
    durations = {s["codec_type"]: float(s["duration"]) for s in data["streams"] if "duration" in s}
    picture_s = sum(end - first for first, end in _frames(segments, 12.0)) / 30
    assert abs(durations["video"] - picture_s) < 1e-3
    assert abs(durations["audio"] - picture_s) < 0.01

    sample_rate = 48000
    audio = _decode_mono(out, sample_rate)
    level_db, median_db = _level_db(audio, sample_rate)
    # From 50 ms in to 17 ms before the picture ends: past any leading
    # declick, short of a trailing one, and covering the stretch a misplaced
    # trailing fade used to silence.
    inner = level_db[50 : int((picture_s - 0.022) * 1000)]
    quietest = int(np.argmin(inner))
    assert inner[quietest] > median_db - 8.0, (
        f"5 ms level fell to {inner[quietest]:.1f} dB at {(quietest + 50) / 1000:.3f}s "
        f"against a {median_db:.1f} dB median"
    )


@needs_ffmpeg
def test_audio_starting_after_the_video_stays_aligned(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A source whose audio starts after its video keeps every piece aligned.

    Each segment's widened trim is rebased to its own first sample; without
    rebasing the source audio to t=0 first, a late first sample shifted
    segment 0 and misplaced the first cut's crossfade by that offset.
    """
    _small_output(monkeypatch)
    fixture = tmp_path / "late.mkv"
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=duration=12:size=320x568:rate=30",
            "-itsoffset",
            "0.02",
            "-f",
            "lavfi",
            "-i",
            "anoisesrc=color=white:amplitude=0.2:seed=5:sample_rate=48000:duration=12",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            str(fixture),
        ],
        check=True,
        capture_output=True,
        timeout=120,
    )
    audio_start = next(
        float(s["start_time"]) for s in _probe(fixture)["streams"] if s["codec_type"] == "audio"
    )
    assert audio_start > 0.01  # the fixture really starts its audio late
    out = tmp_path / "late_cut.mp4"
    segments = [(0.0, 4.0), (5.5, 8.5), (10.0, 12.0)]
    reframe_and_export(
        input_path=str(fixture),
        start_s=0.0,
        end_s=12.0,
        aspect_ratio="9:16",
        ass_subtitle_path=None,
        output_path=str(out),
        keep_segments=segments,
    )

    sample_rate = 48000
    # The source decode starts at its first audio sample, audio_start late.
    source = _decode_mono(fixture, sample_rate)
    cut = _decode_mono(out, sample_rate)
    window = int(0.2 * sample_rate)
    joint_frames = 0
    for first, end in _frames(segments, 12.0):
        for offset_s in (0.3, (end - first) / 30 - 0.3):  # near each end
            out_at = round((joint_frames / 30 + offset_s - 0.1) * sample_rate)
            src_at = round((first / 30 + offset_s - 0.1 - audio_start) * sample_rate)
            piece = cut[out_at : out_at + window]
            lag = max(
                range(-3000, 3001),
                key=lambda k: float(np.dot(piece, source[src_at + k : src_at + k + window])),
            )
            # Within 1 ms: Matroska stamps whole milliseconds. Unrebased, the
            # first segment slid by the whole ~40 ms audio start.
            assert abs(lag) <= 48, f"segment at frame {first} is {lag} samples off its picture"
        joint_frames += end - first


@needs_ffmpeg
def test_audio_timestamp_gap_keeps_later_segments_aligned(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A 50 ms hole in the audio timestamps stays a hole, not a shift.

    Rebasing the audio to t=0 switches on gap compensation, whose 0.1 s
    default closes smaller gaps; every later segment then played its audio
    50 ms away from its picture.
    """
    _small_output(monkeypatch)
    fixture = tmp_path / "gap.mp4"
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=duration=12:size=320x568:rate=30",
            "-f",
            "lavfi",
            "-i",
            "anoisesrc=color=white:amplitude=0.2:seed=13:sample_rate=48000:duration=12",
            "-af",
            "asetpts='if(gte(T,3),PTS+0.05/TB,PTS)'",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-shortest",
            str(fixture),
        ],
        check=True,
        capture_output=True,
        timeout=120,
    )
    out = tmp_path / "gap_cut.mp4"
    segments = [(0.0, 2.013), (2.531, 4.007), (5.123, 7.488), (8.051, 11.0)]
    reframe_and_export(
        input_path=str(fixture),
        start_s=0.0,
        end_s=12.0,
        aspect_ratio="9:16",
        ass_subtitle_path=None,
        output_path=str(out),
        keep_segments=segments,
    )

    sample_rate = 48000
    # Reference timeline with the gap kept as silence.
    reference = np.frombuffer(
        subprocess.run(
            [
                "ffmpeg",
                "-v",
                "error",
                "-i",
                str(fixture),
                "-af",
                "aresample=async=1:min_hard_comp=0.001:first_pts=0",
                "-f",
                "f32le",
                "-ac",
                "1",
                "-ar",
                str(sample_rate),
                "-",
            ],
            check=True,
            capture_output=True,
            timeout=60,
        ).stdout,
        dtype=np.float32,
    ).astype(np.float64)
    cut = _decode_mono(out, sample_rate)
    window = int(0.05 * sample_rate)
    joint_frames = 0
    for first, end in _frames(segments, 12.0):
        out_at = round((joint_frames / 30 + 0.3) * sample_rate)
        ref_at = round((first / 30 + 0.3) * sample_rate)
        piece = cut[out_at : out_at + window]
        lag = max(
            range(-3000, 3001),
            key=lambda k: float(np.dot(piece, reference[ref_at + k : ref_at + k + window])),
        )
        assert abs(lag) <= 2, f"segment at frame {first} is {lag} samples off its picture"
        joint_frames += end - first
