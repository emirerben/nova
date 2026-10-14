"""Bounded background observations from the same analysis proxies used for faces."""

from __future__ import annotations

import subprocess

WIDTH, HEIGHT = 108, 192


def sample_background(path: str, at_s: float, *, crop=None, canvas=None) -> bytes | None:
    filters = []
    if crop is not None:
        filters.append(f"crop=iw*{crop['width']}:ih*{crop['height']}:iw*{crop['x']}:ih*{crop['y']}")
    target_width, target_height = (
        (HEIGHT, WIDTH) if canvas and canvas.width > canvas.height else (WIDTH, HEIGHT)
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
                "-ss",
                str(max(0, at_s)),
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
