"""KRI-950: tapping a clip in the picker never loses an instruction the last turn had checked.

Prod thread 545F53A9 (Don Julio walk-in M2, 2026-10-26, main 5a96e1d58). Turn 1 understood
every clip instruction, including the creator's text "Ojo de bife 🥩" on the ribeye photo, and
asked one picker question (the queue). The creator tapped the only video. Turn 2 re-ran the
clip-intent planner from scratch on the same words; this time the model wrote the photo text as
a `label` whose quote did not contain the text, the parser rejected it
(`creator_text_not_source_backed`), and the salvage held back all six good rows to ask:
"I understood 6 of your clip instructions. I couldn't safely verify "label: the ribeye photo".
Please restate just those so I can add them." The creator had typed nothing.

The fix: the turn that asks a clip picker keeps the planner's checked list on its own turn
record (server-only). A turn whose only new input is that picker's tap or skip reuses the list
instead of asking the model again. A small parser safety net also repairs the two model slips
that only mis-point at the creator's words.

Ways this can fail, each pinned below (written before the code):

 1. The tap turn still asks the planner model again, and one bad row asks "please restate"
    although the creator typed nothing (the bug).
 2. The reused list loses or rewrites an instruction: the photo text must stay exactly
    "Ojo de bife 🥩" on the photo, and the tapped queue must land on the tapped video.
 3. Reuse after the creator TYPED something: a correction must re-plan from their new words.
 4. Reuse against the wrong request: the saved list belongs to other words (an extra message,
    a re-send), or comes from a turn that never saved one (asked before this deploy). Re-plan.
 5. Reuse when the tap rides with another answer (a choice card on the same turn, KRI-802), or
    for any other kind of answer (choice, mark, moment order). Re-plan.
 6. A broken or unknown saved list crashes the turn. It must be ignored (re-plan, as today).
 7. "Skip" stops working: a skipped picker still reuses the list and lets Kria choose.
 8. The second picker in a row (turn 3 after a reused turn 2) re-plans again: each picker turn
    must keep the list it used.
 9. A Turkish chat's tap ("Sorduğun klipleri seçtim.") is not recognised as a tap.
14. (Found in self-review, before the fix.) An event the server writes on its own after the
    tap (a finished render, a review) hides the tap, so the turn quietly re-plans.
10. The saved list leaks: it must never ride in the chat event the app downloads, and the admin
    turns view shows only ids and ops (the planner is a sensitive agent: no creator text).
11. Plans that ask no clip picker change shape (byte-identical: no new key).
12. The safety net loosens the creator-words fence: text that is not the creator's exact words
    (another emoji, capital letters, a paraphrase) or sits in a different sentence than the
    quote must still be asked about.
13. The safety net rewrites the creator's text: an emoji's invisible presentation selector the
    model added or dropped must print the creator's own form.

Replayed from the real M2 message, clip list and turn diagnostics. Only the provider boundary is
replayed (the clip-intent planner and resolver answers); the Main Creator parse, the planner
parse, the picker builder, the selection fold and `_plan_from_creator_output` are real. No
network, no paid model call.
"""

from __future__ import annotations

import copy
import json
import re
import uuid
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from app.agents._runtime import ModelClient, ModelInvocation
from app.agents.clip_intent_planner import ClipIntentPlannerAgent, ClipIntentPlannerInput
from app.agents.main_creator import MainCreatorAgent, MainCreatorInput
from app.config import settings
from app.kria import planner
from app.kria.contracts import KriaTurnPlan
from app.kria.planner import PlannedKriaTurn
from app.kria.reply_language import bind_reply_language, release_reply_language
from app.services import clip_intent_planning as planning
from app.services import clip_intent_resolution as resolution
from app.services.choice_questions import tag_event
from app.services.clip_intent_resolution import IntentClip
from app.services.song_order import (
    CLIP_ANSWER_MESSAGE,
    CLIP_ANSWER_MESSAGE_TR,
    CLIP_SKIP_MESSAGE,
)

# A recorded Don Julio montage turn (KRI-802) carries the Main Creator input; the M2 words,
# clips and clip rows below replace its own.
FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "kri802_don_julio_m1_turn1.json"
CASE: dict[str, Any] = json.loads(FIXTURE.read_text())

# The M2 kit prompt, as sent (thread 545F53A9, 09:53:32Z).
PROMPT = (
    "This is one 2-minute take of our walk into Don Julio. Pull out six moments, about 2 "
    "seconds each, in this order: the DON JULIO window sign from the street, the queue under "
    "the striped awning, the butcher holding a cut of beef, the cook at the grill with his "
    "tongs, the dried flowers hanging from the ceiling, and the two red Michelin plaques on "
    "the brick wall. Right after the butcher moment, show the ribeye photo full screen with "
    'the text "Ojo de bife 🥩". End on Atakan (black T-shirt) saying "Super excited!" with '
    'his sound; everything else silent. Title: "Don Julio, Palermo". Make it 15 seconds.'
)
CAPTION = "Ojo de bife 🥩"
VIDEO = "analysis-proxy-ios-155481DC-92B9-431A-BD9B-6D69972E2894.mp4"
PHOTO = "pool-d17aeec6-ribeye.jpg"
RESTATE = (
    "I understood 6 of your clip instructions. I couldn't safely verify \"label: the ribeye "
    'photo". Please restate just those so I can add them.'
)
QUEUE_KEY = "include:the-queue-under-the-striped-awning"


def _include(intent_id: str, words: str) -> dict[str, Any]:
    return {"intent_id": intent_id, "op": "include", "attribute": words, "source_quote": words}


MOMENTS = [
    _include("clip_moment_sign_include", "the DON JULIO window sign from the street"),
    _include("clip_moment_queue_include", "the queue under the striped awning"),
    _include("clip_moment_butcher_include", "the butcher holding a cut of beef"),
    _include("clip_moment_cook_include", "the cook at the grill with his tongs"),
    _include("clip_moment_flowers_include", "the dried flowers hanging from the ceiling"),
    _include("clip_moment_michelin_include", "the two red Michelin plaques on the brick wall"),
]
PHOTO_INCLUDE = {
    "intent_id": "include_ribeye_photo",
    "op": "include",
    "attribute": "the ribeye photo",
    "source_quote": "show the ribeye photo full screen",
}
PHOTO_CAPTION = {
    "intent_id": "caption_ribeye_photo",
    "op": "caption",
    "attribute": "the ribeye photo",
    "creator_text": CAPTION,
    "caption_attribute": None,
    "source_quote": f'show the ribeye photo full screen with the text "{CAPTION}"',
}
# Turn 1's rows (its intent_stats: every moment, the photo, its caption; the queue n=0).
TURN1_ROWS = [*MOMENTS, PHOTO_INCLUDE, PHOTO_CAPTION]
# Turn 2's re-plan (diagnostics: kept=6, drop_classes=[creator_text_not_source_backed], the
# rejected row previewed as "label: the ribeye photo"): a label whose quote misses the text.
BAD_PHOTO_LABEL = {
    "intent_id": "label_ribeye_photo",
    "op": "label",
    "attribute": "the ribeye photo",
    "creator_text": CAPTION,
    "source_quote": "show the ribeye photo full screen",
}
TURN2_ROWS = [*MOMENTS, BAD_PHOTO_LABEL]
# A re-plan slip the parser can never repair (the model swapped the emoji): only reuse helps.
WRONG_EMOJI_LABEL = {
    **BAD_PHOTO_LABEL,
    "creator_text": "Ojo de bife 🍖",
    "source_quote": PHOTO_CAPTION["source_quote"],
}
TURN2_UNREPAIRABLE = [*MOMENTS, WRONG_EMOJI_LABEL]
# Turn 1's resolver: everything found except the queue (m001 = the video, m002 = the photo).
MATCHES = {
    "clip_moment_sign_include": ["m001"],
    "clip_moment_queue_include": [],
    "clip_moment_butcher_include": ["m001"],
    "clip_moment_cook_include": ["m001"],
    "clip_moment_flowers_include": ["m001"],
    "clip_moment_michelin_include": ["m001"],
    "include_ribeye_photo": ["m002"],
    "caption_ribeye_photo": ["m002"],
}


class _Models(ModelClient):
    """The provider boundary only. Each clip-intent planner call takes the next recorded answer;
    resolver calls get the recorded matches. The REAL agents render prompts and parse answers."""

    _RESOLVER_INTENT = re.compile(r"- intent_id=(\S+) \| op=")

    def __init__(
        self,
        monkeypatch: pytest.MonkeyPatch,
        plans: list[list[dict[str, Any]]],
        matches: dict[str, list[str]] | None = None,
    ) -> None:
        self.plans = list(plans)
        self.matches = MATCHES if matches is None else matches
        self.planner_calls = 0
        monkeypatch.setattr(planning, "default_client", lambda: self)
        monkeypatch.setattr(resolution, "default_client", lambda: self)

    def invoke(self, *, model: str, prompt: str, media_uri: str | None = None, **_: Any):
        assert media_uri is None, "no vision call is recorded for this run"
        asked = self._RESOLVER_INTENT.findall(prompt)
        if not asked:
            self.planner_calls += 1
            answer: dict[str, Any] = {"intents": self.plans.pop(0), "question": None}
        else:
            answer = {
                "intents": [
                    {
                        "intent_id": intent_id,
                        "assignments": [
                            {"media": handle, "confidence": 0.9, "evidence": "clip record"}
                            for handle in self.matches.get(intent_id, [])
                        ],
                    }
                    for intent_id in asked
                ]
            }
        return ModelInvocation(raw_text=json.dumps(answer), tokens_in=10, tokens_out=20)


class _Db:
    """The turn's direct reads: the asking turn's record (``turns``), nothing else."""

    def __init__(self, turns: dict[str, dict[str, Any]] | None = None) -> None:
        self.turns = turns or {}
        self.turn_reads = 0

    async def execute(self, statement: Any) -> Any:
        if "creator_agent_turns" in str(statement):
            self.turn_reads += 1
            wanted = {str(v) for v in statement.compile().params.values()}
            plan = next((p for t, p in self.turns.items() if t in wanted), None)
            return SimpleNamespace(scalar_one_or_none=lambda: plan)
        return SimpleNamespace(scalar_one_or_none=lambda: None)

    async def rollback(self) -> None:
        return None


def _agent_input(user_message: str) -> MainCreatorInput:
    data = copy.deepcopy(CASE["main_creator_input"])
    data["user_message"] = user_message
    data["creator_request"] = PROMPT
    data["conversation"] = [{"role": "user", "content": PROMPT}]
    manifest = data["capability_manifest"]
    manifest["media"] = [
        {"kind": "video", "label": None, "media_id": VIDEO, "duration_s": 136.9},
        {"kind": "image", "label": None, "media_id": PHOTO, "duration_s": None},
    ]
    return MainCreatorInput.model_validate(data)


def _clips() -> list[IntentClip]:
    return [
        IntentClip(
            media_id=VIDEO,
            kind="video",
            analysis={
                "subject": "a walk into Don Julio: window sign, queue, butcher with beef, "
                "cook at the grill, dried flowers, Michelin plaques, Atakan talking"
            },
            gcs_path=VIDEO,
        ),
        IntentClip(
            media_id=PHOTO,
            kind="image",
            analysis={"subject": "a ribeye steak on a wooden board"},
            gcs_path=PHOTO,
        ),
    ]


async def _turn(
    monkeypatch: pytest.MonkeyPatch,
    models: _Models,
    *,
    user_message: str,
    raw_request: str,
    events: list[tuple[str, dict[str, Any]]] | None = None,
    db: _Db | None = None,
) -> PlannedKriaTurn:
    """One M2 turn through the REAL planner (Main Creator output parsed, models replayed)."""
    monkeypatch.setattr(settings, "clip_intents_enabled", True)
    monkeypatch.setattr(settings, "kria_clip_selection_questions_enabled", True)
    monkeypatch.setattr(settings, "kria_choice_questions_enabled", True)
    monkeypatch.setattr(settings, "kria_clip_understanding_enabled", False)
    monkeypatch.setattr(type(planner.settings), "brief_binding_for", lambda _s, _i: True)
    monkeypatch.setattr(planner, "_load_thread_events", AsyncMock(return_value=events or []))
    monkeypatch.setattr(planner, "_kick_clip_understanding", AsyncMock())
    agent_input = _agent_input(user_message)
    strategy = {**copy.deepcopy(CASE["model_strategy"]), "clip_intents": [], "opening_title": None}
    raw = {
        "action": {"kind": "propose_strategy", "strategy": strategy, "summary": "Six moments."},
        "brief_updates": [],
    }
    output = MainCreatorAgent(model_client=None).parse(json.dumps(raw), agent_input)
    return await planner._plan_from_creator_output(
        db or _Db(),
        thread_id=uuid.uuid4(),
        item_id=uuid.UUID(agent_input.capability_manifest.item_id),
        creator_id=uuid.uuid4(),
        user_message=user_message,
        manifest=agent_input.capability_manifest,
        inputs=planner._CreatorInputs(
            agent_input=agent_input,
            intent_clips=_clips(),
            creator_request=None,
            raw_creator_request=raw_request,
            creative_copy_state={},
            creative_copy_digest="d" * 24,
        ),
        output=output,
    )


def _assistant(turn_id: str, plan: KriaTurnPlan) -> tuple[str, dict[str, Any]]:
    """The chat event the runtime writes for a reply (the fields it copies from the plan)."""
    payload = {
        "turn_id": turn_id,
        "turn_value": plan.turn_value,
        "receipt_ids": [],
        "next_actions": [],
        "schema_version": plan.schema_version,
        **({"clip_question": plan.clip_question} if plan.clip_question else {}),
    }
    return tag_event("assistant", payload, "assistant_response", plan.response)


def _tap(question: dict[str, Any], *, skipped: bool = False, **extra: Any):
    if skipped:
        selection: dict[str, Any] = {"question_id": question["question_id"], "skipped": True}
    else:
        selection = {
            "question_id": question["question_id"],
            "answers": [{"key": QUEUE_KEY, "media_ids": [VIDEO]}],
        }
    payload = {"runtime_version": 2, "clip_selection": selection, **extra}
    content = "Skip" if skipped else "The queue under the striped awning: clip 1"
    return tag_event("user", payload, "user_message", content)


async def _turn1(monkeypatch: pytest.MonkeyPatch, models: _Models) -> tuple[str, KriaTurnPlan]:
    planned = await _turn(monkeypatch, models, user_message=PROMPT, raw_request=PROMPT)
    plan = planned.plan
    assert plan.turn_value == "question" and plan.clip_question is not None, plan.response
    assert [c["key"] for c in plan.clip_question["categories"]] == [QUEUE_KEY]
    return str(uuid.uuid4()), plan


async def _ask_then_tap(
    monkeypatch: pytest.MonkeyPatch,
    *,
    answer_message: str = CLIP_ANSWER_MESSAGE,
    tap: Callable[[dict[str, Any]], tuple[str, dict[str, Any]]] = _tap,
    saved: Callable[[dict[str, Any]], Any] = lambda record: record,
    raw_request: str | None = None,
    replan: list[dict[str, Any]] = TURN2_UNREPAIRABLE,
    after_tap: list[tuple[str, dict[str, Any]]] = (),
) -> tuple[PlannedKriaTurn, _Models]:
    """Turn 1 asks about the queue; the creator taps; turn 2 plans (``replan`` = what a new
    planner call would answer: by default a slip only reuse can survive)."""
    models = _Models(monkeypatch, [TURN1_ROWS, replan])
    turn_id, plan1 = await _turn1(monkeypatch, models)
    events = [
        tag_event("user", {"runtime_version": 2}, "user_message", PROMPT),
        _assistant(turn_id, plan1),
        tap(plan1.clip_question),
        *after_tap,
    ]
    planned = await _turn(
        monkeypatch,
        models,
        user_message=answer_message,
        raw_request=raw_request or f"{PROMPT}\n{answer_message}",
        events=events,
        db=_Db({turn_id: saved(plan1.model_dump(mode="json"))}),
    )
    return planned, models


def _clip_intents(planned: PlannedKriaTurn) -> dict[str, dict[str, Any]]:
    assert planned.plan.mode == "act", planned.plan.response
    strategy = planned.plan.intents[0].arguments["strategy"]
    return {row["intent_id"]: row for row in strategy.get("resolved_clip_intents") or []}


# -- 1, 2: the journey ---------------------------------------------------------------------


async def test_the_tap_reuses_the_checked_list_and_keeps_the_photo_text(
    prod_profile, monkeypatch
) -> None:
    planned, models = await _ask_then_tap(monkeypatch, replan=TURN2_ROWS)

    assert planned.plan.response != RESTATE  # failure 1
    assert models.planner_calls == 1, "the tap turn must not ask the planner model again"
    rows = _clip_intents(planned)
    caption = rows["caption_ribeye_photo"]  # failure 2: verbatim, on the photo
    assert (caption["caption_text"], caption["caption_grounding"]) == (CAPTION, "creator_text")
    assert [a["media_id"] for a in caption["assignments"]] == [PHOTO]
    queue = rows["clip_moment_queue_include"]
    assert [a["media_id"] for a in queue["assignments"]] == [VIDEO]


async def test_reuse_survives_a_slip_the_parser_cannot_repair(prod_profile, monkeypatch) -> None:
    planned, models = await _ask_then_tap(monkeypatch)
    assert models.planner_calls == 1
    assert _clip_intents(planned)["caption_ribeye_photo"]["caption_text"] == CAPTION


async def test_red_control_without_a_saved_list_the_tap_asks_to_restate(
    prod_profile, monkeypatch
) -> None:
    """Failure 4 (asked before this deploy) is today's prod path: the re-plan asks to restate."""
    planned, models = await _ask_then_tap(
        monkeypatch,
        saved=lambda record: {k: v for k, v in record.items() if k != "clip_intent_inventory"},
    )
    assert models.planner_calls == 2
    assert planned.plan.response == RESTATE


async def test_without_a_saved_list_the_parser_repairs_the_recorded_slip(
    prod_profile, monkeypatch
) -> None:
    """The safety net alone: turn 2's recorded answer (the quote stops before the text)."""
    planned, models = await _ask_then_tap(
        monkeypatch,
        replan=TURN2_ROWS,
        saved=lambda record: {k: v for k, v in record.items() if k != "clip_intent_inventory"},
    )
    assert models.planner_calls == 2
    assert planned.plan.response != RESTATE


# -- 3, 4, 5, 6: when the list must NOT be reused ------------------------------------------


async def test_a_typed_correction_replans_from_the_new_words(prod_profile, monkeypatch) -> None:
    models = _Models(monkeypatch, [TURN1_ROWS, TURN1_ROWS])
    turn_id, plan1 = await _turn1(monkeypatch, models)
    typed = "Actually skip the queue."
    events = [
        tag_event("user", {"runtime_version": 2}, "user_message", PROMPT),
        _assistant(turn_id, plan1),
        tag_event("user", {"runtime_version": 2}, "user_message", typed),
    ]
    await _turn(
        monkeypatch,
        models,
        user_message=typed,
        raw_request=f"{PROMPT}\n{typed}",
        events=events,
        db=_Db({turn_id: plan1.model_dump(mode="json")}),
    )
    assert models.planner_calls == 2


async def test_a_list_saved_for_other_words_is_not_reused(prod_profile, monkeypatch) -> None:
    # The thread's words are not the ones the list was checked against (a re-send between).
    planned, models = await _ask_then_tap(
        monkeypatch, raw_request=f"{PROMPT}\n{PROMPT}\n{CLIP_ANSWER_MESSAGE}"
    )
    assert models.planner_calls == 2
    assert planned.plan.response == RESTATE  # today's behaviour, no worse


@pytest.mark.parametrize(
    "extra",
    [
        {"choice_selection": {"question_id": "q-title", "option_id": "use"}},
        {"moment_order": {"question_id": "q-moments", "moments": []}},
        {"protected_region": {"label": "plate", "x": 0.5, "y": 0.5}},
    ],
    ids=["choice_card", "moment_order", "mark"],
)
async def test_a_tap_with_another_answer_replans(prod_profile, monkeypatch, extra) -> None:
    _, models = await _ask_then_tap(monkeypatch, tap=lambda q: _tap(q, **extra))
    assert models.planner_calls == 2


@pytest.mark.parametrize(
    "broken",
    [
        {"version": 99, "key": "x", "output": {}},
        {"version": 1, "key": None, "output": {"intents": "nope"}},
        "not a dict",
    ],
    ids=["unknown_version", "bad_shape", "not_a_dict"],
)
async def test_a_broken_saved_list_is_ignored(prod_profile, monkeypatch, broken) -> None:
    planned, models = await _ask_then_tap(
        monkeypatch, saved=lambda record: {**record, "clip_intent_inventory": broken}
    )
    assert models.planner_calls == 2  # re-planned, nothing crashed
    assert planned.plan.response == RESTATE


# -- 7, 8, 9: other answers that are still only a tap --------------------------------------


async def test_skip_reuses_the_list_and_keeps_the_photo_text(prod_profile, monkeypatch) -> None:
    planned, models = await _ask_then_tap(
        monkeypatch, answer_message=CLIP_SKIP_MESSAGE, tap=lambda q: _tap(q, skipped=True)
    )
    assert models.planner_calls == 1
    assert planned.plan.response != RESTATE
    if planned.plan.mode == "act":
        assert _clip_intents(planned)["caption_ribeye_photo"]["creator_text"] == CAPTION


async def test_a_second_picker_in_a_row_reuses_the_list_again(prod_profile, monkeypatch) -> None:
    """Turn 2 (a tap) asks another picker; turn 3's tap reuses what turn 2 kept."""
    models = _Models(monkeypatch, [TURN1_ROWS, TURN2_ROWS, TURN2_ROWS])
    turn1, plan1 = await _turn1(monkeypatch, models)
    events = [
        tag_event("user", {"runtime_version": 2}, "user_message", PROMPT),
        _assistant(turn1, plan1),
        _tap(plan1.clip_question),
    ]
    # Turn 2's resolver does not find the photo, so turn 2 asks about it.
    models.matches = {**MATCHES, "include_ribeye_photo": [], "caption_ribeye_photo": []}
    planned2 = await _turn(
        monkeypatch,
        models,
        user_message=CLIP_ANSWER_MESSAGE,
        raw_request=f"{PROMPT}\n{CLIP_ANSWER_MESSAGE}",
        events=events,
        db=_Db({turn1: plan1.model_dump(mode="json")}),
    )
    plan2 = planned2.plan
    assert models.planner_calls == 1
    assert plan2.clip_question is not None, plan2.response
    turn2 = str(uuid.uuid4())
    events3 = [
        *events,
        _assistant(turn2, plan2),
        tag_event(
            "user",
            {
                "runtime_version": 2,
                "clip_selection": {
                    "question_id": plan2.clip_question["question_id"],
                    "answers": [
                        {"key": c["key"], "media_ids": [PHOTO]}
                        for c in plan2.clip_question["categories"]
                    ],
                },
            },
            "user_message",
            "The ribeye photo: photo 1",
        ),
    ]
    planned3 = await _turn(
        monkeypatch,
        models,
        user_message=CLIP_ANSWER_MESSAGE,
        raw_request=f"{PROMPT}\n{CLIP_ANSWER_MESSAGE}\n{CLIP_ANSWER_MESSAGE}",
        events=events3,
        db=_Db({turn1: plan1.model_dump(mode="json"), turn2: plan2.model_dump(mode="json")}),
    )
    assert models.planner_calls == 1, "turn 3's tap must reuse turn 2's list too"
    caption = _clip_intents(planned3)["caption_ribeye_photo"]
    assert caption["creator_text"] == CAPTION
    assert [a["media_id"] for a in caption["assignments"]] == [PHOTO]


async def test_a_server_event_after_the_tap_still_reuses(prod_profile, monkeypatch) -> None:
    ready = tag_event("assistant", {"job_id": "j1"}, "generation_ready", "Your video is ready.")
    review = tag_event("assistant", {"turn_value": "review"}, "assistant_review", "Looks good.")
    planned, models = await _ask_then_tap(monkeypatch, after_tap=[ready, review])
    assert models.planner_calls == 1
    assert _clip_intents(planned)["caption_ribeye_photo"]["caption_text"] == CAPTION


async def test_a_turkish_chat_tap_is_a_tap(prod_profile, monkeypatch) -> None:
    token = bind_reply_language("tr")
    try:
        planned, models = await _ask_then_tap(monkeypatch, answer_message=CLIP_ANSWER_MESSAGE_TR)
    finally:
        release_reply_language(token)
    assert models.planner_calls == 1
    assert _clip_intents(planned)["caption_ribeye_photo"]["creator_text"] == CAPTION


# -- 10, 11: where the list lives ----------------------------------------------------------


async def test_the_list_stays_on_the_server(prod_profile, monkeypatch) -> None:
    models = _Models(monkeypatch, [TURN1_ROWS])
    _, plan1 = await _turn1(monkeypatch, models)
    saved = plan1.model_dump(mode="json")["clip_intent_inventory"]
    assert saved["version"] == 1

    # The chat event the app downloads copies named fields only: the list is not one of them.
    _, payload = _assistant("t1", plan1)
    assert "clip_intent_inventory" not in payload
    assert CAPTION not in json.dumps(payload["clip_question"], ensure_ascii=False)

    # The admin turns view shows ids and ops, never the creator's words or the model's quotes.
    from app.routes.admin_creation_threads import _scrub_plan  # noqa: PLC0415

    shown = _scrub_plan(plan1.model_dump(mode="json"))["clip_intent_inventory"]
    assert shown["intents"][-1] == {"intent_id": "caption_ribeye_photo", "op": "caption"}
    text = json.dumps(shown, ensure_ascii=False)
    assert CAPTION not in text and "striped awning" not in text and "ribeye photo full" not in text


def test_a_plan_without_a_picker_has_no_new_key() -> None:
    plan = KriaTurnPlan(mode="respond", turn_value="question", response="Which song?")
    assert "clip_intent_inventory" not in plan.model_dump(mode="json")


# -- 12, 13: the parser safety net ----------------------------------------------------------


def _parse(row: dict[str, Any], request: str = PROMPT):
    raw = json.dumps({"intents": [*MOMENTS, row], "question": None}, ensure_ascii=False)
    agent = ClipIntentPlannerAgent(None)  # type: ignore[arg-type]
    return agent.parse(
        raw,
        ClipIntentPlannerInput(
            creator_request=request, latest_user_message=request, available_clip_count=2
        ),
    )


def _kept_text(output) -> str | None:
    row = next((i for i in output.intents if i.intent_id == "ribeye"), None)
    return None if row is None else row.creator_text


_RIBEYE = {"intent_id": "ribeye", "op": "label", "attribute": "the ribeye photo"}


@pytest.mark.parametrize(
    "quote",
    [
        "show the ribeye photo full screen",
        "Right after the butcher moment, show the ribeye photo full screen",
    ],
    ids=["quote_stops_before_the_text", "quote_from_the_start_of_the_sentence"],
)
def test_a_quote_that_misses_the_text_in_the_same_sentence_is_repaired(quote) -> None:
    output = _parse({**_RIBEYE, "creator_text": CAPTION, "source_quote": quote})
    assert output.salvage_question is None
    assert _kept_text(output) == CAPTION
    (row,) = [i for i in output.intents if i.intent_id == "ribeye"]
    assert CAPTION in row.source_quote and "ribeye photo" in row.source_quote


@pytest.mark.parametrize(
    "text",
    ["Ojo de bife 🍖", "OJO DE BIFE 🥩", "Bife de chorizo", "Ojo de bife 🥩 en Palermo"],
    ids=["other_emoji", "capitals", "not_creator_words", "longer_than_written"],
)
def test_text_that_is_not_the_creators_words_is_still_asked(text) -> None:
    output = _parse(
        {**_RIBEYE, "creator_text": text, "source_quote": PHOTO_CAPTION["source_quote"]}
    )
    assert output.salvage_reasons == ["creator_text_not_source_backed"]
    assert _kept_text(output) is None


def test_text_from_another_sentence_than_the_quote_is_still_asked() -> None:
    output = _parse(
        {**_RIBEYE, "creator_text": CAPTION, "source_quote": "the butcher holding a cut of beef"}
    )
    assert output.salvage_reasons == ["creator_text_not_source_backed"]


def test_another_instructions_text_in_the_same_sentence_is_still_asked() -> None:
    """The repair never hands one clip the text the creator wrote for another."""
    request = 'Label the butcher "Asado" and the cook "Parrilla". ' + PROMPT
    output = _parse(
        {
            "intent_id": "ribeye",
            "op": "label",
            "attribute": "the butcher",
            "creator_text": "Parrilla",
            "source_quote": "Label the butcher",
        },
        request=request,
    )
    assert output.salvage_reasons == ["creator_text_not_source_backed"]


def test_an_emoji_marker_the_model_added_prints_the_creators_form() -> None:
    output = _parse(
        {
            **_RIBEYE,
            "creator_text": CAPTION + "️",
            "source_quote": PHOTO_CAPTION["source_quote"],
        }
    )
    assert output.salvage_question is None
    assert _kept_text(output) == CAPTION


def test_an_emoji_marker_the_model_dropped_prints_the_creators_form() -> None:
    request = 'Put the text "Coffee time ☕️" on the café clip.'
    output = _parse(
        {
            "intent_id": "ribeye",
            "op": "caption",
            "attribute": "the café clip",
            "creator_text": "Coffee time ☕",
            "caption_attribute": None,
            "source_quote": 'Put the text "Coffee time ☕" on the café clip.',
        },
        request=request + " " + PROMPT,
    )
    assert output.salvage_question is None
    assert _kept_text(output) == "Coffee time ☕️"
