"""Apply the approved structured composition before the initial device request is pinned."""

from __future__ import annotations

import copy
from types import SimpleNamespace

from app.kria.device_render import make_device_request
from app.pipeline.phone_authored_timeline import compile_phone_authored_timeline
from app.services.authored_editor import resolve_authored_phone_slots
from app.services.phone_editor_sources import phone_editor_source_revision
from app.services.phone_rollout import split_screen_supported, validate_phone_pilot_recipe


def compile_initial_split_screen(job, request):
    raw = ((getattr(job, "all_candidates", None) or {}).get("creator_strategy") or {}).get(
        "split_screen"
    )
    if not raw or request.identity.recipe_revision != 1:
        return request
    if not split_screen_supported(require_client=False):
        raise ValueError("approved split screen is unavailable on the phone")
    variants = (job.assembly_plan or {}).get("variants") or []
    variant = next(row for row in variants if row.get("variant_id") == request.identity.variant_id)
    if (variant.get("user_timeline") or {}).get("split_screen_sections"):
        return request
    from app.routes.generative_jobs import TimelineSlotEdit
    from app.services.initial_split_screen import apply_initial_split_screen

    staged = SimpleNamespace(
        assembly_plan=copy.deepcopy(job.assembly_plan), all_candidates=job.all_candidates
    )
    staged_variant = next(
        row
        for row in staged.assembly_plan["variants"]
        if row["variant_id"] == request.identity.variant_id
    )
    catalog = (phone_editor_source_revision(staged, staged_variant) or {}).get("sources") or []
    by_media = {source["media_id"]: (index, source) for index, source in enumerate(catalog)}
    manifest = {asset.id: asset for asset in request.recipe.asset_manifest.assets}
    rows = []
    for track in request.recipe.tracks:
        if track.kind != "video":
            continue
        for clip in track.clips:
            asset = manifest[clip.source_asset_id]
            media_id = getattr(asset, "media_id", None) or getattr(asset, "visual_id", None)
            if media_id not in by_media:
                raise ValueError("initial split screen source lacks an approved source receipt")
            index, source = by_media[media_id]
            background = request.recipe.speaker_background
            is_subject = background is not None and (
                clip.id in background.subject_clip_ids
                if background.subject_clip_ids is not None
                else track.id == background.subject_track_id
            )
            if background is None:
                # The primary Talking track is the speaker even before a background is added.
                is_subject = (
                    staged_variant.get("resolved_archetype") in {"subtitled", "talking_head"}
                    and track.id == "subtitled"
                    and getattr(asset, "kind", None) == "original"
                )
            audio_clip = next(
                (
                    candidate
                    for audio_track in request.recipe.tracks
                    if audio_track.kind == "audio"
                    for candidate in audio_track.clips
                    if getattr(manifest[candidate.source_asset_id], "source_media_id", None)
                    == media_id
                    and abs(candidate.timeline_start - clip.timeline_start) < 1e-6
                    and abs(candidate.source_start - clip.source_start) < 1e-6
                    and abs(candidate.source_duration - clip.source_duration) < 1e-6
                ),
                clip,
            )
            row = {
                "speaker_subject": is_subject,
                "speaker_frame_override": clip.speaker_frame_override.model_dump(exclude_none=True)
                if clip.speaker_frame_override
                else None,
                "slot_id": clip.id,
                "clip_index": index,
                "media_id": media_id,
                "media_kind": source["kind"],
                "source_duration_s": source.get("duration_s"),
                "in_s": clip.source_start,
                "duration_s": clip.source_duration / clip.rate + (clip.hold_duration or 0),
                "source_end_s": clip.source_start + clip.source_duration
                if source["kind"] == "video"
                else None,
                "freeze_hold_s": clip.hold_duration,
                "freeze_frame_s": clip.source_start + clip.source_duration
                if clip.hold_duration
                else None,
                "playback_rate": clip.rate,
                "muted": audio_clip.volume == 0,
                "source_audio_volume": audio_clip.volume,
                "audio_fade_in": audio_clip.audio_fade_in,
                "audio_fade_out": audio_clip.audio_fade_out,
                "media_transform": clip.transform.model_dump(mode="json"),
                "layout": clip.still_layout,
                "look_preset": clip.look or "none",
                "source_start_locked": True,
                "source_crop": clip.source_crop.model_dump() if clip.source_crop else None,
                "_initial_start": clip.timeline_start,
            }
            if clip.transform.rotation_degrees or clip.backdrop:
                row["tilt"] = {
                    "rotation_deg": clip.transform.rotation_degrees,
                    "zoom": 1,
                    "backdrop": clip.backdrop.model_dump() if clip.backdrop else {"kind": "blank"},
                }
            if rows and clip.transition is not None:
                rows[-1].update(
                    transition_after={
                        "crossfade": "crossfade",
                        "fade_black": "dip_to_black",
                        "fade_white": "flash",
                    }[clip.transition.kind],
                    transition_duration_s=clip.transition.duration,
                )
            rows.append(row)
    rows.sort(key=lambda row: row.pop("_initial_start"))
    staged_variant.update(editor_timeline_mode="authored", user_timeline={"slots": rows})
    source_rows = [
        {
            "clip_index": index,
            "media_id": source["media_id"],
            "media_kind": source["kind"],
            "source_duration_s": source.get("duration_s"),
            "in_s": 0,
            "source_audio_volume": 1.0,
            "duration_s": request.recipe.duration,
        }
        for index, source in enumerate(catalog)
    ]
    source_rows = [
        {**row, **next((old for old in rows if old["media_id"] == row["media_id"]), {})}
        for row in source_rows
    ]
    projected = apply_initial_split_screen(staged, staged_variant, raw, source_rows=source_rows)
    staged_variant.clear()
    staged_variant.update(projected)
    staged_variant["user_timeline"]["slots"] = resolve_authored_phone_slots(
        staged,
        staged_variant,
        [TimelineSlotEdit.model_validate(row) for row in projected["user_timeline"]["slots"]],
        preserve_intent=True,
    )
    recipe = compile_phone_authored_timeline(staged, staged_variant, request.recipe)
    validate_phone_pilot_recipe(recipe, allow_editor_media=True)
    updated = make_device_request(
        job_id=request.identity.job_id,
        variant_id=request.identity.variant_id,
        revision=request.identity.recipe_revision,
        recipe=recipe,
    )
    job.assembly_plan = staged.assembly_plan
    return updated
