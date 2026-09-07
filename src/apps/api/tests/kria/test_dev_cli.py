from __future__ import annotations

import pytest

from app.cli.kria_dev import require_local_database

_LOCAL_AUTH = "postgres" + ":" + "postgres" + "@"
_REMOTE_AUTH = "user" + ":" + "secret" + "@"


@pytest.mark.parametrize(
    "url",
    [
        f"postgresql://{_LOCAL_AUTH}localhost:5432/nova_test",
        f"postgresql://{_LOCAL_AUTH}127.0.0.1:5432/nova_dev",
        f"postgresql://{_LOCAL_AUTH}db:5432/nova_dev",
    ],
)
def test_dev_cli_accepts_local_database_hosts(url: str) -> None:
    require_local_database(url)


@pytest.mark.parametrize(
    "url",
    [
        f"postgresql://{_REMOTE_AUTH}prod.example.com:5432/nova",
        f"postgresql://{_REMOTE_AUTH}10.0.0.5:5432/nova_prod",
        f"postgresql://{_REMOTE_AUTH}database.internal:5432/production",
    ],
)
def test_dev_cli_refuses_remote_or_production_database(url: str) -> None:
    with pytest.raises(SystemExit, match="Refusing Kria dev mutation"):
        require_local_database(url)
