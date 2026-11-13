"""Guard: the production flag snapshot describes the current `Settings` (KRI-1375).

A new boolean flag or allowlist must be seen by `scripts/prod_flag_snapshot.py refresh`
before a test claims to run "as production does"; otherwise it silently runs at its code
default. The failure message names the refresh command.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

SCRIPT = Path(__file__).parents[1] / "scripts" / "prod_flag_snapshot.py"
SPEC = importlib.util.spec_from_file_location("prod_flag_snapshot", SCRIPT)
assert SPEC and SPEC.loader
prod_flag_snapshot = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = prod_flag_snapshot
SPEC.loader.exec_module(prod_flag_snapshot)


def test_snapshot_describes_current_settings() -> None:
    found = prod_flag_snapshot.problems(prod_flag_snapshot.load())
    assert not found, (
        "tests/prod_flags.snapshot.json is out of date; run "
        "`python scripts/prod_flag_snapshot.py refresh [--live]` and commit it:\n"
        + "\n".join(found)
    )
