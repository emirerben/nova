"""Enforce the content-plan/persona tenant invariant in PostgreSQL.

Revision ID: 0072
Revises: 0071
Create Date: 2026-08-11

R1 made every application path fail closed and introduced a durable epoch.
This stacked release makes the same owner equality impossible to violate at
the storage boundary:

    content_plans(persona_id, user_id)
        -> personas(id, user_id)

The constraint is installed NOT VALID, so PostgreSQL protects every new write
immediately, then an explicit validation proves all existing rows. Short lock
and statement timeouts make a busy or unexpectedly large deployment fail
safely instead of blocking application traffic indefinitely.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import sqlalchemy as sa
from alembic import op

revision = "0072"
down_revision = "0071"
branch_labels = None
depends_on = None

_PERSONA_OWNER_UNIQUE = "uq_personas_id_user_id"
_PERSONA_OWNER_FK = "fk_content_plans_persona_owner"
_LEGACY_PERSONA_FK = "content_plans_persona_id_fkey"

_REQUIRED_OWNER_COLUMNS = {
    ("content_plans", "persona_id"),
    ("content_plans", "user_id"),
    ("personas", "id"),
    ("personas", "user_id"),
}


def _set_ddl_timeouts(bind: Any) -> None:
    bind.execute(sa.text("SET LOCAL lock_timeout = '5s'"))
    bind.execute(sa.text("SET LOCAL statement_timeout = '30s'"))


def _catalog_owner_columns(bind: Any) -> list[Mapping[str, Any]]:
    return (
        bind.execute(
            sa.text(
                """
                SELECT
                    c.relname AS table_name,
                    a.attname AS column_name,
                    a.attnotnull,
                    t.typname AS type_name
                FROM pg_catalog.pg_attribute AS a
                JOIN pg_catalog.pg_class AS c ON c.oid = a.attrelid
                JOIN pg_catalog.pg_type AS t ON t.oid = a.atttypid
                WHERE (
                    (a.attrelid = to_regclass('content_plans')
                     AND a.attname IN ('persona_id', 'user_id'))
                    OR
                    (a.attrelid = to_regclass('personas')
                     AND a.attname IN ('id', 'user_id'))
                )
                  AND a.attnum > 0
                  AND NOT a.attisdropped
                """
            )
        )
        .mappings()
        .all()
    )


def _verify_catalog_contract(bind: Any) -> None:
    rows = _catalog_owner_columns(bind)
    by_column = {
        (str(row["table_name"]), str(row["column_name"])): row for row in rows
    }
    if set(by_column) != _REQUIRED_OWNER_COLUMNS:
        missing = sorted(_REQUIRED_OWNER_COLUMNS - set(by_column))
        unexpected = sorted(set(by_column) - _REQUIRED_OWNER_COLUMNS)
        raise RuntimeError(
            "Cannot enforce persona ownership: catalog schema/column shape drifted "
            f"(missing={missing}, unexpected={unexpected})"
        )

    nullable = sorted(key for key, row in by_column.items() if not row["attnotnull"])
    if nullable:
        raise RuntimeError(
            "Cannot enforce persona ownership: owner columns must be NOT NULL "
            f"(nullable={nullable})"
        )

    # These IDs are UUIDs in every released schema. Requiring the underlying
    # PostgreSQL type (rather than accepting merely cast-compatible columns)
    # keeps the FK contract exact and rejects drifted domains.
    type_names = {str(row["type_name"]) for row in by_column.values()}
    if type_names != {"uuid"}:
        raise RuntimeError(
            "Cannot enforce persona ownership: owner columns must all be UUID "
            f"(types={sorted(type_names)})"
        )


def _find_owner_mismatch(bind: Any) -> Mapping[str, Any] | None:
    return (
        bind.execute(
            sa.text(
                """
                SELECT
                    cp.id AS plan_id,
                    cp.user_id AS plan_user_id,
                    cp.persona_id,
                    p.user_id AS persona_user_id
                FROM content_plans AS cp
                LEFT JOIN personas AS p
                  ON p.id = cp.persona_id
                WHERE p.id IS NULL
                   OR p.user_id IS DISTINCT FROM cp.user_id
                LIMIT 1
                """
            )
        )
        .mappings()
        .first()
    )


def _legacy_persona_fks(bind: Any) -> list[Mapping[str, Any]]:
    # Match by relation and ordered column numbers, never by a guessed name.
    # The two-column FK created above cannot satisfy these one-column arrays.
    return (
        bind.execute(
            sa.text(
                """
                SELECT
                    con.conname AS constraint_name,
                    con.convalidated AS is_validated,
                    con.confdeltype::text AS delete_action
                FROM pg_catalog.pg_constraint AS con
                WHERE con.contype = 'f'
                  AND con.conrelid = to_regclass('content_plans')
                  AND con.confrelid = to_regclass('personas')
                  AND con.conkey = ARRAY[
                      (SELECT attnum::smallint
                       FROM pg_catalog.pg_attribute
                       WHERE attrelid = to_regclass('content_plans')
                         AND attname = 'persona_id'
                         AND NOT attisdropped)
                  ]
                  AND con.confkey = ARRAY[
                      (SELECT attnum::smallint
                       FROM pg_catalog.pg_attribute
                       WHERE attrelid = to_regclass('personas')
                         AND attname = 'id'
                         AND NOT attisdropped)
                  ]
                ORDER BY con.conname
                """
            )
        )
        .mappings()
        .all()
    )


def upgrade() -> None:
    bind = op.get_bind()
    _set_ddl_timeouts(bind)
    _verify_catalog_contract(bind)

    mismatch = _find_owner_mismatch(bind)
    if mismatch is not None:
        raise RuntimeError(
            "Cannot enforce persona ownership while mismatched content plan exists: "
            f"plan_id={mismatch['plan_id']}, persona_id={mismatch['persona_id']}, "
            f"plan_user_id={mismatch['plan_user_id']}, "
            f"persona_user_id={mismatch['persona_user_id']}"
        )

    op.create_unique_constraint(
        _PERSONA_OWNER_UNIQUE,
        "personas",
        ["id", "user_id"],
    )
    op.create_foreign_key(
        _PERSONA_OWNER_FK,
        "content_plans",
        "personas",
        ["persona_id", "user_id"],
        ["id", "user_id"],
        ondelete="CASCADE",
        match="FULL",
        postgresql_not_valid=True,
    )
    bind.execute(
        sa.text(
            f"""
            ALTER TABLE content_plans
            VALIDATE CONSTRAINT {_PERSONA_OWNER_FK}
            """
        )
    )

    # Do not remove the legacy protection until the stronger compound FK has
    # validated. Match by relation/columns and then verify every released
    # semantic, not merely the name, before touching it. The enclosing Alembic
    # transaction rolls the whole migration back on any catalog drift.
    legacy_fks = _legacy_persona_fks(bind)
    if len(legacy_fks) != 1:
        raise RuntimeError(
            "Cannot replace legacy persona FK safely: expected exactly one "
            f"persona_id -> personas.id constraint, found {legacy_fks}"
        )
    legacy_fk = legacy_fks[0]
    if (
        legacy_fk["constraint_name"] != _LEGACY_PERSONA_FK
        or legacy_fk["is_validated"] is not True
        or legacy_fk["delete_action"] != "c"
    ):
        raise RuntimeError(
            "Cannot replace legacy persona FK safely: catalog semantics drifted "
            f"(constraint={dict(legacy_fk)})"
        )
    op.drop_constraint(_LEGACY_PERSONA_FK, "content_plans", type_="foreignkey")


def downgrade() -> None:
    bind = op.get_bind()
    _set_ddl_timeouts(bind)
    _verify_catalog_contract(bind)

    # Restore and validate the legacy protection before removing either piece
    # of the compound invariant. NOT VALID avoids coupling ADD CONSTRAINT with
    # its table scan; VALIDATE is explicit and must succeed first.
    bind.execute(
        sa.text(
            f"""
            ALTER TABLE content_plans
            ADD CONSTRAINT {_LEGACY_PERSONA_FK}
            FOREIGN KEY (persona_id) REFERENCES personas (id)
            ON DELETE CASCADE
            NOT VALID
            """
        )
    )
    bind.execute(
        sa.text(
            f"""
            ALTER TABLE content_plans
            VALIDATE CONSTRAINT {_LEGACY_PERSONA_FK}
            """
        )
    )
    op.drop_constraint(_PERSONA_OWNER_FK, "content_plans", type_="foreignkey")
    op.drop_constraint(_PERSONA_OWNER_UNIQUE, "personas", type_="unique")
