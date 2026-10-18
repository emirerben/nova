"""KRI-691: a spoken-word label on a phone Voiceover chat is left out, never a dead end.

Prod thread 2ea478ec (2026-10-15, boat kit N1 "Atakan'ı almaya"): an iPhone Voiceover edit
with eight own clips, one Visuals photo and a 26 s Turkish voiceover. The creator asked for
clip cues on spoken words ("Sergio" dediğimde ...), a photo on "ışıklar", "20 dk" on screen
when they say "yirmi dakika", and the boat-tying clip last. From the picker answer on, every
turn replied "Bu etiketler için kaydedilmiş bir seslendirme ve rehberli görseller gerekiyor"
(diagnostics `{"status": "needs_creator"}`: the planner's clip-intent gate), although the
voiceover was recorded, so the edit never rendered.

Only the guided contract (`guided_voiceover_v1`) draws a transcript label, and since KRI-598
a runtime-v2 manifest with a voiceover never offers it (`native_voiceover_only`). Asking for
"a recorded voiceover with guided visuals" there asks for something the chat can never give.
Which intent carried `label_source="transcript"` is unconfirmed (model outputs are redacted
in the trace); the "yirmi dakika" moment is the stand-in, as the likeliest one.

Every case resolves its manifest through the REAL capability resolver from the recorded
prod media, runs the REAL Main Creator parse and the REAL runtime-v2 planner under the
production flag profile. Only the model text and the clip-intent planner's answer are
stand-ins.

Ways this could go wrong, written down before the code (each has a case):

1. The clip-intent inventory carries a transcript label on a native-only chat and the
   planner still refuses every turn (the prod bug).
2. The Main Creator writes the transcript label itself: the strategy check refuses with
   the same words (`transcript_labels_unavailable`), or, with brief binding on, turns the
   drop into a "simpler version?" question that the next turn asks again.
3. The label is dropped silently, or the reply says so in English in a Turkish chat.
4. Both sources carry a label and the notice is said twice.
5. The dropped label survives in the draft (requested or resolved intents), so the server
   compile refuses the draft later, or the clip cues and the last-clip order are dropped
   with it.
6. A request with only spoken-word labels mints no plan.
7. The fix leaks to a manifest that still offers the guided contract, where the question
   is answerable and must stay (manifests without a voiceover are pinned by
   `test_talking_transcript_labels_still_ask_for_a_voiceover` and
   `test_live_creator_clip_intent_resolution_is_server_owned_and_fails_closed`).
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

from app.agents._schemas.creator_agent import CreativeStrategy
from app.agents._schemas.creator_policy import (
    CAPABILITY_GUIDED_VOICEOVER,
    GUIDED_VOICEOVER_NATIVE_ONLY_CODE,
)
from app.agents.main_creator import MainCreatorAgent, MainCreatorInput
from app.kria import planner
from app.kria.reply_language import reply_language_for
from app.schemas.clip_intents import ClipIntent, ResolvedClipIntent
from app.services import creator_capabilities as capabilities
from app.services.clip_intent_planning import PlannedIntentResolution
from app.services.clip_intent_resolution import IntentClip, IntentResolution

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "kri691_boat_n1_voiceover_turns.json"
CASE: dict[str, Any] = json.loads(FIXTURE.read_text())
OWN = [m["media_id"] for m in CASE["media"] if m["kind"] == "video"]
PHOTO = "asset-d9627a79-b228-4db0-9cbb-201f6f921bb2"
TIE_UP = "analysis-proxy-ios-77D2BC74-C050-4A2B-AA5A-28D1CB1152BD.mp4"  # picker: "clip 1"
SERGIO = "analysis-proxy-ios-40602D2B-CC63-4235-911D-BE67B6608AC0.mp4"
LUNAPARK = "analysis-proxy-ios-50666494-269A-440A-B583-A72B0016D880.mp4"
BROKEN = "analysis-proxy-ios-0847280C-3D64-4327-9318-5EABD6EB8AED.mp4"
TITLE = "Atakan'ı almaya"
# The turns that refused in prod: the picker answer and "Seslindirmeyi kaydettim ...".
REFUSED_TURNS = CASE["turns"][1:]
REFUSAL_TR = "Bu etiketler için kaydedilmiş bir seslendirme ve rehberli görseller gerekiyor."
DROPPED_TR = (
    "Seslendirmendeki sözler bu düzenlemede henüz kliplerin üzerinde gösterilemiyor, o "
    "yüzden bu etiketleri koymadım."
)
SPOKEN_LABEL = {
    "intent_id": "caption_yirmi_dakika",
    "op": "label",
    "attribute": "the moment 'yirmi dakika' is said",
    "label_source": "transcript",
    "transcript_kind": "topic",
}
CUES = [
    ({"intent_id": "include_sergio_clip", "op": "include", "attribute": "kaptan"}, SERGIO),
    ({"intent_id": "include_calismadi_clip", "op": "include", "attribute": "tekne"}, BROKEN),
    ({"intent_id": "include_lunapark_clip", "op": "include", "attribute": "lunapark"}, LUNAPARK),
    (
        {
            "intent_id": "order_last_boat_tie",
            "op": "order",
            "position": "last",
            "attribute": "tekneyi bağladığımız klip",
        },
        TIE_UP,
    ),
]


@pytest.fixture(autouse=True)
def _prod_rollout(monkeypatch: pytest.MonkeyPatch) -> None:
    # A number, not a boolean flag, so the prod profile leaves it at the default (0).
    monkeypatch.setattr(capabilities.settings, "main_creator_agent_rollout_percent", 100)


def _manifest(*, runs_guided_voiceover: bool = False):
    return capabilities.resolve_creator_manifest(
        item_id=CASE["item_id"],
        edit_format=CASE["edit_format"],
        has_voiceover=True,
        narration=CASE["narration"],
        media=CASE["media"],
        phone_source_media_ids=OWN,
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


def _raw(*, spoken_label: bool = False) -> str:
    """A native voiceover answer for the N1 request: no contract, the cues as intents, the
    photo on "ışıklar". ``spoken_label`` adds the transcript label to the model's own
    candidates (failure mode 2)."""

    strategy: dict[str, Any] = {
        "direction": "native",
        "edit_format": "narrated_planned",
        "audio_strategy": "voiceover",
        "render_program": "native",
        "media_scope": "selected",
        "selected_media_ids": OWN,
        "caption_style": "auto",
        "opening_title": TITLE,
        "target_duration_s": 26,
        "clip_intents": [row for row, _media in CUES] + ([SPOKEN_LABEL] if spoken_label else []),
        "reaction_beats": [
            {"beat_id": "isiklar", "trigger": "ışıklar", "visual_id": PHOTO, "visual_role": "photo"}
        ],
        "rationale": "Seslendirme 26 saniye; klipler söylediğin kelimelerde.",
    }
    return json.dumps(
        {
            "action": {
                "kind": "propose_strategy",
                "strategy": strategy,
                "summary": "Klipler seslendirmeni takip edecek.",
                "render_intent_evidence": {"opening_title": f'Başlık: "{TITLE}"'},
            },
            "brief_updates": [],
        },
        ensure_ascii=False,
    )


def _resolution(*, spoken_label: bool = True, cues: bool = True) -> PlannedIntentResolution:
    """The clip-intent planner's inventory: the cues (resolved to their clips, as the
    picker answered) plus, as in prod, a transcript label the resolver never sees."""

    rows = [row for row, _media in CUES] if cues else []
    resolved = [
        ResolvedClipIntent.model_validate(
            {**row, "assignments": [{"media_id": media, "confidence": 1.0}]}
        )
        for row, media in (CUES if cues else [])
    ]
    requested = [ClipIntent.model_validate(row) for row in rows]
    if spoken_label:
        requested.append(ClipIntent.model_validate(SPOKEN_LABEL))
    return PlannedIntentResolution(requested, IntentResolution(intents=resolved))


async def _plan(
    monkeypatch: pytest.MonkeyPatch,
    raw: str,
    *,
    turn: dict[str, Any],
    resolution: PlannedIntentResolution,
    manifest=None,
):
    manifest = manifest if manifest is not None else _manifest()
    agent_input = _agent_input(turn, manifest)
    output = MainCreatorAgent(model_client=None).parse(raw, agent_input)
    monkeypatch.setattr(
        planner, "plan_and_resolve_clip_intents", AsyncMock(return_value=resolution)
    )
    monkeypatch.setattr(planner, "_load_thread_events", AsyncMock(return_value=[]))
    monkeypatch.setattr(planner, "_kick_clip_understanding", AsyncMock())
    # Brief binding on: a dropped request would become a "simpler version?" question.
    monkeypatch.setattr(type(planner.settings), "brief_binding_for", lambda _s, _i: True)
    clips = [IntentClip(media_id=m.media_id, kind=m.kind, analysis=None) for m in manifest.media]
    with reply_language_for("tr"):
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
    apply_intent, render = planned.plan.intents
    assert apply_intent.tool_name == "draft.apply_strategy"
    assert render.tool_name == "render.request"
    return apply_intent.arguments


def _assert_cues_kept_without_the_spoken_label(drafted: dict[str, Any]) -> None:
    strategy = drafted["strategy"]
    requested = strategy.get("clip_intents") or []
    resolved = strategy.get("resolved_clip_intents") or []
    assert all(intent.get("label_source", "clip") == "clip" for intent in requested + resolved)
    assert [i["intent_id"] for i in requested] == [row["intent_id"] for row, _media in CUES]
    assert {i["intent_id"]: i["assignments"][0]["media_id"] for i in resolved} == {
        row["intent_id"]: media for row, media in CUES
    }
    assert strategy.get("execution_contract") is None
    assert strategy["render_program"] == "native"
    assert [(b["trigger"], b["visual_id"]) for b in strategy["reaction_beats"]] == [
        ("ışıklar", PHOTO)
    ]
    # The draft goes through the server compile the render path runs: no narration-label
    # refusal waits for it there.
    capabilities.compile_strategy_to_plan(_manifest(), CreativeStrategy.model_validate(strategy))


def test_the_recorded_manifest_is_native_voiceover_only(prod_profile) -> None:
    """The precondition: the replayed manifest closes the guided contract exactly as the
    recorded prod manifest did, while the voiceover itself is on it."""

    manifest = _manifest()
    guided = manifest.capabilities[CAPABILITY_GUIDED_VOICEOVER]

    assert manifest.has_voiceover and manifest.narration is not None
    assert guided.available is CASE["recorded_guided_voiceover"]["available"] is False
    assert guided.reason_code == CASE["recorded_guided_voiceover"]["reason_code"]
    assert guided.reason_code == GUIDED_VOICEOVER_NATIVE_ONLY_CODE


@pytest.mark.asyncio
@pytest.mark.parametrize("turn", REFUSED_TURNS, ids=[t["label"] for t in REFUSED_TURNS])
async def test_n1_turn_with_a_spoken_word_label_plans_the_edit(
    prod_profile, monkeypatch, turn
) -> None:
    """Failure modes 1, 3 and 5: the prod turns plan the edit and say what was left out."""

    planned = await _plan(monkeypatch, _raw(), turn=turn, resolution=_resolution())
    drafted = _drafted(planned)

    assert REFUSAL_TR not in drafted["summary"]
    assert drafted["summary"].count(DROPPED_TR) == 1
    assert DROPPED_TR in planned.policy_notices
    _assert_cues_kept_without_the_spoken_label(drafted)


@pytest.mark.asyncio
async def test_a_spoken_word_label_the_model_writes_itself_is_dropped_not_asked_about(
    prod_profile, monkeypatch
) -> None:
    """Failure mode 2: with brief binding on, the drop is a notice, not a question."""

    planned = await _plan(
        monkeypatch,
        _raw(spoken_label=True),
        turn=REFUSED_TURNS[0],
        resolution=_resolution(spoken_label=False),
    )
    drafted = _drafted(planned)

    assert drafted["summary"].count(DROPPED_TR) == 1
    assert "Daha sade bir sürüm" not in drafted["summary"]
    _assert_cues_kept_without_the_spoken_label(drafted)


@pytest.mark.asyncio
async def test_both_sources_say_it_once(prod_profile, monkeypatch) -> None:
    """Failure mode 4."""

    planned = await _plan(
        monkeypatch, _raw(spoken_label=True), turn=REFUSED_TURNS[0], resolution=_resolution()
    )
    drafted = _drafted(planned)

    assert drafted["summary"].count(DROPPED_TR) == 1
    assert planned.policy_notices.count(DROPPED_TR) == 1
    _assert_cues_kept_without_the_spoken_label(drafted)


@pytest.mark.asyncio
async def test_only_spoken_word_labels_still_plan(prod_profile, monkeypatch) -> None:
    """Failure mode 6: nothing left to resolve is still a plan, with no clip intents."""

    planned = await _plan(
        monkeypatch,
        _raw(),
        turn=REFUSED_TURNS[0],
        resolution=_resolution(cues=False),
    )
    drafted = _drafted(planned)

    assert "clip_intents" not in drafted["strategy"]
    assert DROPPED_TR in drafted["summary"]


@pytest.mark.asyncio
async def test_a_chat_that_offers_the_guided_contract_keeps_the_question(
    prod_profile, monkeypatch
) -> None:
    """Failure mode 7: where the guided contract is offered, the label is answerable (pick
    the contract), so the planner still asks instead of dropping it."""

    planned = await _plan(
        monkeypatch,
        _raw(),
        turn=REFUSED_TURNS[0],
        resolution=_resolution(),
        manifest=_manifest(runs_guided_voiceover=True),
    )

    assert planned.plan.mode == "respond"
    assert planned.plan.response == REFUSAL_TR
