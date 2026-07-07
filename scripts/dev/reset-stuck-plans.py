#!/usr/bin/env python3
"""Reset content plans stuck after a local worker crash (LOCAL DEV ONLY).

After a Celery worker SIGSEGV/OOM, content_plans rows stay in
plan_status='generating' (or activation_status='seeding'/'activating')
forever and the UI spins. The historical fix was a manual SQL UPDATE —
this script is that fix, made safe and repeatable:

  - plans stuck in 'generating' longer than --minutes:
      -> 'ready'  if the plan already has items
      -> 'failed' if it has none (so the UI offers regenerate)
  - plans stuck in 'seeding'/'activating' longer than --minutes -> 'failed'

Refuses to touch a non-localhost DATABASE_URL unless --force is given.

Usage:
    python3 scripts/dev/reset-stuck-plans.py [--minutes 10] [--dry-run]

Re-execs itself with the API venv's python if psycopg2 is missing, and
reads DATABASE_URL from the environment or the repo-root .env.
"""

import argparse
import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]


def _load_database_url() -> str:
    url = os.environ.get("DATABASE_URL")
    if url:
        return url
    env_file = REPO / ".env"
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            line = line.strip()
            if line.startswith("DATABASE_URL="):
                return line.split("=", 1)[1].strip().strip("'\"")
    return "postgresql://postgres:postgres@localhost:5432/nova"


def _ensure_psycopg2() -> None:
    try:
        import psycopg2  # noqa: F401
    except ImportError:
        venv_py = REPO / "src/apps/api/.venv/bin/python"
        if venv_py.exists() and Path(sys.executable) != venv_py:
            os.execv(str(venv_py), [str(venv_py), *sys.argv])
        sys.exit("psycopg2 not available and no API venv found — run from src/apps/api/.venv")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--minutes", type=int, default=10, help="stuck threshold (default 10)")
    parser.add_argument("--dry-run", action="store_true", help="show what would change")
    parser.add_argument("--force", action="store_true", help="allow non-localhost DATABASE_URL")
    args = parser.parse_args()

    _ensure_psycopg2()
    import psycopg2

    url = _load_database_url()
    host = url.split("@")[-1].split("/")[0].split(":")[0]
    if host not in ("localhost", "127.0.0.1", "db") and not args.force:
        sys.exit(f"refusing to touch non-local DB host '{host}' (use --force if you mean it)")

    conn = psycopg2.connect(url, connect_timeout=5)
    conn.autocommit = False
    cur = conn.cursor()

    cur.execute(
        """
        SELECT p.id, p.plan_status, p.activation_status,
               COALESCE(p.generation_started_at, p.updated_at) AS gen_since,
               COALESCE(p.activation_started_at, p.updated_at) AS act_since,
               (SELECT count(*) FROM plan_items i WHERE i.content_plan_id = p.id) AS item_count
        FROM content_plans p
        WHERE (p.plan_status = 'generating'
               AND COALESCE(p.generation_started_at, p.updated_at) < now() - %s * interval '1 minute')
           OR (p.activation_status IN ('seeding', 'activating')
               AND COALESCE(p.activation_started_at, p.updated_at) < now() - %s * interval '1 minute')
        """,
        (args.minutes, args.minutes),
    )
    rows = cur.fetchall()
    if not rows:
        print(f"no plans stuck longer than {args.minutes} min — nothing to do.")
        return 0

    for plan_id, plan_status, activation_status, gen_since, act_since, item_count in rows:
        if plan_status == "generating":
            new_status = "ready" if item_count else "failed"
            print(
                f"plan {plan_id}: generating since {gen_since:%H:%M:%S} "
                f"({item_count} items) -> plan_status='{new_status}'"
            )
            if not args.dry_run:
                cur.execute(
                    "UPDATE content_plans SET plan_status = %s WHERE id = %s",
                    (new_status, plan_id),
                )
        if activation_status in ("seeding", "activating"):
            print(
                f"plan {plan_id}: {activation_status} since {act_since:%H:%M:%S} "
                f"-> activation_status='failed'"
            )
            if not args.dry_run:
                cur.execute(
                    "UPDATE content_plans SET activation_status = 'failed' WHERE id = %s",
                    (plan_id,),
                )

    if args.dry_run:
        conn.rollback()
        print(f"dry-run: {len(rows)} plan(s) would be reset.")
    else:
        conn.commit()
        print(f"reset {len(rows)} plan(s).")
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
