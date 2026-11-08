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
    # KRI-785: Turkish letters Big Curls lacks use its glyph-safe partner (the
    # face the cloud and iOS swap to); only text NO bundled face draws declines.
    rows = resolve_phone_text_style(
        [title("İstanbul")], requested={"title": {"font_family": "Big Curls"}}
    )
    assert rows[0]["font_family"] == "Great Vibes"
    with pytest.raises(ValueError, match="draw"):
        resolve_phone_text_style([title("東京")], requested={"title": {"font_family": "Big Curls"}})


def test_off_effects_survive_styled_input_and_role_overrides():
    rows = [
        {**title(), "shadow_enabled": False},
        {**title("Pinned corner"), "id": "guided-pinned-0", "shadow_enabled": False},
    ]
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


def _real_lyric_row():
    from app.pipeline.lyric_injector import build_lyric_seed_elements

    cache = {
        "source": "lrclib_synced+whisper",
        "lines": [
            {
                "text": "Hello world",
                "start_s": 0.5,
                "end_s": 1.5,
                "words": [
                    {"text": "Hello", "start_s": 0.5, "end_s": 1.0},
                    {"text": "world", "start_s": 1.0, "end_s": 1.5},
                ],
            },
            {"text": "Goodbye now", "start_s": 2.0, "end_s": 3.0, "words": []},
            {"text": "One more", "start_s": 3.5, "end_s": 4.5, "words": []},
            {"text": "Last line", "start_s": 5.0, "end_s": 6.0, "words": []},
        ],
    }
    rows = build_lyric_seed_elements(cache, 0.0, 10.0, {"enabled": True})
    return next(row for row in rows if row["id"] == "lyr-L3")


def test_template_lyric_row_takes_the_lyric_preference_not_the_title_one():
    """Runs the real style pass: the lyric preference colours the row, the title one does not."""
    row = _real_lyric_row()
    assert row["role"] == "lyric_line" and row["source_params"]["source"] == "lyric_seed"
    (resolved,) = resolve_phone_text_style(
        [row], preferences={"lyric": {"color": "#FFFF00"}, "title": {"color": "#FF0000"}}
    )
    assert resolved["color"] == "#FFFF00"
    assert resolved["source_params"]["phone_text_style_version"] == 1


def test_template_lyric_row_resolves_with_no_saved_preference():
    """The style pass measures a lyric row (the burn path drops lyric lines by default)."""
    (resolved,) = resolve_phone_text_style([_real_lyric_row()])
    assert resolved["size_px"] and 0 <= resolved["y_frac"] <= 1


@pytest.mark.parametrize(
    "overrides",
    [
        {"id": "x1"},
        {"id": "x1", "role": "generative_intro", "source_params": {"source": "lyric_seed"}},
        {"id": "x1", "role": "lyric_line", "source_params": {}},
        {"id": "lyr-L3", "role": "generative_intro", "source_params": {}},
    ],
)
def test_each_lyric_signal_alone_classifies_as_lyric(overrides):
    from app.pipeline.phone_text_style import text_role

    base = {"id": "x1", "role": "generative_intro", "source_params": {}}
    row = {**base, **overrides}
    expected = "title" if overrides == {"id": "x1"} else "lyric"
    assert text_role(row) == expected


def test_a_plan_authored_shadow_does_not_beat_the_account_default():
    result = resolve_phone_text_style([title()])
    assert result[0]["shadow_enabled"] is False


def test_an_explicit_ask_for_a_shadow_wins_over_the_default():
    by_preference = resolve_phone_text_style(
        [title()], preferences={"title": {"shadow_enabled": True}}
    )
    by_request = resolve_phone_text_style(
        [{**title(), "shadow_enabled": False}], requested={"all_text": {"shadow_enabled": True}}
    )
    assert by_preference[0]["shadow_enabled"] is True
    assert by_request[0]["shadow_enabled"] is True


def test_a_shadow_the_creator_added_in_the_editor_survives():
    row = title()
    row["source_params"] = {"creator_style_fields": ["shadow_enabled"]}
    assert resolve_phone_text_style([row])[0]["shadow_enabled"] is True


def test_explicit_shadow_off_still_beats_the_rows_own_shadow():
    off_by_preference = resolve_phone_text_style(
        [title()], preferences={"title": {"shadow_enabled": False}}
    )
    off_by_request = resolve_phone_text_style(
        [title()], requested={"all_text": {"shadow_enabled": False}}
    )
    assert off_by_preference[0]["shadow_enabled"] is False
    assert off_by_request[0]["shadow_enabled"] is False


@pytest.mark.parametrize("own", [None, False])
def test_rows_without_a_shadow_stay_without_one(own):
    row = {**title(), "shadow_enabled": own}
    assert resolve_phone_text_style([row])[0]["shadow_enabled"] is False
    missing = {k: v for k, v in title().items() if k != "shadow_enabled"}
    assert resolve_phone_text_style([missing])[0]["shadow_enabled"] is False


# No role keeps a shadow its writer authored: the account default says no shadow, and only the
# creator's own ask (chat request, saved preference) or an editor edit turns one on.
AUTHORED_SHADOW_ROWS = {
    "title": {},
    "pinned": {"id": "guided-pinned-0", "x_frac": 0.92, "y_frac": 0.12, "alignment": "right"},
    "pin_prefix": {"id": "pin-1", "x_frac": 0.92, "y_frac": 0.12, "alignment": "right"},
    "guided_label": {"id": "guided-label-1"},
    "speech_montage_label": {"id": "clip-label-0"},
    "caption": {"id": "caption-0"},
    "song_caption": {"id": "song-caption-0"},
}


@pytest.mark.parametrize("name", sorted(AUTHORED_SHADOW_ROWS))
def test_no_row_keeps_an_authored_shadow(name):
    row = {**title("Day 1"), "size_px": 60, **AUTHORED_SHADOW_ROWS[name]}
    assert row["shadow_enabled"] is True
    (resolved,) = resolve_phone_text_style([row])
    assert resolved["shadow_enabled"] is False
    assert resolved["size_px"] == 60
