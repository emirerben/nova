"""KRI-1592: an order ask on a writer that gets no order credit is declined plainly, never held.

#2458 (Toroko, prod threads 666b996f and de471354) made an unstated clip order the order the
clips were added (`KRIA_UNSTATED_ORDER_DEFAULT_ENABLED`). The render contract pins that order and
approval readiness credits it only on the phone unified montage with no Visuals: the one writer
that lays out exactly that order and whose verifier refuses any other. The read-side group gate
(`unconfirmed_order_groups`) is global, so with the flag on every other writer lost the clearer
"what order inside the group?" question and held the draft on the generic "I can't confirm this
draft does it yet" (`evidence_missing`), with nothing the creator could answer. A single "start
with X" brief was held on the same line on those writers.

The rule now: with the flag on, an order ask nothing in the draft commits to (no writer credit,
no order pinned in the render contract) is rated ``declined`` with one plain line in the chat's
language, and the draft is offered. A pinned order, and every checker's own verdict, keep today's
path.

Ways this can fail, written before the fix (each is pinned below):

 F1 the incident on each writer that gets no credit (a montage showing a Visuals photo, a
    voiceover montage on the native program, a Talking edit with one "start with X" take): the
    draft is still held, or still on the generic line.
 F2 an order the contract DOES pin (the phone verifier refuses a render that plays another) is
    declined on a writer readiness cannot vouch for: approval passes and the render is refused
    afterwards (the KRI-503 dead end). It must stay held as today.
 F3 a checker's own verdict is hidden by the decline: the contract's "which clip do you mean?"
    question, or a plan that places the order wrongly.
 F4 the flag off changes anything (the group question, the generic hold).
 F5 the credited route (unified montage, no Visuals) changes: it must stay `executable` with the
    "where you didn't say" disclosure.
 F6 the line claims an order the writer does not play (e.g. "the order you added them"), names a
    media id, UUID or file name, or exists in English only (Turkish and German too).
 F7 the decline turns into the "simpler version?" / "make it without these" card that drops the
    order: it must read as a format limit, not a choice.
 F8 the draft card's own receipt still says "I can't confirm the order this draft uses" while
    readiness approved the row as declined (card and readiness disagree), or a receipt for
    anything else changes.
 F9 the approval re-check of the saved draft disagrees with the report saved with it.
 F10 the ask's other requirements change verdict.
 F11 the montage showing Visuals is approved as "can't promise", but the phone montage plan its
    worker builds misses the order, so the worker refuses the render after approval. The plan's
    own checked order miss must hold the draft before approval (`readiness_plan_blockers`).

Runs the REAL approval readiness (manifest, policy re-check, render contract, receipts), the
route resolver the dispatch mirrors (`route_capabilities.resolve_route_id`) and the draft
turn's own receipts (`plan_facts_from_strategy`). No model is called.

Recorded vs reconstructed: the Toroko request and clip rows come from the #2458 replay
(`test_unstated_order_default.py`, itself cloned from the recorded Leverkusen M1 run 2 rows);
the Talking takes and brief come from the recorded S3 Yorum2 thread (`s3_yorum2_talking_order.
json`). RECONSTRUCTED here: the Visuals photo beside the Toroko clips, the voiceover beside the
first ten of them (the native voiceover writer takes at most twelve clips), and a "start with"
pin with no ordering basis on the Talking takes (the recorded r1 named filming order).
"""

from __future__ import annotations

import copy
import json
import uuid
from pathlib import Path
from typing import Any

import pytest

from app.config import settings
from app.kria.approval_readiness import (
    APPROVABLE,
    assess_readiness,
    readiness_matches,
    with_declined_order_receipts,
)
from app.kria.brief import CreativeBrief
from app.kria.brief_checks import (
    build_receipts,
    is_judged,
    needs_creator_choice,
    plan_facts_from_strategy,
)
from app.kria.contracts import RequirementReceipt
from app.kria.reply_language import reply_language_for
from app.kria.route_capabilities import resolve_route_id
from app.services.creator_capabilities import resolve_creator_manifest
from app.services.creator_render_contract import build_render_contract
from tests.kria.test_unstated_order_default import (
    _IDS,
    CREATOR,
    GROUPS,
    ITEM,
    _brief,
    _drafted,
    _snapshot,
)

DECLINE_EN = (
    "I can't promise the clip order on this kind of video yet, so it may not match what you asked"
)
DECLINE_TR = (
    "Bu tür bir videoda klip sırasını henüz garanti edemiyorum, o yüzden sıra istediğin gibi "
    "olmayabilir"
)
GENERIC_HOLD = "I can't confirm this draft does it yet."
DISCLOSURE_EN = "where you didn't say, the clips play in the order you added them"

S3: dict[str, Any] = json.loads(
    (Path(__file__).resolve().parents[1] / "fixtures" / "s3_yorum2_talking_order.json").read_text()
)
S3_CREATOR = uuid.UUID("7b56dd6f-6b9a-4634-89c8-ce91bb50a50d")
PHOTO = "asset-00000000-0000-4000-8000-0000000000aa"
VOICEOVER_CLIPS = 10


@pytest.fixture(autouse=True)
def _prod(prod_profile, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "clip_intents_enabled", True)
    monkeypatch.setattr(settings, "clip_facts_enabled", True)
    monkeypatch.setattr(settings, "kria_brief_binding_enabled", True)
    monkeypatch.setattr(settings, "kria_approval_readiness_enabled", True)
    # Every account renders on the phone (prod's empty allowlist) and several-take Talking is on.
    monkeypatch.setattr(settings, "phone_render_user_ids", [])
    monkeypatch.setattr(settings, "talking_multi_clip_enabled", True)


def _flag(monkeypatch: pytest.MonkeyPatch, on: bool) -> None:
    monkeypatch.setattr(settings, "kria_unstated_order_default_enabled", on)


# -- one draft per writer ------------------------------------------------------------------------


def _toroko(single: bool) -> tuple[CreativeBrief, dict[str, Any], dict[str, Any]]:
    """The Toroko request: the four described groups, or only "the map video first"."""
    if single:
        brief = _brief(first_clip="the map video")
        groups = (("order_map_first", "the map video", "first", (5,), (5,)),)
    else:
        brief, groups = _brief(), GROUPS
    return brief, _drafted(brief, groups=groups), _snapshot()


def _visuals(single: bool) -> dict[str, Any]:
    """The Toroko montage showing one Visuals photo (a Visual is spread, never seated by the
    order the clips were added, so the contract pins nothing)."""
    brief, strategy, media = _toroko(single)
    strategy["selected_media_ids"] = [*strategy["selected_media_ids"], PHOTO]
    media["visuals"] = [
        {"kind": "image", "capture": {}, "media_id": PHOTO, "source_filename": "photo.jpeg"}
    ]
    manifest = resolve_creator_manifest(
        item_id=ITEM,
        edit_format="montage",
        media=[
            *(
                {"media_id": row["media_id"], "kind": "video", "duration_s": row["duration_s"]}
                for row in media["clip_assignments"]
            ),
            {"media_id": PHOTO, "kind": "image"},
        ],
    )
    return {"brief": brief, "strategy": strategy, "media": media, "manifest": manifest}


def _voiceover(single: bool) -> dict[str, Any]:
    """The Toroko clips cut under a recorded voiceover: the native program's voiceover montage
    writer, which follows the voice and pins no clip order."""
    brief, strategy, media = _toroko(single)
    keep = [row["media_id"] for row in media["clip_assignments"][:VOICEOVER_CLIPS]]
    media["clip_assignments"] = media["clip_assignments"][:VOICEOVER_CLIPS]
    media["clip_paths"] = media["clip_paths"][:VOICEOVER_CLIPS]
    for key in ("sources", "windows"):
        media["length_plan"][key] = media["length_plan"][key][:VOICEOVER_CLIPS]
    media.update(
        voiceover_path="voiceovers/toroko.m4a",
        voiceover_duration_s=40.0,
        voiceover={"duration_s": 40.0},
    )
    intents = []
    for intent in strategy["resolved_clip_intents"]:
        assignments = [a for a in intent["assignments"] if a["media_id"] in keep]
        if assignments:
            intents.append({**intent, "assignments": assignments})
    strategy = {
        **{k: v for k, v in strategy.items() if k not in {"montage_audio", "voice_mode"}},
        "audio_strategy": "voiceover",
        "render_program": "native",
        "target_duration_s": 40.0,
        "selected_media_ids": keep,
        "resolved_clip_intents": intents,
        "clip_intents": [
            row
            for row in strategy["clip_intents"]
            if row["intent_id"] in {i["intent_id"] for i in intents}
        ],
    }
    manifest = resolve_creator_manifest(
        item_id=ITEM,
        edit_format="montage",
        has_voiceover=True,
        narration={"gcs_path": "voiceovers/toroko.m4a", "duration_s": 40.0, "generation": "1"},
        media=[
            {"media_id": row["media_id"], "kind": "video", "duration_s": row["duration_s"]}
            for row in media["clip_assignments"]
        ],
        phone_source_media_ids=keep,
        phone_rendering_allowed=True,
        runs_guided_voiceover=False,
    )
    return {"brief": brief, "strategy": strategy, "media": media, "manifest": manifest}


def _talking(facts: dict[str, Any], media: dict[str, Any] | None = None) -> dict[str, Any]:
    """The S3 Yorum2 takes on the phone Talking writer (native program), r1 reset to ``facts``."""
    data = copy.deepcopy(S3["brief"])
    data["requirements"][0]["facts"] = facts
    data["requirements"][0]["description"] = "Start with the 20-second home clip"
    snapshot = media if media is not None else copy.deepcopy(S3["snapshot"])
    manifest = resolve_creator_manifest(
        item_id=str(uuid.uuid5(uuid.NAMESPACE_URL, "kri1592-talking")),
        edit_format="subtitled",
        media=[
            {
                "media_id": row["media_id"],
                "kind": "video",
                "duration_s": row["analysis"]["duration_s"],
            }
            for row in snapshot["clip_assignments"]
        ],
    )
    return {
        "brief": CreativeBrief.model_validate(data),
        "strategy": copy.deepcopy(S3["strategy"]),
        "media": snapshot,
        "manifest": manifest,
        "creator": S3_CREATOR,
    }


ROWS = {
    # row id: (draft, the route the dispatch takes, the order ask's id)
    "visuals_groups": (lambda: _visuals(single=False), "unified_montage", "r2"),
    "visuals_start_with": (lambda: _visuals(single=True), "unified_montage", "r2"),
    "voiceover_groups": (lambda: _voiceover(single=False), "voiceover_montage", "r2"),
    "voiceover_start_with": (lambda: _voiceover(single=True), "voiceover_montage", "r2"),
    "talking_start_with": (
        lambda: _talking({"first_clip": "20-second home clip"}),
        "talking_head",
        "r1",
    ),
}


def _report(draft: dict[str, Any]) -> dict[str, Any]:
    return assess_readiness(
        draft["brief"],
        draft["strategy"],
        media=draft["media"],
        manifest=draft["manifest"],
        creator_id=draft.get("creator", CREATOR),
    )


def _rows(report: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {row["requirement_id"]: row for row in report["requirements"]}


def _route(draft: dict[str, Any]) -> str | None:
    route = resolve_route_id(
        creator_id=draft.get("creator", CREATOR),
        edit_format=draft["strategy"].get("edit_format"),
        strategy=draft["strategy"],
        clip_paths=draft["media"]["clip_paths"],
    )
    return str(route) if route is not None else None


def _pins(draft: dict[str, Any]) -> bool:
    contract = build_render_contract(
        draft["strategy"],
        generation_id="kri1592",
        brief=draft["brief"],
        media_snapshot=draft["media"],
    )
    return bool(contract is not None and contract.order_required and contract.order_ids)


# -- F1 + F4: the incident on every writer that gets no credit ---------------------------------


@pytest.mark.parametrize("row", sorted(ROWS))
def test_the_order_ask_was_held_and_is_now_declined_with_the_draft_offered(
    row: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    build, route, order_id = ROWS[row]
    _flag(monkeypatch, False)
    draft = build()
    assert _route(draft) == route
    before = _rows(_report(draft))
    # Flag off is today: the group question (a real question) or the generic hold.
    assert before[order_id]["disposition"] in {"needs_choice", "evidence_missing"}, before

    _flag(monkeypatch, True)
    draft = build()
    assert not _pins(draft), "this writer's contract pins no order, so nothing is refused later"
    report = _report(draft)
    after = _rows(report)
    assert after[order_id]["disposition"] == "declined", after[order_id]
    assert after[order_id]["reason"] == DECLINE_EN
    # The order ask no longer holds the draft; what still holds (the recorded Talking thread's
    # pop-in asks, whose pictures are not in this snapshot) held for its own reason before too.
    held = {rid for rid, r in after.items() if r["disposition"] not in APPROVABLE}
    assert held == {rid for rid, r in before.items() if r["disposition"] not in APPROVABLE} - {
        order_id
    }
    assert report["ready"] is (not held)
    assert report["policy_problem"] is None and report["policy_refusal"] is None
    if route != "talking_head":
        assert report["ready"] is True, report["requirements"]
        assert GENERIC_HOLD not in json.dumps(report["requirements"])


def test_flag_off_is_unchanged_on_every_row(monkeypatch: pytest.MonkeyPatch) -> None:
    """F4: no row reads the decline with the flag off (the group question or the hold stays)."""
    _flag(monkeypatch, False)
    for row, (build, _route_id, order_id) in ROWS.items():
        report = _report(build())
        order = _rows(report)[order_id]
        assert order["disposition"] != "declined" and order["reason"] != DECLINE_EN, row
        assert report["ready"] is False, row


# -- F2: an order the contract pins stays held -------------------------------------------------


def test_a_pinned_order_on_a_writer_readiness_cannot_vouch_for_stays_held(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The recorded S3 Yorum2 ask (filming order, first and last take) pins the takes in the
    contract; a clip that cannot talk plays as a cutaway the verifier counts, so readiness gives
    no credit. Declining would pass approval and the phone verifier would refuse the render."""
    _flag(monkeypatch, True)
    media = copy.deepcopy(S3["snapshot"])
    media["clip_assignments"][-1]["analysis"]["understanding"]["speech"] = {
        "has_speech": False,
        "transcript": "",
    }
    draft = _talking(
        {"key": "capture_time", "first_clip": "20-second home clip", "last_clip": "the car clip"},
        media,
    )
    assert _pins(draft)
    order = _rows(_report(draft))["r1"]
    assert order["disposition"] == "evidence_missing", order
    assert order["reason"] == GENERIC_HOLD


def test_a_cloud_draft_whose_contract_pins_the_added_order_stays_held(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Off the phone the contract still pins the order the clips were added (#2458) and no
    writer is credited: a pin, so the hold stays (the cloud is not this ticket's writer)."""
    _flag(monkeypatch, True)
    monkeypatch.setattr(settings, "ios_device_only_mode", False)
    monkeypatch.setattr(settings, "phone_rendering_enabled", False)
    brief, strategy, media = _toroko(single=False)
    draft = {
        "brief": brief,
        "strategy": strategy,
        "media": media,
        "manifest": resolve_creator_manifest(
            item_id=ITEM,
            edit_format="montage",
            media=[
                {"media_id": row["media_id"], "kind": "video", "duration_s": row["duration_s"]}
                for row in media["clip_assignments"]
            ],
        ),
    }
    assert _route(draft) is None and _pins(draft)
    assert _rows(_report(draft))["r2"]["disposition"] == "evidence_missing"


# -- F3: a checker's own verdict is never hidden -----------------------------------------------


def test_the_contracts_which_clip_question_is_still_asked(monkeypatch: pytest.MonkeyPatch) -> None:
    """A take nobody can tell apart ("the 7-second clip": 7.0 s and 7.15 s) is asked by name."""
    _flag(monkeypatch, True)
    draft = _talking(
        {"key": "capture_time", "first_clip": "the 7-second clip", "last_clip": "the car clip"}
    )
    order = _rows(_report(draft))["r1"]
    assert order["disposition"] == "unsupported", order
    assert "the 7-second clip" in order["reason"] and order["reason"] != DECLINE_EN


def test_a_plan_that_places_the_order_wrongly_still_holds(monkeypatch: pytest.MonkeyPatch) -> None:
    """A checked "not where you asked" from the phone plan (KRI-775 blockers) is never turned
    into a decline."""
    _flag(monkeypatch, True)
    draft = _visuals(single=True)
    blocker = RequirementReceipt(
        requirement_id="r2",
        status="not_possible",
        verification="checked",
        stage="checked",
        reason="I couldn't place everything you listed in the order you gave.",
    )
    report = assess_readiness(
        draft["brief"],
        draft["strategy"],
        media=draft["media"],
        manifest=draft["manifest"],
        creator_id=CREATOR,
        plan_blockers=[blocker],
    )
    order = _rows(report)["r2"]
    assert order["disposition"] not in APPROVABLE and order["reason"] != DECLINE_EN, order


# -- F5: the credited route is untouched --------------------------------------------------------


def test_the_unified_montage_without_visuals_keeps_its_credit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _flag(monkeypatch, True)
    brief, strategy, media = _toroko(single=True)
    draft = {
        "brief": brief,
        "strategy": strategy,
        "media": media,
        "manifest": resolve_creator_manifest(
            item_id=ITEM,
            edit_format="montage",
            media=[
                {"media_id": row["media_id"], "kind": "video", "duration_s": row["duration_s"]}
                for row in media["clip_assignments"]
            ],
        ),
    }
    order = _rows(_report(draft))["r2"]
    assert order["disposition"] == "executable" and DISCLOSURE_EN in order["reason"], order


# -- F6: what a creator reads -------------------------------------------------------------------


@pytest.mark.parametrize("row", sorted(ROWS))
def test_the_line_is_plain_words_in_turkish_with_no_id_or_file_name(
    row: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    _flag(monkeypatch, True)
    build, _route_id, order_id = ROWS[row]
    with reply_language_for("tr"):
        report = _report(build())
    order = _rows(report)[order_id]
    assert order["disposition"] == "declined" and order["reason"] == DECLINE_TR, order
    assert not _IDS.search(order["reason"])
    for claim in ("added", "eklediğin", "filming", "çekim"):
        assert claim not in order["reason"], "the line never claims an order this writer plays"


def test_german_reads_its_own_line(monkeypatch: pytest.MonkeyPatch) -> None:
    _flag(monkeypatch, True)
    monkeypatch.setattr(settings, "kria_german_replies_enabled", True, raising=False)
    with reply_language_for("de"):
        report = _report(_visuals(single=False))
    reason = _rows(report)["r2"]["reason"]
    assert reason not in {DECLINE_EN, DECLINE_TR} and "Reihenfolge" in reason, reason
    assert not _IDS.search(reason)


# -- F7 + F8: the draft card's own receipt says the same --------------------------------------


def _draft_receipts(draft: dict[str, Any]) -> list[dict[str, Any]]:
    """The receipts the draft turn records for its own strategy (`plan_facts_from_strategy`)."""
    facts = plan_facts_from_strategy(
        draft["strategy"],
        clip_ids=[row["media_id"] for row in draft["media"]["clip_assignments"]],
        manifest=draft["manifest"],
    )
    return [
        receipt.model_dump(mode="json")
        for receipt in build_receipts(
            draft["brief"].live(), facts, include_unchecked=True, strict_order=True
        )
    ]


@pytest.mark.parametrize(("language", "line"), [("en", DECLINE_EN), ("tr", DECLINE_TR)])
def test_the_draft_receipt_carries_the_decline_as_a_format_limit(
    language: str, line: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    _flag(monkeypatch, True)
    draft = _visuals(single=False)
    with reply_language_for(language):
        receipts = _draft_receipts(draft)
        report = _report(draft)
        shown = with_declined_order_receipts(receipts, report)
    before = {r["requirement_id"]: r for r in receipts}
    after = {r["requirement_id"]: r for r in shown}
    assert (before["r2"]["status"], before["r2"]["verification"]) == ("partial", "unchecked")
    assert (after["r2"]["status"], after["r2"]["verification"], after["r2"]["reason"]) == (
        "not_possible",
        "checked",
        line,
    )
    receipt = RequirementReceipt.model_validate(after["r2"])
    req = next(r for r in draft["brief"].live() if r.id == "r2")
    assert is_judged(req, receipt), "said as an answer, not 'have a look'"
    assert not needs_creator_choice(receipt), "a limit of this video, never a simpler version"
    assert after["r1"] == before["r1"]


def test_a_draft_with_nothing_declined_keeps_its_receipts(monkeypatch: pytest.MonkeyPatch) -> None:
    """F8: the flag off, or a credited draft, leaves every receipt as it was."""
    draft = _visuals(single=False)
    _flag(monkeypatch, False)
    receipts = _draft_receipts(draft)
    assert with_declined_order_receipts(receipts, _report(draft)) == receipts
    _flag(monkeypatch, True)
    brief, strategy, media = _toroko(single=False)
    credited = {**draft, "brief": brief, "strategy": strategy, "media": media}
    credited["manifest"] = resolve_creator_manifest(
        item_id=ITEM,
        edit_format="montage",
        media=[
            {"media_id": row["media_id"], "kind": "video", "duration_s": row["duration_s"]}
            for row in media["clip_assignments"]
        ],
    )
    receipts = _draft_receipts(credited)
    assert with_declined_order_receipts(receipts, _report(credited)) == receipts


# -- F9 + F10 ------------------------------------------------------------------------------------


@pytest.mark.parametrize("row", sorted(ROWS))
def test_the_saved_report_matches_a_fresh_one_and_other_asks_keep_their_verdict(
    row: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    build, _route_id, order_id = ROWS[row]
    _flag(monkeypatch, False)
    held = _rows(_report(build()))
    _flag(monkeypatch, True)
    saved = _report(build())
    fresh = _report(build())
    assert saved["digest"] == fresh["digest"]
    # Approval re-checks a ready draft against the report saved with it (the Talking thread's
    # pop-in asks keep it held, so only its digest is compared).
    assert readiness_matches(copy.deepcopy(saved), fresh) is saved["ready"]
    others = {k: v for k, v in _rows(saved).items() if k != order_id}
    assert others == {k: v for k, v in held.items() if k != order_id}


# -- F11: the montage plan's own order miss still holds the draft --------------------------------


def _visual_rows() -> list[Any]:
    """The ready Visuals row the worker places, as `_ready_visuals` returns it."""
    from types import SimpleNamespace

    from app.tasks.generative_build import unified_visual

    return [
        unified_visual(
            SimpleNamespace(
                id=uuid.UUID(PHOTO.removeprefix("asset-")),
                kind="image",
                gcs_path=f"pool/{PHOTO}.jpg",
                gcs_generation="9",
                duration_s=None,
                analysis={},
                aspect=4 / 3,
                source_filename="photo.jpeg",
                user_context="",
                content_hash=None,
                capture={},
            )
        )
    ]


@pytest.mark.parametrize("misplaced", [False, True], ids=["placed", "misplaced"])
def test_a_visuals_plan_that_misses_the_order_holds_before_approval(
    misplaced: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Runs the real preflight (`readiness_plan_blockers` -> the unified montage planner). The
    negative control swaps the planner's seating, as the worker's own receipt test does
    (`test_unstated_order_default.test_a_creator_placed_clip_in_the_wrong_seat_does_not_read_met`)."""
    from types import SimpleNamespace

    from app.pipeline import unified_montage
    from app.services import phone_montage_preflight

    _flag(monkeypatch, True)
    monkeypatch.setattr(settings, "kria_phone_montage_preflight_enabled", True)
    monkeypatch.setattr(phone_montage_preflight, "_ready_visuals", lambda *a, **k: _visual_rows())
    if misplaced:
        seat = unified_montage._apply_sequence

        def swapped(ordered, rows):
            result = list(seat(ordered, rows))
            result[0], result[-1] = result[-1], result[0]
            return result

        monkeypatch.setattr(unified_montage, "_apply_sequence", swapped)
    draft = _visuals(single=False)
    blockers = phone_montage_preflight.readiness_plan_blockers(
        None,
        creator_id=CREATOR,
        item=SimpleNamespace(id=ITEM, edit_format="montage"),
        brief=draft["brief"],
        strategy=draft["strategy"],
        media_snapshot=draft["media"],
    )
    report = assess_readiness(
        draft["brief"],
        draft["strategy"],
        media=draft["media"],
        manifest=draft["manifest"],
        creator_id=CREATOR,
        plan_blockers=blockers,
    )
    order = _rows(report)["r2"]
    if misplaced:
        assert [b.requirement_id for b in blockers] == ["r2"], blockers
        assert order["disposition"] not in APPROVABLE and order["reason"] != DECLINE_EN, order
        assert report["ready"] is False
    else:
        assert blockers == []
        assert order["disposition"] == "declined" and report["ready"] is True, order


def test_flag_off_the_preflight_still_leaves_order_to_readiness(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """F4 for F11: the flag off returns no order blocker (byte-identical to today)."""
    from types import SimpleNamespace

    from app.services import phone_montage_preflight

    _flag(monkeypatch, False)
    called: list[Any] = []
    monkeypatch.setattr(
        phone_montage_preflight, "phone_montage_blockers", lambda *a, **k: called.append(1) or []
    )
    draft = _visuals(single=False)
    assert (
        phone_montage_preflight.readiness_plan_blockers(
            None,
            creator_id=CREATOR,
            item=SimpleNamespace(id=ITEM, edit_format="montage"),
            brief=draft["brief"],
            strategy=draft["strategy"],
            media_snapshot=draft["media"],
        )
        == []
    )
    assert called == [], "no speed or keep-whole ask and the flag off: the plan is not run"
