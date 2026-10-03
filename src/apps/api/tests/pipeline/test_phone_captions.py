import pytest

from app.kria.recipes_v2 import EditRecipeV2
from app.pipeline.captions import SUBTITLED_CAPTION_MARGIN_V, y_frac_to_margin_v
from app.pipeline.narrated_assembler import is_valid_caption_font
from app.pipeline.phone_captions import (
    CAPTION_FONT_FAMILY,
    MAX_CAPTION_LAYERS,
    PhoneCaptionLook,
    caption_font_assets,
    caption_look_from_variant,
    compile_caption_layers,
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
