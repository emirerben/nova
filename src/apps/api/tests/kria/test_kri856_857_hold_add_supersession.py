"""KRI-856 / KRI-857: a hold the model files as a new "add" replaces the older hold on that photo.

Ways this can fail (written before the fix):
1. The creator types new seconds ("keep the beach photo for 3 seconds") and the model files an
   add. The older live hold has no ``asset_holds`` row, so its receipt is a false "partial"
   and it still counts as a live ask (KRI-856).
2. The creator says "a bit longer" and the model files an add. Nothing hands the prior hold to
   ``stamp_authority``, so the new hold stays unowned and the older 1 s hold keeps applying:
   the photo does not get longer (KRI-857).
3. Over-reach: a "longer" add whose number shrank, an add for another photo, or an add with no
   seconds must not retire the creator's hold.

Round trip: ``apply_updates`` (the brief the server persists) -> approved binding -> unified
montage plan -> the finished-video receipt.
"""

from __future__ import annotations

import pytest

from app.kria.asset_holds import asset_holds
from app.kria.brief import BriefUpdate, CreativeBrief, apply_updates
from app.kria.brief_checks import check_requirement
from app.kria.editor_sequence_timing import (
    SequenceTimingIntent,
    SequenceTimingPlan,
    merge_sequence_brief_updates,
)
from tests.kria.test_kri824_photo_hold_e2e import (
    BEACH,
    CAFE,
    PHOTOS,
    _hold_update,
    _render,
    _strategy,
    _turn,
)


def _first_turn() -> CreativeBrief:
    return _turn(
        None,
        [_hold_update(BEACH, 1, "beach photo 1 second")],
        "keep the beach photo on screen for 1 second",
        "t1",
    )


def _live_holds(brief: CreativeBrief, photo):
    return [r for r in brief.live() if r.scope == f"clip:{photo.manifest_id}"]


@pytest.mark.parametrize(
    ("message", "seconds"),
    [
        ("keep the beach photo for 3 seconds", 3.0),
        ("a bit longer", 1.5),
    ],
    ids=["typed_seconds", "a_bit_longer"],
)
def test_an_added_hold_on_the_same_photo_replaces_the_older_one(message, seconds) -> None:
    first = _first_turn()
    second = _turn(
        first,
        [_hold_update(BEACH, seconds, f"beach photo {seconds} seconds")],
        message,
        "t2",
    )

    [live] = _live_holds(second, BEACH)
    assert live.authority == "explicit"
    assert live.facts["duration_s"] == seconds

    rendered = _render(second, _strategy())
    assert rendered["cloud_still_s"][BEACH.media_id] == pytest.approx(seconds, abs=1 / 60)
    assert rendered["phone_still_s"][BEACH.media_id] == pytest.approx(seconds, abs=1 / 60)
    receipts = {r.id: check_requirement(r, rendered["facts"]) for r in second.live()}
    assert {r.status for r in receipts.values()} == {"met"}, {
        k: (v.status, v.reason) for k, v in receipts.items()
    }


def test_a_longer_add_that_shrank_the_hold_leaves_the_creators_hold_in_place() -> None:
    first = _first_turn()
    second = _turn(
        first, [_hold_update(BEACH, 0.5, "beach photo 0.5 seconds")], "a bit longer", "t2"
    )

    older, added = _live_holds(second, BEACH)
    assert older.authority == "explicit" and added.authority is None
    assert asset_holds(second, [p.manifest_id for p in PHOTOS])[BEACH.manifest_id].hold_s == 1.0


def test_an_add_for_another_photo_never_retires_this_photos_hold() -> None:
    first = _first_turn()
    second = _turn(
        first,
        [_hold_update(CAFE, 2, "cafe photo 2 seconds")],
        "keep the cafe photo for 2 seconds",
        "t2",
    )
    assert len(_live_holds(second, BEACH)) == 1
    assert len(_live_holds(second, CAFE)) == 1
    rendered = _render(second, _strategy())
    assert rendered["cloud_still_s"][BEACH.media_id] == pytest.approx(1.0, abs=1 / 60)
    assert rendered["cloud_still_s"][CAFE.media_id] == pytest.approx(2.0, abs=1 / 60)

    # "a bit longer" for the cafe photo has no prior hold of its own to extend.
    third = _turn(
        first,
        [_hold_update(CAFE, 1.5, "cafe photo 1.5 seconds")],
        "a bit longer",
        "t2",
    )
    assert _live_holds(third, BEACH)[0].authority == "explicit"
    assert _live_holds(third, CAFE)[0].authority is None


def test_an_add_without_seconds_never_retires_the_hold() -> None:
    first = _first_turn()
    second = _turn(
        first,
        [
            BriefUpdate(
                kind="timing",
                scope=f"clip:{BEACH.manifest_id}",
                description="keep the beach photo short",
            )
        ],
        "keep the beach photo short",
        "t2",
    )
    assert len(_live_holds(second, BEACH)) == 2
    assert asset_holds(second, [p.manifest_id for p in PHOTOS])[BEACH.manifest_id].hold_s == 1.0


def _tail_hold(photo, seconds: float) -> BriefUpdate:
    """The same photo named by its media id, a unique tail of its manifest id (KRI-824 #20)."""
    return BriefUpdate(
        kind="timing",
        scope=f"clip:{photo.media_id}",
        description=f"beach photo {seconds} seconds",
        facts={"duration_s": seconds},
    )


def _applied(brief: CreativeBrief):
    return asset_holds(brief, [p.manifest_id for p in PHOTOS]).get(BEACH.manifest_id)


def _assert_one_creator_hold(brief: CreativeBrief, seconds: float) -> None:
    timings = [r for r in brief.live() if r.kind == "timing" and r.scope != "global"]
    assert len(timings) == 1, [(r.id, r.scope, r.authority) for r in timings]
    assert timings[0].authority == "explicit"
    assert _applied(brief).hold_s == seconds
    rendered = _render(brief, _strategy())
    assert rendered["cloud_still_s"][BEACH.media_id] == pytest.approx(seconds, abs=1 / 60)
    receipts = {r.id: check_requirement(r, rendered["facts"]) for r in brief.live()}
    assert {r.status for r in receipts.values()} == {"met"}, {
        k: (v.status, v.reason) for k, v in receipts.items()
    }


def test_a_projection_restating_the_hold_keeps_the_creators_hold() -> None:
    """A preview (no creator text) must not retire the hold: the editor turns diff it."""
    first = _first_turn()
    projected = apply_updates(
        first,
        [_hold_update(BEACH, 1, "beach photo 1 second")],
        source_turn_id=None,
    )
    assert [r.authority for r in _live_holds(projected, BEACH)] == ["explicit", None]


def test_a_sequence_timing_turn_that_restates_the_hold_keeps_the_photo_held() -> None:
    first = _first_turn()
    message = "make the video 20 seconds and keep the beach photo as it is"
    planned = (
        BriefUpdate(
            kind="timing",
            scope="global",
            description="video 20 seconds",
            facts={"duration_s": 20.0},
        ),
        _hold_update(BEACH, 1.0, "beach photo stays 1 second"),
    )
    plan = SequenceTimingPlan(
        ({"op": "set_total_duration", "duration_s": 20.0},),
        "",
        SequenceTimingIntent("preserve", 20.0),
    )
    merged = merge_sequence_brief_updates(first, planned, plan, {}, message)
    assert not [u for u in merged if u.operation == "remove"]

    persisted = apply_updates(first, merged, source_turn_id="t2", creator_text=message)
    hold = _applied(persisted)
    assert hold is not None and hold.hold_s == 1.0
    [live] = _live_holds(persisted, BEACH)
    assert live.authority == "explicit"
    assert persisted.requirements[0].facts.get("superseded_by") != "creator"


@pytest.mark.parametrize(
    ("message", "seconds"),
    [("keep the beach photo for 3 seconds", 3.0), ("a bit longer", 1.5)],
    ids=["typed_seconds", "a_bit_longer"],
)
def test_the_same_photo_named_by_a_different_scope_form_is_one_photo(message, seconds) -> None:
    first = _first_turn()
    second = _turn(first, [_tail_hold(BEACH, seconds)], message, "t2")
    _assert_one_creator_hold(second, seconds)


def test_a_hold_stored_under_a_tail_scope_is_replaced_by_a_full_id_add() -> None:
    first = _turn(None, [_tail_hold(BEACH, 1)], "keep the beach photo on screen for 1 second", "t1")
    second = _turn(
        first, [_hold_update(BEACH, 1.5, "beach photo 1.5 seconds")], "a bit longer", "t2"
    )
    _assert_one_creator_hold(second, 1.5)


def test_a_change_that_rewrites_the_scope_form_keeps_the_creators_ownership() -> None:
    first = _first_turn()
    [r1] = first.live()
    change = BriefUpdate(
        operation="change",
        target_requirement_id=r1.id,
        expected_version=first.version,
        kind="timing",
        scope=f"clip:{BEACH.media_id}",
        description="beach photo 1.5 seconds",
        facts={"duration_s": 1.5},
    )
    second = _turn(first, [change], "a bit longer", "t2")
    _assert_one_creator_hold(second, 1.5)


def test_two_photos_sharing_a_tail_are_not_confused() -> None:
    """A tail two photos share names neither: it must not retire either hold."""

    def hold(scope: str, seconds: float) -> BriefUpdate:
        return BriefUpdate(
            kind="timing", scope=scope, description="photo hold", facts={"duration_s": seconds}
        )

    first = _turn(
        None,
        [hold("clip:aaaa-123456789", 1), hold("clip:bbbb-123456789", 2)],
        "keep the first photo 1 second and the second 2 seconds",
        "t1",
    )
    second = _turn(first, [hold("clip:123456789", 3)], "keep it for 3 seconds", "t2")
    assert {r.id for r in first.live()} <= {r.id for r in second.live()}


def test_an_unowned_hold_in_between_does_not_stop_a_longer_ask_extending_the_hold() -> None:
    first = _first_turn()
    shrank = _turn(
        first, [_hold_update(BEACH, 0.5, "beach photo 0.5 seconds")], "a bit longer", "t2"
    )
    third = _turn(
        shrank, [_hold_update(BEACH, 1.5, "beach photo 1.5 seconds")], "a bit longer", "t3"
    )
    assert _applied(third).hold_s == 1.5
    assert _applied(third).authority == "explicit"


def test_a_models_own_number_in_between_does_not_stop_a_longer_ask() -> None:
    first = _first_turn()
    proposal = _turn(
        first, [_hold_update(BEACH, 2.0, "beach photo 2 seconds")], "change the title", "t2"
    )
    assert _applied(proposal).hold_s == 1.0
    third = _turn(
        proposal, [_hold_update(BEACH, 1.5, "beach photo 1.5 seconds")], "a bit longer", "t3"
    )
    assert _applied(third).hold_s == 1.5
    assert not [r for r in third.live() if r.scope.startswith("clip:") and r.authority is None]


@pytest.mark.parametrize("add_first", [False, True], ids=["change_first", "add_first"])
def test_a_change_and_an_add_on_the_same_photo_in_one_batch_leave_one_live_hold(add_first) -> None:
    first = _first_turn()
    [r1] = first.live()
    change = BriefUpdate(
        operation="change",
        target_requirement_id=r1.id,
        expected_version=first.version,
        kind="timing",
        scope=f"clip:{BEACH.manifest_id}",
        description="beach photo 3 seconds",
        facts={"duration_s": 3.0},
    )
    add = _hold_update(BEACH, 3.0, "beach photo 3 seconds")
    batch = [add, change] if add_first else [change, add]
    second = _turn(first, batch, "keep the beach photo for 3 seconds", "t2")
    _assert_one_creator_hold(second, 3.0)
