from __future__ import annotations

import hashlib
import shutil
import subprocess

import pytest

from app.pipeline.canvas import Canvas
from app.pipeline.look_presets import (
    look_preset_filter,
    normalize_look_preset,
    stadium_diffusion_filter,
)
from app.pipeline.reframe import _build_video_filter
from app.pipeline.single_pass import SinglePassInput, _per_clip_filter_chain


def test_neutral_preset_is_exact_bypass() -> None:
    assert look_preset_filter("none", width=1080, height=1920) is None
    assert normalize_look_preset(None) == "none"
    assert normalize_look_preset("") == "none"


def test_unknown_preset_is_rejected() -> None:
    with pytest.raises(ValueError, match="Unknown look preset"):
        normalize_look_preset("stadium_diffusion_plus")


def test_filter_order_is_after_crop_and_recipe_grade_before_graphics() -> None:
    filters = _build_video_filter(
        "9:16",
        None,
        color_hint="warm",
        look_preset="stadium_diffusion",
        has_grid=True,
    )
    look_index = next(i for i, value in enumerate(filters) if "all_seed=5144" in value)
    crop_index = max(i for i, value in enumerate(filters) if value.startswith("crop="))
    recipe_grade_index = next(i for i, value in enumerate(filters) if "colorbalance" in value)
    grid_index = next(i for i, value in enumerate(filters) if value.startswith("drawbox="))

    assert crop_index < recipe_grade_index < look_index < grid_index


@pytest.mark.parametrize("color_trc", ["arib-std-b67", "smpte2084"])
def test_hdr_normalization_stays_before_the_source_look(monkeypatch, color_trc: str) -> None:
    """The preset must consume SDR pixels, never the source HDR transfer."""
    monkeypatch.setattr("app.pipeline.reframe._zscale_available", lambda: True)

    filters = _build_video_filter(
        "9:16",
        None,
        color_trc=color_trc,
        look_preset="stadium_diffusion",
        canvas=Canvas(width=320, height=568),
    )

    tonemap_index = next(i for i, value in enumerate(filters) if "tonemap=" in value)
    look_index = next(i for i, value in enumerate(filters) if "all_seed=5144" in value)
    assert tonemap_index < look_index


def test_single_and_multi_pass_share_the_same_filter_constants() -> None:
    canvas = Canvas(width=320, height=568)
    multi = _build_video_filter(
        "9:16",
        None,
        look_preset="stadium_diffusion",
        look_label_prefix="look_0",
        canvas=canvas,
    )
    single = _per_clip_filter_chain(
        SinglePassInput(
            kind="clip",
            clip_path="/tmp/source.mp4",
            start_s=0,
            end_s=1,
            look_preset="stadium_diffusion",
        ),
        0,
        "v0",
        canvas=canvas,
    )
    shared = stadium_diffusion_filter(width=320, height=568, label_prefix="look_0")

    assert shared in multi
    assert shared in single
    assert single.endswith("fps=30,setpts=PTS-STARTPTS,settb=AVTB[v0]")


def test_approved_portrait_geometry_is_preserved_exactly() -> None:
    graph = stadium_diffusion_filter(width=1080, height=1920)

    for segment in (
        "scale=551:979",
        "scale=565:1004",
        "scale=582:1034",
        "scale=602:1070",
        "scale=584:1038",
        "crop=540:960:x=19:y=36",
    ):
        assert segment in graph


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg unavailable")
def test_preset_executes_in_the_production_simple_filter_chain(tmp_path) -> None:
    """Guard the real ``-vf`` integration, not only the isolated complex graph."""
    output = tmp_path / "simple-filter.mp4"
    vf = ",".join(
        _build_video_filter(
            "9:16",
            None,
            look_preset="stadium_diffusion",
            canvas=Canvas(width=320, height=568),
        )
    )
    result = subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=s=320x568:r=30:d=0.1",
            "-vf",
            vf,
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-pix_fmt",
            "yuv420p",
            str(output),
        ],
        capture_output=True,
        check=False,
        timeout=30,
    )

    assert result.returncode == 0, result.stderr.decode(errors="replace")
    assert output.stat().st_size > 0


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg unavailable")
def test_filter_renders_deterministically_to_valid_h264_aac(tmp_path) -> None:
    graph = stadium_diffusion_filter(width=320, height=568, label_prefix="render")

    def render(name: str) -> bytes:
        output = tmp_path / name
        result = subprocess.run(
            [
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-f",
                "lavfi",
                "-i",
                "testsrc2=s=320x568:r=30:d=0.25",
                "-f",
                "lavfi",
                "-i",
                "anullsrc=r=44100:cl=stereo",
                "-filter_complex",
                f"[0:v]{graph},format=yuv420p[out]",
                "-map",
                "[out]",
                "-map",
                "1:a:0",
                "-t",
                "0.25",
                "-c:v",
                "libx264",
                "-preset",
                "ultrafast",
                "-crf",
                "18",
                "-c:a",
                "aac",
                "-shortest",
                str(output),
            ],
            capture_output=True,
            check=False,
            timeout=30,
        )
        assert result.returncode == 0, result.stderr.decode(errors="replace")
        probe = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "stream=codec_name,width,height",
                "-of",
                "csv=p=0",
                str(output),
            ],
            capture_output=True,
            check=True,
            timeout=10,
        ).stdout.decode()
        assert "h264,320,568" in probe
        assert "aac" in probe
        return output.read_bytes()

    first = render("first.mp4")
    second = render("second.mp4")
    assert hashlib.sha256(first).digest() == hashlib.sha256(second).digest()
