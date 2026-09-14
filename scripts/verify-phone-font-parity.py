"""Render and qualify the synthetic variable-font device fixtures.

Run ``render`` in the production Linux image (with ``PYTHONPATH=.`` from
``src/apps/api``). ``compare`` is platform-independent and compares the
captured iPhone PNGs against those Linux references.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image, ImageFilter

CANVAS_SIZE = (1080, 1920)
SAMPLE_TIMES = (0.6, 1.0, 3.0, 5.3)
SETTLED_TIMES = {1.0, 3.0, 5.3}
DEVICE_KINDS = ("preview", "export")
FONT_CASES = {
    "fade-in-fraunces-default": {
        "family": "Fraunces",
        "axes": {"opsz": 9.0, "wght": 900.0, "SOFT": 0.0, "WONK": 1.0},
    },
    "fade-in-dm-sans-default": {
        "family": "DM Sans",
        "axes": {"opsz": 9.0, "wght": 400.0},
    },
}
BASELINE_CASE = "baseline-no-text"
WHITE_THRESHOLD = 240
MAX_EDGE_DEVIATION_PX = 1
MAX_DILATED_MASK_MISMATCH = 0.005


class VerificationError(ValueError):
    pass


def sample_name(seconds: float) -> str:
    return f"{seconds:.1f}"


def read_fixtures(path: Path) -> dict[str, dict[str, Any]]:
    try:
        values = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as error:
        raise VerificationError(f"cannot read fixtures {path}: {error}") from error
    if not isinstance(values, list):
        raise VerificationError("fixture catalog must be a JSON list")
    fixtures: dict[str, dict[str, Any]] = {}
    for value in values:
        if not isinstance(value, dict) or not isinstance(value.get("id"), str):
            raise VerificationError(
                "fixture catalog contains a case without a string id"
            )
        if value["id"] in fixtures:
            raise VerificationError(
                f"fixture catalog contains duplicate id {value['id']}"
            )
        fixtures[value["id"]] = value
    return fixtures


def validate_font_fixtures(
    fixtures: dict[str, dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    unexpected = [
        case_id
        for case_id, case in fixtures.items()
        if "overlay" in case and case_id not in FONT_CASES
    ]
    if unexpected:
        raise VerificationError(
            f"only default-font cases may carry overlay specs: {', '.join(sorted(unexpected))}"
        )
    selected: dict[str, dict[str, Any]] = {}
    for case_id, expected in FONT_CASES.items():
        case = fixtures.get(case_id)
        if case is None:
            raise VerificationError(f"missing fixture {case_id}")
        overlay = case.get("overlay")
        layer = case.get("layer")
        if not isinstance(overlay, dict) or not isinstance(layer, dict):
            raise VerificationError(
                f"{case_id} must contain overlay and compiled layer"
            )
        if overlay.get("font_family") != expected["family"]:
            raise VerificationError(
                f"{case_id} has unsupported font family {overlay.get('font_family')!r}"
            )
        if overlay.get("effect") != "fade-in":
            raise VerificationError(f"{case_id} must remain a fade-in fixture")
        if overlay.get("start_s") != 0.5 or overlay.get("end_s") != 5.5:
            raise VerificationError(f"{case_id} has unexpected fixture timing")
        runs = layer.get("runs")
        if not isinstance(runs, list) or not runs:
            raise VerificationError(f"{case_id} has no compiled text runs")
        if any(
            run.get("font_variations") != expected["axes"]
            for run in runs
            if isinstance(run, dict)
        ):
            raise VerificationError(
                f"{case_id} does not retain expected Linux font axes {expected['axes']}"
            )
        if any(not isinstance(run, dict) for run in runs):
            raise VerificationError(f"{case_id} has an invalid compiled run")
        selected[case_id] = overlay
    return selected


def ensure_png(path: Path) -> Image.Image:
    if not path.is_file():
        raise VerificationError(f"missing PNG {path}")
    try:
        with Image.open(path) as source:
            source.load()
            if source.size != CANVAS_SIZE:
                raise VerificationError(
                    f"{path} is {source.size}, expected {CANVAS_SIZE}"
                )
            return source.convert("RGBA")
    except OSError as error:
        raise VerificationError(f"cannot decode PNG {path}: {error}") from error


def render(args: argparse.Namespace) -> int:
    # Keep these imports inside the Linux-only command so comparison can run on
    # a host that has Pillow/numpy but no Skia application environment.
    if sys.platform != "linux":
        raise VerificationError(
            "render must run on Linux to qualify production Skia font defaults"
        )
    from app.pipeline.text_overlay_skia import (
        _draw_frame,
        _validate_and_clamp,
        _write_png_pillow,
    )

    fixtures = read_fixtures(args.fixtures)
    overlays = validate_font_fixtures(fixtures)
    args.output.mkdir(parents=True, exist_ok=True)
    for case_id, overlay in overlays.items():
        spec = _validate_and_clamp(overlay, 6.0)
        if spec is None:
            raise VerificationError(
                f"{case_id} was rejected by the production overlay validator"
            )
        target = args.output / case_id
        target.mkdir(parents=True, exist_ok=True)
        for seconds in SAMPLE_TIMES:
            frame = _draw_frame(
                spec, seconds - spec["start_s"], spec["end_s"] - spec["start_s"]
            )
            png = target / f"{sample_name(seconds)}.png"
            _write_png_pillow(frame, str(png))
            ensure_png(png)
        print(f"rendered {case_id}: {len(SAMPLE_TIMES)} 1080x1920 Linux Skia frames")
    return 0


def white_mask(image: Image.Image) -> np.ndarray:
    rgba = np.asarray(image.convert("RGBA"), dtype=np.uint8)
    return (rgba[..., :3].min(axis=2) > WHITE_THRESHOLD) & (
        rgba[..., 3] > WHITE_THRESHOLD
    )


def bbox(mask: np.ndarray) -> tuple[int, int, int, int] | None:
    rows, columns = np.where(mask)
    if not len(rows):
        return None
    return (
        int(columns.min()),
        int(rows.min()),
        int(columns.max()) + 1,
        int(rows.max()) + 1,
    )


def ink_bbox(image: Image.Image) -> tuple[int, int, int, int] | None:
    return bbox(np.asarray(image.convert("RGBA"), dtype=np.uint8)[..., 3] > 0)


def padded(
    box: tuple[int, int, int, int], padding: int = 8
) -> tuple[int, int, int, int]:
    return (
        max(0, box[0] - padding),
        max(0, box[1] - padding),
        min(CANVAS_SIZE[0], box[2] + padding),
        min(CANVAS_SIZE[1], box[3] + padding),
    )


def dilate(mask: np.ndarray) -> np.ndarray:
    return (
        np.asarray(
            Image.fromarray(mask.astype(np.uint8) * 255).filter(
                ImageFilter.MaxFilter(3)
            )
        )
        > 0
    )


def mask_metrics(
    cloud: Image.Image, known_background_white: np.ndarray, actual: Image.Image
) -> dict[str, Any]:
    # The preview/export encoders can put the same white source pixel on
    # opposite sides of the threshold. Exclude the one-pixel-expanded UNION
    # of both matching baselines from cloud and actual masks so the gate only
    # measures observable glyph signal, never known source-video white.
    cloud_mask = white_mask(cloud) & ~known_background_white
    cloud_box = bbox(cloud_mask)
    if cloud_box is None:
        raise VerificationError("cloud frame has no opaque white fill glyph pixels")
    roi = padded(cloud_box)
    actual_mask = white_mask(actual) & ~known_background_white
    limited_actual = np.zeros_like(actual_mask)
    limited_actual[roi[1] : roi[3], roi[0] : roi[2]] = actual_mask[
        roi[1] : roi[3], roi[0] : roi[2]
    ]
    actual_box = bbox(limited_actual)
    if actual_box is None:
        raise VerificationError(
            "actual frame has no white glyph pixels in the cloud glyph ROI"
        )
    edge_deviation = max(abs(a - b) for a, b in zip(cloud_box, actual_box, strict=True))
    missed = cloud_mask & ~dilate(limited_actual)
    extra = limited_actual & ~dilate(cloud_mask)
    glyph_union = cloud_mask | limited_actual
    mismatch = float((missed | extra).sum() / max(1, glyph_union.sum()))
    return {
        "cloud_glyph_bbox": list(cloud_box),
        "actual_glyph_bbox": list(actual_box),
        "glyph_roi": list(roi),
        "edge_deviation_px": edge_deviation,
        "observable_union_pixel_count": int(glyph_union.sum()),
        "dilated_mismatch_pixel_count": int((missed | extra).sum()),
        "dilated_mask_mismatch": mismatch,
        "passed": edge_deviation <= MAX_EDGE_DEVIATION_PX
        and mismatch <= MAX_DILATED_MASK_MISMATCH,
    }


def rgb_roi_mae(
    expected: Image.Image, actual: Image.Image, cloud: Image.Image
) -> float:
    cloud_box = ink_bbox(cloud)
    if cloud_box is None:
        raise VerificationError("cloud frame has no glyph ROI")
    roi = padded(cloud_box)
    expected_pixels = np.asarray(expected.convert("RGB"), dtype=np.int16)
    actual_pixels = np.asarray(actual.convert("RGB"), dtype=np.int16)
    difference = np.abs(expected_pixels - actual_pixels)
    return float(difference[roi[1] : roi[3], roi[0] : roi[2]].mean())


def compare(args: argparse.Namespace) -> int:
    results: list[dict[str, Any]] = []
    failures: list[str] = []
    for case_id in FONT_CASES:
        for seconds in SAMPLE_TIMES:
            name = sample_name(seconds)
            try:
                cloud = ensure_png(args.cloud / case_id / f"{name}.png")
            except VerificationError as error:
                failures.append(str(error))
                continue
            for kind in DEVICE_KINDS:
                row: dict[str, Any] = {"case": case_id, "time_s": seconds, "kind": kind}
                try:
                    baseline = ensure_png(
                        args.device / BASELINE_CASE / f"{kind}-{name}.png"
                    )
                    other_kind = next(value for value in DEVICE_KINDS if value != kind)
                    other_baseline = ensure_png(
                        args.device / BASELINE_CASE / f"{other_kind}-{name}.png"
                    )
                    actual = ensure_png(args.device / case_id / f"{kind}-{name}.png")
                    expected = Image.alpha_composite(baseline, cloud).convert("RGB")
                    row["rgb_roi_mae"] = rgb_roi_mae(expected, actual, cloud)
                    if seconds in SETTLED_TIMES:
                        known_background_white = dilate(
                            white_mask(baseline) | white_mask(other_baseline)
                        )
                        row["glyph_shape"] = mask_metrics(
                            cloud, known_background_white, actual
                        )
                        if not row["glyph_shape"]["passed"]:
                            failures.append(
                                f"{case_id} {kind} {name}: glyph shape {row['glyph_shape']}"
                            )
                    else:
                        row["glyph_shape"] = {"skipped": "fade ramp diagnostic only"}
                except VerificationError as error:
                    row["error"] = str(error)
                    failures.append(f"{case_id} {kind} {name}: {error}")
                results.append(row)
    report = {
        "status": "failed" if failures else "passed",
        "thresholds": {
            "settled_times_s": sorted(SETTLED_TIMES),
            "white_fill": "minimum RGB > 240 and alpha > 240",
            "max_bbox_edge_deviation_px": MAX_EDGE_DEVIATION_PX,
            "max_dilated_mask_mismatch": MAX_DILATED_MASK_MISMATCH,
            "rgb_roi_mae": "diagnostic only; shadow and codec pixels are not exact-parity gated",
        },
        "results": results,
        "failures": failures,
    }
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, indent=2) + "\n")
    print(
        f"phone-font parity: {report['status']}; {len(results)} comparisons; {len(failures)} failures"
    )
    for row in results:
        print(
            f"{row['case']} {row['kind']} t={row['time_s']:.1f}: RGB ROI MAE={row.get('rgb_roi_mae', 'n/a')}"
        )
    if failures:
        for failure in failures:
            print(f"FAIL: {failure}", file=sys.stderr)
        return 1
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    render_parser = commands.add_parser(
        "render", help="render Linux Skia reference PNGs"
    )
    render_parser.add_argument("--fixtures", type=Path, required=True)
    render_parser.add_argument("--output", type=Path, required=True)
    render_parser.set_defaults(handler=render)
    compare_parser = commands.add_parser(
        "compare", help="compare iPhone capture PNGs with Linux references"
    )
    compare_parser.add_argument("--cloud", type=Path, required=True)
    compare_parser.add_argument("--device", type=Path, required=True)
    compare_parser.add_argument("--report", type=Path, required=True)
    compare_parser.set_defaults(handler=compare)
    args = parser.parse_args()
    try:
        return args.handler(args)
    except VerificationError as error:
        print(f"phone-font parity: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
