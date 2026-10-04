"""KRI-282: conflict-choice questions (detect, payload, replay)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.services.choice_questions import (
    CONFLICT_ORDER_VS_GROUP,
    OPT_CHRONOLOGICAL,
    OPT_GROUP_FIRST,
    ChoiceSelectionIn,
    build_choice_question,
    choice_question_text,
    detect_order_vs_group,
    fold_choice_answers,
    latest_open_choice_question,
    split_groups,
)

T0 = datetime(2026, 9, 20, 9, 0, tzinfo=UTC)


def _clips(n: int, *, untimed: tuple[int, ...] = ()):
    return [(f"c{i}", None if i in untimed else T0 + timedelta(minutes=i * 10)) for i in range(n)]


def _detect(groups, clips=None, **kw):
    return detect_order_vs_group(
        wants_capture_order=kw.pop("wants_capture_order", True),
        groups=groups,
        clips=clips if clips is not None else _clips(6),
        **kw,
    )


# Filmed mixed: football c0,c2,c4  /  dodgeball c1,c3,c5
MIXED = [("football", ["c0", "c2", "c4"]), ("dodgeball", ["c1", "c3", "c5"])]
# Already in blocks: football c0-c2 then dodgeball c3-c5
BLOCKS = [("football", ["c0", "c1", "c2"]), ("dodgeball", ["c3", "c4", "c5"])]


def test_mixed_filming_is_a_real_conflict_with_a_recommended_option():
    found = _detect(MIXED, noun="sport")
    assert found is not None and found.conflict_id == CONFLICT_ORDER_VS_GROUP
    assert [o.key for o in found.options] == [OPT_GROUP_FIRST, OPT_CHRONOLOGICAL]
    assert [o.recommended for o in found.options] == [True, False]
    assert found.options[0].label == "Group by sport, chronological inside each sport"
    assert found.options[1].label == "Keep it strictly chronological; sports may interleave"
    assert "filmed mixed together" in found.reason


def test_no_conflict_when_clips_already_form_blocks_in_capture_order():
    assert _detect(BLOCKS) is None


def test_no_conflict_without_a_chronological_ask_or_with_a_single_group():
    assert _detect(MIXED, wants_capture_order=False) is None
    assert _detect(MIXED[:1]) is None


def test_no_conflict_when_there_are_no_capture_times_to_order_by():
    assert _detect(MIXED, clips=_clips(6, untimed=(0, 1, 2, 3, 4, 5))) is None


def test_ungrouped_and_ambiguous_clips_do_not_create_a_conflict():
    # c3 is in no group, c2 in both: neither can split a stretch.
    groups = [("football", ["c0", "c1", "c2"]), ("dodgeball", ["c2", "c4", "c5"])]
    assert _detect(groups) is None


def test_split_groups_counts_stretches_over_owned_clips_only():
    order = ["c0", "c1", "c2", "c3"]
    assert split_groups(order, [("a", ["c0", "c2"]), ("b", ["c1", "c3"])]) == [("a", 2), ("b", 2)]
    assert split_groups(order, [("a", ["c0", "c1"]), ("b", ["c2", "c3"])]) == []


def test_payload_contract_and_self_sufficient_text():
    found = _detect(MIXED, noun="sport")
    payload = build_choice_question(found, question_id="q1")
    assert payload["version"] == 1 and payload["question_id"] == "q1"
    assert payload["allow_free_text"] is True
    assert payload["conflict"] == CONFLICT_ORDER_VS_GROUP
    assert [o["key"] for o in payload["options"]] == [OPT_GROUP_FIRST, OPT_CHRONOLOGICAL]
    assert payload["options"][0]["recommended"] is True
    assert payload["options"][1]["recommended"] is False
    assert all(o["label"] for o in payload["options"])
    text = choice_question_text(found)
    # Old builds show only this text: both options and the recommendation are in it.
    assert "Group by sport, chronological inside each sport (recommended)" in text
    assert "Keep it strictly chronological; sports may interleave" in text
    assert "football and dodgeball" in text


def _events(*items):
    return list(items)


def _question(conflict=CONFLICT_ORDER_VS_GROUP, qid="q1"):
    found = _detect(MIXED)
    payload = build_choice_question(found, question_id=qid)
    payload["conflict"] = conflict
    return payload


def test_open_question_until_answered():
    ev = [("user", {}), ("assistant", {"choice_question": _question()})]
    assert latest_open_choice_question(ev)["question_id"] == "q1"
    ev.append(("user", {"choice_selection": {"question_id": "q1", "option_key": OPT_GROUP_FIRST}}))
    assert latest_open_choice_question(ev) is None


def test_fold_returns_the_standing_answer_and_ignores_unasked_or_unoffered():
    ev = [
        ("assistant", {"choice_question": _question()}),
        ("user", {"choice_selection": {"question_id": "q1", "option_key": OPT_CHRONOLOGICAL}}),
    ]
    assert fold_choice_answers(ev) == {CONFLICT_ORDER_VS_GROUP: OPT_CHRONOLOGICAL}
    # An option the question never offered, or a question never asked, is not an answer.
    bad = [
        ("assistant", {"choice_question": _question()}),
        ("user", {"choice_selection": {"question_id": "q1", "option_key": "nonsense"}}),
        ("user", {"choice_selection": {"question_id": "ghost", "option_key": OPT_GROUP_FIRST}}),
    ]
    assert fold_choice_answers(bad) == {}


def test_a_later_answer_replaces_an_earlier_one():
    ev = [
        ("assistant", {"choice_question": _question(qid="q1")}),
        ("user", {"choice_selection": {"question_id": "q1", "option_key": OPT_CHRONOLOGICAL}}),
        ("assistant", {"choice_question": _question(qid="q2")}),
        ("user", {"choice_selection": {"question_id": "q2", "option_key": OPT_GROUP_FIRST}}),
    ]
    assert fold_choice_answers(ev) == {CONFLICT_ORDER_VS_GROUP: OPT_GROUP_FIRST}


def test_selection_body_is_strict():
    ChoiceSelectionIn(question_id="q", option_key="k")
    with pytest.raises(ValueError):
        ChoiceSelectionIn(question_id="q", option_key="k", extra="x")  # type: ignore[call-arg]
    with pytest.raises(ValueError):
        ChoiceSelectionIn(question_id="", option_key="k")
