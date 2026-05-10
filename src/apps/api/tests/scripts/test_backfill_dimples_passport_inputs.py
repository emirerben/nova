"""Smoke tests for the prod backfill script.

The backfill is operational code (run once per environment), not in the request
hot path. We don't mock the full async DB flow — the real failure mode this
guards against is the backfill drifting out of sync with the seed (e.g., the
seed renames REQUIRED_INPUTS or TEMPLATE_NAME and the backfill writes the wrong
shape).
"""
import importlib.util
import os
import sys

import pytest

_BACKFILL_PATH = os.path.normpath(os.path.join(
    os.path.dirname(__file__), "..", "..", "scripts", "backfill_dimples_passport_inputs.py"
))
_SEED_PATH = os.path.normpath(os.path.join(
    os.path.dirname(__file__), "..", "..", "scripts", "seed_dimples_passport_brazil.py"
))


def _load(name: str, path: str):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def seed_module():
    return _load("seed_dimples_passport_brazil_test", _SEED_PATH)


@pytest.fixture
def backfill_module():
    # DATABASE_URL must be set for app.database to import without crashing.
    os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test@localhost:5432/test")
    return _load("backfill_dimples_passport_inputs_test", _BACKFILL_PATH)


class TestBackfillSourcesFromSeed:
    """Backfill must read REQUIRED_INPUTS and TEMPLATE_NAME from the seed
    file directly — single source of truth. If these drift, prod renders
    the wrong input field after backfill."""

    def test_template_name_matches_seed(self, backfill_module, seed_module):
        assert backfill_module.TEMPLATE_NAME == seed_module.TEMPLATE_NAME

    def test_required_inputs_match_seed(self, backfill_module, seed_module):
        assert backfill_module.REQUIRED_INPUTS == seed_module.REQUIRED_INPUTS

    def test_required_inputs_has_location_key(self, backfill_module):
        keys = [spec["key"] for spec in backfill_module.REQUIRED_INPUTS]
        assert "location" in keys


class TestConfirmTargetDb:
    """The --yes flag bypasses the interactive prompt; non-interactive runs
    without --yes must hard-exit with code 2 to prevent CI/cron from
    accidentally writing to the wrong DB."""

    def test_yes_flag_skips_prompt(self, backfill_module, monkeypatch):
        monkeypatch.setattr(sys, "argv", ["script", "--yes"])
        # Should not raise / not exit.
        backfill_module._confirm_target_db()

    def test_non_tty_without_yes_exits_2(self, backfill_module, monkeypatch):
        monkeypatch.setattr(sys, "argv", ["script"])
        monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
        with pytest.raises(SystemExit) as exc:
            backfill_module._confirm_target_db()
        assert exc.value.code == 2
