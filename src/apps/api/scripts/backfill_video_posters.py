"""Progressively backfill JPEG posters for retained browser-visible videos.

Run from ``src/apps/api``::

    python -m scripts.backfill_video_posters --dry-run
    python -m scripts.backfill_video_posters --batch-size 25 --limit 100

The script is deliberately idempotent and safe to run more than once. It
discovers all output shapes used by the library (generative variants,
single-output jobs, and ready ``JobClip`` rows), downloads one source at a
time, and writes a deterministic ``<video>.poster.jpg`` sibling. A row-lock
and source-key recheck prevents a poster from an old render being attached to
a newer render. A stale-race poster is retained because the deterministic
sibling may already be referenced by the winning writer; lifecycle/maintenance
cleanup can remove unreferenced siblings later. Missing objects and malformed
legacy URLs are counted and do not stop the remaining batch.
"""

from __future__ import annotations

import argparse
import os
import sys
import tempfile
import uuid
from collections import Counter
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import and_, or_, select

from app.database import sync_session
from app.models import Job, JobClip
from app.routes.me import _owned_job_output_path
from app.services.template_poster import (
    PosterExtractionError,
    extract_poster_bytes,
    poster_object_path,
)
from app.storage import (
    delete_object_best_effort,
    download_to_file,
    upload_bytes_public_read,
)


@dataclass(frozen=True)
class PosterCandidate:
    job_id: uuid.UUID
    kind: str
    source_key: str | None
    poster_field: str
    variant_id: str | None = None
    variant_index: int | None = None
    render_identity: object = None
    clip_id: uuid.UUID | None = None
    already_present: bool = False
    blocked_outcome: str | None = None


OUTCOMES = (
    "already_present",
    "generated",
    "expired_source",
    "unresolvable_legacy_url",
    "stale_race",
    "failed",
    "skipped_not_owned",
)


def _is_missing_object(exc: BaseException) -> bool:
    text = f"{type(exc).__name__}: {exc}".lower()
    return any(
        marker in text
        for marker in (
            "notfound",
            "not found",
            "no such object",
            "status code: 404",
            "404",
        )
    )


def _source_candidate(job: Job, raw_source: object) -> tuple[str | None, str | None]:
    """Return an owned key or the disjoint outcome for an untrusted source."""
    source_key = _owned_job_output_path(raw_source, job)
    if source_key:
        return source_key, None
    if isinstance(raw_source, str) and "://" in raw_source:
        return None, "unresolvable_legacy_url"
    return None, "skipped_not_owned"


def _make_candidate(
    job: Job,
    *,
    kind: str,
    raw_source: object,
    poster_value: object,
    poster_field: str,
    variant_id: str | None = None,
    variant_index: int | None = None,
    render_identity: object = None,
    clip_id: uuid.UUID | None = None,
) -> PosterCandidate | None:
    if not isinstance(raw_source, str) or not raw_source.strip():
        return None
    source_key, blocked_outcome = _source_candidate(job, raw_source)
    if blocked_outcome:
        return PosterCandidate(
            job_id=job.id,
            kind=kind,
            source_key=None,
            poster_field=poster_field,
            variant_id=variant_id,
            variant_index=variant_index,
            render_identity=render_identity,
            clip_id=clip_id,
            blocked_outcome=blocked_outcome,
        )
    return PosterCandidate(
        job_id=job.id,
        kind=kind,
        source_key=source_key,
        poster_field=poster_field,
        variant_id=variant_id,
        variant_index=variant_index,
        render_identity=render_identity,
        clip_id=clip_id,
        already_present=bool(poster_value),
    )


def _candidates_for_job(
    job: Job,
    clips: list[JobClip],
    *,
    include_preview_bases: bool,
) -> Iterator[PosterCandidate]:
    if job.status == "cancelled":
        return
    plan = job.assembly_plan if isinstance(job.assembly_plan, dict) else {}
    variants = plan.get("variants")
    if isinstance(variants, list):
        for index, variant in enumerate(variants):
            if not isinstance(variant, dict):
                continue
            if variant.get("render_status") != "ready":
                continue
            variant_id = str(variant.get("variant_id") or "") or None
            video_source = variant.get("video_path") or variant.get("output_url")
            candidate = _make_candidate(
                job,
                kind="variant",
                raw_source=video_source,
                poster_value=variant.get("poster_path"),
                poster_field="poster_path",
                variant_id=variant_id,
                variant_index=index,
                render_identity=(
                    variant.get("render_generation_id") or variant.get("render_finished_at")
                ),
            )
            if candidate:
                yield candidate
            if not include_preview_bases:
                continue
            for video_field, poster_field, kind in (
                ("base_video_path", "base_poster_path", "variant_base"),
                (
                    "pre_media_overlay_video_path",
                    "pre_overlay_poster_path",
                    "variant_pre_overlay",
                ),
            ):
                candidate = _make_candidate(
                    job,
                    kind=kind,
                    raw_source=variant.get(video_field),
                    poster_value=variant.get(poster_field),
                    poster_field=poster_field,
                    variant_id=variant_id,
                    variant_index=index,
                    render_identity=(
                        variant.get("render_generation_id") or variant.get("render_finished_at")
                    ),
                )
                if candidate:
                    yield candidate

    output_source = plan.get("output_path") or plan.get("video_path") or plan.get("output_url")
    candidate = _make_candidate(
        job,
        kind="job_output",
        raw_source=output_source,
        poster_value=plan.get("poster_path"),
        poster_field="poster_path",
    )
    if candidate:
        yield candidate

    for clip in clips:
        if clip.render_status != "ready":
            continue
        candidate = _make_candidate(
            job,
            kind="job_clip",
            raw_source=clip.video_path,
            poster_value=clip.thumbnail_path,
            poster_field="thumbnail_path",
            clip_id=clip.id,
        )
        if candidate:
            yield candidate


def _job_batches(
    db: Any,
    *,
    batch_size: int,
    job_id: uuid.UUID | None,
    mode: str | None,
    cursor: tuple[datetime, uuid.UUID] | None,
) -> Iterator[list[Job]]:
    if job_id is not None:
        job = db.get(Job, job_id)
        if job is not None:
            yield [job]
        return

    current_cursor = cursor
    while True:
        conditions = []
        if mode:
            conditions.append(or_(Job.mode == mode, Job.job_type == mode))
        if current_cursor is not None:
            created_at, last_id = current_cursor
            conditions.append(
                or_(
                    Job.created_at > created_at,
                    and_(Job.created_at == created_at, Job.id > last_id),
                )
            )
        statement = select(Job).order_by(Job.created_at.asc(), Job.id.asc()).limit(batch_size)
        if conditions:
            statement = statement.where(*conditions)
        rows = list(db.execute(statement).scalars().all())
        if not rows:
            return
        yield rows
        last = rows[-1]
        current_cursor = (last.created_at, last.id)


def _load_ready_clips(db: Any, jobs: list[Job]) -> dict[uuid.UUID, list[JobClip]]:
    if not jobs:
        return {}
    rows = list(
        db.execute(
            select(JobClip)
            .where(
                JobClip.job_id.in_([job.id for job in jobs]),
                JobClip.render_status == "ready",
            )
            .order_by(JobClip.job_id.asc(), JobClip.rank.asc(), JobClip.created_at.asc())
        )
        .scalars()
        .all()
    )
    by_job: dict[uuid.UUID, list[JobClip]] = {}
    for clip in rows:
        by_job.setdefault(clip.job_id, []).append(clip)
    return by_job


def _generate_poster(source_key: str) -> tuple[str | None, str]:
    poster_key = poster_object_path(source_key)
    with tempfile.TemporaryDirectory(prefix="nova_poster_backfill_") as tmpdir:
        local_path = os.path.join(tmpdir, "source.mp4")
        try:
            download_to_file(source_key, local_path)
        except Exception as exc:  # noqa: BLE001 — one missing object must not stop the batch
            return None, "expired_source" if _is_missing_object(exc) else "failed"
        try:
            poster_bytes = extract_poster_bytes(local_path)
            upload_bytes_public_read(poster_bytes, poster_key, content_type="image/jpeg")
        except PosterExtractionError:
            return None, "failed"
        except Exception as exc:  # noqa: BLE001 — continue after GCS/FFmpeg failures
            return None, "expired_source" if _is_missing_object(exc) else "failed"
    return poster_key, "generated"


def _variant_for_candidate(
    plan: dict[str, Any], candidate: PosterCandidate
) -> dict[str, Any] | None:
    variants = plan.get("variants")
    if not isinstance(variants, list):
        return None
    if candidate.variant_id:
        for variant in variants:
            if (
                isinstance(variant, dict)
                and str(variant.get("variant_id") or "") == candidate.variant_id
            ):
                return variant
    if candidate.variant_index is not None and 0 <= candidate.variant_index < len(variants):
        variant = variants[candidate.variant_index]
        return variant if isinstance(variant, dict) else None
    return None


def _persist_poster(candidate: PosterCandidate, poster_key: str) -> str:
    """Attach a generated key only if the source is unchanged under a row lock."""
    with sync_session() as db:
        job = db.get(Job, candidate.job_id, with_for_update=True)
        if job is None or job.status == "cancelled":
            return "stale_race"
        plan = dict(job.assembly_plan or {}) if isinstance(job.assembly_plan, dict) else {}

        if candidate.kind == "job_clip":
            clip = db.get(JobClip, candidate.clip_id, with_for_update=True)
            if clip is None or clip.render_status != "ready":
                return "stale_race"
            current_source = _owned_job_output_path(clip.video_path, job)
            if current_source != candidate.source_key or clip.thumbnail_path:
                return "stale_race"
            clip.thumbnail_path = poster_key
        elif candidate.kind == "job_output":
            current_source = _owned_job_output_path(
                plan.get("output_path") or plan.get("video_path") or plan.get("output_url"),
                job,
            )
            if current_source != candidate.source_key or plan.get("poster_path"):
                return "stale_race"
            plan["poster_path"] = poster_key
            job.assembly_plan = plan
        else:
            variant = _variant_for_candidate(plan, candidate)
            if variant is None or variant.get("render_status") != "ready":
                return "stale_race"
            source_field = {
                "variant": "video_path",
                "variant_base": "base_video_path",
                "variant_pre_overlay": "pre_media_overlay_video_path",
            }.get(candidate.kind)
            if source_field is None:
                return "stale_race"
            current_raw_source = (
                variant.get("video_path") or variant.get("output_url")
                if candidate.kind == "variant"
                else variant.get(source_field)
            )
            current_source = _owned_job_output_path(current_raw_source, job)
            current_identity = variant.get("render_generation_id") or variant.get(
                "render_finished_at"
            )
            if (
                current_source != candidate.source_key
                or variant.get(candidate.poster_field)
                or current_identity != candidate.render_identity
            ):
                return "stale_race"
            variant[candidate.poster_field] = poster_key
            job.assembly_plan = plan

        db.commit()
    return "generated"


def _parse_cursor(value: str | None) -> tuple[datetime, uuid.UUID] | None:
    if not value:
        return None
    try:
        created_at_raw, job_id_raw = value.split("|", 1)
        created_at = datetime.fromisoformat(created_at_raw.replace("Z", "+00:00"))
        if created_at.tzinfo is None:
            created_at = created_at.replace(tzinfo=UTC)
        return created_at, uuid.UUID(job_id_raw)
    except (TypeError, ValueError) as exc:
        raise ValueError("cursor must be '<ISO timestamp>|<job UUID>'") from exc


def _cursor_for(job: Job) -> str:
    created_at = job.created_at
    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=UTC)
    return f"{created_at.isoformat()}|{job.id}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run", action="store_true", help="Discover only; do not touch GCS or DB."
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=0,
        help="Maximum missing posters to process (0 = no cap).",
    )
    parser.add_argument("--batch-size", type=int, default=25, help="Jobs fetched per keyset page.")
    parser.add_argument("--job-id", type=uuid.UUID, help="Process one job only.")
    parser.add_argument(
        "--mode", help="Filter by Job.mode or Job.job_type (for example: generative)."
    )
    parser.add_argument(
        "--include-preview-bases",
        action="store_true",
        help="Also backfill generative base and pre-overlay posters.",
    )
    parser.add_argument(
        "--cursor",
        help=(
            "Resume after '<ISO timestamp>|<job UUID>'; the final cursor is printed for operators."
        ),
    )
    args = parser.parse_args(argv)
    if args.limit < 0 or args.batch_size < 1:
        parser.error("--limit must be >= 0 and --batch-size must be >= 1")
    try:
        cursor = _parse_cursor(args.cursor)
    except ValueError as exc:
        parser.error(str(exc))

    counts: Counter[str] = Counter()
    processed = 0
    last_cursor: str | None = None
    stop = False

    with sync_session() as db:
        for jobs in _job_batches(
            db,
            batch_size=args.batch_size,
            job_id=args.job_id,
            mode=args.mode,
            cursor=cursor,
        ):
            clips_by_job = _load_ready_clips(db, jobs)
            for job in jobs:
                for candidate in _candidates_for_job(
                    job,
                    clips_by_job.get(job.id, []),
                    include_preview_bases=args.include_preview_bases,
                ):
                    if candidate.blocked_outcome:
                        counts[candidate.blocked_outcome] += 1
                        continue
                    if candidate.already_present:
                        counts["already_present"] += 1
                        continue
                    if args.limit and processed >= args.limit:
                        stop = True
                        break
                    processed += 1
                    assert candidate.source_key is not None
                    if args.dry_run:
                        counts["generated"] += 1
                        print(
                            f"[dry-run] {candidate.job_id} {candidate.kind} "
                            f"{candidate.source_key} -> {poster_object_path(candidate.source_key)}",
                            flush=True,
                        )
                        continue
                    poster_key, outcome = _generate_poster(candidate.source_key)
                    if poster_key is None:
                        counts[outcome] += 1
                        continue
                    persisted = _persist_poster(candidate, poster_key)
                    if persisted != "generated" and persisted != "stale_race":
                        delete_object_best_effort(poster_key)
                    elif persisted == "stale_race":
                        # The poster key is deterministic. A concurrent writer
                        # may have attached this exact object after discovery;
                        # deleting it here could remove the winner's live
                        # reference. Leave it for lifecycle/maintenance cleanup.
                        print(
                            f"[stale_race] retained shared poster {poster_key}",
                            flush=True,
                        )
                    counts[persisted] += 1
                    print(
                        f"[{persisted}] {candidate.job_id} {candidate.kind} {candidate.source_key}",
                        flush=True,
                    )
                if stop:
                    break
                # Only advance the resume cursor after the entire job has been
                # scanned. If --limit stops in the middle of a job, replaying
                # that job is safe; skipping it could strand another candidate.
                last_cursor = _cursor_for(job)
            if stop or args.job_id is not None:
                break
            # Release the read transaction before downloading the next batch;
            # poster extraction can take seconds per object.
            db.rollback()

    print("Backfill complete:", " ".join(f"{name}={counts[name]}" for name in OUTCOMES))
    if last_cursor and args.job_id is None:
        print(f"resume_cursor={last_cursor}")
    return 0 if counts["failed"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
