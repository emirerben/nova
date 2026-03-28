from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Storage
    storage_bucket: str
    storage_provider: str = "gcs"
    gcloud_project: str = ""
    google_application_credentials: str = ""

    # Redis / Celery
    redis_url: str = "redis://localhost:6379"

    # Database
    database_url: str

    @field_validator("database_url")
    @classmethod
    def normalize_postgres_scheme(cls, v: str) -> str:
        """Fly Postgres (and Heroku) provide postgres:// URLs, but
        SQLAlchemy 1.4+ only recognises the postgresql:// scheme."""
        if v.startswith("postgres://"):
            v = v.replace("postgres://", "postgresql://", 1)
        return v

    @property
    def asyncpg_database_url(self) -> str:
        """Return a postgresql+asyncpg:// URL suitable for asyncpg.

        asyncpg does not understand libpq-specific query params like
        ``sslmode``.  We translate ``sslmode`` to asyncpg's ``ssl``
        param and swap the scheme to ``postgresql+asyncpg://``.
        """
        url = self.database_url.replace(
            "postgresql://", "postgresql+asyncpg://", 1
        )
        parsed = urlparse(url)
        qs = parse_qs(parsed.query)
        # Translate libpq sslmode → asyncpg ssl
        sslmode_vals = qs.pop("sslmode", None)
        if sslmode_vals and "ssl" not in qs:
            qs["ssl"] = sslmode_vals  # e.g. ["disable"]
        cleaned_query = urlencode(qs, doseq=True)
        return urlunparse(parsed._replace(query=cleaned_query))

    # OpenAI (Whisper fallback)
    openai_api_key: str = ""

    # Gemini
    gemini_api_key: str = ""
    gemini_model: str = "gemini-2.5-flash"  # "gemini-2.5-flash" | "gemini-2.5-pro"

    # Admin
    admin_api_key: str = ""

    # Transcription backend
    transcriber_backend: str = "gemini"  # "gemini" | "whisper"

    # Template
    default_template_id: str = ""

    # Whisper
    whisper_backend: str = "openai-api"  # "openai-api" | "local"
    whisper_model: str = "base.en"

    # CORS — comma-separated in env: ALLOWED_ORIGINS=https://nova.io,http://localhost:3000
    allowed_origins: list[str] = ["http://localhost:3000"]

    # Waitlist admin
    waitlist_admin_secret: str = "changeme"

    # Resend (transactional email) — leave empty to skip confirmation emails
    resend_api_key: str = ""

    # Eval harness — gates per-slot GCS upload for visual comparison
    eval_harness_enabled: bool = False

    # Security
    token_encryption_key: str = ""

    # OAuth
    instagram_client_id: str = ""
    instagram_client_secret: str = ""
    instagram_redirect_uri: str = "http://localhost:8000/auth/instagram/callback"

    youtube_client_id: str = ""
    youtube_client_secret: str = ""
    youtube_redirect_uri: str = "http://localhost:8000/auth/youtube/callback"

    tiktok_client_key: str = ""
    tiktok_client_secret: str = ""
    tiktok_redirect_uri: str = "http://localhost:8000/auth/tiktok/callback"

    # Scoring weights (named constants — change here only)
    hook_weight: float = 0.65
    engagement_weight: float = 0.35

    # Pipeline limits
    max_upload_bytes: int = 4 * 1024 * 1024 * 1024  # 4GB
    max_duration_s: float = 1800.0  # 30 min
    max_concurrent_jobs_per_user: int = 5
    max_jobs_per_hour_per_user: int = 20

    # Output spec
    output_width: int = 1080
    output_height: int = 1920
    output_fps: int = 30
    output_video_bitrate: str = "4M"
    output_audio_bitrate: str = "192k"
    output_min_duration_s: float = 45.0
    output_max_duration_s: float = 59.0
    output_target_lufs: float = -14.0


settings = Settings()
