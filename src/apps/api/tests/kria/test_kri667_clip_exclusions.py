"""KRI-667: "Skip the quad bike clip" is an `exclude` intent, and the clip leaves the edit.

Before, the clip-intent planner had no way to say "leave this clip out": the model either
invented an op (a loud rejection) or wrote `include`, which FORCES the clip in, and the
parser re-read the creator's words with a regex to drop it silently. Now the planner
states `exclude` (any language), the resolver keeps a clip for it only on a confident
match, and `apply_clip_exclusions` removes those clips from the pick.

Ways this could go wrong, written down before the code (each has a case below; the
planner-side ones live in tests/agents/test_clip_intent_planner.py, "KRI-667"):

1. A wrong clip is removed: an unsure resolver match (below 0.8, or one the resolver
   wants a vision check for) drops footage the creator wanted.
2. The last own clip, or every clip, is removed.
3. A clip-picker answer for "include: laundry" is reused for "exclude: laundry", so the
   clips the creator tapped to USE are the ones removed.
4. An exclusion that matches nothing, a resolver question about it, or a resolver outage
   turns into a creator question, a clip picker, or a held turn.
5. The resolver receives an op its schema does not know and the whole resolution fails.
6. A clip another ask names ("end on the hallway clip"), or the strategy itself places
   (a reaction beat, a montage audio source), is removed anyway.
7. An excluded hero clip stays the hero.
8. A guided voiceover contract loses the all-media scope it needs.
9. The narrowed pick never reaches dispatch / the phone montage, or fails its contract.
10. A downstream consumer forces the excluded clip back in: the semantic planner requires
    it ("clip intent source was dropped"), the narrated aligner cues it to its words.
11. Flag off drifts: something is removed, or the planner input changes.
12. A strategy persisted with an `exclude` stops loading once the flag is off.
"""

from __future__ import annotations

import copy
import json
import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from pydantic import ValidationError

from app.agents._runtime import ProviderOutcomeUnknownError, RunContext
from app.agents._schemas.creator_agent import (
    CreativeStrategy,
    ProposeStrategy,
    ResolvedCreatorManifest,
)
from app.agents.clip_intent_planner import ClipIntentPlannerAgent
from app.agents.clip_question import ClipQuestionAgent
from app.agents.clip_request_resolver import (
    ClipRequestResolverAgent,
    ClipRequestResolverOutput,
    ResolverAssignment,
    ResolverIntentOut,
    ResolverVisionQuestion,
)
from app.agents.edit_proposal import EditProposalAgentInput, EditProposalMedia
from app.agents.main_creator import MainCreatorAgent, MainCreatorInput
from app.agents.semantic_edit_proposal import SemanticEditProposalAgent
from app.config import settings
from app.kria import planner
from app.kria.clip_scope import apply_clip_exclusions
from app.pipeline.unified_montage import UnifiedClip, plan_unified_montage
from app.routes import creator_agent as creator_routes
from app.schemas.clip_intents import ClipAssignment, ClipIntent, ResolvedClipIntent
from app.services import clip_intent_planning as planning
from app.services.clip_intent_planning import PlannedIntentResolution
from app.services.clip_intent_resolution import (
    IntentClip,
    IntentResolution,
    resolve_clip_intents_for_turn,
)
from app.services.clip_selection import ClipSelections, SelectionEntry
from app.services.creator_capabilities import resolve_creator_manifest
from app.services.creator_render_contract import build_render_contract
from app.services.semantic_edit_scheduler import schedule_semantic_edit
from app.tasks import content_plan_build
from app.tasks.generative_build import _narrated_alignment_record, _narrated_creator_clip_labels
from tests.tasks.test_selected_clip_dispatch import _verify

# The KRI-623 cabin turn (4 clips: two long takes, a dim bedroom and a dark hallway).
FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "kri623_tigre_m2_montage_turn.json"
CASE: dict[str, Any] = json.loads(FIXTURE.read_text())
MANIFEST = CASE["main_creator_input"]["capability_manifest"]
DURATIONS = {m["media_id"]: m["duration_s"] for m in MANIFEST["media"]}
BEDROOM = "analysis-proxy-ios-4DD4C6F8-EE8C-429D-B286-D4356E6F63E1.mp4"  # 5 s, dim bedroom
HALLWAY = "analysis-proxy-ios-91A2CC1A-66BA-4671-889B-5307A669374F.mp4"  # 7 s, dark hallway
OUTDOOR = "analysis-proxy-ios-044463CA-9E6C-41D4-A58D-5F46C00CE340.mp4"  # 67 s
INDOOR = "analysis-proxy-ios-28ECDA66-FB56-4E85-9312-3E09A969AE77.mp4"  # 70 s
ALL = list(DURATIONS)
ROOT = "users/creator/creation-threads/thread/"
SKIP_HALLWAY = "Make a montage of our cabin, 18 seconds. Skip the dark hallway clip."


@pytest.fixture(autouse=True)
def _flags(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "clip_intents_enabled", True)
    monkeypatch.setattr(settings, "clip_intent_exclude_enabled", True)


def _exclude(media_id: str | None = HALLWAY, *, confidence: float = 0.95) -> ResolvedClipIntent:
    return ResolvedClipIntent(
        intent_id="skip-hallway",
        op="exclude",
        attribute="dark hallway clip",
        status="resolved",
        assignments=(
            [ClipAssignment(media_id=media_id, evidence="dark hallway", confidence=confidence)]
            if media_id
            else []
        ),
    )


def _order_last(media_id: str) -> ResolvedClipIntent:
    return ResolvedClipIntent(
        intent_id="end-hallway",
        op="order",
        position="last",
        attribute="the hallway",
        status="resolved",
        assignments=[ClipAssignment(media_id=media_id, confidence=0.9)],
    )


# -- resolution: confident matches only, never a question (1, 3, 4, 5) ------------------


def _clip(media_id: str, subject: str) -> IntentClip:
    return IntentClip(
        media_id=media_id,
        kind="video",
        analysis={"subject": subject},
        gcs_path=f"users/u/clips/{media_id}.mp4",
    )


CLIPS = [_clip("a", "laundry basket"), _clip("b", "laundry room"), _clip("c", "gym")]
LAUNDRY = ClipIntent(intent_id="skip-laundry", op="exclude", attribute="laundry clip")


def _patch_resolver(monkeypatch, output: ClipRequestResolverOutput | Exception) -> list:
    seen: list = []

    def _run(self, input, *, ctx=None):  # noqa: A002, ANN001
        seen.append(input)
        if isinstance(output, Exception):
            raise output
        return output

    monkeypatch.setattr(ClipRequestResolverAgent, "run", _run)
    return seen


@pytest.fixture
def no_vision(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    calls = [0]

    def _run(self, input, *, ctx=None):  # noqa: A002, ANN001
        calls[0] += 1
        raise AssertionError("an exclusion never pays for a vision check")

    monkeypatch.setattr(ClipQuestionAgent, "run", _run)
    return calls


async def test_an_exclusion_keeps_only_the_resolvers_confident_matches(
    monkeypatch, no_vision
) -> None:
    seen = _patch_resolver(
        monkeypatch,
        ClipRequestResolverOutput(
            intents=[
                ResolverIntentOut(
                    intent_id="skip-laundry",
                    assignments=[
                        ResolverAssignment(media="m001", confidence=0.95, evidence="laundry"),
                        # 1: a membership-bar guess (0.7) would include; it never removes.
                        ResolverAssignment(media="m002", confidence=0.7),
                        ResolverAssignment(media="m003", confidence=0.9),
                    ],
                    # 1: the resolver itself is unsure about m003.
                    needs_vision=[ResolverVisionQuestion(media="m003", question="laundry?")],
                )
            ]
        ),
    )
    resolution = await resolve_clip_intents_for_turn(
        intents=[LAUNDRY],
        creator_request="Skip the laundry clip.",
        clips=CLIPS,
        run_context=RunContext(),
    )
    # 5: the resolver was asked the membership question it knows how to answer.
    assert [(i.intent_id, i.op) for i in seen[0].intents] == [("skip-laundry", "include")]
    assert resolution.status == "resolved"
    assert resolution.question is None and resolution.clip_question is None
    [intent] = resolution.intents
    assert (intent.op, intent.status, intent.media_ids()) == ("exclude", "resolved", ["a"])
    assert no_vision == [0]


@pytest.mark.parametrize(
    "output",
    [
        ClipRequestResolverOutput(
            intents=[ResolverIntentOut(intent_id="skip-laundry", question="Which clip?")]
        ),
        ClipRequestResolverOutput(intents=[ResolverIntentOut(intent_id="skip-laundry")]),
        ClipRequestResolverOutput(intents=[]),
        ProviderOutcomeUnknownError("resolver timed out"),
    ],
    ids=["resolver-question", "no-match", "intent-missing", "resolver-down"],
)
async def test_an_exclusion_alone_never_asks_or_holds_the_turn(
    monkeypatch, no_vision, output
) -> None:
    # 4: nothing matched (or nothing could be checked) -> nothing is left out, no question.
    monkeypatch.setattr(settings, "kria_clip_selection_questions_enabled", True)
    _patch_resolver(monkeypatch, output)
    resolution = await resolve_clip_intents_for_turn(
        intents=[LAUNDRY],
        creator_request="Skip the laundry clip.",
        clips=CLIPS,
        run_context=RunContext(),
    )
    assert resolution.status == "resolved"
    assert not resolution.needs_creator
    assert resolution.question is None and resolution.clip_question is None
    assert [(i.op, i.status, i.assignments) for i in resolution.intents] == [
        ("exclude", "resolved", [])
    ]


async def test_a_resolver_outage_still_holds_the_asks_that_need_it(monkeypatch) -> None:
    # 4, the other side: an include still needs the resolver, so the outage still counts.
    _patch_resolver(monkeypatch, ProviderOutcomeUnknownError("resolver timed out"))
    resolution = await resolve_clip_intents_for_turn(
        intents=[LAUNDRY, ClipIntent(intent_id="gym", op="include", attribute="gym")],
        creator_request="Skip the laundry, but the gym has to be in.",
        clips=CLIPS,
        run_context=RunContext(),
    )
    assert resolution.status == "provider_unavailable"


async def test_an_unmatched_exclusion_beside_a_question_is_never_in_the_picker(
    monkeypatch, no_vision
) -> None:
    # 4: the include's picker question lists the include only, never the exclusion.
    monkeypatch.setattr(settings, "kria_clip_selection_questions_enabled", True)
    _patch_resolver(
        monkeypatch,
        ClipRequestResolverOutput(
            intents=[
                ResolverIntentOut(intent_id="skip-laundry"),
                ResolverIntentOut(intent_id="gym"),
            ]
        ),
    )
    resolution = await resolve_clip_intents_for_turn(
        intents=[LAUNDRY, ClipIntent(intent_id="gym", op="include", attribute="gym")],
        creator_request="Skip the laundry, but the gym has to be in.",
        clips=CLIPS,
        run_context=RunContext(),
    )
    assert resolution.status == "needs_creator"
    assert [c["op"] for c in resolution.clip_question["categories"]] == ["include"]
    assert "laundry" not in resolution.question
    by_op = {i.op: i.status for i in resolution.intents}
    assert by_op == {"exclude": "resolved", "include": "needs_creator"}


def _planner_returns(monkeypatch, raw: dict) -> list:
    """The REAL planner parse over this model output; only the model call is faked."""

    inputs: list = []

    def _run(self, input, *, ctx=None):  # noqa: A002, ANN001
        inputs.append(input)
        return self.parse(json.dumps(raw), input)

    monkeypatch.setattr(planning, "default_client", MagicMock())
    monkeypatch.setattr(ClipIntentPlannerAgent, "run", _run)
    return inputs


_SKIP_LAUNDRY_RAW = {
    "intents": [
        {
            "intent_id": "skip-laundry",
            "op": "exclude",
            "attribute": "laundry",
            "source_quote": "Skip the laundry",
        }
    ],
    "question": None,
}


@pytest.mark.parametrize(
    "answer",
    [SelectionEntry(media_ids=("a",)), SelectionEntry(none=True)],
    ids=["tapped-a", "said-none"],
)
async def test_a_tapped_include_answer_is_never_reused_for_an_exclusion(
    monkeypatch, no_vision, answer: SelectionEntry
) -> None:
    # 3: last turn the creator answered the picker for "include: laundry" (tapped clip
    # "a", or "there are none"). This turn's skip must go to the resolver on its own:
    # never settle onto the tapped (wanted) clip, never vanish with the "none" answer.
    _planner_returns(monkeypatch, _SKIP_LAUNDRY_RAW)
    seen = _patch_resolver(
        monkeypatch,
        ClipRequestResolverOutput(
            intents=[
                ResolverIntentOut(
                    intent_id="skip-laundry",
                    assignments=[ResolverAssignment(media="m002", confidence=0.9)],
                )
            ]
        ),
    )
    selections = ClipSelections({"include:laundry": answer})
    result = await planning.plan_and_resolve_clip_intents(
        creator_request="Skip the laundry.",
        latest_user_message="Skip the laundry.",
        candidate_intents=None,
        clips=CLIPS,
        run_context=RunContext(),
        clip_selections=selections,
    )
    assert len(seen) == 1
    [intent] = result.resolution.intents
    assert (intent.op, intent.media_ids()) == ("exclude", ["b"])


@pytest.mark.parametrize("enabled", [True, False])
async def test_the_kill_switch_reaches_the_planner_input(monkeypatch, enabled: bool) -> None:
    # 11: off, the planner input is the pre-KRI-667 one and the skip is dropped silently.
    monkeypatch.setattr(settings, "clip_intent_exclude_enabled", enabled)
    inputs = _planner_returns(monkeypatch, _SKIP_LAUNDRY_RAW)
    seen = _patch_resolver(monkeypatch, ClipRequestResolverOutput(intents=[]))
    result = await planning.plan_and_resolve_clip_intents(
        creator_request="Skip the laundry.",
        latest_user_message="Skip the laundry.",
        candidate_intents=None,
        clips=CLIPS,
        run_context=RunContext(),
    )
    assert ("clip_exclusions" in inputs[0].model_dump()) is enabled
    assert [i.op for i in result.requested_intents] == (["exclude"] if enabled else [])
    assert len(seen) == (1 if enabled else 0)


# -- the pick: apply_clip_exclusions on real strategy shapes (2, 6, 7, 8) ----------------


def _manifest(media: list[dict[str, Any]] | None = None) -> ResolvedCreatorManifest:
    data = copy.deepcopy(MANIFEST)
    if media is not None:
        data["media"] = media
    return ResolvedCreatorManifest.model_validate(data)


def _montage(**update: Any) -> CreativeStrategy:
    return CreativeStrategy.model_validate({"edit_format": "montage", **update})


def test_an_all_scope_becomes_every_clip_but_the_excluded_one() -> None:
    narrowed = apply_clip_exclusions(
        _manifest(), _montage(media_scope="all"), resolved_intents=[_exclude()]
    )
    assert narrowed.media_scope == "selected"
    assert narrowed.selected_media_ids == [m for m in ALL if m != HALLWAY]


def test_an_explicit_pick_loses_only_the_excluded_clip() -> None:
    strategy = _montage(media_scope="selected", selected_media_ids=[OUTDOOR, HALLWAY, INDOOR])
    narrowed = apply_clip_exclusions(_manifest(), strategy, resolved_intents=[_exclude()])
    assert narrowed.selected_media_ids == [OUTDOOR, INDOOR]


def test_a_clip_the_pick_never_had_changes_nothing() -> None:
    strategy = _montage(media_scope="selected", selected_media_ids=[OUTDOOR, INDOOR])
    assert apply_clip_exclusions(_manifest(), strategy, resolved_intents=[_exclude()]) is strategy


@pytest.mark.parametrize(
    "intents",
    [
        [_exclude(), _order_last(HALLWAY)],
        [
            _exclude(),
            ResolvedClipIntent(
                intent_id="gym",
                op="include",
                attribute="hallway",
                status="resolved",
                assignments=[ClipAssignment(media_id=HALLWAY, confidence=0.7)],
            ),
        ],
        # An exclusion the resolver could not settle removes nothing.
        [_exclude().model_copy(update={"status": "needs_creator"})],
        [_exclude(None)],
    ],
    ids=["ordered-last", "included", "unresolved", "no-match"],
)
def test_a_clip_another_ask_names_is_never_removed(intents: list[ResolvedClipIntent]) -> None:
    # 6: two asks about one clip contradict; keeping the footage is the safe side.
    strategy = _montage(media_scope="all")
    assert apply_clip_exclusions(_manifest(), strategy, resolved_intents=intents) is strategy


def test_media_the_strategy_places_is_never_removed() -> None:
    # 6: a montage audio source plays the hallway's sound; dropping it would break that.
    strategy = _montage(
        media_scope="all",
        montage_audio={"source_media_ids": [HALLWAY], "preserve_source_audio": True},
    )
    assert apply_clip_exclusions(_manifest(), strategy, resolved_intents=[_exclude()]) is strategy


def test_the_last_own_clip_is_never_removed() -> None:
    # 2: every own clip excluded -> the pick is left alone, not emptied.
    everything = ResolvedClipIntent(
        intent_id="skip-all",
        op="exclude",
        attribute="clips",
        status="resolved",
        assignments=[ClipAssignment(media_id=m, confidence=1.0) for m in ALL],
    )
    strategy = _montage(media_scope="all")
    assert apply_clip_exclusions(_manifest(), strategy, resolved_intents=[everything]) is strategy
    # A photo left over is not an own clip to make a video from.
    photo = {"kind": "image", "label": None, "media_id": "asset-0b6c1c1e-photo"}
    media = [*MANIFEST["media"], photo]
    assert (
        apply_clip_exclusions(_manifest(media), strategy, resolved_intents=[everything]) is strategy
    )


def test_an_excluded_hero_is_no_longer_the_hero() -> None:
    # 7
    strategy = _montage(media_scope="all", hero_media_id=HALLWAY)
    narrowed = apply_clip_exclusions(_manifest(), strategy, resolved_intents=[_exclude()])
    assert narrowed.hero_media_id is None
    assert HALLWAY not in narrowed.selected_media_ids


def test_a_guided_voiceover_contract_keeps_its_all_media_scope() -> None:
    # 8
    contract = CreativeStrategy.model_validate(
        {
            "edit_format": "narrated",
            "media_scope": "all",
            "execution_contract": "guided_voiceover_v1",
        }
    )
    assert apply_clip_exclusions(_manifest(), contract, resolved_intents=[_exclude()]) is contract


# -- the turn, end to end through the runtime-v2 planner (9, 6, 11) ---------------------


def _agent_input(message: str) -> MainCreatorInput:
    data = copy.deepcopy(CASE["main_creator_input"])
    data["user_message"] = message
    data["conversation"] = [data["conversation"][0], {"role": "user", "content": message}]
    return MainCreatorInput.model_validate(data)


async def _draft(
    monkeypatch: pytest.MonkeyPatch,
    resolved: list[ResolvedClipIntent],
    *,
    message: str = SKIP_HALLWAY,
    **strategy_update: Any,
) -> dict[str, Any]:
    """One recorded planning turn; returns the strategy the draft would apply."""
    planned = await _planned_turn(monkeypatch, resolved, message=message, **strategy_update)
    apply_intent = planned.plan.intents[0]
    assert apply_intent.tool_name == "draft.apply_strategy"
    return apply_intent.arguments["strategy"]


async def _planned_turn(
    monkeypatch: pytest.MonkeyPatch,
    resolved: list[ResolvedClipIntent],
    *,
    message: str = SKIP_HALLWAY,
    dropped_exclusions: int = 0,
    **strategy_update: Any,
) -> Any:
    """One recorded planning turn (a draft); returns the planned turn.

    The REAL Main Creator parse and runtime-v2 planner; only the model text and the
    clip-intent resolution (its recorded answer) are stand-ins.
    """
    agent_input = _agent_input(message)
    strategy = {**copy.deepcopy(CASE["model_strategy"]), "opening_title": None, **strategy_update}
    raw = {
        "action": {
            "kind": "propose_strategy",
            "strategy": strategy,
            "summary": CASE["model_summary"],
        },
        "brief_updates": [],
    }
    output = MainCreatorAgent(model_client=None).parse(json.dumps(raw), agent_input)
    requested = [
        ClipIntent.model_validate(i.model_dump(include=set(ClipIntent.model_fields)))
        for i in resolved
    ]
    monkeypatch.setattr(
        planner,
        "plan_and_resolve_clip_intents",
        AsyncMock(
            return_value=PlannedIntentResolution(
                requested,
                IntentResolution(intents=resolved),
                dropped_exclusions=dropped_exclusions,
            )
        ),
    )
    monkeypatch.setattr(planner, "_load_thread_events", AsyncMock(return_value=[]))
    monkeypatch.setattr(planner, "_kick_clip_understanding", AsyncMock())
    monkeypatch.setattr(type(planner.settings), "brief_binding_for", lambda _s, _i: True)
    clips = [
        IntentClip(media_id=m["media_id"], kind=m["kind"], analysis=None) for m in MANIFEST["media"]
    ]
    planned = await planner._plan_from_creator_output(
        SimpleNamespace(),
        thread_id=uuid.uuid4(),
        item_id=uuid.UUID(MANIFEST["item_id"]),
        creator_id=uuid.uuid4(),
        user_message=message,
        manifest=agent_input.capability_manifest,
        inputs=planner._CreatorInputs(
            agent_input=agent_input,
            intent_clips=clips,
            creator_request=None,
            raw_creator_request=message,
        ),
        output=output,
        brief_selects_clips=planner._brief_selects_clips(None),
        clip_scope_texts=planner._brief_clip_scope_texts(None),
    )
    assert planned.plan.mode == "act", planned.plan.response
    return planned


def _played(strategy: dict[str, Any]) -> set[str]:
    selected = strategy.get("selected_media_ids") or []
    if strategy.get("media_scope") == "all" or not selected:
        return set(ALL)
    return set(selected)


def _dispatched(strategy: dict[str, Any]) -> list[str]:
    """The REAL proposal-less dispatch selection over the item's clips (attachment order)."""
    item = SimpleNamespace(
        id=uuid.uuid4(),
        clip_assignments=[{"media_id": m, "gcs_path": ROOT + m} for m in ALL],
    )
    paths = content_plan_build._creator_selected_clip_paths(
        item, [ROOT + m for m in ALL], strategy, without_proposal=True
    )
    return [path.removeprefix(ROOT) for path in paths]


def _unified_clips(media_ids: list[str]) -> list[UnifiedClip]:
    return [
        UnifiedClip(
            media_id=media_id,
            proxy_path=ROOT + media_id,
            generation="1",
            duration_s=DURATIONS[media_id],
            width=1080,
            height=1920,
            analysis={"best_moments": CASE["clip_analysis"][media_id]["best_moments"]},
        )
        for media_id in media_ids
    ]


async def test_skip_the_hallway_clip_never_plays_it(prod_profile, monkeypatch) -> None:
    monkeypatch.setattr(settings, "clip_intent_exclude_enabled", True)
    # The model took every clip (the guided program's empty pick).
    assert CASE["model_strategy"]["selected_media_ids"] == []
    strategy = await _draft(monkeypatch, [_exclude()])

    assert strategy["media_scope"] == "selected"
    assert strategy["selected_media_ids"] == [m for m in ALL if m != HALLWAY]
    # 9: dispatch hands the phone every clip but the hallway, in attachment order.
    dispatched = _dispatched(strategy)
    assert dispatched == [m for m in ALL if m != HALLWAY]
    # 9: the phone montage plans, compiles and verifies with only those clips.
    clips = _unified_clips(dispatched)
    plan = plan_unified_montage(clips, None, strategy=strategy, clip_intents_enabled=True)
    assert HALLWAY not in {cut.media_id for cut in plan.cuts}
    contract = build_render_contract(strategy, generation_id="gen")
    assert _verify(contract, plan, clips)
    # The exclusion stays on the draft as the creator's resolved ask.
    assert [i["op"] for i in strategy["resolved_clip_intents"]] == ["exclude"]


async def test_a_clip_the_creator_also_placed_still_plays(prod_profile, monkeypatch) -> None:
    # 6: "end on the hallway" and "skip the hallway" contradict: the clip stays. With the
    # picker on, KRI-761 also asks which clip to leave out (test_kri761_look_alike_exclusions).
    monkeypatch.setattr(settings, "clip_intent_exclude_enabled", True)
    monkeypatch.setattr(settings, "kria_clip_selection_questions_enabled", False)
    strategy = await _draft(monkeypatch, [_exclude(), _order_last(HALLWAY)])
    assert _played(strategy) == set(ALL)


async def test_an_unsure_or_empty_exclusion_plays_everything(prod_profile, monkeypatch) -> None:
    # With the picker on, KRI-807 asks which clip to leave out instead of drafting
    # (test_kri807_unmatched_exclusion_picker); either way nothing is removed on a guess.
    monkeypatch.setattr(settings, "clip_intent_exclude_enabled", True)
    monkeypatch.setattr(settings, "kria_clip_selection_questions_enabled", False)
    strategy = await _draft(monkeypatch, [_exclude(None)])
    assert _played(strategy) == set(ALL)
    assert _dispatched(strategy) == ALL


async def test_flag_off_removes_nothing(prod_profile, monkeypatch) -> None:
    # 11: a resolved exclusion (e.g. persisted before a rollback) is never applied.
    monkeypatch.setattr(settings, "clip_intent_exclude_enabled", False)
    strategy = await _draft(monkeypatch, [_exclude()])
    assert _played(strategy) == set(ALL)
    assert _dispatched(strategy) == ALL


# -- KRI-680: a skip that leaves nothing out is said, never silent ----------------------
#
# Tigre night M1: "don't use the two very dark clips" was dropped by the planner on every
# turn and both clips rendered; the reply never said so. Ways this could go wrong:
#
# a. The planner drops the ask (`exclusion_dropped`) and the reply says nothing.
# b. The resolver matches no clip, the ask never asks (by design), and the reply says
#    nothing.
# c. A skip that DID remove its clips gets the notice anyway (a false alarm).
# d. Flag off: the reply changes.
# e. The notice is repeated per unmatched ask.

_UNAPPLIED_EN = "I couldn't tell which clips you asked me to leave out"


def _notices(planned: Any) -> list[str]:
    return [n for n in planned.policy_notices if _UNAPPLIED_EN in n]


async def test_kri680_a_skip_the_planner_dropped_is_said_before_the_render(
    prod_profile, monkeypatch
) -> None:
    planned = await _planned_turn(monkeypatch, [], dropped_exclusions=1)
    assert len(_notices(planned)) == 1
    summary = planned.plan.intents[0].arguments["summary"]
    assert _UNAPPLIED_EN in summary
    assert _played(planned.plan.intents[0].arguments["strategy"]) == set(ALL)


async def test_kri680_a_skip_no_clip_matched_is_said_before_the_render(
    prod_profile, monkeypatch
) -> None:
    # The picker off: KRI-807 asks with the clip picker when it is on.
    monkeypatch.setattr(settings, "kria_clip_selection_questions_enabled", False)
    planned = await _planned_turn(monkeypatch, [_exclude(None)], dropped_exclusions=1)
    assert len(_notices(planned)) == 1


async def test_kri680_a_skip_that_removed_its_clip_says_nothing_extra(
    prod_profile, monkeypatch
) -> None:
    planned = await _planned_turn(monkeypatch, [_exclude()])
    assert _notices(planned) == []
    assert HALLWAY not in _played(planned.plan.intents[0].arguments["strategy"])


async def test_kri680_flag_off_reply_is_unchanged(prod_profile, monkeypatch) -> None:
    monkeypatch.setattr(settings, "clip_intent_exclude_enabled", False)
    planned = await _planned_turn(monkeypatch, [_exclude(None)], dropped_exclusions=1)
    assert _notices(planned) == []


# -- the v1 creator route (9, 11) --------------------------------------------------------


def _route_manifest():
    return resolve_creator_manifest(
        item_id="item-week",
        edit_format="montage",
        media=[
            {"media_id": "clip-1", "kind": "video"},
            {"media_id": "clip-2", "kind": "video"},
            {"media_id": "clip-3", "kind": "video"},
        ],
        guided_capability_enabled=True,
    )


@pytest.mark.parametrize("enabled", [True, False])
async def test_the_creator_route_drafts_the_pick_without_the_excluded_clip(
    monkeypatch, enabled: bool
) -> None:
    monkeypatch.setattr(creator_routes.settings, "clip_intent_exclude_enabled", enabled)
    item = SimpleNamespace(id=uuid.uuid4())
    session = SimpleNamespace(
        id=uuid.uuid4(),
        revision=1,
        status="planning",
        events=[],
        agent_call_count=0,
        agent_call_budget=2,
        question_count=0,
        question_budget=2,
        active_plan=None,
        last_error=None,
        manifest_hash=None,
    )
    response = SimpleNamespace(status="awaiting_confirmation")
    action = ProposeStrategy(
        kind="propose_strategy",
        strategy=CreativeStrategy(
            direction="fast_montage",
            edit_format="montage",
            render_program="native",
            media_scope="selected",
            selected_media_ids=["clip-1", "clip-2", "clip-3"],
            target_duration_s=20,
            rationale="A week montage without the laundry.",
        ),
        summary="A week montage.",
    )
    monkeypatch.setattr(
        creator_routes,
        "_owned_context",
        AsyncMock(return_value=(item, SimpleNamespace(), SimpleNamespace(user_id=uuid.uuid4()))),
    )
    monkeypatch.setattr(creator_routes, "_load_session", AsyncMock(return_value=session))
    monkeypatch.setattr(
        creator_routes,
        "resolve_item_creator_context",
        AsyncMock(return_value=(_route_manifest(), [])),
    )
    monkeypatch.setattr(creator_routes, "creator_context", lambda *_args: ("creator", "item"))
    monkeypatch.setattr(creator_routes, "default_client", lambda: SimpleNamespace())
    monkeypatch.setattr(
        creator_routes.asyncio, "to_thread", AsyncMock(return_value=SimpleNamespace(action=action))
    )
    monkeypatch.setattr(creator_routes, "append_event", AsyncMock())
    monkeypatch.setattr(creator_routes, "_response", AsyncMock(return_value=response))
    skip = ResolvedClipIntent(
        intent_id="skip-laundry",
        op="exclude",
        attribute="laundry clip",
        status="resolved",
        assignments=[ClipAssignment(media_id="clip-2", confidence=0.9)],
    )
    monkeypatch.setattr(
        creator_routes,
        "load_intent_clips_for_item",
        AsyncMock(
            return_value=[
                IntentClip(media_id=f"clip-{n}", kind="video", analysis={}) for n in (1, 2, 3)
            ]
        ),
    )
    monkeypatch.setattr(
        creator_routes,
        "plan_and_resolve_clip_intents",
        AsyncMock(
            return_value=PlannedIntentResolution(
                [ClipIntent(intent_id="skip-laundry", op="exclude", attribute="laundry clip")],
                IntentResolution(intents=[skip], vision_answers={}),
            )
        ),
    )

    result = await creator_routes._run_planning_turn(
        AsyncMock(),
        item_id=str(item.id),
        user=SimpleNamespace(id=uuid.uuid4()),
        session_id=session.id,
        expected_revision=1,
        user_message="Make a montage of my week. Skip the laundry clip.",
    )

    assert result is response
    strategy = session.active_plan["edit_plan"]["strategy"]
    expected = ["clip-1", "clip-3"] if enabled else ["clip-1", "clip-2", "clip-3"]
    assert strategy["selected_media_ids"] == expected


# -- downstream: nothing forces an excluded clip back in (10, 12) ------------------------


def _semantic_input(**updates: Any) -> EditProposalAgentInput:
    values: dict[str, Any] = {
        "direction": "guided_story",
        "pace": "balanced",
        "target_duration_s": 8,
        "media_scope": "selected",
        "selected_media_ids": ["park"],
        "creator_request": "A day in the park. Skip the pub clip.",
        "media": [
            EditProposalMedia(
                media_id="park",
                lane="clip",
                kind="video",
                duration_s=30.0,
                summary="walk in the park",
                best_moments=[{"start_s": 1.0, "end_s": 9.0}],
            ),
            EditProposalMedia(
                media_id="pub",
                lane="clip",
                kind="video",
                duration_s=30.0,
                setting="pub",
                best_moments=[{"start_s": 2.0, "end_s": 9.0}],
            ),
        ],
    }
    values.update(updates)
    return EditProposalAgentInput(**values)


_PARK_ONLY = json.dumps(
    {
        "title": "Park day",
        "chapters": [
            {
                "chapter_id": "one",
                "topic": "Park",
                "thought": "",
                "role": "hook",
                "weight": 1,
                "layout": "fullscreen",
                "sources": [{"media_id": "m001", "candidate_index": 0, "weight": 1}],
            }
        ],
    }
)


def _pub(op: str) -> ResolvedClipIntent:
    return ResolvedClipIntent(
        intent_id="pub",
        op=op,
        attribute="pub clip",
        status="resolved",
        assignments=[ClipAssignment(media_id="pub", confidence=0.9)],
    )


def test_the_semantic_planner_never_requires_an_excluded_clip() -> None:
    # 10: an exclude names the pub clip, which the plan rightly leaves out.
    agent = SemanticEditProposalAgent(None)  # type: ignore[arg-type]
    excluded = _semantic_input(clip_intents=[_pub("exclude")])
    assert '"exclude"' not in agent.render_prompt(excluded)
    plan = agent.parse(_PARK_ONLY, excluded)
    schedule = schedule_semantic_edit(plan, excluded)
    assert {media_id for beat in schedule.story_beats for media_id in beat.media_ids} == {"park"}
    # The same plan against an INCLUDE of the pub clip is refused: the guard is real.
    with pytest.raises(Exception, match="dropped"):
        agent.parse(_PARK_ONLY, _semantic_input(clip_intents=[_pub("include")]))


def _narrated_strategy(op: str) -> dict[str, Any]:
    return {
        "resolved_clip_intents": [
            _pub(op).model_dump(mode="json"),
            ResolvedClipIntent(
                intent_id="park",
                op="include",
                attribute="the park walk",
                status="resolved",
                assignments=[ClipAssignment(media_id="park", confidence=0.9)],
            ).model_dump(mode="json"),
        ]
    }


def test_the_narrated_aligner_never_cues_an_excluded_clip_to_its_words() -> None:
    # 10: an exclude's words ("pub clip") are not that clip's label for the voiceover.
    labels, _first, _last, _groups = _narrated_creator_clip_labels(
        {"creator_strategy": _narrated_strategy("exclude")},
        {"c1": "x/park", "c2": "x/pub"},
        lambda _clip_id: None,
    )
    assert labels == {"c1": "the park walk"}
    record = _narrated_alignment_record(
        generation="g",
        brief_version=None,
        ordering_basis="voiceover",
        steps=[
            SimpleNamespace(step_id="s1", media_id="pub", start_s=0.0, end_s=2.0),
            SimpleNamespace(step_id="s2", media_id="park", start_s=2.0, end_s=4.0),
        ],
        proxy_path_by_media_id={"pub": "x/pub", "park": "x/park"},
        strategy=_narrated_strategy("exclude"),
        words=[],
        caption_language=None,
        spoken_language=None,
        caption_language_request=None,
    )
    rows = {row["media_id"]: row["labels"] for row in record["steps"]}
    assert rows == {"pub": [], "park": ["the park walk"]}


def test_a_persisted_exclusion_loads_whatever_the_flag(monkeypatch) -> None:
    # 12: a strategy drafted with the flag on must still load after a rollback.
    monkeypatch.setattr(settings, "clip_intent_exclude_enabled", False)
    stored = _montage(resolved_clip_intents=[_exclude()]).model_dump(mode="json")
    reloaded = CreativeStrategy.model_validate(stored)
    assert reloaded.resolved_clip_intents[0].op == "exclude"
    # Nothing is printed or placed for a clip left out.
    for field in ({"creator_text": "Laundry"}, {"position": "last"}, {"caption_attribute": "x"}):
        with pytest.raises(ValidationError):
            ClipIntent(intent_id="x", op="exclude", attribute="laundry", **field)


def test_an_exclude_only_turn_opens_no_text_burn_pass_on_a_cloud_render():
    # The cloud render burns a text-free base whenever resolved clip intents exist; an
    # exclude already narrowed the pick and draws nothing, so it must not count.
    from app.tasks.generative_build import _render_clip_intents

    exclude = {"intent_id": "skip-quad", "op": "exclude", "status": "resolved"}
    label = {"intent_id": "rooms", "op": "label", "status": "resolved"}
    assert _render_clip_intents([exclude]) is None
    assert _render_clip_intents([exclude, label]) == [label]
    untouched = [label]
    assert _render_clip_intents(untouched) is untouched
    assert _render_clip_intents(None) is None
