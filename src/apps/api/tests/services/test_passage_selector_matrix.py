"""KRI-1555 / C1: which passages of a spoken take stay, with the length enforced.

Failure modes this matrix covers, each against the REAL `select_passages` and the REAL
`PassageSelectorAgent` (prompt rendering, parser, retry rules). Only the provider is
replayed: each case carries the model's recorded answer, authored by hand from the
transcript. The expectations come from topic labels written next to each sentence and from
the word timings, never from the selector's own output.

1. The length target is missed (the old planner only added a "shorter than" note).
2. The strongest passage moves later in the source and the selection does not follow
   (the old planner never saw sentence 41 onward).
3. The outline changes and the selection does not.
4. A fluent off-topic passage that shares the outline's keywords is kept.
5. A boundary falls inside a word.
6. A long source is read in windows and the join between two windows breaks a passage.
7. Turkish text does not ground (dotted/dotless i, accents).
8. A teleprompter take keeps the stumble instead of the clean retake, or a script line twice.
9. The model points at the wrong sentence ids.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from app.agents._runtime import ModelClient, ModelInvocation, TerminalSchemaError
from app.agents.passage_selector import (
    PassageSelectorAgent,
    PassageSelectorInput,
    PassageSelectorOutput,
)
from app.services.passage_selector import (
    PassageSelection,
    PassageSelectionError,
    SelectionAsk,
    select_passages,
)
from tests.evals.runners.eval_runner import build_eval_run_context

WORD_S = 0.30
WORD_GAP_S = 0.12
SENTENCE_GAP_S = 0.55


@dataclass(frozen=True)
class Line:
    text: str
    topic: str


@dataclass
class Talk:
    words: list[dict]
    spans: list[tuple[float, float]]
    lines: list[Line]


def build_talk(lines: list[Line], *, gaps: dict[int, float] | None = None, word_gap=WORD_GAP_S):
    """A whisper-shaped transcript: one sentence per Line, timed, with a pause after it."""
    words: list[dict] = []
    spans: list[tuple[float, float]] = []
    clock = 0.5
    for index, line in enumerate(lines):
        start = clock
        for token in line.text.split():
            words.append(
                {"text": token, "start_s": round(clock, 3), "end_s": round(clock + WORD_S, 3)}
            )
            clock += WORD_S + word_gap
        clock -= word_gap
        spans.append((start, clock))
        clock += (gaps or {}).get(index, SENTENCE_GAP_S)
    return Talk(words, spans, lines)


LABELED_TALKS = Path(__file__).parents[1] / "fixtures" / "passage_selector_labeled_talks.json"


def labeled(name: str) -> list[Line]:
    """Transcripts whose topic labels were written by hand, apart from any recorded answer."""
    return [Line(row["text"], row["topic"]) for row in json.loads(LABELED_TALKS.read_text())[name]]


class Replay(ModelClient):
    """Answers each call with the next recorded response; keeps every prompt it was sent."""

    def __init__(self, *raw: str) -> None:
        self.raw = list(raw)
        self.prompts: list[str] = []

    def invoke(self, *, prompt: str, **_: object) -> ModelInvocation:
        self.prompts.append(prompt)
        if len(self.prompts) > len(self.raw):
            raise AssertionError("the selector called the model more often than recorded")
        return ModelInvocation(
            raw_text=self.raw[len(self.prompts) - 1], tokens_in=0, tokens_out=0, raw_response=None
        )


@dataclass
class Run:
    selection: PassageSelection
    client: Replay
    inputs: list[PassageSelectorInput] = field(default_factory=list)


def recorded(talk: Talk, picks=(), drops=()) -> str:
    """The model's answer: (first, last, strength, reason) picks and (first, last, reason) drops."""

    def quote(first: int, last: int) -> str:
        head, tail = talk.lines[first].text, talk.lines[last].text
        return head if first == last else f"{head} ... {tail}"

    return json.dumps(
        {
            "passages": [
                {"first": a, "last": b, "quote": quote(a, b), "reason": why, "strength": s}
                for a, b, s, why in picks
            ],
            "dropped": [{"first": a, "last": b, "reason": why} for a, b, why in drops],
        },
        ensure_ascii=False,
    )


def run(talk: Talk, ask: SelectionAsk, *raw: str, **kwargs) -> Run:
    client = Replay(*raw)
    agent = PassageSelectorAgent(client)
    inputs: list[PassageSelectorInput] = []

    def run_agent(selector_input: PassageSelectorInput):
        inputs.append(selector_input)
        return agent.run(selector_input, ctx=build_eval_run_context("matrix", is_live=False))

    selection = select_passages(talk.words, ask, run_agent=run_agent, **kwargs)
    return Run(selection, client, inputs)


def sentences_in(talk: Talk, start: float, end: float) -> list[int]:
    return [i for i, (a, b) in enumerate(talk.spans) if start <= (a + b) / 2 <= end]


def kept_sentences(talk: Talk, selection: PassageSelection) -> list[int]:
    return [i for p in selection.kept for i in sentences_in(talk, p.start_s, p.end_s)]


def assert_boundaries_between_words(talk: Talk, selection: PassageSelection) -> None:
    edges = [t for p in (*selection.kept, *selection.left_out) for t in (p.start_s, p.end_s)]
    for edge in edges:
        inside = [w for w in talk.words if w["start_s"] + 1e-9 < edge < w["end_s"] - 1e-9]
        assert not inside, f"boundary {edge} is inside {inside[0]['text']!r}"


def assert_partition(talk: Talk, selection: PassageSelection) -> None:
    """Every sentence is either kept or listed as left out, never both, and kept never overlaps."""
    kept = kept_sentences(talk, selection)
    left = [i for p in selection.left_out for i in sentences_in(talk, p.start_s, p.end_s)]
    assert sorted(kept + left) == list(range(len(talk.lines)))
    for earlier, later in zip(selection.kept, selection.kept[1:], strict=False):
        assert earlier.end_s <= later.start_s


def assert_on_topic(talk: Talk, selection: PassageSelection, allowed: set[str]) -> None:
    """The independent oracle: every kept sentence carries a topic the creator asked for."""
    off = [i for i in kept_sentences(talk, selection) if talk.lines[i].topic not in allowed]
    assert not off, f"kept off-topic sentences {off}: {[talk.lines[i].text for i in off]}"


TRIP = labeled("trip")
TRIP_PICKS = [
    (0, 0, 4, "opens on the trip"),
    (1, 2, 5, "the espresso moment"),
    (7, 8, 5, "the tram and the guitar"),
    (10, 10, 3, "the dinner"),
    (12, 12, 2, "the map"),
    (13, 13, 3, "leaving"),
    (15, 15, 4, "wants to go back"),
]
TRIP_DROPS = [
    (3, 4, "off_topic"),
    (5, 6, "off_topic"),
    (9, 9, "off_topic"),
    (11, 11, "off_topic"),
    (14, 14, "off_topic"),
]


def trip_answer(talk: Talk) -> str:
    return recorded(talk, TRIP_PICKS, TRIP_DROPS)


@pytest.mark.parametrize(
    ("target", "ceiling", "low", "high"),
    [(30.0, False, 27.0, 33.0), (45.0, False, 40.5, 49.5), (20.0, True, 0.0, 20.0)],
    ids=["thirty", "forty_five", "at_most_twenty"],
)
def test_asked_length_is_enforced_on_every_target(target, ceiling, low, high):
    talk = build_talk(TRIP)
    candidates = sum(
        talk.spans[i][1] - talk.spans[i][0] for a, b, *_ in TRIP_PICKS for i in range(a, b + 1)
    )
    result = run(
        talk, SelectionAsk("make it shorter", target, ceiling), trip_answer(talk)
    ).selection
    assert low <= result.kept_s <= high
    assert result.fits_target
    assert_on_topic(talk, result, {"trip"})
    assert_partition(talk, result)
    assert_boundaries_between_words(talk, result)
    if target < candidates * 0.9:
        assert result.kept_s < candidates, "the model's candidates were passed through untrimmed"
        assert {p.reason for p in result.left_out} >= {"over_length", "off_topic"}


def test_trimming_drops_the_weakest_passages_first():
    talk = build_talk(TRIP)
    result = run(talk, SelectionAsk("shorter", 30.0), trip_answer(talk)).selection
    kept = set(kept_sentences(talk, result))
    assert {1, 2, 7, 8} <= kept, "strength-5 passages must survive the trim"
    assert 12 not in kept, "the strength-2 passage goes first"


def test_keep_whole_talk_never_calls_the_model():
    talk = build_talk(TRIP)
    result = run(talk, SelectionAsk("keep the whole talk", keep_whole=True))
    assert result.client.prompts == []
    assert len(result.selection.kept) == 1
    first, last = talk.words[0], talk.words[-1]
    kept = result.selection.kept[0]
    assert kept.start_s <= first["start_s"] and kept.end_s >= last["end_s"]
    assert result.selection.left_out == ()
    assert_boundaries_between_words(talk, result.selection)


def test_keep_whole_but_drop_a_topic_removes_only_that_topic():
    talk = build_talk(TRIP)
    answer = recorded(
        talk, drops=[(5, 6, "requested_drop"), (9, 9, "requested_drop"), (14, 14, "requested_drop")]
    )
    result = run(
        talk,
        SelectionAsk(
            "keep the whole talk but drop the part about the startup",
            keep_whole=True,
            drop_topics=("the startup",),
        ),
        answer,
    )
    kept = set(kept_sentences(talk, result.selection))
    assert kept == {i for i, line in enumerate(TRIP) if line.topic != "startup"}
    assert {p.reason for p in result.selection.left_out} == {"requested_drop"}
    assert result.inputs[0].mode == "drop"
    assert_partition(talk, result.selection)
    assert_boundaries_between_words(talk, result.selection)


def test_a_drop_request_without_a_length_keeps_everything_else():
    talk = build_talk(TRIP)
    answer = recorded(talk, drops=[(5, 6, "requested_drop")])
    result = run(
        talk, SelectionAsk("drop the part about invoices", drop_topics=("invoices",)), answer
    )
    assert result.inputs[0].mode == "drop"
    assert 5 not in kept_sentences(talk, result.selection)
    assert len(kept_sentences(talk, result.selection)) == len(TRIP) - 2


def filler_talk(count: int, key_at: int) -> Talk:
    lines = [
        Line(f"Filler thought number {i} about nothing in particular.", "filler")
        for i in range(count)
    ]
    lines[key_at] = Line("The one thing I learned is never rush a good espresso.", "key")
    lines[key_at + 1] = Line("Honestly that single rule changed my whole morning routine.", "key")
    return build_talk(lines)


@pytest.mark.parametrize("key_at", [3, 55])
def test_the_strongest_passage_is_found_wherever_it_sits_in_a_long_talk(key_at):
    talk = filler_talk(60, key_at)
    answer = recorded(talk, [(key_at, key_at + 1, 5, "the point"), (0, 0, 1, "filler")])
    result = run(talk, SelectionAsk("find the best part", 10.0), answer)
    assert [u.id for u in result.inputs[0].units] == list(range(60)), "the model saw every sentence"
    assert "Filler thought number 59" in result.client.prompts[0]
    assert kept_sentences(talk, result.selection) == [key_at, key_at + 1]
    assert_on_topic(talk, result.selection, {"key"})
    assert result.selection.kept[0].start_s == pytest.approx(talk.spans[key_at][0], abs=0.1)


def test_the_selection_moves_with_the_strongest_passage():
    early, late = filler_talk(60, 3), filler_talk(60, 55)
    ask = SelectionAsk("find the best part", 10.0)
    a = run(early, ask, recorded(early, [(3, 4, 5, "the point")])).selection.kept[0]
    b = run(late, ask, recorded(late, [(55, 56, 5, "the point")])).selection.kept[0]
    assert a.start_s == pytest.approx(early.spans[3][0], abs=0.1)
    assert b.start_s == pytest.approx(late.spans[55][0], abs=0.1)
    assert b.start_s - a.start_s > 150


def test_a_different_outline_selects_different_passages_and_reaches_the_prompt():
    talk = build_talk(TRIP)
    tram = "the yellow tram and the guitar at the top of the hill"
    sardines = "the grilled sardine dinner by the river and the hostel map"
    first = run(
        talk, SelectionAsk("follow my notes", outline=tram), recorded(talk, [(7, 8, 5, "tram")])
    )
    second = run(
        talk,
        SelectionAsk("follow my notes", outline=sardines),
        recorded(talk, [(10, 10, 5, "dinner"), (12, 12, 4, "map")]),
    )
    assert kept_sentences(talk, first.selection) == [7, 8]
    assert kept_sentences(talk, second.selection) == [10, 12]
    assert tram in first.client.prompts[0] and sardines not in first.client.prompts[0]
    assert sardines in second.client.prompts[0]
    assert "$" not in first.client.prompts[0].split("=== Transcript")[0]


DECOY = labeled("decoy")
OUTLINE = "My trip to Lisbon: the espresso at the hostel, the tram ride, the guitar."


def test_a_fluent_off_topic_passage_sharing_the_outline_keywords_is_left_out():
    talk = build_talk(DECOY)
    answer = recorded(
        talk, [(0, 0, 5, "the espresso"), (3, 3, 4, "the tram")], [(1, 2, "off_topic")]
    )
    result = run(talk, SelectionAsk("follow my trip notes", outline=OUTLINE), answer)
    assert_on_topic(talk, result.selection, {"trip"})
    assert [(p.reason) for p in result.selection.left_out] == ["off_topic"]
    assert sentences_in(
        talk, result.selection.left_out[0].start_s, result.selection.left_out[0].end_s
    ) == [1, 2]


def test_wrong_content_control_the_oracle_rejects_a_selection_that_keeps_the_decoy():
    """A planner answer that keeps the keyword-sharing decoy is grounded, fluent and in order.

    The selector cannot tell it apart offline (that judgement is the model's, pending a live
    run), but the independent topic oracle must refuse it, or this matrix proves nothing.
    """
    talk = build_talk(DECOY)
    answer = recorded(
        talk, [(0, 0, 5, "the espresso"), (1, 2, 5, "espresso machines"), (3, 3, 4, "the tram")]
    )
    wrong = run(talk, SelectionAsk("follow my trip notes", outline=OUTLINE), answer).selection
    assert kept_sentences(talk, wrong) == [0, 1, 2, 3]
    with pytest.raises(AssertionError, match="off-topic"):
        assert_on_topic(talk, wrong, {"trip"})


def test_ids_that_point_at_other_speech_are_rejected_as_ungrounded():
    talk = build_talk(DECOY)
    wrong_ids = json.dumps(
        {
            "passages": [
                {"first": 1, "last": 2, "quote": DECOY[0].text, "reason": "x", "strength": 5},
                {
                    "first": 3,
                    "last": 3,
                    "quote": DECOY[3].text,
                    "reason": "the tram",
                    "strength": 3,
                },
            ],
            "dropped": [],
        }
    )
    result = run(talk, SelectionAsk("follow my trip notes", outline=OUTLINE), wrong_ids).selection
    assert kept_sentences(talk, result) == [3]
    assert "ungrounded" in {p.reason for p in result.left_out}


def test_a_quote_that_was_never_said_is_not_kept():
    talk = build_talk(DECOY)
    invented = json.dumps(
        {
            "passages": [
                {
                    "first": 0,
                    "last": 0,
                    "quote": "Completely invented sentence about nothing here",
                    "reason": "x",
                    "strength": 5,
                },
                {
                    "first": 3,
                    "last": 3,
                    "quote": DECOY[3].text,
                    "reason": "the tram",
                    "strength": 3,
                },
            ]
        }
    )
    result = run(talk, SelectionAsk("notes", outline=OUTLINE), invented).selection
    assert kept_sentences(talk, result) == [3]


def test_boundaries_stay_between_words_when_whisper_leaves_almost_no_gap():
    tight = build_talk(TRIP, gaps={i: 0.02 for i in range(len(TRIP))}, word_gap=0.0)
    result = run(tight, SelectionAsk("shorter", 30.0), trip_answer(tight)).selection
    assert_boundaries_between_words(tight, result)
    assert_partition(tight, result)


def test_a_long_talk_is_read_in_windows_and_a_passage_across_the_seam_stays_whole():
    talk = filler_talk(60, 22)
    ask = SelectionAsk("find the best part", 12.0)
    seen: list[PassageSelectorInput] = []

    def probe(selector_input: PassageSelectorInput) -> PassageSelectorOutput:
        seen.append(selector_input)
        return PassageSelectorOutput()

    with pytest.raises(PassageSelectionError):
        select_passages(talk.words, ask, run_agent=probe, window_chars=2400)
    windows = [[u.id for u in i.units] for i in seen]
    assert len(windows) >= 3
    assert sum(windows, []) == list(range(60))
    seam = windows[0][-1]
    assert seam == 22, "the key sentences must straddle the first window seam"
    answers = [recorded(talk) for _ in windows]
    answers[0] = recorded(talk, [(seam, seam, 5, "the point, first half")])
    answers[1] = recorded(talk, [(seam + 1, seam + 1, 5, "the point, second half")])
    result = run(talk, ask, *answers, window_chars=2400)
    assert [i.window_index for i in result.inputs] == list(range(len(windows)))
    assert kept_sentences(talk, result.selection) == [seam, seam + 1]
    assert len(result.selection.kept) == 1, "the two halves must merge into one passage"
    assert not result.selection.fits_target, "nothing else on-topic exists to reach 12 seconds"
    assert_boundaries_between_words(talk, result.selection)


TURKISH = labeled("turkish")


def test_turkish_talk_with_a_turkish_outline_and_a_keyword_sharing_decoy():
    talk = build_talk(TURKISH)
    outline = "Kapadokya gezisi: balon turu ve manzara"
    answer = recorded(
        talk,
        [(0, 1, 4, "geziye başlangıç"), (4, 5, 5, "manzara ve dönüş")],
        [(2, 3, "off_topic"), (6, 6, "off_topic")],
    )
    result = run(talk, SelectionAsk("notlarımı izle", outline=outline), answer, language="tr")
    assert kept_sentences(talk, result.selection) == [0, 1, 4, 5]
    assert_on_topic(talk, result.selection, {"trip"})
    assert outline in result.client.prompts[0]
    assert {p.reason for p in result.selection.left_out} == {"off_topic"}
    assert_boundaries_between_words(talk, result.selection)


TELEPROMPTER = labeled("teleprompter")
SCRIPT = (
    "Merhaba arkadaşlar bugün size yeni telefonumu tanıtacağım.\n"
    "Önce ekranına ve kamerasına birlikte bakalım.\n"
    "Pil ömrü gerçekten bütün gün dayanıyor."
)


def test_teleprompter_take_keeps_each_script_line_once_from_its_clean_retake():
    talk = build_talk(TELEPROMPTER, gaps={0: 2.5, 1: 0.9, 2: 3.0})
    answer = recorded(
        talk, [(1, 1, 5, "line 1"), (3, 3, 5, "line 2, clean retake"), (4, 4, 5, "line 3")]
    )
    result = run(talk, SelectionAsk("", outline=SCRIPT), answer, language="tr")
    assert kept_sentences(talk, result.selection) == [1, 3, 4]
    assert_on_topic(talk, result.selection, {"line1", "line2", "line3"})
    assert [p.reason for p in result.selection.left_out] == ["not_selected", "not_selected"]
    assert_partition(talk, result.selection)
    assert_boundaries_between_words(talk, result.selection)


def test_a_target_shorter_than_any_sentence_refuses_instead_of_cutting_mid_sentence():
    talk = build_talk(TRIP)
    with pytest.raises(PassageSelectionError):
        run(talk, SelectionAsk("tiny", 2.0, True), trip_answer(talk))


def test_nothing_grounded_is_an_error_not_an_empty_cut():
    talk = build_talk(DECOY)
    nonsense = json.dumps(
        {
            "passages": [
                {"first": 0, "last": 0, "quote": "words nobody ever said aloud", "strength": 3}
            ]
        }
    )
    with pytest.raises(PassageSelectionError):
        run(talk, SelectionAsk("notes", outline=OUTLINE), nonsense)


def test_ids_outside_the_units_shown_fail_the_agent_so_it_retries():
    talk = build_talk(DECOY)
    bad = json.dumps(
        {"passages": [{"first": 0, "last": 99, "quote": DECOY[0].text, "strength": 3}]}
    )
    with pytest.raises(TerminalSchemaError):
        run(talk, SelectionAsk("notes", outline=OUTLINE), bad, bad)


def test_max_passage_length_splits_at_sentence_boundaries():
    talk = build_talk(TRIP)
    answer = recorded(talk, [(0, 2, 5, "the opening")])
    result = run(talk, SelectionAsk("highlight"), answer, max_passage_s=8.0).selection
    assert len(result.kept) == 3
    assert all(p.end_s - p.start_s <= 8.0 for p in result.kept)
    assert_boundaries_between_words(talk, result)


def test_a_single_window_must_name_a_passage_but_one_window_of_many_may_not():
    talk = build_talk(DECOY)
    with pytest.raises(TerminalSchemaError):
        run(talk, SelectionAsk("notes", outline=OUTLINE), recorded(talk), recorded(talk))
