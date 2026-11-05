"""The two Postgres suites that skipped silently when the database was missing.

``tests/signals/test_near_dup_store_postgres.py`` and
``tests/evals/test_suggestion_freshness_postgres.py`` skipped their whole module when
``*_test`` Postgres was unreachable, on CI too: a broken CI database then reads as a green
check that ran none of them. The other suggestion suites use the CI-aware guard
(``tests/services/test_suggestion_serve_wiring_postgres.py``,
``tests/e2e_suggestions/conftest.py``): a laptop run skips, a run with ``CI`` set fails
collection. These rows replay exactly that, in a child pytest pointed at a database that
cannot exist, once with ``CI`` set and once without.

Failure catalogue, written before any code
  C1  CI set, database unreachable: the child must FAIL (non-zero exit), not skip.
  C2  CI unset, database unreachable: the child must SKIP cleanly, with a reason that names
      the problem (pytest exits 5, "nothing collected", when a whole module skips), so a
      laptop without Postgres still runs everything else.
  C3  the refusal to touch a non-``_test`` database stays a skip locally and a failure on CI.
  C4  the guard must not need the database it is guarding to even be importable: the child
      is started with a port nothing listens on.
  C5  CI set but ``NOVA_REQUIRE_POSTGRES_TESTS`` unset: SKIP. The "Structural evals" job runs
      ``pytest tests/evals/`` with ``CI=true`` and has no Postgres service; a hard failure keyed
      on ``CI`` alone broke every prompt/agent PR there. Only the api shards, which provide a
      migrated ``nova_test``, set the opt-in.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

API = Path(__file__).resolve().parents[2]
SUITES = [
    "tests/signals/test_near_dup_store_postgres.py",
    "tests/evals/test_suggestion_freshness_postgres.py",
    "tests/services/test_suggestion_serve_wiring_postgres.py",
    "tests/tasks/test_kria_pinned_suggestion.py",
    "tests/tasks/test_kria_pinned_kinds.py",
]
# Nothing listens on port 1; the name ends in _test so the "refuse a dev database" branch is not
# the one under test.
NOTHING_RAN = {0, 5}  # a module-level skip collects no test, which pytest reports as 5
UNREACHABLE = "postgresql://nobody@127.0.0.1:1/nova_unreachable_test"
NOT_A_TEST_DB = "postgresql://nobody@127.0.0.1:1/nova_dev"


def run_child(
    suite: str, *, database_url: str, ci: bool, require: bool | None = None
) -> subprocess.CompletedProcess[str]:
    """``require`` defaults to ``ci``: the api shards set both; the evals job sets only ``CI``."""
    require = ci if require is None else require
    env = {
        k: v
        for k, v in os.environ.items()
        if k not in {"CI", "DATABASE_URL", "NOVA_REQUIRE_POSTGRES_TESTS"}
    }
    env["DATABASE_URL"] = database_url
    if ci:
        env["CI"] = "1"
    if require:
        env["NOVA_REQUIRE_POSTGRES_TESTS"] = "1"
    return subprocess.run(
        [sys.executable, "-m", "pytest", suite, "-q", "-rs", "-p", "no:cacheprovider"],
        cwd=API,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )


@pytest.mark.parametrize("suite", SUITES)
def test_ci_fails_when_the_database_is_unreachable(suite):
    done = run_child(suite, database_url=UNREACHABLE, ci=True)
    assert done.returncode != 0, done.stdout[-600:]
    assert "must run in CI" in done.stdout + done.stderr


@pytest.mark.parametrize("suite", SUITES)
def test_a_laptop_skips_cleanly_when_the_database_is_unreachable(suite):
    done = run_child(suite, database_url=UNREACHABLE, ci=False)
    assert done.returncode in NOTHING_RAN, done.stdout[-600:]
    assert "SKIPPED" in done.stdout and "Postgres" in done.stdout


@pytest.mark.parametrize("suite", SUITES)
def test_a_non_test_database_is_a_skip_locally_and_a_failure_on_ci(suite):
    local = run_child(suite, database_url=NOT_A_TEST_DB, ci=False)
    assert local.returncode in NOTHING_RAN and "SKIPPED" in local.stdout
    ci = run_child(suite, database_url=NOT_A_TEST_DB, ci=True)
    assert ci.returncode != 0 and "must run in CI" in ci.stdout + ci.stderr


@pytest.mark.parametrize("database_url", [UNREACHABLE, NOT_A_TEST_DB])
@pytest.mark.parametrize("suite", SUITES)
def test_ci_without_the_opt_in_skips_where_no_postgres_is_provided(suite, database_url):
    done = run_child(suite, database_url=database_url, ci=True, require=False)
    assert done.returncode in NOTHING_RAN, done.stdout[-600:]
    assert "SKIPPED" in done.stdout and "must run in CI" not in done.stdout + done.stderr
