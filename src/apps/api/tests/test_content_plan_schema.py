"""Structural guards for the content-plan Phase 2 data model.

No DB required (parallel-safe under pytest-xdist). These lock the two things
that silently break a deploy:

  1. A single, linear alembic head. The prod release command is
     `alembic upgrade head` — a branched head or a renumbered chain fails the
     Fly release step AFTER merge, not in review. We assert the 0035→0039 chain
     is intact and 0039 is the sole head.
  2. The new ORM models exist with the expected columns and the circular FK
     pair (PlanItem.current_job_id ⇄ Job.content_plan_item_id) resolves. The
     migration ordering (plan_items FK in 0038, jobs FK in 0039) exists
     specifically to make this circular pair deployable.

End-to-end up/down was verified manually against Postgres 16 (plan task T6).
"""

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
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
}


@pytest.fixture(scope="module")
def script_dir() -> ScriptDirectory:
    # alembic.ini lives at the api root; tests run with that as the cwd in CI.
    return ScriptDirectory.from_config(Config("alembic.ini"))


def test_single_alembic_head(script_dir: ScriptDirectory) -> None:
    heads = script_dir.get_heads()
    assert heads == ["0059"], f"expected a single head 0059, got {heads}"


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
    } <= plan_cols

    item_cols = set(tables["plan_items"].columns.keys())
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
        "filming_guide",
        "source_idea_seed_id",
        "position",
        "scheduled_date",
        "notes",
        "scenes",
        "content_mode",
    } <= item_cols


def test_jobs_has_content_plan_item_fk() -> None:
    assert "content_plan_item_id" in models.Base.metadata.tables["jobs"].columns


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
