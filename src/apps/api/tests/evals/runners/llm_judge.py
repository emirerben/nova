"""LLM-as-judge — Claude Sonnet scores agent output against a markdown rubric.

Using Claude (different family from Gemini, the agent under test) gives an
independent quality signal. The rubric is cacheable (`cache_control`) so repeated
calls within the same eval run hit the prompt cache.

Rubrics live at `tests/evals/rubrics/{agent_name}.md` and define dimensions to
score 1-5 each, plus a pass threshold for the average. The judge returns
structured JSON via response_schema-style instruction.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import structlog

log = structlog.get_logger()

JUDGE_MODEL = "claude-sonnet-4-6"
DEFAULT_PASS_THRESHOLD = 3.5
DEFAULT_MAX_TOKENS = 800


@dataclass
class JudgeResult:
    scores: dict[str, float] = field(default_factory=dict)
    avg: float = 0.0
    passed: bool = False
    threshold: float = DEFAULT_PASS_THRESHOLD
    reasoning: str = ""
    raw_response: str = ""

    def summary(self) -> str:
        if not self.scores:
            return "no scores"
        parts = [f"{k}={v:.1f}" for k, v in sorted(self.scores.items())]
        verdict = "PASS" if self.passed else "FAIL"
        return f"avg={self.avg:.2f} ({', '.join(parts)}) — {verdict} (≥{self.threshold})"


class JudgeError(Exception):
    """Raised when the judge call itself fails or returns malformed output."""


class LLMJudge:
    """Stateless judge. Loads rubric from disk lazily; reuses the same client across calls."""

    def __init__(
        self,
        rubric_path: Path | str,
        *,
        client: Any = None,
        model: str = JUDGE_MODEL,
        max_tokens: int = DEFAULT_MAX_TOKENS,
    ) -> None:
        self.rubric_path = Path(rubric_path)
        self.model = model
        self.max_tokens = max_tokens
        self._client = client
        self._rubric_cache: str | None = None
        self._threshold_cache: float | None = None

    @property
    def client(self) -> Any:
        if self._client is None:
            try:
                import anthropic
            except ImportError as exc:
                raise JudgeError(
                    "anthropic SDK not installed — add to dev deps and `pip install -e .[dev]`"
                ) from exc
            self._client = anthropic.Anthropic()
        return self._client

    def _load_rubric(self) -> tuple[str, float]:
        if self._rubric_cache is not None and self._threshold_cache is not None:
            return self._rubric_cache, self._threshold_cache
        if not self.rubric_path.exists():
            raise JudgeError(f"rubric not found: {self.rubric_path}")
        text = self.rubric_path.read_text()
        threshold = DEFAULT_PASS_THRESHOLD
        match = re.search(r"Pass threshold:\s*avg\s*[≥>=]+\s*([0-9.]+)", text)
        if match:
            try:
                threshold = float(match.group(1))
            except ValueError:
                pass
        self._rubric_cache = text
        self._threshold_cache = threshold
        return text, threshold

    def score(
        self,
        *,
        agent_name: str,
        agent_input: dict[str, Any],
        agent_output: dict[str, Any],
    ) -> JudgeResult:
        rubric, threshold = self._load_rubric()

        system_blocks = [
            {
                "type": "text",
                "text": (
                    "You are a strict but fair quality judge for a video-pipeline AI agent. "
                    "Score the output below against the rubric exactly as written. "
                    "Return ONLY a JSON object of the form "
                    '{"scores": {"<dim>": <int 1-5>, ...}, "reasoning": "<one sentence>"}. '
                    "No prose outside the JSON."
                ),
                "cache_control": {"type": "ephemeral"},
            },
            {
                "type": "text",
                "text": f"Agent under test: {agent_name}\n\nRubric:\n\n{rubric}",
                "cache_control": {"type": "ephemeral"},
            },
        ]

        user_text = (
            "Agent input (context):\n"
            f"{json.dumps(agent_input, indent=2, default=str)}\n\n"
            "Agent output (judge this):\n"
            f"{json.dumps(agent_output, indent=2, default=str)}"
        )

        try:
            response = self.client.messages.create(
                model=self.model,
                max_tokens=self.max_tokens,
                system=system_blocks,
                messages=[{"role": "user", "content": user_text}],
            )
        except Exception as exc:
            raise JudgeError(f"judge call failed: {exc}") from exc

        raw_text = _extract_text(response)
        scores, reasoning = _parse_judge_json(raw_text)

        if not scores:
            raise JudgeError(f"judge returned no scores; raw response: {raw_text[:500]!r}")

        avg = sum(scores.values()) / len(scores)
        result = JudgeResult(
            scores=scores,
            avg=avg,
            passed=avg >= threshold,
            threshold=threshold,
            reasoning=reasoning,
            raw_response=raw_text,
        )
        log.info(
            "judge_scored",
            agent=agent_name,
            avg=round(avg, 2),
            passed=result.passed,
            threshold=threshold,
        )
        return result


def _extract_text(response: Any) -> str:
    """Pull the first text block out of an Anthropic Message response."""
    content = getattr(response, "content", None)
    if not content:
        return ""
    for block in content:
        text = getattr(block, "text", None)
        if text:
            return text
        if isinstance(block, dict) and block.get("text"):
            return block["text"]
    return ""


def _parse_judge_json(raw: str) -> tuple[dict[str, float], str]:
    """Tolerant JSON parse: strips ```json fences and trailing prose."""
    text = raw.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```\s*$", "", text)
    match = re.search(r"\{.*\}", text, flags=re.DOTALL)
    if not match:
        raise JudgeError(f"no JSON object in judge response: {raw[:200]!r}")
    try:
        data = json.loads(match.group(0))
    except json.JSONDecodeError as exc:
        raise JudgeError(f"judge response was not valid JSON: {exc}") from exc

    raw_scores = data.get("scores", {})
    if not isinstance(raw_scores, dict):
        raise JudgeError("judge `scores` field is not a dict")
    scores: dict[str, float] = {}
    for k, v in raw_scores.items():
        try:
            scores[str(k)] = float(v)
        except (TypeError, ValueError):
            continue
    reasoning = str(data.get("reasoning", ""))
    return scores, reasoning
