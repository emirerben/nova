#!/usr/bin/env python3
"""Clone one production plan item into the local E2E database.

This is deliberately a *read-production / write-local* tool.  It only issues
GET requests to the production admin API and only writes the database selected
by the local ``DATABASE_URL`` plus a caller-selected local media directory.
The explicit ``--allow-prod-read`` switch is required even for dry runs.

The clone is intended for the guided editor dogfood flow, not as a general
backup format.  It keeps the production item/job UUIDs so the real URL can be
opened, remaps ownership to the local QA account, and rewrites every media path
to ``dev-qa/production-clones/<job-id>/...``.  A local filesystem adapter can
serve those paths without granting the local renderer production write access.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import re
import shutil
import sys
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
API_ROOT = REPO_ROOT / "src" / "apps" / "api"
if str(API_ROOT) not in sys.path:
    sys.path.insert(0, str(API_ROOT))

ITEM_ID = "d4ceb1e8-8888-4d07-9433-bab098b20d05"
JOB_ID = "f849d674-1f28-4b71-b7b2-ea0b9952b2d4"
QA_EMAIL = "qa@example.com"
PROD_BASE = "https://nova-video.fly.dev"
LOCAL_MEDIA_PREFIX = "dev-qa/production-clones"
CLONE_MARKER = "nova_local_production_clone_v1"


def _load_env() -> dict[str, str]:
    """Load only simple .env values; never print their contents."""
    out: dict[str, str] = {}
    path = REPO_ROOT / ".env"
    if not path.exists():
        return out
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        out[key.strip()] = value
    return out


def _require_prod_read(args: argparse.Namespace) -> None:
    if not args.allow_prod_read:
        raise RuntimeError(
            "Refusing production access. Re-run with --allow-prod-read; this tool only GETs "
            "production admin endpoints and never writes production."
        )


def _prod_get(path: str, *, token: str, base_url: str = PROD_BASE) -> dict[str, Any]:
    if not path.startswith("/admin/"):
        raise ValueError("production clone accepts only /admin GET paths")
    if base_url.rstrip("/") != PROD_BASE:
        raise ValueError("production clone only sends credentials to the Nova production origin")
    req = urllib.request.Request(
        f"{base_url.rstrip('/')}{path}",
        headers={"X-Admin-Token": token, "Accept": "application/json"},
        method="GET",
    )

    class _NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
            return None

    try:
        opener = urllib.request.build_opener(_NoRedirect)
        with opener.open(req, timeout=60) as response:
            payload = json.loads(response.read())
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"production read failed ({exc.code}) for {path}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"production read failed for {path}: {exc.reason}") from exc
    if not isinstance(payload, dict):
        raise RuntimeError(f"production returned a non-object payload for {path}")
    return payload


def _require_shape(
    item_debug: dict[str, Any], job_debug: dict[str, Any]
) -> tuple[dict, dict, dict]:
    item = item_debug.get("item")
    job = job_debug.get("job")
    assembly = job.get("assembly_plan") if isinstance(job, dict) else None
    if not isinstance(item, dict) or not isinstance(job, dict) or not isinstance(assembly, dict):
        raise RuntimeError("admin response is missing item/job/assembly_plan")
    if str(item.get("id")) != ITEM_ID or str(job.get("id")) != JOB_ID:
        raise RuntimeError("admin response IDs do not match the requested production item/job")
    variants = assembly.get("variants")
    if not isinstance(variants, list):
        raise RuntimeError("production job has no variants array")
    guided = next(
        (v for v in variants if isinstance(v, dict) and v.get("variant_id") == "guided_story"),
        None,
    )
    revision = guided.get("guided_edit_revision") if isinstance(guided, dict) else None
    if not isinstance(revision, dict) or not isinstance(revision.get("sources"), list):
        raise RuntimeError("production job has no guided-story revision/source pool")
    return item, job, assembly


def build_path_map(
    sources: list[dict[str, Any]],
    *,
    job_id: str = JOB_ID,
) -> dict[str, str]:
    """Build deterministic source-path → local-path mapping.

    The original path is never retained in the clone's runtime JSON.  Basenames
    are not trusted as identifiers because production uploads can repeat them.
    """
    result: dict[str, str] = {}
    prefix = f"{LOCAL_MEDIA_PREFIX}/{job_id}/sources"
    for source in sources:
        path = source.get("gcs_path")
        media_id = source.get("media_id")
        kind = source.get("kind")
        if not isinstance(path, str) or not path or not isinstance(media_id, str) or not media_id:
            raise RuntimeError("every guided source must have gcs_path and media_id")
        try:
            parsed_media_id = uuid.UUID(media_id)
        except ValueError as exc:
            raise RuntimeError(f"guided source has unsafe media_id: {media_id!r}") from exc
        if str(parsed_media_id) != media_id.lower():
            raise RuntimeError(f"guided source has non-canonical media_id: {media_id!r}")
        if kind not in {"image", "video"}:
            raise RuntimeError(f"unsupported source kind: {kind!r}")
        suffix = Path(path.split("?", 1)[0]).suffix.lower()
        if not suffix or not re.fullmatch(r"\.[a-z0-9]{1,8}", suffix):
            suffix = ".bin"
        result[path] = f"{prefix}/{media_id}{suffix}"
    if len(result) != len(sources):
        raise RuntimeError("duplicate or missing source paths in guided revision")
    return result


def rewrite_paths(value: Any, path_map: dict[str, str]) -> Any:
    """Deep-copy JSON and rewrite only exact string media paths."""
    if isinstance(value, str):
        return path_map.get(value, value)
    if isinstance(value, list):
        return [rewrite_paths(v, path_map) for v in value]
    if isinstance(value, dict):
        return {k: rewrite_paths(v, path_map) for k, v in value.items()}
    return value


def strip_signed_storage_urls(value: Any) -> Any:
    """Remove production bearer URLs while retaining durable object paths."""
    if isinstance(value, list):
        return [strip_signed_storage_urls(child) for child in value]
    if isinstance(value, dict):
        return {key: strip_signed_storage_urls(child) for key, child in value.items()}
    if not isinstance(value, str) or not value.startswith(("http://", "https://")):
        return value
    parsed = urllib.parse.urlparse(value)
    query_keys = {key.lower() for key, _ in urllib.parse.parse_qsl(parsed.query)}
    if query_keys & {"x-goog-signature", "signature", "googleaccessid"}:
        return None
    return value


def preview_path(local_source_path: str) -> str:
    root, _ = os.path.splitext(local_source_path)
    return f"{root}.preview.jpg"


def _source_index(assembly: dict[str, Any]) -> dict[str, dict[str, Any]]:
    variants = assembly.get("variants") or []
    guided = next(
        v for v in variants if isinstance(v, dict) and v.get("variant_id") == "guided_story"
    )
    revision = guided.get("guided_edit_revision") or {}
    return {str(s["media_id"]): s for s in revision.get("sources", []) if isinstance(s, dict)}


def _all_asset_paths(assembly: dict[str, Any]) -> set[str]:
    """Collect current output/base assets without treating signed URLs as paths."""
    paths: set[str] = set()
    for value in _walk_values(assembly):
        if not isinstance(value, str):
            continue
        if value.startswith(("http://", "https://")):
            continue
        if value.startswith("gs://") or "/" in value:
            if value.endswith((".mp4", ".mov", ".m4v", ".webm", ".jpg", ".jpeg", ".png")):
                paths.add(value.removeprefix("gs://").split("?", 1)[0])
    return paths


def _walk_values(value: Any):
    if isinstance(value, dict):
        for child in value.values():
            yield from _walk_values(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk_values(child)
    else:
        yield value


def clone_fingerprint(item_debug: dict[str, Any], job_debug: dict[str, Any]) -> str:
    raw = json.dumps(
        {"item": item_debug, "job": job_debug}, sort_keys=True, separators=(",", ":")
    ).encode()
    return hashlib.sha256(raw).hexdigest()


def _mirror_object(
    source_client: Any, bucket_name: str, object_path: str, destination: Path
) -> str:
    destination.parent.mkdir(parents=True, exist_ok=True)
    blob = source_client.bucket(bucket_name).blob(object_path)
    # reload() is a read-only metadata check and protects against missing paths.
    blob.reload()
    blob.download_to_filename(destination)
    # The E2E filesystem adapter uses stat().st_mtime_ns as its immutable
    # object generation. Keep the clone revision and DB rows on that local
    # identity rather than retaining the production GCS generation.
    return str(destination.stat().st_mtime_ns)


def mirror_media(
    *,
    sources: list[dict[str, Any]],
    assembly: dict[str, Any],
    path_map: dict[str, str],
    media_dir: Path,
    source_bucket: str,
    source_project: str = "",
    source_credentials: str = "",
    job_id: str = JOB_ID,
) -> dict[str, str]:
    """Read production objects and write only the local mirror directory."""
    from google.cloud import storage as gcs
    from google.oauth2 import service_account

    if not source_bucket:
        raise RuntimeError(
            "source bucket must be explicit; this tool reads production GCS and writes only "
            "the local filesystem"
        )
    credentials = (
        service_account.Credentials.from_service_account_file(source_credentials)
        if source_credentials
        else None
    )
    client = gcs.Client(project=source_project or None, credentials=credentials)
    local_generations: dict[str, str] = {}
    objects = {str(s["gcs_path"]) for s in sources}
    objects.update(_all_asset_paths(assembly))
    for source_path in sorted(objects):
        local_rel = path_map.get(source_path)
        if local_rel is None:
            safe_name = hashlib.sha256(source_path.encode()).hexdigest()[:24]
            suffix = Path(source_path).suffix
            local_rel = f"{LOCAL_MEDIA_PREFIX}/{job_id}/assets/{safe_name}{suffix}"
            path_map[source_path] = local_rel
        destination = media_dir / local_rel
        local_generations[source_path] = _mirror_object(
            client, source_bucket, source_path, destination
        )

        source = next((s for s in sources if s.get("gcs_path") == source_path), None)
        if source and source.get("kind") == "image":
            _make_image_preview(
                destination,
                media_dir / preview_path(local_rel),
            )
    return local_generations


def rewrite_generations(value: Any, generations: dict[str, str]) -> Any:
    """Rewrite generation beside a copied gcs_path to its local stat identity."""
    if isinstance(value, list):
        return [rewrite_generations(v, generations) for v in value]
    if not isinstance(value, dict):
        return value
    rewritten = {key: rewrite_generations(child, generations) for key, child in value.items()}
    path = rewritten.get("gcs_path")
    if isinstance(path, str) and path in generations and "generation" in rewritten:
        rewritten["generation"] = generations[path]
    return rewritten


def _replace_exact_scalar(value: Any, old: str, new: str) -> Any:
    """Deep-copy JSON while replacing one exact integrity identity."""
    if isinstance(value, list):
        return [_replace_exact_scalar(child, old, new) for child in value]
    if isinstance(value, dict):
        return {key: _replace_exact_scalar(child, old, new) for key, child in value.items()}
    return new if value == old else value


def recompute_clone_integrity(assembly: dict[str, Any]) -> dict[str, Any]:
    """Re-sign the guided snapshot/revision after local identity rewriting.

    GCS paths and generations are immutable inputs to both the approved-media
    digest and the editor revision hash.  A local mirror deliberately changes
    those identities, so retaining production hashes makes the real editor
    fail closed with an empty timeline.  Recompute every coupled provenance
    field while preserving the production editorial state byte-for-byte.
    """
    from app.schemas.edit_proposal import (  # noqa: PLC0415
        EditProposalSnapshot,
        canonical_media_digest,
    )
    from app.schemas.guided_edit_revision import (  # noqa: PLC0415
        normalize_guided_editor_revision,
    )

    result = copy.deepcopy(assembly)
    guided = result.get("guided_edit")
    if not isinstance(guided, dict):
        raise RuntimeError("rewritten assembly is missing guided_edit")
    try:
        snapshot = EditProposalSnapshot.model_validate(guided["approved_proposal"])
        old_digest = str(guided["media_digest"])
    except Exception as exc:  # noqa: BLE001 - fail closed on clone corruption
        raise RuntimeError("rewritten guided snapshot is invalid") from exc
    new_digest = canonical_media_digest(snapshot.media)
    result = _replace_exact_scalar(result, old_digest, new_digest)

    variants = result.get("variants")
    guided_variant = next(
        (
            variant
            for variant in variants or []
            if isinstance(variant, dict) and variant.get("variant_id") == "guided_story"
        ),
        None,
    )
    revision = (
        guided_variant.get("guided_edit_revision") if isinstance(guided_variant, dict) else None
    )
    if not isinstance(revision, dict):
        raise RuntimeError("rewritten assembly is missing guided editor revision")
    old_state_hash = str(revision.get("state_hash") or "")
    raw_revision = {**revision, "state_hash": ""}
    try:
        normalized = normalize_guided_editor_revision(
            raw_revision,
            expected_approval_version=int(guided["proposal_version"]),
            expected_media_digest=new_digest,
        )
    except (TypeError, ValueError) as exc:
        raise RuntimeError("rewritten guided editor revision is invalid") from exc
    guided_variant["guided_edit_revision"] = normalized
    if old_state_hash:
        result = _replace_exact_scalar(result, old_state_hash, str(normalized["state_hash"]))
    return result


def _make_image_preview(source: Path, destination: Path) -> None:
    """Create the browser-safe JPEG derivative locally, never in production."""
    try:
        from PIL import Image

        try:
            import pillow_heif

            pillow_heif.register_heif_opener()
        except ImportError:
            pass
        with Image.open(source) as image:
            image.convert("RGB").save(destination, format="JPEG", quality=88, optimize=True)
    except Exception as exc:  # noqa: BLE001 - fail closed for real media QA
        raise RuntimeError(
            f"could not create local image preview for {source.name}: {exc}"
        ) from exc


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--allow-prod-read", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--remove", action="store_true", help="remove only this clone's local DB/files"
    )
    parser.add_argument(
        "--media-dir",
        type=Path,
        required=True,
        help="local storage root (the same path used by LOCAL_STORAGE_ROOT)",
    )
    parser.add_argument("--source-bucket", help="production bucket; required unless --remove")
    parser.add_argument("--source-project", default="")
    parser.add_argument("--source-credentials", default="")
    return parser.parse_args()


def _import_local(
    *,
    item_debug: dict[str, Any],
    item_data: dict[str, Any],
    job_data: dict[str, Any],
    assembly: dict[str, Any],
    path_map: dict[str, str],
    generations: dict[str, str],
    fingerprint: str,
    dry_run: bool,
) -> None:
    from sqlalchemy import select

    from app.database import sync_session
    from app.models import ContentPlan, Job, Persona, PlanItem, PlanItemAsset, User

    with sync_session() as db:
        user = db.execute(select(User).where(User.email == QA_EMAIL)).scalar_one_or_none()
        if user is None:
            raise RuntimeError(f"local QA user {QA_EMAIL!r} does not exist; sign in first")
        persona = db.execute(select(Persona).where(Persona.user_id == user.id)).scalar_one_or_none()
        if persona is None:
            raise RuntimeError("local QA user has no Persona; sign in/onboard first")

        plan_id = uuid.uuid5(uuid.NAMESPACE_URL, f"{CLONE_MARKER}:{ITEM_ID}")
        marker = {"clone": CLONE_MARKER, "fingerprint": fingerprint}
        plan = db.get(ContentPlan, plan_id)
        if plan is None:
            plan = ContentPlan(
                id=plan_id,
                user_id=user.id,
                persona_id=persona.id,
                plan_status="ready",
                horizon_days=1,
                events=marker,
            )
            db.add(plan)
        elif not isinstance(plan.events, dict) or plan.events.get("clone") != CLONE_MARKER:
            raise RuntimeError("deterministic clone ContentPlan ID is occupied by non-clone data")
        else:
            plan.events = marker

        existing_item = db.get(PlanItem, uuid.UUID(ITEM_ID))
        if existing_item is not None and existing_item.content_plan_id != plan.id:
            raise RuntimeError("production item UUID is occupied by unrelated local data")
        if existing_item is not None and CLONE_MARKER not in str(existing_item.notes or ""):
            raise RuntimeError("production item UUID is occupied by unrelated local data")
        existing_job = db.get(Job, uuid.UUID(JOB_ID))
        if existing_job is not None and existing_job.content_plan_item_id not in (
            None,
            uuid.UUID(ITEM_ID),
        ):
            raise RuntimeError("production job UUID is occupied by unrelated local data")
        if existing_job is not None:
            existing_marker = (
                existing_job.assembly_plan.get("_local_clone", {}).get("clone")
                if isinstance(existing_job.assembly_plan, dict)
                else None
            )
            if existing_marker != CLONE_MARKER:
                raise RuntimeError("production job UUID is occupied by unrelated local data")
        if dry_run:
            print(f"[dry-run] would seed item={ITEM_ID} job={JOB_ID} plan={plan_id}")
            db.rollback()
            return
        sources = _source_index(assembly)
        missing_generations = [
            source.get("gcs_path")
            for source in sources.values()
            if source.get("gcs_path") not in generations
        ]
        if missing_generations:
            raise RuntimeError(
                "media mirror did not produce local generations for "
                f"{len(missing_generations)} sources"
            )

        rewritten_item = rewrite_paths(item_data, path_map)
        rewritten_assembly = recompute_clone_integrity(
            strip_signed_storage_urls(
                rewrite_paths(rewrite_generations(assembly, generations), path_map)
            )
        )
        if existing_item is None:
            item = PlanItem(
                id=uuid.UUID(ITEM_ID),
                content_plan_id=plan.id,
                position=1,
                idea=str(rewritten_item.get("idea") or "Production mixed-media clone"),
            )
            db.add(item)
        else:
            item = existing_item
        # Admin debug intentionally redacts text/media fields; merge only fields
        # that are available in the source payload and leave safe defaults intact.
        item.content_plan_id = plan.id
        item.position = int(rewritten_item.get("position") or 1)
        item.idea = str(rewritten_item.get("idea") or item.idea or "Production mixed-media clone")
        item.item_status = "idea"
        item.edit_format = str(rewritten_item.get("edit_format") or "montage")
        item.content_mode = rewritten_item.get("content_mode") or "existing_footage"
        item.montage_preset = str(rewritten_item.get("montage_preset") or "classic")
        item.clip_gcs_paths = rewrite_paths(
            item_debug.get("clip_gcs_paths", {}).get("paths", []), path_map
        )
        # clip_assignments are not in the admin item summary; reconstruct them
        # from the source pool below, preserving video identities/durations.
        sources = _source_index(assembly)
        item.clip_assignments = [
            {
                "gcs_path": path_map[s["gcs_path"]],
                "media_id": s["media_id"],
                "duration_s": s.get("duration_s"),
                "generation": generations.get(s["gcs_path"]),
                "shot_id": None,
            }
            for s in sources.values()
            if s.get("kind") == "video"
        ]
        item.notes = json.dumps(marker, sort_keys=True)
        item.current_job_id = None
        db.flush()

        if existing_job is None:
            job = Job(
                id=uuid.UUID(JOB_ID),
                user_id=user.id,
                status="variants_ready",
                job_type="generative",
                mode="content_plan",
                raw_storage_path=path_map.get(str(job_data.get("raw_storage_path") or ""), ""),
            )
            db.add(job)
        else:
            job = existing_job
        job.user_id = user.id
        job.status = "variants_ready"
        job.job_type = "generative"
        job.mode = "content_plan"
        job.raw_storage_path = path_map.get(str(job_data.get("raw_storage_path") or ""), "")
        job.assembly_plan = {
            **copy.deepcopy(rewritten_assembly),
            "_local_clone": marker,
        }
        for field in (
            "selected_platforms",
            "probe_metadata",
            "transcript",
            "scene_cuts",
            "all_candidates",
            "phase_log",
            "pipeline_trace",
        ):
            if field in job_data:
                setattr(job, field, rewrite_paths(job_data[field], path_map))
        job.content_plan_ownership_epoch = 0
        job.content_plan_item_id = None
        db.flush()

        # Replace only rows owned by this deterministic clone.  The source media
        # IDs are stable and the plan-item FK scopes the delete safely.
        db.query(PlanItemAsset).filter(PlanItemAsset.plan_item_id == item.id).delete(
            synchronize_session=False
        )
        pool_meta = {
            str(a.get("id")): a
            for a in (item_debug.get("pool_assets") or [])
            if isinstance(a, dict)
        }
        for media_id, source in sources.items():
            if source.get("kind") != "image":
                continue
            meta = pool_meta.get(media_id, {})
            local_path = path_map[source["gcs_path"]]
            asset = PlanItemAsset(
                id=uuid.UUID(media_id),
                plan_item_id=item.id,
                user_id=user.id,
                gcs_path=local_path,
                kind="image",
                source_filename=meta.get("source_filename"),
                duration_s=source.get("duration_s"),
                aspect=meta.get("aspect"),
                gcs_generation=generations.get(source["gcs_path"]),
                status="ready",
                media_status="ready",
                preview_gcs_path=preview_path(local_path),
                analysis={},
            )
            db.add(asset)
        db.flush()
        job.content_plan_item_id = item.id
        item.current_job_id = job.id
        db.commit()
        print(f"seeded local clone item={ITEM_ID} job={JOB_ID} sources={len(sources)}")


def _remove_local(media_dir: Path) -> None:
    from app.database import sync_session
    from app.models import ContentPlan, Job, PlanItem, PlanItemAsset

    plan_id = uuid.uuid5(uuid.NAMESPACE_URL, f"{CLONE_MARKER}:{ITEM_ID}")
    with sync_session() as db:
        plan = db.get(ContentPlan, plan_id)
        item = db.get(PlanItem, uuid.UUID(ITEM_ID))
        if (
            plan is None
            or not isinstance(plan.events, dict)
            or plan.events.get("clone") != CLONE_MARKER
        ):
            raise RuntimeError("refusing removal: deterministic plan is absent or not clone-owned")
        if item is not None and item.content_plan_id != plan.id:
            raise RuntimeError("refusing removal: item ownership does not match clone plan")
        if item is not None:
            db.query(PlanItemAsset).filter(PlanItemAsset.plan_item_id == item.id).delete(
                synchronize_session=False
            )
            job = db.get(Job, uuid.UUID(JOB_ID))
            if job is not None and job.content_plan_item_id == item.id:
                db.delete(job)
            db.delete(item)
        db.delete(plan)
        db.commit()
    resolved_media_dir = media_dir.resolve()
    if resolved_media_dir == Path(resolved_media_dir.anchor):
        raise RuntimeError("refusing to remove a clone from the filesystem root")
    clone_root = resolved_media_dir / LOCAL_MEDIA_PREFIX / JOB_ID
    if clone_root.exists():
        shutil.rmtree(clone_root)
    print(f"removed local clone item={ITEM_ID} job={JOB_ID}")


def main() -> int:
    args = _parse_args()
    try:
        env = {**_load_env(), **os.environ}
        if args.remove:
            _remove_local(args.media_dir)
            return 0
        _require_prod_read(args)
        token = env.get("ADMIN_PROD_API_KEY", "").strip()
        if not token:
            raise RuntimeError("ADMIN_PROD_API_KEY is missing")
        item_debug = _prod_get(
            f"/admin/plan-items/{ITEM_ID}/debug",
            token=token,
        )
        job_debug = _prod_get(f"/admin/jobs/{JOB_ID}/debug", token=token)
        item_data, job_data, assembly = _require_shape(item_debug, job_debug)
        sources = list(_source_index(assembly).values())
        path_map = build_path_map(sources)
        fingerprint = clone_fingerprint(item_debug, job_debug)
        if args.dry_run:
            print(
                json.dumps(
                    {
                        "item": ITEM_ID,
                        "job": JOB_ID,
                        "sources": len(sources),
                        "fingerprint": fingerprint,
                    },
                    indent=2,
                )
            )
            _import_local(
                item_debug=item_debug,
                item_data=item_data,
                job_data=job_data,
                assembly=assembly,
                path_map=path_map,
                generations={},
                fingerprint=fingerprint,
                dry_run=True,
            )
            return 0
        if not args.source_bucket:
            raise RuntimeError("--source-bucket is required for a media mirror")
        args.media_dir.mkdir(parents=True, exist_ok=True)
        generations = mirror_media(
            sources=sources,
            assembly=assembly,
            path_map=path_map,
            media_dir=args.media_dir,
            source_bucket=args.source_bucket,
            source_project=(
                args.source_project
                or env.get("KRIA_CLONE_SOURCE_PROJECT", "")
                or env.get("GOOGLE_CLOUD_PROJECT", "")
                or env.get("GCLOUD_PROJECT", "")
            ),
            source_credentials=(
                args.source_credentials
                or env.get("KRIA_CLONE_SOURCE_CREDENTIALS", "")
                or env.get("GOOGLE_APPLICATION_CREDENTIALS", "")
            ),
        )
        _import_local(
            item_debug=item_debug,
            item_data=item_data,
            job_data=job_data,
            assembly=assembly,
            path_map=path_map,
            generations=generations,
            fingerprint=fingerprint,
            dry_run=False,
        )
        return 0
    except Exception as exc:  # noqa: BLE001 - CLI emits one actionable failure
        print(f"clone failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
