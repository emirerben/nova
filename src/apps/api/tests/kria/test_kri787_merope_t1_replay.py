"""KRI-787: the Merope café T1 Talking chat, replayed offline from approval to the worker.

Prod 042c405c9, Main Creator 2026-10-19-v64, 2026-10-21, thread 32A5B608, plan item
b5477ee9: one 68.37 s take. "Talking-to-camera edit of Caro at Merope. Start at "Hi, I'm
Karolina, I'm from Ecuador" and end right after "I'm gonna be here all week". Cut her slip
... Captions for both voices, and spell these right: ... Title: ... Show "MEROPE" in big
letters the first time she says Merope. ... About 35 seconds." Four creator turns, two
approved drafts, two renders refused at dispatch in under a second (job 0701fcc9 after
"Make this simpler version", job f88555bc after a typed "Continue without a fixed order"):
"I can't verify this ordering rule from the approved media." No video.

``tests/fixtures/kri787_merope_t1_turns.json`` is the run as prod recorded it: the chat's
events (messages, the order question, both drafts' receipts and approval cards, both
failure cards), each render's approved draft strategy, the approved brief and the
creator's recorded answer, the take's record (speech segments) and word timings, and each
job's stored contract and failure. Nothing is paraphrased: every assertion that names a
prod value reads it from there. Turns 1-3 run through the real runtime in
``test_kri787_merope_t1_runtime_postgres`` (same fixture); this module needs no database.

How it fails after approval, step by step (failure modes first, KRI-787):

1. The approval backstop (``_unresolved_choice_plan``) builds the contract from the draft
   strategy, which carries the creator's ``attachment_order`` answer: nothing is
   unresolved, so the approval card goes out.
2. Dispatch (``_dispatch_item_render`` over the real ``build_generative_job``) drops the
   answer: the factory erases client ``choice_answers`` and the binding rebuild never puts
   the approved one back, so r2 stays an ordering rule nobody can verify. Rebound to prod's
   generation id, its contract digest equals the one prod stored, for both jobs. KRI-788.
3. The worker's first gate (``_run_generative_job_impl`` → ``check_phone_dispatch_contract``)
   refuses with prod's failure reason, message and decline. KRI-788.
4. The observer posts prod's "Your video wasn't made" card, which blames the iPhone and
   offers options it does not show. KRI-795.
5. Typing the other option's label after the question closed is not read as a new answer:
   no choice is derived and the gate keeps ``attachment_order``. KRI-794.

``test_prod_*`` cases retain the historical incorrect values in the immutable fixture
and now require the corrected current boundary. This pass retains every current test function and
parameter case; nine previously proven acceptance cases are hard requirements.
The one-take typed-answer case requires recognized selection and no invented clip-order
contract; multi-clip replacement/closed/forged/stale-answer checks remain in KRI-794.
Named phrase cuts remain explicitly partial, never fulfilled by a clearer refusal.

Seams a child PR may route through (one place each, so the replay follows the fix):

- ``turn1_brief_updates()``: the Main Creator's recorded turn-1 brief updates, through the
  server's own ``parse_brief_updates``.
- ``kept_window(job)``: what the phone Talking worker keeps of the take (the KRI-628
  opening cut and KRI-790's end cut).
- ``chat_before_turn_4()``: the chat the server reads at turn 4, with the failure card the
  observer posts today in place of prod's (so KRI-795's re-asked picker reaches turn 4).

Speech cleanup (chosen at approval in prod) is off in the dispatch here: it never reads or
writes the contract, and prod's contract digest is reproduced without it.
"""

from __future__ import annotations

import contextlib
import contextvars
import copy
import json
import uuid
from contextlib import ExitStack
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.config import settings
from app.kria import planner
from app.kria.brief import CreativeBrief, parse_brief_updates
from app.kria.brief_binding import BriefBinding, snapshot_media
from app.kria.contracts import KriaToolIntent, KriaTurnPlan
from app.kria.planner import PlannedKriaTurn
from app.pipeline.transcribe import Word
from app.services.choice_questions import tag_event
from app.services.creator_render_contract import CreatorRenderContract, read_render_contract
from app.tasks import generative_build as gb
from app.tasks.content_plan_build import _dispatch_item_render

FIXTURE = json.loads(
    (Path(__file__).resolve().parents[1] / "fixtures" / "kri787_merope_t1_turns.json").read_text(
        encoding="utf-8"
    )
)
RENDERS = FIXTURE["renders"]
JOBS = [render["job_id"][:8] for render in RENDERS]
TAKE = FIXTURE["take"]["media_id"]
TAKE_S = FIXTURE["take"]["duration_s"]
CREATOR_ID = uuid.UUID(FIXTURE["creator_id"])
THREAD_ID = uuid.UUID(FIXTURE["thread_id"])
ITEM_ID = uuid.UUID(FIXTURE["plan_item_id"])
EVENTS = {event["sequence"]: event for event in FIXTURE["events"]}
ORDER_REFUSAL = "I can't verify this ordering rule from the approved media."
# The kit's ground truth for the take (T1 README): where the quoted lines are said.
START_LINE_S = 12.0  # "Hi, I'm Karolina, I'm from Ecuador" (prod segment 12.04-13.58)
END_LINE_S = 50.4  # "... I'm gonna be here all week" (prod segment ends 50.30, words 50.38)


def prod_flags(monkeypatch: pytest.MonkeyPatch) -> None:
    """The prod settings this chat ran under (phone pilot, brief binding, clip intents)."""
    for name, value in {
        "phone_rendering_enabled": True,
        "phone_render_user_ids": [],
        "kria_runtime_v2_phone_enabled": True,
        "kria_runtime_v2_phone_user_ids": [],
        "guided_edit_capability_enabled": True,
        "subtitled_archetype_enabled": True,
        "clip_intents_enabled": True,
        "clip_facts_enabled": True,
        "kria_creative_brief_enabled": True,
        "kria_creative_brief_user_ids": [],
        "kria_brief_binding_enabled": True,
        "kria_brief_binding_user_ids": [],
        "kria_choice_questions_enabled": True,
        "kria_plan_authority_enabled": True,
        # The device text features this chat proves prod had verified: the Talking title was
        # placed (KRI-467 needs both).
        "phone_render_verified_features": ["positionedText", "animatedText"],
    }.items():
        monkeypatch.setattr(settings, name, value)


@pytest.fixture(autouse=True)
def _prod(monkeypatch: pytest.MonkeyPatch) -> None:
    prod_flags(monkeypatch)


# -- the recorded project ------------------------------------------------------------------


def t1_item() -> SimpleNamespace:
    """The plan item as dispatch reads it: one phone take (the analysis proxy) on Talking."""
    row = copy.deepcopy(FIXTURE["take_row"])
    return SimpleNamespace(
        id=ITEM_ID,
        clip_gcs_paths=[row["gcs_path"]],
        clip_assignments=[row],
        filming_guide=[],
        theme="",
        idea="Untitled video",
        edit_format="subtitled",
        audio_mode="kria",
        voiceover_gcs_path=None,
        landscape_fit="fit",
        montage_preset="classic",
        voiceover_bed_level=None,
        voiceover_caption_style=None,
        smart_sound_design_enabled=True,
        speech_cleanup_enabled=False,
        speech_cleanup_notice=None,
        current_job_id=None,
        edit_proposal=None,
    )


def t1_media() -> dict:
    """The media snapshot approval pins for the item (``snapshot_media``)."""
    return snapshot_media(t1_item())


def t1_brief() -> CreativeBrief:
    """The brief both approvals pinned (v2: r2 answered, r7 dropped by "simpler version")."""
    return CreativeBrief.model_validate(copy.deepcopy(FIXTURE["binding"]["brief"]))


def t1_stored_brief_v2() -> CreativeBrief:
    """The thread's brief after "Make this simpler version" (turn 3): v1 with r7 removed.

    The thread keeps what the creator typed; only the copy approval pins is answered
    (``answered_brief``), so r2 here is still the creator's start/end line.
    """
    brief = copy.deepcopy(FIXTURE["brief_v1"])
    brief["version"] = 2
    for requirement in brief["requirements"]:
        if requirement["id"] == "r7":
            requirement["status"] = "superseded"
    return CreativeBrief.model_validate(brief)


def turn1_brief_updates() -> list:
    """The Main Creator's turn-1 brief updates, through the server's parser (a seam)."""
    return parse_brief_updates(
        [
            {
                "kind": req["kind"],
                "scope": req["scope"],
                "literal": req["literal"],
                "description": req["description"],
                "facts": req["facts"],
            }
            for req in FIXTURE["brief_v1"]["requirements"]
        ]
    )


def approved_binding(*, answers: list | None = None, brief: CreativeBrief | None = None) -> dict:
    """The BriefBinding approval pins: brief, media snapshot and the creator's answers."""
    return BriefBinding.create(
        THREAD_ID,
        brief if brief is not None else t1_brief(),
        latest_message="Make this simpler version",
        media_snapshot=t1_media(),
        choice_answers=copy.deepcopy(
            FIXTURE["binding"]["choice_answers"] if answers is None else answers
        ),
    ).model_dump(mode="json")


def without_answers(strategy: dict) -> dict:
    return {key: value for key, value in copy.deepcopy(strategy).items() if key != "choice_answers"}


# -- approval → dispatch → worker → chat ----------------------------------------------------


def thread_rows(*, before: int) -> list[tuple]:
    """Prod's chat as the database returns it: (role, payload, event type, content)."""
    return [
        (event["actor"], event["payload"], event["kind"], event["content"])
        for sequence, event in sorted(EVENTS.items())
        if sequence < before and event["actor"] in {"user", "assistant"}
    ]


def approval_blocker(strategy: dict, brief: CreativeBrief) -> str | None:
    """The runtime's approval backstop (``_unresolved_choice_plan``): None = approval offered."""
    from app.tasks.kria_runtime import _unresolved_choice_plan  # noqa: PLC0415

    blocked = _unresolved_choice_plan(
        strategy,
        brief,
        t1_media(),
        contract_brief=brief,
        events=[tag_event(*row) for row in thread_rows(before=10)],
        has_draft=True,
        creator_id=CREATOR_ID,
    )
    return None if blocked is None else blocked[0].response


def dispatch(
    monkeypatch: pytest.MonkeyPatch,
    render: dict,
    *,
    strategy: dict | None = None,
    binding: dict | None = None,
):
    """``execute_kria_approval`` → ``dispatch_item_render_for`` on an approved draft.

    The real ``_dispatch_item_render``, the real phone source receipt check and the real job
    factory; only the broker and the database session are stand-ins (the item and the
    thread lookup).
    """
    item = t1_item()
    plan = SimpleNamespace(
        id=uuid.uuid4(), user_id=CREATOR_ID, preference_summary="", ownership_epoch=0
    )
    session = MagicMock()
    session.execute.return_value.scalar_one.return_value = 0
    session.execute.return_value.all.return_value = []
    binding = approved_binding() if binding is None else binding
    session.execute.return_value.scalar_one_or_none.return_value = SimpleNamespace(
        id=uuid.UUID(binding["thread_id"]), state={"reply_language": "en"}
    )
    with ExitStack() as stack:
        stack.enter_context(
            patch(
                "app.services.smart_captions.resolve_smart_captions_context_sync", return_value=None
            )
        )
        stack.enter_context(
            patch(
                "app.services.edit_proposals.validate_approved_proposal_media_sync",
                return_value=(None, None),
            )
        )
        stack.enter_context(patch("app.services.job_dispatch.enqueue_orchestrator_sync"))
        result = _dispatch_item_render(
            session,
            item,
            plan,
            {"tone": "natural and direct", "content_pillars": ["real-life moments"]},
            ownership_epoch=0,
            bypass_guided_edit_gate=True,
            allow_phone_unapproved_montage=True,
            phone_speech_cleanup_unattended=True,
            creator_strategy=copy.deepcopy(
                render["draft_strategy"] if strategy is None else strategy
            ),
            creator_request=render["job_creator_request"],
            creator_brief_binding=binding,
        )
    assert result.outcome == "dispatched", result
    job = session.add.call_args.args[0]
    job.id = job.id or uuid.UUID(render["job_id"])  # the INSERT assigns it in prod
    return job


def dispatched_contract(job) -> CreatorRenderContract:  # noqa: ANN001
    contract = read_render_contract(job.assembly_plan)
    assert contract is not None
    return contract


@dataclass(frozen=True)
class WorkerOutcome:
    failure_reason: str | None
    message: str | None
    decline: dict | None
    route: str | None


def prod_refusal(render: dict) -> WorkerOutcome:
    """What prod's worker left on the job (``_fail_job``'s inputs)."""
    failure = render["prod_failure"]
    decline = {k: v for k, v in failure["creator_decline"].items() if k != "failure_reason"}
    return WorkerOutcome(failure["failure_reason"], failure["error_detail"], decline, None)


def run_worker(monkeypatch: pytest.MonkeyPatch, job) -> WorkerOutcome:  # noqa: ANN001
    """``_run_generative_job_impl`` on the dispatched job, up to the phone route it picks.

    Only the database (the locked row, commits, the failure write) and the routes' own
    bodies are stand-ins: the contract read and ``check_phone_dispatch_contract`` are real.
    """
    seen: dict = {}

    @contextlib.contextmanager
    def session():  # noqa: ANN202
        yield SimpleNamespace(commit=lambda: None)

    def fail(
        job_id, error_detail, failure_reason=None, speech_cleanup_failure_reason=None, decline=None
    ):  # noqa: ANN001, ANN202, ARG001
        seen.update(failure_reason=failure_reason, message=error_detail, decline=decline)
        return True

    def route(name):  # noqa: ANN001, ANN202
        def reached(*_args, **_kwargs):  # noqa: ANN002, ANN003, ANN202
            seen["route"] = name

        return reached

    job.status = "queued"
    monkeypatch.setattr(gb, "_sync_session", session)
    monkeypatch.setattr(gb, "_lock_owned_entry_job", lambda _db, _job_id: (job, 1))
    monkeypatch.setattr(gb, "mark_failed_phase", lambda _job_id: None)
    monkeypatch.setattr(gb, "_fail_job", fail)
    monkeypatch.setattr(gb, "_shadow_route", lambda *_a, **_k: None)
    monkeypatch.setattr(gb, "_run_phone_subtitled_job", route("subtitled"))
    monkeypatch.setattr(gb, "_run_phone_narrated_job", route("narrated"))
    monkeypatch.setattr(gb, "_run_phone_guided_job", route("guided"))
    # Run in a copy of the context: the Celery wrapper resets the plan-ownership fence the
    # worker sets, and calling the impl directly would leak it into later tests.
    contextvars.copy_context().run(gb._run_generative_job_impl, str(job.id))
    return WorkerOutcome(
        failure_reason=seen.get("failure_reason"),
        message=seen.get("message"),
        decline=seen.get("decline"),
        route=seen.get("route"),
    )


def failure_card(job, outcome: WorkerOutcome) -> dict:  # noqa: ANN001
    """The chat event the real observer posts for the failed job, as ``_fail_job`` left it."""
    from app.models import CreationThreadEvent  # noqa: PLC0415
    from tests.kria.test_runtime_phone_v2 import _Db, _observe  # noqa: PLC0415

    assembly = copy.deepcopy(job.assembly_plan)
    if outcome.decline:
        assembly["creator_decline"] = {**outcome.decline, "failure_reason": outcome.failure_reason}
    failed = SimpleNamespace(
        id=job.id,
        user_id=CREATOR_ID,
        status="processing_failed",
        content_plan_ownership_epoch=1,
        failure_reason=outcome.failure_reason,
        error_detail=outcome.message,
        assembly_plan=assembly,
    )
    # The generic device-state double has no thread history. Supply this incident's
    # recorded rows to the real refusal/reask code; do not stub its result.
    before = 15 if str(job.id) == RENDERS[0]["job_id"] else 22
    history = [
        (event["actor"], event["payload"], sequence)
        for sequence, event in sorted(EVENTS.items())
        if sequence < before and event["actor"] in {"user", "assistant"}
    ]
    execute = _Db.execute

    def recorded_execute(db, statement):  # noqa: ANN001, ANN202
        columns = statement.column_descriptions
        names = tuple(column["name"] for column in columns)
        if columns and columns[0].get("entity") is CreationThreadEvent:
            if names == ("payload",):
                return SimpleNamespace(scalar_one_or_none=lambda: EVENTS[6]["payload"])
            if names == ("role", "payload", "sequence"):
                return SimpleNamespace(all=lambda: history)
        return execute(db, statement)

    with patch.object(_Db, "execute", recorded_execute):
        _outcome, _execution, events = _observe(failed, {})
    return events[-1]


def kept_window(job) -> tuple[float, float]:  # noqa: ANN001
    """What the phone Talking worker keeps of the take (a seam: the opening and end cuts).

    ``_phone_opening_cut`` reads the start line off the approved brief and grounds it in the
    take's words, as the worker does with cleanup off (KRI-704). The whole take is kept when
    it finds no ask.
    """
    words = [
        Word(text=w["text"], start_s=w["start_s"], end_s=w["end_s"], confidence=1.0)
        for w in FIXTURE["take_words"]
    ]
    plan, _receipt = gb._phone_opening_cut(
        copy.deepcopy(job.assembly_plan), None, (), duration_s=TAKE_S, off_words=words
    )
    if plan is None or not plan.keep_segments:
        return 0.0, TAKE_S
    return plan.keep_segments[0][0], plan.keep_segments[-1][1]


# -- turn 4: the free-text matcher and the server gate ----------------------------------------


def planned(strategy: dict, *, summary: str = "") -> PlannedKriaTurn:
    """A Main Creator draft + render request, before the server gate."""
    return PlannedKriaTurn(
        plan=KriaTurnPlan(
            mode="act",
            turn_value="action",
            intents=[
                KriaToolIntent(
                    intent_id="apply-strategy",
                    tool_name="draft.apply_strategy",
                    tool_version=1,
                    arguments={"summary": summary, "strategy": copy.deepcopy(strategy)},
                ),
                KriaToolIntent(
                    intent_id="request-render",
                    tool_name="render.request",
                    tool_version=1,
                    depends_on=["apply-strategy"],
                ),
            ],
        ),
        manifest_hash="a" * 64,
        context_hash="b" * 64,
        media_snapshot=t1_media(),
        brief_route="replan",
        brief_clip_ids=(TAKE,),
    )


def chat_before_turn_4(monkeypatch: pytest.MonkeyPatch) -> list[tuple]:
    """The chat the server reads when "Continue without a fixed order" arrives: prod's events,
    with the first render's failure card as the observer posts it today (a seam)."""
    card = failure_card(dispatch(monkeypatch, RENDERS[0]), prod_refusal(RENDERS[0]))
    return [
        ("assistant", card["payload"], card["event_type"], card["content"])
        if row[2] == "assistant_render_failed"
        else row
        for row in thread_rows(before=16)
    ]


async def typed_choice(message: str, rows: list[tuple]) -> dict | None:
    """``submit_turn``'s free-text matcher: the choice a typed message becomes, if any."""
    from app.kria.runtime import _free_text_choice_selection  # noqa: PLC0415

    result = MagicMock()
    result.all.return_value = rows
    db = SimpleNamespace(execute=AsyncMock(return_value=result))
    derived = await _free_text_choice_selection(db, SimpleNamespace(id=THREAD_ID), message)
    return None if derived is None else derived[0].model_dump(mode="json", exclude_none=True)


async def turn4_strategy(monkeypatch: pytest.MonkeyPatch) -> dict:
    """Turn 4 as the server runs it: the typed message (with any choice it becomes), then the
    gate on the drafted strategy."""
    message = FIXTURE["creator_messages"][3]
    rows = chat_before_turn_4(monkeypatch)
    selection = await typed_choice(message, rows)
    payload = dict(EVENTS[16]["payload"])
    if selection is not None:
        payload["choice_selection"] = selection
    events = [tag_event(*row) for row in rows]
    events.append(tag_event("user", payload, EVENTS[16]["kind"], message))
    monkeypatch.setattr(planner, "load_latest_brief", AsyncMock(return_value=t1_stored_brief_v2()))
    monkeypatch.setattr(planner, "_load_thread_events", AsyncMock(return_value=events))
    result = await planner._gate_unresolved_choices(
        SimpleNamespace(),
        planned(without_answers(RENDERS[1]["draft_strategy"])),
        thread_id=THREAD_ID,
        creator_id=CREATOR_ID,
    )
    assert result.plan.mode == "act", result.plan.response
    return copy.deepcopy(result.plan.intents[0].arguments["strategy"])


def order_answers(strategy: dict) -> list[tuple[str, str]]:
    return [
        (answer["kind"], answer["option"])
        for answer in strategy.get("choice_answers") or []
        if answer.get("kind") == "order_basis"
    ]


# == the incident as prod showed it ==========================================================


@pytest.mark.parametrize("index", [0, 1], ids=JOBS)
def test_approval_is_offered_for_both_drafts(index: int) -> None:
    """The approval backstop builds the contract from the draft strategy, which carries the
    answer: nothing is unresolved, so the approval card goes out (true before and after)."""
    assert approval_blocker(RENDERS[index]["draft_strategy"], t1_brief()) is None


@pytest.mark.parametrize("index", [0, 1], ids=JOBS)
def test_prod_failure_card(monkeypatch: pytest.MonkeyPatch, index: int) -> None:
    """Keep the historical wrong card, and require KRI-795's real recovery card."""
    render = RENDERS[index]
    card = failure_card(dispatch(monkeypatch, render), prod_refusal(render))
    assert "iPhone can't render" in render["prod_card"]["content"]
    assert "iPhone can't render" not in card["content"]
    assert "different clip or format" not in card["content"]
    assert "order" in card["content"].lower()
    question = card["payload"]["choice_question"]
    assert question["kind"] == "order_basis"
    assert {row["key"] for row in question["options"]} == {"attachment_order", "unordered"}


async def test_prod_turn_4_typed_label_keeps_the_old_answer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The historical ignored label now answers the new failure-card question."""
    rows = chat_before_turn_4(monkeypatch)
    selection = await typed_choice(FIXTURE["creator_messages"][3], rows)
    assert selection is not None and selection["option_key"] == "unordered"
    strategy = await turn4_strategy(monkeypatch)
    assert order_answers(strategy) == []  # One take has timing, no clip-order conflict.


# == what the creator should have had (child tickets) =========================================

_KRI_788 = (
    "KRI-788: the approved order answer is dropped between approval and dispatch "
    "(KRI-789's one-clip rule may land first)"
)


@pytest.mark.parametrize("index", [0, 1], ids=JOBS)
def test_the_approved_answer_reaches_the_contract(
    monkeypatch: pytest.MonkeyPatch, index: int
) -> None:
    contract = dispatched_contract(dispatch(monkeypatch, RENDERS[index]))
    assert contract.unresolved == ()
    assert (contract.order_basis, contract.order_ids) == ("attachment_order", (TAKE,))


@pytest.mark.parametrize("index", [0, 1], ids=JOBS)
def test_an_offered_approval_is_never_refused_at_dispatch(
    monkeypatch: pytest.MonkeyPatch, index: int
) -> None:
    render = RENDERS[index]
    assert approval_blocker(render["draft_strategy"], t1_brief()) is None
    outcome = run_worker(monkeypatch, dispatch(monkeypatch, render))
    assert (outcome.failure_reason, outcome.message) == (None, None)
    assert outcome.route == "subtitled"


_KRI_794 = (
    "KRI-794: a typed option label is ignored once its question has closed "
    "(KRI-795's re-asked picker may land first)"
)


async def test_typing_the_other_option_replaces_the_answer(monkeypatch: pytest.MonkeyPatch) -> None:
    selection = await typed_choice(FIXTURE["creator_messages"][3], chat_before_turn_4(monkeypatch))
    assert selection is not None and selection["option_key"] == "unordered"
    strategy = await turn4_strategy(monkeypatch)
    # KRI-789 correctly classifies this single take's start/end as timing. The
    # multi-clip replacement/closed/forged-answer cases remain in KRI-794's suite.
    assert order_answers(strategy) == []
    from app.services.choice_questions import answered_brief  # noqa: PLC0415

    binding = approved_binding(answers=[], brief=answered_brief(t1_stored_brief_v2(), strategy))
    contract = dispatched_contract(
        dispatch(monkeypatch, RENDERS[1], strategy=strategy, binding=binding)
    )
    assert contract.order_required is False and contract.unresolved == ()


async def test_the_re_answered_draft_reaches_the_talking_renderer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ "Continue without a fixed order" drops the order promise, so the draft renders even
    while KRI-788 is open."""
    from app.services.choice_questions import answered_brief  # noqa: PLC0415

    strategy = await turn4_strategy(monkeypatch)
    answers = [a for a in strategy.get("choice_answers") or [] if a.get("kind") == "order_basis"]
    binding = approved_binding(
        answers=answers, brief=answered_brief(t1_stored_brief_v2(), strategy)
    )
    job = dispatch(monkeypatch, RENDERS[1], strategy=strategy, binding=binding)
    assert dispatched_contract(job).order_required is False
    assert run_worker(monkeypatch, job).route == "subtitled"


@pytest.mark.parametrize("index", [0, 1], ids=JOBS)
def test_the_failure_card_names_the_lost_choice_and_re_asks_it(
    monkeypatch: pytest.MonkeyPatch, index: int
) -> None:
    render = RENDERS[index]
    card = failure_card(dispatch(monkeypatch, render), prod_refusal(render))
    assert "iPhone can't render" not in card["content"]
    assert "different clip or format" not in card["content"]
    assert "order" in card["content"].lower()
    question = card["payload"].get("choice_question") or {}
    assert question.get("kind") == "order_basis"
