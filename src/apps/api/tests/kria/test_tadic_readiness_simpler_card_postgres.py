"""Tadic (prod thread aa36d140, 2026-11-14): approval readiness's own "simpler version?" has a card.

The planner's policy check passed, but approval readiness re-ran it (asking before simplifying)
and refused: "Your clips are about 14 seconds, so the video can't be 14.005 seconds. Should I
make a simpler version?". Readiness stored that sentence as ``policy_problem``, and the held
draft offered no card whenever ``policy_problem`` was set, so the creator's "Yap", "Yes",
"Evet" were plain new messages that re-planned into the same hold. No video was made.

Ways this can fail, written before the fix:

 F1 the incident: a reply that asks "Should I make a simpler version?" carries no
    ``choice_question`` (the structural guard below checks every assistant event).
 F2 the incident's loop: "Evet" / "Yes" / a tap on the card re-plans into the same hold.
 F3 the tap drafts, but approval re-checks the saved strategy and refuses it (the draft must
    be the simplified strategy, not the asked one with a waived check).
 F4 over-reach: the tap is read as consent to leave out the brief's asks (every row is
    ``unsupported`` under a policy problem, so a legacy-offer match would drop them all).
 F5 "Change my request" on the card stops working or drafts something.
 F6 a policy problem that is not a simplification (rendering unavailable, an invalid
    strategy) gets a "make it without these" card it cannot honour.
 F7 a stale card: a tap on a card offered at an older brief revision, or a tap beside a new
    ask, drafts the omission.

Review round (KRI-1447), written before that fix. Under any policy problem every row's reason
is the problem's sentence, so a guard reading reasons always passes:

 M1 a mixed hold (the refusal plus an ask held for its own reason, e.g. a title literal the
    plan lacks) gets the card, and "yes" holds again on that ask.
 M2 the mixed hold's reply names only the refusal and hides the other blocker.
 M3 the mixed hold's reply still asks "Should I make a simpler version?" with no card (F1).
 M4 over-correction: an ask about the setting the refusal changes (a stated length) is
    counted as its own blocker, so the incident's shape loses its card and loops again.
 R1 an "Evet" whose plan restates a saved ask (an identical add, or a change that changes
    nothing) is read as a new request, and the loop is back.
 R2 the restatement allowance lets a real change or a new ask through (F7 still holds).

The real runtime, planner, policy, compiler, readiness and approval run over scratch Postgres.
Only the model transport is replayed, and readiness's own policy re-check is made to refuse the
way prod's did (the length rounding itself is the strategy-policy fix, not this one).
"""

from __future__ import annotations

import asyncio
import json
import uuid
from dataclasses import replace

import pytest
import pytest_asyncio
from sqlalchemy import select

from app.agents._runtime import ModelInvocation
from app.agents._schemas.creator_agent import CapabilityAvailability
from app.config import settings
from app.database import AsyncSessionLocal, sync_session
from app.database import engine as async_engine
from app.kria import approval_readiness
from app.kria.api_schemas import ApprovalDecisionBody, ChoiceSelectionIn, SubmitTurnBody
from app.kria.brief import load_latest_brief_sync
from app.kria.reply_language import say
from app.kria.runtime import approval_fingerprint, decide_approval, submit_turn
from app.kria.strategy_policy import CheckedStrategy, RefusedStrategy
from app.models import (
    CreationThread,
    CreationThreadEvent,
    CreativeBriefVersion,
    CreatorAgentApproval,
    CreatorEditDraft,
    PlanItem,
)
from app.services.choice_questions import SIMPLER_VERSION_REASON
from app.tasks.kria_runtime import run_kria_turn
from tests.kria.test_runtime_postgres_integration import _seed_runtime_project

ASKED_S = 10.0
KEPT_S = 9.0  # what the check keeps without asking (prod: the clip's own length)
SIMPLER_ASKS = (
    "Should I make a simpler version?",
    "Daha sade bir sürüm yapayım mı?",
    "Should I make it without these, or do you want to change your request?",
    "Bunlar olmadan mı hazırlayayım, yoksa isteğini mi değiştirmek istersin?",
)
_REAL_CHECK = approval_readiness.check_strategy_for_runtime_v2
LENGTH = say(
    en="Your clips are about 14 seconds, so the video can't be 14.005 seconds.",
    tr="Klipler toplam yaklaşık 14 saniye, bu yüzden video 14.005 saniye olamıyor.",
)


@pytest.fixture(autouse=True)
def _flags(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "kria_runtime_v2_enabled",
        "main_creator_agent_enabled",
        "kria_creative_brief_enabled",
        "kria_brief_binding_enabled",
        "kria_approval_readiness_enabled",
        "kria_choice_questions_enabled",
        "guided_edit_capability_enabled",
        "edit_format_day_vlog_enabled",
    ):
        monkeypatch.setattr(settings, name, True)
    monkeypatch.setattr(settings, "kria_creative_brief_user_ids", [])
    monkeypatch.setattr(settings, "kria_brief_binding_user_ids", [])
    monkeypatch.setattr(settings, "kria_clip_understanding_enabled", False)
    monkeypatch.setattr(settings, "clip_intents_enabled", False)
    monkeypatch.setattr(settings, "ios_device_only_mode", False)


@pytest_asyncio.fixture(autouse=True)
async def _dispose_async_engine():
    yield
    await async_engine.dispose()


def _readiness_refuses(
    monkeypatch: pytest.MonkeyPatch, *, code: str = SIMPLER_VERSION_REASON, asked: float = ASKED_S
):
    """Readiness's re-check refuses the asked length, as prod's did; the planner's passes."""

    def check(manifest, strategy, **kwargs):  # noqa: ANN001, ANN202
        if strategy.target_duration_s == asked:
            if kwargs.get("ask_before_simplifying"):
                return RefusedStrategy(
                    question=f"{LENGTH} "
                    + say(
                        en="Should I make a simpler version?", tr="Daha sade bir sürüm yapayım mı?"
                    )
                    if code == SIMPLER_VERSION_REASON
                    else say(en="This edit can't be made.", tr="Bu düzenleme yapılamıyor."),
                    code=code,
                    fields=("target_duration_s",) if code == SIMPLER_VERSION_REASON else (),
                    detail=LENGTH if code == SIMPLER_VERSION_REASON else "",
                )
            # Prod's length sentence is a notice of the check that does not ask.
            checked = _REAL_CHECK(
                manifest, strategy.model_copy(update={"target_duration_s": KEPT_S}), **kwargs
            )
            if isinstance(checked, CheckedStrategy):
                return replace(checked, notices=(*checked.notices, LENGTH))
            return checked
        return _REAL_CHECK(manifest, strategy, **kwargs)

    monkeypatch.setattr(approval_readiness, "check_strategy_for_runtime_v2", check)


def _seed(language: str) -> tuple[uuid.UUID, uuid.UUID]:
    user_id, thread_id, _ = _seed_runtime_project()
    with sync_session() as db:
        thread = db.get(CreationThread, thread_id)
        thread.state = {**thread.state, "reply_language": language}
        item = db.get(PlanItem, thread.active_plan_item_id)
        item.edit_format = "day_vlog"
        item.clip_gcs_paths = ["users/test/tadic.mp4"]
        item.clip_assignments = [
            {
                "media_id": "clip-1",
                "gcs_path": "users/test/tadic.mp4",
                "kind": "video",
                "duration_s": 14.0,
                "analysis": {"summary": "A football crowd singing", "has_speech": False},
            }
        ]
        db.commit()
    return user_id, thread_id


def _model(
    monkeypatch: pytest.MonkeyPatch, *, updates: list[dict] | None = None, **strategy
) -> None:
    """The Main Creator proposes the same edit on every turn, as prod's did."""
    output = {
        "action": {
            "kind": "propose_strategy",
            "summary": "The crowd clip with the Fenerbahçe title.",
            "strategy": {
                "direction": "guided_story",
                "edit_format": "day_vlog",
                "audio_strategy": "original_audio",
                "pacing": "balanced",
                "render_program": "guided",
                "opening_title": "Fenerbahçe",
                "target_duration_s": ASKED_S,
                "selected_media_ids": ["clip-1"],
                "rationale": "One clip, its own sound, the title on top.",
                **strategy,
            },
        },
        "brief_updates": updates or [],
    }

    class Transport:
        def invoke(self, **_kwargs):  # noqa: ANN003, ANN202
            return ModelInvocation(raw_text=json.dumps(output))

    monkeypatch.setattr("app.kria.planner.default_client", lambda: Transport())


def _events(thread_id: uuid.UUID) -> list[CreationThreadEvent]:
    with sync_session() as db:
        return list(
            db.scalars(
                select(CreationThreadEvent)
                .where(CreationThreadEvent.thread_id == thread_id)
                .order_by(CreationThreadEvent.sequence)
            )
        )


def _assert_every_simpler_ask_has_a_card(thread_id: uuid.UUID) -> None:
    """F1, the structural guard: a reply asking the simpler-version question carries its card."""
    for event in _events(thread_id):
        if event.role != "assistant" or not any(a in (event.content or "") for a in SIMPLER_ASKS):
            continue
        question = (event.payload or {}).get("choice_question")
        assert isinstance(question, dict), event.content
        assert question["conflict"] == SIMPLER_VERSION_REASON
        assert {o["key"] for o in question["options"]} == {"simpler_version", "change_request"}


async def _turn(
    user_id: uuid.UUID,
    thread_id: uuid.UUID,
    message: str,
    *,
    tap: tuple[str, str] | None = None,
) -> tuple[str, CreationThreadEvent]:
    with sync_session() as db:
        revision = int(db.get(CreationThread, thread_id).revision)
    async with AsyncSessionLocal() as db:
        accepted, _ = await submit_turn(
            db,
            thread_id=thread_id,
            creator_id=user_id,
            body=SubmitTurnBody(
                message=message,
                client_event_id=f"tadic-{uuid.uuid4().hex}",
                expected_thread_revision=revision,
                **(
                    {"choice_selection": ChoiceSelectionIn(question_id=tap[0], option_key=tap[1])}
                    if tap
                    else {}
                ),
            ),
        )
    result = await asyncio.to_thread(run_kria_turn.run, accepted.turn_id)
    assert result["status"] in {"completed", "awaiting_approval"}, result
    _assert_every_simpler_ask_has_a_card(thread_id)
    reply = next(
        e
        for e in reversed(_events(thread_id))
        if e.event_type in {"draft_applied", "assistant_response"}
    )
    return result["status"], reply


def _pending(thread_id: uuid.UUID) -> list[CreatorAgentApproval]:
    with sync_session() as db:
        return list(
            db.scalars(
                select(CreatorAgentApproval).where(
                    CreatorAgentApproval.thread_id == thread_id,
                    CreatorAgentApproval.status == "pending",
                )
            )
        )


def _head_strategy(thread_id: uuid.UUID) -> dict | None:
    with sync_session() as db:
        draft = db.scalars(
            select(CreatorEditDraft).where(
                CreatorEditDraft.thread_id == thread_id, CreatorEditDraft.is_head.is_(True)
            )
        ).one_or_none()
        return draft.snapshot_json["strategy"] if draft is not None else None


async def _hold(monkeypatch, language: str = "en"):  # noqa: ANN001, ANN202
    user_id, thread_id = _seed(language)
    _readiness_refuses(monkeypatch)
    _model(
        monkeypatch,
        updates=[{"kind": "text", "scope": "title", "literal": "Fenerbahçe"}],
    )
    status, held = await _turn(
        user_id,
        thread_id,
        'Add the title "Fenerbahçe"' if language == "en" else 'Başlık "Fenerbahçe" olsun',
    )
    # The answering turns state nothing new.
    _model(monkeypatch)
    return user_id, thread_id, status, held


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("language", "answer"), [("en", "Yes"), ("tr", "Evet"), ("en", None), ("tr", None)]
)
async def test_yes_after_the_readiness_refusal_drafts_the_simpler_version(
    monkeypatch, language, answer
):
    user_id, thread_id, status, held = await _hold(monkeypatch, language)
    # F1: the hold asks the question and carries the card that answers it.
    assert status == "completed" and held.event_type == "assistant_response"
    # The refusal alone: one question, naming what changes, never "without these" under the
    # title the simpler version keeps.
    assert held.content == f"{LENGTH} {SIMPLER_ASKS[0 if language == 'en' else 1]}"
    question = held.payload["choice_question"]
    assert question["dropped_fields"] == ["target_duration_s"]
    assert question["brief_version"] == 1
    assert not _pending(thread_id)

    # F2: a typed yes (or a tap on the card) drafts what the check keeps, once.
    status, drafted = await _turn(
        user_id,
        thread_id,
        answer or "Make it without these",
        tap=None if answer else (question["question_id"], "simpler_version"),
    )
    assert status == "awaiting_approval", drafted.content
    assert drafted.event_type == "draft_applied"
    strategy = _head_strategy(thread_id)
    assert strategy["target_duration_s"] == KEPT_S
    assert strategy["opening_title"] == "Fenerbahçe"
    assert LENGTH in drafted.content
    # F4: the title ask stays in the request; only the strategy's length was left out.
    with sync_session() as db:
        brief = load_latest_brief_sync(db, thread_id)
    assert [req.literal for req in brief.live()] == ["Fenerbahçe"]

    # F3: approval re-checks the saved draft and agrees with it.
    (approval,) = _pending(thread_id)
    with sync_session() as db:
        body = ApprovalDecisionBody(
            expected_thread_revision=db.get(CreationThread, thread_id).revision,
            expected_draft_revision=approval.draft_revision,
            expected_approval_fingerprint=approval_fingerprint(approval),
        )
    async with AsyncSessionLocal() as db:
        await decide_approval(
            db,
            thread_id=thread_id,
            approval_id=approval.id,
            creator_id=user_id,
            decision="approve",
            body=body,
        )
    with sync_session() as db:
        assert db.get(CreatorAgentApproval, approval.id).status == "approved"


@pytest.mark.asyncio
async def test_change_my_request_on_the_card_still_drafts_nothing(monkeypatch):
    user_id, thread_id, _, held = await _hold(monkeypatch)
    status, reply = await _turn(
        user_id,
        thread_id,
        "Change my request",
        tap=(held.payload["choice_question"]["question_id"], "change_request"),
    )
    # F5
    assert status == "completed" and reply.event_type == "assistant_response"
    assert reply.content.startswith("Nothing is made yet.")
    assert _head_strategy(thread_id) is None and not _pending(thread_id)


@pytest.mark.asyncio
@pytest.mark.parametrize("problem", ["rendering_unavailable", "strategy_invalid"])
async def test_a_policy_problem_that_is_no_simplification_gets_no_card(monkeypatch, problem):
    user_id, thread_id = _seed("en")
    _model(monkeypatch, updates=[{"kind": "text", "scope": "title", "literal": "Fenerbahçe"}])
    if problem == "strategy_invalid":
        _readiness_refuses(monkeypatch, code="strategy_invalid")
    else:
        # The simplification refusal is there, but the renderer being unavailable is what holds.
        _readiness_refuses(monkeypatch)
        real = approval_readiness.assess_readiness

        def assess(brief, strategy, *, manifest, **kwargs):  # noqa: ANN001, ANN202
            if manifest is not None:
                manifest = manifest.model_copy(
                    update={
                        "capabilities": {
                            **manifest.capabilities,
                            "dispatch_render": CapabilityAvailability(
                                available=False, reason_code="off", reason="Rendering is off."
                            ),
                        }
                    }
                )
            return real(brief, strategy, manifest=manifest, **kwargs)

        monkeypatch.setattr(approval_readiness, "assess_readiness", assess)
    status, held = await _turn(user_id, thread_id, 'Add the title "Fenerbahçe"')
    # F6
    assert status == "completed" and held.event_type == "assistant_response"
    assert held.payload.get("choice_question") is None
    assert not any(ask in held.content for ask in SIMPLER_ASKS)
    assert held.payload["brief_coverage"]["approval_readiness"]["policy_refusal"] is None


@pytest.mark.asyncio
async def test_a_card_from_an_older_brief_revision_is_not_consent(monkeypatch):
    user_id, thread_id, _, held = await _hold(monkeypatch)
    # The request changed after the card was offered at brief v1.
    with sync_session() as db:
        v1 = load_latest_brief_sync(db, thread_id)
        db.add(
            CreativeBriefVersion(
                thread_id=thread_id,
                version=2,
                requirements=[req.model_dump(mode="json") for req in v1.requirements],
            )
        )
        db.commit()
    question_id = held.payload["choice_question"]["question_id"]
    status, reply = await _turn(
        user_id, thread_id, "Make it without these", tap=(question_id, "simpler_version")
    )
    # F7: offered again at the current revision, never drafted.
    assert status == "completed" and reply.event_type == "assistant_response", reply.content
    assert reply.payload["choice_question"]["brief_version"] == 2
    assert _head_strategy(thread_id) is None and not _pending(thread_id)


@pytest.mark.asyncio
async def test_a_tap_beside_a_new_ask_is_not_consent(monkeypatch):
    user_id, thread_id, _, held = await _hold(monkeypatch)
    _model(
        monkeypatch,
        updates=[{"kind": "style", "scope": "global", "description": "Black and white"}],
    )
    status, reply = await _turn(
        user_id,
        thread_id,
        "Make it without these",
        tap=(held.payload["choice_question"]["question_id"], "simpler_version"),
    )
    # F7: the new ask is a new request; it gets its own offer.
    assert status == "completed" and reply.event_type == "assistant_response", reply.content
    assert reply.payload.get("choice_question", {}).get("brief_version") == 2, reply.payload
    assert "Black and white" not in reply.content


@pytest.mark.asyncio
async def test_a_yes_beside_a_changed_ask_is_not_consent(monkeypatch):
    user_id, thread_id, _, held = await _hold(monkeypatch)
    with sync_session() as db:
        (title,) = load_latest_brief_sync(db, thread_id).live()
    _model(
        monkeypatch,
        updates=[
            {
                "operation": "change",
                "target_requirement_id": title.id,
                "kind": "text",
                "scope": "title",
                "literal": "Galatasaray",
            }
        ],
    )
    status, reply = await _turn(user_id, thread_id, "Yes")
    # F7: the card never covered a rewritten ask. The new literal is its own blocker beside the
    # refusal (M1-M3): both are said, and nothing is asked that no card answers.
    assert status == "completed" and reply.event_type == "assistant_response", reply.content
    assert reply.payload.get("choice_question") is None
    assert LENGTH in reply.content and '"Galatasaray": ' in reply.content
    assert _head_strategy(thread_id) is None and not _pending(thread_id)


# The review's stated 30 s is refused by the planner's own check first (a 14 s clip), which has
# its own card. A length the planner keeps and readiness's re-check refuses, as prod's did, is
# what reaches this hold.
_LENGTH_ASK = {"kind": "timing", "scope": "global", "description": "10 seconds"}


@pytest.mark.asyncio
async def test_a_mixed_hold_names_both_blockers_and_offers_no_card(monkeypatch):
    """The review's input: one 14 s clip, a title literal the plan lacks and a stated length."""
    user_id, thread_id = _seed("en")
    _readiness_refuses(monkeypatch)
    _model(
        monkeypatch,
        updates=[
            {"kind": "text", "scope": "title", "literal": "Fenerbahçe"},
            {**_LENGTH_ASK, "facts": {"duration_s": ASKED_S}},
        ],
        opening_title=None,
    )
    status, held = await _turn(user_id, thread_id, 'Make it 10 seconds with "Fenerbahçe" on it')
    # M1: no card, so no "yes" can agree to leaving the title out.
    assert status == "completed" and held.event_type == "assistant_response", held.content
    assert held.payload.get("choice_question") is None
    # M2/M3: the length and the title are both said; the question itself is not asked.
    assert LENGTH in held.content and '"Fenerbahçe": ' in held.content
    assert not any(ask in held.content for ask in SIMPLER_ASKS)
    readiness = held.payload["brief_coverage"]["approval_readiness"]
    by_kind = {
        row["requirement_id"]: row["requirement_id"]
        in readiness["policy_refusal"]["requirement_ids"]
        for row in readiness["requirements"]
    }
    assert sorted(by_kind.values()) == [False, True]  # the length is the refusal's, not the title

    # A "yes" after it is a plain message: still held, never drafted.
    _model(monkeypatch, opening_title=None)
    status, again = await _turn(user_id, thread_id, "Yes")
    assert status == "completed" and again.event_type == "assistant_response", again.content
    assert _head_strategy(thread_id) is None and not _pending(thread_id)


@pytest.mark.asyncio
async def test_a_stated_length_the_refusal_changes_keeps_the_card(monkeypatch):
    """M4: the stated length is the refusal's own subject, not a blocker of its own."""
    user_id, thread_id = _seed("en")
    _readiness_refuses(monkeypatch)
    _model(
        monkeypatch,
        updates=[
            {"kind": "text", "scope": "title", "literal": "Fenerbahçe"},
            {**_LENGTH_ASK, "facts": {"duration_s": ASKED_S}},
        ],
    )
    status, held = await _turn(user_id, thread_id, 'Make it 10 seconds with "Fenerbahçe" on it')
    assert status == "completed" and held.event_type == "assistant_response", held.content
    assert held.content == f"{LENGTH} {SIMPLER_ASKS[0]}"
    assert held.payload["choice_question"]["dropped_fields"] == ["target_duration_s"]


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["add", "change", "partial_change"])
async def test_evet_beside_a_restated_ask_drafts_the_simpler_version(monkeypatch, operation):
    """R1: the plan behind "Evet" repeats the saved title; it is not a new request."""
    user_id, thread_id, _, held = await _hold(monkeypatch, "tr")
    with sync_session() as db:
        (title,) = load_latest_brief_sync(db, thread_id).live()
    restated = {"kind": "text", "scope": "title", "literal": "Fenerbahçe"}
    if operation == "change":
        restated = {**restated, "operation": "change", "target_requirement_id": title.id}
    elif operation == "partial_change":
        restated = {
            "operation": "change",
            "target_requirement_id": title.id,
            "literal": "Fenerbahçe",
        }
    _model(monkeypatch, updates=[restated])
    status, drafted = await _turn(user_id, thread_id, "Evet")
    assert status == "awaiting_approval", drafted.content
    assert drafted.event_type == "draft_applied"
    assert _head_strategy(thread_id)["target_duration_s"] == KEPT_S
    assert held.payload["choice_question"]["dropped_fields"] == ["target_duration_s"]
    with sync_session() as db:
        assert [req.literal for req in load_latest_brief_sync(db, thread_id).live()] == [
            "Fenerbahçe"
        ]
