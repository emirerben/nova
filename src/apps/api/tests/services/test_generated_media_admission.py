"""Offline admission failures: owner escape, corruption, retry, stale native pins.

Real PNG/WAV decoders and native receipt builders; only DB transport and storage
are substituted. PostgreSQL persistence is covered separately by migration checks.
"""

import io
import uuid
import wave
from contextlib import contextmanager
from datetime import UTC
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from PIL import Image

from app import storage
from app.models import GeneratedMediaOperation
from app.services.generated_asset_contracts import GeneratedAsset
from app.services.generated_media_admission import admit_generated_asset, persist_generated_asset
from app.services.phone_visuals import bind_phone_visual_assets
from app.services.phone_voiceover import inspect_voiceover_asset


@pytest.fixture
def setup(monkeypatch):
    owner, item = uuid.uuid4(), uuid.uuid4()
    op = GeneratedMediaOperation(
        id=uuid.uuid4(),
        user_id=owner,
        plan_item_id=item,
        client_event_id="event",
        request_digest="a" * 64,
        capability="image",
        state="submitting",
    )
    db = Mock()
    db.scalar.return_value = item
    objects = {}

    def upload(local, path, mime):
        objects[path] = Path(local).read_bytes()
        return storage.ObjectMetadata(path, "77", None, len(objects[path]), mime)

    def download(path, local, *, generation):
        if generation != "77":
            raise FileNotFoundError(path)
        Path(local).write_bytes(objects[path])

    monkeypatch.setattr(storage, "upload_local_file_immutable", upload)
    monkeypatch.setattr(storage, "download_generation_to_file", download)
    monkeypatch.setattr(
        storage,
        "object_metadata",
        lambda path: storage.ObjectMetadata(path, "77", None, len(objects[path]), "audio/wav"),
    )
    return db, op, dict(owner_id=owner, plan_item_id=item), objects


def png():
    out = io.BytesIO()
    Image.new("RGB", (1024, 1024), "red").save(out, format="PNG")
    return GeneratedAsset(out.getvalue(), "image/png", "test", {}, editable_title="Editable")


def test_image_retry_and_native_binding(setup, monkeypatch):
    db, op, owner, objects = setup
    asset = png()
    persist_generated_asset(db, op, asset, **owner)
    upload = storage.upload_local_file_immutable
    monkeypatch.setattr(
        storage, "upload_local_file_immutable", Mock(side_effect=OSError("offline"))
    )
    with pytest.raises(OSError):
        admit_generated_asset(db, op, **owner)
    assert op.state == "provider_complete" and op.pending_content == asset.content
    monkeypatch.setattr(storage, "upload_local_file_immutable", upload)
    admit_generated_asset(db, op, **owner)
    row = db.add.call_args.args[0]
    assert row.status == row.media_status == "ready"
    assert op.pending_content is None and op.state == "ready"
    admit_generated_asset(db, op, **owner)
    assert db.add.call_count == 1
    db.get.return_value = SimpleNamespace(content_plan_item_id=op.plan_item_id, user_id=op.user_id)
    db.execute.return_value.scalars.return_value.all.return_value = [row]

    @contextmanager
    def session():
        yield db

    pins = {str(row.id): ("image", row.gcs_path, row.gcs_generation)}
    receipt = bind_phone_visual_assets(session, job_id=str(uuid.uuid4()), pins=pins)[0]
    assert receipt.sha256 == op.content_sha256 and receipt.byte_count == len(asset.content)
    pins[str(row.id)] = ("image", row.gcs_path, "stale")
    with pytest.raises(ValueError, match="no longer"):
        bind_phone_visual_assets(session, job_id=str(uuid.uuid4()), pins=pins)


def test_refuse_foreign_owner_and_corrupted_bytes(setup):
    db, op, owner, objects = setup
    with pytest.raises(ValueError, match="owner"):
        persist_generated_asset(db, op, png(), **(owner | {"owner_id": uuid.uuid4()}))
    db.scalar.return_value = None
    with pytest.raises(ValueError, match="owner"):
        persist_generated_asset(db, op, png(), **owner)
    db.scalar.return_value = op.plan_item_id
    persist_generated_asset(db, op, png(), **owner)
    op.pending_content = b"corrupt"
    with pytest.raises(ValueError, match="digest"):
        admit_generated_asset(db, op, **owner)
    assert not objects


def test_narration_remains_unattached_and_native_inspection(setup):
    db, op, owner, objects = setup
    op.capability = "narration"
    out = io.BytesIO()
    with wave.open(out, "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(24000)
        audio.writeframes(b"\0\0" * 24000)
    persist_generated_asset(
        db, op, GeneratedAsset(out.getvalue(), "audio/wav", "test", {}), **owner
    )
    admit_generated_asset(db, op, **owner)
    assert op.output_path.startswith(f"voiceover-uploads/direct/{op.user_id}/")
    assert op.duration_s == 1 and op.asset_id is None
    db.add.assert_not_called()
    receipt = inspect_voiceover_asset(
        op.output_path, asset_id=str(op.id), plan_item_id=str(op.plan_item_id)
    )
    assert receipt.fingerprint.sha256 == op.content_sha256


def test_reject_same_size_storage_corruption(setup, monkeypatch):
    db, op, owner, objects = setup
    persist_generated_asset(db, op, png(), **owner)

    def corrupt(path, local, *, generation):
        Path(local).write_bytes(b"x" * len(objects[path]))

    monkeypatch.setattr(storage, "download_generation_to_file", corrupt)
    with pytest.raises(ValueError, match="stored digest"):
        admit_generated_asset(db, op, **owner)
    assert op.pending_content and op.failure_code == "storage_failed"
    db.add.assert_not_called()


def test_expired_result_not_uploaded(setup):
    from datetime import datetime, timedelta

    db, op, owner, objects = setup
    persist_generated_asset(db, op, png(), **owner)
    op.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    with pytest.raises(ValueError, match="expired"):
        admit_generated_asset(db, op, **owner)
    assert not objects
