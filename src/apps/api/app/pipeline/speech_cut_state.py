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
        keep_spans=_keep_segments(ordered, math.inf),
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


def _keep_segments(removals: list[Removal], duration_s: float) -> list[tuple[float, float]]:
    cursor = 0.0
    keep: list[tuple[float, float]] = []
    for removal in removals:
        if removal.start_s > cursor:
            keep.append((cursor, removal.start_s))
        cursor = max(cursor, removal.end_s)
    if duration_s > cursor:
        keep.append((cursor, duration_s))
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
    for field in (
        "speech_map",
        "overlay_suggestions",
        "overlay_suggest_hash",
        "director_suggestions",
        "director_revision",
    ):
        updated[field] = None
    return updated
