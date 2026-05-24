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
    # Top-anchored (not vertically centered): position_y_frac is the block TOP,
    # so content starts at/below 0.40*1920=768 — a centered block would start
    # well ABOVE that. Locks the _vertical_block_top fix on the wrap path.
    assert bbox[1] > 700, f"wrapped left-anchored block should be top-anchored; bbox {bbox}"


def test_left_anchor_cumulative_stage_prior_lines_stable():
    """REGRESSION (the "all previous words re-appear" bug, prod 89cde014): a
    cumulative left-anchored phrase that wraps to multiple lines must keep its
    earlier lines PINNED as each new word is revealed. Skia used to vertically
    CENTER the block, so adding a word re-centered everything and every prior
    line jumped. With _vertical_block_top top-anchoring left-anchored text, the
    block top stays fixed and the phrase only grows downward.

    Renders two consecutive cumulative stages of the same phrase and asserts the
    block TOP is identical, and the later (longer) stage extends further DOWN."""
    base = {
        "effect": "pop-in",
        "text_size_px": 120,
        "position_x_frac": 0.05,
        "position_y_frac": 0.40,
        "text_color": "#FFFFFF",
        "text_anchor": "left",
    }
    stage_a = {**base, "text": "Don't allow anyone to diminish you", "pop_animated_suffix": "you"}
    stage_b = {
        **base,
        "text": "Don't allow anyone to diminish you hard work and",
        "pop_animated_suffix": "and",
    }
    a_bbox, _ = _frame_bbox(stage_a, t_local=0.9, duration_s=1.5)
    b_bbox, _ = _frame_bbox(stage_b, t_local=0.9, duration_s=1.5)
    assert a_bbox is not None and b_bbox is not None
    # Both stages wrap to >1 line (the regression only manifests when wrapped).
    assert a_bbox[3] - a_bbox[1] > 200, f"stage A should wrap; bbox {a_bbox}"
    assert b_bbox[3] - b_bbox[1] > 200, f"stage B should wrap; bbox {b_bbox}"
    # Core guarantee: the block top does NOT move between stages (prior words
    # stay constant). Same font size + top anchor → identical top within AA.
    assert abs(a_bbox[1] - b_bbox[1]) <= 3, (
        f"cumulative reveal shifted the block top between stages: "
        f"stage A top {a_bbox[1]} vs stage B top {b_bbox[1]} — prior words moved."
    )
    # The longer stage grows DOWNWARD (more text → equal-or-lower bottom).
    assert b_bbox[3] >= a_bbox[3] - 1, (
        f"later stage should extend down, not up; A bottom {a_bbox[3]}, B bottom {b_bbox[3]}"
    )


def test_both_renderers_honor_vertical_anchor():
    """Parity guard, vertical leg of the #296 class: a wrapping left-anchored
    block must be TOP-anchored (position_y_frac = block top) in BOTH Skia and
    Pillow. Pillow already top-anchors left text; Skia did not until
    _vertical_block_top. Renders the same 2-line phrase through both and asserts
    each starts at/below position_y_frac*CANVAS_H (top-anchored, not centered)
    and that the two agree within a small tolerance."""
    cy = int(0.40 * tos.CANVAS_H)  # 768
    overlay = {
        "text": "combination of hard work",
        "effect": "none",
        "text_size_px": 120,
        "position_x_frac": 0.05,
        "position_y_frac": 0.40,
        "text_color": "#FFFFFF",
        "text_anchor": "left",
        "start_s": 0.0,
        "end_s": 2.0,
    }
    skia_bbox, _ = _frame_bbox(overlay)
    pillow_bbox, _ = _pillow_bbox(overlay)
    assert skia_bbox is not None and pillow_bbox is not None
    # Both wrap (multi-line) so the anchor mode is observable.
    assert skia_bbox[3] - skia_bbox[1] > 200, f"skia should wrap; bbox {skia_bbox}"
    assert pillow_bbox[3] - pillow_bbox[1] > 200, f"pillow should wrap; bbox {pillow_bbox}"
    # Top-anchored: block starts at/below cy. A centered block would start well
    # above cy (cy - block_h/2 ≈ cy - 140).
    assert skia_bbox[1] > cy - 40, f"skia not top-anchored; top {skia_bbox[1]} vs cy {cy}"
    assert pillow_bbox[1] > cy - 40, f"pillow not top-anchored; top {pillow_bbox[1]} vs cy {cy}"
    # The two renderers agree on the vertical origin within AA / metric slack.
    assert abs(skia_bbox[1] - pillow_bbox[1]) < 80, (
        f"renderers disagree on vertical anchor: skia top {skia_bbox[1]} vs "
        f"pillow top {pillow_bbox[1]} — the #296 class on the vertical axis."
    )


def test_center_anchor_stays_vertically_centered():
    """Lock the preserved default: center-anchored text keeps position_y_frac as
    the block CENTER (not the top). Guards against the _vertical_block_top fix
    accidentally top-anchoring centered templates too."""
    cy = 0.45 * tos.CANVAS_H
    overlay = {
        "text": "HELLO WORLD",
        "effect": "none",
        "text_size_px": 60,
        "position_x_frac": 0.5,
        "position_y_frac": 0.45,
        "text_color": "#FFFFFF",
        "text_anchor": "center",
    }
    bbox, _ = _frame_bbox(overlay)
    assert bbox is not None
    mid_y = (bbox[1] + bbox[3]) / 2.0
    assert abs(mid_y - cy) < 40, (
        f"center-anchored block should be centered on cy={cy:.0f}; got mid {mid_y:.0f}"
    )


def test_pop_in_suffix_has_no_bounce_full_size_from_start():
    """The revealed word appears at full size immediately — no 30→115→100
    scale bounce. The word-by-word reveal comes from per-stage timing, not a
    springy pop. Asserts the suffix bbox is identical early (t=0.02) and
    settled (t=0.9)."""
    import io

    ov = {
        "text": "the work",
        "effect": "pop-in",
        "pop_animated_suffix": "work",
        "text_size_px": 120,
        "position_x_frac": 0.05,
        "position_y_frac": 0.44,
        "text_color": "#FFFFFF",
        "text_anchor": "left",
    }

    def bbox(t):
        img = tos._draw_frame(ov, t, 1.0)
        return Image.open(io.BytesIO(bytes(img.encodeToData()))).convert("RGBA").getbbox()

    early, settled = bbox(0.02), bbox(0.9)
    assert early == settled, f"word scaled (bounce) between t=0.02 {early} and t=0.9 {settled}"
def test_left_anchor_karaoke_line_not_clipped():
    """karaoke-line was the last Skia draw path that still centered every line
    on position_x_frac unconditionally (text_overlay_skia._draw_karaoke_line),
    so a left-anchored music-lyric line would clip exactly like 89cde014. With
    the anchor branch it must fit inside the frame at every word."""
    overlay = {
        "text": "rain rain go away come again",
        "effect": "karaoke-line",
        "text_size_px": 100,
        "position_x_frac": 0.05,
        "position_y_frac": 0.45,
        "text_color": "#FFFFFF",
        "highlight_color": "#FFD24A",
        "text_anchor": "left",
        "word_timings": [
            {"text": "rain", "duration_cs": 40},
            {"text": "go", "duration_cs": 40},
            {"text": "away", "duration_cs": 40},
            {"text": "now", "duration_cs": 40},
        ],
    }
    # Mid-line time so a mix of sung/unsung words is drawn — all words render
    # regardless of sung state, so the bbox covers the full line. Before the
    # fix the line centered on x_frac=0.05 and the left half clipped to x<=1.
    bbox, width = _frame_bbox(overlay, t_local=1.0, duration_s=1.6)
    assert bbox is not None
    assert bbox[0] > 10, f"karaoke left edge clipped; bbox {bbox}"
    assert bbox[2] < width, f"karaoke right edge overflow; bbox {bbox}"


# -- Skia / Pillow renderer parity guard -------------------------------------
#
# The #296 class of bug: a field carried through the burn dict but honored by
# only ONE of the two renderers. Agentic templates + music jobs render via
# Skia; classic templates and the admin overlay-preview render via Pillow.
# Every local check (preview, Pillow) looked right while the burned Skia video
# clipped, because the two renderers disagreed on `text_anchor`. This guard
# renders the SAME overlay through both and asserts they honor the anchor
# contract the same way, so a field added to one renderer and dropped by the
# other fails CI instead of shipping.


def _pillow_bbox(overlay: dict):
    """Render one overlay through the Pillow path used by export + admin
    preview (render_overlays_at_time) and return the opaque-content bbox."""
    from app.pipeline.text_overlay import render_overlays_at_time

    with tempfile.TemporaryDirectory(prefix="pillow_parity_") as d:
        out = os.path.join(d, "p.png")
        render_overlays_at_time([overlay], 2.0, 1.0, out)
        im = Image.open(out).convert("RGBA")
        return im.getbbox(), im.width


@pytest.mark.parametrize("renderer", ["skia", "pillow"])
def test_both_renderers_honor_text_anchor(renderer):
    """All three anchors must place the line consistently in BOTH renderers:
    center→left shifts the left edge RIGHT by ~half the line width, and
    center→right shifts it LEFT by ~half. A renderer that ignores text_anchor
    produces a ~0 delta on the affected side and fails here — exactly the
    #296/ff0d2e1c regression class (left was the live bug; right was a latent
    sibling — Skia silently collapsed it to center while Pillow honored it),
    caught before it can ship.

    Anchored at x_frac=0.5 with a single non-wrapping ~400px line so all three
    anchorings fit fully in-frame (left ~540..940, center ~340..740, right
    ~140..540) and the contract is a clean half-line-width shift, not muddied
    by edge clipping (which floors the edge at 0) or word-wrap. The line must
    stay under ~half the frame width or right- and left-anchor can't both fit."""
    base = {
        "text": "HELLO WORLD",
        "effect": "none",
        "text_size_px": 50,
        "position_x_frac": 0.5,
        "position_y_frac": 0.45,
        "text_color": "#FFFFFF",
        "start_s": 0.0,
        "end_s": 2.0,
    }
    bbox_fn = _frame_bbox if renderer == "skia" else _pillow_bbox

    center_bbox, width = bbox_fn({**base, "text_anchor": "center"})
    left_bbox, _ = bbox_fn({**base, "text_anchor": "left"})
    right_bbox, _ = bbox_fn({**base, "text_anchor": "right"})
    assert center_bbox and left_bbox and right_bbox

    # All three fit fully inside the frame at the center position.
    for name, b in (("center", center_bbox), ("left", left_bbox), ("right", right_bbox)):
        assert b[0] > 0 and b[2] < width, f"{renderer}: {name} bbox {b} clips the frame"

    line_w = center_bbox[2] - center_bbox[0]
    left_delta = left_bbox[0] - center_bbox[0]   # left anchor sits RIGHT of center
    right_delta = right_bbox[0] - center_bbox[0]  # right anchor sits LEFT of center
    assert left_delta > 100, (
        f"{renderer} ignores text_anchor='left': center→left shifted the left "
        f"edge by only {left_delta}px (line {line_w}px wide, expected ~half). "
        f"This is the #296 class — a field honored by one renderer, dropped by the other."
    )
    assert right_delta < -100, (
        f"{renderer} ignores text_anchor='right': center→right shifted the left "
        f"edge by only {right_delta}px (line {line_w}px wide, expected ~-half). "
        f"This is the #296 class — a field honored by one renderer, dropped by the other."
    )
