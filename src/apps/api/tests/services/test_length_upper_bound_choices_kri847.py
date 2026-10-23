"""KRI-847: the length questions do not interrogate a plan that already meets "at most N".

"At most 10 seconds" with a 7.5 s take is not "you asked for 10 s but the take is 7.5 s".
``duration_bound: "at_most"`` only removes the LOWER side of the existing +/- 10 % check, so
every card keeps the exact path's upper tolerance and a ceiling is never stricter than exact.
(Exclusive wording, "under N", is the merged KRI-629 strict cap and is not this fact's business.)

Failure modes written first:

1. The "use the available length / trim" card fires for a source shorter than the ceiling.
2. A card is stricter for a ceiling than for exact (it fires for a source inside +10 %).
3. The "voice shorter than asked" card changes: the voice-behind-footage render builds exactly
   N seconds, so skipping that card for a ceiling would end in "Your voice runs 8 seconds but
   the edit is 20." Its behaviour stays exactly as on main (the card, same options).
4. Answering "extend" or "match my voice" leaves the brief saying "at most N" at the new length.
5. The coverage message calls a ceiling "the requested N seconds".
6. An exact length changes behaviour (every exact assertion below is a guard).
"""

from __future__ import annotations

import pytest

from app.kria.brief import BriefRequirement, CreativeBrief
from app.kria.reply_language import reply_language_for
from app.services.choice_questions import (
    CONFLICT_DURATION_VS_COUNT,
    CONFLICT_LENGTH_VS_NATURAL,
    CONFLICT_SOURCE_COVERAGE,
    CONFLICT_VOICE_VS_DURATION,
    OPT_ACCEPT_NATURAL,
    OPT_EXTEND,
    OPT_FEWER,
    OPT_MATCH_VOICE,
    ChoiceCapability,
    answered_brief,
    collect_conflicts,
)


def _brief(seconds, bound=None) -> CreativeBrief:
    facts = {"duration_s": seconds, **({"duration_bound": bound} if bound else {})}
    return CreativeBrief(
        version=1,
        requirements=[
            BriefRequirement(
                id="duration",
                kind="timing",
                scope="global",
                description=f"At most {seconds} seconds" if bound else f"Keep {seconds} seconds",
                facts=facts,
            )
        ],
    )


def _kinds(found) -> list[str]:
    return [item.kind for item in found]


# --------------------------------------------- "use the available length" (the take)

TAKE = {"audio_strategy": "original_audio", "edit_format": "talking_head"}


def _take(seconds: float) -> dict:
    return {"clip_assignments": [{"media_id": "take", "duration_s": seconds}]}


def test_a_take_shorter_than_the_ceiling_asks_nothing():
    found = collect_conflicts(TAKE, _brief(10, "at_most"), _take(7.5))

    assert CONFLICT_LENGTH_VS_NATURAL not in _kinds(found)


def test_guard_the_same_take_under_an_exact_ten_seconds_still_asks():
    found = collect_conflicts(TAKE, _brief(10), _take(7.5))

    assert CONFLICT_LENGTH_VS_NATURAL in _kinds(found)


@pytest.mark.parametrize("take_s", [10.0, 10.5, 11.0])
def test_a_take_up_to_the_exact_tolerance_asks_nothing_for_a_ceiling_as_for_exact(take_s):
    """11.0 s is 10 s + 10 %: no card for exact, so none for at_most (never stricter)."""
    ceiling = collect_conflicts(TAKE, _brief(10, "at_most"), _take(take_s))
    exact = collect_conflicts(TAKE, _brief(10), _take(take_s))

    assert CONFLICT_LENGTH_VS_NATURAL not in _kinds(exact)
    assert CONFLICT_LENGTH_VS_NATURAL not in _kinds(ceiling)


@pytest.mark.parametrize("take_s", [12.0, 51.6])
def test_a_take_past_the_tolerance_still_asks_for_a_ceiling_as_for_exact(take_s):
    ceiling = collect_conflicts(TAKE, _brief(10, "at_most"), _take(take_s))
    exact = collect_conflicts(TAKE, _brief(10), _take(take_s))

    card = next(item for item in ceiling if item.kind == CONFLICT_LENGTH_VS_NATURAL)
    assert OPT_ACCEPT_NATURAL in [option.key for option in card.options]
    assert CONFLICT_LENGTH_VS_NATURAL in _kinds(exact)


def test_the_ceiling_card_says_at_most():
    [card] = [
        item
        for item in collect_conflicts(TAKE, _brief(10, "at_most"), _take(12.0))
        if item.kind == CONFLICT_LENGTH_VS_NATURAL
    ]

    assert "an edit of at most 10 seconds" in card.reason
    assert "At most 10 seconds" in card.intro  # the creator's own words, quoted


def test_the_ceiling_card_is_worded_in_turkish_in_a_turkish_chat():
    with reply_language_for("tr"):
        [card] = [
            item
            for item in collect_conflicts(TAKE, _brief(10, "at_most"), _take(12.0))
            if item.kind == CONFLICT_LENGTH_VS_NATURAL
        ]

    assert "en fazla 10 saniyelik" in card.reason


def test_guard_an_exact_card_keeps_its_wording():
    [card] = [
        item
        for item in collect_conflicts(TAKE, _brief(10), _take(12.0))
        if item.kind == CONFLICT_LENGTH_VS_NATURAL
    ]

    assert "at most" not in card.reason
    assert "cannot make a 10-second edit" in card.reason


def test_footage_shorter_than_the_ceiling_asks_nothing():
    snapshot = {"best_parts": [{"media_id": "clip", "source_start_s": 0, "source_end_s": 7.5}]}

    ceiling = collect_conflicts({}, _brief(10, "at_most"), snapshot)
    exact = collect_conflicts({}, _brief(10), snapshot)

    assert CONFLICT_LENGTH_VS_NATURAL not in _kinds(ceiling)
    assert CONFLICT_LENGTH_VS_NATURAL in _kinds(exact)  # guard


# ------------------------------------------------- "voice shorter than asked": unchanged

VOICE_STRATEGY = {
    "audio_strategy": "original_audio",
    "edit_format": "montage",
    "voice_mode": "continuous",
    "montage_audio": {"preserve_source_audio": True, "source_media_ids": ["talk"]},
}
VOICE_CAP = ChoiceCapability(voice_route=True)


def _voice_rows(voice_s: float) -> dict:
    rows = [{"media_id": "talk", "kind": "video", "duration_s": voice_s}]
    rows += [{"media_id": f"c{i}", "kind": "video", "duration_s": 12.0} for i in range(4)]
    return {"clip_assignments": rows}


def test_a_voice_shorter_than_a_ceiling_gets_the_same_card_as_on_main():
    """The voice-behind-footage render builds exactly N (``contract.duration_s``), so a short
    voice under "at most 20" still needs its question; the follow-up that caps the route is a
    separate change. The card is identical to the exact one."""
    ceiling = collect_conflicts(VOICE_STRATEGY, _brief(20, "at_most"), _voice_rows(8.0), VOICE_CAP)
    exact = collect_conflicts(VOICE_STRATEGY, _brief(20), _voice_rows(8.0), VOICE_CAP)

    [ceiling_card] = [c for c in ceiling if c.kind == CONFLICT_VOICE_VS_DURATION]
    [exact_card] = [c for c in exact if c.kind == CONFLICT_VOICE_VS_DURATION]
    assert [o.key for o in ceiling_card.options] == [o.key for o in exact_card.options]
    assert ceiling_card.input_digest == exact_card.input_digest


# --------------------------------------------- "too many clips for N seconds"

MONTAGE = {"edit_format": "montage", "audio_strategy": "licensed_music"}
ONE_SECOND_SHOTS = ChoiceCapability(min_readable_shot_s=1.0)


def _clips(count: int) -> dict:
    return {
        "clip_assignments": [
            {"media_id": f"c{i:02d}", "kind": "video", "duration_s": 5.0} for i in range(count)
        ]
    }


def test_eleven_one_second_shots_fit_a_ceiling_exactly_as_they_fit_exact():
    """11 s of shots is inside 10 s + 10 % for both: a ceiling is never stricter."""
    ceiling = collect_conflicts(MONTAGE, _brief(10, "at_most"), _clips(11), ONE_SECOND_SHOTS)
    exact = collect_conflicts(MONTAGE, _brief(10), _clips(11), ONE_SECOND_SHOTS)

    assert CONFLICT_DURATION_VS_COUNT not in _kinds(ceiling)
    assert CONFLICT_DURATION_VS_COUNT not in _kinds(exact)


def test_twelve_one_second_shots_do_not_fit_either():
    ceiling = collect_conflicts(MONTAGE, _brief(10, "at_most"), _clips(12), ONE_SECOND_SHOTS)
    exact = collect_conflicts(MONTAGE, _brief(10), _clips(12), ONE_SECOND_SHOTS)

    assert CONFLICT_DURATION_VS_COUNT in _kinds(ceiling)
    assert CONFLICT_DURATION_VS_COUNT in _kinds(exact)


def test_extending_is_not_the_recommended_way_out_of_a_ceiling():
    [card] = [
        item
        for item in collect_conflicts(MONTAGE, _brief(10, "at_most"), _clips(20), ONE_SECOND_SHOTS)
        if item.kind == CONFLICT_DURATION_VS_COUNT
    ]

    recommended = [option.key for option in card.options if option.recommended]
    assert recommended == [OPT_FEWER]
    assert OPT_EXTEND in [option.key for option in card.options]  # still the creator's call


def test_guard_extending_is_still_recommended_for_an_exact_length():
    [card] = [
        item
        for item in collect_conflicts(MONTAGE, _brief(10), _clips(20), ONE_SECOND_SHOTS)
        if item.kind == CONFLICT_DURATION_VS_COUNT
    ]

    assert [option.key for option in card.options if option.recommended] == [OPT_EXTEND]


# ----------------------------------- answering rewrites the length; a ceiling does not survive


def _answer(kind: str, option: str, new_seconds: int) -> dict:
    return {
        "strategy": {
            "target_duration_s": new_seconds,
            "choice_answers": [
                {
                    "kind": kind,
                    "option": option,
                    "source": "creator",
                    "conflict": kind,
                    "input_digest": "x",
                    "requirement_ids": ["duration"],
                }
            ],
        }
    }


@pytest.mark.parametrize(
    ("kind", "option"),
    [
        (CONFLICT_DURATION_VS_COUNT, OPT_EXTEND),
        (CONFLICT_VOICE_VS_DURATION, OPT_MATCH_VOICE),
        (CONFLICT_LENGTH_VS_NATURAL, OPT_ACCEPT_NATURAL),
    ],
)
def test_an_answer_that_changes_the_length_makes_the_new_length_exact(kind, option):
    strategy = _answer(kind, option, 14)["strategy"]

    after = answered_brief(_brief(10, "at_most"), strategy)

    [timing] = [req for req in after.live() if req.kind == "timing"]
    assert timing.facts["duration_s"] == 14
    assert "duration_bound" not in timing.facts


# ------------------------------------------- the coverage message words a ceiling as a ceiling


def _coverage_choice(bound: str | None, *, language: str | None = None):
    plan = {
        "conflicts": [{"code": "target_below_coverage"}],
        "sources": [{"duration_s": 1.0}, {"duration_s": 1.0}],
        "target_duration_s": 4,
        **({"target_duration_bound": bound} if bound else {}),
    }
    with reply_language_for(language):
        found = collect_conflicts({"montage_cadence": "fast"}, _brief(4), {"length_plan": plan})
    [card] = [item for item in found if item.kind == CONFLICT_SOURCE_COVERAGE]
    return card


def test_the_hold_coverage_message_words_a_ceiling_as_at_most():
    card = _coverage_choice("at_most")

    assert "at most 4 seconds" in card.reason
    assert "The requested 4 seconds" not in card.reason


def test_the_hold_coverage_message_words_a_ceiling_in_turkish():
    card = _coverage_choice("at_most", language="tr")

    assert "En fazla 4 saniyelik bir video" in card.reason
    assert "İstenen" not in card.reason


def test_guard_the_hold_coverage_message_for_an_exact_length_is_unchanged():
    card = _coverage_choice(None)

    assert card.reason.startswith("The requested 4 seconds cannot include these clips")


def test_waiving_a_strict_cap_to_the_available_source_also_drops_the_bound():
    """Composes with KRI-629's `answered_brief`: choosing the available source waives an
    exclusive cap even when the number prints the same (take 8.4 s + 1.6 s ending = 10);
    the rewritten length is concrete, so the stale ceiling fact goes too."""
    strict = BriefRequirement(
        id="duration",
        kind="timing",
        scope="global",
        description="Under 10 seconds",
        facts={"duration_s": 10, "duration_bound": "at_most"},
    )
    strategy = _answer(CONFLICT_LENGTH_VS_NATURAL, OPT_ACCEPT_NATURAL, 10)["strategy"]

    after = answered_brief(CreativeBrief(version=1, requirements=[strict]), strategy)

    [timing] = [req for req in after.live() if req.kind == "timing"]
    assert timing.facts == {"duration_s": 10}
    assert timing.description != "Under 10 seconds"  # main's waiver rewrote it
