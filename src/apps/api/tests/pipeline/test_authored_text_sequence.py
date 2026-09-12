"""The classic renderer uses the same absolute-time samples for preview and export."""

import shutil
import subprocess

import pytest
from PIL import Image

from app.pipeline import text_overlay as renderer


def test_authored_animation_is_one_lossless_input_with_preview_frame_parity(tmp_path, monkeypatch):
    if not shutil.which("ffmpeg"):
        pytest.skip("ffmpeg is required")
    monkeypatch.setattr(renderer, "CANVAS_W", 160)
    monkeypatch.setattr(renderer, "CANVAS_H", 160)
    overlay = {
        "text": "Live",
        "start_s": 0.5,
        "end_s": 0.9,
        "text_size_px": 24,
        "font_family": "Inter",
        "shadow_enabled": False,
        "animation_phases": {"entrance": "fade", "exit": "fade", "loop": "float", "speed": 1},
    }
    (config,) = renderer.generate_text_overlay_png([overlay], 1, str(tmp_path), 0)
    assert config["animated"] is True
    assert renderer.overlay_png_input(config)[:2] == ["-itsoffset", "0.5"]
    assert "lt(t,0.900000)" in renderer.overlay_png_filter(config)
    preview = renderer._render_single_overlay_at_time(overlay, 1, 0.7, str(tmp_path), 99)
    encoded = subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-i",
            config["png_path"],
            "-vf",
            "select=eq(n\\,6)",
            "-frames:v",
            "1",
            "-f",
            "rawvideo",
            "-pix_fmt",
            "rgba",
            "pipe:1",
        ],
        capture_output=True,
        check=True,
        timeout=20,
    ).stdout
    with Image.open(preview) as expected:
        assert encoded == expected.convert("RGBA").tobytes()


def test_typewriter_prefix_keeps_full_layout_position(tmp_path, monkeypatch):
    monkeypatch.setattr(renderer, "CANVAS_W", 300)
    monkeypatch.setattr(renderer, "CANVAS_H", 200)
    first = tmp_path / "first.png"
    full = tmp_path / "full.png"
    renderer._draw_text_png(
        "Hello world",
        "center",
        str(first),
        text_size_px=24,
        shadow_enabled=False,
        reveal_progress=0.45,
    )
    renderer._draw_text_png(
        "Hello world", "center", str(full), text_size_px=24, shadow_enabled=False, reveal_progress=1
    )
    with Image.open(first) as partial, Image.open(full) as settled:
        assert partial.getbbox()[0] == settled.getbbox()[0]
        assert partial.getbbox()[2] < settled.getbbox()[2]


def test_authored_animation_composites_only_in_its_absolute_window(tmp_path, monkeypatch):
    if not shutil.which("ffmpeg"):
        pytest.skip("ffmpeg is required")
    monkeypatch.setattr(renderer, "CANVAS_W", 160)
    monkeypatch.setattr(renderer, "CANVAS_H", 160)
    overlay = {
        "text": "Live",
        "start_s": 0.5,
        "end_s": 0.9,
        "text_size_px": 24,
        "font_family": "Inter",
        "shadow_enabled": False,
        "animation_phases": {"entrance": "fade", "exit": "fade", "loop": "none", "speed": 1},
    }
    (config,) = renderer.generate_text_overlay_png([overlay], 1.2, str(tmp_path), 0)
    result = subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "color=black:s=160x160:r=30:d=1.2",
            *renderer.overlay_png_input(config),
            "-filter_complex",
            f"[0:v][1:v]{renderer.overlay_png_filter(config)}[out]",
            "-map",
            "[out]",
            "-an",
            "-f",
            "rawvideo",
            "-pix_fmt",
            "rgb24",
            "pipe:1",
        ],
        capture_output=True,
        check=True,
        timeout=20,
    ).stdout
    frame_bytes = 160 * 160 * 3
    assert len(result) == 36 * frame_bytes
    for frame in [0, 10, 14, 27, 30, 35]:
        assert max(result[frame * frame_bytes : (frame + 1) * frame_bytes]) == 0
    assert max(result[21 * frame_bytes : 22 * frame_bytes]) > 200


def test_span_typewriter_reveals_without_reflow(tmp_path, monkeypatch):
    monkeypatch.setattr(renderer, "CANVAS_W", 600)
    monkeypatch.setattr(renderer, "CANVAS_H", 300)
    overlay = {
        "font_family": "Inter",
        "shadow_enabled": False,
        "spans": [
            {"text": "Hello", "text_color": "#FF0000"},
            {"text": "world", "text_color": "#00FF00"},
        ],
    }
    frames = []
    for index, progress in enumerate([0, 0.5, 1]):
        path = tmp_path / f"span-{index}.png"
        renderer._render_static_overlay_layer(
            {**overlay, "_reveal_progress": progress}, "Hello world", "center", str(path)
        )
        with Image.open(path) as frame:
            frames.append(frame.copy())
    assert frames[0].getbbox() is None
    assert frames[1].getbbox()[0] == frames[2].getbbox()[0]
    assert frames[1].getbbox()[2] < frames[2].getbbox()[2]
    assert frames[1].getchannel("G").getbbox() is None
    assert frames[2].getchannel("G").getbbox() is not None
