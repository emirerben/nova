"""Render an explicitly authored cloud timeline without generated-cut fallbacks."""

from __future__ import annotations

import copy
import os
import subprocess
import tempfile
import uuid
from datetime import datetime

from app import storage
from app.models import Job, MusicTrack
from app.services.authored_editor import explicit_authored_slots, is_authored_timeline


def render_presentation(variant: dict, duration: float) -> dict:
    """Bound timed presentation while retaining the complete saved document."""
    result = copy.deepcopy(variant)
    result["duration_s"] = duration
    # Old caches are pixels from another cut. They must never seed this render.
    for key in (
        "subject_matte_path",
        "visual_blocks_base_path",
        "motion_base_path",
        "motion_base_source_path",
    ):
        result[key] = None
    result["visual_blocks_cache_stale"] = True
    result["motion_cache_stale"] = True
    result["lyrics_baked"] = False  # saved lyric elements now burn as independent layers
    for key in (
        "text_elements",
        "caption_cues",
        "visual_blocks",
        "motion_scenes",
        "camera_effects",
    ):
        rows = result.get(key)
        if not isinstance(rows, list):
            continue
        bounded = []
        for row in rows:
            row = copy.deepcopy(row)
            if not isinstance(row, dict):
                raise ValueError(f"invalid authored {key} record")
            start_key = "start_s" if "start_s" in row else "start"
            end_key = "end_s" if "end_s" in row else "end"
            start = float(row.get(start_key) or 0)
            if start >= duration:
                continue
            if row.get(end_key) is not None:
                row[end_key] = min(float(row[end_key]), duration)
            bounded.append(row)
        result[key] = bounded
    return result


def prepare_authored_assembly(
    slots: list[dict], paths: list[str], tmpdir: str, *, job_id: str, canvas
) -> dict:
    """Materialize each occurrence before assembly; no generated-cut fallback.

    Source indexes belong to the editor catalog, which can differ from the
    original upload pool. Server-resolved source paths therefore win.
    """
    from app.pipeline.agents.gemini_analyzer import AssemblyStep
    from app.pipeline.guided_story import _render_image_moment, _render_video_moment
    from app.pipeline.probe import probe_video
    from app.services.phone_sources import is_analysis_proxy_path
    from app.tasks.template_orchestrate import _download_clips_parallel

    selected = []
    for row in slots:
        path = row.get("source_gcs_path")
        if not path:
            index = row.get("clip_index")
            if not isinstance(index, int) or not 0 <= index < len(paths):
                raise ValueError("authored source identity unavailable")
            path = paths[index]
        if is_analysis_proxy_path(path):
            raise ValueError("device originals cannot render in the cloud")
        selected.append(path)
    unique_paths = list(dict.fromkeys(selected))
    first_rows = {path: row for row, path in reversed(list(zip(slots, selected, strict=True)))}
    pinned = [path for path in unique_paths if first_rows[path].get("source_generation")]
    ordinary = [path for path in unique_paths if path not in pinned]
    local_by_path = {}
    if pinned:
        from app.pipeline.guided_story import _download_selected

        prepared, _receipts = _download_selected(
            {
                "selected_media_ids": pinned,
                "story_timeline": [
                    {
                        "media_id": path,
                        "gcs_path": path,
                        "generation": first_rows[path]["source_generation"],
                        "kind": first_rows[path].get("source_kind", "video"),
                    }
                    for path in pinned
                ],
            },
            tmpdir,
        )
        local_by_path.update(prepared)
    if ordinary:
        downloaded = _download_clips_parallel(ordinary, tmpdir)
        if len(downloaded) != len(ordinary):
            raise ValueError("authored source download incomplete")
        local_by_path.update(zip(ordinary, downloaded, strict=True))
    from app.pipeline.source_guard import downscale_oversized_sources

    video_source_paths = [
        path for path in unique_paths if first_rows[path].get("source_kind", "video") != "image"
    ]
    video_paths = [local_by_path[path] for path in video_source_paths]
    probes = {path: probe_video(path) for path in video_paths}
    downscale_oversized_sources(video_paths, probes, tmpdir, job_id=job_id)
    local_by_path.update(zip(video_source_paths, video_paths, strict=True))
    steps, clip_id_to_local, clip_id_to_gcs, probe_map = [], {}, {}, {}
    for index, (row, path) in enumerate(zip(slots, selected, strict=True)):
        duration = float(row["duration_s"])
        start = float(row["in_s"])
        rate = float(row.get("playback_rate") or 1)
        if duration <= 0 or start < 0 or not 0.25 <= rate <= 4:
            raise ValueError("authored source window invalid")
        output = os.path.join(tmpdir, f"authored_occurrence_{index}.mp4")
        common = {
            "layout": row.get("layout") or "fullscreen",
            "canvas": canvas,
            "look_preset": row.get("look_preset") or "none",
            "look_adjustments": row.get("look_adjustments"),
        }
        if row.get("source_kind") == "image":
            source = local_by_path[path]
            if row.get("source_crop"):
                from PIL import Image

                crop = row["source_crop"]
                with Image.open(source) as image:
                    width, height = image.size
                    cropped = image.crop(
                        (
                            round(crop["x"] * width),
                            round(crop["y"] * height),
                            round((crop["x"] + crop["width"]) * width),
                            round((crop["y"] + crop["height"]) * height),
                        )
                    )
                    source = os.path.join(tmpdir, f"authored_crop_{index}.png")
                    cropped.save(source)
            _render_image_moment(source, output, duration_s=duration, **common)
        else:
            _render_video_moment(
                local_by_path[path],
                output,
                start_s=start,
                end_s=start + duration * rate,
                playback_rate=rate,
                source_crop=row.get("source_crop"),
                preserve_audio=True,
                # reframe's exact-duration cap is in SOURCE seconds; the final
                # assembly applies the output-time cap after retiming instead.
                exact_duration=rate == 1,
                **common,
            )
        clip_id = f"authored_{index}"
        clip_id_to_local[clip_id] = output
        clip_id_to_gcs[clip_id] = path
        probe_map[output] = probe_video(output)
        if probe_map[output].duration_s + 0.05 < duration:
            raise ValueError("authored occurrence was unexpectedly shortened")
        prior = slots[index - 1] if index else {}
        transition = {
            "crossfade": "crossfade",
            "dip_to_black": "dip-to-black",
            "flash": "flash",
        }.get(prior.get("transition_after"), "hard-cut")
        overlap = min(
            float(prior.get("transition_duration_s") or 0.3),
            0.3,
            duration * 0.3,
            float(prior.get("duration_s") or duration) * 0.3,
        )
        if overlap < 0.1:
            transition = "hard-cut"
        steps.append(
            AssemblyStep(
                clip_id=clip_id,
                slot={
                    "position": index + 1,
                    "slot_type": "broll",
                    "target_duration_s": duration,
                    "exact_window": True,
                    "transition_in": transition,
                    "transition_duration_s": overlap if transition != "hard-cut" else None,
                    "look_preset": "none",
                    "look_adjustments": None,
                },
                moment={
                    "start_s": 0,
                    "end_s": duration,
                    "energy": row.get("moment_energy") or 5,
                    "description": row.get("moment_description") or "",
                },
            )
        )
    return {
        "steps": steps,
        "clip_id_to_local": clip_id_to_local,
        "clip_id_to_gcs": clip_id_to_gcs,
        "probe_map": probe_map,
    }


def _mix_narration(
    video: str, voice: str, output: str, *, duration: float, original_gain: float
) -> None:
    """Strict mix: an explicit independent narration lane must never fail open."""
    from app.pipeline.probe import probe_video

    gain = min(max(original_gain, 0), 1)
    graph = f"[1:a]apad,atrim=duration={duration:.6f}[voice]"
    if probe_video(video).has_audio and gain:
        graph += (
            f";[0:a]volume={gain:.6f},apad,atrim=duration={duration:.6f}[bed];"
            "[voice][bed]amix=inputs=2:normalize=0[a]"
        )
    else:
        graph += ";[voice]anull[a]"
    subprocess.run(
        [
            "ffmpeg",
            "-nostdin",
            "-y",
            "-i",
            video,
            "-i",
            voice,
            "-filter_complex",
            graph,
            "-map",
            "0:v:0",
            "-map",
            "[a]",
            "-c:v",
            "copy",
            "-c:a",
            "aac",
            "-t",
            f"{duration:.6f}",
            output,
        ],
        capture_output=True,
        check=True,
        timeout=120,
    )


def _apply_original_audio_level(video: str, output: str, level: float) -> str:
    from app.pipeline.probe import probe_video

    if not probe_video(video).has_audio:
        return video
    subprocess.run(
        [
            "ffmpeg",
            "-nostdin",
            "-y",
            "-i",
            video,
            "-map",
            "0:v:0",
            "-map",
            "0:a:0",
            "-c:v",
            "copy",
            "-af",
            f"volume={float(level):.6f}",
            "-c:a",
            "aac",
            output,
        ],
        capture_output=True,
        check=True,
        timeout=120,
    )
    return output


def _finalize_camera_base(video: str, output: str) -> None:
    """Camera reframing is an intermediate encode; it may be the last visual pass."""
    from app.pipeline.reframe import _encoding_args

    subprocess.run(
        [
            "ffmpeg",
            "-nostdin",
            "-y",
            "-i",
            video,
            *_encoding_args(output, preset="fast", crf="20"),
        ],
        capture_output=True,
        check=True,
        timeout=300,
    )


def rerender_authored_timeline(job_id: str, variant_id: str, generation: str | None) -> None:
    # Imports stay local: the existing task owns tracing, time budget, retry and
    # failure handling. This branch only supplies an explicit assembly program.
    from app.kria.media_sources import is_analysis_proxy_path, require_cloud_render_job
    from app.pipeline.probe import probe_video
    from app.tasks import generative_build as gb
    from app.tasks.template_orchestrate import _assemble_clips, _mix_template_audio

    with gb._sync_session() as db:
        job = db.get(Job, uuid.UUID(job_id))
        if job is None:
            raise ValueError("authored job unavailable")
        require_cloud_render_job(job)
        variant = next(
            (
                v
                for v in (job.assembly_plan or {}).get("variants", [])
                if v.get("variant_id") == variant_id
            ),
            None,
        )
        if (
            variant is None
            or not is_authored_timeline(variant)
            or variant.get("editor_state") == "empty"
        ):
            raise ValueError("authored timeline unavailable")
        if generation is None or variant.get("render_generation_id") != generation:
            return
        variant = copy.deepcopy(variant)
        candidates = copy.deepcopy(job.all_candidates or {})
        assembly = copy.deepcopy(job.assembly_plan or {})
        track = (
            db.get(MusicTrack, uuid.UUID(str(variant["music_track_id"])))
            if variant.get("music_track_id")
            else None
        )
        music_path = track.audio_gcs_path if track is not None else None
        if variant.get("music_track_id") and not music_path:
            raise ValueError("saved authored music is unavailable")
    for lane, enabled in (
        ("visual_blocks", gb.settings.visual_blocks_enabled),
        ("motion_scenes", gb.settings.motion_scenes_enabled),
        ("media_overlays", gb.settings.media_overlays_enabled),
        ("sound_effects", gb.settings.sound_effects_enabled),
        ("background_music_treatment", gb.settings.smart_music_bed_enabled),
        ("smart_music_treatment", gb.settings.smart_music_bed_enabled),
    ):
        if variant.get(lane) and not enabled:
            raise ValueError(f"saved authored {lane} is currently unavailable")
    slots = explicit_authored_slots(variant)
    paths = list(candidates.get("clip_paths") or [])
    if any(is_analysis_proxy_path(path) for path in paths):
        raise ValueError("device originals cannot render in the cloud")
    if not gb._update_variant_entry(
        job_id,
        variant_id,
        {"render_status": "rendering"},
        expected_render_gen_id=generation,
        outcome="authored_start",
    ):
        return
    created: list[str] = []
    accepted = False
    terminal_state = {"accepted": False}
    try:
        with tempfile.TemporaryDirectory(prefix="nova_authored_") as tmpdir:
            assembled = prepare_authored_assembly(
                slots,
                paths,
                tmpdir,
                job_id=job_id,
                canvas=gb.canvas_for_orientation(variant.get("orientation")),
            )
            if assembled is None or len(assembled["steps"]) != len(slots):
                raise ValueError("authored source windows could not be assembled")
            carousel_sink: dict[str, float] = {}
            if variant.get("carousel_moment"):
                cfg = variant["carousel_moment"]
                source_indices = list(
                    dict.fromkeys(
                        row["clip_index"]
                        for row in cfg.get("sequence")
                        or (variant.get("ai_timeline") or {}).get("slots")
                        or []
                        if isinstance(row, dict) and isinstance(row.get("clip_index"), int)
                    )
                )[:5]
                if not source_indices:
                    raise ValueError("saved carousel has no explicit source identities")
                carousel = gb._prepare_timeline_assembly(
                    [
                        {"clip_index": index, "in_s": 0, "duration_s": 0.1}
                        for index in source_indices
                    ],
                    paths,
                    tmpdir,
                    job_id=job_id,
                )
                if carousel is None:
                    raise ValueError("saved carousel sources unavailable")
                assembled["clip_id_to_local"].update(carousel["clip_id_to_local"])
                assembled["probe_map"].update(carousel["probe_map"])
                assembled["steps"] = gb._insert_carousel_moment_step(
                    assembled["steps"],
                    variant,
                    clip_id_to_local=assembled["clip_id_to_local"],
                    clip_id_to_gcs=assembled["clip_id_to_gcs"],
                    probe_map=assembled["probe_map"],
                    variant_dir=tmpdir,
                    inserted_duration_out=carousel_sink,
                    source_steps=carousel["steps"],
                )
                if "duration_s" not in carousel_sink:
                    raise ValueError("saved carousel could not be rendered")
            clean_local = os.path.join(tmpdir, "authored_clean.mp4")
            _assemble_clips(
                assembled["steps"],
                assembled["clip_id_to_local"],
                assembled["probe_map"],
                clean_local,
                tmpdir,
                clip_metas=[],
                job_id=job_id,
                user_subject="",
                interstitials=[],
                force_single_pass=False,
                is_agentic=True,
                allow_slowdown_fill=False,
                canvas=gb.canvas_for_orientation(variant.get("orientation")),
            )
            duration = float(probe_video(clean_local).duration_s)
            if duration <= 0:
                raise ValueError("authored render has no duration")
            pinned_narration = (assembly.get("guided_story_execution_plan") or {}).get("narration")
            voice_path = (pinned_narration or {}).get("gcs_path") or candidates.get(
                "voiceover_gcs_path"
            )
            if music_path:
                mixed = os.path.join(tmpdir, "authored_music.mp4")
                _mix_template_audio(
                    clean_local,
                    music_path,
                    mixed,
                    tmpdir,
                    audio_start_offset_s=float(variant.get("music_start_s") or 0),
                    require_audio=True,
                    force_video_duration=True,
                    audio_gain=float(variant.get("mix", 1))
                    if variant.get("resolved_archetype") == "guided_story"
                    else 1.0,
                    # The creator's footage level plays under the song. With a
                    # narration lane the footage bed is handled by _mix_narration.
                    original_level=None if voice_path else variant.get("original_audio_level"),
                )
                clean_local = mixed
            if voice_path:
                voice_local = os.path.join(tmpdir, "authored_voice")
                if pinned_narration:
                    from app.schemas.edit_proposal import NarrationTrack

                    narration = NarrationTrack.model_validate(pinned_narration)
                    storage.download_generation_to_file(
                        narration.gcs_path,
                        voice_local,
                        generation=narration.generation,
                    )
                elif assembly.get("speech_cleanup_contract") == "required_v1":
                    from app.pipeline.speech_cleanup_apply import (
                        apply_speech_cleanup_to_audio,
                        hydrate_job_speech_cleanup_snapshot,
                    )

                    snapshot = hydrate_job_speech_cleanup_snapshot(assembly).require_source(
                        kind="voiceover",
                        storage_path=voice_path,
                    )
                    storage.download_generation_to_file(
                        voice_path,
                        voice_local,
                        generation=snapshot.generation,
                    )
                    cleaned_voice = os.path.join(tmpdir, "authored_clean_voice.wav")
                    apply_speech_cleanup_to_audio(snapshot, voice_local, cleaned_voice)
                    voice_local = cleaned_voice
                else:
                    storage.download_to_file(voice_path, voice_local)
                mixed = os.path.join(tmpdir, "authored_voice.mp4")
                _mix_narration(
                    clean_local,
                    voice_local,
                    mixed,
                    duration=duration,
                    original_gain=(
                        (1.0 if music_path else float(variant.get("original_audio_level") or 0))
                        if pinned_narration
                        else float(
                            (variant.get("audio_mix") or {}).get(
                                "original_level",
                                variant.get("voiceover_bed_level")
                                if variant.get("voiceover_bed_level") is not None
                                else 1 - float(variant.get("mix", 1)),
                            )
                        )
                    ),
                )
                clean_local = mixed
            if (
                not music_path
                and not voice_path
                and variant.get("original_audio_level") is not None
            ):
                clean_local = _apply_original_audio_level(
                    clean_local,
                    os.path.join(tmpdir, "authored_original_mix.mp4"),
                    float(variant["original_audio_level"]),
                )
            base_key = gb._variant_storage_key(
                job_id, f"authored_{variant_id}_{uuid.uuid4().hex}_base.mp4", generation
            )
            storage.upload_public_read(clean_local, base_key)
            created.append(base_key)
            presentation = render_presentation(variant, duration)
            if carousel_sink:
                presentation["carousel_inserted_duration_s"] = carousel_sink["duration_s"]
                presentation["carousel_insertion_base_s"] = carousel_sink.get("insertion_base_s")
                presentation["carousel_ripple_duration_s"] = carousel_sink.get("ripple_duration_s")
            render_base, visual_cache, motion_cache, motion_source, motion_identity = (
                gb._ensure_creator_layer_base(
                    job_id=job_id,
                    variant_id=variant_id,
                    variant=presentation,
                    base_gcs_path=base_key,
                )
            )
            for path in (visual_cache, motion_cache):
                if path and path not in created:
                    created.append(path)
            if render_base != base_key:
                clean_local = os.path.join(tmpdir, "authored_layers.mp4")
                storage.download_to_file(render_base, clean_local)
            if presentation.get("camera_effects"):
                from app.pipeline.camera_effects import normalize_camera_effects
                from app.pipeline.reframe import reframe_and_export

                effects = normalize_camera_effects(
                    presentation["camera_effects"], duration_s=duration
                )
                camera_local = os.path.join(tmpdir, "authored_camera.mp4")
                probe = probe_video(clean_local)
                reframe_and_export(
                    clean_local,
                    0,
                    duration,
                    "16:9" if variant.get("orientation") == "landscape" else "9:16",
                    None,
                    camera_local,
                    has_audio=probe.has_audio,
                    semantic_crop_pulses=effects,
                    **gb._canvas_kwargs(gb.canvas_for_orientation(variant.get("orientation"))),
                )
                clean_local = os.path.join(tmpdir, "authored_camera_final.mp4")
                _finalize_camera_base(camera_local, clean_local)
            if presentation.get("custom_effects"):
                from app.tasks.custom_effects_render import reapply_persisted_custom_effect

                clean_local, cleared = reapply_persisted_custom_effect(
                    clean_local, presentation, tmpdir
                )
                if cleared:
                    raise ValueError("authored custom effect could not be rendered")
            final_key = gb._variant_storage_key(
                job_id, f"authored_{variant_id}_{uuid.uuid4().hex}.mp4", generation
            )
            final_local, matte_path = gb._compose_subtitled_final(
                clean_local,
                presentation,
                tmpdir,
                job_id=job_id,
                variant_id=variant_id,
                upload_key_base=final_key,
                created_storage_paths=created,
            )
            output_url = storage.upload_public_read(final_local, final_key)
            created.append(final_key)
        reapply = gb._will_reapply_media_layers(variant)
        patch = {
            "base_video_path": base_key,
            "base_video_stale": False,
            "video_path": final_key,
            "output_url": output_url,
            "duration_s": duration,
            "render_status": "rendering" if reapply else "ready",
            "ok": True,
            "error": None,
            "render_finished_at": datetime.utcnow().isoformat() + "Z",
            "subject_matte_path": matte_path,
            "pre_media_overlay_video_path": None,
            "pre_sfx_video_path": None,
            **(
                {
                    "carousel_inserted_duration_s": carousel_sink["duration_s"],
                    "carousel_insertion_base_s": carousel_sink.get("insertion_base_s"),
                    "carousel_ripple_duration_s": carousel_sink.get("ripple_duration_s"),
                }
                if carousel_sink
                else {}
            ),
            **gb._creator_layer_cache_patch(
                visual_blocks_cache_path=visual_cache,
                motion_cache_path=motion_cache,
                motion_base_source_path=motion_source,
                motion_identity=motion_identity,
            ),
        }
        accepted = gb._update_variant_entry(
            job_id,
            variant_id,
            patch,
            expected_render_gen_id=generation,
            outcome="authored_complete",
            cleanup_followup="media_layers",
            accepted_state=terminal_state,
        )
        if (
            accepted
            and reapply
            and not gb._reapply_user_media_layers(
                job_id=job_id, variant_id=variant_id, expected_render_gen_id=generation
            )
        ):
            gb._update_variant_entry(
                job_id,
                variant_id,
                {"render_status": "ready"},
                expected_render_gen_id=generation,
                outcome="authored_reapply_noop",
                cleanup_followup="none",
            )
    finally:
        if not accepted and not terminal_state["accepted"]:
            gb._delete_cancelled_job_objects(job_id, created)
