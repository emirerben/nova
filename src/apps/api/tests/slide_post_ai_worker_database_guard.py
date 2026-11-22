"""Pure database URL guard for the fake-provider process test helper."""

from urllib.parse import urlparse

PRIVATE_DATABASE = "nova_auto_convergence_ai_20261106_test"
LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "::1"}
# The repository answers under its new name since the 2026-11-22 move; the old one still redirects.
TRUSTED_REPOSITORIES = {"emirerben/nova", "Kria-AI/kria"}


def validate_database_url(url, environment):
    parsed = urlparse(url)
    assert parsed.scheme in {"postgresql", "postgres"}
    assert not parsed.query and not parsed.fragment
    assert parsed.hostname in LOOPBACK_HOSTS
    assert (5432 if parsed.port is None else parsed.port) == 5432
    allowed = {PRIVATE_DATABASE}
    if (
        environment.get("CI") == "true"
        and environment.get("GITHUB_ACTIONS") == "true"
        and environment.get("GITHUB_REPOSITORY") in TRUSTED_REPOSITORIES
    ):
        allowed.update({"nova_test", "nova_journey_test"})
    assert parsed.path in {f"/{name}" for name in allowed}
    return parsed.path[1:]
