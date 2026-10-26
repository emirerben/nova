"""KRI-945: the Cunda Island chat, replayed through the real runtime.

Prod thread e3e91b35 (2026-10-26): a montage of 11 short videos and 10 pool photos. The
creator asked for "some facts about Cunda ... an introductory explanation" and "a hook about
it being a must visit spot". Seven turns, no draft. Approving the revised hook got back
"What should the caption on the Cunda Island introductory explanation say?", and once the
creator typed the facts the draft was held over both texts.

Real: ``submit_turn`` + ``run_kria_turn`` over scratch Postgres, so the planner, the brief
store, the creative-copy fold over persisted events, clip-intent planning and resolution, the
choice gate, research preview, brief checks and approval readiness run as in prod. Only the
provider transport is recorded (``tests/fixtures/kri945_cunda_island_turns.json``): the Main
Creator, clip-intent planner and resolver answers go through the real agents' parsers, the
grounded search / copy / review answers through the real research agents. The resolver's one
vision re-query (a GCS download plus a Gemini file upload) answers from the fixture. Prod LLM
I/O is redacted, so every answer is authored from the current prompts.

Approval readiness is on: prod's T5 hold-back said "I haven't established how to render this
change yet", which only ``approval_readiness`` writes. ``app.kria.replay`` was not usable: it
replays a fixed ``KriaTurnPlan`` and never runs the planner, where this bug lives.

Ways this goes wrong, each pinned below:
1. Approving the hook ends on a question about a caption for the whole-video explanation, so
   no draft is made (prod T3, T4 and T6).
2. The approved hook never reaches the brief's title requirement, so the title can't be
   proven before render.
3. The researched explanation is never researched before approval: research preview skips
   every readiness creator, and no draft check can prove a research-only text.
4. Typed facts are held as "That exact text isn't in this draft" although the montage render
   puts a whole-video literal at the opening. A writer that can't draw it (the spoken-excerpt
   montage) must still hold it back.
5. Keeping whole-video text out of clip captions must not drop a real per-clip caption ask.
"""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import re
import uuid
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import select

from app.agents._runtime import ModelClient, ModelInvocation
from app.agents.clip_question import ClipQuestionOutput
from app.config import settings
from app.database import AsyncSessionLocal, sync_session
from app.database import engine as async_engine
from app.kria import research_preview
from app.kria.api_schemas import ApprovalDecisionBody, ChoiceSelectionIn, SubmitTurnBody
from app.kria.brief import CreativeBrief, load_latest_brief_sync
from app.kria.drafts import KriaDraftDocument
from app.kria.planner import PlannedKriaTurn
from app.kria.runtime import approval_fingerprint, decide_approval, submit_turn
from app.models import (
    CreationThread,
    CreationThreadEvent,
    CreatorAgentApproval,
    CreatorEditDraft,
    PlanItem,
    PlanItemAsset,
)
from app.services import clip_intent_resolution as resolution
from app.services import researched_text
from app.services.creator_render_contract import build_render_contract
from app.tasks import kria_runtime
from app.tasks.kria_runtime import run_kria_turn
from tests._prod_profile import apply_prod_profile
from tests.kria.test_kri775_merope_m2_replay import FIXTURE as MEROPE
from tests.kria.test_runtime_postgres_integration import _seed_runtime_project

CASE: dict[str, Any] = json.loads(
    (Path(__file__).resolve().parents[1] / "fixtures" / "kri945_cunda_island_turns.json").read_text(
        encoding="utf-8"
    )
)
MESSAGES = CASE["messages"]
APPROVED = CASE["hook"]["approved"]
FACTS_TEXT = MESSAGES["t5"]
INTRO = CASE["intro_caption"]
BUTCHER = CASE["butcher_caption"]
SOURCED_TEXT = CASE["research"]["caption"]["text"]
SPEAKER_ID = f"analysis-proxy-ios-{CASE['speaker']['uuid']}.mp4"
BUTCHER_ID = f"analysis-proxy-ios-{CASE['clips'][-1]['uuid']}.mp4"
CAPTION_ASK = "What should the caption"
LITERAL_MISS = "That exact text isn't in this draft."


@pytest.fixture(autouse=True)
def _flags(monkeypatch: pytest.MonkeyPatch) -> None:
    apply_prod_profile(monkeypatch)
    monkeypatch.setattr(settings, "kria_brief_binding_enabled", True)
    monkeypatch.setattr(settings, "kria_approval_readiness_enabled", True)
    monkeypatch.setattr(settings, "kria_choice_questions_enabled", True)
    monkeypatch.setattr(settings, "kria_clip_selection_questions_enabled", True)
    monkeypatch.setattr(settings, "kria_research_preview_enabled", True)
    memory = _MemoryRedis()
    monkeypatch.setattr(research_preview, "_redis", lambda: memory)


@pytest_asyncio.fixture(autouse=True)
async def _dispose_async_engine():
    yield
    await async_engine.dispose()


class _MemoryRedis:
    """The research cache's few Redis calls, in memory per test (the real cache code runs)."""

    def __init__(self) -> None:
        self.data: dict[str, bytes] = {}

    def get(self, key: str) -> bytes | None:
        return self.data.get(key)

    def set(self, key: str, value: Any, ex: int | None = None, nx: bool = False) -> bool | None:  # noqa: ARG002
        if nx and key in self.data:
            return None
        self.data[key] = value.encode() if isinstance(value, str) else value
        return True

    def exists(self, key: str) -> int:
        return int(key in self.data)

    def delete(self, key: str) -> int:
        return int(self.data.pop(key, None) is not None)


# -- recorded provider transport ------------------------------------------------------------

_CLIP_LINE = re.compile(r"^- (m\d+) \((\w+)\): (\{.*\})$", re.MULTILINE)
_ASKED_INTENT = re.compile(r"- intent_id=(\S+) \| op=")


def _grounding(facts: list[list[str]]) -> SimpleNamespace:
    """A grounded-search response: each (fact, url, title) supported by its own chunk."""
    return SimpleNamespace(
        candidates=[
            SimpleNamespace(
                grounding_metadata=SimpleNamespace(
                    grounding_chunks=[
                        SimpleNamespace(web=SimpleNamespace(uri=url, title=title))
                        for _text, url, title in facts
                    ],
                    grounding_supports=[
                        SimpleNamespace(
                            segment=SimpleNamespace(text=text), grounding_chunk_indices=[index]
                        )
                        for index, (text, _url, _title) in enumerate(facts)
                    ],
                    search_entry_point=SimpleNamespace(rendered_content="<div>Cunda</div>"),
                )
            )
        ]
    )


def _research_copy(prompt: str) -> dict[str, Any]:
    data = json.loads(prompt.split("Data:\n", 1)[1])
    window = next(row for row in data["snapshot"]["research_windows"] if len(row["media_ids"]) == 1)
    caption = CASE["research"]["caption"]
    return {
        "captions": [
            {
                "text": caption["text"],
                "window_id": window["id"],
                "media_ids": [],
                "evidence_ids": caption["evidence_ids"],
                "visual_quote": str(window["seen"][0].get("text") or "")[:30],
                "scope": "context",
                "context_label": caption["context_label"],
                "subject_name": "",
                "identity_quote": "",
            }
        ]
    }


@dataclass
class _Models(ModelClient):
    """Each agent's prompt answered with this turn's recorded output; every call counted."""

    turn: dict[str, Any] = field(default_factory=dict)
    calls: Counter = field(default_factory=Counter)
    planned: list[PlannedKriaTurn] = field(default_factory=list)

    def invoke(self, *, model: str, prompt: str, **_: Any) -> ModelInvocation:  # noqa: ARG002
        if prompt.startswith("Research this public topic"):
            self.calls["search"] += 1
            evidence = researched_text.evidence_from_response(_grounding(CASE["research"]["facts"]))
            return ModelInvocation(raw_text=evidence.model_dump_json())
        if prompt.startswith("Write SHORT researched"):
            self.calls["copy"] += 1
            return ModelInvocation(raw_text=json.dumps(_research_copy(prompt)))
        if prompt.startswith("Review proposed on-screen captions"):
            self.calls["review"] += 1
            return ModelInvocation(raw_text=json.dumps({"approved": True, "reason": "Cited."}))
        if "You resolve a creator's open-vocabulary request" in prompt:
            self.calls["resolver"] += 1
            return ModelInvocation(raw_text=json.dumps(self._resolve(prompt)))
        if "You extract the COMPLETE inventory of clip operations" in prompt:
            self.calls["clip_intent_planner"] += 1
            rows = self.turn.get("clip_intent_planner", [])
            return ModelInvocation(raw_text=json.dumps({"intents": rows, "question": None}))
        if "You are Nova's Main Creator Agent" in prompt:
            self.calls["main_creator"] += 1
            return ModelInvocation(raw_text=json.dumps(self.turn["main_creator"]))
        raise AssertionError("unrecorded model call: " + prompt[:120])

    def _resolve(self, prompt: str) -> dict[str, Any]:
        """Matches by the clip record's subject, so a shard without the clip gets none."""
        alias_by_subject = {
            json.loads(record).get("subject"): alias
            for alias, _kind, record in _CLIP_LINE.findall(prompt)
        }
        answers = {row["intent_id"]: row for row in self.turn.get("resolver", [])}
        out = []
        for intent_id in _ASKED_INTENT.findall(prompt):
            answer = answers.get(intent_id, {})
            out.append(
                {
                    "intent_id": intent_id,
                    "assignments": [
                        {
                            "media": alias_by_subject[subject],
                            "confidence": 0.9,
                            "evidence": answer["evidence"],
                        }
                        for subject in answer.get("subjects", [])
                        if subject in alias_by_subject
                    ],
                    **({"caption": answer["caption"]} if answer.get("caption") else {}),
                }
            )
        return {"intents": out}


def _wire(monkeypatch: pytest.MonkeyPatch) -> _Models:
    models = _Models()
    for target in (
        "app.kria.planner.default_client",
        "app.services.clip_intent_planning.default_client",
        "app.services.clip_intent_resolution.default_client",
        "app.services.researched_text.GroundedGeminiClient",
        "app.services.researched_text.default_client",
        "app.services.creation_text_composition.default_client",
    ):
        monkeypatch.setattr(target, lambda: models)

    def vision(_clip, _question, **_kwargs):  # noqa: ANN001, ANN003, ANN202
        models.calls["vision"] += 1
        return ClipQuestionOutput(answer=models.turn["vision_answer"], confidence=0.6, evidence="")

    monkeypatch.setattr(resolution, "query_clip_vision", vision)
    plan_with_live_agent = kria_runtime._plan_with_live_agent

    async def planned_turn(*args, **kwargs):  # noqa: ANN002, ANN003, ANN202
        models.planned.append(await plan_with_live_agent(*args, **kwargs))
        return models.planned[-1]

    # Observed, not replaced: the planner's own verdict, before readiness may hold it back.
    monkeypatch.setattr(kria_runtime, "_plan_with_live_agent", planned_turn)
    return models


# -- the project and the creator's turns ----------------------------------------------------


def _row(clip: dict[str, Any], index: int, *, speaking: bool) -> dict[str, Any]:
    media_id = f"analysis-proxy-ios-{clip['uuid']}.mp4"
    row = copy.deepcopy(MEROPE["renders"][0]["media_snapshot"]["clip_assignments"][0])
    proxy = row["upload_contract"]["proxy"]
    proxy["duration_s"] = clip["duration_s"]
    proxy["original"].update(
        duration_s=clip["duration_s"], sha256=hashlib.sha256(media_id.encode()).hexdigest()
    )
    understanding = {key: clip[key] for key in ("subject", "summary", "setting", "activity")}
    transcript = ""
    if speaking and media_id == SPEAKER_ID:
        transcript = CASE["speaker"]["transcript"]
        understanding["speech"] = {
            "has_speech": True,
            "to_camera": True,
            "transcript": transcript,
            "segments": CASE["speaker"]["segments"],
        }
    row.update(
        media_id=media_id,
        manifest_identity=media_id,
        gcs_path=f"users/test/creation-threads/cunda/{media_id}",
        duration_s=clip["duration_s"],
        source_filename=f"{clip['uuid']}-cunda.mov",
        library_asset_id=str(uuid.uuid5(uuid.NAMESPACE_URL, media_id)),
        library_source_id=str(uuid.uuid5(uuid.NAMESPACE_URL, "source-" + media_id)),
        storage_generation="1",
        capture={"capture_time": f"2026-10-25T10:{index:02d}:00Z"},
        analysis={
            "subject": clip["subject"],
            "description": clip["summary"],
            "understanding": understanding,
            "on_screen_text": "",
            "best_moments": [],
            "transcript": transcript,
        },
    )
    return row


def _seed(*, speaking: bool = False) -> tuple[uuid.UUID, uuid.UUID]:
    user_id, thread_id, _session = _seed_runtime_project()
    rows = [_row(clip, i, speaking=speaking) for i, clip in enumerate(CASE["clips"])]
    with sync_session() as db:
        thread = db.get(CreationThread, thread_id)
        item = db.get(PlanItem, thread.active_plan_item_id)
        item.edit_format = "montage"
        item.idea = "Cunda Island"
        item.clip_gcs_paths = [row["gcs_path"] for row in rows]
        item.clip_assignments = rows
        thread.title = "Cunda Island"
        thread.state = {
            "media": [{"media_id": row["media_id"]} for row in rows],
            "media_count": len(rows),
            "edit_format": "montage",
        }
        for visual in CASE["visuals"]:
            asset_id = uuid.uuid4()
            db.add(
                PlanItemAsset(
                    id=asset_id,
                    plan_item_id=item.id,
                    user_id=user_id,
                    gcs_path=f"users/{user_id}/plan/{item.id}/pool/{asset_id}.jpg",
                    gcs_generation="1",
                    kind="image",
                    source_filename=visual["source_filename"],
                    status="ready",
                    aspect=0.5625,
                    analysis={
                        "subject": visual["summary"],
                        "description": visual["summary"],
                        "understanding": {
                            "subject": visual["summary"],
                            "summary": visual["summary"],
                        },
                    },
                )
            )
        db.commit()
    return user_id, thread_id


def _ask(candidate: str, question: str, updates: list) -> dict[str, Any]:
    return {
        "action": {"kind": "ask_user", "question": question, "reason_code": "hook_candidate"},
        "creative_decision": {
            "target": "opening_title",
            "status": "candidate",
            "proposed_text": candidate,
            "source_evidence": None,
            "language": "en",
        },
        "brief_updates": updates,
        "delegates_all_choices": False,
        "starts_fresh": False,
    }


async def _dismiss_card(chat: Chat) -> None:
    """The creator closes the render card to keep talking (a pending card queues the chat)."""
    with sync_session() as db:
        approval = db.execute(
            select(CreatorAgentApproval).where(CreatorAgentApproval.turn_id == chat.turn_ids[-1])
        ).scalar_one_or_none()
        if approval is None:
            return
        body = ApprovalDecisionBody(
            expected_thread_revision=int(db.get(CreationThread, chat.thread_id).revision),
            expected_draft_revision=approval.draft_revision,
            expected_approval_fingerprint=approval_fingerprint(approval),
        )
    async with AsyncSessionLocal() as db:
        await decide_approval(
            db,
            thread_id=chat.thread_id,
            approval_id=approval.id,
            creator_id=chat.user_id,
            decision="deny",
            body=body,
        )


def _propose(
    *,
    intents: list,
    updates: list | tuple = (),
    opening_title: str | None = APPROVED,
    montage_audio: dict | None = None,
) -> dict[str, Any]:
    strategy = {**copy.deepcopy(CASE["strategy"]), "clip_intents": intents or None}
    if opening_title:
        strategy["opening_title"] = opening_title
    if montage_audio:
        strategy["montage_audio"] = montage_audio
    decision = {
        "target": "opening_title",
        "status": "unresolved",
        "proposed_text": None,
        "source_evidence": None,
        "language": "en",
    }
    return {
        "action": {
            "kind": "propose_strategy",
            "strategy": strategy,
            "summary": "A 30-second introduction to Cunda: the drive in, the old town, the food "
            "and the sea.",
        },
        **({"creative_decision": decision} if opening_title else {}),
        "brief_updates": list(updates),
        "delegates_all_choices": False,
        "starts_fresh": False,
    }


T1 = {
    "main_creator": _ask(
        CASE["hook"]["first"],
        f"For the hook I'd use “{CASE['hook']['first']}”. The app will automatically research "
        "and add the introductory facts. Does this wording work?",
        CASE["brief_updates"]["t1"],
    )
}
T2 = {"main_creator": _ask(APPROVED, f"How about “{APPROVED}” as the opening hook?", [])}
# T3 the way prod answered it: the described intro explanation became a clip caption intent.
T3_PROD = {
    "main_creator": _propose(intents=[INTRO["main_creator"]]),
    "clip_intent_planner": [INTRO["clip_intent_planner"]],
    "resolver": [INTRO["resolver"]],
    "vision_answer": INTRO["vision_answer"],
}
# T3 the way a prompt that keeps whole-video text out of clip intents answers it.
T3_ALIGNED = {"main_creator": _propose(intents=[]), "clip_intent_planner": []}
T3_ANSWERS = [
    pytest.param(T3_PROD, id="t3-prod-answer"),
    pytest.param(T3_ALIGNED, id="t3-prompt-aligned-answer"),
]
_CAMERA_VOICE = {
    "source_media_ids": [SPEAKER_ID],
    "preview_source_beds": False,
    "preserve_source_audio": True,
}


@dataclass
class Chat:
    user_id: uuid.UUID
    thread_id: uuid.UUID
    models: _Models
    replies: list[CreationThreadEvent] = field(default_factory=list)
    turn_ids: list[uuid.UUID] = field(default_factory=list)

    async def send(
        self, message: str, answers: dict[str, Any], *, option: str | None = None
    ) -> CreationThreadEvent:
        self.models.turn = answers
        with sync_session() as db:
            revision = int(db.get(CreationThread, self.thread_id).revision)
        selection = {}
        if option is not None:
            question = (self.replies[-1].payload or {})["choice_question"]
            selection = {
                "choice_selection": ChoiceSelectionIn(
                    question_id=question["question_id"], option_key=option
                )
            }
        async with AsyncSessionLocal() as db:
            accepted, _ = await submit_turn(
                db,
                thread_id=self.thread_id,
                creator_id=self.user_id,
                body=SubmitTurnBody(
                    message=message,
                    client_event_id=f"kri945-{uuid.uuid4().hex}",
                    expected_thread_revision=revision,
                    **selection,
                ),
            )
        result = await asyncio.to_thread(run_kria_turn.run, accepted.turn_id)
        assert result["status"] in {"completed", "awaiting_approval"}, result
        self.turn_ids.append(uuid.UUID(accepted.turn_id))
        self.replies.append(self._last_reply())
        return self.replies[-1]

    def _last_reply(self) -> CreationThreadEvent:
        with sync_session() as db:
            reply = (
                db.execute(
                    select(CreationThreadEvent)
                    .where(
                        CreationThreadEvent.thread_id == self.thread_id,
                        CreationThreadEvent.event_type.in_(["draft_applied", "assistant_response"]),
                    )
                    .order_by(CreationThreadEvent.sequence.desc())
                )
                .scalars()
                .first()
            )
            db.expunge(reply)
            return reply

    @property
    def planned(self) -> PlannedKriaTurn:
        return self.models.planned[-1]

    @property
    def strategy(self) -> dict[str, Any]:
        intents = self.planned.plan.intents
        return dict(intents[0].arguments.get("strategy") or {}) if intents else {}

    def brief(self) -> CreativeBrief:
        with sync_session() as db:
            return load_latest_brief_sync(db, self.thread_id)

    def readiness(self) -> dict[str, Any]:
        """The last turn's readiness report: on its approval card, or on the held-back reply."""
        with sync_session() as db:
            approval = db.execute(
                select(CreatorAgentApproval).where(
                    CreatorAgentApproval.turn_id == self.turn_ids[-1]
                )
            ).scalar_one_or_none()
            if approval is not None:
                draft = db.get(CreatorEditDraft, approval.draft_id)
                coverage = KriaDraftDocument.model_validate(draft.snapshot_json).brief_coverage
            else:
                coverage = (self.replies[-1].payload or {}).get("brief_coverage")
        return dict((coverage or {}).get("approval_readiness") or {})

    def unready(self) -> dict[str, tuple[str, str]]:
        return {
            row["requirement_id"]: (row["disposition"], row["reason"])
            for row in self.readiness().get("requirements", [])
            if row["disposition"] not in {"executable", "delegated"}
        }


async def _approve_hook(monkeypatch: pytest.MonkeyPatch, t3: dict[str, Any], **seed: Any) -> Chat:
    """T1 asks the hook wording, T2 revises it, T3 approves the revised wording."""
    user_id, thread_id = _seed(**seed)
    chat = Chat(user_id, thread_id, _wire(monkeypatch))
    first = await chat.send(MESSAGES["t1"], T1)
    assert (first.payload or {})["choice_question"]["candidate"] == CASE["hook"]["first"]
    assert CAPTION_ASK not in str(first.content)
    revised = await chat.send(MESSAGES["t2"], T2, option="revise")
    assert (revised.payload or {})["choice_question"]["candidate"] == APPROVED
    await chat.send(MESSAGES["t3"], t3, option="approve")
    return chat


def _hook_literal(brief: CreativeBrief) -> str | None:
    return next(req.literal for req in brief.live() if req.kind == "text" and req.scope == "title")


def _intro_caption_kept(strategy: dict[str, Any]) -> bool:
    return any(
        row.get("attribute") == INTRO["main_creator"]["attribute"]
        for key in ("clip_intents", "resolved_clip_intents")
        for row in strategy.get(key) or []
    )


# -- the journey ----------------------------------------------------------------------------


@pytest.mark.parametrize("t3", T3_ANSWERS)
async def test_approving_the_hook_drafts_it_with_no_caption_question(
    monkeypatch: pytest.MonkeyPatch, t3: dict[str, Any]
) -> None:
    chat = await _approve_hook(monkeypatch, t3)
    plan = chat.planned.plan
    observed = {
        "mode": plan.mode,
        "tool": plan.intents[0].tool_name if plan.intents else None,
        "opening_title": chat.strategy.get("opening_title"),
        "asks_about_text": plan.clip_question is not None
        or CAPTION_ASK in f"{plan.response} {chat.replies[-1].content}",
        "intro_caption_kept": _intro_caption_kept(chat.strategy),
        "hook_literal": _hook_literal(chat.brief()),
    }
    assert observed == {
        "mode": "act",
        "tool": "draft.apply_strategy",
        "opening_title": APPROVED,
        "asks_about_text": False,
        "intro_caption_kept": False,
        "hook_literal": APPROVED,
    }


@pytest.mark.parametrize("t3", T3_ANSWERS)
async def test_the_researched_explanation_is_sourced_and_ready_before_approval(
    monkeypatch: pytest.MonkeyPatch, t3: dict[str, Any]
) -> None:
    chat = await _approve_hook(monkeypatch, t3)
    observed = {
        "searches": chat.models.calls["search"],
        "sourced_wording_shown": SOURCED_TEXT in str(chat.replies[-1].content),
        "ready": chat.readiness().get("ready"),
        "unready": chat.unready(),
    }
    assert observed == {
        "searches": 1,
        "sourced_wording_shown": True,
        "ready": True,
        "unready": {},
    }


@pytest.mark.parametrize(
    ("speaking", "montage_audio", "expected"),
    [
        pytest.param(False, None, {"ready": True, "r1": None}, id="unified-montage-draws-it"),
        pytest.param(
            True,
            _CAMERA_VOICE,
            {"ready": False, "r1": ("needs_choice", LITERAL_MISS)},
            id="spoken-excerpt-montage-cannot",
        ),
    ],
)
async def test_typed_facts_are_ready_only_where_the_writer_draws_them(
    monkeypatch: pytest.MonkeyPatch,
    speaking: bool,
    montage_audio: dict | None,
    expected: dict[str, Any],
) -> None:
    chat = await _approve_hook(monkeypatch, T3_PROD, speaking=speaking)
    await _dismiss_card(chat)
    await chat.send(
        MESSAGES["t5"],
        {
            "main_creator": _propose(
                intents=[], updates=CASE["brief_updates"]["t5"], montage_audio=montage_audio
            ),
            "clip_intent_planner": [],
        },
    )
    brief = chat.brief()
    contract = build_render_contract(
        generation_id="kri945",
        strategy=chat.strategy,
        brief=brief,
        media_snapshot=chat.planned.media_snapshot,
    )
    assert next(req.literal for req in brief.live() if req.scope == "global") == FACTS_TEXT
    assert ("any", FACTS_TEXT) in {(row.role, row.text) for row in contract.exact_texts}
    assert {"ready": chat.readiness().get("ready"), "r1": chat.unready().get("r1")} == expected


async def test_a_per_clip_caption_ask_stays_on_its_clip(monkeypatch: pytest.MonkeyPatch) -> None:
    user_id, thread_id = _seed()
    chat = Chat(user_id, thread_id, _wire(monkeypatch))
    await chat.send(
        MESSAGES["butcher"],
        {
            "main_creator": _propose(
                intents=[BUTCHER["main_creator"]],
                updates=[BUTCHER["brief_update"]],
                opening_title=None,
            ),
            "clip_intent_planner": [BUTCHER["clip_intent_planner"]],
            "resolver": [BUTCHER["resolver"]],
        },
    )
    captions = [
        (row["op"], row["caption_text"], [a["media_id"] for a in row["assignments"]])
        for row in chat.strategy.get("resolved_clip_intents") or []
    ]
    assert chat.planned.plan.mode == "act"
    assert captions == [("caption", "The butcher's counter", [BUTCHER_ID])]
