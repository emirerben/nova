"""Pinned creator requirements and a narrow verifier for portable phone recipes.

Approval pins objective requirements; the verifier reads the compiled timeline,
text and audio tracks. Unsupported evidence is an explicit refusal. This does
not claim native pixel/audio playback verification or cover every strategy field.
"""

from __future__ import annotations

import hashlib
import json
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, fields, is_dataclass
from types import UnionType
from typing import Annotated, Any, Literal, Union, get_args, get_origin, get_type_hints

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.agents._schemas.creator_agent import CreativeStrategy
from app.agents._schemas.edit_format import NARRATED_EDIT_FORMATS
from app.kria.brief import CreativeBrief
from app.kria.brief_binding import SNAPSHOT_VISUALS_KEY
from app.kria.brief_checks import is_spoken_text_cue
from app.kria.brief_route import chapter_list
from app.kria.freeze_ask import times_one_moment
from app.kria.recipes_v2 import EditRecipeV2
from app.kria.render_assets import (
    OriginalRenderAsset,
    ProcessedAudioRenderAsset,
    VisualRenderAsset,
    VoiceoverRenderAsset,
)
from app.kria.reply_language import say
from app.kria.spoken_text_ask import requirement_spoken_text
from app.pipeline.hidden_parts import (
    HiddenPart,
    hidden_parts_from_strategy,
    shows_hidden,
    unsettled_hidden_parts,
)
from app.pipeline.phone_guided_plan import UnsupportedPhonePlan
from app.pipeline.phone_recipe_shared import voice_tail_slack_s
from app.pipeline.sequence_text_evidence import text_matches
from app.schemas.clip_intents import MAX_HIDDEN_PART_S
from app.services.choice_questions import (
    ATTACHMENT_ORDER_KEY,
    CAPTURE_ORDER_KEYS,
    CONFLICT_DURATION_VS_COUNT,
    CONFLICT_ORDER_BASIS,
    CONFLICT_TEXT_PLACEMENT,
    CONFLICT_VOICE_VS_DURATION,
    OPT_ATTACHMENT_ORDER,
    OPT_SILENT_TAIL,
    OPT_UNORDERED,
    is_pin_only_order,
    one_clip_edit,
    pin_anchors,
)
from app.services.clip_file_names import clip_display_name
from app.services.clip_order_sequence import (
    apply_sequence,
    sequence_rows,
    stated_anchors,
    unresolved_questions,
)
from app.services.creator_label_reply import label_text_reply
from app.services.length_policy import (
    DurationBound,
    creator_duration,
    creator_duration_s,
    length_owner,
)
from app.services.order_mode import order_mode

CONTRACT_FIELD = "creator_render_requirements"
REQUIREMENT_VERSION_FIELD = "creator_render_requirements_version"
# KRI-470 kill-switch stamp in Job.all_candidates. Written once at contract stamp
# time when KRIA_PLAN_AUTHORITY_ENABLED is on; workers branch on the stamp and
# never on the live flag. Absent = legacy behaviour.
PLAN_AUTHORITY_FIELD = "creator_plan_authority_version"
PLAN_AUTHORITY_VERSION = 1


def stamp_plan_authority(all_candidates: Mapping[str, Any], *, enabled: bool) -> dict[str, Any]:
    """``all_candidates`` plus the KRI-470 stamp when the flag is on (else unchanged).

    Called once per job, where its contract is first stamped.  The flag is read by
    the caller at that moment and never again: workers branch on the persisted key.
    """

    stamped = dict(all_candidates)
    if enabled:
        stamped[PLAN_AUTHORITY_FIELD] = PLAN_AUTHORITY_VERSION
    return stamped


# Job.assembly_plan key holding the typed decline of a terminal contract failure:
# {"decline_reason", "field_path"?, "alternative"?}.  Written beside (never in
# place of) the job's failure_reason so existing matchers keep working.
CREATOR_DECLINE_FIELD = "creator_decline"

DeclineReason = Literal[
    "capability_unavailable", "evidence_missing", "requirement_conflict", "needs_choice"
]
"""Why a render path declined an approved requirement.

* ``capability_unavailable`` -- this render path can never honour or prove the
  requirement (the creator needs a different path or a different request).
* ``evidence_missing`` -- the requirement is supported, but the compiled output
  did not demonstrate it (repair or retry may fix it).
* ``requirement_conflict`` -- two approved requirements cannot both hold.
* ``needs_choice`` -- the requirement is ambiguous until the creator decides.
"""

DECLINE_REASONS: tuple[str, ...] = get_args(DeclineReason)


class CreatorRenderContractError(UnsupportedPhonePlan):
    """A confirmed creator requirement is absent from a device recipe.

    ``decline_reason`` / ``field_path`` / ``alternative`` are optional so that
    legacy raise sites keep working; typed sites let the reason survive every
    layer between the verifier and the creator thread (see
    ``decline_payload``). ``missing_text`` is the confirmed on-screen text the
    edit lacks, so the chat can name it (KRI-701).
    """

    def __init__(
        self,
        message: str,
        *,
        decline_reason: DeclineReason | None = None,
        field_path: str | None = None,
        alternative: str | None = None,
        missing_text: str | None = None,
    ) -> None:
        super().__init__(message)
        self.decline_reason = decline_reason
        self.field_path = field_path
        self.alternative = alternative
        self.missing_text = missing_text


class TextPlacementError(CreatorRenderContractError):
    """A confirmed text IS on screen, but not where its approved role requires.

    Carries the failed requirement so a MANUAL editor Save (the creator moved, re-timed or
    re-worded the text on purpose) can relax exactly that role to ``any`` (KRI-736) while
    every automatic caller keeps treating it as the ordinary refusal it subclasses.
    """

    def __init__(self, message: str, *, requirement: TextRequirement, **kwargs: Any) -> None:
        super().__init__(message, **kwargs)
        self.requirement = requirement


class AudioChoiceError(CreatorRenderContractError):
    """The camera-sound requirement no longer matches what the edit plays.

    ``field`` is the contract field (``original_audio`` or ``audio_source_ids``). A MANUAL
    editor Save (the creator muted or un-muted a clip, or moved the camera-sound level)
    clears that field instead of refusing (KRI-736); automatic callers see the ordinary
    refusal this subclasses.
    """

    def __init__(self, message: str, *, field: str, **kwargs: Any) -> None:
        super().__init__(message, **kwargs)
        self.field = field


def decline_payload(exc: BaseException) -> dict[str, str]:
    """The typed decline persisted next to a failure code (empty when untyped)."""

    reason = getattr(exc, "decline_reason", None)
    if reason not in DECLINE_REASONS:
        return {}
    payload = {"decline_reason": str(reason)}
    for key in ("field_path", "alternative", "missing_text"):
        value = getattr(exc, key, None)
        if isinstance(value, str) and value:
            payload[key] = value
    return payload


# --- Field matrix ------------------------------------------------------------
#
# Every path of ``CreativeStrategy`` (nested, lists marked ``[]``) is assigned a
# disposition and an owner.  The path set is derived from the pydantic schema,
# so a new field or nested field fails ``test_every_strategy_path_is_assigned``
# until it is classified here.
#
#   supported          the contract pins it and a verifier produces evidence
#                      (see the adapter declarations for which adapters).
#   preference_only    taste; any renderer may adapt it, nothing verifies it.
#   upstream_resolved  consumed/resolved/repaired before the contract (planner,
#                      capability policy, server resolvers); not re-verified.
#   unsupported        a creator can state it, but no component proves it; a
#                      request that depends on it must not be treated as met.
#
# A disposition is NOT evidence that every renderer enforces the field.

Disposition = Literal["supported", "preference_only", "upstream_resolved", "unsupported"]


@dataclass(frozen=True)
class FieldRule:
    disposition: Disposition
    owner: str
    note: str = ""


def _rules(
    disposition: Disposition, owner: str, *paths: str, note: str = ""
) -> dict[str, FieldRule]:
    return {path: FieldRule(disposition, owner, note) for path in paths}


_CLIP_INTENT_LEAVES = (
    "intent_id",
    "op",
    "attribute",
    "source_kind",
    "label_source",
    "transcript_kind",
    "creator_text",
    "caption_attribute",
    "placeholder",
    "position",
    "order_by",
    "ordered_members",
    "exclusive",
    # KRI-994: the brief `select` ask an exclusion carries out; read only by receipts.
    "requirement_id",
    # KRI-1031: a length-only exclusion, settled from clip lengths while planning (the
    # narrowed pick is what the contract sees).
    "by_length",
    "under_s",
)


def _model_children(annotation: Any) -> list[type[BaseModel]]:
    origin = get_origin(annotation)
    if origin is Annotated:
        return _model_children(get_args(annotation)[0])
    if origin is not None:
        found: list[type[BaseModel]] = []
        for arg in get_args(annotation):
            found += _model_children(arg)
        return found
    if isinstance(annotation, type) and (
        issubclass(annotation, BaseModel) or is_dataclass(annotation)
    ):
        return [annotation]
    return []


def _is_list(annotation: Any) -> bool:
    origin = get_origin(annotation)
    if origin is Annotated:
        return _is_list(get_args(annotation)[0])
    if origin in (Union, UnionType):
        return any(_is_list(arg) for arg in get_args(annotation))
    return origin in (list, tuple, set, frozenset)


def schema_field_paths(model: type[BaseModel] = CreativeStrategy, prefix: str = "") -> set[str]:
    """Every nested field path of ``model``; list-typed fields end in ``[]``."""

    paths: set[str] = set()
    annotations = (
        {name: field.annotation for name, field in model.model_fields.items()}
        if issubclass(model, BaseModel)
        else {field.name: get_type_hints(model)[field.name] for field in fields(model)}
    )
    for name, annotation in annotations.items():
        path = prefix + name + ("[]" if _is_list(annotation) else "")
        paths.add(path)
        for child in _model_children(annotation):
            paths |= schema_field_paths(child, path + ".")
    return paths


FIELD_MATRIX: dict[str, FieldRule] = {
    **_rules(
        "upstream_resolved",
        "confirmed_order_execution",
        "confirmed_order_replacement",
        "confirmed_order_replacement.option_key",
        "confirmed_order_replacement.proposal",
        "confirmed_order_replacement.proposal.moments[]",
        "confirmed_order_replacement.proposal.moments[].duration_s",
        "confirmed_order_replacement.proposal.moments[].evidence_origin",
        "confirmed_order_replacement.proposal.moments[].evidence_ref",
        "confirmed_order_replacement.proposal.moments[].freeze_hold_s",
        "confirmed_order_replacement.proposal.moments[].gcs_path",
        "confirmed_order_replacement.proposal.moments[].generation",
        "confirmed_order_replacement.proposal.moments[].hold_duration_s",
        "confirmed_order_replacement.proposal.moments[].intent_id",
        "confirmed_order_replacement.proposal.moments[].kind",
        "confirmed_order_replacement.proposal.moments[].label",
        "confirmed_order_replacement.proposal.moments[].media_id",
        "confirmed_order_replacement.proposal.moments[].occurrence_id",
        "confirmed_order_replacement.proposal.moments[].output_end_s",
        "confirmed_order_replacement.proposal.moments[].output_start_s",
        "confirmed_order_replacement.proposal.moments[].phone_binding_digest",
        "confirmed_order_replacement.proposal.moments[].playback_rate",
        "confirmed_order_replacement.proposal.moments[].source_end_s",
        "confirmed_order_replacement.proposal.moments[].source_fence_digest",
        "confirmed_order_replacement.proposal.moments[].source_slot",
        "confirmed_order_replacement.proposal.moments[].source_start_s",
        "confirmed_order_replacement.proposal.predecessor_brief_version",
        "confirmed_order_replacement.proposal.predecessor_requirement_digest",
        "confirmed_order_replacement.proposal.predecessor_requirement_id",
        "confirmed_order_replacement.proposal.rejected_intent_id",
        "confirmed_order_replacement.proposal.rejection_category_key",
        "confirmed_order_replacement.proposal.rejection_question_id",
        "confirmed_order_replacement.proposal.rejection_selection_sequence",
        "confirmed_order_replacement.proposal.version",
        "confirmed_order_replacement.question_id",
        "confirmed_order_replacement.question_input_digest",
        "confirmed_order_replacement.question_sequence",
        "confirmed_order_replacement.selection_sequence",
        "confirmed_order_replacement.version",
        note="Server-confirmed sequence; receipts require the current complete compiled artifact.",
    ),
    **_rules(
        "upstream_resolved",
        "clip_selection_reconciliation",
        "selection_reconciliation",
        "selection_reconciliation.rejected_selections[]",
        "selection_reconciliation.rejected_selections[].category_key",
        "selection_reconciliation.rejected_selections[].intent",
        "selection_reconciliation.rejected_selections[].intent.attribute",
        "selection_reconciliation.rejected_selections[].intent.by_length",
        "selection_reconciliation.rejected_selections[].intent.caption_attribute",
        "selection_reconciliation.rejected_selections[].intent.creator_text",
        "selection_reconciliation.rejected_selections[].intent.exclusive",
        "selection_reconciliation.rejected_selections[].intent.intent_id",
        "selection_reconciliation.rejected_selections[].intent.label_source",
        "selection_reconciliation.rejected_selections[].intent.op",
        "selection_reconciliation.rejected_selections[].intent.order_by",
        "selection_reconciliation.rejected_selections[].intent.ordered_members",
        "selection_reconciliation.rejected_selections[].intent.part_attribute",
        "selection_reconciliation.rejected_selections[].intent.part_edge",
        "selection_reconciliation.rejected_selections[].intent.part_s",
        "selection_reconciliation.rejected_selections[].intent.placeholder",
        "selection_reconciliation.rejected_selections[].intent.position",
        "selection_reconciliation.rejected_selections[].intent.requirement_id",
        "selection_reconciliation.rejected_selections[].intent.source_kind",
        "selection_reconciliation.rejected_selections[].intent.transcript_kind",
        "selection_reconciliation.rejected_selections[].intent.under_s",
        "selection_reconciliation.rejected_selections[].question_id",
        "selection_reconciliation.rejected_selections[].selection_sequence",
        "selection_reconciliation.status",
        "selection_reconciliation.story_refs",
        "selection_reconciliation.story_refs.inventory_revision",
        "selection_reconciliation.story_refs.rows[]",
        "selection_reconciliation.story_refs.rows[].intent_ids[]",
        "selection_reconciliation.story_refs.rows[].kind",
        "selection_reconciliation.story_refs.rows[].row_index",
        "selection_reconciliation.story_refs.story_revision",
        "selection_reconciliation.unresolved_story_rows[]",
        "selection_reconciliation.version",
        note="Server-owned omitted moments and quarantined story prose; never visual proof.",
    ),
    **_rules(
        "supported",
        "render_contract:order",
        "editorial_plan.beats[].media_ids[]",
        note="The approved editorial sequence is checked against the rendered picture order.",
    ),
    **_rules(
        "upstream_resolved",
        "creation_text_composition",
        "editorial_plan",
        "editorial_plan.viewer_goal",
        "editorial_plan.beats[]",
        "editorial_plan.beats[].purpose",
        "editorial_plan.beats[].text_goal",
        note="Writing purposes accompany footage windows; compiled text is pinned for replay.",
    ),
    **_rules(
        "upstream_resolved",
        "pace_policy:provenance",
        "pacing_requested",
        note="server-owned: the creator's own words named a pace (cut-length pace unit)",
    ),
    # Hard requirements projected by build_render_contract.
    **_rules(
        "upstream_resolved",
        "duration_policy:proposal",
        "target_duration_s",
        note="model planning value; cannot establish creator duration provenance",
    ),
    **_rules(
        "upstream_resolved",
        "duration_policy:provenance",
        "target_duration_requested",
        note="legacy marker; never sufficient proof on its own",
    ),
    **_rules(
        "supported",
        "render_contract:duration",
        "creator_duration_s",
        note="server-derived creator target from text, approved brief, or authoritative answer",
    ),
    **_rules(
        "upstream_resolved",
        "length_planning:approved_music",
        "selected_music_section",
        note="server-frozen catalog section in the approved generation; consumed by both compilers",
    ),
    **_rules(
        "supported",
        "render_contract:audio",
        "audio_strategy",
        note=(
            "only `voiceover` (require_voiceover) and `original_audio` (original_audio=require) "
            "are projected; licensed_music and the other values pin nothing"
        ),
    ),
    **_rules(
        "supported",
        "render_contract:audio",
        "montage_audio",
        "montage_audio.preserve_source_audio",
        "montage_audio.source_media_ids[]",
        note="source ids are pinned only while preserve_source_audio is true",
    ),
    **_rules(
        "supported",
        "render_contract:text",
        "opening_title",
        "opening_title_duration_s",
        "shot_labels[]",
        "closing_title",
    ),
    **_rules(
        "supported",
        "render_contract:text",
        "pinned_texts[]",
        "pinned_texts[].text",
        "pinned_texts[].corner",
        note=(
            "KRI-523: the pinned text itself is verified as exact burned text; its corner is "
            "placed by the compiler, not separately re-measured by the verifier"
        ),
    ),
    **_rules(
        "upstream_resolved",
        "render_contract:text",
        "pinned_texts[].heading_level",
        "pinned_texts[].stack_id",
        "pinned_texts[].size_px",
        note=(
            "KRI-669: the pinned-text compiler resolves hierarchy, grouping and explicit size "
            "into measured rows; brief checks verify their size and spatial relationships"
        ),
    ),
    **_rules(
        "upstream_resolved",
        "render_contract:text",
        "pinned_texts[].start_s",
        "pinned_texts[].end_s",
        "pinned_texts[].clip",
        note=(
            "KRI-525: the compiler resolves a pin's seconds / clip scope to a window; the "
            "contract still verifies the pin's exact text, not when it is on screen"
        ),
    ),
    **_rules(
        "supported",
        "render_contract:order",
        "ordering_choice",
        note="only `chronological` pins order; `group_first` is resolved upstream, not projected",
    ),
    **_rules(
        "upstream_resolved",
        "render_contract:composition",
        "voice_mode",
        note=(
            "KRI-479: `continuous` = one named camera-audio clip's voice plays under the whole "
            "edit and its own picture is hidden. Not part of the pinned projection (the strict "
            "contract model never grows a field): the route resolver reads it and dispatch "
            "derives the composition commitments (sibling `creator_composition` key) the "
            "verifier checks; absent/`excerpts` = the spoken-excerpt lane unchanged"
        ),
    ),
    # Taste: no renderer is held to it.
    **_rules(
        "preference_only",
        "creator_capabilities",
        "direction",
        "intro_hook",
        "pacing",
        "rationale",
        "story_structure[]",
        "caption_style",
        "optional_treatments[]",
        "image_layout",
        "montage_audio.preview_source_beds",
    ),
    **_rules(
        "preference_only",
        "guided_story",
        "mixed_media_timing",
        *(
            f"mixed_media_timing.{leaf}"
            for leaf in (
                "image_hold",
                "image_hold_s",
                "video_hold",
                "boundary_style",
                "image_grouping",
                "sequence_grouping",
                "sequence_group_order[]",
            )
        ),
    ),
    # Resolved or repaired upstream of the contract.
    **_rules(
        "upstream_resolved",
        "creative_copy_decisions/unified_montage",
        "omitted_copy_targets[]",
        note=(
            "server-folded cancellation clears title requirements and suppresses title "
            "fallbacks; the contract does not independently verify absence of text"
        ),
    ),
    **_rules(
        "upstream_resolved",
        "creator_capabilities",
        "edit_format",
        "archetype",
        "hero_media_id",
        "execution_contract",
        "media_scope",
        "render_program",
        note="routing/shape fields repaired by compile_strategy_to_plan; never re-verified",
    ),
    **_rules(
        "upstream_resolved",
        "creator_capabilities",
        "selected_media_ids[]",
        note=(
            "scopes the clip set whose capture times pin order_ids; that only the selected "
            "media were used is not verified"
        ),
    ),
    **_rules(
        "upstream_resolved",
        "generative_build",
        "overlay_display",
        note=(
            'flat all_candidates["overlay_display"]="fullscreen" is read by the phone worker, '
            "gated by the media_overlays:fullscreen capability; the result is not verified"
        ),
    ),
    **_rules(
        "upstream_resolved",
        "generative_build",
        "font_family",
        "text_color",
        note="applied as typed overrides at render time; typography is not verified",
    ),
    **_rules(
        "upstream_resolved",
        "unified_montage",
        "title_animation",
        "label_position",
        note=(
            "KRI-522: the montage planner reads them (strategy, else the brief's text facts) "
            "into the snapshot; guided_story draws the entrance and the label corner. The "
            "brief receipts judge them against the plan record"
        ),
    ),
    **_rules(
        "upstream_resolved",
        "edit_proposal_planner",
        "video_reuse_policy",
        "montage_cadence",
        "montage_cadence.mode",
        "montage_cadence.source_media_ids[]",
        "montage_cadence.cut_duration_s",
        "montage_cadence.reuse_policy",
        note="enforced while planning; the contract does not re-verify cadence or reuse",
    ),
    **_rules(
        "supported",
        "creator_render_contract.hidden_parts",
        "resolved_clip_intents[].part_edge",
        "resolved_clip_intents[].part_s",
        note="Leverkusen M2: pinned as hidden_parts; the phone recipe check refuses any clip "
        "window inside the part and every cloud adapter declines it.",
    ),
    **_rules(
        "upstream_resolved",
        "clip_intent_resolver",
        "clip_intents[]",
        *(f"clip_intents[].{leaf}" for leaf in _CLIP_INTENT_LEAVES),
        # Leverkusen M2: the planner's raw hidden part, settled by the resolver and the turn.
        "clip_intents[].part_edge",
        "clip_intents[].part_s",
        "clip_intents[].part_attribute",
        "resolved_clip_intents[]",
        *(f"resolved_clip_intents[].{leaf}" for leaf in _CLIP_INTENT_LEAVES),
        # Only worded into the question asking for the seconds; never pinned.
        "resolved_clip_intents[].part_attribute",
        "resolved_clip_intents[].status",
        "resolved_clip_intents[].assignments[]",
        "resolved_clip_intents[].assignments[].media_id",
        "resolved_clip_intents[].assignments[].value",
        "resolved_clip_intents[].assignments[].evidence",
        "resolved_clip_intents[].assignments[].confidence",
        "resolved_clip_intents[].assignments[].grounding",
        "resolved_clip_intents[].question",
        "resolved_clip_intents[].caption_text",
        "resolved_clip_intents[].caption_grounding",
        note=(
            "server-resolved per-clip answers; labels reach the render via the plan. The "
            "`order` intents with a first/last position are also read by build_render_contract "
            "(KRI-503): they seat the described clips ahead of / behind the basis order "
            "in order_ids, the same rule the montage planner lays out. With no basis order "
            "(KRI-510: 'end on the sip') a placed one only lifts the unresolved order rule; "
            "the brief receipt proves where the described clips landed"
        ),
    ),
    **_rules(
        "upstream_resolved",
        "reaction_beats_grounding",
        "reaction_beats[]",
        *(
            f"reaction_beats[].{leaf}"
            for leaf in (
                "beat_id",
                "trigger",
                "after",
                "occurrence",
                "visual_id",
                "visual_role",
                "display_mode",
                "following_clip_id",
                "sound",
                "hold_s",
            )
        ),
        "closing_media",
        "closing_media.visual_id",
        "closing_media.badge_visual_id",
        "closing_media.from_trigger",
        note="grounded against the real transcript by the server; placement is not verified",
    ),
    **_rules(
        "upstream_resolved",
        "timed_cards_grounding",
        "timed_cards[]",
        *(
            f"timed_cards[].{leaf}"
            for leaf in (
                "card_id",
                "visual_id",
                "label",
                "at_s",
                "clip_id",
                "offset_s",
                "hold_s",
            )
        ),
        note=(
            "exact-timed small cards (flag PHONE_TIMED_CARDS_ENABLED): kept or dropped by "
            "creator_capabilities.repair_creator_timed_cards, grounded onto the compiled output "
            "clock by the phone writers (app.pipeline.phone_timed_cards) and recorded on "
            "variant['phone_timed_card_receipt'], which the brief receipts judge. The contract "
            "itself does not verify placement"
        ),
    ),
    **_rules(
        "upstream_resolved",
        "ending_clip_repair",
        "ending_clip",
        "ending_clip.media_id",
        "ending_clip.keep_audio",
        note=(
            "KRI-841: kept or dropped by creator_capabilities.repair_creator_ending_clip; the "
            "phone Talking worker appends it and records variant['phone_ending_clip'], which "
            "the brief receipts judge. The contract itself does not verify it"
        ),
    ),
    **_rules(
        "upstream_resolved",
        "speaker_background_repair",
        "background_media",
        "background_media.media_id",
        "background_media.source_start_s",
        note=(
            "KRI-1117: kept, filled from the creator's camera pick, or dropped by "
            "creator_capabilities.repair_creator_speaker_background; the phone Talking worker "
            "resolves it into the lane request's background and records the drop in "
            "phone_lane_receipt. The contract itself does not verify it"
        ),
    ),
    **_rules(
        "upstream_resolved",
        "choice_questions",
        "choice_answers[]",
        *(
            f"choice_answers[].{leaf}"
            for leaf in (
                "conflict",
                "kind",
                "option",
                "input_digest",
                "requirement_ids[]",
                "source",
                "value",
                "detail",
            )
        ),
        note=(
            "server-owned creator decisions on material conflicts; the planner gate writes "
            "them, build_render_contract consumes them (order basis, shot placement) and the "
            "chosen duration/clip subset is already rewritten into target_duration_s / "
            "selected_media_ids; an unclear-word answer's detail (KRI-849) is copied onto the "
            "approved brief, which the phone Talking caption writer reads"
        ),
    ),
    **_rules(
        "upstream_resolved",
        "user_song_planner",
        "song_sync",
        "resolved_song_takes[]",
        "song_captions",
    ),
    # Named required treatment with no evidence anywhere.
    **_rules(
        "unsupported",
        "none",
        "licensed_sfx",
        "licensed_sfx.effect_id",
        "licensed_sfx.semantics",
        "licensed_sfx.max_placements",
        note="a named licensed effect is required but no verifier proves its placement",
    ),
}


def field_matrix_for_strategy(strategy: Mapping[str, Any]) -> dict[str, FieldRule]:
    unused = set()
    if not strategy.get("confirmed_order_replacement"):
        unused.add("confirmed_order_replacement")
    if not strategy.get("selection_reconciliation"):
        unused.add("selection_reconciliation")
    if not any(
        isinstance(intent, Mapping) and intent.get("source_kind") is not None
        for key in ("clip_intents", "resolved_clip_intents")
        for intent in strategy.get(key) or ()
    ):
        unused.update({"clip_intents[].source_kind", "resolved_clip_intents[].source_kind"})
    return {
        path: rule
        for path, rule in FIELD_MATRIX.items()
        if path not in unused and not any(path.startswith(prefix + ".") for prefix in unused)
    }


# --- Adapter declarations ----------------------------------------------------
#
# What each render path does with each contract requirement: it `consumes` it
# (routes from it and the verifier checks it) or `declines` it with a typed
# reason.  Declarations are grounded in the dispatcher in
# tasks/generative_build.py, the speech-montage job, and the cloud contract.

CONTRACT_REQUIREMENTS: tuple[str, ...] = (
    "duration_s",
    "require_voiceover",
    "audio_source_ids",
    "original_audio",
    "exact_texts",
    "order_required",
    # Leverkusen M2: the parts of clips the creator asked never to show.
    "hidden_parts",
    "unresolved",
)

# The matrix path a requirement is about.  ``None``: refined per item at raise
# time (exact text -> opening_title / closing_title / shot_labels[]) or not
# attributable to one strategy field (unresolved).
REQUIREMENT_FIELD_PATHS: dict[str, str | None] = {
    "duration_s": "target_duration_s",
    "require_voiceover": "audio_strategy",
    "audio_source_ids": "montage_audio.source_media_ids[]",
    "original_audio": "montage_audio.preserve_source_audio",
    "exact_texts": None,
    "order_required": "ordering_choice",
    "hidden_parts": "resolved_clip_intents[].part_s",
    "unresolved": None,
}

BRIEF_TEXT_FIELD_PATH = "brief:text"


def text_field_path(requirement: TextRequirement) -> str:
    """The matrix path an exact-text requirement came from."""

    return {
        "opening": "opening_title",
        "closing": "closing_title",
        "clip": "shot_labels[]",
    }.get(requirement.role, BRIEF_TEXT_FIELD_PATH)


@dataclass(frozen=True)
class Decline:
    reason: DeclineReason
    alternative: str = ""


@dataclass(frozen=True)
class AdapterDeclaration:
    adapter: str
    consumes: frozenset[str]
    declines: Mapping[str, Decline]

    def __post_init__(self) -> None:
        accounted = set(self.consumes) | set(self.declines)
        if (
            accounted != set(CONTRACT_REQUIREMENTS)
            or set(self.consumes) & set(self.declines)
            or set(self.consumes) - set(CONTRACT_REQUIREMENTS)
        ):
            raise ValueError(
                f"{self.adapter}: every contract requirement must be consumed or declined once"
            )


_ASK_FOR_CHOICE = "Tell me which option you want and I'll continue."
_PHONE_REPAIR = "I can rebuild the edit so it shows this, or you can relax the requirement."
_ASK_PHONE = (
    "Ask for it to be rendered on your iPhone, where I can check it, "
    "or tell me to drop that requirement."
)

# What the pin-time phone verifier (`verify_phone_recipe`) does with each
# requirement for adapters that do not plan toward it: refuse to pin unless the
# compiled recipe demonstrates it.
PHONE_VERIFIER_DECLINES: dict[str, Decline] = {
    "duration_s": Decline("evidence_missing", _PHONE_REPAIR),
    "require_voiceover": Decline("evidence_missing", _PHONE_REPAIR),
    "audio_source_ids": Decline("evidence_missing", _PHONE_REPAIR),
    "original_audio": Decline("evidence_missing", _PHONE_REPAIR),
    "exact_texts": Decline("evidence_missing", _PHONE_REPAIR),
    "order_required": Decline("evidence_missing", _PHONE_REPAIR),
    # Never relaxed into showing the part: rebuilt without it, or the clip is left out.
    "hidden_parts": Decline(
        "evidence_missing",
        "I can rebuild the edit without that part of the clip, or leave the clip out.",
    ),
}
_UNRESOLVED_DECLINE = Decline("needs_choice", _ASK_FOR_CHOICE)
# The mix would play one source twice (a track clip and the separate music bed).
_SOUNDTRACK_TWICE = Decline(
    "requirement_conflict",
    "I can rebuild the edit with that soundtrack playing once, or you can pick a different one.",
)


def _phone(adapter: str, consumes: set[str], **overrides: Decline) -> AdapterDeclaration:
    declines = {
        requirement: overrides.get(requirement, decline)
        for requirement, decline in PHONE_VERIFIER_DECLINES.items()
        if requirement not in consumes
    }
    declines["unresolved"] = _UNRESOLVED_DECLINE
    return AdapterDeclaration(adapter, frozenset(consumes), declines)


_VOICE_CONFLICT = Decline(
    "requirement_conflict",
    "Choose either your recorded voice or the camera audio for this edit.",
)

ADAPTER_DECLARATIONS: dict[str, AdapterDeclaration] = {
    declaration.adapter: declaration
    for declaration in (
        # services/phone_speech_montage_job.py: the only adapter that routes from
        # the contract (voice ids, duration, order) and verifies its own recipe.
        _phone(
            "phone_speech_montage",
            {"duration_s", "audio_source_ids", "original_audio", "order_required"},
            require_voiceover=_VOICE_CONFLICT,
        ),
        # services/phone_speech_montage_job.py:run_phone_voice_behind_footage_job (KRI-479):
        # one clip's voice under the others, composed from the contract's own fields and
        # verified with the composition commitments.
        _phone(
            "phone_voice_behind_footage",
            {"duration_s", "audio_source_ids", "original_audio", "exact_texts", "order_required"},
            require_voiceover=_VOICE_CONFLICT,
        ),
        # _run_phone_unified_montage_job -> _run_phone_guided_job.
        # Keep the exact source-id requirement. The shared phone verifier
        # refuses a compiled recipe that cannot prove the required sources.
        # Leverkusen M2: its planner keeps every window out of a hidden part.
        _phone("phone_guided_unified_montage", {"hidden_parts"}),
        # _run_phone_voiceover_montage_job: routed to when the contract needs the voice.
        _phone(
            "phone_voiceover_montage",
            {"require_voiceover"},
            audio_source_ids=_VOICE_CONFLICT,
        ),
        # _run_phone_subtitled_job (also narrated formats without a recording).
        _phone("phone_subtitled", set()),
        # _run_phone_narrated_job.
        _phone(
            "phone_narrated",
            {"require_voiceover"},
        ),
    )
}


class TextRequirement(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)
    role: Literal["opening", "closing", "any", "clip"]
    text: str = Field(min_length=1, max_length=2000)
    media_id: str | None = None
    shot_index: int | None = Field(default=None, ge=0)
    duration_s: float | None = Field(default=None, gt=0)


class HiddenPartRequirement(BaseModel):
    """Leverkusen M2: ``seconds`` at the ``edge`` of clip ``media_id`` never play.

    ``media_id`` is the manifest id: an original's ``media_id`` or ``asset-<visual id>``."""

    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)
    media_id: str = Field(min_length=1, max_length=200)
    edge: Literal["start", "end"]
    seconds: float = Field(gt=0, le=MAX_HIDDEN_PART_S)


class CreatorRenderContract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)
    version: Literal[1] = 1
    generation_id: str = Field(min_length=1)
    strategy_digest: str | None = None
    brief_digest: str | None = None
    duration_s: float | None = Field(default=None, gt=0)
    audio_source_ids: tuple[str, ...] = ()
    original_audio: Literal["forbid", "require"] | None = None
    require_voiceover: bool = False
    exact_texts: tuple[TextRequirement, ...] = ()
    order_ids: tuple[str, ...] = ()
    order_required: bool = False
    order_basis: str | None = None
    unresolved: tuple[str, ...] = ()
    # Leverkusen M2. Omitted from dumps when empty, so every other contract (and its digest)
    # stays byte-identical and an older worker, which forbids unknown keys, keeps reading
    # it; one that carries a part is refused by that older worker (fails closed).
    hidden_parts: tuple[HiddenPartRequirement, ...] = Field(
        default=(), exclude_if=lambda value: not value
    )
    digest: str = ""

    def rebind(self, **changes: object) -> CreatorRenderContract:
        """Return an explicitly changed contract with a fresh integrity digest."""
        data = self.model_dump(mode="json") | changes
        data.pop("digest", None)
        validated = CreatorRenderContract(**data)
        return validated.model_copy(update={"digest": _digest(validated.model_dump(mode="json"))})


def _normal(value: object) -> str:
    # Line wrapping is layout, not a change to the approved words.
    return " ".join(unicodedata.normalize("NFC", str(value)).split())


def _label_intent_media_ids(resolved_intents: object, literal: str) -> tuple[str, ...]:
    """Clips a resolved ``label`` clip intent prints ``literal`` on, in listed order.

    The resolver stores the creator's words on each assignment (``value``); an older
    row may carry them only on the intent (``creator_text``). Either way the label lane
    burns exactly that text on exactly those clips, so they are the verifiable placement
    of a described-shot text.
    """
    wanted = _normal(literal)
    ids: list[str] = []
    for intent in resolved_intents if isinstance(resolved_intents, Sequence) else ():
        if not isinstance(intent, Mapping) or intent.get("op") != "label":
            continue
        if str(intent.get("status") or "resolved") != "resolved":
            continue
        for row in intent.get("assignments") or []:
            if not isinstance(row, Mapping):
                continue
            media_id = str(row.get("media_id") or "")
            value = row.get("value") or intent.get("creator_text")
            if media_id and value and _normal(value) == wanted and media_id not in ids:
                ids.append(media_id)
    return tuple(ids)


def _caption_intent_media_ids(resolved_intents: object, literal: str) -> tuple[str, ...]:
    """Clips a resolved described-shot caption prints the creator's ``literal`` on (KRI-804).

    "Put "chimichurri & salsa criolla" on the sauce bowls" may travel as a caption with the
    creator's words (``creator_text``), the lane that prints them as written however long
    (KRI-296); the montage burns it on exactly its member clips. A chapter caption with no
    creator words, or one the resolver wrote, is never this placement.
    """
    wanted = _normal(literal)
    ids: list[str] = []
    for intent in resolved_intents if isinstance(resolved_intents, Sequence) else ():
        if not isinstance(intent, Mapping) or intent.get("op") != "caption":
            continue
        if str(intent.get("status") or "resolved") != "resolved":
            continue
        # The montage prints exactly these rows (`unified_montage._described_shot_labels`).
        text = intent.get("caption_text")
        if (
            not intent.get("creator_text")
            or intent.get("caption_grounding") != "creator_text"
            or not isinstance(text, str)
            or _normal(text) != wanted
        ):
            continue
        for row in intent.get("assignments") or []:
            media_id = str(row.get("media_id") or "") if isinstance(row, Mapping) else ""
            if media_id and media_id not in ids:
                ids.append(media_id)
    return tuple(ids)


def _json_value(value: object) -> object:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if isinstance(value, tuple | list):
        return [_json_value(item) for item in value]
    if isinstance(value, dict):
        return {key: _json_value(item) for key, item in value.items()}
    return value


def _hash(value: object) -> str:
    return hashlib.sha256(
        json.dumps(
            _json_value(value), sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode()
    ).hexdigest()


# Fields added to ``CreatorRenderContract`` after v1 shipped, mapped to their
# JSON-mode default.  A stored v1 contract has no such key, so its digest was
# computed without it; skipping a field while it holds its default keeps every
# stored contract readable (``read_render_contract`` would otherwise reject
# in-flight jobs with "requirements changed").  A field is added here in the
# same change that adds it to the model, and never removed.
_POST_V1_FIELD_DEFAULTS: dict[str, Any] = {"hidden_parts": []}


def _digest(data: Mapping[str, Any]) -> str:
    return _hash(
        {
            key: value
            for key, value in data.items()
            if key != "digest"
            and not (key in _POST_V1_FIELD_DEFAULTS and value == _POST_V1_FIELD_DEFAULTS[key])
        }
    )


def _strategy(raw: Mapping[str, Any] | None) -> CreativeStrategy | None:
    if raw is None:
        return None
    try:
        return CreativeStrategy.model_validate(raw)
    except ValidationError as exc:
        raise CreatorRenderContractError(
            "I couldn't verify this edit's confirmed requirements."
        ) from exc


# The bases a described sequence can sit on (`order_basis` values, unchanged by KRI-503).
_SEQUENCE_BASES = frozenset({"capture_time", "attachment_order"})
_SEQUENCE_UNRESOLVED = (
    "I couldn't tell which clips you want first or last, so I can't confirm their order."
)


def _pin_not_found(words: str) -> str:
    """A stated first / last clip the server could not place, in the chat's language."""
    return say(
        en=f"I couldn't tell which clip \"{words}\" means, so I can't confirm the order.",
        tr=(
            f'"{words}" derken hangi klibi kastettiğini anlayamadım, '
            "o yüzden sırayı doğrulayamıyorum."
        ),
    )


def text_clip_not_found(literal: str) -> str:
    """Creator text whose clip the server could not tell, in the chat's language."""
    return say(
        en=f"I couldn't tell which clip \"{literal}\" goes on, so I can't confirm that text.",
        tr=(
            f'"{literal}" yazısının hangi klibe gideceğini anlayamadım, o yüzden bu yazıyı '
            "doğrulayamıyorum."
        ),
    )


# KRI-1069: the fixed words after the named media in ``capture_time_missing``, in both
# languages, so a refusal over it still names the order choice (``unresolved_field_path``).
_NO_CAPTURE_TAILS = {
    "en": (
        "has no filming time, so I can't put it in the order you filmed.",
        "have no filming time, so I can't put them in the order you filmed.",
    ),
    "tr": (
        "için çekim saati yok, o yüzden onu çektiğin sıraya koyamıyorum.",
        "için çekim saati yok, o yüzden onları çektiğin sıraya koyamıyorum.",
    ),
}


def capture_time_missing(names: Sequence[tuple[str, str]], *, whole_edit: bool = False) -> str:
    """Media of a filming-order edit with no capture time, named, in the chat's language.

    ``names`` are ``(en, tr)`` pairs (a file name is the same in both). ``whole_edit``: no
    clip or photo of the edit has a time, so naming each is noise and the line says so.
    KRI-1069: the contract once said "every selected clip" when one selected photo was not
    a clip at all.
    """
    shown = list(names[:3])
    more = len(names) - len(shown)
    one = len(names) == 1 and not whole_edit
    en = ", ".join(name for name, _tr in shown) + (f" and {more} more" if more else "")
    tr = ", ".join(name for _en, name in shown) + (f" ve {more} tane daha" if more else "")
    if whole_edit:
        en, tr = "Your clips and photos", "Kliplerinin ve fotoğraflarının hiçbiri"
    return say(
        en=f"{en} {_NO_CAPTURE_TAILS['en'][0 if one else 1]}",
        tr=f"{tr} {_NO_CAPTURE_TAILS['tr'][0 if one else 1]}",
    )


def _media_name(row: Mapping[str, Any], kind: str, ordinal: int) -> tuple[str, str]:
    """How the creator knows one medium: its file name as the app shows it, else its place."""
    name = clip_display_name(row.get("source_filename"))
    if name:
        return f'"{name}"', f'"{name}"'
    if kind == "photo":
        return f"photo {ordinal}", f"{ordinal}. fotoğraf"
    return f"clip {ordinal}", f"{ordinal}. klip"


def _edit_visuals(
    media_snapshot: Mapping[str, Any] | None,
    strategy: Mapping[str, Any],
    hidden_ids: frozenset[str] = frozenset(),
) -> list[Mapping[str, Any]]:
    """KRI-1069: the Visuals a phone montage shows beside the clips, as the snapshot lists them.

    Mirrors the unified montage worker: only the montage family without a recorded voiceover
    places Visuals (``planner._held_visual_pool``), narrowed to an explicit ``selected``
    scope (``unified_montage.selected_visual_ids``). A snapshot that lists no Visuals (one
    written before KRI-1069) gives none.
    """
    rows = (media_snapshot or {}).get(SNAPSHOT_VISUALS_KEY)
    if (
        not isinstance(rows, list)
        or strategy.get("edit_format") != "montage"
        or strategy.get("audio_strategy") == "voiceover"
    ):
        return []
    ids = strategy.get("selected_media_ids")
    selected = (
        {str(media_id) for media_id in ids}
        if strategy.get("media_scope") == "selected" and isinstance(ids, list) and ids
        else None
    )
    return [
        row
        for row in rows
        if isinstance(row, Mapping)
        and str(row.get("media_id") or "").startswith("asset-")
        and str(row["media_id"]) not in hidden_ids
        and (selected is None or str(row["media_id"]) in selected)
    ]


def _clip_intents_on() -> bool:
    """The planner seats a described sequence only when clip intents are on; so does the
    contract, or the two would disagree after a flag flip."""
    from app.config import settings  # noqa: PLC0415

    return bool(settings.clip_intents_enabled)


# --- Composition commitments (KRI-479) -------------------------------------------------
#
# What the plan commits to about HOW the tracks compose, beside (never inside) the strict
# contract model: older workers read stamped jobs with `extra="forbid"`, so a new contract
# field -- even one skipped while unset -- would make them unreadable during a rolling deploy
# or after a rollback. The commitments are a plain dict on the job
# (`assembly_plan["creator_composition"]`), keyed by the digest of the contract they were
# resolved against, written and read only by new code, and only for plan-authority jobs.

COMPOSITION_FIELD = "creator_composition"
ROUTE_VOICE_BEHIND_FOOTAGE = "voice_behind_footage"
# Mirrors `phone_recipe_shared.EXPORT_SAFETY_MARGIN_S` (kept literal: the contract module
# does not import pipeline compilers).
_VOICE_SAFETY_MARGIN_S = 0.05


class CompositionCommitments(BaseModel):
    """Plan commitments the verifier checks; none of them is a timeline."""

    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)
    # The voice clip's own picture is not part of the picture sequence (so it is left out of
    # `order_ids`; `audio_source_ids` keeps it).
    voice_picture: Literal["hidden"] | None = None
    # Seconds of voice the plan promises when that is LESS than the picture (a silent tail
    # the creator chose). None = the voice covers the whole timeline.
    voice_span_s: float | None = Field(default=None, gt=0)
    # Readable-shot floor in force. None = the policy constant (`MIN_READABLE_SHOT_S`).
    min_shot_s: float | None = Field(default=None, ge=0.4)


def commitments_from_strategy(
    strategy: Mapping[str, Any] | None, *, voice_duration_s: float | None = None
) -> CompositionCommitments | None:
    """The commitments a typed strategy implies, or None (no continuous voice).

    Only ``voice_mode == "continuous"`` with exactly ONE named camera-audio source
    commits anything; several candidates are a question (`which_voice`), not a guess.
    ``voice_duration_s`` is the voice clip's length (the silent-tail span needs it).
    """

    typed = _strategy(strategy)
    if typed is None or typed.voice_mode != "continuous":
        return None
    audio = typed.montage_audio
    ids = list(getattr(audio, "source_media_ids", None) or [])
    if audio is None or not audio.preserve_source_audio or len(ids) != 1:
        return None
    span: float | None = None
    answers = {a.conflict: a for a in (typed.choice_answers or ())}
    tail = answers.get(CONFLICT_VOICE_VS_DURATION)
    if tail is not None and tail.option == OPT_SILENT_TAIL and voice_duration_s:
        span = max(0.1, round(float(voice_duration_s) - _VOICE_SAFETY_MARGIN_S, 3))
    return CompositionCommitments(voice_picture="hidden", voice_span_s=span)


def _bound_duration_s(assembly: Mapping[str, Any], media_id: str) -> float | None:
    """The bound original's duration for ``media_id`` from the job's phone-source receipts."""

    from app.services.phone_sources import PHONE_SOURCES_FIELD  # noqa: PLC0415

    for row in assembly.get(PHONE_SOURCES_FIELD) or []:
        if isinstance(row, Mapping) and row.get("media_id") == media_id:
            original = row.get("original")
            value = original.get("duration_s") if isinstance(original, Mapping) else None
            if isinstance(value, int | float) and not isinstance(value, bool) and value > 0:
                return float(value)
    return None


def stamp_composition(
    assembly: Mapping[str, Any],
    candidates: Mapping[str, Any],
    *,
    voice_duration_s: float | None = None,
) -> dict[str, Any]:
    """``assembly`` with its ``creator_composition`` key (re)computed -- plan-authority jobs only.

    Call it once at dispatch, right after the route stamp (the stamped route is copied in so
    there is one source of truth). A job without the plan-authority stamp comes back as the
    SAME object; a strategy with no continuous voice REMOVES a previous key. Never raises.
    """

    unchanged = assembly if isinstance(assembly, dict) else dict(assembly)
    if candidates.get(PLAN_AUTHORITY_FIELD) is None:
        return unchanged
    try:
        contract = read_render_contract(assembly)
        strategy = candidates.get("creator_strategy")
        commitments = (
            commitments_from_strategy(strategy, voice_duration_s=voice_duration_s)
            if contract is not None
            else None
        )
        if commitments is not None and voice_duration_s is None:
            # The silent-tail span needs the voice clip's length, from the bound receipts.
            ids = (strategy.get("montage_audio") or {}).get("source_media_ids") or []
            known = _bound_duration_s(assembly, str(ids[0])) if ids else None
            if known is not None:
                commitments = commitments_from_strategy(strategy, voice_duration_s=known)
        if contract is None or commitments is None:
            if COMPOSITION_FIELD not in assembly:
                return unchanged
            return {k: v for k, v in assembly.items() if k != COMPOSITION_FIELD}
        stamp = assembly.get("creator_route")
        route = stamp.get("route") if isinstance(stamp, Mapping) else None
        return {
            **assembly,
            COMPOSITION_FIELD: {
                "contract_digest": contract.digest,
                "route": route if isinstance(route, str) else None,
                **commitments.model_dump(mode="json"),
            },
        }
    except Exception:  # noqa: BLE001 -- advisory; never block dispatch
        return unchanged


def read_composition(
    assembly: Mapping[str, Any], contract_digest: str
) -> CompositionCommitments | None:
    """The commitments, or None when absent, malformed or stale (another contract's)."""

    raw = assembly.get(COMPOSITION_FIELD)
    if not isinstance(raw, Mapping) or not contract_digest:
        return None
    if raw.get("contract_digest") != contract_digest:
        return None
    try:
        return CompositionCommitments.model_validate(
            {k: v for k, v in raw.items() if k not in {"contract_digest", "route"}}
        )
    except ValidationError:
        return None


def composition_route(assembly: Mapping[str, Any], contract_digest: str) -> str | None:
    """The route recorded with the commitments (None when absent or stale)."""

    raw = assembly.get(COMPOSITION_FIELD)
    if (
        isinstance(raw, Mapping)
        and contract_digest
        and raw.get("contract_digest") == contract_digest
        and isinstance(raw.get("route"), str)
    ):
        return raw["route"]
    return None


def _edit_rows(
    media_snapshot: Mapping[str, Any] | None,
    typed: CreativeStrategy | None,
    hidden_ids: frozenset[str],
) -> list[Any]:
    """The approved clip rows the edit shows: the selected media, minus hidden clips."""
    rows = (media_snapshot or {}).get("clip_assignments") or []
    selected = set(typed.selected_media_ids or ()) - hidden_ids if typed else set()
    if selected:
        rows = [row for row in rows if isinstance(row, Mapping) and row.get("media_id") in selected]
    if hidden_ids:
        rows = [
            row
            for row in rows
            if not (isinstance(row, Mapping) and str(row.get("media_id")) in hidden_ids)
        ]
    return list(rows)


def _one_unkeyed_clip(
    media_snapshot: Mapping[str, Any] | None,
    typed: CreativeStrategy | None,
    hidden_ids: frozenset[str],
) -> bool:
    """One selected clip whose snapshot row carries no ``media_id`` is still one clip.

    Prod 0701fcc9 / f88555bc (2026-10-21): a one-take Talking edit ("in the order the clips
    were added") whose only clip row had no ``media_id``. The selected-media filter in
    ``_edit_rows`` then left NO rows, so ``one_clip_edit`` said "not one clip" and the
    contract pinned "I can't verify this ordering rule" for a single take, refusing the
    approved render. A single clip is in every order (KRI-661 / KRI-789).
    """
    if typed is None:
        return False
    selected = set(typed.selected_media_ids or ()) - hidden_ids
    rows = [
        row
        for row in (media_snapshot or {}).get("clip_assignments") or []
        if isinstance(row, Mapping) and row.get("kind", "video") == "video"
    ]
    return len(selected) == 1 and len(rows) == 1 and not rows[0].get("media_id")


def build_render_contract(
    strategy: Mapping[str, Any] | None,
    *,
    generation_id: str,
    brief: CreativeBrief | None = None,
    media_snapshot: Mapping[str, Any] | None = None,
    clip_order: Sequence[str] = (),
    has_voiceover: bool = False,
    composition: CompositionCommitments | None = None,
    duplicate_aliases: Mapping[str, str] | None = None,
) -> CreatorRenderContract | None:
    """Pin only facts that a portable recipe can objectively demonstrate.

    ``composition`` (KRI-479) carries plan commitments that change how the order set is
    derived without touching the contract model: with ``voice_picture == "hidden"`` the
    camera-audio clip is the voice only, so it is left out of ``order_ids``
    (``audio_source_ids`` keeps it). ``None`` is byte-identical to before.

    ``duplicate_aliases`` (KRI-544) is dispatch's ``{dropped: kept}`` collapse of
    byte-identical uploads (``app.kria.duplicate_uploads``): a dropped copy is not in the
    edit, so it is left out of ``order_ids`` like a hidden voice clip, and the clip intents
    are read with the kept copy in its place. ``strategy_digest`` still hashes the strategy
    as approved. ``None``/empty is byte-identical to before.
    """
    if strategy is None and brief is None:
        return None
    digest_source = dict(strategy or {})
    if duplicate_aliases and strategy is not None:
        from app.kria.duplicate_uploads import collapse_strategy  # noqa: PLC0415

        strategy = collapse_strategy(strategy, duplicate_aliases)
    typed = _strategy(strategy)
    raw = dict(strategy or {})
    # KRI-476 (PR-C): the creator's recorded answers to material conflicts. They
    # resolve the matching requirement; without an answer the legacy behaviour is
    # unchanged (an unresolved item, never a guess).
    answers = {a.conflict: a for a in (typed.choice_answers or ())} if typed else {}
    order_answer = answers.get(CONFLICT_ORDER_BASIS)
    texts: list[TextRequirement] = []
    if typed is not None:
        if typed.opening_title:
            texts.append(
                TextRequirement(
                    role="opening",
                    text=typed.opening_title,
                    duration_s=typed.opening_title_duration_s,
                )
            )
        if typed.closing_title:
            texts.append(TextRequirement(role="closing", text=typed.closing_title))
        # KRI-523: whole-video corner text is exact text the burned layers must carry. The
        # phone/cloud verifiers match a pinned layer (`guided-pinned-*`) as role "any".
        for pin in typed.pinned_texts or ():
            texts.append(TextRequirement(role="any", text=pin.text))
        for index, text in enumerate(typed.shot_labels or ()):
            texts.append(TextRequirement(role="clip", text=text, shot_index=index))
    durations: list[float] = []
    # The Creator model must always emit ``target_duration_s`` (it picks a length from
    # the footage when the creator named none), and the server marks it
    # ``target_duration_requested`` either way, so the marker alone is NOT evidence of a
    # creator-requested length. When a brief exists, its LIVE timing requirements (below)
    # are the creator's own lengths; a strategy value only counts alongside one (so a
    # disagreement stays a conflict) or when the creator answered a duration-vs-count
    # choice (the answer rewrites it). A user-song item's
    # length is owned by the song window, never by a model-chosen target. Legacy
    # brief-less non-song strategies keep the marker behaviour unchanged.
    # A frozen brief/answered conflict is newer and more authoritative than a
    # strategy stamp that may have survived an earlier request.  Only without
    # that authority can the persisted server stamp prove a target.
    authoritative_duration_s = creator_duration_s(
        brief=brief,
        authoritative_answers=answers.values() if brief is not None else (),
    )
    stamped_duration = raw.get("creator_duration_s")
    proven_duration_s = authoritative_duration_s
    if (
        proven_duration_s is None
        and isinstance(stamped_duration, (int, float))
        and stamped_duration > 0
    ):
        proven_duration_s = float(stamped_duration)
    owner = length_owner(typed or raw, creator_duration_proven=proven_duration_s is not None)
    if owner == "creator" and proven_duration_s is not None:
        durations.append(proven_duration_s)
    order_required = bool(typed and typed.ordering_choice == "chronological")
    attachment_order = False
    hidden_ids: frozenset[str] = frozenset()
    if (
        composition is not None
        and composition.voice_picture == "hidden"
        and typed is not None
        and typed.montage_audio is not None
        and typed.montage_audio.preserve_source_audio
    ):
        hidden_ids = frozenset(str(v) for v in typed.montage_audio.source_media_ids or [])
    if duplicate_aliases:
        # KRI-544: a byte-identical copy dispatch left out is not in the picture either.
        hidden_ids = hidden_ids | frozenset(str(v) for v in duplicate_aliases)
    order_ids = tuple(
        str(item) for item in clip_order if str(item).strip() and str(item) not in hidden_ids
    )
    order_basis = "confirmed" if order_ids else None
    unresolved: list[str] = []
    # Leverkusen M2: parts of clips the creator asked never to show. `strategy` is already
    # collapsed onto the kept copy of a byte-identical upload. One nobody settled (no clip,
    # edge or seconds) is never rendered around.
    hidden_parts = tuple(
        HiddenPartRequirement(media_id=part.media_id, edge=part.edge, seconds=part.seconds)
        for part in hidden_parts_from_strategy(raw)
    )
    unresolved.extend(
        say(
            en=f"I still need to know which part of {name} to leave out.",
            tr=f"{name} içinde hangi bölümü göstermeyeceğimi hâlâ bilmem gerekiyor.",
        )
        for name in unsettled_hidden_parts(raw)
    )
    song_time_owns_order = bool(
        typed
        and typed.audio_strategy == "user_song"
        and typed.song_sync == "lipsync"
        and typed.resolved_song_takes
    )
    # The creator's described sequence ("start with X", "end on Y"), as the clip-intent
    # resolver matched it to clips; the montage planner seats exactly these rows.
    sequence = (
        sequence_rows(raw.get("resolved_clip_intents"))
        if brief is not None and _clip_intents_on()
        else []
    )
    clip_ids = {
        str(row["media_id"])
        for row in (media_snapshot or {}).get("clip_assignments") or []
        if isinstance(row, Mapping) and row.get("media_id")
    }
    sequence_placed = all(status == "resolved" for _p, _n, _m, status in sequence) and any(
        clip_ids.intersection(members) for _p, _n, members, _s in sequence
    )
    placed_positions = {
        position
        for position, _name, members, status in sequence
        if position and status == "resolved" and clip_ids.intersection(members)
    }
    talking_edit = str(raw.get("edit_format") or "") in {"subtitled", "talking_head"}
    # A word-timed text ask is omitted from the ordinary exact-copy pin only
    # when this approval can actually use the native narrated word-text lane.
    # Other typed voiceover strategies still need their literal pinned, so a
    # renderer cannot silently treat a caption or title as the requested cue.
    voiceover_spoken_text = False
    if (
        typed is not None
        and typed.audio_strategy == "voiceover"
        and typed.edit_format in NARRATED_EDIT_FORMATS
        and typed.render_program != "guided"
    ):
        from app.services.phone_rollout import (
            phone_voiceover_spoken_text_supported,  # noqa: PLC0415
        )

        voiceover_spoken_text = phone_voiceover_spoken_text_supported(require_client=False)
    edit_rows = _edit_rows(media_snapshot, typed, hidden_ids)
    one_clip = one_clip_edit(edit_rows) or _one_unkeyed_clip(media_snapshot, typed, hidden_ids)
    if brief:
        for requirement in brief.live():
            if (
                requirement.kind == "timing"
                and requirement.facts.get("duration_s") is not None
                # KRI-777: a freeze's or a moment's seconds are not the video's length.
                and not times_one_moment(requirement)
            ):
                if owner != "creator":
                    continue
                answered = answers.get(CONFLICT_DURATION_VS_COUNT) or answers.get(
                    CONFLICT_VOICE_VS_DURATION
                )
                if answered is not None and requirement.id in answered.requirement_ids:
                    # The creator chose a different length for exactly this requirement;
                    # the approved strategy carries it (target_duration_s, requested).
                    continue
                durations.append(float(requirement.facts["duration_s"]))
            # 29 Ekim: a `text_sequence` pins the same words as a `text` ask; the draft shows
            # them whole.
            if requirement.kind in {"text", "text_sequence"} and requirement.literal:
                if (talking_edit or voiceover_spoken_text) and (
                    requirement_spoken_text(requirement) is not None
                    or is_spoken_text_cue(requirement)
                ):
                    # KRI-707: a Talking edit shows text on a spoken word only where the
                    # take says it (`phone_spoken_text`). Its own receipt answers it ("not
                    # heard" / "can't"), so pinning it would refuse an honest render.
                    # KRI-791: so does one whose word the ask doesn't name ("which word?").
                    continue
                shot_index = None
                if requirement.is_shot_text:
                    labels = list(typed.shot_labels or ()) if typed else []
                    matching = [
                        index
                        for index, text in enumerate(labels)
                        if _normal(text) == _normal(requirement.literal)
                    ]
                    placed = answers.get(f"{CONFLICT_TEXT_PLACEMENT}:{requirement.id}")
                    chosen = (
                        int(placed.option.rsplit("_", 1)[-1]) - 1
                        if placed is not None
                        and placed.option.startswith("shot_")
                        and placed.option.rsplit("_", 1)[-1].isdigit()
                        else None
                    )
                    if len(matching) == 1:
                        shot_index = matching[0]
                    elif chosen is not None and chosen in matching:
                        shot_index = chosen
                    else:
                        # "Mutfak on the kitchen clip" normally travels as a resolved
                        # `label` clip intent (the resolver matched the words to a clip and
                        # the label lane burns them there), not as positional
                        # `shot_labels`. Pin those clips; refusing was a dead end (Tigre
                        # M1, 2026-10-10: every room label hit this with no planner miss).
                        placed_ids = _label_intent_media_ids(
                            raw.get("resolved_clip_intents"), requirement.literal
                        )
                        if placed_ids:
                            texts.extend(
                                TextRequirement(
                                    role="clip", text=requirement.literal, media_id=media_id
                                )
                                for media_id in placed_ids
                            )
                            continue
                        captioned = _caption_intent_media_ids(
                            raw.get("resolved_clip_intents"), requirement.literal
                        )
                        if captioned:
                            # A shared caption may cross neighboring member clips, but
                            # its source identities must survive into verification.
                            texts.extend(
                                TextRequirement(
                                    role="clip", text=requirement.literal, media_id=media_id
                                )
                                for media_id in captioned
                            )
                            continue
                        # Nothing here turns the words into a caption, so the reply offers
                        # no "keep as written" (the Talking formats never plan clip texts).
                        label_reply = label_text_reply([requirement.literal], keep=False)
                        if label_reply is not None:
                            # KRI-804: words a label can't print (a "+", too long) are a
                            # question about the TEXT, never "which clip".
                            unresolved.append(label_reply.question)
                            continue
                        unresolved.append(text_clip_not_found(requirement.literal))
                role: Literal["opening", "closing", "any", "clip"] = (
                    "clip"
                    if requirement.scope.startswith("clip:") or requirement.scope == "per_clip"
                    else "any"
                )
                texts.append(
                    TextRequirement(
                        role=role,
                        text=requirement.literal,
                        media_id=requirement.scope[5:]
                        if requirement.scope.startswith("clip:")
                        else None,
                        shot_index=shot_index,
                    )
                )
            if requirement.kind == "order":
                if song_time_owns_order and requirement.facts.get("key") not in {
                    "capture_time",
                    "chronological",
                }:
                    # The song-order answer ("Use this order: clips 1, 2, ..."): the
                    # server-resolved song placement IS the order authority and the
                    # render verifies it through the lip-sync receipts, so it is not
                    # an unverifiable media-order rule (and must never pin capture time).
                    continue
                if order_mode(requirement) is not None:
                    # A seeded shuffle is proved by the brief receipt (`_check_order`), and
                    # "any order" has nothing to pin: neither is an unverifiable rule.
                    continue
                key = requirement.facts.get("key")
                answered = order_answer is not None and (
                    requirement.id in order_answer.requirement_ids or key == ATTACHMENT_ORDER_KEY
                )
                if sequence_placed and key not in CAPTURE_ORDER_KEYS and not answered:
                    # KRI-510: "end on the sip by the window", with no filming or upload
                    # order asked for. The resolver matched the words to clips and the
                    # planner seats them; the question gate stays quiet about a placed
                    # rule (`choice_questions._placed_sequence`), so refusing it here was
                    # a dead end. With a basis order the seating is pinned in `order_ids`
                    # below (KRI-503). Without one there is no full order to pin and the
                    # strict model takes no new field (old workers must read every
                    # contract): the brief receipt proves where each described group
                    # landed (`brief_checks._check_order`), and a checked receipt that is
                    # not met blocks the unified montage before anything renders.
                    continue
                if (
                    one_clip
                    and not answered
                    and key not in CAPTURE_ORDER_KEYS
                    and not is_pin_only_order(requirement)
                ):
                    # KRI-789: "start at '…' and end right after '…'" inside the one take.
                    # A single clip is in any order, so there is nothing to pin or verify;
                    # the question gate stays quiet about it for the same reason
                    # (`choice_questions.one_clip_edit`). Filming order on one clip keeps its
                    # KRI-661 path below; first / last pins keep KRI-762's.
                    continue
                order_required = True
                if order_answer is not None and requirement.id in order_answer.requirement_ids:
                    continue  # the creator's answer decides how this order is met
                if key == ATTACHMENT_ORDER_KEY and order_answer is not None:
                    # Only a recorded creator answer makes "attachment" a verifiable
                    # basis; a bare brief key stays unresolved exactly as before.
                    attachment_order = True
                elif key not in CAPTURE_ORDER_KEYS:
                    # KRI-762: first / last clips the server could not place are named, not
                    # filed under "an ordering rule I can't verify" (the question gate no
                    # longer asks about them; the creator's next message names the clip).
                    missing_pins = (
                        {
                            position: words
                            for position, words in pin_anchors(requirement).items()
                            if position not in placed_positions
                        }
                        if is_pin_only_order(requirement)
                        else {}
                    )
                    if missing_pins:
                        unresolved.extend(_pin_not_found(words) for words in missing_pins.values())
                    else:
                        unresolved.append(
                            "I can't verify this ordering rule from the approved media."
                        )
    if order_answer is not None:
        if order_answer.option == OPT_UNORDERED:
            # The creator chose to drop the chronological promise: nothing to verify.
            order_required = False
        elif order_answer.option == OPT_ATTACHMENT_ORDER:
            order_required = True
            attachment_order = True
    if order_required and attachment_order:
        # An explicit, verifiable basis: the order the clips were added to the project.
        rows = edit_rows
        ids = [
            str(row["media_id"]) for row in rows if isinstance(row, Mapping) and row.get("media_id")
        ]
        if not ids or len(ids) != len(rows) or len(set(ids)) != len(ids):
            unresolved.append("I need to know which clips are in the edit to verify their order.")
        else:
            order_ids = tuple(ids)
            order_basis = "attachment_order"
    elif order_required:
        from app.services.clip_facts import capture_from_assignment, visual_capture_time

        rows = edit_rows
        selected = set(typed.selected_media_ids or ()) - hidden_ids if typed else set()
        # KRI-1069: the Visuals the montage shows sit in the chronology by their own capture
        # time, after the clips on a tie, exactly as the planner orders them.
        visuals = _edit_visuals(media_snapshot, raw, hidden_ids)
        dates = {}
        names: dict[str, tuple[str, str]] = {}
        for ordinal, row in enumerate(rows, start=1):
            if not isinstance(row, Mapping) or not row.get("media_id"):
                continue
            names[str(row["media_id"])] = _media_name(row, "clip", ordinal)
            capture = capture_from_assignment(row)
            if capture and capture.capture_time:
                dates[str(row["media_id"])] = capture.capture_time
        for ordinal, row in enumerate(visuals, start=1):
            names[str(row["media_id"])] = _media_name(row, "photo", ordinal)
            taken = visual_capture_time(row.get("capture"))
            if taken is not None:
                dates[str(row["media_id"])] = taken
        ids = [
            str(row["media_id"]) for row in rows if isinstance(row, Mapping) and row.get("media_id")
        ]
        shown = [*ids, *(str(row["media_id"]) for row in visuals)]
        # KRI-661: a single clip is trivially in order, dated or not (with no Visual beside it).
        single_clip = one_clip and not visuals
        undated = [media_id for media_id in shown if media_id not in dates]
        if single_clip and not ids:
            # The one clip has no snapshot id to pin (``_one_unkeyed_clip``): a single
            # take is already in filming order, so there is nothing left to verify.
            order_required = False
        elif (
            not ids
            or len(ids) != len(rows)
            or len(set(shown)) != len(shown)
            or (selected and set(shown) != selected)
        ):
            # Which media the edit shows is unclear (KRI-1069: a selected Visuals photo in a
            # format that orders none by time, or a snapshot that lists no Visuals): that
            # is not a missing capture time, so the reply never says it is.
            order_ids = ()
            unresolved.append("I need to know which clips are in the edit to verify their order.")
        elif undated and not single_clip:
            order_ids = ()
            if len(undated) == len(shown) and not visuals:
                # No clip has a time: this long-standing line is true, and its plain copy
                # (`kria_runtime._UNRESOLVED_PLAIN`) offers the order choice.
                unresolved.append(
                    "I need capture times for every selected clip to verify chronological order."
                )
            else:
                unresolved.append(
                    capture_time_missing(
                        [names[media_id] for media_id in undated],
                        whole_edit=len(undated) == len(shown) > 1,
                    )
                )
        else:
            order_ids = (
                tuple(ids)
                if single_clip
                else tuple(sorted(shown, key=lambda media_id: dates[media_id]))
            )
            order_basis = "capture_time"
    if (
        order_required
        and order_ids
        and order_basis in _SEQUENCE_BASES
        and brief is not None
        and _clip_intents_on()
    ):
        # KRI-503: "chronological order, starting with the blue video". The planner seats
        # the described clips on top of the basis order (`clip_order_sequence`); the contract
        # pins that same seating, so an edit that does what the creator said still verifies.
        # Without a sequence rule nothing changes: `order_ids` stays the pure basis order.
        rows = sequence
        # KRI-522: the brief remembers "starting with the blue video" even when this
        # turn's planner dropped it. A stated first/last clip with no seated group is
        # asked about, never silently left in plain filming order.
        seated = {position for position, _name, _members, _status in rows}
        unplaced = {
            position: words
            for position, words in stated_anchors(brief).items()
            if position not in seated
        }
        if unplaced:
            order_ids = ()
            order_basis = None
            unresolved.extend(
                message
                for message in map(_pin_not_found, unplaced.values())
                if message not in unresolved
            )
        elif any(status != "resolved" for _position, _name, _members, status in rows):
            order_ids = ()
            order_basis = None
            unresolved.extend(
                unresolved_questions(raw.get("resolved_clip_intents")) or [_SEQUENCE_UNRESOLVED]
            )
        elif rows:
            order_ids = tuple(apply_sequence(order_ids, rows))
    if typed is not None and typed.editorial_plan is not None:
        from app.services.editorial_plan import editorial_order

        assignments = (media_snapshot or {}).get("clip_assignments") or []
        selected = set(typed.selected_media_ids or ())
        available = [
            str(row["media_id"])
            for row in assignments
            if isinstance(row, Mapping)
            and row.get("media_id")
            and str(row["media_id"]) not in hidden_ids
            and (not selected or str(row["media_id"]) in selected)
        ]
        if media_snapshot is None:
            # Job factory projection precedes media binding. Dispatch validates the
            # complete set again against the approval's immutable media snapshot.
            available = [mid for beat in typed.editorial_plan.beats for mid in beat.media_ids]
        try:
            order_ids = tuple(
                editorial_order(
                    typed.editorial_plan,
                    available,
                    required_order=order_ids if order_required or clip_order else (),
                    sequence=sequence_rows(raw.get("resolved_clip_intents")),
                )
            )
        except ValueError as exc:
            raise CreatorRenderContractError(
                "The proposed story does not match the selected footage or confirmed order."
            ) from exc
        order_required = True
        order_basis = "approved_editorial_plan"
    if durations and any(abs(value - durations[0]) > 0.001 for value in durations[1:]):
        raise CreatorRenderContractError(
            "Your confirmed edit lengths conflict, so I can't render it safely.",
            decline_reason="requirement_conflict",
            field_path="target_duration_s",
            alternative="Tell me which length you want.",
        )
    source_ids: tuple[str, ...] = ()
    original_audio: Literal["forbid", "require"] | None = None
    if typed:
        has_voiceover = typed.audio_strategy == "voiceover"
    if typed and typed.audio_strategy == "original_audio":
        original_audio = "require"
    if typed and typed.montage_audio:
        ids = getattr(typed.montage_audio, "source_media_ids", None) or []
        source_ids = (
            tuple(str(value) for value in ids) if typed.montage_audio.preserve_source_audio else ()
        )
        original_audio = "require" if typed.montage_audio.preserve_source_audio else "forbid"
    data: dict[str, Any] = dict(
        version=1,
        generation_id=generation_id,
        strategy_digest=_hash(digest_source) if strategy is not None else None,
        brief_digest=_hash(brief.model_dump(mode="json")) if brief else None,
        duration_s=durations[0] if durations else None,
        audio_source_ids=source_ids,
        original_audio=original_audio,
        require_voiceover=bool(has_voiceover),
        exact_texts=tuple(texts),
        order_ids=order_ids,
        order_required=order_required,
        order_basis=order_basis,
        unresolved=tuple(unresolved),
    )
    if hidden_parts:
        data["hidden_parts"] = hidden_parts
    return CreatorRenderContract(**data).rebind()


def contract_duration_bound(
    contract: CreatorRenderContract | None,
    strategy: Mapping[str, Any] | None,
    brief: CreativeBrief | None,
) -> DurationBound:
    """Whether the contract's ``duration_s`` is the brief's ceiling ("at most 10 seconds").

    KRI-847. The contract model is frozen (an older worker forbids extra keys), so the bound
    cannot ride on it; the length-policy state carries it (`stamp_length_policy`). This reads
    the same brief length `build_render_contract` pinned, and is a ceiling only when the
    contract's number is that length.
    """
    if contract is None or contract.duration_s is None or brief is None:
        return "exact"
    typed = _strategy(strategy)
    answers = {a.conflict: a for a in (typed.choice_answers or ())} if typed else {}
    found = creator_duration(brief=brief, authoritative_answers=answers.values())
    if found is None or found.bound != "at_most":
        return "exact"
    return "at_most" if abs(found.seconds - contract.duration_s) <= 0.001 else "exact"


def read_render_contract(assembly: Mapping[str, Any]) -> CreatorRenderContract | None:
    if CONTRACT_FIELD not in assembly:
        return None
    raw = assembly[CONTRACT_FIELD]
    try:
        contract = CreatorRenderContract.model_validate(raw)
    except ValidationError as exc:
        raise CreatorRenderContractError(
            "I couldn't read this edit's confirmed requirements."
        ) from exc
    if contract.digest != _digest(contract.model_dump(mode="json")):
        raise CreatorRenderContractError(
            "This edit's confirmed requirements changed; please try again."
        )
    return contract


def _phone_decline(
    requirement: str,
    message: str,
    *,
    field_path: str | None = None,
    missing_text: str | None = None,
) -> CreatorRenderContractError:
    """A typed refusal for ``requirement`` using the declared phone-verifier reason."""

    decline = PHONE_VERIFIER_DECLINES[requirement]
    return CreatorRenderContractError(
        message,
        decline_reason=decline.reason,
        field_path=field_path or REQUIREMENT_FIELD_PATHS[requirement],
        alternative=decline.alternative or None,
        missing_text=missing_text,
    )


def _placement_decline(
    requirement: TextRequirement, message: str, *, field_path: str
) -> CreatorRenderContractError:
    """The ``exact_texts`` refusal for a text whose words are present but misplaced."""

    typed = _phone_decline("exact_texts", message, field_path=field_path)
    if requirement.role == "any":
        return typed  # nothing narrower to relax: a plain presence/hold failure
    return TextPlacementError(
        message,
        requirement=requirement,
        decline_reason=typed.decline_reason,
        field_path=typed.field_path,
        alternative=typed.alternative,
    )


def _audio_decline(requirement: str, message: str) -> CreatorRenderContractError:
    """A camera-sound refusal that remembers which contract field it came from."""

    typed = _phone_decline(requirement, message)
    return AudioChoiceError(
        message,
        field=requirement,
        decline_reason=typed.decline_reason,
        field_path=typed.field_path,
        alternative=typed.alternative,
    )


# KRI-794: the unresolved lines an order-basis answer settles ("Use the order you added the
# clips" / "Continue without a fixed order"). A refusal over one of these names the order
# choice (``field_path``), so the next reply offers that choice again instead of drafting
# the same refused edit. First / last clip and described-sequence lines are clip questions
# (KRI-762), not this choice. Each line is written verbatim in ``build_render_contract``.
ORDER_BASIS_UNRESOLVED = frozenset(
    {
        "I can't verify this ordering rule from the approved media.",
        "I need to know which clips are in the edit to verify their order.",
        "I need capture times for every selected clip to verify chronological order.",
    }
)


def unresolved_field_path(message: str) -> str | None:
    """The strategy field an unresolved line's refusal names, or ``None``."""

    if message in ORDER_BASIS_UNRESOLVED or message.endswith(
        (*_NO_CAPTURE_TAILS["en"], *_NO_CAPTURE_TAILS["tr"])
    ):
        return REQUIREMENT_FIELD_PATHS["order_required"]
    return None


def unresolved_decline(message: str) -> CreatorRenderContractError:
    """The typed refusal for an unresolved approved requirement (needs a choice)."""

    return CreatorRenderContractError(
        message,
        decline_reason=_UNRESOLVED_DECLINE.reason,
        field_path=unresolved_field_path(message),
        alternative=_UNRESOLVED_DECLINE.alternative,
    )


def check_phone_dispatch_contract(
    contract: CreatorRenderContract,
    *,
    snapshot_generation_id: object,
    has_voiceover_candidate: bool,
    user_song: object = None,
    allow_voiceover_source_audio: bool = False,
) -> bool:
    """Gate a phone dispatch on the pinned contract; return the voiceover route.

    Raised declines are typed so the reason survives the worker's
    ``phone_plan_unsupported`` collapse.  A recording's mere presence never
    overrides the approved soundtrack: the returned flag is the contract's.
    """

    if contract.generation_id != snapshot_generation_id:
        raise CreatorRenderContractError(
            "This edit belongs to a different approved revision.",
            decline_reason="requirement_conflict",
            alternative="Ask me to make the edit again from your latest request.",
        )
    if contract.unresolved:
        raise unresolved_decline(contract.unresolved[0])
    if contract.require_voiceover and not has_voiceover_candidate:
        raise CreatorRenderContractError(
            "This edit needs your confirmed recorded voice.",
            decline_reason="needs_choice",
            field_path=REQUIREMENT_FIELD_PATHS["require_voiceover"],
            alternative="Record or upload your voice, or tell me to use music instead.",
        )
    if contract.audio_source_ids and (
        user_song or (contract.require_voiceover and not allow_voiceover_source_audio)
    ):
        raise CreatorRenderContractError(
            "This renderer can't combine the confirmed soundtracks.",
            decline_reason="requirement_conflict",
            field_path=REQUIREMENT_FIELD_PATHS["audio_source_ids"],
            alternative="Choose one soundtrack: the camera audio, your voice, or your song.",
        )
    return contract.require_voiceover


def stamped_plan_contract(
    assembly: Mapping[str, Any], candidates: Mapping[str, Any] | None
) -> CreatorRenderContract | None:
    """The pinned contract of a plan-authority job; ``None`` for every legacy job.

    KRI-470 PR-F gate.  A job is plan-authority only when it carries the dispatch-time
    ``PLAN_AUTHORITY_FIELD`` stamp (workers never read the live flag) AND a pinned
    contract.  Every retired heuristic override is guarded by this one function, so an
    unstamped job (or a stamped one with no contract to follow) keeps the legacy branch
    byte for byte.
    """

    if not candidates or candidates.get(PLAN_AUTHORITY_FIELD) is None:
        return None
    return read_render_contract(assembly)


def camera_audio_requires_speech(has_audio_sources: bool, voice_mode: str | None) -> bool:
    """Named camera sound only needs spoken excerpts when that is its delivery mode."""
    return has_audio_sources and voice_mode != "per_clip"


def job_requires_camera_speech(
    contract: CreatorRenderContract | None, candidates: Mapping[str, Any]
) -> bool:
    """Read the approved audio mode without introducing a renderer/shadow dependency.

    Only plan-authority jobs may opt into per-clip sound. An absent mode or
    an unstamped job keeps its historical spoken-excerpt interpretation.
    """
    strategy = candidates.get("creator_strategy")
    mode = (
        strategy.get("voice_mode")
        if candidates.get(PLAN_AUTHORITY_FIELD) is not None and isinstance(strategy, Mapping)
        else None
    )
    return camera_audio_requires_speech(
        bool(contract and contract.audio_source_ids), mode if isinstance(mode, str) else None
    )


def plan_voiceover_path(contract: CreatorRenderContract | None, attached: str | None) -> str | None:
    """The recorded voice that may decide a route (KRI-470 PR-F).

    Legacy: the mere presence of an attached file.  Plan-authority: the approved
    contract -- a stray attached recording does not change the route, and a required
    voice with no attached file stays ``None`` so the caller can ask for it.
    """

    if contract is None:
        return attached or None
    return (attached or None) if contract.require_voiceover else None


def speech_edit_not_built() -> CreatorRenderContractError:
    """The speech adapter declined a contract that requires camera-audio sources."""

    return CreatorRenderContractError(
        "I couldn't build the confirmed camera-audio edit.",
        decline_reason="capability_unavailable",
        field_path=REQUIREMENT_FIELD_PATHS["audio_source_ids"],
        alternative="Pick a different clip to carry the speech, or ask for a plain montage.",
    )


def _clip_audible(recipe: EditRecipeV2, track, clip) -> bool:  # noqa: ANN001
    """True when this track clip reaches the mix (gain, original volume, mute windows)."""

    if clip.volume <= 0 or (track.kind == "video" and recipe.audio.original_volume <= 0):
        return False
    # Mute windows use timeline time. Covering only part of a clip cannot
    # prove silence; adjacent windows can together cover the whole clip.
    start = clip.timeline_start
    end = start + clip.source_duration / clip.rate
    for window in sorted(recipe.audio.mute_windows, key=lambda item: item.start):
        if clip.id not in window.clip_ids or window.end <= start:
            continue
        if window.start > start:
            return True
        start = max(start, window.end)
        if start >= end:
            return False
    return start < end


def music_bed_audible(recipe: EditRecipeV2) -> str | None:
    """The asset id of the separate music bed when it reaches the mix, else None.

    The device plays ``audio.music_asset_id`` as its OWN bed (from source 0, at
    ``music_volume``) in addition to every audio-track clip. A track clip is not
    the only way a source becomes audible.
    """

    asset_id = recipe.audio.music_asset_id
    if asset_id is None or recipe.audio.music_volume <= 0 or recipe.duration <= 0:
        return None
    return asset_id


def doubled_soundtrack_assets(recipe: EditRecipeV2) -> list[str]:
    """Assets the mix plays twice: audible through a track clip AND the music bed.

    KRI-481 (prod job 934811f3): the song lane put the creator's song on a track
    clip at the chosen window and left the bed at its default volume, so the
    device also played it from source 0. Neither copy is "wrong" alone, which is
    why a per-clip check could not see it.
    """

    bed = music_bed_audible(recipe)
    if bed is None:
        return []
    for track in recipe.tracks:
        if track.kind not in {"video", "audio"}:
            continue
        for clip in track.clips:
            if clip.source_asset_id == bed and _clip_audible(recipe, track, clip):
                return [bed]
    return []


_SOUNDTRACK_UNREQUESTED = Decline(
    "requirement_conflict",
    "I can rebuild the edit without that soundtrack, or you can ask for music.",
)


def _verify_composition(
    contract: CreatorRenderContract,
    recipe: EditRecipeV2,
    composition: CompositionCommitments,
    *,
    manifest: Mapping[str, Any],
    audible: Any,
    picture: Sequence[Any],
    frame: float,
) -> None:
    """The checks that only make sense once the plan commits to a composition (KRI-479).

    ``voice_covers_timeline`` / ``voice_window_contiguous``: the approved camera-audio clip
    plays as ONE contiguous window from time zero up to where the picture ends (or the
    committed span), give or take the sentence-snap slack. ``picture_shot_floor``: no shot is
    below the readable floor unless its whole clip is. No soundtrack other than the approved
    voice. Nothing, voice included, runs past the picture (``recipe.duration`` is the max end
    over ALL tracks, so a long voice would silently stretch the video).
    """

    voice_ids = set(contract.audio_source_ids)
    if composition.voice_picture == "hidden" and any(
        isinstance(manifest.get(clip.source_asset_id), OriginalRenderAsset)
        and manifest[clip.source_asset_id].media_id in voice_ids
        for clip in picture
    ):
        raise _phone_decline(
            "order_required",
            "This edit shows the picture of the clip that is only meant to be heard.",
        )

    picture_end = max((c.timeline_start + c.source_duration / c.rate for c in picture), default=0.0)
    if any(layer.end > picture_end + frame for layer in recipe.text_layers):
        # A title held past the last shot would sit over black (or stretch the video).
        raise _phone_decline(
            "exact_texts",
            "This edit keeps confirmed text on screen after the picture has ended.",
            field_path="opening_title_duration_s",
        )
    if recipe.duration > picture_end + frame:
        raise _phone_decline(
            "duration_s",
            "This edit's voice or text runs past the end of the picture.",
            field_path="target_duration_s",
        )

    extras = [
        clip
        for track in recipe.tracks
        if track.kind in {"video", "audio"}
        for clip in track.clips
        if audible(track, clip)
        and not isinstance(manifest.get(clip.source_asset_id), OriginalRenderAsset)
    ]
    if extras or music_bed_audible(recipe) is not None or recipe.audio.narration_asset_id:
        raise CreatorRenderContractError(
            "This edit has a soundtrack you didn't ask for.",
            decline_reason=_SOUNDTRACK_UNREQUESTED.reason,
            field_path="audio_strategy",
            alternative=_SOUNDTRACK_UNREQUESTED.alternative,
        )

    voice = sorted(
        (
            clip
            for track in recipe.tracks
            if track.kind == "audio"
            for clip in track.clips
            if isinstance(manifest.get(clip.source_asset_id), OriginalRenderAsset)
            and manifest[clip.source_asset_id].media_id in voice_ids
            and audible(track, clip)
        ),
        key=lambda clip: clip.timeline_start,
    )
    expected = composition.voice_span_s if composition.voice_span_s is not None else picture_end
    covered = 0.0
    for clip in voice:
        if clip.timeline_start > covered + frame:
            raise _phone_decline(
                "audio_source_ids",
                "This edit's voice has a gap or starts late instead of playing straight through.",
            )
        covered = max(covered, clip.timeline_start + clip.source_duration / clip.rate)
    if not voice or covered < expected - voice_tail_slack_s(expected) - frame:
        raise _phone_decline(
            "audio_source_ids", "This edit's voice stops before the end of the picture."
        )

    from app.pipeline.unified_montage import MIN_READABLE_SHOT_S  # noqa: PLC0415

    floor = composition.min_shot_s if composition.min_shot_s is not None else MIN_READABLE_SHOT_S
    source_seconds = {asset.id: asset.duration for asset in recipe.assets}
    for clip in picture:
        shown = clip.source_duration / clip.rate
        whole = source_seconds.get(clip.source_asset_id)
        if shown + frame / 2 >= floor or (whole is not None and shown + 0.05 + 2 * frame >= whole):
            continue
        raise _phone_decline(
            "duration_s",
            "A shot in this edit is too short to be seen.",
            field_path="target_duration_s",
        )


def text_source_group(
    requirements: Sequence[TextRequirement], requirement: TextRequirement
) -> frozenset[str]:
    """Source IDs for a shared literal, retaining the existing per-clip wire contract."""
    if requirement.role != "clip" or not requirement.media_id or requirement.shot_index is not None:
        return frozenset()
    group = frozenset(
        item.media_id
        for item in requirements
        if item.role == "clip"
        and item.media_id
        and item.shot_index is None
        and item.text == requirement.text
    )
    return group if len(group) > 1 else frozenset()


def source_group_text_matches(
    matches: Sequence[dict[str, Any]],
    *,
    group: frozenset[str],
    media_id: str,
    spans: Sequence[tuple[str, float, float]],
    tolerance_s: float,
    minimum_overlap_s: float,
) -> list[dict[str, Any]]:
    """Visible matches within requested source groups, shared by phone and cloud.

    Every named source must exist. A layer can span adjacent group members, but
    cannot bridge unrelated footage or satisfy a member it never overlaps.
    """
    selected = [(source, start, end) for source, start, end in spans if source in group]
    if not group.issubset({source for source, _start, _end in selected}):
        return []
    connected: list[tuple[float, float]] = []
    for start, end in sorted((start, end) for _source, start, end in selected):
        if connected and start <= connected[-1][1] + tolerance_s:
            connected[-1] = (connected[-1][0], max(connected[-1][1], end))
        else:
            connected.append((start, end))
    bounded = [
        row
        for row in matches
        if (row.get("media_id") is None or row["media_id"] in group)
        and any(
            row["start_s"] >= start - tolerance_s and row["end_s"] <= end + tolerance_s
            for start, end in connected
        )
    ]
    targets = [(start, end) for source, start, end in selected if source == media_id]
    minimum_overlap = max(minimum_overlap_s - 1e-6, 1e-6)
    if not targets or any(
        not any(
            min(row["end_s"], end) - max(row["start_s"], start) >= minimum_overlap
            for row in bounded
        )
        for start, end in targets
    ):
        return []
    return bounded


def check_recipe_duration(
    contract: CreatorRenderContract,
    duration_s: float,
    *,
    length_policy: Any = None,
    max_export_duration_s: float | None = None,
    brand_tail_s: float | None = None,
    exact_tolerance_s: float | None = None,
    emit_observation: bool = True,
) -> dict[str, Any] | None:
    """The length promise `verify_phone_recipe` checks, for an edit of ``duration_s``.

    Raises the typed refusal a pin would raise; returns the length-policy observation (or
    ``None``). Split out so a worker can ask "would adding this clip break the length the
    creator approved?" before it compiles (KRI-841), with the very same rule: an exclusive
    "under N seconds" cap counts the standard ending (``brand_tail_s``), a proven creator
    length is checked as the policy state says, and a legacy stated length drifts by 10 %
    (``exact_tolerance_s`` replaces that for a composer plan's exact shots). A what-if probe
    passes ``emit_observation=False`` so it adds nothing to the fleet length-check proof.
    """
    if max_export_duration_s is not None:
        from app.kria.device_render import BRAND_TAIL_SECONDS  # noqa: PLC0415
        from app.services.export_duration_policy import check_export_maximum  # noqa: PLC0415

        # Reserve the standard ending before the phone does expensive work.
        # Publication independently checks the actual bytes, without this estimate.
        check_export_maximum(
            duration_s + (BRAND_TAIL_SECONDS["standard"] if brand_tail_s is None else brand_tail_s),
            max_export_duration_s,
        )
    elif length_policy is not None:
        from app.services.length_policy_state import check_creator_duration  # noqa: PLC0415

        try:
            return check_creator_duration(
                contract, duration_s, state=length_policy, emit_observation=emit_observation
            )
        except CreatorRenderContractError as exc:
            raise _phone_decline("duration_s", str(exc)) from exc
    elif contract.duration_s is not None:
        drift = abs(duration_s - contract.duration_s)
        allowed = exact_tolerance_s if exact_tolerance_s is not None else contract.duration_s * 0.1
        if drift > allowed + 1e-9:
            raise _phone_decline("duration_s", "This edit couldn't keep the confirmed length.")
    return None


def creator_length_note(
    contract: CreatorRenderContract | None,
    actual_duration_s: float,
    *,
    length_policy: Any = None,
) -> str | None:
    """The creator-visible line for a confirmed length the edit could not keep, or ``None``.

    Since the length policy (every job stamped from 2026-10-08) a confirmed length is a
    ``log_only`` check by default: an edit the footage cannot stretch renders at the
    footage's length, which until now happened without a word to the creator. ``None``
    when there is no confirmed length, it is kept (the ten-percent rule of
    ``creator_duration_matches``), or the pin-time verifier still decides (no policy, or
    an ``enforce`` one: that refusal speaks for itself).
    """
    from app.services.length_policy import creator_duration_matches  # noqa: PLC0415

    if contract is None or contract.duration_s is None:
        return None
    if getattr(length_policy, "check_mode", None) != "log_only":
        return None
    requested = contract.duration_s
    if creator_duration_matches(actual_duration_s, requested):
        return None
    actual = f"{actual_duration_s:.0f}" if actual_duration_s >= 10 else f"{actual_duration_s:.1f}"
    if actual_duration_s < requested:
        return (
            f"Your clips fill about {actual} seconds, so the video is that long instead of "
            f"the {requested:g} seconds you asked for"
        )
    return f"The video runs about {actual} seconds; you asked for {requested:g}"


def _contract_media_id(asset: object) -> str | None:
    """The contract's id for a recipe source: an original's media id, or ``asset-<id>`` for
    a Visuals photo or video (KRI-1069, KRI-1102). ``None`` for anything else."""
    if isinstance(asset, OriginalRenderAsset):
        return asset.media_id
    if isinstance(asset, VisualRenderAsset):
        return f"asset-{asset.visual_id}"
    return None


def _verify_hidden_parts(
    contract: CreatorRenderContract, recipe: EditRecipeV2, manifest: Mapping[str, Any]
) -> None:
    """Leverkusen M2: no clip of the recipe reads a second of a part the creator hid.

    Every track (picture, sound, overlay) of every original or Visuals video is checked by
    its own source window: a crossfade overlaps two clips on the timeline, never reads
    outside either window (`KriaMediaEngine` ClipTransitionTiming), and a held frame is the
    window's last one. An end part needs the file's length; a clip without one is refused.
    """
    by_media: dict[str, list[HiddenPart]] = {}
    for row in contract.hidden_parts:
        part = HiddenPart(row.media_id, row.edge, row.seconds)
        by_media.setdefault(row.media_id, []).append(part)
    lengths = {asset.id: asset.duration for asset in recipe.assets}
    for track in recipe.tracks:
        for clip in track.clips:
            media_id = _contract_media_id(manifest.get(clip.source_asset_id))
            if media_id is None:
                continue
            parts = by_media.get(media_id)
            if not parts:
                continue
            length = lengths.get(clip.source_asset_id)
            if length is None and any(part.edge == "end" for part in parts):
                raise _phone_decline(
                    "hidden_parts", "I couldn't check that this edit leaves out the part you hid."
                )
            if shows_hidden(
                clip.source_start,
                clip.source_start + clip.source_duration,
                float(length) if length is not None else float("inf"),
                parts,
            ):
                raise _phone_decline(
                    "hidden_parts", "This edit would show a part of a clip you asked me to hide."
                )


def verify_phone_recipe(
    contract: CreatorRenderContract,
    recipe: EditRecipeV2,
    *,
    source_audio: Mapping[str, bool] | None = None,
    composition: CompositionCommitments | None = None,
    length_policy: Any = None,
    max_export_duration_s: float | None = None,
    brand_tail_s: float | None = None,
) -> list[dict[str, Any]]:
    """Check a compiled phone recipe against the pinned contract.

    ``composition`` (KRI-479) is passed only for a composer route whose plan carries
    commitments: it adds the voice / shot-floor / soundtrack / overrun checks and tightens
    the duration tolerance from 10 % to ``max(0.1 s, 1 frame)`` (the composer sums exact
    shots, so a looser bound only hides drift). ``None`` leaves every legacy lane unchanged.
    """
    if contract.unresolved:
        raise unresolved_decline(contract.unresolved[0])
    manifest = {asset.id: asset for asset in recipe.asset_manifest.assets}
    if contract.hidden_parts:
        _verify_hidden_parts(contract, recipe, manifest)
    receipts: list[dict[str, Any]] = []
    observation = check_recipe_duration(
        contract,
        recipe.duration,
        length_policy=length_policy,
        max_export_duration_s=max_export_duration_s,
        brand_tail_s=brand_tail_s,
        exact_tolerance_s=max(0.1, 1 / recipe.frame_rate) if composition is not None else None,
    )
    if observation is not None:
        receipts.append(observation)

    def audible(track, clip) -> bool:
        return _clip_audible(recipe, track, clip)

    doubled = doubled_soundtrack_assets(recipe)
    if doubled:
        raise CreatorRenderContractError(
            "This edit would play its soundtrack twice at once.",
            decline_reason=_SOUNDTRACK_TWICE.reason,
            field_path=REQUIREMENT_FIELD_PATHS["require_voiceover"],
            alternative=_SOUNDTRACK_TWICE.alternative,
        )
    bed_asset = manifest.get(music_bed_audible(recipe) or "")
    if contract.require_voiceover:
        voice_is_audible = isinstance(bed_asset, VoiceoverRenderAsset) or any(
            track.kind == "audio"
            and audible(track, clip)
            and isinstance(manifest.get(clip.source_asset_id), VoiceoverRenderAsset)
            for track in recipe.tracks
            for clip in track.clips
        )
        if not voice_is_audible:
            raise _phone_decline(
                "require_voiceover", "This edit needs your recorded voice before it can render."
            )
    original_ids = {
        asset.media_id
        for asset in manifest.values()
        if isinstance(asset, OriginalRenderAsset)
        and any(
            (
                track.kind in {"video", "audio"}
                or (
                    track.kind == "overlay"
                    and clip.visual_placement is None
                    and clip.overlay_preserve_alpha is None
                )
            )
            and clip.source_asset_id == asset.id
            and audible(track, clip)
            and (source_audio is None or source_audio.get(asset.media_id) is True)
            for track in recipe.tracks
            for clip in track.clips
        )
    }
    # A processed-audio clip is still the creator's camera audio, merely a
    # pinned, server-made derivative.  Treat it as its declared source for
    # original-audio requirements; otherwise muting the video clip before
    # adding the timed derivative would falsely look like dropped audio.
    original_ids.update(
        asset.source_media_id
        for asset in manifest.values()
        if isinstance(asset, ProcessedAudioRenderAsset)
        and any(
            track.kind == "audio"
            and clip.source_asset_id == asset.id
            and audible(track, clip)
            and (source_audio is None or source_audio.get(asset.source_media_id) is True)
            for track in recipe.tracks
            for clip in track.clips
        )
    )
    if isinstance(bed_asset, OriginalRenderAsset) and (
        source_audio is None or source_audio.get(bed_asset.media_id) is True
    ):
        # A bed that names the camera's file plays the camera audio too.
        original_ids.add(bed_asset.media_id)
    if contract.original_audio == "forbid" and original_ids:
        raise _audio_decline(
            "original_audio", "This edit can't use the camera audio you turned off."
        )
    if (contract.original_audio == "require" or contract.audio_source_ids) and source_audio is None:
        raise _phone_decline(
            "audio_source_ids" if contract.audio_source_ids else "original_audio",
            "I couldn't verify audio in the approved source files.",
        )
    if contract.original_audio == "require" and not original_ids:
        raise _audio_decline("original_audio", "This edit needs audible camera audio.")
    if contract.audio_source_ids and original_ids != set(contract.audio_source_ids):
        raise _audio_decline(
            "audio_source_ids", "This edit couldn't keep the confirmed camera-audio sources."
        )

    picture = sorted(
        [clip for track in recipe.tracks if track.kind == "video" for clip in track.clips],
        key=lambda clip: clip.timeline_start,
    )
    frame = 1 / recipe.frame_rate
    if composition is not None:
        _verify_composition(
            contract,
            recipe,
            composition,
            manifest=manifest,
            audible=audible,
            picture=picture,
            frame=frame,
        )

    def _run_can_be_visible(run) -> bool:  # noqa: ANN001
        """Reject only layers the portable paint contract proves invisible."""

        if run.fill.alpha > 0 or (run.stroke_width > 0 and run.stroke.alpha > 0):
            return True
        if run.gradient is not None and any(stop.color.alpha > 0 for stop in run.gradient.stops):
            return True
        return any(blur.color.alpha > 0 for blur in run.blur_layers)

    def _layer_text(layer) -> str:  # noqa: ANN001
        runs = [unicodedata.normalize("NFC", run.text) for run in layer.runs]
        # Karaoke compilation emits one word per run on a shared baseline, so
        # its visual word boundaries are spaces. Ordinary runs on one baseline
        # may instead be styled spans of a single word and must stay adjacent.
        if layer.effect == "karaoke-line":
            return " ".join(runs)
        lines: list[list[str]] = []
        baseline: float | None = None
        for run, text in zip(layer.runs, runs, strict=True):
            if baseline is None or abs(run.baseline_y - baseline) > 0.001:
                lines.append([text])
                baseline = run.baseline_y
            else:
                lines[-1].append(text)
        return "\n".join("".join(line) for line in lines)

    def text_layers() -> list[tuple[str, str, float, float]]:
        return [
            (layer.id, _normal(_layer_text(layer)), layer.start, layer.end)
            for layer in recipe.text_layers
            if layer.runs
            and all(
                _run_can_be_visible(run)
                or (
                    layer.karaoke is not None
                    and layer.karaoke.highlight.alpha > 0
                    and layer.karaoke.starts[index] < layer.end - layer.start
                )
                for index, run in enumerate(layer.runs)
                if run.text.strip()
            )
        ]

    rendered = text_layers()
    rendered_evidence = [
        {"element_id": layer_id, "text": text, "start_s": start, "end_s": end}
        for layer_id, text, start, end in rendered
    ]
    for requirement in contract.exact_texts:
        matches = text_matches(
            rendered_evidence,
            requirement.text,
            role=requirement.role,
            tolerance_s=frame,
        )
        if (
            not matches
            and requirement.role == "any"
            and requirement.duration_s is None
            and chapter_list(requirement.text, [row["text"] for row in rendered_evidence])
            is not None
        ):
            # KRI-545: a brief literal that lists chapter names ("Sabah, Üniversite, Akşam")
            # is drawn as those names, each its own label layer on its clips, never as one
            # line. Every name must still be a whole visible layer.
            continue
        if not matches:
            raise _phone_decline(
                "exact_texts",
                "This edit is missing confirmed on-screen text.",
                field_path=text_field_path(requirement),
                missing_text=requirement.text,
            )
        if requirement.duration_s is not None:
            matches = [
                row
                for row in matches
                if row["end_s"] - row["start_s"] + frame >= requirement.duration_s
            ]
            if not matches:
                raise _placement_decline(
                    requirement,
                    "This edit couldn't keep confirmed text on screen long enough.",
                    field_path=(
                        "opening_title_duration_s"
                        if requirement.role == "opening"
                        else text_field_path(requirement)
                    ),
                )
        if requirement.role == "opening":
            matches = [row for row in matches if row["start_s"] <= frame]
        elif requirement.role == "closing":
            matches = [row for row in matches if row["end_s"] >= recipe.duration - frame]
        elif requirement.role == "clip":
            targets = picture
            if requirement.shot_index is not None:
                targets = picture[requirement.shot_index : requirement.shot_index + 1]
            elif requirement.media_id:
                # KRI-1102: a label on a Visuals photo is looked for on that photo's shot.
                targets = [
                    clip
                    for clip in picture
                    if _contract_media_id(manifest.get(clip.source_asset_id))
                    == requirement.media_id
                ]
            group = text_source_group(contract.exact_texts, requirement)
            if group:
                matches = source_group_text_matches(
                    matches,
                    group=group,
                    media_id=requirement.media_id or "",
                    spans=[
                        (
                            source,
                            clip.timeline_start,
                            clip.timeline_start
                            + clip.source_duration / clip.rate
                            + (clip.hold_duration or 0),
                        )
                        for clip in picture
                        if (source := _contract_media_id(manifest.get(clip.source_asset_id)))
                        is not None
                    ],
                    tolerance_s=frame,
                    minimum_overlap_s=frame,
                )
            elif not targets or any(
                not any(
                    row["start_s"] >= clip.timeline_start - frame
                    and row["end_s"]
                    <= clip.timeline_start
                    + clip.source_duration / clip.rate
                    + (clip.hold_duration or 0)
                    + frame
                    for row in matches
                )
                for clip in targets
            ):
                matches = []
        if not matches:
            raise _placement_decline(
                requirement,
                "This edit put confirmed text in the wrong place.",
                field_path=text_field_path(requirement),
            )
    if contract.order_required:
        actual: list[str] = []
        for clip in picture:
            # KRI-1069: a Visuals photo or video sits in the order under its manifest id.
            media_id = _contract_media_id(manifest.get(clip.source_asset_id))
            if media_id is None:
                raise _phone_decline(
                    "order_required",
                    "This edit has an unverified picture source in the confirmed order.",
                )
            if not actual or actual[-1] != media_id:
                actual.append(media_id)
        if not contract.order_ids or tuple(actual) != contract.order_ids:
            raise _phone_decline(
                "order_required", "This edit couldn't keep the confirmed clip order."
            )
    receipts.append({"duration_s": recipe.duration, "verified": True})
    return receipts
