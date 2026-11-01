"""Atomically project validated editor state into the next immutable phone recipe."""

from __future__ import annotations

import copy
import hashlib
import json
import math
from collections.abc import Callable, Mapping, Sequence
from typing import Any

import structlog
from fastapi import HTTPException

from app.config import settings
from app.kria.clip_tilt_row import row_tilt_from_clip
from app.kria.device_render import make_device_request
from app.kria.recipes_v2 import EditRecipeV2
from app.pipeline.editor_render_window import RenderWindowUnavailable
from app.pipeline.guided_story import (
    USER_SONG_LIPSYNC_LOCKED,
    USER_SONG_WINDOW_OUT_OF_RANGE,
    GuidedStoryError,
    GuidedStoryExecutionPlan,
    compile_guided_runtime_plan,
    song_reference_variant_fields,
)
from app.pipeline.lipsync_montage import (
    LipsyncSyncError,
    refuse_lipsync_rate_change,
    resync_lipsync_moments,
)
from app.pipeline.phone_captions import caption_look_from_variant
from app.pipeline.phone_clip_sound import reconcile_clip_sound
from app.pipeline.phone_guided_plan import UnsupportedPhonePlan, compile_phone_guided_plan
from app.pipeline.phone_narrated_plan import (
    _EDITOR_LANE_TRACK_IDS,
    EDITOR_MEDIA_TRACK_ID,
    narrated_authored_text_elements,
    replace_editor_lanes,
    replace_editor_media,
    replace_narrated_captions,
    replace_narrated_title,
)
from app.pipeline.phone_recipe_shared import (
    FIT_VIDEO_TRACK_IDS,
    PhoneNarrationBed,
    PhoneSongBed,
    apply_landscape_fit,
    timeline_end_s,
    with_drawings,
)
from app.pipeline.phone_subtitled_lanes import (
    PhoneSubtitledLanes,
    ResolvedSoundEffect,
    SubtitledBackground,
    SubtitledBackgroundWindow,
    SubtitledSpeakerFrame,
    lane_names,
)
from app.pipeline.phone_subtitled_plan import (
    SFX_DUCK_RECEIPT_FIELD,
    additional_speakers_from_recipe,
    compile_phone_subtitled_plan,
    cutaways_from_recipe,
    landscape_fit_from_recipe,
    sfx_duck_receipt,
    speaker_binding_from_recipe,
)
from app.pipeline.phone_timed_cards import TIMED_CARDS_FIELD, timed_cards_from_rows
from app.pipeline.phone_voiceover_cut import replace_voiceover_cut
from app.services.creator_render_contract import (
    CONTRACT_FIELD,
    AudioChoiceError,
    CreatorRenderContractError,
    TextPlacementError,
    TextRequirement,
    decline_payload,
    read_render_contract,
)
from app.services.device_render import CONTRACT_REVISIONS_FIELD, device_status, pin_device_request
from app.services.phone_editor_sources import (
    editor_source_bindings,
    editor_sources_for_variant,
    editor_visual_bindings,
)
from app.services.phone_rollout import (
    SINGLE_WORD_CAPTIONS_STAMP,
    card_tilt_supported,
    freehand_drawing_client_supported,
    freeze_frame_client_supported,
    phone_guided_sfx_supported,
    phone_narrated_caption_edits_supported,
    phone_narrated_title_edits_supported,
    phone_subtitled_editor_lanes_supported,
    phone_subtitled_title_supported,
    phone_talking_cutaway_sound_supported,
    phone_timed_cards_supported,
    phone_voiceover_editor_lanes_supported,
    phone_voiceover_editor_media_supported,
    recipe_has_freeze_frame,
    recipe_has_single_word_text,
    single_word_text_client_supported,
    single_word_text_verified,
    speaker_background_client_supported,
    speaker_background_windows_client_supported,
    text_bend_client_supported,
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
    variant_carries_speaker_background,
    variant_carries_speaker_background_windows,
)
from app.services.phone_voiceover_timeline import (
    narrated_timings_and_assignments,
    source_pool_paths,
)

log = structlog.get_logger()
# The worker-pinned (possibly repaired) guided plan. ONLY the worker writes it: a
# revision Save compiles from it as the provenance fence, so a Save must never
# overwrite it with its own output.
PHONE_EDITOR_PLAN_FIELD = "_phone_editor_plan_v1"
# What the LAST Save compiled. A text-only Save reuses it so the approved timing
# program (and any cut that Save made) survives; revision Saves never read it.
PHONE_EDITOR_SAVED_PLAN_FIELD = "_phone_editor_saved_plan_v1"

# The only native-editor sections a phone `subtitled` (Talking to camera)
# variant honours today (KRI-182 step 1; `caption_cues`/`caption_meta` added
# KRI-216). Everything else the generic `_prepare_editor_commit` validators
# already accepted for this archetype on cloud (`visual_blocks`,
# `motion_scenes`, `camera_effects`, `timeline`, `mix`, `music`,
# `text_elements`, ...) stays CLOSED on a device variant -- the
# phone subtitled compiler has no lane for any of them, and Save must never
# silently apply one it can't recompile.
_SUBTITLED_EDITOR_SECTIONS = frozenset(
    {
        "sound_effects",
        "media_overlays",
        "caption_cues",
        "caption_meta",
        "landscape_fit",
        # A Talking edit re-canvases on Save: the same recipe recompiles onto the
        # portrait (1080x1920) or the landscape (1920x1080) canvas. The capability
        # (`render_shape.device_editor_offer`) and `_orientation_unsupported_reason`
        # advertise it on the same lane-rollout predicate this branch checks first.
        "orientation",
        # KRI-589: freehand drawings compile into the recipe's `drawing_layers`.
        "drawings",
        # KRI-1117: the whole-edit Talk-to-camera background (`EditRecipeV2.speaker_background`).
        "speaker_background",
    }
)
# KRI-467: ...plus its text lane (the opening title and any creator text),
# only while `phone_subtitled_title_supported`. Every Save compiles the
# variant's persisted text rows, whichever sections it carries, so a caption,
# lane or framing Save never drops the title.
_SUBTITLED_TEXT_SECTIONS = frozenset({"text_elements"})

# The native-editor sections a phone `narrated` (recorded voiceover) variant
# always honours (KRI-280): the caption lines and their look. Every other
# section opens only behind its own gate below, and the rest stay closed rather
# than silently dropped on Save.
_NARRATED_EDITOR_SECTIONS = frozenset({"caption_cues", "caption_meta", "drawings"})
_NARRATED_CAPTION_SECTIONS = frozenset({"caption_cues", "caption_meta"})
# KRI-465: ...and the opening title / text the creator adds, as `text_elements`,
# recompiled into the pinned recipe's `title-` layers by `replace_narrated_title`
# -- only while `phone_narrated_title_edits_supported`.
_NARRATED_TEXT_SECTIONS = frozenset({"text_elements"})
# KRI-281: a phone Voiceover edit (`narrated`, or a montage `voiceover`) also
# honours the sound-effect and Visuals lanes, recompiled through the SAME lane
# helpers phone Talking uses -- only while `phone_voiceover_editor_lanes_supported`.
_VOICEOVER_LANE_SECTIONS = frozenset({"sound_effects", "media_overlays"})
# KRI-287: ...and photos/videos added from the editor, as media Visual blocks,
# only while `phone_voiceover_editor_media_supported`.
_VOICEOVER_MEDIA_SECTIONS = frozenset({"visual_blocks"})
# KRI-290: ...and its clip cut (trim, extend, reorder, split, delete), swapped
# into the pinned recipe's video track by `replace_voiceover_cut`.
_VOICEOVER_CUT_SECTIONS = frozenset({"timeline"})
# KRI-306: ...and the creator's bars/crop choice for sideways clips, re-applied to
# the pinned recipe's main video track (`apply_landscape_fit`).
_VOICEOVER_FIT_SECTIONS = frozenset({"landscape_fit"})
# KRI-589: ...and freehand drawings, recompiled from the variant's stored lane onto the
# pinned recipe's `drawing_layers` (`with_drawings`) on EVERY Save of every device
# archetype, whichever sections it carries.
_DRAWING_SECTIONS = frozenset({"drawings"})
# KRI-821: ...and the footage's own sound level (Sounds > Original audio, `mix.original_level`
# only), written onto the pinned recipe's `audio.original_volume` by `_apply_original_level`.
# The narration and any music bed keep their pinned levels.
_ORIGINAL_LEVEL_SECTIONS = frozenset({"mix"})


def _resolved_landscape_fit(
    variant: dict, previous_recipe: Any, override: str | None = None
) -> str:
    """Bars/crop for a Save: explicit choice -> the variant's persisted value
    (``_prepare_editor_commit`` already wrote an override onto the staged row)
    -> what the pinned recipe encodes -> crop. An all-portrait cut compiled with
    "fit" reads back as "fill", which is why the persisted value comes first."""
    for candidate in (override, variant.get("landscape_fit")):
        if candidate in ("fit", "fill"):
            return str(candidate)
    if isinstance(previous_recipe, EditRecipeV2):
        return landscape_fit_from_recipe(previous_recipe)
    return "fill"


def _resolved_output_orientation(
    variant: dict, previous_recipe: Any, override: str | None = None
) -> str:
    """Canvas for a phone Talking Save: the section's choice -> the variant's persisted
    ``orientation`` -> the canvas the pinned recipe was compiled on -> portrait."""
    for candidate in (override, variant.get("orientation")):
        if candidate in ("portrait", "landscape"):
            return str(candidate)
    return _recipe_orientation(previous_recipe) or "portrait"


def _recipe_orientation(recipe: Any) -> str | None:
    canvas = getattr(recipe, "canvas", None)
    width, height = getattr(canvas, "width", None), getattr(canvas, "height", None)
    if isinstance(width, int | float) and isinstance(height, int | float):
        return "landscape" if width > height else "portrait"
    return None


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
        and not variant.get(PHONE_EDITOR_SAVED_PLAN_FIELD)
        and "guided_story_execution_plan" not in assembly
    )


class _StagedJob:
    """Keep validator mutations off the ORM until the full native program validates."""

    def __init__(self, job: Any):
        self._job = job
        self.assembly_plan = copy.deepcopy(job.assembly_plan or {})

    def __getattr__(self, name: str) -> Any:
        return getattr(self._job, name)


def _text_requirements_for_save(
    rows: list,
    prior: tuple[TextRequirement, ...],
    prior_elements: dict[str, str],
    *,
    keep_shot_roles: bool,
) -> tuple[TextRequirement, ...]:
    """The saved text lane as requirements, each keeping every role it was approved with.

    A row inherits the requirements it carried before the Save: matched by element id (so
    an edited opening title is still the opening title), else by exact text. The standard
    contract shape is TWO requirements for one title, ``[opening X (2.0 s), any X]``
    (the strategy's opening title and the brief's literal), so ALL matches are kept: the
    specific role (opening > closing > clip) with its approved ``duration_s``, and the
    generic ``any`` presence check beside it. An opening or closing role is verified
    against the recompiled recipe; when the creator's manual edit moved the text out of
    that window, or holds it for less than the approved time, ``_relax_requirement``
    degrades exactly that role to ``any`` (KRI-736: the creator's own edit wins) and the
    Save recompiles. A shot-scoped role is kept only while the timeline is untouched: a reorder
    invalidates the shot it was pinned to, and it degrades to ``any`` (never to nothing).
    Anything the contract never asked for stays ``any``.
    """

    def matches(text: object) -> list[TextRequirement]:
        key = _normal_text(text)
        return [item for item in prior if _normal_text(item.text) == key] if key else []

    out: list[TextRequirement] = []
    for row in rows:
        if not (isinstance(row, dict) and isinstance(row.get("text"), str) and row["text"].strip()):
            continue
        before = prior_elements.get(str(row.get("id")))
        sources = matches(before) or matches(row["text"])
        wanted: list[TextRequirement] = []
        for source in sources:
            if source.role in {"opening", "closing"}:
                wanted.append(
                    TextRequirement(
                        role=source.role, text=row["text"], duration_s=source.duration_s
                    )
                )
            elif source.role == "clip" and keep_shot_roles:
                wanted.append(
                    TextRequirement(
                        role="clip",
                        text=row["text"],
                        media_id=source.media_id,
                        shot_index=source.shot_index,
                        duration_s=source.duration_s,
                    )
                )
            else:
                wanted.append(TextRequirement(role="any", text=row["text"]))
        wanted = wanted or [TextRequirement(role="any", text=row["text"])]
        for requirement in wanted:
            if requirement not in out:
                out.append(requirement)
    return tuple(out)


def _normal_text(value: object) -> str:
    return " ".join(str(value or "").split()).casefold()


def _relaxed_receipt(exc: CreatorRenderContractError) -> dict[str, str]:
    if isinstance(exc, TextPlacementError):
        return {
            "requirement": "exact_text",
            "role": exc.requirement.role,
            "text": exc.requirement.text,
        }
    return {"requirement": str(getattr(exc, "field", ""))}


def _relax_requirement(
    staged: _StagedJob, variant_id: str, exc: CreatorRenderContractError
) -> bool:
    """KRI-736: the creator's manual edit wins over an approved requirement it moved past.

    * ``TextPlacementError``: an opening / closing / shot-scoped text no longer sits where its
      role says because the creator moved, re-timed or re-worded it. That one requirement
      becomes the generic ``any`` presence check on the same words, so the text itself must
      still be on screen (never dropped, never silently ignored).
    * ``AudioChoiceError``: the creator muted / un-muted camera sound, so the stale
      ``original_audio`` / ``audio_source_ids`` choice is cleared.

    Re-stamps the length policy against the new digest, like the rebind does. Returns False
    when there is nothing narrower to relax (the refusal then stands).
    """
    assembly = staged.assembly_plan
    revisions = assembly.get(CONTRACT_REVISIONS_FIELD)
    if not isinstance(revisions, dict) or not isinstance(revisions.get(variant_id), dict):
        return False
    contract = read_render_contract({CONTRACT_FIELD: revisions[variant_id]})
    if contract is None:
        return False
    changes: dict[str, Any]
    if isinstance(exc, TextPlacementError):
        failed = exc.requirement
        if failed.role == "any" or failed not in contract.exact_texts:
            return False
        generic = TextRequirement(role="any", text=failed.text)
        relaxed: list[TextRequirement] = []
        for item in contract.exact_texts:
            replacement = generic if item == failed else item
            if replacement not in relaxed:
                relaxed.append(replacement)
        changes = {"exact_texts": tuple(relaxed)}
    elif isinstance(exc, AudioChoiceError) and exc.field == "original_audio":
        if contract.original_audio is None:
            return False
        changes = {"original_audio": None}
    elif isinstance(exc, AudioChoiceError) and exc.field == "audio_source_ids":
        if not contract.audio_source_ids:
            return False
        changes = {"audio_source_ids": ()}
    else:
        return False
    from app.services.length_policy_state import (  # noqa: PLC0415
        read_length_policy,
        stamp_length_policy,
    )

    length_policy = read_length_policy(assembly, contract)
    rebound = contract.rebind(**changes)
    assembly[CONTRACT_REVISIONS_FIELD] = {**revisions, variant_id: rebound.model_dump(mode="json")}
    if length_policy is not None:
        staged.assembly_plan = stamp_length_policy(
            assembly,
            (staged.all_candidates or {}).get("creator_strategy") or {},
            rebound,
            mode=length_policy.check_mode,
            # KRI-847: this rebind never changes the length, so a ceiling stays a ceiling.
            duration_bound=length_policy.requested_duration_bound,
        )
    return True


def _authored_timeline_order(variant: dict, prep: dict) -> tuple[str, ...]:
    """The cut's source ids, in play order, for an AUTHORED timeline Save.

    Deleting a clip from a guided story converts it to an authored timeline
    (`stage_initial_authored_baseline`), which has no guided revision. Its
    resolved slots are the editor's own: `resolve_authored_phone_slots` stamps
    each with the `media_id` of the pinned source catalog entry it indexes, so
    this is the server's identification of every slot's source, never a guess
    from a client clip index. Empty (so the caller still refuses an
    order-required contract) unless EVERY active slot carries one.
    """
    if variant.get("editor_timeline_mode") != "authored":
        return ()
    rows = prep.get("timeline_override")
    if not isinstance(rows, list):
        return ()
    active = [row for row in rows if isinstance(row, dict) and not row.get("removed")]
    media_ids = tuple(str(row["media_id"]) for row in active if row.get("media_id"))
    return media_ids if active and len(media_ids) == len(active) else ()


def _collapse_adjacent(order: tuple[str, ...]) -> tuple[str, ...]:
    """The verifier reads the cut as a sequence of SOURCES, so a clip the creator split
    into adjacent pieces is one entry (``verify_phone_recipe`` collapses the same runs)."""
    return tuple(item for index, item in enumerate(order) if index == 0 or order[index - 1] != item)


def _rebind_editor_render_contract(
    staged: _StagedJob,
    variant_id: str,
    prep: dict,
    *,
    brief_binding: dict | None = None,
    previous_variant: dict | None = None,
) -> None:
    """Carry creator requirements into the editor's next approved generation.

    This operates only on the canonical staged editor projection, before any
    compiler runs.  It intentionally changes only the lanes the save declared:
    a colour/motion save retains every requirement, while a text or timeline
    save refreshes only the corresponding objective constraints.
    """
    assembly = staged.assembly_plan
    revisions = assembly.get(CONTRACT_REVISIONS_FIELD)
    if revisions is not None:
        if not isinstance(revisions, dict):
            raise CreatorRenderContractError("I couldn't read this edit's confirmed requirements.")
        if variant_id in revisions and not isinstance(revisions[variant_id], dict):
            raise CreatorRenderContractError("I couldn't read this edit's confirmed requirements.")
        contract = read_render_contract(
            {CONTRACT_FIELD: revisions[variant_id]} if variant_id in revisions else assembly
        )
    else:
        contract = read_render_contract(assembly)
    if contract is None:
        return
    generation = str(prep.get("generation") or "")
    if not generation:
        raise CreatorRenderContractError("I couldn't verify this edit's approved revision.")
    from app.services.length_policy_state import (  # noqa: PLC0415
        read_length_policy,
        stamp_length_policy,
    )

    length_policy = read_length_policy(assembly, contract)
    variant = next(v for v in assembly.get("variants", []) if v.get("variant_id") == variant_id)
    sections = prep.get("sections") or {}
    changes: dict[str, Any] = {"generation_id": generation}
    if brief_binding is not None:
        brief = brief_binding.get("brief")
        if not isinstance(brief, dict):
            raise CreatorRenderContractError("I couldn't verify this edit's approved brief.")
        changes["brief_digest"] = hashlib.sha256(
            json.dumps(brief, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
        ).hexdigest()
        bindings = dict(assembly.get("creator_brief_bindings") or {})
        bindings[generation] = copy.deepcopy(brief_binding)
        assembly["creator_brief_bindings"] = bindings
    elif contract.brief_digest is not None:
        # A second manual Save has no newly supplied binding, but its previous
        # editor revision is still bound to an immutable creator brief. Carry
        # that exact authority to the new generation; never fall back to an
        # unrelated root binding when the prior generation is missing/tampered.
        raw_bindings = assembly.get("creator_brief_bindings") or {}
        if not isinstance(raw_bindings, dict):
            raise CreatorRenderContractError("I couldn't verify this edit's approved brief.")
        inherited = raw_bindings.get(contract.generation_id)
        if inherited is None and contract.generation_id not in raw_bindings:
            inherited = assembly.get("creator_brief_binding")
        if not isinstance(inherited, dict):
            raise CreatorRenderContractError("I couldn't verify this edit's approved brief.")
        inherited_brief = inherited.get("brief")
        if not isinstance(inherited_brief, dict):
            raise CreatorRenderContractError("I couldn't verify this edit's approved brief.")
        inherited_digest = hashlib.sha256(
            json.dumps(
                inherited_brief, sort_keys=True, separators=(",", ":"), ensure_ascii=False
            ).encode()
        ).hexdigest()
        if inherited_digest != contract.brief_digest:
            raise CreatorRenderContractError("I couldn't verify this edit's approved brief.")
        bindings = dict(raw_bindings)
        bindings[generation] = copy.deepcopy(inherited)
        assembly["creator_brief_bindings"] = bindings
    if sections.get("text_elements"):
        # The staged variant is the accepted editor state.  Do not inspect the
        # generated recipe here: compiler output is proof, not edit authority.
        canonical_text = (prep.get("guided_revision") or {}).get(
            "text_elements", variant.get("text_elements") or []
        )
        prior_elements = {
            str(row.get("id")): row["text"]
            for row in (previous_variant or {}).get("text_elements") or []
            if isinstance(row, dict) and isinstance(row.get("text"), str)
        }
        changes["exact_texts"] = _text_requirements_for_save(
            canonical_text,
            contract.exact_texts,
            prior_elements,
            keep_shot_roles=not sections.get("timeline"),
        )
    if sections.get("timeline"):
        # `_prepare_editor_commit` has already validated and projected this
        # number from the submitted slots (or guided revision); the prior
        # variant duration is deliberately not authority for a trim.
        duration = prep.get("expected_duration_s")
        # A validated timeline Save is creator-authored timing for this new
        # generation. It supersedes the previous target without changing a
        # sibling variant's original contract or policy stamp.
        if (
            not isinstance(duration, bool)
            and isinstance(duration, (int, float))
            and math.isfinite(duration)
            and duration > 0
        ):
            changes["duration_s"] = float(duration)
        slots = (prep.get("guided_revision") or {}).get("segments") or []
        order = tuple(
            str(row["media_id"]) for row in slots if isinstance(row, dict) and row.get("media_id")
        )
        if not order and contract.order_required:
            # Authored cut (a deleted/trimmed/reordered guided story): the
            # resolved slots, not a client index, name each source.
            order = _authored_timeline_order(variant, prep)
        if order:
            changes.update(
                order_ids=_collapse_adjacent(order), order_required=True, order_basis="editor"
            )
        elif contract.order_required:
            # The legacy slot form uses clip indices.  Mapping those indices
            # back to source ids here would re-create routing logic and could
            # approve a reordered cut against the wrong source pool.
            raise CreatorRenderContractError(
                "This timeline edit needs a source-identified editor revision before it can render."
            )
    revisions = dict(revisions or {})
    rebound = contract.rebind(**changes)
    revisions[variant_id] = rebound.model_dump(mode="json")
    assembly[CONTRACT_REVISIONS_FIELD] = revisions
    if length_policy is not None:
        staged.assembly_plan = stamp_length_policy(
            assembly,
            (staged.all_candidates or {}).get("creator_strategy") or {},
            rebound,
            mode=length_policy.check_mode,
            # KRI-847: a timeline Save names a concrete new length (exact); other saves keep it.
            duration_bound=(
                "exact" if "duration_s" in changes else length_policy.requested_duration_bound
            ),
        )


def _compile_editor_commit(
    job: Any,
    staged: _StagedJob,
    variant_id: str,
    prep: dict,
    sfx_catalog_paths: Mapping[str, str] | None,
) -> None:
    """Recompile the staged variant onto the device and pin it (raises on a refusal)."""
    previous = device_status(job, variant_id).request
    assembly = staged.assembly_plan
    variant = next(v for v in assembly["variants"] if v.get("variant_id") == variant_id)
    if variant.get("editor_timeline_mode") == "authored":
        from app.pipeline.phone_authored_timeline import compile_phone_authored_timeline

        recipe = compile_phone_authored_timeline(staged, variant, previous.recipe)
        recipe = with_drawings(recipe, variant.get("drawings"))
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
        if isinstance(variant.get("clip_sound"), dict):
            variant["clip_sound"] = reconcile_clip_sound(
                recipe, variant["clip_sound"], video_track_id="authored"
            )
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
        worker_plan = (
            variant.get(PHONE_EDITOR_PLAN_FIELD) or assembly["guided_story_execution_plan"]
        )
        plan = copy.deepcopy(variant.get(PHONE_EDITOR_SAVED_PLAN_FIELD) or worker_plan)
        revision = prep.get("guided_revision")
        render_sections = {key for key, value in prep["sections"].items() if value}
        if not (render_sections - {"text_elements", "drawings"}):
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
            # KRI-589: a drawings-only Save is the same kind of save -- the
            # drawings compile onto the recipe after the plan, not into it.
            plan["text_elements"] = variant.get("text_elements") or []
        elif revision is not None:
            plan = compile_guided_runtime_plan(
                # A worker that repaired the approved plan for the phone (KRI-286)
                # pins the repaired one: recompiling from the canonical plan
                # would re-introduce what the repair removed.
                worker_plan,
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
        # KRI-374: a creator song is pinned in the previous immutable recipe too
        # (never re-hashed on Save). The song window was already re-fitted to the
        # committed duration (`compile_guided_runtime_plan`); a lip-sync take is
        # re-synced to its pinned delta, and a retimed one is refused, because the
        # song is the master clock and a take at another speed drifts off it.
        song = None
        if plan.get("user_song") is not None:
            refuse_lipsync_rate_change(plan)
            # The device-measured length of each take: a take dragged later in
            # the cut can need footage that was never filmed. That is refused
            # here (422 + reason), not silently shortened at compile time.
            plan = resync_lipsync_moments(
                plan,
                source_durations={
                    binding.media_id: float(binding.original.duration_s) for binding in bindings
                },
            )
            song = _pinned_song_bed(previous.recipe, plan["user_song"])
        # Exact-timed small cards the worker grounded (`phone_timed_cards` rows): a Save
        # recompiles from the plan, which never carried them, so they are re-attached here
        # exactly as pinned while the lane is still on (flag off: the Save drops the lane,
        # like every other refused phone lane -- never a 422).
        timed_cards = (
            timed_cards_from_rows(variant.get(TIMED_CARDS_FIELD))
            if phone_timed_cards_supported()
            else ()
        )
        allow_editor_media = bool(plan.get("editor_visual_blocks")) or bool(timed_cards)
        recipe = compile_phone_guided_plan(
            GuidedStoryExecutionPlan.model_validate(plan),
            bindings,
            visuals=visuals,
            narration=narration,
            allow_editor_media=allow_editor_media,
            song=song,
            sound_effects=_guided_sound_effects(plan, previous.recipe, visuals),
            timed_cards=timed_cards,
            # KRI-306/285: an editor Save keeps (or changes) the bars; without
            # this every guided Save would silently drop a letterbox.
            landscape_fit=_resolved_landscape_fit(
                variant, previous.recipe, prep.get("landscape_fit_override")
            ),
        )
        # KRI-589: the revision is the guided variant's canonical lanes.
        recipe = with_drawings(recipe, revision.get("drawings") if revision is not None else None)
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
                variant[PHONE_EDITOR_SAVED_PLAN_FIELD] = plan
                variant.update(song_reference_variant_fields(plan))
    staged.status = "awaiting_device"


def prepare_phone_editor_commit(
    job: Any,
    variant_id: str,
    *,
    prepare: Callable[[Any], dict],
    sfx_catalog_paths: Mapping[str, str] | None = None,
    creator_brief_binding: dict | None = None,
    learning_sections: set[str] | None = None,
) -> dict:
    """``sfx_catalog_paths`` maps a sound-effect catalog id to its
    `SoundEffect.audio_gcs_path` for the phone Talking effects a Save carries
    over without a known storage path (the caller's async catalog read,
    `generative_jobs._phone_subtitled_sfx_paths`; Save itself never queries)."""
    from app.services.phone_voiceover_timeline import (  # noqa: PLC0415
        require_phone_voiceover_source_audio_client,
    )

    before_variant = next(
        (
            row
            for row in (job.assembly_plan or {}).get("variants", [])
            if isinstance(row, dict) and row.get("variant_id") == variant_id
        ),
        None,
    )
    require_phone_voiceover_source_audio_client(before_variant)
    staged = _StagedJob(job)
    from app.services.finished_style_learning import learning_available, style_snapshot

    owner_id = getattr(job, "user_id", None)
    before_style = None
    if owner_id is not None and learning_available(owner_id):
        before_variant = next(
            (
                v
                for v in (job.assembly_plan or {}).get("variants", [])
                if v.get("variant_id") == variant_id
            ),
            {},
        )
        before_style = style_snapshot(before_variant, learning_sections or set())
    try:
        prep = prepare(staged)
    except HTTPException:
        raise
    except ValueError as exc:
        # KRI-770: an unreadable source catalog is a state problem the creator can recover
        # from by reloading, not a server fault.
        if str(exc) in {"editor_source_catalog_invalid", "editor_source_identity_conflict"}:
            raise HTTPException(409, detail={"code": "editor_source_catalog_invalid"}) from exc
        raise _unsupported_phone_edit(job, variant_id, exc) from exc
    except (KeyError, IndexError, TypeError) as exc:
        raise _unsupported_phone_edit(job, variant_id, exc) from exc
    if not prep["has_render_section"]:
        return {**prep, "render_destination": "device"}
    from app.services.phone_destination import phone_rendering_allowed_for_job  # noqa: PLC0415

    if not phone_rendering_allowed_for_job(job):
        raise HTTPException(422, detail={"code": "phone_rendering_unavailable"})
    stored_drawings = next(
        (
            v.get("drawings")
            for v in (job.assembly_plan or {}).get("variants", [])
            if isinstance(v, dict) and v.get("variant_id") == variant_id
        ),
        None,
    )
    if stored_drawings and not freehand_drawing_client_supported():
        # A recompile would pin a recipe (`drawingLayers`) this app build cannot decode.
        raise HTTPException(426, detail={"code": "client_update_required", "feature": "drawing"})
    stored_variant = next(
        (
            v
            for v in (job.assembly_plan or {}).get("variants", [])
            if isinstance(v, dict) and v.get("variant_id") == variant_id
        ),
        None,
    )
    if (
        stored_variant is not None
        and not speaker_background_client_supported()
        and variant_carries_speaker_background(job.assembly_plan or {}, stored_variant)
    ):
        # KRI-1117: a recompile would pin `speaker_background`, which this app build cannot
        # decode, or (compile strips it for such a build) silently take the creator's
        # background off. Nothing is committed.
        raise HTTPException(
            426,
            detail={
                "code": "client_update_required",
                "feature": "speaker_background",
                "minimum_client_protocol": settings.speaker_background_min_client_protocol,
            },
        )
    if (
        stored_variant is not None
        and not speaker_background_windows_client_supported()
        and variant_carries_speaker_background_windows(job.assembly_plan or {}, stored_variant)
    ):
        # KRI-1162 (S32): a recompile would pin `windows` / `speaker_frame`, which this app
        # build cannot decode, or (compile strips them for such a build) silently take them off.
        # Nothing is committed.
        raise HTTPException(
            426,
            detail={
                "code": "client_update_required",
                "feature": "speaker_background_windows",
                "minimum_client_protocol": settings.speaker_background_windows_min_client_protocol,
            },
        )
    try:
        _rebind_editor_render_contract(
            staged,
            variant_id,
            prep,
            brief_binding=creator_brief_binding,
            previous_variant=next(
                (
                    v
                    for v in (job.assembly_plan or {}).get("variants", [])
                    if isinstance(v, dict) and v.get("variant_id") == variant_id
                ),
                None,
            ),
        )
        staged_before_compile = copy.deepcopy(staged.assembly_plan)
        relaxed: list[dict[str, str]] = []
        while True:
            try:
                _compile_editor_commit(job, staged, variant_id, prep, sfx_catalog_paths)
                break
            except (TextPlacementError, AudioChoiceError) as exc:
                # KRI-736: a MANUAL editor Save is the creator's own choice, so it wins over
                # where an approved text sits (opening / closing / shot) and over the
                # camera-sound toggles. Relax exactly the requirement that no longer holds
                # (a text keeps its `any` presence check) and recompile from the
                # pre-compile staged state. Automatic callers never reach this loop.
                staged.assembly_plan = copy.deepcopy(staged_before_compile)
                if not _relax_requirement(staged, variant_id, exc):
                    raise
                relaxed.append(_relaxed_receipt(exc))
                staged_before_compile = copy.deepcopy(staged.assembly_plan)
                log.warning(
                    "phone_editor_commit_requirement_relaxed",
                    job_id=str(job.id),
                    variant_id=variant_id,
                    **relaxed[-1],
                )
        if relaxed:
            prep["requirements_relaxed"] = relaxed
    except RenderWindowUnavailable as exc:
        if not prep.get("preserve_editor_intent"):
            raise _unsupported_phone_edit(job, variant_id, exc) from exc
        # Section/source validation has finished. Roll back any partial
        # compilation only, retaining the validated authored revision and its
        # new generation for the route's durable snapshot transaction.
        staged.assembly_plan = staged_before_compile
        job.assembly_plan = staged.assembly_plan
        job.status = staged.status
        return {**prep, "render_destination": "device", "render_recovery": str(exc)}
    except LipsyncSyncError as exc:
        # The message is written for the creator (it says what to undo).
        log.warning(
            "phone_editor_commit_lipsync_refused",
            job_id=str(job.id),
            variant_id=variant_id,
            reason=str(exc),
        )
        raise HTTPException(
            422, detail={"code": "unsupported_phone_edit", "reason": str(exc)[:300]}
        ) from exc
    except GuidedStoryError as exc:
        if exc.code not in {USER_SONG_WINDOW_OUT_OF_RANGE, USER_SONG_LIPSYNC_LOCKED}:
            raise _unsupported_phone_edit(job, variant_id, exc) from exc
        # KRI-428: a song-bound refusal keeps its own code so the editor words it as a
        # song problem, not a text-style one.
        raise HTTPException(422, detail={"code": exc.code, "reason": str(exc)[:300]}) from exc
    except (
        KeyError,
        IndexError,
        StopIteration,
        TypeError,
        ValueError,
        UnsupportedPhonePlan,
    ) as exc:
        raise _unsupported_phone_edit(job, variant_id, exc) from exc
    if not text_bend_client_supported() and any(
        layer.bend for layer in device_status(staged, variant_id).request.recipe.text_layers
    ):
        # KRI-646: the recompiled recipe pins bent text (`textLayers[].bend`) that this app
        # build cannot decode or draw; nothing is committed.
        raise HTTPException(426, detail={"code": "client_update_required", "feature": "text_bend"})
    if (
        getattr(device_status(staged, variant_id).request.recipe, "speaker_background", None)
        is not None
        and not speaker_background_client_supported()
    ):
        # KRI-1117: the belt for a recompile that pinned a background this build cannot decode.
        raise HTTPException(
            426,
            detail={
                "code": "client_update_required",
                "feature": "speaker_background",
                "minimum_client_protocol": settings.speaker_background_min_client_protocol,
            },
        )
    staged_background = getattr(
        device_status(staged, variant_id).request.recipe, "speaker_background", None
    )
    if (
        staged_background is not None
        and staged_background.uses_windows
        and not speaker_background_windows_client_supported()
    ):
        # KRI-1162: the belt for a recompile that pinned windows this build cannot decode.
        raise HTTPException(
            426,
            detail={
                "code": "client_update_required",
                "feature": "speaker_background_windows",
                "minimum_client_protocol": settings.speaker_background_windows_min_client_protocol,
            },
        )
    if not freeze_frame_client_supported() and recipe_has_freeze_frame(
        device_status(staged, variant_id).request.recipe
    ):
        # KRI-828: a held frame an older build cannot decode; nothing is committed.
        raise HTTPException(
            426, detail={"code": "client_update_required", "feature": "freeze_frame"}
        )
    _settle_single_word_captions(staged, variant_id)
    job.assembly_plan = staged.assembly_plan
    job.status = staged.status
    if "started_at" in vars(staged):
        job.started_at = staged.started_at
    if before_style is not None:
        after_variant = next(
            (
                v
                for v in (job.assembly_plan or {}).get("variants", [])
                if v.get("variant_id") == variant_id
            ),
            {},
        )
        prep["style_learning_transition"] = {
            "before": before_style,
            "after": style_snapshot(after_variant, learning_sections or set()),
        }
    return {**prep, "render_destination": "device", "render_task_id": None}


def _settle_single_word_captions(staged: Any, variant_id: str) -> None:
    """One-word captions: refuse a build that cannot decode them, remember one that can.

    The recompiled recipe pins `karaoke.single_word` only when the compile knew the build
    could decode it, so the 426 below is the belt for a recipe that reached an older build
    some other way. The stamp is for the next compile that has NO request (a worker applying
    a chat edit): it cannot see the app build, so it trusts the last Save's answer. Written
    only while the feature is verified and only inside a request, so with the feature off the
    committed assembly plan is exactly what it was.
    """
    from app.services.client_protocol import in_http_request

    recipe = device_status(staged, variant_id).request.recipe
    if recipe_has_single_word_text(recipe) and not single_word_text_client_supported():
        raise HTTPException(
            426, detail={"code": "client_update_required", "feature": "single_word_text"}
        )
    if not in_http_request() or not single_word_text_verified():
        return
    qualifies = single_word_text_client_supported(require_client=True)
    for row in staged.assembly_plan.get("variants") or []:
        if isinstance(row, dict) and row.get("variant_id") == variant_id:
            if qualifies:
                row[SINGLE_WORD_CAPTIONS_STAMP] = True
            else:
                row.pop(SINGLE_WORD_CAPTIONS_STAMP, None)


def _guided_sound_effects(
    plan: dict, previous_recipe: object, visuals: tuple[PhoneVisualBinding, ...]
) -> tuple[ResolvedSoundEffect, ...]:
    """The guided plan's catalog sound effects as pinned phone assets (KRI-608).

    An effect the previous recipe already pins reuses that exact asset identity (no GCS
    probe), so a text-only Save keeps its effects untouched; a newly added catalog id is
    probed once. Raises (-> 422 ``unsupported_phone_edit``) while the lane is off, for a
    custom upload, or for an unplayable/invalid effect: Save never silently drops one.
    """
    rows = plan.get("editor_sound_effects") or []
    if not rows:
        return ()
    if not phone_guided_sfx_supported():
        raise UnsupportedPhonePlan(
            "sound effects can't be rendered on your iPhone yet", capability="soundEffects"
        )
    if any(not (isinstance(row, dict) and row.get("sound_effect_id")) for row in rows):
        raise UnsupportedPhonePlan(
            "a custom sound effect can't play on the iPhone yet; pick one from the library",
            capability="soundEffects",
        )
    previous_lanes = (
        lanes_from_recipe(previous_recipe, visuals=visuals)
        if isinstance(previous_recipe, EditRecipeV2)
        else None
    )
    return tuple(
        lanes_from_editor_sections(
            previous=previous_lanes,
            sound_effects=rows,
            media_overlays=None,
            visuals=visuals,
        ).sound_effects
    )


def _unsupported_phone_edit(job: Any, variant_id: str, exc: Exception) -> HTTPException:
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
    # A contract refusal keeps its typed reason on the wire (additive keys): the edit was
    # refused, the last accepted version is untouched, and the client can say why.
    typed = decline_payload(exc)
    return HTTPException(422, detail={"code": "unsupported_phone_edit", "reason": reason, **typed})


def _pinned_song_bed(recipe: EditRecipeV2, user_song: dict[str, Any]) -> PhoneSongBed:
    """The song receipt already pinned in ``recipe`` (KRI-374); Save never re-hashes it.

    The creator's editor volume (KRI-428) lives on the plan, so it is carried over here
    rather than reset to 1.0 on every Save. The start point is left unset: the compiler
    reads it from the plan.
    """
    asset = next((a for a in recipe.asset_manifest.assets if a.kind == "song"), None)
    if asset is None:
        raise ValueError("the previous phone recipe carries no song to keep")
    media = next(a for a in recipe.assets if a.id == asset.id)
    return PhoneSongBed(
        plan_item_id=asset.plan_item_id,
        generation=asset.generation,
        fingerprint=asset.fingerprint,
        duration_s=media.duration,
        volume=float(user_song.get("volume", 1.0)),
    )


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


def _background_lane(
    document: dict | None, visuals: tuple[PhoneVisualBinding, ...]
) -> SubtitledBackground | None:
    """The validated ``speaker_background`` section as a pinned lane request.

    The route validated the section against this job's pinned visuals; the bytes' path and
    generation come from that pin, never from the client.
    """
    if not document:
        return None
    binding = next((v for v in visuals if v.media_id == document["media_id"]), None)
    if binding is None:
        raise ValueError("that background isn't a visual Kria pinned for this Talking edit")
    windows: list[SubtitledBackgroundWindow] = []
    for entry in document.get("windows") or ():
        # KRI-1162: every window is a pinned pool visual too (the route validated each id).
        window_binding = next((v for v in visuals if v.media_id == entry["media_id"]), None)
        if window_binding is None:
            raise ValueError("that background isn't a visual Kria pinned for this Talking edit")
        windows.append(
            SubtitledBackgroundWindow(
                media_id=window_binding.media_id,
                gcs_path=window_binding.gcs_path,
                generation=window_binding.generation,
                kind=window_binding.kind,
                source_start_s=entry.get("source_start_s", 0.0),
                start_s=entry["start_s"],
                end_s=entry["end_s"],
            )
        )
    frame = document.get("speaker_frame")
    return SubtitledBackground(
        media_id=binding.media_id,
        gcs_path=binding.gcs_path,
        generation=binding.generation,
        kind=binding.kind,
        source_start_s=document.get("source_start_s", 0.0),
        windows=tuple(windows),
        speaker_frame=SubtitledSpeakerFrame(**frame) if frame else None,
    )


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
    if "speaker_background" in sections:
        row["speaker_background"] = sections["speaker_background"]
    else:
        row.pop("speaker_background", None)
    if duck_receipt is not None:
        row[SFX_DUCK_RECEIPT_FIELD] = duck_receipt
    else:
        row.pop(SFX_DUCK_RECEIPT_FIELD, None)


def _require_card_tilt_supported(previous: Any, lanes: Any) -> None:
    """KRI-1141: refuse a NEW or CHANGED card angle while the tilt gates are closed.

    Keeping a stored angle and straightening one are always allowed (a Save that does not touch
    the tilt of a card the edit already holds must not fail because the kill switch was flipped
    afterwards).
    """
    held = {card.id: card.rotation_deg for card in (previous.overlays if previous else [])}
    changed = [
        card
        for card in lanes.overlays
        if card.rotation_deg != 0 and card.rotation_deg != held.get(card.id, 0.0)
    ]
    if changed and not card_tilt_supported():
        raise ValueError("tilting a picture isn't available on this edit yet")


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
    allowed_sections = _SUBTITLED_EDITOR_SECTIONS | (
        _SUBTITLED_TEXT_SECTIONS if phone_subtitled_title_supported() else frozenset()
    )
    unsupported_sections = active_sections - allowed_sections
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
        background_touched=bool(prep.get("speaker_background_touched")),
        background=_background_lane(prep.get("speaker_background_override"), visuals),
    )

    _require_card_tilt_supported(previous_lanes, lanes)

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
    # KRI-719: a multi-clip Talking edit can also play several speakers back
    # to back; they carry over in their pinned order, like the cutaways.
    speaker = bindings[0]
    cutaways: tuple = ()
    additional_speakers: tuple = ()
    if isinstance(previous.recipe, EditRecipeV2):
        speaker = speaker_binding_from_recipe(previous.recipe, bindings)
        cutaways = cutaways_from_recipe(previous.recipe, bindings)
        if not phone_talking_cutaway_sound_supported():
            cutaways = tuple(c.model_copy(update={"sound": False}) for c in cutaways)
        additional_speakers = additional_speakers_from_recipe(previous.recipe, bindings)
    elif len(bindings) != 1:
        raise ValueError("multi-clip phone Talking edits need a pinned recipe")
    keep_segments = (
        _previous_keep_segments(previous.recipe, speaker.render_asset().id)
        if isinstance(previous.recipe, EditRecipeV2)
        else None
    )
    # KRI-283: a letterboxed (landscape + fit) variant keeps its bars on Save.
    landscape_fit = _resolved_landscape_fit(
        variant, previous.recipe, prep.get("landscape_fit_override")
    )
    # A landscape canvas never has bars and has no sideways overflow to face-fill, so it
    # compiles "fill" with no speaker shift whatever bars choice is persisted (the choice
    # re-opens, as persisted, when the creator switches back to portrait).
    output_orientation = _resolved_output_orientation(
        variant, previous.recipe, prep.get("orientation_override")
    )
    landscape_canvas = output_orientation == "landscape"
    canvas_changed = (_recipe_orientation(previous.recipe) or "portrait") != output_orientation
    if landscape_canvas:
        landscape_fit = "fill"
    # KRI-547: a face-filled speaker keeps its crop on Save, drops it when the
    # creator picks black bars, and gets it back when they pick crop again.
    # A variant without a framing receipt compiles exactly as before. Imported here:
    # `phone_speaker_framing` pulls in skia via `render_geometry`, and this module is on
    # the routes' import path (the kria contracts CLI imports it without libEGL).
    from app.pipeline.phone_speaker_framing import (  # noqa: PLC0415
        SPEAKER_FRAMING_FIELD,
        editor_speaker_framing,
        landscape_canvas_framing,
    )

    if landscape_canvas:
        # No face-fill on a wide canvas: the receipt says so (`landscape_canvas`) but keeps its
        # window, checks and prior mode, so a switch back to portrait + crop re-derives the
        # face crop (`editor_speaker_framing`).
        speaker_position_x = None
        framing_receipt = landscape_canvas_framing(variant.get(SPEAKER_FRAMING_FIELD))
    else:
        speaker_position_x, framing_receipt = editor_speaker_framing(
            variant.get(SPEAKER_FRAMING_FIELD), landscape_fit=landscape_fit
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
        additional_speakers=additional_speakers,
        landscape_fit=landscape_fit,
        # KRI-467: the staged row's text (already this Save's, when it carries
        # a text section), in the cut-timeline seconds the cues use.
        text_elements=variant.get("text_elements") or [],
        text_elements_user_edited=bool(variant.get("text_elements_user_edited")),
        **({"speaker_position_x": speaker_position_x} if speaker_position_x is not None else {}),
        # Only a landscape canvas names itself: the portrait call stays byte-identical.
        **({"output_orientation": "landscape"} if landscape_canvas else {}),
        **({"clip_tilts": tilts} if (tilts := _previous_clip_tilts(previous.recipe)) else {}),
    )
    recipe = with_drawings(recipe, variant.get("drawings"))
    # A native same-source cut normally borrows 25 ms of removed audio to
    # smooth pause cleanup. An explicit spoken-word removal must instead keep
    # its source edges closed, including on a processed-audio derivative.
    from app.pipeline.explicit_speech_audio import (  # noqa: PLC0415
        protect_explicit_speech_cut_audio,
    )
    from app.pipeline.phone_processed_audio import (  # noqa: PLC0415
        processed_audio_receipt_for,
        replace_audio_with_processed_segments,
    )

    recipe = protect_explicit_speech_cut_audio(
        recipe,
        source_media_id=speaker.media_id,
        cuts=variant.get("explicit_speech_cuts"),
    )
    # Noise reduction remains experimental: only restore its track when this
    # exact immutable prior recipe already carried it. A receipt alone must
    # never make an unrelated Save opt into a new audio treatment.
    prior_has_processed_audio = any(
        track.id == "processed-source-audio" for track in previous.recipe.tracks
    )
    if prior_has_processed_audio:
        processed_receipts = {
            binding.media_id: receipt
            for binding in bindings
            if (
                receipt := processed_audio_receipt_for(
                    assembly, variant_id=variant_id, source_media_id=binding.media_id
                )
            )
            is not None
        }
        required_processed_sources = {
            asset.source_media_id
            for asset in previous.recipe.asset_manifest.assets
            if asset.kind == "processed_audio"
        }
        if not required_processed_sources.issubset(processed_receipts):
            raise ValueError(
                "The saved noise-reduced audio is unavailable. Please regenerate this edit."
            )
        recipe = replace_audio_with_processed_segments(
            recipe, receipts_by_source=processed_receipts
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
        if framing_receipt is not None:
            row[SPEAKER_FRAMING_FIELD] = framing_receipt
        if landscape_canvas:
            row["orientation"] = "landscape"
            # The variant carries the canvas's own truth: a landscape one never has bars.
            row["landscape_fit"] = "fill"
        elif canvas_changed or "orientation" in prep["sections"]:
            row["orientation"] = "portrait"
        _restale_protected_text(
            row,
            speaker=speaker,
            keep_segments=keep_segments,
            landscape_fit=landscape_fit,
            speaker_position_x=speaker_position_x,
            canvas_changed=canvas_changed,
        )
        _persist_lane_state(
            row, lanes, labels, paths, caption_style=caption_style, duck_receipt=duck_receipt
        )


def _restale_protected_text(
    row: dict,
    *,
    speaker: Any,
    keep_segments: list[tuple[float, float]] | None,
    landscape_fit: str,
    speaker_position_x: float | None,
    canvas_changed: bool = False,
) -> None:
    """KRI-827: a Save that changed the cut, the crop or the bars the protected-text
    receipt was measured with marks it ``stale`` (the review then says the text was not
    re-checked), so "no text on the plate" is never reported met from the first render's
    numbers. Text / caption edits are caught by the receipt's own fingerprint."""
    from app.kria.protected_targets import (  # noqa: PLC0415
        PROTECTED_TEXT_FIELD,
        protected_text_after_editor_save,
    )
    from app.pipeline.phone_recipe_shared import display_dims  # noqa: PLC0415

    receipt = row.get(PROTECTED_TEXT_FIELD)
    if not isinstance(receipt, Mapping):
        return
    if canvas_changed:
        # The receipt's boxes were measured on the other canvas (portrait <-> landscape);
        # nothing it stores says which, so the verdict is never carried across.
        from app.kria.protected_targets import stale_protected_text  # noqa: PLC0415

        row[PROTECTED_TEXT_FIELD] = (
            receipt
            if receipt.get("stale_reason")
            else stale_protected_text(receipt, "edited_geometry")
        )
        return
    try:
        width, height = display_dims(speaker.original)
        landscape = width > height
        full = [(0.0, float(speaker.original.duration_s))]
    except Exception:  # noqa: BLE001 - unknown geometry: compare what is known
        landscape, full = True, []
    row[PROTECTED_TEXT_FIELD] = protected_text_after_editor_save(
        receipt,
        keep_segments=keep_segments if keep_segments is not None else full,
        landscape_fit=landscape_fit if landscape else None,
        speaker_position_x=speaker_position_x,
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
    _require_card_tilt_supported(previous_lanes, lanes)
    recipe = replace_editor_lanes(
        recipe, lanes=lanes, visuals=visuals, video_track_id=video_track_id
    )
    labels, paths = _settle_lane_labels_and_paths(
        lanes, labels, paths, prep=prep, sfx_catalog_paths=sfx_catalog_paths
    )
    return recipe, lanes, labels, paths


def _voiceover_sources(
    staged: Any, assembly: dict
) -> tuple[tuple[PhoneSourceBinding, ...], list[str]]:
    """``(bindings, pool)`` a phone Voiceover slot's ``clip_index`` resolves through."""
    bindings = tuple(
        PhoneSourceBinding.model_validate(row) for row in assembly.get(PHONE_SOURCES_FIELD) or []
    )
    return bindings, source_pool_paths(assembly, getattr(staged, "all_candidates", None) or {})


def _swap_voiceover_cut(
    recipe: EditRecipeV2,
    prep: dict,
    bindings: tuple[PhoneSourceBinding, ...],
    pool: list[str],
    *,
    archetype: str,
    landscape_fit: str | None = None,
    visuals: Sequence[PhoneVisualBinding] = (),
) -> EditRecipeV2:
    """KRI-290: the committed creator cut (resolved by
    `resolve_phone_voiceover_slots`) swapped into ``recipe``'s video track."""
    if not settings.phone_voiceover_timeline_edits_enabled:
        raise ValueError("phone Voiceover clip edits aren't available right now")
    slots = [slot for slot in prep.get("timeline_override") or [] if not slot.get("removed")]
    if not slots:
        raise ValueError("a phone Voiceover clip edit needs at least one clip")
    swapped = replace_voiceover_cut(
        recipe,
        archetype=archetype,
        slots=slots,
        bindings=bindings,
        pool=pool,
        landscape_fit=landscape_fit,
        visuals=visuals,
    )
    for slot in prep["timeline_override"]:
        # A cleared tilt was the compile's instruction; the stored row is simply upright.
        if "tilt" in slot and slot["tilt"] is None:
            del slot["tilt"]
    return swapped


def _has_editor_lanes(recipe: EditRecipeV2, variant: dict) -> bool:
    """A cut change must re-bound the sound-effect / Visuals lanes too --
    including ones an earlier, shorter cut pushed past the end, which only the
    persisted lane state still remembers."""
    return bool(variant.get(PHONE_SUBTITLED_EDITOR_LANES_FIELD)) or any(
        track.id in _EDITOR_LANE_TRACK_IDS for track in recipe.tracks
    )


def _has_editor_media(recipe: EditRecipeV2, variant: dict) -> bool:
    """A cut change must re-bound added photos/videos (KRI-287) too, including
    ones an earlier, shorter cut pushed past the end."""
    return bool(variant.get("visual_blocks")) or any(
        track.id == EDITOR_MEDIA_TRACK_ID and track.clips for track in recipe.tracks
    )


def _fit_media_blocks_to_cut(blocks: list[dict], end_s: float) -> list[dict]:
    """KRI-290: saved media blocks clipped to a (possibly shorter) cut for this
    render only. The saved blocks stay intact, so a longer cut brings them back."""
    from app.agents._schemas.visual_block import MIN_MEDIA_DURATION_S  # noqa: PLC0415

    fitted = []
    for block in blocks:
        start = float(block.get("start_s") or 0.0)
        end = min(float(block.get("end_s") or 0.0), end_s)
        if end - start + 1e-6 < MIN_MEDIA_DURATION_S:
            continue
        fitted.append(block if end == block.get("end_s") else {**block, "end_s": end})
    return fitted


def _commit_voiceover_media(
    recipe: EditRecipeV2,
    *,
    assembly: dict,
    variant: dict,
    prep: dict,
    video_track_id: str,
) -> EditRecipeV2:
    """Recompile a phone Voiceover recipe's editor media layer from the saved
    ``visual_blocks`` (KRI-287), against the generation's pinned visuals plus
    every photo/video the editor admitted (async source registration)."""
    visuals = _assembly_visuals(assembly) + editor_visual_bindings(variant)
    blocks = prep.get("visual_blocks_override")
    if blocks is None:
        blocks = variant.get("visual_blocks") or []
    blocks = list(blocks)
    if prep["sections"].get("timeline"):
        video = next(track for track in recipe.tracks if track.id == video_track_id)
        blocks = _fit_media_blocks_to_cut(blocks, timeline_end_s(video.clips))
    return replace_editor_media(
        recipe, blocks=blocks, visuals=visuals, video_track_id=video_track_id
    )


def _voiceover_allows_editor_media(recipe: EditRecipeV2) -> bool:
    """A Voiceover recipe may carry placements only from the editor's own
    qualified compilers: Visuals cards (``subtitled-overlays``) or added media
    (``editor-media``). Pinned layers carry forward on any later Save."""
    return any(
        track.id in {"subtitled-overlays", "subtitled-fullscreen", EDITOR_MEDIA_TRACK_ID}
        and track.clips
        for track in recipe.tracks
    )


def _apply_original_level(recipe: EditRecipeV2, variant: dict, prep: dict) -> EditRecipeV2:
    """Play the footage's own sound at the creator's saved level (KRI-821).

    Only ``audio.original_volume`` changes: the narration clip and a music bed are their
    own audio-track clips and keep their pinned volumes. A music level is not a phone
    Voiceover control, so a Save carrying one is refused rather than dropped.
    """
    if prep.get("mix_override") is not None:
        raise ValueError("music level isn't supported on phone Voiceover edits yet")
    level = variant.get("original_audio_level")
    if isinstance(level, bool) or not isinstance(level, (int, float)):
        raise ValueError("phone Voiceover original audio level is missing")
    audio = recipe.audio.model_copy(update={"original_volume": max(0.0, min(1.0, float(level)))})
    return recipe.model_copy(update={"audio": audio})


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
    pinned recipe (`replace_narrated_captions`), (KRI-281) recompile the
    sound-effect / Visuals lanes (`replace_editor_lanes`), and (KRI-465)
    recompile the opening title / added text (`replace_narrated_title`).

    ``variant`` is the STAGED row, so `_prepare_editor_commit` has already
    written the validated cues and caption-meta fields onto it. The narration
    bed and the audio mix come from ``previous``, and so do the clips unless
    the Save carries a timeline (KRI-290, `_swap_voiceover_cut`) -- Save
    never re-downloads, re-transcribes or re-aligns the voiceover. Raises
    ``ValueError``/``UnsupportedPhonePlan`` on anything unsupported, caught by
    the same broad ``except`` in `prepare_phone_editor_commit`.
    """
    active_sections = {key for key, value in prep["sections"].items() if value}
    caption_active = active_sections & _NARRATED_CAPTION_SECTIONS
    lanes_active = active_sections & _VOICEOVER_LANE_SECTIONS
    media_active = active_sections & _VOICEOVER_MEDIA_SECTIONS
    cut_active = bool(active_sections & _VOICEOVER_CUT_SECTIONS)
    text_active = bool(active_sections & _NARRATED_TEXT_SECTIONS)
    drawings_active = bool(active_sections & _DRAWING_SECTIONS)
    original_active = bool(active_sections & _ORIGINAL_LEVEL_SECTIONS)
    lanes_ok = phone_voiceover_editor_lanes_supported(require_client=False)
    media_ok = phone_voiceover_editor_media_supported(require_client=False)
    text_ok = phone_narrated_title_edits_supported(require_client=False)
    if lanes_active and not lanes_ok:
        raise ValueError("phone Voiceover sound effects and Visuals aren't editable yet")
    if media_active and not media_ok:
        raise ValueError("phone Voiceover photos and videos aren't editable yet")
    if text_active and not text_ok:
        raise ValueError("phone Voiceover titles aren't editable yet")
    # A caption edit, or a Save with nothing else to do, is a caption Save.
    caption_save = bool(caption_active) or not (
        lanes_active
        or media_active
        or cut_active
        or text_active
        or drawings_active
        or original_active
    )
    if caption_save and not phone_narrated_caption_edits_supported():
        raise ValueError("phone Narrated captions aren't editable yet")

    allowed = (
        _NARRATED_EDITOR_SECTIONS
        | _VOICEOVER_CUT_SECTIONS
        | _ORIGINAL_LEVEL_SECTIONS
        | (_VOICEOVER_LANE_SECTIONS if lanes_ok else frozenset())
        | (_VOICEOVER_MEDIA_SECTIONS if media_ok else frozenset())
        | (_NARRATED_TEXT_SECTIONS if text_ok else frozenset())
    )
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
    # KRI-589: patch from the pinned recipe WITHOUT its drawing layers (a shorter cut would
    # leave them past the end, which the recipe validator refuses) and re-add them from the
    # stored lane at the end, whichever sections this Save carried.
    recipe = with_drawings(previous.recipe, None)
    if cut_active:
        bindings, pool = _voiceover_sources(staged, assembly)
        recipe = _swap_voiceover_cut(
            recipe,
            prep,
            bindings,
            pool,
            archetype="narrated",
            visuals=_assembly_visuals(assembly) + editor_visual_bindings(variant),
        )
    title_elements: list[dict] = []
    if text_active:
        # The STAGED row: `_prepare_editor_commit` validated the Save's
        # `text_elements` and wrote them here. The caption mirrors the editor
        # document also sends are the caption lane's, not title text.
        title_elements = narrated_authored_text_elements(variant.get("text_elements") or [])
        recipe = replace_narrated_title(recipe, title_elements)
    elif cut_active and variant.get("spoken_text_placement") is not None:
        # Narration keeps its clock when footage moves. Re-project from canonical rows,
        # so shortening then extending restores spoken text without stretching its hold.
        recipe = replace_narrated_title(
            recipe,
            narrated_authored_text_elements(variant.get("narrated_title_text_elements") or []),
        )
    if caption_save or cut_active:
        # A new cut re-bounds the captions to where the video now ends.
        caption_style = "word" if variant.get("voiceover_caption_style") == "word" else "sentence"
        recipe = replace_narrated_captions(
            recipe,
            caption_cues=caption_cues,
            caption_style=caption_style,
            look=caption_look_from_variant(variant),
        )
    lane_state = None
    if lanes_active or (cut_active and _has_editor_lanes(previous.recipe, variant)):
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
    if media_active or (cut_active and _has_editor_media(previous.recipe, variant)):
        recipe = _commit_voiceover_media(
            recipe, assembly=assembly, variant=variant, prep=prep, video_track_id="narrated"
        )
    if original_active:
        recipe = _apply_original_level(recipe, variant, prep)
    recipe = with_drawings(recipe, variant.get("drawings"))
    validate_phone_pilot_recipe(recipe, allow_editor_media=_voiceover_allows_editor_media(recipe))
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
        if row.get("phone_beat_receipt"):
            from app.pipeline.narrated_photo_sequence import (  # noqa: PLC0415
                refresh_narrated_sequence_receipt,
            )

            row["phone_beat_receipt"] = refresh_narrated_sequence_receipt(
                recipe, row["phone_beat_receipt"]
            )
        if isinstance(row.get("clip_sound"), dict):
            # KRI-842: the receipt follows the recipe this Save pinned, never what the first
            # render promised.
            row["clip_sound"] = reconcile_clip_sound(recipe, row["clip_sound"])
        if caption_cues_overridden:
            row["caption_cues"] = caption_cues
        if text_active:
            # One store for the title: `narrated_title_text_elements` is what the
            # status route shows, so the staged `text_elements` must not keep a
            # second copy (it would render the title twice).
            if title_elements:
                row["narrated_title_text_elements"] = title_elements
            else:
                row.pop("narrated_title_text_elements", None)
            row.pop("text_elements", None)
            row.pop("text_elements_user_edited", None)
        if cut_active:
            # Keep the persisted pair (admin/debug, older app builds) on the new cut.
            projected_cut = narrated_timings_and_assignments(recipe, list(bindings), pool)
            if projected_cut is not None:
                row["narrated_timings"] = projected_cut[0]
                row["narrated_clip_assignments"] = projected_cut[1]
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
    """Save for a phone montage `voiceover` variant (KRI-281): the sound-effect
    and Visuals lanes, (KRI-287) added photos/videos and (KRI-290) the clip cut
    are editable; the voice and music beds and intro text keep their pinned
    shape, re-fitted to the cut."""
    active_sections = {key for key, value in prep["sections"].items() if value}
    lanes_active = active_sections & _VOICEOVER_LANE_SECTIONS
    media_active = active_sections & _VOICEOVER_MEDIA_SECTIONS
    cut_active = bool(active_sections & _VOICEOVER_CUT_SECTIONS)
    # KRI-589: a drawings-only Save touches no lane the sound-effect / Visuals rollout owns.
    # (An empty Save is not drawings-only: `set() <= x` is true, so it keeps its old gates.)
    # KRI-821: an Original-audio Save is the same kind of lane-free Save.
    original_active = bool(active_sections & _ORIGINAL_LEVEL_SECTIONS)
    only_drawings = bool(active_sections) and active_sections <= (
        _DRAWING_SECTIONS | _ORIGINAL_LEVEL_SECTIONS
    )
    if (
        (lanes_active or not cut_active)
        and not only_drawings
        and not phone_voiceover_editor_lanes_supported(require_client=False)
    ):
        raise ValueError("phone Voiceover sound effects and Visuals aren't editable yet")
    if media_active and not phone_voiceover_editor_media_supported(require_client=False):
        raise ValueError("phone Voiceover photos and videos aren't editable yet")
    unsupported_sections = (
        active_sections
        - _VOICEOVER_LANE_SECTIONS
        - _VOICEOVER_MEDIA_SECTIONS
        - _VOICEOVER_CUT_SECTIONS
        - _VOICEOVER_FIT_SECTIONS
        - _DRAWING_SECTIONS
        - _ORIGINAL_LEVEL_SECTIONS
    )
    if unsupported_sections:
        name = sorted(unsupported_sections)[0]
        raise ValueError(f"{name} isn't supported on phone Voiceover edits yet")
    if not isinstance(previous.recipe, EditRecipeV2):
        raise ValueError("phone Voiceover edits need a pinned v2 recipe")

    # KRI-589: patch from the pinned recipe without its drawing layers (see the narrated twin).
    recipe = with_drawings(previous.recipe, None)
    if cut_active:
        bindings, pool = _voiceover_sources(staged, assembly)
        recipe = _swap_voiceover_cut(
            recipe,
            prep,
            bindings,
            pool,
            archetype="voiceover",
            landscape_fit=_resolved_landscape_fit(
                variant, previous.recipe, prep.get("landscape_fit_override")
            ),
            visuals=_assembly_visuals(assembly) + editor_visual_bindings(variant),
        )
    fit_override = prep.get("landscape_fit_override")
    if fit_override is not None:
        fit_bindings, _pool = _voiceover_sources(staged, assembly)
        recipe = apply_landscape_fit(recipe, fit_bindings, fit_override)
    lane_state = None
    if not only_drawings and (
        lanes_active or not cut_active or _has_editor_lanes(previous.recipe, variant)
    ):
        lane_state = _commit_voiceover_lanes(
            recipe,
            assembly=assembly,
            variant=variant,
            previous=previous,
            prep=prep,
            sfx_catalog_paths=sfx_catalog_paths,
            video_track_id="montage",
        )
        recipe = lane_state[0]
    if media_active or (cut_active and _has_editor_media(previous.recipe, variant)):
        recipe = _commit_voiceover_media(
            recipe, assembly=assembly, variant=variant, prep=prep, video_track_id="montage"
        )
    if original_active:
        recipe = _apply_original_level(recipe, variant, prep)
    recipe = with_drawings(recipe, variant.get("drawings"))
    validate_phone_pilot_recipe(recipe, allow_editor_media=_voiceover_allows_editor_media(recipe))
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
        if lane_state is not None:
            _, lanes, labels, paths = lane_state
            _persist_lane_state(
                row, lanes, labels, paths, caption_style="sentence", duck_receipt=None
            )


def _previous_clip_tilts(recipe: Any) -> dict[str, dict]:
    """Each main-track clip's tilt (KRI-1136), by clip id, so a lane Save that recompiles the
    speaker clips keeps what the creator tilted."""
    if not isinstance(recipe, EditRecipeV2):
        return {}
    tilts = {}
    for track in recipe.tracks:
        if track.kind == "video" and track.id in FIT_VIDEO_TRACK_IDS:
            for clip in track.clips:
                if (tilt := row_tilt_from_clip(clip)) is not None:
                    tilts[clip.id] = tilt
    return tilts


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
