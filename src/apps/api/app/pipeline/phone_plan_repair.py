"""Planning-time repair + dry run for phone (on-device) guided plans (KRI-286).

The planner can author things the phone compiler cannot express: a sequence
pop-in effect, a font the device has not qualified, a source file too short for
its transition. Those used to surface only AFTER the creator approved the edit,
as a terminal ``phone_plan_unsupported``. This module moves the answer earlier:

* ``compile_phone_guided_with_repairs`` is the worker's compile call. It runs
  ``compile_phone_guided_plan`` + ``validate_phone_pilot_recipe`` and, when the
  reject is one we can fix deterministically, repairs the PLAN and retries.
  Every repair returns a human-readable note (KRI-129: repair, never silently
  override) so the creator and the debug view can see what changed.
* ``validate_proposal_phone_compiles`` is the same compile as a dry run over a
  freshly planned proposal, so a plan the phone cannot render is repaired or
  replaced by the deterministic fallback before the creator ever sees it.

Repairs (all deterministic, all noted):
  sequence effect outside the composite-safe set -> ``fade-in``
  unqualified font / text effect                 -> qualified default font / ``fade-in``

NOT repaired: a transition whose outgoing source is genuinely too short (a
hole in the timeline). No plan-level edit can conjure footage; the reject keeps
failing closed and the planning-time dry run turns it into a re-plan.

Never enumerate ``compile_phone_guided_plan``'s keyword arguments here: other
lanes add kwargs (e.g. ``landscape_fit``) and they must pass through verbatim.
"""

from __future__ import annotations

from typing import Any, NamedTuple

import structlog

from app.pipeline.phone_guided_plan import UnsupportedPhonePlan, compile_phone_guided_plan
from app.services.phone_rollout import (
    PhoneCapabilityUnavailable,
    PhoneFontUnqualified,
    qualified_default_font_family,
    registry_font_file,
    validate_phone_pilot_recipe,
)

log = structlog.get_logger()

# Mirrors the allowlist at the "sequence effect needs composite-stream parity"
# raise site in `compile_phone_guided_plan`; pinned by
# `tests/pipeline/test_phone_plan_repair.py::test_composite_safe_effects_compile`.
COMPOSITE_SAFE_SEQUENCE_EFFECTS = frozenset(
    {"fade-in", "static", "none", "handwriting", "ink-reveal"}
)
_REPAIR_EFFECT = "fade-in"

# At most one repair pass per reject kind, bounded so a stubborn plan cannot loop.
_MAX_REPAIR_PASSES = 3


class PhoneProposalRejected(ValueError):
    """The planning-time dry run found a phone-inexpressible plan it cannot repair."""


class RepairedCompile(NamedTuple):
    recipe: Any
    notes: list[str]
    # The plan actually compiled: the input object itself when nothing was repaired.
    plan: Any


def _repair_sequence_effects(plan) -> tuple[Any, list[str]]:  # noqa: ANN001
    """Sequence-lane text outside the composite-safe effects becomes a plain fade-in."""
    changed = 0
    elements = []
    for element in plan.text_elements:
        if (
            element.role == "generative_sequence"
            and element.effect is not None
            and element.effect not in COMPOSITE_SAFE_SEQUENCE_EFFECTS
        ):
            element = element.model_copy(update={"effect": _REPAIR_EFFECT})
            changed += 1
        elements.append(element)
    if not changed:
        return plan, []
    return (
        plan.model_copy(update={"text_elements": elements}),
        ["Swapped a text animation your iPhone can't play yet for a simple fade-in"],
    )


def _repair_unqualified_fonts(plan, exc: PhoneFontUnqualified) -> tuple[Any, list[str]]:  # noqa: ANN001
    """Re-face / re-animate exactly the text the font gate rejected.

    The gate reports bundled font FILES and text EFFECTS (the same predicate
    `validate_phone_pilot_recipe` uses); elements are matched back to those by
    resolving their family through the font registry the compiler itself uses.
    """
    default_family = qualified_default_font_family()
    notes: list[str] = []
    refaced = reanimated = 0
    lanes: dict[str, list] = {}
    for lane in ("text_elements", "context_label_text_elements", "narration_label_text_elements"):
        elements = []
        for element in getattr(plan, lane):
            updates: dict[str, Any] = {}
            if element.effect in exc.effects:
                updates["effect"] = _REPAIR_EFFECT
                reanimated += 1
            if (
                default_family is not None
                and exc.font_files
                and registry_font_file(element.font_family) in exc.font_files
                and element.font_family != default_family
            ):
                updates["font_family"] = default_family
                refaced += 1
            elements.append(element.model_copy(update=updates) if updates else element)
        lanes[lane] = elements
    if refaced:
        notes.append(f"Changed a text font to {default_family} so it renders on your iPhone")
    if reanimated:
        notes.append("Swapped a text animation your iPhone can't play yet for a simple fade-in")
    if not notes:
        return plan, []
    return plan.model_copy(update=lanes), notes


def repair_plan_for_phone(plan, exc: Exception) -> tuple[Any, list[str]]:  # noqa: ANN001
    """Deterministic plan repair for one reject; ``(plan, [])`` when it is not repairable."""
    if isinstance(exc, PhoneCapabilityUnavailable):
        return plan, []  # a rollout decision, never a plan defect
    if isinstance(exc, UnsupportedPhonePlan) and exc.reason == "sequence_effect":
        return _repair_sequence_effects(plan)
    if isinstance(exc, PhoneFontUnqualified):
        return _repair_unqualified_fonts(plan, exc)
    # "transition_window" and every other reject: no plan-level edit can fix them.
    return plan, []


def compile_phone_guided_repaired(plan, bindings, **kwargs: Any) -> RepairedCompile:  # noqa: ANN001
    """Compile + pilot-validate, repairing known rejects (see module docstring).

    ``kwargs`` go to ``compile_phone_guided_plan`` verbatim. Raises the compile /
    validation error unchanged when it is not repairable (or a repair did not help).
    """
    allow_editor_media = bool(kwargs.get("allow_editor_media", False))
    notes: list[str] = []
    attempted: set[str] = set()
    current = plan
    for _ in range(_MAX_REPAIR_PASSES + 1):
        try:
            recipe = compile_phone_guided_plan(current, bindings, **kwargs)
            validate_phone_pilot_recipe(recipe, allow_editor_media=allow_editor_media)
            return RepairedCompile(recipe, notes, current)
        except (UnsupportedPhonePlan, ValueError) as exc:
            kind = type(exc).__name__ + ":" + str(getattr(exc, "reason", "") or "")
            if kind in attempted:
                raise
            attempted.add(kind)
            repaired, repair_notes = repair_plan_for_phone(current, exc)
            if not repair_notes:
                raise
            log.info("phone_plan_repaired", error=str(exc), notes=repair_notes)
            current = repaired
            notes.extend(note for note in repair_notes if note not in notes)
    raise AssertionError("unreachable")  # pragma: no cover


def compile_phone_guided_with_repairs(plan, bindings, **kwargs: Any) -> tuple[Any, list[str]]:  # noqa: ANN001
    """``compile_phone_guided_repaired`` as ``(recipe, notes)``."""
    result = compile_phone_guided_repaired(plan, bindings, **kwargs)
    return result.recipe, result.notes


def _dry_run_bindings(plan, snapshot, assignments):  # noqa: ANN001
    """Bindings the worker would build, or None when this proposal can't be dry-run.

    Mirrors dispatch (`bind_phone_sources` over the item's analysis proxies) and
    synthesizes the digest-only fields the worker would read from storage (photo
    bytes, narration receipt). Those never influence compilation, so the dry run
    needs no I/O. Anything we cannot faithfully reconstruct -> None (skip: the
    dispatch gate and worker remain the authority).
    """
    from app.kria.media_sources import is_analysis_proxy_path  # noqa: PLC0415
    from app.kria.render_assets import RenderFingerprint  # noqa: PLC0415
    from app.pipeline.phone_recipe_shared import PhoneNarrationBed  # noqa: PLC0415
    from app.services.phone_sources import (  # noqa: PLC0415
        PhoneVisualBinding,
        bind_phone_sources,
    )

    selected = list(
        dict.fromkeys(
            ref.gcs_path
            for ref in snapshot.media
            if ref.lane == "clip" and is_analysis_proxy_path(ref.gcs_path)
        )
    )
    bindings: tuple = ()
    if selected:
        try:
            bindings = bind_phone_sources(list(assignments), selected)
        except (ValueError, TypeError):
            return None
    bound_ids = {binding.media_id for binding in bindings}
    visuals = []
    refs = {ref.media_id: ref for ref in snapshot.media}
    for moment in plan.story_timeline:
        if moment.lane == "clip":
            if moment.media_id not in bound_ids:
                return None
            continue
        ref = refs.get(moment.media_id)
        if ref is None or any(visual.media_id == moment.media_id for visual in visuals):
            continue
        extra: dict[str, Any] = {}
        if moment.kind == "video":
            width, height = ref.analysis.get("width"), ref.analysis.get("height")
            if not (ref.duration_s and isinstance(width, int) and isinstance(height, int)):
                return None
            extra = {"duration_s": ref.duration_s, "width": width, "height": height}
        try:
            visuals.append(
                PhoneVisualBinding(
                    media_id=moment.media_id,
                    gcs_path=moment.gcs_path,
                    generation=moment.generation,
                    sha256="0" * 64,
                    byte_count=1,
                    kind=moment.kind,
                    **extra,
                )
            )
        except ValueError:
            return None
    narration = None
    if plan.narration is not None:
        narration = PhoneNarrationBed(
            plan_item_id="planning-dry-run",
            generation=plan.narration.generation,
            fingerprint=RenderFingerprint(sha256="0" * 64, byte_count=1),
            duration_s=plan.narration.duration_s,
        )
    return bindings, tuple(visuals), narration


def validate_proposal_phone_compiles(snapshot, assignments) -> list[str]:  # noqa: ANN001
    """Dry-run the phone compiler over a freshly planned proposal (KRI-286).

    compile_execution_plan -> bind_phone_sources -> compile_phone_guided_plan ->
    validate_phone_pilot_recipe, with the deterministic repairs applied. Returns
    the repair notes (``[]`` when the plan is already expressible, or when it
    cannot be dry-run: the dispatch gate stays the authority there). Raises
    ``PhoneProposalRejected`` for a plan the phone cannot render and no repair
    fixes; the caller falls back to the deterministic plan.

    The repairs are NOT persisted on the proposal: approval recompiles from the
    snapshot and the worker re-applies the same deterministic repairs
    (``compile_phone_guided_with_repairs``). The notes just tell the creator.
    """
    from app.pipeline.guided_story import (  # noqa: PLC0415
        GuidedStoryExecutionPlan,
        compile_proposal_execution_plan,
    )

    plan = GuidedStoryExecutionPlan.model_validate(compile_proposal_execution_plan(snapshot))
    prepared = _dry_run_bindings(plan, snapshot, assignments)
    if prepared is None:
        log.info("phone_plan_dry_run_skipped")
        return []
    bindings, visuals, narration = prepared
    try:
        _recipe, notes = compile_phone_guided_with_repairs(
            plan,
            bindings,
            visuals=visuals,
            narration=narration,
        )
    except (UnsupportedPhonePlan, ValueError) as exc:
        raise PhoneProposalRejected(str(exc)) from exc
    return notes
