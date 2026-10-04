"""KRI-374 lane E: song-order question builder, fold, validation, resolution."""

from __future__ import annotations

import pytest

from app.schemas.user_song import (
    SONG_ALIGNMENT_VERSION,
    AlignmentAlternate,
    SongAlignment,
    SongOrderAnswerIn,
    SongOrderQuestion,
    TakeAlignment,
)
from app.services.song_order import (
    INVALID_CODE,
    STALE_CODE,
    SongOrderError,
    build_song_order_question,
    fold_song_orders,
    latest_open_song_order_question,
    load_ready_alignment,
    resolve_uncertain_takes,
    resolved_song_takes_payload,
    uncertain_media_ids,
    validate_song_order_answer,
)


def _confident(mid: str, delta: float) -> TakeAlignment:
    return TakeAlignment(media_id=mid, status="confident", delta_s=delta, confidence=0.9)


def _ambiguous(mid: str, delta: float | None, alts: list[tuple[float, float]]) -> TakeAlignment:
    return TakeAlignment(
        media_id=mid,
        status="ambiguous",
        delta_s=delta,
        alternates=[AlignmentAlternate(delta_s=d, score=s) for d, s in alts],
    )


def _alignment(*takes: TakeAlignment) -> SongAlignment:
    return SongAlignment(song_generation=3, takes={t.media_id: t for t in takes})


# -- build ---------------------------------------------------------------------


def test_question_orders_by_song_position_with_unmatched_last() -> None:
    alignment = _alignment(
        _confident("late", 50.0),
        TakeAlignment(media_id="silent", status="unmatched"),
        _ambiguous("chorus", 20.0, [(20.0, 0.9), (80.0, 0.8)]),
        _confident("early", 5.0),
    )
    q = build_song_order_question(
        alignment, ["late", "silent", "chorus", "early"], question_id="q1"
    )
    assert q.question_id == "q1"
    assert q.proposed_order == ["early", "chorus", "late", "silent"]
    assert [i.media_id for i in q.items] == q.proposed_order
    by_id = {i.media_id: i for i in q.items}
    assert by_id["early"].song_start_s == 5.0 and by_id["early"].alternates == []
    assert by_id["chorus"].status == "ambiguous"
    assert [a.delta_s for a in by_id["chorus"].alternates] == [20.0, 80.0]
    assert by_id["silent"].song_start_s is None and by_id["silent"].status == "unmatched"


def test_missing_alignment_row_counts_as_unmatched_and_ids_are_deduped() -> None:
    alignment = _alignment(_confident("a", 1.0))
    q = build_song_order_question(alignment, ["a", "b", "b"])
    assert q.proposed_order == ["a", "b"]
    assert q.items[1].status == "unmatched"
    assert uncertain_media_ids(alignment, ["a", "b"]) == ["b"]


def test_question_caps_alternates_and_generates_an_id() -> None:
    alts = [(float(i), 0.5) for i in range(7)]
    q = build_song_order_question(_alignment(_ambiguous("x", 0.0, alts)), ["x"])
    assert len(q.items[0].alternates) == 4
    assert q.question_id


def test_all_confident_has_no_uncertain_takes() -> None:
    alignment = _alignment(_confident("a", 1.0), _confident("b", 9.0))
    assert uncertain_media_ids(alignment, ["a", "b"]) == []


# -- alignment readiness ---------------------------------------------------------


def test_load_ready_alignment_accepts_current_complete_rows() -> None:
    alignment = _alignment(_confident("a", 1.0), _confident("b", 2.0))
    raw = alignment.model_dump(mode="json")
    got = load_ready_alignment(raw, media_ids=["a", "b"], song_generation=3)
    assert got is not None and set(got.takes) == {"a", "b"}


@pytest.mark.parametrize(
    "mutate",
    [
        lambda raw: None,  # missing
        lambda raw: {**raw, "song_generation": 2},  # stale generation
        lambda raw: {**raw, "version": SONG_ALIGNMENT_VERSION + 1},  # old/new algorithm
        lambda raw: {**raw, "takes": {"a": raw["takes"]["a"]}},  # incomplete
        lambda raw: {"garbage": True},  # unparseable
    ],
)
def test_load_ready_alignment_pending_when_missing_stale_or_incomplete(mutate) -> None:
    raw = _alignment(_confident("a", 1.0), _confident("b", 2.0)).model_dump(mode="json")
    assert load_ready_alignment(mutate(raw), media_ids=["a", "b"], song_generation=3) is None


def test_failed_song_analysis_yields_all_unmatched_instead_of_waiting_forever() -> None:
    got = load_ready_alignment(
        None, media_ids=["a", "b"], song_generation=3, raw_analysis={"status": "failed"}
    )
    assert got is not None
    assert {t.status for t in got.takes.values()} == {"unmatched"}


# -- events: open question + fold --------------------------------------------------


def _q(qid: str, ids: list[str]) -> dict:
    q = build_song_order_question(
        _alignment(*[TakeAlignment(media_id=i, status="unmatched") for i in ids]),
        ids,
        question_id=qid,
    )
    return {"song_order_question": q.model_dump(mode="json")}


def test_latest_open_question_is_cleared_by_its_answer() -> None:
    events = [("assistant", _q("q1", ["a", "b"]))]
    assert latest_open_song_order_question(events).question_id == "q1"
    events.append(("user", {"song_order": {"question_id": "q1", "ordered_media_ids": ["b", "a"]}}))
    assert latest_open_song_order_question(events) is None
    events.append(("assistant", _q("q2", ["a", "b"])))
    assert latest_open_song_order_question(events).question_id == "q2"


def test_answer_to_another_question_does_not_close_the_open_one() -> None:
    events = [
        ("assistant", _q("q1", ["a", "b"])),
        ("user", {"song_order": {"question_id": "other", "ordered_media_ids": ["a", "b"]}}),
    ]
    assert latest_open_song_order_question(events).question_id == "q1"


def test_malformed_question_payloads_are_ignored() -> None:
    assert (
        latest_open_song_order_question([("assistant", {"song_order_question": {"x": 1}})]) is None
    )
    assert latest_open_song_order_question([("user", None), ("assistant", None)]) is None


def test_fold_latest_answer_wins() -> None:
    events = [
        ("assistant", _q("q1", ["a", "b", "c"])),
        ("user", {"song_order": {"question_id": "q1", "ordered_media_ids": ["a", "b", "c"]}}),
        ("assistant", _q("q2", ["a", "b", "c"])),
        ("user", {"song_order": {"question_id": "q2", "ordered_media_ids": ["c", "a", "b"]}}),
    ]
    folded = fold_song_orders(events)
    assert folded.ordered_media_ids == ("c", "a", "b") and folded.question_id == "q2"
    assert folded.covers(["a", "b", "c"])
    assert not folded.covers(["a", "b", "c", "d"])  # a new take invalidates it


def test_fold_ignores_unknown_question_and_mismatched_ids() -> None:
    events = [
        ("user", {"song_order": {"question_id": "ghost", "ordered_media_ids": ["a"]}}),
        ("assistant", _q("q1", ["a", "b"])),
        ("user", {"song_order": {"question_id": "q1", "ordered_media_ids": ["a", "z"]}}),
        ("user", {"song_order": {"question_id": "q1", "ordered_media_ids": ["a", "a"]}}),
    ]
    assert not fold_song_orders(events)


# -- validate ---------------------------------------------------------------------


def _open(ids: list[str]) -> SongOrderQuestion:
    return SongOrderQuestion.model_validate(_q("q1", ids)["song_order_question"])


def test_validate_accepts_a_permutation_of_the_asked_takes() -> None:
    validate_song_order_answer(
        SongOrderAnswerIn(question_id="q1", ordered_media_ids=["b", "a"]), _open(["a", "b"])
    )


@pytest.mark.parametrize(
    ("answer_ids", "qid", "open_q", "code"),
    [
        (["a", "b"], "q1", None, STALE_CODE),
        (["a", "b"], "nope", _open(["a", "b"]), STALE_CODE),
        (["a", "a"], "q1", _open(["a", "b"]), INVALID_CODE),
        (["a"], "q1", _open(["a", "b"]), INVALID_CODE),
        (["a", "b", "c"], "q1", _open(["a", "b"]), INVALID_CODE),
        (["a", "x"], "q1", _open(["a", "b"]), INVALID_CODE),
    ],
)
def test_validate_rejects_with_a_typed_error(answer_ids, qid, open_q, code) -> None:
    with pytest.raises(SongOrderError) as err:
        validate_song_order_answer(
            SongOrderAnswerIn(question_id=qid, ordered_media_ids=answer_ids), open_q
        )
    assert err.value.code == code


# -- resolve ----------------------------------------------------------------------


def test_ambiguous_take_resolves_to_the_alternate_between_its_neighbours() -> None:
    alignment = _alignment(
        _confident("verse", 10.0),
        _ambiguous("chorus", 25.0, [(25.0, 0.9), (70.0, 0.8)]),
        _confident("outro", 100.0),
    )
    # Creator says the chorus take is the LATE chorus (after verse, before outro).
    out = resolve_uncertain_takes(alignment, ["verse", "chorus", "outro"])
    assert out["chorus"]["delta_s"] == 25.0 and out["chorus"]["confirmed_by_creator"] is True
    # Between verse(10) and a later confident take at 60 only the 25 s candidate fits...
    alignment2 = _alignment(
        _confident("verse", 10.0),
        _ambiguous("chorus", 25.0, [(25.0, 0.9), (70.0, 0.95)]),
        _confident("bridge", 60.0),
        _confident("outro", 100.0),
    )
    first = resolve_uncertain_takes(alignment2, ["verse", "chorus", "bridge", "outro"])
    assert first["chorus"]["delta_s"] == 25.0
    # ...and after the bridge only the 70 s one does.
    second = resolve_uncertain_takes(alignment2, ["verse", "bridge", "chorus", "outro"])
    assert second["chorus"]["delta_s"] == 70.0
    assert second["chorus"]["status"] == "confident"


def test_no_fitting_alternate_becomes_broll_never_a_guess() -> None:
    alignment = _alignment(
        _confident("a", 10.0),
        _ambiguous("x", 20.0, [(20.0, 0.9), (30.0, 0.5)]),
        _confident("b", 15.0),
    )
    out = resolve_uncertain_takes(alignment, ["a", "x", "b"])
    assert out["x"] == {"delta_s": None, "status": "unmatched", "confirmed_by_creator": True}


def test_unmatched_take_is_broll() -> None:
    alignment = _alignment(_confident("a", 1.0), TakeAlignment(media_id="s", status="unmatched"))
    out = resolve_uncertain_takes(alignment, ["a", "s"])
    assert out["s"]["delta_s"] is None and out["s"]["status"] == "unmatched"
    assert out["a"] == {"delta_s": 1.0, "status": "confident", "confirmed_by_creator": False}


def test_consecutive_uncertain_takes_bound_each_other() -> None:
    alignment = _alignment(
        _confident("a", 0.0),
        _ambiguous("x", 40.0, [(40.0, 0.9), (10.0, 0.8)]),
        _ambiguous("y", 40.0, [(40.0, 0.9), (10.0, 0.8)]),
        _confident("z", 100.0),
    )
    out = resolve_uncertain_takes(alignment, ["a", "x", "y", "z"])
    assert out["x"]["delta_s"] == 40.0  # best-scoring fitting candidate
    # y must come after x (40): 40 itself is not > 40, 10 is earlier -> B-roll
    assert out["y"]["delta_s"] is None


def test_resolved_payload_shape_keeps_creator_order() -> None:
    alignment = _alignment(_confident("a", 1.0), TakeAlignment(media_id="s", status="unmatched"))
    payload = resolved_song_takes_payload(resolve_uncertain_takes(alignment, ["s", "a"]))
    assert [p["media_id"] for p in payload] == ["s", "a"]
    assert set(payload[0]) == {"media_id", "delta_s", "status", "confirmed_by_creator"}


# ── apply_resolved_song_takes (the worker's read of the gate's answer) ───────


def test_apply_resolved_takes_narrows_a_confirmed_take_to_the_creators_position() -> None:
    from app.schemas.user_song import AlignmentAlternate, SongAlignment, TakeAlignment
    from app.services.song_order import apply_resolved_song_takes

    alignment = SongAlignment(
        song_generation=1,
        takes={
            "a": TakeAlignment(media_id="a", status="confident", delta_s=5.0),
            "b": TakeAlignment(
                media_id="b",
                status="ambiguous",
                delta_s=70.0,
                alternates=[AlignmentAlternate(delta_s=28.0, score=0.5)],
            ),
            "c": TakeAlignment(media_id="c", status="unmatched"),
        },
    )
    resolved = [
        {"media_id": "a", "delta_s": 5.0, "status": "confident", "confirmed_by_creator": False},
        {"media_id": "b", "delta_s": 28.0, "status": "confident", "confirmed_by_creator": True},
        {"media_id": "c", "delta_s": None, "status": "unmatched", "confirmed_by_creator": True},
        {"media_id": "gone", "delta_s": 1.0, "status": "confident", "confirmed_by_creator": True},
    ]

    patched, order = apply_resolved_song_takes(alignment, resolved)

    assert order == ["a", "b", "c"]  # a removed take is ignored
    assert patched.takes["a"] == alignment.takes["a"]  # confident rows are untouched
    assert (patched.takes["b"].status, patched.takes["b"].delta_s) == ("ambiguous", 28.0)
    assert patched.takes["b"].alternates == []  # only the creator's position remains
    assert (patched.takes["c"].status, patched.takes["c"].delta_s) == ("unmatched", None)
    assert alignment.takes["b"].delta_s == 70.0  # the input is not mutated


def test_apply_resolved_takes_without_an_answer_changes_nothing() -> None:
    from app.schemas.user_song import SongAlignment, TakeAlignment
    from app.services.song_order import apply_resolved_song_takes

    alignment = SongAlignment(
        song_generation=1,
        takes={"a": TakeAlignment(media_id="a", status="ambiguous", delta_s=9.0)},
    )
    patched, order = apply_resolved_song_takes(alignment, None)
    assert (patched, order) == (alignment, [])
