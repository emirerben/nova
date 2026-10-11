"""Sanitized, collector-compatible observations for creator length checks."""

from __future__ import annotations

import json
import logging
import os
import time
from typing import Literal

_logger = logging.getLogger("nova.length_check_observation")
_logger.setLevel(logging.INFO)
_logger.propagate = False
if not _logger.handlers:
    _handler = logging.StreamHandler()
    _handler.setFormatter(logging.Formatter("%(message)s"))
    _logger.addHandler(_handler)


def emit_length_check_observation(
    *, check_mode: Literal["log_only", "enforce"], outcome: Literal["passed", "declined"]
) -> dict[str, object]:
    """Emit one ID-free JSONL observation for external aggregation.

    The deployment's fleet proof binds an event stream to API and worker
    processes. A single event only reports the revision configured locally.
    """
    revision = os.environ.get("CREATOR_DURATION_CHECK_REVISION", "")
    event: dict[str, object] = {
        "event": "creator_length_check",
        "timestamp_epoch_s": int(time.time()),
        "check_mode": check_mode,
        "check_observations": 1,
        "check_declines": 1 if outcome == "declined" else 0,
        "api_revision": revision,
        "worker_revision": revision,
    }
    _logger.info(json.dumps(event, separators=(",", ":"), sort_keys=True))
    return event
