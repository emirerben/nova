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

    # OpenAI
    openai_api_key: str = ""

    # Whisper
    whisper_backend: str = "openai-api"  # "openai-api" | "local"
    whisper_model: str = "base.en"

    # CORS — comma-separated in env: ALLOWED_ORIGINS=https://nova.io,http://localhost:3000
    allowed_origins: list[str] = ["http://localhost:3000"]

    # Waitlist admin
    waitlist_admin_secret: str = "changeme"

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
