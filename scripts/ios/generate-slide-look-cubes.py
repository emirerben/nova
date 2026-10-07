#!/usr/bin/env python3
"""Generate the slide look cubes from authoritative server color filters.

Run with the API venv (NumPy/Pillow) and FFmpeg. Only 64³ float RGBA cubes
are bundled. Optional real-photo references and interpolation measurements
are written separately; they do not certify the native spatial effects.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
from pathlib import Path

import numpy as np
from PIL import Image

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src/apps/api"))
from app.pipeline.look_presets import look_preset_filter  # noqa: E402

PRESETS = (
    "olive_film",
    "smoky_split_tone",
    "golden_hour",
    "faded_analog",
    "stadium_diffusion",
)


def color_filter(name: str, width: int, height: int) -> str:
    graph = look_preset_filter(name, width=width, height=height, label_prefix="cube")
    assert graph is not None
    if name in ("olive_film", "smoky_split_tone"):
        graph = graph.split(",unsharp=", 1)[0]
    elif name in ("golden_hour", "faded_analog"):
        graph = ",".join(graph.split(",")[:2])
    else:
        graph = (
            graph.split(";", 2)[1]
            .split("[cube_graded]", 1)[0]
            .split("[cube_grade_in]", 1)[1]
        )
    # Film starts in GBR. Pin the format immediately before EQ as well so
    # adjacent color samples cannot contaminate one another through 4:2:0.
    graph = graph.replace("eq=", "format=yuv444p,eq=")
    return "format=yuv444p," + graph + ",format=rgb24"


def run(arguments: list[str]) -> None:
    subprocess.run(
        ["ffmpeg", "-y", "-v", "error", "-filter_complex_threads", "1", *arguments],
        check=True,
    )


def cube(name: str, dimension: int) -> np.ndarray:
    # B,G,R array order is CIColorCube's R-fastest order. Every dimension is
    # explicit; no Hald level rounding and no holes or duplicate indices.
    axis = np.rint(np.linspace(0, 255, dimension)).astype(np.uint8)
    b, g, r = np.meshgrid(axis, axis, axis, indexing="ij")
    source = np.stack((r, g, b), axis=-1)
    with tempfile.TemporaryDirectory(prefix="slide-look-cube-") as temporary:
        input_path, output_path = (
            Path(temporary) / "in.rgb",
            Path(temporary) / "out.rgb",
        )
        input_path.write_bytes(source.tobytes())
        run(
            [
                "-f",
                "rawvideo",
                "-pixel_format",
                "rgb24",
                "-video_size",
                f"{dimension**2}x{dimension}",
                "-i",
                str(input_path),
                "-vf",
                color_filter(name, dimension**2, dimension),
                "-frames:v",
                "1",
                "-pix_fmt",
                "rgb24",
                "-f",
                "rawvideo",
                str(output_path),
            ]
        )
        graded = np.frombuffer(output_path.read_bytes(), dtype=np.uint8).reshape(
            dimension, dimension, dimension, 3
        )
    rgba = np.ones((dimension, dimension, dimension, 4), dtype="<f4")
    rgba[..., :3] = graded.astype(np.float32) / 255
    return rgba


def interpolate(cube_data: np.ndarray, rgb: np.ndarray) -> np.ndarray:
    coordinates = rgb.astype(np.float32) / 255 * (cube_data.shape[0] - 1)
    lower = np.floor(coordinates).astype(np.int32)
    upper = np.minimum(lower + 1, cube_data.shape[0] - 1)
    fraction = coordinates - lower
    result = np.zeros_like(coordinates)
    for blue in range(2):
        for green in range(2):
            for red in range(2):
                choices = (red, green, blue)
                indices = [
                    upper[..., c] if choices[c] else lower[..., c] for c in range(3)
                ]
                weight = np.prod(
                    np.stack(
                        [
                            fraction[..., c] if choices[c] else 1 - fraction[..., c]
                            for c in range(3)
                        ],
                        axis=-1,
                    ),
                    axis=-1,
                )
                result += (
                    cube_data[indices[2], indices[1], indices[0], :3]
                    * weight[..., None]
                )
    return result * 255


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--photo", type=Path)
    parser.add_argument("--references-dir", type=Path)
    arguments = parser.parse_args()
    if bool(arguments.photo) != bool(arguments.references_dir):
        parser.error("--photo and --references-dir must be supplied together")
    arguments.out.mkdir(parents=True, exist_ok=True)
    report = {
        "ffmpeg": subprocess.check_output(
            ["ffmpeg", "-version"], text=True
        ).splitlines()[0],
        "looks": {},
    }
    if arguments.references_dir:
        arguments.references_dir.mkdir(parents=True, exist_ok=True)
        cropped = arguments.references_dir / "source.png"
        run(
            [
                "-i",
                str(arguments.photo),
                "-vf",
                "scale=1080:1350:force_original_aspect_ratio=increase,crop=1080:1350",
                "-frames:v",
                "1",
                str(cropped),
            ]
        )
        rgb = np.asarray(Image.open(cropped).convert("RGB"))[::3, ::3]
    for name in PRESETS:
        low = cube(name, 64)
        (arguments.out / f"{name}-64.cube.bin").write_bytes(low.tobytes())
        measurements = {"cube64_bytes": low.nbytes, "spatial_parity_verified": False}
        if arguments.references_dir:
            high = cube(name, 128)
            delta = np.abs(interpolate(low, rgb) - interpolate(high, rgb))
            measurements.update(
                {
                    "interpolation64_vs128_mean_bytes": float(delta.mean()),
                    "interpolation64_vs128_p99_bytes": float(np.percentile(delta, 99)),
                    "interpolation64_vs128_max_bytes": float(delta.max()),
                }
            )
            run(
                [
                    "-i",
                    str(cropped),
                    "-vf",
                    color_filter(name, 1080, 1350),
                    "-frames:v",
                    "1",
                    str(arguments.references_dir / f"color-{name}.png"),
                ]
            )
            graph = look_preset_filter(
                name, width=1080, height=1350, label_prefix="reference"
            )
            run(
                [
                    "-i",
                    str(cropped),
                    "-filter_complex",
                    f"[0:v]{graph}[out]",
                    "-map",
                    "[out]",
                    "-frames:v",
                    "1",
                    str(arguments.references_dir / f"full-{name}.png"),
                ]
            )
        report["looks"][name] = measurements
    if arguments.references_dir:
        (arguments.references_dir / "slide-look-cubes-report.json").write_text(
            json.dumps(report, indent=2) + "\n"
        )


if __name__ == "__main__":
    main()
