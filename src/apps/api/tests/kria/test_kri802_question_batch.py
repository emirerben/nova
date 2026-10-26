"""KRI-802: what Kria can already tell on the first turn is asked in that first turn.

Prod thread F4083F1D (Don Julio M1, 2026-10-21): the first reply asked only "Does this wording
work?" about the title (19:24). The red-cap clip picker came one full re-plan later (19:26),
although the same turn's clip checks would have found it: the title-wording gate in
``_plan_creator_action`` returned before them. Every answer re-runs the Main Creator (1-2 min), so
each serial question costs the creator another wait.

The fix (app protocol 14): for an app that answers a reply's cards together, the title-wording
question no longer ends the turn before the clip checks. When the clip checks end on a clip
picker, ONE reply carries both cards and the app sends both answers on one turn. Older builds and
the web keep one question per turn.

Ways this can fail, each pinned below (written before the code):
 1. An old build or the web gets two cards: each sends on its own and the other shows as answered
    with the wrong summary. Batching only for a message an app at protocol >= 14 sent.
 2. The held wording question is lost and the turn drafts with words the creator never approved
    (KRI-506 consent). Nothing else to ask = the wording question alone, no tool intent.
 3. Two choice cards in one reply: there is one ``choice_question`` slot, the second would
    silently replace the first.
 4. The wording card joined with a TEXT question (the "+" label): a typed answer cannot ride the
    tap, so the creator would be asked twice. The wording question goes alone, as before.
 5. A refusal, a song-order card or a "still checking" ending joined with the wording card. The
    wording question goes alone; a merged reply is never ``continuable`` (the runtime would re-run
    the turn while a question is on screen).
 6. The picker asks about the same words the title question asks about (a title read as a
    caption): one reply would ask for them twice, in two different ways.
 7. The merged reply exceeds the plan's 1200-character response limit: the turn cannot persist.
 8. Batch off: the title turn must stay byte-identical: no clip checks, no resolver call, same
    reply.
 9. Both titles model-written: still one wording question per turn (the opening title first).
10. One combined answer: a selection refused as stale, or only one of the two folded
    (``test_kri802_question_batch_postgres.py``).
11. The app-build stamp changes the request digest, so a resend under another build would not
    replay (``test_kri802_question_batch_postgres.py``).
12. Kill switch: a high minimum protocol stops the stamp without a deploy
    (``test_kri802_question_batch_postgres.py``).
13. Planning after the held question crashes: the turn would fail where it used to ask the
    title (found in review, before the guard was written).
"""

from __future__ import annotations

import copy
import json
import re
import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from app.agents._runtime import ModelClient, ModelInvocation
from app.agents.main_creator import MainCreatorAgent, MainCreatorInput
from app.config import settings
from app.kria import planner
from app.kria.contracts import KriaTurnPlan
from app.kria.planner import PlannedKriaTurn
from app.services import clip_intent_planning as planning
from app.services import clip_intent_resolution as resolution
from app.services.clip_intent_resolution import IntentClip

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "kri802_don_julio_m1_turn1.json"
CASE: dict[str, Any] = json.loads(FIXTURE.read_text())
MESSAGE: str = CASE["main_creator_input"]["user_message"]
TITLE = "Lunch at Don Julio \U0001f1e6\U0001f1f7"
RED_CAP = CASE["red_cap_media_id"]
WORDING_REPLY = (
    "I’d try “Lunch at Don Julio \U0001f1e6\U0001f1f7”. Does this wording work?\n"
    "Use this wording · Revise it · Skip it"
)


class _Models(ModelClient):
    """The provider boundary only: the clip-intent planner call gets the run's recorded asks and
    the resolver call gets the recorded matches (``m00N`` = the Nth clip). The REAL agents render
    their prompts and parse these answers; nothing else is faked."""

    _RESOLVER_INTENT = re.compile(r"- intent_id=(\S+) \| op=")

    def __init__(
        self,
        monkeypatch: pytest.MonkeyPatch,
        rows: list[dict[str, Any]],
        matches: dict[str, list[str]],
    ) -> None:
        self.rows = rows
        self.matches = matches
        self.resolved_intent_ids: list[str] = []
        monkeypatch.setattr(planning, "default_client", lambda: self)
        monkeypatch.setattr(resolution, "default_client", lambda: self)

    def invoke(self, *, model: str, prompt: str, media_uri: str | None = None, **_: Any):
        assert media_uri is None, "no vision call is recorded for this turn"
        asked = self._RESOLVER_INTENT.findall(prompt)
        if not asked:
            answer: dict[str, Any] = {"intents": self.rows, "question": None}
        else:
            self.resolved_intent_ids += asked
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
    """The turn's only direct reads (head draft) find nothing; the rest is mocked below."""

    async def execute(self, _statement: Any) -> Any:
        return SimpleNamespace(scalar_one_or_none=lambda: None)

    async def rollback(self) -> None:
        return None


def _clips() -> list[IntentClip]:
    return [
        IntentClip(
            media_id=m["media_id"],
            kind=m["kind"],
            analysis={"subject": CASE["clip_subjects"][m["media_id"]]},
            gcs_path=m["media_id"],
        )
        for m in CASE["main_creator_input"]["capability_manifest"]["media"]
    ]


async def _turn(
    monkeypatch: pytest.MonkeyPatch,
    *,
    batch: bool,
    strategy: dict[str, Any] | None = None,
    rows: list[dict[str, Any]] | None = None,
    matches: dict[str, list[str]] | None = None,
) -> tuple[PlannedKriaTurn, _Models]:
    """Chat 1's first turn through the REAL planner (Main Creator output parsed, models stubbed)."""

    monkeypatch.setattr(settings, "clip_intents_enabled", True)
    monkeypatch.setattr(settings, "kria_clip_selection_questions_enabled", True)
    monkeypatch.setattr(settings, "kria_choice_questions_enabled", True)
    monkeypatch.setattr(settings, "kria_clip_understanding_enabled", False)
    monkeypatch.setattr(type(planner.settings), "brief_binding_for", lambda _s, _i: True)
    monkeypatch.setattr(planner, "_load_thread_events", AsyncMock(return_value=[]))
    monkeypatch.setattr(planner, "_kick_clip_understanding", AsyncMock())
    agent_input = MainCreatorInput.model_validate(copy.deepcopy(CASE["main_creator_input"]))
    raw = {
        "action": {
            "kind": "propose_strategy",
            "strategy": strategy or copy.deepcopy(CASE["model_strategy"]),
            "summary": CASE["model_summary"],
        },
        "brief_updates": [],
    }
    output = MainCreatorAgent(model_client=None).parse(json.dumps(raw), agent_input)
    models = _Models(
        monkeypatch,
        CASE["clip_intent_rows"] if rows is None else rows,
        CASE["resolver_matches"] if matches is None else matches,
    )
    planned = await planner._plan_from_creator_output(
        _Db(),
        thread_id=uuid.uuid4(),
        item_id=uuid.UUID(agent_input.capability_manifest.item_id),
        creator_id=uuid.uuid4(),
        user_message=MESSAGE,
        manifest=agent_input.capability_manifest,
        inputs=planner._CreatorInputs(
            agent_input=agent_input,
            intent_clips=_clips(),
            creator_request=None,
            raw_creator_request=MESSAGE,
            creative_copy_state={},
            creative_copy_digest="d" * 24,
        ),
        output=output,
        **({"batch_questions": True} if batch else {}),
    )
    return planned, models


# The journey: chat 1's first turn ----------------------------------------------------------


async def test_first_turn_asks_the_title_wording_and_the_red_cap_picker_together(
    prod_profile, monkeypatch
) -> None:
    planned, models = await _turn(monkeypatch, batch=True)

    plan = planned.plan
    assert (plan.mode, plan.turn_value, plan.intents) == ("respond", "question", [])
    # The title card the run asked at 19:24 ...
    assert plan.choice_question["kind"] == "creative_copy_wording"
    assert plan.choice_question["creative_target"] == "opening_title"
    assert plan.choice_question["candidate"] == TITLE
    # ... and the red-cap picker it only asked a re-plan later (19:26), in the same reply.
    (category,) = plan.clip_question["categories"]
    assert category["intent_id"] == "order_friend_last"
    assert category["label"] == "my friend in the red cap smiling at the camera"
    assert RED_CAP in category["candidate_media_ids"]
    assert "order_friend_last" in models.resolved_intent_ids
    assert plan.response.startswith(WORDING_REPLY)
    assert "red cap" in plan.response
    assert plan.response.endswith("Answer both and I'll plan your edit.")
    assert plan.diagnostics["question_batch"] == "wording+clips"
    assert not planned.continuable
    # Persistable exactly as sent (the 1200-character limit and every field validate).
    assert KriaTurnPlan.model_validate(plan.model_dump(mode="json")) == plan


async def test_without_the_new_app_the_first_turn_is_unchanged(prod_profile, monkeypatch) -> None:
    planned, models = await _turn(monkeypatch, batch=False)

    plan = planned.plan
    assert plan.response == WORDING_REPLY
    assert plan.choice_question["candidate"] == TITLE
    assert plan.clip_question is None
    assert plan.diagnostics is None
    assert models.resolved_intent_ids == []  # no clip checks, no resolver call


async def test_nothing_else_to_ask_keeps_the_wording_question_alone_and_drafts_nothing(
    prod_profile, monkeypatch
) -> None:
    matches = {**CASE["resolver_matches"], "order_friend_last": ["m008"]}

    planned, models = await _turn(monkeypatch, batch=True, matches=matches)

    plan = planned.plan
    assert "order_friend_last" in models.resolved_intent_ids  # the clips did resolve ...
    assert plan.mode == "respond" and plan.intents == []  # ... but nothing is drafted
    assert plan.response == WORDING_REPLY
    assert plan.choice_question["candidate"] == TITLE
    assert plan.clip_question is None


async def test_a_label_with_a_plus_is_a_clip_question_that_joins_the_wording_card(
    prod_profile, monkeypatch
) -> None:
    # Chat 2's first question: the planner read the sauce text as a LABEL. Before KRI-826
    # a label could not print "+", so it was a text-only question asked by itself. Now
    # the "+" prints, and the only open question is which clips are the sauce bowls.
    rows = copy.deepcopy(CASE["clip_intent_rows"])
    rows[1] = {**rows[1], "intent_id": "label_sauce_bowls", "op": "label"}

    planned, _ = await _turn(monkeypatch, batch=True, rows=rows)

    plan = planned.plan
    assert plan.response.startswith(WORDING_REPLY)
    assert "can't show" not in plan.response
    assert plan.choice_question["candidate"] == TITLE
    assert plan.clip_question is not None
    assert "label_sauce_bowls" in {c["intent_id"] for c in plan.clip_question["categories"]}
    assert plan.diagnostics["question_batch"] == "wording+clips"


async def test_both_titles_model_written_still_asks_one_wording_question(
    prod_profile, monkeypatch
) -> None:
    strategy = {**copy.deepcopy(CASE["model_strategy"]), "closing_title": "Gracias, Buenos Aires"}

    planned, _ = await _turn(monkeypatch, batch=True, strategy=strategy)

    plan = planned.plan
    assert plan.choice_question["creative_target"] == "opening_title"
    assert plan.choice_question["candidate"] == TITLE
    assert "Gracias" not in plan.response
    assert plan.clip_question is not None


async def test_a_crash_after_the_held_question_still_asks_the_title(
    prod_profile, monkeypatch
) -> None:
    def _crash(*_args: Any, **_kwargs: Any) -> Any:
        raise RuntimeError("strategy check crashed")

    monkeypatch.setattr(planner, "check_strategy_for_runtime_v2", _crash)

    planned, _ = await _turn(monkeypatch, batch=True)

    assert planned.plan.response == WORDING_REPLY
    assert planned.plan.choice_question["candidate"] == TITLE


# The joining rule, over every ending the rest of planning can have -------------------------


def _wording() -> PlannedKriaTurn:
    return PlannedKriaTurn(
        plan=KriaTurnPlan(
            mode="respond",
            turn_value="question",
            response=WORDING_REPLY,
            choice_question={
                "kind": "creative_copy_wording",
                "question_id": "w",
                "candidate": TITLE,
                "options": [{"key": "approve"}, {"key": "revise"}],
            },
        ),
        manifest_hash="a" * 64,
        context_hash="b" * 64,
    )


def _picker(**update: Any) -> PlannedKriaTurn:
    plan = KriaTurnPlan(
        mode="respond",
        turn_value="question",
        response="I couldn't tell which of your clips show my friend in the red cap.",
        clip_question={
            "question_id": "c",
            "categories": [{"key": "k", "label": "my friend in the red cap", "op": "order"}],
        },
    )
    return PlannedKriaTurn(
        plan=plan.model_copy(update=update.pop("plan", {})),
        manifest_hash="a" * 64,
        context_hash="b" * 64,
        **update,
    )


@pytest.mark.parametrize(
    "ending",
    [
        # 3. another choice card (an order-vs-group conflict) already fills the one slot
        pytest.param({"plan": {"choice_question": {"question_id": "o"}}}, id="second-choice"),
        # 5. a "still checking" reply the runtime re-runs
        pytest.param({"continuable": True}, id="continuable"),
        # 5. a question answered by reply chips ("how long?")
        pytest.param({"plan": {"reply_options": ["10 s", "15 s"]}}, id="reply-chips"),
        # 6. the picker asks where the TITLE words go
        pytest.param(
            {
                "plan": {
                    "clip_question": {
                        "question_id": "c",
                        "categories": [
                            {
                                "key": "k",
                                "label": "the opening",
                                "creator_text": "Lunch at Don Julio",
                            }
                        ],
                    }
                }
            },
            id="title-words-in-picker",
        ),
        # 7. too long for one reply
        pytest.param({"plan": {"response": "x" * 1100}}, id="too-long"),
    ],
)
def test_the_wording_question_goes_alone_unless_a_plain_picker_ended_the_turn(ending) -> None:
    wording = _wording()

    assert planner._with_wording_question(wording, _picker(**ending)) is wording


@pytest.mark.parametrize(
    "plan",
    [
        # 2. a draft
        KriaTurnPlan(
            mode="act",
            turn_value="action",
            intents=[
                {
                    "intent_id": "i1",
                    "tool_name": "draft.apply_strategy",
                    "tool_version": 1,
                    "arguments": {},
                }
            ],
        ),
        # 4. a text-only question
        KriaTurnPlan(mode="respond", turn_value="question", response="What label instead?"),
        # 5. a refusal / recovery
        KriaTurnPlan(mode="respond", turn_value="recovery", response="I couldn't match that."),
    ],
    ids=["draft", "text-question", "recovery"],
)
def test_no_picker_means_the_wording_question_alone(plan: KriaTurnPlan) -> None:
    wording = _wording()
    other = PlannedKriaTurn(plan=plan, manifest_hash="a" * 64, context_hash="b" * 64)

    assert planner._with_wording_question(wording, other) is wording


def test_a_plain_picker_joins_and_keeps_both_cards() -> None:
    merged = planner._with_wording_question(_wording(), _picker())

    assert merged.plan.choice_question["question_id"] == "w"
    assert merged.plan.clip_question["question_id"] == "c"
    assert merged.plan.response.split("\n\n") == [
        WORDING_REPLY,
        "I couldn't tell which of your clips show my friend in the red cap.",
        "Answer both and I'll plan your edit.",
    ]
