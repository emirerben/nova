from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.schemas.edit_proposal import (
    EditConversationTurn,
    EditProposalSnapshot,
    FastMontageCut,
    MediaRef,
    MixedMediaTimingProfile,
    ProposalBrief,
    StoryBeat,
    canonical_media_digest,
    parse_edit_proposal,
)
from app.services.edit_proposals import (
    GUIDED_RENDER_MAX_ATTEMPTS,
    ProposalConflictError,
    approve_proposal,
    begin_proposal_attempt,
    direction_guidance_fingerprint,
    guided_render_is_blocked,
    infer_direction_guidance,
    mark_edit_proposal_stale,
    proposal_generate_error,
    record_proposal_render_failure,
    release_edit_conversation_attempt,
    reserve_edit_conversation_attempt,
    save_edit_conversation_turn,
    save_proposal_draft,
)
from app.services.plan_clips import ensure_clip_media_ids


def _item(**overrides):
    values = {"id": uuid4(), "clip_assignments": [], "edit_proposal": None}
    values.update(overrides)
    return SimpleNamespace(**values)


def test_inferred_direction_is_fast_and_media_fingerprinted() -> None:
    item = _item(
        clip_assignments=[{"media_id": "clip-1", "gcs_path": "users/u/clip.mp4", "generation": "7"}]
    )
    guidance = infer_direction_guidance(item, media_digest="a" * 64, duration_s=15)

    assert guidance.state == "awaiting_direction_confirmation"
    assert guidance.provenance == "ai_inferred"
    assert guidance.hypothesis.direction == "fast_montage"
    assert guidance.hypothesis.pace == "fast"
    assert guidance.hypothesis.text_density == "minimal"
    assert guidance.hypothesis.audio_role == "music_led"
    assert guidance.fingerprint == direction_guidance_fingerprint(item, "a" * 64)
    item.clip_assignments[0]["generation"] = "8"
    assert guidance.fingerprint != direction_guidance_fingerprint(item, "a" * 64)


def test_pending_direction_blocks_generate_until_confirmed() -> None:
    item = _item()
    proposal = begin_proposal_attempt(item, approval_mode="auto")
    guidance = infer_direction_guidance(item, media_digest="b" * 64, duration_s=15)
    item.edit_proposal = proposal.model_copy(
        update={"status": "briefing", "media_digest": "b" * 64, "guidance": guidance}
    ).model_dump(mode="json")

    assert proposal_generate_error(item) == "direction_confirmation_required"


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


def _legacy_fast_snapshot_without_cuts() -> EditProposalSnapshot:
    return EditProposalSnapshot.model_validate(
        {**_snapshot().model_dump(mode="json"), "direction": "fast_montage"}
    )


def test_fast_montage_without_cuts_cannot_be_saved_or_approved() -> None:
    item = _item()
    proposal = begin_proposal_attempt(item)
    snapshot = _legacy_fast_snapshot_without_cuts()
    raw = dict(item.edit_proposal)
    raw.update(
        {
            "status": "drafting",
            "media_digest": canonical_media_digest(snapshot.media),
        }
    )
    item.edit_proposal = raw

    with pytest.raises(ProposalConflictError, match="proposal_replan_required"):
        save_proposal_draft(
            item,
            expected_version=proposal.proposal_version,
            snapshot=snapshot,
        )

    raw.update({"status": "draft", "draft": snapshot.model_dump(mode="json")})
    item.edit_proposal = raw
    with pytest.raises(ProposalConflictError, match="proposal_replan_required"):
        approve_proposal(item, expected_version=proposal.proposal_version)


def test_approved_legacy_fast_montage_without_cuts_requires_replan() -> None:
    item = _approved_item()
    raw = dict(item.edit_proposal)
    approved = dict(raw["last_approved"])
    approved["snapshot"] = _legacy_fast_snapshot_without_cuts().model_dump(mode="json")
    raw["last_approved"] = approved
    item.edit_proposal = raw

    assert proposal_generate_error(item) == "proposal_replan_required"


def test_proposal_generate_error_tolerates_proposal_less_dispatch_double() -> None:
    assert proposal_generate_error(SimpleNamespace()) == "proposal_required"


def _photo_only_mixed_snapshot(*, video_duration_s: float) -> EditProposalSnapshot:
    photos = [
        MediaRef(
            lane="asset",
            media_id=f"photo-{index}",
            gcs_path=f"users/u/plan/i/photo-{index}.jpg",
            generation="1",
            kind="image",
        )
        for index in range(4)
    ]
    video = MediaRef(
        lane="clip",
        media_id="video-1",
        gcs_path="users/u/plan/i/video.mp4",
        generation="1",
        kind="video",
        duration_s=video_duration_s,
    )
    return EditProposalSnapshot(
        direction="fast_montage",
        goal="Keep the photos moving",
        pace="fast",
        duration_s=3,
        title="Quick photo sequence",
        media=[*photos, video],
        story_beats=[
            StoryBeat(
                beat_id="beat-1",
                topic="Details",
                media_ids=[photo.media_id for photo in photos],
                duration_s=3,
            )
        ],
        fast_cuts=[
            FastMontageCut(
                cut_id=f"cut-{index}",
                media_id=photo.media_id,
                source_start_s=0,
                source_end_s=0.75,
                output_duration_s=0.75,
                role="hook" if index == 0 else "payoff" if index == 3 else "build",
            )
            for index, photo in enumerate(photos)
        ],
        mixed_media_timing=MixedMediaTimingProfile(
            image_hold="very_fast", video_hold="longer", boundary_style="cut"
        ),
    )


def test_mixed_media_kind_requirement_ignores_video_too_short_for_a_fast_cut() -> None:
    snapshot = _photo_only_mixed_snapshot(video_duration_s=0.2)

    assert {cut.media_id for cut in snapshot.fast_cuts or []} == {
        "photo-0",
        "photo-1",
        "photo-2",
        "photo-3",
    }


def test_mixed_media_kind_requirement_still_requires_a_renderable_video() -> None:
    with pytest.raises(ValueError, match="must use both photos and videos"):
        _photo_only_mixed_snapshot(video_duration_s=2.0)


def _approved_item():
    item = _item()
    analyzing = begin_proposal_attempt(item)
    snapshot = _snapshot()
    raw = dict(item.edit_proposal)
    raw["media_digest"] = canonical_media_digest(snapshot.media)
    raw["status"] = "drafting"
    item.edit_proposal = raw
    draft = save_proposal_draft(
        item, expected_version=analyzing.proposal_version, snapshot=snapshot
    )
    approve_proposal(item, expected_version=draft.proposal_version)
    return item


def test_render_failure_is_recorded_against_the_approved_version() -> None:
    item = _approved_item()
    approved_version = item.edit_proposal["last_approved"]["proposal_version"]

    assert record_proposal_render_failure(item, code="guided_story_duration_impossible") is True

    render_failure = item.edit_proposal["render_failure"]
    assert render_failure["proposal_version"] == approved_version
    assert render_failure["code"] == "guided_story_duration_impossible"
    assert render_failure["attempts"] == 1
    # status/last_approved are untouched -- a render failure does not un-approve
    # the plan or reset the creator's approval.
    assert item.edit_proposal["status"] == "approved"


def test_repeated_same_code_render_failure_increments_attempts() -> None:
    item = _approved_item()

    record_proposal_render_failure(item, code="guided_story_render_failed")
    record_proposal_render_failure(item, code="guided_story_render_failed")
    record_proposal_render_failure(item, code="guided_story_render_failed")

    assert item.edit_proposal["render_failure"]["attempts"] == 3


def test_render_failure_does_not_block_after_a_new_approval() -> None:
    item = _approved_item()
    record_proposal_render_failure(item, code="guided_story_duration_impossible")
    assert guided_render_is_blocked(parse_edit_proposal(item.edit_proposal)) is True

    # A fresh draft/approve cycle bumps last_approved.proposal_version.
    begin_proposal_attempt(item, brief=ProposalBrief())
    new_snapshot = _snapshot()
    raw = dict(item.edit_proposal)
    raw["media_digest"] = canonical_media_digest(new_snapshot.media)
    raw["status"] = "drafting"
    item.edit_proposal = raw
    draft = save_proposal_draft(
        item, expected_version=raw["proposal_version"], snapshot=new_snapshot
    )
    approve_proposal(item, expected_version=draft.proposal_version)

    proposal = parse_edit_proposal(item.edit_proposal)
    assert guided_render_is_blocked(proposal) is False
    assert proposal_generate_error(item) is None


def test_non_retryable_render_code_blocks_on_the_first_failure() -> None:
    # guided_story_duration_impossible is a pure function of the pinned plan
    # + media durations (no I/O), so it's genuinely non-retryable. Codes that
    # wrap I/O/subprocess calls (e.g. guided_story_media_missing) are
    # deliberately NOT in this set -- see test_transient_render_code_blocks_
    # only_at_max_attempts and the comment on _NON_RETRYABLE_GUIDED_RENDER_CODES.
    item = _approved_item()
    record_proposal_render_failure(item, code="guided_story_duration_impossible")

    assert guided_render_is_blocked(parse_edit_proposal(item.edit_proposal)) is True
    assert proposal_generate_error(item) == "proposal_render_blocked"


def test_transient_render_code_blocks_only_at_max_attempts() -> None:
    item = _approved_item()
    for _ in range(GUIDED_RENDER_MAX_ATTEMPTS - 1):
        record_proposal_render_failure(item, code="guided_story_render_failed")
        assert guided_render_is_blocked(parse_edit_proposal(item.edit_proposal)) is False
        assert proposal_generate_error(item) is None

    record_proposal_render_failure(item, code="guided_story_render_failed")
    assert item.edit_proposal["render_failure"]["attempts"] == GUIDED_RENDER_MAX_ATTEMPTS
    assert guided_render_is_blocked(parse_edit_proposal(item.edit_proposal)) is True
    assert proposal_generate_error(item) == "proposal_render_blocked"


@pytest.mark.parametrize(
    "code",
    [
        "guided_story_media_missing",
        "guided_story_media_replaced",
        "guided_story_music_missing",
    ],
)
def test_media_io_codes_get_the_transient_grace_period_not_first_hit_block(code: str) -> None:
    """guided_story_media_missing/media_replaced/music_missing are raised from a
    bare `except Exception` around GCS download / ffprobe / PIL decode / audio
    mix -- any of those can be a transient blip, not just a genuine "the media
    changed" condition, so they must NOT block on the very first occurrence."""
    item = _approved_item()
    for _ in range(GUIDED_RENDER_MAX_ATTEMPTS - 1):
        record_proposal_render_failure(item, code=code)
        assert guided_render_is_blocked(parse_edit_proposal(item.edit_proposal)) is False
        assert proposal_generate_error(item) is None

    record_proposal_render_failure(item, code=code)
    assert item.edit_proposal["render_failure"]["attempts"] == GUIDED_RENDER_MAX_ATTEMPTS
    assert guided_render_is_blocked(parse_edit_proposal(item.edit_proposal)) is True
    assert proposal_generate_error(item) == "proposal_render_blocked"


def test_render_recovery_flag_off_never_blocks(monkeypatch: pytest.MonkeyPatch) -> None:
    """Kill-switch pin: with the flag off, proposal_generate_error must return

    exactly what it returns today -- the new branch is never reached at all,
    even when a blocking render_failure is present on the envelope.
    """
    from app.services import edit_proposals as edit_proposals_module

    item = _approved_item()
    record_proposal_render_failure(item, code="guided_story_duration_impossible")
    assert guided_render_is_blocked(parse_edit_proposal(item.edit_proposal)) is True

    monkeypatch.setattr(edit_proposals_module.settings, "guided_render_recovery_enabled", False)
    assert proposal_generate_error(item) is None


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


def test_human_edited_draft_clears_auto_approval_mode() -> None:
    """P3 (2026-08-18 adversarial review): a creator submitting their own

    corrected snapshot (PATCH /edit-proposal -> save_proposal_draft with
    clear_approval_mode=True) is unambiguous manual review — a later approval
    must never still record approval_mode="auto" from the original
    auto-design reservation.
    """

    item = _item()
    analyzing = begin_proposal_attempt(item, approval_mode="auto")
    snapshot = _snapshot()
    raw = dict(item.edit_proposal)
    raw["media_digest"] = canonical_media_digest(snapshot.media)
    raw["status"] = "drafting"
    item.edit_proposal = raw
    drafted = save_proposal_draft(
        item, expected_version=analyzing.proposal_version, snapshot=snapshot
    )
    assert drafted.approval_mode == "auto"  # auto-design's OWN drafting step

    # The creator now opens the planner and saves their own edit.
    human_edit = save_proposal_draft(
        item,
        expected_version=drafted.proposal_version,
        snapshot=snapshot,
        clear_approval_mode=True,
    )
    assert human_edit.approval_mode is None

    approved = approve_proposal(item, expected_version=human_edit.proposal_version)
    assert approved.last_approved is not None
    assert approved.last_approved.approval_mode is None


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
