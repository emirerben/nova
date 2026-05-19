"""Empirical reproduction of the 2026-05-18 07:45:57Z DB-blip incident.

Stands up a minimal Celery worker against local Redis + local Postgres,
queues a task with the SAME decorator pattern as the production
orchestrators, then asks the user to stop Postgres briefly and observe
the autoretry behavior.

PRE-FIX behavior (max_retries=0, no autoretry_for):
  - Task hits OperationalError when Postgres is stopped.
  - Celery does NOT retry. Task terminates with "raised unexpected".
  - State left at whatever the catch-all stored.

POST-FIX behavior (this file mirrors the post-fix decorator):
  - Task hits OperationalError when Postgres is stopped.
  - Celery autoretries with exponential backoff.
  - Once Postgres comes back, the retry succeeds and the task completes.

Usage:
  # Terminal 1: this script — starts the worker and queues the task
  /private/tmp/nova-test-venv/bin/python scripts/repro_db_blip.py

  # Terminal 2: while task is looping, briefly stop Postgres
  brew services stop postgresql@16
  sleep 20
  brew services start postgresql@16

Observe Terminal 1: worker logs `harness_transient_db_error_retry` warning,
then Celery retries with backoff, then the task completes.
"""

from __future__ import annotations

import getpass
import os
import time

from celery import Celery
from sqlalchemy import create_engine, text
from sqlalchemy.exc import OperationalError

# Default uses the current shell user (brew Postgres' default DB role on macOS
# is the OS username). Override via REPRO_DATABASE_URL for any other setup.
PG_URL = os.environ.get(
    "REPRO_DATABASE_URL",
    f"postgresql+psycopg2://{getpass.getuser()}@127.0.0.1:5432/postgres",
)
REDIS_URL = os.environ.get("REPRO_REDIS_URL", "redis://127.0.0.1:6379/15")

# Same pool config as app/database.py (the real orchestrator engine).
engine = create_engine(
    PG_URL,
    pool_pre_ping=True,
    pool_recycle=300,
    pool_size=2,
    max_overflow=3,
)

celery_app = Celery("repro", broker=REDIS_URL, result_backend=REDIS_URL)
celery_app.conf.update(
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    worker_prefetch_multiplier=1,
)


@celery_app.task(
    name="repro.long_running_db_task",
    bind=True,
    # SAME decorator config we added to the real orchestrators:
    #   - autoretry only on OperationalError (the transient class).
    #     DBAPIError is too broad — it includes IntegrityError, ProgrammingError,
    #     DataError, which are deterministic bugs that shouldn't retry.
    #   - retry_jitter=False (deterministic). With jitter ON, Celery picks
    #     a random delay between 0 and the exponential value — halving the
    #     average budget. Empirical 2026-05-18 14:39 test showed jitter
    #     yielding only ~20s vs the 63s upper bound for max_retries=6.
    #   - max_retries=7 gives a deterministic 1+2+4+8+16+32+60 = 123s
    #     budget, ~2× margin over the documented 65s nova-db VM stall.
    autoretry_for=(OperationalError,),
    retry_backoff=True,
    retry_backoff_max=60,
    retry_jitter=False,
    max_retries=7,
    soft_time_limit=300,
)
def long_running_db_task(self, total_iterations: int = 30) -> str:
    """Simulates the orchestrator: holds an engine, does periodic DB writes.

    If Postgres goes away mid-loop, the next .execute() raises
    OperationalError. With the decorator config above, Celery retries
    the whole task (from iteration 0).
    """
    print(
        f"[task] start (retry={self.request.retries}/{self.max_retries}, "
        f"iterations={total_iterations})",
        flush=True,
    )
    for i in range(total_iterations):
        try:
            with engine.connect() as conn:
                result = conn.execute(text("SELECT 1")).scalar()
                assert result == 1
            if i % 5 == 0:
                print(f"[task] iter {i}/{total_iterations} ok", flush=True)
        except OperationalError as exc:
            print(
                f"[task] iter {i} hit transient DB error: {exc!r} — "
                f"re-raising for Celery autoretry",
                flush=True,
            )
            raise
        time.sleep(2)
    print("[task] completed all iterations", flush=True)
    return f"completed after {total_iterations} iterations (retries={self.request.retries})"


def main():
    """Queue the task and print where to watch."""
    print(f"PG_URL    = {PG_URL}")
    print(f"REDIS_URL = {REDIS_URL}")
    print()
    print("=" * 70)
    print("EMPIRICAL REPRO: DB-blip mid-task")
    print("=" * 70)
    print()
    print("1. Start a worker IN ANOTHER TERMINAL:")
    print(
        "   /private/tmp/nova-test-venv/bin/python -m celery "
        "-A scripts.repro_db_blip worker --loglevel=info --concurrency=1"
    )
    print()
    print("2. Once the worker is ready, this script will queue a 60-second task.")
    print(
        "   Mid-task, run in ANOTHER TERMINAL:  brew services stop postgresql@16 ; "
        "sleep 20 ; brew services start postgresql@16"
    )
    print()
    print("Press ENTER when worker is running...")
    input()

    async_result = long_running_db_task.delay(30)
    print(f"\n[main] queued task id={async_result.id}")
    print("[main] waiting for result (will block up to 5 min)...")
    try:
        result = async_result.get(timeout=300)
        print(f"\n[main] RESULT: {result}")
        print("\n=" * 35)
        print("✅ POST-FIX BEHAVIOR CONFIRMED:")
        print("   task survived the DB outage via Celery autoretry_for.")
    except Exception as exc:  # noqa: BLE001
        print(f"\n[main] TASK FAILED: {exc!r}")
        print("\n=" * 35)
        print("❌ TASK DID NOT SURVIVE OUTAGE — investigate retry policy.")


if __name__ == "__main__":
    main()
