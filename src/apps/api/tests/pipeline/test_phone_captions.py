import pytest

from app.kria.recipes_v2 import EditRecipeV2
from app.pipeline.phone_captions import (
    CAPTION_FONT_FAMILY,
    MAX_CAPTION_LAYERS,
    caption_font_assets,
    compile_caption_layers,
)
from app.pipeline.phone_guided_plan import UnsupportedPhonePlan


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
