#!/usr/bin/env python3
"""PAID: live runs of the 29 Ekim job after approval (scorecard checks 7-8).

Drives the real phone worker (`score_29_ekim.run_post_approval`) on the approved T1 job with the
REAL model clients for the two calls after approval (the creation text composer and the phone
font pick), and grades each run with `post_approval_verdict` (shuffle, one-word pieces, no
static sentence, phone text style + device pin, r3 receipt met). Every model call is metered by
the development ledger (`ai_cost_reservations`, manual-QA envelope): set the run id and cap,
point DATABASE_URL at a throwaway migrated database, and never exceed $1 per run id.

    cd src/apps/api
    DATABASE_URL=postgresql://localhost/<throwaway> NOVA_29EKIM_LIVE_LEDGER_APPROVED=<run id> \\
      .venv/bin/python ../../../scripts/kria/capture_29_ekim_post_approval.py \\
      --max-cost-usd 1 --runs tr:pro tr:pro tr:flash en:pro en:flash --out runs.json

`--save-fixture N` writes run N (a passing one) as `29-ekim/live_post_approval.json`, the
replay check 8 reads. No judge; Gemini only.
"""

from __future__ import annotations

import argparse
import copy
import json
import os
import sys
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
API = REPO / "src" / "apps" / "api"

EN_SENTENCE = (
    "Thank you, Atatürk, for this beautiful homeland, for modern opportunities and for our "
    "independence, for everything we have. Happy Republic Day on the 29th of October"
)
EN_MESSAGE = (
    f'Show all content in random order. add this text "{EN_SENTENCE}". '
    "Show each word one after the other"
)


def _env(run_id: str, cap: float) -> None:
    gemini = os.environ.get("GEMINI_API_KEY", "")
    if not gemini:
        for line in (REPO / ".env").read_text().splitlines():
            if line.startswith("GEMINI_API_KEY="):
                gemini = line.split("=", 1)[1].strip().strip('"')
    if not gemini or not os.environ.get("DATABASE_URL"):
        raise SystemExit("needs GEMINI_API_KEY and a throwaway DATABASE_URL")
    sys.path.insert(0, str(HERE))
    sys.path.insert(0, str(API))
    import score_29_ekim as sc

    for key, value in sc._SAFE_ENV.items():
        if key not in {"DATABASE_URL"}:
            os.environ[key] = value
    os.environ.update(
        {
            "GEMINI_API_KEY": gemini,
            "AI_COST_CONTROL_ENABLED": "true",
            "AI_USAGE_ENVIRONMENT": "development",
            "AI_MANUAL_QA_RESERVATION_APPROVED": "true",
            "AI_MANUAL_QA_TEST_RUN_ID": run_id,
            "AI_MANUAL_QA_MAX_COST_USD": str(cap),
        }
    )


def _recorder(real: Any, log: list[dict]) -> Any:
    from app.agents._runtime import ModelClient

    class Recording(ModelClient):
        def invoke(self, **kwargs: Any):  # noqa: ANN202
            out = real.invoke(**kwargs)
            log.append(
                {
                    "model": out.model_used or kwargs.get("model"),
                    "thinking_level": kwargs.get("thinking_level"),
                    "raw_text": out.raw_text,
                    "tokens_in": out.tokens_in,
                    "tokens_out": out.tokens_out,
                }
            )
            return out

    return Recording()


def _spend(run_id: str) -> float:
    from sqlalchemy import text

    from app.database import sync_engine

    with sync_engine.begin() as conn:
        value = conn.execute(
            text(
                # Settled spend, plus the estimate of any call whose outcome is not known.
                "SELECT COALESCE(SUM(CASE WHEN status = 'settled' THEN settled_cost_usd "
                "WHEN status IN ('reserved', 'provider_started', 'unknown') "
                "THEN estimated_cost_usd ELSE 0 END), 0) "
                "FROM ai_cost_reservations WHERE test_run_id = :id"
            ),
            {"id": run_id},
        ).scalar_one()
    return float(value)


def _job_row(job: Any) -> None:
    """The throwaway database's user and job rows the ledger attributes the calls to."""
    from sqlalchemy import text

    from app.database import sync_engine

    with sync_engine.begin() as conn:
        conn.execute(
            text("INSERT INTO users (id, email) VALUES (:id, :email)"),
            {"id": str(job.user_id), "email": f"29ekim-{job.user_id}@example.invalid"},
        )
        conn.execute(
            text("INSERT INTO jobs (id, user_id, raw_storage_path) VALUES (:id, :user, '')"),
            {"id": str(job.id), "user": str(job.user_id)},
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--max-cost-usd", type=float, required=True)
    parser.add_argument("--runs", nargs="+", default=["tr:pro"])
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    run_id = os.environ.get("NOVA_29EKIM_LIVE_LEDGER_APPROVED", "").strip()
    if not run_id or not 0 < args.max_cost_usd <= 1.0:
        raise SystemExit("needs NOVA_29EKIM_LIVE_LEDGER_APPROVED=<run id> and a cap <= $1")
    _env(run_id, args.max_cost_usd)
    from unittest.mock import patch

    import score_29_ekim as sc

    from app.agents._model_client import default_client
    from app.config import settings

    lf = sc.load_live_fixture()
    # The approved T1 replays recorded answers (no provider call): never metered, so a rerun
    # of this script is not refused as the same logical call.
    with sc._prod_flags(), patch.object(settings, "ai_cost_control_enabled", False):
        sc.approved_t1(lf)
    en_brief = copy.deepcopy(lf["prod_brief"])
    for row in en_brief["requirements"]:
        if row["kind"] == "text_sequence":
            row["literal"] = EN_SENTENCE
    results = []
    for index, spec in enumerate(args.runs):
        language, model = spec.split(":")
        before = _spend(run_id)
        if before >= args.max_cost_usd * 0.85:
            print(f"stopping: ${before:.4f} spent of ${args.max_cost_usd}", file=sys.stderr)
            break
        composer_log: list[dict] = []
        typography_log: list[dict] = []
        brief_raw = en_brief if language == "en" else None
        with sc._prod_flags():
            job = sc.post_approval_job(
                lf, brief_raw=brief_raw, message=EN_MESSAGE if language == "en" else None
            )
            _job_row(job)
            seen = sc.run_post_approval(
                job,
                composer_client=_recorder(default_client(), composer_log),
                typography_client=_recorder(default_client(), typography_log),
                force_flash_fallback=model == "flash",
            )
            verdict = sc.post_approval_verdict(lf, seen, brief_raw=brief_raw)
        reply = None
        if composer_log:
            try:
                reply = json.loads(composer_log[-1]["raw_text"])
            except ValueError:
                reply = None
        row = {
            "index": index,
            "spec": spec,
            "pass": verdict["pass"],
            "parts": verdict["parts"],
            "evidence": {
                k: verdict["evidence"][k]
                for k in (
                    "worker_error",
                    "pin_error",
                    "pieces",
                    "words",
                    "title_span_s",
                    "min_piece_s",
                    "static_sentence_rows",
                    "r3_receipt",
                    "slot_order",
                )
            },
            "composer_reply": (reply or {}).get("reply"),
            "composer_ops": [op.get("op") for op in (reply or {}).get("ops") or []],
            "composer": composer_log[-1] if composer_log else None,
            "typography": typography_log[-1] if typography_log else None,
            "spend_usd": round(_spend(run_id) - before, 6),
        }
        results.append(row)
        print(
            json.dumps(
                {k: row[k] for k in ("index", "spec", "pass", "parts", "spend_usd")}
                | {"reply": row["composer_reply"], "err": row["evidence"]["worker_error"]},
                ensure_ascii=False,
            ),
            file=sys.stderr,
        )
    summary = {
        "test_run_id": run_id,
        "settled_usd": round(_spend(run_id), 6),
        "runs": len(results),
        "passed": sum(1 for r in results if r["pass"]),
    }
    args.out.write_text(
        json.dumps({"summary": summary, "results": results}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(summary), file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
