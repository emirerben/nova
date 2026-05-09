"""Shared agent-test fixtures.

`MockModelClient` is a programmable fake — configure a queue of responses or
exceptions per (model, call) and the runtime's retry/refusal/schema logic can
be exercised deterministically without any network.

`SampleAgent` / `SampleInput` / `SampleOutput` are toy schemas used by the runtime
tests. Real agent tests use their own schemas.
"""

from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass, field
from typing import Any, ClassVar

import pytest
from pydantic import BaseModel, Field

from app.agents._runtime import (
    Agent,
    AgentSpec,
    ModelClient,
    ModelInvocation,
    TerminalError,
    TransientError,
)


# ── Fake response objects (mimic the Gemini SDK shape) ────────────────────────


@dataclass
class FakeFinishReason:
    name: str


@dataclass
class FakeCandidate:
    finish_reason: FakeFinishReason | None = None


@dataclass
class FakeResponse:
    """Stand-in for `genai.GenerateContentResponse`. The runtime only inspects
    `candidates[0].finish_reason.name == 'SAFETY'` for refusal detection.
    """

    candidates: list[FakeCandidate] = field(default_factory=list)


def safety_response() -> FakeResponse:
    return FakeResponse(candidates=[FakeCandidate(finish_reason=FakeFinishReason("SAFETY"))])


# ── Programmable fake client ──────────────────────────────────────────────────


class MockModelClient(ModelClient):
    """Per-model response queue. Each `invoke(model=...)` pops the next item.

    Items can be:
      - `ModelInvocation` → returned directly
      - `Exception` → raised
      - `dict` → wrapped into a JSON ModelInvocation (`raw_text=json.dumps(dict)`)
      - `str` → wrapped into a plain-text ModelInvocation
    """

    def __init__(self) -> None:
        self._queues: dict[str, deque[Any]] = defaultdict(deque)
        self.invocations: list[dict[str, Any]] = []

    def queue(self, model: str, *items: Any) -> None:
        self._queues[model].extend(items)

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
        self.invocations.append(
            {
                "model": model,
                "prompt": prompt,
                "media_uri": media_uri,
                "media_mime": media_mime,
                "response_json": response_json,
                "max_output_tokens": max_output_tokens,
                "timeout_s": timeout_s,
            }
        )
        if not self._queues[model]:
            raise TerminalError(f"mock: no queued response for model {model!r}")
        item = self._queues[model].popleft()
        if isinstance(item, BaseException):
            raise item
        if isinstance(item, ModelInvocation):
            return item
        if isinstance(item, dict):
            import json as _json

            return ModelInvocation(raw_text=_json.dumps(item), tokens_in=10, tokens_out=20)
        if isinstance(item, str):
            return ModelInvocation(raw_text=item, tokens_in=10, tokens_out=20)
        raise AssertionError(f"mock: unrecognized queue item {item!r}")


# ── Toy agent for runtime tests ───────────────────────────────────────────────


class SampleInput(BaseModel):
    topic: str
    file_uri: str | None = None


class SampleOutput(BaseModel):
    answer: str = Field(..., min_length=1)
    score: int = Field(..., ge=0, le=100)


class SampleAgent(Agent[SampleInput, SampleOutput]):
    """Minimal agent for runtime tests. JSON output, requires `answer` + `score`."""

    spec: ClassVar[AgentSpec] = AgentSpec(
        name="test.sample",
        prompt_id="test_sample",
        prompt_version="0",
        model="gemini-2.5-flash",
        fallback_models=("gemini-2.5-pro",),
        max_attempts=3,
        backoff_s=(0.0, 0.0, 0.0),  # no real sleep in tests
        cost_per_1k_input_usd=0.001,
        cost_per_1k_output_usd=0.002,
    )
    Input = SampleInput
    Output = SampleOutput

    def render_prompt(self, input: SampleInput) -> str:  # noqa: A002
        return f"Topic: {input.topic}"

    def parse(self, raw_text: str, input: SampleInput) -> SampleOutput:  # noqa: A002, ARG002
        import json as _json

        data = _json.loads(raw_text)
        return SampleOutput(**data)

    def media_uri(self, input: SampleInput) -> str | None:  # noqa: A002
        return input.file_uri

    def required_fields(self) -> list[str]:
        return ["answer", "score"]


# ── Pytest fixtures ───────────────────────────────────────────────────────────


@pytest.fixture
def mock_client() -> MockModelClient:
    return MockModelClient()


@pytest.fixture
def sample_agent(mock_client: MockModelClient) -> SampleAgent:
    return SampleAgent(mock_client)
