"""
Phase duration baselines — advisory hand-seeded values for ETA display.
Seeded from prod pipeline_trace timings (2026-06-06).
These are ±30% estimates; the D18 ETA ladder absorbs imprecision.
Refresh: extend aggregate_phase_timings.py with a phase_log DB reader
once PR2 instrumentation has soaked (see TODOS.md).
"""

from __future__ import annotations

# Per-phase baseline durations in milliseconds.
# render_variants scales by pending variant count (see scale_render_variants).
GENERATIVE_PHASE_BASELINES_MS: dict[str, int] = {
    "analyze_clips": 45_000,
    "match_song": 15_000,
    "render_variants": 90_000,  # assumes 3 variants @ ~30s each
    "finalize": 10_000,
}

CONTENT_PLAN_ACTIVATION_BASELINES_MS: dict[str, int] = {
    "matching_clips": 75_000,
    "picking_days": 10_000,
    "starting_renders": 35_000,
}


def get_baselines(pipeline: str = "generative") -> dict[str, int] | None:
    """Return baseline map for the given pipeline, or None if unknown."""
    if pipeline == "generative":
        return dict(GENERATIVE_PHASE_BASELINES_MS)
    if pipeline == "content_plan_activation":
        return dict(CONTENT_PLAN_ACTIVATION_BASELINES_MS)
    return None


def scale_render_variants(baselines: dict[str, int], pending_count: int) -> dict[str, int]:
    """Scale the render_variants baseline by actual pending variant count vs assumed 3."""
    out = dict(baselines)
    if "render_variants" in out and pending_count > 0:
        out["render_variants"] = int(out["render_variants"] * pending_count / 3)
    return out
