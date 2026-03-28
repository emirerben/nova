"""Tests for Settings.normalize_postgres_scheme and asyncpg_database_url."""

import pytest


@pytest.fixture()
def _clean_env(monkeypatch):
    """Provide required env vars for a Settings instance."""
    monkeypatch.setenv("STORAGE_BUCKET", "test")
    monkeypatch.setenv("STORAGE_PROVIDER", "gcs")
    monkeypatch.setenv("REDIS_URL", "redis://localhost:6379")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setenv("TOKEN_ENCRYPTION_KEY", "test")
    monkeypatch.setenv("WHISPER_BACKEND", "local")
    monkeypatch.setenv("WAITLIST_ADMIN_SECRET", "test")
    monkeypatch.setenv("ALLOWED_ORIGINS", '["http://localhost:3000"]')


class TestNormalizePostgresScheme:
    """field_validator on database_url rewrites postgres:// to postgresql://."""

    @pytest.mark.usefixtures("_clean_env")
    def test_postgres_scheme_is_rewritten(self, monkeypatch):
        monkeypatch.setenv(
            "DATABASE_URL", "postgres://u:p@host:5432/db"
        )
        from app.config import Settings

        s = Settings()
        assert s.database_url.startswith("postgresql://")
        assert "postgres://u:" not in s.database_url

    @pytest.mark.usefixtures("_clean_env")
    def test_postgresql_scheme_unchanged(self, monkeypatch):
        monkeypatch.setenv(
            "DATABASE_URL", "postgresql://u:p@host:5432/db"
        )
        from app.config import Settings

        s = Settings()
        assert s.database_url == "postgresql://u:p@host:5432/db"


class TestAsyncpgDatabaseUrl:
    """Property that swaps scheme and translates sslmode → ssl."""

    @pytest.mark.usefixtures("_clean_env")
    def test_scheme_swap(self, monkeypatch):
        monkeypatch.setenv(
            "DATABASE_URL", "postgresql://u:p@host:5432/db"
        )
        from app.config import Settings

        s = Settings()
        url = s.asyncpg_database_url
        assert url.startswith("postgresql+asyncpg://")

    @pytest.mark.usefixtures("_clean_env")
    def test_sslmode_translated_to_ssl(self, monkeypatch):
        monkeypatch.setenv(
            "DATABASE_URL",
            "postgresql://u:p@host:5432/db?sslmode=disable",
        )
        from app.config import Settings

        s = Settings()
        url = s.asyncpg_database_url
        assert "sslmode" not in url
        assert "ssl=disable" in url

    @pytest.mark.usefixtures("_clean_env")
    def test_no_sslmode_no_ssl_param(self, monkeypatch):
        monkeypatch.setenv(
            "DATABASE_URL", "postgresql://u:p@host:5432/db"
        )
        from app.config import Settings

        s = Settings()
        url = s.asyncpg_database_url
        assert "ssl" not in url
        assert url == "postgresql+asyncpg://u:p@host:5432/db"

    @pytest.mark.usefixtures("_clean_env")
    def test_existing_ssl_not_overwritten(self, monkeypatch):
        monkeypatch.setenv(
            "DATABASE_URL",
            "postgresql://u:p@host:5432/db?sslmode=require&ssl=prefer",
        )
        from app.config import Settings

        s = Settings()
        url = s.asyncpg_database_url
        # sslmode is removed, but existing ssl=prefer is preserved
        assert "sslmode" not in url
        assert "ssl=prefer" in url
