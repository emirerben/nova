"""Unit tests for the Skia text-overlay renderer.

Covers the public API surface used by the orchestrators plus the internal
effect implementations. Renders are validated by reading the PNG output and
checking alpha/color sentinels at known coordinates — no eyeball tests.
"""

from __future__ import annotations

import os
import tempfile
from unittest import mock

import pytest
import skia
from PIL import Image

from app.pipeline import text_overlay_skia as tos


@pytest.fixture
def tmp_workdir():
    with tempfile.TemporaryDirectory(prefix="skia_test_") as d:
        yield d


# -- Typeface preload --------------------------------------------------------


def test_typeface_cache_preloads_every_registered_font():
    """All fonts in font-registry.json should be available without any
    runtime MakeFromFile calls."""
    from app.pipeline.text_overlay import _FONT_REGISTRY, FONTS_DIR

    expected = {
        os.path.join(FONTS_DIR, e["file"])
        for e in _FONT_REGISTRY.get("fonts", {}).values()
        if os.path.exists(os.path.join(FONTS_DIR, e["file"]))
    }
    assert expected.issubset(tos._TYPEFACE_BY_PATH.keys())
    # And every loaded typeface is a real skia Typeface, not None
    for tf in tos._TYPEFACE_BY_PATH.values():
        assert isinstance(tf, skia.Typeface)


def test_typeface_for_overlay_resolves_font_family():
    overlay = {"font_family": "Playfair Display"}
    tf = tos._typeface_for_overlay(overlay)
    assert isinstance(tf, skia.Typeface)


def test_typeface_for_overlay_falls_back_when_font_unknown():
    overlay = {"font_family": "DefinitelyNotAFont"}
    tf = tos._typeface_for_overlay(overlay)
    # Should fall back to the final Playfair Display Bold path
    assert isinstance(tf, skia.Typeface)


# -- Static effect (effect=none) ---------------------------------------------


def test_static_overlay_produces_single_png_with_alpha(tmp_workdir):
    overlay = {
        "text": "TEST",
        "start_s": 0.0,
        "end_s": 1.0,
        "position": "center",
        "effect": "none",
        "font_family": "Playfair Display",
        "text_size_px": 100,
        "text_color": "#FFFFFF",
        "outline_px": 2,
    }
    seq = tos._generate_overlay_sequence(overlay, tmp_workdir, 0)
    assert seq is not None
    assert seq["n_frames"] == 1
    assert seq["is_animated"] is False
    assert os.path.exists(seq["first_frame"])

    img = Image.open(seq["first_frame"])
    assert img.mode == "RGBA"
    assert img.size == (tos.CANVAS_W, tos.CANVAS_H)
    # Corners are transparent — no opaque background bug
    assert img.getpixel((0, 0))[3] == 0
    assert img.getpixel((img.width - 1, img.height - 1))[3] == 0
    # The text sits around y_frac=0.45 (baseline at ~864 for 1920 canvas).
    # Scan a broad horizontal band that covers the glyph height range.
    has_opaque = False
    for y in range(int(0.30 * img.height), int(0.55 * img.height), 8):
        for x in range(int(0.3 * img.width), int(0.7 * img.width), 8):
            if img.getpixel((x, y))[3] > 200:
                has_opaque = True
                break
        if has_opaque:
            break
    assert has_opaque, "expected at least one opaque pixel near the centered text"


def test_zero_duration_overlay_returns_none(tmp_workdir):
    overlay = {"text": "X", "start_s": 1.0, "end_s": 1.0}
    assert tos._generate_overlay_sequence(overlay, tmp_workdir, 0) is None


def test_empty_text_overlay_returns_none(tmp_workdir):
    # Run through the full pipeline so validation runs
    overlays = [{"text": "", "start_s": 0.0, "end_s": 1.0, "effect": "none"}]
    seqs = tos._render_overlay_sequences(overlays, 2.0, tmp_workdir)
    assert seqs == []


# -- Animated effects --------------------------------------------------------


def test_font_cycle_produces_capped_frame_sequence(tmp_workdir):
    overlay = {
        "text": "CYCLE",
        "start_s": 0.0,
        "end_s": 10.0,  # 10s * 30fps = 300 frames if uncapped
        "effect": "font-cycle",
        "font_family": "Playfair Display",
        "text_size_px": 120,
        "text_color": "#FFFFFF",
    }
    seq = tos._generate_overlay_sequence(overlay, tmp_workdir, 0)
    assert seq is not None
    assert seq["is_animated"] is True
    # MAX_OVERLAY_FRAMES bounds the count even with a long duration
    assert seq["n_frames"] <= tos.MAX_OVERLAY_FRAMES
    # Each frame file exists
    frames = sorted(f for f in os.listdir(tmp_workdir) if f.endswith(".png"))
    assert len(frames) == seq["n_frames"]


def test_font_cycle_selects_settle_font_at_end():
    """Last frames should land in the settle phase (settle font)."""
    overlay = {"font_family": "Playfair Display"}
    settle = tos._select_cycle_font(overlay, t_local=2.6, duration_s=3.0)
    # 2.6/3.0 > 1 - SETTLE_RATIO(0.30) = 0.70 → settle
    assert settle == "Playfair Display"


def test_font_cycle_alternates_between_settle_and_contrast():
    overlay = {"font_family": "Playfair Display"}
    # At step=1, should pick a contrast font (settle alternates every other step)
    contrast = tos._select_cycle_font(overlay, t_local=0.15, duration_s=10.0)
    assert contrast in tos._CYCLE_CONTRAST_NAMES


def test_pop_in_with_suffix_renders_both_layers(tmp_workdir):
    overlay = {
        "text": "one small step",
        "start_s": 0.0,
        "end_s": 1.0,
        "position": "center",
        "effect": "pop-in",
        "pop_animated_suffix": "step",
        "font_family": "Inter",
        "text_size_px": 80,
        "text_color": "#FFFFFF",
    }
    seq = tos._generate_overlay_sequence(overlay, tmp_workdir, 0)
    assert seq is not None
    assert seq["is_animated"] is True
    # Verify the first frame contains opaque text pixels (prefix is rendered
    # static at scale 1.0, so even t=0 has prefix visible). Scan a band that
    # covers the y=0.45 anchor + nearby glyph rows.
    img = Image.open(seq["first_frame"])
    has_opaque_prefix = False
    for y in range(int(0.30 * img.height), int(0.55 * img.height), 8):
        if any(img.getpixel((x, y))[3] > 200 for x in range(100, img.width - 100, 20)):
            has_opaque_prefix = True
            break
    assert has_opaque_prefix


def test_pop_in_without_suffix_falls_back_to_plain_pop_in(tmp_workdir):
    overlay = {
        "text": "OOPS",
        "start_s": 0.0,
        "end_s": 1.0,
        "position": "center",
        "effect": "pop-in",
        # No pop_animated_suffix → should still animate as plain pop-in
        "font_family": "Inter",
        "text_size_px": 100,
        "text_color": "#FFFFFF",
    }
    seq = tos._generate_overlay_sequence(overlay, tmp_workdir, 0)
    assert seq is not None
    assert seq["is_animated"] is True


def test_karaoke_line_highlights_after_word_end_time(tmp_workdir):
    """First word's end_cs = 50 → 0.5s. At t=0.6s the first word is sung
    (highlight color), the rest are still primary."""
    overlay = {
        "text": "uno dos tres",
        "start_s": 0.0,
        "end_s": 2.0,
        "position": "center",
        "effect": "karaoke-line",
        "word_timings": [
            {"text": "uno", "duration_cs": 50},
            {"text": "dos", "duration_cs": 50},
            {"text": "tres", "duration_cs": 50},
        ],
        "font_family": "Inter",
        "text_size_px": 100,
        "text_color": "#FFFFFF",
        "highlight_color": "#FFD24A",  # gold
    }
    seq = tos._generate_overlay_sequence(overlay, tmp_workdir, 0)
    assert seq is not None
    # Frame at t=0.6s ≈ frame index 18 at 30fps
    frame_18 = os.path.join(
        tmp_workdir, f"skia_overlay_000_f{18:04d}.png"
    )
    assert os.path.exists(frame_18)
    img = Image.open(frame_18)
    # Look for a gold-ish pixel anywhere in the middle band of the image
    # (the first "sung" word should be gold)
    found_gold = False
    for y in range(img.height // 2 - 80, img.height // 2 + 80, 8):
        for x in range(0, img.width, 8):
            r, g, b, a = img.getpixel((x, y))
            # Gold #FFD24A = (255, 210, 74)
            if a > 200 and r > 220 and 180 < g < 230 and b < 120:
                found_gold = True
                break
        if found_gold:
            break
    assert found_gold, "expected at least one gold pixel for the sung first word"


def test_animated_effects_all_produce_sequences(tmp_workdir):
    base = {
        "text": "TEST",
        "start_s": 0.0,
        "end_s": 1.0,
        "position": "center",
        "font_family": "Inter",
        "text_size_px": 80,
        "text_color": "#FFFFFF",
    }
    for effect in ("scale-up", "fade-in", "typewriter", "slide-up", "slide-down", "bounce"):
        ov = {**base, "effect": effect}
        seq = tos._generate_overlay_sequence(ov, tmp_workdir, hash(effect) & 0xFF)
        assert seq is not None, f"{effect} returned None"
        assert seq["is_animated"] is True, f"{effect} not marked animated"
        assert seq["n_frames"] > 1


# -- Layout edge cases -------------------------------------------------------


def test_word_wrap_splits_long_lines():
    """A long sentence should split into multiple lines that each fit
    inside 0.9 * canvas width."""
    tf = tos._typeface_for_overlay({"font_family": "Inter"})
    font = skia.Font(tf, 72)
    long_text = "the quick brown fox jumps over the lazy dog repeatedly"
    lines = tos._wrap_text_to_lines(long_text, font, tos.CANVAS_W * 0.9)
    assert len(lines) >= 2
    for ln in lines:
        assert font.measureText(ln) <= tos.CANVAS_W * 0.9


def test_word_wrap_preserves_explicit_newlines():
    tf = tos._typeface_for_overlay({"font_family": "Inter"})
    font = skia.Font(tf, 72)
    lines = tos._wrap_text_to_lines("first\nsecond", font, tos.CANVAS_W * 0.9)
    assert lines == ["first", "second"]


def test_shrink_to_fit_reduces_font_for_single_long_word():
    """A single un-breakable word that's too wide at the requested size
    should trigger the shrink-to-fit loop."""
    tf = tos._typeface_for_overlay({"font_family": "Inter"})
    # Pick a size that's clearly too big for a 30-char single word
    long_word = "a" * 40
    font, final_size, lines = tos._shrink_to_fit(
        long_word, tf, initial_size=300, max_width=tos.CANVAS_W * 0.9
    )
    assert final_size < 300  # actually shrank
    assert final_size >= tos._MIN_FONT_SIZE


# -- PNG encoder choice ------------------------------------------------------


def test_png_encoder_uses_pillow(tmp_workdir):
    """The Skia renderer must write PNGs through Pillow, not through Skia's
    built-in encoder. Mocked verification."""
    surf = skia.Surfaces.MakeRasterN32Premul(100, 100)
    canvas = surf.getCanvas()
    canvas.clear(skia.ColorTRANSPARENT)
    img = surf.makeImageSnapshot()
    out_path = os.path.join(tmp_workdir, "out.png")

    with mock.patch("app.pipeline.text_overlay_skia.Image") as mock_pil_image:
        mock_inst = mock.MagicMock()
        mock_pil_image.frombytes.return_value = mock_inst
        tos._write_png_pillow(img, out_path)

        mock_pil_image.frombytes.assert_called_once()
        mock_inst.save.assert_called_once_with(out_path, "PNG", compress_level=3)


# -- player-card refusal -----------------------------------------------------


def test_player_card_raises_not_implemented(tmp_workdir):
    overlay = {
        "text": "10",
        "start_s": 0.0,
        "end_s": 2.0,
        "effect": "player-card",
        "jersey_no": "10",
        "player_name": "MESSI",
    }
    with pytest.raises(NotImplementedError, match="player-card"):
        tos._draw_frame(overlay, 0.0, 2.0)


# -- Kill switch + dispatch (integration with _burn_text_overlays wrapper) ---


def test_kill_switch_routes_to_pillow_when_disabled(tmp_workdir):
    """When settings.text_renderer_skia_enabled is False, the dispatch in
    _burn_text_overlays should NOT invoke the Skia path even when
    use_skia=True is requested."""
    from app.tasks.template_orchestrate import _burn_text_overlays

    # A trivially-formatted overlay; the test doesn't actually need ffmpeg to
    # succeed — we only assert routing.
    with mock.patch("app.pipeline.text_overlay_skia.burn_text_overlays_skia") as skia_fn, \
         mock.patch("app.pipeline.text_overlay.generate_text_overlay_png") as pil_png, \
         mock.patch("app.pipeline.text_overlay.generate_animated_overlay_ass") as pil_ass, \
         mock.patch("subprocess.run") as run_, \
         mock.patch("app.config.settings") as fake_settings:

        fake_settings.text_renderer_skia_enabled = False
        # Make the Pillow generators return empty so the orchestrator short-
        # circuits to a copy without actually invoking ffmpeg.
        pil_png.return_value = None
        pil_ass.return_value = None
        run_.return_value = mock.MagicMock(returncode=0, stderr=b"")

        # Touch input file so the copy fallback doesn't fail
        in_path = os.path.join(tmp_workdir, "in.mp4")
        out_path = os.path.join(tmp_workdir, "out.mp4")
        with open(in_path, "wb") as f:
            f.write(b"\x00")

        _burn_text_overlays(
            in_path,
            [{"text": "X", "start_s": 0, "end_s": 1, "effect": "none"}],
            out_path,
            tmp_workdir,
            use_skia=True,
        )
        # Skia should NOT have been invoked — kill switch is off.
        skia_fn.assert_not_called()


def test_dispatch_routes_to_skia_when_use_skia_true(tmp_workdir):
    """use_skia=True + settings.text_renderer_skia_enabled=True → Skia."""
    from app.tasks.template_orchestrate import _burn_text_overlays

    with mock.patch(
        "app.pipeline.text_overlay_skia.burn_text_overlays_skia"
    ) as skia_fn, mock.patch("app.config.settings") as fake_settings:
        fake_settings.text_renderer_skia_enabled = True
        in_path = os.path.join(tmp_workdir, "in.mp4")
        out_path = os.path.join(tmp_workdir, "out.mp4")
        with open(in_path, "wb") as f:
            f.write(b"\x00")

        _burn_text_overlays(
            in_path,
            [{"text": "X", "start_s": 0, "end_s": 1, "effect": "none"}],
            out_path,
            tmp_workdir,
            use_skia=True,
        )
        skia_fn.assert_called_once()


def test_pre_burn_curtain_routes_to_skia_when_agentic(tmp_workdir):
    """`_pre_burn_curtain_slot_text(is_agentic=True)` must dispatch to the
    Skia sibling. Verifies the second of the three orchestrator call sites."""
    from app.tasks.template_orchestrate import _pre_burn_curtain_slot_text

    class FakeStep:
        slot = {
            "text_overlays": [
                {
                    "text": "WORLD",
                    "start_s": 0.0,
                    "end_s": 1.0,
                    "position": "center",
                    "effect": "none",
                    "role": "label",
                    "sample_text": "WORLD",
                    "font_family": "Inter",
                    "text_size_px": 80,
                    "text_color": "#FFFFFF",
                }
            ]
        }
        clip_id = "x"

    in_path = os.path.join(tmp_workdir, "in.mp4")
    with open(in_path, "wb") as f:
        f.write(b"\x00")

    with mock.patch(
        "app.pipeline.text_overlay_skia.pre_burn_curtain_slot_text_skia"
    ) as skia_pre, mock.patch("app.config.settings") as fake_settings:
        fake_settings.text_renderer_skia_enabled = True
        _pre_burn_curtain_slot_text(
            in_path,
            FakeStep(),
            slot_dur=2.0,
            clip_metas=None,
            subject="",
            slot_idx=0,
            tmpdir=tmp_workdir,
            inter=None,
            is_agentic=True,
        )
        skia_pre.assert_called_once()


def test_dispatch_keeps_classic_on_pillow(tmp_workdir):
    """use_skia=False (the classic-template default) → never touches Skia,
    regardless of the kill-switch state."""
    from app.tasks.template_orchestrate import _burn_text_overlays

    with mock.patch(
        "app.pipeline.text_overlay_skia.burn_text_overlays_skia"
    ) as skia_fn, mock.patch(
        "app.pipeline.text_overlay.generate_text_overlay_png"
    ) as pil_png, mock.patch(
        "app.pipeline.text_overlay.generate_animated_overlay_ass"
    ) as pil_ass, mock.patch("subprocess.run") as run_, mock.patch(
        "app.config.settings"
    ) as fake_settings:
        fake_settings.text_renderer_skia_enabled = True  # ON globally
        pil_png.return_value = None
        pil_ass.return_value = None
        run_.return_value = mock.MagicMock(returncode=0, stderr=b"")
        in_path = os.path.join(tmp_workdir, "in.mp4")
        out_path = os.path.join(tmp_workdir, "out.mp4")
        with open(in_path, "wb") as f:
            f.write(b"\x00")

        _burn_text_overlays(
            in_path,
            [{"text": "X", "start_s": 0, "end_s": 1, "effect": "none"}],
            out_path,
            tmp_workdir,
            use_skia=False,
        )
        skia_fn.assert_not_called()


# -- Left-anchor positioning (prod 89cde014 left-clip regression) ------------


def _frame_bbox(overlay: dict, t_local: float = 1.0, duration_s: float = 2.0):
    """Render one frame via _draw_frame and return the opaque-content bbox."""
    import io

    img = tos._draw_frame(overlay, t_local, duration_s)
    im = Image.open(io.BytesIO(bytes(img.encodeToData()))).convert("RGBA")
    return im.getbbox(), im.width


def test_left_anchor_does_not_clip_off_left_edge():
    """REGRESSION: a left-anchored wide line (Layer-2 uses text_anchor='left'
    + position_x_frac=0.05) must render its full text inside the frame. Before
    the fix, Skia centered every line on position_x_frac, so the 5% point
    became the line CENTER and the left half clipped off-screen — prod
    template 89cde014 rendered "It's not just luck" as "s not just luck"."""
    base = {
        "text": "It's not just luck",
        "effect": "pop-in",
        "pop_animated_suffix": "luck",
        "text_size_px": 120,
        "position_x_frac": 0.05,
        "position_y_frac": 0.44,
        "text_color": "#FFFFFF",
    }
    # Center-anchor (old behavior) clips: content starts at x=0.
    center_bbox, _ = _frame_bbox({**base, "text_anchor": "center"})
    assert center_bbox is not None and center_bbox[0] <= 1, (
        "expected center-anchor to clip at the left edge (the bug); "
        f"got bbox {center_bbox}"
    )
    # Left-anchor (fixed) fits: content starts well inside the frame and ends
    # before the right edge.
    left_bbox, width = _frame_bbox({**base, "text_anchor": "left"})
    assert left_bbox is not None
    assert left_bbox[0] > 10, f"left-anchored text should not touch left edge; bbox {left_bbox}"
    assert left_bbox[2] < width, f"left-anchored text should fit inside frame; bbox {left_bbox}"


def test_left_anchor_static_overlay_not_clipped():
    """Same guarantee for a non-animated (static) left-anchored overlay, which
    routes through _draw_centered_text directly."""
    overlay = {
        "text": "combination of",
        "effect": "none",
        "text_size_px": 120,
        "position_x_frac": 0.05,
        "position_y_frac": 0.41,
        "text_color": "#FFFFFF",
        "text_anchor": "left",
    }
    bbox, width = _frame_bbox(overlay)
    assert bbox is not None
    assert bbox[0] > 10, f"left edge clipped; bbox {bbox}"
    assert bbox[2] < width, f"right edge overflow; bbox {bbox}"


def test_pop_in_suffix_wide_line_wraps_instead_of_clipping():
    """REGRESSION: a pop-in-with-suffix line too wide for one line must wrap
    (fall back to the whole-line pop) instead of clipping off the right edge.
    A manually-edited cumulative phrase grown past ~90% canvas width
    ("combination of hard work" at 120px) overflowed because the suffix-pop
    layout placed prefix+suffix on a single baseline."""
    import io

    overlay = {
        "text": "combination of hard work",
        "effect": "pop-in",
        "pop_animated_suffix": "work",
        "text_size_px": 120,
        "position_x_frac": 0.05,
        "position_y_frac": 0.40,
        "text_color": "#FFFFFF",
        "text_anchor": "left",
    }
    img = tos._draw_frame(overlay, 0.9, 1.5)  # past the pop animation
    im = Image.open(io.BytesIO(bytes(img.encodeToData()))).convert("RGBA")
    bbox = im.getbbox()
    assert bbox is not None
    assert bbox[2] < im.width, f"wide pop-in line clipped the right edge; bbox {bbox}"
    # Wrapped to >1 line: content spans more vertical room than a single line.
    assert bbox[3] - bbox[1] > 200, f"expected multi-line wrap; bbox {bbox}"
