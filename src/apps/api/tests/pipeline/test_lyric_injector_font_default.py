from app.pipeline.lyric_injector import inject_lyric_overlays


def _recipe() -> dict:
    return {"slots": [{"position": 1, "target_duration_s": 5.0, "text_overlays": []}]}


def _cache() -> dict:
    return {
        "lines": [
            {
                "text": "Default font",
                "start_s": 1.0,
                "end_s": 2.0,
                "words": [{"text": "Default font", "start_s": 1.0, "end_s": 2.0}],
            }
        ]
    }


def test_line_style_default_font_family() -> None:
    out = inject_lyric_overlays(_recipe(), _cache(), 0.0, 5.0, {"enabled": True, "style": "line"})
    overlay = out["slots"][0]["text_overlays"][0]
    assert overlay["font_family"] == "Inter Tight"


def test_line_style_respects_font_family_override() -> None:
    out = inject_lyric_overlays(
        _recipe(),
        _cache(),
        0.0,
        5.0,
        {"enabled": True, "style": "line", "font_family": "Playfair Display"},
    )
    overlay = out["slots"][0]["text_overlays"][0]
    assert overlay["font_family"] == "Playfair Display"
