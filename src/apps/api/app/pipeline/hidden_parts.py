"""Parts of a clip the creator asked never to show (Leverkusen M2, 2026-10-30).

"The kids at the start of the second goal clip are another family's children, so don't show
them": the clip stays (it holds the TOR on the big screen and Taha's scream), but its first
2.5 s never reach the video. The creator's ask is a resolved ``hide_part`` clip intent (one
clip, an edge, the creator's seconds; ``app.schemas.clip_intents``). This module turns those
rows into source windows no frame may come from, and is the one rule every planner and
checker uses:

* the length planner and the montage planner move a window out of a part (``keep_out``) and
  never size a cut past what the clip may still show (``usable_s``);
* the montage plan refuses, rather than renders, a cut it could not move
  (``HiddenPartShown``), and the pinned phone recipe is checked against the same parts on
  the creator render contract, so a writer or a compiler that ignores them never renders.

A part is widened by ``HIDDEN_EDGE_GUARD_S``, two margins:

* ``FRAME_GUARD_S``: a window starting exactly at 2.5 s can show the frame whose presentation
  interval contains 2.5 s, which on a 29.97 fps or variable-rate phone file is timed before
  2.5 s (IMG_1039: a window at 3.1 s opens on the frame at 3.068 s under AVFoundation's
  rule). Frame rounding and the compiler's re-quantising move edges by a fraction of a frame
  too. Three frames at 30 fps (six at 60, two at 24).
* ``ESTIMATE_PAD_S``: the creator's seconds are an estimate ("after about 2.5 seconds"). In
  IMG_1039 a child is still in frame at 2.6-2.7 s; from 2.8 s on, none is. Half a second past
  the creator's number covers that.

Pure: no I/O, no model call.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal

from app.pipeline.phone_guided_plan import UnsupportedPhonePlan

FRAME_GUARD_S = 0.1
ESTIMATE_PAD_S = 0.5
HIDDEN_EDGE_GUARD_S = FRAME_GUARD_S + ESTIMATE_PAD_S
# The phone compiler reads a window that ends within its export margin of the file's end
# a little earlier (`phone_guided_plan._EXPORT_SAFETY_MARGIN_S`, 50 ms), sliding the START
# back with it. A window kept out of a part at the clip's start leaves this much tail so the
# compiler never has to slide it (a frame at 30 fps on top of the margin).
TAIL_ROOM_S = 0.05 + 1 / 30
# Below this a clip has nothing left worth showing (three frames at 30 fps).
MIN_SHOWN_S = 0.1
_EPS = 1e-6

Edge = Literal["start", "end"]


@dataclass(frozen=True)
class HiddenPart:
    """``seconds`` at the ``edge`` of clip ``media_id`` that never play."""

    media_id: str
    edge: Edge
    seconds: float

    def forbidden(self, duration_s: float) -> tuple[float, float]:
        """Source seconds no frame may come from, guard included.

        An end part runs to infinity: a phone file a hair longer than the proxy still never
        plays its last seconds.
        """
        if self.edge == "start":
            return 0.0, self.seconds + HIDDEN_EDGE_GUARD_S
        return max(0.0, duration_s - self.seconds - HIDDEN_EDGE_GUARD_S), math.inf

    def json(self) -> dict[str, Any]:
        return {"media_id": self.media_id, "edge": self.edge, "seconds": self.seconds}


# What the creator reads when a plan is refused: the job's `error_detail` (a typed plan
# defect's message reaches the chat), never media ids or seconds.
HIDDEN_PART_REFUSAL = "This edit would show part of a clip you asked me to hide, so I stopped it."


class HiddenPartShown(UnsupportedPhonePlan):
    """A planned window would show a part the creator asked never to show.

    A typed plan defect (deterministic, never retried): the worker fails the job with
    ``HIDDEN_PART_REFUSAL``; ``detail`` (ids and seconds) is for the log only."""

    def __init__(self, detail: str) -> None:
        super().__init__(HIDDEN_PART_REFUSAL, reason="hidden_part")
        self.detail = detail


def _positive(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) and number > 0 else None


def hidden_part_rows(strategy: Mapping[str, Any] | None) -> list[Mapping[str, Any]]:
    """Every ``hide_part`` row of the strategy's resolved clip intents, settled or not."""
    rows = (strategy or {}).get("resolved_clip_intents")
    if not isinstance(rows, list):
        return []
    return [row for row in rows if isinstance(row, Mapping) and row.get("op") == "hide_part"]


def unsettled_hidden_parts(strategy: Mapping[str, Any] | None) -> list[str]:
    """The attributes of hide_part rows that name no clip, edge or seconds yet.

    A strategy carrying one is a privacy ask nobody can keep: callers refuse it, never
    render the clip whole."""
    return [
        str(row.get("attribute") or "a clip")
        for row in hidden_part_rows(strategy)
        if str(row.get("status") or "resolved") != "resolved"
        or row.get("part_edge") not in ("start", "end")
        or _positive(row.get("part_s")) is None
    ]


def hidden_parts_from_strategy(
    strategy: Mapping[str, Any] | None,
    *,
    aliases: Mapping[str, str] | None = None,
) -> tuple[HiddenPart, ...]:
    """The settled parts, one per clip and edge (the most seconds asked wins).

    ``aliases`` is dispatch's ``{dropped copy: kept copy}`` collapse of byte-identical uploads
    (KRI-544): the kept copy has the same frames, so a part on either applies to both.
    """
    widest: dict[tuple[str, str], float] = {}
    for row in hidden_part_rows(strategy):
        edge = row.get("part_edge")
        seconds = _positive(row.get("part_s"))
        if str(row.get("status") or "resolved") != "resolved" or edge not in ("start", "end"):
            continue
        if seconds is None:
            continue
        for assignment in row.get("assignments") or ():
            media_id = (
                str(assignment.get("media_id") or "") if isinstance(assignment, Mapping) else ""
            )
            if not media_id:
                continue
            ids = {media_id}
            for dropped, kept in (aliases or {}).items():
                if media_id in (dropped, kept):
                    ids |= {str(dropped), str(kept)}
            for target in ids:
                key = (target, str(edge))
                widest[key] = max(seconds, widest.get(key, 0.0))
    return tuple(
        HiddenPart(media_id, edge, seconds)  # type: ignore[arg-type]
        for (media_id, edge), seconds in sorted(widest.items())
    )


def parts_by_media(parts: Iterable[HiddenPart]) -> dict[str, tuple[HiddenPart, ...]]:
    grouped: dict[str, list[HiddenPart]] = {}
    for part in parts:
        grouped.setdefault(part.media_id, []).append(part)
    return {media_id: tuple(rows) for media_id, rows in grouped.items()}


def allowed_spans(duration_s: float, parts: Sequence[HiddenPart]) -> list[tuple[float, float]]:
    """The stretches of a ``duration_s`` clip that may still play, earliest first."""
    if not parts:
        return [(0.0, duration_s)] if duration_s > 0 else []
    lo, hi = 0.0, duration_s
    for part in parts:
        start, end = part.forbidden(duration_s)
        if part.edge == "start":
            lo = max(lo, end)
        else:
            hi = min(hi, start)
    if any(part.edge == "start" for part in parts):
        # The compiler may slide a window that ends at the file's end; keep room for it.
        hi = min(hi, duration_s - TAIL_ROOM_S)
    lo = math.ceil(lo * 1000 - _EPS) / 1000
    hi = math.floor(hi * 1000 + _EPS) / 1000
    return [(lo, hi)] if hi - lo >= MIN_SHOWN_S - _EPS else []


def usable_s(duration_s: float, parts: Sequence[HiddenPart]) -> float:
    """The longest window the clip can still play."""
    return max((end - start for start, end in allowed_spans(duration_s, parts)), default=0.0)


def shows_hidden(
    start_s: float, end_s: float, duration_s: float, parts: Sequence[HiddenPart]
) -> bool:
    """True when source window ``[start_s, end_s)`` reaches into any part."""
    for part in parts:
        lo, hi = part.forbidden(duration_s)
        if start_s < hi - _EPS and end_s > lo + _EPS:
            return True
    return False


def keep_out(
    start_s: float, end_s: float, duration_s: float, parts: Sequence[HiddenPart]
) -> tuple[float, float] | None:
    """The same-length window nearest ``[start_s, end_s)`` that shows no part.

    Unchanged when it already shows none; None when no stretch of the clip is that long.
    """
    if not shows_hidden(start_s, end_s, duration_s, parts):
        return start_s, end_s
    length = end_s - start_s
    best: tuple[float, float] | None = None
    for lo, hi in allowed_spans(duration_s, parts):
        if hi - lo + _EPS < length:
            continue
        at = min(max(start_s, lo), hi - length)
        at = max(lo, math.ceil(at * 1000 - _EPS) / 1000)
        if best is None or abs(at - start_s) < abs(best[0] - start_s):
            best = (at, round(at + length, 6))
    return best


def check_windows(
    windows: Iterable[tuple[str, float, float]],
    durations: Mapping[str, float],
    parts: Iterable[HiddenPart],
) -> None:
    """Raise ``HiddenPartShown`` when any ``(media_id, start, end)`` window shows a part."""
    by_media = parts_by_media(parts)
    for media_id, start, end in windows:
        own = by_media.get(media_id)
        if not own:
            continue
        duration = durations.get(media_id)
        if duration is None:
            # An end part needs the clip's length; without it nothing proves the window safe.
            if any(part.edge == "end" for part in own):
                raise HiddenPartShown(f"no length to keep the end of {media_id} out")
            duration = math.inf
        if shows_hidden(start, end, duration, own):
            raise HiddenPartShown(
                f"a window {start:.3f}-{end:.3f}s of {media_id} shows a part the creator hid"
            )
