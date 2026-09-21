"""KRI-121: phone items keep photo cards, and only plan media the iPhone draws."""

from __future__ import annotations

import uuid
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

import app.tasks.edit_proposal_build as proposal_build
from app.config import settings
from app.pipeline import guided_story
from app.schemas.edit_proposal import (
    EditProposal,
    MediaRef,
    ProposalBrief,
    canonical_media_digest,
    parse_edit_proposal,
)

_CLIP_ID = "85fcc2f9-12c2-42e8-9fd6-6b2d767075cb"
_PHOTO_ID = "4a0b5f0e-2d55-4f5e-9a55-0d0f5d1c9b11"
_POOL_VIDEO_ID = "0c8d7e6f-5a4b-4c3d-8e2f-1a0b9c8d7e6f"
_PHONE_CLIP = "users/u/plan/i/analysis-proxy-harbor.mp4"


class _Result:
    def __init__(self, rows: list) -> None:
        self._rows = rows

    def one_or_none(self):  # noqa: ANN201
        return None

    def all(self) -> list:
        return self._rows

    def scalars(self) -> list:
        return self._rows

    def scalar_one(self) -> int:
        return len(self._rows)


class _Db:
    def __init__(self, rows: list) -> None:
        self.result = _Result(rows)

    def execute(self, _query):  # noqa: ANN001, ANN201
        return self.result

    def commit(self) -> None:
        pass

    def flush(self) -> None:
        pass


class _Beat:
    def __init__(self, media_id: str, layout: str, duration_s: float) -> None:
        self.topic = "Harbor"
        self.thought = "The harbor wakes up slowly."
        self.media_ids = [media_id]
        self.layout = layout
        self.duration_s = duration_s


class _AgentOutput:
    """The specialist asks for a supporting card on every pool beat it was offered."""

    title = "Harbor morning"
    duration_s = 8

    def __init__(self, offered: list[str]) -> None:
        self.story_beats = [_Beat(_CLIP_ID, "fullscreen", 5)] + [
            _Beat(media_id, "supporting_card", 3 / max(1, len(offered) - 1))
            for media_id in offered
            if media_id != _CLIP_ID
        ]


def _settings(monkeypatch, *, verified: list[str], enabled: bool = True) -> None:
    monkeypatch.setattr(settings, "phone_rendering_enabled", enabled)
    monkeypatch.setattr(settings, "phone_render_user_ids", [])
    monkeypatch.setattr(settings, "phone_render_verified_features", verified)


def _approved_snapshot(  # noqa: ANN202
    monkeypatch,
    *,
    clip_path: str,
    brief_layout: str | None,
    pool_video: bool = False,
    offered: list[list[str]] | None = None,
):
    item_id = uuid.uuid4()
    owner_id = uuid.uuid4()
    assignment = {"media_id": _CLIP_ID, "gcs_path": clip_path}
    item = SimpleNamespace(
        id=item_id,
        idea="Harbor",
        theme="",
        clip_assignments=[assignment],
        clip_gcs_paths=[clip_path],
        edit_proposal=EditProposal(
            proposal_version=1,
            generation_attempt_id="attempt-1",
            status="analyzing",
            approval_mode="auto",
            brief=ProposalBrief(direction="guided_story", duration_s=8, image_layout=brief_layout),
        ).model_dump(mode="json"),
    )
    clip_ref = MediaRef(
        lane="clip",
        media_id=_CLIP_ID,
        gcs_path=clip_path,
        generation="1",
        kind="video",
        duration_s=10,
    )
    # Visuals pool ids are the bare PlanItemAsset UUID (_pool_refs).
    photo_ref = MediaRef(
        lane="asset",
        media_id=_PHOTO_ID,
        gcs_path=f"users/u/plan/{item_id}/pool/harbor.jpg",
        generation="2",
        kind="image",
    )
    pool = [photo_ref]
    if pool_video:
        pool.append(
            MediaRef(
                lane="asset",
                media_id=_POOL_VIDEO_ID,
                gcs_path=f"users/u/plan/{item_id}/pool/harbor.mov",
                generation="3",
                kind="video",
                duration_s=6,
            )
        )
    db = _Db([SimpleNamespace(user_id=owner_id, status="ready")])

    def _specialist(_agent, agent_input, **_kw):  # noqa: ANN001, ANN202
        ids = [media.media_id for media in agent_input.media]
        if offered is not None:
            offered.append(ids)
        return _AgentOutput(ids)

    @contextmanager
    def _session():
        yield db

    monkeypatch.setattr(proposal_build, "sync_session", _session)
    monkeypatch.setattr(proposal_build, "_locked_item", lambda *_a, **_kw: (item, owner_id))
    monkeypatch.setattr(proposal_build, "_attempt_is_active", lambda *_a, **_kw: True)
    monkeypatch.setattr(proposal_build, "_pool_refs", lambda *_a, **_kw: list(pool))
    monkeypatch.setattr(
        proposal_build,
        "_analyze_clip_assignments",
        lambda assignments, *_a, **_kw: [(assignments[0], clip_ref)],
    )
    monkeypatch.setattr(proposal_build, "media_generations_match_sync", lambda _refs: True)
    monkeypatch.setattr("app.agents._model_client.default_client", lambda: None)
    monkeypatch.setattr("app.agents.edit_proposal.EditProposalAgent.run", _specialist)

    proposal_build._run_draft_attempt(
        SimpleNamespace(), item_id, str(item_id), "attempt-1", 0, auto_finalize=True
    )

    persisted = parse_edit_proposal(item.edit_proposal)
    assert persisted is not None and persisted.status == "approved"
    return persisted.last_approved.snapshot


def _compiled_layouts(snapshot) -> dict[str, str]:  # noqa: ANN001
    """Compile the approved snapshot exactly as the worker's guided plan does."""

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
    return {moment["media_id"]: moment["layout"] for moment in plan["story_timeline"]}


@pytest.mark.parametrize("brief_layout", ["supporting_card", None])
def test_phone_item_keeps_photo_cards_from_brief_and_beat_layout(
    monkeypatch, brief_layout: str | None
) -> None:
    _settings(monkeypatch, verified=["stillImages"])

    snapshot = _approved_snapshot(monkeypatch, clip_path=_PHONE_CLIP, brief_layout=brief_layout)

    # The iPhone draws supporting cards itself, so "don't crop my photos" and
    # the specialist's photo-only card beat both reach the device unchanged.
    assert snapshot.image_layout == brief_layout
    assert ("image_layout" in snapshot.model_dump(mode="json")) == (brief_layout is not None)
    assert [beat.layout for beat in snapshot.story_beats] == ["fullscreen", "supporting_card"]
    assert _compiled_layouts(snapshot) == {_CLIP_ID: "fullscreen", _PHOTO_ID: "supporting_card"}


@pytest.mark.parametrize("brief_layout", ["supporting_card", None])
@pytest.mark.parametrize(
    ("clip_path", "verified"),
    [
        # A cloud item never needs the phone's layout restriction, verified or not.
        ("users/u/plan/i/2d3cc760377d4b6993467a2955d4c945-harbor.mov", ["stillImages"]),
        ("users/u/plan/i/2d3cc760377d4b6993467a2955d4c945-harbor.mov", []),
    ],
)
def test_flag_off_or_cloud_item_keeps_snapshot_layout(
    monkeypatch, clip_path: str, verified: list[str], brief_layout: str | None
) -> None:
    _settings(monkeypatch, verified=verified)

    snapshot = _approved_snapshot(monkeypatch, clip_path=clip_path, brief_layout=brief_layout)

    assert snapshot.image_layout == brief_layout
    # An unset layout stays absent from the serialized snapshot, as before.
    assert ("image_layout" in snapshot.model_dump(mode="json")) == (brief_layout is not None)
    assert _compiled_layouts(snapshot) == {_CLIP_ID: "fullscreen", _PHOTO_ID: "supporting_card"}


@pytest.mark.parametrize(
    ("verified", "expected"),
    [
        # Each Visuals kind is plannable only once the device is verified for it.
        (["stillImages", "visualVideos"], [_CLIP_ID, _PHOTO_ID, _POOL_VIDEO_ID]),
        (["stillImages"], [_CLIP_ID, _PHOTO_ID]),
        (["visualVideos"], [_CLIP_ID, _POOL_VIDEO_ID]),
        # Before either, only the bound footage is plannable.
        ([], [_CLIP_ID]),
    ],
)
def test_phone_item_plans_only_media_the_iphone_draws(
    monkeypatch, verified: list[str], expected: list[str]
) -> None:
    _settings(monkeypatch, verified=verified)
    offered: list[list[str]] = []

    snapshot = _approved_snapshot(
        monkeypatch, clip_path=_PHONE_CLIP, brief_layout=None, pool_video=True, offered=offered
    )

    assert offered == [expected]
    assert [ref.media_id for ref in snapshot.media] == expected
    # The specialist asked for a card on every pool beat. The iPhone draws video
    # fullscreen only, so the Visuals video's beat is normalized; the photo's isn't.
    assert _compiled_layouts(snapshot) == {
        media_id: "supporting_card" if media_id == _PHOTO_ID else "fullscreen"
        for media_id in expected
    }


def test_cloud_item_keeps_every_pool_visual(monkeypatch) -> None:
    _settings(monkeypatch, verified=["stillImages"])
    offered: list[list[str]] = []

    snapshot = _approved_snapshot(
        monkeypatch,
        clip_path="users/u/plan/i/2d3cc760377d4b6993467a2955d4c945-harbor.mov",
        brief_layout=None,
        pool_video=True,
        offered=offered,
    )

    assert offered == [[_CLIP_ID, _PHOTO_ID, _POOL_VIDEO_ID]]
    # A cloud render draws video cards, so the specialist's layout is untouched.
    assert _compiled_layouts(snapshot)[_POOL_VIDEO_ID] == "supporting_card"
