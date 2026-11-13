"""KRI-1381 guard: the app's protocol number lives in `KriaClientProtocolContract.version`
and the `client_protocol.py` constants only. A test that hard-codes the current number
turns every feature PR into a merge conflict; scripts/bump-protocol.py assigns the next one."""

from __future__ import annotations

import importlib.util
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[4]


def _load_bump_script():
    path = REPO_ROOT / "scripts" / "bump-protocol.py"
    spec = importlib.util.spec_from_file_location("bump_protocol", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_no_test_hard_codes_the_current_client_protocol():
    problems = _load_bump_script().check_hardcoded_literals(REPO_ROOT)
    assert not problems, "\n".join(problems)
