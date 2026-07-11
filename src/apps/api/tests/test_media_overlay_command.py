"""Pure unit tests for build_media_overlay_command (no ffmpeg, no I/O).

Guards:
- Per-card scale filter present (scale={cw}:-2,format=yuv420p — even height + correct colorspace).
- center → top-left overlay=x:y math on 1080x1920 canvas.
- enable='between(t,s,e)' present.
- Video cards get tpad clone; image cards get -loop 1.
- Cards chained in ascending z-order.
- Audio mapped only from input 0 (0:a?).
- preset="veryfast" present (encoder-policy gate; see media_overlay.py module docstring).
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
        # _encoding_args must produce preset=veryfast (re-encoding CRF-18 content)
        assert "-preset" in cmd
        assert "veryfast" in cmd

    def test_output_path_in_command(self):
        card = _card_img()
        cmd = _build([card])
        assert "/tmp/out.mp4" in cmd


def _card_fullscreen_img(**kw) -> MediaOverlay:
    kw.setdefault("id_", "fs1")
    card = _card_img(**kw)
    return card.model_copy(update={"display_mode": "fullscreen"})


class TestFullscreenCard:
    """Plan 009 T1: cover-crop takeover branch."""

    def test_cover_crop_filter_string(self):
        cmd = _build([_card_fullscreen_img()])
        fc = cmd[cmd.index("-filter_complex") + 1]
        assert (
            "scale=1080:1920:force_original_aspect_ratio=increase"
            ",crop=1080:1920,setsar=1,format=yuv420p" in fc
        )

    def test_overlay_at_origin(self):
        cmd = _build([_card_fullscreen_img()])
        fc = cmd[cmd.index("-filter_complex") + 1]
        assert "overlay=0:0:" in fc
        # No runtime height expression for the fullscreen card.
        assert "overlay_h" not in fc

    def test_enable_gate_still_present(self):
        cmd = _build([_card_fullscreen_img(start_s=2.0, end_s=5.0)])
        fc = cmd[cmd.index("-filter_complex") + 1]
        assert "enable='between(t,2.000,5.000)'" in fc

    def test_trim_and_tpad_still_apply_for_fullscreen_video(self):
        card = _card_video(id_="fsv").model_copy(
            update={
                "display_mode": "fullscreen",
                "clip_trim_start_s": 1.0,
                "clip_trim_end_s": 2.0,
                "clip_duration_s": 4.0,
            }
        )
        cmd = _build([card], local_paths=["/tmp/fsv.mp4"])
        fc = cmd[cmd.index("-filter_complex") + 1]
        assert "trim=start=1.000:end=2.000" in fc
        # window 0-3s, trim covers 1s → 2s clone pad
        assert "tpad=stop_mode=clone:stop_duration=2.000" in fc

    def test_mixed_set_keeps_pip_fit_width_pins(self):
        pip = _card_img(id_="pip1", scale=0.4)
        fs = _card_fullscreen_img(id_="fs2", z=1)
        cmd = _build([pip, fs], local_paths=["/tmp/pip1.jpg", "/tmp/fs2.jpg"])
        fc = cmd[cmd.index("-filter_complex") + 1]
        assert f"scale={pip.card_width_px()}:-2,format=yuv420p" in fc
        assert "crop=1080:1920" in fc


def _preset_of(cmd: list[str]) -> str:
    return cmd[cmd.index("-preset") + 1]


class TestEncoderPolicyModes:
    """Plan 009 E7: dual preset assertions — the REAL gate for this module
    (media_overlay.py is not in the encoder-policy AST audit list, so these
    tests are what pins the mode-dependent preset decision)."""

    def test_pip_only_pass_uses_veryfast(self):
        cmd = _build([_card_img()])
        assert _preset_of(cmd) == "veryfast"

    def test_any_fullscreen_pass_uses_fast_not_veryfast(self):
        cmd = _build(
            [_card_img(id_="p"), _card_fullscreen_img(id_="f", z=1)],
            local_paths=["/tmp/p.jpg", "/tmp/f.jpg"],
        )
        assert _preset_of(cmd) == "fast"
        assert "veryfast" not in cmd

    def test_force_veryfast_overrides_fullscreen(self):
        card = _card_fullscreen_img()
        cmd = build_media_overlay_command(
            "/tmp/base.mp4",
            [card],
            ["/tmp/fs1.jpg"],
            [card.card_width_px()],
            "/tmp/out.mp4",
            force_veryfast=True,
        )
        assert _preset_of(cmd) == "veryfast"


class TestPassBudgetDeadlineClamp:
    """R4-2: apply_media_overlays' shared budget was sized for a task that STARTS
    with the pass; caption tasks enter it mid-task and thread a wall-clock
    deadline. _resolve_pass_budget clamps the budget/timeout to what's left and
    fails fast below the floor. Default (None) is byte-identical."""

    def test_default_none_is_byte_identical(self):
        from app.pipeline import media_overlay as mo

        assert mo._resolve_pass_budget(True, None) == (
            mo._TIMEOUT_FULLSCREEN_S,
            float(mo._FULLSCREEN_TOTAL_BUDGET_S),
        )
        assert mo._resolve_pass_budget(False, None) == (
            mo._TIMEOUT_PIP_S,
            float(mo._FULLSCREEN_TOTAL_BUDGET_S),
        )

    def test_deadline_clamps_budget_and_timeout(self, monkeypatch):
        from app.pipeline import media_overlay as mo

        monkeypatch.setattr(mo.time, "monotonic", lambda: 1000.0)
        # 400s left < both the 900s fullscreen attempt timeout and 1500s budget.
        timeout_s, budget_s = mo._resolve_pass_budget(True, 1400.0)
        assert budget_s == 400.0
        assert timeout_s == 400

    def test_deadline_far_away_keeps_standalone_numbers(self, monkeypatch):
        from app.pipeline import media_overlay as mo

        monkeypatch.setattr(mo.time, "monotonic", lambda: 1000.0)
        timeout_s, budget_s = mo._resolve_pass_budget(True, 1000.0 + 10_000.0)
        assert budget_s == float(mo._FULLSCREEN_TOTAL_BUDGET_S)
        assert timeout_s == mo._TIMEOUT_FULLSCREEN_S

    def test_below_floor_fails_fast_with_clear_error(self, monkeypatch):
        import pytest

        from app.pipeline import media_overlay as mo

        monkeypatch.setattr(mo.time, "monotonic", lambda: 1000.0)
        with pytest.raises(mo.MediaOverlayError, match="task deadline"):
            mo._resolve_pass_budget(True, 1000.0 + mo._DEADLINE_FLOOR_S - 1)
