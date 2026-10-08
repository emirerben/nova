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
from dataclasses import dataclass
from types import UnionType
from typing import Annotated, Any, Literal, Union, get_args, get_origin

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.agents._schemas.creator_agent import CreativeStrategy
from app.agents._schemas.edit_format import NARRATED_EDIT_FORMATS
from app.kria.brief import CreativeBrief
from app.kria.brief_route import chapter_list
from app.kria.recipes_v2 import EditRecipeV2
from app.kria.render_assets import OriginalRenderAsset, VoiceoverRenderAsset
from app.pipeline.phone_guided_plan import UnsupportedPhonePlan
from app.pipeline.phone_recipe_shared import voice_tail_slack_s
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
)
from app.services.clip_order_sequence import (
    apply_sequence,
    sequence_rows,
    stated_anchors,
    unresolved_questions,
)

CONTRACT_FIELD = "creator_render_requirements"
REQUIREMENT_VERSION_FIELD = "creator_render_requirements_version"
# Edit formats whose length is set by the footage, not by a target: a Talking
# edit keeps the whole take (minus speech-cleanup pauses) and a voiceover edit
# runs as long as the voiceover. `brief_checks._check_timing` reports the same
# rule to the creator (KRI-142). A declared `talking_head` is not here: its
# cloud assembler caps the output at the target, and the phone only reaches a
# multi-clip Talking head through the narrated family.
TAKE_LENGTH_EDIT_FORMATS: frozenset[str] = frozenset({"subtitled", *NARRATED_EDIT_FORMATS})

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
    ``decline_payload``).
    """

    def __init__(
        self,
        message: str,
        *,
        decline_reason: DeclineReason | None = None,
        field_path: str | None = None,
        alternative: str | None = None,
    ) -> None:
        super().__init__(message)
        self.decline_reason = decline_reason
        self.field_path = field_path
        self.alternative = alternative


def decline_payload(exc: BaseException) -> dict[str, str]:
    """The typed decline persisted next to a failure code (empty when untyped)."""

    reason = getattr(exc, "decline_reason", None)
    if reason not in DECLINE_REASONS:
        return {}
    payload = {"decline_reason": str(reason)}
    for key in ("field_path", "alternative"):
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
    "label_source",
    "transcript_kind",
    "creator_text",
    "caption_attribute",
    "placeholder",
    "position",
    "order_by",
)

FIELD_MATRIX: dict[str, FieldRule] = {
    # Hard requirements projected by build_render_contract.
    **_rules(
        "supported",
        "render_contract:duration",
        "target_duration_s",
        note="pinned only when target_duration_requested is true; a default is not a requirement",
    ),
    **_rules(
        "supported",
        "render_contract:duration",
        "target_duration_requested",
        note="server provenance: only an explicit request becomes a duration requirement",
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
        "upstream_resolved",
        "clip_intent_resolver",
        "clip_intents[]",
        *(f"clip_intents[].{leaf}" for leaf in _CLIP_INTENT_LEAVES),
        "resolved_clip_intents[]",
        *(f"resolved_clip_intents[].{leaf}" for leaf in _CLIP_INTENT_LEAVES),
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
            )
        ),
        note=(
            "server-owned creator decisions on material conflicts; the planner gate writes "
            "them, build_render_contract consumes them (order basis, shot placement) and the "
            "chosen duration/clip subset is already rewritten into target_duration_s / "
            "selected_media_ids"
        ),
    ),
    **_rules(
        "upstream_resolved",
        "user_song_planner",
        "song_sync",
        "resolved_song_takes[]",
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


def _model_children(annotation: Any) -> list[type[BaseModel]]:
    origin = get_origin(annotation)
    if origin is Annotated:
        return _model_children(get_args(annotation)[0])
    if origin is not None:
        found: list[type[BaseModel]] = []
        for arg in get_args(annotation):
            found += _model_children(arg)
        return found
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
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
    for name, field in model.model_fields.items():
        path = prefix + name + ("[]" if _is_list(field.annotation) else "")
        paths.add(path)
        for child in _model_children(field.annotation):
            paths |= schema_field_paths(child, path + ".")
    return paths


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
        _phone(
            "phone_guided_unified_montage",
            set(),
            audio_source_ids=Decline(
                "capability_unavailable",
                "Ask for a spoken-excerpt montage, which can keep that camera audio.",
            ),
        ),
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
            audio_source_ids=_VOICE_CONFLICT,
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
_POST_V1_FIELD_DEFAULTS: dict[str, Any] = {}


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


def build_render_contract(
    strategy: Mapping[str, Any] | None,
    *,
    generation_id: str,
    brief: CreativeBrief | None = None,
    media_snapshot: Mapping[str, Any] | None = None,
    clip_order: Sequence[str] = (),
    has_voiceover: bool = False,
    composition: CompositionCommitments | None = None,
) -> CreatorRenderContract | None:
    """Pin only facts that a portable recipe can objectively demonstrate.

    ``composition`` (KRI-479) carries plan commitments that change how the order set is
    derived without touching the contract model: with ``voice_picture == "hidden"`` the
    camera-audio clip is the voice only, so it is left out of ``order_ids``
    (``audio_source_ids`` keeps it). ``None`` is byte-identical to before.
    """
    if strategy is None and brief is None:
        return None
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
    # A Talking (subtitled) edit runs as long as the take and a voiceover edit (the
    # narrated family, or a montage spined by a recorded voiceover) as long as the
    # voiceover: no compiler trims those to a named length, so a length the creator
    # asked for is not a fact the recipe can prove (the brief receipt already says
    # the length follows the take or the voiceover). Pinning it refused every such
    # edit at the pin step (job e1c5f89e, 2026-10-06: a 68 s Talking take approved
    # with "keep it under 45 seconds").
    length_is_pinnable = typed is None or not (
        typed.edit_format in TAKE_LENGTH_EDIT_FORMATS or typed.audio_strategy == "voiceover"
    )
    is_user_song = bool(typed and typed.audio_strategy == "user_song")
    duration_answered = (
        answers.get(CONFLICT_DURATION_VS_COUNT) is not None
        or answers.get(CONFLICT_VOICE_VS_DURATION) is not None
    )
    brief_names_duration = bool(
        brief is not None
        and any(r.kind == "timing" and r.facts.get("duration_s") is not None for r in brief.live())
    )
    strategy_duration_is_evidence = duration_answered or (
        not is_user_song and (brief is None or brief_names_duration)
    )
    if (
        length_is_pinnable
        and strategy_duration_is_evidence
        and raw.get("target_duration_requested") is True
        and "target_duration_s" in raw
    ):
        durations.append(float(raw["target_duration_s"]))
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
    order_ids = tuple(
        str(item) for item in clip_order if str(item).strip() and str(item) not in hidden_ids
    )
    order_basis = "confirmed" if order_ids else None
    unresolved: list[str] = []
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
    if brief:
        for requirement in brief.live():
            if requirement.kind == "timing" and requirement.facts.get("duration_s") is not None:
                if not length_is_pinnable:
                    continue
                answered = answers.get(CONFLICT_DURATION_VS_COUNT) or answers.get(
                    CONFLICT_VOICE_VS_DURATION
                )
                if answered is not None and requirement.id in answered.requirement_ids:
                    # The creator chose a different length for exactly this requirement;
                    # the approved strategy carries it (target_duration_s, requested).
                    continue
                durations.append(float(requirement.facts["duration_s"]))
            if requirement.kind == "text" and requirement.literal:
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
                        unresolved.append(
                            "I need an explicit shot assignment for the confirmed text."
                        )
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
                order_required = True
                if order_answer is not None and requirement.id in order_answer.requirement_ids:
                    continue  # the creator's answer decides how this order is met
                if key == ATTACHMENT_ORDER_KEY and order_answer is not None:
                    # Only a recorded creator answer makes "attachment" a verifiable
                    # basis; a bare brief key stays unresolved exactly as before.
                    attachment_order = True
                elif key not in CAPTURE_ORDER_KEYS:
                    unresolved.append("I can't verify this ordering rule from the approved media.")
    if order_answer is not None:
        if order_answer.option == OPT_UNORDERED:
            # The creator chose to drop the chronological promise: nothing to verify.
            order_required = False
        elif order_answer.option == OPT_ATTACHMENT_ORDER:
            order_required = True
            attachment_order = True
    if order_required and attachment_order:
        # An explicit, verifiable basis: the order the clips were added to the project.
        rows = (media_snapshot or {}).get("clip_assignments") or []
        selected = set(typed.selected_media_ids or ()) - hidden_ids if typed else set()
        if selected:
            rows = [
                row for row in rows if isinstance(row, Mapping) and row.get("media_id") in selected
            ]
        if hidden_ids:
            rows = [
                row
                for row in rows
                if not (isinstance(row, Mapping) and str(row.get("media_id")) in hidden_ids)
            ]
        ids = [
            str(row["media_id"]) for row in rows if isinstance(row, Mapping) and row.get("media_id")
        ]
        if not ids or len(ids) != len(rows) or len(set(ids)) != len(ids):
            unresolved.append("I need to know which clips are in the edit to verify their order.")
        else:
            order_ids = tuple(ids)
            order_basis = "attachment_order"
    elif order_required:
        from app.services.clip_facts import capture_from_assignment

        rows = (media_snapshot or {}).get("clip_assignments") or []
        selected = set(typed.selected_media_ids or ()) - hidden_ids if typed else set()
        if selected:
            rows = [
                row for row in rows if isinstance(row, Mapping) and row.get("media_id") in selected
            ]
        if hidden_ids:
            rows = [
                row
                for row in rows
                if not (isinstance(row, Mapping) and str(row.get("media_id")) in hidden_ids)
            ]
        dates = {}
        for row in rows:
            if not isinstance(row, Mapping) or not row.get("media_id"):
                continue
            capture = capture_from_assignment(row)
            if capture and capture.capture_time:
                dates[str(row["media_id"])] = capture.capture_time
        ids = [
            str(row["media_id"]) for row in rows if isinstance(row, Mapping) and row.get("media_id")
        ]
        if (
            not ids
            or len(ids) != len(rows)
            or len(set(ids)) != len(ids)
            or set(ids) != set(dates)
            or (selected and set(ids) != selected)
        ):
            order_ids = ()
            unresolved.append(
                "I need capture times for every selected clip to verify chronological order."
            )
        else:
            order_ids = tuple(sorted(ids, key=lambda media_id: dates[media_id]))
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
                f"I couldn't tell which clip \"{words}\" means, so I can't confirm the order."
                for words in unplaced.values()
            )
        elif any(status != "resolved" for _position, _name, _members, status in rows):
            order_ids = ()
            order_basis = None
            unresolved.extend(
                unresolved_questions(raw.get("resolved_clip_intents")) or [_SEQUENCE_UNRESOLVED]
            )
        elif rows:
            order_ids = tuple(apply_sequence(order_ids, rows))
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
        strategy_digest=_hash(raw) if strategy is not None else None,
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
    return CreatorRenderContract(**data).rebind()


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
    requirement: str, message: str, *, field_path: str | None = None
) -> CreatorRenderContractError:
    """A typed refusal for ``requirement`` using the declared phone-verifier reason."""

    decline = PHONE_VERIFIER_DECLINES[requirement]
    return CreatorRenderContractError(
        message,
        decline_reason=decline.reason,
        field_path=field_path or REQUIREMENT_FIELD_PATHS[requirement],
        alternative=decline.alternative or None,
    )


def unresolved_decline(message: str) -> CreatorRenderContractError:
    """The typed refusal for an unresolved approved requirement (needs a choice)."""

    return CreatorRenderContractError(
        message,
        decline_reason=_UNRESOLVED_DECLINE.reason,
        alternative=_UNRESOLVED_DECLINE.alternative,
    )


def check_phone_dispatch_contract(
    contract: CreatorRenderContract,
    *,
    snapshot_generation_id: object,
    has_voiceover_candidate: bool,
    user_song: object = None,
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
    if contract.audio_source_ids and (contract.require_voiceover or user_song):
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


def verify_phone_recipe(
    contract: CreatorRenderContract,
    recipe: EditRecipeV2,
    *,
    source_audio: Mapping[str, bool] | None = None,
    composition: CompositionCommitments | None = None,
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
    receipts: list[dict[str, Any]] = []
    if contract.duration_s is not None:
        drift = abs(recipe.duration - contract.duration_s)
        allowed = (
            max(0.1, 1 / recipe.frame_rate)
            if composition is not None
            else contract.duration_s * 0.1
        )
        if drift > allowed + 1e-9:
            raise _phone_decline("duration_s", "This edit couldn't keep the confirmed length.")

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
    if isinstance(bed_asset, OriginalRenderAsset) and (
        source_audio is None or source_audio.get(bed_asset.media_id) is True
    ):
        # A bed that names the camera's file plays the camera audio too.
        original_ids.add(bed_asset.media_id)
    if contract.original_audio == "forbid" and original_ids:
        raise _phone_decline(
            "original_audio", "This edit can't use the camera audio you turned off."
        )
    if (contract.original_audio == "require" or contract.audio_source_ids) and source_audio is None:
        raise _phone_decline(
            "audio_source_ids" if contract.audio_source_ids else "original_audio",
            "I couldn't verify audio in the approved source files.",
        )
    if contract.original_audio == "require" and not original_ids:
        raise _phone_decline("original_audio", "This edit needs audible camera audio.")
    if contract.audio_source_ids and original_ids != set(contract.audio_source_ids):
        raise _phone_decline(
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

    def text_layers() -> list[tuple[str, float, float]]:
        return [
            (_normal(_layer_text(layer)), layer.start, layer.end)
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
    for requirement in contract.exact_texts:
        matches = [row for row in rendered if row[0] == _normal(requirement.text)]
        if (
            not matches
            and requirement.role == "any"
            and requirement.duration_s is None
            and chapter_list(requirement.text, [row[0] for row in rendered]) is not None
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
            )
        if requirement.duration_s is not None:
            matches = [row for row in matches if row[2] - row[1] + frame >= requirement.duration_s]
            if not matches:
                raise _phone_decline(
                    "exact_texts",
                    "This edit couldn't keep confirmed text on screen long enough.",
                    field_path=(
                        "opening_title_duration_s"
                        if requirement.role == "opening"
                        else text_field_path(requirement)
                    ),
                )
        if requirement.role == "opening":
            matches = [row for row in matches if row[1] <= frame]
        elif requirement.role == "closing":
            matches = [row for row in matches if row[2] >= recipe.duration - frame]
        elif requirement.role == "clip":
            targets = picture
            if requirement.shot_index is not None:
                targets = picture[requirement.shot_index : requirement.shot_index + 1]
            elif requirement.media_id:
                targets = [
                    clip
                    for clip in picture
                    if isinstance(manifest.get(clip.source_asset_id), OriginalRenderAsset)
                    and manifest[clip.source_asset_id].media_id == requirement.media_id
                ]
            if not targets or any(
                not any(
                    start >= clip.timeline_start - frame
                    and end
                    <= clip.timeline_start
                    + clip.source_duration / clip.rate
                    + (clip.hold_duration or 0)
                    + frame
                    for _text, start, end in matches
                )
                for clip in targets
            ):
                matches = []
        if not matches:
            raise _phone_decline(
                "exact_texts",
                "This edit put confirmed text in the wrong place.",
                field_path=text_field_path(requirement),
            )
    if contract.order_required:
        actual: list[str] = []
        for clip in picture:
            asset = manifest.get(clip.source_asset_id)
            if not isinstance(asset, OriginalRenderAsset):
                raise _phone_decline(
                    "order_required",
                    "This edit has an unverified picture source in the confirmed order.",
                )
            if not actual or actual[-1] != asset.media_id:
                actual.append(asset.media_id)
        if not contract.order_ids or tuple(actual) != contract.order_ids:
            raise _phone_decline(
                "order_required", "This edit couldn't keep the confirmed clip order."
            )
    receipts.append({"duration_s": recipe.duration, "verified": True})
    return receipts
