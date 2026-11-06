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
    MediaTransform,
    TimelineClip,
    TimelineTrack,
    Transition,
)
from app.kria.recipes_v2 import EditRecipeV2
from app.kria.render_assets import RenderAssetManifest
from app.kria.split_screen import SplitScreenMembership, layout_split_timeline
from app.pipeline.authored_timeline import render_presentation
from app.pipeline.editor_render_window import phone_source_window
from app.pipeline.phone_captions import (
    caption_font_assets,
    caption_look_from_variant,
    compile_caption_layers,
)
from app.pipeline.phone_editor_visuals import compile_editor_media_track
from app.pipeline.phone_recipe_shared import (
    clip_tilt_fields,
    register_backdrop_visuals,
    snap_text_overshoot,
    timeline_end_s,
)
from app.pipeline.phone_subtitled_plan import _compile_overlay_track, _compile_sfx_track
from app.pipeline.phone_voiceover_montage_plan import voiceover_music_gain
from app.services.authored_editor import explicit_authored_slots
from app.services.phone_editor_sources import (
    editor_source_bindings,
    editor_visual_bindings,
    immutable_video_source_duration,
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
    """Compile saved placements against immutable source and audio receipts.

    Explicit editor sections own their lanes, including an empty section.
    An absent optional global lane retains the previous portable layer.
    """
    from app.pipeline.portable_text_layout import compile_text_overlay
    from app.services.user_song_projection import user_song_for_variant
    from app.tasks.generative_build import _text_element_burn_dicts, canvas_for_orientation

    user_song = user_song_for_variant(job, variant, song_filename=None)
    if user_song is not None and (
        not isinstance(previous, EditRecipeV2)
        or not any(asset.kind == "song" for asset in previous.asset_manifest.assets)
    ):
        raise ValueError("authored phone timeline has no receipt for the creator's own song")
    if not isinstance(previous, EditRecipeV2):
        raise ValueError("authored phone restore requires immutable source receipts")
    if variant.get("speech_timing_unresolved"):
        from app.pipeline.editor_render_window import RenderWindowUnavailable

        raise RenderWindowUnavailable(
            "Caption timing for the newly exposed speech is unavailable. "
            "Your edit is saved; add your captions, turn captions off, "
            "or restore the previous range before exporting."
        )
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
    split_sections = (variant.get("user_timeline") or {}).get("split_screen_sections") or []
    split_starts, recipe_sections, split_duration = layout_split_timeline(rows, split_sections)
    panel_rows = {}
    for row in rows:
        if row.get("split_section_id"):
            panel_rows.setdefault((row["split_section_id"], row["split_panel_index"]), []).append(
                row
            )
    panel_predecessors = {}
    for members in panel_rows.values():
        ordered = sorted(members, key=lambda row: row["split_offset_s"])
        for left, right in zip(ordered, ordered[1:]):
            panel_predecessors[id(right)] = left
    for index, row in enumerate(rows):
        if not 0 <= row["clip_index"] < len(catalog):
            raise ValueError("authored source index is outside the source catalog")
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
        source_end = row.get("source_end_s")
        still = bool(visual and visual.kind == "image")
        if source_end is not None:
            if still:
                raise ValueError("authored image cannot have source trim or speed")
            source_span = float(source_end) - start
            if source_span <= 0:
                raise ValueError("authored source endpoint must follow its start")
            # The saved source edge is authoritative.  The output clock is
            # frame-rounded, so never sample past that edge; hold the final
            # frame only when its rounded output slot is fractionally longer.
            source_span = min(source_span, duration * rate)
            hold = max(0.0, duration - source_span / rate)
            hold_duration = hold if hold > 1e-6 else None
        elif not still:
            start, source_span, hold_duration = phone_source_window(
                start,
                duration,
                rate,
                source_duration,
                start_locked=bool(row.get("source_start_locked")),
            )
            if hold_duration is not None:
                # Existing strict windows keep their behavior, but inferred
                # padding must never choose between ambiguous originals.
                if (
                    immutable_video_source_duration(job, variant, row["clip_index"])
                    != source_duration
                ):
                    raise ValueError("authored original source duration is inconsistent")
        else:
            source_span = duration * rate
            hold_duration = None
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
        if split_sections:
            cursor = split_starts[index]
        transition = None
        member_key = (row.get("split_section_id"), row.get("split_panel_index"))
        prior = (
            panel_predecessors.get(id(row))
            if member_key[0]
            else (rows[index - 1] if index else None)
        )
        if prior is not None and prior.get("transition_after", "cut") != "cut":
            from app.kria.split_screen import transition_overlap

            overlap = transition_overlap(prior, row)
            if bool(prior.get("split_section_id")) != bool(row.get("split_section_id")):
                overlap = 0.0
            if overlap >= 0.1:
                if member_key[0]:
                    actual = (
                        float(prior["split_offset_s"])
                        + float(prior["duration_s"])
                        - float(row["split_offset_s"])
                    )
                    if actual > overlap + 1e-6:
                        raise ValueError("panel transition exceeds its next clip overlap")
                    overlap = max(0.0, actual)
                transition = (
                    Transition(
                        kind={
                            "crossfade": "crossfade",
                            "dip_to_black": "fade_black",
                            "flash": "fade_white",
                        }[prior["transition_after"]],
                        duration=overlap,
                    )
                    if overlap > 0
                    else None
                )
                if not split_sections:
                    cursor -= overlap
        transform = dict(row.get("media_transform") or {})
        tilt_patch, backdrop = clip_tilt_fields(row, fit_scale=float(transform.get("scale", 1)))
        if row.get("tilt") is not None:
            transform["rotation_degrees"] = float(row["tilt"].get("rotation_deg") or 0)
        transform.update(tilt_patch)
        card = still and row.get("layout") == "supporting_card"
        if card:
            transform.pop("scale", None)
        clips.append(
            TimelineClip(
                speaker_frame_override=row.get("speaker_frame_override"),
                split_screen=(
                    SplitScreenMembership(
                        section_id=row["split_section_id"], panel_index=row["split_panel_index"]
                    )
                    if row.get("split_section_id")
                    else None
                ),
                id=row["slot_id"],
                source_asset_id=asset.id,
                source_start=start,
                source_duration=source_span,
                timeline_start=cursor,
                rate=rate,
                hold_duration=hold_duration,
                transition=transition,
                volume=0
                if still or row.get("muted")
                else (
                    row.get("source_audio_volume")
                    if row.get("source_audio_volume") is not None
                    else 1
                ),
                audio_fade_in=row.get("audio_fade_in"),
                audio_fade_out=row.get("audio_fade_out"),
                source_crop=row.get("source_crop"),
                look="golden_hour" if row.get("look_preset") == "golden_hour" else None,
                still_layout="supporting_card" if card else None,
                transform=MediaTransform(**transform),
                backdrop=None if card else backdrop,
            )
        )
        cursor += duration
    if split_sections:
        cursor = split_duration
    register_backdrop_visuals(clips, visuals=visuals, assets=assets, manifest=manifest)
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
    # KRI-736: `render_presentation` clamps every text to the cut's total, but the burn
    # dict then rounds its end UP to 3 decimals (`build_intro_overlay`). A cut whose total
    # is not millisecond-exact (frame-based phone durations: 7.4333333 x 3 = 22.2999999)
    # leaves a text that ends there ~0.1 ms past the clips, and `EditRecipeV2` compares
    # strictly, so the Save hard-failed `unsupported_phone_edit` ("text layer exceeds the
    # timeline"). Every other phone compiler snaps this; a real overrun still fails.
    snap_text_overshoot(layers, cursor if split_sections else timeline_end_s(clips))
    for font_id, font in caption_font_assets(layers).items():
        manifest[font_id] = font
        assets[font_id] = MediaAsset(
            id=font_id,
            relative_path=font_id,
            fingerprint=AssetFingerprint(
                hex=font.fingerprint.sha256, byte_count=font.fingerprint.byte_count
            ),
        )
    by_track = {}
    for clip in clips:
        member = clip.split_screen
        track_id = f"split-{member.section_id}-{member.panel_index}" if member else "authored"
        by_track.setdefault(track_id, []).append(clip)
    tracks = [
        TimelineTrack(
            id=key, kind="video", clips=sorted(value, key=lambda clip: clip.timeline_start)
        )
        for key, value in by_track.items()
    ]
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
        sound_effects=variant.get("sound_effects"),
        media_overlays=variant.get("media_overlays"),
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
    has_voice = variant.get("resolved_archetype") == "voiceover" or any(
        asset.kind == "voiceover" for asset in old_manifest.values()
    )
    for track in previous.tracks:
        if track.kind != "audio":
            continue
        retained = []
        for clip in track.clips:
            asset = old_manifest[clip.source_asset_id]
            is_voice = asset.kind == "voiceover"
            is_song = asset.kind == "song" and user_song is not None
            is_music = (
                asset.kind == "library"
                and asset.catalog == "music"
                and bool(
                    variant.get("music_track_id")
                    or variant.get("background_music_treatment")
                    or variant.get("smart_music_treatment")
                )
            )
            if not (is_voice or is_music or is_song) or clip.timeline_start >= cursor:
                continue
            projected = copy.deepcopy(clip.model_dump(mode="json"))
            projected["source_duration"] = min(
                clip.source_duration, (cursor - clip.timeline_start) * clip.rate
            )
            if is_song:
                start = float(user_song["window_start_s"])
                projected.update(
                    source_start=start,
                    source_duration=min(
                        float(user_song["window_end_s"]) - start,
                        cursor - clip.timeline_start,
                        old_assets[asset.id].duration - start,
                    ),
                    volume=float(user_song.get("volume", clip.volume)),
                )
                if projected["source_duration"] <= 0:
                    raise ValueError("creator song window has no playable audio")
            if is_music and isinstance(variant.get("mix"), (int, float)):
                # The music level lives on the bed's own track clip. `mix` means two
                # things: on a voiceover variant it is the VOICE-prominence slider and the
                # compiler plays the music at `voiceover_music_gain(mix)`; elsewhere it is
                # the editor's music level itself (`mix.music_level`).
                projected["volume"] = (
                    voiceover_music_gain(variant["mix"]) if has_voice else float(variant["mix"])
                )
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
    background = None
    if "speaker_background" in variant:
        from app.pipeline.phone_subtitled_plan import _compile_background
        from app.services.phone_editor import _background_lane

        background_lane = _background_lane(variant.get("speaker_background"), visuals)
        if background_lane is not None:
            background = _compile_background(
                background_lane,
                visuals=visuals,
                assets=assets,
                manifest=manifest,
                timeline_duration_s=cursor,
            )
    elif previous.speaker_background is not None:
        background = previous.speaker_background.model_copy(deep=True)
        for field in ("windows", "full_screen_windows"):
            windows = getattr(background, field) or []
            bounded = [
                window.model_copy(update={"end": min(window.end, cursor)})
                for window in windows
                if window.start < cursor and min(window.end, cursor) - window.start >= 0.1
            ]
            setattr(background, field, bounded or None)
        references = {background.asset_id} | {
            window.asset_id for window in background.windows or []
        }
        for asset_id in references - {None}:
            assets[asset_id] = old_assets[asset_id]
            manifest[asset_id] = old_manifest[asset_id]
    if background is not None:
        prior_background = previous.speaker_background
        original_subject_id = (
            prior_background.subject_track_id if prior_background else background.subject_track_id
        )
        exact_ids = prior_background.subject_clip_ids if prior_background else None
        subject_assets = {
            clip.source_asset_id
            for track in previous.tracks
            for clip in track.clips
            if (clip.id in exact_ids if exact_ids is not None else track.id == original_subject_id)
        }
        provenance = {row["slot_id"]: row.get("speaker_subject") for row in rows}
        subjects = [
            clip.id
            for track in tracks
            if track.kind == "video"
            for clip in track.clips
            if (
                provenance.get(clip.id)
                if provenance.get(clip.id) is not None
                else clip.source_asset_id in subject_assets
            )
            and getattr(manifest.get(clip.source_asset_id), "kind", None) == "original"
        ]
        if split_sections or exact_ids is not None:
            background.subject_clip_ids = subjects
        else:
            owners = [
                track.id for track in tracks if any(clip.id in subjects for clip in track.clips)
            ]
            if len(owners) == 1:
                background.subject_track_id = owners[0]
            else:
                background = None
    recipe = EditRecipeV2(
        speaker_background=background,
        split_screen_sections=recipe_sections,
        canvas=Canvas(width=canvas.width, height=canvas.height),
        assets=list(assets.values()),
        asset_manifest=RenderAssetManifest(assets=tuple(manifest.values())),
        tracks=tracks,
        text_layers=layers,
        audio=AudioMixRecipe.model_validate(audio),
        required_capabilities={"basicComposition", "local1080Export", "audioMix"}
        | ({"positionedText", "animatedText"} if layers else set())
        | ({"narrationAudio"} if narration_id else set())
        | ({"musicBed"} if user_song is not None else set())
        | ({"soundEffects"} if lanes.sound_effects else set())
        | ({"visualBlocks", "alphaOverlay"} if lanes.overlays else set())
        | ({"crossfade"} if any(clip.transition for clip in clips) else set())
        | ({"variableSpeed"} if any(clip.rate != 1 for clip in clips) else set()),
    )

    from app.pipeline.phone_recipe_shared import with_drawings

    if "drawings" in variant:
        recipe = with_drawings(recipe, variant["drawings"])
    elif previous.drawing_layers:
        layers = [
            layer.model_copy(
                update={
                    "end": min(layer.end, recipe.duration),
                    "draw_duration": min(layer.draw_duration, recipe.duration - layer.start),
                }
            )
            for layer in previous.drawing_layers
            if min(layer.end, recipe.duration) - layer.start >= 0.1
        ]
        recipe = EditRecipeV2.model_validate(
            {
                **recipe.model_dump(mode="json"),
                "drawing_layers": [layer.model_dump(mode="json") for layer in layers],
            }
        )
    if any(asset.kind == "processed_audio" for asset in previous.asset_manifest.assets):
        from app.pipeline.phone_processed_audio import (
            processed_audio_receipt_for,
            replace_audio_with_processed_segments,
        )

        required = {
            asset.source_media_id
            for asset in previous.asset_manifest.assets
            if asset.kind == "processed_audio"
        }
        receipts = {
            media_id: processed_audio_receipt_for(
                job.assembly_plan, variant_id=variant["variant_id"], source_media_id=media_id
            )
            for media_id in required
        }
        if any(receipt is None for receipt in receipts.values()):
            raise ValueError("saved processed audio receipt is unavailable")
        recipe = replace_audio_with_processed_segments(recipe, receipts_by_source=receipts)
    return recipe
