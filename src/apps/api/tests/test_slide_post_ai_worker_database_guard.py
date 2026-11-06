"""Offline admission matrix for the fresh-process fake worker's database guard."""

import pytest

from tests.slide_post_ai_worker_database_guard import validate_database_url

PRIVATE = "nova_auto_convergence_ai_20261106_test"
TRUSTED = {"CI": "true", "GITHUB_ACTIONS": "true", "GITHUB_REPOSITORY": "emirerben/nova"}


@pytest.mark.parametrize("host", ["localhost", "127.0.0.1", "[::1]"])
@pytest.mark.parametrize("name", [PRIVATE, "nova_test", "nova_journey_test"])
def test_exact_trusted_databases(name, host):
    assert validate_database_url(f"postgresql://test@{host}:5432/{name}", TRUSTED) == name


def test_private_database_requires_no_ci_environment():
    assert validate_database_url(f"postgres://localhost/{PRIVATE}", {}) == PRIVATE


@pytest.mark.parametrize(
    "changed", [{}, {"CI": "1"}, {"GITHUB_ACTIONS": "false"}, {"GITHUB_REPOSITORY": "other/nova"}]
)
@pytest.mark.parametrize("name", ["nova_test", "nova_journey_test"])
def test_ci_names_require_all_exact_trusted_signals(name, changed):
    environment = {} if not changed else {**TRUSTED, **changed}
    with pytest.raises((AssertionError, ValueError)):
        validate_database_url(f"postgresql://localhost/{name}", environment)


@pytest.mark.parametrize(
    "url",
    [
        "postgresql://localhost/nova",
        "postgresql://localhost/production",
        "postgresql://localhost/another_test",
        "postgresql://remote:5432/nova_test",
        "postgresql://localhost:5433/nova_test",
        "postgresql://localhost:0/nova_test",
        "postgresql://localhost/nova_test?host=production",
        "postgresql://localhost/nova_test#fragment",
        "postgresql://localhost/nova_test/extra",
        "postgresql://localhost/%6eova_test",
        "https://localhost/nova_test",
    ],
)
def test_unsafe_urls_rejected(url):
    with pytest.raises((AssertionError, ValueError)):
        validate_database_url(url, TRUSTED)


@pytest.mark.parametrize(
    "suffix", ["?host=production", "#fragment", "?dbname=nova", "?sslmode=require"]
)
def test_private_database_rejects_url_options(suffix):
    with pytest.raises((AssertionError, ValueError)):
        validate_database_url(f"postgresql://localhost/{PRIVATE}{suffix}", {})
