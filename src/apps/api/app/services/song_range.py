"""The stretch of the song a creator filmed in (KRI-580).

Most songs repeat their verses. A take's words match the song at EVERY repeat, so takes filmed in
one stretch of the song get placed at different repeats, far apart, and the planner splits the
group (it keeps one side and drops the rest). The creator knows where they were in the song; when
they say so, a take is only matched inside that stretch.

Applied where alignment is READ (the planner gate and the render worker), never when it is written:
the stored candidates stay complete, so changing or clearing the range takes effect immediately and
needs no re-alignment.

Pure: no I/O.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

from app.schemas.user_song import (
    AlignmentAlternate,
    PlacementCandidate,
    SongAlignment,
    TakeAlignment,
)

# A take's song position is where the TAKE STARTS, and creators film with a run-up or a late start,
# so a candidate a little outside the stated stretch still counts. Wider than any run-up, narrower
# than the distance between two repeats of a verse.
SONG_RANGE_TOLERANCE_S = 3.0


@dataclass(frozen=True)
class SongRange:
    start_s: float
    end_s: float

    def contains_start(self, delta_s: float, tolerance_s: float = SONG_RANGE_TOLERANCE_S) -> bool:
        return self.start_s - tolerance_s <= delta_s <= self.end_s + tolerance_s


def parse_song_range(start_s: Any, end_s: Any) -> SongRange | None:
    """A valid range, or None when unset or malformed (never raises: it gates nothing)."""
    for value in (start_s, end_s):
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
        ):
            return None
    start, end = float(start_s), float(end_s)
    if start < 0 or end <= start:
        return None
    return SongRange(start, end)


def song_range_of(item: Any) -> SongRange | None:
    """The range stored on a plan item, if any (ignored while the kill switch is off)."""
    from app.config import settings  # noqa: PLC0415

    if not settings.song_filming_range_enabled:
        return None
    return parse_song_range(
        getattr(item, "song_range_start_s", None), getattr(item, "song_range_end_s", None)
    )


def _row_in_range(row: TakeAlignment, song_range: SongRange, cfg: Any) -> TakeAlignment:
    candidates = row.candidates_or_legacy()
    kept = [c for c in candidates if song_range.contains_start(c.delta_s)]
    if len(kept) == len(candidates):
        return row  # nothing outside the range: byte-identical row
    if not kept:
        # Nothing it could be inside the stretch: no evidence, instead of snapping to a far repeat.
        return TakeAlignment(
            media_id=row.media_id,
            proxy_generation=row.proxy_generation,
            status="unmatched",
            delta_s=None,
            confidence=min(row.confidence, 0.3),
            text_score=row.text_score,
            peak_z=row.peak_z,
            peak_ratio=row.peak_ratio,
        )
    ranked = sorted(kept, key=lambda c: -c.likelihood)
    best = ranked[0]
    margin = (
        1.0
        if len(ranked) == 1
        else min(1.0, max(0.0, (best.likelihood - ranked[1].likelihood) / best.likelihood))
    )
    uncertain = margin < float(cfg.song_align_ask_margin) or best.likelihood < float(
        cfg.song_align_ask_likelihood
    )
    has_match = (
        best.match_start_s is not None
        and best.match_end_s is not None
        and best.match_end_s > best.match_start_s
    )
    extra: dict[str, Any] = (
        {"match_start_s": best.match_start_s, "match_end_s": best.match_end_s} if has_match else {}
    )
    return TakeAlignment(
        media_id=row.media_id,
        proxy_generation=row.proxy_generation,
        status="ambiguous" if uncertain else "confident",
        delta_s=best.delta_s,
        confidence=min(1.0, max(0.0, best.likelihood)),
        text_score=row.text_score,
        peak_z=row.peak_z,
        peak_ratio=row.peak_ratio,
        alternates=(
            [AlignmentAlternate(delta_s=c.delta_s, score=c.likelihood) for c in ranked[:4]]
            if uncertain
            else []
        ),
        method=best.method if best.method in ("audio", "lyrics") else row.method,
        likelihood=min(1.0, max(0.0, best.likelihood)),
        margin=margin,
        candidates=[PlacementCandidate.model_validate(c.model_dump()) for c in ranked],
        **extra,
    )


def restrict_alignment_to_range(
    alignment: SongAlignment, song_range: SongRange | None, cfg: Any | None = None
) -> SongAlignment:
    """``alignment`` with every take's candidates cut to the creator's stretch of the song.

    ``None`` (no range) returns the alignment unchanged. A take left with no candidate inside the
    stretch becomes unmatched; one left with several is re-ranked and re-derived like the aligner
    does (margin, status, alternates), so everything downstream reads a consistent row.
    """
    if song_range is None:
        return alignment
    if cfg is None:
        from app.config import settings as cfg  # noqa: PLC0415
    rows = {
        media_id: _row_in_range(row, song_range, cfg) for media_id, row in alignment.takes.items()
    }
    if all(rows[m] is alignment.takes[m] for m in rows):
        return alignment
    return alignment.model_copy(update={"takes": rows})
