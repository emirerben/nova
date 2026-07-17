"""Subject matte engine — per-frame person-segmentation masks for text occlusion.

Feeds the "text behind subject" effect: a low-resolution grayscale matte
video, one frame per rendered output tick, where pixel value ~= probability
that a person occupies that pixel. Downstream text-burn steps multiply
overlay alpha by the (upscaled) mask so the subject appears in front of text.

Best-effort by design: every public function degrades to ``None`` on any
failure (missing model, unreadable video, mediapipe not installed, wall-clock
budget blown, corrupt matte file) and never raises. A matte failure must
never fail a render job — it just means the occlusion effect is skipped.

Uses MediaPipe's ImageSegmenter task (selfie segmenter, stateless IMAGE
running mode — one independent segmentation per frame).
Masks are sampled time-aligned at (up to) every source frame, temporally
median-filtered over 3 samples, hard-cut at 0.40 confidence, cleaned of tiny
fragments, and lightly feathered — the "solid object" treatment. The stored
matte already carries this treatment, so ``mask_at`` readers and both text
renderers stay treatment-agnostic. ``mediapipe`` is imported lazily inside
functions so this module can be imported without it installed (the
structural eval-CI constraint other lazy-imported pipeline deps share).

CRITICAL: Never use MoviePy — see CLAUDE.md. Decoding goes through
cv2.VideoCapture; the matte is muxed via a direct ffmpeg subprocess.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from collections import deque
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

# --- v3 "solid object" mask treatment -------------------------------------
# Confidence below the cut is background, above is subject — a hard cut (not
# the raw soft mask) so text never ghosts through flags/cars at partial
# confidence.
MASK_CONFIDENCE_CUT = 0.40
# Trailing temporal median over this many inference masks kills single-frame
# confidence flicker without visible lag (~1 frame at full rate).
_TEMPORAL_MEDIAN_FRAMES = 3
# Connected components smaller than this fraction of the matte frame are
# segmenter noise (background passers-by, speckle), not subjects. Real
# small/distant subjects (~0.8% of frame) must survive this cut.
_MIN_COMPONENT_AREA_FRAC = 0.002
# Thin feather on the binary mask edge (sigma in px at matte resolution;
# ~5px at 1080x1920 after upscale).
_FEATHER_SIGMA_PX = 1.2
# CAP_PROP_FPS sanity range — outside it we fall back to MATTE_FPS.
_MAX_REASONABLE_SRC_FPS = 240.0

# --- Small-subject ROI refinement ------------------------------------------
# The selfie segmenter squeezes the whole frame into its ~256px input, so a
# distant person is a handful of model pixels — confidence flaps 0.0→1.0→0.0
# across frames (beach wide shot). When the full-frame pass finds only a
# small subject region, a second pass re-segments a zoomed crop around it:
# the same person fills the model input and detection becomes rock-stable
# (measured: peak confidence 1.00 on every frame, 0 presence flips, vs 5
# flips full-frame).
# Trigger: union bbox of the treated pass-1 mask covers less than this
# fraction of the frame.
_ROI_SMALL_UNION_FRAC = 0.25
# Padding around the union bbox (factor on each dimension) and the minimum
# crop side (as a fraction of the frame) so the model keeps context.
_ROI_PAD_FACTOR = 2.0
_ROI_MIN_SIDE_FRAC = 0.2

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
    # Detection-stability signal: how many times the treated mask flipped
    # between "something present" and "essentially nothing" across output
    # frames (counted within windows, never across window boundaries), and
    # that count normalized per second of matte. A real subject doesn't blink
    # out of existence — flapping presence means the segmenter can't reliably
    # see the subject (small/distant people, low light) and occlusion would
    # glitch on/off.
    presence_flips: int = 0
    presence_flips_per_s: float = 0.0


class _MatteAbort(RuntimeError):
    """Internal control-flow signal for a graceful (non-bug) best-effort abort."""


# Presence below this treated-mask mean means "essentially nothing kept"
# (a single min-area component feathered over the frame averages ~0.002).
_PRESENCE_COVERAGE_FLOOR = 0.0015
# Unstable-detection gate: more than this many presence flips AND a flip
# rate above this threshold rejects the matte. Measured anchors: Argentina
# montage (stable, legit scene cut) = 1 flip / 0.29 per s; beach wide shot
# with segmenter dropouts (visible on/off glitch) = 5 flips / 1.56 per s.
_MAX_PRESENCE_FLIPS = 2
_MAX_PRESENCE_FLIPS_PER_S = 0.75


def matte_is_sane(stats: MatteStats) -> bool:
    """Reject degenerate or unstable mattes.

    Small/distant subjects are legitimate (a person at 0.8% of frame must
    keep the effect), so there is no meaningful lower bound on mean
    coverage — only "the segmenter never found anyone at all" (max) and
    "the mask swallowed the whole frame" (mean) are degenerate. A matte
    whose presence flaps on/off (segmenter dropouts on hard footage) is
    rejected too: occlusion that blinks is worse than plain text, and the
    engine is best-effort by design.
    """
    if stats.max_coverage < 0.01 or stats.mean_coverage > 0.85:
        return False
    if (
        stats.presence_flips > _MAX_PRESENCE_FLIPS
        and stats.presence_flips_per_s > _MAX_PRESENCE_FLIPS_PER_S
    ):
        return False
    return True


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
) -> MatteStats | None:
    """Compute a person-segmentation matte for ``windows`` of ``video_path``.

    Writes a grayscale H.264 mp4 (windows concatenated back-to-back, no
    gaps for the time between windows) to ``out_path`` at MATTE_FPS, plus a
    sidecar JSON at ``out_path + ".json"``. Returns MatteStats on success,
    ``None`` on any failure — never raises.
    """
    start_time = time.monotonic()
    try:
        stats = _compute_subject_matte_inner(video_path, windows, out_path, start_time)
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


def _postprocess_mask(recent_soft: deque[np.ndarray]) -> np.ndarray:
    """v3 "solid object" treatment on a trailing window of soft masks.

    Temporal median → 0.40 hard cut → tiny-fragment drop → thin feather.
    Input masks are float32 [0,1] at matte resolution; output is the same
    shape/dtype/range, ready to quantize into the matte stream.
    """
    if len(recent_soft) > 1:
        soft = np.median(np.stack(tuple(recent_soft)), axis=0).astype(np.float32)
    else:
        soft = recent_soft[0]

    binary = (soft >= MASK_CONFIDENCE_CUT).astype(np.uint8)

    if binary.any():
        n_labels, labels, comp_stats, _ = cv2.connectedComponentsWithStats(binary, connectivity=8)
        min_area = int(binary.size * _MIN_COMPONENT_AREA_FRAC)
        for label in range(1, n_labels):
            if comp_stats[label, cv2.CC_STAT_AREA] < min_area:
                binary[labels == label] = 0

    feathered = cv2.GaussianBlur(binary.astype(np.float32), ksize=(0, 0), sigmaX=_FEATHER_SIGMA_PX)
    return np.clip(feathered, 0.0, 1.0)


def _source_fps(cap: cv2.VideoCapture) -> float:
    fps = float(cap.get(cv2.CAP_PROP_FPS) or 0.0)
    if not (0.0 < fps <= _MAX_REASONABLE_SRC_FPS):
        return float(MATTE_FPS)
    return fps


def _small_subject_roi(
    treated_u8: list[np.ndarray],
) -> tuple[float, float, float, float] | None:
    """Fractional crop (fx0, fx1, fy0, fy1) around a small subject region,
    or None when the subject region is large enough for full-frame inference
    (or nothing was detected at all). Input masks are uint8 [0, 255]."""
    union = np.zeros_like(treated_u8[0], dtype=bool)
    for mask in treated_u8:
        union |= mask >= 128
    if not union.any():
        return None
    ys, xs = np.where(union)
    bw = (xs.max() - xs.min() + 1) / _MATTE_WIDTH
    bh = (ys.max() - ys.min() + 1) / _MATTE_HEIGHT
    if bw * bh > _ROI_SMALL_UNION_FRAC:
        return None
    cx = (xs.min() + xs.max() + 1) / 2.0 / _MATTE_WIDTH
    cy = (ys.min() + ys.max() + 1) / 2.0 / _MATTE_HEIGHT
    w = max(bw * _ROI_PAD_FACTOR, _ROI_MIN_SIDE_FRAC)
    h = max(bh * _ROI_PAD_FACTOR, _ROI_MIN_SIDE_FRAC)
    fx0 = float(np.clip(cx - w / 2.0, 0.0, 1.0))
    fx1 = float(np.clip(cx + w / 2.0, 0.0, 1.0))
    fy0 = float(np.clip(cy - h / 2.0, 0.0, 1.0))
    fy1 = float(np.clip(cy + h / 2.0, 0.0, 1.0))
    if fx1 <= fx0 or fy1 <= fy0:
        return None
    return (fx0, fx1, fy0, fy1)


def _compute_subject_matte_inner(
    video_path: str,
    windows: list[MatteWindow],
    out_path: str,
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

    # IMAGE running mode — stateless, one independent segmentation per frame.
    # VIDEO mode's internal temporal filter balloons and oscillates on busy
    # footage (night crowd scenes): its propagated state alternately swallowed
    # and released the whole text region, which survived every downstream
    # cleanup. Frame-to-frame stability comes from our own temporal median in
    # _postprocess_mask instead, which we can reason about and test.
    base_options = mp_python.BaseOptions(model_asset_path=model_path)
    options = mp_vision.ImageSegmenterOptions(
        base_options=base_options,
        running_mode=mp_vision.RunningMode.IMAGE,
        output_confidence_masks=True,
        output_category_mask=False,
    )
    segmenter = mp_vision.ImageSegmenter.create_from_options(options)

    src_fps = _source_fps(cap)

    proc: subprocess.Popen | None = None
    written_windows: list[tuple[float, float]] = []
    coverages: list[float] = []
    frame_count = 0
    presence_flips = 0
    total_produced = 0

    try:
        proc = _spawn_matte_writer(out_path)
        assert proc.stdin is not None

        def collect_window_masks(
            window: MatteWindow,
            roi_frac: tuple[float, float, float, float] | None,
        ) -> tuple[list[np.ndarray], int]:
            """One TREATED uint8 mask (matte res) per output tick; holds
            duplicate the previous mask. Returns (masks, inference_count).
            Buffered as uint8 so a 120s hold-to-EOF window stays in the same
            memory class as SubjectMatteProvider's full load (~470MB), never
            float32 (~1.9GB).

            Time-aligned sampling: for output tick t_rel the capture is
            advanced until it has consumed every source frame up to t_rel
            (frames_read tracks source time as frames_read / src_fps).
            Inference runs on the newest frame covering the tick — i.e. at
            (up to) every source frame, never time-stretched. The old
            sequential 15fps-bucket read consumed source frames at half rate
            on 30fps sources, so the matte lagged the subject by up to half
            the window — the "text blinks every second" bug.

            With roi_frac, inference runs on that fractional crop of the
            frame and the result is pasted back into a full-frame mask (the
            small-subject refinement pass — everything outside the padded
            union of pass-1 detections is known background).
            """
            cap.set(cv2.CAP_PROP_POS_MSEC, window.start_s * 1000.0)
            num_output_frames = max(1, round((window.end_s - window.start_s) * MATTE_FPS))
            masks: list[np.ndarray] = []
            recent_soft: deque[np.ndarray] = deque(maxlen=_TEMPORAL_MEDIAN_FRAMES)
            frames_read = 0
            inferences = 0
            last: np.ndarray | None = None
            source_exhausted = False

            for i in range(num_output_frames):
                _budget_check(start_time)
                # Epsilon guards float floor error: (1/30)*30 == 0.999...,
                # which would silently halve the sampling rate on exact-30fps
                # sources.
                target_reads = int(i * src_fps / MATTE_FPS + 1e-6) + 1

                frame = None
                while frames_read < target_reads and not source_exhausted:
                    ok, next_frame = cap.read()
                    if not ok:
                        source_exhausted = True
                        break
                    frame = next_frame
                    frames_read += 1

                if frame is not None:
                    if roi_frac is None:
                        rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
                    else:
                        fx0, fx1, fy0, fy1 = roi_frac
                        fh, fw = frame.shape[:2]
                        x0, x1 = int(fx0 * fw), max(int(fx0 * fw) + 1, int(fx1 * fw))
                        y0, y1 = int(fy0 * fh), max(int(fy0 * fh) + 1, int(fy1 * fh))
                        rgb = cv2.cvtColor(
                            np.ascontiguousarray(frame[y0:y1, x0:x1]), cv2.COLOR_BGR2RGB
                        )
                    mp_image = mp.Image(image_format=mp.ImageFormat.SRGB, data=rgb)
                    mask = segmenter.segment(mp_image).confidence_masks[0].numpy_view().copy()
                    inferences += 1
                    if roi_frac is None:
                        soft = cv2.resize(
                            mask,
                            (_MATTE_WIDTH, _MATTE_HEIGHT),
                            interpolation=cv2.INTER_LINEAR,
                        )
                    else:
                        fx0, fx1, fy0, fy1 = roi_frac
                        mx0 = int(fx0 * _MATTE_WIDTH)
                        mx1 = max(mx0 + 1, int(fx1 * _MATTE_WIDTH))
                        my0 = int(fy0 * _MATTE_HEIGHT)
                        my1 = max(my0 + 1, int(fy1 * _MATTE_HEIGHT))
                        soft = np.zeros((_MATTE_HEIGHT, _MATTE_WIDTH), dtype=np.float32)
                        soft[my0:my1, mx0:mx1] = cv2.resize(
                            mask, (mx1 - mx0, my1 - my0), interpolation=cv2.INTER_LINEAR
                        )
                    recent_soft.append(soft)
                    last = np.clip(_postprocess_mask(recent_soft) * 255.0, 0, 255).astype(np.uint8)
                elif last is None:
                    # Video shorter than the window — nothing usable yet.
                    break
                # else: video exhausted mid-window (or a sub-src_fps tick);
                # hold the last treated mask.

                if last is None:
                    break
                masks.append(last)
            return masks, inferences

        for window in windows:
            _budget_check(start_time)
            if window.end_s <= window.start_s:
                log.warning(
                    "subject_matte_skipping_empty_window",
                    start_s=window.start_s,
                    end_s=window.end_s,
                )
                continue

            treated, inferences = collect_window_masks(window, None)
            if not treated:
                continue

            roi = _small_subject_roi(treated)
            if roi is not None:
                roi_treated, roi_inferences = collect_window_masks(window, roi)
                if roi_treated:
                    treated = roi_treated
                    inferences += roi_inferences
                    log.info(
                        "subject_matte_roi_refined",
                        start_s=window.start_s,
                        end_s=window.end_s,
                        roi=[round(v, 3) for v in roi],
                    )

            frame_count += inferences
            prev_present: bool | None = None
            produced = 0
            for treated_mask in treated:
                mean_frac = float(np.mean(treated_mask)) / 255.0
                coverages.append(mean_frac)
                present = mean_frac >= _PRESENCE_COVERAGE_FLOOR
                if prev_present is not None and present != prev_present:
                    presence_flips += 1
                prev_present = present
                proc.stdin.write(treated_mask.tobytes())
                produced += 1

            if produced == 0:
                continue
            total_produced += produced
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
        presence_flips=presence_flips,
        presence_flips_per_s=presence_flips / (total_produced / MATTE_FPS)
        if total_produced
        else 0.0,
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
