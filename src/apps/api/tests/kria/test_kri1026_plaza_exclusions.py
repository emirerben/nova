"""KRI-1026: the clips the creator tapped out of the Plaza de Mayo montage still play.

Prod thread A228E85B (Plaza de Mayo kit M1, "maximo 3", 2026-10-28, Fly v1904). Prompt:
"Montage of our afternoon at Plaza de Mayo, in the order we filmed it. ... Skip anything
too short to use. ..." Seven clips: five of 11.6-23.4 s, a 0.97 s square shot (J1) and a
0.2 s mis-tap with no audio track (J2). The creator tapped J1 and J2 out of the "too short"
picker (KRI-1031 is why there was a picker at all). The draft after a title-only follow-up
("Make the title just "Plaza de Mayo", no flag. Keep everything else and render it.") still
carried the resolved exclusion, but its pick was empty, so the render took all seven clips:
J1 played at 6.7-7.7 s and J2 flashed at 15.8 s.

Root cause: ``clip_scope.excluded_clips`` keeps any clip another resolved intent names, and
"in the order we filmed it" is a capture-time order whose assignments are EVERY clip. So the
filming-order ask protected every clip from every exclusion; the turn-3 draft only left the
junk out because the Main Creator happened to pick the five real clips itself.

Ways this could go wrong, written down before the code:

1. A whole-set order (filming order, route) counts as naming each clip, so it cancels every
   exclusion (the incident).
2. A re-draft whose pick is empty or "all" (a title-only follow-up) never re-applies the
   exclusion.
3. The narrowed pick does not reach dispatch, the render contract's capture-time
   ``order_ids`` or the montage timeline, so an excluded clip renders anyway.
4. A clip the creator tapped out is kept because a resolver GUESS for another ask (a label,
   an include) matched the same clip. A tap is the creator's word; a guess is not.
5. A real contradiction is now dropped silently the other way: an ask the creator settled
   on the same clip, or media the strategy places, loses its clip.
6. An exclusion that cannot be applied (it would leave no clip, the strategy plays the clip,
   another settled ask names it) is dropped without a word: the draft must say which clips
   stay.
7. A false alarm: the notice when the clips did leave.
8. Flag off (CLIP_INTENT_EXCLUDE_ENABLED): the pick or the reply changes.

KRI-1031 (the length rule that makes the picker unnecessary) is in
``test_kri1031_too_short_clips.py``.
"""

from __future__ import annotations

import copy
import json
import uuid
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from app.agents._schemas.creator_agent import CreativeStrategy, ResolvedCreatorManifest
from app.agents.main_creator import MainCreatorAgent, MainCreatorInput
from app.config import settings
from app.kria import planner
from app.kria.brief import CreativeBrief
from app.kria.clip_scope import apply_clip_exclusions, apply_subject_scope, excluded_clips
from app.pipeline.unified_montage import UnifiedClip, plan_unified_montage
from app.schemas.clip_intents import ClipAssignment, ClipIntent, ResolvedClipIntent
from app.services.clip_intent_planning import PlannedIntentResolution
from app.services.clip_intent_resolution import IntentClip, IntentResolution
from app.services.creator_render_contract import build_render_contract
from app.tasks import content_plan_build

_FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
CASE: dict[str, Any] = json.loads((_FIXTURES / "kri1026_plaza_m1_exclusions.json").read_text())
# A recorded phone-pilot Main Creator input (KRI-623); only its media are swapped for Plaza's.
_BASE_INPUT = json.loads((_FIXTURES / "kri623_tigre_m2_montage_turn.json").read_text())[
    "main_creator_input"
]
CLIPS: list[dict[str, Any]] = CASE["clips"]
ALL = [clip["media_id"] for clip in CLIPS]  # attachment order
BUS = "analysis-proxy-ios-0E376590-356E-4229-97EB-59D03ADBA72D.mp4"  # 11.6 s
J1 = "analysis-proxy-ios-2B3DABCB-AEE9-4BF1-87C9-DF2CE6D1A521.mp4"  # 0.97 s square shot
CATHEDRAL = "analysis-proxy-ios-A61E17CA-C7A7-4363-B7A6-6BF129E93A95.mp4"  # 23.4 s
J2 = "analysis-proxy-ios-9ECFFEBC-3E41-4F34-B52B-C83A1E4D7FFE.mp4"  # 0.2 s, no audio
CASA_ROSADA = "analysis-proxy-ios-AB2850CF-EE2D-40F8-A1D6-6874E13740BC.mp4"  # 14.0 s
PALMS = "analysis-proxy-ios-4869C2B5-F7C3-4304-9BA2-D713372FBD21.mp4"  # 14.3 s
RECOLETA = "analysis-proxy-ios-71B14ED8-DDAF-42F5-9803-9285030AD2DD.mp4"  # 14.5 s
JUNK = {J1, J2}
REAL = [media_id for media_id in ALL if media_id not in JUNK]
FILMING_ORDER = (PALMS, CASA_ROSADA, BUS, RECOLETA, CATHEDRAL)  # capture times, cathedral last
ITEM_ID = "08b8b60a-085c-407e-84c0-849c59d7fd79"
ROOT = "users/creator/creation-threads/thread/"


@pytest.fixture(autouse=True)
def _flags(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "clip_intents_enabled", True)
    monkeypatch.setattr(settings, "clip_intent_exclude_enabled", True)
    monkeypatch.setattr(settings, "clip_facts_enabled", True)
    monkeypatch.setattr(settings, "kria_clip_selection_questions_enabled", True)


def _manifest_data(clips: list[dict[str, Any]] = CLIPS) -> dict[str, Any]:
    data = copy.deepcopy(_BASE_INPUT["capability_manifest"])
    data["item_id"] = ITEM_ID
    data["media"] = [
        {"kind": c["kind"], "label": None, "media_id": c["media_id"], "duration_s": c["duration_s"]}
        for c in clips
    ]
    return data


def _manifest() -> ResolvedCreatorManifest:
    return ResolvedCreatorManifest.model_validate(_manifest_data())


def _resolved(strategy: dict[str, Any]) -> list[ResolvedClipIntent]:
    return [ResolvedClipIntent.model_validate(row) for row in strategy["resolved_clip_intents"]]


def _played(strategy: dict[str, Any], media_ids: list[str] = ALL) -> list[str]:
    """The clips a draft hands the phone: the REAL proposal-less dispatch selection."""
    item = SimpleNamespace(
        id=uuid.uuid4(),
        clip_assignments=[{"media_id": m, "gcs_path": ROOT + m} for m in media_ids],
    )
    paths = content_plan_build._creator_selected_clip_paths(
        item, [ROOT + m for m in media_ids], strategy, without_proposal=True
    )
    return [path.removeprefix(ROOT) for path in paths]


def _snapshot() -> dict[str, Any]:
    return {
        "clip_assignments": [
            {"media_id": c["media_id"], "kind": c["kind"], "capture": c["capture"]} for c in CLIPS
        ]
    }


def _contract(strategy: dict[str, Any]) -> Any:
    """The render contract the approval pins, with the brief it was approved with."""
    brief = CreativeBrief.model_validate(CASE["brief_v3"])
    return build_render_contract(
        strategy, generation_id="gen", brief=brief, media_snapshot=_snapshot()
    )


def _exclude(*media_ids: str, evidence: str = "creator selected") -> ResolvedClipIntent:
    return ResolvedClipIntent(
        intent_id="exclude_short",
        op="exclude",
        attribute="anything too short to use",
        status="resolved",
        assignments=[
            ClipAssignment(media_id=m, evidence=evidence, confidence=1.0) for m in media_ids
        ],
    )


def _filming_order() -> ResolvedClipIntent:
    return next(i for i in _resolved(CASE["strategy_t5"]) if i.intent_id == "order_filmed")


# -- the recorded shapes: what main did with them (1, 2, 3) -------------------------------


def test_the_recorded_render_played_both_clips_the_creator_tapped_out() -> None:
    """The evidence this file starts from, so the fixture can never drift into a pass."""
    t5 = CASE["strategy_t5"]
    exclude = next(r for r in t5["resolved_clip_intents"] if r["op"] == "exclude")
    assert {a["media_id"] for a in exclude["assignments"]} == JUNK
    assert {a["evidence"] for a in exclude["assignments"]} == {"creator selected"}
    assert t5["selected_media_ids"] == []
    rendered = [cut["media_id"] for cut in CASE["main_render"]["timeline"]]
    assert JUNK <= set(rendered)


def test_the_contract_replays_the_recorded_render_order() -> None:
    # The recorded strategy as approved (every clip) pins exactly the order prod rendered,
    # so the contract checks below test the same builder prod ran.
    assert list(_contract(CASE["strategy_t5"]).order_ids) == CASE["main_render"]["order_ids"]


@pytest.mark.parametrize("turn", ["strategy_t3", "strategy_t5"])
def test_a_filming_order_never_keeps_a_clip_the_creator_tapped_out(turn: str) -> None:
    # 1: "in the order we filmed it" assigns every clip; it names none of them.
    strategy = CreativeStrategy.model_validate(CASE[turn])
    assert excluded_clips(_resolved(CASE[turn]), strategy) == JUNK


def test_the_title_only_redraft_leaves_both_junk_clips_out() -> None:
    # 2: the incident pick (empty, guided "take everything") becomes the five real clips.
    strategy = CreativeStrategy.model_validate(CASE["strategy_t5"])
    narrowed = apply_clip_exclusions(
        _manifest(), strategy, resolved_intents=_resolved(CASE["strategy_t5"])
    )
    assert narrowed.media_scope == "selected"
    assert narrowed.selected_media_ids == REAL
    assert _contract(narrowed.model_dump(mode="json")).order_ids == FILMING_ORDER


def test_a_tap_beats_a_resolver_guess_but_not_another_tap() -> None:
    # 4: the resolver matched "Catedral" to the square shot: a guess; the tap wins.
    strategy = CreativeStrategy.model_validate({"edit_format": "montage"})
    guess = ResolvedClipIntent(
        intent_id="label_catedral",
        op="label",
        attribute="the columns",
        creator_text="Catedral",
        status="resolved",
        assignments=[ClipAssignment(media_id=J1, value="Catedral", confidence=0.9)],
    )
    assert excluded_clips([_exclude(J1, J2), guess], strategy) == JUNK
    # 5: the creator also tapped the square shot IN for another ask: a real contradiction.
    tapped_in = ResolvedClipIntent(
        intent_id="include_square",
        op="include",
        attribute="the square",
        status="resolved",
        assignments=[ClipAssignment(media_id=J1, evidence="creator selected", confidence=1.0)],
    )
    assert excluded_clips([_exclude(J1, J2), tapped_in], strategy) == {J2}
    # 5: a resolver-matched skip still loses to a resolver-matched pin (KRI-667, unchanged).
    pinned = ResolvedClipIntent(
        intent_id="end_square",
        op="order",
        position="last",
        attribute="the square",
        status="resolved",
        assignments=[ClipAssignment(media_id=J1, confidence=0.9)],
    )
    guessed_skip = _exclude(J1, evidence="tiled ground")
    assert excluded_clips([guessed_skip, pinned], strategy) == set()
    # 5: media the strategy plays stays, whoever asked.
    placed = CreativeStrategy.model_validate(
        {
            "edit_format": "montage",
            "montage_audio": {"source_media_ids": [J1], "preserve_source_audio": True},
        }
    )
    assert excluded_clips([_exclude(J1, J2), _filming_order()], placed) == {J2}


def test_a_subject_scope_is_not_cancelled_by_a_filming_order() -> None:
    # The same defect in the KRI-993 sibling: "only about the palms, in filming order"
    # kept every clip, because the filming order named them all.
    only_palms = ResolvedClipIntent(
        intent_id="only_palms",
        op="include",
        exclusive=True,
        attribute="the palm trees",
        status="resolved",
        assignments=[ClipAssignment(media_id=PALMS, confidence=0.9)],
    )
    strategy = CreativeStrategy.model_validate({"edit_format": "montage", "media_scope": "all"})
    scope = apply_subject_scope(
        _manifest(), strategy, resolved_intents=[only_palms, _filming_order()]
    )
    assert scope.strategy.selected_media_ids == [PALMS]


def test_an_exclusion_that_would_leave_no_footage_is_still_reported() -> None:
    # 6: every own clip excluded: the pick is kept (never emptied) and the kept clips listed.
    from app.kria.clip_exclusions import still_played_exclusions

    everything = _exclude(*ALL)
    strategy = CreativeStrategy.model_validate({"edit_format": "montage", "media_scope": "all"})
    assert apply_clip_exclusions(_manifest(), strategy, resolved_intents=[everything]) is strategy
    assert still_played_exclusions([everything], strategy, ALL) == ALL
    # 7: an applied exclusion lists nothing.
    narrowed = apply_clip_exclusions(_manifest(), strategy, resolved_intents=[_exclude(J1, J2)])
    assert still_played_exclusions([_exclude(J1, J2)], narrowed, ALL) == []


# -- the turn, end to end through the runtime-v2 planner, dispatch and the montage (3, 6) --


def _agent_input(message: str, clips: list[dict[str, Any]] = CLIPS) -> MainCreatorInput:
    data = copy.deepcopy(_BASE_INPUT)
    data["capability_manifest"] = _manifest_data(clips)
    data["user_message"] = message
    data["conversation"] = [
        data["conversation"][0],
        {"role": "user", "content": CASE["prompt"]},
        *([{"role": "user", "content": message}] if message != CASE["prompt"] else []),
    ]
    return MainCreatorInput.model_validate(data)


def _model_strategy(**update: Any) -> dict[str, Any]:
    """The strategy the Main Creator emitted on the title turn (the server's fields removed)."""
    strategy = {k: v for k, v in CASE["strategy_t5"].items() if k != "resolved_clip_intents"}
    return {**strategy, **update}


def _intent_clips(clips: list[dict[str, Any]] = CLIPS) -> list[IntentClip]:
    return [
        IntentClip(
            media_id=c["media_id"],
            kind=c["kind"],
            analysis={"understanding": c["analysis"]["understanding"]},
            gcs_path=ROOT + c["media_id"],
            capture_time=datetime.fromisoformat(c["capture"]["capture_time"]),
        )
        for c in clips
    ]


async def _turn(
    monkeypatch: pytest.MonkeyPatch,
    *,
    message: str,
    strategy: dict[str, Any],
    clips: list[dict[str, Any]] = CLIPS,
    resolved: list[ResolvedClipIntent] | None = None,
    intent_clips: list[IntentClip] | None = None,
) -> Any:
    """One planning turn: the REAL Main Creator parse and runtime-v2 planner.

    ``resolved`` stands in for the clip-intent resolution (a recorded answer); without it
    the real planner and resolver agents run against the scripted provider (``_provider``).
    """
    agent_input = _agent_input(message, clips)
    raw = {
        "action": {
            "kind": "propose_strategy",
            "strategy": strategy,
            "summary": "Here's your Plaza de Mayo montage.",
        },
        "brief_updates": [],
    }
    output = MainCreatorAgent(model_client=None).parse(json.dumps(raw), agent_input)
    if resolved is not None:
        requested = [
            ClipIntent.model_validate(i.model_dump(include=set(ClipIntent.model_fields)))
            for i in resolved
        ]
        monkeypatch.setattr(
            planner,
            "plan_and_resolve_clip_intents",
            AsyncMock(
                return_value=PlannedIntentResolution(requested, IntentResolution(intents=resolved))
            ),
        )
    monkeypatch.setattr(planner, "_load_thread_events", AsyncMock(return_value=[]))
    monkeypatch.setattr(planner, "_kick_clip_understanding", AsyncMock())
    monkeypatch.setattr(planner, "_earlier_exclusions", AsyncMock(return_value=[]))
    monkeypatch.setattr(type(planner.settings), "brief_binding_for", lambda _s, _i: True)
    return await planner._plan_from_creator_output(
        SimpleNamespace(),
        thread_id=uuid.uuid4(),
        item_id=uuid.UUID(ITEM_ID),
        creator_id=uuid.uuid4(),
        user_message=message,
        manifest=agent_input.capability_manifest,
        inputs=planner._CreatorInputs(
            agent_input=agent_input,
            intent_clips=intent_clips if intent_clips is not None else _intent_clips(clips),
            creator_request=None,
            raw_creator_request=CASE["prompt"],
        ),
        output=output,
        brief_selects_clips=planner._brief_selects_clips(None),
        clip_scope_texts=planner._brief_clip_scope_texts(None),
    )


def _draft(planned: Any) -> tuple[dict[str, Any], str]:
    """(the strategy the draft applies, the draft's reply)."""
    assert planned.plan.mode == "act", planned.plan.response
    apply_intent = planned.plan.intents[0]
    assert apply_intent.tool_name == "draft.apply_strategy"
    return apply_intent.arguments["strategy"], apply_intent.arguments["summary"]


def _unified_clips(media_ids: list[str]) -> list[UnifiedClip]:
    by_id = {c["media_id"]: c for c in CLIPS}
    return [
        UnifiedClip(
            media_id=media_id,
            proxy_path=ROOT + media_id,
            generation="1",
            duration_s=by_id[media_id]["duration_s"],
            width=1080,
            height=1920,
            analysis={"best_moments": by_id[media_id]["analysis"]["best_moments"]},
        )
        for media_id in media_ids
    ]


async def test_incident_the_title_redraft_never_plays_the_tapped_out_clips(
    prod_profile, monkeypatch
) -> None:
    planned = await _turn(
        monkeypatch,
        message=CASE["title_follow_up"],
        strategy=_model_strategy(),
        resolved=_resolved(CASE["strategy_t5"]),
    )
    strategy, reply = _draft(planned)

    assert strategy["media_scope"] == "selected"
    assert set(strategy["selected_media_ids"]) == set(REAL)
    # 3: dispatch hands the phone only the five real clips ...
    dispatched = _played(strategy)
    assert set(dispatched) == set(REAL)
    # ... the approval pins the true filming order without them ...
    assert _contract(strategy).order_ids == FILMING_ORDER
    # ... and the montage timeline never cuts to one.
    plan = plan_unified_montage(
        _unified_clips(dispatched), None, strategy=strategy, clip_intents_enabled=True
    )
    assert not JUNK & {cut.media_id for cut in plan.snapshot.fast_cuts}
    # 7: the exclusion was applied, so nothing extra is said about it.
    assert "couldn't leave out" not in reply


async def test_a_tapped_clip_the_strategy_plays_stays_and_the_draft_says_so(
    prod_profile, monkeypatch
) -> None:
    # 6: the Main Creator made the square shot the montage's sound source: it stays (dropping
    # it would break that), J2 still leaves, and the draft names the clip that stays.
    planned = await _turn(
        monkeypatch,
        message=CASE["title_follow_up"],
        strategy=_model_strategy(
            montage_audio={"source_media_ids": [J1], "preserve_source_audio": True}
        ),
        resolved=_resolved(CASE["strategy_t5"]),
    )
    strategy, reply = _draft(planned)
    assert set(_played(strategy)) == {*REAL, J1}
    assert "I couldn't leave out clip 2, so it is still in the video." in reply


async def test_flag_off_leaves_the_pick_and_the_reply_alone(prod_profile, monkeypatch) -> None:
    # 8
    monkeypatch.setattr(settings, "clip_intent_exclude_enabled", False)
    planned = await _turn(
        monkeypatch,
        message=CASE["title_follow_up"],
        strategy=_model_strategy(),
        resolved=_resolved(CASE["strategy_t5"]),
    )
    strategy, reply = _draft(planned)
    assert _played(strategy) == ALL
    assert "leave out" not in reply and "left out" not in reply
