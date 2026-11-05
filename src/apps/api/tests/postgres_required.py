"""Whether this run is one that provides a migrated ``*_test`` Postgres and so must use it.

``CI`` alone cannot say so: the "Structural evals" job runs ``pytest tests/evals/`` with ``CI=true``
and no Postgres service. Only the api test shards set ``NOVA_REQUIRE_POSTGRES_TESTS=1``.
"""

from __future__ import annotations

import os


def postgres_required() -> bool:
    return os.environ.get("NOVA_REQUIRE_POSTGRES_TESTS") == "1"
