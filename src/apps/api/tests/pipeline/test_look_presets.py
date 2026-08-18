from __future__ import annotations

import hashlib
import shutil
import subprocess
from unittest.mock import MagicMock

import pytest

from app.pipeline.canvas import Canvas
from app.pipeline.look_presets import (
    FADED_VIGNETTE_MASK_PATH,
    LookAdjustments,
    default_look_adjustments,
    faded_analog_filter,
    golden_hour_filter,
    look_preset_filter,
    normalize_look_adjustments,
    normalize_look_preset,
    olive_film_filter,
    smoky_split_tone_filter,
    stadium_diffusion_filter,
)
from app.pipeline.reframe import _build_video_filter, reframe_and_export
from app.pipeline.single_pass import SinglePassInput, _per_clip_filter_chain


def test_neutral_preset_is_exact_bypass() -> None:
    assert look_preset_filter("none", width=1080, height=1920) is None
    assert normalize_look_preset(None) == "none"
    assert normalize_look_preset("") == "none"


def test_unknown_preset_is_rejected() -> None:
    with pytest.raises(ValueError, match="Unknown look preset"):
        normalize_look_preset("stadium_diffusion_plus")


def test_fixed_edit_wide_look_graphs_match_approved_treatments() -> None:
    golden = golden_hour_filter(width=1080, height=1920)
    faded = faded_analog_filter(width=1080, height=1920)

    assert golden == (
        "eq=brightness=0.015:contrast=1.08:gamma=1.025,"
        "colorcorrect=rl=0.005:bl=-0.015:rh=0.055:bh=-0.055:saturation=1.22,"
        "convolution=0m='0 -0.05 0 -0.05 1.2 -0.05 0 -0.05 0'"
    )
    assert faded == (
        "eq=brightness=0.045:contrast=0.93:saturation=0.76:gamma=1.025,"
        "colorcorrect=rl=-0.010:bl=0.025:rh=0.040:bh=-0.035,"
        "noise=alls=3:allf=u:all_seed=9321,"
        "split=1[faded_base];"
        f"movie=filename='{FADED_VIGNETTE_MASK_PATH}',"
        "scale=1080:1920,format=yuv420p[faded_mask];"
        "[faded_base][faded_mask]"
        "blend=c0_mode=multiply:c1_mode=normal:c2_mode=normal"
    )
    assert default_look_adjustments("golden_hour") is None
    assert default_look_adjustments("faded_analog") is None


def test_reference_look_defaults_and_persisted_fallbacks() -> None:
    olive = default_look_adjustments("olive_film")
    smoky = default_look_adjustments("smoky_split_tone")

    assert olive == LookAdjustments(
        intensity=1.0,
        warmth=0.0,
        contrast=0.0,
        grain=0.18,
        vignette=0.22,
    )
    assert smoky == LookAdjustments(
        intensity=1.0,
        warmth=0.0,
        contrast=0.0,
        grain=0.36,
        vignette=0.55,
    )
    assert default_look_adjustments("stadium_diffusion") is None
    assert normalize_look_adjustments("olive_film", {"grain": 9}) == olive
    assert normalize_look_adjustments("olive_film", {}) == olive
    assert normalize_look_adjustments("smoky_split_tone", {"warmth": 0.5}) == smoky


def test_user_controls_change_grade_grain_and_vignette_independently() -> None:
    graph = olive_film_filter(
        width=1080,
        height=1920,
        adjustments={
            "intensity": 0.5,
            "warmth": 0.4,
            "contrast": -0.25,
            "grain": 0.0,
            "vignette": 0.75,
        },
    )

    assert "eq=contrast=0.9275:saturation=0.9450:gamma=1.0090" in graph
    assert "rm=0.0120" in graph
    assert "vignette=angle=0.2100" in graph
    assert "noise=" not in graph


def test_ass_fallback_preserves_custom_look_controls(monkeypatch, tmp_path) -> None:
    """The PNG-only retry must not silently fall back to authored defaults."""
    calls: list[list[str]] = []

    def fake_run(cmd, **_kwargs):
        calls.append(cmd)
        return MagicMock(returncode=1 if len(calls) == 1 else 0, stderr=b"ass failed")

    monkeypatch.setattr("app.pipeline.reframe.subprocess.run", fake_run)
    monkeypatch.setattr("app.pipeline.reframe.os.path.exists", lambda _path: True)
    monkeypatch.setattr("app.pipeline.reframe.os.path.getsize", lambda _path: 1024)

    controls = {
        "intensity": 0.44,
        "warmth": 0.3,
        "contrast": -0.2,
        "grain": 0.1,
        "vignette": 0.25,
    }
    reframe_and_export(
        input_path="/fake/in.mp4",
        start_s=0,
        end_s=1,
        aspect_ratio="9:16",
        ass_subtitle_path=None,
        output_path=str(tmp_path / "out.mp4"),
        ass_overlay_paths=["/fake/caption.ass"],
        color_trc="bt709",
        has_audio=True,
        look_preset="smoky_split_tone",
        look_adjustments=controls,
        canvas=Canvas(width=320, height=568),
    )

    assert len(calls) == 2
    expected = smoky_split_tone_filter(
        width=320,
        height=568,
        adjustments=controls,
    )
    assert expected in " ".join(calls[0])
    assert expected in " ".join(calls[1])


@pytest.mark.parametrize(
    ("preset", "builder", "seed"),
    [
        ("olive_film", olive_film_filter, "all_seed=6413"),
        ("smoky_split_tone", smoky_split_tone_filter, "all_seed=8721"),
    ],
)
def test_reference_looks_share_single_and_multi_pass_plumbing(preset, builder, seed) -> None:
    canvas = Canvas(width=320, height=568)
    controls = {
        "intensity": 0.8,
        "warmth": -0.2,
        "contrast": 0.1,
        "grain": 0.4,
        "vignette": 0.3,
    }
    multi = _build_video_filter(
        "9:16",
        None,
        look_preset=preset,
        look_adjustments=controls,
        look_label_prefix="look_0",
        canvas=canvas,
    )
    single = _per_clip_filter_chain(
        SinglePassInput(
            kind="clip",
            clip_path="/tmp/source.mp4",
            start_s=0,
            end_s=1,
            look_preset=preset,
            look_adjustments=controls,
        ),
        0,
        "v0",
        canvas=canvas,
    )
    shared = builder(
        width=320,
        height=568,
        adjustments=controls,
        label_prefix="look_0",
    )

    assert seed in shared
    assert shared in multi
    assert shared in single


@pytest.mark.parametrize(
    ("preset", "builder", "marker"),
    [
        ("golden_hour", golden_hour_filter, "saturation=1.22"),
        ("faded_analog", faded_analog_filter, "all_seed=9321"),
    ],
)
def test_fixed_edit_wide_looks_share_single_and_multi_pass_plumbing(
    preset, builder, marker
) -> None:
    canvas = Canvas(width=320, height=568)
    multi = _build_video_filter(
        "9:16",
        None,
        color_hint="warm",
        look_preset=preset,
        look_label_prefix="look_0",
        canvas=canvas,
    )
    single = _per_clip_filter_chain(
        SinglePassInput(
            kind="clip",
            clip_path="/tmp/source.mp4",
            start_s=0,
            end_s=1,
            color_hint="warm",
            look_preset=preset,
        ),
        0,
        "v0",
        canvas=canvas,
    )
    shared = builder(width=320, height=568, label_prefix="look_0")

    assert marker in shared
    assert shared in multi
    assert shared in single


@pytest.mark.parametrize(
    ("preset", "marker"),
    [
        ("stadium_diffusion", "all_seed=5144"),
        ("golden_hour", "saturation=1.22"),
        ("faded_analog", "all_seed=9321"),
    ],
)
def test_filter_order_is_after_crop_and_recipe_grade_before_graphics(
    preset: str, marker: str
) -> None:
    filters = _build_video_filter(
        "9:16",
        None,
        color_hint="warm",
        look_preset=preset,
        has_grid=True,
    )
    look_index = next(i for i, value in enumerate(filters) if marker in value)
    crop_index = max(i for i, value in enumerate(filters) if value.startswith("crop="))
    recipe_grade_index = next(i for i, value in enumerate(filters) if "colorbalance" in value)
    grid_index = next(i for i, value in enumerate(filters) if value.startswith("drawbox="))

    assert crop_index < recipe_grade_index < look_index < grid_index


@pytest.mark.parametrize("color_trc", ["arib-std-b67", "smpte2084"])
@pytest.mark.parametrize(
    ("preset", "marker"),
    [("stadium_diffusion", "all_seed=5144"), ("golden_hour", "saturation=1.22")],
)
def test_hdr_normalization_stays_before_the_source_look(
    monkeypatch, color_trc: str, preset: str, marker: str
) -> None:
    """The preset must consume SDR pixels, never the source HDR transfer."""
    monkeypatch.setattr("app.pipeline.reframe._zscale_available", lambda: True)

    filters = _build_video_filter(
        "9:16",
        None,
        color_trc=color_trc,
        look_preset=preset,
        canvas=Canvas(width=320, height=568),
    )

    tonemap_index = next(i for i, value in enumerate(filters) if "tonemap=" in value)
    look_index = next(i for i, value in enumerate(filters) if marker in value)
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


@pytest.mark.parametrize("preset", ["stadium_diffusion", "golden_hour", "faded_analog"])
@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg unavailable")
def test_preset_executes_in_the_production_simple_filter_chain(tmp_path, preset: str) -> None:
    """Guard the real ``-vf`` integration, not only the isolated complex graph."""
    output = tmp_path / f"simple-filter-{preset}.mp4"
    vf = ",".join(
        _build_video_filter(
            "9:16",
            None,
            look_preset=preset,
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


@pytest.mark.parametrize("preset", ["olive_film", "smoky_split_tone"])
@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg unavailable")
def test_reference_look_executes_with_custom_controls(tmp_path, preset: str) -> None:
    output = tmp_path / f"{preset}.mp4"
    vf = ",".join(
        _build_video_filter(
            "9:16",
            None,
            look_preset=preset,
            look_adjustments={
                "intensity": 0.73,
                "warmth": -0.2,
                "contrast": 0.15,
                "grain": 0.25,
                "vignette": 0.4,
            },
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


@pytest.mark.parametrize("preset", ["stadium_diffusion", "faded_analog"])
@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg unavailable")
def test_filter_renders_deterministically_to_valid_h264_aac(tmp_path, preset: str) -> None:
    graph = look_preset_filter(preset, width=320, height=568, label_prefix="render")
    assert graph is not None

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

    first = render(f"first-{preset}.mp4")
    second = render(f"second-{preset}.mp4")
    assert hashlib.sha256(first).digest() == hashlib.sha256(second).digest()
