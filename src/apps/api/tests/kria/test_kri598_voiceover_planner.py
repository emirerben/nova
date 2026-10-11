"""KRI-598: a runtime-v2 Voiceover edit never plans a contract it cannot run.

Prod thread f77a5c16 (2026-10-11, "Tigre'de bir gün"): an iPhone Voiceover edit with
seven own clips, a bathtub photo in Visuals and a 22 s Turkish voiceover. On the third
turn the creator wrote "7 klibin hepsini kullan" (use all 7 clips). The server's scope
reader turned that into `media_scope: "all"`, the model boundary refused an all-media
voiceover without `guided_voiceover_v1`, the retry feedback named that contract, and
the model (seeing `guided_voiceover` advertised on the manifest) returned it with no
reaction beats. Runtime v2 has no guided-proposal step, so the contract was
downgraded, the bathtub photo's beat was gone, and the creator got a "simpler
version?" question instead of a plan.

Every case resolves its manifest through the REAL capability resolver from the
recorded prod media and runs the REAL Main Creator parse and the REAL runtime-v2
planner (`_plan_from_creator_output`), under the production flag profile. Only the
model text and the clip-intent resolver's answer are stand-ins.

Ways this could go wrong, written down before the code (each has a case):

1. The runtime-v2 manifest still advertises `guided_voiceover`, so the model can pick
   a contract v2 can never run.
2. The gate leaks into v1 (`routes/creator_agent.py` runs that contract) or into a
   v2 manifest with no voiceover (its hash, and every in-flight confirm fence, moves).
3. A planner call site resolves the manifest without the gate (the brief-extraction,
   scoped and live turns must all see the same manifest).
4. "Use all my clips" / "hepsini kullan" now dead-ends: the scope reader forces
   "all", the boundary refuses every retry, and the turn fails.
5. "All" resolves to the clips the model happened to pick (the cue clips) instead of
   every own clip, or pulls a Visuals photo into the native clip list.
6. The photo's reaction beat is lost when "all" resolves.
7. "All" silently drops a Visual that no spoken word places, or more own clips than
   one native edit takes.
8. A contract the model returns anyway passes silently, or fails with feedback that
   does not say what to do instead.
9. Exact photo/video timing (`mixed_media_timing`) on a v2 voiceover edit is quietly
   accepted although nothing can honour it, or (read from the creator's words, so the
   model cannot drop it) fails the parse on every retry instead of asking.
10. A cue intent becomes a forced sequence again: the render-side reader must see the
    cue clips' labels but no ordered group, and dispatch must keep every clip.
"""

from __future__ import annotations

import ast
import copy
import json
import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.agents._runtime import SchemaError
from app.agents._schemas.creator_policy import (
    CAPABILITY_GUIDED_VOICEOVER,
    GUIDED_VOICEOVER_NATIVE_ONLY_CODE,
)
from app.agents.main_creator import MainCreatorAgent, MainCreatorInput
from app.kria import planner
from app.kria.voiceover_scope import VOICEOVER_VISUALS_LEFT_OUT_NOTICE
from app.schemas.clip_intents import ClipIntent, ResolvedClipIntent
from app.services import creator_capabilities as capabilities
from app.services import creator_sessions
from app.services.clip_intent_planning import PlannedIntentResolution
from app.services.clip_intent_resolution import IntentClip, IntentResolution
from app.tasks import content_plan_build, generative_build

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "kri598_tigre_voiceover_turns.json"
CASE: dict[str, Any] = json.loads(FIXTURE.read_text())
OWN = [m["media_id"] for m in CASE["media"] if m["kind"] == "video"]
PHOTO = "asset-fe1c1382-89e1-440e-b54d-f3a4d945f4ff"
RIVER = "analysis-proxy-ios-ECCB47EC-0C2C-4F64-917F-85FB8039C9D7.mp4"
KITCHEN = "analysis-proxy-ios-80DCD337-53E9-426D-8DC8-31AEC9359F76.mp4"
LAPTOP = "analysis-proxy-ios-CFBEC1AE-2523-49F2-B675-04576EAED824.mp4"
BATHROOM = "analysis-proxy-ios-A9859821-B374-42D6-9F07-F9C4734506C7.mp4"
TURN3 = CASE["turns"][2]
TITLE = "Tigre’de bir gün"


@pytest.fixture(autouse=True)
def _prod_rollout(monkeypatch: pytest.MonkeyPatch) -> None:
    # A number, not a boolean flag, so the prod profile leaves it at the default (0).
    monkeypatch.setattr(capabilities.settings, "main_creator_agent_rollout_percent", 100)


def _manifest(*, runs_guided_voiceover: bool = False, media: list[dict] | None = None):
    media = media if media is not None else CASE["media"]
    return capabilities.resolve_creator_manifest(
        item_id=CASE["item_id"],
        edit_format=CASE["edit_format"],
        has_voiceover=True,
        narration=CASE["narration"],
        media=media,
        phone_source_media_ids=[m["media_id"] for m in media if m["kind"] == "video"],
        phone_rendering_allowed=True,
        runs_guided_voiceover=runs_guided_voiceover,
    )


def _agent_input(turn: dict[str, Any], manifest) -> MainCreatorInput:
    return MainCreatorInput.model_validate(
        {
            **copy.deepcopy(CASE["shared_input"]),
            "user_message": turn["user_message"],
            "creator_request": turn["creator_request"],
            "conversation": turn["conversation"],
            "capability_manifest": manifest.model_dump(mode="json"),
        }
    )


def _strategy(**update: Any) -> dict[str, Any]:
    """The shape the KRI-598 prompt asks for on turn 3: no contract, the cues as
    `include`, the river as the one placement, the photo as a beat."""

    strategy: dict[str, Any] = {
        "direction": "native",
        "edit_format": "narrated_planned",
        "audio_strategy": "voiceover",
        "render_program": "native",
        "media_scope": "all",
        "selected_media_ids": [],
        "caption_style": "editorial",
        "opening_title": TITLE,
        "target_duration_s": 23,
        "clip_intents": [
            {"intent_id": "start-river", "op": "order", "position": "first", "attribute": "nehir"},
            {"intent_id": "cue-deniz", "op": "include", "attribute": "Deniz laptop başında"},
            {"intent_id": "cue-mutfak", "op": "include", "attribute": "mutfak"},
        ],
        "reaction_beats": [
            {
                "beat_id": "kirkayak",
                "trigger": "kırkayak",
                "visual_id": PHOTO,
                "visual_role": "photo",
            }
        ],
        "rationale": "Seslendirme 22 saniye; video onun uzunluğunda.",
    }
    strategy.update(update)
    return strategy


def _raw(strategy: dict[str, Any]) -> str:
    return json.dumps(
        {
            "action": {
                "kind": "propose_strategy",
                "strategy": strategy,
                "summary": "Yedi klibin hepsi seslendirmeni takip edecek.",
                "render_intent_evidence": {"opening_title": "Başlık: “Tigre’de bir gün”"},
            },
            "brief_updates": [],
        },
        ensure_ascii=False,
    )


def _resolution() -> PlannedIntentResolution:
    """The resolver's answer for the KRI-598 inventory (river first, two cue clips)."""

    rows = [
        {"intent_id": "start-river", "op": "order", "position": "first", "attribute": "nehir"},
        {"intent_id": "cue-deniz", "op": "include", "attribute": "Deniz laptop başında"},
        {"intent_id": "cue-mutfak", "op": "include", "attribute": "mutfak"},
    ]
    media = {"start-river": RIVER, "cue-deniz": LAPTOP, "cue-mutfak": KITCHEN}
    resolved = [
        ResolvedClipIntent.model_validate(
            {**row, "assignments": [{"media_id": media[row["intent_id"]], "confidence": 1.0}]}
        )
        for row in rows
    ]
    return PlannedIntentResolution(
        [ClipIntent.model_validate(row) for row in rows], IntentResolution(intents=resolved)
    )


async def _plan(monkeypatch: pytest.MonkeyPatch, raw: str, *, manifest=None, turn=None):
    manifest = manifest if manifest is not None else _manifest()
    turn = turn or TURN3
    agent_input = _agent_input(turn, manifest)
    output = MainCreatorAgent(model_client=None).parse(raw, agent_input)
    monkeypatch.setattr(
        planner, "plan_and_resolve_clip_intents", AsyncMock(return_value=_resolution())
    )
    monkeypatch.setattr(planner, "_load_thread_events", AsyncMock(return_value=[]))
    monkeypatch.setattr(planner, "_kick_clip_understanding", AsyncMock())
    monkeypatch.setattr(type(planner.settings), "brief_binding_for", lambda _s, _i: True)
    clips = [IntentClip(media_id=m.media_id, kind=m.kind, analysis=None) for m in manifest.media]
    return await planner._plan_from_creator_output(
        SimpleNamespace(),
        thread_id=uuid.uuid4(),
        item_id=uuid.UUID(CASE["item_id"]),
        creator_id=uuid.uuid4(),
        user_message=turn["user_message"],
        manifest=agent_input.capability_manifest,
        inputs=planner._CreatorInputs(
            agent_input=agent_input,
            intent_clips=clips,
            creator_request=None,
            raw_creator_request="\n".join(
                row["content"] for row in turn["conversation"] if row["role"] == "user"
            ),
        ),
        output=output,
        brief_request=turn["creator_request"],
    )


def _drafted(planned) -> dict[str, Any]:
    assert planned.plan.mode == "act", planned.plan.response
    apply_intent = planned.plan.intents[0]
    assert apply_intent.tool_name == "draft.apply_strategy"
    return apply_intent.arguments


# 1 + 2 -----------------------------------------------------------------------


def test_runtime_v2_voiceover_manifest_closes_only_the_guided_contract(prod_profile) -> None:
    v1 = _manifest(runs_guided_voiceover=True)
    v2 = _manifest(runs_guided_voiceover=False)

    # The fixture reproduces the manifest the prod model saw (catalog aside).
    assert {name: cap.model_dump(mode="json") for name, cap in v1.capabilities.items()} == CASE[
        "recorded_capabilities"
    ]
    assert v1.capabilities[CAPABILITY_GUIDED_VOICEOVER].available is True

    closed = v2.capabilities[CAPABILITY_GUIDED_VOICEOVER]
    assert closed.available is False
    assert closed.reason_code == GUIDED_VOICEOVER_NATIVE_ONLY_CODE
    # The reason is what the model reads: it says what to do instead.
    assert "execution_contract" in closed.reason
    assert "reaction_beats" in closed.reason
    assert {k: v for k, v in v2.capabilities.items() if k != CAPABILITY_GUIDED_VOICEOVER} == {
        k: v for k, v in v1.capabilities.items() if k != CAPABILITY_GUIDED_VOICEOVER
    }


def test_runtime_v2_manifest_without_a_voiceover_is_byte_identical(prod_profile) -> None:
    media = [m for m in CASE["media"] if m["kind"] == "video"]
    kwargs = dict(
        item_id=CASE["item_id"],
        edit_format="montage",
        media=media,
        phone_source_media_ids=[m["media_id"] for m in media],
        phone_rendering_allowed=True,
    )
    v1 = capabilities.resolve_creator_manifest(**kwargs)
    v2 = capabilities.resolve_creator_manifest(**kwargs, runs_guided_voiceover=False)

    assert v2.manifest_hash == v1.manifest_hash
    assert v2.context_hash == v1.context_hash


async def test_item_context_passes_the_gate_through(prod_profile) -> None:
    item = SimpleNamespace(
        id=uuid.UUID(CASE["item_id"]),
        edit_format="narrated_planned",
        audio_mode="voiceover",
        voiceover_gcs_path=CASE["narration"]["gcs_path"],
        voiceover_generation=CASE["narration"]["generation"],
        voiceover_duration_s=CASE["narration"]["duration_s"],
        current_job_id=None,
        clip_gcs_paths=["users/u/river.mp4"],
        clip_assignments=[],
    )
    persona = SimpleNamespace(user_id=uuid.uuid4())

    async def _resolve(**kwargs):
        empty = MagicMock()
        empty.scalars.return_value = []
        db = AsyncMock()
        db.execute.side_effect = [empty, empty, empty]
        manifest, _ = await creator_sessions.resolve_item_creator_context(
            db, item, persona=persona, **kwargs
        )
        return manifest.capabilities[CAPABILITY_GUIDED_VOICEOVER]

    assert (await _resolve()).available is True  # v1 route default: unchanged
    closed = await _resolve(runs_guided_voiceover=False)
    assert closed.reason_code == GUIDED_VOICEOVER_NATIVE_ONLY_CODE


# 3 ---------------------------------------------------------------------------


def test_every_runtime_v2_planner_manifest_is_resolved_without_the_guided_contract() -> None:
    """Every manifest the v2 planner resolves (live, scoped, brief extraction) is gated,
    so one turn's planned manifest hash never depends on which path planned it."""

    tree = ast.parse(Path(planner.__file__).read_text())
    calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "resolve_item_creator_context"
    ]
    assert len(calls) >= 3
    for call in calls:
        gate = [kw for kw in call.keywords if kw.arg == "runs_guided_voiceover"]
        assert gate and isinstance(gate[0].value, ast.Constant), ast.dump(call)
        assert gate[0].value.value is False, ast.dump(call)


# 4 + 5 + 6 + 10 ----------------------------------------------------------------


@pytest.mark.parametrize(
    "model_scope",
    [
        # The model says "all" itself.
        {"media_scope": "all", "selected_media_ids": []},
        # The prod shape: only the cue clips, with "7 klibin hepsini kullan" read as "all"
        # by the server's scope reader at the boundary.
        {"media_scope": "selected", "selected_media_ids": [RIVER, LAPTOP, KITCHEN, BATHROOM]},
    ],
)
async def test_use_all_seven_clips_plans_every_own_clip_with_the_photo_beat(
    prod_profile, monkeypatch, model_scope
) -> None:
    drafted = _drafted(await _plan(monkeypatch, _raw(_strategy(**model_scope))))
    strategy = drafted["strategy"]

    assert strategy.get("execution_contract") is None
    assert strategy["render_program"] == "native"
    assert strategy["media_scope"] == "selected"
    assert sorted(strategy["selected_media_ids"]) == sorted(OWN)
    assert PHOTO not in strategy["selected_media_ids"]
    assert [(b["trigger"], b["visual_id"]) for b in strategy["reaction_beats"]] == [
        ("kırkayak", PHOTO)
    ]
    # The photo IS placed (on "kırkayak"), so nothing is said about Visuals left out.
    assert VOICEOVER_VISUALS_LEFT_OUT_NOTICE not in drafted["summary"]
    assert not [i for i in strategy["clip_intents"] if i["op"] == "order" and not i["position"]]

    # Dispatch hands the phone every clip, river first.
    root = "users/creator/creation-threads/thread/"
    item = SimpleNamespace(
        id=uuid.uuid4(),
        clip_assignments=[{"media_id": media_id, "gcs_path": root + media_id} for media_id in OWN],
    )
    paths = content_plan_build._creator_selected_clip_paths(
        item, [root + media_id for media_id in OWN], strategy
    )
    paths, _placed = content_plan_build._creator_intent_clip_order(item, paths, strategy)
    dispatched = [path.removeprefix(root) for path in paths]
    assert sorted(dispatched) == sorted(OWN)
    assert dispatched[0] == RIVER

    # The render-side reader: cue clips carry the creator's words as labels for the
    # narration alignment, but no described sequence (the voiceover decides).
    clip_id_to_gcs = {f"clip-{index}": root + media_id for index, media_id in enumerate(OWN)}
    labels, first, last, groups = generative_build._narrated_creator_clip_labels(
        {"creator_strategy": strategy}, clip_id_to_gcs, lambda _clip_id: None
    )
    by_media = {
        clip_id_to_gcs[clip_id].removeprefix(root): label for clip_id, label in labels.items()
    }
    assert by_media[LAPTOP] == "Deniz laptop başında"
    assert by_media[KITCHEN] == "mutfak"
    assert [clip_id_to_gcs[c].removeprefix(root) for c in first] == [RIVER]
    assert last == [] and groups == []


# 7 ---------------------------------------------------------------------------


async def test_all_scope_says_which_visuals_no_word_places(prod_profile, monkeypatch) -> None:
    drafted = _drafted(await _plan(monkeypatch, _raw(_strategy(reaction_beats=None))))

    assert sorted(drafted["strategy"]["selected_media_ids"]) == sorted(OWN)
    assert VOICEOVER_VISUALS_LEFT_OUT_NOTICE in drafted["summary"]


async def test_all_scope_over_the_native_cap_says_clips_were_left_out(
    prod_profile, monkeypatch
) -> None:
    extra = [
        {"media_id": f"analysis-proxy-ios-EXTRA-{index}.mp4", "kind": "video", "duration_s": 4.0}
        for index in range(7)
    ]
    manifest = _manifest(media=[*CASE["media"], *extra])
    drafted = _drafted(await _plan(monkeypatch, _raw(_strategy()), manifest=manifest))

    selected = drafted["strategy"]["selected_media_ids"]
    assert len(selected) == 12
    assert PHOTO not in selected
    assert "12" in drafted["summary"]


# 8 + 9 -----------------------------------------------------------------------


@pytest.mark.parametrize("scope", ["all", "selected"])
def test_a_contract_on_a_v2_voiceover_is_refused_with_what_to_do_instead(
    prod_profile, scope
) -> None:
    """The prod turn-3 answer (the contract, no beats) is a retry, and the retry is told
    to drop the contract and time the photo with reaction_beats."""

    agent = MainCreatorAgent(model_client=None)
    raw = _raw(
        _strategy(execution_contract="guided_voiceover_v1", media_scope=scope, reaction_beats=None)
    )

    with pytest.raises(SchemaError):
        agent.parse(raw, _agent_input(TURN3, _manifest()))
    feedback = agent.schema_clarification()
    assert "leave execution_contract unset" in feedback
    assert "reaction_beats" in feedback


async def test_exact_photo_video_timing_on_a_v2_voiceover_is_asked_about(
    prod_profile, monkeypatch
) -> None:
    """The boundary reads exact timing from the creator's words, so the model cannot drop
    it: refusing at the parse would fail every retry. The planner asks instead."""

    turn = {**TURN3, "user_message": "Make the photos very fast and let the videos linger longer."}
    planned = await _plan(monkeypatch, _raw(_strategy()), turn=turn)

    assert planned.plan.mode == "respond"
    assert "timing" in planned.plan.response


def test_v1_all_scope_voiceover_still_needs_the_contract(prod_profile) -> None:
    """The native "all" reading is runtime v2's: v1 still runs the guided contract."""

    agent = MainCreatorAgent(model_client=None)
    with pytest.raises(SchemaError):
        agent.parse(_raw(_strategy()), _agent_input(TURN3, _manifest(runs_guided_voiceover=True)))
    assert "guided_voiceover_v1" in agent.schema_clarification()
