#!/usr/bin/env python3
"""Scorecard for KRI-1563 (prod job fa10c4a4 / thread 1b8974a0, 2026-11-19): the ask ledger.

Each row of ``tests/fixtures/kria_replay/kri1563_ask_ledger/fixture.json`` is one chat edit on
a ready draft. The REAL editor compiler, diff, brief checkers, ask ledger and reply builder run
on the recorded (or reconstructed) requirements and copilot ops; no model, storage, broker or
renderer is touched, and a socket guard fails any network attempt.

    cd src/apps/api && .venv/bin/python ../../../scripts/kria/score_kri1563_ask_ledger.py
    make kria-replay FIXTURE=kri1563_ask_ledger

A row is VERIFIED when every part of the ask has a state (done / declined / needs_choice) with
its proof or reason, every change the turn made is named in the reply, and the row's own
expectations hold (states, receipt statuses, lines). Rows with a ``prod_reply`` also check the
negative control: the reply production actually sent must fail the same ledger check.
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import sys
import traceback
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
API = REPO / "src" / "apps" / "api"

_SAFE_ENV = {
    "STORAGE_BUCKET": "offline-scorecard",
    "STORAGE_PROVIDER": "gcs",
    "DATABASE_URL": "postgresql://localhost:5432/offline",
    "REDIS_URL": "redis://localhost:6379",
    "OPENAI_API_KEY": "offline",
    "GEMINI_API_KEY": "offline-scorecard",
    "TOKEN_ENCRYPTION_KEY": "offline",
    "ALLOWED_ORIGINS": '["http://localhost:3000"]',
}


def _bootstrap() -> None:
    for key, value in _SAFE_ENV.items():
        os.environ.setdefault(key, value)
    if str(API) not in sys.path:
        sys.path.insert(0, str(API))

    def _no_network(*_args, **_kwargs):  # noqa: ANN002, ANN003, ANN202
        raise RuntimeError("the KRI-1563 scorecard is offline: no network")

    socket.create_connection = _no_network  # type: ignore[assignment]


def run_offline() -> dict:
    import pytest

    from tests.kria.kri1563_ledger_cases import check_case, load_cases, run_case

    checks = []
    for index, case in enumerate(load_cases(), start=1):
        why: list[str]
        patcher = pytest.MonkeyPatch()
        try:
            result = run_case(case, patcher)
            problems = check_case(case, result)
            verdict = "VERIFIED" if not problems else "NOT VERIFIED"
            why = problems or [line for line in result.reply.splitlines() if line]
        except Exception as exc:  # noqa: BLE001 - a crashed row is reported, not raised
            verdict, why = "INCONCLUSIVE", [f"{type(exc).__name__}: {exc}"]
            why += traceback.format_exc().splitlines()[-3:]
        finally:
            patcher.undo()
        checks.append(
            {
                "id": index,
                "name": f"{case['id']} ({case['incident']})",
                "verdict": verdict,
                "why": why,
            }
        )
    summary = {
        verdict: sum(check["verdict"] == verdict for check in checks)
        for verdict in ("VERIFIED", "NOT VERIFIED", "INCONCLUSIVE")
    }
    return {"fixture": "kri1563_ask_ledger", "checks": checks, "summary": summary}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", type=Path)
    args = parser.parse_args(argv)
    _bootstrap()
    report = run_offline()
    text = json.dumps(report, ensure_ascii=False, indent=2, default=str)
    if args.out:
        args.out.write_text(text, encoding="utf-8")
    for check in report["checks"]:
        print(f"{check['id']}. {check['verdict']:<13} {check['name']}")
        for line in check["why"]:
            print(f"     - {line}")
    print(json.dumps(report["summary"]))
    return 0 if report["summary"]["VERIFIED"] == len(report["checks"]) else 1


if __name__ == "__main__":
    raise SystemExit(main())
