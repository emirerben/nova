"""Subject matte engine — per-frame person-segmentation masks for text occlusion.

Feeds the "text behind subject" effect: a low-resolution grayscale matte
video, one frame per rendered output tick, where pixel value ~= probability
that a person occupies that pixel. Downstream text-burn steps multiply
overlay alpha by the (upscaled) mask so the subject appears in front of text.

Best-effort by design: every public function degrades to ``None`` on any
failure (missing model, unreadable video, mediapipe not installed, wall-clock
budget blown, corrupt matte file) and never raises. A matte failure must
never fail a render job — it just means the occlusion effect is skipped.

Uses MediaPipe's ImageSegmenter task (selfie segmenter, VIDEO running mode,
soft confidence masks — no hard threshold). ``mediapipe`` is imported lazily
inside functions so this module can be imported without it installed (the
structural eval-CI constraint other lazy-imported pipeline deps share).

CRITICAL: Never use MoviePy — see CLAUDE.md. Decoding goes through
cv2.VideoCapture; the matte is muxed via a direct ffmpeg subprocess.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from dataclasses import asdict, dataclass

import cv2
import numpy as np
import structlog

log = structlog.get_logger()

MATTE_FPS = 30
# Resolved relative to the api app root (src/apps/api/), same pattern as
# FONTS_DIR in text_overlay.py.
MATTE_MODEL_PATH = "assets/models/selfie_segmenter.tflite"
MATTE_WALL_CLOCK_BUDGET_S = 90

# Stored matte resolution (~1/4 of 1080x1920). Hold-to-EOF overlays (see
# generative_overlays.py's _HOLD_TO_END_S) span windows up to the full clip,
# not just a few seconds — a 60s window is ~1800 frames @ 270x480 grayscale,
# ~230MB fully loaded in memory by SubjectMatteProvider. That transient is
# accepted on the worker's 6GB budget (see CLAUDE.md worker VM sizing).
_MATTE_WIDTH = 270
_MATTE_HEIGHT = 480

# Full-res output size mask_at() upscales to — matches template output.
_OUTPUT_WIDTH = 1080
_OUTPUT_HEIGHT = 1920

# mask_at() tolerance for t_abs landing just outside a window's edges
# (float rounding at window boundaries during render).
_WINDOW_EDGE_TOLERANCE_S = 0.05

_FFMPEG_MUX_TIMEOUT_S = 30


@dataclass
class MatteWindow:
    start_s: float
    end_s: float


@dataclass
class MatteStats:
    mean_coverage: float
    min_coverage: float
    max_coverage: float
    frame_count: int
    windows: list[tuple[float, float]]


class _MatteAbort(RuntimeError):
    """Internal control-flow signal for a graceful (non-bug) best-effort abort."""


def matte_is_sane(stats: MatteStats) -> bool:
    return 0.05 <= stats.mean_coverage <= 0.60 and stats.max_coverage > 0.02


def _resolve_model_path() -> str:
    """MATTE_MODEL_PATH resolved relative to the api app root.

    Reads the module-level constant fresh on every call (not cached) so
    tests can monkeypatch ``MATTE_MODEL_PATH`` to point at a missing file.
    """
    return os.path.normpath(os.path.join(os.path.dirname(__file__), "..", "..", MATTE_MODEL_PATH))


def _cleanup_partial(out_path: str) -> None:
    for path in (out_path, f"{out_path}.json"):
        try:
            if os.path.exists(path):
                os.remove(path)
        except OSError as exc:
            log.warning("subject_matte_cleanup_failed", path=path, error=str(exc))


def compute_subject_matte(
    video_path: str,
    windows: list[MatteWindow],
    out_path: str,
    *,
    inference_fps: int = 15,
) -> MatteStats | None:
    """Compute a person-segmentation matte for ``windows`` of ``video_path``.

    Writes a grayscale H.264 mp4 (windows concatenated back-to-back, no
    gaps for the time between windows) to ``out_path`` at MATTE_FPS, plus a
    sidecar JSON at ``out_path + ".json"``. Returns MatteStats on success,
    ``None`` on any failure — never raises.
    """
    start_time = time.monotonic()
    try:
        stats = _compute_subject_matte_inner(
            video_path, windows, out_path, inference_fps, start_time
        )
    except Exception as exc:  # noqa: BLE001
        log.warning(
            "subject_matte_compute_failed",
            error=str(exc),
            video_path=video_path,
            elapsed_s=round(time.monotonic() - start_time, 2),
        )
        _cleanup_partial(out_path)
        return None
    return stats


def _budget_check(start_time: float) -> None:
    if time.monotonic() - start_time > MATTE_WALL_CLOCK_BUDGET_S:
        raise _MatteAbort("matte wall-clock budget exceeded")


def _compute_subject_matte_inner(
    video_path: str,
    windows: list[MatteWindow],
    out_path: str,
    inference_fps: int,
    start_time: float,
) -> MatteStats:
    if not windows:
        raise _MatteAbort("no windows requested")

    model_path = _resolve_model_path()
    if not os.path.isfile(model_path):
        raise _MatteAbort(f"matte model not found at {model_path}")

    _budget_check(start_time)

    cap = cv2.VideoCapture(video_path)
    if not cap.isOpened():
        cap.release()
        raise _MatteAbort(f"cannot open video: {video_path}")

    _budget_check(start_time)

    # Lazy import — module import must succeed without mediapipe installed.
    import mediapipe as mp  # noqa: PLC0415
    from mediapipe.tasks import python as mp_python  # noqa: PLC0415
    from mediapipe.tasks.python import vision as mp_vision  # noqa: PLC0415

    base_options = mp_python.BaseOptions(model_asset_path=model_path)
    options = mp_vision.ImageSegmenterOptions(
        base_options=base_options,
        running_mode=mp_vision.RunningMode.VIDEO,
        output_confidence_masks=True,
        output_category_mask=False,
    )
    segmenter = mp_vision.ImageSegmenter.create_from_options(options)

    proc: subprocess.Popen | None = None
    written_windows: list[tuple[float, float]] = []
    coverages: list[float] = []
    frame_count = 0
    infer_counter_ms = 0

    try:
        proc = _spawn_matte_writer(out_path)
        assert proc.stdin is not None

        for window in windows:
            _budget_check(start_time)
            if window.end_s <= window.start_s:
                log.warning(
                    "subject_matte_skipping_empty_window",
                    start_s=window.start_s,
                    end_s=window.end_s,
                )
                continue

            cap.set(cv2.CAP_PROP_POS_MSEC, window.start_s * 1000.0)
            num_output_frames = max(1, round((window.end_s - window.start_s) * MATTE_FPS))

            last_mask_small: np.ndarray | None = None
            last_infer_bucket = -1
            produced = 0

            for i in range(num_output_frames):
                _budget_check(start_time)
                t_rel = i / MATTE_FPS
                infer_bucket = int(t_rel * inference_fps)

                if infer_bucket != last_infer_bucket:
                    last_infer_bucket = infer_bucket
                    ok, frame = cap.read()
                    if ok:
                        rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
                        mp_image = mp.Image(image_format=mp.ImageFormat.SRGB, data=rgb)
                        infer_counter_ms += 1
                        result = segmenter.segment_for_video(mp_image, infer_counter_ms)
                        mask = result.confidence_masks[0].numpy_view().copy()
                        coverages.append(float(np.mean(mask)))
                        frame_count += 1
                        last_mask_small = cv2.resize(
                            mask,
                            (_MATTE_WIDTH, _MATTE_HEIGHT),
                            interpolation=cv2.INTER_LINEAR,
                        )
                    elif last_mask_small is None:
                        # Video shorter than the window — nothing usable yet.
                        break
                    # else: video exhausted mid-window; hold the last mask.

                if last_mask_small is None:
                    break

                frame_u8 = np.clip(last_mask_small * 255.0, 0, 255).astype(np.uint8)
                proc.stdin.write(frame_u8.tobytes())
                produced += 1

            if produced == 0:
                continue
            effective_end_s = window.start_s + produced / MATTE_FPS
            written_windows.append((window.start_s, effective_end_s))

        # communicate() sends EOF on stdin itself; closing it first makes the
        # flush inside communicate() raise "flush of closed file" on py3.11.
        remaining = max(1.0, MATTE_WALL_CLOCK_BUDGET_S - (time.monotonic() - start_time))
        _, stderr = proc.communicate(timeout=min(remaining, _FFMPEG_MUX_TIMEOUT_S))
        if proc.returncode != 0:
            raise _MatteAbort(f"ffmpeg matte mux failed: {stderr.decode(errors='replace')[:500]}")
    finally:
        segmenter.close()
        cap.release()
        if proc is not None and proc.poll() is None:
            proc.kill()
            proc.wait()

    if not written_windows or frame_count == 0:
        raise _MatteAbort("no matte frames produced")

    stats = MatteStats(
        mean_coverage=float(np.mean(coverages)),
        min_coverage=float(np.min(coverages)),
        max_coverage=float(np.max(coverages)),
        frame_count=frame_count,
        windows=written_windows,
    )

    sidecar = {
        "windows": [list(w) for w in written_windows],
        "fps": MATTE_FPS,
        "size": [_MATTE_WIDTH, _MATTE_HEIGHT],
        "stats": asdict(stats),
    }
    with open(f"{out_path}.json", "w") as f:
        json.dump(sidecar, f)

    return stats


def _spawn_matte_writer(out_path: str) -> subprocess.Popen:
    """Stream raw grayscale frames to ffmpeg's stdin, muxed as H.264 mp4.

    Intermediate artifact (never shown to users directly) — preset
    "ultrafast" is policy-compliant per tests/test_encoder_policy.py.
    """
    cmd = [
        "ffmpeg",
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
        "-f",
        "rawvideo",
        "-pix_fmt",
        "gray",
        "-s",
        f"{_MATTE_WIDTH}x{_MATTE_HEIGHT}",
        "-r",
        str(MATTE_FPS),
        "-i",
        "-",
        "-c:v",
        "libx264",
        "-preset",
        "ultrafast",
        "-pix_fmt",
        "yuv420p",
        "-movflags",
        "+faststart",
        out_path,
    ]
    return subprocess.Popen(
        cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE
    )


@dataclass
class _StoredWindow:
    start_s: float
    end_s: float
    frame_count: int
    first_frame_index: int


class SubjectMatteProvider:
    """Reads a matte file + sidecar once, serves per-timestamp masks in memory."""

    def __init__(
        self,
        frames: list[np.ndarray],
        windows: list[_StoredWindow],
        fps: int,
    ) -> None:
        self._frames = frames
        self._windows = windows
        self._fps = fps

    @classmethod
    def open(cls, matte_path: str) -> SubjectMatteProvider | None:
        try:
            return cls._open_inner(matte_path)
        except Exception as exc:  # noqa: BLE001
            log.warning("subject_matte_provider_open_failed", error=str(exc), matte_path=matte_path)
            return None

    @classmethod
    def _open_inner(cls, matte_path: str) -> SubjectMatteProvider | None:
        sidecar_path = f"{matte_path}.json"
        if not os.path.isfile(matte_path) or not os.path.isfile(sidecar_path):
            return None

        with open(sidecar_path) as f:
            meta = json.load(f)
        fps = int(meta["fps"])
        raw_windows = [(float(w[0]), float(w[1])) for w in meta["windows"]]

        cap = cv2.VideoCapture(matte_path)
        if not cap.isOpened():
            cap.release()
            return None

        frames: list[np.ndarray] = []
        try:
            while True:
                ok, frame = cap.read()
                if not ok:
                    break
                gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY) if frame.ndim == 3 else frame
                frames.append(gray)
        finally:
            cap.release()

        if not frames:
            return None

        stored_windows: list[_StoredWindow] = []
        offset = 0
        for start_s, end_s in raw_windows:
            count = max(1, round((end_s - start_s) * fps))
            count = min(count, len(frames) - offset)
            if count <= 0:
                break
            stored_windows.append(
                _StoredWindow(
                    start_s=start_s, end_s=end_s, frame_count=count, first_frame_index=offset
                )
            )
            offset += count

        if not stored_windows:
            return None

        return cls(frames=frames, windows=stored_windows, fps=fps)

    def _find_window(self, t_abs: float) -> _StoredWindow | None:
        for window in self._windows:
            if (
                window.start_s - _WINDOW_EDGE_TOLERANCE_S
                <= t_abs
                <= window.end_s + _WINDOW_EDGE_TOLERANCE_S
            ):
                return window
        return None

    def mask_at(self, t_abs: float) -> np.ndarray | None:
        # No memoization here: this provider is called concurrently from a
        # ThreadPoolExecutor (see text_overlay_skia's per-frame render pool),
        # where distinct threads request distinct frames — a single-entry
        # cache never legitimately hits and would need a lock to be safe.
        # cv2.resize is cheap relative to the PNG encode it feeds, so we
        # just resize on every call.
        window = self._find_window(t_abs)
        if window is None:
            return None

        t_clamped = min(max(t_abs, window.start_s), window.end_s)
        offset = round((t_clamped - window.start_s) * self._fps)
        offset = max(0, min(offset, window.frame_count - 1))
        global_index = window.first_frame_index + offset

        small = self._frames[global_index]
        return (
            cv2.resize(
                small,
                (_OUTPUT_WIDTH, _OUTPUT_HEIGHT),
                interpolation=cv2.INTER_LINEAR,
            ).astype(np.float32)
            / 255.0
        )
