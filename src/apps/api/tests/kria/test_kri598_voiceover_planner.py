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
8. A contract the model returns anyway is refused at the parse: every refusal spends a
   retry and three end the turn, a terminal failure the downgrade used to avoid. Or the
   repair leaks into v1, where an unavailable contract must still be refused.
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
from app.agents._schemas.creator_agent import CapabilityAvailability, CreativeStrategy
from app.agents._schemas.creator_policy import (
    CAPABILITY_GUIDED_VOICEOVER,
    GUIDED_VOICEOVER_NATIVE_ONLY_CODE,
)
from app.agents.main_creator import MainCreatorAgent, MainCreatorInput
from app.kria import planner
from app.kria.brief import BriefRequirement, CreativeBrief, requested_media
from app.kria.reply_language import reply_language_for
from app.kria.strategy_policy import (
    GUIDED_VOICEOVER_DOWNGRADE_NOTICE,
    VISUALS_NEED_A_WORD_NOTICE,
    VOICEOVER_CLIP_CAP_NOTICE,
    CheckedStrategy,
    RefusedStrategy,
    check_strategy_for_runtime_v2,
)
from app.kria.voiceover_scope import VOICEOVER_VISUALS_LEFT_OUT_NOTICE
from app.schemas.clip_intents import ClipIntent, ResolvedClipIntent
from app.services import creator_capabilities as capabilities
from app.services import creator_sessions
from app.services.clip_intent_planning import PlannedIntentResolution
from app.services.clip_intent_resolution import IntentClip, IntentResolution
from app.tasks import content_plan_build, generative_build

# KRI-594's record of the prod brief turn 3 planned against (r1-r9, r5 = the bathtub photo
# on "kırkayak"), shared so both incidents judge the same requirements.
from tests.kria.test_voiceover_downgrade_losses import PHOTO_ASK, TIGRE_BRIEF

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "kri598_tigre_voiceover_turns.json"
CASE: dict[str, Any] = json.loads(FIXTURE.read_text())
OWN = [m["media_id"] for m in CASE["media"] if m["kind"] == "video"]
PHOTO = "asset-fe1c1382-89e1-440e-b54d-f3a4d945f4ff"
RIVER = "analysis-proxy-ios-ECCB47EC-0C2C-4F64-917F-85FB8039C9D7.mp4"
KITCHEN = "analysis-proxy-ios-80DCD337-53E9-426D-8DC8-31AEC9359F76.mp4"
LAPTOP = "analysis-proxy-ios-CFBEC1AE-2523-49F2-B675-04576EAED824.mp4"
BATHROOM = "analysis-proxy-ios-A9859821-B374-42D6-9F07-F9C4734506C7.mp4"
BRIEF_WITHOUT_PHOTO_ASK = CreativeBrief(
    version=TIGRE_BRIEF.version,
    requirements=[req for req in TIGRE_BRIEF.requirements if req.id != "r5"],
)
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


async def _plan(
    monkeypatch: pytest.MonkeyPatch,
    raw: str,
    *,
    manifest=None,
    turn=None,
    brief: CreativeBrief | None = None,
    brief_binding: bool = True,
):
    """`brief` is the live Creative Brief the planner read the asked-for media from."""

    manifest = manifest if manifest is not None else _manifest()
    turn = turn or TURN3
    agent_input = _agent_input(turn, manifest)
    asked = requested_media(brief, (media.media_id for media in manifest.media))
    output = MainCreatorAgent(model_client=None).parse(raw, agent_input)
    monkeypatch.setattr(
        planner, "plan_and_resolve_clip_intents", AsyncMock(return_value=_resolution())
    )
    monkeypatch.setattr(planner, "_load_thread_events", AsyncMock(return_value=[]))
    monkeypatch.setattr(planner, "_kick_clip_understanding", AsyncMock())
    monkeypatch.setattr(type(planner.settings), "brief_binding_for", lambda _s, _i: brief_binding)
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
        requested_media=asked,
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


def _with_own_clips(count: int):
    """The Tigre manifest with `count` own clips (the 7 recorded ones first)."""

    extra = [
        {"media_id": f"analysis-proxy-ios-EXTRA-{index}.mp4", "kind": "video", "duration_s": 4.0}
        for index in range(count - len(OWN))
    ]
    return _manifest(media=[*CASE["media"], *extra])


USE_ALL_13 = {
    **TURN3,
    "user_message": "Use all 13 clips with my voiceover.",
    "creator_request": "",
    "conversation": [{"role": "user", "content": "Use all 13 clips with my voiceover."}],
}


@pytest.mark.parametrize(
    ("turn", "title"),
    [(TURN3, TITLE), (USE_ALL_13, None)],
    ids=["7-klibin-hepsini-kullan", "use-all-13-clips"],
)
async def test_all_scope_over_the_native_cap_is_asked_about(
    prod_profile, monkeypatch, turn, title
) -> None:
    """PR 1556 review: "all" is an explicit ask for every clip, and one native voiceover
    edit takes 12. A 13th clip left out is a lost ask, as in the KRI-594 downgrade: the
    question names the cap instead of a 12-clip draft with a notice."""

    raw = _raw(_strategy(opening_title=title))
    planned = await _plan(monkeypatch, raw, manifest=_with_own_clips(13), turn=turn)

    assert planned.plan.mode == "respond", planned.plan.intents
    assert VOICEOVER_CLIP_CAP_NOTICE in planned.plan.response
    assert planned.plan.response.endswith("Should I make a simpler version?")

    with reply_language_for("tr"):
        turkish = await _plan(monkeypatch, raw, manifest=_with_own_clips(13), turn=turn)
    assert turkish.plan.mode == "respond"
    assert "en fazla 12" in turkish.plan.response


async def test_all_scope_within_the_native_cap_is_planned(prod_profile, monkeypatch) -> None:
    manifest = _with_own_clips(12)
    drafted = _drafted(await _plan(monkeypatch, _raw(_strategy()), manifest=manifest))

    selected = drafted["strategy"]["selected_media_ids"]
    assert sorted(selected) == sorted(
        media.media_id for media in manifest.media if media.kind == "video"
    )
    assert VOICEOVER_CLIP_CAP_NOTICE not in drafted["summary"]


async def test_all_scope_over_the_cap_without_brief_binding_is_a_notice(
    prod_profile, monkeypatch
) -> None:
    """With brief binding off nothing is asked before simplifying: the cap is said."""

    planned = await _plan(
        monkeypatch, _raw(_strategy()), manifest=_with_own_clips(13), brief_binding=False
    )

    drafted = _drafted(planned)
    selected = drafted["strategy"]["selected_media_ids"]
    assert len(selected) == 12
    assert PHOTO not in selected
    assert VOICEOVER_CLIP_CAP_NOTICE in drafted["summary"]


def test_a_selected_clip_the_check_drops_is_a_lost_ask(prod_profile) -> None:
    """PR 1556 review: the drop check judges `selected_media_ids` too. A clip the
    strategy selected that the checked plan no longer has is asked about."""

    from app.kria.strategy_policy import _drops_requested_action

    before = CreativeStrategy.model_validate(
        _strategy(media_scope="selected", selected_media_ids=OWN)
    )
    after = before.model_copy(update={"selected_media_ids": OWN[:-1]})

    assert _drops_requested_action(before, after) is True
    assert _drops_requested_action(before, before) is False
    # Adding a clip (KRI-593 keeps every clip) is not a loss.
    assert _drops_requested_action(after, before) is False


# 8 + 9 -----------------------------------------------------------------------


@pytest.mark.parametrize(
    "model_scope",
    [
        # The prod turn-3 answer: the contract over all media, no beats.
        {"media_scope": "all", "selected_media_ids": []},
        # The contract over the cue clips; the scope reader still reads "all".
        {"media_scope": "selected", "selected_media_ids": [RIVER, LAPTOP, KITCHEN, BATHROOM]},
    ],
)
async def test_a_contract_on_a_v2_voiceover_is_repaired_into_a_plan(
    prod_profile, monkeypatch, model_scope
) -> None:
    """A contract the v2 manifest never offers is dropped at the parse, not refused: a
    refusal spends a retry, and three refusals end the turn ("I couldn't turn that into a
    plan"). With no live ask for the bathtub photo (a brief without r5), the planner then
    plans every own clip and only adds the notice that the photo is not tied to a word."""

    raw = _raw(
        _strategy(execution_contract="guided_voiceover_v1", reaction_beats=None, **model_scope)
    )
    agent = MainCreatorAgent(model_client=None)
    # One parse, no SchemaError: the turn never spends a retry on it.
    parsed = agent.parse(raw, _agent_input(TURN3, _manifest()))
    assert parsed.action.strategy.execution_contract is None

    planned = await _plan(monkeypatch, raw, brief=BRIEF_WITHOUT_PHOTO_ASK)
    drafted = _drafted(planned)
    strategy = drafted["strategy"]
    assert strategy.get("execution_contract") is None
    assert strategy["render_program"] == "native"
    assert strategy["media_scope"] == "selected"
    assert sorted(strategy["selected_media_ids"]) == sorted(OWN)
    # Nobody asked for the bathtub photo: said, not asked.
    assert VOICEOVER_VISUALS_LEFT_OUT_NOTICE in drafted["summary"]


# 11 + 12 (PR 1556 review) ------------------------------------------------------------


def _asks_about_the_bathtub_photo(question: str) -> None:
    """The question names the photo in the creator's own (brief) words, once, and says
    nothing generic about Visuals."""

    assert question.count(PHOTO_ASK) == 1, question
    for generic in (
        VOICEOVER_VISUALS_LEFT_OUT_NOTICE,
        VISUALS_NEED_A_WORD_NOTICE,
        GUIDED_VOICEOVER_DOWNGRADE_NOTICE,
    ):
        assert generic not in question


@pytest.mark.parametrize(
    "model_scope",
    [
        # (a) The prod turn-3 answer: the contract over all media, no beats.
        {"execution_contract": "guided_voiceover_v1", "media_scope": "all"},
        # (e) A native answer that simply forgot the beat.
        {"media_scope": "selected", "selected_media_ids": OWN},
        {"media_scope": "all"},
    ],
    ids=["repaired-contract", "native-selected", "native-all"],
)
async def test_a_photo_the_brief_asks_for_is_asked_about_when_no_word_places_it(
    prod_profile, monkeypatch, model_scope
) -> None:
    """The prod brief (r1-r9) still carries r5: the bathtub photo on "kırkayak". The native
    voiceover edit can show a Visual only on a spoken word, so an answer without that beat
    loses it -- whether the contract was repaired at the parse or the model answered
    natively. That is a question naming the photo, never a silent draft."""

    raw = _raw(_strategy(reaction_beats=None, **model_scope))

    planned = await _plan(monkeypatch, raw, brief=TIGRE_BRIEF)

    assert planned.plan.mode == "respond", planned.plan.intents
    question = planned.plan.response
    _asks_about_the_bathtub_photo(question)
    assert question.startswith('This version leaves out the photo you asked for: "')
    assert question.endswith("Should I make a simpler version?")

    with reply_language_for("tr"):
        turkish = await _plan(monkeypatch, raw, brief=TIGRE_BRIEF)
    assert turkish.plan.mode == "respond"
    assert turkish.plan.response.startswith('Bu sürümde istediğin fotoğraf yok: "')
    assert turkish.plan.response.endswith("Daha sade bir sürüm yapayım mı?")


def test_the_contract_reaching_the_kri594_downgrade_asks_the_same_question(
    prod_profile,
) -> None:
    """(b) Where the contract survives to the policy (a manifest that still offers it), the
    KRI-594 downgrade asks about the photo: the same question as the native path, once,
    with no generic Visuals notice next to it."""

    v1 = _manifest(runs_guided_voiceover=True)
    strategy = CreativeStrategy.model_validate(
        _strategy(
            execution_contract="guided_voiceover_v1",
            media_scope="all",
            render_program="guided",
            reaction_beats=None,
        )
    )
    asked = requested_media(TIGRE_BRIEF, (media.media_id for media in v1.media))

    refused = check_strategy_for_runtime_v2(
        v1, strategy, ask_before_simplifying=True, requested_media=asked
    )
    native = check_strategy_for_runtime_v2(
        _manifest(),
        strategy.model_copy(update={"execution_contract": None, "render_program": "native"}),
        ask_before_simplifying=True,
        requested_media=asked,
    )

    assert isinstance(refused, RefusedStrategy) and isinstance(native, RefusedStrategy)
    _asks_about_the_bathtub_photo(refused.question)
    assert refused.question == native.question
    assert refused.code == native.code == "simplification_requires_choice"


@pytest.mark.parametrize(
    "words", ["Show the bathtub photo, no zoom", "Show the bathtub photo without zoom"]
)
async def test_a_no_zoom_photo_ask_is_still_asked_about_on_the_native_path(
    prod_profile, monkeypatch, words
) -> None:
    """PR 1556 review (with KRI-594's `requested_media` fix): a select that only restricts
    how the photo looks is still an ask for it. A native answer (no contract to downgrade)
    with no beat for that photo asks about it, in the creator's words."""

    brief = CreativeBrief(
        version=1,
        requirements=[
            *(req for req in TIGRE_BRIEF.requirements if req.id != "r5"),
            BriefRequirement(id="r5", kind="select", scope=f"clip:{PHOTO}", description=words),
        ],
    )
    raw = _raw(_strategy(reaction_beats=None, media_scope="selected", selected_media_ids=OWN))

    planned = await _plan(monkeypatch, raw, brief=brief)

    assert planned.plan.mode == "respond", planned.plan.intents
    assert planned.plan.response.startswith(
        f'This version leaves out the photo you asked for: "{words}".'
    )
    assert planned.plan.response.count(words) == 1


async def test_the_photo_placed_on_its_word_is_planned_not_asked_about(
    prod_profile, monkeypatch
) -> None:
    """(c) The beat on "kırkayak" places the photo r5 asks for: a plan, no question."""

    drafted = _drafted(await _plan(monkeypatch, _raw(_strategy()), brief=TIGRE_BRIEF))

    assert [(b["trigger"], b["visual_id"]) for b in drafted["strategy"]["reaction_beats"]] == [
        ("kırkayak", PHOTO)
    ]
    assert PHOTO_ASK not in drafted["summary"]
    assert VOICEOVER_VISUALS_LEFT_OUT_NOTICE not in drafted["summary"]


async def test_a_visual_nobody_asked_for_is_only_a_notice(prod_profile, monkeypatch) -> None:
    """(d) Visuals the brief never asked for stay notice-only; the brief's own-clip cues
    (r4, r6) are selection, which this check never asks about."""

    planned = await _plan(
        monkeypatch, _raw(_strategy(reaction_beats=None)), brief=BRIEF_WITHOUT_PHOTO_ASK
    )

    drafted = _drafted(planned)
    assert VOICEOVER_VISUALS_LEFT_OUT_NOTICE in drafted["summary"]
    assert PHOTO_ASK not in drafted["summary"]
    checked = check_strategy_for_runtime_v2(
        _manifest(),
        CreativeStrategy.model_validate(
            _strategy(reaction_beats=None, media_scope="selected", selected_media_ids=OWN)
        ),
        ask_before_simplifying=True,
        requested_media=requested_media(BRIEF_WITHOUT_PHOTO_ASK, [*OWN, PHOTO]),
    )
    assert isinstance(checked, CheckedStrategy), getattr(checked, "question", None)


def test_a_contract_on_a_v1_manifest_that_cannot_run_it_is_still_refused(prod_profile) -> None:
    """The repair is runtime v2's: anywhere else an unavailable contract is still a retry."""

    v1 = _manifest(runs_guided_voiceover=True)
    closed = v1.model_copy(
        update={
            "capabilities": {
                **v1.capabilities,
                CAPABILITY_GUIDED_VOICEOVER: CapabilityAvailability(
                    available=False,
                    reason_code="unsupported_phone_audio",
                    reason="phone rendering does not support recorded voiceover",
                ),
            }
        }
    )
    agent = MainCreatorAgent(model_client=None)

    with pytest.raises(SchemaError):
        agent.parse(
            _raw(_strategy(execution_contract="guided_voiceover_v1")), _agent_input(TURN3, closed)
        )
    assert "phone rendering does not support recorded voiceover" in agent.schema_clarification()


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


# PR 1556 review: an explicit all-media ask on the native path -------------------------


@pytest.mark.parametrize(
    "creator_text",
    ["Use all my photos and videos", "Fotoğrafların ve videoların hepsini kullan", "use all 8"],
)
def test_an_all_media_ask_asks_when_a_visual_cannot_be_placed(prod_profile, creator_text) -> None:
    """Parity with the KRI-594 guided downgrade: "all" that is not just the own clips asked
    for the Visuals photo too, so a native edit that cannot place it asks first."""

    checked = check_strategy_for_runtime_v2(
        _manifest(),
        CreativeStrategy.model_validate(_strategy(reaction_beats=None, media_scope="all")),
        ask_before_simplifying=True,
        creator_text=creator_text,
        requested_media={},
    )

    assert isinstance(checked, RefusedStrategy), checked
    assert VOICEOVER_VISUALS_LEFT_OUT_NOTICE in checked.question or ("Visuals" in checked.question)


def test_all_seven_clips_on_the_native_path_stays_a_notice(prod_profile) -> None:
    """The Tigre turn-3 wording counts exactly the own clips: the photo was not asked for by
    "all", so leaving it out is a notice, not a question."""

    checked = check_strategy_for_runtime_v2(
        _manifest(),
        CreativeStrategy.model_validate(_strategy(reaction_beats=None, media_scope="all")),
        ask_before_simplifying=True,
        creator_text="Sira seslendirmeyi takip etsin. 7 klibin hepsini kullan",
        requested_media={},
    )

    assert isinstance(checked, CheckedStrategy), getattr(checked, "question", None)
    assert sorted(checked.strategy.selected_media_ids) == sorted(OWN)


def test_an_all_media_ask_with_the_photo_on_a_word_is_planned(prod_profile) -> None:
    checked = check_strategy_for_runtime_v2(
        _manifest(),
        CreativeStrategy.model_validate(_strategy(media_scope="all")),
        ask_before_simplifying=True,
        creator_text="Use all my photos and videos",
        requested_media={},
    )

    assert isinstance(checked, CheckedStrategy), getattr(checked, "question", None)


async def test_an_all_media_ask_reaches_the_native_check_through_the_planner(
    prod_profile, monkeypatch
) -> None:
    turn = {
        **TURN3,
        "user_message": "Use all my photos and videos",
        "conversation": [{"role": "user", "content": "Use all my photos and videos"}],
    }
    planned = await _plan(
        monkeypatch,
        _raw(_strategy(reaction_beats=None, media_scope="all", opening_title=None)),
        turn=turn,
        brief=BRIEF_WITHOUT_PHOTO_ASK,
    )

    assert planned.plan.mode == "respond", planned.plan
    assert planned.plan.response.endswith("?")
