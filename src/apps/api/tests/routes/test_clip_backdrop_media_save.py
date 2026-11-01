"""KRI-1139: the editor Save contract of a tilt backdrop of kind `media` (a pool photo or video).

Capability `clips.tilt_backdrop_media` (tilt gate + `clipBackdropMedia` verified + protocol 29), a
Save that names a pool visual the job pinned, the 422s (not in the pool / gate closed), the 426 an
older build gets on a variant that already stores one, and the pin a Save makes first so storage
retention keeps the file.
"""

from __future__ import annotations

import json
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException

import app.routes.generative_jobs as gj
import app.services.phone_visuals as phone_visuals
from app.kria.clip_tilt_row import canonical_row_tilt
from app.kria.recipes import ClipBackdrop
from app.services.client_protocol import (
    CLIP_BACKDROP_MEDIA_PROTOCOL,
    CLIP_TILT_PROTOCOL,
    clear_request_context,
    set_client_protocol,
)
from app.services.device_render import device_status
from app.services.phone_sources import PHONE_VISUALS_FIELD, PhoneVisualBinding
from app.services.phone_speaker_background import (
    BACKGROUND_REFUSALS,
    pin_background_for_commit,
)
from tests.routes.test_phone_voiceover_cut_edits import _recipe, _save, _slots, _track, _variant
from tests.routes.test_phone_voiceover_editor_lanes import _enable, voiceover_job

POOL_ID = "photo-1"
MEDIA_TILT = {"rotation_deg": 15.0, "backdrop": {"kind": "media", "asset_id": POOL_ID}}


@pytest.fixture(autouse=True)
def _reset_protocol():
    clear_request_context()
    yield
    clear_request_context()


def _open(monkeypatch, *, protocol=CLIP_BACKDROP_MEDIA_PROTOCOL, media_token=True):
    monkeypatch.setattr(gj.settings, "media_tilt_enabled", True)
    monkeypatch.setattr(
        gj.settings,
        "phone_render_verified_features",
        [
            *gj.settings.phone_render_verified_features,
            "clipTilt",
            "stillImages",
            "visualVideos",
            *(["clipBackdropMedia"] if media_token else []),
        ],
    )
    set_client_protocol(protocol)


def _capability(job, variant):
    return (gj._editor_capabilities(job, variant).get("clips") or {}).get("tilt_backdrop_media")


def test_the_capability_needs_the_tilt_gate_the_media_token_and_protocol_29(monkeypatch):
    _enable(monkeypatch)
    job, _ = voiceover_job(archetype="voiceover")
    variant = _variant(job)
    assert _capability(job, variant) is None

    _open(monkeypatch)
    assert _capability(job, variant) == {"editable": True, "reason": None}

    set_client_protocol(CLIP_TILT_PROTOCOL)
    assert _capability(job, variant) is None
    assert (gj._editor_capabilities(job, variant)["clips"]["tilt"]) == {
        "editable": True,
        "reason": None,
    }
    set_client_protocol(CLIP_BACKDROP_MEDIA_PROTOCOL)

    _open(monkeypatch, media_token=False)
    monkeypatch.setattr(
        gj.settings,
        "phone_render_verified_features",
        [f for f in gj.settings.phone_render_verified_features if f != "clipBackdropMedia"],
    )
    assert _capability(job, variant) is None

    _open(monkeypatch)
    monkeypatch.setattr(gj.settings, "media_tilt_enabled", False)
    assert _capability(job, variant) is None


def test_a_closed_media_gate_leaves_the_capability_payload_byte_identical(monkeypatch):
    _enable(monkeypatch)
    job, _ = voiceover_job(archetype="voiceover")
    _open(monkeypatch, media_token=False)
    closed = gj._editor_capabilities(job, _variant(job))
    _open(monkeypatch)
    opened = gj._editor_capabilities(job, _variant(job))
    del opened["clips"]["tilt_backdrop_media"]
    assert json.dumps(opened, sort_keys=True) == json.dumps(closed, sort_keys=True)


@pytest.mark.parametrize(
    ("archetype", "track_id"), [("narrated", "narrated"), ("voiceover", "montage")]
)
def test_a_saved_media_backdrop_reaches_the_recipe_with_its_asset_and_resets_cleanly(
    monkeypatch, archetype, track_id
):
    _enable(monkeypatch)
    _open(monkeypatch)
    job, vid = voiceover_job(archetype=archetype)
    slots = _slots(job, vid)
    slots[1]["tilt"] = dict(MEDIA_TILT)

    _save(job, vid, timeline_slots=slots)

    recipe = _recipe(job, vid)
    clip = _track(recipe, track_id).clips[1]
    assert clip.backdrop == ClipBackdrop(kind="media", asset_id=POOL_ID)
    assert clip.transform.rotation_degrees == 15.0
    assert {"clipTilt", "clipBackdropMedia"} <= recipe.required_capabilities
    manifest = {a.id: a for a in recipe.asset_manifest.assets}
    assert manifest["visual-photo-1"].visual_id == POOL_ID
    timeline = gj.dispatch_get_timeline(job, vid, sign_url=lambda path, ttl: f"https://s/{path}")
    assert timeline["slots"][1]["tilt"] == canonical_row_tilt(MEDIA_TILT)

    cleared = _slots(job, vid)
    cleared[1]["tilt"] = None
    _save(job, vid, timeline_slots=cleared)
    reset = _recipe(job, vid)
    assert "clipBackdropMedia" not in reset.required_capabilities
    assert "visual-photo-1" not in {a.id for a in reset.asset_manifest.assets}
    assert "backdrop" not in json.dumps(reset.model_dump(mode="json"))


def test_a_backdrop_the_job_never_pinned_is_refused_and_nothing_changes(monkeypatch):
    _enable(monkeypatch)
    _open(monkeypatch)
    job, vid = voiceover_job(archetype="voiceover")
    before = json.dumps(job.assembly_plan, sort_keys=True, default=str)
    slots = _slots(job, vid)
    slots[1]["tilt"] = {
        "rotation_deg": 5,
        "backdrop": {"kind": "media", "asset_id": "somebody-elses"},
    }
    with pytest.raises(HTTPException) as refused:
        _save(job, vid, timeline_slots=slots)
    assert (refused.value.status_code, refused.value.detail) == (
        422,
        {"code": "clip_backdrop_media_not_in_pool"},
    )
    assert json.dumps(job.assembly_plan, sort_keys=True, default=str) == before


def test_a_media_backdrop_needs_its_own_token_even_when_tilt_is_open(monkeypatch):
    _enable(monkeypatch)
    _open(monkeypatch, media_token=False)
    monkeypatch.setattr(
        gj.settings,
        "phone_render_verified_features",
        [f for f in gj.settings.phone_render_verified_features if f != "clipBackdropMedia"],
    )
    job, vid = voiceover_job(archetype="voiceover")
    slots = _slots(job, vid)
    slots[0]["tilt"] = {"rotation_deg": 10, "backdrop": {"kind": "blur"}}
    slots[1]["tilt"] = dict(MEDIA_TILT)
    with pytest.raises(HTTPException) as refused:
        _save(job, vid, timeline_slots=slots)
    assert refused.value.detail == {"code": "clip_backdrop_media_unavailable"}

    slots[1]["tilt"] = {"rotation_deg": 10, "backdrop": {"kind": "blur"}}
    _save(job, vid, timeline_slots=slots)
    assert _track(_recipe(job, vid), "montage").clips[1].backdrop == ClipBackdrop(kind="blur")


def test_the_kill_switch_refuses_a_new_media_backdrop_but_a_stored_one_survives_and_clears(
    monkeypatch,
):
    _enable(monkeypatch)
    _open(monkeypatch)
    job, vid = voiceover_job(archetype="voiceover")
    slots = _slots(job, vid)
    slots[1]["tilt"] = dict(MEDIA_TILT)
    _save(job, vid, timeline_slots=slots)

    monkeypatch.setattr(gj.settings, "media_tilt_enabled", False)
    changed = _slots(job, vid)
    changed[1]["tilt"] = {"rotation_deg": 30, "backdrop": {"kind": "media", "asset_id": POOL_ID}}
    with pytest.raises(HTTPException) as refused:
        _save(job, vid, timeline_slots=changed)
    assert refused.value.detail == {"code": "clip_tilt_unavailable"}

    kept = _slots(job, vid)
    kept[1]["tilt"] = canonical_row_tilt(MEDIA_TILT)
    _save(job, vid, timeline_slots=kept)
    assert _track(_recipe(job, vid), "montage").clips[1].backdrop == ClipBackdrop(
        kind="media", asset_id=POOL_ID
    )

    cleared = _slots(job, vid)
    cleared[1]["tilt"] = None
    _save(job, vid, timeline_slots=cleared)
    assert _track(_recipe(job, vid), "montage").clips[1].backdrop is None


def test_a_build_below_29_cannot_replace_the_timeline_of_a_variant_holding_a_media_backdrop(
    monkeypatch,
):
    _enable(monkeypatch)
    _open(monkeypatch)
    job, vid = voiceover_job(archetype="voiceover")
    slots = _slots(job, vid)
    slots[1]["tilt"] = dict(MEDIA_TILT)
    _save(job, vid, timeline_slots=slots)

    set_client_protocol(CLIP_TILT_PROTOCOL)
    with pytest.raises(HTTPException) as refused:
        _save(job, vid, timeline_slots=_slots(job, vid))
    assert refused.value.status_code == 426
    assert refused.value.detail == {
        "code": "client_update_required",
        "feature": "clip_backdrop_media",
        "minimum_client_protocol": 29,
    }


def test_a_guided_montage_save_declares_the_pool_photo_and_resets_it(monkeypatch):
    from tests.pipeline.test_phone_subtitled_plan import _photo_visual
    from tests.routes._editor_save_matrix import VARIANT, order_required_job
    from tests.routes.test_clip_tilt_editor_save import _guided_save

    job, variant, revision = order_required_job(monkeypatch, order_required=False)
    job.assembly_plan[PHONE_VISUALS_FIELD] = [_photo_visual().model_dump(mode="json")]
    _open(monkeypatch)
    rows = gj.editor_deletion_timeline(job, variant)
    first = rows[0]["slot_id"]

    _guided_save(job, variant, revision, rows, **{first: {"tilt": dict(MEDIA_TILT)}})

    recipe = device_status(job, VARIANT).request.recipe
    clip = next(c for t in recipe.tracks for c in t.clips if c.id == first)
    assert clip.backdrop == ClipBackdrop(kind="media", asset_id=POOL_ID)
    assert "visual-photo-1" in {a.id for a in recipe.asset_manifest.assets}
    saved = gj.editor_deletion_timeline(job, _variant(job))
    assert saved[0]["tilt"] == canonical_row_tilt(MEDIA_TILT)

    _guided_save(job, _variant(job), revision, saved, **{first: {"tilt": None}})
    reset = device_status(job, VARIANT).request.recipe
    assert "clipBackdropMedia" not in reset.required_capabilities


def test_an_authored_save_declares_the_pool_photo(monkeypatch):
    from app.services.authored_editor import prepare_authored_editor_commit
    from tests.pipeline.test_phone_subtitled_plan import _photo_visual
    from tests.routes.test_authored_playback_speed import _authored_phone_job

    job, _ = _authored_phone_job(monkeypatch)
    job.assembly_plan[PHONE_VISUALS_FIELD] = [_photo_visual().model_dump(mode="json")]
    _open(monkeypatch)
    prepare_authored_editor_commit(
        job,
        "original_text",
        gj.EditorCommitRequest(
            base_generation=gj.variant_render_baseline(job.assembly_plan["variants"][0]),
            editor_state_version=1,
            timeline_slots=[
                gj.TimelineSlotEdit(clip_index=1, in_s=2, duration_s=3, tilt=dict(MEDIA_TILT))
            ],
        ),
        validation_arguments={},
    )
    recipe = device_status(job, "original_text").request.recipe
    assert recipe.tracks[0].clips[0].backdrop == ClipBackdrop(kind="media", asset_id=POOL_ID)
    assert "visual-photo-1" in {a.id for a in recipe.assets}


# --- the pin a Save makes first ---------------------------------------------------------

ITEM_ID = uuid.uuid4()
ASSET_ID = uuid.uuid4()


def _binding(kind="image"):
    extra = (
        {"duration_s": 6.0, "width": 1080, "height": 1920, "orientation_degrees": 0}
        if kind == "video"
        else {}
    )
    return PhoneVisualBinding(
        media_id=str(ASSET_ID),
        gcs_path=f"users/u/plan/{ITEM_ID}/pool/a.jpg",
        generation="7",
        sha256="a" * 64,
        byte_count=10,
        kind=kind,
        **extra,
    )


def _db(row):
    db = MagicMock()
    result = MagicMock()
    result.scalar_one_or_none.return_value = row
    db.execute = AsyncMock(return_value=result)
    return db


def _row(**changes):
    return SimpleNamespace(
        **{
            "id": ASSET_ID,
            "status": "ready",
            "kind": "image",
            "gcs_path": f"users/u/plan/{ITEM_ID}/pool/a.jpg",
            "gcs_generation": 7,
        }
        | changes
    )


def _payload(tilt):
    slot = gj.TimelineSlotEdit(clip_index=0, in_s=0, duration_s=1, tilt=tilt)
    return gj.EditorCommitRequest(base_generation="g", timeline_slots=[slot])


def _device_job():
    return SimpleNamespace(
        id=uuid.uuid4(),
        assembly_plan={"variants": [{"variant_id": "v", "render_destination": "device"}]},
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["image", "video"])
async def test_a_save_pins_the_pool_photo_or_video_before_it_is_validated(monkeypatch, kind):
    _open(monkeypatch)
    seen = {}

    def bind(_open_session, *, job_id, pins):
        seen["pins"] = pins
        return (_binding(kind),)

    monkeypatch.setattr(phone_visuals, "bind_phone_visual_assets", bind)
    job = _device_job()
    tilt = {"rotation_deg": 5, "backdrop": {"kind": "media", "asset_id": str(ASSET_ID)}}
    await pin_background_for_commit(
        _db(_row(kind=kind)), job, "v", _payload(tilt), plan_item_id=ITEM_ID
    )
    assert seen["pins"] == {str(ASSET_ID): (kind, f"users/u/plan/{ITEM_ID}/pool/a.jpg", "7")}
    assert [r["media_id"] for r in job.assembly_plan[PHONE_VISUALS_FIELD]] == [str(ASSET_ID)]


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["missing_row", "not_ready", "other_kind", "not_a_uuid"])
async def test_a_backdrop_outside_this_items_ready_pool_is_refused_without_a_download(
    monkeypatch, case
):
    _open(monkeypatch)
    monkeypatch.setattr(
        phone_visuals,
        "bind_phone_visual_assets",
        lambda *a, **k: pytest.fail("a refused visual must not be downloaded"),
    )
    row = {
        "missing_row": None,
        "not_ready": _row(status="analyzing"),
        "other_kind": _row(kind="audio"),
        "not_a_uuid": _row(),
    }[case]
    media_id = "../../someone-else" if case == "not_a_uuid" else str(ASSET_ID)
    job = _device_job()
    with pytest.raises(HTTPException) as refused:
        await pin_background_for_commit(
            _db(row),
            job,
            "v",
            _payload({"rotation_deg": 5, "backdrop": {"kind": "media", "asset_id": media_id}}),
            plan_item_id=ITEM_ID,
        )
    assert refused.value.detail == {"code": "clip_backdrop_media_not_in_pool"}
    assert PHONE_VISUALS_FIELD not in job.assembly_plan


@pytest.mark.asyncio
async def test_a_closed_gate_or_a_cloud_variant_downloads_nothing(monkeypatch):
    monkeypatch.setattr(
        phone_visuals,
        "bind_phone_visual_assets",
        lambda *a, **k: pytest.fail("a closed gate must not download"),
    )
    tilt = {"rotation_deg": 5, "backdrop": {"kind": "media", "asset_id": str(ASSET_ID)}}
    db = _db(_row())
    await pin_background_for_commit(db, _device_job(), "v", _payload(tilt), plan_item_id=ITEM_ID)
    _open(monkeypatch)
    cloud = _device_job()
    cloud.assembly_plan["variants"][0]["render_destination"] = "cloud"
    await pin_background_for_commit(db, cloud, "v", _payload(tilt), plan_item_id=ITEM_ID)
    for blur in ({"kind": "blur"}, {"kind": "blank"}):
        await pin_background_for_commit(
            db,
            _device_job(),
            "v",
            _payload({"rotation_deg": 5, "backdrop": blur}),
            plan_item_id=ITEM_ID,
        )
    db.execute.assert_not_called()


def test_every_refusal_a_backdrop_save_raises_has_creator_words_in_both_languages():
    for code in (
        "clip_backdrop_media_unavailable",
        "clip_backdrop_media_not_in_pool",
        "clip_backdrop_media_unreadable",
    ):
        english, turkish = BACKGROUND_REFUSALS[code]
        assert english.startswith("Couldn't") and turkish
