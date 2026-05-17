"""Unit + integration tests for pipeline/orientation.py.

The integration tests generate a tiny rotated video fixture at runtime
because the repo .gitignore bans committing video files. Cost is ~50ms
per fixture build (libx264 ultrafast on a 1s 320x240 testsrc), amortized
once per test session via `tmp_path_factory`.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from app.pipeline.orientation import (
    OrientationError,
    _extract_rotation,
    _transpose_filter_for,
    detect_rotation,
    normalize_orientation,
)

# ---------------------------------------------------------------------------
# Pure helpers (no subprocess) — fast, no fixtures needed.
# ---------------------------------------------------------------------------


class TestExtractRotation:
    def test_no_side_data(self) -> None:
        assert _extract_rotation({"codec_type": "video"}) == 0

    def test_empty_side_data_list(self) -> None:
        assert _extract_rotation({"side_data_list": []}) == 0

    def test_rotation_neg_90_iphone_case(self) -> None:
        stream = {
            "side_data_list": [
                {"side_data_type": "Display Matrix", "rotation": -90},
            ],
        }
        assert _extract_rotation(stream) == -90

    def test_rotation_pos_90_android_case(self) -> None:
        stream = {
            "side_data_list": [
                {"side_data_type": "Display Matrix", "rotation": 90},
            ],
        }
        assert _extract_rotation(stream) == 90

    def test_rotation_180(self) -> None:
        stream = {
            "side_data_list": [
                {"side_data_type": "Display Matrix", "rotation": 180},
            ],
        }
        assert _extract_rotation(stream) == 180

    def test_non_orthogonal_returns_zero(self) -> None:
        # 45° is impossible from phone cameras — log + drop to 0 so
        # the file falls through to reframe's normal handling.
        stream = {
            "side_data_list": [
                {"side_data_type": "Display Matrix", "rotation": 45},
            ],
        }
        assert _extract_rotation(stream) == 0

    def test_ignores_non_display_matrix_entries(self) -> None:
        # iPhone files often carry "DOVI configuration record" and
        # "Ambient viewing environment" side_data; we must skip past them.
        stream = {
            "side_data_list": [
                {"side_data_type": "DOVI configuration record"},
                {"side_data_type": "Display Matrix", "rotation": -90},
                {"side_data_type": "Ambient viewing environment"},
            ],
        }
        assert _extract_rotation(stream) == -90

    def test_missing_rotation_key(self) -> None:
        stream = {
            "side_data_list": [
                {"side_data_type": "Display Matrix"},
            ],
        }
        assert _extract_rotation(stream) == 0

    def test_string_rotation_value(self) -> None:
        # Defensive — ffprobe may surface stringified ints in some
        # versions. int() coerces, then membership check.
        stream = {
            "side_data_list": [
                {"side_data_type": "Display Matrix", "rotation": "-90"},
            ],
        }
        assert _extract_rotation(stream) == -90


class TestTransposeFilterFor:
    def test_neg_90_maps_to_transpose_1_cw(self) -> None:
        """iPhone portrait (rotation=-90) needs CW rotation in pixels.
        Verified empirically against a real HEVC iPhone clip: transpose=2
        produced upside-down output; transpose=1 produces upright."""
        assert _transpose_filter_for(-90) == "transpose=1"

    def test_pos_90_maps_to_transpose_2_ccw(self) -> None:
        """Android portrait (rotation=90) is the mirror case: CCW in pixels."""
        assert _transpose_filter_for(90) == "transpose=2"

    def test_180_maps_to_double_transpose(self) -> None:
        assert _transpose_filter_for(180) == "transpose=2,transpose=2"

    def test_neg_180_same_as_180(self) -> None:
        assert _transpose_filter_for(-180) == "transpose=2,transpose=2"

    def test_270_aliases_to_neg_90(self) -> None:
        assert _transpose_filter_for(270) == "transpose=1"

    def test_neg_270_aliases_to_pos_90(self) -> None:
        assert _transpose_filter_for(-270) == "transpose=2"

    def test_unsupported_raises(self) -> None:
        with pytest.raises(OrientationError, match="unsupported rotation"):
            _transpose_filter_for(45)


# ---------------------------------------------------------------------------
# detect_rotation — subprocess.run is mocked. No real FFmpeg.
# ---------------------------------------------------------------------------


def _ffprobe_stream(rotation: int | None) -> str:
    """Build a fake ffprobe JSON output with optional rotation flag."""
    stream: dict = {
        "codec_type": "video",
        "codec_name": "hevc",
        "width": 1920,
        "height": 1080,
    }
    if rotation is not None:
        stream["side_data_list"] = [
            {"side_data_type": "Display Matrix", "rotation": rotation},
        ]
    return json.dumps({"streams": [stream]})


class TestDetectRotation:
    def test_returns_zero_when_no_flag(self) -> None:
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout=_ffprobe_stream(None), stderr="")
            assert detect_rotation("/fake/path.mp4") == 0

    def test_returns_minus_90_for_iphone_case(self) -> None:
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout=_ffprobe_stream(-90), stderr="")
            assert detect_rotation("/fake/path.mp4") == -90

    def test_returns_90_for_android_case(self) -> None:
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout=_ffprobe_stream(90), stderr="")
            assert detect_rotation("/fake/path.mp4") == 90

    def test_returns_180_for_upside_down(self) -> None:
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout=_ffprobe_stream(180), stderr="")
            assert detect_rotation("/fake/path.mp4") == 180

    def test_raises_on_ffprobe_failure(self) -> None:
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="broken")
            with pytest.raises(OrientationError, match="ffprobe failed"):
                detect_rotation("/fake/bad.mp4")

    def test_raises_on_timeout(self) -> None:
        with patch(
            "subprocess.run",
            side_effect=subprocess.TimeoutExpired(cmd="ffprobe", timeout=30),
        ):
            with pytest.raises(OrientationError, match="timed out"):
                detect_rotation("/fake/slow.mp4")

    def test_raises_on_invalid_json(self) -> None:
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="not json", stderr="")
            with pytest.raises(OrientationError, match="not valid JSON"):
                detect_rotation("/fake/junk.mp4")

    def test_returns_zero_when_no_video_stream(self) -> None:
        # No video stream isn't our problem — orchestrator fails downstream
        # with a clearer error.
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0,
                stdout=json.dumps({"streams": [{"codec_type": "audio"}]}),
                stderr="",
            )
            assert detect_rotation("/fake/audio_only.mp3") == 0

    def test_raises_when_ffprobe_missing(self) -> None:
        with patch("subprocess.run", side_effect=FileNotFoundError):
            with pytest.raises(OrientationError, match="ffprobe not found"):
                detect_rotation("/fake/path.mp4")


# ---------------------------------------------------------------------------
# normalize_orientation — real FFmpeg invocation on generated fixtures.
# Skipped if ffmpeg isn't on PATH (e.g. CI without ffmpeg-action).
# ---------------------------------------------------------------------------


def _ffmpeg_available() -> bool:
    return shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None


needs_ffmpeg = pytest.mark.skipif(
    not _ffmpeg_available(),
    reason="ffmpeg/ffprobe not installed",
)


def _build_plain_clip(out_path: Path, width: int = 320, height: int = 240) -> Path:
    """Generate a tiny H.264 + AAC test clip with NO rotation metadata."""
    cmd = [
        "ffmpeg",
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
        "-f",
        "lavfi",
        "-i",
        f"testsrc=duration=1:size={width}x{height}:rate=10",
        "-f",
        "lavfi",
        "-i",
        "sine=frequency=440:duration=1",
        "-c:v",
        "libx264",
        "-preset",
        "ultrafast",
        "-pix_fmt",
        "yuv420p",
        "-profile:v",
        "high",
        "-c:a",
        "aac",
        "-shortest",
        str(out_path),
    ]
    subprocess.run(cmd, check=True, capture_output=True, timeout=30)
    return out_path


def _bake_rotation(in_path: Path, out_path: Path, rotation_deg: int) -> Path:
    """Remux `in_path` adding a Display Matrix rotation flag.
    Uses `-display_rotation` as an INPUT option + `-c copy` so no pixels
    are touched — only the container metadata changes. Matches what
    iPhones / Androids emit natively."""
    cmd = [
        "ffmpeg",
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
        "-display_rotation",
        str(rotation_deg),
        "-i",
        str(in_path),
        "-c",
        "copy",
        str(out_path),
    ]
    subprocess.run(cmd, check=True, capture_output=True, timeout=30)
    return out_path


def _probe_for_rotation(path: Path) -> int:
    """Run ffprobe and return the Display Matrix rotation, or 0."""
    cmd = [
        "ffprobe",
        "-v",
        "error",
        "-print_format",
        "json",
        "-show_streams",
        str(path),
    ]
    result = subprocess.run(cmd, check=True, capture_output=True, text=True, timeout=10)
    data = json.loads(result.stdout)
    vstream = next(s for s in data["streams"] if s.get("codec_type") == "video")
    for entry in vstream.get("side_data_list") or []:
        if entry.get("side_data_type") == "Display Matrix":
            return int(entry.get("rotation", 0))
    return 0


def _probe_dims(path: Path) -> tuple[int, int]:
    cmd = [
        "ffprobe",
        "-v",
        "error",
        "-print_format",
        "json",
        "-show_streams",
        str(path),
    ]
    result = subprocess.run(cmd, check=True, capture_output=True, text=True, timeout=10)
    data = json.loads(result.stdout)
    vstream = next(s for s in data["streams"] if s.get("codec_type") == "video")
    return int(vstream["width"]), int(vstream["height"])


def _has_audio_stream(path: Path) -> bool:
    cmd = [
        "ffprobe",
        "-v",
        "error",
        "-print_format",
        "json",
        "-show_streams",
        str(path),
    ]
    result = subprocess.run(cmd, check=True, capture_output=True, text=True, timeout=10)
    data = json.loads(result.stdout)
    return any(s.get("codec_type") == "audio" for s in data["streams"])


@pytest.fixture(scope="module")
def plain_clip(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Session-scoped plain 320x240 H.264 clip, no rotation metadata."""
    if not _ffmpeg_available():
        pytest.skip("ffmpeg not installed")
    d = tmp_path_factory.mktemp("orientation_fixtures")
    return _build_plain_clip(d / "plain.mp4")


@pytest.fixture
def rotated_neg90_clip(plain_clip: Path, tmp_path: Path) -> Path:
    """Per-test copy of plain clip with rotation=-90 baked in."""
    return _bake_rotation(plain_clip, tmp_path / "rot_neg90.mov", -90)


@pytest.fixture
def rotated_pos90_clip(plain_clip: Path, tmp_path: Path) -> Path:
    return _bake_rotation(plain_clip, tmp_path / "rot_pos90.mov", 90)


@pytest.fixture
def rotated_180_clip(plain_clip: Path, tmp_path: Path) -> Path:
    return _bake_rotation(plain_clip, tmp_path / "rot_180.mov", 180)


@needs_ffmpeg
class TestNormalizeOrientationIntegration:
    def test_no_op_when_no_rotation_flag(self, plain_clip: Path, tmp_path: Path) -> None:
        # Copy so we don't mutate the module-scoped fixture.
        target = tmp_path / "plain_copy.mp4"
        target.write_bytes(plain_clip.read_bytes())
        original_bytes = target.read_bytes()

        normalize_orientation(str(target))

        assert target.read_bytes() == original_bytes, (
            "no-rotation file should be untouched byte-for-byte"
        )

    def test_neg_90_normalize_strips_flag(self, rotated_neg90_clip: Path) -> None:
        assert _probe_for_rotation(rotated_neg90_clip) == -90, "precondition"

        normalize_orientation(str(rotated_neg90_clip))

        assert _probe_for_rotation(rotated_neg90_clip) == 0, (
            "rotation metadata must be stripped after normalize"
        )

    def test_neg_90_swaps_pixel_dimensions(self, rotated_neg90_clip: Path) -> None:
        w_before, h_before = _probe_dims(rotated_neg90_clip)
        assert (w_before, h_before) == (320, 240), "precondition"

        normalize_orientation(str(rotated_neg90_clip))

        w_after, h_after = _probe_dims(rotated_neg90_clip)
        assert (w_after, h_after) == (240, 320), (
            f"dimensions should swap landscape→portrait, got {w_after}x{h_after}"
        )

    def test_pos_90_swaps_pixel_dimensions(self, rotated_pos90_clip: Path) -> None:
        normalize_orientation(str(rotated_pos90_clip))
        w, h = _probe_dims(rotated_pos90_clip)
        assert (w, h) == (240, 320)

    def test_180_preserves_dimensions(self, rotated_180_clip: Path) -> None:
        # Two transposes = 180° = same WxH, just upside down.
        normalize_orientation(str(rotated_180_clip))
        w, h = _probe_dims(rotated_180_clip)
        assert (w, h) == (320, 240)
        assert _probe_for_rotation(rotated_180_clip) == 0

    def test_square_pixels_with_rotation_flag_take_re_encode_path(
        self, plain_clip: Path, tmp_path: Path
    ) -> None:
        """Square videos (width == height) with a rotation flag fall through
        to the re-encode path, NOT the stale-flag fast path. iPhone Live
        Photo videos can land here. Trusting the flag is the conservative
        default since pixel dims give no orientation signal for squares.

        Pin the boundary so a future refactor that flips ``height > width``
        to ``height >= width`` doesn't silently change behavior."""
        if not _ffmpeg_available():
            pytest.skip("ffmpeg not installed")
        d = tmp_path / "square_plain.mp4"
        _build_plain_clip(d, width=320, height=320)
        target = _bake_rotation(d, tmp_path / "square_rotated.mov", -90)
        assert _probe_dims(target) == (320, 320), "precondition"

        with patch("app.services.pipeline_trace.record_pipeline_event") as mock_record:
            normalize_orientation(str(target))

        events = [c.kwargs.get("event") or (c.args[1] if len(c.args) > 1 else None)
                  for c in mock_record.call_args_list]
        assert "normalized" in events, (
            f"square+flag must hit the re-encode path (event='normalized'), got {events}"
        )

    def test_pos_90_pixel_direction_matches_player(
        self, rotated_pos90_clip: Path, tmp_path: Path
    ) -> None:
        """Mirror of test_neg_90_pixel_direction_matches_player for the +90
        (Android) branch. The +90 transpose was ALSO flipped in this fix
        (was transpose=1, now transpose=2). Without this test, a future
        regression that re-inverted only the +90 branch would not be caught
        by any integration-level assertion — the dim-swap test passes for
        both directions identically."""
        reference = tmp_path / "ref_pos90.png"
        subprocess.run(
            [
                "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
                "-ss", "0.3", "-i", str(rotated_pos90_clip),
                "-vframes", "1", str(reference),
            ],
            check=True, timeout=30, capture_output=True,
        )
        normalize_orientation(str(rotated_pos90_clip))
        normalized = tmp_path / "norm_pos90.png"
        subprocess.run(
            [
                "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
                "-ss", "0.3", "-i", str(rotated_pos90_clip),
                "-vframes", "1", str(normalized),
            ],
            check=True, timeout=30, capture_output=True,
        )
        psnr_out = subprocess.run(
            [
                "ffmpeg", "-y", "-hide_banner", "-loglevel", "info",
                "-i", str(normalized), "-i", str(reference),
                "-filter_complex", "psnr",
                "-f", "null", "-",
            ],
            capture_output=True, text=True, timeout=30, check=True,
        )
        import re
        match = re.search(r"PSNR.*average:([0-9.]+|inf)", psnr_out.stderr)
        assert match is not None, f"PSNR parse failed:\n{psnr_out.stderr[-500:]}"
        psnr_value = float("inf") if match.group(1) == "inf" else float(match.group(1))
        assert psnr_value > 25.0, (
            f"+90 normalized frame diverges from player reference (PSNR avg = {psnr_value} dB). "
            "Likely a wrong transpose direction on the +90 branch — will ship sideways/upside-down."
        )

    def test_neg_90_pixel_direction_matches_player(
        self, rotated_neg90_clip: Path, tmp_path: Path
    ) -> None:
        """REGRESSION: PR #192 shipped with the wrong transpose direction,
        producing upside-down output for genuine iPhone portrait clips. The
        dim-swap tests above did NOT catch it because both CW and CCW
        rotations swap WxH identically.

        This test compares the first-frame pixel content of our normalized
        output against the canonical "what a video player shows" reference
        (ffmpeg default auto-rotate). If they match within a tight tolerance,
        our transpose direction is correct. If they diverge by more than a
        few RGB units, the rotation is inverted and the clip will play
        upside-down/sideways in production.
        """
        # Reference: what a media player (QuickTime, browser, iPhone)
        # would show. ffmpeg's default behavior reads the Display Matrix
        # and auto-rotates on decode, giving us the "user's intended view".
        reference = tmp_path / "reference_autorotate.png"
        subprocess.run(
            [
                "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
                "-ss", "0.3", "-i", str(rotated_neg90_clip),
                "-vframes", "1", str(reference),
            ],
            check=True,
            timeout=30,
            capture_output=True,
        )

        # Run our normalize (in-place).
        normalize_orientation(str(rotated_neg90_clip))
        normalized_frame = tmp_path / "normalized_frame.png"
        subprocess.run(
            [
                "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
                "-ss", "0.3", "-i", str(rotated_neg90_clip),
                "-vframes", "1", str(normalized_frame),
            ],
            check=True,
            timeout=30,
            capture_output=True,
        )

        # Reference and normalized frame should be visually equivalent.
        # ffmpeg's PSNR filter gives us a single quality number — for two
        # frames showing the same content with the same encoding pass,
        # PSNR should be very high (lossless ≈ inf, near-lossless > 40dB).
        # A wrong-direction rotation would produce a completely different
        # frame, PSNR well below 20dB.
        psnr_out = subprocess.run(
            [
                "ffmpeg", "-y", "-hide_banner", "-loglevel", "info",
                "-i", str(normalized_frame),
                "-i", str(reference),
                "-filter_complex", "psnr",
                "-f", "null", "-",
            ],
            capture_output=True,
            text=True,
            timeout=30,
            check=True,
        )
        # PSNR result is on stderr like:
        #   [Parsed_psnr_0 @ 0x...] PSNR y:42.5 u:48.1 v:48.2 average:43.7 min:43.7 max:43.7
        import re
        match = re.search(r"PSNR.*average:([0-9.]+|inf)", psnr_out.stderr)
        assert match is not None, (
            f"could not parse PSNR from ffmpeg output:\n{psnr_out.stderr[-500:]}"
        )
        psnr_str = match.group(1)
        psnr_value = float("inf") if psnr_str == "inf" else float(psnr_str)
        # 25dB is a permissive floor — same content, same codec, same
        # frame index should easily clear this. A wrong rotation produces
        # PSNR well below 20dB (totally different image).
        assert psnr_value > 25.0, (
            f"normalized frame diverges from player reference (PSNR avg = {psnr_value} dB). "
            "Almost certainly a wrong transpose direction — output will play "
            "upside-down or sideways in production."
        )

    def test_preserves_audio_stream(self, rotated_neg90_clip: Path) -> None:
        assert _has_audio_stream(rotated_neg90_clip), "precondition"
        normalize_orientation(str(rotated_neg90_clip))
        assert _has_audio_stream(rotated_neg90_clip), (
            "audio stream must survive the re-encode (we use c:a copy)"
        )

    def test_returns_same_path(self, rotated_neg90_clip: Path) -> None:
        # Callers in template_orchestrate.py rely on the in-place
        # contract — they don't track a new path.
        returned = normalize_orientation(str(rotated_neg90_clip))
        assert returned == str(rotated_neg90_clip)

    def test_pipeline_event_fires_on_normalize(self, rotated_neg90_clip: Path) -> None:
        """admin/jobs/{id} surfaces orientation decisions via pipeline_trace.
        The user's verification flow ('see in admin debug that the upload was
        normalized') depends on this event actually firing. Pin it."""
        with patch("app.services.pipeline_trace.record_pipeline_event") as mock_record:
            normalize_orientation(str(rotated_neg90_clip))
        assert mock_record.called, "normalize_orientation must record a pipeline event"
        call_kwargs = mock_record.call_args.kwargs or dict(
            zip(("stage", "event", "data"), mock_record.call_args.args, strict=False)
        )
        assert call_kwargs["stage"] == "orientation"
        assert call_kwargs["event"] == "normalized"
        assert call_kwargs["data"]["rotation_applied"] == -90

    def test_pipeline_event_fires_on_skip(self, plain_clip: Path, tmp_path: Path) -> None:
        """Skipped (rotation=0) case also fires an event so admin debug can
        show 'normalize ran, no rotation detected' instead of silence."""
        target = tmp_path / "plain_copy.mp4"
        target.write_bytes(plain_clip.read_bytes())
        with patch("app.services.pipeline_trace.record_pipeline_event") as mock_record:
            normalize_orientation(str(target))
        assert mock_record.called
        call_kwargs = mock_record.call_args.kwargs or dict(
            zip(("stage", "event", "data"), mock_record.call_args.args, strict=False)
        )
        assert call_kwargs["stage"] == "orientation"
        assert call_kwargs["event"] == "skipped"
        assert call_kwargs["data"]["rotation"] == 0

    def test_ffmpeg_failure_raises(self, tmp_path: Path) -> None:
        # Create a file with a valid Display Matrix flag but invalid
        # contents — ffprobe sees the rotation, ffmpeg fails to decode.
        # We simulate this by mocking subprocess.run for the ffmpeg call
        # but letting ffprobe through.
        bad = tmp_path / "bad.mp4"
        bad.write_bytes(b"not a real video")

        # Mock the probe helper directly so we skip ffprobe (which would
        # legitimately fail on this junk file) and reach the ffmpeg
        # re-encode path. We return landscape dims so the stale-flag
        # fast path is NOT taken (we want to exercise the re-encode failure).
        with patch(
            "app.pipeline.orientation.detect_rotation_and_dims",
            return_value=(-90, 320, 240),
        ):
            with pytest.raises(OrientationError, match="ffmpeg normalize failed"):
                normalize_orientation(str(bad))

    def test_strip_flag_only_ffmpeg_failure_raises(self, tmp_path: Path) -> None:
        """The stale-flag (codec-copy remux) path has its own error handler.
        Pin it — a corrupt or unreadable file routed to the strip path must
        raise OrientationError with the strip-specific message."""
        bad = tmp_path / "bad_portrait.mp4"
        bad.write_bytes(b"not a real video")

        # Force the stale-flag fast path by returning portrait dims with a
        # rotation flag (height > width with rotation in ±90 → strip path).
        with patch(
            "app.pipeline.orientation.detect_rotation_and_dims",
            return_value=(-90, 240, 320),
        ):
            with pytest.raises(OrientationError, match="ffmpeg flag-strip failed"):
                normalize_orientation(str(bad))

    def test_strip_flag_only_timeout_raises(
        self, plain_clip: Path, tmp_path: Path
    ) -> None:
        """The strip path's TimeoutExpired handler must clean up the tmp
        file and raise OrientationError with the strip-specific timeout
        message. Mock subprocess.run to simulate the timeout."""
        import subprocess as _subprocess

        # Use a real valid clip so ffprobe (the dims probe) succeeds, but
        # force-route to the stale-flag path by mocking dims, then mock
        # ffmpeg call to raise TimeoutExpired.
        target = tmp_path / "portrait_real.mp4"
        target.write_bytes(plain_clip.read_bytes())

        real_run = _subprocess.run
        cmd_seen: list[list[str]] = []

        def fake_run(cmd, *args, **kwargs):  # type: ignore[no-untyped-def]
            cmd_seen.append(list(cmd))
            # ffprobe calls are fine — pass through
            if cmd[0] == "ffprobe":
                return real_run(cmd, *args, **kwargs)
            # ffmpeg call → simulate hang
            raise _subprocess.TimeoutExpired(cmd=cmd, timeout=300)

        with patch(
            "app.pipeline.orientation.detect_rotation_and_dims",
            return_value=(-90, 240, 320),  # forces strip path
        ):
            with patch(
                "app.pipeline.orientation.subprocess.run",
                side_effect=fake_run,
            ):
                with pytest.raises(OrientationError, match="ffmpeg flag-strip timed out"):
                    normalize_orientation(str(target))

        # Tmp file (would be <input>.flagstrip.tmp.mp4) must not be left behind.
        tmp_artifact = target.with_suffix(target.suffix + ".flagstrip.tmp.mp4")
        assert not tmp_artifact.exists(), (
            "strip path must clean up its tmp file on timeout"
        )


# ---------------------------------------------------------------------------
# Stale-flag regression tests — portrait pixels carrying a redundant rotation
# flag. These are the case that produced upside-down/sideways output in
# v0.4.27.0 (PR #192). The fix makes normalize_orientation strip the flag
# without re-encoding pixels.
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def plain_portrait_clip(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Session-scoped plain 240x320 H.264 clip (portrait pixel dims), no
    rotation metadata. Simulates an already-rotated portrait file (e.g.
    iOS Photos export) before any rotation flag is added."""
    if not _ffmpeg_available():
        pytest.skip("ffmpeg not installed")
    d = tmp_path_factory.mktemp("orientation_fixtures_portrait")
    return _build_plain_clip(d / "portrait_plain.mp4", width=240, height=320)


@pytest.fixture
def portrait_with_stale_neg90(plain_portrait_clip: Path, tmp_path: Path) -> Path:
    """Portrait pixels (240x320) + baked rotation:-90 flag.

    Pre-fix behavior: normalize_orientation re-encoded with transpose=2,
    which rotated the already-upright pixels 90° CCW → sideways/upside-down.
    Post-fix behavior: strip the flag, leave pixels intact.
    """
    return _bake_rotation(plain_portrait_clip, tmp_path / "portrait_stale_neg90.mov", -90)


@pytest.fixture
def portrait_with_stale_pos90(plain_portrait_clip: Path, tmp_path: Path) -> Path:
    return _bake_rotation(plain_portrait_clip, tmp_path / "portrait_stale_pos90.mov", 90)


@pytest.fixture
def portrait_with_stale_180(plain_portrait_clip: Path, tmp_path: Path) -> Path:
    return _bake_rotation(plain_portrait_clip, tmp_path / "portrait_stale_180.mov", 180)


@needs_ffmpeg
class TestNormalizeOrientationStaleFlagRegression:
    """Regression tests for the v0.4.27.0 stale-flag double-rotation bug.

    Each test asserts the fix's hard contract: when pixels are already in
    portrait orientation, the rotation flag is removed but pixel dims and
    pixel content are untouched.
    """

    def test_stale_neg90_strips_flag_without_rotating_pixels(
        self, portrait_with_stale_neg90: Path
    ) -> None:
        assert _probe_for_rotation(portrait_with_stale_neg90) == -90, "precondition"
        assert _probe_dims(portrait_with_stale_neg90) == (240, 320), "precondition"

        normalize_orientation(str(portrait_with_stale_neg90))

        assert _probe_for_rotation(portrait_with_stale_neg90) == 0, (
            "stale rotation flag must be stripped"
        )
        assert _probe_dims(portrait_with_stale_neg90) == (240, 320), (
            "pixel dims must NOT change — applying transpose to already-portrait "
            "pixels was the v0.4.27.0 bug"
        )

    def test_stale_pos90_strips_flag_without_rotating_pixels(
        self, portrait_with_stale_pos90: Path
    ) -> None:
        assert _probe_for_rotation(portrait_with_stale_pos90) == 90, "precondition"
        assert _probe_dims(portrait_with_stale_pos90) == (240, 320), "precondition"

        normalize_orientation(str(portrait_with_stale_pos90))

        assert _probe_for_rotation(portrait_with_stale_pos90) == 0
        assert _probe_dims(portrait_with_stale_pos90) == (240, 320)

    def test_stale_180_strips_flag_without_rotating_pixels(
        self, portrait_with_stale_180: Path
    ) -> None:
        # ffmpeg's `-display_rotation 180` may store the value as either
        # 180 or -180 in side_data depending on version — they're equivalent
        # rotations and `normalize_orientation` accepts both via
        # `_SUPPORTED_ROTATIONS`. Pin the precondition loosely.
        assert _probe_for_rotation(portrait_with_stale_180) in (180, -180), "precondition"
        assert _probe_dims(portrait_with_stale_180) == (240, 320), "precondition"

        normalize_orientation(str(portrait_with_stale_180))

        assert _probe_for_rotation(portrait_with_stale_180) == 0
        assert _probe_dims(portrait_with_stale_180) == (240, 320)

    def test_stale_neg90_records_flag_stripped_event(
        self, portrait_with_stale_neg90: Path
    ) -> None:
        """The admin job-debug view distinguishes stale-flag strips from
        legitimate transposes via the event name. Pin the wire."""
        with patch("app.services.pipeline_trace.record_pipeline_event") as mock_record:
            normalize_orientation(str(portrait_with_stale_neg90))
        assert mock_record.called
        call_kwargs = mock_record.call_args.kwargs or dict(
            zip(("stage", "event", "data"), mock_record.call_args.args, strict=False)
        )
        assert call_kwargs["stage"] == "orientation"
        assert call_kwargs["event"] == "flag_stripped_no_rotation"
        assert call_kwargs["data"]["rotation_flag"] == -90
        assert call_kwargs["data"]["width"] == 240
        assert call_kwargs["data"]["height"] == 320

    def test_stale_180_records_separate_event(
        self, portrait_with_stale_180: Path
    ) -> None:
        """180° on portrait pixels emits its own event so the warning log
        and the event are aligned. Operators looking for 'is there a real
        upside-down recording?' can filter on this event."""
        with patch("app.services.pipeline_trace.record_pipeline_event") as mock_record:
            normalize_orientation(str(portrait_with_stale_180))
        assert mock_record.called
        call_kwargs = mock_record.call_args.kwargs or dict(
            zip(("stage", "event", "data"), mock_record.call_args.args, strict=False)
        )
        assert call_kwargs["event"] == "flag_stripped_no_rotation_180"


# ---------------------------------------------------------------------------
# Env-var kill switch — ops should be able to disable orientation
# normalization without a redeploy if a regression slips in.
# ---------------------------------------------------------------------------


@needs_ffmpeg
class TestNormalizeOrientationKillSwitch:
    def test_env_false_disables_normalize(
        self, rotated_neg90_clip: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """With ORIENTATION_NORMALIZE_ENABLED=false the function must be a
        no-op: the input file is untouched byte-for-byte."""
        original_bytes = rotated_neg90_clip.read_bytes()
        monkeypatch.setenv("ORIENTATION_NORMALIZE_ENABLED", "false")

        normalize_orientation(str(rotated_neg90_clip))

        assert rotated_neg90_clip.read_bytes() == original_bytes, (
            "kill switch must skip ffprobe and ffmpeg — file unchanged"
        )
        # Flag must still be present since we did nothing.
        assert _probe_for_rotation(rotated_neg90_clip) == -90

    def test_env_false_records_disabled_event(
        self, rotated_neg90_clip: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("ORIENTATION_NORMALIZE_ENABLED", "false")
        with patch("app.services.pipeline_trace.record_pipeline_event") as mock_record:
            normalize_orientation(str(rotated_neg90_clip))
        assert mock_record.called
        call_kwargs = mock_record.call_args.kwargs or dict(
            zip(("stage", "event", "data"), mock_record.call_args.args, strict=False)
        )
        assert call_kwargs["stage"] == "orientation"
        assert call_kwargs["event"] == "disabled_by_env"

    def test_env_true_default_runs_normalize(
        self, rotated_neg90_clip: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Default (no env var, or =true) must keep current behavior. Guards
        against accidental opt-out via typo / casing mismatch."""
        monkeypatch.delenv("ORIENTATION_NORMALIZE_ENABLED", raising=False)
        normalize_orientation(str(rotated_neg90_clip))
        assert _probe_for_rotation(rotated_neg90_clip) == 0, (
            "default env state must still normalize"
        )


# ---------------------------------------------------------------------------
# detect_rotation_and_dims — single-call probe returning rotation + dims.
# ---------------------------------------------------------------------------


@needs_ffmpeg
class TestDetectRotationAndDims:
    def test_returns_dims_for_plain_landscape(self, plain_clip: Path) -> None:
        from app.pipeline.orientation import detect_rotation_and_dims

        rotation, width, height = detect_rotation_and_dims(str(plain_clip))
        assert rotation == 0
        assert (width, height) == (320, 240)

    def test_returns_dims_for_rotated(self, rotated_neg90_clip: Path) -> None:
        from app.pipeline.orientation import detect_rotation_and_dims

        rotation, width, height = detect_rotation_and_dims(str(rotated_neg90_clip))
        assert rotation == -90
        # Pre-normalize: pixels are still landscape, flag says rotate.
        assert (width, height) == (320, 240)

    def test_returns_dims_for_portrait_with_stale_flag(
        self, portrait_with_stale_neg90: Path
    ) -> None:
        """This is the case the fix branches on: portrait dims + rotation
        flag together signal a stale flag, not a real rotation."""
        from app.pipeline.orientation import detect_rotation_and_dims

        rotation, width, height = detect_rotation_and_dims(str(portrait_with_stale_neg90))
        assert rotation == -90
        assert (width, height) == (240, 320)
        # The fix uses height > width to recognize this case.
        assert height > width
