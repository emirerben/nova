"""KRI-121: a phone item's video moments stay fullscreen; its photos may be cards."""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import app.routes.plan_items as plan_items
from app.config import settings
from app.pipeline import guided_story
from app.schemas.edit_proposal import (
    EditProposal,
    EditProposalSnapshot,
    MediaRef,
    ProposalBrief,
    StoryBeat,
    canonical_media_digest,
    parse_edit_proposal,
)
from app.services.edit_proposals import phone_story_layouts

_PHONE_CLIP = "users/u/plan/i/analysis-proxy-harbor.mp4"
_CLOUD_CLIP = "users/u/plan/i/2d3cc760377d4b6993467a2955d4c945-harbor.mov"


def _phone_account(monkeypatch, *, enabled: bool = True, cohort: list | None = None) -> None:
    monkeypatch.setattr(settings, "phone_rendering_enabled", enabled)
    monkeypatch.setattr(settings, "phone_render_user_ids", cohort or [])


def _snapshot(
    beats: list[tuple[list[str], str]],
    *,
    clip_path: str = _PHONE_CLIP,
    image_layout: str | None = None,
    with_clip: bool = True,
) -> EditProposalSnapshot:
    media = [
        MediaRef(
            lane="clip",
            media_id="clip",
            gcs_path=clip_path,
            generation="1",
            kind="video",
            duration_s=20,
        ),
        MediaRef(
            lane="asset",
            media_id="photo",
            gcs_path="users/u/plan/i/pool/harbor.jpg",
            generation="2",
            kind="image",
        ),
        MediaRef(
            lane="asset",
            media_id="pool-video",
            gcs_path="users/u/plan/i/pool/harbor.mov",
            generation="3",
            kind="video",
            duration_s=20,
        ),
    ]
    if not with_clip:
        media = [ref for ref in media if ref.lane == "asset"]
    return EditProposalSnapshot(
        direction="guided_story",
        goal="Show the harbor",
        pace="balanced",
        duration_s=4 * len(beats),
        title="Harbor morning",
        image_layout=image_layout,
        media=media,
        story_beats=[
            StoryBeat(
                beat_id=f"beat-{index}",
                topic="Harbor",
                media_ids=media_ids,
                layout=layout,
                duration_s=4,
            )
            for index, (media_ids, layout) in enumerate(beats)
        ],
    )


def _compiled_layouts(snapshot: EditProposalSnapshot) -> list[tuple[str, str]]:
    plan = guided_story.compile_execution_plan(
        {
            "proposal_version": 1,
            "media_digest": canonical_media_digest(snapshot.media, snapshot.narration),
            "approved_proposal": snapshot.model_dump(mode="json"),
            "media_identities": [
                {
                    "lane": ref.lane,
                    "media_id": ref.media_id,
                    "gcs_path": ref.gcs_path,
                    "generation": ref.generation,
                    "kind": ref.kind,
                }
                for ref in snapshot.media
            ],
        },
        track=None,
    )
    return [(moment["media_id"], moment["layout"]) for moment in plan["story_timeline"]]


def test_phone_video_beats_go_fullscreen_and_photo_beats_keep_their_card(monkeypatch) -> None:
    _phone_account(monkeypatch)
    snapshot = _snapshot(
        [
            (["clip"], "supporting_card"),
            (["photo"], "supporting_card"),
            (["pool-video"], "supporting_card"),
            (["clip"], "fullscreen"),
        ]
    )

    normalized = phone_story_layouts(snapshot, uuid.uuid4())

    assert [beat.layout for beat in normalized.story_beats] == [
        "fullscreen",
        "supporting_card",
        "fullscreen",
        "fullscreen",
    ]
    # Nothing but layout moves, and the caller's snapshot is not mutated.
    assert normalized.story_beats[1] is snapshot.story_beats[1]
    assert snapshot.story_beats[0].layout == "supporting_card"
    assert normalized.model_dump(exclude={"story_beats"}) == snapshot.model_dump(
        exclude={"story_beats"}
    )
    assert _compiled_layouts(normalized) == [
        ("clip", "fullscreen"),
        ("photo", "supporting_card"),
        ("pool-video", "fullscreen"),
        ("clip", "fullscreen"),
    ]


@pytest.mark.parametrize(
    ("image_layout", "photo_layout"),
    [
        # "Don't crop my photos" outranks the beat, so the photo keeps its card.
        ("supporting_card", "supporting_card"),
        ("fullscreen", "fullscreen"),
        # No creator preference: only the specialist's styling is lost.
        (None, "fullscreen"),
    ],
)
def test_photo_sharing_a_beat_with_video_follows_the_creators_image_layout(
    monkeypatch, image_layout: str | None, photo_layout: str
) -> None:
    _phone_account(monkeypatch)
    snapshot = _snapshot([(["photo", "clip"], "supporting_card")], image_layout=image_layout)

    normalized = phone_story_layouts(snapshot, uuid.uuid4())

    assert normalized.image_layout == image_layout
    assert _compiled_layouts(normalized) == [("photo", photo_layout), ("clip", "fullscreen")]


def test_already_renderable_phone_snapshot_is_returned_as_is(monkeypatch) -> None:
    _phone_account(monkeypatch)
    snapshot = _snapshot([(["clip"], "fullscreen"), (["photo"], "supporting_card")])

    assert phone_story_layouts(snapshot, uuid.uuid4()) is snapshot


@pytest.mark.parametrize("case", ["cloud_sources", "kill_switch", "outside_cohort"])
def test_items_that_render_in_the_cloud_keep_video_cards(monkeypatch, case: str) -> None:
    _phone_account(
        monkeypatch,
        enabled=case != "kill_switch",
        cohort=[uuid.uuid4()] if case == "outside_cohort" else None,
    )
    snapshot = _snapshot(
        [(["clip", "photo"], "supporting_card")],
        clip_path=_CLOUD_CLIP if case == "cloud_sources" else _PHONE_CLIP,
    )

    assert phone_story_layouts(snapshot, uuid.uuid4()) is snapshot


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("clip_path", "saved_layout"),
    [(_PHONE_CLIP, "fullscreen"), (_CLOUD_CLIP, "supporting_card")],
)
async def test_creator_layout_edit_cannot_put_phone_video_on_a_card(
    monkeypatch, clip_path: str, saved_layout: str
) -> None:
    _phone_account(monkeypatch)
    draft = _snapshot([(["clip"], "fullscreen"), (["photo"], "fullscreen")], clip_path=clip_path)
    item = SimpleNamespace(
        id=uuid.uuid4(),
        clip_assignments=[{"media_id": "clip", "gcs_path": clip_path}],
        edit_proposal=EditProposal(
            proposal_version=2,
            generation_attempt_id="attempt-1",
            media_digest=canonical_media_digest(draft.media),
            status="draft",
            brief=ProposalBrief(),
            draft=draft,
        ).model_dump(mode="json"),
    )
    monkeypatch.setattr(settings, "guided_edit_capability_enabled", True)
    monkeypatch.setattr(settings, "guided_edit_conversation_enabled", True)
    monkeypatch.setattr(plan_items, "_load_owned_item", AsyncMock(return_value=item))
    monkeypatch.setattr(plan_items, "_proposal_media_is_current", AsyncMock(return_value=True))
    monkeypatch.setattr(plan_items, "plan_item_response", lambda loaded: loaded)
    edited = _snapshot(
        [(["clip"], "supporting_card"), (["photo"], "supporting_card")], clip_path=clip_path
    )

    await plan_items.update_item_edit_proposal(
        str(item.id),
        plan_items.UpdateEditProposalBody(expected_proposal_version=2, snapshot=edited),
        SimpleNamespace(id=uuid.uuid4()),
        AsyncMock(),
    )

    persisted = parse_edit_proposal(item.edit_proposal)
    assert persisted is not None and persisted.draft is not None
    assert [beat.layout for beat in persisted.draft.story_beats] == [
        saved_layout,
        "supporting_card",
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("on_device", "saved_layout"), [(True, "fullscreen"), (False, "supporting_card")]
)
async def test_creator_layout_edit_asks_the_visuals_only_rule(
    monkeypatch, on_device: bool, saved_layout: str
) -> None:
    # No footage, so only the shared destination rule says "iPhone". The edit
    # route must ask it like the proposal build does, or a pool video saved onto
    # a card would fail the device compile.
    _phone_account(monkeypatch)
    beats = [(["pool-video"], "fullscreen"), (["photo"], "fullscreen")]
    draft = _snapshot(beats, with_clip=False)
    item = SimpleNamespace(
        id=uuid.uuid4(),
        clip_assignments=[],
        edit_proposal=EditProposal(
            proposal_version=2,
            generation_attempt_id="attempt-1",
            media_digest=canonical_media_digest(draft.media),
            status="draft",
            brief=ProposalBrief(),
            draft=draft,
        ).model_dump(mode="json"),
    )
    rule = AsyncMock(return_value=on_device)
    monkeypatch.setattr(settings, "guided_edit_capability_enabled", True)
    monkeypatch.setattr(settings, "guided_edit_conversation_enabled", True)
    monkeypatch.setattr(plan_items, "_load_owned_item", AsyncMock(return_value=item))
    monkeypatch.setattr(plan_items, "_proposal_media_is_current", AsyncMock(return_value=True))
    monkeypatch.setattr(plan_items, "plan_item_response", lambda loaded: loaded)
    monkeypatch.setattr("app.services.phone_destination.item_visuals_only_on_device", rule)
    user = SimpleNamespace(id=uuid.uuid4())
    db = AsyncMock()

    await plan_items.update_item_edit_proposal(
        str(item.id),
        plan_items.UpdateEditProposalBody(
            expected_proposal_version=2,
            snapshot=_snapshot([(ids, "supporting_card") for ids, _ in beats], with_clip=False),
        ),
        user,
        db,
    )

    rule.assert_awaited_once_with(db, item, user.id)
    persisted = parse_edit_proposal(item.edit_proposal)
    assert persisted is not None and persisted.draft is not None
    assert [beat.layout for beat in persisted.draft.story_beats] == [
        saved_layout,
        "supporting_card",
    ]
