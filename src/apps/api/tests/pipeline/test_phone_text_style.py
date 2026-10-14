"""Regression cases written before the KRI-648 styling implementation.

These exercise real font coverage/measurement and the phone compiler boundary.
"""

from copy import deepcopy

import pytest

from app.pipeline.phone_text_style import resolve_phone_text_style
from app.schemas.phone_text_style import TextStyleRule


def title(text="A slow morning in Lisbon"):
    return dict(
        id="guided-title",
        text=text,
        role="generative_intro",
        start_s=0,
        end_s=3,
        position="custom",
        x_frac=0.5,
        y_frac=0.16,
        font_family="Fraunces",
        size_px=104,
        color="#FFFFFF",
        stroke_width=3,
        shadow_enabled=True,
        alignment="center",
        max_width_frac=0.8,
        effect="static",
    )


def test_default_resolves_without_rewriting_input_or_copy():
    rows = [title()]
    before = deepcopy(rows)
    result = resolve_phone_text_style(rows)
    assert rows == before
    assert result[0]["text"] == before[0]["text"]
    assert result[0]["start_s"] == 0 and result[0]["end_s"] == 3
    assert result[0]["stroke_width"] == 0
    assert result[0]["shadow_enabled"] is False
    assert result[0]["alignment"] == "left"
    assert result[0]["max_width_frac"] <= 0.68


def test_current_video_overrides_properties_independently():
    result = resolve_phone_text_style(
        [title()],
        preferences={
            "all_text": {"font_family": "Inter", "shadow_enabled": False, "stroke_width": 0},
            "title": {"color": "#FFFF00"},
        },
        requested={"title": {"font_family": "Pacifico", "shadow_enabled": True}},
    )
    assert result[0]["font_family"] == "Pacifico"
    assert result[0]["shadow_enabled"] is True
    assert result[0]["stroke_width"] == 0
    assert result[0]["color"] == "#FFFF00"


def test_limited_font_fallback_preserves_turkish_and_exclusions():
    text = "İstanbul'da güzel bir gün"
    result = resolve_phone_text_style(
        [title(text)],
        preferences={"title": {"favorite_fonts": ["Big Curls"], "excluded_fonts": ["Inter"]}},
    )
    assert result[0]["text"] == text
    assert result[0]["font_family"] not in {"Big Curls", "Inter"}


def test_required_font_with_missing_glyphs_requests_clarification():
    with pytest.raises(ValueError, match="draw"):
        resolve_phone_text_style(
            [title("İstanbul")], requested={"title": {"font_family": "Big Curls"}}
        )


def test_off_effects_survive_styled_input_and_role_overrides():
    rows = [title(), {**title("Pinned corner"), "id": "guided-pinned-0"}]
    result = resolve_phone_text_style(
        rows, preferences={"title": {"shadow_enabled": True}, "all_text": {"stroke_width": 0}}
    )
    assert result[0]["shadow_enabled"] is True
    assert result[1]["shadow_enabled"] is False
    assert all(x["stroke_width"] == 0 for x in result)


@pytest.mark.parametrize(
    "text",
    [
        "Lisbon",
        "A slow morning in Lisbon",
        "The best part of Lisbon was getting lost in its beautiful streets",
    ],
)
def test_whole_title_fits_and_clears_watermark(text):
    from app.pipeline.phone_text_style import measure_box

    rows = resolve_phone_text_style([title(text)])
    left, top, right, bottom = measure_box(rows[0])
    assert left >= 0.055 and right <= 0.83
    assert top >= 0.10
    assert bottom < 0.73


def test_preference_rejects_unknown_fonts_and_overlapping_font_sets():
    with pytest.raises(ValueError):
        TextStyleRule(font_family="Unknown font")
    with pytest.raises(ValueError):
        TextStyleRule(favorite_fonts=["Inter"], excluded_fonts=["Inter"])
