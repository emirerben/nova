"""Compile only creator-authored placements using immutable phone receipts."""

from __future__ import annotations

import copy
from typing import Any

from app.kria.recipes import (
    AssetFingerprint,
    AudioMixRecipe,
    Canvas,
    MediaAsset,
    MediaSize,
    TimelineClip,
    TimelineTrack,
    Transition,
)
from app.kria.recipes_v2 import EditRecipeV2
from app.kria.render_assets import RenderAssetManifest
from app.pipeline.authored_timeline import render_presentation
from app.pipeline.phone_captions import (
    caption_font_assets,
    caption_look_from_variant,
    compile_caption_layers,
)
from app.pipeline.phone_editor_visuals import compile_editor_media_track
from app.pipeline.phone_subtitled_plan import _compile_overlay_track, _compile_sfx_track
from app.services.authored_editor import explicit_authored_slots
from app.services.phone_editor_sources import (
    editor_source_bindings,
    editor_visual_bindings,
    phone_editor_source_revision,
)
from app.services.phone_sources import (
    PHONE_SOURCES_FIELD,
    PHONE_VISUALS_FIELD,
    PhoneSourceBinding,
    PhoneVisualBinding,
)
from app.services.phone_subtitled_editor import lanes_from_editor_sections, lanes_from_recipe


def compile_phone_authored_timeline(
    job: Any, variant: dict, previous: EditRecipeV2
) -> EditRecipeV2:
    """The previous recipe contributes immutable independent audio receipts only.

    It never supplies visual placements, caption/text layers, cutaways, ending
    clips or missing lane values. Saved editor sections are authoritative.
    """
    from app.pipeline.portable_text_layout import compile_text_overlay
    from app.tasks.generative_build import _text_element_burn_dicts, canvas_for_orientation

    if not isinstance(previous, EditRecipeV2):
        raise ValueError("authored phone restore requires immutable source receipts")
    catalog = (phone_editor_source_revision(job, variant) or {})["sources"]
    assembly = job.assembly_plan or {}
    bindings = tuple(
        PhoneSourceBinding.model_validate(row) for row in assembly.get(PHONE_SOURCES_FIELD) or []
    ) + editor_source_bindings(variant)
    visuals = tuple(
        PhoneVisualBinding.model_validate(row) for row in assembly.get(PHONE_VISUALS_FIELD) or []
    ) + editor_visual_bindings(variant)
    assets: dict[str, MediaAsset] = {}
    manifest: dict[str, Any] = {}
    clips = []
    cursor = 0.0
    rows = explicit_authored_slots(variant)
    for index, row in enumerate(rows):
        source = catalog[row["clip_index"]]
        binding = next(
            (
                value
                for value in bindings
                if value.media_id == source["media_id"]
                and value.proxy_path == source["gcs_path"]
                and value.generation == source["generation"]
            ),
            None,
        )
        visual = next(
            (
                value
                for value in visuals
                if value.media_id == source["media_id"]
                and value.gcs_path == source["gcs_path"]
                and value.generation == source["generation"]
            ),
            None,
        )
        if (binding is None) == (visual is None):
            raise ValueError("authored source must match exactly one immutable receipt")
        receipt = binding or visual
        asset = receipt.render_asset()
        manifest[asset.id] = asset
        original = binding.original if binding else None
        source_duration = original.duration_s if original else visual.duration_s
        duration, start, rate = (
            float(row["duration_s"]),
            float(row["in_s"]),
            float(row.get("playback_rate") or 1),
        )
        still = bool(visual and visual.kind == "image")
        if not still and (
            source_duration is None or start + duration * rate > source_duration + 0.001
        ):
            raise ValueError("authored source window exceeds its original receipt")
        if still and (start != 0 or rate != 1):
            raise ValueError("authored image cannot have source trim or speed")
        width = original.width if original else visual.width
        height = original.height if original else visual.height
        assets[asset.id] = MediaAsset(
            id=asset.id,
            relative_path=asset.id,
            fingerprint=AssetFingerprint(
                hex=asset.fingerprint.sha256, byte_count=asset.fingerprint.byte_count
            ),
            duration=source_duration,
            natural_size=MediaSize(width=width, height=height) if width and height else None,
            orientation_degrees=original.orientation_degrees
            if original
            else visual.orientation_degrees,
            is_proxy_available=binding is not None,
        )
        transition = None
        if index and rows[index - 1].get("transition_after", "cut") != "cut":
            prior = rows[index - 1]
            overlap = min(
                float(prior.get("transition_duration_s") or 0.3),
                duration * 0.3,
                float(prior["duration_s"]) * 0.3,
                0.3,
            )
            if overlap >= 0.1:
                transition = Transition(
                    kind={
                        "crossfade": "crossfade",
                        "dip_to_black": "fade_black",
                        "flash": "fade_white",
                    }[prior["transition_after"]],
                    duration=overlap,
                )
                cursor -= overlap
        clips.append(
            TimelineClip(
                id=row["slot_id"],
                source_asset_id=asset.id,
                source_start=start,
                source_duration=duration * rate,
                timeline_start=cursor,
                rate=rate,
                transition=transition,
                volume=0 if still else 1,
                source_crop=row.get("source_crop"),
                still_layout="supporting_card"
                if still and row.get("layout") == "supporting_card"
                else None,
            )
        )
        cursor += duration
    presentation = render_presentation(variant, cursor)
    for key in ("carousel_moment", "motion_scenes", "camera_effects", "custom_effects"):
        if presentation.get(key):
            raise ValueError(f"authored phone {key} is not phone-qualified")
    canvas = canvas_for_orientation(variant.get("orientation"))
    layers = []
    for index, overlay in enumerate(_text_element_burn_dicts(presentation)):
        layer, _font = compile_text_overlay(
            overlay, layer_id=f"authored-text-{index}", canvas=canvas
        )
        layers.append(layer)
    layers.extend(
        compile_caption_layers(
            (presentation.get("caption_cues") or [])
            if presentation.get("captions_enabled", True) is not False
            else [],
            canvas_width=canvas.width,
            canvas_height=canvas.height,
            style=variant.get("voiceover_caption_style") or "sentence",
            timeline_duration_s=cursor,
            look=caption_look_from_variant(variant),
        )
    )
    for font_id, font in caption_font_assets(layers).items():
        manifest[font_id] = font
        assets[font_id] = MediaAsset(
            id=font_id,
            relative_path=font_id,
            fingerprint=AssetFingerprint(
                hex=font.fingerprint.sha256, byte_count=font.fingerprint.byte_count
            ),
        )
    tracks = [TimelineTrack(id="authored", kind="video", clips=clips)]
    if presentation.get("visual_blocks"):
        tracks.append(
            compile_editor_media_track(
                presentation["visual_blocks"],
                visuals=visuals,
                timeline_duration_s=cursor,
                assets=assets,
                manifest=manifest,
            )
        )
    previous_lanes = lanes_from_recipe(
        previous, visuals=visuals, duck_receipt=variant.get("phone_sfx_duck_receipt")
    )
    lanes = lanes_from_editor_sections(
        previous=previous_lanes,
        sound_effects=variant.get("sound_effects") or [],
        media_overlays=variant.get("media_overlays") or [],
        visuals=visuals,
    )
    # No ending-clip carryover: main footage comes exclusively from rows above.
    if lanes.overlays:
        overlay_track, _ = _compile_overlay_track(
            lanes.overlays, visuals=visuals, speaker_end=cursor, assets=assets, manifest=manifest
        )
        if overlay_track.clips:
            tracks.append(overlay_track)
    if lanes.sound_effects:
        sfx = _compile_sfx_track(
            lanes.sound_effects, timeline_end=cursor, assets=assets, manifest=manifest
        )
        if sfx.clips:
            tracks.append(sfx)
    old_manifest = {asset.id: asset for asset in previous.asset_manifest.assets}
    old_assets = {asset.id: asset for asset in previous.assets}
    narration_id = None
    for track in previous.tracks:
        if track.kind != "audio":
            continue
        retained = []
        for clip in track.clips:
            asset = old_manifest[clip.source_asset_id]
            is_voice = asset.kind == "voiceover"
            is_music = (
                asset.kind == "library"
                and asset.catalog == "music"
                and bool(
                    variant.get("music_track_id")
                    or variant.get("background_music_treatment")
                    or variant.get("smart_music_treatment")
                )
            )
            if not (is_voice or is_music) or clip.timeline_start >= cursor:
                continue
            projected = copy.deepcopy(clip.model_dump(mode="json"))
            projected["source_duration"] = min(
                clip.source_duration, (cursor - clip.timeline_start) * clip.rate
            )
            if is_music and isinstance(variant.get("mix"), (int, float)):
                # The creator's music level lives on the bed's own track clip.
                projected["volume"] = float(variant["mix"])
            retained.append(TimelineClip.model_validate(projected))
            manifest[asset.id] = asset
            assets[asset.id] = old_assets[asset.id]
            if is_voice:
                narration_id = asset.id
        if retained:
            tracks.append(TimelineTrack(id=track.id, kind="audio", clips=retained))
    original_gain = variant.get("original_audio_level")
    if original_gain is None:
        original_gain = (variant.get("audio_mix") or {}).get("original_level")
    if original_gain is None:
        original_gain = variant.get("voiceover_bed_level") if narration_id else None
    if original_gain is None:
        original_gain = previous.audio.original_volume if not narration_id else 0
    audio = previous.audio.model_dump(mode="json")
    # The retained music plays through its audio-track clip. `music_asset_id` stays
    # unset: the device plays that asset as a SECOND bed from source 0 on top of the
    # clips, which is the KRI-481 double play (and `verify_phone_recipe` refuses it).
    audio.update(
        original_volume=float(original_gain),
        music_asset_id=None,
        narration_asset_id=narration_id,
    )
    # Old mute windows target old clip IDs; current visual placements carry
    # their own audio policy and must not inherit removed footage's windows.
    audio["mute_windows"] = []
    return EditRecipeV2(
        canvas=Canvas(width=canvas.width, height=canvas.height),
        assets=list(assets.values()),
        asset_manifest=RenderAssetManifest(assets=tuple(manifest.values())),
        tracks=tracks,
        text_layers=layers,
        audio=AudioMixRecipe.model_validate(audio),
        required_capabilities={"basicComposition", "local1080Export", "audioMix"}
        | ({"positionedText", "animatedText"} if layers else set())
        | ({"narrationAudio"} if narration_id else set())
        | ({"soundEffects"} if lanes.sound_effects else set())
        | ({"visualBlocks", "alphaOverlay"} if lanes.overlays else set())
        | ({"crossfade"} if any(clip.transition for clip in clips) else set())
        | ({"variableSpeed"} if any(clip.rate != 1 for clip in clips) else set()),
    )
