"""Replay one complete Kria turn without credentials or side effects."""

from __future__ import annotations

import argparse
from pathlib import Path

from app.kria.replay import load_fixture, render_readable, replay_fixture, trace_as_json

DEFAULT_FIXTURES = Path(__file__).parents[2] / "tests" / "fixtures" / "kria_turns"


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
    args = parser.parse_args(argv)

    fixture_path = _fixture_path(args.fixture, args.fixtures_root)
    if not fixture_path.is_file():
        parser.error(f"fixture not found: {fixture_path}")
    trace = replay_fixture(load_fixture(fixture_path))
    print(trace_as_json(trace) if args.json else render_readable(trace))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
