"""Pure render projection; never changes the creator's stored source window."""

import math


class RenderWindowUnavailable(ValueError):
    """Admitted finite authoring intent needs a media/window recovery action."""


def effective_source_start(
    start: float,
    duration: float,
    rate: float,
    source_duration: float | None,
    *,
    start_locked: bool,
) -> float:
    """Choose the nearest real window while keeping output duration and rate.

    Callers must resolve explicit moment/start constraints before setting
    ``start_locked=False``. Missing footage and spans longer than the source
    require recovery, not duplicated frames or a silently shorter timeline.
    """
    if source_duration is None:
        raise RenderWindowUnavailable(
            "Source duration is unavailable; reconnect the original clip."
        )
    if not all(math.isfinite(v) for v in (start, duration, rate, source_duration)):
        raise ValueError("Source window must contain finite values.")
    if start < 0 or duration <= 0 or rate <= 0 or source_duration <= 0:
        raise ValueError("Source window must have a nonnegative start and positive duration/rate.")
    span = duration * rate
    if span > source_duration:
        raise RenderWindowUnavailable(
            "The requested duration needs more footage than this source contains."
        )
    if start + span <= source_duration:
        return start
    if start_locked:
        raise RenderWindowUnavailable(
            "The pinned source moment cannot supply the requested duration."
        )
    return source_duration - span
