"""Real PostgreSQL invariants for Apple deletion-revocation durability."""

from __future__ import annotations

import os
import uuid

import pytest
from sqlalchemy import create_engine, func, select, text
from sqlalchemy.exc import OperationalError

from app.models import AppleRevocationOutbox
from app.services.auth_locks import account_lifecycle_lock_key


@pytest.fixture(scope="module")
def pg_engine():
    """The invoking command supplies the isolated kri55_test DATABASE_URL."""
    url = os.environ["DATABASE_URL"].replace("postgresql+asyncpg://", "postgresql://")
    engine = create_engine(url, pool_pre_ping=True)
    try:
        with engine.connect() as connection:
            # CI uses nova_test; the dedicated local verification cluster uses
            # kri55_test. Refuse every other database, especially development
            # or production, before writing even fixture-owned UUID rows.
            assert connection.scalar(text("SELECT current_database()")) in {
                "nova_test",
                "kri55_test",
            }
            connection.execute(text("SELECT 1 FROM apple_revocation_outbox LIMIT 1"))
    except OperationalError as exc:
        engine.dispose()
        pytest.skip(f"dedicated PostgreSQL unavailable: {exc!r}")
    yield engine
    engine.dispose()


def test_outbox_row_is_atomic_across_rollback_and_commit(pg_engine) -> None:
    rollback_id = uuid.uuid4()
    committed_id = uuid.uuid4()
    with pg_engine.connect() as connection:
        transaction = connection.begin()
        connection.execute(
            AppleRevocationOutbox.__table__.insert().values(
                id=rollback_id, encrypted_refresh_token=b"rollback-ciphertext", client_id="ios"
            )
        )
        transaction.rollback()
        assert (
            connection.scalar(
                select(func.count())
                .select_from(AppleRevocationOutbox)
                .where(AppleRevocationOutbox.id == rollback_id)
            )
            == 0
        )
        # The verification SELECT starts SQLAlchemy's implicit transaction.
        connection.commit()
        with connection.begin():
            connection.execute(
                AppleRevocationOutbox.__table__.insert().values(
                    id=committed_id,
                    encrypted_refresh_token=b"committed-ciphertext",
                    client_id="ios",
                )
            )
    with pg_engine.connect() as connection:
        ciphertext = connection.scalar(
            select(AppleRevocationOutbox.encrypted_refresh_token).where(
                AppleRevocationOutbox.id == committed_id
            )
        )
        assert ciphertext == b"committed-ciphertext"
        connection.execute(
            AppleRevocationOutbox.__table__.delete().where(AppleRevocationOutbox.id == committed_id)
        )
        connection.commit()


def test_account_lifecycle_advisory_lock_excludes_concurrent_link(pg_engine) -> None:
    user_id = uuid.uuid4()
    key = account_lifecycle_lock_key(user_id)
    first = pg_engine.connect()
    second = pg_engine.connect()
    try:
        first_transaction = first.begin()
        first.execute(text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"), {"key": key})
        assert (
            second.scalar(
                text("SELECT pg_try_advisory_xact_lock(hashtextextended(:key, 0))"), {"key": key}
            )
            is False
        )
        first_transaction.commit()
        assert (
            second.scalar(
                text("SELECT pg_try_advisory_xact_lock(hashtextextended(:key, 0))"), {"key": key}
            )
            is True
        )
        second.commit()
    finally:
        first.close()
        second.close()
