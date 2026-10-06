"""KRI-374 lane E: song-order question builder, fold, validation, resolution."""

from __future__ import annotations

import pytest

from app.schemas.user_song import (
    SONG_ALIGNMENT_VERSION,
    AlignmentAlternate,
    PlacementCandidate,
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
    takes_needing_order,
    thread_keeps_lipsync,
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


def _cands(mid: str, *cands: tuple[float, float]) -> TakeAlignment:
    """A v3 row: ``(delta_s, likelihood)`` candidates, best first."""
    ranked = sorted(cands, key=lambda c: -c[1])
    margin = 1.0 if len(ranked) == 1 else (ranked[0][1] - ranked[1][1]) / ranked[0][1]
    return TakeAlignment(
        media_id=mid,
        status="confident" if len(ranked) == 1 and ranked[0][1] >= 0.35 else "ambiguous",
        delta_s=ranked[0][0],
        confidence=ranked[0][1],
        likelihood=ranked[0][1],
        margin=margin,
        candidates=[
            PlacementCandidate(delta_s=d, likelihood=li, method="lyrics") for d, li in ranked
        ],
    )


def _none(mid: str) -> TakeAlignment:
    return TakeAlignment(media_id=mid, status="unmatched")


def _alignment(*takes: TakeAlignment) -> SongAlignment:
    return SongAlignment(song_generation=3, takes={t.media_id: t for t in takes})


# -- build ---------------------------------------------------------------------


def test_question_orders_by_song_position_and_places_no_evidence_after_capture_predecessor() -> (
    None
):
    alignment = _alignment(
        _confident("late", 50.0),
        _none("silent"),
        _ambiguous("chorus", 20.0, [(20.0, 0.9), (80.0, 0.8)]),
        _confident("early", 5.0),
    )
    q = build_song_order_question(
        alignment, ["late", "silent", "chorus", "early"], question_id="q1"
    )
    assert q.question_id == "q1"
    # `silent` was filmed right after `late`, so it rides right after it.
    assert q.proposed_order == ["early", "chorus", "late", "silent"]
    assert [i.media_id for i in q.items] == q.proposed_order
    by_id = {i.media_id: i for i in q.items}
    assert by_id["early"].song_start_s == 5.0 and by_id["early"].alternates == []
    assert by_id["early"].status == "confident" and by_id["early"].reason is None
    assert by_id["chorus"].status == "ambiguous" and by_id["chorus"].reason == "tie"
    assert [a.delta_s for a in by_id["chorus"].alternates] == [20.0, 80.0]
    # No evidence: sent as ambiguous with no position so the app lets it be dragged.
    assert by_id["silent"].song_start_s is None
    assert by_id["silent"].status == "ambiguous" and by_id["silent"].reason == "no_evidence"


def test_no_evidence_takes_interleave_by_capture_order() -> None:
    alignment = _alignment(
        _none("lead"),  # nothing precedes it: leads the list
        _cands("b", (40.0, 0.8)),
        _none("after_b"),
        _cands("a", (10.0, 0.8)),
        _none("after_a"),
    )
    ids = ["lead", "b", "after_b", "a", "after_a"]
    q = build_song_order_question(alignment, ids)
    assert q.proposed_order == ["lead", "a", "after_a", "b", "after_b"]
    assert {i.media_id: i.reason for i in q.items if i.reason} == {
        "lead": "no_evidence",
        "after_b": "no_evidence",
        "after_a": "no_evidence",
    }


def test_weak_and_tied_takes_carry_their_reason_and_likelihood() -> None:
    alignment = _alignment(
        _cands("sure", (5.0, 0.8)),
        _cands("weak", (30.0, 0.2)),
        _cands("repeat", (60.0, 0.7), (120.0, 0.7)),
    )
    q = build_song_order_question(alignment, ["sure", "weak", "repeat"])
    by_id = {i.media_id: i for i in q.items}
    assert by_id["sure"].reason is None
    assert by_id["weak"].reason == "weak" and by_id["weak"].likelihood == 0.2
    assert by_id["repeat"].reason == "tie"


def test_missing_alignment_row_counts_as_no_evidence_and_ids_are_deduped() -> None:
    alignment = _alignment(_confident("a", 1.0))
    q = build_song_order_question(alignment, ["a", "b", "b"])
    assert q.proposed_order == ["a", "b"]
    assert q.items[1].status == "ambiguous" and q.items[1].reason == "no_evidence"
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


def _row(out: dict, mid: str) -> tuple:
    r = out[mid]
    return r["delta_s"], r["place"], r["position_basis"], r["confirmed_by_creator"]


def test_uncertain_take_resolves_to_the_candidate_between_its_neighbours() -> None:
    alignment = _alignment(
        _cands("verse", (10.0, 0.9)),
        _cands("chorus", (25.0, 0.7), (70.0, 0.7)),
        _cands("bridge", (60.0, 0.9)),
        _cands("outro", (100.0, 0.9)),
    )
    # Before the bridge only the 25 s repeat fits: the creator's order decided it.
    first = resolve_uncertain_takes(alignment, ["verse", "chorus", "bridge", "outro"])
    assert _row(first, "chorus") == (25.0, "pinned", "creator_position", True)
    # After the bridge only the 70 s repeat fits.
    second = resolve_uncertain_takes(alignment, ["verse", "bridge", "chorus", "outro"])
    assert _row(second, "chorus") == (70.0, "pinned", "creator_position", True)
    # A take the aligner was sure of keeps its own position and is NOT creator-confirmed.
    assert _row(first, "verse") == (10.0, "pinned", "aligner", False)


def test_several_tied_fits_are_a_tie_break_not_a_creator_confirmation() -> None:
    alignment = _alignment(
        _cands("a", (10.0, 0.9)),
        _cands("x", (30.0, 0.7), (50.0, 0.7)),
        _cands("z", (200.0, 0.9)),
    )
    out = resolve_uncertain_takes(alignment, ["a", "x", "z"])
    # Both repeats fit between a and z: nearest the previous take wins, unconfirmed.
    assert _row(out, "x") == (30.0, "pinned", "tie_break", False)


def test_a_take_with_no_fitting_candidate_is_stacked_in_the_gap() -> None:
    alignment = _alignment(
        _cands("a", (10.0, 0.9)),
        _cands("x", (5.0, 0.7), (60.0, 0.7)),
        _cands("b", (35.0, 0.9)),
    )
    # x must sit between a (10) and b (35): neither repeat fits, so it is stacked, not dropped.
    out = resolve_uncertain_takes(alignment, ["a", "x", "b"], {"a": 8.0, "x": 6.0, "b": 8.0})
    delta, place, basis, confirmed = _row(out, "x")
    assert (place, basis, confirmed) == ("stack", "creator_stack", True)
    assert delta is not None and delta > 10.0  # after a's last trusted frame (10 + 8 - margin)


def test_a_stacked_take_never_lands_after_the_take_it_must_precede() -> None:
    alignment = _alignment(
        _cands("a", (10.0, 0.9)),
        _cands("x", (20.0, 0.7), (30.0, 0.7)),
        _cands("b", (15.0, 0.9)),
    )
    # a and b already overlap: no room for x between them, so it is B-roll (not
    # stacked on the wrong side of b).
    out = resolve_uncertain_takes(alignment, ["a", "x", "b"], {"a": 8.0, "x": 6.0, "b": 8.0})
    delta, place, _basis, _confirmed = _row(out, "x")
    assert place == "broll" and delta is None


def test_no_evidence_takes_stack_between_their_neighbours_in_creator_order() -> None:
    alignment = _alignment(
        _cands("a", (0.0, 0.9)), _none("s1"), _none("s2"), _cands("b", (40.0, 0.9))
    )
    durs = {"a": 8.0, "s1": 5.0, "s2": 6.0, "b": 8.0}
    out = resolve_uncertain_takes(alignment, ["a", "s1", "s2", "b"], durs)
    d1, d2 = out["s1"]["delta_s"], out["s2"]["delta_s"]
    assert out["s1"]["place"] == out["s2"]["place"] == "stack"
    assert 0.0 < d1 < d2 < 40.0
    assert d2 == pytest.approx(d1 + 5.0, abs=0.01)  # laid end to end
    assert out["s1"]["likelihood"] == 0.0
    assert out["s1"]["confirmed_by_creator"] is True


def test_all_no_evidence_takes_stack_from_the_first_lyric_line() -> None:
    alignment = _alignment(_none("x"), _none("y"))
    out = resolve_uncertain_takes(
        alignment, ["x", "y"], {"x": 6.0, "y": 6.0}, song_duration_s=100.0, first_line_s=12.0
    )
    assert out["x"]["delta_s"] == 12.0 and out["y"]["delta_s"] == 18.0
    assert {r["place"] for r in out.values()} == {"stack"}


def test_a_stacked_take_with_no_room_becomes_broll() -> None:
    alignment = _alignment(_cands("a", (0.0, 0.9)), _none("x"))
    out = resolve_uncertain_takes(alignment, ["a", "x"], {"a": 8.0, "x": 6.0}, song_duration_s=8.5)
    assert out["x"]["place"] == "broll" and out["x"]["delta_s"] is None
    assert out["x"]["reason"] == "no_room"


def test_resolved_payload_shape_keeps_creator_order() -> None:
    alignment = _alignment(_confident("a", 1.0), _none("s"))
    payload = resolved_song_takes_payload(resolve_uncertain_takes(alignment, ["s", "a"]))
    assert [p["media_id"] for p in payload] == ["s", "a"]
    assert [p["order_index"] for p in payload] == [0, 1]
    for row in payload:
        assert {"media_id", "order_index", "delta_s", "place", "position_basis"} <= set(row)
        assert row["status"] in ("confident", "unmatched")


# ── apply_resolved_song_takes (the worker's read of the gate's answer) ───────


def test_apply_returns_clean_creator_choices_and_never_rewrites_rows() -> None:
    from app.services.song_order import apply_resolved_song_takes

    alignment = _alignment(
        _cands("a", (5.0, 0.9)), _cands("b", (70.0, 0.5), (28.0, 0.5)), _none("c")
    )
    resolved = [
        {
            "media_id": "a",
            "order_index": 0,
            "delta_s": 5.0,
            "place": "pinned",
            "position_basis": "aligner",
            "confirmed_by_creator": False,
        },
        {
            "media_id": "b",
            "order_index": 1,
            "delta_s": 28.0,
            "place": "pinned",
            "position_basis": "creator_position",
            "confirmed_by_creator": True,
        },
        {
            "media_id": "c",
            "order_index": 2,
            "delta_s": 33.0,
            "place": "stack",
            "position_basis": "creator_stack",
            "confirmed_by_creator": True,
        },
        {"media_id": "gone", "order_index": 3, "delta_s": 1.0, "place": "pinned"},
    ]
    patched, order, choices = apply_resolved_song_takes(alignment, resolved)
    assert patched is alignment  # rows untouched
    assert order == ["a", "b", "c"]  # a removed take is ignored
    assert choices["b"] == {
        "delta_s": 28.0,
        "place": "pinned",
        "position_basis": "creator_position",
        "confirmed_by_creator": True,
        "reason": None,
    }
    assert choices["c"]["place"] == "stack" and choices["c"]["delta_s"] == 33.0
    assert choices["a"]["confirmed_by_creator"] is False


def test_legacy_resolved_rows_keep_their_old_meaning() -> None:
    """Rows written before KRI-471 have no ``place``: delta None => B-roll, a confirmed
    delta pins the take, an unconfirmed row is left to the assignment."""
    from app.services.song_order import apply_resolved_song_takes

    alignment = _alignment(_confident("a", 5.0), _cands("b", (70.0, 0.5), (28.0, 0.5)), _none("c"))
    legacy = [
        {"media_id": "a", "delta_s": 5.0, "status": "confident", "confirmed_by_creator": False},
        {"media_id": "b", "delta_s": 28.0, "status": "confident", "confirmed_by_creator": True},
        {"media_id": "c", "delta_s": None, "status": "unmatched", "confirmed_by_creator": True},
    ]
    _a, order, choices = apply_resolved_song_takes(alignment, legacy)
    assert order == ["a", "b", "c"]
    assert "a" not in choices
    assert (choices["b"]["place"], choices["b"]["delta_s"]) == ("pinned", 28.0)
    assert choices["b"]["position_basis"] == "creator_position"
    assert (choices["c"]["place"], choices["c"]["delta_s"]) == ("broll", None)


def test_apply_resolved_takes_without_an_answer_changes_nothing() -> None:
    from app.services.song_order import apply_resolved_song_takes

    alignment = _alignment(_ambiguous("a", 9.0, []))
    assert apply_resolved_song_takes(alignment, None) == (alignment, [], {})


def test_new_place_values_round_trip_through_the_payload() -> None:
    from app.services.song_order import apply_resolved_song_takes

    alignment = _alignment(_cands("a", (0.0, 0.9)), _none("s"), _cands("b", (40.0, 0.9)))
    resolved = resolve_uncertain_takes(alignment, ["a", "s", "b"], {"a": 8.0, "s": 5.0, "b": 8.0})
    payload = resolved_song_takes_payload(resolved)
    _a, _order, choices = apply_resolved_song_takes(alignment, payload)
    assert choices["s"]["place"] == "stack"
    assert choices["s"]["delta_s"] == resolved["s"]["delta_s"]
    assert choices["s"]["position_basis"] == "creator_stack"


# -- KRI-374 review: answers are tied to the song generation ------------------------


def _qg(qid: str, ids: list[str], generation: int | None) -> dict:
    payload = _q(qid, ids)
    if generation is None:  # a question written before the field existed
        payload["song_order_question"].pop("song_generation", None)
    else:
        payload["song_order_question"]["song_generation"] = generation
    return payload


def _answer(qid: str, ids: list[str]) -> dict:
    return {"song_order": {"question_id": qid, "ordered_media_ids": ids}}


def test_built_questions_record_the_song_generation_and_old_ones_serialize_unchanged() -> None:
    question = build_song_order_question(_alignment(_confident("a", 1.0)), ["a"])
    assert question.song_generation == 3
    assert question.model_dump(mode="json")["song_generation"] == 3
    legacy = SongOrderQuestion.model_validate(_qg("q1", ["a", "b"], None)["song_order_question"])
    assert legacy.song_generation is None
    assert "song_generation" not in legacy.model_dump(mode="json")


def test_an_answer_to_a_previous_song_generation_is_ignored_and_re_asked() -> None:
    """Probe: after the creator replaced the song, the old confirmed order still applied."""
    events = [
        ("assistant", _qg("q1", ["a", "b"], 3)),
        ("user", _answer("q1", ["b", "a"])),
    ]
    assert fold_song_orders(events, song_generation=3).ordered_media_ids == ("b", "a")
    assert not fold_song_orders(events, song_generation=4)  # the song changed
    # No generation to compare against, or a question from before the field existed:
    assert fold_song_orders(events)
    legacy = [("assistant", _qg("q1", ["a", "b"], None)), ("user", _answer("q1", ["b", "a"]))]
    assert fold_song_orders(legacy, song_generation=4)


def test_a_newer_generation_answer_wins_over_an_older_one() -> None:
    events = [
        ("assistant", _qg("q1", ["a", "b"], 3)),
        ("user", _answer("q1", ["b", "a"])),
        ("assistant", _qg("q2", ["a", "b"], 4)),
        ("user", _answer("q2", ["a", "b"])),
    ]
    assert fold_song_orders(events, song_generation=4).ordered_media_ids == ("a", "b")
    assert fold_song_orders(events, song_generation=3).ordered_media_ids == ("b", "a")


def test_thread_keeps_lipsync_while_a_question_is_open_or_being_answered() -> None:
    asked = [("assistant", _qg("q1", ["a", "b"], 3))]
    answered = [*asked, ("user", _answer("q1", ["b", "a"]))]
    assert thread_keeps_lipsync(asked, 3)
    assert thread_keeps_lipsync(answered, 3)  # the answer turn itself
    # A later, unrelated message is the creator's own call again.
    assert not thread_keeps_lipsync([*answered, ("user", {"text": "use it as background"})], 3)
    # Another song generation, or no exchange at all, never forces lip-sync.
    assert not thread_keeps_lipsync(answered, 4)
    assert not thread_keeps_lipsync([], 3)
    assert not thread_keeps_lipsync([("user", None)], 3)


# -- KRI-471: the creator is asked only where their say could matter -----------


def test_takes_needing_order_is_the_assignments_ask_set() -> None:
    alignment = _alignment(
        _cands("sure", (1.0, 0.8)),
        _cands("repeat", (20.0, 0.7), (60.0, 0.7)),
        _cands("weak", (100.0, 0.2)),
        _none("silent"),
    )
    got = takes_needing_order(alignment, ["sure", "repeat", "weak", "silent", "missing"])
    assert got == ["repeat", "weak", "silent", "missing"]


def test_no_ask_when_every_take_is_clearly_placed() -> None:
    alignment = _alignment(_cands("a", (1.0, 0.8)), _cands("b", (40.0, 0.7)))
    assert takes_needing_order(alignment, ["a", "b"]) == []


def test_a_no_evidence_only_set_is_asked_about() -> None:
    assert takes_needing_order(_alignment(_none("a"), _none("b")), ["a", "b"]) == ["a", "b"]


def test_question_text_counts_every_take_the_creator_is_asked_about() -> None:
    from app.services.song_order import song_order_question_text

    alignment = _alignment(
        _cands("a", (5.0, 0.2)), _none("b"), _none("c"), _cands("d", (80.0, 0.9))
    )
    question = build_song_order_question(alignment, ["a", "b", "c", "d"])
    text = song_order_question_text(question)
    assert "3 of your clips" in text and "drag any that are out of place" in text
    one = build_song_order_question(_alignment(_none("z"), _cands("y", (3.0, 0.9))), ["z", "y"])
    assert "one of your clips" in song_order_question_text(one)


def test_an_old_question_without_reasons_still_parses() -> None:
    old = {
        "question_id": "q",
        "proposed_order": ["a"],
        "items": [{"media_id": "a", "status": "ambiguous", "song_start_s": 3.0, "alternates": []}],
    }
    parsed = SongOrderQuestion.model_validate(old)
    assert parsed.items[0].reason is None and parsed.items[0].likelihood is None
