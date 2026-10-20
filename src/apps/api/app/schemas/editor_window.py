"""The one rule for how short an editor clip window may be (KRI-770).

Every Save path (authored phone, guided v2, cloud) used to compare a duration against a bare
``0.1`` with ``<``. The phone computes a trimmed window as ``(in_s + 0.1) - in_s``, which is
``0.09999999999999998`` for many ``in_s`` values, so a clip the phone's own minimum-length drag
produced was refused with a code the app showed as "This save was rejected".

Compare with these helpers instead of a literal. Float noise below ``EDITOR_WINDOW_EPSILON_S``
is not a violation; a real shortfall (90 ms) still is.
"""

from __future__ import annotations

EDITOR_MIN_CLIP_S = 0.1
EDITOR_WINDOW_EPSILON_S = 1e-6


def below_editor_minimum(duration: float, minimum: float = EDITOR_MIN_CLIP_S) -> bool:
    """True when ``duration`` is really shorter than ``minimum`` (not just float noise)."""
    return duration < minimum - EDITOR_WINDOW_EPSILON_S


def snap_to_editor_minimum(duration: float, minimum: float = EDITOR_MIN_CLIP_S) -> float:
    """``minimum`` exactly when ``duration`` only misses it by float noise, else ``duration``.

    Call this right after the check so every later stage (source fit, phone compiler) sees the
    same legal value instead of re-deriving the noisy one.
    """
    if minimum - EDITOR_WINDOW_EPSILON_S <= duration < minimum:
        return minimum
    return duration
