"""Keeping every text on a phone Talking edit off what the creator named (KRI-827).

One entry point for the worker (`generative_build`) and the E2E harness
(`app.cli.verify_protected_text`), so both run the same decisions:

1. `resolve_protected_plan` reads the targets from the approved binding (the brief's
   ``keep_text_off`` facts and the creator's own marks, `app.kria.protected_targets`),
   matches each object to the creator's mark, checks
   the mark belongs to THIS source at THIS generation (a swapped or re-uploaded source
   never reuses the old mark), and tracks it over the speaker's kept span.
2. The title / closing / spoken-text rows are placed with the tracks as hard limits
   (`phone_subtitled_title.place_protected_title`).
3. `choose_caption_look` moves the caption block when a caption line would sit on the
   object (or a face) while it is shown.
4. `protected_text_receipt` says, per target, ``clear`` / ``collision`` / ``unverified`` /
   ``no_selection`` / ``invalidated`` -- only ``clear`` when every text was measured clear of
   an object the tracker actually saw for every sample of every row's window.

Nothing here runs without such an ask or mark: the default render is unchanged.
"""

from __future__ import annotations

import copy
import dataclasses
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

import structlog

from app.kria.protected_targets import (
    ProtectedTarget,
    binding_protected_regions,
    protected_targets,
    selection_for,
)
from app.pipeline.object_track import (
    ObjectTrack,
    object_canvas_boxes,
    object_collision,
    track_object,
)
from app.pipeline.render_geometry import NormalizedBox

log = structlog.get_logger(__name__)

_MAX_COVERAGE = 0.05
# Caption block centres tried when the default band meets the object: a little lower first
# (the object is usually on the table below the speaker), then upward, then the top third.
# All inside the 0.10-0.90 band a saved variant's caption position may hold
# (`caption_look_from_variant`), so the editor reopens the captions where they were drawn.
_CAPTION_Y_CANDIDATES = (0.86, 0.90, 0.72, 0.64, 0.56, 0.32, 0.24)
_STATUS_RANK = {
    "clear": 0,
    "unverified": 1,
    "no_selection": 2,
    "invalidated": 2,
    "other_source": 2,
    "collision": 3,
}

Tracker = Callable[..., ObjectTrack]


class _Canvas(Protocol):
    @property
    def width(self) -> int: ...

    @property
    def height(self) -> int: ...


@dataclass(frozen=True)
class ProtectedGeometry:
    """Where the speaker clip is drawn (cut, crop, scale) and every face seen over the kept
    span, for re-checking rows a later stage restyles (`keep_styled_rows_clear`)."""

    keep_segments: tuple[tuple[float, float], ...]
    display_width: float
    display_height: float
    scale: float
    position_x: float
    faces: tuple[NormalizedBox, ...] = ()


@dataclass(frozen=True)
class ProtectedPlan:
    targets: tuple[ProtectedTarget, ...]
    objects: tuple[ObjectTrack, ...] = ()
    target_rows: tuple[dict[str, Any], ...] = ()
    protect_all_faces: bool = False
    geometry: ProtectedGeometry | None = None


def resolve_protected_plan(
    raw_binding: Mapping[str, Any] | None,
    *,
    media_id: str,
    generation: str | None,
    clip_path: str | None,
    source_span: tuple[float, float],
    tracker: Tracker | None = None,
    timeout_s: float | None = None,
) -> ProtectedPlan | None:
    """The targets and tracks for one speaker clip, or ``None`` when the creator named
    nothing to keep text off (or the binding is unreadable: nothing is claimed)."""
    if not isinstance(raw_binding, Mapping):
        return None
    from app.kria.brief_binding import BriefBinding  # noqa: PLC0415

    try:
        binding = BriefBinding.model_validate(dict(raw_binding))
        brief = binding.resolve()
    except Exception as exc:  # noqa: BLE001 - an unreadable binding is "no ask"
        log.warning("protected_text.binding_unreadable", error=str(exc)[:200])
        return None
    selections = binding_protected_regions(raw_binding)
    targets = protected_targets(brief, selections)
    if not targets:
        return None
    objects: list[ObjectTrack] = []
    rows: list[dict[str, Any]] = []
    for target in targets:
        row: dict[str, Any] = {
            "kind": target.kind,
            "label": target.label,
            "requirement_ids": list(target.requirement_ids),
        }
        if target.kind == "face":
            rows.append({**row, "grounding": "face_sampler"})
            continue
        mark = selection_for(target, selections, media_id=media_id)
        if mark is None:
            rows.append({**row, "status": "no_selection"})
            continue
        row["selection"] = mark.model_dump(mode="json")
        if mark.media_id != media_id:
            rows.append({**row, "status": "other_source"})
            continue
        if generation and mark.generation != generation:
            rows.append({**row, "status": "invalidated", "current_generation": generation})
            continue
        if clip_path is None:
            rows.append({**row, "status": "unverified", "reason": "no_clip"})
            continue
        start_s, end_s = source_span
        track = (tracker or track_object)(
            clip_path,
            label=target.label,
            media_id=mark.media_id,
            generation=generation,
            seed_t_s=mark.source_t_s,
            seed_box=NormalizedBox(*mark.box),
            start_s=start_s,
            end_s=end_s,
            timeout_s=timeout_s,
        )
        objects.append(track)
        rows.append(
            {
                **row,
                "grounding": "creator_mark_tracked",
                "track_status": track.status,
                "track": track.receipt,
                "provenance": track.provenance(),
            }
        )
    return ProtectedPlan(
        targets=targets,
        objects=tuple(objects),
        target_rows=tuple(rows),
        protect_all_faces=any(t.kind == "face" for t in targets),
    )


def choose_caption_look(
    cues: Sequence[dict],
    *,
    caption_style: str,
    canvas: _Canvas,
    objects: Sequence[ObjectTrack],
    faces: Sequence[NormalizedBox],
    keep_segments: Sequence[tuple[float, float]],
    display_width: float,
    display_height: float,
    scale: float = 1.0,
    position_x: float = 0.0,
) -> tuple[Any, dict[str, Any]]:
    """The caption look whose every line is clear of the objects (and faces) while shown.

    ``None`` keeps the default look (already clear, or nothing clears and the receipt says
    ``object_collision``)."""
    from app.pipeline import text_overlay_skia as cloud  # noqa: PLC0415
    from app.pipeline.phone_captions import (  # noqa: PLC0415
        CAPTION_FONT_FAMILY,
        PhoneCaptionLook,
        caption_ink_box,
        compile_caption_layers,
    )

    if not cues or (not objects and not faces):
        return None, {"status": "skipped"}
    typeface = cloud._resolve_typeface_for_overlay({"font_family": CAPTION_FONT_FAMILY}).typeface
    default = PhoneCaptionLook()
    evaluated: list[dict[str, Any]] = []
    verified = True
    for y_frac in (default.position_y_frac, *_CAPTION_Y_CANDIDATES):
        look = dataclasses.replace(default, position_y_frac=y_frac)
        layers = compile_caption_layers(
            [dict(cue) for cue in cues],
            canvas_width=canvas.width,
            canvas_height=canvas.height,
            style=caption_style,
            look=look,
        )
        worst_object = worst_face = 0.0
        colliding = object_lines = face_lines = 0
        for layer in layers:
            inks = [b for run in layer.runs if (b := caption_ink_box(run, typeface)) is not None]
            if not inks:
                continue
            box = NormalizedBox(
                min(b[0] for b in inks) / canvas.width,
                min(b[1] for b in inks) / canvas.height,
                max(b[2] for b in inks) / canvas.width,
                max(b[3] for b in inks) / canvas.height,
            )
            face = max((box.coverage_by(f) for f in faces), default=0.0)
            obj = 0.0
            for track in objects:
                found, receipt = object_canvas_boxes(
                    track,
                    window=(float(layer.start), float(layer.end)),
                    keep_segments=keep_segments,
                    display_width=display_width,
                    display_height=display_height,
                    canvas=canvas,
                    scale=scale,
                    position_x=position_x,
                )
                if receipt["status"] != "seen" and found:
                    verified = False
                obj = max([obj, *(object_collision(box, o) for o in found)])
            worst_object, worst_face = max(worst_object, obj), max(worst_face, face)
            object_lines += obj > _MAX_COVERAGE
            face_lines += face > _MAX_COVERAGE
            if obj > _MAX_COVERAGE or face > _MAX_COVERAGE:
                colliding += 1
        evaluated.append(
            {
                "position_y_frac": y_frac,
                "colliding_lines": colliding,
                "object_lines": object_lines,
                "face_lines": face_lines,
                "object_coverage": round(worst_object, 5),
                "face_coverage": round(worst_face, 5),
            }
        )
        if colliding == 0:
            moved = abs(y_frac - default.position_y_frac) > 1e-6
            return (look if moved else None), {
                "status": "moved" if moved else "preset",
                "position_y_frac": y_frac,
                "object_collision": False,
                "face_collision": False,
                "objects_verified": verified and all(t.status == "tracked" for t in objects),
                "evaluated": evaluated,
            }
    # Nothing clears: the captions stay at the default look, and the receipt says which of
    # the object / the face they still cover there (never one standing in for the other).
    at_default = evaluated[0] if evaluated else {}
    return None, {
        "status": "best_effort",
        "position_y_frac": default.position_y_frac,
        "object_collision": bool(objects) and at_default.get("object_lines", 1) > 0,
        "face_collision": at_default.get("face_lines", 1) > 0,
        "objects_verified": False,
        "evaluated": evaluated,
    }


def _face_status(placement: Mapping[str, Any]) -> str:
    """A face target is ``clear`` only where the placement LOOKED at the faces (the sampler
    detected them) and kept the text off; a fallback box, a sampler failure or timeout, a
    placement error is ``unverified`` -- best effort is never labelled verified."""
    if placement.get("face_collision") is True:
        return "collision"
    if placement.get("status") == "skipped":
        return "clear"
    if placement.get("status") == "error" or placement.get("faces") != "detected":
        return "unverified"
    return "clear"


def _placement_status(placement: Mapping[str, Any] | None) -> str:
    if not isinstance(placement, Mapping):
        return "clear"
    if placement.get("object_collision") is True or placement.get("status") == "error":
        return "collision"
    if placement.get("objects_verified") is False:
        return "unverified"
    return "clear"


def protected_text_receipt(
    plan: ProtectedPlan, placements: Mapping[str, Mapping[str, Any] | None]
) -> dict[str, Any]:
    """Per target, whether every text the render shows stayed off it.

    ``placements`` maps a text kind ("title", "closing", "spoken:<id>", "captions") to its
    placement receipt."""
    texts = {name: _placement_status(p) for name, p in placements.items() if p is not None}
    face_texts = {name: _face_status(p) for name, p in placements.items() if isinstance(p, Mapping)}
    # Text on text (a moved caption block meeting the title) is no target's verdict, but it
    # is never hidden either.
    overlaps = sorted(
        name
        for name, p in placements.items()
        if isinstance(p, Mapping) and p.get("text_overlap") is True
    )
    targets: list[dict[str, Any]] = []
    for row in plan.target_rows:
        entry = dict(row)
        if row["kind"] == "face":
            entry["status"] = max(
                face_texts.values(), key=lambda s: _STATUS_RANK[s], default="clear"
            )
        elif "status" not in row:
            worst = max(texts.values(), key=lambda s: _STATUS_RANK[s], default="clear")
            if row.get("track_status") != "tracked" and worst == "clear":
                worst = "unverified"
            entry["status"] = worst
        targets.append(entry)
    verdict = max(
        (t["status"] for t in targets), key=lambda s: _STATUS_RANK.get(s, 3), default="clear"
    )
    return {
        "version": 1,
        "verdict": verdict,
        "targets": targets,
        "texts": texts,
        "face_texts": face_texts,
        "text_overlaps": overlaps,
    }


def _row_coverage(row: Mapping[str, Any], plan: ProtectedPlan, canvas: _Canvas) -> float:
    """How much of a face or a marked object ``row``'s drawn ink (its font, size, spot and
    motion as styled) covers at worst over its whole window."""
    from app.pipeline.phone_text_style import animation_box, measure_box  # noqa: PLC0415

    geometry = plan.geometry
    assert geometry is not None
    probe = copy.deepcopy(dict(row))
    box = NormalizedBox(*animation_box(probe, measure_box(probe, canvas), canvas))  # type: ignore[arg-type]
    worst = max((box.coverage_by(face) for face in geometry.faces), default=0.0)
    for track in plan.objects:
        found, _ = object_canvas_boxes(
            track,
            window=(float(row["start_s"]), float(row["end_s"])),
            keep_segments=geometry.keep_segments,
            display_width=geometry.display_width,
            display_height=geometry.display_height,
            canvas=canvas,
            scale=geometry.scale,
            position_x=geometry.position_x,
        )
        worst = max([worst, *(object_collision(box, o) for o in found)])
    return worst


def keep_styled_rows_clear(
    before: Sequence[dict], after: Sequence[dict], *, plan: ProtectedPlan | None, canvas: _Canvas
) -> tuple[list[dict], list[str]]:
    """Rows a text-style pass restyled (font, size, spot) are re-measured against the faces
    and marked objects; a restyle that puts a row on one its placement had kept it off of
    is undone (the placed row is kept), so the protected receipt measured at placement stays
    true. A row that cannot be measured keeps its placed geometry too. Returns the rows and
    the ids that were kept as placed."""
    if plan is None or plan.geometry is None:
        return list(after), []
    placed = {row.get("id"): row for row in before}
    out: list[dict] = []
    kept: list[str] = []
    for row in after:
        original = placed.get(row.get("id"))
        if original is None or original == row:
            out.append(row)
            continue
        try:
            moved_onto = _row_coverage(row, plan, canvas) > _MAX_COVERAGE and (
                _row_coverage(original, plan, canvas) <= _MAX_COVERAGE
            )
        except Exception as exc:  # noqa: BLE001 - unmeasurable restyle: keep the measured row
            log.warning("protected_text.styled_row_unmeasured", error=str(exc)[:200])
            moved_onto = True
        if moved_onto:
            out.append(copy.deepcopy(original))
            kept.append(str(row.get("id")))
        else:
            out.append(row)
    return out, kept


__all__ = [
    "ProtectedGeometry",
    "ProtectedPlan",
    "keep_styled_rows_clear",
    "choose_caption_look",
    "protected_text_receipt",
    "resolve_protected_plan",
]
