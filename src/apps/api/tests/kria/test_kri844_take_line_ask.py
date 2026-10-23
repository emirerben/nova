"""KRI-844: a quoted start / end line the take does not hold -> ask which spoken line.

Don Julio T2: English quotes ("Here we have traditional Argentinian sauces", "Thank you") of a
Spanish take ("Lucas Acá tenemos salsas tradicionales argentinas, ... Gracias"). `ground_line`
matches same-language text only, so both lines were `not_found`; the worker kept the whole
12.9 s, the draft card showed no times, and nobody was asked. Now a one-take phone Talking
edit whose quoted line the take's own timed speech does not hold asks, BEFORE approval, which
line of the take to open / end on (``take_line_start`` then ``take_line_end``, the existing
``choice_question`` card). The pick is stored on the answer (``ChoiceAnswer.value``), copied by
``answered_brief`` onto the requirement as ``start_line_spoken`` / ``end_line_spoken``, and
the draft estimate and the worker ground that spoken line (exact, same language) instead of
the creator's quote. The creator's quote stays the receipts' words.

How it can fail (written before the code, CLAUDE.md "Testing rules"):

1. The question is not asked: the quote is in another language and the whole take ships (the
   incident), or it is asked for a find the take DOES hold (a same-language quote, KRI-790's
   Merope T1), or for a multi-clip edit / a non-Talking format / a take with no timed speech
   (no lines to offer, nothing to ground in), or for an account the phone pilot does not cover.
2. The options are wrong: "Lucas Acá tenemos ..." glued into one line (whisper leaves no
   punctuation after a name, so a pick would open on the name); the end side offering lines
   BEFORE the start; more than `MAX_CHOICE_OPTIONS`; the best match dropped when lines are
   capped; a "recommended" option when nothing clearly matches.
3. The answer is lost on the way: the pick is not on the answer; `answered_brief` clobbers the
   start fact with the end fact on the same requirement, or hands the pick to a requirement
   that never asked; the pick is read as a route (`start` / `end` facts print as a title) or
   as a new quote (a one-word pick "Gracias" is below the quote minimum).
4. The cut is wrong: the pick is grounded, but the receipts' quote becomes the pick (the
   draft card would print Spanish for an English ask); a pick the worker's words do not hold
   guesses seconds; a start pick later than the end pick, an implausible or a too-short cut
   is not refused by the existing `before_start` / `implausible` / `too_short` receipts.
5. Asked again or never settled: the same inputs re-ask after an answer; a changed quote or
   a re-transcribed take does NOT re-ask; the question is asked a third time; the "none of
   these" answer is asked again, or hides the honest "couldn't find" receipt.
6. Copy: a Turkish chat gets English; a typed reply ("none of these", a line's words) answers
   nothing; "you choose" picks an arbitrary line when nothing is recommended.

7. A pick is grounded by its TEXT at its first occurrence, so a short line said inside an
   earlier longer one ("Gracias" inside "Y muchas gracias a todos por venir.") cuts there and
   the receipt still says "applied"; a line said twice keeps the wrong occurrence (the start
   pick wants the first, the end pick the last); the words that make a pick unique push the
   stored value past `ChoiceAnswer.value`, or move the span's start (start pick) / end (end
   pick) off the line the creator tapped.
8. A later answer on the same requirement (a length answer that rewrites `duration_s`) rebuilds
   it from the original and drops the `*_spoken` facts.
9. Well-punctuated text is shredded ("Barcelona at" / "La" / "Boqueria"): the Title-case split
   is for a run the ASR left unpunctuated, not for text that ends in a full stop.
10. `app.cli.verify_take_window` ignores the picks and reports FAIL / not_found on a brief
    that has them.
11. A later plain message that equals a line ("Gracias", "Lucas") silently re-answers the
    closed card (KRI-794's re-answer path) and moves the cut.

Each numbered mode has tests below; the real T2 chain is replayed in
``test_kri844_dj_t2_cross_language_replay.py``.
"""

from __future__ import annotations

import copy
import json
import uuid
from pathlib import Path

import pytest

from app.agents._schemas.creator_agent import ChoiceAnswer
from app.config import settings
from app.kria.brief import BriefRequirement, CreativeBrief
from app.kria.opening_cut_ask import (
    END_SPOKEN_FACT,
    START_SPOKEN_FACT,
    estimate_take_window,
    take_window_ask,
)
from app.kria.reply_language import reply_language_for
from app.kria.take_lines import offer_lines, split_clauses, take_lines
from app.pipeline.phone_opening_cut import apply_take_window
from app.pipeline.silence_cut import no_op_plan
from app.services.choice_questions import (
    CONFLICT_TAKE_LINE_END,
    CONFLICT_TAKE_LINE_START,
    MAX_CHOICE_OPTIONS,
    OPT_NO_TAKE_LINE,
    ChoiceCapability,
    answered_brief,
    build_choice_question,
    choice_question_text,
    collect_conflicts,
    delegated_choice,
    match_answered_choice,
    match_open_choice,
    open_conflicts,
    resolve_choices,
)
from app.services.speech_segments import (
    attach_segments_to_analysis,
    exact_line_count,
    words_to_segments,
)

FIXTURE = json.loads(
    (Path(__file__).resolve().parents[1] / "fixtures" / "kri844_dj_t2_take.json").read_text(
        encoding="utf-8"
    )
)
WORDS = FIXTURE["words"]
TAKE_S = float(FIXTURE["clip"]["duration_s"])
START_QUOTE = "Here we have traditional Argentinian sauces"
END_QUOTE = "Thank you"
CREATOR_ID = uuid.UUID("0f5b3c1e-0d4a-4a52-9a8e-5d3a3c1f7e11")
CAP = ChoiceCapability(creator_id=CREATOR_ID)
MEDIA_ID = "analysis-proxy-ios-DJ-T2-LUCAS.mp4"
START_PICK = "Acá tenemos salsas tradicionales argentinas,"
KINDS = {CONFLICT_TAKE_LINE_START, CONFLICT_TAKE_LINE_END}


@pytest.fixture(autouse=True)
def _phone_pilot(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "phone_rendering_enabled", True)
    monkeypatch.setattr(settings, "phone_render_user_ids", [])
    monkeypatch.setattr(settings, "subtitled_archetype_enabled", True)


# -- builders ------------------------------------------------------------------------------


def row(words: list[dict], *, language: str, media_id: str = MEDIA_ID, duration: float = TAKE_S):
    analysis = attach_segments_to_analysis(
        {
            "duration_s": duration,
            "understanding": {"kind": "video", "speech": {"has_speech": True}},
        },
        words,
        language,
    )
    return {
        "kind": "video",
        "media_id": media_id,
        "gcs_path": f"users/u/creation-threads/t/{media_id}",
        "duration_s": duration,
        "analysis": analysis,
    }


def snapshot(*rows: dict) -> dict:
    return {"clip_paths": [r["gcs_path"] for r in rows], "clip_assignments": list(rows)}


SNAP = snapshot(row(WORDS, language="es"))


def brief(start: str | None = START_QUOTE, end: str | None = END_QUOTE) -> CreativeBrief:
    facts = {
        key: value for key, value in (("start_line", start), ("end_line", end)) if value is not None
    }
    return CreativeBrief(
        version=1,
        requirements=[
            BriefRequirement(
                id="r1",
                kind="timing",
                scope="global",
                description="Open straight on the first line and end on the last one.",
                facts=facts,
            )
        ],
    )


STRATEGY = {"edit_format": "subtitled", "audio_strategy": "original_audio"}


def gate(the_brief, snap, events=(), *, strategy=None, cap=CAP):
    """The planner's pass: the model's strategy and the brief as ``answered_brief`` gives it."""
    data = copy.deepcopy(STRATEGY if strategy is None else strategy)
    return resolve_choices(data, answered_brief(the_brief, data), snap, list(events), cap)


def tap(events, res, fragment: str):
    """The creator taps the option whose label holds ``fragment`` on the question ``res`` asked."""
    question = build_choice_question(res.question.candidate())
    option = next(o for o in question["options"] if fragment in o["label"])
    selection = {"question_id": question["question_id"], "option_key": option["key"]}
    return [
        *events,
        ("assistant", {"choice_question": question}),
        ("user", {"choice_selection": selection}),
    ]


def labels(res) -> list[str]:
    return [o.label for o in res.question.options]


def answered(start: str | None, end: str | None, *, base=None) -> tuple[list, object]:
    """Run the gate through both questions; returns the chat events and the last resolution."""
    events: list = []
    res = gate(base or brief(), SNAP, events)
    if start is not None:
        events = tap(events, res, start)
        res = gate(base or brief(), SNAP, events)
    if end is not None:
        events = tap(events, res, end)
        res = gate(base or brief(), SNAP, events)
    return events, res


def english_take() -> dict:
    words = [
        ("so", 0.4, 0.6), ("here", 0.6, 0.9), ("is", 0.9, 1.0), ("the", 1.0, 1.1),
        ("bread.", 1.1, 1.6), ("Hello", 3.0, 3.4), ("my", 3.4, 3.6), ("friends", 3.6, 4.2),
        ("today", 4.2, 4.8), ("we", 5.0, 5.2), ("cook", 5.2, 5.6), ("see", 6.8, 7.0),
        ("you", 7.0, 7.2), ("all", 7.2, 7.5), ("week.", 7.5, 8.0),
    ]  # fmt: skip
    return row(
        [{"text": t, "start_s": s, "end_s": e} for t, s, e in words], language="en", duration=9.0
    )


# == 2. the lines the take offers =====================================================


def test_a_name_with_no_punctuation_is_its_own_line() -> None:
    text = (
        "Lucas Acá tenemos salsas tradicionales argentinas, salsas criollas, "
        "chivichurros y sardines Gracias"
    )
    assert split_clauses(text) == [
        "Lucas",
        START_PICK,
        "salsas criollas,",
        "chivichurros y sardines",
        "Gracias",
    ]


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("¿Qué te llaman?", ["¿Qué te llaman?"]),
        ("Hola ¿qué tal? ¡Bien!", ["Hola", "¿qué tal?", "¡Bien!"]),
        ("so I think I can do it, and I'm ready", ["so I think I can do it,", "and I'm ready"]),
        ("we visited NASA yesterday", ["we visited NASA yesterday"]),
        ("one two three. four five…", ["one two three.", "four five…"]),
        # Well-punctuated text keeps its names whole: the Title-case split is only for a run
        # the ASR left unpunctuated.
        ("Barcelona at La Boqueria.", ["Barcelona at La Boqueria."]),
        ("New York City.", ["New York City."]),
        ("Visit Madrid today?", ["Visit Madrid today?"]),
        ("He said \u201cHello World.\u201d", ["He said \u201cHello World.\u201d"]),
        ("Lucas Acá tenemos salsas Gracias", ["Lucas", "Acá tenemos salsas", "Gracias"]),
    ],
)
def test_clause_splitting(text: str, expected: list[str]) -> None:
    assert split_clauses(text) == expected


def test_lines_come_from_the_segments_in_order_without_repeats() -> None:
    segments = [
        {"start_s": 0.0, "end_s": 2.0, "text": "Hola amigos. Gracias"},
        {"start_s": 3.0, "end_s": 5.0, "text": "Gracias, hasta luego"},
        {"start_s": 0.0, "end_s": 0.0, "text": "ignored zero-length"},
    ]
    lines = take_lines(segments)
    assert [line.text for line in lines] == ["Hola amigos.", "Gracias", "hasta luego"]
    assert [line.index for line in lines] == [0, 1, 2]  # a repeated "Gracias," is dropped
    assert lines[0].start_s == 0.0 and lines[1].end_s <= 2.0 and lines[2].start_s >= 3.0


def test_when_lines_are_capped_the_most_similar_stay_in_time_order() -> None:
    texts = [f"filler number {n} goes here" for n in range(10)]
    texts[7] = "the traditional argentinian sauces and more"
    segments = [
        {"start_s": float(i), "end_s": i + 0.9, "text": text} for i, text in enumerate(texts)
    ]
    lines = take_lines(segments)
    offer = offer_lines(lines, START_QUOTE, side="start", limit=5)
    assert len(offer.lines) == 5
    assert any(line.text == texts[7] for line in offer.lines)
    assert [line.index for line in offer.lines] == sorted(line.index for line in offer.lines)
    assert offer.recommended is not None and offer.recommended.text == texts[7]


def test_nothing_is_recommended_unless_one_line_clearly_matches() -> None:
    segments = [
        {"start_s": 0.0, "end_s": 1.0, "text": "uno dos"},
        {"start_s": 1.0, "end_s": 2.0, "text": "tres cuatro"},
        {"start_s": 2.0, "end_s": 3.0, "text": "cinco seis"},
    ]
    offer = offer_lines(take_lines(segments), "Thank you", side="end", limit=5)
    assert offer.recommended is None
    # Two equally close lines: neither clearly wins.
    twin = take_lines(
        [
            {"start_s": 0.0, "end_s": 1.0, "text": "traditional sauces here"},
            {"start_s": 1.0, "end_s": 2.0, "text": "traditional sauces there"},
        ]
    )
    assert offer_lines(twin, "traditional sauces", side="start", limit=5).recommended is None


def test_with_no_good_match_the_end_side_keeps_the_latest_lines() -> None:
    segments = [
        {"start_s": float(i), "end_s": i + 0.9, "text": f"cosa numero {i} aqui"} for i in range(9)
    ]
    end = offer_lines(take_lines(segments), "Thank you", side="end", limit=3)
    start = offer_lines(take_lines(segments), "Hello there", side="start", limit=3)
    assert [line.index for line in end.lines] == [6, 7, 8]
    assert [line.index for line in start.lines] == [0, 1, 2]


# == 1. who is asked =====================================================================


def test_english_quotes_of_the_spanish_take_ask_the_start_line_first() -> None:
    res = gate(brief(), SNAP)
    assert res.question is not None and res.question.kind == CONFLICT_TAKE_LINE_START
    shown = labels(res)
    assert any(START_PICK in text for text in shown)
    assert not any("Lucas Acá" in text for text in shown)
    assert shown[-1].startswith("None of these")
    assert len(shown) <= MAX_CHOICE_OPTIONS
    assert res.question.requirement_ids == ("r1",)
    recommended = [o.label for o in res.question.options if o.recommended]
    assert len(recommended) == 1 and START_PICK in recommended[0]
    text = choice_question_text(res.question.candidate())
    assert f"“{START_QUOTE}”" in text and "Which line should it open on?" in text
    # One question per turn: the end side waits for the start answer.
    assert [c.kind for c in collect_conflicts(STRATEGY, brief(), SNAP, CAP)] == [
        CONFLICT_TAKE_LINE_START,
        CONFLICT_TAKE_LINE_END,
    ]


def test_after_the_start_pick_the_end_question_offers_the_lines_after_it() -> None:
    events, res = answered(START_PICK, None)
    assert res.question is not None and res.question.kind == CONFLICT_TAKE_LINE_END
    shown = labels(res)
    assert any("Gracias" in text for text in shown)
    assert not any("Lucas" in text or "Qué te llaman" in text for text in shown)
    assert shown[-1].startswith("None of these")
    assert "Which line should it end on?" in choice_question_text(res.question.candidate())
    (answer,) = res.answers
    assert (answer["kind"], answer["value"]) == (CONFLICT_TAKE_LINE_START, START_PICK)
    assert answer["requirement_ids"] == ["r1"]


def test_a_start_line_the_take_holds_is_not_asked_and_bounds_the_end_lines() -> None:
    snap = snapshot(english_take())
    res = gate(brief("Hello my friends today", "thanks a lot"), snap)
    assert res.question is not None and res.question.kind == CONFLICT_TAKE_LINE_END
    shown = labels(res)
    assert not any("here is the bread" in text.lower() for text in shown)  # said before the start
    assert any("Hello" in text for text in shown)


@pytest.mark.parametrize("case", ["merope", "english"])
def test_a_line_the_take_holds_never_asks_and_the_cut_is_unchanged(case: str) -> None:
    if case == "merope":
        merope = json.loads(
            (
                Path(__file__).resolve().parents[1] / "fixtures" / "kri790_merope_t1_take.json"
            ).read_text()
        )
        snap = snapshot(
            {
                "kind": "video",
                "media_id": MEDIA_ID,
                "gcs_path": f"users/u/t/{MEDIA_ID}",
                "duration_s": merope["duration_s"],
                "analysis": {
                    "duration_s": merope["duration_s"],
                    "understanding": {"speech": {"segments": merope["segments"]}},
                },
            }
        )
        quotes = ("Hi, I'm Karolina, I'm from Ecuador", "I'm gonna be here all week")
        words, duration = merope["words"], float(merope["duration_s"])
    else:
        snap = snapshot(english_take())
        quotes = ("Hello my friends today", "see you all week")
        words = [
            {"text": w["text"], "start_s": w["start_s"], "end_s": w["end_s"]}
            for w in english_take()["analysis"]["understanding"]["speech"]["segments"]
        ]  # sentence timings only: enough for the draft; the cut below uses the real words
        duration = 9.0
    the_brief = brief(*quotes)
    assert [c for c in collect_conflicts(STRATEGY, the_brief, snap, CAP) if c.kind in KINDS] == []
    assert gate(the_brief, snap).question is None
    # Byte-identical worker cut: the new optional arguments change nothing when absent.
    ask = take_window_ask(the_brief.live())
    assert (ask.start_spoken, ask.end_spoken) == (None, None)
    if case == "merope":
        bare = apply_take_window(
            no_op_plan(duration), words, start_quote=quotes[0], end_quote=quotes[1],
            duration_s=duration, start_ids=("r1",), end_ids=("r1",),
        )  # fmt: skip
        same = apply_take_window(
            no_op_plan(duration), words, start_quote=quotes[0], end_quote=quotes[1],
            duration_s=duration, start_ids=("r1",), end_ids=("r1",),
            start_spoken=None, end_spoken=None,
        )  # fmt: skip
        assert bare == same
        assert all("spoken" not in receipt for receipt in bare[1:])
        assert bare[1]["status"] == bare[2]["status"] == "applied"


@pytest.mark.parametrize("case", ["two_clips", "montage", "no_creator", "voiceover", "images_only"])
def test_what_is_not_a_one_take_talking_edit_never_asks(case: str) -> None:
    snap, strategy, cap = SNAP, STRATEGY, CAP
    if case == "two_clips":
        snap = snapshot(
            row(WORDS, language="es"), row(WORDS, language="es", media_id="analysis-proxy-two.mp4")
        )
    elif case == "montage":
        strategy = {**STRATEGY, "edit_format": "montage"}
    elif case == "no_creator":
        cap = ChoiceCapability()
    elif case == "voiceover":
        strategy = {**STRATEGY, "audio_strategy": "voiceover"}
    elif case == "images_only":
        snap = snapshot({**row(WORDS, language="es"), "kind": "image"})
    found = collect_conflicts(strategy, brief(), snap, cap)
    assert [c for c in found if c.kind in KINDS] == []


def test_the_phone_pilot_gate_is_the_runtimes_own(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "phone_rendering_enabled", False)
    assert [c for c in collect_conflicts(STRATEGY, brief(), SNAP, CAP) if c.kind in KINDS] == []


@pytest.mark.parametrize(
    "analysis",
    [
        None,
        "not a record",
        {"duration_s": TAKE_S},
        {"duration_s": TAKE_S, "understanding": {"speech": {"has_speech": False}}},
        {"duration_s": TAKE_S, "understanding": {"speech": {"segments": []}}},
        {"duration_s": TAKE_S, "understanding": {"speech": {"segments": ["x", 3]}}},
        {"understanding": {"speech": {"segments": [{"start_s": 0, "end_s": 2, "text": "hola"}]}}},
    ],
    ids=[
        "none",
        "string",
        "no_understanding",
        "no_speech",
        "no_segments",
        "bad_rows",
        "no_duration",
    ],
)
def test_a_take_with_no_timed_speech_never_asks_and_never_crashes(analysis) -> None:  # noqa: ANN001
    bare = {
        "kind": "video",
        "media_id": MEDIA_ID,
        "gcs_path": f"u/{MEDIA_ID}",
        "analysis": analysis,
    }
    snap = snapshot(bare)
    assert [c for c in collect_conflicts(STRATEGY, brief(), snap, CAP) if c.kind in KINDS] == []
    assert gate(brief(), snap).question is None
    estimate = estimate_take_window(brief().live(), snap)
    assert estimate is not None and estimate.start_s is None and estimate.end_s is None


# == 3. the answer travels ================================================================


def test_both_picks_are_recorded_and_copied_onto_the_requirement() -> None:
    events, res = answered(START_PICK, "Gracias")
    assert res.question is None
    assert [(a["kind"], a["value"]) for a in res.answers] == [
        (CONFLICT_TAKE_LINE_START, START_PICK),
        (CONFLICT_TAKE_LINE_END, "Gracias"),
    ]
    the_brief = brief()
    final = answered_brief(the_brief, res.strategy)
    (req,) = final.live()
    assert req.facts[START_SPOKEN_FACT] == START_PICK and req.facts[END_SPOKEN_FACT] == "Gracias"
    # The creator's own words stay; the thread's stored brief is untouched.
    assert (req.facts["start_line"], req.facts["end_line"]) == (START_QUOTE, END_QUOTE)
    assert START_SPOKEN_FACT not in the_brief.requirements[0].facts
    ask = take_window_ask(final.live())
    assert (ask.start_quote, ask.end_quote) == (START_QUOTE, END_QUOTE)
    assert (ask.start_spoken, ask.end_spoken) == (START_PICK, "Gracias")


def test_a_pick_goes_only_to_the_requirement_that_asked() -> None:
    two = CreativeBrief(
        version=1,
        requirements=[
            *brief().requirements,
            BriefRequirement(
                id="r2", kind="select", scope="global", description="Use the best takes",
                facts={"keep": "best"},
            ),
        ],
    )  # fmt: skip
    _events, res = answered(START_PICK, "Gracias", base=two)
    assert res.question is None
    final = answered_brief(two, res.strategy)
    by_id = {r.id: r for r in final.requirements}
    assert START_SPOKEN_FACT in by_id["r1"].facts and END_SPOKEN_FACT in by_id["r1"].facts
    assert by_id["r2"].facts == {"keep": "best"}


def test_a_spoken_fact_alone_is_not_a_line_ask_and_never_a_route() -> None:
    from app.kria.brief_route import route_endpoints  # noqa: PLC0415

    facts = {START_SPOKEN_FACT: "Hola amigos", END_SPOKEN_FACT: "Gracias"}
    only = BriefRequirement(id="r1", kind="timing", scope="global", description="x", facts=facts)
    assert take_window_ask([only]) is None
    # `start` / `end` facts are route keys (printed as a "start -> end" title); these are not.
    assert route_endpoints(facts) == (None, None)


def test_a_one_word_pick_is_a_line_though_a_one_word_quote_is_not() -> None:
    req = BriefRequirement(
        id="r1", kind="timing", scope="global", description="x",
        facts={"start_line": START_QUOTE, "end_line": END_QUOTE, END_SPOKEN_FACT: "Gracias"},
    )  # fmt: skip
    ask = take_window_ask([req])
    assert ask.end_spoken == "Gracias" and ask.start_spoken is None
    plan, start, end = apply_take_window(
        no_op_plan(TAKE_S), WORDS, start_quote=START_QUOTE, end_quote=END_QUOTE,
        duration_s=TAKE_S, end_spoken=ask.end_spoken,
    )  # fmt: skip
    assert (start["reason"], end["status"]) == ("not_found", "applied")
    assert 11.46 <= end["end_s"] <= 11.96 and end["quote"] == END_QUOTE
    assert end["spoken"] == "Gracias"


def test_the_choice_answer_schema_carries_the_pick() -> None:
    answer = ChoiceAnswer(
        conflict=CONFLICT_TAKE_LINE_START, kind=CONFLICT_TAKE_LINE_START, option="line_2",
        input_digest="d" * 24, requirement_ids=["r1"], value=START_PICK,
    )  # fmt: skip
    assert answer.model_dump(mode="json")["value"] == START_PICK
    other = ChoiceAnswer(conflict="order_basis", kind="order_basis", option="x", input_digest="d")
    assert "value" not in other.model_dump(mode="json", exclude_none=True)
    assert "value" not in other.model_dump(mode="json")  # stored answers stay byte-identical


# == 4. the cut ===========================================================================


def test_the_worker_cuts_the_picked_lines_on_the_real_words() -> None:
    plan, start, end = apply_take_window(
        no_op_plan(TAKE_S), WORDS, start_quote=START_QUOTE, end_quote=END_QUOTE,
        duration_s=TAKE_S, start_ids=("r1",), end_ids=("r1",),
        start_spoken=START_PICK, end_spoken="Gracias",
    )  # fmt: skip
    assert (start["status"], end["status"]) == ("applied", "applied")
    opens, ends = plan.keep_segments[0][0], plan.keep_segments[-1][1]
    assert 3.90 <= opens <= 4.20 and 11.46 <= ends <= 11.96
    assert ends - opens < 10.0
    assert (start["quote"], end["quote"]) == (START_QUOTE, END_QUOTE)
    assert (start["spoken"], end["spoken"]) == (START_PICK, "Gracias")


def test_a_pick_the_workers_words_do_not_hold_guesses_nothing() -> None:
    other_words = [  # the same take heard in English: the Spanish pick is not there
        {"text": t, "start_s": s, "end_s": e}
        for t, s, e in [
            ("Here", 4.0, 4.3), ("we", 4.3, 4.5), ("have", 4.5, 4.8), ("sauces", 4.8, 6.0),
            ("Thank", 10.7, 11.0), ("you", 11.0, 11.4),
        ]
    ]  # fmt: skip
    base = no_op_plan(TAKE_S)
    plan, start, end = apply_take_window(
        base, [w for w in other_words if w["start_s"] < 6], start_quote="Zzz qqq xxx",
        end_quote="Yyy www vvv", duration_s=TAKE_S, start_spoken=START_PICK, end_spoken="Gracias",
    )  # fmt: skip
    assert (start["status"], start["reason"]) == ("not_applied", "not_found")
    assert (end["status"], end["reason"]) == ("not_applied", "not_found")
    assert plan.keep_segments == base.keep_segments and plan.removed == base.removed
    assert "start_s" not in start and "end_s" not in end


def test_the_quote_is_the_fallback_when_the_pick_is_gone() -> None:
    # The pick is not in the words but the creator's own quote is: today's grounding, no worse.
    words = [{"text": t, "start_s": s, "end_s": e} for t, s, e in [
        ("hello", 3.0, 3.3), ("my", 3.3, 3.5), ("friends", 3.5, 4.0), ("today", 4.0, 4.5),
        ("bye", 8.0, 8.5), ("now", 8.5, 9.0),
    ]]  # fmt: skip
    _plan, start, _end = apply_take_window(
        no_op_plan(10.0), words, start_quote="hello my friends today", end_quote=None,
        duration_s=10.0, start_spoken="una linea que no esta",
    )  # fmt: skip
    assert start["status"] == "applied" and 2.8 < start["start_s"] < 3.0


def test_bad_picks_hit_the_existing_receipts() -> None:
    def cut(start: str | None, end: str | None):  # noqa: ANN202
        return apply_take_window(
            no_op_plan(TAKE_S), WORDS, start_quote=START_QUOTE, end_quote=END_QUOTE,
            duration_s=TAKE_S, start_spoken=start, end_spoken=end,
        )  # fmt: skip

    _plan, start, _end = cut("Gracias", None)  # opens at 10.7 s of 12.9: implausible
    assert (start["status"], start["reason"]) == ("not_applied", "implausible")
    _plan, start, end = cut("Gracias", START_PICK)  # ends before it starts
    assert end["reason"] == "before_start" or start["reason"] == "implausible"
    _plan, start, end = cut(START_PICK, START_PICK)
    assert (start["status"], end["status"]) == ("applied", "applied")
    _plan, _start, end = cut("salsas criollas,", "salsas criollas,")  # ~1.2 s kept
    assert (end["status"], end["reason"]) == ("not_applied", "too_short")
    _plan, start, end = cut("chivichurros y sardines", START_PICK)
    assert (end["status"], end["reason"]) == ("not_applied", "before_start")


# == 5. asked once, settled once ==========================================================


def test_an_answered_question_is_not_asked_again_and_a_new_quote_is() -> None:
    events, res = answered(START_PICK, "Gracias")
    assert res.question is None
    # Same inputs on a later turn: still settled, the pick still applied.
    again = gate(brief(), SNAP, events)
    assert again.question is None and len(again.answers) == 2

    reworded = brief("Here are the creole sauces")
    reasked = gate(reworded, SNAP, events)
    assert reasked.question is not None and reasked.question.kind == CONFLICT_TAKE_LINE_START
    assert START_PICK not in {a.get("value") for a in reasked.answers}


def test_a_re_transcribed_take_asks_again() -> None:
    events, _res = answered(START_PICK, "Gracias")
    changed = [dict(w) for w in WORDS]
    changed[4]["text"] = "Aquí"  # a new transcription of the same clip
    again = gate(brief(), snapshot(row(changed, language="es")), events)
    assert again.question is not None and again.question.kind == CONFLICT_TAKE_LINE_START
    # ...and so does a different upload of the same words.
    other = gate(
        brief(), snapshot(row(WORDS, language="es", media_id="analysis-proxy-new.mp4")), events
    )
    assert other.question is not None and other.question.kind == CONFLICT_TAKE_LINE_START


def test_the_same_unanswered_question_is_asked_twice_then_let_through() -> None:
    res = gate(brief(), SNAP)
    question = build_choice_question(res.question.candidate())
    asked_twice = [("assistant", {"choice_question": question})] * 2
    later = gate(brief(), SNAP, asked_twice)
    # The start question is spent (never a third time, nothing chosen for the creator); the
    # end side has its own two asks.
    assert [c.kind for c in later.exhausted] == [CONFLICT_TAKE_LINE_START]
    assert later.question is not None and later.question.kind == CONFLICT_TAKE_LINE_END
    end = build_choice_question(later.question.candidate())
    spent = gate(brief(), SNAP, [*asked_twice, *[("assistant", {"choice_question": end})] * 2])
    assert spent.question is None and len(spent.exhausted) == 2
    assert (
        spent.answers == ()
        and START_SPOKEN_FACT not in answered_brief(brief(), spent.strategy).requirements[0].facts
    )


def test_the_end_side_is_settled_by_a_changed_start_pick() -> None:
    events, res = answered(START_PICK, "Gracias")
    assert res.question is None
    # The creator reopens the start side with another line: the end question is new again.
    other_start = tap([], gate(brief(), SNAP, []), "salsas criollas,")
    # (a different start answer for the same start digest replaces the first)
    redone = gate(brief(), SNAP, [*events, *other_start])
    assert redone.question is not None and redone.question.kind == CONFLICT_TAKE_LINE_END


# == 6. "none of these" ====================================================================


def test_none_of_these_settles_the_side_and_keeps_the_honest_receipt() -> None:
    events, res = answered("None of these", None)
    assert res.question is not None and res.question.kind == CONFLICT_TAKE_LINE_END
    # Declined start: the end question still offers every line (nothing bounds it).
    assert any("Lucas" in text for text in labels(res))
    events = tap(events, res, "None of these")
    done = gate(brief(), SNAP, events)
    assert done.question is None and done.exhausted == ()
    final = answered_brief(brief(), done.strategy)
    (req,) = final.live()
    assert START_SPOKEN_FACT not in req.facts and END_SPOKEN_FACT not in req.facts
    estimate = estimate_take_window(final.live(), SNAP)
    assert (estimate.start_s, estimate.end_s, estimate.kept_s) == (None, None, None)
    _plan, start, end = apply_take_window(
        no_op_plan(TAKE_S), WORDS, start_quote=START_QUOTE, end_quote=END_QUOTE, duration_s=TAKE_S,
        start_spoken=None, end_spoken=None,
    )  # fmt: skip
    assert (start["reason"], end["reason"]) == ("not_found", "not_found")


def test_the_backstop_lists_only_what_is_still_open() -> None:
    first = gate(brief(), SNAP)
    assert [c.kind for c in open_conflicts(STRATEGY, brief(), SNAP, CAP)] == [
        CONFLICT_TAKE_LINE_START,
        CONFLICT_TAKE_LINE_END,
    ]
    events = tap([], first, START_PICK)
    one = gate(brief(), SNAP, events)
    # The draft strategy a pass leaves carries the answers it recorded.
    strategy_one = {**one.strategy, "choice_answers": list(one.answers)}
    open_after_start = open_conflicts(
        strategy_one, answered_brief(brief(), strategy_one), SNAP, CAP
    )
    assert [c.kind for c in open_after_start] == [CONFLICT_TAKE_LINE_END]
    # The backstop's end question is the very one the gate asks (same options, same digest).
    assert open_after_start[0].input_digest == one.question.input_digest
    assert [o.key for o in open_after_start[0].options] == [o.key for o in one.question.options]
    both = answered(START_PICK, "Gracias")[1]
    assert open_conflicts(both.strategy, answered_brief(brief(), both.strategy), SNAP, CAP) == []


def test_the_approval_backstop_asks_the_same_question_for_a_draft_that_slipped_through() -> None:
    from app.tasks.kria_runtime import _unresolved_choice_plan  # noqa: PLC0415

    blocked = _unresolved_choice_plan(
        STRATEGY, brief(), SNAP, contract_brief=brief(), events=[], has_draft=True,
        creator_id=CREATOR_ID,
    )  # fmt: skip
    assert blocked is not None
    plan, reason = blocked
    assert reason == "unresolved_choice"
    assert plan.choice_question["kind"] == CONFLICT_TAKE_LINE_START
    assert plan.choice_question["input_digest"] == gate(brief(), SNAP).question.input_digest
    # Asked twice, the start side is spent (the receipts then say what is unmet) and the end
    # side, its own question, is next: never a loop.
    asked = [("assistant", {"choice_question": plan.choice_question})] * 2
    again = _unresolved_choice_plan(
        STRATEGY, brief(), SNAP, contract_brief=brief(), events=asked, has_draft=True,
        creator_id=CREATOR_ID,
    )  # fmt: skip
    assert again is not None and again[0].choice_question["kind"] == CONFLICT_TAKE_LINE_END


# == 7. copy and typed replies ============================================================


def test_a_turkish_chat_is_asked_in_turkish() -> None:
    with reply_language_for("tr"):
        res = gate(brief(), SNAP)
        text = choice_question_text(res.question.candidate())
        escape = res.question.options[-1]
    assert "bulamadım" in text and "Hangi cümleyle açılsın?" in text
    assert escape.key == OPT_NO_TAKE_LINE and escape.label.startswith("Hiçbiri")
    assert (
        "None of these — keep it from the start" in escape.aliases
    )  # the English label stays typed
    with reply_language_for("tr"):
        end = gate(brief(), SNAP, tap([], gate(brief(), SNAP), START_PICK))
        assert "Hangi cümleyle bitsin?" in choice_question_text(end.question.candidate())


def test_typed_replies_answer_the_question() -> None:
    res = gate(brief(), SNAP)
    question = build_choice_question(res.question.candidate())
    keys = {o["label"]: o["key"] for o in question["options"]}
    assert match_open_choice(question, "salsas criollas") == next(
        key for label, key in keys.items() if "salsas criollas" in label
    )
    assert match_open_choice(question, "none of these") == OPT_NO_TAKE_LINE
    assert match_open_choice(question, "hiçbiri") == OPT_NO_TAKE_LINE
    # Words that name no line, or two lines, answer nothing.
    assert match_open_choice(question, "the sauces") is None
    assert match_open_choice(question, "salsas") is None


def test_you_choose_takes_only_a_clearly_recommended_line() -> None:
    start = build_choice_question(gate(brief(), SNAP).question.candidate())
    picked = delegated_choice(start, "you choose")
    assert picked is not None and picked != OPT_NO_TAKE_LINE
    chosen = next(o for o in start["options"] if o["key"] == picked)
    assert START_PICK in chosen["label"]
    end_res = gate(brief(), SNAP, tap([], gate(brief(), SNAP), START_PICK))
    end = build_choice_question(end_res.question.candidate())
    assert not any(o.get("recommended") for o in end["options"])
    assert delegated_choice(end, "you choose") is None  # an arbitrary line is never chosen for them


# == 7. a pick is the line tapped, wherever else its words occur ============================


def _words(*rows: tuple[str, float, float]) -> list[dict]:
    return [{"text": t, "start_s": s, "end_s": e} for t, s, e in rows]


# "Gracias" is also said inside the longer "Y muchas gracias a todos por venir.".
THANKS = _words(
    ("Hola", 0.2, 0.5), ("soy", 0.5, 0.8), ("Lucas.", 0.8, 1.4),
    ("Acá", 3.0, 3.3), ("tenemos", 3.3, 3.8), ("salsas", 3.8, 4.2), ("tradicionales.", 4.2, 5.0),
    ("Y", 7.0, 7.1), ("muchas", 7.1, 7.5), ("gracias", 7.5, 8.0), ("a", 8.0, 8.1),
    ("todos", 8.1, 8.4), ("por", 8.4, 8.7), ("venir.", 8.7, 9.3),
    ("Gracias", 11.0, 11.6),
)  # fmt: skip
THANKS_S = 12.0


def _cut(words, *, start=None, end=None, duration=THANKS_S, start_spoken=None, end_spoken=None):
    return apply_take_window(
        no_op_plan(duration), words, start_quote=start, end_quote=end, duration_s=duration,
        start_spoken=start_spoken, end_spoken=end_spoken,
    )  # fmt: skip


def test_an_end_pick_is_the_line_tapped_not_the_same_words_inside_an_earlier_line() -> None:
    snap = snapshot(row(THANKS, language="es", duration=THANKS_S))
    the_brief = brief("Here we have salsas", "Thank you")
    res = gate(the_brief, snap)
    assert res.question.kind == CONFLICT_TAKE_LINE_START
    events = tap([], res, "Acá tenemos salsas tradicionales.")
    res = gate(the_brief, snap, events)
    assert res.question.kind == CONFLICT_TAKE_LINE_END
    events = tap(events, res, "“Gracias”")
    done = gate(the_brief, snap, events)
    assert done.question is None
    end_answer = next(a for a in done.answers if a["kind"] == CONFLICT_TAKE_LINE_END)
    pinned = end_answer["value"]
    # The tapped line is still the span's END; the words before it make it say one place.
    assert pinned.endswith("Gracias") and pinned != "Gracias"
    assert exact_line_count(THANKS, pinned, min_after_s=3.0) == 1
    assert exact_line_count(THANKS, "Gracias", min_after_s=3.0) == 2  # the bug's ground

    ask = take_window_ask(answered_brief(the_brief, done.strategy).live())
    _plan, start, end = _cut(
        THANKS, start="Here we have salsas", end="Thank you",
        start_spoken=ask.start_spoken, end_spoken=ask.end_spoken,
    )  # fmt: skip
    assert (start["status"], end["status"]) == ("applied", "applied")
    assert end["end_s"] >= 11.6  # after the FINAL "Gracias" (11.6), not inside "muchas gracias"
    # Control: the bare word is found inside the earlier, longer line.
    _plan, _start, bare = _cut(THANKS, end="Thank you", end_spoken="Gracias")
    assert bare["status"] == "applied" and bare["end_s"] < 8.5


def test_a_start_pick_is_the_line_tapped_not_the_same_words_inside_an_earlier_line() -> None:
    words = _words(
        ("Dije", 0.2, 0.5), ("gracias", 0.5, 1.0), ("por", 1.0, 1.2), ("venir", 1.2, 1.6),
        ("ayer.", 1.6, 2.2),
        ("gracias", 4.0, 4.5), ("por", 4.5, 4.7), ("venir.", 4.7, 5.2),
        ("Hasta", 7.0, 7.4), ("luego.", 7.4, 8.0),
    )  # fmt: skip
    snap = snapshot(row(words, language="es", duration=9.0))
    the_brief = brief("thanks for coming", None)
    res = gate(the_brief, snap)
    assert res.question.kind == CONFLICT_TAKE_LINE_START
    done = gate(the_brief, snap, tap([], res, "gracias por venir.”"))
    pinned = done.answers[0]["value"]
    # The tapped line is still the span's START; the words after it make it say one place.
    assert pinned.startswith("gracias por venir.") and pinned != "gracias por venir."
    assert exact_line_count(words, pinned) == 1
    _plan, start, _end = _cut(words, start="thanks for coming", start_spoken=pinned, duration=9.0)
    assert start["status"] == "applied" and 3.8 <= start["start_s"] <= 4.0
    bare_args = {
        "start": "thanks for coming",
        "start_spoken": "gracias por venir.",
        "duration": 9.0,
    }
    _plan, bare, _end = _cut(words, **bare_args)
    assert bare["start_s"] < 1.0  # control: the bare line is found in the earlier, longer one


def test_a_pick_that_is_already_one_place_is_stored_as_the_line() -> None:
    _events, res = answered(START_PICK, "Gracias")
    assert [a["value"] for a in res.answers] == [START_PICK, "Gracias"]  # T2 is unchanged


def test_a_stored_pick_stays_within_the_answer_bound() -> None:
    long_line = " ".join(f"palabra{n}" for n in range(60)) + "."  # ~480 characters, one segment
    analysis = {
        "duration_s": 14.0,
        "understanding": {
            "speech": {"segments": [{"start_s": 0.0, "end_s": 12.0, "text": long_line}]}
        },
    }
    only = {
        "kind": "video",
        "media_id": MEDIA_ID,
        "gcs_path": f"u/{MEDIA_ID}",
        "analysis": analysis,
    }
    snap = snapshot(only)
    start = gate(brief("a line that is nowhere", None), snap)
    end = gate(brief(None, "a line that is nowhere"), snap)
    start_value = start.question.effects[start.question.options[0].key]["value"]
    end_value = end.question.effects[end.question.options[0].key]["value"]
    assert len(start_value) <= 300 and len(end_value) <= 300
    assert start_value.startswith("palabra0 ") and not start_value.endswith("palabra59.")  # head
    assert end_value.endswith("palabra59.") and not end_value.startswith("palabra0 ")  # tail


def test_a_line_said_twice_is_the_first_for_a_start_and_the_last_for_an_end() -> None:
    segments = [
        {"start_s": 0.0, "end_s": 1.0, "text": "Hola amigos."},
        {"start_s": 3.0, "end_s": 4.0, "text": "Gracias"},
        {"start_s": 5.0, "end_s": 6.0, "text": "Mucho gusto."},
        {"start_s": 8.0, "end_s": 9.0, "text": "Gracias"},
    ]
    first = take_lines(segments)
    last = take_lines(segments, keep="last")
    assert [(line.text, line.start_s) for line in first if line.text == "Gracias"] == [
        ("Gracias", 3.0)
    ]
    assert [(line.text, line.start_s) for line in last if line.text == "Gracias"] == [
        ("Gracias", 8.0)
    ]
    assert [line.index for line in last] == [0, 1, 2]


def test_the_last_of_two_identical_lines_is_the_end_pick() -> None:
    words = _words(
        ("Hola", 0.2, 0.5), ("amigos.", 0.5, 1.0), ("Gracias", 3.0, 3.6), ("Mucho", 5.0, 5.3),
        ("gusto.", 5.3, 6.0), ("Gracias", 8.0, 8.6),
    )  # fmt: skip
    snap = snapshot(row(words, language="es", duration=10.0))
    the_brief = brief("hello friends", "Thank you")
    res = gate(the_brief, snap)
    assert [text for text in labels(res) if "Gracias" in text] == ["“Gracias”"]
    events = tap([], res, "Hola amigos.")
    end = gate(the_brief, snap, events)
    assert [text for text in labels(end) if "Gracias" in text] == ["“Gracias”"]
    done = gate(the_brief, snap, tap(events, end, "“Gracias”"))
    value = next(a["value"] for a in done.answers if a["kind"] == CONFLICT_TAKE_LINE_END)
    ask = take_window_ask(answered_brief(the_brief, done.strategy).live())
    _plan, _start, cut = _cut(
        words, end="Thank you", end_spoken=ask.end_spoken, duration=10.0
    )  # fmt: skip
    assert value.endswith("Gracias") and cut["end_s"] >= 8.6  # the final one, not 3.6


def test_lines_before_the_start_words_are_not_offered_for_the_end() -> None:
    lines = take_lines(
        [
            {"start_s": 0.0, "end_s": 2.0, "text": "uno dos tres"},
            {"start_s": 2.0, "end_s": 4.0, "text": "cuatro cinco"},
        ]
    )
    # The start words sit inside the first line: that line cannot be ground AFTER them.
    offered = offer_lines(lines, "x", side="end", limit=5, after_s=1.0).lines
    assert [line.text for line in offered] == ["cuatro cinco"]


# == 8. a later answer on the same requirement ===============================================


def test_a_later_length_answer_does_not_drop_the_spoken_facts() -> None:
    the_brief = CreativeBrief(
        version=1,
        requirements=[
            BriefRequirement(
                id="r1", kind="timing", scope="global", description="Under 10 seconds",
                facts={"start_line": START_QUOTE, "end_line": END_QUOTE, "duration_s": 10},
            )
        ],
    )  # fmt: skip
    answers = [
        {"conflict": CONFLICT_TAKE_LINE_START, "kind": CONFLICT_TAKE_LINE_START, "option": "line_2",
         "input_digest": "d", "requirement_ids": ["r1"], "source": "creator", "value": START_PICK},
        {"conflict": CONFLICT_TAKE_LINE_END, "kind": CONFLICT_TAKE_LINE_END, "option": "line_5",
         "input_digest": "d", "requirement_ids": ["r1"], "source": "creator", "value": "Gracias"},
        {"conflict": "length_vs_natural", "kind": "length_vs_natural", "option": "accept_natural",
         "input_digest": "d", "requirement_ids": ["r1"], "source": "creator"},
    ]  # fmt: skip
    final = answered_brief(the_brief, {"choice_answers": answers, "target_duration_s": 12.9})
    (req,) = final.live()
    assert req.facts["duration_s"] == 12.9  # the length answer rewrote the requirement...
    assert req.facts[START_SPOKEN_FACT] == START_PICK  # ...and the picks are still on it
    assert req.facts[END_SPOKEN_FACT] == "Gracias"
    assert (req.facts["start_line"], req.facts["end_line"]) == (START_QUOTE, END_QUOTE)
    # The order of the answers makes no difference.
    reordered = answered_brief(
        the_brief, {"choice_answers": answers[::-1], "target_duration_s": 12.9}
    )
    assert reordered.live()[0].facts == req.facts


# == 10. the CLI verifier ===================================================================


def test_the_verifier_cli_reads_the_picks(tmp_path: Path) -> None:
    from app.cli.verify_take_window import main  # noqa: PLC0415

    def write(name: str, facts: dict) -> Path:
        take = {
            "duration_s": TAKE_S,
            "words": WORDS,
            "segments": [seg.model_dump() for seg in words_to_segments(WORDS)],
            "brief_v2": {
                "r1": {"id": "r1", "kind": "timing", "scope": "global", "description": "x",
                       "facts": facts},
            },
        }  # fmt: skip
        path = tmp_path / name
        path.write_text(json.dumps(take, ensure_ascii=False), encoding="utf-8")
        return path

    quotes = {"start_line": START_QUOTE, "end_line": END_QUOTE}
    picks = {START_SPOKEN_FACT: START_PICK, END_SPOKEN_FACT: "Gracias"}
    with_picks = tmp_path / "with"
    assert (
        main(["--take", str(write("a.json", {**quotes, **picks})), "--out", str(with_picks)]) == 0
    )
    report = json.loads((with_picks / "report.json").read_text(encoding="utf-8"))
    assert report["verdict"] == "PASS" and report["keep_segments"] == [[4.0, 11.71]]
    without = tmp_path / "without"
    assert main(["--take", str(write("b.json", quotes)), "--out", str(without)]) == 1
    assert json.loads((without / "report.json").read_text(encoding="utf-8"))["verdict"] == "FAIL"


# == 11. a later plain message is not a re-answer ============================================


@pytest.mark.parametrize("message", ["Lucas", "Gracias", "salsas criollas,", "none of these"])
def test_a_later_plain_message_never_re_answers_a_closed_take_line_card(message: str) -> None:
    events, res = answered(START_PICK, "Gracias")
    assert res.question is None
    assert match_answered_choice(events, message) is None
