"""Server-enforced interview turn contract (dogfood 2026-06-12).

The model emitted impossible labels and padded the interview past its own
estimate — arithmetic promises can't be left to the LLM. parse() now derives
the label from the route's turn counter and force-closes at question 6.
"""

from __future__ import annotations

import json

from app.agents.interviewer_agent import (
    _FORCE_FINAL_AT,
    InterviewerAgent,
    InterviewerInput,
)


def _parse(turn_count: int, *, question="What do you film?", label="", is_final=False):
    agent = InterviewerAgent.__new__(InterviewerAgent)
    raw = json.dumps(
        {
            "question": question,
            "suggestions": ["a thing", "another"],
            "is_final": is_final,
            "turn_label": label,
        }
    )
    return agent.parse(raw, InterviewerInput(turn_count=turn_count))


def test_label_never_advertises_total_below_current_turn():
    # The prod screenshot had a question labelled with a total below N.
    out = _parse(_FORCE_FINAL_AT, label=f"~{_FORCE_FINAL_AT} OF ~5")
    assert out.turn_label == f"~{_FORCE_FINAL_AT} OF ~{_FORCE_FINAL_AT}"


def test_force_final_turn_is_always_final():
    out = _parse(
        _FORCE_FINAL_AT,
        label=f"~{_FORCE_FINAL_AT} OF ~{_FORCE_FINAL_AT + 1}",
        is_final=False,
    )
    assert out.is_final is True
    assert out.question.startswith("One last thing")
    assert out.turn_label == f"~{_FORCE_FINAL_AT} OF ~{_FORCE_FINAL_AT}"


def test_nonfinal_label_promises_at_least_one_more_turn():
    out = _parse(3, label="~3 OF ~2")
    assert out.is_final is False
    assert out.turn_label == "~3 OF ~4"


def test_model_final_label_collapses_to_n():
    out = _parse(5, label="~5 OF ~6", is_final=True)
    assert out.is_final is True
    assert out.turn_label == "~5 OF ~5"


def test_unparseable_label_falls_back_to_default_estimate():
    out = _parse(2, label="soon!")
    assert out.turn_label == "~2 OF ~5"


def test_honest_model_label_passes_through():
    out = _parse(2, label="~2 OF ~5")
    assert out.turn_label == "~2 OF ~5"
    assert out.is_final is False


# ── Monotonic denominator (dogfood 2026-07-09: label crept ~5 → ~8) ───────────


def _parse_with_prev(turn_count: int, prev: int | None, *, label="", is_final=False):
    agent = InterviewerAgent.__new__(InterviewerAgent)
    raw = json.dumps(
        {
            "question": "What do you film?",
            "suggestions": ["a thing"],
            "is_final": is_final,
            "turn_label": label,
        }
    )
    return agent.parse(raw, InterviewerInput(turn_count=turn_count, prev_total_estimate=prev))


def test_label_total_never_creeps_above_previously_advertised():
    # Model inflates its estimate mid-interview; the user already saw "OF ~5".
    out = _parse_with_prev(3, 5, label="~3 OF ~8")
    assert out.turn_label == "~3 OF ~5"


def test_label_total_grows_only_when_turns_outrun_the_promise():
    # Interview genuinely ran past the advertised total: n+1 wins, minimally.
    out = _parse_with_prev(5, 5, label="~5 OF ~8")
    assert out.turn_label == "~5 OF ~6"


def test_no_prev_estimate_keeps_existing_bounds():
    out = _parse_with_prev(2, None, label="~2 OF ~4")
    assert out.turn_label == "~2 OF ~4"


def test_prev_total_estimate_helper_reads_last_agent_label():
    from app.routes.personas import _prev_total_estimate

    turns = [
        {"role": "agent", "content": "q1", "turn_label": "~1 OF ~5"},
        {"role": "user", "content": "a1"},
        {"role": "agent", "content": "q2", "turn_label": "~2 OF ~4"},
        {"role": "user", "content": "a2"},
    ]
    assert _prev_total_estimate(turns) == 4
    assert _prev_total_estimate([{"role": "agent", "content": "q"}]) is None
    assert _prev_total_estimate([]) is None
