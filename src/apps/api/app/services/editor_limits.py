"""Shared limits for the guided editor and Creator Block runtime.

The browser and API both consume ``src/packages/motion-runtime/motion-limits.json``.
The production image copies that package to ``/app/motion-runtime``; the source
tree fallback keeps local tests on the same contract.
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path


def _source_tree_limits_path(source: Path) -> Path | None:
    for parent in source.parents:
        candidate = parent / "packages" / "motion-runtime" / "motion-limits.json"
        if candidate.is_file():
            return candidate
    return None


def _limits_path() -> Path:
    candidates = [Path("/app/motion-runtime/motion-limits.json")]
    source_candidate = _source_tree_limits_path(Path(__file__).resolve())
    if source_candidate is not None:
        candidates.append(source_candidate)
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    raise RuntimeError("motion runtime limits are missing")


@lru_cache(maxsize=1)
def _limits() -> dict[str, int]:
    raw = json.loads(_limits_path().read_text())
    required = (
        "motion_fps",
        "timeline_max_slots",
        "motion_max_instances",
        "motion_max_instance_seconds",
        "motion_max_active_seconds",
        "motion_max_concurrent_complexity",
        "motion_max_complexity_multiplier",
    )
    if any(type(raw.get(key)) is not int or raw[key] <= 0 for key in required):
        raise RuntimeError("motion runtime limits are invalid")
    return {key: int(raw[key]) for key in required}


EDITOR_MAX_TIMELINE_SLOTS = _limits()["timeline_max_slots"]
MOTION_FPS = _limits()["motion_fps"]
MOTION_MAX_INSTANCES = _limits()["motion_max_instances"]
MOTION_MAX_INSTANCE_FRAMES = _limits()["motion_max_instance_seconds"] * MOTION_FPS
MOTION_MAX_ACTIVE_FRAMES = _limits()["motion_max_active_seconds"] * MOTION_FPS
MOTION_MAX_CONCURRENT_COMPLEXITY = _limits()["motion_max_concurrent_complexity"]
MOTION_MAX_COMPLEXITY_MULTIPLIER = _limits()["motion_max_complexity_multiplier"]
MOTION_MAX_COMPLEXITY_UNITS = MOTION_MAX_ACTIVE_FRAMES * MOTION_MAX_COMPLEXITY_MULTIPLIER
