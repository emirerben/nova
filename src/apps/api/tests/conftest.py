"""Set required env vars before any app module is imported.

app/config.py instantiates Settings() at module load — required fields must
be present in the environment at collection time, not just at test runtime.
"""

import os

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import OperationalError

os.environ.setdefault("STORAGE_BUCKET", "nova-test")
os.environ.setdefault("STORAGE_PROVIDER", "gcs")
os.environ.setdefault("DATABASE_URL", "postgresql://postgres:postgres@localhost:5432/nova_test")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379")
os.environ.setdefault("OPENAI_API_KEY", "sk-test")
os.environ.setdefault("TOKEN_ENCRYPTION_KEY", "test-key-not-used-in-unit-tests")
os.environ.setdefault("WHISPER_BACKEND", "local")
os.environ.setdefault("WAITLIST_ADMIN_SECRET", "test-admin-secret")
os.environ.setdefault("ALLOWED_ORIGINS", '["http://localhost:3000"]')
# Strict plan-route auth fails closed when INTERNAL_API_KEY is unset, so the
# test env sets it explicitly — strict-path tests must pass the matching bearer
# to exercise the real check (rather than relying on a fail-open bypass).
os.environ.setdefault("INTERNAL_API_KEY", "test-internal-key")


@pytest.fixture(scope="module")
def _build_task_table_lock():
    """Serialize modules that destructively share the build_task table.

    API CI uses pytest-xdist workers against one ``nova_test`` database.  The
    build-task integration modules each TRUNCATE the same table, so without a
    cross-process lock one worker can erase another worker's committed fixture
    between its setup and assertion.  A session-level PostgreSQL advisory lock
    is independent of pytest's scheduling mode and is released automatically
    if a worker process dies.
    """

    db_url = os.environ["DATABASE_URL"].replace(
        "postgresql+asyncpg://",
        "postgresql://",
    )
    engine = create_engine(db_url, pool_pre_ping=True)
    connection = None
    lock_key = 2026082801
    try:
        connection = engine.connect()
        connection.execute(text("SELECT pg_advisory_lock(:key)"), {"key": lock_key})
    except OperationalError as exc:
        if connection is not None:
            connection.close()
        engine.dispose()
        pytest.skip(f"Postgres not reachable for build_task tests: {exc!r}")

    try:
        yield
    finally:
        try:
            try:
                unlocked = connection.scalar(
                    text("SELECT pg_advisory_unlock(:key)"),
                    {"key": lock_key},
                )
                if unlocked is not True:
                    raise RuntimeError("build_task advisory lock was not held at teardown")
            except BaseException:
                # Never return a session with an unproven session-level unlock
                # to this engine's pool. Invalidate its physical connection and
                # fail the test instead of deadlocking the next xdist module.
                connection.invalidate()
                raise
        finally:
            try:
                connection.close()
            finally:
                engine.dispose()
