"""Replay a production creation job end to end from its job id (KRI-1375).

    make kria-replay-job JOB=<job id>

Done for a creation bug means the same prompt and footage produced a finished video under
production settings with the real models. This command does that from a failed job id:

1. Reads the job debug and the plan-item debug from production with
   ``scripts/admin.py --prod GET`` (GET only) and copies the item's footage out of the storage
   bucket with ``gcloud storage cp gs://... <local>`` (read only; nothing is ever written to it).
2. Writes the source manifest ``replay_creator.py`` consumes.
3. Recreates the scratch database, starts a Celery worker on its own queue namespace, and runs
   ``replay_creator.py --render`` under ``tests/prod_flags.snapshot.json``.
4. Prints the rendered video path, or the status and failure reason of the replayed job.

Everything the replay writes is local: scratch Postgres database, Redis database 14 under a
private key namespace, and a directory below /private/tmp. ``replay_creator`` refuses any other
target, and this driver hands it a clean environment so no production credential can reach it.

Fidelity limits: the replay drives the Main Creator route and the real orchestrator task, from
the footage the item holds in storage (the iPhone uploads analysis proxies, not the originals)
and the creator request recorded on the job. It does not replay the chat turns that led to the
request, and the snapshot approximates production settings (see
``scripts/prod_flag_snapshot.py``).
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
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

API_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = API_ROOT.parents[2]
SNAPSHOT = API_ROOT / "tests" / "prod_flags.snapshot.json"
WORK_ROOT = Path("/private/tmp/nova-replay-job")
SCRATCH_DATABASE_PREFIX = "nova_creator_fidelity_test_"
WORKER_QUEUES = (
    "celery,plan-jobs,overlay-jobs,creator-guided-jobs,creator-render-v2,agent-control,"
    "creator-fidelity-v1"
)
KEPT_ENV = ("PATH", "HOME", "TMPDIR", "LANG", "LC_ALL", "USER", "SHELL")
MODEL_KEY = re.compile(r"^[A-Z0-9_]+_API_KEY$")
NOT_MODEL_KEYS = ("ADMIN_", "INTERNAL_", "WAITLIST_")
MEDIA_KINDS = {"video", "image"}


class ReplayJobError(RuntimeError):
    pass


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


def collect_inputs(job_id: str, inputs_dir: Path, bucket: str) -> dict[str, Any]:
    job_debug = prod_get(f"/admin/jobs/{job_id}/debug")
    job = job_debug["job"]
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
    recorded = _recorded_media_ids(job)
    for assignment in item_debug.get("clip_assignments") or []:
        media_id = str(assignment.get("media_id") or "")
        kind = str(assignment.get("kind") or "video")
        object_path = str(assignment.get("gcs_path") or "")
        if not media_id or not object_path or kind not in MEDIA_KINDS or media_id in seen:
            continue
        if recorded is not None and _media_key(media_id) not in recorded:
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
    for asset in item_debug.get("pool_assets") or []:
        kind = str(asset.get("kind") or "")
        if kind not in MEDIA_KINDS or asset.get("status") != "ready":
            continue
        if recorded is not None and _media_key(str(asset["id"])) not in recorded:
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
    coverage = _check_source_coverage(job, sources, recorded)

    manifest = {"job_id": job_id, "creator_request": creator_request, "sources": sources}
    for name, value in (
        ("source-manifest.json", manifest),
        ("job-debug.json", job_debug),
        ("item-debug.json", item_debug),
    ):
        (inputs_dir / name).write_text(
            json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    return {
        "job": job,
        "item": item,
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


def _recorded_media_ids(job: dict[str, Any]) -> set[str] | None:
    """The clips and visuals the job was approved against, or None when it recorded none.

    The item's pool and clip rows may have changed since the job ran (assets added later),
    so the replay takes exactly these and nothing else.
    """

    snapshot = _binding_snapshot(job)
    rows = [*(snapshot.get("clip_assignments") or []), *(snapshot.get("visuals") or [])]
    ids = {_media_key(str(row["media_id"])) for row in rows if row.get("media_id")}
    return ids or None


def _check_source_coverage(
    job: dict[str, Any], sources: list[dict[str, Any]], recorded: set[str] | None
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
        missing = sorted(recorded - {_media_key(s["id"]) for s in visuals})
        if missing:
            raise ReplayJobError(
                f"The job was approved against {len(recorded)} visuals and clips but "
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
        "recorded_visuals_and_clips": len(recorded) if recorded is not None else None,
        "replayed_visuals_and_clips": len(visuals),
        "pool_images": sum(1 for s in visuals if s.get("pool") and s["kind"] == "image"),
        "pool_videos": sum(1 for s in visuals if s.get("pool") and s["kind"] == "video"),
        "voiceover": any(s["kind"] == "audio" for s in sources),
    }


def replay_environment(dotenv: dict[str, str], scratch_url: str, namespace: str) -> dict[str, str]:
    """Clean environment: model keys from .env, local scratch targets, nothing from production."""

    env = {key: os.environ[key] for key in KEPT_ENV if key in os.environ}
    for key, value in dotenv.items():
        if MODEL_KEY.fullmatch(key) and not key.startswith(NOT_MODEL_KEYS):
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
    from sqlalchemy.exc import OperationalError

    configured = urlsplit(dotenv.get("DATABASE_URL") or os.environ.get("DATABASE_URL") or "")
    if (configured.hostname or "").lower() not in {"localhost", "127.0.0.1", "::1"}:
        raise ReplayJobError("DATABASE_URL in .env is not local; refusing to derive a scratch URL")
    host = f"localhost:{configured.port or 5432}"
    candidates = [configured._replace(path=f"/{database}", query="", fragment="")]
    candidates.append(urlsplit(f"postgresql://{os.environ.get('USER', '')}@{host}/{database}"))
    for candidate in candidates:
        engine = create_engine(urlunsplit(candidate._replace(path="/postgres")))
        try:
            with engine.connect() as connection:
                connection.execute(text("SELECT 1"))
            return urlunsplit(candidate)
        except OperationalError:
            continue
        finally:
            engine.dispose()
    raise ReplayJobError("No local PostgreSQL role from .env or $USER can connect to `postgres`")


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


def drop_scratch_database(url: str) -> None:
    from sqlalchemy import text

    engine = _maintenance_engine(url)
    try:
        with engine.connect() as connection:
            connection.execute(text(f'DROP DATABASE IF EXISTS "{_scratch_name(url)}" WITH (FORCE)'))
    finally:
        engine.dispose()


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
    device_plan = receipt.get("device_plan")
    if videos:
        outcome = "video_rendered"
    elif question:
        outcome = "main_creator_question"
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
            f"coverage={json.dumps(info['source_coverage'])}"
        )
        if args.fetch_only:
            print(f"inputs: {inputs_dir}")
            return 0
        run_dir.mkdir(parents=True)
        scratch_url = scratch_database_url(
            dotenv, f"{SCRATCH_DATABASE_PREFIX}{uuid.uuid4().hex[:12]}"
        )
        env = replay_environment(dotenv, scratch_url, namespace)
        recreate_scratch_database(scratch_url, env)
        worker = start_worker(
            {**_creator_environment(env, run_dir, inputs_dir), **env}, run_dir, namespace
        )
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
            ],
            cwd=API_ROOT,
            env=env,
            check=False,
        )
        result = summarize(run_dir / "replay-receipt.json", run_dir)
        result["replay_exit_code"] = completed.returncode
        result["source_job"] = {"id": job_id, "failure_reason": info["job"].get("failure_reason")}
        print(json.dumps(result, indent=2, ensure_ascii=False))
        succeeded = result["outcome"] in {"video_rendered", "device_plan_ready"}
        return 0 if succeeded and completed.returncode == 0 else 1
    except Exception as exc:  # noqa: BLE001 - a CLI driver reports any setup failure once
        print(f"replay failed before the render: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    finally:
        if worker is not None:
            stop_worker(worker)
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
