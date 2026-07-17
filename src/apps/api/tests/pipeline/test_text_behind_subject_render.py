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


def test_apply_subject_mask_shape_mismatch_fails_open():
    rgba = np.zeros((4, 4, 4), dtype=np.uint8)
    rgba[..., 3] = 255
    mask = np.ones((2, 2), dtype=np.float32)
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

    # matte was consulted once per rendered frame, at the frame's absolute t.
    assert len(matte.calls) == seq["n_frames"]
    assert matte.calls[0] == pytest.approx(0.0)
    assert matte.calls[-1] == pytest.approx((seq["n_frames"] - 1) / tos.FPS)

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


# -- Sequence-role overlays: behind_subject unsupported in v1 -----------------


def test_sequence_role_behind_subject_key_stripped_with_warning(tmp_workdir):
    overlays = [
        _sequence_overlay(text="first", start_s=0.0, end_s=3.0, behind_subject=True),
        _sequence_overlay(text="second", start_s=1.0, end_s=4.0, position_y_frac=0.5),
    ]
    with mock.patch.object(tos, "log", wraps=tos.log) as mock_log:
        sequences, work_dir = tos.render_text_overlay_sequences(overlays, tmp_workdir)
    assert sequences  # still renders — degrades, doesn't drop the overlay
    assert work_dir is not None
    mock_log.warning.assert_any_call(
        "text_behind_subject_unsupported_for_sequence_role", text="first"
    )


def test_sequence_role_behind_subject_does_not_reach_composite_with_matte_set(tmp_workdir):
    """Even when a matte IS supplied, sequence-role overlays never occlude —
    behind_subject is stripped before the role split, so the composite path
    (which has no matte hook) never sees it."""
    overlays = [
        _sequence_overlay(text="first", start_s=0.0, end_s=3.0, behind_subject=True),
        _sequence_overlay(text="second", start_s=1.0, end_s=4.0, position_y_frac=0.5),
    ]
    matte = _ConstantMatte(1.0)
    sequences, work_dir = tos.render_text_overlay_sequences(overlays, tmp_workdir, matte=matte)
    assert len(sequences) == 1  # composite still forms (>= 2 sequence overlays)
    assert sequences[0]["is_animated"] is True
    assert work_dir is not None
    # The composite renders "first" fully opaque during its window — proof
    # the (never-consulted) matte did not occlude it.
    assert len(matte.calls) == 0


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
