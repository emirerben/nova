"""KRI-588: the chat question "which part of the song did you film?" -- rules and failure modes."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from app.schemas.user_song import (
    PlacementCandidate,
    SongAlignment,
    SongRangeAnswerIn,
    SongRangeQuestion,
    TakeAlignment,
)
from app.services import song_range as sr
from app.services.song_order import (
    SONG_ORDER_ANSWER_MESSAGE,
    SONG_RANGE_ANSWER_MESSAGE,
    model_text,
    thread_keeps_lipsync,
)

GEN = 3


def _take(mid: str, *cands: tuple[float, float]) -> TakeAlignment:
    return TakeAlignment(
        media_id=mid,
        status="ambiguous",
        delta_s=cands[0][0],
        confidence=cands[0][1],
        likelihood=cands[0][1],
        candidates=[
            PlacementCandidate(delta_s=d, likelihood=li, method="lyrics") for d, li in cands
        ],
    )


def _alignment(*takes: TakeAlignment) -> SongAlignment:
    return SongAlignment(song_generation=GEN, takes={t.media_id: t for t in takes})


def _asked(qid: str = "q1", generation: int | None = GEN) -> tuple[str, dict]:
    return (
        "assistant",
        {"song_range_question": {"question_id": qid, "song_generation": generation}},
    )


def _answered(qid: str = "q1", start: float | None = 40.0, end: float | None = 80.0):
    return ("user", {"song_range": {"question_id": qid, "start_s": start, "end_s": end}})


# -- which takes are ambiguous between repeats -----


def test_only_a_second_plausible_place_far_away_counts_as_a_repeat() -> None:
    alignment = _alignment(
        _take("repeat", (99.0, 0.98), (145.0, 0.31)),  # 46 s apart: a verse repeat
        _take("same_line", (99.0, 0.98), (102.0, 0.9)),  # 3 s apart: the same line
        _take("noise", (99.0, 0.98), (145.0, 0.05)),  # far but implausible
        _take("single", (99.0, 0.98)),
        TakeAlignment(media_id="none", status="unmatched"),
    )
    ids = ["repeat", "same_line", "noise", "single", "none", "missing"]
    assert sr.takes_ambiguous_between_repeats(alignment, ids) == ["repeat"]


def test_the_boundaries_are_inclusive_of_exactly_15_seconds_and_a_fifth_likelihood() -> None:
    exactly = _alignment(_take("t", (50.0, 0.9), (65.0, 0.2)))
    assert sr.takes_ambiguous_between_repeats(exactly, ["t"]) == ["t"]
    just_under = _alignment(_take("t", (50.0, 0.9), (64.9, 0.2)))
    assert sr.takes_ambiguous_between_repeats(just_under, ["t"]) == []


# -- the stored range -----


@pytest.mark.parametrize(
    ("start", "end", "song", "expected"),
    [
        (None, None, 200.0, None),  # whole song
        (40.0, 80.0, 200.0, (40.0, 80.0)),
        (150.0, 400.0, 200.0, (150.0, 200.0)),  # an end past the song is clamped
        (10.0, 15.0, 200.0, (10.0, 15.0)),  # exactly the 5 s minimum
        (40.0, 80.0, None, (40.0, 80.0)),  # unknown song length: trust the numbers
    ],
)
def test_normalized_range(start, end, song, expected) -> None:
    assert sr.normalized_range(start, end, song) == expected


@pytest.mark.parametrize(
    ("start", "end", "song"),
    [
        (40.0, None, 200.0),
        (None, 80.0, 200.0),
        (10.0, 14.9, 200.0),  # under five seconds
        (250.0, 300.0, 200.0),  # starts after the song ends
        (197.0, 400.0, 200.0),  # clamped to 3 s: too short
    ],
)
def test_unusable_ranges_are_refused_with_the_invalid_code(start, end, song) -> None:
    with pytest.raises(sr.SongRangeError) as err:
        sr.normalized_range(start, end, song)
    assert err.value.code == sr.INVALID_CODE


def test_the_answer_model_rejects_half_a_range_and_backwards_ranges() -> None:
    with pytest.raises(ValidationError):
        SongRangeAnswerIn(question_id="q", start_s=10.0)
    with pytest.raises(ValidationError):
        SongRangeAnswerIn(question_id="q", start_s=50.0, end_s=40.0)
    with pytest.raises(ValidationError):
        SongRangeAnswerIn(question_id="q", start_s=float("nan"), end_s=40.0)
    assert SongRangeAnswerIn(question_id="q").start_s is None  # the whole song


def test_apply_sets_and_clears_the_item_and_mirrors_the_thread_song_entry() -> None:
    item = SimpleNamespace(song_range_start_s=None, song_range_end_s=None)
    thread = SimpleNamespace(
        state={"media": [{"role": "song", "media_id": "s"}, {"role": "clip", "media_id": "c"}]}
    )
    sr.apply_song_range(thread, item, (40.0, 80.0))
    assert (item.song_range_start_s, item.song_range_end_s) == (40.0, 80.0)
    song, clip = thread.state["media"]
    assert song["song_range"] == {"start_s": 40.0, "end_s": 80.0}
    assert "song_range" not in clip
    sr.apply_song_range(thread, item, None)
    assert (item.song_range_start_s, item.song_range_end_s) == (None, None)
    assert "song_range" not in thread.state["media"][0]


# -- the question / answer exchange -----


def test_asked_generations_remember_every_question_even_unanswered_ones() -> None:
    fold = sr.fold_song_range_events([_asked("q1", 2), _asked("q2", 3), ("user", {"x": 1})])
    assert fold.asked_generations == frozenset({2, 3})
    assert sr.fold_song_range_events([]).asked_generations == frozenset()


def test_the_open_question_is_the_latest_unanswered_one() -> None:
    assert sr.latest_open_song_range_question([]) is None
    open_q = sr.latest_open_song_range_question([_asked("q1")])
    assert open_q is not None and open_q.question_id == "q1"
    assert sr.latest_open_song_range_question([_asked("q1"), _answered("q1")]) is None
    reasked = sr.latest_open_song_range_question([_asked("q1"), _answered("q1"), _asked("q2")])
    assert reasked is not None and reasked.question_id == "q2"
    # An answer to some other question does not close this one.
    other = sr.latest_open_song_range_question([_asked("q1"), _answered("zzz")])
    assert other is not None and other.question_id == "q1"


def test_a_malformed_stored_question_is_ignored_not_fatal() -> None:
    bad = ("assistant", {"song_range_question": {"song_generation": 3}})  # no question_id
    assert sr.latest_open_song_range_question([bad]) is None
    assert sr.fold_song_range_events([bad]).asked_generations == frozenset()


def test_validate_answer_needs_the_open_question_and_returns_the_range() -> None:
    question = SongRangeQuestion(question_id="q1", song_generation=GEN, song_duration_s=200.0)
    ok = SongRangeAnswerIn(question_id="q1", start_s=40.0, end_s=80.0)
    assert sr.validate_song_range_answer(ok, question) == (40.0, 80.0)
    assert sr.validate_song_range_answer(SongRangeAnswerIn(question_id="q1"), question) is None
    with pytest.raises(sr.SongRangeError) as stale:
        sr.validate_song_range_answer(ok, None)
    assert stale.value.code == sr.STALE_CODE
    with pytest.raises(sr.SongRangeError) as wrong:
        sr.validate_song_range_answer(SongRangeAnswerIn(question_id="old"), question)
    assert wrong.value.code == sr.STALE_CODE
    too_short = SongRangeAnswerIn(question_id="q1", start_s=10.0, end_s=12.0)
    with pytest.raises(sr.SongRangeError) as short:
        sr.validate_song_range_answer(too_short, question)
    assert short.value.code == sr.INVALID_CODE


# -- the model never sees the structured answer as a request; lip-sync survives the exchan -----


def test_the_range_answer_reads_as_neutral_text_to_the_model() -> None:
    payload = {"song_range": {"question_id": "q1", "start_s": 40.0, "end_s": 80.0}}
    text = model_text(payload, "I filmed from 0:40 to 1:20")
    assert text == SONG_RANGE_ANSWER_MESSAGE != SONG_ORDER_ANSWER_MESSAGE
    assert model_text({"song_order": {"question_id": "q"}}, "x") == SONG_ORDER_ANSWER_MESSAGE
    assert model_text(None, "just words") == "just words"


def test_lipsync_is_kept_while_the_range_question_is_open_and_on_the_answer_turn() -> None:
    assert thread_keeps_lipsync([_asked()], GEN) is True  # open question
    assert thread_keeps_lipsync([_asked(), _answered()], GEN) is True  # the answer turn
    # The creator moved on with a free-text request: lip-sync is the model's call again.
    moved_on = [_asked(), _answered(), ("assistant", {}), ("user", {"text": "make it shorter"})]
    assert thread_keeps_lipsync(moved_on, GEN) is False
    # A question about a different song generation is not ours.
    assert thread_keeps_lipsync([_asked(generation=GEN - 1)], GEN) is False
    assert thread_keeps_lipsync([], GEN) is False
