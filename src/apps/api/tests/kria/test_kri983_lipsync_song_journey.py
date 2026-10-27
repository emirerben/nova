"""KRI-983: a lip-sync to the creator's own song, replayed through the real runtime.

Prod thread 953a5265 (2026-10-27): four videos, an uploaded song and "Create a lipsync". The
brief read it as r1 = audio "Create a lipsync to the uploaded song" with no facts. No draft
check knew a lip-sync ask, so approval readiness held the first draft ("I haven't established
how to render this change yet") and offered a "simpler version" that would have dropped the
only thing the creator asked for. "Try a different approach" re-ran the planner, which made
the same plan and asked the same question.

Real: ``submit_turn`` + ``run_kria_turn`` over scratch Postgres, so the planner, the brief
store, the song order gate, strategy policy, brief checks and approval readiness run as in
prod, and approval goes through the HTTP route. Only the provider transport is recorded
(``tests/fixtures/kri983_lipsync_song_turns.json``).

Ways this goes wrong, each pinned below:
1. A lip-sync to the uploaded song is held because nothing checks it before approval, and the
   approve-time re-check refuses what the draft-time check let through.
2. A draft that plays the song in the background where a lip-sync was asked is approved, or is
   held with an internal reason and a simpler version that would leave nothing to make.
3. "Change my request" (and a card stored with the old "Try a different approach" key) calls
   the planner again and asks the same question.
"""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import uuid
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import select

from app.agents._runtime import ModelClient, ModelInvocation
from app.auth import get_current_user
from app.config import settings
from app.database import AsyncSessionLocal, sync_session
from app.database import engine as async_engine
from app.kria.api_schemas import ChoiceSelectionIn, SubmitTurnBody
from app.kria.brief import CreativeBrief, load_latest_brief_sync
from app.kria.drafts import KriaDraftDocument
from app.kria.runtime import approval_fingerprint, submit_turn
from app.main import app
from app.models import (
    CreationThread,
    CreationThreadEvent,
    CreatorAgentApproval,
    CreatorEditDraft,
    PlanItem,
)
from app.schemas.user_song import SongAlignment, SongAnalysis, SongLine, SongWord, TakeAlignment
from app.services.choice_questions import simpler_version_question
from app.tasks import kria_runtime
from app.tasks.kria_runtime import run_kria_turn
from tests._prod_profile import apply_prod_profile
from tests.kria.test_kri775_merope_m2_replay import FIXTURE as MEROPE
from tests.kria.test_runtime_postgres_integration import _seed_runtime_project

CASE: dict[str, Any] = json.loads(
    (Path(__file__).resolve().parents[1] / "fixtures" / "kri983_lipsync_song_turns.json").read_text(
        encoding="utf-8"
    )
)
ASK = CASE["brief_update"]["description"]
BACKGROUND = "This draft plays your song in the background; your clips aren't matched to its words."
HELD = "I held this first draft back."


@pytest.fixture(autouse=True)
def _flags(monkeypatch: pytest.MonkeyPatch) -> None:
    apply_prod_profile(monkeypatch)
    monkeypatch.setattr(settings, "kria_brief_binding_enabled", True)
    monkeypatch.setattr(settings, "kria_approval_readiness_enabled", True)
    monkeypatch.setattr(settings, "kria_choice_questions_enabled", True)
    monkeypatch.setattr(settings, "user_song_montage_enabled", True)
    # A misshapen alignment must fail the test, not wait 45 s for one that never comes.
    monkeypatch.setattr(settings, "song_alignment_turn_deadline_s", 0.0)
    app.dependency_overrides.clear()
    yield
    app.dependency_overrides.clear()


@pytest_asyncio.fixture(autouse=True)
async def _dispose_async_engine():
    yield
    await async_engine.dispose()


# -- recorded provider transport ------------------------------------------------------------


@dataclass
class _Models(ModelClient):
    """Each agent's prompt answered with this turn's recorded output; every call counted."""

    turn: dict[str, Any] = field(default_factory=dict)
    calls: Counter = field(default_factory=Counter)

    def invoke(self, *, model: str, prompt: str, **_: Any) -> ModelInvocation:  # noqa: ARG002
        if "You extract the COMPLETE inventory of clip operations" in prompt:
            self.calls["clip_intent_planner"] += 1
            return ModelInvocation(raw_text=json.dumps({"intents": [], "question": None}))
        if "You are Nova's Main Creator Agent" in prompt:
            self.calls["main_creator"] += 1
            return ModelInvocation(raw_text=json.dumps(self.turn["main_creator"]))
        raise AssertionError("unrecorded model call: " + prompt[:120])


def _wire(monkeypatch: pytest.MonkeyPatch) -> _Models:
    models = _Models()
    for target in (
        "app.kria.planner.default_client",
        "app.services.clip_intent_planning.default_client",
        "app.services.clip_intent_resolution.default_client",
    ):
        monkeypatch.setattr(target, lambda: models)
    return models


def _propose(*, song_sync: str, updates: list[dict[str, Any]], title: str | None = None) -> dict:
    strategy = {**copy.deepcopy(CASE["strategy"]), "song_sync": song_sync}
    decision = {}
    if title:
        strategy["opening_title"] = title
        decision = {
            "creative_decision": {
                "target": "opening_title",
                "status": "creator_supplied",
                "proposed_text": title,
                "source_evidence": f"with the title {title}",
                "language": "en",
            }
        }
    return {
        "main_creator": {
            "action": {
                "kind": "propose_strategy",
                "strategy": strategy,
                "summary": CASE["summary"][song_sync],
            },
            "brief_updates": updates,
            "delegates_all_choices": False,
            "starts_fresh": False,
            **decision,
        }
    }


LIPSYNC = _propose(song_sync="lipsync", updates=[CASE["brief_update"]])
BACKGROUND_ONLY = _propose(song_sync="background", updates=[CASE["brief_update"]])
TITLE = CASE["title_update"]["literal"]
BACKGROUND_WITH_TITLE = _propose(
    song_sync="background", updates=[CASE["brief_update"], CASE["title_update"]], title=TITLE
)


def _legacy_card(*args: Any, **kwargs: Any) -> dict[str, Any]:
    """The held reply's card as a build before KRI-983 stored it."""
    question = simpler_version_question(*args, **kwargs)
    question["options"] = [
        {"key": "different_approach", "label": "Try a different approach", "recommended": False},
        {"key": "simpler_version", "label": "Make this simpler version", "recommended": False},
    ]
    return question


# -- the project ----------------------------------------------------------------------------


def _media_id(clip: dict[str, Any]) -> str:
    return f"analysis-proxy-ios-{clip['uuid']}.mp4"


def _row(clip: dict[str, Any], index: int) -> dict[str, Any]:
    media_id = _media_id(clip)
    row = copy.deepcopy(MEROPE["renders"][0]["media_snapshot"]["clip_assignments"][0])
    proxy = row["upload_contract"]["proxy"]
    proxy["duration_s"] = clip["duration_s"]
    proxy["original"].update(
        duration_s=clip["duration_s"], sha256=hashlib.sha256(media_id.encode()).hexdigest()
    )
    understanding = {key: clip[key] for key in ("subject", "summary", "setting", "activity")}
    row.update(
        media_id=media_id,
        manifest_identity=media_id,
        gcs_path=f"users/test/creation-threads/lipsync/{media_id}",
        duration_s=clip["duration_s"],
        source_filename=f"{clip['uuid']}-take.mov",
        library_asset_id=str(uuid.uuid5(uuid.NAMESPACE_URL, media_id)),
        library_source_id=str(uuid.uuid5(uuid.NAMESPACE_URL, "source-" + media_id)),
        storage_generation="1",
        capture={"capture_time": f"2026-10-27T11:{index:02d}:00Z"},
        analysis={
            "subject": clip["subject"],
            "description": clip["summary"],
            "understanding": understanding,
            "on_screen_text": "",
            "best_moments": [],
            "transcript": "",
        },
    )
    return row


def _song_analysis() -> dict[str, Any]:
    song = CASE["song"]
    lines = [SongLine(**line) for line in song["lines"]]
    words = []
    for line in lines:
        tokens = line.text.split()
        step = (line.end_s - line.start_s) / len(tokens)
        words.extend(
            SongWord(
                text=token, start_s=line.start_s + i * step, end_s=line.start_s + (i + 1) * step
            )
            for i, token in enumerate(tokens)
        )
    return SongAnalysis(
        generation=song["generation"],
        status="ready",
        duration_s=song["duration_s"],
        words=words,
        lines=lines,
    ).model_dump(mode="json")


def _song_alignment() -> dict[str, Any]:
    return SongAlignment(
        song_generation=CASE["song"]["generation"],
        takes={
            _media_id(clip): TakeAlignment(
                media_id=_media_id(clip),
                proxy_generation=1,
                status="confident",
                delta_s=clip["song_delta_s"],
                confidence=0.92,
            )
            for clip in CASE["clips"]
        },
    ).model_dump(mode="json")


def _seed() -> tuple[uuid.UUID, uuid.UUID]:
    user_id, thread_id, _session = _seed_runtime_project()
    rows = [_row(clip, i) for i, clip in enumerate(CASE["clips"])]
    song = CASE["song"]
    with sync_session() as db:
        thread = db.get(CreationThread, thread_id)
        item = db.get(PlanItem, thread.active_plan_item_id)
        item.edit_format = "montage"
        item.idea = "Lipsync Creation"
        item.clip_gcs_paths = [row["gcs_path"] for row in rows]
        item.clip_assignments = rows
        item.song_gcs_path = song["gcs_path"]
        item.song_filename = song["filename"]
        item.song_generation = song["generation"]
        item.song_duration_s = song["duration_s"]
        item.song_analysis = _song_analysis()
        item.song_alignment = _song_alignment()
        thread.title = "Lipsync Creation"
        thread.state = {
            "media": [{"media_id": row["media_id"]} for row in rows],
            "media_count": len(rows),
            "edit_format": "montage",
        }
        db.commit()
    return user_id, thread_id


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
                    client_event_id=f"kri983-{uuid.uuid4().hex}",
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

    def approval(self) -> CreatorAgentApproval | None:
        with sync_session() as db:
            approval = db.execute(
                select(CreatorAgentApproval).where(
                    CreatorAgentApproval.turn_id == self.turn_ids[-1]
                )
            ).scalar_one_or_none()
            if approval is not None:
                db.expunge(approval)
            return approval

    def readiness(self) -> dict[str, Any]:
        """The last turn's readiness report: on its approval card, or on the held-back reply."""
        approval = self.approval()
        with sync_session() as db:
            if approval is not None:
                draft = db.get(CreatorEditDraft, approval.draft_id)
                coverage = KriaDraftDocument.model_validate(draft.snapshot_json).brief_coverage
            else:
                coverage = (self.replies[-1].payload or {}).get("brief_coverage")
        return dict((coverage or {}).get("approval_readiness") or {})

    def verdicts(self) -> dict[str, tuple[str, str]]:
        return {
            row["requirement_id"]: (row["disposition"], row["reason"])
            for row in self.readiness().get("requirements", [])
        }

    def brief(self) -> CreativeBrief:
        with sync_session() as db:
            return load_latest_brief_sync(db, self.thread_id)


async def _approve_over_http(chat: Chat, monkeypatch: pytest.MonkeyPatch) -> httpx.Response:
    approval = chat.approval()
    assert approval is not None
    with sync_session() as db:
        body = {
            "expected_thread_revision": int(db.get(CreationThread, chat.thread_id).revision),
            "expected_draft_revision": approval.draft_revision,
            "expected_approval_fingerprint": approval_fingerprint(approval),
        }
    app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(
        id=chat.user_id, email="qa@test.local"
    )
    monkeypatch.setattr("app.routes.kria_runtime._publish_approval", lambda _id: None)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        return await client.post(
            f"/creation-threads/{chat.thread_id}/approvals/{approval.id}/approve", json=body
        )


async def _chat(monkeypatch: pytest.MonkeyPatch) -> Chat:
    user_id, thread_id = _seed()
    return Chat(user_id, thread_id, _wire(monkeypatch))


# -- the journey ----------------------------------------------------------------------------


async def test_lipsync_to_the_uploaded_song_reaches_approval(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    chat = await _chat(monkeypatch)
    reply = await chat.send(CASE["message"], LIPSYNC)
    approval = chat.approval()
    approved = await _approve_over_http(chat, monkeypatch) if approval is not None else None
    observed = {
        "held_reply": None if approval is not None else reply.content,
        "is_the_prod_hold": reply.content == CASE["prod_reply"],
        "r1": chat.verdicts().get("r1", ("", ""))[0],
        "ready": chat.readiness().get("ready"),
        "approved": None if approved is None else (approved.status_code, approved.json()["status"]),
    }
    assert observed == {
        "held_reply": None,
        "is_the_prod_hold": False,
        "r1": "executable",
        "ready": True,
        "approved": (200, "approved"),
    }


async def test_a_background_song_for_a_lipsync_ask_stays_held(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    chat = await _chat(monkeypatch)
    reply = await chat.send(CASE["message"], BACKGROUND_ONLY)
    observed = {
        "approval_card": chat.approval() is not None,
        "r1": chat.verdicts().get("r1"),
        "choice_question": (reply.payload or {}).get("choice_question"),
        "reply": reply.content,
    }
    assert observed == {
        "approval_card": False,
        "r1": ("unsupported", BACKGROUND),
        # Leaving out the only ask would make nothing the creator asked for.
        "choice_question": None,
        "reply": (
            f"I can't make this yet:\n- {ASK}: {BACKGROUND}\n"
            f"{HELD} Tell me what to change and I'll prepare it again."
        ),
    }


@pytest.mark.parametrize("stored", ["current_card", "legacy_card"])
async def test_change_my_request_answers_without_replanning(
    monkeypatch: pytest.MonkeyPatch, stored: str
) -> None:
    chat = await _chat(monkeypatch)
    request = f"{CASE['message']} with the title {TITLE}"
    if stored == "legacy_card":
        with monkeypatch.context() as old_build:
            old_build.setattr(kria_runtime, "simpler_version_question", _legacy_card)
            await chat.send(request, BACKGROUND_WITH_TITLE)
        key, label = "different_approach", "Try a different approach"
    else:
        held = await chat.send(request, BACKGROUND_WITH_TITLE)
        question = (held.payload or {}).get("choice_question") or {}
        offered = {option["key"]: option.get("label") for option in question.get("options", [])}
        assert offered == {
            "change_request": "Change my request",
            "simpler_version": "Make it without these",
        }, held.content
        key, label = "change_request", "Change my request"
    before = sum(chat.models.calls.values())
    reply = await chat.send(label, BACKGROUND_WITH_TITLE, option=key)
    observed = {
        "provider_calls": sum(chat.models.calls.values()) - before,
        "reply": reply.content,
        "asks_again": (reply.payload or {}).get("choice_question") is not None,
        "approval_card": chat.approval() is not None,
        "live_asks": [req.id for req in chat.brief().live()],
    }
    assert observed == {
        "provider_calls": 0,
        "reply": f"Nothing is made yet. Tell me what to do differently for: {ASK}.",
        "asks_again": False,
        "approval_card": False,
        "live_asks": ["r1", "r2"],
    }
