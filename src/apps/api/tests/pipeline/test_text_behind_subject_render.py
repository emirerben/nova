"""Unit tests for the `behind_subject` text-occlusion hook in the Skia
renderer (Lane B). The matte engine itself (app.pipeline.subject_matte) is a
concurrently-developed sibling module — nothing here imports it. Fake
providers are plain objects exposing `mask_at(t_abs) -> np.ndarray | None`,
matching the `SubjectMatteProvider` Protocol structurally.
"""

from __future__ import annotations

import os
import tempfile
from unittest import mock

import numpy as np
import pytest
from PIL import Image

from app.pipeline import text_overlay_skia as tos


@pytest.fixture
def tmp_workdir():
    with tempfile.TemporaryDirectory(prefix="behind_subject_test_") as d:
        yield d


@pytest.fixture(autouse=True)
def _skip_canvas_probe_for_renderer_unit_tests(monkeypatch):
    """These command-shape tests use one-byte stand-ins instead of real MP4s."""
    monkeypatch.setattr(tos, "_validate_input_canvas", lambda *_args, **_kwargs: None)


class _ConstantMatte:
    """Stub matte provider: reports the same mask value at every timestamp."""

    def __init__(self, value: float):
        self.value = value
        self.calls: list[float] = []

    def mask_at(self, t_abs: float) -> np.ndarray | None:
        self.calls.append(t_abs)
        return np.full((tos.CANVAS_H, tos.CANVAS_W), self.value, dtype=np.float32)


class _NoneMatte:
    """Stub matte provider that never has a mask for the given timestamp."""

    def mask_at(self, t_abs: float) -> np.ndarray | None:
        return None


def _behind_overlay(**kw) -> dict:
    base = {
        "text": "HELLO",
        "start_s": 0.0,
        "end_s": 1.0,
        "effect": "none",
        "behind_subject": True,
        "font_family": "Playfair Display",
        "text_size_px": 100,
        "text_color": "#FFFFFF",
    }
    base.update(kw)
    return base


def _sequence_overlay(**kw) -> dict:
    base = {
        "text": "the days we lost",
        "role": tos.SEQUENCE_OVERLAY_ROLE,
        "effect": "fade-in",
        "fade_out_ms": 500,
        "start_s": 0.0,
        "end_s": 6.0,
        "font_family": "Playfair Display Regular",
        "text_size_px": 80,
        "text_color": "#FFFFFF",
        "position_x_frac": 0.45,
        "position_y_frac": 0.40,
    }
    base.update(kw)
    return base


# -- _apply_subject_mask: pure numpy math -------------------------------------


def test_apply_subject_mask_full_mask_zeroes_alpha():
    rgba = np.zeros((4, 4, 4), dtype=np.uint8)
    rgba[..., 3] = 255
    mask = np.ones((4, 4), dtype=np.float32)
    out = tos._apply_subject_mask(rgba, mask)
    assert out.dtype == np.uint8
    assert (out[..., 3] == 0).all()


def test_apply_subject_mask_zero_mask_is_noop():
    rgba = np.zeros((4, 4, 4), dtype=np.uint8)
    rgba[..., 3] = 200
    rgba[..., 0] = 10
    mask = np.zeros((4, 4), dtype=np.float32)
    out = tos._apply_subject_mask(rgba, mask)
    assert (out == rgba).all()


def test_apply_subject_mask_partial_scales_alpha():
    rgba = np.zeros((2, 2, 4), dtype=np.uint8)
    rgba[..., 3] = 200
    mask = np.full((2, 2), 0.5, dtype=np.float32)
    out = tos._apply_subject_mask(rgba, mask)
    assert (out[..., 3] == 100).all()


def test_apply_subject_mask_straight_alpha_leaves_rgb_untouched():
    """Straight (non-premultiplied) alpha means only the alpha channel needs
    scaling — see `_apply_subject_mask`'s docstring for the premultiplied-vs-
    straight finding this pins."""
    rgba = np.zeros((2, 2, 4), dtype=np.uint8)
    rgba[..., 0] = 200
    rgba[..., 1] = 50
    rgba[..., 2] = 10
    rgba[..., 3] = 255
    mask = np.full((2, 2), 0.7, dtype=np.float32)
    out = tos._apply_subject_mask(rgba, mask)
    assert (out[..., 0] == 200).all()
    assert (out[..., 1] == 50).all()
    assert (out[..., 2] == 10).all()


def test_apply_subject_mask_dtype_preserved():
    rgba = np.zeros((3, 3, 4), dtype=np.uint8)
    rgba[..., 3] = 128
    mask = np.full((3, 3), 0.25, dtype=np.float32)
    out = tos._apply_subject_mask(rgba, mask)
    assert out.dtype == np.uint8


def test_apply_subject_mask_resizes_mismatched_mask():
    """A well-formed 2-D mask of a different shape is resized to the rgba's
    shape and applied — the landscape-canvas case (#661): the stored matte is
    portrait-raster regardless of source orientation, so the resize is the
    geometrically correct registration, not a fallback."""
    rgba = np.zeros((4, 6, 4), dtype=np.uint8)
    rgba[..., 3] = 255
    # Left half subject, right half clear — at a different resolution.
    mask = np.zeros((2, 4), dtype=np.float32)
    mask[:, :2] = 1.0
    with mock.patch.object(tos, "log") as mock_log:
        out = tos._apply_subject_mask(rgba, mask)
    assert out[..., 3][:, 0].max() == 0, "subject side fully occluded after resize"
    assert out[..., 3][:, -1].min() == 255, "clear side untouched after resize"
    for call in mock_log.warning.call_args_list:
        assert call.args[0] != "text_behind_subject_mask_shape_mismatch"


def test_apply_subject_mask_garbage_mask_fails_open():
    """A mask that cannot be resized (wrong ndim) still fails open — returns
    the rgba unchanged with a warning, never raises mid-render."""
    rgba = np.zeros((4, 4, 4), dtype=np.uint8)
    rgba[..., 3] = 255
    mask = np.ones((2, 2, 3), dtype=np.float32)
    with mock.patch.object(tos, "log") as mock_log:
        out = tos._apply_subject_mask(rgba, mask)
    assert (out == rgba).all()
    mock_log.warning.assert_called_once()
    assert mock_log.warning.call_args[0][0] == "text_behind_subject_mask_shape_mismatch"


# -- _uses_long_running_frame_ceiling -----------------------------------------


def test_behind_subject_uses_long_running_frame_ceiling():
    assert tos._uses_long_running_frame_ceiling({"behind_subject": True, "effect": "none"})
    assert tos._uses_long_running_frame_ceiling({"behind_subject": True, "effect": "pop-in"})
    assert not tos._uses_long_running_frame_ceiling({"behind_subject": False, "effect": "none"})
    assert not tos._uses_long_running_frame_ceiling({"effect": "none"})


# -- Fallback when no matte is supplied ---------------------------------------


def test_behind_subject_without_matte_falls_back_to_static_render(tmp_workdir):
    overlay = _behind_overlay()
    with mock.patch.object(tos, "log") as mock_log:
        seq = tos._generate_overlay_sequence(overlay, tmp_workdir, 0)
    assert seq is not None
    assert seq["is_animated"] is False
    assert seq["n_frames"] == 1
    assert os.path.exists(seq["first_frame"])
    mock_log.warning.assert_any_call(
        "text_behind_subject_no_matte_fallback", role=None, text="HELLO"
    )


def test_behind_subject_with_none_provider_result_falls_back_per_frame(tmp_workdir):
    """A matte object IS supplied, but its `mask_at` reports no data at every
    timestamp — frames still render, just unmasked (no exception)."""
    overlay = _behind_overlay(effect="fade-in")
    seq = tos._generate_overlay_sequence(overlay, tmp_workdir, 0, matte=_NoneMatte())
    assert seq is not None
    assert seq["is_animated"] is True
    assert os.path.exists(seq["first_frame"])


def test_pre_burn_curtain_path_never_receives_matte_and_does_not_crash(tmp_workdir):
    """`pre_burn_curtain_slot_text_skia` intentionally has no matte plumbing
    (v1 excludes the curtain path) — a behind_subject overlay there must not
    raise, it degrades to a normal render via the no-matte fallback."""
    in_path = os.path.join(tmp_workdir, "in.mp4")
    out_path = os.path.join(tmp_workdir, "out.mp4")
    with open(in_path, "wb") as f:
        f.write(b"\x00")
    with mock.patch("app.pipeline.text_overlay_skia.subprocess.run") as run_mock:
        run_mock.return_value.returncode = 0
        tos.pre_burn_curtain_slot_text_skia(
            in_path, [_behind_overlay()], out_path, tmp_workdir, slot_duration_s=2.0, slot_index=0
        )
    run_mock.assert_called_once()


# -- Occlusion render with a fake matte provider ------------------------------


def test_behind_subject_with_matte_renders_animated_masked_sequence(tmp_workdir):
    overlay = _behind_overlay()
    matte = _ConstantMatte(0.5)
    seq = tos._generate_overlay_sequence(overlay, tmp_workdir, 0, matte=matte)
    assert seq is not None
    assert seq["is_animated"] is True
    wanted = int(round(1.0 * tos.FPS))
    assert seq["n_frames"] == wanted + 1  # + seam hold frame, same as any animated sequence
    assert seq["n_frames"] <= tos.LONG_RUNNING_TEXT_FRAME_CEILING

    frames = sorted(
        os.path.join(tmp_workdir, f) for f in os.listdir(tmp_workdir) if f.endswith(".png")
    )
    assert len(frames) == seq["n_frames"]
    # Every frame must be a real render, not a hard-linked duplicate — the
    # subject's mask can move even when the settled text doesn't.
    assert all(os.stat(f).st_nlink == 1 for f in frames)

    # The matte is sampled twice per frame: once by the sequential visibility
    # pre-pass (which never engages on a steady 50% occlusion — no strobing,
    # not near-total) and once by the per-pixel render.
    assert len(matte.calls) == 2 * seq["n_frames"]
    distinct = sorted(set(matte.calls))
    assert len(distinct) == seq["n_frames"]
    assert distinct[0] == pytest.approx(0.0)
    assert distinct[-1] == pytest.approx((seq["n_frames"] - 1) / tos.FPS)

    unmasked = tos._skia_image_to_rgba_array(tos._draw_frame(overlay, 0.0, 1.0))
    masked = np.array(Image.open(frames[0]).convert("RGBA"))
    assert unmasked[..., 3].max() > 200, "sanity: unmasked frame has opaque text"
    # 50% occlusion halves alpha wherever the unmasked frame was opaque.
    assert masked[..., 3].max() < unmasked[..., 3].max() // 2 + 5


def test_behind_subject_animated_overlay_masks_every_frame(tmp_workdir):
    """Animated overlays (not just the static shortcut) also get the mask
    multiply applied after their own per-frame draw."""
    overlay = _behind_overlay(effect="fade-in", end_s=0.3)
    matte = _ConstantMatte(1.0)  # fully occluded
    seq = tos._generate_overlay_sequence(overlay, tmp_workdir, 0, matte=matte)
    assert seq is not None
    assert seq["is_animated"] is True
    frames = sorted(
        os.path.join(tmp_workdir, f) for f in os.listdir(tmp_workdir) if f.endswith(".png")
    )
    for f in frames:
        arr = np.array(Image.open(f).convert("RGBA"))
        assert arr[..., 3].max() == 0, f"{f} should be fully occluded (mask=1.0)"


# -- Moving partial occlusion never hides the whole text layer ----------------


class _PartialSweepMatte:
    """Move a rectangular occluder across the text during a scripted window."""

    def __init__(self, occlude_start: float, occlude_end: float):
        self.occlude_start = occlude_start
        self.occlude_end = occlude_end

    def mask_at(self, t_abs: float) -> np.ndarray | None:
        mask = np.zeros((tos.CANVAS_H, tos.CANVAS_W), dtype=np.float32)
        if self.occlude_start <= t_abs < self.occlude_end:
            progress = (t_abs - self.occlude_start) / (self.occlude_end - self.occlude_start)
            x0 = int(tos.CANVAS_W * (0.35 + 0.10 * progress))
            x1 = x0 + int(tos.CANVAS_W * 0.30)
            mask[:, x0:x1] = 1.0
        return mask


def test_partial_sweep_only_occludes_intersecting_text_pixels(tmp_workdir):
    """Regression: partial overlap must never fade the entire text layer out."""
    overlay = _behind_overlay(end_s=1.0)
    matte = _PartialSweepMatte(occlude_start=0.3, occlude_end=0.6)
    seq = tos._generate_overlay_sequence(overlay, tmp_workdir, 0, matte=matte)
    assert seq is not None

    frames = sorted(
        os.path.join(tmp_workdir, f) for f in os.listdir(tmp_workdir) if f.endswith(".png")
    )
    first = np.array(Image.open(frames[0]).convert("RGBA"))
    last = np.array(Image.open(frames[-1]).convert("RGBA"))

    text_pixels = first[..., 3] > 0
    for frame_index in range(9, 18):  # t=0.3 through t=0.566..., inside the sweep
        mask = matte.mask_at(frame_index / tos.FPS) > 0
        covered = text_pixels & mask
        uncovered = text_pixels & ~mask
        assert covered.any(), f"frame {frame_index}: moving matte must intersect the text"
        assert uncovered.any(), f"frame {frame_index}: some text must remain outside the matte"
        partial = np.array(Image.open(frames[frame_index]).convert("RGBA"))
        assert (partial[..., 3][covered] == 0).all(), (
            f"frame {frame_index}: only intersecting text pixels are occluded"
        )
        assert np.array_equal(partial[..., 3][uncovered], first[..., 3][uncovered]), (
            f"frame {frame_index}: uncovered glyph pixels must keep their original alpha"
        )
        assert partial[..., 3].max() > 200, (
            f"frame {frame_index}: partial overlap must not hide the entire text layer"
        )
    assert np.array_equal(last, first), "text returns pixel-identically after the matte clears"


def test_behind_subject_disables_hold_frame_economy_for_sequence_role_case(tmp_workdir):
    """Even a would-be-holdable sequence-shaped window renders every frame
    uniquely once behind_subject + matte are active — the hold-frame
    hard-link trick assumes a static settled frame, which a moving mask
    violates."""
    overlay = _behind_overlay(end_s=2.0)
    matte = _ConstantMatte(0.2)
    seq = tos._generate_overlay_sequence(overlay, tmp_workdir, 0, matte=matte)
    assert seq is not None
    frames = [os.path.join(tmp_workdir, f) for f in os.listdir(tmp_workdir) if f.endswith(".png")]
    assert len(frames) == seq["n_frames"]
    assert all(os.stat(f).st_nlink == 1 for f in frames)


# -- Frame ceiling: behind_subject gets its own, larger ceiling --------------
#
# Generative intro overlays can be hold-to-EOF (effect="static", end_s
# spanning nearly the whole clip). Without behind_subject those take the
# `-loop 1` single-PNG static path and persist forever; WITH behind_subject
# they're forced onto this animated per-frame path (the mask varies per
# frame), which is bounded by BEHIND_SUBJECT_FRAME_CEILING (120s) instead of
# the tighter LONG_RUNNING_TEXT_FRAME_CEILING (30s) other long-running
# effects use. These tests monkeypatch the PNG write + mask-apply to keep
# runtime sane at 1000s of frames — frame COUNT/clamp behavior is what's
# under test here, pixel correctness is covered by the smaller-window tests
# above.


class _FastMatte:
    """Cheap matte stub for large-window ceiling tests: records calls without
    allocating a full-resolution mask array per call (paired with a
    monkeypatched `_apply_subject_mask` that ignores mask contents)."""

    def __init__(self):
        self.calls: list[float] = []

    def mask_at(self, t_abs: float) -> np.ndarray | None:
        self.calls.append(t_abs)
        return np.zeros((1, 1), dtype=np.float32)


def test_behind_subject_45s_window_not_clamped_at_long_running_ceiling(tmp_workdir, monkeypatch):
    """A 45s hold-to-EOF window (1350 frames) must NOT be clamped at the 30s/
    900-frame LONG_RUNNING_TEXT_FRAME_CEILING other long-running effects use —
    behind_subject gets the larger BEHIND_SUBJECT_FRAME_CEILING (120s)."""
    monkeypatch.setattr(tos, "_write_rgba_array_png", lambda arr, out_path: None)
    monkeypatch.setattr(tos, "_apply_subject_mask", lambda rgba, mask: rgba)

    overlay = _behind_overlay(end_s=45.0, effect="static")
    matte = _FastMatte()
    with mock.patch.object(tos, "log", wraps=tos.log) as mock_log:
        seq = tos._generate_overlay_sequence(overlay, tmp_workdir, 0, matte=matte)

    assert seq is not None
    wanted = int(round(45.0 * tos.FPS))
    assert wanted == 1350
    assert seq["n_frames"] == wanted + 1  # + seam hold frame, same as any animated sequence
    assert seq["n_frames"] > tos.LONG_RUNNING_TEXT_FRAME_CEILING
    assert seq["n_frames"] <= tos.BEHIND_SUBJECT_FRAME_CEILING
    # Visibility pre-pass + render each sample every frame.
    assert len(matte.calls) == 2 * seq["n_frames"]
    for call in mock_log.warning.call_args_list:
        assert call.args[0] != "skia_long_running_text_duration_clamped"


def test_behind_subject_150s_window_clamps_at_behind_subject_ceiling_with_warning(
    tmp_workdir, monkeypatch
):
    """A window past the 120s BEHIND_SUBJECT_FRAME_CEILING must clamp to
    exactly 3600 frames and log the existing truncation warning."""
    monkeypatch.setattr(tos, "_write_rgba_array_png", lambda arr, out_path: None)
    monkeypatch.setattr(tos, "_apply_subject_mask", lambda rgba, mask: rgba)

    overlay = _behind_overlay(end_s=150.0, effect="static")
    matte = _FastMatte()
    with mock.patch.object(tos, "log", wraps=tos.log) as mock_log:
        seq = tos._generate_overlay_sequence(overlay, tmp_workdir, 0, matte=matte)

    assert seq is not None
    assert tos.BEHIND_SUBJECT_FRAME_CEILING == 3600
    assert seq["n_frames"] == tos.BEHIND_SUBJECT_FRAME_CEILING
    # Visibility pre-pass + render each sample every frame.
    assert len(matte.calls) == 2 * tos.BEHIND_SUBJECT_FRAME_CEILING
    mock_log.warning.assert_any_call(
        "skia_long_running_text_duration_clamped",
        effect="static",
        duration_s=150.0,
        wanted_frames=4500,
        clamped_to=tos.BEHIND_SUBJECT_FRAME_CEILING,
    )


# -- Sequence-role overlays: matte-aware per-overlay fallback -----------------


def test_sequence_role_behind_subject_without_matte_degrades_to_normal_text(tmp_workdir):
    overlays = [
        _sequence_overlay(text="first", start_s=0.0, end_s=3.0, behind_subject=True),
        _sequence_overlay(text="second", start_s=1.0, end_s=4.0, position_y_frac=0.5),
    ]
    sequences, work_dir = tos.render_text_overlay_sequences(overlays, tmp_workdir)
    assert len(sequences) == 2
    assert work_dir is not None


def test_sequence_handwriting_uses_changing_matte_after_reveal_settles(tmp_workdir):
    """The public path must keep sequence handwriting matte-aware after 2.2s."""
    canvas = tos.Canvas(180, 320)

    class _ChangingMatte:
        def __init__(self):
            self.calls: list[float] = []

        def mask_at(self, t_abs: float) -> np.ndarray:
            self.calls.append(t_abs)
            mask = np.zeros((canvas.height, canvas.width), dtype=np.float32)
            if int(round(t_abs * tos.FPS)) % 2:
                mask[:, : canvas.width // 2] = 0.5
            else:
                mask[:, canvas.width // 2 :] = 0.5
            return mask

    overlays = [
        _sequence_overlay(
            text="first",
            effect="handwriting",
            start_s=0.0,
            end_s=2.5,
            fade_out_ms=0,
            behind_subject=True,
            text_size_px=24,
        ),
        _sequence_overlay(
            text="second",
            effect="handwriting",
            start_s=0.0,
            end_s=2.5,
            fade_out_ms=0,
            behind_subject=True,
            position_y_frac=0.55,
            text_size_px=24,
        ),
    ]
    matte = _ChangingMatte()
    sequences, work_dir = tos.render_text_overlay_sequences(
        overlays, tmp_workdir, matte=matte, canvas=canvas
    )
    assert len(sequences) == 2
    assert all(sequence["is_animated"] for sequence in sequences)
    assert work_dir is not None
    settled_a = np.array(Image.open(sequences[0]["pattern"] % 68).convert("RGBA"))
    settled_b = np.array(Image.open(sequences[0]["pattern"] % 69).convert("RGBA"))
    assert not np.array_equal(settled_a, settled_b)
    assert len(matte.calls) > 0


# -- FFmpeg command shape parity -----------------------------------------------


def test_behind_subject_ffmpeg_cmd_matches_ordinary_animated_shape(tmp_workdir):
    """The burn command for a behind_subject overlay must be structurally
    identical to any other animated overlay: framerate/start_number/image2
    input, one overlay filter stage, preset=fast. THE FFMPEG COMMAND BUILDER
    ITSELF IS UNCHANGED — occlusion lives entirely in the PNG frames."""
    overlay = _behind_overlay(end_s=0.5)
    matte = _ConstantMatte(0.3)
    in_path = os.path.join(tmp_workdir, "in.mp4")
    out_path = os.path.join(tmp_workdir, "out.mp4")
    with open(in_path, "wb") as f:
        f.write(b"\x00")
    with mock.patch("app.pipeline.text_overlay_skia.subprocess.run") as run_mock:
        run_mock.return_value.returncode = 0
        tos.burn_text_overlays_skia(in_path, [overlay], out_path, tmp_workdir, matte=matte)
    cmd = run_mock.call_args[0][0]
    assert cmd.count("-i") == 2, f"expected video + 1 animated PNG-sequence input, got {cmd}"
    assert "-framerate" in cmd
    assert "-start_number" in cmd
    assert any("skia_overlay_000_f%04d.png" in str(a) for a in cmd)
    fc = cmd[cmd.index("-filter_complex") + 1]
    assert fc.count("overlay=") == 1
    assert "setpts=PTS+0.0000/TB" in fc
    assert "-preset" in cmd
    assert cmd[cmd.index("-preset") + 1] == "fast"


def test_behind_subject_without_matte_ffmpeg_cmd_uses_static_loop_shape(tmp_workdir):
    """No matte → the no-matte fallback renders the ordinary single-PNG
    static overlay, so the burn command uses `-loop 1` like any other static
    overlay (no framerate/start_number input)."""
    overlay = _behind_overlay(end_s=1.0)
    in_path = os.path.join(tmp_workdir, "in.mp4")
    out_path = os.path.join(tmp_workdir, "out.mp4")
    with open(in_path, "wb") as f:
        f.write(b"\x00")
    with mock.patch("app.pipeline.text_overlay_skia.subprocess.run") as run_mock:
        run_mock.return_value.returncode = 0
        tos.burn_text_overlays_skia(in_path, [overlay], out_path, tmp_workdir)
    cmd = run_mock.call_args[0][0]
    assert "-loop" in cmd
    assert "-framerate" not in cmd


# -- Visibility policy: anti-strobe hide with hysteresis ----------------------
#
# Restored from the #651 quality train (removed by #670) with a strobe gate:
# heavy occlusion alone no longer hides the layer (the #670 complaint — a
# smooth sweep must stay per-pixel even at 95% occlusion); it hides only when
# the occlusion is ALSO strobing (repeated frame-to-frame visible-alpha
# jumps), or when occlusion is near-total (> BEHIND_FULL_OCCLUSION_FRAC).


class _ScriptedScaleMatte:
    """Frame-indexed scripted masks for `_behind_visibility_scales` tests.
    Masks are text_alpha-shaped float arrays; index = round(t_abs * FPS)."""

    def __init__(self, masks: list[np.ndarray | None]):
        self.masks = masks

    def mask_at(self, t_abs: float) -> np.ndarray | None:
        idx = min(len(self.masks) - 1, max(0, int(round(t_abs * tos.FPS))))
        return self.masks[idx]


def _alpha(shape=(8, 8)) -> np.ndarray:
    return np.ones(shape, dtype=np.float32)


def _flat(value: float, shape=(8, 8)) -> np.ndarray:
    return np.full(shape, value, dtype=np.float32)


def _gap_mask(gap: str, occ: float = 0.8, shape=(8, 8)) -> np.ndarray:
    """Full occlusion except a clear gap band on the `left` or `right` —
    alternating the two emulates mask gaps opening/closing every frame
    (the crowd-strobe failure #651 measured at 12 visible-alpha jumps/s)."""
    mask = np.ones(shape, dtype=np.float32)
    cols = max(1, int(round(shape[1] * (1.0 - occ))))
    if gap == "left":
        mask[:, :cols] = 0.0
    else:
        mask[:, -cols:] = 0.0
    return mask


def test_visibility_policy_smooth_heavy_occlusion_stays_per_pixel():
    """Steady 90% occlusion with no strobing must NOT hide the layer — the
    #670 fix's core requirement, now satisfied by the strobe gate instead of
    deleting the policy."""
    n = 30
    matte = _ScriptedScaleMatte([_flat(0.9)] * n)
    scales = tos._behind_visibility_scales(matte, _alpha(), 0.0, n, 1.0 / tos.FPS)
    assert scales is None


def test_visibility_policy_hides_on_strobing_heavy_occlusion():
    """Alternating gap masks at ~80% occlusion jump visible alpha by ~0.4 of
    the text's own alpha every frame — the strobe gate engages the hide and
    ramps out over the fade window."""
    n = 30
    masks = [_gap_mask("left" if i % 2 == 0 else "right") for i in range(n)]
    scales = tos._behind_visibility_scales(
        _ScriptedScaleMatte(masks), _alpha(), 0.0, n, 1.0 / tos.FPS
    )
    assert scales is not None
    assert scales[-1] == 0.0
    # Fade, not a pop: some frame carries an intermediate scale.
    assert any(0.0 < s < 1.0 for s in scales)
    # Engages within the first strobe window, not at the very first frame.
    assert scales[0] == 1.0
    assert min(scales[: tos._BEHIND_STROBE_WINDOW_FRAMES]) < 1.0


def test_visibility_policy_near_total_occlusion_hides_without_strobe():
    """>98% steady occlusion hides unconditionally — the <=2% surviving
    shreds are below what the strobe detector can measure."""
    n = 12
    matte = _ScriptedScaleMatte([_flat(0.99)] * n)
    scales = tos._behind_visibility_scales(matte, _alpha(), 0.0, n, 1.0 / tos.FPS)
    assert scales is not None
    assert scales[-1] == 0.0
    assert scales[tos._BEHIND_VISIBILITY_FADE_FRAMES] == 0.0


def test_visibility_policy_hysteresis_does_not_flap_in_the_gap():
    """Once hidden, occlusion between SHOW (0.50) and HIDE (0.70) keeps the
    text hidden — jitter around one threshold can't flap the state."""
    frame_dur = 1.0 / tos.FPS
    engage = [_gap_mask("left" if i % 2 == 0 else "right") for i in range(10)]
    in_gap = [_flat(0.6)] * 10
    scales = tos._behind_visibility_scales(
        _ScriptedScaleMatte(engage + in_gap), _alpha(), 0.0, 20, frame_dur
    )
    assert scales is not None
    assert scales[-1] == 0.0, "0.6 occlusion after a hide must stay hidden (hysteresis)"


def test_visibility_policy_reveals_when_clearly_visible_again():
    frame_dur = 1.0 / tos.FPS
    engage = [_gap_mask("left" if i % 2 == 0 else "right") for i in range(10)]
    clear = [_flat(0.2)] * 10
    scales = tos._behind_visibility_scales(
        _ScriptedScaleMatte(engage + clear), _alpha(), 0.0, 20, frame_dur
    )
    assert scales is not None
    assert scales[9] == 0.0, "sanity: strobe segment engaged the hide"
    assert scales[-1] == 1.0, "clearly-visible text fades back in"


def test_visibility_policy_single_smooth_pass_never_engages_strobe_rule():
    """One occluder entering (jump), holding at 80%, and exiting (jump)
    produces exactly 2 strobe events — below the 3-event floor, so a single
    smooth pass can never engage the strobe rule."""
    n = 20
    masks = [_flat(0.0)] * 5 + [_flat(0.8)] * 10 + [_flat(0.0)] * 5
    scales = tos._behind_visibility_scales(
        _ScriptedScaleMatte(masks), _alpha(), 0.0, n, 1.0 / tos.FPS
    )
    assert scales is None


def test_visibility_policy_zero_alpha_returns_none():
    matte = _ScriptedScaleMatte([_flat(1.0)] * 4)
    assert (
        tos._behind_visibility_scales(
            matte, np.zeros((8, 8), dtype=np.float32), 0.0, 4, 1.0 / tos.FPS
        )
        is None
    )


def test_heavily_occluded_window_writes_fully_transparent_frames(tmp_workdir):
    """Full-render integration: a steadily >98%-occluding matte engages the
    policy and the settled frames come out fully transparent — clean hide,
    not a strobe of shredded fragments."""
    overlay = _behind_overlay()
    matte = _ConstantMatte(0.99)
    with mock.patch.object(tos, "log", wraps=tos.log) as mock_log:
        seq = tos._generate_overlay_sequence(overlay, tmp_workdir, 0, matte=matte)
    assert seq is not None
    mock_log.info.assert_any_call(
        "text_behind_subject_visibility_policy_engaged",
        hidden_frames=mock.ANY,
        n_render=seq["n_frames"],
        text="HELLO",
    )
    frames = sorted(
        os.path.join(tmp_workdir, f) for f in os.listdir(tmp_workdir) if f.endswith(".png")
    )
    last = np.array(Image.open(frames[-1]).convert("RGBA"))
    assert last[..., 3].max() == 0, "settled hidden frame must be fully transparent"


# -- Landscape canvas: portrait-raster matte registers onto 1920x1080 ---------


class _PortraitRasterMatte:
    """Always returns the portrait-shaped (1920, 1080) full-occlusion mask
    `mask_at` produces in prod regardless of source orientation."""

    def mask_at(self, t_abs: float) -> np.ndarray | None:
        return np.ones((1920, 1080), dtype=np.float32)


def test_landscape_canvas_behind_subject_occludes(tmp_workdir):
    """Landscape variants (#661) render on a 1920x1080 canvas while the matte
    is stored portrait-raster — before the resize fix the shape mismatch
    failed open and behind_subject was a silent no-op on every landscape
    render."""
    from app.pipeline.canvas import LANDSCAPE

    overlay = _behind_overlay(end_s=0.2)
    with mock.patch.object(tos, "log", wraps=tos.log) as mock_log:
        seq = tos._generate_overlay_sequence(
            overlay, tmp_workdir, 0, matte=_PortraitRasterMatte(), render_canvas=LANDSCAPE
        )
    assert seq is not None
    for call in mock_log.warning.call_args_list:
        assert call.args[0] != "text_behind_subject_mask_shape_mismatch"
    frames = sorted(
        os.path.join(tmp_workdir, f) for f in os.listdir(tmp_workdir) if f.endswith(".png")
    )
    assert frames
    for f in frames:
        arr = np.array(Image.open(f).convert("RGBA"))
        assert arr.shape[:2] == (1080, 1920)
        assert arr[..., 3].max() == 0, f"{f}: full-occlusion mask must occlude on landscape"
