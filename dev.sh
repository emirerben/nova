#!/bin/bash
# Nova local dev launcher — opens API, Celery worker, and frontend in separate Terminal tabs

REPO="/Users/emirerben/projects/nova"
API_DIR="$REPO/src/apps/api"
WEB_DIR="$REPO/src/apps/web"
ENV_FILE="$REPO/.env"

# Load env vars for the API
ENV_VARS=$(grep -v '^#' "$ENV_FILE" | grep -v '^$' | xargs)

osascript <<EOF
tell application "Terminal"
  -- Tab 1: API server
  do script "cd '$API_DIR' && env $ENV_VARS uvicorn app.main:app --reload --port 8000"

  -- Tab 2: Celery worker
  tell application "System Events" to keystroke "t" using command down
  delay 0.5
  do script "cd '$API_DIR' && env $ENV_VARS celery -A app.worker:celery_app worker --loglevel=info --concurrency=2" in front window

  -- Tab 3: Frontend
  tell application "System Events" to keystroke "t" using command down
  delay 0.5
  do script "cd '$WEB_DIR' && npm run dev" in front window
end tell
EOF

echo "Starting Nova..."
echo "  API:      http://localhost:8000"
echo "  Worker:   Celery (2 concurrent tasks)"
echo "  Frontend: http://localhost:3000"
echo ""
echo "Redis + Postgres already running via Homebrew."
echo ""
echo "First time? Run migrations:"
echo "  cd $API_DIR && env $ENV_VARS alembic upgrade head"
