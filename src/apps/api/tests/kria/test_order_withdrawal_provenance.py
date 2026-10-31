"""A creator who withdraws a filming-order ask is not read as a server waiver (#1974 follow-up).

#1974 keeps filming order when the last order basis a brief let go of was filming time ("Make it
without these" waives the check, not the wish). A requirement's status then says only
`superseded`, so a creator's own withdrawal looked the same: a model-emitted `remove` from free
text, or `starts_fresh` (`start_fresh_updates`), also planned chronologically. Probe, #1974 head
111210e37 vs main 4159406a1: both went UPLOAD -> FILMING.

Forward provenance, stamped when the removal is applied and never inferred afterwards:
`apply_updates` marks every removed requirement `facts.superseded_by = "creator"` (the default) or
`"server"` (its id is in `server_removed_ids`: the simpler version and the automatic omission), so a
batch mixing both is exact. Removing an already-superseded requirement is allowed and only moves the
mark up: a creator's removal sets "creator", a server removal never changes an existing mark. The
worker reads the mark. Anything stored before this change has no mark and reads exactly as #1974
reads it (disclosed ambiguity: a creator removal stored earlier cannot be told from a waiver, and
nothing is back-filled).

Ways this can fail (each pinned below):
 W1 a creator's removal / start-fresh of a filming ask still plans chronologically.
 W2 a server waiver is marked "creator", so the incident (simpler version) stops keeping order.
 W3 the stored incident brief (no mark) or `answered_brief`'s "continue without a fixed order" copy
    changes.
 W4 the mark is wrong or missing for some kind of requirement, or alters its other facts.
 W5 a live alternative order (any / random / upload) or a live filming ask changes.
 W7 a later creator withdrawal is read as an earlier waiver (the last word must win).
 W8 a fresh start does not reach an ask the server (or a legacy store) already waived.
 W9 a retry changes the result, a server removal overrides a creator's mark, or an unknown id or a
    change of a superseded requirement stops being refused.
 W10 a batch that mixes a creator removal and a server omission mislabels one of them.
"""

from __future__ import annotations

import copy

import pytest

from app.kria.brief import (
    BriefUpdate,
    BriefUpdateBatchError,
    CreativeBrief,
    apply_updates,
    start_fresh_updates,
)
from app.pipeline.unified_montage import brief_view
from app.services.choice_questions import CONFLICT_ORDER_BASIS, OPT_UNORDERED, answered_brief
from tests.kria.test_leverkusen_m1_simpler_keeps_filming_order import (  # noqa: F401
    FILMING_ORDER,
    SHIPPED_ORDER,
    _brief_with_r1,
    _clip_intents_on,
    _order,
    _pinned_brief,
    _plan,
)

MARK = "superseded_by"


def _live() -> CreativeBrief:
    return _brief_with_r1(status="open")


def _r1(brief: CreativeBrief):  # noqa: ANN202
    return next(r for r in brief.requirements if r.id == "r1")


def _remove(prior: CreativeBrief, requirement_id: str = "r1", **kwargs) -> CreativeBrief:  # noqa: ANN003
    return apply_updates(
        prior,
        [
            BriefUpdate(
                operation="remove",
                target_requirement_id=requirement_id,
                expected_version=prior.version,
            )
        ],
        source_turn_id="t2",
        **kwargs,
    )


def _plays(brief: CreativeBrief) -> str:
    order = _order(_plan(copy.deepcopy(brief)))
    return "filming" if order == FILMING_ORDER else "upload" if order == SHIPPED_ORDER else "other"


# -- W1 / W2: who removed it -----------------------------------------------------------------


def test_w1_a_creators_removal_is_marked_and_plans_in_upload_order() -> None:
    removed = _remove(_live())
    assert (_r1(removed).status, _r1(removed).facts[MARK]) == ("superseded", "creator")
    assert brief_view(removed).kept_filming_order is False
    assert _plays(removed) == "upload"


def test_w1_start_fresh_is_a_creators_withdrawal() -> None:
    prior = _live()
    fresh = apply_updates(prior, start_fresh_updates(prior, ()), source_turn_id="t2")
    assert _r1(fresh).facts[MARK] == "creator"
    assert _plays(fresh) == "upload"


def test_w2_a_server_waiver_is_marked_server_and_keeps_filming_order() -> None:
    waived = _remove(_live(), server_removed_ids={"r1"})
    assert (_r1(waived).status, _r1(waived).facts[MARK]) == ("superseded", "server")
    assert brief_view(waived).kept_filming_order is True
    assert _plays(waived) == "filming"


# -- W3: legacy and derived states read as #1974 reads them ----------------------------------


def test_w3_the_stored_incident_brief_has_no_mark_and_keeps_filming_order() -> None:
    brief = _pinned_brief()
    assert _r1(brief).status == "superseded" and MARK not in _r1(brief).facts
    assert _plays(brief) == "filming"


def test_w3_continue_without_a_fixed_order_is_still_a_waiver() -> None:
    waived = answered_brief(
        _live(),
        {
            "choice_answers": [
                {"kind": CONFLICT_ORDER_BASIS, "option": OPT_UNORDERED, "requirement_ids": ["r1"]}
            ]
        },
    )
    assert MARK not in _r1(waived).facts
    assert _plays(waived) == "filming"


# -- W4: every new removal carries the mark, whatever the kind, and nothing else changes -----


@pytest.mark.parametrize(
    "kind, facts",
    [
        ("order", {"key": "capture_time"}),
        ("order", {"key": "random"}),
        ("order", {"first_clip": "the train ride"}),
        ("text", {}),
        ("timing", {"duration_s": 7}),
    ],
)
@pytest.mark.parametrize("who, expected", [(None, "creator"), ("server", "server")])
def test_w4_every_new_removal_is_marked_and_nothing_else_changes(
    kind: str, facts: dict, who: str | None, expected: str
) -> None:
    prior = apply_updates(
        CreativeBrief(),
        [BriefUpdate(operation="add", kind=kind, scope="global", description="d", facts=facts)],
        source_turn_id="t1",
    )
    [req] = prior.requirements
    kwargs = {} if who is None else {"server_removed_ids": {req.id}}
    [out] = _remove(prior, req.id, **kwargs).requirements
    assert out.status == "superseded"
    assert out.facts == {**req.facts, MARK: expected}
    assert out.model_copy(update={"status": req.status, "facts": req.facts}) == req


def test_w4_only_the_removed_requirement_is_marked() -> None:
    brief = _live()
    removed = _remove(brief, "r6")  # the text ask; r1 (filming order), r2.. stay as they were
    assert removed.requirements[5].facts[MARK] == "creator"
    for req, before in zip(removed.requirements[:5], brief.requirements[:5], strict=True):
        assert req == before and MARK not in req.facts


# -- W5: live asks are untouched --------------------------------------------------------------


@pytest.mark.parametrize("key", ["any_order", "random", "attachment"])
def test_w5_a_live_alternative_order_still_wins(key: str) -> None:
    prior = _live()
    changed = apply_updates(
        prior,
        [
            BriefUpdate(
                operation="change",
                target_requirement_id="r1",
                expected_version=prior.version,
                kind="order",
                scope="global",
                description="x",
                facts={"key": key},
            )
        ],
        source_turn_id="t2",
    )
    assert MARK not in _r1(changed).facts
    assert brief_view(changed).kept_filming_order is False


def test_w5_a_live_filming_ask_is_unchanged() -> None:
    assert _plays(_live()) == "filming"


# -- W7: the last word wins -------------------------------------------------------------------


def test_w7_a_creator_withdrawal_after_a_waiver_is_not_a_waiver() -> None:
    waived = _remove(_live(), server_removed_ids={"r1"})
    added = apply_updates(
        waived,
        [
            BriefUpdate(
                operation="add",
                kind="order",
                scope="global",
                description="in filming order",
                facts={"key": "capture_time"},
            )
        ],
        source_turn_id="t3",
    )
    new_id = added.requirements[-1].id
    withdrawn = _remove(added, new_id)
    assert withdrawn.requirements[-1].facts[MARK] == "creator"
    assert brief_view(withdrawn).kept_filming_order is False


def test_w7_a_waiver_after_a_creator_withdrawal_is_a_waiver() -> None:
    first = _remove(_live())  # the creator withdrew the filming ask
    added = apply_updates(
        first,
        [
            BriefUpdate(
                operation="add",
                kind="order",
                scope="global",
                description="in filming order",
                facts={"key": "capture_time"},
            )
        ],
        source_turn_id="t3",
    )
    new_id = added.requirements[-1].id
    waived = _remove(added, new_id, server_removed_ids={new_id})
    assert waived.requirements[-1].facts[MARK] == "server"
    assert brief_view(waived).kept_filming_order is True


# -- W8: a fresh start reaches what was already waived -----------------------------------------


def test_w8_starting_fresh_after_a_server_waiver_withdraws_the_filming_wish() -> None:
    waived = _remove(_live(), server_removed_ids={"r1"})
    assert _plays(waived) == "filming"
    fresh = apply_updates(waived, start_fresh_updates(waived, ()), source_turn_id="t3")
    assert _r1(fresh).facts[MARK] == "creator"
    assert _plays(fresh) == "upload"


def test_w8_starting_fresh_after_a_legacy_unmarked_drop_withdraws_the_filming_wish() -> None:
    legacy = _pinned_brief()  # stored by the server's waiver before the mark existed
    assert MARK not in _r1(legacy).facts and _plays(legacy) == "filming"
    fresh = apply_updates(legacy, start_fresh_updates(legacy, ()), source_turn_id="t3")
    assert _r1(fresh).facts[MARK] == "creator"
    assert _plays(fresh) == "upload"


def test_w8_a_fresh_start_leaves_a_live_ask_the_turn_itself_changed() -> None:
    prior = _live()
    change = BriefUpdate(
        operation="change",
        target_requirement_id="r1",
        expected_version=prior.version,
        kind="order",
        scope="global",
        description="in the order I added the clips",
        facts={"key": "attachment"},
    )
    fresh = apply_updates(prior, start_fresh_updates(prior, [change]), source_turn_id="t2")
    assert _r1(fresh).status == "open" and _r1(fresh).facts["key"] == "attachment"


# -- W9: retries, and what stays refused ------------------------------------------------------


@pytest.mark.parametrize("server", [False, True])
def test_w9_applying_the_same_removal_twice_changes_nothing(server: bool) -> None:
    kwargs = {"server_removed_ids": {"r1"}} if server else {}
    once = _remove(_live(), **kwargs)
    twice = _remove(once, **kwargs)
    assert twice.requirements == once.requirements


@pytest.mark.parametrize(
    "start, who, expected",
    [
        ("unmarked", "creator", "creator"),
        ("server", "creator", "creator"),
        ("creator", "creator", "creator"),
        ("unmarked", "server", None),
        ("server", "server", "server"),
        ("creator", "server", "creator"),
    ],
)
def test_w9_a_removal_only_moves_the_mark_up(start: str, who: str, expected: str | None) -> None:
    base = _pinned_brief() if start == "unmarked" else _live()
    if start == "server":
        base = _remove(base, server_removed_ids={"r1"})
    elif start == "creator":
        base = _remove(base)
    kwargs = {"server_removed_ids": {"r1"}} if who == "server" else {}
    out = _remove(base, **kwargs)
    assert _r1(out).status == "superseded"
    assert _r1(out).facts.get(MARK) == expected


def test_w9_an_unknown_requirement_and_a_change_of_a_superseded_one_are_still_refused() -> None:
    superseded = _remove(_live())
    with pytest.raises(BriefUpdateBatchError):
        _remove(superseded, "r999")
    with pytest.raises(BriefUpdateBatchError):
        apply_updates(
            superseded,
            [
                BriefUpdate(
                    operation="change",
                    target_requirement_id="r1",
                    expected_version=superseded.version,
                    kind="order",
                    scope="global",
                    description="x",
                    facts={"key": "attachment"},
                )
            ],
            source_turn_id="t3",
        )


# -- W10: a batch can mix both ----------------------------------------------------------------


def test_w10_a_mixed_batch_labels_each_removal_by_its_own_author() -> None:
    prior = _live()
    batch = [
        BriefUpdate(operation="remove", target_requirement_id=rid, expected_version=prior.version)
        for rid in ("r1", "r6")
    ]
    out = apply_updates(prior, batch, source_turn_id="t2", server_removed_ids={"r1"})
    by_id = {r.id: r for r in out.requirements}
    assert by_id["r1"].facts[MARK] == "server"  # the server omitted the filming ask
    assert by_id["r6"].facts[MARK] == "creator"  # the creator withdrew the text ask
    assert brief_view(out).kept_filming_order is True
