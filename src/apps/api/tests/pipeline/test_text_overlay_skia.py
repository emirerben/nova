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


def _has_gold_pixel(
    img: Image.Image,
    *,
    x_start: int = 0,
    x_end: int | None = None,
    y_start: int | None = None,
    y_end: int | None = None,
) -> bool:
    x_end = img.width if x_end is None else x_end
    y_start = img.height // 2 - 80 if y_start is None else y_start
    y_end = img.height // 2 + 80 if y_end is None else y_end
    for y in range(max(0, y_start), min(img.height, y_end), 8):
        for x in range(max(0, x_start), min(img.width, x_end), 8):
            r, g, b, a = img.getpixel((x, y))
            # Gold #FFD24A = (255, 210, 74), with antialiasing tolerance.
            if a > 160 and r > 220 and 165 < g < 235 and 20 < b < 140:
                return True
    return False


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


def test_resolved_typeface_reports_unknown_font_fallback():
    resolved = tos.resolved_typeface_for_overlay(
        {"font_family": "DefinitelyNotAFont", "font_style": "display"}
    )
    assert resolved["requested_font_family"] == "DefinitelyNotAFont"
    assert resolved["name"] != "DefinitelyNotAFont"
    assert resolved["fallback"] is True


def test_font_cycle_reports_actual_settle_typeface_not_style_default():
    resolved = tos.resolved_typeface_for_overlay({"effect": "font-cycle", "font_style": "sans"})
    assert resolved["name"] == "Playfair Display"
    assert resolved["source"] == "font_cycle_settle"
    assert resolved["fallback"] is False


def test_unknown_font_family_emits_fallback_font_resolved_event(tmp_workdir):
    overlay = {
        "text": "fallback font",
        "effect": "static",
        "font_family": "DefinitelyNotAFont",
        "font_style": "display",
        "text_size_px": 80,
        "start_s": 0.0,
        "end_s": 1.0,
    }
    with mock.patch("app.services.pipeline_trace.record_pipeline_event") as record:
        seq = tos._generate_overlay_sequence(overlay, tmp_workdir, 7)
    assert seq is not None
    record.assert_called_once()
    stage, event, payload = record.call_args.args
    assert stage == "overlay"
    assert event == "font_resolved"
    assert payload["requested_font_family"] == "DefinitelyNotAFont"
    assert payload["fallback"] is True
    assert payload["level"] == "warning"


def test_font_resolved_event_no_trace_context_does_not_raise(tmp_workdir):
    overlay = {
        "text": "no trace context",
        "effect": "static",
        "font_family": "DefinitelyNotAFont",
        "text_size_px": 80,
        "start_s": 0.0,
        "end_s": 1.0,
    }
    assert tos._generate_overlay_sequence(overlay, tmp_workdir, 8) is not None


# -- Turkish-diacritic glyph coverage (TR regression) -------------------------
#
# Adding Turkish-language support to intro_writer/overlay_format_matcher is
# necessary but not sufficient: the model can produce ç ş ğ ı İ ö ü perfectly,
# but if the bundled typeface lacks those glyphs Skia draws .notdef (tofu box)
# with no error log. These tests lock the contract: every codepoint Turkish
# creators actually use MUST be present in EVERY bundled typeface that can
# ever be selected to render a TR overlay.

# Codepoints the TR_DIACRITICS_REQUIRED set is built from. Sourced from the
# canonical Turkish alphabet — lower + upper case — plus the dotless/dotted i
# pair which is the most common font-fallback failure on Turkish text.
_TR_DIACRITICS = "çÇşŞğĞıİöÖüÜ"


def test_assert_glyphs_present_raises_on_missing():
    # A null-font reference would be the simplest test, but Skia rejects it;
    # use a real typeface and pick a codepoint nothing in our bundle covers
    # (a CJK ideograph — Playfair Display is a Latin serif).
    from app.pipeline.text_overlay import FONTS_DIR

    tf = skia.Typeface.MakeFromFile(os.path.join(FONTS_DIR, "PlayfairDisplay-Bold.ttf"))
    with pytest.raises(tos.MissingGlyphsError) as exc:
        tos.assert_glyphs_present(tf, "hello 漢字")
    # Error must name the missing codepoints so an operator can grep.
    assert "U+6F22" in str(exc.value)
    assert "U+5B57" in str(exc.value)


def test_assert_glyphs_present_passes_for_ascii():
    from app.pipeline.text_overlay import FONTS_DIR

    tf = skia.Typeface.MakeFromFile(os.path.join(FONTS_DIR, "PlayfairDisplay-Bold.ttf"))
    tos.assert_glyphs_present(tf, "hello world 12345")  # no raise


def test_playfair_display_bold_covers_all_turkish_diacritics():
    # THE regression test. If this fails after a font-bundle change, Turkish
    # creators see tofu boxes on their hero overlay and the renderer logs nothing.
    from app.pipeline.text_overlay import FONTS_DIR

    tf = skia.Typeface.MakeFromFile(os.path.join(FONTS_DIR, "PlayfairDisplay-Bold.ttf"))
    tos.assert_glyphs_present(tf, _TR_DIACRITICS)


# Bundled fonts that knowingly LACK Turkish glyph coverage. Each entry should
# point to the file name in font-registry.json. These fonts MUST NOT be selected
# to render a TR overlay — see TODO below. Adding a font here is a deliberate
# scope decision: it means "we ship this font for EN-only contexts and accept it
# never renders TR jobs". The test below uses this set to allow exceptions without
# silently regressing the rest of the bundle.
_TR_UNSAFE_FONTS: set[str] = {
    # Permanent Marker is a stylized handwriting font that lacks ş Ş ğ Ğ İ. It's
    # registered as a font-cycle contrast font; if a TR generative overlay hits
    # the font-cycle effect the cycle could land on this font mid-animation.
    # TODO(tr-cycle-filter): the cycle-font selector in text_overlay_skia.py /
    # text_overlay.py should consult a `tr_safe` flag in the registry and skip
    # non-TR-safe fonts when the job's language is "tr". Until that ships, the
    # only reachable path for PermanentMarker on a TR job is the font-cycle
    # effect AND a glyph-bearing letter landing on a PermanentMarker frame;
    # the form matcher's TR hint biases toward simpler effects which sharply
    # narrows this exposure.
    "PermanentMarker-Regular.ttf",
    # Satisfy added in v0.4.94.0 — script font lacks Turkish diacritics (İ ı Ş ş Ğ ğ).
    # Great Vibes and Patrick Hand DO cover the TR diacritic set and are NOT listed here.
    # Same TODO(tr-cycle-filter) applies.
    "Satisfy-Regular.ttf",
    # Rascal is a decorative handwriting face (limited_charset: true) — lacks all
    # 12 Turkish diacritics. EN-only use; never rendered for TR jobs.
    "Rascal-Regular.ttf",
    # Big Curls is a decorative script face (limited_charset: true) — lacks ş Ş ğ Ğ ı İ.
    # EN-only use; never rendered for TR jobs.
    "BigCurls-Regular.ttf",
    # Alte Haas Grotesk Bold is missing ş Ş ğ Ğ İ from the Turkish diacritic set.
    # EN-only use; never rendered for TR jobs.
    "AlteHaasGrotesk-Bold.ttf",
}


def test_tr_safe_bundled_typefaces_cover_turkish_diacritics():
    # Bundle-wide invariant minus the documented exceptions: every TR-safe font
    # MUST cover the Turkish diacritic alphabet. Adding a font that fails this
    # test forces a deliberate choice: fix the glyph coverage, or add the font
    # name to _TR_UNSAFE_FONTS and accept the EN-only scope.
    failures: list[tuple[str, str]] = []
    for path, tf in tos._TYPEFACE_BY_PATH.items():
        if os.path.basename(path) in _TR_UNSAFE_FONTS:
            continue
        try:
            tos.assert_glyphs_present(tf, _TR_DIACRITICS)
        except tos.MissingGlyphsError as exc:
            failures.append((os.path.basename(path), str(exc)))
    assert not failures, (
        "TR-safe bundled fonts missing Turkish glyph coverage — TR renders would tofu-box:\n"
        + "\n".join(f"  {f}: {msg}" for f, msg in failures)
        + "\n\nFix the font, or add the file to _TR_UNSAFE_FONTS with a TODO."
    )


def test_tr_unsafe_set_only_lists_fonts_that_actually_lack_glyphs():
    # Sentinel for the exception list: if a "TR-unsafe" font is upgraded (e.g.
    # we replace PermanentMarker with a variant covering Turkish), drop it from
    # the set. This test fails when an entry is no longer needed so the exception
    # list doesn't ossify into bit-rot.
    from app.pipeline.text_overlay import FONTS_DIR

    falsely_unsafe: list[str] = []
    for name in _TR_UNSAFE_FONTS:
        path = os.path.join(FONTS_DIR, name)
        if not os.path.exists(path):
            continue
        tf = skia.Typeface.MakeFromFile(path)
        try:
            tos.assert_glyphs_present(tf, _TR_DIACRITICS)
            falsely_unsafe.append(name)
        except tos.MissingGlyphsError:
            pass
    assert not falsely_unsafe, (
        f"{falsely_unsafe} are listed as TR-unsafe but DO cover Turkish glyphs — "
        "remove them from _TR_UNSAFE_FONTS."
    )


# -- Static effect (effect=none) ---------------------------------------------


def test_animated_overlay_renders_one_extra_hold_frame(tmp_workdir):
    """Animated sequences render round(duration*FPS) + 1 frames. The extra
    held frame keeps the overlay on screen through `end_s` — FFmpeg's image2
    secondary EOFs at the last frame's PTS, so without it the overlay vanishes
    ~1 frame early. With cumulative reveals butting edge-to-edge that hole
    lands at every word boundary and blanks the line for a frame (the prod
    89cde014 flicker). Locks the seam-continuity fix."""
    overlay = {
        "text": "hello",
        "start_s": 0.0,
        "end_s": 0.4,  # 0.4s * 30fps = 12 logical frames
        "effect": "pop-in",
        "pop_animated_suffix": "hello",
        "font_family": "Playfair Display",
        "text_size_px": 120,
        "text_color": "#FFFFFF",
    }
    seq = tos._generate_overlay_sequence(overlay, tmp_workdir, 0)
    assert seq is not None
    assert seq["is_animated"] is True
    # 12 logical frames + 1 hold frame.
    assert seq["n_frames"] == int(round(0.4 * tos.FPS)) + 1
    frames = sorted(f for f in os.listdir(tmp_workdir) if f.endswith(".png"))
    assert len(frames) == seq["n_frames"]


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


def test_plain_pop_in_still_uses_short_animation_cap(tmp_workdir):
    overlay = {
        "text": "OOPS",
        "start_s": 0.0,
        "end_s": 5.0,
        "position": "center",
        "effect": "pop-in",
        "font_family": "Inter",
        "text_size_px": 100,
        "text_color": "#FFFFFF",
    }
    seq = tos._generate_overlay_sequence(overlay, tmp_workdir, 0)
    assert seq is not None
    assert seq["is_animated"] is True
    assert seq["n_frames"] == tos.MAX_OVERLAY_FRAMES


def test_long_lyric_pop_in_stage_uses_long_running_frame_ceiling(tmp_workdir):
    overlay = {
        "text": "make this happen",
        "start_s": 0.0,
        "end_s": 5.0,
        "position": "center",
        "effect": "pop-in",
        "pop_animated_suffix": "happen",
        "font_family": "Inter",
        "text_size_px": 100,
        "text_color": "#FFFFFF",
    }
    seq = tos._generate_overlay_sequence(overlay, tmp_workdir, 0)
    assert seq is not None
    assert seq["is_animated"] is True
    assert seq["n_frames"] == int(round(5.0 * tos.FPS)) + 1
    assert seq["n_frames"] > tos.MAX_OVERLAY_FRAMES


def test_karaoke_line_highlights_at_word_start_time(tmp_workdir):
    """First word starts at t=0, so it is already highlighted at t=0.2s."""
    overlay = {
        "text": "uno dos tres",
        "start_s": 0.0,
        "end_s": 2.0,
        "position": "center",
        "effect": "karaoke-line",
        "word_timings": [
            {"text": "uno", "start_s": 0.0, "end_s": 0.5, "duration_cs": 50},
            {"text": "dos", "start_s": 0.5, "end_s": 1.0, "duration_cs": 50},
            {"text": "tres", "start_s": 1.0, "end_s": 1.5, "duration_cs": 50},
        ],
        "font_family": "Inter",
        "text_size_px": 100,
        "text_color": "#FFFFFF",
        "highlight_color": "#FFD24A",  # gold
    }
    seq = tos._generate_overlay_sequence(overlay, tmp_workdir, 0)
    assert seq is not None
    # Frame at t=0.2s is before the old end-based renderer would highlight "uno".
    frame_06 = os.path.join(tmp_workdir, f"skia_overlay_000_f{6:04d}.png")
    assert os.path.exists(frame_06)
    img = Image.open(frame_06)
    assert _has_gold_pixel(img), "expected the first word to be gold from its start"


def test_karaoke_line_honors_explicit_word_start_after_gap(tmp_workdir):
    """The second word starts at 1.0s even though the first word ended at 0.3s."""
    overlay = {
        "text": "wait now",
        "start_s": 0.0,
        "end_s": 1.5,
        "position": "center",
        "effect": "karaoke-line",
        "word_timings": [
            {"text": "wait", "start_s": 0.0, "end_s": 0.3, "duration_cs": 30},
            {"text": "now", "start_s": 1.0, "end_s": 1.3, "duration_cs": 100},
        ],
        "font_family": "Inter",
        "text_size_px": 110,
        "text_color": "#FFFFFF",
        "highlight_color": "#FFD24A",
    }
    seq = tos._generate_overlay_sequence(overlay, tmp_workdir, 0)
    assert seq is not None
    # Frame at t~1.07s is after "now" starts but before the old end-based
    # renderer would highlight it at t=1.3s.
    frame_32 = os.path.join(tmp_workdir, f"skia_overlay_000_f{32:04d}.png")
    assert os.path.exists(frame_32)
    img = Image.open(frame_32)
    assert _has_gold_pixel(img, x_start=img.width // 2), (
        "expected the second word to highlight at its explicit start_s"
    )


def test_karaoke_line_keeps_completed_word_highlighted(tmp_workdir):
    """Between words, already-sung words stay yellow."""
    overlay = {
        "text": "wait now",
        "start_s": 0.0,
        "end_s": 1.5,
        "position": "center",
        "effect": "karaoke-line",
        "word_timings": [
            {"text": "wait", "start_s": 0.0, "end_s": 0.3, "duration_cs": 30},
            {"text": "now", "start_s": 1.0, "end_s": 1.3, "duration_cs": 30},
        ],
        "font_family": "Inter",
        "text_size_px": 110,
        "text_color": "#FFFFFF",
        "highlight_color": "#FFD24A",
    }
    seq = tos._generate_overlay_sequence(overlay, tmp_workdir, 0)
    assert seq is not None
    frame_15 = os.path.join(tmp_workdir, f"skia_overlay_000_f{15:04d}.png")
    assert os.path.exists(frame_15)
    img = Image.open(frame_15)
    assert _has_gold_pixel(img), "completed words should stay highlighted after being sung"


def test_long_karaoke_line_uses_long_running_frame_ceiling(tmp_workdir):
    overlay = {
        "text": "one two three four five",
        "start_s": 0.0,
        "end_s": 5.0,
        "position": "center",
        "effect": "karaoke-line",
        "word_timings": [
            {"text": "one", "start_s": 0.0, "end_s": 0.8, "duration_cs": 80},
            {"text": "two", "start_s": 1.0, "end_s": 1.8, "duration_cs": 80},
            {"text": "three", "start_s": 2.0, "end_s": 2.8, "duration_cs": 80},
            {"text": "four", "start_s": 3.0, "end_s": 3.8, "duration_cs": 80},
            {"text": "five", "start_s": 4.0, "end_s": 4.8, "duration_cs": 80},
        ],
        "font_family": "Inter",
        "text_size_px": 90,
        "text_color": "#FFFFFF",
        "highlight_color": "#FFD24A",
    }
    seq = tos._generate_overlay_sequence(overlay, tmp_workdir, 0)
    assert seq is not None
    assert seq["is_animated"] is True
    assert seq["n_frames"] == int(round(5.0 * tos.FPS)) + 1
    assert seq["n_frames"] > tos.MAX_OVERLAY_FRAMES


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


@pytest.mark.parametrize("effect", ["typewriter", "stream-in"])
@pytest.mark.parametrize("text_anchor", ["left", "center", "right"])
def test_reveal_effects_keep_full_wrapped_layout_geometry(effect, text_anchor):
    overlay = {
        "text": "ALPHA  BETA GAMMA DELTA EPSILON ZETA ETA THETA",
        "effect": effect,
        "position": "center",
        "position_x_frac": 0.5,
        "position_y_frac": 0.5,
        "max_width_frac": 0.45,
        "text_anchor": text_anchor,
        "vertical_anchor": "center",
        "font_family": "Inter",
        "text_size_px": 80,
        "text_color": "#FFFFFF",
    }
    surface = skia.Surfaces.MakeRasterN32Premul(tos.CANVAS_W, tos.CANVAS_H)

    def draw_calls_at(t_local):
        calls = []

        def capture(_canvas, line, x, baseline_y, *_args, **_kwargs):
            calls.append((line, x, baseline_y))

        with mock.patch.object(tos, "_draw_line_with_layers", side_effect=capture):
            tos._draw_with_animation(surface.getCanvas(), overlay, t_local, 10.0)
        return calls

    partial = draw_calls_at(0.0)
    settled = draw_calls_at(10.0)
    partial_text = "A" if effect == "typewriter" else "ALPHA"

    assert partial[0][0] == partial_text
    assert len(settled) >= 2
    assert partial[0][1:] == pytest.approx(settled[0][1:])

    first_line = settled[0][0]
    if effect == "typewriter":
        visible_units = len(first_line) + 2  # separator + first next-line glyph
        wrapped_partial = draw_calls_at((visible_units - 1) / 12.0)
        assert wrapped_partial[1][0] == settled[1][0][:1]
    else:
        visible_words = len(first_line.split()) + 1
        wrapped_partial = draw_calls_at((visible_words - 1) / 6.0)
        assert wrapped_partial[1][0] == settled[1][0].split()[0]
    assert wrapped_partial[1][1:] == pytest.approx(settled[1][1:])


def test_reveal_text_normalization_matches_wrapping_contract():
    assert tos._normalize_reveal_text("  ALPHA   BETA\n GAMMA\tDELTA ") == (
        "ALPHA BETA\nGAMMA DELTA"
    )


def test_stream_in_cursor_blinks_without_changing_full_layout_geometry():
    overlay = {
        "text": "one two three four five six",
        "effect": "stream-in",
        "position_x_frac": 0.5,
        "position_y_frac": 0.5,
        "max_width_frac": 0.45,
        "text_anchor": "right",
        "vertical_anchor": "center",
        "font_family": "Inter",
        "text_size_px": 80,
        "text_color": "#FFFFFF",
    }
    surface = skia.Surfaces.MakeRasterN32Premul(tos.CANVAS_W, tos.CANVAS_H)

    def draw_calls_at(t_local):
        calls = []

        def capture(_canvas, line, x, baseline_y, *_args, **_kwargs):
            calls.append((line, x, baseline_y))

        with mock.patch.object(tos, "_draw_line_with_layers", side_effect=capture):
            tos._draw_with_animation(surface.getCanvas(), overlay, t_local, 10.0)
        return calls

    cursor_on = draw_calls_at(0.0)
    cursor_off = draw_calls_at(0.5)
    settled = draw_calls_at(10.0)

    assert [call[0] for call in cursor_on] == ["one", " |"]
    assert " |" not in [call[0] for call in cursor_off]
    assert " |" not in [call[0] for call in settled]
    assert cursor_on[0][1:] == pytest.approx(settled[0][1:])


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


def test_karaoke_word_wrap_balances_production_orphan_lines():
    """Regression for job 1af7113b: greedy wrapping produced 8+1 and 6+3
    word splits. Karaoke should keep the minimum line count but avoid
    preventable orphan tails."""
    tf = tos._typeface_for_overlay({"font_family": "Bodoni Moda"})
    font = skia.Font(tf, 64)
    max_width = tos.CANVAS_W * tos._MAX_LINE_W_FRAC

    cases = [
        "I only call you when it's half past five",
        "The only time that I'd be by your side",
        "I only love it when you touch me not feel me",
        "When I'm fucked up that's the real me yeah",
    ]
    for text in cases:
        words = text.split()
        wrapped = tos._wrap_word_indices(words, font, max_width)
        counts = [len(line) for line in wrapped]
        assert len(wrapped) == 2, f"expected two-line wrap for {text!r}, got {counts}"
        assert min(counts) >= 4, f"preventable orphan line for {text!r}: {counts}"
        assert max(counts) - min(counts) <= 1, f"unbalanced line split for {text!r}: {counts}"
        for line in wrapped:
            line_text = " ".join(words[i] for i in line)
            assert font.measureText(line_text) <= max_width


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
    with (
        mock.patch("app.pipeline.text_overlay_skia.burn_text_overlays_skia") as skia_fn,
        mock.patch("app.pipeline.text_overlay.generate_text_overlay_png") as pil_png,
        mock.patch("app.pipeline.text_overlay.generate_animated_overlay_ass") as pil_ass,
        mock.patch("subprocess.run") as run_,
        mock.patch("app.config.settings") as fake_settings,
    ):
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

    with (
        mock.patch("app.pipeline.text_overlay_skia.burn_text_overlays_skia") as skia_fn,
        mock.patch("app.config.settings") as fake_settings,
    ):
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

    with (
        mock.patch("app.pipeline.text_overlay_skia.pre_burn_curtain_slot_text_skia") as skia_pre,
        mock.patch("app.config.settings") as fake_settings,
    ):
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

    with (
        mock.patch("app.pipeline.text_overlay_skia.burn_text_overlays_skia") as skia_fn,
        mock.patch("app.pipeline.text_overlay.generate_text_overlay_png") as pil_png,
        mock.patch("app.pipeline.text_overlay.generate_animated_overlay_ass") as pil_ass,
        mock.patch("subprocess.run") as run_,
        mock.patch("app.config.settings") as fake_settings,
    ):
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
        f"expected center-anchor to clip at the left edge (the bug); got bbox {center_bbox}"
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
    while still using the suffix-aware draw path instead of clipping off the
    right edge. A manually-edited cumulative phrase grown past ~90% canvas
    width ("combination of hard work" at 120px) overflowed because the
    suffix-pop layout placed prefix+suffix on a single baseline."""
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


def test_pop_in_suffix_wide_line_caps_at_two_lines():
    """Long lyric Pop-up stages should wrap to at most two caption lines."""
    overlay = {
        "text": "Let's make this happen let's make this happen",
        "effect": "pop-in",
        "pop_animated_suffix": "happen",
        "text_size_px": 120,
        "text_color": "#FFFFFF",
        "text_anchor": "center",
    }

    typeface = tos._typeface_for_overlay(overlay)
    font, _size, lines = tos._shrink_to_fit_max_lines(
        overlay["text"],
        typeface,
        tos._resolve_font_size_px(overlay),
        tos.CANVAS_W * tos._MAX_LINE_W_FRAC,
        tos._POP_SUFFIX_MAX_LINES,
    )

    assert 1 < len(lines) <= 2
    assert lines[-1].endswith("happen")
    assert max(font.measureText(line) for line in lines) <= tos.CANVAS_W * tos._MAX_LINE_W_FRAC


def test_pop_in_suffix_preserve_font_size_wraps_without_shrinking():
    """Per-word-pop lyrics keep the chosen type size and wrap to new lines.

    This is the music lyric behavior: as cumulative text grows, typography
    should remain stable instead of shrinking the whole phrase to fit.
    """
    import io

    overlay = {
        "text": "Let's make this happen let's make this happen",
        "effect": "pop-in",
        "pop_animated_suffix": "happen",
        "font_family": "Bodoni Moda",
        "text_size_px": 64,
        "position_x_frac": 0.06,
        "position_y_frac": 0.78,
        "text_color": "#FFFFFF",
        "text_anchor": "left",
        "preserve_font_size": True,
    }

    typeface = tos._typeface_for_overlay(overlay)
    font, size, lines = tos._wrap_at_fixed_size(
        overlay["text"],
        typeface,
        tos._resolve_font_size_px(overlay),
        tos.CANVAS_W * tos._MAX_LINE_W_FRAC,
    )
    assert size == 64
    assert len(lines) > 1
    assert max(font.measureText(line) for line in lines) <= tos.CANVAS_W * tos._MAX_LINE_W_FRAC

    with (
        mock.patch.object(tos, "_shrink_to_fit", wraps=tos._shrink_to_fit) as shrink,
        mock.patch.object(
            tos, "_shrink_to_fit_max_lines", wraps=tos._shrink_to_fit_max_lines
        ) as shrink_max,
    ):
        img = tos._draw_frame(overlay, 0.9, 1.5)

    assert shrink.call_count == 0
    assert shrink_max.call_count == 0
    im = Image.open(io.BytesIO(bytes(img.encodeToData()))).convert("RGBA")
    bbox = im.getbbox()
    assert bbox is not None
    assert bbox[3] - bbox[1] > 100, f"expected wrapped multi-line text; bbox {bbox}"


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
    """The revealed word appears at full size immediately.

    The word-by-word reveal comes from per-stage timing, not a scale bounce.
    """
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
    assert early == settled, f"word scaled between t=0.02 {early} and t=0.9 {settled}"


def test_plain_pop_in_scales_from_small_to_settled():
    import io

    ov = {
        "text": "OOPS",
        "effect": "pop-in",
        "text_size_px": 120,
        "position_x_frac": 0.5,
        "position_y_frac": 0.44,
        "text_color": "#FFFFFF",
        "text_anchor": "center",
    }

    def bbox(t):
        img = tos._draw_frame(ov, t, 1.0)
        return Image.open(io.BytesIO(bytes(img.encodeToData()))).convert("RGBA").getbbox()

    early, settled = bbox(0.0), bbox(0.9)
    assert early is not None and settled is not None
    assert early[2] - early[0] < (settled[2] - settled[0]) * 0.5


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


def test_karaoke_line_wraps_without_shrinking_font():
    words = "Let's make this happen let's make this happen".split()
    overlay = {
        "text": " ".join(words),
        "effect": "karaoke-line",
        "text_size_px": 120,
        "position_x_frac": 0.5,
        "position_y_frac": 0.78,
        "text_color": "#FFFFFF",
        "highlight_color": "#FFD24A",
        "font_family": "Inter",
        "text_anchor": "center",
        "word_timings": [
            {
                "text": word,
                "start_s": i * 0.25,
                "end_s": i * 0.25 + 0.2,
                "duration_cs": 20,
            }
            for i, word in enumerate(words)
        ],
    }

    seen_sizes: list[int] = []
    original_draw = tos._draw_line_with_layers

    def capture_draw(canvas, line, x, baseline_y, font, fill_color, stroke_px, shadow_alpha):
        seen_sizes.append(int(font.getSize()))
        return original_draw(canvas, line, x, baseline_y, font, fill_color, stroke_px, shadow_alpha)

    with mock.patch.object(tos, "_draw_line_with_layers", side_effect=capture_draw):
        bbox, width = _frame_bbox(overlay, t_local=1.0, duration_s=3.0)

    assert bbox is not None
    assert seen_sizes and set(seen_sizes) == {120}
    assert bbox[2] < width, f"wrapped karaoke line should fit horizontally; bbox {bbox}"
    assert bbox[3] - bbox[1] > 200, f"expected multi-line karaoke render; bbox {bbox}"


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
    left_delta = left_bbox[0] - center_bbox[0]  # left anchor sits RIGHT of center
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


@pytest.mark.parametrize("renderer", ["skia", "pillow"])
@pytest.mark.parametrize("effect", ["none", "pop-in"])
def test_text_element_horizontal_alignment_preserves_vertical_center(renderer, effect):
    """Authored TextElements decouple horizontal line alignment from y anchoring.

    Legacy left-anchored reveal overlays remain top-anchored, but the editor's
    left/center/right choices must all keep the same multiline block center.
    """
    base = {
        "text": "FIRST LINE\nSECOND LINE",
        "effect": effect,
        "text_size_px": 72,
        "position_x_frac": 0.5,
        "position_y_frac": 0.45,
        "max_width_frac": 0.6,
        "text_color": "#FFFFFF",
        "vertical_anchor": "center",
        "start_s": 0.0,
        "end_s": 2.0,
    }
    bbox_fn = _frame_bbox if renderer == "skia" else _pillow_bbox

    midpoints = []
    for anchor in ("left", "center", "right"):
        bbox, _ = bbox_fn({**base, "text_anchor": anchor})
        assert bbox is not None
        midpoints.append((bbox[1] + bbox[3]) / 2.0)

    assert max(midpoints) - min(midpoints) < 20, (
        f"{renderer}: horizontal alignment moved the text block vertically: {midpoints}"
    )


# ── Renderer parity: text_gradient honored by both Skia and Pillow ───────────
# Lock the CLAUDE.md renderer-parity invariant for the new `text_gradient`
# field.  When a gradient is set (red→blue, top→bottom), the top of the text
# block should have substantially more RED than BLUE, and the bottom
# substantially more BLUE than RED.  A renderer that ignores text_gradient
# falls back to solid white fill — then both halves are nearly white
# (R ≈ B ≈ 255) and the directional assertion fails, exposing the regression.
#
# Two-line text is intentional: it forces a taller block, making the gradient
# sweep more visible and making top/bottom sampling more reliable.


@pytest.mark.parametrize("renderer", ["skia", "pillow"])
def test_both_renderers_honor_text_gradient(renderer):
    """Both renderers must paint glyph fills with the requested gradient.

    The parity contract (CLAUDE.md "renderer-parity invariant", #296 class):
    every field carried in the burn dict MUST be honored by BOTH the Pillow
    and Skia renderers.  `text_gradient` was introduced in PR #487 together
    with renderer support on both sides; this test is the sentinel.
    """
    import numpy as np

    overlay = {
        "text": "NOVA\nGRADIENT",
        "effect": "none",
        "text_size_px": 72,
        "position_x_frac": 0.5,
        "position_y_frac": 0.45,
        "text_color": "#FFFFFF",  # solid fallback (should NOT appear when gradient is set)
        "start_s": 0.0,
        "end_s": 2.0,
        # Red at LEFT → blue at RIGHT, left-to-right (angle 0°).
        # We use a HORIZONTAL gradient rather than vertical (90°) because the
        # two renderers produce different line structures from "NOVA\nGRADIENT":
        # Skia respects the explicit '\n' and renders 2 lines, while Pillow's
        # word-wrap joins them into 1 line "NOVA GRADIENT". A horizontal
        # (left→right) gradient is direction-invariant w.r.t. line count — the
        # left glyph columns are always red, the right always blue, regardless
        # of how many lines are rendered.
        "text_gradient": {
            "colors": ["#FF0000", "#0000FF"],
            "angle_deg": 0,
        },
    }

    bbox_fn = _frame_bbox if renderer == "skia" else _pillow_bbox
    bbox, _width = bbox_fn(overlay)
    assert bbox is not None, f"{renderer}: gradient overlay produced no visible pixels"

    # Re-render to get the full image (bbox_fn discards it)
    if renderer == "skia":
        img = _skia_rgba_image(overlay)
    else:
        img = _pillow_rgba_image(overlay)

    arr = np.array(img)  # (H, W, 4) uint8 RGBA

    # Restrict to glyph bbox so we don't sample transparent padding.
    x0, y0, x1, y1 = bbox[0], bbox[1], bbox[2], bbox[3]
    block_w = x1 - x0

    # Sample inner horizontal bands (12.5%–37.5% and 62.5%–87.5% of bbox
    # width). Inner bands avoid anti-aliased / shadow edges that have low alpha,
    # and the [10%, 35%] / [65%, 90%] ranges sit well inside the red / blue
    # halves of the left→right gradient for any plausible text width.
    left_band = arr[y0:y1, x0 + block_w // 8 : x0 + 3 * block_w // 8]
    right_band = arr[y0:y1, x0 + 5 * block_w // 8 : x0 + 7 * block_w // 8]

    # Keep only opaque-ish pixels (alpha > 128) so we measure glyph fill only.
    left_opaque = left_band[left_band[..., 3] > 128]
    right_opaque = right_band[right_band[..., 3] > 128]

    assert len(left_opaque) > 50, f"{renderer}: left band has too few opaque pixels"
    assert len(right_opaque) > 50, f"{renderer}: right band has too few opaque pixels"

    left_r = float(left_opaque[:, 0].mean())
    left_b = float(left_opaque[:, 2].mean())
    right_r = float(right_opaque[:, 0].mean())
    right_b = float(right_opaque[:, 2].mean())

    assert left_r > left_b + 30, (
        f"{renderer}: left of gradient should be red (R={left_r:.0f} B={left_b:.0f}). "
        "If the renderer ignores text_gradient the fill is solid white and R≈B."
    )
    assert right_b > right_r + 30, (
        f"{renderer}: right of gradient should be blue (R={right_r:.0f} B={right_b:.0f}). "
        "If the renderer ignores text_gradient the fill is solid white and R≈B."
    )


def _skia_rgba_image(overlay: dict) -> Image.Image:
    """Render overlay at t=0.5s via the Skia path and return the RGBA PIL image.

    Must use readPixels(kRGBA_8888, kUnpremul) rather than tobytes() — the
    surface is N32Premul whose byte layout is BGRA on little-endian hosts, so
    tobytes() + frombytes("RGBA") silently swaps R and B channels.
    """
    import skia as _skia

    from app.pipeline.text_overlay_skia import _draw_frame

    skia_img = _draw_frame(overlay, 0.5, 2.0)
    info = _skia.ImageInfo.Make(
        skia_img.width(),
        skia_img.height(),
        _skia.ColorType.kRGBA_8888_ColorType,
        _skia.AlphaType.kUnpremul_AlphaType,
    )
    row_bytes = skia_img.width() * 4
    buf = bytearray(row_bytes * skia_img.height())
    skia_img.readPixels(info, buf, row_bytes, 0, 0)
    return Image.frombytes("RGBA", (skia_img.width(), skia_img.height()), bytes(buf))


def _pillow_rgba_image(overlay: dict) -> Image.Image:
    """Render overlay at t=2.0s via the Pillow path and return the RGBA PIL image."""
    import tempfile as _tf

    from app.pipeline.text_overlay import render_overlays_at_time

    with _tf.TemporaryDirectory(prefix="pillow_grad_") as d:
        out = os.path.join(d, "p.png")
        render_overlays_at_time([overlay], 2.0, 1.0, out)
        return Image.open(out).convert("RGBA").copy()


# -- Skia-only colored glow ---------------------------------------------------


def test_skia_text_glow_changes_rendered_pixels():
    base = {
        "text": "GLOW",
        "effect": "none",
        "text_size_px": 120,
        "position_x_frac": 0.5,
        "position_y_frac": 0.5,
        "text_color": "#FFD24A",
        "start_s": 0.0,
        "end_s": 2.0,
    }

    without = _skia_rgba_image(base)
    with_glow = _skia_rgba_image({**base, "glow_color": "#FFD24A", "glow_strength": 0.8})

    assert with_glow.tobytes() != without.tobytes()
    without_bbox = without.getbbox()
    with_bbox = with_glow.getbbox()
    assert without_bbox is not None and with_bbox is not None
    assert with_bbox[0] < without_bbox[0]
    assert with_bbox[2] > without_bbox[2]


def test_skia_text_glow_strength_zero_matches_absent_bytes():
    base = {
        "text": "NO GLOW",
        "effect": "none",
        "text_size_px": 96,
        "position_x_frac": 0.5,
        "position_y_frac": 0.5,
        "text_color": "#FFD24A",
        "start_s": 0.0,
        "end_s": 2.0,
    }

    absent = _skia_rgba_image(base)
    zero = _skia_rgba_image({**base, "glow_color": "#FFD24A", "glow_strength": 0.0})

    assert zero.tobytes() == absent.tobytes()


def test_draw_line_glow_alpha_scales_with_strength():
    font = skia.Font(tos._typeface_for_overlay({"font_family": "Inter"}), 80)
    seen_colors: list[int] = []

    def capture_draw(canvas, line, x, baseline_y, font, paint, letter_spacing_px):
        seen_colors.append(paint.getColor())
        return None

    with mock.patch.object(tos, "_draw_string_spaced", side_effect=capture_draw):
        tos._draw_line_with_layers(
            mock.MagicMock(),
            "GLOW",
            100.0,
            200.0,
            font,
            skia.ColorSetARGB(255, 255, 210, 74),
            stroke_px=0,
            shadow_alpha=160,
            glow_rgb=(255, 210, 74),
            glow_strength=0.5,
        )

    assert len(seen_colors) == 4
    assert (seen_colors[0] >> 24) & 0xFF == 60
    assert (seen_colors[1] >> 24) & 0xFF == 110
    assert seen_colors[0] & 0xFFFFFF == 0xFFD24A
    assert seen_colors[1] & 0xFFFFFF == 0xFFD24A
    assert seen_colors[2] == skia.ColorSetARGB(160, 0, 0, 0)
    assert seen_colors[3] == skia.ColorSetARGB(255, 255, 210, 74)


def test_pillow_renderer_ignores_skia_only_glow_fields():
    """Glow is a deliberate Skia-only decoration for burned agentic/generative
    overlays. The Pillow preview/classic renderer must safely drop the unknown
    fields without crashing or changing bytes; this is the explicit exception
    to the renderer-parity convention for glow_color/glow_strength."""
    base = {
        "text": "PILLOW",
        "effect": "none",
        "text_size_px": 96,
        "position_x_frac": 0.5,
        "position_y_frac": 0.5,
        "text_color": "#FFD24A",
        "start_s": 0.0,
        "end_s": 2.0,
    }

    without = _pillow_rgba_image(base)
    with_glow_fields = _pillow_rgba_image({**base, "glow_color": "#FFD24A", "glow_strength": 0.8})

    assert with_glow_fields.tobytes() == without.tobytes()


# ── Renderer parity: display_text honored by both Skia and Pillow ────────────
# Lock the CLAUDE.md renderer-parity invariant for the new `display_text`
# field. Layer 2's `_finalize_lyric_audible_window` writes `display_text` on
# partial lyric lines. Both renderers MUST prefer it over `text`. A regression
# on either side silently degrades to rendering the full original (Bug B
# returns).
#
# History: this is the #296-class invariant (see CLAUDE.md "Renderer-parity
# invariant"). The admin preview uses Pillow; prod music renders use Skia. A
# Skia-only break would pass the admin preview and only surface as misleading
# text in the actual rendered video.


def test_both_renderers_honor_display_text_skia():
    """Skia: with display_text set, the burned frame must match what would be
    burned if `text` itself were the display_text value."""
    short_only = {
        "text": "HI",
        "effect": "none",
        "text_size_px": 100,
        "position_x_frac": 0.5,
        "position_y_frac": 0.5,
        "text_color": "#FFFFFF",
        "text_anchor": "center",
    }
    long_with_short_display = {
        **short_only,
        "text": "THIS IS A VERY LONG ORIGINAL LINE",
        "display_text": "HI",
    }
    bbox_short, _ = _frame_bbox(short_only)
    bbox_long_short, _ = _frame_bbox(long_with_short_display)
    assert bbox_short is not None and bbox_long_short is not None
    short_w = bbox_short[2] - bbox_short[0]
    long_short_w = bbox_long_short[2] - bbox_long_short[0]
    # If display_text is honored, the two widths match within AA / kerning slack.
    # If display_text is IGNORED, the second renders the LONG text and width
    # explodes (long_short_w would be ~10× short_w).
    assert abs(long_short_w - short_w) < 50, (
        f"Skia did not honor display_text — expected ~{short_w}px (short text), "
        f"got {long_short_w}px (likely rendered the long original). The fix in "
        f"text_overlay_skia._overlay_text dropped or _draw_frame didn't route "
        f"the static-effect path through it."
    )


def test_both_renderers_honor_display_text_pillow():
    """Pillow: same parity assertion via render_overlays_at_time path used by
    export + admin preview. _validate_overlay must read `display_text or
    text` for the static path."""
    short_only = {
        "text": "HI",
        "effect": "none",
        "text_size_px": 100,
        "position_x_frac": 0.5,
        "position_y_frac": 0.5,
        "text_color": "#FFFFFF",
        "text_anchor": "center",
        "start_s": 0.0,
        "end_s": 5.0,
    }
    long_with_short_display = {
        **short_only,
        "text": "THIS IS A VERY LONG ORIGINAL LINE",
        "display_text": "HI",
    }
    bbox_short, _ = _pillow_bbox(short_only)
    bbox_long_short, _ = _pillow_bbox(long_with_short_display)
    assert bbox_short is not None and bbox_long_short is not None
    short_w = bbox_short[2] - bbox_short[0]
    long_short_w = bbox_long_short[2] - bbox_long_short[0]
    assert abs(long_short_w - short_w) < 50, (
        f"Pillow did not honor display_text — expected ~{short_w}px, got "
        f"{long_short_w}px. _validate_overlay reverted to `overlay.get('text')`?"
    )


def test_both_renderers_fall_back_to_text_when_display_text_absent():
    """No display_text → renderers use overlay['text'] exactly as pre-PR.
    Locks the byte-identical-fallback guarantee for non-music callers."""
    base = {
        "text": "FALLBACK",
        "effect": "none",
        "text_size_px": 100,
        "position_x_frac": 0.5,
        "position_y_frac": 0.5,
        "text_color": "#FFFFFF",
        "text_anchor": "center",
        "start_s": 0.0,
        "end_s": 5.0,
    }
    skia_bbox, _ = _frame_bbox(base)
    pillow_bbox, _ = _pillow_bbox(base)
    # Both render the "FALLBACK" text — non-empty bbox, comparable width.
    assert skia_bbox is not None and pillow_bbox is not None
    skia_w = skia_bbox[2] - skia_bbox[0]
    pillow_w = pillow_bbox[2] - pillow_bbox[0]
    # Cross-renderer width slack (different shapers): within 20%.
    assert abs(skia_w - pillow_w) / max(skia_w, pillow_w) < 0.2, (
        f"Renderers disagree on FALLBACK width: skia {skia_w}, pillow {pillow_w}"
    )


def test_display_text_empty_string_falls_back_to_text():
    """`display_text=''` is falsy in Python; `_overlay_text(overlay)` should
    treat it the same as missing and fall back to `text`. Prevents a render-
    time blank when finalizer (or a future caller) sets display_text to an
    empty string in error."""
    overlay = {
        "text": "VISIBLE",
        "display_text": "",  # empty falsy string
        "effect": "none",
        "text_size_px": 100,
        "position_x_frac": 0.5,
        "position_y_frac": 0.5,
        "text_color": "#FFFFFF",
        "text_anchor": "center",
        "start_s": 0.0,
        "end_s": 5.0,
    }
    skia_bbox, _ = _frame_bbox(overlay)
    # Should render "VISIBLE", not empty.
    assert skia_bbox is not None, "Skia rendered nothing — empty display_text wasn't ignored"
    assert (skia_bbox[2] - skia_bbox[0]) > 100, "rendered text too narrow for VISIBLE"


# ── Renderer parity: lyric-line fade honored by both Skia and libass ─────────
# Lock the renderer-parity invariant for `effect='lyric-line'`'s fade_in_ms /
# fade_out_ms. libass honors them via _emit_lyric_line_alpha_tags
# (text_overlay.py); Skia honors them via _lyric_line_alpha
# (text_overlay_skia.py). Without parity, the orchestrator's intentional
# crossfade window (lyric_injector.py's dynamic_max_overlap) renders as two
# stacked full-opacity PNGs in the Skia path — the prod regression that
# shipped with PR #319 and was visible in job f191e328-4a18-420b-8c68-
# f9539071334b ("If I can live through this" + "What comes next" stacked
# at t≈5.85s).


def _frame_max_alpha(overlay: dict, t_local: float, duration_s: float) -> int:
    """Render one frame via _draw_frame and return the max alpha across all
    pixels — captures the brightest text pixel after fade is applied."""
    import io

    img = tos._draw_frame(overlay, t_local, duration_s)
    im = Image.open(io.BytesIO(bytes(img.encodeToData()))).convert("RGBA")
    alpha = im.split()[-1]
    return alpha.getextrema()[1]


def test_lyric_line_is_animated_under_skia():
    """`lyric-line` MUST be in _ANIMATED_EFFECTS_SKIA. The static path emits a
    single full-opacity PNG that FFmpeg gates on/off with
    `enable='between(t,start,end)'` — when two lyric overlays' enable windows
    overlap (the designed crossfade window), they BOTH render at 100% alpha,
    stacked at the same y_frac. The animated path emits one PNG per frame
    with per-frame alpha so the same overlap crossfades correctly."""
    assert "lyric-line" in tos._ANIMATED_EFFECTS_SKIA, (
        "lyric-line must be in _ANIMATED_EFFECTS_SKIA so fade_in_ms / "
        "fade_out_ms are honored per-frame. Without this, the renderer-"
        "parity invariant breaks vs the libass path."
    )
    overlay = {
        "text": "test",
        "effect": "lyric-line",
        "start_s": 0.0,
        "end_s": 1.0,
        "fade_in_ms": 150,
        "fade_out_ms": 250,
        "text_size_px": 64,
        "position_x_frac": 0.5,
        "position_y_frac": 0.8,
        "text_color": "#FFFFFF",
    }
    with tempfile.TemporaryDirectory(prefix="lyric_seq_") as d:
        seq = tos._generate_overlay_sequence(overlay, d, 0)
    assert seq is not None
    assert seq["is_animated"] is True, (
        "lyric-line must produce an animated PNG sequence so fade ramps "
        f"render per-frame; got is_animated={seq['is_animated']}"
    )
    assert seq["n_frames"] > 1, (
        f"animated lyric-line must have >1 frame; got n_frames={seq['n_frames']}"
    )


def test_lyric_line_alpha_ramps_in_skia():
    """At three sample t_local values, the rendered alpha must form a
    fade-in → hold → fade-out ramp. Sample max alpha (brightest text pixel)
    to compare. With fade_in_ms=150, fade_out_ms=250, duration=1.0s and
    libass ease curves (accel=0.5 in, accel=2.0 out):

      t=0.075 (mid fade-in):  alpha = sqrt(0.5)   ≈ 0.71 → max α ≈ 180
      t=0.500 (hold):         alpha = 1.00              → max α = 255
      t=0.875 (mid fade-out): alpha = 1 - 0.5**2  = 0.75 → max α ≈ 191

    Slack: shadow has its own alpha curve, so we assert ordering + that the
    hold sample is fully opaque and the fade samples are at least
    moderately dimmer (well below 255).
    """
    overlay = {
        "text": "FADE LYRIC",
        "effect": "lyric-line",
        "start_s": 0.0,
        "end_s": 1.0,
        "fade_in_ms": 150,
        "fade_out_ms": 250,
        "text_size_px": 80,
        "position_x_frac": 0.5,
        "position_y_frac": 0.8,
        "text_color": "#FFFFFF",
    }
    a_in = _frame_max_alpha(overlay, t_local=0.075, duration_s=1.0)
    a_hold = _frame_max_alpha(overlay, t_local=0.500, duration_s=1.0)
    a_out = _frame_max_alpha(overlay, t_local=0.875, duration_s=1.0)

    assert a_hold == 255, f"hold sample must be fully opaque; got {a_hold}"
    # Fade samples should be reduced. Ease(0.5) at 50% in = 0.71; ease(2.0)
    # at 50% out = 0.75. Threshold 240 still catches "no ramp applied"
    # (which would render at exactly 255) without being brittle to AA +
    # shadow contribution.
    assert a_in < 240, (
        f"fade-in sample at t=0.075 must be dimmer than hold; got {a_in}. "
        "Skia is rendering at full opacity — alpha ramp not applied."
    )
    assert a_out < 240, (
        f"fade-out sample at t=0.875 must be dimmer than hold; got {a_out}. "
        "Skia is rendering at full opacity — alpha ramp not applied."
    )


def test_lyric_line_alpha_zero_fade_holds_full_opacity():
    """fade_in_ms=0 + fade_out_ms=0 (the kill-switch case) must render solid
    full alpha at every t_local. Guards against the helper accidentally
    dividing by zero or producing NaN."""
    overlay = {
        "text": "SOLID",
        "effect": "lyric-line",
        "start_s": 0.0,
        "end_s": 1.0,
        "fade_in_ms": 0,
        "fade_out_ms": 0,
        "text_size_px": 80,
        "position_x_frac": 0.5,
        "position_y_frac": 0.8,
        "text_color": "#FFFFFF",
    }
    for t_local in (0.0, 0.25, 0.5, 0.75, 1.0):
        assert _frame_max_alpha(overlay, t_local=t_local, duration_s=1.0) == 255, (
            f"zero-fade lyric-line at t={t_local} should be fully opaque"
        )


def test_lyric_line_alpha_helper_matches_libass_clamp_semantics():
    """The Skia alpha helper must mirror libass `_emit_lyric_line_alpha_tags`
    clamp semantics exactly (fade_out shrinks to fit any time fade_in did
    not consume). Documenting + locking the cross-renderer contract here so
    a future change to one helper forces a deliberate change to the other."""
    # Normal case: both fades fit. Curves match libass:
    #   fade-in:  alpha = sqrt(progress)        (accel=0.5)
    #   fade-out: alpha = 1 - progress**2       (accel=2.0)
    base = {"effect": "lyric-line", "fade_in_ms": 150, "fade_out_ms": 250}
    assert tos._lyric_line_alpha(base, 0.0, 1.0) == 0.0
    # Mid fade-in (50% through 150ms): sqrt(0.5) ≈ 0.707
    assert abs(tos._lyric_line_alpha(base, 0.075, 1.0) - 0.5**0.5) < 0.01
    assert tos._lyric_line_alpha(base, 0.5, 1.0) == 1.0  # hold
    # Mid fade-out (50% through 250ms starting at 750ms): 1 - 0.5**2 = 0.75
    assert abs(tos._lyric_line_alpha(base, 0.875, 1.0) - (1.0 - 0.5**2)) < 0.01
    # Short overlay: fade_in_ms > duration → fade_in clamps; fade_out clamps to 0.
    short = {"effect": "lyric-line", "fade_in_ms": 200, "fade_out_ms": 200}
    a = tos._lyric_line_alpha(short, 0.05, 0.1)
    assert 0.0 < a < 1.0  # mid-of-clamped-fade-in, no NaN, no overshoot
    # Missing keys → defaults match libass (fade_in_ms=150, fade_out_ms=250).
    # At t=0 we're at the start of fade-in → alpha=0; mid-hold → alpha=1.
    none = {"effect": "lyric-line"}
    assert tos._lyric_line_alpha(none, 0.0, 1.0) == 0.0
    assert tos._lyric_line_alpha(none, 0.5, 1.0) == 1.0
    # Explicit 0 still means "no fade" (kill switch).
    zero = {"effect": "lyric-line", "fade_in_ms": 0, "fade_out_ms": 0}
    assert tos._lyric_line_alpha(zero, 0.0, 1.0) == 1.0


def test_lyric_line_longer_than_frame_cap_renders_full_duration():
    """REGRESSION: making lyric-line animated naively inherits
    MAX_OVERLAY_FRAMES (120 frames @ 30fps = 4s). The user-reported job's
    Line 2 was 4.26s — capping would chop the last 0.26s (fade-out + tail),
    re-introducing the stack-without-crossfade visible symptom right at
    the next line's hand-off. lyric-line must opt out of the font-cycle
    cap and render frames for its full duration."""
    overlay = {
        "text": "long lyric line that goes well past 4 seconds",
        "effect": "lyric-line",
        "start_s": 0.0,
        "end_s": 6.0,  # 6s > 4s cap
        "fade_in_ms": 150,
        "fade_out_ms": 250,
        "text_size_px": 64,
        "position_x_frac": 0.5,
        "position_y_frac": 0.8,
        "text_color": "#FFFFFF",
    }
    with tempfile.TemporaryDirectory(prefix="lyric_long_") as d:
        seq = tos._generate_overlay_sequence(overlay, d, 0)
    assert seq is not None
    # 6.0s * 30 fps = 180 frames + 1 hold = 181. Must exceed MAX_OVERLAY_FRAMES.
    expected_min = int(round(6.0 * tos.FPS))
    assert seq["n_frames"] >= expected_min, (
        f"lyric-line longer than {tos.MAX_OVERLAY_FRAMES / tos.FPS:.1f}s is "
        f"silently truncated to {seq['n_frames']} frames; expected "
        f">={expected_min} for a 6s line. The font-cycle frame cap must "
        "not apply to lyric-line."
    )


def test_overlapping_lyric_lines_crossfade_through_burn_pipeline(tmp_workdir):
    """END-TO-END REGRESSION: render two consecutive lyric-line overlays
    that overlap by 100ms (the prod job f191e328 shape) through the actual
    Skia + FFmpeg burn pipeline, sample a mid-overlap frame, and assert
    NEITHER overlay saturates to full opacity.

    Before this fix, both overlays burned at full alpha throughout the
    overlap window — the "two stacked texts" visible symptom. The earlier
    tests assert the renderer produces a ramp; this test asserts the ramp
    produces a real crossfade through the full pipeline (which is what the
    user actually sees in the rendered MP4)."""
    import shutil
    import subprocess

    if not shutil.which("ffmpeg"):
        pytest.skip("ffmpeg not available")

    line_a = {
        "text": "FADE OUT LINE",
        "effect": "lyric-line",
        "start_s": 0.0,
        "end_s": 1.0,
        "fade_in_ms": 150,
        "fade_out_ms": 250,
        "text_size_px": 80,
        "position_x_frac": 0.5,
        "position_y_frac": 0.5,
        "text_color": "#FFFFFF",
    }
    line_b = {
        "text": "FADE IN LINE",
        "effect": "lyric-line",
        "start_s": 0.9,  # 100ms overlap with line_a's fade-out tail
        "end_s": 2.0,
        "fade_in_ms": 150,
        "fade_out_ms": 250,
        "text_size_px": 80,
        "position_x_frac": 0.5,
        "position_y_frac": 0.7,  # different Y so we can tell them apart
        "text_color": "#FFFFFF",
    }

    sequences = tos._render_overlay_sequences([line_a, line_b], 2.5, tmp_workdir)
    assert len(sequences) == 2
    assert all(s["is_animated"] for s in sequences), "both lyric-line overlays must be animated"

    bg = os.path.join(tmp_workdir, "bg.mp4")
    burned = os.path.join(tmp_workdir, "burned.mp4")
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "color=c=black:s=1080x1920:r=30:d=2.5",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-pix_fmt",
            "yuv420p",
            bg,
        ],
        check=True,
        capture_output=True,
    )
    tos._ffmpeg_burn_pngs(bg, sequences, burned)

    # Sample mid-overlap frame at t=0.95s (line_a's fade-out is ~80%
    # remaining toward 1.0; line_b's fade-in is ~33% into the 150ms ramp).
    frame_path = os.path.join(tmp_workdir, "mid.png")
    subprocess.run(
        ["ffmpeg", "-y", "-ss", "0.95", "-i", burned, "-frames:v", "1", frame_path],
        check=True,
        capture_output=True,
    )
    im = Image.open(frame_path).convert("RGBA")
    # line_a band centered at y_frac=0.5 → y≈960. line_b band at y_frac=0.7 → y≈1344.
    # Both should be in fade — neither pixel band hits full white.
    band_a_max = im.crop((0, 920, im.width, 1000)).split()[0].getextrema()[1]
    band_b_max = im.crop((0, 1300, im.width, 1380)).split()[0].getextrema()[1]
    assert band_a_max < 240, (
        f"line_a at t=0.95 (mid fade-out) burned at near-full opacity (max R={band_a_max}); "
        "alpha ramp not applied through the burn pipeline — the visible 'stacked text' bug."
    )
    assert band_b_max < 240, (
        f"line_b at t=0.95 (early fade-in) burned at near-full opacity (max R={band_b_max}); "
        "alpha ramp not applied through the burn pipeline — the visible 'stacked text' bug."
    )


def test_real_milky_overlapping_lines_crossfade_through_burn_pipeline(tmp_workdir):
    """END-TO-END on REAL prod data: take a consecutive overlapping lyric pair
    straight out of the production scheduler + consolidation
    (`inject_lyric_overlays` → `_collect_absolute_overlays`) for the Milky track
    that stacked in prod job 901fe271, then burn it through the actual Skia +
    FFmpeg pipeline and assert the overlap frame shows a real crossfade — NEITHER
    line at full opacity.

    The sibling test above uses synthetic timings; this one proves the EXACT
    windows/fades/curves the live generative path emits survive to real pixels.
    Distinct probe-Y per line (the real overlays share y=0.80) so each band is
    independently measurable — the crossfade property depends on timing+curve,
    not Y."""
    import shutil
    import subprocess

    if not shutil.which("ffmpeg"):
        pytest.skip("ffmpeg not available")

    from app.pipeline.lyric_injector import inject_lyric_overlays
    from app.tasks.template_orchestrate import _collect_absolute_overlays

    # Real synced lyrics from prod track 29da2cbf, best section 60-75s. The
    # sustained "Do do" lines overlap in source time — the prod stacking shape.
    prod_lines = [
        ("Just the way you are", 58.22, 60.12),
        ("Do do do do do do do do", 60.12, 63.19),
        ("Do do do do do do do do", 62.92, 66.54),
        ("And it goes", 66.74, 67.15),
        ("Do do do do do do do do", 67.32, 70.24),
    ]
    cache = {
        # Injector requires a publishable source after 2026-05-27 — see
        # `_INJECTOR_ALLOWED_SOURCES` in app/pipeline/lyric_injector.py.
        "source": "lrclib_synced+whisper",
        "lines": [
            {
                "text": t,
                "start_s": s,
                "end_s": e,
                "words": [{"text": t, "start_s": s, "end_s": e}],
            }
            for t, s, e in prod_lines
        ],
    }
    slot_durations = [7.5, 7.5]
    recipe = {
        "slots": [
            {"position": i + 1, "target_duration_s": d, "text_overlays": []}
            for i, d in enumerate(slot_durations)
        ]
    }
    # Realistic `effective_lyrics_config` shape (admin Test tab / generative
    # path): form-default fade_in_ms=150 + fade_out_ms=250 that PR #343 misread
    # as user overrides and used to disable its own crossfade (the #344 bug).
    # The bare {"enabled","style"} config bypasses that path, so use the shape
    # that actually broke — this asserts the crossfade reaches real pixels even
    # under the gate-tripping config.
    effective_cfg = {
        "style": "line",
        "enabled": True,
        "fade_in_ms": 150,
        "fade_out_ms": 250,
        "pre_roll_s": 0.1,
        "post_dwell_s": 1.0,
        "next_line_gap_s": 0.1,
    }
    out = inject_lyric_overlays(recipe, cache, 60.0, 75.0, effective_cfg)
    steps = [{"clip_id": f"c{i}", "slot": slot} for i, slot in enumerate(out["slots"])]
    overlays = [
        o
        for o in _collect_absolute_overlays(steps, slot_durations, None, "", is_agentic=True)
        if o.get("effect") == "lyric-line"
    ]
    overlays.sort(key=lambda o: o["start_s"])

    # Find the first consecutive pair that overlaps in time (the transition the
    # viewer sees as one line handing off to the next).
    pair = next(
        ((a, b) for a, b in zip(overlays, overlays[1:]) if a["end_s"] > b["start_s"]),
        None,
    )
    assert pair is not None, "expected at least one overlapping lyric transition"
    a_src, b_src = pair
    overlap_mid = (b_src["start_s"] + a_src["end_s"]) / 2.0

    # Clone with distinct probe-Y so each band is independently measurable.
    # Everything else (start/end/fade_in/fade_out/fade_out_curve) is the REAL
    # scheduler output — that is what governs the crossfade.
    line_a = {**a_src, "position_x_frac": 0.5, "position_y_frac": 0.5, "text_color": "#FFFFFF"}
    line_b = {**b_src, "position_x_frac": 0.5, "position_y_frac": 0.7, "text_color": "#FFFFFF"}

    duration = b_src["end_s"] + 0.5
    sequences = tos._render_overlay_sequences([line_a, line_b], duration, tmp_workdir)
    assert len(sequences) == 2
    assert all(s["is_animated"] for s in sequences), "both lyric-line overlays must be animated"

    bg = os.path.join(tmp_workdir, "bg.mp4")
    burned = os.path.join(tmp_workdir, "burned.mp4")
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-f",
            "lavfi",
            "-i",
            f"color=c=black:s=1080x1920:r=30:d={duration:.2f}",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-pix_fmt",
            "yuv420p",
            bg,
        ],
        check=True,
        capture_output=True,
    )
    tos._ffmpeg_burn_pngs(bg, sequences, burned)

    frame_path = os.path.join(tmp_workdir, "mid.png")
    subprocess.run(
        ["ffmpeg", "-y", "-ss", f"{overlap_mid:.3f}", "-i", burned, "-frames:v", "1", frame_path],
        check=True,
        capture_output=True,
    )
    im = Image.open(frame_path).convert("RGBA")
    # line_a band at y_frac=0.5 → y≈960; line_b band at y_frac=0.7 → y≈1344.
    band_a_max = im.crop((0, 900, im.width, 1020)).split()[0].getextrema()[1]
    band_b_max = im.crop((0, 1284, im.width, 1404)).split()[0].getextrema()[1]
    # At the overlap midpoint a true crossfade keeps BOTH lines below full
    # opacity. The pre-#343 bug burned both at 255 (the stacked-text symptom).
    assert band_a_max < 240, (
        f"outgoing line {a_src['text'][:16]!r} at overlap midpoint t={overlap_mid:.2f}s "
        f"burned near-full (max R={band_a_max}) — stacked-text regression in the burned video."
    )
    assert band_b_max < 240, (
        f"incoming line {b_src['text'][:16]!r} at overlap midpoint t={overlap_mid:.2f}s "
        f"burned near-full (max R={band_b_max}) — stacked-text regression in the burned video."
    )


def test_lyric_line_sanity_ceiling_caps_runaway_duration():
    """Defense in depth: a malformed transcript could (in theory) produce a
    lyric-line overlay with `end_s = 240.0`. Without a sanity cap, the
    encode worker would write 7200 PNGs (~7GB scratch) before FFmpeg
    consumes any. The ceiling caps n_frames at 30s × FPS regardless of
    `wanted` so the worker survives bad upstream data."""
    overlay = {
        "text": "runaway",
        "effect": "lyric-line",
        "start_s": 0.0,
        "end_s": 120.0,  # 2 minutes — well past any real lyric line
        "fade_in_ms": 150,
        "fade_out_ms": 250,
        "text_size_px": 64,
        "position_x_frac": 0.5,
        "position_y_frac": 0.8,
        "text_color": "#FFFFFF",
    }
    with tempfile.TemporaryDirectory(prefix="lyric_runaway_") as d:
        seq = tos._generate_overlay_sequence(overlay, d, 0)
    assert seq is not None
    ceiling = int(tos.FPS * 30)
    assert seq["n_frames"] <= ceiling + 1, (
        f"lyric-line n_frames={seq['n_frames']} exceeded sanity ceiling "
        f"{ceiling}+1 — encode worker is exposed to runaway upstream data."
    )


def test_libass_lyric_line_emits_fade_tags():
    """Parity reference: confirm the libass path emits `\\alpha` fade-in +
    fade-out tags. If libass ever stops emitting these, the Skia
    implementation needs to be reconciled with whatever replaced them."""
    from app.pipeline.text_overlay import _emit_lyric_line_alpha_tags

    tags = _emit_lyric_line_alpha_tags(
        section_start_s=0.0, section_end_s=1.0, fade_in_ms=150, fade_out_ms=250
    )
    # Fade-in ramp: starts opaque-tag-0 (\alpha&HFF&) and animates to opaque
    # (&H00&) over [0, 150ms].
    assert r"\alpha&HFF&" in tags
    assert r"\t(0,150" in tags and r"\alpha&H00&" in tags
    # Fade-out ramp: animates back to fully transparent over [750, 1000ms].
    assert r"\t(750,1000" in tags and tags.count(r"\alpha&HFF&") >= 2


# -- Lyric glyph fail-fast (P6 backstop behind the language gate) --------------


def test_lyric_burn_fails_fast_on_missing_glyphs():
    """CJK lyric text against a Latin-only bundled face must raise
    MissingGlyphsError (→ error_class=lyrics_unsupported_language) instead of
    silently burning .notdef tofu boxes into the video."""
    surf = skia.Surfaces.MakeRasterN32Premul(200, 200)
    canvas = surf.getCanvas()
    overlay = {
        "text": "八方來財 八方來財",
        "effect": "karaoke-line",
        "font_family": "Inter",
        "word_timings": [
            {"text": "八方來財", "start_s": 0.0, "end_s": 0.5},
            {"text": "八方來財", "start_s": 0.5, "end_s": 1.0},
        ],
    }
    with pytest.raises(tos.MissingGlyphsError):
        tos._draw_karaoke_line(canvas, overlay, 0.2, 1.0)


def test_lyric_glyph_check_scopes_to_letters():
    """assert_lyric_glyphs checks LETTERS only — a face missing an em-dash or a
    curly quote must not fail a previously-working Latin lyric render."""
    tf = tos._typeface_for_overlay({"font_family": "Inter"})
    tos.assert_lyric_glyphs(tf, "don’t stop — believin’…")
    tos.assert_lyric_glyphs(tf, "")  # empty text is a no-op, never raises


# -- generative_sequence role (editorial transcript-synced sequences) ----------
#
# Sequence overlays carry role="generative_sequence" (an existing FIELD with a
# new VALUE — "generative_intro" is the precedent) and reuse the lyric-path
# fade field names: fade_out_ms + fade_out_curve. The renderer must (a) hold
# the scene through its full window (long-running frame ceiling), (b) ramp the
# final fade_out_ms to transparent, and (c) not render unique frames for the
# settled middle (hard-linked hold frames).


def _sequence_overlay(**kw) -> dict:
    base = {
        "text": "the days we lost",
        "role": "generative_sequence",
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


def test_generative_sequence_role_uses_long_running_frame_ceiling():
    assert tos._uses_long_running_frame_ceiling(_sequence_overlay())
    assert tos._uses_long_running_frame_ceiling(_sequence_overlay(effect="static"))
    # Legacy overlays without the role keep the short cap.
    assert not tos._uses_long_running_frame_ceiling({"effect": "fade-in"})
    assert not tos._uses_long_running_frame_ceiling({"effect": "pop-in"})
    assert not tos._uses_long_running_frame_ceiling({"effect": "static"})


def test_long_generative_sequence_does_not_vanish_at_frame_120(tmp_workdir):
    """A >4s sequence scene must render frames for its FULL window — the
    120-frame MAX_OVERLAY_FRAMES cap would make the scene vanish at ~4s
    (image2 EOF + eof_action=pass) while its transcript window keeps going."""
    overlay = _sequence_overlay(end_s=6.0)
    seq = tos._generate_overlay_sequence(overlay, tmp_workdir, 0)
    assert seq is not None
    assert seq["is_animated"] is True
    # 6.0s * 30fps = 180 logical frames + 1 hold frame, well past the cap.
    assert seq["n_frames"] == int(round(6.0 * tos.FPS)) + 1
    assert seq["n_frames"] > tos.MAX_OVERLAY_FRAMES
    frames = sorted(f for f in os.listdir(tmp_workdir) if f.endswith(".png"))
    assert len(frames) == seq["n_frames"]


def test_generative_sequence_fade_out_ms_consumed_for_fade_in_effect():
    """fade-in head → hold → fade_out_ms tail. Sampled via rendered frames."""
    overlay = _sequence_overlay()
    a_hold = _frame_max_alpha(overlay, t_local=3.0, duration_s=6.0)
    a_out = _frame_max_alpha(overlay, t_local=5.75, duration_s=6.0)  # mid fade-out
    a_end = _frame_max_alpha(overlay, t_local=6.0, duration_s=6.0)
    assert a_hold == 255, f"mid-hold must be fully opaque; got {a_hold}"
    assert a_out < a_hold, f"fade-out sample must dim below hold; got {a_out}"
    assert a_out < 240, "fade_out_ms not consumed — still full opacity near end"
    assert a_end == 0, f"end of window must be fully transparent; got {a_end}"


def test_generative_sequence_fade_out_curve_sqrt_honored():
    """fade_out_curve="sqrt" must drop FASTER mid-fade than the default
    lingering curve (1-√p < 1-p² for 0<p<1) — same semantics as lyric-line."""
    default_curve = _sequence_overlay()
    sqrt_curve = _sequence_overlay(fade_out_curve="sqrt")
    t_mid_fade = 5.75  # 50% through the 500ms tail
    a_default = _frame_max_alpha(default_curve, t_local=t_mid_fade, duration_s=6.0)
    a_sqrt = _frame_max_alpha(sqrt_curve, t_local=t_mid_fade, duration_s=6.0)
    assert a_sqrt < a_default


def test_generative_sequence_static_with_fade_out_is_animated_and_ramps():
    """effect="static" + fade_out_ms must route through the animated path —
    the single-PNG static loop renders full opacity through the whole window
    and would never fade."""
    overlay = _sequence_overlay(effect="static")
    assert tos._is_animated(overlay) is True
    a_hold = _frame_max_alpha(overlay, t_local=3.0, duration_s=6.0)
    a_end = _frame_max_alpha(overlay, t_local=6.0, duration_s=6.0)
    assert a_hold == 255
    assert a_end == 0


def test_generative_sequence_static_without_fade_out_stays_static():
    """No fade_out_ms → the static single-PNG path (`-loop 1`) is the most
    frame-economical hold and already spans the full window."""
    overlay = _sequence_overlay(effect="static", fade_out_ms=None)
    assert tos._is_animated(overlay) is False


def test_generative_sequence_hold_frames_are_linked_not_rendered(tmp_workdir):
    """Frame economy: the settled middle must NOT render unique frames — only
    the fade-in head and fade-out tail are real renders; the hold is one
    settled frame hard-linked into every middle slot."""
    overlay = _sequence_overlay(end_s=6.0, fade_out_ms=500)
    seq = tos._generate_overlay_sequence(overlay, tmp_workdir, 0)
    assert seq is not None
    frames = sorted(
        os.path.join(tmp_workdir, f) for f in os.listdir(tmp_workdir) if f.endswith(".png")
    )
    assert len(frames) == seq["n_frames"]
    # The hold region (well inside head=0.4s .. tail-start=5.5s) shares one
    # inode — hard links, not re-rendered frames.
    f_1s = os.path.join(tmp_workdir, "skia_overlay_000_f0030.png")
    f_3s = os.path.join(tmp_workdir, "skia_overlay_000_f0090.png")
    f_5s = os.path.join(tmp_workdir, "skia_overlay_000_f0150.png")
    assert os.path.samefile(f_1s, f_3s)
    assert os.path.samefile(f_3s, f_5s)
    # Unique renders are bounded by head + tail (~0.4s + 0.5s ≈ 30 frames),
    # nowhere near the 181-frame window.
    unique_inodes = {os.stat(f).st_ino for f in frames}
    assert len(unique_inodes) < 40, (
        f"{len(unique_inodes)} unique frames rendered for a 6s sequence "
        "overlay — the settled hold must be linked, not re-rendered"
    )
    # Head (fade-in) and tail (fade-out) frames are NOT the settled frame.
    f_head = os.path.join(tmp_workdir, "skia_overlay_000_f0003.png")
    f_tail = os.path.join(tmp_workdir, "skia_overlay_000_f0172.png")
    assert not os.path.samefile(f_head, f_3s)
    assert not os.path.samefile(f_tail, f_3s)


def test_generative_sequence_two_block_scene_fades_in_rendered_pngs(tmp_workdir):
    """End-to-end pixel check on a tiny 2-block editorial scene: each block's
    PNG sequence is opaque mid-hold and transparent on its final frame."""

    def _png_max_alpha(path: str) -> int:
        return Image.open(path).split()[-1].getextrema()[1]

    scene = [
        _sequence_overlay(
            text="when the",
            font_family="Playfair Display Regular",
            position_x_frac=0.40,
            position_y_frac=0.40,
        ),
        _sequence_overlay(
            text="days we lost",
            font_family="Great Vibes",
            text_size_px=102,
            position_x_frac=0.49,
            position_y_frac=0.46,
        ),
    ]
    seqs = tos._render_overlay_sequences(scene, 7.0, tmp_workdir)
    assert len(seqs) == 2
    for seq in seqs:
        prefix = seq["pattern"].replace("%04d.png", "")
        mid = f"{prefix}{seq['n_frames'] // 2:04d}.png"
        last = f"{prefix}{seq['n_frames'] - 1:04d}.png"
        assert _png_max_alpha(mid) == 255, "mid-hold frame must be fully opaque"
        assert _png_max_alpha(last) < 30, "final frame must have faded out"


def test_legacy_fade_in_overlay_ignores_fade_out_ms(tmp_workdir):
    """Renderer-conservatism guard: WITHOUT role="generative_sequence",
    fade_out_ms on a fade-in overlay stays inert (pre-existing behavior) and
    the short frame cap still applies."""
    overlay = _sequence_overlay()
    del overlay["role"]
    # Alpha at the very end of the window stays fully opaque — no tail ramp.
    assert _frame_max_alpha(overlay, t_local=5.9, duration_s=6.0) == 255
    seq = tos._generate_overlay_sequence(overlay, tmp_workdir, 0)
    assert seq is not None
    assert seq["n_frames"] == tos.MAX_OVERLAY_FRAMES  # legacy cap untouched


def test_generative_sequence_short_window_renders_every_frame(tmp_workdir):
    """A window too short to have a holdable middle (fade-in head + fade-out
    tail covering everything) falls back to rendering every frame."""
    overlay = _sequence_overlay(end_s=0.8, fade_out_ms=400)
    seq = tos._generate_overlay_sequence(overlay, tmp_workdir, 0)
    assert seq is not None
    frames = [os.path.join(tmp_workdir, f) for f in os.listdir(tmp_workdir) if f.endswith(".png")]
    assert len(frames) == seq["n_frames"]
    assert len({os.stat(f).st_ino for f in frames}) == len(frames)


def test_pillow_path_never_dispatched_for_generative_sequence(tmp_workdir):
    """Generative is Skia-only: `generative_build` calls
    `burn_text_overlays_skia` directly, and the `_burn_text_overlays`
    dispatch (use_skia=True) must route sequence overlays to Skia without
    touching the Pillow generators. There is no Pillow implementation of the
    sequence fade/hold mechanics, so a Pillow dispatch would silently drop
    them — this is the generative analogue of the renderer-parity guard."""
    from app.tasks.template_orchestrate import _burn_text_overlays

    overlays = [_sequence_overlay()]
    with (
        mock.patch("app.pipeline.text_overlay_skia.burn_text_overlays_skia") as skia_fn,
        mock.patch("app.pipeline.text_overlay.generate_text_overlay_png") as pil_png,
        mock.patch("app.pipeline.text_overlay.generate_animated_overlay_ass") as pil_ass,
        mock.patch("app.config.settings") as fake_settings,
    ):
        fake_settings.text_renderer_skia_enabled = True
        in_path = os.path.join(tmp_workdir, "in.mp4")
        out_path = os.path.join(tmp_workdir, "out.mp4")
        with open(in_path, "wb") as f:
            f.write(b"\x00")
        _burn_text_overlays(in_path, overlays, out_path, tmp_workdir, use_skia=True)
        skia_fn.assert_called_once()
        pil_png.assert_not_called()
        pil_ass.assert_not_called()


def test_sequence_fade_out_alpha_helper_semantics():
    """Locks the helper contract the orchestration agent emits against:
    no fade_out_ms → no ramp; ramp occupies exactly the final fade_out_ms;
    curves mirror _lyric_line_alpha's fade-out branch."""
    no_fade = {"role": "generative_sequence", "effect": "fade-in"}
    assert tos._sequence_fade_out_alpha(no_fade, 5.9, 6.0) == 1.0
    ov = _sequence_overlay()  # fade_out_ms=500 on a 6s window
    assert tos._sequence_fade_out_alpha(ov, 0.0, 6.0) == 1.0
    assert tos._sequence_fade_out_alpha(ov, 5.49, 6.0) == 1.0  # just before tail
    # Mid-tail, default lingering curve: 1 - 0.5**2 = 0.75
    assert abs(tos._sequence_fade_out_alpha(ov, 5.75, 6.0) - 0.75) < 0.01
    assert tos._sequence_fade_out_alpha(ov, 6.0, 6.0) == 0.0
    # sqrt curve mirrors the crossfade fade-out: 1 - sqrt(0.5) ≈ 0.293
    sqrt_ov = _sequence_overlay(fade_out_curve="sqrt")
    assert abs(tos._sequence_fade_out_alpha(sqrt_ov, 5.75, 6.0) - (1 - 0.5**0.5)) < 0.01
    # fade_out_ms longer than the window clamps to the window.
    long_tail = _sequence_overlay(fade_out_ms=10_000, end_s=1.0)
    assert tos._sequence_fade_out_alpha(long_tail, 0.5, 1.0) < 1.0


# -- Sequence composite stream (one FFmpeg input for the whole sequence) -------
#
# FFmpeg burn cost scales ~linearly with input count (every output frame flows
# through every chained overlay stage), so an 80-block editorial sequence as 80
# image2 inputs blows the burn budget. >= 2 sequence overlays must composite
# into ONE full-canvas PNG stream: one input, one overlay stage, per-frame
# visibility carried by the PNGs (transparent frames during clear gaps).


def _seq_block(text: str, start: float, end: float, **kw) -> dict:
    """A production-shaped sequence block: static pop (instant, settled),
    optional fade_out_ms tail — what build_sequence_overlays emits."""
    kw.setdefault("effect", "static")
    kw.setdefault("fade_out_ms", None)
    return _sequence_overlay(text=text, start_s=start, end_s=end, **kw)


def test_sequence_composite_single_ffmpeg_input_for_many_blocks(tmp_workdir):
    """N>=2 sequence overlays must become exactly ONE image2 input + ONE
    overlay filter stage, enabled across the union [min start, max end]."""
    overlays = [
        _seq_block("when the", 0.5, 4.0),
        _seq_block("days we lost", 1.2, 4.0, position_y_frac=0.48),
        _seq_block("come back", 4.0, 9.0, fade_out_ms=350),
    ]
    in_path = os.path.join(tmp_workdir, "in.mp4")
    out_path = os.path.join(tmp_workdir, "out.mp4")
    with open(in_path, "wb") as f:
        f.write(b"\x00")
    with mock.patch("app.pipeline.text_overlay_skia.subprocess.run") as run_mock:
        run_mock.return_value.returncode = 0
        tos.burn_text_overlays_skia(in_path, overlays, out_path, tmp_workdir)
    cmd = run_mock.call_args[0][0]
    assert cmd.count("-i") == 2, f"expected video + 1 composite input, got cmd {cmd}"
    assert any("skia_seq_composite_f%05d.png" in str(a) for a in cmd)
    fc = cmd[cmd.index("-filter_complex") + 1]
    assert fc.count("overlay=") == 1, f"expected ONE overlay stage, got: {fc}"
    assert "between(t,0.5000,9.0000)" in fc, f"composite must span the union window: {fc}"
    assert "setpts=PTS+0.5000/TB" in fc


def test_sequence_composite_keeps_non_sequence_overlays_on_their_own_inputs(tmp_workdir):
    """Lyric/intro/legacy overlays keep the per-overlay path untouched; the
    composite is appended LAST so sequence text composites on top."""
    intro = {
        "text": "hello",
        "role": "generative_intro",
        "effect": "static",
        "start_s": 0.0,
        "end_s": 2.0,
        "position": "center",
    }
    overlays = [
        intro,
        _seq_block("first", 1.0, 3.5),
        _seq_block("second", 2.0, 6.0, position_y_frac=0.52),
    ]
    in_path = os.path.join(tmp_workdir, "in.mp4")
    out_path = os.path.join(tmp_workdir, "out.mp4")
    with open(in_path, "wb") as f:
        f.write(b"\x00")
    with mock.patch("app.pipeline.text_overlay_skia.subprocess.run") as run_mock:
        run_mock.return_value.returncode = 0
        tos.burn_text_overlays_skia(in_path, overlays, out_path, tmp_workdir)
    cmd = run_mock.call_args[0][0]
    # video + 1 static intro PNG + 1 composite = 3 inputs, 2 overlay stages.
    assert cmd.count("-i") == 3, f"got cmd {cmd}"
    fc = cmd[cmd.index("-filter_complex") + 1]
    assert fc.count("overlay=") == 2
    # The LAST overlay stage is the composite (union window of the 2 blocks).
    assert "between(t,1.0000,6.0000)" in fc.split(";")[-1]


def test_sequence_composite_frame_economy_links_settled_frames(tmp_workdir):
    """A 3-block scene renders unique frames only at pops + the fade ramp;
    the settled holds and the seam frame hard-link to existing frames."""
    blocks = [
        _seq_block("one", 0.0, 4.0, fade_out_ms=350, position_y_frac=0.30),
        _seq_block("two", 1.0, 4.0, fade_out_ms=350, position_y_frac=0.42),
        _seq_block("three", 2.0, 4.0, fade_out_ms=350, position_y_frac=0.54),
    ]
    seq = tos._render_sequence_composite(blocks, 10.0, tmp_workdir)
    assert seq is not None
    assert seq["is_animated"] is True
    assert seq["start_s"] == 0.0
    assert seq["end_s"] == 4.0
    # 4s * 30fps logical frames + 1 seam hold frame, all present on disk.
    assert seq["n_frames"] == int(round(4.0 * tos.FPS)) + 1
    frames = sorted(
        os.path.join(tmp_workdir, f)
        for f in os.listdir(tmp_workdir)
        if f.startswith("skia_seq_composite_f")
    )
    assert len(frames) == seq["n_frames"]
    # Unique renders: 3 pop states ({one}, {one,two}, {one,two,three}) + the
    # ~11-frame fade ramp. Everything else links.
    unique_inodes = {os.stat(f).st_ino for f in frames}
    assert len(unique_inodes) <= 20, (
        f"{len(unique_inodes)} unique frames for a 3-block scene — settled "
        "holds must be hard links, not re-renders"
    )
    # Settled holds inside the same pop state share one inode.
    f_25 = os.path.join(tmp_workdir, "skia_seq_composite_f00075.png")  # t=2.5
    f_30 = os.path.join(tmp_workdir, "skia_seq_composite_f00090.png")  # t=3.0
    assert os.path.samefile(f_25, f_30)
    # Frames in different pop states must NOT share content.
    f_05 = os.path.join(tmp_workdir, "skia_seq_composite_f00015.png")  # t=0.5
    assert not os.path.samefile(f_05, f_25)


def test_sequence_composite_per_frame_visibility_and_fade(tmp_workdir):
    """Pixel-level contract on a tiny 2-scene sequence: scene-0 frames carry
    only scene-0 pixels, the clear gap is fully transparent (but the frames
    exist — contiguous image2 numbering), scene-1 frames carry only scene-1
    pixels, and the fade tail's alpha decreases."""

    def _region_max_alpha(i: int, *, top: bool) -> int:
        path = os.path.join(tmp_workdir, f"skia_seq_composite_f{i:05d}.png")
        im = Image.open(path).convert("RGBA")
        half = im.height // 2
        box = (0, 0, im.width, half) if top else (0, half, im.width, im.height)
        return im.crop(box).split()[-1].getextrema()[1]

    blocks = [
        # Scene 0: two blocks in the TOP half, fading out over 300ms.
        _seq_block("when the", 0.0, 2.0, fade_out_ms=300, position_y_frac=0.22),
        _seq_block("days we lost", 0.4, 2.0, fade_out_ms=300, position_y_frac=0.32),
        # Clear gap [2.0, 3.0], then scene 1 in the BOTTOM half.
        _seq_block("come back", 3.0, 5.0, position_y_frac=0.75),
    ]
    seq = tos._render_sequence_composite(blocks, 10.0, tmp_workdir)
    assert seq is not None
    # t=1.0 — scene-0 hold: pixels in the top half only, fully opaque.
    assert _region_max_alpha(30, top=True) == 255
    assert _region_max_alpha(30, top=False) == 0
    # t=2.5 — clear gap: the frame exists and is fully transparent.
    assert _region_max_alpha(75, top=True) == 0
    assert _region_max_alpha(75, top=False) == 0
    # t=4.0 — scene-1 hold: pixels in the bottom half only.
    assert _region_max_alpha(120, top=True) == 0
    assert _region_max_alpha(120, top=False) == 255
    # Fade tail (starts t=1.7): alpha decreases monotonically toward the end.
    a_mid_fade = _region_max_alpha(55, top=True)  # t≈1.833
    a_late_fade = _region_max_alpha(59, top=True)  # t≈1.967
    assert 150 < a_mid_fade < 250, f"mid-fade must be dimmed, got {a_mid_fade}"
    assert 0 < a_late_fade < 120, f"late fade must be near-transparent, got {a_late_fade}"
    assert a_late_fade < a_mid_fade


def test_single_sequence_overlay_keeps_per_overlay_path(tmp_workdir):
    """0/1 sequence overlays stay on the pre-existing per-overlay path —
    no composite, identical pattern naming."""
    solo = [_seq_block("solo", 0.0, 3.0)]
    assert tos._render_sequence_composite(solo, 10.0, tmp_workdir) is None
    with mock.patch.object(tos, "_ffmpeg_burn_pngs") as burn_mock:
        in_path = os.path.join(tmp_workdir, "in.mp4")
        out_path = os.path.join(tmp_workdir, "out.mp4")
        with open(in_path, "wb") as f:
            f.write(b"\x00")
        tos.burn_text_overlays_skia(in_path, [_seq_block("solo", 0.0, 3.0)], out_path, tmp_workdir)
        sequences = burn_mock.call_args[0][1]
    assert len(sequences) == 1
    assert "skia_overlay_000" in sequences[0]["pattern"], (
        "single sequence overlay must keep the per-overlay path/naming"
    )


def test_masonry_sequence_origins_survive_render_and_bypass_composite(tmp_workdir):
    blocks = [
        _seq_block(
            "static late",
            0.0,
            2.0,
            masonry_layer_origin_x_px=600.0,
        ),
        _seq_block(
            "animated later",
            1.0,
            3.0,
            effect="fade-in",
            masonry_layer_origin_x_px=760.0,
            position_y_frac=0.58,
        ),
    ]

    with mock.patch.object(tos, "_render_sequence_composite") as composite_mock:
        sequences, _work_dir = tos.render_text_overlay_sequences(blocks, tmp_workdir)

    composite_mock.assert_not_called()
    assert [sequence["layer_origin_x_px"] for sequence in sequences] == [600.0, 760.0]
    assert [sequence["is_animated"] for sequence in sequences] == [False, True]


def test_masonry_origin_keeps_unshifted_sequence_blocks_composited(tmp_workdir):
    blocks = [
        _seq_block("first", 0.0, 2.0),
        _seq_block("second", 0.5, 2.0, position_y_frac=0.58),
        _seq_block(
            "late pocket",
            1.0,
            3.0,
            masonry_layer_origin_x_px=600.0,
            position_y_frac=0.72,
        ),
    ]

    sequences, _work_dir = tos.render_text_overlay_sequences(blocks, tmp_workdir)

    assert len(sequences) == 2
    composite, shifted = sequences
    assert "sequence_group_000" in composite["pattern"]
    assert "skia_seq_composite_" in composite["pattern"]
    assert "layer_origin_x_px" not in composite
    assert shifted["layer_origin_x_px"] == 600.0
    assert "sequence_group_001" in shifted["pattern"]
    assert "skia_overlay_" in shifted["pattern"]


def test_sequence_composite_falls_back_when_fewer_than_two_valid_blocks(tmp_workdir):
    """Two sequence overlays where one fails validation (empty text) → the
    composite declines and the surviving block renders per-overlay."""
    overlays = [_seq_block("", 0.0, 3.0), _seq_block("valid", 0.0, 3.0)]
    with mock.patch.object(tos, "_ffmpeg_burn_pngs") as burn_mock:
        in_path = os.path.join(tmp_workdir, "in.mp4")
        out_path = os.path.join(tmp_workdir, "out.mp4")
        with open(in_path, "wb") as f:
            f.write(b"\x00")
        tos.burn_text_overlays_skia(in_path, overlays, out_path, tmp_workdir)
        sequences = burn_mock.call_args[0][1]
    assert len(sequences) == 1
    assert "skia_overlay_" in sequences[0]["pattern"]


def test_burn_frees_png_work_dir_after_encode(tmp_workdir, monkeypatch):
    """The large PNG frame sequences must be deleted right after the encode so
    they don't accumulate on the worker's RAM-backed scratch across overlays and
    variants (the prod /tmp-exhaustion fix)."""
    seen = {}

    def fake_render(_overlays, _slot_dur, work_dir, *, matte=None):
        os.makedirs(work_dir, exist_ok=True)
        with open(os.path.join(work_dir, "frame0000.png"), "wb") as f:
            f.write(b"png-bytes")
        seen["work_dir"] = work_dir
        return [
            {
                "is_animated": False,
                "first_frame": os.path.join(work_dir, "frame0000.png"),
                "start_s": 0.0,
                "end_s": 1.0,
                "n_frames": 1,
            }
        ]

    def fake_burn(_inp, _seqs, output_path):
        with open(output_path, "wb") as f:
            f.write(b"out")

    monkeypatch.setattr(tos, "_render_overlay_sequences", fake_render)
    monkeypatch.setattr(tos, "_ffmpeg_burn_pngs", fake_burn)

    in_path = os.path.join(tmp_workdir, "in.mp4")
    out_path = os.path.join(tmp_workdir, "out.mp4")
    with open(in_path, "wb") as f:
        f.write(b"in")

    tos.burn_text_overlays_skia(
        in_path,
        [{"text": "hi", "start_s": 0.0, "end_s": 1.0}],
        out_path,
        tmp_workdir,
    )

    assert os.path.exists(out_path)
    assert "work_dir" in seen
    assert not os.path.exists(seen["work_dir"])  # PNG scratch freed
