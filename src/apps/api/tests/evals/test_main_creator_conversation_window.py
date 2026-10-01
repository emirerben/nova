"""Structural guard: the Main Creator conversation window and its schema cap agree.

KRI-237 follow-up: the planner read 24 rows while the schema allowed 20, which crashed
every non-fast-path turn on long threads. The cap is now one shared constant.
"""

from __future__ import annotations

from app.agents.main_creator import MAIN_CREATOR_CONVERSATION_MAX, MainCreatorInput


def test_schema_cap_is_the_shared_constant() -> None:
    cap = MainCreatorInput.model_fields["conversation"].metadata[0].max_length
    assert cap == MAIN_CREATOR_CONVERSATION_MAX


def test_window_keeps_a_long_thread_usable() -> None:
    # 20 was too small for real threads; the planner truncates each row to 1000 chars,
    # so this stays well inside a prompt budget.
    assert MAIN_CREATOR_CONVERSATION_MAX >= 40
