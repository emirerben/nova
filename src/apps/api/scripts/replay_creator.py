"""Run a local, same-input Creator Agent replay.

This is intentionally a driver rather than a product route.  It imports the
captured source bytes into a fresh local namespace, seeds the isolated replay
database, calls the real Creator controllers, and writes an auditable receipt.
The default run stops after Main Creator planning.  ``--render`` is an explicit
opt-in to proposal generation, queue dispatch, and worker rendering.

The module has no application imports at module load time.  That matters for
the safety checks: a stale shell with production environment variables must be
rejected before SQLAlchemy, storage, or Celery can be initialized.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import mimetypes
import os
import re
import subprocess
import sys
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

DEFAULT_MANIFEST = Path("/private/tmp/kria-prompt-replay/source-manifest.json")
DEFAULT_JOB_DEBUG = Path("/tmp/kria-prompt-job-debug.json")
DEFAULT_ITEM_DEBUG = Path("/tmp/kria-prompt-item-debug.json")
DEFAULT_DATABASE_URL = "postgresql://postgres@localhost:5432/nova_creator_fidelity_test"
DEFAULT_REDIS_URL = "redis://localhost:6379/14"
DEFAULT_OUTPUT_ROOT = Path("/private/tmp/nova-creator-fidelity-runs")
EXPECTED_SOURCE_COUNT = 40
EXPECTED_VISUAL_COUNT = 39
LOCAL_BUCKET = "creator-fidelity-local"
TERMINAL_JOB_STATUSES = frozenset(
    {
        "done",
        "failed",
        "processing_failed",
        "variants_ready",
        "variants_ready_partial",
    }
)
SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,159}$")


class ReplaySafetyError(RuntimeError):
    """Raised when a replay could escape the local-only boundary."""


@dataclass(frozen=True)
class ReplayInputs:
    manifest: dict[str, Any]
    job_debug: dict[str, Any]
    item_debug: dict[str, Any]
    sources: list[dict[str, Any]]
    seed_description: str
    source_identity_hash: str
    manifest_hash: str
    clip_analysis_cache: dict[str, dict[str, Any]]
    transcript_cache: dict[str, Any]


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_json(value: Any) -> str:
    return sha256_bytes(_canonical_json(value).encode("utf-8"))


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ReplaySafetyError(f"Cannot read JSON input {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ReplaySafetyError(f"JSON input must be an object: {path}")
    return value


def _safe_component(value: object, *, field: str) -> str:
    candidate = str(value or "").strip()
    if not SAFE_ID.fullmatch(candidate) or candidate in {".", ".."}:
        raise ReplaySafetyError(f"Unsafe {field}: {candidate!r}")
    return candidate


def _safe_filename(value: object, *, field: str) -> str:
    candidate = str(value or "").strip()
    name = Path(candidate).name
    if not candidate or name != candidate or name in {".", ".."}:
        raise ReplaySafetyError(f"Unsafe {field}: {candidate!r}")
    name = re.sub(r"[^A-Za-z0-9._-]+", "_", name)
    if not name or len(name) > 200:
        raise ReplaySafetyError(f"Unsafe {field}: {candidate!r}")
    return name


def _database_target(database_url: str) -> tuple[str, str]:
    parsed = urlsplit(database_url.replace("postgresql+asyncpg://", "postgresql://", 1))
    host = (parsed.hostname or "").lower()
    database = parsed.path.lstrip("/")
    if parsed.scheme not in {"postgresql", "postgres"}:
        raise ReplaySafetyError("Replay database must be PostgreSQL on localhost")
    if host not in {"localhost", "127.0.0.1", "::1"}:
        raise ReplaySafetyError(f"Refusing non-local replay database host: {host or '<missing>'}")
    if database != "nova_creator_fidelity_test":
        raise ReplaySafetyError(
            f"Refusing replay database {database!r}; expected nova_creator_fidelity_test"
        )
    return host, database


def _redis_target(redis_url: str) -> tuple[str, int]:
    parsed = urlsplit(redis_url)
    host = (parsed.hostname or "").lower()
    if parsed.scheme != "redis" or host not in {"localhost", "127.0.0.1", "::1"}:
        raise ReplaySafetyError("Replay Redis must be redis:// on localhost")
    if parsed.port != 6379 or parsed.path != "/14" or parsed.query or parsed.username:
        raise ReplaySafetyError("Replay Redis must use localhost:6379 database 14")
    if parsed.password:
        raise ReplaySafetyError("Replay Redis must not carry a non-local password")
    return host, 14


def validate_local_boundary(
    *,
    database_url: str,
    storage_provider: str,
    e2e_fixtures: bool,
    destination_root: Path,
    source_root: Path,
) -> tuple[str, str]:
    """Validate every mutable destination before application imports."""

    host, database = _database_target(database_url)
    if storage_provider.strip().lower() != "local":
        raise ReplaySafetyError("Replay requires STORAGE_PROVIDER=local")
    if not e2e_fixtures:
        raise ReplaySafetyError("Replay requires E2E_FIXTURES=true")

    destination = destination_root.expanduser().resolve()
    source = source_root.expanduser().resolve()
    temp_roots = (Path("/private/tmp").resolve(), Path("/tmp").resolve())
    if not any(destination == root or root in destination.parents for root in temp_roots):
        raise ReplaySafetyError(
            f"Replay destination must be under /private/tmp or /tmp: {destination}"
        )
    if destination == source or destination in source.parents or source in destination.parents:
        raise ReplaySafetyError(
            "Replay destination must be separate from the source directory; "
            "source bytes are read-only"
        )
    if destination == Path("/") or len(destination.parts) < 4:
        raise ReplaySafetyError(f"Refusing broad replay destination: {destination}")
    return host, database


def _extract_transcript_cache(job_debug: dict[str, Any]) -> dict[str, Any]:
    all_candidates = (job_debug.get("job") or {}).get("all_candidates") or {}
    for key in ("voiceover_transcript", "transcript", "narration_transcript"):
        value = all_candidates.get(key)
        if value:
            return {"available": True, "source": f"job.all_candidates.{key}", "value": value}
    for key in ("voiceover_transcript", "transcript", "narration_transcript"):
        value = job_debug.get(key)
        if value:
            return {"available": True, "source": f"job_debug.{key}", "value": value}
    for run_index, run in enumerate(job_debug.get("agent_runs") or []):
        if not isinstance(run, dict):
            continue
        input_json = run.get("input_json")
        words = input_json.get("words") if isinstance(input_json, dict) else None
        if isinstance(words, list) and words:
            return {
                "available": True,
                "source": f"agent_runs[{run_index}].input_json.words",
                "value": {"words": words},
            }
    return {
        "available": False,
        "source": None,
        "value": None,
        "reason": "the supplied debug capture has no recorded voiceover transcript",
    }


def _normalise_clip_analysis(raw: dict[str, Any]) -> dict[str, Any]:
    analysis = dict(raw)
    detected_subject = analysis.get("detected_subject")
    if detected_subject and not analysis.get("subject"):
        analysis["subject"] = detected_subject
    return analysis


def _extract_clip_analysis_cache(
    job_debug: dict[str, Any], sources: list[dict[str, Any]]
) -> dict[str, dict[str, Any]]:
    all_candidates = (job_debug.get("job") or {}).get("all_candidates") or {}
    cache = all_candidates.get("clip_metadata_cache")
    if not isinstance(cache, dict) or not isinstance(cache.get("clip_metas"), list):
        return {}
    selected = (all_candidates.get("creator_strategy") or {}).get("selected_media_ids") or []
    selected_ids = [str(value) for value in selected]
    source_ids = {str(source.get("id")) for source in sources if source.get("kind") == "video"}
    result: dict[str, dict[str, Any]] = {}
    for raw in cache["clip_metas"]:
        if not isinstance(raw, dict):
            continue
        media_id = None
        for key in ("media_id", "source_id"):
            if raw.get(key) in source_ids:
                media_id = str(raw[key])
                break
        if media_id is None:
            clip_path = str(raw.get("clip_path") or "")
            match = re.search(r"(?:^|/)clip_(\d+)\.[^/]+$", clip_path)
            if match:
                index = int(match.group(1))
                if index < len(selected_ids):
                    media_id = selected_ids[index]
        if media_id is None and len(result) < len(selected_ids):
            media_id = selected_ids[len(result)]
        if media_id in source_ids and isinstance(raw, dict):
            result[media_id] = _normalise_clip_analysis(raw)
    return result


def load_inputs(
    *,
    manifest_path: Path,
    job_debug_path: Path,
    item_debug_path: Path,
    seed_description: str | None = None,
    allow_source_count_drift: bool = False,
) -> ReplayInputs:
    manifest = _read_json(manifest_path)
    raw_sources = manifest.get("sources")
    if not isinstance(raw_sources, list) or not raw_sources:
        raise ReplaySafetyError("Manifest must contain a non-empty sources list")
    if not allow_source_count_drift and len(raw_sources) != EXPECTED_SOURCE_COUNT:
        raise ReplaySafetyError(
            f"Expected {EXPECTED_SOURCE_COUNT} replay sources, found {len(raw_sources)}"
        )

    sources: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for raw in raw_sources:
        if not isinstance(raw, dict):
            raise ReplaySafetyError("Every manifest source must be an object")
        source_id = _safe_component(raw.get("id"), field="source id")
        if source_id in seen_ids:
            raise ReplaySafetyError(f"Duplicate source id: {source_id}")
        seen_ids.add(source_id)
        kind = str(raw.get("kind") or "").strip().lower()
        if kind not in {"video", "image", "audio"}:
            raise ReplaySafetyError(f"Unsupported source kind for {source_id}: {kind!r}")
        local_path = Path(str(raw.get("local_path") or "")).expanduser().resolve()
        if not local_path.is_file():
            raise ReplaySafetyError(f"Manifest source is missing: {local_path}")
        data = local_path.read_bytes()
        actual_hash = sha256_bytes(data)
        expected_hash = str(raw.get("sha256") or "").lower()
        if expected_hash and actual_hash != expected_hash:
            raise ReplaySafetyError(f"SHA-256 mismatch for {source_id}")
        sources.append(
            {
                **raw,
                "id": source_id,
                "kind": kind,
                "local_path": str(local_path),
                "size": len(data),
                "sha256": actual_hash,
            }
        )

    visual_count = sum(source["kind"] in {"video", "image"} for source in sources)
    audio_count = sum(source["kind"] == "audio" for source in sources)
    if not allow_source_count_drift and (visual_count != EXPECTED_VISUAL_COUNT or audio_count != 1):
        raise ReplaySafetyError(
            f"Expected {EXPECTED_VISUAL_COUNT} visuals and one voiceover; "
            f"found {visual_count} visuals and {audio_count} audio sources"
        )
    seed = str(
        seed_description if seed_description is not None else manifest.get("creator_request") or ""
    ).strip()
    if not seed:
        raise ReplaySafetyError("A creator seed description is required")
    if len(seed) > 12_000:
        raise ReplaySafetyError("Creator seed description exceeds the Creator request limit")
    job_debug = _read_json(job_debug_path)
    item_debug = _read_json(item_debug_path)
    identity = [
        {
            key: source[key]
            for key in ("id", "kind", "size", "sha256", "generation")
            if key in source
        }
        for source in sources
    ]
    return ReplayInputs(
        manifest=manifest,
        job_debug=job_debug,
        item_debug=item_debug,
        sources=sources,
        seed_description=seed,
        source_identity_hash=sha256_json(identity),
        manifest_hash=sha256_json(manifest),
        clip_analysis_cache=_extract_clip_analysis_cache(job_debug, sources),
        transcript_cache=_extract_transcript_cache(job_debug),
    )


def _probe_duration(path: Path) -> float:
    try:
        completed = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                str(path),
            ],
            capture_output=True,
            text=True,
            check=True,
            timeout=30,
        )
        duration = float(completed.stdout.strip())
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        raise ReplaySafetyError(f"ffprobe failed for {path}: {exc}") from exc
    if duration <= 0:
        raise ReplaySafetyError(f"ffprobe returned a non-positive duration for {path}")
    return duration


def _content_type(source: dict[str, Any]) -> str:
    declared = str(source.get("content_type") or "").split(";", 1)[0].strip().lower()
    if declared:
        return declared
    guessed = mimetypes.guess_type(str(source.get("local_path")))[0]
    return (
        guessed
        or {"video": "video/mp4", "image": "image/jpeg", "audio": "audio/mpeg"}[source["kind"]]
    )


def _source_debug_map(item_debug: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {
        str(value.get("id")): value
        for value in (item_debug.get("pool_assets") or [])
        if isinstance(value, dict) and value.get("id")
    }


def _copy_source(source: dict[str, Any], object_path: str, storage: Any) -> dict[str, Any]:
    storage.upload_local_file(str(source["local_path"]), object_path, _content_type(source))
    metadata = storage.object_metadata(object_path)
    return {
        "object_path": object_path,
        "generation": str(metadata.generation),
        "size": int(metadata.size),
        "content_type": str(metadata.content_type or _content_type(source)),
    }


def seed_replay_database(
    *,
    inputs: ReplayInputs,
    output_dir: Path,
    user_id: uuid.UUID,
    thread_id: uuid.UUID,
    plan_id: uuid.UUID,
    item_id: uuid.UUID,
) -> dict[str, Any]:
    """Copy sources and create the minimum owned graph the real controllers need."""

    from app import storage
    from app.database import sync_session
    from app.models import (
        ContentPlan,
        CreationThread,
        CreationThreadEvent,
        Persona,
        PlanItem,
        PlanItemAsset,
        User,
    )

    storage_root = output_dir / "storage"
    storage_root.mkdir(parents=True, exist_ok=True)
    debug_item = (
        (inputs.item_debug.get("item") or {}) if isinstance(inputs.item_debug, dict) else {}
    )
    source_debug = _source_debug_map(inputs.item_debug)
    videos = [source for source in inputs.sources if source["kind"] == "video"]
    images = [source for source in inputs.sources if source["kind"] == "image"]
    audio = [source for source in inputs.sources if source["kind"] == "audio"]
    if len(audio) != 1:
        raise ReplaySafetyError("Replay requires exactly one voiceover source")

    assignments: list[dict[str, Any]] = []
    imported: list[dict[str, Any]] = []
    for source in videos:
        object_path = f"users/{user_id}/creation-threads/{thread_id}/{source['id']}"
        copied = _copy_source(source, object_path, storage)
        duration = _probe_duration(Path(source["local_path"]))
        analysis = inputs.clip_analysis_cache.get(source["id"])
        assignment = {
            "media_id": source["id"],
            "gcs_path": object_path,
            "kind": "video",
            "duration_s": duration,
            "generation": copied["generation"],
            "storage_generation": copied["generation"],
            "has_audio": None,
        }
        if analysis is not None:
            assignment["analysis"] = analysis
        assignments.append(assignment)
        imported.append({"source_id": source["id"], "kind": source["kind"], **copied})

    voiceover = audio[0]
    voiceover_path = f"users/{user_id}/creation-threads/{thread_id}/{voiceover['id']}"
    voiceover_copy = _copy_source(voiceover, voiceover_path, storage)
    voiceover_duration = _probe_duration(Path(voiceover["local_path"]))
    imported.append({"source_id": voiceover["id"], "kind": "audio", **voiceover_copy})

    asset_rows: list[dict[str, Any]] = []
    for source in images:
        asset_id = uuid.uuid5(user_id, f"replay-pool-asset:{source['id']}")
        filename = str(source.get("source_filename") or Path(source["local_path"]).name)
        safe_filename = _safe_filename(filename, field="source filename")
        object_path = f"users/{user_id}/plan/{item_id}/pool/{asset_id}-{safe_filename}"
        copied = _copy_source(source, object_path, storage)
        debug_asset = source_debug.get(source["id"], {})
        asset_rows.append(
            {
                "id": asset_id,
                "source": source,
                "object_path": object_path,
                "copy": copied,
                "aspect": source.get("aspect") or debug_asset.get("aspect"),
                "analysis": source.get("analysis")
                if isinstance(source.get("analysis"), dict)
                else {},
                "filename": filename,
            }
        )
        imported.append({"source_id": source["id"], "kind": source["kind"], **copied})

    all_candidates = (inputs.job_debug.get("job") or {}).get("all_candidates") or {}
    old_persona = (
        all_candidates.get("persona") if isinstance(all_candidates.get("persona"), dict) else {}
    )
    persona_data = dict(old_persona)
    persona_data.setdefault("summary", str(old_persona.get("idea") or ""))
    persona_data.setdefault("content_pillars", [])
    persona_data.setdefault("tone", "")
    persona_data.setdefault("audience", "")
    persona_data.setdefault("preference_summary", "")
    item_format = str(debug_item.get("edit_format") or "narrated_planned")
    if item_format not in {"narrated_planned", "narrated", "montage", "talking_head", "subtitled"}:
        item_format = "narrated_planned"
    media_projection = [
        {
            "media_id": assignment["media_id"],
            "kind": "video",
            "filename": source["id"],
            "content_type": _content_type(source),
        }
        for assignment, source in zip(assignments, videos, strict=True)
    ]
    media_projection.append(
        {
            "media_id": voiceover["id"],
            "kind": "audio",
            "filename": voiceover.get("source_filename") or voiceover["id"],
            "content_type": _content_type(voiceover),
        }
    )

    with sync_session() as db:
        persona_id = uuid.uuid5(user_id, "replay-persona")
        user = User(
            id=user_id,
            email=f"creator-fidelity-{user_id}@local.invalid",
            name="Creator Fidelity Replay",
            auth_provider="dev",
            onboarding_status="persona_ready",
        )
        persona = Persona(
            id=persona_id,
            user_id=user_id,
            questionnaire={},
            persona=persona_data,
            style={},
            idea_seeds=[],
            persona_status="edited",
        )
        plan = ContentPlan(
            id=plan_id,
            user_id=user_id,
            persona_id=persona_id,
            plan_status="edited",
            horizon_days=30,
            ownership_epoch=0,
        )
        item = PlanItem(
            id=item_id,
            content_plan_id=plan_id,
            position=1,
            idea=inputs.seed_description[:1200],
            edit_format=item_format,
            content_mode="existing_footage",
            montage_preset="classic",
            item_status=str(debug_item.get("item_status") or "awaiting_clips"),
            clip_gcs_paths=[assignment["gcs_path"] for assignment in assignments],
            clip_assignments=assignments,
            voiceover_gcs_path=voiceover_path,
            voiceover_generation=voiceover_copy["generation"],
            voiceover_duration_s=voiceover_duration,
            audio_mode="voiceover",
            theme=str(old_persona.get("theme") or "") or None,
            user_edited=True,
        )
        thread = CreationThread(
            id=thread_id,
            creator_id=user_id,
            content_plan_id=plan_id,
            active_plan_item_id=item_id,
            title="Creator prompt fidelity replay",
            revision=1,
            state={
                "intent": inputs.seed_description,
                "edit_format": item_format,
                "media": media_projection,
                "media_count": len(media_projection),
                "title_source": "replay",
            },
        )
        # The database enforces composite owner foreign keys that are not all
        # represented as ORM relationships. Flush in dependency order.
        for row in (user, persona, plan, item, thread):
            db.add(row)
            db.flush()
        for row in asset_rows:
            db.add(
                PlanItemAsset(
                    id=row["id"],
                    plan_item_id=item_id,
                    user_id=user_id,
                    gcs_path=row["object_path"],
                    kind="image",
                    source_filename=row["filename"],
                    upload_content_type=row["copy"]["content_type"],
                    upload_size_bytes=row["copy"]["size"],
                    gcs_generation=row["copy"]["generation"],
                    content_hash=row["source"]["sha256"],
                    content_fingerprint=f"sha256:{row['source']['sha256']}",
                    client_upload_id=f"replay:{row['source']['id']}",
                    aspect=float(row["aspect"]) if row["aspect"] else None,
                    analysis=row["analysis"],
                    status="uploaded",
                    media_status="pending",
                )
            )
        db.add(
            CreationThreadEvent(
                thread_id=thread_id,
                sequence=0,
                client_event_id=f"replay-seed-{user_id.hex}",
                role="user",
                event_type="thread_created",
                content=inputs.seed_description,
                payload={"source": "local_creator_replay"},
                revision=1,
            )
        )
        db.commit()

    return {
        "user_id": str(user_id),
        "content_plan_id": str(plan_id),
        "plan_item_id": str(item_id),
        "thread_id": str(thread_id),
        "source_count": len(imported),
        "imported": imported,
        "cached_clip_analysis_count": len(inputs.clip_analysis_cache),
        "voiceover_duration_s": voiceover_duration,
        "storage_root": str(storage_root),
    }


def analyze_replay_media(*, item_id: uuid.UUID) -> dict[str, Any]:
    """Run the real local media analyzers before Main Creator sees the pool."""

    from app.database import sync_session
    from app.models import PlanItem, PlanItemAsset
    from app.tasks.autoplace import analyze_pool_asset
    from app.tasks.edit_proposal_build import _analyze_clip_assignment

    with sync_session() as db:
        item = db.get(PlanItem, item_id)
        if item is None:
            raise ReplaySafetyError("Seeded replay item disappeared before media analysis")
        assignments = [dict(value) for value in (item.clip_assignments or [])]
        asset_ids = list(
            db.query(PlanItemAsset.id)
            .filter(PlanItemAsset.plan_item_id == item_id)
            .order_by(PlanItemAsset.created_at)
            .all()
        )
        asset_ids = [value[0] for value in asset_ids]

    for asset_id in asset_ids:
        analyze_pool_asset.run(str(asset_id), False)

    with sync_session() as db:
        assets = (
            db.query(PlanItemAsset)
            .filter(PlanItemAsset.plan_item_id == item_id)
            .order_by(PlanItemAsset.created_at)
            .all()
        )
        failed_assets = [
            str(asset.id)
            for asset in assets
            if asset.status != "ready" or not isinstance(asset.analysis, dict) or not asset.analysis
        ]
        if failed_assets:
            raise ReplaySafetyError(
                f"Pool media analysis did not produce ready analysis for {failed_assets}"
            )

        analyzed_assignments: list[dict[str, Any]] = []
        live_clip_count = 0
        cached_clip_count = 0
        for assignment in assignments:
            before = assignment.get("analysis")
            updated, _ref = _analyze_clip_assignment(assignment, {})
            if isinstance(before, dict) and before:
                cached_clip_count += 1
            else:
                live_clip_count += 1
            if not updated.get("analysis"):
                raise ReplaySafetyError(
                    f"Clip analysis did not produce analysis for {assignment.get('media_id')}"
                )
            analyzed_assignments.append(updated)
        item.clip_assignments = analyzed_assignments
        db.commit()

    return {
        "pool_asset_count": len(asset_ids),
        "pool_asset_analysis": "live_task",
        "clip_count": len(assignments),
        "cached_clip_analysis_count": cached_clip_count,
        "live_clip_analysis_count": live_clip_count,
        "clip_analysis_hash": sha256_json(analyzed_assignments),
    }


async def run_creator_planning(
    *,
    item_id: uuid.UUID,
    seed_description: str,
    user_id: uuid.UUID,
) -> dict[str, Any]:
    from app.database import AsyncSessionLocal
    from app.models import User
    from app.routes.creator_agent import StartBody, start_creator_session_controller

    async with AsyncSessionLocal() as db:
        user = await db.get(User, user_id)
        if user is None:
            raise ReplaySafetyError("Seeded replay user disappeared before planning")
        started = await start_creator_session_controller(
            None,
            str(item_id),
            StartBody(message=seed_description, client_event_id=f"replay-plan-{uuid.uuid4().hex}"),
            user,
            db,
            allow_chat=True,
        )
        return started.model_dump(mode="json")


async def confirm_creator_plan(
    *,
    item_id: uuid.UUID,
    user_id: uuid.UUID,
    plan: dict[str, Any],
) -> dict[str, Any]:
    from app.database import AsyncSessionLocal
    from app.models import User
    from app.routes.creator_agent import ConfirmBody, confirm_creator_plan_controller

    pending = plan.get("pending_plan") or {}
    if not pending.get("plan_hash") or not pending.get("version"):
        raise ReplaySafetyError("Main Creator did not return a confirmable plan")
    async with AsyncSessionLocal() as db:
        user = await db.get(User, user_id)
        if user is None:
            raise ReplaySafetyError("Seeded replay user disappeared before confirmation")
        confirmed = await confirm_creator_plan_controller(
            str(item_id),
            ConfirmBody(
                session_id=uuid.UUID(str(plan["id"])),
                expected_revision=int(plan["revision"]),
                plan_version=int(pending["version"]),
                plan_hash=str(pending["plan_hash"]),
                client_event_id=f"replay-confirm-{uuid.uuid4().hex}",
            ),
            user,
            db,
            allow_chat=True,
        )
        return confirmed.model_dump(mode="json")


def _row_snapshot(db: Any, *, item_id: uuid.UUID, session_id: uuid.UUID) -> dict[str, Any]:
    from app.models import CreatorAgentSession, Job, PlanItem
    from app.schemas.edit_proposal import parse_edit_proposal

    item = db.get(PlanItem, item_id)
    session = db.get(CreatorAgentSession, session_id)
    job = db.get(Job, item.current_job_id) if item and item.current_job_id else None
    proposal = parse_edit_proposal(item.edit_proposal) if item else None
    return {
        "session": {
            "id": str(session.id) if session else None,
            "status": session.status if session else None,
            "revision": session.revision if session else None,
            "render_attempts": session.render_attempts if session else None,
            "target_job_id": str(session.target_job_id)
            if session and session.target_job_id
            else None,
        },
        "proposal": proposal.model_dump(mode="json") if proposal else None,
        "job": {
            "id": str(job.id),
            "status": job.status,
            "failure_reason": job.failure_reason,
            "error_detail": job.error_detail,
            "assembly_plan": job.assembly_plan,
            "all_candidates": job.all_candidates,
        }
        if job
        else None,
    }


def _output_receipts(
    snapshot: dict[str, Any], storage: Any, source_paths: set[str]
) -> list[dict[str, Any]]:
    job = snapshot.get("job") or {}
    assembly_plan = job.get("assembly_plan") if isinstance(job.get("assembly_plan"), dict) else {}
    variants = (
        assembly_plan.get("variants") if isinstance(assembly_plan.get("variants"), list) else []
    )
    receipts: list[dict[str, Any]] = []
    seen: set[str] = set()
    for variant in variants:
        if not isinstance(variant, dict):
            continue
        for field in (
            "output_url",
            "video_path",
            "poster_path",
            "base_video_path",
            "base_poster_path",
        ):
            value = variant.get(field)
            if not isinstance(value, str) or not value or value in seen or "://" in value:
                continue
            seen.add(value)
            if value in source_paths:
                raise ReplaySafetyError("Worker output points at an imported source object")
            try:
                metadata = storage.object_metadata(value)
                local_path = storage.local_object_path(value)
                digest = sha256_bytes(local_path.read_bytes()) if local_path.is_file() else None
                receipts.append(
                    {
                        "variant_id": variant.get("variant_id"),
                        "field": field,
                        "object_path": value,
                        "generation": str(metadata.generation),
                        "size": int(metadata.size),
                        "sha256": digest,
                    }
                )
            except FileNotFoundError:
                receipts.append(
                    {
                        "variant_id": variant.get("variant_id"),
                        "field": field,
                        "object_path": value,
                        "missing": True,
                    }
                )
    return receipts


def wait_for_worker(
    *,
    item_id: uuid.UUID,
    session_id: uuid.UUID,
    timeout_s: float,
    poll_interval_s: float,
    on_snapshot: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    from app import storage
    from app.database import sync_session

    deadline = time.monotonic() + timeout_s
    last: dict[str, Any] = {}
    source_paths: set[str] = set()
    with sync_session() as db:
        from app.models import PlanItem

        item = db.get(PlanItem, item_id)
        if item:
            source_paths.update(
                str(value.get("gcs_path"))
                for value in (item.clip_assignments or [])
                if isinstance(value, dict)
            )
            if item.voiceover_gcs_path:
                source_paths.add(str(item.voiceover_gcs_path))
    while time.monotonic() < deadline:
        with sync_session() as db:
            last = _row_snapshot(db, item_id=item_id, session_id=session_id)
            if on_snapshot is not None:
                on_snapshot(last)
            job = last.get("job") or {}
            proposal = last.get("proposal") or {}
            session_state = last.get("session") or {}
            if not job and session_state.get("status") in {"failed", "cancelled"}:
                raise ReplaySafetyError(
                    f"Creator execution stopped before dispatch: {session_state['status']}"
                )
            if job.get("status") in TERMINAL_JOB_STATUSES:
                last["outputs"] = _output_receipts(last, storage, source_paths)
                return last
            if proposal.get("status") == "failed" and not job:
                raise ReplaySafetyError(
                    f"Proposal worker failed before dispatch: {proposal.get('failure') or proposal}"
                )
        time.sleep(min(max(poll_interval_s, 0.2), 30.0))
    raise ReplaySafetyError(f"Timed out waiting for proposal/worker; last state: {last}")


def _configure_environment(args: argparse.Namespace, *, output_dir: Path) -> dict[str, Any]:
    database_url = str(args.database_url or os.environ.get("DATABASE_URL") or DEFAULT_DATABASE_URL)
    redis_url = str(args.redis_url or DEFAULT_REDIS_URL)
    source_root = Path(args.manifest).expanduser().resolve().parent
    validate_local_boundary(
        database_url=database_url,
        storage_provider="local",
        e2e_fixtures=True,
        destination_root=output_dir,
        source_root=source_root,
    )
    redis_host, redis_database = _redis_target(redis_url)
    if args.render and not args.enable_fidelity:
        raise ReplaySafetyError("--render requires --enable-fidelity")
    storage_root = output_dir / "storage"
    output_dir.mkdir(parents=True, exist_ok=True)
    os.environ.update(
        {
            "DATABASE_URL": database_url,
            "STORAGE_PROVIDER": "local",
            "STORAGE_BUCKET": LOCAL_BUCKET,
            "E2E_FIXTURES": "true",
            "LOCAL_STORAGE_ROOT": str(storage_root),
            "LOCAL_STORAGE_BASE_URL": "http://127.0.0.1:8000/dev-qa/storage",
            "CREATOR_PROMPT_FIDELITY_ENABLED": "true" if args.enable_fidelity else "false",
            "MAIN_CREATOR_AGENT_ENABLED": "true",
            "MAIN_CREATOR_AGENT_ROLLOUT_PERCENT": "100",
            "MAIN_CREATOR_AGENT_EXECUTION_ENABLED": "true",
            "OPENAI_API_KEY": os.environ.get("OPENAI_API_KEY", "local-replay-placeholder"),
            "GEMINI_API_KEY": os.environ.get("GEMINI_API_KEY", ""),
            "TOKEN_ENCRYPTION_KEY": os.environ.get(
                "TOKEN_ENCRYPTION_KEY", "local-replay-placeholder"
            ),
            "REDIS_URL": redis_url,
        }
    )
    return {
        "database_url": database_url,
        "database_host": _database_target(database_url)[0],
        "database_name": _database_target(database_url)[1],
        "redis_url": redis_url,
        "redis_host": redis_host,
        "redis_database": redis_database,
        "storage_provider": "local",
        "storage_bucket": LOCAL_BUCKET,
        "e2e_fixtures": True,
        "storage_root": str(storage_root),
        "destination_root": str(output_dir),
        "creator_prompt_fidelity_enabled": bool(args.enable_fidelity),
        "main_creator_agent_enabled": True,
        "main_creator_agent_rollout_percent": 100,
        "main_creator_agent_execution_enabled": True,
        "render_requested": bool(args.render),
    }


def _write_receipt(path: Path, receipt: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(f"{path.suffix}.tmp-{os.getpid()}")
    temporary.write_text(
        json.dumps(receipt, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    temporary.replace(path)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--job-debug", type=Path, default=DEFAULT_JOB_DEBUG)
    parser.add_argument("--item-debug", type=Path, default=DEFAULT_ITEM_DEBUG)
    parser.add_argument("--database-url", default=None)
    parser.add_argument("--redis-url", default=DEFAULT_REDIS_URL)
    parser.add_argument("--output-dir", type=Path, default=None)
    parser.add_argument("--seed-description", default=None)
    parser.add_argument(
        "--enable-fidelity", action="store_true", help="opt into the local fidelity flag"
    )
    parser.add_argument(
        "--render", action="store_true", help="continue through proposal, dispatch, and worker"
    )
    parser.add_argument(
        "--allow-source-count-drift",
        action="store_true",
        help="allow a manifest other than the pinned 40-source input",
    )
    parser.add_argument("--timeout-s", type=float, default=1800.0)
    parser.add_argument("--poll-interval-s", type=float, default=5.0)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    output_dir = (
        (args.output_dir or (DEFAULT_OUTPUT_ROOT / datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")))
        .expanduser()
        .resolve()
    )
    receipt_path = output_dir / "replay-receipt.json"
    receipt: dict[str, Any] = {
        "schema_version": 1,
        "run_id": output_dir.name,
        "started_at": datetime.now(UTC).isoformat(),
        "mode": "render" if args.render else "plan_only",
        "dry_run": not args.render,
        "inputs": {
            "manifest": str(args.manifest.expanduser().resolve()),
            "job_debug": str(args.job_debug.expanduser().resolve()),
            "item_debug": str(args.item_debug.expanduser().resolve()),
        },
    }
    runner = asyncio.Runner()
    try:
        receipt["runtime"] = _configure_environment(args, output_dir=output_dir)
        _write_receipt(receipt_path, receipt)
        inputs = load_inputs(
            manifest_path=args.manifest,
            job_debug_path=args.job_debug,
            item_debug_path=args.item_debug,
            seed_description=args.seed_description,
            allow_source_count_drift=args.allow_source_count_drift,
        )
        receipt["input_hashes"] = {
            "manifest_hash": inputs.manifest_hash,
            "source_identity_hash": inputs.source_identity_hash,
            "source_sha256": {source["id"]: source["sha256"] for source in inputs.sources},
            "creator_seed_hash": sha256_bytes(inputs.seed_description.encode("utf-8")),
            "cached_clip_analysis_hash": sha256_json(inputs.clip_analysis_cache),
            "transcript_cache_hash": sha256_json(inputs.transcript_cache),
        }
        receipt["cache"] = {
            "clip_analysis_count": len(inputs.clip_analysis_cache),
            "clip_analysis_ids": sorted(inputs.clip_analysis_cache),
            "transcript": inputs.transcript_cache,
        }
        _write_receipt(receipt_path, receipt)
        ids = {key: uuid.uuid4() for key in ("user_id", "thread_id", "plan_id", "item_id")}
        receipt["seed"] = seed_replay_database(
            inputs=inputs,
            output_dir=output_dir,
            user_id=ids["user_id"],
            thread_id=ids["thread_id"],
            plan_id=ids["plan_id"],
            item_id=ids["item_id"],
        )
        _write_receipt(receipt_path, receipt)
        receipt["analysis"] = analyze_replay_media(item_id=ids["item_id"])
        _write_receipt(receipt_path, receipt)
        plan = runner.run(
            run_creator_planning(
                item_id=ids["item_id"],
                seed_description=inputs.seed_description,
                user_id=ids["user_id"],
            )
        )
        receipt["planning"] = {
            "session_id": plan.get("id"),
            "status": plan.get("status"),
            "revision": plan.get("revision"),
            "pending_plan_hash": (plan.get("pending_plan") or {}).get("plan_hash"),
            "pending_plan": plan.get("pending_plan"),
        }
        _write_receipt(receipt_path, receipt)
        if args.render:
            confirmed = runner.run(
                confirm_creator_plan(item_id=ids["item_id"], user_id=ids["user_id"], plan=plan)
            )
            receipt["confirmation"] = {
                "session_id": confirmed.get("id"),
                "status": confirmed.get("status"),
                "revision": confirmed.get("revision"),
                "current_job_id": confirmed.get("current_job_id"),
            }
            _write_receipt(receipt_path, receipt)
            worker_state = wait_for_worker(
                item_id=ids["item_id"],
                session_id=uuid.UUID(str(confirmed["id"])),
                timeout_s=args.timeout_s,
                poll_interval_s=args.poll_interval_s,
                on_snapshot=lambda snapshot: _write_receipt(
                    receipt_path,
                    {
                        **receipt,
                        "worker_last_snapshot": snapshot,
                        "updated_at": datetime.now(UTC).isoformat(),
                    },
                ),
            )
            receipt["worker"] = worker_state
            receipt["job"] = worker_state.get("job")
            receipt["outputs"] = worker_state.get("outputs", [])
        receipt["finished_at"] = datetime.now(UTC).isoformat()
        _write_receipt(receipt_path, receipt)
        print(json.dumps({"receipt": str(receipt_path), "mode": receipt["mode"], "status": "ok"}))
        return 0
    except KeyboardInterrupt:
        receipt["finished_at"] = datetime.now(UTC).isoformat()
        receipt["error"] = {"type": "KeyboardInterrupt", "message": "replay interrupted"}
        _write_receipt(receipt_path, receipt)
        print(json.dumps({"receipt": str(receipt_path), "status": "interrupted"}), file=sys.stderr)
        return 130
    except Exception as exc:  # noqa: BLE001 - receipt must capture driver failures
        receipt["finished_at"] = datetime.now(UTC).isoformat()
        receipt["error"] = {"type": type(exc).__name__, "message": str(exc)}
        try:
            _write_receipt(receipt_path, receipt)
        except OSError:
            pass
        print(
            json.dumps({"receipt": str(receipt_path), "status": "error", "error": str(exc)}),
            file=sys.stderr,
        )
        return 1
    finally:
        runner.close()


if __name__ == "__main__":
    raise SystemExit(main())
