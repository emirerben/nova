#!/usr/bin/env python3
"""Mode A: run the REAL content_plan_generator agent over a persona bank.

Drives `nova.plan.content_plan_generator` (live Gemini) against each persona in
the bank and writes a plans JSON the grader consumes. This is the reproducible
"is the prompt good?" harness — the controls in the bank are the canary.

Run from src/apps/api with the shared test venv + GEMINI_API_KEY (repo-root .env):

    cd src/apps/api && /Users/.../.venv-test/bin/python \
      ../../../.claude/skills/audit-plan-quality/scripts/generate_plans.py \
      --personas <bank.json> --horizon 14 --out /tmp/plan-audit/plans.json

Output shape: {"mode": "synthetic", "gemini_model": "...", "model_matches_prod": bool,
  "plans": [{"persona_id", "is_control", "persona": {...}, "items": [...], "error": null}]}
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _envload import load_dotenv  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description="Generate content plans for a persona bank.")
    ap.add_argument("--personas", required=True, help="Path to the persona bank JSON.")
    ap.add_argument("--horizon", type=int, default=14, help="Days per plan (default 14).")
    ap.add_argument("--out", required=True, help="Where to write the plans JSON.")
    ap.add_argument("--limit", type=int, default=0, help="Only first N personas (0 = all).")
    args = ap.parse_args()

    # cwd must be src/apps/api so `import app` resolves; load env from repo root.
    sys.path.insert(0, os.getcwd())
    load_dotenv()

    if not os.environ.get("GEMINI_API_KEY"):
        print("ERROR: GEMINI_API_KEY not set (repo-root .env). Mode A needs real Gemini.", file=sys.stderr)
        return 2

    from app.agents._model_client import default_client
    from app.agents._runtime import RunContext
    from app.agents.content_plan_generator import ContentPlanGeneratorAgent

    # Every Gemini call is funnelled through settings.gemini_model (app/config.py),
    # which is overridable per-process by the GEMINI_MODEL env var. PROD does NOT set
    # it, so prod runs the flash default; a local .env that sets GEMINI_MODEL=...-pro
    # means a local audit grades a STRONGER model than real users get and will
    # under-estimate the cheese. Surface the active model so the report can caveat it.
    PROD_DEFAULT_MODEL = "gemini-2.5-flash"
    try:
        from app.config import settings

        active_model = getattr(settings, "gemini_model", None) or PROD_DEFAULT_MODEL
    except Exception:  # noqa: BLE001 — config import shouldn't block a run
        active_model = os.environ.get("GEMINI_MODEL", PROD_DEFAULT_MODEL)
    print(f"active gemini model: {active_model} (prod default: {PROD_DEFAULT_MODEL})", file=sys.stderr)
    if active_model != PROD_DEFAULT_MODEL:
        print(
            f"WARNING: GEMINI_MODEL={active_model} ≠ prod default {PROD_DEFAULT_MODEL}. To grade what "
            f"real users get, re-run with GEMINI_MODEL={PROD_DEFAULT_MODEL} set.",
            file=sys.stderr,
        )

    bank = json.loads(Path(args.personas).read_text())
    personas = bank["personas"] if isinstance(bank, dict) else bank
    if args.limit:
        personas = personas[: args.limit]

    client = default_client()
    agent = ContentPlanGeneratorAgent(client)
    # Keep eval-style traffic out of the prod agent_run table / Langfuse.
    ctx = RunContext(extra={"skip_agent_run_persist": True, "skip_langfuse_trace": True})

    plans = []
    for i, p in enumerate(personas, 1):
        persona_fields = {k: v for k, v in p.items() if k not in ("id", "is_control", "label")}
        pid = p.get("id", f"persona-{i}")
        print(f"[{i}/{len(personas)}] generating plan for {pid} ...", file=sys.stderr)
        record = {
            "persona_id": pid,
            "is_control": bool(p.get("is_control", False)),
            "persona": persona_fields,
            "items": [],
            "error": None,
        }
        try:
            output = agent.run(
                {"persona": persona_fields, "horizon_days": args.horizon},
                ctx=ctx,
            )
            record["items"] = output.model_dump()["items"]
        except Exception as exc:  # noqa: BLE001 — one bad persona must not sink the run
            record["error"] = f"{type(exc).__name__}: {exc}"
            print(f"    FAILED: {record['error']}", file=sys.stderr)
        plans.append(record)

    out = {
        "mode": "synthetic",
        "generated_at": _dt.datetime.now(_dt.timezone.utc).isoformat(),
        "horizon_days": args.horizon,
        "gemini_model": active_model,
        "prod_default_model": PROD_DEFAULT_MODEL,
        "model_matches_prod": active_model == PROD_DEFAULT_MODEL,
        "plans": plans,
    }
    out_path = Path(args.out).resolve()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(out, indent=2, ensure_ascii=False))
    n_items = sum(len(pl["items"]) for pl in plans)
    n_failed = sum(1 for pl in plans if pl["error"])
    print(f"\nWrote {len(plans)} plans ({n_items} items, {n_failed} failed) → {out_path}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
