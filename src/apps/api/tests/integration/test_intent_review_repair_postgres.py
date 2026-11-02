"""From a reviewed generation to a staged, reversible repair and its honest settlement.

Failure modes, written before the staging code (matrix row in parentheses). Each has a test.

  A repair that is not the creator's words (23, 24, 25):
    only a creator-confirmed exact text or the rendered duration can justify a candidate; the
    compiled draft changes exactly the named fields of exactly the named elements and nothing
    else, byte for byte; a compile that touched more is refused and nothing is staged; a
    candidate the draft already satisfies is never a repair.
  A repair over the creator's newer work (26, 27):
    a head draft that is no longer the approved one, or a thread with an open approval or an
    active turn, holds the repair back as a suggestion; the creator's edit wins.
  A repair that ignores a later protection (23):
    a requirement added after the render that forbids touching the element removes the candidate.
  A repair that renders by itself (18):
    staging ends at a pending approval on a new head draft that has the approved draft as its
    parent; nothing is dispatched, no job state changes, the video is untouched.
  A second cycle or a duplicate (28):
    one review stages at most once; the source event and turn are deduped on one client id; the
    follow-up review of the repaired render is capped below repair and has one parent.
  A crash anywhere in staging (29):
    before the turn commits, the source event and a planning turn are cancelled and the review
    falls back to a suggestion; after the draft commits, recovery finishes the staging.
  A repair the creator walks away from (30):
    undo, a denied or expired approval, a failed render and a day without approval each abandon it
    with the right reason, and the sweep is idempotent.
  A repair that makes the video worse (31, 32, 33):
    judged by the follow-up review against the parent's own findings, by requirement: worse
    undoes the draft through its own parent chain only while it is still the head; a follow-up
    that never lands is `unverified`, never `verified`; fixed needs every targeted requirement met.
  Live flags and the tier (13, 41):
    a repair flag turned off after the review, or a tier below repair, downgrades to a suggestion;
    the code ceiling alone keeps the stage from running.
  A system turn reaching the planner (12, 39):
    the reviewer's source event is a system event the planner never reads, a claim of its turn
    cancels it instead of planning, and what the chat shows are the ordinary draft and approval
    events old app builds already render.
  A follow-up judged against the wrong request (40):
    the repaired render's review inherits the parent's request and issued requirements.

The only fakes are the OpenAI SDK object and the synthetic frames/publication; the runtime-v2
turn, the compiler, the drafts, the approval and the undo chain run for real on PostgreSQL.
"""

# ruff: noqa: F811

import asyncio
import copy
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select, text

from app.config import settings
from app.database import AsyncSessionLocal, sync_session
from app.kria import intent_review_tier as tier
from app.kria.brief import BriefRequirement
from app.kria.brief_binding import BriefBinding
from app.kria.drafts import undo_draft
from app.kria.intent_review_repair import INTENT_REPAIR_EVENT
from app.models import (
    CreationThread,
    CreationThreadEvent,
    CreatorAgentApproval,
    CreatorAgentExecution,
    CreatorAgentTurn,
    CreatorEditDraft,
    Job,
)
from app.services import intent_review_flow as flow
from app.services import intent_review_repair_flow as repair
from tests.integration.test_intent_review_lifecycle_postgres import (  # noqa: F401
    Provider,
    edit_row,
    expire_lease,
    ledger,
    paid,
    review_row,
    run_async,
)
from tests.integration.test_intent_review_outcome_postgres import (  # noqa: F401
    edit_packet,
    finding,
    ready,
    reply,
)
from tests.kria.test_render_review_admission_postgres import (  # noqa: F401
    approve_publication,
)

DRIFTED_TITLE = "Béfore"
REWRITE_TITLE_WITH_ACCENT = [
    {
        "op": "rewrite_text",
        "text": DRIFTED_TITLE,
        "selector": {"ids": ["title"]},
        "target_ids": ["title"],
        "expected_count": 1,
    }
]
COPY_MISMATCH = reply(finding("R1", "unmet", "copy_mismatch", ["f0"]))
TEXT_CUT_OFF = reply(finding("R1", "unmet", "text_cut_off", ["f1"]))


@pytest.fixture
def repair_on(paid, monkeypatch):
    monkeypatch.setattr(flow, "_PROMPT_BUILDER", None)
    monkeypatch.setattr(settings, "kria_intent_review_repair_enabled", True)
    monkeypatch.setattr(tier, "_CODE_TIER_CEILING", "repair")
    return paid


@pytest.fixture
async def drifted(monkeypatch):
    return await approve_publication(
        monkeypatch, ops=REWRITE_TITLE_WITH_ACCENT, contract_text="Before"
    )


@pytest.fixture
async def plain(monkeypatch):
    return await approve_publication(monkeypatch)


def drafts(thread_id):
    with sync_session() as db:
        return list(
            db.scalars(
                select(CreatorEditDraft)
                .where(CreatorEditDraft.thread_id == thread_id)
                .order_by(CreatorEditDraft.draft_revision)
            )
        )


def events(thread_id, *types):
    with sync_session() as db:
        query = select(CreationThreadEvent).where(CreationThreadEvent.thread_id == thread_id)
        if types:
            query = query.where(CreationThreadEvent.event_type.in_(types))
        return list(db.scalars(query.order_by(CreationThreadEvent.sequence)))


def review_events(thread_id):
    return [e for e in events(thread_id) if e.event_type not in {"user_message"}]


def title_of(draft):
    elements = draft.snapshot_json["editor_payload"]["text_elements"]
    return next(el for el in elements if el["id"] == "title")


def job_plan(job_id):
    with sync_session() as db:
        return db.get(Job, job_id).assembly_plan


def staged_rows(row):
    with sync_session() as db:
        execution = db.get(CreatorAgentExecution, row.repair_render_execution_id)
        approval = db.scalar(
            select(CreatorAgentApproval).where(CreatorAgentApproval.turn_id == execution.turn_id)
        )
        turn = db.get(CreatorAgentTurn, execution.turn_id)
        source = db.get(CreationThreadEvent, turn.source_event_id)
        return execution, approval, turn, source


@pytest.mark.asyncio
async def test_a_high_confidence_wording_drift_stages_a_pending_draft_and_renders_nothing(
    drifted, repair_on, monkeypatch
):
    _, thread, job_id, _ = drifted
    rid = await ready(drifted, monkeypatch)
    approved = drafts(thread)[-1]
    assert title_of(approved)["text"] == DRIFTED_TITLE
    plan_before, seen = copy.deepcopy(job_plan(job_id)), {e.id for e in events(thread)}

    assert await run_async(rid, Provider(COPY_MISMATCH)) == "repair_staged"

    row = review_row(rid)
    assert (row.status, row.reason) == ("repair_staged", "awaiting_approval")
    assert row.lease_token is None and row.repair_staged_at is not None
    staged = drafts(thread)[-1]
    assert staged.id == row.repair_draft_id and staged.id != approved.id
    assert staged.is_head and staged.parent_draft_id == approved.id
    assert [d.is_head for d in drafts(thread)].count(True) == 1
    assert title_of(staged)["text"].encode() == b"Before"
    assert {k: v for k, v in title_of(staged).items() if k != "text"} == {
        k: v for k, v in title_of(approved).items() if k != "text"
    }
    others = lambda d: [  # noqa: E731
        el for el in d.snapshot_json["editor_payload"]["text_elements"] if el["id"] != "title"
    ]
    assert others(staged) == others(approved)
    assert job_plan(job_id) == plan_before

    execution, approval, turn, source = staged_rows(row)
    assert execution.status == "awaiting_approval" and approval.status == "pending"
    assert approval.draft_id == staged.id and turn.status == "awaiting_approval"
    assert row.repair_render_execution_id == execution.id
    assert (source.role, source.event_type) == ("system", INTENT_REPAIR_EVENT)
    assert source.client_event_id == f"intent-review:{rid}" and source.content is None
    assert source.payload["review_id"] == str(rid)
    created = [e for e in events(thread) if e.id not in seen]
    assert [(e.role, e.event_type) for e in created] == [
        ("system", INTENT_REPAIR_EVENT),
        ("assistant", "draft_applied"),
        ("system", "approval_requested"),
    ]
    applied = created[1]
    assert applied.payload["can_undo"] is True and applied.payload["draft_id"] == str(staged.id)
    assert "One thing didn't match your request: the title isn't your exact wording" in (
        applied.content
    )
    with sync_session() as db:
        draft_run = db.scalar(
            select(CreatorAgentExecution).where(
                CreatorAgentExecution.turn_id == turn.id,
                CreatorAgentExecution.tool_name == "draft.apply_editor_ops",
            )
        )
        receipt = draft_run.result["intent_repair"]
    assert receipt["authority"]["kind"] == "instruction_preserving_v1"
    assert receipt["authority"]["approval"] == "creator_tap"
    assert receipt["base_draft_id"] == str(approved.id)
    assert row.review["repair"]["draft_id"] == str(staged.id)
    assert row.review["repair"]["targeted_brief_ids"] == ["r1"]
    assert len(ledger()) == 1


@pytest.mark.asyncio
async def test_a_title_that_outlives_the_video_is_fitted_and_its_words_are_untouched(
    plain, repair_on, monkeypatch
):
    _, thread, _, _ = plain
    rid = await ready(plain, monkeypatch, duration=1.6)
    approved = drafts(thread)[-1]
    assert await run_async(rid, Provider(TEXT_CUT_OFF)) == "repair_staged"
    staged = drafts(thread)[-1]
    before, after = title_of(approved), title_of(staged)
    assert (after["start_s"], after["end_s"]) == (0.0, 1.3) and before["end_s"] == 2
    assert {k: v for k, v in after.items() if k not in {"start_s", "end_s"}} == {
        k: v for k, v in before.items() if k not in {"start_s", "end_s"}
    }
    assert review_row(rid).review["repair"]["targeted_brief_ids"] == ["r1"]


@pytest.mark.asyncio
async def test_a_compile_that_touches_more_than_the_candidates_named_stages_nothing(
    drifted, repair_on, monkeypatch
):
    _, thread, job_id, _ = drifted
    rid = await ready(drifted, monkeypatch)
    real = repair.build_repair_ops

    def with_a_style_change(candidates):
        ops, expected = real(candidates)
        return [
            *ops,
            {"op": "patch_text_style", "bar_index": 0, "patch": {"color": "#00FF00"}},
        ], expected

    monkeypatch.setattr(repair, "build_repair_ops", with_a_style_change)
    seen, count = {e.id for e in events(thread)}, len(drafts(thread))
    assert await run_async(rid, Provider(COPY_MISMATCH)) == "suggestion"
    row = review_row(rid)
    assert row.repair_draft_id is None and len(drafts(thread)) == count
    assert [s["why"] for s in row.review["disposition"]["suggestions"]] == ["protected"]
    new = [e for e in events(thread) if e.id not in seen]
    assert [e.event_type for e in new] == [INTENT_REPAIR_EVENT]
    with sync_session() as db:
        turn = db.scalar(
            select(CreatorAgentTurn).where(
                CreatorAgentTurn.client_event_id == f"intent-review:{rid}"
            )
        )
        assert turn.status == "cancelled"
        assert (
            db.scalar(select(CreatorAgentApproval).where(CreatorAgentApproval.turn_id == turn.id))
            is None
        )


@pytest.mark.asyncio
async def test_staging_twice_is_one_event_one_turn_one_draft(drifted, repair_on, monkeypatch):
    _, thread, _, _ = drifted
    rid = await ready(drifted, monkeypatch)
    assert await run_async(rid, Provider(COPY_MISMATCH)) == "repair_staged"
    snapshot = (len(events(thread, INTENT_REPAIR_EVENT)), len(drafts(thread)))
    assert flow.run_intent_review(rid) == "ignored"
    assert repair.stage_intent_repair(rid, token=uuid.uuid4()) == "ignored"
    assert (len(events(thread, INTENT_REPAIR_EVENT)), len(drafts(thread))) == snapshot == (1, 2)


def make_head_move(thread_id):
    with sync_session() as db:
        head = db.scalar(
            select(CreatorEditDraft).where(
                CreatorEditDraft.thread_id == thread_id, CreatorEditDraft.is_head.is_(True)
            )
        )
        head.is_head = False
        db.flush()
        later = CreatorEditDraft(
            creator_id=head.creator_id,
            thread_id=head.thread_id,
            item_id=head.item_id,
            variant_key=head.variant_key,
            base_job_id=head.base_job_id,
            base_generation_id=head.base_generation_id,
            draft_revision=head.draft_revision + 1,
            parent_draft_id=head.id,
            snapshot_json=head.snapshot_json,
            snapshot_hash=head.snapshot_hash,
            is_head=True,
        )
        db.add(later)
        db.commit()


def make_thread_busy(thread_id):
    with sync_session() as db:
        db.execute(
            text(
                "UPDATE creator_agent_turns SET status = 'observing' "
                "WHERE thread_id = :t AND id = (SELECT id FROM creator_agent_turns "
                "WHERE thread_id = :t ORDER BY created_at DESC LIMIT 1)"
            ),
            {"t": str(thread_id)},
        )
        db.commit()


def forbid_the_element(thread_id):
    from app.models import CreativeBriefVersion

    with sync_session() as db:
        latest = db.scalar(
            select(CreativeBriefVersion)
            .where(CreativeBriefVersion.thread_id == thread_id)
            .order_by(CreativeBriefVersion.version.desc())
        )
        locked = {
            "id": "r9",
            "kind": "timing",
            "scope": "global",
            "description": "Exact timings",
            "facts": {"sequence_timing": {"mode": "absolute"}},
        }
        db.add(
            CreativeBriefVersion(
                thread_id=thread_id,
                version=latest.version + 1,
                requirements=[*latest.requirements, locked],
            )
        )
        db.commit()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "interfere,why",
    [
        (make_head_move, "head_moved"),
        (make_thread_busy, "thread_busy"),
        (forbid_the_element, "protected"),
    ],
)
async def test_a_repair_never_overrides_what_changed_since_the_render(
    drifted, repair_on, monkeypatch, interfere, why
):
    _, thread, _, _ = drifted
    rid = await ready(drifted, monkeypatch)
    interfere(thread)
    before, seen = len(drafts(thread)), {e.id for e in events(thread)}
    assert await run_async(rid, Provider(COPY_MISMATCH)) == "suggestion"
    row = review_row(rid)
    (suggestion,) = row.review["disposition"]["suggestions"]
    assert suggestion["why"] == why and suggestion["candidate_id"]
    assert row.repair_draft_id is None and len(drafts(thread)) == before
    assert {e.id for e in events(thread)} == seen


@pytest.mark.asyncio
async def test_a_repair_flag_turned_off_after_the_review_downgrades_to_a_suggestion(
    drifted, repair_on, monkeypatch
):
    _, thread, _, _ = drifted
    rid = await ready(drifted, monkeypatch)
    count = len(drafts(thread))
    staged = flow._stage_repair_if_pending

    def flag_flipped_before_staging(review_id, outcome):
        monkeypatch.setattr(settings, "kria_intent_review_repair_enabled", False)
        return staged(review_id, outcome)

    monkeypatch.setattr(flow, "_stage_repair_if_pending", flag_flipped_before_staging)
    assert await run_async(rid, Provider(COPY_MISMATCH)) == "suggestion"
    (suggestion,) = review_row(rid).review["disposition"]["suggestions"]
    assert suggestion["why"] == "tier_review" and len(drafts(thread)) == count


@pytest.mark.asyncio
async def test_the_code_ceiling_alone_keeps_repair_off_with_every_flag_on(
    drifted, repair_on, monkeypatch
):
    _, thread, _, _ = drifted
    monkeypatch.setattr(tier, "_CODE_TIER_CEILING", "review")
    rid = await ready(drifted, monkeypatch)
    assert review_row(rid).tier == "review"
    count = len(drafts(thread))
    assert await run_async(rid, Provider(COPY_MISMATCH)) == "suggestion"
    row = review_row(rid)
    assert [s["why"] for s in row.review["disposition"]["suggestions"]] == ["tier_review"]
    assert row.repair_draft_id is None and len(drafts(thread)) == count


class Crash(BaseException):
    """A worker killed mid-flight: nothing in the code under test may catch it."""


@pytest.mark.asyncio
async def test_a_crash_before_the_draft_commits_cancels_the_turn_and_holds_the_repair_back(
    drifted, repair_on, monkeypatch
):
    _, thread, _, _ = drifted
    rid = await ready(drifted, monkeypatch)
    count = len(drafts(thread))

    def killed(staging):
        raise Crash

    monkeypatch.setattr(repair, "_drive", killed)
    with pytest.raises(Crash):
        await run_async(rid, Provider(COPY_MISMATCH))
    stranded = review_row(rid)
    assert stranded.status == "reviewing" and stranded.repair_draft_id is None
    with sync_session() as db:
        turn = db.scalar(
            select(CreatorAgentTurn).where(
                CreatorAgentTurn.client_event_id == f"intent-review:{rid}"
            )
        )
        assert turn.status == "planning"
    monkeypatch.undo()
    expire_lease(rid)
    assert flow.run_intent_review(rid) == "suggestion"
    row = review_row(rid)
    assert [s["why"] for s in row.review["disposition"]["suggestions"]] == ["thread_busy"]
    assert len(drafts(thread)) == count and len(events(thread, INTENT_REPAIR_EVENT)) == 1
    with sync_session() as db:
        assert db.get(CreatorAgentTurn, turn.id).status == "cancelled"


@pytest.mark.asyncio
async def test_a_crash_after_the_draft_commits_is_finished_by_recovery_without_a_second_draft(
    drifted, repair_on, monkeypatch
):
    _, thread, _, _ = drifted
    rid = await ready(drifted, monkeypatch)

    def killed(drafted_turn):
        raise Crash

    real = repair._finalize
    monkeypatch.setattr(repair, "_finalize", killed)
    with pytest.raises(Crash):
        await run_async(rid, Provider(COPY_MISMATCH))
    assert review_row(rid).status == "reviewing" and len(drafts(thread)) == 2
    monkeypatch.setattr(repair, "_finalize", real)
    expire_lease(rid)
    assert flow.run_intent_review(rid) == "repair_staged"
    row = review_row(rid)
    assert row.repair_draft_id == drafts(thread)[-1].id and len(drafts(thread)) == 2
    assert len(events(thread, INTENT_REPAIR_EVENT)) == 1
    assert len(events(thread, "draft_applied")) == 2


@pytest.mark.asyncio
async def test_the_planner_never_reads_or_claims_the_reviewers_system_turn(
    drifted, repair_on, monkeypatch
):
    from app.database import AsyncSessionLocal
    from app.kria import planner
    from app.tasks import kria_runtime

    _, thread, _, _ = drifted
    rid = await ready(drifted, monkeypatch)

    def killed(staging):
        raise Crash

    real = repair._drive
    monkeypatch.setattr(repair, "_drive", killed)
    with pytest.raises(Crash):
        await run_async(rid, Provider(COPY_MISMATCH))
    monkeypatch.setattr(repair, "_drive", real)

    async with AsyncSessionLocal() as db:
        transcript = await planner._load_thread_events(db, thread)
    assert INTENT_REPAIR_EVENT not in [str(entry) for entry in transcript]
    assert not any(INTENT_REPAIR_EVENT in str(entry) for entry in transcript)

    with sync_session() as db:
        turn = db.scalar(
            select(CreatorAgentTurn).where(
                CreatorAgentTurn.client_event_id == f"intent-review:{rid}"
            )
        )
        turn.lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)
        db.commit()
        turn_id = turn.id
    seen = {e.id for e in events(thread)}

    async def refuses(*args, **kwargs):
        raise AssertionError("the live planner was asked to plan the reviewer's system turn")

    monkeypatch.setattr(kria_runtime, "_plan_with_live_agent", refuses)
    assert kria_runtime.run_kria_turn.run(str(turn_id))["status"] == "failed"
    with sync_session() as db:
        assert db.get(CreatorAgentTurn, turn_id).status == "cancelled"
    assert {e.id for e in events(thread)} == seen


async def approve_the_repair(case, rid, monkeypatch, *, attempt="synthetic-publication-2"):
    """The creator taps Approve on the staged draft and the phone publishes its render."""
    from tests.kria.test_render_review_admission_postgres import approve_and_publish

    _, thread, job_id, _ = case
    row = review_row(rid)
    with sync_session() as db:
        turn_id = db.get(CreatorAgentExecution, row.repair_render_execution_id).turn_id
        user = db.get(CreationThread, thread).creator_id
    return await approve_and_publish(
        user,
        thread,
        job_id,
        turn_id,
        attempt=attempt,
        sha="b" * 64,
        path="renders/repaired.mp4",
    )


async def staged_and_rendered(case, monkeypatch, *, provider=None):
    rid = await ready(case, monkeypatch)
    assert await run_async(rid, provider or Provider(COPY_MISMATCH)) == "repair_staged"
    claim, thread, job_id, proof = await approve_the_repair(case, rid, monkeypatch)
    child = await ready((claim, thread, job_id, proof), monkeypatch)
    return rid, child


@pytest.mark.asyncio
async def test_the_repaired_render_is_reviewed_once_below_repair_and_inherits_the_request(
    drifted, repair_on, monkeypatch
):
    rid, child = await staged_and_rendered(drifted, monkeypatch)
    parent, follow_up = review_row(rid), review_row(child)
    assert follow_up.parent_review_id == rid and follow_up.tier == "review"
    assert follow_up.status == "evidence_ready"
    assert (
        follow_up.result["packet"]["original_request"]
        == parent.result["packet"]["original_request"]
    )
    assert follow_up.result["packet"]["original_request"].strip()


@pytest.mark.asyncio
async def test_a_follow_up_that_meets_the_target_verifies_the_repair_and_never_repairs_again(
    drifted, repair_on, monkeypatch
):
    _, thread, _, _ = drifted
    rid, child = await staged_and_rendered(drifted, monkeypatch)
    count = len(drafts(thread))
    met = Provider(reply(finding("R1", "met", frames=["f0"])))
    assert await run_async(child, met) == "no_findings"
    parent = review_row(rid)
    assert (parent.status, parent.reason) == ("repair_verified", "fixed")
    assert parent.review["comparison"] == {
        "child_review_id": str(child),
        "fixed": True,
        "worse": False,
    }
    assert len(drafts(thread)) == count and len(events(thread, INTENT_REPAIR_EVENT)) == 1
    assert review_row(child).repair_draft_id is None


@pytest.mark.asyncio
async def test_a_follow_up_that_cannot_confirm_leaves_the_repair_unverified_not_verified(
    drifted, repair_on, monkeypatch
):
    rid, child = await staged_and_rendered(drifted, monkeypatch)
    still = Provider(reply(finding("R1", "unmet", "copy_mismatch", ["f0"], confidence="medium")))
    assert await run_async(child, still) == "suggestion"
    parent = review_row(rid)
    assert (parent.status, parent.reason) == ("repair_unverified", "not_confirmed")
    assert parent.review["comparison"]["fixed"] is False
    assert parent.review["comparison"]["worse"] is False


@pytest.mark.asyncio
async def test_a_follow_up_that_never_lands_leaves_the_repair_unverified(
    drifted, repair_on, monkeypatch
):
    from tests.integration.test_intent_review_lifecycle_postgres import HttpError

    rid, child = await staged_and_rendered(drifted, monkeypatch)
    assert await run_async(child, Provider(HttpError(400))) == "unavailable"
    parent = review_row(rid)
    assert (parent.status, parent.reason) == ("repair_unverified", "rereview_unavailable")
    assert parent.review["comparison"]["fixed"] is None


def two_requirements(packet):
    from app.kria.brief import CreativeBrief

    old = BriefBinding.model_validate_json(packet["binding_json"])
    brief = CreativeBrief(
        version=1,
        requirements=[
            *old.brief.requirements,
            BriefRequirement(
                id="r2", kind="select", scope="global", literal=None, description="creator ask"
            ),
        ],
    )
    packet["binding_json"] = BriefBinding.create(old.thread_id, brief).model_dump_json()


@pytest.mark.asyncio
async def test_a_repair_that_loses_ground_is_undone_through_its_own_draft_chain(
    drifted, repair_on, monkeypatch
):
    _, thread, job_id, _ = drifted
    rid = await ready(drifted, monkeypatch)
    edit_packet(rid, two_requirements)
    parent_reply = reply(
        finding("R1", "unmet", "copy_mismatch", ["f0"]), finding("R2", "met", frames=["f1"])
    )
    assert await run_async(rid, Provider(parent_reply)) == "repair_staged"
    claim, _, _, proof = await approve_the_repair(drifted, rid, monkeypatch)
    child = await ready((claim, thread, job_id, proof), monkeypatch)
    edit_packet(child, two_requirements)
    worse = reply(
        finding("R1", "met", frames=["f0"]),
        finding("R2", "unmet", "wrong_content", ["f1"]),
    )
    plan_before = copy.deepcopy(job_plan(job_id))
    staged = drafts(thread)[-1]
    assert await run_async(child, Provider(worse)) == "suggestion"
    parent = review_row(rid)
    assert (parent.status, parent.reason) == ("repair_reverted", "worse")
    assert parent.review["comparison"]["worse"] is True
    chain = drafts(thread)
    head = chain[-1]
    assert head.is_head and head.id != staged.id and head.parent_draft_id == staged.id
    assert title_of(head)["text"] == DRIFTED_TITLE
    assert job_plan(job_id) == plan_before
    undone = events(thread, "draft_undone")
    assert len(undone) == 1 and undone[0].payload["restored_from_draft_id"] == str(
        staged.parent_draft_id
    )


@pytest.mark.asyncio
async def test_a_worse_repair_over_a_newer_edit_is_marked_but_not_undone(
    drifted, repair_on, monkeypatch
):
    _, thread, job_id, _ = drifted
    rid = await ready(drifted, monkeypatch)
    edit_packet(rid, two_requirements)
    parent_reply = reply(
        finding("R1", "unmet", "copy_mismatch", ["f0"]), finding("R2", "met", frames=["f1"])
    )
    assert await run_async(rid, Provider(parent_reply)) == "repair_staged"
    claim, _, _, proof = await approve_the_repair(drifted, rid, monkeypatch)
    child = await ready((claim, thread, job_id, proof), monkeypatch)
    edit_packet(child, two_requirements)
    make_head_move(thread)
    after_move = [(d.id, d.is_head) for d in drafts(thread)]
    worse = reply(
        finding("R1", "met", frames=["f0"]),
        finding("R2", "unmet", "wrong_content", ["f1"]),
    )
    assert await run_async(child, Provider(worse)) == "suggestion"
    parent = review_row(rid)
    assert (parent.status, parent.reason) == ("repair_reverted", "worse_head_moved")
    assert [(d.id, d.is_head) for d in drafts(thread)] == after_move
    assert events(thread, "draft_undone") == []


def end_the_approval(status):
    def apply(rid):
        row = review_row(rid)
        with sync_session() as db:
            execution = db.get(CreatorAgentExecution, row.repair_render_execution_id)
            approval = db.scalar(
                select(CreatorAgentApproval).where(
                    CreatorAgentApproval.turn_id == execution.turn_id
                )
            )
            approval.status = status
            db.commit()

    return apply


def fail_the_render(rid):
    row = review_row(rid)
    with sync_session() as db:
        db.get(CreatorAgentExecution, row.repair_render_execution_id).status = "failed"
        db.commit()


def let_a_day_pass(rid):
    edit_row(rid, repair_staged_at=datetime.now(UTC) - timedelta(hours=25))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "act,reason",
    [
        (end_the_approval("denied"), "approval_rejected"),
        (end_the_approval("expired"), "approval_expired"),
        (fail_the_render, "render_failed"),
        (let_a_day_pass, "unapproved_timeout"),
    ],
)
async def test_a_repair_the_creator_walks_away_from_is_abandoned_with_the_right_reason(
    drifted, repair_on, monkeypatch, act, reason
):
    rid = await ready(drifted, monkeypatch)
    assert await run_async(rid, Provider(COPY_MISMATCH)) == "repair_staged"
    assert repair.recover_repair_staged() == 0
    act(rid)
    assert repair.recover_repair_staged() == 1
    row = review_row(rid)
    assert (row.status, row.reason) == ("repair_abandoned", reason)
    assert repair.recover_repair_staged() == 0


@pytest.mark.asyncio
async def test_undoing_the_repair_draft_abandons_it_as_undone(drifted, repair_on, monkeypatch):
    _, thread, _, _ = drifted
    rid = await ready(drifted, monkeypatch)
    assert await run_async(rid, Provider(COPY_MISMATCH)) == "repair_staged"
    head = drafts(thread)[-1]
    async with AsyncSessionLocal() as db:
        await undo_draft(
            db,
            thread_id=thread,
            creator_id=review_row(rid).owner_id,
            expected_revision=head.draft_revision,
        )
    assert repair.recover_repair_staged() == 1
    row = review_row(rid)
    assert (row.status, row.reason) == ("repair_abandoned", "undone")


@pytest.mark.asyncio
async def test_a_render_that_was_never_reviewed_is_unverified_after_the_grace(
    drifted, repair_on, monkeypatch
):
    rid = await ready(drifted, monkeypatch)
    assert await run_async(rid, Provider(COPY_MISMATCH)) == "repair_staged"
    monkeypatch.setattr(settings, "kria_intent_review_enabled", False)
    claim, thread, job_id, proof = await approve_the_repair(drifted, rid, monkeypatch)
    from tests.kria.test_render_review_admission_postgres import _observe_dispatched_execution

    assert (await asyncio.to_thread(_observe_dispatched_execution, claim.execution_id))[0] == (
        "completed"
    )
    monkeypatch.setattr(settings, "kria_intent_review_enabled", True)
    assert repair.recover_repair_staged() == 0
    with sync_session() as db:
        db.get(CreatorAgentExecution, claim.execution_id).completed_at = datetime.now(
            UTC
        ) - timedelta(minutes=11)
        db.commit()
    assert repair.recover_repair_staged() == 1
    row = review_row(rid)
    assert (row.status, row.reason) == ("repair_unverified", "no_rereview")


def drive_after(monkeypatch, interfere):
    real = repair._drive

    def interfered(staging):
        interfere(staging)
        return real(staging)

    monkeypatch.setattr(repair, "_drive", interfered)


def a_message_arrives(thread_id):
    from app.tasks.kria_runtime import _append_sync_event

    with sync_session() as db:
        thread = db.get(CreationThread, thread_id)
        _append_sync_event(
            db,
            thread,
            role="user",
            event_type="user_message",
            content="Actually make the title bigger",
            payload={"runtime_version": 2},
        )
        db.commit()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "interfere,why",
    [
        (lambda thread: make_head_move(thread), "head_moved"),
        (lambda thread: a_message_arrives(thread), "thread_busy"),
    ],
)
async def test_a_change_between_the_staging_fence_and_the_draft_transaction_is_refused_inside_it(
    drifted, repair_on, monkeypatch, interfere, why
):
    _, thread, _, _ = drifted
    rid = await ready(drifted, monkeypatch)
    drive_after(monkeypatch, lambda staging: interfere(staging.thread_id))
    count, requests = len(drafts(thread)), len(events(thread, "approval_requested"))
    outcome = await run_async(rid, Provider(COPY_MISMATCH))
    assert outcome == "suggestion"
    row = review_row(rid)
    (suggestion,) = row.review["disposition"]["suggestions"]
    assert suggestion["why"] == ("head_moved" if why == "head_moved" else "thread_busy")
    assert row.repair_draft_id is None
    assert len(drafts(thread)) == count + (1 if why == "head_moved" else 0)
    assert len(events(thread, "approval_requested")) == requests
    with sync_session() as db:
        turn = db.scalar(
            select(CreatorAgentTurn).where(
                CreatorAgentTurn.client_event_id == f"intent-review:{rid}"
            )
        )
        assert turn.status == "cancelled"
        assert (
            db.scalar(select(CreatorAgentApproval).where(CreatorAgentApproval.turn_id == turn.id))
            is None
        )


@pytest.mark.asyncio
async def test_a_generation_that_changes_before_staging_is_stale_and_stages_nothing(
    drifted, repair_on, monkeypatch
):
    from tests.integration.test_intent_review_lifecycle_postgres import supersede_generation

    _, thread, job_id, _ = drifted
    rid = await ready(drifted, monkeypatch)
    staged = flow._stage_repair_if_pending

    def superseded_before_staging(review_id, outcome):
        supersede_generation(job_id)
        return staged(review_id, outcome)

    monkeypatch.setattr(flow, "_stage_repair_if_pending", superseded_before_staging)
    count, seen = len(drafts(thread)), {e.id for e in events(thread)}
    assert await run_async(rid, Provider(COPY_MISMATCH)) == "stale"
    row = review_row(rid)
    assert (row.status, row.reason) == ("stale", "generation_changed")
    assert row.repair_draft_id is None and len(drafts(thread)) == count
    assert {e.id for e in events(thread)} == seen


@pytest.mark.asyncio
async def test_a_follow_up_with_the_same_defect_never_stages_a_second_repair(
    drifted, repair_on, monkeypatch
):
    from tests.integration.test_intent_review_outcome_postgres import draft_edit

    _, thread, job_id, _ = drifted
    rid = await ready(drifted, monkeypatch)
    assert await run_async(rid, Provider(COPY_MISMATCH)) == "repair_staged"
    claim, _, _, proof = await approve_the_repair(drifted, rid, monkeypatch)
    child = await ready((claim, thread, job_id, proof), monkeypatch)
    edit_packet(child, draft_edit(lambda element: element.update(text=DRIFTED_TITLE)))
    count, seen = len(drafts(thread)), {e.id for e in events(thread)}
    assert await run_async(child, Provider(COPY_MISMATCH)) == "suggestion"
    follow_up = review_row(child)
    (candidate,) = [
        c for c in follow_up.review["catalog"]["candidates"] if c["kind"] == "restore_exact_text"
    ]
    assert [s["why"] for s in follow_up.review["disposition"]["suggestions"]] == ["tier_review"]
    assert (
        follow_up.review["disposition"]["suggestions"][0]["candidate_id"]
        == candidate["candidate_id"]
    )
    assert follow_up.repair_draft_id is None and len(drafts(thread)) == count
    assert {e.id for e in events(thread)} == seen
    parent = review_row(rid)
    assert parent.status == "repair_unverified" and parent.reason == "not_confirmed"


@pytest.mark.asyncio
async def test_what_a_repair_adds_to_the_chat_is_only_what_old_app_builds_already_render(
    drifted, repair_on, monkeypatch
):
    import re
    from pathlib import Path

    _, thread, _, _ = drifted
    rid = await ready(drifted, monkeypatch)
    seen = {e.id for e in events(thread)}
    assert await run_async(rid, Provider(COPY_MISMATCH)) == "repair_staged"
    swift = Path(__file__).parents[5] / "src/apps/ios/Kria/Features/ChatWorkspaceView.swift"
    block = re.search(
        r"conversationalAssistantEvents: Set<String> = \[(.*?)\]", swift.read_text(), re.S
    )
    allowed = set(re.findall(r'"([a-z_]+)"', block.group(1)))
    added = [e for e in events(thread) if e.id not in seen]
    assert {e.event_type for e in added if e.role == "assistant"} == {"draft_applied"}
    assert {e.event_type for e in added if e.role == "assistant"} <= allowed
    assert not {e.event_type for e in added if e.role == "system"} & allowed
