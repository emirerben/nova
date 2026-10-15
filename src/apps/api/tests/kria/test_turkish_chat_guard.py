"""KRI-667: no English sentence reaches a Turkish Kria chat.

KRI-592 and KRI-630 were each found by a phone test, one at a time. This file is the
one guard for the whole class, in two nets (failure modes: ``turkish_chat_guard.py``):

A. Runtime net (``turkish_chat_net``): wraps every v2 chat-event writer and backstops
   direct ``CreationThreadEvent(...)`` construction; any English segment written to a
   Turkish chat (content, choice options, receipts, approval summaries, plan blocks,
   change chips) fails the test. The scenarios below drive each known producer on a
   Turkish thread, no database: the real writer runs against a fake session.
B. Static net (AST, no DB): no writer, turn plan or strategy refusal is handed fixed
   English that skips ``say()``; every Turkish table has its English source's keys; every
   worker sentence the chat re-words still exists where the worker writes it.

The net also runs over the Postgres task flows in ``test_kri520_task_copy_postgres.py``.
"""

from __future__ import annotations

import ast
import importlib
import re
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest

from app.config import settings
from app.kria import plan_blocks
from app.kria.brief import BriefRequirement, CreativeBrief
from app.kria.brief_checks import PlanFacts, _check_timing, reply_from_receipts
from app.kria.contracts import RequirementReceipt
from app.kria.reply_language import reply_language_for
from app.models import CreationThread, CreatorAgentExecution, CreatorAgentSession, PlanItem
from app.services.choice_questions import (
    CONFLICT_DURATION_VS_COUNT,
    CONFLICT_ORDER_BASIS,
    CONFLICT_SOURCE_COVERAGE,
    CONFLICT_VOICE_VS_DURATION,
    CONFLICT_WHICH_VOICE,
    OPT_ATTACHMENT_ORDER,
    OPT_EXTEND,
    OPT_MATCH_VOICE,
    OPT_SILENT_TAIL,
    ChoiceResolution,
    answered_brief,
    choice_notices,
    length_inputs_waiting,
)
from app.tasks import kria_runtime
from tests.kria.turkish_chat_guard import (  # noqa: F401 - the fixture is used by name
    TurkishChatNet,
    english_segments,
    turkish_chat_net_fixture,
)

API = Path(__file__).resolve().parents[2]
APP = API / "app"


# -- fakes: the real writers run against these ------------------------------------------


class _Result:
    def __init__(self, value: Any = None) -> None:
        self._value = value

    def scalar_one(self) -> Any:
        return -1 if self._value is None else self._value

    def scalar_one_or_none(self) -> Any:
        return self._value

    def scalars(self) -> Any:
        return SimpleNamespace(all=lambda: [])


class _SyncDb:
    """Enough of a sync Session for `_append_sync_event` (sequence, add, flush)."""

    def __init__(self) -> None:
        self.added: list[Any] = []

    def execute(self, _statement: Any) -> _Result:
        return _Result()

    def add(self, row: Any) -> None:
        self.added.append(row)

    def flush(self) -> None:
        return None


class _AsyncDb:
    """An AsyncSession double answering ``execute`` from a queue (then empty results)."""

    def __init__(self, *results: Any) -> None:
        self._results = list(results)
        self.added: list[Any] = []

    async def execute(self, _statement: Any) -> _Result:
        return _Result(self._results.pop(0) if self._results else None)

    def add(self, row: Any) -> None:
        self.added.append(row)

    async def flush(self) -> None:
        return None

    async def commit(self) -> None:
        return None


def _thread(language: str | None = "tr") -> SimpleNamespace:
    state = {"reply_language": language} if language else {}
    return SimpleNamespace(id=uuid.uuid4(), revision=1, state=state, creator_id=uuid.uuid4())


_THREAD_TR = _thread("tr")


def _write(content: str | None, *, event_type: str = "assistant_response", **payload: Any) -> None:
    """Write one turn event through the REAL (net-wrapped) `_append_sync_event`."""
    kria_runtime._append_sync_event(
        _SyncDb(),
        _THREAD_TR,
        role="assistant",
        event_type=event_type,
        content=content,
        payload=payload,
    )


# -- the classifier itself (it must be able to fail, and not fail on literals) ---------


def test_the_classifier_flags_server_english_and_keeps_creator_literals() -> None:
    assert english_segments("I restored the previous draft. Nothing was rendered.") == [
        "I restored the previous draft.",
        "Nothing was rendered.",
    ]
    for short in ("Not used", "Lip-sync", "Background", "Your song", "3 clips · 24s"):
        assert english_segments(short), short
    for turkish in (
        "Tempo: Hızlı",
        "Format: Günlük vlog",
        "whisking.mov ile aç; hikâye ilk andan net bir görsel bakış açısı kazanır.",
        'iPhone\'unda açıp "Retry" düğmesine dokun.',
        "Render this draft: Hızlı tempolu günlük vlog",
        "Kullanılmadı",
        "3 klip · 24 sn",
        "“Summer in Rome” başlığını açılışa koydum.",
    ):
        assert english_segments(turkish) == [], turkish
    # The creator's own words are never a leak, quoted or not.
    assert english_segments("Köşeye SUMMER VIBES yazdım.", ["SUMMER VIBES"]) == []
    assert english_segments("Yapıldı: Keep 24 seconds (extended from 15 so every clip fits)")


def test_the_net_records_english_only_in_a_turkish_chat() -> None:
    """Control: the same English is a leak in a Turkish chat and nothing in an English one."""
    net = TurkishChatNet()
    english, turkish = _thread("en"), _thread("tr")
    for thread in (english, turkish):
        net.inspect(
            writer="test",
            thread=thread,
            role="assistant",
            event_type="assistant_response",
            content="I left out on-screen text that wasn't in your words.",
            payload={"choice_question": {"options": [{"label": "Keep my length"}]}},
        )
    assert [leak.path for leak in net.leaks] == ["content", "choice_question.options[0].label"]
    # A user's own message is never checked; its words become literals.
    net.inspect(
        writer="test",
        thread=turkish,
        role="user",
        event_type="user_message",
        content="Make it pop",
        payload={},
    )
    assert len(net.leaks) == 2 and "Make it pop" in net.literals


def test_the_net_catches_a_direct_construction_through_the_thread_it_saw(
    turkish_chat_net: TurkishChatNet,
) -> None:
    """The `CreationThreadEvent(...)` backstop finds the chat's language by thread id."""
    from app.models import CreationThreadEvent

    thread = CreationThread(id=uuid.uuid4(), state={"reply_language": "tr"}, revision=1)
    CreationThreadEvent(
        thread_id=thread.id,
        sequence=1,
        revision=2,
        role="assistant",
        event_type="assistant_response",
        content="Bunu hazırladım. I kept the order you set.",
        payload={},
    )
    assert [leak.writer for leak in turkish_chat_net.leaks] == ["CreationThreadEvent()"]
    turkish_chat_net.leaks.clear()


# -- A. runtime scenarios: each known producer, on a Turkish thread --------------------


@pytest.mark.asyncio
async def test_undo_says_what_it_restored_in_turkish(
    monkeypatch: pytest.MonkeyPatch, turkish_chat_net: TurkishChatNet
) -> None:
    """Leak 1: `draft_undone` content."""
    from app.kria import drafts

    thread = _thread("tr")
    target = SimpleNamespace(
        thread=thread,
        item=SimpleNamespace(id=uuid.uuid4()),
        job=None,
        variant_key="original_text",
        generation_id=None,
    )
    head = SimpleNamespace(id=uuid.uuid4(), draft_revision=3, parent_draft_id=uuid.uuid4())
    parent = SimpleNamespace(id=head.parent_draft_id, snapshot_json={"kind": "initial"})

    async def _target(*_a: Any, **_k: Any) -> Any:
        return target

    async def _head(*_a: Any, **_k: Any) -> Any:
        return head

    async def _insert(*_a: Any, **_k: Any) -> Any:
        return SimpleNamespace(id=uuid.uuid4(), draft_revision=4)

    async def _owned(*_a: Any, **_k: Any) -> Any:
        return thread

    async def _successor(*_a: Any, **_k: Any) -> None:
        return None

    monkeypatch.setattr(drafts, "_target", _target)
    monkeypatch.setattr(drafts, "_head", _head)
    monkeypatch.setattr(drafts, "_insert_revision", _insert)
    monkeypatch.setattr(drafts, "_owned_thread", _owned)
    monkeypatch.setattr(drafts, "_out", lambda row, can_undo: row)
    monkeypatch.setattr(drafts, "_promote_queued_successor", _successor)
    # pending approvals (none), the parent draft, the approvals to cancel (none).
    db = _AsyncDb(None, parent, None)
    await drafts.undo_draft(db, thread_id=thread.id, creator_id=uuid.uuid4(), expected_revision=3)

    [event] = [row for row in db.added if getattr(row, "event_type", None) == "draft_undone"]
    assert event.content and turkish_chat_net.checked
    turkish_chat_net.assert_clean()


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["create_item", "extracted_supersede", "extracted_forget"])
async def test_memory_receipts_are_turkish(kind: str, turkish_chat_net: TurkishChatNet) -> None:
    """Leak 2: `memory_updated` is built directly as `CreationThreadEvent(...)`."""
    from app.services.creator_memory_learning import append_memory_receipt

    thread = CreationThread(
        id=uuid.uuid4(), creator_id=uuid.uuid4(), revision=1, state={"reply_language": "tr"}
    )
    operation = SimpleNamespace(
        id=uuid.uuid4(),
        actor_kind="system",
        operation_kind=kind,
        undo_expires_at=datetime.now(UTC) + timedelta(hours=1),
        resulting_revision=2,
    )
    db = _AsyncDb(thread, None)  # the locked thread, no earlier receipt
    await append_memory_receipt(
        db,
        user_id=thread.creator_id,
        thread_id=thread.id,
        operation=operation,
        candidate="explicit",
    )
    [event] = db.added
    assert event.event_type == "memory_updated" and turkish_chat_net.checked == 1
    turkish_chat_net.assert_clean()


@pytest.mark.asyncio
async def test_dropped_corner_text_is_explained_in_turkish(
    monkeypatch: pytest.MonkeyPatch, turkish_chat_net: TurkishChatNet
) -> None:
    """Leak 3: the planner appends a note when it drops corner text the creator never wrote."""
    from app.agents._schemas.creator_agent import CreatorRenderIntentEvidence, ProposeStrategy
    from app.agents.main_creator import MainCreatorOutput
    from app.kria import planner
    from app.kria.strategy_policy import CheckedStrategy
    from app.schemas.edit_proposal import PinnedText
    from tests.kria.test_strategy_policy import _narrated, _narrated_manifest

    monkeypatch.setattr(settings, "clip_intents_enabled", False)
    monkeypatch.setattr(
        planner,
        "check_strategy_for_runtime_v2",
        lambda _manifest, strategy, **_kw: CheckedStrategy(strategy=strategy, notices=()),
    )
    message = "Seslendirmemle bir video yap"
    output = MainCreatorOutput(
        action=ProposeStrategy(
            kind="propose_strategy",
            strategy=_narrated(pinned_texts=[PinnedText(text="SUMMER VIBES", corner="top_left")]),
            summary="Seslendirmeli bir düzenleme hazırladım.",
            render_intent_evidence=CreatorRenderIntentEvidence(),
        )
    )
    inputs = SimpleNamespace(
        agent_input=SimpleNamespace(conversation=[{"role": "user", "content": message}]),
        creative_copy_state={},
        creative_copy_digest="d" * 24,
        intent_clips=[],
        creator_request=message,
        raw_creator_request=message,
    )
    with reply_language_for("tr"):
        planned = await planner._plan_creator_action(
            None,
            thread_id=uuid.uuid4(),
            item_id=uuid.uuid4(),
            creator_id=uuid.uuid4(),
            user_message=message,
            manifest=_narrated_manifest(),
            inputs=inputs,
            output=output,
        )
    summary = planned.plan.intents[0].arguments["summary"]
    assert summary.startswith("Seslendirmeli bir düzenleme hazırladım.")
    assert summary != "Seslendirmeli bir düzenleme hazırladım."  # the note is there
    _write(summary, event_type="draft_applied")
    turkish_chat_net.assert_clean()


def test_corner_text_refusal_asks_in_turkish(turkish_chat_net: TurkishChatNet) -> None:
    """Leak 5: the pinned-text refusal for a format without a corner-text lane."""
    from app.kria.strategy_policy import _refusal_question
    from app.services.creator_errors import CreatorStrategyError
    from tests.kria.test_strategy_policy import _talking

    exc = CreatorStrategyError(
        "pinned_texts is not supported by the subtitled renderer",
        code="unsupported_treatment",
        edit_format="subtitled",
    )
    with reply_language_for("tr"):
        refused = _refusal_question(exc, _talking())
    assert refused.code == "pinned_text_unavailable"
    _write(refused.question, event_type="assistant_question")
    turkish_chat_net.assert_clean()


def test_waiting_on_a_length_choice_is_turkish(turkish_chat_net: TurkishChatNet) -> None:
    """Leak 6: `length_inputs_waiting` (shown as the turn's progress reply)."""
    exhausted = ChoiceResolution(
        strategy={},
        answers=(),
        question=None,
        exhausted=(SimpleNamespace(kind=CONFLICT_DURATION_VS_COUNT),),  # type: ignore[arg-type]
    )
    coverage = ChoiceResolution(
        strategy={}, answers=({"kind": CONFLICT_SOURCE_COVERAGE},), question=None
    )
    with reply_language_for("tr"):
        texts = [length_inputs_waiting(exhausted), length_inputs_waiting(coverage)]
    assert all(texts)
    for text in texts:
        _write(text, turn_value="progress")
    turkish_chat_net.assert_clean()


def test_every_answered_choice_is_disclosed_in_turkish(turkish_chat_net: TurkishChatNet) -> None:
    """Leak 7: disclosures for voice length, silent tail, which voice, ``length_N``."""
    answers = [
        {"kind": CONFLICT_VOICE_VS_DURATION, "option": OPT_MATCH_VOICE, "source": "creator"},
        {"kind": CONFLICT_VOICE_VS_DURATION, "option": OPT_SILENT_TAIL, "source": "creator"},
        {"kind": CONFLICT_WHICH_VOICE, "option": "clip_2", "source": "creator_delegated"},
        {"kind": CONFLICT_VOICE_VS_DURATION, "option": "length_45", "source": "creator"},
    ]
    with reply_language_for("tr"):
        notes = choice_notices({"choice_answers": answers})
    english = choice_notices({"choice_answers": answers})
    # Each answer is disclosed in both languages, including the dynamic length_N key.
    assert len(notes) == len(english) == 4
    assert english[3] == "I'm making it 45 seconds."
    _write("Seslendirmeli bir düzenleme hazırladım. " + " ".join(notes), event_type="draft_applied")
    turkish_chat_net.assert_clean()


def test_answered_requirements_are_reviewed_in_turkish(turkish_chat_net: TurkishChatNet) -> None:
    """Leak 8: `answered_brief` rewrites a requirement the review line then quotes."""
    brief = CreativeBrief(
        version=2,
        requirements=[
            BriefRequirement(
                id="r1",
                kind="timing",
                scope="global",
                description="15 saniye olsun",
                facts={"duration_s": 15},
            ),
            BriefRequirement(
                id="r2",
                kind="order",
                scope="global",
                description="çektiğim sırayla",
                facts={"key": "capture_time"},
            ),
        ],
    )
    strategy = {
        "target_duration_s": 24,
        "choice_answers": [
            {"kind": CONFLICT_DURATION_VS_COUNT, "option": OPT_EXTEND, "requirement_ids": ["r1"]},
            {
                "kind": CONFLICT_ORDER_BASIS,
                "option": OPT_ATTACHMENT_ORDER,
                "requirement_ids": ["r2"],
            },
        ],
    }
    english = answered_brief(brief, strategy)
    assert english.requirements[0].description == (
        "Keep 24 seconds (extended from 15 so every clip can be seen)"
    )
    assert english.requirements[1].description == "in the order the clips were added"
    with reply_language_for("tr"):
        answered = answered_brief(brief, strategy)
        receipts = [
            RequirementReceipt(requirement_id=req.id, status="met", verification="checked")
            for req in answered.requirements
        ]
        review = reply_from_receipts(answered, receipts, summary="Videon hazır.")
    _write(review, event_type="assistant_review")
    turkish_chat_net.assert_clean()


def test_text_that_ends_early_is_reported_in_turkish(turkish_chat_net: TurkishChatNet) -> None:
    """Leak 9: the whole-video text timing receipt reason."""
    req = BriefRequirement(
        id="r1",
        kind="timing",
        scope="title",
        description="başlık video boyunca kalsın",
        facts={"persistent": True},
    )
    facts = PlanFacts(editor=True, duration_s=20.0, text_spans=(("t1", "title", 0.0, 5.0),))
    with reply_language_for("tr"):
        receipt = _check_timing(req, facts)
    assert receipt.status == "partial"
    _write(
        "Videon hazır.",
        event_type="assistant_review",
        requirement_receipts=[receipt.model_dump(mode="json")],
    )
    turkish_chat_net.assert_clean()


# -- observer scenarios (leaks 10-12): the real `_observe_dispatched_execution` ---------


class _ObserverDb:
    """`sync_session` double: `get` by model, `execute` rows in the observer's order."""

    def __init__(self, gets: dict, executes: list) -> None:
        self._gets = gets
        self._executes = list(executes)

    def get(self, model: Any, _identity: Any) -> Any:
        return self._gets[model]

    def execute(self, _statement: Any) -> Any:
        if not self._executes:
            return SimpleNamespace(all=lambda: [], scalar_one_or_none=lambda: None)
        row = self._executes.pop(0)
        return SimpleNamespace(scalar_one_or_none=lambda: row)

    def commit(self) -> None:
        return None


def _observe_in_turkish(net: TurkishChatNet, job: SimpleNamespace, **execution: Any) -> list:
    user_id = job.user_id
    plan = SimpleNamespace(id=uuid.uuid4(), user_id=user_id, ownership_epoch=1)
    item = SimpleNamespace(id=uuid.uuid4(), content_plan_id=plan.id, current_job_id=job.id)
    job.content_plan_item_id = item.id
    session = SimpleNamespace(
        id=uuid.uuid4(),
        creator_id=user_id,
        plan_item_id=item.id,
        target_job_id=job.id,
        ownership_epoch=1,
        status="rendering",
        target_variant_id=None,
        target_generation_id=None,
        last_good=None,
        last_error=None,
    )
    thread = SimpleNamespace(
        id=uuid.uuid4(),
        runtime_version=2,
        creator_id=user_id,
        active_creator_agent_session_id=session.id,
        active_plan_item_id=item.id,
        active_job_id=None,
        state={"reply_language": "tr"},
    )
    turn = SimpleNamespace(
        id=uuid.uuid4(), status="observing", completed_at=None, error=None, observed_event_id=None
    )
    row = SimpleNamespace(
        **{
            "id": uuid.uuid4(),
            "status": "dispatched",
            "target_job_id": job.id,
            "turn_id": turn.id,
            "session_id": session.id,
            "target_thread_id": thread.id,
            "target_draft_id": None,
            "target_variant_id": None,
            "target_generation_id": None,
            "result": {},
            "error": None,
            "completed_at": None,
            "observed_at": None,
            "observed_event_id": None,
            **execution,
        }
    )
    db = _ObserverDb(
        {CreatorAgentExecution: row, CreatorAgentSession: session, PlanItem: item},
        [plan, item, job, session, turn, row, thread],
    )
    events: list = []

    def _record(_db: Any, _thread: Any, **kwargs: Any) -> Any:
        events.append(kwargs)
        return SimpleNamespace(id=uuid.uuid4())

    @contextmanager
    def _sessions() -> Any:
        yield db

    with (
        patch.object(kria_runtime, "sync_session", _sessions),
        patch.object(kria_runtime, "_append_sync_event", net.wrap_sync(_record)),
        patch.object(kria_runtime, "_promote_queued_successor_sync", return_value=None),
    ):
        outcome, _ = kria_runtime._observe_dispatched_execution(row.id)
    assert outcome == "failed"
    return events


def _cloud_job(code: str, decline: dict[str, str], detail: str) -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        status="processing_failed",
        content_plan_ownership_epoch=1,
        failure_reason=code,
        error_detail=detail,
        assembly_plan={"variants": [], "creator_decline": {**decline, "failure_reason": code}},
    )


def test_a_phone_decline_adds_its_way_forward_in_turkish(turkish_chat_net: TurkishChatNet) -> None:
    """Leak 10: a deterministic phone decline appends the worker's English alternative."""
    from app.services.creator_render_contract import _PHONE_REPAIR

    job = _cloud_job(
        "phone_plan_unsupported",
        {"decline_reason": "evidence_missing", "alternative": _PHONE_REPAIR},
        "Clip 3 has no speech.",
    )
    [event] = _observe_in_turkish(turkish_chat_net, job)
    assert event["event_type"] == "assistant_render_failed"
    turkish_chat_net.assert_clean()


def test_an_unknown_worker_alternative_is_dropped_not_shown_in_english(
    turkish_chat_net: TurkishChatNet,
) -> None:
    """Leak 10: a worker sentence with no Turkish twin never reaches a Turkish chat."""
    job = _cloud_job(
        "phone_plan_unsupported",
        {"decline_reason": "evidence_missing", "alternative": "Ask for a brand new thing."},
        "",
    )
    [event] = _observe_in_turkish(turkish_chat_net, job)
    turkish_chat_net.assert_clean()
    assert "brand new" not in event["content"]


@pytest.mark.parametrize(
    "reason", ["no_speech", "spine_too_short", "spine_extraction_failed", "silent_audio"]
)
def test_a_cloud_capability_refusal_is_turkish(
    reason: str, turkish_chat_net: TurkishChatNet
) -> None:
    """Leak 11: the speech-fallback declines show the job's own English error_detail."""
    from app.tasks.generative_build import _SPEECH_FALLBACK_DECLINES

    message, alternative = _SPEECH_FALLBACK_DECLINES[reason]
    job = _cloud_job(
        "creator_render_contract_unsupported",
        {"decline_reason": "evidence_missing", "alternative": alternative},
        message,
    )
    [event] = _observe_in_turkish(turkish_chat_net, job)
    assert event["payload"]["decline_reason"] == "evidence_missing"
    turkish_chat_net.assert_clean()


@pytest.mark.parametrize("reason", ["evidence_missing", "needs_choice", "capability_unavailable"])
def test_a_refused_phone_edit_is_turkish(reason: str, turkish_chat_net: TurkishChatNet) -> None:
    """Leak 12: the contract's refusal of a phone render, recorded on the device record."""
    from app.services.creator_render_contract import (
        _ASK_FOR_CHOICE,
        CreatorRenderContractError,
    )
    from app.services.device_render import mark_device_failed, record_contract_decline
    from tests.kria.test_runtime_phone_v2 import VARIANT, _device_job

    job = _device_job()
    mark_device_failed(job, VARIANT, reason_code="export_failed", detail="")
    record_contract_decline(
        job,
        VARIANT,
        CreatorRenderContractError(
            "The approved order of clips 2 and 5 is not in this recipe.",
            decline_reason=reason,  # type: ignore[arg-type]
            alternative=_ASK_FOR_CHOICE,
        ),
        stage="publication",
    )
    [event] = _observe_in_turkish(turkish_chat_net, job, target_variant_id=VARIANT)
    assert event["event_type"] == "assistant_render_failed"
    turkish_chat_net.assert_clean()


# -- plan blocks (leak 13): `_emit` holds the thread, the builders ran in the worker ----


def test_plan_blocks_are_emitted_in_the_chats_language(
    monkeypatch: pytest.MonkeyPatch, turkish_chat_net: TurkishChatNet
) -> None:
    thread = SimpleNamespace(id=uuid.uuid4(), runtime_version=2, revision=1, state={})
    thread.state = {"reply_language": "tr"}
    job = SimpleNamespace(id=uuid.uuid4(), status="processing", mode="generative")
    rows = [uuid.uuid4(), thread]  # the turn id, then the locked thread

    class _Db:
        def get(self, _model: Any, _identity: Any) -> Any:
            return job

        def execute(self, _statement: Any) -> Any:
            row = rows.pop(0) if rows else None
            return SimpleNamespace(scalar_one_or_none=lambda: row)

        def commit(self) -> None:
            return None

    @contextmanager
    def _sessions() -> Any:
        yield _Db()

    written: list = []
    monkeypatch.setattr(plan_blocks, "sync_session", _sessions)
    monkeypatch.setattr(plan_blocks, "_thread_id_for", lambda _db, _job: thread.id)
    monkeypatch.setattr(plan_blocks, "load_plan_events", lambda _db, _thread_id: [])
    monkeypatch.setattr(
        kria_runtime,
        "_append_sync_event",
        turkish_chat_net.wrap_sync(lambda _db, _thread, **kw: written.append(kw)),
    )
    # Built the way the render worker builds them: no chat language bound.
    blocks = [
        plan_blocks._summary_block(
            {"story_timeline": [{}, {}, {}], "resolved_duration_s": 24}, "clips"
        ),
        plan_blocks._summary_block({"narration_label_text_elements": [{}, {}]}, "captions"),
        plan_blocks._summary_block({"user_song": {"mode": "lipsync"}}, "music"),
        plan_blocks._summary_block({"editor_sound_effects": [{}]}, "sfx"),
        plan_blocks._skipped("overlays"),
        plan_blocks._summary_block(
            {"text_elements": [{"id": "t", "text": "Summer in Rome"}]}, "title"
        ),
    ]
    assert plan_blocks._emit(job.id, blocks) is not None
    [event] = written
    assert [b["section_id"] for b in event["payload"]["blocks"]] == [
        "clips",
        "captions",
        "music",
        "sfx",
        "overlays",
        "title",
    ]
    # The title is the creator's words and stays as written.
    assert event["payload"]["blocks"][-1]["summary"] == "Summer in Rome"
    turkish_chat_net.assert_clean()


def test_plan_blocks_stay_byte_identical_in_an_english_chat(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    thread = SimpleNamespace(id=uuid.uuid4(), runtime_version=2, revision=1, state={})
    job = SimpleNamespace(id=uuid.uuid4(), status="processing", mode="generative")
    rows = [uuid.uuid4(), thread]

    class _Db:
        def get(self, _model: Any, _identity: Any) -> Any:
            return job

        def execute(self, _statement: Any) -> Any:
            row = rows.pop(0) if rows else None
            return SimpleNamespace(scalar_one_or_none=lambda: row)

        def commit(self) -> None:
            return None

    @contextmanager
    def _sessions() -> Any:
        yield _Db()

    written: list = []
    monkeypatch.setattr(plan_blocks, "sync_session", _sessions)
    monkeypatch.setattr(plan_blocks, "_thread_id_for", lambda _db, _job: thread.id)
    monkeypatch.setattr(plan_blocks, "load_plan_events", lambda _db, _thread_id: [])
    monkeypatch.setattr(
        kria_runtime, "_append_sync_event", lambda _db, _thread, **kw: written.append(kw)
    )
    blocks = [
        plan_blocks._summary_block(
            {"story_timeline": [{}, {}, {}], "resolved_duration_s": 24}, "clips"
        ),
        plan_blocks._summary_block({"user_song": {"mode": "lipsync"}}, "music"),
        plan_blocks._skipped("overlays"),
    ]
    plan_blocks._emit(job.id, blocks)
    [event] = written
    assert [(b["summary"], b["detail"]) for b in event["payload"]["blocks"]] == [
        ("3 clips · 24s", None),
        ("Your song", "Lip-sync"),
        ("Not used", None),
    ]


# -- B. static net -------------------------------------------------------------------

_WRITERS = {"_append_sync_event", "_append_event", "CreationThreadEvent"}
# Creator-facing text handed to the turn plan and the strategy refusal (the turn task
# writes them as the event content).
_TEXT_ARGUMENTS = {
    **{writer: {"content"} for writer in _WRITERS},
    "KriaTurnPlan": {"response"},
    "RefusedStrategy": {"question"},
}
_SAY = {"say", "_thread_say"}
_LETTER = re.compile(r"[^\W\d_]", re.UNICODE)

# Intentional English handed to a writer, with why. Keyed by (file, function).
# (Empty today: the "Never localized" items are machine payload values and HTTP problem
# messages, which never pass through `content=`.)
_FIXED_TEXT_ALLOWLIST: dict[tuple[str, str], str] = {}


def _callee(call: ast.Call) -> str | None:
    func = call.func
    if isinstance(func, ast.Name):
        return func.id
    return func.attr if isinstance(func, ast.Attribute) else None


def _module_values(tree: ast.Module) -> dict[str, ast.AST]:
    out: dict[str, ast.AST] = {}
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    out[target.id] = node.value
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and node.value:
            out[node.target.id] = node.value
    return out


def _fixed_text(node: ast.AST, constants: dict, local: dict) -> bool:
    """Is this value hard-coded words (not picked by `say()` or passed in)?"""
    if isinstance(node, ast.Constant):
        return isinstance(node.value, str) and bool(_LETTER.search(node.value))
    if isinstance(node, ast.JoinedStr):
        return any(
            isinstance(part, ast.Constant) and _LETTER.search(str(part.value))
            for part in node.values
        )
    if isinstance(node, ast.BinOp):
        return _fixed_text(node.left, constants, local) or _fixed_text(node.right, constants, local)
    if isinstance(node, ast.BoolOp):
        return any(_fixed_text(value, constants, local) for value in node.values)
    if isinstance(node, ast.IfExp):
        return _fixed_text(node.body, constants, local) or _fixed_text(
            node.orelse, constants, local
        )
    if isinstance(node, ast.Subscript) and isinstance(node.value, ast.Dict):
        return any(_fixed_text(v, constants, local) for v in node.value.values if v is not None)
    if isinstance(node, ast.Call):
        if _callee(node) in _SAY:
            return False
        # "...".strip() / .format() keep the literal's words.
        return isinstance(node.func, ast.Attribute) and _fixed_text(
            node.func.value, constants, local
        )
    if isinstance(node, ast.Name):
        if node.id in local:
            return any(_fixed_text(value, constants, {}) for value in local[node.id])
        if node.id.lstrip("_").isupper() and node.id in constants:
            return _fixed_text(constants[node.id], constants, {})
    return False


def _fixed_english_handed_to_writers() -> list[str]:
    problems: list[str] = []
    for path in sorted(APP.rglob("*.py")):
        tree = ast.parse(path.read_text())
        constants = _module_values(tree)
        relative = str(path.relative_to(API))
        for func in [
            n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef | ast.AsyncFunctionDef)
        ]:
            local: dict[str, list[ast.AST]] = {}
            for node in ast.walk(func):
                if isinstance(node, ast.Assign):
                    for target in node.targets:
                        if isinstance(target, ast.Name):
                            local.setdefault(target.id, []).append(node.value)
            for call in [n for n in ast.walk(func) if isinstance(n, ast.Call)]:
                keys = _TEXT_ARGUMENTS.get(_callee(call) or "", set())
                for keyword in call.keywords:
                    if keyword.arg not in keys:
                        continue
                    if not _fixed_text(keyword.value, constants, local):
                        continue
                    if (relative, func.name) in _FIXED_TEXT_ALLOWLIST:
                        continue
                    problems.append(
                        f"{relative}:{call.lineno} {func.name}: {_callee(call)}"
                        f"({keyword.arg}={ast.unparse(keyword.value)[:80]})"
                    )
    return problems


def test_no_writer_or_turn_plan_is_handed_fixed_english() -> None:
    """A fixed sentence a Turkish chat would show in English goes through `say()`."""
    problems = _fixed_english_handed_to_writers()
    assert not problems, "fixed English, wrap it in say(en=..., tr=...):\n" + "\n".join(problems)


# Turkish tables whose English source lives in another module or is several tables.
_CROSS_MODULE_SOURCES: dict[str, tuple[str, ...]] = {
    "app.tasks.kria_runtime._PHONE_GATE_MESSAGES_TR": (
        "app.tasks.content_plan_build.PHONE_GATE_MESSAGES",
    ),
    "app.tasks.kria_runtime._JOB_FAILURE_MESSAGES_TR": (
        "app.tasks.content_plan_build.JOB_FAILURE_MESSAGES",
    ),
    "app.tasks.kria_runtime._DISPATCH_REFUSALS_TR": (
        "app.tasks.kria_runtime._SPEECH_CLEANUP_DISPATCH_REFUSALS",
        "app.tasks.kria_runtime._VISUALS_DISPATCH_REFUSALS",
    ),
}
# Turkish tables keyed by the English text itself or by a machine value: there is no
# English table to compare with (a missing key falls back to the creator's own words, an
# id, or a generic Turkish line, never to English copy).
_LOOKUP_TABLES = frozenset(
    {
        "app.kria.strategy_policy._KNOWN_TR",  # exact English notice text
        "app.kria.strategy_policy._SHAPE_NAMES_TR",
        "app.kria.brief_checks._SPOT_TR",
        "app.kria.brief_checks._BASIS_TR",  # machine ordering basis
        "app.kria.brief_checks._FORMAT_TR",  # machine edit format
        "app.kria.brief_checks._REASON_TR",  # exact English reason text
        "app.kria.brief_checks._OUTCOME_REASON_TR",  # exact English outcome text
        "app.services.choice_questions._COVERAGE_LABELS_TR",
        "app.services.kria_editor_ops_diff._ENTRANCE_TR",
        "app.services.kria_editor_ops_diff._CASE_TR",
        "app.services.kria_editor_ops_diff._SIDE_TR",
        # Reads creator words (a parser vocabulary beside `_NUMBER_WORDS_EN`), not copy.
        "app.services.phone_reaction_grounding._NUMBER_WORDS_TR",
        "app.tasks.kria_runtime._PACING_TR",
        "app.tasks.kria_runtime._EDIT_FORMAT_TR",
        "app.tasks.kria_runtime._VARIANT_NAMES_TR",  # unknown ids are left out on purpose
        "app.tasks.kria_runtime._WORKER_TEXT_TR",  # exact worker text (checked below)
        "app.kria.plan_blocks._BLOCK_TEXT_TR",  # exact block summary text
        "app.kria.plan_blocks._COUNT_NOUNS_TR",  # nouns of `_count_label`
    }
)


def _turkish_tables() -> list[tuple[str, str]]:
    """(module, name) of every module-level ``*_TR`` dict the chat copy reads."""
    files = [
        *sorted((APP / "kria").rglob("*.py")),
        *sorted((APP / "services").rglob("*.py")),
        APP / "tasks" / "kria_runtime.py",
    ]
    found = []
    for path in files:
        module = ".".join(path.relative_to(API).with_suffix("").parts)
        for name in _module_values(ast.parse(path.read_text())):
            if name.endswith("_TR"):
                found.append((module, name))
    return found


def _resolve(dotted: str) -> Any:
    module, _, name = dotted.rpartition(".")
    return getattr(importlib.import_module(module), name)


def test_every_turkish_table_has_the_keys_of_its_english_source() -> None:
    missing: list[str] = []
    unclassified: list[str] = []
    for module_name, name in _turkish_tables():
        module = importlib.import_module(module_name)
        table = getattr(module, name)
        if not isinstance(table, dict):
            continue  # a pattern or a word set, not copy
        dotted = f"{module_name}.{name}"
        if dotted in _LOOKUP_TABLES:
            continue
        if dotted in _CROSS_MODULE_SOURCES:
            sources = [_resolve(source) for source in _CROSS_MODULE_SOURCES[dotted]]
        else:
            base = name.removesuffix("_TR")
            names = (base, base.lstrip("_"), f"{base}_EN")
            twins = [n for n in dict.fromkeys(names) if hasattr(module, n)]
            sources = [getattr(module, n) for n in twins if isinstance(getattr(module, n), dict)]
        if not sources:
            unclassified.append(dotted)
            continue
        english = set().union(*(set(source) for source in sources))
        if english - set(table):
            missing.append(f"{dotted}: {sorted(map(str, english - set(table)))}")
    assert not missing, "Turkish tables missing keys of their English source:\n" + "\n".join(
        missing
    )
    assert not unclassified, (
        "new Turkish table: name its English source in _CROSS_MODULE_SOURCES or list it in "
        f"_LOOKUP_TABLES with why: {unclassified}"
    )


def _app_string_constants(exclude: Path) -> set[str]:
    strings: set[str] = set()
    for path in APP.rglob("*.py"):
        if path == exclude:
            continue
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                strings.add(node.value)
    return strings


def test_every_worker_sentence_the_chat_rewords_still_exists_and_is_covered() -> None:
    """The exact-text twins in `_WORKER_TEXT_TR` must match what the workers write."""
    from app.services import cloud_render_contract as cloud
    from app.services import creator_render_contract as contract
    from app.tasks import generative_build

    table = kria_runtime._WORKER_TEXT_TR
    stale = sorted(set(table) - _app_string_constants(APP / "tasks" / "kria_runtime.py"))
    assert not stale, f"worker text changed; update its Turkish twin: {stale}"
    required = {
        contract._ASK_FOR_CHOICE,
        contract._PHONE_REPAIR,
        contract._ASK_PHONE,
        contract._SOUNDTRACK_TWICE.alternative,
        contract._VOICE_CONFLICT.alternative,
        cloud._ASK_CHRONOLOGICAL,
        cloud._ASK_CAMERA_AUDIO,
        generative_build._PLAN_REPAIR,
        *(text for pair in generative_build._SPEECH_FALLBACK_DECLINES.values() for text in pair),
    }
    assert not required - set(table), sorted(required - set(table))
