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


def _install_fake_mediapipe(monkeypatch: pytest.MonkeyPatch, calls: list[float]) -> None:
    """Fake mediapipe module tree that records the input brightness per
    segment() call and returns an empty mask."""
    import sys
    import types

    class _FakeImage:
        def __init__(self, image_format: object = None, data: np.ndarray | None = None) -> None:
            self.data = data

    class _FakeMask:
        def numpy_view(self) -> np.ndarray:
            return np.zeros((16, 16), dtype=np.float32)

    class _FakeResult:
        confidence_masks = [_FakeMask()]

    class _FakeSegmenter:
        def segment(self, image: _FakeImage) -> _FakeResult:
            assert image.data is not None
            calls.append(float(image.data.mean()))
            return _FakeResult()

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
