"""Replay a production creation job end to end from its job id (KRI-1375).

    make kria-replay-job JOB=<job id>

Done for a creation bug means the same prompt and footage produced a finished video under
production settings with the real models. This command does that from a failed job id:

1. Reads the job debug, the plan-item debug and, for a job the app's chat made (runtime v2),
   the thread's events from production with ``scripts/admin.py --prod GET`` (GET only), and
   copies the item's footage out of the storage bucket with ``gcloud storage cp gs://...
   <local>`` (read only; nothing is ever written to it).
2. Writes the source manifest ``replay_creator.py`` consumes.
3. Recreates the scratch database, starts a Celery worker on its own queue namespace, and runs
   ``replay_creator.py --render`` under ``tests/prod_flags.snapshot.json``. A chat job is
   replayed message by message through the Kria runtime, each with the app build's client
   protocol, approving where the creator approved; an older job through the Main Creator.
4. Prints the rendered video path or device plan, or the status and failure reason of the
   replayed job, and whether it failed where production did.

Everything the replay writes is local: scratch Postgres database, Redis database 14 under a
private key namespace, and a directory below /private/tmp. ``replay_creator`` refuses any other
target, and this driver hands it a clean environment so no production credential can reach it.

Fidelity limits are listed in ``docs/runbooks/replay-from-job-id.md``.
"""

from __future__ import annotations

import argparse
import json
import mimetypes
import os
import re
import signal
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlsplit, urlunsplit

from sqlalchemy.exc import OperationalError

API_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = API_ROOT.parents[2]
SNAPSHOT = API_ROOT / "tests" / "prod_flags.snapshot.json"
WORK_ROOT = Path("/private/tmp/nova-replay-job")
SCRATCH_DATABASE_PREFIX = "nova_creator_fidelity_test_"
WORKER_QUEUES = (
    "celery,plan-jobs,overlay-jobs,creator-guided-jobs,creator-render-v2,agent-control,"
    "creator-fidelity-v1,speech-analysis,maintenance"
)
LOCAL_POSTGRES_PORT = 5432
KEPT_ENV = ("PATH", "HOME", "TMPDIR", "LANG", "LC_ALL", "USER", "SHELL")
MODEL_KEY = re.compile(r"^(?:GEMINI|OPENAI|ANTHROPIC)_API_KEY$")
REDIS_URL = "redis://localhost:6379/14"
REDIS_KEY_PREFIX = "nova-dev"
MIN_FREE_DB_CONNECTIONS = 20
MEDIA_KINDS = {"video", "image"}
DURATION_TOLERANCE_S = 0.15


class ReplayJobError(RuntimeError):
    pass


class ReplayInfrastructureError(ReplayJobError):
    """The local machine could not host the replay; says nothing about the production job."""


def _load_dotenv(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        value = value.strip()
        if value[:1] in {'"', "'"} and value[-1:] == value[:1] and len(value) > 1:
            value = value[1:-1]
        else:
            value = re.split(r"\s#", value, maxsplit=1)[0].rstrip()
        values[key.strip()] = value
    return values


def prod_get(path: str) -> dict[str, Any]:
    """The only way this driver talks to production: a GET through the admin CLI."""

    completed = subprocess.run(
        [sys.executable, str(REPO_ROOT / "scripts" / "admin.py"), "--prod", "GET", path],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    if completed.returncode != 0:
        raise ReplayJobError(f"prod GET {path} failed: {completed.stdout.strip()[:300]}")
    return json.loads(completed.stdout)


def _bucket(explicit: str | None) -> str:
    bucket = explicit or _load_dotenv(REPO_ROOT / ".env").get("STORAGE_BUCKET") or ""
    if not re.fullmatch(r"[a-z0-9][a-z0-9._-]{2,221}", bucket):
        raise ReplayJobError("Pass --bucket (production STORAGE_BUCKET); none found in .env")
    return bucket


def _owned_object_path(object_path: str, user_id: str) -> str:
    """A bucket path the job's owner holds; anything else is refused before it is fetched."""

    prefix = f"users/{user_id}/"
    rest = object_path[len(prefix) :]
    if (
        not object_path.startswith(prefix)
        or "\\" in object_path
        or any(part in {"", ".", ".."} for part in rest.split("/"))
    ):
        raise ReplayJobError(
            f"Refusing a storage path outside the job owner's prefix: {object_path}"
        )
    return object_path


def storage_copy(
    bucket: str, object_path: str, destination: Path, generation: str | None = None
) -> None:
    """Read-only copy out of the bucket. The destination is always a local path.

    A recorded generation pins the exact bytes the production job analysed.
    """

    if destination.is_file() and destination.stat().st_size > 0:
        return
    destination.parent.mkdir(parents=True, exist_ok=True)
    pinned = f"#{generation}" if generation and str(generation).isdigit() else ""
    subprocess.run(
        [
            "gcloud",
            "storage",
            "cp",
            "--no-clobber",
            f"gs://{bucket}/{object_path}{pinned}",
            str(destination),
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=600,
    )


def storage_list(bucket: str, prefix: str) -> list[str]:
    completed = subprocess.run(
        ["gcloud", "storage", "ls", f"gs://{bucket}/{prefix}"],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    return [
        line.strip().removeprefix(f"gs://{bucket}/")
        for line in completed.stdout.splitlines()
        if line.strip()
    ]


def _sha256(path: Path) -> str:
    import hashlib

    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _source(
    path: Path, *, source_id: str, kind: str, object_path: str, pool: bool = False
) -> dict[str, Any]:
    return {
        **({"pool": True} if pool else {}),
        "id": source_id,
        "kind": kind,
        "local_path": str(path),
        "size": path.stat().st_size,
        "sha256": _sha256(path),
        "content_type": mimetypes.guess_type(path.name)[0] or "",
        "source_filename": path.name,
        "production_object": object_path,
    }


THREAD_EVENT_PAGES = 10


def production_thread_events(thread_id: str) -> list[dict[str, Any]]:
    """Every event of the production creation thread, oldest first (GET only)."""

    events: list[dict[str, Any]] = []
    cursor: str | None = None
    for _ in range(THREAD_EVENT_PAGES):
        suffix = f"&cursor={quote(cursor, safe='')}" if cursor else ""
        page = prod_get(f"/admin/creation-threads/{uuid.UUID(thread_id)}/events?limit=500{suffix}")
        events.extend(page.get("events") or [])
        cursor = page.get("next_cursor")
        if not cursor:
            return events
    raise ReplayJobError(f"Thread {thread_id} has more than {THREAD_EVENT_PAGES * 500} events")


def collect_inputs(job_id: str, inputs_dir: Path, bucket: str) -> dict[str, Any]:
    job_debug = prod_get(f"/admin/jobs/{job_id}/debug")
    job = job_debug["job"]
    thread_events: list[dict[str, Any]] | None = None
    if job_debug.get("runtime_version") == 2:
        # The app's chat thread made this job: its messages, not the compiled request, are
        # what the replay sends.
        if not job_debug.get("thread_id") or not job_debug.get("kria_turn_id"):
            raise ReplayJobError(
                "A runtime-v2 job without a thread and Kria turn id cannot be replayed turn by turn"
            )
        thread_events = production_thread_events(str(job_debug["thread_id"]))
    item_id = job.get("content_plan_item_id")
    if not item_id:
        raise ReplayJobError(
            f"Job {job_id} has no content_plan_item_id (job_type={job.get('job_type')}); "
            "only plan-item creation jobs can be replayed"
        )
    creator_request = str((job.get("all_candidates") or {}).get("creator_request") or "").strip()
    if not creator_request:
        raise ReplayJobError("The job records no creator_request to replay")
    item_debug = prod_get(f"/admin/plan-items/{item_id}/debug")
    item = item_debug["item"]
    # replay_creator seeds the item with this status; a finished one would skip planning.
    item_debug["item"] = {**item, "item_status": "awaiting_clips"}

    sources: list[dict[str, Any]] = []
    seen: set[str] = set()
    user_id = str(job["user_id"])
    recorded = _recorded_media(job)
    clip_rows = [
        a
        for a in item_debug.get("clip_assignments") or []
        if a.get("media_id") and a.get("gcs_path") and (a.get("kind") or "video") in MEDIA_KINDS
    ]
    pool_rows = [
        a
        for a in item_debug.get("pool_assets") or []
        if a.get("kind") in MEDIA_KINDS and a.get("status") == "ready"
    ]
    chosen = _select_recorded_media(recorded, clip_rows, pool_rows)
    _name_unnamed_binding_rows(job_debug, chosen)
    for assignment in clip_rows:
        media_id = str(assignment["media_id"])
        kind = str(assignment.get("kind") or "video")
        object_path = str(assignment["gcs_path"])
        if media_id in seen or (chosen is not None and _media_key(media_id) not in chosen.clips):
            continue
        seen.add(media_id)
        local = inputs_dir / "media" / media_id
        storage_copy(
            bucket,
            _owned_object_path(object_path, user_id),
            local,
            assignment.get("generation"),
        )
        sources.append(_source(local, source_id=media_id, kind=kind, object_path=object_path))
    pool_prefix = f"users/{user_id}/plan/{item_id}/pool/"
    pool_objects: list[str] | None = None
    for asset in pool_rows:
        kind = str(asset["kind"])
        if chosen is not None and _media_key(str(asset["id"])) not in chosen.pool:
            continue
        object_path = asset.get("gcs_path")
        generation = asset.get("gcs_generation")
        if not object_path:
            # A production API from before the item debug exposed pool paths: the item's own
            # pool prefix is the only place to look, and library assets are not under it.
            if pool_objects is None:
                pool_objects = storage_list(bucket, pool_prefix)
            object_path = next(
                (o for o in pool_objects if o.removeprefix(pool_prefix).startswith(asset["id"])),
                None,
            )
            if object_path is None:
                raise ReplayJobError(
                    f"Pool {kind} {asset['id']} has no storage path in the item debug and is "
                    f"not under {pool_prefix}; it is a media-library asset and this production "
                    "API predates the gcs_path field on /admin/plan-items/<id>/debug"
                )
        local = inputs_dir / "media" / f"{asset['id']}-{Path(object_path).name}"
        storage_copy(bucket, _owned_object_path(object_path, user_id), local, generation)
        sources.append(
            _source(
                local, source_id=str(asset["id"]), kind=kind, object_path=object_path, pool=True
            )
        )
    voiceover_path = item.get("voiceover_gcs_path")
    snapshot = _binding_snapshot(job)
    job_used_voiceover = bool(snapshot.get("voiceover") or snapshot.get("voiceover_path"))
    if voiceover_path and (recorded is None or job_used_voiceover):
        voiceover_path = _owned_object_path(str(voiceover_path), user_id)
        local = inputs_dir / "media" / Path(voiceover_path).name
        storage_copy(bucket, voiceover_path, local)
        sources.append(
            _source(
                local, source_id=Path(voiceover_path).stem, kind="audio", object_path=voiceover_path
            )
        )
    if not sources:
        raise ReplayJobError("The plan item holds no footage in storage to replay")
    coverage = _check_source_coverage(job, sources, recorded, chosen)

    manifest = {"job_id": job_id, "creator_request": creator_request, "sources": sources}
    for name, value in (
        ("source-manifest.json", manifest),
        ("job-debug.json", job_debug),
        ("item-debug.json", item_debug),
        *((("thread-events.json", {"events": thread_events}),) if thread_events else ()),
    ):
        (inputs_dir / name).write_text(
            json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    return {
        "job": job,
        "item": item,
        "item_id": str(item_id),
        "thread_events": inputs_dir / "thread-events.json" if thread_events else None,
        "creator_messages": sum(1 for e in thread_events or [] if e.get("kind") == "user_message"),
        "job_agent_names": sorted(
            {r["agent_name"] for r in job_debug.get("agent_runs") or [] if r.get("agent_name")}
        ),
        "source_count": len(sources),
        "source_coverage": coverage,
        "creator_request": creator_request,
    }


def _media_key(media_id: str) -> str:
    """Pool assets are recorded as ``asset-<uuid>`` in the brief binding and bare in the debug."""

    return media_id.removeprefix("asset-")


def _binding_snapshot(job: dict[str, Any]) -> dict[str, Any]:
    plan = job.get("assembly_plan") or {}
    return ((plan.get("creator_brief_binding") or {}).get("media_snapshot")) or {}


@dataclass(frozen=True)
class RecordedMedia:
    """The media a job was approved against, as its brief binding recorded it.

    Older bindings name every row (``media_id``); newer ones keep only the kind, duration and
    upload receipt of a clip and the kind of a visual, so those rows are matched to the
    item's media by shape instead of by id.
    """

    named: frozenset[str]
    unnamed_clips: tuple[dict[str, Any], ...]
    unnamed_visuals: tuple[dict[str, Any], ...]

    @property
    def total(self) -> int:
        return len(self.named) + len(self.unnamed_clips) + len(self.unnamed_visuals)


@dataclass(frozen=True)
class ChosenMedia:
    clips: frozenset[str]
    pool: frozenset[str]
    matched_clip_rows: tuple[tuple[dict[str, Any], str], ...]


def _recorded_media(job: dict[str, Any]) -> RecordedMedia | None:
    """What the job was approved against, or None when it recorded no media at all.

    The item's pool and clip rows may have changed since the job ran (assets added later),
    so the replay takes exactly these and nothing else.
    """

    snapshot = _binding_snapshot(job)
    clips = [r for r in snapshot.get("clip_assignments") or [] if isinstance(r, dict)]
    visuals = [r for r in snapshot.get("visuals") or [] if isinstance(r, dict)]
    named = frozenset(
        _media_key(str(r["media_id"])) for r in [*clips, *visuals] if r.get("media_id")
    )
    recorded = RecordedMedia(
        named=named,
        unnamed_clips=tuple(r for r in clips if not r.get("media_id")),
        unnamed_visuals=tuple(r for r in visuals if not r.get("media_id")),
    )
    return recorded if recorded.total else None


def _select_recorded_media(
    recorded: RecordedMedia | None,
    clip_rows: list[dict[str, Any]],
    pool_rows: list[dict[str, Any]],
) -> ChosenMedia | None:
    """Pick the item's media rows the job used; refuse when they cannot be told apart.

    A clip row without an id matches an item clip of the same kind whose duration agrees
    within ``DURATION_TOLERANCE_S``; two equally close candidates are ambiguous. Visuals
    without ids are item pool assets, matched by kind: the item must hold exactly as many ready
    assets of that kind as the binding recorded, otherwise which ones the job used is unknown.
    """

    if recorded is None:
        return None
    clips = {_media_key(str(r["media_id"])) for r in clip_rows} & recorded.named
    pool = {_media_key(str(r["id"])) for r in pool_rows} & recorded.named
    matched: list[tuple[dict[str, Any], str]] = []
    for row in recorded.unnamed_clips:
        kind = str(row.get("kind") or "video")
        duration = row.get("duration_s")
        scored = sorted(
            (
                abs(float(candidate.get("duration_s") or 0) - float(duration or 0)),
                str(candidate["media_id"]),
            )
            for candidate in clip_rows
            if str(candidate.get("kind") or "video") == kind
            and _media_key(str(candidate["media_id"])) not in clips
            and (
                duration is None
                or abs(float(candidate.get("duration_s") or 0) - float(duration))
                <= DURATION_TOLERANCE_S
            )
        )
        if not scored:
            raise ReplayJobError(
                f"The job recorded a {kind} clip of {duration} s without a media id and the "
                "item holds no unclaimed clip of that kind and length; refusing to replay on "
                "different footage"
            )
        if len(scored) > 1 and abs(scored[0][0] - scored[1][0]) < 0.01:
            raise ReplayJobError(
                f"Two item clips match the recorded {kind} clip of {duration} s equally; "
                "which one the job used is unknown"
            )
        clips.add(_media_key(scored[0][1]))
        matched.append((row, scored[0][1]))
    for kind in sorted({str(r.get("kind") or "") for r in recorded.unnamed_visuals}):
        wanted = sum(1 for r in recorded.unnamed_visuals if str(r.get("kind") or "") == kind)
        held = [r for r in pool_rows if r["kind"] == kind and _media_key(str(r["id"])) not in pool]
        if len(held) != wanted:
            raise ReplayJobError(
                f"The job recorded {wanted} pool {kind}s without media ids and the item holds "
                f"{len(held)} ready ones now; which ones the job used is unknown, so the replay "
                "would run on different footage"
            )
        pool |= {_media_key(str(r["id"])) for r in held}
    return ChosenMedia(frozenset(clips), frozenset(pool), tuple(matched))


def _name_unnamed_binding_rows(job_debug: dict[str, Any], chosen: ChosenMedia | None) -> None:
    """Give matched binding clip rows their item media id so their upload receipts restore."""

    for row, media_id in chosen.matched_clip_rows if chosen else ():
        row["media_id"] = media_id
        row["replay_matched_by"] = "kind_and_duration"


def _check_source_coverage(
    job: dict[str, Any],
    sources: list[dict[str, Any]],
    recorded: RecordedMedia | None,
    chosen: ChosenMedia | None,
) -> dict[str, Any]:
    """Refuse a replay that would run on different inputs than the production job.

    The job's brief binding records the media it was approved against. Every one of them
    must have a replayed source, and a creator song or voiceover the replay cannot reproduce
    stops the run: a green replay on partial footage is stand-in evidence. A job without a
    binding replays everything the item holds, and the coverage says so.
    """

    snapshot = _binding_snapshot(job)
    visuals = [s for s in sources if s["kind"] in MEDIA_KINDS]
    if recorded is not None:
        missing = sorted(recorded.named - {_media_key(s["id"]) for s in visuals})
        if missing:
            raise ReplayJobError(
                f"The job was approved against {recorded.total} visuals and clips but "
                f"{len(missing)} are not recoverable ({', '.join(missing[:5])}); refusing to "
                "replay on different footage"
            )
    if any(snapshot.get(key) for key in ("song_analysis", "song_alignment", "song_generation")):
        raise ReplayJobError(
            "The job used a creator song; replay_creator does not seed songs, so a replay "
            "would not reproduce the production inputs"
        )
    if snapshot.get("voiceover") and not any(s["kind"] == "audio" for s in sources):
        raise ReplayJobError("The job recorded a voiceover but the item holds none in storage")
    return {
        "bound_to_recorded_media": recorded is not None,
        "recorded_visuals_and_clips": recorded.total if recorded is not None else None,
        "replayed_visuals_and_clips": len(visuals),
        "matched_without_media_id": {
            "clips": len(chosen.matched_clip_rows) if chosen else 0,
            "pool_visuals": len(recorded.unnamed_visuals) if recorded else 0,
        },
        "pool_images": sum(1 for s in visuals if s.get("pool") and s["kind"] == "image"),
        "pool_videos": sum(1 for s in visuals if s.get("pool") and s["kind"] == "video"),
        "voiceover": any(s["kind"] == "audio" for s in sources),
    }


def replay_environment(dotenv: dict[str, str], scratch_url: str, namespace: str) -> dict[str, str]:
    """Clean environment: model provider keys from .env, local scratch targets, nothing else."""

    env = {key: os.environ[key] for key in KEPT_ENV if key in os.environ}
    for key, value in dotenv.items():
        if MODEL_KEY.fullmatch(key):
            env[key] = value
    if dotenv.get("TOKEN_ENCRYPTION_KEY"):
        env["TOKEN_ENCRYPTION_KEY"] = dotenv["TOKEN_ENCRYPTION_KEY"]
    env["DATABASE_URL"] = scratch_url
    env["NOVA_CELERY_QUEUE_NAMESPACE"] = namespace
    env["PYTHONUNBUFFERED"] = "1"
    env["PYTHONPATH"] = str(API_ROOT)
    return env


def scratch_database_url(dotenv: dict[str, str], database: str) -> str:
    """A local PostgreSQL URL for the scratch database that accepts a connection.

    Tries the credentials in ``.env`` first, then the operating-system account (a Homebrew
    Postgres has no ``postgres`` role). Anything not on localhost is refused, and the derived
    URL never keeps the ``.env`` query string, which libpq lets override the host.
    """

    from sqlalchemy import create_engine, text

    configured = urlsplit(dotenv.get("DATABASE_URL") or os.environ.get("DATABASE_URL") or "")
    if (configured.hostname or "").lower() not in {"localhost", "127.0.0.1", "::1"}:
        raise ReplayJobError("DATABASE_URL in .env is not local; refusing to derive a scratch URL")
    if (configured.port or LOCAL_POSTGRES_PORT) != LOCAL_POSTGRES_PORT:
        raise ReplayJobError(
            f"DATABASE_URL in .env uses port {configured.port}, not the local dev server's "
            f"{LOCAL_POSTGRES_PORT}; a tunnel (fly proxy) also listens on localhost, so the "
            "scratch database is not created there"
        )
    host = f"localhost:{LOCAL_POSTGRES_PORT}"
    candidates = [
        configured._replace(scheme="postgresql", path=f"/{database}", query="", fragment="")
    ]
    candidates.append(urlsplit(f"postgresql://{os.environ.get('USER', '')}@{host}/{database}"))
    for candidate in candidates:
        engine = create_engine(urlunsplit(candidate._replace(path="/postgres")))
        try:
            with engine.connect() as connection:
                connection.execute(text("SELECT 1"))
                free = _free_connections(connection)
            if free < MIN_FREE_DB_CONNECTIONS:
                raise ReplayInfrastructureError(
                    f"Local PostgreSQL has only {free} free connections (the worker and the "
                    f"replay need {MIN_FREE_DB_CONNECTIONS}); other sessions hold the rest"
                )
            return urlunsplit(candidate)
        except OperationalError:
            continue
        finally:
            engine.dispose()
    raise ReplayInfrastructureError(
        "No local PostgreSQL role from .env or $USER can connect to `postgres`"
    )


def _free_connections(connection: Any) -> int:
    from sqlalchemy import text

    row = connection.execute(
        text("SELECT current_setting('max_connections')::int - count(*) FROM pg_stat_activity")
    ).one()
    return int(row[0])


def _scratch_name(url: str) -> str:
    name = urlsplit(url).path.lstrip("/")
    if not re.fullmatch(rf"{SCRATCH_DATABASE_PREFIX}[a-z0-9]{{1,16}}", name):
        raise ReplayJobError(f"Refusing to drop or create database {name!r}")
    return name


def _maintenance_engine(url: str):  # noqa: ANN202 - sqlalchemy Engine, imported lazily
    from sqlalchemy import create_engine

    return create_engine(
        urlunsplit(urlsplit(url)._replace(path="/postgres", query="")),
        isolation_level="AUTOCOMMIT",
    )


def recreate_scratch_database(url: str, env: dict[str, str]) -> None:
    from sqlalchemy import text

    name = _scratch_name(url)
    engine = _maintenance_engine(url)
    with engine.connect() as connection:
        connection.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        connection.execute(text(f'CREATE DATABASE "{name}"'))
    engine.dispose()
    subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=API_ROOT,
        env={**env, "STORAGE_BUCKET": "replay-scratch"},
        check=True,
        capture_output=True,
        text=True,
        timeout=900,
    )


def drop_scratch_database(url: str, attempts: int = 5) -> None:
    """Drop the run's scratch database; a busy server (connection limit) is retried."""

    from sqlalchemy import text

    for attempt in range(1, attempts + 1):
        engine = _maintenance_engine(url)
        try:
            with engine.connect() as connection:
                connection.execute(
                    text(f'DROP DATABASE IF EXISTS "{_scratch_name(url)}" WITH (FORCE)')
                )
            return
        except OperationalError:
            if attempt == attempts:
                raise
            time.sleep(3 * attempt)
        finally:
            engine.dispose()


def delete_queue_namespace(namespace: str) -> int:
    """Remove the Redis keys (broker bindings, unacked index) this run's worker left behind."""

    if not re.fullmatch(r"replay-[0-9a-f]{8}-[0-9a-z]+", namespace):
        raise ReplayJobError(f"Refusing to delete Redis keys for namespace {namespace!r}")
    import redis

    client = redis.Redis.from_url(REDIS_URL)
    try:
        keys = list(client.scan_iter(match=f"{REDIS_KEY_PREFIX}:{namespace}:*", count=500))
        return int(client.delete(*keys)) if keys else 0
    finally:
        client.close()


def start_worker(env: dict[str, str], run_dir: Path, namespace: str) -> subprocess.Popen[bytes]:
    log = (run_dir / "worker.log").open("ab")
    return subprocess.Popen(
        [
            sys.executable,
            "-m",
            "celery",
            "-A",
            "app.worker:celery_app",
            "worker",
            "--loglevel=info",
            "--concurrency=4",
            "--pool=threads",
            "-Q",
            WORKER_QUEUES,
            "-n",
            f"{namespace}@%h",
        ],
        cwd=API_ROOT,
        env=env,
        stdout=log,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )


def stop_worker(worker: subprocess.Popen[bytes]) -> None:
    if worker.poll() is None:
        os.killpg(worker.pid, signal.SIGTERM)
        try:
            worker.wait(timeout=30)
        except subprocess.TimeoutExpired:
            os.killpg(worker.pid, signal.SIGKILL)


PRODUCTION_TRACE_PAGES = 10
AGENT_RUNS_CODE = """
import json
from app.agents._runtime import SUCCESS_OUTCOMES
from app.database import sync_session
from app.models import AgentRun

with sync_session() as db:
    runs = [
        {"agent_name": r.agent_name, "outcome": r.outcome, "cost_usd": float(r.cost_usd or 0)}
        for r in db.query(AgentRun).order_by(AgentRun.created_at)
    ]
print(json.dumps({"runs": runs, "success": sorted(SUCCESS_OUTCOMES)}))
"""


def production_agents(item_id: str, job_agent_names: list[str]) -> tuple[set[str], bool]:
    """Agent names production ran for the plan item (all pages of its proposal trace).

    The second value is False when the trace was cut off, so an absence proves nothing.
    """

    names = set(job_agent_names)
    cursor: str | None = None
    for _ in range(PRODUCTION_TRACE_PAGES):
        suffix = f"&cursor={quote(cursor, safe='')}" if cursor else ""
        page = prod_get(f"/admin/plan-items/{item_id}/proposal-trace?limit=100{suffix}")
        names |= {r["agent_name"] for r in page.get("agent_runs") or [] if r.get("agent_name")}
        cursor = page.get("next_cursor")
        if not cursor:
            return names, True
    return names, False


def replay_agents(env: dict[str, str]) -> dict[str, Any]:
    """The agent runs the replay recorded in its scratch database."""

    completed = subprocess.run(
        [sys.executable, "-c", AGENT_RUNS_CODE],
        cwd=API_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    if completed.returncode != 0:
        raise ReplayJobError(f"Could not read the replay's agent runs: {completed.stderr[-300:]}")
    return json.loads(completed.stdout.strip().splitlines()[-1])


def compare_routes(
    production: set[str], trace_complete: bool, recorded: dict[str, Any]
) -> dict[str, Any]:
    """Did the replay fail inside an agent production never ran for this plan item?

    The replay drives the same controllers as the chat, but a flag the snapshot approximates
    can send it down a planner production does not use. A failure there is the replay's own, so
    it is reported as a divergence instead of as the production job's failure reason.
    """

    success = set(recorded["success"])
    last_outcome: dict[str, str] = {}
    for run in recorded["runs"]:
        last_outcome[run["agent_name"]] = run["outcome"]
    ran = sorted(last_outcome)
    failed = sorted(name for name, outcome in last_outcome.items() if outcome not in success)
    return {
        "production_agents": sorted(production),
        "production_trace_complete": trace_complete,
        "replay_agents": ran,
        "replay_only_agents": sorted(set(ran) - production),
        "replay_failed_agents": failed,
        "diverged_failures": sorted(set(failed) - production) if trace_complete else [],
        "replay_agent_runs": len(recorded["runs"]),
        "replay_spend_usd": round(sum(r.get("cost_usd") or 0 for r in recorded["runs"]), 4),
    }


def classify_failure(
    result: dict[str, Any], route: dict[str, Any], production_reason: str | None
) -> None:
    """Say whether a failed replay is the production job's failure or one of its own.

    A failure inside an agent production never ran, or a different failure reached after
    such agents, did not happen where production failed: printing it as the job's
    remaining failure would be stand-in evidence.
    """

    if result["outcome"] != "failed":
        return
    reproduced = result["failure_reason"] == production_reason
    result["production_failure_reproduced"] = reproduced
    if route["diverged_failures"]:
        where = f"inside {', '.join(route['diverged_failures'])}, which production never ran"
    elif not reproduced and route["production_trace_complete"] and route["replay_only_agents"]:
        where = (
            f"with {result['failure_reason']} after running "
            f"{', '.join(route['replay_only_agents'])}, which production never ran (production "
            f"failed with {production_reason})"
        )
    else:
        return
    result["replay_failure"] = {
        "failure_reason": result["failure_reason"],
        "error_detail": result["error_detail"],
    }
    result["outcome"] = "diverged_from_production"
    result["failure_reason"] = (
        f"The replay failed {where} for this plan item. It did not fail where production did, "
        "so it is not the production job's failure: replay_failure is what the replay hit; "
        "check the flag snapshot (scripts/prod_flag_snapshot.py unverified) and the turns."
    )


def summarize(receipt_path: Path, run_dir: Path) -> dict[str, Any]:
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    job = receipt.get("job") or {}
    videos = [
        str(run_dir / "storage" / out["object_path"])
        for out in receipt.get("outputs") or []
        if out.get("field") in {"output_url", "video_path"} and not out.get("missing")
    ]
    error = (receipt.get("error") or {}).get("message")
    question = (receipt.get("error") or {}).get("question")
    diverged_turn = (receipt.get("error") or {}).get("diverged_turn")
    device_plan = receipt.get("device_plan")
    if videos:
        outcome = "video_rendered"
    elif question:
        outcome = "main_creator_question"
    elif diverged_turn:
        outcome = "diverged_from_production"
    elif device_plan and device_plan.get("complete"):
        outcome = "device_plan_ready"
    elif device_plan:
        outcome = "device_plan_incomplete"
        error = error or "The job reached awaiting_device without a pinned device request"
    else:
        outcome = "failed"
    return {
        "outcome": outcome,
        "replayed_job_status": job.get("status"),
        "failure_reason": None
        if outcome in {"video_rendered", "device_plan_ready"}
        else job.get("failure_reason") or error,
        "error_detail": job.get("error_detail"),
        "video_paths": videos,
        "device_plan": device_plan,
        "main_creator_question": question,
        "diverged_turn": diverged_turn,
        "creator_client_protocol": (job.get("all_candidates") or {}).get("creator_client_protocol"),
        "turns": receipt.get("turns"),
        "stopped_early": receipt.get("stopped_early"),
        "receipt": str(receipt_path),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawTextHelpFormatter
    )
    parser.add_argument("job_id", help="production job id (uuid)")
    parser.add_argument(
        "--bucket", default=None, help="production bucket (default: STORAGE_BUCKET in .env)"
    )
    parser.add_argument("--timeout-s", type=float, default=1800.0)
    parser.add_argument("--fetch-only", action="store_true", help="stop after writing the inputs")
    args = parser.parse_args(argv)
    try:
        job_id = str(uuid.UUID(args.job_id))
    except ValueError:
        print(f"not a job id: {args.job_id!r}", file=sys.stderr)
        return 2

    signal.signal(signal.SIGTERM, lambda *_: sys.exit(143))
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    work = WORK_ROOT / job_id
    inputs_dir, run_dir = work / "inputs", work / "runs" / stamp
    namespace = f"replay-{job_id[:8]}-{stamp.lower()}"
    dotenv = _load_dotenv(REPO_ROOT / ".env")
    worker: subprocess.Popen[bytes] | None = None
    scratch_url: str | None = None
    try:
        info = collect_inputs(job_id, inputs_dir, _bucket(args.bucket))
        print(
            f"job {job_id}: status={info['job']['status']} "
            f"reason={info['job'].get('failure_reason')} sources={info['source_count']} "
            f"creator_messages={info['creator_messages']} "
            f"coverage={json.dumps(info['source_coverage'])}"
        )
        if args.fetch_only:
            print(f"inputs: {inputs_dir}")
            return 0
        production, trace_complete = production_agents(info["item_id"], info["job_agent_names"])
        run_dir.mkdir(parents=True)
        scratch_url = scratch_database_url(
            dotenv, f"{SCRATCH_DATABASE_PREFIX}{uuid.uuid4().hex[:12]}"
        )
        env = replay_environment(dotenv, scratch_url, namespace)
        worker_env = {**_creator_environment(env, run_dir, inputs_dir), **env}
        try:
            recreate_scratch_database(scratch_url, env)
        except (OperationalError, subprocess.CalledProcessError) as exc:
            raise ReplayInfrastructureError(
                f"Could not create the scratch database: {type(exc).__name__}"
            ) from exc
        worker = start_worker(worker_env, run_dir, namespace)
        completed = subprocess.run(
            [
                sys.executable,
                str(API_ROOT / "scripts" / "replay_creator.py"),
                "--manifest",
                str(inputs_dir / "source-manifest.json"),
                "--job-debug",
                str(inputs_dir / "job-debug.json"),
                "--item-debug",
                str(inputs_dir / "item-debug.json"),
                "--output-dir",
                str(run_dir),
                "--prod-flags",
                str(SNAPSHOT),
                "--database-url",
                scratch_url,
                "--allow-source-count-drift",
                "--render",
                "--timeout-s",
                str(args.timeout_s),
                *(("--thread-events", str(info["thread_events"])) if info["thread_events"] else ()),
            ],
            cwd=API_ROOT,
            env=env,
            check=False,
        )
        result = summarize(run_dir / "replay-receipt.json", run_dir)
        result["replay_exit_code"] = completed.returncode
        route = compare_routes(production, trace_complete, replay_agents(worker_env))
        result["route_comparison"] = route
        # A replay that stopped at an earlier render is compared with production's render there.
        compared = (result.get("stopped_early") or {}).get("production_render") or {
            "job_id": job_id,
            "failure_reason": info["job"].get("failure_reason"),
        }
        result["compared_production_render"] = compared
        classify_failure(result, route, compared["failure_reason"])
        result["source_job"] = {
            "id": job_id,
            "failure_reason": info["job"].get("failure_reason"),
            "creator_client_protocol": (info["job"].get("all_candidates") or {}).get(
                "creator_client_protocol"
            ),
            "replayed_route": "kria_thread_turns" if info["thread_events"] else "main_creator",
        }
        print(json.dumps(result, indent=2, ensure_ascii=False))
        succeeded = result["outcome"] in {"video_rendered", "device_plan_ready"}
        return 0 if succeeded and completed.returncode == 0 else 1
    except ReplayInfrastructureError as exc:
        print(f"replay not run, local infrastructure: {exc}", file=sys.stderr)
        return 3
    except Exception as exc:  # noqa: BLE001 - a CLI driver reports any setup failure once
        print(f"replay failed before the render: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    finally:
        if worker is not None:
            stop_worker(worker)
            try:
                delete_queue_namespace(namespace)
            except Exception as exc:  # noqa: BLE001 - cleanup must not mask the result
                print(f"redis keys not deleted: {exc}", file=sys.stderr)
        if scratch_url is not None:
            try:
                drop_scratch_database(scratch_url)
            except Exception as exc:  # noqa: BLE001 - cleanup must not mask the result
                print(f"scratch database not dropped: {exc}", file=sys.stderr)


def _creator_environment(env: dict[str, str], run_dir: Path, inputs_dir: Path) -> dict[str, str]:
    """The environment replay_creator would export, so the worker sees the same settings."""

    sys.path.insert(0, str(API_ROOT / "scripts"))
    import replay_creator  # noqa: PLC0415

    parsed = replay_creator.build_parser().parse_args(
        [
            "--manifest",
            str(inputs_dir / "source-manifest.json"),
            "--output-dir",
            str(run_dir),
            "--prod-flags",
            str(SNAPSHOT),
            "--database-url",
            env["DATABASE_URL"],
            "--render",
        ]
    )
    return replay_creator.build_environment(parsed, output_dir=run_dir)


if __name__ == "__main__":
    raise SystemExit(main())
