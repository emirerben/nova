"""Real database proof that project removal and account erasure differ."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from cryptography.fernet import Fernet
from sqlalchemy import delete
from sqlalchemy.engine import make_url

from app.config import settings
from app.database import AsyncSessionLocal, sync_session
from app.models import JobStorageDeletion, MediaAsset, MediaAssetSource, User
from app.routes import me
from app.services.job_storage_deletion import build_job_storage_manifest
from app.storage import PrefixDeletionResult
from app.tasks import account_lifecycle

if not (make_url(settings.database_url).database or "").endswith("_test"):
    pytest.skip("requires disposable test database", allow_module_level=True)


@pytest.fixture
def owned_source():
    owner_id, asset_id, source_id, old_job_id = (uuid.uuid4() for _ in range(4))
    path = f"{owner_id}/{old_job_id}/raw.mp4"
    with sync_session() as db:
        db.add(User(id=owner_id, email=f"library-erasure-{owner_id}@test.local"))
        db.flush()
        db.add(MediaAsset(id=asset_id, user_id=owner_id, kind="video"))
        db.flush()
        db.add(
            MediaAssetSource(
                id=source_id,
                user_id=owner_id,
                asset_id=asset_id,
                role="analysis_proxy",
                object_path=path,
                object_generation="42",
                byte_count=123,
                content_type="video/mp4",
            )
        )
        db.commit()
    yield owner_id, source_id, old_job_id, path
    with sync_session() as db:
        db.execute(delete(User).where(User.id == owner_id))
        db.execute(delete(JobStorageDeletion).where(JobStorageDeletion.job_id == old_job_id))
        db.commit()


@pytest.mark.parametrize("shape", ["legacy", "v2", "project"])
def test_project_deletion_preserves_adopted_source_and_cleans_unadopted_siblings(
    owned_source, monkeypatch, tmp_path: Path, shape: str
):
    owner_id, source_id, job_id, path = owned_source
    prefix = f"{owner_id}/{job_id}/"
    sibling = f"{prefix}unadopted.mp4"
    root = tmp_path / "storage"
    for object_path, contents in ((path, b"adopted"), (sibling, b"unadopted")):
        target = root / object_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(contents)
    monkeypatch.setattr(settings, "storage_provider", "local")
    monkeypatch.setattr(settings, "e2e_fixtures", True)
    monkeypatch.setattr(settings, "local_storage_root", str(root))
    if shape == "legacy":
        payload: list[str] | dict = [path]
    elif shape == "v2":
        payload = build_job_storage_manifest(
            job_id=job_id,
            exact_paths=[path],
            prefixes=[prefix],
            not_before=datetime.now(UTC) - timedelta(seconds=1),
        ).to_payload()
    else:
        payload = {
            "_creation_project_exact_paths": [path],
            "_creation_project_prefixes": [prefix],
        }
    with sync_session() as db:
        outbox = JobStorageDeletion(job_id=job_id, object_paths=payload)
        db.add(outbox)
        db.commit()
        outbox_id = outbox.id
    result = account_lifecycle.purge_job_storage.run(str(outbox_id))
    assert result["status"] == "completed"
    assert (root / path).read_bytes() == b"adopted"
    if shape == "legacy":
        # Legacy manifests have no prefix target and therefore make no prefix
        # call; exact adopted key filtering leaves the sibling untouched.
        assert (root / sibling).read_bytes() == b"unadopted"
    else:
        assert not (root / sibling).exists()
    with sync_session() as db:
        assert db.get(MediaAssetSource, source_id).status == "available"


def test_project_prefix_outage_retains_v2_outbox_and_adopted_source(owned_source, monkeypatch):
    owner_id, source_id, job_id, path = owned_source
    prefix = f"{owner_id}/{job_id}/"
    payload = build_job_storage_manifest(
        job_id=job_id,
        exact_paths=[path],
        prefixes=[prefix],
        not_before=datetime.now(UTC) - timedelta(seconds=1),
    ).to_payload()
    with sync_session() as db:
        outbox = JobStorageDeletion(job_id=job_id, object_paths=payload)
        db.add(outbox)
        db.commit()
        outbox_id = outbox.id
    monkeypatch.setattr(
        "app.storage.delete_prefix_verified",
        lambda _prefix, **_kwargs: PrefixDeletionResult(status="unavailable", remaining=None),
    )

    result = account_lifecycle.purge_job_storage.run(str(outbox_id))

    assert result["status"] == "pending"
    with sync_session() as db:
        outbox = db.get(JobStorageDeletion, outbox_id)
        assert outbox.status == "pending"
        assert outbox.object_paths["prefixes"][0]["prefix"] == prefix
        assert db.get(MediaAssetSource, source_id).status == "available"


@pytest.mark.asyncio
async def test_confirmed_account_erasure_cascades_library_and_owns_both_storage_roots(
    owned_source, monkeypatch
):
    owner_id, source_id, _, _ = owned_source
    key = Fernet.generate_key().decode()
    monkeypatch.setattr(me.settings, "token_encryption_key", key)
    dispatch = AsyncMock()
    monkeypatch.setattr(me, "_delete_job_storage_after_commit", dispatch)
    monkeypatch.setattr(account_lifecycle.purge_user_storage, "delay", lambda *_a: None)
    token = Fernet(key.encode()).encrypt(str(owner_id).encode()).decode()
    async with AsyncSessionLocal() as db:
        user = await db.get(User, owner_id)
        response = await me.confirm_account_deletion(me.DeleteConfirmBody(token=token), user, db)
    assert response.status_code == 204
    dispatch.assert_awaited_once()
    with sync_session() as db:
        assert db.get(User, owner_id) is None
        assert db.get(MediaAssetSource, source_id) is None
        outbox = db.get(JobStorageDeletion, dispatch.call_args.args[0])
        assert outbox.object_prefixes == [f"users/{owner_id}/", f"{owner_id}/"]
        assert outbox.next_attempt_at > datetime.now(UTC) + timedelta(hours=24)
        db.delete(outbox)
        db.commit()
