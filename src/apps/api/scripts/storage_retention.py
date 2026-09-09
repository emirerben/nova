#!/usr/bin/env python3
"""Inspect and explicitly approve/reject a soaked storage retention manifest."""

from __future__ import annotations

import argparse
import uuid
from datetime import UTC, datetime

from sqlalchemy import select

from app.database import sync_session
from app.models import StorageRetentionEntry, StorageRetentionManifest


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("report", "approve", "reject"))
    parser.add_argument("manifest_id")
    parser.add_argument("--operator", default="")
    args = parser.parse_args()
    manifest_id = uuid.UUID(args.manifest_id)
    with sync_session() as db:
        manifest = db.get(StorageRetentionManifest, manifest_id)
        if manifest is None:
            parser.error("manifest not found")
        if args.action == "report":
            print(f"status={manifest.status} report_only_until={manifest.report_only_until}")
            print(f"summary={manifest.summary_json}")
            rows = db.scalars(
                select(StorageRetentionEntry)
                .where(StorageRetentionEntry.manifest_id == manifest_id)
                .order_by(StorageRetentionEntry.action, StorageRetentionEntry.object_path)
            ).all()
            for row in rows:
                print(
                    f"{row.action}\t{row.reason}\t{row.size_bytes}\t"
                    f"{row.object_generation}\t{row.object_path}"
                )
            return 0
        if not args.operator.strip():
            parser.error("--operator is required for approve/reject")
        if manifest.status != "pending_approval":
            parser.error("manifest is not pending approval")
        if datetime.now(UTC) < manifest.report_only_until:
            parser.error("seven-day report-only soak has not completed")
        if args.action == "approve":
            manifest.status = "approved"
            manifest.approved_at = datetime.now(UTC)
            manifest.approved_by = args.operator.strip()
        else:
            manifest.status = "rejected"
            manifest.approved_by = args.operator.strip()
        db.commit()
        print(f"manifest {manifest.id} {manifest.status}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
