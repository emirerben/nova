import math
from pathlib import Path

import pytest

from app.kria.recipes_v2 import EditRecipeV2
from app.pipeline import phone_captions
from app.pipeline.captions import SUBTITLED_CAPTION_MARGIN_V, y_frac_to_margin_v
from app.pipeline.narrated_assembler import is_valid_caption_font
from app.pipeline.phone_captions import (
    CAPTION_FONT_FAMILY,
    MAX_CAPTION_LAYERS,
    PhoneCaptionLook,
    caption_font_assets,
    caption_ink_box,
    caption_look_from_variant,
    compile_caption_layers,
    watermark_keepout_rect,
)
from app.pipeline.phone_guided_plan import UnsupportedPhonePlan
from app.pipeline.text_overlay import _FONT_REGISTRY


def _word(text: str, start_s: float, end_s: float) -> dict:
    return {"text": text, "start_s": start_s, "end_s": end_s}


def test_sentence_style_compiles_one_pop_in_layer_per_cue():
    cues = [
        {"text": "Hello there", "start_s": 0.0, "end_s": 1.5},
        {"text": "How are you", "start_s": 1.5, "end_s": 3.0},
    ]
    layers = compile_caption_layers(cues, canvas_width=1080, canvas_height=1920)

    assert [layer.id for layer in layers] == ["caption-0", "caption-1"]
    assert [layer.effect for layer in layers] == ["pop-in", "pop-in"]
    assert layers[0].start == pytest.approx(0.0)
    assert layers[0].end == pytest.approx(1.5)
    assert layers[1].start == pytest.approx(1.5)
    assert layers[1].end == pytest.approx(3.0)
    # Bottom-safe-zone placement mirrors captions.py::SUBTITLED_CAPTION_MARGIN_V
    # (384px of a 1920px-tall canvas) -> y_frac 0.8 -> anchor_y 1536.
    assert layers[0].anchor_y == pytest.approx(1536.0)
    assert layers[0].anchor_x == pytest.approx(540.0)
    assert layers[0].runs[0].font_asset_id == "font-TikTokSans-Regular.ttf"
    assert layers[0].karaoke is None


def test_word_style_compiles_karaoke_layer_with_relative_word_starts():
    cues = [
        {
            "text": "Hello there",
            "start_s": 10.0,
            "end_s": 11.0,
            "words": [_word("Hello", 10.0, 10.4), _word("there", 10.4, 11.0)],
        }
    ]
    layers = compile_caption_layers(cues, canvas_width=1080, canvas_height=1920, style="word")

    assert len(layers) == 1
    layer = layers[0]
    assert layer.effect == "karaoke-line"
    assert layer.karaoke is not None
    # Word times are absolute (base-clip time) on the cue; the compiled layer
    # must re-express them relative to the cue's own start (10.0).
    assert layer.karaoke.starts == pytest.approx([0.0, 0.4])
    assert len(layer.karaoke.starts) == len(layer.runs)
    assert [run.text for run in layer.runs] == ["Hello", "there"]


def test_stale_word_timings_are_respread_over_the_edited_text():
    """KRI-280 / plan 026 R11: a text edit that kept the old `words` (the chat
    edit path) must not burn the pre-edit words on a karaoke line."""
    cues = [
        {
            "text": "Packed and ready",
            "start_s": 10.0,
            "end_s": 11.5,
            "words": [
                _word("First", 10.0, 10.4),
                _word("we", 10.5, 10.8),
                _word("pack", 10.9, 11.5),
            ],
        }
    ]
    layer = compile_caption_layers(cues, canvas_width=1080, canvas_height=1920, style="word")[0]

    assert [run.text for run in layer.runs] == ["Packed", "and", "ready"]
    # The iOS preview's own fallback: equal slices of the cue window.
    assert layer.karaoke.starts == pytest.approx([0.0, 0.5, 1.0])


def test_words_that_still_spell_the_text_keep_their_real_timings():
    # Compared without whitespace: an ASR split the text doesn't share ("Wow"
    # + "!" for "Wow!", or a language written without spaces) still spells
    # the cue, so its real timings stay.
    cues = [
        {
            "text": "Wow! Look",
            "start_s": 0.0,
            "end_s": 1.0,
            "words": [_word("Wow", 0.0, 0.2), _word("!", 0.2, 0.3), _word("Look", 0.7, 1.0)],
        }
    ]
    layer = compile_caption_layers(cues, canvas_width=1080, canvas_height=1920, style="word")[0]

    assert [run.text for run in layer.runs] == ["Wow", "!", "Look"]
    assert layer.karaoke.starts == pytest.approx([0.0, 0.2, 0.7])


def test_word_style_without_word_timings_falls_back_to_sentence():
    cues = [{"text": "No timings here", "start_s": 0.0, "end_s": 2.0}]
    layers = compile_caption_layers(cues, canvas_width=1080, canvas_height=1920, style="word")

    assert len(layers) == 1
    assert layers[0].effect == "pop-in"
    assert layers[0].karaoke is None


def test_word_style_with_empty_words_list_falls_back_to_sentence():
    cues = [{"text": "No timings here", "start_s": 0.0, "end_s": 2.0, "words": []}]
    layers = compile_caption_layers(cues, canvas_width=1080, canvas_height=1920, style="word")

    assert layers[0].effect == "pop-in"


def test_cues_are_sorted_and_overlapping_cue_end_is_clamped():
    cues = [
        {"text": "second", "start_s": 5.0, "end_s": 8.0},
        {"text": "first", "start_s": 0.0, "end_s": 6.0},  # overlaps "second" by 1s
    ]
    layers = compile_caption_layers(cues, canvas_width=1080, canvas_height=1920)

    assert [layer.id for layer in layers] == ["caption-0", "caption-1"]
    assert [run.text for layer in layers for run in layer.runs] == ["first", "second"]
    assert layers[0].start == pytest.approx(0.0)
    assert layers[0].end == pytest.approx(5.0)  # clamped to the next cue's start
    assert layers[1].start == pytest.approx(5.0)
    assert layers[1].end == pytest.approx(8.0)


def test_empty_text_cues_are_dropped_without_leaving_an_id_gap():
    cues = [
        {"text": "   ", "start_s": 0.0, "end_s": 1.0},
        {"text": "real caption", "start_s": 1.0, "end_s": 2.0},
    ]
    layers = compile_caption_layers(cues, canvas_width=1080, canvas_height=1920)

    assert len(layers) == 1
    assert layers[0].id == "caption-0"


def test_timeline_duration_clamps_and_drops_out_of_bounds_cues():
    cues = [
        {"text": "in bounds", "start_s": 5.0, "end_s": 15.0},
        {"text": "fully past the end", "start_s": 12.0, "end_s": 20.0},
    ]
    layers = compile_caption_layers(
        cues, canvas_width=1080, canvas_height=1920, timeline_duration_s=10.0
    )

    assert len(layers) == 1
    assert layers[0].end == pytest.approx(10.0)


def test_layer_count_over_cap_raises_unsupported():
    cues = [
        {"text": f"cue {i}", "start_s": i * 0.05, "end_s": i * 0.05 + 0.02}
        for i in range(MAX_CAPTION_LAYERS + 1)
    ]
    with pytest.raises(UnsupportedPhonePlan, match="text-layer cap"):
        compile_caption_layers(cues, canvas_width=1080, canvas_height=1920)


def test_matches_edit_recipe_v2_text_layer_cap():
    field = EditRecipeV2.model_fields["text_layers"]
    max_len = next(m.max_length for m in field.metadata if hasattr(m, "max_length"))
    assert MAX_CAPTION_LAYERS == max_len


def test_id_prefix_is_honored():
    cues = [{"text": "hi", "start_s": 0.0, "end_s": 1.0}]
    layers = compile_caption_layers(cues, canvas_width=1080, canvas_height=1920, id_prefix="cap")
    assert layers[0].id == "cap-0"


def test_caption_font_assets_resolves_the_bundled_tiktok_sans_font():
    cues = [{"text": "hi there", "start_s": 0.0, "end_s": 1.0}]
    layers = compile_caption_layers(cues, canvas_width=1080, canvas_height=1920)
    assets = caption_font_assets(layers)

    assert set(assets) == {"font-TikTokSans-Regular.ttf"}
    asset = assets["font-TikTokSans-Regular.ttf"]
    assert asset.kind == "library"
    assert asset.catalog == "font"
    assert asset.catalog_id == "TikTokSans-Regular.ttf"
    assert asset.fingerprint.byte_count > 0
    assert CAPTION_FONT_FAMILY == "TikTok Sans"


def test_empty_cue_list_compiles_to_no_layers():
    assert compile_caption_layers([], canvas_width=1080, canvas_height=1920) == []


# --- PhoneCaptionLook (KRI-216) -----------------------------------------------


def test_default_look_is_byte_identical_to_no_look_and_to_look_none():
    cues = [{"text": "Hello there", "start_s": 0.0, "end_s": 1.5}]
    legacy = compile_caption_layers(cues, canvas_width=1080, canvas_height=1920)
    explicit_none = compile_caption_layers(cues, canvas_width=1080, canvas_height=1920, look=None)
    explicit_default = compile_caption_layers(
        cues, canvas_width=1080, canvas_height=1920, look=PhoneCaptionLook()
    )
    assert legacy == explicit_none == explicit_default


def test_captions_enabled_false_compiles_no_layers():
    cues = [{"text": "Hello there", "start_s": 0.0, "end_s": 1.5}]
    look = PhoneCaptionLook(captions_enabled=False)
    assert compile_caption_layers(cues, canvas_width=1080, canvas_height=1920, look=look) == []


def test_every_valid_caption_font_key_compiles_to_its_own_font_file():
    cue = [{"text": "hi", "start_s": 0.0, "end_s": 1.0}]
    checked = 0
    for key, entry in _FONT_REGISTRY.get("fonts", {}).items():
        if not is_valid_caption_font(key):
            continue
        look = PhoneCaptionLook(font_family=key)
        layers = compile_caption_layers(cue, canvas_width=1080, canvas_height=1920, look=look)
        expected_asset_id = "font-" + entry["file"]
        assert layers[0].runs[0].font_asset_id == expected_asset_id, key
        checked += 1
    assert checked >= 40  # sanity: the registry loaded and most fonts are non-deprecated


def test_text_size_color_and_outline_flow_through_look():
    look = PhoneCaptionLook(text_size_px=100, text_color="#112233", outline_px=8)
    cues = [{"text": "hi", "start_s": 0.0, "end_s": 1.0}]
    layers = compile_caption_layers(cues, canvas_width=1080, canvas_height=1920, look=look)
    run = layers[0].runs[0]
    assert run.font_size == pytest.approx(100)
    assert run.stroke_width == pytest.approx(16)  # outline_px * 2, mirrors the karaoke path
    assert run.fill.red == pytest.approx(0x11 / 255)
    assert run.fill.green == pytest.approx(0x22 / 255)
    assert run.fill.blue == pytest.approx(0x33 / 255)


def test_position_y_frac_from_look_moves_the_anchor():
    look = PhoneCaptionLook(position_y_frac=0.5)
    cues = [{"text": "hi", "start_s": 0.0, "end_s": 1.0}]
    layers = compile_caption_layers(cues, canvas_width=1080, canvas_height=1920, look=look)
    assert layers[0].anchor_y == pytest.approx(960.0)


def test_alignment_left_sets_anchor_x_and_keeps_vertical_position():
    cues = [{"text": "hi", "start_s": 0.0, "end_s": 1.0}]
    centered = compile_caption_layers(cues, canvas_width=1080, canvas_height=1920)
    look = PhoneCaptionLook(text_anchor="left", position_x_frac=80 / 1080)
    layers = compile_caption_layers(cues, canvas_width=1080, canvas_height=1920, look=look)
    assert layers[0].anchor_x == pytest.approx(80.0)
    # `vertical_anchor="center"` (set alongside `text_anchor`) keeps the same
    # y anchor a "left" text_anchor would otherwise flip to a TOP anchor.
    assert layers[0].anchor_y == pytest.approx(centered[0].anchor_y)


def test_alignment_right_sets_anchor_x_from_the_far_margin():
    cues = [{"text": "hi", "start_s": 0.0, "end_s": 1.0}]
    look = PhoneCaptionLook(text_anchor="right", position_x_frac=1 - 80 / 1080)
    layers = compile_caption_layers(cues, canvas_width=1080, canvas_height=1920, look=look)
    assert layers[0].anchor_x == pytest.approx(1000.0)


def test_shadow_enabled_false_with_no_other_override_disables_the_shadow():
    cues = [{"text": "hi", "start_s": 0.0, "end_s": 1.0}]
    default_layers = compile_caption_layers(cues, canvas_width=1080, canvas_height=1920)
    assert len(default_layers[0].runs[0].blur_layers) > 0  # today's implicit standard shadow

    look = PhoneCaptionLook(shadow_enabled=False)
    layers = compile_caption_layers(cues, canvas_width=1080, canvas_height=1920, look=look)
    assert layers[0].runs[0].blur_layers == []


def test_shadow_opacity_zero_disables_the_shadow():
    look = PhoneCaptionLook(shadow_color="#ff0000", shadow_opacity=0.0)
    cues = [{"text": "hi", "start_s": 0.0, "end_s": 1.0}]
    layers = compile_caption_layers(cues, canvas_width=1080, canvas_height=1920, look=look)
    assert layers[0].runs[0].blur_layers == []


def test_stroke_color_overrides_the_outline_ink():
    look = PhoneCaptionLook(stroke_color="#00ff00")
    cues = [{"text": "hi", "start_s": 0.0, "end_s": 1.0}]
    layers = compile_caption_layers(cues, canvas_width=1080, canvas_height=1920, look=look)
    stroke = layers[0].runs[0].stroke
    assert stroke.red == pytest.approx(0.0, abs=1e-3)
    assert stroke.green == pytest.approx(1.0, abs=1e-3)
    assert stroke.blue == pytest.approx(0.0, abs=1e-3)


def test_default_word_style_highlight_color_is_lime():
    cues = [
        {
            "text": "Hello there",
            "start_s": 0.0,
            "end_s": 1.0,
            "words": [_word("Hello", 0.0, 0.4), _word("there", 0.4, 1.0)],
        }
    ]
    layers = compile_caption_layers(cues, canvas_width=1080, canvas_height=1920, style="word")
    highlight = layers[0].karaoke.highlight
    assert highlight.red == pytest.approx(0x84 / 255)
    assert highlight.green == pytest.approx(0xCC / 255)
    assert highlight.blue == pytest.approx(0x16 / 255)


def test_highlight_spoken_word_true_forces_karaoke_for_sentence_style():
    cues = [
        {
            "text": "Hello there",
            "start_s": 0.0,
            "end_s": 1.0,
            "words": [_word("Hello", 0.0, 0.4), _word("there", 0.4, 1.0)],
        }
    ]
    look = PhoneCaptionLook(highlight_spoken_word=True)
    layers = compile_caption_layers(
        cues, canvas_width=1080, canvas_height=1920, style="sentence", look=look
    )
    assert layers[0].effect == "karaoke-line"


def test_highlight_spoken_word_false_keeps_karaoke_but_drops_the_tint():
    cues = [
        {
            "text": "Hello there",
            "start_s": 0.0,
            "end_s": 1.0,
            "words": [_word("Hello", 0.0, 0.4), _word("there", 0.4, 1.0)],
        }
    ]
    look = PhoneCaptionLook(highlight_spoken_word=False, text_color="#123456")
    layers = compile_caption_layers(
        cues, canvas_width=1080, canvas_height=1920, style="word", look=look
    )
    layer = layers[0]
    assert layer.effect == "karaoke-line"  # word display, but without the lime tint
    highlight = layer.karaoke.highlight
    assert highlight.red == pytest.approx(0x12 / 255)
    assert highlight.green == pytest.approx(0x34 / 255)
    assert highlight.blue == pytest.approx(0x56 / 255)


# --- caption_look_from_variant (KRI-216) ---------------------------------------


def test_caption_look_from_variant_empty_variant_is_the_default_look():
    assert caption_look_from_variant({}) == PhoneCaptionLook()


def test_caption_look_from_variant_captions_enabled_false():
    assert caption_look_from_variant({"captions_enabled": False}).captions_enabled is False


def test_caption_look_from_variant_valid_font_key_passes_through_raw():
    look = caption_look_from_variant({"voiceover_caption_font": "Montserrat Bold"})
    # NOT resolved to its ASS name ("Montserrat") -- see the module docstring
    # on why the raw registry key must survive onto the phone overlay.
    assert look.font_family == "Montserrat Bold"


def test_caption_look_from_variant_unknown_font_falls_back():
    look = caption_look_from_variant({"voiceover_caption_font": "Not A Real Font"})
    assert look.font_family == CAPTION_FONT_FAMILY


def test_caption_look_from_variant_deprecated_font_falls_back():
    deprecated_key = next(k for k, v in _FONT_REGISTRY["fonts"].items() if v.get("deprecated"))
    look = caption_look_from_variant({"voiceover_caption_font": deprecated_key})
    assert look.font_family == CAPTION_FONT_FAMILY


def test_caption_look_from_variant_size_px_is_clamped():
    assert caption_look_from_variant({"caption_size_px": 10}).text_size_px == 36
    assert caption_look_from_variant({"caption_size_px": 999}).text_size_px == 160
    assert caption_look_from_variant({"caption_size_px": 92}).text_size_px == 92


def test_caption_look_from_variant_stroke_width_is_clamped():
    assert caption_look_from_variant({"caption_stroke_width": -1}).outline_px == 0
    assert caption_look_from_variant({"caption_stroke_width": 99}).outline_px == 12


def test_caption_look_from_variant_colors_override_and_invalid_falls_back():
    look = caption_look_from_variant(
        {"caption_text_color": "#112233", "caption_highlight_color": "#445566"}
    )
    assert look.text_color == "#112233"
    assert look.highlight_color == "#445566"
    assert caption_look_from_variant({"caption_text_color": "not-a-color"}).text_color == "#FFFFFF"


def test_caption_look_from_variant_margin_v_absent_uses_legacy_default():
    look = caption_look_from_variant({})
    assert look.position_y_frac == pytest.approx(1 - SUBTITLED_CAPTION_MARGIN_V / 1920)


def test_caption_look_from_variant_margin_v_valid_is_honored():
    margin = y_frac_to_margin_v(0.5)
    look = caption_look_from_variant({"caption_margin_v": margin})
    assert look.position_y_frac == pytest.approx(1 - margin / 1920)


def test_caption_look_from_variant_margin_v_out_of_band_falls_back():
    look = caption_look_from_variant({"caption_margin_v": -100})
    assert look.position_y_frac == pytest.approx(1 - SUBTITLED_CAPTION_MARGIN_V / 1920)


def test_caption_look_from_variant_alignment_left():
    look = caption_look_from_variant({"caption_editor_style": {"alignment": "left"}})
    assert look.text_anchor == "left"
    assert look.position_x_frac == pytest.approx(80 / 1080)


def test_caption_look_from_variant_alignment_right():
    look = caption_look_from_variant({"caption_editor_style": {"alignment": "right"}})
    assert look.text_anchor == "right"
    assert look.position_x_frac == pytest.approx(1 - 80 / 1080)


def test_caption_look_from_variant_alignment_center_is_none():
    look = caption_look_from_variant({"caption_editor_style": {"alignment": "center"}})
    assert look.text_anchor is None
    assert look.position_x_frac == pytest.approx(0.5)


def test_caption_look_from_variant_highlight_spoken_word_passthrough():
    on = caption_look_from_variant({"caption_editor_style": {"highlight_spoken_word": True}})
    off = caption_look_from_variant({"caption_editor_style": {"highlight_spoken_word": False}})
    absent = caption_look_from_variant({})
    assert on.highlight_spoken_word is True
    assert off.highlight_spoken_word is False
    assert absent.highlight_spoken_word is None


def test_caption_look_from_variant_shadow_enabled_top_level():
    assert caption_look_from_variant({"caption_shadow_enabled": False}).shadow_enabled is False
    assert caption_look_from_variant({"caption_shadow_enabled": True}).shadow_enabled is True


def test_caption_look_from_variant_editor_style_shadow_enabled_wins_over_top_level():
    look = caption_look_from_variant(
        {
            "caption_shadow_enabled": False,
            "caption_editor_style": {"shadow_enabled": True},
        }
    )
    assert look.shadow_enabled is True


def test_caption_look_from_variant_stroke_and_shadow_from_editor_style():
    look = caption_look_from_variant(
        {
            "caption_editor_style": {
                "stroke_color": "#abcdef",
                "shadow_color": "#000011",
                "shadow_opacity": 0.4,
            }
        }
    )
    assert look.stroke_color == "#ABCDEF"
    assert look.shadow_color == "#000011"
    assert look.shadow_opacity == pytest.approx(0.4)


def test_explicit_highlight_toggle_uses_the_editor_default_highlight_color():
    """Parity with `captions._write_editor_caption_cues`: once the creator sets
    `highlight_spoken_word`, an unset highlight color is #C5F82A, not the
    legacy word-pop lime."""
    from app.pipeline.phone_captions import caption_look_from_variant

    assert caption_look_from_variant({}).highlight_color == "#84CC16"
    toggled = {"caption_editor_style": {"highlight_spoken_word": True}}
    assert caption_look_from_variant(toggled).highlight_color == "#C5F82A"
    explicit = {**toggled, "caption_highlight_color": "#ff0000"}
    assert caption_look_from_variant(explicit).highlight_color == "#FF0000"


# --- KRI-548: captions make room for the device watermark ------------------------
#
# Every phone export draws the Kria mark bottom-left, ABOVE text
# (KriaMediaEngine/Branding.swift). Prod 2026-10-08: a Turkish Talking edit's
# wide first lines ran under it ("İlk durak..." lost the İ, "Üçüncü..." the Ü).

_REPO_ROOT = Path(__file__).resolve().parents[5]
_API_MARK = Path(__file__).resolve().parents[2] / "assets/branding/kria-watermark-mist-standard.png"
_ENGINE_RESOURCES = (
    _REPO_ROOT / "src/apps/ios/Packages/KriaMediaEngine/Sources/KriaMediaEngine/Resources"
)
# `KriaBranding.tileTransform` on 1080x1920: the padded tile's own origin.
_TILE_LEFT_PX = 30
_TILE_BOTTOM_INSET_PX = 415

_KADIKOY_ILK = "İlk durak Moda'da, deniz kenarında küçücük bir yer."
_KADIKOY_UCUNCU = "Üçüncü ve en sevdiğim yer Yeldeğirmeni'nde."
# Turkish and English sentences that wrap to 1-3 lines at the default look,
# with lines wide enough to reach the mark's column.
_WIDE_CAPTIONS = [
    _KADIKOY_ILK,
    _KADIKOY_UCUNCU,
    "Bakın, ben günde 4 kahve içiyorum, o yüzden bu konuda biraz uzmanım.",
    "Selam, bugün sizi Kadıköy'de en sevdiğim 3 kahveciye götürüyorum.",
    "Biraz kalabalık ama kahve fiyatları çok uygun.",
    "İkinci durak Bahariye'de.",
    "So today I'm taking you to my three favourite coffee spots in Kadikoy.",
    "Honestly this is the best filter coffee I have had all year long.",
    "What's up everyone, welcome back to the channel!",
]
# The default y (SUBTITLED_CAPTION_MARGIN_V), the iOS editor's preview default,
# the cloud face ladder (`render_geometry.choose_caption_y_frac` candidates)
# and an even sweep of the editor's whole 0.30-0.90 band.
_CAPTION_Y_FRACS = sorted(
    {0.8, 0.82, 0.705, 0.62, 0.78, 0.55, 0.86}
    | {round(0.30 + 0.03 * step, 2) for step in range(21)}
)


def _cues(texts: list[str], *, words: bool = False) -> list[dict]:
    cues = []
    for index, text in enumerate(texts):
        start = index * 2.0
        cue: dict = {"text": text, "start_s": start, "end_s": start + 1.9}
        if words:
            cue["words"] = [
                _word(token, start + 0.1 * k, start + 0.1 * (k + 1))
                for k, token in enumerate(text.split())
            ]
        cues.append(cue)
    return cues


def _look(y_frac: float = 0.8, anchor: str | None = None, **changes) -> PhoneCaptionLook:
    x_frac = {None: 0.5, "left": 80 / 1080, "right": 1000 / 1080}[anchor]
    return PhoneCaptionLook(
        position_y_frac=y_frac, text_anchor=anchor, position_x_frac=x_frac, **changes
    )


def _ink_pixels_under_the_mark(
    layers,
    *,
    canvas_width: int = 1080,
    canvas_height: int = 1920,
    font_family: str = CAPTION_FONT_FAMILY,
) -> int:
    """Rasterize every run (fill + outline stroke with round joins, the way the
    phone's PortableTextVectorPainter draws it) and count the inked pixels
    inside the mark's opaque rect plus its clearance gap. Independent of the
    bounds math the compiler uses to decide."""
    import skia

    from app.pipeline import text_overlay_skia as cloud

    left, top, right, bottom = watermark_keepout_rect(canvas_width, canvas_height)
    surface = skia.Surface(math.ceil(right - left), math.ceil(bottom - top))
    canvas = surface.getCanvas()
    canvas.clear(skia.ColorTRANSPARENT)
    canvas.translate(-left, -top)
    typeface = cloud._resolve_typeface_for_overlay({"font_family": font_family}).typeface
    for layer in layers:
        for run in layer.runs:
            font = skia.Font(typeface, run.font_size)
            font.setSubpixel(True)
            fill = skia.Paint(AntiAlias=True, Color=skia.ColorWHITE)
            canvas.drawString(run.text, run.x, run.baseline_y, font, fill)
            if run.stroke_width:
                stroke = skia.Paint(
                    AntiAlias=True,
                    Color=skia.ColorWHITE,
                    Style=skia.Paint.kStroke_Style,
                    StrokeWidth=run.stroke_width,
                )
                stroke.setStrokeJoin(skia.Paint.kRound_Join)
                canvas.drawString(run.text, run.x, run.baseline_y, font, stroke)
    return int((surface.makeImageSnapshot().toarray()[..., 3] > 0).sum())


def _compile_without_making_room(monkeypatch, *args, **kwargs):
    """Today's (pre-KRI-548) layout: the same compile with the pass switched off."""
    with monkeypatch.context() as patch:
        patch.setattr(phone_captions, "_make_room_for_watermark", lambda layers, *_a, **_k: layers)
        return compile_caption_layers(*args, **kwargs)


def _line_texts(layer) -> list[str]:
    lines: dict[float, list[str]] = {}
    for run in layer.runs:
        lines.setdefault(run.baseline_y, []).append(run.text)
    return [" ".join(lines[baseline]) for baseline in sorted(lines)]


@pytest.mark.parametrize(
    "mark",
    [
        _API_MARK,
        _ENGINE_RESOURCES / "kria-watermark-mist-standard.png",
        _ENGINE_RESOURCES / "kria-watermark-graphite-standard.png",
    ],
    ids=["api-mist", "engine-mist", "engine-graphite"],
)
def test_watermark_keepout_matches_the_bundled_mark(mark):
    """The keep-out is the mark's measured opaque extent (alpha >= 8; the
    fainter fringe is its shadow) where the engine places the tile, plus the
    clearance gap."""
    if not mark.exists():
        pytest.skip(f"{mark.name} not present in this checkout")
    import numpy as np
    from PIL import Image

    alpha = np.array(Image.open(mark).convert("RGBA"))[..., 3]
    rows, cols = np.nonzero(alpha >= 8)
    tile_top = 1920 - _TILE_BOTTOM_INSET_PX - alpha.shape[0]
    gap = phone_captions._WATERMARK_GAP_PX
    assert watermark_keepout_rect(1080, 1920) == (
        _TILE_LEFT_PX + cols.min() - gap,
        tile_top + rows.min() - gap,
        _TILE_LEFT_PX + cols.max() + 1 + gap,
        tile_top + rows.max() + 1 + gap,
    )


def test_watermark_keepout_scales_with_the_canvas_like_the_engine():
    # One scale for mark and insets: min(w/1080, h/1920) -- `tileTransform`.
    assert watermark_keepout_rect(1080, 1920) == (48.0, 1404.0, 205.0, 1487.0)
    s = 1080 / 1920
    for width, height in ((1080, 1080), (1920, 1080)):
        bottom = height - 445 * s
        assert watermark_keepout_rect(width, height) == pytest.approx(
            (48 * s, bottom - 71 * s, 205 * s, bottom + 12 * s)
        )


@pytest.mark.parametrize("style", ["sentence", "word"])
@pytest.mark.parametrize("text", [_KADIKOY_ILK, _KADIKOY_UCUNCU])
def test_prod_kadikoy_captions_no_longer_run_under_the_mark(monkeypatch, text, style):
    cues = _cues([text], words=style == "word")
    before = _compile_without_making_room(
        monkeypatch, cues, canvas_width=1080, canvas_height=1920, style=style
    )
    after = compile_caption_layers(cues, canvas_width=1080, canvas_height=1920, style=style)

    assert _ink_pixels_under_the_mark(before) > 0  # the prod bug, reproduced
    assert _ink_pixels_under_the_mark(after) == 0
    old, new = before[0], after[0]
    # Same words, timing, size and anchor -- only the line breaks moved.
    assert " ".join(_line_texts(new)) == " ".join(_line_texts(old)) == text
    assert (new.start, new.end, new.effect) == (old.start, old.end, old.effect)
    assert (new.anchor_x, new.anchor_y) == (old.anchor_x, old.anchor_y)
    assert {run.font_size for run in new.runs} == {78.0}
    if style == "word":
        assert new.karaoke.starts == old.karaoke.starts
    # Every line level with the mark is short enough to start right of it.
    from app.pipeline import text_overlay_skia as cloud

    typeface = cloud._resolve_typeface_for_overlay({"font_family": CAPTION_FONT_FAMILY}).typeface
    _left, top, right, bottom = watermark_keepout_rect(1080, 1920)
    level_with_mark = 0
    for run in new.runs:
        ink = caption_ink_box(run, typeface)
        if ink[1] < bottom and ink[3] > top:
            assert ink[0] >= right
            level_with_mark += 1
    assert level_with_mark


def test_prod_kadikoy_line_breaks():
    # The exact split depends on Skia's glyph metrics, which differ slightly between
    # macOS and Linux (local "İlk durak / Moda'da, deniz kenarında / …", CI "İlk durak
    # Moda'da, / deniz kenarında / …"). Pin what matters: both prod cues gain one line,
    # keep every word in order, and leave no ink under the mark.
    cues = [_KADIKOY_ILK, _KADIKOY_UCUNCU]
    layers = compile_caption_layers(_cues(cues), canvas_width=1080, canvas_height=1920)
    for layer, text in zip(layers, cues, strict=True):
        lines = _line_texts(layer)
        assert len(lines) == 3, lines
        assert " ".join(lines).split() == text.split()
    assert _ink_pixels_under_the_mark(layers) == 0


@pytest.mark.parametrize("style", ["sentence", "word"])
@pytest.mark.parametrize("anchor", [None, "right", "left"])
def test_wide_captions_never_ink_the_mark_wherever_the_caption_sits(anchor, style):
    """Geometry guard: 1-3 line Turkish/English captions, every y the editor,
    the default and the cloud face ladder can choose, every alignment."""
    cues = _cues(_WIDE_CAPTIONS, words=style == "word")
    for y_frac in _CAPTION_Y_FRACS:
        layers = compile_caption_layers(
            cues,
            canvas_width=1080,
            canvas_height=1920,
            style=style,
            look=_look(y_frac, anchor),
        )
        for layer, cue in zip(layers, cues, strict=True):
            assert _ink_pixels_under_the_mark([layer]) == 0, (y_frac, layer.id)
            assert " ".join(_line_texts(layer)) == cue["text"]
            assert (layer.start, layer.end) == (cue["start_s"], cue["end_s"])


@pytest.mark.parametrize("size", [36, 120, 160])
def test_custom_caption_sizes_never_ink_the_mark(size):
    cues = _cues(_WIDE_CAPTIONS)
    for anchor in (None, "left"):
        for y_frac in (0.62, 0.705, 0.74, 0.8, 0.86):
            layers = compile_caption_layers(
                cues,
                canvas_width=1080,
                canvas_height=1920,
                look=_look(y_frac, anchor, text_size_px=size),
            )
            assert _ink_pixels_under_the_mark(layers) == 0, (anchor, y_frac)


@pytest.mark.parametrize("canvas", [(1080, 1080), (1920, 1080)])
def test_square_and_landscape_canvases_never_ink_the_mark(canvas):
    """Narrated and authored-timeline recipes can carry a non-9:16 canvas; the
    engine scales the mark into the same corner, and so does the keep-out."""
    width, height = canvas
    for style in ("sentence", "word"):
        cues = _cues(_WIDE_CAPTIONS, words=style == "word")
        for y_frac in (0.62, 0.7, 0.75, 0.8, 0.86):
            layers = compile_caption_layers(
                cues, canvas_width=width, canvas_height=height, style=style, look=_look(y_frac)
            )
            assert (
                _ink_pixels_under_the_mark(layers, canvas_width=width, canvas_height=height) == 0
            ), (style, y_frac)


def test_cues_clear_of_the_mark_compile_exactly_as_before(monkeypatch):
    """No-change case: short cues at the default y, and every cue placed where
    it cannot reach the mark, are byte-identical to the pre-KRI-548 compile."""
    short = _cues(["Filtre kahve efsane.", "Hadi kahve sizden.", "Wow.", "Hello there"])
    far = _cues(_WIDE_CAPTIONS)
    for cues, look in (
        (short, None),
        (far, _look(0.4)),
        (far, _look(0.9)),
        (short, _look(0.8, "left")),
    ):
        for style in ("sentence", "word"):
            kwargs = {"canvas_width": 1080, "canvas_height": 1920, "style": style, "look": look}
            before = _compile_without_making_room(monkeypatch, cues, **kwargs)
            assert _ink_pixels_under_the_mark(before) == 0
            assert compile_caption_layers(cues, **kwargs) == before


def test_only_the_cues_on_the_mark_change(monkeypatch):
    cues = _cues(_WIDE_CAPTIONS)
    before = _compile_without_making_room(monkeypatch, cues, canvas_width=1080, canvas_height=1920)
    after = compile_caption_layers(cues, canvas_width=1080, canvas_height=1920)

    touched = [_ink_pixels_under_the_mark([layer]) > 0 for layer in before]
    assert any(touched) and not all(touched)
    for was_touching, old, new in zip(touched, before, after, strict=True):
        assert (new != old) is was_touching, old.id


def test_left_aligned_captions_share_one_edge_right_of_the_mark():
    layers = compile_caption_layers(
        _cues(_WIDE_CAPTIONS), canvas_width=1080, canvas_height=1920, look=_look(0.8, "left")
    )
    edges = {round(run.x, 3) for layer in layers for run in layer.runs}
    assert len(edges) == 1
    assert edges.pop() > watermark_keepout_rect(1080, 1920)[2]
    assert _ink_pixels_under_the_mark(layers) == 0


def test_a_caption_clear_of_the_platform_zone_never_grows_into_it(monkeypatch):
    """brand/social/README.md: below y=1530 is the platforms' caption/username
    block. Growing a line to clear the mark must not push a caption that sat
    above it down into it."""
    from app.pipeline import text_overlay_skia as cloud

    typeface = cloud._resolve_typeface_for_overlay({"font_family": CAPTION_FONT_FAMILY}).typeface

    def ink_bottom(layer) -> float:
        return max(caption_ink_box(run, typeface)[3] for run in layer.runs)

    checked = 0
    cues = _cues(_WIDE_CAPTIONS)
    for step in range(25):
        look = _look(0.6 + 0.01 * step)
        kwargs = {"canvas_width": 1080, "canvas_height": 1920, "look": look}
        before = _compile_without_making_room(monkeypatch, cues, **kwargs)
        after = compile_caption_layers(cues, **kwargs)
        for old, new in zip(before, after, strict=True):
            if new != old and ink_bottom(old) <= 1530:
                assert ink_bottom(new) <= 1530, (look.position_y_frac, old.id)
                checked += 1
    assert checked > 0


# The whole prod caption set from the 2026-10-08 Kadıköy Talking render.
_KADIKOY_PROD_CUES = [
    "Selam, bugün sizi Kadıköy'de en sevdiğim 3 kahveciye götürüyorum.",
    "Bakın, ben günde 4 kahve içiyorum, o yüzden bu konuda biraz uzmanım.",
    _KADIKOY_ILK,
    "Buranın kahve çekirdeklerini kendileri kavuruyor.",
    "Filtre kahve efsane.",
    "İkinci durak Bahariye'de.",
    "Biraz kalabalık ama kahve fiyatları çok uygun.",
    "Sabah kahve almak için birebir.",
    _KADIKOY_UCUNCU,
    "İçerisi kitaplarla dolu, kahve kokusu sokağa taşıyor.",
    "Ben her hafta sonu buradayım.",
    "Kahve içip kitap okuyorum.",
    "Siz Kadıköy'de en iyi kahve nerede diyorsunuz?",
    "Yorumlara yazın.",
    "Hadi kahve sizden.",
]


def test_the_default_look_never_moves_a_caption_only_rebreaks_it(monkeypatch):
    cues = _cues(_KADIKOY_PROD_CUES)
    before = _compile_without_making_room(monkeypatch, cues, canvas_width=1080, canvas_height=1920)
    after = compile_caption_layers(cues, canvas_width=1080, canvas_height=1920)

    assert _ink_pixels_under_the_mark(before) > 0
    assert _ink_pixels_under_the_mark(after) == 0
    for old, new in zip(before, after, strict=True):
        assert (new.anchor_x, new.anchor_y) == (old.anchor_x, old.anchor_y)
        assert len(_line_texts(new)) <= len(_line_texts(old)) + 1


@pytest.mark.parametrize("font", ["Montserrat Bold", "Syne", "Inter", "Fraunces"])
def test_wide_caption_fonts_never_ink_the_mark(font):
    """Wider faces leave less room beside the mark; the band of y where the
    last line sits level with it is where the lift fallback earns its keep."""
    for style in ("sentence", "word"):
        cues = _cues(_WIDE_CAPTIONS, words=style == "word")
        for y_frac in (0.62, 0.66, 0.7, 0.71, 0.72, 0.74, 0.76, 0.8, 0.86):
            layers = compile_caption_layers(
                cues,
                canvas_width=1080,
                canvas_height=1920,
                style=style,
                look=_look(y_frac, font_family=font),
            )
            assert _ink_pixels_under_the_mark(layers, font_family=font) == 0, (style, y_frac)
