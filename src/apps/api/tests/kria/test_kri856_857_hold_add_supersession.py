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
from app.kria.brief import BriefUpdate, CreativeBrief
from app.kria.brief_checks import check_requirement
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
