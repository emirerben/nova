"""ASS render smoke tests for every active registry font."""

from __future__ import annotations

import json
import shutil
import subprocess

import pytest
from PIL import Image

from app.pipeline.text_overlay import FONTS_DIR


def _active_fonts() -> list[tuple[str, str]]:
    with open(f"{FONTS_DIR}/font-registry.json", encoding="utf-8") as f:
        registry = json.load(f)
    return [
        (family, entry["ass_name"])
        for family, entry in registry["fonts"].items()
        if entry.get("deprecated") is not True
    ]


@pytest.mark.parametrize(("family", "ass_name"), _active_fonts())
def test_active_font_renders_with_ass(family: str, ass_name: str, tmp_path) -> None:
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        pytest.skip("ffmpeg/ffprobe not available")

    ass_path = tmp_path / "font-smoke.ass"
    png_path = tmp_path / "font-smoke.png"
    ass_path.write_text(
        "\n".join(
            [
                "[Script Info]",
                "ScriptType: v4.00+",
                "PlayResX: 100",
                "PlayResY: 100",
                "",
                "[V4+ Styles]",
                (
                    "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, "
                    "OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, "
                    "ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, "
                    "Alignment, MarginL, MarginR, MarginV, Encoding"
                ),
                (
                    f"Style: Default,{ass_name},32,&H00FFFFFF,&H00FFFFFF,&H00000000,"
                    "&H00000000,0,0,0,0,100,100,0,0,1,0,0,5,5,5,5,1"
                ),
                "",
                "[Events]",
                "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
                "Dialogue: 0,0:00:00.00,0:00:01.00,Default,,0,0,0,,Hello",
            ]
        ),
        encoding="utf-8",
    )

    proc = subprocess.run(
        [
            "ffmpeg",
            "-f",
            "lavfi",
            "-i",
            "color=c=black:s=100x100:d=1:r=10",
            "-vf",
            f"subtitles={ass_path}:fontsdir={FONTS_DIR}",
            "-frames:v",
            "1",
            "-y",
            str(png_path),
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    assert proc.returncode == 0, f"{family} failed to render: {proc.stderr[-1000:]}"

    probe = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=width,height",
            "-of",
            "csv=p=0",
            str(png_path),
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    assert probe.returncode == 0
    assert probe.stdout.strip() == "100,100"

    with Image.open(png_path) as img:
        pixels = img.convert("RGB").getdata()
        assert any(pixel != (0, 0, 0) for pixel in pixels), f"{family} painted no text"
