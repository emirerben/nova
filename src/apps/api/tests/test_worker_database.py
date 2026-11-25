"""One Postgres database per pytest-xdist worker: how the selection could go wrong.

Written before the code, as the list of failures the module has to rule out:

1. A worker selects a database the runner never made (worker count mismatch, a renamed base)
   and quietly keeps using the shared one, so the isolation is claimed and absent.
2. The runner made no copies, or pytest runs without xdist, and the URL changes anyway (laptop
   runs, the git-history job, the journey jobs, which bring other databases).
3. A pytest started by a test inherits the worker's environment. It either keeps the URL the
   worker already selected, and maps it twice, or brings a URL of its own on purpose (the "wrong
   database must be refused" tests pass ``nova_prod`` and an unreachable host) and is mapped or
   stopped although it is not one of this run's workers. Both happened in the first hosted run.
4. The worker database's name stops ending in ``_test``, and every "refusing to write to a
   non-test database" check in the suite turns its file into a skip.
5. The base URL carries options, a second path part or a name that is not ``*_test``, and the
   clone is built from (or dropped on) something that is not the test database.
6. The worker id is not xdist's ``gwN`` and ends up inside a database name.
7. A name check widened for worker databases admits a look-alike (``nova_gw_test``,
   ``nova_gw0x_test``, a worker number nobody creates).
8. The runner is pointed at a server that is not this machine and drops databases there.
"""

from __future__ import annotations

import pytest

from tests import _worker_database as wd

BASE = "postgresql://postgres:postgres@localhost:5432/nova_test"


def _environ(**changes):
    return {"DATABASE_URL": BASE, wd.COPIED_FROM: BASE, "PYTEST_XDIST_WORKER": "gw2", **changes}


def test_a_worker_gets_its_own_database_and_the_name_still_ends_in_test():
    environ = _environ()
    seen = []
    assert wd.select(environ, probe=seen.append) == (
        "postgresql://postgres:postgres@localhost:5432/nova_gw2_test"
    )
    assert environ["DATABASE_URL"].endswith("/nova_gw2_test")
    assert seen == [environ["DATABASE_URL"]]


def test_the_async_url_form_keeps_its_driver():
    url = "postgresql+asyncpg://localhost/nova_test"
    environ = _environ(DATABASE_URL=url, **{wd.COPIED_FROM: url})
    wd.select(environ, probe=lambda url: None)
    assert environ["DATABASE_URL"] == "postgresql+asyncpg://localhost/nova_gw2_test"


def test_a_missing_worker_database_stops_the_worker_and_leaves_the_url_alone():
    environ = _environ()

    def missing(url):
        raise RuntimeError(f"no database behind {url}")

    with pytest.raises(RuntimeError):
        wd.select(environ, probe=missing)
    assert environ["DATABASE_URL"] == BASE


def _never(url):
    raise AssertionError("no database may be probed")


@pytest.mark.parametrize("changes", [{wd.COPIED_FROM: ""}, {"PYTEST_XDIST_WORKER": ""}])
def test_without_copies_or_a_worker_nothing_changes(changes):
    environ = _environ(**changes)
    assert wd.select(environ, probe=_never) is None
    assert environ["DATABASE_URL"] == BASE


def test_the_switch_alone_selects_nothing_because_only_the_runner_knows_copies_exist():
    environ = {"DATABASE_URL": BASE, wd.SWITCH: "1", "PYTEST_XDIST_WORKER": "gw2"}
    assert wd.select(environ, probe=_never) is None
    assert environ["DATABASE_URL"] == BASE


@pytest.mark.parametrize(
    "own",
    [
        "postgresql://postgres:postgres@localhost:5432/nova_prod",
        "postgresql://postgres:postgres@127.0.0.1:1/nova_unreachable_test",
        "postgresql://localhost/nova_test?host=production",
        "not a url",
    ],
)
def test_a_pytest_started_by_a_test_with_its_own_database_is_left_alone(own):
    environ = _environ(DATABASE_URL=own)
    assert wd.select(environ, probe=_never) is None
    assert environ["DATABASE_URL"] == own


def test_a_pytest_started_by_a_test_keeps_the_workers_database():
    environ = _environ()
    wd.select(environ, probe=lambda url: None)
    first = environ["DATABASE_URL"]
    assert first != BASE
    assert wd.select(environ, probe=_never) is None
    assert environ["DATABASE_URL"] == first


@pytest.mark.parametrize(
    "url",
    [
        "postgresql://localhost/nova_test?host=production",
        "postgresql://localhost/nova_test#fragment",
        "postgresql://localhost/nova_test/extra",
        "postgresql://localhost/nova",
        "postgresql://localhost/nova_dev",
        "postgresql://localhost/_test",
        "postgresql://localhost/Nova_test",
        'postgresql://localhost/nova"_test',
        "postgresql://localhost/",
        "sqlite:///nova_test",
    ],
)
def test_a_base_that_is_not_a_plain_test_database_is_refused(url):
    with pytest.raises(ValueError):
        wd.worker_url(url, "gw0")
    with pytest.raises(ValueError):
        wd.select(_environ(DATABASE_URL=url, **{wd.COPIED_FROM: url}), probe=lambda url: None)


@pytest.mark.parametrize(
    "worker", ["master", "gw", "gw-1", "gw01", "gw0 ", "gw0;drop", "0", f"gw{wd.MAX_WORKERS}"]
)
def test_a_worker_id_that_is_not_one_of_xdists_is_refused(worker):
    with pytest.raises(ValueError):
        wd.worker_url(BASE, worker)


def test_the_name_list_is_exactly_the_databases_a_worker_can_select():
    names = wd.worker_database_names("nova_test")
    assert len(names) == wd.MAX_WORKERS
    for index in range(wd.MAX_WORKERS):
        url = wd.worker_url(BASE, f"gw{index}")
        assert url.rsplit("/", 1)[1] in names
    assert all(name.endswith("_test") for name in names)
    for look_alike in (
        "nova_test",
        "nova_gw_test",
        "nova_gw0x_test",
        "nova_gw00_test",
        f"nova_gw{wd.MAX_WORKERS}_test",
        "nova_gw0_test2",
        "xnova_gw0_test",
    ):
        assert look_alike not in names


@pytest.mark.parametrize(
    "url",
    [
        "postgresql://db.internal:5432/nova_test",
        "postgresql://10.0.0.5/nova_test",
        "postgresql://database.example/nova_test",
    ],
)
def test_databases_are_only_made_on_this_machine(url):
    with pytest.raises(ValueError):
        wd.creation_plan(url, 4)


@pytest.mark.parametrize("count", [0, -1, wd.MAX_WORKERS + 1])
def test_a_worker_count_outside_the_name_list_is_refused(count):
    with pytest.raises(ValueError):
        wd.creation_plan(BASE, count)


def test_each_worker_database_is_dropped_then_cloned_from_the_migrated_one():
    admin_url, statements = wd.creation_plan(BASE, 2)
    assert admin_url == "postgresql://postgres:postgres@localhost:5432/postgres"
    assert statements == [
        'DROP DATABASE IF EXISTS "nova_gw0_test" WITH (FORCE)',
        'CREATE DATABASE "nova_gw0_test" TEMPLATE "nova_test"',
        'DROP DATABASE IF EXISTS "nova_gw1_test" WITH (FORCE)',
        'CREATE DATABASE "nova_gw1_test" TEMPLATE "nova_test"',
    ]


def test_the_admin_connection_is_synchronous_even_for_an_async_url():
    admin_url, _ = wd.creation_plan("postgresql+asyncpg://localhost/nova_test", 1)
    assert admin_url == "postgresql://localhost/postgres"
