"""Tests for reframe_and_export(keep_segments=...) — plans/010 T4.

Three layers:

1. Command-construction pins (subprocess mocked, no ffmpeg needed):
   - IRON RULE: keep_segments=None produces a command byte-identical to the
     pre-change builder (expected lists are hardcoded arg-by-arg, including
     the full encoding tail — NOT rebuilt from production helpers).
   - keep_segments provided: per-segment trim/atrim + setpts/asetpts, 12ms
     afade declick at CUT-ADJACENT edges only, concat, silent-audio contract
     for has_audio=False sources, alternating punch-in on odd segments when
     keep_segments_punch_in is set.
   - fail-loud ValueError on malformed segment lists.
2. Micro-e2e [real ffmpeg]: lavfi-generated fixture (media files are NEVER
   committed to git), 3 keep_segments, ffprobe duration == sum(segments).
3. Drift stress e2e [real ffmpeg]: 60s fixture, 31 segments (30 cuts),
   terminal |video_duration - audio_duration| < 40ms (eng review 11A).

No uuid4()/nondeterminism in parametrize (xdist collection must agree).
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from app.config import settings
from app.pipeline.reframe import reframe_and_export

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

# First segment starts at the clip's true start (no fade-in); last segment
# ends at the clip's true end (no fade-out); the middle one is cut-adjacent
# on both flanks. duration = end_s - start_s = 12.0.
SEGMENTS = [(0.0, 4.0), (5.5, 8.5), (10.0, 12.0)]


class TestKeepSegmentsCommand:
    def test_full_command_with_audio(self) -> None:
        cmd = _capture_cmd(**_base_kwargs(keep_segments=SEGMENTS))
        expected_fc = (
            f"[0:v]{EXPECTED_VF}[base]"
            ";[base]split=3[vs0][vs1][vs2]"
            ";[vs0]trim=start=0.000000:end=4.000000,setpts=PTS-STARTPTS[v0]"
            ";[vs1]trim=start=5.500000:end=8.500000,setpts=PTS-STARTPTS[v1]"
            ";[vs2]trim=start=10.000000:end=12.000000,setpts=PTS-STARTPTS[v2]"
            ";[0:a]asplit=3[as0][as1][as2]"
            ";[as0]atrim=start=0.000000:end=4.000000,asetpts=PTS-STARTPTS"
            ",afade=t=out:st=3.988000:d=0.012[a0]"
            ";[as1]atrim=start=5.500000:end=8.500000,asetpts=PTS-STARTPTS"
            ",afade=t=in:st=0:d=0.012"
            ",afade=t=out:st=2.988000:d=0.012[a1]"
            ";[as2]atrim=start=10.000000:end=12.000000,asetpts=PTS-STARTPTS"
            ",afade=t=in:st=0:d=0.012[a2]"
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

    def test_afade_only_at_cut_adjacent_edges(self) -> None:
        cmd = _capture_cmd(**_base_kwargs(keep_segments=SEGMENTS))
        fc = cmd[cmd.index("-filter_complex") + 1]
        chains = {p.split("]", 1)[0].lstrip("["): p for p in fc.split(";")}
        # Segment 0 starts at the clip's true start: fade-out only.
        assert "afade=t=in" not in chains["as0"]
        assert "afade=t=out:st=3.988000:d=0.012" in chains["as0"]
        # Segment 1 is cut-adjacent on both flanks: both fades.
        assert "afade=t=in:st=0:d=0.012" in chains["as1"]
        assert "afade=t=out:st=2.988000:d=0.012" in chains["as1"]
        # Segment 2 ends at the clip's true end: fade-in only.
        assert "afade=t=in:st=0:d=0.012" in chains["as2"]
        assert "afade=t=out" not in chains["as2"]

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
            f",crop={settings.output_width}:{settings.output_height}[v1]"
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
        cmd = _capture_cmd(**_base_kwargs(has_audio=False, keep_segments=SEGMENTS))
        expected_fc = (
            f"[0:v]{EXPECTED_VF}[base]"
            ";[base]split=3[vs0][vs1][vs2]"
            ";[vs0]trim=start=0.000000:end=4.000000,setpts=PTS-STARTPTS[v0]"
            ";[vs1]trim=start=5.500000:end=8.500000,setpts=PTS-STARTPTS[v1]"
            ";[vs2]trim=start=10.000000:end=12.000000,setpts=PTS-STARTPTS[v2]"
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


def _make_fixture(out_path: Path, duration_s: int) -> Path:
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
        f"sine=frequency=440:duration={duration_s}",
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
