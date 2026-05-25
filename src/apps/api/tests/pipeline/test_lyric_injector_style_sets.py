"""Style-set integration in the lyric injector: set supplies defaults,
lyrics_config overrides win, and the set's lyric role picks the injector."""

from __future__ import annotations

from app.pipeline.lyric_injector import _apply_style_set_defaults, inject_lyric_overlays

_LYRICS = {
    "lines": [
        {
            "text": "hello world",
            "start_s": 1.0,
            "end_s": 3.0,
            "words": [
                {"text": "hello", "start_s": 1.0, "end_s": 2.0},
                {"text": "world", "start_s": 2.0, "end_s": 3.0},
            ],
        }
    ]
}


def _recipe() -> dict:
    return {"slots": [{"position": "center", "target_duration_s": 10.0, "text_overlays": []}]}


def test_set_supplies_style_and_defaults() -> None:
    cfg = _apply_style_set_defaults(
        {"enabled": True, "style_set_id": "lyric_line_calm"}, "lyric_line_calm"
    )
    assert cfg["style"] == "line"  # set's single lyric role implies the injector
    assert cfg["font_family"] == "Playfair Display"
    assert cfg["text_color"] == "#FFFFFF"
    # Timing knobs flow from the set.
    assert cfg["fade_in_ms"] == 300


def test_lyrics_config_overrides_set() -> None:
    cfg = _apply_style_set_defaults(
        {
            "enabled": True,
            "style_set_id": "lyric_line_calm",
            "font_family": "Anton",
            "text_color": "#FF0000",
        },
        "lyric_line_calm",
    )
    # Explicit lyrics_config fields win over the set.
    assert cfg["font_family"] == "Anton"
    assert cfg["text_color"] == "#FF0000"


def test_karaoke_set_picks_karaoke_injector() -> None:
    recipe = inject_lyric_overlays(
        _recipe(),
        _LYRICS,
        best_start_s=0.0,
        best_end_s=10.0,
        lyrics_config={"enabled": True, "style_set_id": "lyric_karaoke_bold"},
    )
    overlays = recipe["slots"][0]["text_overlays"]
    assert overlays, "expected an injected lyric overlay"
    assert overlays[0]["effect"] == "karaoke-line"
    # lyric_karaoke_bold uses Fraunces after the editorial restyle (was Space Grotesk).
    assert overlays[0]["font_family"] == "Fraunces"


def test_no_set_id_unchanged_behavior() -> None:
    # Without a set, the explicit style still drives the injector.
    recipe = inject_lyric_overlays(
        _recipe(),
        _LYRICS,
        best_start_s=0.0,
        best_end_s=10.0,
        lyrics_config={"enabled": True, "style": "karaoke"},
    )
    overlays = recipe["slots"][0]["text_overlays"]
    assert overlays[0]["effect"] == "karaoke-line"
