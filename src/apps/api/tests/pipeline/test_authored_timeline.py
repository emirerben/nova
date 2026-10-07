import copy
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.kria.media_sources import OriginalMediaDescriptor
from app.pipeline.authored_timeline import render_presentation
from app.pipeline.phone_authored_timeline import compile_phone_authored_timeline
from app.pipeline.phone_subtitled_plan import compile_phone_subtitled_plan
from app.routes.generative_jobs import TimelineSlotEdit
from app.services.authored_editor import explicit_authored_slots, resolve_authored_phone_slots
from app.services.creator_render_contract import doubled_soundtrack_assets
from app.services.phone_editor_sources import EDITOR_SOURCES_FIELD, phone_editor_source_revision
from app.services.phone_sources import PHONE_SOURCES_FIELD, PhoneSourceBinding


def binding(media_id):
    return PhoneSourceBinding(
        media_id=media_id,
        proxy_path=f"users/owner/analysis-proxy-{media_id}.mp4",
        generation="1",
        original=OriginalMediaDescriptor(
            sha256=("a" if media_id == "old" else "b") * 64,
            byte_count=1000,
            duration_s=12,
            width=1080,
            height=1920,
            has_audio=True,
        ),
    )


def fixture():
    old, new = binding("old"), binding("new")
    variant = {
        "variant_id": "original_text",
        "resolved_archetype": "subtitled",
        "render_destination": "device",
        "editor_timeline_mode": "authored",
        "caption_cues": [],
        "text_elements": [],
        "text_elements_user_edited": True,
        EDITOR_SOURCES_FIELD: {
            "sources": [
                {
                    "media_id": "new",
                    "lane": "clip",
                    "gcs_path": new.proxy_path,
                    "generation": "1",
                    "kind": "video",
                    "duration_s": 12,
                    "source_index": 1,
                    "status": "ready",
                    "source_binding": new.model_dump(mode="json"),
                }
            ]
        },
        "user_timeline": {
            "slots": [{"slot_id": "added", "clip_index": 1, "in_s": 2, "duration_s": 3}]
        },
    }
    job = SimpleNamespace(
        assembly_plan={PHONE_SOURCES_FIELD: [old.model_dump(mode="json")]},
        all_candidates={"clip_paths": [old.proxy_path]},
    )
    return job, variant, compile_phone_subtitled_plan((old,), caption_cues=[])


def test_empty_never_falls_back_to_ai_or_old_recipe():
    with pytest.raises(ValueError, match="empty"):
        explicit_authored_slots(
            {"user_timeline": {"slots": []}, "ai_timeline": {"slots": [{"clip_index": 0}]}}
        )


def test_new_phone_source_replaces_old_speaker_without_cloud_paths():
    job, variant, previous = fixture()
    recipe = compile_phone_authored_timeline(job, variant, previous)
    clips = [clip for track in recipe.tracks if track.kind == "video" for clip in track.clips]
    assert [(clip.source_asset_id, clip.source_start, clip.source_duration) for clip in clips] == [
        ("new", 2, 3)
    ]
    assert {asset.id for asset in recipe.asset_manifest.assets} == {"new"}
    assert recipe.duration == 3
    assert "analysis-proxy" not in recipe.model_dump_json()
    assert "old" not in recipe.model_dump_json()


def test_phone_source_catalog_is_stable_and_does_not_mutate_job_pool():
    job, variant, _ = fixture()
    before = copy.deepcopy(job.all_candidates)
    revision = phone_editor_source_revision(job, variant)
    assert revision["revision_number"] == 1
    assert [row["media_id"] for row in revision["sources"]] == ["old", "new"]
    assert job.all_candidates == before


def test_phone_resolver_rejects_unknown_source_and_out_of_bounds():
    job, variant, _ = fixture()
    for slot in [
        TimelineSlotEdit(clip_index=2, in_s=0, duration_s=3),
        TimelineSlotEdit(clip_index=1, in_s=11, duration_s=3),
    ]:
        with pytest.raises(HTTPException):
            resolve_authored_phone_slots(job, variant, [slot])
    result = resolve_authored_phone_slots(
        job, variant, [TimelineSlotEdit(clip_index=1, in_s=2, duration_s=3)]
    )
    assert result[0]["media_id"] == "new"


def test_presentation_clamps_without_erasing_saved_layers_or_reusing_pixels():
    variant = {
        "duration_s": 12,
        "subject_matte_path": "old",
        "caption_cues": [
            {"id": "keep", "start_s": 1, "end_s": 7},
            {"id": "later", "start_s": 9, "end_s": 10},
        ],
    }
    rendered = render_presentation(variant, 3)
    assert rendered["caption_cues"] == [{"id": "keep", "start_s": 1, "end_s": 3}]
    assert rendered["subject_matte_path"] is None
    assert len(variant["caption_cues"]) == 2
    assert variant["caption_cues"][0]["end_s"] == 7


def test_phone_restore_uses_saved_captions_and_keeps_their_authoritative_snapshot():
    job, variant, previous = fixture()
    variant["caption_cues"] = [{"text": "Saved words", "start_s": 0, "end_s": 6}]
    recipe = compile_phone_authored_timeline(job, variant, previous)
    assert recipe.text_layers
    assert all(layer.end <= 3 for layer in recipe.text_layers)
    assert variant["caption_cues"][0]["end_s"] == 6


@pytest.mark.parametrize("archetype", ["narrated", "subtitled"])
def test_phone_restore_never_compiles_caption_cue_mirrors_as_text(archetype):
    """The authored commit snapshots the merged editor lane, caption-cue mirrors
    included; only `compile_caption_layers` may draw those sentences."""
    from app.agents._schemas.text_element import (
        CAPTION_CUE_SOURCE,
        merge_projected_text_elements_for_variant,
    )

    job, variant, previous = fixture()
    variant["resolved_archetype"] = archetype
    variant["text_mode"] = "agent_text"
    variant["caption_cues"] = [
        {"text": "First sentence", "start_s": 0.0, "end_s": 1.2},
        {"text": "Second sentence", "start_s": 1.2, "end_s": 2.5},
    ]
    mirrors = merge_projected_text_elements_for_variant(variant) or []
    assert [row["source_params"]["source"] for row in mirrors] == [CAPTION_CUE_SOURCE] * 2
    title = {
        "id": "title",
        "text": "TITLE",
        "start_s": 0.0,
        "end_s": 2.0,
        "role": "generative_intro",
    }
    variant["text_elements"] = [title, *mirrors]

    recipe = compile_phone_authored_timeline(job, variant, previous)

    ids = [layer.id for layer in recipe.text_layers]
    authored = [layer_id for layer_id in ids if layer_id.startswith("authored-text-")]
    captions = [layer_id for layer_id in ids if not layer_id.startswith("authored-text-")]
    assert authored == ["authored-text-0"]
    assert len(captions) == 2


def test_phone_restore_keeps_saved_captions_disabled():
    job, variant, previous = fixture()
    variant["caption_cues"] = [{"text": "Saved words", "start_s": 0, "end_s": 3}]
    variant["captions_enabled"] = False
    recipe = compile_phone_authored_timeline(job, variant, previous)
    assert recipe.text_layers == []
    assert variant["caption_cues"][0]["text"] == "Saved words"


@pytest.mark.parametrize("music", [False, True])
def test_cloud_orchestration_never_downloads_deleted_clips(monkeypatch, tmp_path, music):
    import uuid
    from contextlib import contextmanager
    from unittest.mock import Mock

    from app.pipeline import authored_timeline as authored
    from app.pipeline import probe
    from app.tasks import generative_build as gb
    from app.tasks import template_orchestrate as template

    job_id = uuid.uuid4()
    variant = {
        "variant_id": "original_text",
        "resolved_archetype": "subtitled",
        "editor_timeline_mode": "authored",
        "render_generation_id": "new-gen",
        "duration_s": 12,
        "text_elements": [],
        "caption_cues": [],
        "user_timeline": {
            "slots": [{"slot_id": "new", "clip_index": 1, "in_s": 2, "duration_s": 3}]
        },
        "ai_timeline": {"slots": [{"clip_index": 0, "in_s": 0, "duration_s": 12}]},
    }
    job = SimpleNamespace(
        assembly_plan={"variants": [variant]},
        all_candidates={"clip_paths": ["deleted.mp4", "added.mp4"]},
    )

    if music:
        variant.update(
            music_track_id=str(uuid.uuid4()), resolved_archetype="guided_story", mix=0.25
        )

    @contextmanager
    def session():
        yield SimpleNamespace(
            get=lambda model, _: (
                job if model is authored.Job else SimpleNamespace(audio_gcs_path="music/bed")
            )
        )

    monkeypatch.setattr(gb, "_sync_session", session)
    writes = []
    monkeypatch.setattr(
        gb, "_update_variant_entry", lambda *args, **kwargs: writes.append(args[2]) or True
    )
    prepare = Mock(
        return_value={
            "steps": [object()],
            "clip_id_to_local": {"clip_1": "added-local"},
            "probe_map": {},
        }
    )
    monkeypatch.setattr(authored, "prepare_authored_assembly", prepare)
    mix_audio = Mock()
    monkeypatch.setattr(template, "_mix_template_audio", mix_audio)
    assemble = Mock()
    monkeypatch.setattr(template, "_assemble_clips", assemble)
    monkeypatch.setattr(probe, "probe_video", lambda _: SimpleNamespace(duration_s=3))
    monkeypatch.setattr(
        authored.storage, "upload_public_read", lambda _, path: f"https://example.test/{path}"
    )
    monkeypatch.setattr(
        gb,
        "_ensure_creator_layer_base",
        lambda **kwargs: (kwargs["base_gcs_path"], None, None, None, {}),
    )
    compose = Mock(return_value=(str(tmp_path / "final.mp4"), None))
    monkeypatch.setattr(gb, "_compose_subtitled_final", compose)
    monkeypatch.setattr(gb, "_will_reapply_media_layers", lambda _: False)
    authored.rerender_authored_timeline(str(job_id), "original_text", "new-gen")
    assert prepare.call_args.args[0] == variant["user_timeline"]["slots"]
    assert assemble.call_args.args[1] == {"clip_1": "added-local"}
    assert writes[-1]["render_status"] == "ready"
    assert writes[-1]["duration_s"] == 3
    assert "user_timeline" not in writes[-1]  # stored authored state is not replaced
    assert compose.call_args.args[1]["caption_cues"] == []
    if music:
        assert mix_audio.call_args.kwargs["force_video_duration"] is True
        assert mix_audio.call_args.kwargs["audio_gain"] == 0.25


@pytest.mark.parametrize(
    "rate, layout, crop",
    [
        (1, "fullscreen", None),
        (0.5, "fullscreen", {"x": 0.25, "y": 0.1, "width": 0.5, "height": 0.8}),
        (2, "supporting_card", None),
    ],
)
def test_authored_ffmpeg_assembly_uses_only_selected_window(
    monkeypatch, tmp_path, rate, layout, crop
):
    import shutil
    import subprocess
    from pathlib import Path

    from app.pipeline.probe import probe_video
    from app.tasks import template_orchestrate as template

    if shutil.which("ffmpeg") is None:
        pytest.skip("ffmpeg unavailable")
    source = tmp_path / "source.mp4"
    subprocess.run(
        [
            "ffmpeg",
            "-nostdin",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "color=c=red:s=96x160:r=30:d=2",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:duration=2",
            "-c:v",
            "libx264",
            "-preset",
            "fast",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            str(source),
        ],
        capture_output=True,
        check=True,
        timeout=30,
    )
    from unittest.mock import Mock

    from app.pipeline import guided_story

    render_occurrence = Mock(wraps=guided_story._render_video_moment)
    monkeypatch.setattr(guided_story, "_render_video_moment", render_occurrence)
    downloaded = []

    def download(paths, _):
        downloaded.extend(paths)
        return [str(source) for path in paths]

    monkeypatch.setattr(template, "_download_clips_parallel", download)
    from app.pipeline.authored_timeline import prepare_authored_assembly
    from app.pipeline.canvas import Canvas

    assembled = prepare_authored_assembly(
        [
            {
                "clip_index": 0,
                "source_gcs_path": "chosen-source",
                "in_s": 0.2,
                "duration_s": 0.8,
                "playback_rate": rate,
                "layout": layout,
                "source_crop": crop,
            },
        ],
        ["must-not-download", "chosen-source"],
        str(tmp_path),
        job_id="authored-smoke",
        canvas=Canvas(width=96, height=160),
    )
    out = tmp_path / "output.mp4"
    from app.pipeline.canvas import Canvas

    template._assemble_clips(
        assembled["steps"],
        assembled["clip_id_to_local"],
        assembled["probe_map"],
        str(out),
        str(tmp_path),
        clip_metas=[],
        is_agentic=True,
        allow_slowdown_fill=False,
        canvas=Canvas(width=96, height=160),
    )
    result = probe_video(str(out))
    assert downloaded == ["chosen-source"]
    assert render_occurrence.call_args.kwargs["playback_rate"] == rate
    assert render_occurrence.call_args.kwargs["source_crop"] == crop
    assert render_occurrence.call_args.kwargs["layout"] == layout
    assert render_occurrence.call_args.kwargs["end_s"] == pytest.approx(0.2 + 0.8 * rate)
    assert result.duration_s == pytest.approx(0.8, abs=0.12)
    assert result.has_audio
    assert Path(out).stat().st_size > 0


def test_phone_saved_overlay_and_sfx_survive_restore_without_old_ending_clip():
    from app.pipeline.phone_subtitled_lanes import PhoneSubtitledLanes
    from app.services.phone_sources import PHONE_VISUALS_FIELD
    from app.services.phone_subtitled_editor import sections_from_lanes
    from tests.pipeline.test_phone_subtitled_plan import _overlay_card, _photo_visual, _resolved_sfx

    job, variant, _ = fixture()
    photo = _photo_visual()
    lanes = PhoneSubtitledLanes(overlays=[_overlay_card()], sound_effects=[_resolved_sfx()])
    old = binding("old")
    previous = compile_phone_subtitled_plan((old,), caption_cues=[], visuals=(photo,), lanes=lanes)
    job.assembly_plan[PHONE_VISUALS_FIELD] = [photo.model_dump(mode="json")]
    sections = sections_from_lanes(lanes, labels={}, paths={"pop": "sound-effects/pop/audio.mp3"})
    variant["sound_effects"] = sections["sound_effects"]
    variant["media_overlays"] = sections["media_overlays"]
    recipe = compile_phone_authored_timeline(job, variant, previous)
    assert any(track.kind == "overlay" and track.clips for track in recipe.tracks)
    assert any(track.id == "sfx" and track.clips for track in recipe.tracks)
    assert [
        clip.source_asset_id
        for track in recipe.tracks
        if track.kind == "video"
        for clip in track.clips
    ] == ["new"]
    # Explicitly deleting either independent lane must not revive the pinned lane.
    variant["sound_effects"] = []
    variant["media_overlays"] = []
    cleared = compile_phone_authored_timeline(job, variant, previous)
    assert not any(track.kind in {"overlay", "audio"} for track in cleared.tracks)


@pytest.mark.parametrize("saved_original", [None, 0.3])
def test_phone_restore_keeps_pinned_independent_narration(saved_original):
    from app.kria.recipes import AssetFingerprint, MediaAsset, TimelineClip, TimelineTrack
    from app.kria.recipes_v2 import EditRecipeV2
    from app.kria.render_assets import RenderFingerprint, VoiceoverRenderAsset

    job, variant, previous = fixture()
    voice = VoiceoverRenderAsset(
        id="voice",
        plan_item_id="item",
        generation="3",
        fingerprint=RenderFingerprint(sha256="c" * 64, byte_count=123),
    )
    raw = previous.model_dump(mode="json")
    raw["assets"].append(
        MediaAsset(
            id="voice",
            relative_path="voice",
            fingerprint=AssetFingerprint(hex="c" * 64, byte_count=123),
            duration=10,
        ).model_dump(mode="json")
    )
    raw["asset_manifest"]["assets"].append(voice.model_dump(mode="json"))
    raw["tracks"].append(
        TimelineTrack(
            id="narration",
            kind="audio",
            clips=[
                TimelineClip(
                    id="voice",
                    source_asset_id="voice",
                    source_start=0,
                    source_duration=10,
                    timeline_start=0,
                    rate=1,
                )
            ],
        ).model_dump(mode="json")
    )
    raw["audio"]["target_lufs"] = -16
    previous = EditRecipeV2.model_validate(raw)
    variant["original_audio_level"] = saved_original
    recipe = compile_phone_authored_timeline(job, variant, previous)
    track = next(track for track in recipe.tracks if track.id == "narration")
    assert track.clips[0].source_duration == 3
    assert recipe.audio.narration_asset_id == "voice"
    assert recipe.audio.original_volume == (saved_original or 0)
    assert recipe.audio.target_lufs == -16
    assert any(asset.id == "voice" for asset in recipe.asset_manifest.assets)


@pytest.mark.parametrize("treatment_key", ["background_music_treatment", "smart_music_treatment"])
def test_phone_restore_keeps_independent_music_treatment(treatment_key):
    from app.kria.recipes import AssetFingerprint, MediaAsset, TimelineClip, TimelineTrack
    from app.kria.render_assets import LibraryRenderAsset, RenderFingerprint

    job, variant, previous = fixture()
    previous.assets.append(
        MediaAsset(
            id="bed",
            relative_path="bed",
            duration=10,
            fingerprint=AssetFingerprint(hex="c" * 64, byte_count=123),
        )
    )
    previous.asset_manifest = previous.asset_manifest.model_copy(
        update={
            "assets": (
                *previous.asset_manifest.assets,
                LibraryRenderAsset(
                    id="bed",
                    catalog="music",
                    catalog_id="track",
                    generation="1",
                    fingerprint=RenderFingerprint(sha256="c" * 64, byte_count=123),
                ),
            )
        }
    )
    previous.tracks.append(
        TimelineTrack(
            id="bed",
            kind="audio",
            clips=[
                TimelineClip(
                    id="bed",
                    source_asset_id="bed",
                    rate=1,
                    source_start=0,
                    source_duration=10,
                    timeline_start=0,
                )
            ],
        )
    )
    variant[treatment_key] = {"track_id": "track"}
    variant["mix"] = 0.2
    recipe = compile_phone_authored_timeline(job, variant, previous)
    clip = next(track for track in recipe.tracks if track.id == "bed").clips[0]
    assert clip.source_duration == 3
    # KRI-470 PR-G: the bed plays through its track clip, at the creator's mix. Naming the
    # asset in `audio.music_asset_id` too would make the device play it a second time from
    # source 0 (the KRI-481 double play), which the contract verifier now refuses.
    assert clip.volume == 0.2
    assert recipe.audio.music_asset_id is None
    assert doubled_soundtrack_assets(recipe) == []


def test_authored_prepare_runs_normal_validation_and_stamps_new_generation(monkeypatch):
    from app.routes import generative_jobs as gj
    from app.services.authored_editor import prepare_authored_editor_commit
    from tests.routes.test_editor_commit import _arm, _job

    _arm(monkeypatch)
    job = _job(resolved_archetype="narrated")
    prior = copy.deepcopy(job.assembly_plan)
    result = prepare_authored_editor_commit(
        job,
        "song_text",
        gj.EditorCommitRequest(
            base_generation="2026-07-01T00:00:00Z",
            editor_state_version=1,
            timeline_slots=[gj.TimelineSlotEdit(clip_index=2, in_s=0, duration_s=1)],
        ),
        validation_arguments={},
    )
    variant = job.assembly_plan["variants"][0]
    assert variant["editor_timeline_mode"] == "authored"
    assert variant["resolved_archetype"] == "narrated"
    assert variant["render_generation_id"] != prior["variants"][0].get("render_generation_id")
    assert result["authored_timeline"]
    assert result["has_render_section"]
    assert [row["clip_index"] for row in variant["user_timeline"]["slots"]] == [2]


def test_authored_prepare_rejection_leaves_original_job_unchanged(monkeypatch):
    from app.routes import generative_jobs as gj
    from app.services.authored_editor import prepare_authored_editor_commit
    from tests.routes.test_editor_commit import _arm, _job

    _arm(monkeypatch)
    job = _job(resolved_archetype="subtitled")
    prior = copy.deepcopy(job.assembly_plan)
    with pytest.raises(HTTPException):
        prepare_authored_editor_commit(
            job,
            "song_text",
            gj.EditorCommitRequest(
                base_generation="2026-07-01T00:00:00Z",
                editor_state_version=1,
                timeline_slots=[gj.TimelineSlotEdit(clip_index=999, in_s=0, duration_s=1)],
            ),
            validation_arguments={},
        )
    assert job.assembly_plan == prior


def test_authored_phone_prepare_pins_new_recipe_and_leaves_original_program_unchanged(monkeypatch):
    import uuid

    from app.config import settings
    from app.kria.device_render import make_device_request
    from app.routes import generative_jobs as gj
    from app.services.authored_editor import prepare_authored_editor_commit
    from app.services.device_render import device_status, pin_device_request

    job, variant, previous = fixture()
    job.id, job.user_id = uuid.uuid4(), uuid.uuid4()
    job.status, job.current_phase = "variants_ready", None
    variant.update(
        render_generation_id="first", render_status="ready", duration_s=12, text_mode="none"
    )
    job.assembly_plan["variants"] = [variant]
    pin_device_request(
        job,
        make_device_request(job_id=job.id, variant_id="original_text", revision=1, recipe=previous),
        base_generation="first",
    )
    monkeypatch.setattr(settings, "phone_rendering_enabled", True)
    monkeypatch.setattr(settings, "GENERATIVE_TIMELINE_EDITOR_ENABLED", True)
    monkeypatch.setattr(
        settings,
        "phone_render_verified_features",
        ["basicComposition", "local1080Export", "audioMix"],
    )
    prep = prepare_authored_editor_commit(
        job,
        "original_text",
        gj.EditorCommitRequest(
            base_generation="first",
            editor_state_version=1,
            timeline_slots=[gj.TimelineSlotEdit(clip_index=1, in_s=2, duration_s=3)],
        ),
        validation_arguments={},
    )
    assert prep["render_destination"] == "device"
    assert job.status == "awaiting_device"
    request = device_status(job, "original_text").request
    assert request.identity.recipe_revision == 2
    assert request.recipe.tracks[0].clips[0].source_asset_id == "new"
    assert previous.tracks[0].clips[0].source_asset_id == "old"


@pytest.mark.asyncio
async def test_authored_source_validation_checks_initial_phone_receipt_liveness(monkeypatch):
    from unittest.mock import Mock

    from app.routes import editor_sources

    job, variant, _ = fixture()
    job.user_id, job.content_plan_item_id = "owner", "item"
    metadata = Mock(return_value=SimpleNamespace(generation="replaced"))
    monkeypatch.setattr(editor_sources.storage, "object_metadata", metadata)
    with pytest.raises(HTTPException, match="editor_source_generation_stale"):
        await editor_sources.validate_editor_sources(
            None, job=job, variant=variant, used_media_ids={"old"}
        )
    metadata.assert_called_once_with(binding("old").proxy_path)


@pytest.mark.parametrize("rate", [0.5, 2])
def test_authored_cloud_resolver_preserves_guided_source_controls_and_identity(monkeypatch, rate):
    from app.routes import generative_jobs as gj
    from app.services.authored_editor import resolve_authored_cloud_slots

    crop = {"x": 0.2, "y": 0.1, "width": 0.6, "height": 0.8}
    variant = {
        "resolved_archetype": "guided_story",
        "editor_timeline_mode": "authored",
        "user_timeline": {
            "slots": [
                {
                    "slot_id": "keep",
                    "clip_index": 0,
                    "in_s": 1,
                    "duration_s": 2,
                    "playback_rate": rate,
                    "source_crop": crop,
                    "layout": "supporting_card",
                    "source_gcs_path": "correct.mp4",
                }
            ]
        },
    }
    job = SimpleNamespace(id="job", all_candidates={"clip_paths": ["wrong.mp4", "correct.mp4"]})
    monkeypatch.setattr(
        gj, "editor_deletion_timeline", lambda *_: variant["user_timeline"]["slots"]
    )
    monkeypatch.setattr(
        gj,
        "_guided_v2_revision",
        lambda *_: {
            "sources": [
                {"gcs_path": "correct.mp4", "duration_s": 5, "kind": "video", "generation": "4"}
            ]
        },
    )
    rows = resolve_authored_cloud_slots(
        job, variant, [gj.TimelineSlotEdit(slot_id="keep", clip_index=0, in_s=1, duration_s=2)]
    )
    assert rows[0]["source_gcs_path"] == "correct.mp4"
    assert rows[0]["source_generation"] == "4"
    assert rows[0]["playback_rate"] == rate
    assert rows[0]["source_crop"] == crop
    assert rows[0]["layout"] == "supporting_card"
    cleared = resolve_authored_cloud_slots(
        job,
        variant,
        [
            gj.TimelineSlotEdit(
                slot_id="keep",
                clip_index=0,
                in_s=1,
                duration_s=2,
                source_crop=None,
                playback_rate=1,
                layout="fullscreen",
            )
        ],
    )
    assert cleared[0]["source_crop"] is None
    assert cleared[0]["playback_rate"] == 1
    with pytest.raises(HTTPException, match="TIMELINE_OUT_OF_BOUNDS"):
        resolve_authored_cloud_slots(
            job,
            variant,
            [
                gj.TimelineSlotEdit(
                    slot_id="keep", clip_index=0, in_s=2, duration_s=2, playback_rate=2
                )
            ],
        )


def test_authored_original_audio_level_preserves_video_stream(monkeypatch):
    from unittest.mock import Mock

    from app.pipeline import authored_timeline as authored
    from app.pipeline import probe

    monkeypatch.setattr(probe, "probe_video", lambda _: SimpleNamespace(has_audio=True))
    run = Mock()
    monkeypatch.setattr(authored.subprocess, "run", run)
    assert authored._apply_original_audio_level("input.mp4", "output.mp4", 0.2) == "output.mp4"
    command = run.call_args.args[0]
    assert command[command.index("-af") + 1] == "volume=0.200000"
    assert command[command.index("-c:v") + 1] == "copy"


def test_empty_guided_add_source_get_index_roundtrip_uses_shared_catalog(monkeypatch):
    from app.routes import generative_jobs as gj
    from app.services.authored_editor import authored_cloud_source_catalog
    from app.services.editor_empty_drafts import stage_saved_draft_baseline
    from tests.routes.test_editor_commit import _narrated_guided_job

    job = _narrated_guided_job()
    variant = job.assembly_plan["variants"][0]
    revision = gj._guided_v2_revision(job, variant)
    canonical_paths = [source["gcs_path"] for source in revision["sources"]]
    job.all_candidates = {"clip_paths": list(reversed(canonical_paths[:2]))}
    variant.update(editor_state="empty", render_generation_id="empty-generation")
    job._saved_editor_drafts = {
        variant["variant_id"]: SimpleNamespace(
            snapshot={"editor_payload": {"sections": {"timeline_slots": [], "text_elements": []}}},
            model_dump=lambda **_: {},
        )
    }

    def signer(path, _):
        return f"signed:{path}"

    before = gj.dispatch_get_timeline(job, variant["variant_id"], sign_url=signer)
    assert before["slots"] == []
    assert before["clips"][0]["signed_url"] == f"signed:{canonical_paths[0]}"
    # add_clip extends only the owned shared pool; its variant index is the
    # shared catalog's index, regardless of the legacy pool's ordering.
    new_path = "users/owner/new-photo.png"
    job.all_candidates["clip_paths"].append(new_path)
    job.all_candidates["editor_source_metadata"] = {
        new_path: {
            "source_kind": "image",
            "source_generation": "27",
        }
    }
    catalog = authored_cloud_source_catalog(job, variant)
    added_index = next(i for i, row in enumerate(catalog) if row["source_gcs_path"] == new_path)
    after = gj.dispatch_get_timeline(job, variant["variant_id"], sign_url=signer)
    assert after["clips"][: len(before["clips"])] == before["clips"]
    assert after["clips"][added_index]["signed_url"] == f"signed:{new_path}"
    assert after["clips"][added_index]["native_source"]["source_url"] == f"signed:{new_path}"
    assert after["clips"][added_index]["kind"] == "image"
    body = gj.EditorCommitRequest(
        base_generation="empty-generation",
        editor_state_version=1,
        timeline_slots=[gj.TimelineSlotEdit(clip_index=added_index, in_s=0, duration_s=2)],
    )
    staged = stage_saved_draft_baseline(job, variant["variant_id"], body)
    effective = staged.assembly_plan["variants"][0]
    rows = gj.resolve_timeline_slots_for_edit(staged, effective, body.timeline_slots)
    assert rows[0]["source_gcs_path"] == new_path
    assert rows[0]["source_generation"] == "27"
    assert rows[0]["source_kind"] == "image"
    effective["user_timeline"] = {"slots": rows}
    reopened = gj.dispatch_get_timeline(staged, variant["variant_id"], sign_url=signer)
    assert reopened["slots"][0]["clip_index"] == added_index
    assert reopened["clips"][added_index]["used"] is True
    assert reopened["clips"][added_index]["signed_url"] == f"signed:{new_path}"


# --- KRI-470 PR-G: the retained music keeps its compiled level semantics -------------------


def _voiceover_music_recipe(mix: float):
    from app.kria.render_assets import RenderFingerprint
    from app.pipeline.phone_recipe_shared import PhoneMusicBed
    from tests.pipeline.test_phone_voiceover_montage_plan import (
        _narration,
        compile_phone_voiceover_montage_plan,
    )
    from tests.pipeline.test_phone_voiceover_montage_plan import (
        fixture as montage_fixture,
    )

    decision, bindings = montage_fixture(
        voiceover_target_s=11.4, mix=mix, music_track_id="track1", music_start_s=10.0
    )
    music = PhoneMusicBed(
        catalog_id="track1",
        generation="7",
        fingerprint=RenderFingerprint(sha256="c" * 64, byte_count=500),
        duration_s=120.0,
        start_s=10.0,
    )
    return compile_phone_voiceover_montage_plan(
        decision, bindings, music=music, narration=_narration(duration_s=11.4)
    )


def _music_clip(recipe):
    return next(t for t in recipe.tracks if t.id == "music").clips[0]


@pytest.mark.parametrize("mix", [0.7, 0.9, 0.4])
def test_a_voiceover_variant_keeps_the_compiled_music_level_through_an_authored_restore(mix):
    """For voiceover variants `variant["mix"]` is the VOICE-prominence slider (default 0.7)
    and the compiler plays the music at min(1 - mix, 0.5). Restoring must not turn the music
    up to the voice's level."""
    job, variant, _ = fixture()
    previous = _voiceover_music_recipe(mix)
    compiled = _music_clip(previous).volume
    variant.update(variant_id="voiceover_music", resolved_archetype="voiceover", mix=mix)
    variant["music_track_id"] = "track1"

    recipe = compile_phone_authored_timeline(job, variant, previous)

    assert _music_clip(recipe).volume == pytest.approx(compiled)
    assert _music_clip(recipe).volume < 0.5 + 1e-9  # never as loud as the voice
    assert recipe.audio.music_asset_id is None
    assert doubled_soundtrack_assets(recipe) == []


def test_a_guided_variant_still_reads_mix_as_the_music_level():
    """The editor's `mix.music_level` (a variant with no voiceover) IS the music level."""
    from app.kria.recipes import AssetFingerprint, MediaAsset, TimelineClip, TimelineTrack
    from app.kria.render_assets import LibraryRenderAsset, RenderFingerprint

    job, variant, previous = fixture()
    previous.assets.append(
        MediaAsset(
            id="bed",
            relative_path="bed",
            duration=10,
            fingerprint=AssetFingerprint(hex="c" * 64, byte_count=123),
        )
    )
    previous.asset_manifest = previous.asset_manifest.model_copy(
        update={
            "assets": (
                *previous.asset_manifest.assets,
                LibraryRenderAsset(
                    id="bed",
                    catalog="music",
                    catalog_id="track",
                    generation="1",
                    fingerprint=RenderFingerprint(sha256="c" * 64, byte_count=123),
                ),
            )
        }
    )
    previous.tracks.append(
        TimelineTrack(
            id="bed",
            kind="audio",
            clips=[
                TimelineClip(
                    id="bed",
                    source_asset_id="bed",
                    rate=1,
                    source_start=0,
                    source_duration=10,
                    timeline_start=0,
                    volume=0.9,
                )
            ],
        )
    )
    variant.update(music_track_id="track", mix=0.35)

    recipe = compile_phone_authored_timeline(job, variant, previous)

    assert next(t for t in recipe.tracks if t.id == "bed").clips[0].volume == pytest.approx(0.35)
