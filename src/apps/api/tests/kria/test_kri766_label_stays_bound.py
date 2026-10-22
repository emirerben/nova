"""KRI-766: a dictated shot label ("Oblea de la casa" on the plate-to-camera clip) must
survive a turn that does not mention it, and must ask with the clip picker when nobody has
placed it.

Prod thread A9100235 (Merope café M1, 2026-10-19): draft 1 resolved the label. The next
message only re-ordered ("don't use filming order, first = the tray clip, last = bye bye").
The clip-intent planner re-inventories the thread every turn and did not emit the label
again, so the new strategy had no placement for a text the brief still demands; the
backstop answered 'I couldn't tell which clip "Oblea de la casa" goes on ... draft
unchanged' with no picker and no buttons.

Ways this can fail, each pinned below (written before the code):
 1. Order-only turn, the planner omits the label -> dead-end text instead of a picker.
 2. The omitted label is unresolved by the resolver -> must be a `clip_question` picker.
 3. The planner re-emits the label (case/accents differ) -> it is added a second time.
 4. A positional `shot_labels` row already carries the words -> a second placement is asked.
 5. The creator names the label again this turn -> the stale binding wins over their words.
 6. A bound clip left the project -> the label is pinned on a clip that is gone.
 7. The creator tapped a (newer) picker answer -> the old binding wins over the tap.
 8. A kept label re-runs the resolver and can land on a different clip than last turn.
 9. Nothing dictated, no draft -> any change in behaviour (must be byte-identical).
10. Only resolved label rows of the draft are read; unresolved/other-op rows never bind.
11. The creator talks about the label without typing all of it ("the Oblea label goes ...",
    "move the label") -> the old binding must not decide.
12. A dictated text a label can never print (long) becomes a new "label too long" question.
13. The planner carried the same words as a caption -> a second label prints them twice.
14. A model-written per-clip value, or a text timed to a spoken word, binds / becomes a clip ask.
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

from app.agents.clip_intent_planner import ClipIntentPlannerAgent
from app.agents.clip_request_resolver import (
    ClipRequestResolverAgent,
    ClipRequestResolverOutput,
    ResolverIntentOut,
)
from app.agents.main_creator import MainCreatorAgent, MainCreatorInput
from app.config import settings
from app.kria import planner
from app.kria.brief import BriefRequirement, CreativeBrief
from app.schemas.clip_intents import ClipIntent
from app.services import clip_intent_planning as planning
from app.services.clip_intent_planning import (
    BriefLabel,
    KeptLabel,
    brief_label_asks,
    kept_labels_from_strategy,
    plan_and_resolve_clip_intents,
)
from app.services.clip_intent_resolution import IntentClip
from app.services.clip_selection import ClipSelections, SelectionEntry, intent_key
from app.services.creator_render_contract import build_render_contract

LABEL = "Oblea de la casa"
DESCRIPTION = "Tabağı kameraya uzattığı klip"
INTRO = "analysis-proxy-ios-62514CA5.mp4"  # M1_06, her intro: says the dish name
PLATE = "analysis-proxy-ios-9E2A6CEB.mp4"  # M1_09, the plate pushed to the lens
BYE = "analysis-proxy-ios-2F878362.mp4"  # M1_03

PROMPT = (
    'Tabağı kameraya uzattığı klibe "Oblea de la casa" etiketi koy. 20 saniye. '
    "Çekim sırasını kullanma. İlk klip tepsiyle geldiği klip, son klip bye bye olsun."
)
ORDER_ONLY_MESSAGE = "Çekim sırasını kullanma. İlk klip tepsiyle geldiği klip, aradakileri sen diz."

LABEL_ASK = BriefLabel(literal=LABEL, description=DESCRIPTION)
SHOT_TEXT = BriefRequirement(
    id="r6", kind="text", scope="per_clip", literal=LABEL, description=DESCRIPTION
)


def _clips() -> list[IntentClip]:
    return [
        IntentClip(media_id=m, kind="video", analysis={"subject": s}, gcs_path=f"u/{m}")
        for m, s in (
            (INTRO, "presenting a food dish; And this is the Oblea de la casa"),
            (PLATE, "presents a plate"),
            (BYE, "laughing and waving goodbye"),
        )
    ]


def _label_row(text: str = LABEL, attribute: str = DESCRIPTION) -> dict[str, Any]:
    return {
        "intent_id": "label_oblea",
        "op": "label",
        "attribute": attribute,
        "creator_text": text,
        "source_quote": '"Oblea de la casa" etiketi',
    }


class _Models:
    """The REAL clip-intent parser and resolver over recorded output; nothing is asked."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, planner_rows: list[dict[str, Any]]):
        self.resolved_intent_ids: list[str] = []
        outer = self

        def _plan(self, input, *, ctx=None):  # noqa: A002, ANN001
            return self.parse(json.dumps({"intents": planner_rows, "question": None}), input)

        def _resolve(self, input, *, ctx=None):  # noqa: A002, ANN001
            outer.resolved_intent_ids += [i.intent_id for i in input.intents]
            # Nothing matched: an honest empty answer for every intent.
            return ClipRequestResolverOutput(
                intents=[ResolverIntentOut(intent_id=i.intent_id) for i in input.intents]
            )

        monkeypatch.setattr(planning, "default_client", MagicMock())
        monkeypatch.setattr(ClipIntentPlannerAgent, "run", _plan)
        monkeypatch.setattr(ClipRequestResolverAgent, "run", _resolve)


@pytest.fixture(autouse=True)
def _flags(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "clip_intents_enabled", True)
    monkeypatch.setattr(settings, "kria_clip_selection_questions_enabled", True)
    monkeypatch.setattr(settings, "kria_clip_understanding_enabled", False)


async def _plan(models_rows: list[dict], monkeypatch, *, message=ORDER_ONLY_MESSAGE, **kwargs):  # noqa: ANN001, ANN003
    models = _Models(monkeypatch, models_rows)
    planned = await plan_and_resolve_clip_intents(
        creator_request=f"{PROMPT}\n{message}",
        latest_user_message=message,
        candidate_intents=None,
        clips=_clips(),
        run_context=MagicMock(),
        **kwargs,
    )
    return models, planned


# 1 + 2: the order-only turn ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_label_the_planner_dropped_is_asked_with_the_clip_picker(monkeypatch):
    models, planned = await _plan([], monkeypatch, brief_labels=[LABEL_ASK])

    # The omitted label went through the resolver like any other label ...
    assert models.resolved_intent_ids == ["label-oblea-de-la-casa"]
    # ... and, unresolved, it is the existing tappable picker, never a bare refusal.
    assert planned.resolution.status == "needs_creator"
    question = planned.resolution.clip_question
    assert question is not None
    (category,) = question["categories"]
    assert (category["op"], category["creator_text"], category["label"]) == (
        "label",
        LABEL,
        DESCRIPTION,
    )
    assert PLATE in category["candidate_media_ids"]
    # The inventory the strategy carries now includes the label, so the tap has a target.
    assert [(i.op, i.creator_text) for i in planned.requested_intents] == [("label", LABEL)]


# 3: no duplicates ------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_label_the_planner_did_emit_is_not_added_twice(monkeypatch):
    # The planner quotes the creator's later spelling; same words, different case.
    message = 'Bir de "oblea de la Casa" etiketi kalsın.'
    row = {**_label_row("oblea de la Casa"), "source_quote": '"oblea de la Casa" etiketi'}
    models, planned = await _plan([row], monkeypatch, message=message, brief_labels=[LABEL_ASK])

    assert models.resolved_intent_ids == ["label_oblea"]
    assert [i.intent_id for i in planned.requested_intents] == ["label_oblea"]


# 4: brief parsing -----------------------------------------------------------------------


def test_brief_label_asks_reads_only_live_dictated_shot_texts():
    superseded = BriefRequirement(
        id="r7",
        kind="text",
        scope="per_clip",
        literal="Old",
        description="old clip",
        status="superseded",
    )
    title = BriefRequirement(id="r1", kind="text", scope="title", literal="Tatlı", description="x")
    every_clip = BriefRequirement(id="r2", kind="text", scope="per_clip", literal="Hi")
    brief = CreativeBrief(version=1, requirements=[title, SHOT_TEXT, superseded, every_clip])

    assert brief_label_asks(brief, placed_texts=()) == [LABEL_ASK]
    # Positional shot_labels already carry the words (the contract pins that row).
    assert brief_label_asks(brief, placed_texts=["oblea de la CASA"]) == []
    assert brief_label_asks(None, placed_texts=()) == []
    # A text timed to a spoken word is a moment in the voiceover, never a clip.
    assert brief_label_asks(brief, placed_texts=(), spoken_text=[LABEL]) == []


@pytest.mark.asyncio
async def test_words_a_label_can_never_print_are_not_added_as_a_new_question(monkeypatch):
    # 120-char positional shot labels are legal; a label intent allows far fewer. Adding it
    # as a label would turn valid dictated text into a "label too long" question for the
    # creator. KRI-804: leaving it out dead-ended the turn on "which clip" instead, so it
    # joins as the shot's caption, which prints the creator's words as written.
    long_text = "Where every single crumb tells a whole story about the house dessert"
    models, planned = await _plan(
        [], monkeypatch, brief_labels=[BriefLabel(literal=long_text, description=DESCRIPTION)]
    )

    assert [(i.op, i.creator_text) for i in planned.requested_intents] == [("caption", long_text)]
    # ... resolved (or asked with the picker) like any described shot.
    assert models.resolved_intent_ids == [planned.requested_intents[0].intent_id]
    assert planned.resolution.error_code != "label_text_unusable"


@pytest.mark.asyncio
async def test_a_caption_the_planner_wrote_for_the_same_words_is_not_labelled_twice(monkeypatch):
    caption = {
        "intent_id": "caption_oblea",
        "op": "caption",
        "attribute": DESCRIPTION,
        "creator_text": LABEL,
        "source_quote": '"Oblea de la casa" etiketi',
    }
    models, planned = await _plan([caption], monkeypatch, brief_labels=[LABEL_ASK])

    assert models.resolved_intent_ids == ["caption_oblea"]
    assert [i.op for i in planned.requested_intents] == ["caption"]


# 5-8: a bound label stays bound ----------------------------------------------------------


@pytest.mark.asyncio
async def test_a_label_bound_in_the_draft_stays_on_the_same_clip_without_the_resolver(
    monkeypatch,
):
    kept = [KeptLabel(text=LABEL, media_ids=(PLATE,))]
    models, planned = await _plan([], monkeypatch, brief_labels=[LABEL_ASK], kept_labels=kept)

    assert models.resolved_intent_ids == []  # not re-decided, so it cannot move
    assert planned.resolution.status == "resolved"
    assert planned.resolution.clip_question is None
    (intent,) = planned.resolution.intents
    assert (intent.op, intent.status, intent.creator_text) == ("label", "resolved", LABEL)
    assert [(a.media_id, a.value) for a in intent.assignments] == [(PLATE, LABEL)]
    assert intent.assignments[0].evidence == "kept from your draft"


@pytest.mark.asyncio
async def test_the_kept_binding_also_covers_a_label_the_planner_did_emit(monkeypatch):
    kept = [KeptLabel(text=LABEL, media_ids=(PLATE,))]
    models, planned = await _plan([_label_row()], monkeypatch, kept_labels=kept)

    assert models.resolved_intent_ids == []
    (intent,) = planned.resolution.intents
    assert [a.media_id for a in intent.assignments] == [PLATE]


@pytest.mark.asyncio
async def test_naming_the_label_again_is_a_change_and_resolves_fresh(monkeypatch):
    kept = [KeptLabel(text=LABEL, media_ids=(INTRO,))]
    message = "Oblea de la casa etiketini tabağı kameraya yaklaştırdığı klibe koy"
    models, planned = await _plan(
        [_label_row()], monkeypatch, message=message, brief_labels=[LABEL_ASK], kept_labels=kept
    )

    assert models.resolved_intent_ids == ["label_oblea"]  # the resolver decides again
    assert planned.resolution.status == "needs_creator"  # nothing matched -> picker


@pytest.mark.asyncio
async def test_a_binding_whose_clip_left_the_project_is_not_kept(monkeypatch):
    kept = [KeptLabel(text=LABEL, media_ids=("analysis-proxy-ios-GONE.mp4",))]
    models, planned = await _plan([], monkeypatch, brief_labels=[LABEL_ASK], kept_labels=kept)

    assert models.resolved_intent_ids == ["label-oblea-de-la-casa"]
    assert planned.resolution.clip_question is not None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "message",
    [
        "Hayır, Oblea etiketi sosu döktüğü klibe gitsin",  # only part of the words
        "Etiketi başka klibe taşı",  # no words of it, but it is about labels
        "move the label to the other clip",
    ],
)
async def test_a_message_about_the_label_without_its_full_words_is_still_a_change(
    monkeypatch, message
):
    kept = [KeptLabel(text=LABEL, media_ids=(INTRO,))]
    models, planned = await _plan(
        [_label_row()], monkeypatch, message=message, brief_labels=[LABEL_ASK], kept_labels=kept
    )

    assert models.resolved_intent_ids == ["label_oblea"]  # the old placement does not decide
    assert planned.resolution.status == "needs_creator"


@pytest.mark.asyncio
async def test_a_newer_tapped_answer_wins_over_the_kept_binding(monkeypatch):
    kept = [KeptLabel(text=LABEL, media_ids=(INTRO,))]
    key = intent_key(ClipIntent(intent_id="x", op="label", attribute=DESCRIPTION))
    selections = ClipSelections(by_key={key: SelectionEntry(media_ids=(BYE,), seq=2)})
    models, planned = await _plan(
        [_label_row()], monkeypatch, kept_labels=kept, clip_selections=selections
    )

    assert models.resolved_intent_ids == []
    (intent,) = planned.resolution.intents
    assert [a.media_id for a in intent.assignments] == [BYE]
    assert intent.assignments[0].evidence == "creator selected"


# 9: untouched when nothing is dictated ----------------------------------------------------


@pytest.mark.asyncio
async def test_without_dictated_labels_or_a_draft_nothing_changes(monkeypatch):
    models, planned = await _plan([], monkeypatch)

    assert models.resolved_intent_ids == []
    assert planned.requested_intents == []
    assert planned.resolution.intents == []
    assert planned.resolution.clip_question is None


# 10: reading the draft ------------------------------------------------------------------


def test_only_resolved_label_rows_with_clips_bind():
    strategy = {
        "resolved_clip_intents": [
            {
                "op": "label",
                "status": "resolved",
                "intent_id": "a",
                "attribute": "x",
                "creator_text": LABEL,
                "assignments": [{"media_id": PLATE, "value": LABEL}],
            },
            {
                "op": "label",
                "intent_id": "b",
                "attribute": "y",
                "creator_text": "Atakan'ın ilk ısırığı",
                "assignments": [{"media_id": BYE}],
            },
            {  # a per-clip value the model wrote is not words the creator chose
                "op": "label",
                "status": "resolved",
                "intent_id": "f",
                "attribute": "each dish",
                "assignments": [{"media_id": INTRO, "value": "Waffle"}],
            },
            {  # unresolved: nobody placed it, nothing to keep
                "op": "label",
                "status": "needs_creator",
                "intent_id": "c",
                "attribute": "z",
                "creator_text": "Nope",
                "assignments": [{"media_id": INTRO, "value": "Nope"}],
            },
            {
                "op": "order",
                "status": "resolved",
                "intent_id": "d",
                "attribute": "first",
                "assignments": [{"media_id": INTRO}],
            },
            {
                "op": "label",
                "status": "resolved",
                "intent_id": "e",
                "attribute": "e",
                "creator_text": "Empty",
                "assignments": [],
            },
        ]
    }

    assert kept_labels_from_strategy(strategy) == [
        KeptLabel(text=LABEL, media_ids=(PLATE,)),
        KeptLabel(text="Atakan'ın ilk ısırığı", media_ids=(BYE,)),
    ]
    assert kept_labels_from_strategy(None) == []
    assert kept_labels_from_strategy({"resolved_clip_intents": "junk"}) == []


# The journey: the order-only turn through the REAL planner and render contract ---------------

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "kri593_tigre_voiceover_turn.json"
CASE: dict[str, Any] = json.loads(FIXTURE.read_text())
MANIFEST = CASE["main_creator_input"]["capability_manifest"]
KITCHEN = next(m for m, s in CASE["clip_subjects"].items() if s == "kitchen interior")
TIGRE_LABEL = "Evin tatlısı"
TIGRE_BRIEF = CreativeBrief(
    version=2,
    requirements=[
        BriefRequirement(
            id="r4",
            kind="text",
            scope="per_clip",
            literal=TIGRE_LABEL,
            description="mutfaktaki klip",
        )
    ],
)
ASK = '"Evin tatlısı" yazısı mutfaktaki klibe gelsin.'
ORDER_MESSAGE = "Çekim sırasını kullanma, son klip nehir olsun."


class _Db:
    """The one query the label step runs: the item's head draft snapshot."""

    def __init__(self, snapshot: dict[str, Any] | None) -> None:
        self.snapshot = snapshot
        self.rollbacks = 0

    async def execute(self, _statement: Any) -> Any:
        return SimpleNamespace(scalar_one_or_none=lambda: self.snapshot)

    async def rollback(self) -> None:
        self.rollbacks += 1


def _head_draft(media_id: str) -> dict[str, Any]:
    return {
        "kind": "strategy",
        "strategy": {
            "resolved_clip_intents": [
                {
                    "op": "label",
                    "status": "resolved",
                    "intent_id": "label_evin_tatlisi",
                    "attribute": "mutfaktaki klip",
                    "creator_text": TIGRE_LABEL,
                    "assignments": [{"media_id": media_id, "value": TIGRE_LABEL}],
                }
            ]
        },
    }


async def _order_only_turn(monkeypatch: pytest.MonkeyPatch, db: _Db):
    data = copy.deepcopy(CASE["main_creator_input"])
    data["user_message"] = ORDER_MESSAGE
    data["conversation"] = [
        data["conversation"][0],
        {"role": "user", "content": f"{ASK}\n{ORDER_MESSAGE}"},
    ]
    agent_input = MainCreatorInput.model_validate(data)
    strategy = {
        **copy.deepcopy(CASE["model_strategy"]),
        "clip_intents": [],
        "opening_title": None,
    }
    raw = {
        "action": {
            "kind": "propose_strategy",
            "strategy": strategy,
            "summary": CASE["model_summary"],
        },
        "brief_updates": [],
    }
    output = MainCreatorAgent(model_client=None).parse(json.dumps(raw), agent_input)
    _Models(monkeypatch, [])  # the inventory planner returns nothing for an order-only message
    monkeypatch.setattr(planner, "_load_thread_events", AsyncMock(return_value=[]))
    monkeypatch.setattr(planner, "_kick_clip_understanding", AsyncMock())
    monkeypatch.setattr(type(planner.settings), "brief_binding_for", lambda _s, _i: True)
    clips = [
        IntentClip(
            media_id=m["media_id"], kind=m["kind"], analysis={"subject": s}, gcs_path=m["media_id"]
        )
        for m in MANIFEST["media"]
        for s in [CASE["clip_subjects"][m["media_id"]]]
    ]
    return await planner._plan_from_creator_output(
        db,
        thread_id=uuid.uuid4(),
        item_id=uuid.UUID(MANIFEST["item_id"]),
        creator_id=uuid.uuid4(),
        user_message=ORDER_MESSAGE,
        manifest=agent_input.capability_manifest,
        inputs=planner._CreatorInputs(
            agent_input=agent_input,
            intent_clips=clips,
            creator_request=None,
            raw_creator_request=f"{ASK}\n{ORDER_MESSAGE}",
        ),
        output=output,
        brief_request=CASE["main_creator_input"]["creator_request"],
        effective_brief=TIGRE_BRIEF,
    )


async def test_order_only_turn_keeps_the_label_and_the_render_contract_has_no_gap(
    prod_profile, monkeypatch
) -> None:
    db = _Db(_head_draft(KITCHEN))

    planned = await _order_only_turn(monkeypatch, db)

    assert planned.plan.mode == "act", planned.plan.response
    strategy = planned.plan.intents[0].arguments["strategy"]
    (label,) = [i for i in strategy["resolved_clip_intents"] if i["op"] == "label"]
    assert [a["media_id"] for a in label["assignments"]] == [KITCHEN]
    # The symptom itself: the backstop ('I couldn't tell which clip ... goes on') stays quiet.
    contract = build_render_contract(strategy, generation_id="g", brief=TIGRE_BRIEF)
    assert contract is not None and not contract.unresolved
    assert [(t.role, t.text, t.media_id) for t in contract.exact_texts] == [
        ("clip", TIGRE_LABEL, KITCHEN)
    ]
    assert db.rollbacks == 1  # no transaction held across the model calls


async def test_order_only_turn_without_a_draft_asks_with_the_picker_not_a_dead_end(
    prod_profile, monkeypatch
) -> None:
    planned = await _order_only_turn(monkeypatch, _Db(None))

    assert planned.plan.mode == "respond"
    assert planned.plan.clip_question is not None
    (category,) = planned.plan.clip_question["categories"]
    assert (category["op"], category["creator_text"]) == ("label", TIGRE_LABEL)
    assert category["label"] == "mutfaktaki klip"


# The draft query -------------------------------------------------------------------------


async def test_load_kept_labels_reads_the_head_draft_and_releases_the_transaction() -> None:
    db = _Db(_head_draft(KITCHEN))
    assert await planner._load_kept_labels(db, uuid.uuid4()) == [
        KeptLabel(text=TIGRE_LABEL, media_ids=(KITCHEN,))
    ]
    assert db.rollbacks == 1
    # No draft yet, or an editor draft that carries no strategy: nothing is kept.
    assert await planner._load_kept_labels(_Db(None), uuid.uuid4()) == []
    assert await planner._load_kept_labels(_Db({"kind": "editor"}), uuid.uuid4()) == []
