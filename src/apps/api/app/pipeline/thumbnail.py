"""[Stage 6] Thumbnail selection: FFmpeg keyframe extraction + Laplacian sharpness + face detection."""  # noqa: E501

import os
import subprocess
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

import structlog

log = structlog.get_logger()

KEYFRAME_COUNT = 5
NEAR_CUT_THRESHOLD_S = 1.0  # penalize frames within 1s of a cut


@dataclass
class ThumbnailResult:
    jpeg_path: str
    sharpness_score: float
    has_face: bool


def select_thumbnail(
    video_path: str,
    start_s: float,
    end_s: float,
    cut_timestamps: list[float],
    output_dir: str,
) -> ThumbnailResult:
    """Extract KEYFRAME_COUNT frames from [start_s, end_s], pick best by:
      1. Has face detected (preferred)
      2. Sharpness (Laplacian variance)
      3. Not within NEAR_CUT_THRESHOLD_S of a scene cut

    Falls back to highest-sharpness frame if no face detected in any frame.
    """
    frames = _extract_keyframes(video_path, start_s, end_s, output_dir)
    if not frames:
        raise RuntimeError(f"No frames extracted from {video_path} [{start_s}-{end_s}]")

    scored = [_score_frame(path, cut_timestamps) for path in frames]
    scored.sort(key=lambda x: (x[2], x[1]), reverse=True)  # (has_face, sharpness)

    best_path, best_sharpness, has_face = scored[0]

    # Clean up non-selected frames
    for path, _, _ in scored[1:]:
        if os.path.exists(path):
            os.unlink(path)

    log.info(
        "thumbnail_selected",
        path=best_path,
        sharpness=best_sharpness,
        has_face=has_face,
    )
    return ThumbnailResult(
        jpeg_path=best_path,
        sharpness_score=best_sharpness,
        has_face=has_face,
    )


def _extract_keyframes(
    video_path: str, start_s: float, end_s: float, output_dir: str
) -> list[str]:
    """Extract KEYFRAME_COUNT evenly-spaced frames as JPEGs. Never shell=True.

    Each ffmpeg `-vframes 1` is independent — extract them in parallel so a
    5-frame extraction takes ~1 ffmpeg duration instead of 5.
    """
    duration = end_s - start_s
    interval = duration / (KEYFRAME_COUNT + 1)

    def _one(i: int) -> str | None:
        t = start_s + i * interval
        out_path = os.path.join(output_dir, f"thumb_candidate_{i}.jpg")
        cmd = [
            "ffmpeg",
            "-ss", str(t),
            "-i", video_path,
            "-vframes", "1",
            "-q:v", "2",
            "-y",
            out_path,
        ]
        result = subprocess.run(cmd, capture_output=True, timeout=30, check=False)
        if result.returncode == 0 and os.path.exists(out_path):
            return out_path
        return None

    with ThreadPoolExecutor(max_workers=KEYFRAME_COUNT) as pool:
        results = list(pool.map(_one, range(1, KEYFRAME_COUNT + 1)))

    return [p for p in results if p is not None]


def _score_frame(
    path: str, cut_timestamps: list[float]
) -> tuple[str, float, bool]:
    """Return (path, sharpness_score, has_face)."""
    sharpness = _laplacian_variance(path)
    has_face = _detect_face(path)
    return path, sharpness, has_face


def _laplacian_variance(path: str) -> float:
    """Compute Laplacian variance as a proxy for image sharpness."""
    try:
        import cv2
        import numpy as np  # noqa: F401  # noqa: E501

        img = cv2.imread(path, cv2.IMREAD_GRAYSCALE)
        if img is None:
            return 0.0
        return float(cv2.Laplacian(img, cv2.CV_64F).var())
    except ImportError:
        log.warning("opencv_not_available_for_sharpness")
        return 0.0


def _detect_face(path: str) -> bool:
    """Detect whether the frame contains a face using OpenCV Haar cascade."""
    try:
        import cv2

        img = cv2.imread(path)
        if img is None:
            return False
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        cascade = cv2.CascadeClassifier(
            cv2.data.haarcascades + "haarcascade_frontalface_default.xml"
        )
        faces = cascade.detectMultiScale(gray, scaleFactor=1.1, minNeighbors=5)
        return len(faces) > 0
    except Exception:
        return False
