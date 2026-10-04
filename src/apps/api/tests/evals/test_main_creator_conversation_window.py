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


def test_use_all_overlays_is_not_all_media_scope() -> None:
    """KRI-297: "Use all overlays as full screen" names the Visuals pool, not the
    whole manifest; read as all-media scope it was refused on phone Talking
    edits (guided unavailable) on every attempt."""
    from app.agents.main_creator import _explicit_media_scope_from_request
    from app.routes.creator_agent import _explicit_media_scope, _has_explicit_media_scope

    msg = "Use all overlays as full screen."
    assert _explicit_media_scope_from_request(msg) is None
    assert _explicit_media_scope(msg) == "selected"
    assert not _has_explicit_media_scope(msg)
    # Plain "use all" / "use everything" keep their all-media meaning.
    assert _explicit_media_scope_from_request("Use all of them") == "all"
    assert _explicit_media_scope("use everything") == "all"
