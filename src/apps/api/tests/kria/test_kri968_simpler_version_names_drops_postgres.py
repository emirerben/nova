"""KRI-968: "Make this simpler version" names what it leaves out, and the review says so.

Don Julio walk-in T1 (prod a2426885d, thread F0F3D5FB, 2026-10-26; parent KRI-964). The
Talking prompt asked for an "ATAKAN" name tag "when he first appears". The first draft was
held back with five receipt lines (three "Partly") and "Should I try a different approach,
or make this simpler version?"; nothing said which line the simpler version drops. After
the tap the draft and approval card said "Left out as agreed: name tag when he first
appears ("ATAKAN")", and the after-render review never mentioned the tag.

The chat runs through the real runtime over scratch Postgres (``submit_turn`` +
``run_kria_turn``), from prod's state just before the held draft: brief v3 (r1-r7 exactly as
prod pinned them, r4 still open), prod's earlier events (the prompt, the opening-line
question and tap, the length question), the take's clip assignment and the approved draft
strategy (``tests/fixtures/kri968_don_julio_walkin_t1_simpler.json``). Only the Main
Creator's model call is replaced (``planner._call_main_creator``, as in the KRI-787 replay)
by that strategy, its title evidence quoted from the prompt, and no brief updates (the brief
did not change on these two turns in prod). Everything around it is the server's own: the
planner, the receipts and hold-back, the tap, the approval card, the binding and dispatch
claim. The after-render review runs the real review builder on prod's rendered variant,
with the binding and draft this chain approved.

Failure modes (enumerated before the fix; docs/reviews/kri-968-simpler-version-names-drops.md):
F1 the offer names other ids than the unmet ones; F2 agreement claimed for an id the offer
did not name; F3 the unmet set changed between offer and tap; F4 the review omits the
agreed omission; F5/F6 the automatic-omission and strategy-refusal paths change (their own
suites, plus the refusal-question check below); F7 a legacy offer without a list (prod's own
event 10) stops being accepted or keeps claiming "as agreed"; F8 Turkish copy; F9 a second
offer in the same thread; F10 the left-out line printed under "Have a look at these in the
video:"; F11 an omitted id live again is listed; F12 nothing left live drops the line.
"""

from __future__ import annotations

import asyncio
import copy
import json
import uuid
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace

import pytest
import pytest_asyncio
from sqlalchemy import select, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import OperationalError

import app.tasks.kria_runtime as kria_runtime
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
from app.kria.brief import BriefUpdate, CreativeBrief, load_latest_brief_sync
from app.kria.reply_language import reply_language_for
from app.kria.runtime import RuntimeFailure, approval_fingerprint, decide_approval, submit_turn
from app.models import (
    CreationThread,
    CreationThreadEvent,
    CreativeBriefVersion,
    CreatorAgentApproval,
    CreatorAgentExecution,
    CreatorEditDraft,
    PlanItem,
)
from app.services.choice_questions import simpler_version_question
from app.tasks.kria_runtime import _claim_approval_dispatch, run_kria_turn
from tests.kria.test_runtime_postgres_integration import _seed_runtime_project

_db_name = make_url(settings.database_url).database or ""
if not _db_name.endswith("_test"):
    pytest.skip(f"refusing to write to non-test database {_db_name!r}", allow_module_level=True)
try:
    with sync_session() as _probe:
        _probe.execute(text("select 1"))
except OperationalError:
    pytest.skip("nova_test Postgres not reachable", allow_module_level=True)

FIXTURE = json.loads(
    (
        Path(__file__).resolve().parents[1] / "fixtures" / "kri968_don_julio_walkin_t1_simpler.json"
    ).read_text(encoding="utf-8")
)
EVENTS = {event["sequence"]: event for event in FIXTURE["events"]}
HELD = EVENTS[10]  # the held first draft that offered the simpler version
TAP = EVENTS[11]  # "Make this simpler version"
DRAFT = EVENTS[12]  # "Here's the simpler version." ... "Left out as agreed: ..."
CARD = EVENTS[13]  # the approval card
REVIEW = EVENTS[20]  # the after-render review
TAG = 'name tag when he first appears ("ATAKAN")'
# Prod asked the KRI-968 question in the words before KRI-983.
PROD_ASK = "Should I try a different approach, or make this simpler version?"
ASK = "Should I make it without these, or do you want to change your request?"
LOOK = "Have a look at these in the video:"
# The phone Talking writer reads none of the draft's story structure, pacing or direction, so the
# card names them for every account (KRI-1097 route registry). The held draft's reply has a hard
# length limit: there the three are named on one line instead of cut mid-sentence.
HEADS_UP_LINES = [
    "Heads-up for this video:",
    "- I can't follow the planned story structure (Open on the question 'what do you think…, "
    "Capture Atakan's enthusiastic reply, End the edit right after 'Nice!') on this video yet, so "
    "it won't change the order of the clips",
    "- The balanced pacing isn't applied on this video yet: the video follows your speech",
    "- The native direction isn't applied on this video yet: the video follows your speech",
]
HELD_HEADS_UP = (
    "Heads-up for this video:\n- Also not applied on this video yet: the planned story "
    "structure, the planned pacing, the planned direction"
)


@pytest.fixture(autouse=True)
def _flags(monkeypatch: pytest.MonkeyPatch) -> None:
    """The prod settings this chat ran under (phone pilot, brief binding, choice questions)."""
    for name, value in {
        "kria_runtime_v2_enabled": True,
        "main_creator_agent_enabled": True,
        "phone_rendering_enabled": True,
        "phone_render_user_ids": [],
        "kria_runtime_v2_phone_enabled": True,
        "kria_runtime_v2_phone_user_ids": [],
        "subtitled_archetype_enabled": True,
        "kria_creative_brief_enabled": True,
        "kria_creative_brief_user_ids": [],
        "kria_brief_binding_enabled": True,
        "kria_brief_binding_user_ids": [],
        "kria_choice_questions_enabled": True,
        "phone_render_verified_features": ["positionedText", "animatedText"],
    }.items():
        monkeypatch.setattr(settings, name, value)


@pytest_asyncio.fixture(autouse=True)
async def _dispose_async_engine():
    yield
    await async_engine.dispose()


# -- the recorded project -------------------------------------------------------------------


def _seed_t1(*, language: str = "en", through: int = 8) -> tuple[uuid.UUID, uuid.UUID]:
    """A runtime project at prod's state after event ``through``: 8 is just before the held
    draft; 10 is prod's held draft itself (the offer as the old code stored it)."""
    user_id, thread_id, _session_id = _seed_runtime_project()
    row = copy.deepcopy(FIXTURE["take_row"])
    with sync_session() as db:
        thread = db.get(CreationThread, thread_id)
        item = db.get(PlanItem, thread.active_plan_item_id)
        item.edit_format = "subtitled"
        item.idea = "Untitled video"
        item.clip_gcs_paths = [row["gcs_path"]]
        item.clip_assignments = [row]
        thread.state = {
            "media": [{"media_id": row["media_id"], "filename": "T1_01_footage"}],
            "media_count": 1,
            "edit_format": "subtitled",
            "reply_language": language,
        }
        db.add(
            CreativeBriefVersion(
                thread_id=thread_id,
                version=FIXTURE["brief_v3"]["version"],
                requirements=copy.deepcopy(FIXTURE["brief_v3"]["requirements"]),
            )
        )
        # Prod's prompt, the opening-line question and tap, and the length question: the
        # question gate reads the creator's answers from these, as it did in prod.
        sequence, revision = 2, int(thread.revision)
        for prod in (EVENTS[n] for n in (4, 6, 7, 8, 9, 10) if n <= through):
            revision += 1
            user = prod["actor"] == "user"
            db.add(
                CreationThreadEvent(
                    thread_id=thread_id,
                    sequence=sequence,
                    revision=revision,
                    role="user" if user else "assistant",
                    event_type="user_message" if user else "assistant_response",
                    content=prod["content"],
                    payload=copy.deepcopy(prod["payload"]),
                )
            )
            sequence += 1
        thread.revision = revision
        db.commit()
    return user_id, thread_id


def _strategy(**changes) -> dict:
    return {**copy.deepcopy(FIXTURE["draft_strategy"]), **changes}


def _planned(strategy: dict, *updates: BriefUpdate) -> MainCreatorOutput:
    """What the Main Creator returns: prod's approved draft strategy, its title evidence
    quoted from the prompt, and any brief updates (none on prod's two turns)."""
    return MainCreatorOutput(
        action=ProposeStrategy(
            kind="propose_strategy",
            strategy=CreativeStrategy.model_validate(strategy),
            summary="",
            render_intent_evidence=CreatorRenderIntentEvidence(
                opening_title="Title: “Before the meat”"
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


async def _turn(
    monkeypatch: pytest.MonkeyPatch,
    user_id: uuid.UUID,
    thread_id: uuid.UUID,
    message: str,
    model_output: MainCreatorOutput,
    *,
    choice: tuple[str, str] | None = None,
) -> CreationThreadEvent:
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
            thread_id=thread_id,
            creator_id=user_id,
            body=SubmitTurnBody(
                message=message,
                client_event_id=f"kri968-{uuid.uuid4().hex}",
                expected_thread_revision=_revision(thread_id),
                **selection,
            ),
        )
    result = await asyncio.to_thread(run_kria_turn.run, accepted.turn_id)
    assert result["status"] in {"completed", "awaiting_approval"}, result
    return _last_reply(thread_id)


async def _held_draft(monkeypatch, user_id, thread_id, **strategy) -> CreationThreadEvent:  # noqa: ANN001
    """Prod's turn 3: the creator taps "Use the available 10.2 seconds" (event 9)."""
    return await _turn(
        monkeypatch,
        user_id,
        thread_id,
        EVENTS[9]["content"],
        _planned(_strategy(**strategy)),
        choice=(
            EVENTS[9]["payload"]["choice_selection"]["question_id"],
            EVENTS[9]["payload"]["choice_selection"]["option_key"],
        ),
    )


async def _tap_simpler(monkeypatch, user_id, thread_id, offer, **strategy) -> CreationThreadEvent:  # noqa: ANN001
    """Prod's turn 4: the creator taps "Make this simpler version" on ``offer``."""
    return await _turn(
        monkeypatch,
        user_id,
        thread_id,
        TAP["content"],
        _planned(_strategy(**strategy)),
        choice=(offer.payload["choice_question"]["question_id"], "simpler_version"),
    )


def _pending_approval(thread_id: uuid.UUID) -> CreatorAgentApproval | None:
    with sync_session() as db:
        return db.execute(
            select(CreatorAgentApproval).where(
                CreatorAgentApproval.thread_id == thread_id,
                CreatorAgentApproval.status == "pending",
            )
        ).scalar_one_or_none()


def _head_coverage(thread_id: uuid.UUID) -> dict:
    with sync_session() as db:
        draft = db.execute(
            select(CreatorEditDraft).where(
                CreatorEditDraft.thread_id == thread_id, CreatorEditDraft.is_head.is_(True)
            )
        ).scalar_one()
        return dict(draft.snapshot_json.get("brief_coverage") or {})


async def _decide(user_id: uuid.UUID, thread_id: uuid.UUID, decision: str) -> uuid.UUID:
    approval = _pending_approval(thread_id)
    assert approval is not None, "no approval card was offered"
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
            decision=decision,
            body=body,
        )
    return approval.id


async def _approve_and_review(user_id: uuid.UUID, thread_id: uuid.UUID) -> str:
    """Approve, claim the dispatch, then build the after-render review on prod's variant."""
    approval_id = await _decide(user_id, thread_id, "approve")
    claim = await asyncio.to_thread(_claim_approval_dispatch, approval_id)
    assert claim is not None
    variant = copy.deepcopy(FIXTURE["rendered_variant"])
    with sync_session() as db, ExitStack() as language:
        thread = db.get(CreationThread, thread_id)
        kria_runtime._bind_thread_language(language, thread)
        execution = db.get(CreatorAgentExecution, claim.execution_id)
        job = SimpleNamespace(
            id=uuid.UUID(FIXTURE["job_id"]),
            assembly_plan={"variants": [variant], "creator_brief_binding": claim.brief_binding},
            all_candidates={},
        )
        text, _receipts = kria_runtime._approved_generation_review(
            db,
            thread,
            job,
            variant,
            execution,
            kria_runtime._render_ready_review_copy(variant["variant_id"]),
        )
    return text


def _verdicts(rows: list[dict]) -> dict[str, tuple]:
    return {r["requirement_id"]: (r["status"], r["verification"], r["reason"]) for r in rows}


# == the incident as prod recorded it ==========================================================


def test_prod_offer_named_nothing_and_the_card_claimed_agreement() -> None:
    """The immutable record: an offer with no list, then "as agreed", then a silent review."""
    assert "leaves out" not in HELD["content"]
    assert set(HELD["payload"]["choice_question"]) == {
        "options",
        "version",
        "conflict",
        "question_id",
        "allow_free_text",
    }
    assert f"- Partly: {TAG} (That exact text isn't in this draft)" in HELD["content"]
    assert TAP["payload"]["choice_selection"]["option_key"] == "simpler_version"
    for shown in (DRAFT["content"], CARD["payload"]["consequence_summary"]):
        assert shown.endswith(
            f"{LOOK}\n- No text on faces\n- Use the available 10.2 seconds\n"
            f"- Left out as agreed: {TAG}"
        )
    assert "ATAKAN" not in REVIEW["content"]


# == F1: the offer names exactly what the simpler version leaves out ===========================


@pytest.mark.asyncio
async def test_the_held_draft_names_what_the_simpler_version_leaves_out(monkeypatch) -> None:
    user_id, thread_id = _seed_t1()
    held = await _held_draft(monkeypatch, user_id, thread_id)

    # The replay reproduces prod's held draft: every receipt verdict, word for word.
    assert _verdicts(held.payload["requirement_receipts"]) == _verdicts(
        HELD["payload"]["requirement_receipts"]
    )
    assert held.payload["brief_coverage"]["reason"] == "simplification_requires_choice"
    # Prod's reply, plus one sentence naming the one requirement the simpler version drops.
    assert held.content == HELD["content"].replace(
        f"I held this first draft back. {PROD_ASK}",
        f"{HELD_HEADS_UP}\nI held this first draft back. The simpler version leaves out: {TAG}. "
        f"{ASK}",
    )
    question = held.payload["choice_question"]
    assert question["omitted_requirement_ids"] == ["r4"]
    assert question["brief_version"] == 3
    assert "dropped_fields" not in question  # not the KRI-759 policy-refusal question (F6)


# == F2 + F10: the tap drops only what was named, and says so outside "have a look" ============


@pytest.mark.asyncio
async def test_the_tap_leaves_out_only_the_named_tag(monkeypatch) -> None:
    user_id, thread_id = _seed_t1()
    held = await _held_draft(monkeypatch, user_id, thread_id)
    draft = await _tap_simpler(monkeypatch, user_id, thread_id, held)

    assert draft.event_type == "draft_applied"
    assert _verdicts(draft.payload["requirement_receipts"]) == _verdicts(
        DRAFT["payload"]["requirement_receipts"]
    )
    lines = draft.content.splitlines()
    assert lines.count(f"- Left out as agreed: {TAG}") == 1
    # F10: the line sits with the Done/Partly lines, never under "Have a look".
    assert lines.index(f"- Left out as agreed: {TAG}") < lines.index(LOOK)
    assert sorted(draft.content.splitlines()) == sorted(
        [*DRAFT["content"].splitlines(), *HEADS_UP_LINES]
    )
    assert _pending_approval(thread_id).consequence_summary == f"Render this draft: {draft.content}"
    with sync_session() as db:
        brief = load_latest_brief_sync(db, thread_id)
    assert brief.version == 4
    assert [r.id for r in brief.requirements if not r.live] == ["r4"]
    coverage = _head_coverage(thread_id)
    assert coverage["accepted_omission_ids"] == ["r4"]
    assert coverage["agreed_omission_ids"] == ["r4"]


# == F4: the after-render review says what was left out ========================================


@pytest.mark.asyncio
async def test_the_review_lists_the_agreed_omission(monkeypatch) -> None:
    user_id, thread_id = _seed_t1()
    held = await _held_draft(monkeypatch, user_id, thread_id)
    await _tap_simpler(monkeypatch, user_id, thread_id, held)
    review = await _approve_and_review(user_id, thread_id)

    # Prod's review word for word, plus the left-out line before "Have a look".
    assert review == REVIEW["content"].replace(
        f"\n{LOOK}", f"\n- Left out as agreed: {TAG}\n{LOOK}"
    )


# == F3: the unmet set changed between the offer and the tap ===================================


@pytest.mark.asyncio
async def test_a_changed_unmet_set_is_offered_again_not_dropped(monkeypatch) -> None:
    user_id, thread_id = _seed_t1()
    held = await _held_draft(monkeypatch, user_id, thread_id)
    # The tap turn's draft also loses the title: r2 is now unmet too, and was never named.
    again = await _tap_simpler(monkeypatch, user_id, thread_id, held, opening_title=None)

    assert again.event_type == "assistant_response"
    assert "Left out as agreed" not in again.content
    assert again.content.endswith(
        f'The simpler version leaves out: Title ("Before the meat"); {TAG}. {ASK}'
    )
    assert again.payload["choice_question"]["omitted_requirement_ids"] == ["r2", "r4"]
    assert (
        again.payload["choice_question"]["question_id"]
        != held.payload["choice_question"]["question_id"]
    )
    assert _pending_approval(thread_id) is None
    with sync_session() as db:
        assert [r.id for r in load_latest_brief_sync(db, thread_id).live()] == [
            "r1",
            "r2",
            "r3",
            "r4",
            "r5",
            "r6",
            "r7",
        ]


# == F7: prod's own offer (no list) still works, without claiming an agreement ================


@pytest.mark.asyncio
async def test_a_legacy_offer_is_accepted_without_claiming_agreement(monkeypatch) -> None:
    """Prod's own held draft (event 10, stored with no list) and prod's tap (event 11)."""
    user_id, thread_id = _seed_t1(through=10)
    draft = await _turn(
        monkeypatch,
        user_id,
        thread_id,
        TAP["content"],
        _planned(_strategy()),
        choice=(
            TAP["payload"]["choice_selection"]["question_id"],
            TAP["payload"]["choice_selection"]["option_key"],
        ),
    )

    assert draft.event_type == "draft_applied"
    assert "Left out as agreed" not in draft.content
    assert f"- Left out in the simpler version: {TAG}" in draft.content.splitlines()
    coverage = _head_coverage(thread_id)
    assert coverage["accepted_omission_ids"] == ["r4"]
    assert coverage["agreed_omission_ids"] == []
    review = await _approve_and_review(user_id, thread_id)
    assert f"- Left out in the simpler version: {TAG}" in review.splitlines()
    assert "as agreed" not in review


# == F8: Turkish ================================================================================


@pytest.mark.asyncio
async def test_turkish_offer_draft_and_review_name_the_drop(monkeypatch) -> None:
    user_id, thread_id = _seed_t1(language="tr")
    held = await _held_draft(monkeypatch, user_id, thread_id)
    assert f"Daha basit sürümde çıkarılacak: {TAG}." in held.content
    assert "leaves out" not in held.content
    draft = await _turn(
        monkeypatch,
        user_id,
        thread_id,
        "Daha basit sürümü hazırla",
        _planned(_strategy()),
        choice=(held.payload["choice_question"]["question_id"], "simpler_version"),
    )
    assert f"- Kararlaştırdığımız gibi çıkarıldı: {TAG}" in draft.content.splitlines()
    review = await _approve_and_review(user_id, thread_id)
    assert f"- Kararlaştırdığımız gibi çıkarıldı: {TAG}" in review.splitlines()


# == F9: a second offer in the same thread =====================================================


@pytest.mark.asyncio
async def test_a_second_offer_names_only_its_own_drop(monkeypatch) -> None:
    user_id, thread_id = _seed_t1()
    first = await _held_draft(monkeypatch, user_id, thread_id)
    await _tap_simpler(monkeypatch, user_id, thread_id, first)
    await _decide(user_id, thread_id, "deny")
    # A new text the draft does not carry: a second, different offer.
    second = await _turn(
        monkeypatch,
        user_id,
        thread_id,
        "Also put “BEST STEAK” in big letters",
        _planned(
            _strategy(),
            BriefUpdate(
                kind="text", scope="global", literal="BEST STEAK", description="in big letters"
            ),
        ),
    )
    assert second.event_type == "assistant_response", second.content
    question = second.payload["choice_question"]
    assert question["omitted_requirement_ids"] == ["r8"]
    assert 'The simpler version leaves out: in big letters ("BEST STEAK").' in second.content
    assert "ATAKAN" not in second.content

    # The first offer's button no longer answers anything (KRI-794 refuses it at submit).
    with pytest.raises(RuntimeFailure) as stale:
        await _tap_simpler(monkeypatch, user_id, thread_id, first)
    assert stale.value.code == "choice_selection_stale"

    # The second offer's tap drops r8 alone, as agreed; r4's old line is not re-claimed.
    draft = await _tap_simpler(monkeypatch, user_id, thread_id, second)
    assert draft.event_type == "draft_applied", draft.content
    assert '- Left out as agreed: in big letters ("BEST STEAK")' in draft.content.splitlines()
    assert "ATAKAN" not in draft.content
    coverage = _head_coverage(thread_id)
    assert coverage["accepted_omission_ids"] == coverage["agreed_omission_ids"] == ["r8"]


# == F12 + F11: unit edges of the review lines ================================================


def test_review_left_out_lines_skip_live_ids_and_unknown_ids() -> None:
    brief = CreativeBrief.model_validate(copy.deepcopy(FIXTURE["brief_v4"]))
    coverage = {"accepted_omission_ids": ["r4", "r2", "r99"], "agreed_omission_ids": ["r4"]}
    with reply_language_for("en"):
        assert kria_runtime._left_out_lines(brief, coverage) == [f"- Left out as agreed: {TAG}"]
    legacy = {"accepted_omission_ids": ["r4"]}
    with reply_language_for("tr"):
        assert kria_runtime._left_out_lines(brief, legacy) == [
            f"- Daha basit sürümde çıkarıldı: {TAG}"
        ]


def test_the_policy_refusal_question_carries_no_requirement_list() -> None:
    """F6: KRI-759's question keeps ``dropped_fields`` and never looks like a named offer."""
    refusal = simpler_version_question(("target_duration_s",))
    assert refusal["dropped_fields"] == ["target_duration_s"]
    assert "omitted_requirement_ids" not in refusal
    assert "omitted_requirement_ids" not in simpler_version_question()
