"""Stamp eval fixtures with the prompt fingerprint the agent sends today.

A fixture's `prompt_sha` says which prompt its recorded answer belongs to. Replay
fails a fixture whose agent now sends a different prompt (tests/evals/runners/
eval_runner.py::_stale_prompt_error). Live captures stamp themselves. This script
is for fixtures that were never captured live (hand-written, exported) and for a
prompt change you have judged cannot change the recorded answer.

    python scripts/stamp_eval_fixtures.py --agent music_matcher
    python scripts/stamp_eval_fixtures.py --baseline --except music_matcher,text_designer
    python scripts/stamp_eval_fixtures.py --check                  # list, write nothing

`--baseline` is the one-time backfill. A fixture whose `prompt_version` equals the
agent's current one is stamped (it trusts the hand-set version once). Any other
fixture is marked "unpinned": it replays a historical answer through the parser and
compiler and is not checked against the prompt. Unpinned fixtures are listed in
tests/fixtures/agent_evals/unpinned.json, which may only shrink. `--except` leaves
whole agents untouched, so they fail replay until re-recorded.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

API_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(API_ROOT))

# Settings() needs these at import; they mirror tests/conftest.py. No call leaves the machine.
for _key, _value in {
    "STORAGE_BUCKET": "nova-test",
    "STORAGE_PROVIDER": "gcs",
    "DATABASE_URL": "postgresql://postgres:postgres@localhost:5432/nova_test",
    "REDIS_URL": "redis://localhost:6379",
    "OPENAI_API_KEY": "sk-test",
    "GEMINI_API_KEY": "ci-noop",
    "ANTHROPIC_API_KEY": "test",
}.items():
    os.environ.setdefault(_key, _value)

from tests.evals.runners.eval_runner import (  # noqa: E402
    EVAL_FIXTURES_ROOT,
    UNPINNED,
    UNPINNED_LIST,
    _build_agent_class_for,
    current_prompt_sha,
    load_fixture,
)


def _fixtures(agent_dir: str | None) -> list[Path]:
    roots = [EVAL_FIXTURES_ROOT / agent_dir] if agent_dir else sorted(EVAL_FIXTURES_ROOT.iterdir())
    return sorted(p for root in roots if root.is_dir() for p in root.rglob("*.json"))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--agent", help="fixture directory name, e.g. music_matcher")
    group.add_argument("--baseline", action="store_true")
    group.add_argument("--check", action="store_true", help="report only, write nothing")
    parser.add_argument("--except", dest="except_agents", default="", help="comma-separated dirs")
    args = parser.parse_args()
    skip = {name for name in args.except_agents.split(",") if name}

    counts = {"stamped": 0, "unpinned": 0, "current": 0, "left": 0, "broken": 0, "other": 0}
    unpinned_paths: list[str] = []
    for path in _fixtures(args.agent):
        label = path.relative_to(EVAL_FIXTURES_ROOT).as_posix()
        if label.split("/")[0] in skip:
            counts["left"] += 1
            continue
        try:
            fixture = load_fixture(path)
        except ValueError:
            counts["other"] += 1  # another harness's schema (suggestion chips, ...)
            continue
        try:
            current = current_prompt_sha(fixture)
        except Exception as exc:  # noqa: BLE001
            counts["broken"] += 1
            print(f"BROKEN   {label}: {exc}")
            continue
        if current is None:
            counts["broken"] += 1
            print(f"NO-PROMPT {label}: the agent never reached the model")
            continue
        new = current
        if args.baseline:
            version = _build_agent_class_for(fixture.agent).spec.prompt_version
            if fixture.prompt_version != version:
                new = UNPINNED
        if fixture.prompt_sha == new:
            counts["current"] += 1
            if new == UNPINNED:
                unpinned_paths.append(label)
            continue
        if args.check:
            counts["left"] += 1
            print(f"DIFFERS  {label}: {fixture.prompt_sha or 'none'} -> {new}")
            continue
        data = json.loads(path.read_text())
        data["prompt_sha"] = new
        path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n")
        if new == UNPINNED:
            counts["unpinned"] += 1
            unpinned_paths.append(label)
        else:
            counts["stamped"] += 1

    if args.baseline:
        UNPINNED_LIST.write_text(json.dumps(sorted(unpinned_paths), indent=2) + "\n")
    print(" ".join(f"{key}={value}" for key, value in counts.items()))
    return 1 if counts["broken"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
