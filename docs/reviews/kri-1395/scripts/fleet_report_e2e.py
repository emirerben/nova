"""KRI-1395 end to end: metered OpenAI calls reach the ledger, and the real endpoint reports them.

Run from src/apps/api against a migrated scratch database:

    eval "$(bash scripts/dev/test-db.sh --export)"
    cd src/apps/api && PYTHONPATH=. .venv/bin/python \
        ../../../docs/reviews/kri-1395/scripts/fleet_report_e2e.py

The real ``openai`` SDK talks to a local HTTP server standing in for api.openai.com (success,
503, 429 and a dropped connection). The ledger, the caption and Whisper code, ``persist_agent_run``
and ``GET /admin/agents/report`` all run for real on PostgreSQL. Exits non-zero on any failed
assertion and writes docs/reviews/kri-1395/fleet-report-e2e.json.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
import uuid
import wave
from pathlib import Path

for name, value in {
    "STORAGE_BUCKET": "nova-e2e",
    "STORAGE_PROVIDER": "gcs",
    "REDIS_URL": "redis://localhost:6379",
    "TOKEN_ENCRYPTION_KEY": "e2e-key-not-used",
    "ALLOWED_ORIGINS": '["http://localhost:3000"]',
    "INTERNAL_API_KEY": "e2e-internal",
}.items():
    os.environ.setdefault(name, value)
os.environ.update(
    AI_COST_CONTROL_ENABLED="true",
    AI_USAGE_ENVIRONMENT="production",
    AI_OPENAI_MONTHLY_BUDGET_USD="100",
    AI_PRODUCTION_MONTHLY_BUDGET_USD="100",
    ADMIN_API_KEY="e2e-token",
    OPENAI_API_KEY="sk-e2e",
)
sys.path.insert(0, str(Path.cwd() / "tests"))
sys.path.insert(0, str(Path.cwd()))

from fastapi.testclient import TestClient  # noqa: E402
from services.fake_openai_server import FakeOpenAI  # noqa: E402
from sqlalchemy import text  # noqa: E402

from app.agents._persistence import persist_agent_run  # noqa: E402
from app.config import settings  # noqa: E402
from app.database import sync_engine, sync_session  # noqa: E402
from app.main import app  # noqa: E402
from app.models import Job, User  # noqa: E402
from app.pipeline.caption_correct import correct_caption_cues  # noqa: E402
from app.pipeline.caption_translate import ensure_caption_language  # noqa: E402
from app.services.whisper_lyrics import transcribe_for_lyrics  # noqa: E402

REPO = Path(__file__).resolve().parents[4]
OUT = REPO / "docs/reviews/kri-1395/fleet-report-e2e.json"
checks: list[dict] = []


def check(name: str, ok: bool, detail: object) -> None:
    checks.append({"check": name, "ok": bool(ok), "detail": detail})
    print(("PASS " if ok else "FAIL ") + name, "" if ok else f"-> {detail}")


def ledger_rows(since: float) -> list[dict]:
    with sync_engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT feature, status, requested_model, resolved_model, estimated_cost_usd, "
                "settled_cost_usd, creator_id IS NOT NULL AS attributed "
                "FROM ai_cost_reservations WHERE created_at >= to_timestamp(:t) "
                "ORDER BY created_at"
            ),
            {"t": since},
        ).mappings()
        return [
            {k: (float(v) if k.endswith("usd") and v is not None else v) for k, v in r.items()}
            for r in rows
        ]


def main() -> None:
    started = time.time() - 1
    fake = FakeOpenAI(prompt_tokens=1_000, completion_tokens=200)
    os.environ["OPENAI_BASE_URL"] = fake.url
    tag = uuid.uuid4().hex[:8]
    user_id, job_id = uuid.uuid4(), uuid.uuid4()
    with sync_session() as db:
        db.add(User(id=user_id, email=f"e2e-{tag}@example.test"))
        db.flush()
        db.add(Job(id=job_id, user_id=user_id, status="done", raw_storage_path="e2e/x.mp4"))
        db.commit()
    job = str(job_id)

    cues = [{"text": "fix teh text", "start_s": 0.0, "end_s": 1.0}]
    fixed = correct_caption_cues(cues, "en", job_id=job)
    check("correction landed through the fake provider", fixed[0]["text"] == "fix the text", fixed)

    fake.behaviors.extend([("status", 503)] * 3)
    kept = correct_caption_cues(cues, "en", job_id=job)
    check("a 503 degrades to the uncorrected cues, render not failed", kept == cues, kept)

    fake.behaviors.extend([("status", 429)] * 3)
    kept = correct_caption_cues(cues, "en", job_id=job)
    check("a 429 degrades to the uncorrected cues", kept == cues, kept)

    words = [
        type("W", (), {"text": w, "start_s": i * 0.4, "end_s": i * 0.4 + 0.3, "confidence": 1.0})()
        for i, w in enumerate("la casa es muy grande y bonita hoy".split())
    ]
    ensure_caption_language(words, requested="en", fallback_language="es", enabled=True, job_id=job)

    with tempfile.TemporaryDirectory() as tmp:
        wav = os.path.join(tmp, "song.wav")
        with wave.open(wav, "wb") as out:
            out.setnchannels(1)
            out.setsampwidth(2)
            out.setframerate(8000)
            out.writeframes(b"\0\0" * 8000)
        from app.agents._runtime import RunContext

        transcribe_for_lyrics(
            wav, ctx=RunContext(job_id=f"track:{uuid.uuid4()}", usage_purpose="optional_background")
        )

    settings.ai_openai_monthly_budget_usd = 0.0
    fake.requests.clear()
    refused = correct_caption_cues(cues, "en", job_id=job)
    check(
        "a refused reservation never reaches the provider and the render continues",
        refused == cues and fake.requests == [],
        {"provider_requests": len(fake.requests)},
    )
    settings.ai_openai_monthly_budget_usd = 100.0

    persist_agent_run(
        job_id=f"eval:{tag}",
        segment_idx=None,
        agent_name=f"e2e.unowned.{tag}",
        prompt_version="v1",
        model="gemini-e2e",
        outcome="ok",
        attempts=1,
        tokens_in=1,
        tokens_out=1,
        settled_cost_usd=0.25,
        cost_usd=0.25,
        latency_ms=40,
        input_dict=None,
        output_dict=None,
        raw_text=None,
    )

    rows = ledger_rows(started)

    def by(feature: str, status: str) -> list[dict]:
        return [r for r in rows if r["feature"] == feature and r["status"] == status]

    expected_cost = 1_000 * 2.50e-6 + 200 * 10.00e-6
    settled = by("caption_correction", "settled")
    check(
        "one settled correction row at provider usage",
        len(settled) == 1
        and abs(settled[0]["settled_cost_usd"] - expected_cost) < 1e-9
        and settled[0]["attributed"],
        settled,
    )
    check("the 503 left unknown liability", len(by("caption_correction", "unknown")) == 1, rows)
    check("the 429 was released", len(by("caption_correction", "released")) == 1, rows)
    check("translation reserved and settled", len(by("caption_translation", "settled")) == 1, rows)
    whisper = by("lyrics_whisper", "settled")
    check(
        "whisper booked 125 s at $0.006/min",
        len(whisper) == 1 and abs(whisper[0]["settled_cost_usd"] - 125 / 60 * 0.006) < 1e-9,
        whisper,
    )

    app_client = TestClient(app)
    response = app_client.get(
        "/admin/agents/report?days=14", headers={"X-Admin-Token": "e2e-token"}
    )
    body = response.json()
    check("the report endpoint answers 200", response.status_code == 200, response.status_code)
    ledger = {(r["feature"]): r for r in body["ledger"] if r["provider"] == "openai"}
    cc = ledger["caption_correction"]
    check(
        "report ledger: correction 3 reservations, 1 settled, 1 released, 1 unknown",
        (cc["reservations"], cc["settled"], cc["released"], cc["unknown"]) == (3, 1, 1, 1),
        cc,
    )
    check(
        "report ledger: whisper cost",
        abs(ledger["lyrics_whisper"]["settled_cost_usd"] - 125 / 60 * 0.006) < 1e-6,
        ledger["lyrics_whisper"],
    )
    check(
        "report counts the owner-less run",
        body["unowned_runs"]["by_agent"].get(f"e2e.unowned.{tag}") == 1,
        body["unowned_runs"],
    )
    row = next(r for r in body["agents"] if r["agent_name"] == f"e2e.unowned.{tag}")
    check(
        "report agent row for the owner-less run",
        row["calls"] == 1 and row["settled_cost_usd"] == 0.25,
        row,
    )

    OUT.write_text(
        json.dumps(
            {
                "head_sha": subprocess.check_output(
                    ["git", "-C", str(REPO), "rev-parse", "HEAD"], text=True
                ).strip(),
                "command": "PYTHONPATH=. .venv/bin/python "
                "../../../docs/reviews/kri-1395/scripts/fleet_report_e2e.py",
                "checks": checks,
                "ledger_rows": rows,
                "report": body,
            },
            indent=2,
            default=str,
        )
        + "\n"
    )

    with sync_engine.begin() as conn:
        conn.execute(
            text("DELETE FROM ai_cost_reservations WHERE created_at >= to_timestamp(:t)"),
            {"t": started},
        )
        conn.execute(
            text("DELETE FROM agent_run WHERE agent_name = :a"), {"a": f"e2e.unowned.{tag}"}
        )
        conn.execute(text("DELETE FROM jobs WHERE id = :j"), {"j": job})
        conn.execute(text("DELETE FROM users WHERE id = :u"), {"u": str(user_id)})
    fake.close()
    failed = [c for c in checks if not c["ok"]]
    print(f"{len(checks) - len(failed)}/{len(checks)} checks passed; wrote {OUT}")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
