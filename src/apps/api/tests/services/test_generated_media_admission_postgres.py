"""Opt-in migration replay on a disposable database, never the shared test DB."""

import importlib.util
import os
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app import storage
from app.models import ContentPlan, GeneratedMediaOperation, Persona, PlanItem, PlanItemAsset, User
from app.services.generated_media_admission import (
    admit_generated_asset,
    cleanup_expired_generated_media,
    persist_generated_asset,
)
from tests.services.test_generated_media_admission import png


def test_generated_media_postgres_contract(monkeypatch):
    url = os.environ.get("NOVA_ADMISSION_TEST_DATABASE_URL")
    if not url:
        pytest.skip("requires dedicated admission scratch database")
    if not url.endswith("/nova_admission_test"):
        pytest.fail("use only dedicated nova_admission_test database")
    spec = importlib.util.spec_from_file_location(
        "migration", "app/migrations/versions/0127_generated_media_operations.py"
    )
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    engine = create_engine(url)
    with engine.begin() as conn:
        conn.execute(text("DROP TABLE IF EXISTS generated_media_operations"))
    with engine.begin() as conn:
        m.op = Operations(MigrationContext.configure(conn))
        m.upgrade()
    with Session(engine) as db:
        user = User(email=f"{uuid.uuid4()}@example.test")
        db.add(user)
        db.flush()
        persona = Persona(user_id=user.id)
        db.add(persona)
        db.flush()
        plan = ContentPlan(user_id=user.id, persona_id=persona.id)
        db.add(plan)
        db.flush()
        item = PlanItem(content_plan_id=plan.id, position=0, idea="fixture")
        db.add(item)
        db.flush()
        op = GeneratedMediaOperation(
            user_id=user.id,
            plan_item_id=item.id,
            client_event_id="fixture",
            request_digest="a" * 64,
            capability="image",
            pending_content=b"png",
            expires_at=datetime.now(UTC) - timedelta(seconds=1),
        )
        db.add(op)
        db.commit()
        op_id = op.id
        assert cleanup_expired_generated_media(db) == 1
        db.commit()
        db.refresh(op)
        assert op.pending_content is None and op.state == "failed"
        for sql in [
            "UPDATE generated_media_operations SET pending_content="
            "decode(repeat('00',20971521),'hex') WHERE id=:id",
            "UPDATE generated_media_operations SET state='bad' WHERE id=:id",
        ]:
            try:
                with db.begin_nested():
                    db.execute(text(sql), {"id": op_id})
            except IntegrityError:
                pass
            else:
                raise AssertionError("constraint accepted invalid row")
        try:
            with db.begin_nested():
                db.add(
                    GeneratedMediaOperation(
                        user_id=user.id,
                        plan_item_id=item.id,
                        client_event_id="fixture",
                        request_digest="b" * 64,
                        capability="image",
                    )
                )
                db.flush()
        except IntegrityError:
            pass
        else:
            raise AssertionError("duplicate operation accepted")
        generated = GeneratedMediaOperation(
            user_id=user.id,
            plan_item_id=item.id,
            client_event_id="admission",
            request_digest="c" * 64,
            capability="image",
            state="submitting",
        )
        db.add(generated)
        db.commit()
        owner = dict(owner_id=user.id, plan_item_id=item.id)
        persist_generated_asset(db, generated, png(), **owner)
        db.commit()
        objects = {}

        def upload(local, path, mime):
            objects[path] = Path(local).read_bytes()
            return storage.ObjectMetadata(path, "77", None, len(objects[path]), mime)

        def download(path, local, *, generation):
            assert generation == "77"
            Path(local).write_bytes(objects[path])

        monkeypatch.setattr(storage, "upload_local_file_immutable", upload)
        monkeypatch.setattr(storage, "download_generation_to_file", download)
        admit_generated_asset(db, generated, **owner)
        db.commit()
        db.expire_all()
        assert generated.state == "ready" and generated.pending_content is None
        registered = db.get(PlanItemAsset, generated.asset_id)
        assert registered.status == "ready" and registered.gcs_path == generated.output_path
        first_id = registered.id
        admit_generated_asset(db, generated, **owner)
        db.commit()
        assert generated.asset_id == first_id
        db.execute(text("DELETE FROM plan_items WHERE id=:id"), {"id": item.id})
        db.commit()
        assert (
            db.scalar(
                text("SELECT count(*) FROM generated_media_operations WHERE id=:id"), {"id": op_id}
            )
            == 0
        )
    with engine.begin() as conn:
        m.op = Operations(MigrationContext.configure(conn))
        m.downgrade()
        m.upgrade()
