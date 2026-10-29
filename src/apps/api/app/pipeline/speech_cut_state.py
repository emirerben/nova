"""Persisted speech-cut review state and timeline remapping.

The detector owns source-timeline ranges.  Everything exposed to the editor is
derived from this module so candidate receipts, revision guards, Director
operations, and render-time lane remapping cannot disagree.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Callable, Iterable, Sequence
from copy import deepcopy
from dataclasses import dataclass
from itertools import pairwise
from typing import Any

from app.pipeline.cut_grid import frames_to_spans, output_time
from app.pipeline.silence_cut import Removal

SPEECH_CUT_STATE_VERSION = 1
# Bounds for a persisted frame_grid read back from a stored summary: a row
# outside them is treated as having no grid.
_MAX_GRID_FPS = 240
_MAX_GRID_FRAMES = 10_000
_MAX_GRID_FRAME = _MAX_GRID_FPS * 86_400  # a day at the highest fps
# Lane times and a summary's original_duration_s persist rounded to the
# millisecond (`_round_time`), so a time within one of a span edge is at it.
_CUT_POINT_TOL_S = 0.001
_TIMED_LIST_FIELDS = (
    "text_elements",
    "media_overlays",
    "sound_effects",
    "camera_effects",
    "boundary_effects",
    "motion_scenes",
    "visual_blocks",
    # KRI-589: freehand drawings retime like text (`_fit_drawings` finishes the job).
    "drawings",
    "transcript",
    "overlay_transcript",
)


def _round_time(value: float) -> float:
    return round(float(value), 3)


def candidate_id(
    *, start_s: float, end_s: float, reason: str, source: str, source_fingerprint: str
) -> str:
    payload = (
        f"{source_fingerprint}|{source}|{_round_time(start_s):.3f}|"
        f"{_round_time(end_s):.3f}|{reason}"
    )
    return "cut_" + hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def make_candidate(
    *,
    start_s: float,
    end_s: float,
    reason: str,
    source: str,
    preview: str,
    source_fingerprint: str,
    transcript_hash: str,
) -> dict[str, Any]:
    return {
        "candidate_id": candidate_id(
            start_s=start_s,
            end_s=end_s,
            reason=reason,
            source=source,
            source_fingerprint=source_fingerprint,
        ),
        "start_s": _round_time(start_s),
        "end_s": _round_time(end_s),
        "reason": str(reason).strip()[:240],
        "source": source,
        "preview": " ".join(str(preview).split())[:160],
        "source_fingerprint": source_fingerprint,
        "transcript_hash": transcript_hash,
        "coordinate_space": "source_v1",
        "status": "pending",
    }


def cut_revision(variant: dict[str, Any]) -> str:
    """Stable optimistic-concurrency token for every cut-affecting state."""
    payload = {
        "disabled": variant.get("speech_cuts_disabled") is True,
        "candidates": [
            {
                "candidate_id": c.get("candidate_id"),
                "start_s": c.get("start_s"),
                "end_s": c.get("end_s"),
                "status": c.get("status"),
            }
            for c in variant.get("speech_cut_candidates") or []
            if isinstance(c, dict)
        ],
        "forced": variant.get("speech_cut_forced_removals") or [],
        "in_flight": variant.get("speech_cut_in_flight"),
        "automatic": (variant.get("silence_cut") or {}).get("removed") or [],
    }
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


def public_candidates(variant: dict[str, Any]) -> list[dict[str, Any]]:
    revision = cut_revision(variant)
    return [
        {**c, "revision": revision}
        for c in variant.get("speech_cut_candidates") or []
        if isinstance(c, dict) and c.get("status") == "pending"
    ]


def accept_candidate(
    variant: dict[str, Any], *, candidate_id_value: str, expected_revision: str
) -> tuple[dict[str, Any], dict[str, Any]]:
    current = cut_revision(variant)
    if expected_revision != current:
        raise ValueError("speech_cut_revision_conflict")
    updated = deepcopy(variant)
    candidate = next(
        (
            c
            for c in updated.get("speech_cut_candidates") or []
            if isinstance(c, dict) and c.get("candidate_id") == candidate_id_value
        ),
        None,
    )
    if candidate is None or candidate.get("status") != "pending":
        raise LookupError("speech_cut_candidate_not_found")
    candidate["status"] = "applying"
    removal = {
        "start_s": _round_time(candidate["start_s"]),
        "end_s": _round_time(candidate["end_s"]),
        "reason": str(candidate.get("source") or "manual_review"),
        "candidate_id": candidate_id_value,
    }
    forced = list(updated.get("speech_cut_forced_removals") or [])
    forced.append(removal)
    updated["speech_cut_in_flight"] = {
        "operation": "apply_speech_cut_candidate",
        "candidate_id": candidate_id_value,
        "desired_forced_removals": forced,
        "desired_disabled": False,
    }
    updated["speech_cut_revision"] = cut_revision(updated)
    receipt = {
        "operation": "apply_speech_cut_candidate",
        "candidate_id": candidate_id_value,
        "removed": removal,
        "time_saved_s": _round_time(removal["end_s"] - removal["start_s"]),
        "revision": updated["speech_cut_revision"],
    }
    return updated, receipt


def restore_original_timing(
    variant: dict[str, Any], *, expected_revision: str
) -> tuple[dict[str, Any], dict[str, Any]]:
    current = cut_revision(variant)
    if expected_revision != current:
        raise ValueError("speech_cut_revision_conflict")
    updated = deepcopy(variant)
    intervals = sorted(
        (float(r.get("start_s", 0.0)), float(r.get("end_s", 0.0)))
        for r in [
            *((variant.get("silence_cut") or {}).get("removed") or []),
            *(variant.get("speech_cut_forced_removals") or []),
        ]
        if isinstance(r, dict) and float(r.get("end_s", 0.0)) > float(r.get("start_s", 0.0))
    )
    restored_s = 0.0
    cursor_start: float | None = None
    cursor_end = 0.0
    for start, end in intervals:
        if cursor_start is None:
            cursor_start, cursor_end = start, end
        elif start <= cursor_end:
            cursor_end = max(cursor_end, end)
        else:
            restored_s += cursor_end - cursor_start
            cursor_start, cursor_end = start, end
    if cursor_start is not None:
        restored_s += cursor_end - cursor_start
    updated["speech_cut_in_flight"] = {
        "operation": "restore_original_timing",
        "desired_forced_removals": [],
        "desired_disabled": True,
    }
    updated["speech_cut_revision"] = cut_revision(updated)
    receipt = {
        "operation": "restore_original_timing",
        "restored_s": _round_time(restored_s),
        "revision": updated["speech_cut_revision"],
    }
    return updated, receipt


def _removed_before(t: float, removals: list[Removal]) -> float:
    return sum(max(0.0, min(r.end_s, t) - r.start_s) for r in removals)


@dataclass(frozen=True)
class RenderedCut:
    """The source spans a cloud cut render plays, in output order.

    ``reframe`` renders each keep segment as whole frames on the cut's frame
    grid (``cut_grid.keep_segment_frames``), so its timeline is these spans,
    not the removals: every removal edge is up to half a frame off, and the
    offsets add up over the cuts before a lane. The last span is open, so time
    past the clip end runs on one to one, as it does under the removals.
    """

    fps: int
    spans: tuple[tuple[float, float], ...]

    @classmethod
    def from_summary(cls, summary: dict[str, Any] | None) -> RenderedCut | None:
        """The render a ``silence_cut`` summary records, or None.

        Only a cloud cut render persists ``frame_grid``
        (``silence_cut.plan_summary(grid=...)``). Phone and narration cuts
        play the exact float segments, and older cloud renders recorded no
        grid; those keep the removal mapping.
        """
        if not isinstance(summary, dict) or not isinstance(summary.get("frame_grid"), dict):
            return None
        grid = summary["frame_grid"]
        try:
            fps = grid["fps"]
            frames = [(first, end) for first, end in grid["frames"]]
            clip_end = float(summary.get("original_duration_s") or 0.0)
        except (KeyError, TypeError, ValueError, OverflowError):
            return None
        # A stored row is data: anything but whole, ordered, in-range frames
        # keeps the removal mapping instead of failing the re-cut.
        if (
            type(fps) is not int
            or not 0 < fps <= _MAX_GRID_FPS
            or not 0 < len(frames) <= _MAX_GRID_FRAMES
            or any(type(v) is not int for frame in frames for v in frame)
            or frames[0][0] < 0
            or any(end <= first for first, end in frames)
            or any(nxt[0] < prev[1] for prev, nxt in pairwise(frames))
            or frames[-1][1] > _MAX_GRID_FRAME
            or not math.isfinite(clip_end)
            or clip_end < 0
        ):
            return None
        spans = frames_to_spans(frames, fps)
        if clip_end > spans[-1][1] + _CUT_POINT_TOL_S:
            # A trailing cut: the clip's tail does not play.
            spans.append((clip_end, math.inf))
        else:
            spans[-1] = (spans[-1][0], math.inf)
        return cls(fps=fps, spans=tuple(spans))

    def to_output(self, t: float) -> float:
        """Where source ``t`` plays; an instant inside a cut maps to that cut."""
        return output_time(t, self.spans)

    def anchor_to_output(self, t: float) -> float | None:
        """`to_output` for one anchor, or None when the render cut it.

        An anchor within half a frame of a cut stays, at the cut. The grid
        moved each edge by up to that much, so an anchor at a cut's float
        time (a lane from an uncut prior placed on the new cut) is not lost.
        """
        tol = 0.5 / self.fps
        prev_end: float | None = None
        for start, end in self.spans:
            if t < start:
                at_cut = start - t <= tol or (prev_end is not None and t - prev_end <= tol)
                return self.to_output(t) if at_cut else None
            if t < end:
                break
            prev_end = end
        return self.to_output(t)

    def to_source(self, t: float, *, prefer_post_cut: bool = True) -> float:
        """The source instant output ``t`` plays (`to_output` inverted).

        At a cut, ``prefer_post_cut`` picks the start of the span after it,
        else the end of the span before. Lane times persist rounded to the
        millisecond, so a time within one of a cut is at it.
        """
        offset = 0.0
        for start, end in self.spans[:-1]:
            out_end = offset + (end - start)
            if t < out_end - _CUT_POINT_TOL_S or (
                not prefer_post_cut and t <= out_end + _CUT_POINT_TOL_S
            ):
                return min(end, start + max(0.0, t - offset))
            offset = out_end
        return self.spans[-1][0] + max(0.0, t - offset)


def remap_time(t: float, removals: Iterable[Removal]) -> float | None:
    ordered = sorted(removals, key=lambda r: (r.start_s, r.end_s))
    value = float(t)
    if any(r.start_s <= value < r.end_s for r in ordered):
        return None
    return _round_time(value - _removed_before(value, ordered))


def remap_timed_records(
    records: list[dict[str, Any]] | None, removals: Iterable[Removal]
) -> list[dict[str, Any]]:
    """Remap start/end records; fully removed records are dropped, overlaps clamp."""
    ordered = sorted(removals, key=lambda r: (r.start_s, r.end_s))
    return _remap_records(
        records,
        keep_spans=_keep_segments(ordered),
        to_output=lambda t: t - _removed_before(t, ordered),
        anchor_to_output=lambda t: remap_time(t, ordered),
    )


def _remap_records(
    records: list[dict[str, Any]] | None,
    *,
    keep_spans: Sequence[tuple[float, float]],
    to_output: Callable[[float], float],
    anchor_to_output: Callable[[float], float | None],
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for raw in records or []:
        if not isinstance(raw, dict):
            continue
        start_key = "start_s" if "start_s" in raw else "start"
        end_key = "end_s" if "end_s" in raw else "end"
        if start_key not in raw or end_key not in raw:
            if "at_s" in raw:
                mapped_at = anchor_to_output(float(raw["at_s"]))
                if mapped_at is None:
                    continue
                item = deepcopy(raw)
                item["at_s"] = _round_time(mapped_at)
                out.append(item)
                continue
            out.append(deepcopy(raw))
            continue
        start = float(raw[start_key])
        end = float(raw[end_key])
        kept = [
            (max(start, lo), min(end, hi)) for lo, hi in keep_spans if min(end, hi) > max(start, lo)
        ]
        if not kept:
            continue
        item = deepcopy(raw)
        item[start_key] = _round_time(to_output(kept[0][0]))
        item[end_key] = _round_time(to_output(kept[-1][1]))
        if isinstance(item.get("words"), list):
            item["words"] = _remap_records(
                item["words"],
                keep_spans=keep_spans,
                to_output=to_output,
                anchor_to_output=anchor_to_output,
            )
        if isinstance(item.get("source_params"), dict):
            params = dict(item["source_params"])
            schedule = params.get("reveal_schedule_s")
            if isinstance(schedule, list):
                params["reveal_schedule_s"] = [
                    _round_time(mapped)
                    for value in schedule
                    if (mapped := anchor_to_output(float(value))) is not None
                ]
            item["source_params"] = params
        out.append(item)
    return out


def _keep_segments(removals: list[Removal]) -> list[tuple[float, float]]:
    """The spans sorted ``removals`` keep; the last runs open past any clip end."""
    cursor = 0.0
    keep: list[tuple[float, float]] = []
    for removal in removals:
        if removal.start_s > cursor:
            keep.append((cursor, removal.start_s))
        cursor = max(cursor, removal.end_s)
    keep.append((cursor, math.inf))
    return keep


def output_to_source_time(
    t: float, removals: Iterable[Removal], *, prefer_post_cut: bool = True
) -> float:
    """Invert a cut timeline coordinate onto the original source timeline."""
    ordered = sorted(removals, key=lambda r: (r.start_s, r.end_s))
    source = float(t)
    removed_before = 0.0
    for removal in ordered:
        cut_start = removal.start_s - removed_before
        if float(t) > cut_start or (prefer_post_cut and float(t) == cut_start):
            duration = removal.end_s - removal.start_s
            source += duration
            removed_before += duration
    return _round_time(source)


def reproject_timed_records(
    records: list[dict[str, Any]] | None,
    *,
    old_removals: Iterable[Removal],
    new_removals: Iterable[Removal],
    old_render: RenderedCut | None = None,
    new_render: RenderedCut | None = None,
) -> list[dict[str, Any]]:
    """Project records from an old cut timeline through source into a new one.

    A side with a ``RenderedCut`` (a cloud cut render) maps through the frames
    that render played and ignores its removals; a side without one maps
    through its removals. An old render that cut but recorded no frames (made
    before ``frame_grid`` was persisted) keeps both sides on the removals: its
    lanes sit where the removals put them, and only the same mapping forward
    cancels that, so lanes the new cut never touches stay put.
    """
    old = list(old_removals)
    if old_render is None and old:
        new_render = None

    def to_source(t: float, *, prefer_post_cut: bool) -> float:
        if old_render is not None:
            return old_render.to_source(t, prefer_post_cut=prefer_post_cut)
        return output_to_source_time(t, old, prefer_post_cut=prefer_post_cut)

    def _to_source(raw_records: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
        source_records: list[dict[str, Any]] = []
        for raw in raw_records or []:
            if not isinstance(raw, dict):
                continue
            item = deepcopy(raw)
            start_key = "start_s" if "start_s" in item else "start"
            end_key = "end_s" if "end_s" in item else "end"
            if start_key in item and end_key in item:
                item[start_key] = to_source(float(item[start_key]), prefer_post_cut=True)
                item[end_key] = to_source(float(item[end_key]), prefer_post_cut=False)
            elif "at_s" in item:
                item["at_s"] = to_source(float(item["at_s"]), prefer_post_cut=True)
            if isinstance(item.get("words"), list):
                item["words"] = _to_source(item["words"])
            if isinstance(item.get("source_params"), dict):
                params = dict(item["source_params"])
                schedule = params.get("reveal_schedule_s")
                if isinstance(schedule, list):
                    params["reveal_schedule_s"] = [
                        to_source(float(value), prefer_post_cut=True) for value in schedule
                    ]
                item["source_params"] = params
            source_records.append(item)
        return source_records

    source_records = _to_source(records)
    if new_render is not None:
        # What survives is what the new render plays.
        return _remap_records(
            source_records,
            keep_spans=new_render.spans,
            to_output=new_render.to_output,
            anchor_to_output=new_render.anchor_to_output,
        )
    return remap_timed_records(source_records, list(new_removals))


def _fit_drawings(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Finish a retimed freehand-drawing lane (KRI-589).

    The generic remap already shifted each window and dropped the drawings a cut removed
    entirely; what is left to do is the drawing-specific part: a window the cut shrank
    below the schema floor is dropped, and a draw-on that no longer fits its window is
    shortened to it (never below the schema floor of 0.2 s).
    """
    from app.agents._schemas.drawing import MIN_DRAWING_DURATION_S  # noqa: PLC0415

    out: list[dict[str, Any]] = []
    for row in records:
        window = float(row["end_s"]) - float(row["start_s"])
        if window < MIN_DRAWING_DURATION_S - 1e-6:
            continue
        item = deepcopy(row)
        animation = item.get("animation")
        if isinstance(animation, dict) and animation.get("draw_duration_s") is not None:
            animation["draw_duration_s"] = _round_time(
                max(0.2, min(float(animation["draw_duration_s"]), window))
            )
        out.append(item)
    return out


def reproject_variant_timing(
    variant: dict[str, Any],
    *,
    old_removals: Iterable[Removal],
    new_removals: Iterable[Removal],
    old_render: RenderedCut | None = None,
    new_render: RenderedCut | None = None,
) -> dict[str, Any]:
    """Carry every timed lane from the old render's timeline onto the new one's.

    Pass each side's ``RenderedCut.from_summary(variant["silence_cut"])``. A
    cloud cut render plays frame-snapped segments, which the removals miss by
    up to half a frame per cut edge: restoring the original timing would
    carry every earlier cut's miss into a lane, and each accepted cut would
    add its own.
    """
    updated = deepcopy(variant)
    old = list(old_removals)
    new = list(new_removals)

    def reproject(records: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
        return reproject_timed_records(
            records,
            old_removals=old,
            new_removals=new,
            old_render=old_render,
            new_render=new_render,
        )

    updated["caption_cues"] = reproject(updated.get("caption_cues"))
    for field in _TIMED_LIST_FIELDS:
        if isinstance(updated.get(field), list):
            updated[field] = reproject(updated[field])
    if isinstance(updated.get("drawings"), list):
        updated["drawings"] = _fit_drawings(updated["drawings"])
    for field in (
        "speech_map",
        "overlay_suggestions",
        "overlay_suggest_hash",
        "director_suggestions",
        "director_revision",
    ):
        updated[field] = None
    return updated


def authored_speech_windows(slots: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    """Ordered source/output windows, with the authored renderer's overlap policy."""
    windows = []
    cursor = 0.0
    previous = None
    for slot in slots:
        if slot.get("removed"):
            continue
        duration = float(slot["duration_s"])
        if previous and previous.get("transition_after", "cut") != "cut":
            overlap = min(
                float(previous.get("transition_duration_s") or 0.3),
                duration * 0.3,
                float(previous["duration_s"]) * 0.3,
                0.3,
            )
            if overlap >= 0.1:
                cursor -= overlap
        if slot.get("_speech_output_start_s") is not None:
            cursor = float(slot["_speech_output_start_s"])
        rate = float(slot.get("playback_rate") or 1)
        source_start = float(slot["in_s"])
        source_end = float(slot.get("source_end_s") or source_start + duration * rate)
        moving_duration = min(duration, max(0.0, source_end - source_start) / rate)
        windows.append(
            {
                "id": slot.get("slot_id"),
                "clip_index": slot["clip_index"],
                "source": float(slot["in_s"]),
                "rate": rate,
                "start": cursor,
                "end": cursor + duration,
                "caption_end": cursor + moving_duration,
            }
        )
        cursor += duration
        previous = slot
    return windows


def reproject_authored_speech_records(
    records: list[dict[str, Any]] | None,
    *,
    old_slots: Sequence[dict[str, Any]],
    new_slots: Sequence[dict[str, Any]],
    captions: bool = False,
) -> list[dict[str, Any]]:
    """Carry output records through their source occurrence into an authored cut.

    Unlike a removal-only cut, authored windows can slide, change rate and move
    out of source order. Each interval is intersected per occurrence; it must
    never bridge another segment after reorder. This is the same source/output
    boundary as cleanup's ``reproject_timed_records``, extended for that order.
    Continuous music and independent narration are deliberately not inputs.
    """
    old = authored_speech_windows(old_slots)
    new = {w["id"]: w for w in authored_speech_windows(new_slots)}
    result = []
    for raw in records or []:
        start_key = "start_s" if "start_s" in raw else "start"
        end_key = "end_s" if "end_s" in raw else "end"
        point = "at_s" in raw
        if point:
            start_key = end_key = "at_s"
        if start_key not in raw or end_key not in raw:
            result.append(deepcopy(raw))
            continue
        start, end = float(raw[start_key]), float(raw[end_key])
        parts = []
        point_owner = next((w for w in reversed(old) if w["start"] <= start < w["end"]), None)
        for prior in old:
            if point and prior is not point_owner:
                continue
            target = new.get(prior["id"])
            if target is None or target["clip_index"] != prior["clip_index"]:
                continue

            def project(t: float) -> float:
                source = prior["source"] + (t - prior["start"]) * prior["rate"]
                return target["start"] + (source - target["source"]) / target["rate"]

            lo = max(start, prior["start"])
            hi = min(end, prior["caption_end"] if captions else prior["end"])
            target_end = target["caption_end"] if captions else target["end"]
            a, b = max(target["start"], project(lo)), min(target_end, project(hi))
            if point:
                if not (
                    prior["start"] <= start < prior["end"]
                    and target["start"] <= project(start) < target["end"]
                ):
                    continue
            elif hi <= lo or b <= a + 1e-9:
                continue
            item = deepcopy(raw)
            item[start_key], item[end_key] = _round_time(a), _round_time(b)
            if isinstance(raw.get("words"), list):
                words = []
                for word in raw["words"]:
                    wk = "start_s" if "start_s" in word else "start"
                    ek = "end_s" if "end_s" in word else "end"
                    wa, wb = max(a, project(float(word[wk]))), min(b, project(float(word[ek])))
                    if wb > wa + 1e-9:
                        words.append({**deepcopy(word), wk: _round_time(wa), ek: _round_time(wb)})
                item["words"] = words
                if captions:
                    if not words:
                        continue
                    # Keep punctuation/case carried on the timed words. A cut
                    # caption must not still spell the words the creator removed.
                    if len(words) != len(raw["words"]):
                        item["text"] = " ".join(
                            str(w.get("text", w.get("word", ""))) for w in words
                        )
            if isinstance(raw.get("word_timings"), list):
                timings = []
                for word in raw["word_timings"]:
                    ws = start + float(word.get("start_s", 0))
                    we = start + float(
                        word.get(
                            "end_s",
                            word.get("start_s", 0) + float(word.get("duration_cs", 5)) / 100,
                        )
                    )
                    wa, wb = max(a, project(ws)), min(b, project(we))
                    if wb > wa + 1e-9:
                        timing = {
                            **deepcopy(word),
                            "start_s": _round_time(wa - a),
                            "end_s": _round_time(wb - a),
                        }
                        if "duration_cs" in word:
                            timing["duration_cs"] = round((wb - wa) * 100)
                        timings.append(timing)
                item["word_timings"] = timings
                if len(timings) != len(raw["word_timings"]):
                    item["text"] = " ".join(str(w.get("text", "")) for w in timings)
            params = item.get("source_params")
            if isinstance(params, dict) and isinstance(params.get("reveal_schedule_s"), list):
                params["reveal_schedule_s"] = [
                    _round_time(project(float(t)))
                    for t in params["reveal_schedule_s"]
                    if a <= project(float(t)) < b
                ]
            parts.append(item)
        for index, part in enumerate(parts):
            if index and part.get("id"):
                part["id"] = f"{part['id']}-part-{index}"
            result.append(part)
    return sorted(
        result, key=lambda row: float(row.get("start_s", row.get("start", row.get("at_s", 0))))
    )


SPEECH_EDITOR_LANES = (
    "caption_cues",
    "text_elements",
    "sound_effects",
    "media_overlays",
    "visual_blocks",
    "motion_scenes",
    "camera_effects",
    "drawings",
)


def speech_basis_entry(record: dict, slots: Sequence[dict]) -> dict:
    """Retain source provenance, never paths, URLs or another asset authority."""
    keys = (
        "slot_id",
        "clip_index",
        "in_s",
        "duration_s",
        "playback_rate",
        "transition_after",
        "transition_duration_s",
        "removed",
        "source_end_s",
    )
    start = record.get("start_s", record.get("start", record.get("at_s")))
    end = record.get("end_s", record.get("end", record.get("at_s")))
    windows = authored_speech_windows(slots)
    anchors = []
    for row, window in zip((r for r in slots if not r.get("removed")), windows, strict=True):
        if start is not None and end is not None:
            if "at_s" in record:
                if not window["start"] <= float(start) < window["end"]:
                    continue
            elif min(float(end), window["end"]) <= max(float(start), window["start"]):
                continue
        anchors.append(
            {**{k: row[k] for k in keys if k in row}, "_speech_output_start_s": window["start"]}
        )
    return {"record": deepcopy(record), "slots": anchors}


def project_speech_basis(
    entries: list[dict], slots: Sequence[dict], *, captions: bool
) -> list[dict]:
    rows = []
    for entry in entries:
        if "source_groups" in entry:
            rows.extend(_project_source_caption_group(entry, slots))
        else:
            rows.extend(
                reproject_authored_speech_records(
                    [entry["record"]], old_slots=entry["slots"], new_slots=slots, captions=captions
                )
            )
    return sorted(rows, key=lambda r: float(r.get("start_s", r.get("start", r.get("at_s", 0)))))


def sync_speech_basis(
    entries: list[dict] | None,
    records: list[dict],
    slots: Sequence[dict],
    *,
    captions: bool,
    deleted_ids: set[str] | None = None,
) -> list[dict]:
    """Update visible authored edits while retaining temporarily trimmed content.

    A supplied lane is already on its submitted output timeline. Compare each
    source-anchored record's projection there, retaining its full source extent
    only if untouched. A changed/deleted record is replaced/removed explicitly;
    hidden records survive a window move, but explicit deletion wins everywhere.
    """
    if entries is None:
        return [speech_basis_entry(row, slots) for row in records]
    remaining = list(records)
    kept = []
    deleted = deleted_ids or set()
    for entry in sorted(entries, key=lambda row: "source_groups" in row):
        if "source_groups" in entry:
            group = deepcopy(entry)
            suppressed = set(group.get("suppressed_ids") or []) | deleted
            group["suppressed_ids"] = sorted(suppressed)
            for row in project_speech_basis([group], slots, captions=True):
                if row in remaining:
                    remaining.remove(row)
                else:
                    suppressed.add(row["id"])
            group["suppressed_ids"] = sorted(suppressed)
            kept.append(group)
            continue
        identifier = str(entry["record"].get("id") or "")
        if identifier in deleted:
            continue
        visible = project_speech_basis([entry], slots, captions=captions)
        if not visible:
            kept.append(deepcopy(entry))
        elif all(row in remaining for row in visible):
            kept.append(deepcopy(entry))
            for row in visible:
                remaining.remove(row)
        # Otherwise a visible record was edited/deleted. The current replacement
        # below becomes its new authored source anchor (no resurrection later).
    return kept + [speech_basis_entry(row, slots) for row in remaining]


def initial_speech_timing_basis(variant: dict, slots: Sequence[dict]) -> dict:
    """A durable basis for existing cut lanes; no transcript/model inference."""
    from app.services.editor_deletions import canonical_caption_rows

    basis = deepcopy(variant.get("speech_timing_basis") or {})
    for field in SPEECH_EDITOR_LANES:
        if field in basis or not isinstance(variant.get(field), list):
            continue
        records = variant[field]
        if field == "caption_cues":
            records = canonical_caption_rows(records)
        elif field == "text_elements":
            records = authored_speech_texts(records)
        basis[field] = [speech_basis_entry(record, slots) for record in records]
    if "text_elements" in basis:
        basis["text_elements"] = [
            entry for entry in basis["text_elements"] if authored_speech_texts([entry["record"]])
        ]
    return basis


def authored_speech_texts(records: list[dict]) -> list[dict]:
    """Caption mirrors are read projections; the cue lane owns their timing."""
    return [
        row for row in records if (row.get("source_params") or {}).get("source") != "caption_cue"
    ]


def _project_source_caption_group(entry: dict, slots: Sequence[dict]) -> list[dict]:
    targets = {row.get("slot_id"): row for row in slots if not row.get("removed")}
    suppressed = set(entry.get("suppressed_ids") or [])
    result = []
    for group in entry["source_groups"]:
        full = group["slot"]
        target = targets.get(full["slot_id"])
        if target is None:
            continue
        start = float(target["in_s"])
        end = min(
            start + float(target["duration_s"]) * float(target.get("playback_rate") or 1),
            float(target.get("source_end_s") or math.inf),
        )
        for index, raw in enumerate(entry["source_captions"]):
            identifier = f"speech-source-{full['slot_id']}-{index}"
            if identifier in suppressed:
                continue
            outside = [
                w
                for w in raw.get("words") or []
                if float(w["end_s"]) <= group["initial_start_s"]
                or float(w["start_s"]) >= group["initial_end_s"]
            ]
            if not any(float(w["end_s"]) > start and float(w["start_s"]) < end for w in outside):
                continue
            cue = {
                **deepcopy(raw),
                "id": identifier,
                "words": outside,
                "text": " ".join(w["text"] for w in outside),
                "start_s": outside[0]["start_s"],
                "end_s": outside[-1]["end_s"],
            }
            result.extend(
                reproject_authored_speech_records(
                    [cue], old_slots=[full], new_slots=slots, captions=True
                )
            )
    return result
