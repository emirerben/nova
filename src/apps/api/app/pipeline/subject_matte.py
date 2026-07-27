"""Subject matte engine — per-frame person-segmentation masks for text occlusion.

Feeds the "text behind subject" effect: a low-resolution grayscale matte
video, one frame per rendered output tick, where pixel value ~= probability
that a person occupies that pixel. Downstream text-burn steps multiply
overlay alpha by the (upscaled) mask so the subject appears in front of text.

Best-effort by design: every public function degrades to ``None`` on any
failure (missing model, unreadable video, mediapipe not installed, wall-clock
budget blown, corrupt matte file) and never raises. A matte failure must
never fail a render job — it just means the occlusion effect is skipped.

Segmentation backbone: RobustVideoMatting (rvm_mobilenetv3, onnxruntime CPU,
recurrent — temporally stable by construction) is primary; MediaPipe's
ImageSegmenter (selfie segmenter, stateless IMAGE mode) is the fallback when
RVM is disabled (``MATTE_RVM_ENABLED=false``) or unavailable. The selfie
segmenter's confidence oscillates en masse on person-adjacent textures (beach
sand/rock read like skin over ~5–9 frame periods — prod job add80a9c), which
no per-frame treatment can hide; RVM's recurrence eliminates it (measured
median adjacent-frame IoU 0.980 vs area flapping 7%↔63% on the same footage).
Masks are sampled time-aligned at (up to) every source frame, temporally
median-filtered over 3 samples, hard-cut at 0.40 confidence, cleaned of tiny
fragments, and lightly feathered — the "solid object" treatment. The stored
matte already carries this treatment, so ``mask_at`` readers and both text
renderers stay treatment-agnostic. ``mediapipe`` and ``onnxruntime`` are
imported lazily inside functions so this module can be imported without them
installed (the structural eval-CI constraint other lazy-imported pipeline
deps share).

Recurrent state and the temporal median are RESET at known hard-cut
boundaries (``cut_boundaries_s``, montage slot joins passed in by the
orchestrator) so a clip's silhouette never bleeds into the next clip —
without the reset, ~2 frames of the previous clip's mask occlude text at
every cut.

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
# RobustVideoMatting mobilenetv3 (ONNX, GPL-3.0 weights — server-side use
# only, never distributed; see agents/DECISIONS.md).
MATTE_RVM_MODEL_PATH = "assets/models/rvm_mobilenetv3_fp32.onnx"
# Frames are pre-downscaled to this fraction of source resolution (natural
# aspect preserved) and fed with downsample_ratio=1.0. Feeding full-res
# frames with downsample_ratio=0.25 runs the SAME encoder resolution but
# makes RVM's guided-filter refiner upsample pha (and a never-used fgr) back
# to full res — ~25MB float32 tensors per frame, all discarded by the
# 270x480 store resize. Pre-downscaling skips that entirely.
_RVM_INPUT_SCALE = 0.25
# ORT threading: pin intra-op threads and disable spin-waiting — the worker
# shares 4 Fly vCPUs with the ffmpeg mux subprocess; default all-cores
# spinning starves everything else on the machine. 3 threads measured 30fps
# inference-only on M-series (2 threads: 22fps; 4: 32fps — diminishing and
# it would leave nothing for the mux).
_RVM_INTRA_OP_THREADS = 3
# RVM inference budget guard: windows totalling more than this many output
# ticks fall back to the mediapipe backbone (the new stability gates still
# protect quality). Measured (pre-downscaled input, 3 pinned threads,
# M-series): ~30fps inference, ~20fps end-to-end with decode; assume Fly
# shared vCPUs ~half that (~10fps end-to-end) → 900 ticks ≈ 90s — the edge
# of the budget. Typical intro windows are 300-400 ticks.
_RVM_MAX_TOTAL_TICKS = 900
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
    # Shape-stability signal: median IoU of the binarized mask across
    # consecutive present-frame pairs (within windows, never across window
    # boundaries). Presence flips catch a subject blinking in/out; this
    # catches a silhouette that never disappears but wobbles violently
    # frame to frame — occlusion registered to a shape that won't hold
    # still reads as glitching. None when too few pairs to judge.
    shape_stability_iou: float | None = None
    iou_pair_count: int = 0
    # Oscillation signal: adjacent present-pair IoU below _LARGE_JUMP_IOU is
    # one "large jump" — the silhouette teleported between consecutive
    # frames. The MEDIAN IoU gate above is blind to multi-frame oscillation
    # (prod job add80a9c: mask area flapping 7%↔63% every ~5–9 frames kept
    # median IoU 0.927 because ~15 jump pairs hid among 308 stable ones);
    # counting the jumps directly is not. Boundary-crossing pairs at known
    # hard cuts are excluded, so legit montage cuts don't inflate the count.
    large_jump_count: int = 0
    large_jumps_per_s: float = 0.0


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
# Shape-stability gate: a median adjacent-frame IoU below this means the
# typical consecutive mask pair shares under 40% of its area — violent shape
# instability no downstream treatment can hide. Real subjects at 30fps keep
# adjacent-frame IoU well above 0.7 even in fast motion, and the *median* is
# immune to isolated scene cuts (the Argentina-montage anchor keeps its
# single cut pair out of the median). Applied only when enough present
# pairs exist to judge.
_MIN_SHAPE_STABILITY_IOU = 0.40
_MIN_IOU_PAIRS = 5
# Oscillation gate on the large-jump stats above. The rate is a FRACTION of
# present adjacent pairs, not jumps-per-second — a per-second rate dilutes
# linearly with window length, so the beach strobe (15 jumps in an 11s
# matte, prod job add80a9c) would pass inside a 60s hold-to-EOF window.
# Measured anchors: beach matte (RVM-era mediapipe control) 48/307 ≈ 15.6%
# → reject; original beach matte ≈ 15/308 ≈ 4.9% → reject; stable footage
# ≈ 0; a single whip-pan in a 10s window ≈ 1-2/300 < 1% → keep. AND-gate
# (count + fraction) mirrors the presence-flip gate so short windows with a
# couple of abrupt legit events can't trip it.
_LARGE_JUMP_IOU = 0.50
_MAX_LARGE_JUMPS = 3
_MAX_LARGE_JUMP_FRAC = 0.02


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
    if (
        stats.shape_stability_iou is not None
        and stats.shape_stability_iou < _MIN_SHAPE_STABILITY_IOU
    ):
        return False
    if stats.large_jump_count > _MAX_LARGE_JUMPS and stats.large_jump_count > (
        _MAX_LARGE_JUMP_FRAC * max(1, stats.iou_pair_count)
    ):
        return False
    return True


def _resolve_asset_path(rel_path: str) -> str:
    """An assets/ path resolved relative to the api app root."""
    return os.path.normpath(os.path.join(os.path.dirname(__file__), "..", "..", rel_path))


def _resolve_model_path() -> str:
    """MATTE_MODEL_PATH resolved relative to the api app root.

    Reads the module-level constant fresh on every call (not cached) so
    tests can monkeypatch ``MATTE_MODEL_PATH`` to point at a missing file.
    """
    return _resolve_asset_path(MATTE_MODEL_PATH)


# More hint entries than this and the hint list is ignored wholesale: a
# flooded list (boundary per tick) would exclude every pair from the
# stability stats and degrade the recurrent backbone to stateless.
_MAX_CUT_BOUNDARIES = 60


def _window_boundary_ticks(window: MatteWindow, cut_boundaries_s: list[float] | None) -> set[int]:
    """Output-tick indices inside ``window`` where a source hard cut lands.

    Best-effort: garbage entries (non-numeric, out of window, unsorted) are
    simply ignored; an implausibly long list (> _MAX_CUT_BOUNDARIES) is
    ignored wholesale. Slot durations are rounded to 3dp upstream and CFR
    normalization can shift the encoded cut by ±1 frame — a reset one frame
    early/late still removes the cross-clip ghost.
    """
    if not cut_boundaries_s or len(cut_boundaries_s) > _MAX_CUT_BOUNDARIES:
        return set()
    ticks: set[int] = set()
    for b in cut_boundaries_s:
        try:
            t = float(b)
        except (TypeError, ValueError):
            continue
        if window.start_s < t < window.end_s:
            ticks.add(int(round((t - window.start_s) * MATTE_FPS)))
    return ticks


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
    cut_boundaries_s: list[float] | None = None,
) -> MatteStats | None:
    """Compute a person-segmentation matte for ``windows`` of ``video_path``.

    Writes a grayscale H.264 mp4 (windows concatenated back-to-back, no
    gaps for the time between windows) to ``out_path`` at MATTE_FPS, plus a
    sidecar JSON at ``out_path + ".json"``. Returns MatteStats on success,
    ``None`` on any failure — never raises.

    ``cut_boundaries_s``: output-timeline timestamps of hard cuts in
    ``video_path`` (montage slot joins). Best-effort hint: resets the
    temporal median + backbone recurrence at each cut and excludes the
    crossing frame-pair from stability stats. ``None``/garbage tolerated.
    """
    start_time = time.monotonic()
    try:
        stats = _compute_subject_matte_inner(
            video_path, windows, out_path, start_time, cut_boundaries_s=cut_boundaries_s
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


class _RvmBackbone:
    """RobustVideoMatting via onnxruntime — recurrent, temporally stable.

    ``infer`` takes an RGB uint8 frame and returns a float32 [0,1] soft
    alpha at the DOWNSCALED inference resolution (natural aspect,
    ``_RVM_INPUT_SCALE`` of the source — the caller resizes to matte
    storage). The frame is pre-downscaled here and fed with
    ``downsample_ratio=1.0``: same encoder resolution as full-res +
    ratio-0.25, minus the discarded full-res guided-filter/fgr work.
    The four recurrent states carry frame to frame; ``reset()`` zeroes
    them (window starts and hard-cut boundaries — state must never bleed
    across a cut).
    """

    kind = "rvm"

    def __init__(self, session: object) -> None:
        self._session = session
        self._rec: list[np.ndarray] = []
        self.reset()

    def reset(self) -> None:
        self._rec = [np.zeros([1, 1, 1, 1], dtype=np.float32) for _ in range(4)]

    def infer(self, rgb: np.ndarray) -> np.ndarray:
        h, w = rgb.shape[:2]
        small = cv2.resize(
            rgb,
            (max(2, round(w * _RVM_INPUT_SCALE)), max(2, round(h * _RVM_INPUT_SCALE))),
            interpolation=cv2.INTER_AREA,
        )
        src = np.ascontiguousarray((small.astype(np.float32) / 255.0).transpose(2, 0, 1)[None])
        # Named outputs: positional unpack would silently read the red
        # foreground channel as "pha" if a re-exported asset reordered the
        # graph; naming also lets ORT prune the unused fgr subgraph.
        pha, *rec = self._session.run(
            ["pha", "r1o", "r2o", "r3o", "r4o"],
            {
                "src": src,
                "r1i": self._rec[0],
                "r2i": self._rec[1],
                "r3i": self._rec[2],
                "r4i": self._rec[3],
                "downsample_ratio": np.array([1.0], dtype=np.float32),
            },
        )
        self._rec = list(rec)
        return np.asarray(pha[0, 0], dtype=np.float32)

    def close(self) -> None:
        self._session = None


class _MediapipeBackbone:
    """Stateless selfie-segmenter fallback (the pre-RVM path).

    IMAGE running mode — one independent segmentation per frame. VIDEO
    mode's internal temporal filter balloons and oscillates on busy footage
    (night crowd scenes); frame-to-frame stability comes from the temporal
    median in _postprocess_mask instead.
    """

    kind = "mediapipe"

    def __init__(self, mp_module: object, segmenter: object) -> None:
        self._mp = mp_module
        self._segmenter = segmenter

    def reset(self) -> None:  # stateless — nothing to reset
        pass

    def infer(self, rgb: np.ndarray) -> np.ndarray:
        mp_image = self._mp.Image(image_format=self._mp.ImageFormat.SRGB, data=rgb)
        return self._segmenter.segment(mp_image).confidence_masks[0].numpy_view().copy()

    def close(self) -> None:
        self._segmenter.close()


def _rvm_enabled() -> bool:
    try:
        from app.config import settings  # noqa: PLC0415

        return bool(getattr(settings, "matte_rvm_enabled", True))
    except Exception:  # noqa: BLE001 — config import must never break the matte
        return True


def _create_backbone(prefer_rvm: bool = True) -> _RvmBackbone | _MediapipeBackbone:
    """RVM when enabled/preferred and loadable, else the mediapipe fallback.

    Raises (any exception) only when NO backbone can be created —
    best-effort contract: the caller degrades to no occlusion, never a
    failed render. ``prefer_rvm=False`` forces the mediapipe path (used
    when the requested window total exceeds the RVM inference budget).
    """
    if prefer_rvm and _rvm_enabled():
        rvm_path = _resolve_asset_path(MATTE_RVM_MODEL_PATH)
        try:
            if not os.path.isfile(rvm_path):
                raise FileNotFoundError(rvm_path)
            import onnxruntime as ort  # noqa: PLC0415 — lazy: eval CI has no onnxruntime

            opts = ort.SessionOptions()
            # Pinned threads + no spin-wait: the worker shares 4 vCPUs with
            # the ffmpeg mux subprocess; ORT's all-cores spinning default
            # starves the box for the whole compute.
            opts.intra_op_num_threads = _RVM_INTRA_OP_THREADS
            opts.inter_op_num_threads = 1
            try:
                opts.add_session_config_entry("session.intra_op.allow_spinning", "0")
            except Exception:  # noqa: BLE001 — config entry name varies across ORT versions
                pass
            session = ort.InferenceSession(
                rvm_path, sess_options=opts, providers=["CPUExecutionProvider"]
            )
            return _RvmBackbone(session)
        except Exception as exc:  # noqa: BLE001 — fall back to mediapipe
            log.warning("subject_matte_rvm_unavailable", error=str(exc), model_path=rvm_path)

    model_path = _resolve_model_path()
    if not os.path.isfile(model_path):
        raise _MatteAbort(f"matte model not found at {model_path}")

    # Lazy import — module import must succeed without mediapipe installed.
    import mediapipe as mp  # noqa: PLC0415
    from mediapipe.tasks import python as mp_python  # noqa: PLC0415
    from mediapipe.tasks.python import vision as mp_vision  # noqa: PLC0415

    base_options = mp_python.BaseOptions(model_asset_path=model_path)
    options = mp_vision.ImageSegmenterOptions(
        base_options=base_options,
        running_mode=mp_vision.RunningMode.IMAGE,
        output_confidence_masks=True,
        output_category_mask=False,
    )
    return _MediapipeBackbone(mp, mp_vision.ImageSegmenter.create_from_options(options))


def _compute_subject_matte_inner(
    video_path: str,
    windows: list[MatteWindow],
    out_path: str,
    start_time: float,
    cut_boundaries_s: list[float] | None = None,
) -> MatteStats:
    if not windows:
        raise _MatteAbort("no windows requested")

    _budget_check(start_time)

    cap = cv2.VideoCapture(video_path)
    if not cap.isOpened():
        cap.release()
        raise _MatteAbort(f"cannot open video: {video_path}")

    _budget_check(start_time)

    # RVM budget guard: very long window totals (hold-to-EOF overlays) can't
    # finish RVM inference inside the wall-clock budget on prod vCPUs — fall
    # back to mediapipe up front instead of burning 90s and aborting.
    total_ticks = sum(
        max(1, round((w.end_s - w.start_s) * MATTE_FPS)) for w in windows if w.end_s > w.start_s
    )
    try:
        backbone = _create_backbone(prefer_rvm=total_ticks <= _RVM_MAX_TOTAL_TICKS)
    except Exception:
        cap.release()
        raise
    log.info("subject_matte_backbone", kind=backbone.kind, total_ticks=total_ticks)

    src_fps = _source_fps(cap)

    proc: subprocess.Popen | None = None
    written_windows: list[tuple[float, float]] = []
    coverages: list[float] = []
    frame_count = 0
    presence_flips = 0
    total_produced = 0
    iou_values: list[float] = []

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

            At every known hard-cut boundary tick the temporal-median deque
            AND the backbone's recurrent state are reset — median/recurrence
            across a cut occludes text with the PREVIOUS clip's silhouette
            for ~2 frames at every montage slot join.
            """
            cap.set(cv2.CAP_PROP_POS_MSEC, window.start_s * 1000.0)
            num_output_frames = max(1, round((window.end_s - window.start_s) * MATTE_FPS))
            boundary_ticks = _window_boundary_ticks(window, cut_boundaries_s)
            masks: list[np.ndarray] = []
            recent_soft: deque[np.ndarray] = deque(maxlen=_TEMPORAL_MEDIAN_FRAMES)
            frames_read = 0
            inferences = 0
            last: np.ndarray | None = None
            source_exhausted = False
            backbone.reset()

            cut_pending = False
            for i in range(num_output_frames):
                _budget_check(start_time)
                if i in boundary_ticks:
                    recent_soft.clear()
                    backbone.reset()
                    cut_pending = True
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
                    mask = backbone.infer(rgb)
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
                elif cut_pending:
                    # No fresh frame at the cut tick (sub-src_fps source or
                    # decode hiccup): never re-emit the PRE-cut silhouette —
                    # that ghost is exactly what the reset removes. Hold
                    # empty until a real post-cut frame arrives.
                    last = np.zeros_like(last)
                # else: video exhausted mid-window (or a sub-src_fps tick);
                # hold the last treated mask.

                if frame is not None:
                    cut_pending = False
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

            # Small-subject ROI refinement is a mediapipe-only workaround (its
            # ~256px model input loses distant subjects). RVM's downsample
            # ratio + recurrence keeps them stable full-frame — and a single
            # window-wide crop across a multi-clip montage would zero out any
            # clip whose subject sits outside it.
            roi = _small_subject_roi(treated) if backbone.kind == "mediapipe" else None
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
            # Pairs that straddle a known hard cut are legit discontinuities,
            # not segmenter instability — exclude them from every stability
            # stat so montage cuts can't inflate flip/jump counts. The
            # post-cut warmup tick is excluded too: its median is built from
            # 1-2 samples and settles on the next frame.
            cut_ticks = _window_boundary_ticks(window, cut_boundaries_s)
            stat_boundary_ticks = cut_ticks | {t + 1 for t in cut_ticks}
            prev_present: bool | None = None
            prev_binary: np.ndarray | None = None
            produced = 0
            for tick, treated_mask in enumerate(treated):
                at_cut = tick in stat_boundary_ticks
                mean_frac = float(np.mean(treated_mask)) / 255.0
                coverages.append(mean_frac)
                present = mean_frac >= _PRESENCE_COVERAGE_FLOOR
                if prev_present is not None and present != prev_present and not at_cut:
                    presence_flips += 1
                binary = treated_mask >= 128 if present else None
                if binary is not None and prev_binary is not None and not at_cut:
                    union = int(np.count_nonzero(binary | prev_binary))
                    if union > 0:
                        intersection = int(np.count_nonzero(binary & prev_binary))
                        iou_values.append(intersection / union)
                prev_present = present
                prev_binary = binary
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
        backbone.close()
        cap.release()
        if proc is not None and proc.poll() is None:
            proc.kill()
            proc.wait()

    if not written_windows or frame_count == 0:
        raise _MatteAbort("no matte frames produced")

    large_jump_count = sum(1 for v in iou_values if v < _LARGE_JUMP_IOU)
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
        shape_stability_iou=float(np.median(iou_values))
        if len(iou_values) >= _MIN_IOU_PAIRS
        else None,
        iou_pair_count=len(iou_values),
        large_jump_count=large_jump_count,
        large_jumps_per_s=large_jump_count / (total_produced / MATTE_FPS)
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
    Encoded lossless (-qp 0): the mask carries hard-cut edges with a thin
    feather, and default-CRF quantization rings along the silhouette
    differently on every frame — a per-frame edge shimmer the downstream
    occlusion multiply makes visible.
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
        "-qp",
        "0",
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

        # A mixed pair (matte blob from one burn, sidecar from another —
        # non-atomic uploads at a deterministic key) serves silently shifted
        # masks if reconciled; the writer emits exactly the sidecar's frame
        # total, so any real mismatch means corruption. Reject; the resolver
        # treats an unopenable cache as a miss and recomputes.
        expected_total = sum(max(1, round((e - st) * fps)) for st, e in raw_windows)
        if abs(len(frames) - expected_total) > 2:
            log.warning(
                "subject_matte_sidecar_frame_mismatch",
                matte_path=matte_path,
                expected=expected_total,
                actual=len(frames),
            )
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

    def window_spans(self) -> list[tuple[float, float]]:
        """Stored [start_s, end_s] spans — lets callers check whether a
        cached matte covers the windows a burn is about to request."""
        return [(w.start_s, w.end_s) for w in self._windows]

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
        # Half-up, not round(): banker's rounding at a constant half-frame
        # offset (a 0.25s window pad = 7.5 frames) makes the index sequence
        # repeat-then-skip (8, 8, 10, 10, ...) — a 15fps judder of the mask
        # edge against 30fps text. floor(x + 0.5) keeps it monotonic with
        # +1 steps for any constant fractional offset.
        offset = int(np.floor((t_clamped - window.start_s) * self._fps + 0.5))
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
