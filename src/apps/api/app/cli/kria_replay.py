"""Replay one complete Kria turn without credentials or side effects.

A fixture is either a frozen turn (``tests/fixtures/kria_turns/<name>.json``) or a recorded
conversation (``tests/fixtures/kria_replay/<name>/fixture.json``) scored by
``scripts/kria/score_<name>.py``, e.g. ``make kria-replay FIXTURE=29-ekim``.
"""

from __future__ import annotations

import argparse
import importlib.util
from pathlib import Path

from app.kria.replay import load_fixture, render_readable, replay_fixture, trace_as_json

DEFAULT_FIXTURES = Path(__file__).parents[2] / "tests" / "fixtures" / "kria_turns"
CONVERSATION_FIXTURES = Path(__file__).parents[2] / "tests" / "fixtures" / "kria_replay"


def _scorecard_for(value: str) -> Path | None:
    """The scorecard script of a recorded-conversation fixture, when ``value`` names one."""
    if Path(value).exists() and Path(value).suffix == ".json":
        return None
    name = Path(value).name
    if len(Path(__file__).parents) < 6:  # the prod image has no repo checkout or scripts/
        return None
    script = Path(__file__).parents[5] / "scripts" / "kria" / f"score_{name.replace('-', '_')}.py"
    if (CONVERSATION_FIXTURES / name / "fixture.json").is_file() and script.is_file():
        return script
    return None


def _run_scorecard(script: Path, argv: list[str]) -> int:
    spec = importlib.util.spec_from_file_location(script.stem, script)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load scorecard {script}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return int(module.main(argv))


def _fixture_path(value: str, root: Path) -> Path:
    candidate = Path(value)
    if candidate.exists():
        return candidate
    name = value if value.endswith(".json") else f"{value}.json"
    return root / name


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m app.cli.kria_replay",
        description="Replay a frozen Kria turn without model, storage, broker, or render access.",
    )
    parser.add_argument("fixture", help="fixture name or JSON path")
    parser.add_argument("--fixtures-root", type=Path, default=DEFAULT_FIXTURES)
    parser.add_argument("--json", action="store_true", help="emit the complete JSON trace")
    args, extra = parser.parse_known_args(argv)
    scorecard = _scorecard_for(args.fixture)
    if scorecard is not None:
        return _run_scorecard(scorecard, extra)
    if extra:
        parser.error(f"unrecognized arguments: {' '.join(extra)}")

    fixture_path = _fixture_path(args.fixture, args.fixtures_root)
    if not fixture_path.is_file():
        parser.error(f"fixture not found: {fixture_path}")
    trace = replay_fixture(load_fixture(fixture_path))
    print(trace_as_json(trace) if args.json else render_readable(trace))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
