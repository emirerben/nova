"""Real-PostgreSQL fixture for the content-plan owner constraint migration.

This script is intentionally limited to databases whose name ends in ``_test``.
The CI migration job uses it to prove that revision 0072 rejects a corrupt
pre-existing row and that the installed composite key rejects later corruption.
"""

from __future__ import annotations

import argparse
import os
import uuid

import psycopg2
from psycopg2 import errors
from psycopg2.extras import register_uuid

USER_A = uuid.UUID("10000000-0000-0000-0000-000000000001")
USER_B = uuid.UUID("10000000-0000-0000-0000-000000000002")
PERSONA_A = uuid.UUID("20000000-0000-0000-0000-000000000001")
PERSONA_B = uuid.UUID("20000000-0000-0000-0000-000000000002")
PLAN = uuid.UUID("30000000-0000-0000-0000-000000000001")
SECOND_PLAN = uuid.UUID("30000000-0000-0000-0000-000000000002")


def _connect():
    database_url = os.environ["DATABASE_URL"]
    connection = psycopg2.connect(database_url)
    register_uuid(conn_or_curs=connection)
    with connection.cursor() as cursor:
        cursor.execute("SELECT current_database()")
        database_name = str(cursor.fetchone()[0])
    if not database_name.endswith("_test"):
        connection.close()
        raise RuntimeError(
            "Refusing to run destructive migration fixture outside a *_test database"
        )
    return connection


def seed_mismatch() -> None:
    """Create the exact invalid state that revision 0071 still permits."""

    with _connect() as connection, connection.cursor() as cursor:
        cursor.executemany(
            "INSERT INTO users (id, email) VALUES (%s, %s)",
            [
                (USER_A, "owner-a@constraint.test"),
                (USER_B, "owner-b@constraint.test"),
            ],
        )
        cursor.executemany(
            "INSERT INTO personas (id, user_id) VALUES (%s, %s)",
            [(PERSONA_A, USER_A), (PERSONA_B, USER_B)],
        )
        cursor.execute(
            "INSERT INTO content_plans (id, user_id, persona_id) VALUES (%s, %s, %s)",
            (PLAN, USER_A, PERSONA_B),
        )


def repair_mismatch() -> None:
    """Model the operator repair required before the invariant can be installed."""

    with _connect() as connection, connection.cursor() as cursor:
        cursor.execute(
            "UPDATE content_plans SET persona_id = %s WHERE id = %s",
            (PERSONA_A, PLAN),
        )
        if cursor.rowcount != 1:
            raise AssertionError("expected exactly one corrupt fixture plan to repair")


def _expect_foreign_key_violation(cursor, statement: str, params: tuple[object, ...]) -> None:
    cursor.execute("SAVEPOINT expected_fk_violation")
    try:
        cursor.execute(statement, params)
    except errors.ForeignKeyViolation:
        cursor.execute("ROLLBACK TO SAVEPOINT expected_fk_violation")
        cursor.execute("RELEASE SAVEPOINT expected_fk_violation")
        return
    except Exception:
        # Recover the transaction before re-raising the original unexpected
        # error; a RELEASE against an aborted transaction would mask its cause.
        cursor.execute("ROLLBACK TO SAVEPOINT expected_fk_violation")
        cursor.execute("RELEASE SAVEPOINT expected_fk_violation")
        raise
    else:
        cursor.execute("RELEASE SAVEPOINT expected_fk_violation")
        raise AssertionError("database accepted a cross-owner content-plan/persona link")


def verify_installed_constraint() -> None:
    """Exercise constraint metadata, writes, updates, and ON DELETE CASCADE."""

    with _connect() as connection, connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT
                con.conname,
                con.convalidated,
                con.confmatchtype,
                con.confdeltype,
                ARRAY(
                    SELECT att.attname
                      FROM unnest(con.conkey) WITH ORDINALITY AS key(attnum, ordinal)
                      JOIN pg_attribute AS att
                        ON att.attrelid = con.conrelid
                       AND att.attnum = key.attnum
                     ORDER BY key.ordinal
                ) AS local_columns,
                ARRAY(
                    SELECT att.attname
                      FROM unnest(con.confkey) WITH ORDINALITY AS key(attnum, ordinal)
                      JOIN pg_attribute AS att
                        ON att.attrelid = con.confrelid
                       AND att.attnum = key.attnum
                     ORDER BY key.ordinal
                ) AS remote_columns
              FROM pg_constraint AS con
             WHERE conrelid = 'content_plans'::regclass
               AND conname = 'fk_content_plans_persona_owner'
            """
        )
        assert cursor.fetchone() == (
            "fk_content_plans_persona_owner",
            True,
            "f",  # MATCH FULL
            "c",  # ON DELETE CASCADE
            ["persona_id", "user_id"],
            ["id", "user_id"],
        )

        cursor.execute(
            """
            SELECT count(*)
              FROM pg_constraint
             WHERE conrelid = 'personas'::regclass
               AND conname = 'uq_personas_id_user_id'
               AND contype = 'u'
            """
        )
        assert cursor.fetchone()[0] == 1

        cursor.execute(
            """
            SELECT count(*)
              FROM pg_constraint
             WHERE conrelid = 'content_plans'::regclass
               AND conname = 'content_plans_persona_id_fkey'
            """
        )
        assert cursor.fetchone()[0] == 0, "legacy persona-only FK was not removed"

        _expect_foreign_key_violation(
            cursor,
            "INSERT INTO content_plans (id, user_id, persona_id) VALUES (%s, %s, %s)",
            (SECOND_PLAN, USER_A, PERSONA_B),
        )

        cursor.execute(
            "INSERT INTO content_plans (id, user_id, persona_id) VALUES (%s, %s, %s)",
            (SECOND_PLAN, USER_A, PERSONA_A),
        )
        _expect_foreign_key_violation(
            cursor,
            "UPDATE content_plans SET persona_id = %s WHERE id = %s",
            (PERSONA_B, SECOND_PLAN),
        )

        cursor.execute("DELETE FROM personas WHERE id = %s", (PERSONA_A,))
        cursor.execute("SELECT count(*) FROM content_plans WHERE user_id = %s", (USER_A,))
        assert cursor.fetchone()[0] == 0, "composite FK lost ON DELETE CASCADE semantics"

        # Leave the shared CI database exactly as the migration step found it;
        # the full pytest suite runs against this same service afterward.
        cursor.execute("DELETE FROM users WHERE id IN (%s, %s)", (USER_A, USER_B))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "action",
        choices=("seed-mismatch", "repair-mismatch", "verify-installed"),
    )
    args = parser.parse_args()

    actions = {
        "seed-mismatch": seed_mismatch,
        "repair-mismatch": repair_mismatch,
        "verify-installed": verify_installed_constraint,
    }
    actions[args.action]()


if __name__ == "__main__":
    main()
