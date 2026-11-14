"""Run a local, same-input Creator Agent replay.

This is intentionally a driver rather than a product route.  It imports the
captured source bytes into a fresh local namespace, seeds the isolated replay
database, calls the real Creator controllers, and writes an auditable receipt.
The default run stops after Main Creator planning.  ``--render`` is an explicit
opt-in to proposal generation, queue dispatch, and worker rendering.
``--thread-events`` replays a runtime-v2 chat instead: the creator's recorded
messages go through the Kria runtime's submit and approve controllers in order.

The module has no application imports at module load time.  That matters for
the safety checks: a stale shell with production environment variables must be
rejected before SQLAlchemy, storage, or Celery can be initialized.
"""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
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
# `awaiting_device` is the success state of an iPhone job: the worker pinned the device render
# plan and the phone renders it, so a replay has no video to wait for.
TERMINAL_JOB_STATUSES = frozenset(
    {
        "awaiting_device",
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
    recorded_assignments: dict[str, dict[str, Any]] = dataclasses.field(default_factory=dict)


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


_SCRATCH_DATABASE = re.compile(r"nova_creator_fidelity_test(?:_[a-z0-9]{1,16})?")
# Only the dev server's port. `fly proxy 15432:5432 -a nova-db` (export-template-text-snapshots
# runbook) puts the production cluster on localhost too, so the host alone proves nothing.
LOCAL_POSTGRES_PORT = 5432


def _database_target(database_url: str) -> tuple[str, str]:
    parsed = urlsplit(database_url.replace("postgresql+asyncpg://", "postgresql://", 1))
    host = (parsed.hostname or "").lower()
    database = parsed.path.lstrip("/")
    if parsed.scheme not in {"postgresql", "postgres"}:
        raise ReplaySafetyError("Replay database must be PostgreSQL on localhost")
    if host not in {"localhost", "127.0.0.1", "::1"}:
        raise ReplaySafetyError(f"Refusing non-local replay database host: {host or '<missing>'}")
    if (parsed.port or LOCAL_POSTGRES_PORT) != LOCAL_POSTGRES_PORT:
        raise ReplaySafetyError(
            f"Refusing replay database port {parsed.port}; only the local dev server on "
            f"{LOCAL_POSTGRES_PORT} (a tunnel such as fly proxy also listens on localhost)"
        )
    if parsed.query:
        # libpq reads host, hostaddr and service from the query and they override the netloc.
        raise ReplaySafetyError("Refusing a replay database URL with a query string")
    if not _SCRATCH_DATABASE.fullmatch(database):
        raise ReplaySafetyError(
            f"Refusing replay database {database!r}; expected nova_creator_fidelity_test"
            " or nova_creator_fidelity_test_<run id>"
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


RECEIPT_FIELDS = (
    "upload_contract",
    "manifest_identity",
    "has_audio",
    "duration_s",
    "pool_source",
    "analysis",
)


def _extract_recorded_assignments(job_debug: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """The upload receipts and clip analysis the approved plan recorded.

    The admin item debug summarizes clip assignments without their receipts; the job's
    brief binding keeps the full rows. Its analysis is the one production planned with, so
    the replay reuses it instead of paying for a second analysis of the same clip.
    """

    plan = (job_debug.get("job") or {}).get("assembly_plan") or {}
    snapshot = ((plan.get("creator_brief_binding") or {}).get("media_snapshot")) or {}
    return {
        str(row["media_id"]): {key: row[key] for key in RECEIPT_FIELDS if key in row}
        for row in snapshot.get("clip_assignments") or []
        if isinstance(row, dict) and row.get("media_id")
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
    source_ids = {
        str(source.get("id"))
        for source in sources
        if source.get("kind") == "video" and not source.get("pool")
    }
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
        recorded_assignments=_extract_recorded_assignments(job_debug),
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


def _event_order(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(events, key=lambda event: int(event.get("sequence") or 0))


def _sent_from_app(thread_events: list[dict[str, Any]]) -> bool:
    """The iPhone app sends a client protocol with every message; the web sends none."""

    return any(
        (event.get("payload") or {}).get("client_protocol") is not None
        for event in thread_events
        if event.get("kind") == "user_message"
    )


def _with_recorded_media(
    projection: list[dict[str, Any]], thread_events: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Each replayed medium as production's thread projected it (filename, upload receipt)."""

    recorded: dict[str, dict[str, Any]] = {}
    for event in _event_order(thread_events):
        if event.get("kind") != "media_added":
            continue
        for entry in (event.get("payload") or {}).get("media") or []:
            if isinstance(entry, dict) and entry.get("media_id"):
                recorded[str(entry["media_id"]).removeprefix("asset-")] = entry
    return [
        {**entry, **recorded.get(str(entry["media_id"]).removeprefix("asset-"), {})}
        for entry in projection
    ]


# Production events before the first message that the app's thread creation writes; media is
# re-projected from the replayed sources instead of copied.
SEEDED_THREAD_EVENTS = frozenset({"format_prompt", "action_select_format"})


def _runtime_seed_events(
    thread_events: list[dict[str, Any]], media: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    seeded = [{"role": "system", "event_type": "thread_created", "content": None, "payload": None}]
    for event in _event_order(thread_events):
        if event.get("kind") == "user_message":
            break
        if event.get("kind") in SEEDED_THREAD_EVENTS:
            seeded.append(
                {
                    "role": str(event.get("actor") or "system"),
                    "event_type": str(event["kind"]),
                    "content": event.get("content"),
                    "payload": event.get("payload"),
                }
            )
    seeded.append(
        {
            "role": "user",
            "event_type": "media_added",
            "content": None,
            "payload": {"media": media, "media_count": len(media)},
        }
    )
    return seeded


def seed_replay_database(
    *,
    inputs: ReplayInputs,
    output_dir: Path,
    user_id: uuid.UUID,
    thread_id: uuid.UUID,
    plan_id: uuid.UUID,
    item_id: uuid.UUID,
    session_id: uuid.UUID | None = None,
    thread_events: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Copy sources and create the minimum owned graph the real controllers need.

    With ``thread_events`` (a runtime-v2 production thread) the thread is a runtime-v2 one
    with its own Creator session, set up the way the app's thread creation and media attach
    leave it before the creator's first message.
    """

    from app import storage
    from app.database import sync_session
    from app.models import (
        ContentPlan,
        CreationThread,
        CreationThreadEvent,
        CreatorAgentSession,
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
    videos = [
        source for source in inputs.sources if source["kind"] == "video" and not source.get("pool")
    ]
    pool_sources = [
        source
        for source in inputs.sources
        if source["kind"] == "image" or (source["kind"] == "video" and source.get("pool"))
    ]
    audio = [source for source in inputs.sources if source["kind"] == "audio"]
    if len(audio) > 1:
        raise ReplaySafetyError("Replay accepts at most one voiceover source")

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
        assignment.update(inputs.recorded_assignments.get(source["id"], {}))
        if analysis is not None:
            assignment["analysis"] = analysis
        assignments.append(assignment)
        imported.append({"source_id": source["id"], "kind": source["kind"], **copied})

    voiceover = audio[0] if audio else None
    voiceover_path: str | None = None
    voiceover_copy: dict[str, Any] = {}
    voiceover_duration: float | None = None
    if voiceover is not None:
        voiceover_path = f"users/{user_id}/creation-threads/{thread_id}/{voiceover['id']}"
        voiceover_copy = _copy_source(voiceover, voiceover_path, storage)
        voiceover_duration = _probe_duration(Path(voiceover["local_path"]))
        imported.append({"source_id": voiceover["id"], "kind": "audio", **voiceover_copy})

    asset_rows: list[dict[str, Any]] = []
    for source in pool_sources:
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
                "duration_s": _probe_duration(Path(source["local_path"]))
                if source["kind"] == "video"
                else None,
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
    if voiceover is not None:
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
            voiceover_generation=voiceover_copy.get("generation"),
            voiceover_duration_s=voiceover_duration,
            **({"audio_mode": "voiceover"} if voiceover is not None else {}),
            theme=str(old_persona.get("theme") or "") or None,
            user_edited=True,
        )
        if thread_events is None:
            state: dict[str, Any] = {
                "intent": inputs.seed_description,
                "edit_format": item_format,
                "media": media_projection,
                "media_count": len(media_projection),
                "title_source": "replay",
            }
            seed_events = [
                {
                    "role": "user",
                    "event_type": "thread_created",
                    "content": inputs.seed_description,
                    "payload": {"source": "local_creator_replay"},
                }
            ]
            session = None
        else:
            media_projection = _with_recorded_media(media_projection, thread_events)
            state = {
                "media": media_projection,
                "media_count": len(media_projection),
                "edit_format": item_format,
            }
            if _sent_from_app(thread_events):
                from app.services.phone_destination import with_device_intent

                state = with_device_intent(state, native_client=True, user_id=user_id) or state
            seed_events = _runtime_seed_events(thread_events, media_projection)
            session = CreatorAgentSession(
                id=session_id or uuid.uuid4(),
                creator_id=user_id,
                plan_item_id=item_id,
                ownership_epoch=0,
            )
        thread = CreationThread(
            id=thread_id,
            creator_id=user_id,
            runtime_version=1 if session is None else 2,
            content_plan_id=plan_id,
            active_plan_item_id=item_id,
            active_creator_agent_session_id=session.id if session is not None else None,
            title="Creator prompt fidelity replay",
            revision=len(seed_events),
            state=state,
        )
        # The database enforces composite owner foreign keys that are not all
        # represented as ORM relationships. Flush in dependency order.
        for row in (user, persona, plan, item, session, thread):
            if row is not None:
                db.add(row)
                db.flush()
        for row in asset_rows:
            db.add(
                PlanItemAsset(
                    id=row["id"],
                    plan_item_id=item_id,
                    user_id=user_id,
                    gcs_path=row["object_path"],
                    kind=row["source"]["kind"],
                    duration_s=row["duration_s"],
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
        for sequence, event in enumerate(seed_events):
            db.add(
                CreationThreadEvent(
                    thread_id=thread_id,
                    sequence=sequence,
                    client_event_id=f"replay-seed-{sequence}-{user_id.hex}",
                    revision=sequence + 1,
                    **event,
                )
            )
        db.commit()

    return {
        "user_id": str(user_id),
        "content_plan_id": str(plan_id),
        "plan_item_id": str(item_id),
        "thread_id": str(thread_id),
        "runtime_version": 1 if session is None else 2,
        "seed_events": [event["event_type"] for event in seed_events],
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
    from app.services.creator_clip_analysis import analyze_clip_assignment
    from app.tasks.autoplace import analyze_pool_asset

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
            updated, _ref = analyze_clip_assignment(assignment, {})
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


class MainCreatorQuestion(ReplaySafetyError):
    """Planning stopped on a clarifying question; only the creator can answer it."""

    def __init__(self, question: dict[str, Any]) -> None:
        self.question = question
        super().__init__(
            "Main Creator asked a question instead of proposing a plan "
            f"({question.get('reason_code') or 'no reason code'}): {question.get('message')}"
        )


PREPARING = frozenset({"queued", "analyzing", "resolving"})


def _open_question(plan: dict[str, Any]) -> dict[str, Any] | None:
    """The question the session is waiting on, once clip preparation has stopped feeding it."""

    if (plan.get("preparation") or {}).get("status") in PREPARING:
        return None
    events = plan.get("events") or []
    if not events or events[-1].get("event_type") != "assistant_question":
        return None
    payload = events[-1].get("payload") or {}
    return {
        key: payload[key]
        for key in ("message", "reason_code", "options", "choice_question")
        if payload.get(key)
    }


async def run_creator_planning(
    *,
    item_id: uuid.UUID,
    seed_description: str,
    user_id: uuid.UUID,
    timeout_s: float = 600.0,
    poll_interval_s: float = 5.0,
) -> dict[str, Any]:
    """Start the Main Creator session, then wait for the worker to produce a pending plan."""

    from starlette.requests import Request

    from app.database import AsyncSessionLocal
    from app.models import User
    from app.routes.creator_agent import (
        StartBody,
        get_creator_session,
        start_creator_session_controller,
    )

    async with AsyncSessionLocal() as db:
        user = await db.get(User, user_id)
        if user is None:
            raise ReplaySafetyError("Seeded replay user disappeared before planning")
        started = await start_creator_session_controller(
            Request({"type": "http", "method": "POST", "path": "/", "headers": []}),
            str(item_id),
            StartBody(message=seed_description, client_event_id=f"replay-plan-{uuid.uuid4().hex}"),
            user,
            db,
            allow_chat=True,
        )
    plan = started.model_dump(mode="json")
    deadline = time.monotonic() + timeout_s
    while not plan.get("pending_plan"):
        if plan.get("status") in {"failed", "cancelled"}:
            raise ReplaySafetyError(f"Main Creator planning ended as {plan['status']}")
        question = _open_question(plan)
        if question is not None:
            raise MainCreatorQuestion(question)
        if time.monotonic() > deadline:
            raise ReplaySafetyError(f"Timed out waiting for a Main Creator plan; last: {plan}")
        await asyncio.sleep(min(max(poll_interval_s, 0.2), 30.0))
        async with AsyncSessionLocal() as db:
            user = await db.get(User, user_id)
            polled = await get_creator_session(str(item_id), user, db)
        if polled is not None:
            plan = polled.model_dump(mode="json")
    return plan


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


TURN_SETTLED = frozenset({"awaiting_approval", "completed", "failed", "cancelled", "superseded"})
# What the app sends beside the text of a message; the user_message event stores each one.
ANSWER_FIELDS = (
    "clip_selection",
    "choice_selection",
    "song_order",
    "song_range",
    "moment_order",
    "protected_region",
    "scope",
    "manual_edits",
)


class TurnDiverged(ReplaySafetyError):
    """The replayed turn ended where production's did not, so later turns cannot follow it."""

    def __init__(self, detail: dict[str, Any]) -> None:
        self.detail = detail
        super().__init__(detail["reason"])


@dataclass(frozen=True)
class RecordedTurn:
    """One creator message of the production thread, as its user_message event recorded it."""

    turn_id: str
    message: str
    client_protocol: int | None
    payload: dict[str, Any]
    approved: bool
    # The render the approval queued and the failure code the thread showed for it, if any.
    job_id: str | None = None
    render_failure: str | None = None


def recorded_turns(thread_events: list[dict[str, Any]], target_turn_id: str) -> list[RecordedTurn]:
    """The creator's messages in order, up to the one whose approval dispatched the job."""

    def by_turn(kind: str, key: str) -> dict[str, Any]:
        return {
            str((event.get("payload") or {}).get("turn_id")): (event.get("payload") or {}).get(key)
            for event in _event_order(thread_events)
            if event.get("kind") == kind
        }

    approved = by_turn("approval_approved", "approval_id")
    queued_jobs = by_turn("render_queued", "job_id")
    render_failures = by_turn("assistant_render_failed", "code")
    turns: list[RecordedTurn] = []
    for event in _event_order(thread_events):
        payload = event.get("payload") or {}
        if event.get("kind") != "user_message" or not payload.get("turn_id"):
            continue
        protocol = payload.get("client_protocol")
        turn = RecordedTurn(
            turn_id=str(payload["turn_id"]),
            message=str(event.get("content") or ""),
            client_protocol=int(protocol) if isinstance(protocol, int) else None,
            payload=payload,
            approved=str(payload["turn_id"]) in approved,
            job_id=queued_jobs.get(str(payload["turn_id"])),
            render_failure=render_failures.get(str(payload["turn_id"])),
        )
        turns.append(turn)
        if turn.turn_id == target_turn_id:
            if not turn.approved:
                raise ReplaySafetyError(
                    f"Production turn {target_turn_id} dispatched the job without a recorded "
                    "approval; only approval-dispatched jobs can be replayed turn by turn"
                )
            return turns
    raise ReplaySafetyError(f"The thread records no creator message for turn {target_turn_id}")


def _model_fields(field: str) -> set[str] | None:
    from typing import get_args

    from pydantic import BaseModel

    from app.kria.api_schemas import SubmitTurnBody

    annotation = SubmitTurnBody.model_fields[field].annotation
    model = next(
        (
            arg
            for arg in get_args(annotation)
            if isinstance(arg, type) and issubclass(arg, BaseModel)
        ),
        None,
    )
    return set(model.model_fields) if model is not None else None


def _turn_body(
    turn: RecordedTurn, *, revision: int, replay_turn_of: dict[str, str]
) -> tuple[Any, list[str]]:
    """The request body the app sent, with production turn ids mapped to the replay's."""

    from pydantic import ValidationError

    from app.kria.api_schemas import SubmitTurnBody

    answers: dict[str, Any] = {}
    for field in ANSWER_FIELDS:
        value = turn.payload.get(field)
        if value is None:
            continue
        allowed = _model_fields(field)
        if allowed is not None and isinstance(value, dict):
            # The server stamps its own keys onto the stored answer (`delegated`, generations).
            value = {key: item for key, item in value.items() if key in allowed}
        answers[field] = value
    link = turn.payload.get("request_outcome_link") or {}
    context = {
        key: link[key] for key in ("relation", "client_build", "client_version") if link.get(key)
    }
    if replay_turn_of.get(str(link.get("parent_turn_id"))):
        context["parent_turn_id"] = replay_turn_of[str(link["parent_turn_id"])]
    try:
        body = SubmitTurnBody.model_validate(
            {
                "message": turn.message,
                "client_event_id": f"replay-turn-{uuid.uuid4().hex}",
                "expected_thread_revision": revision,
                **({"outcome_context": context} if context.get("relation") else {}),
                **answers,
            }
        )
    except ValidationError as exc:
        raise ReplaySafetyError(
            f"Production message of turn {turn.turn_id} does not rebuild as a request "
            f"({sorted(answers)}): {exc.errors()[:2]}"
        ) from exc
    return body, sorted(answers)


async def submit_recorded_turn(
    *,
    thread_id: uuid.UUID,
    user_id: uuid.UUID,
    turn: RecordedTurn,
    replay_turn_of: dict[str, str],
) -> tuple[str, list[str]]:
    """Send one recorded message the way `POST /creation-threads/<id>/turns` does."""

    from app.database import AsyncSessionLocal
    from app.kria.runtime import RuntimeFailure, submit_turn
    from app.models import CreationThread
    from app.routes.kria_runtime import _publish_turn
    from app.services.client_protocol import clear_request_context, set_client_protocol

    set_client_protocol(turn.client_protocol)
    try:
        async with AsyncSessionLocal() as db:
            thread = await db.get(CreationThread, thread_id)
            if thread is None:
                raise ReplaySafetyError("Seeded replay thread disappeared")
            body, answers = _turn_body(
                turn, revision=int(thread.revision), replay_turn_of=replay_turn_of
            )
            try:
                accepted, should_publish = await submit_turn(
                    db, thread_id=thread_id, creator_id=user_id, body=body
                )
            except RuntimeFailure as failure:
                await db.rollback()
                raise TurnDiverged(
                    {
                        "production_turn_id": turn.turn_id,
                        "reason": f"The runtime refused the creator's message of production turn "
                        f"{turn.turn_id}: {failure.code}: {failure.message}",
                    }
                ) from failure
    finally:
        clear_request_context()
    if should_publish:
        _publish_turn(accepted.turn_id)
    return accepted.turn_id, answers


async def approve_replayed_turn(
    *,
    thread_id: uuid.UUID,
    user_id: uuid.UUID,
    turn_id: uuid.UUID,
    client_protocol: int | None,
    render_shape: dict[str, str],
) -> str:
    """Approve the turn's pending approval the way the app's Approve tap does."""

    from sqlalchemy import select

    from app.database import AsyncSessionLocal
    from app.kria.api_schemas import ApprovalDecisionBody
    from app.kria.runtime import RuntimeFailure, approval_fingerprint, decide_approval
    from app.models import CreationThread, CreatorAgentApproval
    from app.routes.kria_runtime import _publish_approval, _publish_turn
    from app.services.client_protocol import clear_request_context, set_client_protocol

    set_client_protocol(client_protocol)
    try:
        async with AsyncSessionLocal() as db:
            approval = (
                (
                    await db.execute(
                        select(CreatorAgentApproval)
                        .where(
                            CreatorAgentApproval.turn_id == turn_id,
                            CreatorAgentApproval.status == "pending",
                        )
                        .order_by(CreatorAgentApproval.created_at.desc())
                    )
                )
                .scalars()
                .first()
            )
            thread = await db.get(CreationThread, thread_id)
            if approval is None or thread is None:
                raise ReplaySafetyError(f"Replay turn {turn_id} has no pending approval")
            body = ApprovalDecisionBody(
                expected_thread_revision=int(thread.revision),
                expected_draft_revision=int(approval.draft_revision or 0),
                expected_approval_fingerprint=approval_fingerprint(approval),
                **render_shape,
            )
            try:
                decided, successor = await decide_approval(
                    db,
                    thread_id=thread_id,
                    approval_id=approval.id,
                    creator_id=user_id,
                    decision="approve",
                    body=body,
                )
            except RuntimeFailure as failure:
                await db.rollback()
                raise ReplaySafetyError(
                    f"The runtime refused the approval: {failure.code}: {failure.message}"
                ) from failure
    finally:
        clear_request_context()
    _publish_approval(decided.approval_id)
    if successor is not None:
        _publish_turn(successor)
    return decided.approval_id


def wait_for_turn(
    turn_id: uuid.UUID, *, settled: frozenset[str], timeout_s: float, poll_interval_s: float
) -> dict[str, Any]:
    from app.database import sync_session
    from app.models import CreatorAgentTurn

    deadline = time.monotonic() + timeout_s
    while True:
        with sync_session() as db:
            turn = db.get(CreatorAgentTurn, turn_id)
            state = {"status": turn.status, "error": turn.error} if turn else {"status": None}
        if state["status"] in settled:
            return state
        if time.monotonic() > deadline:
            raise ReplaySafetyError(
                f"Timed out waiting for Kria turn {turn_id}; last status {state['status']}"
            )
        time.sleep(min(max(poll_interval_s, 0.2), 30.0))


def turn_reply(turn_id: uuid.UUID) -> dict[str, Any]:
    """What the thread showed after the replayed message: event kinds and the last reply."""

    from app.database import sync_session
    from app.models import CreationThreadEvent, CreatorAgentTurn

    with sync_session() as db:
        turn = db.get(CreatorAgentTurn, turn_id)
        source = db.get(CreationThreadEvent, turn.source_event_id) if turn else None
        rows = (
            db.query(CreationThreadEvent)
            .filter(
                CreationThreadEvent.thread_id == turn.thread_id,
                CreationThreadEvent.sequence > source.sequence,
            )
            .order_by(CreationThreadEvent.sequence)
            .all()
            if source is not None
            else []
        )
        reply = next((row for row in reversed(rows) if row.role == "assistant"), None)
        return {
            "events": [row.event_type for row in rows],
            "reply_kind": reply.event_type if reply else None,
            "reply": (reply.content or "")[:400] if reply else None,
            "reply_code": (reply.payload or {}).get("code") if reply else None,
        }


def _item_job_id(item_id: uuid.UUID) -> str | None:
    from app.database import sync_session
    from app.models import PlanItem

    with sync_session() as db:
        item = db.get(PlanItem, item_id)
        return str(item.current_job_id) if item and item.current_job_id else None


def _render_shape(job_debug: dict[str, Any]) -> dict[str, str]:
    """The shape the creator picked for the production job; a default is the server's own."""

    shape = ((job_debug.get("job") or {}).get("all_candidates") or {}).get("creator_render_shape")
    if not isinstance(shape, dict) or shape.get("origin") != "explicit_pick":
        return {}
    return {
        **{key: shape[key] for key in ("output_orientation", "landscape_fit") if shape.get(key)},
        "render_shape_origin": "explicit_pick",
    }


def replay_recorded_thread(
    runner: asyncio.Runner,
    *,
    ids: dict[str, uuid.UUID],
    turns: list[RecordedTurn],
    render_shape: dict[str, str],
    timeout_s: float,
    poll_interval_s: float,
    log: list[dict[str, Any]],
    on_progress: Callable[[], None],
) -> dict[str, Any]:
    """Send the creator's messages in order and approve where the creator approved.

    Each message waits for the previous turn (and any render it approved) to settle, as in
    production. A turn that production approved must reach an approval in the replay too;
    otherwise the replay stops with what the replay showed instead.
    """

    replay_turn_of: dict[str, str] = {}
    worker_state: dict[str, Any] = {}
    for index, recorded in enumerate(turns):
        target = index == len(turns) - 1
        replay_turn_id, answers = runner.run(
            submit_recorded_turn(
                thread_id=ids["thread_id"],
                user_id=ids["user_id"],
                turn=recorded,
                replay_turn_of=replay_turn_of,
            )
        )
        replay_turn_of[recorded.turn_id] = replay_turn_id
        entry: dict[str, Any] = {
            "production_turn_id": recorded.turn_id,
            "replay_turn_id": replay_turn_id,
            "message": recorded.message[:300],
            "client_protocol": recorded.client_protocol,
            "answers_sent": answers,
            "production_approved": recorded.approved,
        }
        log.append(entry)
        state = wait_for_turn(
            uuid.UUID(replay_turn_id),
            settled=TURN_SETTLED,
            timeout_s=timeout_s,
            poll_interval_s=poll_interval_s,
        )
        entry.update(status=state["status"], **turn_reply(uuid.UUID(replay_turn_id)))
        on_progress()
        if not recorded.approved:
            continue
        if state["status"] != "awaiting_approval":
            raise TurnDiverged(
                {
                    "production_turn_id": recorded.turn_id,
                    "replay_turn_id": replay_turn_id,
                    "replay_status": state["status"],
                    "replay_reply": entry.get("reply"),
                    "reason": f"Production approved a draft after the creator's message "
                    f"{recorded.message[:80]!r}; the replay's turn ended {state['status']} "
                    f"({entry.get('reply_kind')}: {str(entry.get('reply'))[:160]!r}) instead",
                }
            )
        previous_job_id = _item_job_id(ids["item_id"])
        entry["approval_id"] = runner.run(
            approve_replayed_turn(
                thread_id=ids["thread_id"],
                user_id=ids["user_id"],
                turn_id=uuid.UUID(replay_turn_id),
                client_protocol=recorded.client_protocol,
                render_shape=render_shape if target else {},
            )
        )
        worker_state = wait_for_worker(
            item_id=ids["item_id"],
            session_id=ids["session_id"],
            timeout_s=timeout_s,
            poll_interval_s=poll_interval_s,
            previous_job_id=previous_job_id,
            turn_id=uuid.UUID(replay_turn_id),
        )
        job = worker_state.get("job") or {}
        entry["job"] = {key: job.get(key) for key in ("id", "status", "failure_reason")}
        on_progress()
        if target:
            break
        if stop := _stop_after_render(recorded, job):
            worker_state["stopped_early"] = {
                "after_production_turn": recorded.turn_id,
                "messages_replayed": index + 1,
                "messages_recorded": len(turns),
                "production_render": {
                    "job_id": recorded.job_id,
                    "failure_reason": recorded.render_failure,
                },
                "replay_render": entry["job"],
                "reason": stop,
            }
            return worker_state
        # Both renders failed: the creator read that before the next message.
        try:
            wait_for_turn(
                uuid.UUID(replay_turn_id),
                settled=TURN_SETTLED - {"awaiting_approval"},
                timeout_s=min(timeout_s, 300.0),
                poll_interval_s=poll_interval_s,
            )
        except ReplaySafetyError:
            entry["turn_settled_after_render"] = False
    return worker_state


FAILED_JOB_STATUSES = frozenset({"failed", "processing_failed"})


def _stop_after_render(recorded: RecordedTurn, job: dict[str, Any]) -> str | None:
    """Why the replay cannot send the creator's next message after this render, or None.

    The next message answered what production's render did. When the replay's render did
    something else, that message no longer fits; a render waiting for a phone also holds
    every later turn of the thread, since only a phone finishes it.
    """

    replay_failed = job.get("status") in FAILED_JOB_STATUSES
    if recorded.render_failure is not None and not replay_failed:
        return (
            f"Production's render after this message failed ({recorded.render_failure}); the "
            f"replay's ended {job.get('status')}. The creator's later messages answered that "
            "failure, so they are not sent and this render is the result."
        )
    if replay_failed and recorded.render_failure is None:
        return (
            f"The replay's render after this message failed ({job.get('failure_reason')}) "
            "where production's did not; the later messages followed production's render."
        )
    if job.get("status") == "awaiting_device":
        return (
            "This render waits for a phone (awaiting_device) and the next message would queue "
            "behind it, so the replay stops with this device plan."
        )
    return None


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
    previous_job_id: str | None = None,
    turn_id: uuid.UUID | None = None,
) -> dict[str, Any]:
    """Wait for the job a confirmation (or, with ``turn_id``, a Kria approval) dispatched.

    ``previous_job_id`` is the item's job before the approval: an earlier turn's render that
    already ended must not be read as this one.
    """

    from app import storage
    from app.database import sync_session
    from app.models import CreatorAgentTurn

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
            if previous_job_id is not None and job.get("id") == previous_job_id:
                job = {}
            if turn_id is not None:
                turn = db.get(CreatorAgentTurn, turn_id)
                turn_status = turn.status if turn is not None else None
                if not job and turn_status in TURN_SETTLED - {"awaiting_approval"}:
                    raise ReplaySafetyError(
                        f"The approved Kria turn ended {turn_status} without dispatching a "
                        f"render: {turn.error if turn is not None else None}"
                    )
            else:
                proposal = last.get("proposal") or {}
                session_state = last.get("session") or {}
                if not job and session_state.get("status") in {"failed", "cancelled"}:
                    raise ReplaySafetyError(
                        f"Creator execution stopped before dispatch: {session_state['status']}"
                    )
                if proposal.get("status") == "failed" and not job:
                    raise ReplaySafetyError(
                        "Proposal worker failed before dispatch: "
                        f"{proposal.get('failure') or proposal}"
                    )
            if job.get("status") in TERMINAL_JOB_STATUSES:
                last["job"] = job
                last["outputs"] = _output_receipts(last, storage, source_paths)
                return last
        time.sleep(min(max(poll_interval_s, 0.2), 30.0))
    raise ReplaySafetyError(f"Timed out waiting for proposal/worker; last state: {last}")


def device_plan_receipt(job: dict[str, Any] | None, output_dir: Path) -> dict[str, Any] | None:
    """The pinned iPhone render plan of a job that ended `awaiting_device`, else None."""

    if not job or job.get("status") != "awaiting_device":
        return None
    from app.services.device_render import DEVICE_RENDER_FIELD

    assembly_plan = job.get("assembly_plan") if isinstance(job.get("assembly_plan"), dict) else {}
    variants = [v for v in assembly_plan.get("variants") or [] if isinstance(v, dict)]
    records = assembly_plan.get(DEVICE_RENDER_FIELD)
    records = records if isinstance(records, dict) else {}
    variant_ids = [str(v.get("variant_id")) for v in variants]
    path = output_dir / "device-plan.json"
    path.write_text(
        json.dumps(
            {"job_id": job.get("id"), "variants": variants, "device_render": records},
            ensure_ascii=False,
            indent=2,
            default=str,
        ),
        encoding="utf-8",
    )
    return {
        "path": str(path),
        "variant_ids": variant_ids,
        "pinned_variant_ids": sorted(records),
        "complete": bool(variant_ids) and all(vid in records for vid in variant_ids),
    }


def snapshot_environment(snapshot_path: Path) -> dict[str, str]:
    """Environment variables that make ``Settings`` read the production snapshot values."""

    entries = _read_json(snapshot_path).get("settings")
    if not isinstance(entries, dict):
        raise ReplaySafetyError(f"Not a production flag snapshot: {snapshot_path}")
    return {
        name.upper(): json.dumps(entry["value"])
        if not isinstance(entry["value"], str)
        else entry["value"]
        for name, entry in entries.items()
        if entry.get("source") != "default"
    }


def build_environment(args: argparse.Namespace, *, output_dir: Path) -> dict[str, str]:
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
    _redis_target(redis_url)
    prod_flags = getattr(args, "prod_flags", None)
    if args.render and not args.enable_fidelity and prod_flags is None:
        raise ReplaySafetyError("--render requires --enable-fidelity or --prod-flags")
    environment: dict[str, str] = snapshot_environment(prod_flags) if prod_flags else {}
    environment.update(
        {
            "DATABASE_URL": database_url,
            "STORAGE_PROVIDER": "local",
            "STORAGE_BUCKET": LOCAL_BUCKET,
            "E2E_FIXTURES": "true",
            "LOCAL_STORAGE_ROOT": str(output_dir / "storage"),
            "LOCAL_STORAGE_BASE_URL": "http://127.0.0.1:8000/dev-qa/storage",
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
    if prod_flags is None:
        environment["CREATOR_PROMPT_FIDELITY_ENABLED"] = "true" if args.enable_fidelity else "false"
    return environment


def _configure_environment(args: argparse.Namespace, *, output_dir: Path) -> dict[str, Any]:
    environment = build_environment(args, output_dir=output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    os.environ.update(environment)
    database_url = environment["DATABASE_URL"]
    redis_host, redis_database = _redis_target(environment["REDIS_URL"])
    return {
        "database_url": database_url,
        "database_host": _database_target(database_url)[0],
        "database_name": _database_target(database_url)[1],
        "redis_url": environment["REDIS_URL"],
        "redis_host": redis_host,
        "redis_database": redis_database,
        "storage_provider": "local",
        "storage_bucket": LOCAL_BUCKET,
        "e2e_fixtures": True,
        "storage_root": environment["LOCAL_STORAGE_ROOT"],
        "destination_root": str(output_dir),
        "creator_prompt_fidelity_enabled": environment["CREATOR_PROMPT_FIDELITY_ENABLED"] == "true"
        if "CREATOR_PROMPT_FIDELITY_ENABLED" in environment
        else None,
        "prod_flag_snapshot": str(prod_flags)
        if (prod_flags := getattr(args, "prod_flags", None))
        else None,
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
    parser.add_argument(
        "--prod-flags",
        type=Path,
        default=None,
        help="flag snapshot (tests/prod_flags.snapshot.json); replaces --enable-fidelity",
    )
    parser.add_argument(
        "--thread-events",
        type=Path,
        default=None,
        help="production runtime-v2 thread events ({'events': [...]}); replays its messages "
        "turn by turn up to the job debug's kria_turn_id",
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
            "thread_events": str(args.thread_events.expanduser().resolve())
            if args.thread_events
            else None,
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
        thread_events = _read_json(args.thread_events).get("events") if args.thread_events else None
        if thread_events is not None and not args.render:
            raise ReplaySafetyError(
                "A runtime-v2 thread replay approves and renders: pass --render"
            )
        turns = (
            recorded_turns(thread_events, str(inputs.job_debug.get("kria_turn_id") or ""))
            if thread_events is not None
            else None
        )
        ids = {
            key: uuid.uuid4()
            for key in ("user_id", "thread_id", "plan_id", "item_id", "session_id")
        }
        receipt["seed"] = seed_replay_database(
            inputs=inputs,
            output_dir=output_dir,
            user_id=ids["user_id"],
            thread_id=ids["thread_id"],
            plan_id=ids["plan_id"],
            item_id=ids["item_id"],
            session_id=ids["session_id"],
            thread_events=thread_events,
        )
        _write_receipt(receipt_path, receipt)
        receipt["analysis"] = analyze_replay_media(item_id=ids["item_id"])
        _write_receipt(receipt_path, receipt)
        if turns is not None:
            receipt["turns"] = []
            worker_state = replay_recorded_thread(
                runner,
                ids=ids,
                turns=turns,
                render_shape=_render_shape(inputs.job_debug),
                timeout_s=args.timeout_s,
                poll_interval_s=args.poll_interval_s,
                log=receipt["turns"],
                on_progress=lambda: _write_receipt(receipt_path, receipt),
            )
            receipt["worker"] = worker_state
            receipt["job"] = worker_state.get("job")
            receipt["outputs"] = worker_state.get("outputs", [])
            receipt["device_plan"] = device_plan_receipt(worker_state.get("job"), output_dir)
            receipt["stopped_early"] = worker_state.get("stopped_early")
            receipt["finished_at"] = datetime.now(UTC).isoformat()
            _write_receipt(receipt_path, receipt)
            print(json.dumps({"receipt": str(receipt_path), "mode": "thread", "status": "ok"}))
            return 0
        plan = runner.run(
            run_creator_planning(
                item_id=ids["item_id"],
                seed_description=inputs.seed_description,
                user_id=ids["user_id"],
                timeout_s=args.timeout_s,
                poll_interval_s=args.poll_interval_s,
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
            receipt["device_plan"] = device_plan_receipt(worker_state.get("job"), output_dir)
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
        if isinstance(exc, MainCreatorQuestion):
            receipt["error"]["question"] = exc.question
        if isinstance(exc, TurnDiverged):
            receipt["error"]["diverged_turn"] = exc.detail
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
