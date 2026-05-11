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
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.agents._runtime import (
    Agent,
    ModelClient,
    ModelInvocation,
    RunContext,
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
        return f"[{'PASS' if self.passed else 'FAIL'}] {' | '.join(bits)}"


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
    raise ValueError(f"no Agent class registered for {agent_name!r}")


def run_eval(
    fixture: Fixture,
    *,
    model_client: ModelClient | None = None,
    judge: LLMJudge | None = None,
    rubric_dir: Path = RUBRIC_ROOT,
) -> EvalResult:
    """Run one fixture end-to-end.

    - If `model_client` is None, builds a CassetteModelClient from `fixture.raw_text`.
    - If `judge` is None, judge step is skipped (structural-only).
    """
    agent_cls = _build_agent_class_for(fixture.agent)
    client = model_client or CassetteModelClient(fixture.raw_text)
    agent = agent_cls(client)
    # Eval runs post their own Langfuse trace (with source:eval) at the end
    # of run_eval. Suppress the inner per-Agent.run() trace so we don't
    # double-post replay-mode evals as if they were prod traffic.
    eval_ctx = RunContext(extra={"skip_langfuse_trace": True})

    try:
        output = agent.run(fixture.input, ctx=eval_ctx)
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

    # Optionally post this eval run to Langfuse so scores trend in the UI
    # alongside prod traces. No-op unless LANGFUSE_PUBLIC_KEY/SECRET_KEY are
    # set + the langfuse SDK is installed. Fails open.
    _post_eval_to_langfuse(fixture, result, output)

    return result


def _post_eval_to_langfuse(
    fixture: Fixture,
    result: EvalResult,
    output: Any,
) -> None:
    """Create a Langfuse trace tagged source:eval and attach structural +
    per-dimension judge scores. Closes the loop between offline evals and
    prod-traffic traces in the same Langfuse project.
    """
    try:
        from app.agents._langfuse import score_trace, trace_agent_run  # noqa: PLC0415
    except ImportError:
        return  # prod code not importable from here for some reason — silent skip

    try:
        output_dump = output.model_dump() if output is not None else None
    except Exception:  # noqa: BLE001
        output_dump = None

    extra_tags = [
        f"fixture:{fixture.path.parent.name}/{fixture.path.stem}",
        "structural_pass" if not result.structural_failures else "structural_fail",
    ]
    trace_id = trace_agent_run(
        agent_name=fixture.agent,
        prompt_version=fixture.prompt_version,
        model="eval",  # not a real model invocation — replay or live happens inside agent.run
        outcome="ok" if result.passed else "fail",
        input_dict=fixture.input,
        output_dict=output_dump,
        job_id=None,
        source="eval",
        extra_tags=extra_tags,
        error=result.error,
    )
    if not trace_id:
        return

    score_trace(
        trace_id,
        name="structural",
        value=0.0 if result.structural_failures else 1.0,
        comment=(
            f"{len(result.structural_failures)} failures: "
            f"{', '.join(result.structural_failures[:3])}"
            if result.structural_failures
            else "passed"
        ),
    )
    if result.judge is not None:
        for dim, value in result.judge.scores.items():
            score_trace(
                trace_id,
                name=f"judge_{dim}",
                value=value,
                comment=result.judge.reasoning,
            )
        score_trace(trace_id, name="judge_avg", value=result.judge.avg)
        score_trace(
            trace_id,
            name="judge_passed",
            value=1.0 if result.judge.passed else 0.0,
            comment=f"threshold={result.judge.threshold}",
        )


def rubric_path_for(agent_name: str, rubric_dir: Path = RUBRIC_ROOT) -> Path:
    """Map agent_name → rubric markdown path."""
    short = agent_name.rsplit(".", 1)[-1]
    return rubric_dir / f"{short}.md"
