"""KRI-623: a montage never plays clips the creator ruled out.

Incident (prod thread 35ccc8ef, 2026-10-12, cabin kit M2 re-send): two long handheld
takes (70 s indoors, 67 s outdoors) and two short clips (5 s, 7 s). "Pick the best six
moments from the long takes, each very short, and skip the dark ones." The Main Creator
(v56) took every clip (``selected_media_ids: []`` under the guided program), nothing on
the server read "from the long takes", and the moment selector gives every selected
source at least one cut. The first 6 s of the phone render were the two short clips.

Each case runs the recorded turn through the REAL Main Creator parse and the REAL
runtime-v2 planner (``_plan_from_creator_output``), then the drafted strategy through the
REAL length plan, dispatch selection, unified montage planner, phone compiler and contract
verifier, under the production flag profile. Only the model text and the clip-intent
resolver (its recorded answer) are stand-ins.

Ways the fix could go wrong, written down before the code (each has a case):

1. "from the long takes" is not read, so the short clips stay (the incident).
2. A description ("Two long handheld takes, plus a couple of dark clips") or a style ask
   ("fast cuts, short clips") is read as a limit, and clips the creator wanted are dropped.
3. "each very short" (the moments) is read as "the short clips".
4. A removal ("skip the short clips", "kısa klipleri kullanma") is read as keeping them,
   or a negated removal ("don't skip the short clips") as dropping them.
5. Clips of similar length (no clear long/short split) are split anyway.
6. Photos or Visuals are dropped for not being "long takes".
7. The model's empty pick ("take everything") is not narrowed, so nothing changes.
8. The narrowed pick never reaches the phone: dispatch still hands over every clip, or the
   moment windows still come from the short clips.
9. An edit that asks none of this changes at all.
10. "use the short clips too" in a later turn is ignored and they stay dropped; an ask
    only the Creative Brief still remembers is lost in a later turn.
11. A guided Voiceover contract, valid only with the all-media scope, is rewritten into
    a list and refused (found while writing the fix).
"""

from __future__ import annotations

import copy
import json
import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from app.agents._schemas.creator_agent import CreativeStrategy, ResolvedCreatorManifest
from app.agents.main_creator import MainCreatorAgent, MainCreatorInput
from app.config import settings
from app.kria import planner
from app.kria.brief import BriefRequirement, CreativeBrief
from app.kria.brief_binding import BriefBinding
from app.kria.clip_scope import apply_clip_scope, ruled_out_clips
from app.pipeline.unified_montage import UnifiedClip, brief_view, plan_unified_montage
from app.schemas.clip_intents import ClipIntent, ResolvedClipIntent
from app.services.clip_intent_planning import PlannedIntentResolution
from app.services.clip_intent_resolution import IntentClip, IntentResolution
from app.services.creator_render_contract import build_render_contract
from app.tasks import content_plan_build
from tests.tasks.test_selected_clip_dispatch import _verify

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "kri623_tigre_m2_montage_turn.json"
CASE: dict[str, Any] = json.loads(FIXTURE.read_text())
MANIFEST = CASE["main_creator_input"]["capability_manifest"]
DURATIONS = {m["media_id"]: m["duration_s"] for m in MANIFEST["media"]}
SHORT_A = "analysis-proxy-ios-4DD4C6F8-EE8C-429D-B286-D4356E6F63E1.mp4"  # 5 s, dim bedroom
SHORT_B = "analysis-proxy-ios-91A2CC1A-66BA-4671-889B-5307A669374F.mp4"  # 7 s, dark hallway
OUTDOOR = "analysis-proxy-ios-044463CA-9E6C-41D4-A58D-5F46C00CE340.mp4"  # 67 s, the line
INDOOR = "analysis-proxy-ios-28ECDA66-FB56-4E85-9312-3E09A969AE77.mp4"  # 70 s
LONG = {INDOOR, OUTDOOR}
PROD_MESSAGE = CASE["main_creator_input"]["user_message"]
BRIEF = CreativeBrief.model_validate(CASE["brief"])
ROOT = "users/creator/creation-threads/thread/"


@pytest.fixture(autouse=True)
def _clip_intents_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "clip_intents_enabled", True)


def _agent_input(messages: list[str], manifest: dict[str, Any]) -> MainCreatorInput:
    data = copy.deepcopy(CASE["main_creator_input"])
    data["capability_manifest"] = manifest
    data["user_message"] = messages[-1]
    conversation = [data["conversation"][0]]
    for index, message in enumerate(messages):
        if index:
            conversation.append({"role": "assistant", "content": "Sure."})
        conversation.append({"role": "user", "content": message})
    data["conversation"] = conversation
    return MainCreatorInput.model_validate(data)


def _recorded_resolution() -> PlannedIntentResolution:
    resolved = [ResolvedClipIntent.model_validate(row) for row in CASE["resolved_clip_intents"]]
    requested = [ClipIntent.model_validate(row) for row in CASE["model_strategy"]["clip_intents"]]
    return PlannedIntentResolution(requested, IntentResolution(intents=resolved))


async def _draft(
    monkeypatch: pytest.MonkeyPatch,
    *,
    messages: list[str] | None = None,
    brief: CreativeBrief | None = BRIEF,
    manifest: dict[str, Any] | None = None,
    **strategy_update: Any,
) -> dict[str, Any]:
    """One recorded planning turn; returns the strategy the draft would apply."""

    messages = messages or [PROD_MESSAGE]
    manifest = manifest or MANIFEST
    agent_input = _agent_input(messages, manifest)
    strategy = {**copy.deepcopy(CASE["model_strategy"]), **strategy_update}
    raw = {
        "action": {
            "kind": "propose_strategy",
            "strategy": strategy,
            "summary": CASE["model_summary"],
            "render_intent_evidence": {"opening_title": 'Title: "Our cabin in Tigre"'},
        },
        "brief_updates": [],
    }
    output = MainCreatorAgent(model_client=None).parse(json.dumps(raw), agent_input)
    monkeypatch.setattr(
        planner, "plan_and_resolve_clip_intents", AsyncMock(return_value=_recorded_resolution())
    )
    monkeypatch.setattr(planner, "_load_thread_events", AsyncMock(return_value=[]))
    monkeypatch.setattr(planner, "_kick_clip_understanding", AsyncMock())
    monkeypatch.setattr(type(planner.settings), "brief_binding_for", lambda _s, _i: True)
    clips = [
        IntentClip(media_id=m["media_id"], kind=m["kind"], analysis=None) for m in manifest["media"]
    ]
    planned = await planner._plan_from_creator_output(
        SimpleNamespace(),
        thread_id=uuid.uuid4(),
        item_id=uuid.UUID(manifest["item_id"]),
        creator_id=uuid.uuid4(),
        user_message=messages[-1],
        manifest=agent_input.capability_manifest,
        inputs=planner._CreatorInputs(
            agent_input=agent_input,
            intent_clips=clips,
            creator_request=None,
            raw_creator_request="\n".join(messages),
        ),
        output=output,
        brief_selects_clips=planner._brief_selects_clips(brief),
        clip_scope_texts=planner._brief_clip_scope_texts(brief),
    )
    assert planned.plan.mode == "act", planned.plan.response
    apply_intent = planned.plan.intents[0]
    assert apply_intent.tool_name == "draft.apply_strategy"
    return apply_intent.arguments["strategy"]


def _played(strategy: dict[str, Any]) -> set[str]:
    """What the strategy plays: an empty guided pick, or an "all" scope, is every clip."""

    selected = strategy.get("selected_media_ids") or []
    if strategy.get("media_scope") == "all" or not selected:
        return set(DURATIONS)
    return set(selected)


def _dispatched(strategy: dict[str, Any]) -> list[str]:
    """The REAL proposal-less dispatch selection over the item's clips (attachment order)."""

    item = SimpleNamespace(
        id=uuid.uuid4(),
        clip_assignments=[{"media_id": m, "gcs_path": ROOT + m} for m in DURATIONS],
    )
    paths = content_plan_build._creator_selected_clip_paths(
        item, [ROOT + m for m in DURATIONS], strategy, without_proposal=True
    )
    return [path.removeprefix(ROOT) for path in paths]


def _snapshot() -> dict[str, Any]:
    return {
        "clip_assignments": [
            {
                "media_id": media_id,
                "kind": "video",
                "gcs_path": ROOT + media_id,
                "storage_generation": "1",
                "duration_s": row["duration_s"],
                "analysis": {"best_moments": row["best_moments"]},
            }
            for media_id, row in CASE["clip_analysis"].items()
        ]
    }


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


# -- the incident, end to end ----------------------------------------------------------


async def test_incident_m2_plays_six_moments_from_the_long_takes_only(
    prod_profile, monkeypatch
) -> None:
    # 7: the model took every clip, as in prod.
    assert CASE["model_strategy"]["selected_media_ids"] == []
    strategy = await _draft(monkeypatch)

    # 1: "from the long takes" narrows the pick to the two long takes.
    assert strategy["media_scope"] == "selected"
    assert set(strategy["selected_media_ids"]) == LONG

    # 8: dispatch hands the phone only the long takes, in attachment order.
    dispatched = _dispatched(strategy)
    assert dispatched == [m for m in DURATIONS if m in LONG]

    # 8: the frozen length plan cuts its six moments from the long takes only.
    snapshot = planner._enrich_best_parts_snapshot(
        strategy, _snapshot(), BRIEF, closing_quote=CASE["closing_quote"]
    )
    length_plan = snapshot["length_plan"]
    assert not length_plan["conflicts"]
    windows = length_plan["windows"]
    assert len(windows) == 6
    assert {w["media_id"] for w in windows} == LONG
    assert sum(w["source_end_s"] - w["source_start_s"] for w in windows) == pytest.approx(
        18.0, abs=0.05
    )

    # The phone montage plans, compiles and verifies with only those clips.
    resolved = BriefBinding.create(
        uuid.uuid5(uuid.NAMESPACE_URL, "kri-623"),
        BRIEF,
        latest_message=PROD_MESSAGE,
        media_snapshot=snapshot,
    ).resolve()
    clips = _unified_clips(dispatched)
    plan = plan_unified_montage(
        clips,
        brief_view(resolved),
        strategy=strategy,
        clip_intents_enabled=True,
        creator_duration_proven=True,
        approved_length_plan=length_plan,
    )
    cuts = plan.snapshot.fast_cuts
    assert len(cuts) == 6
    assert {c.media_id for c in cuts} == LONG
    # KRI-597 still holds: the closing cut is the outdoor take's line.
    assert cuts[-1].media_id == OUTDOOR
    assert cuts[-1].source_start_s <= 53.9 and cuts[-1].source_end_s >= 55.8
    contract = build_render_contract(
        strategy, generation_id="gen", brief=resolved, media_snapshot=snapshot
    )
    assert _verify(contract, plan, clips)


async def test_the_models_own_pick_alone_would_have_played_the_short_clips(
    prod_profile, monkeypatch
) -> None:
    # The incident's shape without the creator's limit: every clip plays (also 9).
    strategy = await _draft(
        monkeypatch,
        messages=["Make a montage of our cabin, 18 seconds."],
        brief=None,
        opening_title=None,
    )
    assert _played(strategy) == set(DURATIONS)
    assert _dispatched(strategy) == list(DURATIONS)


async def test_a_model_that_picked_the_short_clips_too_is_narrowed(
    prod_profile, monkeypatch
) -> None:
    # 7: an explicit "selected" pick of all four (the approved proposal's shape) narrows too.
    strategy = await _draft(monkeypatch, media_scope="selected", selected_media_ids=list(DURATIONS))
    assert strategy["media_scope"] == "selected"
    assert set(strategy["selected_media_ids"]) == LONG


async def test_a_later_turn_keeps_the_brief_ask(prod_profile, monkeypatch) -> None:
    # 10: the ask scrolled out of the chat window; the live brief still carries it.
    strategy = await _draft(monkeypatch, messages=[PROD_MESSAGE, "Make the title bigger please."])
    assert set(strategy["selected_media_ids"]) == LONG


async def test_use_the_short_clips_too_brings_them_back(prod_profile, monkeypatch) -> None:
    # 10: the creator widened the pick in a later turn.
    widened = CreativeBrief(
        version=2,
        requirements=[
            *BRIEF.requirements,
            BriefRequirement(
                id="r6", kind="select", scope="global", description="Use the short clips too"
            ),
        ],
    )
    strategy = await _draft(
        monkeypatch, messages=[PROD_MESSAGE, "Use the short clips too."], brief=widened
    )
    assert _played(strategy) == set(DURATIONS)


async def test_visuals_stay_in_the_pick(prod_profile, monkeypatch) -> None:
    # 6: "the long takes" limits the creator's own clips; a Visual is not a take, even a
    # short Visuals video.
    photo = "asset-0b6c1c1e-6c55-4c47-9d2f-1b2f0b8f8a11"
    video = "asset-5e0d7a52-3d1b-4f0e-8a3c-7c1d2b9e4f60"
    manifest = copy.deepcopy(MANIFEST)
    manifest["media"] += [
        {"kind": "image", "label": None, "media_id": photo},
        {"kind": "video", "label": None, "media_id": video, "duration_s": 4.0},
    ]
    strategy = await _draft(monkeypatch, manifest=manifest)
    assert set(strategy["selected_media_ids"]) == LONG | {photo, video}


def test_a_guided_voiceover_contract_keeps_its_all_media_scope() -> None:
    # 11: the guided voiceover contract keeps its scope; a plain montage is narrowed.
    manifest = ResolvedCreatorManifest.model_validate(MANIFEST)
    contract = CreativeStrategy.model_validate(
        {
            "edit_format": "narrated",
            "media_scope": "all",
            "execution_contract": "guided_voiceover_v1",
        }
    )
    assert apply_clip_scope(manifest, contract, creator_texts=[PROD_MESSAGE]) is contract
    montage = CreativeStrategy.model_validate({"edit_format": "montage", "media_scope": "all"})
    scoped = apply_clip_scope(manifest, montage, creator_texts=[PROD_MESSAGE])
    assert scoped.media_scope == "selected" and set(scoped.selected_media_ids) == LONG


def test_own_photos_are_never_ruled_out() -> None:
    # 6: a photo has no length; it is never a short clip.
    photo = SimpleNamespace(
        media_id="analysis-proxy-ios-PHOTO-1.jpg", kind="image", duration_s=None
    )
    assert ruled_out_clips(["only the long takes"], [*M2, photo]) == SHORT


# -- reading the creator's words ---------------------------------------------------------


def _media(durations: dict[str, float]) -> list[SimpleNamespace]:
    return [
        SimpleNamespace(media_id=media_id, kind="video", duration_s=seconds)
        for media_id, seconds in durations.items()
    ]


M2 = _media(DURATIONS)
SHORT = {SHORT_A, SHORT_B}


@pytest.mark.parametrize(
    ("text", "dropped"),
    [
        # 1: the prod words, from the brief and from the chat message.
        (BRIEF.requirements[0].description, SHORT),
        (PROD_MESSAGE, SHORT),
        ("only the long takes", SHORT),
        ("just the long ones", SHORT),
        ("use the long takes only", SHORT),
        ("pick moments only from the longer clips", SHORT),
        ("Don't use anything except the long takes.", SHORT),
        ("6 quick cuts out of the two long videos", SHORT),
        # 4: removals.
        ("skip the short clips", SHORT),
        ("don't use the short clips", SHORT),
        ("leave the short ones out", SHORT),
        ("everything except the short clips", SHORT),
        ("no short clips please, skip the shorter ones", SHORT),
        ("skip the long takes", LONG),
        ("only the short clips", LONG),
        # 70 s vs 67 s: neither is clearly the longest.
        ("leave out the longest one", set()),
        # Turkish.
        ("Uzun çekimlerden en iyi altı anı seç, her biri çok kısa olsun", SHORT),
        ("sadece uzun videoları kullan", SHORT),
        ("kısa klipleri kullanma", SHORT),
        ("kısa videolar hariç", SHORT),
        ("uzun çekimleri atla", LONG),
        # 2 + 3: descriptions, cut lengths and moments name no subset.
        ("Two long handheld walkthrough takes of our cabin, plus a couple of dark clips.", set()),
        ("fast cuts, short clips, upbeat", set()),
        ("only short clips, fast pace", set()),
        ("pick six moments, each very short", set()),
        ("keep it short, 18 seconds", set()),
        ("short moments from my clips", set()),
        ("use the long takes and the short clips", set()),
        ("use the short clips too", set()),
        ("make the long takes shorter", set()),
        ("iki uzun çekim ve iki kısa klip var", set()),
        # 4: a negated removal keeps them.
        ("don't skip the short clips", set()),
        ("kısa klipleri atlama", set()),
        # Something we cannot measure is left to the model.
        ("skip the dark ones", set()),
        # "all the long takes" is still the long takes; "use all the clips" alone is no limit.
        ("pick moments only from all the long takes", SHORT),
        ("use all the clips", set()),
        # Asking to drop everything is not a pick.
        ("skip the short clips and skip the long takes", set()),
    ],
)
def test_which_clips_the_words_rule_out(text: str, dropped: set[str]) -> None:
    assert ruled_out_clips([text], M2) == dropped


@pytest.mark.parametrize(
    "texts",
    [
        ["Pick the best six moments from the long takes", "Use the short clips too"],
        ["skip the short clips", "actually, add the short clips back"],
        ["sadece uzun çekimler", "kısa klipleri de kullan"],
        ["Pick the best six moments from the long takes", "Actually, use all the clips."],
        ["skip the short clips", "use all 4 videos"],
        ["kısa klipleri kullanma", "tüm klipleri kullan"],
    ],
)
def test_a_later_ask_brings_the_clips_back(texts: list[str]) -> None:
    # 10: asks are read oldest first; a later "too" / "back" widens the pick again.
    assert ruled_out_clips(texts, M2) == set()
    assert ruled_out_clips(texts[:1], M2) == SHORT


@pytest.mark.parametrize(
    "durations",
    [
        # 5: no clear gap between short and long.
        {"a": 20.0, "b": 25.0, "c": 31.0, "d": 40.0},
        # two gaps of the same kind: which side is "long" is a guess.
        {"a": 3.0, "b": 10.0, "c": 40.0},
        # a clip with no known length could be either.
        {"a": 5.0, "b": 60.0, "c": None},
        # one clip has nothing to compare with.
        {"a": 60.0},
    ],
)
def test_no_clear_long_short_split_drops_nothing(durations: dict[str, float | None]) -> None:
    assert ruled_out_clips(["only the long takes"], _media(durations)) == set()
    assert ruled_out_clips(["skip the short clips"], _media(durations)) == set()


def test_a_clear_split_with_more_clips_on_each_side() -> None:
    durations = {"a": 4.0, "b": 6.5, "c": 3.0, "d": 48.0, "e": 95.0, "f": 61.0}
    assert ruled_out_clips(["from the long takes"], _media(durations)) == {"a", "b", "c"}
    assert ruled_out_clips(["skip the longest clip"], _media(durations)) == {"e"}
    assert ruled_out_clips(["drop the shortest one"], _media(durations)) == set()  # 3 vs 4 s
