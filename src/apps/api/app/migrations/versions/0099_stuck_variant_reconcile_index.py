"""Index the bounded stuck-variant reconcile sweep.

Revision ID: 0099
Revises: 0098
Create Date: 2026-09-07

`reconcile_stuck_variants` runs on every Celery `worker_ready` and on the Beat
tick. Its discovery SELECT filters on `assembly_plan @? <jsonpath>` and orders by
`(updated_at, id)` with LIMIT 50. Nothing served that: the only sibling sparse
indexes carry `_poster_backfill_cleanup_receipts` / `_speech_cleanup_internal`
predicates that do not imply this one, and there is no GIN on `assembly_plan`.

The query never actually ran in production — the jsonpath was malformed and
Postgres rejected it at parse-analysis, which `worker_ready` swallowed as
non-fatal. Fixing the path makes it live, so it gets an index before it does.

Two-argument `jsonb_path_exists` is IMMUTABLE, so the `@?` predicate is legal in
an index predicate.
"""

from alembic import op

revision = "0099"
down_revision = "0098"
branch_labels = None
depends_on = None

_INDEX = "idx_jobs_stuck_variant_reconcile_sweep"
# Must stay byte-identical to `_STUCK_VARIANT_JSONPATH` in app/tasks/reaper.py,
# or the planner will not match the predicate. Pinned by
# `test_reconcile_sweep_index_predicate_matches_the_reaper_jsonpath`.
_PREDICATE = (
    "assembly_plan @? "
    '\'$.variants[*] ? (@.render_status == "rendering" '
    '|| @.render_status == "pending")\'::jsonpath'
)


def upgrade() -> None:
    # The jobs table is continuously written by render workers. Build the
    # sparse sweep index without holding a write-blocking table lock for the
    # duration of the scan.
    with op.get_context().autocommit_block():
        # A failed prior CONCURRENTLY attempt can leave an invalid same-name
        # index while Alembic still records 0098. Remove that artifact first;
        # CREATE ... IF NOT EXISTS would otherwise accept a broken index.
        op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {_INDEX}")
        op.execute(
            f"CREATE INDEX CONCURRENTLY {_INDEX} ON jobs (updated_at, id) WHERE {_PREDICATE}"
        )


def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {_INDEX}")
