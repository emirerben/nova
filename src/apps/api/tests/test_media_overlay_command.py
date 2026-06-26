"""Pure unit tests for build_media_overlay_command (no ffmpeg, no I/O).

Guards:
- Per-card scale filter present (scale={cw}:-2,format=yuv420p — even height + correct colorspace).
- center → top-left overlay=x:y math on 1080x1920 canvas.
- enable='between(t,s,e)' present.
- Video cards get tpad clone; image cards get -loop 1.
- Cards chained in ascending z-order.
- Audio mapped only from input 0 (0:a?).
- preset="fast" present (encoder-policy gate).
"""

from __future__ import annotations

from app.agents._schemas.media_overlay import MediaOverlay
from app.pipeline.media_overlay import build_media_overlay_command


def _card_img(
    *,
    id_="img1",
    x_frac=0.5,
    y_frac=0.5,
    scale=0.4,
    start_s=1.0,
    end_s=4.0,
    z=0,
) -> MediaOverlay:
    return MediaOverlay(
        id=id_,
        kind="image",
        src_gcs_path="users/u/plan/p/overlays/img.jpg",
        position="custom",
        x_frac=x_frac,
        y_frac=y_frac,
        scale=scale,
        start_s=start_s,
        end_s=end_s,
        z=z,
    )


def _card_video(
    *,
    id_="vid1",
    x_frac=0.5,
    y_frac=0.18,  # top preset equivalent
    scale=0.35,
    start_s=0.0,
    end_s=3.0,
    z=1,
) -> MediaOverlay:
    return MediaOverlay(
        id=id_,
        kind="video",
        src_gcs_path="users/u/plan/p/overlays/clip.mp4",
        position="custom",
        x_frac=x_frac,
        y_frac=y_frac,
        scale=scale,
        start_s=start_s,
        end_s=end_s,
        z=z,
    )


def _build(cards, local_paths=None, widths_px=None):
    """Build command with sensible defaults for local_paths / widths."""
    if local_paths is None:
        local_paths = []
        for c in cards:
            if c.kind == "image":
                local_paths.append(f"/tmp/{c.id}.jpg")
            else:
                local_paths.append(f"/tmp/{c.id}.mp4")
    if widths_px is None:
        widths_px = [c.card_width_px() for c in cards]
    return build_media_overlay_command(
        "/tmp/base.mp4", cards, local_paths, widths_px, "/tmp/out.mp4"
    )


class TestImageCard:
    def test_loop_flag_present_for_image(self):
        card = _card_img()
        cmd = _build([card])
        assert "-loop" in cmd
        assert "1" in cmd

    def test_scale_filter_present(self):
        card = _card_img(scale=0.4)
        expected_w = card.card_width_px()  # round(0.4 * 1080) = 432
        cmd = _build([card])
        fc = cmd[cmd.index("-filter_complex") + 1]
        # -2 rounds height to nearest even (yuv420p chroma-subsampling safe); format=yuv420p
        assert f"scale={expected_w}:-2,format=yuv420p" in fc

    def test_enable_gate_present(self):
        card = _card_img(start_s=2.0, end_s=5.0)
        cmd = _build([card])
        fc = cmd[cmd.index("-filter_complex") + 1]
        assert "enable='between(t,2.000,5.000)'" in fc

    def test_overlay_top_left_math_centered(self):
        # Card at center (x=0.5, y=0.5), scale=0.4
        # cx=540, cw=432, ox=540-216=324
        # cy=960, oy = (960-overlay_h/2) expression
        card = _card_img(x_frac=0.5, y_frac=0.5, scale=0.4)
        cw = card.card_width_px()
        cx = round(0.5 * 1080)
        ox = cx - cw // 2
        cmd = _build([card])
        fc = cmd[cmd.index("-filter_complex") + 1]
        assert f"overlay={ox}:" in fc

    def test_no_tpad_for_image(self):
        card = _card_img()
        cmd = _build([card])
        fc = cmd[cmd.index("-filter_complex") + 1]
        assert "tpad" not in fc

    def test_eof_action_pass(self):
        card = _card_img()
        cmd = _build([card])
        fc = cmd[cmd.index("-filter_complex") + 1]
        assert "eof_action=pass" in fc


class TestVideoCard:
    def test_tpad_clone_present_for_video(self):
        card = _card_video()
        cmd = _build([card], local_paths=["/tmp/vid1.mp4"])
        fc = cmd[cmd.index("-filter_complex") + 1]
        assert "tpad=stop_mode=clone" in fc

    def test_no_loop_flag_for_video(self):
        card = _card_video()
        cmd = _build([card], local_paths=["/tmp/vid1.mp4"])
        # -loop should not appear for video cards
        assert "-loop" not in cmd

    def test_pts_shift_present(self):
        card = _card_video(start_s=5.0)
        cmd = _build([card], local_paths=["/tmp/vid1.mp4"])
        fc = cmd[cmd.index("-filter_complex") + 1]
        assert "PTS-STARTPTS+5.000/TB" in fc


class TestChaining:
    def test_two_cards_chained(self):
        img = _card_img(id_="img1", z=0)
        vid = _card_video(id_="vid1", z=1)
        cmd = _build([img, vid], local_paths=["/tmp/img1.jpg", "/tmp/vid1.mp4"])
        fc = cmd[cmd.index("-filter_complex") + 1]
        # Both overlay labels should appear
        assert "ov0" in fc
        assert "ov1" in fc

    def test_z_order_ascending(self):
        # z=1 card must be rendered AFTER z=0 card (higher z = on top = later in chain)
        low_z = _card_img(id_="low", z=0, start_s=0.0, end_s=2.0)
        high_z = _card_img(id_="high", z=1, start_s=0.0, end_s=2.0)
        cmd = _build([high_z, low_z], local_paths=["/tmp/high.jpg", "/tmp/low.jpg"])
        fc = cmd[cmd.index("-filter_complex") + 1]
        # ov0 is applied first (z=0 card), ov1 is applied on top (z=1 card)
        assert fc.index("ov0") < fc.index("ov1")


class TestAudioMapping:
    def test_only_base_audio_mapped(self):
        card = _card_img()
        cmd = _build([card])
        # 0:a? maps only the base input's audio — card audio intentionally dropped
        assert "0:a?" in cmd
        # No card audio streams should be mapped
        audio_maps = [cmd[i + 1] for i, x in enumerate(cmd) if x == "-map" and i + 1 < len(cmd)]
        for m in audio_maps:
            if m.startswith("1:") or m.startswith("2:"):
                assert False, f"Card audio stream mapped: {m}"


class TestEncoderPolicy:
    def test_preset_fast_in_command(self):
        card = _card_img()
        cmd = _build([card])
        # _encoding_args must produce preset=fast
        assert "-preset" in cmd
        assert "fast" in cmd

    def test_output_path_in_command(self):
        card = _card_img()
        cmd = _build([card])
        assert "/tmp/out.mp4" in cmd
