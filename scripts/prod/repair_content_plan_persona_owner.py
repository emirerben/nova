#!/usr/bin/env python3
"""Forward-only repair for the one content-plan/persona ownership incident.

This is deliberately phased. ``fence`` cannot repair ownership, and ``repair``
cannot clear quarantine. The operator must replace workers, inspect Celery, and
deploy/verify 0073 between phases exactly as documented in the runbook.

The command prints counts and a one-way fingerprint only. Exact tenant, plan,
job, task, seed, and object identifiers live exclusively in the private GCS
evidence document.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select

from app.config import settings
from app.database import sync_session
from app.models import ContentPlan, Job, Persona, PlanItem
from app.storage import _get_client
from app.worker import celery_app

EVIDENCE_ROOT = "incident-evidence/content-plan-persona-owner"
OUTPUT_PREFIXES = ("generative-jobs", "dev-user", "music-jobs")


def _fingerprint(plan: ContentPlan, linked: Persona, owner: Persona) -> str:
    raw = f"{plan.id}:{plan.user_id}:{linked.id}:{linked.user_id}:{owner.id}"
    return hashlib.sha256(raw.encode()).hexdigest()[:24]


def _seed_map(persona: Persona) -> dict[str, dict[str, Any]]:
    rows = persona.idea_seeds if isinstance(persona.idea_seeds, list) else []
    return {
        str(row.get("id")): dict(row)
        for row in rows
        if isinstance(row, dict) and row.get("id")
    }


def _inventory(*, lock: bool = False) -> dict[str, Any]:
    with sync_session() as db:
        stmt = (
            select(ContentPlan)
            .join(Persona, Persona.id == ContentPlan.persona_id)
            .where(Persona.user_id != ContentPlan.user_id)
        )
        if lock:
            stmt = stmt.with_for_update()
        plans = db.execute(stmt).scalars().all()
        if len(plans) != 1:
            raise RuntimeError(f"expected exactly one mismatch, found {len(plans)}")
        plan = plans[0]
        linked = db.get(Persona, plan.persona_id)
        owners = db.execute(select(Persona).where(Persona.user_id == plan.user_id)).scalars().all()
        if linked is None or len(owners) != 1:
            raise RuntimeError("linked/destination persona cardinality changed")
        owner = owners[0]
        items = (
            db.execute(
                select(PlanItem)
                .where(PlanItem.content_plan_id == plan.id)
                .order_by(PlanItem.id)
            )
            .scalars()
            .all()
        )
        item_ids = [row.id for row in items]
        jobs = (
            db.execute(
                select(Job)
                .where(Job.content_plan_item_id.in_(item_ids))
                .order_by(Job.id)
            )
            .scalars()
            .all()
            if item_ids
            else []
        )
        linked_seeds = _seed_map(linked)
        owner_seeds = _seed_map(owner)
        seed_checks = []
        for item in items:
            seed_id = item.source_idea_seed_id
            if not seed_id:
                continue
            source = linked_seeds.get(str(seed_id))
            destination = owner_seeds.get(str(seed_id))
            seed_checks.append(
                {
                    "item_id": str(item.id),
                    "seed_id": str(seed_id),
                    "item_idea": item.idea,
                    "source_seed": source,
                    "destination_seed": destination,
                    "exact_text_match": bool(source and source.get("text") == item.idea),
                }
            )
        fingerprint = _fingerprint(plan, linked, owner)
        object_rows = []
        bucket = _get_client().bucket(settings.storage_bucket)
        for job in jobs:
            for root in OUTPUT_PREFIXES:
                prefix = f"{root}/{job.id}/"
                for blob in bucket.list_blobs(prefix=prefix):
                    object_rows.append(
                        {
                            "path": blob.name,
                            "generation": str(blob.generation),
                            "size": int(blob.size or 0),
                            "md5": blob.md5_hash,
                            "crc32c": blob.crc32c,
                        }
                    )
        return {
            "fingerprint": fingerprint,
            "captured_at": datetime.now(UTC).isoformat(),
            "plan": {
                "id": str(plan.id),
                "user_id": str(plan.user_id),
                "linked_persona_id": str(linked.id),
                "linked_persona_user_id": str(linked.user_id),
                "owner_persona_id": str(owner.id),
                "ownership_epoch": int(plan.ownership_epoch or 0),
                "quarantined": plan.ownership_quarantined_at is not None,
            },
            "items": [
                {
                    "id": str(item.id),
                    "current_job_id": str(item.current_job_id) if item.current_job_id else None,
                    "source_idea_seed_id": item.source_idea_seed_id,
                }
                for item in items
            ],
            "jobs": [
                {
                    "id": str(job.id),
                    "item_id": str(job.content_plan_item_id),
                    "status": job.status,
                    "task_id": job.celery_task_id,
                }
                for job in jobs
            ],
            "seed_checks": seed_checks,
            "objects": object_rows,
        }


def _evidence_path(snapshot: dict[str, Any]) -> str:
    return f"{EVIDENCE_ROOT}/{snapshot['fingerprint']}/snapshot.json"


def _store_snapshot(snapshot: dict[str, Any]) -> str:
    payload = json.dumps(snapshot, sort_keys=True, separators=(",", ":")).encode()
    bucket = _get_client().bucket(settings.storage_bucket)
    path = _evidence_path(snapshot)
    blob = bucket.blob(path)
    try:
        blob.upload_from_string(
            payload,
            content_type="application/json",
            if_generation_match=0,
        )
    except Exception:
        existing = bucket.blob(path).download_as_bytes()
        if existing != payload:
            raise RuntimeError("private evidence snapshot already exists with different bytes")
    return path


def _assert_fingerprint(snapshot: dict[str, Any], expected: str) -> None:
    if snapshot["fingerprint"] != expected:
        raise RuntimeError("incident fingerprint does not match the operator-provided value")


def _fence(snapshot: dict[str, Any]) -> None:
    from sqlalchemy import update

    expected_jobs = {row["id"]: row for row in snapshot["jobs"]}
    with sync_session() as db, db.begin():
        plan = db.get(ContentPlan, snapshot["plan"]["id"], with_for_update=True)
        if plan is None:
            raise RuntimeError("plan disappeared")
        persona_ids = sorted(
            [snapshot["plan"]["linked_persona_id"], snapshot["plan"]["owner_persona_id"]]
        )
        personas = (
            db.execute(select(Persona).where(Persona.id.in_(persona_ids)).order_by(Persona.id).with_for_update())
            .scalars()
            .all()
        )
        items = (
            db.execute(
                select(PlanItem)
                .where(PlanItem.content_plan_id == plan.id)
                .order_by(PlanItem.id)
                .with_for_update()
            )
            .scalars()
            .all()
        )
        jobs = (
            db.execute(
                select(Job)
                .where(Job.content_plan_item_id.in_([row.id for row in items]))
                .order_by(Job.id)
                .with_for_update()
            )
            .scalars()
            .all()
        )
        if len(personas) != 2 or {str(row.id) for row in jobs} != set(expected_jobs):
            raise RuntimeError("locked inventory changed since evidence capture")
        if any(expected_jobs[str(job.id)]["status"] != job.status for job in jobs):
            raise RuntimeError("job status changed since evidence capture")
        plan.ownership_epoch = int(plan.ownership_epoch or 0) + 1
        plan.ownership_quarantined_at = datetime.now(UTC)
        now = datetime.now(UTC)
        for job in jobs:
            result = db.execute(
                update(Job)
                .where(Job.id == job.id, Job.status == expected_jobs[str(job.id)]["status"])
                .values(
                    status="cancelled",
                    failure_reason="persona_owner_mismatch",
                    error_detail="Cancelled after an internal ownership integrity check.",
                    finished_at=job.finished_at or now,
                )
            )
            if result.rowcount != 1:
                raise RuntimeError("expected-status cancellation predicate failed")


def _archive_and_delete_outputs(snapshot: dict[str, Any]) -> int:
    bucket = _get_client().bucket(settings.storage_bucket)
    copied = 0
    for row in snapshot["objects"]:
        source = bucket.blob(row["path"], generation=int(row["generation"]))
        key = hashlib.sha256(f"{row['path']}:{row['generation']}".encode()).hexdigest()
        destination_path = f"{EVIDENCE_ROOT}/{snapshot['fingerprint']}/objects/{key}"
        destination = bucket.copy_blob(
            source,
            bucket,
            destination_path,
            if_source_generation_match=int(row["generation"]),
        )
        destination.reload()
        if int(destination.size or 0) != row["size"]:
            raise RuntimeError("evidence copy size mismatch")
        if row.get("md5") and destination.md5_hash != row["md5"]:
            raise RuntimeError("evidence copy content hash mismatch")
        if row.get("crc32c") and destination.crc32c != row["crc32c"]:
            raise RuntimeError("evidence copy checksum mismatch")
        source.delete(if_generation_match=int(row["generation"]))
        copied += 1
    return copied


def _revoke_tasks(snapshot: dict[str, Any]) -> int:
    task_ids = sorted({row["task_id"] for row in snapshot["jobs"] if row.get("task_id")})
    for task_id in task_ids:
        celery_app.control.revoke(task_id, terminate=True, signal="SIGTERM")
    return len(task_ids)


def _verify_quiescence(snapshot: dict[str, Any]) -> tuple[int, int]:
    inspect = celery_app.control.inspect(timeout=5)
    states = {
        "active": inspect.active(),
        "reserved": inspect.reserved(),
        "scheduled": inspect.scheduled(),
    }
    if any(value is None for value in states.values()):
        raise RuntimeError("Celery queue inspection is unavailable")
    needles = {
        row["id"] for row in snapshot["jobs"]
    } | {row["item_id"] for row in snapshot["jobs"]} | {
        row["task_id"] for row in snapshot["jobs"] if row.get("task_id")
    }
    matches = 0
    inspected = 0
    for workers in states.values():
        for tasks in workers.values():
            for task in tasks:
                inspected += 1
                serialized = json.dumps(task, sort_keys=True, default=str)
                if any(needle in serialized for needle in needles):
                    matches += 1
    if matches:
        raise RuntimeError("an affected task remains active, reserved, or scheduled")
    return inspected, matches


def _foreign_seed_references(db, foreign_user_id: str, seed_ids: set[str]) -> int:  # noqa: ANN001
    plans = db.execute(select(ContentPlan.id).where(ContentPlan.user_id == foreign_user_id)).scalars()
    return len(
        db.execute(
            select(PlanItem.id).where(
                PlanItem.content_plan_id.in_(list(plans)),
                PlanItem.source_idea_seed_id.in_(seed_ids),
            )
        ).scalars().all()
    )


def _repair(snapshot: dict[str, Any]) -> None:
    with sync_session() as db, db.begin():
        plan = db.get(ContentPlan, snapshot["plan"]["id"], with_for_update=True)
        if plan is None or plan.ownership_quarantined_at is None:
            raise RuntimeError("plan is not durably quarantined")
        personas = (
            db.execute(
                select(Persona)
                .where(
                    Persona.id.in_(
                        [
                            snapshot["plan"]["linked_persona_id"],
                            snapshot["plan"]["owner_persona_id"],
                        ]
                    )
                )
                .order_by(Persona.id)
                .with_for_update()
            )
            .scalars()
            .all()
        )
        by_id = {str(row.id): row for row in personas}
        linked = by_id.get(snapshot["plan"]["linked_persona_id"])
        owner = by_id.get(snapshot["plan"]["owner_persona_id"])
        if linked is None or owner is None or owner.user_id != plan.user_id:
            raise RuntimeError("destination persona invariant changed")
        items = (
            db.execute(
                select(PlanItem)
                .where(PlanItem.content_plan_id == plan.id)
                .order_by(PlanItem.id)
                .with_for_update()
            )
            .scalars()
            .all()
        )
        jobs = (
            db.execute(
                select(Job)
                .where(Job.content_plan_item_id.in_([row.id for row in items]))
                .order_by(Job.id)
                .with_for_update()
            )
            .scalars()
            .all()
        )
        if any(job.status != "cancelled" for job in jobs):
            raise RuntimeError("an affected job is no longer cancelled")
        linked_seeds = _seed_map(linked)
        owner_seeds = _seed_map(owner)
        contaminated = {str(row.source_idea_seed_id) for row in items if row.source_idea_seed_id}
        if _foreign_seed_references(db, str(linked.user_id), contaminated):
            raise RuntimeError("a foreign-owner plan still references an attributable seed")
        for item in items:
            if not item.source_idea_seed_id:
                continue
            seed_id = str(item.source_idea_seed_id)
            source = linked_seeds.get(seed_id)
            destination = owner_seeds.get(seed_id)
            if source is None or source.get("text") != item.idea:
                raise RuntimeError("attributable seed text does not exactly match its PlanItem")
            if source.get("status") not in {"pending", "in_plan"}:
                raise RuntimeError("attributable seed has an invalid status")
            if destination is not None:
                comparable_source = {**source, "status": "in_plan"}
                comparable_destination = {**destination, "status": "in_plan"}
                if comparable_source != comparable_destination:
                    raise RuntimeError("destination contains a conflicting seed value")
                destination["status"] = "in_plan"
                owner_seeds[seed_id] = destination
            else:
                owner_seeds[seed_id] = {**source, "status": "in_plan"}
            linked_seeds.pop(seed_id)
        linked.idea_seeds = list(linked_seeds.values())
        owner.idea_seeds = list(owner_seeds.values())
        bad_job_ids = {job.id for job in jobs}
        for item in items:
            if item.current_job_id in bad_job_ids:
                item.current_job_id = None
        plan.persona_id = owner.id

    with sync_session() as db:
        remaining = len(
            db.execute(
                select(ContentPlan.id)
                .join(Persona, Persona.id == ContentPlan.persona_id)
                .where(Persona.user_id != ContentPlan.user_id)
            )
            .scalars()
            .all()
        )
    if remaining != 0:
        raise RuntimeError("global mismatch audit is not zero after repair")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("phase", choices=("audit", "fence", "verify", "repair"))
    parser.add_argument("--fingerprint")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    snapshot = _inventory()
    exact_matches = sum(row["exact_text_match"] for row in snapshot["seed_checks"])
    print(
        "AUDIT "
        f"fingerprint={snapshot['fingerprint']} plans=1 items={len(snapshot['items'])} "
        f"jobs={len(snapshot['jobs'])} seeds={len(snapshot['seed_checks'])} "
        f"exact_seed_matches={exact_matches} objects={len(snapshot['objects'])}"
    )
    if args.phase == "audit":
        return 0
    if not args.apply or not args.fingerprint:
        raise RuntimeError("mutating phases require --apply and --fingerprint")
    _assert_fingerprint(snapshot, args.fingerprint)
    if args.phase == "fence":
        evidence = _store_snapshot(snapshot)
        _fence(snapshot)
        copied = _archive_and_delete_outputs(snapshot)
        revoked = _revoke_tasks(snapshot)
        print(
            f"FENCED evidence={evidence} archived_objects={copied} revoked_tasks={revoked} "
            "next=replace_workers_and_verify_queues"
        )
        return 0
    if args.phase == "verify":
        inspected, matches = _verify_quiescence(snapshot)
        archived_late = _archive_and_delete_outputs(snapshot) if snapshot["objects"] else 0
        if _inventory()["objects"]:
            raise RuntimeError("an affected output remains after the second cleanup pass")
        print(
            f"QUIESCENT inspected_tasks={inspected} matched_tasks={matches} "
            f"archived_late_objects={archived_late}"
        )
        return 0
    _repair(snapshot)
    print("REPAIRED mismatch_count=0 quarantine=retained next=deploy_0073")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:  # identifiers and user content must never reach stdout/stderr
        print(f"ERROR type={type(exc).__name__}", file=sys.stderr)
        raise SystemExit(1) from None
