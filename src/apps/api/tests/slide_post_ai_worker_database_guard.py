"""Pure database URL guard for the fake-provider process test helper."""

from urllib.parse import urlparse

from tests._worker_database import worker_database_names

PRIVATE_DATABASE = "nova_auto_convergence_ai_20261106_test"
LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "::1"}


def validate_database_url(url, environment):
    parsed = urlparse(url)
    assert parsed.scheme in {"postgresql", "postgres"}
    assert not parsed.query and not parsed.fragment
    assert parsed.hostname in LOOPBACK_HOSTS
    assert (5432 if parsed.port is None else parsed.port) == 5432
    allowed = {PRIVATE_DATABASE}
    if all(
        environment.get(key) == value
        for key, value in {
            "CI": "true",
            "GITHUB_ACTIONS": "true",
            "GITHUB_REPOSITORY": "Kria-AI/kria",
        }.items()
    ):
        allowed.update({"nova_test", "nova_journey_test"})
        # The api shards' per-worker copies of nova_test: exact names, never a prefix.
        allowed.update(worker_database_names("nova_test"))
    assert parsed.path in {f"/{name}" for name in allowed}
    return parsed.path[1:]
