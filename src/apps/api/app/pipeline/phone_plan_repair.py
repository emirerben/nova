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
  a text window past the end of the timeline     -> clamped onto the timeline
  confirmed on-screen text the plan lost         -> placed back (``repair_plan_for_contract``)

NOT repaired: a transition whose outgoing source is genuinely too short (a
hole in the timeline). No plan-level edit can conjure footage; the reject keeps
failing closed and the planning-time dry run turns it into a re-plan.

Never enumerate ``compile_phone_guided_plan``'s keyword arguments here: other
lanes add kwargs (e.g. ``landscape_fit``) and they must pass through verbatim.
"""

from __future__ import annotations

import unicodedata
from typing import Any, NamedTuple

import structlog

from app.pipeline import phone_guided_plan
from app.pipeline.phone_guided_plan import UnsupportedPhonePlan
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


_TEXT_LANES = ("text_elements", "context_label_text_elements")
# Prod 5df2e3ec (2026-09-25): ``EditRecipeV2`` rejected the compiled recipe with a raw
# Pydantic "text layer exceeds the timeline" that reached the creator verbatim.
_TEXT_PAST_TIMELINE = "text layer exceeds the timeline"
_MIN_TEXT_WINDOW_S = 0.1


def _repair_text_past_timeline(plan) -> tuple[Any, list[str]]:  # noqa: ANN001
    """Clamp text windows that run past the timeline onto it; drop only wholly-late text."""
    total = float(plan.resolved_duration_s)
    clamped = dropped = 0
    lanes: dict[str, list] = {}
    for lane in _TEXT_LANES:
        elements = []
        for element in getattr(plan, lane):
            if element.end_s <= total + 1e-9:
                elements.append(element)
                continue
            if element.start_s > total - _MIN_TEXT_WINDOW_S:
                dropped += 1
                continue
            elements.append(element.model_copy(update={"end_s": total}))
            clamped += 1
        lanes[lane] = elements
    notes: list[str] = []
    if clamped:
        notes.append("Ended a text that ran past the end of your video with the video")
    if dropped:
        notes.append("Left out a text that started after your video ends")
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
    if _TEXT_PAST_TIMELINE in str(exc):
        return _repair_text_past_timeline(plan)
    # "transition_window" and every other reject: no plan-level edit can fix them.
    return plan, []


# --- confirmed on-screen text (KRI-129: the creator's words win) ---------------------
#
# Prod, 2026-10: six approved phone edits were refused at pin time with "This edit is
# missing confirmed on-screen text." (150d30b4, be180d9f: the montage planner wrote its own
# title over the creator's confirmed one, or dropped its emoji). The contract pinned the
# exact words; nothing put them back. ``repair_plan_for_contract`` puts each confirmed text
# the plan lost back on screen BEFORE compile, in the slot the contract names, and says so.
# ``verify_phone_recipe`` stays the authority at pin time: this only removes the refusal
# for a plan that can be made to show the words.

_TITLE_ID = "guided-title"
_TITLE_HOLD_S = 2.2
_TITLE_Y = 0.16
_LABEL_Y = 0.78
_STACK_STEP_Y = 0.09


def _normal(text: object) -> str:
    # Mirrors ``creator_render_contract._normal``: wrapping is layout, not new words.
    return " ".join(unicodedata.normalize("NFC", str(text)).split())


def _quoted(text: str) -> str:
    flat = _normal(text)
    return f"\u201c{flat if len(flat) <= 60 else flat[:57].rstrip() + '...'}\u201d"


def _is_title(element) -> bool:  # noqa: ANN001
    return element.id == _TITLE_ID or element.id.startswith(_TITLE_ID + "::sequence-")


def _visible(plan) -> list:  # noqa: ANN001
    return [
        element
        for lane in (*_TEXT_LANES, "narration_label_text_elements")
        for element in getattr(plan, lane)
        if not element.removed and element.text.strip()
    ]


def _shows(plan, requirement, frame: float) -> bool:  # noqa: ANN001
    """Whether the plan already carries ``requirement``'s exact words where it asks."""
    from app.kria.brief_route import chapter_list  # noqa: PLC0415
    from app.pipeline.sequence_text_evidence import text_matches  # noqa: PLC0415

    rows = [
        {"element_id": e.id, "text": e.text, "start_s": e.start_s, "end_s": e.end_s}
        for e in _visible(plan)
    ]
    matches = text_matches(rows, requirement.text, role="any", tolerance_s=frame)
    if (
        not matches
        and requirement.role == "any"
        and requirement.duration_s is None
        and chapter_list(requirement.text, [row["text"] for row in rows]) is not None
    ):
        # KRI-545, mirrored from ``verify_phone_recipe``: a literal that lists chapter
        # names ("Sabah, Akşam") is drawn as those names, each its own clip label, never as
        # one line. The plan already shows it; putting the list back would override it.
        return True
    total = float(plan.resolved_duration_s)
    if requirement.duration_s is not None:
        wanted = min(requirement.duration_s, total)
        matches = [r for r in matches if r["end_s"] - r["start_s"] + frame >= wanted]
    if requirement.role == "opening":
        matches = [r for r in matches if r["start_s"] <= frame]
    elif requirement.role == "closing":
        matches = [r for r in matches if r["end_s"] >= total - frame]
    elif requirement.role == "clip":
        windows = _clip_windows(plan, requirement)
        matches = [
            r
            for r in matches
            if any(r["start_s"] >= s - frame and r["end_s"] <= e + frame for s, e in windows)
        ]
    return bool(matches)


def _clip_windows(plan, requirement) -> list[tuple[float, float]]:  # noqa: ANN001
    clips = [m for m in plan.story_timeline if m.lane == "clip"]
    if requirement.shot_index is not None:
        clips = clips[requirement.shot_index : requirement.shot_index + 1]
    elif requirement.media_id:
        clips = [m for m in clips if m.media_id == requirement.media_id]
    return [(float(m.output_start_s), float(m.output_end_s)) for m in clips]


def _new_text(plan, template, **updates: Any):  # noqa: ANN001
    from app.agents._schemas.text_element import TextElement  # noqa: PLC0415

    if template is not None:
        # Copy the look, never the identity: no sequence lineage, no case transform (the
        # verifier compares the drawn words), no block or caption binding.
        return template.model_copy(
            update={
                "word_timings": None,
                "animation_phases": None,
                "source_params": None,
                "visual_block_id": None,
                "text_case": None,
                "removed": False,
                **updates,
            }
        )
    return TextElement(
        role="generative_intro",
        position="custom",
        x_frac=0.5,
        font_family=getattr(plan.typography, "font", None) or "Fraunces",
        size_px=64,
        color="#FFF8F0",
        stroke_width=0,
        shadow_enabled=True,
        shadow_style="standard",
        effect="static",
        alignment="center",
        max_width_frac=0.84,
        **updates,
    )


def repair_plan_for_contract(plan, contract, *, frame_s: float = 1 / 30) -> tuple[Any, list[str]]:  # noqa: ANN001
    """Put every confirmed exact text the plan lost back on screen; ``(plan, notes)``.

    Slots, in the contract's own words: an ``opening`` text becomes the title (retexted,
    or added at 0 s); a ``closing`` text is added over the last seconds; a ``clip`` text
    goes on that clip's window (retexting its label when one is there); an ``any`` text
    takes the title slot when the planner filled it with its own copy, otherwise it is
    added over the opening. A text already shown but too briefly or in the wrong place is
    held / moved, never duplicated. Nothing else in the plan changes.
    """
    if contract is None or not contract.exact_texts:
        return plan, []
    total = float(plan.resolved_duration_s)
    required = {_normal(r.text) for r in contract.exact_texts}
    texts = list(plan.text_elements)
    notes: list[str] = []
    added = 0
    current = plan
    seen: set[tuple[str, str, str | None, int | None]] = set()
    for requirement in contract.exact_texts:
        key = (
            _normal(requirement.text),
            requirement.role,
            requirement.media_id,
            requirement.shot_index,
        )
        if key in seen or _shows(current, requirement, frame_s):
            continue
        seen.add(key)
        literal = requirement.text
        hold = min(total, max(requirement.duration_s or 0.0, _TITLE_HOLD_S))
        same = [i for i, e in enumerate(texts) if not e.removed and _normal(e.text) == key[0]]
        title = [i for i, e in enumerate(texts) if _is_title(e) and not e.removed]
        if same and requirement.role in {"opening", "any"}:
            # The words are there, just too briefly or not from the start: hold / move them.
            element = texts[same[0]]
            start = 0.0 if requirement.role == "opening" else element.start_s
            end = min(total, max(element.end_s - element.start_s + start, start + hold))
            texts[same[0]] = element.model_copy(update={"start_s": start, "end_s": end})
            notes.append(f"Kept your text {_quoted(literal)} on screen longer")
        elif (
            requirement.role in {"opening", "any"}
            and title
            and all(_normal(texts[i].text) not in required for i in title)
        ):
            # The planner wrote its own title where the creator's confirmed words belong.
            first = texts[title[0]]
            end = max(max(texts[i].end_s for i in title), hold)
            replacement = _new_text(
                current, first, id=_TITLE_ID, text=literal, start_s=0.0, end_s=min(total, end)
            )
            texts = [e for i, e in enumerate(texts) if i not in title]
            texts.insert(title[0], replacement)
            notes.append(f"Used your title {_quoted(literal)} exactly as you wrote it")
        elif requirement.role == "clip":
            windows = _clip_windows(current, requirement)
            if not windows:
                continue  # no such clip in the plan: the pin-time check stays the authority
            start, end = windows[0]
            label = next(
                (
                    i
                    for i, e in enumerate(texts)
                    if e.id.startswith("clip-label")
                    and not e.removed
                    and e.start_s >= start - frame_s
                    and e.end_s <= end + frame_s
                    and _normal(e.text) not in required
                ),
                None,
            )
            if label is not None:
                texts[label] = texts[label].model_copy(update={"text": literal, "text_case": None})
            else:
                added += 1
                texts.append(
                    _new_text(
                        current,
                        None,
                        id=f"contract-text-{added}",
                        text=literal,
                        start_s=start,
                        end_s=end,
                        y_frac=_LABEL_Y,
                    )
                )
            notes.append(f"Added your text {_quoted(literal)} on its clip")
        else:
            added += 1
            closing = requirement.role == "closing"
            start = total - hold if closing else 0.0
            template = texts[title[0]] if title else None
            texts.append(
                _new_text(
                    current,
                    template,
                    id=f"contract-text-{added}",
                    text=literal,
                    start_s=max(0.0, start),
                    end_s=min(total, start + hold) if not closing else total,
                    y_frac=min(0.7, _TITLE_Y + _STACK_STEP_Y * (added if title else added - 1)),
                )
            )
            notes.append(f"Added your text {_quoted(literal)} on screen")
        current = current.model_copy(update={"text_elements": texts})
    if not notes:
        return plan, []
    return current, list(dict.fromkeys(notes))


def compile_phone_guided_repaired(  # noqa: ANN001
    plan, bindings, *, contract: Any = None, **kwargs: Any
) -> RepairedCompile:
    """Compile + pilot-validate, repairing known rejects (see module docstring).

    ``contract`` (the job's pinned ``CreatorRenderContract``, when it has one) first puts
    back any confirmed exact text the plan lost (``repair_plan_for_contract``). ``kwargs``
    go to ``compile_phone_guided_plan`` verbatim. Raises the compile / validation error
    unchanged when it is not repairable (or a repair did not help).
    """
    allow_editor_media = bool(kwargs.get("allow_editor_media", False))
    current, contract_notes = repair_plan_for_contract(plan, contract)
    if contract_notes:
        log.info("phone_plan_contract_repaired", notes=contract_notes)
    notes: list[str] = list(contract_notes)
    attempted: set[str] = set()
    for _ in range(_MAX_REPAIR_PASSES + 1):
        try:
            # Looked up on the module at call time so a patched compiler is honoured.
            recipe = phone_guided_plan.compile_phone_guided_plan(current, bindings, **kwargs)
            validate_phone_pilot_recipe(recipe, allow_editor_media=allow_editor_media)
            return RepairedCompile(recipe, notes, current)
        except (UnsupportedPhonePlan, ValueError) as exc:
            # Keyed on the reject's payload, not just its type: a second pass may
            # legitimately hit the same exception class for a different cause.
            kind = repr(
                (
                    type(exc).__name__,
                    getattr(exc, "reason", None),
                    sorted(getattr(exc, "font_files", ())),
                    sorted(getattr(exc, "effects", ())),
                )
            )
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


def validate_proposal_phone_compiles(snapshot, assignments, **compile_kwargs: Any) -> list[str]:  # noqa: ANN001
    """Dry-run the phone compiler over a freshly planned proposal (KRI-286).

    compile_execution_plan -> bind_phone_sources -> compile_phone_guided_plan ->
    validate_phone_pilot_recipe, with the deterministic repairs applied. Returns
    the repair notes (``[]`` when the plan is already expressible, or when it
    cannot be dry-run: the dispatch gate stays the authority there). Raises
    ``PhoneProposalRejected`` for a plan the phone cannot render and no repair
    fixes; the caller falls back to the deterministic plan. This is an advisory
    planning-time check: ONLY ``PhoneProposalRejected`` ever escapes; an
    unverified device capability (a rollout decision the dispatch gate and the
    worker own) and any unexpected error are logged and return ``[]``.

    ``compile_kwargs`` (e.g. ``landscape_fit``) go to ``compile_phone_guided_plan``
    verbatim so the dry run matches the worker's call.

    The repairs are NOT persisted on the proposal: approval recompiles from the
    snapshot and the worker re-applies the same deterministic repairs
    (``compile_phone_guided_with_repairs``). The notes just tell the creator.
    """
    try:
        return _validate_proposal_phone_compiles(snapshot, assignments, compile_kwargs)
    except PhoneProposalRejected:
        raise
    except PhoneCapabilityUnavailable as exc:
        log.info("phone_plan_dry_run_capability_unavailable", capability=exc.capability)
        return []
    except Exception as exc:  # noqa: BLE001 - an advisory check must never fail a proposal
        log.warning("phone_plan_dry_run_errored", error=str(exc)[:300], exc_info=True)
        return []


def _validate_proposal_phone_compiles(snapshot, assignments, compile_kwargs) -> list[str]:  # noqa: ANN001
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
            plan, bindings, visuals=visuals, narration=narration, **compile_kwargs
        )
    except PhoneCapabilityUnavailable:
        raise
    except (UnsupportedPhonePlan, ValueError) as exc:
        raise PhoneProposalRejected(str(exc)) from exc
    return notes
