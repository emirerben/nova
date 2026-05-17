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
    def test_neg_90_maps_to_transpose_2(self) -> None:
        assert _transpose_filter_for(-90) == "transpose=2"

    def test_pos_90_maps_to_transpose_1(self) -> None:
        assert _transpose_filter_for(90) == "transpose=1"

    def test_180_maps_to_double_transpose(self) -> None:
        assert _transpose_filter_for(180) == "transpose=2,transpose=2"

    def test_neg_180_same_as_180(self) -> None:
        assert _transpose_filter_for(-180) == "transpose=2,transpose=2"

    def test_270_aliases_to_neg_90(self) -> None:
        assert _transpose_filter_for(270) == "transpose=2"

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

        # Mock detect_rotation directly so we skip ffprobe (which would
        # legitimately fail on this junk file) and reach the ffmpeg path.
        with patch("app.pipeline.orientation.detect_rotation", return_value=-90):
            with pytest.raises(OrientationError, match="ffmpeg normalize failed"):
                normalize_orientation(str(bad))
