"""One Postgres database per pytest-xdist worker.

Every xdist worker used to share ``nova_test``, so one worker's sweep or ``TRUNCATE`` could
change rows another worker's test was about to assert on. Each such collision was patched with
an advisory-lock fixture in ``conftest.py``. With the switch on, each worker gets a copy of the
migrated database instead:

- ``scripts/ci/api-tests.py`` makes the copies before pytest starts (``python -m
  tests._worker_database COUNT``) and tells pytest which URL they are copies of. Postgres
  refuses to copy a database that has open connections, so this cannot happen once tests run.
- ``conftest.py`` calls ``select`` before any app module is imported. A worker whose copy is
  missing stops; it never falls back to the shared database.
- Only a process whose ``DATABASE_URL`` is still the copied one is moved. A pytest that a test
  starts inherits the worker's id; it keeps whatever URL the test gave it, be that the
  worker's own copy or a wrong database the test wants refused.

Off (the default) nothing changes. ``test_worker_database.py`` lists the failures this rules out.
Not isolated yet: Redis, and the scripts that hardcode the ``nova_test`` URL.
"""

from __future__ import annotations

import os
import re
import sys
from collections.abc import Callable, MutableMapping
from urllib.parse import urlsplit, urlunsplit

# What ci.yml asks the runner for.
SWITCH = "NOVA_TEST_DB_PER_WORKER"
# What the runner tells pytest once the copies exist: the URL they are copies of.
COPIED_FROM = "NOVA_TEST_DB_WORKER_COPIES_OF"
# The name checks that admit worker databases list exactly this many names.
MAX_WORKERS = 16
_SUFFIX = "_test"
_BASE = re.compile(r"[a-z][a-z0-9_]*_test\Z")
_WORKER = re.compile(r"gw(0|[1-9][0-9]?)\Z")
_LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "::1"}


def _worker_name(base: str, index: int) -> str:
    return f"{base[: -len(_SUFFIX)]}_gw{index}{_SUFFIX}"


def worker_database_names(base: str) -> frozenset[str]:
    """Every database name a worker can select for ``base``, and nothing else."""

    if not _BASE.match(base):
        raise ValueError(f"{base!r} is not a plain *_test database name")
    return frozenset(_worker_name(base, index) for index in range(MAX_WORKERS))


def _base_name(url: str) -> str:
    parts = urlsplit(url)
    if parts.scheme not in {"postgresql", "postgres", "postgresql+asyncpg"}:
        raise ValueError("worker databases need a PostgreSQL URL")
    if parts.query or parts.fragment:
        raise ValueError("worker databases need a URL without options")
    base = parts.path[1:]
    if not _BASE.match(base):
        raise ValueError(f"{base!r} is not a plain *_test database name")
    return base


def worker_url(url: str, worker: str) -> str:
    """``url`` with its database replaced by the one that belongs to xdist worker ``worker``."""

    base = _base_name(url)
    match = _WORKER.match(worker)
    if match is None or int(match.group(1)) >= MAX_WORKERS:
        raise ValueError(f"{worker!r} is not an xdist worker id below gw{MAX_WORKERS}")
    name = _worker_name(base, int(match.group(1)))
    return urlunsplit(urlsplit(url)._replace(path=f"/{name}"))


def _probe(url: str) -> None:
    from sqlalchemy import create_engine, text  # noqa: PLC0415

    engine = create_engine(url.replace("postgresql+asyncpg://", "postgresql://"))
    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
    except Exception as error:
        name = urlsplit(url).path[1:]
        raise RuntimeError(
            f"{COPIED_FROM} is set but this worker's database {name!r} cannot be opened. "
            "scripts/ci/api-tests.py creates it before pytest starts; "
            "`python -m tests._worker_database COUNT` does the same by hand."
        ) from error
    finally:
        engine.dispose()


def select(environ: MutableMapping[str, str], probe: Callable[[str], None] = _probe) -> str | None:
    """Point ``DATABASE_URL`` at this worker's database. Returns it, or None when not in use."""

    worker = environ.get("PYTEST_XDIST_WORKER")
    copied_from = environ.get(COPIED_FROM)
    if not worker or not copied_from or environ.get("DATABASE_URL") != copied_from:
        return None
    selected = worker_url(copied_from, worker)
    probe(selected)
    environ["DATABASE_URL"] = selected
    return selected


def creation_plan(url: str, count: int) -> tuple[str, list[str]]:
    """The maintenance URL and the statements that give ``count`` workers a fresh copy each."""

    base = _base_name(url)
    parts = urlsplit(url)
    if parts.hostname not in _LOOPBACK_HOSTS:
        raise ValueError("worker databases are only created on this machine")
    if not 1 <= count <= MAX_WORKERS:
        raise ValueError(f"worker count must be between 1 and {MAX_WORKERS}, got {count}")
    statements = []
    for index in range(count):
        name = _worker_name(base, index)
        statements.append(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        statements.append(f'CREATE DATABASE "{name}" TEMPLATE "{base}"')
    admin = urlunsplit(parts._replace(scheme="postgresql", path="/postgres"))
    return admin, statements


def create(url: str, count: int) -> None:
    from sqlalchemy import create_engine, text  # noqa: PLC0415

    admin_url, statements = creation_plan(url, count)
    engine = create_engine(admin_url, isolation_level="AUTOCOMMIT")
    try:
        with engine.connect() as connection:
            for statement in statements:
                connection.execute(text(statement))
    finally:
        engine.dispose()


def main(argv: list[str]) -> int:
    if len(argv) != 1 or not argv[0].isdigit():
        raise SystemExit("usage: python -m tests._worker_database WORKER_COUNT")
    try:
        create(os.environ["DATABASE_URL"], int(argv[0]))
    except ValueError as error:
        raise SystemExit(f"error: {error}") from error
    print(f"Worker databases: {argv[0]} copies of the migrated test database")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
