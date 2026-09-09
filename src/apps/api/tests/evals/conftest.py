"""Shared pytest fixtures + CLI options for the agent-eval suite.

Modes (selected via CLI flags + env):

  Default                           — replay-mode, structural-only, no network.
  --with-judge                      — adds Claude-Sonnet judge call (needs ANTHROPIC_API_KEY).
  --eval-mode=live                  — bypass cassettes, hit real Gemini (needs GEMINI_API_KEY).
  --shadow-prompts-dir=<path>       — run candidate prompts alongside prod, log per-fixture delta.
                                       Live-only; replay's recorded raw_text was produced under
                                       the prod prompt, so comparing it against a candidate is
                                       meaningless.
  --usage-purpose=live_eval         — mandatory attribution for paid runs.
  --test-run-id=<id>                — mandatory stable run identifier.
  --max-cost-usd=<amount>           — mandatory approved run cap (maximum $2).
  --approve-reservation             — explicit acknowledgement of paid usage.

Set NOVA_EVAL_MODE=live as an alternative to --eval-mode=live.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from .runners.eval_runner import (
    RUBRIC_ROOT,
    estimate_live_cost,
    load_fixture,
    rubric_path_for,
)
from .runners.llm_judge import LLMJudge

LIVE_COST_CAP_USD = 2.0
WEEKLY_SMOKE_COST_CAP_USD = 0.20


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
        "--usage-purpose",
        action="store",
        default=None,
        choices=["live_eval", "provider_smoke"],
        help="Required attribution for a paid live run.",
    )
    parser.addoption("--test-run-id", action="store", default=None)
    parser.addoption("--max-cost-usd", action="store", type=float, default=None)
    parser.addoption(
        "--approve-reservation",
        action="store_true",
        default=False,
        help="Confirm that the named run may reserve up to --max-cost-usd.",
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
    mode = cli_mode or os.environ.get("NOVA_EVAL_MODE", "replay")
    os.environ["NOVA_EVAL_MODE"] = mode
    return mode


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


_LIVE_TIMEOUT_S = 300
"""Per-test timeout in live mode. Gemini latency for the template_recipe agent
is typically 30-65s/fixture on real reference videos; the global 30s default
in pyproject.toml is for unit tests and would falsely fail every live call.
300s gives generous headroom (still catches infinite loops) without forcing
callers to remember `--timeout=600`."""


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Live-mode pre-flight: lift per-test timeout, then cost-cap check.

    Walks each selected eval fixture, looks up its agent's `AgentSpec`
    cost-per-1k fields, applies a deliberately conservative token heuristic
    (chars/3 for input, fixed 1500 for output), and refuses to run if the sum
    exceeds the explicitly approved cap. There is no bypass.

    Replay mode is free AND fast, so both checks no-op there.
    """
    cli_mode = config.getoption("--eval-mode")
    eval_mode_resolved = cli_mode or os.environ.get("NOVA_EVAL_MODE", "replay")
    if eval_mode_resolved != "live":
        return

    # Bump per-test timeout for every live-eval item. Items already carrying
    # an explicit @pytest.mark.timeout(...) override are left alone.
    timeout_mark = pytest.mark.timeout(_LIVE_TIMEOUT_S)
    for item in items:
        if not item.get_closest_marker("timeout"):
            item.add_marker(timeout_mark)

    usage_purpose = config.getoption("--usage-purpose")
    test_run_id = str(config.getoption("--test-run-id") or "").strip()
    max_cost_usd = config.getoption("--max-cost-usd")
    approved = bool(config.getoption("--approve-reservation"))
    if not usage_purpose or not test_run_id or max_cost_usd is None or not approved:
        pytest.exit(
            "Refusing paid eval: --usage-purpose, --test-run-id, --max-cost-usd, "
            "and --approve-reservation are all required.",
            returncode=2,
        )
    purpose_cap = (
        WEEKLY_SMOKE_COST_CAP_USD if usage_purpose == "provider_smoke" else LIVE_COST_CAP_USD
    )
    if max_cost_usd <= 0 or max_cost_usd > purpose_cap:
        pytest.exit(
            f"Refusing paid eval: --max-cost-usd must be within (0, ${purpose_cap:.2f}] "
            f"for {usage_purpose}.",
            returncode=2,
        )
    os.environ.update(
        {
            "NOVA_EVAL_USAGE_PURPOSE": usage_purpose,
            "NOVA_EVAL_TEST_RUN_ID": test_run_id,
            "NOVA_EVAL_MAX_COST_USD": str(max_cost_usd),
            "NOVA_EVAL_RESERVATION_APPROVED": "true",
        }
    )

    fixture_paths: list[Path] = []
    for item in items:
        callspec = getattr(item, "callspec", None)
        candidate = callspec.params.get("fixture_path") if callspec is not None else None
        if isinstance(candidate, Path):
            fixture_paths.append(candidate)

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
    if total <= max_cost_usd:
        return

    parts = [f"  {name}: ${cost:.2f} ({n} fixtures)" for name, (cost, n) in breakdown.items()]
    pytest.exit(
        "Refusing to run live evals: estimated cost "
        f"${total:.2f} > ${max_cost_usd:.2f} approved run cap.\n"
        + "\n".join(parts)
        + "\n\nSelect fewer fixtures or approve a separate run; "
        "the $2 hard cap cannot be bypassed.",
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
