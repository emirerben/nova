"""Paid intent review lifecycle on the generation review outbox (KRI-1171)."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql as pg

revision = "0131"
down_revision = "0130"
branch_labels = None
depends_on = None

TABLE = "creator_generation_reviews"
EVIDENCE_STATUSES = ("pending", "running", "evidence_ready", "unavailable", "stale", "exhausted")
REVIEW_STATUSES = (
    "reviewing",
    "review_unknown",
    "no_findings",
    "suggestion",
    "needs_confirmation",
    "repair_staged",
    "repair_verified",
    "repair_unverified",
    "repair_reverted",
    "repair_abandoned",
)
TERMINAL_STATUSES = (
    "unavailable",
    "stale",
    "exhausted",
    "review_unknown",
    "no_findings",
    "suggestion",
    "needs_confirmation",
    "repair_verified",
    "repair_unverified",
    "repair_reverted",
    "repair_abandoned",
)


def _in(values):
    return ",".join(f"'{value}'" for value in values)


def upgrade():
    op.execute(
        f"UPDATE {TABLE} SET reason = 'legacy_unspecified' "
        f"WHERE status IN ({_in(TERMINAL_STATUSES)}) AND reason IS NULL"
    )
    op.drop_constraint("ck_generation_review_status", TABLE, type_="check")
    op.create_check_constraint(
        "ck_generation_review_status",
        TABLE,
        f"status IN ({_in(EVIDENCE_STATUSES + REVIEW_STATUSES)})",
    )
    op.add_column(TABLE, sa.Column("tier", sa.Text(), nullable=False, server_default="evidence"))
    op.add_column(TABLE, sa.Column("max_usd", sa.Numeric(6, 4), nullable=True))
    op.add_column(TABLE, sa.Column("evidence_ready_at", sa.DateTime(timezone=True)))
    op.add_column(
        TABLE,
        sa.Column("review_attempts", sa.SmallInteger(), nullable=False, server_default="0"),
    )
    op.add_column(TABLE, sa.Column("paid_call_key", sa.Text(), nullable=True))
    op.add_column(TABLE, sa.Column("review", pg.JSONB(), nullable=True))
    op.add_column(TABLE, sa.Column("parent_review_id", pg.UUID(as_uuid=True), nullable=True))
    op.add_column(TABLE, sa.Column("repair_draft_id", pg.UUID(as_uuid=True), nullable=True))
    op.add_column(
        TABLE, sa.Column("repair_render_execution_id", pg.UUID(as_uuid=True), nullable=True)
    )
    op.add_column(TABLE, sa.Column("repair_staged_at", sa.DateTime(timezone=True)))
    op.add_column(TABLE, sa.Column("receipt_event_id", pg.UUID(as_uuid=True), nullable=True))
    op.add_column(TABLE, sa.Column("question_event_id", pg.UUID(as_uuid=True), nullable=True))
    op.create_unique_constraint("uq_generation_review_paid_call_key", TABLE, ["paid_call_key"])
    op.create_unique_constraint("uq_generation_review_parent", TABLE, ["parent_review_id"])
    op.create_unique_constraint(
        "uq_generation_review_repair_execution", TABLE, ["repair_render_execution_id"]
    )
    op.create_foreign_key(
        "fk_generation_review_parent",
        TABLE,
        TABLE,
        ["parent_review_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_foreign_key(
        "fk_generation_review_repair_draft",
        TABLE,
        "creator_edit_drafts",
        ["repair_draft_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.create_foreign_key(
        "fk_generation_review_repair_execution",
        TABLE,
        "creator_agent_executions",
        ["repair_render_execution_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_check_constraint(
        "ck_generation_review_tier", TABLE, "tier IN ('evidence','review','repair')"
    )
    op.create_check_constraint(
        "ck_generation_review_max_usd",
        TABLE,
        "max_usd IS NULL OR (max_usd > 0 AND max_usd <= 0.25)",
    )
    op.create_check_constraint(
        "ck_generation_review_review_attempts",
        TABLE,
        "review_attempts >= 0 AND review_attempts <= 2",
    )
    op.create_check_constraint(
        "ck_generation_review_review_size",
        TABLE,
        "review IS NULL OR octet_length(review::text) <= 131072",
    )
    op.create_check_constraint(
        "ck_generation_review_terminal_reason",
        TABLE,
        f"status NOT IN ({_in(TERMINAL_STATUSES)}) OR reason IS NOT NULL",
    )
    op.create_check_constraint(
        "ck_generation_review_reviewing_lease",
        TABLE,
        "status <> 'reviewing' OR "
        "(lease_token IS NOT NULL AND lease_until IS NOT NULL AND paid_call_key IS NOT NULL)",
    )
    op.create_check_constraint(
        "ck_generation_review_repair_draft",
        TABLE,
        "status !~ '^repair_' OR repair_draft_id IS NOT NULL",
    )
    op.create_index(
        "ix_generation_review_repair_staged",
        TABLE,
        ["repair_staged_at"],
        postgresql_where=sa.text("status = 'repair_staged'"),
    )


def downgrade():
    op.drop_index("ix_generation_review_repair_staged", TABLE)
    for name in (
        "ck_generation_review_repair_draft",
        "ck_generation_review_reviewing_lease",
        "ck_generation_review_terminal_reason",
        "ck_generation_review_review_size",
        "ck_generation_review_review_attempts",
        "ck_generation_review_max_usd",
        "ck_generation_review_tier",
    ):
        op.drop_constraint(name, TABLE, type_="check")
    for name in (
        "fk_generation_review_repair_execution",
        "fk_generation_review_repair_draft",
        "fk_generation_review_parent",
    ):
        op.drop_constraint(name, TABLE, type_="foreignkey")
    for name in (
        "uq_generation_review_repair_execution",
        "uq_generation_review_parent",
        "uq_generation_review_paid_call_key",
    ):
        op.drop_constraint(name, TABLE, type_="unique")
    for column in (
        "question_event_id",
        "receipt_event_id",
        "repair_staged_at",
        "repair_render_execution_id",
        "repair_draft_id",
        "parent_review_id",
        "review",
        "paid_call_key",
        "review_attempts",
        "evidence_ready_at",
        "max_usd",
        "tier",
    ):
        op.drop_column(TABLE, column)
    op.execute(
        f"UPDATE {TABLE} SET status = 'unavailable', reason = COALESCE(reason, 'downgraded') "
        f"WHERE status IN ({_in(REVIEW_STATUSES)})"
    )
    op.drop_constraint("ck_generation_review_status", TABLE, type_="check")
    op.create_check_constraint(
        "ck_generation_review_status", TABLE, f"status IN ({_in(EVIDENCE_STATUSES)})"
    )
