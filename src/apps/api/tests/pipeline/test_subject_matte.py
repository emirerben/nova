"""Unit + integration tests for pipeline/subject_matte.py.

Fixture videos/mattes are generated at runtime (no committed media, per repo
.gitignore). Provider-side tests build a synthetic matte + sidecar directly
via the module's own ffmpeg writer, bypassing mediapipe entirely, so most of
this file runs without mediapipe installed. Only the real end-to-end smoke
test needs mediapipe + the downloaded selfie-segmenter model.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import numpy as np
import pytest

from app.pipeline import subject_matte
from app.pipeline.subject_matte import (
    MatteStats,
    MatteWindow,
    SubjectMatteProvider,
    compute_subject_matte,
    matte_is_sane,
)

# ---------------------------------------------------------------------------
# matte_is_sane — pure function, no fixtures.
# ---------------------------------------------------------------------------


def _stats(mean: float, min_: float = 0.0, max_: float = 0.5) -> MatteStats:
    return MatteStats(
        mean_coverage=mean, min_coverage=min_, max_coverage=max_, frame_count=10, windows=[]
    )


class TestMatteIsSane:
    def test_small_distant_subject_is_sane(self) -> None:
        # A person at ~0.8% of frame (beach wide shot) is a legitimate
        # subject — the old 5% mean floor disabled the effect for them.
        assert matte_is_sane(_stats(0.008, max_=0.3)) is True

    def test_tiny_mean_with_real_peak_is_sane(self) -> None:
        assert matte_is_sane(_stats(0.001, max_=0.05)) is True

    def test_never_found_anyone_is_degenerate(self) -> None:
        # max_coverage below 1% — the segmenter never confidently found a
        # person in any frame.
        assert matte_is_sane(_stats(0.001, max_=0.009)) is False

    def test_max_coverage_at_floor(self) -> None:
        assert matte_is_sane(_stats(0.005, max_=0.01)) is True

    def test_swallowed_frame_is_degenerate(self) -> None:
        # mean coverage above 85% — the mask ate essentially the whole
        # frame; occluding text with it would just hide the text.
        assert matte_is_sane(_stats(0.86, max_=1.0)) is False

    def test_mean_at_ceiling(self) -> None:
        assert matte_is_sane(_stats(0.85, max_=1.0)) is True

    def test_unstable_presence_is_rejected(self) -> None:
        # Beach-wide-shot anchor: segmenter dropouts flap the mask on/off
        # (5 flips at 1.56/s measured) — occlusion would blink, reject.
        stats = _stats(0.007, max_=0.3)
        stats.presence_flips = 5
        stats.presence_flips_per_s = 1.56
        assert matte_is_sane(stats) is False

    def test_legit_scene_cut_is_not_rejected(self) -> None:
        # Argentina-montage anchor: one flip at a scene cut (0.29/s) is fine.
        stats = _stats(0.14, max_=0.4)
        stats.presence_flips = 1
        stats.presence_flips_per_s = 0.29
        assert matte_is_sane(stats) is True

    def test_few_flips_at_high_rate_is_not_rejected(self) -> None:
        # A very short window can have a high flip RATE with only 1-2 flips
        # (e.g. subject steps out once) — both conditions must trip.
        stats = _stats(0.05, max_=0.3)
        stats.presence_flips = 2
        stats.presence_flips_per_s = 2.0
        assert matte_is_sane(stats) is True

    def test_many_slow_flips_is_not_rejected(self) -> None:
        # Long window, occasional legitimate exits/entries — rate stays low.
        stats = _stats(0.05, max_=0.3)
        stats.presence_flips = 5
        stats.presence_flips_per_s = 0.2
        assert matte_is_sane(stats) is True

    def test_oscillating_shape_rejected(self) -> None:
        # Beach-montage anchor (prod job add80a9c): the selfie segmenter's
        # confidence on sand/rock oscillated en masse every ~5-9 frames, area
        # flapping 7%<->63%. The presence gate saw only 4 flips at 0.365/s
        # (below the AND-gate) and the MEDIAN IoU stayed 0.927 because ~15
        # jump pairs hid among 308 stable ones — this matte shipped and the
        # burned text visibly strobed. The large-jump gate is the stat that
        # actually catches it.
        stats = _stats(0.123, max_=0.671)
        stats.presence_flips = 4
        stats.presence_flips_per_s = 0.365
        stats.shape_stability_iou = 0.927
        stats.iou_pair_count = 308
        stats.large_jump_count = 15
        stats.large_jumps_per_s = 1.34
        assert matte_is_sane(stats) is False

    def test_few_jumps_at_high_fraction_kept(self) -> None:
        # AND-gate: a short window with a couple of abrupt legit events (an
        # in-window whip-pan) has a high pair-FRACTION but a count at the
        # threshold — both clauses must trip.
        stats = _stats(0.1, max_=0.4)
        stats.large_jump_count = 3
        stats.iou_pair_count = 30  # 10% of pairs, but only 3 events
        assert matte_is_sane(stats) is True

    def test_many_jumps_at_low_fraction_kept(self) -> None:
        # Long window, occasional hard events — the FRACTION stays low even
        # though the raw count exceeds the threshold. This is why the gate
        # normalizes by pair count and not seconds: a per-second rate would
        # also dilute the beach strobe inside a 60s hold-to-EOF window.
        stats = _stats(0.1, max_=0.4)
        stats.large_jump_count = 6
        stats.iou_pair_count = 600  # 1% of pairs
        assert matte_is_sane(stats) is True

    def test_beach_strobe_in_long_window_still_rejected(self) -> None:
        # The prod strobe density (15 jumps / 308 pairs ≈ 4.9%) must reject
        # regardless of how long the surrounding window is — scaled to a 60s
        # window the same density is 82/1770, still 4.6% of pairs.
        stats = _stats(0.1, max_=0.4)
        stats.large_jump_count = 82
        stats.iou_pair_count = 1770
        assert matte_is_sane(stats) is False

    def test_legacy_stats_without_jump_fields_kept(self) -> None:
        # Old sidecars predate the jump stats — dataclass defaults (0) must
        # never reject what the stat cannot judge.
        stats = _stats(0.1, max_=0.4)
        assert stats.large_jump_count == 0
        assert matte_is_sane(stats) is True


# ---------------------------------------------------------------------------
# _postprocess_mask — pure numpy/cv2, no fixtures, no mediapipe.
# ---------------------------------------------------------------------------


def _soft(value: float, shape: tuple[int, int] = (48, 27)) -> np.ndarray:
    return np.full(shape, value, dtype=np.float32)


class TestPostprocessMask:
    def test_hard_cut_below_threshold_is_background(self) -> None:
        from collections import deque

        out = subject_matte._postprocess_mask(deque([_soft(0.39)]))
        assert float(out.max()) == 0.0

    def test_hard_cut_above_threshold_is_solid(self) -> None:
        from collections import deque

        out = subject_matte._postprocess_mask(deque([_soft(0.41)]))
        # Interior is fully solid — the soft 0.41 confidence does not leak
        # into alpha as 41% ghosting.
        assert float(out[24, 13]) == pytest.approx(1.0, abs=1e-3)

    def test_temporal_median_suppresses_single_frame_spike(self) -> None:
        from collections import deque

        spike = deque([_soft(0.0), _soft(0.9), _soft(0.0)], maxlen=3)
        out = subject_matte._postprocess_mask(spike)
        assert float(out.max()) == 0.0

    def test_tiny_fragment_dropped_large_subject_kept(self) -> None:
        from collections import deque

        mask = np.zeros((480, 270), dtype=np.float32)
        # Large subject: ~0.8% of frame (the beach person) — must survive.
        mask[100:140, 100:126] = 0.9  # 40*26 = 1040 px ≈ 0.8%
        # Tiny fragment: well under the 0.2% floor — must be dropped.
        mask[300:306, 50:56] = 0.9  # 36 px ≈ 0.03%
        out = subject_matte._postprocess_mask(deque([mask]))
        assert float(out[120, 113]) > 0.9
        assert float(out[303, 53]) == 0.0

    def test_output_range_and_dtype(self) -> None:
        from collections import deque

        mask = np.zeros((480, 270), dtype=np.float32)
        mask[100:200, 80:180] = 1.0
        out = subject_matte._postprocess_mask(deque([mask]))
        assert out.shape == mask.shape
        assert float(out.min()) >= 0.0
        assert float(out.max()) <= 1.0
        # Feather produces intermediate values at the edge, solid interior.
        assert float(out[150, 130]) == pytest.approx(1.0, abs=1e-3)


# ---------------------------------------------------------------------------
# compute_subject_matte — best-effort None-return scenarios.
# All three short-circuit before touching mediapipe, so no skip needed.
# ---------------------------------------------------------------------------


class TestComputeSubjectMatteNeverRaises:
    def test_model_missing_returns_none(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            subject_matte, "MATTE_MODEL_PATH", "assets/models/does_not_exist.tflite"
        )
        result = compute_subject_matte(
            str(tmp_path / "nonexistent_video.mp4"),
            [MatteWindow(0.0, 1.0)],
            str(tmp_path / "out.mp4"),
        )
        assert result is None
        assert not (tmp_path / "out.mp4").exists()

    def test_video_unreadable_returns_none(self, tmp_path: Path) -> None:
        # Model resolves to the real downloaded asset; the video path is
        # what's broken here.
        result = compute_subject_matte(
            str(tmp_path / "not_a_video.mp4"),
            [MatteWindow(0.0, 1.0)],
            str(tmp_path / "out.mp4"),
        )
        assert result is None
        assert not (tmp_path / "out.mp4").exists()

    def test_budget_exceeded_returns_none(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls = {"n": 0}

        def fake_monotonic() -> float:
            calls["n"] += 1
            return 0.0 if calls["n"] == 1 else subject_matte.MATTE_WALL_CLOCK_BUDGET_S + 10.0

        monkeypatch.setattr(subject_matte.time, "monotonic", fake_monotonic)
        result = compute_subject_matte(
            str(tmp_path / "irrelevant.mp4"),
            [MatteWindow(0.0, 1.0)],
            str(tmp_path / "out.mp4"),
        )
        assert result is None
        assert calls["n"] >= 2

    def test_empty_windows_returns_none(self, tmp_path: Path) -> None:
        result = compute_subject_matte(
            str(tmp_path / "irrelevant.mp4"), [], str(tmp_path / "out.mp4")
        )
        assert result is None


# ---------------------------------------------------------------------------
# SubjectMatteProvider — synthetic matte fixture built via the module's own
# ffmpeg writer (_spawn_matte_writer), no mediapipe required.
# ---------------------------------------------------------------------------


def _ffmpeg_available() -> bool:
    return shutil.which("ffmpeg") is not None


needs_ffmpeg = pytest.mark.skipif(not _ffmpeg_available(), reason="ffmpeg not installed")

# The matte is H.264-encoded (lossy) even though source frames are written as
# flat uint8 values — libx264 ultrafast quantization drifts written values by
# ~2/255 on round-trip (empirically verified: 5 -> 3, 80 -> 77, 150 -> 148).
# Value-identity assertions below use this tolerance instead of exact match.
_ENCODE_TOLERANCE = 0.02


def _build_synthetic_matte(
    tmp_path: Path,
    windows: list[tuple[float, float]],
    fps: int = subject_matte.MATTE_FPS,
) -> Path:
    """Write a matte mp4 + sidecar directly, mirroring compute_subject_matte's
    own writer, with each frame holding a distinct known value (5, 10, 15, ...)
    so tests can assert exactly which stored frame a given t_abs resolved to."""
    out_path = tmp_path / "matte.mp4"
    proc = subject_matte._spawn_matte_writer(str(out_path))
    assert proc.stdin is not None

    written_windows: list[list[float]] = []
    frame_index = 0
    for start_s, end_s in windows:
        n = max(1, round((end_s - start_s) * fps))
        for _ in range(n):
            value = min(250, (frame_index + 1) * 5)
            frame = np.full(
                (subject_matte._MATTE_HEIGHT, subject_matte._MATTE_WIDTH), value, dtype=np.uint8
            )
            proc.stdin.write(frame.tobytes())
            frame_index += 1
        written_windows.append([start_s, end_s])

    # communicate() sends EOF on stdin itself; closing it first makes the
    # flush inside communicate() raise "flush of closed file" on py3.11.
    _, stderr = proc.communicate(timeout=30)
    assert proc.returncode == 0, stderr.decode(errors="replace")

    sidecar = {
        "windows": written_windows,
        "fps": fps,
        "size": [subject_matte._MATTE_WIDTH, subject_matte._MATTE_HEIGHT],
        "stats": {},
    }
    (tmp_path / "matte.mp4.json").write_text(json.dumps(sidecar))
    return out_path


class TestSubjectMatteProviderOpen:
    def test_missing_file_returns_none(self, tmp_path: Path) -> None:
        assert SubjectMatteProvider.open(str(tmp_path / "nope.mp4")) is None

    def test_missing_sidecar_returns_none(self, tmp_path: Path) -> None:
        video_path = tmp_path / "matte.mp4"
        video_path.write_bytes(b"not a real video")
        assert SubjectMatteProvider.open(str(video_path)) is None

    def test_corrupt_video_returns_none(self, tmp_path: Path) -> None:
        video_path = tmp_path / "matte.mp4"
        video_path.write_bytes(b"garbage, not an mp4 at all")
        (tmp_path / "matte.mp4.json").write_text(
            json.dumps({"windows": [[0.0, 1.0]], "fps": 30, "size": [270, 480], "stats": {}})
        )
        assert SubjectMatteProvider.open(str(video_path)) is None

    def test_corrupt_sidecar_json_returns_none(self, tmp_path: Path) -> None:
        video_path = tmp_path / "matte.mp4"
        video_path.write_bytes(b"also not a real video")
        (tmp_path / "matte.mp4.json").write_text("{not valid json")
        assert SubjectMatteProvider.open(str(video_path)) is None

    @needs_ffmpeg
    def test_open_succeeds_on_synthetic_matte(self, tmp_path: Path) -> None:
        matte_path = _build_synthetic_matte(tmp_path, [(0.0, 1.0)])
        provider = SubjectMatteProvider.open(str(matte_path))
        assert provider is not None


@needs_ffmpeg
class TestMaskAt:
    def test_shape_dtype_range(self, tmp_path: Path) -> None:
        matte_path = _build_synthetic_matte(tmp_path, [(0.0, 1.0)])
        provider = SubjectMatteProvider.open(str(matte_path))
        assert provider is not None

        mask = provider.mask_at(0.5)
        assert mask is not None
        assert mask.shape == (1920, 1080)
        assert mask.dtype == np.float32
        assert float(mask.min()) >= 0.0
        assert float(mask.max()) <= 1.0

    def test_out_of_window_returns_none(self, tmp_path: Path) -> None:
        matte_path = _build_synthetic_matte(tmp_path, [(0.0, 1.0)])
        provider = SubjectMatteProvider.open(str(matte_path))
        assert provider is not None
        assert provider.mask_at(5.0) is None
        assert provider.mask_at(-5.0) is None

    def test_clamps_at_window_start_edge(self, tmp_path: Path) -> None:
        matte_path = _build_synthetic_matte(tmp_path, [(0.0, 1.0)])
        provider = SubjectMatteProvider.open(str(matte_path))
        assert provider is not None
        # Just outside the window but within the small edge tolerance —
        # clamps to the first stored frame (value 5).
        mask = provider.mask_at(-0.01)
        assert mask is not None
        assert np.allclose(mask, 5.0 / 255.0, atol=_ENCODE_TOLERANCE)

    def test_clamps_at_window_end_edge(self, tmp_path: Path) -> None:
        matte_path = _build_synthetic_matte(tmp_path, [(0.0, 1.0)])
        provider = SubjectMatteProvider.open(str(matte_path))
        assert provider is not None
        # window is [0, 1.0) at 30fps => 30 frames, values 5..150, last=150.
        mask = provider.mask_at(1.03)
        assert mask is not None
        assert np.allclose(mask, 150.0 / 255.0, atol=_ENCODE_TOLERANCE)

    def test_second_window_offset_indexing(self, tmp_path: Path) -> None:
        # Two windows concatenated back-to-back — mask_at on the second
        # window must resolve into the correct offset region of the file,
        # not restart from frame 0.
        matte_path = _build_synthetic_matte(tmp_path, [(0.0, 0.5), (10.0, 10.5)])
        provider = SubjectMatteProvider.open(str(matte_path))
        assert provider is not None

        # First window: 0.5s @ 30fps = 15 frames, values 5..75.
        first_window_mask = provider.mask_at(0.0)
        assert first_window_mask is not None
        assert np.allclose(first_window_mask, 5.0 / 255.0, atol=_ENCODE_TOLERANCE)

        # Second window starts at global frame index 15, value (15+1)*5=80.
        second_window_mask = provider.mask_at(10.0)
        assert second_window_mask is not None
        assert np.allclose(second_window_mask, 80.0 / 255.0, atol=_ENCODE_TOLERANCE)

        # Gap between windows (t=5.0) is outside both windows.
        assert provider.mask_at(5.0) is None

    def test_repeated_lookup_of_same_frame_is_consistent(self, tmp_path: Path) -> None:
        # No memoization by design (mask_at is called concurrently from a
        # ThreadPoolExecutor) — repeated lookups of the same t_abs must
        # still resolve to equal (freshly-resized) mask values.
        matte_path = _build_synthetic_matte(tmp_path, [(0.0, 1.0)])
        provider = SubjectMatteProvider.open(str(matte_path))
        assert provider is not None
        first = provider.mask_at(0.5)
        second = provider.mask_at(0.5)
        assert first is not None
        assert second is not None
        assert first is not second  # not cached, distinct arrays
        assert np.array_equal(first, second)

    def test_half_frame_offset_indices_monotonic(self, tmp_path: Path) -> None:
        """A constant half-frame offset between render ticks and the window
        start (the 0.25s pad = 7.5 frames) must resolve to a MONOTONIC mask
        index sequence with +1 steps. Python's banker's round() produced
        0, 2, 2, 4, 4, ... here — the mask repeated then skipped every other
        frame, a 15fps judder of the occlusion edge against 30fps text."""
        matte_path = _build_synthetic_matte(tmp_path, [(0.0, 1.0)])
        provider = SubjectMatteProvider.open(str(matte_path))
        assert provider is not None

        resolved_values = []
        for i in range(28):
            mask = provider.mask_at((i + 0.5) / 30.0)
            assert mask is not None
            resolved_values.append(float(mask[0, 0]) * 255.0)

        # Stored frame k holds value (k+1)*5 — recover indices from values.
        indices = [round(v / 5.0) - 1 for v in resolved_values]
        deltas = [b - a for a, b in zip(indices, indices[1:])]
        assert all(d == 1 for d in deltas), (
            f"mask index sequence not monotonic with +1 steps: {indices} — "
            "half-frame rounding regressed to repeat/skip"
        )


# ---------------------------------------------------------------------------
# Time-alignment regression — fake mediapipe injected via sys.modules, so it
# runs everywhere (no GL needed). Pins the fix for the "text blinks every
# second" prod bug: the old loop read source frames sequentially at a 15fps
# inference cadence, so on a 30fps source the matte content played at half
# speed and progressively lagged the real subject.
# ---------------------------------------------------------------------------


def _build_brightness_ramp_clip(out_path: Path, n_frames: int = 30, fps: int = 30) -> Path:
    """30fps clip where frame k is a flat gray of value k*8 — brightness
    identifies the source frame on the other side of the decode."""
    proc = subprocess.Popen(
        [
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
            "64x64",
            "-r",
            str(fps),
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
            str(out_path),
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    assert proc.stdin is not None
    for k in range(n_frames):
        proc.stdin.write(np.full((64, 64), k * 8, dtype=np.uint8).tobytes())
    _, stderr = proc.communicate(timeout=30)
    assert proc.returncode == 0, stderr.decode(errors="replace")
    return out_path


def _install_fake_mediapipe(
    monkeypatch: pytest.MonkeyPatch,
    calls: list[float],
    shapes: list[tuple[int, int]] | None = None,
    mask_value: np.ndarray | None = None,
    mask_fn: object | None = None,
) -> None:
    """Fake mediapipe module tree that records the input brightness (and
    optionally input shape) per segment() call and returns `mask_value`
    (an empty mask by default), or `mask_fn(call_index)` when given.

    Also points MATTE_RVM_MODEL_PATH at a missing file so the backbone
    selector deterministically falls back to this fake mediapipe path —
    these tests pin the mediapipe backbone regardless of whether
    onnxruntime + the real RVM model are present on the host."""
    import sys
    import types

    monkeypatch.setattr(subject_matte, "MATTE_RVM_MODEL_PATH", "assets/models/_missing_rvm.onnx")

    class _FakeImage:
        def __init__(self, image_format: object = None, data: np.ndarray | None = None) -> None:
            self.data = data

    class _FakeMask:
        def __init__(self, array: np.ndarray) -> None:
            self._array = array

        def numpy_view(self) -> np.ndarray:
            return self._array

    class _FakeResult:
        def __init__(self, array: np.ndarray) -> None:
            self.confidence_masks = [_FakeMask(array)]

    class _FakeSegmenter:
        def segment(self, image: _FakeImage) -> _FakeResult:
            assert image.data is not None
            index = len(calls)
            calls.append(float(image.data.mean()))
            if shapes is not None:
                shapes.append(image.data.shape[:2])
            if mask_fn is not None:
                return _FakeResult(mask_fn(index))
            if mask_value is not None:
                return _FakeResult(mask_value)
            return _FakeResult(np.zeros((16, 16), dtype=np.float32))

        def close(self) -> None:
            pass

    mp_mod = types.ModuleType("mediapipe")
    mp_mod.Image = _FakeImage  # type: ignore[attr-defined]
    mp_mod.ImageFormat = types.SimpleNamespace(SRGB="srgb")  # type: ignore[attr-defined]

    tasks_mod = types.ModuleType("mediapipe.tasks")
    python_mod = types.ModuleType("mediapipe.tasks.python")
    python_mod.BaseOptions = lambda **kwargs: types.SimpleNamespace(**kwargs)  # type: ignore[attr-defined]
    vision_mod = types.ModuleType("mediapipe.tasks.python.vision")
    vision_mod.ImageSegmenterOptions = lambda **kwargs: types.SimpleNamespace(**kwargs)  # type: ignore[attr-defined]
    vision_mod.RunningMode = types.SimpleNamespace(IMAGE="image")  # type: ignore[attr-defined]
    vision_mod.ImageSegmenter = types.SimpleNamespace(  # type: ignore[attr-defined]
        create_from_options=lambda options: _FakeSegmenter()
    )
    mp_mod.tasks = tasks_mod  # type: ignore[attr-defined]
    tasks_mod.python = python_mod  # type: ignore[attr-defined]
    python_mod.vision = vision_mod  # type: ignore[attr-defined]

    monkeypatch.setitem(sys.modules, "mediapipe", mp_mod)
    monkeypatch.setitem(sys.modules, "mediapipe.tasks", tasks_mod)
    monkeypatch.setitem(sys.modules, "mediapipe.tasks.python", python_mod)
    monkeypatch.setitem(sys.modules, "mediapipe.tasks.python.vision", vision_mod)


@needs_ffmpeg
class TestTimeAlignment:
    def test_30fps_source_sampled_at_every_frame(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        video_path = _build_brightness_ramp_clip(tmp_path / "ramp.mp4")
        calls: list[float] = []
        _install_fake_mediapipe(monkeypatch, calls)

        result = compute_subject_matte(
            str(video_path),
            [MatteWindow(0.0, 1.0)],
            str(tmp_path / "matte.mp4"),
        )
        assert result is not None

        # Full-rate: one inference per source frame. The old 15fps-bucket
        # loop made exactly 15 calls here and only ever saw frames 0..14.
        assert len(calls) == 30

        for k, brightness in enumerate(calls):
            # Brightness identifies the source frame: call k must see frame
            # k (value k*8), not frame k//2. Lossless encode, so tight tol.
            assert brightness == pytest.approx(k * 8, abs=3.0), (
                f"call {k} saw source frame ~{brightness / 8:.1f}, expected {k} "
                "— matte sampling is time-stretched again"
            )


class TestSmallSubjectRoiRefinement:
    def test_small_subject_triggers_roi_second_pass(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A small detected subject re-runs the window on a zoomed crop —
        the fix for segmenter dropouts on distant people (beach wide shot:
        full-frame peak confidence flapped 0.0->1.0->0.0; ROI-zoomed
        inference held 1.00 on every frame)."""
        video_path = _build_brightness_ramp_clip(tmp_path / "ramp.mp4")
        calls: list[float] = []
        shapes: list[tuple[int, int]] = []
        blob = np.zeros((16, 16), dtype=np.float32)
        blob[6:9, 6:9] = 0.9  # ~3.5% bbox after resize — a small subject
        _install_fake_mediapipe(monkeypatch, calls, shapes=shapes, mask_value=blob)

        result = compute_subject_matte(
            str(video_path),
            [MatteWindow(0.0, 1.0)],
            str(tmp_path / "matte.mp4"),
        )
        assert result is not None
        # Pass 1 (full frame) + pass 2 (ROI crop): 30 ticks each.
        assert len(calls) == 60
        assert result.frame_count == 60
        full = [sh for sh in shapes if sh == (64, 64)]
        crops = [sh for sh in shapes if sh != (64, 64)]
        assert len(full) == 30
        assert len(crops) == 30
        # The crop really is a zoom: strictly smaller than the frame.
        assert all(h < 64 and w < 64 for h, w in crops)

    def test_no_detection_skips_roi_pass(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        video_path = _build_brightness_ramp_clip(tmp_path / "ramp2.mp4")
        calls: list[float] = []
        _install_fake_mediapipe(monkeypatch, calls)  # empty masks
        result = compute_subject_matte(
            str(video_path),
            [MatteWindow(0.0, 1.0)],
            str(tmp_path / "matte.mp4"),
        )
        assert result is not None
        assert len(calls) == 30  # single pass only


class TestSmallSubjectRoiGeometry:
    def test_large_union_returns_none(self) -> None:
        big = np.full((480, 270), 255, dtype=np.uint8)
        assert subject_matte._small_subject_roi([big]) is None

    def test_empty_union_returns_none(self) -> None:
        empty = np.zeros((480, 270), dtype=np.uint8)
        assert subject_matte._small_subject_roi([empty]) is None

    def test_small_blob_returns_padded_clamped_fractions(self) -> None:
        m = np.zeros((480, 270), dtype=np.uint8)
        m[230:250, 125:145] = 255  # centered ~7% x ~4% blob
        roi = subject_matte._small_subject_roi([m])
        assert roi is not None
        fx0, fx1, fy0, fy1 = roi
        assert 0.0 <= fx0 < fx1 <= 1.0
        assert 0.0 <= fy0 < fy1 <= 1.0
        # Padded to at least the minimum crop side.
        assert fx1 - fx0 >= subject_matte._ROI_MIN_SIDE_FRAC - 1e-6
        assert fy1 - fy0 >= subject_matte._ROI_MIN_SIDE_FRAC - 1e-6
        # Blob center stays inside the crop.
        assert fx0 < 0.5 < fx1
        assert fy0 < 0.5 < fy1


# ---------------------------------------------------------------------------
# Real end-to-end smoke test — needs mediapipe + the downloaded model.
# ---------------------------------------------------------------------------


def _mediapipe_available() -> bool:
    try:
        import mediapipe  # noqa: F401, PLC0415
    except ImportError:
        return False
    return True


def _segmenter_usable() -> bool:
    """True when an ImageSegmenter can actually be created here.

    Importing mediapipe is not enough: GL-less hosts (e.g. the test-api CI
    runner, which lacks libGLESv2 — same constraint as the skia-free eval
    CI) import fine but fail at segmenter creation, making compute's
    best-effort None legitimate. Only environments that pass this probe
    (local dev, the prod Docker image) run the strict end-to-end assertion.
    """
    if not _mediapipe_available():
        return False
    try:
        from mediapipe.tasks import python as mp_python  # noqa: PLC0415
        from mediapipe.tasks.python import vision as mp_vision  # noqa: PLC0415

        from app.pipeline.subject_matte import _resolve_model_path  # noqa: PLC0415

        options = mp_vision.ImageSegmenterOptions(
            base_options=mp_python.BaseOptions(model_asset_path=_resolve_model_path()),
            running_mode=mp_vision.RunningMode.IMAGE,
            output_confidence_masks=True,
            output_category_mask=False,
        )
        mp_vision.ImageSegmenter.create_from_options(options).close()
    except Exception:  # noqa: BLE001 — any creation failure means unusable here
        return False
    return True


needs_mediapipe = pytest.mark.skipif(not _mediapipe_available(), reason="mediapipe not installed")
needs_segmenter = pytest.mark.skipif(
    not _segmenter_usable(), reason="mediapipe segmenter not usable on this host (no GL)"
)


def _build_testsrc_clip(out_path: Path, duration: float = 1.0) -> Path:
    cmd = [
        "ffmpeg",
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
        "-f",
        "lavfi",
        "-i",
        # 30fps like real prod sources. The original rate=15 exactly matched
        # the old inference cadence and hid the time-stretch bug in CI.
        f"testsrc=duration={duration}:size=320x568:rate=30",
        "-c:v",
        "libx264",
        "-preset",
        "ultrafast",
        "-pix_fmt",
        "yuv420p",
        str(out_path),
    ]
    subprocess.run(cmd, check=True, capture_output=True, timeout=30)
    return out_path


@needs_ffmpeg
@needs_segmenter
class TestComputeSubjectMatteEndToEnd:
    def test_real_clip_never_raises(self, tmp_path: Path) -> None:
        video_path = _build_testsrc_clip(tmp_path / "clip.mp4", duration=1.0)
        out_path = tmp_path / "matte.mp4"

        result = compute_subject_matte(
            str(video_path),
            [MatteWindow(0.0, 0.5)],
            str(out_path),
        )

        # This test only runs where a segmenter is creatable (needs_segmenter),
        # so a None result here would mask a real regression in the writer
        # path rather than a legitimate best-effort skip — assert success.
        assert result is not None and isinstance(result, MatteStats)
        assert out_path.exists()
        assert (tmp_path / "matte.mp4.json").exists()
        assert result.frame_count > 0


# ---------------------------------------------------------------------------
# Shape-stability gate (median adjacent-frame IoU) — pure function, no fixtures.
# ---------------------------------------------------------------------------


class TestShapeStabilityGate:
    def test_stable_shape_is_sane(self) -> None:
        stats = _stats(0.1, max_=0.4)
        stats.shape_stability_iou = 0.90
        stats.iou_pair_count = 100
        assert matte_is_sane(stats) is True

    def test_scene_cut_median_survives(self) -> None:
        # Argentina-montage anchor shape: one scene-cut pair with near-zero
        # IoU cannot drag the MEDIAN down when every other pair is stable.
        stats = _stats(0.14, max_=0.4)
        stats.presence_flips = 1
        stats.presence_flips_per_s = 0.29
        stats.shape_stability_iou = float(np.median([0.05] + [0.88] * 60))
        stats.iou_pair_count = 61
        assert matte_is_sane(stats) is True

    def test_violently_unstable_shape_rejected(self) -> None:
        # Silhouette never disappears (0 presence flips) but the typical
        # adjacent-frame pair shares under 40% of its area — occlusion
        # registered to a shape that won't hold still reads as glitching.
        stats = _stats(0.1, max_=0.4)
        stats.presence_flips = 0
        stats.shape_stability_iou = 0.30
        assert matte_is_sane(stats) is False

    def test_threshold_boundary_is_kept(self) -> None:
        stats = _stats(0.1, max_=0.4)
        stats.shape_stability_iou = subject_matte._MIN_SHAPE_STABILITY_IOU
        assert matte_is_sane(stats) is True

    def test_missing_iou_stat_is_ignored(self) -> None:
        # Old sidecars (and short mattes with < _MIN_IOU_PAIRS pairs) carry
        # no IoU stat — the gate must not reject what it cannot judge.
        stats = _stats(0.1, max_=0.4)
        assert stats.shape_stability_iou is None
        assert matte_is_sane(stats) is True


@needs_ffmpeg
class TestShapeStabilityStatCompute:
    def test_sidecar_carries_shape_stability_iou(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A steady synthetic subject yields IoU 1.0 across every adjacent
        pair, persisted in the sidecar stats for the sanity gate."""
        video_path = _build_brightness_ramp_clip(tmp_path / "ramp.mp4")
        calls: list[float] = []
        # Constant confident blob → identical treated mask every frame.
        blob = np.zeros((16, 16), dtype=np.float32)
        blob[4:12, 4:12] = 0.95
        _install_fake_mediapipe(monkeypatch, calls, mask_value=blob)

        out_path = tmp_path / "matte.mp4"
        result = compute_subject_matte(str(video_path), [MatteWindow(0.0, 1.0)], str(out_path))
        assert result is not None
        assert result.shape_stability_iou == pytest.approx(1.0)
        assert result.iou_pair_count == 29  # 30 output ticks -> 29 adjacent pairs

        sidecar = json.loads((tmp_path / "matte.mp4.json").read_text())
        assert sidecar["stats"]["shape_stability_iou"] == pytest.approx(1.0)
        assert sidecar["stats"]["iou_pair_count"] == 29


# ---------------------------------------------------------------------------
# Lossless matte intermediate encode (-qp 0).
# ---------------------------------------------------------------------------


class TestMatteWriterLossless:
    def test_writer_cmd_pins_lossless_qp(self, monkeypatch: pytest.MonkeyPatch) -> None:
        seen: dict = {}

        def _fake_popen(cmd, **kwargs):  # noqa: ANN001
            seen["cmd"] = cmd
            raise RuntimeError("stop before spawning")

        monkeypatch.setattr(subject_matte.subprocess, "Popen", _fake_popen)
        with pytest.raises(RuntimeError):
            subject_matte._spawn_matte_writer("/tmp/out.mp4")
        cmd = seen["cmd"]
        qp_idx = cmd.index("-qp")
        assert cmd[qp_idx + 1] == "0", "matte intermediate must encode lossless"

    @needs_ffmpeg
    def test_hard_edge_roundtrip_has_no_ringing(self, tmp_path: Path) -> None:
        """The matte carries hard-cut edges; default-CRF x264 rings along
        them differently per frame (visible edge shimmer after the occlusion
        multiply). Lossless encode must round-trip a hard edge within LSB
        tolerance of the yuv420p range conversion."""
        import cv2

        out_path = str(tmp_path / "matte.mp4")
        frames = []
        for k in range(3):
            frame = np.zeros(
                (subject_matte._MATTE_HEIGHT, subject_matte._MATTE_WIDTH), dtype=np.uint8
            )
            frame[:, : 100 + k] = 255  # hard vertical edge, shifting per frame
            frames.append(frame)

        proc = subject_matte._spawn_matte_writer(out_path)
        assert proc.stdin is not None
        for frame in frames:
            proc.stdin.write(frame.tobytes())
        _, stderr = proc.communicate(timeout=30)
        assert proc.returncode == 0, stderr.decode(errors="replace")

        cap = cv2.VideoCapture(out_path)
        try:
            for frame in frames:
                ok, decoded = cap.read()
                assert ok
                gray = cv2.cvtColor(decoded, cv2.COLOR_BGR2GRAY)
                diff = np.abs(gray.astype(np.int16) - frame.astype(np.int16))
                assert int(diff.max()) <= 2, (
                    f"edge ringing detected (max diff {int(diff.max())}) — "
                    "matte encode is no longer lossless"
                )
        finally:
            cap.release()


# ---------------------------------------------------------------------------
# Boundary-aware compute (cut_boundaries_s) — fake mediapipe, runs everywhere.
# Pins the per-clip ghost fix: temporal state must reset at known hard cuts
# and boundary-crossing pairs must not pollute the stability stats.
# ---------------------------------------------------------------------------


def _left_mask() -> np.ndarray:
    m = np.zeros((16, 16), dtype=np.float32)
    m[:, :8] = 0.95
    return m


def _right_mask() -> np.ndarray:
    m = np.zeros((16, 16), dtype=np.float32)
    m[:, 8:] = 0.95
    return m


@needs_ffmpeg
class TestBoundaryAwareCompute:
    def test_median_resets_at_boundary(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Clip A (left silhouette) cuts to clip B (right silhouette) at
        t=0.5. Without a reset, the 3-frame trailing median keeps A's
        silhouette alive for 2 ticks past the cut — the previous clip's
        subject occludes text in the next clip. With the boundary hint, the
        very first tick of clip B is pure B."""
        video_path = _build_brightness_ramp_clip(tmp_path / "ramp.mp4")
        calls: list[float] = []
        _install_fake_mediapipe(
            monkeypatch,
            calls,
            mask_fn=lambda i: _left_mask() if i < 15 else _right_mask(),
        )

        out_path = tmp_path / "matte.mp4"
        result = compute_subject_matte(
            str(video_path),
            [MatteWindow(0.0, 1.0)],
            str(out_path),
            cut_boundaries_s=[0.5],
        )
        assert result is not None

        import cv2

        cap = cv2.VideoCapture(str(out_path))
        try:
            frames = []
            while True:
                ok, frame = cap.read()
                if not ok:
                    break
                frames.append(cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY))
        finally:
            cap.release()
        assert len(frames) == 30

        h, w = frames[0].shape
        # Tick 15 is clip B's first frame: right side solid, left side empty.
        # Without the reset the median of {A, A, B} keeps the LEFT half solid
        # here (verified by test_no_boundaries_median_carries_across_cut).
        boundary_frame = frames[15]
        assert float(boundary_frame[:, : w // 4].mean()) < 30.0
        assert float(boundary_frame[:, 3 * w // 4 :].mean()) > 180.0

    def test_no_boundaries_median_carries_across_cut(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Control for the test above: WITHOUT the boundary hint the median
        really does ghost the previous clip's silhouette across the cut —
        proving the reset (not something else) removes it."""
        video_path = _build_brightness_ramp_clip(tmp_path / "ramp.mp4")
        calls: list[float] = []
        _install_fake_mediapipe(
            monkeypatch,
            calls,
            mask_fn=lambda i: _left_mask() if i < 15 else _right_mask(),
        )

        out_path = tmp_path / "matte.mp4"
        result = compute_subject_matte(str(video_path), [MatteWindow(0.0, 1.0)], str(out_path))
        assert result is not None

        import cv2

        cap = cv2.VideoCapture(str(out_path))
        try:
            frames = []
            while True:
                ok, frame = cap.read()
                if not ok:
                    break
                frames.append(cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY))
        finally:
            cap.release()

        h, w = frames[0].shape
        # Median of {A[13], A[14], B[15]} at tick 15 → LEFT half still solid.
        assert float(frames[15][:, : w // 4].mean()) > 180.0

    def test_boundary_pair_excluded_from_stats(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Subject present in clip 1, absent in clip 2. With the boundary
        hint the transition is a legit cut, not a presence flip; without it
        the same footage counts one flip."""
        blob = np.zeros((16, 16), dtype=np.float32)
        blob[4:12, 4:12] = 0.95

        def _mask_fn(i: int) -> np.ndarray:
            return blob if i < 15 else np.zeros((16, 16), dtype=np.float32)

        video_path = _build_brightness_ramp_clip(tmp_path / "ramp.mp4")

        calls_a: list[float] = []
        _install_fake_mediapipe(monkeypatch, calls_a, mask_fn=_mask_fn)
        with_hint = compute_subject_matte(
            str(video_path),
            [MatteWindow(0.0, 1.0)],
            str(tmp_path / "matte_a.mp4"),
            cut_boundaries_s=[0.5],
        )
        assert with_hint is not None
        assert with_hint.presence_flips == 0

        calls_b: list[float] = []
        _install_fake_mediapipe(monkeypatch, calls_b, mask_fn=_mask_fn)
        without_hint = compute_subject_matte(
            str(video_path),
            [MatteWindow(0.0, 1.0)],
            str(tmp_path / "matte_b.mp4"),
        )
        assert without_hint is not None
        assert without_hint.presence_flips == 1

    def test_garbage_boundaries_never_raise(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        video_path = _build_brightness_ramp_clip(tmp_path / "ramp.mp4")
        calls: list[float] = []
        _install_fake_mediapipe(monkeypatch, calls)
        result = compute_subject_matte(
            str(video_path),
            [MatteWindow(0.0, 1.0)],
            str(tmp_path / "matte.mp4"),
            cut_boundaries_s=["x", None, -3.0, 99.0, 0.5],  # type: ignore[list-item]
        )
        assert result is not None

    def test_sidecar_carries_large_jump_stats(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        video_path = _build_brightness_ramp_clip(tmp_path / "ramp.mp4")
        calls: list[float] = []
        blob = np.zeros((16, 16), dtype=np.float32)
        blob[4:12, 4:12] = 0.95
        _install_fake_mediapipe(monkeypatch, calls, mask_value=blob)

        out_path = tmp_path / "matte.mp4"
        result = compute_subject_matte(str(video_path), [MatteWindow(0.0, 1.0)], str(out_path))
        assert result is not None
        assert result.large_jump_count == 0
        assert result.large_jumps_per_s == 0.0

        sidecar = json.loads((tmp_path / "matte.mp4.json").read_text())
        assert sidecar["stats"]["large_jump_count"] == 0
        assert sidecar["stats"]["large_jumps_per_s"] == 0.0

    def test_oscillating_masks_produce_rejecting_stats(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Synthetic reproduction of the beach failure shape: the mask
        teleports left<->right every 3 ticks (period longer than the 3-frame
        median can suppress). The computed stats must trip the oscillation
        gate even though presence never flips."""
        video_path = _build_brightness_ramp_clip(tmp_path / "ramp.mp4")
        calls: list[float] = []
        _install_fake_mediapipe(
            monkeypatch,
            calls,
            mask_fn=lambda i: _left_mask() if (i // 3) % 2 == 0 else _right_mask(),
        )
        result = compute_subject_matte(
            str(video_path), [MatteWindow(0.0, 1.0)], str(tmp_path / "matte.mp4")
        )
        assert result is not None
        assert result.presence_flips == 0  # the old gates' blind spot
        assert result.large_jump_count > subject_matte._MAX_LARGE_JUMPS
        assert result.large_jump_count > (
            subject_matte._MAX_LARGE_JUMP_FRAC * result.iou_pair_count
        )
        assert matte_is_sane(result) is False

    def test_oscillation_gate_accepts_stable_matte_with_cuts(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A montage whose mask is stable WITHIN each clip but changes at
        every cut must pass the gate when boundaries are provided — legit
        cuts are not segmenter instability."""
        video_path = _build_brightness_ramp_clip(tmp_path / "ramp.mp4")
        calls: list[float] = []
        _install_fake_mediapipe(
            monkeypatch,
            calls,
            mask_fn=lambda i: _left_mask() if (i // 10) % 2 == 0 else _right_mask(),
        )
        result = compute_subject_matte(
            str(video_path),
            [MatteWindow(0.0, 1.0)],
            str(tmp_path / "matte.mp4"),
            cut_boundaries_s=[10 / 30, 20 / 30],
        )
        assert result is not None
        assert result.large_jump_count == 0
        assert matte_is_sane(result) is True


# ---------------------------------------------------------------------------
# RVM backbone — fake onnxruntime injected via sys.modules, runs everywhere.
# ---------------------------------------------------------------------------


class _FakeRvmSession:
    """Mimics the RVM ONNX graph contract: run(None, feeds) -> [fgr, pha,
    r1o..r4o]. Recurrent outputs are r_in + 1, so the r1i value observed at
    call k equals the number of frames since the last reset — a direct probe
    of reset behavior. pha is a fixed blob at input resolution."""

    def __init__(self) -> None:
        self.rec_values: list[float] = []
        self.downsample_ratios: list[float] = []
        self.input_shapes: list[tuple[int, int]] = []

    def run(self, outputs: object, feeds: dict) -> list:
        src = feeds["src"]
        assert src.ndim == 4 and src.shape[0] == 1 and src.shape[1] == 3
        h, w = src.shape[2], src.shape[3]
        self.input_shapes.append((h, w))
        self.rec_values.append(float(feeds["r1i"].flat[0]))
        self.downsample_ratios.append(float(feeds["downsample_ratio"][0]))
        pha = np.zeros((1, 1, h, w), dtype=np.float32)
        pha[0, 0, h // 4 : 3 * h // 4, w // 4 : 3 * w // 4] = 0.95
        fgr = np.zeros((1, 3, h, w), dtype=np.float32)
        rec_out = [feeds[k] + 1.0 for k in ("r1i", "r2i", "r3i", "r4i")]
        by_name = {
            "fgr": fgr,
            "pha": pha,
            "r1o": rec_out[0],
            "r2o": rec_out[1],
            "r3o": rec_out[2],
            "r4o": rec_out[3],
        }
        if outputs:
            # Production requests named outputs (pha first) so a re-exported
            # graph with a different declaration order can't silently swap
            # channels — the fake honors the same contract.
            return [by_name[name] for name in outputs]
        return [fgr, pha, *rec_out]


class _FakeSessionOptions:
    def __init__(self) -> None:
        self.intra_op_num_threads = 0
        self.inter_op_num_threads = 0
        self.config_entries: dict[str, str] = {}

    def add_session_config_entry(self, key: str, value: str) -> None:
        self.config_entries[key] = value


def _install_fake_onnxruntime(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> _FakeRvmSession:
    """Fake onnxruntime + an existing dummy model file so _create_backbone
    deterministically picks the RVM path."""
    import sys
    import types

    session = _FakeRvmSession()
    ort_mod = types.ModuleType("onnxruntime")
    ort_mod.InferenceSession = (  # type: ignore[attr-defined]
        lambda path, sess_options=None, providers=None: session
    )
    ort_mod.SessionOptions = _FakeSessionOptions  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "onnxruntime", ort_mod)

    model_file = tmp_path / "fake_rvm.onnx"
    model_file.write_bytes(b"onnx")
    # MATTE_RVM_MODEL_PATH is joined against the api app root — an absolute
    # path survives normpath and hits our tmp file.
    monkeypatch.setattr(subject_matte, "MATTE_RVM_MODEL_PATH", str(model_file))
    return session


@needs_ffmpeg
class TestRvmBackbone:
    def test_recurrent_state_carries_and_resets_at_boundaries(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        video_path = _build_brightness_ramp_clip(tmp_path / "ramp.mp4")
        session = _install_fake_onnxruntime(monkeypatch, tmp_path)

        result = compute_subject_matte(
            str(video_path),
            [MatteWindow(0.0, 1.0)],
            str(tmp_path / "matte.mp4"),
            cut_boundaries_s=[0.5],
        )
        assert result is not None
        # 30 calls; state counts 0..14 within each clip, reset at the cut.
        assert session.rec_values == [float(i % 15) for i in range(30)]
        # Frames are pre-downscaled (64x64 * 0.25 = 16x16) and fed with
        # downsample_ratio=1.0 — the full-res guided-filter/fgr work the
        # old full-res + ratio-0.25 shape paid for is gone.
        assert all(r == pytest.approx(1.0) for r in session.downsample_ratios)
        assert all(shape == (16, 16) for shape in session.input_shapes)

    def test_state_resets_between_windows(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        video_path = _build_brightness_ramp_clip(tmp_path / "ramp.mp4")
        session = _install_fake_onnxruntime(monkeypatch, tmp_path)

        result = compute_subject_matte(
            str(video_path),
            [MatteWindow(0.0, 0.5), MatteWindow(0.5, 1.0)],
            str(tmp_path / "matte.mp4"),
        )
        assert result is not None
        assert session.rec_values == [float(i % 15) for i in range(30)]

    def test_output_matte_format_unchanged(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        video_path = _build_brightness_ramp_clip(tmp_path / "ramp.mp4")
        _install_fake_onnxruntime(monkeypatch, tmp_path)

        out_path = tmp_path / "matte.mp4"
        result = compute_subject_matte(str(video_path), [MatteWindow(0.0, 1.0)], str(out_path))
        assert result is not None
        sidecar = json.loads((tmp_path / "matte.mp4.json").read_text())
        assert sidecar["size"] == [subject_matte._MATTE_WIDTH, subject_matte._MATTE_HEIGHT]
        assert sidecar["fps"] == subject_matte.MATTE_FPS
        provider = SubjectMatteProvider.open(str(out_path))
        assert provider is not None
        mask = provider.mask_at(0.5)
        assert mask is not None and mask.shape == (1920, 1080)

    def test_rvm_skips_roi_second_pass(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The window-wide ROI crop is a mediapipe-only workaround; on a
        multi-clip montage it zeroes any clip whose subject sits outside the
        crop. RVM must run exactly one pass even for a small subject."""
        video_path = _build_brightness_ramp_clip(tmp_path / "ramp.mp4")
        session = _install_fake_onnxruntime(monkeypatch, tmp_path)

        class _SmallBlobSession(_FakeRvmSession):
            def run(self, outputs: object, feeds: dict) -> list:
                out = super().run(outputs, feeds)
                src = feeds["src"]
                h, w = src.shape[2], src.shape[3]
                pha = np.zeros((1, 1, h, w), dtype=np.float32)
                pha[0, 0, h // 2 : h // 2 + 4, w // 2 : w // 2 + 4] = 0.95
                out[1] = pha
                return out

        small = _SmallBlobSession()
        import sys

        sys.modules["onnxruntime"].InferenceSession = (  # type: ignore[attr-defined]
            lambda path, sess_options=None, providers=None: small
        )
        result = compute_subject_matte(
            str(video_path), [MatteWindow(0.0, 1.0)], str(tmp_path / "matte.mp4")
        )
        assert result is not None
        assert len(small.rec_values) == 30  # single pass — no ROI re-run
        assert result.frame_count == 30
        assert session.rec_values == []  # first fake never used

    def test_kill_switch_falls_back_to_mediapipe(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from app.config import settings

        video_path = _build_brightness_ramp_clip(tmp_path / "ramp.mp4")
        session = _install_fake_onnxruntime(monkeypatch, tmp_path)
        monkeypatch.setattr(settings, "matte_rvm_enabled", False, raising=False)

        mp_calls: list[float] = []
        _install_fake_mediapipe(monkeypatch, mp_calls)
        # _install_fake_mediapipe points MATTE_RVM_MODEL_PATH at a missing
        # file; restore the existing fake model so the FLAG is the only
        # reason RVM is skipped — otherwise this test is indistinguishable
        # from test_missing_rvm_model_falls_back_to_mediapipe.
        monkeypatch.setattr(subject_matte, "MATTE_RVM_MODEL_PATH", str(tmp_path / "fake_rvm.onnx"))

        result = compute_subject_matte(
            str(video_path), [MatteWindow(0.0, 1.0)], str(tmp_path / "matte.mp4")
        )
        assert result is not None
        assert session.rec_values == []  # RVM never touched
        assert len(mp_calls) == 30

    def test_long_window_total_falls_back_to_mediapipe(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Window totals beyond _RVM_MAX_TOTAL_TICKS can't finish RVM
        inference inside the wall-clock budget on prod vCPUs — the budget
        guard must pick mediapipe up front instead of burning 90s and
        aborting."""
        video_path = _build_brightness_ramp_clip(tmp_path / "ramp.mp4")
        session = _install_fake_onnxruntime(monkeypatch, tmp_path)
        mp_calls: list[float] = []
        _install_fake_mediapipe(monkeypatch, mp_calls)
        monkeypatch.setattr(subject_matte, "MATTE_RVM_MODEL_PATH", str(tmp_path / "fake_rvm.onnx"))
        monkeypatch.setattr(subject_matte, "_RVM_MAX_TOTAL_TICKS", 10)

        result = compute_subject_matte(
            str(video_path), [MatteWindow(0.0, 1.0)], str(tmp_path / "matte.mp4")
        )
        assert result is not None
        assert session.rec_values == []  # RVM skipped by the tick guard
        assert len(mp_calls) == 30

    def test_missing_rvm_model_falls_back_to_mediapipe(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        video_path = _build_brightness_ramp_clip(tmp_path / "ramp.mp4")
        mp_calls: list[float] = []
        # _install_fake_mediapipe already points MATTE_RVM_MODEL_PATH at a
        # missing file — exactly the unavailable-RVM production shape.
        _install_fake_mediapipe(monkeypatch, mp_calls)
        result = compute_subject_matte(
            str(video_path), [MatteWindow(0.0, 1.0)], str(tmp_path / "matte.mp4")
        )
        assert result is not None
        assert len(mp_calls) == 30

    def test_rvm_enabled_defaults_true_on_config_failure(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Config import must never break the matte: a broken app.config
        (no `settings` attr → ImportError) defaults the backbone selector to
        RVM-on rather than raising."""
        import sys
        import types

        monkeypatch.setitem(sys.modules, "app.config", types.ModuleType("app.config"))
        assert subject_matte._rvm_enabled() is True
