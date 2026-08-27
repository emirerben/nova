"""Distinguish Copilot proposals, staged drafts, and committed saves."""

from alembic import op

revision = "0090"
down_revision = "0089"
branch_labels = None
depends_on = None


def _replace_checks(*, include_staged: bool) -> None:
    op.drop_constraint(
        "ck_edit_interaction_receipts_proposal_outcome",
        "edit_interaction_receipts",
        type_="check",
    )
    op.drop_constraint(
        "ck_edit_interaction_receipts_execution_outcome",
        "edit_interaction_receipts",
        type_="check",
    )
    proposal_values = (
        "'applied', 'proposed', 'clarification', 'no_effect', 'unsupported', 'stale', 'failed'"
        if include_staged
        else "'applied', 'clarification', 'no_effect', 'unsupported', 'stale', 'failed'"
    )
    execution_values = (
        "'applied', 'staged', 'no_effect', 'rejected', 'stale', 'failed'"
        if include_staged
        else "'applied', 'no_effect', 'rejected', 'stale', 'failed'"
    )
    op.create_check_constraint(
        "ck_edit_interaction_receipts_proposal_outcome",
        "edit_interaction_receipts",
        f"proposal_outcome IN ({proposal_values})",
    )
    op.create_check_constraint(
        "ck_edit_interaction_receipts_execution_outcome",
        "edit_interaction_receipts",
        f"execution_outcome IS NULL OR execution_outcome IN ({execution_values})",
    )


def upgrade() -> None:
    _replace_checks(include_staged=True)


def downgrade() -> None:
    # The old constraint cannot represent the more precise lifecycle. Mapping
    # both new values to its historical `applied` spelling is an intentional
    # compatibility downgrade; no committed editor data is changed.
    op.execute(
        "UPDATE edit_interaction_receipts "
        "SET proposal_outcome = 'applied' WHERE proposal_outcome = 'proposed'"
    )
    op.execute(
        "UPDATE edit_interaction_receipts "
        "SET execution_outcome = 'applied' WHERE execution_outcome = 'staged'"
    )
    _replace_checks(include_staged=False)
