"""Measured per-clip signals for suggestion blocks. Importing the package registers every signal.

Stored next to the clip analysis, not inside ``ClipUnderstanding``. A backfill
(``app.cli.backfill_clip_signals``) fills clips that are already understood, because
``kria_clip_understanding._needs_understanding`` skips those.
"""

from __future__ import annotations

from app.services.clip_signals import audio_quality, near_dup_hash  # noqa: F401 - registration
from app.services.clip_signals.registry import (
    SIGNALS_KEY,
    SignalInputs,
    SignalSpec,
    compute_signals,
    merge_signals,
    missing_signals,
    names,
    read_signals,
    signals_version,
    specs,
)

__all__ = [
    "SIGNALS_KEY",
    "SignalInputs",
    "SignalSpec",
    "compute_signals",
    "merge_signals",
    "missing_signals",
    "names",
    "read_signals",
    "signals_version",
    "specs",
]
