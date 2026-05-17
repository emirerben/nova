"""Tests for POST /admin/templates/{id}/font-default.

PR #189 introduced trigger='admin_font_override' on TemplateRecipeVersion
without expanding the ck_recipe_version_trigger CHECK constraint defined
in migration 0010. Migration 0025 closes the gap. These tests guard:

1. The route persists a version row with the expected trigger value.
2. Cascade rules (empty/old-default → new; explicit override → kept) hold.
3. The trigger string the route emits is in the SET allowed by the latest
   migration. This is the static link that prevents a repeat of #189 — if
   anyone adds a new trigger value without a matching migration the test
   fails before deploy.
"""

import re
import uuid
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from app.database import get_db
from app.main import app

VALID_TOKEN = "test-admin-token"

API_ROOT = Path(__file__).resolve().parent.parent.parent / "app"
MIGRATIONS_DIR = API_ROOT / "migrations" / "versions"
ADMIN_ROUTE_FILE = API_ROOT / "routes" / "admin.py"


def _recipe_with_overlays(font_default: str = "Inter Tight") -> dict:
    """Minimal recipe with three overlays: empty / inherits-default / pinned.

    cascade_font_default_change should touch the first two and leave the third.
    """
    return {
        "font_default": font_default,
        "slots": [
            {
                "position": 1,
                "text_overlays": [
                    {"text": "A", "font_family": ""},
                    {"text": "B", "font_family": font_default},
                    {"text": "C", "font_family": "Playfair Display"},
                ],
            }
        ],
    }


def _mock_template(recipe_cached=None):
    t = MagicMock()
    t.id = "tmpl-123"
    t.name = "Test"
    t.analysis_status = "ready"
    t.recipe_cached = recipe_cached
    t.recipe_cached_at = datetime.now(UTC) if recipe_cached else None
    return t


@pytest.fixture()
def client():
    return TestClient(app, raise_server_exceptions=False)


class TestSetFontDefault:
    def _run(self, client, recipe, body):
        template = _mock_template(recipe_cached=recipe)

        mock_db = AsyncMock()
        template_result = MagicMock()
        template_result.scalar_one_or_none.return_value = template

        count_result = MagicMock()
        count_result.scalar.return_value = 7

        mock_db.execute.side_effect = [template_result, count_result]
        mock_db.refresh = AsyncMock()

        added = []
        mock_db.add = lambda obj: added.append(obj)

        def _override_db():
            yield mock_db

        with patch("app.routes.admin.settings") as mock_settings:
            mock_settings.admin_api_key = VALID_TOKEN
            app.dependency_overrides[get_db] = _override_db
            try:
                res = client.post(
                    "/admin/templates/tmpl-123/font-default",
                    headers={"X-Admin-Token": VALID_TOKEN},
                    json=body,
                )
            finally:
                app.dependency_overrides.pop(get_db, None)
        return res, added, template, mock_db

    def test_persists_version_with_admin_font_override_trigger(self, client):
        """Regression for the PR #189 / ck_recipe_version_trigger 500.

        The trigger string emitted here MUST match a value permitted by the
        latest migration's CHECK constraint, otherwise commit raises an
        IntegrityConstraintViolationError → 500.
        """
        recipe = _recipe_with_overlays(font_default="Inter Tight")
        res, added, _, mock_db = self._run(
            client, recipe, {"font_default": "DM Sans"}
        )

        assert res.status_code == 200, res.text
        assert mock_db.commit.await_count == 1
        assert len(added) == 1
        version = added[0]
        assert version.trigger == "admin_font_override"
        assert version.template_id == "tmpl-123"

    def test_cascade_rules(self, client):
        recipe = _recipe_with_overlays(font_default="Inter Tight")
        res, _, template, _ = self._run(
            client, recipe, {"font_default": "DM Sans"}
        )

        assert res.status_code == 200
        overlays = template.recipe_cached["slots"][0]["text_overlays"]
        # Empty inherited the new default
        assert overlays[0]["font_family"] == "DM Sans"
        # Equal-to-old cascaded to new
        assert overlays[1]["font_family"] == "DM Sans"
        # Explicit pin survived
        assert overlays[2]["font_family"] == "Playfair Display"
        assert template.recipe_cached["font_default"] == "DM Sans"

    def test_no_op_when_same_font(self, client):
        recipe = _recipe_with_overlays(font_default="Inter Tight")
        res, added, _, mock_db = self._run(
            client, recipe, {"font_default": "Inter Tight"}
        )

        assert res.status_code == 200
        body = res.json()
        assert body["version_id"] == ""
        assert body["version_number"] == 0
        # No write side effects
        assert added == []
        assert mock_db.commit.await_count == 0

    def test_rejects_unknown_font(self, client):
        recipe = _recipe_with_overlays(font_default="Inter Tight")
        res, _, _, _ = self._run(
            client,
            recipe,
            {"font_default": "Not-A-Real-Font-Family-Name"},
        )

        assert res.status_code == 400
        assert "font registry" in res.json()["detail"].lower()


class TestMigrationStaticLink:
    """Cheap, no-DB guard against the class of bug PR #189 hit.

    If someone introduces a new TemplateRecipeVersion.trigger value in
    code without a matching alembic migration, this fails at unit-test
    time rather than at production-deploy time.
    """

    @staticmethod
    def _latest_trigger_allowlist() -> set[str]:
        """Parse all migrations that touch ck_recipe_version_trigger and
        return the SET defined by the most-recent upgrade()."""
        migrations = sorted(MIGRATIONS_DIR.glob("[0-9]*.py"))
        latest_allowlist: set[str] = set()
        for path in migrations:
            text = path.read_text()
            if "ck_recipe_version_trigger" not in text:
                continue
            # Take the upgrade() body, find the most recent create_check_constraint
            # block, and pull every single-quoted token from its trigger IN (...) clause.
            upgrade = text.split("def upgrade")[1].split("def downgrade")[0]
            match = re.search(
                r"create_check_constraint\([^)]*ck_recipe_version_trigger[^)]*?"
                r"trigger IN \(([^)]+)\)",
                upgrade,
                re.DOTALL,
            )
            if not match:
                continue
            latest_allowlist = set(re.findall(r"'([a-z_]+)'", match.group(1)))
        assert latest_allowlist, (
            "No ck_recipe_version_trigger create_check_constraint found in "
            "any migration — has the constraint name changed?"
        )
        return latest_allowlist

    def test_admin_font_override_is_allowed(self):
        assert "admin_font_override" in self._latest_trigger_allowlist()

    def test_route_trigger_value_matches_migration(self):
        """Every literal `trigger="<value>"` written by app/routes/admin.py
        must be in the latest CHECK constraint's allowlist."""
        route_text = ADMIN_ROUTE_FILE.read_text()
        triggers_used = set(re.findall(r'trigger="([a-z_]+)"', route_text))
        assert triggers_used, "Expected at least one trigger= literal in admin.py"
        allowed = self._latest_trigger_allowlist()
        missing = triggers_used - allowed
        assert not missing, (
            f"admin.py emits TemplateRecipeVersion trigger values not in "
            f"ck_recipe_version_trigger: {sorted(missing)}. Add a migration."
        )
