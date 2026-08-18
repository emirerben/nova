from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from app.schemas.edit_proposal import (
    EditConversationTurn,
    EditProposalSnapshot,
    MediaRef,
    ProposalBrief,
    StoryBeat,
    canonical_media_digest,
)
from app.services.edit_proposals import (
    ProposalConflictError,
    approve_proposal,
    begin_proposal_attempt,
    mark_edit_proposal_stale,
    proposal_generate_error,
    release_edit_conversation_attempt,
    reserve_edit_conversation_attempt,
    save_edit_conversation_turn,
    save_proposal_draft,
)
from app.services.plan_clips import ensure_clip_media_ids


def _item(**overrides):
    values = {"clip_assignments": [], "edit_proposal": None}
    values.update(overrides)
    return SimpleNamespace(**values)


def _snapshot() -> EditProposalSnapshot:
    media = [
        MediaRef(
            lane="clip",
            media_id="clip-1",
            gcs_path="users/u/plan/i/corfu.mp4",
            generation="42",
            kind="video",
        )
    ]
    return EditProposalSnapshot(
        direction="guided_story",
        goal="Share what stood out in Corfu",
        pace="balanced",
        duration_s=24,
        title="What I noticed in Corfu",
        media=media,
        story_beats=[
            StoryBeat(
                beat_id="beat-1",
                topic="Coast",
                thought="The water set the pace for the whole trip.",
                media_ids=["clip-1"],
                duration_s=4,
            )
        ],
    )


def test_canonical_digest_ignores_editorial_order_but_tracks_generation() -> None:
    a = MediaRef(lane="clip", media_id="a", gcs_path="a.mp4", generation="1", kind="video")
    b = MediaRef(lane="asset", media_id="b", gcs_path="b.jpg", generation="2", kind="image")
    assert canonical_media_digest([a, b]) == canonical_media_digest([b, a])
    assert canonical_media_digest([a, b]) != canonical_media_digest(
        [a, b.model_copy(update={"generation": "3"})]
    )


def test_clip_ids_are_backfilled_once() -> None:
    item = _item(clip_assignments=[{"gcs_path": "one.mp4", "shot_id": None}])
    assert ensure_clip_media_ids(item) is True
    media_id = item.clip_assignments[0]["media_id"]
    assert ensure_clip_media_ids(item) is False
    assert item.clip_assignments[0]["media_id"] == media_id


def test_draft_approval_and_media_stale_retain_last_approval() -> None:
    item = _item()
    analyzing = begin_proposal_attempt(item)
    snapshot = _snapshot()
    # The drafting task owns the immutable media digest.
    raw = dict(item.edit_proposal)
    raw["media_digest"] = canonical_media_digest(snapshot.media)
    raw["status"] = "drafting"
    item.edit_proposal = raw

    draft = save_proposal_draft(
        item, expected_version=analyzing.proposal_version, snapshot=snapshot
    )
    approved = approve_proposal(item, expected_version=draft.proposal_version)
    assert proposal_generate_error(item) is None
    assert approved.last_approved is not None

    assert mark_edit_proposal_stale(item) is True
    assert proposal_generate_error(item) == "proposal_stale"
    assert item.edit_proposal["last_approved"]["snapshot"]["title"] == snapshot.title


def test_compare_and_swap_rejects_lost_update() -> None:
    item = _item()
    current = begin_proposal_attempt(item)
    with pytest.raises(ProposalConflictError, match="another tab"):
        save_proposal_draft(
            item,
            expected_version=current.proposal_version - 1,
            snapshot=_snapshot(),
        )


def test_conversation_persists_typed_brief_without_starting_analysis() -> None:
    item = _item()
    saved = save_edit_conversation_turn(
        item,
        expected_version=0,
        brief=ProposalBrief(
            direction="fast_montage",
            goal="Show the food and town quickly",
            pace="fast",
            duration_s=20,
        ),
        user_message="Make it quick and fun, mostly food and town.",
        agent_reply="I’ll make a quick, music-led highlight reel.",
        suggestions=["Keep a short title"],
        ready_to_plan=True,
    )
    assert saved.status == "briefing"
    assert saved.brief.direction == "fast_montage"
    assert saved.brief_ready is True
    assert [turn.role for turn in saved.conversation] == ["user", "agent"]
    assert proposal_generate_error(item) == "proposal_draft"


def test_conversation_attempt_is_single_flight_and_releasable() -> None:
    item = _item()
    reserved, token = reserve_edit_conversation_attempt(item, expected_version=0)
    assert reserved.conversation_attempt is not None
    with pytest.raises(ProposalConflictError, match="already thinking"):
        reserve_edit_conversation_attempt(item, expected_version=0)
    assert release_edit_conversation_attempt(item, token=token) is True
    assert item.edit_proposal is None


def test_stale_conversation_attempt_can_be_reclaimed() -> None:
    item = _item()
    started = datetime(2026, 8, 16, tzinfo=UTC)
    reserved, first_token = reserve_edit_conversation_attempt(item, expected_version=0, now=started)
    reclaimed, second_token = reserve_edit_conversation_attempt(
        item,
        # A reload sees the synthetic placeholder's public version rather
        # than the original first-turn expected version of zero.
        expected_version=reserved.proposal_version,
        now=started + timedelta(seconds=91),
    )
    assert first_token != second_token
    assert reclaimed.conversation_attempt is not None
    assert reclaimed.conversation_attempt.token == second_token


@pytest.mark.parametrize("status", ["failed", "stale"])
def test_conversation_survives_recovery_states(status: str) -> None:
    item = _item()
    analyzing = begin_proposal_attempt(item)
    snapshot = _snapshot()
    item.edit_proposal = {
        **item.edit_proposal,
        "media_digest": canonical_media_digest(snapshot.media),
        "status": "drafting",
        "conversation": [
            EditConversationTurn(role="user", content="Keep the food and coastline.").model_dump(
                mode="json"
            ),
            EditConversationTurn(
                role="agent", content="I’ll make those the main chapters."
            ).model_dump(mode="json"),
        ],
        "brief_ready": True,
    }
    draft = save_proposal_draft(
        item,
        expected_version=analyzing.proposal_version,
        snapshot=snapshot,
    )
    approved = approve_proposal(item, expected_version=draft.proposal_version)
    item.edit_proposal = {
        **item.edit_proposal,
        "status": status,
        "failure": (
            {"code": "proposal_failed", "message": "Try again.", "retryable": True}
            if status == "failed"
            else None
        ),
    }

    recovered = save_edit_conversation_turn(
        item,
        expected_version=approved.proposal_version,
        brief=ProposalBrief(goal="A reflective travel diary"),
        user_message="Also keep the architecture.",
        agent_reply="I’ll include the town as a third chapter.",
        suggestions=[],
        ready_to_plan=True,
    )

    assert [turn.content for turn in recovered.conversation] == [
        "Keep the food and coastline.",
        "I’ll make those the main chapters.",
        "Also keep the architecture.",
        "I’ll include the town as a third chapter.",
    ]
    assert recovered.draft == snapshot
    assert recovered.last_approved == approved.last_approved


@pytest.mark.parametrize("status", ["analyzing", "drafting"])
def test_conversation_rejects_active_proposal_attempt(status: str) -> None:
    item = _item()
    active = begin_proposal_attempt(item)
    item.edit_proposal = {**item.edit_proposal, "status": status}

    with pytest.raises(ProposalConflictError, match="already building"):
        save_edit_conversation_turn(
            item,
            expected_version=active.proposal_version,
            brief=ProposalBrief(),
            user_message="Make it faster.",
            agent_reply="I’ll speed it up.",
            suggestions=[],
            ready_to_plan=True,
        )


def test_conversation_keeps_latest_twenty_turns_and_attempt_preserves_brief() -> None:
    item = _item()
    prior = [
        EditConversationTurn(role="user" if index % 2 == 0 else "agent", content=f"turn {index}")
        for index in range(20)
    ]
    item.edit_proposal = {
        **begin_proposal_attempt(item).model_dump(mode="json"),
        "status": "briefing",
        "conversation": [turn.model_dump(mode="json") for turn in prior],
        "brief_ready": True,
    }

    saved = save_edit_conversation_turn(
        item,
        expected_version=1,
        brief=ProposalBrief(goal="Keep the whole trip"),
        user_message="new user turn",
        agent_reply="new agent turn",
        suggestions=[],
        ready_to_plan=True,
    )
    assert len(saved.conversation) == 20
    assert saved.conversation[0].content == "turn 2"
    assert saved.conversation[-1].content == "new agent turn"

    attempt = begin_proposal_attempt(item, brief=saved.brief)
    assert attempt.conversation == saved.conversation
    assert attempt.brief_ready is True


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        ("analyzing", "proposal_analyzing"),
        ("drafting", "proposal_analyzing"),
        ("briefing", "proposal_draft"),
        ("draft", "proposal_draft"),
        ("failed", "proposal_failed"),
        ("stale", "proposal_stale"),
    ],
)
def test_generate_error_codes_are_stable(status: str, expected: str) -> None:
    item = _item()
    proposal = begin_proposal_attempt(item)
    item.edit_proposal = {**item.edit_proposal, "status": status}
    assert item.edit_proposal["proposal_version"] == proposal.proposal_version
    assert proposal_generate_error(item) == expected


def test_edit_conversation_attempt_ttl_matches_proxy_max_duration() -> None:
    from app.services.edit_proposals import EDIT_CONVERSATION_ATTEMPT_TTL_S

    # Must match src/apps/web/src/lib/api-proxy.ts proxyMaxDuration (60s) so a
    # client-visible proxy timeout and the server-side reservation expire
    # together (Task 5, was 90s).
    assert EDIT_CONVERSATION_ATTEMPT_TTL_S == 60


def test_auto_design_approval_mode_survives_draft_and_approve() -> None:
    """GUIDED_AUTO_DESIGN_ENABLED state machine: approval_mode="auto" set at

    reservation time rides through drafting onto the ApprovedProposalSnapshot,
    distinctly from any later manual reservation that overwrites the mutable
    envelope field.
    """

    item = _item()
    analyzing = begin_proposal_attempt(item, approval_mode="auto")
    assert analyzing.approval_mode == "auto"
    assert item.edit_proposal["approval_mode"] == "auto"

    snapshot = _snapshot()
    raw = dict(item.edit_proposal)
    raw["media_digest"] = canonical_media_digest(snapshot.media)
    raw["status"] = "drafting"
    item.edit_proposal = raw

    draft = save_proposal_draft(
        item, expected_version=analyzing.proposal_version, snapshot=snapshot
    )
    assert draft.approval_mode == "auto"  # model_copy preserves fields it doesn't update

    approved = approve_proposal(item, expected_version=draft.proposal_version)
    assert approved.status == "approved"
    assert approved.last_approved is not None
    assert approved.last_approved.approval_mode == "auto"

    # A later EXPLICIT (manual) reservation does not retroactively rewrite the
    # already-approved snapshot's recorded approval_mode.
    begin_proposal_attempt(item, brief=ProposalBrief())
    assert item.edit_proposal["approval_mode"] is None
    assert item.edit_proposal["last_approved"]["approval_mode"] == "auto"


def test_manual_approval_mode_defaults_to_none() -> None:
    item = _item()
    analyzing = begin_proposal_attempt(item)
    assert analyzing.approval_mode is None

    snapshot = _snapshot()
    raw = dict(item.edit_proposal)
    raw["media_digest"] = canonical_media_digest(snapshot.media)
    raw["status"] = "drafting"
    item.edit_proposal = raw
    draft = save_proposal_draft(
        item, expected_version=analyzing.proposal_version, snapshot=snapshot
    )
    approved = approve_proposal(item, expected_version=draft.proposal_version)
    assert approved.last_approved is not None
    assert approved.last_approved.approval_mode is None
