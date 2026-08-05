#!/usr/bin/env python3
"""Scan persisted intro hooks for transformation-slop patterns (READ-ONLY).

plans/015: the intro_writer prompt now bans the retrospective transformation /
lesson-learned frame ("the monkey changed my whole marketing perspective"), but
re-renders REUSE the persisted `variants[i]["intro_text"]` without an LLM call —
so hooks written under the old prompt survive forever, and no runtime hook can
see them. This scanner is the measurement tool the plan ships instead of a
runtime warn (eng-review: outside-voice #3 superseded D4):

  - run BEFORE deploying the prompt fix -> prevalence baseline + the legacy
    remediation list (job ids whose persisted intros match the slop class);
  - run AFTER the deploy (new-jobs window) -> the before/after delta that
    serves as the rollout signal.

`sequence_quote` values are scanned REPORT-ONLY to size the follow-up
(TODOS.md "sequence_quote_writer anti-slop pass") — quotes are deliberately
aphorism-like, so a match there is data, not a defect.

Usage:
    python3 scripts/dev/scan_intro_slop.py [--limit N] [--json] [--force]

Read-only (single SELECT). Refuses a non-localhost DATABASE_URL unless --force
is given (mirrors reset-stuck-plans.py; pass --force for the prod baseline run).
Re-execs itself with the API venv's python if psycopg2 is missing, and reads
DATABASE_URL from the environment or the repo-root .env.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "src" / "apps" / "api"))

from app.agents.intro_writer import slop_structural_failures  # noqa: E402


def scan_assembly_plan(job_id: str, plan: object) -> list[dict]:
    """Pure scan of one job's assembly_plan. Tolerant of malformed shapes —
    a bad row yields no findings, never an exception (per-job try/except in
    main() is the second belt)."""
    findings: list[dict] = []
    if not isinstance(plan, dict):
        return findings
    variants = plan.get("variants")
    if not isinstance(variants, list):
        return findings
    for idx, variant in enumerate(variants):
        if not isinstance(variant, dict):
            continue
        for kind in ("intro_text", "sequence_quote"):
            text = variant.get(kind)
            if not isinstance(text, str) or not text.strip():
                continue
            patterns = slop_structural_failures(text)
            if patterns:
                findings.append(
                    {
                        "job_id": str(job_id),
                        "variant_index": idx,
                        "kind": kind,
                        "patterns": patterns,
                        "text": text,
                    }
                )
    return findings


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
    parser.add_argument("--limit", type=int, default=0, help="scan at most N jobs (0 = all)")
    parser.add_argument("--json", action="store_true", help="emit findings as JSON lines")
    parser.add_argument("--force", action="store_true", help="allow non-localhost DATABASE_URL")
    args = parser.parse_args()

    _ensure_psycopg2()
    import psycopg2

    url = _load_database_url()
    host = url.split("@")[-1].split("/")[0].split(":")[0]
    if host not in ("localhost", "127.0.0.1", "db") and not args.force:
        sys.exit(f"refusing non-local DB host '{host}' (read-only, but pass --force to confirm)")

    conn = psycopg2.connect(url, connect_timeout=5)
    conn.set_session(readonly=True)
    cur = conn.cursor()
    sql = (
        "SELECT id, assembly_plan FROM jobs "
        "WHERE assembly_plan IS NOT NULL ORDER BY created_at DESC"
    )
    if args.limit > 0:
        cur.execute(sql + " LIMIT %s", (args.limit,))
    else:
        cur.execute(sql)

    scanned = 0
    findings: list[dict] = []
    for job_id, plan in cur:
        scanned += 1
        try:
            findings.extend(scan_assembly_plan(job_id, plan))
        except Exception as exc:  # noqa: BLE001 — a bad row must not stop the scan
            print(f"warn: job {job_id}: {exc}", file=sys.stderr)

    if args.json:
        for f in findings:
            print(json.dumps(f, ensure_ascii=False))
    else:
        for f in findings:
            print(
                f"job {f['job_id']} variants[{f['variant_index']}].{f['kind']}: "
                f"{f['patterns']} — {f['text']!r}"
            )
    intro_hits = sum(1 for f in findings if f["kind"] == "intro_text")
    quote_hits = len(findings) - intro_hits
    print(
        f"\nscanned {scanned} jobs: {intro_hits} slopped intro_text, "
        f"{quote_hits} sequence_quote matches (report-only)",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
