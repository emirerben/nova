"""A photo timing edit keeps the confirmed order through the real guided phone Save."""

from __future__ import annotations

import copy
import hashlib
import uuid
from types import SimpleNamespace

import pytest

from app.config import settings
from app.kria.device_render import make_device_request
from app.kria.media_sources import OriginalMediaDescriptor
from app.pipeline.guided_story import GuidedStoryExecutionPlan, compile_execution_plan
from app.pipeline.phone_guided_plan import compile_phone_guided_plan
from app.routes import generative_jobs as gj
from app.schemas.edit_proposal import (
    EditProposalSnapshot,
    FastMontageCut,
    MediaRef,
    StoryBeat,
    canonical_media_digest,
)
from app.services.creator_render_contract import (
    CONTRACT_FIELD,
    CreatorRenderContractError,
    build_render_contract,
    read_render_contract,
    verify_phone_recipe,
)
from app.services.device_render import CONTRACT_REVISIONS_FIELD, device_status, pin_device_request
from app.services.kria_editor_ops import build_editor_snapshot, compile_editor_ops
from app.services.phone_sources import (
    PHONE_SOURCES_FIELD,
    PHONE_VISUALS_FIELD,
    PhoneSourceBinding,
    PhoneVisualBinding,
)
from tests.services.test_photo_timeline_duration import _photo_chat_turn


def mixed_montage(
    monkeypatch,
    *,
    with_overlay=False,
    overlay_end=4.0,
    transition=False,
    contract=False,
    labels=False,
    song=False,
    prod=False,
    order_required=True,
):
    if prod:
        from tests._prod_profile import apply_prod_profile

        apply_prod_profile(monkeypatch)
    for name in (
        "phone_rendering_enabled",
        "guided_story_editor_v2_enabled",
        "kria_guided_timeline_ops",
        "edit_transitions_enabled",
        "phone_editor_media_enabled",
        "visual_blocks_enabled",
    ):
        monkeypatch.setattr(settings, name, True)
    monkeypatch.setattr(
        settings,
        "phone_render_verified_features",
        [
            "basicComposition",
            "local1080Export",
            "positionedText",
            "animatedText",
            "audioMix",
            "stillImages",
            "visualVideos",
            "authoredText",
            "crossfade",
            "visualBlocks",
            "alphaOverlay",
            "musicBed",
        ],
    )
    job_id, user_id, item_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    video_refs = [
        MediaRef(
            lane="clip",
            media_id=f"v{i}",
            gcs_path=f"generative-jobs/{job_id}/sources/analysis-proxy-v{i}.mp4",
            generation="123",
            kind="video",
            duration_s=10,
        )
        for i in range(12)
    ]
    image_refs = [
        MediaRef(
            lane="asset",
            media_id=f"p{i}",
            gcs_path=f"users/{user_id}/plan/{item_id}/pool/p{i}.jpg",
            generation="77",
            kind="image",
        )
        for i in range(13)
    ]
    refs = [*video_refs, *image_refs]
    ordered = [ref for i in range(12) for ref in (video_refs[i], image_refs[i])] + [image_refs[-1]]
    cuts = []
    for i, ref in enumerate(ordered):
        duration = (4.46 if ref.media_id != "v11" else 4.5) if ref.kind == "video" else 1.0
        source_start = 1.0 if ref.kind == "video" else 0.0
        cuts.append(
            FastMontageCut(
                cut_id=f"cut-{i}",
                media_id=ref.media_id,
                source_start_s=source_start,
                source_end_s=source_start + duration,
                output_duration_s=duration,
                role="hook" if i == 0 else "build",
            )
        )
    duration = sum(c.output_duration_s for c in cuts)
    extra = {}
    if labels:
        from app.schemas.edit_proposal import ClipLabel

        extra["clip_labels"] = [
            ClipLabel(
                media_id=m.media_id, text=f"Photo {i}", provenance="creator", min_display_s=1.0
            )
            for i, m in enumerate(image_refs)
        ]
    bed = None
    if song:
        from app.schemas.user_song import UserSongPlan
        from tests.pipeline.user_song_helpers import song_bed

        extra["user_song"] = UserSongPlan(
            mode="background",
            plan_item_id=str(item_id),
            generation=77,
            duration_s=100,
            window_start_s=5,
            window_end_s=5 + duration,
        )
        bed = song_bed(plan_item_id=str(item_id), generation="77", duration_s=100)
    snapshot = EditProposalSnapshot(
        direction="fast_montage",
        duration_s=round(duration, 3),
        title="Synthetic montage",
        opening_title="Match day",
        video_reuse_policy="once",
        media=refs,
        fast_cuts=cuts,
        story_beats=[
            StoryBeat(
                beat_id="story",
                topic="Scene",
                media_ids=[ref.media_id for ref in ordered[:4]],
                duration_s=round(duration, 3),
            )
        ],
        **extra,
    )
    guided = {
        "proposal_version": 1,
        "media_digest": canonical_media_digest(refs),
        "approved_proposal": snapshot.model_dump(mode="json"),
        "media_identities": [
            {k: getattr(m, k) for k in ("lane", "media_id", "gcs_path", "generation", "kind")}
            for m in refs
        ],
    }
    plan = compile_execution_plan(guided, track=None)
    bindings = tuple(
        PhoneSourceBinding(
            media_id=m.media_id,
            proxy_path=m.gcs_path,
            generation=m.generation,
            original=OriginalMediaDescriptor(
                sha256=hashlib.sha256(m.media_id.encode()).hexdigest(),
                byte_count=1000,
                duration_s=10,
                width=1920,
                height=1080,
                has_audio=True,
            ),
        )
        for m in video_refs
    )
    visuals = tuple(
        PhoneVisualBinding(
            media_id=m.media_id,
            gcs_path=m.gcs_path,
            generation=m.generation,
            sha256=hashlib.sha256(m.media_id.encode()).hexdigest(),
            byte_count=1000,
        )
        for m in image_refs
    )
    variant = {
        "variant_id": "guided_story",
        "resolved_archetype": "guided_story",
        "render_destination": "device",
        "render_status": "awaiting_device",
        "render_generation_id": "first",
        "duration_s": plan["resolved_duration_s"],
        "text_mode": "agent_text",
        "intro_mode": "linear",
        "intro_layout": "linear",
        "text_elements": copy.deepcopy(plan["text_elements"]),
    }
    job = SimpleNamespace(
        id=job_id,
        user_id=user_id,
        status="awaiting_device",
        current_phase=None,
        mode="content_plan",
        all_candidates={"clip_paths": [m.gcs_path for m in video_refs]},
        assembly_plan={
            "guided_edit": guided,
            "guided_story_execution_plan": plan,
            PHONE_SOURCES_FIELD: [b.model_dump(mode="json") for b in bindings],
            PHONE_VISUALS_FIELD: [v.model_dump(mode="json") for v in visuals],
            "variants": [variant],
        },
    )
    if contract:
        order = tuple(
            f"asset-{m['media_id']}" if m["lane"] == "asset" else m["media_id"]
            for m in plan["story_timeline"]
        )
        bound = build_render_contract({}, generation_id="first", clip_order=order)
        job.assembly_plan[CONTRACT_FIELD] = bound.rebind(
            duration_s=duration,
            order_ids=order,
            order_required=order_required,
            order_basis="editor",
        ).model_dump(mode="json")
    pin_device_request(
        job,
        make_device_request(
            job_id=job.id,
            variant_id="guided_story",
            revision=1,
            recipe=compile_phone_guided_plan(
                GuidedStoryExecutionPlan.model_validate(plan), bindings, visuals=visuals, song=bed
            ),
        ),
        base_generation="first",
    )
    variant = job.assembly_plan["variants"][0]
    revision = gj._guided_v2_revision(job, variant)
    if with_overlay:
        p = image_refs[0]
        assets = {p.media_id: {"status": "ready", "gcs_path": p.gcs_path, "kind": "image"}}
        gj.prepare_editor_commit(
            job,
            "guided_story",
            gj.EditorCommitRequest(
                base_generation=variant["render_generation_id"],
                guided_revision_number=revision["revision_number"],
                visual_blocks=[
                    {
                        "id": "overlay",
                        "kind": "media",
                        "asset_id": p.media_id,
                        "src_gcs_path": p.gcs_path,
                        "media_kind": "image",
                        "start_s": overlay_end - 1.0,
                        "end_s": overlay_end,
                        "display_mode": "overlay",
                        "scale": 0.4,
                    }
                ],
            ),
            visual_assets=assets,
        )
        variant = job.assembly_plan["variants"][0]
        revision = gj._guided_v2_revision(job, variant)
    if transition:
        compiled = compile_editor_ops(
            job,
            variant,
            [
                {
                    "op": "patch_slots",
                    "selector": {"all": True},
                    "patch": {"transition_after": "crossfade", "transition_duration_s": 0.3},
                }
            ],
        )
        gj.prepare_editor_commit(job, "guided_story", compiled.payload)
        variant = job.assembly_plan["variants"][0]
        revision = gj._guided_v2_revision(job, variant)
    return job, variant, revision


@pytest.mark.parametrize(
    "options",
    [
        {},
        {"with_overlay": True},
        {"with_overlay": True, "overlay_end": 62.0},
        {"transition": True},
        {"contract": True},
        {"labels": True},
        {"labels": True, "contract": True},
        {"song": True, "contract": True},
        {"prod": True, "contract": True},
        {"contract": True, "order_required": False},
        {"contract": True, "with_overlay": True},
    ],
    ids=[
        "legacy-no-contract",
        "overlay",
        "late-overlay",
        "crossfades",
        "confirmed-order",
        "labels",
        "labels-confirmed-order",
        "song-confirmed-order",
        "recorded-prod-flags",
        "contract-without-original-order",
        "overlay-confirmed-order",
    ],
)
async def test_mixed_photo_chat_save_stage(monkeypatch, options):
    job, variant, revision = mixed_montage(monkeypatch, **options)
    before = build_editor_snapshot(job, variant)
    assert len(before["slots"]) == 25
    assert sum(row["media_kind"] == "image" for row in before["slots"]) == 13
    old_recipe = device_status(job, "guided_story").request.recipe
    old_order = _recipe_order(old_recipe)
    response = await _photo_chat_turn(
        monkeypatch,
        (job, variant, revision),
        "Make the photos 0.3 seconds",
        [{"op": "set_total_duration", "target_s": 0.3, "strategy": "proportional"}],
    )
    assert response.outcome == "proposed", response
    compiled = compile_editor_ops(job, variant, response.ops)
    old = copy.deepcopy(job.assembly_plan)
    prep = gj.prepare_editor_commit(job, "guided_story", compiled.payload)
    recipe = device_status(job, "guided_story").request.recipe
    after = build_editor_snapshot(job, job.assembly_plan["variants"][0])
    assert prep["render_destination"] == "device"
    assert _recipe_order(recipe) == old_order
    assert job.assembly_plan != old
    assert recipe.duration == pytest.approx(57.533333, abs=1 / 30)
    if options.get("contract"):
        rebound = read_render_contract(
            {CONTRACT_FIELD: job.assembly_plan[CONTRACT_REVISIONS_FIELD]["guided_story"]}
        )
        assert rebound.order_required
        assert tuple(rebound.order_ids) == old_order
        assert rebound.generation_id == prep["generation"]
        # A genuinely wrong order is still refused; timing and duration alone cannot pass.
        wrong = recipe.model_copy(deep=True)
        video = next(track for track in wrong.tracks if track.kind == "video")
        video.clips[0].source_asset_id, video.clips[1].source_asset_id = (
            video.clips[1].source_asset_id,
            video.clips[0].source_asset_id,
        )
        with pytest.raises(CreatorRenderContractError, match="confirmed clip order"):
            verify_phone_recipe(rebound, wrong)
    if options.get("with_overlay"):
        assert any(track.kind == "overlay" and track.clips for track in recipe.tracks)
    if options.get("song"):
        old_song = next(track for track in old_recipe.tracks if track.id == "song").clips[0]
        new_song = next(track for track in recipe.tracks if track.id == "song").clips[0]
        assert new_song.source_asset_id == old_song.source_asset_id
        assert new_song.source_start == old_song.source_start
        assert new_song.source_duration == pytest.approx(recipe.duration, abs=1 / 30)
    assert len(after["slots"]) == len(before["slots"])
    assert [r["duration_s"] for r in after["slots"] if r["media_kind"] == "image"] == pytest.approx(
        [0.3] * 13
    )
    assert [r["duration_s"] for r in after["slots"] if r["media_kind"] == "video"] == pytest.approx(
        [r["duration_s"] for r in before["slots"] if r["media_kind"] == "video"], abs=1 / 30
    )
    if options.get("labels"):
        assert {"".join(run.text for run in layer.runs) for layer in old_recipe.text_layers} == {
            "".join(run.text for run in layer.runs) for layer in recipe.text_layers
        }


async def test_mixed_photo_reorder_rebinds_the_new_order(monkeypatch):
    job, variant, revision = mixed_montage(monkeypatch, contract=True)
    old_order = _recipe_order(device_status(job, "guided_story").request.recipe)
    response = await _photo_chat_turn(
        monkeypatch,
        (job, variant, revision),
        "Make the photos 0.3 seconds and move the second clip first",
        [
            {
                "op": "set_media_duration",
                "selector": {"scope": "timeline", "media_kind": "image", "quantifier": "all"},
                "duration_s": 0.3,
            },
            {"op": "reorder_clip", "from_index": 1, "to_index": 0},
        ],
    )
    assert response.outcome == "proposed", response
    compiled = compile_editor_ops(job, variant, response.ops)
    prep = gj.prepare_editor_commit(job, "guided_story", compiled.payload)
    recipe = device_status(job, "guided_story").request.recipe
    rebound = read_render_contract(
        {CONTRACT_FIELD: job.assembly_plan[CONTRACT_REVISIONS_FIELD]["guided_story"]}
    )
    expected = (old_order[1], old_order[0], *old_order[2:])
    assert _recipe_order(recipe) == expected
    assert rebound.order_ids == expected
    assert rebound.generation_id == prep["generation"]
    assert rebound.order_required


def _recipe_order(recipe):
    from app.kria.render_assets import OriginalRenderAsset, VisualRenderAsset

    assets = {asset.id: asset for asset in recipe.asset_manifest.assets}
    result = []
    for track in recipe.tracks:
        if track.kind != "video":
            continue
        for clip in track.clips:
            asset = assets[clip.source_asset_id]
            assert isinstance(asset, (OriginalRenderAsset, VisualRenderAsset))
            identity = (
                asset.media_id
                if isinstance(asset, OriginalRenderAsset)
                else f"asset-{asset.visual_id}"
            )
            if not result or result[-1] != identity:
                result.append(identity)
    return tuple(result)


async def test_authored_mixed_photo_order_commits(monkeypatch):
    from app.services.authored_editor import prepare_authored_editor_commit
    from app.services.editor_empty_drafts import stage_initial_authored_baseline

    job, variant, revision = mixed_montage(monkeypatch, contract=True)
    old_order = _recipe_order(device_status(job, "guided_story").request.recipe)
    response = await _photo_chat_turn(
        monkeypatch,
        (job, variant, revision),
        "Make the photos 0.3 seconds",
        [{"op": "set_total_duration", "target_s": 0.3, "strategy": "proportional"}],
    )
    assert response.outcome == "proposed"
    compiled = compile_editor_ops(job, variant, response.ops)
    baseline = stage_initial_authored_baseline(
        job, "guided_story", gj.editor_deletion_timeline(job, variant)
    )
    payload = compiled.payload.model_copy(update={"editor_state_version": 1})
    prep = prepare_authored_editor_commit(
        baseline, "guided_story", payload, validation_arguments={}
    )
    recipe = device_status(baseline, "guided_story").request.recipe
    assert prep["render_destination"] == "device"
    assert _recipe_order(recipe) == old_order
    rebound = read_render_contract(
        {CONTRACT_FIELD: baseline.assembly_plan[CONTRACT_REVISIONS_FIELD]["guided_story"]}
    )
    assert rebound.order_ids == old_order
    assert rebound.order_required
    assert [
        clip.source_duration
        for clip in recipe.tracks[0].clips
        if clip.source_asset_id.startswith("visual-")
    ] == pytest.approx([0.3] * 13)
