"""``audio_quality``: peak and clipping, low-band energy (wind) and the noise floor of a clip.

Stub: registered (version 0, not implemented) so the lane that owns it edits only this module.
Planned measurement: ffmpeg ``astats`` peak/clipping, energy below 200 Hz versus the full band,
and the noise floor in silent spans, with thresholds from a labeled calibration set.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from app.services.clip_signals.registry import SignalInputs, SignalSpec, register


def compute(inputs: SignalInputs) -> Mapping[str, Any] | None:
    return None


SPEC = register(
    SignalSpec(
        name="audio_quality",
        version=0,
        kinds=frozenset({"video"}),
        compute=compute,
        implemented=False,
    )
)
