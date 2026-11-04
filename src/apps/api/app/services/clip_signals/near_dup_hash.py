"""``near_dup_hash``: a 64-bit dHash on three keyframes per clip (photos: the image itself).

Stub: registered (version 0, not implemented). Only the hashes are stored; Hamming distance and
capture-time proximity are computed per pair when a suggestion set is served.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from app.services.clip_signals.registry import SignalInputs, SignalSpec, register


def compute(inputs: SignalInputs) -> Mapping[str, Any] | None:
    return None


SPEC = register(
    SignalSpec(
        name="near_dup_hash",
        version=0,
        kinds=frozenset({"video", "image"}),
        compute=compute,
        implemented=False,
    )
)
