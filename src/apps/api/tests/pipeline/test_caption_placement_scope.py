"""Size and placement must survive the shared phone compilation paths.

Failure cases recorded before implementation: a line override leaks to another
line, a global edit erases an override, geometry misses guided captions, or
legacy omission changes an authored render.
"""

import pytest

from app.agents._schemas.text_element import TextElement
from app.pipeline.guided_story import _apply_guided_caption_meta
from app.pipeline.phone_captions import caption_look_from_variant, compile_caption_layers


def test_guided_placement_reaches_existing_text_elements():
    row = TextElement(
        id="line",
        text="Hello",
        start_s=0,
        end_s=2,
        source_params={"source": "caption_cue"},
    )
    result, _ = _apply_guided_caption_meta(
        [row],
        {
            "appearance": {"position_x_frac": 0.2, "position_y_frac": 0.3, "max_width_frac": 0.5},
        },
    )
    assert result[0].position == "custom"
    assert result[0].x_frac == pytest.approx(0.2)
    assert result[0].y_frac == pytest.approx(0.3)
    assert result[0].max_width_frac == pytest.approx(0.5)
    assert row.x_frac != 0.2


def test_line_size_and_placement_do_not_change_the_other_caption():
    cues = [
        {"id": "one", "text": "Hello", "start_s": 0, "end_s": 1},
        {"id": "two", "text": "Hello", "start_s": 1, "end_s": 2},
    ]
    look = caption_look_from_variant(
        {
            "caption_size_px": 78,
            "caption_editor_style": {
                "position_y_frac": 0.6,
                "line_overrides": {"one": {"size_px": 150, "position_y_frac": 0.3}},
            },
        }
    )
    layers = compile_caption_layers(cues, canvas_width=1080, canvas_height=1920, look=look)
    assert layers[0].runs[0].font_size == 150
    assert layers[1].runs[0].font_size == 78
    assert layers[0].anchor_y == pytest.approx(576)
    assert layers[1].anchor_y == pytest.approx(1152)


@pytest.mark.parametrize("size", [36, 78, 150, 160])
@pytest.mark.parametrize("font", ["Inter", "TikTok Sans", "Fraunces", "Pacifico"])
@pytest.mark.parametrize("canvas", [(1080, 1920), (1920, 1080), (1080, 1080)])
def test_font_size_is_in_output_pixels_for_each_canvas(size, font, canvas):
    look = caption_look_from_variant(
        {
            "voiceover_caption_font": font,
            "caption_size_px": size,
            "caption_editor_style": {"position_y_frac": 0.4},
        }
    )
    layers = compile_caption_layers(
        [{"text": "Hi", "start_s": 0, "end_s": 1}],
        canvas_width=canvas[0],
        canvas_height=canvas[1],
        look=look,
    )
    assert layers[0].runs[0].font_size == size
    assert layers[0].anchor_y == pytest.approx(canvas[1] * 0.4)


def test_guided_individual_size_wins_without_mutating_authored_rows():
    rows = [
        TextElement(
            id=key,
            text="Hello",
            start_s=index,
            end_s=index + 1,
            size_px=78,
            source_params={"source": "caption_cue"},
        )
        for index, key in enumerate(["one", "two"])
    ]
    result, _ = _apply_guided_caption_meta(
        rows,
        {
            "size_px": 90,
            "appearance": {"line_overrides": {"one": {"size_px": 150, "position_y_frac": 0.3}}},
        },
    )
    assert [row.size_px for row in result] == [150, 90]
    assert [row.size_px for row in rows] == [78, 78]
    assert result[0].y_frac == 0.3


def test_position_change_preserves_existing_per_cue_font_color_and_size_in_recipe():
    cues = [
        {
            "id": "one",
            "text": "Hi",
            "start_s": 0,
            "end_s": 1,
            "font_family": "Inter",
            "text_color": "#FF0000",
            "size_px": 100,
        },
        {
            "id": "two",
            "text": "Hi",
            "start_s": 1,
            "end_s": 2,
            "font_family": "Pacifico",
            "text_color": "#00FF00",
            "size_px": 120,
        },
    ]
    look = caption_look_from_variant({"caption_editor_style": {"position_y_frac": 0.4}})
    layers = compile_caption_layers(cues, canvas_width=1080, canvas_height=1920, look=look)
    assert [layer.runs[0].font_size for layer in layers] == [100, 120]
    assert "Inter" in layers[0].runs[0].font_asset_id
    assert "Pacifico" in layers[1].runs[0].font_asset_id
    assert (layers[0].runs[0].fill.red, layers[0].runs[0].fill.green) == (1, 0)
    assert (layers[1].runs[0].fill.red, layers[1].runs[0].fill.green) == (0, 1)
    assert all(layer.anchor_y == pytest.approx(768) for layer in layers)
