#!/usr/bin/env python
"""Run or plan the camera-roll voice eval (tests/evals/test_camera_roll_voice_evals.py).

  --dry-run            Print the plan and a cost estimate. Calls nothing. Safe anywhere.
  --live --label X     Run the CURRENT agent on the Cunda fixture and save
                       tests/evals/baselines/camera_roll_voice/X.json. PAID: needs an operator's
                       approval, so --approve-reservation and --max-cost-usd (<= $2) are required.
  --compare A.json B.json
                       Print B minus A for every structural rate and judge dimension.

Examples (from src/apps/api):
  .venv/bin/python scripts/run_camera_roll_voice_eval.py --dry-run
  .venv/bin/python scripts/run_camera_roll_voice_eval.py --live --label baseline-2026-10-28 \
      --runs 3 --with-judge --max-cost-usd 1 --approve-reservation
  .venv/bin/python scripts/run_camera_roll_voice_eval.py --compare tests/evals/baselines/\
camera_roll_voice/baseline.json tests/evals/baselines/camera_roll_voice/new-prompt.json

Cost is shown for the agent (Gemini Flash, reservation-style upper bound) and the judge
(Claude Sonnet, approximate list price). Judge prices are constants below; check them.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

API_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(API_ROOT))
os.chdir(API_ROOT)
CALLER_ENV = dict(os.environ)
# Importing app.config needs these set; the plan only renders prompts, it never connects.
for _key, _value in {
    "STORAGE_BUCKET": "nova-test",
    "DATABASE_URL": "postgresql://postgres:postgres@localhost:5432/nova_test",
}.items():
    os.environ.setdefault(_key, _value)

TEST_FILE = "tests/evals/test_camera_roll_voice_evals.py"
HARD_CAP_USD = 2.0
JUDGE_USD_PER_MTOK_IN = 3.0
JUDGE_USD_PER_MTOK_OUT = 15.0
JUDGE_OUT_TOKENS = 400
AGENT_EXPECTED_OUT_TOKENS = 1500


def plan(variant_names: list[str], runs: int, with_judge: bool) -> dict:
    from app.agents.camera_roll_stories import CameraRollStoriesAgent
    from app.services.ai_cost_control import estimate_call_cost_usd
    from tests.evals.camera_roll_voice_fixture import VARIANTS
    from tests.evals.runners.camera_roll_voice import judge_view

    spec = CameraRollStoriesAgent.spec
    agent = CameraRollStoriesAgent(None)
    rubric_chars = len((API_ROOT / "tests/evals/rubrics/camera_roll_stories.md").read_text())
    available = {v.name: v for v in VARIANTS}
    chosen = [available[n] for n in variant_names or available if n in available]
    rows, upper, expected = [], 0.0, 0.0
    for v in chosen:
        value = v.build()
        prompt = agent.render_prompt(value)
        kwargs = dict(model=spec.model, prompt=prompt, media_mime=None)
        ceiling = estimate_call_cost_usd(max_output_tokens=agent.max_output_tokens, **kwargs)
        typical = estimate_call_cost_usd(max_output_tokens=AGENT_EXPECTED_OUT_TOKENS, **kwargs)
        judge_in = (rubric_chars + len(json.dumps(judge_view(value.model_dump())))) / 3
        judge_cost = (
            judge_in * JUDGE_USD_PER_MTOK_IN + JUDGE_OUT_TOKENS * JUDGE_USD_PER_MTOK_OUT
        ) / 1e6
        per_run_ceiling = ceiling + (judge_cost * 3 if with_judge else 0)
        per_run_typical = typical + (judge_cost if with_judge else 0)
        upper += per_run_ceiling * runs
        expected += per_run_typical * runs
        rows.append(
            {
                "variant": v.name,
                "query": v.query,
                "event_fields": v.event,
                "prompt_chars": len(prompt),
                "agent_usd_ceiling_per_run": round(ceiling, 4),
                "agent_usd_typical_per_run": round(typical, 4),
                "judge_usd_per_run": round(judge_cost, 4) if with_judge else 0,
            }
        )
    return {
        "agent": spec.name,
        "model": spec.model,
        "prompt_version": spec.prompt_version,
        "runs_per_variant": runs,
        "with_judge": with_judge,
        "variants": rows,
        "total_calls": len(rows) * runs * (2 if with_judge else 1),
        "estimated_usd_typical": round(expected, 3),
        "estimated_usd_ceiling": round(upper, 3),
        "hard_cap_usd": HARD_CAP_USD,
    }


def live_command(args: argparse.Namespace) -> tuple[list[str], dict[str, str]]:
    cmd = [
        sys.executable,
        "-m",
        "pytest",
        TEST_FILE,
        "-k",
        "live_baseline",
        "-v",
        "-s",
        "--eval-mode=live",
        "--usage-purpose=live_eval",
        f"--test-run-id=camera-roll-voice-{args.label}",
        f"--max-cost-usd={args.max_cost_usd}",
        "--approve-reservation",
    ]
    if args.with_judge:
        cmd.append("--with-judge")
    env = {
        **CALLER_ENV,
        "AI_COST_CONTROL_ENABLED": "true",
        "AI_USAGE_ENVIRONMENT": "development",
        "NOVA_VOICE_EVAL_LABEL": args.label,
        "NOVA_VOICE_EVAL_RUNS": str(args.runs),
        "NOVA_VOICE_EVAL_VARIANTS": ",".join(args.variants),
    }
    return cmd, env


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--compare", nargs=2, metavar=("BEFORE", "AFTER"))
    parser.add_argument("--label")
    parser.add_argument("--runs", type=int, default=1)
    parser.add_argument("--variants", nargs="*", default=[], help="bare query event")
    parser.add_argument("--with-judge", action="store_true")
    parser.add_argument("--max-cost-usd", type=float)
    parser.add_argument("--approve-reservation", action="store_true")
    args = parser.parse_args()

    if args.compare:
        from tests.evals.runners.camera_roll_voice import compare

        before, after = (json.loads(Path(p).read_text()) for p in args.compare)
        print(json.dumps(compare(before, after), indent=2))
        return 0

    estimate = plan(args.variants, args.runs, args.with_judge)
    print(json.dumps(estimate, indent=2))
    if args.dry_run or not args.live:
        print("\nDRY RUN: nothing was called. Add --live (with approval flags) to spend money.")
        return 0

    problems = []
    if not args.label:
        problems.append("--label is required (names the baseline file)")
    if not args.approve_reservation:
        problems.append("--approve-reservation is required: this run is paid")
    if args.max_cost_usd is None or not 0 < args.max_cost_usd <= HARD_CAP_USD:
        problems.append(f"--max-cost-usd must be in (0, {HARD_CAP_USD}]")
    elif estimate["estimated_usd_ceiling"] > args.max_cost_usd:
        problems.append(
            f"ceiling ${estimate['estimated_usd_ceiling']} exceeds --max-cost-usd "
            f"${args.max_cost_usd}; reduce --runs/--variants or raise the approved cap"
        )
    if not CALLER_ENV.get("GEMINI_API_KEY"):
        problems.append("GEMINI_API_KEY is not set")
    if args.with_judge and not CALLER_ENV.get("ANTHROPIC_API_KEY"):
        problems.append("--with-judge needs ANTHROPIC_API_KEY")
    if problems:
        print("\nRefusing live run:\n  - " + "\n  - ".join(problems), file=sys.stderr)
        return 2

    cmd, env = live_command(args)
    return subprocess.call(cmd, env=env)


if __name__ == "__main__":
    raise SystemExit(main())
