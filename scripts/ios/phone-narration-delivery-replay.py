#!/usr/bin/env python3
"""Replay one persisted phone-narration delivery without re-planning it.

The private input is deliberately outside git.  It contains the persisted
``_device_render_v1`` request, phone-source bindings, a plan item snapshot,
and a map of storage keys to local bytes.  ``prepare`` writes a self-contained
fixture for ``DeviceNarrationDeliveryReplayTests``.  It only changes source
fingerprints when its local proxy bytes differ; this is a truthful local
rebinding, never a claim that an analysis proxy is the creator's original.

After the simulator writes ``export.mp4``, ``complete`` exercises the real
reservation and completion routes with only auth/DB/storage boundaries faked.

    src/apps/api/.venv/bin/python scripts/ios/phone-narration-delivery-replay.py \
      prepare /private/tmp/nova-kri277-replay/input.json /private/tmp/nova-kri277-replay/out
    # After an iOS build, rerun prepare so the 15-minute captured grant is fresh.
    TEST_RUNNER_KRIA_NARRATION_REPLAY_DIR=/private/tmp/nova-kri277-replay/out \
      xcodebuild -project src/apps/ios/Kria.xcodeproj -scheme Kria ... \
      -only-testing:KriaTests/DeviceNarrationDeliveryReplayTests test
    src/apps/api/.venv/bin/python scripts/ios/phone-narration-delivery-replay.py \
      complete /private/tmp/nova-kri277-replay/input.json /private/tmp/nova-kri277-replay/out
"""

# ruff: noqa: E402
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import shutil
import sys
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "src/apps/api"))
for key, value in {
    "STORAGE_BUCKET": "nova-test",
    "DATABASE_URL": "postgresql://localhost/test",
    "REDIS_URL": "redis://localhost:6379/0",
    "INTERNAL_API_KEY": "test",
}.items():
    os.environ.setdefault(key, value)

from fastapi.testclient import TestClient

from app.auth import get_current_user
from app.config import settings
from app.database import get_db
from app.kria.device_render import DeviceRenderStatus, make_device_request
from app.kria.recipes_v2 import EditRecipeV2
from app.main import app
from app.routes import device_render as routes


def sha(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest(), path.stat().st_size


def media_path(payload: dict, key: str) -> Path:
    value = payload["media"].get(key)
    if not value:
        raise ValueError(f"input media is missing {key!r}")
    path = Path(value if isinstance(value, str) else value["path"])
    if not path.is_file():
        raise FileNotFoundError(path)
    return path


def replay_state(payload: dict) -> tuple[str, dict, dict]:
    assembly = copy.deepcopy(payload["job"]["assembly_plan"])
    records = assembly.get("_device_render_v1") or {}
    choices = [
        (variant, record)
        for variant, record in records.items()
        if record.get("status", {})
        .get("request", {})
        .get("recipe", {})
        .get("audio", {})
        .get("narration_asset_id")
    ]
    if len(choices) != 1:
        raise ValueError(
            "input must contain exactly one persisted narration device request"
        )
    variant, record = choices[0]
    return variant, assembly, copy.deepcopy(record["status"])


def capture_voiceover_grant(
    payload: dict, assembly: dict, status: dict, asset: dict, source: Path
) -> dict:
    """Run the real grant route; storage/auth/DB are the only local seams."""
    user = SimpleNamespace(id=uuid.UUID(payload["job"]["user_id"]))
    job = SimpleNamespace(
        id=uuid.UUID(payload["job"]["id"]),
        user_id=user.id,
        content_plan_item_id=uuid.UUID(payload["item"]["id"]),
        assembly_plan=assembly,
    )
    item = SimpleNamespace(**payload["item"])
    metadata = payload["media_metadata"]
    source_key = next(
        key
        for key in metadata
        if str(metadata[key].get("generation")) == str(asset["generation"])
        and sha(media_path(payload, key))
        == (asset["fingerprint"]["sha256"], asset["fingerprint"]["byte_count"])
    )
    db = AsyncMock()
    db.get.return_value = item
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_db] = lambda: db
    prior_enabled, prior_limiter = (
        settings.phone_rendering_enabled,
        routes.limiter.enabled,
    )
    settings.phone_rendering_enabled = True
    routes.limiter.enabled = False

    async def owned(_db, user_id, job_id):
        return job if user_id == user.id and job_id == job.id else None

    def object_metadata(path):
        if path != source_key:
            raise FileNotFoundError(path)
        value = metadata[path]
        return SimpleNamespace(
            size=value["size"],
            generation=str(value["generation"]),
            content_type="audio/mp4",
        )

    def download(path, target, generation=None):
        if path != source_key or str(generation) != str(asset["generation"]):
            raise FileNotFoundError(path)
        shutil.copy2(source, target)

    try:
        with (
            patch.object(routes, "_owned_job", owned),
            patch.object(routes.storage, "object_metadata", object_metadata),
            patch.object(routes.storage, "download_generation_to_file", download),
            patch.object(
                routes.storage,
                "signed_get_url_for_generation",
                lambda path, generation: f"https://storage.replay.test/{asset['id']}",
            ),
        ):
            with TestClient(app, raise_server_exceptions=False) as client:
                response = client.post(
                    f"/me/jobs/{job.id}/device-render/assets",
                    json={
                        "identity": status["request"]["identity"],
                        "asset_id": asset["id"],
                    },
                )
                if response.status_code != 200:
                    raise ValueError(
                        f"voiceover grant rejected: {response.status_code} {response.text}"
                    )
                return response.json()
    finally:
        settings.phone_rendering_enabled = prior_enabled
        routes.limiter.enabled = prior_limiter
        app.dependency_overrides.clear()


def prepare(input_file: Path, out: Path) -> None:
    payload = json.loads(input_file.read_text())
    variant, assembly, status_doc = replay_state(payload)
    recipe_doc = status_doc["request"]["recipe"]
    if recipe_doc.get("schema_version") != 2:
        raise ValueError("KRI-277 requires a V2 portable recipe")
    bindings = {row["media_id"]: row for row in assembly.get("_phone_sources_v1") or []}
    manifest = recipe_doc["asset_manifest"]["assets"]
    assets = {asset["id"]: asset for asset in recipe_doc["assets"]}
    copied: dict[str, str] = {}
    out.mkdir(parents=True, exist_ok=True)
    assets_dir = out / "assets"
    assets_dir.mkdir(exist_ok=True)

    # The asset manifest is source identity.  Only original entries are allowed
    # to be rebound to local analysis proxies; voiceover/library identities stay
    # exact and are verified below before the Swift resolver receives a grant.
    for entry in manifest:
        if entry["kind"] != "original":
            continue
        binding = bindings.get(entry["media_id"])
        if binding is None:
            raise ValueError(
                f"recipe original {entry['media_id']!r} has no phone binding"
            )
        source = media_path(payload, binding["proxy_path"])
        digest, size = sha(source)
        entry["fingerprint"] = {"sha256": digest, "byte_count": size}
        asset = assets[entry["id"]]
        asset["fingerprint"] = {
            "algorithm": "sha256",
            "hex": digest,
            "byte_count": size,
        }
        binding["original"]["sha256"] = digest
        binding["original"]["byte_count"] = size
        name = f"original-{entry['id']}"
        shutil.copy2(source, assets_dir / name)
        copied[entry["id"]] = f"assets/{name}"

    grants: dict[str, str] = {}
    grant_responses: dict[str, dict] = {}
    for entry in manifest:
        if entry["kind"] != "voiceover":
            continue
        # A cleaned derivative is selected by the persisted recipe's generation
        # and fingerprint, rather than by current item.voiceover_gcs_path.
        candidates = [
            key
            for key, meta in payload.get("media_metadata", {}).items()
            if str(meta.get("generation")) == str(entry["generation"])
        ]
        matched = next(
            (
                key
                for key in candidates
                if sha(media_path(payload, key))
                == (entry["fingerprint"]["sha256"], entry["fingerprint"]["byte_count"])
            ),
            None,
        )
        if matched is None:
            raise ValueError(
                "pinned narration bytes/generation are absent or checksum-mismatched"
            )
        name = f"grant-{entry['id']}{Path(matched).suffix or '.m4a'}"
        shutil.copy2(media_path(payload, matched), assets_dir / name)
        grants[entry["id"]] = f"assets/{name}"

    recipe = EditRecipeV2.model_validate(recipe_doc)
    request = make_device_request(
        job_id=uuid.UUID(payload["job"]["id"]),
        variant_id=variant,
        revision=status_doc["request"]["identity"]["recipe_revision"],
        recipe=recipe,
    )
    status = DeviceRenderStatus(phase="awaiting_device", request=request).model_dump(
        mode="json"
    )
    assembly["_device_render_v1"][variant]["status"] = status
    for entry in manifest:
        if entry["kind"] == "voiceover":
            grant_responses[entry["id"]] = capture_voiceover_grant(
                payload, assembly, status, entry, out / grants[entry["id"]]
            )
    (out / "status.json").write_text(json.dumps(status, indent=2))
    (out / "assembly.json").write_text(json.dumps(assembly, indent=2))
    (out / "replay.json").write_text(
        json.dumps(
            {
                "variant_id": variant,
                "duration_s": recipe.duration,
                "originals": copied,
                "grants": grants,
                "grant_responses": grant_responses,
                "voiceover_asset_id": recipe.audio.narration_asset_id,
            },
            indent=2,
        )
    )
    print(
        f"prepared {out}; variant={variant}; duration={recipe.duration:.6f}s; "
        f"originals={len(copied)} grants={len(grants)}"
    )


def complete(input_file: Path, out: Path) -> None:
    """Drive the real reserve/complete route against the simulator's MP4."""
    export = out / "export.mp4"
    if not export.is_file():
        raise FileNotFoundError(f"simulator export missing: {export}")
    payload = json.loads(input_file.read_text())
    replay = json.loads((out / "replay.json").read_text())
    assembly = json.loads((out / "assembly.json").read_text())
    status = json.loads((out / "status.json").read_text())
    user = SimpleNamespace(id=uuid.UUID(payload["job"]["user_id"]))
    job = SimpleNamespace(
        id=uuid.UUID(payload["job"]["id"]),
        user_id=user.id,
        status="awaiting_device",
        assembly_plan=assembly,
        content_plan_item_id=payload["job"].get("content_plan_item_id"),
        current_phase=None,
        finished_at=None,
    )
    identity = status["request"]["identity"]
    attempt = str(uuid.uuid4())
    digest, size = sha(export)
    cleanup = SimpleNamespace(
        user_id=user.id,
        status="reserved",
        retention_expires_at=datetime.now(UTC) + timedelta(hours=1),
    )
    db = AsyncMock()
    db.add = MagicMock()
    db.execute.return_value = SimpleNamespace(scalar_one_or_none=lambda: cleanup)
    local_object = out / "uploaded.mp4"
    shutil.copy2(export, local_object)
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_db] = lambda: db
    old_enabled, old_limiter = (
        settings.phone_rendering_enabled,
        routes.limiter.enabled,
    )
    settings.phone_rendering_enabled = True
    routes.limiter.enabled = False

    async def owned(_db, user_id, job_id):
        return job if user_id == user.id and job_id == job.id else None

    def metadata(_):
        return SimpleNamespace(
            size=size, content_type="video/mp4", generation="replay-local-1"
        )

    def download(_, target, generation=None):
        shutil.copy2(local_object, target)

    try:
        with (
            patch.object(routes, "_owned_job", owned),
            patch.object(
                routes.storage,
                "signed_put_url",
                lambda *args: "https://replay.invalid/upload",
            ),
            patch.object(routes.storage, "object_metadata", metadata),
            patch.object(routes.storage, "download_generation_to_file", download),
            patch.object(
                routes.storage,
                "signed_get_url",
                lambda *args: "https://replay.invalid/playback",
            ),
            patch.object(routes, "upload_video_poster", lambda *args, **kwargs: None),
        ):
            with TestClient(app, raise_server_exceptions=False) as client:
                reserve = client.post(
                    f"/me/jobs/{job.id}/device-render/uploads",
                    json={
                        "identity": identity,
                        "attempt_id": attempt,
                        "file_size_bytes": size,
                        "sha256": digest,
                        "brand_tail": "none",
                    },
                )
                if reserve.status_code != 200:
                    raise RuntimeError(
                        f"reservation failed: {reserve.status_code} {reserve.text}"
                    )
                done = client.post(
                    f"/me/jobs/{job.id}/device-render/complete",
                    json={"identity": identity, "attempt_id": attempt},
                )
                if done.status_code != 200:
                    raise RuntimeError(
                        f"completion failed: {done.status_code} {done.text}"
                    )
    finally:
        settings.phone_rendering_enabled = old_enabled
        routes.limiter.enabled = old_limiter
        app.dependency_overrides.clear()
    result = {
        "variant_id": replay["variant_id"],
        "export": str(export),
        "sha256": digest,
        "size": size,
        "completion": "published",
        "job_status": job.status,
        "speech_cleanup_outcome": job.assembly_plan.get("speech_cleanup_outcome"),
    }
    (out / "completion-report.json").write_text(
        json.dumps(result, indent=2, default=str)
    )
    print(json.dumps(result, indent=2, default=str))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("prepare", "complete"))
    parser.add_argument("input", type=Path)
    parser.add_argument("out", type=Path)
    args = parser.parse_args()
    (prepare if args.command == "prepare" else complete)(args.input, args.out)


if __name__ == "__main__":
    main()
