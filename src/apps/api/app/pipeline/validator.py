"""FFprobe output spec validation — asserts resolution, codec, duration, audio."""

import json
import subprocess
from dataclasses import dataclass

import structlog

from app.config import settings

log = structlog.get_logger()


@dataclass
class ValidationResult:
    passed: bool
    errors: list[str]


def validate_output(
    file_path: str,
    expected_resolution: tuple[int, int] | None = None,
    expected_duration_range: tuple[float, float] | None = None,
) -> ValidationResult:
    """Run FFprobe on an exported clip and assert it meets the output spec.

    Checks:
    - resolution == 1080×1920 unless expected_resolution is provided
    - codec == h264
    - duration is within the configured template range unless a caller-specific
      range is provided
    - audio stream present
    """
    cmd = [
        "ffprobe",
        "-v",
        "quiet",
        "-print_format",
        "json",
        "-show_streams",
        "-show_format",
        file_path,
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30, check=False)
    except subprocess.TimeoutExpired:
        return ValidationResult(passed=False, errors=["ffprobe timed out"])

    if result.returncode != 0:
        return ValidationResult(passed=False, errors=[f"ffprobe failed: {result.stderr[:200]}"])

    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError:
        return ValidationResult(passed=False, errors=["ffprobe output not parseable"])

    streams = data.get("streams", [])
    fmt = data.get("format", {})
    video_stream = next((s for s in streams if s.get("codec_type") == "video"), None)
    audio_stream = next((s for s in streams if s.get("codec_type") == "audio"), None)

    errors: list[str] = []

    if video_stream is None:
        errors.append("No video stream found")
        return ValidationResult(passed=False, errors=errors)

    width = int(video_stream.get("width", 0))
    height = int(video_stream.get("height", 0))
    codec = video_stream.get("codec_name", "")
    duration_s = float(fmt.get("duration") or video_stream.get("duration", 0))

    expected_width, expected_height = expected_resolution or (
        settings.output_width,
        settings.output_height,
    )
    if width != expected_width or height != expected_height:
        errors.append(
            f"Resolution mismatch: expected {expected_width}×{expected_height}, "
            f"got {width}×{height}"
        )

    if codec != "h264":
        errors.append(f"Codec mismatch: expected h264, got {codec}")

    min_duration_s, max_duration_s = expected_duration_range or (
        settings.output_min_duration_s,
        settings.output_max_duration_s,
    )
    if not (min_duration_s <= duration_s <= max_duration_s):
        errors.append(
            f"Duration {duration_s:.1f}s outside spec [{min_duration_s}-{max_duration_s}]"
        )

    if audio_stream is None:
        errors.append("No audio stream found")

    passed = len(errors) == 0
    if not passed:
        log.warning("validation_failed", path=file_path, errors=errors)
    else:
        log.info("validation_passed", path=file_path, duration_s=duration_s)

    return ValidationResult(passed=passed, errors=errors)
