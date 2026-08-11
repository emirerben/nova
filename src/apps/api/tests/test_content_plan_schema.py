"""Structural guards for the content-plan Phase 2 data model.

No DB required (parallel-safe under pytest-xdist). These lock the two things
that silently break a deploy:

  1. A single, linear alembic head. The prod release command is
     `alembic upgrade head` — a branched head or a renumbered chain fails the
     Fly release step AFTER merge, not in review. We assert the migration tail
     is intact and the latest numbered revision is the sole head.
  2. The new ORM models exist with the expected columns and the circular FK
     pair (PlanItem.current_job_id ⇄ Job.content_plan_item_id) resolves. The
     migration ordering (plan_items FK in 0038, jobs FK in 0039) exists
     specifically to make this circular pair deployable.

End-to-end up/down was verified manually against Postgres 16 (plan task T6).
"""

import importlib

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import ForeignKeyConstraint, UniqueConstraint
from sqlalchemy.orm import configure_mappers

from app import models

# Expected linear tail of the migration chain (child -> parent down_revision).
_EXPECTED_CHAIN = {
    "0036": "0035",
    "0037": "0036",
    "0038": "0037",
    "0039": "0038",
    "0040": "0039",
    "0041": "0040",
    "0042": "0041",
    "0043": "0042",
    "0044": "0043",
    "0045": "0044",
    "0046": "0045",
    "0047": "0046",
    "0048": "0047",
    "0049": "0048",
    "0050": "0049",
    "0051": "0050",
    "0052": "0051",
    "0053": "0052",
    "0054": "0053",
    "0055": "0054",
    "0056": "0055",
    "0057": "0056",
    "0058": "0057",
    "0059": "0058",
    "0060": "0059",
    "0061": "0060",
    "0062": "0061",
    "0063": "0062",
    "0064": "0063",
    "0065": "0064",
    "0066": "0065",
    "0067": "0066",
    "0068": "0067",
    "0069": "0068",
    "0070": "0069",
    "0071": "0070",
    "0072": "0071",
}


@pytest.fixture(scope="module")
def script_dir() -> ScriptDirectory:
    # alembic.ini lives at the api root; tests run with that as the cwd in CI.
    return ScriptDirectory.from_config(Config("alembic.ini"))


def test_single_alembic_head(script_dir: ScriptDirectory) -> None:
    heads = script_dir.get_heads()
    assert heads == ["0072"], f"expected a single head 0072, got {heads}"


def test_migration_chain_is_linear(script_dir: ScriptDirectory) -> None:
    for rev, expected_down in _EXPECTED_CHAIN.items():
        script = script_dir.get_revision(rev)
        assert script is not None, f"migration {rev} is missing"
        assert script.down_revision == expected_down, (
            f"{rev} down_revision is {script.down_revision!r}, expected {expected_down!r} "
            "— the circular-FK ordering depends on this exact chain"
        )


def test_new_tables_registered() -> None:
    tables = models.Base.metadata.tables
    assert "personas" in tables
    assert "content_plans" in tables
    assert "plan_items" in tables
    assert "creator_style_assignments" in tables

    persona_cols = set(tables["personas"].columns.keys())
    assert {
        "user_id",
        "questionnaire",
        "persona",
        "persona_status",
        "prompt_version",
        "tiktok_profile",
        "generation_started_at",
        "style",
        "idea_seeds",
    } <= persona_cols

    plan_cols = set(tables["content_plans"].columns.keys())
    assert {
        "user_id",
        "persona_id",
        "horizon_days",
        "start_date",
        "plan_status",
        "generation_started_at",
        "activation_started_at",
        "activation_phase",
        "ownership_epoch",
        "ownership_quarantined_at",
    } <= plan_cols

    item_cols = set(tables["plan_items"].columns.keys())
    assert "smart_captions_enabled" in item_cols
    assert "smart_sound_design_enabled" in item_cols
    assert {
        "content_plan_id",
        "day_index",
        "theme",
        "idea",
        "clip_gcs_paths",
        "clip_assignments",
        "item_status",
        "current_job_id",
        "edit_format",
        "montage_preset",
        "filming_guide",
        "source_idea_seed_id",
        "position",
        "scheduled_date",
        "notes",
        "scenes",
        "content_mode",
    } <= item_cols
    item_constraints = {constraint.name for constraint in tables["plan_items"].constraints}
    assert "ck_plan_items_smart_captions_format" in item_constraints

    assignment = tables["creator_style_assignments"]
    assert {"shadow_preset_id", "shadow_preset_version"} <= set(assignment.columns.keys())
    assignment_constraints = {constraint.name for constraint in assignment.constraints}
    assert "ck_creator_style_shadow_pair" in assignment_constraints


def test_quality_core_defers_unused_revision_and_outbox_tables() -> None:
    tables = models.Base.metadata.tables
    assert "smart_edit_plans" not in tables
    assert "smart_edit_plan_revisions" not in tables
    assert "smart_edit_dispatches" not in tables


def test_0065_places_constraints_on_their_actual_tables(monkeypatch) -> None:
    """Regression guard for DDL that compiles but references another table's columns."""

    migration = importlib.import_module("app.migrations.versions.0065_smart_captions_foundation")
    created: dict[str, tuple] = {}

    monkeypatch.setattr(migration.op, "add_column", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(migration.op, "create_check_constraint", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(migration.op, "create_index", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        migration.op,
        "create_table",
        lambda name, *elements, **_kwargs: created.__setitem__(name, elements),
    )

    migration.upgrade()

    creator_constraints = {
        element.name
        for element in created["creator_style_assignments"]
        if getattr(element, "name", None)
    }
    assert not any(name.startswith("ck_smart_edit_plans_") for name in creator_constraints)

    assert set(created) == {"creator_style_assignments"}


def test_0066_upgrade_downgrade_roundtrip_is_exact(monkeypatch) -> None:
    """Execute both migration directions against an in-memory schema ledger."""

    migration = importlib.import_module("app.migrations.versions.0066_smart_captions_shadow_preset")
    table = "creator_style_assignments"
    columns: set[str] = set()
    constraints: dict[str, tuple[str, str]] = {}

    def add_column(target: str, column) -> None:
        assert target == table
        assert column.name not in columns
        columns.add(column.name)

    def create_check_constraint(name: str, target: str, expression: str) -> None:
        assert target == table
        assert {"shadow_preset_id", "shadow_preset_version"} <= columns
        constraints[name] = (target, expression)

    def drop_constraint(name: str, target: str, *, type_: str) -> None:
        assert target == table
        assert type_ == "check"
        constraints.pop(name)

    def drop_column(target: str, name: str) -> None:
        assert target == table
        assert not constraints
        columns.remove(name)

    monkeypatch.setattr(migration.op, "add_column", add_column)
    monkeypatch.setattr(migration.op, "create_check_constraint", create_check_constraint)
    monkeypatch.setattr(migration.op, "drop_constraint", drop_constraint)
    monkeypatch.setattr(migration.op, "drop_column", drop_column)

    migration.upgrade()
    assert columns == {"shadow_preset_id", "shadow_preset_version"}
    assert constraints == {
        "ck_creator_style_shadow_pair": (
            table,
            "(shadow_preset_id IS NULL) = (shadow_preset_version IS NULL)",
        )
    }

    migration.downgrade()
    assert columns == set()
    assert constraints == {}


def test_plan_item_assets_registered() -> None:
    """Auto-placement PR0 (plans/005): the asset-pool table + expected columns."""
    tables = models.Base.metadata.tables
    assert "plan_item_assets" in tables
    asset_cols = set(tables["plan_item_assets"].columns.keys())
    assert {
        "plan_item_id",
        "user_id",
        "gcs_path",
        "kind",
        "content_hash",
        "source_filename",
        "duration_s",
        "aspect",
        "analysis",
        "status",
        "created_at",
    } <= asset_cols


def test_jobs_has_content_plan_item_fk() -> None:
    job_columns = models.Base.metadata.tables["jobs"].columns
    assert "content_plan_item_id" in job_columns
    assert "content_plan_ownership_epoch" in job_columns
    assert job_columns["content_plan_ownership_epoch"].nullable is True


def test_circular_fk_relationships_resolve() -> None:
    # Configuring mappers raises if either side of the circular pair is ambiguous.
    configure_mappers()
    assert models.PlanItem.current_job.property.target.name == "jobs"
    assert models.Job.content_plan_item.property.target.name == "plan_items"
    # 1:1 persona on user.
    assert models.User.persona.property.uselist is False


def test_personas_user_id_is_unique() -> None:
    # 1:1 with users is enforced at the column level (unique=True).
    assert models.Base.metadata.tables["personas"].columns["user_id"].unique is True


def test_content_plan_persona_owner_constraints_registered() -> None:
    """ORM DDL must encode the same compound tenant invariant as Alembic."""

    personas = models.Base.metadata.tables["personas"]
    persona_owner_key = next(
        constraint
        for constraint in personas.constraints
        if isinstance(constraint, UniqueConstraint)
        and constraint.name == "uq_personas_id_user_id"
    )
    assert tuple(persona_owner_key.columns.keys()) == ("id", "user_id")

    content_plans = models.Base.metadata.tables["content_plans"]
    persona_owner_fk = next(
        constraint
        for constraint in content_plans.constraints
        if isinstance(constraint, ForeignKeyConstraint)
        and constraint.name == "fk_content_plans_persona_owner"
    )
    assert tuple(persona_owner_fk.columns.keys()) == ("persona_id", "user_id")
    assert tuple(element.target_fullname for element in persona_owner_fk.elements) == (
        "personas.id",
        "personas.user_id",
    )
    assert persona_owner_fk.ondelete == "CASCADE"
    assert persona_owner_fk.match == "FULL"
    assert content_plans.columns["persona_id"].nullable is False
    assert content_plans.columns["user_id"].nullable is False

    # The compound FK supersedes the old persona_id-only trust boundary.  A
    # second FK here would let ORM metadata drift from the migration ordering.
    assert not any(
        tuple(constraint.columns.keys()) == ("persona_id",)
        for constraint in content_plans.foreign_key_constraints
    )


def test_persona_content_plan_navigation_is_viewonly_and_owner_joined() -> None:
    """Navigation remains compatible without synchronizing the shared user_id."""

    configure_mappers()
    plan_to_persona = models.ContentPlan.persona.property
    persona_to_plans = models.Persona.content_plans.property
    assert plan_to_persona.viewonly is True
    assert persona_to_plans.viewonly is True

    def _column_pair_names(relationship) -> set[frozenset[tuple[str, str]]]:
        return {
            frozenset(
                {
                    (local.table.name, local.name),
                    (remote.table.name, remote.name),
                }
            )
            for local, remote in relationship.local_remote_pairs
        }

    expected_pairs = {
        frozenset({("content_plans", "persona_id"), ("personas", "id")}),
        frozenset({("content_plans", "user_id"), ("personas", "user_id")}),
    }
    assert _column_pair_names(plan_to_persona) == expected_pairs
    assert _column_pair_names(persona_to_plans) == expected_pairs


def test_0071_adds_durable_ownership_fence_columns(monkeypatch) -> None:
    migration = importlib.import_module(
        "app.migrations.versions.0071_content_plan_ownership_fence"
    )
    added: list[tuple[str, object]] = []

    monkeypatch.setattr(
        migration.op,
        "add_column",
        lambda table, column: added.append((table, column)),
    )

    migration.upgrade()

    assert [table for table, _column in added] == [
        "jobs",
        "content_plans",
        "content_plans",
    ]
    columns = {(table, column.name): column for table, column in added}
    assert set(columns) == {
        ("jobs", "content_plan_ownership_epoch"),
        ("content_plans", "ownership_epoch"),
        ("content_plans", "ownership_quarantined_at"),
    }
    assert columns[("jobs", "content_plan_ownership_epoch")].nullable is True
    assert columns[("content_plans", "ownership_epoch")].nullable is False
    assert str(columns[("content_plans", "ownership_epoch")].server_default.arg) == "0"
    assert columns[("content_plans", "ownership_quarantined_at")].nullable is True


@pytest.mark.parametrize("used_fences", [1, 2])
def test_0071_refuses_to_erase_used_ownership_fences(monkeypatch, used_fences: int) -> None:
    migration = importlib.import_module(
        "app.migrations.versions.0071_content_plan_ownership_fence"
    )
    dropped: list[tuple[str, str]] = []

    class _Result:
        def scalar_one(self) -> int:
            return used_fences

    class _Bind:
        def execute(self, _stmt) -> _Result:
            return _Result()

    monkeypatch.setattr(migration.op, "get_bind", lambda: _Bind())
    monkeypatch.setattr(
        migration.op,
        "drop_column",
        lambda table, column: dropped.append((table, column)),
    )

    with pytest.raises(RuntimeError, match="ownership fence has been used"):
        migration.downgrade()
    assert dropped == []


def test_0071_downgrade_removes_an_unused_fence(monkeypatch) -> None:
    migration = importlib.import_module(
        "app.migrations.versions.0071_content_plan_ownership_fence"
    )
    dropped: list[tuple[str, str]] = []

    class _Result:
        def scalar_one(self) -> int:
            return 0

    class _Bind:
        def execute(self, _stmt) -> _Result:
            return _Result()

    monkeypatch.setattr(migration.op, "get_bind", lambda: _Bind())
    monkeypatch.setattr(
        migration.op,
        "drop_column",
        lambda table, column: dropped.append((table, column)),
    )

    migration.downgrade()
    assert dropped == [
        ("content_plans", "ownership_quarantined_at"),
        ("content_plans", "ownership_epoch"),
        ("jobs", "content_plan_ownership_epoch"),
    ]


_0072_CATALOG_ROWS = [
    {
        "table_schema": "public",
        "table_name": table,
        "column_name": column,
        "is_nullable": "NO",
        "is_not_null": True,
        "not_null": True,
        "attnotnull": True,
        "data_type": "uuid",
        "formatted_type": "uuid",
        "type_name": "uuid",
        "udt_name": "uuid",
        "type_oid": 2950,
        "type_modifier": -1,
        "atttypid": 2950,
    }
    for table, column in (
        ("personas", "id"),
        ("personas", "user_id"),
        ("content_plans", "persona_id"),
        ("content_plans", "user_id"),
    )
]


class _0072Result:
    def __init__(self, rows: list[object]):
        self._rows = rows

    def mappings(self):
        return self

    def scalars(self):
        return _0072Result(
            [
                row["constraint_name"]
                if isinstance(row, dict) and "constraint_name" in row
                else row
                for row in self._rows
            ]
        )

    def all(self) -> list[object]:
        return self._rows

    def first(self):
        return self._rows[0] if self._rows else None


class _0072Bind:
    def __init__(
        self,
        events: list[tuple],
        *,
        catalog_rows: list[dict] | None = None,
        mismatch: dict | None = None,
        legacy_rows: list[dict] | None = None,
    ) -> None:
        self.events = events
        self.catalog_rows = (
            [dict(row) for row in _0072_CATALOG_ROWS]
            if catalog_rows is None
            else catalog_rows
        )
        self.mismatch = mismatch
        self.legacy_rows = (
            [
                {
                    "constraint_name": "content_plans_persona_id_fkey",
                    "is_validated": True,
                    "delete_action": "c",
                }
            ]
            if legacy_rows is None
            else legacy_rows
        )

    def execute(self, statement) -> _0072Result:
        sql = str(statement)
        self.events.append(("sql", sql))
        normalized = " ".join(sql.lower().split())
        if "left join personas" in normalized:
            return _0072Result([self.mismatch] if self.mismatch is not None else [])
        if "pg_constraint" in normalized:
            return _0072Result(list(self.legacy_rows))
        if "pg_attribute" in normalized or "information_schema.columns" in normalized:
            return _0072Result(list(self.catalog_rows))
        return _0072Result([])


def _patch_0072_ops(monkeypatch, migration, bind: _0072Bind) -> list[tuple]:
    events = bind.events
    monkeypatch.setattr(migration.op, "get_bind", lambda: bind)
    monkeypatch.setattr(
        migration.op,
        "execute",
        lambda statement: events.append(("sql", str(statement))),
    )
    monkeypatch.setattr(
        migration.op,
        "create_unique_constraint",
        lambda name, table, columns, **kwargs: events.append(
            ("create_unique", name, table, tuple(columns), kwargs)
        ),
    )
    monkeypatch.setattr(
        migration.op,
        "create_foreign_key",
        lambda name, source, target, local, remote, **kwargs: events.append(
            ("create_fk", name, source, target, tuple(local), tuple(remote), kwargs)
        ),
    )
    monkeypatch.setattr(
        migration.op,
        "drop_constraint",
        lambda name, table, **kwargs: events.append(("drop", name, table, kwargs)),
    )
    return events


def test_0072_upgrade_enforces_owner_invariant_before_dropping_legacy_fk(
    monkeypatch,
) -> None:
    """The composite boundary must exist before the permissive legacy FK leaves."""

    migration = importlib.import_module(
        "app.migrations.versions.0072_content_plan_persona_owner_invariant"
    )
    events: list[tuple] = []
    bind = _0072Bind(events)
    _patch_0072_ops(monkeypatch, migration, bind)

    migration.upgrade()

    unique_event = next(event for event in events if event[0] == "create_unique")
    assert unique_event[1:4] == (
        "uq_personas_id_user_id",
        "personas",
        ("id", "user_id"),
    )
    fk_event = next(event for event in events if event[0] == "create_fk")
    assert fk_event[1:6] == (
        "fk_content_plans_persona_owner",
        "content_plans",
        "personas",
        ("persona_id", "user_id"),
        ("id", "user_id"),
    )
    assert fk_event[6]["ondelete"] == "CASCADE"
    assert fk_event[6]["match"] == "FULL"
    assert fk_event[6]["postgresql_not_valid"] is True
    legacy_drop = next(
        event
        for event in events
        if event[:3]
        == ("drop", "content_plans_persona_id_fkey", "content_plans")
    )
    assert legacy_drop[3]["type_"] == "foreignkey"

    normalized_sql = [
        " ".join(event[1].lower().split()) for event in events if event[0] == "sql"
    ]
    assert any("set local lock_timeout" in sql and "5s" in sql for sql in normalized_sql)
    assert any(
        "set local statement_timeout" in sql and "30s" in sql for sql in normalized_sql
    )
    mismatch_index = next(
        index
        for index, event in enumerate(events)
        if event[0] == "sql" and "left join personas" in event[1].lower()
    )
    unique_index = events.index(unique_event)
    fk_index = events.index(fk_event)
    validate_index = next(
        index
        for index, event in enumerate(events)
        if event[0] == "sql"
        and "validate constraint fk_content_plans_persona_owner" in event[1].lower()
    )
    drop_index = events.index(legacy_drop)
    assert mismatch_index < unique_index < fk_index < validate_index < drop_index


def test_0072_upgrade_aborts_on_existing_cross_owner_link(monkeypatch) -> None:
    migration = importlib.import_module(
        "app.migrations.versions.0072_content_plan_persona_owner_invariant"
    )
    events: list[tuple] = []
    bind = _0072Bind(
        events,
        mismatch={
            "plan_id": "00000000-0000-0000-0000-000000000001",
            "plan_user_id": "00000000-0000-0000-0000-000000000002",
            "persona_id": "00000000-0000-0000-0000-000000000003",
            "persona_user_id": "00000000-0000-0000-0000-000000000004",
        },
    )
    _patch_0072_ops(monkeypatch, migration, bind)

    with pytest.raises(RuntimeError, match="mismatch|owner"):
        migration.upgrade()

    assert not any(event[0] in {"create_unique", "create_fk", "drop"} for event in events)


def test_0072_upgrade_aborts_when_required_uuid_column_is_nullable(monkeypatch) -> None:
    migration = importlib.import_module(
        "app.migrations.versions.0072_content_plan_persona_owner_invariant"
    )
    events: list[tuple] = []
    drifted_rows = [dict(row) for row in _0072_CATALOG_ROWS]
    drifted_rows[-1]["is_nullable"] = "YES"
    drifted_rows[-1]["is_not_null"] = False
    drifted_rows[-1]["not_null"] = False
    drifted_rows[-1]["attnotnull"] = False
    bind = _0072Bind(events, catalog_rows=drifted_rows)
    _patch_0072_ops(monkeypatch, migration, bind)

    with pytest.raises(RuntimeError, match="NOT NULL|precondition|schema"):
        migration.upgrade()

    assert not any(event[0] in {"create_unique", "create_fk", "drop"} for event in events)


def test_0072_upgrade_aborts_when_owner_column_type_drifted(monkeypatch) -> None:
    migration = importlib.import_module(
        "app.migrations.versions.0072_content_plan_persona_owner_invariant"
    )
    events: list[tuple] = []
    drifted_rows = [dict(row) for row in _0072_CATALOG_ROWS]
    drifted_rows[-1]["type_name"] = "text"
    bind = _0072Bind(events, catalog_rows=drifted_rows)
    _patch_0072_ops(monkeypatch, migration, bind)

    with pytest.raises(RuntimeError, match="UUID|type"):
        migration.upgrade()

    assert not any(event[0] in {"create_unique", "create_fk", "drop"} for event in events)


@pytest.mark.parametrize(
    "legacy_rows",
    [
        [],
        [
            {
                "constraint_name": "wrong_name",
                "is_validated": True,
                "delete_action": "c",
            }
        ],
        [
            {
                "constraint_name": "content_plans_persona_id_fkey",
                "is_validated": False,
                "delete_action": "c",
            }
        ],
        [
            {
                "constraint_name": "content_plans_persona_id_fkey",
                "is_validated": True,
                "delete_action": "a",
            }
        ],
        [
            {
                "constraint_name": "content_plans_persona_id_fkey",
                "is_validated": True,
                "delete_action": "c",
            },
            {
                "constraint_name": "unexpected_second_fk",
                "is_validated": True,
                "delete_action": "c",
            },
        ],
    ],
)
def test_0072_upgrade_rejects_legacy_fk_catalog_drift(
    monkeypatch, legacy_rows: list[dict]
) -> None:
    migration = importlib.import_module(
        "app.migrations.versions.0072_content_plan_persona_owner_invariant"
    )
    events: list[tuple] = []
    bind = _0072Bind(events, legacy_rows=legacy_rows)
    _patch_0072_ops(monkeypatch, migration, bind)

    with pytest.raises(RuntimeError):
        migration.upgrade()

    assert not any(event[0] == "drop" for event in events)


def test_0072_downgrade_validates_legacy_fk_before_removing_owner_invariant(
    monkeypatch,
) -> None:
    migration = importlib.import_module(
        "app.migrations.versions.0072_content_plan_persona_owner_invariant"
    )
    events: list[tuple] = []
    bind = _0072Bind(events)
    _patch_0072_ops(monkeypatch, migration, bind)

    migration.downgrade()

    add_index = next(
        index
        for index, event in enumerate(events)
        if event[0] == "sql"
        and "add constraint content_plans_persona_id_fkey" in event[1].lower()
    )
    validate_index = next(
        index
        for index, event in enumerate(events)
        if event[0] == "sql"
        and "validate constraint content_plans_persona_id_fkey" in event[1].lower()
    )
    composite_drop = next(
        event
        for event in events
        if event[:3]
        == ("drop", "fk_content_plans_persona_owner", "content_plans")
    )
    unique_drop = next(
        event for event in events if event[:3] == ("drop", "uq_personas_id_user_id", "personas")
    )
    composite_drop_index = events.index(composite_drop)
    unique_drop_index = events.index(unique_drop)
    assert add_index < validate_index < composite_drop_index < unique_drop_index
    assert "not valid" in events[add_index][1].lower()
    assert "on delete cascade" in events[add_index][1].lower()
    assert composite_drop[3]["type_"] == "foreignkey"
    assert unique_drop[3]["type_"] == "unique"


def test_0072_downgrade_checks_schema_before_recreating_legacy_fk(monkeypatch) -> None:
    migration = importlib.import_module(
        "app.migrations.versions.0072_content_plan_persona_owner_invariant"
    )
    events: list[tuple] = []
    bind = _0072Bind(events, catalog_rows=_0072_CATALOG_ROWS[:-1])
    _patch_0072_ops(monkeypatch, migration, bind)

    with pytest.raises(RuntimeError, match="precondition|schema|column|catalog"):
        migration.downgrade()

    assert not any(
        event[0] == "sql" and "add constraint" in event[1].lower() for event in events
    )
    assert not any(event[0] == "drop" for event in events)


def test_0067_upgrades_only_cigdem_v1_rows(monkeypatch) -> None:
    """0067 lifts exactly cigdem/v1 rows to v2 (and the inverse on downgrade)."""

    migration = importlib.import_module("app.migrations.versions.0067_upgrade_v1_style_assignments")
    executed: list[str] = []

    class _Result:
        rowcount = 3

    class _Bind:
        def execute(self, stmt):
            executed.append(str(stmt))
            return _Result()

    monkeypatch.setattr(migration.op, "get_bind", lambda: _Bind())

    migration.upgrade()
    assert len(executed) == 1
    assert "SET preset_version = 'v2'" in executed[0]
    assert "WHERE preset_id = 'cigdem' AND preset_version IN ('v1', 'cigdem-v1')" in executed[0]

    executed.clear()
    migration.downgrade()
    assert len(executed) == 1
    assert "SET preset_version = 'v1'" in executed[0]
    assert "WHERE preset_id = 'cigdem' AND preset_version = 'v2'" in executed[0]
