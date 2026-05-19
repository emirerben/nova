"""Allow 'admin_font_override' in ck_recipe_version_trigger.

Revision ID: 0025
Revises: 0024
Create Date: 2026-05-17

PR #189 added the admin font-override picker, which writes
``TemplateRecipeVersion(trigger="admin_font_override")``, but did not update
the CHECK constraint defined in migration 0010 (later widened to include
``remerge``). Every POST to ``/admin/templates/{id}/font-default`` therefore
fails with ``ck_recipe_version_trigger`` violation → HTTP 500.
"""

from alembic import op

revision = "0025"
down_revision = "0024"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_constraint("ck_recipe_version_trigger", "template_recipe_versions")
    op.create_check_constraint(
        "ck_recipe_version_trigger",
        "template_recipe_versions",
        "trigger IN ("
        "'initial_analysis', 'reanalysis', 'manual_edit', "
        "'remerge', 'admin_font_override'"
        ")",
    )


def downgrade() -> None:
    op.drop_constraint("ck_recipe_version_trigger", "template_recipe_versions")
    op.create_check_constraint(
        "ck_recipe_version_trigger",
        "template_recipe_versions",
        "trigger IN ('initial_analysis', 'reanalysis', 'manual_edit', 'remerge')",
    )
