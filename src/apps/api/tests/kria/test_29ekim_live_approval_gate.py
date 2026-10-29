"""29 Ekim, new thread (prod 64bb9853): the live model's own answers pass the approval gate.

Scorecard check 6 (`scripts/kria/score_29_ekim.py`) replays the turn the creator re-ran after
every 29 Ekim lane had shipped and that was still refused. The brief is prod's (its digest
equals the stored `approval_readiness.brief_digest`), and the Main Creator, clip-intent planner
and clip resolver answers were captured live on that exact message. Everything between them is
production code: the parsers, clip-intent planning, the planner's order fields, approval
readiness and the choice gate. A variant of the planner answer without `order_by` proves the
server holds when the model does not write that field.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

REPO = Path(__file__).resolve().parents[5]


def _scorecard():
    spec = importlib.util.spec_from_file_location(
        "score_29_ekim", REPO / "scripts" / "kria" / "score_29_ekim.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_live_new_thread_t1_is_a_draft_not_a_question() -> None:
    card = _scorecard()
    with card._prod_flags():
        result = card.check_live_approval_gate()
    assert result["pass"], result["why_it_fails"]
    evidence = result["evidence"]
    assert evidence["brief_digest_ok"] and evidence["live_brief_matches_prod"]
    assert evidence["ordering_choice"] == "random"
    # Prod asked; the same answers now give a draft the creator can approve.
    assert evidence["prod_outcome"]["turn_value"] == "question"
    assert all(v["passes"] for v in evidence["planner_variants"].values())
