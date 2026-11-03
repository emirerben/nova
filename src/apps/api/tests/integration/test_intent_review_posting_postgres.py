"""What an intent review puts in the creator's chat, on the real thread, and what it leaves out.

Failure modes, written before the posting stage (matrix row in parentheses). Each has a test.

  A post the creator should never see (20):
    unavailable, stale, exhausted, review_unknown, an abandoned repair and every follow-up review
    post nothing at all: no event, no thread revision, no recorded event id, so the deterministic
    post-render receipt stands alone.
  A post twice (35):
    a redelivered run, a sweep that finds a posted row, and two posters racing on one unposted
    row add exactly one receipt and one question; the second writer finds the decision recorded.
  A post lost to a crash (20, 35):
    a worker that dies between the terminal state and the post leaves a row the sweep posts once
    (and only while the outcome is recent), after which the sweep adds nothing.
  A post about a video that is no longer the video (1):
    a render, draft or recipe change since the review means "I checked the finished video" would
    describe something the creator no longer has: nothing is posted, and that decision is
    recorded so the sweep does not retry it forever. Both ways it is noticed are guarded: the
    authority graph no longer loads (a superseded render), and it loads but its digest moved (a
    changed ownership epoch or dispatch setting), where the digest comparison is the only thing
    left between the creator and a receipt about a video they no longer have.
  A pass that is only a guess (5, 38):
    a low-confidence "met" gets no row and no "everything matches"; a review whose only passes
    are guesses posts nothing; a repair is never reported as having worked on a guess.
  A post into a conversation that moved on or is mid-turn (12, 27):
    a creator message since the render, or a turn in flight, means nothing is posted and the
    thread revision does not move, so a turn that is planning is not thrown back to plan again.
    A revert that changed the creator's draft is the one post that does not wait for a quiet
    thread, because silence would leave them with a draft that changed under them.
  A post after the kill switch (13):
    a live tier below review at post time posts nothing.
  An order the creator cannot follow (34, 12):
    the receipt is written before its question; a staged repair's card is not touched and the
    receipt follows it; a follow-up review posts nothing itself, its parent's settlement posts one
    receipt.
  A settlement that says more than it knows (31, 32, 33):
    verified, unconfirmed, reverted and reverted-over-newer-edits each post their own sentence,
    the revert's undo is already durable when it is posted, and a settlement is posted once however
    many settlers and sweeps reach it.
  A receipt that rewrites the brief (12):
    the requirement view the app reads (`GET brief`) is the deterministic ledger's; the reviewer's
    advisory receipts are never folded into it.
  A thread that is not the owner's (2):
    a thread whose creator is not the review's owner, or that is not runtime v2, gets nothing.

The only fake is the OpenAI SDK object; admission, the frozen packet, the review, the repair and
the settlement run for real on PostgreSQL.
"""

# ruff: noqa: F811

import asyncio
import json
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select, text

from app.config import settings
from app.database import AsyncSessionLocal, sync_session
from app.kria.intent_review_posts import POSTABLE_STATUSES
from app.models import (
    CreationThread,
    CreationThreadEvent,
    CreatorAgentTurn,
    CreatorGenerationReview,
)
from app.services import intent_review_flow as flow
from app.services import intent_review_post as posting
from app.services import intent_review_repair_flow as repair
from tests.integration.test_intent_review_lifecycle_postgres import (  # noqa: F401
    Provider,
    bump_ownership,
    change_dispatch_settings,
    edit_row,
    ledger,
    paid,
    review_row,
    run_async,
    supersede_generation,
)
from tests.integration.test_intent_review_outcome_postgres import (  # noqa: F401
    draft_edit,
    edit_packet,
    finding,
    ready,
    real_builder,
    reply,
    with_brief,
)
from tests.integration.test_intent_review_repair_postgres import (  # noqa: F401
    COPY_MISMATCH,
    R1_MET,
    R1_UNMET,
    R2_MET,
    R2_TITLE_UNMET,
    R2_UNMET,
    a_message_arrives,
    approve_the_repair,
    drafts,
    drifted,
    make_head_move,
    plain,
    repair_on,
    settle_in_a_worker,
    staged_and_rendered,
    two_requirements,
    two_title_requirements,
    unsettled_follow_up,
)
from tests.kria.test_render_review_admission_postgres import approved_publication  # noqa: F401
from tests.services.test_openai_vision_transport import HttpError, response

pytestmark = pytest.mark.usefixtures("release_abandoned_db_connections")


def thread_events(thread_id):
    with sync_session() as db:
        return list(
            db.scalars(
                select(CreationThreadEvent)
                .where(CreationThreadEvent.thread_id == thread_id)
                .order_by(CreationThreadEvent.sequence)
            )
        )


def seen_ids(thread_id):
    return {e.id for e in thread_events(thread_id)}


def added(thread_id, seen):
    return [e for e in thread_events(thread_id) if e.id not in seen]


def reviewer_posts(thread_id, seen=frozenset()):
    return [
        e
        for e in added(thread_id, seen)
        if e.role == "assistant" and (e.payload or {}).get("turn_value") == "intent_review"
    ]


def thread_revision(thread_id):
    with sync_session() as db:
        return db.get(CreationThread, thread_id).revision


def speak_in(thread_id, language):
    with sync_session() as db:
        thread = db.get(CreationThread, thread_id)
        thread.state = {**(thread.state or {}), "reply_language": language}
        db.commit()


def posted_decision(rid):
    return (review_row(rid).review or {}).get("posting") or {}


def silent(thread_id, seen, rid):
    assert reviewer_posts(thread_id, seen) == []
    row = review_row(rid)
    assert row.receipt_event_id is None and row.question_event_id is None


def wording_doubt(packet):
    draft_edit(lambda element: element.update(text="Bef0re"))(packet)
    packet.update(
        contract_json=json.dumps({**json.loads(packet["contract_json"]), "exact_texts": []})
    )


async def no_findings(case, monkeypatch):
    rid = await ready(case, monkeypatch)
    assert await run_async(rid, Provider(reply(finding("R1", frames=["f0"])))) == "no_findings"
    return rid


# --- what the creator sees ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_clean_review_posts_one_receipt_that_names_what_was_checked(
    approved_publication, real_builder, monkeypatch
):
    _, thread, _, _ = approved_publication
    rid = await ready(approved_publication, monkeypatch)
    seen, revision = seen_ids(thread), thread_revision(thread)
    assert await run_async(rid, Provider(reply(finding("R1", frames=["f0"])))) == "no_findings"
    (post,) = added(thread, seen)
    assert (post.role, post.event_type) == ("assistant", "assistant_response")
    assert post.content == (
        "I checked the finished video against your request. Everything I could see matches."
    )
    payload = post.payload
    assert payload["turn_value"] == "intent_review" and payload["review_id"] == str(rid)
    assert payload["reply_lead"] == post.content
    (receipt,) = payload["requirement_receipts"]
    assert (receipt["requirement_id"], receipt["status"]) == ("r1", "met")
    assert receipt["reason"] == "Checked in the video."
    assert receipt["generation_id"] == payload["render_generation_id"]
    assert receipt["brief_version"] == 1
    row = review_row(rid)
    assert row.receipt_event_id == post.id and row.question_event_id is None
    assert posted_decision(rid) == {"no_findings": "posted"}
    assert post.revision == revision + 1 == thread_revision(thread)
    assert row.status == "no_findings" and len(ledger()) == 1


@pytest.mark.asyncio
async def test_a_turkish_chat_is_answered_in_turkish(
    approved_publication, real_builder, monkeypatch
):
    _, thread, _, _ = approved_publication
    speak_in(thread, "tr")
    rid = await ready(approved_publication, monkeypatch)
    seen = seen_ids(thread)
    assert await run_async(rid, Provider(reply(finding("R1", frames=["f0"])))) == "no_findings"
    (post,) = reviewer_posts(thread, seen)
    assert post.content == "Bitmiş videoyu isteğinle karşılaştırdım. Görebildiğim her şey uyuyor."
    assert post.payload["requirement_receipts"][0]["reason"] == "Videoda kontrol edildi."


@pytest.mark.asyncio
async def test_wrong_footage_posts_a_receipt_that_changed_nothing_and_asks_nothing(
    approved_publication, real_builder, monkeypatch
):
    _, thread, _, _ = approved_publication
    rid = await ready(approved_publication, monkeypatch)
    edit_packet(rid, with_brief(*briefs_for_wrong_footage()))
    seen = seen_ids(thread)
    provider = Provider(
        reply(finding("R1", frames=["f0"]), finding("R2", "unmet", "wrong_content", ["f1"]))
    )
    assert await run_async(rid, provider) == "suggestion"
    (post,) = added(thread, seen)
    assert post.event_type == "assistant_response"
    assert "I didn't change anything" in post.content
    rows = {r["requirement_id"]: r for r in post.payload["requirement_receipts"]}
    assert (rows["r1"]["status"], rows["r2"]["status"]) == ("met", "partial")
    assert "Optional" not in rows["r2"]["reason"]
    assert review_row(rid).question_event_id is None


@pytest.mark.asyncio
async def test_a_guessed_pass_gets_no_row_and_the_headline_names_the_limit(
    approved_publication, real_builder, monkeypatch
):
    _, thread, _, _ = approved_publication
    rid = await ready(approved_publication, monkeypatch)
    edit_packet(rid, with_brief(*briefs_for_wrong_footage()))
    seen = seen_ids(thread)
    provider = Provider(
        reply(finding("R1", frames=["f0"]), finding("R2", frames=["f1"], confidence="low"))
    )
    assert await run_async(rid, provider) == "no_findings"
    (post,) = added(thread, seen)
    assert post.content == (
        "I checked frames from the finished video against your request. Everything I could see "
        "matches. I can't judge sound or motion from still frames."
    )
    assert [r["requirement_id"] for r in post.payload["requirement_receipts"]] == ["r1"]


@pytest.mark.asyncio
async def test_a_review_whose_only_pass_is_a_guess_is_not_a_review_and_posts_nothing(
    approved_publication, real_builder, monkeypatch
):
    _, thread, _, _ = approved_publication
    rid = await ready(approved_publication, monkeypatch)
    seen = seen_ids(thread)
    provider = Provider(reply(finding("R1", frames=["f0"], confidence="low")))
    assert await run_async(rid, provider) == "unavailable"
    assert review_row(rid).reason == "nothing_verifiable"
    silent(thread, seen, rid)
    assert posted_decision(rid) == {}


def briefs_for_wrong_footage():
    from tests.integration.test_intent_review_outcome_postgres import ask

    return [ask("r1", "text", "title", "Before"), ask("r2", "select")]


@pytest.mark.asyncio
async def test_a_wording_doubt_posts_its_receipt_first_and_then_one_question(
    approved_publication, real_builder, monkeypatch
):
    _, thread, _, _ = approved_publication
    rid = await ready(approved_publication, monkeypatch)
    edit_packet(rid, wording_doubt)
    seen = seen_ids(thread)
    provider = Provider(reply(finding("R1", "unmet", "copy_mismatch", ["f0"])))
    assert await run_async(rid, provider) == "needs_confirmation"
    receipt, question = added(thread, seen)
    assert (receipt.event_type, question.event_type) == ("assistant_response", "assistant_question")
    assert receipt.sequence < question.sequence
    assert question.content == (
        'The text on screen doesn\'t match what you asked for. Want me to change it to "Before"?'
    )
    assert question.payload["options"] == ['Change it to "Before"', "Keep it"]
    assert question.payload["recommended_option"] == 'Change it to "Before"'
    row = review_row(rid)
    assert (row.receipt_event_id, row.question_event_id) == (receipt.id, question.id)
    assert posted_decision(rid) == {"needs_confirmation": "posted"}
    assert drafts(thread)[-1].is_head and len(drafts(thread)) == 1


@pytest.mark.asyncio
async def test_a_staged_repair_leaves_its_card_alone_and_adds_one_receipt_after_it(
    drifted, repair_on, monkeypatch
):
    _, thread, _, _ = drifted
    rid = await ready(drifted, monkeypatch)
    seen = seen_ids(thread)
    assert await run_async(rid, Provider(COPY_MISMATCH)) == "repair_staged"
    created = added(thread, seen)
    assert [(e.role, e.event_type) for e in created] == [
        ("system", "intent_review_repair"),
        ("assistant", "draft_applied"),
        ("system", "approval_requested"),
        ("assistant", "assistant_response"),
    ]
    card, receipt = created[1], created[3]
    assert card.payload.get("turn_value") != "intent_review" and card.payload["can_undo"] is True
    assert receipt.content == (
        "I checked the finished video against your request. One thing didn't match, and a fix "
        "is waiting for your approval."
    )
    (row_,) = receipt.payload["requirement_receipts"]
    assert (row_["status"], row_["reason"]) == ("partial", "Fix waiting for your approval.")
    row = review_row(rid)
    assert row.receipt_event_id == receipt.id and row.question_event_id is None
    assert [e.event_type for e in created].count("assistant_question") == 0
    assert posted_decision(rid) == {"repair_staged": "posted"}


# --- what the creator never sees (20) ----------------------------------------------------


async def unavailable(case, monkeypatch):
    rid = await ready(case, monkeypatch)
    assert await run_async(rid, Provider(HttpError(400))) == "unavailable"
    return rid


async def unknown(case, monkeypatch):
    rid = await ready(case, monkeypatch)
    assert await run_async(rid, Provider(HttpError(500))) == "review_unknown"
    return rid


async def voided(case, monkeypatch):
    rid = await ready(case, monkeypatch)
    assert await run_async(rid, Provider(response(text="not json"))) == "unavailable"
    assert review_row(rid).reason == "invalid_output"
    return rid


async def stale(case, monkeypatch):
    rid = await ready(case, monkeypatch)
    supersede_generation(case[2])
    assert await run_async(rid, Provider(reply(finding("R1", frames=["f0"])))) == "stale"
    return rid


async def exhausted(case, monkeypatch):
    rid = await ready(case, monkeypatch)
    edit_row(rid, evidence_ready_at=datetime.now(UTC) - timedelta(hours=1))
    assert await run_async(rid, Provider(reply(finding("R1", frames=["f0"])))) == "exhausted"
    return rid


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", [unavailable, unknown, voided, stale, exhausted])
async def test_an_outcome_the_creator_has_no_use_for_posts_nothing_at_all(
    approved_publication, real_builder, monkeypatch, outcome
):
    _, thread, _, _ = approved_publication
    seen = seen_ids(thread)
    rid = await outcome(approved_publication, monkeypatch)
    revision, seen = thread_revision(thread), seen_ids(thread)
    assert posted_decision(rid) == {}
    assert posting.post_intent_review_outcome(rid) == "nothing"
    assert thread_revision(thread) == revision and seen_ids(thread) == seen
    assert review_row(rid).receipt_event_id is None and review_row(rid).question_event_id is None


@pytest.mark.asyncio
async def test_an_abandoned_repair_posts_nothing(drifted, repair_on, monkeypatch):
    _, thread, _, _ = drifted
    rid = await ready(drifted, monkeypatch)
    assert await run_async(rid, Provider(COPY_MISMATCH)) == "repair_staged"
    from tests.integration.test_intent_review_repair_postgres import end_the_approval

    end_the_approval("denied")(rid)
    seen = seen_ids(thread)
    await asyncio.to_thread(repair.recover_repair_staged)
    assert review_row(rid).status == "repair_abandoned"
    assert flow.recover_intent_reviews() >= 0
    assert reviewer_posts(thread, seen) == []


@pytest.mark.asyncio
async def test_a_follow_up_review_never_posts_and_its_parent_posts_one_settlement(
    drifted, repair_on, monkeypatch
):
    _, thread, _, _ = drifted
    rid, child = await staged_and_rendered(drifted, monkeypatch)
    seen = seen_ids(thread)
    assert await run_async(child, Provider(reply(finding("R1", frames=["f0"])))) == "no_findings"
    (post,) = reviewer_posts(thread, seen)
    assert post.payload["review_id"] == str(rid)
    assert post.content == "I checked the new video: the fix worked. You can still undo it."
    assert review_row(child).receipt_event_id is None
    assert posted_decision(child) == {}
    parent = review_row(rid)
    assert (parent.status, parent.reason) == ("repair_verified", "fixed")
    assert parent.receipt_event_id == post.id
    assert posted_decision(rid) == {"repair_staged": "posted", "repair_verified": "posted"}


# --- once (35) ----------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_redelivered_run_and_a_sweep_add_nothing_to_a_posted_outcome(
    approved_publication, real_builder, monkeypatch
):
    _, thread, _, _ = approved_publication
    seen = seen_ids(thread)
    rid = await no_findings(approved_publication, monkeypatch)
    (first,) = reviewer_posts(thread, seen)
    assert flow.run_intent_review(rid) == "ignored"
    assert posting.post_intent_review_outcome(rid) == "nothing"
    posting.post_unposted_outcomes()
    assert flow.recover_intent_reviews() >= 0
    assert [e.id for e in reviewer_posts(thread, seen)] == [first.id]
    assert review_row(rid).receipt_event_id == first.id


@pytest.mark.asyncio
async def test_a_post_lost_to_a_crash_is_made_once_by_the_sweep(
    approved_publication, real_builder, monkeypatch
):
    _, thread, _, _ = approved_publication
    seen = seen_ids(thread)
    monkeypatch.setattr(flow, "post_intent_review_outcome", lambda review_id: "crashed")
    rid = await no_findings(approved_publication, monkeypatch)
    assert reviewer_posts(thread, seen) == [] and posted_decision(rid) == {}
    assert posting.post_unposted_outcomes() >= 1
    (post,) = reviewer_posts(thread, seen)
    assert review_row(rid).receipt_event_id == post.id
    posting.post_unposted_outcomes()
    assert len(reviewer_posts(thread, seen)) == 1


@pytest.mark.asyncio
async def test_two_posters_racing_on_one_unposted_row_add_one_receipt_and_one_question(
    approved_publication, real_builder, monkeypatch
):
    _, thread, _, _ = approved_publication
    rid = await ready(approved_publication, monkeypatch)
    edit_packet(rid, wording_doubt)
    monkeypatch.setattr(flow, "post_intent_review_outcome", lambda review_id: "crashed")
    provider = Provider(reply(finding("R1", "unmet", "copy_mismatch", ["f0"])))
    assert await run_async(rid, provider) == "needs_confirmation"
    seen = seen_ids(thread)
    with ThreadPoolExecutor(4) as pool:
        results = list(pool.map(lambda _: posting.post_intent_review_outcome(rid), range(4)))
    assert results.count("posted") == 1 and results.count("nothing") == 3
    assert sorted(e.event_type for e in reviewer_posts(thread, seen)) == [
        "assistant_question",
        "assistant_response",
    ]
    sequences = [e.sequence for e in thread_events(thread)]
    assert sequences == sorted(set(sequences))


@pytest.mark.asyncio
async def test_the_sweep_only_looks_at_recent_outcomes(
    approved_publication, real_builder, monkeypatch
):
    _, thread, _, _ = approved_publication
    monkeypatch.setattr(flow, "post_intent_review_outcome", lambda review_id: "crashed")
    rid = await no_findings(approved_publication, monkeypatch)
    with sync_session() as db:
        db.execute(
            text(
                "UPDATE creator_generation_reviews SET updated_at = now() - interval '3 hours' "
                "WHERE id = :id"
            ),
            {"id": str(rid)},
        )
        db.commit()
    seen = seen_ids(thread)
    posting.post_unposted_outcomes()
    assert reviewer_posts(thread, seen) == [] and review_row(rid).receipt_event_id is None


@pytest.mark.asyncio
async def test_a_poster_never_waits_for_a_row_a_settler_holds_while_it_holds_the_thread(
    approved_publication, real_builder, monkeypatch
):
    from sqlalchemy.exc import OperationalError

    from app.database import sync_engine

    _, thread, _, _ = approved_publication
    monkeypatch.setattr(flow, "post_intent_review_outcome", lambda review_id: "crashed")
    rid = await no_findings(approved_publication, monkeypatch)
    seen = seen_ids(thread)
    with sync_engine.connect() as holder:
        holder.execute(
            text("SELECT id FROM creator_generation_reviews WHERE id = :id FOR UPDATE"),
            {"id": str(rid)},
        )
        started = time.monotonic()
        assert posting.post_intent_review_outcome(rid) == "busy"
        assert time.monotonic() - started < 5
        with sync_engine.connect() as probe:
            probe.execute(text("SET lock_timeout = '1s'"))
            try:
                probe.execute(
                    text("SELECT id FROM creation_threads WHERE id = :id FOR UPDATE"),
                    {"id": str(thread)},
                )
            except OperationalError:
                pytest.fail("the busy poster kept the thread locked")
        holder.rollback()
    assert reviewer_posts(thread, seen) == [] and posted_decision(rid) == {}
    assert posting.post_unposted_outcomes() >= 1
    assert len(reviewer_posts(thread, seen)) == 1


# --- only when it is still true and still welcome (1, 12, 13, 2) --------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change",
    [supersede_generation, change_dispatch_settings, bump_ownership],
    ids=["the_render_is_superseded", "the_dispatch_setting_moved", "the_ownership_epoch_moved"],
)
async def test_a_video_the_creator_no_longer_has_is_not_reported_on_and_the_decision_is_kept(
    approved_publication, real_builder, monkeypatch, change
):
    _, thread, job_id, _ = approved_publication
    monkeypatch.setattr(flow, "post_intent_review_outcome", lambda review_id: "crashed")
    rid = await no_findings(approved_publication, monkeypatch)
    change(job_id)
    seen, revision = seen_ids(thread), thread_revision(thread)
    assert posting.post_intent_review_outcome(rid) == "skipped"
    silent(thread, seen, rid)
    assert thread_revision(thread) == revision
    assert posted_decision(rid) == {"no_findings": "skipped:stale"}
    assert posting.post_intent_review_outcome(rid) == "nothing"
    posting.post_unposted_outcomes()


@pytest.mark.asyncio
async def test_a_creator_who_has_spoken_since_the_render_is_not_interrupted(
    approved_publication, real_builder, monkeypatch
):
    _, thread, _, _ = approved_publication
    monkeypatch.setattr(flow, "post_intent_review_outcome", lambda review_id: "crashed")
    rid = await no_findings(approved_publication, monkeypatch)
    a_message_arrives(thread)
    seen, revision = seen_ids(thread), thread_revision(thread)
    assert posting.post_intent_review_outcome(rid) == "skipped"
    silent(thread, seen, rid)
    assert thread_revision(thread) == revision
    assert posted_decision(rid) == {"no_findings": "skipped:creator_moved_on"}


@pytest.mark.asyncio
@pytest.mark.parametrize("active", ["planning", "queued", "executing", "observing"])
async def test_a_turn_in_flight_is_never_thrown_back_to_planning_by_a_receipt(
    approved_publication, real_builder, monkeypatch, active
):
    _, thread, _, _ = approved_publication
    monkeypatch.setattr(flow, "post_intent_review_outcome", lambda review_id: "crashed")
    rid = await no_findings(approved_publication, monkeypatch)
    with sync_session() as db:
        turn = db.scalar(
            select(CreatorAgentTurn)
            .where(CreatorAgentTurn.thread_id == thread)
            .order_by(CreatorAgentTurn.created_at.desc())
        )
        turn.status = active
        turn_id = turn.id
        db.commit()
    seen, revision = seen_ids(thread), thread_revision(thread)
    assert posting.post_intent_review_outcome(rid) == "busy"
    silent(thread, seen, rid)
    assert thread_revision(thread) == revision
    assert posted_decision(rid) == {}
    assert posting.post_unposted_outcomes() == 0
    silent(thread, seen, rid)

    with sync_session() as db:
        db.get(CreatorAgentTurn, turn_id).status = "completed"
        db.commit()
    assert posting.post_unposted_outcomes() == 1
    (post,) = reviewer_posts(thread, seen)
    assert posted_decision(rid) == {"no_findings": "posted"}
    assert review_row(rid).receipt_event_id == post.id


@pytest.mark.asyncio
async def test_a_switched_off_review_stops_posting_at_once(
    approved_publication, real_builder, monkeypatch
):
    _, thread, _, _ = approved_publication
    monkeypatch.setattr(flow, "post_intent_review_outcome", lambda review_id: "crashed")
    rid = await no_findings(approved_publication, monkeypatch)
    monkeypatch.setattr(settings, "kria_intent_review_paid_enabled", False)
    seen = seen_ids(thread)
    assert posting.post_intent_review_outcome(rid) == "skipped"
    silent(thread, seen, rid)
    assert posted_decision(rid) == {"no_findings": "skipped:tier_off"}


@pytest.mark.asyncio
async def test_a_thread_that_is_not_the_reviewed_owners_gets_nothing(
    drifted, repair_on, monkeypatch
):
    import uuid

    from app.models import User

    _, thread, _, _ = drifted
    rid, child = await staged_and_rendered(drifted, monkeypatch)
    seen = seen_ids(thread)
    with sync_session() as db:
        stranger = User(email=f"{uuid.uuid4()}@example.com", name="Stranger")
        db.add(stranger)
        db.flush()
        db.get(CreatorGenerationReview, rid).owner_id = stranger.id
        db.commit()
    assert await run_async(child, Provider(reply(finding("R1", frames=["f0"])))) == "no_findings"
    assert reviewer_posts(thread, seen) == []
    assert posted_decision(rid)["repair_verified"] == "skipped:thread_changed"


# --- a settled repair (31, 32, 33) --------------------------------------------------------


@pytest.mark.asyncio
async def test_an_unconfirmed_repair_posts_the_honest_sentence_with_its_fix_line(
    drifted, repair_on, monkeypatch
):
    _, thread, _, _ = drifted
    rid, child = await staged_and_rendered(drifted, monkeypatch)
    seen = seen_ids(thread)
    assert await run_async(child, Provider(HttpError(400))) == "unavailable"
    (post,) = reviewer_posts(thread, seen)
    assert post.content == (
        "The fix is in the new video, but I couldn't confirm it worked. You can still undo it."
    )
    assert post.payload["also_changed"] == ["Restored your exact wording on the title."]
    (row_,) = post.payload["requirement_receipts"]
    assert (row_["status"], row_["reason"]) == ("partial", "Fixed, but not confirmed.")
    assert review_row(rid).status == "repair_unverified"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "follow_up,settled",
    [
        (Provider(reply(finding("R1", frames=["f0"]))), "repair_verified"),
        (Provider(HttpError(400)), "repair_unverified"),
    ],
    ids=["verified", "unverified"],
)
async def test_a_settled_fix_is_not_announced_once_the_creator_edited_past_it(
    drifted, repair_on, monkeypatch, follow_up, settled
):
    _, thread, _, _ = drifted
    rid, child = await staged_and_rendered(drifted, monkeypatch)
    make_head_move(thread)
    seen = seen_ids(thread)
    await run_async(child, follow_up)
    assert review_row(rid).status == settled
    assert reviewer_posts(thread, seen) == []
    assert posted_decision(rid)[settled] == "skipped:head_moved"


@pytest.mark.asyncio
async def test_a_follow_up_that_only_guesses_the_target_is_met_never_reports_the_fix_as_worked(
    drifted, repair_on, monkeypatch
):
    _, thread, _, _ = drifted
    rid, child = await unsettled_follow_up(
        drifted,
        monkeypatch,
        [R1_UNMET, R2_MET],
        [finding("R1", "met", frames=["f0"], confidence="low"), R2_MET],
    )
    seen = seen_ids(thread)
    assert await settle_in_a_worker(child) == "repair_unverified"
    (post,) = reviewer_posts(thread, seen)
    assert post.content == (
        "The fix is in the new video, but I couldn't confirm it worked. You can still undo it."
    )
    (row_,) = post.payload["requirement_receipts"]
    assert (row_["requirement_id"], row_["status"]) == ("r1", "partial")
    assert (review_row(rid).status, review_row(rid).reason) == (
        "repair_unverified",
        "not_confirmed",
    )


@pytest.mark.asyncio
async def test_a_follow_up_that_never_lands_is_reported_once_by_the_sweep(
    drifted, repair_on, monkeypatch
):
    from tests.kria.test_render_review_admission_postgres import _observe_dispatched_execution

    _, thread, _, _ = drifted
    rid = await ready(drifted, monkeypatch)
    assert await run_async(rid, Provider(COPY_MISMATCH)) == "repair_staged"
    monkeypatch.setattr(settings, "kria_intent_review_enabled", False)
    claim, _, _, _ = await approve_the_repair(drifted, rid, monkeypatch)
    assert (await asyncio.to_thread(_observe_dispatched_execution, claim.execution_id))[0] == (
        "completed"
    )
    monkeypatch.setattr(settings, "kria_intent_review_enabled", True)
    monkeypatch.setattr(repair, "REREVIEW_GRACE", timedelta(0))
    seen = seen_ids(thread)
    flow.recover_intent_reviews()
    assert (review_row(rid).status, review_row(rid).reason) == ("repair_unverified", "no_rereview")
    (post,) = reviewer_posts(thread, seen)
    assert "couldn't confirm" in post.content
    flow.recover_intent_reviews()
    assert len(reviewer_posts(thread, seen)) == 1


@pytest.mark.asyncio
async def test_a_worse_repair_posts_after_its_undo_is_durable_and_never_claims_the_old_video(
    drifted, repair_on, monkeypatch
):
    _, thread, _, _ = drifted
    rid, child = await unsettled_follow_up(
        drifted,
        monkeypatch,
        [R1_UNMET, R2_MET],
        [R1_MET, R2_TITLE_UNMET],
        second=two_title_requirements,
    )
    seen = seen_ids(thread)
    assert await settle_in_a_worker(child) == "repair_reverted"
    created = added(thread, seen)
    assert [e.event_type for e in created] == ["draft_undone", "assistant_response"]
    post = created[1]
    assert post.content == (
        "The fix made the video worse, so I undid it in your draft. The video still shows the "
        "fixed version. Ask me to render your previous version if you want it."
    )
    (row_,) = post.payload["requirement_receipts"]
    assert (row_["requirement_id"], row_["reason"]) == ("r1", "Fix undone in your draft.")
    assert review_row(rid).receipt_event_id == post.id
    assert await settle_in_a_worker(child) is None
    assert len(reviewer_posts(thread, seen)) == 1


@pytest.mark.asyncio
async def test_a_worse_repair_is_reported_even_to_a_creator_who_has_moved_on(
    drifted, repair_on, monkeypatch
):
    _, thread, _, _ = drifted
    rid, child = await unsettled_follow_up(
        drifted,
        monkeypatch,
        [R1_UNMET, R2_MET],
        [R1_MET, R2_TITLE_UNMET],
        second=two_title_requirements,
    )
    a_message_arrives(thread)
    seen = seen_ids(thread)
    assert await settle_in_a_worker(child) == "repair_reverted"
    (post,) = reviewer_posts(thread, seen)
    assert "I undid it in your draft" in post.content
    assert review_row(rid).reason == "worse"


@pytest.mark.asyncio
async def test_a_worse_repair_over_newer_edits_says_it_touched_nothing(
    drifted, repair_on, monkeypatch
):
    _, thread, job_id, _ = drifted
    rid = await ready(drifted, monkeypatch)
    edit_packet(rid, two_title_requirements)
    staged = reply(
        finding("R1", "unmet", "copy_mismatch", ["f0"]), finding("R2", "met", frames=["f1"])
    )
    assert await run_async(rid, Provider(staged)) == "repair_staged"
    claim, _, _, proof = await approve_the_repair(drifted, rid, monkeypatch)
    child = await ready((claim, thread, job_id, proof), monkeypatch)
    edit_packet(child, two_title_requirements)
    make_head_move(thread)
    seen = seen_ids(thread)
    worse = reply(finding("R1", "met", frames=["f0"]), R2_TITLE_UNMET)
    assert await run_async(child, Provider(worse)) == "suggestion"
    (post,) = reviewer_posts(thread, seen)
    assert post.content.startswith("The fix may have made the video worse.")
    assert "requirement_receipts" not in post.payload
    assert [e.event_type for e in added(thread, seen)] == ["assistant_response"]
    assert review_row(rid).reason == "worse_head_moved"


@pytest.mark.asyncio
async def test_a_creator_who_spoke_after_the_repaired_render_is_not_told_the_fix_worked(
    drifted, repair_on, monkeypatch
):
    _, thread, _, _ = drifted
    rid, child = await unsettled_follow_up(
        drifted, monkeypatch, [R1_UNMET, R2_MET], [R1_MET, R2_MET]
    )
    a_message_arrives(thread)
    seen = seen_ids(thread)
    assert await settle_in_a_worker(child) == "repair_verified"
    assert reviewer_posts(thread, seen) == []
    assert posted_decision(rid)["repair_verified"] == "skipped:creator_moved_on"


# --- the brief the app reads (12) --------------------------------------------------------


@pytest.mark.asyncio
async def test_reviewer_receipts_are_never_folded_into_the_requirement_view_the_app_reads(
    approved_publication, real_builder, monkeypatch
):
    from app.kria.runtime import read_creative_brief

    _, thread, _, _ = approved_publication
    with sync_session() as db:
        creator = db.get(CreationThread, thread).creator_id
    monkeypatch.setattr(settings, "kria_creative_brief_enabled", True)
    monkeypatch.setattr(settings, "kria_creative_brief_user_ids", ["*"])

    async def view():
        async with AsyncSessionLocal() as db:
            return (await read_creative_brief(db, thread_id=thread, creator_id=creator)).model_dump(
                mode="json"
            )

    rid = await ready(approved_publication, monkeypatch)
    edit_packet(rid, wording_doubt)
    before = await view()
    provider = Provider(reply(finding("R1", "unmet", "copy_mismatch", ["f0"])))
    assert await run_async(rid, provider) == "needs_confirmation"
    assert reviewer_posts(thread)
    assert await view() == before


# --- every status the reviewer can post is one the sweep can find ------------------------


def test_the_sweep_and_the_composer_agree_on_which_statuses_post():
    assert posting.SWEEP_STATUSES == POSTABLE_STATUSES
    assert posting.INFORMATIONAL == POSTABLE_STATUSES - {"repair_reverted"}
