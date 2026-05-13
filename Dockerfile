# Production Dockerfile for Nova API — deployed to Fly.io
# Build context: repo root (not src/apps/api/)

FROM python:3.11-slim

# System deps for FFmpeg pipeline, opencv, python-magic, libheif (HEIC photo support)
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    fonts-dejavu-core \
    libmagic1 \
    libgl1 \
    libglib2.0-0 \
    libheif1 \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Non-root user
RUN groupadd --gid 1000 nova && \
    useradd --uid 1000 --gid nova --create-home nova

WORKDIR /app

# ---------- dependency install (cached layer) ----------
# Parse deps from pyproject.toml into a requirements file, then install.
# Includes the [observability] optional-dependencies group (langfuse +
# anthropic) so Loop A tracing + Loop B online judge are live in prod when
# LANGFUSE_* / ANTHROPIC_API_KEY env vars are set; otherwise fail-open.
# [dev] and [local-whisper] extras are intentionally excluded.
# This layer only busts when pyproject.toml changes, not on source edits.
COPY src/apps/api/pyproject.toml /tmp/pyproject.toml
RUN pip install --no-cache-dir --upgrade pip setuptools && \
    python -c "import tomllib; \
      f = open('/tmp/pyproject.toml', 'rb'); \
      data = tomllib.load(f); \
      deps = data['project']['dependencies'] + data['project']['optional-dependencies']['observability']; \
      print('\n'.join(deps))" > /tmp/requirements.txt && \
    pip install --no-cache-dir -r /tmp/requirements.txt && \
    rm /tmp/requirements.txt /tmp/pyproject.toml

# ---------- application source ----------
# uvicorn adds CWD to sys.path, so app.main resolves from /app/app/main.py
COPY src/apps/api/app ./app
COPY src/apps/api/assets ./assets
COPY src/apps/api/prompts ./prompts
COPY src/apps/api/scripts ./scripts
COPY src/apps/api/alembic.ini .
# Eval rubrics — read at runtime by app/agents/_online_eval.py (Loop B online
# judge). _RUBRICS_ROOT is __file__-relative and resolves to /app/tests/evals/
# rubrics, so the path here MUST match. Markdown only (~24K), no test code.
COPY src/apps/api/tests/evals/rubrics ./tests/evals/rubrics

# Own everything under /app by nova
RUN chown -R nova:nova /app

USER nova

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD curl -f http://localhost:8000/health || exit 1

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
