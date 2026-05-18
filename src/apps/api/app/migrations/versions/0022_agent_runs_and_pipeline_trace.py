"""Add agent_run table and Job.pipeline_trace column.

Revision ID: 0022
Revises: 0021
Create Date: 2026-05-16

Adds the persistence layer for the admin job-debug view (see plan
``in-the-admin-tool-vivid-charm``). Two pieces:

  - ``agent_run`` table: one row per agent invocation, capturing the full
    input/output and the raw LLM response so we can answer "did this agent
    return bad data" without re-running the job. job_id is nullable so
    that off-job calls (track-level analysis, eval harness) can also be
    captured; we still index on job_id for the per-job admin query.
  - ``jobs.pipeline_trace`` (JSONB array): append-only log of non-LLM
    pipeline decisions (interstitial picks, beat-snap offsets, transition
    choices). Lives on the Job row because it's small (~200 events max)
    and read-with-the-job for the debug view.

Downgrade drops both. Existing rows are unaffected (column is nullable,
the new table is independent).
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0022"
down_revision = "0021"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "agent_run",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "job_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("jobs.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column("segment_idx", sa.Integer(), nullable=True),
        sa.Column("agent_name", sa.Text(), nullable=False),
        sa.Column("prompt_version", sa.Text(), nullable=False),
        sa.Column("model", sa.Text(), nullable=False),
        sa.Column("input_json", postgresql.JSONB(), nullable=True),
        sa.Column("raw_text", sa.Text(), nullable=True),
        sa.Column("output_json", postgresql.JSONB(), nullable=True),
        sa.Column("outcome", sa.Text(), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("tokens_in", sa.Integer(), nullable=True),
        sa.Column("tokens_out", sa.Integer(), nullable=True),
        sa.Column("cost_usd", sa.Numeric(10, 6), nullable=True),
        sa.Column("latency_ms", sa.Integer(), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), server_default=sa.text("now()")),
    )
    op.create_index("idx_agent_run_job_id_created", "agent_run", ["job_id", "created_at"])
    op.create_index(
        "idx_agent_run_failures",
        "agent_run",
        ["outcome"],
        postgresql_where=sa.text("outcome NOT IN ('ok', 'ok_fallback')"),
    )
    op.create_index("idx_agent_run_agent_name", "agent_run", ["agent_name"])

    op.add_column(
        "jobs",
        sa.Column("pipeline_trace", postgresql.JSONB(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("jobs", "pipeline_trace")
    op.drop_index("idx_agent_run_agent_name", table_name="agent_run")
    op.drop_index("idx_agent_run_failures", table_name="agent_run")
    op.drop_index("idx_agent_run_job_id_created", table_name="agent_run")
    op.drop_table("agent_run")
