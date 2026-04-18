"""Generate a TemplateRecipe-compatible structure from a MusicTrack's beat timestamps.

The recipe is a plain dict (not a SQLAlchemy model) that can be passed directly to
template_matcher.match() and _assemble_clips() — the same code path used by template jobs.

Key concepts:
  - slot_every_n_beats: a clip cut happens every N beats (default 8 ≈ 2 bars at 120 BPM)
  - best_start_s / best_end_s: the window of the track to use (auto-detected or admin-set)
  - _auto_best_section: finds the highest beat-density 45s window (proxy for chorus/drop)
"""

import math
from bisect import bisect_left, bisect_right

DEFAULT_WINDOW_S = 45.0
DEFAULT_SLOT_EVERY_N_BEATS = 8


def generate_music_recipe(track_data: dict) -> dict:
    """Build a recipe dict from a MusicTrack's stored config and beat timestamps.

    Args:
        track_data: dict with keys matching MusicTrack columns:
            - beat_timestamps_s: list[float]
            - track_config: dict with best_start_s, best_end_s, slot_every_n_beats, etc.
            - duration_s: float | None

    Returns:
        A recipe dict shaped like TemplateRecipe (JSON-serialisable).

    Raises:
        ValueError: if the beat window produces 0 slots (track too short / config mismatch).
    """
    beats: list[float] = track_data.get("beat_timestamps_s") or []
    cfg: dict = track_data.get("track_config") or {}
    duration_s: float = float(track_data.get("duration_s") or 0.0)

    start_s: float = float(cfg.get("best_start_s", 0.0))
    end_s: float = float(
        cfg.get("best_end_s", min(DEFAULT_WINDOW_S, duration_s or DEFAULT_WINDOW_S))
    )
    n: int = int(cfg.get("slot_every_n_beats", DEFAULT_SLOT_EVERY_N_BEATS))

    # Beats inside the configured window (inclusive on both ends)
    window_beats = sorted(b for b in beats if start_s <= b <= end_s)

    # Generate slots: every N beats → one clip cut
    slots = []
    for i in range(0, len(window_beats) - n, n):
        slot_start_s = window_beats[i] - start_s
        slot_end_s = window_beats[i + n] - start_s
        duration = slot_end_s - slot_start_s
        if duration <= 0:
            continue
        slots.append({
            "position": len(slots) + 1,
            "target_duration_s": round(duration, 3),
            "slot_type": "broll",
            "energy": 5.0,      # overridden by _enrich_slots_with_energy in orchestrator
            "priority": 5,
            "text_overlays": [],
            "transition_in": "cut",
            "speed_factor": 1.0,
        })

    if not slots:
        raise ValueError(
            f"Music recipe produced 0 slots: {len(window_beats)} beats in window "
            f"[{start_s:.1f}s–{end_s:.1f}s] with slot_every_n_beats={n}. "
            "Increase the window or decrease slot_every_n_beats."
        )

    # Adjust required_clips from config or derive
    n_slots = len(slots)
    req_min = int(cfg.get("required_clips_min", max(1, math.floor(n_slots / 2))))
    req_max = int(cfg.get("required_clips_max", n_slots))

    recipe = {
        "shot_count": n_slots,
        "total_duration_s": round(end_s - start_s, 3),
        "hook_duration_s": slots[0]["target_duration_s"] if slots else 0.0,
        "slots": slots,
        "beat_timestamps_s": [round(b - start_s, 3) for b in window_beats],
        "sync_style": "cut-on-beat",
        "pacing_style": "fast",
        "color_grade": "none",
        "transition_style": "cut",
        "copy_tone": "energetic",
        "caption_style": "none",
        "creative_direction": "beat-sync music video",
        "interstitials": [],
        "required_clips_min": req_min,
        "required_clips_max": req_max,
    }
    return recipe


def auto_best_section(
    beat_timestamps_s: list[float],
    window_s: float = DEFAULT_WINDOW_S,
    track_duration_s: float = 0.0,
) -> tuple[float, float]:
    """Find the window of *window_s* seconds with the highest beat density.

    Beat density is used as a proxy for the chorus/drop — the most energetic
    part of the song. Uses a sliding-window sweep over detected beat positions,
    so it only considers windows that start *at* a beat (not every 1s).

    This avoids librosa as a dependency.

    Args:
        beat_timestamps_s: sorted or unsorted list of beat timestamps in seconds.
        window_s: desired window length in seconds.
        track_duration_s: full track duration (used as fallback end cap).

    Returns:
        (best_start_s, best_end_s) — the best window boundaries.
    """
    if not beat_timestamps_s:
        end = min(window_s, track_duration_s) if track_duration_s > 0 else window_s
        return 0.0, end

    candidates = sorted(set(beat_timestamps_s))
    best_start: float = candidates[0]
    best_count: int = 0

    for start in candidates:
        end = start + window_s
        count = bisect_right(candidates, end) - bisect_left(candidates, start)
        if count > best_count:
            best_count = count
            best_start = start

    best_end = best_start + window_s

    # Cap to track duration if known
    if track_duration_s > 0 and best_end > track_duration_s:
        best_end = track_duration_s
        # If capping makes the window shorter than desired, backtrack start
        best_start = max(0.0, best_end - window_s)

    return best_start, best_end
