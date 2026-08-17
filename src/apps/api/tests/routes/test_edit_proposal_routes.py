from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from starlette.requests import Request

import app.routes.plan_items as plan_items
from app.agents.edit_guide import EditGuideOutput, EditGuideRevision, EditGuideRevisionBeat
from app.schemas.edit_proposal import (
    EditProposal,
    EditProposalSnapshot,
    MediaRef,
    ProposalBrief,
    StoryBeat,
    canonical_media_digest,
    parse_edit_proposal,
)


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
