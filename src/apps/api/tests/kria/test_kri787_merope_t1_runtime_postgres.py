"""KRI-787 (runtime half): the Merope T1 chat through the real runtime, approval to worker.

Real runtime turns (``submit_turn`` + ``run_kria_turn`` over scratch Postgres). Only the Main
Creator's model call is replaced (``planner._call_main_creator``) by the turn's output: turn
1's recorded brief updates (``test_kri787_merope_t1_replay.turn1_brief_updates``) and a draft
strategy (the approved draft's own, minus its server-owned answers; the model's turn-1/2
strategies were recorded only as hashes). Everything around it is the server's own: the
planner's handling of the model output, the question gate on the stored brief and the
thread's events, the tapped answers, the draft receipts and hold-back, "Make this simpler
version", the approval card, the approval's BriefBinding, the dispatch claim, then the real
dispatch and the worker's first gate (``test_kri787_merope_t1_replay.dispatch`` /
``run_worker``). The creator answers each question the way Yasin did ("Use the order you
added the clips", then "Make this simpler version"), whatever turn it comes on.

On 45ed79016 the three replies, every receipt and the approval card equal prod's word for
word (``test_prod_chat_word_for_word``). Failure modes, each pinned below:

1. Turn 1 asks the clip-order question on a one-take Talking edit (KRI-789).
2. The first draft is held back over "MEROPE" ("That exact text isn't in this draft"), so the
   creator can only drop it (KRI-791).
3. The length line judges the whole 68.4 s take; no line names the start or end quote
   (KRI-790).
4. A named phrase cut gets the pause-cleanup line, then "Done" once cleanup is chosen,
   though cleanup never cuts a spoken line (KRI-792).
5. Captions for both voices and the four spellings get no line at all (KRI-793).
6. The approval card goes out for a draft the worker then refuses (KRI-788).

``test_prod_*`` tests pin the incident as prod showed it; each names the tickets whose fix
changes it, and that PR retires it with its own ``xfail`` marker. Turn 4 (a typed option
label after the refusal) is replayed without a database in ``test_kri787_merope_t1_replay``
(KRI-794/795).
"""

from __future__ import annotations

import asyncio
import copy
import uuid
from dataclasses import dataclass

import pytest
import pytest_asyncio
from sqlalchemy import select, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import OperationalError

from app.agents._schemas.creator_agent import (
    CreativeStrategy,
    CreatorRenderIntentEvidence,
    ProposeStrategy,
)
from app.agents.main_creator import MainCreatorOutput
from app.config import settings
from app.database import AsyncSessionLocal, sync_session
from app.database import engine as async_engine
from app.kria import planner
from app.kria.api_schemas import ApprovalDecisionBody, ChoiceSelectionIn, SubmitTurnBody
from app.kria.runtime import approval_fingerprint, decide_approval, submit_turn
from app.models import CreationThread, CreationThreadEvent, CreatorAgentApproval, PlanItem
from app.tasks.kria_runtime import _claim_approval_dispatch, run_kria_turn
from tests.kria.test_kri787_merope_t1_replay import (
    END_LINE_S,
    EVENTS,
    FIXTURE,
    ORDER_REFUSAL,
    RENDERS,
    START_LINE_S,
    TAKE_S,
    dispatch,
    kept_window,
    prod_flags,
    run_worker,
    turn1_brief_updates,
    without_answers,
)
from tests.kria.test_runtime_postgres_integration import _seed_runtime_project

_db_name = make_url(settings.database_url).database or ""
if not _db_name.endswith("_test"):
    pytest.skip(f"refusing to write to non-test database {_db_name!r}", allow_module_level=True)
try:
    with sync_session() as _probe:
        _probe.execute(text("select 1"))
except OperationalError:
    pytest.skip("nova_test Postgres not reachable", allow_module_level=True)

MESSAGES = FIXTURE["creator_messages"]
# What the creator tapped in prod, by question (``kind`` or ``conflict``).
ANSWERS = {
    "order_basis": ("attachment_order", MESSAGES[1]),
    "simplification_requires_choice": ("simpler_version", MESSAGES[2]),
}
MEROPE_MISS = "That exact text isn't in this draft."


@pytest.fixture(autouse=True)
def _flags(monkeypatch: pytest.MonkeyPatch) -> None:
    prod_flags(monkeypatch)
    monkeypatch.setattr(settings, "kria_runtime_v2_enabled", True)
    monkeypatch.setattr(settings, "main_creator_agent_enabled", True)
    # Prod offered "Clean up speech" on this take (its receipts say "Choose Clean up speech
    # when you approve ..."): the item's preflight cohort, decided from storage-backed
    # narration state this seed does not carry.
    monkeypatch.setattr("app.tasks.kria_runtime._speech_cleanup_offered", lambda *_a: True)


@pytest_asyncio.fixture(autouse=True)
async def _dispose_async_engine():
    yield
    await async_engine.dispose()


def _seed_t1(*, cleanup_chosen: bool) -> tuple[uuid.UUID, uuid.UUID]:
    """A runtime project holding prod's take, on Talking (subtitled), as the phone made it.

    ``cleanup_chosen``: the item after the first approval chose "Clean up speech" (prod's
    second draft).
    """
    user_id, thread_id, _session_id = _seed_runtime_project()
    row = copy.deepcopy(FIXTURE["take_row"])
    with sync_session() as db:
        thread = db.get(CreationThread, thread_id)
        item = db.get(PlanItem, thread.active_plan_item_id)
        item.edit_format = "subtitled"
        item.idea = "Untitled video"
        item.clip_gcs_paths = [row["gcs_path"]]
        item.clip_assignments = [row]
        item.speech_cleanup_enabled = cleanup_chosen
        thread.state = {
            "media": [{"media_id": row["media_id"], "filename": "M2_01_footage"}],
            "media_count": 1,
            "edit_format": "subtitled",
        }
        db.commit()
    return user_id, thread_id


def _model_output(*, summary: str = "", updates: tuple = ()) -> MainCreatorOutput:
    """What the Main Creator returns: the approved draft's strategy (minus its server-owned
    answers), its title evidence quoted from the prompt, and any brief updates."""
    return MainCreatorOutput(
        action=ProposeStrategy(
            kind="propose_strategy",
            strategy=CreativeStrategy.model_validate(without_answers(RENDERS[0]["draft_strategy"])),
            summary=summary,
            render_intent_evidence=CreatorRenderIntentEvidence(
                opening_title='Title: "Colombian dessert, Ecuadorian hands"'
            ),
        ),
        brief_updates=list(updates),
    )


def _revision(thread_id: uuid.UUID) -> int:
    with sync_session() as db:
        return int(db.get(CreationThread, thread_id).revision)


def _last_reply(thread_id: uuid.UUID) -> CreationThreadEvent:
    with sync_session() as db:
        return (
            db.execute(
                select(CreationThreadEvent)
                .where(
                    CreationThreadEvent.thread_id == thread_id,
                    CreationThreadEvent.event_type.in_(["draft_applied", "assistant_response"]),
                )
                .order_by(CreationThreadEvent.sequence.desc())
            )
            .scalars()
            .first()
        )


def _pending_approval(thread_id: uuid.UUID) -> CreatorAgentApproval | None:
    with sync_session() as db:
        return db.execute(
            select(CreatorAgentApproval).where(
                CreatorAgentApproval.thread_id == thread_id,
                CreatorAgentApproval.status == "pending",
            )
        ).scalar_one_or_none()


@dataclass
class Chat:
    user_id: uuid.UUID
    thread_id: uuid.UUID
    replies: list[CreationThreadEvent]

    @property
    def approval(self) -> CreatorAgentApproval:
        approval = _pending_approval(self.thread_id)
        assert approval is not None, "no approval card was offered"
        return approval

    @property
    def approved_draft(self) -> CreationThreadEvent:
        """The reply that carried the approval card."""
        assert self.approval is not None
        return self.replies[-1]


async def _turn(
    monkeypatch: pytest.MonkeyPatch,
    chat: Chat,
    message: str,
    model_output: MainCreatorOutput,
    *,
    choice: tuple[str, str] | None = None,
) -> None:
    """One creator turn: submit (a typed message or a tapped option) and run it."""

    async def _model(*_args, **_kwargs):  # noqa: ANN002, ANN003, ANN202
        return model_output

    monkeypatch.setattr(planner, "_call_main_creator", _model)
    selection = (
        {"choice_selection": ChoiceSelectionIn(question_id=choice[0], option_key=choice[1])}
        if choice
        else {}
    )
    async with AsyncSessionLocal() as db:
        accepted, _ = await submit_turn(
            db,
            thread_id=chat.thread_id,
            creator_id=chat.user_id,
            body=SubmitTurnBody(
                message=message,
                client_event_id=f"kri787-{uuid.uuid4().hex}",
                expected_thread_revision=_revision(chat.thread_id),
                **selection,
            ),
        )
    result = await asyncio.to_thread(run_kria_turn.run, accepted.turn_id)
    assert result["status"] in {"completed", "awaiting_approval"}, result
    chat.replies.append(_last_reply(chat.thread_id))


async def _chat_until_approval(
    monkeypatch: pytest.MonkeyPatch, *, cleanup_chosen: bool = False
) -> Chat:
    """Prod's prompt, then each question answered as the creator did, until approval."""
    user_id, thread_id = _seed_t1(cleanup_chosen=cleanup_chosen)
    chat = Chat(user_id, thread_id, [])
    await _turn(monkeypatch, chat, MESSAGES[0], _model_output(updates=tuple(turn1_brief_updates())))
    for _ in ANSWERS:
        question = (chat.replies[-1].payload or {}).get("choice_question")
        if _pending_approval(thread_id) is not None or question is None:
            break
        option, message = ANSWERS[question.get("kind") or question["conflict"]]
        summary = RENDERS[0]["draft_summary"] if option == "simpler_version" else ""
        await _turn(
            monkeypatch,
            chat,
            message,
            _model_output(summary=summary),
            choice=(question["question_id"], option),
        )
    return chat


async def _approve(chat: Chat):  # noqa: ANN202
    approval = chat.approval
    with sync_session() as db:
        body = ApprovalDecisionBody(
            expected_thread_revision=db.get(CreationThread, chat.thread_id).revision,
            expected_draft_revision=approval.draft_revision,
            expected_approval_fingerprint=approval_fingerprint(approval),
        )
    async with AsyncSessionLocal() as db:
        await decide_approval(
            db,
            thread_id=chat.thread_id,
            approval_id=approval.id,
            creator_id=chat.user_id,
            decision="approve",
            body=body,
        )
    claim = await asyncio.to_thread(_claim_approval_dispatch, approval.id)
    assert claim is not None
    return claim


def _receipts(reply: CreationThreadEvent) -> dict[str, dict]:
    rows = (reply.payload or {}).get("requirement_receipts", [])
    return {row["requirement_id"]: row for row in rows}


def _prod_receipts(sequence: int) -> dict[str, dict]:
    rows = EVENTS[sequence]["payload"]["requirement_receipts"]
    return {row["requirement_id"]: row for row in rows}


def _verdicts(receipts: dict[str, dict], ids: tuple[str, ...] | None = None) -> dict:
    return {
        rid: (row["status"], row["verification"], row["reason"])
        for rid, row in receipts.items()
        if ids is None or rid in ids
    }


# == the incident as prod showed it ===========================================================


@pytest.mark.asyncio
async def test_prod_turn_1_asks_the_clip_order_question(monkeypatch: pytest.MonkeyPatch) -> None:
    """Retired by KRI-789."""
    chat = await _chat_until_approval(monkeypatch)
    first = chat.replies[0]
    assert first.content == EVENTS[6]["content"]
    assert first.payload["choice_question"]["kind"] == "order_basis"


@pytest.mark.asyncio
async def test_prod_the_length_line_judges_the_whole_take(monkeypatch: pytest.MonkeyPatch) -> None:
    """Retired by KRI-790."""
    chat = await _chat_until_approval(monkeypatch)
    assert _verdicts(_receipts(chat.approved_draft), ("r1",)) == _verdicts(
        _prod_receipts(10), ("r1",)
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("cleanup_chosen", "sequence"), [(False, 10), (True, 17)], ids=["first-draft", "cleanup-on"]
)
async def test_prod_named_cuts_get_the_pause_line_then_done(
    monkeypatch: pytest.MonkeyPatch, cleanup_chosen: bool, sequence: int
) -> None:
    """Retired by KRI-792. ``cleanup-on`` is prod's second draft (cleanup chosen at the first
    approval): the slip and the half question read "Done" with nothing cut."""
    chat = await _chat_until_approval(monkeypatch, cleanup_chosen=cleanup_chosen)
    assert _verdicts(_receipts(chat.approved_draft), ("r3", "r4")) == _verdicts(
        _prod_receipts(sequence), ("r3", "r4")
    )


@pytest.mark.asyncio
async def test_prod_the_approved_draft_keeps_the_whole_take(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Neither quoted line reaches the Talking plan: Yasin's intro and Atakan's goodbye stay
    in. Retired by KRI-789/790."""
    chat = await _chat_until_approval(monkeypatch)
    claim = await _approve(chat)
    job = dispatch(monkeypatch, RENDERS[0], strategy=claim.strategy, binding=claim.brief_binding)
    assert kept_window(job) == (0.0, TAKE_S)


# == what the creator should have had (child tickets) =========================================


@pytest.mark.xfail(
    strict=True,
    raises=AssertionError,
    reason="KRI-789: a one-take Talking edit with start/end lines asks the clip-order question",
)
@pytest.mark.asyncio
async def test_one_take_asks_no_clip_order_question(monkeypatch: pytest.MonkeyPatch) -> None:
    chat = await _chat_until_approval(monkeypatch)
    for reply in chat.replies:
        question = (reply.payload or {}).get("choice_question") or {}
        assert question.get("kind") != "order_basis", reply.content
        assert "order for the clips" not in str(reply.content)


@pytest.mark.asyncio
async def test_the_approved_draft_reaches_the_talking_renderer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    chat = await _chat_until_approval(monkeypatch)
    claim = await _approve(chat)
    job = dispatch(monkeypatch, RENDERS[0], strategy=claim.strategy, binding=claim.brief_binding)
    outcome = run_worker(monkeypatch, job)
    assert (outcome.failure_reason, outcome.message) == (None, None)
    assert outcome.route == "subtitled"


@pytest.mark.xfail(
    strict=True,
    raises=AssertionError,
    reason="KRI-790: the length line judges the whole take and ignores the quoted start/end",
)
@pytest.mark.asyncio
async def test_the_length_line_measures_the_quoted_window(monkeypatch: pytest.MonkeyPatch) -> None:
    chat = await _chat_until_approval(monkeypatch)
    reason = str(_receipts(chat.approved_draft)["r1"]["reason"] or "")
    assert "keeps your whole take" not in reason
    assert "68.4s" not in reason


@pytest.mark.xfail(
    strict=True,
    raises=AssertionError,
    reason="KRI-790: no draft line or approval card names the start and end lines",
)
@pytest.mark.asyncio
async def test_the_approval_card_names_the_start_and_end_lines(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    chat = await _chat_until_approval(monkeypatch)
    summary = chat.approval.consequence_summary.lower()
    assert "karolina" in summary or "carolina" in summary
    assert "here all week" in summary


@pytest.mark.asyncio
async def test_merope_is_placed_on_her_word_or_honestly_explained(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """MEROPE stays in the approved draft: placed on Caro's first "Merope" (≈40.0 s), or an
    honest line about the speaker; never dropped as an exact-text miss."""
    chat = await _chat_until_approval(monkeypatch)
    for reply in chat.replies:
        receipt = _receipts(reply).get("r7")
        assert receipt is None or receipt["reason"] != MEROPE_MISS, reply.content
    assert "Left out as agreed: Show MEROPE" not in chat.approval.consequence_summary
    assert "r7" in _receipts(chat.approved_draft)


@pytest.mark.xfail(
    strict=True,
    raises=AssertionError,
    reason="KRI-792: a named phrase cut is promised by the pause-cleanup line",
)
@pytest.mark.asyncio
async def test_a_named_phrase_cut_says_it_needs_the_editor(monkeypatch: pytest.MonkeyPatch) -> None:
    chat = await _chat_until_approval(monkeypatch)
    receipts = _receipts(chat.approved_draft)
    for rid in ("r3", "r4"):
        assert receipts[rid]["status"] != "met", rid
        assert receipts[rid]["reason"] != _prod_receipts(10)[rid]["reason"], rid


@pytest.mark.xfail(
    strict=True,
    raises=AssertionError,
    reason="KRI-792: with cleanup chosen a named phrase cut reads Done before any render",
)
@pytest.mark.asyncio
async def test_with_cleanup_chosen_a_named_phrase_cut_is_never_done(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    chat = await _chat_until_approval(monkeypatch, cleanup_chosen=True)
    receipts = _receipts(chat.approved_draft)
    assert receipts["r3"]["status"] != "met"
    assert receipts["r4"]["status"] != "met"


@pytest.mark.asyncio
async def test_captions_and_spellings_get_a_line(monkeypatch: pytest.MonkeyPatch) -> None:
    chat = await _chat_until_approval(monkeypatch)
    assert "r5" in _receipts(chat.approved_draft)
    for word in ("Oblea de la Casa", "dulce de leche", "leche condensada", "Merope"):
        assert word in chat.approval.consequence_summary, word


@pytest.mark.xfail(
    strict=True,
    raises=AssertionError,
    reason="KRI-788 + KRI-789/KRI-790: the quoted start and end lines become the kept window",
)
@pytest.mark.asyncio
async def test_the_video_starts_on_hi_im_karolina_and_ends_after_here_all_week(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Checklist: no Yasin intro, no Atakan part (38.4 s kept before the two short cuts), and
    the render reaches the phone."""
    chat = await _chat_until_approval(monkeypatch)
    claim = await _approve(chat)
    job = dispatch(monkeypatch, RENDERS[0], strategy=claim.strategy, binding=claim.brief_binding)
    start, end = kept_window(job)
    assert START_LINE_S - 0.3 <= start <= START_LINE_S + 0.6, start
    assert END_LINE_S - 0.3 <= end <= END_LINE_S + 0.6, end
    assert run_worker(monkeypatch, job).route == "subtitled"
