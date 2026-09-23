"""PostgreSQL regression: plan-item routes that roll back before slow work.

These routes release their read transaction with ``db.rollback()`` before a
model call, storage I/O or a client upload stream.  The rollback expires every
instance in the request session, including the ``User`` that
``get_current_user`` loaded into it, so any later ``user.id`` lazy-loads
outside the greenlet (``MissingGreenlet``).  Inside a broad ``except`` that
silently turned into "Shot list generation failed" and a skipped script agent;
elsewhere it was a 500.  Uses the real dependency on the same ``AsyncSession``.
"""

from __future__ import annotations

import hashlib
import io
import sys
import types
import uuid

import pytest
from sqlalchemy import delete, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import OperationalError
from starlette.datastructures import Headers, UploadFile
from starlette.requests import Request

from app import storage
from app.auth import get_current_user
from app.config import settings
from app.database import AsyncSessionLocal, sync_engine
from app.database import engine as async_engine
from app.models import ContentPlan, Persona, PlanItem, PlanItemAsset, User
from app.routes import plan_items

_database_name = make_url(settings.database_url).database or ""
if not _database_name.endswith("_test"):
    pytest.skip(
        f"refusing to write plan-item integration fixtures to {_database_name!r}",
        allow_module_level=True,
    )
try:
    with sync_engine.connect() as _probe:
        _probe.execute(text("SELECT 1"))
except (OperationalError, OSError) as exc:
    pytest.skip(f"test PostgreSQL unavailable: {exc!r}", allow_module_level=True)


def _request(user_id: uuid.UUID) -> Request:
    request = Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/",
            "headers": [(b"x-user-id", str(user_id).encode())],
            "client": (f"test-{uuid.uuid4()}", 0),
        }
    )
    request.state.correlation_id = None
    return request


@pytest.fixture()
async def owned_item(request: pytest.FixtureRequest):
    await async_engine.dispose()
    request.addfinalizer(lambda: async_engine.sync_engine.dispose(close=False))
    user_id, plan_id, item_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    async with AsyncSessionLocal() as db:
        db.add(User(id=user_id, email=f"plan-rollback-{user_id}@example.test"))
        persona = Persona(
            user_id=user_id,
            persona_status="ready",
            persona={"summary": "test creator"},
            questionnaire={},
        )
        db.add(persona)
        await db.flush()
        db.add(ContentPlan(id=plan_id, user_id=user_id, persona_id=persona.id, plan_status="ready"))
        await db.flush()
        db.add(
            PlanItem(
                id=item_id,
                content_plan_id=plan_id,
                position=1,
                idea="Sunrise run",
                theme="morning",
                item_status="awaiting_clips",
                clip_gcs_paths=[],
            )
        )
        await db.commit()
    try:
        yield {"user_id": user_id, "item_id": item_id}
    finally:
        try:
            async with AsyncSessionLocal() as db:
                await db.execute(delete(PlanItemAsset).where(PlanItemAsset.user_id == user_id))
                await db.execute(delete(PlanItem).where(PlanItem.id == item_id))
                await db.execute(delete(ContentPlan).where(ContentPlan.id == plan_id))
                await db.execute(delete(Persona).where(Persona.user_id == user_id))
                await db.execute(delete(User).where(User.id == user_id))
                await db.commit()
        finally:
            await async_engine.dispose()


async def _request_user(db, user_id: uuid.UUID) -> User:
    return await get_current_user(
        x_user_id=str(user_id),
        authorization=f"Bearer {settings.internal_api_key}",
        db=db,
    )


async def test_generate_guide_persists_after_releasing_the_read_transaction(
    owned_item, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.agents.shot_list_writer import ShotListWriterOutput, ShotSpecWithCount

    calls: list[dict[str, str]] = []

    def _writer(inp, *, creator_id, request_id):  # noqa: ANN001, ANN202
        calls.append({"creator_id": creator_id, "request_id": request_id})
        return ShotListWriterOutput(shots=[ShotSpecWithCount(what="lace up", how="close-up")])

    monkeypatch.setattr("app.agents.shot_list_writer.run_shot_list_writer", _writer)
    async with AsyncSessionLocal() as db:
        user = await _request_user(db, owned_item["user_id"])
        out = await plan_items.generate_guide(str(owned_item["item_id"]), user, db)

    assert [call["creator_id"] for call in calls] == [str(owned_item["user_id"])]
    assert calls[0]["request_id"].startswith(f"shot-list:{owned_item['item_id']}:")
    assert [shot.what for shot in out.filming_guide] == ["lace up"]


async def test_transcript_script_runs_the_agent_after_releasing_the_read_transaction(
    owned_item, monkeypatch: pytest.MonkeyPatch
) -> None:
    import app.agents._model_client as model_client
    import app.agents.voiceover_script_writer as writer

    creators: list[str] = []

    class _Agent:
        def __init__(self, _client) -> None:  # noqa: ANN001
            pass

        def run(self, _inp, *, ctx):  # noqa: ANN001, ANN202
            creators.append(ctx.creator_id)
            return types.SimpleNamespace(text="Run at sunrise.", lines=["Run at sunrise."])

    monkeypatch.setattr(settings, "transcript_helper_enabled", True)
    monkeypatch.setattr(settings, "gemini_api_key", "test-key")
    monkeypatch.setattr(model_client, "default_client", lambda: None)
    monkeypatch.setattr(writer, "VoiceoverScriptWriterAgent", _Agent)
    async with AsyncSessionLocal() as db:
        user = await _request_user(db, owned_item["user_id"])
        out = await plan_items.transcript_script(
            _request(owned_item["user_id"]),
            str(owned_item["item_id"]),
            plan_items.TranscriptScriptBody(brief="a day at the beach"),
            user,
            db,
        )

    # The agent path ran (not the heuristic fallback it used to degrade to).
    assert creators == [str(owned_item["user_id"])]
    assert out.text == "Run at sunrise."
    assert out.version == 1


async def test_transcribe_direction_audio_persists_after_releasing_the_read_transaction(
    owned_item, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = f"users/{owned_item['user_id']}/plan/{owned_item['item_id']}/direction-audio/a.m4a"
    monkeypatch.setattr(
        storage,
        "object_metadata",
        lambda _path: types.SimpleNamespace(size=2048, content_type="audio/mp4"),
    )
    monkeypatch.setattr(storage, "download_to_file", lambda _path, _local: None)
    monkeypatch.setattr(
        "app.services.audio_download.probe_duration", lambda _local: 4.0, raising=False
    )
    transcribe = types.ModuleType("app.pipeline.transcribe")
    transcribe.transcribe_whisper = lambda _local: types.SimpleNamespace(
        full_text="Open on the sunrise"
    )
    monkeypatch.setitem(sys.modules, "app.pipeline.transcribe", transcribe)
    async with AsyncSessionLocal() as db:
        user = await _request_user(db, owned_item["user_id"])
        out = await plan_items.transcribe_direction_audio(
            _request(owned_item["user_id"]),
            str(owned_item["item_id"]),
            plan_items.DirectionAudioTranscribeBody(gcs_path=path),
            user,
            db,
        )

    assert out.notes == "Open on the sunrise"


async def test_pool_upload_dedupe_returns_the_existing_asset(
    owned_item, monkeypatch: pytest.MonkeyPatch
) -> None:
    payload = b"\x89PNG\r\n\x1a\n pool asset bytes"
    async with AsyncSessionLocal() as db:
        existing = PlanItemAsset(
            plan_item_id=owned_item["item_id"],
            user_id=owned_item["user_id"],
            gcs_path=f"users/{owned_item['user_id']}/plan/{owned_item['item_id']}/pool/a.png",
            kind="image",
            content_hash=hashlib.sha256(payload).hexdigest(),
            source_filename="a.png",
            status="ready",
        )
        db.add(existing)
        await db.commit()
        existing_id = existing.id
    monkeypatch.setattr(settings, "overlay_autoplace_enabled", True)
    # Signing is best-effort in _asset_out; without a stub it probes for GCS
    # credentials over the network for several seconds.
    monkeypatch.setattr(
        storage, "signed_get_url", lambda path, **_kwargs: f"https://signed.test/{path}"
    )
    upload = UploadFile(
        file=io.BytesIO(payload),
        filename="a.png",
        headers=Headers({"content-type": "image/png"}),
    )
    async with AsyncSessionLocal() as db:
        user = await _request_user(db, owned_item["user_id"])
        out = await plan_items.upload_pool_asset(
            str(owned_item["item_id"]),
            _request(owned_item["user_id"]),
            user,
            upload,
            db,
        )

    assert out.id == str(existing_id)
    assert out.deduped is True
    async with AsyncSessionLocal() as db:
        assets = (
            await db.execute(
                select(PlanItemAsset.id).where(PlanItemAsset.user_id == owned_item["user_id"])
            )
        ).all()
    assert len(assets) == 1
