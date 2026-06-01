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
os.environ.setdefault("WAITLIST_ADMIN_SECRET", "test-admin-secret")
os.environ.setdefault("ALLOWED_ORIGINS", '["http://localhost:3000"]')
# Strict plan-route auth fails closed when INTERNAL_API_KEY is unset, so the
# test env sets it explicitly — strict-path tests must pass the matching bearer
# to exercise the real check (rather than relying on a fail-open bypass).
os.environ.setdefault("INTERNAL_API_KEY", "test-internal-key")
