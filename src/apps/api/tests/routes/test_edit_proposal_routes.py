from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException
from starlette.requests import Request

import app.routes.plan_items as plan_items
from app.agents.edit_guide import EditGuideOutput, EditGuideRevision, EditGuideRevisionBeat
from app.schemas.edit_proposal import (
    EditProposal,
    EditProposalSnapshot,
    FastMontageCut,
    MediaRef,
    MixedMediaTimingProfile,
    MontageCadenceConstraint,
    ProposalBrief,
    StoryBeat,
    canonical_media_digest,
    parse_edit_proposal,
)
from app.services.edit_proposals import infer_direction_guidance


def _snapshot() -> EditProposalSnapshot:
    media = MediaRef(
        lane="clip",
        media_id="clip-1",
        gcs_path="users/u/plan/i/corfu.mp4",
        generation="42",
        kind="video",
        duration_s=30,
    )
    return EditProposalSnapshot(
        direction="guided_story",
        goal="Share what stood out",
        pace="balanced",
        duration_s=24,
        title="What I noticed in Corfu",
        media=[media],
        story_beats=[
            StoryBeat(
                beat_id="coast",
                topic="Coast",
                thought="The water set the pace.",
                media_ids=[media.media_id],
                duration_s=4,
            )
        ],
    )


def _draft_item() -> SimpleNamespace:
    snapshot = _snapshot()
    proposal = EditProposal(
        proposal_version=2,
        generation_attempt_id="attempt-1",
        media_digest=canonical_media_digest(snapshot.media),
        status="draft",
        brief=ProposalBrief(),
        draft=snapshot,
    )
    return SimpleNamespace(
        id=uuid.uuid4(),
        clip_assignments=[
            {
                "media_id": snapshot.media[0].media_id,
                "gcs_path": snapshot.media[0].gcs_path,
            }
        ],
        edit_proposal=proposal.model_dump(mode="json"),
    )


def _awaiting_direction_item() -> SimpleNamespace:
    snapshot = _snapshot()
    item = SimpleNamespace(
        id=uuid.uuid4(),
        clip_gcs_paths=[snapshot.media[0].gcs_path],
        clip_assignments=[
            {
                "media_id": snapshot.media[0].media_id,
                "gcs_path": snapshot.media[0].gcs_path,
                "generation": snapshot.media[0].generation,
            }
        ],
        edit_proposal=None,
        voiceover_gcs_path=None,
        edit_format="montage",
    )
    digest = canonical_media_digest(snapshot.media)
    guidance = infer_direction_guidance(item, media_digest=digest, duration_s=15)
    item.edit_proposal = EditProposal(
        proposal_version=3,
        generation_attempt_id="attempt-inferred",
        media_digest=digest,
        status="briefing",
        approval_mode="auto",
        guidance=guidance,
        brief=ProposalBrief(
            direction="fast_montage",
            goal="Show the strongest visual moments quickly.",
            pace="fast",
            duration_s=15,
        ),
    ).model_dump(mode="json")
    return item


def _post_request() -> Request:
    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/",
            "headers": [],
            "client": ("127.0.0.1", 1234),
            "scheme": "http",
            "server": ("test", 80),
            "query_string": b"",
        }
    )


@pytest.mark.asyncio
async def test_confirm_direction_is_versioned_and_dispatches_once(monkeypatch) -> None:
    item = _awaiting_direction_item()
    item.edit_proposal["brief"]["creator_request"] = (
        "Photos should have a very fast transition, videos can be a bit longer"
    )
    item.edit_proposal["brief"]["mixed_media_timing"] = {
        "image_hold": "very_fast",
        "video_hold": "longer",
        "boundary_style": "cut",
    }
    item.edit_proposal["brief"]["output_orientation"] = "portrait"
    proposal = parse_edit_proposal(item.edit_proposal)
    assert proposal is not None and proposal.guidance is not None
    plan = SimpleNamespace(ownership_epoch=4)
    user = SimpleNamespace(id=uuid.uuid4())
    db = AsyncMock()
    db.execute.return_value = SimpleNamespace(scalars=lambda: [])
    dispatches: list[dict] = []

    monkeypatch.setattr(plan_items.settings, "guided_edit_direction_confirmation_enabled", True)
    monkeypatch.setattr(plan_items, "_require_guided_edit", lambda: None)
    monkeypatch.setattr(plan_items, "_require_guided_edit_applicable", lambda _item: None)
    monkeypatch.setattr(
        plan_items,
        "_load_owned_item_context",
        AsyncMock(return_value=(item, plan, None)),
    )
    monkeypatch.setattr(plan_items, "_load_owned_item", AsyncMock(return_value=item))
    monkeypatch.setattr(plan_items, "plan_item_response", lambda loaded, **_kw: loaded)
    monkeypatch.setattr("app.services.plan_clips.ensure_clip_media_ids", lambda _item: False)
    monkeypatch.setattr(
        "app.services.edit_proposals.media_generations_match_sync", lambda _refs: True
    )
    monkeypatch.setattr(
        "app.tasks.edit_proposal_build.draft_edit_proposal.apply_async",
        lambda **kwargs: dispatches.append(kwargs),
    )

    result = await plan_items.confirm_item_edit_direction(
        _post_request(),
        str(item.id),
        plan_items.ConfirmDirectionBody(
            expected_proposal_version=proposal.proposal_version,
            fingerprint=proposal.guidance.fingerprint,
        ),
        user,
        db,
    )

    saved = parse_edit_proposal(item.edit_proposal)
    assert result is item
    assert saved is not None and saved.guidance is not None
    assert saved.status == "analyzing"
    assert saved.guidance.state == "confirmed"
    assert saved.guidance.provenance == "creator_confirmed"
    assert saved.approval_mode is None
    assert saved.brief.creator_request.startswith("Photos should")
    assert saved.brief.mixed_media_timing == MixedMediaTimingProfile(
        image_hold="very_fast",
        video_hold="longer",
        boundary_style="cut",
    )
    assert saved.brief.output_orientation == "portrait"
    assert len(dispatches) == 1

    # A lost-response retry is idempotent, but a conflicting override is not.
    await plan_items.confirm_item_edit_direction(
        _post_request(),
        str(item.id),
        plan_items.ConfirmDirectionBody(
            expected_proposal_version=proposal.proposal_version,
            fingerprint=proposal.guidance.fingerprint,
        ),
        user,
        db,
    )
    assert len(dispatches) == 1
    with pytest.raises(HTTPException) as conflict:
        await plan_items.confirm_item_edit_direction(
            _post_request(),
            str(item.id),
            plan_items.ConfirmDirectionBody(
                expected_proposal_version=proposal.proposal_version,
                fingerprint=proposal.guidance.fingerprint,
                direction="guided_story",
            ),
            user,
            db,
        )
    assert conflict.value.detail["code"] == "proposal_conflict"


@pytest.mark.asyncio
async def test_confirm_direction_rejects_stale_version_without_dispatch(monkeypatch) -> None:
    item = _awaiting_direction_item()
    proposal = parse_edit_proposal(item.edit_proposal)
    assert proposal is not None and proposal.guidance is not None
    user = SimpleNamespace(id=uuid.uuid4())
    dispatches: list[dict] = []

    monkeypatch.setattr(plan_items.settings, "guided_edit_direction_confirmation_enabled", True)
    monkeypatch.setattr(plan_items, "_require_guided_edit", lambda: None)
    monkeypatch.setattr(plan_items, "_require_guided_edit_applicable", lambda _item: None)
    monkeypatch.setattr(
        plan_items,
        "_load_owned_item_context",
        AsyncMock(return_value=(item, SimpleNamespace(ownership_epoch=0), None)),
    )
    monkeypatch.setattr(
        "app.tasks.edit_proposal_build.draft_edit_proposal.apply_async",
        lambda **kwargs: dispatches.append(kwargs),
    )

    with pytest.raises(HTTPException) as exc_info:
        await plan_items.confirm_item_edit_direction(
            _post_request(),
            str(item.id),
            plan_items.ConfirmDirectionBody(
                expected_proposal_version=proposal.proposal_version - 1,
                fingerprint=proposal.guidance.fingerprint,
            ),
            user,
            AsyncMock(),
        )

    assert exc_info.value.status_code == 409
    assert exc_info.value.detail["code"] == "proposal_conflict"
    assert dispatches == []


@pytest.mark.asyncio
async def test_confirm_direction_rejects_changed_media_generation(monkeypatch) -> None:
    item = _awaiting_direction_item()
    proposal = parse_edit_proposal(item.edit_proposal)
    assert proposal is not None and proposal.guidance is not None
    item.clip_assignments[0]["generation"] = "replacement-generation"
    user = SimpleNamespace(id=uuid.uuid4())
    db = AsyncMock()
    db.execute.return_value = SimpleNamespace(scalars=lambda: [])
    dispatches: list[dict] = []

    monkeypatch.setattr(plan_items.settings, "guided_edit_direction_confirmation_enabled", True)
    monkeypatch.setattr(plan_items, "_require_guided_edit", lambda: None)
    monkeypatch.setattr(plan_items, "_require_guided_edit_applicable", lambda _item: None)
    monkeypatch.setattr(
        plan_items,
        "_load_owned_item_context",
        AsyncMock(return_value=(item, SimpleNamespace(ownership_epoch=0), None)),
    )
    monkeypatch.setattr(
        "app.services.edit_proposals.media_generations_match_sync", lambda _refs: True
    )
    monkeypatch.setattr(
        "app.tasks.edit_proposal_build.draft_edit_proposal.apply_async",
        lambda **kwargs: dispatches.append(kwargs),
    )

    with pytest.raises(HTTPException) as exc:
        await plan_items.confirm_item_edit_direction(
            _post_request(),
            str(item.id),
            plan_items.ConfirmDirectionBody(
                expected_proposal_version=proposal.proposal_version,
                fingerprint=proposal.guidance.fingerprint,
            ),
            user,
            db,
        )

    assert exc.value.status_code == 409
    assert exc.value.detail["code"] == "proposal_stale"
    assert dispatches == []


@pytest.mark.asyncio
async def test_confirm_direction_retries_a_failed_dispatch(monkeypatch) -> None:
    item = _awaiting_direction_item()
    proposal = parse_edit_proposal(item.edit_proposal)
    assert proposal is not None and proposal.guidance is not None
    user = SimpleNamespace(id=uuid.uuid4())
    db = AsyncMock()
    db.execute.return_value = SimpleNamespace(scalars=lambda: [])
    dispatch_attempts = 0

    def dispatch(**_kwargs) -> None:
        nonlocal dispatch_attempts
        dispatch_attempts += 1
        if dispatch_attempts == 1:
            raise RuntimeError("broker unavailable")

    monkeypatch.setattr(plan_items.settings, "guided_edit_direction_confirmation_enabled", True)
    monkeypatch.setattr(plan_items, "_require_guided_edit", lambda: None)
    monkeypatch.setattr(plan_items, "_require_guided_edit_applicable", lambda _item: None)
    monkeypatch.setattr(
        plan_items,
        "_load_owned_item_context",
        AsyncMock(return_value=(item, SimpleNamespace(ownership_epoch=0), None)),
    )
    monkeypatch.setattr(plan_items, "_load_owned_item", AsyncMock(return_value=item))
    monkeypatch.setattr(plan_items, "plan_item_response", lambda loaded, **_kw: loaded)
    monkeypatch.setattr("app.services.plan_clips.ensure_clip_media_ids", lambda _item: False)
    monkeypatch.setattr(
        "app.services.edit_proposals.media_generations_match_sync", lambda _refs: True
    )
    monkeypatch.setattr(
        "app.tasks.edit_proposal_build.draft_edit_proposal.apply_async",
        dispatch,
    )
    body = plan_items.ConfirmDirectionBody(
        expected_proposal_version=proposal.proposal_version,
        fingerprint=proposal.guidance.fingerprint,
    )

    with pytest.raises(HTTPException) as first:
        await plan_items.confirm_item_edit_direction(_post_request(), str(item.id), body, user, db)
    assert first.value.status_code == 503
    failed = parse_edit_proposal(item.edit_proposal)
    assert failed is not None and failed.status == "failed"

    result = await plan_items.confirm_item_edit_direction(
        _post_request(), str(item.id), body, user, db
    )

    assert result is item
    retried = parse_edit_proposal(item.edit_proposal)
    assert retried is not None and retried.status == "analyzing"
    assert dispatch_attempts == 2


def test_snapshot_revision_rejoins_reassigned_media_aliases() -> None:
    current = _snapshot()
    second = MediaRef(
        lane="clip",
        media_id="clip-2",
        gcs_path="users/u/plan/i/istanbul.mp4",
        generation="43",
        kind="video",
        duration_s=30,
    )
    current.media.append(second)
    current.story_beats.append(
        StoryBeat(
            beat_id="city",
            topic="Cityscape",
            thought="Istanbul",
            media_ids=[second.media_id],
            duration_s=4,
        )
    )
    revision = EditGuideRevision(
        direction="guided_story",
        goal="Match every city label to its video",
        pace="balanced",
        duration_s=24,
        title="Summer 26",
        story_beats=[
            EditGuideRevisionBeat(
                beat_id="coast",
                topic="Lisbon",
                thought="Lisbon",
                layout="fullscreen",
                duration_s=4,
                media_refs=["media_2"],
            ),
            EditGuideRevisionBeat(
                beat_id="city",
                topic="Istanbul",
                thought="Istanbul",
                layout="fullscreen",
                duration_s=4,
                media_refs=["media_1"],
            ),
        ],
    )

    revised = plan_items._snapshot_from_edit_guide_revision(current, revision)

    assert [beat.media_ids for beat in revised.story_beats] == [["clip-2"], ["clip-1"]]


def test_snapshot_revision_preserves_mixed_media_timing_profile() -> None:
    current = _snapshot().model_copy(
        update={
            "mixed_media_timing": MixedMediaTimingProfile(
                image_hold="very_fast", video_hold="longer", boundary_style="cut"
            )
        }
    )
    revision = EditGuideRevision(
        direction="guided_story",
        goal=current.goal,
        pace=current.pace,
        duration_s=current.duration_s,
        title=current.title,
        story_beats=[
            EditGuideRevisionBeat(
                beat_id="coast",
                topic="Coast",
                thought="Coast",
                layout="fullscreen",
                duration_s=4,
                media_refs=["media_1"],
            )
        ],
    )

    revised = plan_items._snapshot_from_edit_guide_revision(current, revision)

    assert revised.mixed_media_timing == current.mixed_media_timing


def test_review_timing_recognizes_affirmative_request_and_explicit_removal() -> None:
    profile = MixedMediaTimingProfile(
        image_hold="very_fast", video_hold="longer", boundary_style="cut"
    )

    enabled = plan_items._review_mixed_media_timing(
        None,
        "Photos should have a very fast transition, videos can be a bit longer",
    )
    assert enabled == profile

    assert (
        plan_items._review_mixed_media_timing(
            profile,
            "Don't make the photos very fast; let the videos breathe.",
        )
        is None
    )
    assert plan_items._review_mixed_media_timing(profile, "Remove the mixed-media timing") is None


def test_review_timing_preserves_profile_for_unrelated_feedback() -> None:
    profile = MixedMediaTimingProfile(
        image_hold="very_fast", video_hold="longer", boundary_style="cut"
    )

    assert plan_items._review_mixed_media_timing(profile, "Put the coast chapter first") == profile
    assert plan_items._review_mixed_media_timing(profile, "Remove the first two cuts") == profile


@pytest.mark.parametrize(
    "message",
    [
        "Don't clear the mixed-media timing.",
        "Do not disable the timing.",
        "Never drop the media pacing.",
        "Don't return the timing to default.",
    ],
)
def test_review_timing_preserves_profile_for_negated_reset(message: str) -> None:
    profile = MixedMediaTimingProfile(
        image_hold="very_fast", video_hold="longer", boundary_style="cut"
    )

    assert plan_items._review_mixed_media_timing(profile, message) == profile


@pytest.mark.parametrize(
    "message",
    [
        "Keep photos snappy, but do not hold videos longer.",
        "Make the photos not very fast; videos can linger.",
        "Let the photos linger and make the videos shorter.",
    ],
)
def test_review_timing_clears_profile_for_natural_retractions(message: str) -> None:
    profile = MixedMediaTimingProfile(
        image_hold="very_fast", video_hold="longer", boundary_style="cut"
    )

    assert plan_items._review_mixed_media_timing(profile, message) is None


def test_review_timing_does_not_clear_for_negated_inverse_request() -> None:
    profile = MixedMediaTimingProfile(
        image_hold="very_fast", video_hold="longer", boundary_style="cut"
    )

    assert (
        plan_items._review_mixed_media_timing(
            profile,
            "Don't make the photos slower and don't shorten the videos.",
        )
        == profile
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("message", "current_profile", "expected_profile"),
    [
        (
            "Photos should have a very fast transition, videos can be a bit longer",
            None,
            MixedMediaTimingProfile(
                image_hold="very_fast", video_hold="longer", boundary_style="cut"
            ),
        ),
        (
            "Remove the mixed-media timing",
            MixedMediaTimingProfile(
                image_hold="very_fast", video_hold="longer", boundary_style="cut"
            ),
            None,
        ),
    ],
)
async def test_conversation_same_direction_replans_explicit_timing_change(
    monkeypatch, message, current_profile, expected_profile
) -> None:
    item = _draft_item()
    item.idea = "Corfu trip"
    item.theme = "Corfu"
    if current_profile is not None:
        item.edit_proposal["brief"]["mixed_media_timing"] = current_profile.model_dump(mode="json")
        item.edit_proposal["draft"]["mixed_media_timing"] = current_profile.model_dump(mode="json")
    item.edit_proposal["brief"]["output_orientation"] = "portrait"
    item.edit_proposal["draft"]["output_orientation"] = "portrait"
    item.edit_proposal["draft"]["output_orientation_reason"] = (
        "The creator selected this output format."
    )
    monkeypatch.setattr(plan_items.settings, "guided_edit_capability_enabled", True)
    monkeypatch.setattr(plan_items.settings, "guided_edit_conversation_enabled", True)
    monkeypatch.setattr(plan_items, "_load_owned_item", AsyncMock(return_value=item))
    monkeypatch.setattr(plan_items, "plan_item_response", lambda loaded: loaded)
    monkeypatch.setattr("app.agents._model_client.default_client", lambda: None)
    monkeypatch.setattr(
        "app.agents.edit_guide.EditGuideAgent.run",
        lambda _self, _input: EditGuideOutput(
            reply="I updated the timing.",
            suggestions=[],
            brief=ProposalBrief(direction="guided_story", pace="balanced", duration_s=24),
            ready_to_plan=True,
            revision=EditGuideRevision(
                direction="guided_story",
                goal="Share what stood out",
                pace="balanced",
                duration_s=24,
                title="Corfu story",
                story_beats=[
                    EditGuideRevisionBeat(
                        beat_id="coast",
                        topic="Coast",
                        thought="The coast sets the pace.",
                        layout="fullscreen",
                        duration_s=4,
                        media_refs=["media_1"],
                    )
                ],
            ),
        ),
    )
    seen: list[object] = []

    def replan(current, **kwargs):  # noqa: ANN001, ANN202
        seen.append(kwargs["mixed_media_timing"])
        return current.model_copy(
            update={
                "direction": kwargs["direction"],
                "pace": kwargs["pace"],
                "mixed_media_timing": kwargs["mixed_media_timing"],
            }
        )

    monkeypatch.setattr("app.services.edit_direction_planner.plan_direction_snapshot", replan)
    monkeypatch.setattr("app.pipeline.guided_story.validate_proposal_timing", lambda _value: None)
    db = AsyncMock()
    db.execute.return_value = SimpleNamespace(scalars=lambda: [])

    await plan_items.edit_proposal_conversation_turn(
        _request(),
        str(item.id),
        plan_items.EditGuideTurnBody(expected_proposal_version=2, message=message),
        SimpleNamespace(id=uuid.uuid4()),
        db,
    )

    persisted = parse_edit_proposal(item.edit_proposal)
    assert seen == [expected_profile]
    assert persisted is not None and persisted.draft is not None
    assert persisted.draft.mixed_media_timing == expected_profile
    assert persisted.brief.mixed_media_timing == expected_profile
    assert persisted.draft.output_orientation == "portrait"
    assert persisted.brief.output_orientation == "portrait"


def test_mixed_media_proposals_use_the_deploy_fenced_worker_queue() -> None:
    proposal = SimpleNamespace(
        brief=ProposalBrief(
            mixed_media_timing=MixedMediaTimingProfile(
                image_hold="very_fast", video_hold="longer", boundary_style="cut"
            )
        )
    )

    assert plan_items._proposal_analysis_queue(proposal) == "creator-guided-jobs"


def test_cadence_proposals_use_the_deploy_fenced_worker_queue() -> None:
    proposal = SimpleNamespace(
        brief=ProposalBrief(
            montage_cadence=MontageCadenceConstraint(
                source_media_ids=["clip-1", "clip-2"], cut_duration_s=1
            )
        )
    )

    assert plan_items._proposal_analysis_queue(proposal) == "creator-guided-jobs"


def test_snapshot_revision_recalculates_auto_orientation_from_reassigned_media() -> None:
    current = _snapshot()
    current.media[0].aspect = 1.7778
    current.output_orientation = "landscape"
    current.output_orientation_reason = "Auto-selected landscape from the previous story."
    portrait = MediaRef(
        lane="clip",
        media_id="clip-2",
        gcs_path="users/u/plan/i/portrait.mp4",
        generation="43",
        kind="video",
        duration_s=30,
        aspect=0.5625,
    )
    current.media.append(portrait)
    revision = EditGuideRevision(
        direction="guided_story",
        goal="Make the portrait clip the story",
        pace="balanced",
        duration_s=24,
        title="Portrait story",
        story_beats=[
            EditGuideRevisionBeat(
                beat_id="coast",
                topic="Portrait",
                thought="Portrait",
                layout="fullscreen",
                duration_s=12,
                media_refs=["media_2"],
            )
        ],
    )

    revised = plan_items._snapshot_from_edit_guide_revision(current, revision)

    assert revised.output_orientation == "portrait"
    assert "12.0s portrait" in revised.output_orientation_reason


def test_snapshot_revision_preserves_creator_pinned_portrait_orientation() -> None:
    current = _snapshot()
    current.media[0].aspect = 1.7778
    current.output_orientation = "portrait"
    current.output_orientation_reason = "The creator selected this output format."
    revision = EditGuideRevision(
        direction="guided_story",
        goal="Keep the landscape source in a vertical edit",
        pace="balanced",
        duration_s=24,
        title="Vertical story",
        story_beats=[
            EditGuideRevisionBeat(
                beat_id="coast",
                topic="Landscape source",
                thought="Keep it vertical",
                layout="fullscreen",
                duration_s=12,
                media_refs=["media_1"],
            )
        ],
    )

    revised = plan_items._snapshot_from_edit_guide_revision(
        current,
        revision,
        output_orientation="portrait",
    )

    assert revised.output_orientation == "portrait"
    assert revised.output_orientation_reason == "The creator selected this output format."


def _patch_route_dependencies(monkeypatch, item, *, media_current: bool) -> AsyncMock:
    monkeypatch.setattr(plan_items.settings, "guided_edit_capability_enabled", True)
    monkeypatch.setattr(plan_items.settings, "guided_edit_conversation_enabled", True)
    monkeypatch.setattr(plan_items, "_load_owned_item", AsyncMock(return_value=item))
    monkeypatch.setattr(
        plan_items,
        "_proposal_media_is_current",
        AsyncMock(return_value=media_current),
    )
    monkeypatch.setattr(plan_items, "plan_item_response", lambda loaded: loaded)
    return AsyncMock()


def _request() -> Request:
    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/plan-items/item/edit-proposal/draft",
            "headers": [],
            "client": (f"test-{uuid.uuid4().hex}", 1234),
        }
    )


def test_conversation_turn_rejects_whitespace_only_message() -> None:
    with pytest.raises(ValueError, match="message cannot be blank"):
        plan_items.EditGuideTurnBody(message="   \n\t")


def test_conversation_rate_key_uses_proxy_authenticated_user() -> None:
    first = Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/",
            "headers": [(b"x-user-id", b"creator-1"), (b"x-forwarded-for", b"1.1.1.1")],
            "client": ("10.0.0.1", 1234),
        }
    )
    rotated_ip = Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/",
            "headers": [(b"x-user-id", b"creator-1"), (b"x-forwarded-for", b"9.9.9.9")],
            "client": ("10.0.0.2", 1234),
        }
    )
    another_user = Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/",
            "headers": [(b"x-user-id", b"creator-2")],
            "client": ("10.0.0.1", 1234),
        }
    )
    assert plan_items._edit_conversation_rate_key(first) == "user:creator-1"
    assert plan_items._edit_conversation_rate_key(rotated_ip) == "user:creator-1"
    assert plan_items._edit_conversation_rate_key(another_user) == "user:creator-2"


def test_proposal_response_hides_attempt_token_and_exposes_safe_resume_state() -> None:
    from app.schemas.edit_proposal import EditConversationAttempt, EditProposalResponse

    proposal = EditProposal(
        proposal_version=1,
        generation_attempt_id="brief-1",
        status="briefing",
        conversation_attempt=EditConversationAttempt(
            token="server-secret-token",
            expected_proposal_version=0,
            reserved_proposal_version=1,
            started_at=datetime.now(UTC),
            placeholder=True,
        ),
    )
    item = SimpleNamespace(
        id=uuid.uuid4(), clip_assignments=[], edit_proposal=proposal.model_dump(mode="json")
    )

    payload = plan_items._edit_proposal_response(item)

    assert payload is not None
    assert payload["conversation_attempt"] is None
    assert payload["conversation_in_progress"] is True
    assert payload["conversation_retry_required"] is False
    EditProposalResponse.model_validate(payload)


@pytest.mark.asyncio
async def test_conversation_endpoint_stays_dark_until_reader_rollout_finishes(monkeypatch) -> None:
    monkeypatch.setattr(plan_items.settings, "guided_edit_capability_enabled", True)
    monkeypatch.setattr(plan_items.settings, "guided_edit_conversation_enabled", False)
    load = AsyncMock()
    monkeypatch.setattr(plan_items, "_load_owned_item", load)

    with pytest.raises(HTTPException) as exc:
        await plan_items.edit_proposal_conversation_turn(
            _request(),
            str(uuid.uuid4()),
            plan_items.EditGuideTurnBody(message="Make it reflective."),
            SimpleNamespace(id=uuid.uuid4()),
            AsyncMock(),
        )

    assert exc.value.status_code == 404
    load.assert_not_awaited()


@pytest.mark.asyncio
async def test_active_conversation_reservation_blocks_duplicate_model_call(monkeypatch) -> None:
    from app.services.edit_proposals import reserve_edit_conversation_attempt

    item = _draft_item()
    item.idea = "Corfu trip"
    item.theme = "Corfu"
    reserve_edit_conversation_attempt(item, expected_version=2)
    run = AsyncMock()
    monkeypatch.setattr(plan_items.settings, "guided_edit_capability_enabled", True)
    monkeypatch.setattr(plan_items.settings, "guided_edit_conversation_enabled", True)
    monkeypatch.setattr(plan_items, "_load_owned_item", AsyncMock(return_value=item))
    monkeypatch.setattr("app.agents.edit_guide.EditGuideAgent.run", run)
    db = AsyncMock()

    with pytest.raises(HTTPException) as exc:
        await plan_items.edit_proposal_conversation_turn(
            _request(),
            str(item.id),
            plan_items.EditGuideTurnBody(
                expected_proposal_version=2,
                message="Put food first.",
            ),
            SimpleNamespace(id=uuid.uuid4()),
            db,
        )

    assert exc.value.status_code == 409
    assert "already thinking" in exc.value.detail["message"]
    run.assert_not_awaited()
    db.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_conversation_rejects_empty_media_before_reserving_attempt(monkeypatch) -> None:
    """Mirrors draft_item_edit_proposal's media gate — no model call for advice

    the item page can't act on when nothing has been uploaded yet.
    """

    item = SimpleNamespace(id=uuid.uuid4(), clip_assignments=[], edit_proposal=None)
    run = AsyncMock()
    monkeypatch.setattr(plan_items.settings, "guided_edit_capability_enabled", True)
    monkeypatch.setattr(plan_items.settings, "guided_edit_conversation_enabled", True)
    monkeypatch.setattr(plan_items, "_load_owned_item", AsyncMock(return_value=item))
    monkeypatch.setattr("app.agents.edit_guide.EditGuideAgent.run", run)
    db = AsyncMock()
    db.execute.return_value = SimpleNamespace(scalar_one=lambda: 0)

    with pytest.raises(HTTPException) as exc:
        await plan_items.edit_proposal_conversation_turn(
            _request(),
            str(item.id),
            plan_items.EditGuideTurnBody(
                expected_proposal_version=0,
                message="Put food first.",
            ),
            SimpleNamespace(id=uuid.uuid4()),
            db,
        )

    assert exc.value.status_code == 409
    assert exc.value.detail["code"] == "media_required"
    assert "Add a photo or video" in exc.value.detail["message"]
    run.assert_not_awaited()
    db.commit.assert_not_awaited()
    # No attempt reservation was persisted for this rejected turn.
    assert item.edit_proposal is None


@pytest.mark.asyncio
async def test_draft_rejects_live_conversation_attempt_without_dispatch(monkeypatch) -> None:
    from app.services.edit_proposals import reserve_edit_conversation_attempt

    item = SimpleNamespace(id=uuid.uuid4(), clip_assignments=[], edit_proposal=None)
    reserve_edit_conversation_attempt(item, expected_version=0)
    plan = SimpleNamespace(ownership_epoch=1)
    monkeypatch.setattr(plan_items.settings, "guided_edit_capability_enabled", True)
    monkeypatch.setattr(
        plan_items,
        "_load_owned_item_context",
        AsyncMock(return_value=(item, plan, SimpleNamespace())),
    )
    db = AsyncMock()

    with pytest.raises(HTTPException) as exc:
        await plan_items.draft_item_edit_proposal(
            _request(),
            str(item.id),
            plan_items.DraftEditProposalBody(),
            SimpleNamespace(id=uuid.uuid4()),
            db,
        )

    assert exc.value.status_code == 409
    assert exc.value.detail["code"] == "edit_guide_in_progress"
    db.execute.assert_not_awaited()
    db.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_draft_releases_expired_attempt_and_requires_chat_retry(monkeypatch) -> None:
    from app.services.edit_proposals import reserve_edit_conversation_attempt

    item = SimpleNamespace(id=uuid.uuid4(), clip_assignments=[], edit_proposal=None)
    started = datetime.now(UTC) - timedelta(seconds=91)
    reserve_edit_conversation_attempt(item, expected_version=0, now=started)
    plan = SimpleNamespace(ownership_epoch=1)
    monkeypatch.setattr(plan_items.settings, "guided_edit_capability_enabled", True)
    monkeypatch.setattr(
        plan_items,
        "_load_owned_item_context",
        AsyncMock(return_value=(item, plan, SimpleNamespace())),
    )
    db = AsyncMock()

    with pytest.raises(HTTPException) as exc:
        await plan_items.draft_item_edit_proposal(
            _request(),
            str(item.id),
            plan_items.DraftEditProposalBody(),
            SimpleNamespace(id=uuid.uuid4()),
            db,
        )

    assert exc.value.status_code == 409
    assert exc.value.detail["code"] == "edit_guide_retry_required"
    assert item.edit_proposal is None
    db.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_conversation_turn_persists_brief_before_analysis(monkeypatch) -> None:
    class ExpiringUser:
        def __init__(self) -> None:
            self._id = uuid.uuid4()
            self.expired = False

        @property
        def id(self):  # noqa: ANN201
            if self.expired:
                raise RuntimeError("user ORM attributes expired after rollback")
            return self._id

    user = ExpiringUser()
    item = SimpleNamespace(
        id=uuid.uuid4(),
        idea="Corfu trip",
        theme="Corfu",
        clip_assignments=[
            {
                "gcs_path": "users/u/plan/i/corfu.mp4",
                "kind": "video",
                "user_note": "arrival by boat",
                "analysis": {"subject": "sailboat"},
            }
        ],
        edit_proposal=None,
    )
    monkeypatch.setattr(plan_items.settings, "guided_edit_capability_enabled", True)
    monkeypatch.setattr(plan_items.settings, "guided_edit_conversation_enabled", True)
    monkeypatch.setattr(plan_items, "_load_owned_item", AsyncMock(return_value=item))
    monkeypatch.setattr(plan_items, "plan_item_response", lambda loaded: loaded)
    monkeypatch.setattr("app.agents._model_client.default_client", lambda: None)
    monkeypatch.setattr(
        "app.agents.edit_guide.EditGuideAgent.run",
        lambda _self, _input: EditGuideOutput(
            reply="I’ll make a quick, music-led trip highlight.",
            suggestions=["Focus on food", "Keep all topics"],
            brief=ProposalBrief(
                direction="fast_montage",
                goal="Show the food, town, and water",
                pace="fast",
                duration_s=20,
            ),
            ready_to_plan=True,
        ),
    )
    db = AsyncMock()
    db.execute.return_value = SimpleNamespace(scalars=lambda: [])
    db.commit.side_effect = lambda: setattr(user, "expired", True)

    response = await plan_items.edit_proposal_conversation_turn(
        _request(),
        str(item.id),
        plan_items.EditGuideTurnBody(
            expected_proposal_version=0,
            message="Make it quick and fun, but show food, town, and water.",
        ),
        user,
        db,
    )

    assert response is item
    persisted = parse_edit_proposal(item.edit_proposal)
    assert persisted is not None and persisted.status == "briefing"
    assert persisted.brief.direction == "fast_montage"
    assert persisted.brief_ready is True
    assert persisted.conversation[-1].suggestions == ["Focus on food", "Keep all topics"]
    db.rollback.assert_not_awaited()
    assert db.commit.await_count == 2


@pytest.mark.asyncio
async def test_conversation_stops_after_brief_is_ready(monkeypatch) -> None:
    proposal = EditProposal(
        proposal_version=4,
        generation_attempt_id="attempt-ready",
        status="briefing",
        brief=ProposalBrief(direction="fast_montage", pace="fast", duration_s=15),
        brief_ready=True,
    )
    item = SimpleNamespace(
        id=uuid.uuid4(),
        clip_assignments=[{"gcs_path": "users/u/corfu.mp4"}],
        edit_proposal=proposal.model_dump(mode="json"),
    )
    monkeypatch.setattr(plan_items.settings, "guided_edit_capability_enabled", True)
    monkeypatch.setattr(plan_items.settings, "guided_edit_conversation_enabled", True)
    monkeypatch.setattr(plan_items, "_load_owned_item", AsyncMock(return_value=item))

    with pytest.raises(HTTPException) as exc:
        await plan_items.edit_proposal_conversation_turn(
            _request(),
            str(item.id),
            plan_items.EditGuideTurnBody(
                expected_proposal_version=4,
                message="Ask me one more question.",
            ),
            SimpleNamespace(id=uuid.uuid4()),
            AsyncMock(),
        )

    assert exc.value.status_code == 409
    assert exc.value.detail["code"] == "brief_already_ready"


@pytest.mark.asyncio
async def test_conversation_revision_preserves_media_and_creator_thought(monkeypatch) -> None:
    item = _draft_item()
    item.idea = "Corfu trip"
    item.theme = "Corfu"
    item.edit_proposal["draft"]["story_beats"][0]["thought_source"] = "user"
    item.edit_proposal["draft"]["story_beats"][0]["thought"] = "I loved the quiet water."
    monkeypatch.setattr(plan_items.settings, "guided_edit_capability_enabled", True)
    monkeypatch.setattr(plan_items.settings, "guided_edit_conversation_enabled", True)
    monkeypatch.setattr(plan_items, "_load_owned_item", AsyncMock(return_value=item))
    monkeypatch.setattr(plan_items, "plan_item_response", lambda loaded: loaded)
    monkeypatch.setattr("app.agents._model_client.default_client", lambda: None)
    monkeypatch.setattr(
        "app.agents.edit_guide.EditGuideAgent.run",
        lambda _self, _input: EditGuideOutput(
            reply="I slowed the story and made the coast chapter more reflective.",
            suggestions=[],
            brief=ProposalBrief(
                direction="guided_story",
                goal="Share what stood out",
                pace="relaxed",
                duration_s=30,
            ),
            ready_to_plan=True,
            revision=EditGuideRevision(
                direction="guided_story",
                goal="Share what stood out",
                pace="relaxed",
                duration_s=30,
                title="A slower day in Corfu",
                story_beats=[
                    EditGuideRevisionBeat(
                        beat_id="coast",
                        topic="Quiet coast",
                        thought="The still water creates a reflective pause.",
                        layout="supporting_card",
                        duration_s=6,
                        media_refs=["media_1"],
                    )
                ],
            ),
        ),
    )
    db = AsyncMock()
    db.execute.return_value = SimpleNamespace(scalars=lambda: [])

    await plan_items.edit_proposal_conversation_turn(
        _request(),
        str(item.id),
        plan_items.EditGuideTurnBody(
            expected_proposal_version=2,
            message="Make it slower and more reflective.",
        ),
        SimpleNamespace(id=uuid.uuid4()),
        db,
    )

    persisted = parse_edit_proposal(item.edit_proposal)
    assert persisted is not None and persisted.status == "draft"
    assert persisted.proposal_version == 3
    assert persisted.draft is not None
    assert persisted.draft.media[0].media_id == "clip-1"
    assert persisted.draft.story_beats[0].media_ids == ["clip-1"]
    assert persisted.draft.story_beats[0].thought == "I loved the quiet water."
    assert persisted.draft.story_beats[0].thought_source == "user"
    assert persisted.draft.pace == "relaxed"
    assert [turn.phase for turn in persisted.conversation[-2:]] == ["review", "review"]


@pytest.mark.asyncio
async def test_conversation_direction_change_preserves_mixed_media_timing(monkeypatch) -> None:
    item = _draft_item()
    item.idea = "Corfu trip"
    item.theme = "Corfu"
    profile = MixedMediaTimingProfile(
        image_hold="very_fast",
        video_hold="longer",
        boundary_style="cut",
    )
    item.edit_proposal["brief"]["creator_request"] = (
        "Photos should have a very fast transition, videos can be a bit longer"
    )
    item.edit_proposal["brief"]["mixed_media_timing"] = profile.model_dump(mode="json")
    item.edit_proposal["brief"]["output_orientation"] = "portrait"
    item.edit_proposal["draft"]["mixed_media_timing"] = profile.model_dump(mode="json")
    item.edit_proposal["draft"]["output_orientation"] = "portrait"
    item.edit_proposal["draft"]["output_orientation_reason"] = (
        "The creator selected this output format."
    )
    monkeypatch.setattr(plan_items.settings, "guided_edit_capability_enabled", True)
    monkeypatch.setattr(plan_items.settings, "guided_edit_conversation_enabled", True)
    monkeypatch.setattr(plan_items, "_load_owned_item", AsyncMock(return_value=item))
    monkeypatch.setattr(plan_items, "plan_item_response", lambda loaded: loaded)
    monkeypatch.setattr("app.agents._model_client.default_client", lambda: None)
    monkeypatch.setattr(
        "app.agents.edit_guide.EditGuideAgent.run",
        lambda _self, _input: EditGuideOutput(
            reply="I changed the structure but kept your photo and video rhythm.",
            suggestions=[],
            brief=ProposalBrief(direction="fast_montage", pace="fast", duration_s=24),
            ready_to_plan=True,
            revision=EditGuideRevision(
                direction="fast_montage",
                goal="Share what stood out",
                pace="fast",
                duration_s=24,
                title="Corfu highlights",
                story_beats=[
                    EditGuideRevisionBeat(
                        beat_id="coast",
                        topic="Coast",
                        thought="The coast sets the pace.",
                        layout="fullscreen",
                        duration_s=12,
                        media_refs=["media_1"],
                    )
                ],
            ),
        ),
    )
    seen: dict = {}

    def replan(current, **kwargs):  # noqa: ANN001, ANN202
        seen.update(kwargs)
        return current.model_copy(
            update={
                "direction": "fast_montage",
                "pace": "fast",
                "mixed_media_timing": kwargs["mixed_media_timing"],
            }
        )

    monkeypatch.setattr("app.services.edit_direction_planner.plan_direction_snapshot", replan)
    monkeypatch.setattr("app.pipeline.guided_story.validate_proposal_timing", lambda _value: None)
    db = AsyncMock()
    db.execute.return_value = SimpleNamespace(scalars=lambda: [])

    await plan_items.edit_proposal_conversation_turn(
        _request(),
        str(item.id),
        plan_items.EditGuideTurnBody(
            expected_proposal_version=2,
            message="Make it a fast montage but keep the timing I requested.",
        ),
        SimpleNamespace(id=uuid.uuid4()),
        db,
    )

    persisted = parse_edit_proposal(item.edit_proposal)
    assert seen["mixed_media_timing"] == profile
    assert persisted is not None and persisted.draft is not None
    assert persisted.draft.mixed_media_timing == profile
    assert persisted.brief.mixed_media_timing == profile
    assert persisted.brief.creator_request.startswith("Photos should")
    assert persisted.draft.output_orientation == "portrait"
    assert persisted.brief.output_orientation == "portrait"


@pytest.mark.asyncio
async def test_revision_validation_failure_releases_conversation_attempt(monkeypatch) -> None:
    item = _draft_item()
    item.idea = "Corfu trip"
    item.theme = "Corfu"
    monkeypatch.setattr(plan_items.settings, "guided_edit_capability_enabled", True)
    monkeypatch.setattr(plan_items.settings, "guided_edit_conversation_enabled", True)
    monkeypatch.setattr(plan_items, "_load_owned_item", AsyncMock(return_value=item))
    monkeypatch.setattr("app.agents._model_client.default_client", lambda: None)
    monkeypatch.setattr(
        "app.agents.edit_guide.EditGuideAgent.run",
        lambda _self, _input: EditGuideOutput(
            reply="I moved the coast first.",
            suggestions=[],
            brief=ProposalBrief(goal="Share what stood out"),
            ready_to_plan=True,
            revision=EditGuideRevision(
                direction="guided_story",
                goal="Share what stood out",
                pace="balanced",
                duration_s=24,
                title="Corfu",
                story_beats=[
                    EditGuideRevisionBeat(
                        beat_id="coast",
                        topic="Coast",
                        thought="The water sets the pace.",
                        layout="fullscreen",
                        duration_s=4,
                        media_refs=["media_1"],
                    )
                ],
            ),
        ),
    )
    monkeypatch.setattr(
        "app.pipeline.guided_story.validate_proposal_timing",
        lambda _snapshot: (_ for _ in ()).throw(AttributeError("malformed analysis")),
    )
    db = AsyncMock()
    db.execute.return_value = SimpleNamespace(scalars=lambda: [])

    with pytest.raises(HTTPException) as exc:
        await plan_items.edit_proposal_conversation_turn(
            _request(),
            str(item.id),
            plan_items.EditGuideTurnBody(
                expected_proposal_version=2,
                message="Put the coast first.",
            ),
            SimpleNamespace(id=uuid.uuid4()),
            db,
        )

    assert exc.value.status_code == 503
    assert exc.value.detail["code"] == "edit_guide_failed"
    persisted = parse_edit_proposal(item.edit_proposal)
    assert persisted is not None and persisted.conversation_attempt is None
    assert db.commit.await_count == 2


@pytest.mark.asyncio
async def test_review_mixed_media_capacity_failure_is_actionable(monkeypatch) -> None:
    item = _draft_item()
    item.idea = "Corfu trip"
    item.theme = "Corfu"
    profile = MixedMediaTimingProfile(
        image_hold="very_fast",
        video_hold="longer",
        boundary_style="cut",
    )
    item.edit_proposal["brief"]["mixed_media_timing"] = profile.model_dump(mode="json")
    item.edit_proposal["draft"]["mixed_media_timing"] = profile.model_dump(mode="json")
    monkeypatch.setattr(plan_items.settings, "guided_edit_capability_enabled", True)
    monkeypatch.setattr(plan_items.settings, "guided_edit_conversation_enabled", True)
    monkeypatch.setattr(plan_items, "_load_owned_item", AsyncMock(return_value=item))
    monkeypatch.setattr("app.agents._model_client.default_client", lambda: None)
    monkeypatch.setattr(
        "app.agents.edit_guide.EditGuideAgent.run",
        lambda _self, _input: EditGuideOutput(
            reply="I changed this to a fast montage.",
            suggestions=[],
            brief=ProposalBrief(direction="fast_montage", pace="fast", duration_s=24),
            ready_to_plan=True,
            revision=EditGuideRevision(
                direction="fast_montage",
                goal="Share what stood out",
                pace="fast",
                duration_s=24,
                title="Corfu highlights",
                story_beats=[
                    EditGuideRevisionBeat(
                        beat_id="coast",
                        topic="Coast",
                        thought="The coast sets the pace.",
                        layout="fullscreen",
                        duration_s=12,
                        media_refs=["media_1"],
                    )
                ],
            ),
        ),
    )
    monkeypatch.setattr(
        "app.services.edit_direction_planner.plan_direction_snapshot",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            ValueError(
                "mixed-media fast montage has less than the minimum 3s of usable source capacity"
            )
        ),
    )
    db = AsyncMock()
    db.execute.return_value = SimpleNamespace(scalars=lambda: [])

    with pytest.raises(HTTPException) as exc:
        await plan_items.edit_proposal_conversation_turn(
            _request(),
            str(item.id),
            plan_items.EditGuideTurnBody(
                expected_proposal_version=2,
                message="Make it a fast montage but keep the timing I requested.",
            ),
            SimpleNamespace(id=uuid.uuid4()),
            db,
        )

    assert exc.value.status_code == 503
    assert exc.value.detail["code"] == "guided_edit_infeasible"
    assert "Add another photo or video" in exc.value.detail["message"]
    persisted = parse_edit_proposal(item.edit_proposal)
    assert persisted is not None and persisted.conversation_attempt is None
    assert db.commit.await_count == 2


@pytest.mark.asyncio
async def test_review_clarification_preserves_current_brief(monkeypatch) -> None:
    item = _draft_item()
    item.idea = "Corfu trip"
    item.theme = "Corfu"
    expected_brief = ProposalBrief(
        direction="guided_story",
        goal="Share what stood out",
        pace="balanced",
        duration_s=24,
    )
    monkeypatch.setattr(plan_items.settings, "guided_edit_capability_enabled", True)
    monkeypatch.setattr(plan_items.settings, "guided_edit_conversation_enabled", True)
    monkeypatch.setattr(plan_items, "_load_owned_item", AsyncMock(return_value=item))
    monkeypatch.setattr(plan_items, "plan_item_response", lambda loaded: loaded)
    monkeypatch.setattr("app.agents._model_client.default_client", lambda: None)
    seen_briefs = []
    seen_beats = []
    seen_media_refs = []

    def run(_self, agent_input):  # noqa: ANN001, ANN202
        seen_briefs.append(agent_input.brief)
        seen_beats.extend(agent_input.beats)
        seen_media_refs.extend(row.media_ref for row in agent_input.media)
        return EditGuideOutput(
            reply="Should the food chapter come first?",
            suggestions=["Yes", "Keep the coast first"],
            brief=ProposalBrief(goal="A model-authored conflicting goal", pace="fast"),
            ready_to_plan=True,
            revision=None,
        )

    monkeypatch.setattr("app.agents.edit_guide.EditGuideAgent.run", run)
    db = AsyncMock()
    db.execute.return_value = SimpleNamespace(scalars=lambda: [])

    await plan_items.edit_proposal_conversation_turn(
        _request(),
        str(item.id),
        plan_items.EditGuideTurnBody(
            expected_proposal_version=2,
            message="Could food come first?",
        ),
        SimpleNamespace(id=uuid.uuid4()),
        db,
    )

    persisted = parse_edit_proposal(item.edit_proposal)
    assert persisted is not None and persisted.status == "draft"
    assert persisted.brief == expected_brief
    assert persisted.draft == _snapshot()
    assert seen_briefs == [expected_brief]
    assert [beat.media_refs for beat in seen_beats] == [["media_1"]]
    assert seen_media_refs == ["media_1"]


@pytest.mark.asyncio
async def test_conversation_rejects_stale_browser_version_before_agent_call(monkeypatch) -> None:
    item = _draft_item()
    item.idea = "Corfu trip"
    item.theme = "Corfu"
    run = AsyncMock()
    monkeypatch.setattr(plan_items.settings, "guided_edit_capability_enabled", True)
    monkeypatch.setattr(plan_items.settings, "guided_edit_conversation_enabled", True)
    monkeypatch.setattr(plan_items, "_load_owned_item", AsyncMock(return_value=item))
    monkeypatch.setattr("app.agents.edit_guide.EditGuideAgent.run", run)
    db = AsyncMock()

    with pytest.raises(HTTPException) as exc:
        await plan_items.edit_proposal_conversation_turn(
            _request(),
            str(item.id),
            plan_items.EditGuideTurnBody(
                expected_proposal_version=1,
                message="Make it faster.",
            ),
            SimpleNamespace(id=uuid.uuid4()),
            db,
        )

    assert exc.value.status_code == 409
    assert exc.value.detail["code"] == "proposal_conflict"
    run.assert_not_awaited()
    db.rollback.assert_not_awaited()


@pytest.mark.asyncio
async def test_conversation_rejects_version_change_after_agent_returns(monkeypatch) -> None:
    initial = _draft_item()
    initial.idea = "Corfu trip"
    initial.theme = "Corfu"
    concurrent = _draft_item()
    concurrent.edit_proposal = {
        **concurrent.edit_proposal,
        "proposal_version": 3,
        "draft": {**concurrent.edit_proposal["draft"], "title": "Changed in another tab"},
    }
    load = AsyncMock(side_effect=[initial, concurrent])
    monkeypatch.setattr(plan_items.settings, "guided_edit_capability_enabled", True)
    monkeypatch.setattr(plan_items.settings, "guided_edit_conversation_enabled", True)
    monkeypatch.setattr(plan_items, "_load_owned_item", load)
    monkeypatch.setattr("app.agents._model_client.default_client", lambda: None)
    monkeypatch.setattr(
        "app.agents.edit_guide.EditGuideAgent.run",
        lambda _self, _input: EditGuideOutput(
            reply="I’ll make it faster.",
            suggestions=[],
            brief=ProposalBrief(pace="fast"),
            ready_to_plan=True,
        ),
    )
    db = AsyncMock()
    db.execute.return_value = SimpleNamespace(scalars=lambda: [])

    with pytest.raises(HTTPException) as exc:
        await plan_items.edit_proposal_conversation_turn(
            _request(),
            str(initial.id),
            plan_items.EditGuideTurnBody(
                expected_proposal_version=2,
                message="Make it faster.",
            ),
            SimpleNamespace(id=uuid.uuid4()),
            db,
        )

    assert exc.value.status_code == 409
    assert exc.value.detail["code"] == "proposal_conflict"
    assert concurrent.edit_proposal["proposal_version"] == 3
    assert concurrent.edit_proposal["draft"]["title"] == "Changed in another tab"
    assert db.commit.await_count == 1
    db.rollback.assert_awaited_once()


@pytest.mark.asyncio
async def test_conversation_agent_failure_is_retryable_without_mutation(monkeypatch) -> None:
    item = _draft_item()
    item.idea = "Corfu trip"
    item.theme = "Corfu"
    before = dict(item.edit_proposal)
    monkeypatch.setattr(plan_items.settings, "guided_edit_capability_enabled", True)
    monkeypatch.setattr(plan_items.settings, "guided_edit_conversation_enabled", True)
    monkeypatch.setattr(plan_items, "_load_owned_item", AsyncMock(return_value=item))
    monkeypatch.setattr("app.agents._model_client.default_client", lambda: None)

    def fail(_self, _input):  # noqa: ANN001, ANN202
        raise RuntimeError("model unavailable")

    monkeypatch.setattr("app.agents.edit_guide.EditGuideAgent.run", fail)
    db = AsyncMock()
    db.execute.return_value = SimpleNamespace(scalars=lambda: [])

    with pytest.raises(HTTPException) as exc:
        await plan_items.edit_proposal_conversation_turn(
            _request(),
            str(item.id),
            plan_items.EditGuideTurnBody(
                expected_proposal_version=2,
                message="Put food first.",
            ),
            SimpleNamespace(id=uuid.uuid4()),
            db,
        )

    assert exc.value.status_code == 503
    assert exc.value.detail["code"] == "edit_guide_failed"
    assert item.edit_proposal == before
    db.rollback.assert_not_awaited()
    assert db.commit.await_count == 2


@pytest.mark.asyncio
async def test_conversation_with_full_history_sends_bounded_window(monkeypatch) -> None:
    item = _draft_item()
    item.idea = "Corfu trip"
    item.theme = "Corfu"
    item.edit_proposal["conversation"] = [
        {
            "role": "user" if index % 2 == 0 else "agent",
            "content": f"turn {index}",
            "suggestions": [],
        }
        for index in range(20)
    ]
    seen_turns = []

    def run(_self, agent_input):  # noqa: ANN001, ANN202
        seen_turns.extend(agent_input.turns)
        return EditGuideOutput(
            reply="I’ll keep the coast as the ending.",
            suggestions=[],
            brief=ProposalBrief(goal="End on the coast"),
            ready_to_plan=True,
        )

    monkeypatch.setattr(plan_items.settings, "guided_edit_capability_enabled", True)
    monkeypatch.setattr(plan_items.settings, "guided_edit_conversation_enabled", True)
    monkeypatch.setattr(plan_items, "_load_owned_item", AsyncMock(return_value=item))
    monkeypatch.setattr(plan_items, "plan_item_response", lambda loaded: loaded)
    monkeypatch.setattr("app.agents._model_client.default_client", lambda: None)
    monkeypatch.setattr("app.agents.edit_guide.EditGuideAgent.run", run)
    db = AsyncMock()
    db.execute.return_value = SimpleNamespace(scalars=lambda: [])

    await plan_items.edit_proposal_conversation_turn(
        _request(),
        str(item.id),
        plan_items.EditGuideTurnBody(
            expected_proposal_version=2,
            message="Keep the coast as the ending.",
        ),
        SimpleNamespace(id=uuid.uuid4()),
        db,
    )

    assert len(seen_turns) == 19
    assert seen_turns[0].content == "turn 2"
    assert seen_turns[-1].content == "Keep the coast as the ending."
    persisted = parse_edit_proposal(item.edit_proposal)
    assert persisted is not None
    assert len(persisted.conversation) == 20
    assert persisted.conversation[-1].content == "I’ll keep the coast as the ending."


@pytest.mark.asyncio
async def test_draft_requires_media_before_dispatch(monkeypatch) -> None:
    item = SimpleNamespace(id=uuid.uuid4(), clip_assignments=[], edit_proposal=None)
    plan = SimpleNamespace(ownership_epoch=4)
    monkeypatch.setattr(plan_items.settings, "guided_edit_capability_enabled", True)
    monkeypatch.setattr(
        plan_items,
        "_load_owned_item_context",
        AsyncMock(return_value=(item, plan, SimpleNamespace())),
    )
    count_result = SimpleNamespace(scalar_one=lambda: 0)
    db = AsyncMock()
    db.execute.return_value = count_result

    with pytest.raises(HTTPException) as exc:
        await plan_items.draft_item_edit_proposal(
            _request(),
            str(item.id),
            plan_items.DraftEditProposalBody(),
            SimpleNamespace(id=uuid.uuid4()),
            db,
        )

    assert exc.value.status_code == 409
    assert exc.value.detail["code"] == "proposal_required"
    db.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_explicit_draft_marks_brief_ready_across_auto_design_retries(monkeypatch) -> None:
    item = SimpleNamespace(
        id=uuid.uuid4(),
        clip_assignments=[{"gcs_path": "users/u/plan/i/corfu.mp4"}],
        edit_proposal=None,
    )
    plan = SimpleNamespace(ownership_epoch=4)
    monkeypatch.setattr(plan_items.settings, "guided_edit_capability_enabled", True)
    monkeypatch.setattr(
        plan_items,
        "_load_owned_item_context",
        AsyncMock(return_value=(item, plan, SimpleNamespace())),
    )
    monkeypatch.setattr(plan_items, "plan_item_response", lambda loaded: loaded)
    db = AsyncMock()
    db.execute.return_value = SimpleNamespace(scalar_one=lambda: 0)
    apply_calls = []
    monkeypatch.setattr(
        "app.tasks.edit_proposal_build.draft_edit_proposal.apply_async",
        lambda **kw: apply_calls.append(kw),
    )

    response = await plan_items.draft_item_edit_proposal(
        _request(),
        str(item.id),
        plan_items.DraftEditProposalBody(
            direction="fast_montage",
            goal="Show Corfu quickly",
            pace="fast",
            duration_s=12,
        ),
        SimpleNamespace(id=uuid.uuid4()),
        db,
    )

    assert response is item
    persisted = parse_edit_proposal(item.edit_proposal)
    assert persisted is not None
    assert persisted.status == "analyzing"
    assert persisted.brief_ready is True
    assert persisted.brief.direction == "fast_montage"
    assert persisted.brief.goal == "Show Corfu quickly"
    assert persisted.approval_mode is None
    assert len(apply_calls) == 1


@pytest.mark.parametrize("operation", ["conversation", "draft", "update", "approve"])
@pytest.mark.asyncio
async def test_audio_led_proposal_routes_reject_before_side_effects(monkeypatch, operation) -> None:
    """Dormant guided proposals cannot be edited while native audio is selected."""
    item = SimpleNamespace(
        id=uuid.uuid4(),
        edit_format="narrated_ready",
        audio_mode="voiceover",
        voiceover_gcs_path="voiceover-uploads/u/voice.m4a",
        clip_assignments=[],
        edit_proposal=None,
    )
    user = SimpleNamespace(id=uuid.uuid4())
    monkeypatch.setattr(plan_items.settings, "guided_edit_capability_enabled", True)
    monkeypatch.setattr(plan_items.settings, "guided_edit_conversation_enabled", True)
    monkeypatch.setattr(plan_items, "_load_owned_item", AsyncMock(return_value=item))
    monkeypatch.setattr(
        plan_items,
        "_load_owned_item_context",
        AsyncMock(return_value=(item, SimpleNamespace(ownership_epoch=4), SimpleNamespace())),
    )
    db = AsyncMock()

    with pytest.raises(HTTPException) as exc:
        if operation == "conversation":
            await plan_items.edit_proposal_conversation_turn(
                _request(),
                str(item.id),
                plan_items.EditGuideTurnBody(expected_proposal_version=0, message="Keep it short"),
                user,
                db,
            )
        elif operation == "draft":
            await plan_items.draft_item_edit_proposal(
                _request(), str(item.id), plan_items.DraftEditProposalBody(), user, db
            )
        elif operation == "update":
            await plan_items.update_item_edit_proposal(
                str(item.id),
                plan_items.UpdateEditProposalBody(
                    expected_proposal_version=1, snapshot=_snapshot()
                ),
                user,
                db,
            )
        else:
            await plan_items.approve_item_edit_proposal(
                str(item.id),
                plan_items.ApproveEditProposalBody(expected_proposal_version=1),
                user,
                db,
            )

    assert exc.value.status_code == 409
    assert exc.value.detail == "Guided editing is unavailable for this audio-led edit."
    db.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_draft_double_click_reuses_active_attempt(monkeypatch) -> None:
    item = SimpleNamespace(
        id=uuid.uuid4(),
        clip_assignments=[{"gcs_path": "users/u/plan/i/corfu.mp4"}],
        edit_proposal=EditProposal(
            proposal_version=1,
            generation_attempt_id="attempt-1",
            status="analyzing",
        ).model_dump(mode="json"),
    )
    monkeypatch.setattr(plan_items.settings, "guided_edit_capability_enabled", True)
    monkeypatch.setattr(
        plan_items,
        "_load_owned_item_context",
        AsyncMock(return_value=(item, SimpleNamespace(ownership_epoch=4), SimpleNamespace())),
    )
    monkeypatch.setattr(plan_items, "plan_item_response", lambda loaded: loaded)
    db = AsyncMock()

    response = await plan_items.draft_item_edit_proposal(
        _request(),
        str(item.id),
        plan_items.DraftEditProposalBody(),
        SimpleNamespace(id=uuid.uuid4()),
        db,
    )

    assert response is item
    db.execute.assert_not_awaited()
    db.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_update_rejects_stale_compare_and_swap_version(monkeypatch) -> None:
    item = _draft_item()
    db = _patch_route_dependencies(monkeypatch, item, media_current=True)
    body = plan_items.UpdateEditProposalBody(
        expected_proposal_version=1,
        snapshot=_snapshot(),
    )

    with pytest.raises(HTTPException) as exc:
        await plan_items.update_item_edit_proposal(
            str(item.id), body, SimpleNamespace(id=uuid.uuid4()), db
        )

    assert exc.value.status_code == 409
    assert exc.value.detail["code"] == "proposal_conflict"
    db.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_update_discards_client_supplied_media_analysis(monkeypatch) -> None:
    item = _draft_item()
    db = _patch_route_dependencies(monkeypatch, item, media_current=True)
    snapshot = _snapshot()
    snapshot.media[0].analysis = {"invented": "client-controlled"}
    body = plan_items.UpdateEditProposalBody(
        expected_proposal_version=2,
        snapshot=snapshot,
    )

    await plan_items.update_item_edit_proposal(
        str(item.id), body, SimpleNamespace(id=uuid.uuid4()), db
    )

    persisted = parse_edit_proposal(item.edit_proposal)
    assert persisted is not None and persisted.draft is not None
    assert persisted.draft.media[0].analysis == {}


@pytest.mark.asyncio
async def test_update_rejects_cut_beyond_server_owned_source_duration(monkeypatch) -> None:
    item = _draft_item()
    db = _patch_route_dependencies(monkeypatch, item, media_current=True)
    client_media = _snapshot().media[0].model_copy(update={"duration_s": 100})
    server_media = client_media.model_copy(update={"duration_s": 30})
    current = parse_edit_proposal(item.edit_proposal)
    assert current is not None
    current.draft = EditProposalSnapshot(
        direction="fast_montage",
        goal="Move quickly through the strongest moments",
        pace="fast",
        duration_s=3,
        title="Corfu",
        media=[server_media],
        story_beats=_snapshot().story_beats,
        fast_cuts=[
            FastMontageCut(
                cut_id=f"server-cut-{index}",
                media_id=server_media.media_id,
                source_start_s=index,
                source_end_s=index + 1,
                output_duration_s=1,
                role="hook" if index == 0 else "payoff" if index == 2 else "build",
            )
            for index in range(3)
        ],
    )
    current.media_digest = canonical_media_digest(current.draft.media)
    item.edit_proposal = current.model_dump(mode="json")
    snapshot = EditProposalSnapshot(
        direction="fast_montage",
        goal="Move quickly through the strongest moments",
        pace="fast",
        duration_s=3,
        title="Corfu",
        media=[client_media],
        story_beats=_snapshot().story_beats,
        fast_cuts=[
            FastMontageCut(
                cut_id=f"cut-{index}",
                media_id=client_media.media_id,
                source_start_s=30 + index,
                source_end_s=31 + index,
                output_duration_s=1,
                role="hook" if index == 0 else "payoff" if index == 2 else "build",
            )
            for index in range(3)
        ],
    )

    with pytest.raises(HTTPException) as exc:
        await plan_items.update_item_edit_proposal(
            str(item.id),
            plan_items.UpdateEditProposalBody(
                expected_proposal_version=2,
                snapshot=snapshot,
            ),
            SimpleNamespace(id=uuid.uuid4()),
            db,
        )

    assert exc.value.status_code == 422
    assert exc.value.detail["code"] == "proposal_invalid"
    db.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_update_rejects_direct_semantic_control_changes(monkeypatch) -> None:
    item = _draft_item()
    db = _patch_route_dependencies(monkeypatch, item, media_current=True)
    snapshot = _snapshot().model_copy(
        update={"direction": "fast_montage", "pace": "fast", "duration_s": 12}
    )

    with pytest.raises(HTTPException) as exc:
        await plan_items.update_item_edit_proposal(
            str(item.id),
            plan_items.UpdateEditProposalBody(
                expected_proposal_version=2,
                snapshot=snapshot,
            ),
            SimpleNamespace(id=uuid.uuid4()),
            db,
        )

    assert exc.value.status_code == 409
    assert exc.value.detail["code"] == "proposal_replan_required"
    db.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_approve_cutless_fast_montage_returns_replan_required(monkeypatch) -> None:
    snapshot = _snapshot().model_copy(
        update={
            "direction": "fast_montage",
            "pace": "fast",
            "fast_cuts": None,
        }
    )
    proposal = EditProposal(
        proposal_version=2,
        generation_attempt_id="attempt-1",
        media_digest=canonical_media_digest(snapshot.media),
        status="draft",
        brief=ProposalBrief(direction="fast_montage", pace="fast"),
        draft=snapshot,
    )
    item = SimpleNamespace(
        id=uuid.uuid4(),
        clip_assignments=[
            {
                "media_id": snapshot.media[0].media_id,
                "gcs_path": snapshot.media[0].gcs_path,
            }
        ],
        edit_proposal=proposal.model_dump(mode="json"),
    )
    db = _patch_route_dependencies(monkeypatch, item, media_current=True)

    with pytest.raises(HTTPException) as exc:
        await plan_items.approve_item_edit_proposal(
            str(item.id),
            plan_items.ApproveEditProposalBody(expected_proposal_version=2),
            SimpleNamespace(id=uuid.uuid4()),
            db,
        )

    assert exc.value.status_code == 409
    assert exc.value.detail == {
        "code": "proposal_replan_required",
        "message": "This montage needs a new cut plan. Ask Kria to replan it.",
    }
    db.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_approve_marks_plan_stale_when_media_identity_changed(monkeypatch) -> None:
    item = _draft_item()
    db = _patch_route_dependencies(monkeypatch, item, media_current=False)
    body = plan_items.ApproveEditProposalBody(expected_proposal_version=2)

    with pytest.raises(HTTPException) as exc:
        await plan_items.approve_item_edit_proposal(
            str(item.id), body, SimpleNamespace(id=uuid.uuid4()), db
        )

    assert exc.value.status_code == 409
    assert exc.value.detail["code"] == "proposal_stale"
    persisted = parse_edit_proposal(item.edit_proposal)
    assert persisted is not None and persisted.status == "stale"
    assert persisted.proposal_version == 3
    db.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_approve_current_draft_persists_immutable_approval(monkeypatch) -> None:
    item = _draft_item()
    db = _patch_route_dependencies(monkeypatch, item, media_current=True)
    body = plan_items.ApproveEditProposalBody(expected_proposal_version=2)

    response = await plan_items.approve_item_edit_proposal(
        str(item.id), body, SimpleNamespace(id=uuid.uuid4()), db
    )

    assert response is item
    persisted = parse_edit_proposal(item.edit_proposal)
    assert persisted is not None and persisted.status == "approved"
    assert persisted.proposal_version == 3
    assert persisted.last_approved is not None
    assert persisted.last_approved.snapshot.title == "What I noticed in Corfu"
    db.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_approval_validation_rejects_replaced_pool_asset_object(monkeypatch) -> None:
    asset_id = uuid.uuid4()
    media = MediaRef(
        lane="asset",
        media_id=str(asset_id),
        gcs_path="users/u/plan/i/pool/corfu.jpg",
        generation="42",
        kind="image",
    )
    snapshot = EditProposalSnapshot(
        direction="guided_story",
        duration_s=15,
        title="Corfu",
        media=[media],
        story_beats=[
            StoryBeat(beat_id="food", topic="Food", media_ids=[str(asset_id)], duration_s=4)
        ],
    )
    item = SimpleNamespace(id=uuid.uuid4(), clip_assignments=[])
    asset = SimpleNamespace(
        id=asset_id,
        gcs_path=media.gcs_path,
        gcs_generation=media.generation,
        kind="image",
    )
    rows = SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: [asset]))
    db = AsyncMock()
    db.execute.return_value = rows
    monkeypatch.setattr(
        plan_items.storage,
        "object_metadata",
        lambda _path: SimpleNamespace(generation="replacement-generation"),
    )

    assert (
        await plan_items._proposal_media_is_current(item, snapshot, db, user_id=uuid.uuid4())
        is False
    )


# ── Auto-design status-machine guards (P2-2/P2-3, 2026-08-18 adversarial review) ──


def _auto_design_plan() -> SimpleNamespace:
    return SimpleNamespace(ownership_epoch=1)


def _auto_design_user() -> SimpleNamespace:
    return SimpleNamespace(id=uuid.uuid4())


@pytest.mark.asyncio
@pytest.mark.parametrize("existing_draft", [False, True])
async def test_raw_auto_design_never_mutates_while_creator_session_is_active(
    monkeypatch, existing_draft: bool
) -> None:
    """The reservation lock fences both fresh and existing-draft auto-design."""
    user = _auto_design_user()
    if existing_draft:
        item = _draft_item()
        item.clip_gcs_paths = [item.clip_assignments[0]["gcs_path"]]
        original_proposal = dict(item.edit_proposal)
    else:
        item = SimpleNamespace(
            id=uuid.uuid4(),
            clip_gcs_paths=["users/u/plan/i/match.mp4"],
            clip_assignments=[
                {
                    "media_id": "clip-1",
                    "gcs_path": "users/u/plan/i/match.mp4",
                }
            ],
            edit_proposal=None,
            edit_format="montage",
        )
        original_proposal = None

    active_result = SimpleNamespace(scalar_one_or_none=lambda: uuid.uuid4())
    db = AsyncMock()
    db.execute.return_value = active_result
    monkeypatch.setattr(plan_items.settings, "guided_auto_design_enabled", True)
    monkeypatch.setattr(plan_items, "_load_owned_item", AsyncMock(return_value=item))
    enqueue = MagicMock()
    monkeypatch.setattr("app.tasks.edit_proposal_build.draft_edit_proposal.apply_async", enqueue)

    with pytest.raises(HTTPException) as exc:
        await plan_items._maybe_auto_design_generate(
            str(item.id), item, _auto_design_plan(), user, db
        )

    assert exc.value.status_code == 409
    assert exc.value.detail["code"] == "creator_agent_plan_required"
    assert item.edit_proposal == original_proposal
    db.rollback.assert_awaited_once()
    enqueue.assert_not_called()


@pytest.mark.asyncio
async def test_auto_design_never_clobbers_a_live_conversation_attempt(monkeypatch) -> None:
    """P2-3: Generate racing a live Kria reply must never void the in-flight

    turn — idempotent current-state response, no new reservation.

    P1-1 regression: AsyncSession expires every ORM attribute on rollback
    (expire_on_commit=False only covers commit) — serializing the row that
    was locked BEFORE `await db.rollback()` without reloading first raises
    MissingGreenlet in prod the instant a field is touched outside an awaited
    context. `_load_owned_item` is mocked with two DISTINCT return values
    (locked snapshot, then reload) so this test fails if the code path ever
    goes back to serializing the pre-rollback object.
    """

    from app.services.edit_proposals import reserve_edit_conversation_attempt

    locked_item = _draft_item()
    locked_item.clip_gcs_paths = [locked_item.clip_assignments[0]["gcs_path"]]
    locked_item.edit_proposal["status"] = "briefing"
    reserve_edit_conversation_attempt(
        locked_item, expected_version=locked_item.edit_proposal["proposal_version"]
    )
    reloaded_item = SimpleNamespace(**vars(locked_item))  # a DIFFERENT object, post-reload

    monkeypatch.setattr(plan_items.settings, "guided_auto_design_enabled", True)
    monkeypatch.setattr(
        plan_items, "_load_owned_item", AsyncMock(side_effect=[locked_item, reloaded_item])
    )
    monkeypatch.setattr(plan_items, "plan_item_response", lambda loaded, **_kw: loaded)
    monkeypatch.setattr(plan_items, "_get_instruction_level", AsyncMock(return_value="full"))
    db = AsyncMock()

    result = await plan_items._maybe_auto_design_generate(
        str(locked_item.id), locked_item, _auto_design_plan(), _auto_design_user(), db
    )

    assert result is reloaded_item  # serialized the RELOADED row, not the pre-rollback one
    assert result is not locked_item
    db.rollback.assert_awaited()
    db.commit.assert_not_awaited()
    # The live reservation is untouched.
    assert locked_item.edit_proposal["conversation_attempt"] is not None


@pytest.mark.asyncio
async def test_auto_design_duplicate_click_while_analyzing_reloads_before_serializing(
    monkeypatch,
) -> None:
    """P1-1: the original bug's exact location — a duplicate Generate click

    while an attempt is already `analyzing`/`drafting` must serialize the
    RELOADED row (post-rollback), never the row that was locked before
    `await db.rollback()` expired it (MissingGreenlet in prod under a real
    AsyncSession). Same two-distinct-objects technique as the
    conversation_attempt test above.
    """

    locked_item = _draft_item()
    locked_item.clip_gcs_paths = [locked_item.clip_assignments[0]["gcs_path"]]
    locked_item.edit_proposal["status"] = "analyzing"
    reloaded_item = SimpleNamespace(**vars(locked_item))

    monkeypatch.setattr(plan_items.settings, "guided_auto_design_enabled", True)
    monkeypatch.setattr(
        plan_items, "_load_owned_item", AsyncMock(side_effect=[locked_item, reloaded_item])
    )
    monkeypatch.setattr(plan_items, "plan_item_response", lambda loaded, **_kw: loaded)
    monkeypatch.setattr(plan_items, "_get_instruction_level", AsyncMock(return_value="full"))
    db = AsyncMock()

    result = await plan_items._maybe_auto_design_generate(
        str(locked_item.id), locked_item, _auto_design_plan(), _auto_design_user(), db
    )

    assert result is reloaded_item
    assert result is not locked_item
    db.rollback.assert_awaited()
    db.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_auto_design_preserves_creators_brief_on_a_fresh_attempt(monkeypatch) -> None:
    """P2-2a/b: a stale/failed/briefing attempt reserves a FRESH attempt but

    must preserve the creator's already-stated direction/goal/pace/duration —
    never reset it to ProposalBrief() defaults.
    """

    stated_brief = ProposalBrief(
        direction="fast_montage", goal="Quick highlights", pace="fast", duration_s=15
    )
    proposal = EditProposal(
        proposal_version=4,
        generation_attempt_id="attempt-old",
        status="stale",
        brief=stated_brief,
    )
    item = SimpleNamespace(
        id=uuid.uuid4(),
        clip_gcs_paths=["users/u/plan/i/corfu.mp4"],
        clip_assignments=[{"media_id": "clip-1", "gcs_path": "users/u/plan/i/corfu.mp4"}],
        edit_proposal=proposal.model_dump(mode="json"),
    )
    monkeypatch.setattr(plan_items.settings, "guided_auto_design_enabled", True)
    monkeypatch.setattr(plan_items, "_load_owned_item", AsyncMock(return_value=item))
    monkeypatch.setattr(plan_items, "plan_item_response", lambda loaded, **_kw: loaded)
    monkeypatch.setattr(plan_items, "_get_instruction_level", AsyncMock(return_value="full"))
    monkeypatch.setattr("app.services.plan_clips.ensure_clip_media_ids", lambda _item: False)
    apply_calls = []
    monkeypatch.setattr(
        "app.tasks.edit_proposal_build.draft_edit_proposal.apply_async",
        lambda **kw: apply_calls.append(kw),
    )
    db = AsyncMock()

    await plan_items._maybe_auto_design_generate(
        str(item.id), item, _auto_design_plan(), _auto_design_user(), db
    )

    assert item.edit_proposal["status"] == "analyzing"
    assert item.edit_proposal["approval_mode"] == "auto"
    assert item.edit_proposal["brief"] == stated_brief.model_dump(mode="json")
    assert len(apply_calls) == 1
    assert "auto_finalize" not in apply_calls[0].get("kwargs", {})  # P2-6


@pytest.mark.asyncio
async def test_auto_design_resumes_legacy_pending_direction_idempotently(monkeypatch) -> None:
    """Old paused attempts must resume from Generate without a second click."""

    item = _awaiting_direction_item()
    monkeypatch.setattr(plan_items.settings, "guided_auto_design_enabled", True)
    monkeypatch.setattr(plan_items, "_load_owned_item", AsyncMock(return_value=item))
    monkeypatch.setattr(plan_items, "plan_item_response", lambda loaded, **_kw: loaded)
    monkeypatch.setattr(plan_items, "_get_instruction_level", AsyncMock(return_value="full"))
    monkeypatch.setattr("app.services.plan_clips.ensure_clip_media_ids", lambda _item: False)
    apply_calls = []
    monkeypatch.setattr(
        "app.tasks.edit_proposal_build.draft_edit_proposal.apply_async",
        lambda **kw: apply_calls.append(kw),
    )
    db = AsyncMock()

    first = await plan_items._maybe_auto_design_generate(
        str(item.id), item, _auto_design_plan(), _auto_design_user(), db
    )
    second = await plan_items._maybe_auto_design_generate(
        str(item.id), item, _auto_design_plan(), _auto_design_user(), db
    )

    proposal = parse_edit_proposal(item.edit_proposal)
    assert first is item and second is item
    assert proposal is not None and proposal.status == "analyzing"
    assert proposal.approval_mode == "auto"
    assert proposal.guidance is not None
    assert proposal.guidance.state == "confirmed"
    assert proposal.guidance.provenance == "ai_inferred"
    assert len(apply_calls) == 1


@pytest.mark.asyncio
async def test_auto_design_finalizes_an_existing_draft_instead_of_redrafting(monkeypatch) -> None:
    """P2-2c: status="draft" auto-finalizes THAT draft (approve + dispatch)

    rather than discarding it for a fresh redraft.
    """

    from app.tasks.content_plan_build import DispatchResult

    item = _draft_item()
    item.clip_gcs_paths = [item.clip_assignments[0]["gcs_path"]]
    monkeypatch.setattr(plan_items.settings, "guided_auto_design_enabled", True)
    monkeypatch.setattr(plan_items, "_load_owned_item", AsyncMock(return_value=item))
    monkeypatch.setattr(plan_items, "plan_item_response", lambda loaded, **_kw: loaded)
    monkeypatch.setattr(plan_items, "_get_instruction_level", AsyncMock(return_value="full"))
    monkeypatch.setattr(
        plan_items.storage,
        "object_metadata",
        lambda _path: SimpleNamespace(generation="42"),
    )
    dispatch_calls = []

    def _fake_dispatch(item_id_arg, epoch, *, reject_active_creator_session=False):
        dispatch_calls.append((item_id_arg, epoch, reject_active_creator_session))
        return DispatchResult("dispatched", job_id=str(uuid.uuid4()))

    monkeypatch.setattr("app.tasks.content_plan_build.dispatch_item_render_for", _fake_dispatch)
    db = AsyncMock()

    result = await plan_items._maybe_auto_design_generate(
        str(item.id), item, _auto_design_plan(), _auto_design_user(), db
    )

    assert result is item
    assert item.edit_proposal["status"] == "approved"
    assert item.edit_proposal["last_approved"] is not None
    assert dispatch_calls == [(str(item.id), 1, True)]


@pytest.mark.asyncio
async def test_auto_design_cutless_fast_montage_returns_replan_required(monkeypatch) -> None:
    """Auto-finalize must not mislabel an invalid legacy montage as a retry race."""

    item = _draft_item()
    proposal = parse_edit_proposal(item.edit_proposal)
    assert proposal is not None and proposal.draft is not None
    cutless = proposal.draft.model_copy(
        update={
            "direction": "fast_montage",
            "pace": "fast",
            "fast_cuts": None,
        }
    )
    proposal = proposal.model_copy(update={"draft": cutless})
    item.edit_proposal = proposal.model_dump(mode="json")
    monkeypatch.setattr(plan_items, "_proposal_media_is_current", AsyncMock(return_value=True))
    monkeypatch.setattr(
        plan_items,
        "_auto_design_idempotent_current",
        AsyncMock(side_effect=AssertionError("invalid montage is not an idempotent approval race")),
    )
    db = AsyncMock()

    with pytest.raises(HTTPException) as exc:
        await plan_items._auto_finalize_existing_draft(
            str(item.id),
            uuid.uuid4(),
            item,
            1,
            proposal,
            db,
            reject_active_creator_session=True,
        )

    assert exc.value.status_code == 409
    assert exc.value.detail["code"] == "proposal_replan_required"
    db.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_auto_design_dispatches_directly_for_an_already_approved_proposal(
    monkeypatch,
) -> None:
    """P2-2d: an "approved" proposal reached by a race (landed between the

    caller's initial unlocked read and this lock) must never be reset —
    begin_proposal_attempt must never clobber it back to "analyzing". It must
    also not just return None: the caller's `item`/`proposal_error` are a
    STALE pre-lock snapshot, so falling through to them would incorrectly
    re-raise the ORIGINAL conflict for a proposal that is actually approved
    now. Dispatch it directly instead.
    """

    from app.tasks.content_plan_build import DispatchResult

    snapshot = _snapshot()
    digest = canonical_media_digest(snapshot.media)
    proposal = EditProposal(
        proposal_version=5,
        generation_attempt_id="attempt-approved",
        media_digest=digest,
        status="approved",
        draft=snapshot,
        last_approved={
            "proposal_version": 5,
            "media_digest": digest,
            "approved_at": datetime.now(UTC).isoformat(),
            "snapshot": snapshot.model_dump(mode="json"),
        },
    )
    item = SimpleNamespace(
        id=uuid.uuid4(),
        clip_gcs_paths=["users/u/plan/i/corfu.mp4"],
        clip_assignments=[{"media_id": "clip-1", "gcs_path": "users/u/plan/i/corfu.mp4"}],
        edit_proposal=proposal.model_dump(mode="json"),
    )
    monkeypatch.setattr(plan_items.settings, "guided_auto_design_enabled", True)
    monkeypatch.setattr(plan_items, "_load_owned_item", AsyncMock(return_value=item))
    monkeypatch.setattr(plan_items, "plan_item_response", lambda loaded, **_kw: loaded)
    monkeypatch.setattr(plan_items, "_get_instruction_level", AsyncMock(return_value="full"))
    db = AsyncMock()

    def _boom(*_a, **_kw):
        raise AssertionError("must never reserve a fresh attempt over an approved proposal")

    monkeypatch.setattr("app.services.edit_proposals.begin_proposal_attempt", _boom)
    dispatch_calls = []

    def _fake_dispatch(item_id_arg, epoch, *, reject_active_creator_session=False):
        dispatch_calls.append((item_id_arg, epoch, reject_active_creator_session))
        return DispatchResult("dispatched", job_id=str(uuid.uuid4()))

    monkeypatch.setattr("app.tasks.content_plan_build.dispatch_item_render_for", _fake_dispatch)

    result = await plan_items._maybe_auto_design_generate(
        str(item.id), item, _auto_design_plan(), _auto_design_user(), db
    )

    assert result is item  # plan_item_response mocked to identity
    assert dispatch_calls == [(str(item.id), 1, True)]
    db.rollback.assert_awaited()
    assert item.edit_proposal["status"] == "approved"  # untouched
