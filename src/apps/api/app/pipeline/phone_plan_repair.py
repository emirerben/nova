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
  confirmed text with no clear spot at its size  -> shrunk until one is clear, never below
                                                    a readable size or a size the creator chose

A reject no repair fixes and that names one row the phone cannot draw (a look, a zoom, a
transition after the last clip, a text animation) degrades that row instead
(``phone_plan_degrade``, KRI-1376).

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

from app.kria.reply_language import say
from app.pipeline import phone_guided_plan, phone_plan_degrade
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
# Degrades for at most this many distinct reject reasons (`phone_plan_degrade`), each row once
# per reason: a look every clip carries degrades clip by clip, a fourth kind of reject refuses.
_MAX_DEGRADE_REASONS = 3


class PhoneProposalRejected(ValueError):
    """The planning-time dry run found a phone-inexpressible plan it cannot repair."""


class RepairedCompile(NamedTuple):
    recipe: Any
    notes: list[str]
    # The plan actually compiled: the input object itself when nothing was repaired.
    plan: Any
    # The rows degraded or shrunk; the worker records them once it keeps this compile.
    recoveries: tuple[phone_plan_degrade.Recovery, ...] = ()


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
        [_swapped_animation_note()],
    )


def _swapped_animation_note() -> str:
    return say(
        en="Swapped a text animation your iPhone can't play yet for a simple fade-in",
        tr="iPhone'unun henüz oynatamadığı bir yazı animasyonunu basit bir belirmeyle değiştirdim",
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
        notes.append(
            say(
                en="Changed a text font to {font} so it renders on your iPhone",
                tr="Bir yazının yazı tipini iPhone'unda görünsün diye {font} yaptım",
                font=default_family,
            )
        )
    if reanimated:
        notes.append(_swapped_animation_note())
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
        notes.append(
            say(
                en="Ended a text that ran past the end of your video with the video",
                tr="Videonun sonunu aşan bir yazıyı videoyla birlikte bitirdim",
            )
        )
    if dropped:
        notes.append(
            say(
                en="Left out a text that started after your video ends",
                tr="Videon bittikten sonra başlayan bir yazıyı çıkardım",
            )
        )
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

# The contract text roles ``repair_plan_for_contract`` always puts on screen. A ``clip``
# text lands only when its clip is in the compiled plan, so only the plan can say.
PLACED_WITHOUT_PLAN = frozenset({"opening", "closing", "any"})
# The phone routes whose writer compiles through ``compile_phone_guided_repaired`` with the
# job's pinned contract (``generative_build._run_phone_guided_job``).
CONTRACT_REPAIR_ROUTES = frozenset(
    {"guided_story", "unified_montage", "user_song_montage", "lipsync_montage"}
)


def contract_text_drawn(requirement, *, route: str | None) -> bool:  # noqa: ANN001
    """Whether the render shows this pinned contract text whatever its plan holds (KRI-945).

    The draft check before approval (``approval_readiness``) asks this; the repair below is
    what makes it true, so a writer that cannot draw contract text is never promised it.
    """
    return route in CONTRACT_REPAIR_ROUTES and requirement.role in PLACED_WITHOUT_PLAN


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


def _shown_rows(plan, requirement, frame: float) -> list[dict[str, Any]]:  # noqa: ANN001
    """The plan's texts that already carry ``requirement``'s exact words where it asks."""
    from app.pipeline.sequence_text_evidence import text_matches  # noqa: PLC0415
    from app.services.creator_render_contract import chapter_text_matches  # noqa: PLC0415

    rows = [
        {"element_id": e.id, "text": e.text, "start_s": e.start_s, "end_s": e.end_s}
        for e in _visible(plan)
    ]
    matches = text_matches(rows, requirement.text, role="any", tolerance_s=frame)
    total = float(plan.resolved_duration_s)
    if requirement.duration_s is not None:
        wanted = min(requirement.duration_s, total)
        matches = [r for r in matches if r["end_s"] - r["start_s"] + frame >= wanted]
    if requirement.role == "opening":
        matches = [r for r in matches if r["start_s"] <= frame]
    elif requirement.role == "closing":
        matches = [r for r in matches if r["end_s"] >= total - frame]
    elif requirement.role == "clip" and requirement.chapter_media_ids:
        matches = chapter_text_matches(
            matches,
            members=frozenset(requirement.chapter_media_ids),
            spans=_clip_spans(plan),
            tolerance_s=frame,
        )
    elif requirement.role == "clip":
        windows = _clip_windows(plan, requirement)
        matches = [
            r
            for r in matches
            if any(r["start_s"] >= s - frame and r["end_s"] <= e + frame for s, e in windows)
        ]
    return matches


def _shows(plan, requirement, frame: float) -> bool:  # noqa: ANN001
    """Whether the plan already carries ``requirement``'s exact words where it asks."""
    from app.kria.brief_route import chapter_list  # noqa: PLC0415

    if _shown_rows(plan, requirement, frame):
        return True
    # KRI-545, mirrored from ``verify_phone_recipe``: a literal that lists chapter names
    # ("Sabah, Akşam") is drawn as those names, each its own clip label, never as one line.
    # The plan already shows it; putting the list back would override it.
    return (
        requirement.role == "any"
        and requirement.duration_s is None
        and chapter_list(requirement.text, [e.text for e in _visible(plan)]) is not None
    )


def _clip_spans(plan) -> list[tuple[str, float, float]]:  # noqa: ANN001
    return [
        (m.media_id, float(m.output_start_s), float(m.output_end_s))
        for m in plan.story_timeline
        if m.lane == "clip"
    ]


def _candidate_clips(plan, requirement) -> list:  # noqa: ANN001
    """The timeline clips a clip text may sit on."""
    clips = [m for m in plan.story_timeline if m.lane == "clip"]
    if requirement.shot_index is not None:
        return clips[requirement.shot_index : requirement.shot_index + 1]
    if requirement.chapter_media_ids:
        return [m for m in clips if m.media_id in requirement.chapter_media_ids]
    if requirement.media_id:
        return [m for m in clips if m.media_id == requirement.media_id]
    return clips


def _clip_windows(plan, requirement) -> list[tuple[float, float]]:  # noqa: ANN001
    return [
        (float(m.output_start_s), float(m.output_end_s))
        for m in _candidate_clips(plan, requirement)
    ]


def _clip_anchor(plan, requirement, frame: float) -> tuple[float, float, float] | None:  # noqa: ANN001
    """``(start, end, run_end)`` of the clip a clip text goes on, or ``None``.

    A chapter title goes on the chapter's first clip in the timeline and may run on over
    the member clips straight after it (``run_end``); any other clip text stays on its clip.
    """
    from app.services.creator_render_contract import chapter_window  # noqa: PLC0415

    if requirement.chapter_media_ids:
        return chapter_window(
            frozenset(requirement.chapter_media_ids), _clip_spans(plan), tolerance_s=frame
        )
    windows = _clip_windows(plan, requirement)
    if not windows:
        return None
    start, end = windows[0]
    return start, end, end


def _claimed_ids(plan, contract, frame: float) -> set[str]:  # noqa: ANN001
    """Texts that already show some pinned requirement: never moved to show another."""
    return {
        str(row.get("element_id"))
        for requirement in contract.exact_texts
        for row in _shown_rows(plan, requirement, frame)
    }


def _own_text(element) -> bool:  # noqa: ANN001
    """A text of its own: never a caption-lane word, a lyric or one piece of a sequence."""
    from app.pipeline.sequence_text_evidence import sequence_lineage  # noqa: PLC0415
    from app.services.kria_editor_ops import is_caption_text_bar  # noqa: PLC0415

    row = {"id": element.id, "source_params": element.source_params}
    return (
        element.role != "lyric_line"
        and not is_caption_text_bar(row)
        and sequence_lineage(row) is None
        and not (element.source_params or {}).get("sequence_source_id")
    )


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


def _shrink_sizes(element) -> list[float | None]:  # noqa: ANN001
    """The sizes a confirmed text may take to fit, largest first: its own, then smaller.

    Never one the creator chose (``creator_style_fields``), never below the smallest size
    the text style pass calls readable.
    """
    from app.pipeline.phone_text_style import _READABLE_FLOOR_PX  # noqa: PLC0415

    size = element.size_px
    chosen = "size_px" in ((element.source_params or {}).get("creator_style_fields") or ())
    if size is None or chosen:
        return [size]
    return [size, *(float(px) for px in range(int(size) - 2, _READABLE_FLOOR_PX - 1, -2))]


def _place_added_contract_text(plan, element):  # noqa: ANN001
    """Reserve measured ink space without changing confirmed copy or time.

    The text keeps its size when a clear spot exists; otherwise it shrinks (``_shrink_sizes``)
    until one does. Only when no readable size fits is the creator asked to choose.
    """
    from app.pipeline.canvas import canvas_for_orientation  # noqa: PLC0415
    from app.pipeline.generative_overlays import build_overlays_from_text_elements  # noqa: PLC0415
    from app.pipeline.phone_text_style import animation_box  # noqa: PLC0415
    from app.pipeline.text_overlay_skia import measure_text_overlay_box  # noqa: PLC0415
    from app.services.creator_render_contract import CreatorRenderContractError  # noqa: PLC0415

    canvas = canvas_for_orientation(getattr(plan, "output_orientation", None))

    def ink(text):  # noqa: ANN001
        overlays = build_overlays_from_text_elements(
            [text],
            video_duration_s=float(text.end_s),
            independent_box_alignment=True,
            canvas=canvas,
        )
        boxes = [
            measure_text_overlay_box(overlay, render_canvas=canvas, resolved_size=True)
            for overlay in overlays
        ]
        bounds = (
            min(box["left"] for box in boxes),
            min(box["top"] for box in boxes),
            max(box["right"] for box in boxes),
            max(box["bottom"] for box in boxes),
        )
        return animation_box(text.model_dump(mode="json"), bounds, canvas)

    element = element.model_copy(update={"position": "custom"})
    concurrent = [
        ink(other)
        for other in _visible(plan)
        if other.start_s < element.end_s and other.end_s > element.start_s
    ]
    origin = float(element.y_frac if element.y_frac is not None else _TITLE_Y)
    gap = 24 / canvas.height
    for size in _shrink_sizes(element):
        sized = element.model_copy(update={"size_px": size})
        box = ink(sized)
        obstacles = [other for other in concurrent if other[0] < box[2] and box[0] < other[2]]
        candidates = [origin]
        candidates += sorted(origin + other[3] + gap - box[1] for other in obstacles)
        candidates += sorted(
            (origin + other[1] - gap - box[3] for other in obstacles), reverse=True
        )
        candidates += [origin + 0.05 - box[1], origin + 0.90 - box[3]]
        for y in candidates:
            top, bottom = box[1] + y - origin, box[3] + y - origin
            if box[0] < 0 or box[2] > 1 or top < 0.05 or bottom > 0.90:
                continue
            if any(top < other[3] and other[1] < bottom for other in obstacles):
                continue
            return sized.model_copy(update={"y_frac": round(y, 6)})
    raise CreatorRenderContractError(
        "Your confirmed text cannot fit clearly alongside the other text at this size.",
        decline_reason="needs_choice",
        field_path="text_elements",
        alternative="Choose a smaller text size or a separate time window.",
        missing_text=element.text,
    )


def _added_on_clip_note(literal: str) -> str:
    return say(
        en="Added your text {text} on its clip",
        tr="{text} yazını kendi klibine ekledim",
        text=_quoted(literal),
    )


def _shrunk_notes(before, after, recoveries: list | None) -> list[str]:  # noqa: ANN001
    if after.size_px == before.size_px:
        return []
    if recoveries is not None:
        recoveries.append(
            phone_plan_degrade.Recovery(
                reason="contract_text_fit", lane="text", item_id=after.id, action="shrunk"
            )
        )
    return [
        say(
            en="Made your text {text} smaller so it fits clearly next to the other text",
            tr="{text} yazını diğer yazıların yanına net sığsın diye küçülttüm",
            text=_quoted(after.text),
        )
    ]


def repair_plan_for_contract(  # noqa: ANN001
    plan, contract, *, frame_s: float = 1 / 30, recoveries: list | None = None
) -> tuple[Any, list[str]]:
    """Put every confirmed exact text the plan lost back on screen; ``(plan, notes)``.

    ``recoveries``, when given, gets one ``phone_plan_degrade.Recovery`` per text shrunk to fit.

    Slots, in the contract's own words: an ``opening`` text becomes the title (retexted,
    or added at 0 s); a ``closing`` text is added over the last seconds; a ``clip`` text
    goes on that clip's window, a chapter title on the chapter's first clip (moving the same
    words from a clip where they show nothing pinned, else retexting its label, else added);
    an ``any`` text takes the title slot when the planner filled it with its own copy,
    otherwise it is added over the opening. A text already shown but too briefly or in the
    wrong place is held / moved, never duplicated, and a moved or added text is placed clear
    of the text around it (or refused when it cannot fit). Nothing else in the plan changes.
    """
    if contract is None or not contract.exact_texts:
        return plan, []
    total = float(plan.resolved_duration_s)
    required = {_normal(r.text) for r in contract.exact_texts}
    texts = list(plan.text_elements)
    notes: list[str] = []
    added = 0
    current = plan
    # Clip texts moved or added below: placed clear of other text once every one has its time.
    placed: list[str] = []
    seen: set[tuple[str, str, str | None, int | None, tuple[str, ...]]] = set()
    for requirement in contract.exact_texts:
        key = (
            _normal(requirement.text),
            requirement.role,
            requirement.media_id,
            requirement.shot_index,
            requirement.chapter_media_ids,
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
            notes.append(
                say(
                    en="Kept your text {text} on screen longer",
                    tr="{text} yazını ekranda daha uzun tuttum",
                    text=_quoted(literal),
                )
            )
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
            notes.append(
                say(
                    en="Used your title {text} exactly as you wrote it",
                    tr="{text} başlığını tam yazdığın gibi kullandım",
                    text=_quoted(literal),
                )
            )
        elif requirement.role not in PLACED_WITHOUT_PLAN:
            anchor = _clip_anchor(current, requirement, frame_s)
            if anchor is None:
                continue  # no such clip in the plan: the pin-time check stays the authority
            start, end, run_end = anchor
            claimed = _claimed_ids(current, contract, frame_s)
            stray = next(
                (i for i in same if texts[i].id not in claimed and _own_text(texts[i])), None
            )
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
            if stray is not None:
                # KRI-1331: the words are already drawn on another clip (the text composer's
                # guess): move that text here, never draw them a second time.
                element = texts[stray]
                texts[stray] = element.model_copy(
                    update={
                        "start_s": start,
                        "end_s": min(run_end, start + element.end_s - element.start_s),
                    }
                )
                placed.append(texts[stray].id)
                notes.append(
                    say(
                        en="Moved your text {text} onto its clip",
                        tr="{text} yazını kendi klibine taşıdım",
                        text=_quoted(literal),
                    )
                )
            elif label is not None:
                texts[label] = texts[label].model_copy(update={"text": literal, "text_case": None})
                notes.append(_added_on_clip_note(literal))
            else:
                added += 1
                texts.append(
                    _new_text(
                        current,
                        None,
                        id=f"contract-text-{added}",
                        text=literal,
                        start_s=start,
                        # A chapter's title stays up long enough to read over a short first clip.
                        end_s=min(run_end, max(end, start + _TITLE_HOLD_S)),
                        y_frac=_LABEL_Y,
                    )
                )
                placed.append(texts[-1].id)
                notes.append(_added_on_clip_note(literal))
        else:
            added += 1
            closing = requirement.role == "closing"
            start = total - hold if closing else 0.0
            template = texts[title[0]] if title else None
            element = _new_text(
                current,
                template,
                id=f"contract-text-{added}",
                text=literal,
                start_s=max(0.0, start),
                end_s=min(total, start + hold) if not closing else total,
                y_frac=min(0.7, _TITLE_Y + _STACK_STEP_Y * (added if title else added - 1)),
            )
            texts.append(_place_added_contract_text(current, element))
            notes.append(
                say(
                    en="Added your text {text} on screen",
                    tr="{text} yazını ekrana ekledim",
                    text=_quoted(literal),
                )
            )
            notes.extend(_shrunk_notes(element, texts[-1], recoveries))
        current = current.model_copy(update={"text_elements": texts})
    for done, element_id in enumerate(placed):
        index = next(i for i, e in enumerate(texts) if e.id == element_id)
        waiting = set(placed[done:])
        others = [e for e in texts if e.id not in waiting]
        before = texts[index]
        texts[index] = _place_added_contract_text(
            current.model_copy(update={"text_elements": others}), before
        )
        notes.extend(_shrunk_notes(before, texts[index], recoveries))
    current = current.model_copy(update={"text_elements": list(texts)})
    linked = _link_clip_texts(current, contract, frame_s)
    if not notes and linked is current:
        return plan, []
    return linked, list(dict.fromkeys(notes))


def _link_clip_texts(plan, contract, frame: float):  # noqa: ANN001, ANN202
    """KRI-1331: every text that shows a pinned clip text records that clip, as data.

    ``source_params.source_asset_id`` (the clip's media id) and ``source_timeline_id`` (its
    ``story_timeline`` moment) name the clip under the text's start, so an edit to the
    timeline can keep the text with its clip.
    """
    texts = list(plan.text_elements)
    changed = False
    for requirement in contract.exact_texts:
        if requirement.role != "clip":
            continue
        clips = _candidate_clips(plan, requirement)
        for row in _shown_rows(plan, requirement, frame):
            index = next((i for i, e in enumerate(texts) if e.id == row["element_id"]), None)
            clip = next(
                (
                    m
                    for m in clips
                    if float(m.output_start_s) - frame <= row["start_s"] < float(m.output_end_s)
                ),
                None,
            )
            if index is None or clip is None or not _own_text(texts[index]):
                continue
            params = texts[index].source_params or {}
            link = {"source_timeline_id": clip.moment_id, "source_asset_id": clip.media_id}
            if any(params.get(key) != value for key, value in link.items()):
                texts[index] = texts[index].model_copy(update={"source_params": {**params, **link}})
                changed = True
    return plan.model_copy(update={"text_elements": texts}) if changed else plan


def compile_phone_guided_repaired(  # noqa: ANN001
    plan, bindings, *, contract: Any = None, **kwargs: Any
) -> RepairedCompile:
    """Compile + pilot-validate, repairing known rejects (see module docstring).

    ``contract`` (the job's pinned ``CreatorRenderContract``, when it has one) first puts
    back any confirmed exact text the plan lost (``repair_plan_for_contract``). ``kwargs``
    go to ``compile_phone_guided_plan`` verbatim. A reject no repair fixes may degrade the
    one row it names (``phone_plan_degrade``). Raises the compile / validation error
    unchanged when neither helps, or when the passes run out.

    Every pass either raises or tries a reject it has not tried before; repairs are capped
    at ``_MAX_REPAIR_PASSES`` and degrades at ``_MAX_DEGRADE_REASONS`` reasons times the
    plan's rows, so a stubborn plan always ends in its typed error.
    """
    allow_editor_media = bool(kwargs.get("allow_editor_media", False))
    shrunk: list[phone_plan_degrade.Recovery] = []
    current, contract_notes = repair_plan_for_contract(plan, contract, recoveries=shrunk)
    if contract_notes:
        log.info("phone_plan_contract_repaired", notes=contract_notes)
    notes: list[str] = list(contract_notes)
    attempted: set[str] = set()
    repairs = 0
    degraded: list[phone_plan_degrade.Recovery] = []
    while True:
        try:
            # Looked up on the module at call time so a patched compiler is honoured.
            recipe = phone_guided_plan.compile_phone_guided_plan(current, bindings, **kwargs)
            validate_phone_pilot_recipe(recipe, allow_editor_media=allow_editor_media)
            degrade_notes = phone_plan_degrade.degrade_notes(degraded)
            return RepairedCompile(
                recipe,
                [*notes, *(note for note in degrade_notes if note not in notes)],
                current,
                (*shrunk, *degraded),
            )
        except (UnsupportedPhonePlan, ValueError) as exc:
            # Keyed on the reject's payload, not just its type: a second pass may
            # legitimately hit the same exception class for a different cause.
            kind = repr(
                (
                    type(exc).__name__,
                    getattr(exc, "reason", None),
                    getattr(exc, "item_id", None),
                    sorted(getattr(exc, "font_files", ())),
                    sorted(getattr(exc, "effects", ())),
                )
            )
            if kind in attempted:
                raise
            attempted.add(kind)
            if repairs < _MAX_REPAIR_PASSES:
                repaired, repair_notes = repair_plan_for_phone(current, exc)
                if repair_notes:
                    repairs += 1
                    log.info("phone_plan_repaired", error=str(exc), notes=repair_notes)
                    current = repaired
                    notes.extend(note for note in repair_notes if note not in notes)
                    continue
            reasons = {recovery.reason for recovery in degraded}
            if getattr(exc, "reason", None) not in reasons and len(reasons) >= _MAX_DEGRADE_REASONS:
                raise
            repaired, recoveries = phone_plan_degrade.degrade_plan_for_phone(current, exc)
            if not recoveries:
                raise
            log.info("phone_plan_degraded", error=str(exc), recoveries=recoveries)
            current = repaired
            degraded.extend(recoveries)


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
    validate_phone_pilot_recipe, with the deterministic repairs and degrades applied.
    Returns their notes (``[]`` when the plan is already expressible, or when it
    cannot be dry-run: the dispatch gate stays the authority there). A row the
    worker would degrade (a look, a zoom, a transition after the last clip) is a
    note here too, so the creator approves the model's draft with that note on the
    card instead of the deterministic fallback (KRI-1376). Raises
    ``PhoneProposalRejected`` for a plan the phone cannot render and neither a
    repair nor a degrade fixes; the caller falls back to the deterministic plan. This is an advisory
    planning-time check: ONLY ``PhoneProposalRejected`` ever escapes; an
    unverified device capability (a rollout decision the dispatch gate and the
    worker own) and any unexpected error are logged and return ``[]``.

    ``compile_kwargs`` (e.g. ``landscape_fit``) go to ``compile_phone_guided_plan``
    verbatim so the dry run matches the worker's call.

    The repairs and degrades are NOT persisted on the proposal: approval recompiles
    from the snapshot and the worker re-applies the same deterministic ones
    (``compile_phone_guided_repaired``). The notes just tell the creator.
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
