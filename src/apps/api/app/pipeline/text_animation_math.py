"""Renderer-neutral timing math for text animations.

Keep this module free of Pillow, Skia, and FFmpeg imports so classic and Skia
renderers share the exact same animation envelope.
"""

HANDWRITING_NOMINAL_DELAY_S = 0.2
HANDWRITING_NOMINAL_DRAW_S = 2.0
HANDWRITING_NOMINAL_SETTLE_S = HANDWRITING_NOMINAL_DELAY_S + HANDWRITING_NOMINAL_DRAW_S


def motion_cubic_bezier(t: float, x1: float, y1: float, x2: float, y2: float) -> float:
    """Evaluate a CSS cubic-bezier easing curve at progress ``t``."""
    target_x = max(0.0, min(1.0, t))
    if target_x <= 0.0:
        return 0.0
    if target_x >= 1.0:
        return 1.0

    def sample(axis_1: float, axis_2: float, u: float) -> float:
        inv = 1.0 - u
        return 3.0 * axis_1 * inv * inv * u + 3.0 * axis_2 * inv * u * u + u**3

    def sample_x(u: float) -> float:
        return sample(x1, x2, u)

    def sample_y(u: float) -> float:
        return sample(y1, y2, u)

    def sample_x_derivative(u: float) -> float:
        inv = 1.0 - u
        return 3.0 * x1 * inv * inv + 6.0 * (x2 - x1) * inv * u + 3.0 * (1.0 - x2) * u * u

    u = target_x
    for _ in range(8):
        error = sample_x(u) - target_x
        if abs(error) < 1e-6:
            return sample_y(u)
        derivative = sample_x_derivative(u)
        if abs(derivative) < 1e-6:
            break
        u = max(0.0, min(1.0, u - error / derivative))

    lower = 0.0
    upper = 1.0
    u = target_x
    for _ in range(12):
        if sample_x(u) < target_x:
            lower = u
        else:
            upper = u
        u = (lower + upper) / 2.0
    return sample_y(u)


def handwriting_progress(t_local: float, duration_s: float) -> float:
    """Return deterministic CSS-ease write-on progress for an overlay."""
    if duration_s <= 0:
        return 1.0
    timing_scale = min(1.0, duration_s / HANDWRITING_NOMINAL_SETTLE_S)
    delay_s = HANDWRITING_NOMINAL_DELAY_S * timing_scale
    draw_s = HANDWRITING_NOMINAL_DRAW_S * timing_scale
    normalized = (max(0.0, t_local) - delay_s) / max(0.001, draw_s)
    return motion_cubic_bezier(normalized, 0.25, 0.1, 0.25, 1.0)


def handwriting_settle_s(duration_s: float) -> float:
    return max(0.0, min(HANDWRITING_NOMINAL_SETTLE_S, duration_s))
