"""KRI-287: a phone-rendered Voiceover video (`narrated` or montage `voiceover`)
can add a photo or video from the iPhone editor's Visuals panel.

Covers the capability (`phone_editor_media` + `visual_blocks` + the revision the
app registers against), every gate that keeps it closed, source registration,
and the Save that compiles media Visual blocks into the pinned device recipe.
"""

from __future__ import annotations

import uuid

import pytest
from fastapi import HTTPException

import app.routes.generative_jobs as gj
from app.pipeline.phone_narrated_plan import EDITOR_MEDIA_TRACK_ID
from app.services.client_protocol import clear_request_context, set_client_protocol
from app.services.device_render import device_status
from app.services.phone_editor_sources import EDITOR_SOURCES_FIELD
from tests.routes.test_phone_voiceover_editor_lanes import (
    _POOL,
    _VERIFIED,
    _fake_inspect,
    _sfx_payload,
    voiceover_job,
)
from tests.services import test_phone_visuals as pool

# The KRI-281 app build: it already previews and reopens editor media.
_MEDIA_PROTOCOL = 3


@pytest.fixture(autouse=True)
def _reset_protocol():
    clear_request_context()
    yield
    clear_request_context()


def _enable(monkeypatch, *, protocol: int | None = _MEDIA_PROTOCOL) -> None:
    for name, value in {
        "phone_rendering_enabled": True,
        "phone_render_user_ids": [],
        "phone_render_verified_features": list(_VERIFIED),
        "phone_narrated_caption_edits_enabled": True,
        "sound_effects_enabled": True,
        "media_overlays_enabled": True,
        "phone_voiceover_editor_lanes_enabled": True,
        "phone_editor_media_enabled": True,
        "visual_blocks_enabled": True,
    }.items():
        monkeypatch.setattr(gj.settings, name, value)
    set_client_protocol(protocol)


def _admit(variant: dict, *visuals) -> None:
    """What the source-admission worker records for each imported visual: the
    pinned pool comes first (indices 0-2), admitted media append after it."""
    variant[EDITOR_SOURCES_FIELD] = {
        "imports": {},
        "sources": [
            {
                "media_id": visual.media_id,
                "lane": "asset",
                "kind": visual.kind,
                "gcs_path": visual.gcs_path,
                "generation": visual.generation,
                "duration_s": visual.duration_s,
                "status": "ready",
                "source_index": len(_POOL) + index,
                "visual_binding": visual.model_dump(mode="json"),
            }
            for index, visual in enumerate(visuals)
        ],
    }


def _photo_block(**changes) -> dict:
    return {
        "id": "photo-layer",
        "kind": "media",
        "asset_id": pool.PHOTO_ID,
        "src_gcs_path": pool.PHOTO_PATH,
        "media_kind": "image",
        "start_s": 1.0,
        "end_s": 3.0,
        "display_mode": "overlay",
        "scale": 0.4,
    } | changes


def _video_block(**changes) -> dict:
    return {
        "id": "video-layer",
        "kind": "media",
        "asset_id": pool.VIDEO_ID,
        "src_gcs_path": pool.VIDEO_PATH,
        "media_kind": "video",
        "source_duration_s": 6.05,
        "start_s": 4.0,
        "end_s": 7.0,
        "display_mode": "fullscreen",
        "z": 1,
    } | changes


_ASSETS = {
    pool.PHOTO_ID: {"status": "ready", "gcs_path": pool.PHOTO_PATH, "kind": "image"},
    pool.VIDEO_ID: {
        "status": "ready",
        "gcs_path": pool.VIDEO_PATH,
        "kind": "video",
        "duration_s": 6.05,
    },
}


def _media_job(archetype: str = "narrated"):
    job, vid = voiceover_job(archetype=archetype)
    variant = job.assembly_plan["variants"][0]
    variant["video_path"] = "device-output.mp4"
    _admit(variant, pool.photo_visual(), pool.video_visual())
    return job, vid


def save(job, variant_id, **sections):
    variant = job.assembly_plan["variants"][0]
    return gj.prepare_editor_commit(
        job,
        variant_id,
        gj.EditorCommitRequest(base_generation=variant["render_generation_id"], **sections),
        user_id="owner",
        plan_item_id="item",
        visual_assets=_ASSETS,
    )


def _recipe(job, vid):
    return device_status(job, vid).request.recipe


def _track(recipe, track_id):
    return next((track for track in recipe.tracks if track.id == track_id), None)


# --- capabilities ---------------------------------------------------------------------


@pytest.mark.parametrize("archetype", ["narrated", "voiceover"])
def test_voiceover_edit_offers_photo_and_video_import(monkeypatch, archetype):
    _enable(monkeypatch)
    job, _vid = _media_job(archetype)
    variant = job.assembly_plan["variants"][0]

    caps = gj._editor_capabilities(job, variant)

    assert caps["phone_editor_media"] == {
        "enabled": True,
        "visual_kinds": ["image", "video"],
        "source_registration": True,
    }
    assert caps["visual_blocks"] is True
    assert caps["visual_blocks_reason"] is None
    assert caps["visual_block_kinds"] == ["media"]
    # The KRI-281 lanes stay open; nothing else does.
    assert caps["sfx"] is True and caps["overlays"] is True
    assert caps["motion_scenes"] is False and caps["camera_effects"] is False
    assert caps["visual_editor_style"] is False
    assert "clips" not in caps  # no add-clip: the cut is locked to the voiceover


@pytest.mark.parametrize(
    "mutate",
    [
        pytest.param(lambda m: set_client_protocol(_MEDIA_PROTOCOL - 1), id="old-build"),
        pytest.param(lambda m: set_client_protocol(None), id="no-header"),
        pytest.param(
            lambda m: m.setattr(gj.settings, "phone_editor_media_enabled", False), id="media-off"
        ),
        pytest.param(
            lambda m: m.setattr(gj.settings, "visual_blocks_enabled", False), id="blocks-off"
        ),
        pytest.param(
            lambda m: m.setattr(gj.settings, "phone_voiceover_editor_lanes_enabled", False),
            id="lanes-off",
        ),
        pytest.param(
            lambda m: m.setattr(
                gj.settings,
                "phone_render_verified_features",
                [f for f in _VERIFIED if f != "visualVideos"],
            ),
            id="parity-gate",
        ),
        pytest.param(
            lambda m: m.setattr(gj.settings, "phone_render_user_ids", [uuid.uuid4()]),
            id="not-a-phone-creator",
        ),
    ],
)
def test_import_stays_closed_unless_every_gate_passes(monkeypatch, mutate):
    _enable(monkeypatch)
    mutate(monkeypatch)
    job, _vid = _media_job()
    variant = job.assembly_plan["variants"][0]

    caps = gj._editor_capabilities(job, variant)

    assert "phone_editor_media" not in caps
    assert caps["visual_blocks"] is False


@pytest.mark.parametrize("state", [{"editor_timeline_mode": "authored"}, {"editor_state": "empty"}])
def test_authored_and_empty_voiceover_edits_keep_their_own_media_path(monkeypatch, state):
    """A speech edit that gained an authored timeline imports media and clips
    through the authored path; this rollout never narrows it."""
    _enable(monkeypatch)
    job, _vid = _media_job()
    variant = {**job.assembly_plan["variants"][0], **state}
    assert gj._phone_voiceover_editor_media_available(job, variant) is False


def test_pool_without_source_receipts_keeps_import_closed(monkeypatch):
    _enable(monkeypatch)
    job, _vid = _media_job()
    job.all_candidates = {"clip_paths": [*_POOL, "user/analysis-proxy-unbound.mp4"]}
    variant = job.assembly_plan["variants"][0]

    assert "phone_editor_media" not in gj._editor_capabilities(job, variant)


def test_variant_read_carries_the_registration_revision(monkeypatch):
    _enable(monkeypatch)
    job, _vid = _media_job()

    (variant,) = gj._variants_for_response(job)

    assert variant["editor_revision_number"] == 1
    assert variant["editor_capabilities"]["phone_editor_media"]["source_registration"] is True


def test_variant_read_has_no_revision_when_import_is_closed(monkeypatch):
    _enable(monkeypatch, protocol=_MEDIA_PROTOCOL - 1)
    job, _vid = _media_job()

    (variant,) = gj._variants_for_response(job)

    assert "editor_revision_number" not in variant


# --- source registration --------------------------------------------------------------


def test_registration_fences_against_the_pinned_pool(monkeypatch):
    from app.routes import editor_sources

    _enable(monkeypatch)
    job, vid = voiceover_job()
    variant = job.assembly_plan["variants"][0]
    job.content_plan_item_id = uuid.uuid4()

    editor_sources._require_available(job, variant, job.user_id, job.content_plan_item_id)
    revision = editor_sources._source_revision(job, variant)
    assert revision["revision_number"] == 1
    assert [source["gcs_path"] for source in revision["sources"]] == _POOL
    editor_sources._fence(
        job,
        variant,
        editor_sources.EditorSourceRequest(
            client_import_id=uuid.uuid4(),
            base_generation="first",
            guided_revision_number=1,
            source_kind="visual",
            source_id=pool.PHOTO_ID,
        ),
    )


def test_admission_worker_is_judged_on_server_gates_only(monkeypatch):
    """The Celery admission task runs with no app request to qualify."""
    _enable(monkeypatch)
    clear_request_context()
    job, _vid = _media_job()
    assert gj._phone_editor_media_available(job, job.assembly_plan["variants"][0]) is True


# --- Save -----------------------------------------------------------------------------


def test_narrated_save_adds_photo_and_video_and_keeps_the_cut(monkeypatch):
    _enable(monkeypatch)
    job, vid = _media_job()
    old = _recipe(job, vid)

    prep = save(job, vid, visual_blocks=[_photo_block(), _video_block()])

    assert prep["render_destination"] == "device"
    recipe = _recipe(job, vid)
    assert device_status(job, vid).request.identity.recipe_revision == 2
    for kept in ("narrated", "narration"):
        assert _track(recipe, kept) == _track(old, kept)
    assert recipe.text_layers == old.text_layers
    assert recipe.audio == old.audio
    media = _track(recipe, EDITOR_MEDIA_TRACK_ID)
    assert media.kind == "overlay"
    assert [clip.id for clip in media.clips] == [
        "editor-media-photo-layer",
        "editor-media-video-layer",
    ]
    photo, video = media.clips
    assert photo.visual_placement.width_fraction == pytest.approx(0.4)
    assert (photo.timeline_start, photo.source_duration) == (1.0, 2.0)
    assert video.visual_placement.width_fraction is None  # fullscreen
    assert (video.timeline_start, video.source_duration, video.volume) == (4.0, 3.0, 0)
    assert {"visualBlocks", "alphaOverlay", "audioMix"} <= set(recipe.required_capabilities)
    assert recipe.tracks[-1].id == EDITOR_MEDIA_TRACK_ID
    saved = job.assembly_plan["variants"][0]["visual_blocks"]
    assert [block["id"] for block in saved] == ["photo-layer", "video-layer"]


def test_reopen_serves_the_saved_media_as_native_assets(monkeypatch):
    """The app replays saved media on reopen from the timeline's `native_assets`
    (keyed `<block>:<shot>`), the same contract every other variant uses."""
    _enable(monkeypatch)
    job, vid = _media_job()
    save(job, vid, visual_blocks=[_photo_block(), _video_block()])

    assets = gj._native_editor_assets(job, vid, sign_url=lambda path, ttl: f"https://s/{path}")

    media = {row["id"]: row for row in assets if row["kind"] == "visual_block"}
    assert set(media) == {"photo-layer:photo-layer", "video-layer:video-layer"}
    assert media["photo-layer:photo-layer"]["source_url"] == f"https://s/{pool.PHOTO_PATH}"
    assert media["video-layer:video-layer"]["source_url"] == f"https://s/{pool.VIDEO_PATH}"


def test_voiceover_montage_save_adds_media_over_the_pinned_montage(monkeypatch):
    _enable(monkeypatch)
    job, vid = _media_job("voiceover")
    old = _recipe(job, vid)

    save(job, vid, visual_blocks=[_photo_block()])

    recipe = _recipe(job, vid)
    assert _track(recipe, "montage") == _track(old, "montage")
    assert len(_track(recipe, EDITOR_MEDIA_TRACK_ID).clips) == 1


def test_media_survives_lane_and_caption_saves_then_removes_cleanly(monkeypatch):
    _enable(monkeypatch)
    _fake_inspect(monkeypatch)
    job, vid = _media_job()
    save(job, vid, visual_blocks=[_photo_block()])
    media = _track(_recipe(job, vid), EDITOR_MEDIA_TRACK_ID)

    save(job, vid, sound_effects=[_sfx_payload()])
    after_lanes = _recipe(job, vid)
    assert _track(after_lanes, EDITOR_MEDIA_TRACK_ID) == media
    assert after_lanes.tracks[-1].id == EDITOR_MEDIA_TRACK_ID
    assert {"visualBlocks", "alphaOverlay"} <= set(after_lanes.required_capabilities)

    save(job, vid, caption_cues=[{"text": "Packed", "start_s": 0.0, "end_s": 2.0}])
    assert _track(_recipe(job, vid), EDITOR_MEDIA_TRACK_ID) == media

    save(job, vid, visual_blocks=[])
    cleared = _recipe(job, vid)
    assert _track(cleared, EDITOR_MEDIA_TRACK_ID) is None
    assert not {"visualBlocks", "alphaOverlay"} & set(cleared.required_capabilities)
    assert any(track.id == "sfx" for track in cleared.tracks)
    assert pool.PHOTO_ID not in {asset.id for asset in cleared.assets}


def test_save_does_not_need_the_request_header(monkeypatch):
    """Chat edits reach Save with no app request; only the server gates apply."""
    _enable(monkeypatch, protocol=None)
    job, vid = _media_job()
    save(job, vid, visual_blocks=[_photo_block()])
    assert _track(_recipe(job, vid), EDITOR_MEDIA_TRACK_ID) is not None


@pytest.mark.parametrize("archetype", ["narrated", "voiceover"])
def test_save_refuses_media_when_the_rollout_is_off(monkeypatch, archetype):
    _enable(monkeypatch)
    monkeypatch.setattr(gj.settings, "phone_editor_media_enabled", False)
    job, vid = _media_job(archetype)
    before = device_status(job, vid).request

    with pytest.raises(HTTPException) as error:
        save(job, vid, visual_blocks=[_photo_block()])

    assert error.value.status_code == 422
    assert device_status(job, vid).request == before


def test_block_past_the_voiceover_cut_is_refused(monkeypatch):
    _enable(monkeypatch)
    job, vid = _media_job()
    before = device_status(job, vid).request

    with pytest.raises(HTTPException) as error:
        save(job, vid, visual_blocks=[_photo_block(start_s=11.0, end_s=12.5)])

    assert error.value.status_code == 422
    assert device_status(job, vid).request == before


def test_unadmitted_media_is_refused(monkeypatch):
    _enable(monkeypatch)
    job, vid = voiceover_job()  # nothing admitted from the editor
    job.assembly_plan["variants"][0]["video_path"] = "device-output.mp4"

    with pytest.raises(HTTPException) as error:
        save(job, vid, visual_blocks=[_photo_block()])

    assert error.value.status_code == 422
    assert error.value.detail["code"] == "unsupported_phone_edit"
