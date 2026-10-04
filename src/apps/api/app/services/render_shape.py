"""Creator-selectable output shape (KRI-306): orientation + bars/crop.

One module owns what a creator may choose, so the approval route, the thread
projection, the dispatch path and the device editor can never disagree:

* ``creation_offer`` -- what the confirm screen offers for a pending strategy
  (and what ``decide_approval`` validates a choice against).
* ``resolve_choice`` -- turns a (possibly partial) choice into the
  ``creator_render_shape`` payload a Job carries, or a typed 422.
* ``device_editor_offer`` -- what the native editor may Save on a phone-rendered
  variant. It advertises only what Save accepts.

The choice is NEVER part of ``approval_fingerprint``: it is a creator setting on
top of an already-reviewed direction, not a different direction.

Landscape output always center-crops (same as the cloud: a landscape canvas has
no bars). Bars/crop (``landscape_fit``) is a portrait-canvas preference for
sideways source clips.
"""

from __future__ import annotations

import os
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any, Literal

OutputOrientationChoice = Literal["portrait", "landscape"]
LandscapeFitChoice = Literal["fit", "fill"]

ORIENTATIONS: tuple[str, ...] = ("portrait", "landscape")
FIT_CHOICES: tuple[str, ...] = ("fit", "fill")

# `all_candidates` key the Job carries; written ONLY when the creator chose.
CREATOR_RENDER_SHAPE_KEY = "creator_render_shape"

# The same aspect thresholds as `infer_story_output_orientation`.
_LANDSCAPE_ASPECT = 1.05
_PORTRAIT_ASPECT = 0.95

# Format table (see plans/kri-285-and-kri-286). `montage` covers guided/unified
# and voiceover montage; day_vlog / single_hero are story shapes layered on it.
_LANDSCAPE_AND_FIT_FORMATS = frozenset({"montage", "day_vlog", "single_hero"})
_FIT_ONLY_FORMATS = frozenset({"subtitled"})

REASON_FORMAT_CLOSED = "format_unsupported"
REASON_MIXED_MEDIA = "mixed_media_timing"
REASON_DISABLED = "disabled"
REASON_NOT_ON_DEVICE = "not_on_device"
REASON_SPEECH_MONTAGE = "speech_montage"
REASON_LOOK = "look_unsupported"


class RenderShapeError(Exception):
    """A creator choice the offer does not allow. Maps to HTTP 422."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def landscape_output_enabled() -> bool:
    """Read at call time so a worker restart is the only thing a flip needs."""
    return os.getenv("LANDSCAPE_OUTPUT_ENABLED", "false").lower() == "true"


@dataclass(frozen=True)
class RenderShapeOffer:
    orientations: tuple[str, ...]
    fit_choices: tuple[str, ...]
    default_orientation: str
    default_fit: str
    reason: str | None = None

    @property
    def has_choice(self) -> bool:
        return len(self.orientations) > 1 or len(self.fit_choices) > 1

    def projection(self) -> dict[str, Any] | None:
        """The public ``CreationThreadOut.render_shape``; None = nothing to pick."""
        if not self.has_choice:
            return None
        return {
            "orientations": list(self.orientations),
            "fit_choices": list(self.fit_choices),
            "default": {
                "output_orientation": self.default_orientation,
                "landscape_fit": self.default_fit,
            },
        }


def _get(strategy: object, name: str) -> Any:
    if isinstance(strategy, Mapping):
        return strategy.get(name)
    return getattr(strategy, name, None)


def _media_display_aspect(original: object) -> float | None:
    """Display aspect of one phone original (rotation applied), or None."""
    width = _get(original, "width")
    height = _get(original, "height")
    rotation = _get(original, "orientation_degrees")
    try:
        w, h = float(width), float(height)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if w <= 0 or h <= 0:
        return None
    if rotation in (90, 270):
        w, h = h, w
    return w / h


def vote_orientation(assignments: Iterable[object] | None) -> OutputOrientationChoice | None:
    """Display-aspect vote over the attached clips; None when nothing votes.

    Reads ``upload_contract.proxy.original`` (width/height/rotation) -- the same
    receipt the phone sources bind. Near-square clips are neutral. Aligned with
    ``infer_story_output_orientation`` (what the unified montage picks on its own
    when nothing is chosen): the majority wins and a tie follows the FIRST
    non-square clip, so the preselected default matches the auto path. Clips are
    weighted equally -- a montage gives every clip a similar-length cut.
    """
    landscape = portrait = 0
    first: OutputOrientationChoice | None = None
    for assignment in assignments or ():
        contract = _get(assignment, "upload_contract")
        proxy = _get(contract, "proxy") if contract else None
        original = _get(proxy, "original") if proxy else None
        aspect = _media_display_aspect(original) if original else None
        if aspect is None or _PORTRAIT_ASPECT <= aspect <= _LANDSCAPE_ASPECT:
            continue
        vote: OutputOrientationChoice = "landscape" if aspect > _LANDSCAPE_ASPECT else "portrait"
        first = first or vote
        if vote == "landscape":
            landscape += 1
        else:
            portrait += 1
    if first is None:
        return None
    if landscape == portrait:
        return first
    return "landscape" if landscape > portrait else "portrait"


def previous_ready_orientation(job: object | None) -> OutputOrientationChoice | None:
    """Orientation of the item's previous ready variant, if there is one."""
    assembly = getattr(job, "assembly_plan", None)
    if not isinstance(assembly, Mapping):
        return None
    for variant in assembly.get("variants") or []:
        if isinstance(variant, Mapping) and variant.get("render_status") == "ready":
            orientation = variant.get("orientation")
            if orientation in ORIENTATIONS:
                return orientation  # type: ignore[return-value]
            return "portrait"
    return None


def _may_route_to_speech_montage(item: object, strategy: object, creator_request: str) -> bool:
    """Whether the worker could take this montage down the spoken-excerpt lane.

    Mirrors the dispatcher's own gate (`run_phone_speech_montage_job`): not a
    voiceover montage, the kill switch on, and `speech_montage_possible` -- the
    SAME first-stage predicate the worker uses -- over the creator's words and the
    attached clips' analysed speech.
    """
    from app.config import settings  # noqa: PLC0415
    from app.services.clip_understanding import clip_record  # noqa: PLC0415
    from app.services.speech_montage_planning import speech_montage_possible  # noqa: PLC0415

    if not settings.speech_excerpt_montage_enabled:
        return False
    if _get(strategy, "audio_strategy") == "voiceover" or getattr(item, "voiceover_gcs_path", None):
        return False
    any_speech = False
    for assignment in getattr(item, "clip_assignments", None) or ():
        if not isinstance(assignment, Mapping):
            continue
        kind = "image" if str(assignment.get("kind") or "video") == "image" else "video"
        try:
            any_speech = any_speech or bool(
                kind == "video"
                and clip_record(assignment.get("analysis"), kind=kind).speech.has_speech
            )
        except Exception:  # noqa: BLE001 - an unreadable analysis is "no speech signal"
            continue
    # The worker's request is the first message plus the brief, which is never
    # empty on a real thread; the draft/plan text available here can be, so an
    # unknown request must not read as "nothing asked".
    return speech_montage_possible(creator_request.strip() or "-", any_clip_has_speech=any_speech)


def creation_offer(
    item: object,
    strategy: object,
    *,
    previous_orientation: str | None = None,
    device_render: bool = True,
    landscape_enabled: bool | None = None,
    creator_request: str = "",
) -> RenderShapeOffer:
    """What the confirm screen offers for ``strategy`` on ``item``.

    ``device_render`` is whether this creator's render runs on the phone: the
    cloud generative loop does not take a creation-time orientation, so landscape
    is only offered where the phone workers honour it. ``creator_request`` is the
    creator's own words (used only to spot a spoken-excerpt montage).
    """
    edit_format = str(_get(strategy, "edit_format") or "montage")
    if landscape_enabled is None:
        landscape_enabled = landscape_output_enabled()
    default_fit = str(getattr(item, "landscape_fit", None) or "fit")
    if default_fit not in FIT_CHOICES:
        default_fit = "fit"

    if edit_format in _LANDSCAPE_AND_FIT_FORMATS:
        fit_choices: tuple[str, ...] = FIT_CHOICES
        landscape_allowed = True
    elif edit_format in _FIT_ONLY_FORMATS:
        fit_choices = FIT_CHOICES
        landscape_allowed = False
    else:
        # narrated / speech-spined / slides: closed (a follow-up ticket).
        return RenderShapeOffer(
            orientations=("portrait",),
            fit_choices=(),
            default_orientation="portrait",
            default_fit=default_fit,
            reason=REASON_FORMAT_CLOSED,
        )

    if edit_format in _LANDSCAPE_AND_FIT_FORMATS and _may_route_to_speech_montage(
        item, strategy, creator_request
    ):
        # The spoken-excerpt montage renders portrait and has no bars/crop: offering
        # a shape it would silently ignore breaks "no silent override" (KRI-129).
        return RenderShapeOffer(
            orientations=("portrait",),
            fit_choices=(),
            default_orientation="portrait",
            default_fit=default_fit,
            reason=REASON_SPEECH_MONTAGE,
        )

    reason: str | None = None
    if landscape_allowed and not landscape_enabled:
        landscape_allowed, reason = False, REASON_DISABLED
    elif landscape_allowed and _get(strategy, "mixed_media_timing") is not None:
        landscape_allowed, reason = False, REASON_MIXED_MEDIA
    elif landscape_allowed and not device_render:
        landscape_allowed, reason = False, REASON_NOT_ON_DEVICE

    orientations: tuple[str, ...] = ORIENTATIONS if landscape_allowed else ("portrait",)
    default_orientation = "portrait"
    if landscape_allowed:
        previous = previous_orientation if previous_orientation in ORIENTATIONS else None
        voted = vote_orientation(getattr(item, "clip_assignments", None))
        default_orientation = previous or voted or "portrait"
    return RenderShapeOffer(
        orientations=orientations,
        fit_choices=fit_choices,
        default_orientation=default_orientation,
        default_fit=default_fit,
        reason=reason,
    )


def resolve_choice(
    offer: RenderShapeOffer,
    output_orientation: str | None,
    landscape_fit: str | None,
) -> dict[str, str] | None:
    """Validate a creator's choice against ``offer``.

    Returns the ``creator_render_shape`` payload (both keys, the unchosen one
    filled from the default), or None when the creator chose nothing. A landscape
    result always carries ``landscape_fit="fill"`` (landscape never has bars).
    """
    if output_orientation is None and landscape_fit is None:
        return None
    if not offer.has_choice:
        raise RenderShapeError(
            "render_shape_not_applicable",
            "There is no output shape to choose for this edit.",
        )
    if output_orientation is not None and output_orientation not in offer.orientations:
        raise RenderShapeError(
            "render_shape_unsupported",
            "That output shape isn't available for this edit.",
        )
    if landscape_fit is not None and landscape_fit not in FIT_CHOICES:
        raise RenderShapeError("render_shape_unsupported", "Unknown bars/crop choice.")
    if landscape_fit is not None and not offer.fit_choices:
        raise RenderShapeError(
            "render_shape_unsupported",
            "Bars or crop isn't available for this edit.",
        )
    orientation = output_orientation or offer.default_orientation
    fit = landscape_fit or offer.default_fit
    if orientation == "landscape":
        if landscape_fit == "fit" and output_orientation is None:
            # The client only asked for bars, but the preselected shape is
            # landscape, which never has them: a contradiction, not something to
            # drop silently. (A client that explicitly picked Landscape may still
            # carry a stale bars value in its state; that is simply ignored.)
            raise RenderShapeError(
                "render_shape_unsupported",
                "Landscape videos always fill the frame; black bars aren't available.",
            )
        fit = "fill"
    return {"output_orientation": orientation, "landscape_fit": fit}


def shape_from_all_candidates(all_candidates: Mapping[str, Any] | None) -> dict[str, str] | None:
    """The creator's explicit choice carried on a Job, validated, else None."""
    raw = (all_candidates or {}).get(CREATOR_RENDER_SHAPE_KEY)
    if not isinstance(raw, Mapping):
        return None
    orientation = raw.get("output_orientation")
    fit = raw.get("landscape_fit")
    if orientation not in ORIENTATIONS or fit not in FIT_CHOICES:
        return None
    return {"output_orientation": str(orientation), "landscape_fit": str(fit)}


# ── device editor ────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class ShapeAxis:
    editable: bool
    reason: str | None
    value: str | None = None


@dataclass(frozen=True)
class EditorShapeOffer:
    # None = leave the base capability map's orientation entry untouched.
    orientation: ShapeAxis | None
    landscape_fit: ShapeAxis


def current_fit_value(variant: Mapping[str, Any], all_candidates: Mapping[str, Any] | None) -> str:
    """The bars/crop value the variant was rendered with (display only)."""
    stored = variant.get("landscape_fit")
    if stored in FIT_CHOICES:
        return str(stored)
    chosen = shape_from_all_candidates(all_candidates)
    if chosen is not None:
        return chosen["landscape_fit"]
    if variant.get("resolved_archetype") == "guided_story":
        return "fill"
    return "fit" if (all_candidates or {}).get("landscape_fit") == "fit" else "fill"


def _pinned_recipe_has_look(variant: Mapping[str, Any], assembly: Mapping[str, Any]) -> bool:
    """Whether a letterbox-capable main video track carries an edit-wide look."""
    from types import SimpleNamespace  # noqa: PLC0415

    from app.pipeline.phone_recipe_shared import FIT_VIDEO_TRACK_IDS  # noqa: PLC0415
    from app.services.device_render import device_status  # noqa: PLC0415

    variant_id = variant.get("variant_id")
    if not variant_id:
        return False
    try:
        recipe = device_status(
            SimpleNamespace(assembly_plan=dict(assembly)), str(variant_id)
        ).request.recipe
    except Exception:  # noqa: BLE001 - no pinned recipe -> no look signal
        return False
    return any(
        clip.look is not None
        for track in getattr(recipe, "tracks", ())
        if track.kind == "video" and track.id in FIT_VIDEO_TRACK_IDS
        for clip in track.clips
    )


def device_editor_offer(
    variant: Mapping[str, Any],
    assembly: Mapping[str, Any],
    *,
    subtitled_lanes: bool = False,
    voiceover_lanes: bool = False,
    guided_revision: bool = False,
    all_candidates: Mapping[str, Any] | None = None,
) -> EditorShapeOffer:
    """What Save accepts for a phone-rendered variant (never advertise more).

    ``subtitled_lanes`` / ``voiceover_lanes`` / ``guided_revision`` are the same
    predicates the matching Save branch checks first; passing them keeps the
    advertised capability and the Save gate in lockstep.
    """
    from app.services.phone_editor import (  # noqa: PLC0415
        is_phone_voiceover_montage_editor_variant,
    )

    archetype = variant.get("resolved_archetype")
    value = current_fit_value(variant, all_candidates)
    orientation_value = str(variant.get("orientation") or "portrait")

    def closed(reason: str) -> ShapeAxis:
        return ShapeAxis(False, reason, value)

    if variant.get("editor_timeline_mode") == "authored":
        return EditorShapeOffer(None, closed("unsupported_archetype"))

    if orientation_value == "landscape":
        # Landscape never has bars: nothing to choose.
        fit_axis = ShapeAxis(False, "landscape_output", "fill")
    elif _pinned_recipe_has_look(variant, assembly):
        # The device throws on a look + transform, so a look clip is never
        # letterboxed: advertising bars would save an unchanged recipe.
        fit_axis = closed(REASON_LOOK)
    else:
        fit_axis = None  # type: ignore[assignment]

    if archetype == "guided_story":
        if fit_axis is None:
            if not guided_revision:
                fit_axis = closed("guided_story_revision_unavailable")
            else:
                fit_axis = ShapeAxis(True, None, value)
        # The guided orientation capability is already revision-gated upstream.
        return EditorShapeOffer(None, fit_axis)

    orientation_closed = ShapeAxis(False, "orientation_unsupported", orientation_value)
    if archetype == "subtitled":
        if fit_axis is None:
            fit_axis = (
                ShapeAxis(True, None, value)
                if subtitled_lanes
                else closed("phone_edit_unsupported")
            )
        return EditorShapeOffer(None, fit_axis)
    if is_phone_voiceover_montage_editor_variant(variant, dict(assembly)):
        if fit_axis is None:
            if not voiceover_lanes:
                fit_axis = closed("phone_edit_unsupported")
            else:
                fit_axis = ShapeAxis(True, None, value)
        # Save recompiles lanes/cut only: re-canvassing a voiceover montage is
        # a re-plan, so advertise orientation closed instead of a Save 422.
        return EditorShapeOffer(orientation_closed, fit_axis)
    # narrated, speech_montage, talking_head, anything else on the phone.
    return EditorShapeOffer(orientation_closed, closed("unsupported_archetype"))


def cloud_fit_axis(variant: Mapping[str, Any]) -> dict[str, Any]:
    """The ``landscape_fit`` capability entry every cloud map carries (always closed)."""
    stored = variant.get("landscape_fit")
    return {
        "editable": False,
        "value": stored if stored in FIT_CHOICES else "fill",
        "reason": "cloud_unsupported",
    }


async def offer_for_item(
    db: Any, item: object, strategy: object, creator_id: object, *, creator_request: str = ""
) -> RenderShapeOffer:
    """``creation_offer`` with the item's previous ready variant and the account's
    render destination resolved (one unlocked Job read)."""
    from app.config import settings  # noqa: PLC0415
    from app.models import Job  # noqa: PLC0415

    job_id = getattr(item, "current_job_id", None)
    job = await db.get(Job, job_id) if job_id is not None else None
    return creation_offer(
        item,
        strategy,
        previous_orientation=previous_ready_orientation(job),
        device_render=bool(settings.phone_rendering_for(creator_id)),
        creator_request=creator_request,
    )
