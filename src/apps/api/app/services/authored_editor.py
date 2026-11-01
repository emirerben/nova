"""An explicit creator timeline, independent of the original generated cut.

Only a supported, baseline-checked draft restore may enter this mode. The mode
is sticky: later renders must never re-run an archetype's original assignment.
"""

from __future__ import annotations

import copy
import re
from typing import Any

import structlog
from fastapi import HTTPException

from app.schemas.editor_window import below_editor_minimum, snap_to_editor_minimum

AUTHORED_TIMELINE_MODE = "authored"

USER_SONG_CLIP_EDIT_UNSUPPORTED = (
    "Removing clips from a video with your own song isn't supported yet."
)


def is_authored_timeline(variant: dict) -> bool:
    return variant.get("editor_timeline_mode") == AUTHORED_TIMELINE_MODE


def explicit_authored_slots(variant: dict) -> list[dict]:
    """No AI, previous recipe, or composite fallback is permitted here."""
    timeline = variant.get("user_timeline")
    rows = timeline.get("slots") if isinstance(timeline, dict) else None
    if not isinstance(rows, list):
        raise ValueError("authored timeline is missing")
    active = [
        copy.deepcopy(row) for row in rows if isinstance(row, dict) and not row.get("removed")
    ]
    if not active:
        raise ValueError("authored timeline is empty")
    return active


def prepare_authored_editor_commit(
    job: Any,
    variant_id: str,
    payload: Any,
    *,
    validation_arguments: dict[str, Any],
    phone_sfx_catalog_paths: dict[str, str] | None = None,
    creator_brief_binding: dict | None = None,
) -> dict:
    """Prepare on a staging copy supplied by the saved-draft transaction.

    The caller has loaded the creator-owned immutable snapshot and checked its
    generation. The ordinary commit validator still owns every section, source
    bound, and generation stamp. Only eligibility dispatch recognizes this mode.
    """
    from app.routes.generative_jobs import (
        _find_variant,
        _prepare_editor_commit,
        _require_caption_animation_client,
        _require_clip_tilt_gates,
        require_editor_clock_reader,
        variant_render_baseline,
    )
    from app.services.phone_editor import _StagedJob

    original_job = job
    job = _StagedJob(job)
    variant = _find_variant(job, variant_id)
    if variant is None:
        raise HTTPException(404, detail="Variant not found")
    if payload.base_generation != variant_render_baseline(variant):
        raise HTTPException(409, detail="baseline_conflict")
    require_editor_clock_reader(job, variant)
    _require_caption_animation_client(job, variant, payload)
    _require_clip_tilt_gates(job, variant, payload)
    if not is_authored_timeline(variant) and (
        payload.timeline_slots is None or not any(not row.removed for row in payload.timeline_slots)
    ):
        raise HTTPException(422, detail={"code": "TIMELINE_EMPTY"})
    if payload.timeline_slots is not None and not any(
        not row.removed for row in payload.timeline_slots
    ):
        raise HTTPException(422, detail={"code": "TIMELINE_EMPTY"})
    # A cloud composite is a display-only track. Phone speech segments,
    # however, map to exact original windows; retain only server-derived
    # identities when deleting another segment from that locked track.
    from app.routes.generative_jobs import editor_deletion_timeline

    canonical = {row.get("slot_id"): row for row in editor_deletion_timeline(job, variant)}
    for row in payload.timeline_slots or []:
        if row.removed or not row.slot_id:
            continue
        if row.slot_id.startswith("native-composite-base"):
            raise HTTPException(422, detail={"code": "authored_source_required"})
        if row.slot_id.startswith("native-phone-talking-source"):
            baseline = canonical.get(row.slot_id)
            if baseline is None or row.clip_index != baseline["clip_index"]:
                raise HTTPException(409, detail={"code": "TIMELINE_STALE"})
    if (payload.timeline_slots is not None or variant.get("speech_timing_basis")) and any(
        str(key).startswith("native-phone-talking-source") for key in canonical
    ):
        from app.pipeline.speech_cut_state import (
            SPEECH_EDITOR_LANES,
            authored_speech_texts,
            project_speech_basis,
            speech_basis_entry,
            sync_speech_basis,
        )
        from app.services.editor_deletions import canonical_caption_rows

        variant["editor_timeline_mode"] = AUTHORED_TIMELINE_MODE
        resolved = (
            resolve_authored_phone_slots(job, variant, payload.timeline_slots)
            if payload.timeline_slots is not None
            else list(canonical.values())
        )
        old_slots = list(canonical.values())
        basis = speech_timing_basis_for_job(job, variant, old_slots)
        captions_enabled = variant.get("captions_enabled", True)
        if payload.caption_meta is not None and payload.caption_meta.enabled is not None:
            captions_enabled = payload.caption_meta.enabled
        unresolved = False
        projected_cues = project_speech_basis(
            basis.get("caption_cues", []), resolved, captions=True
        )
        authored_captions = payload.caption_cues is not None and (
            canonical_caption_rows(payload.caption_cues) != projected_cues
            or any(row.kind == "caption_cue" for row in payload.deletions)
        )
        if authored_captions:
            # A creator-authored full lane replacement owns its content on this
            # exact submitted timeline. Merely sending the automatically rebased
            # lane does not confirm missing source words (the native client
            # sends that lane whenever source geometry makes it dirty).
            basis["source_windows"] = speech_basis_entry({}, resolved)["slots"]
        elif captions_enabled and (variant.get("caption_cues") or basis.get("caption_cues")):
            coverage = basis.get("source_windows") or old_slots
            for row in resolved:
                if not str(row.get("slot_id")).startswith("native-phone-talking-source"):
                    continue
                start = float(row["in_s"])
                nominal_end = start + float(row["duration_s"]) * float(
                    row.get("playback_rate") or 1
                )
                end = min(nominal_end, float(row.get("source_end_s") or nominal_end))
                if not any(
                    w.get("slot_id") == row["slot_id"]
                    and start >= float(w["in_s"]) - 1e-6
                    and end
                    <= float(w["in_s"])
                    + float(w["duration_s"]) * float(w.get("playback_rate") or 1)
                    + 1e-6
                    for w in coverage
                ):
                    unresolved = True
        variant["speech_timing_unresolved"] = unresolved
        if payload.text_elements is not None:
            payload = payload.model_copy(
                update={"text_elements": authored_speech_texts(payload.text_elements)}
            )
        for field in SPEECH_EDITOR_LANES:
            current = variant.get(field)
            if field == "text_elements" and isinstance(current, list):
                current = authored_speech_texts(current)
            if not isinstance(current, list):
                continue
            if field == "caption_cues":
                current = canonical_caption_rows(current)
            entries = sync_speech_basis(
                basis.get(field), current, old_slots, captions=field == "caption_cues"
            )
            supplied = getattr(payload, field, None)
            if supplied is not None:
                if field == "caption_cues":
                    supplied = canonical_caption_rows(supplied)
                entries = sync_speech_basis(
                    entries,
                    supplied,
                    resolved,
                    captions=field == "caption_cues",
                    deleted_ids={
                        row.id
                        for row in payload.deletions
                        if row.kind
                        == {
                            "caption_cues": "caption_cue",
                            "text_elements": "text",
                            "sound_effects": "sound_effect",
                            "media_overlays": "media_overlay",
                            "visual_blocks": "visual_block",
                            "motion_scenes": "motion_scene",
                            "camera_effects": "camera_effect",
                            "drawings": "drawing",
                        }[field]
                    },
                )
            basis[field] = entries
            # Only omitted lanes need server projection. A present lane is an
            # authored replacement on the submitted output clock, never remap twice.
            if supplied is None:
                variant[field] = project_speech_basis(
                    entries, resolved, captions=field == "caption_cues"
                )
        variant["speech_timing_basis"] = basis
    variant["editor_timeline_mode"] = AUTHORED_TIMELINE_MODE
    if variant.get("render_destination") == "device":
        from app.services.phone_editor import prepare_phone_editor_commit

        prep = prepare_phone_editor_commit(
            job,
            variant_id,
            prepare=lambda staged: _prepare_editor_commit(
                staged, variant_id, payload, **validation_arguments
            ),
            learning_sections=payload.model_fields_set,
            sfx_catalog_paths=phone_sfx_catalog_paths,
            creator_brief_binding=creator_brief_binding,
        )
    else:
        prep = _prepare_editor_commit(job, variant_id, payload, **validation_arguments)
    prep["authored_timeline"] = True
    for name, value in vars(job).items():
        if name != "_job":
            setattr(original_job, name, value)
    return prep


# The phone clamps its own windows with this slack (KriaSourceWindowGeometry.boundsTolerance,
# also the guided-v2 rule): a window it considers legal may end up to 50 ms past the source.
log = structlog.get_logger(__name__)

SOURCE_BOUNDS_TOLERANCE_S = 0.05
_TIMELINE_FRAME_S = 1 / 30


def _canonical_output_duration(source_span: float, rate: float) -> float:
    """Return the authored output clock on the shared 30 fps timeline grid."""
    return round((source_span / rate) * 30) / 30


def _source_end_for_authored_slot(
    slot: Any,
    *,
    old: dict,
    source: dict,
    duration: float,
    rate: float,
    minimum_duration: float,
) -> tuple[float, float | None]:
    """Resolve the optional exact source edge without changing legacy windows.

    ``source_end_s`` is an absolute source-time edge.  It is deliberately
    absent from old payloads, whose ``duration_s * playback_rate`` behavior
    must remain unchanged.  A reopened exact window can omit the field only
    when its complete geometry is unchanged; a trim must never revive an old
    endpoint.
    """
    import math

    supplied = "source_end_s" in slot.model_fields_set and slot.source_end_s is not None
    unchanged = (
        slot.slot_id is not None
        and slot.clip_index == old.get("clip_index")
        and math.isclose(float(slot.in_s), float(old.get("in_s", -1)), abs_tol=1e-6)
        and math.isclose(duration, float(old.get("duration_s", -1)), abs_tol=1e-6)
        and math.isclose(rate, float(old.get("playback_rate") or 1), abs_tol=1e-6)
    )
    source_end = slot.source_end_s if supplied else old.get("source_end_s") if unchanged else None
    if source_end is None:
        return duration, None
    if source.get("kind") != "video":
        raise HTTPException(422, detail={"code": "phone_edit_unsupported"})
    source_end = float(source_end)
    bound = source.get("duration_s")
    if (
        not math.isfinite(source_end)
        or source_end <= slot.in_s
        or bound is None
        or source_end > float(bound)
    ):
        raise HTTPException(422, detail={"code": "TIMELINE_INVALID_WINDOW"})
    exact_output = (source_end - slot.in_s) / rate
    if (
        below_editor_minimum(exact_output, minimum_duration)
        or abs(exact_output - duration) > _TIMELINE_FRAME_S + 1e-9
    ):
        raise HTTPException(422, detail={"code": "TIMELINE_INVALID_WINDOW"})
    canonical = _canonical_output_duration(source_end - slot.in_s, rate)
    if below_editor_minimum(canonical, minimum_duration):
        raise HTTPException(422, detail={"code": "TIMELINE_INVALID_WINDOW"})
    return canonical, source_end


# Slot ids the chat compile minted for clips that had none (``cs-<client_state_id[:12]>-<index>``,
# removed in #1698). They were never saved, but chat drafts stored before that fix still carry them
# into the editor, and every Save then refused them as a stale slot. "Reload and keep my edits"
# re-applies the same local timeline, so the creator could never get out (KRI-773). Only this exact
# server-minted shape is read as "a new placement"; any other unknown id stays a stale-slot 409.
_LEGACY_MINTED_SLOT_ID = re.compile(r"^cs-[A-Za-z0-9_-]{1,12}-\d+$")


def is_legacy_minted_slot_id(slot_id: str | None) -> bool:
    return slot_id is not None and _LEGACY_MINTED_SLOT_ID.match(slot_id) is not None


# Overshoot at or below this was always persisted untouched (frame rounding); keep it so.
_SOURCE_BOUNDS_EXACT_S = 0.001


def _out_of_bounds_error(
    slot: Any,
    *,
    order: int,
    duration: float | None,
    rate: float,
    bound: Any,
    reason: str,
) -> HTTPException:
    """`TIMELINE_OUT_OF_BOUNDS` with the numbers needed to diagnose it from the 422 log."""
    source_duration = float(bound) if bound is not None else None
    overshoot = (
        round(float(slot.in_s) + float(duration) * rate - source_duration, 4)
        if source_duration is not None and duration is not None
        else None
    )
    return HTTPException(
        422,
        detail={
            "code": "TIMELINE_OUT_OF_BOUNDS",
            "reason": reason,
            "slot_id": slot.slot_id,
            "slot_order": order,
            "clip_index": slot.clip_index,
            "in_s": round(float(slot.in_s), 4),
            "duration_s": round(float(duration), 4) if duration is not None else None,
            "rate": round(rate, 4),
            "source_duration_s": round(source_duration, 4) if source_duration is not None else None,
            "overshoot_s": overshoot,
        },
    )


def _fit_window_to_source(
    slot: Any,
    *,
    order: int,
    duration: float,
    rate: float,
    bound: Any,
    minimum_duration: float,
) -> float:
    """Return the slot duration, shortened if the window overshoots the source by <= 50 ms.

    A window the phone considers legal can end a few ms past the catalog duration; shorten it
    so the renderer never reads past the source end. Anything further out is refused.
    """
    import math

    if bound is None:
        raise _out_of_bounds_error(
            slot, order=order, duration=duration, rate=rate, bound=bound, reason="source_unmeasured"
        )
    source_duration = float(bound)
    end = slot.in_s + duration * rate
    if end <= source_duration + _SOURCE_BOUNDS_EXACT_S:
        return duration
    if slot.in_s >= source_duration or end > source_duration + SOURCE_BOUNDS_TOLERANCE_S:
        raise _out_of_bounds_error(
            slot,
            order=order,
            duration=duration,
            rate=rate,
            bound=bound,
            reason="source_window_too_short",
        )
    fitted = math.floor((source_duration - slot.in_s) / rate * 1e6) / 1e6
    if below_editor_minimum(fitted, minimum_duration):
        raise _out_of_bounds_error(
            slot,
            order=order,
            duration=duration,
            rate=rate,
            bound=bound,
            reason="source_window_too_short",
        )
    return fitted


def resolve_authored_phone_slots(
    job: Any, variant: dict, slots: list[Any], *, preserve_intent: bool = False
) -> list[dict]:
    """Resolve explicit placements against the original/admitted receipt catalog."""
    import math
    import uuid

    from app.routes.generative_jobs import _TIMELINE_MAX_SLOTS, TIMELINE_MAX_TOTAL_S
    from app.services.phone_editor_sources import phone_editor_source_revision

    catalog = (phone_editor_source_revision(job, variant) or {}).get("sources") or []
    from app.routes.generative_jobs import editor_deletion_timeline

    current = editor_deletion_timeline(job, variant)
    known = {row.get("slot_id") for row in current}
    baseline = {row.get("slot_id"): row for row in current}
    resolved = []
    total = 0.0
    for slot in slots:
        if slot.removed:
            continue
        if slot.slot_id is not None and slot.slot_id not in known:
            if not is_legacy_minted_slot_id(slot.slot_id):
                log.warning(
                    "editor_commit_stale_slot",
                    slot_id=slot.slot_id,
                    clip_index=slot.clip_index,
                    known_slots=len(known),
                )
                raise HTTPException(409, detail={"code": "TIMELINE_STALE"})
            slot = slot.model_copy(update={"slot_id": None})  # a new placement (KRI-773)
        if not 0 <= slot.clip_index < len(catalog):
            raise HTTPException(422, detail={"code": "TIMELINE_UNKNOWN_CLIP"})
        source = catalog[slot.clip_index]
        old = baseline.get(slot.slot_id) or baseline.get(slot.parent_segment_id) or {}
        duration = slot.duration_s
        # A partial stale Save may omit newer speed fields. Preserve a saved
        # authored rate in that case; explicit null remains the creator's 1x
        # reset. New placements have no baseline and therefore retain 1x.
        if "playback_rate" in slot.model_fields_set:
            rate = slot.playback_rate or 1.0
        else:
            rate = float(old.get("playback_rate") or 1.0)
        if slot.duration_beats is not None or duration is None:
            raise HTTPException(422, detail={"code": "authored_seconds_required"})
        minimum_duration = (
            0.001
            if slot.slot_id
            and slot.slot_id.startswith("native-phone-talking-source")
            and slot.slot_id in known
            else 0.1
        )
        if (
            not all(math.isfinite(value) for value in (slot.in_s, duration, rate))
            or slot.in_s < 0
            or below_editor_minimum(duration, minimum_duration)
        ):
            raise HTTPException(422, detail={"code": "TIMELINE_INVALID_WINDOW"})
        duration = snap_to_editor_minimum(duration, minimum_duration)
        if source["kind"] == "image" and (
            not math.isclose(rate, 1.0) or slot.source_end_s is not None
        ):
            raise HTTPException(422, detail={"code": "phone_edit_unsupported"})
        duration, source_end = _source_end_for_authored_slot(
            slot,
            old=old,
            source=source,
            duration=duration,
            rate=rate,
            minimum_duration=minimum_duration,
        )
        bound = source.get("duration_s")
        if source["kind"] == "video" and source_end is None and not preserve_intent:
            duration = _fit_window_to_source(
                slot,
                order=len(resolved),
                duration=duration,
                rate=rate,
                bound=bound,
                minimum_duration=minimum_duration,
            )
        if slot.look_preset not in (None, "none") or slot.look_adjustments is not None:
            raise HTTPException(422, detail={"code": "phone_edit_unsupported"})
        total += duration
        tilt = slot.tilt if "tilt" in slot.model_fields_set else old.get("tilt")
        resolved.append(
            {
                **slot.model_dump(mode="json", exclude={"tilt"}),
                **({"tilt": tilt} if tilt is not None else {}),
                "slot_id": slot.slot_id or str(uuid.uuid4()),
                "source_gcs_path": source["gcs_path"],
                "source_duration_s": bound,
                "source_kind": source["kind"],
                "media_id": source["media_id"],
                "order": len(resolved),
                "duration_s": duration,
                "source_end_s": source_end,
                "playback_rate": (
                    rate
                    if "playback_rate" in slot.model_fields_set
                    or old.get("playback_rate") is not None
                    else None
                ),
                "source_start_locked": bool(
                    slot.source_start_locked or old.get("source_start_locked")
                ),
            }
        )
    if not resolved:
        raise HTTPException(422, detail={"code": "TIMELINE_EMPTY"})
    if len(resolved) > _TIMELINE_MAX_SLOTS or total > TIMELINE_MAX_TOTAL_S:
        raise HTTPException(422, detail={"code": "TIMELINE_TOO_LONG"})
    return resolved


def authored_cloud_source_catalog(job: Any, variant: dict) -> list[dict]:
    """Stable guided identities first, then owned job-pool additions in append order."""
    from app.pipeline.image_clip import is_image_file
    from app.routes.generative_jobs import _guided_v2_revision, _timeline_parts

    candidates = job.all_candidates or {}
    revision = (
        _guided_v2_revision(job, variant)
        if variant.get("resolved_archetype") == "guided_story"
        else None
    )
    if revision:
        catalog = [
            {
                "source_gcs_path": row["gcs_path"],
                "source_duration_s": row.get("duration_s"),
                "source_kind": row.get("kind", "video"),
                "media_id": row.get("media_id"),
                "source_generation": row.get("generation"),
            }
            for row in revision["sources"]
        ]
        known_paths = {row["source_gcs_path"] for row in catalog}
        for path in candidates.get("clip_paths") or []:
            if path not in known_paths:
                catalog.append(
                    {
                        "source_gcs_path": path,
                        "source_kind": "image" if is_image_file(path) else "video",
                    }
                )
                known_paths.add(path)
    else:
        catalog = [
            {"source_gcs_path": path, "source_kind": "image" if is_image_file(path) else "video"}
            for path in (job.all_candidates or {}).get("clip_paths") or []
        ]
        ai, user, _ = _timeline_parts(variant)
        for row in [*ai, *user]:
            index = row.get("clip_index")
            if isinstance(index, int) and 0 <= index < len(catalog):
                for key in ("source_gcs_path", "source_duration_s", "source_kind"):
                    if row.get(key) is not None:
                        catalog[index][key] = row[key]
    metadata = candidates.get("editor_source_metadata") or {}
    for row in catalog:
        receipt = metadata.get(row["source_gcs_path"]) or {}
        for key in ("source_kind", "source_generation", "source_duration_s"):
            if row.get(key) is None and receipt.get(key) is not None:
                row[key] = receipt[key]
        if receipt.get("source_kind"):
            row["source_kind"] = receipt["source_kind"]
    return catalog


def authored_cloud_timeline_clips(
    job: Any, variant: dict, *, sign_url, image_preview_paths: dict | None = None
) -> list[dict]:
    """Project exactly the same source indexes accepted by authored Save."""
    from app.routes.generative_jobs import PLAYBACK_URL_TTL_MIN, _native_timeline_source

    used = {
        row.get("clip_index")
        for row in (variant.get("user_timeline") or {}).get("slots") or []
        if not row.get("removed")
    }
    result = []
    for index, source in enumerate(authored_cloud_source_catalog(job, variant)):
        path = source["source_gcs_path"]
        preview_path = (image_preview_paths or {}).get(path, path)
        try:
            url = sign_url(preview_path, PLAYBACK_URL_TTL_MIN)
        except Exception:
            url = None
        result.append(
            {
                "clip_index": index,
                "signed_url": url,
                "native_source": _native_timeline_source(
                    job,
                    path,
                    source.get("media_id") or f"clip-{index}",
                    sign_url=sign_url,
                    variant=variant,
                ),
                "duration_s": source.get("source_duration_s"),
                "kind": source["source_kind"],
                "media_id": source.get("media_id"),
                "generation": source.get("source_generation"),
                "used": index in used,
            }
        )
    return result


def resolve_authored_cloud_slots(
    job: Any, variant: dict, slots: list[Any], *, preserve_intent: bool = False
) -> list[dict]:
    """Preserve exact authored source identities and per-occurrence geometry."""
    import math
    import uuid

    from app import storage
    from app.config import settings
    from app.pipeline.look_presets import (
        EDIT_WIDE_LOOK_PRESETS,
        normalize_look_adjustments,
        normalize_look_preset,
    )
    from app.routes.generative_jobs import (
        TIMELINE_MAX_TOTAL_S,
        _durable_sources_prefix,
        _timeline_parts,
        editor_deletion_timeline,
    )

    baseline_rows = editor_deletion_timeline(job, variant)
    baseline = {row.get("slot_id"): row for row in baseline_rows if row.get("slot_id")}
    catalog = authored_cloud_source_catalog(job, variant)
    _, _, grid = _timeline_parts(variant)
    cursor = 0
    resolved = []
    total = 0.0
    for slot in slots:
        if slot.removed:
            continue
        if slot.slot_id is not None and slot.slot_id not in baseline:
            if not is_legacy_minted_slot_id(slot.slot_id):
                log.warning(
                    "editor_commit_stale_slot",
                    slot_id=slot.slot_id,
                    clip_index=slot.clip_index,
                    known_slots=len(baseline),
                )
                raise HTTPException(409, detail={"code": "TIMELINE_STALE"})
            slot = slot.model_copy(update={"slot_id": None})  # a new placement (KRI-773)
        if not 0 <= slot.clip_index < len(catalog):
            raise HTTPException(422, detail={"code": "TIMELINE_UNKNOWN_CLIP"})
        old = baseline.get(slot.slot_id) or {}
        source = catalog[slot.clip_index]
        # Absent means preserve; explicit null resets crop/rate/layout.
        controls = {}
        for key, default in (
            ("playback_rate", 1.0),
            ("source_crop", None),
            ("layout", "fullscreen"),
        ):
            value = getattr(slot, key) if key in slot.model_fields_set else old.get(key)
            controls[key] = value if value is not None else default
        rate = float(controls["playback_rate"])
        duration = slot.duration_s
        if slot.duration_beats is not None:
            unchanged = (
                old.get("duration_beats") == slot.duration_beats and old.get("in_s") == slot.in_s
            )
            if unchanged and old.get("duration_s") is not None:
                duration = float(old["duration_s"])
            elif slot.duration_beats > 0 and cursor + slot.duration_beats < len(grid):
                duration = grid[cursor + slot.duration_beats] - grid[cursor]
            else:
                raise HTTPException(422, detail={"code": "TIMELINE_BEATS_EXHAUSTED"})
            cursor += slot.duration_beats
        if (
            duration is None
            or not all(math.isfinite(v) for v in (slot.in_s, duration, rate))
            or slot.in_s < 0
            or below_editor_minimum(duration)
        ):
            raise HTTPException(422, detail={"code": "TIMELINE_INVALID_WINDOW"})
        duration = snap_to_editor_minimum(duration)
        bound = source.get("source_duration_s")
        if (
            not preserve_intent
            and source["source_kind"] == "video"
            and bound is not None
            and slot.in_s + duration * rate > float(bound) + SOURCE_BOUNDS_TOLERANCE_S
        ):
            raise _out_of_bounds_error(
                slot,
                order=len(resolved),
                duration=duration,
                rate=rate,
                bound=bound,
                reason="source_window_too_short",
            )
        preset = normalize_look_preset(
            slot.look_preset if slot.look_preset is not None else old.get("look_preset")
        )
        if (
            preset in EDIT_WIDE_LOOK_PRESETS[1:]
            and not settings.edit_wide_looks_enabled
            and preset != normalize_look_preset(old.get("look_preset"))
        ):
            raise HTTPException(422, detail={"code": "LOOK_PRESET_NOT_AVAILABLE"})
        adjustments = (
            slot.look_adjustments
            if "look_adjustments" in slot.model_fields_set
            else old.get("look_adjustments")
            if preset == normalize_look_preset(old.get("look_preset"))
            else None
        )
        normalized = normalize_look_adjustments(preset, adjustments)
        resolved.append(
            {
                **slot.model_dump(mode="json"),
                **source,
                **controls,
                "source_start_locked": bool(
                    slot.source_start_locked or old.get("source_start_locked")
                ),
                "slot_id": slot.slot_id or str(uuid.uuid4()),
                "duration_s": float(duration),
                "look_preset": preset,
                "look_adjustments": normalized.model_dump() if normalized else None,
                "order": len(resolved),
            }
        )
        total += duration
    if not resolved:
        raise HTTPException(422, detail={"code": "TIMELINE_EMPTY"})
    if total > TIMELINE_MAX_TOTAL_S:
        raise HTTPException(422, detail={"code": "TIMELINE_TOO_LONG"})
    prefix = _durable_sources_prefix(job)
    for path in {row["source_gcs_path"] for row in resolved}:
        if path.startswith(prefix) and not storage.object_exists(path):
            if not preserve_intent:
                raise HTTPException(422, detail={"code": "sources_expired"})
            # Identity is already admitted from this owned job's catalogue.
            # Missing bytes prevent rendering, not persistence of that identity.
            for row in resolved:
                if row["source_gcs_path"] == path:
                    row["source_unavailable"] = True
    return resolved


def speech_timing_basis_for_job(job: Any, variant: dict, slots: list[dict]) -> dict:
    """Seed source anchors from existing, generation-checked speech evidence only.

    Historical jobs without a full preflight transcript still retain every
    caption they have. No read/Save may invoke transcription to fill a gap.
    """
    from app.pipeline.speech_cut_state import initial_speech_timing_basis, speech_basis_entry

    basis = initial_speech_timing_basis(variant, slots)
    basis.setdefault("source_windows", speech_basis_entry({}, slots)["slots"])
    if variant.get("speech_timing_basis"):
        return basis
    from app.pipeline.speech_cleanup_apply import (
        SpeechCleanupSnapshotError,
        hydrate_job_speech_cleanup_snapshot,
    )
    from app.services.phone_editor_sources import phone_editor_source_revision

    try:
        snapshot = hydrate_job_speech_cleanup_snapshot(job.assembly_plan)
        catalog = (phone_editor_source_revision(job, variant) or {})["sources"]
        source = catalog[0]
        snapshot.require_source(
            kind="embedded_spine",
            storage_path=source["gcs_path"],
            window_start_s=0,
            window_duration_s=float(source["duration_s"]),
        )
        if snapshot.generation != source["generation"]:
            return basis
    except (SpeechCleanupSnapshotError, KeyError, IndexError, TypeError, ValueError):
        return basis
    basis["source_windows"] = [
        {
            "slot_id": row["slot_id"],
            "clip_index": row["clip_index"],
            "in_s": 0,
            "duration_s": float(source["duration_s"]),
        }
        for row in slots
        if row.get("clip_index") == 0
    ]
    # Preserve existing corrected/creator-authored captions. Extra anchors only
    # cover source words outside each occurrence's initial visible source window.
    from app.pipeline.captions import PHONE_CUE_BUDGET, build_plain_cues

    groups = []
    for slot in slots:
        if slot.get("clip_index") != 0 or not str(slot.get("slot_id")).startswith(
            "native-phone-talking-source"
        ):
            continue
        start = float(slot["in_s"])
        end = start + float(slot["duration_s"]) * float(slot.get("playback_rate") or 1)
        groups.append(
            {
                "slot": {
                    "slot_id": slot["slot_id"],
                    "clip_index": 0,
                    "in_s": 0,
                    "duration_s": float(source["duration_s"]),
                    "transition_after": "cut",
                },
                "initial_start_s": start,
                "initial_end_s": end,
            }
        )
    # One shared source transcript plus one small occurrence descriptor. Never
    # clone the full transcript for each possible source-window placement.
    # Bound cue planning too: an unpunctuated long take is one sentence to the
    # general caption balancer. Small word batches keep editor reads linear.
    words = snapshot.source_words()
    source_captions = [
        cue
        for offset in range(0, len(words), PHONE_CUE_BUDGET.max_words)
        for cue in build_plain_cues(
            words[offset : offset + PHONE_CUE_BUDGET.max_words],
            attach_words=True,
            budget=PHONE_CUE_BUDGET,
        )
    ]
    basis.setdefault("caption_cues", []).append(
        {
            "source_captions": source_captions,
            "source_groups": groups,
            "suppressed_ids": [],
        }
    )
    return basis
