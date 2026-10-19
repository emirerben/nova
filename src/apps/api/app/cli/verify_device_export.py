"""Repeat the KRI-734 actual-export checks without publishing or calling models.

python -m app.cli.verify_device_export --status request-status.json \
    --sources local-source-paths.json --video export.mp4 --report report.json \
    --brand-tail standard

Inputs are private evidence captured from the server's pinned request/receipts.
The sources file maps recipe asset IDs to downloaded pinned analysis proxies.
This local command cannot authenticate those operator-supplied input files;
production completion resolves/fences the receipts itself.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--status", type=Path, required=True)
    parser.add_argument("--sources", type=Path, required=True)
    parser.add_argument("--video", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--brand-tail", choices=["none", "standard"], default="none")
    args = parser.parse_args()
    # Recipe models transitively construct Settings. This offline command never
    # connects to storage/DB; mirror the real-export harness's inert defaults.
    # Keep imports after parsing so --help needs neither settings nor media tools.
    os.environ.setdefault("STORAGE_BUCKET", "offline-export-check")
    os.environ.setdefault("DATABASE_URL", "postgresql://localhost/unused")
    from app.kria.device_render import DeviceRenderStatus  # noqa: PLC0415
    from app.services.actual_export_verification import verify_actual_export  # noqa: PLC0415

    status = DeviceRenderStatus.model_validate_json(args.status.read_text())
    paths = json.loads(args.sources.read_text())
    if not isinstance(paths, dict) or not all(
        isinstance(key, str) and isinstance(path, str) for key, path in paths.items()
    ):
        parser.error("sources must map asset IDs to local paths")
    report = verify_actual_export(
        args.video,
        status,
        {key: Path(path) for key, path in paths.items()},
        brand_tail=args.brand_tail,
    )
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
