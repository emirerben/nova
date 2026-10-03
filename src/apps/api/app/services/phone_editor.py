"""Atomically project validated editor state into the next immutable phone recipe."""

from __future__ import annotations

import copy
from collections.abc import Callable, Mapping
from typing import Any

import structlog
from fastapi import HTTPException

from app.config import settings
from app.kria.device_render import make_device_request
from app.kria.recipes_v2 import EditRecipeV2
from app.pipeline.guided_story import (
    GuidedStoryError,
    GuidedStoryExecutionPlan,
    compile_guided_runtime_plan,
    song_reference_variant_fields,
)
from app.pipeline.phone_captions import caption_look_from_variant
from app.pipeline.phone_guided_plan import UnsupportedPhonePlan, compile_phone_guided_plan
from app.pipeline.phone_narrated_plan import replace_editor_lanes, replace_narrated_captions
from app.pipeline.phone_recipe_shared import PhoneNarrationBed
from app.pipeline.phone_subtitled_lanes import PhoneSubtitledLanes, lane_names
from app.pipeline.phone_subtitled_plan import (
    SFX_DUCK_RECEIPT_FIELD,
    compile_phone_subtitled_plan,
    cutaways_from_recipe,
    landscape_fit_from_recipe,
    sfx_duck_receipt,
    speaker_binding_from_recipe,
)
from app.services.device_render import device_status, pin_device_request
from app.services.phone_editor_sources import (
    editor_source_bindings,
    editor_sources_for_variant,
    editor_visual_bindings,
)
from app.services.phone_rollout import (
    phone_narrated_caption_edits_supported,
    phone_subtitled_editor_lanes_supported,
    phone_voiceover_editor_lanes_supported,
    validate_phone_pilot_recipe,
)
from app.services.phone_sources import (
    PHONE_SOURCES_FIELD,
    PHONE_VISUALS_FIELD,
    PhoneSourceBinding,
    PhoneVisualBinding,
)
from app.services.phone_subtitled_editor import (
    PHONE_SUBTITLED_EDITOR_LANES_FIELD,
    is_catalog_sfx_path,
    is_phone_subtitled_editor_variant,
    lanes_from_editor_sections,
    lanes_from_recipe,
    sections_from_lanes,
)

log = structlog.get_logger()
PHONE_EDITOR_PLAN_FIELD = "_phone_editor_plan_v1"

# The only native-editor sections a phone `subtitled` (Talking to camera)
# variant honours today (KRI-182 step 1; `caption_cues`/`caption_meta` added
# KRI-216). Everything else the generic `_prepare_editor_commit` validators
# already accepted for this archetype on cloud (`visual_blocks`,
# `motion_scenes`, `camera_effects`, `timeline`, `mix`, `music`,
# `orientation`, `text_elements`, ...) stays CLOSED on a device variant -- the
# phone subtitled compiler has no lane for any of them, and Save must never
# silently apply one it can't recompile.
_SUBTITLED_EDITOR_SECTIONS = frozenset(
    {"sound_effects", "media_overlays", "caption_cues", "caption_meta"}
)

# The only native-editor sections a phone `narrated` (recorded voiceover)
# variant honours (KRI-280): the caption lines and their look. The narrated
# phone compiler has no editor lane for anything else, so every other section
# stays closed rather than silently dropped on Save.
_NARRATED_EDITOR_SECTIONS = frozenset({"caption_cues", "caption_meta"})
_NARRATED_CAPTION_SECTIONS = frozenset({"caption_cues", "caption_meta"})
# KRI-281: a phone Voiceover edit (`narrated`, or a montage `voiceover`) also
# honours the sound-effect and Visuals lanes, recompiled through the SAME lane
# helpers phone Talking uses -- only while `phone_voiceover_editor_lanes_supported`.
_VOICEOVER_LANE_SECTIONS = frozenset({"sound_effects", "media_overlays"})


def is_phone_narrated_editor_variant(variant: object) -> bool:
    """True for a phone-rendered `narrated` (recorded voiceover) variant --
    the shape `app.tasks.generative_build._run_phone_narrated_job` pins."""
    return (
        isinstance(variant, dict)
        and variant.get("render_destination") == "device"
        and variant.get("resolved_archetype") == "narrated"
    )


def is_phone_voiceover_montage_editor_variant(variant: object, assembly: dict) -> bool:
    """True for a phone-rendered montage `voiceover` variant -- the shape
    `_run_phone_voiceover_montage_job` pins -- that has no guided plan to fall
    back to (KRI-281)."""
    return (
        isinstance(variant, dict)
        and variant.get("render_destination") == "device"
        and variant.get("resolved_archetype") == "voiceover"
        and not variant.get(PHONE_EDITOR_PLAN_FIELD)
        and "guided_story_execution_plan" not in assembly
    )


class _StagedJob:
    """Keep validator mutations off the ORM until the full native program validates."""

    def __init__(self, job: Any):
        self._job = job
        self.assembly_plan = copy.deepcopy(job.assembly_plan or {})

    def __getattr__(self, name: str) -> Any:
        return getattr(self._job, name)


def prepare_phone_editor_commit(
    job: Any,
    variant_id: str,
    *,
    prepare: Callable[[Any], dict],
    sfx_catalog_paths: Mapping[str, str] | None = None,
) -> dict:
    """``sfx_catalog_paths`` maps a sound-effect catalog id to its
    `SoundEffect.audio_gcs_path` for the phone Talking effects a Save carries
    over without a known storage path (the caller's async catalog read,
    `generative_jobs._phone_subtitled_sfx_paths`; Save itself never queries)."""
    staged = _StagedJob(job)
    prep = prepare(staged)
    if not prep["has_render_section"]:
        return {**prep, "render_destination": "device"}
    if not settings.phone_rendering_for(job.user_id):
        raise HTTPException(422, detail={"code": "phone_rendering_unavailable"})
    try:
        previous = device_status(job, variant_id).request
        assembly = staged.assembly_plan
        variant = next(v for v in assembly["variants"] if v.get("variant_id") == variant_id)
        if variant.get("editor_timeline_mode") == "authored":
            from app.pipeline.phone_authored_timeline import compile_phone_authored_timeline

            recipe = compile_phone_authored_timeline(staged, variant, previous.recipe)
            validate_phone_pilot_recipe(recipe, allow_editor_media=True)
            request = make_device_request(
                job_id=job.id,
                variant_id=variant_id,
                revision=previous.identity.recipe_revision + 1,
                recipe=recipe,
            )
            pin_device_request(staged, request, base_generation=prep["generation"])
            variant = next(
                v for v in staged.assembly_plan["variants"] if v.get("variant_id") == variant_id
            )
            variant["render_status"] = "awaiting_device"
            variant["duration_s"] = recipe.duration
        elif is_phone_subtitled_editor_variant(variant):
            _compile_subtitled_editor_commit(
                staged,
                assembly,
                variant,
                variant_id,
                prep=prep,
                previous=previous,
                sfx_catalog_paths=sfx_catalog_paths or {},
            )
        elif is_phone_narrated_editor_variant(variant):
            _compile_narrated_editor_commit(
                staged,
                assembly,
                variant,
                variant_id,
                prep=prep,
                previous=previous,
                sfx_catalog_paths=sfx_catalog_paths or {},
            )
        elif is_phone_voiceover_montage_editor_variant(variant, assembly):
            _compile_voiceover_montage_editor_commit(
                staged,
                assembly,
                variant,
                variant_id,
                prep=prep,
                previous=previous,
                sfx_catalog_paths=sfx_catalog_paths or {},
            )
        elif variant.get("resolved_archetype") == "speech_montage":
            # KRI-282: a spoken-excerpt montage has no guided plan to recompile; fail
            # closed with a reason instead of a KeyError on the missing plan.
            raise ValueError(
                "a spoken-excerpt montage can't be re-rendered from the server editor; "
                "edit its timeline in the app"
            )
        else:
            plan = copy.deepcopy(
                variant.get(PHONE_EDITOR_PLAN_FIELD) or assembly["guided_story_execution_plan"]
            )
            revision = prep.get("guided_revision")
            render_sections = {key for key, value in prep["sections"].items() if value}
            if not (render_sections - {"text_elements"}):
                # A text-only save keeps the approved plan's device-verified timing
                # program and swaps just the text lane, whether it came from a
                # legacy guided editor or the v2 revision contract. Re-projecting
                # through `compile_guided_runtime_plan` re-clocks every moment and
                # transition onto 1/30 s frames, which `compile_phone_guided_plan`
                # rejects as a different program from the millisecond timing the
                # phone was pinned against -- so under guided editor v2 every phone
                # text edit was a 422 `unsupported_phone_edit` (2026-09-19 chat-edit
                # incident, job d9a965b0). Other approved lanes, including the
                # contextual and narration label lanes, are preserved untouched.
                plan["text_elements"] = variant.get("text_elements") or []
            elif revision is not None:
                plan = compile_guided_runtime_plan(
                    assembly["guided_story_execution_plan"],
                    assembly["guided_edit"],
                    revision,
                    admitted_sources=editor_sources_for_variant(variant),
                )
            else:
                raise ValueError("phone editor requires a canonical guided revision")
            bindings = tuple(
                PhoneSourceBinding.model_validate(row) for row in assembly[PHONE_SOURCES_FIELD]
            ) + editor_source_bindings(variant)
            # Reuse approved and asynchronously admitted receipts; Save never
            # downloads or hashes media while holding its database locks.
            visuals = tuple(
                PhoneVisualBinding.model_validate(row)
                for row in assembly.get(PHONE_VISUALS_FIELD) or []
            ) + editor_visual_bindings(variant)
            # Narration is already pinned in the previous immutable recipe. Never
            # re-download/hash on Save, nor silently drop its audio when adding media.
            narration = None
            if plan.get("narration") is not None:
                voice = next(
                    asset
                    for asset in previous.recipe.asset_manifest.assets
                    if asset.kind == "voiceover"
                )
                media = next(asset for asset in previous.recipe.assets if asset.id == voice.id)
                narration = PhoneNarrationBed(
                    plan_item_id=voice.plan_item_id,
                    generation=voice.generation,
                    fingerprint=voice.fingerprint,
                    duration_s=media.duration,
                )
            allow_editor_media = bool(plan.get("editor_visual_blocks"))
            recipe = compile_phone_guided_plan(
                GuidedStoryExecutionPlan.model_validate(plan),
                bindings,
                visuals=visuals,
                narration=narration,
                allow_editor_media=allow_editor_media,
            )
            validate_phone_pilot_recipe(recipe, allow_editor_media=allow_editor_media)
            request = make_device_request(
                job_id=job.id,
                variant_id=variant_id,
                revision=previous.identity.recipe_revision + 1,
                recipe=recipe,
            )
            pin_device_request(staged, request, base_generation=prep["generation"])
            for variant in staged.assembly_plan["variants"]:
                if variant.get("variant_id") == variant_id:
                    variant["render_status"] = "awaiting_device"
                    variant["render_destination"] = "device"
                    variant["duration_s"] = plan["resolved_duration_s"]
                    variant[PHONE_EDITOR_PLAN_FIELD] = plan
                    variant.update(song_reference_variant_fields(plan))
        staged.status = "awaiting_device"
    except (
        KeyError,
        StopIteration,
        TypeError,
        ValueError,
        GuidedStoryError,
        UnsupportedPhonePlan,
    ) as exc:
        # The cause was invisible: a bare code reached the creator and nothing
        # reached the logs (2026-09-19 phone chat-edit incident). Keep the
        # wire code stable; name the failing step for operators.
        reason = f"{type(exc).__name__}: {exc}"[:300]
        log.warning(
            "phone_editor_commit_unsupported",
            job_id=str(job.id),
            variant_id=variant_id,
            reason=reason,
            exc_info=True,
        )
        raise HTTPException(
            422, detail={"code": "unsupported_phone_edit", "reason": reason}
        ) from exc
    job.assembly_plan = staged.assembly_plan
    job.status = staged.status
    if "started_at" in vars(staged):
        job.started_at = staged.started_at
    return {**prep, "render_destination": "device", "render_task_id": None}


def _assembly_visuals(assembly: dict) -> tuple[PhoneVisualBinding, ...]:
    return tuple(
        PhoneVisualBinding.model_validate(row) for row in assembly.get(PHONE_VISUALS_FIELD) or []
    )


def _previous_lane_state(
    variant: dict, previous: Any, visuals: tuple[PhoneVisualBinding, ...]
) -> tuple[PhoneSubtitledLanes | None, dict[str, str], dict[str, str]]:
    """``(lanes, labels, paths)`` as of the last Save: the persisted lanes field,
    else (first-ever Save) derived from the worker-pinned recipe so an untouched
    section carries forward instead of silently vanishing."""
    persisted = variant.get(PHONE_SUBTITLED_EDITOR_LANES_FIELD)
    previous_lanes: PhoneSubtitledLanes | None = None
    if isinstance(persisted, dict) and isinstance(persisted.get("lanes"), dict):
        previous_lanes = PhoneSubtitledLanes.model_validate(persisted["lanes"])
    labels: dict[str, str] = {}
    if isinstance(persisted, dict) and isinstance(persisted.get("labels"), dict):
        labels = dict(persisted["labels"])
    # Real catalog audio object per catalog id (recipes carry no storage
    # paths); the committed section carries the route-resolved path for every
    # effect it names, previously persisted paths and `sfx_catalog_paths`
    # cover the rest (see `_settle_lane_labels_and_paths`).
    paths: dict[str, str] = {}
    if isinstance(persisted, dict) and isinstance(persisted.get("paths"), dict):
        paths = {str(k): str(v) for k, v in persisted["paths"].items() if v}
    if previous_lanes is None and isinstance(previous.recipe, EditRecipeV2):
        previous_lanes = lanes_from_recipe(
            previous.recipe, visuals=visuals, duck_receipt=variant.get(SFX_DUCK_RECEIPT_FIELD)
        )
    return previous_lanes, labels, paths


def _settle_lane_labels_and_paths(
    lanes: PhoneSubtitledLanes,
    labels: dict[str, str],
    paths: dict[str, str],
    *,
    prep: dict,
    sfx_catalog_paths: Mapping[str, str],
) -> tuple[dict[str, str], dict[str, str]]:
    """The labels/paths bags to persist after a lane compile (shared by every
    phone archetype that edits the sound-effect lane)."""
    # Keep the labels bag limited to catalog ids still on the timeline, and
    # pick up any label the committed sfx section carried for a newly added
    # effect (`resolve_editor_sound_effect_placements` fills it from the
    # catalog row before this function ever runs).
    active_catalog_ids = {resolved.asset.catalog_id for resolved in lanes.sound_effects}
    labels = {
        catalog_id: label
        for catalog_id, label in labels.items()
        if catalog_id in active_catalog_ids
    }
    paths = {
        catalog_id: path for catalog_id, path in paths.items() if catalog_id in active_catalog_ids
    }
    for item in prep.get("sfx_override") or []:
        catalog_id = item.get("sound_effect_id")
        label = item.get("label")
        path = item.get("src_gcs_path")
        if isinstance(catalog_id, str) and isinstance(label, str) and label:
            labels[catalog_id] = label
        # A chat edit compiles its section from the variant's own rows without
        # the route's catalog resolve, so it can echo a placeholder back.
        if isinstance(catalog_id, str) and is_catalog_sfx_path(catalog_id, path):
            paths[catalog_id] = path
    # iOS commits only CHANGED sections: an effect carried over from the
    # pinned recipe (or from a Save that never learned its path) has no known
    # object, and persisting the placeholder made every later preview sign a
    # 404. Fill it from the caller's catalog read; the bag only ever holds real
    # catalog objects.
    for catalog_id in active_catalog_ids:
        if is_catalog_sfx_path(catalog_id, paths.get(catalog_id)):
            continue
        resolved_path = sfx_catalog_paths.get(catalog_id)
        if is_catalog_sfx_path(catalog_id, resolved_path):
            paths[catalog_id] = resolved_path
        else:
            paths.pop(catalog_id, None)

    return labels, paths


def _persist_lane_state(
    row: dict,
    lanes: PhoneSubtitledLanes,
    labels: dict[str, str],
    paths: dict[str, str],
    *,
    caption_style: str,
    duck_receipt: dict | None,
) -> None:
    """Write the compiled lane state onto the variant row (shared by every
    phone archetype whose Save recompiles the sound-effect / Visuals lanes)."""
    sections = sections_from_lanes(lanes, labels=labels, paths=paths)
    row[PHONE_SUBTITLED_EDITOR_LANES_FIELD] = {
        "version": 1,
        "caption_style": caption_style,
        "lanes": lanes.model_dump(mode="json"),
        "labels": labels,
        "paths": paths,
    }
    row["sound_effects"] = sections["sound_effects"] or None
    row["media_overlays"] = sections["media_overlays"] or None
    row["phone_lane_receipt"] = {"applied": list(lane_names(lanes)), "dropped": []}
    if duck_receipt is not None:
        row[SFX_DUCK_RECEIPT_FIELD] = duck_receipt
    else:
        row.pop(SFX_DUCK_RECEIPT_FIELD, None)


def _compile_subtitled_editor_commit(
    staged: Any,
    assembly: dict,
    variant: dict,
    variant_id: str,
    *,
    prep: dict,
    previous: Any,
    sfx_catalog_paths: Mapping[str, str],
) -> None:
    """The `resolved_archetype == "subtitled"` counterpart of the guided-story
    branch above: recompile `compile_phone_subtitled_plan` from the committed
    editor sections instead of a guided execution plan (KRI-182 step 1).

    Mutates ``staged`` in place (pins the new device request, updates the
    matching variant dict) exactly like the guided branch; raises
    ``ValueError``/``UnsupportedPhonePlan`` on anything unsupported, caught by
    the same broad ``except`` in `prepare_phone_editor_commit`.
    """
    if not phone_subtitled_editor_lanes_supported():
        raise ValueError("phone Talking edits aren't editable yet")

    active_sections = {key for key, value in prep["sections"].items() if value}
    unsupported_sections = active_sections - _SUBTITLED_EDITOR_SECTIONS
    if unsupported_sections:
        name = sorted(unsupported_sections)[0]
        raise ValueError(f"{name} isn't supported on phone Talking edits yet")

    visuals = _assembly_visuals(assembly)
    previous_lanes, labels, paths = _previous_lane_state(variant, previous, visuals)

    bindings = tuple(
        PhoneSourceBinding.model_validate(row) for row in assembly[PHONE_SOURCES_FIELD]
    )
    lanes = lanes_from_editor_sections(
        previous=previous_lanes,
        sound_effects=prep.get("sfx_override"),
        media_overlays=prep.get("media_overlays_override"),
        visuals=visuals,
    )

    caption_cues = prep.get("caption_cues_override")
    caption_cues_overridden = caption_cues is not None
    if caption_cues is None:
        caption_cues = variant.get("caption_cues") or []
    caption_style = variant.get("voiceover_caption_style") or "sentence"
    caption_look = caption_look_from_variant(variant)

    # KRI-216: reconstruct the previously pinned speech-cleanup cut (if any)
    # from the worker-pinned recipe's own main-track clips, so a Save never
    # silently recompiles the full uncut clip underneath cues/lanes that are
    # already expressed in CUT-timeline coordinates (the pre-KRI-216 bug --
    # every editor Save on a cleaned-up variant desynced captions/lanes from
    # the video). An uncut variant's main track is a single full-duration
    # clip, so this reproduces `keep_segments=None`'s own default byte-for-
    # byte -- no branch on "was this cut" needed.
    # KRI-136: a multi-clip Talking head pins every clip as a phone source;
    # the speaker is whichever one plays on the main track, and its cutaways
    # carry over from the pinned recipe unchanged (captions/lanes share the
    # same timeline, and the editor never moves a cutaway).
    speaker = bindings[0]
    cutaways: tuple = ()
    if isinstance(previous.recipe, EditRecipeV2):
        speaker = speaker_binding_from_recipe(previous.recipe, bindings)
        cutaways = cutaways_from_recipe(previous.recipe, bindings)
    elif len(bindings) != 1:
        raise ValueError("multi-clip phone Talking edits need a pinned recipe")
    keep_segments = (
        _previous_keep_segments(previous.recipe, speaker.render_asset().id)
        if isinstance(previous.recipe, EditRecipeV2)
        else None
    )
    # KRI-283: a letterboxed (landscape + fit) variant keeps its bars on Save.
    landscape_fit = (
        landscape_fit_from_recipe(previous.recipe)
        if isinstance(previous.recipe, EditRecipeV2)
        else "fill"
    )

    recipe = compile_phone_subtitled_plan(
        (speaker,),
        caption_cues=caption_cues,
        caption_style=caption_style,
        visuals=visuals,
        lanes=lanes,
        duck_sfx_under_speech=settings.phone_sfx_speech_duck_enabled,
        keep_segments=keep_segments,
        caption_look=caption_look,
        cutaways=cutaways,
        landscape_fit=landscape_fit,
    )
    duck_receipt = sfx_duck_receipt(lanes, recipe)
    validate_phone_pilot_recipe(recipe, allow_editor_media=bool(lanes.overlays or cutaways))
    request = make_device_request(
        job_id=previous.identity.job_id,
        variant_id=variant_id,
        revision=previous.identity.recipe_revision + 1,
        recipe=recipe,
    )
    pin_device_request(staged, request, base_generation=prep["generation"])

    labels, paths = _settle_lane_labels_and_paths(
        lanes, labels, paths, prep=prep, sfx_catalog_paths=sfx_catalog_paths
    )
    for row in staged.assembly_plan["variants"]:
        if row.get("variant_id") != variant_id:
            continue
        row["render_status"] = "awaiting_device"
        row["render_destination"] = "device"
        row["duration_s"] = recipe.duration
        if caption_cues_overridden:
            row["caption_cues"] = caption_cues
        _persist_lane_state(
            row, lanes, labels, paths, caption_style=caption_style, duck_receipt=duck_receipt
        )


def _commit_voiceover_lanes(
    recipe: EditRecipeV2,
    *,
    assembly: dict,
    variant: dict,
    previous: Any,
    prep: dict,
    sfx_catalog_paths: Mapping[str, str],
    video_track_id: str,
) -> tuple[EditRecipeV2, PhoneSubtitledLanes, dict[str, str], dict[str, str]]:
    """Recompile a phone Voiceover recipe's sound-effect / Visuals lanes from the
    committed sections (KRI-281), reusing Talking's lane bridge end to end:
    ``(recipe, lanes, labels, paths)``.

    Visuals admitted by the editor (async source registration) count alongside
    the generation's own pinned visuals, like the guided branch.
    """
    visuals = _assembly_visuals(assembly) + editor_visual_bindings(variant)
    previous_lanes, labels, paths = _previous_lane_state(variant, previous, visuals)
    lanes = lanes_from_editor_sections(
        previous=previous_lanes,
        sound_effects=prep.get("sfx_override"),
        media_overlays=prep.get("media_overlays_override"),
        visuals=visuals,
    )
    recipe = replace_editor_lanes(
        recipe, lanes=lanes, visuals=visuals, video_track_id=video_track_id
    )
    labels, paths = _settle_lane_labels_and_paths(
        lanes, labels, paths, prep=prep, sfx_catalog_paths=sfx_catalog_paths
    )
    return recipe, lanes, labels, paths


def _compile_narrated_editor_commit(
    staged: Any,
    assembly: dict,
    variant: dict,
    variant_id: str,
    *,
    prep: dict,
    previous: Any,
    sfx_catalog_paths: Mapping[str, str],
) -> None:
    """The `resolved_archetype == "narrated"` counterpart of the subtitled
    branch above (KRI-280): swap the committed caption cues and look into the
    pinned recipe (`replace_narrated_captions`), and (KRI-281) recompile the
    sound-effect / Visuals lanes (`replace_editor_lanes`).

    ``variant`` is the STAGED row, so `_prepare_editor_commit` has already
    written the validated cues and caption-meta fields onto it. The clips,
    the narration bed and the audio mix all come from ``previous`` -- Save
    never re-downloads, re-transcribes or re-aligns the voiceover. Raises
    ``ValueError``/``UnsupportedPhonePlan`` on anything unsupported, caught by
    the same broad ``except`` in `prepare_phone_editor_commit`.
    """
    active_sections = {key for key, value in prep["sections"].items() if value}
    caption_active = active_sections & _NARRATED_CAPTION_SECTIONS
    lanes_active = active_sections & _VOICEOVER_LANE_SECTIONS
    lanes_ok = phone_voiceover_editor_lanes_supported(require_client=False)
    if lanes_active and not lanes_ok:
        raise ValueError("phone Voiceover sound effects and Visuals aren't editable yet")
    if (caption_active or not lanes_active) and not phone_narrated_caption_edits_supported():
        raise ValueError("phone Narrated captions aren't editable yet")

    allowed = _NARRATED_EDITOR_SECTIONS | (_VOICEOVER_LANE_SECTIONS if lanes_ok else frozenset())
    unsupported_sections = active_sections - allowed
    if unsupported_sections:
        name = sorted(unsupported_sections)[0]
        raise ValueError(f"{name} isn't supported on phone Narrated edits yet")
    if not isinstance(previous.recipe, EditRecipeV2):
        raise ValueError("phone Narrated edits need a pinned v2 recipe")

    caption_cues = prep.get("caption_cues_override")
    caption_cues_overridden = caption_cues is not None
    if caption_cues is None:
        caption_cues = variant.get("caption_cues") or []
    recipe = previous.recipe
    if caption_active or not lanes_active:
        caption_style = "word" if variant.get("voiceover_caption_style") == "word" else "sentence"
        recipe = replace_narrated_captions(
            recipe,
            caption_cues=caption_cues,
            caption_style=caption_style,
            look=caption_look_from_variant(variant),
        )
    lane_state = None
    if lanes_active:
        lane_state = _commit_voiceover_lanes(
            recipe,
            assembly=assembly,
            variant=variant,
            previous=previous,
            prep=prep,
            sfx_catalog_paths=sfx_catalog_paths,
            video_track_id="narrated",
        )
        recipe = lane_state[0]
    validate_phone_pilot_recipe(
        recipe, allow_editor_media=bool(lane_state and lane_state[1].overlays)
    )
    request = make_device_request(
        job_id=previous.identity.job_id,
        variant_id=variant_id,
        revision=previous.identity.recipe_revision + 1,
        recipe=recipe,
    )
    # The cleaned-narration binding (KRI-277) carries over: the voiceover
    # asset is unchanged, and `pin_device_request` reuses a matching binding.
    pin_device_request(staged, request, base_generation=prep["generation"])
    for row in staged.assembly_plan["variants"]:
        if row.get("variant_id") != variant_id:
            continue
        row["render_status"] = "awaiting_device"
        row["render_destination"] = "device"
        row["duration_s"] = recipe.duration
        if caption_cues_overridden:
            row["caption_cues"] = caption_cues
        if lane_state is not None:
            _, lanes, labels, paths = lane_state
            _persist_lane_state(
                row,
                lanes,
                labels,
                paths,
                caption_style=str(row.get("voiceover_caption_style") or "sentence"),
                duck_receipt=None,
            )


def _compile_voiceover_montage_editor_commit(
    staged: Any,
    assembly: dict,
    variant: dict,
    variant_id: str,
    *,
    prep: dict,
    previous: Any,
    sfx_catalog_paths: Mapping[str, str],
) -> None:
    """Save for a phone montage `voiceover` variant (KRI-281): only the
    sound-effect and Visuals lanes are editable; the clips, voice and music beds
    and intro text stay exactly as pinned."""
    if not phone_voiceover_editor_lanes_supported(require_client=False):
        raise ValueError("phone Voiceover sound effects and Visuals aren't editable yet")
    active_sections = {key for key, value in prep["sections"].items() if value}
    unsupported_sections = active_sections - _VOICEOVER_LANE_SECTIONS
    if unsupported_sections:
        name = sorted(unsupported_sections)[0]
        raise ValueError(f"{name} isn't supported on phone Voiceover edits yet")
    if not isinstance(previous.recipe, EditRecipeV2):
        raise ValueError("phone Voiceover edits need a pinned v2 recipe")

    recipe, lanes, labels, paths = _commit_voiceover_lanes(
        previous.recipe,
        assembly=assembly,
        variant=variant,
        previous=previous,
        prep=prep,
        sfx_catalog_paths=sfx_catalog_paths,
        video_track_id="montage",
    )
    validate_phone_pilot_recipe(recipe, allow_editor_media=bool(lanes.overlays))
    request = make_device_request(
        job_id=previous.identity.job_id,
        variant_id=variant_id,
        revision=previous.identity.recipe_revision + 1,
        recipe=recipe,
    )
    pin_device_request(staged, request, base_generation=prep["generation"])
    for row in staged.assembly_plan["variants"]:
        if row.get("variant_id") != variant_id:
            continue
        row["render_status"] = "awaiting_device"
        row["render_destination"] = "device"
        row["duration_s"] = recipe.duration
        _persist_lane_state(row, lanes, labels, paths, caption_style="sentence", duck_receipt=None)


def _previous_keep_segments(
    recipe: EditRecipeV2, speaker_asset_id: str
) -> list[tuple[float, float]] | None:
    """Reconstruct `compile_phone_subtitled_plan`'s own ``keep_segments`` input
    from a previously pinned subtitled recipe's main (``"subtitled"``) video
    track -- the inverse of that function's ``keep_segments`` ->
    one-`TimelineClip`-per-kept-segment projection.

    Only clips over ``speaker_asset_id`` (the speaker binding's own asset)
    count -- the SAME track can also carry a muted ending clip
    (`_compile_ending_clip`) appended right after the speaker segments, over
    a DIFFERENT (Visuals-pool) asset; that clip is never part of the cut and
    must not be read back as a kept segment. Clips are ordered by
    ``timeline_start`` (the same order they were originally appended in).

    Returns ``None`` when the track is missing or carries no speaker clip at
    all -- defensive only; every real pinned subtitled recipe has one.
    """
    main_track = next((track for track in recipe.tracks if track.id == "subtitled"), None)
    if main_track is None:
        return None
    speaker_clips = sorted(
        (clip for clip in main_track.clips if clip.source_asset_id == speaker_asset_id),
        key=lambda clip: clip.timeline_start,
    )
    if not speaker_clips:
        return None
    return [(clip.source_start, clip.source_start + clip.source_duration) for clip in speaker_clips]
