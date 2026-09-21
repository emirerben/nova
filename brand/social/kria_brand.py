"""Shared brand primitives for the Kria social kit (KRI-52).

Everything here derives from in-repo sources of truth:
  * wordmark geometry  -> src/apps/web/public/favicon.svg (baked DynaPuff outlines)
  * palette            -> DESIGN.md section 2 "Sunlit"
  * typography         -> src/apps/api/assets/fonts (Fraunces / Inter)

Nothing is re-drawn by hand, so the kit can never drift from the product mark.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import numpy as np
import skia

REPO_ROOT = Path(__file__).resolve().parents[2]
FAVICON = REPO_ROOT / "src/apps/web/public/favicon.svg"
FONT_DIR = REPO_ROOT / "src/apps/api/assets/fonts"

# --- Sunlit palette (DESIGN.md section 2) -------------------------------------
SKY = "#9BCAFF"
BUTTER = "#FFF0A6"
SAGE = "#DDE6CB"
INK = "#30352C"
INK2 = "#526071"
PAPER = "#FFFFFF"

# --- Canvas -------------------------------------------------------------------
W, H = 1080, 1920
FPS = 30

# --- Platform UI occlusion map ------------------------------------------------
# Rectangles (x0, y0, x1, y1) of the 1080x1920 frame that each app covers with
# its own chrome. Measured from full-screen captures, September 2026. These are
# the numbers every placement in this kit is checked against; re-measure and
# re-run `build.py` when an app reflows its UI.
OCCLUSION: dict[str, list[tuple[int, int, int, int]]] = {
    "tiktok": [
        (0, 0, 1080, 200),        # For You / Following tabs + search
        (890, 980, 1080, 1740),   # right action rail
        (0, 1560, 890, 1840),     # username / caption / audio ticker
        (0, 1840, 1080, 1920),    # tab bar
    ],
    "reels": [
        (0, 0, 1080, 170),        # Reels title + camera
        (900, 1000, 1080, 1700),  # right action rail
        (0, 1530, 900, 1790),     # username / caption / audio
        (0, 1790, 1080, 1920),    # tab bar
    ],
    "shorts": [
        (0, 0, 1080, 180),        # search / cast
        (910, 1060, 1080, 1760),  # right action rail
        (0, 1600, 910, 1860),     # channel / title
        (0, 1860, 1080, 1920),    # tab bar
    ],
}

# Conservative rectangle that is clear on all three platforms at once. Verified
# by `verify.check_rect` rather than asserted by hand.
TEXT_SAFE = (60, 200, 890, 1520)

# The top band is wider than TEXT_SAFE because no right rail reaches above
# y=980 on any platform.
TOP_BAND_SAFE = (60, 200, 1020, 980)


def hex_to_rgb(value: str) -> tuple[int, int, int]:
    value = value.lstrip("#")
    return tuple(int(value[i : i + 2], 16) for i in (0, 2, 4))  # type: ignore[return-value]


def hex_to_color(value: str, alpha: float = 1.0) -> int:
    r, g, b = hex_to_rgb(value)
    return skia.Color(r, g, b, int(round(alpha * 255)))


# --- Wordmark -----------------------------------------------------------------

_SVG_SHELL = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 128 64">'
    '<g fill="{fill}">{body}</g></svg>'
)


def _favicon_paths() -> list[str]:
    """The four DynaPuff glyph outlines (k, r, i, a) in document order."""
    paths = re.findall(r"<path\b[^>]*/>", FAVICON.read_text(encoding="utf-8"))
    if len(paths) != 4:
        raise RuntimeError(f"expected 4 glyph paths in {FAVICON}, found {len(paths)}")
    return paths


def _rasterize(svg: str, container_w: int) -> np.ndarray:
    container_h = int(round(container_w / 2))  # viewBox is 128x64
    surface = skia.Surface(container_w, container_h)
    # MakeDirect does not copy: `payload` must outlive the stream and the DOM.
    payload = svg.encode("utf-8")
    stream = skia.MemoryStream.MakeDirect(payload)
    dom = skia.SVGDOM.MakeFromStream(stream)
    if dom is None:
        raise RuntimeError("skia failed to parse the wordmark SVG")
    with surface as canvas:
        canvas.clear(skia.ColorTRANSPARENT)
        dom.setContainerSize(skia.Size(container_w, container_h))
        dom.render(canvas)
    return np.array(surface.makeImageSnapshot().toarray())


def alpha_bbox(rgba: np.ndarray, threshold: int = 8) -> tuple[int, int, int, int]:
    """Tight (x0, y0, x1, y1) box around pixels more opaque than `threshold`."""
    ys, xs = np.nonzero(rgba[..., 3] > threshold)
    if len(xs) == 0:
        raise ValueError("image is fully transparent")
    return int(xs.min()), int(ys.min()), int(xs.max()) + 1, int(ys.max()) + 1


@dataclass(frozen=True)
class Wordmark:
    """A rendered `kria` wordmark plus its four letters, all sharing one origin.

    `letters` are cropped to the *same* box as `full`, so drawing any subset at
    (0, 0) reproduces the approved letter rhythm exactly. That is what lets the
    outro animate letters independently without reconstructing the mark.
    """

    full: np.ndarray
    letters: list[np.ndarray]

    @property
    def size(self) -> tuple[int, int]:
        return self.full.shape[1], self.full.shape[0]


@lru_cache(maxsize=32)
def wordmark(width_px: int, fill: str = SKY) -> Wordmark:
    """Render the approved mark at exactly `width_px` of inked width.

    Cached, so treat the returned arrays as read-only.
    """
    paths = _favicon_paths()
    body = "".join(paths)

    # Pass 1: measure how much of the container the ink actually occupies.
    probe_w = 1024
    probe = _rasterize(_SVG_SHELL.format(fill=fill, body=body), probe_w)
    px0, _, px1, _ = alpha_bbox(probe)
    container_w = int(round(probe_w * width_px / (px1 - px0)))

    # Pass 2: render at the corrected scale and crop every layer identically.
    full = _rasterize(_SVG_SHELL.format(fill=fill, body=body), container_w)
    x0, y0, x1, y1 = alpha_bbox(full)

    def crop(a: np.ndarray) -> np.ndarray:
        return np.ascontiguousarray(a[y0:y1, x0:x1])

    letters = [
        crop(_rasterize(_SVG_SHELL.format(fill=fill, body=p), container_w))
        for p in paths
    ]
    return Wordmark(full=crop(full), letters=letters)


def to_image(rgba: np.ndarray) -> skia.Image:
    return skia.Image.fromarray(
        np.ascontiguousarray(rgba), colorType=skia.kRGBA_8888_ColorType
    )


# --- Typography ---------------------------------------------------------------

_TYPEFACES: dict[str, skia.Typeface] = {}


def typeface(name: str) -> skia.Typeface:
    if name not in _TYPEFACES:
        path = FONT_DIR / f"{name}.ttf"
        tf = skia.Typeface.MakeFromFile(str(path))
        if tf is None:
            raise FileNotFoundError(f"could not load typeface {path}")
        _TYPEFACES[name] = tf
    return _TYPEFACES[name]


def font(name: str, size: float) -> skia.Font:
    f = skia.Font(typeface(name), size)
    f.setSubpixel(True)
    f.setEdging(skia.Font.Edging.kAntiAlias)
    return f


def wrap(text: str, f: skia.Font, max_width: float) -> list[str]:
    lines: list[str] = []
    current = ""
    for word in text.split():
        candidate = f"{current} {word}".strip()
        if current and f.measureText(candidate) > max_width:
            lines.append(current)
            current = word
        else:
            current = candidate
    if current:
        lines.append(current)
    return lines


# --- Drawing helpers ----------------------------------------------------------


def shadow_filter(dy: float = 3.0, sigma: float = 9.0,
                  opacity: float = 0.42) -> skia.ImageFilter:
    """The kit's one legibility device: a diffuse shadow, never a hard outline.

    Matches the render pipeline's house style (`text_overlay.py` uses gaussian
    shadows, not strokes) so burned-in captions and this kit look related.
    """
    return skia.ImageFilters.DropShadow(
        0.0, dy, sigma, sigma, skia.Color(0, 0, 0, int(round(opacity * 255)))
    )


def soft_shadow(dy: float = 3.0, sigma: float = 9.0, opacity: float = 0.42) -> skia.Paint:
    paint = skia.Paint(AntiAlias=True)
    paint.setImageFilter(shadow_filter(dy, sigma, opacity))
    return paint


def draw_rgba(canvas: skia.Canvas, rgba: np.ndarray, x: float, y: float,
              paint: skia.Paint | None = None, opacity: float = 1.0) -> None:
    if opacity <= 0:
        return
    p = paint or skia.Paint(AntiAlias=True)
    if opacity < 1.0:
        p = skia.Paint(p)
        p.setAlphaf(opacity)
    canvas.drawImage(to_image(rgba), x, y, skia.SamplingOptions(
        skia.FilterMode.kLinear, skia.MipmapMode.kLinear), p)


def save_png(rgba_surface: skia.Surface, path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    rgba_surface.makeImageSnapshot().save(str(path), skia.kPNG)
    return path


def ease_out_cubic(t: float) -> float:
    t = min(max(t, 0.0), 1.0)
    return 1.0 - (1.0 - t) ** 3
