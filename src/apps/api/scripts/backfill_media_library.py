"""Catalogue one account's retained uploads. Default: metadata-only dry run.

Run from src/apps/api: python -m scripts.backfill_media_library --email EMAIL
Add --apply after reviewing the report. No uploads, deletions or AI dispatch.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import uuid
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from fastapi import HTTPException
from google.api_core.exceptions import NotFound
from sqlalchemy import func, select

from app import storage
from app.database import AsyncSessionLocal
from app.kria.media_sources import MediaUploadContract, is_analysis_proxy_path
from app.models import (
    ContentPlan,
    CreationThread,
    CreationThreadEvent,
    MediaAsset,
    MediaAssetSource,
    PlanItem,
    PlanItemAsset,
    User,
)
from app.services import media_library_ingest as ingest
from app.services.job_storage_paths import normalize_job_storage_path


@dataclass
class Candidate:
    item_id: uuid.UUID
    threads: tuple[uuid.UUID, ...]
    path: str
    generation: str | None
    kind: str
    filename: str
    contract: dict
    error: str | None = None
    uploaded_at: datetime | None = None

    def __post_init__(self):
        try:
            self.contract = MediaUploadContract.model_validate(self.contract).model_dump(
                mode="json"
            )
        except ValueError:
            self.error = self.error or "invalid_receipt"

    @property
    def identity(self):
        return (self.path, self.generation, self.kind, json.dumps(self.contract, sort_keys=True))


async def candidates(db, owner, item_ids=None):
    query = select(PlanItem).join(ContentPlan).where(ContentPlan.user_id == owner)
    if item_ids is not None:
        query = query.where(PlanItem.id.in_(item_ids))
    items = (
        await db.scalars(
            query.order_by(PlanItem.updated_at.desc(), PlanItem.id).execution_options(
                populate_existing=True
            )
        )
    ).all()
    ids = [item.id for item in items]
    threads = (
        await db.scalars(
            select(CreationThread)
            .where(CreationThread.creator_id == owner, CreationThread.active_plan_item_id.in_(ids))
            .execution_options(populate_existing=True)
        )
    ).all()
    by_item = {t.active_plan_item_id: t for t in threads}
    upload_times = {}
    events = await db.execute(
        select(
            CreationThreadEvent.thread_id,
            CreationThreadEvent.payload["media"],
            CreationThreadEvent.created_at,
        )
        .where(
            CreationThreadEvent.thread_id.in_([t.id for t in threads]),
            CreationThreadEvent.event_type == "media_added",
        )
        .order_by(CreationThreadEvent.created_at)
    )
    for thread_id, media_rows, created_at in events:
        for media_row in media_rows if isinstance(media_rows, list) else []:
            if isinstance(media_row, dict):
                upload_times[thread_id, media_row.get("media_id")] = created_at
    result = []
    for item in items:
        thread = by_item.get(item.id)
        media = (
            [m for m in (thread.state or {}).get("media", []) if isinstance(m, dict)]
            if thread
            else []
        )
        by_id = {m.get("media_id"): m for m in media}
        thread_ids = (thread.id,) if thread else ()
        for row in item.clip_assignments or []:
            if not isinstance(row, dict) or not isinstance(row.get("gcs_path"), str):
                continue
            path = row["gcs_path"]
            matching = by_id.get(row.get("media_id") or path.rsplit("/", 1)[-1], {})
            contract = row.get("upload_contract") or matching.get("upload_contract") or {}
            generation = row.get("storage_generation") or matching.get("generation")
            try:
                conflict = (
                    row.get("upload_contract")
                    and matching.get("upload_contract")
                    and MediaUploadContract.model_validate(row["upload_contract"])
                    != MediaUploadContract.model_validate(matching["upload_contract"])
                )
            except ValueError:
                conflict = True
            if (
                row.get("storage_generation")
                and matching.get("generation")
                and str(row["storage_generation"]) != str(matching["generation"])
            ):
                conflict = True
            result.append(
                Candidate(
                    item.id,
                    thread_ids,
                    path,
                    str(generation) if generation else None,
                    "video",
                    row.get("source_filename") or matching.get("filename") or Path(path).name,
                    contract,
                    "receipt_conflict" if conflict else None,
                    uploaded_at=upload_times.get(
                        (thread.id, matching.get("media_id")), item.created_at
                    )
                    if thread
                    else item.created_at,
                )
            )
        for role, path, generation, filename in (
            ("song", item.song_gcs_path, item.song_generation, item.song_filename),
            ("voiceover", item.voiceover_gcs_path, item.voiceover_generation, None),
        ):
            if not path:
                continue
            matching = next((m for m in media if m.get("media_id") == Path(path).name), {})
            result.append(
                Candidate(
                    item.id,
                    thread_ids,
                    path,
                    str(generation) if generation else None,
                    "audio",
                    filename or matching.get("filename") or Path(path).name,
                    matching.get("upload_contract") or {},
                    uploaded_at=upload_times.get(
                        (thread.id, matching.get("media_id")), item.created_at
                    )
                    if thread
                    else item.created_at,
                )
            )
    pool = (
        await db.scalars(
            select(PlanItemAsset)
            .where(PlanItemAsset.plan_item_id.in_(ids), PlanItemAsset.user_id == owner)
            .order_by(PlanItemAsset.created_at.desc())
            .execution_options(populate_existing=True)
        )
    ).all()
    for row in pool:
        thread = by_item.get(row.plan_item_id)
        result.append(
            Candidate(
                row.plan_item_id,
                (thread.id,) if thread else (),
                row.gcs_path,
                str(row.gcs_generation) if row.gcs_generation else None,
                row.kind,
                row.source_filename or Path(row.gcs_path).name,
                {},
                None
                if row.status == "ready" or (row.status == "failed" and row.media_status == "ready")
                else "not_ready",
                uploaded_at=row.created_at,
            )
        )
    return result


def validate(candidate, owner):
    if candidate.error:
        return candidate.error
    if candidate.kind not in {"video", "image", "audio"}:
        return "unsupported_kind"
    path = normalize_job_storage_path(candidate.path)
    if path != candidate.path or not path.startswith(f"users/{owner}/"):
        return "ownership_unproven"
    try:
        contract = MediaUploadContract.model_validate(candidate.contract)
    except ValueError:
        return "invalid_receipt"
    if is_analysis_proxy_path(path) and contract.proxy is None:
        return "proxy_receipt_missing"
    if candidate.kind == "video" and contract.proxy is None:
        # The shipped phone-only creation route cannot dispatch cloud footage.
        # Do not publish a source that can be selected but cannot render a post.
        return "native_receipt_missing"
    if contract.proxy and contract.proxy.original.kind != candidate.kind:
        return "invalid_receipt"
    if contract.proxy and candidate.kind != "video":
        return "unsupported_audio_proxy"
    return None


async def run(owner: uuid.UUID, *, apply=False, max_bytes=1024**3, max_candidates=5000):
    report = {
        "owner_id": str(owner),
        "mode": "apply" if apply else "dry_run",
        "counts": Counter(),
        "verified_bytes": 0,
        "attempted_download_bytes": 0,
        "eligible_bytes": 0,
        "entries": [],
    }
    async with AsyncSessionLocal() as db:
        if await db.get(User, owner) is None:
            raise ValueError("Account not found")
        snapshot = await candidates(db, owner)
    groups = defaultdict(list)

    def record(candidate, status, **extra):
        report["counts"][status] += 1
        report["entries"].append(
            {
                "filename": candidate.filename,
                "kind": candidate.kind,
                "path": candidate.path,
                "status": status,
                **extra,
            }
        )

    for candidate in sorted(
        snapshot,
        key=lambda c: (c.uploaded_at or datetime.min.replace(tzinfo=UTC), c.path),
        reverse=True,
    ):
        if error := validate(candidate, owner):
            record(candidate, error)
        else:
            groups[candidate.identity].append(candidate)
    if len(groups) > max_candidates:
        report["counts"]["candidate_limit"] = len(groups) - max_candidates
    selected = list(groups.values())[:max_candidates]
    # Metadata reads are bounded and do not hold owner locks or download media.
    semaphore = asyncio.Semaphore(8)

    async def inspect(group):
        candidate = group[0]
        async with semaphore:
            try:
                metadata = await asyncio.to_thread(storage.object_metadata, candidate.path)
            except (FileNotFoundError, NotFound):
                return group, None, "missing"
            except Exception:
                return group, None, "storage_unavailable"
        if candidate.generation and str(metadata.generation) != candidate.generation:
            return group, None, "generation_changed"
        try:
            ingest.validate_upload_request(
                {
                    "kind": candidate.kind,
                    "content_type": metadata.content_type,
                    "file_size_bytes": metadata.size,
                    "upload_contract": candidate.contract,
                },
                legacy=True,
            )
        except HTTPException:
            return group, None, "unsupported_media"
        return group, metadata, None

    prepared = await asyncio.gather(*(inspect(group) for group in selected))
    signatures = defaultdict(set)
    for group, metadata, error in prepared:
        if not error:
            signatures[group[0].path, str(metadata.generation)].add(group[0].identity[2:])
    for position, (group, metadata, error) in enumerate(prepared):
        candidate = group[0]
        if error:
            record(candidate, error)
            continue
        generation = str(metadata.generation)
        if len(signatures[candidate.path, generation]) > 1:
            record(candidate, "receipt_conflict")
            continue
        report["eligible_bytes"] += metadata.size
        if not apply:
            record(candidate, "eligible", byte_count=metadata.size, generation=generation)
            continue
        async with AsyncSessionLocal() as db:
            try:
                await ingest.lock_library_owner(db, owner)
                current = await candidates(db, owner, {c.item_id for c in group})
                live = [
                    c
                    for c in current
                    if not validate(c, owner) and c.identity == candidate.identity
                ]
                if not live:
                    record(candidate, "consumer_changed")
                    continue
                latest = await asyncio.to_thread(storage.object_metadata, candidate.path)
                if str(latest.generation) != generation or latest.size != metadata.size:
                    record(candidate, "generation_changed")
                    continue
                known = (
                    await db.execute(
                        select(MediaAsset, MediaAssetSource)
                        .join(MediaAssetSource, MediaAssetSource.asset_id == MediaAsset.id)
                        .where(
                            MediaAsset.user_id == owner,
                            MediaAssetSource.user_id == owner,
                            MediaAssetSource.object_path == candidate.path,
                            MediaAssetSource.object_generation == generation,
                        )
                    )
                ).first()
                if known and known[0].archived_at is not None:
                    record(candidate, "archived")
                    continue
                contract = MediaUploadContract.model_validate(candidate.contract)
                original = contract.proxy.original if contract.proxy else None
                if known:
                    asset, source = known
                    if (
                        source.status != "available"
                        or source.byte_count != metadata.size
                        or asset.kind != candidate.kind
                        or source.role != ("analysis_proxy" if original else "original")
                        or asset.original_declared_sha256 != (original.sha256 if original else None)
                        or asset.original_byte_count != (original.byte_count if original else None)
                    ):
                        record(candidate, "existing_source_conflict")
                        continue
                    outcome = "already_catalogued"
                else:
                    if report["attempted_download_bytes"] + metadata.size > max_bytes:
                        record(candidate, "byte_budget")
                        report["counts"]["not_scanned_after_byte_budget"] = (
                            len(prepared) - position - 1
                        )
                        break
                    payload = {
                        "kind": candidate.kind,
                        "content_type": metadata.content_type,
                        "file_size_bytes": metadata.size,
                        "upload_contract": candidate.contract,
                    }
                    report["attempted_download_bytes"] += metadata.size
                    proof = await asyncio.to_thread(
                        ingest.verified_source_metadata,
                        candidate.path,
                        generation,
                        candidate.kind,
                        payload,
                        legacy=True,
                    )
                    report["verified_bytes"] += metadata.size
                    archived = await db.scalar(
                        select(MediaAsset.id)
                        .join(MediaAssetSource, MediaAssetSource.asset_id == MediaAsset.id)
                        .where(
                            MediaAsset.user_id == owner,
                            MediaAssetSource.user_id == owner,
                            MediaAsset.archived_at.is_not(None),
                            MediaAsset.kind == candidate.kind,
                            MediaAssetSource.uploaded_sha256 == proof["uploaded_sha256"],
                            MediaAssetSource.role == proof["role"],
                            MediaAsset.original_declared_sha256.is_not_distinct_from(
                                original.sha256 if original else None
                            ),
                            MediaAsset.original_byte_count.is_not_distinct_from(
                                original.byte_count if original else None
                            ),
                        )
                        .limit(1)
                    )
                    if archived:
                        record(candidate, "archived")
                        continue
                    existing_ids = set(
                        (
                            await db.scalars(
                                select(MediaAsset.id).where(MediaAsset.user_id == owner)
                            )
                        ).all()
                    )
                    asset, source = await ingest.register_verified_source(
                        db,
                        owner_id=owner,
                        kind=candidate.kind,
                        object_path=candidate.path,
                        generation=generation,
                        uploaded_sha256=proof["uploaded_sha256"],
                        byte_count=proof["byte_count"],
                        content_type=proof["content_type"],
                        role=proof["role"],
                        original_declared_sha256=original.sha256 if original else None,
                        original_byte_count=original.byte_count if original else None,
                        metadata={
                            **proof["metadata"],
                            "filename": candidate.filename,
                            "upload_contract": candidate.contract,
                            "backfill": {"analysis": "not_requested"},
                        },
                    )
                    outcome = "reused" if asset.id in existing_ids else "created"
                    if outcome == "created":
                        # Ready to reuse the verified bytes. Facts remain absent; do not leave
                        # pending work that the watchdog would turn into unsolicited AI calls.
                        asset.status = "ready"
                        asset.created_at = max(c.uploaded_at for c in live if c.uploaded_at)
                for consumer in live:
                    await ingest.reference_library_source(
                        db,
                        owner_id=owner,
                        asset_id=asset.id,
                        source_id=source.id,
                        plan_item_id=consumer.item_id,
                    )
                    for thread_id in consumer.threads:
                        await ingest.reference_library_source(
                            db,
                            owner_id=owner,
                            asset_id=asset.id,
                            source_id=source.id,
                            creation_thread_id=thread_id,
                        )
                await db.commit()
                record(candidate, outcome, asset_id=str(asset.id), source_id=str(source.id))
            except HTTPException as exc:
                detail = exc.detail
                record(
                    candidate,
                    detail.get("code", "verification_failed")
                    if isinstance(detail, dict)
                    else "verification_failed",
                )
                await db.rollback()
                if isinstance(detail, dict) and detail.get("code") == "library_quota":
                    report["counts"]["not_imported_at_capacity"] = len(prepared) - position - 1
                    break
            except Exception as exc:
                record(candidate, "failed", error_type=type(exc).__name__)
                await db.rollback()
    async with AsyncSessionLocal() as db:
        report["active_count"] = await db.scalar(
            select(func.count())
            .select_from(MediaAsset)
            .where(MediaAsset.user_id == owner, MediaAsset.archived_at.is_(None))
        )
    report["counts"] = dict(report["counts"])
    return report


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--email", required=True)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--max-bytes", type=int, default=1024**3)
    parser.add_argument("--max-candidates", type=int, default=5000)
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    if args.max_bytes < 1 or not 1 <= args.max_candidates <= 10000:
        parser.error("Positive byte budget and 1..10000 candidates required")
    async with AsyncSessionLocal() as db:
        owner = await db.scalar(select(User.id).where(func.lower(User.email) == args.email.lower()))
    if owner is None:
        raise SystemExit("Account not found")
    report = await run(
        owner, apply=args.apply, max_bytes=args.max_bytes, max_candidates=args.max_candidates
    )
    if args.report:
        args.report.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps({k: v for k, v in report.items() if k != "entries"}))
    return 1 if report["counts"].get("failed") else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
