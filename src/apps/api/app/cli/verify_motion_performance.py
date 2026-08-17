"""Production-container performance gate for maximum-complexity Creator motion."""

from __future__ import annotations

import argparse
import binascii
import json
import resource
import struct
import sys
import tempfile
import time
import zlib
from pathlib import Path

from app.pipeline.motion_scene import (
    MOTION_RUNTIME_HASH,
    _render_sequence,
    validate_motion_instances,
)

DEFAULT_MAX_SECONDS = 180.0
DEFAULT_MAX_PEAK_BYTES = int(2.5 * 1024**3)


def _maximum_complexity_scene() -> dict:
    """Max-content full-window Evolving block: exactly 960 weighted frame units."""
    return {
        "id": "verify-evolving",
        "preset_id": "evolving_type",
        "preset_version": 2,
        "start_frame": 0,
        "end_frame_exclusive": 240,
        "palette": {"primary": "#000000", "accent": "#FFFFFF"},
        "intensity": 1,
        "params": {
            "headline": "W" * 48,
            "subtitle": "M" * 72,
            "icon_count": 5,
            "icon_style": "botanical",
            "text_stagger_ms": 45,
            "icon_stagger_ms": 100,
            "morph_amplitude": 1,
            "density": "high",
            "layout": "spread",
            "order": "center-out",
            "typography_scale": 2,
            "backdrop_opacity": 1,
            "split_icons": True,
        },
        "motion": {
            "version": 2,
            "speed": 0.75,
            "easing": "ease-in-out-cubic",
            "hold_frames": 74,
        },
    }


def _maximum_media_scenes() -> list[dict]:
    """Eight weight-3 Film Strips for 36 frames: 864 units and 64 unique images."""
    scenes: list[dict] = []
    for scene_index in range(8):
        assets = [
            {
                "asset_id": f"benchmark-{scene_index}-{asset_index}",
                "gcs_path": (
                    f"users/performance/plan/item/pool/benchmark-{scene_index}-{asset_index}.png"
                ),
            }
            for asset_index in range(8)
        ]
        scenes.append(
            {
                "id": f"verify-film-{scene_index}",
                "preset_id": "film_strip",
                "preset_version": 2,
                "start_frame": 0,
                "end_frame_exclusive": 36,
                "palette": {"primary": "#0C0C0E", "accent": "#C7FF3D"},
                "intensity": 1,
                "params": {"assets": assets},
                "motion": {
                    "version": 2,
                    "speed": 4,
                    "easing": "ease-in-out-cubic",
                    "hold_frames": 0,
                },
            }
        )
    return scenes


def _png_chunk(kind: bytes, data: bytes) -> bytes:
    payload = kind + data
    return struct.pack(">I", len(data)) + payload + struct.pack(">I", binascii.crc32(payload))


def _normalized_benchmark_png() -> bytes:
    """Return a deterministic opaque 2048px PNG without adding image dependencies."""
    width = 2048
    height = 2048
    row = b"\x00" + bytes((39, 56, 47)) * width
    pixels = row * height
    return b"".join(
        (
            b"\x89PNG\r\n\x1a\n",
            _png_chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)),
            _png_chunk(b"IDAT", zlib.compress(pixels, level=1)),
            _png_chunk(b"IEND", b""),
        )
    )


def _write_benchmark_assets(root: Path, scenes: list[dict]) -> dict[str, str]:
    asset_dir = root / "normalized-assets"
    asset_dir.mkdir(parents=True, exist_ok=True)
    png = _normalized_benchmark_png()
    paths: dict[str, str] = {}
    for scene in scenes:
        for asset in scene["params"]["assets"]:
            path = asset_dir / f"{asset['asset_id']}.png"
            path.write_bytes(png)
            paths[asset["asset_id"]] = str(path)
    return paths


def _peak_child_bytes() -> int:
    value = int(resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss)
    return value if sys.platform == "darwin" else value * 1024


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default=".motion-verify")
    parser.add_argument("--max-seconds", type=float, default=DEFAULT_MAX_SECONDS)
    parser.add_argument("--max-peak-bytes", type=int, default=DEFAULT_MAX_PEAK_BYTES)
    args = parser.parse_args()

    evolving = validate_motion_instances([_maximum_complexity_scene()], duration_frames=240)
    media = validate_motion_instances(_maximum_media_scenes(), duration_frames=240)
    results: list[dict] = []
    with tempfile.TemporaryDirectory(prefix="nova_motion_performance_") as tmpdir:
        root = Path(tmpdir)
        prepared_assets = _write_benchmark_assets(root, media)
        cases = (
            ("evolving_max_content", evolving, 240, None),
            ("film_strip_max_resources", media, 36, prepared_assets),
        )
        for name, scenes, expected_frames, asset_paths in cases:
            case_tmpdir = root / name
            case_tmpdir.mkdir()
            started = time.perf_counter()
            _, segments, frame_count = _render_sequence(
                scenes,
                width=1080,
                height=1920,
                tmpdir=str(case_tmpdir),
                prepared_asset_paths=asset_paths,
            )
            elapsed = time.perf_counter() - started
            results.append(
                {
                    "name": name,
                    "frame_count": frame_count,
                    "expected_frame_count": expected_frames,
                    "segments": segments,
                    "elapsed_seconds": round(elapsed, 3),
                    # RUSAGE_CHILDREN is cumulative, so later values safely include
                    # the largest earlier renderer child as well as this case.
                    "peak_worker_child_bytes": _peak_child_bytes(),
                }
            )
    worst_elapsed = max(result["elapsed_seconds"] for result in results)
    worst_peak_bytes = max(result["peak_worker_child_bytes"] for result in results)
    passed = (
        all(result["frame_count"] == result["expected_frame_count"] for result in results)
        and worst_elapsed < args.max_seconds
        and worst_peak_bytes < args.max_peak_bytes
    )
    report = {
        "ok": passed,
        "runtime_hash": MOTION_RUNTIME_HASH,
        "frame_count": sum(result["frame_count"] for result in results),
        "cases": results,
        "elapsed_seconds": worst_elapsed,
        "peak_worker_child_bytes": worst_peak_bytes,
        "limits": {
            "elapsed_seconds": args.max_seconds,
            "peak_worker_child_bytes": args.max_peak_bytes,
        },
    }
    output = Path(args.out)
    output.mkdir(parents=True, exist_ok=True)
    (output / "performance.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))
    if not passed:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
