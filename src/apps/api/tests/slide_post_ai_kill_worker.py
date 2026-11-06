# ruff: noqa: E402
"""Fresh-interpreter fake AI worker; exact scratch DB only, all external sockets denied."""

import asyncio
import json
import os
import socket
import sys
from pathlib import Path
from urllib.parse import urlparse

expected = urlparse(os.environ["DATABASE_URL"])
assert expected.path == "/nova_auto_convergence_ai_20261106_test"
assert expected.hostname in {"localhost", "127.0.0.1", "::1"} and (expected.port or 5432) == 5432

connect = socket.socket.connect
connect_ex = socket.socket.connect_ex


def allowed(address):
    assert (
        isinstance(address, tuple)
        and address[0] in {"localhost", "127.0.0.1", "::1"}
        and address[1] == 5432
    ), "External network forbidden"


def guarded(self, address):
    allowed(address)
    return connect(self, address)


def guarded_ex(self, address):
    allowed(address)
    return connect_ex(self, address)


socket.socket.connect = guarded
socket.socket.connect_ex = guarded_ex
import asyncpg
import psycopg2
import pydantic_settings


def assert_db(dsn=None, **kwargs):
    values = {}
    if dsn:
        if dsn.startswith(("postgresql://", "postgres://")):
            parsed = urlparse(dsn)
            values = {
                "host": parsed.hostname,
                "port": parsed.port or 5432,
                "database": parsed.path.lstrip("/"),
            }
        else:
            values = psycopg2.extensions.parse_dsn(dsn)
    values.update(kwargs)
    assert values.get("dbname", values.get("database")) == expected.path.lstrip("/")
    assert (
        values.get("host") in {"localhost", "127.0.0.1", "::1"}
        and int(values.get("port", 5432)) == 5432
    )


original_sync = psycopg2.connect
original_async = asyncpg.connect


def guarded_sync(dsn=None, *args, **kwargs):
    assert_db(dsn, **kwargs)
    return original_sync(dsn, *args, **kwargs)


async def guarded_async(dsn=None, **kwargs):
    assert_db(dsn, **kwargs)
    return await original_async(dsn, **kwargs)


psycopg2.connect = guarded_sync
asyncpg.connect = guarded_async
original_init = pydantic_settings.BaseSettings.__init__


def isolated_settings(self, **values):
    values["_env_file"] = None
    original_init(self, **values)


pydantic_settings.BaseSettings.__init__ = isolated_settings
import uuid

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

import app.agents._model_client as clients
from app.agents.edit_copilot import EditCopilotAgent
from app.config import settings
from app.services import slide_post_ai_delivery as delivery
from app.services import slide_post_chat_edit as chat
from app.services.slide_post_ai_requests import SQLAIRequestStore

identifier, stage, counter_path, admission = json.loads(sys.argv[1])
settings.slide_post_ai_recovery_enabled = True
settings.slide_posts_enabled = True
settings.slide_post_chat_edit_enabled = True
settings.slide_post_rich_text_enabled = True


def boundary():
    print("BOUNDARY", flush=True)
    sys.stdin.read()
    raise AssertionError("fixture must be terminated at the boundary")


original_claim = SQLAIRequestStore.claim


async def claim(self, key, snapshot):
    if stage == "before_claim":
        boundary()
    token = await original_claim(self, key, snapshot)
    if stage == "after_claim" and token:
        boundary()
    return token


class Fake:
    def __init__(self, _client):
        pass

    def run(self, value, ctx=None):
        with Path(counter_path).open("a") as output:
            output.write("called\n")
            output.flush()
            os.fsync(output.fileno())
        if stage == "after_provider":
            boundary()
        return EditCopilotAgent(None).parse(
            json.dumps(
                {"intent": "edit", "ops": [], "confidence": 0.9, "reply": "ok", "suggestions": []}
            ),
            value,
        )


SQLAIRequestStore.claim = claim
chat.EditCopilotAgent = Fake
clients.default_client = lambda: object()


async def run():
    engine = create_async_engine(settings.asyncpg_database_url, poolclass=NullPool)
    try:
        async with async_sessionmaker(engine, expire_on_commit=False)() as db:
            if stage == "before_enqueue":
                from inspect import unwrap
                from types import SimpleNamespace

                from starlette.requests import Request

                from app.routes.slide_post_ai_requests import AIRequestBody, request_ai

                settings.kria_brief_binding_enabled = True
                settings.kria_brief_binding_user_ids = []
                settings.thought_summaries_enabled = False

                async def pause_inline(*args, **kwargs):
                    pass

                async def pause_publish(*args, **kwargs):
                    boundary()

                delivery.deliver = pause_inline
                delivery.publish_due = pause_publish
                await unwrap(request_ai)(
                    Request({"type": "http", "headers": []}),
                    admission["item_id"],
                    AIRequestBody.model_validate(admission["body"]),
                    SimpleNamespace(id=uuid.UUID(admission["owner_id"])),
                    db,
                    None,
                )
            else:
                await delivery.deliver(db, uuid.UUID(identifier))
    finally:
        await engine.dispose()
    if stage == "observe":
        print("OBSERVED", flush=True)
        return
    if stage == "after_completion":
        boundary()
    raise AssertionError("worker did not reach requested fault boundary")


asyncio.run(run())
