"""CA-5 (KRI-1644): the creator keeps some of a research draft's sourced facts, or none, and
approves exactly that, with no new search and no model call.

Drives the real runtime turn over scratch Postgres (``run_kria_turn`` with recorded research
I/O, as the KRI-881 journey does), then the real selection submit, the real approval decision
and the real phone montage worker up to its pin.

Failure modes written before the code:
S1  A subset breaks the approval binding: the new draft's binding digest no longer verifies,
    the pin still holds a dropped fact, or the old approval stays decidable beside the new one.
S2  A subset is approved but the render shows a dropped fact, or searches again.
S3  The selection pays for research: any search, copy or review call.
S4  A selection made for other research (a different basis) is applied instead of refused
    with 409 ``research_selection_stale``, or the refusal writes anything.
S5  The world moved after research (a Visual added): the selection pins research the render
    would refuse, instead of 409.
S6  An untouched draft reads as stale (the basis cannot be recomputed at selection time), so
    the creator can never keep a subset.
S7  A fact id the client never saw is applied, or fails the selection, instead of dropped.
S8  An empty selection leaves no pin (the render researches again after approval), raises at
    render on zero texts, or keeps sourced text. It must write the creator-waived receipt.
S9  Posting the same selection twice (same or new client_event_id) writes a second revision.
S10 Flag off: the preview gains ids, or the key changes the turn.
S11 A build below the minimum protocol sends the key and it is applied, instead of 426.
S12 Approval after a selection is refused: the readiness gate (ON in prod) or the fingerprint
    rejects the new binding.
S13 Approve and select race: both win, or the loser writes a draft.
S14 A selection from an older card (same research, a draft since narrowed past it) narrows the
    newer draft silently instead of 409 stale: the card shows [K, D, T], the creator keeps
    [K, D], and a late [K, T] from the first card leaves only K.
S15 Per-card ids break idempotency: the identical selection (same card, same kept ids) posted
    again, with a new event id or with the same one after the new approval was decided,
    writes a revision or is refused instead of replaying.
S16 The narrowed draft's card carries the old card's id, so the client's next selection on it
    cannot be told from a late one.
S17 Two posts with one client_event_id in flight: the second waits for the first's locks, then
    is refused as stale instead of replaying the first.
S18 The draft row changes between the unlocked read and the lock, and the selection saves a
    revision built from the document it read before.
S19 A follow-up is queued behind the approval, and the selection mints a new approval anyway.
S20 A drafting build below the minimum protocol is shown fact ids it cannot send back.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select, update

from app.agents._runtime import ModelInvocation
from app.config import settings
from app.database import AsyncSessionLocal, sync_session
from app.kria import research_selection
from app.kria.api_schemas import ApprovalDecisionBody, ResearchSelectionIn, SubmitTurnBody
from app.kria.brief_binding import BriefBinding
from app.kria.drafts import KriaDraftDocument, undo_draft
from app.kria.runtime import RuntimeFailure, approval_fingerprint, decide_approval, submit_turn
from app.models import (
    CreationThread,
    CreationThreadEvent,
    CreatorAgentApproval,
    CreatorAgentTurn,
    CreatorEditDraft,
)
from app.services import creation_text_composition as text_lane
from app.services import researched_text
from app.services.client_protocol import RESEARCH_SELECTION_PROTOCOL, set_client_protocol
from app.tasks.kria_runtime import execute_kria_approval
from tests.evals.request_following.test_kri881_research_before_approval import (
    INSTITUTIONAL,
    _dispose_async_engine,  # noqa: F401 - autouse: the async engine per test
    _flags,  # noqa: F401 - autouse: prod flags + an in-memory research cache
    _record,
    _Recorded,
    _render_worker,
    _rendered_texts,
    _turn,
    _visual,
    readiness_on,  # noqa: F401 - fixture
)
from tests.kria.test_clip_order_card_turns_postgres import _capture_dispatch

KEPT_TEXT = "Troy: Settled from the Bronze Age to Roman times"
DROPPED_TEXT = "Troy Museum: Finds from Troy and the Troad"
THIRD_TEXT = "The Troad: The road crosses the plain of the Troad"
STALE = "research_selection_stale"
THREE_SOURCES = [
    *INSTITUTIONAL,
    (
        "The Troad is the historical region around Troy, crossed by the road to the site.",
        "https://whc.unesco.org/en/list/849/gallery/",
        "Archaeological Site of Troy gallery",
    ),
]


class _ThreeFacts(_Recorded):
    """The KRI-881 recording with a third sourced fact on the countryside clip."""

    def invoke(self, *, model: str, prompt: str, **kw: object) -> ModelInvocation:
        result = super().invoke(model=model, prompt=prompt, **kw)
        if not prompt.startswith("Write SHORT researched"):
            return result
        windows = json.loads(prompt.split("Data:\n", 1)[1])["snapshot"]["research_windows"]
        window = next(
            row
            for row in windows
            if len(row["media_ids"]) == 1 and "countryside" in json.dumps(row["seen"])
        )
        payload = json.loads(result.raw_text)
        payload["captions"].insert(
            0,
            {
                "text": "The road crosses the plain of the Troad",
                "window_id": window["id"],
                "media_ids": [],
                "evidence_ids": ["fact-2"],
                "visual_quote": "countryside",
                "scope": "context",
                "context_label": "The Troad",
                "subject_name": "",
                "identity_quote": "",
            },
        )
        return ModelInvocation(raw_text=json.dumps(payload))


@pytest.fixture(autouse=True)
def _selection_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "kria_research_selection_enabled", True)


def _record_three(monkeypatch) -> dict[str, int]:  # noqa: ANN001
    calls = {"search": 0, "copy": 0, "review": 0, "composer": 0}
    client = _ThreeFacts(calls, THREE_SOURCES)
    monkeypatch.setattr(researched_text, "GroundedGeminiClient", lambda: client)
    monkeypatch.setattr(researched_text, "default_client", lambda: client)
    monkeypatch.setattr(text_lane, "default_client", lambda: client)
    return calls


async def _research_draft(
    monkeypatch,  # noqa: ANN001
    *,
    readiness: bool = False,
    three_facts: bool = False,
    protocol: int = RESEARCH_SELECTION_PROTOCOL,
) -> SimpleNamespace:
    set_client_protocol(protocol)
    calls = _record_three(monkeypatch) if three_facts else _record(monkeypatch, INSTITUTIONAL)
    result, accepted, thread_id = await _turn(monkeypatch, through_readiness=readiness)
    assert result["status"] == "awaiting_approval", result
    with sync_session() as db:
        user_id = db.get(CreationThread, thread_id).creator_id
    return SimpleNamespace(
        calls=calls,
        thread_id=thread_id,
        user_id=user_id,
        turn_id=accepted.turn_id,
        preview=_draft_events(thread_id)[-1].payload["research_preview"],
    )


def _draft_events(thread_id: uuid.UUID) -> list[CreationThreadEvent]:
    with sync_session() as db:
        return list(
            db.execute(
                select(CreationThreadEvent)
                .where(
                    CreationThreadEvent.thread_id == thread_id,
                    CreationThreadEvent.event_type == "draft_applied",
                )
                .order_by(CreationThreadEvent.sequence)
            ).scalars()
        )


def _approvals(thread_id: uuid.UUID) -> list[CreatorAgentApproval]:
    with sync_session() as db:
        return list(
            db.execute(
                select(CreatorAgentApproval)
                .where(CreatorAgentApproval.thread_id == thread_id)
                .order_by(CreatorAgentApproval.created_at)
            ).scalars()
        )


def _pending(thread_id: uuid.UUID) -> CreatorAgentApproval:
    (pending,) = [row for row in _approvals(thread_id) if row.status == "pending"]
    return pending


def _revisions(thread_id: uuid.UUID) -> int:
    with sync_session() as db:
        return int(
            db.execute(
                select(func.count(CreatorEditDraft.id)).where(
                    CreatorEditDraft.thread_id == thread_id
                )
            ).scalar_one()
        )


def _document(draft_id: uuid.UUID) -> KriaDraftDocument:
    with sync_session() as db:
        return KriaDraftDocument.model_validate(db.get(CreatorEditDraft, draft_id).snapshot_json)


def _revision(thread_id: uuid.UUID) -> int:
    with sync_session() as db:
        return int(db.get(CreationThread, thread_id).revision)


def _head_texts(thread_id: uuid.UUID) -> list[str]:
    with sync_session() as db:
        head = db.execute(
            select(CreatorEditDraft).where(
                CreatorEditDraft.thread_id == thread_id, CreatorEditDraft.is_head.is_(True)
            )
        ).scalar_one()
        pinned = KriaDraftDocument.model_validate(head.snapshot_json).brief_binding
    return [row["text"] for row in pinned.prepared_research["texts"]]


async def _dispatched_research(monkeypatch, draft: SimpleNamespace, approval_id) -> dict:  # noqa: ANN001
    seen = _capture_dispatch(monkeypatch, draft.user_id, draft.thread_id)
    result = await asyncio.to_thread(execute_kria_approval.run, str(approval_id))
    assert result["status"] == "dispatched", result
    return seen[-1]["creator_brief_binding"]["prepared_research"]


def _fact_id(draft: SimpleNamespace, text: str) -> str:
    return next(row["fact_id"] for row in draft.preview["explanations"] if row["text"] == text)


async def _select(  # noqa: ANN202
    draft: SimpleNamespace,
    kept: list[str],
    *,
    selection_id: str | None = None,
    event_id: str | None = None,
    protocol: int | None = RESEARCH_SELECTION_PROTOCOL,
    revision: int | None = None,
):
    set_client_protocol(protocol)
    async with AsyncSessionLocal() as db:
        accepted, _publish = await submit_turn(
            db,
            thread_id=draft.thread_id,
            creator_id=draft.user_id,
            body=SubmitTurnBody(
                message="Use the facts I kept",
                client_event_id=event_id or f"selection-{uuid.uuid4().hex}",
                expected_thread_revision=_revision(draft.thread_id)
                if revision is None
                else revision,
                research_selection=ResearchSelectionIn(
                    selection_id=selection_id or draft.preview["selection_id"],
                    kept_fact_ids=kept,
                ),
            ),
        )
    return accepted


async def _approve(draft: SimpleNamespace):  # noqa: ANN202
    approval = _pending(draft.thread_id)
    async with AsyncSessionLocal() as db:
        decided, _successor = await decide_approval(
            db,
            thread_id=draft.thread_id,
            approval_id=approval.id,
            creator_id=draft.user_id,
            decision="approve",
            body=ApprovalDecisionBody(
                expected_thread_revision=_revision(draft.thread_id),
                expected_draft_revision=approval.draft_revision,
                expected_approval_fingerprint=approval_fingerprint(approval),
            ),
        )
    return decided


@pytest.mark.asyncio
async def test_the_preview_names_each_fact_and_the_research_it_belongs_to(monkeypatch) -> None:
    draft = await _research_draft(monkeypatch)

    assert len(draft.preview["selection_id"]) == 64
    ids = [row["fact_id"] for row in draft.preview["explanations"]]
    assert [row["text"] for row in draft.preview["explanations"]] == [KEPT_TEXT, DROPPED_TEXT]
    assert len(set(ids)) == 2 and all(ids)


@pytest.mark.asyncio
@pytest.mark.parametrize("readiness", [False, True])
async def test_keeping_some_facts_saves_a_new_draft_that_approves_and_renders_only_them(
    monkeypatch, request, readiness: bool
) -> None:
    """S1, S2, S3, S6, S12."""
    if readiness:
        request.getfixturevalue("readiness_on")
    draft = await _research_draft(monkeypatch, readiness=readiness)
    old = _pending(draft.thread_id)
    paid = dict(draft.calls)

    accepted = await _select(draft, [_fact_id(draft, KEPT_TEXT)])

    assert accepted.status == "awaiting_approval"
    assert draft.calls == paid
    new = _pending(draft.thread_id)
    assert new.id != old.id and new.draft_revision == old.draft_revision + 1
    assert approval_fingerprint(new) != approval_fingerprint(old)
    assert {row.id: row.status for row in _approvals(draft.thread_id)}[old.id] == "cancelled"
    with sync_session() as db:
        assert db.get(CreatorAgentTurn, old.turn_id).status == "superseded"
        cancelled = db.execute(
            select(CreationThreadEvent).where(
                CreationThreadEvent.thread_id == draft.thread_id,
                CreationThreadEvent.event_type == "approval_cancelled",
            )
        ).scalar_one()
    assert cancelled.payload["approval_id"] == str(old.id)

    binding = _document(new.draft_id).brief_binding
    assert BriefBinding.model_validate(binding.model_dump(mode="json")).digest == binding.digest
    assert [row["text"] for row in binding.prepared_research["texts"]] == [KEPT_TEXT]
    old_research = _document(old.draft_id).brief_binding.prepared_research
    assert binding.prepared_research["basis_digest"] == old_research["basis_digest"]
    reply = _draft_events(draft.thread_id)[-1]
    assert reply.payload["draft_id"] == str(new.draft_id)
    assert [
        (row["text"], row["fact_id"]) for row in reply.payload["research_preview"]["explanations"]
    ] == [(KEPT_TEXT, _fact_id(draft, KEPT_TEXT))]
    assert reply.payload["research_preview"]["selection_id"] != draft.preview["selection_id"]
    assert reply.payload["research_selection"]["kept_fact_ids"] == [_fact_id(draft, KEPT_TEXT)]
    assert KEPT_TEXT in reply.content and DROPPED_TEXT not in reply.content
    assert DROPPED_TEXT not in new.consequence_summary
    if DROPPED_TEXT in old.consequence_summary:
        assert KEPT_TEXT in new.consequence_summary

    decided = await _approve(draft)
    assert decided.status == "approved"
    dispatched = await _dispatched_research(monkeypatch, draft, new.id)
    assert [row["text"] for row in dispatched["texts"]] == [KEPT_TEXT]

    strategy = _document(new.draft_id).strategy
    seen = _render_worker(monkeypatch, binding, strategy)
    assert "refusal" not in seen, seen.get("refusal")
    sourced = [
        row["text"]
        for row in _rendered_texts(seen["composed"])
        if (row.get("source_params") or {}).get("research")
    ]
    assert sourced == [KEPT_TEXT]
    assert (draft.calls["search"], draft.calls["copy"], draft.calls["review"]) == (
        paid["search"],
        paid["copy"],
        paid["review"],
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("readiness", [False, True])
async def test_keeping_no_facts_pins_the_creators_waiver_and_never_researches_at_render(
    monkeypatch, request, readiness: bool
) -> None:
    """S8, S12."""
    if readiness:
        request.getfixturevalue("readiness_on")
    draft = await _research_draft(monkeypatch, readiness=readiness)

    await _select(draft, [])

    new = _pending(draft.thread_id)
    binding = _document(new.draft_id).brief_binding
    waived = binding.prepared_research
    assert waived["waived"] is True and waived["texts"] == []
    reply = _draft_events(draft.thread_id)[-1]
    assert reply.payload["research_selection"] == {
        "selection_id": draft.preview["selection_id"],
        "kept_fact_ids": [],
        "waived": True,
    }
    assert "research_preview" not in reply.payload
    for shown in (reply.content, new.consequence_summary):
        assert KEPT_TEXT not in shown and DROPPED_TEXT not in shown
    assert (await _approve(draft)).status == "approved"
    assert (await _dispatched_research(monkeypatch, draft, new.id))["waived"] is True

    strategy = _document(new.draft_id).strategy
    before = draft.calls["search"]
    seen = _render_worker(monkeypatch, binding, strategy)
    assert "refusal" not in seen, seen.get("refusal")
    assert not [
        row
        for row in _rendered_texts(seen["composed"])
        if (row.get("source_params") or {}).get("research")
    ]
    assert draft.calls["search"] == before

    # The control: the same render with no pin researches again, so the count above can see it.
    unpinned = binding.model_copy(
        update={
            "prepared_research": None,
            "digest": BriefBinding._digest(
                binding.thread_id,
                binding.state,
                binding.brief,
                binding.creator_request,
                binding.media_snapshot,
                binding.choice_answers,
                binding.protected_regions,
                None,
            ),
        }
    )
    _render_worker(monkeypatch, unpinned, strategy)
    assert draft.calls["search"] == before + 1


@pytest.mark.asyncio
async def test_a_fact_id_the_client_never_saw_is_dropped(monkeypatch) -> None:
    """S7."""
    draft = await _research_draft(monkeypatch)

    await _select(draft, [_fact_id(draft, KEPT_TEXT), "not-a-shown-fact"])

    reply = _draft_events(draft.thread_id)[-1]
    assert reply.payload["research_selection"]["kept_fact_ids"] == [_fact_id(draft, KEPT_TEXT)]
    pinned = _document(_pending(draft.thread_id).draft_id).brief_binding.prepared_research
    assert [row["text"] for row in pinned["texts"]] == [KEPT_TEXT]


@pytest.mark.asyncio
async def test_ids_that_match_no_shown_fact_are_stale_never_a_waiver(monkeypatch) -> None:
    """S7, S8: only an empty list waives, on the current card and on an older one."""
    draft = await _research_draft(monkeypatch)
    untouched = (_revisions(draft.thread_id), _pending(draft.thread_id).id)
    with pytest.raises(RuntimeFailure) as refused:
        await _select(draft, ["not-a-shown-fact"])
    assert (refused.value.status_code, refused.value.code) == (409, STALE)
    assert (_revisions(draft.thread_id), _pending(draft.thread_id).id) == untouched

    await _select(draft, [_fact_id(draft, KEPT_TEXT)])
    after = (_revisions(draft.thread_id), _pending(draft.thread_id).id)

    for kept in (["not-a-shown-fact"], [_fact_id(draft, DROPPED_TEXT)]):
        with pytest.raises(RuntimeFailure) as refused:
            await _select(draft, kept)
        assert (refused.value.status_code, refused.value.code) == (409, STALE)

    assert (_revisions(draft.thread_id), _pending(draft.thread_id).id) == after
    assert _head_texts(draft.thread_id) == [KEPT_TEXT]


@pytest.mark.asyncio
async def test_a_repeated_waiver_writes_nothing(monkeypatch) -> None:
    """S9."""
    draft = await _research_draft(monkeypatch)
    first = await _select(draft, [])
    after = (_revisions(draft.thread_id), _pending(draft.thread_id).id)

    again = await _select(draft, [])
    with pytest.raises(RuntimeFailure) as refused:
        await _select(draft, ["not-a-shown-fact"])

    assert again.replayed and again.turn_id == first.turn_id
    assert (refused.value.status_code, refused.value.code) == (409, STALE)
    assert (_revisions(draft.thread_id), _pending(draft.thread_id).id) == after


@pytest.mark.asyncio
async def test_undo_after_a_selection_restores_every_fact(monkeypatch) -> None:
    """The selection's reply offers Undo: it puts the full research back and closes the card."""
    draft = await _research_draft(monkeypatch)
    await _select(draft, [_fact_id(draft, KEPT_TEXT)])
    narrowed = _pending(draft.thread_id)

    async with AsyncSessionLocal() as db:
        await undo_draft(
            db,
            thread_id=draft.thread_id,
            creator_id=draft.user_id,
            expected_revision=narrowed.draft_revision,
        )

    assert _head_texts(draft.thread_id) == [KEPT_TEXT, DROPPED_TEXT]
    assert not [row for row in _approvals(draft.thread_id) if row.status == "pending"]


@pytest.mark.asyncio
async def test_the_same_selection_twice_saves_one_revision(monkeypatch) -> None:
    """S9, S15."""
    draft = await _research_draft(monkeypatch)
    kept = [_fact_id(draft, KEPT_TEXT)]

    sent_at = _revision(draft.thread_id)
    first = await _select(draft, kept, event_id="selection-once", revision=sent_at)
    after_first = (_revisions(draft.thread_id), _pending(draft.thread_id).id)
    replay = await _select(draft, kept, event_id="selection-once", revision=sent_at)
    again = await _select(draft, kept)
    unseen = await _select(draft, [*kept, "not-a-shown-fact"])

    for repeat in (replay, again, unseen):
        assert repeat.replayed and repeat.turn_id == first.turn_id
    assert (_revisions(draft.thread_id), _pending(draft.thread_id).id) == after_first

    assert (await _approve(draft)).status == "approved"
    decided = await _select(draft, kept, event_id="selection-once", revision=sent_at)
    assert decided.replayed and decided.turn_id == first.turn_id
    assert _revisions(draft.thread_id) == after_first[0]


@pytest.mark.asyncio
async def test_an_older_cards_selection_never_narrows_the_newer_draft(monkeypatch) -> None:
    """S14, S16: the first card shows three facts and the creator keeps two."""
    draft = await _research_draft(monkeypatch, three_facts=True)
    ids = {row["text"]: row["fact_id"] for row in draft.preview["explanations"]}
    assert set(ids) == {KEPT_TEXT, DROPPED_TEXT, THIRD_TEXT}
    sent_at = _revision(draft.thread_id)
    await _select(draft, [ids[KEPT_TEXT], ids[DROPPED_TEXT]], revision=sent_at)
    card = _draft_events(draft.thread_id)[-1].payload["research_preview"]
    after = (_revisions(draft.thread_id), _pending(draft.thread_id).id)

    assert card["selection_id"] != draft.preview["selection_id"]
    for late, revision in (
        ([ids[KEPT_TEXT], ids[THIRD_TEXT]], sent_at),
        ([ids[KEPT_TEXT]], None),
        ([ids[KEPT_TEXT], ids[DROPPED_TEXT], ids[THIRD_TEXT]], None),
    ):
        with pytest.raises(RuntimeFailure) as refused:
            await _select(draft, late, revision=revision)
        assert (refused.value.status_code, refused.value.code) == (409, STALE)
    assert (_revisions(draft.thread_id), _pending(draft.thread_id).id) == after
    assert _head_texts(draft.thread_id) == [KEPT_TEXT, DROPPED_TEXT]

    await _select(draft, [ids[KEPT_TEXT]], selection_id=card["selection_id"])
    assert _head_texts(draft.thread_id) == [KEPT_TEXT]


@pytest.mark.asyncio
async def test_a_duplicate_in_flight_with_the_same_event_id_replays(monkeypatch) -> None:
    """S17: the first post reads before the second commits and locks after it."""
    draft = await _research_draft(monkeypatch)
    kept = [_fact_id(draft, KEPT_TEXT)]
    sent_at = _revision(draft.thread_id)
    revisions = _revisions(draft.thread_id)
    reached, release = asyncio.Event(), asyncio.Event()
    lock = research_selection._lock_strategy_plan_item

    async def first_waits(db, **kwargs):  # noqa: ANN001, ANN003, ANN202
        if not reached.is_set():
            reached.set()
            await release.wait()
        return await lock(db, **kwargs)

    monkeypatch.setattr(research_selection, "_lock_strategy_plan_item", first_waits)
    late = asyncio.create_task(_select(draft, kept, event_id="selection-dup", revision=sent_at))
    await reached.wait()
    saved = await _select(draft, kept, event_id="selection-dup", revision=sent_at)
    release.set()
    replayed = await late

    assert not saved.replayed
    assert replayed.replayed and replayed.turn_id == saved.turn_id
    assert _revisions(draft.thread_id) == revisions + 1


@pytest.mark.asyncio
async def test_a_draft_rewritten_before_the_lock_is_stale(monkeypatch) -> None:
    """S18."""
    draft = await _research_draft(monkeypatch)
    old = _pending(draft.thread_id)
    revisions = _revisions(draft.thread_id)
    lock = research_selection._lock_strategy_plan_item

    async def rewritten_first(db, **kwargs):  # noqa: ANN001, ANN003, ANN202
        with sync_session() as other:
            other.execute(
                update(CreatorEditDraft)
                .where(CreatorEditDraft.id == old.draft_id)
                .values(snapshot_hash="f" * 64)
            )
            other.commit()
        return await lock(db, **kwargs)

    monkeypatch.setattr(research_selection, "_lock_strategy_plan_item", rewritten_first)
    with pytest.raises(RuntimeFailure) as refused:
        await _select(draft, [_fact_id(draft, KEPT_TEXT)])

    assert (refused.value.status_code, refused.value.code) == (409, STALE)
    assert _revisions(draft.thread_id) == revisions and _pending(draft.thread_id).id == old.id


@pytest.mark.asyncio
async def test_a_queued_follow_up_refuses_the_selection(monkeypatch) -> None:
    """S19."""
    draft = await _research_draft(monkeypatch)
    old = _pending(draft.thread_id)
    async with AsyncSessionLocal() as db:
        queued, _publish = await submit_turn(
            db,
            thread_id=draft.thread_id,
            creator_id=draft.user_id,
            body=SubmitTurnBody(
                message="Make the ending slower",
                client_event_id=f"follow-up-{uuid.uuid4().hex}",
                expected_thread_revision=_revision(draft.thread_id),
            ),
        )
    assert queued.status == "queued"
    revisions = _revisions(draft.thread_id)

    with pytest.raises(RuntimeFailure) as refused:
        await _select(draft, [_fact_id(draft, KEPT_TEXT)])

    assert (refused.value.status_code, refused.value.code) == (409, "queued_successor_exists")
    assert _revisions(draft.thread_id) == revisions and _pending(draft.thread_id).id == old.id


@pytest.mark.asyncio
async def test_keeping_every_fact_changes_nothing(monkeypatch) -> None:
    """S9: the approval the creator is looking at stays the one to approve."""
    draft = await _research_draft(monkeypatch)
    old = _pending(draft.thread_id)
    revisions = _revisions(draft.thread_id)

    accepted = await _select(draft, [row["fact_id"] for row in draft.preview["explanations"]])

    assert accepted.turn_id == str(old.turn_id) and accepted.replayed
    assert _pending(draft.thread_id).id == old.id and _revisions(draft.thread_id) == revisions
    assert (await _approve(draft)).status == "approved"


@pytest.mark.asyncio
async def test_a_selection_for_other_research_is_refused_as_stale(monkeypatch) -> None:
    """S4."""
    draft = await _research_draft(monkeypatch)
    old = _pending(draft.thread_id)
    revisions = _revisions(draft.thread_id)

    with pytest.raises(RuntimeFailure) as refused:
        await _select(draft, [_fact_id(draft, KEPT_TEXT)], selection_id="0" * 64)

    assert (refused.value.status_code, refused.value.code) == (409, STALE)
    assert _pending(draft.thread_id).id == old.id and _revisions(draft.thread_id) == revisions


@pytest.mark.asyncio
async def test_research_the_moved_footage_no_longer_fits_is_refused_as_stale(monkeypatch) -> None:
    """S5: a Visual added after research changes the windows research was bound to."""
    draft = await _research_draft(monkeypatch)
    old = _pending(draft.thread_id)
    monkeypatch.setattr(
        "app.services.phone_montage_preflight._ready_visuals", lambda *_a, **_k: [_visual()]
    )

    with pytest.raises(RuntimeFailure) as refused:
        await _select(draft, [_fact_id(draft, KEPT_TEXT)])

    assert (refused.value.status_code, refused.value.code) == (409, STALE)
    assert _pending(draft.thread_id).id == old.id


@pytest.mark.asyncio
async def test_a_selection_after_the_draft_was_approved_writes_nothing(monkeypatch) -> None:
    """S13."""
    draft = await _research_draft(monkeypatch)
    assert (await _approve(draft)).status == "approved"
    revisions = _revisions(draft.thread_id)

    with pytest.raises(RuntimeFailure) as refused:
        await _select(draft, [_fact_id(draft, KEPT_TEXT)])

    assert (refused.value.status_code, refused.value.code) == (409, STALE)
    assert _revisions(draft.thread_id) == revisions


@pytest.mark.asyncio
async def test_an_approval_for_the_replaced_draft_is_refused(monkeypatch) -> None:
    """S13: the creator's old card loses once the selection won."""
    draft = await _research_draft(monkeypatch)
    old = _pending(draft.thread_id)
    await _select(draft, [_fact_id(draft, KEPT_TEXT)])

    async with AsyncSessionLocal() as db:
        with pytest.raises(RuntimeFailure) as refused:
            await decide_approval(
                db,
                thread_id=draft.thread_id,
                approval_id=old.id,
                creator_id=draft.user_id,
                decision="approve",
                body=ApprovalDecisionBody(
                    expected_thread_revision=_revision(draft.thread_id),
                    expected_draft_revision=old.draft_revision,
                    expected_approval_fingerprint=approval_fingerprint(old),
                ),
            )
    assert refused.value.status_code == 409


@pytest.mark.asyncio
async def test_a_build_below_the_minimum_protocol_is_told_to_update(monkeypatch) -> None:
    """S11."""
    draft = await _research_draft(monkeypatch)
    revisions = _revisions(draft.thread_id)

    with pytest.raises(RuntimeFailure) as refused:
        await _select(draft, [_fact_id(draft, KEPT_TEXT)], protocol=RESEARCH_SELECTION_PROTOCOL - 1)

    assert (refused.value.status_code, refused.value.code) == (426, "client_update_required")
    assert _revisions(draft.thread_id) == revisions


@pytest.mark.asyncio
async def test_a_build_below_the_minimum_protocol_is_shown_no_ids(monkeypatch) -> None:
    """S20."""
    draft = await _research_draft(monkeypatch, protocol=RESEARCH_SELECTION_PROTOCOL - 1)

    assert "selection_id" not in draft.preview
    assert all(
        set(row) == {"text", "start_s", "end_s", "sources"} for row in draft.preview["explanations"]
    )


@pytest.mark.asyncio
async def test_flag_off_keeps_the_legacy_preview_and_ignores_the_key(monkeypatch) -> None:
    """S10."""
    monkeypatch.setattr(settings, "kria_research_selection_enabled", False)
    draft = await _research_draft(monkeypatch)
    old = _pending(draft.thread_id)

    assert "selection_id" not in draft.preview
    assert all(
        set(row) == {"text", "start_s", "end_s", "sources"} for row in draft.preview["explanations"]
    )
    accepted = await _select(draft, [], selection_id="0" * 64)

    assert accepted.status == "queued"
    assert _pending(draft.thread_id).id == old.id
