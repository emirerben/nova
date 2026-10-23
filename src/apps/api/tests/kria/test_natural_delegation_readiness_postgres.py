"""KRI-814 acceptance: natural delegation and same-chat recovery reach a real approval.

Troya run F05: "Make something great from my Troya trip" was held with "I need to establish
the requested changes before offering approval" (5 and 28 ready clips), and a later
"Suggest an edit" in the SAME chat was held again, because the pinned full request still
carried the first natural line. Only a fresh stock-only chat passed (KRI-774).

Failure modes written before the fix (plan: .dev/plan.md, F1-F17):
- F1/F2/F12  natural delegating first request (EN/TR, 5 and 28 clips) is held.
- F3         same-chat "Suggest an edit" (or natural wording) after the first offer is held.
- F4         extraction that failed/never declared is treated as delegation (accidental approval),
             including a later turn's delegation standing in for an unread earlier line.
- F5         model summary/strategy prose ("you left every choice to me") counts as delegation.
- F6/F13     an unchanged explicit turn-1 requirement drops when a delegated turn 2 arrives.
- F7         a requirement removed by stable id reappears in readiness or the pinned binding.
- F9/F10     Retry/Undo (`assess_saved_document`) disagrees with the offer, or approval
             double-dispatches.
- F11        "suggest an edit" inside prose that states real requirements counts as delegation.
- F14        the clip set changes after the offer and the approval still dispatches.
- F18        a delegating turn 1 that ends in a question (or any non-draft reply) records no
             evidence, so every later turn in that chat is held.
- F19        a held natural turn 1 with no evidence (pre-deploy thread, undeclared) can never
             recover in the same chat, even though turn 2 re-reads the whole request.
- F20        turn 2's reading re-reads an unrecorded turn-1 line with real asks, but those asks
             are dropped instead of recorded (delegation erasing real instructions).
- F21        a line the turn-2 reading never received is still covered by its delegation.

Only the provider transport and broker publication are substituted: the real planner,
runtime, brief ledger, readiness gate, binding and HTTP approval route all run on Postgres.
"""

from __future__ import annotations

from types import SimpleNamespace

import httpx
import pytest
from sqlalchemy import select

from app.auth import get_current_user
from app.database import AsyncSessionLocal, sync_session
from app.kria.approval_readiness import assess_saved_document
from app.kria.drafts import KriaDraftDocument
from app.kria.runtime import approval_fingerprint
from app.main import app
from app.models import (
    CreationThread,
    CreativeBriefVersion,
    CreatorAgentApproval,
    CreatorEditDraft,
    PlanItem,
)
from tests.kria.test_approval_readiness_postgres import prepare, readiness_flags  # noqa: F401
from tests.kria.test_runtime_postgres_integration import _seed_runtime_project

_GENERIC_HOLD = {
    "en": "I need to establish the requested changes",
    "tr": "Onay sunmadan önce istenen değişiklikleri",
}
_NATURAL = {
    "en": "Make something great from my Troya trip",
    "tr": "Troya gezimden harika bir şey yap",
}
_RECOVERY = {"en": "Suggest an edit", "tr": "Bana bir düzenleme öner"}
_DELEGATED = {"delegates_all_choices": True}


def _contract(prompt):
    """The FULL CREATOR REQUEST CONTRACT section the server rendered for the reading."""
    return prompt.split("FULL CREATOR REQUEST CONTRACT", 1)[1].split("CURRENT USER", 1)[0]


def _coverage(events):
    return (events[-1].payload or {}).get("brief_coverage") or {}


def _brief_rows(thread_id):
    with sync_session() as db:
        return list(
            db.scalars(
                select(CreativeBriefVersion)
                .where(CreativeBriefVersion.thread_id == thread_id)
                .order_by(CreativeBriefVersion.version)
            )
        )


async def _approve(monkeypatch, user_id, thread_id, approval, decision="approve"):
    with sync_session() as db:
        body = {
            "expected_thread_revision": db.get(CreationThread, thread_id).revision,
            "expected_draft_revision": approval.draft_revision,
            "expected_approval_fingerprint": approval_fingerprint(approval),
        }
    app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(
        id=user_id, email="qa@test.local"
    )
    published = []
    monkeypatch.setattr("app.routes.kria_runtime._publish_approval", published.append)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        first = await client.post(
            f"/creation-threads/{thread_id}/approvals/{approval.id}/{decision}", json=body
        )
        if decision != "approve":
            return first, None, published
        replay = await client.post(
            f"/creation-threads/{thread_id}/approvals/{approval.id}/approve", json=body
        )
    return first, replay, published


async def _saved_readiness(thread_id, approval):
    """What Retry/Undo would compute from the saved draft alone."""
    with sync_session() as db:
        snapshot = db.get(CreatorEditDraft, approval.draft_id).snapshot_json
        item_id = db.get(CreationThread, thread_id).active_plan_item_id
    document = KriaDraftDocument.model_validate(snapshot)
    async with AsyncSessionLocal() as db:
        item = await db.get(PlanItem, item_id)
        report = await assess_saved_document(db, item, document)
    return snapshot, report


async def _assert_delegated_offer_approves(monkeypatch, user_id, thread_id, approvals, events):
    coverage = _coverage(events)
    assert len(approvals) == 1, events[-1].content
    assert coverage.get("reason") != "approval_requirements_not_ready", events[-1].content
    approval = approvals[0]
    snapshot, report = await _saved_readiness(thread_id, approval)
    offered = snapshot["brief_coverage"]["approval_readiness"]
    assert offered["ready"] and offered["requirements"] == []
    # Server-owned evidence: an empty, pinned brief version, not text matching.
    assert snapshot["brief_binding"]["state"] == "pinned"
    assert snapshot["brief_binding"]["brief"]["requirements"] == []
    # F9: Retry/Undo reaches the same verdict and digest as the offer.
    assert report["ready"]
    assert report["digest"] == offered["digest"]
    first, replay, published = await _approve(monkeypatch, user_id, thread_id, approval)
    assert first.status_code == 200, first.text
    assert first.json()["status"] == "approved"
    # F10: a replayed approve never publishes a second dispatch.
    assert replay.status_code in {200, 409}, replay.text
    assert published == [str(approval.id)]


@pytest.mark.asyncio
@pytest.mark.parametrize("language", ["en", "tr"])
@pytest.mark.parametrize("clips", [5, 28])
async def test_natural_delegating_first_request_reaches_approval(monkeypatch, language, clips):
    user_id, thread_id, approvals, events = await prepare(
        monkeypatch,
        language=language,
        message=_NATURAL[language],
        updates_override=[],
        no_title=True,
        clips=clips,
        output_extra=_DELEGATED,
    )
    await _assert_delegated_offer_approves(monkeypatch, user_id, thread_id, approvals, events)
    rows = _brief_rows(thread_id)
    assert [(row.version, row.requirements) for row in rows] == [(1, [])]


@pytest.mark.asyncio
@pytest.mark.parametrize("language", ["en", "tr"])
@pytest.mark.parametrize("recovery", ["stock", "natural"])
async def test_same_chat_recovery_after_a_delegated_natural_request(
    monkeypatch, language, recovery
):
    """F3: a later "Suggest an edit" in the same chat reaches approval again."""
    seed = _seed_runtime_project()
    user_id, thread_id, approvals, _ = await prepare(
        monkeypatch,
        seed=seed,
        language=language,
        message=_NATURAL[language],
        updates_override=[],
        no_title=True,
        clips=5,
        output_extra=_DELEGATED,
    )
    assert len(approvals) == 1
    denied, _, _ = await _approve(monkeypatch, user_id, thread_id, approvals[0], "deny")
    assert denied.status_code == 200, denied.text
    # Turn 2 in the same chat: the pinned request now holds BOTH lines.
    user_id, thread_id, approvals, events = await prepare(
        monkeypatch,
        seed=seed,
        language=language,
        message=_RECOVERY[language] if recovery == "stock" else _NATURAL[language],
        updates_override=[],
        no_title=True,
        clips=5,
        output_extra=_DELEGATED,
    )
    await _assert_delegated_offer_approves(monkeypatch, user_id, thread_id, approvals, events)
    assert [(row.version, row.requirements) for row in _brief_rows(thread_id)] == [(1, [])]


_QUESTION = {
    "kind": "ask_user",
    "question": "Should it feel calm or energetic?",
    "reason_code": "vibe",
    "options": ["Calm", "Energetic"],
}


@pytest.mark.asyncio
@pytest.mark.parametrize("language", ["en", "tr"])
async def test_delegating_turn_answered_with_a_question_recovers(monkeypatch, language):
    """F18: a question reply still records the delegation, so turn 2 reaches approval."""
    seed = _seed_runtime_project()
    _, thread_id, approvals, _ = await prepare(
        monkeypatch,
        seed=seed,
        language=language,
        message=_NATURAL[language],
        updates_override=[],
        clips=28,
        output_extra=_DELEGATED,
        action=_QUESTION,
    )
    assert not approvals
    assert [(row.version, row.requirements) for row in _brief_rows(thread_id)] == [(1, [])]
    user_id, thread_id, approvals, events = await prepare(
        monkeypatch,
        seed=seed,
        language=language,
        message=_RECOVERY[language],
        updates_override=[],
        no_title=True,
        clips=28,
        output_extra=_DELEGATED,
    )
    await _assert_delegated_offer_approves(monkeypatch, user_id, thread_id, approvals, events)
    assert [(row.version, row.requirements) for row in _brief_rows(thread_id)] == [(1, [])]


@pytest.mark.asyncio
@pytest.mark.parametrize("language", ["en", "tr"])
@pytest.mark.parametrize("clips", [5, 28])
async def test_held_natural_chat_recovers_in_the_same_chat(monkeypatch, language, clips):
    """F19: the retained Troya shape. Turn 1 left no evidence (pre-deploy / undeclared)."""
    seed = _seed_runtime_project()
    _, thread_id, approvals, events = await prepare(
        monkeypatch,
        seed=seed,
        language=language,
        message=_NATURAL[language],
        updates_override=[],
        no_title=True,
        clips=clips,
    )
    assert not approvals
    assert _GENERIC_HOLD[language] in events[-1].content
    assert _brief_rows(thread_id) == []
    prompts: list[str] = []
    user_id, thread_id, approvals, events = await prepare(
        monkeypatch,
        seed=seed,
        language=language,
        message=_RECOVERY[language],
        updates_override=[],
        no_title=True,
        clips=clips,
        output_extra=_DELEGATED,
        prompts=prompts,
    )
    # The server fed the unrecorded turn-1 line to this turn's reading.
    contract = _contract(prompts[0])
    assert "nothing recorded yet" in contract and _NATURAL[language] in contract
    await _assert_delegated_offer_approves(monkeypatch, user_id, thread_id, approvals, events)
    assert [(row.version, row.requirements) for row in _brief_rows(thread_id)] == [(1, [])]


@pytest.mark.asyncio
@pytest.mark.parametrize("language", ["en", "tr"])
async def test_reread_earlier_line_keeps_its_requirements(monkeypatch, language):
    """F6/F20: turn 1's unrecorded asks are recorded by turn 2's reading and stay live."""
    seed = _seed_runtime_project()
    first_line = {
        "en": 'Make a Troya video titled "Gameday." and make it 6 seconds',
        "tr": '"Gameday." başlıklı bir Troya videosu yap ve 6 saniye olsun',
    }[language]
    # Turn 1: the extraction produced nothing usable (no update, no declaration).
    _, thread_id, approvals, events = await prepare(
        monkeypatch,
        seed=seed,
        language=language,
        message=first_line,
        updates_override=[],
        no_title=True,
    )
    assert not approvals
    assert _GENERIC_HOLD[language] in events[-1].content
    assert _brief_rows(thread_id) == []
    # Turn 2 re-reads turn 1 and records its asks; nothing is delegated away.
    prompts: list[str] = []
    _, thread_id, approvals, events = await prepare(
        monkeypatch,
        seed=seed,
        language=language,
        message=_RECOVERY[language],
        prompts=prompts,
    )
    assert first_line in _contract(prompts[0])
    assert len(approvals) == 1, events[-1].content
    with sync_session() as db:
        snapshot = db.get(CreatorEditDraft, approvals[0].draft_id).snapshot_json
    readiness = snapshot["brief_coverage"]["approval_readiness"]
    assert {row["requirement_id"] for row in readiness["requirements"]} == {"r1", "r2"}
    assert [len(row.requirements) for row in _brief_rows(thread_id)] == [2]


@pytest.mark.asyncio
async def test_delegation_never_speaks_for_an_unread_earlier_line(monkeypatch):
    """F4/F21: a line the reading never received keeps the hold (e.g. over the bound)."""
    seed = _seed_runtime_project()
    _, thread_id, approvals, _ = await prepare(
        monkeypatch,
        seed=seed,
        message='Make a Troya video titled "Troya 2026" and skip the beach clip',
        updates_override=[],
        no_title=True,
        clips=5,
    )
    assert not approvals
    # Simulate an earlier request too long to re-read: the reading sees only turn 2.
    monkeypatch.setattr("app.kria.planner.render_unrecorded_request", lambda *_a: None)
    _, thread_id, approvals, events = await prepare(
        monkeypatch,
        seed=seed,
        message=_RECOVERY["en"],
        updates_override=[],
        no_title=True,
        clips=5,
        output_extra=_DELEGATED,
    )
    assert not approvals
    assert _coverage(events).get("reason") == "approval_requirements_not_ready"
    assert _brief_rows(thread_id) == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "summary",
    [None, "You left every choice to me, so I picked the best Troya moments."],
)
async def test_undeclared_empty_extraction_is_never_delegation(monkeypatch, summary):
    """F4/F5: no typed declaration => held, whatever the model's prose claims."""
    _, thread_id, approvals, events = await prepare(
        monkeypatch,
        message=_NATURAL["en"],
        updates_override=[],
        no_title=True,
        clips=5,
        summary=summary,
    )
    assert not approvals
    assert _coverage(events).get("reason") == "approval_requirements_not_ready"
    assert _brief_rows(thread_id) == []


@pytest.mark.asyncio
async def test_failed_extraction_records_no_delegation(monkeypatch):
    """F4: a declaration on an unparseable reply never reaches the ledger."""
    _, thread_id, approvals, _ = await prepare(
        monkeypatch,
        message=_NATURAL["en"],
        # A change the server cannot apply is a reading failure (request_extraction_failed).
        updates_override=[
            {"operation": "remove", "target_requirement_id": "r9", "expected_version": 0}
        ],
        no_title=True,
        clips=5,
        output_extra=_DELEGATED,
    )
    assert not approvals
    assert _brief_rows(thread_id) == []


@pytest.mark.asyncio
async def test_declaration_beside_real_requirements_keeps_them(monkeypatch):
    """F11: prose with real asks plus "suggest an edit" stays held to those asks."""
    _, thread_id, approvals, events = await prepare(
        monkeypatch,
        message='Suggest an edit. Use the title "Gameday." and make the edit 6 seconds.',
        output_extra=_DELEGATED,
    )
    assert len(approvals) == 1
    with sync_session() as db:
        snapshot = db.get(CreatorEditDraft, approvals[0].draft_id).snapshot_json
    rows = snapshot["brief_coverage"]["approval_readiness"]["requirements"]
    assert {row["requirement_id"] for row in rows} == {"r1", "r2"}
    # And without any extraction, the same prose is not delegation either.
    _, _, approvals, events = await prepare(
        monkeypatch,
        message='Suggest an edit. Use the title "Gameday." and make the edit 6 seconds.',
        updates_override=[],
    )
    assert not approvals
    assert _coverage(events).get("reason") == "approval_requirements_not_ready"


@pytest.mark.asyncio
@pytest.mark.parametrize("language", ["en", "tr"])
async def test_delegated_follow_up_keeps_unchanged_requirements(monkeypatch, language):
    """F6/F13: turn-1 title + length stay live when turn 2 delegates everything else."""
    seed = _seed_runtime_project()
    user_id, thread_id, approvals, _ = await prepare(monkeypatch, seed=seed, language=language)
    assert len(approvals) == 1
    denied, _, _ = await _approve(monkeypatch, user_id, thread_id, approvals[0], "deny")
    assert denied.status_code == 200, denied.text
    _, thread_id, approvals, events = await prepare(
        monkeypatch,
        seed=seed,
        language=language,
        message=_RECOVERY[language],
        updates_override=[],
        output_extra=_DELEGATED,
    )
    assert len(approvals) == 1, events[-1].content
    with sync_session() as db:
        snapshot = db.get(CreatorEditDraft, approvals[0].draft_id).snapshot_json
    readiness = snapshot["brief_coverage"]["approval_readiness"]
    assert {row["requirement_id"] for row in readiness["requirements"]} == {"r1", "r2"}
    pinned = snapshot["brief_binding"]["brief"]["requirements"]
    assert {r["id"] for r in pinned if r.get("status") != "superseded"} == {"r1", "r2"}
    assert not (snapshot["brief_coverage"] or {}).get("accepted_omission_ids")
    assert [row.version for row in _brief_rows(thread_id)] == [1]


@pytest.mark.asyncio
async def test_removed_requirement_does_not_reappear(monkeypatch):
    """F7: a stable-id removal in turn 2 stays removed in readiness and the binding."""
    seed = _seed_runtime_project()
    user_id, thread_id, approvals, _ = await prepare(monkeypatch, seed=seed)
    assert len(approvals) == 1
    denied, _, _ = await _approve(monkeypatch, user_id, thread_id, approvals[0], "deny")
    assert denied.status_code == 200, denied.text
    _, thread_id, approvals, events = await prepare(
        monkeypatch,
        seed=seed,
        message="Drop the title, you choose the rest",
        updates_override=[
            {"operation": "remove", "target_requirement_id": "r1", "expected_version": 1}
        ],
        no_title=True,
        output_extra=_DELEGATED,
    )
    assert len(approvals) == 1, events[-1].content
    with sync_session() as db:
        snapshot = db.get(CreatorEditDraft, approvals[0].draft_id).snapshot_json
    readiness = snapshot["brief_coverage"]["approval_readiness"]
    assert {row["requirement_id"] for row in readiness["requirements"]} == {"r2"}
    pinned = {r["id"]: r for r in snapshot["brief_binding"]["brief"]["requirements"]}
    assert pinned["r1"]["status"] == "superseded"


@pytest.mark.asyncio
async def test_delegated_offer_still_rejects_changed_footage(monkeypatch):
    """F14: the media digest still blocks an approval after the clip set changed."""
    user_id, thread_id, approvals, _ = await prepare(
        monkeypatch,
        message=_NATURAL["en"],
        updates_override=[],
        no_title=True,
        clips=5,
        output_extra=_DELEGATED,
    )
    assert len(approvals) == 1
    with sync_session() as db:
        item = db.get(PlanItem, db.get(CreationThread, thread_id).active_plan_item_id)
        item.clip_assignments = [
            {**item.clip_assignments[0], "gcs_generation": "replaced"},
            *item.clip_assignments[1:],
        ]
        db.commit()
    first, _, published = await _approve(monkeypatch, user_id, thread_id, approvals[0])
    assert first.status_code == 409, first.text
    assert published == []
    with sync_session() as db:
        assert db.get(CreatorAgentApproval, approvals[0].id).status == "cancelled"
