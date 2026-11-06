"""Pure render projection; never changes the creator's stored source window."""

import math

# Match NativeEditorRenderCompiler's source-window geometry tolerance. Saved
# end-minus-start arithmetic can add a few ULPs without asking for another frame.
SOURCE_WINDOW_TOLERANCE_S = 1e-6


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
    if span > source_duration + SOURCE_WINDOW_TOLERANCE_S:
        raise RenderWindowUnavailable(
            "The requested duration needs more footage than this source contains."
        )
    if start + span <= source_duration + SOURCE_WINDOW_TOLERANCE_S:
        return start
    if start_locked:
        raise RenderWindowUnavailable(
            "The pinned source moment cannot supply the requested duration."
        )
    return max(0.0, source_duration - span)


def phone_source_window(
    start: float,
    duration: float,
    rate: float,
    source_duration: float | None,
    *,
    start_locked: bool,
) -> tuple[float, float, float | None]:
    """Project a legacy phone window onto its immutable original source.

    Successful strict fits and reanchors retain their existing behavior. A
    missing explicit endpoint may represent the nearest 30fps output frame,
    not additional footage. Only that exact half-frame rounding case holds
    the source's final frame; authoring, playback rate and output clock stay
    unchanged. Callers must supply the bound original's measured duration.
    """
    try:
        effective = effective_source_start(
            start, duration, rate, source_duration, start_locked=start_locked
        )
        return effective, duration * rate, None
    except RenderWindowUnavailable:
        # The strict helper already validates finite positive geometry when
        # a duration exists. Missing measurements must remain recoverable.
        if source_duration is None:
            raise
        available = source_duration - start
        tail_output = available / rate
        padding = duration - tail_output
        if (
            available <= 0
            or not math.isfinite(tail_output)
            or not 0 < padding <= 1 / 60 + SOURCE_WINDOW_TOLERANCE_S
            or abs(round(tail_output * 30) / 30 - duration) > SOURCE_WINDOW_TOLERANCE_S
        ):
            raise
        return start, available, padding
