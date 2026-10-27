"""KRI-990: a timing question is never a dead end.

Prod 4C536AFE: a single-option source-coverage card, then one typed message that was not an
option match. The planner answered with a bare "Your length choice is still waiting." (no card),
which closed the question, and the phone made the old card read-only. Nothing to tap, no label
to type, every timing kind alike.

Failure modes (listed before the code):

1. A timing question (source_coverage, duration_vs_count) past its ask limit and not answered
   by this turn goes to ``exhausted`` -> bare progress text. Must come back as the open question
   (same conflict + digest, "still waiting" lead, same options).
2. The re-ask is not answerable: typed label / alias no longer matches, or the question is not
   the live one (``latest_open_choice_question``) after the non-answer + the next card.
3. A third and fourth non-answer drop the question (count keeps growing).
4. source_coverage has no way forward without new footage even on the FIRST ask.
5. The exit drops the wrong ids: keeps the gone clip, drops a good one, forgets media_scope,
   mishandles an empty ("all") selection, or leaves an empty edit.
6. The exit is offered when the server cannot name the clips (guessing), or when it would
   leave nothing.
7. ``length_inputs_waiting`` still says "Add or update the selected footage" after the exit
   (a draft would never be made); update_footage must still say it.
8. The digest of existing source_coverage cases changes (an answered card would stop matching).
9. Non-timing kinds (order_basis) stop going to ``exhausted`` (KRI-476 backstop relies on it).
11. Photo-only selection (every id an attached Visual): no source rows, so a draft was minted
    and the render raised later. Must be a source_coverage question ("You picked only photos"),
    never "no longer attached", with and without a creator length, and no exit.
12. holds_vs_length (2 asks) dead-ends into the bare wait text after its second ask.
13. The empty-selection exit silently drops every pool photo (so it is not offered).
10. A delegated "you choose" lands on the wait-for-footage option instead of the exit.
"""

from __future__ import annotations

import pytest

from app.kria.reply_language import reply_language_for
from app.services.choice_questions import (
    CONFLICT_HOLDS_VS_LENGTH,
    CONFLICT_SOURCE_COVERAGE,
    OPT_LEAVE_OUT_SOURCES,
    OPT_UPDATE_FOOTAGE,
    build_choice_question,
    collect_conflicts,
    delegated_choice,
    latest_open_choice_question,
    length_inputs_waiting,
    match_open_choice,
    resolve_choices,
    tag_event,
)
from app.services.length_planning import build_length_plan
from tests.kria.test_choice_conflict_gate import _brief, _order, _rows, _timing

NON_ANSWER = (
    "All my clips are still attached, nothing was removed. Use the steak photo and make "
    "the montage."
)


def _clip(media_id: str, seconds: float) -> dict:
    return {"media_id": media_id, "kind": "video", "duration_s": seconds}


def _coverage(rows: list[dict], selected: list[str], *, strategy_selected=None):
    """A strategy + snapshot whose length plan is built by the real planner code."""
    strategy = {"edit_format": "montage", "media_scope": "selected"}
    chosen = selected if strategy_selected is None else strategy_selected
    if chosen:
        strategy["selected_media_ids"] = chosen
    snapshot = {"clip_assignments": rows}
    snapshot["length_plan"] = build_length_plan(strategy, snapshot, selected_ids=selected)
    return strategy, snapshot


def _gone():
    """Clip ``a`` is attached; ``gone`` is selected but has no clip row."""
    return _coverage([_clip("a", 6.0), _clip("b", 6.0)], ["a", "b", "gone"])


def _card(resolution) -> dict:
    assert resolution.question is not None
    return build_choice_question(resolution.question.candidate())


def _asked(card: dict) -> tuple[str, dict]:
    return "assistant", {"choice_question": card, "turn_value": "question"}


def _said(text: str) -> tuple[str, dict]:
    return tag_event("user", {}, None, text)


def _picked(card: dict, option: str, **extra) -> tuple[str, dict]:
    return "user", {
        "choice_selection": {
            "question_id": card["question_id"],
            "option_key": option,
            **extra,
        }
    }


# ── 1 / 3: the dead end, for source_coverage ─────────────────────────────────


def test_source_coverage_first_ask_offers_a_way_forward_without_new_footage():
    strategy, snapshot = _gone()
    first = resolve_choices(strategy, None, snapshot, [])
    assert first.question.kind == CONFLICT_SOURCE_COVERAGE
    assert [o.key for o in first.question.options] == [OPT_UPDATE_FOOTAGE, OPT_LEAVE_OUT_SOURCES]
    assert first.question.recommended().key == OPT_LEAVE_OUT_SOURCES
    assert first.exhausted == ()


@pytest.mark.parametrize("non_answers", [1, 2, 4])
def test_a_typed_non_answer_brings_the_same_source_coverage_card_back(non_answers):
    strategy, snapshot = _gone()
    first = resolve_choices(strategy, None, snapshot, [])
    events: list = []
    previous, card = first, _card(first)
    for _ in range(non_answers):
        events += [_asked(card), _said(NON_ANSWER)]
        previous = resolve_choices(strategy, None, snapshot, events)
        previous_card = card
        card = _card(previous)
        again = previous.question
        assert again is not None and again.kind == CONFLICT_SOURCE_COVERAGE
        assert again.intro.startswith("Your length choice is still waiting. ")
        assert again.input_digest == first.question.input_digest
        assert [o.key for o in again.options] == [OPT_UPDATE_FOOTAGE, OPT_LEAVE_OUT_SOURCES]
        assert again.recommended().key == OPT_LEAVE_OUT_SOURCES
        assert previous.exhausted == ()
        assert card["question_id"] != previous_card["question_id"]
    # The re-asked card is the live question the phone can tap.
    live = latest_open_choice_question([*events, _asked(card)])
    assert live["question_id"] == card["question_id"]


def test_the_lead_is_turkish_in_a_turkish_chat():
    strategy, snapshot = _gone()
    first = resolve_choices(strategy, None, snapshot, [])
    events = [_asked(_card(first)), _said(NON_ANSWER)]
    with reply_language_for("tr"):
        again = resolve_choices(strategy, None, snapshot, events).question
    assert again.intro.startswith("Uzunluk seçimin hâlâ bekliyor. ")


# ── 1: the same for a different timing kind ──────────────────────────────────


def test_duration_vs_count_is_asked_again_after_a_non_answer():
    strategy = {"edit_format": "montage", "target_duration_s": 3, "target_duration_requested": True}
    snapshot = {"clip_assignments": _rows(41)}
    brief = _brief(_timing(3))
    first = resolve_choices(strategy, brief, snapshot, [])
    assert first.question.kind == "duration_vs_count"
    events = [_asked(_card(first)), _said("make it more fun")]
    again = resolve_choices(strategy, brief, snapshot, events)
    assert again.question is not None and again.question.kind == "duration_vs_count"
    assert again.question.intro.startswith("Your length choice is still waiting. ")
    assert [o.key for o in again.question.options] == [o.key for o in first.question.options]
    assert again.question.input_digest == first.question.input_digest
    assert again.exhausted == ()
    # Tapping an option on the re-asked card still answers it.
    card = _card(again)
    done = resolve_choices(
        strategy, brief, snapshot, [*events, _asked(card), _picked(card, "extend")]
    )
    assert done.question is None and done.answers[0]["option"] == "extend"


# ── 2: the re-ask is answerable by typing ────────────────────────────────────


@pytest.mark.parametrize(
    "typed",
    ["Leave those clips out", "leave them out", "continue without them", "2", "option 2"],
)
def test_a_typed_label_or_alias_on_the_re_asked_card_picks_the_exit(typed):
    strategy, snapshot = _gone()
    first = resolve_choices(strategy, None, snapshot, [])
    events = [_asked(_card(first)), _said(NON_ANSWER)]
    card = _card(resolve_choices(strategy, None, snapshot, events))
    assert match_open_choice(card, typed) == OPT_LEAVE_OUT_SOURCES
    assert match_open_choice(card, "Update the selected clips") == OPT_UPDATE_FOOTAGE
    assert match_open_choice(card, NON_ANSWER) is None


def test_the_turkish_label_and_aliases_pick_the_exit():
    strategy, snapshot = _gone()
    with reply_language_for("tr"):
        card = _card(resolve_choices(strategy, None, snapshot, []))
    assert [o["label"] for o in card["options"]][1] == "O klipleri dışarıda bırak"
    for typed in ("O klipleri dışarıda bırak", "onlar olmadan devam et", "Leave those clips out"):
        assert match_open_choice(card, typed) == OPT_LEAVE_OUT_SOURCES


# ── 5: what the exit does ────────────────────────────────────────────────────


def test_leave_out_drops_the_gone_clip_keeps_the_rest_and_reaches_a_draft():
    strategy, snapshot = _gone()
    card = _card(resolve_choices(strategy, None, snapshot, []))
    done = resolve_choices(
        strategy, None, snapshot, [_asked(card), _picked(card, OPT_LEAVE_OUT_SOURCES)]
    )
    assert done.question is None and done.exhausted == ()
    assert done.strategy["selected_media_ids"] == ["a", "b"]
    assert done.strategy["media_scope"] == "selected"
    assert done.answers[0]["option"] == OPT_LEAVE_OUT_SOURCES
    assert length_inputs_waiting(done) is None
    assert any("I left those clips out." in n for n in done.notices)


def test_the_answer_keeps_applying_on_the_next_turn():
    strategy, snapshot = _gone()
    card = _card(resolve_choices(strategy, None, snapshot, []))
    events = [_asked(card), _picked(card, OPT_LEAVE_OUT_SOURCES), _said("make it faster")]
    later = resolve_choices(strategy, None, snapshot, events)
    assert later.question is None
    assert later.strategy["selected_media_ids"] == ["a", "b"]


def test_no_exit_with_an_empty_selection_so_pool_photos_are_never_dropped():
    """An empty selection is "everything", pool photos included; an explicit list built from
    clip rows alone would silently drop them, so the exit is not offered."""
    rows = [_clip("a", 6.0), _clip("b", 0.05), _clip("c", 6.0)]
    strategy, snapshot = _coverage(rows, ["a", "b", "c"], strategy_selected=[])
    assert "selected_media_ids" not in strategy
    first = resolve_choices(strategy, None, snapshot, [])
    assert [o.key for o in first.question.options] == [OPT_UPDATE_FOOTAGE]
    assert first.question.recommended().key == OPT_UPDATE_FOOTAGE
    assert first.question.effects == {}


def test_leave_out_also_names_a_clip_that_is_too_short():
    rows = [_clip("a", 6.0), _clip("b", 0.05)]
    strategy, snapshot = _coverage(rows, ["a", "b"])
    (conflict,) = collect_conflicts(strategy, None, snapshot)
    assert conflict.effects[OPT_LEAVE_OUT_SOURCES]["strategy"]["selected_media_ids"] == ["a"]


# ── 6: never guess, never leave nothing ──────────────────────────────────────


def test_no_exit_when_dropping_would_leave_no_media():
    strategy, snapshot = _coverage([_clip("a", 6.0)], ["gone"])
    (conflict,) = collect_conflicts(strategy, None, snapshot)
    assert [o.key for o in conflict.options] == [OPT_UPDATE_FOOTAGE]
    assert conflict.options[0].recommended is True
    assert conflict.effects == {}


def test_no_exit_when_the_server_cannot_name_the_clips():
    snapshot = {
        "length_plan": {
            "conflicts": [{"code": "no_usable_sources"}],
            "sources": [{"duration_s": 10.0}],
        }
    }
    (conflict,) = collect_conflicts({"edit_format": "montage"}, None, snapshot)
    assert [o.key for o in conflict.options] == [OPT_UPDATE_FOOTAGE]


@pytest.mark.parametrize("code", ["cap_below_coverage", "insufficient_media"])
def test_no_exit_for_other_coverage_reasons(code):
    snapshot = {
        "length_plan": {
            "conflicts": [{"code": code}],
            "sources": [{"media_id": "a", "duration_s": 0.05}],
            "missing_source_ids": ["gone"],
        }
    }
    (conflict,) = collect_conflicts({"edit_format": "montage"}, None, snapshot)
    assert [o.key for o in conflict.options] == [OPT_UPDATE_FOOTAGE]


# ── 7: update_footage keeps its meaning ──────────────────────────────────────


def test_update_footage_still_waits_for_new_footage():
    strategy, snapshot = _gone()
    card = _card(resolve_choices(strategy, None, snapshot, []))
    done = resolve_choices(
        strategy, None, snapshot, [_asked(card), _picked(card, OPT_UPDATE_FOOTAGE)]
    )
    assert "Add or update the selected footage" in length_inputs_waiting(done)


# ── 8: digest unchanged ──────────────────────────────────────────────────────


def test_the_digest_does_not_depend_on_the_new_option():
    strategy, snapshot = _gone()
    (with_exit,) = collect_conflicts(strategy, None, snapshot)
    without = {
        **snapshot,
        "length_plan": {
            k: v for k, v in snapshot["length_plan"].items() if k != "missing_source_ids"
        },
    }
    (without_exit,) = collect_conflicts(strategy, None, without)
    assert [o.key for o in without_exit.options] == [OPT_UPDATE_FOOTAGE]
    assert with_exit.input_digest == without_exit.input_digest


# ── 10: "you choose" ─────────────────────────────────────────────────────────


def test_you_choose_lands_on_the_exit():
    strategy, snapshot = _gone()
    card = _card(resolve_choices(strategy, None, snapshot, []))
    assert delegated_choice(card, "you choose") == OPT_LEAVE_OUT_SOURCES


# ── 9: non-timing kinds keep the exhausted backstop ──────────────────────────


def test_a_non_timing_question_at_its_limit_still_goes_to_exhausted():
    strategy = {"edit_format": "montage", "ordering_choice": "chronological"}
    snapshot = {"clip_assignments": _rows(5, dated=False)}
    brief = _brief(_order())
    first = resolve_choices(strategy, brief, snapshot, [])
    assert first.question.kind == "order_basis"
    events = [_asked(_card(first)), _said("hmm?")]
    second = resolve_choices(strategy, brief, snapshot, events)
    assert second.question is not None and second.question.kind == "order_basis"
    events += [_asked(_card(second)), _said("hmm?")]
    spent = resolve_choices(strategy, brief, snapshot, events)
    assert spent.question is None
    assert [c.kind for c in spent.exhausted] == ["order_basis"]


# ── 11: photo-only selection ─────────────────────────────────────────────────

# After "Unfortunately" the reason loses its capital (``_after_connector``).
PHOTO_ONLY_TEXT = (
    "you picked only photos. A montage needs at least one of your video clips too. "
    "Pick a clip, or add footage."
)


@pytest.mark.parametrize("target", [10.0, None])
def test_a_photo_only_selection_is_a_source_coverage_question(target):
    from app.services.choice_questions import choice_question_text

    strategy = {"edit_format": "montage", "media_scope": "selected"}
    strategy["selected_media_ids"] = ["asset-1"]
    snapshot = {"clip_assignments": [_clip("a", 6.0)]}
    snapshot["length_plan"] = build_length_plan(
        strategy,
        snapshot,
        selected_ids=["asset-1"],
        attached_visual_ids=["asset-1"],
        creator_target_s=target,
    )
    codes = [c["code"] for c in snapshot["length_plan"]["conflicts"]]
    assert "no_video_sources" in codes and "no_usable_sources" not in codes
    first = resolve_choices(strategy, None, snapshot, [])
    assert first.question is not None and first.question.kind == CONFLICT_SOURCE_COVERAGE
    text = choice_question_text(first.question.candidate())
    assert "no longer attached" not in text and "None" not in text
    assert [o.key for o in first.question.options] == [OPT_UPDATE_FOOTAGE]
    assert PHOTO_ONLY_TEXT in text  # with a creator length or without: the same honest reason
    # Turkish twin renders too, with no stray English.
    with reply_language_for("tr"):
        tr_text = choice_question_text(
            resolve_choices(strategy, None, snapshot, []).question.candidate()
        )
    assert "yalnızca fotoğraf seçtin" in tr_text


def test_a_photo_only_selection_with_a_missing_clip_still_says_no_longer_attached():
    strategy, snapshot = _coverage([_clip("a", 6.0)], ["gone"])
    codes = [c["code"] for c in snapshot["length_plan"]["conflicts"]]
    assert "no_usable_sources" in codes and "no_video_sources" not in codes


# ── 12: holds_vs_length comes back as a card too ─────────────────────────────


def test_holds_vs_length_asked_twice_comes_back_as_a_card_with_the_waiting_lead():
    import tests.kria.test_kri825_explicit_hold_e2e as kri825
    from app.kria.planner import _enrich_best_parts_snapshot

    _message, brief = kri825._over_budget()
    strategy = kri825._strategy()
    snapshot = _enrich_best_parts_snapshot(
        strategy,
        kri825.kri824._media_snapshot(),
        brief,
        visual_photo_ids=[p.manifest_id for p in kri825.PHOTOS],
    )
    first = resolve_choices(strategy, brief, snapshot, [])
    assert first.question.kind == CONFLICT_HOLDS_VS_LENGTH
    one = _card(first)
    events = [_asked(one), _said("hmm")]
    second = resolve_choices(strategy, brief, snapshot, events)
    assert second.question.kind == CONFLICT_HOLDS_VS_LENGTH  # asked once more (limit 2)
    two = _card(second)
    events += [_asked(two), _said("still not an answer")]
    spent = resolve_choices(strategy, brief, snapshot, events)
    assert spent.question is not None and spent.exhausted == ()
    assert spent.question.kind == CONFLICT_HOLDS_VS_LENGTH
    assert spent.question.intro.startswith("Your length choice is still waiting. ")
    assert spent.question.input_digest == first.question.input_digest
    assert _card(spent)["question_id"] not in {one["question_id"], two["question_id"]}
