"""Shared pytest fixtures + CLI options for the agent-eval suite.

Modes (selected via CLI flags + env):

  Default                           — replay-mode, structural-only, no network.
  --with-judge                      — adds Claude-Sonnet judge call (needs ANTHROPIC_API_KEY).
  --eval-mode=live                  — bypass cassettes, hit real Gemini (needs GEMINI_API_KEY).
  --shadow-prompts-dir=<path>       — run candidate prompts alongside prod, log per-fixture delta.
                                       Live-only; replay's recorded raw_text was produced under
                                       the prod prompt, so comparing it against a candidate is
                                       meaningless.
  --allow-cost                      — bypass the $20 live-mode cost cap.

Set NOVA_EVAL_MODE=live as an alternative to --eval-mode=live.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from .runners.eval_runner import (
    RUBRIC_ROOT,
    discover_fixtures,
    estimate_live_cost,
    load_fixture,
    rubric_path_for,
)
from .runners.llm_judge import LLMJudge

LIVE_COST_CAP_USD = 20.0


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--with-judge",
        action="store_true",
        default=False,
        help="Run LLM-as-judge scoring (needs ANTHROPIC_API_KEY).",
    )
    parser.addoption(
        "--shadow",
        action="store_true",
        default=False,
        help="(deprecated) — use --shadow-prompts-dir=<path> instead.",
    )
    parser.addoption(
        "--shadow-prompts-dir",
        action="store",
        default=None,
        help=(
            "Path to a directory of candidate prompt files to overlay on prod "
            "prompts/ for shadow runs. Live mode only."
        ),
    )
    parser.addoption(
        "--allow-cost",
        action="store_true",
        default=False,
        help=f"Bypass the ${LIVE_COST_CAP_USD:.0f} live-mode cost cap.",
    )
    parser.addoption(
        "--eval-mode",
        action="store",
        default=None,
        choices=["replay", "live"],
        help="replay (default, uses cassettes) | live (calls real Gemini).",
    )


@pytest.fixture(scope="session")
def eval_mode(request: pytest.FixtureRequest) -> str:
    cli_mode = request.config.getoption("--eval-mode")
    if cli_mode:
        return cli_mode
    return os.environ.get("NOVA_EVAL_MODE", "replay")


@pytest.fixture(scope="session")
def with_judge(request: pytest.FixtureRequest) -> bool:
    return bool(request.config.getoption("--with-judge"))


@pytest.fixture(scope="session")
def shadow_mode(request: pytest.FixtureRequest) -> bool:
    return bool(request.config.getoption("--shadow"))


@pytest.fixture(scope="session")
def shadow_prompts_dir(request: pytest.FixtureRequest, eval_mode: str) -> Path | None:
    raw = request.config.getoption("--shadow-prompts-dir")
    if not raw:
        return None
    if eval_mode != "live":
        pytest.fail(
            "--shadow-prompts-dir requires --eval-mode=live (replay-mode raw_text "
            "was recorded under the prod prompt; comparing against a candidate "
            "prompt with the same recorded response is meaningless)"
        )
    path = Path(raw)
    if not path.is_absolute():
        path = (Path.cwd() / path).resolve()
    if not path.is_dir():
        pytest.fail(f"--shadow-prompts-dir not found or not a directory: {path}")
    return path


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Pre-flight cost check for live-mode runs.

    Walks each collected eval fixture, looks up its agent's `AgentSpec`
    cost-per-1k fields, applies a deliberately conservative token heuristic
    (chars/3 for input, fixed 1500 for output), and refuses to run if the sum
    exceeds LIVE_COST_CAP_USD unless --allow-cost is passed.

    Replay mode is free, so the check no-ops there.
    """
    cli_mode = config.getoption("--eval-mode")
    eval_mode_resolved = cli_mode or os.environ.get("NOVA_EVAL_MODE", "replay")
    if eval_mode_resolved != "live":
        return
    if config.getoption("--allow-cost"):
        return

    fixture_paths: list[Path] = []
    for agent_dir in (
        "template_recipe",
        "clip_metadata",
        "creative_direction",
        "transcript",
        "platform_copy",
        "audio_template",
        "clip_router",
        "shot_ranker",
    ):
        fixture_paths.extend(discover_fixtures(agent_dir))

    fixtures = []
    for p in fixture_paths:
        try:
            fixtures.append(load_fixture(p))
        except Exception:  # noqa: BLE001 — preflight must never crash collection
            # Malformed fixture, pydantic ValidationError, encoding error, etc.
            # Skip silently — the per-fixture test will surface the real failure
            # if the fixture is actually selected to run.
            continue

    breakdown, total = estimate_live_cost(fixtures)
    if total <= LIVE_COST_CAP_USD:
        return

    parts = [f"  {name}: ${cost:.2f} ({n} fixtures)" for name, (cost, n) in breakdown.items()]
    pytest.exit(
        "Refusing to run live evals: estimated cost "
        f"${total:.2f} > ${LIVE_COST_CAP_USD:.0f} cap.\n"
        + "\n".join(parts)
        + "\n\nPass --allow-cost to override.",
        returncode=2,
    )


@pytest.fixture(scope="session")
def rubric_root() -> Path:
    return RUBRIC_ROOT


@pytest.fixture(scope="session")
def judge_for(with_judge: bool, rubric_root: Path):
    """Returns a callable: agent_name -> LLMJudge | None.

    Cached per-agent so the rubric is loaded once and the Anthropic client is
    reused across calls (which lets prompt caching kick in for the rubric block).
    """
    if not with_judge:
        return lambda _agent: None

    if not os.environ.get("ANTHROPIC_API_KEY"):
        pytest.skip("--with-judge requires ANTHROPIC_API_KEY")

    cache: dict[str, LLMJudge] = {}

    def _factory(agent_name: str) -> LLMJudge:
        if agent_name not in cache:
            cache[agent_name] = LLMJudge(rubric_path_for(agent_name, rubric_root))
        return cache[agent_name]

    return _factory


@pytest.fixture(scope="session")
def live_model_client(eval_mode: str):
    """Build the production ModelDispatcher for --eval-mode=live, else None."""
    if eval_mode != "live":
        return None
    if not os.environ.get("GEMINI_API_KEY"):
        pytest.skip("--eval-mode=live requires GEMINI_API_KEY")
    from app.agents._model_client import default_client

    return default_client()


@pytest.fixture(scope="session")
def live_input_normalizer(eval_mode: str):
    """Build a fixture-input normalizer for --eval-mode=live, else None.

    The eval suite's fixtures store `input.file_uri` as a bucket-relative
    GCS path (`clips/<id>.mp4`). The Studio `GEMINI_API_KEY` can't resolve
    those — production gets around this by calling `gemini_upload_and_wait`
    in the orchestrator before invoking the agent. This fixture mirrors
    that step for the live eval path: it downloads from GCS and uploads to
    Gemini File API, returning a `files/<id>` URI that the model accepts.

    Returns a callable `(input_dict) -> input_dict` (a closure over a
    session-scoped upload cache), or None when in replay mode.
    """
    if eval_mode != "live":
        return None
    from ._fixture_uploader import build_default_uploader

    uploader = build_default_uploader()
    return uploader.normalize_input
