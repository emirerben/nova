"""Whether this run has the repository's full history and so must use it.

The API shards clone a single commit, so tests that ask git about older commits
skip there. The `creator-contract-replay` job clones everything and sets
``NOVA_REQUIRE_GIT_HISTORY=1``: in that job a missing commit or merge base is a
failure, never a skip. Same shape as ``tests/postgres_required.py``.
"""

from __future__ import annotations

import os


def git_history_required() -> bool:
    return os.environ.get("NOVA_REQUIRE_GIT_HISTORY") == "1"
