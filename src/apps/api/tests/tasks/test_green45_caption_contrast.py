"""Green 5 (2026-11-04): captions over a green-screen background are chosen against the room.

The chip render (job 58c39348, no caption ask) drew its captions grey ``#767676`` with no outline,
left-aligned at y 0.655, over the bright terraces of the fill photo: hard to read. The automatic
fill (`phone_text_background.automatic_fill`) was picked from the speaker take's frames (white
wall + navy shirt), because `phone_text_styling._ensure_generation_environment` samples only the
source proxies, as if the speaker filled the frame. With a speaker background the pixels behind a
caption are the background photo or video, with the small speaker in its corner frame.

This runs the real writer (`_run_generative_job`) on the Green 3 harness (fill + timed windows +
``small_bottom_right``), with the real environment sampler. Only I/O is faked: downloads return
small media made from luminance grids of the real Green 5 take and backgrounds
(`green45_caption_contrast.json`), and no faces are sampled. Every caption is then judged against
the composite the phone draws (background under the caption's time, speaker take in its frame):
the WCAG contrast of the text against the darkest and brightest tenth of the pixels behind it
must reach 4.5:1, or the caption carries an outline that does.

Ways the fix could fail (written before the fix):
1. The colour is still picked from the take alone (prod: #767676 over bright terraces).
2. Background frames are sampled, but at the wrong time (a window's photo judged for another
   window's captions) or without the speaker frame that also sits behind the caption.
3. No plain colour reaches 4.5:1 against a busy background and the caption ships without the
   outline that would make it readable.
4. An outline is added in a colour that does not contrast with the text.
5. A Talking edit WITHOUT a background changes at all (colour or outline): byte-identical.
6. A caption colour the creator asked for is overridden.
7. The switch off is byte-identical to before.
"""

from __future__ import annotations

import base64
import json
import subprocess
from pathlib import Path

from app.config import settings
from app.services import phone_text_styling as styling
from app.tasks import generative_build as gb
from tests.tasks.test_green3_caption_look_worker import R1, _green3_job
from tests.tasks.test_kri841_talking_ending_clip import (
    BITE,
    COTOPAXI,
    OBLEA,
    _offline,  # noqa: F401 - autouse fixture the harness needs
    _recipe,
)

FIXTURE = json.loads((Path(__file__).parent / "green45_caption_contrast.json").read_text())
W, H = FIXTURE["width"], FIXTURE["height"]
REAL_ENVIRONMENT = styling._ensure_generation_environment
# The harness pool, played by the real Green 5 media.
MEDIA = {COTOPAXI: "IMG_7120", OBLEA: "IMG_7122", BITE: "IMG_7125"}
REQUEST = "Make it like a green screen."


def _grid(name: str) -> bytes:
    return base64.b64decode(FIXTURE["grids"][name])


def _write_media(directory: Path) -> dict[str, Path]:
    """One PGM still per grid, and a 30 s video of each grid for the video sources."""
    files: dict[str, Path] = {}
    for name in FIXTURE["grids"]:
        still = directory / f"{name}.pgm"
        still.write_bytes(f"P5 {W} {H} 255\n".encode() + _grid(name))
        files[name] = still
        movie = directory / f"{name}.mp4"
        subprocess.run(
            [
                "ffmpeg",
                "-v",
                "error",
                "-y",
                "-loop",
                "1",
                "-i",
                str(still),
                "-t",
                "30",
                "-r",
                "2",
                "-vf",
                "scale=108:192,format=yuv420p",
                str(movie),
            ],
            check=True,
        )
        files[f"{name}.mp4"] = movie
    return files


def _real_environment(monkeypatch, tmp_path) -> None:
    files = _write_media(tmp_path)

    def download(remote, local, *args, **kwargs):  # noqa: ARG001
        remote = str(remote)
        if "analysis-proxy-c0" in remote:
            source = files["take.mp4"]
        else:
            media = next((name for media_id, name in MEDIA.items() if media_id in remote), None)
            if media is None:
                raise FileNotFoundError(remote)
            source = files[f"{media}.mp4" if media == "IMG_7125" else media]
        Path(local).write_bytes(source.read_bytes())
        return local

    import app.pipeline.render_geometry as render_geometry
    import app.storage as storage

    monkeypatch.setattr(storage, "download_to_file", download)
    monkeypatch.setattr(
        render_geometry,
        "sample_face_regions",
        lambda path, points, **k: ([], {"decoded": len(points)}),
    )
    monkeypatch.setattr(styling, "_ensure_generation_environment", REAL_ENVIRONMENT)


def _job(monkeypatch, tmp_path, *, requirements=(R1,), background=True):
    job = _green3_job(monkeypatch, list(requirements), request=REQUEST)
    if not background:
        job.all_candidates["creator_strategy"].pop("background_media", None)
    _real_environment(monkeypatch, tmp_path)
    return job


# ------------------------------------------------------------------ the judge


def _lum(value: float) -> float:
    v = value / 255
    return v / 12.92 if v <= 0.04045 else ((v + 0.055) / 1.055) ** 2.4


def _hex_lum(color: str) -> float:
    color = color.lstrip("#")
    r, g, b = (int(color[i : i + 2], 16) for i in (0, 2, 4))
    return 0.2126 * _lum(r) + 0.7152 * _lum(g) + 0.0722 * _lum(b)


def _ratio(a: float, b: float) -> float:
    return (max(a, b) + 0.05) / (min(a, b) + 0.05)


def _composite(recipe, at: float) -> list[int]:
    """The canvas the phone draws at ``at`` (grid resolution): the background photo or video under
    that time, the speaker take in its frame (unmatted: the conservative case)."""
    layer = recipe.speaker_background
    asset = next((w.asset_id for w in layer.windows or [] if w.start <= at < w.end), layer.asset_id)
    media_id = asset.removeprefix("visual-")
    canvas = list(_grid(MEDIA[media_id]))
    take = _grid("take")
    frame = layer.speaker_frame
    left = frame.x_fraction - frame.width_fraction / 2
    top = frame.y_fraction - frame.width_fraction / 2
    for y in range(H):
        for x in range(W):
            fx = (x / W - left) / frame.width_fraction
            fy = (y / H - top) / frame.width_fraction
            if 0 <= fx < 1 and 0 <= fy < 1:
                canvas[y * W + x] = take[int(fy * H) * W + int(fx * W)]
    return canvas


def _caption_judgements(job) -> list[tuple[float, float]]:
    """(time, contrast the caption reaches) for every caption the recipe draws."""
    recipe = _recipe(job)
    variant = job.assembly_plan["variants"][0]
    style = variant["caption_editor_style"]
    text = _hex_lum(variant["caption_text_color"])
    outline = int(variant.get("caption_stroke_width") or 0)
    stroke = style.get("stroke_color") or "#000000"
    half = 2.4 * variant["caption_size_px"] / 1920 / 2
    out = []
    for layer in recipe.text_layers:
        if not layer.id.startswith("caption-"):
            continue
        x0 = layer.anchor_x / 1080
        x1 = min(1.0, x0 + style["max_width_frac"])
        yc = layer.anchor_y / 1920
        canvas = _composite(recipe, layer.start)
        pixels = sorted(
            canvas[y * W + x]
            for y in range(max(0, int((yc - half) * H)), min(H, int((yc + half) * H) + 1))
            for x in range(int(x0 * W), min(W, int(x1 * W) + 1))
        )
        dark, bright = _lum(pixels[len(pixels) // 10]), _lum(pixels[9 * len(pixels) // 10])
        plain = min(_ratio(text, dark), _ratio(text, bright))
        outlined = _ratio(text, _hex_lum(stroke)) if outline >= 3 else 0.0
        out.append((layer.start, max(plain, outlined)))
    return out


# ------------------------------------------------------------------ the tests


def test_green5_every_caption_is_readable_over_the_background(monkeypatch, tmp_path):
    job = _job(monkeypatch, tmp_path)
    gb._run_generative_job(str(job.id))
    assert job.status == "awaiting_device", job.error_detail
    variant = job.assembly_plan["variants"][0]
    judged = _caption_judgements(job)
    assert judged
    worst = min(judged, key=lambda item: item[1])
    assert worst[1] >= 4.5, (
        f"caption at {worst[0]:.1f}s reaches {worst[1]:.2f}:1 "
        f"(text {variant['caption_text_color']}, outline {variant.get('caption_stroke_width')})"
    )


def _look(job) -> dict:
    variant = job.assembly_plan["variants"][0]
    return {
        key: variant.get(key)
        for key in ("caption_text_color", "caption_stroke_width", "caption_shadow_enabled")
    } | {"stroke_color": (variant.get("caption_editor_style") or {}).get("stroke_color")}


def test_without_a_background_the_caption_look_is_what_main_picks(monkeypatch, tmp_path):
    """5: negative control, a plain Talking edit is chosen from the take exactly as before."""
    job = _job(monkeypatch, tmp_path, background=False)
    gb._run_generative_job(str(job.id))
    assert _recipe(job).speaker_background is None
    assert _look(job) == {
        "caption_text_color": "#767676",
        "caption_stroke_width": 0,
        "caption_shadow_enabled": False,
        "stroke_color": None,
    }


def test_switch_off_is_the_look_main_picks_over_a_background(monkeypatch, tmp_path):
    """7: off, the background edit gets exactly the take-picked look (prod Green 5)."""
    monkeypatch.setattr(settings, "caption_background_contrast_enabled", False)
    job = _job(monkeypatch, tmp_path)
    gb._run_generative_job(str(job.id))
    assert _look(job)["caption_text_color"] == "#767676"
    assert _look(job)["caption_stroke_width"] == 0
