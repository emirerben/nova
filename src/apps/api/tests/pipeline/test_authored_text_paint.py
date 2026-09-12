from PIL import Image

from app.pipeline.text_overlay import generate_text_overlay_png, render_overlays_at_time


def test_pillow_authored_background_and_outline_reach_export_and_preview(tmp_path):
    overlay = {
        "text": "Editor",
        "start_s": 0,
        "end_s": 2,
        "effect": "none",
        "text_color": "#FFFFFF",
        "stroke_color": "#FF0000",
        "stroke_width": 5,
        "shadow_enabled": False,
        "background_color": "#00FF00",
    }
    configs = generate_text_overlay_png([overlay], 2, str(tmp_path), 0)
    assert configs
    preview = tmp_path / "preview.png"
    render_overlays_at_time([overlay], 2, 1, str(preview))
    with Image.open(configs[0]["png_path"]) as image:
        with Image.open(preview) as sampled:
            assert sampled.tobytes() == image.tobytes()
        pixels = list(image.getdata())
        assert any(r > 150 and g < 50 and b < 50 and a > 150 for r, g, b, a in pixels)
        assert any(g > 150 and r < 50 and b < 50 and a > 150 for r, g, b, a in pixels)


def test_pillow_zero_shadow_opacity_does_not_leave_legacy_shadow(tmp_path):
    overlay = {"text": "Editor", "start_s": 0, "end_s": 2, "shadow_opacity": 0}
    transparent = generate_text_overlay_png([overlay], 2, str(tmp_path), 0)
    disabled = generate_text_overlay_png(
        [{**overlay, "shadow_enabled": False}], 2, str(tmp_path), 1
    )
    with Image.open(transparent[0]["png_path"]) as a, Image.open(disabled[0]["png_path"]) as b:
        assert a.tobytes() == b.tobytes()
