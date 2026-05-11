"""Core eval orchestration.

A fixture is a JSON file with the shape:

    {
      "agent": "nova.compose.template_recipe",
      "prompt_version": "2026-05-09",
      "input": {...},                       # ClipMetadataInput / TemplateRecipeInput / ...
      "raw_text": "<recorded model response>",
      "output": {...},                      # parsed Output (sanity, also used by judge)
      "meta": {"source": "prod_snapshots", "template_id": "...", ...}
    }

`run_eval` loads a fixture, builds a CassetteModelClient (replay) or live client,
invokes the agent's `Agent.run`, applies structural checks, optionally calls the
judge, and returns an EvalResult.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.agents._runtime import (
    Agent,
    ModelClient,
    ModelInvocation,
    TerminalError,
)

from .llm_judge import JudgeResult, LLMJudge
from .structural import run_structural

EVAL_FIXTURES_ROOT = Path(__file__).parent.parent.parent / "fixtures" / "agent_evals"
RUBRIC_ROOT = Path(__file__).parent.parent / "rubrics"


# ── Fixture loading ──────────────────────────────────────────────────────────


@dataclass
class Fixture:
    path: Path
    agent: str
    prompt_version: str
    input: dict[str, Any]
    raw_text: str
    output: dict[str, Any]
    meta: dict[str, Any]

    @property
    def fixture_id(self) -> str:
        return f"{self.path.parent.name}/{self.path.stem}"


def load_fixture(path: Path) -> Fixture:
    if not path.exists():
        raise FileNotFoundError(f"fixture not found: {path}")
    data = json.loads(path.read_text())
    missing = [k for k in ("agent", "input", "raw_text") if k not in data]
    if missing:
        raise ValueError(f"fixture {path.name} missing fields: {missing}")
    return Fixture(
        path=path,
        agent=data["agent"],
        prompt_version=data.get("prompt_version", ""),
        input=data["input"],
        raw_text=data["raw_text"],
        output=data.get("output", {}),
        meta=data.get("meta", {}),
    )


def discover_fixtures(agent_dir_name: str) -> list[Path]:
    """Return all *.json files under fixtures/agent_evals/{agent_dir_name}/, sorted."""
    base = EVAL_FIXTURES_ROOT / agent_dir_name
    if not base.exists():
        return []
    return sorted(p for p in base.rglob("*.json") if p.is_file())


# ── Cassette client (replay mode) ────────────────────────────────────────────


class CassetteModelClient(ModelClient):
    """Returns a queued raw_text for the next `invoke` call, ignoring the prompt.

    Eval fixtures pin the input the agent will see, so the prompt rendered from
    that input is deterministic. We don't need to match on prompt — we only need
    to replay the recorded response.
    """

    def __init__(self, raw_text: str, *, tokens_in: int = 0, tokens_out: int = 0) -> None:
        self.raw_text = raw_text
        self.tokens_in = tokens_in
        self.tokens_out = tokens_out
        self.invocations = 0

    def invoke(
        self,
        *,
        model: str,
        prompt: str,
        media_uri: str | None = None,
        media_mime: str | None = None,
        response_json: bool = True,
        max_output_tokens: int | None = None,
        timeout_s: float = 30.0,
    ) -> ModelInvocation:
        self.invocations += 1
        if self.invocations > 1:
            raise TerminalError("CassetteModelClient: agent retried more than once during replay")
        return ModelInvocation(
            raw_text=self.raw_text,
            tokens_in=self.tokens_in,
            tokens_out=self.tokens_out,
            raw_response=None,
        )


# ── Result + runner ──────────────────────────────────────────────────────────


@dataclass
class EvalResult:
    fixture_id: str
    agent: str
    prompt_version: str
    structural_failures: list[str] = field(default_factory=list)
    judge: JudgeResult | None = None
    error: str | None = None
    # Shadow-mode results — populated when --shadow-prompts-dir is active.
    # Shadow never gates the test; it only reports a delta. A shadow that
    # fails (raises, structural-fails, or judge-fails) is informational.
    shadow_ran: bool = False
    shadow_structural_failures: list[str] = field(default_factory=list)
    shadow_judge: JudgeResult | None = None
    shadow_error: str | None = None

    @property
    def passed(self) -> bool:
        if self.error is not None:
            return False
        if self.structural_failures:
            return False
        if self.judge is not None and not self.judge.passed:
            return False
        return True

    def summary(self) -> str:
        if self.error:
            return f"ERROR: {self.error}"
        n = len(self.structural_failures)
        bits = [f"structural={'OK' if not n else f'{n} failures'}"]
        if self.judge is not None:
            bits.append(f"judge={self.judge.summary()}")
        if self.shadow_ran:
            bits.append(self._shadow_summary())
        return f"[{'PASS' if self.passed else 'FAIL'}] {' | '.join(bits)}"

    def _shadow_summary(self) -> str:
        if self.shadow_error:
            return f"shadow=ERROR({self.shadow_error[:60]})"
        primary_avg = self.judge.avg if self.judge else None
        shadow_avg = self.shadow_judge.avg if self.shadow_judge else None
        if primary_avg is not None and shadow_avg is not None:
            delta = shadow_avg - primary_avg
            return (
                f"shadow=primary_avg={primary_avg:.2f} shadow_avg={shadow_avg:.2f} Δ={delta:+.2f}"
            )
        n = len(self.shadow_structural_failures)
        return f"shadow=structural={'OK' if not n else f'{n} failures'}"


def _build_agent_class_for(agent_name: str) -> type[Agent]:
    """Map structlog agent name → Agent subclass."""
    if agent_name == "nova.compose.template_recipe":
        from app.agents.template_recipe import TemplateRecipeAgent

        return TemplateRecipeAgent
    if agent_name == "nova.video.clip_metadata":
        from app.agents.clip_metadata import ClipMetadataAgent

        return ClipMetadataAgent
    if agent_name == "nova.compose.creative_direction":
        from app.agents.creative_direction import CreativeDirectionAgent

        return CreativeDirectionAgent
    if agent_name == "nova.audio.transcript":
        from app.agents.transcript import TranscriptAgent

        return TranscriptAgent
    if agent_name == "nova.compose.platform_copy":
        from app.agents.platform_copy import PlatformCopyAgent

        return PlatformCopyAgent
    if agent_name == "nova.audio.template_recipe":
        from app.agents.audio_template import AudioTemplateAgent

        return AudioTemplateAgent
    raise ValueError(f"no Agent class registered for {agent_name!r}")


@contextmanager
def _shadow_prompts(shadow_dir: Path) -> Iterator[None]:
    """Temporarily overlay prompts: candidate-dir files take precedence,
    everything else falls through to prod `prompts/`.

    Implemented by monkey-patching `prompt_loader._get_raw` so the agent's own
    `render_prompt` keeps working unchanged. Cache is cleared on enter and exit
    to avoid leaking shadow content into subsequent calls.

    NOT thread-safe — mutates module-level state. Eval runs are sequential
    within a pytest worker; cross-worker isolation is provided by pytest-xdist's
    process-per-worker model. Do not use this from concurrent threads.
    """
    from app.pipeline import prompt_loader

    original_get_raw = prompt_loader._get_raw
    original_cache = prompt_loader._cache.copy()
    prompt_loader._cache.clear()

    def patched_get_raw(name: str) -> str:
        candidate = shadow_dir / f"{name}.txt"
        if candidate.exists():
            return candidate.read_text(encoding="utf-8")
        return original_get_raw(name)

    prompt_loader._get_raw = patched_get_raw  # type: ignore[assignment]
    try:
        yield
    finally:
        prompt_loader._get_raw = original_get_raw  # type: ignore[assignment]
        prompt_loader._cache.clear()
        prompt_loader._cache.update(original_cache)


def run_eval(
    fixture: Fixture,
    *,
    model_client: ModelClient | None = None,
    judge: LLMJudge | None = None,
    rubric_dir: Path = RUBRIC_ROOT,
    shadow_prompts_dir: Path | None = None,
) -> EvalResult:
    """Run one fixture end-to-end.

    - If `model_client` is None, builds a CassetteModelClient from `fixture.raw_text`.
    - If `judge` is None, judge step is skipped (structural-only).
    - If `shadow_prompts_dir` is set AND `model_client` is live, runs the agent a
      second time with prompts from the candidate dir overlaid on prod prompts.
      Shadow result is informational; never gates `passed`.
    """
    agent_cls = _build_agent_class_for(fixture.agent)
    client = model_client or CassetteModelClient(fixture.raw_text)
    agent = agent_cls(client)

    try:
        output = agent.run(fixture.input)
    except Exception as exc:
        return EvalResult(
            fixture_id=fixture.fixture_id,
            agent=fixture.agent,
            prompt_version=fixture.prompt_version,
            error=f"agent.run failed: {exc}",
        )

    validated_input = agent.Input.model_validate(fixture.input)
    structural_failures = run_structural(fixture.agent, output, validated_input)

    judge_result: JudgeResult | None = None
    if judge is not None and not structural_failures:
        try:
            judge_result = judge.score(
                agent_name=fixture.agent,
                agent_input=fixture.input,
                agent_output=output.model_dump(),
            )
        except Exception as exc:  # noqa: BLE001 — surface judge errors per fixture
            return EvalResult(
                fixture_id=fixture.fixture_id,
                agent=fixture.agent,
                prompt_version=fixture.prompt_version,
                structural_failures=structural_failures,
                error=f"judge failed: {exc}",
            )

    result = EvalResult(
        fixture_id=fixture.fixture_id,
        agent=fixture.agent,
        prompt_version=fixture.prompt_version,
        structural_failures=structural_failures,
        judge=judge_result,
    )

    if shadow_prompts_dir is not None and model_client is not None:
        # Shadow only runs when the cassette is bypassed (live). With cassette
        # replay, the recorded raw_text was produced by the prod prompt; pairing
        # it with a candidate prompt would compare apples to oranges.
        result.shadow_ran = True
        try:
            with _shadow_prompts(shadow_prompts_dir):
                shadow_agent = agent_cls(model_client)
                shadow_output = shadow_agent.run(fixture.input)
            result.shadow_structural_failures = run_structural(
                fixture.agent, shadow_output, validated_input
            )
            if judge is not None and not result.shadow_structural_failures:
                result.shadow_judge = judge.score(
                    agent_name=fixture.agent,
                    agent_input=fixture.input,
                    agent_output=shadow_output.model_dump(),
                )
        except Exception as exc:  # noqa: BLE001 — shadow must never break the test
            result.shadow_error = str(exc)

    return result


# Explicit overrides for agent names whose `rsplit('.', 1)[-1]` would collide.
# `nova.audio.template_recipe` (audio_template) collides with
# `nova.compose.template_recipe`; route audio to its own rubric file.
_RUBRIC_FILENAME_OVERRIDES: dict[str, str] = {
    "nova.audio.template_recipe": "audio_template",
}


# ── Cost preflight for live-mode runs ────────────────────────────────────────
# Token estimation is intentionally pessimistic: bias toward overestimating cost
# so the cap fires before a real run blows past it.
_INPUT_CHARS_PER_TOKEN = 3
_ASSUMED_OUTPUT_TOKENS = 1500


def estimate_live_cost(
    fixtures: list[Fixture],
) -> tuple[dict[str, tuple[float, int]], float]:
    """Estimate the dollar cost of a live-mode run for the given fixtures.

    Returns a tuple of:
      - per-agent breakdown: {agent_name: (cost_usd, fixture_count)}
      - total cost across all fixtures

    Heuristic: input tokens ≈ chars/3 of (prompt + serialized input);
    output tokens fixed at ASSUMED_OUTPUT_TOKENS. Both deliberately
    pessimistic.
    """
    breakdown: dict[str, tuple[float, int]] = {}
    total = 0.0
    warned_zero_cost: set[str] = set()
    for fixture in fixtures:
        try:
            agent_cls = _build_agent_class_for(fixture.agent)
        except ValueError:
            continue
        spec = agent_cls.spec
        # Guard: zero cost spec means the cap is theater for this agent. Warn
        # so misconfigured agents don't silently bypass the gate.
        if (
            spec.cost_per_1k_input_usd == 0
            and spec.cost_per_1k_output_usd == 0
            and fixture.agent not in warned_zero_cost
        ):
            print(
                f"WARNING: agent {fixture.agent} has zero cost spec — "
                "preflight cap will not gate this agent's fixtures."
            )
            warned_zero_cost.add(fixture.agent)
        input_chars = len(json.dumps(fixture.input, default=str))
        # Rough prompt size guess: agents we care about all have prompts in the
        # 1–8KB range. We assume 4KB as a fixed overhead to avoid loading the
        # actual prompt template here (which could trigger filesystem reads
        # during pytest collection).
        prompt_chars = 4000
        input_tokens = (input_chars + prompt_chars) / _INPUT_CHARS_PER_TOKEN
        cost = (input_tokens / 1000.0) * spec.cost_per_1k_input_usd + (
            _ASSUMED_OUTPUT_TOKENS / 1000.0
        ) * spec.cost_per_1k_output_usd
        prev_cost, prev_n = breakdown.get(fixture.agent, (0.0, 0))
        breakdown[fixture.agent] = (prev_cost + cost, prev_n + 1)
        total += cost
    return breakdown, total


def rubric_path_for(agent_name: str, rubric_dir: Path = RUBRIC_ROOT) -> Path:
    """Map agent_name → rubric markdown path."""
    if agent_name in _RUBRIC_FILENAME_OVERRIDES:
        return rubric_dir / f"{_RUBRIC_FILENAME_OVERRIDES[agent_name]}.md"
    short = agent_name.rsplit(".", 1)[-1]
    return rubric_dir / f"{short}.md"
