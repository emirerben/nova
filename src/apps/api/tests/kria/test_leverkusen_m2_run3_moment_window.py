"""Leverkusen M2 run 3: the TOR clip played its first seconds, and the scream's slow motion
was dropped after approval.

Prod 355a33dae, 2026-10-31, thread d1ac7437, job ad2587a7 (phone, unified montage). "Order:
our selfie, the 0:0 scoreboard, the "TOR" on the big screen, then only the moments where Taha
is celebrating in both goal clips. ... Make it 12 seconds." The creator tapped clip 2
(IMG_1039, 18.3 s) for the TOR and both goal clips for Taha, then asked "put Taha's scream at
the end of clip 2 in slow motion". The video played IMG_1039 0.0-5.6 s (other guests'
children, a pan): no TOR (6.0-7.5), no scream (12.0-13.8). The review said "Done: Order", the
card before approval said "have a look" at the slow motion, and only the finished video said
"Couldn't: I couldn't tell which clip you meant".

Causes (offline, prod's own inputs in ``leverkusen_m2_run3.json``):
- IMG_1039's clip record has no analysed moments (``best_moments: []``), so its window is the
  clip's opening (``analysis_fallback``); no step seated a multi-clip window on a named moment,
  and the order outcome was judged per clip.
- The speed reader dropped "clip 2", found no "scream" in any record: ``not_grounded``.
- The account has approval readiness on, and readiness skipped the phone montage plan that
  already knew the speed could not be placed.

Ways this can fail, written before the fix (each pinned below):
 I1 the incident: a clip with no moments plays its opening and the order row says met.
 I2 a record that places the named moment still plays another window and says met.
 I3 generic words ("moments", "clips", "end") place a moment on unrelated footage.
 I4 two rows name different moments of one clip: never both met.
 I5 a photo row ("our selfie") is judged as a moment.
 I6 a row the resolver matched from the clip's own record drops from met to partial.
 I7 seating a window breaks the creator's 12 s.
 I8 the honest partial holds the draft back ("Make it without these" drops the whole order).
 I9 Turkish reads English.
 I10 the draft before approval says nothing.
 F1 "clip 2" ignored: "which clip?" though the creator named it.
 F2 "clip 2" read in play order instead of the app's numbering.
 F3 the slow motion lands on a cut of clip 2 that doesn't show the scream: "Done" on the
    wrong footage.
 F4 "2x" / "2 clips" / "2 seconds" read as clip 2.
 F5 under approval readiness the plan's verdict never reaches the draft.
 F6 readiness holds back a draft whose speed the plan places.
 F7 a renderer limit (no variableSpeed) becomes a hard block under readiness.
 F8 Turkish "2. klip" not read.

Runs the REAL planner length step (``_enrich_best_parts_snapshot``), approval readiness and
the worker's own phone montage plan (``preflight_montage_plan``); the right-content cases put
the answer key's times into the clip record as stored moments (independent evidence). No
model is called.
"""

from __future__ import annotations

import copy
import json
import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from app.config import settings
from app.kria.approval_readiness import APPROVABLE, assess_readiness, readiness_reply
from app.kria.brief import CreativeBrief
from app.kria.brief_checks import (
    build_receipts,
    is_format_limit,
    needs_creator_choice,
    plan_facts_from_unified_montage,
)
from app.kria.planner import (
    _attached_visual_ids,
    _clip_moment_notice,
    _enrich_best_parts_snapshot,
    _held_visual_pool,
)
from app.kria.reply_language import reply_language_for
from app.kria.speed_ask import _clip_number, speed_spec
from app.services import phone_montage_preflight as preflight
from app.services.creator_capabilities import resolve_creator_manifest
from app.tasks.generative_build import unified_visual

CASE: dict[str, Any] = json.loads(Path(__file__).with_name("leverkusen_m2_run3.json").read_text())
CREATOR = uuid.UUID("7b56dd6f-6b9a-4634-89c8-ce91bb50a50d")
TAHA_CLIP = "analysis-proxy-ios-49F3233F-92C5-4EC1-A239-AA4CC837F688.mp4"  # IMG_1037, clip 1
TOR_CLIP = "analysis-proxy-ios-DABF1B6F-03AC-4937-A1D3-C556EBCC21CD.mp4"  # IMG_1039, clip 2
TOR_ROW = 'then: the "TOR" on the big screen'
TAHA_ROW = "then: the moments where Taha is celebrating in both goal clips"
# The kit's answer key (a reviewed source video), as stored moments a record could carry.
TOR_MOMENT = {"start_s": 6.0, "end_s": 7.5, "energy": 7.0, "description": "TOR on the big screen"}
SCREAM_MOMENT = {
    "start_s": 12.0,
    "end_s": 13.8,
    "energy": 9.0,
    "description": "man screams with scarf",
}
# The same 6.0-7.5 stretch described as something else: right times, wrong words.
PAN_MOMENT = {**TOR_MOMENT, "description": "camera pans across the roof"}


@pytest.fixture(autouse=True)
def _prod(prod_profile, monkeypatch: pytest.MonkeyPatch) -> None:
    # Clip intents/facts are on in prod; this account had the readiness gate (the turns
    # carried `approval_readiness`). The phone retimes clips (`variableSpeed` verified).
    monkeypatch.setattr(settings, "clip_intents_enabled", True)
    monkeypatch.setattr(settings, "clip_facts_enabled", True)
    monkeypatch.setattr(settings, "kria_brief_binding_enabled", True)
    monkeypatch.setattr(settings, "kria_approval_readiness_enabled", True)
    monkeypatch.setattr(
        settings,
        "phone_render_verified_features",
        sorted({*settings.phone_render_verified_features, "variableSpeed", "stillImages"}),
    )


def _brief(**r3: Any) -> CreativeBrief:
    data = copy.deepcopy(CASE["brief"])
    for req in data["requirements"]:
        if req["id"] == "r3":
            req.update(r3)
    return CreativeBrief.model_validate(data)


def _strategy(*, resolver_matched_tor: bool = False) -> dict[str, Any]:
    strategy = copy.deepcopy(CASE["strategy"])
    if resolver_matched_tor:
        for intent in strategy["resolved_clip_intents"]:
            if intent["intent_id"] == "order_tor_screen":
                intent["assignments"][0].update(evidence="big screen shows TOR", confidence=0.9)
    return strategy


def _snapshot(**moments: list[dict[str, Any]]) -> dict[str, Any]:
    """Prod's pinned snapshot; ``tor=[...]`` / ``taha=[...]`` give that clip stored moments."""
    snapshot = copy.deepcopy(CASE["media_snapshot"])
    ids = {"tor": TOR_CLIP, "taha": TAHA_CLIP}
    for row in snapshot["clip_assignments"]:
        for key, rows in moments.items():
            if row["media_id"] == ids[key]:
                row["analysis"]["best_moments"] = copy.deepcopy(rows)
    return snapshot


def _manifest(snapshot: dict[str, Any]) -> Any:
    return resolve_creator_manifest(
        item_id="973abc03-bf34-42a3-ab76-8bf125d074e6",
        edit_format="montage",
        media=[
            *(
                {"media_id": row["media_id"], "kind": "video", "duration_s": row["duration_s"]}
                for row in snapshot["clip_assignments"]
            ),
            *({"media_id": row["media_id"], "kind": "image"} for row in snapshot["visuals"]),
        ],
    )


def _length_planned(
    brief: CreativeBrief, strategy: dict[str, Any], snapshot: dict[str, Any]
) -> dict[str, Any]:
    """The pre-approval length step the planner gate runs, on this snapshot."""
    manifest = _manifest(snapshot)
    unplanned = {k: v for k, v in snapshot.items() if k != "length_plan"}
    return _enrich_best_parts_snapshot(
        strategy,
        unplanned,
        brief,
        visual_photo_ids=_held_visual_pool(manifest, strategy),
        attached_visual_ids=_attached_visual_ids(manifest),
    )


def _visuals(snapshot: dict[str, Any]) -> list[Any]:
    return [
        unified_visual(
            SimpleNamespace(
                id=uuid.UUID(row["media_id"].removeprefix("asset-")),
                kind="image",
                gcs_path=f"users/{CREATOR}/plan/pool/{row['media_id']}.jpg",
                gcs_generation="1",
                duration_s=None,
                analysis={},
                aspect=CASE["visual_aspects"][row["media_id"]],
                source_filename=row["source_filename"],
                user_context="",
                content_hash=None,
                capture=row.get("capture"),
            )
        )
        for row in snapshot["visuals"]
    ]


def _plan(brief: CreativeBrief, strategy: dict[str, Any], snapshot: dict[str, Any]) -> dict:
    """The worker's own phone montage plan record on these inputs."""
    planned = preflight.preflight_montage_plan(
        creator_id=CREATOR,
        edit_format="montage",
        brief=brief,
        strategy=strategy,
        media_snapshot=snapshot,
        visuals=_visuals(snapshot),
        client_protocol=CASE["client_protocol"],
    )
    assert isinstance(planned, tuple), planned
    return planned[0].record()


def _receipts(brief: CreativeBrief, record: dict) -> dict[str, Any]:
    return {
        r.requirement_id: r
        for r in build_receipts(
            brief.live(), plan_facts_from_unified_montage(record), include_unchecked=True
        )
    }


def _outcomes(record: dict) -> dict[str, dict[str, Any]]:
    return {row["name"]: row for row in record["intent_outcomes"] if row["op"] == "order"}


def _window(record: dict, media_id: str) -> tuple[float, float]:
    [part] = [p for p in record["best_parts"] if p["media_id"] == media_id]
    return part["source_start_s"], part["source_end_s"]


def _speed(record: dict) -> dict[str, Any]:
    [row] = record["clip_speeds"]
    return row


# -- the incident, replayed on prod's own inputs ----------------------------------------------


def test_prod_recorded_the_incident() -> None:
    """What prod wrote (the fixture is the evidence, not a guess)."""
    prod = CASE["prod_unified_montage"]
    assert prod["best_parts"][0] == {
        "score": 0.0,
        "reason": "analysis_fallback",
        "media_id": TOR_CLIP,
        "source_end_s": 5.6,
        "source_start_s": 0.0,
    }
    assert all(row["status"] == "met" for row in prod["intent_outcomes"])
    assert prod["clip_speeds"][0]["status"] == "not_grounded"
    review = {row["requirement_id"]: row for row in CASE["prod_review_receipts"]}
    assert review["r1"]["status"] == "met"
    draft = {row["requirement_id"]: row for row in CASE["prod_draft_receipts"]}
    assert draft["r3"]["verification"] == "unchecked"
    assert "Have a look at these in the video" in CASE["prod_card"]
    # The record behind it: no analysed moment anywhere in clip 2.
    [tor] = [r for r in CASE["media_snapshot"]["clip_assignments"] if r["media_id"] == TOR_CLIP]
    assert tor["analysis"]["best_moments"] == []
    assert tor["analysis"]["understanding"]["notable_moments"] == []


def test_rebuilt_length_plan_is_prods() -> None:
    """The replay is faithful: with no moment in clip 2's record, nothing can seat the TOR,
    so the length step still gives prod's windows (I1 is said, not hidden)."""
    planned = _length_planned(_brief(), _strategy(), _snapshot())
    assert planned["length_plan"]["windows"] == CASE["media_snapshot"]["length_plan"]["windows"]


def test_worker_plan_no_longer_calls_the_order_done_or_asks_which_clip() -> None:
    """I1 + F1 + F3 on prod's inputs: clip 2 still plays 0.0-5.6 (nothing places the TOR), and
    the receipts say so; the slow motion names clip 2 and what is missing there."""
    brief = _brief()
    record = _plan(brief, _strategy(), _snapshot())
    assert _window(record, TOR_CLIP) == (0.0, 5.6)
    rows = _outcomes(record)
    assert rows["then: our selfie"]["status"] == "met"  # I5: photos are not moments
    assert rows["then: the 0:0 scoreboard"]["status"] == "met"
    assert rows[TOR_ROW]["code"] == "not_found_in_clip"
    assert rows[TOR_ROW]["clip_number"] == 2
    assert rows[TAHA_ROW]["code"] == "not_found_in_clip"  # tapped; "Taha" is in no record
    receipts = _receipts(brief, record)
    r1 = receipts["r1"]
    assert r1.status == "partial"
    assert 'the "TOR" on the big screen (clip 2)' in r1.reason
    assert is_format_limit(r1.reason)  # I8: a note, never a hold-back on the whole order
    assert _speed(record)["status"] == "moment_not_found"
    r3 = receipts["r3"]
    assert r3.status == "not_possible"
    assert r3.reason.startswith("I couldn't find that moment in clip 2")
    assert "which clip" not in r3.reason
    assert needs_creator_choice(r3)


def test_turkish_chat_reads_turkish() -> None:
    """I9 / F8."""
    brief = _brief()
    with reply_language_for("tr"):
        receipts = _receipts(brief, _plan(brief, _strategy(), _snapshot()))
        notice = _clip_moment_notice(_strategy(), CASE["media_snapshot"])
    assert receipts["r1"].reason.startswith("Oynayan klip parçasının")
    assert "(2. klip)" in receipts["r1"].reason
    assert receipts["r3"].reason.startswith("O anı 2. klipte bulamadım")
    assert notice is not None and notice.startswith("2. klipte")
    assert is_format_limit(receipts["r1"].reason)


# -- before approval: the creator hears it before the card -------------------------------------


def test_draft_names_the_tor_and_the_part_of_clip_2_that_plays() -> None:
    """I10: the pre-approval reply says which named moment no record places, and what plays."""
    notice = _clip_moment_notice(_strategy(), CASE["media_snapshot"])
    assert notice is not None
    assert 'the "TOR" on the big screen in clip 2' in notice
    assert "from 0.0 s to 5.6 s" in notice
    # Clip 1 plays 5.0-9.0 of 9.8 s: Taha is in no record either, so it is said too.
    assert "clip 1" in notice


def test_without_readiness_the_plan_holds_the_slow_motion_back() -> None:
    """The KRI-775 preflight path (accounts without readiness)."""
    blockers = preflight.montage_blockers(
        creator_id=CREATOR,
        edit_format="montage",
        brief=_brief(),
        strategy=_strategy(),
        media_snapshot=_snapshot(),
        visuals=_visuals(_snapshot()),
        client_protocol=CASE["client_protocol"],
    )
    assert blockers is not None
    by_id = {r.requirement_id: r for r in blockers}
    assert set(by_id) == {"r3"}  # the order note is a limit, not a blocker (I8)
    assert by_id["r3"].reason.startswith("I couldn't find that moment in clip 2")


def _readiness(
    brief: CreativeBrief, strategy: dict[str, Any], snapshot: dict[str, Any]
) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    """Readiness as the runtime runs it: the plan's speed blockers, then the report."""
    blockers = preflight.readiness_plan_blockers(
        None,
        creator_id=CREATOR,
        item=SimpleNamespace(id="973abc03", edit_format="montage"),
        brief=brief,
        strategy=strategy,
        media_snapshot=snapshot,
        client_protocol=CASE["client_protocol"],
    )
    report = assess_readiness(
        brief,
        strategy,
        media=snapshot,
        manifest=_manifest(snapshot),
        creator_id=CREATOR,
        plan_blockers=blockers,
    )
    return {row["requirement_id"]: row for row in report["requirements"]}, report


def test_readiness_says_the_slow_motion_cannot_be_placed_before_the_card(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """F5: the incident turn (readiness on). Prod rated r3 "unverified" and offered the card."""
    monkeypatch.setattr(preflight, "_ready_visuals", lambda *_a, **_k: _visuals(_snapshot()))
    brief, snapshot = _brief(), _snapshot()
    unplanned = assess_readiness(
        brief, _strategy(), media=snapshot, manifest=_manifest(snapshot), creator_id=CREATOR
    )
    assert {r["requirement_id"]: r["disposition"] for r in unplanned["requirements"]}["r3"] == (
        "unverified"
    )  # prod's verdict, without the plan
    rows, report = _readiness(brief, _strategy(), snapshot)
    assert rows["r3"]["disposition"] not in APPROVABLE
    assert rows["r3"]["reason"].startswith("I couldn't find that moment in clip 2")
    assert not report["ready"]
    assert "Put Taha’s scream at the end of clip 2 in slow motion" in readiness_reply(brief, report)
    # Nothing else changes: the order and length verdicts are readiness's own.
    assert {k: v["disposition"] for k, v in rows.items() if k != "r3"} == {
        k: v["disposition"]
        for k, v in ((r["requirement_id"], r) for r in unplanned["requirements"])
        if k != "r3"
    }


def test_readiness_is_unchanged_where_the_plan_places_the_speed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """F6: "put clip 2 in slow motion" lands on clip 2's cut, so nothing is held back and the
    report is byte-for-byte the one an approval re-check (which runs no plan) computes."""
    monkeypatch.setattr(preflight, "_ready_visuals", lambda *_a, **_k: _visuals(_snapshot()))
    brief = _brief(description="Put clip 2 in slow motion")
    snapshot = _snapshot()
    record = _plan(brief, _strategy(), snapshot)
    assert _speed(record)["status"] == "applied"
    rows, report = _readiness(brief, _strategy(), snapshot)
    recheck = assess_readiness(
        brief, _strategy(), media=snapshot, manifest=_manifest(snapshot), creator_id=CREATOR
    )
    assert report["digest"] == recheck["digest"]


def test_readiness_keeps_a_renderer_limit_a_note(monkeypatch: pytest.MonkeyPatch) -> None:
    """F7: with no verified ``variableSpeed`` the plan's "can't change clip speed" is a limit,
    as before: readiness does not turn it into a hold-back."""
    monkeypatch.setattr(
        settings,
        "phone_render_verified_features",
        [f for f in settings.phone_render_verified_features if f != "variableSpeed"],
    )
    monkeypatch.setattr(preflight, "_ready_visuals", lambda *_a, **_k: _visuals(_snapshot()))
    rows, _report = _readiness(_brief(), _strategy(), _snapshot())
    assert rows["r3"]["disposition"] == "unverified"


# -- the right footage: a record that places the moments -----------------------------------------


def test_a_placed_tor_decides_clip_2s_window_and_the_order_is_met() -> None:
    """I2: with the TOR stored at 6.0-7.5 (the scream, at higher energy, would win without the
    seat), the length step seats clip 2 on the TOR and the order is met."""
    brief, strategy = _brief(), _strategy()
    snapshot = _length_planned(brief, strategy, _snapshot(tor=[TOR_MOMENT, SCREAM_MOMENT]))
    record = _plan(brief, strategy, snapshot)
    start, end = _window(record, TOR_CLIP)
    assert start <= 6.0 and end >= 7.5, (start, end)
    assert sum(p["source_end_s"] - p["source_start_s"] for p in record["best_parts"]) + 2.4 == (
        pytest.approx(12.0, abs=0.1)
    )  # I7: still the creator's 12 s
    assert _outcomes(record)[TOR_ROW]["status"] == "met"
    # The placed TOR is not announced as missing; Taha (in no record) still is.
    notice = _clip_moment_notice(strategy, snapshot)
    assert notice is not None and '"TOR"' not in notice


def test_wrong_words_on_the_right_times_are_not_the_tor() -> None:
    """Wrong-content control: the same 6.0-7.5 stretch described as a pan. Count, order and
    length are unchanged, so only the content check can reject it, and it does."""
    brief, strategy = _brief(), _strategy()
    snapshot = _length_planned(brief, strategy, _snapshot(tor=[PAN_MOMENT, SCREAM_MOMENT]))
    record = _plan(brief, strategy, snapshot)
    assert _outcomes(record)[TOR_ROW]["code"] == "not_found_in_clip"
    assert _receipts(brief, record)["r1"].status == "partial"


def test_slow_motion_never_lands_on_a_cut_that_does_not_show_the_scream() -> None:
    """F3: clip 2 is seated on the TOR (the creator's order asked for it there); the scream is
    placed at 12.0-13.8 but that cut doesn't show it, so nothing is slowed and the reply says
    so. Two moments of one clip are never both met (I4)."""
    brief, strategy = _brief(), _strategy()
    snapshot = _length_planned(brief, strategy, _snapshot(tor=[TOR_MOMENT, SCREAM_MOMENT]))
    record = _plan(brief, strategy, snapshot)
    speed = _speed(record)
    assert speed["status"] == "moment_not_shown" and speed["cut_ids"] == []
    r3 = _receipts(brief, record)["r3"]
    assert r3.status == "not_possible"
    assert r3.reason.startswith("The part of clip 2 in this video doesn't show that moment")


def test_slow_motion_lands_on_the_scream_when_clip_2_plays_it() -> None:
    """The whole chain right: clip 2's record places only the scream, its window holds it, and
    only that cut plays at 0.5x."""
    brief, strategy = _brief(), _strategy()
    snapshot = _length_planned(brief, strategy, _snapshot(tor=[SCREAM_MOMENT]))
    record = _plan(brief, strategy, snapshot)
    start, end = _window(record, TOR_CLIP)
    assert start <= 12.0 + 0.9 and end >= 13.8 - 0.9, (start, end)
    speed = _speed(record)
    assert speed["status"] == "applied" and speed["grounding"] == "clip_moment"
    assert speed["media_ids"] == [TOR_CLIP] and speed["rate"] == 0.5
    r3 = _receipts(brief, record)["r3"]
    assert r3.status == "met" and "0.5x" in r3.reason


def test_a_row_the_resolver_matched_keeps_its_clip_level_verdict() -> None:
    """I6: the clip's own record matched the TOR row (not a tap): no moment check is invented
    where none ran before, so the row stays met."""
    brief = _brief()
    record = _plan(brief, _strategy(resolver_matched_tor=True), _snapshot())
    assert _outcomes(record)[TOR_ROW]["status"] == "met"


# -- reading "clip 2" --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("words", "number"),
    [
        ("Put Taha's scream at the end of clip 2 in slow motion", 2),
        ("slow down video two", 2),
        ("make clip 3 2x faster", 3),
        ("2. klibin sonunu ağır çekim yap", 2),
        ("klip 2 yavaşlat", 2),
        ("slow motion on clip 1 and clip 2", None),  # two clips: nothing guessed
        ("speed up 2 clips", None),
        ("2 klibi hızlandır", None),  # Turkish counts with a singular noun
        ("slow the scream down for 2 seconds", None),
        ("make it 2x slower", None),
        ("take 2 seconds and slow it down", None),
    ],
)
def test_clip_number_reader(words: str, number: int | None) -> None:
    """F1, F4, F8."""
    assert _clip_number(words) == number


def test_speed_spec_keeps_the_moment_words_beside_the_clip_number() -> None:
    [req] = [r for r in _brief().live() if r.id == "r3"]
    spec = speed_spec(req)
    assert spec is not None
    assert spec.clip_number == 2 and spec.rate == 0.5
    assert spec.target_words == ("taha", "scream")
    assert spec.position is None  # "the end of" is inside clip 2, not the edit's last cut
