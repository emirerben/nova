"""Add auth_provider and onboarding_status to users.

Revision ID: 0035
Revises: 0034
Create Date: 2026-05-29

Adds the two columns needed for Google-only sign-in and wizard resumption:
- auth_provider: 'google' (only supported provider for v1)
- onboarding_status: tracks where the user is in the onboarding wizard so
  they can resume after closing the tab.

The synthetic MVP user gets 'onboarding_status = complete' to keep admin
flows unaffected.
"""

import sqlalchemy as sa
from alembic import op

revision = "0035"
down_revision = "0034"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column(
            "auth_provider",
            sa.Text(),
            nullable=False,
            server_default="google",
        ),
    )
    op.add_column(
        "users",
        sa.Column(
            "onboarding_status",
            sa.Text(),
            nullable=False,
            server_default="pending",
        ),
    )
    # Synthetic MVP user is already "done" — don't show it the onboarding wizard.
    op.execute(
        "UPDATE users SET onboarding_status = 'complete' "
        "WHERE id = '00000000-0000-0000-0000-000000000001'"
    )


def downgrade() -> None:
    op.drop_column("users", "onboarding_status")
    op.drop_column("users", "auth_provider")
