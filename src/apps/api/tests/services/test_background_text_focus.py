"""Tests for the free text-column measure behind background focus (Qendresa item 18).

All fixtures are built in tmp_path with PIL's bundled font (CI is Linux) and ffmpeg.
Failure modes F3-F9 of docs/reviews/background-focus-failure-modes.md.
"""

from __future__ import annotations

import random
import shutil
import subprocess
from pathlib import Path

import numpy as np
import pytest
from PIL import Image, ImageDraw, ImageFont

from app.services import background_text_focus as btf
from app.services.background_text_focus import (
    MAX_AUTO_ZOOM,
    MAX_FOCUS_ZOOM,
    MIN_FOCUS_ZOOM,
    BackgroundFocus,
    TextColumn,
    focus_for_text_column,
    measure_text_column,
    should_measure,
)

needs_ffmpeg = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not on PATH")

DARK = ((13, 17, 23), (230, 237, 243))
LIGHT = ((255, 255, 255), (31, 35, 40))


# --------------------------------------------------------------------------- fixtures


def _words(rng: random.Random) -> str:
    n = rng.randint(2, 9)
    return "".join(rng.choice("abcdefghijklmnoprstuvwy") for _ in range(n))


def _draw_block(
    draw: ImageDraw.ImageDraw,
    *,
    x0: int,
    x1: int,
    y0: int,
    y1: int,
    pitch: int,
    font: ImageFont.ImageFont,
    fill: tuple[int, int, int],
    seed: int,
    ragged: bool = True,
) -> None:
    """Word-wrapped paragraphs of nonsense between x0 and x1, one line every ``pitch`` px."""
    rng = random.Random(seed)
    y = y0
    while y + pitch <= y1:
        line = ""
        limit = x1 - x0
        if ragged and rng.random() < 0.25:
            limit = int(limit * rng.uniform(0.3, 0.8))
        while True:
            cand = (line + " " + _words(rng)).strip()
            if draw.textlength(cand, font=font) > limit:
                break
            line = cand
        draw.text((x0, y), line, font=font, fill=fill)
        y += pitch
        if rng.random() < 0.12:  # paragraph gap
            y += pitch


def _page(
    w: int,
    h: int,
    *,
    theme=DARK,
    column=(0.18, 0.60),
    pitch: int = 26,
    font_size: int = 15,
    sidebar: tuple[float, float] | None = (0.80, 0.95),
    seed: int = 1,
    y_shift: int = 0,
) -> Image.Image:
    bg, fg = theme
    img = Image.new("RGB", (w, h), bg)
    d = ImageDraw.Draw(img)
    font = ImageFont.load_default(size=font_size)
    _draw_block(
        d,
        x0=int(column[0] * w),
        x1=int(column[1] * w),
        y0=int(0.14 * h) + y_shift,
        y1=int(0.95 * h),
        pitch=pitch,
        font=font,
        fill=fg,
        seed=seed,
    )
    if sidebar:
        # a sparse sidebar: few short lines, much wider spacing
        _draw_block(
            d,
            x0=int(sidebar[0] * w),
            x1=int(sidebar[1] * w),
            y0=int(0.2 * h),
            y1=int(0.5 * h),
            pitch=pitch * 3,
            font=font,
            fill=fg,
            seed=seed + 7,
        )
    return img


def _save(img: Image.Image, path: Path) -> str:
    img.save(path)
    return str(path)


def _encode(frames_dir: Path, fps: float, out: Path, extra: list[str] | None = None) -> str:
    cmd = [
        "ffmpeg", "-nostdin", "-v", "error", "-y",
        "-framerate", str(fps), "-i", str(frames_dir / "f%03d.png"),
        "-c:v", "libx264", "-preset", "ultrafast", "-crf", "12", "-pix_fmt", "yuv420p",
        *(extra or []), str(out),
    ]  # fmt: skip
    subprocess.run(cmd, check=True, capture_output=True, timeout=60)
    return str(out)


def _scroll_video(tmp_path: Path, *, seconds: int = 4, fps: int = 2) -> str:
    """1280x720 page scrolling upward, column 0.18-0.60."""
    w, h = 1280, 720
    tall = _page(w, h * 3, column=(0.18, 0.60), pitch=18, font_size=11, sidebar=None, seed=5)
    fdir = tmp_path / "scroll_frames"
    fdir.mkdir()
    n = seconds * fps
    for i in range(n):
        top = int(i * 30)
        tall.crop((0, top, w, top + h)).save(fdir / f"f{i + 1:03d}.png")
    return _encode(fdir, fps, tmp_path / "scroll.mp4")


# --------------------------------------------------------------------------- should_measure


def test_should_measure_rules():
    assert should_measure(1920, 1080)
    assert should_measure(3024, 1964)
    assert not should_measure(1080, 2340)  # portrait phone screenshot
    # a square picture is already 1.78x wider in aspect than 9:16 (margin 1.25): measured
    assert should_measure(1080, 1080)
    assert not should_measure(760 - 2, 1080)  # just under the margin
    assert not should_measure(None, 1080)
    assert not should_measure(0, 0)
    # 4:3 is 1.33 aspect vs canvas 0.5625 * 1.25 -> measure; just under margin is not
    assert should_measure(1440, 1080)
    assert not should_measure(int(1080 / 1920 * 1080 * 1.2), 1080)


# --------------------------------------------------------------------------- images, F3


def test_dark_github_page(tmp_path):
    img = _page(1920, 1080, theme=DARK)
    col = measure_text_column(_save(img, tmp_path / "a.png"), kind="image")
    assert col is not None
    assert col.left == pytest.approx(0.18, abs=0.03)
    assert col.right == pytest.approx(0.60, abs=0.04)
    assert col.line_pitch is not None
    assert col.line_pitch == pytest.approx(26 / 1080, rel=0.25)


def test_light_github_page(tmp_path):
    img = _page(1920, 1080, theme=LIGHT)
    col = measure_text_column(_save(img, tmp_path / "a.png"), kind="image")
    assert col is not None
    assert col.left == pytest.approx(0.18, abs=0.03)
    assert col.right == pytest.approx(0.60, abs=0.04)
    assert col.line_pitch == pytest.approx(26 / 1080, rel=0.25)


def test_readme_left_with_sparse_terminal_right_picks_readme(tmp_path):
    img = _page(1920, 1080, theme=DARK, column=(0.04, 0.47), sidebar=None, seed=3)
    d = ImageDraw.Draw(img)
    # a terminal: short lines (prompt + one word), lots of black
    font = ImageFont.load_default(size=15)
    for i in range(9):
        d.text(
            (int(0.58 * 1920), 200 + i * 60),
            "$ ls " + "x" * (i % 4 + 1),
            font=font,
            fill=(0, 255, 0),
        )
    col = measure_text_column(_save(img, tmp_path / "a.png"), kind="image")
    assert col is not None
    assert col.left == pytest.approx(0.04, abs=0.03)
    assert col.right == pytest.approx(0.47, abs=0.04)


def test_retina_sized_image(tmp_path):
    img = _page(3024, 1964, column=(0.09, 0.67), pitch=52, font_size=29, sidebar=None, seed=9)
    col = measure_text_column(_save(img, tmp_path / "a.png"), kind="image")
    assert col is not None
    assert col.left == pytest.approx(0.09, abs=0.03)
    assert col.right == pytest.approx(0.67, abs=0.04)


def test_photo_like_image_has_no_text_column(tmp_path):
    rng = np.random.default_rng(42)
    small = rng.integers(0, 255, (36, 64, 3), dtype=np.uint8)
    img = Image.fromarray(small).resize((1920, 1080), Image.BICUBIC)
    d = ImageDraw.Draw(img)
    for _ in range(40):
        x, y = int(rng.integers(0, 1800)), int(rng.integers(0, 1000))
        d.ellipse((x, y, x + int(rng.integers(20, 200)), y + int(rng.integers(20, 200))),
                  fill=tuple(int(v) for v in rng.integers(0, 255, 3)))  # fmt: skip
    assert measure_text_column(_save(img, tmp_path / "p.png"), kind="image") is None


def test_noisy_texture_image_has_no_text_column(tmp_path):
    rng = np.random.default_rng(7)
    arr = rng.integers(0, 255, (1080, 1920), dtype=np.uint8)
    img = Image.fromarray(arr).convert("RGB")
    assert measure_text_column(_save(img, tmp_path / "n.png"), kind="image") is None


def test_blank_image_has_no_text_column(tmp_path):
    img = Image.new("RGB", (1920, 1080), (20, 20, 20))
    assert measure_text_column(_save(img, tmp_path / "b.png"), kind="image") is None


def test_portrait_screenshot_is_not_even_read(tmp_path):
    # a path that does not exist would be a read error; portrait must short-circuit first
    missing = str(tmp_path / "does-not-exist.png")
    assert measure_text_column(missing, kind="image", width=1080, height=2340) is None
    img = _page(1080, 2340, column=(0.1, 0.9), sidebar=None)
    p = _save(img, tmp_path / "portrait.png")
    assert measure_text_column(p, kind="image") is None


def test_missing_and_corrupt_files_return_none(tmp_path):
    assert measure_text_column(str(tmp_path / "nope.png"), kind="image") is None
    bad = tmp_path / "bad.png"
    bad.write_bytes(b"not an image at all" * 50)
    assert measure_text_column(str(bad), kind="image") is None
    assert (
        measure_text_column(str(tmp_path / "nope.mp4"), kind="video", width=1920, height=1080)
        is None
    )
    badv = tmp_path / "bad.mp4"
    badv.write_bytes(b"\x00\x01garbage" * 500)
    assert measure_text_column(str(badv), kind="video", width=1920, height=1080) is None


# --------------------------------------------------------------------------- video, F6/F7/F9


@needs_ffmpeg
def test_scrolling_video_matches_still_column(tmp_path):
    vid = _scroll_video(tmp_path)
    col = measure_text_column(vid, kind="video", width=1280, height=720)
    assert col is not None
    assert col.left == pytest.approx(0.18, abs=0.04)
    assert col.right == pytest.approx(0.60, abs=0.05)
    still = _page(1280, 720, column=(0.18, 0.60), pitch=18, font_size=11, sidebar=None, seed=5)
    sc = measure_text_column(_save(still, tmp_path / "still.png"), kind="image")
    assert sc is not None
    assert col.left == pytest.approx(sc.left, abs=0.04)
    assert col.right == pytest.approx(sc.right, abs=0.05)


@needs_ffmpeg
def test_rotated_video_is_measured_upright(tmp_path):
    upright = _page(1280, 720, column=(0.18, 0.60), pitch=18, font_size=11, sidebar=None, seed=5)
    stored = upright.rotate(-90, expand=True)  # clockwise; display_rotation 90 turns it back
    fdir = tmp_path / "rot_frames"
    fdir.mkdir()
    for i in range(6):
        stored.save(fdir / f"f{i + 1:03d}.png")
    plain = tmp_path / "rot_plain.mp4"
    _encode(fdir, 2, plain)
    tagged = tmp_path / "rot_tagged.mp4"
    r = subprocess.run(
        ["ffmpeg", "-nostdin", "-v", "error", "-y", "-display_rotation", "90",
         "-i", str(plain), "-c", "copy", str(tagged)],
        capture_output=True,
    )  # fmt: skip
    if r.returncode != 0:
        pytest.skip("this ffmpeg cannot write a display-rotation tag")
    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-show_streams", str(tagged)], capture_output=True, text=True
    )
    if "rotation" not in probe.stdout.lower():
        pytest.skip("ffprobe shows no rotation side data for the tagged file")
    col = measure_text_column(str(tagged), kind="video", width=1280, height=720)
    assert col is not None
    assert col.left == pytest.approx(0.18, abs=0.04)
    assert col.right == pytest.approx(0.60, abs=0.05)


@needs_ffmpeg
def test_blank_video_has_no_text_column(tmp_path):
    out = tmp_path / "blank.mp4"
    subprocess.run(
        ["ffmpeg", "-nostdin", "-v", "error", "-y", "-f", "lavfi",
         "-i", "color=c=0x202020:s=1280x720:r=5:d=3", "-pix_fmt", "yuv420p", str(out)],
        check=True, capture_output=True, timeout=60,
    )  # fmt: skip
    assert measure_text_column(str(out), kind="video", width=1280, height=720) is None


@needs_ffmpeg
def test_video_path_never_decodes_more_than_six_frames(tmp_path, monkeypatch):
    seen: list[list[str]] = []
    real_run = subprocess.run

    def spy(args, *a, **kw):
        seen.append(list(args))
        return real_run(args, *a, **kw)

    monkeypatch.setattr(btf.subprocess, "run", spy)
    # a 90 s synthetic video (tiny, so it encodes fast)
    out = tmp_path / "long.mp4"
    real_run(
        ["ffmpeg", "-nostdin", "-v", "error", "-y", "-f", "lavfi",
         "-i", "testsrc=s=160x90:r=2:d=90", "-pix_fmt", "yuv420p", str(out)],
        check=True, capture_output=True, timeout=60,
    )  # fmt: skip
    measure_text_column(str(out), kind="video", width=1920, height=1080)
    ff = [a for a in seen if a and a[0] == "ffmpeg"]
    assert len(ff) == 1
    args = ff[0]
    assert "-frames:v" in args
    assert int(args[args.index("-frames:v") + 1]) <= 6
    assert "-t" in args and float(args[args.index("-t") + 1]) <= 60
    # also without a known size, nothing is decoded for a portrait hint
    seen.clear()
    assert measure_text_column(str(out), kind="video", width=1080, height=1920) is None
    assert not seen


def test_ffmpeg_missing_or_timeout_returns_none(tmp_path, monkeypatch):
    f = tmp_path / "x.mp4"
    f.write_bytes(b"x")

    def boom(*a, **k):
        raise FileNotFoundError("ffmpeg")

    monkeypatch.setattr(btf.subprocess, "run", boom)
    assert measure_text_column(str(f), kind="video", width=1920, height=1080) is None

    def slow(*a, **k):
        raise subprocess.TimeoutExpired("ffmpeg", 1)

    monkeypatch.setattr(btf.subprocess, "run", slow)
    assert measure_text_column(str(f), kind="video", width=1920, height=1080) is None


# --------------------------------------------------------------------------- focus math


def test_focus_github_1x():
    col = TextColumn(left=0.18, right=0.60, line_pitch=26 / 1080)
    f = focus_for_text_column(col, source_w=1920, source_h=1080)
    assert f is not None
    assert f.zoom == pytest.approx(0.865, abs=0.01)
    assert f.x == pytest.approx(0.24, abs=0.02)
    assert f.y == 0.5


def test_focus_retina():
    col = TextColumn(left=0.09, right=0.67, line_pitch=52 / 1964)
    f = focus_for_text_column(col, source_w=3024, source_h=1964)
    assert f is not None
    assert 0.55 <= f.zoom <= 0.85
    assert 0.10 <= f.x <= 0.17


def test_focus_none_when_picture_fits():
    col = TextColumn(left=0.1, right=0.9, line_pitch=0.02)
    assert focus_for_text_column(col, source_w=1080, source_h=2340) is None
    assert focus_for_text_column(col, source_w=1080, source_h=1920) is None
    assert focus_for_text_column(col, source_w=0, source_h=1080) is None


def test_narrow_column_never_zooms_past_cap():
    col = TextColumn(left=0.45, right=0.50, line_pitch=0.004)
    f = focus_for_text_column(col, source_w=1920, source_h=1080)
    assert f is not None
    assert f.zoom <= MAX_AUTO_ZOOM


def test_no_pitch_shows_at_most_half_the_width():
    col = TextColumn(left=0.1, right=0.9, line_pitch=None)
    f = focus_for_text_column(col, source_w=1920, source_h=1080)
    assert f is not None
    cover = max(1080 / 1920, 1920 / 1080)
    contain = min(1080 / 1920, 1920 / 1080)
    v_cover = 1080 / (1920 * cover)
    shown = v_cover / max(f.zoom, contain / cover)
    assert shown <= 0.5 + 1e-3


@pytest.mark.parametrize(
    "left,right", [(0.0, 1.0), (0.02, 0.2), (0.7, 0.99), (0.3, 0.35), (0.4, 0.9)]
)
@pytest.mark.parametrize("pitch", [None, 0.005, 0.024, 0.08])
@pytest.mark.parametrize("size", [(1920, 1080), (3024, 1964), (2560, 1080), (4000, 1000)])
def test_every_focus_is_inside_wire_bounds(left, right, pitch, size):
    f = focus_for_text_column(
        TextColumn(left=left, right=right, line_pitch=pitch), source_w=size[0], source_h=size[1]
    )
    if f is None:
        return
    assert 0.0 <= f.x <= 1.0 and 0.0 <= f.y <= 1.0
    assert MIN_FOCUS_ZOOM <= f.zoom <= MAX_FOCUS_ZOOM


def test_models_are_strict():
    with pytest.raises(ValueError):
        TextColumn(left=0.6, right=0.5, line_pitch=None)
    with pytest.raises(ValueError):
        TextColumn(left=-0.1, right=0.5, line_pitch=None)
    with pytest.raises(ValueError):
        TextColumn(left=0.1, right=float("nan"), line_pitch=None)
    with pytest.raises(ValueError):
        BackgroundFocus(x=0.5, y=0.5, zoom=9)
    with pytest.raises(ValueError):
        BackgroundFocus(x=0.5, y=0.5, zoom=1, extra=1)  # type: ignore[call-arg]
    assert TextColumn(left=0.1, right=0.5, line_pitch=None).version == btf.TEXT_COLUMN_VERSION
