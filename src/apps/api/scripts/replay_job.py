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
SCRATCH_DATABASE = "nova_creator_fidelity_test"
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


def storage_copy(bucket: str, object_path: str, destination: Path) -> None:
    """Read-only copy out of the bucket. The destination is always a local path."""

    if destination.is_file() and destination.stat().st_size > 0:
        return
    destination.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            "gcloud",
            "storage",
            "cp",
            "--no-clobber",
            f"gs://{bucket}/{object_path}",
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


def _source(path: Path, *, source_id: str, kind: str, object_path: str) -> dict[str, Any]:
    return {
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
    for assignment in item_debug.get("clip_assignments") or []:
        media_id = str(assignment.get("media_id") or "")
        kind = str(assignment.get("kind") or "video")
        object_path = str(assignment.get("gcs_path") or "")
        if not media_id or not object_path or kind not in MEDIA_KINDS or media_id in seen:
            continue
        seen.add(media_id)
        local = inputs_dir / "media" / media_id
        storage_copy(bucket, object_path, local)
        sources.append(_source(local, source_id=media_id, kind=kind, object_path=object_path))
    pool_prefix = f"users/{job['user_id']}/plan/{item_id}/pool/"
    pool_objects = storage_list(bucket, pool_prefix)
    for asset in item_debug.get("pool_assets") or []:
        if asset.get("kind") != "image" or asset.get("status") != "ready":
            continue
        match = next(
            (o for o in pool_objects if o.removeprefix(pool_prefix).startswith(asset["id"])), None
        )
        if match is None:
            raise ReplayJobError(f"Pool image {asset['id']} is not in {pool_prefix}")
        local = inputs_dir / "media" / f"{asset['id']}-{Path(match).name}"
        storage_copy(bucket, match, local)
        sources.append(_source(local, source_id=str(asset["id"]), kind="image", object_path=match))
    voiceover_path = item.get("voiceover_gcs_path")
    if voiceover_path:
        local = inputs_dir / "media" / Path(voiceover_path).name
        storage_copy(bucket, voiceover_path, local)
        sources.append(
            _source(
                local, source_id=Path(voiceover_path).stem, kind="audio", object_path=voiceover_path
            )
        )
    if not sources:
        raise ReplayJobError("The plan item holds no footage in storage to replay")

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
        "creator_request": creator_request,
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


def scratch_database_url(dotenv: dict[str, str]) -> str:
    """A local PostgreSQL URL for the scratch database that accepts a connection.

    Tries the credentials in ``.env`` first, then the operating-system account (a Homebrew
    Postgres has no ``postgres`` role). Anything not on localhost is refused.
    """

    from sqlalchemy import create_engine, text
    from sqlalchemy.exc import OperationalError

    configured = urlsplit(dotenv.get("DATABASE_URL") or os.environ.get("DATABASE_URL") or "")
    if (configured.hostname or "").lower() not in {"localhost", "127.0.0.1", "::1"}:
        raise ReplayJobError("DATABASE_URL in .env is not local; refusing to derive a scratch URL")
    host = f"localhost:{configured.port or 5432}"
    candidates = [configured._replace(path=f"/{SCRATCH_DATABASE}")]
    candidates.append(
        urlsplit(f"postgresql://{os.environ.get('USER', '')}@{host}/{SCRATCH_DATABASE}")
    )
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


def recreate_scratch_database(url: str, env: dict[str, str]) -> None:
    from sqlalchemy import create_engine, text

    maintenance = urlsplit(url)._replace(path="/postgres")
    engine = create_engine(urlunsplit(maintenance), isolation_level="AUTOCOMMIT")
    with engine.connect() as connection:
        connection.execute(text(f'DROP DATABASE IF EXISTS "{SCRATCH_DATABASE}" WITH (FORCE)'))
        connection.execute(text(f'CREATE DATABASE "{SCRATCH_DATABASE}"'))
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
    return {
        "replayed_job_status": job.get("status"),
        "failure_reason": job.get("failure_reason") or error,
        "error_detail": job.get("error_detail"),
        "video_paths": videos,
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
    try:
        info = collect_inputs(job_id, inputs_dir, _bucket(args.bucket))
        print(
            f"job {job_id}: status={info['job']['status']} "
            f"reason={info['job'].get('failure_reason')} sources={info['source_count']}"
        )
        if args.fetch_only:
            print(f"inputs: {inputs_dir}")
            return 0
        run_dir.mkdir(parents=True)
        scratch_url = scratch_database_url(dotenv)
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
        return 0 if result["video_paths"] and completed.returncode == 0 else 1
    except Exception as exc:  # noqa: BLE001 - a CLI driver reports any setup failure once
        print(f"replay failed before the render: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    finally:
        if worker is not None:
            stop_worker(worker)


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
