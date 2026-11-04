"""Deterministic near-duplicate fixtures with known ground truth (no network, no model).

Scenes are procedural (seeded multi-octave noise plus a few shapes), so nothing binary is
committed. ``build(dest)`` writes JPEGs and MP4s with ffmpeg/PIL and returns the manifest:

    {"items": [{"id", "path", "kind", "group", "note"}...], "ambiguous": [[group, group]...]}

Ground truth: two items of the SAME ``group`` are near-duplicates; items of different groups are
not. ``ambiguous`` group pairs (same background, other subject: a judgement call) are
never scored.

    python tests/fixtures/clip_signals/dups/build.py <dest-dir>     # then inspect the files
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image, ImageDraw, ImageEnhance, ImageFilter, ImageOps

W, H = 540, 960  # portrait phone frame; small enough to keep the suite fast


def scene(seed: int, *, flat: bool = False, size: tuple[int, int] = (W, H)) -> Image.Image:
    """A repeatable pseudo-photo: coloured low-frequency blobs plus a few hard-edged shapes."""
    rng = np.random.default_rng(seed)
    w, h = size
    canvas = np.zeros((h, w, 3), dtype=np.float32)
    for octave, weight in ((4, 0.55), (8, 0.28), (20, 0.12), (60, 0.05)):
        small = rng.random((max(2, h // octave), max(2, w // octave), 3), dtype=np.float32)
        layer = Image.fromarray((small * 255).astype(np.uint8)).resize((w, h), Image.BICUBIC)
        canvas += weight * np.asarray(layer, dtype=np.float32)
    img = Image.fromarray(np.clip(canvas, 0, 255).astype(np.uint8))
    if flat:
        return img
    draw = ImageDraw.Draw(img)
    for _ in range(6):
        x0, y0 = int(rng.integers(0, w - 80)), int(rng.integers(0, h - 80))
        x1, y1 = x0 + int(rng.integers(40, w // 2)), y0 + int(rng.integers(40, h // 3))
        colour = tuple(int(c) for c in rng.integers(0, 255, 3))
        (draw.ellipse if rng.random() < 0.5 else draw.rectangle)([x0, y0, x1, y1], fill=colour)
    return img


def sky(seed: int, *, sun: bool) -> Image.Image:
    """Low-detail frame: a vertical blue gradient, optionally one small bright disc."""
    rng = np.random.default_rng(seed)
    top = np.array([40 + int(rng.integers(0, 40)), 90, 200], dtype=np.float32)
    bottom = np.array([180, 200 + int(rng.integers(0, 40)), 235], dtype=np.float32)
    ramp = np.linspace(0, 1, H, dtype=np.float32)[:, None, None]
    img = Image.fromarray(
        np.broadcast_to(top * (1 - ramp) + bottom * ramp, (H, W, 3)).astype(np.uint8).copy()
    )
    if sun:
        ImageDraw.Draw(img).ellipse([W // 2 - 30, 120, W // 2 + 30, 180], fill=(255, 250, 220))
    return img


def horizon(seed: int) -> Image.Image:
    """Sky over a dark ridge line: low detail but informative. Different seeds, different shots."""
    rng = np.random.default_rng(seed)
    img = sky(seed, sun=False)
    ridge = [(0, H)]
    y = int(H * 0.62)
    for x in range(0, W + 1, 45):
        y = int(np.clip(y + rng.integers(-60, 60), H * 0.45, H * 0.8))
        ridge.append((x, y))
    ridge.append((W, H))
    ImageDraw.Draw(img).polygon(ridge, fill=(25, 30, 28))
    return img


def black(noise: int) -> Image.Image:
    rng = np.random.default_rng(900 + noise)
    base = rng.integers(0, 3, (H, W, 3), dtype=np.uint8)
    return Image.fromarray(base if noise else np.zeros((H, W, 3), dtype=np.uint8))


def with_object(base: Image.Image, x: int, y: int = 600) -> Image.Image:
    """``base`` plus a small bright subject at (x, y): the moving thing in a burst."""
    img = base.copy()
    ImageDraw.Draw(img).ellipse([x, y, x + 70, y + 110], fill=(250, 235, 40), outline=(20, 20, 20))
    return img


def recompress(img: Image.Image, *, quality: int, scale: float) -> Image.Image:
    small = img.resize((int(img.width * scale), int(img.height * scale)), Image.BILINEAR)
    from io import BytesIO

    buf = BytesIO()
    small.save(buf, "JPEG", quality=quality)
    return Image.open(BytesIO(buf.getvalue())).convert("RGB")


def crop_fraction(img: Image.Image, frac: float) -> Image.Image:
    dx, dy = int(img.width * frac / 2), int(img.height * frac / 2)
    return img.crop((dx, dy, img.width - dx, img.height - dy))


def _ffmpeg(*args: str) -> None:
    subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-y", *args], check=True)


def video_from(
    img: Image.Image,
    out: Path,
    *,
    crf: int = 23,
    width: int = 360,
    pan: float = 0.05,
    phase: float = 0.0,
    seconds: float = 2.0,
    rotate: int | None = None,
    frames: int | None = None,
) -> None:
    """An MP4 of ``img`` with a slow pan (so keyframes differ a little, like a handheld take)."""
    h = int(round(width * img.height / img.width / 2)) * 2
    w = width
    if rotate is not None:
        # A landscape raster whose display matrix turns it upright: how a phone stores portrait.
        img = img.rotate(-rotate, expand=True)
        w, h = h, width
    src = out.with_suffix(".src.png")
    img.save(src)
    crop = (
        f"crop=w=iw*{1 - pan}:h=ih*{1 - pan}:x='(iw-ow)*(t/{seconds}+{phase})/2':y='(ih-oh)/2',"
        f"scale={w}:{h},format=yuv420p"
    )
    args = ["-loop", "1", "-framerate", "12", "-i", str(src), "-vf", crop]
    args += ["-frames:v", str(frames)] if frames else ["-t", str(seconds)]
    args += ["-c:v", "libx264", "-preset", "veryfast", "-crf", str(crf), "-g", "12"]
    if rotate is None:
        _ffmpeg(*args, str(out))
    else:
        plain = out.with_suffix(".plain.mp4")
        _ffmpeg(*args, str(plain))
        _ffmpeg("-display_rotation", str(rotate), "-i", str(plain), "-c", "copy", str(out))
        plain.unlink()
    src.unlink()


def build(dest: Path) -> dict[str, Any]:
    dest.mkdir(parents=True, exist_ok=True)
    items: list[dict[str, Any]] = []

    def add_image(item_id: str, group: str, img: Image.Image, note: str) -> None:
        path = dest / f"{item_id}.jpg"
        img.convert("RGB").save(path, "JPEG", quality=92)
        items.append(
            {"id": item_id, "path": str(path), "kind": "image", "group": group, "note": note}
        )

    def add_video(item_id: str, group: str, img: Image.Image, note: str, **kw: Any) -> None:
        path = dest / f"{item_id}.mp4"
        video_from(img, path, **kw)
        items.append(
            {"id": item_id, "path": str(path), "kind": "video", "group": group, "note": note}
        )

    # ── photos ───────────────────────────────────────────────────────────────────────
    a = scene(1)
    add_image("p_a", "pa", a, "original")
    add_image("p_a_recompressed", "pa", recompress(a, quality=35, scale=0.6), "re-encoded, smaller")
    add_image("p_a_crop", "pa", crop_fraction(a, 0.04), "slightly cropped (4%)")
    add_image("p_a_dark", "pa", ImageEnhance.Brightness(a).enhance(0.35), "dark twin")
    add_image("p_a_blur", "pa", a.filter(ImageFilter.GaussianBlur(4)), "blurred twin")
    add_image("p_a_flipped", "pa_flip", ImageOps.mirror(a), "mirrored: a different shot")
    b = scene(2)
    add_image("p_b1", "pb", with_object(b, 100), "burst frame 1")
    add_image("p_b2", "pb", with_object(b, 108), "burst frame 2")
    add_image("p_b3", "pb", with_object(b, 118), "burst frame 3")
    for n in range(3, 15):
        add_image(f"p_scene{n}", f"p_scene{n}", scene(n), "unrelated scene")
    # Same blobs, different shapes drawn on top: shares low-frequency layout, still another shot.
    add_image("p_cousin", "p_cousin", scene(1, flat=True), "same background, no shapes")

    # ── low-information frames: must never read as duplicates of each other ──────────
    add_image("p_sky1", "p_sky1", sky(1, sun=False), "plain sky")
    add_image("p_sky2", "p_sky2", sky(2, sun=False), "another plain sky")
    add_image("p_sky_sun", "p_sky_sun", sky(1, sun=True), "sky with a sun")
    add_image("p_horizon1", "p_horizon1", horizon(1), "sky over a ridge")
    add_image("p_horizon2", "p_horizon2", horizon(2), "another ridge, same kind of sky")
    add_image("p_horizon3", "p_horizon3", horizon(3), "a third ridge")
    add_image("p_black1", "p_black1", black(0), "pure black")
    add_image("p_black2", "p_black2", black(1), "near black, sensor noise")

    # ── videos ───────────────────────────────────────────────────────────────────────
    add_video("v_a", "va", a, "original")
    add_video("v_a_reenc", "va", a, "re-encoded smaller, harsher", crf=36, width=240)
    add_video("v_a_crop", "va", crop_fraction(a, 0.04), "slightly cropped")
    add_video("v_a_dark", "va", ImageEnhance.Brightness(a).enhance(0.3), "dark twin")
    add_video("v_a_retake", "va", a, "second pass over the same scene, other pan phase", phase=0.25)
    add_video("v_a_rotated", "va", a, "stored landscape + display rotation 90", rotate=90)
    add_video("v_a_oneframe", "va", a, "single-frame clip", frames=1)
    add_video("v_a_flipped", "va_flip", ImageOps.mirror(a), "mirrored: a different shot")
    add_video("v_burst1", "vb", with_object(b, 100), "burst clip 1")
    add_video("v_burst2", "vb", with_object(b, 112), "burst clip 2", phase=0.1)
    for n in range(3, 9):
        add_video(f"v_scene{n}", f"v_scene{n}", scene(n), "unrelated scene")
    add_video("v_sky1", "v_sky1", sky(1, sun=False), "plain sky")
    add_video("v_sky2", "v_sky2", sky(2, sun=False), "another plain sky")
    add_video("v_black", "v_black", black(0), "all black")

    ambiguous = [["pa", "p_cousin"]]
    manifest = {"items": items, "ambiguous": ambiguous}
    (dest / "manifest.json").write_text(json.dumps(manifest, indent=1))
    return manifest


if __name__ == "__main__":
    out = build(Path(sys.argv[1]))
    print(f"{len(out['items'])} items in {sys.argv[1]}")
