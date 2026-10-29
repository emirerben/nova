"""Real worker binding -> saved recipe -> HTTP source grant (KRI-835).

Failure modes: an owned original outside pool/ is refused; a pool-shaped library
link bypasses source revocation; another owner/item or changed original receives
a grant. Only GCS transport and the authenticated caller are substituted.
"""

from __future__ import annotations

import base64
import hashlib
import subprocess
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from app import storage
from app.auth import get_current_user
from app.database import sync_session
from app.kria.device_render import make_device_request
from app.kria.recipes_v2 import EditRecipeV2
from app.main import app
from app.models import Job, MediaAsset, MediaAssetSource, PlanItemAsset
from app.services.device_render import pin_device_request
from app.services.phone_visuals import bind_phone_visual_assets
from tests.kria.test_render_retry_postgres import client, seed_failed  # noqa: F401


@pytest.fixture(scope="module")
def media_bytes(tmp_path_factory):
    image = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aX1sAAAAASUVORK5CYII="
    )
    video = tmp_path_factory.mktemp("library-original") / "source.mp4"
    subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "color=black:s=64x64:r=30",
            "-t",
            "2",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-an",
            "-y",
            str(video),
        ],
        check=True,
        capture_output=True,
    )
    return {"image": image, "video": video.read_bytes()}


def bind_and_pin(monkeypatch, payload, *, kind="image", namespace="library", linked=True):
    case = seed_failed()
    with sync_session() as db:
        case.item_id = db.get(Job, case.job_id).content_plan_item_id
    visual_id, asset_id, source_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    extension = "png" if kind == "image" else "mp4"
    if namespace == "same_pool":
        path = f"users/{case.user_id}/plan/{case.item_id}/pool/{visual_id}.{extension}"
    elif namespace == "other_pool":
        path = f"users/{case.user_id}/plan/{uuid.uuid4()}/pool/{visual_id}.{extension}"
    else:
        path = f"users/{case.user_id}/library/originals/{source_id}.{extension}"
    digest = hashlib.sha256(payload).hexdigest()
    with sync_session() as db:
        if linked:
            db.add(MediaAsset(id=asset_id, user_id=case.user_id, kind=kind, status="ready"))
            db.flush()
            db.add(
                MediaAssetSource(
                    id=source_id,
                    user_id=case.user_id,
                    asset_id=asset_id,
                    role="original",
                    object_path=path,
                    object_generation="42",
                    uploaded_sha256=digest,
                    byte_count=len(payload),
                    content_type="image/png" if kind == "image" else "video/mp4",
                    status="available",
                )
            )
            db.flush()
        db.add(
            PlanItemAsset(
                id=visual_id,
                user_id=case.user_id,
                plan_item_id=case.item_id,
                kind=kind,
                gcs_path=path,
                gcs_generation="42",
                status="ready",
                library_asset_id=asset_id if linked else None,
                library_source_id=source_id if linked else None,
            )
        )
        db.commit()

    def download(requested_path, local, *, generation):
        assert (requested_path, generation) == (path, "42")
        Path(local).write_bytes(payload)

    monkeypatch.setattr(storage, "download_generation_to_file", download)
    (binding,) = bind_phone_visual_assets(
        sync_session, job_id=str(case.job_id), pins={str(visual_id): (kind, path, "42")}
    )
    assert binding.sha256 == digest and binding.byte_count == len(payload)
    manifest_asset = binding.render_asset()
    recipe = EditRecipeV2.model_validate(
        {
            "schema_version": 2,
            "renderer_version": "kria-ios-2",
            "assets": [
                {
                    "id": manifest_asset.id,
                    "relative_path": manifest_asset.id,
                    "fingerprint": {
                        "algorithm": "sha256",
                        "hex": digest,
                        "byte_count": len(payload),
                    },
                }
            ],
            "asset_manifest": {"assets": [manifest_asset.model_dump(mode="json")]},
            "tracks": [
                {
                    "id": "v",
                    "kind": "video",
                    "clips": [
                        {
                            "id": "c",
                            "source_asset_id": manifest_asset.id,
                            "source_start": 0,
                            "source_duration": 2,
                            "timeline_start": 0,
                            "rate": 1,
                        }
                    ],
                }
            ],
        }
    )
    request = make_device_request(job_id=case.job_id, variant_id="first", revision=2, recipe=recipe)
    with sync_session() as db:
        pin_device_request(db.get(Job, case.job_id), request, base_generation="approved")
        db.commit()
    app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(id=case.user_id)
    signer = Mock(return_value="https://storage.example/exact-original")
    monkeypatch.setattr(storage, "signed_get_url_for_generation", signer)
    return SimpleNamespace(
        **vars(case),
        visual_id=visual_id,
        source_id=source_id,
        path=path,
        request=request,
        manifest_asset=manifest_asset,
        signer=signer,
    )


def grant(http, case):
    return http.post(
        f"/me/jobs/{case.job_id}/device-render/assets",
        json={
            "identity": case.request.identity.model_dump(mode="json"),
            "asset_id": case.manifest_asset.id,
        },
    )


@pytest.mark.parametrize(
    "kind,namespace,linked",
    [
        ("image", "library", True),
        ("image", "same_pool", True),
        ("image", "other_pool", True),
        ("video", "library", True),
        ("video", "same_pool", True),
        ("image", "same_pool", False),
    ],
)
def test_bound_original_reaches_the_http_device_grant(
    client,  # noqa: F811
    monkeypatch,
    media_bytes,
    kind,
    namespace,
    linked,
):
    http, _ = client
    case = bind_and_pin(
        monkeypatch,
        media_bytes[kind],
        kind=kind,
        namespace=namespace,
        linked=linked,
    )
    response = grant(http, case)
    assert response.status_code == 200, response.text
    assert response.json()["asset_id"] == case.manifest_asset.id
    case.signer.assert_called_once_with(case.path, generation="42")


@pytest.mark.parametrize("namespace", ["library", "same_pool"])
@pytest.mark.parametrize(
    "change",
    [
        "proxy",
        "missing",
        "deleting",
        "deleted",
        "source_path",
        "source_generation",
        "hash",
        "missing_hash",
        "bytes",
        "row_generation",
        "row_kind",
        "row_failed",
    ],
)
def test_changed_original_is_rejected_even_if_stored_under_pool(
    client,  # noqa: F811
    monkeypatch,
    media_bytes,
    namespace,
    change,
):
    http, _ = client
    case = bind_and_pin(monkeypatch, media_bytes["image"], namespace=namespace)
    with sync_session() as db:
        source = db.get(MediaAssetSource, case.source_id)
        row = db.get(PlanItemAsset, case.visual_id)
        if change in {"missing", "deleting", "deleted"}:
            source.status = change
        elif change in {"row_generation", "row_kind", "row_failed"}:
            if change == "row_generation":
                row.gcs_generation = "43"
            elif change == "row_kind":
                row.kind = "video"
            else:
                row.status = "failed"
        else:
            # Original versions are immutable in PostgreSQL. A replacement is
            # a new source, and the project visual may now reference that source.
            new_path = source.object_path + ".replacement"
            replacement = MediaAssetSource(
                id=uuid.uuid4(),
                user_id=source.user_id,
                asset_id=source.asset_id,
                role="analysis_proxy" if change == "proxy" else "original",
                object_path=source.object_path if change == "source_generation" else new_path,
                object_generation="43" if change == "source_generation" else "42",
                uploaded_sha256=None
                if change == "missing_hash"
                else "b" * 64
                if change == "hash"
                else source.uploaded_sha256,
                byte_count=source.byte_count + (1 if change == "bytes" else 0),
                content_type=source.content_type,
                status="available",
            )
            db.add(replacement)
            db.flush()
            row.library_source_id = replacement.id
            if change not in {"source_path", "source_generation"}:
                row.gcs_path = replacement.object_path
        db.commit()
    response = grant(http, case)
    assert response.status_code == 409, response.text
    case.signer.assert_not_called()


def test_detached_non_pool_original_and_foreign_caller_receive_no_grant(
    client,  # noqa: F811
    monkeypatch,
    media_bytes,
):
    http, _ = client
    case = bind_and_pin(monkeypatch, media_bytes["image"])
    app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(id=uuid.uuid4())
    assert grant(http, case).status_code == 404
    app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(id=case.user_id)
    with sync_session() as db:
        row = db.get(PlanItemAsset, case.visual_id)
        row.library_source_id = row.library_asset_id = None
        db.commit()
    assert grant(http, case).status_code == 409
    case.signer.assert_not_called()
