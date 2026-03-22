"""Set required env vars before any app module is imported.

app/config.py instantiates Settings() at module load — required fields must
be present in the environment at collection time, not just at test runtime.
"""

import os

os.environ.setdefault("STORAGE_BUCKET", "nova-test")
os.environ.setdefault("STORAGE_PROVIDER", "gcs")
os.environ.setdefault("DATABASE_URL", "postgresql://postgres:postgres@localhost:5432/nova_test")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379")
os.environ.setdefault("OPENAI_API_KEY", "sk-test")
os.environ.setdefault("TOKEN_ENCRYPTION_KEY", "test-key-not-used-in-unit-tests")
os.environ.setdefault("WHISPER_BACKEND", "local")
