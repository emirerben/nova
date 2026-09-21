"""KRI-121 round 2: a pilot project holding only Visuals becomes a device job.

Every decision point asks ``phone_destination``: the media filter and story
layouts, the render dispatch, the job builder, and the phone guided worker.
"""

from __future__ import annotations

import uuid
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.config import settings
from app.kria.render_assets import VisualRenderAsset
from app.pipeline.guided_story import GuidedStoryExecutionPlan
from app.schemas.edit_proposal import (
    EditProposalSnapshot,
    FastMontageCut,
    LicensedSfxIntent,
    MediaRef,
    StoryBeat,
)
from app.services import generative_jobs
from app.services.device_render import device_status
from app.services.edit_proposals import (
    phone_renderable_media,
    phone_story_layouts,
    renders_on_phone,
)
from app.services.generative_jobs import build_generative_job
from app.services.phone_destination import DEVICE_INTENT_KEY
from app.services.phone_sources import PHONE_SOURCES_FIELD, PHONE_VISUALS_FIELD
from app.tasks import generative_build as gb
from app.tasks.content_plan_build import _dispatch_item_render
from tests.services import test_phone_visuals as photos
from tests.tasks import test_phone_guided_dispatch as guided
from tests.tasks.test_content_plan_build import _cleanup_dispatch_item

OWNER = photos.USER_ID
PHOTO = MediaRef(
    lane="asset",
    media_id=photos.PHOTO_ID,
    gcs_path=photos.PHOTO_PATH,
    generation=photos.PHOTO_GENERATION,
    kind="image",
)
POOL_VIDEO = MediaRef(
    lane="asset",
    media_id=photos.VIDEO_ID,
    gcs_path=photos.VIDEO_PATH,
    generation=photos.VIDEO_GENERATION,
    kind="video",
    duration_s=6,
)
WEB_CLIP = MediaRef(
    lane="clip",
    media_id="web-clip",
    gcs_path=f"users/{OWNER}/plan/{photos.ITEM_ID}/harbor.mov",
    generation="5",
    kind="video",
    duration_s=20,
)


@pytest.fixture
def pilot(monkeypatch):
    monkeypatch.setattr(settings, "phone_rendering_enabled", True)
    monkeypatch.setattr(settings, "phone_render_user_ids", [OWNER])
    monkeypatch.setattr(
        settings,
        "phone_render_verified_features",
        ["basicComposition", "local1080Export", "audioMix", "stillImages"],
    )


# --- media filter + story layouts ---------------------------------------------


def test_the_destination_decision_makes_a_footage_free_item_a_phone_item(pilot):
    media = [PHOTO, POOL_VIDEO]
    assert not renders_on_phone(media, OWNER)
    assert renders_on_phone(media, OWNER, visuals_only_device=True)
    # visualVideos is unverified here, so the plan keeps only what the phone draws.
    assert phone_renderable_media(media, OWNER, visuals_only_device=True) == [PHOTO]
    assert phone_renderable_media(media, OWNER) == media


def test_the_decision_never_applies_to_media_with_a_clip_lane_source(pilot):
    media = [WEB_CLIP, PHOTO, POOL_VIDEO]
    assert not renders_on_phone(media, OWNER, visuals_only_device=True)
    assert phone_renderable_media(media, OWNER, visuals_only_device=True) == media


def test_the_decision_needs_an_enrolled_account(pilot):
    assert not renders_on_phone([PHOTO], uuid.uuid4(), visuals_only_device=True)


def test_pool_video_beats_go_fullscreen_on_a_footage_free_phone_item(pilot, monkeypatch):
    monkeypatch.setattr(
        settings,
        "phone_render_verified_features",
        [*settings.phone_render_verified_features, "visualVideos"],
    )
    snapshot = EditProposalSnapshot(
        direction="guided_story",
        goal="Show the harbor",
        pace="balanced",
        duration_s=8,
        title="Harbor morning",
        media=[PHOTO, POOL_VIDEO],
        story_beats=[
            StoryBeat(
                beat_id=f"beat-{index}",
                topic="Harbor",
                media_ids=[ref.media_id],
                layout="supporting_card",
                duration_s=4,
            )
            for index, ref in enumerate([PHOTO, POOL_VIDEO])
        ],
    )
    assert phone_story_layouts(snapshot, OWNER) is snapshot
    normalized = phone_story_layouts(snapshot, OWNER, visuals_only_device=True)
    assert [beat.layout for beat in normalized.story_beats] == ["supporting_card", "fullscreen"]


# --- job builder ---------------------------------------------------------------


def _builder_args(**changes):
    return {
        "user_id": OWNER,
        "clip_paths": [photos.PHOTO_PATH],
        "mode": "content_plan",
        "content_plan_item_id": photos.ITEM_ID,
        "content_plan_ownership_epoch": 3,
    } | changes


def test_builder_marks_a_device_job_that_has_no_source_bindings(pilot):
    job = build_generative_job(**_builder_args(), render_on_device=True)
    assert job.assembly_plan == {PHONE_SOURCES_FIELD: []}
    assert job.raw_storage_path == photos.PHOTO_PATH
    # Without the flag the same inputs stay the cloud job they are today.
    assert build_generative_job(**_builder_args()).assembly_plan is None


@pytest.mark.parametrize(
    "changes",
    [
        {"user_id": uuid.UUID("9d8c7b6a-5f4e-4d3c-8b2a-1f0e9d8c7b6a")},
        {"mode": "generative", "content_plan_item_id": None},
        {"voiceover_gcs_path": f"users/{OWNER}/creation-threads/{photos.ITEM_ID}/voice.m4a"},
        {"edit_format": "talking_head"},
        {"clip_paths": [f"users/{OWNER}/plan/{photos.ITEM_ID}/analysis-proxy-source.mp4"]},
    ],
    ids=["outside_the_cohort", "public_mode", "voiceover", "unguided_format", "proxy_seed"],
)
def test_builder_refuses_a_device_job_it_cannot_honor(pilot, changes):
    with pytest.raises(ValueError):
        build_generative_job(**_builder_args(**changes), render_on_device=True)


def test_builder_refuses_a_device_job_while_the_kill_switch_is_off(pilot, monkeypatch):
    monkeypatch.setattr(settings, "phone_rendering_enabled", False)
    with pytest.raises(ValueError, match="unavailable"):
        build_generative_job(**_builder_args(), render_on_device=True)


# --- render dispatch -------------------------------------------------------------


def _approved(
    *refs: MediaRef,
    selected: tuple[MediaRef, ...] | None = None,
    layout: str = "fullscreen",
    **changes,
) -> dict:
    chosen = refs if selected is None else selected
    snapshot = EditProposalSnapshot(
        title="Harbor morning",
        duration_s=max(3, 2 * len(chosen)),
        media=list(refs),
        story_beats=[
            StoryBeat(
                beat_id=f"beat-{index}",
                topic="Harbor",
                media_ids=[ref.media_id],
                layout=layout,
                duration_s=2,
            )
            for index, ref in enumerate(chosen)
        ],
        **changes,
    )
    return {
        "proposal_version": 1,
        "media_digest": "d" * 64,
        "snapshot": snapshot.model_dump(mode="json"),
    }


def _visuals_only_item(**changes) -> SimpleNamespace:
    item = _cleanup_dispatch_item()
    item.id = photos.ITEM_ID
    item.clip_gcs_paths = []
    item.clip_assignments = []
    item.edit_format = "montage"
    item.edit_proposal = {"generation_attempt_id": "gid-1"}
    for name, value in changes.items():
        setattr(item, name, value)
    return item


def _session(*, thread_state: dict | None, pool_kinds: list[str]) -> MagicMock:
    """Answer the destination rule's two reads; every other read stays a mock."""
    session = MagicMock()

    def execute(statement, *args, **kwargs):
        text = str(statement)
        result = MagicMock()
        if "creation_threads.state" in text:
            result.scalars.return_value = iter([] if thread_state is None else [thread_state])
        elif "plan_item_assets.kind" in text:
            result.scalars.return_value = iter(pool_kinds)
        return result

    session.execute.side_effect = execute
    return session


def _dispatch(monkeypatch, item, approved, *, thread_state, pool_kinds):
    monkeypatch.setattr(settings, "speech_cleanup_mode", "opt_in")
    monkeypatch.setattr(settings, "guided_edit_capability_enabled", True)
    plan = SimpleNamespace(id=uuid.uuid4(), user_id=OWNER, preference_summary="", ownership_epoch=0)
    session = _session(thread_state=thread_state, pool_kinds=pool_kinds)
    built: list = []
    real_build = generative_jobs.build_generative_job

    def build(**kwargs):
        # The real builder validates the request; a light stand-in carries its
        # assembly plan through the rest of dispatch.
        job = real_build(**kwargs)
        built.append(kwargs)
        return SimpleNamespace(id=uuid.uuid4(), assembly_plan=dict(job.assembly_plan or {}))

    with (
        patch(
            "app.services.smart_captions.resolve_smart_captions_context_sync",
            return_value=None,
        ),
        patch(
            "app.services.edit_proposals.validate_approved_proposal_media_sync",
            return_value=(None, approved),
        ),
        patch("app.services.generative_jobs.build_generative_job", side_effect=build),
        patch("app.services.creator_direction_snapshot.ensure_job_snapshot"),
        patch("app.services.job_dispatch.enqueue_orchestrator_sync"),
    ):
        result = _dispatch_item_render(
            session,
            item,
            plan,
            {"tone": "direct", "content_pillars": []},
            ownership_epoch=0,
        )
    return result, built, session


STAMPED = {"media": [], DEVICE_INTENT_KEY: "device"}


def test_a_photos_only_pilot_project_dispatches_a_device_job(pilot, monkeypatch):
    result, built, session = _dispatch(
        monkeypatch,
        _visuals_only_item(),
        _approved(PHOTO),
        thread_state=STAMPED,
        pool_kinds=["image"],
    )
    assert result.outcome == "dispatched"
    [kwargs] = built
    assert kwargs["render_on_device"] is True
    assert "phone_sources" not in kwargs
    assert kwargs["clip_paths"] == [photos.PHOTO_PATH]
    job = session.add.call_args.args[0]
    assert job.assembly_plan[PHONE_SOURCES_FIELD] == []
    # The worker routes on the marker and plans from the approved snapshot.
    assert job.assembly_plan["guided_edit"]["approved_proposal"]["media"][0]["lane"] == "asset"


def test_photos_and_pool_videos_dispatch_a_device_job_once_both_are_verified(pilot, monkeypatch):
    monkeypatch.setattr(
        settings,
        "phone_render_verified_features",
        [*settings.phone_render_verified_features, "visualVideos"],
    )
    result, built, _ = _dispatch(
        monkeypatch,
        _visuals_only_item(),
        _approved(PHOTO, POOL_VIDEO),
        thread_state=STAMPED,
        pool_kinds=["image", "video"],
    )
    assert result.outcome == "dispatched"
    assert built[0]["render_on_device"] is True


def test_an_unselected_undrawable_visual_does_not_block_the_device_job(pilot, monkeypatch):
    result, built, _ = _dispatch(
        monkeypatch,
        _visuals_only_item(),
        _approved(PHOTO, POOL_VIDEO, selected=(PHOTO,)),
        thread_state=STAMPED,
        pool_kinds=["image", "video"],
    )
    assert result.outcome == "dispatched"
    assert built[0]["render_on_device"] is True


@pytest.mark.parametrize("used_by", ["story_beat", "fast_cut"])
def test_a_plan_approved_around_an_undrawable_visual_is_replanned_not_sent_to_the_cloud(
    pilot, monkeypatch, used_by
):
    item = _visuals_only_item()
    approved = _approved(PHOTO, POOL_VIDEO)
    if used_by == "fast_cut":
        cuts = [(PHOTO, "hook", 0.0), (POOL_VIDEO, "build", 0.0), (PHOTO, "payoff", 1.0)]
        approved = _approved(
            PHOTO,
            POOL_VIDEO,
            selected=(PHOTO,),
            direction="fast_montage",
            fast_cuts=[
                FastMontageCut(
                    cut_id=f"cut-{index}",
                    media_id=ref.media_id,
                    source_start_s=start,
                    source_end_s=start + 1,
                    output_duration_s=1,
                    role=role,
                )
                for index, (ref, role, start) in enumerate(cuts)
            ],
        )
    with patch("app.services.edit_proposals.mark_edit_proposal_stale") as stale:
        result, built, _ = _dispatch(
            monkeypatch,
            item,
            approved,
            thread_state=STAMPED,
            pool_kinds=["image", "video"],
        )
    assert result.outcome == "proposal_stale"
    assert built == []
    stale.assert_called_once_with(item)


@pytest.mark.parametrize("case", ["video_on_a_card", "licensed_sfx"])
def test_a_plan_approved_before_the_device_route_that_the_phone_refuses_is_replanned(
    pilot, monkeypatch, case
):
    # Every selected Visual is drawable, but the plan predates the device stamp
    # (or the visualVideos verification): the phone layout rule and the phone
    # manifest's SFX refusal never ran on it, and the phone compiler rejects it.
    monkeypatch.setattr(
        settings,
        "phone_render_verified_features",
        [*settings.phone_render_verified_features, "visualVideos"],
    )
    approved = (
        _approved(PHOTO, POOL_VIDEO, layout="supporting_card")
        if case == "video_on_a_card"
        else _approved(PHOTO, licensed_sfx=LicensedSfxIntent(effect_id="sfx-fah"))
    )
    item = _visuals_only_item()
    with patch("app.services.edit_proposals.mark_edit_proposal_stale") as stale:
        result, built, _ = _dispatch(
            monkeypatch,
            item,
            approved,
            thread_state=STAMPED,
            pool_kinds=["image", "video"],
        )
    assert result.outcome == "proposal_stale"
    assert built == []
    stale.assert_called_once_with(item)


def test_photos_on_cards_still_dispatch_a_device_job(pilot, monkeypatch):
    # The phone draws a still on a card; only video moments must be fullscreen.
    result, built, _ = _dispatch(
        monkeypatch,
        _visuals_only_item(),
        _approved(PHOTO, *MORE_PHOTOS, layout="supporting_card"),
        thread_state=STAMPED,
        pool_kinds=["image"],
    )
    assert result.outcome == "dispatched"
    assert built[0]["render_on_device"] is True


@pytest.mark.parametrize(
    "case",
    ["web_thread", "no_thread", "kill_switch", "outside_the_cohort", "still_images_unverified"],
)
def test_without_the_rule_a_photos_only_project_stays_the_cloud_job_it_is_today(
    pilot, monkeypatch, case
):
    thread_state: dict | None = STAMPED
    if case == "web_thread":
        thread_state = {"media": []}
    elif case == "no_thread":
        thread_state = None
    elif case == "kill_switch":
        monkeypatch.setattr(settings, "phone_rendering_enabled", False)
    elif case == "outside_the_cohort":
        monkeypatch.setattr(settings, "phone_render_user_ids", [uuid.uuid4()])
    else:
        monkeypatch.setattr(settings, "phone_render_verified_features", ["audioMix"])
    result, built, session = _dispatch(
        monkeypatch,
        _visuals_only_item(),
        _approved(PHOTO),
        thread_state=thread_state,
        pool_kinds=["image"],
    )
    assert result.outcome == "dispatched"
    assert "render_on_device" not in built[0]
    assert PHONE_SOURCES_FIELD not in session.add.call_args.args[0].assembly_plan


def test_web_footage_beside_the_visuals_stays_a_cloud_job(pilot, monkeypatch):
    item = _visuals_only_item(
        clip_gcs_paths=[WEB_CLIP.gcs_path],
        clip_assignments=[{"media_id": WEB_CLIP.media_id, "gcs_path": WEB_CLIP.gcs_path}],
    )
    result, built, session = _dispatch(
        monkeypatch,
        item,
        _approved(WEB_CLIP, PHOTO),
        thread_state=STAMPED,
        pool_kinds=["image"],
    )
    assert result.outcome == "dispatched"
    assert "render_on_device" not in built[0]
    assert built[0]["clip_paths"] == [WEB_CLIP.gcs_path]
    # The rule is decided on the item alone: footage means it is never consulted.
    assert not any(
        "creation_threads.state" in str(c.args[0]) for c in session.execute.call_args_list
    )


@pytest.mark.parametrize(
    "changes",
    [
        {"audio_mode": "voiceover", "voiceover_gcs_path": "users/u/plan/i/voice.m4a"},
        {"edit_format": "subtitled"},
    ],
    ids=["voiceover", "unguided_format"],
)
def test_voiceover_and_unguided_formats_keep_todays_outcome(pilot, monkeypatch, changes):
    outcomes = []
    for thread_state in (STAMPED, {"media": []}):
        result, built, _ = _dispatch(
            monkeypatch,
            _visuals_only_item(**changes),
            _approved(PHOTO),
            thread_state=thread_state,
            pool_kinds=["image"],
        )
        outcomes.append((result.outcome, [kwargs.get("render_on_device") for kwargs in built]))
    # The stamp changes nothing for a project the phone cannot draw entirely:
    # neither format takes an asset-only guided plan, exactly as before.
    assert outcomes == [("invalid_clips", []), ("invalid_clips", [])]


# --- phone guided worker ------------------------------------------------------------


def _visuals_only_plan(*moments) -> GuidedStoryExecutionPlan:
    """``photos.pool_plan`` without its bound device clip."""
    plan, _ = photos.pool_plan(*moments)
    raw = plan.model_dump(mode="json")
    lead = raw["story_timeline"].pop(0)
    raw["beat_windows"].pop(0)
    raw["selected_media_ids"].remove(lead["media_id"])
    shift = lead["output_end_s"]
    for moment in raw["story_timeline"]:
        moment["output_start_s"] -= shift
        moment["output_end_s"] -= shift
    for window in raw["beat_windows"]:
        window["start_s"] -= shift
        window["end_s"] -= shift
    raw["approved_duration_s"] = raw["resolved_duration_s"] = raw["resolved_duration_s"] - shift
    return GuidedStoryExecutionPlan.model_validate(raw)


def _worker(monkeypatch, *, moments, rows, features):
    job, _, session, planner, cloud = guided.setup(monkeypatch)
    job.assembly_plan[PHONE_SOURCES_FIELD] = []
    job.user_id = OWNER
    job.content_plan_item_id = photos.ITEM_ID
    planner.return_value = (_visuals_only_plan(*moments).model_dump(mode="json"), None)
    session.get.return_value = job
    session.execute.return_value.scalars.return_value.all.return_value = rows
    photos.fake_storage(
        monkeypatch,
        {
            (photos.PHOTO_PATH, photos.PHOTO_GENERATION): photos.PHOTO_BYTES,
            (photos.VIDEO_PATH, photos.VIDEO_GENERATION): photos.VIDEO_BYTES,
        },
    )
    photos.fake_probe(monkeypatch)
    monkeypatch.setattr(
        gb.settings,
        "phone_render_verified_features",
        [*gb.settings.phone_render_verified_features, *features],
    )
    return job, cloud


def test_worker_pins_a_photos_only_recipe_with_no_source_bindings(monkeypatch):
    job, cloud = _worker(
        monkeypatch,
        moments=[photos.photo_moment()],
        rows=[photos.photo_row()],
        features=["stillImages"],
    )
    gb._run_generative_job(str(job.id))
    assert job.status == "awaiting_device"
    recipe = device_status(job, "guided_story").request.recipe
    assert [type(asset) for asset in recipe.asset_manifest.assets] == [VisualRenderAsset]
    assert [clip.source_asset_id for clip in recipe.tracks[0].clips] == [
        f"visual-{photos.PHOTO_ID}"
    ]
    assert recipe.duration == 2
    assert "stillImages" in recipe.required_capabilities
    assert job.assembly_plan[PHONE_SOURCES_FIELD] == []
    assert job.assembly_plan[PHONE_VISUALS_FIELD] == [photos.photo_visual().model_dump(mode="json")]
    assert job.assembly_plan["variants"][0]["render_destination"] == "device"
    cloud.assert_not_called()


def test_worker_pins_photos_and_pool_videos_with_no_source_bindings(monkeypatch):
    job, cloud = _worker(
        monkeypatch,
        moments=[photos.photo_moment(), photos.video_moment()],
        rows=[photos.photo_row(), photos.video_row()],
        features=["stillImages", "visualVideos"],
    )
    gb._run_generative_job(str(job.id))
    assert job.status == "awaiting_device"
    recipe = device_status(job, "guided_story").request.recipe
    assets = recipe.asset_manifest.assets
    assert all(isinstance(asset, VisualRenderAsset) for asset in assets)
    assert sorted(asset.media_kind for asset in assets) == ["image", "video"]
    assert not any(asset.is_proxy_available for asset in recipe.assets)
    assert {"stillImages", "visualVideos"} <= set(recipe.required_capabilities)
    assert recipe.duration == 4
    cloud.assert_not_called()


def test_worker_fails_closed_when_there_is_nothing_at_all_to_render_from(monkeypatch):
    # stillImages lost its verification after dispatch: nothing binds, and a
    # device job never falls back to a cloud renderer.
    job, cloud = _worker(
        monkeypatch, moments=[photos.photo_moment()], rows=[photos.photo_row()], features=[]
    )
    args, kwargs = guided.run_until_failure(monkeypatch, job)
    assert kwargs.get("failure_reason") == "phone_plan_unsupported"
    assert "source bindings or pinned visuals" in args[1]
    assert "_device_render_v1" not in job.assembly_plan
    cloud.assert_not_called()


# --- proposal build -----------------------------------------------------------------


# Fewer than three photos are too short for a guided story, on the phone or in
# the cloud (each still is credited GUIDED_STORY_MIN_MOMENT_S).
MORE_PHOTOS = [
    PHOTO.model_copy(
        update={
            "media_id": media_id,
            "gcs_path": photos.PHOTO_PATH.replace("photo.jpg", f"{media_id}.jpg"),
        }
    )
    for media_id in (
        "2f6c1a9e-7b3d-4e8f-a1c2-d3e4f5a6b7c8",
        "6e5d4c3b-2a19-4f8e-9d7c-6b5a4f3e2d1c",
    )
]
PHOTOS = [PHOTO, *MORE_PHOTOS]
POOL = [*PHOTOS, POOL_VIDEO]


class _VisualsOnlyOutput:
    """The specialist gives every offered Visual its own supporting-card beat."""

    title = "Harbor morning"

    def __init__(self, offered: list[str]) -> None:
        # The shortest legible moment each, which a still can always fill.
        self.duration_s = 1.4 * len(offered)
        self.story_beats = [
            SimpleNamespace(
                topic="Harbor",
                thought="The harbor wakes up slowly.",
                media_ids=[media_id],
                layout="supporting_card",
                duration_s=1.4,
            )
            for media_id in offered
        ]


def _drafted_snapshot(monkeypatch, *, on_device: bool):
    import app.tasks.edit_proposal_build as proposal_build
    from app.schemas.edit_proposal import EditProposal, ProposalBrief, parse_edit_proposal
    from tests.tasks.test_phone_photo_layout import _Db

    item = SimpleNamespace(
        id=photos.ITEM_ID,
        idea="Harbor",
        theme="",
        clip_assignments=[],
        clip_gcs_paths=[],
        edit_proposal=EditProposal(
            proposal_version=1,
            generation_attempt_id="attempt-1",
            status="analyzing",
            approval_mode="auto",
            brief=ProposalBrief(direction="guided_story", duration_s=6),
        ).model_dump(mode="json"),
    )
    offered: list[list[str]] = []
    decisions: list[tuple] = []

    def specialist(_agent, agent_input, **_kw):  # noqa: ANN001, ANN202
        offered.append([media.media_id for media in agent_input.media])
        return _VisualsOnlyOutput(offered[-1])

    def decide(_db, decided_item, owner_id):  # noqa: ANN001, ANN202
        decisions.append((decided_item, owner_id))
        return on_device

    @contextmanager
    def session():
        yield _Db([SimpleNamespace(user_id=OWNER, status="ready")])

    monkeypatch.setattr(proposal_build, "sync_session", session)
    monkeypatch.setattr(proposal_build, "_locked_item", lambda *_a, **_kw: (item, OWNER))
    monkeypatch.setattr(proposal_build, "_attempt_is_active", lambda *_a, **_kw: True)
    monkeypatch.setattr(proposal_build, "_pool_refs", lambda *_a, **_kw: list(POOL))
    monkeypatch.setattr(proposal_build, "item_visuals_only_on_device_sync", decide)
    monkeypatch.setattr(proposal_build, "_analyze_clip_assignments", lambda *_a, **_kw: [])
    monkeypatch.setattr(proposal_build, "media_generations_match_sync", lambda _refs: True)
    monkeypatch.setattr("app.agents._model_client.default_client", lambda: None)
    monkeypatch.setattr("app.agents.edit_proposal.EditProposalAgent.run", specialist)

    proposal_build._run_draft_attempt(
        SimpleNamespace(), item.id, str(item.id), "attempt-1", 0, auto_finalize=True
    )

    persisted = parse_edit_proposal(item.edit_proposal)
    assert persisted is not None and persisted.status == "approved", persisted
    # Decided when planning starts and rechecked under the lock that saves it.
    assert decisions == [(item, OWNER), (item, OWNER)]
    return offered, persisted.last_approved.snapshot


def test_a_footage_free_phone_item_plans_only_the_visuals_the_iphone_draws(pilot, monkeypatch):
    offered, snapshot = _drafted_snapshot(monkeypatch, on_device=True)
    # visualVideos is unverified in this pilot fixture.
    assert offered == [[ref.media_id for ref in PHOTOS]]
    assert [ref.media_id for ref in snapshot.media] == offered[0]
    assert [beat.layout for beat in snapshot.story_beats] == ["supporting_card"] * 3


def test_a_footage_free_phone_item_draws_its_pool_video_fullscreen(pilot, monkeypatch):
    monkeypatch.setattr(
        settings,
        "phone_render_verified_features",
        [*settings.phone_render_verified_features, "visualVideos"],
    )
    offered, snapshot = _drafted_snapshot(monkeypatch, on_device=True)
    assert offered == [[ref.media_id for ref in POOL]]
    assert [beat.layout for beat in snapshot.story_beats] == [
        *["supporting_card"] * 3,
        "fullscreen",
    ]


def test_a_footage_free_cloud_item_is_planned_exactly_as_before(pilot, monkeypatch):
    offered, snapshot = _drafted_snapshot(monkeypatch, on_device=False)
    assert offered == [[ref.media_id for ref in POOL]]
    assert [beat.layout for beat in snapshot.story_beats] == ["supporting_card"] * 4


# --- confirm-time digest (plan_items) --------------------------------------------------


def _pool_row(ref: MediaRef, **changes) -> SimpleNamespace:
    return SimpleNamespace(
        id=ref.media_id,
        gcs_path=ref.gcs_path,
        gcs_generation=ref.generation,
        kind=ref.kind,
        content_hash=None,
        duration_s=ref.duration_s,
        analysis=dict(ref.analysis),
        **changes,
    )


@pytest.mark.asyncio
async def test_confirm_and_build_drop_the_same_videos_the_iphone_cannot_draw(pilot, monkeypatch):
    import app.routes.plan_items as plan_items

    monkeypatch.setattr(
        settings,
        "phone_render_verified_features",
        [*settings.phone_render_verified_features, "visualVideos"],
    )

    def video(name: str, **fields) -> MediaRef:
        return POOL_VIDEO.model_copy(
            update={"media_id": str(uuid.uuid5(uuid.NAMESPACE_URL, name)), **fields}
        )

    playable = video("h264", analysis={"video_codec": "h264", "pix_fmt": "yuv420p"})
    vp9 = video("vp9", analysis={"video_codec": "vp9", "pix_fmt": "yuv420p"})
    long_recording = video("long", duration_s=2400.0)
    pool = [PHOTO, playable, vp9, long_recording]
    db = MagicMock()
    db.execute = AsyncMock(return_value=MagicMock(scalars=lambda: [_pool_row(ref) for ref in pool]))
    monkeypatch.setattr(
        "app.services.phone_destination.item_visuals_only_on_device",
        AsyncMock(return_value=True),
    )

    confirmed = await plan_items._current_direction_media_refs(
        _visuals_only_item(), db, user_id=OWNER
    )
    built = phone_renderable_media(pool, OWNER, visuals_only_device=True)

    # Both digests must see the same media, or confirmation loops on proposal_stale.
    assert [ref.media_id for ref in confirmed] == [ref.media_id for ref in built]
    assert [ref.media_id for ref in built] == [PHOTO.media_id, playable.media_id]


@pytest.mark.asyncio
@pytest.mark.parametrize("on_device", [False, True])
async def test_the_confirm_digest_covers_the_same_media_the_build_planned(
    pilot, monkeypatch, on_device
):
    import app.routes.plan_items as plan_items

    rows = [_pool_row(ref) for ref in (PHOTO, POOL_VIDEO)]
    db = MagicMock()
    db.execute = AsyncMock(return_value=MagicMock(scalars=lambda: rows))
    decide = AsyncMock(return_value=on_device)
    monkeypatch.setattr("app.services.phone_destination.item_visuals_only_on_device", decide)
    item = _visuals_only_item()

    refs = await plan_items._current_direction_media_refs(item, db, user_id=OWNER)

    decide.assert_awaited_once_with(db, item, OWNER)
    # visualVideos is unverified in this pilot fixture.
    expected = [photos.PHOTO_ID] if on_device else [photos.PHOTO_ID, photos.VIDEO_ID]
    assert [ref.media_id for ref in refs] == expected
