"""Bounded background observations from the same analysis proxies used for faces."""

from __future__ import annotations

import subprocess

WIDTH, HEIGHT = 108, 192


def sample_background(
    path: str, at_s: float | None, *, crop=None, canvas=None, focus=None
) -> bytes | None:
    """One gray frame of ``path`` at ``at_s``, cover-fit to the analysis grid. ``at_s`` None reads
    the first frame without seeking: a still photo (Green 5 backgrounds), where an input seek,
    even to 0, yields no frame.

    ``focus`` (Qendresa item 18, ``{"x", "y", "zoom"}``): the picture placed the way the phone
    places an aimed background (`VisualMediaPlacement`): scaled by cover x zoom but never below
    contain, offset by (canvas - picture) x focal, black where it does not reach the frame."""
    filters = []
    if crop is not None:
        filters.append(f"crop=iw*{crop['width']}:ih*{crop['height']}:iw*{crop['x']}:ih*{crop['y']}")
    target_width, target_height = (
        (HEIGHT, WIDTH) if canvas and canvas.width > canvas.height else (WIDTH, HEIGHT)
    )
    if focus is not None:
        x, y, zoom = float(focus["x"]), float(focus["y"]), float(focus["zoom"])
        tw, th = target_width, target_height
        scale = f"max(max({tw}/iw,{th}/ih)*{zoom},min({tw}/iw,{th}/ih))"
        filters.extend(
            [
                "format=gray",
                f"scale=w='iw*{scale}+0.5':h='ih*{scale}+0.5'",
                f"crop=w='min(iw,{tw})':h='min(ih,{th})':x='(iw-ow)*{x}':y='(ih-oh)*{y}'",
                f"pad=w={tw}:h={th}:x='(ow-iw)*{x}':y='(oh-ih)*{y}':color=black",
            ]
        )
    filters.extend(
        [
            f"scale={target_width}:{target_height}:force_original_aspect_ratio=increase",
            f"crop={target_width}:{target_height}",
            f"scale={WIDTH}:{HEIGHT}",
            "format=gray",
        ]
    )
    try:
        result = subprocess.run(
            [
                "ffmpeg",
                "-v",
                "error",
                *(() if at_s is None else ("-ss", str(max(0, at_s)))),
                "-i",
                path,
                "-vf",
                ",".join(filters),
                "-frames:v",
                "1",
                "-f",
                "rawvideo",
                "-",
            ],
            capture_output=True,
            timeout=1.5,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return (
        result.stdout if result.returncode == 0 and len(result.stdout) == WIDTH * HEIGHT else None
    )


def automatic_fill(box, frames: list[dict]) -> tuple[str, float]:
    """Choose one stable fill across samples, without adding an effect or backdrop."""
    x0, y0 = max(0, int(box[0] * WIDTH)), max(0, int(box[1] * HEIGHT))
    x1, y1 = min(WIDTH, int(box[2] * WIDTH) + 1), min(HEIGHT, int(box[3] * HEIGHT) + 1)
    limits = []
    for frame in frames:
        pixels = frame["pixels"]
        values = sorted(pixels[y * WIDTH + x] for y in range(y0, y1) for x in range(x0, x1))
        if values:
            limits.extend(
                (values[int(len(values) * 0.1)] / 255, values[int((len(values) - 1) * 0.9)] / 255)
            )
    if not limits:
        return "#FFFFFF", 0
    luminance = [v / 12.92 if v <= 0.04045 else ((v + 0.055) / 1.055) ** 2.4 for v in limits]
    choices = [("#FFFFFF", 1.0), ("#000000", 0.0), ("#767676", 0.18116424424986022)]
    return max(
        (
            (color, min((max(v, fill) + 0.05) / (min(v, fill) + 0.05) for v in luminance))
            for color, fill in choices
        ),
        key=lambda choice: choice[1],
    )
