from app.pipeline import lyric_injector
from app.pipeline.lyric_injector import inject_lyric_overlays


def test_line_defaults_locked() -> None:
    assert lyric_injector._LINE_POST_DWELL_S == 1.0
    assert lyric_injector._LINE_HOLD_TO_NEXT_THRESHOLD_MS == 500
    assert lyric_injector._MIN_LINE_VISIBLE_S == 0.20


def test_line_style_default_text_size_and_position() -> None:
    """Locks the fit-to-screen defaults added after job 09a2afa1 shipped lyrics
    that overflowed the 1080px frame. The libass `lyric-line` path uses \\q2
    (no auto-wrap), so the injector MUST hand the renderer a narrow enough
    font + a position that clears the social-UI bottom safe area for any
    wrapped multi-line block. Both values are setdefaults — caller overrides
    still win — but the defaults themselves are the safety net."""
    recipe = {
        "slots": [{"position": 1, "target_duration_s": 6.0, "text_overlays": []}],
    }
    cache = {
        "lines": [
            {
                "text": "I hope I make it outta here (let's go! Yeah)",
                "start_s": 0.5,
                "end_s": 3.5,
                "words": [],
            }
        ]
    }
    out = inject_lyric_overlays(recipe, cache, 0.0, 6.0, {"enabled": True, "style": "line"})
    overlays = out["slots"][0]["text_overlays"]
    assert overlays, "line style must inject at least one overlay"
    ov = overlays[0]
    assert ov["effect"] == "lyric-line"
    assert ov["text_size_px"] == 56
    assert ov["position_y_frac"] == 0.80


def test_line_style_respects_caller_overrides() -> None:
    """The setdefault precedence must keep caller overrides on top — only the
    fallback path uses 56/0.80. Without this, a future operator who wants
    bigger or differently-placed lyrics couldn't override them."""
    recipe = {
        "slots": [{"position": 1, "target_duration_s": 6.0, "text_overlays": []}],
    }
    cache = {
        "lines": [
            {"text": "Tuned", "start_s": 0.5, "end_s": 1.5, "words": []},
        ]
    }
    out = inject_lyric_overlays(
        recipe,
        cache,
        0.0,
        6.0,
        {
            "enabled": True,
            "style": "line",
            "text_size_px": 80,
            "position_y_frac": 0.70,
        },
    )
    ov = out["slots"][0]["text_overlays"][0]
    assert ov["text_size_px"] == 80
    assert ov["position_y_frac"] == 0.70
