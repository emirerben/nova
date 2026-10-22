"""Repeat the KRI-734 actual-export checks without publishing or calling models.

python -m app.cli.verify_device_export --status request-status.json \
    --sources local-source-paths.json --video export.mp4 --report report.json \
    --brand-tail standard [--montage montage.png]

Inputs are private evidence captured from the server's pinned request/receipts.
The sources file maps recipe asset IDs to downloaded pinned analysis proxies.
This local command cannot authenticate those operator-supplied input files;
production completion resolves/fences the receipts itself.

The status may be the server's stored form or the phone's camelCase wire form
(any phase, e.g. a retained "superseded" request): only its pinned request is
verified. --montage writes the sampled pictures behind every check.
"""

from __future__ import annotations

import argparse
import json
import os
import re
from pathlib import Path


def _snake(value):
    """Wire-form keys to the stored model's field names (values untouched)."""
    if isinstance(value, dict):
        return {
            re.sub(r"(?<=[a-z0-9])([A-Z])", lambda m: "_" + m.group(1).lower(), key): _snake(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_snake(item) for item in value]
    return value


def _montage(evidence: list[dict], path: Path) -> None:
    from PIL import Image, ImageDraw  # noqa: PLC0415

    width = 1400
    rows = []
    for row in evidence:
        images = [image.convert("RGB") for image in row["images"]]
        # Tiny 96 px picture samples are enlarged; wide text crops are stacked.
        height = max(120, min(240, max(image.height for image in images)))
        tiles = [
            image.resize((max(1, round(image.width * height / image.height)), height))
            for image in images
        ]
        tiles = [tile.crop((0, 0, min(tile.width, width), height)) for tile in tiles]
        side_by_side = sum(tile.width + 6 for tile in tiles) <= width
        canvas = Image.new(
            "RGB",
            (width, 22 + (height if side_by_side else len(tiles) * (height + 6))),
            "white",
        )
        ImageDraw.Draw(canvas).text((4, 4), row["label"][:240], fill="black")
        x, y = 0, 22
        for tile in tiles:
            canvas.paste(tile, (x, y))
            if side_by_side:
                x += tile.width + 6
            else:
                y += height + 6
        rows.append(canvas)
    sheet = Image.new("RGB", (width, sum(row.height for row in rows) or 1), "white")
    y = 0
    for row in rows:
        sheet.paste(row, (0, y))
        y += row.height
    path.parent.mkdir(parents=True, exist_ok=True)
    sheet.save(path)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--status", type=Path, required=True)
    parser.add_argument("--sources", type=Path, required=True)
    parser.add_argument("--video", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--brand-tail", choices=["none", "standard"], default="none")
    parser.add_argument("--montage", type=Path)
    args = parser.parse_args()
    # Recipe models transitively construct Settings. This offline command never
    # connects to storage/DB; mirror the real-export harness's inert defaults.
    # Keep imports after parsing so --help needs neither settings nor media tools.
    os.environ.setdefault("STORAGE_BUCKET", "offline-export-check")
    os.environ.setdefault("DATABASE_URL", "postgresql://localhost/unused")
    from app.kria.device_render import DeviceRenderStatus  # noqa: PLC0415
    from app.services.actual_export_verification import verify_actual_export  # noqa: PLC0415

    raw = _snake(json.loads(args.status.read_text()))
    # Only the pinned request is verified; any retained phase replays it.
    status = DeviceRenderStatus.model_validate({"phase": "syncing", "request": raw["request"]})
    paths = json.loads(args.sources.read_text())
    if not isinstance(paths, dict) or not all(
        isinstance(key, str) and isinstance(path, str) for key, path in paths.items()
    ):
        parser.error("sources must map asset IDs to local paths")
    evidence = [] if args.montage else None
    report = verify_actual_export(
        args.video,
        status,
        {key: Path(path) for key, path in paths.items()},
        brand_tail=args.brand_tail,
        evidence=evidence,
    )
    if args.montage:
        _montage(evidence, args.montage)
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, indent=2) + "\n")
    print(
        json.dumps(
            {
                "status": report["status"],
                "checks": len(report["checks"]),
                "report": str(args.report),
            }
        )
    )
    return 0 if report["status"] == "checked" else 1


if __name__ == "__main__":
    raise SystemExit(main())
