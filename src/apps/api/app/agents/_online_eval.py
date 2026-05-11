"""Optional online evaluation: sample a fraction of successful prod Agent.run()
calls, score them with the LLM judge, and post per-dimension scores back to
the Langfuse trace.

Gated by FOUR independent things — all must be true for online eval to fire:
  1. The Langfuse trace was actually posted (trace_id is set)
  2. NOVA_ONLINE_EVAL_SAMPLE_RATE > 0 (float in [0, 1], default 0)
  3. ANTHROPIC_API_KEY is set (judge runs Claude Sonnet)
  4. The agent has a rubric markdown at tests/evals/rubrics/<short>.md

If any of these is false, online eval is a no-op. Failure within the judge or
the Langfuse client never breaks prod work (it runs in a Celery task).

Cost discipline: at NOVA_ONLINE_EVAL_SAMPLE_RATE=0.05 (5%), and ~6 agents per
job at ~50 jobs/day, expect ~15 judge calls/day @ ~$0.01 = ~$5/month.
"""

from __future__ import annotations

import os
import random
import re
from pathlib import Path
from typing import Any

import structlog

log = structlog.get_logger()

JUDGE_MODEL = "claude-sonnet-4-6"
JUDGE_MAX_TOKENS = 800
DEFAULT_PASS_THRESHOLD = 3.5

# Rubrics live in the eval harness tree but they are pure markdown — safe to
# read from prod code. Resolved at module load to avoid filesystem walks on the
# hot path.
_RUBRICS_ROOT = Path(__file__).resolve().parent.parent.parent / "tests" / "evals" / "rubrics"

# Agents whose structlog name collides on rsplit; mirror the override map in
# tests/evals/runners/eval_runner.py so online evals find the right rubric.
_RUBRIC_FILENAME_OVERRIDES: dict[str, str] = {
    "nova.audio.template_recipe": "audio_template",
}


def _rubric_path_for(agent_name: str) -> Path:
    if agent_name in _RUBRIC_FILENAME_OVERRIDES:
        return _RUBRICS_ROOT / f"{_RUBRIC_FILENAME_OVERRIDES[agent_name]}.md"
    short = agent_name.rsplit(".", 1)[-1]
    return _RUBRICS_ROOT / f"{short}.md"


def _sample_rate() -> float:
    raw = os.environ.get("NOVA_ONLINE_EVAL_SAMPLE_RATE", "0")
    try:
        rate = float(raw)
    except (TypeError, ValueError):
        return 0.0
    return max(0.0, min(1.0, rate))


def _should_sample() -> bool:
    rate = _sample_rate()
    if rate <= 0:
        return False
    return random.random() < rate


def maybe_schedule_judge(
    *,
    trace_id: str,
    agent_name: str,
    input_dict: dict,
    output_dict: dict,
) -> None:
    """Conditionally dispatch the judge Celery task. Fails open.

    Sampling happens HERE (in the request thread, cheap) so we don't pay the
    Celery dispatch overhead for the 95% of traces we won't score.
    """
    if not _should_sample():
        return
    if not _rubric_path_for(agent_name).exists():
        # Agent has no rubric — nothing to score against. Silent skip.
        return
    if not os.environ.get("ANTHROPIC_API_KEY"):
        # Judge can't run without an API key. Silent skip.
        return
    try:
        # Lazy import so the rest of the app doesn't pay for Celery/Anthropic
        # at import time. If the task module fails to import, we no-op.
        from app.tasks.online_eval import score_trace_async  # noqa: PLC0415

        score_trace_async.delay(
            trace_id=trace_id,
            agent_name=agent_name,
            input_dict=input_dict,
            output_dict=output_dict,
        )
    except Exception as exc:  # noqa: BLE001
        log.debug(
            "online_eval_schedule_failed", agent=agent_name, trace_id=trace_id, error=str(exc)
        )


# ── Judge + score (called inside the Celery worker, not the request path) ────


def run_judge_and_score(
    *,
    trace_id: str,
    agent_name: str,
    input_dict: dict,
    output_dict: dict,
) -> dict[str, float] | None:
    """Run the LLM judge for one prod trace and post per-dimension scores
    back to Langfuse. Returns the score dict on success, None on failure.

    Designed to be called from a Celery worker context.
    """
    rubric_path = _rubric_path_for(agent_name)
    if not rubric_path.exists():
        log.debug("online_eval_no_rubric", agent=agent_name)
        return None

    try:
        rubric_text, threshold = _load_rubric(rubric_path)
        scores, reasoning = _judge(
            agent_name=agent_name,
            rubric=rubric_text,
            input_dict=input_dict,
            output_dict=output_dict,
        )
    except Exception as exc:  # noqa: BLE001
        log.warning(
            "online_eval_judge_failed",
            agent=agent_name,
            trace_id=trace_id,
            error=str(exc),
        )
        return None

    if not scores:
        return None

    avg = sum(scores.values()) / len(scores)
    from app.agents._langfuse import score_trace  # noqa: PLC0415

    for dim, value in scores.items():
        score_trace(trace_id, name=f"judge_{dim}", value=value, comment=reasoning)
    score_trace(trace_id, name="judge_avg", value=avg, comment=reasoning)
    score_trace(
        trace_id,
        name="judge_passed",
        value=1.0 if avg >= threshold else 0.0,
        comment=f"threshold={threshold}",
    )
    log.info(
        "online_eval_scored",
        agent=agent_name,
        trace_id=trace_id,
        avg=round(avg, 2),
        passed=avg >= threshold,
    )
    return scores


def _load_rubric(path: Path) -> tuple[str, float]:
    text = path.read_text()
    threshold = DEFAULT_PASS_THRESHOLD
    match = re.search(r"Pass threshold:\s*avg\s*[≥>=]+\s*([0-9.]+)", text)
    if match:
        try:
            threshold = float(match.group(1))
        except ValueError:
            pass
    return text, threshold


def _judge(
    *, agent_name: str, rubric: str, input_dict: dict, output_dict: dict
) -> tuple[dict[str, float], str]:
    """Call Claude Sonnet with the rubric. Returns (scores, reasoning).

    Identical contract to tests/evals/runners/llm_judge.py — same prompt
    shape, same JSON output format. Kept as a separate copy (not imported)
    so prod code doesn't reach into tests/.
    """
    import json  # noqa: PLC0415

    try:
        import anthropic  # noqa: PLC0415
    except ImportError as exc:
        raise RuntimeError("anthropic SDK not installed — needed for online eval") from exc

    client = anthropic.Anthropic()
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
        f"{json.dumps(input_dict, indent=2, default=str)}\n\n"
        "Agent output (judge this):\n"
        f"{json.dumps(output_dict, indent=2, default=str)}"
    )
    response = client.messages.create(
        model=JUDGE_MODEL,
        max_tokens=JUDGE_MAX_TOKENS,
        system=system_blocks,
        messages=[{"role": "user", "content": user_text}],
    )
    raw = _extract_text(response)
    return _parse_judge_json(raw)


def _extract_text(response: Any) -> str:
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
    import json  # noqa: PLC0415

    text = raw.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```\s*$", "", text)
    match = re.search(r"\{.*\}", text, flags=re.DOTALL)
    if not match:
        return {}, ""
    try:
        data = json.loads(match.group(0))
    except json.JSONDecodeError:
        return {}, ""
    raw_scores = data.get("scores", {})
    if not isinstance(raw_scores, dict):
        return {}, ""
    scores: dict[str, float] = {}
    for k, v in raw_scores.items():
        try:
            scores[str(k)] = float(v)
        except (TypeError, ValueError):
            continue
    return scores, str(data.get("reasoning", ""))
