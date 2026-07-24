"""Shared spacing guard for automatically placed sound effects."""

from __future__ import annotations

from typing import Any

MIN_AUTO_SFX_SPACING_S = 1.5


def _at_s(row: dict[str, Any]) -> float:
    try:
        return max(0.0, float(row.get("at_s", 0.0)))
    except (TypeError, ValueError):
        return 0.0


def normalize_auto_sfx_placements(
    candidates: list[dict[str, Any]],
    *,
    protected: list[dict[str, Any]] | None = None,
    min_spacing_s: float = MIN_AUTO_SFX_SPACING_S,
) -> list[dict[str, Any]]:
    """Drop automatic SFX candidates that collide with protected or kept placements.

    Manual editor writes should not use this helper; it is for generated SFX from
    Smart Captions and AI overlay suggestions, where the system should avoid
    creating inaudible stacked effects.
    """

    protected_times = sorted(_at_s(row) for row in (protected or []))
    kept: list[dict[str, Any]] = []
    kept_times: list[float] = []
    for row in sorted(candidates, key=lambda item: (_at_s(item), str(item.get("id") or ""))):
        at_s = _at_s(row)
        if any(abs(at_s - existing) < min_spacing_s for existing in protected_times):
            continue
        if any(abs(at_s - existing) < min_spacing_s for existing in kept_times):
            continue
        kept.append(row)
        kept_times.append(at_s)
    return kept
